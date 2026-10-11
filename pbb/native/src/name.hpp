#pragma once
// ============================================================================
// name.hpp — RC4-like 名字状态机 (PBB 核心评分引擎)
//
// Name 结构体实现了基于 RC4 流密码变体的名字评分算法:
//
//   评分流程:
//     1. load_team()     → RC4 KSA 用队伍名初始化 val_base
//     2. load_prefix()   → 部分 KSA 预处理共享前缀 (性能优化)
//     3. load_name()     → 完整 KSA + SIMD 属性评分 → 计算 V 值
//     4. calc_skills()   → RC4 PRGA 生成技能分布
//     5. score_full()    → (在 scoring_1035.hpp) 封装 3+4 + 模型评分
//
//   V 值 (八围评分) 计算:
//     V = Σ 7 个属性的中位数 + HP/3 加成
//     属性从 ual 数组过滤后 (89 <= ual < 217) 的 name_base 中取中位数
//     包含三级渐进式早退: V < 24, V < 165, V < 250
//
//   对齐: alignas(64) 确保 AVX2 加载对齐到 cache line 边界
//
// 中文补充 (跨文件契约, 改动前必读):
//   * 结构体布局随编译期宏变化: PBB_HAS_SIMD 打开时会额外带上 ual_skills/saved_val/
//     prefix_loaded 三个成员。也就是说 SIMD 档与标量档的对象不二进制兼容, 不能跨二进制
//     传递或持久化; 但在同一份二进制内, 结果必须与线程数/调度顺序无关 (确定性要求)。
//   * q_len 采用「-1 起点 + 前置自增」约定: 有效数据是 name_base[0..q_len], 写入永远是
//     name_base[++q_len], 因此 q_len 的初值必须是 -1 (空集), 忘记重置会造成脏数据。
//   * V 与 sum 的区别: V 只是 7 个属性中位数之和 (不含 HP 的 154 基值); 对外汇报的
//     「八围总分」是 sum = V + 36*7 (7 个属性各补 36 的展示偏移), 见 scoring_1124.hpp。
//   * 三级早退只省计算不改结果: 每一级累加的都是非负中位数 (值域 0..63), 所以提前返回
//     得到的 V 与「无条件算到底」逐位相同 —— 早退是纯粹的剪枝, 不是近似。
//   * 副作用可复用: simd_mul_add_dual_filter 在早退之前就已把 name_base[0..30] 填好,
//     因此上层门控 (gate_level_of) 可以直接复用; 唯一例外是 V<250 提前 return 时
//     没做 sort10, 上层若要用 HP (name_base[3..6]) 必须自己补一次排序。
// ============================================================================
#include "common.hpp"
#include "utils.hpp"

struct alignas(64) Name {
  // ---- RC4 状态数组 ----
  alignas(64) u8_t val[N];          // RC4 S-box (256 字节, 对齐到 64 字节)
  alignas(64) u8_t ual[N];          // val 的线性变换结果 (val*181+160) & 0xFF
#if PBB_HAS_SIMD
  alignas(64) u8_t ual_skills[N];   // 技能评分用变换 (val*181+71) & 0xFF
  u8_t saved_val[256];              // AVX2 优化: 保存 load_prefix 后的 val 状态
                                    // 语义: 「PRELEN 前缀 KSA 已完成」的快照。有了它,
                                    // 每个候选就不必再从 val_base2 重跑前缀部分。
  bool prefix_loaded;               // 是否已执行 load_prefix (用于 AVX2 快速恢复)
                                    // 未置位时一律退回 val_base2 (未做前缀的干净副本)。
#endif
                                    // 注意: ual/ual_skills 是「一次候选的中间量」,
                                    // 跨调用会被覆盖, 不可当作持久状态使用。

  // ---- KSA 基础状态 ----
  u8_t val_base[N];                 // 队伍名 KSA 后的初始 S-box (所有名字共享)
                                    // 由 load_team() 生成, 是整条评分链的根: 换队伍 = 换全部结果。
  u8_t val_base2[N];                // load_prefix 修改后的 S-box 副本
                                    // 每候选在做前缀 KSA 前从 val_base 完整复制一份, 保证
                                    // val_base 永不被污染 (下一个候选还要用它)。

  // ---- 复用标志 ----
  bool _ksa_done = false;           // load_name_quad 已做完 KSA 时跳过重做
                                    // 约定: quad 路径跑完 KSA 后置 true, 紧接着的 load_name()
                                    // 只做属性/技能收尾; load_name 读完立刻清回 false, 防止
                                    // 状态泄漏到下一个候选。

  // ---- 属性/技能缓冲区 ----
  u8_t name_base[M];                // 过滤后的 ual 值 (低 6 位, 最多 30 个有效值)
                                    // 分区约定: [0..30] 属性 (7 个三元组 + 高位组),
                                    //            [K..K+63]=[64..127] 技能;
                                    // 两个区互不重叠, 因此可以先算属性再被 calc_skills 覆盖技能区。
  u8_t freq[16];                    // 16 组技能的频次分布
  u8_t skill[skill_cnt];            // 技能排列 (40 个技能 ID 的随机排列)

  // ---- RC4 状态变量 ----
  u8_t p, q;                        // RC4 PRGA 的 i, j 指针
                                    // 声明为 u8_t 是刻意的: 自增溢出即等价于 &255 回绕。
  u8_t i_pre, j_pre;                // load_prefix 保存的中间状态
  u8_t s_pre;                       // load_prefix 保存的累加和

  // ---- 派生属性 ----
  int q_len;                        // name_base 中有效值的数量 (0-30)
                                    // 约定: 初值 -1, 只通过 ++q_len 写入 → 有效下标 0..q_len。
  int V;                            // 八围评分 (load_name 计算)
                                    // 仅 7 个属性中位数之和, 不含 154/36 之类展示偏移。
  int seed;                         // 种子 (未使用, 保留)
  int PRELEN;                       // load_prefix 预处理的名字字节数
                                    // 由调用方按「所有候选共同的最长前缀字节数」设置; 设得越大
                                    // 单候选省得越多, 但只有在候选确实共享该前缀时才正确。
  int NAMELEN;                      // 名字总长度
                                    // 注意: KSA 密钥长度按 NAMELEN+1 计算 (含结尾 NUL),
                                    // 所以 name[nlen] 必须可读且为 0 (engine.hpp 有专门保证)。

  // ===== m(): RC4 PRGA 单步 =====
  // 标准 RC4: i++, j = (j + S[i]), swap(S[i], S[j]), 输出 S[(S[i]+S[j]) & 255]
  // 与教科书 RC4 的差别仅在索引写法: p/q 是 u8_t, 自增/相加天然 mod 256。
  // 调用方必须保证 p/q 已被重置为 0 (calc_skills 会显式 p = q = 0)。
  inline u8_t m() {
    q += val[++p];
    std::swap(val[p], val[q]);
    return val[(val[p] + val[q]) & 255];
  }

  // ===== gen(): 生成技能索引 =====
  // 两次 PRGA 输出拼接为 16 位，对 skill_cnt (40) 取模得到 0-39 的技能 ID
  // 取模而非掩码: 40 不是 2 的幂, 因此低位/高位分布不均 (算法原样如此, 属于评分语义)。
  inline int gen() {
    int u = m();
    return (u << 8 | m()) % skill_cnt;
  }

  // ===== load_team(): 队伍名 KSA (密钥调度) =====
  // 用队伍名字节初始化 val_base[0..255] 为 0..255 的随机排列。
  // 使用标准 RC4 KSA: 对每个 i, s += key[i % keylen] + S[i], swap(S[i], S[s])
  // 密钥细节: t_len = strlen(team) + 1, 即把结尾 NUL 也算作一个密钥字节;
  //   且循环写成 `if (i % t_len) s += _team[i % t_len - 1];` —— 当 i%t_len==0 时
  //   本次「不加密钥字节」, 等价于密钥序列的第 0 位被跳过 (原版 pbb 行为, 不要"修正")。
  // 副作用: 只改 val_base, 不动 val; 必须在所有线程启动之前调用一次, 之后共享只读。
  void load_team(const char *_team) {
    int t_len = strlen(_team) + 1;
    u8_t s;
    for (int i = 0; i < N; i++)
      val_base[i] = i;
    for (int i = s = 0; i < N; ++i) {
      if (i % t_len)
        s += _team[i % t_len - 1];
      s += val_base[i];
      std::swap(val_base[i], val_base[s]);
    }
  }

  // ===== load_prefix(): 部分 KSA (共享前缀预处理) =====
  // 对名字的前 PRELEN 字节执行 KSA，避免为每个名字重复处理固定前缀。
  // 从 NAMELEN 位置开始循环读取名字字节 (模拟 RC4 密钥循环)。
  // 保存 i_pre/j_pre/s_pre 供 load_name 接续。
  // AVX2 优化: 额外保存 saved_val 副本以加速 load_name 恢复。
  // 前置条件: 调用方须先设置 this->PRELEN (共同前缀的字节数) 且 name 至少有 name_len+1 字节。
  // 边界: PRELEN==0 时循环不执行, i_pre==0, 此时 j_pre/s_pre 被显式重置为
  //   (name_len, 0) —— 这是为了让后续 KSA 从「密钥末尾」而不是密钥中部开始。
  // 关键不变量: val_base 不被修改, 所有写入都落在 val_base2 的副本上。
  void load_prefix(const char *name, int name_len) {
    memcpy(val_base2, val_base, sizeof val_base2);
    NAMELEN = name_len;
    int i, j;
    u8_t s;
    for (i = s = 0, j = NAMELEN; i < PRELEN; i++, j++) {
      s += name[j] + val_base2[i];
      std::swap(val_base2[i], val_base2[s]);
      if (j == NAMELEN)
        j = -1;
    }
    i_pre = i;
    j_pre = j;
    s_pre = s;
    if (i_pre == 0)
      j_pre = name_len, s_pre = 0;
#if PBB_HAS_SIMD
    memcpy(saved_val, val_base2, sizeof saved_val);
    prefix_loaded = true;
#endif
  }

  // ===== advance_ksa_n(): 从 load_prefix 保存的状态再推进 n 字节 KSA =====
  // 用于 per-batch 动态前缀优化：同一 batch 内 4 候选的额外共同前缀只需推进一次。
  // 语义与 load_name_batch 第一遍循环的前 n 次迭代完全一致。
  // 依赖 saved_val/prefix_loaded (仅 SIMD 档有), 且调用方 (engine.hpp) 也在 SIMD 档内。
  // 注意它会「推进」i_pre/s_pre 并刷新 saved_val, 即改变了对象的前缀状态;
  //   v2_drop_batchup 实测该函数在生产调用点恒为 n=0 (adv=0), 因此当前 engine.hpp 已不再调用它,
  //   但保留实现以维持历史版本/工具的语义可对照。
#if PBB_HAS_SIMD
  void advance_ksa_n(const char* name, int nlen, int n) {
    memcpy(val, saved_val, sizeof val);
    u8_t s = s_pre;
    int j = j_pre;
    for (int i = i_pre; i < i_pre + n && i < N; i++, j++) {
      s += name[j] + val[i];
      std::swap(val[i], val[s]);
      if (j == nlen) j = -1;
    }
    i_pre += n; if (i_pre > N) i_pre = N;
    j_pre = j;
    s_pre = s;
    memcpy(saved_val, val, sizeof saved_val);
  }
#endif

  // ===== load_name(): 完整 KSA + 属性评分 (AVX2 路径) =====
  // 从 load_prefix 保存的状态接续执行 KSA，两遍循环 (标准 RC4 双 KSA)。
  // 然后用 SIMD 计算 ual 数组，过滤出 name_base 并累加 V 值。
  //
  // V 值计算分级 (渐进式早退):
  //   第 1 级 (V<24 直接返回): 28/29/30 位中位数 (对应高位属性)
  //   第 2 级 (V<165 直接返回): +13/14/15 + 16/17/18 + 25/26/27 中位数
  //   第 3 级 (V<250 直接返回): +10/11/12 + 19/20/21 + 22/23/24 中位数
  //   第 4 级: 排序后 + HP 加成 = (154 + sorted[3..6]) / 3
  //
  // AVX2 优化: 使用 prefix_loaded 标志选择从 saved_val 快速恢复
  // 参数: name_len_hint 非 0 时覆盖 NAMELEN (对应「本批候选的真实长度」), 便于 solver
  //   在不改对象字段的前提下换用不同长度的缓冲区。
  // _ksa_done 语义: 若上一步已由 load_name_quad* 完成 KSA, 这里跳过两遍 KSA 只做收尾,
  //   并且无论走哪条分支都会把它清回 false (一次性消费)。
  // 两遍循环的必要性: 第一遍是「前缀剩余段」, 第二遍是完整的 0..N 二次 KSA (标准 RC4 双轮),
  //   两遍都用 `if (j == _NAMELEN) j = -1;` 实现密钥循环 —— 因 j 在下一轮先自增,
  //   -1 后即为 0, 与 % (len+1) 的密钥循环等价。
  // 早退位置: finish_load_name 内部, 见其注释。
#if PBB_HAS_SIMD
  void load_name(const char *name, int name_len_hint = 0) {
    if (!_ksa_done) {
      u8_t s = s_pre;
      int _NAMELEN = name_len_hint ? name_len_hint : NAMELEN;
      memcpy(val, prefix_loaded ? saved_val : val_base2, sizeof val);
      for (int i = i_pre, j = j_pre; i < N; i++, j++) {
        s += name[j] + val[i];
        std::swap(val[i], val[s]);
        if (j == _NAMELEN) j = -1;
      }
      s = 0;
      for (int i = 0, j = _NAMELEN; i < N; i++, j++) {
        s += name[j] + val[i];
        std::swap(val[i], val[s]);
        if (j == _NAMELEN) j = -1;
      }
    }
    _ksa_done = false;
    finish_load_name();
  }

  template<int W>
  static void load_name_batch(Name (&lanes)[W], const char *const (&names)[W], int name_len) {
    // 交错语义: W 个 lane 共用同一个 S-box 索引 i 与同一个密钥位置 j, 只是各自的密钥字节
    // 和各自的 S-box 不同 —— 这样密钥回绕判断只算一次, 摊薄到 W 个候选。
    // 前置条件 (调用方必须保证): 所有 lane 的 i_pre/j_pre/s_pre/PRELEN 相同
    //   (engine.hpp 在批量前把 lane0 的前缀状态整体复制给其余 lane)。
    // name_len 是「候选缓冲区的可变部分长度」, 密钥长度按 name_len+1 处理 (含 NUL),
    //   故 names[lane][name_len] 必须是 0。
    // 该函数是 load_name_batch4 的通用版本; 生产热路径走 4 路特化。
    u8_t s[W];
    for (int lane = 0; lane < W; lane++) {
      memcpy(lanes[lane].val, lanes[lane].prefix_loaded ? lanes[lane].saved_val : lanes[lane].val_base2,
             sizeof lanes[lane].val);
      s[lane] = lanes[lane].s_pre;
    }

    int j = lanes[0].j_pre;
    const int wrap = name_len + 1;
    for (int i = lanes[0].i_pre; i < N; i++) {
      for (int lane = 0; lane < W; lane++) {
        s[lane] += names[lane][j] + lanes[lane].val[i];
        std::swap(lanes[lane].val[i], lanes[lane].val[s[lane]]);
      }
      j++;
      j = (j == wrap) ? 0 : j;
    }

    for (int lane = 0; lane < W; lane++) s[lane] = 0;
    j = name_len;
    for (int i = 0; i < N; i++) {
      for (int lane = 0; lane < W; lane++) {
        s[lane] += names[lane][j] + lanes[lane].val[i];
        std::swap(lanes[lane].val[i], lanes[lane].val[s[lane]]);
      }
      j++;
      j = (j == wrap) ? 0 : j;
    }

    for (int lane = 0; lane < W; lane++) lanes[lane].finish_load_name();
  }

  // W=4 特化: 尝试用 __restrict__ 指针提示编译器无别名
  // 这是 mode 1/2/4 顺序批量的主热函数 (每 4 个候选一次 KSA)。
  // 两条分支必须逐值等价:
  //   PBB_KSA_SEGMENTED=1 (GCC native 默认): 把「逐字节判断 j 是否回绕」改写成
  //     「按段推进」—— 段长 take = min(S-box 剩余字节, 密钥剩余字节含 NUL), 段内 j 单调递增
  //     不回绕, 因此编译器可以把内层循环向量化/展开而不必每步测试 j==name_len。
  //     段边界处 j 归 0, 与原来的 -1 后自增语义一致。
  //   PBB_KSA_SEGMENTED=0: 原始逐字节写法, 作为标量/可移植回退与正确性基线。
  //   #pragma GCC unroll 1 只在这两个变长循环上关闭展开: 展开会显著抬高寄存器与代码压力,
  //   而函数其余部分仍受全局 -funroll-loops 影响。
  // 两个分支都以 lane 循环在最内层, 保证「同一 i 上 4 个 lane 依次交换」的顺序不变 ——
  // 这决定了结果可复现性, 不可重排。
  static void load_name_batch4(Name (&lanes)[4], const char *const (&names)[4], int name_len) {
    u8_t s[4];
    for (int lane = 0; lane < 4; lane++) {
      memcpy(lanes[lane].val, lanes[lane].prefix_loaded ? lanes[lane].saved_val : lanes[lane].val_base2,
             sizeof lanes[lane].val);
      s[lane] = lanes[lane].s_pre;
    }

#if defined(PBB_KSA_SEGMENTED) && PBB_KSA_SEGMENTED
    // Native GCC: move key wrap to segment boundaries. Each segment consumes
    // min(remaining S-box bytes, remaining key bytes including the NUL).
    // Keep the per-lane swaps and byte arithmetic in their original order.
    // Unrolling these variable-length loops increases register/code pressure;
    // only these two loops opt out, retaining global -funroll-loops elsewhere.
    { int i = lanes[0].i_pre, j = lanes[0].j_pre;
      while (i < N) {
        const int take = std::min(N - i, name_len + 1 - j);
#pragma GCC unroll 1
        for (int k = 0; k < take; k++) {
          for (int lane = 0; lane < 4; lane++) {
            s[lane] += names[lane][j + k] + lanes[lane].val[i + k];
            std::swap(lanes[lane].val[i + k], lanes[lane].val[s[lane]]);
          }
        }
        i += take;
        j = 0;
      }
    }
    for (int lane = 0; lane < 4; lane++) s[lane] = 0;
    { int i = 0, j = name_len;
      while (i < N) {
        const int take = std::min(N - i, name_len + 1 - j);
#pragma GCC unroll 1
        for (int k = 0; k < take; k++) {
          for (int lane = 0; lane < 4; lane++) {
            s[lane] += names[lane][j + k] + lanes[lane].val[i + k];
            std::swap(lanes[lane].val[i + k], lanes[lane].val[s[lane]]);
          }
        }
        i += take;
        j = 0;
      }
    }
#else
    int j = lanes[0].j_pre;
    for (int i = lanes[0].i_pre; i < N; i++, j++) {
      for (int lane = 0; lane < 4; lane++) {
        s[lane] += names[lane][j] + lanes[lane].val[i];
        std::swap(lanes[lane].val[i], lanes[lane].val[s[lane]]);
      }
      if (j == name_len) j = -1;
    }

    for (int lane = 0; lane < 4; lane++) s[lane] = 0;
    j = name_len;
    for (int i = 0; i < N; i++, j++) {
      for (int lane = 0; lane < 4; lane++) {
        s[lane] += names[lane][j] + lanes[lane].val[i];
        std::swap(lanes[lane].val[i], lanes[lane].val[s[lane]]);
      }
      if (j == name_len) j = -1;
    }

#endif
    for (int lane = 0; lane < 4; lane++) lanes[lane].finish_load_name();
  }

  // ===== load_name_quad(): 四候选交错 RC4 KSA (无共享 key) =====
  // 四个候选 (a,b,c,d) 在同一次 i 循环里各自推进 KSA。之所以要"交错"而不是分别调用:
  //   四个 S-box 共处一组 cache line 内轮转, 隐藏 RC4 固有的依赖链延迟
  //   (每次 swap 依赖上一次的 s)。总指令数不变, 但访存并行度更高。
  // 前置条件: 四个 Name 对象的 PRELEN 必须相同 (都做过 load_prefix), 否则 i_pre/j_pre
  //   不一致会导致结果错误。
  // 收尾约定: 只做完 KSA 并置 _ksa_done=true; 属性/技能计算留给随后的
  //   finish_load_name / load_name (后者见 _ksa_done 会跳过 KSA)。
  // 与 load_name_quad_shared_key 的区别: 本函数每个 lane 都用各自的密钥字节 (通用);
  //   后者假定四者在前面若干字节上完全相同。结果必须一致, 只是省 key load。
  void load_name_quad(const char *a, const char *b, const char *c, const char *d,
                       int nlen, Name& ob, Name& oc, Name& od) {
    q_len = -1; ob.q_len = -1; oc.q_len = -1; od.q_len = -1;
    memcpy(val, prefix_loaded ? saved_val : val_base2, sizeof val);
    memcpy(ob.val, ob.prefix_loaded ? ob.saved_val : ob.val_base2, sizeof ob.val);
    memcpy(oc.val, oc.prefix_loaded ? oc.saved_val : oc.val_base2, sizeof oc.val);
    memcpy(od.val, od.prefix_loaded ? od.saved_val : od.val_base2, sizeof od.val);
    u8_t sa = s_pre, sb = s_pre, sc = s_pre, sd = s_pre;
    for (int i = i_pre, j = j_pre; i < N; i++, j++) {
      sa += a[j] + val[i]; std::swap(val[i], val[sa]);
      sb += b[j] + ob.val[i]; std::swap(ob.val[i], ob.val[sb]);
      sc += c[j] + oc.val[i]; std::swap(oc.val[i], oc.val[sc]);
      sd += d[j] + od.val[i]; std::swap(od.val[i], od.val[sd]);
      if (j == nlen) j = -1;
    }
    sa = 0; sb = 0; sc = 0; sd = 0;
    for (int i = 0, j = nlen; i < N; i++, j++) {
      sa += a[j] + val[i]; std::swap(val[i], val[sa]);
      sb += b[j] + ob.val[i]; std::swap(ob.val[i], ob.val[sb]);
      sc += c[j] + oc.val[i]; std::swap(oc.val[i], oc.val[sc]);
      sd += d[j] + od.val[i]; std::swap(od.val[i], od.val[sd]);
      if (j == nlen) j = -1;
    }
    _ksa_done = true; ob._ksa_done = true; oc._ksa_done = true; od._ksa_done = true;
  }

  // ===== load_name_quad_shared_key(): 四候选交错 KSA + 共享 key 优化 =====
  // 顺序枚举时 4 候选只在最后 scl 字节不同, 其余字节共享 key load.
  // vary_start 语义: [0, vary_start) 内四个候选的字节完全相同, 因此只需读 a[j] 一次;
  //   [vary_start, ...) 才开始逐 lane 读各自缓冲区。
  // 正确性前提 (调用方保证): a/b/c/d 在 [0, vary_start) 上逐字节相同 —— 一旦不成立,
  //   本函数会给出与 load_name_quad 不同的结果, 属于静默错误, 所以 vary_start 必须由
  //   「本批实际变化的最低位」推导 (engine.hpp 用 clen 进制取模法求 up, 再乘 scl)。
  void load_name_quad_shared_key(const char *a, const char *b, const char *c, const char *d,
                                  int nlen, int vary_start, Name& ob, Name& oc, Name& od) {
    q_len = -1; ob.q_len = -1; oc.q_len = -1; od.q_len = -1;
    memcpy(val, prefix_loaded ? saved_val : val_base2, sizeof val);
    memcpy(ob.val, ob.prefix_loaded ? ob.saved_val : ob.val_base2, sizeof ob.val);
    memcpy(oc.val, oc.prefix_loaded ? oc.saved_val : oc.val_base2, sizeof oc.val);
    memcpy(od.val, od.prefix_loaded ? od.saved_val : od.val_base2, sizeof od.val);
    u8_t sa = s_pre, sb = s_pre, sc = s_pre, sd = s_pre;
    for (int i = i_pre, j = j_pre; i < N; i++, j++) {
      if (j >= vary_start) {
        sa += a[j] + val[i]; std::swap(val[i], val[sa]);
        sb += b[j] + ob.val[i]; std::swap(ob.val[i], ob.val[sb]);
        sc += c[j] + oc.val[i]; std::swap(oc.val[i], oc.val[sc]);
        sd += d[j] + od.val[i]; std::swap(od.val[i], od.val[sd]);
      } else {
        u8_t kb = a[j];
        sa += kb + val[i]; std::swap(val[i], val[sa]);
        sb += kb + ob.val[i]; std::swap(ob.val[i], ob.val[sb]);
        sc += kb + oc.val[i]; std::swap(oc.val[i], oc.val[sc]);
        sd += kb + od.val[i]; std::swap(od.val[i], od.val[sd]);
      }
      if (j == nlen) j = -1;
    }
    sa = 0; sb = 0; sc = 0; sd = 0;
    for (int i = 0, j = nlen; i < N; i++, j++) {
      if (j >= vary_start) {
        sa += a[j] + val[i]; std::swap(val[i], val[sa]);
        sb += b[j] + ob.val[i]; std::swap(ob.val[i], ob.val[sb]);
        sc += c[j] + oc.val[i]; std::swap(oc.val[i], oc.val[sc]);
        sd += d[j] + od.val[i]; std::swap(od.val[i], od.val[sd]);
      } else {
        u8_t kb = a[j];
        sa += kb + val[i]; std::swap(val[i], val[sa]);
        sb += kb + ob.val[i]; std::swap(ob.val[i], ob.val[sb]);
        sc += kb + oc.val[i]; std::swap(oc.val[i], oc.val[sc]);
        sd += kb + od.val[i]; std::swap(od.val[i], od.val[sd]);
      }
      if (j == nlen) j = -1;
    }
    _ksa_done = true; ob._ksa_done = true; oc._ksa_done = true; od._ksa_done = true;
  }

  void finish_load_name() {
    // KSA 之后的收尾: 计算 ual/ual_skills、压缩出属性候选、求 V。
    // 调用约定: 内部假定 val 已就绪 (load_name / load_name_batch4 之后)。
    // max_len 固定传 30: 属性只需要 name_base[0..30] (7 个三元组 + 高位组)。
    q_len = -1;
#if PBB_HAS_AVX512 || PBB_HAS_AVX2
    simd_mul_add_dual_filter(val, ual_skills, name_base, q_len, 30);
#else
    simd_mul_add_dual(val, ual, ual_skills);
    for (int i = 0; i < N && q_len < 30; i++)
      if (ual[i] >= 89 && ual[i] < 217)
        name_base[++q_len] = ual[i] & 63;
#endif
    // === V 值计算 (三级早退) ===
    // 三个阈值的来源: 24 / 165 / 250 是「按旧门控顺序累加时的剪枝点」, 不是评分阈值。
    // 每级之间累加的都是非负中位数 ⇒ 提前返回与算完全部逐位等价。
    V = 0;
    // 第 1 级: 位置 28-30
    V += median(name_base[28], name_base[29], name_base[30]);
    if (V < 24) return;
    // 第 2 级: 位置 13-18, 25-27
    V += median(name_base[13], name_base[14], name_base[15]);
    V += median(name_base[16], name_base[17], name_base[18]);
    V += median(name_base[25], name_base[26], name_base[27]);
    if (V < 165) return;
    // 第 3 级: 位置 10-12, 19-24
    V += median(name_base[10], name_base[11], name_base[12]);
    V += median(name_base[19], name_base[20], name_base[21]);
    V += median(name_base[22], name_base[23], name_base[24]);
    if (V < 250) return;
    // 第 4 级: 排序 + HP 加成
    sort10(name_base);
    V += (154 + name_base[3] + name_base[4] + name_base[5] + name_base[6]) / 3;
  }
#else
  // ===== load_name(): 标量回退路径 =====
  // 无 AVX2 时的纯 C++ 实现。ual 计算通过手动展开循环 (每次 8 个)。
  // 分批处理前 96 字节和后 160 字节以减少不必要的计算。
  // 96 是经验切分点: 30 个属性命中绝大多数落在这 96 字节内, 命中满 30 个后
  //   后 160 字节的乘法与扫描整体跳过 (纯性能优化)。
  // 正确性要求: 本路径与 SIMD 路径必须逐值等价 —— 同样的 ual 公式、同样的
  //   [89,217) 判据、同样的 &63、同样的 V 计算顺序; 自动测试会跨路径对拍。
  void load_name(const char *name, int name_len_hint = 0) {
    q_len = -1;
    u8_t s = s_pre;
    memcpy(val, val_base2, sizeof val);
    for (int i = i_pre, j = j_pre; i < N; i++, j++) {
      s += name[j] + val[i];
      std::swap(val[i], val[s]);
      if (j == NAMELEN) j = -1;
    }
    for (int i = s = 0, j = NAMELEN; i < N; i++, j++) {
      s += name[j] + val[i];
      std::swap(val[i], val[s]);
      if (j == NAMELEN) j = -1;
    }
    // 标量 ual 计算: val * 181 + 160 (前 96 字节)
    for (int i = 0; i < 96; i += 8) {
      ual[i+0] = val[i+0] * 181 + 160; ual[i+1] = val[i+1] * 181 + 160;
      ual[i+2] = val[i+2] * 181 + 160; ual[i+3] = val[i+3] * 181 + 160;
      ual[i+4] = val[i+4] * 181 + 160; ual[i+5] = val[i+5] * 181 + 160;
      ual[i+6] = val[i+6] * 181 + 160; ual[i+7] = val[i+7] * 181 + 160;
    }
    for (int i = 0; i < 96 && q_len < 30; i++)
      if (ual[i] >= 89 && ual[i] < 217)
        name_base[++q_len] = ual[i] & 63;
    // 仅在前 96 字节不足 30 个有效值时处理剩余 160 字节
    if (q_len < 30) {
      for (int i = 96; i < N; i += 8) {
        ual[i+0] = val[i+0] * 181 + 160; ual[i+1] = val[i+1] * 181 + 160;
        ual[i+2] = val[i+2] * 181 + 160; ual[i+3] = val[i+3] * 181 + 160;
        ual[i+4] = val[i+4] * 181 + 160; ual[i+5] = val[i+5] * 181 + 160;
        ual[i+6] = val[i+6] * 181 + 160; ual[i+7] = val[i+7] * 181 + 160;
      }
      for (int i = 96; i < N && q_len < 30; i++)
        if (ual[i] >= 89 && ual[i] < 217)
          name_base[++q_len] = ual[i] & 63;
    }
    // 与 AVX2 路径相同的 V 值计算
    V = 0;
    V += median(name_base[28], name_base[29], name_base[30]);
    if (V < 24) return;
    V += median(name_base[13], name_base[14], name_base[15]);
    V += median(name_base[16], name_base[17], name_base[18]);
    V += median(name_base[25], name_base[26], name_base[27]);
    if (V < 165) return;
    V += median(name_base[10], name_base[11], name_base[12]);
    V += median(name_base[19], name_base[20], name_base[21]);
    V += median(name_base[22], name_base[23], name_base[24]);
    if (V < 250) return;
    sort10(name_base);
    V += (154 + name_base[3] + name_base[4] + name_base[5] + name_base[6]) / 3;
  }
#endif

  // ===== loading_name(): 独立 KSA (无前缀) =====
  // 用于暗影二段评分 (shadow mechanic)。
  // 直接用队伍名 KSA 的 val_base，两遍完整 KSA (no prefix)。
  // V 值计算包含全部 7 个属性 (无早退)。
  // 由于名字含 "?shadow" 后缀 (8 字节)，t_len = strlen+1 循环使用这些字节。
  // 数据依赖: 只用 val_base 作起点 (完全不看 PRELEN/saved_val), 因此与普通路径的
  //   前缀优化无关; 调用它会重写 val / q_len / name_base / V。
  // 顺序约束: 必须在 build_x2 之后调用 —— build_x2 要读原始的 skill[]/freq[] 与
  //   name_base 属性区, 而本函数会把 name_base 覆盖成 shadow 号的值。
  // 与 load_name 的关键差异: 这里不做早退, 7 个属性全算, 且结果 7 项不是 8 项
  //   (HP 由 name_base[3..6] 单独算), 上层据此得到 shadowi/shadowi2 两个加权和。
#if PBB_HAS_SIMD
  void loading_name(const char *name) {
    memcpy(val, val_base, sizeof val);
    q_len = -1;
    u8_t s;
    int t_len = strlen(name) + 1;
    for (int _ = 0; _ < 2; _++)
      for (int i = s = 0; i < N; i++) {
        if (i % t_len) s += name[i % t_len - 1];
        s += val[i];
        std::swap(val[i], val[s]);
      }
#if PBB_HAS_AVX512 || PBB_HAS_AVX2
    simd_mul_add_filter(val, name_base, q_len, 30);
#else
    simd_mul_add(val, ual, 181, 160);
    for (int i = 0; i < N && q_len < 30; i++)
      if (ual[i] >= 89 && ual[i] < 217)
        name_base[++q_len] = ual[i] & 63;
#endif
    V = 0;
    V += median(name_base[10], name_base[11], name_base[12]);
    V += median(name_base[13], name_base[14], name_base[15]);
    V += median(name_base[16], name_base[17], name_base[18]);
    V += median(name_base[19], name_base[20], name_base[21]);
    V += median(name_base[22], name_base[23], name_base[24]);
    V += median(name_base[25], name_base[26], name_base[27]);
    V += median(name_base[28], name_base[29], name_base[30]);
    sort10(name_base);
    V += (154 + name_base[3] + name_base[4] + name_base[5] + name_base[6]) / 3;
  }
#else
  void loading_name(const char *name) {
    memcpy(val, val_base, sizeof val);
    q_len = -1;
    u8_t s;
    int t_len = strlen(name) + 1;
    for (int _ = 0; _ < 2; _++)
      for (int i = s = 0; i < N; i++) {
        if (i % t_len) s += name[i % t_len - 1];
        s += val[i];
        std::swap(val[i], val[s]);
      }
    for (int i = 0; i < 96; i += 8) {
      ual[i+0] = val[i+0] * 181 + 160; ual[i+1] = val[i+1] * 181 + 160;
      ual[i+2] = val[i+2] * 181 + 160; ual[i+3] = val[i+3] * 181 + 160;
      ual[i+4] = val[i+4] * 181 + 160; ual[i+5] = val[i+5] * 181 + 160;
      ual[i+6] = val[i+6] * 181 + 160; ual[i+7] = val[i+7] * 181 + 160;
    }
    for (int i = 0; i < 96 && q_len < 30; i++)
      if (ual[i] >= 89 && ual[i] < 217)
        name_base[++q_len] = ual[i] & 63;
    if (q_len < 30) {
      for (int i = 96; i < N; i += 8) {
        ual[i+0] = val[i+0] * 181 + 160; ual[i+1] = val[i+1] * 181 + 160;
        ual[i+2] = val[i+2] * 181 + 160; ual[i+3] = val[i+3] * 181 + 160;
        ual[i+4] = val[i+4] * 181 + 160; ual[i+5] = val[i+5] * 181 + 160;
        ual[i+6] = val[i+6] * 181 + 160; ual[i+7] = val[i+7] * 181 + 160;
      }
      for (int i = 96; i < N && q_len < 30; i++)
        if (ual[i] >= 89 && ual[i] < 217)
          name_base[++q_len] = ual[i] & 63;
    }
    V = 0;
    V += median(name_base[10], name_base[11], name_base[12]);
    V += median(name_base[13], name_base[14], name_base[15]);
    V += median(name_base[16], name_base[17], name_base[18]);
    V += median(name_base[19], name_base[20], name_base[21]);
    V += median(name_base[22], name_base[23], name_base[24]);
    V += median(name_base[25], name_base[26], name_base[27]);
    V += median(name_base[28], name_base[29], name_base[30]);
    sort10(name_base);
    V += (154 + name_base[3] + name_base[4] + name_base[5] + name_base[6]) / 3;
  }
#endif

  // ===== calc_skills(): 技能分布计算 =====
  // 使用 ual_skills 数组 (val*181+71) 过滤出值的高位为 0 的项。
  // 步骤:
  //   1. 过滤 ual_skills → name_base[64..127] (K=64 偏移)
  //   2. 用 RC4 PRGA 对 skill[0..39] 进行 Fisher-Yates 洗牌
  //   3. 将 name_base[64..] 按 4 个一组取最小值，分配到 16 个频次槽
  //   4. 技巧列表:
  //      - 最后一个 <25 的技能 freq *= 2 (强化)
  //      - 槽 14/15 有特殊加成 (基于 name_base[60..63])
  //
  // AVX2 路径: ual_skills 已在 load_name 中由 simd_mul_add_dual 预计算。
  // 顺序依赖 (易错点): SIMD 档下本函数不自算 ual_skills, 而是直接消费 load_name/
  //   load_name_batch4 留下的产物 —— 因此必须先做 KSA 再算技能, 反过来会拿到上一个
  //   候选的陈旧数据 (静默错误)。
  // 状态重置: p = q = 0 让 PRGA 从头开始; 因此同一次调用内 gen() 的调用次数完全决定了
  //   skill[] 的洗牌结果 —— 两遍 × 40 次是评分数值的一部分, 不可增减。
  // name_base 复用: 技能值写到 name_base[64..127], 与属性区 [0..30] 不重叠, 所以
  //   门控算出的属性值在本函数之后仍然可读 (score_full 正是靠这一点衔接 Step 2 与 Step 3)。
  // freq[] 语义: 槽 0..15 对应技能排列中的位置；只有 skill[j]<35 且组内最小值 >10 才有值,
  //   14/15 槽另有基于 name_base[60..63] 的额外加成 (见下)。
#if PBB_HAS_SIMD
  void calc_skills(const char *name) {
    q_len = -1;
    simd_skill_filter(ual_skills, name_base, q_len);
    u8_t *a = name_base + K;       // 技能值从 name_base[64] 开始
    for (int i = 0; i < skill_cnt; i++) skill[i] = i;
    memset(freq, 0, sizeof freq);
    p = q = 0;
    // Fisher-Yates 洗牌 (两遍)
    for (int s = 0, _ = 0; _ < 2; _++)
      for (int i = 0; i < skill_cnt; i++) {
        s = (s + gen() + skill[i]) % skill_cnt;
        std::swap(skill[i], skill[s]);
      }
    int last = -1;                 // 最后一个 <25 的技能槽
    for (int i = 0, j = 0; i < K; i += 4, j++) {
      u8_t p = std::min({a[i], a[i+1], a[i+2], a[i+3]});
      if (p > 10 && skill[j] < 35) {
        freq[j] = p - 10;
        if (skill[j] < 25) last = j;  // 记录最后一个 "好" 技能
      } else freq[j] = 0;
    }
    if (last != -1) freq[last] <<= 1;                        // 强化最后一个好技能
    if (freq[14] && last != 14) freq[14] += std::min({name_base[60], name_base[61], freq[14]});
    if (freq[15] && last != 15) freq[15] += std::min({name_base[62], name_base[63], freq[15]});
  }

  static void calc_skills_batch4(Name (&lanes)[4], unsigned active_mask) {
    // 四路批量 (SIMD 档): 把 4 个候选的技能步骤交错执行, 摊薄 PRGA 的串行依赖。
    // active_mask: bit i 表示 lane i 还需要算 (被门控淘汰的 lane 直接跳过, 省 PRGA 与洗牌)。
    // 等价性要求: 每个 lane 的 gen() 消耗顺序必须与单独调用 calc_skills 完全一致
    //   (因此内层循环统一为「同一 i 上逐 lane」, 不能按 lane 再按 i 重排);
    //   被跳过的 lane 不能消耗任何 PRGA 状态, 否则会把其余 lane 的结果带偏 ——
    //   本实现用 active_mask 在每个内层位置判定, 保证这一点。
    for (int lane = 0; lane < 4; lane++)
      if (active_mask & (1u << lane)) {
        lanes[lane].q_len = -1;
        simd_skill_filter(lanes[lane].ual_skills, lanes[lane].name_base, lanes[lane].q_len);
      }
    for (int i = 0; i < skill_cnt; i++)
      for (int lane = 0; lane < 4; lane++)
        if (active_mask & (1u << lane)) lanes[lane].skill[i] = i;
    for (int lane = 0; lane < 4; lane++) if (active_mask & (1u << lane)) {
      memset(lanes[lane].freq, 0, sizeof lanes[lane].freq);
      lanes[lane].p = lanes[lane].q = 0;
    }
    int s[4] = {};
    for (int round = 0; round < 2; round++)
      for (int i = 0; i < skill_cnt; i++)
        for (int lane = 0; lane < 4; lane++) if (active_mask & (1u << lane)) {
          Name& n = lanes[lane];
          s[lane] = (s[lane] + n.gen() + n.skill[i]) % skill_cnt;
          std::swap(n.skill[i], n.skill[s[lane]]);
        }
    int last[4] = {-1, -1, -1, -1};
    for (int i = 0, j = 0; i < K; i += 4, j++)
      for (int lane = 0; lane < 4; lane++) if (active_mask & (1u << lane)) {
        Name& n = lanes[lane];
        u8_t* a = n.name_base + K;
        u8_t v = std::min({a[i], a[i+1], a[i+2], a[i+3]});
        if (v > 10 && n.skill[j] < 35) {
          n.freq[j] = v - 10;
          if (n.skill[j] < 25) last[lane] = j;
        } else n.freq[j] = 0;
      }
    for (int lane = 0; lane < 4; lane++) if (active_mask & (1u << lane)) {
      Name& n = lanes[lane];
      int l = last[lane];
      if (l != -1) n.freq[l] <<= 1;
      if (n.freq[14] && l != 14) n.freq[14] += std::min({n.name_base[60], n.name_base[61], n.freq[14]});
      if (n.freq[15] && l != 15) n.freq[15] += std::min({n.name_base[62], n.name_base[63], n.freq[15]});
    }
  }
#else
  // 标量回退版的 calc_skills: 与 SIMD 版数值等价, 差别仅在于 ual_skills 现场计算
  // (add=71 而非属性侧的 160), 且过滤用 (ual[i] & 0x80) == 0 的逐字节判断。
  void calc_skills(const char *name) {
    q_len = -1;
    // 标量 ual 计算: val * 181 + 71 (技能用, add=71 而非 160)
    for (int i = 0; i < N; i += 8) {
      ual[i+0] = val[i+0] * 181 + 71; ual[i+1] = val[i+1] * 181 + 71;
      ual[i+2] = val[i+2] * 181 + 71; ual[i+3] = val[i+3] * 181 + 71;
      ual[i+4] = val[i+4] * 181 + 71; ual[i+5] = val[i+5] * 181 + 71;
      ual[i+6] = val[i+6] * 181 + 71; ual[i+7] = val[i+7] * 181 + 71;
    }
    for (int i = 0; i < N; i++)
      if ((ual[i] & 0x80) == 0)
        name_base[++q_len] = (ual[i] + 89) & 63;
    u8_t *a = name_base + K;
    for (int i = 0; i < skill_cnt; i++) skill[i] = i;
    memset(freq, 0, sizeof freq);
    p = q = 0;
    for (int s = 0, _ = 0; _ < 2; _++)
      for (int i = 0; i < skill_cnt; i++) {
        s = (s + gen() + skill[i]) % skill_cnt;
        std::swap(skill[i], skill[s]);
      }
    int last = -1;
    for (int i = 0, j = 0; i < K; i += 4, j++) {
      u8_t p = std::min({a[i], a[i+1], a[i+2], a[i+3]});
      if (p > 10 && skill[j] < 35) {
        freq[j] = p - 10;
        if (skill[j] < 25) last = j;
      } else freq[j] = 0;
    }
    if (last != -1) freq[last] <<= 1;
    if (freq[14] && last != 14) freq[14] += std::min({name_base[60], name_base[61], freq[14]});
    if (freq[15] && last != 15) freq[15] += std::min({name_base[62], name_base[63], freq[15]});
  }

  static void calc_skills_batch4(Name (&lanes)[4], unsigned active_mask) {
    // 标量档的批量实现: 逐个 lane 转调单路 calc_skills (无 SIMD 也就没有交错收益)。
    // active_mask 同样用于跳过被淘汰的 lane, 保证不浪费 PRGA 状态。
    for (int lane = 0; lane < 4; lane++)
      if (active_mask & (1u << lane)) lanes[lane].calc_skills(nullptr);
  }
#endif
};

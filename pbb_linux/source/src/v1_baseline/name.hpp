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
  bool prefix_loaded;               // 是否已执行 load_prefix (用于 AVX2 快速恢复)
#endif

  // ---- KSA 基础状态 ----
  u8_t val_base[N];                 // 队伍名 KSA 后的初始 S-box (所有名字共享)
  u8_t val_base2[N];                // load_prefix 修改后的 S-box 副本

  // ---- 复用标志 ----
  bool _ksa_done = false;           // load_name_quad 已做完 KSA 时跳过重做

  // ---- 属性/技能缓冲区 ----
  u8_t name_base[M];                // 过滤后的 ual 值 (低 6 位, 最多 30 个有效值)
  u8_t freq[16];                    // 16 组技能的频次分布
  u8_t skill[skill_cnt];            // 技能排列 (40 个技能 ID 的随机排列)

  // ---- RC4 状态变量 ----
  u8_t p, q;                        // RC4 PRGA 的 i, j 指针
  u8_t i_pre, j_pre;                // load_prefix 保存的中间状态
  u8_t s_pre;                       // load_prefix 保存的累加和

  // ---- 派生属性 ----
  int q_len;                        // name_base 中有效值的数量 (0-30)
  int V;                            // 八围评分 (load_name 计算)
  int seed;                         // 种子 (未使用, 保留)
  int PRELEN;                       // load_prefix 预处理的名字字节数
  int NAMELEN;                      // 名字总长度

  // ===== m(): RC4 PRGA 单步 =====
  // 标准 RC4: i++, j = (j + S[i]), swap(S[i], S[j]), 输出 S[(S[i]+S[j]) & 255]
  inline u8_t m() {
    q += val[++p];
    std::swap(val[p], val[q]);
    return val[(val[p] + val[q]) & 255];
  }

  // ===== gen(): 生成技能索引 =====
  // 两次 PRGA 输出拼接为 16 位，对 skill_cnt (40) 取模得到 0-39 的技能 ID
  inline int gen() {
    int u = m();
    return (u << 8 | m()) % skill_cnt;
  }

  // ===== load_team(): 队伍名 KSA (密钥调度) =====
  // 用队伍名字节初始化 val_base[0..255] 为 0..255 的随机排列。
  // 使用标准 RC4 KSA: 对每个 i, s += key[i % keylen] + S[i], swap(S[i], S[s])
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
  static void load_name_batch4(Name (&lanes)[4], const char *const (&names)[4], int name_len) {
    u8_t s[4];
    for (int lane = 0; lane < 4; lane++) {
      memcpy(lanes[lane].val, lanes[lane].prefix_loaded ? lanes[lane].saved_val : lanes[lane].val_base2,
             sizeof lanes[lane].val);
      s[lane] = lanes[lane].s_pre;
    }

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

    for (int lane = 0; lane < 4; lane++) lanes[lane].finish_load_name();
  }

  // ===== load_name_quad(): 四候选交错 RC4 KSA (无共享 key) =====
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
    for (int lane = 0; lane < 4; lane++)
      if (active_mask & (1u << lane)) lanes[lane].calc_skills(nullptr);
  }
#endif
};

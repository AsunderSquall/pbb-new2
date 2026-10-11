#pragma once
// ============================================================================
// utils.hpp — 无分支排序网络与 SIMD 加速工具函数
//
// 所有函数均声明为 static inline，确保编译器内联优化。
// AVX2 路径使用 256 位 SIMD 指令并行处理 32 字节。
//
// 中文总述: 本文件是「叶子工具层」—— 只包含无状态的小工具 (中位数/条件交换/排序网络)
//   以及三类字节数组过滤原语:
//     simd_skill_filter       — 技能侧过滤 (取 ual_skills 高位为 0 的项)
//     simd_mul_add*[_filter]  — 属性侧 ual = (val*181 + add) & 0xFF 的批量计算 + 范围过滤
//     simd_filter_range_attr  — 仅范围过滤 (ual 已算好时使用)
//   三条过滤原语在语义上都必须与「标量顺序扫描」逐值等价: mask 的 bit0 对应最低地址字节,
//   ctz 从最低位取, 因此压缩后的写入顺序与标量版完全一致。
//
//   实现分层由编译期宏选择 (不是运行期派发):
//     VBMI2 (vpcompressb 一条指令) > AVX-512 BW (掩码+tzcnt) > AVX2 (movemask+tzcnt) > NEON > 标量。
//   所有路径共用同一套「范围判据」: 属性命中 ⇔ 89 <= ual < 217; 技能命中 ⇔ (ual & 0x80) == 0。
//
//   外部可控的两个调优宏 (由 build.py 传递, 只影响速度/寄存器压力, 绝不改变评分门控):
//     PBB_ATTR_MINMAX      : 用 std::min/std::max 替代位运算实现 median/cond_swap。
//     PBB_ATTR_FILTER_MODE : 属性过滤配方 0..4 (0=原版/1=guard512/2=reg512/3=mixed512/4=reg256)。
// ============================================================================
#include "common.hpp"

// ===== 稳定压缩查找表 (Issue #17 附件 E14) =====
// 将 8 位 lane mask 映射为 shuffle 索引和匹配计数。
// constexpr 编译期计算，零运行时开销。AVX2/NEON 均可用。
// 未使用索引填 0xff 是为了让 shuffle 落到「不可能被选中」的位置; counts 供调用方决定
// 写回多少个字节。当前生产路径使用 vpcompressb / 掩码提取, 本表属于 Issue #17 的备用实现,
// 保留在头文件中供后续替换, 删除它不影响现有评分路径。
#if PBB_HAS_NEON || PBB_HAS_AVX2 || PBB_HAS_AVX512
struct Compress8Table {
  u8_t indexes[256][8]{};
  u8_t counts[256]{};
  constexpr Compress8Table() {
    for (int mask = 0; mask < 256; mask++) {
      int count = 0;
      for (int lane = 0; lane < 8; lane++)
        if (mask & (1 << lane)) indexes[mask][count++] = lane;
      for (int lane = count; lane < 8; lane++) indexes[mask][lane] = 0xff;
      counts[mask] = count;
    }
  }
};
static constexpr Compress8Table compress8_table{};
#endif

// ===== 无分支中位数 (branchless median) =====
// 编译器生成 cmov 指令代替分支跳转，避免分支预测失败惩罚。
// 输入: 3 个 u8_t 值
// 输出: 中间值
// 为什么必须无分支: 该函数在每候选的热路径上被调用 7 次 (对应 7 个属性),
//   而属性分布本身近乎随机, 分支预测命中率低, 这里用条件传送换取稳定吞吐。
// 取值域提示: 传入的 name_base[] 已被 &63, 故结果恒在 0..63, 这正是门控上界 GATE_P_MAX 的依据。
static inline u8_t median(u8_t a, u8_t b, u8_t c) {
#if defined(PBB_ATTR_MINMAX) && PBB_ATTR_MINMAX
  u8_t max_ab = a > b ? a : b;
  u8_t min_ab = a < b ? a : b;
  return std::max(min_ab, std::min(max_ab, c));
#else
  u8_t max_ab = a > b ? a : b;
  u8_t min_ab = a < b ? a : b;
  return (c > max_ab) ? max_ab : ((c < min_ab) ? min_ab : c);
#endif
}

// ===== 无分支条件交换 (branchless conditional swap) =====
// 通过位运算实现无分支的交换:
//   diff = a - b, 若 a < b 则 diff 的 bit7 = 1
//   mask = (diff >> 7) - 1 → 全 0 (a>=b) 或全 1 (a<b)
//   tmp = (a ^ b) & mask → 仅在 a<b 时非零
//   a ^= tmp, b ^= tmp → 仅在 a<b 时交换
static inline void cond_swap(u8_t& a, u8_t& b) {
#if defined(PBB_ATTR_MINMAX) && PBB_ATTR_MINMAX
    // sort10 inputs are attribute values masked to 0..63.
    const u8_t x = a, y = b;
    a = std::min(x, y);
    b = std::max(x, y);
#else
    u8_t diff = a - b;
    u8_t mask = (diff >> 7) - 1;
    u8_t tmp  = (a ^ b) & mask;
    a ^= tmp;
    b ^= tmp;
#endif
}

// ===== 10 元素排序网络 (最优 29 次交换, 深度 9) =====
// 硬编码的 Batcher 奇偶归并排序网络，用于对 name_base 前 10 元素排序。
// 排序后 name_base[3..6] 用于计算 HP 加成:
//   V += (154 + name_base[3] + name_base[4] + name_base[5] + name_base[6]) / 3
// 约束: 只对前 10 项排序 —— name_base[10..30] 有固定的属性语义 (每个三元组一个属性),
//   一旦被排序就会破坏后续 median 取值的含义, 因此绝不能对整个缓冲区排序。
// 该网络的交换次数/层数与 scoring_1124.hpp::sort10_net 必须逐值等价 (自动测试会对比)。
static inline void sort10(u8_t* arr) {
    #define SWAP(i,j) cond_swap(arr[i], arr[j])
    SWAP(0, 5); SWAP(1, 6); SWAP(2, 7); SWAP(3, 8); SWAP(4, 9);
    SWAP(0, 3); SWAP(1, 4); SWAP(5, 8); SWAP(6, 9);
    SWAP(0, 2); SWAP(3, 6); SWAP(4, 7); SWAP(1, 5);
    SWAP(0, 1); SWAP(2, 4); SWAP(7, 9); SWAP(3, 5);
    SWAP(2, 3); SWAP(6, 8); SWAP(4, 5);
    SWAP(1, 2); SWAP(3, 4); SWAP(5, 6); SWAP(7, 8);
    SWAP(1, 3); SWAP(4, 6); SWAP(2, 5);
    SWAP(0, 1); SWAP(2, 3); SWAP(4, 5); SWAP(6, 7); SWAP(8, 9);
    SWAP(1, 2); SWAP(3, 4); SWAP(5, 6); SWAP(7, 8);
    SWAP(2, 3); SWAP(4, 5); SWAP(6, 7);
    #undef SWAP
}

// ===== 技能过滤: ual_skills<128 的项 → (ual_skills+89)&63, 压缩写入 name_base =====
// 按指令集层级选择最优实现: VBMI2 > AVX-512 BW > AVX2 > NEON > 标量
// 不变量: ual_skills 是 256 字节的一个排列 (val 本身是 RC4 排列, ×181+71 在 mod 256 下
//   仍与 256 互素), 因此「高位为 0」的项恰好 128 个 —— 这正是 name_base 容量 M=128 的由来,
//   也是 v3_nobranch_filter 能安全去掉 q_len<max_len 守卫的前提。
// 顺序保证: 三条 SIMD 路径都按地址升序压缩写入, 与标量回退逐值一致 (否则技能排列会变)。
// 变换 (x+89)&63 与属性侧的 &63 共用同一套取低 6 位语义。
#if defined(__AVX512VBMI2__)
// VBMI2 compressstore: 单指令压缩写入 (最快)
static inline void simd_skill_filter(const u8_t* __restrict__ ual_skills,
                                     u8_t* __restrict__ name_base, int& q_len) {
  const __m512i v128 = _mm512_set1_epi8((char)128);
  const __m512i v89  = _mm512_set1_epi8(89);
  const __m512i v63  = _mm512_set1_epi8(63);
  for (int i = 0; i < 256; i += 64) {
    __m512i s = _mm512_loadu_si512((const __m512i*)&ual_skills[i]);
    __mmask64 mask = _mm512_cmplt_epu8_mask(s, v128);
    __m512i val = _mm512_and_si512(_mm512_add_epi8(s, v89), v63);
    _mm512_mask_compressstoreu_epi8(&name_base[q_len + 1], mask, val);
    q_len += _mm_popcnt_u64(mask);
  }
}

#elif PBB_HAS_AVX512
// AVX-512 BW: mask + tzcnt 提取 (无 vpcompressb)
static inline void simd_skill_filter(const u8_t* __restrict__ ual,
                                     u8_t* __restrict__ name_base, int& q_len) {
  for (int i = 0; i < 256; i += 64) {
    __m512i data = _mm512_loadu_si512((const __m512i*)&ual[i]);
    __mmask64 mask = _mm512_cmp_epu8_mask(data, _mm512_set1_epi8(0x80), _MM_CMPINT_LT);
    while (mask) {
      int idx = __builtin_ctzll(mask);
      name_base[++q_len] = (ual[i + idx] + 89) & 63;
      mask &= mask - 1;
    }
  }
}

#elif PBB_HAS_AVX2
// AVX2: movemask 提取高位 → tzcnt 遍历
static inline void simd_skill_filter(const u8_t* __restrict__ ual,
                                     u8_t* __restrict__ name_base, int& q_len) {
  for (int i = 0; i < 256; i += 32) {
    __m256i data = _mm256_loadu_si256((const __m256i*)&ual[i]);
    unsigned mask = (unsigned)~_mm256_movemask_epi8(data);
    while (mask) {
      int idx = __builtin_ctz(mask);
      name_base[++q_len] = (ual[i + idx] + 89) & 63;
      mask &= mask - 1;
    }
  }
}

#else
// 标量回退: 逐字节检查高位
static inline void simd_skill_filter(const u8_t* __restrict__ ual_skills,
                                     u8_t* __restrict__ name_base, int& q_len) {
  for (int i = 0; i < 256; i++)
    if ((ual_skills[i] & 0x80) == 0)
      name_base[++q_len] = (ual_skills[i] + 89) & 63;
}
#endif

// 属性过滤配方开关 (由 build.py / build_config.json 注入, 默认 0):
//   0 = 原版 (vpcompressb 无条件压缩写)
//   1 = guard512  (保留 if(q_len<max_len) 守卫)
//   2 = reg512    (去掉守卫 + 容量回退, 寄存器版压缩写)
//   3 = mixed512  (AVX2 寄存器 + AVX-512 压缩写的混合配方)
//   4 = reg256    (纯 AVX2 压缩写, 需 AVX512VBMI2 + AVX512VL)
// 这些配方只调整指令/寄存器使用方式, 命中集合与写入顺序与 mode 0 完全一致, 不改变评分门控。
#ifndef PBB_ATTR_FILTER_MODE
#define PBB_ATTR_FILTER_MODE 0
#endif
#if PBB_ATTR_FILTER_MODE < 0 || PBB_ATTR_FILTER_MODE > 4
#error Invalid PBB_ATTR_FILTER_MODE
#endif

// ===== SIMD ual 计算: val[i] = (val[i] * mul + add) & 0xFF =====
// 按指令集层级选择最优实现: AVX-512 (64B/iter) > AVX2 (32B/iter) > NEON (16B/iter)
// 数值契约 (评分语义的一部分, 不可改动):
//   属性侧 mul=181, add=160  → ual 中落在 [89,217) 的项即属性候选, 取低 6 位后 ≤63。
//   技能侧 mul=181, add=71   → ual_skills 中高位为 0 的项即技能候选。
// 之所以拆成「先转 16 位再乘」: 8 位通道内乘法会溢出, 必须 unpack 到 16 位相乘后
// 再 &0xFF 截断、packus 回 8 位。所有实现都写回完整 256 字节。
#if PBB_HAS_AVX512
// ---- AVX-512: 4 次迭代覆盖 256 字节 ----
static inline void simd_mul_add(const u8_t* __restrict__ val, u8_t* __restrict__ ual,
                                 u8_t mul, u8_t add) {
  const __m512i vmul = _mm512_set1_epi16(mul);
  const __m512i vadd = _mm512_set1_epi16(add);
  const __m512i vmask = _mm512_set1_epi16(0xFF);
  const __m512i vzero = _mm512_setzero_si512();
  for (int i = 0; i < 256; i += 64) {
    __m512i v = _mm512_loadu_si512((const __m512i*)&val[i]);
    __m512i lo = _mm512_unpacklo_epi8(v, vzero);
    __m512i hi = _mm512_unpackhi_epi8(v, vzero);
    lo = _mm512_mullo_epi16(lo, vmul);
    hi = _mm512_mullo_epi16(hi, vmul);
    lo = _mm512_add_epi16(lo, vadd);
    hi = _mm512_add_epi16(hi, vadd);
    lo = _mm512_and_si512(lo, vmask);
    hi = _mm512_and_si512(hi, vmask);
    __m512i res = _mm512_packus_epi16(lo, hi);
    _mm512_storeu_si512((__m512i*)&ual[i], res);
  }
}

static inline void simd_mul_add_dual(const u8_t* __restrict__ val,
                                      u8_t* __restrict__ ual_attr,
                                      u8_t* __restrict__ ual_skill) {
  const __m512i vmul  = _mm512_set1_epi16(181);
  const __m512i vaddA = _mm512_set1_epi16(160);
  const __m512i vaddS = _mm512_set1_epi16(71);
  const __m512i vmask = _mm512_set1_epi16(0xFF);
  const __m512i vzero = _mm512_setzero_si512();
  for (int i = 0; i < 256; i += 64) {
    __m512i v = _mm512_loadu_si512((const __m512i*)&val[i]);
    __m512i lo = _mm512_unpacklo_epi8(v, vzero);
    __m512i hi = _mm512_unpackhi_epi8(v, vzero);
    __m512i loA = _mm512_add_epi16(_mm512_mullo_epi16(lo, vmul), vaddA);
    __m512i hiA = _mm512_add_epi16(_mm512_mullo_epi16(hi, vmul), vaddA);
    __m512i loS = _mm512_add_epi16(_mm512_mullo_epi16(lo, vmul), vaddS);
    __m512i hiS = _mm512_add_epi16(_mm512_mullo_epi16(hi, vmul), vaddS);
    loA = _mm512_and_si512(loA, vmask); hiA = _mm512_and_si512(hiA, vmask);
    loS = _mm512_and_si512(loS, vmask); hiS = _mm512_and_si512(hiS, vmask);
    _mm512_storeu_si512((__m512i*)&ual_attr[i], _mm512_packus_epi16(loA, hiA));
    _mm512_storeu_si512((__m512i*)&ual_skill[i], _mm512_packus_epi16(loS, hiS));
  }
}

// Host tuning recipes; default 0 preserves the existing production path.
// 1 guard512, 2 reg512, 3 mixed512, 4 reg256. Never changes scoring gates.
// 中文说明: simd_mul_add_dual_filter 是热路径核心 —— 一次遍历同时产出技能侧 ual_skills
//   和属性侧候选, 并按 [89,217) 把属性候选压缩写入 name_base[1..]。参数 max_len 是
//   属性缓冲区容量哨兵 (生产传入 30): 命中数达到 max_len 后即可停止写入, 因为 V 只用
//   name_base[0..30], 多写无益且可能越界。max_len=127/128 的边界由自动测试专门覆盖
//   (「随机排列的前 max_len+1 项属性」与「容量哨兵」正确性对拍)。
// 注意与 mode 2/4 的差异: 这两档用 maskz_compress + 普通 store 写 32/64 字节,
//   在缓冲区尾部会写到「填充区」(name_base 之后仍属于 M=128 数组之内), 只在
//   q_len+1 > M-32 / M-64 的临界处才回退到 mask_compressstoreu 以免越界。
#if PBB_ATTR_FILTER_MODE == 3 || PBB_ATTR_FILTER_MODE == 4
#if PBB_ATTR_FILTER_MODE == 4 && (!defined(__AVX512VBMI2__) || !defined(__AVX512VL__))
#error reg256 requires AVX512VBMI2 and AVX512VL
#endif
static inline void simd_mul_add_dual_filter(const u8_t* __restrict__ val,
                                             u8_t* __restrict__ ual_skill,
                                             u8_t* __restrict__ name_base,
                                             int& q_len, int max_len) {
  const __m256i vmul  = _mm256_set1_epi16(181);
  const __m256i vaddA = _mm256_set1_epi16(160);
  const __m256i vaddS = _mm256_set1_epi16(71);
  const __m256i vmask = _mm256_set1_epi16(0xFF);
  const __m256i vzero = _mm256_setzero_si256();
  const __m256i v88  = _mm256_set1_epi8(88);
  const __m256i v217 = _mm256_set1_epi8(217);
  const __m256i vones = _mm256_cmpeq_epi8(vzero, vzero);
  for (int i = 0; i < 256; i += 32) {
    __m256i v = _mm256_loadu_si256((const __m256i*)&val[i]);
    __m256i lo = _mm256_unpacklo_epi8(v, vzero);
    __m256i hi = _mm256_unpackhi_epi8(v, vzero);
    __m256i loA = _mm256_add_epi16(_mm256_mullo_epi16(lo, vmul), vaddA);
    __m256i hiA = _mm256_add_epi16(_mm256_mullo_epi16(hi, vmul), vaddA);
    __m256i loS = _mm256_add_epi16(_mm256_mullo_epi16(lo, vmul), vaddS);
    __m256i hiS = _mm256_add_epi16(_mm256_mullo_epi16(hi, vmul), vaddS);
    loA = _mm256_and_si256(loA, vmask);  hiA = _mm256_and_si256(hiA, vmask);
    loS = _mm256_and_si256(loS, vmask);  hiS = _mm256_and_si256(hiS, vmask);
    _mm256_storeu_si256((__m256i*)&ual_skill[i], _mm256_packus_epi16(loS, hiS));
    if (q_len < max_len) {
      __m256i attr = _mm256_packus_epi16(loA, hiA);
#if PBB_ATTR_FILTER_MODE == 4
      __mmask32 mask = _mm256_cmpge_epu8_mask(attr, _mm256_set1_epi8(89)) &
                         _mm256_cmplt_epu8_mask(attr, _mm256_set1_epi8((char)217));
      __m256i attr63 = _mm256_and_si256(attr, _mm256_set1_epi8(63));
      // Ordinary stores touch padding: retain compress-store at the capacity edge.
      if (q_len + 1 <= M - 32)
        _mm256_storeu_si256((__m256i*)&name_base[q_len + 1], _mm256_maskz_compress_epi8(mask, attr63));
      else
        _mm256_mask_compressstoreu_epi8(&name_base[q_len + 1], mask, attr63);
      q_len += _mm_popcnt_u32((unsigned)mask);
#else
      __m256i ge = _mm256_subs_epu8(attr, v88);
      __m256i lt = _mm256_subs_epu8(v217, attr);
      __m256i ge_mask = _mm256_xor_si256(_mm256_cmpeq_epi8(ge, vzero), vones);
      __m256i lt_mask = _mm256_xor_si256(_mm256_cmpeq_epi8(lt, vzero), vones);
      unsigned mask = (unsigned)_mm256_movemask_epi8(_mm256_and_si256(ge_mask, lt_mask));
      while (mask && q_len < max_len) {
        int idx = _tzcnt_u32(mask);
        name_base[++q_len] = ((const u8_t*)&attr)[idx] & 63;
        mask &= mask - 1;
      }
#endif
    }
  }
}
#else
// 默认/guard512 路径 (mode 0 与 1): AVX-512 双输出 + 属性压缩写入
static inline void simd_mul_add_dual_filter(const u8_t* __restrict__ val,
                                             u8_t* __restrict__ ual_skill,
                                             u8_t* __restrict__ name_base,
                                             int& q_len, int max_len) {
  const __m512i vmul  = _mm512_set1_epi16(181);
  const __m512i vaddA = _mm512_set1_epi16(160);
  const __m512i vaddS = _mm512_set1_epi16(71);
  const __m512i vmask = _mm512_set1_epi16(0xFF);
  const __m512i vzero = _mm512_setzero_si512();
  const __m512i v89  = _mm512_set1_epi8(89);
  const __m512i v217 = _mm512_set1_epi8(217);
  for (int i = 0; i < 256; i += 64) {
    __m512i v = _mm512_loadu_si512((const __m512i*)&val[i]);
    __m512i lo = _mm512_unpacklo_epi8(v, vzero);
    __m512i hi = _mm512_unpackhi_epi8(v, vzero);
    __m512i loM = _mm512_mullo_epi16(lo, vmul);   // 共享乘积 lo*181
    __m512i hiM = _mm512_mullo_epi16(hi, vmul);   // 共享乘积 hi*181
    __m512i loA = _mm512_add_epi16(loM, vaddA);
    __m512i hiA = _mm512_add_epi16(hiM, vaddA);
    __m512i loS = _mm512_add_epi16(loM, vaddS);
    __m512i hiS = _mm512_add_epi16(hiM, vaddS);
    loA = _mm512_and_si512(loA, vmask); hiA = _mm512_and_si512(hiA, vmask);
    loS = _mm512_and_si512(loS, vmask); hiS = _mm512_and_si512(hiS, vmask);
    _mm512_storeu_si512((__m512i*)&ual_skill[i], _mm512_packus_epi16(loS, hiS));
    // [v3_nobranch_filter] 消除 if(q_len<max_len) 守卫 (对齐 pbb_new v45):
    // ual 是 0..255 的排列 → 命中数恒为 128, 连续写入 name_base[0..127] 恰好填满 M=128, 无溢出;
    // V 只用 name_base[0..30], 多余高位无害 (calc_skills 会重置 q_len)。
#if PBB_ATTR_FILTER_MODE == 1 || PBB_ATTR_FILTER_MODE == 2
    if (q_len < max_len) {
#endif
    __m512i attr = _mm512_packus_epi16(loA, hiA);
    __mmask64 ge = _mm512_cmpge_epu8_mask(attr, v89);
    __mmask64 lt = _mm512_cmplt_epu8_mask(attr, v217);
    __mmask64 mask = ge & lt;
#ifdef __AVX512VBMI2__
    // vpcompressb: 一条指令压缩所有命中字节, 保序写入 name_base
    __m512i attr63 = _mm512_and_si512(attr, _mm512_set1_epi8(63));
#if PBB_ATTR_FILTER_MODE == 2
    // Archive reg512, with a capacity fallback for max_len values beyond 30.
    if (q_len + 1 <= M - 64)
      _mm512_storeu_si512((void*)&name_base[q_len + 1], _mm512_maskz_compress_epi8(mask, attr63));
    else
      _mm512_mask_compressstoreu_epi8(&name_base[q_len + 1], mask, attr63);
#else
    _mm512_mask_compressstoreu_epi8(&name_base[q_len + 1], mask, attr63);
#endif
    q_len += _mm_popcnt_u64(mask);
#else
    while (mask && q_len < max_len) {
      int idx = _tzcnt_u64(mask);
      name_base[++q_len] = ((const u8_t*)&attr)[idx] & 63;
      mask &= mask - 1;
    }
#endif
#if PBB_ATTR_FILTER_MODE == 1 || PBB_ATTR_FILTER_MODE == 2
    }
#endif
  }
}
#endif

// 单输出属性过滤 (只产出 name_base, 不产出 ual_skills): 供 shadow 二段评分
// (loading_name) 使用 —— 那条路径不需要技能值, 因此省掉一半的 pack/store。
// q_len 命中 max_len 后立即 return, 后面的 256 字节不再扫描 (属性侧不需要像技能侧那样
// 保证「恰好 128 个」的排列特性)。
static inline void simd_mul_add_filter(const u8_t* __restrict__ val,
                                        u8_t* __restrict__ name_base,
                                        int& q_len, int max_len) {
  const __m512i vmul  = _mm512_set1_epi16(181);
  const __m512i vadd  = _mm512_set1_epi16(160);
  const __m512i vmask = _mm512_set1_epi16(0xFF);
  const __m512i vzero = _mm512_setzero_si512();
  const __m512i v89  = _mm512_set1_epi8(89);
  const __m512i v217 = _mm512_set1_epi8(217);
  for (int i = 0; i < 256; i += 64) {
    __m512i v = _mm512_loadu_si512((const __m512i*)&val[i]);
    __m512i lo = _mm512_unpacklo_epi8(v, vzero);
    __m512i hi = _mm512_unpackhi_epi8(v, vzero);
    lo = _mm512_mullo_epi16(lo, vmul);
    hi = _mm512_mullo_epi16(hi, vmul);
    lo = _mm512_add_epi16(lo, vadd);
    hi = _mm512_add_epi16(hi, vadd);
    lo = _mm512_and_si512(lo, vmask);
    hi = _mm512_and_si512(hi, vmask);
    __m512i attr = _mm512_packus_epi16(lo, hi);
    __mmask64 ge = _mm512_cmpge_epu8_mask(attr, v89);
    __mmask64 lt = _mm512_cmplt_epu8_mask(attr, v217);
    __mmask64 mask = ge & lt;
#ifdef __AVX512VBMI2__
    __m512i attr63 = _mm512_and_si512(attr, _mm512_set1_epi8(63));
    _mm512_mask_compressstoreu_epi8(&name_base[q_len + 1], mask, attr63);
    q_len += _mm_popcnt_u64(mask);
    if (q_len >= max_len) return;
#else
    while (mask && q_len < max_len) {
      int idx = _tzcnt_u64(mask);
      name_base[++q_len] = ((const u8_t*)&attr)[idx] & 63;
      mask &= mask - 1;
    }
#endif
  }
}

#elif PBB_HAS_AVX2
// ---- AVX2: 8 次迭代覆盖 256 字节 ----
static inline void simd_mul_add(const u8_t* __restrict__ val, u8_t* __restrict__ ual,
                                 u8_t mul, u8_t add) {
  const __m256i vmul = _mm256_set1_epi16(mul);
  const __m256i vadd = _mm256_set1_epi16(add);
  const __m256i vmask = _mm256_set1_epi16(0xFF);
  const __m256i vzero = _mm256_setzero_si256();
  for (int i = 0; i < 256; i += 32) {
    __m256i v = _mm256_loadu_si256((const __m256i*)&val[i]);
    __m256i lo = _mm256_unpacklo_epi8(v, vzero);
    __m256i hi = _mm256_unpackhi_epi8(v, vzero);
    lo = _mm256_mullo_epi16(lo, vmul);
    hi = _mm256_mullo_epi16(hi, vmul);
    lo = _mm256_add_epi16(lo, vadd);
    hi = _mm256_add_epi16(hi, vadd);
    lo = _mm256_and_si256(lo, vmask);
    hi = _mm256_and_si256(hi, vmask);
    __m256i res = _mm256_packus_epi16(lo, hi);
    _mm256_storeu_si256((__m256i*)&ual[i], res);
  }
}

static inline void simd_mul_add_dual(const u8_t* __restrict__ val,
                                      u8_t* __restrict__ ual_attr,
                                      u8_t* __restrict__ ual_skill) {
  const __m256i vmul  = _mm256_set1_epi16(181);
  const __m256i vaddA = _mm256_set1_epi16(160);
  const __m256i vaddS = _mm256_set1_epi16(71);
  const __m256i vmask = _mm256_set1_epi16(0xFF);
  const __m256i vzero = _mm256_setzero_si256();
  for (int i = 0; i < 256; i += 32) {
    __m256i v = _mm256_loadu_si256((const __m256i*)&val[i]);
    __m256i lo = _mm256_unpacklo_epi8(v, vzero);
    __m256i hi = _mm256_unpackhi_epi8(v, vzero);
    __m256i loA = _mm256_add_epi16(_mm256_mullo_epi16(lo, vmul), vaddA);
    __m256i hiA = _mm256_add_epi16(_mm256_mullo_epi16(hi, vmul), vaddA);
    __m256i loS = _mm256_add_epi16(_mm256_mullo_epi16(lo, vmul), vaddS);
    __m256i hiS = _mm256_add_epi16(_mm256_mullo_epi16(hi, vmul), vaddS);
    loA = _mm256_and_si256(loA, vmask);  hiA = _mm256_and_si256(hiA, vmask);
    loS = _mm256_and_si256(loS, vmask);  hiS = _mm256_and_si256(hiS, vmask);
    _mm256_storeu_si256((__m256i*)&ual_attr[i], _mm256_packus_epi16(loA, hiA));
    _mm256_storeu_si256((__m256i*)&ual_skill[i], _mm256_packus_epi16(loS, hiS));
  }
}

static inline void simd_mul_add_dual_filter(const u8_t* __restrict__ val,
                                             u8_t* __restrict__ ual_skill,
                                             u8_t* __restrict__ name_base,
                                             int& q_len, int max_len) {
  const __m256i vmul  = _mm256_set1_epi16(181);
  const __m256i vaddA = _mm256_set1_epi16(160);
  const __m256i vaddS = _mm256_set1_epi16(71);
  const __m256i vmask = _mm256_set1_epi16(0xFF);
  const __m256i vzero = _mm256_setzero_si256();
  const __m256i v88  = _mm256_set1_epi8(88);
  const __m256i v217 = _mm256_set1_epi8(217);
  const __m256i vones = _mm256_cmpeq_epi8(vzero, vzero);
  for (int i = 0; i < 256; i += 32) {
    __m256i v = _mm256_loadu_si256((const __m256i*)&val[i]);
    __m256i lo = _mm256_unpacklo_epi8(v, vzero);
    __m256i hi = _mm256_unpackhi_epi8(v, vzero);
    __m256i loA = _mm256_add_epi16(_mm256_mullo_epi16(lo, vmul), vaddA);
    __m256i hiA = _mm256_add_epi16(_mm256_mullo_epi16(hi, vmul), vaddA);
    __m256i loS = _mm256_add_epi16(_mm256_mullo_epi16(lo, vmul), vaddS);
    __m256i hiS = _mm256_add_epi16(_mm256_mullo_epi16(hi, vmul), vaddS);
    loA = _mm256_and_si256(loA, vmask);  hiA = _mm256_and_si256(hiA, vmask);
    loS = _mm256_and_si256(loS, vmask);  hiS = _mm256_and_si256(hiS, vmask);
    _mm256_storeu_si256((__m256i*)&ual_skill[i], _mm256_packus_epi16(loS, hiS));
    if (q_len < max_len) {
      __m256i attr = _mm256_packus_epi16(loA, hiA);
      __m256i ge = _mm256_subs_epu8(attr, v88);
      __m256i lt = _mm256_subs_epu8(v217, attr);
      __m256i ge_mask = _mm256_xor_si256(_mm256_cmpeq_epi8(ge, vzero), vones);
      __m256i lt_mask = _mm256_xor_si256(_mm256_cmpeq_epi8(lt, vzero), vones);
      unsigned mask = (unsigned)_mm256_movemask_epi8(_mm256_and_si256(ge_mask, lt_mask));
      while (mask && q_len < max_len) {
        int idx = _tzcnt_u32(mask);
        name_base[++q_len] = ((const u8_t*)&attr)[idx] & 63;
        mask &= mask - 1;
      }
    }
  }
}

static inline void simd_mul_add_filter(const u8_t* __restrict__ val,
                                        u8_t* __restrict__ name_base,
                                        int& q_len, int max_len) {
  const __m256i vmul  = _mm256_set1_epi16(181);
  const __m256i vadd  = _mm256_set1_epi16(160);
  const __m256i vmask = _mm256_set1_epi16(0xFF);
  const __m256i vzero = _mm256_setzero_si256();
  const __m256i v88  = _mm256_set1_epi8(88);
  const __m256i v217 = _mm256_set1_epi8(217);
  const __m256i vones = _mm256_cmpeq_epi8(vzero, vzero);
  for (int i = 0; i < 256; i += 32) {
    __m256i v = _mm256_loadu_si256((const __m256i*)&val[i]);
    __m256i lo = _mm256_unpacklo_epi8(v, vzero);
    __m256i hi = _mm256_unpackhi_epi8(v, vzero);
    lo = _mm256_mullo_epi16(lo, vmul);
    hi = _mm256_mullo_epi16(hi, vmul);
    lo = _mm256_add_epi16(lo, vadd);
    hi = _mm256_add_epi16(hi, vadd);
    lo = _mm256_and_si256(lo, vmask);
    hi = _mm256_and_si256(hi, vmask);
    __m256i attr = _mm256_packus_epi16(lo, hi);
    __m256i ge = _mm256_subs_epu8(attr, v88);
    __m256i lt = _mm256_subs_epu8(v217, attr);
    __m256i ge_mask = _mm256_xor_si256(_mm256_cmpeq_epi8(ge, vzero), vones);
    __m256i lt_mask = _mm256_xor_si256(_mm256_cmpeq_epi8(lt, vzero), vones);
    unsigned mask = (unsigned)_mm256_movemask_epi8(_mm256_and_si256(ge_mask, lt_mask));
    while (mask && q_len < max_len) {
      int idx = _tzcnt_u32(mask);
      name_base[++q_len] = ((const u8_t*)&attr)[idx] & 63;
      mask &= mask - 1;
    }
  }
}

#elif PBB_HAS_NEON
// ---- ARM NEON: 16 次迭代覆盖 256 字节 ----
static inline void simd_mul_add(const u8_t* __restrict__ val, u8_t* __restrict__ ual,
                                 u8_t mul, u8_t add) {
  const uint16x8_t vmul = vdupq_n_u16(mul);
  const uint16x8_t vadd = vdupq_n_u16(add);
  const uint16x8_t vmask = vdupq_n_u16(0xFF);
  for (int i = 0; i < 256; i += 16) {
    uint8x16_t v = vld1q_u8(&val[i]);
    uint16x8_t lo = vmovl_u8(vget_low_u8(v));
    uint16x8_t hi = vmovl_u8(vget_high_u8(v));
    lo = vmulq_u16(lo, vmul);
    hi = vmulq_u16(hi, vmul);
    lo = vaddq_u16(lo, vadd);
    hi = vaddq_u16(hi, vadd);
    lo = vandq_u16(lo, vmask);
    hi = vandq_u16(hi, vmask);
    uint8x16_t res = vcombine_u8(vqmovn_u16(lo), vqmovn_u16(hi));
    vst1q_u8(&ual[i], res);
  }
}

static inline void simd_mul_add_dual(const u8_t* __restrict__ val,
                                      u8_t* __restrict__ ual_attr,
                                      u8_t* __restrict__ ual_skill) {
  const uint16x8_t vmul  = vdupq_n_u16(181);
  const uint16x8_t vaddA = vdupq_n_u16(160);
  const uint16x8_t vaddS = vdupq_n_u16(71);
  const uint16x8_t vmask = vdupq_n_u16(0xFF);
  for (int i = 0; i < 256; i += 16) {
    uint8x16_t v = vld1q_u8(&val[i]);
    uint16x8_t lo = vmovl_u8(vget_low_u8(v));
    uint16x8_t hi = vmovl_u8(vget_high_u8(v));
    uint16x8_t loA = vaddq_u16(vmulq_u16(lo, vmul), vaddA);
    uint16x8_t hiA = vaddq_u16(vmulq_u16(hi, vmul), vaddA);
    uint16x8_t loS = vaddq_u16(vmulq_u16(lo, vmul), vaddS);
    uint16x8_t hiS = vaddq_u16(vmulq_u16(hi, vmul), vaddS);
    loA = vandq_u16(loA, vmask); hiA = vandq_u16(hiA, vmask);
    loS = vandq_u16(loS, vmask); hiS = vandq_u16(hiS, vmask);
    vst1q_u8(&ual_attr[i], vcombine_u8(vqmovn_u16(loA), vqmovn_u16(hiA)));
    vst1q_u8(&ual_skill[i], vcombine_u8(vqmovn_u16(loS), vqmovn_u16(hiS)));
  }
}
#endif

// ===== SIMD 范围过滤: ual[i] ∈ [89, 217) → name_base[++q_len] = ual[i] & 63 =====
// 遍历 256 字节，每次加载一个向量做范围比较，用位掩码 + tzcnt 提取匹配位置。
// mask bit0 对应 byte0（最低地址），ctz 从最低位取，保持标量稳定顺序。
// 与 simd_mul_add_filter 的分工: 本函数假定 ual 已由调用方算好 (例如 AVX-512 档
//   load_name 先走 simd_mul_add_dual 得到 ual_skills, 或标量回退路径手算 ual 后复用),
//   只做「筛选 + 压缩写」, 不再做乘法。q_len 达 max_len 即 break。
// 只有 AVX-512 / AVX2 两个实现: 无 SIMD 时调用方直接写标量循环 (见 name.hpp), 无需本函数。
#if PBB_HAS_AVX512
static inline void simd_filter_range_attr(const u8_t* __restrict__ ual,
                                           u8_t* __restrict__ name_base,
                                           int& q_len, int max_len) {
  const __m512i v89  = _mm512_set1_epi8(89);
  const __m512i v217 = _mm512_set1_epi8(217);
  for (int i = 0; i < 256; i += 64) {
    __m512i v = _mm512_loadu_si512((const __m512i*)&ual[i]);
    __mmask64 ge = _mm512_cmpge_epu8_mask(v, v89);
    __mmask64 lt = _mm512_cmplt_epu8_mask(v, v217);
    __mmask64 mask = ge & lt;
    while (mask && q_len < max_len) {
      int idx = _tzcnt_u64(mask);
      name_base[++q_len] = ual[i + idx] & 63;
      mask &= mask - 1;
    }
    if (q_len >= max_len) break;
  }
}

#elif PBB_HAS_AVX2
static inline void simd_filter_range_attr(const u8_t* __restrict__ ual,
                                           u8_t* __restrict__ name_base,
                                           int& q_len, int max_len) {
  // AVX2 无无符号字节比较，用饱和减模拟:
  //   x >= 89  ⇔  _mm256_subs_epu8(x, 88) > 0
  //   x < 217  ⇔  _mm256_subs_epu8(217, x) > 0
  const __m256i v88  = _mm256_set1_epi8(88);
  const __m256i v217 = _mm256_set1_epi8(217);
  const __m256i vzero = _mm256_setzero_si256();
  const __m256i vones = _mm256_cmpeq_epi8(vzero, vzero);
  for (int i = 0; i < 256; i += 32) {
    __m256i v = _mm256_loadu_si256((const __m256i*)&ual[i]);
    __m256i ge = _mm256_subs_epu8(v, v88);        // >0 iff x >= 89
    __m256i lt = _mm256_subs_epu8(v217, v);        // >0 iff x < 217
    __m256i ge_mask = _mm256_xor_si256(_mm256_cmpeq_epi8(ge, vzero), vones);
    __m256i lt_mask = _mm256_xor_si256(_mm256_cmpeq_epi8(lt, vzero), vones);
    unsigned mask = (unsigned)_mm256_movemask_epi8(_mm256_and_si256(ge_mask, lt_mask));
    while (mask && q_len < max_len) {
      int idx = _tzcnt_u32(mask);
      name_base[++q_len] = ual[i + idx] & 63;
      mask &= mask - 1;
    }
    if (q_len >= max_len) break;
  }
}
#endif

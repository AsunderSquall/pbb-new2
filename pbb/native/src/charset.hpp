#pragma once
// ============================================================================
// charset.hpp — 字符集加载与管理
//
// 提供三类字符集数据:
//   1. 扩展汉字 (hanzi_3/4): 预加载的 CJK Ext-A/B/C/D/E/F/G 区字符
//   2. 基础汉字 (charset): 按需加载的基本多文种平面 (BMP) 汉字
//   3. Unicode 任意区间: 通过 load_unicode_range 动态加载
//
// 所有函数声明为 inline，作为 header-only 库使用。
//
// 中文补充 (关键约束, 改动前必读):
//   1. 数组容量是「精确贴合」的: hanzi_3/hanzi_4 的长度正好等于 init_exhanzi() 里那 8 段
//      Unicode 区间的字符总数×编码宽度 (6400×3 与 64287×4)。若要增删区间, 必须同步改数组
//      长度, 否则会静默越界写坏相邻全局变量。
//   2. 字符数 ≠ 字节数: charset_len/hanzi_len_* 记的是「字符个数」, charset_cnt/hanzi_cnt_*
//      记的是「已写入的字节下标」, 两者不可互换。缓存区按 UTF-8 字节流连续存放, 取第 i 个
//      字符必须由枚举侧知道该字符的编码宽度 (即配置里的「字节宽度」), 不能靠扫描推算。
//   3. 空/未初始化的哨兵是 -1 (charset_cnt = -1 表示缓冲区为空), 因此写入前先 ++cnt 再写,
//      首字符恰好落在下标 0。
//   4. 这些是全局可变状态且非线程安全: 必须在启动工作线程之前完成加载, 线程中只读。
// ============================================================================
#include "common.hpp"

// ===== 扩展汉字数据 (CJK Ext-A ~ Ext-G) =====
// hanzi_3: 长度 3 字节的扩展汉字 (6400 个), 每个 3 字节 UTF-8
// hanzi_4: 长度 4 字节的扩展汉字 (64287 个), 每个 4 字节 UTF-8
inline int hanzi_3[19200];           // 6400 chars × 3 bytes
inline int hanzi_4[257148];          // 64287 chars × 4 bytes
inline int hanzi_cnt_3 = -1;         // hanzi_3 当前写入位置
inline int hanzi_len_3;              // hanzi_3 中的字符数量
inline int hanzi_cnt_4 = -1;         // hanzi_4 当前写入位置
inline int hanzi_len_4;              // hanzi_4 中的字符数量
inline bool exhanzi_initialized = false;

// ===== 基础汉字字符集缓冲区 =====
// charset: 存储 load_hanzi / load_unicode_range 加载的 UTF-8 字节流
// charset_len: 字符数量 (非字节数)
// charset_cnt: 当前字节写入位置 (-1 表示空)
inline char charset[1000005];
inline int charset_cnt = -1;
inline int charset_len = 0;

// ===== load_exhanzi: 将 Unicode 区间加载到扩展汉字数组 =====
// 根据 UTF-8 编码长度分入 hanzi_3 (3字节) 或 hanzi_4 (4字节)。
// 不处理 1-2 字节字符 (扩展汉字通常在 U+3400 以上)。
// 参数: start/end — Unicode 码点区间 (含两端)
// 写入策略: 按编码长度分流到两个独立缓冲区, 各自维护「字节下标 + 字符计数」。
// 注意: 本函数不做容量检查, 也只在 exhanzi_initialized 为 false 时由一个线程调用;
//       3 字节字符 (U+3400..U+4DBF) 与 4 字节字符 (U+20000 起) 分别累加, 互不影响。
inline void load_exhanzi(int start, int end) {
  for (int cp = start; cp <= end; cp++) {
    unsigned char utf8[4];
    int len = 0;
    if (cp <= 0x7F) {
      utf8[len++] = cp;
    } else if (cp <= 0x7FF) {
      utf8[len++] = 0xC0 | (cp >> 6);
      utf8[len++] = 0x80 | (cp & 0x3F);
    } else if (cp <= 0xFFFF) {
      utf8[len++] = 0xE0 | (cp >> 12);
      utf8[len++] = 0x80 | ((cp >> 6) & 0x3F);
      utf8[len++] = 0x80 | (cp & 0x3F);
    } else if (cp <= 0x10FFFF) {
      utf8[len++] = 0xF0 | (cp >> 18);
      utf8[len++] = 0x80 | ((cp >> 12) & 0x3F);
      utf8[len++] = 0x80 | ((cp >> 6) & 0x3F);
      utf8[len++] = 0x80 | (cp & 0x3F);
    }
    if (len == 4) {
      hanzi_len_4++;
      memcpy(&hanzi_4[hanzi_cnt_4 + 1], utf8, len);
      hanzi_cnt_4 += len;
    } else if (len == 3) {
      hanzi_len_3++;
      memcpy(&hanzi_3[hanzi_cnt_3 + 1], utf8, len);
      hanzi_cnt_3 += len;
    }
  }
}

// ===== init_exhanzi: 初始化所有扩展汉字数据 =====
// 按 Unicode 区块加载:
//   U+3400..U+4DBF    — CJK Ext-A (6592 汉字)
//   U+20000..U+2A6DF  — CJK Ext-B (42720 汉字)
//   U+2A700..U+2B739  — CJK Ext-C (4154 汉字)
//   U+2B740..U+2B81D  — CJK Ext-D (222 汉字)
//   U+2B820..U+2CEA1  — CJK Ext-E (5762 汉字)
//   U+2CEB0..U+2EBE0  — CJK Ext-F (7473 汉字)
//   U+2EBF0..U+2EE5D  — CJK Ext-G (4939 汉字)
// 总计约 6400 个 3 字节 + 64287 个 4 字节扩展汉字
// 幂等: exhanzi_initialized 只用于防止重复累加 (重复调用会把字符数翻倍导致越界)。
// 上表注释里的区块名与「实际传入的码点区间」并不完全对齐 (部分段合并/近似), 以代码中的
// 数值区间为准; 这些数值与数组容量严格配套, 不要单独改动。
inline void init_exhanzi() {
  if (exhanzi_initialized) return;
  load_exhanzi(13312, 19711);       // CJK Ext-A: U+3400..U+4DBF
  load_exhanzi(131072, 175999);     // CJK Ext-B: U+20000..U+2AF9F
  load_exhanzi(172032, 173782);     // CJK Ext-C: U+2A700..U+2B739
  load_exhanzi(173824, 177972);     // CJK Ext-D: U+2B740..U+2B81D
  load_exhanzi(177984, 178175);     // CJK Ext-E: U+2B820..U+2CEA1 (approx)
  load_exhanzi(178176, 178431);     // CJK Ext-F: (approx)
  load_exhanzi(178432, 183969);     // CJK Ext-E cont.
  load_exhanzi(183984, 191456);     // CJK Ext-F/G
  exhanzi_initialized = true;
}

// ===== load_hanzi: 加载 BMP 基本汉字到 charset 缓冲区 =====
// 用于加载 U+4E00..U+9FFF (CJK Unified Ideographs, 20992 个汉字)。
// 注意: 此函数直接写入全局 charset 缓冲区，使用前应先 reset_charset()。
// 语义约束: 该路径假定每个字符都是 3 字节 (BMP 基本汉字)。若区间里出现 4 字节字符,
//   只打一条 stderr 告警却仍然写 4 字节, 会让后续按「固定字节宽度」切分的枚举错位,
//   因此这里只应传入 BMP 区间; 需要 4 字节请用 load_unicode_range。
inline void reset_charset() { charset_cnt = -1; charset_len = 0; }
inline void load_hanzi(int start, int end) {
  for (int cp = start; cp <= end; cp++) {
    unsigned char utf8[4];
    int len = 0;
    if (cp <= 0x7F) {
      utf8[len++] = cp;
    } else if (cp <= 0x7FF) {
      utf8[len++] = 0xC0 | (cp >> 6);
      utf8[len++] = 0x80 | (cp & 0x3F);
    } else if (cp <= 0xFFFF) {
      utf8[len++] = 0xE0 | (cp >> 12);
      utf8[len++] = 0x80 | ((cp >> 6) & 0x3F);
      utf8[len++] = 0x80 | (cp & 0x3F);
    } else if (cp <= 0x10FFFF) {
      utf8[len++] = 0xF0 | (cp >> 18);
      utf8[len++] = 0x80 | ((cp >> 12) & 0x3F);
      utf8[len++] = 0x80 | ((cp >> 6) & 0x3F);
      utf8[len++] = 0x80 | (cp & 0x3F);
    }
    if (len == 4)
      fprintf(stderr, "WARNING!!! 4-byte char in load_hanzi\n");
    charset_len++;
    memcpy(&charset[charset_cnt + 1], utf8, len);
    charset_cnt += len;
  }
}

// ===== load_unicode_codepoint: 将单个 Unicode 码点编码为 UTF-8 =====
// 输出写入 out_buf (需至少 4 字节空间)。
// 返回值: UTF-8 字节数 (1-4), 非法码点返回 0
// 返回 0 是唯一的失败信号 (不抛异常); Python 侧 bridge.cpp::encode_unicode 会把
// 长度为 0 的 bytes 直接返回, 调用方需自行判空。
inline int load_unicode_codepoint(int codepoint, char* out_buf) {
  if (codepoint <= 0x7F) {
    out_buf[0] = codepoint;
    return 1;
  } else if (codepoint <= 0x7FF) {
    out_buf[0] = 0xC0 | (codepoint >> 6);
    out_buf[1] = 0x80 | (codepoint & 0x3F);
    return 2;
  } else if (codepoint <= 0xFFFF) {
    out_buf[0] = 0xE0 | (codepoint >> 12);
    out_buf[1] = 0x80 | ((codepoint >> 6) & 0x3F);
    out_buf[2] = 0x80 | (codepoint & 0x3F);
    return 3;
  } else if (codepoint <= 0x10FFFF) {
    out_buf[0] = 0xF0 | (codepoint >> 18);
    out_buf[1] = 0x80 | ((codepoint >> 12) & 0x3F);
    out_buf[2] = 0x80 | ((codepoint >> 6) & 0x3F);
    out_buf[3] = 0x80 | (codepoint & 0x3F);
    return 4;
  }
  return 0;
}

// ===== load_unicode_range: 加载 Unicode 码点区间到 charset 缓冲区 =====
// 内联 UTF-8 编码 + memcpy 批量写入，避免逐码点函数调用。
// 与 load_hanzi 的区别: 这里是「任意区间」入口, 允许 1~4 字节混合; 但枚举侧的字节宽度
// 是配置项, 因此调用方 (engine.py) 有责任保证所选区间的字符宽度与 charset_len 一致。
// 非追加语义: 与 load_hanzi 一样从当前 charset_cnt 续写, 通常先 reset_charset()。
// 超过 U+10FFFF 的码点被 continue 跳过 (不计数也不写入)。
inline void load_unicode_range(int start, int end) {
  for (int cp = start; cp <= end; cp++) {
    char buf[4];
    int len;
    if (cp <= 0x7F) {
      buf[0] = cp; len = 1;
    } else if (cp <= 0x7FF) {
      buf[0] = 0xC0 | (cp >> 6);
      buf[1] = 0x80 | (cp & 0x3F);
      len = 2;
    } else if (cp <= 0xFFFF) {
      buf[0] = 0xE0 | (cp >> 12);
      buf[1] = 0x80 | ((cp >> 6) & 0x3F);
      buf[2] = 0x80 | (cp & 0x3F);
      len = 3;
    } else if (cp <= 0x10FFFF) {
      buf[0] = 0xF0 | (cp >> 18);
      buf[1] = 0x80 | ((cp >> 12) & 0x3F);
      buf[2] = 0x80 | ((cp >> 6) & 0x3F);
      buf[3] = 0x80 | (cp & 0x3F);
      len = 4;
    } else { continue; }
    charset_len++;
    memcpy(&charset[charset_cnt + 1], buf, len);
    charset_cnt += len;
  }
}

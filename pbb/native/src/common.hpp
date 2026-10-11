#pragma once
// ============================================================================
// common.hpp — type aliases, global constants
//
// PBB core infrastructure. AVX2 controlled by compiler -mavx2 flag.
//
// 中文总述: 本文件是所有 C++ 源文件的最底层共享头, 只做两件事:
//   1) 探测当前编译目标的 SIMD 能力并给出统一宏 (PBB_HAS_*), 供 utils.hpp /
//      name.hpp / scoring_1124.hpp 选择「向量化路径」还是「标量回退路径」;
//   2) 定义全局类型别名 (u64_t / u8_t) 与算法所需的尺寸常量 (N / M / K / skill_cnt)。
// 重要约束: 这些宏完全由「编译期命令行开关」决定 (-mavx2 / -mavx512f / -march=native),
//   因此同一份源码在不同机器上可能编出不同实现路径。发布包使用保守 x86-64 基础指令集,
//   本机 native 构建才可能启用 AVX2/AVX-512。运行期不做指令集二次派发。
// ============================================================================

// AVX2: compiler passes -mavx2 to define __AVX2__
#if defined(__AVX2__)
  #include <immintrin.h>
  #define PBB_HAS_AVX2 1
#else
  #define PBB_HAS_AVX2 0
#endif

// AVX-512: -mavx512f -mavx512bw
// 必须同时具备 F (基础) 与 BW (字节/字宽) 才认定为可用: 技能过滤与属性过滤都用
// 字节粒度的掩码比较 (cmpge_epu8 / cmplt_epu8 / mask_compressstoreu_epi8)。
#if defined(__AVX512F__) && defined(__AVX512BW__)
  #define PBB_HAS_AVX512 1
#else
  #define PBB_HAS_AVX512 0
#endif

// ARM NEON: aarch64 强制特性
#if defined(__aarch64__) || defined(__ARM_NEON)
  #include <arm_neon.h>
  #define PBB_HAS_NEON 1
#else
  #define PBB_HAS_NEON 0
#endif

// 是否存在任意 SIMD 后端
// PBB_HAS_SIMD 是一个「总开关」: 打开后 Name 结构体会多出 ual_skills/saved_val/
// prefix_loaded 三个成员 (见 name.hpp), 因此结构性内存布局也随之改变。
// 这是纯编译期常量, 不能在同一二进制内混用两条路径。
#define PBB_HAS_SIMD (PBB_HAS_AVX2 || PBB_HAS_AVX512 || PBB_HAS_NEON)

// SIMD 名称字符串 (编译期确定)
// 仅用于日志/构建信息展示 (engine.hpp 启动时写一行 "[engine] SIMD: xxx"),
// 不参与任何计算分支。自动探测到指令集支持 ≠ 该路径已在本机测速证明更快。
#if PBB_HAS_AVX512
  #define PBB_SIMD_NAME "AVX-512"
#elif PBB_HAS_AVX2
  #define PBB_SIMD_NAME "AVX2"
#elif PBB_HAS_NEON
  #define PBB_SIMD_NAME "NEON"
#else
  #define PBB_SIMD_NAME "none"
#endif

// Windows CRT 安全警告
// MSVC 会因 fopen/memcpy/strcpy 等报 C4996; 这里在包含 CRT 头之前抑制,
// 保证与 GCC 行为一致。只影响编译告警, 不影响运行语义。
#if defined(_WIN32) || defined(_WIN64)
  #ifndef _CRT_SECURE_NO_WARNINGS
    #define _CRT_SECURE_NO_WARNINGS
  #endif
#endif

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <iostream>
#include <mutex>
#include <random>
#include <string>
#include <vector>

// ===== Type aliases =====
// u64_t: 枚举/范围/种子的通用无符号整型 (range_L/range_R 等外部参数按它解析)。
// u8_t : RC4 S-box 与所有中间字节数组的载体; 取模语义全部依赖它的 8 位回绕。
typedef unsigned long long u64_t;
typedef unsigned char u8_t;

// ===== Global constants =====
// 以下四个常量是评分算法的尺寸契约, 改动任一个都会改变评分结果或越界:
const int N = 256;        // RC4 S-box size
                          // 必须是 256: 算法用 (val[p]+val[q]) & 255 与 u8_t 自然回绕取索引, 且 KSA 恰好跑满 i∈[0,256)。
const int M = 128;        // name_base size
                          // 容量哨兵: 技能过滤把 u8_t 值高位为 0 的项 (恰好 128 个) 连续写入 name_base[0..127],
                          // 因此 128 是「写满但不越界」的硬上界; 属性过滤只用到前 30 项 (name_base[0..30])。
const int K = 64;         // skill group offset
                          // 技能值写入 name_base[K..K+63] = name_base[64..127], 与属性区 (0..30) 物理隔离。
const int skill_cnt = 40; // total skills
                          // 技能 ID 空间 0..39; 其中 35..39 是占位项, 无实际技能效果。

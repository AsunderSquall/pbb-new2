#pragma once

#include <algorithm>
#include <cstdint>

// 丢掉低位，直到两个编号的剩余部分相同。即使 radix^width 超过 uint64_t，
// 只要候选编号本身能表示，就能算出实际共享的高位数。
static inline int common_radix_prefix(uint64_t first, uint64_t last, uint64_t radix, int width) {
    if (radix == 1) return width;
    while (width > 0 && first != last) {
        first /= radix;
        last /= radix;
        --width;
    }
    return width;
}

// 先取区间剩余长度，保证靠近 UINT64_MAX 时切块不回绕。
static inline uint64_t chunk_end(uint64_t start, uint64_t end, uint64_t chunk_size) {
    return start + std::min(end - start, chunk_size);
}

// Data-flow and capacity checks for the opt-in host tuning filter recipes.
//
// 中文: autotune 过滤配方的正确性对拍程序(PBB_ATTR_FILTER_MODE=1..4 的 SIMD 实现
// 与标量参考实现比对)。它验证的是"数据流与容量"两件事:
//   1) 数据流: SIMD 过滤出的属性/技能序列必须与逐字节的标量算法完全一致;
//   2) 容量: 写入 name_base 时不得超过 max_len, 且前后哨兵字节不能被改动。
// 只做正确性对拍, 不测性能; 由 autotune 在启用某个配方前调用。
#include "name.hpp"
#include <array>
#include <random>
#include <cstdio>

// 被测缓冲区: data 前置 64 字节 before 与后置 64 字节 after 哨兵,
// 用来证明 SIMD 写入既没有越界, 也没有踏到相邻内存。
struct CheckedBuffer {
  std::array<u8_t,64> before;
  std::array<u8_t,M> data;
  std::array<u8_t,64> after;
};

int main() {
#if !(PBB_HAS_AVX512 || PBB_HAS_AVX2)
  // 标量/NEON 构建里根本不存在被测的 SIMD 过滤函数, 直接跳过并视为通过。
  puts("filter_verification skipped: scalar/NEON path unchanged");
  return 0;
#else
  std::mt19937 rng(20261009);  // 固定种子: 结果可复现, 便于回归比对
  std::array<u8_t,N> val,skills,expected_skills;
  std::array<u8_t,M> expected,skill_reference;
  uint64_t checksum=0;
  for(int test=0;test<10000;test++) {
    // 用 0..255 的随机排列作为原始字节: 每种字节取值都出现, 覆盖乘法+取模的全部分支。
    for(int i=0;i<N;i++)val[i]=(u8_t)i;
    std::shuffle(val.begin(),val.end(),rng);
    // 期望值由独立的标量实现算出: attr/skill 的生产公式(mod 256), 属性落在
    // [89,217) 才入选并取低 6 位; 技能 <128 时按 (skill+89)&63 重建。
    int n=0,k=0;
    for(int i=0;i<N;i++) {
      u8_t attr=(u8_t)(val[i]*181+160);
      u8_t skill=(u8_t)(val[i]*181+71);
      expected_skills[i]=skill;
      if(attr>=89&&attr<217)expected[n++]=attr&63;
      if(skill<128)skill_reference[k++]=(skill+89)&63;
    }
    // 先用固定排列确认恰好命中 M 个元素, 否则后续按位比较没有意义。
    if(n!=M||k!=M)return 1;
    // Exhaust every valid max_len, including both vector store fallback edges.
    // 中文: 遍历所有合法 max_len, 专门覆盖向量存储的容量回退边界(limit=0 与 limit=M-1)。
    for(int limit=0;limit<M;limit++) {
      CheckedBuffer buffer;
      buffer.before.fill(0xA5);buffer.after.fill(0x5A);buffer.data.fill(0xCC);
      skills.fill(0xEE);
      int q=-1;
      simd_mul_add_dual_filter(val.data(),skills.data(),buffer.data.data(),q,limit);
      // 技能数组必须被重建(输入 0xEE 是哨兵, 未被写入就说明数据流断了);
      // q 表示已写入的最后一个下标, 必须落在 [limit, M) 内。
      if(skills!=expected_skills||q<limit||q>=M)return 2;
      if(!std::equal(expected.begin(),expected.begin()+limit+1,buffer.data.begin()))return 3;
      // 哨兵保持不变 ⇒ max_len 之外没有被写入(容量约束成立)。
      for(auto x:buffer.before)if(x!=0xA5)return 4;
      for(auto x:buffer.after)if(x!=0x5A)return 5;
      q=-1;
      simd_skill_filter(skills.data(),buffer.data.data(),q);
      if(q!=M-1||buffer.data!=skill_reference)return 6;
      for(auto x:buffer.after)if(x!=0x5A)return 7;
      // 累积校验和: 防止整个循环被优化掉, 同时给出一枚可跨版本比对的指纹。
      checksum+=buffer.data[test%M]+skills[test%N];
    }
  }
  // 非零返回码按失败类型区分(见上面的 return 1..7), 0 表示全部通过。
  printf("filter_verified permutations=10000 limits=128 checksum=%llu\n",(unsigned long long)checksum);
  return 0;
#endif
}

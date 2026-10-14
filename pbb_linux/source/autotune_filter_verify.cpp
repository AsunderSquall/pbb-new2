// Data-flow and capacity checks for the opt-in host tuning filter recipes.
#include "name.hpp"
#include <array>
#include <random>
#include <cstdio>

struct CheckedBuffer {
  std::array<u8_t,64> before;
  std::array<u8_t,M> data;
  std::array<u8_t,64> after;
};

int main() {
#if !(PBB_HAS_AVX512 || PBB_HAS_AVX2)
  puts("filter_verification skipped: scalar/NEON path unchanged");
  return 0;
#else
  std::mt19937 rng(20261009);
  std::array<u8_t,N> val,skills,expected_skills;
  std::array<u8_t,M> expected,skill_reference;
  uint64_t checksum=0;
  for(int test=0;test<10000;test++) {
    for(int i=0;i<N;i++)val[i]=(u8_t)i;
    std::shuffle(val.begin(),val.end(),rng);
    int n=0,k=0;
    for(int i=0;i<N;i++) {
      u8_t attr=(u8_t)(val[i]*181+160);
      u8_t skill=(u8_t)(val[i]*181+71);
      expected_skills[i]=skill;
      if(attr>=89&&attr<217)expected[n++]=attr&63;
      if(skill<128)skill_reference[k++]=(skill+89)&63;
    }
    if(n!=M||k!=M)return 1;
    // Exhaust every valid max_len, including both vector store fallback edges.
    for(int limit=0;limit<M;limit++) {
      CheckedBuffer buffer;
      buffer.before.fill(0xA5);buffer.after.fill(0x5A);buffer.data.fill(0xCC);
      skills.fill(0xEE);
      int q=-1;
      simd_mul_add_dual_filter(val.data(),skills.data(),buffer.data.data(),q,limit);
      if(skills!=expected_skills||q<limit||q>=M)return 2;
      if(!std::equal(expected.begin(),expected.begin()+limit+1,buffer.data.begin()))return 3;
      for(auto x:buffer.before)if(x!=0xA5)return 4;
      for(auto x:buffer.after)if(x!=0x5A)return 5;
      q=-1;
      simd_skill_filter(skills.data(),buffer.data.data(),q);
      if(q!=M-1||buffer.data!=skill_reference)return 6;
      for(auto x:buffer.after)if(x!=0x5A)return 7;
      checksum+=buffer.data[test%M]+skills[test%N];
    }
  }
  printf("filter_verified permutations=10000 limits=128 checksum=%llu\n",(unsigned long long)checksum);
  return 0;
#endif
}

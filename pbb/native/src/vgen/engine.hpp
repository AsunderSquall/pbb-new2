#pragma once
// ============================================================================
// engine.hpp (vgen) — 虚拟号生成器 (VGEN)
//
// 目的: 不枚举真实名字, 而是直接随机生成符合名竞出现分布的 name_base 序列,
//       并复用主引擎的门控/评分/ptt 逻辑 (scoring_1124.hpp), 提前淘汰大量候选。
//
// 分布依据: name_base (过滤后) 恒为多重集 {0,0,1,1,...,63,63} 的一个均匀随机排列。
//   ⇒ 可视为 0..127 的一个均匀随机排列, 每项 >>1 即得 name_base 项 (跳过 RC4 KSA)。
//
// 增量生成: 按「计算属性 + 三级早退」的顺序逐个位置采样 (partial Fisher-Yates),
//   门控任一档淘汰即停止采样, 显著减少随机数消耗与后续计算。
//
// 输出: 每个 keep 候选一行, 92 个浮点数 = xp_x[0..45] + x2[0..45]。
// ============================================================================
#include "common.hpp"
#include "scoring_1124.hpp"
#include "utils.hpp"
#include <thread>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <string>
#include <vector>
#include <chrono>
#include <unordered_map>

// 每个 chunk 处理多少个虚拟候选
#define VGEN_CHUNK_SIZE 1000000ULL

// ---- 可选性能插桩 (编译时 -DVGEN_STATS 开启, 生产构建零开销) ----
#ifdef VGEN_STATS
// [0]=①免费淘汰(12d) [1]=②免费淘汰(21d) [2]=最终level0 [3]=level1 [4]=level2
static thread_local unsigned long long g_st_exit[5];
static thread_local unsigned long long g_st_draws;
#define VG_STAT_EXIT(i) (g_st_exit[i]++)
#define VG_STAT_DRAW()  (g_st_draws++)
#else
#define VG_STAT_EXIT(i) do {} while (0)
#define VG_STAT_DRAW()  do {} while (0)
#endif

// ---- 确定性子种子派生 (与主引擎 splitmix64 逐字一致) ----
static inline uint64_t vg_splitmix64(uint64_t x) {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

// ===== 可自定义顺序的 partial Fisher-Yates (swap-remove) =====
// 维护池 R[0..N-1] 与剩余计数 rem; 每次 draw 均匀取一个未用标签并移除。
// 对固定位置顺序调用 draw 即得「均匀随机排列投影到这些位置」, 与位置顺序无关。
// 复杂度 O(1)/draw, 极少的随机数消耗 ⇒ 硬件层面飞快。
static const u8_t vg_ident[128] = {
     0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15,
    16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
    32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47,
    48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63,
    64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79,
    80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95,
    96, 97, 98, 99,100,101,102,103,104,105,106,107,108,109,110,111,
   112,113,114,115,116,117,118,119,120,121,122,123,124,125,126,127
};
template <int N_>
struct VPerm {
    u8_t R[N_];
    u64_t s;
    int rem;
    void init(u64_t seed) {
        memcpy(R, vg_ident, (size_t)N_);   // 常量表拷贝, 向量化成 ~2 条 64B 存
        rem = N_;
        s = seed;
    }
    // splitmix64 计数流 + murmur3 fmix64: 每次只剩 1 个串行加法 (s += 常量),
    // 其余混合运算各 draw 相互独立 ⇒ 乱序执行可充分重叠, 远快于 xorshift 的
    // 三次串行移位异或链 (后者每次 next 有 ~12 周期硬依赖)。
    inline u64_t next() {
        s += 0x9E3779B97F4A7C15ULL;
        u64_t z = s;
        z ^= z >> 33; z *= 0xff51afd7ed558ccdULL; z ^= z >> 33;
        return z;
    }
    // 均匀取一个标签 (Lemire 无偏取模: 取高 32 位)
    inline u8_t draw() {
        VG_STAT_DRAW();
        u64_t r = next();
        unsigned idx = (unsigned)(((u64_t)(unsigned)(r >> 32) * (u64_t)rem) >> 32);
        u8_t lab = R[idx];
        R[idx] = R[--rem];
        return lab;
    }
    // name_base 项 = 排列项 >> 1 (等价 0..127 排列除以 2)
    inline u8_t draw_nb() { return (u8_t)(draw() >> 1); }
};

// ===== 一级 Q 门 (逐字对标 gate_level_of, 但边画边算以支持早退) =====
// 返回 0(淘汰)/1(PRE)/2(FREE); >0 时 P[0..6] 已填好, x0 已输出。
static inline void vg_draw3(VPerm<128>& pm, u8_t* nb, int a, int b, int c) {
    nb[a] = pm.draw_nb(); nb[b] = pm.draw_nb(); nb[c] = pm.draw_nb();
}
static inline int vg_gate(VPerm<128>& pm, u8_t* nb, int* P, int& x0) {
    // ---- 第 1+2 级: 位置 28-30 / 13-15 / 16-18 / 25-27 (12 draws) ----
    vg_draw3(pm, nb, 28, 29, 30);
    vg_draw3(pm, nb, 13, 14, 15);
    vg_draw3(pm, nb, 16, 17, 18);
    vg_draw3(pm, nb, 25, 26, 27);
    const int P7 = P[6] = median(nb[28], nb[29], nb[30]);
    const int P2 = P[1] = median(nb[13], nb[14], nb[15]);
    const int P3 = P[2] = median(nb[16], nb[17], nb[18]);
    const int P6 = P[5] = median(nb[25], nb[26], nb[27]);
    int Fa = GATE_F_W7 * P7 + GATE_F_W2 * P2 + GATE_F_W3 * P3 + GATE_F_W6 * P6;
    int Ra = GATE_R_W7 * P7 + GATE_R_W2 * P2 + GATE_R_W3 * P3 + GATE_R_W6 * P6;
    int Sa = GATE_S_W7 * P7 + GATE_S_W2 * P2 + GATE_S_W3 * P3 + GATE_S_W6 * P6;
    int Ta = GATE_T_W7 * P7 + GATE_T_W2 * P2 + GATE_T_W3 * P3 + GATE_T_W6 * P6;
    // ① 免费淘汰 (剩余维取最大仍 <θ)
    if (Fa + GATE_F_REM2 < GATE_F_TH && Ra + GATE_R_REM2 < GATE_R_TH &&
        Sa + GATE_S_REM2 < GATE_S_TH && Ta + GATE_T_REM2 < GATE_T_TH) { VG_STAT_EXIT(0); return 0; }

    // ---- 第 3 级: 位置 10-12 / 19-21 / 22-24 (9 draws) ----
    vg_draw3(pm, nb, 10, 11, 12);
    vg_draw3(pm, nb, 19, 20, 21);
    vg_draw3(pm, nb, 22, 23, 24);
    const int P1 = P[0] = median(nb[10], nb[11], nb[12]);
    const int P4 = P[3] = median(nb[19], nb[20], nb[21]);
    const int P5 = P[4] = median(nb[22], nb[23], nb[24]);
    const int Fb = Fa + GATE_F_W1 * P1 + GATE_F_W4 * P4 + GATE_F_W5 * P5;
    int Rb = Ra + GATE_R_W1 * P1 + GATE_R_W4 * P4 + GATE_R_W5 * P5;
    int Sb = Sa + GATE_S_W1 * P1 + GATE_S_W4 * P4 + GATE_S_W5 * P5;
    int Tb = Ta + GATE_T_W1 * P1 + GATE_T_W4 * P4 + GATE_T_W5 * P5;
    // ② 免费淘汰 (只剩付费维 x0, 取最大仍 <θ)
    if (Fb + GATE_F_REM3 < GATE_F_TH && Rb + GATE_R_REM3 < GATE_R_TH &&
        Sb + GATE_S_REM3 < GATE_S_TH && Tb + GATE_T_REM3 < GATE_T_TH) { VG_STAT_EXIT(1); return 0; }

    // ---- 第 4 级: 位置 0-9 (10 draws) → sort10 → x0 ----
    for (int i = 0; i < 10; i++) nb[i] = pm.draw_nb();
    u8_t b[10];
    for (int i = 0; i < 10; i++) b[i] = nb[i];
    sort10_net(b);
    x0 = 154 + b[3] + b[4] + b[5] + b[6];
    if (Fb + GATE_F_W0 * x0 >= GATE_F_TH) { VG_STAT_EXIT(4); return 2; }   // FREE 直过
    if (Rb + GATE_R_W0 * x0 >= GATE_R_TH) { VG_STAT_EXIT(3); return 1; }   // PRE 待定
    if (Sb + GATE_S_W0 * x0 >= GATE_S_TH) { VG_STAT_EXIT(3); return 1; }
    if (Tb + GATE_T_W0 * x0 >= GATE_T_TH) { VG_STAT_EXIT(3); return 1; }
    VG_STAT_EXIT(2); return 0;
}

// ===== 虚拟暗影号: 独立生成 snb[0..30] (31 draws, 无早退) =====
// 输出 shadowi (SP1 语义) / shadowi2 (SP2 语义)。
static inline void vg_shadow(u64_t seed, double& sh1, double& sh2) {
    VPerm<128> sp; sp.init(seed);
    u8_t snb[31];
    for (int i = 0; i < 31; i++) snb[i] = sp.draw_nb();
    int s0 = median(snb[10], snb[11], snb[12]);
    int s1 = median(snb[13], snb[14], snb[15]);
    int s2 = median(snb[16], snb[17], snb[18]);
    int s3 = median(snb[19], snb[20], snb[21]);
    int s4 = median(snb[22], snb[23], snb[24]);
    int s5 = median(snb[25], snb[26], snb[27]);
    int s6 = median(snb[28], snb[29], snb[30]);
    u8_t b[10];
    for (int i = 0; i < 10; i++) b[i] = snb[i];
    sort10_net(b);  // loading_name 会 in-place sort10
    int s7 = 154 + b[3] + b[4] + b[5] + b[6];
    sh1 = s0*2.8 + s1*0.6 + s2*2.5 + s3*1.2 + s4*1.0 + s5*1.0 - s6*1.2 + s7*0.8;
    sh2 = s0*2.2 + s1*0.75 + s2*1.9 + s3*1.3 + s4*0.6 + s5*1.2 - s6*1.8 + s7*1.0;
}

// ===== 由 x1 构造 SP2 语义 x2 (逐字复刻 build_x2, 但用原始 skill/freq 数组) =====
static inline void vg_build_x2(const u8_t* skill, const u8_t* freq,
                               const double* x1, double* x2) {
    for (int i = 0; i < 46; i++) x2[i] = 0.0;
    for (int i = 0; i < 8; i++) x2[i] = x1[i];
    static const double skill_para[25] = {
        1, 1, 1, 0.5, 0.75, 0.75, 1, 1, 0.75, 0.5, 1, 1, 1, 0.75, 1, 0.75,
        0.2, 1, 0.75, 0.5, 0.3, 0.75, 0.75, 0.3, 0.75
    };
    double zd = 1.0, kill = 1.0;
    for (int k = 0; k < 16; k++) {
        int sk = skill[k];
        double f = freq[k];
        if (f == 0) continue;
        if (sk < 25) {
            x2[sk + 8] = zd * f;
            zd *= (1.0 - f * skill_para[sk] / 128.0);
        } else if (sk == 31 || sk == 32) {
            if (f >= 64.0) f = 64.0;
            x2[sk + 8] = kill * f;
            kill *= (1.0 - f * 0.8 / 128.0);
        } else {
            x2[sk + 8] = f;
        }
    }
    if (x2[37] <= 60.0) x2[37] = x2[37] * x2[37] / 60.0;
    else x2[37] = x2[37] * 2.0 - 60.0;
}

// ===== 单个虚拟号: 生成 → 门控 → 评分 → ptt, 结果写入 result =====
// 返回 true 表示通过所有门控 (result.flag=true); false 表示被淘汰。
static inline bool vgen_one(u64_t cseed, const ScorePlan& pl, bool need_sp2, bool ptt_any,
                            double* xp, double* x2, ScoreResult& result) {
    // nb[31..59] 恒不被读取 (仅用 {0..30,60..63,64..127}), 无需清零
    u8_t nb[M];

    // ---- 门控 + 增量采样 (位置 0..30) ----
    int P[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    int x0 = 0;
    VPerm<128> perm; perm.init(cseed);
    const int lvl = vg_gate(perm, nb, P, x0);
    if (lvl == 0) { result.flag = false; return false; }  // ~99.99% 在此早退

    // ---- 通过门控后才初始化输出 (门控是最热路径, 提前排除) ----
    memset(&result, 0, sizeof(result));
    memset(xp, 0, 46 * sizeof(double));
    memset(x2, 0, 46 * sizeof(double));

    // ---- V 值 (逐字复刻 finish_load_name 三级早退) ----
    int V = P[6];
    if (V >= 24) {
        V += P[1] + P[2] + P[5];
        if (V >= 165) {
            V += P[0] + P[3] + P[4];
            if (V >= 250) V += x0 / 3;
        }
    }

    // ---- 剩余采样: 位置 60..63 (4 draws) + 64..127 (64 draws) ----
    for (int p = 60; p <= 63; p++) nb[p] = perm.draw_nb();
    for (int p = 64; p < M; p++) nb[p] = perm.draw_nb();

    // ---- 技能顺序: 独立洗牌取 0..39 的均匀有序子集前 16 个 ----
    u8_t skill[16];
    {
        VPerm<40> sp; sp.init(vg_splitmix64(cseed ^ 0x94D049BB133111EBULL));
        for (int k = 0; k < 16; k++) skill[k] = sp.draw();
    }

    // ---- freq[0..15] (逐字复刻 calc_skills 的 freq 推导; a = nb + K) ----
    u8_t freq[16];
    int last = -1;
    for (int i = 0, j = 0; i < K; i += 4, j++) {
        u8_t p = std::min({nb[K+i], nb[K+i+1], nb[K+i+2], nb[K+i+3]});
        if (p > 10 && skill[j] < 35) { freq[j] = (u8_t)(p - 10); if (skill[j] < 25) last = j; }
        else freq[j] = 0;
    }
    if (last != -1) freq[last] = (u8_t)(freq[last] << 1);
    if (freq[14] && last != 14)
        freq[14] = (u8_t)(freq[14] + std::min({nb[60], nb[61], freq[14]}));
    if (freq[15] && last != 15)
        freq[15] = (u8_t)(freq[15] + std::min({nb[62], nb[63], freq[15]}));

    // ---- 构建 xp_x[0..45] (逐字复刻 score_full Step 4) ----
    int prop[8];
    for (int j = 0; j < 7; j++) prop[j] = P[j] + 36;
    prop[7] = x0;
    for (int j = 0; j < 7; j++) xp[j + 1] = prop[j];
    xp[0] = prop[7];
    for (int i = 0; i < 35; i++) xp[i + 8] = 0.0;

    double zd = 1.0, kill = 1.0;
    for (int k = 0; k < 16; k++) {
        int sk = skill[k];
        double f = freq[k];
        if (f == 0) continue;
        if (sk == 9 || sk == 16) { xp[sk + 8] = zd * f; zd *= (1.0 - f * 0.30 / 128.0); }
        else if (sk == 18)       { xp[sk + 8] = zd * f; zd *= (1.0 - f * 0.35 / 128.0); }
        else if (sk == 19 || sk == 23) { xp[sk + 8] = zd * f; zd *= (1.0 - f * 0.60 / 128.0); }
        else if (sk == 20 || sk == 22) { xp[sk + 8] = zd * f; zd *= (1.0 - f * 0.70 / 128.0); }
        else if (sk < 25)        { xp[sk + 8] = zd * f; zd *= (1.0 - f * 1.00 / 128.0); }
        else if (sk == 31 || sk == 32) { xp[sk + 8] = kill * f; kill *= (1.0 - f * 1.00 / 128.0); }
        else                     { xp[sk + 8] = f; }
    }
    if (xp[37] <= 70.0) xp[37] = xp[37] * xp[37] / 70.0;
    else                xp[37] = xp[37] * 2.0 - 70.0;

    // ---- SP2 语义 x2 ----
    if (need_sp2) vg_build_x2(skill, freq, xp, x2);

    // ---- 暗影 (仅当存在幻术来源) ----
    const u64_t sh_seed = vg_splitmix64(cseed ^ 0x9E3779B97F4A7C15ULL);
    double ptt_shadowi2 = 0.0;
    bool ptt_shadow_loaded = false;
    if (xp[32] > 0.0 || x2[32] > 0.0) {
        double sh1, sh2;
        vg_shadow(sh_seed, sh1, sh2);
        result.illusion_effective = xp[32];
        result.shadow_strength = sh1;
        xp[43] = (xp[32] > 0.0) ? sh1 * xp[32] / 100.0 : 0.0;
        x2[43] = (x2[32] > 0.0) ? sh2 * x2[32] / 100.0 : 0.0;
        ptt_shadowi2 = sh2;
        ptt_shadow_loaded = true;
    }

    // ---- 隐匿 / 守护 ----
    if (xp[42] > 0.0) xp[44] = 1.0;
    if (x2[42] > 0.0) x2[44] = 1.0;
    if (xp[37] > 0.0) { xp[45] = xp[26]; xp[26] = 0.0; }
    if (x2[37] > 0.0) { x2[45] = x2[26]; x2[26] = 0.0; }

    // ---- Step 5b: 共享二级门 + 每族一阶预筛 ----
    if (lvl == 1 && !skill_ok_of(xp)) { result.flag = false; return false; }
    const unsigned fam_mask = fam_pass_mask(pl, xp);

    // ---- Step 6: 逐族评分 ----
    result.sum = V + 36 * 7;
    result.flag = true;
    for (int k = 0; k < 16; ++k)
        if (skill[k] == 24) result.illusion_freq = freq[k];
    sp_eval_plan(pl, xp, x2, fam_mask, result);
    result.xp = result.sc[M_QP];
    result.xd = result.sc[M_QD];

    // ---- Step 7: ptt ----
    if (ptt_any) {
        bool any_normal_pass = false;
        for (int m = 0; m < M_N; m++) {
            if (score_normal_pass(pl, result, m)) {
                any_normal_pass = true; break;
            }
        }
        if (!any_normal_pass) {
            if (!ptt_shadow_loaded) {
                double sh1, sh2;
                vg_shadow(sh_seed, sh1, sh2);
                ptt_shadowi2 = sh2;
            }
            int ptt_last; bool f14, f15;
            ptt_fixed_of(nb, skill, ptt_last, f14, f15);
            for (int m = 0; m < M_N; m++) {
                if (!((fam_mask >> m) & 1u) || pl.pttv[m] <= 0) continue;
                if (pl.minv[m] > 0 && result.sc[m] >= pl.minv[m]) continue;
                result.sc_max[m] = ptt_score_max(m, nb, skill, ptt_last, f14, f15, ptt_shadowi2);
            }
        }
    }

    return true;
}

// ===== 引擎入口 =====
inline int engine_main(int argc, char** argv) {
    (void)argc; (void)argv;

    // ---- 从 stdin 读取参数 (与主引擎一致的 kv 格式) ----
    std::unordered_map<std::string, std::string> kv;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;
        size_t eq = line.find('=');
        if (eq != std::string::npos) kv[line.substr(0, eq)] = line.substr(eq + 1);
    }

    int n_threads = kv.count("n_threads") ? std::stoi(kv["n_threads"]) : 1;
    if (n_threads < 1) n_threads = 1;
    uint64_t rL = std::stoull(kv["range_L"]);
    uint64_t rR = std::stoull(kv["range_R"]);

    static const char* kMinKey[M_N] = {"min_qp","min_qd","min_pp","min_pd","min_cqd",
                                       "min_cp","min_fp","min_wc","min_fs","min_pj"};
    int min_[M_N];
    for (int m = 0; m < M_N; m++) min_[m] = kv.count(kMinKey[m]) ? std::stoi(kv[kMinKey[m]]) : 0;
    static const char* kPttKey[M_N] = {"","","","","","ptt_cp","ptt_fp","ptt_wc","ptt_fs","ptt_pj"};
    int ptt_[M_N];
    for (int m = 0; m < M_N; m++)
        ptt_[m] = (kPttKey[m][0] && kv.count(kPttKey[m])) ? std::stoi(kv[kPttKey[m]]) : 0;

    // ---- 评分计划 (与主引擎 ScorePlan 构造一致) ----
    ScorePlan plan = {}; plan.norder = 0;
    {
        static const struct { const char* tok; int m; } kScoreTok[] = {
            {"qp",M_QP},{"qd",M_QD},{"pp",M_PP},{"pd",M_PD},{"cqd",M_CQD},
            {"cp",M_CP},{"fp",M_FP},{"wc",M_WC},{"fs",M_FS},{"rh",M_FS},{"pj",M_PJ}};
        for (int m = 0; m < M_N; m++) plan.comp[m] = false;
        std::string sl = kv.count("score_list") ? kv["score_list"] : "";
        if (!sl.empty()) {
            std::string cur;
            auto flush = [&]() {
                size_t a = cur.find_first_not_of(" \t");
                if (a == std::string::npos) { cur.clear(); return; }
                size_t b = cur.find_last_not_of(" \t");
                std::string t = cur.substr(a, b - a + 1);
                for (auto& e : kScoreTok) if (t == e.tok) {
                    if (!plan.comp[e.m]) { plan.comp[e.m] = true; plan.order[plan.norder++] = e.m; }
                    break;
                }
                cur.clear();
            };
            for (char c : sl) { if (c == ',') flush(); else cur += c; }
            flush();
        }
        if (plan.norder == 0) for (int m = 0; m < M_N; m++) { plan.comp[m] = true; plan.order[plan.norder++] = m; }
        for (int m = 0; m < M_N; m++) plan.minv[m] = min_[m];
        for (int m = 0; m < M_N; m++) plan.pttv[m] = ptt_[m];
        std::string sm = kv.count("score_mode") ? kv["score_mode"] : "all";
        plan.any_mode = (sm == "any");
    }

    const bool need_sp2 = plan.comp[M_CP] || plan.comp[M_FP] || plan.comp[M_WC]
                       || plan.comp[M_FS] || plan.comp[M_PJ];
    bool ptt_any = false, any_gate = false;
    for (int m = 0; m < M_N; m++) {
        if (!plan.comp[m]) continue;
        if (plan.pttv[m] > 0) ptt_any = true;
        if (min_[m] > 0 || ptt_[m] > 0) any_gate = true;
    }

    // ---- 输出文件 (与主引擎同目录 out/) ----
    std::string out_path = "out/" + kv["result_file"];
    FILE* fp = fopen(out_path.c_str(), "a");
    if (!fp) { fprintf(stderr, "[vgen] ERROR: cannot open %s\n", out_path.c_str()); return 1; }

    // ---- 随机种子 ----
    uint64_t g_seed;
    {
        auto it = kv.find("seed");
        if (it != kv.end() && !it->second.empty() && it->second != "-1")
            g_seed = std::stoull(it->second);
        else
            g_seed = (uint64_t)std::chrono::steady_clock::now().time_since_epoch().count();
    }

    auto t_start = std::chrono::steady_clock::now();
    std::atomic<uint64_t> nxt{rL};
    std::atomic<unsigned long long> total_found{0};
    std::mutex out_mtx;
    int max_sum = 0, max_xp = 0, max_xd = 0;
#ifdef VGEN_STATS
    unsigned long long g_st_exit_all[5] = {0,0,0,0,0};
    unsigned long long g_st_draws_all = 0;
#endif

    auto worker = [&]() {
        double xp[46], x2[46];
        ScoreResult result;
        unsigned long long local_found = 0;
        int l_max_sum = 0, l_max_xp = 0, l_max_xd = 0;
        for (;;) {
            uint64_t cL = nxt.fetch_add(VGEN_CHUNK_SIZE);
            if (cL >= rR) break;
            uint64_t cR = cL + VGEN_CHUNK_SIZE; if (cR > rR) cR = rR;
            for (uint64_t i = cL; i < cR; i++) {
                uint64_t cseed = vg_splitmix64(i) ^ g_seed;
                if (!vgen_one(cseed, plan, need_sp2, ptt_any, xp, x2, result)) continue;
                const int* sc = result.sc;
                if (result.sum > l_max_sum) l_max_sum = result.sum;
                if (result.xp > l_max_xp) l_max_xp = result.xp;
                if (result.xd > l_max_xd) l_max_xd = result.xd;
                bool keep = score_output_keep(plan, result, any_gate);
                if (keep) {
                    std::lock_guard<std::mutex> lk(out_mtx);
                    local_found++;
                    for (int k = 0; k < 46; k++) fprintf(fp, "%.9g ", xp[k]);
                    for (int k = 0; k < 46; k++) fprintf(fp, "%s%.9g", k ? " " : "", x2[k]);
                    fputc('\n', fp);
                }
            }
            fflush(fp);
        }
        std::lock_guard<std::mutex> lk(out_mtx);
        total_found += local_found;
        if (l_max_sum > max_sum) max_sum = l_max_sum;
        if (l_max_xp > max_xp) max_xp = l_max_xp;
        if (l_max_xd > max_xd) max_xd = l_max_xd;
#ifdef VGEN_STATS
        for (int i = 0; i < 5; i++) g_st_exit_all[i] += g_st_exit[i];
        g_st_draws_all += g_st_draws;
#endif
    };

    std::vector<std::thread> ts;
    for (int i = 0; i < n_threads; i++) ts.emplace_back(worker);
    for (auto& t : ts) t.join();

    {
        auto t_end = std::chrono::steady_clock::now();
        double sec = std::chrono::duration<double>(t_end - t_start).count();
        unsigned long long processed = rR - rL;
        double speed = sec > 0 ? (double)processed / sec : 0.0;
        fprintf(stderr, "SUMMARY max_sum=%d max_xp=%d max_xd=%d found=%llu count=%llu elapsed=%.6f speed=%.6f\n",
                max_sum, max_xp, max_xd, total_found.load(), processed, sec, speed);
#ifdef VGEN_STATS
        {
            unsigned long long tot = 0;
            for (int i = 0; i < 5; i++) tot += g_st_exit_all[i];
            if (tot == 0) tot = 1;
            fprintf(stderr, "VSTATS draws=%llu avg_draws=%.3f\n",
                    g_st_draws_all, (double)g_st_draws_all / (double)tot);
            static const char* nm[5] = {"exit_free1", "exit_free2", "exit_lvl0", "exit_lvl1", "exit_lvl2"};
            for (int i = 0; i < 5; i++)
                fprintf(stderr, "VSTATS %-11s %12llu  %.4f%%\n",
                        nm[i], g_st_exit_all[i], 100.0 * (double)g_st_exit_all[i] / (double)tot);
        }
#endif
        fflush(stderr);
    }

    fclose(fp);
    return 0;
}

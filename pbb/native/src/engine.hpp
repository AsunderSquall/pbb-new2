#pragma once
// ============================================================================
// engine.hpp — C++ 引擎 (子进程, 无外部依赖)
//
// 进度显示对齐原版 pbb_all.cpp:
//   每 100 task 输出两行:
//     taskX finished,task_mex=Y,count:Z.ZZZT
//     tot=N, (八围,XP,XD),time: X.XXs, speed: X.XXXT/d,time left:XhXmXs
//
// 中文总述 (整体架构, 先读这段再看代码):
//   * 本文件是独立子进程引擎的全部逻辑 (无第三方依赖)。Python 侧用管道把这些参数
//     "key=value\n" 写进 stdin, 引擎把结果写文件、把进度/摘要写 stderr。
//     因此「参数协议」是跨语言契约: 键名改动必须同步 engine.py, 缺失键大多取默认值。
//   * 两层切分 (术语不可混用):
//       task  — 分布式级单位, 由「服务端」切总范围下发, task_id 全局递增且需按序收敛 (mex);
//       chunk — 进程内线程级单位, 固定 CHUNK_SIZE = 100 万候选, 由 producer 投喂给消费者线程。
//     同一抽象的两层, 本文件的队列只处理 chunk。
//   * 随机性来源: 每个 chunk 的随机决策只由 (g_seed, task_id) 经 splitmix64 派生,
//     与线程数、调度顺序、哪个线程执行完全无关 ⇒ 同 seed + 同 range 在任意线程数下可复现。
//   * 四个枚举模式: 1 顺序 [L,R) 进位; 2/3/4 先随机选前缀/后缀, 再在随机局部区间内展开。
//     模式 2/4 的尾 chunk 仍按满 100 万候选处理 (见 ALL_totnum 与 consume_mode24),
//     所以「不足 100 万的范围长度」不能被当成精确处理量, 且不保证全程去重。
//   * 编译期开关 PBB_UI_ENGINE: =0 普通引擎, =1 UI 引擎。打开后唯一的额外职责是
//     额外维护并发送 Top 5 排行榜 (UI_TOP_BEGIN/TAB 行/UI_TOP_END 走 stderr),
//     它不影响评分结果、门控或结果文件 —— 排行榜走内部协议, 不需要把 output_xp 改成 1。
// ============================================================================
#include "common.hpp"
#include "charset_data.hpp"
#include "charset.hpp"
#include "scoring_1124.hpp"
#include <thread>
#include <mutex>
#include <condition_variable>
#include <cstdio>
#include <vector>
#include <cstring>
#include <string>
#include <random>
#include <chrono>
#include <atomic>
#include <algorithm>
#include <unordered_map>
#include <cstdlib>
#include <limits>

#ifndef PBB_UI_ENGINE
#define PBB_UI_ENGINE 0
#endif
// 默认 0: 未显式指定即编成普通引擎。build.py 总是在命令行追加 -DPBB_UI_ENGINE={0|1},
// 这里是给手工编译 (如 g++ -fsyntax-only) 兜底。#if PBB_UI_ENGINE 分支内的代码
// 在普通引擎里完全不参与编译, 因此普通引擎没有排行榜相关的内存与分支开销。

#define MAX_QUEUE_LEN 4        // 任务队列容量
                               // 只放 4 个 chunk 是有意的背压设计: producer 领先消费者
                               // 超过 4 个 chunk 就阻塞, 避免「瞬间算出上百万个待处理任务」
                               // 把内存吃光。容量小也意味着 task_id 跨度不会过大, 利于 mex 收敛。
// ---- 两层并行术语 (第一性原理: f(i) 无依赖, 切 i 空间) ----
//   chunk: 引擎内部线程级切分单位 (本文件), 生产者按 CHUNK_SIZE 切 [rL,rR) 喂给消费者线程
//   task : 分布式级切分单位 (服务端), 服务端按更大粒度切总 range 下发给各客户端
//          客户端拿到一个 task 后, 在本进程内再被切成多个 chunk 并行
// 二者是同一抽象的两层, 不可混用。本宏仅指 chunk。
#define CHUNK_SIZE 1000000ULL  // 每个 chunk 处理的名字数 (引擎内部线程级单位)

// ---- 确定性子种子派生 (splitmix64) ----
// 由 (g_seed, task_id) 混合出每个 chunk 的专属种子。
// 第一性原理: chunk 的随机性必须由其逻辑坐标 (seed, task_id) 唯一决定,
//             与"哪个线程跑、跑的顺序、用几个线程"等执行环境细节完全无关。
//             这样同 seed + 同 range 在任意线程数下结果可复现。
// 用途: 服务端给每个 task 存固定 seed, 超时重发同一 task 时结果可复现/可去重。
static inline uint64_t splitmix64(uint64_t x){
    x+=0x9E3779B97F4A7C15ULL;
    x=(x^(x>>30))*0xBF58476D1CE4E5B9ULL;
    x=(x^(x>>27))*0x94D049BB133111EBULL;
    return x^(x>>31);
}
static inline uint64_t derive_chunk_seed(uint64_t g_seed,int task_id){
    return splitmix64(g_seed ^ splitmix64((uint64_t)task_id));
}

// ---- 任务数据结构 ----
// chunk_seed: 该 chunk 的确定性随机种子 (producer 创建时派生, 与线程无关)
// L/R: [L,R) 为候选编号区间 (R 已按 chunk 收紧); type: 枚举模式 (1/2/3/4);
// prefix_id/suffix_id: 均为「1 基」编号, 使用时先减 1 再对数量取模 (见消费端) ——
//   之所以 1 基, 是为了与配置里的序号语义及历史版本保持一致。
// task_id: 从 0 递增, 用于 mex 收敛统计与 chunk_seed 派生。
struct TaskData{uint64_t L,R;int prefix_id,suffix_id,type,task_id;uint64_t chunk_seed;};
// 环形队列 (h = 取指针, t = 放指针): h==t 表示空; 关闭后不再接受新任务, 但消费者
// 会把队列里剩余的任务全部取完才退出 (q_get 仅在 closed && 空 时返回 false)。
struct TaskQueue{TaskData d[MAX_QUEUE_LEN];int h=0,t=0;bool closed=false;std::mutex mtx;std::condition_variable cv_add,cv_get;};

// 环形队列操作 (生产者-消费者)
// q_add: 队列满且未关闭时阻塞等待消费者腾位; 已关闭则丢弃 (不再投喂新任务)。
// q_get: 队列空且未关闭时阻塞等待; 返回 false 是「结束信号」, 消费者据此退出。
// 两个函数各自在改动 h/t 后唤醒对方, 因此不存在忙等。
static void q_add(TaskQueue& q,const TaskData& d){std::unique_lock lk(q.mtx);q.cv_add.wait(lk,[&]{return q.t-q.h<MAX_QUEUE_LEN||q.closed;});if(!q.closed){q.d[q.t++%MAX_QUEUE_LEN]=d;q.cv_get.notify_one();}}
static bool q_get(TaskQueue& q,TaskData& d){std::unique_lock lk(q.mtx);q.cv_get.wait(lk,[&]{return q.h<q.t||q.closed;});if(q.closed&&q.h>=q.t)return false;d=q.d[q.h++%MAX_QUEUE_LEN];q.cv_add.notify_one();return true;}
static void q_close(TaskQueue& q){std::unique_lock lk(q.mtx);q.closed=true;q.cv_add.notify_all();q.cv_get.notify_all();}

// hex 字符串 → 字节流 (charset_bytes 解码)
// Python 侧把字符集字节流编码成十六进制以避免管道编码问题。约定: 输入长度必须为偶数;
// 出现奇数长度时最后一个半字节被忽略 (不报错)。
static std::string hex_decode(const std::string& h){std::string o;for(size_t i=0;i+1<h.length();i+=2){int hi=h[i]>='a'?h[i]-'a'+10:h[i]>='A'?h[i]-'A'+10:h[i]-'0';int lo=h[i+1]>='a'?h[i+1]-'a'+10:h[i+1]>='A'?h[i+1]-'A'+10:h[i+1]-'0';o+=(char)((hi<<4)|lo);}return o;}

// CSV 分割 (prefixes/suffixes 列表)
// 边界约定: 空串返回长度为 1 的 [""], 即「只有一个空前缀/空后缀」而不是零个 ——
//   这一条被消费端依赖 (np/ns 恒 >= 1, 不会有除零); 配置里用 "+" 显式表示空串。
static std::vector<std::string> split_csv(const std::string& s){std::vector<std::string> v;if(s.empty()){v.push_back("");return v;}std::string cur;for(char c:s){if(c==','){v.push_back(cur);cur.clear();}else cur+=c;}v.push_back(cur);return v;}

// ===== 引擎入口 =====
#if PBB_UI_ENGINE
// ---- UI 引擎专用: 各族 Top 5 排行榜 ----
// score 初值取 int 最小值作为「空槽哨兵」, 因此任何真实分数 (含 0 与负分) 都能入榜,
// 而空槽在输出时靠这个哨兵识别并 break (见 emit_ui_top)。
struct UiTopEntry { int score = std::numeric_limits<int>::min(); std::string name; };
// 插入规则 (UI 协议的一部分, 改动会改变界面观感):
//   1) 先与第 5 名比较, 不严格大于则直接丢弃;
//   2) 同名去重 —— 同名只能占一个槽 (分数不更新, 保留先到的那条);
//   3) 严格大于才前移, 因此同分保持「先到达者在前」的稳定顺序。
static inline void ui_top_insert(UiTopEntry (&board)[5], const UiTopEntry& entry){
    if(entry.score<=board[4].score)return;
    for(const auto& existing:board)if(existing.name==entry.name)return;
    int pos=4;
    while(pos>0&&entry.score>board[pos-1].score){board[pos]=board[pos-1];--pos;}
    board[pos]=entry;
}
// 名字按十六进制写出: 名字可能包含任意 UTF-8 字节甚至 NUL, 直接写文本会破坏按行解析,
//   因此协议统一用 HEX 编码, 由 Python 侧解码。
static inline void ui_write_hex(FILE* f,const std::string& s){
    static const char hex[]="0123456789ABCDEF";
    for(unsigned char c:s){fputc(hex[c>>4],f);fputc(hex[c&15],f);}
}
#endif

inline int engine_main(int argc,char**argv){
    (void)argc;(void)argv;   // 不使用命令行参数: 全部配置走 stdin 的 key=value 协议

    // 从 stdin 读取参数 (Python 通过管道传入)
    // 协议: 每行 "key=value"; 空行跳过; 值里可以含 '=' —— 只按第一个 '=' 切分
    // (charset_bytes 之类的大块数据因此不会被截断)。缺键的后果由各读取点决定。
    std::unordered_map<std::string,std::string> kv;std::string line;
    while(std::getline(std::cin,line)){if(line.empty())continue;size_t eq=line.find('=');if(eq!=std::string::npos)kv[line.substr(0,eq)]=line.substr(eq+1);}

    int debug_mode=kv.count("debug_mode")?std::stoi(kv["debug_mode"]):0;
// DBG 只在 debug_mode 打开时往 stderr 打诊断 (默认关闭, 以免污染 UI 的 stderr 协议解析)。
#define DBG(fmt,...) do{if(debug_mode){fprintf(stderr,"[engine] " fmt "\n",##__VA_ARGS__);fflush(stderr);}}while(0)
    DBG("=== START ===");

    // 读取参数
    // 说明: n_threads 是「启动线程数」上限; 运行中可下调 (见 target_threads)。
    // clen = 字符集字符数 (枚举进制基数), scl = 每个枚举单元的字节宽度;
    // 关键前提: bytes(char_set) == clen * scl 且每个字符恰好 scl 字节 —— 引擎按固定宽度
    // 切分, 不做 UTF-8 解析, 因此字节宽度与「一个 Unicode 字符」不是一对一 (足球配置用 6 字节)。
    std::string team=kv["team_name"];int n_threads=std::stoi(kv["n_threads"]);
    int scl=std::stoi(kv["scl"]),clen=std::stoi(kv["charset_len"]);
    std::string cbytes=hex_decode(kv["charset_bytes"]);
    auto prefixes=split_csv(kv["prefixes"]),suffixes=split_csv(kv["suffixes"]);
    int np=(int)prefixes.size(),ns=(int)suffixes.size();
    int mode=std::stoi(kv["mode"]),vlen=std::stoi(kv["variable_len"]);
    uint64_t rL=std::stoull(kv["range_L"]),rR=std::stoull(kv["range_R"]);
    // 逐族评分阈值 (MNAME 序): min_qp/min_qd/min_pp/min_pd/min_cqd/min_cp/min_fp/min_wc/min_fs/min_pj
    // <=0 视为该族不参与门控。
    // 注意: min_ 只决定「哪些族参与判定」, 不决定「算哪些族」—— 后者由 score_list 决定。
    // 若所有参与族都不 >0, 则门控整体失效, 所有过初筛的候选都会被写出 (见 any_gate)。
    static const char* kMinKey[M_N]={"min_qp","min_qd","min_pp","min_pd","min_cqd",
                                     "min_cp","min_fp","min_wc","min_fs","min_pj"};
    int min_[M_N];
    for(int m=0;m<M_N;m++)min_[m]=kv.count(kMinKey[m])?std::stoi(kv[kMinKey[m]]):0;
    // 逐族 ptt(potential) 阈值 (MNAME 序): ptt_cp/ptt_fp/ptt_wc/ptt_fs/ptt_pj
    //   ptt = 单个 name base 变为 63 后该族评分可达到的最大值 (score_max)。
    //   <=0 视为该族不启用 ptt; 仅 SP2 族 (cp/fp/wc/fs/pj) 有意义。
    // 用途: 普通阈值没过时, 再看「理论上限」是否够得着 ptt 阈值 —— 这是潜力筛的判据,
    //   与 min_ 是并列的两条放行通道 (任一条过即 keep)。ptt 计算代价高, 只在必要时才做。
    static const char* kPttKey[M_N]={"","","","","","ptt_cp","ptt_fp","ptt_wc","ptt_fs","ptt_pj"};
    int ptt_[M_N];
    for(int m=0;m<M_N;m++)ptt_[m]=(kPttKey[m][0]&&kv.count(kPttKey[m]))?std::stoi(kv[kPttKey[m]]):0;

    // ---- 评分计划 (score_list + score_mode) ----
    // score_list: 逗号分隔的族名, 声明要计算哪些评分 (空 ⇒ 全 10 族, 按 MNAME 序)。
    //   支持的 token: qp qd pp pd cqd cp fp wc fs pj; 别名 rh = fs (人海)。
    //   未列出的族完全不计算 (sc 恒为 0), 从而真正省下对应点积/特征构造。
    // score_mode: all = 全部算完; any = 按 list 顺序算, 遇首个 (min_>0 且 sc>=min_)
    //   即停止后续族的计算。两种模式对最终 keep 判定等价, 仅影响计算量。
    // 重要: any 会改变「实际被计算/被输出分数」的集合 (未算族 sc 保持 0, 且若 output_xp 打开
    //   会打印 0), 因此它不是「保持语义的提速手段」, 只能用于明确接受该副作用的任务。
    // 计划构建:
    //   * score_list 为空 ⇒ 全 10 族按 MNAME 序;
    //   * 重复 token 只取首次出现的位置 (order 去重), 未识别 token 打警告后忽略;
    //   * minv/pttv 总是从 min_/ptt_ 全量拷贝, 与 comp 无关 (未 comp 的族值不参与计算)。
    ScorePlan plan={};plan.norder=0;
    {
        static const struct{const char*tok;int m;}kScoreTok[]={
            {"qp",M_QP},{"qd",M_QD},{"pp",M_PP},{"pd",M_PD},{"cqd",M_CQD},
            {"cp",M_CP},{"fp",M_FP},{"wc",M_WC},{"fs",M_FS},{"rh",M_FS},{"pj",M_PJ}};
        for(int m=0;m<M_N;m++)plan.comp[m]=false;
        std::string sl=kv.count("score_list")?kv["score_list"]:"";
        if(!sl.empty()){
            for(auto&tk:split_csv(sl)){
                size_t a=tk.find_first_not_of(" \t");
                if(a==std::string::npos)continue;
                size_t b=tk.find_last_not_of(" \t");
                std::string t=tk.substr(a,b-a+1);
                bool hit=false;
                for(auto&e:kScoreTok)if(t==e.tok){hit=true;
                    if(!plan.comp[e.m]){plan.comp[e.m]=true;plan.order[plan.norder++]=e.m;}break;}
                if(!hit)fprintf(stderr,"[engine] WARN: unknown score token '%s' ignored\n",t.c_str());
            }
        }
        if(plan.norder==0)for(int m=0;m<M_N;m++){plan.comp[m]=true;plan.order[plan.norder++]=m;}
        for(int m=0;m<M_N;m++)plan.minv[m]=min_[m];
        for(int m=0;m<M_N;m++)plan.pttv[m]=ptt_[m];
        std::string sm=kv.count("score_mode")?kv["score_mode"]:"all";
        plan.any_mode=(sm=="any");
    }
    int collect_mode=kv.count("collect_mode")?std::stoi(kv["collect_mode"]):0;  // 0=否, 1=硬编码阈值, 2=自定义阈值
    // collect_mode=2: 自定义阈值 (对齐原版 special_thresholds)
    // 收集语义独立于普通评分门控: 即使名字没被普通阈值放行, 只要满足 blue 判据也会进 out/blue.txt,
    //   因此特殊号收集不能当作「普通结果文件的子集」。mode 1 使用内置常数, mode 2 用这四个 kv。
    int c_8v=0,c_7v=0,c_hl=0,c_hp=0;
    if(collect_mode==2){
        c_8v=std::stoi(kv["c_eight_v_min"]);c_7v=std::stoi(kv["c_seven_v_min"]);
        c_hl=std::stoi(kv["c_hl_min"]);c_hp=std::stoi(kv["c_hp398_min"]);
    }
    int output_xp=kv.count("output_xp")?std::stoi(kv["output_xp"]):1;
    int output_log=kv.count("output_log")?std::stoi(kv["output_log"]):1;
    int output_speed=kv.count("output_speed")?std::stoi(kv["output_speed"]):1;
#if PBB_UI_ENGINE
    // ui_mode 只有 UI 引擎才存在 (普通引擎编译时本行不存在, 因而也没有任何 UI 分支开销)。
    // 仅当 Python 显式传 ui_mode=1 才输出排行榜: 这样同一个 UI 二进制也能用于纯 CLI 任务。
    const bool ui_mode=kv.count("ui_mode")&&kv["ui_mode"]=="1";
#endif

    // Per-prefix ranges (可选: 给每个前缀分配独立的枚举区间)
    // prefix_range_L / prefix_range_R 是 CSV 字符串, 与 prefixes 顺序一一对应
    // 不存在时回退到全局 range_L / range_R
    // 边界: 两个 CSV 取较短的长度配对; 全空则 has_prefix_ranges 为 false (回退全局区间)。
    // 该特性与随机模式 (2/3) 组合时语义受限, docs/AUTOTUNE.md 明确说明自动测试不支持它。
    std::vector<uint64_t> prefix_L, prefix_R;
    bool has_prefix_ranges = false;
    if(kv.count("prefix_range_L") && kv.count("prefix_range_R")){
        auto parts_L = split_csv(kv["prefix_range_L"]);
        auto parts_R = split_csv(kv["prefix_range_R"]);
        for(size_t i=0;i<parts_L.size()&&i<parts_R.size();i++){
            prefix_L.push_back(std::stoull(parts_L[i]));
            prefix_R.push_back(std::stoull(parts_R[i]));
        }
        has_prefix_ranges = !prefix_L.empty();
    }

    // 打开输出文件
    // 结果文件路径固定为 out/<result_file>, 以追加方式打开 (因此 resume/续跑能续写同一文件)。
    // 打不开即返回 1: 宁可失败也不静默丢结果。
    // 日志/速度三条流的选择 (由 output_log/output_speed 决定): 打开时写 stderr, 关闭时写
    //   out/task_log.txt / out/speed_log.txt。写 stderr 是为了让 Python 侧实时读取进度。
    // blue.txt 仅在 collect_mode>=1 时打开。
    std::string out_path="out/"+kv["result_file"];
    FILE*fp=fopen(out_path.c_str(),"a");if(!fp){fprintf(stderr,"[engine] ERROR: cannot open %s\n",out_path.c_str());return 1;}
    FILE*fp_blue=nullptr;if(collect_mode>=1)fp_blue=fopen("out/blue.txt","a");  // mode 1/2 均收集
    FILE*flog=output_log?stderr:fopen("out/task_log.txt","a");
    FILE*fspeed=output_speed?stderr:fopen("out/speed_log.txt","a");
    fprintf(flog,"[engine] SIMD: %s\n",PBB_SIMD_NAME);fflush(flog);
    DBG("files opened, n_threads=%d mode=%d range=[%llu,%llu)",
        n_threads,mode,(unsigned long long)rL,(unsigned long long)rR);

    // 初始化 Name 状态机 (队伍名 KSA)
    // 只做一次: 队伍名决定 val_base, 之后被所有线程只读共享 (name_init 仅作模板)。
    // max_sc[] 与 max_sum 是「全局最高分」统计, 由 out_mtx 保护并在 SUMMARY 行汇报。
    Name name_init;name_init.load_team(team.c_str());
    TaskQueue q;std::mutex out_mtx;std::atomic<int>tasks_done{0},total_found{0};
    int max_sum=0;int max_sc[M_N]={0};      // 全局最大值 (out_mtx 保护): 八围最高 + 各族最高分
#if PBB_UI_ENGINE
    UiTopEntry ui_top[M_N][5];
    // Keep UI events independent of result-file score output and log settings.
    // 中文说明: 排行榜输出只受 ui_mode 控制, 与 output_xp / output_log / output_speed 无关,
    //   否则用户关掉分数输出就会连带丢掉排行榜。整块输出用 BEGIN/END 包裹以便 Python 定界。
    auto emit_ui_top=[&]{
        if(!ui_mode)return;
        static const char* kScoreName[M_N]={"qp","qd","pp","pd","cqd","cp","fp","wc","fs","pj"};
        fprintf(stderr,"UI_TOP_BEGIN\n");
        for(int oi=0;oi<plan.norder;oi++){
            int m=plan.order[oi];
            for(const auto& entry:ui_top[m]){
                if(entry.score==std::numeric_limits<int>::min())break;
                fprintf(stderr,"UI_TOP\t%s\t%d\t",kScoreName[m],entry.score);
                ui_write_hex(stderr,entry.name);fputc('\n',stderr);
            }
        }
        fprintf(stderr,"UI_TOP_END\n");fflush(stderr);
    };
#endif
    int mex_cur=0;                          // 最小未完成 task_id
    std::vector<bool> mex_vis;              // task_id 完成标记
    unsigned long long ALL_totnum;  // 总名字数 (用于 time left + SUMMARY speed)
    // 计数口径 (直接影响进度百分比与 speed 的可比性, 不可随意改):
    //   * 有 per-prefix ranges: mode 1 按各前缀区间长度之和; 随机模式按「向上取整到整 chunk」再乘
    //     CHUNK_SIZE —— 因为尾 chunk 依然会跑满 100 万候选。
    //   * 无 per-prefix ranges: mode 1 为 (rR-rL)*np (每个前缀都跑完整区间);
    //     mode 2/4 与 mode 3 为整 chunk 数 × CHUNK_SIZE; mode 2/4 还要再按整 chunk 上取整。
    //   因此 ALL_totnum 是「实际会处理的候选数」, 而 SUMMARY 的 speed 由它导出 (纯算力吞吐)。
    if(has_prefix_ranges){
        // 每个前缀有自己的区间, 分别求和
        ALL_totnum = 0;
        if(mode==1){
            for(int j=0;j<np;j++) ALL_totnum += prefix_R[j] - prefix_L[j];
        } else {
            uint64_t total_chunks = 0;
            for(int j=0;j<np;j++) total_chunks += (prefix_R[j] - prefix_L[j] + CHUNK_SIZE - 1) / CHUNK_SIZE;
            ALL_totnum = total_chunks * CHUNK_SIZE;
        }
    } else {
        ALL_totnum = (rR-rL)*np;
        if(mode>=2) ALL_totnum = rR - rL;
        if(mode==2||mode==4) ALL_totnum = ((rR - rL + CHUNK_SIZE - 1) / CHUNK_SIZE) * CHUNK_SIZE;
    }
    auto t_start=std::chrono::steady_clock::now();
    // cur_speed: 定期速度采样 (每 1000 task ≈ 1B 名字), 对齐原版 pbb_all.cpp
    auto t_cur_last = t_start;
    int td_cur_last = 0;
    // ---- 随机种子 (A1: 种子驱动随机 mode, 为分布式可复现性预留) ----
    //   seed 缺失 / 为空 / 为 "-1": 用时间熵 (单机默认, 每次结果不同, 行为不变)
    //   seed 为具体数值        : 确定性种子 (分布式: 服务端给每个 task 存固定 seed,
    //                            超时重发同 seed → 结果可复现/可去重)
    // 问题3修复 (2026-06-01): 每个 chunk 的随机性派生自 (g_seed, task_id),
    //   与线程数/调度顺序完全无关。同 seed + 同 range → 任意线程数下结果可复现。
    uint64_t g_seed;
    {
        auto it=kv.find("seed");
        if(it!=kv.end() && !it->second.empty() && it->second!="-1")
            g_seed=std::stoull(it->second);
        else
            g_seed=(uint64_t)std::chrono::steady_clock::now().time_since_epoch().count();
    }

    // mode=2/3/4 预处理
    // varlen_task: 在随机模式下「用于顺序展开的位数」, 取满足 clen^w >= CHUNK_SIZE 的最小 w
    //   (再被配置的 vlen 封顶)。random_range_max = clen^varlen_task, 是随机区间的取模上界。
    // 目的: 让随机模式每个 chunk 也能顺序走满 100 万候选, 而不是逐个随机生成。
    int varlen_task=vlen;uint64_t random_range_max=1;
    if(mode>=2){varlen_task=0;uint64_t x=1;while(x<CHUNK_SIZE){varlen_task++;x*=clen;}if(varlen_task>vlen)varlen_task=vlen;random_range_max=x;}

    // 预分配 mex_vis (预计 task 数, 留 20% 余量)
    // mode 1 (无 per-prefix ranges): 每个 prefix 跑完整区间 → 总 task = range_chunks × np
    // mode 2/3/4 (无 per-prefix ranges): producer 只按 interval 切 chunk → 总 task = range_chunks
    // 有 per-prefix ranges: 每个 prefix 在自己区间内切 chunk → 总 task = sum(各 prefix 的 chunk 数)
    uint64_t est_tasks_u;
    if(has_prefix_ranges){
        est_tasks_u = 0;
        for(int j=0;j<np;j++) est_tasks_u += (prefix_R[j] - prefix_L[j] + CHUNK_SIZE - 1) / CHUNK_SIZE;
    } else {
        uint64_t range_chunks = (rR - rL + CHUNK_SIZE - 1) / CHUNK_SIZE;
        est_tasks_u = (mode == 1 ? range_chunks * (uint64_t)np : range_chunks);
    }
    // 安全上限: 10M 条已覆盖任何实际运行场景, 防止超大 range 或 uint64 溢出 (mex_vis 支持动态扩容)
    // 注意这里只是「预分配规模」的上限; 真正的 task_id 仍可超过它, 由消费端的 resize 兜底。
    // 预留 20% + 10 的余量避免频繁扩容, 但容量不是正确性依赖。
    if (est_tasks_u > 10000000ULL) est_tasks_u = 10000000ULL;
    int est_tasks = (int)(est_tasks_u * 1.2) + 10;
    mex_vis.resize(est_tasks,false);

    // ---- task_id 序号生成器 ----
    int task_id_counter=0;

    // ---- Producer: 生成任务 ----
    // 每个 chunk 的随机决策 (prefix/suffix 选择) 由其 chunk_seed 派生,
    // 不再共享 rng_global, 确保与执行顺序/线程数无关。
    // 有 prefix_ranges: 每个前缀在自己的 [prefix_L[j], prefix_R[j]) 区间内生成 chunk.
    // mode 1: 前缀固定 (j), suffix 按 index 轮转
    // mode 2/3/4: 前缀固定 (j, 由 per-prefix range 决定), suffix 随机 (mode 4 配对)
    // mode 2/3/4: 前缀固定 (j, 由 per-prefix range 决定), suffix 随机 (mode 4 配对)
    // 关键差异:
    //   * mode 1 是唯一「每个前缀都跑完整区间」的模式, 且 suffix 按前缀序号轮转 (j%ns),
    //     保证前后缀组合被系统性地遍历而不是随机抽样;
    //   * mode 4 = mode 2 的编码方式, 但 suffix_id = prefix_id (配对), 因此后缀不随机;
    //   * mode 2/3 用 chunk_seed 重建的局部 prng 选择 prefix/suffix —— 注意这里同时用了
    //     两次/三次 prng() 抽值, 抽取次数与顺序属于「随机结果的语义」, 不能重排或增减。
    auto prod=[&]{
        if(has_prefix_ranges){
            for(int j=0;j<np;j++){
                uint64_t pl=prefix_L[j], pr=prefix_R[j];
                for(uint64_t i=pl;i<pr;i+=CHUNK_SIZE){
                    uint64_t tr=i+CHUNK_SIZE;if(tr>pr)tr=pr;
                    TaskData t;t.L=i;t.R=tr;t.type=mode;
                    t.prefix_id=j+1;
                    t.task_id=task_id_counter++;t.chunk_seed=derive_chunk_seed(g_seed,t.task_id);
                    if(mode==1) t.suffix_id=(j%ns)+1;
                    else if(mode==4) t.suffix_id=t.prefix_id;
                    else { std::mt19937_64 prng2(t.chunk_seed); t.suffix_id=prng2()%ns+1; }
                    q_add(q,t);
                }
            }
        } else {
            if(mode==1)for(int j=0;j<np;j++)for(uint64_t i=rL;i<rR;i+=CHUNK_SIZE){
                uint64_t tr=i+CHUNK_SIZE;if(tr>rR)tr=rR;
                TaskData t;t.L=i;t.R=tr;t.type=1;t.prefix_id=j+1;t.suffix_id=(j%ns)+1;
                t.task_id=task_id_counter++;t.chunk_seed=derive_chunk_seed(g_seed,t.task_id);q_add(q,t);}
            else for(uint64_t i=rL;i<rR;i+=CHUNK_SIZE){
                uint64_t tr=i+CHUNK_SIZE;if(tr>rR)tr=rR;
                TaskData t;t.L=i;t.R=tr;t.type=mode;
                t.task_id=task_id_counter++;t.chunk_seed=derive_chunk_seed(g_seed,t.task_id);
                std::mt19937_64 prng(t.chunk_seed);
                t.prefix_id=prng()%np+1;t.suffix_id=mode==4?t.prefix_id:prng()%ns+1;
                q_add(q,t);}
        }
        q_close(q);
    };

    // ---- Consumer: 处理任务 (编码 + RC4 + 评分) ----
    // rng 改为 per-chunk 重建 (用 t.chunk_seed), 不再按线程 id 初始化、跨 chunk 连续使用。
    // 重构 (2026-06-02): 四种模式编码逻辑抽出为独立 helper — 消除分支交织与评分/blue 重复代码。
    //   process_one()   — 评分 + blue 判定 (四种模式共享)
    //   consume_seq()   — 顺序进位编码 (mode 1/2/4 共用)
    //   consume_rand()  — 随机逐位编码 (mode 3 专用)
    //   consume_mode1() — mode 1 进位检测 + 顺序编码
    //   consume_mode24()— mode 2/4 随机前缀/LR + 顺序编码
    //   consume_mode3() — mode 3 随机前缀 + 随机逐位编码

    // 动态线程数: worker id >= target_threads 时该线程睡眠, 不取任务
    // 只允许下调 (reader_thread 校验 1 <= val <= n_threads): 线程一旦创建就不会销毁,
    //   超额线程在每轮循环开头 sleep 500ms 后重新检查, 因此「调低再调高」可以恢复。
    std::atomic<int> target_threads{n_threads};

    // ---- 消费者主体 ----
    // BATCH_WIDTH=4 是评分内核的硬约束: load_name_batch4 / calc_skills_batch4 /
    //   score_full_loaded_batch4 都按 4 路特化, 改成别的值需要新增特化实现。
    // names[lane] 只从这里复制一次 val_base (之后每个候选复用同一批对象, 避免反复构造)。
    // local_* 是「本 chunk 的局部累计量」, chunk 结束时才在 out_mtx 下并入全局,
    //   这样热路径上完全不加锁 (结果文件写入是唯一的加锁点)。
    auto cons=[&](int cid){(void)cid;constexpr int BATCH_WIDTH=4;Name names[BATCH_WIDTH];
        for(int lane=0;lane<BATCH_WIDTH;lane++)
            memcpy(names[lane].val_base,name_init.val_base,sizeof(names[lane].val_base));
        Name& name=names[0];TaskData t;
        int local_found=0,local_max_sum=0;int local_max_sc[M_N]={0};
#if PBB_UI_ENGINE
        UiTopEntry local_top[M_N][5];
#endif
        bool any_gate=false;  // min_/ptt_ 为常量, 每线程只算一次
        for(int m=0;m<M_N;m++)if(plan.comp[m]&&(min_[m]>0||ptt_[m]>0)){any_gate=true;break;}
        const char* cb=cbytes.data();  // 局部缓存指针
        // 编码宏: 全内联, 编译器在 scl==4 分支将 memcpy(...,4) 优化为单条 mov
        // PBB_ENCODING_COPY=1 额外提供 scl==6 的专用分支 (4+2 两次 memcpy, 即足球配置的六字节单元),
        //   让编译器把整单元拷贝识别成两条定长 mov 而不是可变的 memcpy 调用。
        // 语义要求: 无论哪档, ENC 都必须把第 ci 个字符的 scl 个字节原样搬进 dst —— 它是纯拷贝优化,
        //   不改变候选生成结果 (即 PBB_ENCODING_COPY 只影响速度)。
#if defined(PBB_ENCODING_COPY) && PBB_ENCODING_COPY
        #define ENC(dst,ci) do{const char*_s=cb+(ci)*scl;if(scl==4)memcpy((dst),_s,4);else if(scl==1)*(dst)=*_s;else if(scl==2)memcpy((dst),_s,2);else if(scl==3)memcpy((dst),_s,3);else if(scl==6){memcpy((dst),_s,4);memcpy((dst)+4,_s+4,2);}else memcpy((dst),_s,scl);}while(0)
#else
        #define ENC(dst,ci) do{const char*_s=cb+(ci)*scl;if(scl==4)memcpy((dst),_s,4);else if(scl==1)*(dst)=*_s;else if(scl==2)memcpy((dst),_s,2);else if(scl==3)memcpy((dst),_s,3);else memcpy((dst),_s,scl);}while(0)
#endif

        // ---- 公共 helper: 评分 + blue 判定 (四种模式共享, 消除重复) ----
        // 逐族阈值门控: 任一参与族 (min_[m]>0) 达标即输出; 无参与族时保留全部过初筛候选。
        // 前置: r.flag 已由 score_full 置位 (初筛/门控淘汰的名字 flag=false, 直接返回, 连
        //   最高分统计都不更新)。
        // 注意顺序: 先更新 max 统计与 UI 榜, 再做 keep 判定 —— 因此 max 统计覆盖「过了初筛
        //   但未达输出阈值」的名字, 这正是 SUMMARY 里 max_sum/max_sc 的口径 (引擎是唯一真相源,
        //   Python 不再从结果文件重算 max)。
        auto process_one=[&](const char* buf,int nlen,const ScoreResult& r){
            if(!r.flag)return;
            const int* sc=r.sc;
            if(r.sum>local_max_sum)local_max_sum=r.sum;
            for(int oi=0;oi<plan.norder;oi++){const int m=plan.order[oi];
                if(sc[m]>local_max_sc[m])local_max_sc[m]=sc[m];
#if PBB_UI_ENGINE
                if(ui_mode&&sc[m]>local_top[m][4].score){
                    UiTopEntry entry;entry.score=sc[m];entry.name.assign(buf,nlen);
                    entry.name+='@';entry.name+=team;
                    ui_top_insert(local_top[m],entry);
                }
#endif
            }
            bool keep=true;
            // any_gate=false (没有任何参与族) 时 keep 恒为 true: 此时引擎退化成「只做初筛,
            //   所有过筛名字全部记录」, 用于统计与调试。
            if(any_gate){keep=false;
                for(int m=0;m<M_N;m++){if(!plan.comp[m])continue;
                    if(min_[m]>0&&sc[m]>=min_[m]){keep=true;break;}          // 普通阈值已过
                    if(ptt_[m]>0&&r.sc_max[m]>=ptt_[m]){keep=true;break;}    // 普通未过 → 查 ptt(score_max)
                }}
            if(keep){std::lock_guard lk(out_mtx);local_found++;
                // 输出格式: 名字@队伍名 [分数...]。分数列按 MNAME 序取「被计算 (plan.comp)」
                //   的子集 —— 不是固定的 XP/XD 两列。output_xp=0 时只写名字与队伍。
                if(output_xp){fprintf(fp,"%.*s@%s",nlen,buf,team.c_str());
                    for(int m=0;m<M_N;m++)if(plan.comp[m])fprintf(fp," %d",sc[m]);
                    fputc('\n',fp);}
                else fprintf(fp,"%.*s@%s\n",nlen,buf,team.c_str());
                fflush(fp);}
            // props[7] 已是原始 HP (154..406, 无 +36 偏移); props[0..6] 含 +36 偏移。
            // 对齐原版 pbb_old.cpp 的 blue 判据 (sum*3-prop[7] / sum-prop[7]/3 / prop[7]==398)。
            // 单位说明: sum = V + 252 为八围总分; hp=398 是特殊号判据里的固定哨兵值。
            // collect_mode==1 用内置常数 (777/2000/741/93), ==2 用配置阈值 (c_8v/c_7v/c_hp/c_hl)。
            // 收集走独立锁区, 与结果文件写入分开, 二者互不影响。
            if(collect_mode>=1&&fp_blue){int sum=r.sum,hp=r.props[7],hl=*std::min_element(r.props,r.props+7);bool blue=false;
                if(collect_mode==1){if(sum>=777||(sum*3-hp)>=2000||(hp==398&&sum>=741)||(hl>=93))blue=true;}
                else{if(sum>=c_8v||(sum-hp/3)>=c_7v||(hp==398&&sum>=c_hp)||(hl>=c_hl))blue=true;}
                if(blue){std::lock_guard lk(out_mtx);fprintf(fp_blue,"%.*s@%s\n",nlen,buf,team.c_str());fflush(fp_blue);}}
        };

        // ---- 编码 helper: 顺序进位 (mode 1/2/4 共用) ----
        // 从低位到高位写 digits: pos 从 epre+evar*scl-scl 递减到 epre, 每次取 i%clen 并 i/=clen。
        // 单候选模式 (非批量), 每候选都重新做完整 KSA; 批量路径见 consume_mode1/24。
        auto consume_seq=[&](char* buf,int nlen,Name& name,int epre,int evar,uint64_t L,uint64_t R){
            for(uint64_t i=L;i<R;i++){uint64_t now=i;
                for(int pos=epre+evar*scl-scl;pos>=epre;pos-=scl){int ci=now%clen;ENC(buf+pos,ci);now/=clen;}
                process_one(buf,nlen,score_full(buf,nlen,name,&plan));}
        };

        // ---- 编码 helper: 随机逐位 (mode 3 专用) ----
        // 每个位置独立 rng()%clen, 因此允许重复 (mode 3 明确不保证去重, 也不能用于 resume)。
        auto consume_rand=[&](char* buf,int nlen,Name& name,int epre,int evar,uint64_t L,uint64_t R,std::mt19937_64& rng){
            for(uint64_t i=L;i<R;i++){
                for(int pos=epre+evar*scl-scl;pos>=epre;pos-=scl){int ci=rng()%clen;ENC(buf+pos,ci);}
                process_one(buf,nlen,score_full(buf,nlen,name,&plan));}
        };

        // ---- mode 1: 固定批宽候选交错 KSA，持久 digits/carry 生成连续候选 ----
        // SIMD 档 (PBB_HAS_SIMD) 才有批量路径; 非 SIMD 档退回 consume_seq 逐候选。
        // 批量路径的逻辑:
        //   1. 用「取模法」求 up = 最高的共同 clen 进制位 (L 与 R-1 从该位起才不同);
        //      epre = plen + up*scl 即所有候选共享的最长字节前缀, 交给 load_prefix 预处理一次;
        //   2. 把 lane0 的前缀状态 (i_pre/j_pre/s_pre/NAMELEN/val_base2/saved_val) 整体复制给
        //      lane1..3 —— 这是 load_name_batch4 的前提 (四 lane 必须同状态);
        //   3. digits[] 只维护变化的低位, advance_buf() 做进位并同步写回 buf (不需要重算整串);
        //   4. 每次取 4 个候选拷进 batch_buf 再一起 KSA + 评分; 不足 4 个的尾批由标量循环收尾
        //      (尾批必须逐候选处理, 因为批量路径会一次消费 4 个候选的密钥状态)。
        // buf 长度约束: 名字缓冲固定 char buf[512], 因此 plen + vlen*scl + slen 必须 < 512。
        auto consume_mode1=[&](char* buf,int nlen,Name& name,int plen,int vlen,uint64_t L,uint64_t R){
#if PBB_HAS_SIMD
            // 取模法计算 up: 找到 L 和 R-1 最高的共同 clen 进制位
            int up=0;{uint64_t dl=L,dr=R-1,div=1;for(int d=0;d<vlen;d++)div*=clen;
                while(up<vlen){div/=clen;if(dl/div!=dr/div)break;dl%=div;dr%=div;up++;}}
            int epre=plen+up*scl;
            // 初始化第一个候选的 digits (仅需要 advance_buf 使用)
            int digits[16];uint64_t now=L;
            for(int d=vlen-1;d>=0;d--){digits[d]=(int)(now%clen);now/=clen;ENC(buf+plen+d*scl,digits[d]);}
            names[0].PRELEN=epre;names[0].load_prefix(buf,nlen);
            for(int lane=1;lane<BATCH_WIDTH;lane++){
                names[lane].PRELEN=names[0].PRELEN;names[lane].i_pre=names[0].i_pre;
                names[lane].j_pre=names[0].j_pre;names[lane].s_pre=names[0].s_pre;
                names[lane].NAMELEN=names[0].NAMELEN;
                memcpy(names[lane].val_base2,names[0].val_base2,sizeof(names[lane].val_base2));
                memcpy(names[lane].saved_val,names[0].saved_val,sizeof(names[lane].saved_val));
                names[lane].prefix_loaded=true;
            }

            auto advance_buf=[&]{
                for(int d=vlen-1;d>=up;d--){
                    if(++digits[d]<clen){ENC(buf+plen+d*scl,digits[d]);break;}
                    digits[d]=0;ENC(buf+plen+d*scl,0);
                }
            };
            char batch_buf[BATCH_WIDTH][512];
            const char* batch_names[BATCH_WIDTH];
            for(int lane=0;lane<BATCH_WIDTH;lane++)batch_names[lane]=batch_buf[lane];
            // [v2_drop_batchup] batch_up/advance_ksa_n 实测恒不触发 (adv=0),
            // 且 batch 循环内 names[lane] 的 i_pre/j_pre/s_pre/saved_val 从不被修改,
            // 故移除「取模法 batch_up 检测」+「advance_ksa_n 分支」+「每 batch 状态重置」。
            // 中文说明: 这是历史优化 (v2 实验版本) 的删除记录, 保留注释是为了说明「为什么这里
            //   不需要每个 batch 重新装载前缀状态」—— 一旦有人在循环内改动 names[lane] 的前缀字段,
            //   这个前提就被破坏, 必须把被删掉的逻辑加回来。
            uint64_t i=L;
            for(;R-i>=BATCH_WIDTH;i+=BATCH_WIDTH){
                for(int lane=0;lane<BATCH_WIDTH;lane++){
#if defined(PBB_ENCODING_COPY) && PBB_ENCODING_COPY
                    if(nlen+1<=64)memcpy(batch_buf[lane],buf,64);
                    else memcpy(batch_buf[lane],buf,nlen+1);
#else
                    memcpy(batch_buf[lane],buf,nlen+1);
#endif
                    advance_buf();
                }
                Name::load_name_batch4(names,batch_names,nlen);
                ScoreResult batch_results[BATCH_WIDTH];
                score_full_loaded_batch4(batch_names,nlen,names,batch_results,&plan);
                for(int lane=0;lane<BATCH_WIDTH;lane++)
                    process_one(batch_buf[lane],nlen,batch_results[lane]);
            }
            for(;i<R;i++){
                process_one(buf,nlen,score_full(buf,nlen,name,&plan));
                if(i+1<R)advance_buf();
            }
#else
            uint8_t dl[16],dr[16];uint64_t now;
            now=L;for(int d=vlen-1;d>=0;d--){dl[d]=now%clen;now/=clen;}
            now=R-1;for(int d=vlen-1;d>=0;d--){dr[d]=now%clen;now/=clen;}
            int up=0;while(up<vlen&&dl[up]==dr[up])up++;
            now=L;for(int d=vlen-1;d>=0;d--){int ci=now%clen;ENC(buf+plen+d*scl,ci);now/=clen;}
            name.PRELEN=plen+up*scl;name.load_prefix(buf,nlen);
            consume_seq(buf,nlen,name,plen+up*scl,vlen-up,L,R);
#endif
        };

        // ---- mode 2/4: 随机额外字符 + 随机区间 + 顺序进位 (batch 优化版) ----
        // 结构: 高 extra = vlen-varlen_task 位由 rng 一次性随机填好 (每个 chunk 内固定不变),
        //   低位 varlen_task 位在 [L, L+CHUNK_SIZE) 上顺序进位。
        // 关键语义 (与 README 的说明一致, 不要"修正"):
        //   L = rng() % random_range_max, R = L + CHUNK_SIZE —— R 允许超过 random_range_max,
        //   不同 chunk 的区间也可能重叠, 所以模式 2/4 不保证全程去重, 处理量也不是精确值。
        //   这样设计的原因是要让每个 chunk 恒为 100 万候选 (吞吐稳定、进度可估)。
        // prefix/suffix 已在 producer 中按 chunk_seed 选好, 这里不再抽随机数 (抽取顺序敏感)。
        auto consume_mode24=[&](char* buf,int nlen,Name& name,int plen,uint64_t& L,uint64_t& R,std::mt19937_64& rng){
            int extra=vlen-varlen_task;if(extra>0)for(int pos=plen;pos<plen+extra*scl;pos+=scl){int ci=rng()%clen;ENC(buf+pos,ci);}
            int epre=plen+extra*scl,evar=varlen_task;
            L=rng()%random_range_max;R=L+CHUNK_SIZE;
#if PBB_HAS_SIMD
            // 取模法计算 up: 找到 L 和 R-1 最高的共同 clen 进制位
            int up=0;{uint64_t dl=L,dr=R-1,div=1;for(int d=0;d<evar;d++)div*=clen;
                while(up<evar){div/=clen;if(dl/div!=dr/div)break;dl%=div;dr%=div;up++;}}
            int epre_b=epre+up*scl;
            // 初始化第一个候选的 digits
            int digits[16];uint64_t now=L;
            for(int d=evar-1;d>=0;d--){digits[d]=(int)(now%clen);now/=clen;ENC(buf+epre+d*scl,digits[d]);}
            names[0].PRELEN=epre_b;names[0].load_prefix(buf,nlen);
            for(int lane=1;lane<BATCH_WIDTH;lane++){
                names[lane].PRELEN=names[0].PRELEN;names[lane].i_pre=names[0].i_pre;
                names[lane].j_pre=names[0].j_pre;names[lane].s_pre=names[0].s_pre;
                names[lane].NAMELEN=names[0].NAMELEN;
                memcpy(names[lane].val_base2,names[0].val_base2,sizeof(names[lane].val_base2));
                memcpy(names[lane].saved_val,names[0].saved_val,sizeof(names[lane].saved_val));
                names[lane].prefix_loaded=true;
            }
            auto advance_buf=[&]{
                for(int d=evar-1;d>=up;d--){
                    if(++digits[d]<clen){ENC(buf+epre+d*scl,digits[d]);break;}
                    digits[d]=0;ENC(buf+epre+d*scl,0);
                }
            };
            char batch_buf[BATCH_WIDTH][512];
            const char* batch_names[BATCH_WIDTH];
            for(int lane=0;lane<BATCH_WIDTH;lane++)batch_names[lane]=batch_buf[lane];
            // [v2_drop_batchup] 同 mode1: 移除 batch_up 检测 + advance_ksa_n + 每 batch 状态重置。
            uint64_t i=L;
            for(;R-i>=BATCH_WIDTH;i+=BATCH_WIDTH){
                for(int lane=0;lane<BATCH_WIDTH;lane++){
#if defined(PBB_ENCODING_COPY) && PBB_ENCODING_COPY
                    if(nlen+1<=64)memcpy(batch_buf[lane],buf,64);
                    else memcpy(batch_buf[lane],buf,nlen+1);
#else
                    memcpy(batch_buf[lane],buf,nlen+1);
#endif
                    advance_buf();
                }
                Name::load_name_batch4(names,batch_names,nlen);
                ScoreResult batch_results[BATCH_WIDTH];
                score_full_loaded_batch4(batch_names,nlen,names,batch_results,&plan);
                for(int lane=0;lane<BATCH_WIDTH;lane++)
                    process_one(batch_buf[lane],nlen,batch_results[lane]);
            }
            for(;i<R;i++){
                process_one(buf,nlen,score_full(buf,nlen,name,&plan));
                if(i+1<R)advance_buf();
            }
#else
            uint8_t dl[16],dr[16];uint64_t now2;
            now2=L;for(int d=evar-1;d>=0;d--){dl[d]=now2%clen;now2/=clen;}
            now2=R-1;for(int d=evar-1;d>=0;d--){dr[d]=now2%clen;now2/=clen;}
            int up=0;while(up<evar&&dl[up]==dr[up])up++;
            now2=L;for(int d=evar-1;d>=0;d--){int ci=now2%clen;ENC(buf+epre+d*scl,ci);now2/=clen;}
            name.PRELEN=epre+up*scl;name.load_prefix(buf,nlen);
            consume_seq(buf,nlen,name,epre+up*scl,evar-up,L,R);
#endif
        };

        // ---- mode 3: 随机额外字符 + 随机逐位编码 ----
        // 与 mode 2/4 的差别: 低位不是顺序进位, 而是每个位置独立 rng()%clen (允许重复);
        // 因此 mode 3 没有批量 KSA 优化, 只能逐候选走 consume_rand。
        // 同样地, 每个消耗一次 rng() 的调用顺序都属于随机语义, 不可重排。
        auto consume_mode3=[&](char* buf,int nlen,Name& name,int plen,uint64_t L,uint64_t R,std::mt19937_64& rng){
            int extra=vlen-varlen_task;if(extra>0)for(int pos=plen;pos<plen+extra*scl;pos+=scl){int ci=rng()%clen;ENC(buf+pos,ci);}
            int epre=plen+extra*scl,evar=varlen_task;name.PRELEN=epre;name.load_prefix(buf,nlen);
            consume_rand(buf,nlen,name,epre,evar,L,R,rng);
        };

        while(true){
            // 动态线程数: 如果当前线程 id >= target_threads, 睡眠等待
            // 注意: 这里 continue 会跳过 q_get, 所以被"停用"的线程不会占用队列里的 chunk;
            //   醒来后若 target_threads 已恢复, 它会正常继续取任务。
            if(cid >= target_threads.load(std::memory_order_relaxed)){
                std::this_thread::sleep_for(std::chrono::milliseconds(500));
                continue;
            }
            if(!q_get(q,t)) break;
            // 每个 chunk 用自己的确定性种子重建 rng (与执行环境无关)
            // 重建而不是沿用上一个 chunk 的 rng: 这样同一 chunk 无论被谁、在什么时刻执行,
            //   抽到的前缀/后缀/随机位都一样, 从而线程数变化不影响结果集合。
            std::mt19937_64 rng(t.chunk_seed);
            // 构建名字缓冲区: prefix + 占位 + suffix
            // 布局: [0, plen) 前缀 | [plen, plen+vlen*scl) 枚举单元 (先清零) | 之后后缀。
            // prefix_id/suffix_id 是 1 基, 这里取模保证与列表长度对齐 (producer 已保证不越界,
            //   取模只是复用同一表达式的兜底)。
            const std::string&p=prefixes[(t.prefix_id-1)%np];const std::string&s=suffixes[(t.suffix_id-1)%ns];
            int plen=(int)p.size(),slen=(int)s.size(),nlen=plen+vlen*scl+slen;
#if defined(PBB_ENCODING_COPY) && PBB_ENCODING_COPY
            char buf[512]{};
#else
            char buf[512];
#endif
            memcpy(buf,p.data(),plen);memset(buf+plen,0,vlen*scl);
            if(slen)memcpy(buf+plen+vlen*scl,s.data(),slen);
            buf[nlen]=0;  // 修复: load_prefix/load_name 循环第一轮读 name[nlen], 与 pbb_all.cpp 全局零初始化对齐
            uint64_t L=t.L,R=t.R;

            // 模式分发: 各模式独立的编码逻辑
            if(mode==1)      consume_mode1(buf,nlen,name,plen,vlen,L,R);
            else if(mode==3) consume_mode3(buf,nlen,name,plen,L,R,rng);
            else             consume_mode24(buf,nlen,name,plen,L,R,rng);  // mode 2/4

            // ===== task 完成: 更新全局状态 + 进度显示 (对齐原版 pbb_all.cpp) =====
            // 整块在 out_mtx 下执行: 热路径不加锁, 每个 chunk 只在这里付出一次锁代价。
            // total_found/max_* 采用「局部累加 + 此处合并」的模式, 因此 SUMMARY 的数字是精确的。
            // 末尾把 local_* 清零 —— 忘记清零会导致后续 chunk 重复累加 (历史 bug 类型)。
            {
                std::lock_guard lk(out_mtx);
                total_found+=local_found;
                if(local_max_sum>max_sum)max_sum=local_max_sum;
                for(int m=0;m<M_N;m++)if(local_max_sc[m]>max_sc[m])max_sc[m]=local_max_sc[m];
#if PBB_UI_ENGINE
                if(ui_mode)for(int m=0;m<M_N;m++)for(const auto& entry:local_top[m])
                    ui_top_insert(ui_top[m],entry);
#endif
                // mex 追踪 (原版 mex_vis + mex_cur)
                // mex_cur = 最小未完成 task_id, 用于判断「已连续完成到哪个位置」。
                // 动态扩容: task_id 可能超出预估容量 (预估有 10M 上限), 这里按需 +256 扩张。
                if(t.task_id>=(int)mex_vis.size())mex_vis.resize(t.task_id+256,false);
                mex_vis[t.task_id]=true;
                while(mex_cur<(int)mex_vis.size()&&mex_vis[mex_cur])mex_cur++;
                // 进度显示: 每 100 task (对齐原版)
                int td=++tasks_done;
#if PBB_UI_ENGINE
                if(td==1)emit_ui_top();
#endif
                if(td%100==0){
                    auto now=std::chrono::steady_clock::now();
                    double sec=std::chrono::duration<double>(now-t_start).count();
                    long long done_num=1ll*td*CHUNK_SIZE;
                    long long tmlft=ALL_totnum>done_num?((ALL_totnum-done_num)*1.0/done_num)*sec:0;
                    // 原版第一行: taskX finished, task_mex=Y, count:Z.ZZZT
                    fprintf(flog,"task%d finished,task_mex=%d,count:%.6lfT\n",
                        t.task_id,mex_cur,done_num/1e12);
                    // 原版第二行: tot=N, (八围, score_list 各族最高分), time: Xs, speed: XT/d, time left:XhXmXs
                    // 括号内第一个数是八围最高分 max_sum, 之后按 plan.order 顺序列出各族最高分 ——
                    //   因此当 score_list 只含部分族时, 列数会随计划变化 (不是固定 10 列)。
                    // speed 用「已完成 task 数 × CHUNK_SIZE / 墙钟」估算, 含等待 I/O 的开销;
                    //   权威吞吐以结尾的 SUMMARY 行为准 (它用纯计算耗时)。
                    fprintf(fspeed,"tot=%d, (%d",total_found.load(),max_sum);
                    for(int oi=0;oi<plan.norder;oi++)fprintf(fspeed,",%d",max_sc[plan.order[oi]]);
                    fprintf(fspeed,"),time: %.2fs, speed: %.6fT/d,time left:%lldh%lldm%llds\n",
                        sec,td/sec*86400*CHUNK_SIZE/1e12,
                        tmlft/3600,(tmlft%3600)/60,tmlft%60);
#if PBB_UI_ENGINE
                    emit_ui_top();
#endif
                    fflush(output_log?stderr:flog);
                    fflush(output_speed?stderr:fspeed);
                    // cur_speed: 每 1000 task 输出近期速度 (≈1B 名字), 对齐原版 pbb_all.cpp
                    // 与上面的累计速度区别: 这里只用「最近 1000 个 task」的增量, 反映当前瞬时吞吐;
                    //   time left 标了 (?) 表示它按瞬时速度外推, 波动较大, 仅供观察。
                    if(td%1000==0){
                        auto t_cur=std::chrono::steady_clock::now();
                        double cur_sec=std::chrono::duration<double>(t_cur-t_cur_last).count();
                        long long cur_done=(td-td_cur_last)*CHUNK_SIZE;
                        long long cur_tmlft=ALL_totnum>done_num?((ALL_totnum-done_num)*1.0/cur_done)*cur_sec:0;
                        if(cur_sec>0)
                            fprintf(fspeed,"cur_speed:%.6fT/d,time left(?):%lldh%lldm%llds\n\n",
                                86400.0*(cur_done/1e12)/cur_sec,
                                cur_tmlft/3600,(cur_tmlft%3600)/60,cur_tmlft%60);
                        fflush(output_speed?stderr:fspeed);
                        t_cur_last=t_cur;td_cur_last=td;
                    }
                }
            }
            local_found=0;local_max_sum=0;for(int m=0;m<M_N;m++)local_max_sc[m]=0;
#if PBB_UI_ENGINE
            if(ui_mode)for(auto& board:local_top)for(auto& entry:board)entry=UiTopEntry{};
#endif
        }};

    // ---- 动态线程数: 通过控制文件 out/.threads 运行时调整 ----
    //   写入整数到 out/.threads 即可实时增减 Worker 线程 (1 ~ n_threads)
    //   excess 线程在每次 chunk 前 sleep 500ms 等待唤醒
    // 实现说明: 每秒轮询一次, 只在值变化且落在 [1, n_threads] 时生效 —— 超出范围的值被忽略
    //   (不会报错), 因此 UI 侧无需额外校验。该线程是纯 I/O (每秒一次 fopen), 对主循环无影响。
    std::thread reader_thread([&]{
        // Poll control file every second (轻量, 不影响性能)
        std::string ctl_path = "out/.threads";
        int last_val = n_threads;
        while(true){
            std::this_thread::sleep_for(std::chrono::milliseconds(1000));
            FILE* f = fopen(ctl_path.c_str(), "r");
            if(!f) continue;
            int val = -1;
            if(fscanf(f, "%d", &val) == 1 && val >= 1 && val <= n_threads && val != last_val){
                target_threads.store(val);
                fprintf(stderr, "\n[engine] threads → %d\n", val);
                fflush(stderr);
                last_val = val;
            }
            fclose(f);
        }
    });
    reader_thread.detach();  // 守护线程, 进程退出时自动清理
                             // 因为它 while(true) 永不退出, detach 是唯一安全选择:
                             //   join 会永久阻塞, 析构未 join 的 thread 会直接 terminate。

    // 启动线程
    // 一个 producer + n_threads 个 consumer; main 阻塞在 join 上直到 producer 关闭队列
    // 且所有 consumer 取空退出。任务分配是「工作窃取式的动态领取」(谁空谁取),
    // 因此不同线程的 chunk 数量不均衡是正常的, 不影响结果 (每个 chunk 自带种子)。
    DBG("creating 1 producer + %d consumers...",n_threads);
    std::thread pt(prod);std::vector<std::thread>cts;
    for(int i=0;i<n_threads;i++)cts.emplace_back(cons,i);
    DBG("all threads running");
    pt.join();for(auto&t:cts)t.join();
#if PBB_UI_ENGINE
    emit_ui_top();
#endif
    DBG("all threads joined");

    // ===== 权威摘要 (问题4/5修复, 2026-06-01) =====
    // 引擎是唯一真相源: max_sum/max_sc 追踪通过 V 值/技能检查的名字
    // (不止达标的——包含检查通过但未达输出阈值的名字), speed 是纯算力吞吐。
    // Python 直接采信此行, 不再从文件重算 max、不再用墙钟反算 speed。
    // 格式: SUMMARY max_sum=.. max_xp=.. max_xd=.. found=.. count=.. elapsed=.. speed=..
    //   max_xp/max_xd 仍保留 (兼容 engine.py), 分别取 qp / qd 两族的最高分
    //   count=已处理名字总数, speed=名字/秒 (纯计算)
    {
        auto t_end=std::chrono::steady_clock::now();
        double calc_sec=std::chrono::duration<double>(t_end-t_start).count();
        unsigned long long processed=ALL_totnum;  // 总名字数 (= (rR-rL)*np)
                                                  // 注意: 用估算量而非实际计数, 这是历史口径,
                                                  // 与进度行的 speed 保持一致 (可比性优先)。
        double speed=calc_sec>0?(double)processed/calc_sec:0.0;
        fprintf(stderr,"SUMMARY max_sum=%d max_xp=%d max_xd=%d found=%d count=%llu elapsed=%.6f speed=%.6f\n",
            max_sum,max_sc[M_QP],max_sc[M_QD],total_found.load(),processed,calc_sec,speed);
        fflush(stderr);
    }

    // 关闭文件
    // 条件关闭: flog/fspeed 在「写 stderr」模式下不是本进程打开的文件, 不能 fclose。
    fclose(fp);if(fp_blue)fclose(fp_blue);
    if(!output_log&&flog!=stderr)fclose(flog);
    if(!output_speed&&fspeed!=stderr)fclose(fspeed);
    return 0;
}

// engine_main.cpp — PBB 引擎入口
// 版本切换: 编译时通过 PBB_SRC_DIR env var 控制 -I 优先级
// 例如 PBB_SRC_DIR=v1_baseline → -I src/v1_baseline -I src
// include "engine.hpp" 会优先找到 v1_baseline 版本
//
// 中文说明 (为什么文件这么短):
//   本文件只是「薄入口」—— 真正的引擎实现在 src/engine.hpp 的 engine_main() 里, 只有把它
//   放进一个 .cpp 才会被编译成可执行文件 (header-only 便于历史版本用 -I 覆盖实现)。
//   因此: 正常构建只编译本文件 + 共享的 src/*.hpp; -I 顺序决定用哪一份引擎实现。
//   注意 src/ 下那些 v0..v5/vgen 子目录是历史实验版本, 正常编译不使用它们 (见 README)。
//   argc/argv 原样透传, 但引擎实际从 stdin 读取参数 (见 engine.hpp 的参数协议)。
#include "engine.hpp"
int main(int argc, char** argv) { return engine_main(argc, argv); }

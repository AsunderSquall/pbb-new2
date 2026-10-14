// engine_main.cpp — PBB 引擎入口
// 版本切换: 编译时通过 PBB_SRC_DIR env var 控制 -I 优先级
// 例如 PBB_SRC_DIR=v1_baseline → -I src/v1_baseline -I src
// include "engine.hpp" 会优先找到 v1_baseline 版本
#include "engine.hpp"
int main(int argc, char** argv) { return engine_main(argc, argv); }

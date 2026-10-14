# Linux CLI 自动调优

PBB_Linux 只支持 CLI 引擎，UI、监控器和 UI 引擎不可用。自动调优仅编译和比较
`pbb_engine`，不会生成 UI 产物。

在 `PBB_Linux` 目录运行：

```bash
./tune_quick.sh --speed 4 --minutes 20
./tune_full.sh --speed 4 --minutes 20
```

`--speed` 是此前完整测号速度，单位 T/day；`--minutes` 是测试预算，不包含编译
时间，超出软目标后仍会完成当前计划和最终确认。若不提供参数，脚本会交互询问。

每次运行的隔离记录位于 `source/build/autotune/日期_时间/`，包括编译命令、测试
参数、输出哈希和报告。完成测试后，选择会写入 `config/build_config.json`；随后
运行 `./rebuild.sh` 才会将选择应用到正式 CLI 引擎。使用 `--no-write-config`
可以只试跑而不修改配置。

```bash
./tune_quick.sh --speed 4 --minutes 20 --no-write-config
./tune_quick.sh --install-from source/build/autotune/日期_时间
./tune_quick.sh --rollback-from source/build/autotune/日期_时间
```

自动调优结果绑定本机、编译器和源码哈希；换机器或源码变化后会拒绝安装。native
二进制只适用于当前主机。

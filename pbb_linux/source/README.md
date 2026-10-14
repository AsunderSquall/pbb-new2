# PBB Linux CLI 源码

此目录是 Linux CLI 版源码。PBB_Linux 已移除图形 UI、监控器和 UI 引擎；UI 在此版本中不可用。

使用上一级目录的脚本：

```bash
../setup_env.sh
../start_cli.sh --range-end 1000000 --threads 4
../rebuild.sh
../tune_quick.sh --speed 4 --minutes 20
```

`main.py` 调用 `build/pbb_engine`，`build.py` 编译 `pbb_core*.so` 和 CLI 引擎。

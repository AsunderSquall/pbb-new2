# environment 包放在哪里

这是完整、独立的 Python 3.12 / GCC 环境组件，不是差分补丁，不包含测号程序、源码或个人配置。

第一次使用：下载 `PBB-windows-x64-pure.zip` 和 `PBB-windows-x64-environment.zip`，把两个包解压到**同一个父目录**，让它们的 `PBB` 文件夹合并。先解压哪个都可以。

例如程序在 `D:\PBB\source`，那么本包中的环境必须放在 `D:\PBB\runtime`：

```text
D:\PBB\
  PBBMonitor\             ← pure 包中的监控器
  source\                 ← pure 包中的源码
  config\                 ← pure 包中的所有配置
  runtime\                ← environment 包中的环境
    python\python.exe
    mingw64\bin\g++.exe
  start_ui.bat
  rebuild.bat
  README_ENVIRONMENT.md
```

不要放成 `D:\PBB\PBB\runtime`，也不要放到 `source`、`config` 或 `PBBMonitor\_internal` 里面。

已经有这套 environment 的用户，以后只下载新 pure 包，覆盖程序和源码即可，保留 `runtime`。**更新 pure 时保留自己的 config 文件夹，不要用包里的默认配置覆盖个人配置。** Python ABI或工具链要求有变化时，新版本 README 会明确要求更新 environment。

pure 中的预编译监控器可独立运行；源码入口、自动优化测试和随包 Rebuild 使用这里的完整环境。无需另外安装 Python 或 GCC。

推荐先关闭测号，运行 `source\tune_full.bat`，测试时长推荐20分钟。脚本按输入速度的90%估算任务量，编译不计时；超过期望时长仍继续完成。测试结束后仍须运行 `rebuild.bat` 或在 UI 勾选 Rebuild 重建一次，才应用测试选择。

环境依赖的第三方许可和版本见 `THIRD_PARTY_ENVIRONMENT.md`、`environment.json` 及 runtime 内的许可文件。

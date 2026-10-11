# PBB pure / environment 使用说明

解压整个 `PBB` 文件夹，勿只复制 exe。适用于 Windows x64，默认二进制使用保守指令集。
pure 包含程序、源码和配置；environment 是独立的完整 Python/GCC 组件，不是差分补丁。已有环境只更新 pure；没有环境时下载两个包，将它们解压到同一个父目录，合并 PBB 文件夹。runtime 必须与 source、config、PBBMonitor 同级，具体示例见 environment 内的 README_ENVIRONMENT.md。

- `start_ui.bat`：启动测号监控器，无需安装 Python 或编译器。
- `start_cli.bat`：使用 environment 中的 Python 启动普通测号器，默认读取 `config/config.yaml`。
- `rebuild.bat`：先关闭监控器，再双击，使用包内 GCC 按当前 CPU 重编普通引擎、UI 引擎和 Python 核心模块。
- `source/run_ui.bat`：从 Python 源码启动 UI。

**推荐暂停测号后运行 source/tune_full.bat，时长推荐20分钟；测试完成后仍必须 Rebuild 一遍，否则引擎尚未应用选择。** 测试按输入速度90%估算任务量，编译不计入期望时长，超过时长也继续完成。可运行 rebuild.bat 重建普通/UI和核心，或 UI 勾选 Rebuild 后启动来重建 UI 引擎。重编后的二进制针对当前电脑优化；换电脑请重新编译。
UI 主界面的 `Rebuild` 只重编监控器的 UI 引擎；普通版没有 Top 排行榜收集开销。

两个组件合并后包含 Python 运行时、标准库、Tk、头文件、导入库、Python 依赖、MinGW-w64 GCC 工具链及其依赖、监控器、普通和 UI 两版引擎、Python/C++ 源码、配置示例、字体、图标、测试和第三方许可文件。
不包含个人输出数据、虚拟测号大文件、Git 历史、VS 或 Intel oneAPI。

终端自定义参数示例（在解压目录运行）：

```bat
start_cli.bat -c config\source_config.yaml --threads 8 --range-end 1000000
```

配置文件均位于最外层的 config 文件夹：`config/config.yaml`、`config/config_football.yaml`、`config/config.example.*`、`config/build_config.json`。更新 pure 时保留个人配置，不要覆盖它们。
相对配置路径以最外层 PBB 为基准；上面的文件应位于 PBB/config/source_config.yaml。
也可以直接调用 `runtime/python/python.exe`，或先 `call environment.bat` 再调用包内工具。

运行日志及结果：普通版写入 `source/out`，打包监控器写入 `PBBMonitor/_internal/out`。
两者运行数据独立，默认读取同一份 config/config.yaml。编辑器可选择“保存到当前文件”或“保存到 config.yaml”；前者保留当前 YAML/JSON/TOML 格式，后者保存 config/config.yaml 并切换到它。字符类型支持多选，自定义字符、Unicode 区间输入框按需显示。

第三方说明见 environment 里的 THIRD_PARTY_ENVIRONMENT.md。工具链的许可文本和原始包元数据随包保留；
本包未附 GCC 等第三方工具的完整源码，若对外再分发，请自行核实对应许可及源码提供要求。

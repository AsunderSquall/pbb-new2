# PBB Linux 版

本目录由 `PBB` Windows 源码和说明迁移而来，使用 GCC、Python 3.14 和
pybind11 构建 Linux x86-64 CLI。图形 UI、监控器和 UI 引擎已从 Linux 版移除，
明确不可用。Linux 构建会根据本机 CPU 选择 `-march=native` 和可用 SIMD；
生成的二进制只应在兼容的本机使用。

## 快速配置环境

在 `PBB_Linux` 目录执行：

```bash
chmod +x setup_env.sh
./setup_env.sh
```

脚本会依次完成以下步骤：

1. 检查 `runtime/miniconda/bin/python`；不存在时优先使用项目根目录的
   `Miniconda3-latest-Linux-x86_64.sh`，否则使用系统 Python 3.10+ 创建 venv。
2. 检查 `g++`，并给出 Debian/Ubuntu、Fedora/RHEL、Arch 的安装命令。
3. 安装 `pybind11` 和 `PyYAML`。
4. 编译 CLI 引擎和 `pbb_core` Python 模块。

只配置 Python 依赖而暂不编译：

```bash
./setup_env.sh --skip-build
```

脚本可以重复执行；已有环境会复用，编译结果会由 `rebuild_local.py` 更新。
若没有 Miniconda 安装器且系统 Python 低于 3.10，请先安装 Python 3.10+、
`python3-venv` 和 GCC，再重新运行脚本。

如果看到 `ImportError: libpython3.12.so.1.0`（或其他 Python 主次版本），
说明旧的 `pbb_core` 是用另一套 Python 编译的。使用下面的命令清理并重建：

```bash
rm -f source/build/pbb_core*.so source/build/pbb_core*.so.build.json
./setup_env.sh
```

`start_cli.sh`、`tune_full.sh` 和 `tune_quick.sh` 会自动加载
`runtime/miniconda/lib`，不要直接用另一套 Python 从 `source/` 导入 `pbb_core`。

## 已完成的环境和构建

- `runtime/miniconda/`：独立 Python 3.14 环境，已安装 `pybind11`、`PyYAML`。
- `source/build/pbb_core.cpython-314-x86_64-linux-gnu.so`：Python 字符集模块。
- `source/build/pbb_engine`：CLI 引擎。
- `config/`：从 Windows 版复制的配置文件。

## 使用

```bash
./start_cli.sh --range-end 1000000 --threads 4
./rebuild.sh
./tune_quick.sh --speed 6.1 --minutes 20
```

`rebuild.sh` 只重建 CLI 引擎和 Python 模块。CLI 默认使用 `config/config.yaml`，
输出写入 `source/out/` 或配置指定目录。不要运行或依赖任何 UI/监控器入口；它们
在 PBB_Linux 中不可用。

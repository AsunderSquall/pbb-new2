# 仓库结构与约定

本文说明本仓库的**代码分类、路径解析约定、注释约定**，以及从发布包反推开发仓库的过程与验证记录。
规则本身在 [`.gitignore`](.gitignore) 与 [`.gitattributes`](.gitattributes)。

---

## 1. 目录结构

```
.
├── main.py  ui.py               根目录的两个薄启动器（runpy 转发到 pbb 包）
├── pbb/                         Python 包（全部实现代码）
│   ├── __init__.py              包的说明与层次索引
│   ├── paths.py                 全项目唯一的路径解析入口
│   ├── config_schema.py         配置字段与默认值；config→engine 键名映射唯一真相源
│   ├── build_settings.py        本机构建选项（build_config.json）读写与有效性判定
│   ├── engine.py                引擎子进程参数构建、输出解析、结果拆分
│   ├── main.py                  CLI 编排层
│   ├── ui.py                    图形监控器（界面实现，可用相对导入）
│   ├── gui.py                   冻结入口：PyInstaller 用的绝对导入薄壳 + 启动自检
│   ├── autotune.py              本机自动测速（局部搜索 + 正确性对拍）
│   ├── _build.py                编译实现（_binary_is_current / _compile_engine / …）
│   └── native/                  C++ 源码
│       ├── engine_main.cpp      引擎入口
│       ├── autotune_filter_verify.cpp  过滤函数与容量哨兵的对拍程序
│       └── src/                 核心头文件 + v0..v5 历史实验版本 + vgen
├── tools/                       构建与打包脚本（不属于 pbb 包）
│   ├── PBBMonitor.spec          PyInstaller 打包定义
│   ├── package_release.py       生成 pure 包
│   ├── package_offline.py       生成 environment 包
│   └── rebuild_local.py         按本机重建三个二进制并同步监控器
├── tests/                       8 个测试模块（自带 sys.path 引导）
├── docs/                        AUTOTUNE.md、PACKAGING.md
├── assets/                      icon.ico / icon.png / icon_taskbar.png
├── fonts/                       SarasaMonoSC-Regular.ttf
├── config/                      config_football.yaml、config.example.*、build_config.fresh.json
├── offline/                     发布包根目录模板（README/ENVIRONMENT.md + 8 个脚本）
├── build/                       编译产物（首次构建时生成，已忽略）
└── _release_artifacts_20261010/ 旧发布布局归档（已忽略，见 §5）
```

### 1.1 为什么 `pbb/native/` 放在包内

C++ 源码是构建输入，不是运行期资源，但它必须能用 `PBB_SRC_DIR` 选择历史实验版本
（`native/src/v0_diag_ksa` … `v5_nobranch_restrict`）。放进包内让「Python 代码 + 它编译的
C++ 代码」成为同一个单元，`tools/package_release.py` 只要整棵带出 `pbb/` 就同时带上两者。

### 1.2 为什么 `build/` 不在包内

编译产物是环境相关的（`-march=native`、绝对编译器路径），而且要在开发仓库与发布包的
`source/` 目录之间以同样的相对位置出现。放在数据根（`<仓库根>/build`）既方便忽略，
也让发布包内的路径推导与开发仓库一致。

---

## 2. 路径解析：`pbb/paths.py` 是唯一入口

重构前 `build.py`（`BASE_DIR`）、`engine.py`（`_runtime_base_dir`）、`ui.py`（`ROOT`）、
`autotune.py`（`ROOT`）各自算一次目录，而且都假设「模块文件所在目录 == 仓库根」。
代码一旦分层这个假设就不成立，所以把路径集中到一处：

| 名称 | 开发仓库 | 发布包 `source/` | PyInstaller 冻结 |
|---|---|---|---|
| `PACKAGE_DIR` | `<仓库根>/pbb` | `<包根>/source/pbb` | `_internal/pbb` |
| `REPO_ROOT` | `<仓库根>` | `<包根>/source` | 退回 `DATA_ROOT` |
| `DATA_ROOT` | `<仓库根>` | `<包根>/source` | `_internal` |
| `BUILD_DIR` | `<仓库根>/build` | `<包根>/source/build` | `_internal/build` |
| `NATIVE_SRC` | `<仓库根>/pbb/native/src` | 同左（在 source 内） | `_internal/pbb/native/src` |

- `REPO_ROOT` 通过「向上找到同时含 `pbb/__init__.py` 与 `tools/` 的那一级」确定，
  开发仓库与发布包都成立；冻结时没有 `tools/`，退回包目录上一级。
- `DATA_ROOT` 在冻结时用 `sys._MEIPASS`（`_internal/`），其余情况等于 `REPO_ROOT`。
KeyError 的关键推论：**`pbb/paths.py` 里没有一处 `os.path.dirname(__file__)` 的临时推导**，
所有模块都从这里取路径。新增路径需求请加到这个模块，不要在别处再算一次。

`build_settings.package_root()` 负责从任意起点归一到「外层包根」（`config/`、`runtime/`、
`PBBMonitor/` 所在的那一级）：

- `_internal` → 上浮两层（`PBBMonitor/_internal` → 包根）
- `source/` 且内含 `pbb/` → 上浮一层
- 其余 → 自身

---

## 3. 入口与调用方式

根目录只有两个 `.py`，它们是薄壳，负责把仓库根加入 `sys.path` 后
用 `runpy.run_module(..., run_name="__main__")` 执行包内模块。不能写成
`python pbb/main.py`：包内模块使用相对导入（`from .engine import ...`），按文件路径
执行会失去包上下文。

| 用途 | 命令 | 等价写法 |
|---|---|---|
| 命令行测号 | `python main.py -c config/config_football.yaml` | `python -m pbb.main` |
| 图形监控器 | `python ui.py` | `python -m pbb.ui` |
| 重建二进制 | `python tools/rebuild_local.py` | — |
| 自动测速 | `tune_full.bat` / `tune_quick.bat` | `python tools/tune.py`（见下） |
| 打包 pure | `python tools/package_release.py` | — |
| 打包 environment | `python tools/package_offline.py` | — |

`tools/` 下的脚本都用同样的方式引导：先把仓库根与 `tools/` 自身加入 `sys.path`，
再 `from pbb import ...` 或导入同目录的兄弟模块。

### 3.1 根目录脚本的两种角色

| 脚本 | 角色 | 开发仓库能否直接跑 |
|---|---|---|
| `run.bat`、`run.sh`、`run_ui.bat` | 开发入口：准备 `.venv`、探测解释器与编译器 | 可以 |
| `tune_full.bat`、`tune_quick.bat` | 开发入口：转发到 `tools/tune.py` | 可以 |
| `start_ui.bat`、`start_cli.bat`、`rebuild.bat`、`environment.bat` | **发布外壳**：假设 `runtime/`、`PBBMonitor/`、`source/` 已按发布布局就位 | **不能** |

后一组的权威副本在 `offline/`，由 `tools/package_release.py` 放进发布包根。
开发仓库里重建二进制请用 `python tools/rebuild_local.py`；它已能识别
「没有 PBBMonitor」并跳过同步。

---

## 4. 注释约定

1. **说明性注释一律用中文**（既有英文注释保留）。
2. 注释解释**为什么**与**约束**，不复述代码。该写注释的地方：单位与量纲、边界与哨兵、
   编译宏的开关后果、跨语言/跨进程的协议约定、以及「看起来可以简化但不能简化」的原因。
3. 简单赋值、显而易见的 `return`、`import` 不加注释。
4. 公开函数/类写中文 docstring；私有助手用行内注释即可。

### 4.1 用测试守住注释不回退

`tests/test_docstring_coverage.py` 用 `ast` 扫描核心模块，要求带 docstring 的公开定义数
与其中含中文的条数都不低于基线（当前 66 个公开定义 / 36 个带 docstring / 35 条含中文）。
**基线只允许上调。**

### 4.2 大文件的分区块注释

`pbb/native/src/scoring_1124.hpp`、`sp_models_extra.hpp`、`charset_data.hpp` 主要是数据表，
采用「文件头总述 + 数据块起始处一行说明 + 只给计算函数写 docstring」，不逐行注释常量。

---

## 5. 建仓过程记录

### 5.1 起点：这是一个发布包，不是开发仓库

建仓时目录是 `tools/package_offline.py` 产出的**合并后发布布局**
（`source/`、`config/`、`PBBMonitor/`、`runtime/` 同级）。已按打包脚本实际读取的路径
反推成开发仓库，再按功能分层重排为上表结构。

### 5.2 重构中修掉的缺陷

移动文件会暴露原先靠目录层级隐藏的假设，以下是真实会崩的问题：

| 缺陷 | 症状 | 修法 |
|---|---|---|
| `BASE_DIR` 用 `__file__` 推导 | 模块进包后 `BASE_DIR` 变成 `pbb/`，`config/`、`build/`、`src/` 全部错位 | 真实定义收拢到 `pbb/paths.py`，`_build.BASE_DIR` 降级为别名 |
| `engine._runtime_base_dir()` | 同一问题，且冻结/源码两种形态各写一套推导 | 统一走 `paths.BUILD_DIR`；`pbb_core` 延迟加载改为 `_pbb_core()`，避免模块导入即失败 |
| `autotune.ROOT` | 快照、源码哈希、二进制目标路径全错 | 改用 `paths.REPO_ROOT` / `paths.engine_binary(root=ROOT)` |
| `rebuild_local.py` 用 `.parent` 找 PBBMonitor | 上移到根后指向 `D:\namerena`，同步静默失败 | 改用 `build_settings.package_root(paths.DATA_ROOT)` |
| `_bundled_toolchain()` 从 `BASE_DIR` 向上探 | 发布布局下探不到包根的 `runtime/` | 改为从 `paths.DATA_ROOT` 向上探 |
| `distribution_configs()` 依赖本机 `build_config.json` | 开发仓库只有 `.fresh.json`，发布包缺 `build_config.json` | 改为无条件写出 `build_settings.fresh()` |
| `stage_configs()` 的 `root/'source'` 清理分支 | 删掉它会让内层残留第二份配置 | 两种布局都清理 |
| `install()` 局部变量名 `paths` | 遮蔽同名模块导入，触发 `UnboundLocalError` | 改名 `artifacts` |
| 测试用 `patch("ui.messagebox...")` 字符串形式 | `pkgutil.resolve_name` 会导入顶层模块 `ui`，也就是根目录启动器，**直接启动 GUI 主循环把测试挂死** | 全部改为 `patch.object(ui, ...)` |

最后一条值得强调：字符串形式的 `patch()` 会按模块名重新导入。根目录存在同名启动器
（`main.py` / `ui.py`）时，这会把启动器当模块导入并执行其 `runpy` 调用。测试里一律用
`patch.object()`。

### 5.3 首次发布暴露并修掉的问题

| 问题 | 症状 | 修法 |
|---|---|---|
| `tools/PBBMonitor.spec` 用相对路径 | PyInstaller 把 spec 内的相对路径相对于 **spec 文件所在目录** 解析，`pbb/ui.py` 被找成 `tools/pbb/ui.py` | 头部按 spec 的 `__file__` 推出 `REPO`，脚本、`datas`、`binaries`、`icon` 全部改成绝对路径 |
| `toolchain_files()` 校验 `__pycache__` 条目 | 工具链目录里被清理过的 `.pyc` 让打包在"文件必须存在"上报错，而拷贝/压缩阶段本来就会跳过它们 | 跳过 `__pycache__` 与 `.pyc` 条目 |
| `package_offline.py` 打印 unittest 输出崩溃 | stdout 是 GBK，遇到替换字符 `\ufffd` 直接 `UnicodeEncodeError`，打断与本问题无关的打包流程 | 新增 `_force_utf8_stdio()`，把 stdout/stderr 固定为 UTF-8 并 `errors="replace"` |
| `_bundled_toolchain()` 命中归档目录 | 向上探测会命中 `_release_artifacts_*/runtime/mingw64`，导致"是否找到随包工具链"取决于归档还在不在 | 跳过名字以 `_` 开头的祖先目录 |
| 测试依赖 `build/` 已存在 | 干净检出后 `tempfile.TemporaryDirectory(dir=build.BUILD_DIR)` 直接报 `FileNotFoundError` | 新增 `paths.ensure_build_dir()`；`pbb/_build.ensure_core()` 与相关测试显式调用 |
| `test_ui_engine` 在无二进制时失败 | 把"还没编译"当成回归 | 改为 `setUpClass` 里 `SkipTest`，并去掉写死的 `Path("build/...")` |

### 5.4 冻结监控器启动即崩（`start_ui.bat` → `ImportError`）

**现象**

```
Traceback (most recent call last):
  File "ui.py", line 29, in <module>
ImportError: attempted relative import with no known parent package
```

`start_ui.bat` 启动的是冻结后的 `PBBMonitor.exe`，一启动就崩。

**根因**

PyInstaller 把 `Analysis` 里的入口脚本当作**顶层脚本**执行（模块名 `__main__`），
此时没有包上下文。而 spec 的入口当时写的是 `pbb/ui.py`，它第 29 行是

```python
from . import build_settings, engine as engine_runner, paths
```

相对导入在顶层脚本里必然失败。之前的 `ui.py` 用的是绝对导入，所以这个问题一直没暴露；
重构成包之后把 `pbb/ui.py` 直接当入口，才第一次踩到。

注意 PyInstaller 的报错文件名只显示 `ui.py`（basename），不含 `pbb/` 前缀，容易误判成
根目录那个启动器。

**修法**

新增 `pbb/gui.py`：只用绝对导入（`from pbb.ui import Monitor, ...`），作为冻结入口；
spec 的 `Analysis` 改为指向它。分工变成：

- `pbb/ui.py`  界面实现，可自由使用相对导入；
- `pbb/gui.py` 冻结入口，只用绝对导入；
- 根 `ui.py`   源码用户的 `runpy` 薄启动器。

### 5.5 打包流程新增「冻结产物启动自检」

这次事故的教训是：**PyInstaller 打包成功 ≠ 能跑起来**。相对导入、漏收集的延迟导入模块，
都只在运行时才炸。因此 `tools/package_release.py` 新增 `check_frozen_monitor()`：
冻结完成后执行 `PBBMonitor.exe --check`，不等同于走完整事件循环 —— 它构建主窗口、
跑一遍事件循环、再销毁，能自动发现导入与资源路径问题并立即返回。

windowed 构建（`console=False`）的 bootloader 会丢弃 stdout/stderr，崩溃的包还会静默挂住，
所以判据不靠标准输出：

- 成功：进程正常退出**且**出现 `.gui_check_ok`；
- 失败：超时（坏包不会自己退出）或非零退出 → 读 `.gui_check_error.log`（`pbb/gui.py`
  在 `except` 里把完整 traceback 落盘），把内容原样打进打包日志后中断发布。

实测反证：把归档里未修复的 `PBBMonitor.exe` 换进去，自检在 120 秒后以
`timed out ... the frozen monitor hung instead of exiting` 正确拦下；修复后的包
`--check` 退出码 0 且标记文件出现，另起真实 GUI 也不崩。

### 5.6 批处理指向了不存在的 `tools/tune.py`

**现象**（用户双击 `tune_full.bat` / `tune_quick.bat`）

```
..\runtime\python\python.exe: can't open file
'D:\namerena\PBB-windows-x64-pure\PBB\source\tools\tune.py': [Errno 2] No such file or directory
```

**根因**

包结构重构时，我把开发仓库里的 `tune_full.bat` / `tune_quick.bat` 改成调用
`tools/tune.py`（对齐 `main.py` / `ui.py` / `rebuild_local.py` 的薄启动器风格），
但**那个文件从未创建**。三个入口里只有它没做薄启动器。

为什么会漏掉：我当时的验证是"在开发仓库里跑 `python -m unittest` 与各模块导入"，
而没有任何一步真的去执行 `.bat` 指向的路径。开发仓库里也没人会双击 `tune_*.bat`，
所以直到用户拿到包才暴露。

**修法**

1. 新增 `tools/tune.py`：与 `main.py` / `ui.py` 同一套做法 —— 把源码根加入 `sys.path`，
   再 `runpy.run_module("pbb.autotune", run_name="__main__")`。不能直接执行
   `pbb/autotune.py`，它内部用相对导入（`from . import build_settings, paths`）。
2. **新增 `check_entry_points()` 到打包流程**：把「批处理脚本 → 源码入口」的引用关系
   显式建模 —— 扫描 `offline/*.bat` 与根 `*.bat` 里的 `.py` 字面量（跳过含 `%` 的变量
   与 `%~dp0` 拼接路径），逐个核对「文件存在」且「在 `source_paths()` 清单里」，
   任何一条不成立就在打包期报错。同类问题从此自曝，不再留给用户。
3. 这个校验立刻又抓出一个**真实缺陷**：根目录的 `rebuild.bat` 第 5 行是
   `... rebuild_local.py`（裸文件名，依赖 cwd）——它们是旧发布布局的残留副本，
   在开发仓库里根本跑不起来（都假设 `runtime/`、`PBBMonitor/` 同级）。
   既然权威副本就在 `offline/`，直接删掉根目录这 4 个（`rebuild.bat`、`start_cli.bat`、
   `start_ui.bat`、`environment.bat`），只留可用开发入口，消除歧义。

**验证**：用归档的 Python 3.12 在**发布布局**里演练（解出 `source/` 与外壳脚本，以
`source/` 为 cwd）—— `tools/tune.py --help`、`tools/rebuild_local.py --help`、
`main.py --help` 全部 exit 0 且不再报文件缺失；`DATA_ROOT` / `BUILD_DIR` / `config_root`
分别解析到 `<包根>/source`、`<包根>/source/build`、`<包根>/config`，正确。

### 5.7 输出 zip 被占用

`os.replace` 落到 `release/PBB-windows-x64-environment.zip` 时报 `PermissionError [WinError 5]`，
连 `Remove-Item` 都失败（"正由另一进程使用"）——杀软/索引服务长时间持有刚生成的包。
两个打包脚本都改成走 `tools/_archive.write_zip_atomic()`：

1. 先写 `<目标>.zip.building` 临时文件，再原子替换（失败也不会留下半个 zip）；
2. 目标被占用时重试 10 次（指数退避）；
3. 仍然占用则改名为 `<名字>.<时间戳>.zip` 保留成品，并明确打印实际路径。

### 5.8 行尾统一为 LF
整理时发现仓库行尾是**混合**的（`engine.py`、`main.py`、`run.bat`、`run.sh`、
`config/*`、`docs/AUTOTUNE.md` 以及全部 `src/**` 都是 CRLF，其余 LF）。
已逐字节统一为 LF（只丢弃 CR，不改动其它字节），并在 `.gitattributes` 里对每类文本
显式写 `eol=lf`。复核：116 个文本文件，含 CRLF 的 **0** 个。

> cmd.exe 能正确执行 LF 结尾的 `.bat`，所以 `.bat` 也用了 LF。若以后确实需要 CRLF
> 批处理，把 `.gitattributes` 的 `*.bat` 改成 `eol=crlf` 并把已有 `.bat` 转回 CRLF。

---

## 6. 分类总览（提交 / 忽略）

| 类别 | 处置 | 依据 |
|---|---|---|
| `pbb/**.py`、`pbb/native/**` | **提交** | 实现代码与编译输入 |
| `tools/*.py`、`tools/PBBMonitor.spec` | **提交** | 构建与打包脚本 |
| `tests/*.py`（8 个） | **提交** | 离线包验证会跑整套 |
| `main.py`、`ui.py` | **提交** | 根目录薄启动器 |
| `docs/`、`README.md`、`先读我.md`、`REPO.md` | **提交** | 文档 |
| `assets/`、`fonts/` | **提交** | 图标与字体（`tools/PBBMonitor.spec` 收集） |
| `config/config_football.yaml`、`config.example.*`、`build_config.fresh.json` | **提交** | 生产门控、示例、未测试模板 |
| `config/config.yaml`、`config/build_config.json`、`config/sby.toml` | **忽略** | 生成物 / 本机状态 / 个人配置 |
| `build/**`（含 `build/autotune/**`） | **忽略** | `pbb/paths.py` 的 `BUILD_DIR` |
| `PBBMonitor/**`、`runtime/**` | **忽略** | 只在发布时由打包脚本生成 |
| `release/`、`dist_releases/`、`release_build/`、`release_stage/` | **忽略** | 打包中间产物与 zip |
| `out/`、`**/out/`、`*.log`、`blue.txt` | **忽略** | 运行结果 |
| `__pycache__/`、`*.pyc`、`.venv/` | **忽略** | 缓存与虚拟环境 |
| `README.pdf`、`先读我.pdf` | **忽略** | 由外部工具从 Markdown 导出 |
| `_release_artifacts_*/` | **忽略** | 旧发布布局归档 |

---

## 7. 验证记录

### 7.1 结构重构

- **C++ 语法**：8 个核心头文件 + 两个入口文件逐个 `-fsyntax-only` 独立包含，全部 exit 0；
  `engine_main.cpp` 做过一次完整 `-O2` 编译，exit 0。
- **「只加注释」校验**：用 `ast` 剔除 docstring 后 dump 对比（Python），
  以及词法状态机剥离 `//` `/* */` 后逐字符对比（C++，跳过字符串字面量与预处理续行），
  与归档原始副本一致，不同数 0。
- **路径解析**：`paths.describe()` 逐项核对，与预期目录一致。
- **导入无副作用**：`import pbb.ui` 不会启动 GUI（有 `if __name__ == "__main__"` 保护）。
- **真实测号**：`python main.py -c config/config_football.yaml --threads 2 --range-end 1000000 --seed 123`
  → `Done in 0.3s`、`Speed: 0.3464T/d`，exit 0（先真实编译出 `pbb_core` 与引擎）。

### 7.2 单元测试（两种环境都通过）

```
# 干净检出（没有 build/）：测试自建目录，需要引擎二进制的用例跳过
Ran 46 tests ... OK (skipped=1)

# 构建之后（python tools/rebuild_local.py）
Ran 47 tests ... OK
```

### 7.3 首次发布（2026-10-10）

发布环境：开发机只有 Python 3.13/3.14，而 spec 绑定 **CPython 3.12**（`pbb_core.cp312`），
PyInstaller 也只装在随包 Python 里。因此从归档复制了一份可写的 Python 3.12.11 到
`build/_release_py312/`（已忽略），配合归档的 MinGW g++ 15.1.0 完成打包；
`uv python install 3.12` 装的解释器受 PEP 668 保护装不了包，未采用。

产物（`release/`，已被 `.gitignore` 忽略）：

| 文件 | 大小 | 条目 | CRC |
|---|---|---|---|
| `PBB-windows-x64-pure.zip` | 40.52 MB | 1145 | 通过 |
| `PBB-windows-x64-environment.zip` | 161.14 MB | 9711 | 通过 |

pure 包内已验证关键文件 16/16 就位（启动器、`config/` 六个配置、`source/pbb`、
`source/tools`、`PBBMonitor/PBBMonitor.exe`、三个预编译二进制及清单、两份 PDF）；
environment 包 5/5 就位（`runtime/python`、`runtime/mingw64`、许可与元数据）。

**离线包内验证全部通过**：随包 Python + GCC 冒烟测试 → 重新编译三个原生二进制 →
真实跑一次百万级测号 → `unittest discover` **47 项全过（OK）**。

---

## 8. 尚未解决的缺口

### 缺口 1：PDF 生成工具未固定

`tools/package_release.py` 在 PDF 缺失时给出可操作提示，但导出工具链本身未固定
（用哪个工具、什么字体、CI 是否需要装 xelatex）。建议选定一种后补进 `docs/PACKAGING.md`。

### 缺口 2：没有 `LICENSE`

`THIRD_PARTY_ENVIRONMENT.md:29-30` 自己提示再分发前需核实许可与对应源码提供方式。

### 缺口 3：字体许可文本缺失

`THIRD_PARTY_ENVIRONMENT.md:7` 写「Font license: `fonts/`」，但该目录只有 ttf，
没有 OFL 文本。

### 缺口 4：`README_ENVIRONMENT.md` 与 `offline/ENVIRONMENT.md` 内容重复

前者是后者被 `tools/package_offline.py` 拷贝成发布包文件的结果，需手工同步。

### 缺口 5：构建环境前置条件只写在脚本里

`tools/package_offline.py`：`--mingw-root` 默认 `C:\msys64\mingw64`；
要求 Windows x64 + **精确 Python 3.12**；校验 `g++/gcc/as/ld/gendef/dlltool` 存在。

### 缺口 6：`.bat` 里的中文正文

cmd.exe 按 OEM 代码页解析 `.bat`，无 `chcp 65001` 的脚本里中文注释/`echo` 会显示错乱。
目前 `offline/*.bat` 与根 `*.bat` 正文基本只用 ASCII。后续若加中文，需先定编码策略。

---

## 9. 建仓操作

```powershell
git init
git add -A
git status                       # 应约为 126 个文件 / 28 MB
git commit -m "Initial commit: PBB package layout, sources and packaging tools"
git tag v<version>
```

发布流程（Windows x64 + Python 3.12，需要两个 PDF 齐备）：

```powershell
python -m pip install pyinstaller pybind11 PyYAML
python tools/package_release.py     # -> release/PBB-windows-x64-pure.zip
python tools/package_offline.py     # -> release/PBB-windows-x64-environment.zip
```

两个 zip 上传到 GitHub Release 作为附件。

# PBB 名字评分测号器

PBB 用真实名字计算属性、技能和评分，支持顺序及随机枚举、前后缀、队伍名、潜力筛选和特殊属性号收集。当前发布配置选择 QP、QD、PP、PD、CQD、CP、FP、WC、FS、PJ 十个评分族。图形界面提供进度、速度、排行榜、配置编辑和线程控制。

支持具名多字符集与格式列表：固定字符串和 `{charset: 族名}` 可交错排列，族内枚举单元等宽。旧配置继续兼容，使用说明见 [多字符集格式](docs/FLEX.md)，可从 `config/config_flex.example.yaml` 开始。
随包另提供 `config/config_words_sequential.yaml` 和 `charsets/` 词库，顺序枚举“形容词的名词副词地动词”，例如“重要的经济必须地发展”。所有随附测号配置均启用PP8000与PD8800。

## 下载与放置

Windows x64 发布提供两个独立组件：

- **pure**（`PBB-windows-x64-pure.zip`）：监控器、源码、预编译引擎、默认配置、启动脚本和说明。
- **environment**（`PBB-windows-x64-environment.zip`）：完整 Python 3.12、MinGW GCC 工具链及其依赖和许可说明。

已有 environment 的用户只需更新 pure。没有环境的用户下载两个包，解压到同一个父目录，合并其中的 `PBB` 文件夹。`runtime`、`source`、`config`、`PBBMonitor` 应当同级，不要嵌套成 `PBB/PBB/runtime`。environment 内的 `README_ENVIRONMENT.md` 说明放置方法。

pure 的图形监控器自带运行所需的精简环境，可以独立启动；外层 CLI、自动测试和 `rebuild.bat` 的标准使用方式需要配套 environment。自行配置 Python、依赖和编译器的用户也可以直接运行源码入口。

更新 pure 前备份个人 `config/` 及输出。压缩包包含同名默认配置，直接覆盖可能替换自己的设置。environment 可继续复用。

## 首次使用

1. 暂停其他测号任务，双击外层 **`tune_full.bat`**。
2. 输入以前完整测号的速度，单位 **T/day**，允许小数，例如 `6.1`；输入期望测试时长，建议 **20 分钟**，回车默认20。
3. 测试成功完成后运行 **`start_ui.bat`**，勾选 **Rebuild 引擎**，再启动测号。测试本身不会替换正在使用的引擎，必须重建才能应用选择。
4. 配置在外层 **`config/`**。可以通过 UI 的“编辑配置”修改，也可以手动编辑。

UI 的 Rebuild 只重建 UI 引擎。如果还要使用 CLI，关闭监控器后运行外层 **`rebuild.bat`**：它重建普通/UI 引擎及 Python 字符集模块，并同步监控器的二进制。

`tune_quick.bat` 是精简测试，尝试的选项及确认轮数少于 full。测试脚本使用配置中的线程数，不自动扫描线程数。

## 发布包文件结构

下面是两个组件合并后的结构。标注“运行后生成”的目录不一定在首次解压时出现。

```text
PBB/
├── start_ui.bat                 图形监控器入口
├── start_cli.bat                普通命令行入口
├── tune_full.bat                完整自动测试入口
├── tune_quick.bat               精简自动测试入口
├── rebuild.bat                  重建普通/UI 引擎及 Python 模块
├── environment.bat              设置随包 Python/GCC 环境
├── README.md / README.pdf        完整说明
├── 先读我.md / 先读我.pdf        极简说明
├── PACKAGING.md                  构建和发布说明
├── config/                      测号配置（要测什么）
│   ├── config.yaml              外层 CLI 默认使用的配置
│   ├── config_football.yaml      足球配置
│   └── config.example.*          YAML/JSON/TOML 示例
├── settings/                    应用设置（程序自己的选择/偏好，随包发布）
│   ├── build_config.json         本机优化选择，初始未测试
│   └── ui_settings.json          启动默认配置 + 新配置的默认评分阈值
├── out/                         测号输出（结果/blue.txt/续测检查点），与 source 同级
├── PBBMonitor/
│   ├── PBBMonitor.exe            预编译监控器
│   └── _internal/                监控器依赖、源码和运行资源
│       ├── build/                UI 引擎、Python 模块及清单
│       └── src/                  随监控器提供的 C++ 源码
├── source/                      可独立运行的项目源码
│   ├── main.py / ui.py           CLI / 源码 UI
│   ├── build.py                  编译与二进制清单检查
│   ├── build_settings.py         构建选项和配置路径
│   ├── engine.py                 字符集构建、引擎调用和结果解析
│   ├── engine_main.cpp           C++ 引擎入口
│   ├── config_schema.py          配置字段和默认值
│   ├── autotune.py               自动测试实现
│   ├── AUTOTUNE.md               测试、安装和回退细节
│   ├── rebuild_local.py          重建并同步监控器二进制
│   ├── tune_full.bat / tune_quick.bat
│   ├── run.bat / run.sh / run_ui.bat
│   ├── fonts/                    字体资源
│   ├── src/                      C++ 实现，详见下节
│   └── build/
│       ├── pbb_engine.exe        普通引擎
│       ├── pbb_ui_engine.exe     UI 引擎
│       ├── pbb_core*.pyd         Python 字符集模块
│       ├── *.build.json          二进制清单
│       └── autotune/             隔离测试记录，运行后生成
├── runtime/                     environment 组件提供
│   ├── python/                  完整 Python 和依赖
│   └── mingw64/                 GCC 工具链
├── README_ENVIRONMENT.md         environment 放置说明
├── THIRD_PARTY_ENVIRONMENT.md    环境许可说明
└── environment.json             环境组件信息
```

外层的两个测试脚本转到 `source/` 的对应脚本执行。图形监控器使用 `_internal/build/` 中的 UI 引擎，外层 CLI 使用 `source/build/` 中的普通引擎。它们共用外层配置，**输出也统一写到外层的 `out/`**。

结果不再跟着入口走：无论是监控器、源码 UI 还是 CLI，结果文件、`blue.txt`、续测检查点与线程控制文件都落在**包根 `out/`**（开发仓库里就是仓库根的 `out/`）。找不到结果时先看这里的 `out/`。

## 源码结构与计算路径

开发仓库以 `main.py`、`ui.py`、`build.py` 等文件为根，不额外套一层 `source/`。未建立根 `config/` 时，开发入口兼容根目录现有配置；发布包统一使用外层 `config/`。

```text
src/
├── common.hpp             共享类型和指令集条件
├── charset.hpp            字符集与 Unicode 编码
├── charset_data.hpp       内置字符数据
├── utils.hpp              SIMD 等基础实现
├── name.hpp               名字状态、KSA、属性及技能计算
├── sp_models_extra.hpp    附加评分模型系数
├── scoring_1124.hpp       评分特征、模型、评分计划及门控
├── engine.hpp             任务队列、枚举、评分、输出和 UI 协议
├── bridge.cpp             pybind11 字符集数据接口
├── v0_diag_ksa/ ... v5_nobranch_restrict/
│                          历史实验版本
└── vgen/                  虚拟属性生成分析工具
```

正常入口编译共享 `src/`，不使用历史版本或 `vgen/` 替代真实引擎。`engine_main.cpp` 包含 `engine.hpp`；后者包含字符集及 `scoring_1124.hpp`，评分路径继续使用 `name.hpp`、`utils.hpp` 和模型数据。Python 的 `pbb_core` 提供字符集接口，真实测号由 C++ 子进程执行。

普通和 UI 引擎通过 `PBB_UI_ENGINE` 分别编译。只有 UI 引擎维护和传输 Top 5；排行榜走内部协议，无需把 `output_xp` 改成1。

## 配置与编辑器

支持 YAML、JSON、TOML。外层 `start_cli.bat` 不带参数时使用「启动默认配置」；直接运行 `main.py` 不指定 `-c` 时，同样先看 `settings/ui_settings.json` 的 `default_config`，再依次尝试 `config.yaml`、`config_football.yaml`、配置目录里第一个测号配置；UI 可选择配置文件。

编辑器只有一个**保存**按钮：目标由顶部的文件名输入框决定（改文件名即"另存为"，目录固定为 `config/`，缺扩展名补 `.toml`）。另存为已存在的文件时会先确认覆盖。保存成功会弹确认框，两个按钮「继续编辑」/「退出编辑」。各标签页（基础 / 前后缀 / 评分 / 输出 / 多字符集格式）都可纵向滚动，小屏上不用拉伸窗口。

「评分」页底部有两个默认值按钮，只作用于评分阈值：

- **使用默认评分**：把 `settings/ui_settings.json` 里记着的默认评分阈值填回本页（不动字符集/前缀/枚举范围），点「保存」才落盘。
- **设为默认评分**：把本页阈值记为"新配置的默认值"（只写 `collection`，不改启动默认配置）。

启动默认配置由配置选择器的「设为启动默认」设置。选择器里默认配置那一行带 **★ 默认** 标记并整行高亮。

配置中的主要字段：

- `team_name`、`prefixes`、`suffixes` 表示队伍名与前后缀。前后缀名称 `+` 表示空串。名称可以包含英文逗号和首尾空格，例如 `MerryChristmas,Mr.Lawrence`。结果文件按行保存，名称中禁用换行、回车、制表符和 NUL。
- `character_set`：单个枚举单元的字节宽度（1～64）、类型、自定义内容及 Unicode 区间。自定义内容按字节宽度切分，每个单元必须是完整的 UTF-8 文本。单个 Unicode 码点最多占 4 个 UTF-8 字节，更长的单元可用自定义内容组合多个字符。
- `enumeration`：枚举模式、可变单元数和范围。区间允许 `end == start`（空区间，测完后「自动更新 start」会写这种值）；`end < start` 仍然报错。
- `enumeration.ranges[0]`（顺序模式）：**全局区间是唯一的总控制器**。`start` 表示"总量里已经测过多少、从哪开始"，`end` 是总量坐标的上界、`-1` 表示所有前缀的总量之和。`prefix_ranges` 只是各前缀的**布局**，程序永不修改它的 start/end；其中 `end = -1` 表示该前缀的完整字符集总量。全局坐标按前缀顺序累加（前缀 `p` 占 `[C_p, C_p + 总量_p)`），开跑时把全局 `[start, end)` 映射回各前缀的绝对区间。resume 与「自动更新 start」都只改全局 `start`。
- `collection`：评分族、普通及潜力阈值、评分模式、特殊号收集规则。
- `output`：文件名、是否输出分数、日志和速度设置。
- `threads.worker_threads`：启动线程数，`-1` 表示自动检测逻辑 CPU 数。

配置选择器里有「设为启动默认」：把当前选中配置的文件名记进 `settings/ui_settings.json`，之后打开界面就直接加载它；文件不存在时按 `config.yaml` → `config_football.yaml` → 第一个可用配置的顺序回退。

`settings/ui_settings.json` 的 `collection` 段是**新配置的默认评分阈值**（初始值等于 `config_football.yaml` 的 collection）；「编辑新配置」用它填阈值。这份文件随版本控制并随发布包发布，改它就改了默认值。

**两类文件分开放**：`config/` 只放**测号配置**（要测什么：队伍、前后缀、字符集、枚举范围、阈值……）；`settings/` 放**应用设置**——`build_config.json`（本机编译/调优选择）与 `ui_settings.json`（界面偏好）。旧包装把 `build_config.json` 放在 `config/` 下，仍然读得到，但新写入一律进 `settings/`。

发布时 `config.yaml` 由足球配置生成。当前普通阈值为 QP5400、QD6200、PP8000、PD8800、CQD4000、CP4100、FP4200、WC4000、FS3900、PJ4100；潜力阈值为 CP4500、FP4600、WC4400、FS4300、PJ4500。其他初筛条件由当前评分源码决定。自动优化不修改这些门控或收集规则。

QP按模型有效幻术熟练度与幻影属性强度作连续二次扣分，公式见“UI运行与输出”。不再使用 `QP-PP` 差值或幻影强度硬排除整名；其他评分族、潜力及属性收集继续按各自规则判断。输出列仍由 `score_list` 决定。

不要把降低阈值、少算评分族、改成短路评分所得的速度当成同一生产任务的优化收益。配置示例用于说明字段；实际测号以选中的配置为准。

### 字符类型

UI 字符类型是多选，至少选一项。选择自定义或 Unicode 类型后出现相应输入框，两者可以同时选择。类型按配置顺序拼接；原有顺序保留，新增选项追加，不自动去重。

- **1字节**：1数字、2小写英文、3大写英文、4自定义、5自定义兼容类型。类型5并非完整 ASCII 表。
- **2字节**：1希腊小写、2希腊大写、3俄文小写、4俄文大写、5拉丁小写、6拉丁大写、7自定义、8 Unicode区间。
- **3字节**：1基础汉字区间、2内置汉字表、3平假名、4片假名、5盲文、6扩展汉字、7自定义、8 Unicode区间。
- **4字节**：1扩展汉字、2自定义、3 Unicode区间。
- **5至8字节**：1自定义、2 Unicode区间。字节宽度表示一个枚举单元的长度，不一定等于一个 Unicode 字符；足球配置采用6字节单元。

自定义字符串按 UTF-8 编码，也兼容数字字节列表。内容的总字节数必须符合所选宽度。

Unicode 区间在界面里每行填一段，用 `-` 连接两个端点，包含起点和终点。端点支持两种写法，可以混用。

- **`U+` 表示 Unicode 码点**。例如 `U+10F70-U+10FAF`。原先输入的十六进制码点 `0x4E00` 现在写作 `U+4E00`。
- **`0x` 表示一个字符的 UTF-8 字节**。每字节用两位十六进制数字，字节之间直接连写。`0xF090BDB0-0xF090BEAF` 解码后和 `U+10F70-U+10FAF` 是同一区间，单字符字节数都填 `4`。

界面保存时将端点转换为 `unicode_ranges` 中的 `start`、`end` 整数码点，重新打开时显示为 `U+` 形式。手动编辑配置文件时，这两个字段直接填写整数码点。

### 枚举、种子与前后缀

模式1顺序枚举 `[start, end)`，各前缀分别处理范围，后缀按前缀序号循环配对。模式2随机选择前后缀并在每个任务中顺序展开局部编号。模式3逐位随机抽样，可能重复。模式4采用模式2的编码方式，前后缀按序号配对。

引擎以一百万为 chunk 大小。模式2/4的尾 chunk 仍处理一百万候选，不能将不足一百万的范围长度直接当成精确处理量。局部空间小于处理量或不同 chunk 取到重叠空间时仍可能重复，模式2/4不保证全程去重。

顺序枚举的区间端点最多能用无符号 64 位整数表示，`end: -1` 也受此上限约束。组合数超过上限时，可用多个任务分别固定部分位置，将剩余位置控制在可枚举范围内。随机模式指定 `seed` 后，chunk 种子由全局种子和任务编号派生。比较结果需要同时固定字符集、前后缀、范围和其他配置；输出顺序及 UI 同分名字的到达顺序可能随线程调度变化。

### 评分和收集

`score_list` 选择计算族；可用名称为 `qp,qd,pp,pd,cqd,cp,fp,wc,fs,pj`，`rh` 为 `fs` 别名。留空会提示「评分族为空，测号文件不会产生任何输出！」。`score_mode: all` 按计划计算；`any` 遇到达标族后停止后续族，会改变计算和输出语义，不能作为保持语义的提速手段。

评分族非空时，图形编辑器保存会自动补上有正阈值（`min_*` 或 `ptt_*`）的族。要停算其中某一族，请把该族阈值改回 0，再从评分族栏删掉。手工编辑非空 `score_list` 时，有正阈值的族也必须列出。清空评分族栏可以停算全部评分族。

`collect_mode: 0` 关闭特殊号收集，`1` 使用预设规则并将特殊号追加到 `out/blue.txt`。预设收集与 `score_list` 独立。以前使用自定义阈值的配置（`collect_mode: 2`）会按关闭处理。

## 自动测试与重建

自动测试目前使用 GCC，按 CPU 支持情况尝试过滤路径、指令集、KSA、编码复制、属性比较和编译选项等组合。每接受一项改变，后续试验基于当前累积组合。它是局部搜索，不保证全局最快；具体候选、确认轮数和正确性覆盖见 `source/AUTOTUNE.md`。

输入速度只用于规划测试量：初始按输入吞吐量的90%估算，范围取整到一百万的倍数，至少一百万。编译时间另计。期望分钟数是软目标，超出后继续完成计划测试及最终确认；错误或用户中断不会发布未完成选择。

默认比较 UI 引擎完整任务性能，并验证普通引擎正确性。结果、参数、输出、哈希、逐轮速度和报告在 `source/build/autotune/日期_时间/`；不写生产结果文件。成功后更新外层 `settings/build_config.json`，初始状态为 `tested: false`、`status: "none"`。

选择绑定机器、编译器路径和源码哈希。换机器或相关源码变化后会回到默认参数。`build.py` 在 GCC native 普通/UI 构建中使用有效选择；Python 字符集模块和 portable 模式不采用这些优化开关。

发布引擎使用保守 x86-64 基础指令集。本机 native 构建可以使用 CPU 支持的 SIMD；默认 GCC 引擎含 LTO、分段 KSA、属性 min/max 和编码复制优化，实际收益因机器和任务而异。显式 `PBB_CXXFLAGS` 覆盖默认编译参数；随包 `rebuild.bat` 调用的重建脚本会清除此覆盖，按本机配置重建。

回退本轮仅保存的选择：关闭引擎，将测试目录中的 `build_config.before.json` 恢复到外层 `settings/build_config.json`，然后重新 Rebuild。通过自动测试高级安装选项替换二进制的回退方法见 `AUTOTUNE.md`。

## UI 运行与输出

QP使用连续二次扣分：`max(0, u*(82.48156669219784-0.24932235019037452*s+0.0001942206736111687*s*s))`，其中 `u` 是SP1模型有效幻术熟练度，`s` 是SP1幻影属性加权强度。先按旧口径截断原QP，再减扣分并截断为整数；负扣分夹到0。没有有效熟练度60或左零点的硬排除，也不使用QP−PP差值过滤。普通/UI Top/输出阈值使用修正QP，其他评分族与配置阈值不变。

UI 左侧的启动线程、范围终点和结果文件会用于当前任务，启动前检查这些值。“活跃线程”可在运行中调整，范围是1至本次启动线程数，提交后工作线程在当前 chunk 完成后调整。

**Resume 续测仅支持模式1**。多字符集格式任务使用结果文件对应的 `.resume` 连续完成检查点；旧单字符集配置仍按最后一条命中反推位置——命中稀少或结果文件还是空的时候，续测会比实际进度**落后**（甚至从头开始），这是已知限制，需要引擎也为旧式顺序任务写每前缀检查点才能彻底解决。**续测只修改全局 `start`**（把完成位置折算成总坐标），然后从 `start` 开跑；`prefix_ranges` 保持原样。继续原任务需要选择原配置和原结果文件，不能用随机模式或自动生成的新文件名代替。评分规则版本改变后，格式任务的旧检查点会被拒绝，需使用新的结果文件启动。

收起状态的Top1保持无边框的文字外观，可划选后Ctrl+C复制，也可Ctrl+A全选。旁边的小“复制”按钮复制当前第一名的完整`名字@队伍`，不附加评分；暂无数据时按钮禁用。

CLI 文件名 `-` 表示时间戳，`+` 表示随机名，其他值使用指定名称。UI 按左侧“结果文件”文本使用文件名。`output_xp: 0` 只写名字与队伍，1附加实际被计算评分族的整数分数；列按 `qp qd pp pd cqd cp fp wc fs pj` 的固定顺序取被计算的子集，并非固定只有 XP/XD 两列。

普通结果为 `名字@队伍名`，后接可选分数。具体输出目录见发布结构一节。CLI 的 `--output-dest` 可选 `file`（默认）、`stdout`、`both`；stdout 模式使用临时目录，不保留结果文件。

## 源码运行与命令行

自行运行源码需要 Python、`pybind11`、`PyYAML` 和 C++17 编译器。TOML 读取使用 Python 3.11+ 的标准库；更早 Python 的 CLI 需 `tomli`，UI 使用标准库 `tomllib`。随包环境为 Python 3.12。

开发仓库可用 `run.bat` 或 `run.sh` 完成环境准备和 CLI 启动；`-y` 跳过脚本的安装确认。发布包用户优先使用外层入口，不需要重新克隆仓库。

```powershell
# 在源码目录执行；发布包可使用 ../runtime/python/python.exe
python -m pip install pybind11 PyYAML
python main.py -c config_football.yaml --range-end 1000000
python ui.py
```

`main.py --help` 显示完整参数。常用覆盖项包括 `--threads`、`--team`、`--mode`、`--vlen`、`--range-start`、`--range-end`、`--seed`、`--scl`、`--types`、`--custom-values`、`--output-xp`、`-o`、`--output-dest`、`--resume`、`--rebuild`、`--no-auto-start`。修改参数会改变任务，测速比较双方应使用相同设置。

命令行与界面共用同一套默认值：

- **不带 `-c`** 时按「`settings/ui_settings.json` 里的启动默认 → `config.yaml` → `config_football.yaml` → `config/` 里第一个测号配置」选配置；外层的 `start_cli.bat` 直接双击也能跑（没有随包运行时就用 PATH 里的 `python`）。
- **`--range-start/--range-end` 在顺序模式下也生效**：它们改的是配置里的**全局**区间（已测数量 / 预计总量），再按前缀布局映射成各前缀的绝对区间；随机模式照旧直接改编号区间。
- **「自动更新 start」默认开启**：测号过程中每处理 0.01T（10^10 个候选）就把进度写回配置的全局 `start`，中途断电/崩溃最多损失 0.01T；写盘由引擎自己的进度行触发，不轮询、不占线程。`--no-auto-start` 可以关掉（关掉后跑完也不回写）。

编译器和 SIMD 的实际选择以构建输出为准；自动探测到指令集支持不等于该路径已在本机测速证明更快。二进制旁的 `.build.json` 记录构建信息，源码 UI 与预编译监控器使用不同的二进制位置，替换其中一个不会自动替换另一个。

## 特别鸣谢

特别鸣谢：powerless、曙光、shenjack、区猴猴区、超导体元素、Shabby_fish、abruce 对本项目的贡献。也感谢每一个参与测试的玩家。最后感谢 deepseek 和 openai 的模型。

"""全项目唯一的路径解析入口。

**为什么需要这个模块**

在重构之前，`build.py`（`BASE_DIR`）、`engine.py`（`_runtime_base_dir()`）、
`ui.py`（`ROOT`）、`autotune.py`（`ROOT`）各自算了一次目录，而且都建立在
「模块文件所在目录 == 仓库根」这个隐含前提上。代码一旦按包结构分层，这个前提
就不成立了（模块在 `pbb/`，而 `build/`、`fonts/`、`config/` 在包外）。

所以这里把「仓库根 / 包根 / 数据根」和各个标准子目录集中定义一次，其余模块一律
从这里引用，不再自己推导。

**两种布局**

- 开发仓库：``pbb/paths.py`` → 升两级得到仓库根。
  ``main.py``、``build/``、``config/``、``fonts/``、``pbb/native/`` 都在仓库根下。
- 冻结的监控器（PyInstaller onedir）：``sys._MEIPASS`` 指向 ``_internal/``，
  引擎与 ``pbb_core`` 位于 ``_internal/build/``，字体位于 ``_internal/fonts/``。
  此时包内代码所在的 ``_internal`` 就是数据根。
"""
from pathlib import Path
import sys

# 冻结时 _MEIPASS 是 PyInstaller 的解包目录（_internal/）；否则取本文件所在目录。
_FROZEN = hasattr(sys, "_MEIPASS")
_SELF = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

# 包目录 pbb/：开发仓库里就是本文件所在目录，冻结后是 _internal/pbb/。
PACKAGE_DIR = Path(__file__).resolve().parent


def _find_repo_root() -> Path:
    """从包目录向上找到"源码根"（同时含 pbb/__init__.py 与 tools/ 的那一级）。

    开发仓库与发布包的 source/ 都满足这个条件；PyInstaller 冻结后的 _internal/
    不满足（没有 tools/），此时退回包目录的上一级作为兜底。
    """
    for candidate in (PACKAGE_DIR, *PACKAGE_DIR.parents):
        if (candidate / "pbb" / "__init__.py").is_file() and (candidate / "tools").is_dir():
            return candidate
    return PACKAGE_DIR.parent


# 仓库根（"源码根"）：pbb/ 与 tools/ 的父目录。发布包里是 <包根>/source。
REPO_ROOT = _find_repo_root()

# 数据根：build/、fonts/、assets/ 的父目录。
#   开发仓库 / 发布包 → 仓库根（= 源码根，与 REPO_ROOT 相同）
#   PyInstaller 冻结   → _internal/（spec 把 fonts、assets、build 都收集到这里）
DATA_ROOT = _SELF if _FROZEN else REPO_ROOT

# ── 标准子目录（开发仓库与发布包布局；冻结布局与之同名，都在 DATA_ROOT 下） ──
BUILD_DIR = DATA_ROOT / "build"          # 编译产物：引擎、pbb_core、autotune 记录
FONTS_DIR = DATA_ROOT / "fonts"
ASSETS_DIR = DATA_ROOT / "assets"
CONFIG_DIR = DATA_ROOT / "config"
NATIVE_DIR = PACKAGE_DIR / "native"      # C++ 源码：engine_main.cpp、filter 对拍、src/

# 默认头文件树（PBB_SRC_DIR 为空时的选择）与默认桥接源码。
# 历史实验版本是 NATIVE_DIR/src/ 下的子目录，用 PBB_SRC_DIR=<子目录名> 选择。
NATIVE_SRC = NATIVE_DIR / "src"
ENGINE_MAIN = NATIVE_DIR / "engine_main.cpp"
FILTER_VERIFY = NATIVE_DIR / "autotune_filter_verify.cpp"
BRIDGE_SRC = NATIVE_SRC / "bridge.cpp"

# 运行时资源。
FONT_FILE = FONTS_DIR / "SarasaMonoSC-Regular.ttf"
ICON_FILE = ASSETS_DIR / "icon.ico"
ICON_PNG_FILE = ASSETS_DIR / "icon_taskbar.png"


def ensure_build_dir() -> Path:
    """确保编译产物目录存在并返回它。

    路径常量本身保持"纯函数"（只做推导、不碰文件系统），所以创建目录放在这里显式调用。
    调用点有两处：
      - ``pbb/_build.py`` 在写二进制/清单之前；
      - ``pbb/ui.py`` 的启动流程（那里会拿它拼 ``pbb_core`` 的搜索路径）。
    这样测试或工具即使先删掉 ``build/``，也不会因为目录不存在而报错。
    """
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    return BUILD_DIR


def engine_binary(ui: bool = False, root: Path | None = None) -> Path:
    """返回引擎二进制路径（含 Windows 的 .exe 后缀）。

    ``ui=True`` 取维护 Top 5 排行榜的 UI 引擎；两者由同一个源码树按
    ``PBB_UI_ENGINE`` 宏分别编译（见 pbb/_build.py）。

    ``root`` 可覆盖数据根（默认 ``DATA_ROOT``），只有把仓库根指向临时目录的场景
    才需要：autotune.install() 显式传入它自己的 ROOT，这样测试 patch 掉
    autotune.ROOT 之后，读写的仍然是夹具目录而不是真实 build/。
    """
    base = Path(root) if root is not None else DATA_ROOT
    name = "pbb_ui_engine.exe" if ui else "pbb_engine.exe"
    return base / "build" / name


def native_source_tree(env: str) -> tuple[Path, list[Path]]:
    """按 ``PBB_SRC_DIR`` 的值返回 (bridge.cpp 路径, include 目录列表)。

    ``env`` 为空或 "src" 时用正式头文件树；否则当作 ``native/src/`` 下的历史
    实验版本子目录名（如 ``v1_baseline``）。include 列表始终把正式 ``src/``
    放在最后，保证历史版本缺省时仍能找到公共头文件。
    """
    env = (env or "").strip()
    if env and env != "src":
        candidate = NATIVE_SRC / env
        if candidate.is_dir():
            return candidate / "bridge.cpp", [candidate, NATIVE_SRC]
    return BRIDGE_SRC, [NATIVE_SRC]


def describe() -> dict[str, str]:
    """返回各关键路径的字符串形式，供诊断输出与测试断言使用。"""
    return {
        "data_root": str(DATA_ROOT),
        "repo_root": str(REPO_ROOT),
        "package_dir": str(PACKAGE_DIR),
        "build_dir": str(BUILD_DIR),
        "native_dir": str(NATIVE_DIR),
        "native_src": str(NATIVE_SRC),
        "config_dir": str(CONFIG_DIR),
        "frozen": str(hasattr(sys, "_MEIPASS")),
    }

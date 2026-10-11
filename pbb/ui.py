"""PBB 图形监控器：编排普通测号引擎并向用户呈现进度、速度与排行榜。

与 CLI（main.py）的关系：
  - 共用 main.py 的配置校验与任务组装（_validate_config、_build_task_config），
    共用 engine.py 的参数构建与输出解析；
  - 差别在于监控器编译并使用 pbb_ui_engine.exe（PBB_UI_ENGINE=1），只有它维护
    并上报 Top 5 排行榜。

排行榜的数据来源是引擎在 stderr 上发出的 UI_TOP 事件（见 src/engine.hpp），
**不是**读取结果文件，也不要求把 output_xp 打开。

界面与引擎之间的并发约定：工作线程只向 self.events 队列投递消息，
所有控件操作都在 Tk 主线程的 _drain_events() 里完成。
"""
import copy
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from . import build_settings, engine as engine_runner, paths
from ._build import _compile_engine, _binary_is_current, ensure_core
from .main import _build_task_config, _resolve_result_file, _validate_config


# 数据根：开发仓库里是仓库根，PyInstaller 冻结后是 _internal/。
# 字体、图标、build/ 都在它下面（config/ 走 build_settings.config_root）。
ROOT = str(paths.DATA_ROOT)
CONFIG_DIR = str(build_settings.config_root(ROOT))
UI_FONT = "等距更纱黑体 SC"
# 字体与图标的绝对路径统一由 pbb/paths.py 给出，PyInstaller 打包后同样解析到 _internal/ 下。
UI_FONT_FILE = str(paths.FONT_FILE)
ICON_FILE = str(paths.ICON_FILE)
ICON_PNG_FILE = str(paths.ICON_PNG_FILE)
APP_USER_MODEL_ID = "pbb.name.search.monitor.v3"
FAMILIES = ("qp", "qd", "pp", "pd", "cqd", "cp", "fp", "wc", "fs", "pj")
# score_list 里允许出现的别名：rh（人海）等价于 fs。归一逻辑集中在 _normalize_score_list，
# 避免"编辑器的归一"和"排行榜的归一"各写一套而漂移。
_FAMILY_ALIASES = {"rh": "fs"}
ENUMERATION_MODES = {
    "顺序枚举": 1,
    "随机区间": 2,
    "随机逐位": 3,
    "随机区间并配对前后缀": 4,
}
SCORE_MODES = {"全部评分后判断": "all", "任一达标即停止后续评分": "any"}
COLLECT_MODES = {"不收集特殊号": 0, "收集预设阈值特殊号": 1, "收集自定义阈值特殊号": 2}
OUTPUT_SCORE_MODES = {"仅输出名字": 0, "输出名字和评分": 1}
# 结果文件名模式 (对应 output.result_file 的三种写法, 见 main._resolve_result_file):
#   "+" = 随机名 (result_<8位hex>.txt), "-" = 时间戳 (result_<YYYYMMDD_HHMMSS>.txt),
#   其他 = 自定义文件名。两个符号是跨语言契约 (CLI 的 -o 也用同一套), 不要改含义。
RESULT_FILE_MODES = {"随机名": "+", "时间戳": "-", "自定义": ""}
CHARSET_TYPES = {
    1: {
        "数字": 1,
        "小写英文字母": 2,
        "大写英文字母": 3,
        "自定义字符": 4,
        "自定义字符（兼容类型 5）": 5,
    },
    2: {"希腊小写": 1, "希腊大写": 2, "俄文小写": 3, "俄文大写": 4, "拉丁小写": 5, "拉丁大写": 6, "自定义字符": 7, "Unicode 区间": 8},
    3: {"全部基础汉字": 1, "常用汉字": 2, "平假名": 3, "片假名": 4, "盲文": 5, "扩展汉字": 6, "自定义字符": 7, "Unicode 区间": 8},
    4: {"扩展汉字": 1, "自定义字符": 2, "Unicode 区间": 3},
}
PROGRESS_RE = re.compile(
    r"tot=(\d+), \([^)]*\),time: ([0-9.]+)s, speed: ([0-9.]+)T/d,time left:([^\s]+)"
)
COUNT_RE = re.compile(r"count:([0-9.]+)T")
CURRENT_RE = re.compile(r"cur_speed:([0-9.]+)T/d")


def _normalize_score_list(raw) -> list[str]:
    """把 score_list 的原始写法归一成有序的族名列表（``rh`` → ``fs``、去重、丢弃无效项）。

    空结果表示"全十族"的语义由调用方决定（编辑器会写成空串，排行榜会展开成全十族）。
    """
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(item) for item in raw)
    result: list[str] = []
    for part in str(raw or "").split(","):
        name = part.strip().lower()
        name = _FAMILY_ALIASES.get(name, name)
        if name in FAMILIES and name not in result:
            result.append(name)
    return result


def load_config(path: str) -> dict:
    """按扩展名读取 YAML / JSON / TOML 配置。

    相对路径一律相对 CONFIG_DIR（由 build_settings.config_root 推出，见文件头），
    因此 UI 里选择的文件名可以只是一个裸文件名。TOML 用标准库 tomllib（Python 3.11+）。
    """
    if not os.path.isabs(path):
        path = os.path.join(CONFIG_DIR, path)
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".toml":
        with open(path, "rb") as fh:
            import tomllib
            return tomllib.load(fh)
    with open(path, "r", encoding="utf-8") as fh:
        if suffix in (".yaml", ".yml"):
            import yaml
            return yaml.safe_load(fh)
        return json.load(fh)


def save_config(path, cfg):
    """保存配置到指定文件，保留该文件原有的格式（YAML / JSON / TOML）。

    TOML 没有标准库写入器，这里手写最小序列化器：只支持配置里实际用到的
    标量、列表与单层内联表；遇到其它类型直接抛 ValueError（编辑器会提示改用
    "保存到 config.yaml"）。写盘前先 tomllib.loads 自校验一遍，并写入 .tmp 再替换，
    保证校验失败时原文件不被破坏。
    """
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix in ('.yaml', '.yml'):
        import yaml
        text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    elif suffix == '.json':
        text = json.dumps(cfg, ensure_ascii=False, indent=2)+'\n'
    elif suffix == '.toml':
        import tomllib
        def value(item):
            if isinstance(item, bool):return 'true' if item else 'false'
            if isinstance(item, str):return json.dumps(item, ensure_ascii=False)
            if isinstance(item, (int, float)):return repr(item)
            if isinstance(item, list):return '['+', '.join(value(x) for x in item)+']'
            if isinstance(item, dict):
                return '{'+', '.join(json.dumps(k,ensure_ascii=False)+' = '+value(v) for k,v in item.items())+'}'
            raise ValueError('TOML 无法保存此类型，请使用保存到 config.yaml。')
        text = '\n'.join(json.dumps(k,ensure_ascii=False)+' = '+value(v) for k,v in cfg.items())+'\n'
        tomllib.loads(text)  # Check serialization before replacing the current file.
    else:
        raise ValueError('请选择 .yaml、.yml、.json 或 .toml 配置文件。')
    temporary = target.with_suffix(target.suffix+'.tmp')
    try:
        temporary.write_text(text, encoding='utf8')
        os.replace(temporary, target)
    finally:
        if temporary.exists():temporary.unlink()


def enable_windows_dpi_awareness() -> None:
    """声明本进程为 DPI 感知，避免窗口被 Windows 当位图拉伸而发虚。

    **不做会怎样**：``python.exe`` 与 PyInstaller 生成的 exe 都没有 DPI 感知声明，
    Windows 于是按 96 DPI 渲染整个窗口、再放大到实际显示器缩放比例。在 150% 缩放的
    2560x1600 屏上，Tk 看到的屏幕是 1707x1067（虚拟坐标）、11pt 文字的行高只有 20px，
    随后被整体拉伸 1.5 倍 —— 结果是整窗发虚，而且硬编码的 1100x820 只相当于
    733x547 逻辑像素，窗口显得又小又糊。

    声明之后：Tk 拿到真实坐标（2560x1600）、文字按真实 DPI 绘制（11pt 行高 30px），
    清晰度与物理尺寸都正确。相应地，窗口几何必须改用逻辑像素（见 ``_scaled_geometry``）。

    **必须在 import tkinter / 创建 Tk 根窗口之前调用**：Tk 初始化时会读取当前进程的
    DPI 感知状态并据此算出 ``tk scaling``，之后再改就晚了。
    `pbb/gui.py`（冻结入口）与 `main.py`（源码入口）都在导入 GUI 之前调它。

    调用顺序是实测出来的：``SetProcessDPIAware``（user32，始终可用）与
    ``SetProcessDpiAwareness(2)``（shcore）在本机都能生效；而
    ``SetProcessDpiAwarenessContext(-4)`` 会静默失败（屏宽仍为 1707 虚拟坐标），
    所以不把它当首选。DLL 加载时机也会影响结果，因此让 user32 那条先跑。
    """
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 2 = PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        pass


# 量 DPI 需要一个已存在的 Tk 解释器（winfo_fpixels 是 interpreter 上的命令）。
# 这里复用一个隐藏的根窗口，避免为了量一次 DPI 而多起 Tk 实例。
_DPI_PROBE_ROOT = None


def _dpi_probe_root():
    """返回用于查询 DPI / 屏幕尺寸的隐藏 Tk 根窗口（按需创建并复用）。"""
    global _DPI_PROBE_ROOT
    if _DPI_PROBE_ROOT is None:
        _DPI_PROBE_ROOT = tk.Tk()
        _DPI_PROBE_ROOT.withdraw()
    return _DPI_PROBE_ROOT


def windows_dpi_scale() -> float:
    """返回系统 DPI 相对 96 的缩放系数（例如 150% → 1.5）。

    问 Tk（``winfo_fpixels('1i')`` 是 Tk 眼中的每英寸像素数）：它已经反映了本进程的
    DPI 感知状态与所在显示器。查询失败时兜底 1.0，让界面退化成旧行为而不是崩掉。
    非 Windows 上这个值在多数平台同样正确，所以不做平台分支。
    """
    try:
        pixels_per_inch = float(_dpi_probe_root().winfo_fpixels("1i"))
        if pixels_per_inch > 0:
            return pixels_per_inch / 96.0
    except Exception:
        pass
    return 1.0


def scaled_geometry(logical_width: int, logical_height: int,
                    max_fraction: tuple[float, float] = (0.96, 0.92)) -> str:
    """把逻辑像素尺寸换算成当前 DPI 下的物理像素 ``"WxH"``，并限制在屏幕内。

    ``logical_width/height`` 按 96 DPI 的设计尺寸书写（例如 1100x820）；在 150% 缩放下
    会得到 1650x1230。``max_fraction`` 是对屏幕宽/高的占用上限 —— 高分屏上物理像素会
    成比例变大，超出屏幕就等于窗口开不出来，所以必须夹一下（高度留更多余量给标题栏
    与任务栏）。
    """
    try:
        root = _dpi_probe_root()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    except Exception:
        screen_w, screen_h = 1920, 1080
    scale = windows_dpi_scale()
    width = min(int(round(logical_width * scale)), int(screen_w * max_fraction[0]))
    height = min(int(round(logical_height * scale)), int(screen_h * max_fraction[1]))
    return f"{max(width, 640)}x{max(height, 480)}"


def _scaled_min_size(logical_width: int, logical_height: int) -> tuple[int, int]:
    """把逻辑像素的最小尺寸换算成物理像素，供 ``minsize()`` 使用。

    与 ``scaled_geometry`` 不同，这里不保证能塞进屏幕：最小尺寸是"用户手动拖拽的下限"，
    把它夹到屏幕以内会让高分屏上的窗口能被拖到过小。只对高度留一点余量（标题栏 + 任务栏）。
    """
    try:
        screen_h = _dpi_probe_root().winfo_screenheight()
    except Exception:
        screen_h = 1080
    scale = windows_dpi_scale()
    width = int(round(logical_width * scale))
    height = min(int(round(logical_height * scale)), int(screen_h * 0.95))
    return max(width, 480), max(height, 360)


def pixels(logical: int) -> int:
    """把「96 DPI 下的逻辑像素」换算成当前 DPI 的物理像素，用于 Tk 的像素类参数。

    **为什么需要**：Tk 里 ``width=`` / ``height=`` 在不同控件上单位不同 ——
    Entry/Label/Text 等按**字符数**计（与 DPI 无关，不需要换算），
    而 Frame 的 ``width``、Label 的 ``wraplength``、Canvas 的 ``height`` 按**像素**计。
    这类像素参数如果写死，在 150% 缩放下就会偏小（例如侧栏 390 只相当于 260 逻辑像素），
    表现为布局被挤扁、文字换行位置莫名其妙。

    因此：控件尺寸写逻辑像素时一律经本函数换算，不要直接写物理像素。
    """
    return max(1, int(round(logical * windows_dpi_scale())))


def load_ui_font():
    """把随包字体注册到本进程，供 Tk 使用「等距更纱黑体 SC」而不必系统安装。

    AddFontResourceExW 的 0x10 = FR_PRIVATE：字体只对当前进程可见，不污染系统字体表。
    非 Windows 或字体缺失时静默返回，Tk 会回退到默认等宽字体。
    """
    if sys.platform != "win32" or not os.path.exists(UI_FONT_FILE):
        return
    import ctypes
    ctypes.windll.gdi32.AddFontResourceExW(UI_FONT_FILE, 0x10, None)


def configure_windows_identity():
    """声明 AppUserModelID，让任务栏把监控器与自己的图标、窗口归为同一应用。

    不设置时 Windows 会用 python.exe（源码运行）或宿主的 ID，导致任务栏图标错乱。
    """
    if sys.platform != "win32":
        return
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)


def _top_level_hwnd(window) -> int:
    """返回承载窗口图标与任务栏按钮的**顶层** HWND。

    **为什么不能直接用 winfo_id()**：Tk 的 ``winfo_id()`` 返回的是 toplevel 内部的
    子窗口，而 Windows 的任务栏与标题栏读的是它外面那层 wrapper（``GetParent``）。
    实测：图标设在 ``winfo_id()`` 上时，wrapper 的 ``WM_GETICON`` 两个槽位仍是旧句柄，
    任务栏因此继续显示旧图标。所以必须一路向上取到没有父窗口的那一层。
    """
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = window.winfo_id()
    while True:
        parent = user32.GetParent(hwnd)
        if not parent:
            return hwnd
        hwnd = parent


def apply_windows_window_icon(window):
    """把随包 .ico 设到窗口的**顶层 HWND**，让标题栏与任务栏图标一致。

    Tk 的 ``iconphoto`` 不保证传到 Windows 任务栏，所以直接在 HWND 上设 WM_SETICON。
    两个槽位分别使用各自的标准尺寸，否则会被系统缩放而发糊：

      * ``ICON_SMALL`` —— 任务栏/标题栏小图标，尺寸取 ``SM_CXSMICON``（本机 24px）；
      * ``ICON_BIG``   —— Alt+Tab / 任务切换大图标，尺寸取 ``SM_CXICON``（本机 48px）。

    早期实现把 ``SM_CXICON``(48) 同时塞进两个槽位，小图标槽位被从 48 缩到 24，
    是"任务栏图标不对"的原因之一；另一个原因是设在了子窗口上（见 _top_level_hwnd）。

    句柄缓存在 ``window._native_icon_handles`` 上，避免被 GC 回收导致图标回落默认值。
    """
    if sys.platform != "win32" or not os.path.exists(ICON_FILE):
        return
    import ctypes
    image_icon = 1           # IMAGE_ICON
    load_from_file = 0x0010  # LR_LOADFROMFILE
    wm_seticon = 0x0080      # WM_SETICON
    user32 = ctypes.windll.user32

    small_size = user32.GetSystemMetrics(49)   # SM_CXSMICON
    large_size = user32.GetSystemMetrics(11)   # SM_CXICON
    small = user32.LoadImageW(None, ICON_FILE, image_icon, small_size, small_size, load_from_file)
    large = user32.LoadImageW(None, ICON_FILE, image_icon, large_size, large_size, load_from_file)

    target = _top_level_hwnd(window)
    if small:
        user32.SendMessageW(target, wm_seticon, 0, small)
    if large:
        user32.SendMessageW(target, wm_seticon, 1, large)

    # 类图标也要换：部分外壳（含任务栏缩略图）优先读类图标，只设 WM_SETICON 时
    # 仍可能显示旧图标。
    try:
        gdi32 = ctypes.windll.gdi32
        if large:
            gdi32.SetClassLongPtrW(target, -14, large)   # GCLP_HICON
        if small:
            gdi32.SetClassLongPtrW(target, -34, small)   # GCLP_HICONSM
    except Exception:
        pass

    window._native_icon_handles = (small, large, target)


class Monitor(tk.Tk):
    """监控器主窗口：负责界面、任务启停、进度解析与排行榜展示。

    运行状态机（self.running / self.stop_requested）：
      未运行 → _start() 校验并锁定参数 → 后台线程 _launch() 编译+启动引擎
      → 引擎 stderr 逐行进入 self.events → 主线程 _drain_events() 消费
      → 正常结束或 _stop() 请求停止 → _set_finished() 解锁界面。

    跨线程规则：只有主线程可以碰控件；工作线程一律通过 self.events 队列通信。
    """

    def __init__(self):
        """建立窗口、加载图标与字体，并初始化运行期状态字段。

        状态字段的含义（后面多处依赖）：
          running            是否处于一次运行中（拦截重复启动、决定控件可用性）
          stop_requested     用户已请求停止，工作线程在若干检查点据此提前收尾
          rebuild_requested  本次启动前是否强制重编 UI 引擎
          runtime_thread_limit / runtime_active_threads
                             本次启动的线程上限，以及当前实际生效的线程数
                             （后者可在运行中通过 out/.threads 调整，见 _apply_runtime_threads）
          boards / boards_dirty / last_board_refresh
                             排行榜数据、是否需要重绘、以及限流用的时间戳
        """
        load_ui_font()
        configure_windows_identity()
        super().__init__()
        self.title("PBB 测号监控")
        self._icon_image = None
        if os.path.exists(ICON_FILE):
            self.iconbitmap(ICON_FILE)
        if os.path.exists(ICON_PNG_FILE):
            self._icon_image = tk.PhotoImage(file=ICON_PNG_FILE)
            self.iconphoto(True, self._icon_image)
        self.after_idle(lambda: apply_windows_window_icon(self))
        # 尺寸按"96 DPI 下的逻辑像素"书写，由 scaled_geometry 换算到当前 DPI 的物理像素。
        # 直接写死物理像素在 150% 缩放下会显得很小（1100 物理像素只相当于 733 逻辑像素）。
        self.geometry(scaled_geometry(1100, 820))
        self.minsize(*_scaled_min_size(880, 640))
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.engine_bin: str | None = None
        self.pbb_core_initialized = False
        self.running = False
        self.stop_requested = False
        self.dark_mode = False
        self.colors = {"bg": "#f7f7f7", "panel": "#ffffff", "field": "#ffffff", "text": "#171717", "accent": "#171717"}
        self._theme_checkbuttons: list[tk.Checkbutton] = []
        # 滚轮绑定去重表: canvas id -> 已绑定的控件 id 集合（见 _bind_wheel 的幂等说明）
        self._wheel_bound: dict[str, set[str]] = {}
        # canvas -> 它的滚动条（见 _setup_scrollbar / _sync_scrollbar）
        self._scrollbars: dict[str, tk.Scrollbar] = {}
        # 行容器 -> 承载它的 canvas（见 _canvas_of）
        self._scroll_canvases: dict[str, tk.Canvas] = {}
        self._editor_canvases: list[tk.Canvas] = []
        self.families: list[str] = list(FAMILIES)
        self.boards: dict[str, list[tuple[int, str]]] = {}
        self.score_views: dict[str, tk.Widget] = {}
        self.expanded_families: set[str] = set()
        self.boards_dirty = False
        self.last_board_refresh = 0.0
        self.stats = {"realtime": "--", "overall": "--", "elapsed": "--", "count": "0", "found": "0", "left": "--"}
        self.stat_vars = {key: tk.StringVar(value=value) for key, value in self.stats.items()}
        default_config = os.path.join(CONFIG_DIR, "config.yaml")
        if not os.path.isfile(default_config):
            default_config = os.path.join(CONFIG_DIR, "config_football.yaml")
        self.config_var = tk.StringVar(value=default_config)
        self.threads_var = tk.StringVar(value=str(os.cpu_count() or 4))
        self.runtime_threads_var = tk.StringVar(value=self.threads_var.get())
        self.runtime_thread_limit = 0
        self.runtime_active_threads = 0
        self.range_var = tk.StringVar(value="-1")
        # 结果文件：模式（随机名/时间戳/自定义）+ 自定义名。
        # 用两个变量而不是一个裸输入框，避免用户直接填 "+"/"-" 时被当成字面文件名。
        self.result_mode_var = tk.StringVar(value="时间戳")
        self.result_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="准备就绪")
        self.editor: tk.Toplevel | None = None
        self.editor_cfg: dict | None = None
        self.editor_vars: dict[str, tk.Variable] = {}
        self.rebuild_requested = False
        self.resume_var = tk.BooleanVar(value=False)
        self.rebuild_var = tk.BooleanVar(value=False)
        # 关闭状态与 after 记账表必须在 _build_layout() 之前初始化：
        # 布局构建过程里就会调用 _schedule_after（例如排行榜的换行绑定）。
        self._closing = False
        self._after_ids: set[str] = set()
        self._build_layout()
        self._apply_theme()
        self._schedule_after(150, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_layout(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TButton", font=(UI_FONT, 10), padding=(10, 6))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        # 侧栏宽度按逻辑像素给出后再换算：Frame 的 width 是物理像素，写死会在高 DPI 下发窄
        # （150% 下 390 只相当于 260 逻辑像素，多出来的宽度全被右栏吃掉）。
        sidebar = ttk.Frame(body, width=pixels(390))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        right = ttk.Frame(body, padding=(14, 12, 18, 12))
        right.pack(side="left", fill="both", expand=True)

        header = ttk.Frame(sidebar, style="Header.TFrame", padding=(22, 24))
        header.pack(fill="x")
        ttk.Label(header, text="PBB 测号监控", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 24)
        )
        stat_spec = (("实时速度", "realtime"), ("总体速度", "overall"), ("总时间", "elapsed"),
                     ("总测号量", "count"), ("已找到", "found"), ("剩余时间", "left"))
        for index, (label, key) in enumerate(stat_spec):
            block = ttk.Frame(header, style="Header.TFrame")
            block.grid(row=index // 2 + 1, column=index % 2, sticky="ew", padx=(0, 18), pady=4)
            ttk.Label(block, text=label, style="Header.TLabel").pack(anchor="w")
            ttk.Label(block, textvariable=self.stat_vars[key], style="StatValue.TLabel").pack(anchor="w")
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=1)

        theme_bar = ttk.Frame(sidebar, padding=(22, 14, 22, 0))
        theme_bar.pack(fill="x")
        ttk.Label(theme_bar, text="显示模式").pack(side="left")
        ttk.Button(theme_bar, text="亮色", command=lambda: self._set_theme(False)).pack(side="right")
        ttk.Button(theme_bar, text="暗色", command=lambda: self._set_theme(True)).pack(side="right", padx=(0, 6))

        controls = ttk.Frame(sidebar, padding=(22, 18, 22, 10))
        controls.pack(fill="x")
        ttk.Label(controls, text="配置").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.config_var).grid(row=1, column=0, sticky="ew", pady=(4, 6))
        ttk.Button(controls, text="导入", command=self._choose_config).grid(row=1, column=1, padx=(6, 0))
        ttk.Label(controls, text="启动线程").grid(row=2, column=0, sticky="w", pady=(8, 2))
        self.start_threads_spinbox = ttk.Spinbox(
            controls, from_=1, to=256, textvariable=self.threads_var, width=8,
        )
        self.start_threads_spinbox.grid(row=3, column=0, sticky="w")
        ttk.Label(controls, text="活跃线程").grid(row=4, column=0, sticky="w", pady=(10, 2))
        # 下限是 0：0 表示暂停计算（引擎侧把它当作"所有 worker 都睡"），
        # 不是"非法值"。负数由 Spinbox 与 _apply_runtime_threads 双重拦下。
        self.runtime_threads_spinbox = ttk.Spinbox(
            controls, from_=0, to=0, textvariable=self.runtime_threads_var,
            width=8, command=self._apply_runtime_threads, state="disabled",
        )
        self.runtime_threads_spinbox.grid(row=5, column=0, sticky="w")
        self.runtime_threads_spinbox.bind("<Return>", self._apply_runtime_threads)
        self.runtime_threads_spinbox.bind("<FocusOut>", self._apply_runtime_threads)
        # 手动输入数字后需要回车才生效，而界面没有任何提示；这里补一个显式的应用按钮，
        # 并在下面加一行小字说明取值含义与生效方式（避免"为什么打了数字没反应"）。
        self.apply_threads_button = ttk.Button(
            controls, text="应用", command=self._apply_runtime_threads, state="disabled",
        )
        self.apply_threads_button.grid(row=5, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(controls, text="0 = 暂停计算；回车或点“应用”生效",
                  font=(UI_FONT, 9)).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(controls, text="结束范围").grid(row=7, column=0, sticky="w", pady=(10, 2))
        ttk.Entry(controls, textvariable=self.range_var).grid(row=8, column=0, columnspan=2, sticky="ew")
        ttk.Label(controls, text="结果文件").grid(row=9, column=0, sticky="w", pady=(10, 2))
        self._build_main_result_file(controls)
        launch_options = ttk.Frame(controls)
        launch_options.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.resume_checkbutton = self._checkbutton(launch_options, "Resume 续测", self.resume_var)
        self.resume_checkbutton.pack(side="left")
        self.rebuild_checkbutton = self._checkbutton(launch_options, "Rebuild 引擎", self.rebuild_var)
        self.rebuild_checkbutton.pack(side="left", padx=(12, 0))
        self.start_button = ttk.Button(controls, text="开始", command=self._start)
        self.start_button.grid(row=12, column=0, sticky="ew", pady=(10, 0))
        self.stop_button = ttk.Button(controls, text="停止", command=self._stop, state="disabled")
        self.stop_button.grid(row=12, column=1, sticky="ew", padx=(6, 0), pady=(10, 0))
        ttk.Button(controls, text="编辑配置", command=self._open_editor).grid(row=13, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        controls.columnconfigure(0, weight=1)

        # 提示文字：wraplength 是像素，写死会带来两个问题 ——
        #   1. 高 DPI 下过窄，文字比可用宽度更早断行；
        #   2. 窗口拉宽后仍按旧宽度断行，右边留一大片空白。
        # 改用"跟随父容器宽度"的绑定（见 _wrap_label）。
        #
        # 同时**不再在正文里写 '\n' 硬换行**：硬换行会让断行点固定在句子中间，
        # 与容器实际宽度无关，看起来就是"换行位置莫名其妙"。现在交给 wraplength
        # 按容器宽度自然断行。
        ttk.Label(
            right, text="重要启动选项：Resume 续测 / Rebuild 引擎", style="LaunchNotice.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        self._wrap_label(ttk.Label(
            right,
            text=("接着上次进度测号请勾选 Resume。推荐暂停测号后运行外层 tune_full.bat"
                  "（推荐 20 分钟），自动选择本机优化。测试完成后仍须勾选 Rebuild 引擎"
                  "重建一次；重建前实际引擎尚未应用测试选择。"),
            style="LaunchNotice.TLabel", justify="left",
        )).pack(anchor="w", fill="x", pady=(0, 12))
        score_header = ttk.Frame(right)
        score_header.pack(fill="x", pady=(0, 6))
        ttk.Label(score_header, text="评分排行榜", font=(UI_FONT, 14, "bold")).pack(side="left")
        ttk.Button(score_header, text="全部收起为 Top 1", command=self._collapse_top5).pack(side="right")
        ttk.Button(score_header, text="全部展开 Top 5", command=self._expand_top5).pack(side="right", padx=(0, 6))
        score_view = ttk.Frame(right)
        score_view.pack(fill="both", expand=True)
        self.score_canvas = tk.Canvas(score_view, highlightthickness=0)
        score_scrollbar = ttk.Scrollbar(score_view, orient="vertical", command=self.score_canvas.yview)
        self.score_canvas.pack(side="left", fill="both", expand=True)
        score_scrollbar.pack(side="right", fill="y")
        # 内容不足一屏时自动隐藏滚动条（见 _setup_scrollbar）
        self._scrollbars[str(self.score_canvas)] = score_scrollbar
        self._setup_scrollbar(self.score_canvas, score_scrollbar)
        self.score_area = ttk.Frame(self.score_canvas)
        self.score_window = self.score_canvas.create_window((0, 0), window=self.score_area, anchor="nw")
        self.score_area.bind("<Configure>", self._sync_score_scroll)
        self.score_canvas.bind("<Configure>", self._resize_score_area)
        # 排行榜也要支持滚轮。原来这里没有任何滚轮绑定，只能拖滚动条。
        self._bind_wheel(self.score_canvas, self.score_area)
        self._render_score_boxes(self.families)

        footer = ttk.Frame(sidebar, padding=(22, 8))
        footer.pack(side="bottom", fill="x")
        ttk.Separator(footer).pack(fill="x", pady=(0, 8))
        ttk.Label(footer, textvariable=self.status_var).pack(anchor="w")

    def _set_theme(self, dark_mode: bool):
        self.dark_mode = dark_mode
        self._apply_theme()

    def _apply_theme(self):
        if self.dark_mode:
            colors = {"bg": "#101510", "panel": "#161d16", "field": "#0d120d", "text": "#eaf3ea", "accent": "#74d88d"}
        else:
            colors = {"bg": "#f7f7f7", "panel": "#ffffff", "field": "#ffffff", "text": "#171717", "accent": "#171717"}
        style = ttk.Style(self)
        self.colors = colors
        self.configure(bg=colors["bg"])
        style.configure("TFrame", background=colors["bg"])
        style.configure("Header.TFrame", background=colors["bg"])
        style.configure("TLabel", background=colors["bg"], foreground=colors["text"], font=(UI_FONT, 11))
        style.configure("Header.TLabel", background=colors["bg"], foreground=colors["text"], font=(UI_FONT, 11))
        style.configure("Title.TLabel", background=colors["bg"], foreground=colors["text"], font=(UI_FONT, 22, "bold"))
        style.configure("StatValue.TLabel", background=colors["bg"], foreground=colors["accent"], font=(UI_FONT, 15, "bold"))
        style.configure("Score.TLabel", background=colors["bg"], foreground=colors["text"], font=(UI_FONT, 11))
        launch_red = "#ff8585" if self.dark_mode else "#b91c1c"
        style.configure("LaunchNotice.TLabel", background=colors["bg"], foreground=launch_red,
                        font=(UI_FONT, 12, "bold"))
        style.configure("TButton", background=colors["panel"], foreground=colors["text"], font=(UI_FONT, 11))
        style.map("TButton", background=[("active", colors["field"])], foreground=[("disabled", "#808080")])
        style.configure("TEntry", fieldbackground=colors["field"], foreground=colors["text"], insertcolor=colors["text"], font=(UI_FONT, 11))
        style.configure("TSpinbox", fieldbackground=colors["field"], foreground=colors["text"], insertcolor=colors["text"], font=(UI_FONT, 11))
        style.configure("TCombobox", fieldbackground=colors["field"], background=colors["panel"], foreground=colors["text"], font=(UI_FONT, 11))
        style.map("TCombobox", fieldbackground=[("readonly", colors["field"])], foreground=[("readonly", colors["text"])])
        self.score_canvas.configure(bg=colors["bg"])
        for canvas in getattr(self, "_editor_canvases", []):
            if canvas.winfo_exists():
                canvas.configure(bg=colors["panel"])
        for checkbox in self._theme_checkbuttons:
            if checkbox.winfo_exists():
                checkbox.configure(
                    background=colors["bg"], foreground=colors["text"],
                    activebackground=colors["bg"], activeforeground=colors["text"],
                    selectcolor=colors["field"], font=(UI_FONT, 11),
                )
        for checkbox in (self.resume_checkbutton, self.rebuild_checkbutton):
            checkbox.configure(foreground=launch_red, activeforeground=launch_red,
                               disabledforeground=launch_red, font=(UI_FONT, 11, "bold"))
        for view in self.score_views.values():
            if isinstance(view, tk.Text):
                view.configure(bg=colors["field"], fg=colors["text"], font=(UI_FONT, 11))

    def _build_main_result_file(self, controls):
        """主窗口「结果文件」行：模式下拉框 + （仅自定义时可见的）文件名输入框。

        与编辑器输出页共用同一套语义（见 RESULT_FILE_MODES 与 main._resolve_result_file）。
        初始为「时间戳」，与历史行为一致（以前默认填 ui_<时间戳>.txt）。
        """
        holder = ttk.Frame(controls)
        holder.grid(row=10, column=0, columnspan=2, sticky="ew")
        holder.columnconfigure(0, weight=1)
        holder.columnconfigure(1, weight=1)

        self.result_mode_box = ttk.Combobox(
            holder, textvariable=self.result_mode_var, values=tuple(RESULT_FILE_MODES),
            state="readonly", width=8)
        self.result_mode_box.grid(row=0, column=0, sticky="ew")
        self.result_name_entry = ttk.Entry(holder, textvariable=self.result_var)
        self.result_name_entry.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        def sync_mode(*_args):
            """只有「自定义」显示输入框；其余模式隐藏（不覆盖 result_var，
            因为 _launch 会按模式决定实际取值，而 _resolve_result_file 以 "-" 为时间戳）。"""
            if self.result_mode_var.get() == "自定义":
                self.result_name_entry.grid()
            else:
                self.result_name_entry.grid_remove()
        self.result_mode_var.trace_add("write", sync_mode)
        sync_mode()

    def _apply_result_file_setting(self, value):
        """把配置里的 output.result_file 反映到主窗口的模式下拉框（导入配置时调用）。"""
        raw = str(value or "").strip()
        if raw == "+":
            self.result_mode_var.set("随机名")
        elif raw in ("-", ""):
            self.result_mode_var.set("时间戳")
        else:
            self.result_mode_var.set("自定义")
            self.result_var.set(raw)

    def _selected_result_file(self) -> str:
        """按当前模式给出交给 _resolve_result_file 的取值（"+"/"-"/自定义名）。"""
        mode = self.result_mode_var.get()
        if mode == "自定义":
            return self.result_var.get().strip()
        return RESULT_FILE_MODES.get(mode, "-")

    def _render_score_boxes(self, families: list[str]):
        for child in self.score_area.winfo_children():
            child.destroy()
        self.score_views.clear()
        if not families:
            ttk.Label(self.score_area, text="选择配置后点击“开始”，这里会显示各评分族的第一名。", font=(UI_FONT, 12)).pack(anchor="center", pady=80)
            self._bind_wheel(self.score_canvas, self.score_area)
            return
        for index, family in enumerate(families):
            panel = ttk.Frame(self.score_area, padding=8)
            panel.grid(row=index, column=0, sticky="ew", pady=4)
            is_expanded = family in self.expanded_families
            label = "Top 5" if is_expanded else "Top 1"
            panel_header = ttk.Frame(panel)
            panel_header.pack(fill="x", pady=(0, 5))
            family_label = f"{family.upper():<3}"
            ttk.Label(panel_header, text=f"{family_label} {label}", font=(UI_FONT, 12, "bold")).pack(side="left")
            ttk.Button(
                panel_header, text="收起" if is_expanded else "展开",
                command=lambda name=family: self._toggle_family(name),
            ).pack(side="right")
            if is_expanded:
                view = tk.Text(panel, height=6, wrap="none", state="disabled", font=(UI_FONT, 11),
                               bg=self.colors["field"], fg=self.colors["text"], relief="solid", borderwidth=1, padx=8, pady=6)
                view.pack(fill="both", expand=True)
            else:
                view = ttk.Label(panel_header, text="-", style="Score.TLabel")
                view.pack(side="left", padx=(18, 0))
            self.score_views[family] = view
        self.score_area.columnconfigure(0, weight=1)
        # 面板建完之后才绑滚轮：_bind_wheel 只遍历"当前已存在"的控件，
        # 放在建面板之前会漏掉全部面板（只剩 score_area 自己绑上）。
        self._bind_wheel(self.score_canvas, self.score_area)

    def _expand_top5(self):
        self.expanded_families = set(self.families)
        self._render_score_boxes(self.families)
        self._refresh_boards()

    def _collapse_top5(self):
        self.expanded_families.clear()
        self._render_score_boxes(self.families)
        self._refresh_boards()

    def _toggle_family(self, family: str):
        if family in self.expanded_families:
            self.expanded_families.remove(family)
        else:
            self.expanded_families.add(family)
        self._render_score_boxes(self.families)
        self._refresh_boards()

    def _sync_score_scroll(self, _event=None):
        self.score_canvas.configure(scrollregion=self.score_canvas.bbox("all"))

    def _resize_score_area(self, event):
        self.score_canvas.itemconfigure(self.score_window, width=event.width)

    def _choose_config(self, edit=False):
        parent = self.editor if self.editor and self.editor.winfo_exists() else self
        path = filedialog.askopenfilename(
            parent=parent, initialdir=CONFIG_DIR,
            filetypes=[("配置文件", "*.yaml *.yml *.json *.toml")],
        )
        if not path:
            return
        try:
            cfg = load_config(path)
            _validate_config(cfg)
        except Exception as exc:
            messagebox.showerror("无法导入配置", str(exc), parent=parent)
            return
        self.config_var.set(path)
        self._sync_config_controls(cfg)
        self.status_var.set("配置已导入")
        if not edit:
            edit = messagebox.askyesno("配置已导入", "是否打开配置编辑器进行修改？", parent=parent)
        self._close_editor()
        if edit:
            self._open_editor()

    def _sync_config_controls(self, cfg):
        if not self.running:
            threads = self._cfg_value(cfg, "threads.worker_threads", -1)
            self.threads_var.set(str(os.cpu_count() or 4) if threads == -1 else str(threads))
            families = self._families_from_task(cfg.get("collection", {}))
            if families != self.families:
                self.families = families
                self.boards = {family: [] for family in families}
                self.expanded_families.intersection_update(families)
                self._render_score_boxes(families)
                self._refresh_boards()
        ranges = cfg.get("enumeration", {}).get("ranges", [])
        self.range_var.set(str(ranges[0].get("end", -1) if ranges else -1))
        self._apply_result_file_setting(cfg.get("output", {}).get("result_file", "-"))

    @staticmethod
    def _cfg_value(cfg: dict, path: str, default=""):
        value = cfg
        for part in path.split("."):
            if not isinstance(value, dict):
                return default
            value = value.get(part, default)
        return value

    @staticmethod
    def _set_cfg_value(cfg: dict, path: str, value):
        parts = path.split(".")
        current = cfg
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value

    @staticmethod
    def _prefix_ranges_for_editor(enumeration: dict, prefix_count: int) -> list[tuple[int, int]]:
        """Expand compact YAML prefix_ranges into one range per visible prefix row."""
        fallback = enumeration.get("ranges", [{}])[0]
        default = (fallback.get("start", 0), fallback.get("end", -1))
        ranges = [default] * prefix_count
        index = 0
        for item in enumeration.get("prefix_ranges") or []:
            start = item.get("start", 0)
            end = item.get("end", -1)
            for _ in range(item.get("count", 1)):
                if index >= prefix_count:
                    return ranges
                ranges[index] = (start, end)
                index += 1
        return ranges

    def _sync_all_scrollbars(self, _event=None):
        """重新判断所有已登记的 canvas 是否需要滚动条。

        两个触发时机缺一不可：
          * 切换 Notebook 标签页（``<<NotebookTabChanged>>``）—— Tk 不映射非当前页里的
            控件，此时 winfo_height() 为 1，判断结果不可信；切回来必须重算。
          * canvas 被映射（``<Map>``）。
        """
        for canvas in list(self._scroll_canvases.values()) + [self.score_canvas]:
            try:
                if canvas.winfo_exists():
                    self._sync_scrollbar(canvas)
            except Exception:
                pass

    def _setup_scrollbar(self, canvas, scrollbar):
        """登记 canvas↔scrollbar，并让滚动条在「内容不足一屏」时自动隐藏。

        **为什么需要**：canvas 的滚动条常驻时，即使内容只有两三行也会显示一条（且拖不动、
        滚轮也没反应），看起来像界面出了问题。惯例做法是内容放不下才显示。

        ``yscrollcommand`` 必须走 ``_on_canvas_scrolled``：它先让 Tk 更新滑块位置
        （``scrollbar.set``），再判断是否需要隐藏。顺序不能反 —— 隐藏/显示会改变
        canvas 宽度，进而可能触发重排，必须放在位置更新之后。

        隐藏用 ``pack_forget`` 而不是 ``grid_remove``：三处滚动条都是用 pack 放进
        viewport 的，重新显示时要插回 canvas 之后（用 ``after=canvas`` 保证仍在右侧）。
        """
        canvas.configure(yscrollcommand=lambda first, last: self._on_canvas_scrolled(
            canvas, scrollbar, first, last))
        # 尺寸变化与被映射时都要重算（<Map> 覆盖"切到该标签页/窗口首次显示"）
        canvas.bind("<Configure>", lambda _e, c=canvas: self._sync_scrollbar(c), add="+")
        canvas.bind("<Map>", lambda _e, c=canvas: self._sync_scrollbar(c), add="+")
        scrollbar.bind("<Configure>", lambda _e, c=canvas: self._sync_scrollbar(c), add="+")

    def _canvas_can_scroll(self, canvas) -> bool:
        """内容是否超出可视高度（超出才需要滚动条/滚轮）。"""
        try:
            region = canvas.bbox("all")
            if not region:
                return False
            return (region[3] - region[1]) > canvas.winfo_height()
        except Exception:
            return False

    def _sync_scrollbar(self, canvas):
        """按「内容是否放得下」显示或隐藏该 canvas 的滚动条。

        判据用 ``winfo_manager()`` 而不是 ``winfo_ismapped()``：后者在窗口**未映射**时
        恒为假（例如窗口最小化、或在 Notebook 的非当前页里），会让"隐藏"分支永远不执行，
        滚动条就一直留着 —— 这正是"内容没占满也有滚动条"的原因之一。
        ``winfo_manager()`` 直接反映当前由哪个几何管理器管理（``""`` 表示已 forget），
        与是否可见无关。
        """
        scrollbar = self._scrollbars.get(str(canvas))
        if scrollbar is None or not scrollbar.winfo_exists():
            return
        packed = bool(scrollbar.winfo_manager())
        if self._canvas_can_scroll(canvas):
            if not packed:
                scrollbar.pack(side="right", fill="y", after=canvas)
        else:
            if packed:
                scrollbar.pack_forget()

    def _on_canvas_scrolled(self, canvas, scrollbar, first, last):
        """canvas 的 yscrollcommand：先更新滑块，再决定滚动条是否显示。"""
        try:
            scrollbar.set(first, last)
        except Exception:
            return
        self._sync_scrollbar(canvas)

    def _create_scrollable_editor_section(self, parent, title: str, height: int = 230):
        section = ttk.LabelFrame(parent, text=title, padding=10)
        viewport = ttk.Frame(section)
        viewport.pack(fill="both", expand=True)
        canvas = tk.Canvas(viewport, height=height, highlightthickness=0, bg=self.colors["panel"])
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview)
        rows = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=rows, anchor="nw")
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._scrollbars[str(canvas)] = scrollbar
        self._setup_scrollbar(canvas, scrollbar)
        # scrollregion 变化后也要重算滚动条可见性（内容增删就走这条路径）
        rows.bind("<Configure>", lambda _event: (
            canvas.configure(scrollregion=canvas.bbox("all")), self._sync_scrollbar(canvas)))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self._editor_canvases.append(canvas)
        # 登记 rows→canvas 映射，供新增行后重绑滚轮（_canvas_of）
        self._scroll_canvases[str(rows)] = canvas
        # 滚轮绑定交给 _bind_wheel（它会覆盖整个子树，且新建行后需再次调用）。
        # 这里先绑一次，覆盖"打开编辑器时还没有任何行"的情况。
        self._bind_wheel(canvas, rows)
        return section, rows, viewport

    def _wrap_label(self, label, margin: int = 8):
        """让一个多行 Label 的换行宽度跟随父容器实际宽度（而不是写死 wraplength）。

        **为什么需要**：Tk 的 ``wraplength`` 单位是像素，写死会带来两个问题 ——
          1. 高 DPI 下偏窄，文字比可用宽度更早断行，出现莫名其妙的换行位置；
          2. 窗口拉宽后文字仍按旧宽度断行，右边留一大片空白。
        改为在父容器 ``<Configure>`` 时把 ``wraplength`` 设为「父容器宽度 - margin」，
        文字就能始终铺满可用宽度，且随窗口缩放自适应。

        ``margin`` 用来给父容器的内边距留出余量，避免贴边。

        注意：调用方需要让该 Label ``fill="x"``，否则 Label 自身宽度不会随之增长。
        """
        parent = label.master

        def resize(event=None):
            try:
                width = (event.width if event is not None else parent.winfo_width())
            except Exception:
                return
            if width > 1:
                label.configure(wraplength=max(width - margin, 80))

        parent.bind("<Configure>", resize, add="+")
        # 父容器尺寸确定前先给一个初始值，避免首帧出现超长单行
        resize()
        return label

    def _canvas_of(self, container):
        """找出承载 ``container`` 的那个可滚动 canvas（没有则返回 None）。

        编辑器里的行容器（prefix_rows_frame 等）是 canvas.create_window 放进 canvas 的，
        两者之间没有父子关系（Tk 里 window item 不在 canvas 的控件树中），所以没法直接
        取到。这里按 create_window 登记的映射查，供"新增行后重绑滚轮"使用。
        """
        return self._scroll_canvases.get(str(container))

    def _bind_wheel(self, canvas, container):
        """让鼠标滚轮在整个可滚动区域内生效（含所有子控件，且适配动态新增的行）。

        **为什么不能只绑 canvas**：Tk 的鼠标事件只发给**光标下的那个控件**，不会冒泡到
        父级。原来只写了 ``canvas.bind("<MouseWheel>", ...)``，于是只有把鼠标精确移到
        canvas 自身的空白处才能滚，移到里面的标签/输入框上就完全没反应 —— 表现为
        "滚轮失效，只能拖滚动条"。

        做法：遍历 ``container`` 的整棵控件树，逐个绑定滚轮，统一驱动该 canvas 的
        yview_scroll。新建行之后需要再调用一次（见 _add_prefix_row 等）。

        幂等：同一个控件只绑一次（用 ``_wheel_bound`` 记录），避免重复调用把回调叠加
        多次 —— 那会让一次滚轮滚动多倍距离。

        设计要点（都来自实测，不要"简化"）：

        * **Tk 事件不冒泡**。鼠标事件只发给光标下的那个控件，所以必须在整个子树上逐个
          绑定；也正因如此，"滚不动就让事件传给父级"是不成立的 —— 事件会直接消失。
          因此文本控件滚不动时必须**显式调用**外层 canvas 的滚动，而不是 return None。
        * ``tk.Text`` 优先滚自己（编辑器里的多行输入框），滚到头再滚外层。
        * ``ttk.Combobox`` 的滚轮用于切换选项，交给它自己，不接管。
        * ``state="disabled"`` 的 ``tk.Text``（例如排行榜里的 Top 5 只读框）纵向本来
          就不滚，应当把滚轮完全让给外层 —— 这也是"鼠标放进文字框滚轮就失效"的根因。
        * **外层滚不动时不接管滚轮**（返回 ``None``）：内容不足一屏时滚动条已隐藏，
          此时继续"吃掉"滚轮事件会让人以为界面卡住；交给更外层（若有）更自然。
        """
        def scroll_units(event):
            """把 Windows 的 wheel delta 换算成滚动行数（delta=120 即一行）。"""
            steps = int(event.delta / 120)
            return -steps if steps else (-1 if event.delta < 0 else 1)

        def scroll_outer(event):
            """滚动外层 canvas；滚不动就不接管，让事件继续传给更外层。"""
            if not self._canvas_can_scroll(canvas):
                return None
            canvas.yview_scroll(scroll_units(event), "units")
            return "break"

        def on_wheel(event):
            return scroll_outer(event)

        def on_text_wheel(event, text):
            """文本控件优先滚自己；滚不动则显式滚外层。

            disabled 的 Text 直接跳到自己那步（它纵向不可滚，滚它会报错或被忽略），
            让滚轮完全用于外层列表。
            """
            if str(text.cget("state")) != "disabled":
                first, last = text.yview()
                can_scroll = (event.delta < 0 and last < 1.0) or (event.delta > 0 and first > 0.0)
                if can_scroll:
                    text.yview_scroll(scroll_units(event), "units")
                    return "break"
            # 自己滚不动（或本来不可滚）→ 显式交给外层
            return scroll_outer(event)

        bound = self._wheel_bound.setdefault(str(canvas), set())

        def walk(widget):
            key = str(widget)
            if key not in bound:
                bound.add(key)
                if isinstance(widget, ttk.Combobox):
                    pass
                elif isinstance(widget, tk.Text):
                    widget.bind("<MouseWheel>", lambda event, text=widget: on_text_wheel(event, text), add="+")
                else:
                    widget.bind("<MouseWheel>", on_wheel, add="+")
            for child in widget.winfo_children():
                walk(child)

        walk(container)
        # 区域本身也要能滚（鼠标停在 padding 空白上时）
        canvas.bind("<MouseWheel>", on_wheel, add="+")

    def _checkbutton(self, parent, text: str, variable):
        """Use the native check indicator; clam renders a selected state as a cross."""
        checkbox = tk.Checkbutton(parent, text=text, variable=variable, anchor="w", relief="flat", borderwidth=0,
                                  highlightthickness=0, takefocus=True)
        self._theme_checkbuttons.append(checkbox)
        checkbox.configure(
            background=self.colors["bg"], foreground=self.colors["text"],
            activebackground=self.colors["bg"], activeforeground=self.colors["text"],
            selectcolor=self.colors["field"], font=(UI_FONT, 11),
        )
        return checkbox

    def _bind_row_drag(self, row, kind: str):
        frame = row["frame"]
        widgets = [frame]
        while widgets:
            widget = widgets.pop()
            if isinstance(widget, ttk.Entry):
                continue
            widgets.extend(widget.winfo_children())
            if not isinstance(widget, ttk.Button):
                widget.configure(cursor="fleur")
            widget.bind("<ButtonPress-1>", lambda event: self._start_row_drag(event, row, kind), add="+")
            widget.bind("<B1-Motion>", lambda event: self._drag_row(event, kind), add="+")
            widget.bind("<ButtonRelease-1>", lambda event: self._finish_row_drag(event, kind), add="+")

    def _start_row_drag(self, event, row, kind: str):
        self._drag_state = {"row": row, "kind": kind, "origin": (event.x_root, event.y_root), "moved": False}

    def _drag_row(self, event, kind: str):
        state = getattr(self, "_drag_state", None)
        if not state or state["kind"] != kind:
            return
        if not state["moved"]:
            x, y = state["origin"]
            if max(abs(event.x_root - x), abs(event.y_root - y)) < 5:
                return
            state["moved"] = True
        rows = self.prefix_rows if kind == "prefix" else self.suffix_rows if kind == "suffix" else self.pair_rows
        row = state["row"]
        if row not in rows:
            return
        target_index = len(rows)
        for index, item in enumerate(rows):
            midpoint = item["frame"].winfo_rooty() + item["frame"].winfo_height() / 2
            if event.y_root < midpoint:
                target_index = index
                break
        source_index = rows.index(row)
        if target_index > source_index:
            target_index -= 1
        if target_index == source_index:
            return "break"
        rows.pop(source_index)
        rows.insert(target_index, row)
        for item in rows:
            item["frame"].pack_forget()
        for item in rows:
            item["frame"].pack(fill="x", pady=3)
        if kind == "prefix":
            self._reindex_prefix_rows()
        elif kind == "suffix":
            self._reindex_suffix_rows()
        else:
            self._reindex_pair_rows()
        return "break"

    def _finish_row_drag(self, event, kind: str):
        state = getattr(self, "_drag_state", None)
        if state and state["kind"] == kind:
            self._drag_state = None
            if state["moved"]:
                # Suppress the button's click action after a reorder gesture.
                if isinstance(event.widget, ttk.Button):
                    event.widget.state(["!pressed"])
                return "break"

    def _reindex_prefix_rows(self):
        for index, row in enumerate(self.prefix_rows, start=1):
            row["index"].configure(text=str(index))

    def _reindex_suffix_rows(self):
        for index, row in enumerate(self.suffix_rows, start=1):
            row["index"].configure(text=str(index))

    def _add_prefix_row(self, name="+", start=0, end=-1):
        row_frame = ttk.Frame(self.prefix_rows_frame)
        row_frame.pack(fill="x", pady=3)
        drag = ttk.Label(row_frame, text="≡", width=3, anchor="center", cursor="fleur")
        drag.grid(row=0, column=0, padx=(0, 4))
        index = ttk.Label(row_frame, width=3, anchor="e")
        index.grid(row=0, column=1, padx=(0, 8))
        name_var = tk.StringVar(value="" if name == "+" else str(name))
        start_var = tk.StringVar(value=str(start))
        end_var = tk.StringVar(value=str(end))
        index_entry = ttk.Entry(row_frame, textvariable=name_var, width=12)
        range_bar = ttk.Frame(row_frame)
        range_bar.grid(row=1, column=2, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(range_bar, text="开始").grid(row=0, column=0, padx=(0, 4))
        start_entry = ttk.Entry(range_bar, textvariable=start_var, width=8)
        ttk.Label(range_bar, text="结束").grid(row=0, column=2, padx=(6, 4))
        end_entry = ttk.Entry(range_bar, textvariable=end_var, width=8)
        index_entry.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        start_entry.grid(row=0, column=1, sticky="ew")
        end_entry.grid(row=0, column=3, sticky="ew")
        range_bar.columnconfigure(1, weight=1)
        range_bar.columnconfigure(3, weight=1)
        row = {"frame": row_frame, "index": index, "name": name_var, "start": start_var, "end": end_var,
               "range_widgets": (start_entry, end_entry)}
        ttk.Button(row_frame, text="删除", command=lambda: self._remove_prefix_row(row)).grid(row=0, column=3)
        self._bind_row_drag(row, "prefix")
        row_frame.columnconfigure(2, weight=1)
        self.prefix_rows.append(row)
        self._reindex_prefix_rows()
        self._refresh_prefix_range_state()
        # 新行也要能滚（_bind_wheel 幂等，只绑新控件）
        self._bind_wheel(self._canvas_of(self.prefix_rows_frame), self.prefix_rows_frame)

    def _remove_prefix_row(self, row):
        self.prefix_rows.remove(row)
        row["frame"].destroy()
        self._reindex_prefix_rows()

    def _add_suffix_row(self, name="+"):
        row_frame = ttk.Frame(self.suffix_rows_frame)
        row_frame.pack(fill="x", pady=3)
        drag = ttk.Label(row_frame, text="≡", width=4, anchor="center", cursor="fleur")
        drag.grid(row=0, column=0, padx=(0, 4))
        index = ttk.Label(row_frame, width=4, anchor="e")
        index.grid(row=0, column=1, padx=(0, 8))
        name_var = tk.StringVar(value="" if name == "+" else str(name))
        ttk.Entry(row_frame, textvariable=name_var, width=12).grid(row=0, column=2, sticky="ew", padx=(0, 8))
        row = {"frame": row_frame, "index": index, "name": name_var}
        ttk.Button(row_frame, text="删除", command=lambda: self._remove_suffix_row(row)).grid(row=0, column=3)
        self._bind_row_drag(row, "suffix")
        row_frame.columnconfigure(2, weight=1)
        self.suffix_rows.append(row)
        self._reindex_suffix_rows()
        # 新行也要能滚（_bind_wheel 幂等，只绑新控件）
        self._bind_wheel(self._canvas_of(self.suffix_rows_frame), self.suffix_rows_frame)

    def _remove_suffix_row(self, row):
        self.suffix_rows.remove(row)
        row["frame"].destroy()
        self._reindex_suffix_rows()

    def _affix_editor_data(self):
        """从当前前后缀编辑视图读回 (prefixes, suffixes)，供切换视图与写配置共用。

        一一对应视图有个关键约束（有单测守护）：**尾部缺失的条目保持缺失，中间的空洞
        必须保留其下标**。所以这里用「最后一个非空行的下标 + 1」求长度，而不是取
        非空计数 —— 否则中间留空会把后面的前缀整体前移，改变与后缀的配对关系。
        "+" 表示空串（README.md 有说明）。
        """
        if self.affix_view == "一一对应编辑":
            # Missing trailing entries stay absent; interior gaps must keep their index.
            prefix_count = max((i + 1 for i, row in enumerate(self.pair_rows)
                                if row["prefix_exists"] or row["prefix"].get()), default=0)
            suffix_count = max((i + 1 for i, row in enumerate(self.pair_rows)
                                if row["suffix_exists"] or row["suffix"].get()), default=0)
            prefixes = [(row["prefix"].get() or "+", row["start"].get(), row["end"].get())
                        for row in self.pair_rows[:prefix_count]]
            suffixes = [row["suffix"].get() or "+" for row in self.pair_rows[:suffix_count]]
        else:
            prefixes = [(row["name"].get() or "+", row["start"].get(), row["end"].get())
                        for row in self.prefix_rows]
            suffixes = [row["name"].get() or "+" for row in self.suffix_rows]
        return prefixes, suffixes

    def _switch_affix_view(self, _event=None):
        """在「分开编辑」与「一一对应编辑」两个视图之间切换，并搬运已填内容。

        切换是有损的：先把当前视图读成 (prefixes, suffixes)，再按新视图重建行。
        一一对应视图的行数取前后缀的**较大值**，较短的一侧以 None 补位（显示为"缺x"），
        这样用户能直观看到哪几条没有配对项，而不是被静默丢弃。
        """
        selected = self.affix_view_var.get()
        if selected == self.affix_view:
            return
        prefixes, suffixes = self._affix_editor_data()
        if selected == "一一对应编辑":
            for row in self.pair_rows:
                row["frame"].destroy()
            self.pair_rows.clear()
            for i in range(max(len(prefixes), len(suffixes))):
                self._add_pair_row(prefixes[i] if i < len(prefixes) else None,
                                   suffixes[i] if i < len(suffixes) else None)
            self.separate_affix_sections.pack_forget()
            self.pair_section.pack(fill="both", expand=True)
        else:
            for row in self.prefix_rows + self.suffix_rows:
                row["frame"].destroy()
            self.prefix_rows.clear()
            self.suffix_rows.clear()
            for name, start, end in prefixes:
                self._add_prefix_row(name, start, end)
            for name in suffixes:
                self._add_suffix_row(name)
            self.pair_section.pack_forget()
            self.separate_affix_sections.pack(fill="both", expand=True)
        self.affix_view = selected
        self._refresh_prefix_range_state()
        # 切换视图会重建全部行，新控件需要补绑滚轮（_bind_wheel 幂等）
        for rows_frame in (self.prefix_rows_frame, self.suffix_rows_frame, self.pair_rows_frame):
            canvas = self._canvas_of(rows_frame)
            if canvas is not None:
                self._bind_wheel(canvas, rows_frame)

    def _add_pair_row(self, prefix=("+", "0", "-1"), suffix="+"):
        """在一一对应视图里加一行「前缀 + 区间 + 后缀」组合。

        prefix/suffix 传 None 表示**这一侧本来就没有条目**（区别于空串 ""）：
        prefix_exists / suffix_exists 记录这个区别，_affix_editor_data() 依赖它判断
        尾部缺失，界面上也据此显示"缺前缀"/"缺后缀"提示。
        区间默认 ("+", "0", "-1")，即空名 + 全范围，与配置文件默认值一致。
        """
        frame = ttk.Frame(self.pair_rows_frame)
        frame.pack(fill="x", pady=3)
        ttk.Label(frame, text="≡", width=3, anchor="center").grid(row=0, column=0)
        index = ttk.Label(frame, width=3, anchor="e")
        index.grid(row=0, column=1, padx=(0, 6))
        name, start, end = prefix if prefix is not None else ("+", "0", "-1")
        row = {"frame": frame, "index": index, "prefix_exists": prefix is not None,
               "suffix_exists": suffix is not None,
               "prefix": tk.StringVar(value="" if name == "+" else name),
               "suffix": tk.StringVar(value="" if suffix in (None, "+") else suffix),
               "start": tk.StringVar(value=start), "end": tk.StringVar(value=end)}
        for column, key in ((2, "prefix"), (3, "suffix")):
            ttk.Entry(frame, textvariable=row[key], width=18).grid(row=0, column=column, sticky="ew", padx=(0, 6))
        range_widgets = []
        for column, key in ((4, "start"), (5, "end")):
            entry = ttk.Entry(frame, textvariable=row[key], width=10)
            entry.grid(row=0, column=column, sticky="ew", padx=(0, 6))
            range_widgets.append(entry)
        row["range_widgets"] = range_widgets
        note = ttk.Label(frame, width=6)
        note.grid(row=0, column=6)
        def update_note(*_args):
            missing_prefix = not row["prefix_exists"] and not row["prefix"].get()
            missing_suffix = not row["suffix_exists"] and not row["suffix"].get()
            note.configure(text="缺前缀" if missing_prefix else "缺后缀" if missing_suffix else "")
        row["prefix"].trace_add("write", update_note)
        row["suffix"].trace_add("write", update_note)
        update_note()
        ttk.Button(frame, text="删除", command=lambda: self._remove_pair_row(row)).grid(row=0, column=7)
        frame.columnconfigure(2, weight=1)
        frame.columnconfigure(3, weight=1)
        self._bind_row_drag(row, "pair")
        self.pair_rows.append(row)
        self._reindex_pair_rows()
        self._refresh_prefix_range_state()
        # 新行也要能滚（_bind_wheel 幂等，只绑新控件）
        self._bind_wheel(self._canvas_of(self.pair_rows_frame), self.pair_rows_frame)

    def _reindex_pair_rows(self):
        for index, row in enumerate(self.pair_rows, start=1):
            row["index"].configure(text=str(index))

    def _remove_pair_row(self, row):
        self.pair_rows.remove(row)
        row["frame"].destroy()
        self._reindex_pair_rows()

    def _apply_prefix_range_batch(self):
        start = self.prefix_batch_start_var.get().strip()
        end = self.prefix_batch_end_var.get().strip()
        for row in self.pair_rows if self.affix_view == "一一对应编辑" else self.prefix_rows:
            row["start"].set(start)
            row["end"].set(end)

    def _refresh_prefix_range_state(self, _event=None):
        is_sequential = self.editor_choice_maps["enumeration.mode"][self.editor_vars["enumeration.mode"].get()] == 1
        for widget in self.global_range_widgets:
            widget.state(["disabled"] if is_sequential else ["!disabled"])
        for widget in self.prefix_batch_widgets:
            widget.state(["!disabled"] if is_sequential else ["disabled"])
        for row in self.prefix_rows + self.pair_rows:
            for widget in row["range_widgets"]:
                widget.state(["!disabled"] if is_sequential else ["disabled"])
        self.global_range_section.configure(text="全局区间（随机模式使用）" if not is_sequential else "全局区间（顺序模式不使用）")

    def _open_editor(self):
        """打开配置编辑器窗口（同一个实例只开一个，重复点击只是置顶）。

        打开时先 load_config + _validate_config 并把结果深拷进 self.editor_cfg：
        编辑期内所有改动都写在副本与 editor_vars 上，只有点保存才落盘，
        所以直接关窗不会污染磁盘上的配置文件。
        各输入控件把值写进 self.editor_vars（键名即 config_schema 的 config_path），
        _editor_config() 再按这些键回填成完整配置。
        """
        if self.editor and self.editor.winfo_exists():
            self.editor.lift()
            return
        try:
            cfg = load_config(self.config_var.get())
            _validate_config(cfg)
        except Exception as exc:
            messagebox.showerror("无法导入配置", str(exc), parent=self)
            return
        self.editor_cfg = copy.deepcopy(cfg)
        editor = tk.Toplevel(self)
        self.editor = editor
        editor.title("PBB 配置编辑")
        if os.path.exists(ICON_FILE):
            editor.iconbitmap(ICON_FILE)
        if self._icon_image is not None:
            editor.iconphoto(True, self._icon_image)
        editor.after_idle(lambda: apply_windows_window_icon(editor))
        editor.geometry(scaled_geometry(980, 820))
        editor.minsize(*_scaled_min_size(820, 660))
        editor.configure(bg=self.colors["bg"])
        editor.transient(self)
        editor.protocol("WM_DELETE_WINDOW", self._close_editor)

        top = ttk.Frame(editor, padding=(22, 18))
        top.pack(fill="x")
        ttk.Label(top, text="配置编辑", font=(UI_FONT, 18, "bold")).pack(side="left")
        actions = ttk.Frame(top)
        actions.pack(side="right")
        ttk.Button(actions, text="导入", command=lambda: self._choose_config(edit=True)).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(actions, text="保存到当前文件", command=self._save_editor).pack(side="left")
        ttk.Button(actions, text="保存到 config.yaml", command=lambda: self._save_editor(to_default=True)).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="保存并启动", command=lambda: self._save_editor(start=True)).pack(
            side="left", padx=(8, 0)
        )
        recommendation = ttk.Frame(editor, padding=(22, 0, 22, 10))
        recommendation.pack(fill="x")
        self._wrap_label(ttk.Label(
            recommendation, text="推荐运行外层 tune_full.bat 自动优化（推荐 20 分钟）。测试后必须再 Rebuild 一遍，才能应用测试选择。",
            style="LaunchNotice.TLabel", justify="left")).pack(anchor="w", fill="x")
        notebook = ttk.Notebook(editor)
        notebook.pack(fill="both", expand=True, padx=22, pady=(0, 12))
        basic = ttk.Frame(notebook, padding=18)
        affixes = ttk.Frame(notebook, padding=18)
        scores = ttk.Frame(notebook, padding=18)
        output = ttk.Frame(notebook, padding=18)
        notebook.add(basic, text="基础")
        notebook.add(affixes, text="前后缀")
        notebook.add(scores, text="评分")
        notebook.add(output, text="输出")
        # 切页后要重算滚动条：Tk 不会映射非当前页里的控件（winfo_height() 为 1），
        # 仅靠 <Configure> 不足以保证切回来时状态正确。
        notebook.bind("<<NotebookTabChanged>>", self._sync_all_scrollbars)
        self.editor_vars = {}
        self.editor_choice_maps = {}
        self._editor_canvases = []
        # 上一次编辑器窗口的 canvas/控件已随窗口销毁，相关映射必须清空，否则会持有
        # 失效控件引用。但主窗口排行榜的登记要保留 —— 它仍然活着。
        score_id = str(getattr(self, "score_canvas", ""))
        self._scroll_canvases = {k: v for k, v in self._scroll_canvases.items() if k == score_id}
        self._scrollbars = {k: v for k, v in self._scrollbars.items() if k == score_id}
        self._wheel_bound = {k: v for k, v in self._wheel_bound.items() if k == score_id}
        self.prefix_rows = []
        self.suffix_rows = []
        self.pair_rows = []
        self.affix_view = "分开编辑"
        self.affix_view_var = tk.StringVar(value=self.affix_view)

        def field(parent, row, label, key, value, column=0, width=26):
            ttk.Label(parent, text=label).grid(row=row, column=column * 2, sticky="w", padx=(0, 8), pady=6)
            var = tk.StringVar(value=str(value))
            self.editor_vars[key] = var
            entry = ttk.Entry(parent, textvariable=var, width=width)
            entry.grid(row=row, column=column * 2 + 1, sticky="ew", pady=6)
            return entry

        def choice(parent, row, label, key, value, options, column=0, width=26):
            ttk.Label(parent, text=label).grid(row=row, column=column * 2, sticky="w", padx=(0, 8), pady=6)
            selected = next((name for name, code in options.items() if code == value), next(iter(options)))
            var = tk.StringVar(value=selected)
            self.editor_vars[key] = var
            self.editor_choice_maps[key] = options
            box = ttk.Combobox(parent, textvariable=var, values=tuple(options), state="readonly", width=width)
            box.grid(row=row, column=column * 2 + 1, sticky="ew", pady=6)
            return box

        cs = cfg["character_set"]
        rng = cfg["enumeration"].get("ranges", [{}])[0]
        basic_fields = (
            (0, "战队", "team_name", cfg.get("team_name", "")),
            (1, "线程", "threads.worker_threads", self._cfg_value(cfg, "threads.worker_threads", -1)),
            (3, "变量长度", "enumeration.variable_length", self._cfg_value(cfg, "enumeration.variable_length", 1)),
        )
        for row, label, key, value in basic_fields:
            field(basic, row, label, key, value)
        mode_box = choice(basic, 2, "枚举模式", "enumeration.mode", self._cfg_value(cfg, "enumeration.mode", 2), ENUMERATION_MODES)
        choice(basic, 5, "单字符字节数", "character_set.single_char_length", cs.get("single_char_length", 1), {str(value): value for value in range(1, 9)})
        type_row = 6
        ttk.Label(basic, text="字符类型").grid(row=type_row, column=0, sticky="w", padx=(0, 8), pady=6)
        self.charset_types_frame = ttk.Frame(basic)
        self.charset_types_frame.grid(row=type_row, column=1, sticky="ew", pady=6)
        self.charset_type_vars = {}
        self.charset_type_order = list(cs.get("types", [1]))
        self.charset_width = int(cs.get("single_char_length", 1))
        self.charset_selections = {self.charset_width:self.charset_type_order.copy()}
        self.charset_custom_frame = ttk.Frame(basic)
        self.charset_custom_frame.grid(row=7,column=0,columnspan=2,sticky="ew")
        self.charset_custom_original = copy.deepcopy(cs.get("custom_values", ""))
        custom_text = (json.dumps(self.charset_custom_original,ensure_ascii=False)
                       if isinstance(self.charset_custom_original,list) else str(self.charset_custom_original))
        self.charset_custom_initial = custom_text
        field(self.charset_custom_frame,0,"自定义字符 / JSON 字节列表","character_set.custom_values",custom_text,width=40)
        self.charset_custom_frame.columnconfigure(1,weight=1)
        self.charset_unicode_frame = ttk.Frame(basic)
        self.charset_unicode_frame.grid(row=8,column=0,columnspan=2,sticky="ew",pady=6)
        ttk.Label(self.charset_unicode_frame,text="Unicode 区间（每行一段，如 0x4E00-0x9FFF）").pack(anchor="w")
        self.charset_unicode_text = tk.Text(self.charset_unicode_frame,height=3,width=44)
        self.charset_unicode_text.pack(fill="x")
        self.charset_unicode_text.insert('1.0','\n'.join(f"0x{r['start']:X}-0x{r['end']:X}" for r in cs.get('unicode_ranges',[])))
        byte_box = next(widget for widget in basic.grid_slaves(row=5, column=1) if isinstance(widget, ttk.Combobox))
        byte_box.bind("<<ComboboxSelected>>", self._refresh_charset_types)
        self._refresh_charset_types()
        basic.columnconfigure(1, weight=1)

        affix_toolbar = ttk.Frame(affixes)
        affix_toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(affix_toolbar, text="枚举 / 匹配模式").pack(side="left", padx=(0, 8))
        affix_mode_box = ttk.Combobox(
            affix_toolbar, textvariable=self.editor_vars["enumeration.mode"],
            values=tuple(ENUMERATION_MODES), state="readonly", width=20,
        )
        affix_mode_box.pack(side="left")
        ttk.Label(affix_toolbar, text="编辑视图").pack(side="left", padx=(18, 8))
        view_box = ttk.Combobox(affix_toolbar, textvariable=self.affix_view_var,
                               values=("分开编辑", "一一对应编辑"), state="readonly", width=18)
        view_box.pack(side="left")
        view_box.bind("<<ComboboxSelected>>", self._switch_affix_view)
        self.global_range_section = ttk.LabelFrame(affixes, padding=10)
        self.global_range_section.pack(fill="x", pady=(0, 12))
        global_start = field(self.global_range_section, 0, "范围开始", "range.start", rng.get("start", 0), width=18)
        global_end = field(self.global_range_section, 0, "范围结束", "range.end", rng.get("end", -1), column=1, width=18)
        self.global_range_widgets = (global_start, global_end)

        self.separate_affix_sections = ttk.Frame(affixes)
        self.separate_affix_sections.pack(fill="both", expand=True)
        self.separate_affix_sections.columnconfigure(0, weight=1, uniform="affixes")
        self.separate_affix_sections.columnconfigure(1, weight=1, uniform="affixes")
        self.separate_affix_sections.rowconfigure(0, weight=1)
        prefix_section, self.prefix_rows_frame, prefix_viewport = self._create_scrollable_editor_section(self.separate_affix_sections, "前缀", height=420)
        self.prefix_section = prefix_section
        prefix_section.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        prefix_toolbar = ttk.Frame(prefix_section)
        prefix_toolbar.pack(fill="x", pady=(0, 8), before=prefix_viewport)
        ttk.Button(prefix_toolbar, text="新增前缀", command=self._add_prefix_row).pack(anchor="w")
        batch_bar = ttk.Frame(prefix_toolbar)
        batch_bar.pack(fill="x", pady=(8, 0))
        ttk.Label(batch_bar, text="批量区间").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.prefix_batch_start_var = tk.StringVar(value="0")
        self.prefix_batch_end_var = tk.StringVar(value="-1")
        batch_start = ttk.Entry(batch_bar, textvariable=self.prefix_batch_start_var, width=8)
        batch_end = ttk.Entry(batch_bar, textvariable=self.prefix_batch_end_var, width=8)
        batch_start.grid(row=1, column=0, sticky="ew")
        ttk.Label(batch_bar, text="至").grid(row=1, column=1, padx=4)
        batch_end.grid(row=1, column=2, sticky="ew")
        batch_apply = ttk.Button(batch_bar, text="应用全部", command=self._apply_prefix_range_batch)
        batch_apply.grid(row=1, column=3, padx=(6, 0))
        batch_bar.columnconfigure(0, weight=1)
        batch_bar.columnconfigure(2, weight=1)
        self.prefix_batch_widgets = (batch_start, batch_end, batch_apply)
        prefix_headers = ttk.Frame(self.prefix_rows_frame)
        prefix_headers.pack(fill="x", pady=(0, 3))
        for column, (label, width) in enumerate((("拖动", 3), ("#", 3), ("前缀", 12))):
            ttk.Label(prefix_headers, text=label, width=width).grid(row=0, column=column, padx=(0, 8), sticky="w")
        prefix_headers.columnconfigure(2, weight=1)
        prefix_ranges = self._prefix_ranges_for_editor(cfg["enumeration"], len(cfg.get("prefixes", [])))
        for index, item in enumerate(cfg.get("prefixes", [])):
            start, end = prefix_ranges[index]
            self._add_prefix_row(item.get("name", "+"), start, end)

        suffix_section, self.suffix_rows_frame, suffix_viewport = self._create_scrollable_editor_section(self.separate_affix_sections, "后缀", height=420)
        self.suffix_section = suffix_section
        suffix_section.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        suffix_toolbar = ttk.Frame(suffix_section)
        suffix_toolbar.pack(fill="x", pady=(0, 8), before=suffix_viewport)
        ttk.Button(suffix_toolbar, text="新增后缀", command=self._add_suffix_row).pack(side="left")
        suffix_headers = ttk.Frame(self.suffix_rows_frame)
        suffix_headers.pack(fill="x", pady=(0, 3))
        ttk.Label(suffix_headers, text="拖动", width=4).grid(row=0, column=0, padx=(0, 8), sticky="w")
        ttk.Label(suffix_headers, text="#", width=4).grid(row=0, column=1, padx=(0, 8), sticky="w")
        ttk.Label(suffix_headers, text="后缀", width=12).grid(row=0, column=2, padx=(0, 8), sticky="w")
        suffix_headers.columnconfigure(2, weight=1)
        for item in cfg.get("suffixes", []):
            self._add_suffix_row(item.get("name", "+"))
        self.pair_section, self.pair_rows_frame, pair_viewport = self._create_scrollable_editor_section(
            affixes, "前后缀条目", height=420,
        )
        pair_toolbar = ttk.Frame(self.pair_section)
        pair_toolbar.pack(fill="x", pady=(0, 8), before=pair_viewport)
        ttk.Button(pair_toolbar, text="新增配对", command=self._add_pair_row).pack(side="left")
        ttk.Label(pair_toolbar, text="批量区间").pack(side="left", padx=(18, 6))
        for variable in (self.prefix_batch_start_var, self.prefix_batch_end_var):
            entry = ttk.Entry(pair_toolbar, textvariable=variable, width=10)
            entry.pack(side="left", padx=(0, 6))
            self.prefix_batch_widgets += (entry,)
        pair_apply = ttk.Button(pair_toolbar, text="应用到全部前缀", command=self._apply_prefix_range_batch)
        pair_apply.pack(side="left")
        self.prefix_batch_widgets += (pair_apply,)
        pair_headers = ttk.Frame(self.pair_rows_frame)
        pair_headers.pack(fill="x", pady=(0, 3))
        for column, (label, width) in enumerate((("拖动", 3), ("#", 3), ("前缀", 18), ("后缀", 18),
                                               ("开始", 10), ("结束", 10))):
            ttk.Label(pair_headers, text=label, width=width).grid(row=0, column=column, sticky="w", padx=(0, 6))
        pair_headers.columnconfigure(2, weight=1)
        pair_headers.columnconfigure(3, weight=1)
        mode_box.bind("<<ComboboxSelected>>", self._refresh_prefix_range_state)
        affix_mode_box.bind("<<ComboboxSelected>>", self._refresh_prefix_range_state)
        self._refresh_prefix_range_state()
        if cfg["enumeration"].get("mode") == 4:
            self.affix_view_var.set("一一对应编辑")
            self._switch_affix_view()

        collection = cfg.get("collection", {})
        field(scores, 0, "评分族", "collection.score_list", collection.get("score_list", ""), width=45)
        choice(scores, 1, "评分模式", "collection.score_mode", collection.get("score_mode", "all"), SCORE_MODES)
        choice(scores, 2, "收集模式", "collection.collect_mode", collection.get("collect_mode", 0), COLLECT_MODES)
        for index, family in enumerate(FAMILIES):
            field(scores, 3 + index // 2, f"最低 {family.upper()}", f"collection.min_{family}", collection.get(f"min_{family}", 0), index % 2, 14)
        for index, family in enumerate(("cp", "fp", "wc", "fs", "pj")):
            field(scores, 8 + index // 2, f"PTT {family.upper()}", f"collection.ptt_{family}", collection.get(f"ptt_{family}", 0), index % 2, 14)
        scores.columnconfigure(1, weight=1)
        scores.columnconfigure(3, weight=1)

        out = cfg.get("output", {})
        self._build_result_file_row(output, str(out.get("result_file", "+")))
        choice(output, 1, "输出模式", "output.output_xp", out.get("output_xp", 0), OUTPUT_SCORE_MODES)
        for row, (label, key, default) in enumerate((("记录日志", "output.log_output", 1), ("记录速度", "output.speed_output", 1)), start=2):
            var = tk.BooleanVar(value=bool(out.get(key.split(".")[-1], default)))
            self.editor_vars[key] = var
            self._checkbutton(output, label, var).grid(row=row, column=0, sticky="w", pady=8)


    def _build_result_file_row(self, parent, value: str):
        """在「输出」页建一行「结果文件」选择器：随机名 / 时间戳 / 自定义。

        背景：``output.result_file`` 支持三种写法（见 main._resolve_result_file）——
        ``+`` 随机名、``-`` 时间戳、其他为自定义文件名。以前界面上是一个裸输入框，
        用户看不出 ``+``/``-`` 有特殊含义；更糟的是写成 ``-`` 时**字面量**会被当成文件名，
        结果在 out/ 下产生一个名为 ``-`` 的文件（引擎只认字面量，解析发生在 Python 侧）。

        这里把三种模式做成下拉框，选「自定义」时才显示输入框，避免用户误填符号。
        """
        ttk.Label(parent, text="结果文件").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=6)
        holder = ttk.Frame(parent)
        holder.grid(row=0, column=1, sticky="ew", pady=6)
        holder.columnconfigure(0, weight=1)
        holder.columnconfigure(1, weight=1)

        # 已有取值 -> 模式：+ 随机、- 或空 时间戳、其他 自定义（沿用其自定义名）
        raw = (value or "").strip()
        if raw == "+":
            initial = "随机名"
        elif raw in ("-", ""):
            initial = "时间戳"
        else:
            initial = "自定义"

        mode_var = tk.StringVar(value=initial)
        self.editor_vars["output.result_file.mode"] = mode_var
        self.editor_choice_maps["output.result_file.mode"] = RESULT_FILE_MODES
        box = ttk.Combobox(holder, textvariable=mode_var, values=tuple(RESULT_FILE_MODES),
                           state="readonly", width=12)
        box.grid(row=0, column=0, sticky="ew")

        name_var = tk.StringVar(value="" if raw in ("+", "-") else raw)
        self.editor_vars["output.result_file.name"] = name_var
        entry = ttk.Entry(holder, textvariable=name_var, width=30)
        entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        def sync_mode(*_args):
            """只有「自定义」才显示文件名输入框；其余模式隐藏并清空该框。

            清空是为了让保存逻辑只依赖模式（见 _editor_config）：避免用户先填了名字、
            又切回随机名之后，那个残留名字被误当作生效值。
            """
            if mode_var.get() == "自定义":
                entry.grid()
            else:
                entry.grid_remove()
                name_var.set("")
        mode_var.trace_add("write", sync_mode)
        sync_mode()
        return box

    def _selected_charset_types(self):
        selected={code for code,var in self.charset_type_vars.items() if var.get()}
        # Preserve imported order and duplicate entries: charset order affects enumeration.
        return [code for code in self.charset_type_order if code in selected]+[
            code for code in self.charset_type_vars if code in selected and code not in self.charset_type_order]

    def _refresh_charset_types(self, _event=None):
        if self.charset_type_vars:
            self.charset_selections[self.charset_width]=self._selected_charset_types()
        scl=int(self.editor_vars['character_set.single_char_length'].get())
        options=CHARSET_TYPES.get(scl, {'自定义字符':1,'Unicode 区间':2})
        self.charset_width=scl
        self.charset_type_order=list(self.charset_selections.get(scl,[next(iter(options.values()))]))
        for widget in self.charset_types_frame.winfo_children():widget.destroy()
        self.charset_type_vars={}
        for index,(label,code) in enumerate(options.items()):
            variable=tk.BooleanVar(value=code in self.charset_type_order)
            self.charset_type_vars[code]=variable
            self._checkbutton(self.charset_types_frame,label,variable).grid(row=index//2,column=index%2,sticky='w',padx=(0,12),pady=3)
            variable.trace_add('write',lambda *_:self._refresh_charset_inputs())
        self._refresh_charset_inputs()

    def _refresh_charset_inputs(self):
        options=CHARSET_TYPES.get(self.charset_width, {'自定义字符':1,'Unicode 区间':2})
        selected=set(self._selected_charset_types())
        custom=any(code in selected and label.startswith('自定义') for label,code in options.items())
        unicode=any(code in selected and label=='Unicode 区间' for label,code in options.items())
        self.charset_custom_frame.grid() if custom else self.charset_custom_frame.grid_remove()
        self.charset_unicode_frame.grid() if unicode else self.charset_unicode_frame.grid_remove()

    def _editor_config(self) -> dict:
        """把编辑器界面上的所有输入汇总成一份完整配置（不落盘）。

        以打开编辑器时的副本 self.editor_cfg 为基底（保留 UI 不管理的字段），
        只覆盖界面上真正编辑过的项：
          - 标量字段经 self.editor_vars 读取，键名就是 config_schema 的 config_path；
          - 枚举模式等下拉框走 editor_choice_maps 映射回数值/字符串；
          - 前后缀由 _affix_editor_data() 统一读回；
          - mode=1（顺序枚举）时把每个前缀的区间写成 prefix_ranges（count 恒为 1），
            其它模式则移除该键，避免残留旧的按前缀区间影响引擎行为；
          - 自定义字符内容若用户没改过（仍等于打开时的初值），直接沿用原始对象，
            这样数字列表等非字符串写法不会被文本框转成字符串而丢失类型。
        """
        cfg = copy.deepcopy(self.editor_cfg)
        def integer(key):
            return int(self.editor_vars[key].get().strip())
        self._set_cfg_value(cfg, "team_name", self.editor_vars["team_name"].get().strip())
        for key in ("threads.worker_threads", "enumeration.variable_length"):
            self._set_cfg_value(cfg, key, integer(key))
        mode = self.editor_choice_maps["enumeration.mode"][self.editor_vars["enumeration.mode"].get()]
        self._set_cfg_value(cfg, "enumeration.mode", mode)
        cfg["enumeration"]["ranges"] = [{"start": integer("range.start"), "end": integer("range.end")}]
        prefixes, suffixes = self._affix_editor_data()
        cfg["prefixes"] = [{"name": name} for name, _, _ in prefixes]
        cfg["suffixes"] = [{"name": name} for name in suffixes]
        if mode == 1:
            cfg["enumeration"]["prefix_ranges"] = [
                {"count": 1, "start": int(str(start).strip()), "end": int(str(end).strip())}
                for _, start, end in prefixes
            ]
        else:
            cfg["enumeration"].pop("prefix_ranges", None)
        cs = cfg["character_set"]
        cs["single_char_length"] = self.editor_choice_maps["character_set.single_char_length"][self.editor_vars["character_set.single_char_length"].get()]
        cs["types"] = self._selected_charset_types()
        if not cs["types"]:raise ValueError('请至少选择一种字符类型。')
        custom_text=self.editor_vars["character_set.custom_values"].get()
        if custom_text==self.charset_custom_initial:
            cs['custom_values']=copy.deepcopy(self.charset_custom_original)
        else:
            cs['custom_values']=json.loads(custom_text) if custom_text.strip().startswith('[') else custom_text
        options=CHARSET_TYPES.get(cs['single_char_length'], {'自定义字符':1,'Unicode 区间':2})
        unicode_code=options.get('Unicode 区间')
        if unicode_code in cs['types']:
            ranges=[]
            for line in self.charset_unicode_text.get('1.0','end').splitlines():
                if not line.strip():continue
                tokens=re.split(r'\s*-\s*',line.strip())
                if len(tokens)!=2:raise ValueError('Unicode 区间应为 开始-结束，每行一段。')
                def codepoint(token):
                    return int(token[2:],16) if token.upper().startswith('U+') else int(token,0)
                start,end=map(codepoint,tokens)
                if not 0<=start<=end<=0x10ffff:raise ValueError('Unicode 区间超出有效范围。')
                ranges.append(dict(start=start,end=end))
            if not ranges:raise ValueError('请选择至少一段 Unicode 区间。')
            cs['unicode_ranges']=ranges
        for family in FAMILIES:
            cfg["collection"][f"min_{family}"] = integer(f"collection.min_{family}")
        for family in ("cp", "fp", "wc", "fs", "pj"):
            cfg["collection"][f"ptt_{family}"] = integer(f"collection.ptt_{family}")
        # score_list 由界面自动维护（见 _effective_score_list）：用户在某族上填了正阈值，
        # 就说明该族要被计算，不必再去手改这一串族名。同步回输入框，让界面显示真实结果。
        cfg["collection"]["score_list"] = self._effective_score_list(cfg)
        self.editor_vars["collection.score_list"].set(cfg["collection"]["score_list"])
        cfg["collection"]["score_mode"] = self.editor_choice_maps["collection.score_mode"][self.editor_vars["collection.score_mode"].get()]
        cfg["collection"]["collect_mode"] = self.editor_choice_maps["collection.collect_mode"][self.editor_vars["collection.collect_mode"].get()]
        # 结果文件：模式决定写 "+"/"-"，自定义模式写用户填的文件名。
        # 注意这里**不做** _resolve_result_file 展开 —— 展开发生在运行/保存时，
        # 这样配置文件里保留的是"+"/"-"这种意图，而不是某一次随机出来的具体文件名
        # （否则每次保存都会把配置改写成新的时间戳，且 resume 会找不到上次的文件）。
        result_mode = self.editor_vars["output.result_file.mode"].get()
        if result_mode == "自定义":
            custom = self.editor_vars["output.result_file.name"].get().strip()
            cfg["output"]["result_file"] = custom or "-"   # 没填就退回时间戳，避免产生空文件名
        else:
            cfg["output"]["result_file"] = self.editor_choice_maps["output.result_file.mode"][result_mode]
        cfg["output"]["output_xp"] = self.editor_choice_maps["output.output_xp"][self.editor_vars["output.output_xp"].get()]
        cfg["output"]["log_output"] = int(self.editor_vars["output.log_output"].get())
        cfg["output"]["speed_output"] = int(self.editor_vars["output.speed_output"].get())
        return cfg

    def _effective_score_list(self, cfg: dict) -> str:
        """返回应该生效的 ``collection.score_list``：凡填了正阈值的族一律纳入。

        **为什么要在界面层做这件事**：编辑器上每一族都有「最低 XX」输入框，用户在某族上
        填了非零阈值，意思就是"我要算这一族"。但校验规则要求「阈值 >0 ⇒ 该族必须在
        score_list 里」（否则设了阈值却不计算，门控形同虚设），于是用户会被一个看不见的
        内部约束拦住 —— 还得自己去手改 ``score_list`` 那串族名。这是把内部一致性的负担
        推给了用户，所以改由界面维护。

        规则：
          - 用户已写的族名做归一（``rh`` → ``fs``、丢弃无法识别的项、去重），
            空列表仍表示"全十族"；
          - 再把「正阈值（``min_*`` 或 ``ptt_*``）却没在列表里的族」补进来；
          - 结果统一按 FAMILIES 的规范顺序排列 —— 与引擎的输出列序一致，便于阅读比对，
            也不影响计算（引擎本来就不看这串名字的顺序）；
          - 要真正停算某族，把它的阈值改回 0 再把它从这串里删掉即可。
        """
        collection = cfg.get("collection", {})
        normalized = _normalize_score_list(self.editor_vars["collection.score_list"].get())
        for family in FAMILIES:
            thresholds = (collection.get(f"min_{family}", 0), collection.get(f"ptt_{family}", 0))
            if any(isinstance(value, int) and value > 0 for value in thresholds):
                if family not in normalized:
                    normalized.append(family)
        return ",".join(family for family in FAMILIES if family in normalized)

    def _save_editor(self, start=False, to_default=False):
        """保存编辑器内容。两个保存目标对应界面上两个按钮：

        - 默认（to_default=False）：写回当前配置文件，保留其 YAML/JSON/TOML 格式；
        - to_default=True：写 config.yaml 并把 UI 切换到它（README.md 有说明）。

        保存前统一走 _validate_config；--一一对应模式（mode 4）下前后缀数量不等时先
        弹确认框（_confirm_affix_mismatch），因为缺口会被取模循环或多出的后缀静默吞掉。
        任何失败都只弹窗不改状态，保证编辑器里的未保存内容不丢。
        """
        try:
            cfg = self._editor_config()
            _validate_config(cfg)
            if not self._confirm_affix_mismatch(cfg):
                return
            target = os.path.join(CONFIG_DIR, "config.yaml") if to_default else self.config_var.get().strip()
            if not target:
                raise ValueError("请先选择当前配置文件，或使用保存到 config.yaml。")
            if not os.path.isabs(target):
                target = os.path.join(CONFIG_DIR, target)
            save_config(target, cfg)
        except Exception as exc:
            messagebox.showerror("无法保存配置", str(exc), parent=self.editor)
            return
        self.config_var.set(target)
        self.editor_cfg = cfg
        self._sync_config_controls(cfg)
        if start:
            self._close_editor()
            self._start()

    def _confirm_affix_mismatch(self, cfg):
        if cfg.get("enumeration", {}).get("mode") != 4:
            return True
        np = len(cfg.get("prefixes", []))
        ns = len(cfg.get("suffixes", []))
        if np == ns:
            return True
        detail = "后缀不足，将按下标取模循环使用后缀。" if ns < np else "后缀多于前缀，多出的后缀不会参与配对。"
        return messagebox.askyesno(
            "一一对应：前后缀数量不匹配",
            f"前缀 {np} 条，后缀 {ns} 条。\n配对以 {np} 条前缀为准。\n{detail}\n\n仍要继续保存吗？",
            parent=self.editor,
        )

    def _close_editor(self):
        if self.editor and self.editor.winfo_exists():
            self.editor.destroy()
        self.editor = None

    def _start(self):
        """校验界面输入并启动一次测号任务（图形监控器入口）。

        流程：读配置 → 校验 → 可选续测 → 组装 task → 锁定启动参数 → 交给后台线程。
        这里只做「启动瞬间」的参数拍板：启动线程、范围终点、结果文件取自界面，
        之后就锁住不再允许改（活跃线程仍可在运行中调整，见 _apply_runtime_threads）。
        任何异常都弹窗提示且不进入运行态。
        """
        if self.running:
            return
        resume = self.resume_var.get()
        rebuild = self.rebuild_var.get()
        try:
            config_path = self.config_var.get().strip()
            cfg = load_config(config_path)
            _validate_config(cfg)
            # Resume 仅支持枚举模式 1：位置由结果文件最后一条命中推出，不是精确检查点。
            if resume:
                from .main import _resolve_result_file, _try_resume
                resumed = _try_resume(cfg, _resolve_result_file(cfg["output"].get("result_file", "result.txt")))
                if resumed is not None:
                    cfg = resumed[0]
            task = _build_task_config(copy.deepcopy(cfg))
            task["n_threads"] = int(self.threads_var.get())
            if not 1 <= task["n_threads"] <= 256:
                raise ValueError("启动线程必须在 1 到 256 之间")
            range_end = int(self.range_var.get())
            # 界面上的 -1 表示「无上界」：转成 u64 上限，而不是一个有限任务。
            task["range_R"] = 2**64 - 1 if range_end == -1 else range_end
            # ui_mode 让引擎额外维护 Top 5 并回传排行榜；不依赖 output_xp。
            task["ui_mode"] = True
            # 监控器需要进度与速度行来驱动界面显示。
            task["output_log"] = 1
            task["output_speed"] = 1
            self.families = self._families_from_task(task)
            self.boards = {family: [] for family in self.families}
            self.expanded_families.clear()
            self._render_score_boxes(self.families)
        except Exception as exc:
            messagebox.showerror("无法启动", str(exc))
            return
        self.running = True
        self.stop_requested = False
        self.rebuild_requested = rebuild
        self.runtime_thread_limit = task["n_threads"]
        self.runtime_active_threads = self.runtime_thread_limit
        self.runtime_threads_var.set(str(self.runtime_thread_limit))
        self._set_threads_controls_enabled(True, self.runtime_thread_limit)
        self.start_threads_spinbox.configure(state="disabled")
        self.resume_checkbutton.configure(state="disabled")
        self.rebuild_checkbutton.configure(state="disabled")
        self._write_runtime_threads(self.runtime_thread_limit)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("正在构建并启动普通引擎...")
        threading.Thread(target=self._launch, args=(task,), daemon=True).start()

    def _launch(self, task: dict):
        """后台线程：按需编译 UI 引擎，然后启动它并把 stderr 逐行投递给界面。

        与 CLI 的关键差别：
          - 使用 pbb_ui_engine.exe（PBB_UI_ENGINE=1），只有它维护 Top 5 排行榜；
            这样终端模式的普通测号可以继续跑，不会与这里的编译目标抢文件锁。
          - 界面线程与引擎之间只通过 self.events 队列通信，不在工作线程里直接碰控件。
        """
        try:
            os.environ.pop("PBB_SRC_DIR", None)
            # pbb_core is already loaded by this Python process, so rebuilding
            # its .pyd here would fail on Windows due to the active file lock.
            if self.engine_bin is None or self.rebuild_requested:
                self.events.put(("status", "正在构建普通引擎..."))
                # 用 UI 专用可执行文件：这样终端模式的普通测号可以继续跑，
                # 不会与这里的编译目标抢同一个文件锁。
                self.engine_bin = str(paths.engine_binary(ui=True))
                if self.rebuild_requested or not _binary_is_current(self.engine_bin, engine_target="ui"):
                    if not paths.ENGINE_MAIN.is_file():
                        raise RuntimeError("缺少引擎源码，无法重新编译")
                    os.environ["PBB_BUILD_MODE"] = "native"
                    _compile_engine(verbose=False, output_path=self.engine_bin, ui_engine=True)
                self.rebuild_requested = False
            if self.stop_requested:
                self.events.put(("finished", "已停止"))
                return
            binary = self.engine_bin
            if not self.pbb_core_initialized:
                ensure_core()
            # pbb_core 的位置由 paths.BUILD_DIR 统一决定（开发仓库是 <仓库根>/build，
            # 冻结后是 _internal/build），不需要 UI 自己拼路径。
            engine_runner._pbb_core()
            self.pbb_core_initialized = True
            charset_hex = engine_runner._build_charset(task["character_set"])
            # 结果文件名必须在交给引擎之前解析：引擎只认字面文件名，"+"/"-" 的特殊含义
            # 由 Python 侧解释（见 main._resolve_result_file）。原来这里直接传原值，
            # 于是把 "-" 当成文件名，在 out/ 下产生了一个名为 "-" 的文件。
            # 空值也交给它处理（会得到时间戳名），不再另写一套默认值。
            result_file = _resolve_result_file(self._selected_result_file())
            params = engine_runner._build_params(task, charset_hex, result_file)
            if self.stop_requested:
                self.events.put(("finished", "已停止"))
                return
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.proc = subprocess.Popen([binary], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                         creationflags=creationflags)
            if self.stop_requested:
                self.proc.terminate()
            self.events.put(("status", "普通引擎正在运行"))
            assert self.proc.stdin is not None
            self.proc.stdin.write(params)
            self.proc.stdin.close()
            assert self.proc.stderr is not None
            for line in self.proc.stderr:
                self.events.put(("line", line.strip()))
            code = self.proc.wait()
            self.events.put(("finished", "已停止" if code else "已完成"))
        except BaseException as exc:
            self.events.put(("error", str(exc) or "普通引擎构建或启动失败"))

    @staticmethod
    def _thread_control_path() -> str:
        """运行期「活跃线程」控制文件的路径。

        引擎在工作期间轮询 out/.threads；UI 改写它即可在不重启引擎的前提下
        调整工作线程数（每个工作线程在当前 chunk 完成后才响应）。
        """
        return os.path.join(ROOT, "out", ".threads")

    def _write_runtime_threads(self, count: int):
        """原子地发布当前活跃线程上限，供运行中的引擎读取。

        先写临时文件再 os.replace：引擎可能随时读取该文件，避免它读到写了一半的内容。
        """
        control = self._thread_control_path()
        os.makedirs(os.path.dirname(control), exist_ok=True)
        temporary = f"{control}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="ascii", newline="") as fh:
            fh.write(str(count))
        os.replace(temporary, control)

    def _clear_runtime_threads(self):
        try:
            os.remove(self._thread_control_path())
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _set_threads_controls_enabled(self, enabled: bool, limit: int = 0):
        """统一启用/禁用「活跃线程」输入框与它的应用按钮。

        两者必须同步（曾经只禁用输入框而按钮仍是可点的），所以集中到一处，
        避免以后再加控件时漏掉其中一个。
        """
        state = "normal" if enabled else "disabled"
        if enabled:
            self.runtime_threads_spinbox.configure(from_=0, to=limit, state=state)
        else:
            self.runtime_threads_spinbox.configure(state=state)
        self.apply_threads_button.configure(state=state)

    def _apply_runtime_threads(self, _event=None):
        """把「活跃线程」输入框的值写进 out/.threads，运行时实时增减 worker。

        取值 0 是**合法值**，表示暂停计算（引擎侧让所有 worker 睡下、生产者也不再投喂，
        两者都进入真正的等待而不是空转）；恢复时填回正数即可，线程不会销毁。
        因此下限是 0 而不是 1 —— 早期实现用 max(1, ...) 把 0 夹掉了，用户无法暂停。

        越界或非数字一律显示错误并还原输入框，**不静默改成别的值**：
        静默夹取会让界面显示一个用户没输入过的数字，看起来像"输入被吃了"。
        """
        if not self.running or self.runtime_thread_limit < 1:
            return
        raw = self.runtime_threads_var.get().strip()
        try:
            requested = int(raw)
        except ValueError:
            self.runtime_threads_var.set(str(self.runtime_active_threads))
            messagebox.showerror("无法调整线程", f"请输入 0 到 {self.runtime_thread_limit} 之间的整数，当前输入：{raw!r}")
            return
        if not 0 <= requested <= self.runtime_thread_limit:
            self.runtime_threads_var.set(str(self.runtime_active_threads))
            messagebox.showerror(
                "无法调整线程",
                f"活跃线程必须在 0 到 {self.runtime_thread_limit} 之间（0 = 暂停计算），当前输入：{requested}")
            return
        try:
            self._write_runtime_threads(requested)
            self.runtime_active_threads = requested
            self.runtime_threads_var.set(str(requested))
            self.status_var.set("活跃线程已暂停（0）" if requested == 0
                                else f"活跃线程已调整为 {requested}")
        except OSError as exc:
            self.runtime_threads_var.set(str(self.runtime_active_threads))
            messagebox.showerror("无法调整线程", str(exc))

    @staticmethod
    def _families_from_task(task: dict) -> list[str]:
        """把 task 的 score_list 归一成 UI 排行榜要显示的族列表。

        空列表表示「全部十族」（与引擎语义一致）；`rh` 是 `fs` 的别名，先去别名再去重，
        顺序按配置文件里的书写顺序，仅影响排行榜面板的排列，不影响引擎输出列序。
        """
        raw = task.get("score_list", "")
        if isinstance(raw, (list, tuple)):
            raw = ",".join(raw)
        chosen = [part.strip().lower() for part in str(raw).split(",") if part.strip()]
        if not chosen:
            return list(FAMILIES)
        return _normalize_score_list(raw)

    def _drain_events(self):
        """主线程侧的队列消费者：把工作线程的消息刷成界面更新。

        由 Tk 的 after 循环周期性调用。两点约束：
          - 单次最多取 250 条，避免引擎刷得太快时界面长时间无法响应；
          - 排行榜重绘按 0.15 秒限流（boards_dirty + last_board_refresh），
            否则高频 UI_TOP 事件会让界面卡死。
        """
        for _ in range(250):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                self._handle_line(value)
            elif kind == "status":
                self.status_var.set(value)
            elif kind == "finished":
                self._set_finished(value)
            elif kind == "error":
                self._set_finished("启动失败")
                messagebox.showerror("引擎错误", value)
        if self.boards_dirty and time.monotonic() - self.last_board_refresh >= 0.15:
            self._refresh_boards()
            self.boards_dirty = False
            self.last_board_refresh = time.monotonic()
        # Never monopolize Tk's event loop when the engine writes progress fast.
        # 用 _schedule_after 记账，关闭时才能把这些待执行回调取消掉（见 _close）。
        self._schedule_after(50 if self.running else 150, self._drain_events)

    def _handle_line(self, line: str):
        """解析引擎的一行 stderr 输出：UI_TOP 协议、进度行、计数行与 SIMD 提示。

        UI_TOP 是**成批**到达的：UI_TOP_BEGIN 开一块新榜单，随后若干 UI_TOP 行，
        最后 UI_TOP_END 才整体换上去 —— 这样界面永远不会展示只更新了一半的榜单。
        UI_TOP 的名字列是十六进制编码的 UTF-8（名字可能含制表符/换行之外任意字符），
        解码失败就丢弃该行而不是让整块榜单失败。
        其余进度字段用模块级正则从引擎日志里提取，引擎日志格式才是唯一契约。
        """
        if line == "UI_TOP_BEGIN":
            self._incoming_boards = {family: [] for family in self.families}
            return
        if line == "UI_TOP_END":
            self.boards = self._incoming_boards
            self.boards_dirty = True
            return
        if line.startswith("UI_TOP\t"):
            parts = line.split("\t", 3)
            if len(parts) == 4 and parts[1] in self.families:
                try:
                    name = bytes.fromhex(parts[3]).decode("utf-8", errors="replace")
                    self._incoming_boards[parts[1]].append((int(parts[2]), name))
                except ValueError:
                    pass
            return
        match = PROGRESS_RE.search(line)
        if match:
            self.stat_vars["found"].set(match.group(1))
            self.stat_vars["elapsed"].set(f"{float(match.group(2)):.1f}s")
            self.stat_vars["overall"].set(f"{match.group(3)}T/d")
            self.stat_vars["left"].set(match.group(4))
        match = COUNT_RE.search(line)
        if match:
            self.stat_vars["count"].set(f"{match.group(1)}T")
        match = CURRENT_RE.search(line)
        if match:
            self.stat_vars["realtime"].set(f"{match.group(1)}T/d")
        if line.startswith("[engine] SIMD:"):
            self.status_var.set(line)

    def _refresh_boards(self):
        """把排行榜数据重绘到各族面板：默认只显示第 1 名，展开后显示前 5 名。

        行不足 limit 时用 "-" 补齐，保证展开/收起时面板高度不跳。
        用 tk.Text 的面板要先切到 normal 才能改写内容，写完必须切回 disabled，
        否则用户可以手动编辑排行榜文本。
        """
        for family, view in self.score_views.items():
            entries = self.boards.get(family, [])
            limit = 5 if family in self.expanded_families else 1
            if limit == 1:
                lines = [f"{score:>6}  {name}" for score, name in entries[:1]]
            else:
                lines = [f"{rank}. {score:>6}  {name}" for rank, (score, name) in enumerate(entries[:limit], 1)]
            while len(lines) < limit:
                lines.append("-")
            if isinstance(view, tk.Text):
                view.configure(state="normal")
                view.delete("1.0", "end")
                view.insert("1.0", "\n".join(lines))
                view.configure(state="disabled")
            else:
                view.configure(text=lines[0])

    def _stop(self):
        """请求停止当前运行：先温和 terminate，1.5 秒后仍未退出才强杀。

        分两步是为了让引擎有机会在当前 chunk 结束处干净退出（并写完 SUMMARY），
        直接 kill 会丢掉最后一次统计。若引擎还在编译阶段（self.proc 为 None），
        只置标志位，由工作线程在下一个检查点自行收尾。
        """
        if not self.running:
            return
        self.stop_requested = True
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在停止引擎..." if self.proc else "正在取消启动...")
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._schedule_after(1500, self._force_stop, self.proc)

    def _force_stop(self, proc: subprocess.Popen):
        if proc is self.proc and proc.poll() is None:
            self.status_var.set("正在强制停止引擎...")
            proc.kill()

    def _set_finished(self, status: str):
        self.running = False
        self.stop_requested = False
        self.proc = None
        self._clear_runtime_threads()
        self.runtime_thread_limit = 0
        self.runtime_active_threads = 0
        self._set_threads_controls_enabled(False)
        self.start_threads_spinbox.configure(state="normal")
        self.resume_checkbutton.configure(state="normal")
        self.rebuild_checkbutton.configure(state="normal")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set(status)

    def _schedule_after(self, delay_ms: int, callback, *args) -> str:
        """``after`` 的记账版本：记录 id，便于关闭时统一取消。

        **为什么必须记账**：Tk 的 ``after`` 回调在窗口销毁后**仍会被执行**，此时回调里
        引用的控件已不存在，Tk 抛 ``invalid command name "..."``。在冻结的 windowed 应用
        里这个 Tcl 错误会弹**模态错误框** —— 窗口已经消失，用户看不到它，于是主循环
        永不返回，进程一直留在任务管理器里（表现为"关了 UI 还残留 PBBMonitor"）。

        这里同时用 ``_closing`` 兜底：关闭过程中不再安排新的回调（``_drain_events``
        会在每个周期末尾重排自己，不拦就会源源不断地产生新的待执行回调）。
        """
        if self._closing:
            return ""
        after_id = self.after(delay_ms, callback, *args)
        self._after_ids.add(after_id)
        return after_id

    def _cancel_scheduled_after(self):
        """取消本窗口所有已记账、尚未执行的 ``after`` 回调。"""
        for after_id in tuple(self._after_ids):
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._after_ids.clear()

    def _close(self):
        """关闭主窗口：停止任务 → 结束主循环 → 销毁窗口。

        顺序与每一步的必要性（都经过实测，不是照抄惯例）：

        1. ``_closing = True``：让 ``_drain_events`` 停止自我重排。
        2. ``_cancel_scheduled_after()``：**必须取消待执行的 after 回调**。Tk 在窗口
           销毁后仍会执行已排队的 after，此时回调引用的控件已不存在，会报
           ``invalid command name "..."``。
        3. ``_stop()`` + 直接 ``kill()`` 引擎：``_stop()`` 走的是 ``terminate()`` +
           ``after(1500)`` 兜底，而窗口随即销毁、那个兜底回调不会执行；用户点关闭就该
           立刻停，不必依赖引擎自己的父进程监视（那要 1~2 秒）。
        4. ``quit()`` **再** ``destroy()``：**这一步是残留的根因**。
           实测只调 ``destroy()`` 时 ``mainloop()`` 并不返回，进程会一直留在任务管理器
           里（窗口已消失，用户看不到任何提示）；先 ``quit()`` 显式结束主循环，再
           ``destroy()`` 释放窗口，才能干净退出。
        """
        self._closing = True
        self._cancel_scheduled_after()
        self._stop()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        # quit() 结束 mainloop —— 只 destroy() 时 mainloop 不返回，进程会残留。
        try:
            self.quit()
        finally:
            self.destroy()


if __name__ == "__main__":
    Monitor().mainloop()

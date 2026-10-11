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
from .main import _build_task_config, _validate_config


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
OUTPUT_SCORE_MODES = {"仅输出姓名": 0, "输出姓名和评分": 1}
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


def apply_windows_window_icon(window):
    """给窗口同时设置大小两个原生图标槽位，保证标题栏与任务栏图标一致。

    Tk 的 iconphoto 不总能传递到 Windows 任务栏，因此直接对 HWND 发 WM_SETICON：
    先按当前 DPI 的 SM_CXICON 尺寸加载同一个 .ico 到两个槽位，加载句柄缓存到窗口对象上，
    避免被 GC 回收（句柄失效会导致图标变回默认）。
    """
    if sys.platform != "win32" or not os.path.exists(ICON_FILE):
        return
    import ctypes
    # Tk's iconphoto is not always propagated to the Windows taskbar.  Set both
    # native icon slots on the HWND so taskbar and title bar share the same ICO.
    image_icon = 1
    load_from_file = 0x0010
    wm_seticon = 0x0080
    user32 = ctypes.windll.user32
    taskbar_size = user32.GetSystemMetrics(11)  # SM_CXICON: 32/48/64 by DPI
    # Taskbar implementations may query either icon slot.  Supplying a 16px
    # small icon here lets it be selected and then enlarged, so both slots use
    # the same DPI-matched taskbar source instead.
    small = user32.LoadImageW(
        None,
        ICON_FILE,
        image_icon,
        taskbar_size,
        taskbar_size,
        load_from_file,
    )
    large = user32.LoadImageW(
        None,
        ICON_FILE,
        image_icon,
        taskbar_size,
        taskbar_size,
        load_from_file,
    )
    if small:
        user32.SendMessageW(window.winfo_id(), wm_seticon, 0, small)
    if large:
        user32.SendMessageW(window.winfo_id(), wm_seticon, 1, large)
    window._native_icon_handles = (small, large)


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
        self.geometry("1100x820")
        self.minsize(880, 640)
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.engine_bin: str | None = None
        self.pbb_core_initialized = False
        self.running = False
        self.stop_requested = False
        self.dark_mode = False
        self.colors = {"bg": "#f7f7f7", "panel": "#ffffff", "field": "#ffffff", "text": "#171717", "accent": "#171717"}
        self._theme_checkbuttons: list[tk.Checkbutton] = []
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
        stamp = datetime.now().strftime("ui_%Y%m%d_%H%M%S.txt")
        self.result_var = tk.StringVar(value=stamp)
        self.status_var = tk.StringVar(value="准备就绪")
        self.editor: tk.Toplevel | None = None
        self.editor_cfg: dict | None = None
        self.editor_vars: dict[str, tk.Variable] = {}
        self.rebuild_requested = False
        self.resume_var = tk.BooleanVar(value=False)
        self.rebuild_var = tk.BooleanVar(value=False)
        self._build_layout()
        self._apply_theme()
        self.after(150, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_layout(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TButton", font=(UI_FONT, 10), padding=(10, 6))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        sidebar = ttk.Frame(body, width=390)
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
        self.runtime_threads_spinbox = ttk.Spinbox(
            controls, from_=1, to=1, textvariable=self.runtime_threads_var,
            width=8, command=self._apply_runtime_threads, state="disabled",
        )
        self.runtime_threads_spinbox.grid(row=5, column=0, sticky="w")
        self.runtime_threads_spinbox.bind("<Return>", self._apply_runtime_threads)
        self.runtime_threads_spinbox.bind("<FocusOut>", self._apply_runtime_threads)
        ttk.Label(controls, text="结束范围").grid(row=6, column=0, sticky="w", pady=(10, 2))
        ttk.Entry(controls, textvariable=self.range_var).grid(row=7, column=0, columnspan=2, sticky="ew")
        ttk.Label(controls, text="结果文件").grid(row=8, column=0, sticky="w", pady=(10, 2))
        ttk.Entry(controls, textvariable=self.result_var).grid(row=9, column=0, columnspan=2, sticky="ew")
        launch_options = ttk.Frame(controls)
        launch_options.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.resume_checkbutton = self._checkbutton(launch_options, "Resume 续测", self.resume_var)
        self.resume_checkbutton.pack(side="left")
        self.rebuild_checkbutton = self._checkbutton(launch_options, "Rebuild 引擎", self.rebuild_var)
        self.rebuild_checkbutton.pack(side="left", padx=(12, 0))
        self.start_button = ttk.Button(controls, text="开始", command=self._start)
        self.start_button.grid(row=11, column=0, sticky="ew", pady=(10, 0))
        self.stop_button = ttk.Button(controls, text="停止", command=self._stop, state="disabled")
        self.stop_button.grid(row=11, column=1, sticky="ew", padx=(6, 0), pady=(10, 0))
        ttk.Button(controls, text="编辑配置", command=self._open_editor).grid(row=12, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        controls.columnconfigure(0, weight=1)

        ttk.Label(
            right, text="重要启动选项：Resume 续测 / Rebuild 引擎", style="LaunchNotice.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            right, text="接着上次进度测号请勾选 Resume。\n推荐暂停测号后运行外层 tune_full.bat（推荐 20 分钟），自动选择本机优化。\n测试完成后仍须勾选 Rebuild 引擎重建一次；重建前实际引擎尚未应用测试选择。",
            style="LaunchNotice.TLabel",
            wraplength=540,
        ).pack(anchor="w", pady=(0, 12))
        score_header = ttk.Frame(right)
        score_header.pack(fill="x", pady=(0, 6))
        ttk.Label(score_header, text="评分排行榜", font=(UI_FONT, 14, "bold")).pack(side="left")
        ttk.Button(score_header, text="全部收起为 Top 1", command=self._collapse_top5).pack(side="right")
        ttk.Button(score_header, text="全部展开 Top 5", command=self._expand_top5).pack(side="right", padx=(0, 6))
        score_view = ttk.Frame(right)
        score_view.pack(fill="both", expand=True)
        self.score_canvas = tk.Canvas(score_view, highlightthickness=0)
        score_scrollbar = ttk.Scrollbar(score_view, orient="vertical", command=self.score_canvas.yview)
        self.score_canvas.configure(yscrollcommand=score_scrollbar.set)
        score_scrollbar.pack(side="right", fill="y")
        self.score_canvas.pack(side="left", fill="both", expand=True)
        self.score_area = ttk.Frame(self.score_canvas)
        self.score_window = self.score_canvas.create_window((0, 0), window=self.score_area, anchor="nw")
        self.score_area.bind("<Configure>", self._sync_score_scroll)
        self.score_canvas.bind("<Configure>", self._resize_score_area)
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

    def _render_score_boxes(self, families: list[str]):
        for child in self.score_area.winfo_children():
            child.destroy()
        self.score_views.clear()
        if not families:
            ttk.Label(self.score_area, text="选择配置后点击“开始”，这里会显示各评分族的第一名。", font=(UI_FONT, 12)).pack(anchor="center", pady=80)
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
        self.result_var.set(str(cfg.get("output", {}).get("result_file", "result.txt")))

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

    def _create_scrollable_editor_section(self, parent, title: str, height: int = 230):
        section = ttk.LabelFrame(parent, text=title, padding=10)
        viewport = ttk.Frame(section)
        viewport.pack(fill="both", expand=True)
        canvas = tk.Canvas(viewport, height=height, highlightthickness=0, bg=self.colors["panel"])
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview)
        rows = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=rows, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        rows.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        self._editor_canvases.append(canvas)
        return section, rows, viewport

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
        editor.geometry("980x820")
        editor.minsize(820, 660)
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
        ttk.Label(recommendation, text="推荐运行外层 tune_full.bat 自动优化（推荐 20 分钟）。测试后必须再 Rebuild 一遍，才能应用测试选择。",
                  style="LaunchNotice.TLabel", wraplength=700).pack(anchor="w")
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
        self.editor_vars = {}
        self.editor_choice_maps = {}
        self._editor_canvases = []
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
        field(output, 0, "结果文件", "output.result_file", out.get("result_file", "result.txt"), width=45)
        choice(output, 1, "输出模式", "output.output_xp", out.get("output_xp", 0), OUTPUT_SCORE_MODES)
        for row, (label, key, default) in enumerate((("记录日志", "output.log_output", 1), ("记录速度", "output.speed_output", 1)), start=2):
            var = tk.BooleanVar(value=bool(out.get(key.split(".")[-1], default)))
            self.editor_vars[key] = var
            self._checkbutton(output, label, var).grid(row=row, column=0, sticky="w", pady=8)


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
        cfg["output"]["result_file"] = self.editor_vars["output.result_file"].get().strip()
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
        self.runtime_threads_spinbox.configure(to=self.runtime_thread_limit, state="normal")
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
            result_file = self.result_var.get().strip() or datetime.now().strftime("ui_%Y%m%d_%H%M%S.txt")
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

    def _apply_runtime_threads(self, _event=None):
        if not self.running or self.runtime_thread_limit < 1:
            return
        try:
            requested = int(self.runtime_threads_var.get().strip())
        except ValueError:
            requested = self.runtime_active_threads
        requested = max(1, min(requested, self.runtime_thread_limit))
        try:
            self._write_runtime_threads(requested)
            self.runtime_active_threads = requested
            self.runtime_threads_var.set(str(requested))
            self.status_var.set(f"活跃线程已调整为 {requested}")
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
        self.after(50 if self.running else 150, self._drain_events)

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
            self.after(1500, self._force_stop, self.proc)

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
        self.runtime_threads_spinbox.configure(state="disabled")
        self.start_threads_spinbox.configure(state="normal")
        self.resume_checkbutton.configure(state="normal")
        self.rebuild_checkbutton.configure(state="normal")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set(status)

    def _close(self):
        self._stop()
        self.destroy()


if __name__ == "__main__":
    Monitor().mainloop()

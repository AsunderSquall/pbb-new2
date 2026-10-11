#!/usr/bin/env python3
"""PBB 测号器命令行入口（薄壳）。

真正的实现在 ``pbb/main.py``；本文件只负责把仓库根加入 ``sys.path`` 并把 ``__main__``
交给对应的包内模块。之所以不写成 ``python pbb/main.py``：包内模块之间用相对导入
（``from .engine import ...``），直接按文件路径执行会失去包上下文而报
``attempted relative import with no known parent package``。

用法::

    python main.py -c config/config_football.yaml --range-end 1000000
    python main.py --help
    python -m pbb.main --help        # 等价写法

命令行参数原样透传给包内实现。
"""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# DPI 感知必须在 import tkinter / 建 Tk 根窗口之前声明：Tk 初始化时会读取进程的感知
# 状态并据此决定渲染尺寸，之后再改就晚了。CLI 自己不用 Tk，但配置编辑器（pbb.ui）会用到。
#
# 这里刻意内联 ctypes 而**不** import pbb.ui：那个导入会让 pbb.ui 在 runpy 执行
# pbb.main 之前就进入 sys.modules，触发 runpy 的 RuntimeWarning，并可能扰乱以
# __main__ 身份运行的模块。实现与说明见 pbb/ui.py 的 enable_windows_dpi_awareness()。
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 2 = PER_MONITOR_DPI_AWARE
    except Exception:
        pass

# run_module 会让 pbb.main 以 __main__ 身份运行，因此它的
# `if __name__ == "__main__":` 分支会正常触发，且包相对导入保持有效。
runpy.run_module("pbb.main", run_name="__main__", alter_sys=True)
#!/usr/bin/env python3
"""PBB 图形监控器入口（薄壳）。

真正的实现在 ``pbb/ui.py``；本文件只负责把仓库根加入 ``sys.path`` 并执行包内模块。
直接 ``python pbb/ui.py`` 不可行：包内模块使用相对导入（``from .engine import ...``），
按文件路径执行会失去包上下文。

用法::

    python ui.py                     # 等价于 python -m pbb.ui
    pythonw ui.py                    # 无控制台窗口
"""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

runpy.run_module("pbb.ui", run_name="__main__", alter_sys=True)
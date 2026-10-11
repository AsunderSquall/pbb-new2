#!/usr/bin/env python3
"""本机自动测速入口（薄壳）。

真正的实现在 ``pbb/autotune.py``。这里与仓库根的 ``main.py`` / ``ui.py`` 用同一套做法：
把源码根加入 ``sys.path``，再用 ``runpy.run_module`` 以 ``__main__`` 身份执行包内模块 ——
``pbb.autotune`` 内部用的是相对导入（``from . import build_settings, paths``），
直接按文件路径执行会报 ``attempted relative import with no known parent package``。

用法::

    python tools/tune.py --mode quick
    python tools/tune.py --mode full --speed 6.1 --minutes 20

发布包里的 ``tune_full.bat`` / ``tune_quick.bat`` 就是转发到这里（它们位于
``<包根>/source/``，所以本文件在 ``source/tools/`` 下、源码根是它的上一级）。
"""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

runpy.run_module("pbb.autotune", run_name="__main__", alter_sys=True)
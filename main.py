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

# run_module 会让 pbb.main 以 __main__ 身份运行，因此它的
# `if __name__ == "__main__":` 分支会正常触发，且包相对导入保持有效。
runpy.run_module("pbb.main", run_name="__main__", alter_sys=True)
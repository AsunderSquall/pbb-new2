"""图形监控器的进程入口。

**为什么需要这个模块**

PyInstaller 把 ``Analysis`` 里的入口脚本当作**顶层脚本**执行（模块名是 ``__main__``），
此时它没有包上下文。所以入口模块里不能出现 ``from . import ...`` 这种相对导入 —— 冻结后
会直接报 ``attempted relative import with no known parent package``（这正是把
``pbb/ui.py`` 直接当作 spec 入口会踩的坑：``pbb/ui.py`` 内部用的是相对导入）。

因此把"冻结入口"与"界面实现"分开：

- ``pbb/ui.py``  界面实现，可以自由使用相对导入；
- ``pbb/gui.py`` 本模块，只用绝对导入，供 PyInstaller 冻结使用。

仓库根的 ``ui.py`` 是给源码用户用的薄启动器（``runpy`` 转发到 ``pbb.ui``），
与冻结入口是两条路径。

**启动自检（``--check``）**

``--check`` 会构建主窗口、跑一遍布局与资源加载、然后立刻退出，用于打包后的冒烟验证。
问题在于 windowed 构建（``console=False``）的 bootloader 会把 stdout/stderr 丢掉，
崩溃时外界既看不到原因、进程还可能静默挂住。所以自检不靠标准输出传递结果，而是：

- 成功时在当前目录写 ``.gui_check_ok``；
- 失败时把 traceback 写进 ``.gui_check_error.log``。

打包脚本据此判定，并把日志内容原样打出来，无需人工干预。
"""
import os
import sys
import traceback

OK_MARKER = ".gui_check_ok"
ERROR_LOG = ".gui_check_error.log"


def main(argv=None) -> int:
    """构建主窗口并进入 Tk 事件循环；``--check`` 只做启动自检。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in argv
    # 固定写在工作目录下，调用方（tools/package_release.py）用 cwd 指定位置。
    ok_path = os.path.join(os.getcwd(), OK_MARKER)
    err_path = os.path.join(os.getcwd(), ERROR_LOG)
    for stale in (ok_path, err_path):
        try:
            os.remove(stale)
        except OSError:
            pass

    try:
        from pbb.ui import (Monitor, configure_windows_identity, enable_windows_dpi_awareness,
                            load_ui_font)

        # 顺序有讲究：DPI 感知必须早于任何 Tk 根窗口的创建（Monitor() 会建），
        # 否则 Tk 已经按未感知的 96 DPI 算好渲染尺寸，之后改也来不及。
        enable_windows_dpi_awareness()
        load_ui_font()
        configure_windows_identity()
        window = Monitor()
        if check_only:
            # update_idletasks 真正跑一遍布局与资源加载，然后立刻退出：
            # 既验证窗口能建起来，又不会把无人值守的打包流程卡在事件循环里。
            window.update_idletasks()
            window.destroy()
            with open(ok_path, "w", encoding="utf-8") as handle:
                handle.write("ok\n")
            return 0
        window.mainloop()
        return 0
    except BaseException:
        # 自检失败时把完整 traceback 落盘，供打包脚本读取。非自检模式下
        # 仍然落盘（此时目录是用户可写的工作目录），方便排查启动失败。
        try:
            with open(err_path, "w", encoding="utf-8") as handle:
                handle.write("".join(traceback.format_exc()))
        except OSError:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
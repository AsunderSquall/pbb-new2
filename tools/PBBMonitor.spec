# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包定义：把 pbb/gui.py 冻结成 PBBMonitor（图形监控器）。
#
# 入口必须是 pbb/gui.py，不能是 pbb/ui.py：PyInstaller 把入口脚本当作**顶层脚本**
# （模块名 __main__）执行，没有包上下文，而 pbb/ui.py 内部用相对导入
# （from . import build_settings ...），冻结后会直接报
# "attempted relative import with no known parent package"。pbb/gui.py 只用绝对导入。
# 仓库根的 ui.py 也不能作入口：它是 runpy 薄启动器，PyInstaller 收集不到实现代码。
#
# 输入路径以仓库根为基准。release_build 由 PBB_RELEASE_BUILD_DIR 覆盖，默认是
# build/ 目录 —— 三个原生二进制（pbb_core.pyd、pbb_ui_engine.exe 及各自清单）都在其中。
#
# 注意：PyInstaller 把 spec 里的相对路径相对于 **spec 文件所在目录** 解析，而不是
# 调用者的 cwd。所以这里一律用基于 __file__ 的绝对路径，仓库根取 spec 的上一级。
import os
import sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(SPEC))) if 'SPEC' in globals() else os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
release_build = os.environ.get('PBB_RELEASE_BUILD_DIR') or os.path.join(REPO, 'build')


a = Analysis(
    [os.path.join(REPO, 'pbb', 'gui.py')],
    pathex=[REPO],
    # 引擎、Python 字符集模块及各自的 .build.json 清单：打包后落在 _internal/build/。
    binaries=[(os.path.join(release_build, 'pbb_core.cp312-win_amd64.pyd'), 'build'),
              (os.path.join(release_build, 'pbb_ui_engine.exe'), 'build')],
    # 运行期资源与随包源码：
    #   fonts/、assets/ 解包到 _internal/，pbb/paths.py 按 sys._MEIPASS 定位它们；
    #   pbb/native/ 是 C++ 源码，随监控器提供，供用户在解压目录里用 rebuild.bat 重建引擎。
    datas=[(os.path.join(REPO, 'fonts'), 'fonts'), (os.path.join(REPO, 'assets'), 'assets'),
           (os.path.join(release_build, 'pbb_core.cp312-win_amd64.pyd.build.json'), 'build'),
           (os.path.join(release_build, 'pbb_ui_engine.exe.build.json'), 'build'),
           (os.path.join(REPO, 'pbb', 'native'), os.path.join('pbb', 'native'))],
    # 显式声明 pbb 包内的子模块：入口 pbb/gui.py 里用的是延迟导入
    # （函数内部 import），静态分析不一定收集得到，漏掉就会在冻结后运行时才炸。
    hiddenimports=['pbb_core', 'yaml', 'pbb.ui', 'pbb.main', 'pbb.engine',
                   'pbb.paths', 'pbb.build_settings', 'pbb.config_schema', 'pbb._build',
                   'pbb.flex', 'pbb.threading_config'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PBBMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(REPO, 'assets', 'icon.ico')],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PBBMonitor',
)

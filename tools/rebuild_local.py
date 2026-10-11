"""Rebuild the offline package for this CPU, then update its monitor binaries.

中文: 重建入口 —— 按本机 CPU 重新编译普通引擎、UI 引擎与 Python 核心,
若存在监控器包(PBBMonitor/_internal), 再把这三个二进制同步进去。
发布包的 rebuild.bat 与 UI 的 Rebuild 按钮最终都走这里。
"""
import argparse
import os
from pathlib import Path
import shutil
import sys

# 本文件在 tools/ 下，需要把仓库根加入 sys.path 才能 import pbb 包。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbb import _build as build
from pbb import build_settings, paths


def monitor_build_dir():
    """定位 <包根>/PBBMonitor/_internal/build；不存在时返回一个不存在的路径。

    由调用方走「没有监控器可同步」的分支。为什么用 package_root 而不是数据根的上一级：
    开发仓库里数据根就是包根（没有 PBBMonitor 要同步）；发布布局里包根是 source/ 的
    上一级，config/、runtime/ 与 PBBMonitor/ 都在那里。早期版本写死 `.parent`，
    仓库上移到根之后会指向 D:\\namerena 而静默失败。
    """
    root = build_settings.package_root(paths.DATA_ROOT)
    return root / "PBBMonitor" / "_internal" / "build"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-sync", action="store_true")
    args = parser.parse_args()
    # 强制 native 模式, 并清掉 PBB_SRC_DIR / PBB_CXXFLAGS 覆盖: 本脚本的语义是
    # "按本机当前能力重建正式二进制", 不接受上一次调优或实验残留的环境变量。
    os.environ["PBB_BUILD_MODE"] = "native"
    os.environ.pop("PBB_SRC_DIR", None)
    os.environ.pop("PBB_CXXFLAGS", None)
    # 重建普通引擎 + Python 核心(rebuild=True 无条件重编, 不看复用清单)。
    build.ensure_all(rebuild=True, verbose=True)
    ui_engine = str(paths.engine_binary(ui=True))
    # UI 引擎与普通引擎同源但目标宏不同(PBB_UI_ENGINE=1), 必须单独编一份,
    # 并用 output_path 指定产物名, 避免覆盖默认的 pbb_engine.exe。
    build._compile_engine(verbose=True, output_path=ui_engine, ui_engine=True)
    destination = monitor_build_dir()
    if args.no_sync:
        # --no-sync: 离线包内验证用 —— 只重建制品, 不同步监控器(那时监控器尚未就位)。
        pass
    elif destination.is_dir():
        # 同步 UI 引擎与 Python 核心(含 .build.json 清单, 否则监控器下次启动会误判过期)。
        for binary in (Path(ui_engine), Path(build._pbb_core_path())):
            for source in (binary, Path(str(binary) + ".build.json")):
                try:
                    shutil.copy2(source, destination / source.name)
                except PermissionError as exc:
                    # 监控器正在运行会锁住 _internal/build 下的文件; 给出可操作提示,
                    # 而不是抛出裸的 PermissionError。
                    raise RuntimeError("Close PBBMonitor before updating its binaries, then run rebuild.bat again.") from exc
    else:
        print(f"No monitor bundle at {destination}; skipped synchronising PBBMonitor binaries.")
    print("Rebuild complete: standard engine, UI engine, and Python core.")


if __name__ == "__main__":
    main()

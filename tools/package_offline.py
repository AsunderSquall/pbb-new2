"""Build and verify one relocatable Windows package with Python and GCC.

中文: 生成并验证 "environment" 组件(自包含的 Windows 离线包): 把随包 Python 运行时、
MinGW GCC 的依赖闭包、源码与配置合并成一棵可解压即用的目录树, 然后在"剥离系统
Python / 编译器路径"的环境里真跑一遍重建与测试, 最后压成
release/PBB-windows-x64-environment.zip。该产物是独立组件(只有 runtime/),
不是 pure 包的增量或补丁。
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import zipfile

# 本文件在 tools/ 下，需要把仓库根加入 sys.path 才能 import pbb 包与本目录的兄弟模块。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbb import _build as build
from pbb import build_settings, paths
from _archive import write_zip_atomic
import package_release


def package_sections(path):
    # 解析 MSYS2/pacman 的 desc 与 files 文件: 内容按 "%字段名%" 行分段,
    # 同一字段可能出现多次, 所以值是列表(NAME/VERSION 取 [0], FILES 整体遍历)。
    sections = {}
    key = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("%") and line.endswith("%"):
            key = line[1:-1]
            sections[key] = []
        elif key and line:
            sections[key].append(line)
    return sections


def toolchain_files(root):
    # 计算 GCC 的依赖闭包, 并列出需要随包分发的文件。
    # 为什么要算闭包: 只拷 g++.exe 远远不够, GCC 还依赖 binutils、运行库、头文件等
    # 若干 MSYS2 包; 发布包必须能脱离本机 MSYS2 独立编译。
    # 返回 (按相对路径排序的 [(相对路径, 源文件)], 被选中的包 [(目录, 元数据)]）。
    database = root.parent / "var/lib/pacman/local"
    if not database.is_dir():
        # MSYS2 的 pacman 数据库不在时, 退回发布包内精简的 package-metadata 目录。
        database = root / "package-metadata"
    packages = {}
    for directory in database.iterdir():
        if (directory / "desc").is_file():
            metadata = package_sections(directory / "desc")
            packages[metadata["NAME"][0]] = (directory, metadata)
            # 依赖里可能写的是虚拟名(provides), 需要能解析回真实包名, 否则闭包会漏包。
            for alias in metadata.get("PROVIDES", []):
                packages.setdefault(re.split(r"[<>=]", alias)[0], (directory, metadata))
    # 从三个顶层依赖出发, 沿 DEPENDS 中的 mingw-w64-x86_64-* 继续展开(栈式遍历)。
    pending = ["mingw-w64-x86_64-gcc", "mingw-w64-x86_64-binutils", "mingw-w64-x86_64-tools-git"]
    selected = set()
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        # 依赖未安装就直接报错: 少一个包会编不出引擎, 不能静默产出残缺发布包。
        if name not in packages:
            raise RuntimeError(f"Missing installed toolchain dependency: {name}")
        name = packages[name][1]["NAME"][0]
        if name in selected:
            continue
        selected.add(name)
        pending.extend(re.split(r"[<>=]", item)[0] for item in packages[name][1].get("DEPENDS", [])
                       if item.startswith("mingw-w64-x86_64-"))
    files = {}
    for name in selected:
        directory, _ = packages[name]
        # 只收普通文件, 且必须是本工具链根目录下的条目 —— 跳过目录项与其它前缀。
        for item in package_sections(directory / "files").get("FILES", []):
            path = Path(item)
            if item.endswith("/") or not path.parts or path.parts[0] != root.name:
                continue
            # 跳过 Python 字节码：MSYS2 的 pacman 元数据里有 __pycache__/*.pyc 条目，
            # 但随包工具链里这些文件未必存在（也可能被用户清理过），而且拷贝与压缩
            # 阶段本来就会把它们过滤掉。让它们参与"文件必须存在"的校验只会误报。
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            source = root.parent / path
            if not source.is_file():
                # Some installations omit the unprefixed binutils aliases.
                # 中文: 个别安装缺少无前缀的 binutils 别名(如 bin/as.exe), 这时从
                # x86_64-w64-mingw32/bin 取同名文件; 两边都没有才算真缺文件。
                alias = root / "x86_64-w64-mingw32/bin" / source.name
                if path.parts[:2] != (root.name, "bin") or not alias.is_file():
                    raise RuntimeError(f"Missing installed toolchain file: {source}")
                source = alias
            files[path.relative_to(root.name)] = source
    # 排序输出: 让打包顺序与 zip 内容可复现, 便于比对两次发布包的差异。
    return sorted(files.items()), [packages[name] for name in sorted(selected)]


def copy_file(source, destination):
    # 统一拷贝入口: 自动创建父目录, 用 copy2 保留时间戳/权限, 便于发布包内文件可溯源。
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_python(destination):
    # 拷贝整个基础解释器(base_prefix 而非 prefix: venv 里没有 libs/ 与完整标准库),
    # 再把当前环境的 site-packages 叠加上去, 得到自包含的 Python 运行时。
    base = Path(sys.base_prefix)
    def ignore(_directory, names):
        # 忽略的都是"搬到别的机器就没意义或会出错"的东西:
        #   __pycache__/*.pyc  字节码与解释器版本强绑定, 只占空间;
        #   _virtualenv.*      记录本机 venv 的绝对路径, 会污染目标机的 sys.path;
        #   Scripts/           venv 生成的启动器包装(内嵌绝对路径), 不可重定位。
        return [name for name in names if name == "__pycache__" or name.endswith(".pyc")
                or name in ("_virtualenv.pth", "_virtualenv.py")
                or (Path(_directory) == base and name == "Scripts")]
    shutil.copytree(base, destination, ignore=ignore)
    # Venv executable wrappers contain absolute paths; module invocation is portable.
    # 中文: 第三方依赖(含 pybind11)另拷一份到 Lib/site-packages; 目标机一律用
    # "python -m xxx" 调用, 因此不需要 venv 的 Scripts/ 包装器。
    packages = Path(sysconfig.get_path("purelib"))
    shutil.copytree(packages, destination / "Lib/site-packages", dirs_exist_ok=True, ignore=ignore)
    # 最低限度自检: 缺 python312.dll 或 include/libs 会在用户机器上拖到重建时才报错,
    # 所以在这里就把不完整的运行时挡掉。
    for required in ("python.exe", "pythonw.exe", "python312.dll", "include/Python.h", "libs/python312.lib"):
        if not (destination / required).is_file():
            raise RuntimeError(f"Python runtime is incomplete: {required}")


def copy_baseline_binaries(destination):
    # 拷贝三个"保守基线"二进制(普通引擎/UI 引擎/Python 核心)及其 .build.json 清单。
    # 清单必须一起拷: 目标机 Rebuild 时靠它判断能否复用, 缺了会被当成过期二进制,
    # 导致离线包首次启动就触发一次编译。
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("pbb_engine.exe", "pbb_ui_engine.exe", os.path.basename(build._pbb_core_path())):
        for suffix in ("", ".build.json"):
            copy_file(Path(build.BUILD_DIR) / (name + suffix), destination / (name + suffix))


def run_verified(command, cwd, environment):
    # 统一的"必须成功"执行器: 捕获输出并设 600 秒上限; 失败时把尾部日志带进异常,
    # 否则离线验证失败只剩一个返回码, 无法定位是哪一步坏了。
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=600,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(f"Offline verification failed: {command}\n{result.stdout[-4000:]}\n{result.stderr[-6000:]}")
    return result


def _force_utf8_stdio():
    """把 stdout/stderr 固定成 UTF-8（遇到无法编码的字符替换而不是抛异常）。

    为什么需要：打包日志里会原样转发子进程的输出，其中可能含 Windows 控制台代码页
    解出来的替换字符（U+FFFD）。默认 stdout 若是 GBK，print 这类字符会直接
    UnicodeEncodeError 把整个打包流程打断 —— 而那与包本身是否成功无关。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main():
    _force_utf8_stdio()
    # --skip-build: 复用已生成的 pure 包(跳过 package_release);
    # --resume-stage: 从已铺好的发布目录继续, 省掉几百 MB 的重复拷贝;
    # --mingw-root: 指定要随包的 MSYS2 工具链根, 默认优先用随包工具链,
    # 否则退回 MSYS2 的默认安装位置。
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--resume-stage")
    parser.add_argument("--mingw-root", default=build._bundled_toolchain() or r"C:\msys64\mingw64")
    args = parser.parse_args()
    # 硬约束: 本离线包只做 Windows x64 + Python 3.12(与 PBBMonitor.spec 一致)。
    # 其它组合下拷出的 DLL/头文件与监控器不匹配, 直接拒绝而不是产出坏包。
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        raise SystemExit("This offline release currently requires Windows x64 Python 3.12")
    # 后续所有相对路径(pbb/、tools/、tests/、offline/、config/、release/)都以仓库根为基准。
    os.chdir(paths.REPO_ROOT)
    toolchain = Path(args.mingw_root).resolve()
    files, packages = toolchain_files(toolchain)
    # 先打印闭包规模: 文件数/体积异常通常意味着 pacman 数据库没读到。
    print(f"[offline] GCC dependency closure: {len(packages)} packages, {len(files)} files, "
          f"{sum(path.stat().st_size for _, path in files) / 1048576:.1f} MB", flush=True)
    if not args.skip_build:
        # 先生成 pure 包(监控器 + 源码 + 配置), environment 包要在它的产物之上合并布局。
        package_release.main()
    # 中间目录名带时间戳(含微秒): 连续两次运行不会互相覆盖。
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    root = Path(args.resume_stage) if args.resume_stage else Path("release_stage") / ("offline verify " + stamp) / "PBB"
    if args.resume_stage:
        # 只接受 release_stage 下、且已铺好 Python 的目录, 防止误指向别处后被覆盖。
        if not root.resolve().is_relative_to(Path("release_stage").resolve()) or not (root / "runtime/python/python.exe").is_file():
            raise RuntimeError("Resume stage must be an existing generated release_stage/PBB directory")
    else:
        root.mkdir(parents=True)
    # ui_dist.txt 由 package_release 写出, 记录 PyInstaller 的产物目录; 监控器包从这里取。
    distribution = Path(paths.BUILD_DIR, "ui_dist.txt").read_text().strip()
    shutil.copytree(Path(distribution) / "PBBMonitor", root / "PBBMonitor", dirs_exist_ok=True)
    # 复用 package_release 的白名单取源码, 保证 pure 与 environment 两个包的源码内容一致。
    for path in package_release.source_paths():
        copy_file(path, root / "source" / path)
    # 配置统一放在发布根的 config/; source/ 与 PBBMonitor/_internal/ 里不留副本。
    package_release.stage_configs(root)
    # offline/ 是发布包根目录模板: README/ENVIRONMENT.md 与 8 个外壳脚本。
    for path in Path("offline").iterdir():
        copy_file(path, root / path.name)
    # 先放一份保守基线二进制; 后面验证时会用随包工具链重新编译并覆盖它们。
    copy_baseline_binaries(root / "source/build")
    if not args.resume_stage:
        # 首次铺目录才需要拷运行时; --resume-stage 时这些已就位, 跳过以节省时间。
        print("[offline] Copying Python runtime and installed dependencies...", flush=True)
        copy_python(root / "runtime/python")
        print("[offline] Copying GCC, headers, link libraries, tools and licenses...", flush=True)
        # 按相对路径还原工具链的目录结构: runtime/mingw64/<相对路径>。
        for relative, path in files:
            copy_file(path, root / "runtime/mingw64" / relative)
        # 保留原始 pacman 元数据: 既是许可与来源审计凭据, 也是包内再次解析依赖的依据。
        for directory, metadata in packages:
            shutil.copytree(directory, root / "runtime/mingw64/package-metadata" / directory.name)
    # 最终校验六个关键可执行文件确实落地, 把"拷贝静默失败"变成明确报错。
    for name in ("g++.exe", "gcc.exe", "as.exe", "ld.exe", "gendef.exe", "dlltool.exe"):
        if not (root / "runtime/mingw64/bin" / name).is_file():
            raise RuntimeError(f"Missing bundled compiler tool: {name}")
    # 第三方组件清单(THIRD_PARTY.md): 版本号取自 pacman 元数据并逐包列名,
    # 满足再分发时的许可提示与来源说明要求。
    notices = ["# Third-Party Components", "", f"Python: {sys.version.split()[0]}; license: runtime/python/LICENSE.txt.",
               "Python dependencies and their licenses: runtime/python/Lib/site-packages/*.dist-info/.",
               "GCC dependency package licenses: runtime/mingw64/share/licenses/.",
               "Original MSYS2 package metadata: runtime/mingw64/package-metadata/.",
               "Font license: source/fonts/.", "", "Included toolchain packages:", ""]
    notices += [f"- {metadata['NAME'][0]} {metadata['VERSION'][0]}" for _, metadata in packages]
    notices += ["", "Third-party compiler source archives are not included. Review the original licenses",
                "and corresponding-source requirements before redistributing this bundle."]
    (root / "THIRD_PARTY.md").write_text("\n".join(notices) + "\n", encoding="utf-8")
    # 验证环境: PATH 只留随包工具链 + 随包 Python + 系统目录, 系统里装的 Python/GCC
    # 一律不可见 —— 这样"离线包能否独立工作"才是真的验证过。
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join([str((root / "runtime/mingw64/bin").resolve()),
                                          str((root / "runtime/python").resolve()),
                                          os.path.join(os.environ["SystemRoot"], "System32"), os.environ["SystemRoot"]])
    # 让 build.py 用随包工具链, 而不是用户 PATH 里的编译器。
    environment["PBB_TOOLCHAIN_DIR"] = str((root / "runtime/mingw64").resolve())
    # 禁止加载用户 site-packages; 并清掉会改变解释器行为(PYTHONHOME/PYTHONPATH)
    # 或改变编译参数(PBB_SRC_DIR/PBB_CXXFLAGS)的宿主变量, 保证构建可复现。
    environment["PYTHONNOUSERSITE"] = "1"
    for key in ("PYTHONHOME", "PYTHONPATH", "PBB_SRC_DIR", "PBB_CXXFLAGS"):
        environment.pop(key, None)
    python = str((root / "runtime/python/python.exe").resolve())
    source = (root / "source").resolve()
    print("[offline] Verifying with system Python/compiler paths removed...", flush=True)
    # 冒烟测试: 随包 Python 能 import 全部关键依赖(含 tkinter, 监控器 UI 需要),
    # 且 sysconfig/pybind11 的路径都指向随包目录而不是开发机上的安装。
    smoke = run_verified([python, "-c", "import sys,sysconfig,pybind11,yaml,tkinter; "
                          "print(sys.executable); print(sysconfig.get_path('include')); "
                          "print(pybind11.get_include()); root=tkinter.Tk(); root.withdraw(); root.destroy()"],
                         source, environment)
    print(smoke.stdout.strip(), flush=True)
    # 用随包 Python + GCC 重建三个原生二进制: 证明工具链自洽, 而不是靠开发机上的编译器。
    print("[offline] Rebuilding all three native binaries with bundled Python and GCC...", flush=True)
    run_verified([python, "tools/rebuild_local.py", "--no-sync"], source, environment)
    # 真跑一次小规模测号(range-end 100 万 + 固定种子): 覆盖配置解析、引擎执行与结果输出。
    print("[offline] Running main.py with the rebuilt Python core...", flush=True)
    run_verified([python, "main.py", "-c", "../config/config_football.yaml", "--threads", "2",
                  "--range-end", "1000000", "--seed", "123"], source, environment)
    print("[offline] Testing UI events, ordinary engine, editor and build safeguards...", flush=True)
    # 测试文件在 source/tests/ 下，用 discover 运行；各测试自带 sys.path 引导，
    # 保证在被搬进发布包的 source/ 目录后仍能 import pbb 包与 tools/ 下的模块。
    result = run_verified([python, "-m", "unittest", "discover", "-s", "tests", "-t", "tests"],
                          source, environment)
    # unittest 的测试结果写在 stderr 上, 所以这里打印 stderr 而不是 stdout。
    print(result.stderr.strip(), flush=True)
    # The distributed binaries must stay conservative, not tuned for this build host.
    # 中文: 上面的重建会按本机 CPU 能力生成二进制, 而发布物必须是保守基线;
    # 因此在写 manifest 与压缩之前, 再用基线二进制覆盖回去。
    copy_baseline_binaries(root / "source/build")
    # bundle.json: 机器可读的包信息(版本/平台/构建模式/验证方式/工具链包清单)。
    manifest = {"python": sys.version.split()[0], "platform": "windows-x64", "binary_mode": "portable",
                "verification": "offline native rebuild plus test suite passed",
                "toolchain_packages": [metadata["NAME"][0] for _, metadata in packages]}
    (root / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="ascii")
    archive_path = Path("release/PBB-windows-x64-environment.zip")
    print("[offline] Compressing the independent Python/GCC environment...", flush=True)

    # compresslevel=6: 环境包约 600 MB, 取默认级别之上的一档做速度/体积折中。
    # write_zip_atomic 负责"先写临时文件再原子替换 + 锁重试"，见 tools/_archive.py
    # （目标 zip 常被杀软/索引服务占用，直接打开会 PermissionError 让构建白做）。
    def fill(archive):
        # environment 包只装 runtime/: 应用本体在 pure 包里, 用户把两个 zip 解到同一
        # 父目录后合并; 顶层统一用 PBB/ 才能合并成同一棵树。
        for path in sorted((root/'runtime').rglob("*")):
            relative = path.relative_to(root)
            # 压缩前再过滤一次字节码(拷贝之后才可能产生 __pycache__/*.pyc)。
            if not path.is_file() or "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            archive.write(path, Path("PBB") / relative)
        # 这三份说明/清单描述整个包(而非 runtime/), 因此单独写进包根。
        archive.write('offline/ENVIRONMENT.md','PBB/README_ENVIRONMENT.md')
        archive.write(root/'THIRD_PARTY.md','PBB/THIRD_PARTY_ENVIRONMENT.md')
        archive.writestr('PBB/environment.json',json.dumps(dict(python=sys.version.split()[0],
            platform='windows-x64',toolchain_packages=manifest['toolchain_packages']),indent=2))

    archive_path = write_zip_atomic(archive_path, fill, compresslevel=6)
    print(f"[offline] Complete: {archive_path.resolve()} ({archive_path.stat().st_size / 1048576:.1f} MB)", flush=True)

if __name__ == "__main__":
    main()

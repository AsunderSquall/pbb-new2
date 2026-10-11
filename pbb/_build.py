#!/usr/bin/env python3
"""
PBB build system — compiler detection, SIMD probing, unified compilation.

Called by main.py via ensure_all(rebuild, verbose).
Not meant to be run directly — use ./run.sh or run.bat.

中文说明: PBB 构建系统 —— 编译器探测、SIMD 能力选择与统一编译入口。
由 main.py 通过 ensure_all(rebuild, verbose) 调用, 不作为独立脚本直接运行;
真正需要编译时走 ./run.sh 或 run.bat(它们会先补齐 Python 依赖)。
编译决策顺序: 环境变量 (PBB_BUILD_MODE / PBB_CXXFLAGS / PBB_SRC_DIR) >
本机测速结果 (config/build_config.json) > CPU 与编译器自动探测。
"""
import os, sys, shutil, subprocess, tempfile, sysconfig
import glob, hashlib, json, platform
from functools import lru_cache

from . import build_settings, paths

# BASE_DIR 保留为"仓库根"的别名：构建选项（config/build_config.json）以它为基准定位，
# 编译器命令行也在 chdir 到这里之后执行，因此它必须与 package_release.py 的
# os.chdir 目标一致。真实定义已收拢到 pbb/paths.py，此处不再自己用 __file__ 推导 ——
# 本模块在 pbb/ 包里，用 __file__ 推导会算成 pbb/ 而不是仓库根。
BASE_DIR = str(paths.REPO_ROOT)
# 全部编译产物集中在此目录: 三个二进制本体与它们的 .build.json 清单、mingw_lib 链接缓存、
# autotune 记录。该目录已加入 .gitignore; package_offline.py 也直接从这里取基线二进制。
BUILD_DIR = str(paths.BUILD_DIR)


def _build_mode():
    # PBB_BUILD_MODE 只接受 native/portable, 缺省 native。
    # native  = 按编译机实际 CPU 优化(-march=native 等), 产物只允许在本机复用;
    # portable= 面向 x86-64 基线(SSE2)的保守参数, 产物允许跨同为 Windows x64 的机器复用。
    # 取值非法时直接抛错, 避免环境变量拼错后静默退回到另一套参数。
    mode = os.environ.get("PBB_BUILD_MODE", "native").strip().lower()
    if mode not in ("native", "portable"):
        raise ValueError("PBB_BUILD_MODE must be native or portable")
    return mode


def _machine_identity():
    # Native builds may contain more ISA extensions than their SIMD label.
    # Bind reuse to the build host until portable ISA baselines are distributed.
    # 中文: native 产物可能含有超出其 SIMD 标签的指令(例如 -march=native 顺带开了别的扩展),
    # 因此把"能否复用"绑定到编译主机本身: 主机名/架构/处理器型号/平台任一变化即视为失效。
    return [platform.node(), platform.machine(), platform.processor(),
            os.environ.get("PROCESSOR_IDENTIFIER", ""), sys.platform]


def _binary_digest(path):
    # 按文件内容算 sha256。hasattr 分支是兼容旧解释器: file_digest 需要 Python 3.11+,
    # 更老的环境退回一次性读入内存。
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(fh.read()).hexdigest()


def _record_binary(path, compiler, flags, engine_target=None):
    # 在二进制旁写 <binary>.build.json 二进制清单, 供下次构建判断能否复用。
    # 字段含义:
    #   version       清单格式版本, 只有 2 被认为有效;
    #   mode          native / portable, 决定下方用哪一组兼容性依据;
    #   machine       编译主机指纹, 仅 native 构建比对(换机器即失效);
    #   platform      [sys.platform, 机器架构], 仅 portable 构建比对(可跨机器);
    #   source        编译时选定的头文件树(PBB_SRC_DIR 或默认 "src");
    #   digest        二进制内容的 sha256, 用于发现文件被替换或损坏;
    #   compiler/flags 实际使用的编译器与参数(供排查, 不参与复用判定);
    #   engine_target standard / ui, 两个目标变体不可互用;
    #   build_config  本机测速配置签名, 配置一变引擎就必须重建。
    metadata = dict(version=2, mode=_build_mode(), machine=_machine_identity(),
                    platform=[sys.platform, platform.machine()], source=_engine_source_name(),
                    digest=_binary_digest(path), compiler=compiler, flags=flags)
    if engine_target is not None:
        # 只有引擎产物才带目标变体与测速签名: pbb_core 是纯桥接层, 与调优开关无关。
        metadata["engine_target"] = engine_target
        metadata['build_config'] = _build_config_signature(compiler)
    with open(path + ".build.json", "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, ensure_ascii=True)


def _binary_is_current(path, engine_target=None):
    # 判定已有二进制能否直接复用; 返回 False 就重编。任一条件不成立即作废:
    #   1) 清单存在、可解析且 version == 2;
    #   2) 兼容性: portable 只要求平台一致(允许换机器), native 必须回到原编译主机;
    #   3) engine_target 与本次要编的目标一致(standard 与 ui 不能互换);
    #   4) 头文件树与当前 PBB_SRC_DIR 选择一致;
    #   5) build_config 测速签名一致(调优结果变了就必须重建才生效);
    #   6) 二进制内容哈希与清单记录一致(被手工替换/损坏即失效)。
    # 清单缺失、JSON 损坏、权限异常等一律按"不当前"处理, 交给调用方重编。
    try:
        with open(path + ".build.json", encoding="utf-8") as fh:
            metadata = json.load(fh)
        # portable 产物只看平台; native 产物要求本机指纹完全相同。
        compatible = (metadata.get("mode") == "portable" and
                      metadata.get("platform") == [sys.platform, platform.machine()])
        compatible = compatible or (metadata.get("mode") == "native" and
                                     metadata.get("machine") == _machine_identity())
        return (metadata.get("version") == 2 and compatible
                and (engine_target is None or metadata.get("engine_target") == engine_target)
                and metadata.get("source") == _engine_source_name()
                # 未指定 engine_target 的产物(pbb_core)不参与测速签名的比对。
                and (engine_target is None or metadata.get('build_config') == _build_config_signature())
                and metadata.get("digest") == _binary_digest(path))
    except (OSError, ValueError, TypeError):
        return False


def _build_config_signature(compiler=None):
    # 返回当前生效的测速配置签名; None 表示"本次构建不采用本机测速结果"。
    # 三种情况直接返回 None: portable 模式(发布物不绑定本机选择)、编译器不是 g++
    # (调优只针对 GCC)、或用户用 PBB_CXXFLAGS/PBB_SRC_DIR 显式覆盖参数
    # (这种产物的参数由用户负责, 不应因配置文件变化而被迫重建)。
    if (_build_mode()!='native' or (compiler is not None and 'g++' not in os.path.basename(compiler).lower())
            or os.environ.get('PBB_CXXFLAGS','').strip() or os.environ.get('PBB_SRC_DIR','').strip()):
        return None
    profile,active=build_settings.load(BASE_DIR,_machine_identity(),compiler)
    return build_settings.signature(profile,active)


def ensure_core(verbose=False):
    """Validate before importing native code into the Python/UI process."""
    # 中文: 在 Python/UI 进程 import 原生扩展之前先做校验, 保证拿到的 pbb_core
    # 确实是为本机、当前源码编出来的。冻结(PyInstaller)环境里没有编译器,
    # 只能明确报错要求用户在目标机器上从源码重编, 而不是留下一个后续才崩的扩展。
    paths.ensure_build_dir()
    if not _binary_is_current(_pbb_core_path()):
        if getattr(sys, "frozen", False):
            raise RuntimeError("pbb_core 的构建来源或机器不匹配，请在目标机器从源码重编后使用")
        _compile_pbb_core(verbose)


@lru_cache(maxsize=None)
def _compiler_environment(compiler):
    # 返回调用该编译器所需的环境变量。
    # 普通编译器沿用当前环境即可; Windows 上传绝对路径时要把该目录插到 PATH 最前,
    # 否则编译器找不到与它同目录的 cc1/as 等内部工具。
    if sys.platform != "win32" or "icpx" not in os.path.basename(compiler).lower():
        environment = os.environ.copy()
        if sys.platform == "win32" and os.path.isabs(compiler):
            environment["PATH"] = os.path.dirname(compiler) + os.pathsep + environment.get("PATH", "")
        return environment
    # icpx 依赖 oneAPI 的 setvars 脚本注入 INCLUDE/LIB/DLL 搜索路径, 仅凭可执行文件
    # 无法编译。这里沿其所在目录向上逐级找初始化脚本, 执行 `call ... && set` 抓取
    # 完整环境; 找不到脚本(用户已在初始化过的开发者 shell 里)就沿用当前环境。
    # 结果按编译器路径缓存, 同一会话只探测一次。
    directory = os.path.dirname(os.path.abspath(compiler))
    while directory != os.path.dirname(directory):
        for script in ("setvars.bat", "oneapi-vars.bat"):
            path = os.path.join(directory, script)
            if os.path.isfile(path):
                result = subprocess.run(
                    [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c",
                     f'call "{path}" >nul && set'], capture_output=True, text=True,
                    errors="replace", timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode:
                    raise RuntimeError(f"oneAPI 环境初始化失败: {result.stderr[-500:]}")
                env = os.environ.copy()
                for line in result.stdout.splitlines():
                    key, sep, value = line.partition("=")
                    if sep and key: env[key] = value
                return env
        directory = os.path.dirname(directory)
    # An already initialized developer shell may not expose the install root.
    return os.environ.copy()


def _run_compiler(command, **kwargs):
    # 编译器调用的唯一出口: 注入环境、抑制控制台窗口, 并把"进程根本没起来"也
    # 归一成非零返回码。这样 _compile_pbb_core/_compile_engine 的"换下一个编译器"
    # 循环只有一条失败路径, 不必在每处 try。
    try:
        return subprocess.run(command, env=_compiler_environment(command[0]),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=str(exc))


def check_python():
    # 运行期要求 Python 3.10+: 代码里用到 list[str] 等 3.10 才稳定的写法。
    # 版本不足直接退出, 而不是等到语法/运行时错误。
    if sys.version_info < (3, 10):
        print("ERROR: Python 3.10+ required", file=sys.stderr)
        sys.exit(1)


def check_deps():
    # pybind11 是编译 pbb_core 的硬依赖, 缺失必须提前失败; PyYAML 只影响配置文件
    # 支持, 缺失可接受。这里只做检查不做安装 —— 安装交给 run.sh/run.bat, 保持本模块
    # 无副作用(它也会被打包脚本 import)。
    missing = []
    try: import pybind11
    except ImportError: missing.append("pybind11")
    try: import yaml
    except ImportError: pass  # optional
    if missing:
        print(f"ERROR: Missing Python packages: {', '.join(missing)}", file=sys.stderr)
        print("  Use run.sh or run.bat to auto-install", file=sys.stderr)
        sys.exit(1)


def _engine_bin():
    # 默认(standard)引擎产物的路径; .exe 后缀只在 Windows 上加。
    name = "pbb_engine" + (".exe" if sys.platform == "win32" else "")
    return os.path.join(BUILD_DIR, name)


def _engine_source_stamp():
    """Sidecar recording which include tree produced the shared engine binary.

    中文: 旁车文件, 记录这份共享引擎二进制是由哪棵头文件树(PBB_SRC_DIR)编出来的。
    默认产物同时被 main.py 与 UI 使用, 必须能识别它到底对应哪个版本目录。
    """
    return _engine_bin() + ".source"


def _engine_source_name():
    # PBB_SRC_DIR 为空表示使用正式 src/ 头文件树, 否则是 src/ 下的历史版本子目录名。
    return os.environ.get("PBB_SRC_DIR", "").strip() or "src"


def _engine_matches_selected_source():
    # 读旁车文件, 判断现有引擎是否由当前选定的头文件树编译; 缺失/读取失败视为不匹配。
    try:
        with open(_engine_source_stamp(), encoding="utf-8") as f:
            return f.read().strip() == _engine_source_name()
    except OSError:
        return False


def _pbb_core_path():
    # 扩展模块后缀由解释器/平台决定(如 .cp312-win_amd64.pyd、.cpython-312-x86_64-linux-gnu.so),
    # 必须用 sysconfig 的 EXT_SUFFIX 才能让生成的文件名与 import pbb_core 期望的名字一致;
    # 取不到时按平台兜底为 .pyd/.so。
    ext = sysconfig.get_config_var("EXT_SUFFIX") or (".pyd" if sys.platform == "win32" else ".so")
    return os.path.join(BUILD_DIR, "pbb_core" + ext)


def _pbb_core_exists():
    return os.path.exists(_pbb_core_path())


def _compiler_probe(compiler, flag):
    # 只回答一个问题: "这个编译器接受这个 flag 吗" —— 编译一个空 main 的最小源文件。
    # 它不代表本机 CPU 能执行该指令; CPU 侧的真实能力判定是 _cpu_supports。
    # MSVC 与 GCC 的写法差异(标准开关、输出对象、/Fo)在此分流; 临时文件无论成败都清理。
    src = tempfile.NamedTemporaryFile(mode='w', suffix='.cpp', delete=False)
    src.write("int main(){return 0;}\n")
    src.close()
    obj = src.name + (".obj" if "cl" in os.path.basename(compiler) else ".o")
    is_msvc = os.path.basename(compiler).startswith("cl")
    std_flag = "/std:c++17" if is_msvc else "-std=c++17"
    try:
        command = ([compiler, std_flag, "/c", flag, src.name, "/Fo" + obj] if is_msvc else
                   [compiler, std_flag, "-c", flag, src.name, "-o", obj])
        r = _run_compiler(command,
                           capture_output=True, timeout=15, encoding='utf-8', errors='replace')
        return r.returncode == 0
    except:
        return False
    finally:
        for f in [src.name, obj]:
            if os.path.exists(f): os.unlink(f)


def _cpu_supports(compiler, feature):
    """真实检测「CPU」是否支持某指令集: 编译并运行 __builtin_cpu_supports 探针.

    与 _compiler_probe 的区别: 后者只验证编译器是否接受某个 flag, 不代表本机 CPU
    能执行该指令 (例如 Zen5 上的 g++ 接受 -mavx512f, 但 Intel Raptor Lake 跑不了).
    MSVC 使用 CPUID/XGETBV，检查 CPU 与操作系统是否都支持向量状态。
    编译或执行失败时返回 False。
    """
    is_msvc = os.path.basename(compiler).lower() in ("cl", "cl.exe")
    src = tempfile.NamedTemporaryFile(mode='w', suffix='.cpp', delete=False)
    if is_msvc:
        # MSVC 没有 __builtin_cpu_supports, 只能手写 CPUID: r[1] 取自 CPUID.7.0 的 EBX
        # 特性位; AVX/AVX-512 还要求操作系统已开启对应的向量状态保存(XGETBV 检查),
        # 否则即使 CPU 支持, 执行指令也会崩。
        checks = {"avx2": "(r[1] & (1<<5))", "fma": "fma",
                  "avx512f": "(r[1] & (1<<16)) && ((state & 0xe6) == 0xe6)",
                  "avx512bw": "(r[1] & (1<<30)) && ((state & 0xe6) == 0xe6)",
                  "avx512dq": "(r[1] & (1<<17)) && ((state & 0xe6) == 0xe6)",
                  "avx512cd": "(r[1] & (1<<28)) && ((state & 0xe6) == 0xe6)",
                  "avx512vl": "(r[1] & (1u<<31)) && ((state & 0xe6) == 0xe6)"}
        src.write('#include <intrin.h>\nint main(){ int r[4]; __cpuid(r,0); if(r[0]<7) return 1; '
                  '__cpuidex(r,1,0); bool fma=(r[2]&(1<<12))!=0; '
                  'if(!(r[2]&(1<<27)) || !(r[2]&(1<<28))) return 1; '
                  'unsigned long long state=_xgetbv(0); if((state&6)!=6) return 1; '
                  '__cpuidex(r,7,0); return (' + checks[feature] + ') ? 0 : 1; }\n')
    else:
        src.write('int main(){\n'
              '  __builtin_cpu_init();\n'
              f'  return __builtin_cpu_supports("{feature}") ? 0 : 1;\n'
              '}\n')
    src.close()
    exe = src.name + (".exe" if sys.platform == "win32" else ".bin")
    try:
        command = ([compiler, "/nologo", "/std:c++17", src.name, "/Fo" + src.name + ".obj", "/Fe" + exe]
                   if is_msvc else [compiler, "-std=c++17", src.name, "-o", exe])
        c = _run_compiler(command,
                           capture_output=True, timeout=30, encoding='utf-8', errors='replace')
        if c.returncode != 0:
            return False
        r = subprocess.run([exe], capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        for f in [src.name, exe, src.name + ".obj"]:
            if os.path.exists(f):
                try: os.unlink(f)
                except OSError: pass


def _detect_simd(compiler):
    """Returns (additional_flags, simd_name).

    基于「本机 CPU 真实能力」选择 SIMD 级别 (而非仅看编译器是否接受该 flag),
    避免在无 AVX-512 的 Intel 机器上编出非法指令而崩溃 (0xC000001D / SIGILL).

    两级判定: _compiler_probe 确认编译器认识这组参数, _cpu_supports 再确认本机
    CPU(及操作系统状态)真能执行。候选按能力从高到低排列, 取第一个两级都通过的方案。
    """
    is_win = sys.platform == "win32"
    is_msvc = os.path.basename(compiler).startswith("cl")
    is_icpx = "icpx" in os.path.basename(compiler).lower()

    if not is_win:
        # 非 Windows 的 ARM 机器直接用 NEON, 不走 x86 参数表。
        import platform
        machine = platform.machine()
        if machine.startswith("aarch") or machine.startswith("arm"):
            return ([], "NEON")

    if is_msvc:
        candidates = [(["/arch:AVX512"], "AVX-512", "avx512f"),
                      (["/arch:AVX2"], "AVX2", "avx2")]
    elif is_icpx:
        # Windows: keep the conservative AVX2 baseline for the unverified bridge.
        # 中文: Windows 上 icpx 走 Python 扩展桥接, -xHost/-xCORE-AVX512 未经验证,
        # 因此固定保守的 AVX2 基线; 非 Windows 才尝试 AVX-512。
        if is_win:
            candidates = [(["-xCORE-AVX2"], "AVX2", "avx2")]
        else:
            candidates = [(["-xCORE-AVX512"], "AVX-512", "avx512f"),
                          (["-xCORE-AVX2"], "AVX2", "avx2")]
    else:
        candidates = [(["-mavx512f", "-mavx512bw", "-mfma"], "AVX-512", "avx512f"),
                      (["-mavx2", "-mfma"], "AVX2", "avx2")]

    for flags, name, feat in candidates:
        # 同组 flag 通常同时可用, 只拿第一个去问编译器。
        if not _compiler_probe(compiler, flags[0]):
            continue
        # 除了主特性, 还要求 fma 与下一级指令集可用 —— 只满足其中之一就选整组会编出
        # 无法执行的二进制。AVX-512 另需 bw/avx2, MSVC 的 /arch:AVX512 还隐含 dq/cd/vl。
        required = [feat, "fma"]
        if feat == "avx512f": required += ["avx512bw", "avx2"]
        if feat == "avx512f" and is_msvc:
            required += ["avx512dq", "avx512cd", "avx512vl"]
        cpu_ok = all(_cpu_supports(compiler, feature) is True for feature in required)
        if cpu_ok:
            return (flags, name)

    # 都不满足则不加任何 SIMD 参数, 退回编译器默认的标量基线。
    return ([], "none")


def _gcc_arch_flags(compiler, verbose=False):
    """使用 -march=native: 让 GCC 按编译机实际 CPU 选择 ISA 与调度 (Intel/AMD 通用)."""
    # 先用一次探针确认编译器认识该 flag: 老版本或交叉工具链可能不支持,
    # 那种情况下宁可不做 CPU 优化, 也不能让整个构建失败。
    flag = "-march=native"
    if _compiler_probe(compiler, flag):
        return [flag], ", march=native"

    if verbose:
        print(f"[build] WARNING: {compiler} 不支持 {flag}; 跳过 CPU 优化", file=sys.stderr)
    return [], ""


def _bundled_toolchain():
    # 离线 environment 包把 MinGW 放在 runtime/mingw64。两种布局下 runtime/ 的位置不同：
    #   开发仓库：<仓库根>/runtime/           （通常不存在）
    #   发布布局：<包根>/runtime/，而源码在 <包根>/source/
    # 因此从数据根（paths.DATA_ROOT：开发仓库是仓库根，发布包是 .../source）逐级向上探，
    # 只要命中 bin/g++.exe 就算找到，避免把空目录当工具链。
    # PBB_TOOLCHAIN_DIR 优先级最高，可显式指定其它工具链。
    #
    # 另外跳过名字以 "_" 开头的祖先目录（例如历史发布布局的归档
    # _release_artifacts_*/runtime/mingw64）。那些归档是开发期的参考资料，
    # 不应该让"是否找到随包工具链"取决于它们是否还在磁盘上 —— 否则删掉归档
    # 会让同一份源码走到不同的编译路径。
    candidates = [os.environ.get("PBB_TOOLCHAIN_DIR", "")]
    directory = str(paths.DATA_ROOT)
    for _ in range(3):
        if not os.path.basename(directory).startswith("_"):
            candidates.append(os.path.join(directory, "runtime", "mingw64"))
        directory = os.path.dirname(directory)
    return next((path for path in candidates if path and os.path.isfile(os.path.join(path, "bin", "g++.exe"))), None)


def _find_compilers(verbose=False):
    """Detect all available compilers, return in performance order.
    Each entry: (name, flags, simd_name, is_msvc).
    Order: icpx > g++ > cl, regardless of platform or target.

    中文: 探测所有可用编译器并按性能优先级返回, 元素为
    (可执行文件, 参数列表, SIMD 标签, 是否 MSVC); 顺序固定 icpx > g++ > cl。
    调用方按此顺序逐个尝试, 第一个编译成功的胜出。
    """
    result = []
    is_win = sys.platform == "win32"

    # ---- Detect all compilers ----
    # 随包 MinGW 优先级最高: 只要 environment 包带了工具链就固定用它, 不再去找
    # icpx/cl —— 离线包的可复现性依赖"用哪套工具链"是确定的。
    bundled = _bundled_toolchain() if is_win else None
    icpx = None if bundled else shutil.which("icpx")
    gpp = os.path.join(bundled, "bin", "g++.exe") if bundled else shutil.which("g++")
    cl = shutil.which("cl") if is_win and not bundled else None

    if _build_mode() == "portable":
        # portable: 统一 x86-64 基线参数, 不带任何本机 ISA 与调度优化, 这样产物才能在
        # 别的 Windows x64 机器上复用; cl 用 /MT 静态运行库以免依赖额外运行时 DLL。
        entries = []
        if gpp:
            flags = ["-std=c++17", "-O3", "-ffast-math"]
            if platform.machine().lower() in ("amd64", "x86_64"):
                flags += ["-march=x86-64", "-mtune=generic"]
            entries.append((gpp, flags, "portable baseline", False))
        if cl:
            entries.append((cl, ["/std:c++17", "/Ox", "/EHsc", "/utf-8", "/w", "/MT"],
                            "portable baseline", True))
        return entries

    # icpx: search oneAPI install paths if not in PATH
    # icpx 常只在初始化过的 shell 里可见; 这里按 oneAPI 安装目录下的版本号倒序找最新一份。
    if not icpx and not bundled:
        for d in [r"C:\Program Files (x86)\Intel\oneAPI", r"C:\Program Files\Intel\oneAPI",
                  "/opt/intel/oneapi"]:
            versions = sorted(glob.glob(os.path.join(d, "compiler", "*")), reverse=True)
            for version_dir in versions:
                p = os.path.join(version_dir, "bin",
                                 "icpx.exe" if is_win else "icpx")
                if os.path.exists(p): icpx = p; break

    # ---- Build compiler entries ----
    entries = []

    if icpx:
        if is_win:
            # Keep a conservative shared baseline; bridge -xHost is unverified.
            # 中文: Windows 下 icpx 的 -xHost 桥接路径未经验证, 保持保守共享基线;
            # 探测本身也可能失败(oneAPI 环境不全), 那种情况退成不加 SIMD 参数。
            try:
                simd_flags, simd_name = _detect_simd(icpx)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                if verbose: print(f"[build] icpx unavailable: {exc}", file=sys.stderr)
                simd_flags, simd_name = [], "none"
            flags = ["-std=c++17", "-w", "-O3", "-ipo", "-ffast-math",
                     "-funroll-loops", "-qopt-mem-layout-trans=4", "-qopt-prefetch=5",
                     "-qopenmp"] + simd_flags
            entries.append((icpx, flags, simd_name, False))
        else:
            flags = ["-std=c++17", "-w", "-O3", "-ipo", "-ffast-math",
                     "-funroll-loops", "-qopt-mem-layout-trans=4", "-qopt-prefetch=5",
                     "-lpthread", "-xHost", "-finline-functions"]
            entries.append((icpx, flags, "auto (-xHost)", False))

    if gpp:
        # g++: 通用优化 + -march=native(编译器支持时) + 独立探测出的 SIMD 参数。
        base = ["-std=c++17", "-O3", "-funroll-loops", "-ffast-math"]
        arch_flags, arch_name = _gcc_arch_flags(gpp, verbose)
        simd_flags, simd_name = _detect_simd(gpp)
        entries.append((gpp, base + arch_flags + simd_flags, simd_name + arch_name, False))

    if cl:
        # MSVC 参数风格与 GCC 不同, SIMD 由 _detect_simd("cl") 给出 /arch: 系列。
        simd_flags, simd_name = _detect_simd("cl")
        entries.append(("cl", ["/std:c++17", "/Ox", "/EHsc", "/utf-8", "/w"] + simd_flags, simd_name, True))

    # ---- Order: icpx > g++ > cl on all platforms ----

    return entries  # default: icpx > g++ > cl


def _py_include():
    # Windows 官方安装版把 Python.h 放在 <base_prefix>/include, 优先用它;
    # 取不到时退回 sysconfig 的 INCLUDEPY(venv/conda/Unix 上更可靠), 最后再兜底拼路径。
    if sys.platform == "win32":
        local = os.path.join(sys.base_prefix, "include")
        if os.path.isdir(local):
            return [local]
    inc = sysconfig.get_config_var("INCLUDEPY")
    if inc: return [inc]
    return [os.path.join(sys.prefix, "include")]


def _py_link_flags():
    # MSVC 风格的 Python 链接参数。
    # Windows: 直接用 <base_prefix>/libs/python3xx.lib; 文件不存在时退成
    # "/LIBPATH:<dir> python3xx.lib" 交给链接器搜索。这里必须用 sys.base_prefix 而不是
    # sys.prefix —— venv 里没有 libs/, 只有基础解释器才有。
    if sys.platform == "win32":
        ver = f"{sys.version_info.major}{sys.version_info.minor}"
        libdir = os.path.join(sys.base_prefix, "libs")
        lib = os.path.join(libdir, f"python{ver}.lib")
        return [lib] if os.path.exists(lib) else [f"/LIBPATH:{libdir}", f"python{ver}.lib"]
    libdir = sysconfig.get_config_var("LIBDIR") or os.path.join(sys.prefix, "lib")
    ldflags = sysconfig.get_config_var("LDFLAGS") or ""
    flags = [f"-L{libdir}"] + ldflags.split()
    # 非 Windows 上尝试用 python3-config --embed 补齐 -lpython3.x; 工具不存在或输出
    # 不可用时静默跳过, 只用上面的 -L 与 LDFLAGS。
    try:
        pyld = subprocess.run(["python3-config", "--ldflags", "--embed"],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        if pyld.returncode == 0 and "-lpython" in pyld.stdout:
            flags += pyld.stdout.strip().split()
    except: pass
    return flags


def _mingw_python_link() -> list[str]:
    """Generate MinGW-compatible libpython from MSVC python DLL via gendef+dlltool.
    Returns list of link flags (e.g. ['-Lbuild/mingw_lib', '-lpython314']).
    Cached — only generated once per session.

    中文: MinGW 的 ld 无法直接链接 MSVC 生成的 python3xx.lib, 必须先用 gendef 从
    python3xx.dll 导出 .def, 再用 dlltool 生成 MinGW 可用的导入库 libpython3xx.a。
    生成的 .a 缓存在 build/mingw_lib, 存在即直接复用(每个环境只生成一次);
    任何一步失败都回退到 _py_link_flags(), 不让链接问题中断构建流程。
    """
    cache_dir = os.path.join(BUILD_DIR, "mingw_lib")
    ver = f"{sys.version_info.major}{sys.version_info.minor}"
    lib_a = os.path.join(cache_dir, f"libpython{ver}.a")

    if os.path.exists(lib_a):
        # 已有缓存: 直接给出 -L/-l, 省掉一次 gendef+dlltool。
        return [f"-L{cache_dir}", f"-lpython{ver}"]

    # Find python DLL
    # 定位 python3xx.dll: 官方安装版在 base_prefix/prefix, 但 uv 等安装器会把解释器
    # 放到非标准路径, 所以还要看 _base_executable 所在目录, 最后用通配再兜一层。
    dll = None
    candidates = [
        os.path.join(sys.base_prefix, f"python{ver}.dll"),
        os.path.join(sys.prefix, f"python{ver}.dll"),
    ]
    # uv installs Python to a non-standard path — exec dir is reliable
    exe_dir = os.path.dirname(getattr(sys, '_base_executable', sys.executable))
    candidates.append(os.path.join(exe_dir, f"python{ver}.dll"))
    # Also check directory of full python3.dll if found
    for c_dir in [sys.base_prefix, sys.prefix, exe_dir]:
        try:
            import glob as _glob
            hits = _glob.glob(os.path.join(c_dir, f"python{ver}*.dll"))
            for h in hits:
                if h not in candidates:
                    candidates.append(h)
        except:
            pass
    for c in candidates:
        if os.path.exists(c):
            dll = c
            break
    if not dll:
        # 仍找不到就放宽到任意 python*.dll; 实在没有只能回退 MSVC 风格参数
        # (链接大概会失败, 但至少给出明确告警而不是静默产出坏二进制)。
        import ctypes, glob as _glob
        try:
            dll = _glob.glob(os.path.join(sys.prefix, "python*.dll"))
            if dll:
                dll = dll[0]
        except:
            pass
    if not dll:
        print("[build] WARNING: cannot find python DLL for gendef", file=sys.stderr)
        return _py_link_flags()

    os.makedirs(cache_dir, exist_ok=True)

    # Find gendef/dlltool (bundled with MinGW g++)
    # gendef/dlltool 随 MinGW 分发; PATH 里没有时按 MSYS2 常见安装位置兜底。
    gendef = shutil.which("gendef")
    dlltool = shutil.which("dlltool")
    # Fallback to msys2 paths
    if not gendef:
        for p in [r"C:\msys64\mingw64\bin\gendef.exe", r"C:\msys64\ucrt64\bin\gendef.exe"]:
            if os.path.exists(p): gendef = p; break
    if not dlltool:
        for p in [r"C:\msys64\mingw64\bin\dlltool.exe", r"C:\msys64\ucrt64\bin\dlltool.exe"]:
            if os.path.exists(p): dlltool = p; break

    if not gendef or not dlltool:
        print("[build] WARNING: gendef/dlltool not found — falling back to MSVC .lib", file=sys.stderr)
        return _py_link_flags()

    def_file = os.path.join(cache_dir, f"python{ver}.def")
    r = subprocess.run([gendef, dll], capture_output=True, cwd=cache_dir, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        print(f"[build] WARNING: gendef failed: {r.stderr[-200:]}", file=sys.stderr)
        return _py_link_flags()
    # gendef outputs to current dir, named python{ver}.def
    # 中文: gendef 把 .def 写在当前目录, 名字取自 DLL 文件名; 老版本还可能无视 cwd,
    # 因此依次尝试: 搬到缓存目录 → 用缓存目录里任意 .def 兜底。
    gendef_out = os.path.join(cache_dir, os.path.basename(dll).replace(".dll", ".def"))
    if not os.path.exists(gendef_out) and os.path.exists(os.path.join(os.getcwd(), os.path.basename(dll).replace(".dll", ".def"))):
        # gendef wrote to cwd
        import shutil as _shutil
        _shutil.move(os.path.join(os.getcwd(), os.path.basename(dll).replace(".dll", ".def")), gendef_out)
    if not os.path.exists(gendef_out):
        # try common naming
        for f in os.listdir(cache_dir):
            if f.endswith(".def"):
                gendef_out = os.path.join(cache_dir, f)
                break

    r2 = subprocess.run([dlltool, "-d", gendef_out, "-l", lib_a, "-D", dll],
                         capture_output=True, cwd=cache_dir, encoding='utf-8', errors='replace')
    if r2.returncode != 0:
        print(f"[build] WARNING: dlltool failed: {r2.stderr[-200:]}", file=sys.stderr)
        return _py_link_flags()

    # 成功生成导入库后才给出 MinGW 链接参数; 否则仍走 MSVC 风格回退。
    if os.path.exists(lib_a):
        print(f"[build] Generated MinGW lib: {lib_a}", file=sys.stderr)
        return [f"-L{cache_dir}", f"-lpython{ver}"]
    return _py_link_flags()


def _cxxflags_override(target, compiler_name, auto_flags, simd_name, verbose):
    """If PBB_CXXFLAGS set, use it directly; otherwise return auto_flags.
    PBB_CXXFLAGS can be a space-separated string of compiler flags.
    Example: PBB_CXXFLAGS="-march=native -Ofast -funroll-loops -ffast-math"

    中文: PBB_CXXFLAGS 是优先级最高的编译参数覆盖(空格分隔)。一旦设置就整体替换
    自动探测出的参数, 不再追加任何东西(调用方只在末尾补必需的 -DPBB_UI_ENGINE)。
    portable 模式禁止覆盖 —— 否则产物不再是可跨机器复用的保守基线, 与发布承诺冲突。
    """
    env_flags = os.environ.get("PBB_CXXFLAGS", "").strip()
    if not env_flags:
        # 未设置时原样返回自动探测结果。
        return auto_flags
    if _build_mode() == "portable":
        raise ValueError("Portable builds forbid PBB_CXXFLAGS overrides")
    user_flags = env_flags.split()
    if verbose or True:  # always show override
        print(f"[build] {target}: overriding flags → PBB_CXXFLAGS={env_flags}", file=sys.stderr)
    return user_flags


def _compile(name, flags, is_msvc, src, out, extra_includes=[], extra_link=[],
             shared=False):
    # 只负责拼命令行, 不执行编译; MSVC 与 GCC 的参数写法在此分流。
    if is_msvc:
        cmd = [name] + flags
        for d in extra_includes:
            cmd.append(f"/I{d}")
        # Redirect .obj to build/
        # 中文: /Fo 必须指向 build/, 否则 cl 会把 .obj 落在源码目录里。
        obj_dir = os.path.dirname(out)
        cmd.append(f"/Fo{obj_dir}/")
        if shared: cmd.append("/LD")
        cmd.append(src)
        if shared:
            # 共享库(DLL)还需要导入库 .lib, 供后续链接使用。
            implib = os.path.join(obj_dir, os.path.splitext(os.path.basename(out))[0] + ".lib")
            cmd += ["/link"] + extra_link + [f"/OUT:{out}", f"/IMPLIB:{implib}"]
        else:
            cmd += [f"/Fe:{out}"]
    else:
        cmd = [name] + flags
        for d in extra_includes:
            cmd += [f"-I{d}"]
        if shared:
            cmd += ["-shared"]
            # 非 Windows 上生成共享库必须显式 -fPIC, 否则重定位报错。
            if sys.platform != "win32":
                cmd += ["-fPIC"]
        cmd += ["-o", out, src]
        cmd += extra_link
    return cmd


def _src_dir() -> tuple[str, list[str]]:
    """Return (bridge_src_path, include_dirs) based on PBB_SRC_DIR env var.
    PBB_SRC_DIR=v1_baseline → native/src/v1_baseline/bridge.cpp, [-I 该目录, -I native/src]
    Not set → native/src/bridge.cpp, [-I native/src]

    中文: 按 PBB_SRC_DIR 选择要编译的头文件树, 返回 (bridge.cpp 路径, include 目录列表)。
    正式头文件树现在是 pbb/native/src/（历史实验版本是它下面的子目录），选择逻辑本身
    已收拢到 pbb/paths.native_source_tree(), 这里只负责把环境变量传进去。
    指定历史版本时把该版本目录排在正式 src/ 之前, engine.hpp/name.hpp 等会先命中版本目录
    里的副本, 从而实现"同一套构建脚本编不同历史版本"; 目录不存在则静默退回正式 src/。
    """
    bridge, includes = paths.native_source_tree(os.environ.get("PBB_SRC_DIR", ""))
    return (str(bridge), [str(p) for p in includes])


def _compile_pbb_core(verbose=False):
    """Compile bridge.cpp → pbb_core.{so,pyd}. Tries compilers in priority order."""
    # 中文: 把 bridge.cpp 编成 pbb_core 扩展模块, 按 icpx > g++ > cl 依次尝试,
    # 第一个成功的编译器胜出并写入清单; 全部失败才以退出码 1 终止。
    import pybind11
    compilers = _find_compilers(verbose)  # icpx > g++ > cl
    if not compilers:
        print("ERROR: No C++ compiler found", file=sys.stderr)
        sys.exit(1)

    os.makedirs(BUILD_DIR, exist_ok=True)
    bridge_src, include_dirs = _src_dir()
    out = _pbb_core_path()
    includes = include_dirs + [pybind11.get_include()] + _py_include()
    link = _py_link_flags()

    last_err = ""
    for name, flags, simd_name, is_msvc in compilers:
        # MinGW g++ on Windows: static link runtime to avoid missing DLL errors
        # 中文: Windows 上的 MinGW g++ 需要静态链接 GCC 运行时, 否则用户机器缺少
        # libgcc/libstdc++ DLL 时扩展无法加载; 同时 Python 侧链接参数要换成
        # dlltool 生成的 libpython3xx.a(MSVC 的 .lib 格式 MinGW 不认)。
        extra_link = link[:]
        if sys.platform == "win32" and not is_msvc and "g++" in str(name):
            flags = flags + ["-static", "-static-libgcc", "-static-libstdc++"]
            extra_link = _mingw_python_link()

        # PBB_CXXFLAGS 环境变量: 覆盖自动检测的 flags
        overrides = _cxxflags_override("pbb_core", name, flags, simd_name, verbose)

        cmd = _compile(name, overrides, is_msvc, bridge_src, out,
                       extra_includes=includes, extra_link=extra_link, shared=True)
        if verbose:
            print(f"[build] pbb_core [{name}]: {' '.join(cmd)}", file=sys.stderr)
        print(f"[build] pbb_core: {name} ({simd_name})", file=sys.stderr)
        r = _run_compiler(
            cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
        )
        if r.returncode == 0:
            # 不带 engine_target: Python 核心不使用测速开关, 因此不因 build_config 变化失效。
            _record_binary(out, name, overrides)
            return  # success
        last_err = r.stderr
        print(f"[build] {name} failed, trying next compiler ...", file=sys.stderr)

    # 保留最后一个编译器的错误输出: 通常是根因所在。
    print(f"[build] All compilers failed:\n{last_err[-1000:]}", file=sys.stderr)
    sys.exit(1)


def _compile_engine(verbose=False, output_path=None, ui_engine=False):
    """Compile engine_main.cpp. Tries compilers in priority order."""
    # 中文: 把 engine_main.cpp 编成普通引擎或 UI 引擎(由 PBB_UI_ENGINE 宏区分),
    # 同样按编译器优先级逐个尝试。output_path 给定时(UI 引擎/自测二进制)不写共享旁车文件。
    compilers = _find_compilers(verbose)
    if _build_mode()=='native' and not os.environ.get('PBB_CXXFLAGS','').strip():
        # 已测速选定编译器时把它排到最前: 性能结论只对该编译器成立。
        # 用户显式给了 PBB_CXXFLAGS 就不再干预顺序。
        profile,active=build_settings.load(BASE_DIR,_machine_identity())
        if active:
            selected=os.path.normcase(os.path.abspath(profile['compiler']))
            compilers=sorted(compilers,key=lambda c:os.path.normcase(os.path.abspath(c[0]))!=selected)
    if not compilers:
        print("ERROR: No C++ compiler found", file=sys.stderr)
        sys.exit(1)

    os.makedirs(BUILD_DIR, exist_ok=True)
    src = str(paths.ENGINE_MAIN)
    out = output_path or _engine_bin()
    _, include_dirs = _src_dir()
    includes = include_dirs

    last_err = ""
    for name, flags, simd_name, is_msvc in compilers:
        if _build_mode() == "native" and not is_msvc and "g++" in os.path.basename(str(name)).lower():
            # native + g++: 先追加生产默认优化开关(LTO、分段 KSA、属性 min/max、编码复制)。
            flags = flags + ["-flto", "-DPBB_KSA_SEGMENTED=1", "-DPBB_ATTR_MINMAX=1", "-DPBB_ENCODING_COPY=1"]
            profile,active=build_settings.load(BASE_DIR,_machine_identity(),name)
            if active and not os.environ.get('PBB_CXXFLAGS','').strip() and not os.environ.get('PBB_SRC_DIR','').strip():
                # 有生效的本机测速结果且用户没有覆盖参数时, 用配置里的选项重写上面这些开关:
                # apply_flags 会先剔除同名旧参数再按选项追加, 因此结果与来源顺序无关。
                # 用 PBB_SRC_DIR 做实验时不套用测速配置(结论只对正式头文件树成立)。
                flags=build_settings.apply_flags(flags,profile['options'])
                if verbose:print('[build] using tested build_config.json',file=sys.stderr)
        if sys.platform == "win32" and not is_msvc and "g++" in str(name):
            # Windows MinGW 静态链接运行时(原因同 _compile_pbb_core)。
            flags = flags + ["-static", "-static-libgcc", "-static-libstdc++"]

        # PBB_CXXFLAGS 环境变量: 覆盖自动检测的 flags
        overrides = _cxxflags_override("engine", name, flags, simd_name, verbose)
        # Append after optional user flags so the requested target cannot change.
        # 中文: 目标宏追加在用户参数之后, 保证 PBB_CXXFLAGS 无法把 UI/普通引擎的目标改掉。
        overrides = overrides + [f"{'/D' if is_msvc else '-D'}PBB_UI_ENGINE={int(ui_engine)}"]

        cmd = _compile(name, overrides, is_msvc, src, out, extra_includes=includes)
        if verbose:
            print(f"[build] engine [{name}]: {' '.join(cmd)}", file=sys.stderr)
        print(f"[build] engine: {name} ({simd_name})", file=sys.stderr)
        r = _run_compiler(
            cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
        )
        if r.returncode == 0:
            # 记录目标变体: standard 与 ui 两个二进制不允许互相复用。
            _record_binary(out, name, overrides, engine_target="ui" if ui_engine else "standard")
            # The default target is shared by main.py and must never silently
            # reuse a binary compiled with a variant include directory.
            # 中文: 只有默认产物写旁车文件 —— 它被 main.py 共用, 绝不能悄悄复用由
            # 另一棵头文件树(PBB_SRC_DIR)编出来的二进制; UI 引擎走 output_path 分支。
            if output_path is None:
                with open(_engine_source_stamp(), "w", encoding="utf-8") as f:
                    f.write(_engine_source_name() + "\n")
            return
        last_err = r.stderr
        print(f"[build] {name} failed, trying next compiler ...", file=sys.stderr)

    print(f"[build] All compilers failed:\n{last_err[-1000:]}", file=sys.stderr)
    sys.exit(1)


def _env_info(verbose=False):
    """Print environment summary: OS, CPU, Python, available compilers + SIMD."""
    # 中文: 打印环境摘要(系统/CPU/Python/编译器及其 SIMD 档位), 便于从构建日志
    # 判断本次到底用了什么工具链 —— 编译器探测失败时的第一手线索。
    import platform
    print(f"[env] OS: {sys.platform} {platform.machine()}", file=sys.stderr)
    try:
        cpu = platform.processor() or "unknown"
        cores = os.cpu_count() or "?"
        print(f"[env] CPU: {cpu} ({cores} cores)", file=sys.stderr)
    except:
        pass
    print(f"[env] Python: {sys.version.split()[0]}", file=sys.stderr)

    # Show all detected compilers with their SIMD levels
    compilers = _find_compilers(verbose)
    names = []
    for name, flags, simd_name, is_msvc in compilers:
        names.append(f"{name}({simd_name})")
    print(f"[env] compilers: {' -> '.join(names) if names else 'NONE'}", file=sys.stderr)


def ensure_all(rebuild=False, verbose=False):
    # 对外唯一入口(main.py / rebuild_local.py / 打包脚本都走它)。
    # 先做 Python 版本与依赖检查, 再 chdir 到 BASE_DIR: 编译器命令行与后续相对路径
    # (src/、build/)都以仓库根为基准, package_release.py 也依赖同一约定。
    # rebuild=True 无条件重编两份产物; 否则只在 _binary_is_current 判定过期时补编。
    check_python()
    check_deps()
    os.chdir(BASE_DIR)
    _env_info(verbose)

    if rebuild or not _binary_is_current(_pbb_core_path()):
        _compile_pbb_core(verbose)

    # engine_target="standard": 这里只保证普通引擎可用; UI 引擎由调用方单独编译。
    if rebuild or not _binary_is_current(_engine_bin(), engine_target="standard"):
        _compile_engine(verbose)

    return _engine_bin()

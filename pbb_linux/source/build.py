#!/usr/bin/env python3
"""
PBB build system — compiler detection, SIMD probing, unified compilation.

Called by main.py via ensure_all(rebuild, verbose).
Not meant to be run directly — use ./run.sh or run.bat.
"""
import os, sys, shutil, subprocess, tempfile, sysconfig
import glob, hashlib, json, platform
from functools import lru_cache
import build_settings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(BASE_DIR, "build")


def _build_mode():
    mode = os.environ.get("PBB_BUILD_MODE", "native").strip().lower()
    if mode not in ("native", "portable"):
        raise ValueError("PBB_BUILD_MODE must be native or portable")
    return mode


def _machine_identity():
    # Native builds may contain more ISA extensions than their SIMD label.
    # Bind reuse to the build host until portable ISA baselines are distributed.
    return [platform.node(), platform.machine(), platform.processor(),
            os.environ.get("PROCESSOR_IDENTIFIER", ""), sys.platform]


def _binary_digest(path):
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(fh.read()).hexdigest()


def _record_binary(path, compiler, flags, engine_target=None):
    metadata = dict(version=2, mode=_build_mode(), machine=_machine_identity(),
                    platform=[sys.platform, platform.machine()], source=_engine_source_name(),
                    digest=_binary_digest(path), compiler=compiler, flags=flags)
    if engine_target is None:
        # Prevent a module built with (for example) Python 3.12 from being
        # reused by the bundled Python 3.14 interpreter merely because its
        # filename still has a cpython-314 suffix.
        metadata["python_abi"] = f"{sys.version_info.major}{sys.version_info.minor}"
    if engine_target is not None:
        metadata["engine_target"] = engine_target
        metadata['build_config'] = _build_config_signature(compiler)
    with open(path + ".build.json", "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, ensure_ascii=True)


def _binary_is_current(path, engine_target=None):
    try:
        with open(path + ".build.json", encoding="utf-8") as fh:
            metadata = json.load(fh)
        compatible = (metadata.get("mode") == "portable" and
                      metadata.get("platform") == [sys.platform, platform.machine()])
        compatible = compatible or (metadata.get("mode") == "native" and
                                     metadata.get("machine") == _machine_identity())
        current_abi = f"{sys.version_info.major}{sys.version_info.minor}"
        abi_ok = engine_target is not None or metadata.get("python_abi") == current_abi
        return (metadata.get("version") == 2 and compatible and abi_ok
                and (engine_target is None or metadata.get("engine_target") == engine_target)
                and metadata.get("source") == _engine_source_name()
                and (engine_target is None or metadata.get('build_config') == _build_config_signature())
                and metadata.get("digest") == _binary_digest(path))
    except (OSError, ValueError, TypeError):
        return False


def _build_config_signature(compiler=None):
    if (_build_mode()!='native' or (compiler is not None and 'g++' not in os.path.basename(compiler).lower())
            or os.environ.get('PBB_CXXFLAGS','').strip() or os.environ.get('PBB_SRC_DIR','').strip()):
        return None
    profile,active=build_settings.load(BASE_DIR,_machine_identity(),compiler)
    return build_settings.signature(profile,active)


def ensure_core(verbose=False):
    """Validate before importing native code into the Python/UI process."""
    if not _binary_is_current(_pbb_core_path()):
        if getattr(sys, "frozen", False):
            raise RuntimeError("pbb_core 的构建来源或机器不匹配，请在目标机器从源码重编后使用")
        _compile_pbb_core(verbose)


@lru_cache(maxsize=None)
def _compiler_environment(compiler):
    if sys.platform != "win32" or "icpx" not in os.path.basename(compiler).lower():
        environment = os.environ.copy()
        if sys.platform == "win32" and os.path.isabs(compiler):
            environment["PATH"] = os.path.dirname(compiler) + os.pathsep + environment.get("PATH", "")
        return environment
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
    try:
        return subprocess.run(command, env=_compiler_environment(command[0]),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=str(exc))


def check_python():
    if sys.version_info < (3, 10):
        print("ERROR: Python 3.10+ required", file=sys.stderr)
        sys.exit(1)


def check_deps():
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
    name = "pbb_engine" + (".exe" if sys.platform == "win32" else "")
    return os.path.join(BUILD_DIR, name)


def _engine_source_stamp():
    """Sidecar recording which include tree produced the shared engine binary."""
    return _engine_bin() + ".source"


def _engine_source_name():
    return os.environ.get("PBB_SRC_DIR", "").strip() or "src"


def _engine_matches_selected_source():
    try:
        with open(_engine_source_stamp(), encoding="utf-8") as f:
            return f.read().strip() == _engine_source_name()
    except OSError:
        return False


def _pbb_core_path():
    ext = sysconfig.get_config_var("EXT_SUFFIX") or (".pyd" if sys.platform == "win32" else ".so")
    return os.path.join(BUILD_DIR, "pbb_core" + ext)


def _pbb_core_exists():
    return os.path.exists(_pbb_core_path())


def _compiler_probe(compiler, flag):
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
    """
    is_win = sys.platform == "win32"
    is_msvc = os.path.basename(compiler).startswith("cl")
    is_icpx = "icpx" in os.path.basename(compiler).lower()

    if not is_win:
        import platform
        machine = platform.machine()
        if machine.startswith("aarch") or machine.startswith("arm"):
            return ([], "NEON")

    if is_msvc:
        candidates = [(["/arch:AVX512"], "AVX-512", "avx512f"),
                      (["/arch:AVX2"], "AVX2", "avx2")]
    elif is_icpx:
        # Windows: keep the conservative AVX2 baseline for the unverified bridge.
        if is_win:
            candidates = [(["-xCORE-AVX2"], "AVX2", "avx2")]
        else:
            candidates = [(["-xCORE-AVX512"], "AVX-512", "avx512f"),
                          (["-xCORE-AVX2"], "AVX2", "avx2")]
    else:
        candidates = [(["-mavx512f", "-mavx512bw", "-mfma"], "AVX-512", "avx512f"),
                      (["-mavx2", "-mfma"], "AVX2", "avx2")]

    for flags, name, feat in candidates:
        if not _compiler_probe(compiler, flags[0]):
            continue
        required = [feat, "fma"]
        if feat == "avx512f": required += ["avx512bw", "avx2"]
        if feat == "avx512f" and is_msvc:
            required += ["avx512dq", "avx512cd", "avx512vl"]
        cpu_ok = all(_cpu_supports(compiler, feature) is True for feature in required)
        if cpu_ok:
            return (flags, name)

    return ([], "none")


def _gcc_arch_flags(compiler, verbose=False):
    """使用 -march=native: 让 GCC 按编译机实际 CPU 选择 ISA 与调度 (Intel/AMD 通用)."""
    flag = "-march=native"
    if _compiler_probe(compiler, flag):
        return [flag], ", march=native"

    if verbose:
        print(f"[build] WARNING: {compiler} 不支持 {flag}; 跳过 CPU 优化", file=sys.stderr)
    return [], ""


def _bundled_toolchain():
    candidates = [os.environ.get("PBB_TOOLCHAIN_DIR", "")]
    directory = BASE_DIR
    for _ in range(3):
        candidates.append(os.path.join(directory, "runtime", "mingw64"))
        directory = os.path.dirname(directory)
    return next((path for path in candidates if path and os.path.isfile(os.path.join(path, "bin", "g++.exe"))), None)


def _find_compilers(verbose=False):
    """Detect all available compilers, return in performance order.
    Each entry: (name, flags, simd_name, is_msvc).
    Order: icpx > g++ > cl, regardless of platform or target.
    """
    result = []
    is_win = sys.platform == "win32"

    # ---- Detect all compilers ----
    bundled = _bundled_toolchain() if is_win else None
    icpx = None if bundled else shutil.which("icpx")
    gpp = os.path.join(bundled, "bin", "g++.exe") if bundled else shutil.which("g++")
    cl = shutil.which("cl") if is_win and not bundled else None

    if _build_mode() == "portable":
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
        # pthread_create and related APIs are used by the engine on Linux.
        # Keep the flag in the common compiler flags so it applies to both
        # the shared Python module and executable engine links.
        base = ["-std=c++17", "-O3", "-funroll-loops", "-ffast-math"]
        if not is_win:
            base.append("-pthread")
        arch_flags, arch_name = _gcc_arch_flags(gpp, verbose)
        simd_flags, simd_name = _detect_simd(gpp)
        entries.append((gpp, base + arch_flags + simd_flags, simd_name + arch_name, False))

    if cl:
        simd_flags, simd_name = _detect_simd("cl")
        entries.append(("cl", ["/std:c++17", "/Ox", "/EHsc", "/utf-8", "/w"] + simd_flags, simd_name, True))

    # ---- Order: icpx > g++ > cl on all platforms ----

    return entries  # default: icpx > g++ > cl


def _py_include():
    if sys.platform == "win32":
        local = os.path.join(sys.base_prefix, "include")
        if os.path.isdir(local):
            return [local]
    inc = sysconfig.get_config_var("INCLUDEPY")
    if inc: return [inc]
    return [os.path.join(sys.prefix, "include")]


def _py_link_flags():
    if sys.platform == "win32":
        ver = f"{sys.version_info.major}{sys.version_info.minor}"
        libdir = os.path.join(sys.base_prefix, "libs")
        lib = os.path.join(libdir, f"python{ver}.lib")
        return [lib] if os.path.exists(lib) else [f"/LIBPATH:{libdir}", f"python{ver}.lib"]
    libdir = sysconfig.get_config_var("LIBDIR") or os.path.join(sys.prefix, "lib")
    ldflags = sysconfig.get_config_var("LDFLAGS") or ""
    flags = [f"-L{libdir}"] + ldflags.split()
    try:
        pyld = subprocess.run(["python3-config", "--ldflags", "--embed"],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        expected = f"-lpython{sys.version_info.major}.{sys.version_info.minor}"
        if pyld.returncode == 0 and expected in pyld.stdout:
            flags += pyld.stdout.strip().split()
    except: pass
    return flags


def _mingw_python_link() -> list[str]:
    """Generate MinGW-compatible libpython from MSVC python DLL via gendef+dlltool.
    Returns list of link flags (e.g. ['-Lbuild/mingw_lib', '-lpython314']).
    Cached — only generated once per session.
    """
    cache_dir = os.path.join(BUILD_DIR, "mingw_lib")
    ver = f"{sys.version_info.major}{sys.version_info.minor}"
    lib_a = os.path.join(cache_dir, f"libpython{ver}.a")

    if os.path.exists(lib_a):
        return [f"-L{cache_dir}", f"-lpython{ver}"]

    # Find python DLL
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

    if os.path.exists(lib_a):
        print(f"[build] Generated MinGW lib: {lib_a}", file=sys.stderr)
        return [f"-L{cache_dir}", f"-lpython{ver}"]
    return _py_link_flags()


def _cxxflags_override(target, compiler_name, auto_flags, simd_name, verbose):
    """If PBB_CXXFLAGS set, use it directly; otherwise return auto_flags.
    PBB_CXXFLAGS can be a space-separated string of compiler flags.
    Example: PBB_CXXFLAGS="-march=native -Ofast -funroll-loops -ffast-math"
    """
    env_flags = os.environ.get("PBB_CXXFLAGS", "").strip()
    if not env_flags:
        return auto_flags
    if _build_mode() == "portable":
        raise ValueError("Portable builds forbid PBB_CXXFLAGS overrides")
    user_flags = env_flags.split()
    if verbose or True:  # always show override
        print(f"[build] {target}: overriding flags → PBB_CXXFLAGS={env_flags}", file=sys.stderr)
    return user_flags


def _compile(name, flags, is_msvc, src, out, extra_includes=[], extra_link=[],
             shared=False):
    if is_msvc:
        cmd = [name] + flags
        for d in extra_includes:
            cmd.append(f"/I{d}")
        # Redirect .obj to build/
        obj_dir = os.path.dirname(out)
        cmd.append(f"/Fo{obj_dir}/")
        if shared: cmd.append("/LD")
        cmd.append(src)
        if shared:
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
            if sys.platform != "win32":
                cmd += ["-fPIC"]
        cmd += ["-o", out, src]
        cmd += extra_link
    return cmd


def _src_dir() -> tuple[str, list[str]]:
    """Return (bridge_src_path, include_dirs) based on PBB_SRC_DIR env var.
    PBB_SRC_DIR=v1_baseline → src/v1_baseline/bridge.cpp, [-I src/v1_baseline, -I src]
    Not set → src/bridge.cpp, [-I src]
    """
    env = os.environ.get("PBB_SRC_DIR", "").strip()
    if env:
        d = os.path.join(BASE_DIR, "src", env)
        if os.path.isdir(d):
            bridge = os.path.join(d, "bridge.cpp")
            return (bridge, [d, os.path.join(BASE_DIR, "src")])
    return (os.path.join(BASE_DIR, "src", "bridge.cpp"), [os.path.join(BASE_DIR, "src")])


def _compile_pbb_core(verbose=False):
    """Compile bridge.cpp → pbb_core.{so,pyd}. Tries compilers in priority order."""
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
            _record_binary(out, name, overrides)
            return  # success
        last_err = r.stderr
        print(f"[build] {name} failed, trying next compiler ...", file=sys.stderr)

    print(f"[build] All compilers failed:\n{last_err[-1000:]}", file=sys.stderr)
    sys.exit(1)


def _compile_engine(verbose=False, output_path=None):
    """Compile the Linux CLI engine."""
    compilers = _find_compilers(verbose)
    if _build_mode()=='native' and not os.environ.get('PBB_CXXFLAGS','').strip():
        profile,active=build_settings.load(BASE_DIR,_machine_identity())
        if active:
            selected=os.path.normcase(os.path.abspath(profile['compiler']))
            compilers=sorted(compilers,key=lambda c:os.path.normcase(os.path.abspath(c[0]))!=selected)
    if not compilers:
        print("ERROR: No C++ compiler found", file=sys.stderr)
        sys.exit(1)

    os.makedirs(BUILD_DIR, exist_ok=True)
    src = os.path.join(BASE_DIR, "engine_main.cpp")
    out = output_path or _engine_bin()
    _, include_dirs = _src_dir()
    includes = include_dirs

    last_err = ""
    for name, flags, simd_name, is_msvc in compilers:
        if _build_mode() == "native" and not is_msvc and "g++" in os.path.basename(str(name)).lower():
            flags = flags + ["-flto", "-DPBB_KSA_SEGMENTED=1", "-DPBB_ATTR_MINMAX=1", "-DPBB_ENCODING_COPY=1"]
            profile,active=build_settings.load(BASE_DIR,_machine_identity(),name)
            if active and not os.environ.get('PBB_CXXFLAGS','').strip() and not os.environ.get('PBB_SRC_DIR','').strip():
                flags=build_settings.apply_flags(flags,profile['options'])
                if verbose:print('[build] using tested build_config.json',file=sys.stderr)
        if sys.platform == "win32" and not is_msvc and "g++" in str(name):
            flags = flags + ["-static", "-static-libgcc", "-static-libstdc++"]

        # PBB_CXXFLAGS 环境变量: 覆盖自动检测的 flags
        overrides = _cxxflags_override("engine", name, flags, simd_name, verbose)
        cmd = _compile(name, overrides, is_msvc, src, out, extra_includes=includes)
        if verbose:
            print(f"[build] engine [{name}]: {' '.join(cmd)}", file=sys.stderr)
        print(f"[build] engine: {name} ({simd_name})", file=sys.stderr)
        r = _run_compiler(
            cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
        )
        if r.returncode == 0:
            _record_binary(out, name, overrides, engine_target="standard")
            # The default target is shared by main.py and must never silently
            # reuse a binary compiled with a variant include directory.
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
    check_python()
    check_deps()
    os.chdir(BASE_DIR)
    _env_info(verbose)

    if rebuild or not _binary_is_current(_pbb_core_path()):
        _compile_pbb_core(verbose)

    if rebuild or not _binary_is_current(_engine_bin(), engine_target="standard"):
        _compile_engine(verbose)

    return _engine_bin()

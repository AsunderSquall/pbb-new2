import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# 测试文件位于 tests/ 子目录，被测代码在 pbb/ 包里；先把仓库根与 tools/ 加入 sys.path。
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / 'tools'))

from pbb import _build as build


# 测试把临时目录建在 build/ 下，所以要保证它存在（干净检出后无需先构建）。
from pbb import paths as _paths
_paths.ensure_build_dir()

class BuildSafetyTests(unittest.TestCase):
    # 本组用例守护编译安全：激进优化开关只在“本机 GCC 且未被用户覆盖”时生效，
    # 二进制清单负责判断能否复用，CPU 能力探测不确定时一律按不支持处理。
    def test_engine_lto_is_native_gcc_only_and_respects_overrides(self):
        # build.py 只在 native 模式 + GCC + 无 PBB_CXXFLAGS 覆盖时追加 LTO、分段 KSA、
        # 属性 min/max、编码复制这组优化（见 _compile_engine）。portable 要能跨机运行、
        # cl/icpx 未验证、用户显式覆盖优先，这三种情况都不许带上它们；
        # 而 -DPBB_UI_ENGINE 必须始终按目标显式给出，普通/UI 引擎不能编混。
        # 夹具把编译器探测、编译执行和清单记录全部 mock 掉，只检查实际传入的 flags。
        cases = [("native", "g++", False, "", True),
                 ("portable", "g++", False, "", False),
                 ("native", "cl", True, "", False),
                 ("native", "icpx", False, "", False),
                 ("native", "g++", False, "-O2", False)]
        for mode, compiler, msvc, override, expected in cases:
            # 每个用例都对普通/UI 两种目标各跑一遍：同一份开关在两个目标上的出现与否必须一致。
            for ui_engine in (False, True):
                with self.subTest(mode=mode, compiler=compiler, override=override, ui=ui_engine), \
                     patch.dict(os.environ, {"PBB_BUILD_MODE": mode, "PBB_CXXFLAGS": override}), \
                     patch.object(build, "_find_compilers", return_value=[(compiler, ["-O3"], "test", msvc)]), \
                     patch.object(build, "_run_compiler", return_value=SimpleNamespace(returncode=0)), \
                     patch.object(build, "_record_binary") as record:
                    # 编译成功才会走到 _record_binary；从它收到的实参里取出真正传给编译器的 flags。
                    build._compile_engine(output_path=os.path.join(build.BUILD_DIR, "mock-engine.exe"),
                                          ui_engine=ui_engine)
                    flags = record.call_args.args[2]
                    self.assertEqual("-flto" in flags, expected)
                    self.assertEqual("-DPBB_KSA_SEGMENTED=1" in flags, expected)
                    self.assertEqual("-DPBB_ATTR_MINMAX=1" in flags, expected)
                    self.assertEqual("-DPBB_ENCODING_COPY=1" in flags, expected)
                    self.assertIn(f"{'/D' if msvc else '-D'}PBB_UI_ENGINE={int(ui_engine)}", flags)

    # .build.json 清单决定“现有二进制能否复用”（_binary_is_current）：
    # 没有清单、清单标的是另一个引擎目标、换到别的机器（native）、通过 PBB_SRC_DIR
    # 换了源码树、或二进制内容被改动（摘要不符），都必须判定为过期并触发重编；
    # 只有清单齐备且逐一吻合时才为 True。
    def test_binary_origin_and_integrity(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory, \
             patch.dict(os.environ, {"PBB_BUILD_MODE": "native"}):
            binary = os.path.join(directory, "engine.exe")
            with open(binary, "wb") as fh:
                fh.write(b"build-one")
            self.assertFalse(build._binary_is_current(binary))
            build._record_binary(binary, "test", ["-march=native"])
            self.assertTrue(build._binary_is_current(binary))
            self.assertFalse(build._binary_is_current(binary, engine_target="ui"))
            build._record_binary(binary, "test", [], engine_target="standard")
            self.assertTrue(build._binary_is_current(binary, engine_target="standard"))
            self.assertFalse(build._binary_is_current(binary, engine_target="ui"))
            with patch.object(build, "_machine_identity", return_value=["another-host"]):
                self.assertFalse(build._binary_is_current(binary))
            with patch.dict(os.environ, {"PBB_SRC_DIR": "vgen"}):
                self.assertFalse(build._binary_is_current(binary))
            with open(binary, "wb") as fh:
                fh.write(b"build-two")
            self.assertFalse(build._binary_is_current(binary))

    # _cpu_supports 返回 None 表示“探测不出”，这种不确定必须当成不支持：MSVC 不能
    # 因此打开 SIMD。防的是在无 AVX-512 的机器上编出非法指令直接崩（0xC000001D / SIGILL）。
    def test_unknown_cpu_support_never_enables_msvc_simd(self):
        with patch.object(build.sys, "platform", "win32"), \
             patch.object(build, "_compiler_probe", return_value=True), \
             patch.object(build, "_cpu_supports", return_value=None):
            self.assertEqual(build._detect_simd("cl.exe"), ([], "none"))

    # portable 清单只绑定平台（sys.platform + 机器架构），不绑定具体主机，因此换一台
    # 同平台机器后仍可复用——这是发布包能直接带预编译引擎的前提。
    def test_portable_binary_can_be_reused_on_another_host(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory, \
             patch.dict(os.environ, {"PBB_BUILD_MODE": "portable"}):
            binary = os.path.join(directory, "engine.bin")
            with open(binary, "wb") as fh:
                fh.write(b"portable")
            build._record_binary(binary, "g++", ["-march=x86-64"])
            with patch.object(build, "_machine_identity", return_value=["another-host"]):
                self.assertTrue(build._binary_is_current(binary))

    # portable 模式编译必须彻底跳开 -march=native 与 _detect_simd 探测：
    # 一旦按编译机 CPU 出指令，发布出去的引擎在别的机器上就会崩。
    def test_portable_flags_never_probe_native_simd(self):
        with patch.dict(os.environ, {"PBB_BUILD_MODE": "portable"}), \
             patch.object(build.shutil, "which", side_effect=lambda name: name if name == "g++" else None), \
             patch.object(build, "_detect_simd") as detect:
            entries = build._find_compilers()
            self.assertNotIn("-march=native", entries[0][1])
            detect.assert_not_called()

    # AVX-512 需要 f + bw + fma 等整组能力同时具备。这里让 avx512bw 缺失，
    # 必须降级到 AVX2，而不能因为编译器接受 -mavx512f 就启用 512 路径。
    def test_avx512_requires_bw_and_os_support(self):
        def supports(_compiler, feature):
            return feature != "avx512bw"
        with patch.object(build.sys, "platform", "win32"), \
             patch.object(build, "_compiler_probe", return_value=True), \
             patch.object(build, "_cpu_supports", side_effect=supports):
            self.assertEqual(build._detect_simd("g++")[1], "AVX2")

    # PyInstaller 冻结运行（sys.frozen）时，核心模块来源/机器不匹配只能报错退出，
    # 绝不允许现场编译：发布包的运行环境里没有工具链，而且编译会污染安装目录。
    # 因此顺带断言 _compile_pbb_core 一次都没被调用。
    def test_frozen_core_fails_before_import(self):
        with patch.object(build, "_binary_is_current", return_value=False), \
             patch.object(build.sys, "frozen", True, create=True), \
             patch.object(build, "_compile_pbb_core") as compile_core:
            with self.assertRaises(RuntimeError):
                build.ensure_core()
            compile_core.assert_not_called()


if __name__ == "__main__":
    unittest.main()

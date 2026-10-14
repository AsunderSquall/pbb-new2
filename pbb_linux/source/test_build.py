import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import build


class BuildSafetyTests(unittest.TestCase):
    def test_engine_lto_is_native_gcc_only_and_respects_overrides(self):
        cases = [("native", "g++", False, "", True),
                 ("portable", "g++", False, "", False),
                 ("native", "cl", True, "", False),
                 ("native", "icpx", False, "", False),
                 ("native", "g++", False, "-O2", False)]
        for mode, compiler, msvc, override, expected in cases:
                with self.subTest(mode=mode, compiler=compiler, override=override), \
                     patch.dict(os.environ, {"PBB_BUILD_MODE": mode, "PBB_CXXFLAGS": override}), \
                     patch.object(build, "_find_compilers", return_value=[(compiler, ["-O3"], "test", msvc)]), \
                     patch.object(build, "_run_compiler", return_value=SimpleNamespace(returncode=0)), \
                     patch.object(build, "_record_binary") as record:
                    build._compile_engine(output_path=os.path.join(build.BUILD_DIR, "mock-engine.exe"))
                    flags = record.call_args.args[2]
                    self.assertEqual("-flto" in flags, expected)
                    self.assertEqual("-DPBB_KSA_SEGMENTED=1" in flags, expected)
                    self.assertEqual("-DPBB_ATTR_MINMAX=1" in flags, expected)
                    self.assertEqual("-DPBB_ENCODING_COPY=1" in flags, expected)

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

    def test_unknown_cpu_support_never_enables_msvc_simd(self):
        with patch.object(build.sys, "platform", "win32"), \
             patch.object(build, "_compiler_probe", return_value=True), \
             patch.object(build, "_cpu_supports", return_value=None):
            self.assertEqual(build._detect_simd("cl.exe"), ([], "none"))

    def test_portable_binary_can_be_reused_on_another_host(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory, \
             patch.dict(os.environ, {"PBB_BUILD_MODE": "portable"}):
            binary = os.path.join(directory, "engine.bin")
            with open(binary, "wb") as fh:
                fh.write(b"portable")
            build._record_binary(binary, "g++", ["-march=x86-64"])
            with patch.object(build, "_machine_identity", return_value=["another-host"]):
                self.assertTrue(build._binary_is_current(binary))

    def test_portable_flags_never_probe_native_simd(self):
        with patch.dict(os.environ, {"PBB_BUILD_MODE": "portable"}), \
             patch.object(build.shutil, "which", side_effect=lambda name: name if name == "g++" else None), \
             patch.object(build, "_detect_simd") as detect:
            entries = build._find_compilers()
            self.assertNotIn("-march=native", entries[0][1])
            detect.assert_not_called()

    def test_avx512_requires_bw_and_os_support(self):
        def supports(_compiler, feature):
            return feature != "avx512bw"
        with patch.object(build.sys, "platform", "win32"), \
             patch.object(build, "_compiler_probe", return_value=True), \
             patch.object(build, "_cpu_supports", side_effect=supports):
            self.assertEqual(build._detect_simd("g++")[1], "AVX2")

    def test_frozen_core_fails_before_import(self):
        with patch.object(build, "_binary_is_current", return_value=False), \
             patch.object(build.sys, "frozen", True, create=True), \
             patch.object(build, "_compile_pbb_core") as compile_core:
            with self.assertRaises(RuntimeError):
                build.ensure_core()
            compile_core.assert_not_called()


if __name__ == "__main__":
    unittest.main()

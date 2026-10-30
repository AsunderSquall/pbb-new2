"""「自动更新 start」与命令行默认配置的回归用例。"""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbb import autostart, build_settings, paths
from pbb.config_file import load_config, save_config

BASE_CONFIG = {
    "team_name": "测试",
    "prefixes": [{"name": ""}],
    "suffixes": [{"name": ""}],
    "character_set": {"single_char_length": 1, "types": [1], "custom_values": "ab",
                      "unicode_ranges": [], "output_charset": 1},
    "enumeration": {"mode": 1, "variable_length": 6, "ranges": [{"start": 100, "end": 1000}]},
    "collection": {"score_list": "qp", "min_qp": 9999, "score_mode": "all", "collect_mode": 0},
    "output": {"result_file": "t.txt", "output_xp": 0},
    "threads": {"worker_threads": 2},
}


class ProcessedFromLineTests(unittest.TestCase):
    def test_reads_the_engine_progress_line(self):
        line = "task1234 finished,task_mex=1200,count:0.012345T"
        self.assertEqual(autostart.processed_from_line(line), 12345000000)

    def test_skips_lines_without_a_count(self):
        self.assertIsNone(autostart.processed_from_line("tot=3, (500,1),time: 1.00s"))
        self.assertIsNone(autostart.processed_from_line(""))


class StartUpdaterTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(dir=str(paths.BUILD_DIR))
        self.directory = Path(self._temp.name)
        self.path = self.directory / "config.yaml"
        self.cfg = copy.deepcopy(BASE_CONFIG)
        # 阈值放大到 0.01T 之外，避免被全局 end 截断干扰断言。
        self.cfg["enumeration"]["ranges"] = [{"start": 100, "end": 10 ** 13}]
        save_config(self.path, self.cfg)

    def tearDown(self):
        self._temp.cleanup()

    def updater(self, enabled=True):
        return autostart.StartUpdater(self.path, self.cfg, enabled=enabled)

    def start(self):
        return load_config(self.path)["enumeration"]["ranges"][0]["start"]

    def test_advances_only_when_the_step_is_crossed(self):
        updater = self.updater()
        self.assertIsNone(updater.on_processed(autostart.STEP - 1))
        self.assertEqual(self.start(), 100)
        self.assertEqual(updater.on_processed(autostart.STEP), 100 + autostart.STEP)
        self.assertEqual(self.start(), 100 + autostart.STEP)
        # 没跨过下一个阶梯就不再写盘
        self.assertIsNone(updater.on_processed(autostart.STEP + 1))
        self.assertEqual(updater.on_processed(2 * autostart.STEP), 100 + 2 * autostart.STEP)

    def test_written_value_is_absolute_not_cumulative(self):
        updater = self.updater()
        updater.on_processed(autostart.STEP)
        updater.on_processed(2 * autostart.STEP)
        self.assertEqual(self.start(), 100 + 2 * autostart.STEP)
        # 收尾写入用同一个绝对公式，不会把之前的推进再加一遍
        updater.finish(2 * autostart.STEP + 12345)
        self.assertEqual(self.start(), 100 + 2 * autostart.STEP + 12345)

    def test_clamped_by_global_end(self):
        self.cfg["enumeration"]["ranges"] = [{"start": 100, "end": 1000}]
        save_config(self.path, self.cfg)
        updater = autostart.StartUpdater(self.path, self.cfg)
        self.assertEqual(updater.on_processed(autostart.STEP), 1000)
        self.assertEqual(self.start(), 1000)

    def test_disabled_updater_never_writes(self):
        updater = self.updater(enabled=False)
        self.assertIsNone(updater.on_processed(autostart.STEP))
        self.assertIsNone(updater.finish(autostart.STEP))
        self.assertEqual(self.start(), 100)

    def test_external_edit_stops_the_updater(self):
        updater = self.updater()
        updater.on_processed(autostart.STEP)              # 第一次写盘建立基线
        edited = load_config(self.path)
        edited["team_name"] = "手动改过"
        save_config(self.path, edited)
        self.assertIsNone(updater.on_processed(2 * autostart.STEP))
        self.assertTrue(updater.stopped)
        self.assertEqual(load_config(self.path)["team_name"], "手动改过")


class DefaultConfigPathTests(unittest.TestCase):
    """命令行不带 -c 时的默认配置：与界面共用 ui_settings 的启动默认。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(dir=str(paths.BUILD_DIR))
        self.root = Path(self._temp.name)
        (self.root / "config").mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def write_config(self, name, start=0):
        cfg = copy.deepcopy(BASE_CONFIG)
        cfg["enumeration"]["ranges"] = [{"start": start, "end": start + 10}]
        save_config(self.root / "config" / name, cfg)

    def test_uses_ui_settings_default_config(self):
        self.write_config("config_football.yaml")
        self.write_config("mine.yaml")
        build_settings.write_ui_settings({"default_config": "mine.yaml"}, self.root) \
            if hasattr(build_settings, "write_ui_settings") else None
        (self.root / "settings").mkdir(exist_ok=True)
        (self.root / "settings" / "ui_settings.json").write_text(
            '{"default_config": "mine.yaml"}', encoding="utf8")
        self.assertEqual(build_settings.default_config_path(self.root).name, "mine.yaml")

    def test_falls_back_to_config_football(self):
        self.write_config("config_football.yaml")
        self.assertEqual(build_settings.default_config_path(self.root).name, "config_football.yaml")

    def test_ignores_non_search_files_and_returns_none(self):
        (self.root / "settings").mkdir(exist_ok=True)
        (self.root / "settings" / "ui_settings.json").write_text(
            '{"default_config": "build_config.json"}', encoding="utf8")
        (self.root / "config" / "build_config.json").write_text('{"version": 1}', encoding="utf8")
        self.assertIsNone(build_settings.default_config_path(self.root))


class CliAutoStartTests(unittest.TestCase):
    """命令行默认开启「自动更新 start」，--no-auto-start 关闭。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(dir=str(paths.BUILD_DIR))
        self.directory = Path(self._temp.name)
        self.path = self.directory / "small.yaml"
        cfg = copy.deepcopy(BASE_CONFIG)
        cfg["enumeration"]["ranges"] = [{"start": 0, "end": 2_000_000}]
        save_config(self.path, cfg)

    def tearDown(self):
        self._temp.cleanup()

    def run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, "-m", "pbb.main", "-c", str(self.path), "--threads", "2", *extra],
            cwd=str(paths.PACKAGE_ROOT), capture_output=True, text=True, encoding="utf8",
            errors="replace", timeout=300)

    def test_start_is_updated_by_default(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        start = load_config(self.path)["enumeration"]["ranges"][0]["start"]
        self.assertGreater(start, 0)
        self.assertLessEqual(start, 2_000_000)
        self.assertIn("[auto-start]", result.stderr)

    def test_no_auto_start_leaves_the_config_untouched(self):
        result = self.run_cli("--no-auto-start")
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertEqual(load_config(self.path)["enumeration"]["ranges"][0]["start"], 0)


if __name__ == "__main__":
    unittest.main()

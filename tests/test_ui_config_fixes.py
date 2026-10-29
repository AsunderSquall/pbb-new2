"""配置校验与界面配置行为的回归用例。

覆盖 2026-10 的八条反馈里可以由纯逻辑验证的部分：
  * 区间测完后 `ranges[0].end == start` 仍可编辑/校验（反馈 2）；
  * 名字里的换行/逗号必须被明确拒绝，而不是静默截断参数（反馈 3、6）；
  * 顺序模式下"三个前缀区间一样"应当收成全局区间，让全局终点/start/resume 生效（反馈 5）；
  * 界面设置里的启动默认配置（反馈 8）；
  * 三种格式保存后首尾空格不丢失（反馈 3）。
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from pbb import ui                                    # noqa: E402
from pbb.main import ConfigError, _validate_config    # noqa: E402


def base_config(**enumeration):
    """一份能通过校验的最小配置；enumeration 用关键字覆盖。"""
    en = {'mode': 1, 'variable_length': 3, 'ranges': [{'start': 0, 'end': 100}]}
    en.update(enumeration)
    return {
        'team_name': '队伍',
        'prefixes': [{'name': '前'}],
        'suffixes': [{'name': '后'}],
        'character_set': {'single_char_length': 1, 'types': [1]},
        'enumeration': en,
        'collection': {'min_qp': 5400, 'score_list': 'qp'},
        'output': {'output_xp': 0, 'log_output': 1, 'speed_output': 1, 'result_file': 'r.txt'},
        'threads': {'worker_threads': -1},
    }


class RangeValidationTests(unittest.TestCase):
    def test_finished_range_with_equal_start_and_end_is_editable(self):
        """实测场景：跑完后「自动更新 start」把 start 推到 end，旧规则会让配置打不开。"""
        _validate_config(base_config(ranges=[{'start': 1000, 'end': 1000}]))

    def test_inverted_range_is_still_rejected(self):
        with self.assertRaisesRegex(ConfigError, 'end 必须 >= start'):
            _validate_config(base_config(ranges=[{'start': 1000, 'end': 999}]))

    def test_prefix_range_with_equal_bounds_is_allowed(self):
        """各前缀区间也可能被续测/完成状态写成空区间（end == start）。"""
        _validate_config(base_config(prefix_ranges=[{'count': 1, 'start': 500, 'end': 500}]))


class NameCharacterTests(unittest.TestCase):
    """名字会原样进入不转义的参数协议与结果文件：坏字符必须明确拒绝。"""

    def assert_rejected(self, **overrides):
        with self.assertRaisesRegex(ConfigError, '不能包含'):
            _validate_config(base_config(**overrides))

    def test_newline_in_prefix_is_rejected(self):
        # 反馈 6 的现场：前缀里带换行 → `prefixes=` 行被截断 → 第三个前缀静默不测。
        cfg = base_config()
        cfg['prefixes'] = [{'name': '一'}, {'name': '二\n '}, {'name': '三\n '}]
        with self.assertRaisesRegex(ConfigError, '换行'):
            _validate_config(cfg)

    def test_comma_in_prefix_suffix_or_team_is_rejected(self):
        cfg = base_config()
        cfg['prefixes'] = [{'name': 'a,b'}]
        with self.assertRaisesRegex(ConfigError, '逗号'):
            _validate_config(cfg)
        cfg = base_config()
        cfg['suffixes'] = [{'name': 'a,b'}]
        with self.assertRaisesRegex(ConfigError, '逗号'):
            _validate_config(cfg)
        cfg = base_config()
        cfg['team_name'] = 'a,b'
        with self.assertRaisesRegex(ConfigError, '逗号'):
            _validate_config(cfg)

    def test_plain_spaces_are_still_allowed(self):
        cfg = base_config()
        cfg['prefixes'] = [{'name': ' 前 '}]
        cfg['suffixes'] = [{'name': '后 卫 '}]
        cfg['team_name'] = ' 队 伍 '
        _validate_config(cfg)


class RangeEndApplicationTests(unittest.TestCase):
    """`Monitor._apply_range_end`：全局终点对所有模式生效，并收拢重复的逐前缀区间。"""

    @staticmethod
    def stub(end: str):
        return types.SimpleNamespace(
            range_var=types.SimpleNamespace(get=lambda: end),
            status_var=types.SimpleNamespace(set=lambda _value: None),
        )

    def test_prefix_ranges_are_never_rewritten(self):
        """全局终点只落在 ranges 上；前缀区间是布局，程序绝不改写（也不再"收拢"）。"""
        layout = [{'count': 1, 'start': 0, 'end': 1000}] * 3
        cfg = base_config(prefix_ranges=[dict(item) for item in layout])
        cfg['prefixes'] = [{'name': '一'}, {'name': '二'}, {'name': '三'}]
        ui.Monitor._apply_range_end(self.stub('5000'), cfg)
        self.assertEqual(cfg['enumeration']['prefix_ranges'], layout)
        self.assertEqual(cfg['enumeration']['ranges'], [{'start': 0, 'end': 5000}])

    def test_distinct_prefix_ranges_are_kept(self):
        cfg = base_config(prefix_ranges=[{'count': 1, 'start': 0, 'end': 10},
                                         {'count': 1, 'start': 0, 'end': 20}])
        cfg['prefixes'] = [{'name': '一'}, {'name': '二'}]
        ui.Monitor._apply_range_end(self.stub('5000'), cfg)
        self.assertEqual(len(cfg['enumeration']['prefix_ranges']), 2)
        self.assertEqual(cfg['enumeration']['ranges'], [{'start': 0, 'end': 5000}])

    def test_global_range_end_applies_without_prefix_ranges(self):
        """以前只有 format 配置会应用「范围终点」，普通/随机配置改了没用。"""
        cfg = base_config(mode=2)
        ui.Monitor._apply_range_end(self.stub('777'), cfg)
        self.assertEqual(cfg['enumeration']['ranges'], [{'start': 0, 'end': 777}])


class DefaultConfigTests(unittest.TestCase):
    """反馈 8：界面设置里的启动默认配置。"""

    def test_default_config_comes_from_ui_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'real.yaml').write_text('x', encoding='utf8')
            (Path(temp) / 'config.yaml').write_text('fallback', encoding='utf8')
            with patch.object(ui, 'CONFIG_DIR', temp), \
                 patch.object(ui, 'load_config', side_effect=lambda path: {'ok': True}), \
                 patch.object(ui.ConfigSelector, 'is_search_config', staticmethod(lambda cfg: True)):
                ui.save_ui_settings({'default_config': 'real.yaml'})
                self.assertEqual(os.path.basename(ui.default_config_path()), 'real.yaml')
                # 设置了但文件不存在 → 回退到 config.yaml
                ui.save_ui_settings({'default_config': 'missing.yaml'})
                self.assertEqual(os.path.basename(ui.default_config_path()), 'config.yaml')

    def test_ui_settings_round_trip_and_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(ui, 'CONFIG_DIR', temp):
            ui.save_ui_settings({'default_config': 'a.yaml', 'other': 1})
            self.assertEqual(ui.load_ui_settings(), {'default_config': 'a.yaml', 'other': 1})
            Path(ui.ui_settings_path()).write_text('{ not json', encoding='utf8')
            self.assertEqual(ui.load_ui_settings(), {})


class WhitespaceRoundTripTests(unittest.TestCase):
    """反馈 3：首尾空格必须原样往返（YAML/JSON/TOML）。"""

    def test_spaces_survive_save_and_load_in_all_formats(self):
        cfg = base_config()
        cfg['prefixes'] = [{'name': ' 前 '}, {'name': 'a b'}]
        cfg['suffixes'] = [{'name': ' 后'}]
        cfg['team_name'] = ' 队 '
        with tempfile.TemporaryDirectory() as temp:
            for suffix in ('.yaml', '.json', '.toml'):
                with self.subTest(suffix=suffix):
                    path = os.path.join(temp, 'c' + suffix)
                    ui.save_config(path, cfg)
                    back = ui.load_config(path)
                    self.assertEqual([p['name'] for p in back['prefixes']], [' 前 ', 'a b'])
                    self.assertEqual([s['name'] for s in back['suffixes']], [' 后'])
                    self.assertEqual(back['team_name'], ' 队 ')


class SingleLineInputTests(unittest.TestCase):
    """单行输入框里不能留下看不见的换行（反馈：粘贴多行/按回车后配置里藏了 \\n）。"""

    def test_single_line_text_drops_control_characters_but_keeps_spaces(self):
        self.assertEqual(ui.single_line_text(' 前\n缀\t '), ' 前缀 ')
        self.assertEqual(ui.single_line_text('a\r\nb'), 'ab')
        self.assertEqual(ui.single_line_text('a\x00b'), 'ab')
        self.assertIsNone(ui.single_line_text(None))

    def test_string_var_cleans_on_every_write(self):
        root = tk.Tk()
        root.withdraw()
        try:
            var = tk.StringVar(master=root, value='')
            ui.sanitize_single_line(var)
            var.set('前\n缀')          # 等价于粘贴多行文本
            root.update()
            self.assertEqual(var.get(), '前缀')
            var.set(' 后 ')            # 空格仍然保留
            root.update()
            self.assertEqual(var.get(), ' 后 ')
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main()

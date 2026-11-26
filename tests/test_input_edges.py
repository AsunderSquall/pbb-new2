import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from pbb import engine, flex, paths, ui
from pbb.autostart import StartUpdater
from pbb.config_file import load_config, save_config
from pbb.main import ConfigError, _build_task_config, _build_charset_entries, _try_resume, _validate_config


class InputEdgesTests(unittest.TestCase):
    def test_new_config_does_not_select_an_unsaved_path(self):
        owner = Mock(_pending_editor_cfg=ui.blank_config())
        ui.Monitor._open_editor_for(owner, 'draft.toml')
        owner._open_editor.assert_called_once_with(draft_path='draft.toml')
        owner._set_config_path.assert_not_called()

    def test_reports_invalid_shapes_and_charset_units(self):
        for field, value, message in (
            ('prefixes', [12], r'prefixes\[0\]'),
            ('enumeration', {'mode': 1, 'variable_length': 1, 'ranges': [0]}, r'ranges\[0\]'),
            ('character_set', {'single_char_length': 3, 'types': [8]}, 'Unicode 区间'),
            ('character_set', {'single_char_length': 3, 'types': [8],
                               'unicode_ranges': [{'start': 65, 'end': 70}]}, '必须为 3 字节'),
            ('character_set', {'single_char_length': 4, 'types': [2], 'custom_values': 'A😀BCD'}, 'UTF-8'),
            ('character_set', {'single_char_length': 1, 'types': [4], 'custom_values': [-1, 256]}, '0~255'),
            ('output', {'result_file': '../name.txt'}, '相对文件路径'),
        ):
            with self.subTest(field=field, value=value):
                cfg = ui.blank_config()
                cfg[field] = value
                with self.assertRaisesRegex(ConfigError, message):
                    _validate_config(cfg)

    def test_type_five_matches_engine_and_compound_units_are_valid(self):
        cfg = ui.blank_config()
        cs = {'single_char_length': 1, 'types': [5], 'custom_values': 'KZ'}
        self.assertEqual(_build_charset_entries(cs), ([b'K', b'Z'], 1))
        cfg['character_set'] = {'single_char_length': 4, 'types': [2], 'custom_values': 'INTJINTP😀'}
        _validate_config(cfg)

    def test_sequential_prefix_end_is_clipped_to_candidate_space(self):
        cfg = ui.blank_config()
        cfg['character_set'] = {'single_char_length': 1, 'types': [4], 'custom_values': 'AB'}
        layout = [{'count': 1, 'start': 0, 'end': 10}]
        cfg['enumeration'].update(mode=1, variable_length=2, ranges=[{'start': 0, 'end': -1}],
                                  prefix_ranges=copy.deepcopy(layout))
        _validate_config(cfg)
        self.assertEqual(_build_task_config(cfg)['prefix_range_R'], '4')
        self.assertEqual(cfg['enumeration']['prefix_ranges'], layout)

    def test_first_progress_save_preserves_concurrent_config_edit(self):
        cfg = ui.blank_config()
        cfg['enumeration']['ranges'] = [{'start': 0, 'end': 100}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.toml'
            save_config(path, cfg)
            updater = StartUpdater(path, cfg)
            edited = copy.deepcopy(cfg)
            edited['team_name'] = '外部编辑'
            save_config(path, edited)
            self.assertIsNone(updater.finish(10))
            self.assertEqual(load_config(path), edited)

    def test_checkpoint_at_old_global_end_does_not_skip_prefix_tail(self):
        cfg = ui.blank_config()
        cfg.pop('character_set')
        cfg['format'] = [{'charset': '数字'}] * 2
        cfg['character_sets'] = {'数字': {'single_char_length': 1, 'types': [1]}}
        cfg['enumeration'].update(mode=1, ranges=[{'start': 0, 'end': 70}])
        task = _build_task_config(cfg)
        layout, slots = flex.build_layout(task, engine._build_charset)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out'
            out.mkdir()
            checkpoint = out / 'r.txt.resume'
            checkpoint.write_text(f'{flex.resume_id(task, layout, slots)}\n0 30 30\n', encoding='ascii')
            resumed, _ = _try_resume(cfg, 'r.txt', directory)
        self.assertEqual(resumed['enumeration']['ranges'], [{'start': 30, 'end': 70}])

    def test_cli_malformed_inputs_and_overrides_have_focused_errors(self):
        cfg = ui.blank_config()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'edge.json'
            for text, args, field in (
                ('[]', [], '配置文件顶层'),
                ('{', [], '无法读取配置'),
                (json.dumps(cfg), ['--vlen', '2000'], '名字过长'),
                (json.dumps(cfg), ['--seed', str(2**64)], '--seed'),
                (json.dumps(cfg), ['--types', 'not-an-integer'], '--types'),
            ):
                with self.subTest(args=args, field=field):
                    path.write_text(text, encoding='utf-8')
                    result = subprocess.run(
                        [sys.executable, '-X', 'utf8', '-m', 'pbb.main', '-c', str(path),
                         '--no-auto-start', '--output-dest', 'stdout', *args],
                        cwd=paths.REPO_ROOT, capture_output=True, text=True, encoding='utf-8', timeout=20,
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(field, result.stderr)
                    self.assertNotIn('Traceback', result.stderr)


class NativeInputEdgesTests(unittest.TestCase):
    def test_single_entry_in_every_mode_and_unicode_result_path(self):
        for ui_engine in (False, True):
            if not paths.engine_binary(ui=ui_engine).is_file():
                self.skipTest('请先重建普通及 UI 引擎')
        task = dict(team_name='测试', scl=1, variable_len=1, n_threads=1, mode=1,
                    range_L=0, range_R=1, score_list='', prefixes=['+'], suffixes=['+'])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out'
            out.mkdir()
            for ui_engine in (False, True):
                for mode in (1, 2, 3, 4):
                    with self.subTest(ui_engine=ui_engine, mode=mode):
                        filename = f'结果😀{ui_engine}-{mode}.txt'
                        params = engine._build_params(dict(task, mode=mode), '41', filename)
                        result = subprocess.run(
                            [str(paths.engine_binary(ui=ui_engine))], input=params, cwd=directory,
                            capture_output=True, text=True, encoding='utf-8', timeout=10,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('contiguous=1', result.stderr)
                        self.assertTrue((out / filename).is_file())


if __name__ == '__main__':
    unittest.main()

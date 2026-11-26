"""配置校验与界面配置行为的回归用例。

覆盖 2026-10 的八条反馈里可以由纯逻辑验证的部分：
  * 区间测完后 `ranges[0].end == start` 仍可编辑/校验（反馈 2）；
  * 名字里的换行必须被明确拒绝，英文逗号应当能作为名字的一部分；
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
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from pbb import ui                                    # noqa: E402
from pbb.main import ConfigError, _config_warnings, _validate_config    # noqa: E402


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
    """允许合法标点，拒绝会破坏结果文件行结构的字符。"""

    def assert_rejected(self, **overrides):
        with self.assertRaisesRegex(ConfigError, '不能包含'):
            _validate_config(base_config(**overrides))

    def test_newline_in_prefix_is_rejected(self):
        # 反馈 6 的现场：前缀里带换行 → `prefixes=` 行被截断 → 第三个前缀静默不测。
        cfg = base_config()
        cfg['prefixes'] = [{'name': '一'}, {'name': '二\n '}, {'name': '三\n '}]
        with self.assertRaisesRegex(ConfigError, '换行'):
            _validate_config(cfg)

    def test_comma_in_prefix_suffix_and_team_is_allowed(self):
        cfg = base_config()
        cfg['prefixes'] = [{'name': 'MerryChristmas,Mr.Lawrence'}]
        cfg['suffixes'] = [{'name': '后,缀'}]
        cfg['team_name'] = 'A,B'
        _validate_config(cfg)

    def test_plain_spaces_are_still_allowed(self):
        cfg = base_config()
        cfg['prefixes'] = [{'name': ' 前 '}]
        cfg['suffixes'] = [{'name': '后 卫 '}]
        cfg['team_name'] = ' 队 伍 '
        _validate_config(cfg)


class CharacterAndLengthValidationTests(unittest.TestCase):
    """字节上限按实际 UTF-8 计算，提示不能代替或扩大硬限制。"""

    def test_character_width_bounds_and_input_errors(self):
        for width in (1, 64):
            cfg = base_config(variable_length=1)
            cfg['character_set'] = {'single_char_length': width, 'types': [1], 'custom_values': 'a' * width}
            _validate_config(cfg)
            self.assertEqual(ui._parse_charset_width(str(width)), width)
        for width in (0, -1, 65, True, 3.0, '3', None):
            cfg = base_config()
            cfg['character_set']['single_char_length'] = width
            with self.subTest(width=width), self.assertRaisesRegex(ConfigError, '1~64'):
                _validate_config(cfg)
        for raw in ('', '0', '65', '-1', '3.0', 'a', ' 3', '３'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, '1~64'):
                ui._parse_charset_width(raw)

    def test_empty_custom_values_only_require_content_when_selected(self):
        for width, builtin, custom_type in ((1, 1, 4), (1, 1, 5), (2, 1, 7),
                                             (3, 1, 7), (3, 5, 7), (4, 1, 2)):
            for empty in ('', [], None):
                with self.subTest(width=width, builtin=builtin, empty=empty):
                    cfg = base_config(variable_length=1)
                    cs = {'single_char_length': width, 'types': [builtin], 'custom_values': empty}
                    cfg['character_set'] = cs
                    _validate_config(cfg)
                    self.assertEqual(_config_warnings(cfg), [])
                    cfg['character_sets'] = {'内置': cs}
                    cfg['format'] = [{'charset': '内置'}]
                    _validate_config(cfg)
                    cfg.pop('character_sets')
                    cfg.pop('format')
                    cs['types'] = [builtin, custom_type]
                    with self.assertRaisesRegex(ConfigError, '自定义字符.*不能为空'):
                        _validate_config(cfg)

    def test_name_limit_counts_longest_affixes_in_utf8_bytes(self):
        cfg = base_config(variable_length=64)
        cfg['character_set'] = {'single_char_length': 3, 'types': [5]}
        cfg['prefixes'] = [{'name': '+'}, {'name': '中' * 20}]
        cfg['suffixes'] = [{'name': '+'}, {'name': 'abcd'}]
        _validate_config(cfg)  # 192 + 60 + 4 = 256，允许边界。
        cfg['suffixes'][1]['name'] += 'x'
        with self.assertRaisesRegex(ConfigError, '257.*256'):
            _validate_config(cfg)
        cfg['prefixes'] = cfg['suffixes'] = [{'name': '+'}]
        cfg['character_set'] = {'single_char_length': 1, 'types': [1]}
        cfg['enumeration']['variable_length'] = 256
        _validate_config(cfg)  # “+”不能算成额外的两个字节。

    def test_flex_width_and_total_name_limits(self):
        cfg = base_config()
        cfg['character_sets'] = {'字符': {'single_char_length': 64, 'types': [1], 'custom_values': 'a' * 64}}
        cfg['format'] = [{'charset': '字符'}] * 3 + ['x' * 58]
        _validate_config(cfg)  # 192 + 58 + “前后”的 6 字节 = 256。
        cfg['format'][-1] += 'x'
        with self.assertRaisesRegex(ConfigError, '257.*256'):
            _validate_config(cfg)
        cfg['character_sets']['字符']['single_char_length'] = 65
        with self.assertRaisesRegex(ConfigError, '1~64'):
            _validate_config(cfg)

    def test_long_random_part_is_only_a_warning(self):
        cfg = base_config(variable_length=64)
        self.assertEqual(_config_warnings(cfg), [])
        cfg['enumeration']['variable_length'] = 65
        _validate_config(cfg)
        self.assertIn('随机部分较长', '\n'.join(_config_warnings(cfg)))
        cfg['character_sets'] = {'数字': cfg['character_set']}
        cfg['format'] = [{'charset': '数字'}] * 65
        cfg.pop('prefixes')
        cfg.pop('suffixes')
        _validate_config(cfg)
        self.assertIn('随机部分较长', '\n'.join(_config_warnings(cfg)))

    def test_invisible_and_invalid_unicode_warn_without_changing_content(self):
        cfg = base_config()
        cfg['character_set'] = {'single_char_length': 3, 'types': [7]}
        for custom in ('你好\u200b', 'a\n', ' ', '\ud800', '\uffff', '\u2800', [0xff]):
            with self.subTest(custom=repr(custom)):
                cfg['character_set']['custom_values'] = custom
                self.assertIn('自定义字符中出现非法字符', '\n'.join(_config_warnings(cfg)))
                self.assertEqual(cfg['character_set']['custom_values'], custom)
        for custom in ('你好世界', '🐒🐵', [0xE4, 0xBD, 0xA0]):
            cfg['character_set']['custom_values'] = custom
            self.assertEqual(_config_warnings(cfg), [])
        cfg['character_set'].update(types=[5], custom_values='\u200b')
        self.assertEqual(_config_warnings(cfg), [])

    def test_save_warns_but_allows_long_random_and_rejects_overlong_names(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ui, 'CONFIG_DIR', directory):
            for length in (65, 257):
                cfg = base_config(variable_length=length)
                cfg['prefixes'] = cfg['suffixes'] = [{'name': '+'}]
                owner = types.SimpleNamespace(
                    _editor_config=lambda: cfg, _confirm_affix_mismatch=lambda _: True,
                    editor_name_var=Mock(get=lambda: str(length)), config_var=Mock(),
                    _set_config_path=Mock(), _sync_config_controls=Mock(), status_var=Mock(), editor=None,
                )
                with patch.object(ui.messagebox, 'showwarning') as warning, \
                     patch.object(ui.messagebox, 'showerror') as error:
                    ui.Monitor._save_editor(owner)
                target = Path(directory) / f'{length}.toml'
                self.assertEqual(target.exists(), length == 65)
                if length == 65:
                    warning.assert_called_once()
                    error.assert_not_called()
                    self.assertEqual(ui.load_config(str(target))['enumeration']['variable_length'], 65)
                else:
                    error.assert_called_once()
                    warning.assert_not_called()


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
            # 设置文件在 SETTINGS_DIR，测号配置在 CONFIG_DIR —— 两个都要指到临时目录，
            # 否则测试会改写仓库里真实的 settings/ui_settings.json。
            with patch.object(ui, 'SETTINGS_DIR', os.path.join(temp, 'settings')), \
                 patch.object(ui, 'CONFIG_DIR', temp), \
                 patch.object(ui, 'load_config', side_effect=lambda path: {'ok': True}), \
                 patch.object(ui.ConfigSelector, 'is_search_config', staticmethod(lambda cfg: True)):
                ui.save_ui_settings({'default_config': 'real.yaml'})
                self.assertEqual(os.path.basename(ui.default_config_path()), 'real.yaml')
                # 设置了但文件不存在 → 回退到 config.yaml
                ui.save_ui_settings({'default_config': 'missing.yaml'})
                self.assertEqual(os.path.basename(ui.default_config_path()), 'config.yaml')

    def test_ui_settings_round_trip_and_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(ui, 'SETTINGS_DIR', os.path.join(temp, 'settings')):
                ui.save_ui_settings({'default_config': 'a.yaml', 'other': 1})
                self.assertEqual(ui.load_ui_settings(), {'default_config': 'a.yaml', 'other': 1})
                Path(ui.ui_settings_path()).write_text('{ not json', encoding='utf8')
                self.assertEqual(ui.load_ui_settings(), {})


class DefaultThresholdsTests(unittest.TestCase):
    """「编辑新配置」的默认评分阈值来自 settings/ui_settings.json。"""

    def test_blank_config_uses_ui_settings_collection(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(ui, 'SETTINGS_DIR', os.path.join(temp, 'settings')):
                ui.save_ui_settings({'collection': {'min_qp': 1234, 'ptt_cp': 5678, 'score_list': 'qp'}})
                cfg = ui.blank_config()
                self.assertEqual(cfg['collection']['min_qp'], 1234)
                self.assertEqual(cfg['collection']['ptt_cp'], 5678)
                self.assertEqual(cfg['collection']['score_list'], 'qp')

    def test_repository_ui_settings_match_the_football_config(self):
        """仓库里的 ui_settings.json 必须就是足球配置的阈值（随包默认值）。"""
        import yaml
        football = yaml.safe_load(
            (Path(ui.CONFIG_DIR) / 'config_football.yaml').read_text(encoding='utf8'))
        shipped = json.loads((Path(ui.SETTINGS_DIR) / 'ui_settings.json').read_text(encoding='utf8'))
        self.assertEqual(shipped.get('collection'), football['collection'])


class EditorDefaultButtonsTests(unittest.TestCase):
    """配置编辑页「评分」页上的「使用默认评分」/「设为默认评分」。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(dir=str(ROOT / 'build'))
        self.settings_dir = os.path.join(self._temp.name, 'settings')
        self._patches = [patch.object(ui, 'SETTINGS_DIR', self.settings_dir)]
        for item in self._patches:
            item.start()
        ui.save_ui_settings({'default_config': 'config_football.yaml',
                             'collection': {'min_qp': 5400, 'score_list': 'qp,qd',
                                            'score_mode': 'all', 'collect_mode': 1}})
        self.app = ui.Monitor()
        self.app.withdraw()
        self.app._open_editor()
        self.app.update()

    def tearDown(self):
        self.app._close_editor()
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()
        for item in reversed(self._patches):
            item.stop()
        self._temp.cleanup()

    def test_use_default_scores_fills_thresholds_only(self):
        self.app.editor_vars['collection.min_qp'].set('1')
        self.app.editor_vars['team_name'].set('不该被默认值覆盖')
        self.app._use_default_scores()
        self.assertEqual(self.app.editor_vars['collection.min_qp'].get(), '5400')
        self.assertEqual(self.app.editor_vars['team_name'].get(), '不该被默认值覆盖')

    def test_use_default_scores_maps_dropdown_labels(self):
        """评分模式/收集模式是下拉框：默认值要按显示文案填回，保存能取回原值。"""
        self.app.editor_vars['collection.score_mode'].set('任一达标即停止后续评分')
        self.app.editor_vars['collection.collect_mode'].set('不收集特殊号')
        self.app._use_default_scores()
        self.assertEqual(self.app.editor_vars['collection.score_mode'].get(), '全部评分后判断')
        self.assertEqual(self.app.editor_vars['collection.collect_mode'].get(), '收集预设阈值特殊号')
        collection = self.app._editor_config()['collection']
        self.assertEqual(collection['score_mode'], 'all')
        self.assertEqual(collection['collect_mode'], 1)

    def test_save_as_default_scores_writes_collection_only(self):
        self.app.editor_vars['collection.min_qp'].set('1234')
        with patch.object(ui.messagebox, 'askyesno', return_value=True) as ask:
            self.app._save_as_default_scores()
        ask.assert_called_once()
        saved = ui.load_ui_settings()
        self.assertEqual(saved['collection']['min_qp'], 1234)
        # 「设为默认评分」不该改启动默认配置（那是选择器「设为启动默认」的职责）。
        self.assertEqual(saved['default_config'], 'config_football.yaml')

    def test_default_score_buttons_live_on_the_score_page(self):
        """两个按钮必须在「评分」页里，且编辑器顶部不再有它们的旧版本。"""
        import tkinter.ttk as ttk
        notebook = [w for w in self.app.editor.winfo_children() if isinstance(w, ttk.Notebook)][0]
        tabs = {notebook.tab(tab, 'text'): tab for tab in notebook.tabs()}

        def texts(widget):
            for child in widget.winfo_children():
                if child.winfo_class() == 'TButton':
                    yield str(child.cget('text'))
                yield from texts(child)

        score_page = notebook.nametowidget(tabs['评分'])
        self.assertIn('使用默认评分', set(texts(score_page)))
        self.assertIn('设为默认评分', set(texts(score_page)))
        self.assertNotIn('使用默认配置', set(texts(self.app.editor)))
        self.assertNotIn('设为默认配置', set(texts(self.app.editor)))

    def test_selector_marks_the_default_row(self):
        selector = ui.ConfigSelector(self.app)
        try:
            self.app.update()
            rows = {selector.tree.item(item, 'values')[0]: selector.tree.item(item, 'values')[-1]
                    for item in selector.tree.get_children()}
            self.assertEqual(rows.get('config_football.yaml'), '★ 默认')
            self.assertTrue(all(value == '' for name, value in rows.items()
                                if name != 'config_football.yaml'))
        finally:
            selector.destroy()


class SmallScreenLayoutTests(unittest.TestCase):
    """1366x768 / 100% 缩放下，主窗口底部的「开始」按钮必须在窗口内。"""

    def _close(self, app):
        """销毁前先取消挂起的 after 回调，否则 Tk 会打印 ("after" script) 错误。"""
        for callback in app.tk.call('after', 'info'):
            app.after_cancel(callback)
        app.destroy()

    def test_start_button_stays_visible_when_window_is_short(self):
        app = ui.Monitor()
        try:
            for height in (700, 646, 560):
                app.geometry(f"1100x{height}")
                app.update()
                app.update_idletasks()
                button = app.start_button
                bottom = button.winfo_rooty() - app.winfo_rooty() + button.winfo_height()
                self.assertTrue(button.winfo_ismapped(), f'h={height}: 开始按钮没有显示')
                self.assertLessEqual(bottom, app.winfo_height(),
                                     f'h={height}: 开始按钮底部 {bottom} 超出窗口 {app.winfo_height()}')
        finally:
            self._close(app)

    def test_sidebar_controls_scroll_instead_of_clipping(self):
        app = ui.Monitor()
        try:
            app.geometry("1100x560")
            app.update()
            app.update_idletasks()
            box = app.sidebar_canvas.bbox('all')
            self.assertIsNotNone(box)
            self.assertGreater(box[3] - box[1], app.sidebar_canvas.winfo_height(),
                               '侧栏内容比视口高时应当可滚动')
        finally:
            self._close(app)


class PeriodicStartAdvanceTests(unittest.TestCase):
    """每测 0.01T 由引擎进度行触发一次 start 回写（不轮询、不开线程）。"""

    def _close(self, app):
        for callback in app.tk.call('after', 'info'):
            app.after_cancel(callback)
        app.destroy()

    def test_crossing_each_step_writes_once(self):
        app = ui.Monitor()
        try:
            app._start_advance_mark = ui.autostart.STEP
            calls = []
            app._advance_config_start = lambda final=True: calls.append(final)
            # 引擎的进度行是 count:X.XXXXXXT；0.01T 对应 0.010000T。
            app._handle_line("task1 finished,task_mex=0,count:0.005000T")
            self.assertEqual(calls, [], "还没到 0.01T 不该写盘")
            app._handle_line("task2 finished,task_mex=1,count:0.010000T")
            self.assertEqual(calls, [False], "跨过 0.01T 应写一次（中途写盘，不标记完成）")
            app._handle_line("task3 finished,task_mex=2,count:0.015000T")
            self.assertEqual(calls, [False], "同一阶梯内不该重复写")
            app._handle_line("task4 finished,task_mex=3,count:0.020000T")
            self.assertEqual(calls, [False, False])
        finally:
            self._close(app)


class OutputDirectoryTests(unittest.TestCase):
    """测号输出落在包根 out/，不再埋进 source/ 或 PBBMonitor/_internal/。"""

    def test_out_dir_is_the_package_root_out(self):
        from pbb import paths
        self.assertEqual(paths.OUT_DIR, paths.PACKAGE_ROOT / 'out')
        self.assertEqual(paths.OUT_DIR.name, 'out')
        self.assertEqual(ui.OUT_DIR, str(paths.OUT_DIR))
        self.assertEqual(ui.RESULT_ROOT, str(paths.PACKAGE_ROOT))

    def test_thread_control_file_lands_in_the_package_out(self):
        from pbb import paths
        self.assertEqual(ui.Monitor._thread_control_path(),
                         os.path.join(str(paths.OUT_DIR), '.threads'))


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


class LowThreadWarningTests(unittest.TestCase):
    """启动线程低于自动值的 75% 时先确认：是=继续启动，否=取消。"""

    def setUp(self):
        self.app = ui.Monitor()

    def tearDown(self):
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()

    def confirm(self, threads, auto, answer=False):
        with patch.object(ui, 'available_threads', return_value=auto), \
             patch.object(ui.messagebox, 'askyesno', return_value=answer) as ask:
            self.app.threads_var.set(str(threads))
            return self.app._confirm_thread_count(), ask

    def test_below_75_percent_asks_and_can_abort(self):
        allowed, ask = self.confirm(6, 32, answer=False)
        self.assertFalse(allowed, '选「否」时不应继续启动')
        ask.assert_called_once()
        self.assertIn('75%', ask.call_args.args[1])
        allowed, ask = self.confirm(6, 32, answer=True)
        self.assertTrue(allowed, '选「是」时照原样启动')

    def test_at_or_above_75_percent_starts_silently(self):
        for threads in (24, 32):          # 24/32 正好 75%，要求"低于"才提示
            allowed, ask = self.confirm(threads, 32)
            self.assertTrue(allowed, threads)
            ask.assert_not_called()

    def test_auto_threads_never_prompt(self):
        allowed, ask = self.confirm(-1, 32)
        self.assertTrue(allowed)
        ask.assert_not_called()

    def test_declining_does_not_start_a_run(self):
        with patch.object(ui, 'available_threads', return_value=32), \
             patch.object(ui.messagebox, 'askyesno', return_value=False):
            self.app.threads_var.set('4')
            self.app._start()
        self.assertFalse(self.app.running, '取消后不应进入运行态')
        self.assertIn('已取消启动', self.app.status_var.get())


if __name__ == '__main__':
    unittest.main()

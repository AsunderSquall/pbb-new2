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


if __name__ == '__main__':
    unittest.main()

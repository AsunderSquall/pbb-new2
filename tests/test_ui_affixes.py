import copy
from pathlib import Path
import sys
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

# 测试文件位于 tests/ 子目录，被测代码在 pbb/ui.py；先把仓库根加入 sys.path。
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / 'tools'))

from pbb import ui
from pbb.ui import Monitor, load_config


class AffixWarningTests(unittest.TestCase):
    # 本组用例守护“一一对应”（枚举模式4）下的前后缀配对语义，以及保存/启动不被
    # 用户取消绕过。
    # 模式4 要求前后缀按序号配对：数量不等时必须先弹确认框，且提示语要分方向——
    # 后缀少是“按下标取模循环使用”，后缀多是“多出的后缀不会参与配对”。
    # 用户选“否”则返回 False（调用方据此放弃保存），不能让数量不匹配静默通过。
    # 注意：下面的期望文案是既有用例的一部分，属于项目原有内容，不要改动。
    def test_mismatch_warning_both_directions(self):
        for np, ns, expected in ((3, 1, "取模"), (1, 3, "不会参与")):
            cfg = {"enumeration": {"mode": 4}, "prefixes": [{}] * np, "suffixes": [{}] * ns}
            with patch.object(ui.messagebox, "askyesno", return_value=False) as dialog:
                self.assertFalse(Monitor._confirm_affix_mismatch(SimpleNamespace(editor=None), cfg))
                self.assertIn(expected, dialog.call_args.args[1])

    # 数量相等时不打扰用户；非模式4（这里 mode=2）本来就不按序号配对，也不该弹框。
    # 防的是把提示条件放宽，导致每次保存都跳出无意义的确认。
    def test_equal_counts_and_other_modes_do_not_warn(self):
        with patch.object(ui.messagebox, "askyesno") as dialog:
            for mode, ns in ((4, 1), (2, 3)):
                cfg = {"enumeration": {"mode": mode}, "prefixes": [{}], "suffixes": [{}] * ns}
                self.assertTrue(Monitor._confirm_affix_mismatch(SimpleNamespace(editor=None), cfg))
            dialog.assert_not_called()

    # 用户在上面的配对确认里取消后，既不能写配置文件，也不能顺势启动任务
    # （start=True 的“保存并启动”路径同样要被拦住）。
    # builtins.open 被 mock 成 mock_open，用来断言“根本没写文件”。
    def test_cancel_prevents_save_and_start(self):
        for start in (False, True):
            owner = SimpleNamespace(_editor_config=Mock(return_value={}), editor=None,
                                    _confirm_affix_mismatch=Mock(return_value=False), _start=Mock())
            with patch.object(ui, "_validate_config"), patch("builtins.open", mock_open()) as output:
                Monitor._save_editor(owner, start=start)
                output.assert_not_called()
                owner._start.assert_not_called()


class AffixEditorTests(unittest.TestCase):
    # 夹具启动真实 Monitor（编辑器行为依赖 Tk 控件状态），但把 Tk/Toplevel 初始化
    # 包起来并 withdraw，避免弹窗；同时 patch 掉字体、窗口图标等与断言无关的副作用，
    # 并用固定的足球配置替换 load_config，得到 3 前缀 / 1 后缀的模式4 场景。
    def setUp(self):
        cfg = load_config("config_football.yaml")
        cfg["enumeration"]["mode"] = 4
        cfg["prefixes"] = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        cfg["suffixes"] = [{"name": "X"}]
        tk_init, top_init = tk.Tk.__init__, tk.Toplevel.__init__
        def hidden_tk(window, *args, **kwargs):
            tk_init(window, *args, **kwargs)
            window.withdraw()
        def hidden_top(window, *args, **kwargs):
            top_init(window, *args, **kwargs)
            window.withdraw()
        with patch.object(ui, "load_ui_font"), patch.object(ui, "configure_windows_identity"), \
             patch.object(ui, "apply_windows_window_icon"), patch.object(tk.Tk, "__init__", hidden_tk), \
             patch.object(tk.Toplevel, "__init__", hidden_top), patch.object(ui, "load_config", return_value=copy.deepcopy(cfg)):
            self.app = Monitor()
            self.app._open_editor()
            self.app.update_idletasks()
        self.addCleanup(self.app.destroy)

    # 两种前后缀视图（分开编辑 / 一一对应编辑）来回切换时，用户已填的前缀名、起始
    # 范围和后缀都不能丢：转换只改展示方式，不改变配置语义。
    def test_round_trip_preserves_mismatch_and_ranges(self):
        app = self.app
        self.assertEqual(app.affix_view, "一一对应编辑")
        app.pair_rows[0]["start"].set("42")
        app.pair_rows[1]["prefix"].set("B2")
        for view in ("分开编辑", "一一对应编辑", "分开编辑"):
            app.affix_view_var.set(view)
            app._switch_affix_view()
            prefixes, suffixes = app._affix_editor_data()
            self.assertEqual([item[0] for item in prefixes], ["A", "B2", "C"])
            self.assertEqual(prefixes[0][1], "42")
            self.assertEqual(suffixes, ["X"])

    # 一一对应视图里后缀比前缀少时，前面缺后缀的行用 "+" 占位：只补第 3 行的后缀
    # 必须仍落在下标 2，不能被压缩到第 2 行（否则前缀范围与后缀就配错了对）。
    # 之后新增一行时，前后缀数量要继续保持一致。
    def test_filling_later_missing_suffix_keeps_index(self):
        self.app.pair_rows[2]["suffix"].set("Z")
        _, suffixes = self.app._affix_editor_data()
        self.assertEqual(suffixes, ["X", "+", "Z"])
        self.app._add_pair_row()
        cfg = self.app._editor_config()
        self.assertEqual(len(cfg["prefixes"]), 4)
        self.assertEqual(len(cfg["suffixes"]), 4)

    # 分开编辑视图要求前后缀两个区块并排（同一 grid 行的第 0/1 列），
    # 且容器用 pack 真正挂上；防的是切换视图后有一侧被隐藏或叠在一起。
    def test_separate_view_uses_side_by_side_sections(self):
        self.app.affix_view_var.set("分开编辑")
        self.app._switch_affix_view()
        self.assertEqual(self.app.prefix_section.grid_info()["column"], 0)
        self.assertEqual(self.app.suffix_section.grid_info()["column"], 1)
        self.assertEqual(self.app.prefix_section.grid_info()["row"],
                         self.app.suffix_section.grid_info()["row"])
        self.assertEqual(self.app.separate_affix_sections.winfo_manager(), "pack")

    # 辅助方法：把 editor_cfg 换成指定的 character_set 后关掉再重开编辑器，
    # 模拟用户修改字节宽度/类型后重新载入配置，让测试从真实控件状态出发。
    def reopen_charset(self, charset):
        app=self.app;cfg=copy.deepcopy(app.editor_cfg);cfg['character_set']=charset
        app._close_editor()
        top_init=tk.Toplevel.__init__
        def hidden_top(window,*args,**kwargs):
            top_init(window,*args,**kwargs);window.withdraw()
        with patch.object(ui,'load_config',return_value=cfg),patch.object(ui,'apply_windows_window_icon'), \
             patch.object(tk.Toplevel,'__init__',hidden_top):app._open_editor()
        app.update_idletasks()

    # 字符类型是多选，且必须原样保留配置里的顺序与重复项（[3,1,3] 不能去重或排序）；
    # 新勾选的类型追加到末尾。选中“自定义”类型时自定义输入框才出现，取消则收起。
    def test_multiselect_preserves_charset_order_and_duplicates(self):
        self.reopen_charset(dict(single_char_length=1,types=[3,1,3],custom_values='xyz'))
        app=self.app
        self.assertEqual(app._editor_config()['character_set']['types'],[3,1,3])
        self.assertFalse(app.charset_custom_frame.winfo_manager())
        app.charset_type_vars[4].set(True)
        self.assertEqual(app._editor_config()['character_set']['types'],[3,1,3,4])
        self.assertEqual(app.charset_custom_frame.winfo_manager(),'grid')
        app.charset_type_vars[4].set(False)
        self.assertFalse(app.charset_custom_frame.winfo_manager())

    # 自定义字符与 Unicode 区间是两个独立输入：取消自定义类型只收起自定义框，
    # Unicode 框要留着；区间文本按 UI 约定解析为 start/end（含端点），
    # 每行一段。防的是两者共用显隐开关或解析成半开区间。
    def test_unicode_and_custom_inputs_are_independent(self):
        self.reopen_charset(dict(single_char_length=3,types=[1,7,8],custom_values='天地',
            unicode_ranges=[dict(start=0x4e00,end=0x4e10)]))
        app=self.app
        self.assertEqual(app.charset_custom_frame.winfo_manager(),'grid')
        self.assertEqual(app.charset_unicode_frame.winfo_manager(),'grid')
        self.assertEqual(app._editor_config()['character_set']['unicode_ranges'],[dict(start=0x4e00,end=0x4e10)])
        app.charset_type_vars[7].set(False)
        self.assertFalse(app.charset_custom_frame.winfo_manager())
        self.assertEqual(app.charset_unicode_frame.winfo_manager(),'grid')
        app.charset_unicode_text.delete('1.0','end');app.charset_unicode_text.insert('1.0','U+4E20-U+4E22')
        self.assertEqual(app._editor_config()['character_set']['unicode_ranges'],[dict(start=0x4e20,end=0x4e22)])

    # 每个字节宽度各自记住自己的类型选择：1→2→1 来回切换后，1 字节那份选择
    # （[3,1]）必须原样恢复，不能把 2 字节下勾选的类型带过来。
    # 同时自定义内容要能在数字字节列表与文本之间正确往返。
    def test_byte_width_switch_restores_each_selection(self):
        self.reopen_charset(dict(single_char_length=1,types=[3,1],custom_values=[65,66]))
        app=self.app
        self.assertEqual(app._editor_config()['character_set']['custom_values'],[65,66])
        app.editor_vars['character_set.single_char_length'].set('2');app._refresh_charset_types()
        app.charset_type_vars[2].set(True)
        app.editor_vars['character_set.single_char_length'].set('1');app._refresh_charset_types()
        self.assertEqual(app._editor_config()['character_set']['types'],[3,1])

    # score_list 由界面自动维护：在某族上填了正阈值，保存时该族必须自动进入 score_list。
    #
    # 背景：校验规则要求"阈值>0 ⇒ 该族必须在 score_list 里"（否则设了阈值却不计算，
    # 门控形同虚设）。但界面上每族都有独立的阈值输入框，用户填了 CQD 阈值却被告知
    # 还得自己去改 score_list 那串族名 —— 那等于把内部一致性的负担推给用户，
    # 而且不满足时监控器还会直接退出（见 ConfigLocationTests 里那条闪退回归）。
    # 现在由 _effective_score_list() 自动补齐，并在保存前同步回输入框让用户看到结果。
    def test_threshold_auto_adds_family_to_score_list(self):
        app = self.app
        app.editor_vars["collection.score_list"].set("qp,qd,cp,fp,wc,fs,pj")
        for family in ui.FAMILIES:
            app.editor_vars[f"collection.min_{family}"].set("0")
        app.editor_vars["collection.min_cqd"].set("4000")

        cfg = app._editor_config()
        self.assertEqual(cfg["collection"]["score_list"], "qp,qd,cqd,cp,fp,wc,fs,pj")
        # 界面上的输入框也要跟着更新，否则用户看到的还是旧值。
        self.assertEqual(app.editor_vars["collection.score_list"].get(),
                         "qp,qd,cqd,cp,fp,wc,fs,pj")

    # 已有族不重复追加；顺序统一按 FAMILIES 规范序，避免同一份配置因书写顺序不同
    # 而反复产生 diff。
    def test_score_list_deduplicates_and_normalizes_order(self):
        app = self.app
        app.editor_vars["collection.score_list"].set("fs,fs,rh,qp")
        # min_* 与 ptt_* 都要清零：两者都代表"要算这一族"，只清一个会被自动补回来。
        for family in ui.FAMILIES:
            app.editor_vars[f"collection.min_{family}"].set("0")
        for family in ("cp", "fp", "wc", "fs", "pj"):
            app.editor_vars[f"collection.ptt_{family}"].set("0")
        cfg = app._editor_config()
        # rh 归一到 fs 且只出现一次；结果按规范序（qp 在 fs 之前）。
        self.assertEqual(cfg["collection"]["score_list"], "qp,fs")

    # PTT 阈值同样代表"要算这一族"，也必须触发纳入（否则 ptt_* 门控会失效）。
    def test_ptt_threshold_also_adds_family(self):
        app = self.app
        app.editor_vars["collection.score_list"].set("qp,qd,cp,fp,wc,pj")
        for family in ui.FAMILIES:
            app.editor_vars[f"collection.min_{family}"].set("0")
        for family in ("cp", "fp", "wc", "fs", "pj"):
            app.editor_vars[f"collection.ptt_{family}"].set("0")
        app.editor_vars["collection.ptt_fs"].set("4300")
        cfg = app._editor_config()
        self.assertIn("fs", cfg["collection"]["score_list"].split(","))

    # 反向也要成立：把阈值改回 0 并删掉该族，才能真正停算它 —— 自动补齐不能把
    # 用户的显式选择又加回来。
    def test_zero_threshold_lets_family_be_removed(self):
        app = self.app
        for family in ui.FAMILIES:
            app.editor_vars[f"collection.min_{family}"].set("0")
        for family in ("cp", "fp", "wc", "fs", "pj"):
            app.editor_vars[f"collection.ptt_{family}"].set("0")
        app.editor_vars["collection.score_list"].set("qp,cp,fp,wc,fs,pj")
        cfg = app._editor_config()
        self.assertNotIn("qd", cfg["collection"]["score_list"].split(","))

    # 结果文件的三模式：模式决定写入 "+"/"-"，自定义才写文件名。
    # 背景：output.result_file 里 "+"=随机名、"-"=时间戳（见 main._resolve_result_file），
    # 以前是一个裸输入框，用户填 "-" 会被引擎当成字面文件名，在 out/ 下写出名为 "-" 的文件。
    def test_result_file_mode_maps_to_sign_or_custom_name(self):
        app = self.app
        mode = app.editor_vars["output.result_file.mode"]
        name = app.editor_vars["output.result_file.name"]

        mode.set("随机名")
        self.assertEqual(app._editor_config()["output"]["result_file"], "+")

        mode.set("时间戳")
        self.assertEqual(app._editor_config()["output"]["result_file"], "-")

        mode.set("自定义")
        name.set("my_result.txt")
        self.assertEqual(app._editor_config()["output"]["result_file"], "my_result.txt")

    # 自定义模式留空时必须退回时间戳：否则会写出空文件名，
        # 引擎拼出 "out/" 的目录路径而打不开结果文件（直接报错退出）。
    def test_empty_custom_name_falls_back_to_timestamp(self):
        app = self.app
        app.editor_vars["output.result_file.mode"].set("自定义")
        app.editor_vars["output.result_file.name"].set("   ")
        self.assertEqual(app._editor_config()["output"]["result_file"], "-")

    # 三个可滚动区域（前缀/后缀/排行榜）里的子控件都必须绑上滚轮。
    # 原来只绑了 canvas 自身，鼠标停在里面的标签/输入框上就完全滚不动 ——
    # 表现为"只能拖滚动条"。
    def test_scroll_areas_bind_wheel_on_child_widgets(self):
        app = self.app
        # 前后缀区：加几行再检查
        for _ in range(5):
            app._add_prefix_row("P")
        for _ in range(5):
            app._add_suffix_row("S")
        app.update_idletasks()

        def descendants(widget):
            out, stack = [], [widget]
            while stack:
                current = stack.pop()
                out.append(current)
                stack.extend(current.winfo_children())
            return out

        for label, canvas, container in (
            ("前缀区", app._canvas_of(app.prefix_rows_frame), app.prefix_rows_frame),
            ("后缀区", app._canvas_of(app.suffix_rows_frame), app.suffix_rows_frame),
        ):
            self.assertIsNotNone(canvas, f"{label} 未登记 canvas")
            unbound = [w for w in descendants(container) if not w.bind("<MouseWheel>")]
            self.assertEqual(unbound, [], f"{label} 有未绑定滚轮的控件: {unbound[:3]}")

    # 幂等性：重复绑定同一个控件会让一次滚轮滚动多倍距离，必须只绑一次。
    def test_repeated_binding_is_idempotent(self):
        app = self.app
        app._add_prefix_row("P")
        app.update_idletasks()
        canvas = app._canvas_of(app.prefix_rows_frame)
        container = app.prefix_rows_frame
        app._bind_wheel(canvas, container)
        first = container.winfo_children()[0]
        before = first.bind("<MouseWheel>")
        app._bind_wheel(canvas, container)
        self.assertEqual(first.bind("<MouseWheel>"), before)

    # 像素换算：Frame 的 width / Label 的 wraplength 都是物理像素，
    # 这类尺寸必须经 pixels() 换算，否则高 DPI 下会被挤扁（侧栏 390 只相当于 260 逻辑像素）。
    def test_pixels_scales_with_dpi(self):
        scale = ui.windows_dpi_scale()
        self.assertEqual(ui.pixels(390), round(390 * scale))
        self.assertGreaterEqual(ui.pixels(1), 1)


if __name__ == "__main__":
    unittest.main()

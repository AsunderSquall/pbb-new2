from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

# 测试文件位于 tests/ 子目录，被测代码在 pbb/ui.py；先把仓库根加入 sys.path。
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / 'tools'))

from pbb import ui
from pbb.ui import Monitor


class RowDragTests(unittest.TestCase):
    # 本组用例守护两件事：配置导入后评分面板要立刻跟上新配置；前后缀行的拖拽排序
    # 要有阈值、要真的重排，并且不能顺带触发控件自身的点击行为。
    # 导入/保存配置时（未运行状态）评分面板必须按新的 score_list 重建：families
    # 更新、boards 重建、展开状态只保留仍然存在的评分族（交集），并各刷新一次面板。
    # 防的是导入配置后界面还显示旧评分族，用户以为在跑新配置。
    def test_import_updates_idle_score_panels(self):
        owner = SimpleNamespace(
            running=False, threads_var=Mock(), range_var=Mock(),
            _apply_result_file_setting=Mock(),
            families=["qp"], boards={}, expanded_families={"qp", "fp"},
            _cfg_value=Monitor._cfg_value, _families_from_task=Monitor._families_from_task,
            _render_score_boxes=Mock(), _refresh_boards=Mock(),
        )
        Monitor._sync_config_controls(owner, {"collection": {"score_list": "qd,fp"}})
        self.assertEqual(owner.families, ["qd", "fp"])
        self.assertEqual(owner.expanded_families, {"fp"})
        owner._render_score_boxes.assert_called_once_with(["qd", "fp"])
        owner._refresh_boards.assert_called_once()

    # 文本框不能成为拖拽手柄：否则在输入框里按下拖动会被当成重排，破坏文本选择。
    # 其余控件（行框架、标签、按钮）都要绑定按下/移动/松开三种事件，且各绑一次。
    # isinstance 被替换成自定义判断，是为了在无 Tk 环境下模拟 ttk.Entry/ttk.Button 的类型分支。
    def test_entries_are_not_drag_targets(self):
        entry = Mock()
        label = Mock()
        button = Mock()
        for widget in (entry, label, button):
            widget.winfo_children.return_value = []
        frame = Mock()
        frame.winfo_children.return_value = [entry, label, button]
        owner = SimpleNamespace()
        with patch.object(ui, "isinstance", side_effect=lambda widget, cls: (
            widget is entry if cls.__name__ == "Entry" else widget is button
        ), create=True):
            Monitor._bind_row_drag(owner, {"frame": frame}, "prefix")
        entry.bind.assert_not_called()
        for widget in (frame, label, button):
            self.assertEqual(widget.bind.call_count, 3)

        # 1) 位移在 5 像素以内视为误触，不算拖拽（_drag_row 返回 None，行序不变）；
        #    两行各高 20，夹具用 winfo_rooty/winfo_height 提供中点用于判断落点。
        # 2) 越过阈值且落点在下一行中点之后（y=45）时，把该行插入到目标位置并重编号，
        #    返回 "break" 阻止事件继续传播。
        # 3) 拖拽结束后（_finish_row_drag）同样返回 "break" 吞掉随之而来的点击，
        #    避免误触按钮动作，并清掉拖拽状态。
    def test_drag_threshold_reorder_and_click_suppression(self):
        rows = []
        for y in (0, 20):
            frame = Mock()
            frame.winfo_rooty.return_value = y
            frame.winfo_height.return_value = 20
            rows.append({"frame": frame})
        first = rows[0]
        owner = SimpleNamespace(prefix_rows=rows, _reindex_prefix_rows=Mock())
        event = SimpleNamespace(x_root=0, y_root=5, widget=object())
        Monitor._start_row_drag(owner, event, first, "prefix")
        event.y_root = 8
        self.assertIsNone(Monitor._drag_row(owner, event, "prefix"))
        self.assertIs(rows[0], first)
        self.assertIsNone(Monitor._finish_row_drag(owner, event, "prefix"))
        Monitor._start_row_drag(owner, event, first, "prefix")
        event.y_root = 45
        self.assertEqual(Monitor._drag_row(owner, event, "prefix"), "break")
        self.assertIs(rows[1], first)
        owner._reindex_prefix_rows.assert_called_once()
        self.assertEqual(Monitor._finish_row_drag(owner, event, "prefix"), "break")
        self.assertIsNone(owner._drag_state)


class RuntimeThreadTests(unittest.TestCase):
    # 「活跃线程」的运行时可调逻辑。用 SimpleNamespace 当 owner，避免真起 Tk。
    def make_owner(self, limit=32):
        owner = SimpleNamespace(
            running=True, runtime_thread_limit=limit, runtime_active_threads=limit,
            runtime_threads_var=Mock(), status_var=Mock(), _write_runtime_threads=Mock(),
        )
        owner.runtime_threads_var.get.return_value = str(limit)
        return owner

    # 0 必须是合法值：它表示暂停计算（引擎侧让所有 worker 睡下、生产者休眠等待），
    # 不是"非法输入"。早期实现用 max(1, min(...)) 把 0 夹成 1，用户根本无法暂停。
    def test_zero_is_accepted_and_pauses(self):
        owner = self.make_owner()
        owner.runtime_threads_var.get.return_value = "0"
        Monitor._apply_runtime_threads(owner)
        owner._write_runtime_threads.assert_called_once_with(0)
        self.assertEqual(owner.runtime_active_threads, 0)
        # 状态栏要说清是"暂停"，不能只显示一个 0 让用户猜
        self.assertIn("暂停", owner.status_var.set.call_args[0][0])

    def test_upper_bound_and_normal_values(self):
        for value in (1, 8, 32):
            owner = self.make_owner()
            owner.runtime_threads_var.get.return_value = str(value)
            Monitor._apply_runtime_threads(owner)
            owner._write_runtime_threads.assert_called_once_with(value)

    # 越界或非数字：弹出错误并**还原输入框**，不能静默改成别的值 ——
    # 静默夹取会让界面显示一个用户没输入过的数字，看起来像"输入被吃了"。
    def test_out_of_range_reports_and_restores(self):
        for raw in ("33", "-1", "abc", ""):
            owner = self.make_owner()
            owner.runtime_active_threads = 32
            owner.runtime_threads_var.get.return_value = raw
            with patch.object(ui.messagebox, "showerror") as error:
                Monitor._apply_runtime_threads(owner)
            error.assert_called_once()
            owner._write_runtime_threads.assert_not_called()
            owner.runtime_threads_var.set.assert_called_once_with("32")

    # 未运行时（或还没拿到线程上限时）不该写控制文件，也不该弹窗。
    def test_noop_when_not_running(self):
        for running, limit in ((False, 32), (True, 0)):
            owner = self.make_owner(limit=limit)
            owner.running = running
            with patch.object(ui.messagebox, "showerror") as error:
                Monitor._apply_runtime_threads(owner)
            owner._write_runtime_threads.assert_not_called()
            error.assert_not_called()


class CloseTests(unittest.TestCase):
    # 关闭窗口必须结束 mainloop，否则进程会留在任务管理器里（"关了 UI 还残留
    # PBBMonitor"）。实测：只调 destroy() 时 mainloop() 不返回；必须 quit()。
    # 这条断言直接锁住这个关键调用，防止以后被"简化"掉。
    def test_close_quits_mainloop(self):
        owner = SimpleNamespace(
            _closing=False, _after_ids=set(),
            _cancel_scheduled_after=Mock(), _stop=Mock(),
            _advance_config_start=Mock(),
            proc=None, quit=Mock(), destroy=Mock(),
        )
        Monitor._close(owner)
        owner.quit.assert_called_once()
        owner.destroy.assert_called_once()
        self.assertTrue(owner._closing)

    # 关闭时若还有引擎在跑，必须立刻结束它：_stop() 走的是 terminate() + after(1500)
    # 兜底，而窗口随即销毁，那个兜底回调不会执行。
    def test_close_kills_running_engine(self):
        proc = Mock()
        proc.poll.return_value = None
        owner = SimpleNamespace(
            _closing=False, _after_ids=set(),
            _cancel_scheduled_after=Mock(), _stop=Mock(),
            _advance_config_start=Mock(),
            proc=proc, quit=Mock(), destroy=Mock(),
        )
        Monitor._close(owner)
        proc.kill.assert_called_once()

    # _schedule_after 必须记账，关闭时才能取消；关闭过程中不再安排新回调。
    def test_schedule_after_is_tracked_and_stops_when_closing(self):
        owner = SimpleNamespace(_closing=False, _after_ids=set(),
                                after=Mock(return_value="after#1"))
        after_id = Monitor._schedule_after(owner, 100, lambda: None)
        self.assertEqual(after_id, "after#1")
        self.assertEqual(owner._after_ids, {"after#1"})

        owner._closing = True
        self.assertEqual(Monitor._schedule_after(owner, 100, lambda: None), "")
        self.assertEqual(owner.after.call_count, 1)

    # 取消时要清空记账表，避免 id 无限累积（每次都重排 _drain_events）。
    def test_cancel_scheduled_after_clears_tracking(self):
        owner = SimpleNamespace(_closing=False, _after_ids={"a", "b"}, after_cancel=Mock())
        Monitor._cancel_scheduled_after(owner)
        self.assertEqual(owner.after_cancel.call_count, 2)
        self.assertEqual(owner._after_ids, set())


class BlankConfigTests(unittest.TestCase):
    # 「编辑新配置」从一份空白配置起步，所以这份配置必须**先能通过校验**，
    # 否则用户一打开编辑器就被报错拦住，根本没法从空白开始填。
    def test_blank_config_passes_validation(self):
        from pbb.main import _validate_config
        cfg = ui.blank_config()
        _validate_config(cfg)                      # 不抛异常即通过
        # 必填项不能为空，否则校验必然失败
        self.assertTrue(str(cfg["team_name"]).strip())
        self.assertIn("character_set", cfg)
        self.assertTrue(cfg["enumeration"]["ranges"])

    # 空白配置要能存成 YAML 再读回并仍然合法 —— 保存路径不能产生坏文件，
    # 否则「保存后才加入表格」加入的就是一份读不回来的配置。
    def test_blank_config_round_trips(self):
        import tempfile
        from pathlib import Path
        cfg = ui.blank_config()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blank.yaml"
            ui.save_config(path, cfg)
            back = ui.load_config(str(path))
        self.assertEqual(back["team_name"], cfg["team_name"])
        self.assertEqual(back["enumeration"]["mode"], cfg["enumeration"]["mode"])


class ConfigSelectorReuseTests(unittest.TestCase):
    # 选择器已打开时再次触发「选择」，必须复用原窗口而不是再弹一个。
    # 旧实现没有 grab_set（非模态），主窗口仍可点击，于是能叠出多个选择器。
    def test_existing_selector_is_reused_not_recreated(self):
        existing = SimpleNamespace(winfo_exists=lambda: True, lift=Mock(), focus_force=Mock())
        owner = SimpleNamespace(_config_selector=existing)
        created = []
        with patch.object(ui, "ConfigSelector", side_effect=lambda master: created.append(master)):
            Monitor._open_config_selector(owner)
        self.assertEqual(created, [], "已有选择器时不应新建窗口")
        existing.lift.assert_called_once()
        existing.focus_force.assert_called_once()

    # 打开期间要禁用主窗口的「选择」按钮（键盘也能触发，单靠 grab 不够），
    # 关闭后必须恢复，否则用户再也点不开选择器。
    def test_select_button_disabled_while_open_and_restored(self):
        button = Mock()
        state = {"selector": None}

        class FakeSelector:
            def __init__(self, master):
                pass

        owner = SimpleNamespace(
            _config_selector=None, config_select_button=button,
            wait_window=Mock(),
        )
        with patch.object(ui, "ConfigSelector", FakeSelector):
            Monitor._open_config_selector(owner)
        # 打开时禁用、结束后恢复（state 是关键字参数）
        states = [call.kwargs["state"] for call in button.configure.call_args_list
                  if "state" in call.kwargs]
        self.assertEqual(states, ["disabled", "normal"])
        self.assertIsNone(owner._config_selector, "结束后应清空引用")


if __name__ == "__main__":
    unittest.main()

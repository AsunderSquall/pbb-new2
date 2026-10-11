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


if __name__ == "__main__":
    unittest.main()

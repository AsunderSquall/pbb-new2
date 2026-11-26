"""实际调用UI开始入口，覆盖结果文件解析和Resume分支，不启动后台引擎。"""
from pathlib import Path
import sys
import unittest
from queue import Queue
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from pbb import build_settings
from pbb import ui


class StartEntryTests(unittest.TestCase):
    def test_engine_crash_is_reported_separately_from_user_stop(self):
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                events = Queue()
                owner = Mock(engine_bin='engine.exe', rebuild_requested=False,
                             pbb_core_initialized=True, stop_requested=False, events=events)
                process = Mock(stderr=['[engine] ERROR: 测试故障\n'])

                def exit_engine():
                    owner.stop_requested = stopped
                    return -8

                process.wait.side_effect = exit_engine
                with patch.object(ui.engine_runner, '_pbb_core'), \
                     patch.object(ui.engine_runner, '_build_charset'), \
                     patch.object(ui.engine_runner, '_build_params', return_value=''), \
                     patch.object(ui.os, 'makedirs'), patch.object(ui.subprocess, 'Popen', return_value=process):
                    ui.Monitor._launch(owner, {'character_set': {}})
                events.get_nowait()  # 启动状态
                events.get_nowait()  # 引擎日志
                kind, detail = events.get_nowait()
                if stopped:
                    self.assertEqual((kind, detail), ('finished', '已停止'))
                else:
                    self.assertEqual(kind, 'error')
                    self.assertIn('-8', detail)
                    self.assertIn('测试故障', detail)

    def test_long_random_warns_and_still_starts_but_overlong_name_does_not(self):
        for length in (65, 257):
            with self.subTest(length=length):
                cfg = ui.blank_config()
                cfg['enumeration']['variable_length'] = length
                owner = Mock(running=False)
                owner.resume_var.get.return_value = False
                owner.rebuild_var.get.return_value = False
                owner.threads_var.get.return_value = '2'
                owner.range_var.get.return_value = '100'
                owner._families_from_task.return_value = ['qp']
                owner.expanded_families = set()
                with patch.object(ui, 'load_config', return_value=cfg), \
                     patch.object(ui, '_resolve_result_file', return_value='test.txt'), \
                     patch.object(ui.messagebox, 'showwarning') as warning, \
                     patch.object(ui.messagebox, 'showerror') as error, \
                     patch.object(ui.threading, 'Thread') as worker:
                    ui.Monitor._start(owner)
                self.assertEqual(owner.running, length == 65)
                if length == 65:
                    warning.assert_called_once()
                    error.assert_not_called()
                    worker.return_value.start.assert_called_once()
                else:
                    warning.assert_not_called()
                    error.assert_called_once()
                    worker.assert_not_called()

    def test_normal_and_resume_start_resolve_result_before_launch(self):
        for resume in (False,True):
            with self.subTest(resume=resume):
                cfg=ui.load_config(str(Path(build_settings.config_root(ROOT))/"config_football.yaml"))
                cfg['enumeration']['mode'] = 1
                owner=Mock(running=False)
                owner.resume_var.get.return_value=resume
                owner.rebuild_var.get.return_value=False
                owner._config_path.return_value='config_football.yaml'
                owner._selected_result_file.return_value='selected.txt'
                owner.threads_var.get.return_value='2'
                owner.range_var.get.return_value='100'
                owner._families_from_task.return_value=['qp']
                owner.expanded_families=set()
                with patch.object(ui,'load_config',return_value=cfg), \
                     patch.object(ui,'_resolve_result_file',wraps=ui._resolve_result_file) as resolve, \
                     patch('pbb.main._try_resume',return_value=None) as try_resume, \
                     patch.object(ui.messagebox,'showwarning'), \
                     patch.object(ui.messagebox,'showerror') as error, \
                     patch.object(ui.threading,'Thread') as worker:
                    ui.Monitor._start(owner)
                    error.assert_not_called()
                    resolve.assert_called_once_with('selected.txt')
                    self.assertTrue(owner.running)
                    self.assertEqual(owner._run_result_file,'selected.txt')
                    worker.return_value.start.assert_called_once()
                    task=worker.call_args.kwargs['args'][0]
                    self.assertEqual(task['n_threads'],2)
                    self.assertEqual(task['range_R'],100)
                    if resume:try_resume.assert_called_once_with(cfg,'selected.txt',out_dir=ui.RESULT_ROOT)
                    else:try_resume.assert_not_called()


if __name__=='__main__':unittest.main()

"""实际调用UI开始入口，覆盖结果文件解析和Resume分支，不启动后台引擎。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from pbb import ui


class StartEntryTests(unittest.TestCase):
    def test_normal_and_resume_start_resolve_result_before_launch(self):
        for resume in (False,True):
            with self.subTest(resume=resume):
                cfg=ui.load_config(str(ROOT/'config/config_football.yaml'))
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
                    if resume:try_resume.assert_called_once_with(cfg,'selected.txt',out_dir=ui.ROOT)
                    else:try_resume.assert_not_called()


if __name__=='__main__':unittest.main()

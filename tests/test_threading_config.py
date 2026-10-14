import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from pbb import threading_config as threads
from pbb import ui
from pbb.main import _build_task_config


class ThreadDefaultsTests(unittest.TestCase):
    def test_explicit_count_is_preserved_without_detection(self):
        with patch.object(threads,'available_threads',side_effect=AssertionError('must not detect')):
            self.assertEqual(threads.resolve_threads(40),40)
            self.assertEqual(threads.resolve_threads(3),3)

    def test_auto_uses_process_available_count(self):
        with patch.object(threads.os,'process_cpu_count',return_value=12,create=True),patch.object(threads.os,'cpu_count',return_value=64):
            self.assertEqual(threads.resolve_threads(-1),12)

    def test_auto_respects_affinity(self):
        with patch.object(threads.os,'process_cpu_count',return_value=None,create=True),patch.object(threads.os,'sched_getaffinity',return_value={2,3,4},create=True):
            self.assertEqual(threads.available_threads(),3)

    def test_windows_and_system_fallback(self):
        with patch.object(threads.os,'process_cpu_count',return_value=None,create=True),patch.object(threads.os,'sched_getaffinity',side_effect=OSError,create=True),patch.object(threads,'_windows_affinity_count',return_value=8),patch.object(threads.os,'cpu_count',return_value=32):
            self.assertEqual(threads.available_threads(),8)
        with patch.object(threads.os,'process_cpu_count',return_value=None,create=True),patch.object(threads.os,'sched_getaffinity',side_effect=OSError,create=True),patch.object(threads,'_windows_affinity_count',return_value=None),patch.object(threads.os,'cpu_count',return_value=None):
            self.assertEqual(threads.available_threads(),4)

    def test_shipped_configs_use_auto_and_manual_override_survives(self):
        root=Path(__file__).resolve().parent.parent
        for name in ('config_football.yaml','config_flex.example.yaml','config.example.yaml','config.example.json','config.example.toml'):
            cfg=ui.load_config(str(root/'config'/name))
            self.assertEqual(_build_task_config(cfg)['n_threads'],-1)
            cfg['threads']['worker_threads']=7
            self.assertEqual(threads.resolve_threads(_build_task_config(cfg)['n_threads']),7)

    def test_engine_params_resolve_auto_but_keep_manual_value(self):
        from pbb import engine
        task=_build_task_config(ui.blank_config())
        task['n_threads']=-1
        with patch.object(threads,'available_threads',return_value=6):
            self.assertIn('n_threads=6\n',engine._build_params(task,'3031','threads.txt'))
        task['n_threads']=7
        with patch.object(threads,'available_threads',side_effect=AssertionError('must not detect')):
            self.assertIn('n_threads=7\n',engine._build_params(task,'3031','threads.txt'))


if __name__=='__main__':unittest.main()

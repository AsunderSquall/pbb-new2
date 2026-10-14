"""Fault injection uses temporary configs/processes, never a system shutdown."""
import copy
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pbb import ui, flex, engine
from pbb.main import _build_task_config


def fixture(directory):
    cfg = ui.blank_config()
    cfg['character_sets'] = {'digits': {'single_char_length': 1, 'types': [1]}}
    cfg['format'] = [{'charset': 'digits'}] * 6
    cfg['enumeration'].update(mode=1, ranges=[{'start': 0, 'end': 100000}])
    path = Path(directory) / 'config.json'
    ui.save_config(path, cfg)
    task = _build_task_config(cfg)
    template, slots = flex.build_layout(task, engine._build_charset)
    checkpoint = Path(directory) / 'out' / 'test.resume'
    checkpoint.parent.mkdir()
    header = flex.resume_id(task, template, slots)
    checkpoint.write_text(header + '\n0 12345 100000\n', encoding='ascii')
    owner = SimpleNamespace(running=True, _run_origin=cfg, _run_config_path=str(path),
                            _run_result_file='test', _run_processed=0, _run_start_updated=False,
                            _run_last_saved_config=copy.deepcopy(cfg), _next_progress_save=0,
                            auto_start_var=SimpleNamespace(get=lambda: True), status_var=Mock())
    owner._advance_config_start = lambda **kw: ui.Monitor._advance_config_start(owner, **kw)
    return owner, path, checkpoint, header


class ProgressSafetyTests(unittest.TestCase):
    def test_periodic_contiguous_frontier_and_final_save(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ui, 'ROOT', directory):
            owner, path, checkpoint, header = fixture(directory)
            ui.Monitor._periodic_progress_save(owner)
            self.assertEqual(ui.load_config(str(path))['enumeration']['prefix_ranges'][0]['start'], 12345)
            self.assertFalse(owner._run_start_updated)
            checkpoint.write_text(header + '\n0 23456 100000\n', encoding='ascii')
            # A cancelled shutdown must not prevent a later final save.
            ui.Monitor._save_session_progress(owner)
            self.assertFalse(owner._run_start_updated)
            checkpoint.write_text(header + '\n0 34567 100000\n', encoding='ascii')
            owner._advance_config_start()
            self.assertEqual(ui.load_config(str(path))['enumeration']['prefix_ranges'][0]['start'], 34567)
            self.assertTrue(owner._run_start_updated)

    def test_disabled_legacy_and_random_do_not_periodically_advance(self):
        for kind in ('disabled', 'legacy', 'random'):
            with tempfile.TemporaryDirectory() as directory, patch.object(ui, 'ROOT', directory):
                owner, path, _, _ = fixture(directory)
                before = path.read_bytes()
                if kind == 'disabled': owner.auto_start_var.get = lambda: False
                if kind == 'legacy': owner._run_origin.pop('format')
                if kind == 'random': owner._run_origin['enumeration']['mode'] = 2
                ui.Monitor._periodic_progress_save(owner)
                self.assertEqual(path.read_bytes(), before)

    def test_failed_save_retries_and_manual_edits_survive(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ui, 'ROOT', directory):
            owner, path, _, _ = fixture(directory)
            with patch.object(ui, 'save_config', side_effect=OSError('injected disk error')):
                owner._advance_config_start()
            self.assertFalse(owner._run_start_updated)
            owner._advance_config_start(final=False)
            edited = ui.load_config(str(path)); edited['team_name'] = 'manual edit'
            ui.save_config(path, edited)
            owner._advance_config_start()
            self.assertEqual(ui.load_config(str(path))['team_name'], 'manual edit')
            self.assertFalse(owner._run_start_updated)

    def run_child_until_ready(self, source, directory):
        child = subprocess.Popen([sys.executable, '-u', '-c', source, directory],
                                 cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        child.kill()
        child.wait(timeout=10)
        child.stdout.close(); child.stderr.close()

    def test_forced_process_death_after_periodic_save(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_child_until_ready('''
import sys,time
sys.path.insert(0,'tests')
from test_progress_safety import fixture,ui
ui.ROOT=sys.argv[1]
owner,*_=fixture(sys.argv[1])
ui.Monitor._periodic_progress_save(owner)
print('ready',flush=True)
time.sleep(60)
''', directory)
            saved = ui.load_config(str(Path(directory)/'config.json'))
            self.assertEqual(saved['enumeration']['prefix_ranges'][0]['start'], 12345)

    def test_forced_death_before_atomic_replace_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            ui.save_config(path, {'start': 123})
            self.run_child_until_ready('''
import sys,time
from pathlib import Path
from pbb import ui
def interrupted_replace(*args):
    print('ready',flush=True)
    time.sleep(60)
ui.os.replace=interrupted_replace
ui.save_config(Path(sys.argv[1])/'config.json',{'start':999})
''', directory)
            self.assertEqual(json.loads(path.read_text(encoding='utf8')), {'start': 123})

    @unittest.skipUnless(os.name == 'nt', 'Windows messages')
    def test_real_windows_session_messages_on_test_window(self):
        from pbb.session_guard import SessionGuard
        from ctypes import wintypes
        with tempfile.TemporaryDirectory() as directory, patch.object(ui, 'ROOT', directory):
            owner, path, checkpoint, header = fixture(directory)
            window = ui.tk.Tk(); window.withdraw()
            guard = SessionGuard(window, lambda: ui.Monitor._save_session_progress(owner))
            try:
                api = ctypes.WinDLL('user32', use_last_error=True)
                api.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                api.SendMessageW.restype = ctypes.c_ssize_t
                self.assertEqual(api.SendMessageW(guard.hwnd, 0x11, 0, 0), 1)
                self.assertEqual(ui.load_config(str(path))['enumeration']['prefix_ranges'][0]['start'], 12345)
                checkpoint.write_text(header + '\n0 23456 100000\n', encoding='ascii')
                api.SendMessageW(guard.hwnd, 0x16, 0, 0)  # cancelled logout: do not save
                self.assertEqual(ui.load_config(str(path))['enumeration']['prefix_ranges'][0]['start'], 12345)
                api.SendMessageW(guard.hwnd, 0x16, 1, 0)  # only this test window receives it
                self.assertEqual(ui.load_config(str(path))['enumeration']['prefix_ranges'][0]['start'], 23456)
                self.assertFalse(owner._run_start_updated)
            finally:
                guard.close(); window.destroy()


if __name__ == '__main__': unittest.main()

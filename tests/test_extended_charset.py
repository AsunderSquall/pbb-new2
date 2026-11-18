"""验证扩展汉字表的 UTF-8 边界、混合字符集和真实引擎输出。"""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pbb import engine, paths, ui
from pbb.main import _build_task_config


CUSTOM_EMOJI = '👋🤚🖐🖖🫱🫲🫳🫴👌🤌🤏🤞🫰🤟🤘🤙👈👉👆🖕👇🫵👍👎👊🤛🤜👏🙌🫶👐🤲🤝🙏💪'


class ExtendedCharsetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.core = engine._pbb_core()
        except ImportError as exc:
            raise unittest.SkipTest(f'原生字符集模块尚未构建：{exc}')

    def test_tables_contain_only_complete_characters(self):
        for width, count, first, last, raw in (
            (3, 6400, 13312, 19711, self.core.get_extended_hanzi_3_bytes()),
            (4, 64287, 131072, 191456, self.core.get_extended_hanzi_4_bytes()),
        ):
            with self.subTest(width=width):
                self.assertEqual(len(raw), width * count)
                self.assertNotIn(b'\0', raw)
                text = raw.decode('utf8')
                self.assertEqual(len(text), count)
                self.assertEqual(text[0], chr(first))
                self.assertEqual(text[-1], chr(last))
                self.assertTrue(all(len(char.encode('utf8')) == width for char in text))
                if width == 3:
                    self.assertEqual(text, ''.join(map(chr, range(first, last + 1))))

    def test_repeated_initialization_keeps_table_contents(self):
        before = (self.core.get_extended_hanzi_3_bytes(),
                  self.core.get_extended_hanzi_4_bytes())
        for _ in range(3):
            self.core.init_exhanzi()
        self.assertEqual(before, (self.core.get_extended_hanzi_3_bytes(),
                                  self.core.get_extended_hanzi_4_bytes()))

    def test_custom_emoji_and_extended_hanzi_have_correct_boundaries(self):
        cs = {'single_char_length': 4, 'types': [2, 1],
              'custom_values': CUSTOM_EMOJI}
        raw = bytes.fromhex(engine._build_charset(cs))
        self.assertEqual(len(CUSTOM_EMOJI), 35)
        self.assertEqual(len(raw), (35 + 64287) * 4)
        self.assertNotIn(b'\0', raw)
        self.assertEqual(raw.decode('utf8'),
                         CUSTOM_EMOJI + self.core.get_extended_hanzi_4_bytes().decode('utf8'))
        self.assertEqual(ui.Monitor._charset_size(cs), 64322)


class ExtendedCharsetEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            engine._pbb_core()
        except ImportError as exc:
            raise unittest.SkipTest(f'原生字符集模块尚未构建：{exc}')
        if not all(paths.engine_binary(ui=target).exists() for target in (False, True)):
            raise unittest.SkipTest('请先运行 tools/rebuild_local.py')

    def test_sequential_output_crosses_custom_and_builtin_boundary(self):
        for width, custom, types in ((3, '春夏秋', [7, 6]),
                                     (4, CUSTOM_EMOJI, [2, 1])):
            for ui_target in (False, True):
                with self.subTest(width=width, ui_target=ui_target):
                    cfg = ui.blank_config()
                    cfg['team_name'] = 'test'
                    cfg['threads']['worker_threads'] = 1
                    cfg['character_set'] = {'single_char_length': width,
                        'types': types, 'custom_values': custom}
                    start = len(custom) - 2
                    end = 100000
                    cfg['enumeration'].update(mode=1, variable_length=2,
                        ranges=[{'start': start, 'end': end}])
                    cfg['collection'].update(score_list='qp', min_qp=0, collect_mode=0)
                    cfg['output'].update(output_xp=0, log_output=0, speed_output=0)
                    task = _build_task_config(cfg)
                    charset = engine._build_charset(task['character_set'])
                    candidates = bytes.fromhex(charset).decode('utf8')
                    params = engine._build_params(task, charset, 'result.txt')
                    with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
                        output = Path(directory) / 'out'
                        output.mkdir()
                        result = subprocess.run([str(paths.engine_binary(ui=ui_target))],
                            input=params, capture_output=True, text=True, encoding='utf8',
                            cwd=directory, timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        self.assertEqual(result.returncode, 0, result.stderr)
                        raw = (output / 'result.txt').read_bytes()
                    self.assertNotIn(b'\0', raw)
                    # 引擎自带属性初筛，只应输出候选窗口内的完整名字。
                    names = raw.decode('utf8').splitlines()
                    self.assertTrue(names)
                    for name in names:
                        self.assertTrue(name.endswith('@test'))
                        text = name[:-5]
                        self.assertEqual(len(text), 2)
                        rank = candidates.index(text[0]) * len(candidates) + candidates.index(text[1])
                        self.assertGreaterEqual(rank, start)
                        self.assertLess(rank, end)
                    self.assertTrue(any(char not in custom for name in names for char in name[:-5]))
                    summary = re.search(r'SUMMARY .*\bcount=(\d+)', result.stderr)
                    self.assertIsNotNone(summary, result.stderr)
                    self.assertEqual(int(summary.group(1)), end - start)


if __name__ == '__main__':
    unittest.main()

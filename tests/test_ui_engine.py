import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

# 测试文件位于 tests/ 子目录，被测代码在 pbb/ 包里；先把仓库根与 tools/ 加入 sys.path。
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / 'tools'))

from pbb import paths, ui
from pbb.engine import _build_params
from pbb.main import _build_task_config, _validate_config
from pbb.ui import Monitor


# 旧代码写死 Path("build/...")，依赖"测试从仓库根运行"这个巧合；改为走 paths 常量，
# 目录位置只有一个来源。
def _ui_engine() -> Path:
    return paths.engine_binary(ui=True)


def _std_engine() -> Path:
    return paths.engine_binary(ui=False)


class EngineLeaderboardTests(unittest.TestCase):
    # 守护 UI 排行榜的传输路径：output_xp=0 时结果文件只写名字和队伍、根本没有分数列，
    # 但排行榜仍必须通过 UI_TOP 内部协议送到界面（README：排行榜走内部协议，无需把
    # output_xp 改成 1）。同时约束：普通引擎不得带 UI_TOP（它不维护 Top5），
    # 两个引擎的 SUMMARY 统计必须一致，说明 UI 引擎只是在同样计算之上多输出排行榜。
    #
    # 本用例需要真实引擎二进制（build/pbb_ui_engine.exe 与 build/pbb_engine.exe）。
    # 它们不属于源码，干净检出后并不存在，所以这里选择**跳过**而不是断言失败：
    # "还没编译"是环境状态，不是回归。要跑本用例请先执行 python tools/rebuild_local.py。
    @classmethod
    def setUpClass(cls):
        missing = [str(p) for p in (_ui_engine(), _std_engine()) if not p.is_file()]
        if missing:
            raise unittest.SkipTest(
                "需要先构建引擎二进制（python tools/rebuild_local.py），缺少: " + ", ".join(missing))

    def test_empty_score_list_warns_and_writes_no_result_rows(self):
        task = {'team_name': 'test', 'n_threads': 1, 'scl': 1, 'mode': 1,
                'variable_len': 1, 'range_L': 0, 'range_R': 1, 'prefixes': ['0348428'],
                'suffixes': [''], 'ui_mode': True, 'output_xp': 0, 'output_log': 0,
                'output_speed': 0, 'min_qp': 1}
        for binary in (_std_engine(), _ui_engine()):
            for score_list, collect_mode in (('qp', 0), ('', 1), ([], 2)):
                with self.subTest(binary=binary.name, score_list=score_list, collect_mode=collect_mode):
                    params = _build_params(dict(task, score_list=score_list, collect_mode=collect_mode),
                                           b'6'.hex(), 'results.txt')
                    with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
                        out = Path(directory, 'out')
                        out.mkdir()
                        result = subprocess.run([str(binary)], input=params, capture_output=True,
                                                text=True, encoding='utf8', cwd=directory, timeout=30,
                                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual((out / 'blue.txt').exists(), collect_mode == 1)
                        rows = (out / 'results.txt').read_text(encoding='utf8')
                    if score_list:
                        self.assertEqual(rows, '03484286@test\n')
                    else:
                        self.assertEqual(rows, '')
                        self.assertIn('评分族为空，测号文件不会产生任何输出！', result.stderr)
                        self.assertIn('found=0 count=1', result.stderr)
                        self.assertNotIn('UI_TOP\t', result.stderr)

    def test_long_sequential_names_use_the_requested_rank_window(self):
        units = 'INTJ INTP ENTJ ENTP INFJ ENFJ INFP ENFP ISTJ ISFJ ESTJ ESFJ ESTP ISTP ISFP ESFP'.split()
        charset = ''.join(units).encode('ascii').hex()
        upper = (1 << 64) - 1
        for binary in (_std_engine(), _ui_engine()):
            for start, end in ((0, 100000), (upper - 100000, upper)):
                with self.subTest(binary=binary.name, start=start):
                    task = dict(team_name='爱', n_threads=1, scl=4, mode=1, variable_len=23,
                                range_L=start, range_R=end, prefixes=['+'], suffixes=['+'],
                                score_list='qp', output_xp=0, output_log=0, output_speed=0)
                    with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
                        out = Path(directory, 'out')
                        out.mkdir()
                        proc = subprocess.run([str(binary)], input=_build_params(task, charset),
                                              capture_output=True, text=True, encoding='utf8',
                                              cwd=directory, timeout=30)
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertIn(f'count={end - start} ', proc.stderr)
                        rows = (out / 'result.txt').read_text(encoding='utf8').splitlines()
                    self.assertTrue(rows, proc.stderr)
                    for row in rows:
                        name, team = row.rsplit('@', 1)
                        self.assertEqual(team, '爱')
                        self.assertEqual(len(name), 92)
                        rank = 0
                        for index in range(0, 92, 4):
                            rank = 16 * rank + units.index(name[index:index + 4])
                        self.assertTrue(start <= rank < end, (row, rank))

    def test_affixes_with_commas_survive_config_and_engine(self):
        prefixes = ['MerryChristmas,Mr.Lawrence', '+', ' 前, ']
        suffixes = [',尾 ', '+']
        cfg = ui.blank_config()
        cfg.update(team_name='A,B', prefixes=[{'name': name} for name in prefixes],
                   suffixes=[{'name': name} for name in suffixes],
                   collection={'score_list': 'qp'}, output={'output_xp': 0},
                   threads={'worker_threads': 1})
        cfg['enumeration'].update(mode=1, variable_length=5, ranges=[{'start': 0, 'end': 300000}])
        patterns = [
            re.compile(re.escape(prefix if prefix != '+' else '') + r'[0-9]{5}'
                       + re.escape((suffix if suffix != '+' else '') + '@A,B'))
            for prefix, suffix in zip(prefixes, [*suffixes, suffixes[0]])
        ]

        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            root = Path(directory)
            out = root / 'out'
            out.mkdir()
            for extension in ('yaml', 'toml', 'json'):
                config_path = root / f'commas.{extension}'
                ui.save_config(config_path, cfg)
                loaded = ui.load_config(str(config_path))
                _validate_config(loaded)
                task = _build_task_config(loaded)
                for binary in (_std_engine(), _ui_engine()):
                    with self.subTest(extension=extension, binary=binary.name):
                        filename = f'{extension}-{binary.stem}.txt'
                        params = _build_params(task, b'0123456789'.hex(), filename)
                        result = subprocess.run([str(binary)], input=params.encode('utf8'),
                                                capture_output=True, cwd=root, timeout=30,
                                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace'))
                        self.assertIn(b'count=300000', result.stderr)
                        seen = set()
                        for name in (out / filename).read_text(encoding='utf8').splitlines():
                            matches = [index for index, pattern in enumerate(patterns)
                                       if pattern.fullmatch(name)]
                            self.assertEqual(len(matches), 1, name)
                            seen.add(matches[0])
                        self.assertEqual(seen, {0, 1, 2})

    def test_scores_reach_ui_without_result_score_output(self):
        binary = _ui_engine()
        self.assertTrue(binary.is_file())
        # 夹具：把十个评分族的下限都抬到 9999999，使结果文件保持为空，
        # 从而证明“拿不到分数”并不妨碍排行榜数据送达界面。
        task = {"team_name": "test", "n_threads": 2, "scl": 1, "mode": 1,
                "variable_len": 6, "range_L": 0, "range_R": 1000000,
                "prefixes": [""], "suffixes": [""], "score_list": "qp,qd",
                "score_mode": "all", "ui_mode": True, "output_xp": 0,
                "output_log": 0, "output_speed": 0, "collect_mode": 0}
        for family in ("qp", "qd", "pp", "pd", "cqd", "cp", "fp", "wc", "fs", "pj"):
            task[f"min_{family}"] = 9999999
            task[f"ptt_{family}"] = 0
        params = _build_params(task, b"0123456789".hex(), "results.txt")
        self.assertIn("output_xp=0\n", params)
        with tempfile.TemporaryDirectory(dir="build") as directory:
            os.mkdir(os.path.join(directory, "out"))
            # 两个引擎用同一份 params、同一个空 out/ 目录各跑一次：结果文件都必须为空
            # （阈值不可达 + output_xp=0），差别只应体现在 stderr 的排行榜输出上。
            result = subprocess.run([str(binary)], input=params, capture_output=True,
                                    text=True, encoding="utf-8", cwd=directory, timeout=45,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(directory, "out", "results.txt").read_text(), "")
            regular_binary = _std_engine()
            regular = subprocess.run([str(regular_binary)], input=params, capture_output=True,
                                     text=True, encoding="utf-8", cwd=directory, timeout=45,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(regular.returncode, 0, regular.stderr)
            self.assertNotIn("UI_TOP", regular.stderr)
            self.assertNotIn(b"UI_TOP_BEGIN", regular_binary.read_bytes())
            summary = r"SUMMARY max_sum=\d+ max_xp=\d+ max_xd=\d+ found=\d+ count=\d+"
            self.assertEqual(re.search(summary, result.stderr).group(),
                             re.search(summary, regular.stderr).group())
        self.assertGreaterEqual(result.stderr.count("UI_TOP_BEGIN"), 2)
        # 不启动真窗口：直接把引擎输出的 UI_TOP 行交给界面侧的解析函数，
        # 验证“引擎发得出来 + 界面收得下”这条链路，而不只是看引擎自说自话。
        receiver = SimpleNamespace(families=["qp", "qd"], boards={}, boards_dirty=False)
        for line in result.stderr.splitlines():
            if line.startswith("UI_TOP"):
                Monitor._handle_line(receiver, line)
        self.assertTrue(receiver.boards_dirty)
        # 每个评分族各 5 条、分数为正、按分数降序、名字不重复，且都带上队伍后缀。
        for family in receiver.families:
            entries = receiver.boards[family]
            self.assertEqual(len(entries), 5)
            self.assertGreater(entries[0][0], 0)
            self.assertEqual(entries, sorted(entries, key=lambda entry: entry[0], reverse=True))
            self.assertEqual(len({name for _, name in entries}), 5)
            self.assertTrue(all(name.endswith("@test") for _, name in entries))


if __name__ == "__main__":
    unittest.main()

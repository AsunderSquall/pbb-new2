"""顺序模式（mode 1）的全局区间语义。

约定（用户口径，见 pbb/sequential.py）：
  * 全局 ranges[0] 是唯一的总控制器：start = 总量里已测过多少，end = 总量坐标上界，
    -1 = 所有前缀总量之和；
  * prefix_ranges 只是布局，程序永不修改它的 start/end；
  * resume 与「自动更新 start」都只改全局 start。

夹具按用户给的 test-shunxu2.toml 复刻数字：3 个前缀、前缀布局
[200e6,1000e6) / [400e6,-1) / [0,500e6]，字符集 10 个数字、variable_length=9
（单前缀完整总量 = 10^9），全局 start=1e9，正好落在第二个前缀的绝对位置 600e6。
"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from pbb import sequential, ui                      # noqa: E402
from pbb.main import (apply_range_overrides, _build_task_config,  # noqa: E402
                      _try_resume, _validate_config)


def sequential_config(**enumeration):
    """复刻 test-shunxu2.toml 的数字（不依赖仓库外的文件）。"""
    en = {
        'mode': 1,
        'variable_length': 9,
        'ranges': [{'start': 1000000000, 'end': -1}],
        'prefix_ranges': [{'count': 1, 'start': 200000000, 'end': 1000000000},
                          {'count': 1, 'start': 400000000, 'end': -1},
                          {'count': 1, 'start': 0, 'end': 500000000}],
    }
    en.update(enumeration)
    cfg = {
        'team_name': '新纪元',
        'prefixes': [{'name': '哦净净 *'}, {'name': '我是奶龙！ *'}, {'name': '涵仲 *'}],
        'suffixes': [{'name': '+'}],
        'character_set': {'single_char_length': 1, 'types': [1]},
        'enumeration': en,
        'collection': {'min_qp': 5400, 'score_list': 'qp'},
        'output': {'output_xp': 0, 'log_output': 1, 'speed_output': 1, 'result_file': 'r.txt'},
        'threads': {'worker_threads': -1},
    }
    _validate_config(cfg)
    return cfg


class GlobalRangeTests(unittest.TestCase):
    """全局坐标 ↔ (前缀, 前缀内绝对位置) 的换算。"""

    def test_full_total_is_charset_power(self):
        self.assertEqual(sequential.full_total(sequential_config()), 10 ** 9)

    def test_global_start_points_at_second_prefix_600m(self):
        cfg = sequential_config()
        self.assertEqual(sequential.prefix_layout(cfg),
                         [(200_000_000, 1_000_000_000),
                          (400_000_000, -1),
                          (0, 500_000_000)])
        self.assertEqual(sequential.totals(cfg), [800_000_000, 600_000_000, 500_000_000])
        self.assertEqual(sequential.locate(cfg, 1_000_000_000), (1, 600_000_000))

    def test_cli_range_overrides_are_applied_to_the_global_window(self):
        """`--range-start/--range-end` 必须落到全局 ranges 上，否则顺序模式整段重扫。"""
        cfg = apply_range_overrides(sequential_config(), 1_000_000_000, 1_000_002_000)
        task = _build_task_config(copy.deepcopy(cfg))
        left = task['prefix_range_L'].split(',')
        right = task['prefix_range_R'].split(',')
        total = sum(int(b) - int(a) for a, b in zip(left, right))
        self.assertEqual(total, 2000, f'窗口应只有 2000 个候选，实际 {total}')
        self.assertEqual(task['range_L'], 1_000_000_000)
        self.assertEqual(task['range_R'], 1_000_002_000)

    def test_global_end_minus_one_is_sum_of_all_prefixes(self):
        self.assertEqual(sequential.global_bounds(sequential_config()),
                         (1_000_000_000, 1_900_000_000))

    def test_effective_ranges_start_inside_second_prefix(self):
        cfg = sequential_config()
        self.assertEqual(sequential.effective_prefix_ranges(cfg),
                         [(0, 0), (600_000_000, 1_000_000_000), (0, 500_000_000)])

    def test_finite_global_end_truncates_later_prefixes(self):
        """全局 end 是总量坐标：落在第二个前缀里时，第三个前缀就不再跑。"""
        cfg = sequential_config(ranges=[{'start': 0, 'end': 1_000_000_000}])
        mapped = sequential.effective_prefix_ranges(cfg)
        self.assertEqual(mapped[0], (200_000_000, 1_000_000_000))
        self.assertEqual(mapped[1], (400_000_000, 600_000_000))
        self.assertEqual(mapped[2], (0, 0))

    def test_finished_global_start_maps_to_nothing(self):
        cfg = sequential_config(ranges=[{'start': 1_900_000_000, 'end': -1}])
        self.assertTrue(all(low == high for low, high in sequential.effective_prefix_ranges(cfg)))

    def test_to_global_is_the_inverse_of_locate(self):
        cfg = sequential_config()
        for prefix_index, value, expected in [(0, 300_000_000, 100_000_000),
                                              (1, 600_000_000, 1_000_000_000),
                                              (2, 100_000_000, 1_500_000_000)]:
            with self.subTest(prefix=prefix_index):
                self.assertEqual(sequential.to_global(cfg, prefix_index, value), expected)
                self.assertEqual(sequential.locate(cfg, expected), (prefix_index, value))


class TaskMappingTests(unittest.TestCase):
    """任务参数用映射结果，但配置一个字都不能变。"""

    def test_build_task_maps_global_range_without_touching_config(self):
        cfg = sequential_config()
        before = copy.deepcopy(cfg)
        task = _build_task_config(cfg)
        self.assertEqual(task['prefix_range_L'], '0,600000000,0')
        self.assertEqual(task['prefix_range_R'], '0,1000000000,500000000')
        self.assertEqual(cfg, before, '构任务不能改配置（prefix_ranges 必须原样）')

    def test_random_modes_still_use_the_global_range(self):
        cfg = sequential_config(mode=2)
        cfg['enumeration'].pop('prefix_ranges', None)
        task = _build_task_config(cfg)
        self.assertEqual((task['range_L'], task['range_R']), (1_000_000_000, -1))
        self.assertNotIn('prefix_range_L', task)


class ResumeWritesGlobalStartTests(unittest.TestCase):
    """resume 只改全局 start：prefix_ranges 原样，start 折算成交叉点。"""

    @staticmethod
    def simple_config():
        """小到能算名字的夹具：3 个单字母前缀，字符集 10 数字，vlen=3（总量 1000）。"""
        cfg = {
            'team_name': 't',
            'prefixes': [{'name': 'A'}, {'name': 'B'}, {'name': 'C'}],
            'suffixes': [{'name': '+'}],
            'character_set': {'single_char_length': 1, 'types': [1]},
            'enumeration': {'mode': 1, 'variable_length': 3,
                            'ranges': [{'start': 0, 'end': -1}],
                            'prefix_ranges': [{'count': 1, 'start': 200, 'end': 1000},
                                              {'count': 1, 'start': 400, 'end': -1},
                                              {'count': 1, 'start': 0, 'end': 500}]},
            'collection': {'min_qp': 5400, 'score_list': 'qp'},
            'output': {'output_xp': 0, 'log_output': 1, 'speed_output': 1, 'result_file': 'r.txt'},
            'threads': {'worker_threads': -1},
        }
        _validate_config(cfg)
        return cfg

    def test_legacy_resume_sets_global_start(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / 'out'
            out.mkdir()
            cfg = self.simple_config()
            # 前缀[1] 的 600 → 全局 800 + (600-400) = 1000；续测从 601 开始 → 1001
            (out / 'r').write_text('B600@t\n', encoding='utf8')
            layout_before = copy.deepcopy(cfg['enumeration']['prefix_ranges'])
            resumed, desc = _try_resume(cfg, 'r', out_dir=temp)
            self.assertIsNotNone(resumed, desc)
            self.assertEqual(resumed['enumeration']['ranges'][0]['start'], 1001)
            self.assertEqual(resumed['enumeration']['prefix_ranges'], layout_before)
            # 配置里的全局 end 不变
            self.assertEqual(resumed['enumeration']['ranges'][0]['end'], -1)
            # 映射回前缀：从第二个前缀的 601 开始，后面的前缀整段跑
            self.assertEqual(sequential.effective_prefix_ranges(resumed),
                             [(0, 0), (601, 1000), (0, 500)])


class AdvanceStartTests(unittest.TestCase):
    """「自动更新 start」只推进全局 start，绝不碰 prefix_ranges。"""

    def stub(self, cfg, path, processed, effective_start):
        owner = types.SimpleNamespace(
            _run_start_updated=False, _run_origin=cfg, _run_config_path=str(path),
            _run_result_file=None, _run_processed=processed,
            _run_effective_start=effective_start, _run_last_saved_config=copy.deepcopy(cfg),
            auto_start_var=types.SimpleNamespace(get=lambda: True),
            status_var=Mock())
        owner._advance_config_start = lambda **kw: ui.Monitor._advance_config_start(owner, **kw)
        return owner

    def test_only_global_start_moves(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'c.yaml'
            cfg = sequential_config(ranges=[{'start': 1_000_000_000, 'end': -1}])
            ui.save_config(path, cfg)
            owner = self.stub(cfg, path, processed=123_456, effective_start=1_000_000_000)
            owner._advance_config_start()
            saved = ui.load_config(str(path))
            self.assertEqual(saved['enumeration']['ranges'][0]['start'], 1_000_123_456)
            self.assertEqual(saved['enumeration']['prefix_ranges'],
                             cfg['enumeration']['prefix_ranges'])
            self.assertEqual(saved['enumeration']['ranges'][0]['end'], -1)

    def test_advance_is_clamped_by_global_end(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'c.yaml'
            cfg = sequential_config(ranges=[{'start': 1_000_000_000, 'end': 1_500_000_000}])
            ui.save_config(path, cfg)
            owner = self.stub(cfg, path, processed=900_000_000, effective_start=1_000_000_000)
            owner._advance_config_start()
            saved = ui.load_config(str(path))
            self.assertEqual(saved['enumeration']['ranges'][0]['start'], 1_500_000_000)


if __name__ == '__main__':
    unittest.main()

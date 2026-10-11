import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# 测试文件位于 tests/ 子目录，被测代码在 pbb/ 包里、打包工具在 tools/ 下，
# 因此先把仓库根与 tools/ 加入 sys.path，直接运行或用 unittest discover 都能导入。
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / 'tools'))

from pbb import _build as build
from pbb import build_settings as settings
from pbb import autotune
import package_release  # tools/ 下的打包工具


# 测试把临时目录建在 build/ 下，所以要保证它存在（干净检出后无需先构建）。
from pbb import paths as _paths
_paths.ensure_build_dir()

class BuildConfigTests(unittest.TestCase):
    # 构造一份“可信”的 build_config.json：源码哈希与磁盘上的 engine_main.cpp 一致，
    # 且 tested/status/machine/compiler 都匹配，好让 settings.load() 返回 active=True。
    # 各用例再按需改动 options 或文件内容，验证绑定关系何时失效。
    def fixture(self,root):
        (root/'engine_main.cpp').write_bytes(b'source')
        profile=dict(settings.fresh(),tested=True,status='complete',machine=['host'],compiler='g++',
            source_hashes={'engine_main.cpp':hashlib.sha256(b'source').hexdigest()},
            options=dict(settings.DEFAULTS,attr=False,filter=2,unroll=False))
        settings.write(root/'build_config.json',profile)
        return profile

    # fresh() 是发布包模板与回退基准：必须 tested=False、status='none'、options 为默认值。
    # 防的是把本机测速结论（或残留的开关）当成默认值发给别人的机器。
    def test_fresh_is_unverified_with_defaults(self):
        profile=settings.fresh()
        self.assertFalse(profile['tested']);self.assertEqual(profile['status'],'none')
        self.assertEqual(profile['options'],settings.DEFAULTS)

    # 选择绑定三样东西：机器指纹、编译器路径、源码哈希。换机器、换编译器、或改源码
    # 任一发生，load() 的 active 都必须变 False（即回到默认参数重编）。
    def test_host_source_and_compiler_bind_selection(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            root=Path(directory);self.fixture(root)
            self.assertTrue(settings.load(root,['host'],'g++')[1])
            self.assertFalse(settings.load(root,['other'],'g++')[1])
            self.assertFalse(settings.load(root,['host'],'icpx')[1])
            (root/'engine_main.cpp').write_bytes(b'changed')
            self.assertFalse(settings.load(root,['host'],'g++')[1])

    # 普通与 UI 两个目标都要读同一份 build_config.json 的选项（否则 UI 引擎会悄悄用
    # 默认参数），但 PBB_CXXFLAGS 覆盖优先级最高：覆盖时只剩用户 flags + UI 目标宏。
    # 夹具 mock 掉编译器探测/执行/清单记录，只断言实际传入的 flags。
    def test_rebuild_reads_profile_for_both_targets_and_override_wins(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            root=Path(directory);self.fixture(root)
            for ui in (False,True):
                with patch.object(build,'BASE_DIR',str(root)),patch.object(build,'_machine_identity',return_value=['host']), \
                     patch.dict(os.environ,{'PBB_BUILD_MODE':'native','PBB_CXXFLAGS':'','PBB_SRC_DIR':''}), \
                     patch.object(build,'_find_compilers',return_value=[('g++',['-O3'],'native',False)]), \
                     patch.object(build,'_run_compiler',return_value=SimpleNamespace(returncode=0)),patch.object(build,'_record_binary') as record:
                    build._compile_engine(output_path=str(root/'engine.exe'),ui_engine=ui)
                    flags=record.call_args.args[2]
                    self.assertIn('-DPBB_ATTR_FILTER_MODE=2',flags)
                    self.assertIn('-DPBB_ATTR_MINMAX=0',flags)
                    self.assertIn('-fno-unroll-loops',flags)
                    self.assertIn('-DPBB_UI_ENGINE='+str(int(ui)),flags)
                    with patch.dict(os.environ,{'PBB_CXXFLAGS':'-O2'}):
                        build._compile_engine(output_path=str(root/'engine.exe'),ui_engine=ui)
                        self.assertEqual(record.call_args.args[2],['-O2','-DPBB_UI_ENGINE='+str(int(ui))])

    # 二进制清单里记着 build_config 的选项签名：选项一变（这里把 attr 改成 True），
    # 已编好的引擎就判定为过期，必须重编——这是“改选择后要 Rebuild 才生效”的机制。
    def test_selection_change_invalidates_engine_manifest(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory, \
             patch.dict(os.environ,{'PBB_BUILD_MODE':'native','PBB_CXXFLAGS':'','PBB_SRC_DIR':''}), \
             patch.object(build,'_machine_identity',return_value=['host']):
            root=Path(directory);profile=self.fixture(root);binary=root/'engine.exe';binary.write_bytes(b'engine')
            with patch.object(build,'BASE_DIR',str(root)):
                build._record_binary(str(binary),'g++',[],engine_target='ui')
                self.assertTrue(build._binary_is_current(str(binary),engine_target='ui'))
                profile['options']['attr']=True;settings.write(root/'build_config.json',profile)
                self.assertFalse(build._binary_is_current(str(binary),engine_target='ui'))

    # portable 构建完全不采用本机选择（签名为 None），保证发布引擎与本机测速结论无关。
    def test_portable_ignores_local_profile(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            root=Path(directory);self.fixture(root)
            with patch.object(build,'BASE_DIR',str(root)),patch.dict(os.environ,{'PBB_BUILD_MODE':'portable'}):
                self.assertIsNone(build._build_config_signature('g++'))

    # 发布 payload 必须无条件把 build_config.json 换成 fresh()：即便本机这份是
    # tested=true 的结论，也不能随包发出去（对应 REPO.md §4.2 的回归）。
    def test_packaged_config_is_always_fresh(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            root=Path(directory);self.fixture(root)
            shipped=json.loads(package_release.source_payload(root/'build_config.json'))
            self.assertEqual(shipped,settings.fresh())
            self.assertTrue(json.loads((root/'build_config.json').read_text())['tested'])

    # 候选量公式必须和 AUTOTUNE.md 一致：T/day × 10^12 ÷ 86400 × 秒数，向下取整到
    # 一百万的整数倍、下限一百万。取整若变成四舍五入或去掉了对齐，任务量就会漂移。
    def test_candidate_volume_uses_tday_minutes_and_integer_chunks(self):
        count=autotune.candidate_count(1.5,60,2)
        self.assertEqual(count,520000000)
        self.assertEqual(count%1000000,0)
        self.assertEqual(autotune.candidate_count(1.5,120,2),1041000000)

    # 编译时间单独计入 compile_seconds，不占用测试预算（left()）：预算只扣真正的
    # 引擎运行时间。编译慢不应该让计划中的测试轮次被砍掉。
    # 夹具直接 __new__ 出一个 Tuner 并为 time.monotonic 排出 0/5/6/9 的时间序列。
    def test_compilation_does_not_consume_test_budget(self):
        tuner=autotune.Tuner.__new__(autotune.Tuner)
        tuner.compiler='g++';tuner.test_budget=100;tuner.test_used=1;tuner.compile_seconds=0
        with patch.object(autotune.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
             patch.object(autotune.time,'monotonic',side_effect=[0,5,6,9]):
            tuner.process(['g++','source.cpp'])
            self.assertEqual(tuner.left(),99)
            tuner.process(['engine.exe'])
        self.assertEqual(tuner.left(),96)
        self.assertEqual(tuner.compile_seconds,5)

    # 发布布局里源码在 source/（内含 pbb 包）、冻结监控器在 PBBMonitor/_internal/，
    # 二者必须解析到同一份外层 config/build_config.json（settings.config_path）。
    # 防的是内层再冒出一份可编辑配置、两边各读各的（对应 REPO.md §7.2/§7.3）。
    # 判据是"这一级有没有 pbb 包"，所以夹具必须先造出 source/pbb/__init__.py。
    def test_frozen_offline_monitor_reads_shared_source_config(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            root=Path(directory);source=root/'source';(source/'pbb').mkdir(parents=True)
            (source/'pbb'/'__init__.py').write_text('')
            (root/'PBBMonitor').mkdir()
            settings.write(root/'config/build_config.json',settings.fresh())
            self.assertEqual(settings.config_path(root/'PBBMonitor/_internal'),root/'config/build_config.json')
            self.assertEqual(settings.config_path(source),root/'config/build_config.json')
            # 开发仓库形态：数据根本身就是包根，config/ 就在它下面。
            self.assertEqual(settings.config_path(root),root/'config/build_config.json')

    # 期望时长只是软目标：预算用尽后任务仍要跑完并返回正常结果，因此不能给
    # subprocess.run 传 timeout 把任务强杀（那会留下半截任务/假失败）。
    def test_soft_test_target_keeps_running_after_expiry(self):
        tuner=autotune.Tuner.__new__(autotune.Tuner)
        tuner.compiler='g++';tuner.test_budget=1;tuner.test_used=5;tuner.compile_seconds=0
        with patch.object(autotune.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as process:
            result=tuner.process(['engine.exe'])
        self.assertEqual(result.returncode,0)
        self.assertNotIn('timeout',process.call_args.kwargs)


if __name__=='__main__':unittest.main()

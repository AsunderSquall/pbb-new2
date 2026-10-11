import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# 测试文件位于 tests/ 子目录，被测代码在 pbb/ 包里；先把仓库根与 tools/ 加入 sys.path。
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / 'tools'))

from pbb import autotune as tune


# 测试把临时目录建在 build/ 下，所以要保证它存在（干净检出后无需先构建）。
from pbb import paths as _paths
_paths.ensure_build_dir()

class AutoTuneTests(unittest.TestCase):
    # 本组用例守护自动测速（autotune）的三条底线：候选开关只改自己声明的那个维度、
    # 只有可复现的收益才被接受、正确性对拍与二进制安装/回退绝不静默降级。
    def test_repeatable_small_gain_is_accepted(self):
        # 收益很小（0.2%~0.6%）但三局全为正、中位数过线，就应算“可复现收益”。
        # 防止门槛写成只认大收益，从而漏掉真实存在的小优化。
        self.assertTrue(tune.choose([.006,.004,.002,.005],.003,3))

    # 三种必须拒绝的情形：正负交替的噪声、连续两局为负的回退、以及两局虽正
    # 但中位数低于门槛。防止把抖动或小幅回退写进 tested:true 的正式选择。
    def test_noise_and_regression_are_rejected(self):
        self.assertFalse(tune.choose([.05,-.04,.001,-.03],.003,3))
        self.assertFalse(tune.choose([-.08,-.06],.003,2))
        self.assertFalse(tune.choose([.002,.001],.003,2))

    # 打开 avx2（即主动放弃 AVX-512 路径）时，必须用 -mno-avx512f 覆盖调用方传进来
    # 的 -mavx512f，否则 512 配方仍会编进二进制；同时 -DPBB_UI_ENGINE 只能有一个
    # 取值——普通/UI 是两个不同目标，宏重复或冲突会让引擎类型与开关不符。
    # 关闭 lto/copy 也应如实反映为不留 -flto、写出 -DPBB_ENCODING_COPY=0。
    def test_flags_make_one_explicit_target_and_disable_512(self):
        flags=tune.flags_for(['-O3','-march=native','-mavx512f','-mavx512bw',
            '-DPBB_UI_ENGINE=0','-DPBB_ENCODING_COPY=1','-flto'],
            dict(tune.DEFAULT,avx2=True,copy=False,lto=False),True)
        self.assertNotIn('-mavx512f',flags)
        self.assertIn('-mno-avx512f',flags)
        self.assertNotIn('-flto',flags)
        self.assertEqual([f for f in flags if f.startswith('-DPBB_UI_ENGINE=')],['-DPBB_UI_ENGINE=1'])
        self.assertIn('-DPBB_ENCODING_COPY=0',flags)

    # filter 类候选只能改 filter 一项：其它开关必须与基准逐项相同，否则测出的收益
    # 无法归因，累积式局部搜索也会跑偏（见 autotune.proposals）。
    # full 模式下按 2→1→3→4 的顺序给出候选，并各自生成 -DPBB_ATTR_FILTER_MODE=n。
    def test_7640_filter_recipes_are_independent_from_other_switches(self):
        candidates=list(tune.proposals('filter',tune.DEFAULT,'full',dict(avx512=True,vbmi2=True,vl=True)))
        self.assertEqual([s['filter'] for label,s in candidates],[2,1,3,4])
        for label,switches in candidates:
            self.assertEqual({k:v for k,v in switches.items() if k!='filter'},
                             {k:v for k,v in tune.DEFAULT.items() if k!='filter'})
            self.assertIn('-DPBB_ATTR_FILTER_MODE='+str(switches['filter']),tune.flags_for([],switches,True))

    # CPU 能力探测决定候选集合：没有 vbmi2 就跳过依赖它的 2/4 号配方，没有 vl 就
    # 跳过 4 号，避免在无对应指令集的机器上编出会崩（非法指令）的引擎；
    # 已经打开 avx2 时不再提过滤候选，因为 512 路径已被主动禁用。
    def test_unsupported_or_inactive_register_recipes_are_skipped(self):
        caps=dict(avx512=True,vbmi2=False,vl=False)
        self.assertEqual([s['filter'] for label,s in tune.proposals('filter',tune.DEFAULT,'full',caps)],[1,3])
        self.assertEqual(list(tune.proposals('filter',dict(tune.DEFAULT,avx2=True),'full',caps)),[])

    # identity（SUMMARY 计数）与 hashes（结果文件摘要）是同一任务的指纹：候选与基准
    # 对拍必须完全一致，一旦不一致就抛错终止试验，避免用算错的引擎继续刷速度。
    def test_score_output_mismatch_stops_trial(self):
        a=dict(identity={'count':'1'},hashes={'results.txt':'a'},top=[],directory='a')
        b=dict(a,hashes={'results.txt':'b'})
        with self.assertRaisesRegex(RuntimeError,'Correctness mismatch'):
            tune.Tuner.same(a,b)

    # 正分名字在同一 (族,分数) 组内顺序可变，但必须有实际数值输出证明它们同分。
    # 夹具在 out/results.txt 里给 foo/bar 各写满 10 个评分族的分值（qp 都是 100）
    # 作为数值来源：把组内名字换成同分的 bar（626172）可通过，换成没有数值记录的
    # baz（62617A）必须报 name mismatch。
    def test_positive_top_tie_requires_matching_numeric_scores(self):
        with tempfile.TemporaryDirectory(dir=tune.ROOT/'build') as temp:
            p=Path(temp);(p/'out').mkdir()
            (p/'out/results.txt').write_bytes(b'foo 100 0 0 0 0 0 0 0 0 0\nbar 100 0 0 0 0 0 0 0 0 0\n')
            a=dict(identity={},hashes={},top=[('qp',100,'666F6F')],directory=temp)
            b=dict(a,top=[('qp',100,'626172')])
            tune.Tuner.same(a,b)
            b['top']=[('qp',100,'62617A')]
            with self.assertRaisesRegex(RuntimeError,'name mismatch'):
                tune.Tuner.same(a,b)

    # 同族同分名字的到达顺序取决于线程调度，因此允许换序；但分数本身变了必须报
    # Top score mismatch。directory='not-read' 表示这一条不依赖数值输出文件即可判定。
    def test_equal_score_names_can_swap_without_numeric_output(self):
        a=dict(identity={},hashes={},directory='not-read',
            top=[('cp',3746,'AA'),('cp',3746,'BB')])
        b=dict(a,top=[('cp',3746,'BB'),('cp',3746,'AA')])
        tune.Tuner.same(a,b)
        b['top']=[('cp',3746,'BB'),('cp',3747,'AA')]
        with self.assertRaisesRegex(RuntimeError,'Top score mismatch'):
            tune.Tuner.same(a,b)

    # 数值对拍必须按 params.txt 的 score_list / output_xp 还原“实际被计算的子集列”
    # （README：列序固定为 qp..pj 的子集，不是固定两列）。夹具先给 output_xp=1 使
    # 两个名字的分数可被证明相同，再改成 0 —— 失去了证明依据，同一对拍必须失败。
    def test_numeric_tie_proof_uses_actual_subset_columns(self):
        with tempfile.TemporaryDirectory(dir=tune.ROOT/'build') as temp:
            p=Path(temp);(p/'out').mkdir()
            (p/'params.txt').write_text('score_list=cp,qp\noutput_xp=1\n')
            (p/'out/results.txt').write_bytes(b'foo 50 100\nbar 60 100\n')
            a=dict(identity={},hashes={},directory=temp,top=[('cp',100,'666F6F')])
            b=dict(a,top=[('cp',100,'626172')])
            tune.Tuner.same(a,b)
            (p/'params.txt').write_text('score_list=cp,qp\noutput_xp=0\n')
            with self.assertRaisesRegex(RuntimeError,'name mismatch'):
                tune.Tuner.same(a,b)

    # 安装/回退的安全边界：安装前必须备份现有二进制与 .build.json 清单，同一结果目录
    # 只能安装一次（第二次应报 backup already），回退要还原二进制和清单原文。
    # 夹具在临时目录里伪造一次完整测速结果（selection.json + 新旧二进制 + 源码哈希），
    # 并 patch 掉 ROOT、机器指纹和清单写入，确保不碰真实 build/ 目录。
    def test_install_and_rollback_preserve_previous_binaries(self):
        # 注意：这里必须显式导入 pbb._build，不能写 `import build` —— 仓库根有个
        # build/ 目录（编译产物），它会作为一个命名空间包遮蔽同名模块。
        from pbb import _build as build
        with tempfile.TemporaryDirectory(dir=tune.ROOT/'build') as temp,patch.dict(os.environ,{'PBB_SRC_DIR':''}):
            root=Path(temp);(root/'build').mkdir();run=root/'trial';run.mkdir()
            (root/'engine_main.cpp').write_text('unchanged')
            selected={}
            for name,file in [('standard','pbb_engine.exe'),('ui','pbb_ui_engine.exe')]:
                # 每个目标都预置一份“旧”二进制和旧清单，用于验证回退内容与安装前一致。
                target=root/'build'/file;target.write_bytes(b'old')
                Path(str(target)+'.build.json').write_bytes(b'old manifest')
                binary=run/file;binary.write_bytes(b'new')
                selected[name]=dict(binary=file,sha256=tune.digest(binary),flags=['-O3'])
            tune.save(run/'selection.json',dict(status='complete',machine=['test'],selected=selected,
                source_hashes={'engine_main.cpp':tune.digest(root/'engine_main.cpp')},compiler='g++'))
            def record(path,*args,**kwargs):
                # 用假清单代替 build._record_binary：只关心“清单被一起替换/还原”，
                # 不真的去算二进制摘要。
                Path(path+'.build.json').write_text(json.dumps(kwargs))
            with patch.object(tune,'ROOT',root),patch.object(build,'_machine_identity',return_value=['test']),patch.object(build,'_record_binary',side_effect=record):
                # 让 install 把临时目录当成仓库根，从而全部读写都落在夹具里。
                tune.install(run)
                self.assertEqual((root/'build/pbb_engine.exe').read_bytes(),b'new')
                with self.assertRaisesRegex(RuntimeError,'backup already'):
                    tune.install(run)
                tune.install(run,rollback=True)
                self.assertEqual((root/'build/pbb_engine.exe').read_bytes(),b'old')
                self.assertEqual((root/'build/pbb_ui_engine.exe.build.json').read_bytes(),b'old manifest')


if __name__=='__main__':unittest.main()

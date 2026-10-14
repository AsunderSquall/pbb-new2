"""默认阈值、QP二次扣分、标量/四路及普通/UI 输出。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pbb import engine, paths, ui
from pbb.main import _validate_config


class DefaultScoreTests(unittest.TestCase):
    def test_resume_rule_changed_from_shadow_gate(self):
        import hashlib
        from pbb.flex import resume_id
        previous={'output_rule':'sp1-illusion-effective-gt60-shadow-lt396.4282801381853-v3','layout':['61',[]]}
        old=hashlib.sha256(json.dumps(previous,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        self.assertNotEqual(resume_id({},b'a',[]),old)

    def test_new_defaults_include_pp_pd_and_old_missing_fields_stay_disabled(self):
        for path in [ROOT/'config'/('config.example.'+ext) for ext in ('yaml','json','toml')]+[ROOT/'config/config_flex.example.yaml',ROOT/'config/config_football.yaml']:
            cfg=ui.load_config(str(path))
            self.assertEqual(cfg['collection']['min_pp'],8000)
            self.assertEqual(cfg['collection']['min_pd'],8800)
            self.assertTrue({'pp','pd'} <= set(ui._normalize_score_list(cfg['collection']['score_list'])))
            _validate_config(cfg)
        cfg=ui.blank_config()
        self.assertEqual((cfg['collection']['min_pp'],cfg['collection']['min_pd']),(8000,8800))
        cfg['collection'].pop('min_pp');cfg['collection'].pop('min_pd')
        from pbb.main import _build_task_config
        task=_build_task_config(cfg)
        self.assertEqual((task['min_pp'],task['min_pd']),(0,0))


class QpFilterEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """显式使用正式引擎同一组编译参数，测试二进制放在隔离目录。"""
        if not all(paths.engine_binary(ui=x).exists() for x in (False,True)):
            raise unittest.SkipTest('请先运行 tools/rebuild_local.py')
        cls.directory=tempfile.TemporaryDirectory(dir=paths.BUILD_DIR)
        cls.addClassCleanup(cls.directory.cleanup)
        run=Path(cls.directory.name)
        manifest=json.loads(Path(str(paths.engine_binary())+'.build.json').read_text())
        compiler=manifest['compiler']
        if not any(name in Path(compiler).name.lower() for name in ('g++','clang++')):
            raise unittest.SkipTest('原生模型夹具使用 GCC/Clang 编译参数')
        binary=run/'qp_filter_probe.exe'
        command=[compiler,*manifest['flags'],
                 '-I'+str(ROOT/'pbb/native/src'),str(ROOT/'tests/native_qp_filter.cpp'),'-o',str(binary)]
        build=subprocess.run(command,capture_output=True,text=True,encoding='utf8',timeout=120)
        if build.returncode:raise AssertionError(build.stderr)
        result=subprocess.run([str(binary)],capture_output=True,text=True,encoding='utf8',timeout=180)
        if result.returncode:raise AssertionError(result.stderr)
        cls.fixture=json.loads(result.stdout)
        print('[qp filter fixtures] '+json.dumps(cls.fixture,ensure_ascii=False))

    def execute(self, task, ui_target=False):
        """只测 fixture 对应的一个真实名字，不生成或修改生产结果。"""
        directory=tempfile.TemporaryDirectory(dir=paths.BUILD_DIR)
        self.addCleanup(directory.cleanup)
        out=Path(directory.name)/'out';out.mkdir()
        candidate=task.get('candidate',self.fixture['name'])
        params=engine._build_params(task,task.get('test_charset',candidate[-1:].encode().hex()),'result.txt')
        result=subprocess.run([str(paths.engine_binary(ui=ui_target))],input=params,capture_output=True,
                text=True,encoding='utf8',cwd=directory.name,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return (out/'result.txt').read_text(encoding='utf8'),(out/'blue.txt').read_text(encoding='utf8'),result.stderr

    def task(self):
        f=self.fixture
        task={'team_name':'test','prefixes':[f['name'][:-1]],'suffixes':[''],
              'scl':1,'variable_len':1,'mode':1,'range_L':0,'range_R':1,
              'n_threads':2,'output_xp':1,'output_log':0,'output_speed':0,
              'score_mode':'all','score_list':'qp,qd','collect_mode':2,'c_eight_v_min':0,
              'c_seven_v_min':9999,'c_hl_min':9999,'c_hp398_min':9999}
        for family in ('qp','qd','pp','pd','cqd','cp','fp','wc','fs','pj'):
            task['min_'+family]=0;task['ptt_'+family]=0
        task['min_qp']=1
        return task

    def test_surviving_high_illusion_is_kept_with_both_engines(self):
        for mode in ('all','any'):
            for ui_target in (False,True):
                with self.subTest(mode=mode,ui=ui_target):
                    task=self.task();task.update(score_mode=mode,ui_mode=ui_target)
                    output,blue,stderr=self.execute(task,ui_target)
                    self.assertIn(self.fixture['name']+'@test',output)
                    self.assertIn(self.fixture['name']+'@test',blue)
                    self.assertIn('found=1 count=1',stderr)
                    if ui_target:self.assertIn('UI_TOP_BEGIN',stderr)
                    else:self.assertNotIn('UI_TOP_BEGIN',stderr)

    def test_other_score_threshold_boundary_rescues_in_any_mode(self):
        for offset,expected in ((0,True),(1,False)):
            candidate=self.fixture['potential_name']
            task=self.task();task.update(candidate=candidate,prefixes=[candidate[:-1]],score_mode='any',score_list='qp,pj',min_qp=1000000,min_pj=self.fixture['pj']+offset)
            output,_,_=self.execute(task)
            self.assertEqual(bool(output),expected)
            if expected:self.assertIn(str(self.fixture['pj']),output)

    def test_other_potential_threshold_rescues(self):
        candidate=self.fixture['potential_name']
        task=self.task();task.update(candidate=candidate,prefixes=[candidate[:-1]],score_mode='any',min_qp=1000000,score_list='qp,pj',ptt_pj=1)
        output,_,_=self.execute(task)
        self.assertIn(candidate+'@test',output)

    def test_raw_high_but_effective_low_illusion_is_kept(self):
        candidate='旋风逐步测量杂费'
        for mode in ('all','any'):
            for ui_target in (False,True):
                task=self.task();task.update(candidate=candidate,team_name='昀澤',prefixes=[candidate[:-1]],
                    scl=3,test_charset=candidate[-1].encode().hex(),score_mode=mode,ui_mode=ui_target)
                output,_,_=self.execute(task,ui_target)
                self.assertIn(candidate+'@昀澤',output)

    def supplied_task(self):
        """复用真实默认阈值和线程；仅把候选范围缩为用户提供的完整名字。"""
        from pbb.main import _build_task_config
        task=_build_task_config(ui.load_config(str(ROOT/'config/config_football.yaml')))
        candidate='帕秋莉 R6PIXzP'
        task.update(candidate=candidate,team_name='Squall',prefixes=[candidate[:-1]],suffixes=[''],
                    scl=1,variable_len=1,range_L=0,range_R=1,mode=1,
                    output_xp=1,output_log=0,output_speed=0)
        return task

    def test_supplied_sample_kept_by_default_fs_after_qp_correction(self):
        sample=self.fixture['user_sample']
        self.assertLess(sample['qp'],5400)
        self.assertEqual(sample['fs'],4007)
        self.assertGreater(sample['deduction'],0)
        self.assertGreater(sample['illusion_freq'],60)
        self.assertGreater(sample['effective'],60)
        self.assertLess(sample['s'],396.4282801381853)
        for mode in ('all','any'):
            for ui_target in (False,True):
                task=self.supplied_task();task.update(score_mode=mode,ui_mode=ui_target)
                output,_,_=self.execute(task,ui_target)
                self.assertIn('帕秋莉 R6PIXzP@Squall',output)

    def test_supplied_real_sample_is_rejected_when_only_qp_passes(self):
        for mode in ('all','any'):
            for ui_target in (False,True):
                for count in (1,4):
                    with self.subTest(mode=mode,ui=ui_target,count=count):
                        task=self.supplied_task();task.update(score_mode=mode,ui_mode=ui_target,
                            score_list='qp',range_R=count,test_charset=(b'P'*count).hex())
                        for key in tuple(task):
                            if (key.startswith('min_') and key!='min_qp') or key.startswith('ptt_'):task[key]=0
                        output,_,stderr=self.execute(task,ui_target)
                        self.assertEqual(output,'')
                        self.assertIn('found=0 count='+str(count),stderr)

    def test_supplied_sample_kept_when_only_fs_is_output(self):
        for mode in ('all','any'):
            for ui_target in (False,True):
                task=self.supplied_task();task.update(score_mode=mode,ui_mode=ui_target,score_list='fs',min_fs=3900)
                for key in tuple(task):
                    if (key.startswith('min_') and key!='min_fs') or key.startswith('ptt_'):task[key]=0
                output,_,_=self.execute(task,ui_target)
                self.assertIn('帕秋莉 R6PIXzP@Squall',output)
                self.assertIn('4007',output)


if __name__=='__main__':unittest.main()

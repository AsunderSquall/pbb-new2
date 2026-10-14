"""守护格式位置、混合进制、旧配置兼容及真实普通/UI 引擎输出。"""
import copy
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pbb import engine, flex, paths, ui
from pbb.main import _build_task_config, _validate_config, _try_resume


def config():
    """构造含固定片段和具名引用的测试配置。"""
    cfg = ui.blank_config()
    cfg['team_name'] = 'test'
    cfg['character_sets'] = {'数字': {'single_char_length': 1, 'types': [1]}}
    cfg['format'] = ['固定', *[{'charset': '数字'} for _ in range(6)], '-尾']
    cfg['enumeration']['mode'] = 1
    cfg['enumeration']['ranges'] = [{'start': 0, 'end': 100000}]
    cfg['threads']['worker_threads'] = 2
    cfg['collection'].update(score_list='qp,qd,pp,pd,cqd,cp,fp,wc,fs,pj', collect_mode=2,
                             special_thresholds={'eight_v_min': 0})
    for family in ('qp', 'qd', 'pp', 'pd', 'cqd', 'cp', 'fp', 'wc', 'fs', 'pj'):
        cfg['collection']['min_'+family] = 1
    cfg['output'].update(output_xp=1, log_output=0, speed_output=0)
    return cfg


class FlexConfigTests(unittest.TestCase):
    def test_normalization_keeps_original_and_legacy(self):
        cfg = config()
        original = copy.deepcopy(cfg)
        cfg.pop('prefixes'); cfg.pop('suffixes'); cfg.pop('character_set')
        normalized = flex.normalize_config(cfg)
        self.assertEqual(normalized['enumeration']['variable_length'], 6)
        self.assertEqual(normalized['prefixes'], [{'name': '+'}])
        self.assertNotIn('character_set', cfg)
        legacy = ui.blank_config()
        self.assertIs(flex.normalize_config(legacy), legacy)
        _validate_config(cfg)
        self.assertEqual(_build_task_config(cfg)['format'], original['format'])
        self.assertTrue(ui.ConfigSelector.is_search_config(cfg))
        self.assertFalse(ui.ConfigSelector.is_search_config({'options': {}}))

    def test_layout_rank_and_fixed_string_disambiguation(self):
        task = {'character_sets': {'a': {'single_char_length': 1}, 'b': {'single_char_length': 3}},
                'format': ['a', {'charset': 'a'}, '-', {'charset': 'b'}, '尾']}
        template, slots = flex.build_layout(task, lambda cs: ('xy' if cs['single_char_length']==1 else '春夏').encode().hex())
        self.assertEqual([(s[0], s[1]) for s in slots], [(1, 1), (3, 3)])
        for rank, name in enumerate(('ax-春尾', 'ax-夏尾', 'ay-春尾', 'ay-夏尾')):
            self.assertEqual(flex.parse_rank(name.encode(), template, slots), rank)
        self.assertIsNone(flex.parse_rank('bx-春尾'.encode(), template, slots))
        self.assertIsNone(flex.parse_rank('ax-秋尾'.encode(), template, slots))

    def test_invalid_format_and_widths_rejected(self):
        for fmt in ([], ['固定'], [{'charset': 'missing'}], [{'charset': '数字', 'extra': 1}], [1]):
            cfg=config();cfg['format']=fmt
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):flex.normalize_config(cfg)
        for width, custom in ((3, '春A'), (6, '护球春'), (1, [0]), (2, [1, 2, 3])):
            cfg=config();cfg['character_sets']={'数字': {'single_char_length': width,
                              'types': [{1:4,2:7,3:7}.get(width,1)], 'custom_values': custom}}
            with self.subTest(width=width), self.assertRaises(ValueError):flex.normalize_config(cfg)
        cfg=config();cfg['format'].insert(0, 'x'*512)
        with self.assertRaises(ValueError):flex.normalize_config(cfg)

    def test_yaml_json_toml_roundtrip(self):
        cfg=config();cfg.pop('character_set')
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            for ext in ('yaml','json','toml'):
                path=Path(directory)/('flex.'+ext)
                ui.save_config(path,cfg)
                self.assertEqual(ui.load_config(str(path)),cfg)

    def test_resume_reconstructs_mixed_radix(self):
        cfg=config()
        cfg['character_sets']['汉字']={'single_char_length':3,'types':[7],'custom_values':'春夏'}
        cfg['format']=['号',{'charset':'数字'},'-',{'charset':'汉字'}]
        cfg['enumeration']['ranges']=[{'start':0,'end':20}]
        task = _build_task_config(cfg)
        template, slots = flex.build_layout(task, engine._build_charset)
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            out = Path(directory)/'out';out.mkdir()
            (out/'unused.resume').write_text(flex.resume_id(task, template, slots)+'\n0 8 20\n', encoding='ascii')
            resumed, _ = _try_resume(cfg, 'unused', out_dir=directory)
        self.assertEqual(resumed['enumeration']['prefix_ranges'],[{'count':1,'start':8,'end':20}])

    def test_file_import_relative_bom_words_and_frozen_task(self):
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            file = Path(directory)/'words.txt'
            file.write_text('护球\n盘带\n护球\n', encoding='utf-8-sig')
            cfg=config();cfg['character_sets']={'词':{'single_char_length':6,'file':'words.txt'}}
            cfg['format']=[{'charset':'词'},' ',{'charset':'词'}]
            path=Path(directory)/'config.yaml';ui.save_config(path,cfg)
            loaded=ui.load_config(str(path));task=_build_task_config(loaded)
            self.assertEqual(engine._build_charset(task['character_sets']['词']), '护球盘带护球'.encode().hex())
            self.assertEqual(ui.Monitor._charset_size(loaded['character_sets']['词']),3)
            file.write_text('过人\n射门\n',encoding='utf8')
            self.assertEqual(engine._build_charset(task['character_sets']['词']), '护球盘带护球'.encode().hex())
            file.write_text('护球\nA\n',encoding='utf8')
            with self.assertRaises(ValueError):flex.normalize_config(loaded)

    def test_resume_checks_file_content_and_keeps_each_prefix_range(self):
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            file=Path(directory)/'chars.txt';file.write_text('0\n1\n',encoding='utf8')
            cfg=config();cfg['character_sets']={'数字':{'single_char_length':1,'file':str(file)}}
            cfg['prefixes']=[{'name':'A'},{'name':'AB'},{'name':'A'}]
            cfg['enumeration']['ranges']=[{'start':0,'end':-1}]
            cfg['enumeration']['prefix_ranges']=[{'count':1,'start':2,'end':-1},{'count':1,'start':10,'end':40},{'count':1,'start':0,'end':0}]
            task=_build_task_config(cfg);template,slots=flex.build_layout(task,engine._build_charset)
            out=Path(directory)/'out';out.mkdir()
            self.assertIsNone(_try_resume(cfg,'r',out_dir=directory))
            checkpoint=out/'r.resume'
            checkpoint.write_text(flex.resume_id(task,template,slots)+'\n2 12 64\n10 20 40\n0 0 0\n',encoding='ascii')
            resumed,_=_try_resume(cfg,'r',out_dir=directory)
            self.assertEqual(resumed['enumeration']['prefix_ranges'],[{'count':1,'start':12,'end':64},{'count':1,'start':20,'end':40},{'count':1,'start':0,'end':0}])
            wider=copy.deepcopy(cfg);wider['enumeration']['prefix_ranges'][1]['end']=41
            with self.assertRaisesRegex(ValueError,'覆盖范围'):_try_resume(wider,'r',out_dir=directory)
            file.write_text('1\n0\n',encoding='utf8')
            with self.assertRaisesRegex(ValueError,'不一致'):_try_resume(cfg,'r',out_dir=directory)

    def test_text_file_import_preserves_spaces_and_rejects_invalid_sources(self):
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            path=Path(directory)/'chars.txt';path.write_text('a b\n',encoding='utf8')
            cs={'single_char_length':1,'file':str(path),'file_format':'text'}
            self.assertEqual(flex.file_bytes(cs),b'a b')
            cfg=config();cfg['character_sets']['数字']=dict(cs,types=[1])
            with self.assertRaises(ValueError):flex.normalize_config(cfg)

    def test_editor_preserves_and_edits_flex_configuration(self):
        cfg=config();cfg.pop('character_set')
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory, \
                patch.object(ui.messagebox, 'showerror', side_effect=AssertionError('编辑器不应显示错误弹窗')):
            path=Path(directory)/'editor.yaml';ui.save_config(path,cfg)
            window=ui.Monitor();window.withdraw()
            try:
                window._set_config_path(str(path))
                with patch.object(ui, 'CONFIG_DIR', directory):
                    window._open_editor()
                window.editor.withdraw()
                result=window._editor_config()
                self.assertEqual(result['format'],cfg['format'])
                self.assertEqual(result['character_sets'],cfg['character_sets'])
                self.assertNotIn('character_set',result)
                imported=Path(directory)/'letters.txt';imported.write_text('α\nβ\n',encoding='utf8')
                with patch.object(ui.filedialog,'askopenfilename',return_value=str(imported)), patch.object(ui.simpledialog,'askstring',return_value='希腊'):
                    window._import_flex_file()
                saved=window._editor_config()
                self.assertEqual(saved['character_sets']['希腊']['single_char_length'],2)
                self.assertEqual(saved['character_sets']['希腊']['file'],str(imported.resolve()))
                # UI 还没有收到进度行时，也能用检查点回写每前缀起点。
                out=Path(directory)/'out';out.mkdir()
                task=_build_task_config(cfg);template,slots=flex.build_layout(task,engine._build_charset)
                (out/'r.resume').write_text(flex.resume_id(task,template,slots)+'\n0 12 100000\n',encoding='ascii')
                window._run_origin=cfg;window._run_config_path=str(path)
                window._run_result_file='r';window._run_processed=0
                window._run_start_updated=False;window.auto_start_var.set(True)
                with patch.object(ui,'ROOT',directory):window._advance_config_start()
                updated=ui.load_config(str(path))
                self.assertEqual(updated['enumeration']['prefix_ranges'],[{'count':1,'start':12,'end':100000}])
                _validate_config(updated)
                window.flex_enabled_var.set(False)
                legacy=window._editor_config()
                self.assertNotIn('format',legacy)
                self.assertIn('character_set',legacy)
                self.assertTrue(ui.ConfigSelector.is_search_config(result))
                _validate_config(result)
            finally:
                window.update_idletasks()
                # 清掉测试窗口的延迟回调，避免之后的 Tk 测试收到已销毁控件事件。
                for callback in window.tk.call('after','info'):
                    window.after_cancel(callback)
                window._close_editor();window.destroy()


class FlexEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not all(paths.engine_binary(ui=x).exists() for x in (False,True)):
            raise unittest.SkipTest('请先运行 tools/rebuild_local.py')

    def execute(self, task, ui_target=False, charset=None):
        """用独立目录执行已构建引擎，返回完整输出、收集和权威摘要。"""
        charset = charset or engine._build_charset(task['character_set'])
        params=engine._build_params(task,charset,'result.txt')
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            out=Path(directory)/'out';out.mkdir()
            result=subprocess.run([str(paths.engine_binary(ui=ui_target))],input=params,
                capture_output=True,text=True,encoding='utf8',cwd=directory,timeout=90)
            self.assertEqual(result.returncode,0,result.stderr)
            lines=sorted((out/'result.txt').read_text(encoding='utf8').splitlines())
            blue=sorted((out/'blue.txt').read_text(encoding='utf8').splitlines()) if (out/'blue.txt').exists() else []
        summary=re.search(r'SUMMARY max_sum=\d+ max_xp=\d+ max_xd=\d+ found=\d+ count=\d+',result.stderr).group()
        return lines,blue,summary,result.stderr

    def test_native_checkpoint_without_hits_and_completed_resume(self):
        cfg=config();cfg['prefixes']=[{'name':'A'},{'name':'B'}]
        cfg['enumeration']['ranges']=[{'start':13,'end':2100031}]
        cfg['format']=[{'charset':'数字'}]*7
        task=_build_task_config(cfg)
        # 所有候选评分仍实际计算，但输出门阈值确保结果文件为空。
        for key in tuple(task):
            if key.startswith('min_'):task[key]=1000000
        for ui_target in (False,True):
            with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
                out=Path(directory)/'out';out.mkdir()
                result=subprocess.run([str(paths.engine_binary(ui=ui_target))],input=engine._build_params(task,engine._build_charset(task['character_set']),'r'),capture_output=True,text=True,encoding='utf8',cwd=directory,timeout=90)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual((out/'r').read_text(), '')
                rows=(out/'r.resume').read_text().splitlines()
                self.assertEqual(rows[1:],['13 2100031 2100031']*2)
                template,slots=flex.build_layout(task,engine._build_charset)
                self.assertEqual(rows[0],flex.resume_id(task,template,slots))

    def test_imported_charset_matches_inline_real_scoring(self):
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            file=Path(directory)/'digits.txt';file.write_text('\n'.join('0123456789'),encoding='utf-8-sig')
            cfg=config();cfg['character_sets']['数字']={'single_char_length':1,'file':str(file)}
            imported=self.execute(_build_task_config(cfg))
            inline=self.execute(_build_task_config(config()))
            self.assertEqual(imported[:3],inline[:3]);self.assertTrue(imported[0])

    def test_interrupted_parallel_resume_matches_full_output(self):
        cfg=config();cfg['format']=[' 空 ',*[{'charset':'数字'}]*7,' 尾 ']
        cfg['prefixes']=[{'name':'A'},{'name':'AB'}]
        cfg['enumeration']['ranges']=[{'start':17,'end':4000031}]
        cfg['threads']['worker_threads']=3
        task=_build_task_config(cfg)
        reference=self.execute(task)
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            out=Path(directory)/'out';out.mkdir()
            params=engine._build_params(task,engine._build_charset(task['character_set']),'r')
            proc=subprocess.Popen([str(paths.engine_binary())],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,encoding='utf8',cwd=directory)
            try:
                proc.stdin.write(params);proc.stdin.close()
                deadline=time.monotonic()+30
                while time.monotonic()<deadline and proc.poll() is None:
                    cp=out/'r.resume'
                    if cp.exists():
                        rows=cp.read_text().splitlines()[1:]
                        if rows and int(rows[0].split()[1])>17:break
                    time.sleep(.02)
                self.assertIsNone(proc.poll(),'测试任务应在检查点生成后仍在运行')
                proc.kill();proc.wait(timeout=10)
            finally:
                if proc.poll() is None:proc.kill();proc.wait()
                proc.stderr.close()
            resumed,_=_try_resume(cfg,'r',out_dir=directory)
            remainder=_build_task_config(resumed)
            done=subprocess.run([str(paths.engine_binary())],input=engine._build_params(remainder,engine._build_charset(remainder['character_set']),'r'),capture_output=True,text=True,encoding='utf8',cwd=directory,timeout=90)
            self.assertEqual(done.returncode,0,done.stderr)
            self.assertEqual(set((out/'r').read_text(encoding='utf8').splitlines()),set(reference[0]))
            self.assertEqual(set((out/'blue.txt').read_text(encoding='utf8').splitlines()),set(reference[1]))
            final,_=_try_resume(cfg,'r',out_dir=directory)
            self.assertTrue(all(r['start']==r['end']==4000031 for r in final['enumeration']['prefix_ranges']))
            _validate_config(final)

    def test_homogeneous_format_matches_legacy_all_modes_and_widths(self):
        nonempty=0
        for width, chars in ((1,'0123456789'),(2,'αβγδεζηθικ'),(3,'春夏秋冬山河星月日云'),
                             (4,'😀😁😂😃😄😅😆😉😊😋'),(6,'护球盘带过人射门拉球扣球趟球内切拨球踩球')):
            cs={'single_char_length':width,'types':[{1:4,2:7,3:7,4:2}.get(width,1)],'custom_values':chars}
            for mode in (1,2,3,4):
                cfg=config();cfg['character_sets']={'数字':cs};cfg['format']=[{'charset':'数字'} for _ in range(7)]
                cfg['enumeration']['mode']=mode
                cfg['enumeration']['ranges']=[{'start':13,'end':200013}]
                task=_build_task_config(cfg);task['seed']=123;task['prefixes']=['固定'];task['suffixes']=['尾']
                legacy=copy.deepcopy(task);legacy.pop('format');legacy.pop('character_sets')
                legacy['character_set']=cs;legacy['scl']=width
                with self.subTest(width=width,mode=mode):
                    new=self.execute(task);old=self.execute(legacy)
                    self.assertEqual(new[:3],old[:3])
                    self.assertTrue(new[0]);nonempty+=len(new[0])
        self.assertGreater(nonempty,0)

    def test_ui_scores_output_zero_and_threads_do_not_change_results(self):
        task=_build_task_config(config());task['ui_mode']=True;task['output_xp']=0
        regular=self.execute(task)
        single=self.execute(dict(task,n_threads=1),True)
        parallel=self.execute(task,True)
        self.assertEqual(regular[:3],single[:3]);self.assertEqual(single[:3],parallel[:3])
        self.assertTrue(single[0]);self.assertNotIn('UI_TOP',regular[3])
        receiver=type('Receiver',(),{'families':['qp','qd'],'boards':{},'boards_dirty':False})()
        for line in parallel[3].splitlines():
            if line.startswith('UI_TOP'):ui.Monitor._handle_line(receiver,line)
        self.assertTrue(receiver.boards_dirty)
        self.assertGreater(receiver.boards['qp'][0][0],0)

    def test_range_clipping_and_scalar_tail(self):
        cfg=config();cfg['format']=[{'charset':'数字'}]
        task=_build_task_config(cfg)
        for start,end,count in ((0,3,3),(0,4,4),(7,30,3),(15,30,0)):
            with self.subTest(start=start,end=end):
                result=self.execute(dict(task,range_L=start,range_R=end))
                self.assertTrue(result[2].endswith('count='+str(count)))

    def test_cross_chunk_tail_and_many_positions(self):
        cfg=config();cfg['format']=[{'charset':'数字'} for _ in range(7)]
        task=_build_task_config(cfg);task.update(range_L=999997,range_R=1200003,n_threads=3)
        legacy=copy.deepcopy(task);legacy.pop('format');legacy.pop('character_sets')
        self.assertEqual(self.execute(task)[:3],self.execute(legacy)[:3])
        cfg['character_sets']={'数字':{'single_char_length':1,'types':[4],'custom_values':'a'}}
        cfg['format']=[{'charset':'数字'} for _ in range(32)]
        task=_build_task_config(cfg);task.update(range_L=0,range_R=100)
        self.assertTrue(self.execute(task)[2].endswith('count=1'))

    def test_mixed_random_modes_are_seeded_and_thread_independent(self):
        cfg=config()
        cfg['character_sets']['汉字']={'single_char_length':3,'types':[7],'custom_values':'春夏秋冬'}
        cfg['format']=['[',{'charset':'汉字'},'-',*[{'charset':'数字'} for _ in range(7)],']']
        task=_build_task_config(cfg);task.update(range_L=0,range_R=1000003,seed=42)
        for mode in (2,3,4):
            with self.subTest(mode=mode):
                a=self.execute(dict(task,mode=mode,n_threads=1))
                b=self.execute(dict(task,mode=mode,n_threads=3))
                self.assertEqual(a[:3],b[:3]);self.assertTrue(a[0])
                count=1000003 if mode==3 else 2000000
                self.assertTrue(a[2].endswith('count='+str(count)))
                self.assertTrue(all(re.match(r'\[[春夏秋冬]-\d{7}\]@test ',x) for x in a[0]))

    def test_runtime_thread_control_with_flex_ui(self):
        task=_build_task_config(config());task.update(range_R=20000000,mode=2,seed=123,
                                                     ui_mode=True,output_xp=0,n_threads=2)
        params=engine._build_params(task,engine._build_charset(task['character_set']),'result.txt')
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            out=Path(directory)/'out';out.mkdir()
            proc=subprocess.Popen([str(paths.engine_binary(ui=True))],stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',cwd=directory)
            def control():
                time.sleep(.2);(out/'.threads').write_text('1')
                time.sleep(1.5);(out/'.threads').write_text('2')
            writer=threading.Thread(target=control);writer.start()
            try:
                _,stderr=proc.communicate(params,timeout=90)
            finally:
                if proc.poll() is None:proc.kill();proc.wait()
                writer.join()
            self.assertEqual(proc.returncode,0,stderr)
            self.assertIn('threads → 1',stderr);self.assertIn('threads → 2',stderr)
            self.assertIn('UI_TOP_END',stderr)


if __name__=='__main__':unittest.main()

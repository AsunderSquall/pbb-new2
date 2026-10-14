"""PBB host-local paired autotuning with persistent build choices.

python autotune.py --mode quick --config config_football.yaml --speed 6.1 --minutes 10
python autotune.py --install-from build/autotune/<run>
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import math
import subprocess
import sys
import time
from collections import Counter, defaultdict
import build_settings

ROOT = Path(__file__).resolve().parent
DEFAULT = build_settings.DEFAULTS
FILTER_NAMES = {0:'original',1:'guard512',2:'reg512',3:'mixed512',4:'reg256'}
SEEDS = (123, 789, 20261009)
FAMILIES = 'qp qd pp pd cqd cp fp wc fs pj'.split()


def digest(path):
    with open(path, 'rb') as f:
        return (hashlib.file_digest(f, 'sha256').hexdigest() if hasattr(hashlib,'file_digest')
                else hashlib.sha256(f.read()).hexdigest())


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    os.replace(temporary, path)


def choose(pairs, minimum, required_wins):
    return len(pairs) >= required_wins and sum(x > 0 for x in pairs) >= required_wins and statistics.median(pairs) >= minimum


def flags_for(base, switches):
    return build_settings.apply_flags(base,dict(DEFAULT,**switches))


def load_config(path):
    suffix = Path(path).suffix.lower()
    if suffix in ('.yaml', '.yml'):
        import yaml
        return yaml.safe_load(Path(path).read_text(encoding='utf8'))
    if suffix == '.toml':
        import tomllib
        return tomllib.loads(Path(path).read_text(encoding='utf8'))
    return json.loads(Path(path).read_text(encoding='utf8'))


def candidate_count(speed_tday, seconds, multiplier=1):
    raw=speed_tday*1e12/86400*seconds/max(1,multiplier)
    if not math.isfinite(raw) or raw>2**63-1:raise ValueError('Requested candidate count is too large')
    return max(1000000,int(raw)//1000000*1000000)


def proposals(key, switches, mode, capabilities):
    if key != 'filter':
        yield key,dict(switches, **{key:not switches[key]})
        return
    # Evaluate native filtering before forcing AVX2: otherwise all 512 recipes
    # disappear from the compiled path and reg512 can never be discovered.
    if switches['avx2'] or not capabilities['avx512']:
        return
    options=([2,1]+([0] if switches['filter'] else []) if mode=='quick' else [2,1,3,4,0])
    for recipe in options:
        if recipe==switches['filter']:
            continue
        if recipe in (2,4) and not capabilities['vbmi2']:
            continue
        if recipe==4 and not capabilities['vl']:
            continue
        yield 'filter_'+FILTER_NAMES[recipe],dict(switches,filter=recipe)


class Tuner:
    def __init__(self, args):
        import build
        import engine
        from main import _build_task_config
        self.build, self.engine, self.args = build, engine, args
        if os.environ.get('PBB_CXXFLAGS', '').strip() or os.environ.get('PBB_SRC_DIR', '').strip():
            raise RuntimeError('Unset PBB_CXXFLAGS and PBB_SRC_DIR before tuning; custom baselines are not supported.')
        os.environ['PBB_BUILD_MODE'] = 'native'
        self.started = time.monotonic()
        self.test_budget = args.minutes*60
        self.test_used = self.compile_seconds = 0.
        self.run = ROOT/'build'/'autotune'/time.strftime('%Y%m%d_%H%M%S')
        self.run.mkdir(parents=True)
        settings_path=build_settings.config_path(ROOT)
        if settings_path.exists():shutil.copy2(settings_path,self.run/'build_config.before.json')
        else:save(self.run/'build_config.before.json',build_settings.fresh())
        shutil.copy2(__file__, self.run/'autotune_script.py')
        self.snapshot = self.run/'source'
        shutil.copytree(ROOT/'src', self.snapshot/'src')
        shutil.copy2(ROOT/'engine_main.cpp', self.snapshot/'engine_main.cpp')
        shutil.copy2(ROOT/'autotune_filter_verify.cpp', self.snapshot/'autotune_filter_verify.cpp')
        self.sources = {str(p.relative_to(self.snapshot)): digest(p) for p in self.snapshot.rglob('*') if p.is_file()}
        shutil.copy2(args.config, self.run/('config'+Path(args.config).suffix))
        self.task = _build_task_config(load_config(args.config))
        build.ensure_core()
        sys.path.insert(0, str(ROOT/'build'))
        import pbb_core
        pbb_core.init_exhanzi()
        self.charset = engine._build_charset(self.task['character_set'])
        if not self.charset:
            raise ValueError('Empty character set')
        compilers = [c for c in build._find_compilers() if not c[3] and 'g++' in Path(c[0]).name.lower()]
        if not compilers:
            raise RuntimeError('This first version requires GCC/g++; icpx/MSVC tuning is not implemented.')
        self.compiler, self.base_flags, self.simd, _ = compilers[0]
        profile,active=build_settings.load(ROOT,build._machine_identity(),self.compiler)
        self.initial = profile['options'].copy() if active else DEFAULT.copy()
        if sys.platform == 'win32':
            self.base_flags += ['-static', '-static-libgcc', '-static-libstdc++']
        self.ui = False
        self.cache, self.check_cache, self.reference_checks = {}, {}, {}
        self.records, self.trials = [], []
        self.serial = 0
        save(self.run/'environment.json', dict(machine=build._machine_identity(), compiler=self.compiler,
             compiler_version=self.process([self.compiler, '--version']).stdout.decode(errors='replace'),
             base_flags=self.base_flags, simd=self.simd, target=args.target, mode=args.mode,
             test_minutes=args.minutes, speed_tday=args.speed, threads=self.task['n_threads'], task=self.task, charset=self.charset,
             sources=self.sources, python=sys.executable))
        self.flush()
        print('Results:', self.run, flush=True)

    def left(self):
        return self.test_budget-self.test_used

    def process(self, command, **kwargs):
        compiling=os.path.normcase(str(command[0]))==os.path.normcase(str(self.compiler))
        start=time.monotonic()
        try:
            return subprocess.run(command,capture_output=True,**kwargs)
        finally:
            elapsed=time.monotonic()-start
            if compiling:self.compile_seconds+=elapsed
            else:self.test_used+=elapsed

    def flush(self):
        save(self.run/'measurements.json', self.records)
        save(self.run/'trials.json', self.trials)

    def compile(self, switches):
        key = tuple(sorted(switches.items()))
        if key in self.cache:
            return self.cache[key]
        label = f'variant_{len(self.cache):02d}'
        path = self.run/(label+('.exe' if sys.platform == 'win32' else '.bin'))
        flags = flags_for(self.base_flags, switches)
        cmd = self.build._compile(self.compiler, flags, False, str(self.snapshot/'engine_main.cpp'), str(path),
                                  extra_includes=[str(self.snapshot/'src')])
        print('Compile', label, switches, 'CLI', flush=True)
        r = self.process(cmd, env=self.build._compiler_environment(self.compiler))
        (self.run/(label+'.compile.log')).write_bytes(r.stderr)
        if r.returncode:
            raise RuntimeError(f'Compilation failed: {label}; see compile.log')
        save(self.run/(label+'.build.json'), dict(command=cmd, flags=flags, switches=switches,
             compiler=self.compiler, sha256=digest(path), sources=self.sources))
        self.cache[key] = path
        return path

    def execute(self, binary, task, charset, label):
        self.serial += 1
        directory = self.run/'runs'/f'{self.serial:04d}_{label}'
        directory.mkdir(parents=True)
        (directory/'out').mkdir()
        task = dict(task, ui_mode=int(self.ui), output_log=0, output_speed=0)
        params = self.engine._build_params(task, charset, 'results.txt')
        (directory/'params.txt').write_text(params, encoding='utf8')
        start = time.monotonic()
        r = self.process([str(binary)], cwd=directory, input=params.encode('utf8'))
        (directory/'stderr.txt').write_bytes(r.stderr)
        (directory/'stdout.txt').write_bytes(r.stdout)
        if r.returncode:
            raise RuntimeError(f'Engine failed ({r.returncode}): {directory}')
        match = re.search(rb'SUMMARY (.*)', r.stderr)
        if not match:
            raise RuntimeError(f'No authoritative SUMMARY: {directory}')
        fields = dict(x.split('=', 1) for x in match[1].decode().split())
        hashes, lines = {}, {}
        for name in ('results.txt', 'blue.txt'):
            p = directory/'out'/name
            data = sorted(p.read_bytes().splitlines()) if p.exists() else []
            hashes[name] = hashlib.sha256(b'\n'.join(data)).hexdigest()
            lines[name] = len(data)
        top = []
        row = dict(binary=binary.name, directory=str(directory), wall=time.monotonic()-start,
                   speed=float(fields['speed']), elapsed=float(fields['elapsed']), lines=lines,
                   identity={k:fields[k] for k in ('max_sum','max_xp','max_xd','found','count')}, hashes=hashes,
                   top=top)
        self.records.append(row)
        self.flush()
        return row

    @staticmethod
    def same(a, b):
        if a['identity'] != b['identity'] or a['hashes'] != b['hashes']:
            raise RuntimeError('Correctness mismatch: '+a['directory']+' versus '+b['directory'])
        # Strict score insertion has arrival-order ties. Preserve score sequences;
        # names with positive scores must agree up to equal-score groups.
        if [(f,s) for f,s,n in a['top']] != [(f,s) for f,s,n in b['top']]:
            raise RuntimeError('UI Top score mismatch')
        groups = []
        for row in (a,b):
            grouped = defaultdict(Counter)
            for family,score,name in row['top']:
                grouped[(family,score)][name] += 1
            groups.append(grouped)
        for (family,score),names in groups[0].items():
            other = groups[1][(family,score)]
            # Completion order can reorder the same names within a tied group.
            # This proof does not need numeric output (or lower output thresholds).
            if score <= 0 or names == other:
                continue
            pool = {}
            for row in (a,b):
                directory=Path(row['directory'])
                params=directory/'params.txt'
                families=FAMILIES
                if params.exists():
                    import engine
                    values=dict(line.split('=',1) for line in params.read_text(encoding='utf8').splitlines() if '=' in line)
                    if values.get('output_xp')=='0':continue
                    families=engine._plan_families(values)
                for line in (directory/'out/results.txt').read_bytes().splitlines():
                    parts=line.rsplit(b' ',len(families))
                    if len(parts)==len(families)+1:
                        try:scores=list(map(int,parts[1:]))
                        except ValueError:continue
                        pool[parts[0].hex().upper()]=dict(zip(families,scores))
            changed=set(names-other)|set(other-names)
            if any(pool.get(name,{}).get(family)!=score for name in changed):
                raise RuntimeError('UI Top name mismatch outside verified equal-score group: '
                    +family+' '+str(score)+'; '+a['directory']+' versus '+b['directory'])

    def checks(self):
        encodings = [(1,'0123456789'), (2,'αβγδεζηθ'), (3,'天地玄黄宇宙洪荒'),
                     (4,'😀😁😂😃😄😅😆😇'), (6,'盘带射门传球过人抢断扑救带球进球')]
        tests = []
        for scl,chars in encodings:
            for mode in ((1,2,3,4) if self.args.mode == 'full' else (1,2)):
                t = dict(self.task, scl=scl, mode=mode, prefixes=['前','另'], suffixes=['后'],
                         team_name='验证', variable_len=7, range_L=999997, range_R=2000000,
                         score_list=','.join(FAMILIES), score_mode='all', output_xp=1,
                         seed=SEEDS[mode%3], collect_mode=mode%3)
                for family in FAMILIES:
                    t['min_'+family] = 1
                tests.append((t, chars.encode().hex()))
        for length in (63,64,65,66):
            t = dict(self.task, scl=1, mode=1, prefixes=['+'], suffixes=['x'*(length-9)],
                     variable_len=7, range_L=999997, range_R=1000004, output_xp=1)
            tests.append((t,b'0123456789'.hex()))
        return tests

    def verify(self, binary):
        if binary in self.check_cache:
            return
        metadata=json.loads((self.run/(binary.stem+'.build.json')).read_text(encoding='utf8'))
        verify_binary=self.run/(binary.stem+'_filter_verify'+('.exe' if sys.platform=='win32' else '.bin'))
        cmd=self.build._compile(self.compiler,metadata['flags'],False,
             str(self.snapshot/'autotune_filter_verify.cpp'),str(verify_binary),extra_includes=[str(self.snapshot/'src')])
        compiled=self.process(cmd,env=self.build._compiler_environment(self.compiler))
        (self.run/(binary.stem+'.filter_compile.log')).write_bytes(compiled.stderr)
        if compiled.returncode:
            raise RuntimeError('Filter verifier compilation failed: '+binary.name)
        checked=self.process([str(verify_binary)])
        (self.run/(binary.stem+'.filter_verify.txt')).write_bytes(checked.stdout+checked.stderr)
        if checked.returncode:
            raise RuntimeError('Intermediate filter/capacity verification failed: '+binary.name)
        save(self.run/(binary.stem+'.filter_build.json'),dict(command=cmd,sha256=digest(verify_binary)))
        found = 0
        for i,(task,charset) in enumerate(self.checks()):
            if i not in self.reference_checks:
                self.reference_checks[i] = self.execute(self.reference, task, charset, f'check_ref_{i}')
            row = self.reference_checks[i] if binary == self.reference else self.execute(binary, task, charset, f'check_{binary.stem}_{i}')
            self.same(self.reference_checks[i], row)
            found += int(row['identity']['found'])
        if not found:
            raise RuntimeError('Correctness suite had no scoring hits')
        self.check_cache[binary] = found
        print('Correctness passed', binary.name, 'hits', found, flush=True)

    def paired(self, a, b, rounds, label):
        self.last_rates={a:[],b:[]}
        for binary in dict.fromkeys((a,b)):
            self.execute(binary, dict(self.performance, range_R=self.performance['range_L']+max(1000000,self.count//2)), self.charset, 'warm_'+label)
        pairs = []
        for i in range(rounds):
            data = {}
            for binary in ((a,b) if i%2==0 else (b,a)):
                data[binary] = self.execute(binary, dict(self.performance,seed=SEEDS[i%3]), self.charset, label+f'_{i}')
            self.same(data[a],data[b])
            for binary in (a,b):self.last_rates[binary].append(data[binary]['speed'])
            change = data[b]['speed']/data[a]['speed']-1
            pairs.append(change)
            print(label, i+1, f'{change:+.3%}', flush=True)
        return pairs

    def main(self):
        switches = self.initial.copy()
        self.reference = current = self.compile(switches)
        self.verify(current)
        start = self.task['range_L']
        if isinstance(start,list):
            raise ValueError('Per-prefix ranges are not yet supported by autotune')
        start = max(0,int(start))
        multiplier = len(self.task['prefixes'])*len(self.task['suffixes'])
        capabilities=dict(avx512=all(self.build._cpu_supports(self.compiler,f) for f in ('avx512f','avx512bw')),
                          vbmi2=bool(self.build._cpu_supports(self.compiler,'avx512vbmi2')),
                          vl=bool(self.build._cpu_supports(self.compiler,'avx512vl')))
        keys = ['filter','avx2','ksa','copy','attr','lto']
        if not self.build._cpu_supports(self.compiler,'avx2'):
            keys.remove('avx2')
        if self.args.mode == 'full':
            keys += ['unroll','prefer256']
        trials_per_pass=sum(len(list(proposals(k,switches,self.args.mode,capabilities))) for k in keys)
        passes_count=1 if self.args.mode=='quick' else 2
        rounds=2 if self.args.mode=='quick' else 3
        final_rounds=4 if self.args.mode=='quick' else 6
        units=trials_per_pass*passes_count*(2*rounds+1)+(2*final_rounds+1)
        self.planned_seconds=max(.05,self.left()*.80/max(1,units))
        self.count=candidate_count(self.args.speed*.9,self.planned_seconds,multiplier)
        self.performance=dict(self.task,range_L=start,range_R=start+self.count,seed=SEEDS[0])
        save(self.run/'test_plan.json',dict(speed_tday=self.args.speed,test_minutes=self.args.minutes,
            planning_speed_tday=self.args.speed*.9,speed_factor=.9,time_limit='soft',
            planned_trials=trials_per_pass*passes_count,measurement_units=units,
            seconds_per_task=self.planned_seconds,candidates_per_range=self.count,rounding_unit=1000000,
            formula='T/day * 0.9 * 1e12 / 86400 * seconds_per_task / affix_combinations'))
        save(self.run/'build_config.progress.json',dict(build_settings.fresh(),status='testing',options=switches))
        # Each pass visits O(n) neighbors; full mode revisits in reverse order.
        passes = [keys] if self.args.mode == 'quick' else [keys,list(reversed(keys))]
        over_target_reported=False
        for pass_number,order in enumerate(passes):
            for key in order:
                for label,proposed in proposals(key,switches,self.args.mode,capabilities):
                    if self.left() <= 0 and not over_target_reported:
                        print('已超过期望测试时长；继续完成剩余测试、最终确认和产物验证。',flush=True)
                        over_target_reported=True
                    candidate = self.compile(proposed)
                    self.verify(candidate)
                    pairs = self.paired(current,candidate,2 if self.args.mode=='quick' else 3,f'pass{pass_number}_{label}')
                    accepted = choose(pairs,self.args.minimum,2)
                    self.trials.append(dict(pass_number=pass_number,key=label,before=dict(switches),proposal=proposed,
                                            pairs=pairs,paired_median=statistics.median(pairs),accepted=accepted))
                    if accepted:
                        switches,current = proposed,candidate
                        speed=statistics.median(self.last_rates[current])
                        self.count=candidate_count(speed*.9*86400/1e12,self.planned_seconds,multiplier)
                        self.performance['range_R']=start+self.count
                    save(self.run/'build_config.progress.json',dict(build_settings.fresh(),status='testing',
                        options=dict(switches),completed_trials=len(self.trials)))
                    self.flush()
        final_pairs = []
        confirmed = current == self.reference
        if current != self.reference:
            final_pairs = self.paired(self.reference,current,4 if self.args.mode=='quick' else 6,'final')
            confirmed = choose(final_pairs,self.args.minimum,3 if self.args.mode=='quick' else 5)
            if not confirmed:
                print('Combined result not confirmed; keep initial selection.',flush=True)
                switches,current = self.initial.copy(),self.reference
        binary = self.compile(switches)
        self.verify(binary)
        selected = {'standard': dict(binary=binary.name,sha256=digest(binary),
                flags=flags_for(self.base_flags,switches))}
        result = dict(status='complete',switches=switches,selected=selected,source_hashes=self.sources,
                      machine=self.build._machine_identity(),compiler=self.compiler,mode=self.args.mode,
                      measured_target='standard',final_pairs=final_pairs,combination_confirmed=confirmed,
                      elapsed=time.monotonic()-self.started,config=str(Path(self.args.config).resolve()),
                      test_seconds=self.test_used,compile_seconds=self.compile_seconds,
                      initial_options=self.initial,minimum=self.args.minimum,threads=self.task['n_threads'])
        save(self.run/'selection.json',result)
        settings=dict(build_settings.fresh(),tested=True,status='complete',options=dict(switches),
             machine=result['machine'],compiler=self.compiler,source_hashes=self.sources,result=str(self.run))
        save(self.run/'build_config.selected.json',settings)
        save(self.run/'build_config.progress.json',settings)
        if not self.args.no_write_config:
            previous=(self.run/'build_config.before.json').read_bytes()
            path=build_settings.config_path(ROOT)
            if path.exists() and path.read_bytes()!=previous:
                raise RuntimeError('build_config.json changed during testing; refuse overwrite')
            build_settings.write(path,settings)
            print('已保存 build_config.json。测试完成后仍须 Rebuild 一遍，才会把选择应用到实际引擎。',flush=True)
        self.report(result)
        print('Complete. Selection:', self.run/'selection.json',flush=True)

    def report(self,result):
        lines=['# PBB 自动测评结果','',f'状态：{result["status"]}；目标：{self.args.target}；线程保持 {result["threads"]}。',
               f'测试耗时 {result["test_seconds"]:.1f}s；测试预算 {self.args.minutes*60}s；编译 {result["compile_seconds"]:.1f}s（不计入预算）；总耗时 {result["elapsed"]:.1f}s。','',
               '逐项测试的原始速度、完整输出排序哈希、汇总、UI协议和运行参数见 measurements.json / runs/。',
               '邻居试验均先编译和正确性检查，再预热、正反交错配对；只保留达到重复确认条件的收益。','']
        for trial in self.trials:
            lines.append(f'- {trial["key"]}：'+', '.join(f'{p:+.3%}' for p in trial['pairs'])+f'；{"接受" if trial["accepted"] else "保留原设置"}。')
        lines += ['', '最终组合相对本轮起点：'+(', '.join(f'{p:+.3%}' for p in result['final_pairs']) or '未选出可信更快组合，保留本轮起点。'),
                  '', '组合确认：'+('通过或保留本轮起点' if result['combination_confirmed'] else '未通过，已回到本轮起点'),
                  '', '选中开关：'+str(result['switches']), '',
                  'CLI 引擎及完整参数、源码/二进制哈希见 selection.json 和 variant_*.build.json。',
                  '过滤函数另以随机排列对拍前 max_len+1 项属性、完整256字节技能数据、技能阶段重建数据和容量哨兵；包含max_len=30及全部0..127边界。',
                  '正确性包含多编码、枚举、前后缀、队伍、全部评分族、非空数值输出、收集及复制边界。潜力使用原阈值，未强制验证每种潜力命中；未追踪所有其他中间状态。',
                  '精简版验证 mode1/2；完整版验证 mode1/2/3/4。仅在所选配置和目标上判断性能，另一目标只验证正确性。',
                  '未调线程数，未修改评分、门控或候选生成。低阈值只用于正确性；性能保留配置的评分、潜力、收集及结果输出设置，关闭终端进度日志。',
                  '同分Top保持原到达顺序规则；零分同分名允许变化，正分差异须验证同分或拒绝。',
                  '这是 O(n) 局部搜索，不声称穷举最优。时长为软目标，超时继续；错误/中断会留下 incomplete.json。',
                  '默认只保存构建选择，测试后仍须 Rebuild，否则实际引擎尚未应用该选择；--apply 是直接安装的例外。',
                  '没有可靠温度、频率、功耗或 PMU 遥测。native产物只限本机，不用于通用发布包。','',
                  '安装（关闭占用引擎的进程）：','',
                  f'```powershell\n& "{sys.executable}" autotune.py --install-from "{self.run}"\n```']
        (self.run/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')


def install(run, rollback=False):
    import build
    if os.environ.get('PBB_SRC_DIR','').strip():
        raise RuntimeError('Unset PBB_SRC_DIR before installation/rollback')
    run = Path(run).resolve()
    selection = json.loads((run/'selection.json').read_text(encoding='utf8'))
    if selection['status'] != 'complete' or selection['machine'] != build._machine_identity():
        raise RuntimeError('Incomplete result or different host; installation refused')
    for rel,sha in selection['source_hashes'].items():
        if digest(ROOT/rel) != sha:
            raise RuntimeError('Source changed since tuning; rerun: '+rel)
    extension = '.exe' if sys.platform == 'win32' else ''
    targets = {'standard': ROOT/'build'/('pbb_engine'+extension)}
    backup = run/'before_install'
    state = run/'installed.json'
    if rollback:
        expected = json.loads(state.read_text(encoding='utf8'))
        for rel,sha in expected.items():
            if digest(ROOT/rel)!=sha:
                raise RuntimeError('Installed file changed; refuse to overwrite: '+rel)
        for rel in expected:
            shutil.copy2(backup/Path(rel).name,ROOT/rel)
        print('Restored previous ordinary/UI engines and manifests.')
        return
    if state.exists() or backup.exists():
        raise RuntimeError('This run has an installation backup already; refuse to overwrite it')
    for name,item in selection['selected'].items():
        if digest(run/item['binary']) != item['sha256']:
            raise RuntimeError('Selected binary hash mismatch')
        if not targets[name].exists() or not Path(str(targets[name])+'.build.json').exists():
            raise RuntimeError('Build the CLI engine first, so it can be backed up')
    backup.mkdir()
    paths = [p for binary in targets.values() for p in (binary,Path(str(binary)+'.build.json'))]
    profile_path=build_settings.config_path(ROOT)
    if profile_path.exists():
        old=run/'build_config.before.json'
        published=run/'build_config.selected.json'
        is_published=(published.exists() and json.loads(profile_path.read_text(encoding='utf8'))==
                      json.loads(published.read_text(encoding='utf8')))
        shutil.copy2(old if old.exists() and is_published else profile_path,backup/profile_path.name)
    else:save(backup/profile_path.name,build_settings.fresh())
    paths.append(profile_path)
    for p in paths:
        if p!=profile_path:shutil.copy2(p,backup/p.name)
    os.environ['PBB_BUILD_MODE']='native'
    try:
        settings=dict(build_settings.fresh(),tested=True,status='complete',options=dict(DEFAULT,**selection.get('switches',{})),
            machine=selection['machine'],compiler=selection['compiler'],source_hashes=selection['source_hashes'],result=str(run))
        build_settings.write(profile_path,settings)
        for name,item in selection['selected'].items():
            binary=targets[name]
            shutil.copy2(run/item['binary'],binary)
            build._record_binary(str(binary),selection['compiler'],item['flags'],engine_target=name)
        save(state,{os.path.relpath(p,ROOT):digest(p) for p in paths})
    except BaseException:
        for p in paths:
            shutil.copy2(backup/p.name,p)
        raise
    print('Installed. CLI rebuild now reads build_config.json.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['quick','full'],default='quick')
    config_root=build_settings.config_root(ROOT)
    default_config=config_root/'config.yaml'
    if not default_config.is_file():default_config=config_root/'config_football.yaml'
    parser.add_argument('--config',default=str(default_config))
    parser.add_argument('--target',choices=['standard'],default='standard', help='Linux 仅支持 CLI 引擎')
    parser.add_argument('--speed',type=float,help='Previous real engine throughput in T/day (float)')
    parser.add_argument('--minutes',type=float,help='Expected testing minutes (soft target), excluding compilation; recommended 20')
    parser.add_argument('--budget',type=float,help='Legacy alias: testing time in seconds, excluding compilation')
    parser.add_argument('--minimum',type=float,default=.003,help='Required median gain; default 0.3%%')
    parser.add_argument('--install-from',type=Path)
    parser.add_argument('--rollback-from',type=Path)
    parser.add_argument('--apply',action='store_true',help='Install only after a complete successful tuning run')
    parser.add_argument('--no-write-config',action='store_true',help='Isolated evaluation; keep the project build_config unchanged')
    args=parser.parse_args()
    if args.install_from and args.rollback_from:
        parser.error('Choose installation or rollback')
    if args.install_from or args.rollback_from:
        install(args.install_from or args.rollback_from,rollback=bool(args.rollback_from))
        return
    if args.speed is None:
        args.speed=float(input('此前测号速度是多少 T/day？（浮点数，例如 1.5）：'))
    if args.minutes is None:
        entered=input('期望测试多少分钟？（不含编译，推荐 20，回车使用 20）：') if args.budget is None else ''
        args.minutes=args.budget/60 if args.budget is not None else float(entered.strip() or '20')
    if not all(math.isfinite(x) and x>0 for x in (args.speed,args.minutes)) or not math.isfinite(args.minimum) or args.minimum<0:
        parser.error('speed/minutes must be finite and positive; minimum >= 0')
    tuner=None
    try:
        tuner=Tuner(args)
        tuner.main()
        if args.apply:
            install(tuner.run)
    except KeyboardInterrupt as exc:
        if tuner:
            save(tuner.run/'incomplete.json',dict(reason=type(exc).__name__,elapsed=time.monotonic()-tuner.started))
        print('用户中断测试。未安装正式引擎；已保留完成的记录。',file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        if tuner:
            save(tuner.run/'incomplete.json',dict(reason=str(exc),elapsed=time.monotonic()-tuner.started))
        raise


if __name__=='__main__':
    main()

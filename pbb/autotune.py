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

from . import build_settings, paths
from .threading_config import resolve_threads

# 仓库根：构建选项、源码树与二进制都以它为基准。定义收拢在 pbb/paths.py。
ROOT = paths.REPO_ROOT
DEFAULT = build_settings.DEFAULTS
# 过滤方案编号 → 名称。与 build_settings.apply_flags 的 PBB_ATTR_FILTER_MODE 取值对应：
#   0 original 原版 / 1 guard512 / 2 reg512 / 3 mixed512 / 4 reg256
FILTER_NAMES = {0:'original',1:'guard512',2:'reg512',3:'mixed512',4:'reg256'}
# 固定种子：同一种子下两次比较才是可复现的同一任务（见 AUTOTUNE.md「测试量与预算」）。
SEEDS = (123, 789, 20261009)
FAMILIES = 'qp qd pp pd cqd cp fp wc fs pj'.split()


def digest(path):
    """返回文件的 SHA-256 十六进制摘要。

    用于 build_config.json 的 source_hashes：源码一变，本机测速结论即失效。
    hashlib.file_digest 是 3.11+ 才有的，低版本回退到一次性读入内存。
    """
    with open(path, 'rb') as f:
        return (hashlib.file_digest(f, 'sha256').hexdigest() if hasattr(hashlib,'file_digest')
                else hashlib.sha256(f.read()).hexdigest())


def save(path, value):
    """以「先写 .tmp 再替换」的方式落盘 JSON，避免中途失败留下半个文件。"""
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    os.replace(temporary, path)


def choose(pairs, minimum, required_wins):
    """判定一组配对测量是否足以「接受」某个候选组合。

    三个条件同时满足才算通过：样本数够、变快的次数够、中位收益不低于 minimum。
    用中位数而不是均值，是为了抗单次测量的抖动（墙钟测量含 IPC 噪声）。
    """
    return len(pairs) >= required_wins and sum(x > 0 for x in pairs) >= required_wins and statistics.median(pairs) >= minimum


def flags_for(base, switches, ui):
    """把一组开关折叠成实际编译参数；UI 目标额外加 -DPBB_UI_ENGINE=1。

    注意 apply_flags 会先剔除 base 里的 PBB_* 宏与 LTO/展开选项再按 switches 重写，
    所以这里传 base 是为了继承 --mode/--std 等无关优化项的原始参数。
    """
    return build_settings.apply_flags(base,dict(DEFAULT,**switches))+[f'-DPBB_UI_ENGINE={int(ui)}']


def candidate_count(speed_tday, seconds, multiplier=1):
    """由「输入速度(T/day) + 单任务秒数」估算一个任务的枚举范围长度。

    公式见 AUTOTUNE.md：范围长度 = T/day × 0.9 × 10^12 ÷ 86400 × 秒数 ÷ 前后缀组合数
    （调用方已把 0.9 折进 speed_tday、把组合数折进 multiplier）。
    结果向下取整到一百万的整数倍、最少一百万 —— 引擎以一百万为 chunk，
    不足一个 chunk 的尾批仍按整块处理，取整可以避免把尾批开销当成真实吞吐。
    """
    raw=speed_tday*1e12/86400*seconds/max(1,multiplier)
    if not math.isfinite(raw) or raw>2**63-1:raise ValueError('Requested candidate count is too large')
    return max(1000000,int(raw)//1000000*1000000)


def proposals(key, switches, mode, capabilities):
    """生成某一开关的邻居候选（局部搜索的一步）。

    非 filter 开关很简单：直接取反生成一个候选。
    filter 特殊：它是多值枚举（5 种方案），且必须**先测 native 过滤再考虑 AVX2**——
    否则所有 512 位配方都会被编译期排除，reg512 永远发现不了。
    另外按 CPU 实际能力裁剪：reg256(4) 需要 AVX-512VL，reg512(2)/reg256(4) 需要 VBMI2。
    """
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
        from . import _build as build
        from . import engine
        from .main import _build_task_config
        from .ui import load_config
        self.build, self.engine, self.args = build, engine, args
        if os.environ.get('PBB_CXXFLAGS', '').strip() or os.environ.get('PBB_SRC_DIR', '').strip():
            raise RuntimeError('Unset PBB_CXXFLAGS and PBB_SRC_DIR before tuning; custom baselines are not supported.')
        os.environ['PBB_BUILD_MODE'] = 'native'
        self.started = time.monotonic()
        self.test_budget = args.minutes*60
        self.test_used = self.compile_seconds = 0.
        # 每一轮测速都落一个独立目录，内含源码快照、编译命令、参数、输出与哈希，
        # 便于事后单独复查"这一轮到底测了什么"。
        self.run = paths.BUILD_DIR/'autotune'/time.strftime('%Y%m%d_%H%M%S')
        self.run.mkdir(parents=True)
        # 先备份当前构建选项，回退时用它还原（见 install(rollback=True)）。
        settings_path=build_settings.config_path(paths.DATA_ROOT)
        if settings_path.exists():shutil.copy2(settings_path,self.run/'build_config.before.json')
        else:save(self.run/'build_config.before.json',build_settings.fresh())
        shutil.copy2(__file__, self.run/'autotune_script.py')
        # 源码快照：编译的是这份副本而不是工作区，保证本轮结论与当时源码严格对应。
        # 目录结构与 pbb/native/ 保持一致，编译命令里的相对路径才不会错位。
        self.snapshot = self.run/'source'
        shutil.copytree(paths.NATIVE_SRC, self.snapshot/'native'/'src')
        shutil.copy2(paths.ENGINE_MAIN, self.snapshot/'native'/'engine_main.cpp')
        shutil.copy2(paths.FILTER_VERIFY, self.snapshot/'native'/'autotune_filter_verify.cpp')
        self.sources = {str(p.relative_to(self.snapshot)): digest(p) for p in self.snapshot.rglob('*') if p.is_file()}
        shutil.copy2(args.config, self.run/('config'+Path(args.config).suffix))
        self.task = _build_task_config(load_config(args.config))
        configured_threads = self.task.get('n_threads', -1)
        self.task['n_threads'] = resolve_threads(configured_threads)
        print(f"Threads: {self.task['n_threads']} ({'automatic' if configured_threads == -1 else 'configured'}); fixed for all trials.", flush=True)
        build.ensure_core()
        pbb_core = engine._pbb_core()
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
        self.ui = args.target == 'ui'
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

    def compile(self, switches, ui=None):
        """按一组开关编译候选引擎，返回二进制路径（同一开关组合只编译一次）。

        为了可复现，编译的是 self.snapshot 里的源码副本而不是工作区：
        run 目录会保存源码、完整编译命令、参数与哈希，事后可以单独复查这一轮测的是什么。
        """
        ui = self.ui if ui is None else ui
        key = (tuple(sorted(switches.items())), ui)
        if key in self.cache:
            return self.cache[key]
        label = f'variant_{len(self.cache):02d}'
        path = self.run/(label+('.exe' if sys.platform == 'win32' else '.bin'))
        flags = flags_for(self.base_flags, switches, ui)
        cmd = self.build._compile(self.compiler, flags, False, str(self.snapshot/'native'/'engine_main.cpp'), str(path),
                                  extra_includes=[str(self.snapshot/'native'/'src')])
        print('Compile', label, switches, 'UI' if ui else 'ordinary', flush=True)
        r = self.process(cmd, env=self.build._compiler_environment(self.compiler))
        (self.run/(label+'.compile.log')).write_bytes(r.stderr)
        if r.returncode:
            raise RuntimeError(f'Compilation failed: {label}; see compile.log')
        save(self.run/(label+'.build.json'), dict(command=cmd, flags=flags, switches=switches, ui=ui,
             compiler=self.compiler, sha256=digest(path), sources=self.sources))
        self.cache[key] = path
        return path

    def execute(self, binary, task, charset, label):
        """在隔离目录里跑完一个完整任务，记录结果供「正确性对拍」和「性能比较」使用。

        每次调用落一个 runs/NNNN_label/ 目录，保存参数、引擎输出与结果文件；
        关键产物是三类指纹：
          - identity：引擎 SUMMARY 里的 max_sum/max_xp/max_xd/found/count
          - hashes  ：results.txt / blue.txt 排序后的 SHA-256
          - top     ：UI 模式的 Top 名单（族、分数、名字）
        这三类指纹就是 same() 判定「两个二进制结果一致」的依据。
        另外把 output_log/output_speed 关掉：性能测量不需要生产日志，且必须保证
        不同候选拿到的任务参数完全一致，只有编译开关不同。
        """
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
        if self.ui:
            text = r.stderr.decode()
            if 'UI_TOP_BEGIN' not in text:
                raise RuntimeError('UI protocol missing')
            for line in text.split('UI_TOP_BEGIN')[-1].split('UI_TOP_END')[0].splitlines():
                if line.startswith('UI_TOP\t'):
                    _, family, score, name = line.split('\t')
                    top.append((family, int(score), name))
        row = dict(binary=binary.name, directory=str(directory), wall=time.monotonic()-start,
                   speed=float(fields['speed']), elapsed=float(fields['elapsed']), lines=lines,
                   identity={k:fields[k] for k in ('max_sum','max_xp','max_xd','found','count')}, hashes=hashes,
                   top=top)
        self.records.append(row)
        self.flush()
        return row

    @staticmethod
    def same(a, b):
        """断言两次运行的「结果」完全相同，不同就抛错终止本轮。

        这是自动测速的安全阀：只接受**结果等价**的加速。
        判定分三层：
          1. identity 与结果文件哈希必须逐字节一致（排序后比较，抵消线程到达顺序差异）；
          2. UI Top 的 (族, 分数) 序列必须一致；
          3. Top 名单里同分组内的名字允许顺序不同（完成顺序会重排并列项），
             但**分数为正**的名字若出现差异，必须用结果文件的分数证明它们确实同分，
             否则判为真实差异。
        第 3 层是为了不把「同分并列的到达顺序变化」误判成正确性问题，
        同时又不放过真正的分数差异；低于等于 0 分的分组不参与比较。
        """
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
                    from . import engine
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
        """构造正确性覆盖用的任务清单（每个元素是 (task, charset_hex)）。

        覆盖目标是 AUTOTUNE.md 列出的那些边界，而不是"多跑几个任务"：
          - 五种字节宽度：1 / 2 / 3 / 4 / 6 字节单元，各配一个非 ASCII 字符集；
          - 枚举模式：quick 覆盖 1/2，full 再补 3/4；
          - 每个任务都带前缀、后缀、队伍名，并把 output_xp 打开（要能对拍分数列）；
          - collect_mode 按 mode%3 轮换，让 0/1/2 三种收集规则都被走到；
          - 范围起点取 999997：故意跨 chunk 边界，覆盖尾批与复制边界。
        第二组专门做「长后缀」：63/64/65/66 字节，用来撞 256 字节技能数据的容量哨兵边界。
        所有族的阈值都压到 1，目的只是保证有命中可以对比，不代表生产门控。
        """
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
                # 编码矩阵仍验证指定的五种编码，不能被用户格式覆盖。
                t.pop('format', None)
                t.pop('character_sets', None)
                tests.append((t, chars.encode().hex()))
        for length in (63,64,65,66):
            t = dict(self.task, scl=1, mode=1, prefixes=['+'], suffixes=['x'*(length-9)],
                     variable_len=7, range_L=999997, range_R=1000004, output_xp=1)
            t.pop('format', None)
            t.pop('character_sets', None)
            tests.append((t,b'0123456789'.hex()))
        if 'format' in self.task:
            # 另外对拍用户的真实位置布局；合成矩阵的非空要求仍然保留。
            t = dict(self.task, mode=1, range_L=0, range_R=1000003, output_xp=1)
            for family in FAMILIES:
                t['min_'+family] = 1
            tests.append((t,self.charset))
        return tests

    def verify(self, binary):
        """验证一个候选二进制：先编译过滤函数对拍程序，再跑正确性覆盖清单。

        两步缺一不可：
          1. autotune_filter_verify.cpp 用**同一组编译参数**编译，单独检查中间过滤
             函数与容量哨兵（属性排列、完整 256 字节技能数据、技能重建、max_len 边界）。
             这一步能抓到"结果碰巧一样但中间状态已被改坏"的情况。
          2. 逐个跑 checks()，与参考二进制 self.same() 对拍。
        最后要求命中总数非 0：全是 0 命中意味着这轮对拍其实什么都没验证到，
        必须当成失败而不是"通过"。结果按二进制缓存，避免重复验证。
        """
        if binary in self.check_cache:
            return
        metadata=json.loads((self.run/(binary.stem+'.build.json')).read_text(encoding='utf8'))
        verify_binary=self.run/(binary.stem+'_filter_verify'+('.exe' if sys.platform=='win32' else '.bin'))
        cmd=self.build._compile(self.compiler,metadata['flags'],False,
             str(self.snapshot/'native'/'autotune_filter_verify.cpp'),str(verify_binary),extra_includes=[str(self.snapshot/'native'/'src')])
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
        """交替测量两个二进制若干轮，返回每轮的相对速度差（b 相对 a 的百分比）。

        三个设计要点：
          - 先各跑一次半程预热（warm_*），把首次运行的页缓存/频率爬坡影响摊掉；
          - 每轮**交换先后顺序**（偶数轮 a→b，奇数轮 b→a），抵消"后跑者受前一次影响"
            这类系统偏差；
          - 每轮都调用 same() 对拍，确保比较的是两个结果等价的二进制 —— 只比快慢
            不比正确性的加速结论是不能采信的。
        返回的 pairs 交给 choose() 判定是否接受。
        """
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
        # Produce both real targets. Do not silently apply a UI speed conclusion
        # to ordinary performance: the report identifies which one was measured.
        selected = {}
        measured_ui = self.ui
        measured_reference=self.reference
        measured_checks=self.reference_checks
        measured_cache=self.check_cache
        for ui in (False,True):
            binary = self.compile(switches,ui=ui)
            self.ui = ui
            if ui==measured_ui:
                self.reference=measured_reference
                self.reference_checks=measured_checks
                self.check_cache=measured_cache
            else:
                self.reference = self.compile(self.initial,ui=ui)
                self.reference_checks = {}
                self.check_cache = {}
            self.verify(binary)
            selected['ui' if ui else 'standard'] = dict(binary=binary.name,sha256=digest(binary),
                flags=flags_for(self.base_flags,switches,ui))
        self.ui = measured_ui
        result = dict(status='complete',switches=switches,selected=selected,source_hashes=self.sources,
                      machine=self.build._machine_identity(),compiler=self.compiler,mode=self.args.mode,
                      measured_target=self.args.target,final_pairs=final_pairs,combination_confirmed=confirmed,
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
                  '两个可运行目标及完整参数、源码/二进制哈希见 selection.json 和 variant_*.build.json。',
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
    from . import _build as build
    if os.environ.get('PBB_SRC_DIR','').strip():
        raise RuntimeError('Unset PBB_SRC_DIR before installation/rollback')
    run = Path(run).resolve()
    selection = json.loads((run/'selection.json').read_text(encoding='utf8'))
    if selection['status'] != 'complete' or selection['machine'] != build._machine_identity():
        raise RuntimeError('Incomplete result or different host; installation refused')
    # source_hashes 的键是相对快照根的路径（形如 native/src/engine.hpp），
    # 与仓库布局一致，因此可以直接在仓库根下逐个复核。
    for rel,sha in selection['source_hashes'].items():
        if digest(ROOT/rel) != sha:
            raise RuntimeError('Source changed since tuning; rerun: '+rel)
    targets = {name: paths.engine_binary(ui=(name=='ui'), root=ROOT) for name in selection['selected']}
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
            raise RuntimeError('Build ordinary/UI engines first, so both can be backed up')
    backup.mkdir()
    # 注意不能把这个局部变量叫 paths —— 它会遮蔽模块级的 `from . import paths`
    # （Python 会把整个函数体内的 paths 都当局部名，导致上面的 paths.engine_binary 报
    # UnboundLocalError）。这里叫 artifacts。
    artifacts = [p for binary in targets.values() for p in (binary,Path(str(binary)+'.build.json'))]
    profile_path=build_settings.config_path(ROOT)
    if profile_path.exists():
        old=run/'build_config.before.json'
        published=run/'build_config.selected.json'
        is_published=(published.exists() and json.loads(profile_path.read_text(encoding='utf8'))==
                      json.loads(published.read_text(encoding='utf8')))
        shutil.copy2(old if old.exists() and is_published else profile_path,backup/profile_path.name)
    else:save(backup/profile_path.name,build_settings.fresh())
    artifacts.append(profile_path)
    for p in artifacts:
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
        save(state,{os.path.relpath(p,ROOT):digest(p) for p in artifacts})
    except BaseException:
        for p in artifacts:
            shutil.copy2(backup/p.name,p)
        raise
    # 生成一个"以此组合启动 UI"的便捷脚本。入口是仓库根的薄启动器 main.py/ui.py。
    (run/'start_tuned_ui.bat').write_text('@echo off\nchcp 65001 >nul\nsetlocal\ncd /d "'+str(ROOT)+'"\nset "PYTHONHOME="\nset "PYTHONPATH="\nset "PYTHONNOUSERSITE=1"\nset "PBB_BUILD_MODE=native"\nset "PBB_CXXFLAGS="\n"'+sys.executable+'" ui.py\n',encoding='utf8')
    print('Installed. Normal UI/Rebuild now reads build_config.json.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['quick','full'],default='quick')
    config_root=build_settings.config_root(ROOT)
    default_config=config_root/'config.yaml'
    if not default_config.is_file():default_config=config_root/'config_football.yaml'
    parser.add_argument('--config',default=str(default_config))
    parser.add_argument('--target',choices=['ui','standard'],default='ui')
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

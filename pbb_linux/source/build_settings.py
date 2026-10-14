"""Shared, host-local build choices. Portable distribution always starts fresh."""
import hashlib
import json
import os
from pathlib import Path

DEFAULTS = dict(lto=True, ksa=True, attr=True, copy=True, unroll=True,
                avx2=False, filter=0, prefer256=False)


def fresh():
    return dict(version=1, tested=False, status='none', options=DEFAULTS.copy(),
                machine=None, compiler=None, source_hashes={}, result=None)


def package_root(base):
    base=Path(base)
    if base.name=='_internal':
        app=base.parent
        return app.parent if (app.parent/'source').is_dir() else app
    if base.name=='source' and ((base.parent/'PBBMonitor').is_dir() or (base.parent/'config').is_dir()):
        return base.parent
    return base


def config_root(base):
    base=Path(base)
    outer=package_root(base)
    return outer/'config' if outer!=base or (outer/'config').is_dir() else outer


def config_path(base):
    return config_root(base)/'build_config.json'


def write(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    os.replace(temp,path)


def load(base, machine, compiler=None):
    path=config_path(base)
    if not path.exists():return fresh(),False
    value=json.loads(path.read_text(encoding='utf8'))
    if value.get('version')!=1 or type(value.get('tested')) is not bool:
        raise ValueError('Invalid build_config.json schema')
    options=value.get('options',{})
    if set(options)!=set(DEFAULTS):raise ValueError('Invalid build options')
    for key in DEFAULTS:
        if key=='filter':
            if type(options[key]) is not int or not 0<=options[key]<=4:raise ValueError('Invalid filter recipe')
        elif type(options[key]) is not bool:raise ValueError('Invalid boolean option: '+key)
    active=value['tested'] and value.get('status')=='complete' and value.get('machine')==machine
    if active and compiler is not None:
        active=os.path.normcase(os.path.abspath(compiler))==os.path.normcase(os.path.abspath(value.get('compiler') or ''))
    if active:
        sources=value.get('source_hashes',{})
        active=bool(sources)
        for rel,expected in sources.items():
            path=(Path(base)/rel).resolve()
            if not path.is_relative_to(Path(base).resolve()):raise ValueError('Unsafe source path in build config')
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                active=False;break
    return value,active


def signature(value, active):
    return hashlib.sha256(json.dumps(value['options'],sort_keys=True).encode()).hexdigest() if active else None


def apply_flags(base, options):
    flags=[f for f in base if f not in ('-flto','-funroll-loops','-fno-unroll-loops')
           and not f.startswith(('-DPBB_KSA_SEGMENTED=','-DPBB_ATTR_MINMAX=',
               '-DPBB_ENCODING_COPY=','-DPBB_ATTR_FILTER_MODE=','-mprefer-vector-width='))]
    flags += ['-funroll-loops' if options['unroll'] else '-fno-unroll-loops']
    if options['lto']:flags += ['-flto']
    flags += [f'-DPBB_KSA_SEGMENTED={int(options["ksa"])}',f'-DPBB_ATTR_MINMAX={int(options["attr"])}',
              f'-DPBB_ENCODING_COPY={int(options["copy"])}',f'-DPBB_ATTR_FILTER_MODE={options["filter"]}']
    if options['prefer256']:flags += ['-mprefer-vector-width=256']
    if options['avx2']:
        flags=[f for f in flags if not f.startswith('-mavx512')]
        flags += ['-mno-avx512f','-mno-avx512bw','-mno-avx512vl','-mno-avx512vbmi','-mno-avx512vbmi2','-mavx2']
    return flags


def distribution_config():
    # Called by PyInstaller specs; never bundles the developer's tested profile.
    path=Path('build/distribution_defaults/build_config.json')
    write(path,fresh())
    return str(path)

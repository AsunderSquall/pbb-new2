"""Shared, host-local build choices. Portable distribution always starts fresh.

中文: 构建选项的读写模块 —— 本机测速结果(config/build_config.json)的结构定义、
schema 校验与"这份选择现在是否仍然有效"的判定。
要点: 选择绑定 机器指纹 + 编译器路径 + 源码哈希, 换机器/换编译器/改源码即失效;
发布包永远只写 fresh()(未测试默认值), 绝不带走打包机器的调优结果。
"""
import hashlib
import json
import os
from pathlib import Path

from . import paths

# 默认构建选项(与 config/build_config.fresh.json 的 options 保持一致)。
# 键集是固定的, load() 会据此逐项校验; 含义:
#   lto      -flto;  ksa 分段 KSA;  attr 属性 min/max;  copy 编码复制;
#   unroll  循环展开;  avx2=True 表示强制禁用 AVX-512 退回 AVX2;
#   filter  过滤配方编号 0..4(0=原版, 其余见 src/utils.hpp PBB_ATTR_FILTER_MODE);
#   prefer256 限制向量宽度为 256 位。
DEFAULTS = dict(lto=True, ksa=True, attr=True, copy=True, unroll=True,
                avx2=False, filter=0, prefer256=False)


def fresh():
    # 未测试的全新配置: tested=False / status='none' 表示"没有有效选择",
    # 构建方(build/autotune)据此回退到默认参数。
    return dict(version=1, tested=False, status='none', options=DEFAULTS.copy(),
                machine=None, compiler=None, source_hashes={}, result=None)


def package_root(base):
    # 把任意起点归一到"外层包根"——即 config/、runtime/、PBBMonitor/ 所在的那一级。
    #
    # 三种起点都要能识别：
    #   开发仓库    ：config/ 就在仓库根，包根 = 仓库根（paths.DATA_ROOT）。
    #   发布包源码  ：源码在 <包根>/source/，config/ 与 PBBMonitor/、runtime/ 在包根，
    #                 所以 source/ 的上一级才是包根。
    #   冻结的监控器：起点是 PyInstaller 的 <包根>/PBBMonitor/_internal/，需要上浮两层。
    # 判据统一为"这一级是不是源码根"（有没有 pbb/ 包或 config/），命中即返回。
    base = Path(base)
    if base.name == "_internal":
        # PyInstaller onedir：_internal 的上一级是 PBBMonitor/，再上一级才是包根。
        return base.parent.parent
    if base.name == "source" and (base / "pbb").is_dir():
        return base.parent
    return base


def config_root(base):
    # config/ 的位置：包根下的 config/；若包根本身没有 config/（开发仓库尚未建立
    # config 目录的兼容情况），就用包根本身。
    base = Path(base)
    outer = package_root(base)
    return outer / "config" if (outer / "config").is_dir() else outer


def config_path(base):
    # 构建选项的唯一文件名; 开发仓库与发布包都叫 build_config.json。
    return config_root(base)/'build_config.json'


def write(path, value):
    # 原子写入: 先写同目录的 .tmp 再 os.replace 覆盖。测速/打包可能并发读取,
    # 直接原地写会让人读到半截 JSON。
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    os.replace(temp,path)


def load(base, machine, compiler=None):
    # 读取本机构建选项, 返回 (配置内容, 是否有效)。
    # 无效时调用方一律按默认参数处理(不是错误, 只是"这份选择不能用")。
    # 有效性判定链, 任一环不满足即失效:
    #   1) 文件不存在 → 全新配置;
    #   2) schema: version 必须为 1、tested 必须是 bool、options 键集与 DEFAULTS 完全一致、
    #      各类型正确(filter 限定 0..4) —— 不合法直接抛错, 避免静默用错参数;
    #   3) tested 为真 且 status=='complete' 且 machine 指纹一致(换机器失效);
    #   4) 传入 compiler 时要求规范化路径完全相同(换编译器失效);
    #   5) source_hashes 非空且每个文件的 sha256 都对得上(改源码失效);
    #      同时拒绝越出 base 的路径, 防止配置里的相对路径指向仓库外。
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
    # 只有"已测试完成 + 同一台机器"的记录才算有效选择。
    active=value['tested'] and value.get('status')=='complete' and value.get('machine')==machine
    if active and compiler is not None:
        # 路径按 normcase 比较: Windows 上大小写与斜杠写法不同也视为同一个编译器。
        active=os.path.normcase(os.path.abspath(compiler))==os.path.normcase(os.path.abspath(value.get('compiler') or ''))
    if active:
        # 逐个核对被记录源码的内容哈希; 任一文件被改或删除即整体失效。
        sources=value.get('source_hashes',{})
        active=bool(sources)
        for rel,expected in sources.items():
            path=(Path(base)/rel).resolve()
            if not path.is_relative_to(Path(base).resolve()):raise ValueError('Unsafe source path in build config')
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                active=False;break
    return value,active


def signature(value, active):
    # 只取 options 做哈希, 因此"其它字段变了但编译参数没变"不会导致二进制重建;
    # active 为假时返回 None, 表示本次构建不采用这份选择(build.py 据此判定)。
    return hashlib.sha256(json.dumps(value['options'],sort_keys=True).encode()).hexdigest() if active else None


def apply_flags(base, options):
    # 按选项重写影响代码生成的参数: 先剔除所有可能被选项推翻的旧参数
    # (-flto/-funroll-loops 及全部 -DPBB_* 功能宏、-mprefer-vector-width),
    # 再按选项统一追加。因为总是先清后加, 结果与旧参数的来源/顺序无关,
    # 重复调用也不会叠加出矛盾的 -D 定义。
    flags=[f for f in base if f not in ('-flto','-funroll-loops','-fno-unroll-loops')
           and not f.startswith(('-DPBB_KSA_SEGMENTED=','-DPBB_ATTR_MINMAX=',
               '-DPBB_ENCODING_COPY=','-DPBB_UI_ENGINE=','-DPBB_ATTR_FILTER_MODE=','-mprefer-vector-width='))]
    flags += ['-funroll-loops' if options['unroll'] else '-fno-unroll-loops']
    if options['lto']:flags += ['-flto']
    flags += [f'-DPBB_KSA_SEGMENTED={int(options["ksa"])}',f'-DPBB_ATTR_MINMAX={int(options["attr"])}',
              f'-DPBB_ENCODING_COPY={int(options["copy"])}',f'-DPBB_ATTR_FILTER_MODE={options["filter"]}']
    if options['prefer256']:flags += ['-mprefer-vector-width=256']
    if options['avx2']:
        # 先禁掉 AVX-512 各扩展开关再加 -mavx2, 保证在只支持 AVX2 的机器上既能编也能跑。
        flags=[f for f in flags if not f.startswith('-mavx512')]
        flags += ['-mno-avx512f','-mno-avx512bw','-mno-avx512vl','-mno-avx512vbmi','-mno-avx512vbmi2','-mavx2']
    return flags


def distribution_config():
    # Called by PyInstaller specs; never bundles the developer's tested profile.
    # 中文: 供 PyInstaller spec 调用, 无条件写出 fresh() —— 发布包必须携带未测试的
    # 默认配置, 不能把打包机器的测速选择带出去; 返回 spec 需要的路径字符串。
    # 注意：相对路径会随调用者的 cwd 漂移，所以显式写到 build/ 下。
    path = paths.BUILD_DIR / "distribution_defaults" / "build_config.json"
    write(path, fresh())
    return str(path)

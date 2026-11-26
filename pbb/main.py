#!/usr/bin/env python3
"""
PBB 名字评分测号器 — Python 编排层.

职责:
  1. 解析配置文件 (JSON/YAML/TOML 三格式) + 命令行覆盖
  2. 校验配置合法性
  3. 调用 engine.run() 启动 C++ 子进程
  4. 统计并输出结果

编译由 build.py 负责, 依赖由 run.sh/run.bat 安装到 .venv。

用法:
  ./run.sh -c config.yaml --threads 8    # Linux/Termux
  run.bat -c config.yaml                 # Windows
  python3 main.py -c config.yaml         # 直接调用
  python3 main.py                         # 自动查找 config.json/yaml/toml

config → engine 键名映射: 见 config_schema.py (CONFIG_MAP)，唯一定义处。
新增配置字段只需在 CONFIG_MAP 加一行，main.py 和 engine.py 自动同步。
"""
import argparse, copy, os, sys, time, uuid, unicodedata
from datetime import datetime
from typing import Any, Optional

from ._build import ensure_all         # 编译模块
from .engine import EngineError, _custom_bytes, _plan_families, _split_trailing_ints, run as run_engine, validate_charset
from .config_file import load_config
from .config_schema import (           # config→engine 映射唯一真相源
    CONFIG_MAP, read_config, engine_default,
)

# 仓库根（= 数据根）：config/ 的定位以它为基准。真实定义在 pbb/paths.py，
# 这里保留 BASE_DIR 这个名字是为了让 config_root(BASE_DIR) 这类调用保持可读。
from . import autostart, build_settings, paths

BASE_DIR = str(paths.DATA_ROOT)

# ══════════════════════════════════════════════════════════════════════════════
# 断点续测 (--resume)
# ══════════════════════════════════════════════════════════════════════════════

def _build_charset_entries(cs: dict) -> tuple[list[bytes], int]:
    """构建字符集条目列表 (每个条目 scl 字节), 与引擎完全一致.

    Returns (entries, scl) 或 ([], 0) 表示失败.
    纯 Python 实现, 不依赖 pbb_core.

    只服务断点续测：要把结果文件里的「名字」反解回全局编号，必须先知道枚举单元的
    字节表。能用纯 Python 直接算的（数字/大小写英文/自定义/兼容 ASCII）就地算；
    其余依赖 pbb_core 数据表或 Unicode 区间的类型，一律转交 _build_via_engine()
    去调 pbb_core 拿同一张表，保证与真实引擎的编号一致 —— 编号一旦错位，
    续测就会从错误的位置开始。
    """
    scl = cs.get("single_char_length", 0)
    if scl < 1:
        return [], 0

    types = cs.get("types", [])
    custom = cs.get("custom_values")
    buf = bytearray()

    for t in types:
        if scl == 1:
            if t == 1:   buf.extend(b'0123456789')
            elif t == 2: buf.extend(b'abcdefghijklmnopqrstuvwxyz')
            elif t == 3: buf.extend(b'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
            elif t in (4, 5): buf.extend(_custom_bytes(custom))

        elif scl == 2:
            if t == 7: buf.extend(_custom_bytes(custom))
            else:
                return _build_via_engine(cs, scl)

        elif scl == 3:
            if t == 7: buf.extend(_custom_bytes(custom))
            else:
                return _build_via_engine(cs, scl)

        elif scl == 4:
            if t == 2: buf.extend(_custom_bytes(custom))
            else:
                return _build_via_engine(cs, scl)

        else:  # scl >= 5
            if t == 1: buf.extend(_custom_bytes(custom))
            else:
                return _build_via_engine(cs, scl)

    if len(buf) == 0:
        return [], 0

    entries = []
    for i in range(0, len(buf), scl):
        entries.append(bytes(buf[i:i + scl]))
    return entries, scl


def _build_via_engine(cs: dict, scl: int) -> tuple[list[bytes], int]:
    """回退: 通过 engine/pbb_core 构建 charset (用于内置汉字/字母表)."""
    try:
        from .engine import _build_charset as build_hex, _pbb_core
        _pbb_core()
        hex_str = build_hex(cs)
        raw = bytes.fromhex(hex_str)
    except Exception as e:
        print(f"[resume] 构建复杂 charset 失败: {e}", file=sys.stderr)
        return [], 0

    entries = []
    for i in range(0, len(raw), scl):
        entries.append(raw[i:i + scl])
    return entries, scl


def _parse_name(name: str, prefixes: list, suffixes: list,
                team_name: str, charset_entries: list[bytes],
                vlen: int, scl: int) -> tuple[int, int] | None:
    """从完整名字解析 (前缀索引, 数值).

    格式: {prefix}{variable_bytes}{suffix}@{team_name}
    variable_bytes = vlen * scl 字节 (每个"字符" scl 字节).
    """
    team_suffix = f"@{team_name}"
    if not name.endswith(team_suffix):
        return None
    name = name[:-len(team_suffix)]

    name_bytes = name.encode('utf-8')

    expected = vlen * scl
    indexes = {}
    for i, entry in enumerate(charset_entries):
        indexes.setdefault(entry, i)
    match = None
    for pi, prefix in enumerate(prefixes):
        suffix = suffixes[pi % len(suffixes)] if suffixes else ''
        left = b'' if prefix in ('', '+') else prefix.encode('utf-8')
        right = b'' if suffix in ('', '+') else suffix.encode('utf-8')
        if len(name_bytes) != len(left) + expected + len(right):
            continue
        if not name_bytes.startswith(left) or not name_bytes.endswith(right):
            continue
        value = 0
        for pos in range(len(left), len(left) + expected, scl):
            digit = indexes.get(name_bytes[pos:pos + scl])
            if digit is None:
                break
            value = value * len(charset_entries) + digit
        else:
            if match is not None:
                return None  # 同一个名字对应多个前缀，无法安全判断续测位置。
            match = (pi, value)
    return match


def _read_last_result(result_file: str, out_dir: str = ".", *, score_columns=None) -> str | None:
    """读取结果文件最后一行的名字. 引擎写 out/ 前缀.
    名字可能含空格; output_xp=0 行=名字, output_xp=1 行="名字 sc..." (列数随 score_list 可变).
    """
    path = os.path.join(out_dir, "out", result_file)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return None
            chunk = max(size - 8192, 0)
            f.seek(chunk)
            data = f.read()
        lines = data.decode("utf-8", errors="replace").split('\n')
        for line in reversed(lines):
            line = line.rstrip('\r')
            if not line:
                continue
            return _split_trailing_ints(line, score_columns)[0]
        return None
    except Exception as e:
        print(f"[resume] WARNING: 无法读取 {path}: {e}", file=sys.stderr)
        return None


def _try_resume(cfg: dict, result_file: str, out_dir: str = '.') -> tuple[dict, str] | None:
    """尝试从已有结果文件断点续测.

    返回 (修改后的 cfg, 描述字符串) 或 None.
    不修改原始 cfg, 返回新的副本.

    格式配置使用每前缀连续完成检查点；旧配置保持结果文件最后命中反推的语义。
    两种方式只对 mode=1 有意义，随机模式没有单调的扫描位置。
    """
    from .flex import normalize_config
    cfg = normalize_config(cfg)
    en = cfg["enumeration"]
    if en.get("mode") != 1:
        print("[resume] 仅支持 mode=1 (顺序枚举)", file=sys.stderr)
        return None

    if 'format' in cfg:
        return _resume_flex(cfg, result_file, out_dir)

    cs = cfg["character_set"]
    charset_entries, scl = _build_charset_entries(cs)
    if not charset_entries:
        print("[resume] 无法构建 charset", file=sys.stderr)
        return None

    prefixes = [p["name"] for p in cfg.get("prefixes", [])]
    suffixes = [s["name"] for s in cfg.get("suffixes", [])]
    team_name = cfg.get("team_name", "")
    vlen = en.get("variable_length", 0)

    # 读最后一行
    score_columns = len(_plan_families(cfg.get('collection', {}))) if _read_cfg(cfg, 'output.output_xp') else 0
    last_name = _read_last_result(result_file, out_dir, score_columns=score_columns)
    if last_name is None:
        print("[resume] 结果文件不存在或为空, 全量开始", file=sys.stderr)
        return None

    parsed = _parse_name(last_name, prefixes, suffixes, team_name,
                         charset_entries, vlen, scl)
    if parsed is None:
        print(f"[resume] 无法解析最后结果: {last_name}", file=sys.stderr)
        return None

    pidx, last_val = parsed
    print(f"[resume] 最后结果: 前缀[{pidx}] \"{prefixes[pidx]}\"  值={last_val}", file=sys.stderr)

    # 新口径：resume 只把「全局 start」改到读到的位置，prefix_ranges 一个字都不动。
    # 命中的下一个候选就是续测起点；若该前缀已经跑完，to_global 会把它夹到前缀末尾
    # （即下一个前缀的开头）。
    from . import sequential
    next_val = last_val + 1
    start, end = sequential.global_bounds(cfg)
    new_start = max(start, min(end, sequential.to_global(cfg, pidx, next_val)))
    desc = (f"前缀[{pidx}] \"{prefixes[pidx]}\" 从 {next_val} 继续；"
            f"全局 start → {new_start}")
    print(f"[resume] {desc}", file=sys.stderr)

    import copy
    new_cfg = copy.deepcopy(cfg)
    ranges = new_cfg["enumeration"].setdefault("ranges", [{}])
    ranges[0] = dict(ranges[0] or {}, start=new_start)
    new_cfg["enumeration"]["mode"] = 1
    return new_cfg, desc


def _resume_flex(cfg, result_file, out_dir='.'):
    """只用连续完成检查点；旧结果文件不能证明多线程已完成的区间。"""
    from pathlib import Path
    from .flex import build_layout, resume_id
    from .engine import _build_charset
    import copy
    task = _build_task_config(cfg)
    template, slots = build_layout(task, _build_charset)
    path = Path(out_dir) / 'out' / (result_file + '.resume')
    if not path.exists():
        print('[resume] 格式任务没有连续完成检查点，从配置起点重测（可能重复，不跳过候选）', file=sys.stderr)
        return None
    lines = path.read_text(encoding='ascii').splitlines()
    if not lines or lines[0] != resume_id(task, template, slots):
        raise ValueError('续测检查点与当前字符集、格式或计算配置不一致，请恢复原配置/文件或换用新的结果文件')
    count = len(task['prefixes'])
    if len(lines) != count + 1:
        raise ValueError('续测检查点不完整')
    total = min(2**64-1, __import__('math').prod(len(d)//w for _, w, d in slots))
    # 新口径：检查点给出的是每个前缀的连续完成位置（绝对坐标）；把它折算回**全局
    # start**，prefix_ranges 保持用户填的样子不动。全局坐标按前缀顺序累加，遇到的
    # 第一个没跑完的前缀决定续测点。
    from . import sequential
    layout = sequential.prefix_layout(cfg)
    if len(layout) != count:
        raise ValueError('续测检查点与前缀数量不一致')
    global_start, global_end = sequential.global_bounds(cfg)
    prefix_base = 0
    for index, line in enumerate(lines[1:]):
        original, frontier, limit = map(int, line.split())
        origin, bound = layout[index]
        end = total if bound == -1 else min(total, bound)
        # limit 是上次运行时该前缀的区间上界；它比当前布局还宽，说明检查点不是这份
        # 配置跑出来的 —— 宁可报错，也不要拿它去算一个不可信的起点。
        if not 0 <= original <= frontier <= limit <= total or limit > end:
            raise ValueError('续测区间超出检查点覆盖范围，请使用原区间或其子区间')
        prefix_end = prefix_base + max(0, end - origin)
        if global_start < prefix_end:
            position = origin + (global_start - prefix_base)
            if original <= position <= frontier:
                global_start = prefix_base + frontier - origin
            # 上次预算可能在这个前缀中途用完。只有抵达布局终点才能继续下一前缀。
            if global_start < prefix_end:
                break
        prefix_base = prefix_end
        if global_start >= global_end:
            break
    result = copy.deepcopy(cfg)
    ranges = result['enumeration'].setdefault('ranges', [{}])
    # 夹到全局 end：跑完最后一块时累计值可能越过 end（那时前缀区间被 end 截断过），
    # 但 start 绝不能大于 end —— 否则配置立刻变成"非法区间"，用户无法再编辑。
    ranges[0] = dict(ranges[0] or {}, start=min(global_start, global_end))
    desc = f'按各前缀连续完成检查点续测；全局 start → {ranges[0]["start"]}'
    print('[resume] ' + desc, file=sys.stderr)
    return result, desc


def _resolve_result_file(raw: str) -> str:
    """解析 result_file 配置值.

    +  → 随机 hex 名 (result_<8hex>.txt)
    -  → 时间戳名   (result_<YYYYMMDD_HHMMSS>.txt)
    其他 → 原样使用
    自动去除前导 out/ (引擎内部已加)
    """
    _check_result_file(raw)
    raw = raw.strip()
    if raw == "+":
        resolved = f"result_{uuid.uuid4().hex[:8]}.txt"
    elif raw == "-" or raw == "":
        resolved = f"result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    else:
        resolved = raw
    # 引擎内部统一加 out/ 前缀, 此处去除避免双写
    if resolved.startswith("out/") or resolved.startswith("out\\"):
        resolved = resolved[4:]
    return resolved


def _check_result_file(value):
    if not isinstance(value, str):
        _die('output.result_file 必须为字符串')
    _check_name_text('output.result_file', value)
    from pathlib import PureWindowsPath
    name = value.strip()
    if name in ('', '+', '-'):
        return
    normalized = name.replace('\\', '/')
    if normalized.startswith('out/'):
        normalized = normalized[4:]
    parts = normalized.split('/')
    if PureWindowsPath(normalized).drive or any(part in ('', '.', '..') for part in parts):
        _die('output.result_file 必须是 out/ 下的相对文件路径')
    if os.name == 'nt' and any(
            PureWindowsPath(part).is_reserved() or part.endswith((' ', '.'))
            or any(char in '<>:"|?*' or ord(char) < 32 for char in part)
            for part in parts):
        _die('output.result_file 包含 Windows 不允许的文件名或字符')


def _print_results(results: list, output_xp: int):
    """将结果列表打印到 stdout (格式与输出文件一致)."""
    for r in results:
        if output_xp:
            print(f"{r['name']} " + " ".join(str(v) for v in r["sc"]))
        else:
            print(r["name"])


class ConfigError(Exception):
    """配置校验失败。

    **为什么不用 SystemExit**：``_validate_config`` 同时被 CLI 和图形监控器调用。
    早期实现里 ``_die()`` 直接 ``sys.exit(1)``（抛 ``SystemExit``），而 ``SystemExit``
    继承自 ``BaseException``，图形界面各处的 ``except Exception`` 都拦不住它 ——
    结果是「保存配置时校验不通过」会直接把监控器进程干掉（表现为闪退、无任何提示），
    而不是弹一个错误框。

    改成抛本异常（继承 ``Exception``）后：
      - CLI 在 main() 的边界统一捕获并转成退出码 1，行为与以前一致；
      - GUI 各处既有的 ``except Exception`` 自然能捕获并弹窗，信息也能完整显示。
    """


def _die(msg: str):
    """配置校验失败的统一出口：抛出 ``ConfigError``。

    只抛不打印 —— 打印交给各自的边界处理（CLI 在 ``__main__`` 里打印一次并退出码 1；
    图形界面由调用处的 ``except`` 弹窗展示），避免同一条信息被打印两遍。
    """
    raise ConfigError(msg)


# 结果文件直接按行写入名字，前后缀传参采用十六进制后仍需拒绝控制字符。
_UNSAFE_NAME_CHARS = {'\n': '换行', '\r': '回车', '\t': '制表符', '\0': 'NUL'}


def _check_name_text(where: str, value: str) -> None:
    """拒绝会破坏参数协议/结果文件行结构、且无法转义的字符。"""
    for char, label in _UNSAFE_NAME_CHARS.items():
        if char in value:
            _die(f"{where} 不能包含{label}，否则名字可能截断或破坏结果文件的行结构。"
                 f"当前值为 {value!r}")
    try:
        value.encode('utf-8')
    except UnicodeError:
        _die(f'{where} 包含无法编码为 UTF-8 的字符，当前值为 {value!r}')


def _name_byte_lengths(cfg: dict) -> tuple[int, int]:
    # 按 UTF-8 字节计算；“+”是空前后缀标记，战队名不属于这里的名字长度。
    if 'format' in cfg:
        families = cfg['character_sets']
        variable = sum(families[item['charset']]['single_char_length']
                       for item in cfg['format'] if isinstance(item, dict))
        fixed = sum(len(item.encode('utf8')) for item in cfg['format'] if isinstance(item, str))
    else:
        variable = cfg['character_set']['single_char_length'] * cfg['enumeration']['variable_length']
        fixed = 0
    affixes = sum(max((len(item['name'].encode('utf8')) if item['name'] != '+' else 0
                      for item in cfg.get(key, [{'name': '+'}])), default=0)
                  for key in ('prefixes', 'suffixes'))
    return variable, variable + fixed + affixes


def _config_warnings(cfg: dict) -> list[str]:
    # 提示与硬校验分开：打开编辑器不弹窗，保存或启动时由界面统一提示。
    messages = []
    enumeration = cfg.get('enumeration')
    if isinstance(enumeration, dict) and isinstance(enumeration.get('ranges'), list):
        if len(enumeration['ranges']) > 1:
            messages.append('enumeration.ranges 目前只使用第一个区间，后续区间不会参与枚举。')
        if enumeration.get('mode') != 1 and enumeration.get('prefix_ranges'):
            messages.append('随机模式使用逐前缀区间，本次不使用全局区间，也不会回写全局已测数量。')
    collection = cfg.get('collection', {})
    if isinstance(collection, dict) and isinstance(collection.get('score_list', ''), (str, list)):
        if not _plan_families(collection):
            messages.append('评分族为空，测号文件不会产生任何输出！')
    try:
        variable, total = _name_byte_lengths(cfg)
        if variable > 64 and total <= 256:
            messages.append(f'随机部分较长（{variable} 字节，超过 64 字节），仍可继续保存和运行。')
    except (KeyError, TypeError, ValueError):
        pass  # 不完整配置交给硬校验报告，不用警告掩盖真正的错误。
    families = cfg.get('character_sets', {}) if 'format' in cfg else {'默认': cfg.get('character_set', {})}
    if not isinstance(families, dict):
        return messages
    for name, cs in families.items():
        if not isinstance(cs, dict) or type(cs.get('single_char_length')) is not int:
            continue
        custom_types = {1: (4, 5), 2: (7,), 3: (7,), 4: (2,)}.get(cs['single_char_length'], (1,))
        types = cs.get('types', [])
        if not isinstance(types, list) or not any(kind in custom_types for kind in types):
            continue
        custom = cs.get('custom_values', '')
        if isinstance(custom, list):
            try:
                custom = bytes(custom).decode('utf8')
            except (TypeError, ValueError):
                messages.append(f'自定义字符中出现非法字符（{name}）：字节列表不是合法的 UTF-8 文本。')
                continue
        if not isinstance(custom, str):
            continue
        invalid = list(dict.fromkeys(
            ord(char) for char in custom
            if char.isspace() or unicodedata.category(char) in ('Cc', 'Cf', 'Cs', 'Cn')
            or ord(char) in (0x034F, 0x115F, 0x1160, 0x2800, 0x3164, 0xFFA0)
            or 0xFE00 <= ord(char) <= 0xFE0F or 0xE0100 <= ord(char) <= 0xE01EF))
        if invalid:
            codes = '、'.join(f'U+{cp:04X}' for cp in invalid[:8])
            messages.append(f'自定义字符中出现非法字符（{name}）：{codes}。包含不可见字符或无效 Unicode 字符，请检查。')
    return messages


def _validate_config(cfg: dict):
    """校验配置字段合法性, 不合法立即退出.

    使用 CONFIG_MAP 中的 config_path 读取值，发现命名不一致时直接报错。
    """
    from .flex import normalize_config
    try:
        cfg = normalize_config(cfg)
    except ValueError as exc:
        _die(str(exc))
    for field in ('enumeration', 'collection', 'output', 'threads'):
        if not isinstance(cfg.get(field, {}), dict):
            _die(f'{field} 必须为表')
    # ── debug_mode ──
    dm = _read_cfg(cfg, "debug_mode")
    if type(dm) not in (bool, int) or dm not in (0, 1):
        _die(f"debug_mode 必须为 0 或 1, 当前: {dm}")

    # ── team_name ──
    tn = _read_cfg(cfg, "team_name")
    if not tn or not isinstance(tn, str):
        _die("team_name 不能为空")
    _check_name_text("team_name", tn)

    # ── prefixes ──
    pfx = cfg.get("prefixes", [])
    if not isinstance(pfx, list) or not pfx:
        _die("prefixes 至少需要一个元素")
    for i, p in enumerate(pfx):
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            _die(f"prefixes[{i}].name 必须为字符串")
        _check_name_text(f"prefixes[{i}].name", p["name"])

    # ── suffixes ──
    sfx = cfg.get("suffixes", [])
    if not isinstance(sfx, list) or not sfx:
        _die("suffixes 至少需要一个元素")
    for i, s in enumerate(sfx):
        if not isinstance(s, dict) or not isinstance(s.get("name"), str):
            _die(f"suffixes[{i}].name 必须为字符串")
        _check_name_text(f"suffixes[{i}].name", s["name"])

    # ── character_set ──
    try:
        validate_charset(cfg.get('character_set'))
    except ValueError as exc:
        _die(str(exc))

    # ── enumeration ──
    en = cfg.get("enumeration", {})
    mode = en.get("mode")
    if type(mode) is not int or mode not in (1, 2, 3, 4):
        _die(f"enumeration.mode 必须为 1/2/3/4, 当前: {mode}")

    vlen = en.get("variable_length")
    if type(vlen) is not int or vlen < 1:
        _die(f"enumeration.variable_length 必须为正整数, 当前: {vlen}")

    _variable_bytes, name_bytes = _name_byte_lengths(cfg)
    if name_bytes > 256:
        _die(f"名字过长：随机部分加固定内容及最长前后缀共 {name_bytes} 字节，不能超过 256 字节。")

    ranges = en.get("ranges", [])
    if not isinstance(ranges, list) or not ranges:
        _die("enumeration.ranges 至少需要一个区间")
    for i, r in enumerate(ranges):
        if not isinstance(r, dict):
            _die(f'enumeration.ranges[{i}] 必须为表')
        start = r.get("start", 0)
        end = r.get("end", int(1e18))
        if type(start) is not int or not 0 <= start < 2**64:
            _die(f"enumeration.ranges[{i}].start 必须为 0 到 2^64-1 之间的整数，当前为 {start!r}")
        # end == start 是合法的「空区间」：一个区间测完之后「自动更新 start」正好把
        # start 推到 end，旧规则此时会让配置变成"无法编辑/无法打开"（end 必须 > start）。
        if type(end) is not int or (end != -1 and not start <= end < 2**64):
            _die(f"enumeration.ranges[{i}].end 必须 >= start 且不超过 2^64-1（或填 -1），当前为 {end!r}")

    # ── enumeration.prefix_ranges (可选: 按连续段分配前缀区间) ──
    pfx_count = len(cfg.get("prefixes", []))
    pr_cfg = en.get("prefix_ranges")
    if pr_cfg is not None:
        if not isinstance(pr_cfg, list) or not pr_cfg:
            _die("enumeration.prefix_ranges 必须为非空列表")
        total_cnt = 0
        for i, pr in enumerate(pr_cfg):
            if not isinstance(pr, dict):
                _die(f'enumeration.prefix_ranges[{i}] 必须为表')
            cnt = pr.get("count")
            if type(cnt) is not int or cnt < 1:
                _die(f"enumeration.prefix_ranges[{i}].count 必须为正整数, 当前: {cnt}")
            start = pr.get("start", 0)
            end = pr.get("end", int(1e18))
            if type(start) is not int or not 0 <= start < 2**64:
                _die(f"enumeration.prefix_ranges[{i}].start 必须为 0 到 2^64-1 之间的整数，当前为 {start!r}")
            if type(end) is not int or (end != -1 and not start <= end < 2**64):
                _die(f"enumeration.prefix_ranges[{i}].end 必须 >= start 且不超过 2^64-1（或填 -1），当前为 {end!r}")
            total_cnt += cnt
        if total_cnt != pfx_count:
            _die(f"enumeration.prefix_ranges count 总和 ({total_cnt}) != prefixes 数量 ({pfx_count})")

    # ── collection (使用 _read_cfg 统一读取) ──
    # 逐族评分阈值 (MNAME 序): min_qp/min_qd/min_pp/min_pd/min_cqd/min_cp/min_fp/min_wc/min_fs/min_pj
    for mk in ("min_qp", "min_qd", "min_pp", "min_pd", "min_cqd",
               "min_cp", "min_fp", "min_wc", "min_fs", "min_pj"):
        mv = _read_cfg(cfg, f"collection.{mk}")
        if type(mv) is not int or mv < 0 or mv > 9999:
            _die(f"collection.{mk} 必须为 0~9999, 当前: {mv}")

    # 逐族 ptt(potential) 阈值: 仅 SP2 族 cp/fp/wc/fs/pj 有定义
    for pk in ("ptt_cp", "ptt_fp", "ptt_wc", "ptt_fs", "ptt_pj"):
        pv = _read_cfg(cfg, f"collection.{pk}")
        if type(pv) is not int or pv < 0 or pv > 9999:
            _die(f"collection.{pk} 必须为 0~9999, 当前: {pv}")

    # ── collection.score_list / score_mode (限定计算哪些族 + 全算/短路) ──
    _VAL_TOK = {"qp", "qd", "pp", "pd", "cqd", "cp", "fp", "wc", "fs", "rh", "pj"}
    _MIN_BY_TOK = {"qp": "min_qp", "qd": "min_qd", "pp": "min_pp", "pd": "min_pd",
                   "cqd": "min_cqd", "cp": "min_cp", "fp": "min_fp", "wc": "min_wc",
                   "fs": "min_fs", "pj": "min_pj"}
    sl = _read_cfg(cfg, "collection.score_list")
    if isinstance(sl, str):
        sl_list = [t.strip() for t in sl.split(",") if t.strip()]
    elif isinstance(sl, list):
        sl_list = [str(t).strip() for t in sl if str(t).strip()]
    else:
        _die(f"collection.score_list 必须为字符串或列表, 当前: {sl!r}")
    for t in sl_list:
        if t not in _VAL_TOK:
            _die(f"collection.score_list 含未知族名 '{t}', 合法: {sorted(_VAL_TOK)}")
    sm = _read_cfg(cfg, "collection.score_mode")
    if sm not in ("all", "any"):
        _die(f"collection.score_mode 必须为 all 或 any, 当前: {sm!r}")
    if sl_list:
        norm = {"fs" if t == "rh" else t for t in sl_list}
        for tok, mk in _MIN_BY_TOK.items():
            mv2 = _read_cfg(cfg, f"collection.{mk}")
            if isinstance(mv2, int) and mv2 > 0 and tok not in norm:
                _die(f"collection.{mk}>0 但族 '{tok}' 不在 collection.score_list 中, "
                     f"该族不会被计算, 门控将失效")
        _PTT_BY_TOK = {"cp": "ptt_cp", "fp": "ptt_fp", "wc": "ptt_wc",
                       "fs": "ptt_fs", "pj": "ptt_pj"}
        for tok, pk in _PTT_BY_TOK.items():
            pv2 = _read_cfg(cfg, f"collection.{pk}")
            if isinstance(pv2, int) and pv2 > 0 and tok not in norm:
                _die(f"collection.{pk}>0 但族 '{tok}' 不在 collection.score_list 中, "
                     f"该族不会被计算, ptt 门控将失效")

    cm = _read_cfg(cfg, "collection.collect_mode")
    # 旧配置的自定义收集模式仍可打开，运行时按关闭处理。
    if type(cm) not in (bool, int) or cm not in (0, 1, 2):
        _die(f"collection.collect_mode 必须为 0 或 1, 当前: {cm}")

    # ── output (使用 _read_cfg 统一读取) ──
    _check_result_file(_read_cfg(cfg, 'output.result_file'))
    ox = _read_cfg(cfg, "output.output_xp")
    if type(ox) not in (bool, int) or ox not in (0, 1):
        _die(f"output.output_xp 必须为 0 或 1, 当前: {ox}")

    ol = _read_cfg(cfg, "output.log_output")
    if type(ol) not in (bool, int) or ol not in (0, 1):
        _die(f"output.log_output 必须为 0 或 1, 当前: {ol}")

    os_ = _read_cfg(cfg, "output.speed_output")
    if type(os_) not in (bool, int) or os_ not in (0, 1):
        _die(f"output.speed_output 必须为 0 或 1, 当前: {os_}")

    # ── threads (使用 _read_cfg 统一读取) ──
    wt = _read_cfg(cfg, "threads.worker_threads")
    if type(wt) is not int or wt < -1 or wt == 0 or wt > 256:
        _die(f"threads.worker_threads 必须为 -1(自动) 或 1~256, 当前: {wt}")


# ── CONFIG_MAP 索引 (启动时构建, 避免每次遍历) ──────────────────────────────
_config_index: dict = {fd.config_path: fd for fd in CONFIG_MAP}


def _read_cfg(cfg: dict, config_path: str) -> Any:
    """从配置 dict 按 config_path 读取值, 使用 CONFIG_MAP 定义的默认值."""
    fd = _config_index.get(config_path)
    if fd is None:
        raise KeyError(f"未知 config_path: {config_path} (未在 CONFIG_MAP 中定义)")
    return read_config(cfg, fd)


def apply_range_overrides(cfg: dict, start=None, end=None) -> dict:
    """把 ``--range-start`` / ``--range-end`` 写到配置的**全局** ``ranges[0]`` 上。

    **为什么不能只改任务参数里的 range_L/range_R**：顺序模式（mode 1）下全局区间是
    总控制器，``_build_task_config`` 会把它映射成每个前缀的绝对区间（``prefix_range_*``）；
    映射发生在任务组装时，之后再改 ``range_L/range_R`` 引擎根本不看 —— 于是命令行
    指定的窗口被静默忽略，整段 4.4T 全扫（实测一个"2M 候选"的窗口跑了十几分钟）。
    随机模式仍然照旧用 ``range_L/range_R``，这里改全局区间对它们同样等价。
    """
    if start is None and end is None:
        return cfg
    ranges = cfg.setdefault('enumeration', {}).setdefault('ranges', [{}])
    if not ranges:
        ranges.append({})
    if start is not None:
        ranges[0]['start'] = int(start)
    if end is not None:
        ranges[0]['end'] = int(end)
    return cfg


def _build_task_config(cfg: dict) -> dict:
    """从配置 dict 构建 engine 层 task_config，统一使用 engine 键名.

    新增字段只需在 CONFIG_MAP 加一行，此函数自动包含。
    特殊字段 (character_set/prefixes/suffixes/ranges) 手工补充。
    """
    from .flex import normalize_config
    cfg = normalize_config(cfg)
    en = cfg["enumeration"]
    rng = en.get("ranges", [{}])[0]

    # ── 标量字段: 从 CONFIG_MAP 自动生成 ──
    tc: dict = {}
    for fd in CONFIG_MAP:
        value = read_config(cfg, fd)
        tc[fd.engine_key] = int(value) if isinstance(value, bool) else value
    if tc["collect_mode"] == 2:
        tc["collect_mode"] = 0

    # ── 手工字段: 需要变换的非标量 ──
    tc["character_set"] = cfg["character_set"]
    tc["prefixes"] = [p["name"] for p in cfg["prefixes"]]
    tc["suffixes"] = [s["name"] for s in cfg["suffixes"]]
    tc["scl"] = cfg["character_set"]["single_char_length"]
    if 'format' in cfg:
        tc['format'] = cfg['format']
        import copy
        from .flex import file_bytes
        tc['character_sets'] = copy.deepcopy(cfg['character_sets'])
        for name, cs in tc['character_sets'].items():
            if 'file' in cs:
                width = cs['single_char_length']
                tc['character_sets'][name] = {'single_char_length': width,
                    'types': [{1:4, 2:7, 3:7, 4:2}.get(width, 1)],
                    'custom_values': list(file_bytes(cs))}
    tc["range_L"] = rng.get("start", 0)
    tc["range_R"] = rng.get("end", int(1e18))

    # ── 顺序模式：全局区间是总控制器，前缀区间只是布局 ──
    # 把全局 [start, end) 按前缀顺序累加映射成各前缀的绝对区间（见 pbb/sequential.py）。
    # 映射结果只用于本次任务参数，**绝不写回配置** —— prefix_ranges 永远保持用户填的样子。
    # 没有 prefix_ranges 时布局默认"每个前缀 = 完整字符集"，所以全局 start 会落在第一个
    # 前缀里；这与"从全局 start 开始测"一致，也保证 resume 写回的全局坐标能被正确分发。
    mapped = None
    if tc["mode"] == 1:
        from . import sequential
        mapped = sequential.effective_prefix_ranges(cfg)
    if mapped is not None:
        tc["prefix_range_L"] = ",".join(str(low) for low, _high in mapped)
        tc["prefix_range_R"] = ",".join(str(high) for _low, high in mapped)
    else:
        # 退路：算不出总量（字符集表拿不到）时沿用配置里的区间，至少不静默跑空。
        pr_cfg = en.get("prefix_ranges")
        if pr_cfg:
            pfx_list = tc["prefixes"]
            total_pfx = len(pfx_list)
            range_L, range_R = [], []
            idx = 0
            for pr in pr_cfg:
                cnt = pr["count"]
                s = pr.get("start", 0)
                e = pr.get("end", int(1e18))
                if e == -1:
                    e = 2**64 - 1  # 与普通 ranges 一致：-1 表示几乎无限
                for _ in range(cnt):
                    if idx >= total_pfx:
                        break
                    range_L.append(str(s))
                    range_R.append(str(e))
                    idx += 1
            # 校验已在 _validate_config 做过, 此处防御
            if idx != total_pfx:
                _die(f"prefix_ranges count 总和 ({idx}) != 前缀总数 ({total_pfx})")
            tc["prefix_range_L"] = ",".join(range_L)
            tc["prefix_range_R"] = ",".join(range_R)

    # result_file: 优先 CONFIG_MAP 默认 "result.txt"
    tc["result_file"] = tc.get("result_file", "result.txt")

    return tc


def main():
    # ── 1. 解析命令行 ──
    parser = argparse.ArgumentParser(description="PBB 名字评分测号器")
    parser.add_argument("-c", "--config", default=None,
                        help="配置文件路径 (默认: 界面设置的启动默认 → config.yaml → config_football.yaml)")
    parser.add_argument("--no-auto-start", action="store_true",
                        help="关闭「自动更新 start」（默认开启：每测 0.01T 把已处理量写回配置的全局 start）")
    parser.add_argument("--rebuild", action="store_true", help="强制重编 C++ 引擎")
    parser.add_argument("--threads", type=int, help="覆盖: threads.worker_threads (-1=自动)")
    parser.add_argument("--team", help="覆盖: team_name")
    parser.add_argument("--mode", type=int, help="覆盖: enumeration.mode (1=顺序 2/3/4=随机)")
    parser.add_argument("--vlen", type=int, help="覆盖: enumeration.variable_length")
    parser.add_argument("--range-start", type=int, help="覆盖: enumeration.ranges[0].start")
    parser.add_argument("--range-end", type=int, help="覆盖: enumeration.ranges[0].end")
    for _mk in ("qp", "qd", "pp", "pd", "cqd", "cp", "fp", "wc", "fs", "pj"):
        parser.add_argument(f"--min-{_mk}", type=int,
                            help=f"覆盖: collection.min_{_mk}")
    parser.add_argument("--collect-mode", type=int, choices=[0, 1], help="覆盖: collection.collect_mode")
    parser.add_argument("--output-xp", type=int, choices=[0,1], help="覆盖: output.output_xp")
    parser.add_argument("-o", "--output-file", help="覆盖: output.result_file (+ =随机, - =时间戳, 其他=自定义)")
    parser.add_argument("--output-dest", choices=["file", "stdout", "both"], default=None,
                        help="输出目标: file=仅文件(默认) stdout=仅终端 both=文件+终端")
    parser.add_argument("--debug", action="store_true", help="启用引擎 debug 诊断输出")
    parser.add_argument("--resume", action="store_true", help="断点续测: 从输出文件最后一行继续")
    parser.add_argument("--seed", type=int, help="覆盖: 随机种子 (mode 2/3/4 确定性随机)")
    parser.add_argument("--scl", type=int, help="覆盖: character_set.single_char_length (1~64)")
    parser.add_argument("--types", help="覆盖: character_set.types (逗号分隔, 如 1,2,3)")
    parser.add_argument("--custom-values", help="覆盖: character_set.custom_values (字符串)")
    args = parser.parse_args()

    config_path = args.config
    if config_path is not None and not os.path.isabs(config_path) and not os.path.exists(config_path):
        candidate = os.path.join(build_settings.config_root(BASE_DIR), config_path)
        if os.path.isfile(candidate):config_path = candidate
    if config_path is None:
        # 与界面同一口径：界面设置里的「启动默认」优先，其次 config.yaml /
        # config_football.yaml / 配置目录里第一个像测号配置的文件。
        default_path = build_settings.default_config_path(BASE_DIR)
        if default_path is None:
            print("ERROR: 未找到配置文件 (界面设置的启动默认 / config.yaml / config_football.yaml)", file=sys.stderr)
            print("  提示: 复制 config.example.* → config.* 并修改", file=sys.stderr)
            sys.exit(1)
        config_path = str(default_path)
        print(f"[main] Auto config: {config_path}", file=sys.stderr)
    elif not os.path.exists(config_path):
        print(f"ERROR: 配置文件不存在: {config_path}", file=sys.stderr)
        sys.exit(1)

    config_path = os.path.abspath(config_path)
    try:
        original = load_config(config_path)
    except Exception as exc:
        _die(f'无法读取配置 {config_path}：{exc}')
    _validate_config(original)
    cfg = copy.deepcopy(original)

    # 覆盖必须早于校验和区间映射，使校验和引擎看到同一份配置。
    if 'format' in cfg and any(value is not None for value in
                              (args.vlen, args.scl, args.types, args.custom_values)):
        _die('格式配置的位置与字符集由 format/character_sets 定义，不能用 --vlen/--scl/--types/--custom-values 覆盖')
    overrides = {
        'team_name': args.team,
        'debug_mode': 1 if args.debug else None,
        'enumeration.mode': args.mode,
        'enumeration.variable_length': args.vlen,
        'collection.collect_mode': args.collect_mode,
        'output.output_xp': args.output_xp,
        'output.result_file': args.output_file,
        'threads.worker_threads': args.threads,
        'character_set.single_char_length': args.scl,
    }
    for family in ('qp', 'qd', 'pp', 'pd', 'cqd', 'cp', 'fp', 'wc', 'fs', 'pj'):
        overrides[f'collection.min_{family}'] = getattr(args, f'min_{family}')
    for field, value in overrides.items():
        if value is None:
            continue
        section, sep, key = field.partition('.')
        if sep:
            cfg.setdefault(section, {})[key] = value
        else:
            cfg[section] = value
    if args.types is not None:
        try:
            cfg['character_set']['types'] = [int(x.strip()) for x in args.types.split(',')]
        except ValueError:
            _die('--types 需要用逗号分隔的整数，如 1,2,3')
    if args.custom_values is not None:
        value = args.custom_values
        if ',' in value:
            try:
                value = [int(x.strip()) for x in value.split(',')]
            except ValueError:
                pass
        cfg['character_set']['custom_values'] = value
    apply_range_overrides(cfg, args.range_start, args.range_end)
    _validate_config(cfg)
    if args.seed is not None and not -1 <= args.seed < 2**64:
        _die('--seed 必须为 -1 或 0 到 2^64-1 之间的整数')
    for warning in _config_warnings(cfg):
        print(f'[main] WARNING: {warning}', file=sys.stderr)
    if cfg['enumeration']['mode'] == 4 and len(cfg.get('prefixes', ['+'])) != len(cfg.get('suffixes', ['+'])):
        print('[main] WARNING: 配对模式的前后缀数量不同，后缀会按前缀序号循环配对。', file=sys.stderr)
    auto_update = not args.no_auto_start
    changed_task = (
        any(cfg.get(key) != original.get(key) for key in ('team_name', 'character_set', 'collection'))
        or any(cfg['enumeration'].get(key) != original['enumeration'].get(key)
               for key in ('mode', 'variable_length'))
    )
    if auto_update and changed_task:
        auto_update = False
        print('[auto-start] WARNING: 本次覆盖改变了待测任务，保留原配置的已测数量。', file=sys.stderr)
    if cfg['enumeration']['mode'] != 1 and cfg['enumeration'].get('prefix_ranges'):
        auto_update = False

    # 配置正确后才构建。正式入口一律使用正式引擎。
    os.environ.pop("PBB_SRC_DIR", None)
    engine_bin = ensure_all(rebuild=args.rebuild, verbose=args.debug)
    result_file = _resolve_result_file(_read_cfg(cfg, 'output.result_file'))
    if args.resume:
        try:
            resumed = _try_resume(cfg, result_file, out_dir=paths.PACKAGE_ROOT)
        except ValueError as exc:
            _die(f'续测失败：{exc}')
        if resumed is not None:
            cfg = resumed[0]
            apply_range_overrides(cfg, args.range_start, args.range_end)
            _validate_config(cfg)
    task_config = _build_task_config(cfg)
    if task_config['range_R'] == -1:
        task_config['range_R'] = 2**64 - 1
    if args.seed is not None:
        task_config['seed'] = args.seed

    # ── 5. 执行 ──
    from .threading_config import resolve_threads
    n = resolve_threads(task_config.get("n_threads", -1))
    task_config["n_threads"] = n

    # 输出目标: CLI > 默认 file
    output_dest = args.output_dest or "file"

    # stdout 模式: 使用临时目录 (引擎写文件, Python 读取后自动清理)
    if output_dest == "stdout":
        out_dir = None       # run() 内部创建 TemporaryDirectory, 用完即删
        print(f"[main] Threads: {n}, Mode: {task_config['mode']}, Output: stdout", file=sys.stderr)
    elif output_dest == "both":
        # 结果统一落在包根 out/（发布包就是 PBB/out），不再跟着启动目录跑。
        out_dir = str(paths.PACKAGE_ROOT)
        os.makedirs(paths.OUT_DIR, exist_ok=True)
        print(f"[main] Threads: {n}, Mode: {task_config['mode']}, "
              f"Output: file+stdout → {paths.OUT_DIR / result_file}", file=sys.stderr)
    else:  # file
        out_dir = str(paths.PACKAGE_ROOT)
        os.makedirs(paths.OUT_DIR, exist_ok=True)
        print(f"[main] Threads: {n}, Mode: {task_config['mode']}, "
              f"Output: {paths.OUT_DIR / result_file}", file=sys.stderr)

    t0 = time.time()
    # 「自动更新 start」默认开启：按 0.01T 阶梯把已处理量写回配置的全局 start，
    # 中途断电/崩溃最多只损失 0.01T；--no-auto-start 可关闭（见 autostart）。
    updater = autostart.StartUpdater(
        config_path, cfg, original_config=original, enabled=auto_update,
        notify=lambda message: print(f"[auto-start] {message}", file=sys.stderr))
    progress = {"done": 0}

    def _on_progress(done):
        progress["done"] = done
        updater.on_processed(done)

    try:
        result = run_engine(task_config, engine_bin, out_dir=out_dir, result_file=result_file,
                            on_progress=_on_progress)
    except KeyboardInterrupt:
        updater.finish(progress["done"])
        print("\n[main] Interrupted", file=sys.stderr)
        sys.exit(130)
    elapsed = time.time() - t0
    updater.finish(result.get("contiguous") or progress["done"])

    # ── 6. 输出 ──
    count = result["found"]
    speed = result["speed"]
    output_xp = task_config["output_xp"]

    # 终端打印结果 (stdout / both 模式)
    if output_dest in ("stdout", "both"):
        _print_results(result["results"], output_xp)

    print(f"\n[main] Done in {elapsed:.1f}s", file=sys.stderr)
    print(f"[main] Found: {count}, Max: XP={result['max_xp']} XD={result['max_xd']}", file=sys.stderr)
    if speed > 0:
        print(f"[main] Speed: {speed*86400/1e12:.4f}T/d ({speed/1e6:.2f}M/s)", file=sys.stderr)
    # 机器可读: benchmark.py 解析此行
    print(f"BENCHMARK speed={speed:.6f} found={count} elapsed={elapsed:.6f}", file=sys.stderr)


if __name__ == "__main__":
    # CLI 边界：把配置错误转成退出码 1（与旧的 sys.exit(1) 行为一致）。
    # 这样模块内部只需要 raise，不必关心"谁在调用我"。
    try:
        main()
    except (ConfigError, EngineError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

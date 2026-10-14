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
import json, os, sys, time, argparse, uuid, tempfile
from datetime import datetime
from typing import Any, Optional

from ._build import ensure_all         # 编译模块
from .engine import run as run_engine  # 引擎执行
from .config_schema import (           # config→engine 映射唯一真相源
    CONFIG_MAP, read_config, engine_default,
)

# 仓库根（= 数据根）：config/ 的定位以它为基准。真实定义在 pbb/paths.py，
# 这里保留 BASE_DIR 这个名字是为了让 config_root(BASE_DIR) 这类调用保持可读。
from . import build_settings, paths

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

    # 辅助: 解析 custom_values → bytes
    def _cv_bytes():
        if custom is None:
            return b''
        if isinstance(custom, str):
            return custom.encode('utf-8')
        if isinstance(custom, list) and custom:
            return bytes(int(x) & 0xFF for x in custom)
        return b''

    for t in types:
        if scl == 1:
            if t == 1:   buf.extend(b'0123456789')
            elif t == 2: buf.extend(b'abcdefghijklmnopqrstuvwxyz')
            elif t == 3: buf.extend(b'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
            elif t == 5: buf.extend(bytes(range(32, 127)))   # printable ASCII
            elif t in (4,): buf.extend(_cv_bytes())

        elif scl == 2:
            if t == 7: buf.extend(_cv_bytes())
            else:
                return _build_via_engine(cs, scl)

        elif scl == 3:
            if t == 7: buf.extend(_cv_bytes())
            else:
                return _build_via_engine(cs, scl)

        elif scl == 4:
            if t == 2: buf.extend(_cv_bytes())
            else:
                return _build_via_engine(cs, scl)

        else:  # scl >= 5
            if t == 1: buf.extend(_cv_bytes())
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

    # 匹配前缀 (最长匹配, 字节级)
    best_pi, best_plen = -1, -1
    for pi, pfx in enumerate(prefixes):
        if pfx in ("", "+"):
            plen = 0
        else:
            pfx_bytes = pfx.encode('utf-8')
            if not name_bytes.startswith(pfx_bytes):
                continue
            plen = len(pfx_bytes)
        if plen > best_plen:
            best_pi, best_plen = pi, plen
    if best_pi < 0:
        return None

    # 去掉前缀
    remaining = name_bytes[best_plen:]

    # 去掉后缀 (mode 1 中后缀与 prefix index 轮转配对)
    sfx = suffixes[best_pi] if best_pi < len(suffixes) else ""
    if sfx not in ("", "+"):
        sfx_bytes = sfx.encode('utf-8')
        if remaining.endswith(sfx_bytes):
            remaining = remaining[:-len(sfx_bytes)]

    # 提取变长部分 (前 vlen*scl 字节)
    expected = vlen * scl
    if len(remaining) < expected:
        return None
    var_bytes = remaining[:expected]
    if len(var_bytes) != expected:
        return None

    # 解码: 每 scl 字节查 charset 索引 (big-endian base-N)
    n = len(charset_entries)
    val = 0
    for i in range(0, expected, scl):
        chunk = var_bytes[i:i + scl]
        try:
            idx = charset_entries.index(chunk)
        except ValueError:
            return None
        val = val * n + idx

    return (best_pi, val)


def _read_last_result(result_file: str, out_dir: str = ".") -> str | None:
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
        lines = data.decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            # output_xp=1: "名字 sc..." → 名字可能含空格, 去掉末尾所有整数 token
            # (score_list 可只取部分族, 列数不固定; 名字含 '@team', 永不被当整数)
            rest = line
            while True:
                head, sep, tail = rest.rpartition(" ")
                if not sep:
                    break
                try:
                    int(tail)
                except ValueError:
                    break
                rest = head
            if rest:
                return rest  # 名字部分 (可能含空格)
            return line
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
    last_name = _read_last_result(result_file, out_dir) if out_dir != '.' else _read_last_result(result_file)
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

    # 解析原有 prefix_ranges 得到每前缀的原始 start/end
    pr_cfg = en.get("prefix_ranges")
    n_pfx = len(prefixes)
    orig_starts = [0] * n_pfx
    orig_ends = [int(1e18)] * n_pfx
    if pr_cfg:
        idx = 0
        for pr in pr_cfg:
            cnt = pr["count"]
            s = pr.get("start", 0)
            e = pr.get("end", int(1e18))
            if e == -1:
                e = 2**64 - 1
            for _ in range(cnt):
                if idx >= n_pfx:
                    break
                orig_starts[idx] = s
                orig_ends[idx] = e
                idx += 1
    else:
        # 无 prefix_ranges → 从 ranges 获取 start/end (兼容 -1 表示无限 / full charset)
        rng = en.get("ranges", [{}])[0]
        rng_start = rng.get("start", 0)
        rng_end = rng.get("end", int(1e18))
        if rng_end == -1:
            rng_end = 2**64 - 1
        for i in range(n_pfx):
            orig_starts[i] = rng_start
            orig_ends[i] = rng_end

    # 生成新的 prefix_ranges
    new_pr = []
    # 已完成的
    if pidx > 0:
        new_pr.append({"count": pidx, "start": 0, "end": 0})
    # 当前前缀
    next_val = last_val + 1
    if next_val >= orig_ends[pidx]:
        # 当前前缀也跑完了 → 跳过
        new_pr.append({"count": 1, "start": 0, "end": 0})
        desc = f"前缀[{pidx}] \"{prefixes[pidx]}\" 已完成, 跳到[{pidx+1}]"
        print(f"[resume] {desc}", file=sys.stderr)
    else:
        new_pr.append({"count": 1, "start": next_val, "end": orig_ends[pidx]})
        desc = f"前缀[{pidx}] \"{prefixes[pidx]}\" 从 {next_val} 继续 (共 ~{orig_ends[pidx]})"
        print(f"[resume] {desc}", file=sys.stderr)
    # 剩余: 保留每前缀原始 start/end (合并连续相同区间)
    for j in range(pidx + 1, n_pfx):
        s = orig_starts[j]
        e = orig_ends[j]
        if new_pr and new_pr[-1]["start"] == s and new_pr[-1]["end"] == e and new_pr[-1]["count"] > 0:
            new_pr[-1]["count"] += 1
        else:
            new_pr.append({"count": 1, "start": s, "end": e})

    # 复制 cfg 并替换 prefix_ranges + 清理 ranges
    import copy
    new_cfg = copy.deepcopy(cfg)
    new_cfg["enumeration"]["prefix_ranges"] = new_pr
    # ranges 在 mode=1+prefix_ranges 下不生效, 设为占位值避免混淆
    new_cfg["enumeration"]["ranges"] = [{"start": 0, "end": 0}]

    # 强制 mode=1 (加上以防修改)
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
    starts = list(map(int, task.get('prefix_range_L', '').split(','))) if 'prefix_range_L' in task else [task['range_L']] * count
    ends = list(map(int, task.get('prefix_range_R', '').split(','))) if 'prefix_range_R' in task else [task['range_R']] * count
    total = min(2**64-1, __import__('math').prod(len(d)//w for _, w, d in slots))
    ranges = []
    for i, line in enumerate(lines[1:]):
        original, frontier, limit = map(int, line.split())
        end = min(total, 2**64-1 if ends[i] == -1 else ends[i])
        start = min(starts[i], end)
        if not 0 <= original <= frontier <= limit <= total or start < original or end > limit:
            raise ValueError('续测区间超出检查点覆盖范围，请使用原区间或其子区间')
        ranges.append({'count': 1, 'start': min(end, max(start, frontier)), 'end': end})
    result = copy.deepcopy(cfg)
    result['enumeration']['prefix_ranges'] = ranges
    desc = '按各前缀连续完成检查点续测；未完成的块重新计算'
    print('[resume] ' + desc, file=sys.stderr)
    return result, desc


def _resolve_result_file(raw: str) -> str:
    """解析 result_file 配置值.

    +  → 随机 hex 名 (result_<8hex>.txt)
    -  → 时间戳名   (result_<YYYYMMDD_HHMMSS>.txt)
    其他 → 原样使用
    自动去除前导 out/ (引擎内部已加)
    """
    raw = raw.strip() if raw else ""
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


def _validate_config(cfg: dict):
    """校验配置字段合法性, 不合法立即退出.

    使用 CONFIG_MAP 中的 config_path 读取值，发现命名不一致时直接报错。
    """
    from .flex import normalize_config
    try:
        cfg = normalize_config(cfg)
    except ValueError as exc:
        _die(str(exc))
    # ── debug_mode ──
    dm = _read_cfg(cfg, "debug_mode")
    if dm not in (0, 1):
        _die(f"debug_mode 必须为 0 或 1, 当前: {dm}")

    # ── team_name ──
    tn = _read_cfg(cfg, "team_name")
    if not tn or not isinstance(tn, str):
        _die("team_name 不能为空")

    # ── prefixes ──
    pfx = cfg.get("prefixes", [])
    if not isinstance(pfx, list) or not pfx:
        _die("prefixes 至少需要一个元素")
    for i, p in enumerate(pfx):
        if not isinstance(p.get("name"), str):
            _die(f"prefixes[{i}].name 必须为字符串")

    # ── suffixes ──
    sfx = cfg.get("suffixes", [])
    if not isinstance(sfx, list) or not sfx:
        _die("suffixes 至少需要一个元素")
    for i, s in enumerate(sfx):
        if not isinstance(s.get("name"), str):
            _die(f"suffixes[{i}].name 必须为字符串")

    # ── character_set ──
    cs = cfg.get("character_set", {})
    scl = cs.get("single_char_length")
    if not isinstance(scl, int) or scl < 1:
        _die(f"character_set.single_char_length 必须为正整数, 当前: {scl}")

    types = cs.get("types", [])
    if not isinstance(types, list) or not types:
        _die("character_set.types 至少需要一个类型")
    scl_type_max = {1: 5, 2: 8, 3: 8, 4: 3}
    tmax = scl_type_max.get(scl, 2)  # scl >= 5: 1=自定义 2=Unicode区间
    for t in types:
        if not isinstance(t, int) or t < 1 or t > tmax:
            _die(f"character_set.types: scl={scl} 时 type 必须为 1~{tmax}, 当前: {t}")

    custom = cs.get("custom_values")
    if custom is not None:
        if not isinstance(custom, (str, list)):
            _die("character_set.custom_values 必须为字符串或数字列表")
        if isinstance(custom, str) and len(custom) == 0:
            _die("character_set.custom_values 不能为空字符串")
        if isinstance(custom, list) and len(custom) == 0:
            _die("character_set.custom_values 不能为空列表")

    # ── enumeration ──
    en = cfg.get("enumeration", {})
    mode = en.get("mode")
    if mode not in (1, 2, 3, 4):
        _die(f"enumeration.mode 必须为 1/2/3/4, 当前: {mode}")

    vlen = en.get("variable_length")
    if not isinstance(vlen, int) or vlen < 1:
        _die(f"enumeration.variable_length 必须为正整数, 当前: {vlen}")

    ranges = en.get("ranges", [])
    if not isinstance(ranges, list) or not ranges:
        _die("enumeration.ranges 至少需要一个区间")
    for i, r in enumerate(ranges):
        start = r.get("start", 0)
        end = r.get("end", int(1e18))
        if not isinstance(start, int) or start < 0:
            _die(f"enumeration.ranges[{i}].start 必须 >= 0, 当前: {start}")
        if not isinstance(end, int) or (end != -1 and end <= start):
            _die(f"enumeration.ranges[{i}].end 必须 > start (或 -1=无限), 当前: end={end} start={start}")

    # ── enumeration.prefix_ranges (可选: 按连续段分配前缀区间) ──
    pfx_count = len(cfg.get("prefixes", []))
    pr_cfg = en.get("prefix_ranges")
    if pr_cfg is not None:
        if not isinstance(pr_cfg, list) or not pr_cfg:
            _die("enumeration.prefix_ranges 必须为非空列表")
        total_cnt = 0
        for i, pr in enumerate(pr_cfg):
            cnt = pr.get("count")
            if not isinstance(cnt, int) or cnt < 1:
                _die(f"enumeration.prefix_ranges[{i}].count 必须为正整数, 当前: {cnt}")
            start = pr.get("start", 0)
            end = pr.get("end", int(1e18))
            if not isinstance(start, int) or start < 0:
                _die(f"enumeration.prefix_ranges[{i}].start 必须 >= 0, 当前: {start}")
            if not isinstance(end, int):
                _die(f"enumeration.prefix_ranges[{i}].end 必须为整数, 当前: {end}")
            # end == 0 且 start == 0: 跳过此前缀 (零区间)
            if end == 0 and start == 0:
                pass
            elif end == -1:
                pass  # 几乎无限 — engine 层会展开
            elif end == start and 'format' in cfg and mode == 1:
                pass  # 格式检查点完成的有限区间保留 end，不丢失覆盖边界。
            elif end <= start:
                _die(f"enumeration.prefix_ranges[{i}].end 必须 > start (或 0=跳过, -1=无限), 当前: end={end} start={start}")
            total_cnt += cnt
        if total_cnt != pfx_count:
            _die(f"enumeration.prefix_ranges count 总和 ({total_cnt}) != prefixes 数量 ({pfx_count})")

    # ── collection (使用 _read_cfg 统一读取) ──
    # 逐族评分阈值 (MNAME 序): min_qp/min_qd/min_pp/min_pd/min_cqd/min_cp/min_fp/min_wc/min_fs/min_pj
    for mk in ("min_qp", "min_qd", "min_pp", "min_pd", "min_cqd",
               "min_cp", "min_fp", "min_wc", "min_fs", "min_pj"):
        mv = _read_cfg(cfg, f"collection.{mk}")
        if not isinstance(mv, int) or mv < 0 or mv > 9999:
            _die(f"collection.{mk} 必须为 0~9999, 当前: {mv}")

    # 逐族 ptt(potential) 阈值: 仅 SP2 族 cp/fp/wc/fs/pj 有定义
    for pk in ("ptt_cp", "ptt_fp", "ptt_wc", "ptt_fs", "ptt_pj"):
        pv = _read_cfg(cfg, f"collection.{pk}")
        if not isinstance(pv, int) or pv < 0 or pv > 9999:
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
    if cm not in (0, 1, 2):
        _die(f"collection.collect_mode 必须为 0/1/2, 当前: {cm}")

    if cm == 2:
        cl = cfg.get("collection", {})
        st = cl.get("special_thresholds", {})
        for key, label, vmax in [
            ("eight_v_min", "八围最低", 999),
            ("seven_v_min", "七围最低", 999),
            ("hl_min", "HL 最低", 99),
            ("hp398_eight_v_min", "HP398 八围最低", 999),
        ]:
            v = st.get(key)
            if v is not None and (not isinstance(v, int) or v < 0 or v > vmax):
                _die(f"collection.special_thresholds.{key} ({label}) 必须为 0~{vmax}, 当前: {v}")

    # ── output (使用 _read_cfg 统一读取) ──
    ox = _read_cfg(cfg, "output.output_xp")
    if ox not in (0, 1):
        _die(f"output.output_xp 必须为 0 或 1, 当前: {ox}")

    ol = _read_cfg(cfg, "output.log_output")
    if ol not in (0, 1):
        _die(f"output.log_output 必须为 0 或 1, 当前: {ol}")

    os_ = _read_cfg(cfg, "output.speed_output")
    if os_ not in (0, 1):
        _die(f"output.speed_output 必须为 0 或 1, 当前: {os_}")

    # ── threads (使用 _read_cfg 统一读取) ──
    wt = _read_cfg(cfg, "threads.worker_threads")
    if not isinstance(wt, int) or wt < -1 or wt == 0 or wt > 256:
        _die(f"threads.worker_threads 必须为 -1(自动) 或 1~256, 当前: {wt}")


# ── CONFIG_MAP 索引 (启动时构建, 避免每次遍历) ──────────────────────────────
_config_index: dict = {fd.config_path: fd for fd in CONFIG_MAP}


def _read_cfg(cfg: dict, config_path: str) -> Any:
    """从配置 dict 按 config_path 读取值, 使用 CONFIG_MAP 定义的默认值."""
    fd = _config_index.get(config_path)
    if fd is None:
        raise KeyError(f"未知 config_path: {config_path} (未在 CONFIG_MAP 中定义)")
    return read_config(cfg, fd)


def _build_task_config(cfg: dict) -> dict:
    """从配置 dict 构建 engine 层 task_config，统一使用 engine 键名.

    新增字段只需在 CONFIG_MAP 加一行，此函数自动包含。
    特殊字段 (character_set/prefixes/suffixes/ranges) 手工补充。
    """
    from .flex import normalize_config
    cfg = normalize_config(cfg)
    en = cfg["enumeration"]
    cl = cfg["collection"]
    out_cfg = cfg["output"]
    rng = en.get("ranges", [{}])[0]

    # ── 标量字段: 从 CONFIG_MAP 自动生成 ──
    tc: dict = {}
    for fd in CONFIG_MAP:
        tc[fd.engine_key] = read_config(cfg, fd)

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

    # ── Per-prefix ranges (可选): 按枚举区间分配给各前缀 ──
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
                        help="配置文件路径 (默认依次尝试 config.json/yaml/toml)")
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
    parser.add_argument("--collect-mode", type=int, choices=[0,1,2], help="覆盖: collection.collect_mode")
    parser.add_argument("--output-xp", type=int, choices=[0,1], help="覆盖: output.output_xp")
    parser.add_argument("-o", "--output-file", help="覆盖: output.result_file (+ =随机, - =时间戳, 其他=自定义)")
    parser.add_argument("--output-dest", choices=["file", "stdout", "both"], default=None,
                        help="输出目标: file=仅文件(默认) stdout=仅终端 both=文件+终端")
    parser.add_argument("--debug", action="store_true", help="启用引擎 debug 诊断输出")
    parser.add_argument("--resume", action="store_true", help="断点续测: 从输出文件最后一行继续")
    parser.add_argument("--seed", type=int, help="覆盖: 随机种子 (mode 2/3/4 确定性随机)")
    parser.add_argument("--scl", type=int, choices=[1,2,3,4], help="覆盖: character_set.single_char_length")
    parser.add_argument("--types", help="覆盖: character_set.types (逗号分隔, 如 1,2,3)")
    parser.add_argument("--custom-values", help="覆盖: character_set.custom_values (字符串)")
    args = parser.parse_args()

    # ── 2. 环境准备 ──
    # main.py is the real-name search entry point. Variant engines such as
    # src/vgen are analysis tools and must not leak in through the shell.
    os.environ.pop("PBB_SRC_DIR", None)
    engine_bin = ensure_all(rebuild=args.rebuild, verbose=args.debug)

    # ── 3. 解析配置文件 ──
    try: import yaml; HAS_YAML = True
    except ImportError: HAS_YAML = False

    config_path = args.config
    if config_path is not None and not os.path.isabs(config_path) and not os.path.exists(config_path):
        candidate = os.path.join(build_settings.config_root(BASE_DIR), config_path)
        if os.path.isfile(candidate):config_path = candidate
    if config_path is None:
        for name in ("config.json", "config.yaml", "config.toml"):
            candidate = os.path.join(build_settings.config_root(BASE_DIR), name)
            if os.path.exists(candidate):
                config_path = candidate
                break
        if config_path is None:
            print("ERROR: 未找到配置文件 (尝试了 config.json/yaml/toml)", file=sys.stderr)
            print("  提示: 复制 config.example.* → config.* 并修改", file=sys.stderr)
            sys.exit(1)
        print(f"[main] Auto config: {config_path}", file=sys.stderr)
    elif not os.path.exists(config_path):
        print(f"ERROR: 配置文件不存在: {config_path}", file=sys.stderr)
        sys.exit(1)

    suffix = os.path.splitext(config_path)[1].lower()
    if suffix in ('.yaml', '.yml'):
        if not HAS_YAML: print("ERROR: PyYAML not installed", file=sys.stderr); sys.exit(1)
        with open(config_path, encoding="utf-8") as f: cfg = yaml.safe_load(f)
    elif suffix == '.toml':
        try: import tomllib
        except ImportError: import tomli as tomllib
        with open(config_path, "rb") as f: cfg = tomllib.load(f)
    else:
        with open(config_path, encoding="utf-8") as f: cfg = json.load(f)

    # ── 3.5 配置合法性校验 ──
    from .flex import resolve_files
    cfg = resolve_files(cfg, config_path)
    _validate_config(cfg)

    # ── 3.6 断点续测 (--resume) ──
    resume_desc = None
    resume_temp_file = None
    if args.resume:
        rf_raw = cfg.get("output", {}).get("result_file", "result.txt")
        rf = _resolve_result_file(rf_raw)
        try:
            resumed = _try_resume(cfg, rf)
        except ValueError as exc:
            print(f'[resume] ERROR: {exc}', file=sys.stderr)
            sys.exit(1)
        if resumed is not None:
            new_cfg, resume_desc = resumed
            # 写临时 YAML 配置文件 (保留原始 config)
            fd, resume_temp_file = tempfile.mkstemp(suffix=".yaml", prefix="pbb_resume_",
                                                     dir=os.path.dirname(config_path) or ".")
            os.close(fd)
            try:
                import yaml
            except ImportError:
                print("ERROR: PyYAML not installed", file=sys.stderr)
                sys.exit(1)
            with open(resume_temp_file, "w", encoding="utf-8") as f:
                yaml.dump(new_cfg, f, allow_unicode=True, default_flow_style=False)
            print(f"[resume] 临时配置: {resume_temp_file}", file=sys.stderr)
            # 使用新配置文件
            cfg = new_cfg
            config_path = resume_temp_file

    # ── 4. 组装引擎配置 (CONFIG_MAP 驱动 + CLI 覆盖) ──
    task_config = _build_task_config(cfg)
    if 'format' in task_config and any(value is not None for value in
                                      (args.vlen, args.scl, args.types, args.custom_values)):
        _die('格式配置的位置与字符集由 format/character_sets 定义，不能用 --vlen/--scl/--types/--custom-values 覆盖')

    # -1 = 几乎无限 (uint64_t max ≈ 1.8×10^19)
    if task_config["range_R"] == -1:
        task_config["range_R"] = 2**64 - 1

    # collect_mode=2: 阈值 (从 CONFIG_MAP 自动包含, 无需手动 add)
    # 但需确认 collect_mode 一致性
    if task_config.get("collect_mode") != 2:
        for k in ("c_eight_v_min", "c_seven_v_min", "c_hl_min", "c_hp398_min"):
            task_config.pop(k, None)

    # CLI 覆盖 (使用 engine 键名)
    if args.team:           task_config["team_name"] = args.team
    if args.mode is not None:    task_config["mode"] = args.mode
    if args.vlen is not None:    task_config["variable_len"] = args.vlen
    if args.range_start is not None: task_config["range_L"] = args.range_start
    if args.range_end is not None:   task_config["range_R"] = args.range_end
    for _mk in ("qp", "qd", "pp", "pd", "cqd", "cp", "fp", "wc", "fs", "pj"):
        _mv = getattr(args, f"min_{_mk}")
        if _mv is not None: task_config[f"min_{_mk}"] = _mv
    if args.collect_mode is not None: task_config["collect_mode"] = args.collect_mode
    if args.output_xp is not None: task_config["output_xp"] = args.output_xp
    if args.debug:             task_config["debug_mode"] = 1
    if args.seed is not None:  task_config["seed"] = args.seed
    if args.threads is not None: task_config["n_threads"] = args.threads
    if args.scl is not None: task_config["character_set"]["single_char_length"] = args.scl
    if args.types is not None:
        task_config["character_set"]["types"] = [int(x.strip()) for x in args.types.split(",")]
    if args.custom_values is not None:
        # 支持字符串和逗号分隔的数字列表 (如 240,159,153)
        val = args.custom_values
        try:
            parts = [int(x.strip()) for x in val.split(",")]
            if len(parts) > 1:
                task_config["character_set"]["custom_values"] = parts
            else:
                task_config["character_set"]["custom_values"] = val
        except ValueError:
            task_config["character_set"]["custom_values"] = val

    # ── 5. 执行 ──
    from .threading_config import resolve_threads
    n = resolve_threads(task_config.get("n_threads", -1))
    task_config["n_threads"] = n

    # 解析输出文件名: 配置文件 → CLI 覆盖
    result_file = _resolve_result_file(task_config.get("result_file", "result.txt"))
    if args.output_file is not None:
        result_file = _resolve_result_file(args.output_file)

    # 输出目标: CLI > 默认 file
    output_dest = args.output_dest or "file"

    # stdout 模式: 使用临时目录 (引擎写文件, Python 读取后自动清理)
    if output_dest == "stdout":
        out_dir = None       # run() 内部创建 TemporaryDirectory, 用完即删
        print(f"[main] Threads: {n}, Mode: {task_config['mode']}, Output: stdout", file=sys.stderr)
    elif output_dest == "both":
        out_dir = "."
        print(f"[main] Threads: {n}, Mode: {task_config['mode']}, Output: file+stdout → out/{result_file}", file=sys.stderr)
    else:  # file
        out_dir = "."
        print(f"[main] Threads: {n}, Mode: {task_config['mode']}, Output: out/{result_file}", file=sys.stderr)

    t0 = time.time()
    try:
        result = run_engine(task_config, engine_bin, out_dir=out_dir, result_file=result_file)
    except KeyboardInterrupt:
        print("\n[main] Interrupted", file=sys.stderr)
        sys.exit(130)
    elapsed = time.time() - t0

    # ── 6. 输出 ──
    count = len(result["results"])
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
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

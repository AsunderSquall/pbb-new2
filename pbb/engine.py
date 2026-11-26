"""
PBB 引擎执行模块 — 从 main.py 拆分, 供单机/服务端/客户端三方共用.

run(config, engine_bin, out_dir) → {results, max_xp, max_xd, speed}

config 字典使用 engine 键名 (与 engine.hpp kv[...] 及 config_schema.CONFIG_MAP 一致)。
"""
import os, sys, time, tempfile, subprocess, signal

from . import paths
from .autostart import processed_from_line
from .config_schema import engine_default, ALL_ENGINE_KEYS
from .threading_config import resolve_threads


def _pbb_core():
    """返回已初始化的 pbb_core 原生扩展（首次调用时插入 build/ 到 sys.path）。

    pbb_core.*.pyd 由 tools/build.py 编译到 build/ 目录，**不在包内**，所以不能写成
    模块级 `import pbb_core` —— 那会在包导入时立刻失败。所有用到它的地方都通过本函数
    获取，路径由 paths.BUILD_DIR 统一给出（开发仓库是 <仓库根>/build，
    PyInstaller 冻结后是 _internal/build）。
    """
    build_dir = str(paths.BUILD_DIR)
    if build_dir not in sys.path:
        sys.path.insert(0, build_dir)
    import pbb_core

    # init_exhanzi() 载入内置扩展汉字表，只需一次；重复调用无副作用。
    pbb_core.init_exhanzi()
    return pbb_core


def _custom_bytes(values):
    """将自定义字符串或字节列表转为原始字节。"""
    if isinstance(values, str):
        return values.encode("utf-8")
    if not isinstance(values, list) or any(type(x) is not int or not 0 <= x <= 255 for x in values):
        raise ValueError('custom_values 必须为字符串或由 0~255 整数组成的字节列表')
    return bytes(values)


def validate_charset(cs, field='character_set'):
    """报告会产生空字符集、损坏 UTF-8 单元或破坏结果文件的配置。"""
    if not isinstance(cs, dict):
        raise ValueError(f'{field} 必须为表')
    width = cs.get('single_char_length')
    if type(width) is not int or not 1 <= width <= 64:
        raise ValueError(f'单字符字节数（{field}.single_char_length）必须为 1~64 的整数，当前为 {width!r}')
    types = cs.get('types')
    custom_types, range_type = {1: ((4, 5), None), 2: ((7,), 8), 3: ((7,), 8), 4: ((2,), 3)}.get(width, ((1,), 2))
    maximum = range_type or 5
    if not isinstance(types, list) or not types or any(type(t) is not int or not 1 <= t <= maximum for t in types):
        raise ValueError(f'{field}.types 必须为非空列表，字节数为 {width} 时类型码为 1~{maximum}')
    if any(t in custom_types for t in types):
        custom = cs.get('custom_values')
        if custom is None or custom == '' or custom == []:
            raise ValueError(f'已选择自定义字符，{field}.custom_values 不能为空')
        try:
            data = _custom_bytes(custom)
        except UnicodeError as exc:
            raise ValueError(f'{field}.custom_values 自定义字符中出现非法字符，无法编码为 UTF-8') from exc
        except ValueError as exc:
            raise ValueError(f'{field}.{exc}') from exc
        if len(data) % width:
            raise ValueError(f'{field}.custom_values 共 {len(data)} 字节，必须能分成 {width} 字节的等宽单元')
        if any(delimiter in data for delimiter in (b'\0', b'\r', b'\n', b'\t')):
            raise ValueError(f'{field}.custom_values 自定义字符中出现非法字符，不能包含 NUL、换行、回车或制表符')
        try:
            for offset in range(0, len(data), width):
                data[offset:offset + width].decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError(f'{field}.custom_values 自定义字符中出现非法字符，第 {offset // width + 1} 个单元不是完整的 UTF-8 文本，请检查字节数') from exc
    if range_type not in types:
        return
    intervals = cs.get('unicode_ranges')
    if width > 4:
        raise ValueError(f'{field}.unicode_ranges 的单个码点最多占 4 个 UTF-8 字节，请用自定义字符填写等宽单元')
    if not isinstance(intervals, list) or not intervals:
        raise ValueError(f'已选择 Unicode 区间，{field}.unicode_ranges 不能为空')
    for i, interval in enumerate(intervals):
        label = f'{field}.unicode_ranges[{i}]'
        if not isinstance(interval, dict):
            raise ValueError(f'{label} 必须为包含 start 和 end 的表')
        start, end = interval.get('start'), interval.get('end')
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= 0x10ffff:
            raise ValueError(f'{label} 需要有效且按顺序排列的 Unicode 码点')
        if start <= 0xdfff and end >= 0xd800:
            raise ValueError(f'{label} 不能包含代理码点 U+D800 至 U+DFFF')
        if any(len(chr(cp).encode('utf-8')) != width for cp in (start, end)):
            raise ValueError(f'{label} 中每个码点的 UTF-8 编码必须为 {width} 字节')


def _build_charset(cs: dict) -> str:
    """根据 character_set 配置构建 charset hex 字符串.

    调用方必须保证 pbb_core 已就绪（engine.run() 会先取 _pbb_core()，它已 init_exhanzi()）；
    这里直接用模块级导入的 pbb_core，不再重复插 sys.path。

    返回的 hex 串长度必须是 single_char_length 的整数倍：每个「枚举单元」占
    scl 字节，而一个 Unicode 字符不一定只占一个单元（汉字在 UTF-8 下 3 字节）。
    这一点是配置校验的关键，见 main.py 的 _validate_config。

    类型码含义见 README.md「字符类型」一节：
      1 字节: 1 数字 / 2 小写英文 / 3 大写英文 / 4、5 自定义（5 并非完整 ASCII 表）
      2 字节: 1 希腊小写 2 希腊大写 3 俄文小写 4 俄文大写 5 拉丁小写 6 拉丁大写
              7 自定义 / 8 Unicode 区间
      3 字节: 1 基础汉字区间 2 内置汉字表 3 平假名 4 片假名 5 盲文
              6 扩展汉字 7 自定义 / 8 Unicode 区间
      4 字节: 1 扩展汉字 2 自定义 / 3 Unicode 区间
      >=5 字节: 1 自定义 / 2 Unicode 区间（足球配置用 6 字节单元）
    同一 scl 下多个类型会被依次拼接，不自动去重（README.md 有说明）。
    """
    if 'file' in cs:
        from .flex import file_bytes
        return file_bytes(cs).hex()
    pbb_core = _pbb_core()
    scl = cs["single_char_length"]
    buf = bytearray()

    for t in cs["types"]:
        if scl == 1:
            if t == 1:   buf.extend(b'0123456789')
            elif t == 2: buf.extend(b'abcdefghijklmnopqrstuvwxyz')
            elif t == 3: buf.extend(b'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
            elif t in (4, 5): buf.extend(_custom_bytes(cs.get("custom_values", [])))

        elif scl == 2:
            if t == 1:   buf.extend(pbb_core.get_xila_bytes())
            elif t == 2: buf.extend(pbb_core.get_XILA_bytes())
            elif t == 3: buf.extend(pbb_core.get_ewen_bytes())
            elif t == 4: buf.extend(pbb_core.get_EWEN_bytes())
            elif t == 5: buf.extend(pbb_core.get_lading_bytes())
            elif t == 6: buf.extend(pbb_core.get_LADING_bytes())
            elif t == 7: buf.extend(_custom_bytes(cs.get("custom_values", [])))
            elif t == 8:
                for r in cs.get("unicode_ranges", []):
                    for cp in range(r["start"], r["end"] + 1):
                        buf.extend(pbb_core.encode_unicode(cp))

        elif scl == 3:
            if t == 1:
                pbb_core.load_hanzi(19968, 40959)
                buf.extend(pbb_core.get_charset_bytes())
                pbb_core.reset_charset()
            elif t == 2: buf.extend(pbb_core.get_hanzi_bytes())
            elif t == 3: buf.extend(pbb_core.get_pingjia_bytes())
            elif t == 4: buf.extend(pbb_core.get_pianjia_bytes())
            elif t == 5: buf.extend(pbb_core.get_mangwen_bytes())
            elif t == 6: buf.extend(pbb_core.get_extended_hanzi_3_bytes())
            elif t == 7: buf.extend(_custom_bytes(cs.get("custom_values", [])))
            elif t == 8:
                for r in cs.get("unicode_ranges", []):
                    for cp in range(r["start"], r["end"] + 1):
                        buf.extend(pbb_core.encode_unicode(cp))

        elif scl == 4:
            if t == 1:   buf.extend(pbb_core.get_extended_hanzi_4_bytes())
            elif t == 2: buf.extend(_custom_bytes(cs.get("custom_values", [])))
            elif t == 3:
                for r in cs.get("unicode_ranges", []):
                    for cp in range(r["start"], r["end"] + 1):
                        buf.extend(pbb_core.encode_unicode(cp))

        else:  # scl >= 5 (5+字节)
            if t == 1:   buf.extend(_custom_bytes(cs.get("custom_values", [])))
            elif t == 2:
                for r in cs.get("unicode_ranges", []):
                    for cp in range(r["start"], r["end"] + 1):
                        buf.extend(pbb_core.encode_unicode(cp))

    return bytes(buf).hex()


# ── 评分族 (引擎输出列顺序) ────────────────────────────────────────────────
# 引擎输出为 `for(m=0;m<M_N;m++) if(plan.comp[m]) print(sc[m])` (src/engine.hpp L298),
# 即恒按 M_QP..M_PJ 枚举序输出「被计算族」的子序列, 与 score_list 书写顺序无关。
# 故此处也须按 MNAME 序取子集，并把 rh 归一为 fs。
# 这是「Python 侧列序」与「C++ 侧输出序」之间唯一的约定：两边都按枚举序取子集，
# 才能保证 output_xp=1 时第 i 列确实对应 _FAM[i]。改这里必须同步改 engine.hpp。
_FAM = ("qp", "qd", "pp", "pd", "cqd", "cp", "fp", "wc", "fs", "pj")
_FAM_ALIAS = {"rh": "fs"}  # rh = 人海 = fs


def _plan_families(config: dict) -> list[str]:
    """返回引擎实际输出的评分族列序，按 MNAME 顺序排列。"""
    sl = config.get("score_list", "")
    toks = sl if isinstance(sl, (list, tuple)) else str(sl or "").split(",")
    listed = {_FAM_ALIAS.get(str(t).strip(), str(t).strip()) for t in toks}
    return [f for f in _FAM if f in listed]


def _split_trailing_ints(line: str, max_scores=None) -> tuple[str, list[int]]:
    """剥离行尾连续纯整数 token → (名字, [分数..]).

    输出行 = "名字@战队 [sc..]"；sc 列数随 score_list 变化。名字可含空格, 但总以
    "@战队" 结尾, 故末列永非纯整数 —— 逐个剥离末尾整数即得名字。
    """
    sc: list[int] = []
    rest = line
    while max_scores is None or len(sc) < max_scores:
        head, sep, tail = rest.rpartition(" ")
        if not sep:
            break
        try:
            sc.append(int(tail))
        except ValueError:
            break
        rest = head
    sc.reverse()
    return (rest if sc else line), sc


class EngineError(RuntimeError):
    """引擎拒绝启动或运行失败时可直接展示给用户的诊断。"""


def _affix_param(key: str, names) -> str:
    # 每个名字先单独编码，分隔符只出现在编码后的条目之间。
    # 直接调用引擎层时仍支持原来的 CSV 字符串参数。
    if isinstance(names, (list, tuple)):
        encoded = ",".join("" if name == "+" else name.encode("utf-8").hex() for name in names)
        return f"{key}_hex={encoded}\n"
    return f"{key}={names or ''}\n"


def _build_params(config: dict, charset_hex: str, result_file: str = "result.txt") -> str:
    """根据配置 + charset_hex 构建引擎 stdin 参数.

    所有 engine 键名来自 config_schema.CONFIG_MAP，默认值通过 engine_default() 统一获取。
    result_file: 引擎输出文件名 (相对 out/). 分布式多 task 并发时应传入唯一名,
                 避免同目录撞文件 (如 f"result_{task_id}.txt").
    """
    scl = config["scl"]
    charset_len = len(bytes.fromhex(charset_hex)) // scl if scl else 0

    # _dv(key): engine_default() 获取默认值 (CONFIG_MAP 唯一定义)
    def _dv(key: str):
        try:
            return engine_default(key)
        except KeyError:
            return None  # 计算字段无默认值

    # score_list: 字符串或列表统一拼为逗号串，空串表示不选评分族。
    slist = config.get("score_list", _dv("score_list"))
    if isinstance(slist, (list, tuple)):
        slist = ",".join(str(x).strip() for x in slist)
    if slist is None:
        slist = ""
    smode = config.get("score_mode", _dv("score_mode")) or "all"

    params = (
        f"team_name={config['team_name']}\n"
        f"n_threads={resolve_threads(config.get('n_threads', _dv('n_threads')))}\n"
        f"scl={scl}\ncharset_len={charset_len}\ncharset_bytes={charset_hex}\n"
        f"{_affix_param('prefixes', config.get('prefixes'))}"
        f"{_affix_param('suffixes', config.get('suffixes'))}"
        f"mode={config['mode']}\nvariable_len={config['variable_len']}\n"
        f"range_L={config['range_L']}\nrange_R={config['range_R']}\n"
        f"min_qp={config.get('min_qp', _dv('min_qp'))}\n"
        f"min_qd={config.get('min_qd', _dv('min_qd'))}\n"
        f"min_pp={config.get('min_pp', _dv('min_pp'))}\n"
        f"min_pd={config.get('min_pd', _dv('min_pd'))}\n"
        f"min_cqd={config.get('min_cqd', _dv('min_cqd'))}\n"
        f"min_cp={config.get('min_cp', _dv('min_cp'))}\n"
        f"min_fp={config.get('min_fp', _dv('min_fp'))}\n"
        f"min_wc={config.get('min_wc', _dv('min_wc'))}\n"
        f"min_fs={config.get('min_fs', _dv('min_fs'))}\n"
        f"min_pj={config.get('min_pj', _dv('min_pj'))}\n"
        f"ptt_cp={config.get('ptt_cp', _dv('ptt_cp'))}\n"
        f"ptt_fp={config.get('ptt_fp', _dv('ptt_fp'))}\n"
        f"ptt_wc={config.get('ptt_wc', _dv('ptt_wc'))}\n"
        f"ptt_fs={config.get('ptt_fs', _dv('ptt_fs'))}\n"
        f"ptt_pj={config.get('ptt_pj', _dv('ptt_pj'))}\n"
        f"collect_mode={int(config.get('collect_mode', _dv('collect_mode')) == 1)}\n"
        f"score_list={slist}\nscore_mode={smode}\n"
        f"output_xp={config.get('output_xp', _dv('output_xp'))}\n"
        f"output_log={config.get('output_log', _dv('output_log'))}\n"
        f"output_speed={config.get('output_speed', _dv('output_speed'))}\n"
        f"debug_mode={config.get('debug_mode', _dv('debug_mode'))}\n"
        f"result_file={result_file}\n"
    )
    # A1: seed 传递 (config 有 seed 才传; 缺失则引擎用时间熵, 单机行为不变)
    if config.get("seed") is not None:
        params += f"seed={config['seed']}\n"
    # ui_mode 让引擎额外维护 Top 5 并列集合并通过自己的协议回传排行榜；
    # 排行榜不需要把 output_xp 改成 1，普通引擎没有这部分开销。
    if config.get("ui_mode"):
        params += "ui_mode=1\n"
    # Per-prefix ranges (可选; 不存在时引擎回退到 range_L/R)
    if config.get("prefix_range_L"):
        params += f"prefix_range_L={config['prefix_range_L']}\n"
        params += f"prefix_range_R={config['prefix_range_R']}\n"
    if 'format' in config:
        from .flex import build_layout
        template, slots = build_layout(config, _build_charset)
        if config['mode'] == 1:
            from .flex import resume_id
            params += f'flex_resume_id={resume_id(config, template, slots)}\n'
        params += f'flex_template={template.hex()}\n'
        params += 'flex_offsets=' + ','.join(str(s[0]) for s in slots) + '\n'
        params += 'flex_widths=' + ','.join(str(s[1]) for s in slots) + '\n'
        params += 'flex_charsets=' + ','.join(s[2].hex() for s in slots) + '\n'

    # 自检: 所有生成的 engine key 必须在 ALL_ENGINE_KEYS 中注册
    # (仅 debug_mode 开启时检查，避免影响性能)
    if config.get("debug_mode"):
        for line in params.strip().split("\n"):
            if "=" in line:
                key = line.split("=", 1)[0]
                if key not in ALL_ENGINE_KEYS:
                    print(f"[engine] WARNING: unknown engine key '{key}' — "
                          f"not in CONFIG_MAP or _ENGINE_KEYS_COMPUTED", file=sys.stderr)

    return params


def run(config: dict, engine_bin: str = None, out_dir: str = None,
        result_file: str = "result.txt", on_progress=None) -> dict:
    """执行单个 task, 返回 {results, max_xp, max_xd, speed, count_raw}.

    config 两种模式:
      模式 A: character_set -> 内部调 pbb_core 构建 charset
      模式 B: charset_hex     -> 直接使用 (服务端下发)

    config 可选键:
      seed: 随机种子. None=引擎用时间熵 (单机默认). 指定值=确定性 (分布式可复现).

    out_dir:     输出目录. None=临时目录(用完删除), "."=当前目录保留.
    result_file: 引擎结果文件名 (相对 out/). 分布式多 task 并发同目录时传唯一名.
    on_progress: 可选回调 ``on_progress(processed)``，引擎每输出一行进度就用"已处理
                 候选数"调用一次（「自动更新 start」按 0.01T 阶梯写盘就挂在这里，
                 不需要另开轮询线程）。
    """
    # 字符集
    if "charset_hex" in config:
        charset_hex = config["charset_hex"]
    else:
        _pbb_core()
        charset_hex = _build_charset(config["character_set"])

    # 线程数
    config["n_threads"] = resolve_threads(config.get("n_threads", -1))

    params = _build_params(config, charset_hex, result_file)

    # 引擎路径
    if engine_bin is None:
        engine_bin = str(paths.engine_binary(ui=False))

    # 输出目录
    _use_temp = out_dir is None
    if _use_temp:
        _tmp = tempfile.TemporaryDirectory()
        out_dir = _tmp.name

    result_dir = os.path.join(out_dir, "out")
    os.makedirs(result_dir, exist_ok=True)
    result_path = os.path.join(result_dir, result_file)
    try:
        original_size = os.path.getsize(result_path)
    except FileNotFoundError:
        original_size = 0

    _interrupted = False
    _proc = None

    def _on_sigint(sig, frame):
        nonlocal _interrupted
        if _interrupted:
            # 第二次 Ctrl+C: 强制退出
            os._exit(130)
        _interrupted = True
        print("", file=sys.stderr, flush=True)
        print("[engine] Interrupted — stopping engine...", file=sys.stderr, flush=True)
        if _proc and _proc.poll() is None:
            _proc.terminate()

    old_handler = signal.signal(signal.SIGINT, _on_sigint)

    try:
        t0 = time.time()
        proc = subprocess.Popen(
            [os.path.abspath(engine_bin)], cwd=out_dir, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace")
        _proc = proc
        # 协议：参数一次性写进 stdin 后立即关闭；引擎把进度/摘要写到 stderr，
        # 结果名字写到 out/<result_file>。stdout 无内容，故丢弃。
        try:
            proc.stdin.write(params)
            proc.stdin.close()
        except BrokenPipeError:
            pass  # 继续读取 stderr，保留引擎拒绝参数时的原因。

        # 引擎权威摘要 (问题4/5: SUMMARY 行由引擎输出, Python 采信不重算)
        summary = None
        diagnostic = None
        for line in proc.stderr:
            line = line.strip()
            if not line:
                continue
            if line.startswith("SUMMARY "):
                # 解析 key=value 摘要; 不当普通进度行打印 (避免污染输出)
                summary = {}
                for tok in line[len("SUMMARY "):].split():
                    k, _, v = tok.partition("=")
                    summary[k] = v
                continue
            if line.startswith("[engine] ERROR:"):
                diagnostic = line
            if on_progress is not None:
                done = processed_from_line(line)
                if done is not None:
                    on_progress(done)
            print(f"  {line}", file=sys.stderr, flush=True)
        proc.wait()
        elapsed = time.time() - t0

        if _interrupted:
            raise KeyboardInterrupt
        if proc.returncode != 0:
            message = f"引擎运行失败（退出码 {proc.returncode}）"
            raise EngineError(f"{message}：{diagnostic}" if diagnostic else message)

        # 读取结果 (名字列表仍从文件读; max/speed 优先采信引擎摘要)
        # 输出列随 score_list 变化 ⇒ 按「末尾纯整数 token」定列; xp/xd 仅当 qp/qd 在列内才非 0。
        fams = _plan_families(config)
        results = []
        mxp, mxd = 0, 0
        # 空计划不会写结果，已有文件中的历史结果也不计入本次运行。
        if fams and os.path.exists(result_path):
            score_columns = len(fams) if config.get('output_xp', engine_default('output_xp')) else 0
            with open(result_path, encoding="utf-8", errors="replace") as f:
                f.seek(original_size)
                for line in f:
                    line = line.rstrip('\r\n')
                    if not line: continue
                    name, sc = _split_trailing_ints(line, score_columns)
                    xp = xd = 0
                    for i, fam in enumerate(fams):
                        if i >= len(sc): break
                        if fam == "qp":   xp = sc[i]
                        elif fam == "qd": xd = sc[i]
                    results.append({"name": name, "sc": sc, "xp": xp, "xd": xd})
                    mxp = max(mxp, xp)
                    mxd = max(mxd, xd)
    finally:
        signal.signal(signal.SIGINT, old_handler)
        if _use_temp:
            _tmp.cleanup()

    # 问题4/5: 优先采信引擎权威摘要 (max 含通过 V 值/技能检查的名字、speed 为纯算力吞吐)。
    # 文件重算的 max 仅是达标名字的 max (语义不同), 墙钟反算的 speed 含 IPC 噪声。
    # 摘要缺失时 (旧引擎/异常) 回退到文件重算 + 墙钟反算, 保证健壮性。
    if summary is not None:
        max_xp = int(summary.get("max_xp", mxp))
        max_xd = int(summary.get("max_xd", mxd))
        max_sum = int(summary.get("max_sum", 0))
        found = int(summary.get("found", len(results)))
        speed = float(summary.get("speed", 0.0))
        return {"results": results, "max_xp": max_xp, "max_xd": max_xd,
                "max_sum": max_sum, "found": found, "speed": speed,
                "count_raw": int(summary.get("count", 0) or 0),
                "contiguous": int(summary.get("contiguous", summary.get("count", 0)))}

    # 回退路径 (无摘要)
    rng = config["range_R"] - config["range_L"]
    speed = rng / elapsed if elapsed > 0 and rng > 0 else 0
    return {"results": results, "max_xp": mxp, "max_xd": mxd,
            "max_sum": 0, "found": len(results), "speed": speed,
            "count_raw": 0, "contiguous": 0}

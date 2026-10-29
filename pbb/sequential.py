"""顺序模式（mode 1）的全局区间语义。

用户口径（这就是本模块要保证的约定）：

* **全局区间是唯一的总控制器**：`enumeration.ranges[0]` 的 `start` 表示"总量里已经
  测过多少、从哪开始"，`end` 是总量坐标的上界；`end = -1` 表示"所有前缀的总量之和"。
* **每个前缀的区间只是布局**（`enumeration.prefix_ranges`）：程序永不修改它的
  start/end —— 跑多少次、resume 多少次都不改。
* 前缀区间里的 `end = -1` 表示该前缀的**完整字符集总量**。
* 一个前缀的总量 = `end - start`（`end` 为 -1 时用完整字符集总量代入）。
* 全局坐标按前缀顺序累加：前缀 `p` 占 `[C_p, C_p + total_p)`，其中
  `C_p = total_0 + ... + total_{p-1}`。
* 真正开跑时，把全局 `[start, end)` 映射成各前缀的绝对区间；映射结果只进入本次
  任务的引擎参数，**不写回配置**。
* `resume` 只修改全局 `start`；"自动更新 start" 也只推进全局 `start`。

例（`config/test-shunxu2.toml`）：前缀布局是
`[200e6, 1000e6)`、`[400e6, -1)`、`[0, 500e6)`，`total_0 = 800e6`；全局 `start =
1000e6 = total_0 + 200e6`，于是落在第二个前缀里，对应它的绝对位置
`400e6 + 200e6 = 600e6`。
"""
import math

from . import engine as _engine

U64_MAX = 2 ** 64 - 1


def _u64(value) -> int:
    """夹到 [0, 2^64-1]：引擎的区间是 uint64，Python 侧先按同样口径收敛。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, U64_MAX))


def _charset_entries(config: dict) -> list:
    """字符集条目表（与引擎同一编号口径）；不支持的类型返回空表。"""
    from .main import _build_charset_entries

    entries, _width = _build_charset_entries(config)
    return entries


def full_total(cfg: dict) -> int:
    """单个前缀的完整枚举空间。

    * 非格式配置：``字符集条目数 ^ variable_length``（引擎在 mode 1 里按这个进制
      展开候选编号）。
    * 多字符集格式配置：各可变位置的字表大小之积（与 flex 的编号空间一致）。
    """
    en = cfg.get("enumeration", {}) or {}
    if "format" in cfg:
        from .flex import file_bytes, normalize_config

        normalized = normalize_config(cfg)
        product = 1
        for item in normalized["format"]:
            if not (isinstance(item, dict) and set(item) == {"charset"}):
                continue
            cs = normalized["character_sets"][item["charset"]]
            width = int(cs["single_char_length"])
            data = file_bytes(cs) if "file" in cs else bytes.fromhex(_engine._build_charset(cs))
            product *= max(1, len(data) // max(1, width))
        return _u64(product)
    entries = _charset_entries(cfg["character_set"])
    vlen = int(en.get("variable_length", 0) or 0)
    if not entries or vlen <= 0:
        return 0
    return _u64(len(entries) ** vlen)


def prefix_layout(cfg: dict) -> list[tuple[int, int]]:
    """展开各前缀的**布局区间** ``[(start, end), ...]``，``end = -1`` 保留为 -1。

    ``prefix_ranges`` 按 ``count`` 展开；条目少于前缀数时，多出来的前缀按"完整空间"
    处理（``(0, -1)``），这样"没写区间 = 整个字符集"与直觉一致。
    """
    en = cfg.get("enumeration", {}) or {}
    count = len(cfg.get("prefixes") or [])
    layout: list[tuple[int, int]] = []
    for item in (en.get("prefix_ranges") or []):
        start = int(item.get("start", 0) or 0)
        end = int(item.get("end", -1) if item.get("end", -1) is not None else -1)
        for _ in range(int(item.get("count", 1) or 1)):
            if len(layout) >= count:
                break
            layout.append((start, end))
    while len(layout) < count:
        layout.append((0, -1))
    return layout


def _resolved_totals(cfg: dict, layout: list[tuple[int, int]], complete: int | None = None) -> list[int]:
    """每个前缀的总量；``end = -1`` 时用完整字符集总量代入（只在这里算一次）。"""
    if complete is None:
        complete = full_total(cfg) if any(end == -1 for _start, end in layout) else 0
    totals = []
    for start, end in layout:
        start = _u64(start)
        if end == -1:
            totals.append(max(0, complete - start))
        else:
            totals.append(max(0, _u64(end) - start))
    return totals


def totals(cfg: dict) -> list[int]:
    """各前缀的总量（按 prefix_ranges 的顺序）。"""
    return _resolved_totals(cfg, prefix_layout(cfg))


def total_units(cfg: dict) -> int:
    """所有前缀的总量之和（全局 ``end = -1`` 时的含义）。"""
    return _u64(sum(totals(cfg)))


def global_bounds(cfg: dict) -> tuple[int, int]:
    """全局 ``(start, end)``，``end = -1`` 已替换为"所有前缀总量之和"。"""
    en = cfg.get("enumeration", {}) or {}
    rng = (en.get("ranges") or [{}])[0]
    start = _u64(rng.get("start", 0) or 0)
    end = rng.get("end", -1)
    if end is None or int(end) == -1:
        end = total_units(cfg)
    return start, _u64(end)


def locate(cfg: dict, offset: int) -> tuple[int, int]:
    """把全局坐标 ``offset`` 映射成 ``(前缀下标, 该前缀内的绝对位置)``。

    越界时分别夹到"第一个前缀的开头"或"最后一个前缀的结尾"，便于调用方安全使用。
    """
    layout = prefix_layout(cfg)
    if not layout:
        return 0, _u64(offset)
    sizes = _resolved_totals(cfg, layout)
    remaining = _u64(offset)
    for index, (start, _end) in enumerate(layout):
        size = sizes[index]
        if remaining < size or index == len(layout) - 1:
            return index, _u64(start) + min(remaining, size)
        remaining -= size
    return len(layout) - 1, _u64(layout[-1][0])


def to_global(cfg: dict, prefix_index: int, value: int) -> int:
    """``(前缀下标, 该前缀内的绝对位置)`` → 全局坐标（``locate`` 的逆运算）。"""
    layout = prefix_layout(cfg)
    if not layout:
        return _u64(value)
    index = max(0, min(int(prefix_index), len(layout) - 1))
    sizes = _resolved_totals(cfg, layout)
    base = sum(sizes[:index])
    inside = max(0, _u64(value) - _u64(layout[index][0]))
    return _u64(base + min(inside, sizes[index]))


def effective_prefix_ranges(cfg: dict):
    """把全局 ``[start, end)`` 映射成各前缀的绝对区间（**不改配置**）。

    已扫完的前缀返回 ``(0, 0)``（引擎拿到空区间，直接跳过）；当前前缀从映射位置开始；
    后面的前缀按布局整段跑，直到全局 end 用尽。

    返回 ``None`` 表示"这份配置算不出完整总量"（例如字符集类型依赖 pbb_core 而当前
    环境拿不到表）：调用方应退回旧逻辑（直接把配置里的区间交给引擎），而不是当成
    空区间跑一轮什么都不做。
    """
    layout = prefix_layout(cfg)
    if not layout:
        return []
    complete = full_total(cfg) if any(end == -1 for _start, end in layout) else 0
    if any(end == -1 for _start, end in layout) and complete == 0:
        return None
    sizes = _resolved_totals(cfg, layout, complete)
    start, end = global_bounds(cfg)
    ranges: list[tuple[int, int]] = []
    cursor = 0  # 当前前缀在全局坐标里的起点
    for index, (origin, _end) in enumerate(layout):
        origin = _u64(origin)
        size = sizes[index]
        low = max(start, cursor)
        high = min(end, cursor + size)
        if high <= low:
            ranges.append((0, 0))
        else:
            ranges.append((_u64(origin + (low - cursor)), _u64(origin + (high - cursor))))
        cursor += size
    return ranges


def describe(cfg: dict) -> str:
    """一行摘要，供日志/报告使用：全局区间落在第几个前缀、该前缀从哪开始。"""
    start, end = global_bounds(cfg)
    index, value = locate(cfg, start)
    layout = prefix_layout(cfg)
    if not layout:
        return f"全局 [{start}, {end}) 无前缀"
    name = ""
    prefixes = cfg.get("prefixes") or []
    if index < len(prefixes):
        name = "「" + str(prefixes[index].get("name", "")) + "」"
    return (f"全局 [{start}, {end}) = 前缀[{index}]{name} 的绝对位置 {value}"
            f"（布局 {layout}）")

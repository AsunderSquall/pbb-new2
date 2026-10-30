"""「自动更新 start」：把测号进度按 0.01T 的阶梯写回配置里的全局 ``start``。

**为什么按阶梯写**：测号中途断电/崩溃时，配置里的 ``start`` 就是下一次的起点；每处理
一小段就落一次盘，能把损失控制在 0.01T 以内。写的是**绝对位置**（``起步 start +
已处理量``，并按全局 end 夹住），所以写多少次都不会累加过头。

**为什么不轮询**：引擎每完成 100 个 chunk（≈10^8 个候选）就会往 stderr 打一行
``taskN finished,task_mex=...,count:X.XXXXXXT``；这里只在这行到达时比较一次阈值
（``on_processed``），没有定时器、没有额外的线程，对吞吐没有影响。界面把同一个回调
挂在已有的进度解析上，命令行把它挂在 ``engine.run`` 的行回调上。

0.01T = 10^10 个候选 = 100 个 chunk，正好是引擎那条进度行的 100 倍粒度。
"""
import copy
import re

from .config_file import load_config, save_config

# 每测这么多候选就把 start 往前推一次（0.01T）。
STEP = 10 ** 10

# 引擎的进度行：`task12 finished,task_mex=5,count:0.001200T`
COUNT_RE = re.compile(r"count:([0-9.]+)T")


def processed_from_line(line: str):
    """从引擎的一行进度里取出「已处理候选数」（不是 0.01T 阈值判断，只是解析）。"""
    match = COUNT_RE.search(line or "")
    if not match:
        return None
    try:
        return int(round(float(match.group(1)) * 1e12))
    except (TypeError, ValueError):
        return None


class StartUpdater:
    """把「已处理量」按 0.01T 阶梯写回配置文件；线程/回调皆可调用。

    - ``config_path`` 为 None 或 ``enabled`` 为 False 时全部方法都是空操作；
    - 每次写盘前都重新读一遍磁盘：如果配置在测号期间被手动改过，就停止回写并给出
      提示，避免把用户的修改冲掉（与界面的 ``_advance_config_start`` 同一口径）；
    - 写盘失败只记录一次 ``error``，不影响测号本身。
    """

    def __init__(self, config_path, cfg, enabled: bool = True, notify=None):
        self.config_path = config_path
        self.enabled = bool(enabled) and config_path is not None
        self.notify = notify or (lambda message: None)
        self.origin = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
        self.base = 0
        self.end = None
        self.next_mark = STEP
        self.written = None
        self.error = None
        self.stopped = False
        self._last_saved = copy.deepcopy(self.origin)
        ranges = (self.origin.get('enumeration') or {}).get('ranges') or []
        if ranges:
            self.base = int(ranges[0].get('start', 0) or 0)
        try:
            from . import sequential
            _start, self.end = sequential.global_bounds(self.origin)
        except Exception:
            self.end = None

    def _target(self, processed: int) -> int:
        """本次进度对应的全局 start（绝对位置，已按全局 end 夹住）。"""
        target = self.base + max(0, int(processed))
        if self.end is not None:
            target = min(target, int(self.end))
        return max(0, target)

    def on_processed(self, processed):
        """引擎进度行回调：跨过 0.01T 才写一次，返回写入的 start（没写返回 None）。"""
        if not self.enabled or self.stopped or processed is None:
            return None
        try:
            processed = int(processed)
        except (TypeError, ValueError):
            return None
        if processed < self.next_mark:
            return None
        self.next_mark = (processed // STEP + 1) * STEP
        return self._write(processed)

    def finish(self, processed):
        """测号结束时的收尾写入（不论是否跨过阶梯）。"""
        if not self.enabled or self.stopped or not processed:
            return None
        try:
            processed = int(processed)
        except (TypeError, ValueError):
            return None
        if processed <= 0:
            return None
        return self._write(processed)

    def _write(self, processed: int):
        try:
            if self.written is not None and load_config(self.config_path) != self._last_saved:
                self.stopped = True
                self.notify('配置在测号期间被修改，已停止回写 start')
                return None
            cfg = copy.deepcopy(self.origin)
            ranges = cfg.setdefault('enumeration', {}).setdefault('ranges', [{}])
            if not ranges:
                ranges.append({})
            ranges[0] = dict(ranges[0] or {}, start=self._target(processed))
            save_config(self.config_path, cfg)
            self._last_saved = copy.deepcopy(cfg)
            self.written = ranges[0]['start']
            self.notify(f'已更新全局 start（→{self.written}）')
            return self.written
        except Exception as exc:                      # 写盘失败不能影响测号
            self.error = exc
            self.notify(f'更新 start 失败：{exc}')
            return None

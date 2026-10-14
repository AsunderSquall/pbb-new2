"""统一解析自动线程数；显式配置始终优先，不做线程数扫描。"""
import os


def _windows_affinity_count():
    """读取当前进程亲和性；超过单处理器组时交给系统CPU计数回退。"""
    if os.name != 'nt' or (os.cpu_count() or 0) > 64:
        return None
    try:
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.GetProcessAffinityMask.argtypes = [ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        kernel.GetProcessAffinityMask.restype = ctypes.c_int
        process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
        if kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(),
                ctypes.byref(process_mask), ctypes.byref(system_mask)):
            return process_mask.value.bit_count() or None
    except (AttributeError, OSError):
        pass
    return None


def available_threads():
    """优先使用进程可用逻辑CPU数/亲和性，失败时回退系统CPU数。"""
    process_count = getattr(os, 'process_cpu_count', None)
    count = None
    if process_count is not None:
        try:
            count = process_count()
        except (OSError, NotImplementedError):
            pass
    if not count and hasattr(os, 'sched_getaffinity'):
        try:
            count = len(os.sched_getaffinity(0))
        except (OSError, NotImplementedError):
            pass
    if not count:
        count = _windows_affinity_count()
    return min(256, max(1, count or os.cpu_count() or 4))


def resolve_threads(configured=-1):
    """-1/缺省自动；1～256为用户显式选择，不按CPU数覆盖。"""
    if type(configured) is not int or configured == 0 or configured < -1 or configured > 256:
        raise ValueError('线程数必须为 -1（自动）或 1～256')
    return available_threads() if configured == -1 else configured

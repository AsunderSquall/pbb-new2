"""打包脚本共用的小工具。

目前只有一件事：**安全地生成 zip 到指定路径**。

为什么需要它：把 zip 直接以 "w" 打开目标文件时，如果目标正被杀软/索引服务扫描或
被用户打开着，Windows 会抛 ``PermissionError [WinError 5]``（连 ``Remove-Item``
都会报"正由另一进程使用"）。这类锁通常很快释放，但足以让一次十几分钟的构建作废。

做法：先写到 ``<目标>.building`` 临时文件，再原子替换到目标；替换失败按指数退避重试；
仍然被占用就退而保留为 ``<名字>.<时间戳>.zip`` 并明确报告实际路径 —— 构建成果绝不丢。
"""
import os
import time
import zipfile
from pathlib import Path

# 替换重试次数与首次退避秒数（第 n 次等待 n * BACKOFF 秒）。
ATTEMPTS = 10
BACKOFF = 0.5


def write_zip_atomic(target: Path, fill, compresslevel: int | None = None) -> Path:
    """用 ``fill(archive)`` 填充 zip，然后原子落盘到 ``target``，返回最终路径。

    ``fill`` 接收一个已打开的 ``zipfile.ZipFile``，在其中调用 write/writestr。
    ``compresslevel`` 为 None 时用 zlib 默认级别；环境包约 600 MB，传 6 做速度/体积折中。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".building")
    temporary.unlink(missing_ok=True)

    if compresslevel is None:
        archive = zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED)
    else:
        archive = zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED,
                                  compresslevel=compresslevel)
    with archive:
        fill(archive)

    last_error = None
    for attempt in range(ATTEMPTS):
        try:
            os.replace(temporary, target)
            return target
        except PermissionError as exc:
            last_error = exc
            print(f"[pack] {target.name} is locked, retrying ({attempt + 1}/{ATTEMPTS})...",
                  flush=True)
            time.sleep(BACKOFF * (attempt + 1))

    # 锁一直没释放：构建本身已成功，只是"改名"做不了，所以换个名字保留成品，
    # 并让调用方知道真实路径。
    stamped = target.with_name(f"{target.stem}.{time.strftime('%Y%m%d_%H%M%S')}{target.suffix}")
    os.replace(temporary, stamped)
    print(f"[pack] WARNING: {target.name} is held by another process ({last_error}); "
          f"kept the fresh archive as {stamped.name} instead.", flush=True)
    return stamped
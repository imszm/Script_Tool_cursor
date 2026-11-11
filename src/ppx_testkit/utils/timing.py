"""计时工具：截止时间与可中断等待。

Windows 上网页「停止」发的是 Ctrl+Break（SIGBREAK）。它打不断主线程里的 ``time.sleep`` 和
``Event.wait``：信号处理函数要等这一段等待结束才执行。主线程里的长等待因此按 :data:`STOP_POLL_S`
分段，停止信号最多晚一段就能变成 ``KeyboardInterrupt``，``finally`` 里的硬件复位才来得及跑。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

#: 主线程等待的分段长度（秒）。停止信号最多晚这么久被处理。
STOP_POLL_S = 0.2


class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.start = clock()
        self.end = self.start + max(0.0, seconds)

    @property
    def remaining(self) -> float:
        return max(0.0, self.end - self._clock())

    @property
    def elapsed(self) -> float:
        return self._clock() - self.start

    def expired(self) -> bool:
        return self._clock() >= self.end


def sleep(seconds: float, stop_event: threading.Event | None = None, *, slice_s: float = STOP_POLL_S) -> bool:
    """按截止时间分段等待；``stop_event`` 被置位时提前返回。

    总时长与一次性等待相同，只是每段不超过 ``slice_s``，两段之间 Python 能处理停止信号。

    :param seconds: 等待秒数，小于等于 0 时不等待。
    :param stop_event: 可选的停止事件。
    :param slice_s: 每段最长秒数。
    :return: 等满返回 True；停止事件被置位返回 False。
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        chunk = min(slice_s, remaining)
        if stop_event is None:
            time.sleep(chunk)
        elif stop_event.wait(chunk):
            return False


def responsive_sleep(seconds: float) -> None:
    """可直接替换 ``time.sleep`` 的分段等待，供各处可注入的 ``sleep`` 参数做默认值。

    :param seconds: 等待秒数。
    """
    sleep(seconds)

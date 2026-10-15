"""计时工具：截止时间与可中断等待。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


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


def sleep(seconds: float, stop_event: threading.Event | None = None) -> bool:
    """等待指定秒数；若 stop_event 被置位则提前返回 False，正常等满返回 True。"""
    if seconds <= 0:
        return not (stop_event and stop_event.is_set())
    if stop_event is None:
        time.sleep(seconds)
        return True
    return not stop_event.wait(seconds)

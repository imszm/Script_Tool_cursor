"""滑动窗口频率计数（N 秒内出现 M 次即触发）。"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class SlidingWindowCounter:
    def __init__(self, window_s: float, threshold: int, clock: Callable[[], float] = time.monotonic) -> None:
        if window_s <= 0:
            raise ValueError("window_s 必须 > 0")
        if threshold <= 0:
            raise ValueError("threshold 必须 > 0")
        self.window_s = window_s
        self.threshold = threshold
        self._clock = clock
        self._hits: deque[float] = deque()

    def hit(self) -> bool:
        """记录一次命中；窗口内累计次数达到阈值时返回 True。"""
        now = self._clock()
        self._hits.append(now)
        self._evict(now)
        return len(self._hits) >= self.threshold

    def count(self) -> int:
        self._evict(self._clock())
        return len(self._hits)

    def reset(self) -> None:
        self._hits.clear()

    def _evict(self, now: float) -> None:
        limit = now - self.window_s
        while self._hits and self._hits[0] < limit:
            self._hits.popleft()

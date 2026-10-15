"""MCB 影子寄存器心跳（L5 MCB 在测试模式下需周期性刷新控制寄存器）。"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass

from ppx_testkit.core.protocol.region_client import RegionClient
from ppx_testkit.exceptions import HardwareError, ProtocolError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowReg:
    reg: int
    field: str
    skip_when_zero: bool = False


class ShadowHeartbeat:
    """后台线程按固定周期把影子值写入设备。

    写失败不会被静默吞掉：每次失败都记录日志，连续失败达到 ``max_consecutive_failures``
    时置位 :attr:`failed_event`，由主流程决定是否中止测试。
    """

    def __init__(
        self,
        client: RegionClient,
        regs: list[ShadowReg],
        *,
        period_s: float = 0.15,
        max_consecutive_failures: int = 20,
    ) -> None:
        self.client = client
        self.regs = regs
        self.period_s = period_s
        self.max_consecutive_failures = max_consecutive_failures
        self.values: dict[int, int] = {r.reg: 0 for r in regs}
        self._lock = threading.Lock()
        self._paused = threading.Event()
        self._stop = threading.Event()
        self.failed_event = threading.Event()
        self.consecutive_failures = 0
        self.total_failures = 0
        self._thread: threading.Thread | None = None

    def set(self, reg: int, value: int) -> None:
        if reg not in self.values:
            raise KeyError(f"寄存器 {reg} 不在心跳影子列表中")
        with self._lock:
            self.values[reg] = value

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mcb-heartbeat", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @contextlib.contextmanager
    def paused(self, settle_s: float = 0.2) -> Iterator[None]:
        self.pause()
        self._stop.wait(settle_s)
        try:
            yield
        finally:
            self.resume()

    def tick(self) -> None:
        with self._lock:
            snapshot = dict(self.values)
        for r in self.regs:
            value = snapshot[r.reg]
            if r.skip_when_zero and value == 0:
                continue
            try:
                self.client.write(r.reg, {r.field: value}, expect_response=False, label=f"心跳{r.field}")
                self.consecutive_failures = 0
            except (HardwareError, ProtocolError) as exc:
                self.consecutive_failures += 1
                self.total_failures += 1
                log.error("心跳写寄存器 %d 失败 (连续 %d 次): %s", r.reg, self.consecutive_failures, exc)
                if self.consecutive_failures >= self.max_consecutive_failures:
                    self.failed_event.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._paused.is_set():
                self.tick()
            self._stop.wait(self.period_s)

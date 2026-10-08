"""被测设备串口日志监听。

* :class:`DeviceLogMonitor`：在调用线程内阻塞监听指定时长（开关机 / 充电压测）。
* :class:`BackgroundLogReader`：后台线程持续读取，适合“动作与监听并行”的场景（舵机 NFC 压测）。

两者都会把每一行原始输出写入 ``device_raw.log``，并把关键字事件写入 full.log。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ppx_testkit.core.monitor.keyword_rules import KeywordEvaluator, LineVerdict
from ppx_testkit.core.serial.transport import SerialTransport
from ppx_testkit.exceptions import SerialDisconnectedError, SerialTimeoutError
from ppx_testkit.logger import get_raw_logger
from ppx_testkit.utils.ansi import clean_line

log = logging.getLogger(__name__)


@dataclass
class MonitorResult:
    lines: list[str] = field(default_factory=list)
    success: str | None = None
    abort_reason: str | None = None
    abort_keep_power: bool = True
    any_data: bool = False
    disconnected: bool = False
    disconnect_error: str | None = None
    elapsed_s: float = 0.0
    first_data_after_s: float | None = None
    success_after_s: float | None = None

    @property
    def aborted(self) -> bool:
        return self.abort_reason is not None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _log_verdict(verdict: LineVerdict, line: str) -> None:
    for kw in verdict.infos:
        log.info("信息关键字 [%s] -> %s", kw, line)
    for kw in verdict.exceptions:
        log.error("异常关键字 [%s] -> %s", kw, line)
    if verdict.success:
        log.info("成功关键字命中 [%s](%s) -> %s", verdict.success,
                 "逐行" if verdict.success_source == "line" else "跨行缓冲", line)
    if verdict.abort_reason:
        log.error("熔断: %s -> %s", verdict.abort_reason, line)


class DeviceLogMonitor:
    def __init__(
        self,
        transport: SerialTransport,
        evaluator: KeywordEvaluator,
        *,
        encoding: str = "utf-8",
        errors: str = "replace",
        raw_source: str = "device",
        no_data_warning_s: float | None = None,
        on_line: Callable[[str, LineVerdict], None] | None = None,
        poll_s: float = 0.005,
        max_lines: int = 5000,
    ) -> None:
        self.transport = transport
        self.evaluator = evaluator
        self.encoding = encoding
        self.errors = errors
        self.raw = get_raw_logger(raw_source)
        self.no_data_warning_s = no_data_warning_s
        self.on_line = on_line
        self.poll_s = poll_s
        self.max_lines = max_lines

    def flush_input(self) -> None:
        try:
            self.transport.reset_input()
            log.debug("已清空设备串口输入缓冲")
        except (SerialDisconnectedError, SerialTimeoutError) as exc:
            log.error("清空设备串口缓冲失败: %s", exc)

    def decode(self, raw: bytes) -> str:
        return raw.decode(self.encoding, errors=self.errors)

    def handle_line(self, raw: bytes, result: MonitorResult, t0: float) -> LineVerdict | None:
        decoded = self.decode(raw)
        cleaned = clean_line(decoded)
        if cleaned:
            self.raw.info(cleaned)
        stripped = cleaned.strip()
        if not stripped:
            return None
        if result.first_data_after_s is None:
            result.first_data_after_s = time.monotonic() - t0
        if len(result.lines) < self.max_lines:
            result.lines.append(stripped)
        verdict = self.evaluator.feed(stripped)
        _log_verdict(verdict, stripped)
        if self.on_line:
            self.on_line(stripped, verdict)
        return verdict

    def watch(
        self,
        duration_s: float,
        *,
        stop_on_success: bool = False,
        stop_event: threading.Event | None = None,
    ) -> MonitorResult:
        result = MonitorResult()
        t0 = time.monotonic()
        end = t0 + max(0.0, duration_s)
        warned = False
        while time.monotonic() < end:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                waiting = self.transport.in_waiting
                if waiting:
                    raw = self.transport.readline()
                    if not raw:
                        continue
                    result.any_data = True
                    verdict = self.handle_line(raw, result, t0)
                    if verdict is None:
                        continue
                    if verdict.success and result.success is None:
                        result.success = verdict.success
                        result.success_after_s = time.monotonic() - t0
                    if verdict.should_abort:
                        result.abort_reason = verdict.abort_reason
                        result.abort_keep_power = verdict.abort_keep_power
                        break
                    if stop_on_success and result.success:
                        break
                else:
                    if (
                        self.no_data_warning_s is not None
                        and not result.any_data
                        and not warned
                        and time.monotonic() - t0 >= self.no_data_warning_s
                    ):
                        warned = True
                        log.error(
                            "[串口静默预警] 监听 %.1fs 后 %s 仍未收到任何字节。请检查: ①串口接线 ②设备 UART-TX ③串口号是否正确",
                            self.no_data_warning_s, self.transport.port,
                        )
                    time.sleep(self.poll_s)
            except (SerialDisconnectedError, SerialTimeoutError) as exc:
                log.error("设备串口读取失败: %s", exc)
                result.disconnected = True
                result.disconnect_error = str(exc)
                break
        result.elapsed_s = time.monotonic() - t0
        if result.success is None and self.evaluator.cycle_success:
            result.success = self.evaluator.cycle_success
        return result


class BackgroundLogReader(threading.Thread):
    """后台持续读取设备日志。

    * 每行回调 ``on_line(line, verdict)``；
    * 命中熔断规则时置位 :attr:`abort_event` 并记录 :attr:`abort_reason`；
    * 串口断开时按 ``reconnect`` 回调尝试恢复，失败则置位 :attr:`failed_event`。
    """

    def __init__(
        self,
        monitor: DeviceLogMonitor,
        *,
        reconnect: Callable[[], bool] | None = None,
        reconnect_interval_s: float = 3.0,
        name: str = "device-log-reader",
        recent_lines: int = 200,
    ) -> None:
        super().__init__(name=name, daemon=True)
        self.monitor = monitor
        self.reconnect = reconnect
        self.reconnect_interval_s = reconnect_interval_s
        self.stop_event = threading.Event()
        self.abort_event = threading.Event()
        self.failed_event = threading.Event()
        self.abort_reason: str | None = None
        self.abort_keep_power = True
        self.recent: deque[str] = deque(maxlen=recent_lines)
        self._lock = threading.Lock()

    def snapshot(self, clear: bool = False) -> list[str]:
        with self._lock:
            data = list(self.recent)
            if clear:
                self.recent.clear()
            return data

    def stop(self, timeout: float = 3.0) -> None:
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout)

    def run(self) -> None:
        t0 = time.monotonic()
        scratch = MonitorResult()
        transport = self.monitor.transport
        while not self.stop_event.is_set():
            try:
                if transport.in_waiting:
                    raw = transport.readline()
                    if not raw:
                        continue
                    scratch.lines.clear()
                    verdict = self.monitor.handle_line(raw, scratch, t0)
                    if verdict is None:
                        continue
                    with self._lock:
                        self.recent.append(verdict.line)
                    if verdict.should_abort and not self.abort_event.is_set():
                        self.abort_reason = verdict.abort_reason
                        self.abort_keep_power = verdict.abort_keep_power
                        self.abort_event.set()
                else:
                    time.sleep(self.monitor.poll_s)
            except (SerialDisconnectedError, SerialTimeoutError) as exc:
                log.error("后台日志串口异常: %s", exc)
                if self.reconnect is None:
                    self.failed_event.set()
                    return
                recovered = False
                while not self.stop_event.is_set():
                    if self.stop_event.wait(self.reconnect_interval_s):
                        return
                    try:
                        recovered = self.reconnect()
                    except Exception:  # noqa: BLE001 - 重连回调异常不应杀死监听线程
                        log.exception("重连回调异常")
                        recovered = False
                    if recovered:
                        log.info("后台日志串口已恢复")
                        break
                if not recovered:
                    return
            except Exception:  # noqa: BLE001 - 线程内任何异常都必须通知主流程，而不是静默退出
                log.exception("后台日志线程异常退出")
                self.failed_event.set()
                return

"""设备日志按块后台读取。

充电口大约每 100ms 吐出一大批日志。主线程若用 readline 逐行读，再在继电器
两次发令之间休眠，关键字会被截断或留在驱动缓冲里。本类用独立线程按
``in_waiting`` 整段抽走，先匹配再把原文交给日志线程，磁盘变慢不影响判定。

约定：先拿 ``_state``，再调用串口（串口内部自有锁）。禁止反序。
"""

from __future__ import annotations

import codecs
import logging
import queue
import threading
import time
from collections.abc import Callable

from ppx_testkit.core.monitor.chunk_matcher import ChunkMatcher, ChunkVerdict
from ppx_testkit.core.monitor.keyword_rules import KeywordConfig
from ppx_testkit.core.monitor.line_reader import MonitorResult
from ppx_testkit.core.serial.transport import SerialTransport
from ppx_testkit.exceptions import SerialDisconnectedError, SerialPortError, SerialTimeoutError
from ppx_testkit.logger import get_raw_logger
from ppx_testkit.utils.ansi import AnsiStreamFilter

log = logging.getLogger(__name__)

_RAW_QUEUE_MAX = 20000
_DRAIN_MAX_BYTES = 2 * 1024 * 1024


class ChunkLogMonitor:
    """后台抽空设备串口，并在指定时间窗内给出与 :class:`MonitorResult` 相同的结论。"""

    def __init__(
        self,
        transport: SerialTransport,
        keywords: KeywordConfig,
        *,
        encoding: str = "utf-8",
        errors: str = "ignore",
        raw_source: str = "device",
        read_bytes: int = 256 * 1024,
        drain_grace_s: float = 0.05,
        read_idle_s: float = 0.001,
        digit_boundary: bool = False,
        reconnect_delay_s: float = 3.0,
        resolve_port: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport
        self.encoding = encoding
        self.errors = errors
        self.read_bytes = read_bytes
        self.drain_grace_s = drain_grace_s
        self.read_idle_s = read_idle_s
        self.reconnect_delay_s = reconnect_delay_s
        self._resolve_port = resolve_port
        self._clock = clock
        self.matcher = ChunkMatcher(keywords, digit_boundary=digit_boundary, clock=clock)
        self.raw = get_raw_logger(raw_source)
        self.reconnects = 0
        self._state = threading.Lock()
        self._reader_stop = threading.Event()
        self._writer_stop = threading.Event()
        self._stop_event = threading.Event()
        self._stop_reason: str | None = None
        self._stop_keep_power = False
        self._ansi = AnsiStreamFilter()
        self._decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
        self._raw_queue: queue.Queue[str] = queue.Queue(maxsize=_RAW_QUEUE_MAX)
        self._raw_drop_count = 0
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None

    @property
    def exception_count(self) -> int:
        """累计异常关键字次数，跨轮不清空。"""
        return self.matcher.exception_count

    def start(self) -> None:
        """先启动原始日志线程，再启动读线程，避免开跑瞬间队列无人消费。"""
        if self._reader is not None or self._writer is not None:
            return
        self._reader_stop.clear()
        self._writer_stop.clear()
        self._writer = threading.Thread(target=self._writer_loop, name="charge-raw-log", daemon=True)
        self._reader = threading.Thread(target=self._reader_loop, name="charge-chunk-reader", daemon=True)
        self._writer.start()
        self._reader.start()
        log.info("设备日志按块读取已启动，原文写入 device_raw.log")

    def stop(self, timeout: float = 5.0) -> None:
        """停止读线程和日志线程。重复调用无效果。"""
        self._reader_stop.set()
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout)
            if reader.is_alive():
                log.warning("读取线程未在时限内退出")
        self._writer_stop.set()
        writer, self._writer = self._writer, None
        if writer is not None and writer.is_alive() and writer is not threading.current_thread():
            writer.join(timeout)
            if writer.is_alive():
                log.warning("原始日志线程未在时限内退出")

    def prepare_listen(self) -> None:
        """丢掉上一轮残留后再接收本轮成功关键字。清空和打开判定在同一把锁里完成。"""
        err: BaseException | None = None
        with self._state:
            try:
                if self.transport.is_open:
                    self.transport.reset_input()
                    self.transport.reset_output()
            except (SerialDisconnectedError, SerialTimeoutError) as exc:
                err = exc
            self._decoder.reset()
            self._ansi.reset()
            self.matcher.reset_cycle()
            self._stop_event.clear()
            self._stop_reason = None
        if err is not None:
            log.error("清空设备串口缓冲失败: %s", err)

    def watch(
        self,
        duration_s: float,
        *,
        stop_on_success: bool = False,
        stop_event: threading.Event | None = None,
    ) -> MonitorResult:
        """主线程只等结果。字节由读线程持续抽走。

        :param duration_s: 本段最长等待秒数。
        :param stop_on_success: 为真时，命中成功关键字立即返回，并停止接收后续成功词。
        :return: 本段的成功词、熔断原因和是否断开。断开由读线程自行重连，这里不因此失败。
        """
        result = MonitorResult()
        t0 = self._clock()
        end = t0 + max(0.0, duration_s)
        while self._clock() < end:
            if stop_event is not None and stop_event.is_set():
                break
            with self._state:
                if self._stop_event.is_set():
                    self._fill_abort(result)
                    result.elapsed_s = self._clock() - t0
                    return result
                if stop_on_success and self.matcher.success:
                    self._fill_success(result, t0)
                    self.matcher.accept_success = False
                    result.elapsed_s = self._clock() - t0
                    return result
            time.sleep(0.02)

        self._drain()
        with self._state:
            if stop_on_success:
                self.matcher.accept_success = False
            if self._stop_event.is_set():
                self._fill_abort(result)
            elif stop_on_success and self.matcher.success:
                self._fill_success(result, t0)
        result.elapsed_s = self._clock() - t0
        return result

    # ------------------------------------------------------------ 内部
    def _fill_success(self, result: MonitorResult, t0: float) -> None:
        result.success = self.matcher.success
        result.any_data = True
        if result.success_after_s is None:
            result.success_after_s = self._clock() - t0

    def _fill_abort(self, result: MonitorResult) -> None:
        result.abort_reason = self._stop_reason
        result.abort_keep_power = self._stop_keep_power

    def _note_verdict(self, verdict: ChunkVerdict) -> None:
        """调用方已持有 ``_state``。把熔断记到停止事件，供主线程查询。"""
        if verdict.success:
            log.info("捕获目标指令: %s", verdict.success)
        for kw in verdict.exceptions:
            log.error("[异常] %s | %s", kw, verdict.snippet)
        if verdict.abort_reason and not self._stop_event.is_set():
            self._stop_reason = verdict.abort_reason
            self._stop_keep_power = verdict.abort_keep_power
            self._stop_event.set()
            log.error(verdict.abort_reason)

    def _ingest_locked(self, data: bytes) -> None:
        """调用方已持有 ``_state``。先匹配，再把原文送进队列。"""
        if not data:
            return
        text = self._decoder.decode(data)
        if not text:
            return
        text = self._ansi.feed(text)
        text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        self._note_verdict(self.matcher.feed(text))
        try:
            self._raw_queue.put_nowait(text)
        except queue.Full:
            self._raw_drop_count += 1
            if self._raw_drop_count == 1 or self._raw_drop_count % 1000 == 0:
                log.warning(
                    "原始日志队列已满，累计丢弃 %d 段；关键字检测不受影响", self._raw_drop_count
                )

    def _read_and_ingest(self) -> tuple[bool, BaseException | None]:
        err: BaseException | None = None
        got = False
        with self._state:
            data = b""
            try:
                data = self.transport.read_waiting(self.read_bytes)
            except (SerialDisconnectedError, SerialTimeoutError) as exc:
                err = exc
            if data:
                got = True
                self._ingest_locked(data)
        return got, err

    def _reader_loop(self) -> None:
        while not self._reader_stop.is_set():
            try:
                got, err = self._read_and_ingest()
                if err is not None:
                    log.error("设备串口读取异常: %s", err)
                    self._reconnect()
                    continue
                if not got:
                    time.sleep(self.read_idle_s)
            except Exception:
                log.exception("读取线程异常")
                time.sleep(0.05)

    def _reconnect(self) -> None:
        """休眠期间不占 ``_state``，避免堵住换轮清空。失败只记录，不中止整场测试。"""
        if self._reader_stop.is_set():
            return
        self.reconnects += 1
        log.warning("设备断开，%.1fs 后重连 (次序: %d)", self.reconnect_delay_s, self.reconnects)
        try:
            self.transport.close()
        except Exception:
            log.exception("关闭已断开的设备串口失败")
        deadline = time.monotonic() + self.reconnect_delay_s
        while time.monotonic() < deadline:
            if self._reader_stop.is_set():
                return
            time.sleep(0.1)
        if self._reader_stop.is_set():
            return
        with self._state:
            self._decoder.reset()
            self._ansi.reset()
            self.matcher.clear_tail()
        try:
            if self._resolve_port is not None:
                self.transport.port = self._resolve_port()
            self.transport.open()
        except SerialPortError as exc:
            log.error("重连失败: %s", exc)
            return
        except Exception:
            log.exception("重连时发生未知异常")
            return
        log.info("重连成功: %s", self.transport.port)

    def _drain(self) -> None:
        """窗口到期时把驱动缓冲里已经到达的数据读完。缓冲已空就结束，不等满宽限时间。"""
        deadline = time.monotonic() + self.drain_grace_s
        drained = 0
        empty_polls = 0
        with self._state:
            try:
                while drained < _DRAIN_MAX_BYTES and time.monotonic() < deadline:
                    if not self.transport.is_open:
                        break
                    data = self.transport.read_waiting(self.read_bytes)
                    if not data:
                        empty_polls += 1
                        if empty_polls >= 2:
                            break
                        time.sleep(0.002)
                        continue
                    empty_polls = 0
                    drained += len(data)
                    self._ingest_locked(data)
            except (SerialDisconnectedError, SerialTimeoutError) as exc:
                log.error("监听结束时读取剩余缓冲失败: %s", exc)

    def _writer_loop(self) -> None:
        """按行写入 device_raw.log。一行被拆进两次 read() 时，只在拼完整后记一次。"""
        pending = ""
        last_flush = time.monotonic()

        def emit(force: bool) -> None:
            nonlocal pending, last_flush
            if not pending:
                return
            if force or "\n" in pending or time.monotonic() - last_flush >= 0.1 or len(pending) >= 32768:
                parts = pending.split("\n")
                if force:
                    pending = ""
                    lines = parts
                else:
                    pending = parts.pop()
                    lines = parts
                for line in lines:
                    if line:
                        self.raw.info(line)
                last_flush = time.monotonic()

        while True:
            stop = self._writer_stop.is_set()
            try:
                pending += self._raw_queue.get(timeout=0.1)
            except queue.Empty:
                if stop and self._raw_queue.empty():
                    break
            emit(force=False)
            if stop and self._raw_queue.empty() and "\n" not in pending:
                break
        emit(force=True)

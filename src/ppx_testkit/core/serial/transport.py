"""串口传输层。

约束：
* pyserial 抛出的 ``SerialException`` / ``OSError`` / ``ValueError`` 一律转换为
  :mod:`ppx_testkit.exceptions` 中带端口号的异常，调用方无需再捕获 pyserial 类型；
* :meth:`SerialTransport.close` 幂等且绝不抛出，可安全放在 ``finally`` 中；
* 所有收发在 DEBUG 级别以十六进制记录到 full.log（可通过 ``log_traffic`` 关闭）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import serial

from ppx_testkit.core.serial.port_finder import PortFinder, PortMatcher
from ppx_testkit.exceptions import (
    ConfigError,
    SerialDisconnectedError,
    SerialOpenError,
    SerialTimeoutError,
)

R = TypeVar("R")

SerialFactory = Callable[..., Any]


def default_factory(port: str, **kwargs: Any) -> Any:
    # serial_for_url 同时支持 "COM5"、"/dev/ttyUSB0" 与 "loop://" 等 URL
    return serial.serial_for_url(port, **kwargs)


@dataclass(frozen=True)
class SerialEndpoint:
    match: PortMatcher = field(default_factory=PortMatcher)
    baudrate: int = 115200
    timeout: float = 0.1
    write_timeout: float = 0.5
    open_retries: int = 3
    retry_delay_s: float = 1.0
    encoding: str = "utf-8"
    encoding_errors: str = "replace"
    log_traffic: bool = True

    def __post_init__(self) -> None:
        if self.baudrate <= 0:
            raise ConfigError(f"波特率必须 > 0，实际 {self.baudrate}")
        if self.timeout < 0 or self.write_timeout < 0:
            raise ConfigError("timeout / write_timeout 不能为负数")
        if self.open_retries < 1:
            raise ConfigError("open_retries 至少为 1")
        try:
            "".encode(self.encoding)
        except LookupError as exc:
            raise ConfigError(f"未知编码: {self.encoding}") from exc


class SerialTransport:
    def __init__(
        self,
        port: str,
        baudrate: int,
        *,
        timeout: float = 0.1,
        write_timeout: float = 0.5,
        open_retries: int = 3,
        retry_delay_s: float = 1.0,
        name: str = "serial",
        log_traffic: bool = True,
        factory: SerialFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.open_retries = max(1, open_retries)
        self.retry_delay_s = retry_delay_s
        self.name = name
        self.log_traffic = log_traffic
        self._factory = factory or default_factory
        self._sleep = sleep
        self._ser: Any = None
        self._lock = threading.RLock()
        self.log = logging.getLogger(f"ppx_testkit.serial.{name}")

    # ------------------------------------------------------------ 生命周期
    @classmethod
    def from_endpoint(cls, ep: SerialEndpoint, port: str, *, name: str, **kwargs: Any) -> SerialTransport:
        return cls(
            port,
            ep.baudrate,
            timeout=ep.timeout,
            write_timeout=ep.write_timeout,
            open_retries=ep.open_retries,
            retry_delay_s=ep.retry_delay_s,
            name=name,
            log_traffic=ep.log_traffic,
            **kwargs,
        )

    @property
    def is_open(self) -> bool:
        ser = self._ser
        try:
            return bool(ser is not None and ser.is_open)
        except Exception:  # noqa: BLE001 - 句柄异常时视为已关闭
            return False

    def open(self) -> None:
        with self._lock:
            if self.is_open:
                return
            last_exc: BaseException | None = None
            for attempt in range(1, self.open_retries + 1):
                try:
                    self._ser = self._factory(
                        self.port,
                        baudrate=self.baudrate,
                        timeout=self.timeout,
                        write_timeout=self.write_timeout,
                    )
                    self.log.info("串口已打开 %s @ %d (第 %d 次尝试)", self.port, self.baudrate, attempt)
                    return
                except (serial.SerialException, OSError, ValueError) as exc:
                    last_exc = exc
                    self._ser = None
                    self.log.warning("打开串口 %s 失败 (第 %d/%d 次): %s", self.port, attempt, self.open_retries, exc)
                    if attempt < self.open_retries:
                        self._sleep(self.retry_delay_s)
            raise SerialOpenError(self.port, f"重试 {self.open_retries} 次后仍无法打开: {last_exc}") from last_exc

    def close(self) -> None:
        with self._lock:
            ser, self._ser = self._ser, None
            if ser is None:
                return
            try:
                ser.close()
                self.log.info("串口已关闭 %s", self.port)
            except Exception as exc:  # noqa: BLE001 - close 必须吞掉异常以保证 finally 链路完整
                self.log.warning("关闭串口 %s 时发生异常（已忽略）: %s", self.port, exc)

    def reconnect(self, new_port: str | None = None, delay_s: float = 0.0) -> None:
        with self._lock:
            self.close()
            if delay_s > 0:
                self._sleep(delay_s)
            if new_port:
                self.port = new_port
            self.open()

    def __enter__(self) -> SerialTransport:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ 内部
    def _require_open(self) -> Any:
        ser = self._ser
        if ser is None:
            raise SerialDisconnectedError(self.port, "串口未打开")
        return ser

    def _guard(self, op: str, fn: Callable[[Any], R]) -> R:
        with self._lock:
            ser = self._require_open()
            try:
                return fn(ser)
            except serial.SerialTimeoutException as exc:
                raise SerialTimeoutError(self.port, f"{op} 超时: {exc}") from exc
            except (serial.SerialException, OSError, ValueError, TypeError, AttributeError) as exc:
                # TypeError/AttributeError: pyserial 在句柄被系统回收后可能抛出
                self.log.error("串口 %s %s 失败，判定为断开: %s", self.port, op, exc)
                raise SerialDisconnectedError(self.port, f"{op} 失败: {exc}") from exc

    # ------------------------------------------------------------ 读写
    def write(self, data: bytes, *, flush: bool = True) -> int:
        def _w(ser: Any) -> int:
            n = ser.write(data)
            if flush:
                ser.flush()
            return int(n or 0)

        written = self._guard("写入", _w)
        if self.log_traffic:
            self.log.debug("TX %s", data.hex(" ").upper())
        if written != len(data):
            raise SerialTimeoutError(self.port, f"仅写入 {written}/{len(data)} 字节")
        return written

    @property
    def in_waiting(self) -> int:
        return self._guard("查询缓冲", lambda s: int(s.in_waiting))

    def read(self, size: int = 1) -> bytes:
        data = self._guard("读取", lambda s: bytes(s.read(size)))
        if data and self.log_traffic:
            self.log.debug("RX %s", data.hex(" ").upper())
        return data

    def read_available(self) -> bytes:
        def _r(ser: Any) -> bytes:
            n = int(ser.in_waiting)
            return bytes(ser.read(n)) if n else b""

        data = self._guard("读取", _r)
        if data and self.log_traffic:
            self.log.debug("RX %s", data.hex(" ").upper())
        return data

    def readline(self) -> bytes:
        data = self._guard("读取行", lambda s: bytes(s.readline()))
        if data and self.log_traffic:
            self.log.debug("RX %s", data.hex(" ").upper())
        return data

    def reset_input(self) -> None:
        self._guard("清空输入缓冲", lambda s: s.reset_input_buffer())

    def reset_output(self) -> None:
        self._guard("清空输出缓冲", lambda s: s.reset_output_buffer())

    def read_until(
        self,
        timeout_s: float,
        is_complete: Callable[[bytes], bool] | None = None,
        *,
        poll_s: float = 0.005,
        idle_gap_s: float | None = None,
    ) -> bytes:
        """在 timeout_s 内累积读取，直到 ``is_complete(buf)`` 为真或超时。

        ``idle_gap_s``：已收到数据后若持续该时长无新数据，也视为一帧结束。
        超时不抛异常，返回已收到的数据（可能为空），由调用方判定。
        """
        buf = bytearray()
        end = time.monotonic() + max(0.0, timeout_s)
        last_rx = None
        while True:
            chunk = self.read_available()
            now = time.monotonic()
            if chunk:
                buf.extend(chunk)
                last_rx = now
                if is_complete and is_complete(bytes(buf)):
                    break
            elif last_rx is not None and idle_gap_s is not None and now - last_rx >= idle_gap_s:
                break
            if now >= end:
                break
            self._sleep(poll_s)
        return bytes(buf)

    def transact(
        self,
        request: bytes,
        timeout_s: float,
        is_complete: Callable[[bytes], bool] | None = None,
        *,
        clear_input: bool = True,
        idle_gap_s: float | None = None,
    ) -> bytes:
        with self._lock:
            if clear_input:
                self.reset_input()
            self.write(request)
            return self.read_until(timeout_s, is_complete, idle_gap_s=idle_gap_s)


def open_endpoint(
    ep: SerialEndpoint,
    *,
    name: str,
    finder: PortFinder | None = None,
    exclude: Iterable[str] = (),
    factory: SerialFactory | None = None,
    auto_open: bool = True,
) -> SerialTransport:
    """按端点配置定位端口并（可选）打开串口。"""
    port = (finder or PortFinder()).find(ep.match, exclude=exclude, name=name)
    transport = SerialTransport.from_endpoint(ep, port, name=name, factory=factory)
    if auto_open:
        transport.open()
    return transport

"""公共测试夹具：假串口、内存配置、日志隔离。全部测试无需真实硬件。"""

from __future__ import annotations

import logging
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import serial

from ppx_testkit.core.serial.transport import SerialTransport
from ppx_testkit.logger import shutdown_logging
from ppx_testkit.settings import AppSettings, LoggingSettings
from ppx_testkit.utils import paths


class FakeSerial:
    """模拟 pyserial 句柄。

    * ``feed(data)`` 注入设备输出；``tx`` 记录写入的每一帧；
    * ``responder(frame) -> bytes|None`` 可模拟“请求-应答”设备；
    * ``fail_on`` 指定在某操作上抛出的异常（如 ``{"read": serial.SerialException("gone")}``）。
    """

    def __init__(self, port: str = "FAKE", **kwargs: Any) -> None:
        self.port = port
        self.kwargs = kwargs
        self.is_open = True
        self.rx = bytearray()
        self.tx: list[bytes] = []
        self.responder: Callable[[bytes], bytes | None] | None = None
        self.fail_on: dict[str, BaseException] = {}
        self.short_write = False
        self.close_calls = 0

    def _maybe_fail(self, op: str) -> None:
        exc = self.fail_on.get(op)
        if exc is not None:
            raise exc

    def feed(self, data: bytes | str) -> None:
        self.rx.extend(data.encode() if isinstance(data, str) else data)

    def write(self, data: bytes) -> int:
        self._maybe_fail("write")
        self.tx.append(bytes(data))
        if self.responder is not None:
            resp = self.responder(bytes(data))
            if resp:
                self.rx.extend(resp)
        return len(data) - 1 if self.short_write else len(data)

    def flush(self) -> None:
        self._maybe_fail("flush")

    @property
    def in_waiting(self) -> int:
        self._maybe_fail("in_waiting")
        return len(self.rx)

    def read(self, size: int = 1) -> bytes:
        self._maybe_fail("read")
        out = bytes(self.rx[:size])
        del self.rx[:size]
        return out

    def readline(self) -> bytes:
        self._maybe_fail("readline")
        idx = self.rx.find(b"\n")
        end = len(self.rx) if idx < 0 else idx + 1
        out = bytes(self.rx[:end])
        del self.rx[:end]
        return out

    def reset_input_buffer(self) -> None:
        self._maybe_fail("reset_input")
        self.rx.clear()

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.close_calls += 1
        self._maybe_fail("close")
        self.is_open = False


class FakeSerialFactory:
    """可注入 SerialTransport 的工厂；记录每次打开的句柄，支持前 N 次打开失败。"""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.opened: list[FakeSerial] = []
        self.attempts = 0
        self.setup: Callable[[FakeSerial], None] | None = None

    def __call__(self, port: str, **kwargs: Any) -> FakeSerial:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise serial.SerialException(f"could not open port {port}")
        handle = FakeSerial(port, **kwargs)
        if self.setup:
            self.setup(handle)
        self.opened.append(handle)
        return handle

    @property
    def last(self) -> FakeSerial:
        return self.opened[-1]


@pytest.fixture
def fake_factory() -> FakeSerialFactory:
    return FakeSerialFactory()


@pytest.fixture
def make_transport(fake_factory: FakeSerialFactory) -> Callable[..., SerialTransport]:
    def _make(port: str = "FAKE", *, open: bool = True, **kwargs: Any) -> SerialTransport:
        kwargs.setdefault("sleep", lambda _s: None)
        t = SerialTransport(port, 115200, factory=fake_factory, **kwargs)
        if open:
            t.open()
        return t

    return _make


def make_settings(data: dict[str, Any], *, station: str = "test_station", app: str = "dummy") -> AppSettings:
    merged = {"station": station, "app": app, **data}
    return AppSettings(
        station=station,
        app=app,
        description=str(merged.get("description", "")),
        logging=LoggingSettings(),
        data=types.MappingProxyType(merged),
        sources=("<memory>",),
    )


@pytest.fixture
def settings_factory() -> Callable[..., AppSettings]:
    return make_settings


@pytest.fixture
def project_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把项目根目录切换到临时目录（含空的 config/stations/）。"""
    (tmp_path / "config" / "stations").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv(paths.ENV_ROOT, str(tmp_path))
    paths.project_root.cache_clear()
    yield tmp_path
    paths.project_root.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_logging() -> Iterator[None]:
    yield
    shutdown_logging()
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("ppx.raw"):
            lg = logging.getLogger(name)
            lg.handlers.clear()

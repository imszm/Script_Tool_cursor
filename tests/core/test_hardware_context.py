"""HardwareContext 的紧急复位顺序：钩子逆序执行，单个钩子失败不挡住后续断电和串口关闭。"""

from __future__ import annotations

import logging
import signal
import threading

import pytest

from ppx_testkit.core.context import HardwareContext
from ppx_testkit.core.runner.base_station import BaseStation


class _FakeSerial:
    """用上下文协议模拟串口。退出时记下关闭，不打开真实端口。"""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.closed = False

    def __enter__(self) -> _FakeSerial:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.closed = True
        self.order.append("close-serial")


def test_emergency_stops_run_lifo_when_body_raises() -> None:
    order: list[str] = []
    serial_port = _FakeSerial(order)

    def hook(label: str) -> None:
        order.append(label)

    with pytest.raises(RuntimeError, match="测试中断"):
        with HardwareContext() as hw:
            hw.enter_resource(serial_port)
            hw.register_emergency_stop(lambda: hook("stop-a"), name="stop-a")
            hw.register_emergency_stop(lambda: hook("stop-b"), name="stop-b")
            hw.register_emergency_stop(lambda: hook("stop-c"), name="stop-c")
            raise RuntimeError("测试中断")

    assert order == ["stop-c", "stop-b", "stop-a", "close-serial"]
    assert serial_port.closed
    hw.close_safely()
    assert order == ["stop-c", "stop-b", "stop-a", "close-serial"]


def test_failing_hook_does_not_block_later_hooks_or_serial_close(caplog: pytest.LogCaptureFixture) -> None:
    order: list[str] = []
    serial_port = _FakeSerial(order)

    def boom() -> None:
        order.append("boom")
        raise RuntimeError("断电钩子失败")

    def relay_off() -> None:
        order.append("relay-off")

    with caplog.at_level(logging.ERROR, logger="ppx_testkit.core.context"):
        with HardwareContext() as hw:
            hw.enter_resource(serial_port)
            hw.register_emergency_stop(relay_off, name="relay-off")
            hw.register_emergency_stop(boom, name="boom")

    assert order == ["boom", "relay-off", "close-serial"]
    assert serial_port.closed
    assert "紧急复位钩子执行失败 [boom]" in caplog.text


def test_base_station_execute_still_resets_and_restores_signals() -> None:
    order: list[str] = []
    previous = signal.getsignal(signal.SIGINT)

    class _Demo(BaseStation):
        def setup(self) -> None:
            self.hw.register_emergency_stop(lambda: order.append("stop"), name="stop")

        def run(self) -> None:
            raise RuntimeError("步骤失败")

    with pytest.raises(RuntimeError, match="步骤失败"):
        _Demo().execute()

    assert order == ["stop"]
    assert signal.getsignal(signal.SIGINT) == previous


def test_base_station_skips_signals_off_main_thread() -> None:
    errors: list[BaseException] = []
    done = threading.Event()

    class _Demo(BaseStation):
        def setup(self) -> None:
            return None

        def run(self) -> None:
            return None

    def worker() -> None:
        try:
            _Demo().execute()
        except BaseException as exc:  # noqa: BLE001 - 线程里把异常带回主线程断言
            errors.append(exc)
        finally:
            done.set()

    threading.Thread(target=worker, name="station-off-main").start()
    assert done.wait(2)
    assert errors == []

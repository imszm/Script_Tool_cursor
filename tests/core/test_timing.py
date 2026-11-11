"""分段等待：总时长不变、每段不超过 STOP_POLL_S，停止事件能提前结束等待。"""

from __future__ import annotations

import threading
import time
import types

import pytest

from ppx_testkit.utils import timing


class _FakeClock:
    """替换 timing 模块里的 time：sleep 只推进假时钟并记下每段时长。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """返回假时钟。

        :return: 秒。
        """
        return self.now

    def sleep(self, seconds: float) -> None:
        """记下这一段并推进假时钟。

        :param seconds: 本段时长。
        """
        self.sleeps.append(seconds)
        self.now += seconds


class _NeverSetEvent:
    """从不置位的事件：wait 推进假时钟并记下超时。"""

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    def is_set(self) -> bool:
        """事件状态。

        :return: 永远为 False。
        """
        return False

    def wait(self, timeout: float) -> bool:
        """模拟等到超时。

        :param timeout: 本段时长。
        :return: 永远为 False。
        """
        self.waits.append(timeout)
        self.clock.now += timeout
        return False


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(timing, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    return clock


def test_responsive_sleep_slices_and_keeps_total(fake_clock: _FakeClock) -> None:
    timing.responsive_sleep(1.05)
    assert len(fake_clock.sleeps) >= 6
    assert max(fake_clock.sleeps) <= timing.STOP_POLL_S
    assert sum(fake_clock.sleeps) == pytest.approx(1.05)


def test_non_positive_wait_returns_at_once(fake_clock: _FakeClock) -> None:
    assert timing.sleep(0) is True
    assert timing.sleep(-1.0) is True
    stopped = threading.Event()
    stopped.set()
    assert timing.sleep(0, stopped) is False
    assert timing.sleep(5.0, stopped) is False
    assert fake_clock.sleeps == []


def test_event_wait_is_sliced(fake_clock: _FakeClock) -> None:
    event = _NeverSetEvent(fake_clock)
    assert timing.sleep(0.5, event) is True  # type: ignore[arg-type]
    assert max(event.waits) <= timing.STOP_POLL_S
    assert sum(event.waits) == pytest.approx(0.5)


def test_event_set_from_other_thread_ends_wait_early() -> None:
    stopped = threading.Event()
    timer = threading.Timer(0.05, stopped.set)
    timer.start()
    started = time.monotonic()
    try:
        assert timing.sleep(5.0, stopped) is False
    finally:
        timer.cancel()
    assert time.monotonic() - started < 1.0

"""条码键入循环：用假的上位机状态验证提前结束和通过率，不碰真实键盘。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pytest

from ppx_testkit.apps.pctool.barcode_type_loop import (
    TypeLoopConfig,
    classify_stamp,
    run_type_loop,
    stamp_pixels,
)
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.settings import load_settings


class _Script:
    """按给定顺序返回大字状态。多读一次就失败，避免测试悄悄卡住。"""

    def __init__(self, states: list[str]) -> None:
        self.states = list(states)

    def read(self) -> str:
        if not self.states:
            raise AssertionError("状态脚本提前用完")
        return self.states.pop(0)


def _clock(sleeps: list[float]) -> tuple[Callable[[float], None], Callable[[], float]]:
    """等待会把虚拟时钟往前拨，和真实的 monotonic 配合方式一样。"""
    now = [0.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    return sleep, lambda: now[0]


def _pixels(ratio: float, color: tuple[int, int, int], *, total: int = 1000) -> list[tuple[int, int, int]]:
    count = int(total * ratio)
    return [color] * count + [(240, 240, 240)] * (total - count)


def test_defaults_match_requested_loop() -> None:
    cfg = TypeLoopConfig()
    assert cfg.text == "2020005002R309GD006900364"
    assert cfg.cycles == 1000
    assert cfg.wait_s == 120.0
    assert cfg.arm_timeout_s == 20.0
    assert cfg.window_title == "皮皮熊R3_CCB"
    assert cfg.verdict_roi == (0.70, 0.78, 0.98, 0.96)


def test_config_rejects_empty_or_negative() -> None:
    with pytest.raises(ConfigError, match="text"):
        TypeLoopConfig(text="")
    with pytest.raises(ConfigError, match="cycles"):
        TypeLoopConfig(cycles=0)
    with pytest.raises(ConfigError, match="wait_s"):
        TypeLoopConfig(wait_s=0)
    with pytest.raises(ConfigError, match="poll_s"):
        TypeLoopConfig(poll_s=0)
    with pytest.raises(ConfigError, match="verdict_roi"):
        TypeLoopConfig(verdict_roi=(0.9, 0.1, 0.2, 0.8))
    with pytest.raises(ConfigError, match="window_title"):
        TypeLoopConfig(window_title="  ")


def test_stamp_color_separates_pass_fail_and_running() -> None:
    """比例取自三张上位机截图：通过约 15% 绿，不通过约 17% 红，测试中约 13% 近黑。"""
    assert classify_stamp(_pixels(0.15, (61, 235, 62))) == "pass"
    assert classify_stamp(_pixels(0.17, (212, 22, 24))) == "fail"
    assert classify_stamp(_pixels(0.13, (20, 20, 20))) == "running"
    assert classify_stamp(_pixels(0.0, (240, 240, 240))) == "unknown"
    assert classify_stamp(_pixels(0.01, (61, 235, 62))) == "unknown"
    assert classify_stamp([]) == "unknown"


def test_stamp_pixels_uses_fraction_of_the_window() -> None:
    class _Image:
        def __init__(self, width: int, height: int) -> None:
            self.size = (width, height)

        def crop(self, box: tuple[int, int, int, int]) -> _Image:
            x0, y0, x1, y1 = box
            cropped = _Image(x1 - x0, y1 - y0)
            return cropped

        def getdata(self) -> list[tuple[int, int, int]]:
            width, height = self.size
            return [(1, 2, 3)] * (width * height)

    pixels = stamp_pixels(_Image(100, 100), (0.70, 0.78, 0.98, 0.96))
    assert len(pixels) == 28 * 18
    assert pixels[0] == (1, 2, 3)


def test_pass_or_fail_starts_the_next_round_without_the_full_wait() -> None:
    sleeps: list[float] = []
    calls: list[tuple[str, float, bool]] = []
    sleep, clock = _clock(sleeps)
    script = _Script([
        "running", "pass",
        "running", "fail",
        "pass", "running", "pass",
    ])

    def type_text(text: str, *, interval: float = 0.03, enter: bool = False) -> None:
        calls.append((text, interval, enter))

    cfg = TypeLoopConfig(
        text="ABC", cycles=3, wait_s=120, startup_delay_s=5, poll_s=1, arm_timeout_s=20,
    )
    outcome = run_type_loop(cfg, type_text=type_text, read_status=script, sleep=sleep, clock=clock)
    assert outcome.summary.exit_code == 1
    assert outcome.summary.passed == 2
    assert outcome.summary.failed == 1
    assert outcome.summary.pass_rate == pytest.approx(66.666, abs=0.01)
    assert calls == [("ABC", 0.03, True)] * 3
    assert sleeps == [5, 1, 1, 1, 1]
    assert [row["verdict"] for row in outcome.rows] == ["PASS", "FAIL", "PASS"]
    assert outcome.rows[1]["detail"] == "右下角为不通过"


def test_previous_pass_is_ignored_until_the_new_round_is_running() -> None:
    sleeps: list[float] = []
    sleep, clock = _clock(sleeps)
    script = _Script(["pass", "pass", "running", "pass"])

    def type_text(text: str, *, interval: float = 0.03, enter: bool = False) -> None:
        return None

    cfg = TypeLoopConfig(text="ABC", cycles=1, wait_s=120, startup_delay_s=0, poll_s=1, arm_timeout_s=20)
    outcome = run_type_loop(cfg, type_text=type_text, read_status=script, sleep=sleep, clock=clock)
    assert outcome.summary.passed == 1
    assert outcome.summary.exit_code == 0
    assert sleeps == [1, 1, 1]


def test_already_running_still_types_immediately() -> None:
    """右下角已经是测试中时也要先键入，不能干等上一轮结束。"""
    sleeps: list[float] = []
    calls: list[str] = []
    sleep, clock = _clock(sleeps)
    script = _Script(["running", "pass"])

    def type_text(text: str, *, interval: float = 0.03, enter: bool = False) -> None:
        calls.append(text)

    cfg = TypeLoopConfig(text="ABC", cycles=1, wait_s=30, startup_delay_s=0, poll_s=10, arm_timeout_s=20)
    outcome = run_type_loop(cfg, type_text=type_text, read_status=script, sleep=sleep, clock=clock)
    assert calls == ["ABC"]
    assert outcome.summary.passed == 1
    assert outcome.summary.executed == 1
    assert sleeps == [10]


def test_stuck_on_running_still_sends_a_barcode() -> None:
    sleeps: list[float] = []
    calls: list[str] = []
    sleep, clock = _clock(sleeps)

    class _Running:
        def read(self) -> str:
            return "running"

    def type_text(text: str, *, interval: float = 0.03, enter: bool = False) -> None:
        calls.append(text)

    cfg = TypeLoopConfig(text="ABC", cycles=1, wait_s=30, startup_delay_s=0, poll_s=10, arm_timeout_s=20)
    outcome = run_type_loop(cfg, type_text=type_text, read_status=_Running(), sleep=sleep, clock=clock)
    assert calls == ["ABC"]
    assert outcome.summary.executed == 1
    assert outcome.summary.failed == 1
    assert outcome.summary.exit_code == 1
    assert sleeps == [10, 10, 10]


def test_timeout_after_running_counts_as_failure_and_can_continue() -> None:
    sleeps: list[float] = []
    calls: list[str] = []
    sleep, clock = _clock(sleeps)

    class _RunningAfterIdle:
        def read(self) -> str:
            return "running"

    def type_text(text: str, *, interval: float = 0.03, enter: bool = False) -> None:
        calls.append(text)

    cfg = TypeLoopConfig(text="ABC", cycles=2, wait_s=30, startup_delay_s=0, poll_s=10, arm_timeout_s=20)
    outcome = run_type_loop(
        cfg, type_text=type_text, read_status=_RunningAfterIdle(), sleep=sleep, clock=clock,
    )
    assert calls == ["ABC", "ABC"]
    assert outcome.summary.executed == 2
    assert outcome.summary.failed == 2
    assert outcome.summary.exit_code == 1
    assert "30 秒" in outcome.rows[0]["detail"]
    assert sleeps == [10, 10, 10, 10, 10, 10]


def test_not_seeing_running_counts_as_not_started_and_stops_after_three() -> None:
    sleeps: list[float] = []
    calls: list[str] = []
    sleep, clock = _clock(sleeps)

    class _StalePass:
        def read(self) -> str:
            return "pass"

    def type_text(text: str, *, interval: float = 0.03, enter: bool = False) -> None:
        calls.append(text)

    cfg = TypeLoopConfig(text="ABC", cycles=5, wait_s=120, startup_delay_s=0, poll_s=10, arm_timeout_s=20)
    outcome = run_type_loop(cfg, type_text=type_text, read_status=_StalePass(), sleep=sleep, clock=clock)
    assert calls == ["ABC", "ABC", "ABC"]
    assert outcome.summary.failed == 3
    assert outcome.summary.aborted
    assert outcome.summary.exit_code == 1
    assert "连续 3 轮" in (outcome.summary.abort_reason or "")


def test_interrupt_during_wait_stops_later_rounds() -> None:
    calls: list[str] = []

    def type_text(text: str, *, interval: float = 0.03, enter: bool = False) -> None:
        calls.append(text)

    class _IdleThenRunning:
        def read(self) -> str:
            return "pass" if not calls else "running"

    def sleep(_seconds: float) -> None:
        if calls:
            raise KeyboardInterrupt

    cfg = TypeLoopConfig(text="ABC", cycles=5, wait_s=120, startup_delay_s=0, poll_s=1, arm_timeout_s=20)
    outcome = run_type_loop(
        cfg, type_text=type_text, read_status=_IdleThenRunning(), sleep=sleep, clock=lambda: 0.0,
    )
    assert calls == ["ABC"]
    assert outcome.summary.interrupted
    assert outcome.summary.executed == 0
    assert outcome.summary.exit_code == 130


def test_write_reports_writes_summary_json(tmp_path) -> None:
    """收尾必须能带上 summary_name。上次实跑就是在这里抛 TypeError，报告没有落盘。"""
    from ppx_testkit.apps.pctool.common import write_reports
    from ppx_testkit.logger import RunContext

    ctx = RunContext(station="barcode_type_loop", run_id="t", run_dir=tmp_path, started_at=datetime.now())
    write_reports(
        ctx,
        "report",
        title="条码键入",
        rows=[{"cycle": 1, "verdict": "PASS", "detail": "右下角为通过", "duration_s": 44}],
        summary={"passed": 1, "failed": 0, "executed": 1, "ok": True},
        columns=["cycle", "verdict", "detail", "duration_s"],
        summary_name="summary.json",
    )
    assert (tmp_path / "report.csv").is_file()
    assert (tmp_path / "report.html").is_file()
    assert "passed" in (tmp_path / "summary.json").read_text(encoding="utf-8")


def test_station_yaml_loads_requested_values() -> None:
    settings = load_settings("barcode_type_loop", use_local=False, environ={})
    cfg = settings.section("type_loop", TypeLoopConfig)
    assert settings.app == "pctool.barcode_type_loop"
    assert cfg.text == "2020005002R309GD006900364"
    assert cfg.cycles == 10000
    assert cfg.wait_s == 120.0
    assert cfg.arm_timeout_s == 20.0
    assert cfg.poll_s == 0.5
    assert cfg.window_title == "皮皮熊R3_CCB"
    assert cfg.verdict_roi == (0.70, 0.78, 0.98, 0.96)

"""刹车、LRD 点灯、FCT/组装/PC 工具里不依赖屏幕和 DLL 的流程。"""

from __future__ import annotations

from typing import Any

import pytest

from ppx_testkit.apps.assembly.r3_assembly_upgrade import make_vin
from ppx_testkit.apps.fct.common import ColorVerdict, PointSample, classify_points, poll_decision
from ppx_testkit.apps.fct.p3_fcb import (
    ChannelState,
    P3FctConfig,
    advance,
    check_timeout,
    generate_sn_list,
    green_decision,
    register_green,
)
from ppx_testkit.apps.motor.brake_lock_unlock import (
    BrakeLockUnlockCycle,
    BrakeTestConfig,
    MotorLink,
    MotorProfile,
    OverheatConfig,
)
from ppx_testkit.apps.pctool.l5_upgrade_stress import SnRule, make_sn, next_sequence, render_text
from ppx_testkit.apps.pctool.upgrade_log_stress import classify_log, new_log_part
from ppx_testkit.apps.whitebox.lrd_debug import LrdLedConfig, apply_and_verify
from ppx_testkit.core.protocol.ble_client import BleResult
from ppx_testkit.core.protocol.ppx_types import BrakeStateV2, RegionDataV2
from ppx_testkit.exceptions import ConfigError


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _Motor:
    """按写入更新刹车状态，读寄存器返回对应字段。"""

    def __init__(self) -> None:
        self.brake = int(BrakeStateV2.CLOSED)
        self.writes: list[tuple[int, dict[str, Any]]] = []
        self.transport = type("T", (), {"reconnect": lambda *a, **k: None})()

    def write(self, reg: int, fields: dict[str, Any], **_kw: Any) -> None:
        self.writes.append((reg, dict(fields)))
        if "brake_state" in fields:
            self.brake = int(fields["brake_state"])

    def read(self, reg: int, nums: int = 1, **_kw: Any) -> RegionDataV2:
        data = RegionDataV2()
        data.mcu_errcode = 0
        data.brake_state = self.brake
        data.mosfet_temp = 40
        data.motor_temp = 50
        return data


def test_brake_round_unlock_and_lock() -> None:
    clock = _Clock()
    tested, load = _Motor(), _Motor()
    cfg = BrakeTestConfig(
        cycles=1,
        pre_run_s=0,
        run_time_min_s=0,
        run_time_max_s=0,
        stop_before_lock_s=0,
        round_interval_s=0,
        command_retry_delay_s=0,
        clear_error_on_start=True,
        overheat=OverheatConfig(enabled=False),
        tested=MotorProfile(2, -1000, 100, 10),
        load=MotorProfile(4, 1000, 100, 20),
    )
    cycle = BrakeLockUnlockCycle(
        MotorLink(tested, "被测", retries=1, retry_delay_s=0, sleep=clock.sleep),
        MotorLink(load, "负载", retries=1, retry_delay_s=0, sleep=clock.sleep),
        cfg, sleep=clock.sleep, clock=clock,
    )
    cycle.setup()
    result = cycle.run_cycle(1)
    assert result.passed, result.detail
    assert tested.brake == int(BrakeStateV2.CLOSED)
    from ppx_testkit.core.runner.result import RunSummary

    cycle.teardown(RunSummary(station="st", target_cycles=1))
    assert "1/1" in cycle.stats()["解锁成功率"]


def test_overheat_requires_hysteresis() -> None:
    with pytest.raises(ConfigError, match="回差"):
        OverheatConfig(mos_recover=90, mos_limit=85)


class _Led:
    def __init__(self, led: dict[str, int] | None = None, fail: str | None = None) -> None:
        self.led = led
        self.fail = fail
        self.written: dict[str, int] | None = None

    def set_led(self, values: dict[str, int], *, timeout_s: float | None = None) -> BleResult:
        self.written = dict(values)
        if self.fail:
            return BleResult(False, b"", None, error=self.fail)
        return BleResult(True, b"\xa5\x55", 1)

    def read_led(self, *, timeout_s: float | None = None) -> BleResult:
        return BleResult(True, b"\xa5\x55", 1, led=self.led)


def test_lrd_apply_and_verify() -> None:
    cfg = LrdLedConfig()
    board = _Led(cfg.values())
    ok, detail = apply_and_verify(board, cfg)  # type: ignore[arg-type]
    assert ok and board.written == cfg.values()
    board.led = {**cfg.values(), "digital": 1}
    ok, detail = apply_and_verify(board, cfg)  # type: ignore[arg-type]
    assert not ok and "digital" in detail
    board.fail = "SerialDisconnectedError: gone"
    ok, detail = apply_and_verify(board, cfg)  # type: ignore[arg-type]
    assert not ok and "写 LED 失败" in detail


def test_sn_and_log_helpers() -> None:
    assert generate_sn_list("P3FCT000001", 6, 2) == ["P3FCT000001", "P3FCT000002"]
    assert make_vin("VIN", 12, 5) == "VIN00012"
    with pytest.raises(ConfigError):
        make_vin("VIN", 100000, 5)
    rule = SnRule(mode="sequence", prefix="A", start=3, width=2)
    assert make_sn(rule, 2, __import__("random").Random(0)) == "A04"
    assert render_text("SN-{sn}-{index}", sn="X", index=2) == "SN-X-2"
    assert next_sequence(0, 2, passed=True) == 1
    assert next_sequence(0, 2, passed=False) == 0
    assert new_log_part("abc", "abcdef") == "def"
    assert new_log_part("abcdef", "ab") == ""
    assert classify_log("升级超时", ["失败", "超时"]) == (False, ["超时"])
    assert classify_log("升级完成", ["失败"]) == (True, [])


def test_color_and_green_state_machine() -> None:
    assert green_decision(0.3, 10, 0.2, 180)
    assert green_decision(0.0, 200, 0.2, 180)
    assert not green_decision(0.1, 10, 0.2, 180)
    samples = [PointSample((1, 1), True, False), PointSample((2, 2), False, True)]
    verdict = classify_points(samples)
    assert not verdict.all_green and verdict.has_red and verdict.not_green == [(2, 2)]
    assert poll_decision(verdict, fail_on_red=True) == "FAIL"
    assert poll_decision(ColorVerdict(True, False), fail_on_red=True) == "PASS"

    state = ChannelState(channel=1)
    assert register_green(state, False, 3) is False
    assert register_green(state, True, 2) is False
    assert register_green(state, True, 2) is True
    advance(state, "WAIT_DATABASE", 10.0)
    assert state.state == "WAIT_DATABASE" and state.green_streak == 0


def test_channel_timeout(settings_factory: Any) -> None:
    cfg = settings_factory(
        {"fct": {"sn_base": "P3FCT000001", "channels": [{
            "pass_button": [1, 1], "database_point": [1, 2],
            "final_pass_roi": [0, 0, 2, 2], "status_points": {"a": [1, 3]},
        }]}},
        app="fct.p3_fcb",
    ).section("fct", P3FctConfig)
    state = ChannelState(channel=1)
    state.start_time = 0.0
    state.phase_start_time = 0.0
    reason, tag = check_timeout(state, cfg.status_timeout_s + 1, cfg) or ("", "")
    assert "状态灯" in reason and tag == "WAIT_STATUS_TIMEOUT"

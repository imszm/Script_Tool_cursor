"""灯光类压测（转向灯 / 大灯）单元测试：纯逻辑 + 假串口端到端，无需硬件。"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import pytest
import serial

from ppx_testkit.apps.lamp import headlight, turn_signal
from ppx_testkit.apps.lamp._common import (
    IntervalCheck,
    IntervalMeter,
    LampConfig,
    LampProfile,
    LampStep,
    cycle_verdict,
    evaluate_intervals,
    pair_intervals,
    run_lamp,
    validate_profile_against_relay,
)
from ppx_testkit.core.relay.base import RelayConfig
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import from_dict, load_settings

ICSE = {
    "ch3_on": bytes.fromhex("A0 03 01 A4"),
    "ch3_off": bytes.fromhex("A0 03 00 A3"),
    "ch2_on": bytes.fromhex("A0 02 01 A3"),
    "ch2_off": bytes.fromhex("A0 02 00 A2"),
}


class FakeClock:
    """注入 sleep 与 clock：sleep 推进虚拟时间，可按时长额外“拖慢”以制造超限。"""

    def __init__(self) -> None:
        self.t = 100.0
        self.slept: list[float] = []
        self.extra: dict[float, float] = {}
        self.interrupt_on: float | None = None

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        if self.interrupt_on is not None and seconds == self.interrupt_on:
            raise KeyboardInterrupt
        self.slept.append(seconds)
        self.t += seconds + self.extra.get(seconds, 0.0)


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(station="t", run_id="r", run_dir=tmp_path, started_at=_dt.datetime.now())


def _step(**kw: Any) -> LampStep:
    return from_dict(LampStep, kw, "step")


def _all_tx(fake_factory: Any) -> list[bytes]:
    return [frame for handle in fake_factory.opened for frame in handle.tx]


HEADLIGHT_SPEC: dict[str, Any] = {
    "serial": {"relay_icse": {"baudrate": 9600, "match": {"port": "COM4"}}},
    "relays": {"icse": {"type": "icse_a0", "post_write_delay_s": 0.0}},
    "lamp": {
        "variant": "spec",
        "variants": {
            "spec": {
                "serial": "relay_icse",
                "relay": "relays.icse",
                "cycles": 3,
                "cycle_steps": [
                    {"action": "ch_on", "channel": 3, "wait_s": 0.5},
                    {"action": "ch_off", "channel": 3, "wait_s": 1.0},
                ],
                "teardown_steps": [{"action": "ch_off", "channel": 3}],
                "checks": [
                    {"name": "点亮时长", "start": "ch3_on", "end": "ch3_off", "min_s": 0.4, "max_s": 0.7},
                    {"name": "熄灭时长", "start": "ch3_off", "end": "ch3_on", "min_s": 0.9, "max_s": 1.3},
                ],
            }
        },
    },
}


# ============================================================ 配置校验
class TestStepConfig:
    def test_channel_action_requires_channel(self) -> None:
        with pytest.raises(ConfigError, match="channel"):
            _step(action="ch_on")

    def test_channel_range(self) -> None:
        with pytest.raises(ConfigError, match="通道号非法"):
            _step(action="press", channel=0)

    def test_command_requires_name(self) -> None:
        with pytest.raises(ConfigError, match="command"):
            _step(action="command")

    def test_raw_data_parsed(self) -> None:
        assert _step(action="raw", data="A0 02 01 A3").payload() == ICSE["ch2_on"]
        assert _step(action="raw", data="ascii:R").payload() == b"R"
        with pytest.raises(ConfigError):
            _step(action="raw", data="ZZ")
        with pytest.raises(ConfigError, match="data"):
            _step(action="raw")

    def test_negative_wait_rejected(self) -> None:
        with pytest.raises(ConfigError, match="负数"):
            _step(action="wait", wait_s=-1)

    def test_yaml_bool_action_rejected(self) -> None:
        # YAML 中裸写 on/off 会变成布尔值，必须给出明确的配置错误
        with pytest.raises(ConfigError, match="action"):
            _step(action=True, channel=2)

    def test_marks(self) -> None:
        assert _step(action="ch_on", channel=2).marks() == ("ch2_on",)
        assert _step(action="ch_off", channel=2, mark="x").marks() == ("ch2_off", "x")
        assert _step(action="press", channel=3).marks() == ("ch3_on", "ch3_off")
        assert _step(action="command", command="wake").marks() == ()


class TestProfileConfig:
    def test_check_range_invalid(self) -> None:
        with pytest.raises(ConfigError, match="max_s"):
            IntervalCheck(name="a", start="x", end="y", min_s=2, max_s=1)

    def test_unknown_mark_rejected(self) -> None:
        with pytest.raises(ConfigError, match="不会在 cycle_steps 中产生"):
            from_dict(LampProfile, {
                "cycle_steps": [{"action": "ch_on", "channel": 2}],
                "checks": [{"name": "a", "start": "ch2_on", "end": "ch2_off"}],
            })

    def test_setup_marks_do_not_count(self) -> None:
        with pytest.raises(ConfigError, match="不会在 cycle_steps 中产生"):
            from_dict(LampProfile, {
                "setup_steps": [{"action": "ch_off", "channel": 2}],
                "cycle_steps": [{"action": "ch_on", "channel": 2}],
                "checks": [{"name": "a", "start": "ch2_on", "end": "ch2_off"}],
            })

    def test_duplicate_check_names(self) -> None:
        with pytest.raises(ConfigError, match="重复"):
            from_dict(LampProfile, {
                "cycle_steps": [{"action": "press", "channel": 2}],
                "checks": [
                    {"name": "a", "start": "ch2_on", "end": "ch2_off"},
                    {"name": "a", "start": "ch2_off", "end": "ch2_on"},
                ],
            })

    def test_empty_cycle_steps(self) -> None:
        with pytest.raises(ConfigError, match="cycle_steps"):
            from_dict(LampProfile, {"cycle_steps": []})

    def test_unknown_variant(self) -> None:
        with pytest.raises(ConfigError, match="可用方案"):
            from_dict(LampConfig, {
                "variant": "nope",
                "variants": {"a": {"cycle_steps": [{"action": "wait", "wait_s": 1}]}},
            })

    def test_cycles_override(self) -> None:
        cfg = from_dict(LampConfig, {
            "variant": "a", "cycles": 7,
            "variants": {"a": {"cycles": 3, "cycle_steps": [{"action": "wait", "wait_s": 1}]}},
        })
        assert cfg.total_cycles() == 7

    def test_validate_against_relay(self) -> None:
        profile = from_dict(LampProfile, {"cycle_steps": [{"action": "command", "command": "left_on"}]})
        with pytest.raises(ConfigError, match="left_on"):
            validate_profile_against_relay(profile, RelayConfig(type="command", commands={"wake": 0x50}))
        validate_profile_against_relay(profile, RelayConfig(type="command", commands={"left_on": "ascii:R"}))

        ch_profile = from_dict(LampProfile, {"cycle_steps": [{"action": "press", "channel": 5}]})
        with pytest.raises(ConfigError, match="通道 5"):
            validate_profile_against_relay(ch_profile, RelayConfig(type="command", commands={"x": 1}))
        validate_profile_against_relay(ch_profile, RelayConfig(type="icse_a0"))


# ============================================================ 间隔计量与判定
class TestIntervals:
    def test_pair_distinct_marks(self) -> None:
        events = [(0.0, "on"), (0.5, "off"), (1.5, "on"), (2.1, "off"), (3.0, "off")]
        assert pair_intervals(events, "on", "off") == pytest.approx([0.5, 0.6])
        assert pair_intervals(events, "off", "on") == pytest.approx([1.0])

    def test_pair_same_mark_parity(self) -> None:
        events = [(0.0, "t"), (2.0, "t"), (4.1, "t"), (6.2, "t"), (8.0, "t")]
        assert pair_intervals(events, "t", "t") == pytest.approx([2.0, 2.1])

    def test_repeated_start_uses_latest(self) -> None:
        events = [(0.0, "on"), (1.0, "on"), (1.4, "off")]
        assert pair_intervals(events, "on", "off") == pytest.approx([0.4])

    def test_meter_streams_across_cycles_and_reset(self) -> None:
        check = IntervalCheck(name="灭", start="off", end="on")
        meter = IntervalMeter([check])
        assert meter.feed(0.0, "on") == []
        assert meter.feed(0.5, "off") == []
        done = meter.feed(1.5, "on")
        assert [(c.name, round(d, 6)) for c, d in done] == [("灭", 1.0)]
        meter.feed(2.0, "off")
        meter.reset()
        assert meter.feed(9.0, "on") == []

    def test_evaluate_and_verdict(self) -> None:
        a = IntervalCheck(name="亮", start="on", end="off", min_s=0.4, max_s=0.6)
        b = IntervalCheck(name="记录", start="off", end="on", min_s=0.0, max_s=0.1, enforce=False)
        outcomes = evaluate_intervals([(a, 0.5), (b, 1.0)], [a, b])
        assert [o.passed for o in outcomes] == [True, True]  # b 超限但仅记录
        assert outcomes[1].violations == [1.0]
        ok, detail = cycle_verdict(outcomes, [a, b])
        assert ok and "亮=0.500s" in detail

        outcomes = evaluate_intervals([(a, 0.7)], [a, b])
        ok, detail = cycle_verdict(outcomes, [a, b])
        assert not ok and "亮 超出规范" in detail and "0.700s" in detail
        assert outcomes[1].durations == []

    def test_within_open_upper_bound(self) -> None:
        c = IntervalCheck(name="x", start="a", end="b", min_s=1.0)
        assert c.within(100.0) and not c.within(0.99)
        assert c.describe_range() == "[1.000, ∞]s"


# ============================================================ 端到端（假串口）
class TestRunLamp:
    def test_headlight_spec_pass(self, settings_factory, fake_factory, tmp_path) -> None:
        clock = FakeClock()
        settings = settings_factory(HEADLIGHT_SPEC, station="headlight", app="lamp.headlight")
        code = run_lamp(settings, _ctx(tmp_path), title="大灯", serial_factory=fake_factory,
                        sleep=clock.sleep, clock=clock)
        assert code == 0
        tx = _all_tx(fake_factory)
        assert tx == [ICSE["ch3_on"], ICSE["ch3_off"]] * 3 + [ICSE["ch3_off"]]
        assert all(not h.is_open for h in fake_factory.opened)

        summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
        assert summary["passed"] == 3 and summary["ok"] is True
        stats = {s["name"]: s for s in json.loads((tmp_path / "interval_stats.json").read_text(encoding="utf-8"))}
        assert stats["点亮时长"]["count"] == 3 and stats["点亮时长"]["mean_s"] == pytest.approx(0.5)
        assert stats["熄灭时长"]["count"] == 2  # 跨轮间隔：第 2、3 轮各完成一次
        assert (tmp_path / "lamp_cycles.csv").exists() and (tmp_path / "report.html").exists()

    def test_interval_violation_fails_cycle(self, settings_factory, fake_factory, tmp_path) -> None:
        clock = FakeClock()
        clock.extra[0.5] = 0.3  # 亮灯等待被拖慢 0.3s -> 0.8s 超出 [0.4, 0.7]
        settings = settings_factory(HEADLIGHT_SPEC)
        code = run_lamp(settings, _ctx(tmp_path), title="大灯", serial_factory=fake_factory,
                        sleep=clock.sleep, clock=clock)
        assert code == 1
        summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
        assert summary["failed"] == 3
        assert "点亮时长 超出规范" in (tmp_path / "lamp_cycles.csv").read_text(encoding="utf-8-sig")

    def test_serial_failure_fails_cycle_then_aborts(self, settings_factory, fake_factory, tmp_path) -> None:
        clock = FakeClock()
        state = {"broken": False}

        def _setup(handle: Any) -> None:
            if state["broken"]:
                handle.fail_on["write"] = serial.SerialException("device gone")

        fake_factory.setup = _setup
        data = json.loads(json.dumps(HEADLIGHT_SPEC))
        data["lamp"]["variants"]["spec"]["cycles"] = 10
        settings = settings_factory(data)

        original_sleep = clock.sleep

        def _sleep(s: float) -> None:
            original_sleep(s)
            if s == 1.0 and not state["broken"]:  # 第 1 轮结束后拔掉 USB
                state["broken"] = True
                fake_factory.last.fail_on["write"] = serial.SerialException("device gone")

        code = run_lamp(settings, _ctx(tmp_path), title="大灯", serial_factory=fake_factory,
                        sleep=_sleep, clock=clock)
        assert code == 1
        summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
        assert summary["passed"] == 1
        assert summary["failed"] == 3  # max_consecutive_failures 默认 3
        assert summary["aborted"] is True
        assert all(not h.is_open for h in fake_factory.opened)

    def test_interrupt_during_press_releases_relay(self, settings_factory, fake_factory, tmp_path) -> None:
        clock = FakeClock()
        clock.interrupt_on = 0.5
        data = {
            "serial": {"relay_icse": {"match": {"port": "COM4"}}},
            "relays": {"icse": {"type": "icse_a0", "post_write_delay_s": 0.0}},
            "lamp": {"variant": "a", "variants": {"a": {
                "serial": "relay_icse", "relay": "relays.icse", "cycles": 5,
                "cycle_steps": [{"action": "press", "channel": 2, "hold_s": 0.5, "wait_s": 0.7}],
            }}},
        }
        code = run_lamp(settings_factory(data), _ctx(tmp_path), title="t", serial_factory=fake_factory,
                        sleep=clock.sleep, clock=clock)
        assert code == 1
        assert _all_tx(fake_factory) == [ICSE["ch2_on"], ICSE["ch2_off"]]
        summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
        assert summary["interrupted"] is True

    def test_open_failure_is_reported(self, settings_factory, fake_factory, tmp_path) -> None:
        fake_factory.fail_times = 99
        data = json.loads(json.dumps(HEADLIGHT_SPEC))
        data["serial"]["relay_icse"]["open_retries"] = 1
        clock = FakeClock()
        code = run_lamp(settings_factory(data), _ctx(tmp_path), title="t", serial_factory=fake_factory,
                        sleep=clock.sleep, clock=clock)
        assert code == 1
        assert fake_factory.opened == []
        summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
        assert summary["executed"] == 0 and "SerialOpenError" in summary["error"]

    def test_formal_turn_signal_station(self, fake_factory, tmp_path) -> None:
        settings = load_settings("turn_signal", overrides={"lamp": {"cycles": 2}}, use_local=False, environ={})
        clock = FakeClock()
        code = run_lamp(settings, _ctx(tmp_path), title="转向灯", serial_factory=fake_factory,
                        sleep=clock.sleep, clock=clock)
        assert code == 0
        assert _all_tx(fake_factory) == [b"\x50", b"P"] + [b"R", b"P", b"T", b"P"] * 2 + [b"P"]

    def test_left_spec_variant_bytes(self, fake_factory, tmp_path) -> None:
        settings = load_settings(
            "turn_signal", overrides={"lamp": {"variant": "left_spec", "cycles": 2}}, use_local=False, environ={}
        )
        clock = FakeClock()
        code = run_lamp(settings, _ctx(tmp_path), title="转向灯", serial_factory=fake_factory,
                        sleep=clock.sleep, clock=clock)
        assert code == 0
        assert _all_tx(fake_factory) == [b"\x50", b"\x51", b"\x50", b"\x42"] + [b"\x50", b"\x42"] * 4
        stats = {s["name"]: s for s in json.loads((tmp_path / "interval_stats.json").read_text(encoding="utf-8"))}
        assert stats["开启保持"]["mean_s"] == pytest.approx(2.1)
        assert stats["关闭保持"]["count"] == 1

    def test_app_modules_expose_run(self) -> None:
        assert callable(turn_signal.run) and callable(headlight.run)


# ============================================================ 工位配置
@pytest.mark.parametrize("station", ["turn_signal", "headlight"])
def test_station_yaml_loads(station: str) -> None:
    settings = load_settings(station, use_local=False, environ={})
    assert settings.app == f"lamp.{station}"
    cfg = settings.section("lamp", LampConfig)
    assert cfg.variant in cfg.variants
    for name, profile in cfg.variants.items():
        relay_cfg = settings.section(profile.relay, RelayConfig)
        validate_profile_against_relay(profile, relay_cfg)
        assert settings.has(f"serial.{profile.serial}"), name

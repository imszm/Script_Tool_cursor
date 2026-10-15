"""开关机 / 充电 / 舵机 NFC 压测：假串口覆盖成功、失败保留现场、熔断与收尾断电。"""

from __future__ import annotations

from typing import Any

import pytest
import serial

from ppx_testkit.apps.charge.charge_cycle import ChargeCycle, ChargeCycleConfig
from ppx_testkit.apps.power_cycle.relay_power_on import RelayPowerCycle, RelayPowerCycleConfig
from ppx_testkit.apps.servo.servo_nfc import ServoNfcConfig, ServoNfcCycle
from ppx_testkit.core.monitor.keyword_rules import KeywordConfig, KeywordEvaluator
from ppx_testkit.core.monitor.line_reader import BackgroundLogReader, DeviceLogMonitor
from ppx_testkit.core.relay.base import ChannelCommands, RelayConfig
from ppx_testkit.core.relay.drivers import CommandRelay, ServoDriver
from ppx_testkit.core.runner.result import RunSummary
from ppx_testkit.exceptions import ConfigError, TestAbort
from ppx_testkit.settings import load_settings


def _relay(transport: Any) -> CommandRelay:
    cfg = RelayConfig(channels={1: ChannelCommands(on=0x50, off=0x4F)}, post_write_delay_s=0, drain_response=False)
    return CommandRelay(transport, cfg, sleep=lambda _s: None)


def _monitor(transport: Any, **kw: Any) -> DeviceLogMonitor:
    return DeviceLogMonitor(transport, KeywordEvaluator(KeywordConfig(**kw)), poll_s=0.001)


def _device(fake_factory: Any, port: str) -> Any:
    return next(h for h in fake_factory.opened if h.port == port)


class TestRelayPowerCycle:
    def _cycle(self, make_transport, fake_factory, **cfg):
        relay_t = make_transport("RELAY")
        dev_t = make_transport("DEV")
        base = dict(cycles=1, power_on_s=0.05, power_off_s=0, precheck=False, flush_before_on=False,
                    fail_keeps_power=True, stop_on_fail=True, notify=False)
        base.update(cfg)
        mon = _monitor(dev_t, success=["poweron"], abort=["fatal"])
        cyc = RelayPowerCycle(RelayPowerCycleConfig(**base), _relay(relay_t), mon, sleep=lambda _s: None)
        return cyc, _device(fake_factory, "DEV"), _device(fake_factory, "RELAY")

    def test_success_powers_off(self, make_transport, fake_factory) -> None:
        cyc, dev, relay_port = self._cycle(make_transport, fake_factory)
        dev.feed("boot PowerOn ok\n")
        cyc.setup()
        result = cyc.run_cycle(1)
        assert result.passed and "poweron" in result.detail
        assert cyc.relay.state[1] is False
        assert relay_port.tx[-1] == b"\x4f"

    def test_no_keyword_keeps_power(self, make_transport, fake_factory) -> None:
        cyc, dev, relay_port = self._cycle(make_transport, fake_factory)
        dev.feed("boot only\n")
        result = cyc.run_cycle(1)
        assert not result.passed and result.data["fail_type"] == "关键字未命中"
        assert cyc.relay.state[1] is True
        assert b"\x4f" not in relay_port.tx

    def test_zero_data_is_distinct_failure(self, make_transport, fake_factory) -> None:
        cyc, _dev, _relay = self._cycle(make_transport, fake_factory)
        result = cyc.run_cycle(1)
        assert result.data["fail_type"] == "零数据故障" and cyc.zero_data_failures == 1

    def test_abort_keeps_power(self, make_transport, fake_factory) -> None:
        cyc, dev, _relay = self._cycle(make_transport, fake_factory)
        dev.feed("FATAL reset\n")
        with pytest.raises(TestAbort) as exc:
            cyc.run_cycle(1)
        assert exc.value.keep_power and cyc.relay.state[1] is True

    def test_teardown_powers_off_unless_keep(self, make_transport, fake_factory) -> None:
        cyc, _dev, relay_port = self._cycle(make_transport, fake_factory)
        cyc.relay.on(1)
        relay_port.tx.clear()
        cyc.teardown(RunSummary(station="st", target_cycles=1, keep_power=False))
        assert relay_port.tx == [b"\x4f"] and not cyc.relay.transport.is_open
        cyc2, _d, relay2 = self._cycle(make_transport, fake_factory)
        cyc2.relay.on(1)
        relay2.tx.clear()
        cyc2.teardown(RunSummary(station="st", target_cycles=1, keep_power=True))
        assert relay2.tx == []

    def test_precheck_failure_powers_off(self, make_transport, fake_factory) -> None:
        clock = {"t": 0.0}

        def sleep(seconds: float) -> None:
            clock["t"] += seconds

        relay_t = make_transport("RELAY")
        dev_t = make_transport("DEV")
        cfg = RelayPowerCycleConfig(cycles=1, power_on_s=0.05, power_off_s=0, precheck=True,
                                    precheck_timeout_s=1.0, precheck_poll_s=0.5,
                                    fail_keeps_power=True, stop_on_fail=True, notify=False)
        cyc = RelayPowerCycle(cfg, _relay(relay_t), _monitor(dev_t, success=["poweron"]),
                              sleep=sleep, clock=lambda: clock["t"])
        with pytest.raises(TestAbort, match="连通性验证失败") as exc:
            cyc.setup()
        assert exc.value.keep_power is False
        assert cyc.relay.state[1] is False

    def test_fail_keeps_power_requires_stop_on_fail(self) -> None:
        with pytest.raises(ConfigError, match="stop_on_fail"):
            RelayPowerCycleConfig(fail_keeps_power=True, stop_on_fail=False)


class TestChargeCycle:
    def test_success_then_off(self, make_transport, fake_factory) -> None:
        relay_t = make_transport("RELAY")
        dev_t = make_transport("DEV")
        cfg = ChargeCycleConfig(cycles=1, charge_on_s=0.2, post_success_hold_s=0, off_reset_s=0,
                                initial_off_s=None, flush_before_on=False, notify=False)
        cyc = ChargeCycle(cfg, _relay(relay_t), _monitor(dev_t, success=["voice_msgnum:9"]), sleep=lambda _s: None)
        _device(fake_factory, "DEV").feed("voice_msg num: 9\n")
        result = cyc.run_cycle(1)
        assert result.passed
        assert cyc.relay.state[1] is False

    def test_miss_still_opens_relay(self, make_transport, fake_factory) -> None:
        relay_t = make_transport("RELAY")
        dev_t = make_transport("DEV")
        cfg = ChargeCycleConfig(cycles=1, charge_on_s=0.03, post_success_hold_s=0, off_reset_s=0,
                                initial_off_s=None, notify=False)
        cyc = ChargeCycle(cfg, _relay(relay_t), _monitor(dev_t, success=["voice_msgnum:9"]), sleep=lambda _s: None)
        result = cyc.run_cycle(1)
        assert not result.passed and cyc.relay.state[1] is False

    def test_rate_abort_propagates(self, make_transport, fake_factory) -> None:
        from ppx_testkit.core.monitor.keyword_rules import RateRule

        relay_t = make_transport("RELAY")
        dev_t = make_transport("DEV")
        mon = DeviceLogMonitor(
            dev_t,
            KeywordEvaluator(KeywordConfig(success=["ok"], rate_rules=[RateRule("err", 10, 1, keep_power=False)])),
            poll_s=0.001,
        )
        cfg = ChargeCycleConfig(cycles=1, charge_on_s=0.2, post_success_hold_s=0, off_reset_s=0,
                                initial_off_s=None, flush_before_on=False, notify=False)
        cyc = ChargeCycle(cfg, _relay(relay_t), mon, sleep=lambda _s: None)
        _device(fake_factory, "DEV").feed("ERR burst\n")
        with pytest.raises(TestAbort) as exc:
            cyc.run_cycle(1)
        assert exc.value.keep_power is False


class TestServoJudge:
    def _cycle(self) -> ServoNfcCycle:
        cfg = ServoNfcConfig(status_on=["ACC ON"], status_off=["ACC OFF"], status_match_mode="normalized",
                             gap_min_s=0, gap_max_s=0, notify=False)
        return ServoNfcCycle(cfg, servo=None, reader=None)  # type: ignore[arg-type]

    def test_last_keyword_wins(self) -> None:
        cyc = self._cycle()
        assert cyc.judge(["acc off", "later acc on"], "off") == (True, "on")
        assert cyc.judge(["acc on", "acc off"], "off") == (False, "off")
        assert cyc.judge(["noise"], "off") == (False, "off")

    def test_teardown_lifts_servo(self, make_transport, fake_factory) -> None:
        cfg = ServoNfcConfig(status_on=["ON"], status_off=["OFF"], high_position=1500, move_time_ms=500,
                             gap_min_s=0, gap_max_s=0, notify=False)
        transport = make_transport(open=False)
        servo = ServoDriver(transport, retries=1, open_per_command=True, sleep=lambda _s: None)
        reader = BackgroundLogReader(DeviceLogMonitor(make_transport(), KeywordEvaluator(KeywordConfig())))
        cyc = ServoNfcCycle(cfg, servo, reader)
        cyc.teardown(RunSummary(station="st", target_cycles=1, aborted=True))
        assert any(h.tx == [b"#000P1500T0500!"] for h in fake_factory.opened)


def test_all_station_configs_load() -> None:
    """每个工位 YAML 都能合并加载，且声明的应用模块可导入。"""
    import importlib

    for name in (
        "relay_power_cycle", "nfc_power_cycle", "handle_power_cycle", "w3_power_button", "relay_probe",
        "l5_charge_cycle", "r3_charge_cycle", "servo_nfc",
        "l5_lcb_ble", "mcb_whitebox", "l5_mcb_hall_diag", "r3_mcb_whitebox", "lrd_debug",
        "r3_brake_lock_unlock", "turn_signal", "headlight", "speed_calc", "time_diff",
        "r3_leb_fct", "r3_mcb_fct", "p3_fct", "r3_assembly_upgrade",
        "l5_pctool_stress", "l5_fixture_relay_stress", "w3_pctool_stress", "w3_assembly_stress",
        "upgrade_log_stress", "inspect_controls", "mouse_locate",
    ):
        settings = load_settings(name, use_local=False, environ={})
        module = importlib.import_module(f"ppx_testkit.apps.{settings.app}")
        assert callable(module.run), name
        if "relay" in settings.data and settings.has("relay"):
            from ppx_testkit.core.relay.base import RelayConfig

            settings.section("relay", RelayConfig)


def test_relay_disconnect_during_on(make_transport, fake_factory) -> None:
    relay_t = make_transport("RELAY")
    dev_t = make_transport("DEV")
    fake_factory.opened[0].fail_on["write"] = serial.SerialException("gone")
    cfg = RelayPowerCycleConfig(cycles=1, power_on_s=0.01, power_off_s=0, notify=False)
    relay = CommandRelay(relay_t, RelayConfig(
        channels={1: ChannelCommands(0x50, 0x4F)}, post_write_delay_s=0, drain_response=False, send_retries=1,
    ), sleep=lambda _s: None)
    cyc = RelayPowerCycle(cfg, relay, _monitor(dev_t, success=["x"]), sleep=lambda _s: None)
    result = cyc.run_cycle(1)
    assert not result.passed and "继电器上电失败" in result.detail

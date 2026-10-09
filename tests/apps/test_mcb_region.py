"""L5 MCB 白盒测试应用：用例逻辑 + run() 全链路（假 codec + 假串口 + 真实心跳线程）。"""

from __future__ import annotations

import contextlib
import csv
import json
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import serial
from _whitebox_fakes import (
    DirectMcbClient,
    FakeHeartbeat,
    FakeMcbDevice,
    FakeRegionCodec,
    make_ctx,
    region_connector,
)

from ppx_testkit.apps.whitebox import mcb_region
from ppx_testkit.apps.whitebox._mcb_common import McbLinkConfig, mcb_shadow_regs, write_raw
from ppx_testkit.apps.whitebox.mcb_region import CASES, McbWhiteboxConfig, McbWhiteboxTester
from ppx_testkit.core.protocol.ppx_types import Msg, RegV1
from ppx_testkit.core.protocol.region_client import RegionClient
from ppx_testkit.exceptions import ConfigError, DllLoadError, SerialDisconnectedError, SerialTimeoutError
from ppx_testkit.settings import from_dict

FAST = McbWhiteboxConfig(
    setup_settle_s=0, teardown_settle_s=0, hb_pause_settle_s=0, ping_retry_delay_s=0, clear_err_hold_s=0,
    clear_err_settle_s=0, mode_settle_s=0, gear_settle_s=0, light_settle_s=0, acceleration_settle_s=0,
    speed_poll_interval_s=0, speed_err_clear_hold_s=0,
)


class Harness:
    def __init__(self, cfg: McbWhiteboxConfig = FAST) -> None:
        self.device = FakeMcbDevice()
        self.client = DirectMcbClient(self.device)
        self.hb = FakeHeartbeat(self.client)
        self.raw_writes: list[tuple[int, dict[str, Any], int]] = []
        self.sleeps: list[float] = []
        self.tester = McbWhiteboxTester(self.client, self.hb, cfg, raw_write=self._raw_write,
                                        sleep=self.sleeps.append)

    def _raw_write(self, reg: int, fields: Any, nums: int) -> None:
        self.raw_writes.append((reg, dict(fields), nums))
        self.client.write(reg, fields, nums=nums)

    def verdicts(self) -> list[str]:
        return [r["verdict"] for r in self.tester.rows]


@pytest.fixture
def h() -> Harness:
    return Harness()


# ============================================================ 用例逻辑
class TestCases:
    def test_all_pass_on_healthy_device(self, h: Harness) -> None:
        rows = h.tester.run_all()
        assert [r["case_id"] for r in rows] == [1, 2, 3, 4, 5, 6]
        assert h.verdicts() == ["PASS"] * 6, rows
        assert h.tester.abort_reason is None
        # Case 3 通过心跳影子写入 TEST 模式 + TST_MOTO
        assert h.hb.values[RegV1.RUN_MODE] == 7 and h.hb.values[RegV1.DAT_SETTING] == 0x20
        # Case 4/5 在暂停心跳期间写寄存器
        assert h.hb.pause_count == 2
        # Case 5 结束后 RT_SETTING 恢复为 0；Case 6 结束后目标转速归零
        assert h.device.data.rt_setting == 0
        assert h.hb.values[RegV1.TARGET_SPEED] == 0
        # Case 6 软启动：加速度以 nums=2 下发
        assert (RegV1.ACCERATION, {"acceration": 100}, 2) in h.raw_writes

    def test_negative_speed_counts_by_absolute_value(self, h: Harness) -> None:
        h.device.speed_ramp = [-290]
        assert h.tester.case_mode_switch().verdict == "PASS"
        out = h.tester.case_power_loop()
        assert out.verdict == "PASS"
        assert out.measured["final_rpm"] == -290

    def test_power_loop_timeout_fails_and_resets_target(self, h: Harness) -> None:
        h.device.speed_ramp = []
        h.device.reach_target = False
        h.tester.case_mode_switch()
        out = h.tester.case_power_loop()
        assert out.verdict == "FAIL"
        assert out.measured["samples"] == FAST.speed_poll_count
        assert h.device.data.target_speed == 0

    def test_power_loop_clears_runtime_error(self, h: Harness) -> None:
        h.tester.case_mode_switch()
        h.device.data.mcu_errcode = 0x040000
        h.device.speed_ramp = [0, 0]
        out = h.tester.case_power_loop()
        assert out.verdict == "PASS"
        assert out.measured["err_clears"] == 1
        assert h.device.data.mcu_errcode == 0

    def test_error_code_cleared_passes(self, h: Harness) -> None:
        h.device.data.mcu_errcode = 0x240000
        out = h.tester.case_environment()
        assert out.verdict == "PASS" and "已清除" in out.detail
        # 先写 0x8000 再写 0
        rt_writes = [v["rt_setting"] for reg, v in h.device.writes if reg == RegV1.RT_SETTING]
        assert rt_writes[-2:] == [0x8000, 0]

    def test_uncleared_error_aborts_remaining(self, h: Harness) -> None:
        h.device.data.mcu_errcode = 0x040000
        h.device.clearable_error = False
        h.tester.run_all()
        assert h.verdicts() == ["PASS", "FAIL", "SKIP", "SKIP", "SKIP", "SKIP"]
        assert "0x040000" in h.tester.rows[1]["detail"]
        assert "Case 2" in (h.tester.abort_reason or "")

    def test_low_voltage_fails(self, h: Harness) -> None:
        h.device.data.bus_voltage = 250
        out = h.tester.case_environment()
        assert out.verdict == "FAIL" and "低于下限" in out.detail

    def test_voltage_check_can_be_disabled(self) -> None:
        h = Harness(replace(FAST, min_bus_voltage_v=0.0))
        h.device.data.bus_voltage = 10
        assert h.tester.case_environment().verdict == "PASS"

    def test_offline_device_fails_link_and_skips_rest(self, h: Harness) -> None:
        h.device.offline = True
        h.tester.run_all()
        assert h.verdicts() == ["FAIL"] + ["SKIP"] * 5
        assert h.client.reads.count(RegV1.HW_VERSION) == FAST.ping_attempts

    def test_non_critical_failure_continues(self, h: Harness) -> None:
        orig = h.device.apply_write

        def broken_gear(reg: int, values: dict[str, Any]) -> None:
            if reg == RegV1.GEARS:
                values = {"gear": 1}
            orig(reg, values)

        h.device.apply_write = broken_gear  # type: ignore[method-assign]
        h.tester.run_all()
        assert h.verdicts() == ["PASS", "PASS", "PASS", "FAIL", "PASS", "PASS"]

    def test_disconnect_marks_fail_and_skips_rest(self, h: Harness) -> None:
        h.tester.run_case(CASES[0])
        h.tester.run_case(CASES[1])
        h.tester.run_case(CASES[2])
        h.client.write_error = SerialDisconnectedError("COM9", "写入失败")
        h.tester.run_case(CASES[3])
        h.tester.run_case(CASES[4])
        assert h.verdicts() == ["PASS", "PASS", "PASS", "FAIL", "SKIP"]
        assert "串口断开" in h.tester.rows[3]["detail"]
        assert h.hb.paused_now is False  # 异常时心跳也必须恢复

    def test_protocol_error_in_case_is_fail_not_crash(self, h: Harness) -> None:
        h.client.write_error = SerialTimeoutError("COM9", "写超时")
        out = h.tester.run_case(CASES[3])
        assert out.verdict == "FAIL" and "通信/协议异常" in out.detail
        assert h.tester.abort_reason is None

    def test_light_restore_failure_is_logged_not_raised(self, h: Harness) -> None:
        calls: list[int] = []

        def flaky(reg: int, fields: Any, nums: int) -> None:
            calls.append(fields["rt_setting"])
            if fields["rt_setting"] == 0:
                raise SerialTimeoutError("COM9", "写超时")
            h.client.write(reg, fields)

        h.tester._raw_write = flaky
        out = h.tester.case_light_io()
        assert out.verdict == "PASS" and calls == [0x0C, 0]

    def test_heartbeat_failure_skips_remaining(self, h: Harness) -> None:
        h.tester.run_case(CASES[0])
        h.hb.failed_event.set()
        h.tester.run_case(CASES[1])
        assert h.verdicts() == ["PASS", "SKIP"]
        assert h.tester.abort_reason == "心跳连续写失败"

    def test_teardown_resets_safe_values(self, h: Harness) -> None:
        h.hb.set(RegV1.TARGET_SPEED, 300)
        h.hb.set(RegV1.RT_SETTING, 0x0C)
        h.tester.teardown()
        assert h.hb.values[RegV1.TARGET_SPEED] == 0 and h.hb.values[RegV1.RT_SETTING] == 0


class TestConfig:
    def test_shadow_regs_match_legacy_heartbeat(self) -> None:
        regs = mcb_shadow_regs()
        assert [(r.reg, r.field, r.skip_when_zero) for r in regs] == [
            (RegV1.RUN_MODE, "run_mode", True),
            (RegV1.RT_SETTING, "rt_setting", False),
            (RegV1.TARGET_SPEED, "target_speed", False),
            (RegV1.DAT_SETTING, "dat_setting", True),
        ]

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ConfigError, match="mode_settle_s"):
            from_dict(McbWhiteboxConfig, {"mode_settle_s": -1}, "mcb_whitebox")

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ConfigError, match="未知字段"):
            from_dict(McbWhiteboxConfig, {"target_rmp": 300}, "mcb_whitebox")

    def test_link_validation(self) -> None:
        with pytest.raises(ConfigError):
            from_dict(McbLinkConfig, {"retries": 0}, "link")


# ============================================================ 报文 / 全链路
def test_write_raw_keeps_legacy_write_cmd(make_transport: Any, fake_factory: Any) -> None:
    device = FakeMcbDevice()
    transport = make_transport()
    fake_factory.last.responder = device.respond
    codec = FakeRegionCodec()
    codec.g_data.dat_setting = 0x20
    client = RegionClient(codec, transport, rx_timeout_s=0.05, retries=1)
    write_raw(client, RegV1.ACCERATION, {"acceration": 100}, nums=2)
    assert device.requests == [(Msg.WRITE, RegV1.ACCERATION, 2)]
    assert device.data.acceration == 100 and device.data.dat_setting == 0x20


def test_write_raw_maps_serial_errors(make_transport: Any, fake_factory: Any) -> None:
    transport = make_transport()
    fake_factory.last.fail_on["write"] = serial.SerialException("device gone")
    client = RegionClient(FakeRegionCodec(), transport, rx_timeout_s=0.05, retries=1)
    with pytest.raises(SerialDisconnectedError):
        write_raw(client, RegV1.GEARS, {"gear": 2})


def _settings(settings_factory: Any) -> Any:
    fast = {k: 0.0 for k in mcb_region._DURATION_FIELDS}
    # 需要等真实心跳线程把影子值（TEST 模式 / 目标转速）刷到设备
    fast["mode_settle_s"] = 0.15
    fast["speed_poll_interval_s"] = 0.02
    return settings_factory(
        {
            "link": {"rx_timeout_s": 0.05, "retries": 1, "retry_delay_s": 0.0},
            "heartbeat": {"period_s": 0.01},
            "mcb_whitebox": fast,
        },
        station="mcb_whitebox",
        app="whitebox.mcb_region",
    )


def test_run_end_to_end(settings_factory: Any, make_transport: Any, fake_factory: Any, tmp_path: Path) -> None:
    device = FakeMcbDevice()
    holder: dict[str, Any] = {}
    connect = region_connector(device, make_transport, fake_factory, holder=holder)
    code = mcb_region.run(_settings(settings_factory), make_ctx(tmp_path), connect=connect)

    assert code == 0
    rows = list(csv.DictReader((tmp_path / "mcb_whitebox.csv").open(encoding="utf-8-sig")))
    assert [r["verdict"] for r in rows] == ["PASS"] * 6
    assert (tmp_path / "mcb_whitebox.html").is_file()
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["通过"] == 6 and summary["失败"] == 0
    # 心跳刷新了 RUN_MODE / DAT_SETTING（非零后才写）以及 RT_SETTING / TARGET_SPEED
    written = {reg for cmd, reg, _ in device.requests if cmd == Msg.WRITE}
    assert {RegV1.RUN_MODE, RegV1.RT_SETTING, RegV1.TARGET_SPEED, RegV1.DAT_SETTING} <= written
    assert (Msg.WRITE, RegV1.ACCERATION, 2) in device.requests
    # 结束时停转，串口已释放
    assert device.data.target_speed == 0
    assert holder["serial"].close_calls == 1 and not holder["transport"].is_open


def test_run_offline_device_returns_fail(settings_factory: Any, make_transport: Any, fake_factory: Any,
                                         tmp_path: Path) -> None:
    device = FakeMcbDevice()
    device.offline = True
    holder: dict[str, Any] = {}
    connect = region_connector(device, make_transport, fake_factory, holder=holder)
    code = mcb_region.run(_settings(settings_factory), make_ctx(tmp_path), connect=connect)
    assert code == 1
    rows = list(csv.DictReader((tmp_path / "mcb_whitebox.csv").open(encoding="utf-8-sig")))
    assert [r["verdict"] for r in rows] == ["FAIL"] + ["SKIP"] * 5
    assert not holder["transport"].is_open


def test_run_connect_failure_still_writes_summary(settings_factory: Any, tmp_path: Path) -> None:
    @contextlib.contextmanager
    def broken(settings: Any, link: Any) -> Iterator[Any]:
        raise DllLoadError("DLL 文件不存在")
        yield  # pragma: no cover

    with pytest.raises(DllLoadError):
        mcb_region.run(_settings(settings_factory), make_ctx(tmp_path), connect=broken)
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["中止原因"] == "未能建立连接" and summary["已执行"] == 0


@pytest.mark.hardware
@pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows 加载 L5 ppx_region.dll")
def test_real_dll_formats_read_frame() -> None:
    from ppx_testkit.core.protocol.dll_loader import PpxDll
    from ppx_testkit.core.protocol.ppx_types import looks_like_frame
    from ppx_testkit.core.protocol.region_client import RegionCodecV1

    codec = RegionCodecV1(PpxDll(McbLinkConfig().dll))
    frame = codec.format(0x20, Msg.READ, RegV1.HW_VERSION, 1)
    assert looks_like_frame(frame)

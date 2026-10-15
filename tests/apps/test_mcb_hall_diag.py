"""L5 MCB 霍尔传感器体检：判定逻辑（假时钟）+ run() 全链路（假 codec + 假串口）。"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from _whitebox_fakes import FakeMcbDevice, make_ctx, region_connector

from ppx_testkit.apps.whitebox import mcb_hall_diag
from ppx_testkit.apps.whitebox.mcb_hall_diag import HallDiagConfig, diagnose_hall, hall_status
from ppx_testkit.core.protocol.ppx_types import RegV1
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.settings import from_dict


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


class ScriptedHallClient:
    """按脚本返回霍尔值；脚本用完后保持最后一个值。None 表示读取失败。"""

    def __init__(self, voltage: int | None, hall: Iterable[int | None]) -> None:
        self.voltage = voltage
        self.hall = list(hall)
        self.reads = 0

    def try_read_field(self, reg: int, field: str, *, label: str = "") -> Any | None:
        if reg == RegV1.BUS_VOLTAGE:
            return self.voltage
        assert reg == RegV1.HALL_STATE and field == "hall_state"
        self.reads += 1
        if len(self.hall) > 1:
            return self.hall.pop(0)
        return self.hall[0] if self.hall else None

    def write(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - 体检不写寄存器
        raise AssertionError("霍尔体检不应写寄存器")


CFG = HallDiagConfig(duration_s=2.0, poll_interval_s=0.125)  # 0.125 可精确表示，避免浮点累加误差


def _diag(client: ScriptedHallClient, cfg: HallDiagConfig = CFG) -> mcb_hall_diag.HallDiagResult:
    clock = FakeClock()
    return diagnose_hall(client, cfg, clock=clock, sleep=clock.sleep)


def test_rotating_wheel_passes() -> None:
    res = _diag(ScriptedHallClient(360, [1, 1, 3, 2, 6, 4, 5, 1, 3]))
    assert res.verdict == "PASS"
    assert res.valid_transitions == 8
    assert [t["hall_state"] for t in res.transitions] == [1, 3, 2, 6, 4, 5, 1, 3]
    assert res.bus_voltage_v == 36.0
    assert res.samples == 16  # 2s / 0.125s


def test_stuck_at_zero_fails_with_disconnect_hint() -> None:
    res = _diag(ScriptedHallClient(360, [0]))
    assert res.verdict == "FAIL" and "霍尔线" in res.detail
    assert res.transitions == [{"index": 1, "t_s": 0.0, "hall_state": 0, "status": "异常:断线/非法",
                                "verdict": "FAIL"}]


def test_threshold_matches_legacy_strictly_greater_than_five() -> None:
    five = _diag(ScriptedHallClient(360, [1, 2, 3, 4, 5]))
    six = _diag(ScriptedHallClient(360, [1, 2, 3, 4, 5, 6]))
    assert (five.verdict, six.verdict) == ("FAIL", "PASS")


def test_invalid_states_do_not_count() -> None:
    res = _diag(ScriptedHallClient(360, [1, 7, 2, 0, 3, 7, 4, 0, 5]))
    assert res.valid_transitions == 5 and res.verdict == "FAIL"
    assert len(res.transitions) == 9


@pytest.mark.parametrize("voltage", [None, 0])
def test_no_voltage_aborts_before_listening(voltage: int | None) -> None:
    client = ScriptedHallClient(voltage, [1, 2])
    res = _diag(client)
    assert res.verdict == "FAIL" and "通信中断" in res.detail
    assert client.reads == 0


def test_consecutive_read_failures_abort() -> None:
    res = _diag(ScriptedHallClient(360, [1, 2, None]), replace(CFG, max_consecutive_read_failures=3))
    assert res.verdict == "FAIL" and "连续 3 次" in res.detail
    assert res.read_failures == 3


def test_hall_status_labels() -> None:
    assert [hall_status(v) for v in (0, 1, 6, 7)] == ["异常:断线/非法", "正常", "正常", "异常:断线/非法"]


def test_config_validation() -> None:
    with pytest.raises(ConfigError):
        from_dict(HallDiagConfig, {"duration_s": 0}, "hall_diag")


def test_run_end_to_end(settings_factory: Any, make_transport: Any, fake_factory: Any, tmp_path: Path) -> None:
    device = FakeMcbDevice()
    sequence = iter([1, 3, 2, 6, 4, 5] * 50)
    orig = device.before_read

    def spinning(reg: int) -> None:
        orig(reg)
        if reg == RegV1.HALL_STATE:
            device.data.hall_state = next(sequence)

    device.before_read = spinning  # type: ignore[method-assign]
    holder: dict[str, Any] = {}
    settings = settings_factory(
        {
            "link": {"rx_timeout_s": 0.05, "retries": 1, "retry_delay_s": 0.0},
            "hall_diag": {"duration_s": 0.3, "poll_interval_s": 0.01},
        },
        station="l5_mcb_hall_diag",
        app="whitebox.mcb_hall_diag",
    )
    code = mcb_hall_diag.run(settings, make_ctx(tmp_path),
                             connect=region_connector(device, make_transport, fake_factory, holder=holder))
    assert code == 0
    rows = list(csv.DictReader((tmp_path / "mcb_hall_diag.csv").open(encoding="utf-8-sig")))
    assert len(rows) >= 6 and {r["status"] for r in rows} == {"正常"}
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["结论"] == "PASS" and summary["母线电压(V)"] == 36.0
    assert not holder["transport"].is_open

"""L5 灯板 BLE LED 测试：用例解析/断言 + 真实 BleClient（假 codec + 假串口）+ run()。"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import serial
from _whitebox_fakes import FakeBleCodec, FakeLedBoard, ble_connector, make_ctx

from ppx_testkit.apps.whitebox import lcb_ble
from ppx_testkit.apps.whitebox.lcb_ble import (
    BleLinkConfig,
    LcbBleConfig,
    case_params,
    check_led_expectations,
    loop_case,
    make_combo_cases,
    run_case,
    run_cases,
)
from ppx_testkit.core.protocol.ble_client import BleClient
from ppx_testkit.core.protocol.ppx_types import LED_FIELDS_V1, Msg
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.settings import from_dict
from ppx_testkit.utils.paths import resources_dir

CASE_OK = {
    "id": 1, "comment": "基本点亮", "screen_on": 1, "brightness": 7, "digital": 88, "logo": 1, "rim_state": 1,
    "rdygo": 1, "turn_left": 2, "turn_right": 2, "ring": 2,
    "expect_screen_on": 1, "expect_brightness": 7, "expect_digital": 88, "expect_ring": 2,
    "recv_timeout": None, "delay_after": 0.2,
}


@pytest.fixture
def board() -> FakeLedBoard:
    return FakeLedBoard()


@pytest.fixture
def client(board: FakeLedBoard, make_transport: Any, fake_factory: Any) -> BleClient:
    transport = make_transport()
    fake_factory.last.responder = board.respond
    return BleClient(FakeBleCodec(), transport, rx_timeout_s=0.05)  # type: ignore[arg-type]


# ============================================================ 纯逻辑
class TestCaseParsing:
    def test_defaults_match_legacy(self) -> None:
        assert case_params({}) == {"screen_on": 1, **{k: 0 for k in LED_FIELDS_V1 if k != "screen_on"}}
        params = case_params({"screen_on": "关", "brightness": "7", "digital": 50.0, "logo": None})
        assert params["screen_on"] == 0 and params["brightness"] == 7 and params["digital"] == 50
        assert params["logo"] == 0

    def test_expectations_pass_fail_and_blank(self) -> None:
        led = {"screen_on": 1, "brightness": 7, "digital": 88, "logo": 1, "rim_state": 1, "rdygo": 1,
               "turn_left": 2, "turn_right": 2, "ring": 2}
        assert check_led_expectations(CASE_OK, led) == ("PASS", "全部匹配")
        verdict, detail = check_led_expectations({**CASE_OK, "expect_brightness": 3}, led)
        assert verdict == "FAIL" and detail == "brightness: 实际=7, 期望=3"
        assert check_led_expectations({"expect_logo": None}, led) == ("PASS", "未配置期望值（不校验）")
        assert check_led_expectations(CASE_OK, None) == ("FAIL", "未能读取到 LED 状态")
        assert check_led_expectations({"expect_screen_on": "是"}, {**led, "screen_on": 0})[0] == "FAIL"

    def test_combo_cases(self) -> None:
        cases = make_combo_cases()
        assert len(cases) == 3 ** 7  # screen_on=1、brightness=7 固定，其余 7 个字段各 3 种取值
        assert all(c["screen_on"] == 1 and c["brightness"] == 7 for c in cases)
        assert [c["id"] for c in cases[:3]] == [1, 2, 3]


# ============================================================ 经 BleClient 的用例执行
class TestRunCase:
    def test_pass_writes_then_reads(self, client: BleClient, board: FakeLedBoard) -> None:
        res = run_case(client, CASE_OK, 1)
        assert res.row["verdict"] == "PASS", res.row
        assert board.requests == [(Msg.WRITE, 8), (Msg.READ, 8)]
        assert res.row["checks"]["digital"] == 88
        assert res.row["send_ok"] == res.row["read_ok"] == "PASS"
        assert res.row["recv_hex"].startswith("A5 60 83 08")

    def test_expectation_mismatch_now_fails(self, client: BleClient, board: FakeLedBoard) -> None:
        """旧脚本 V1.6 从未调用 _assert_led_expectations，此类用例会被误判 PASS。"""
        board.force = {"brightness": 3}
        res = run_case(client, CASE_OK, 1)
        assert res.row["send_ok"] == "PASS" and res.row["read_ok"] == "PASS"
        assert res.row["verdict"] == "FAIL"
        assert res.row["expect_detail"] == "brightness: 实际=3, 期望=7"

    def test_timeout_fails(self, client: BleClient, board: FakeLedBoard) -> None:
        board.silent = True
        res = run_case(client, {**CASE_OK, "recv_timeout": 0.02}, 1)
        assert res.row["verdict"] == "FAIL" and res.row["error"] == "应答超时"
        assert not res.fatal

    def test_parse_failure_fails(self, client: BleClient, board: FakeLedBoard) -> None:
        board.garbage = True
        res = run_case(client, CASE_OK, 1)
        assert res.row["verdict"] == "FAIL" and "解析失败" in res.row["error"]

    def test_disconnect_aborts_remaining(self, client: BleClient, fake_factory: Any) -> None:
        fake_factory.last.fail_on["write"] = serial.SerialException("USB 拔出")
        rows: list[dict[str, Any]] = []
        aborted = run_cases(client, [CASE_OK, {**CASE_OK, "id": 2}, {**CASE_OK, "id": 3}], rows,
                            sleep=lambda _s: None)
        assert aborted == "串口断开"
        assert [r["verdict"] for r in rows] == ["FAIL", "SKIP", "SKIP"]
        assert "串口断开" in rows[0]["error"]

    def test_delay_after_and_default_case_id(self, client: BleClient) -> None:
        sleeps: list[float] = []
        rows: list[dict[str, Any]] = []
        assert run_cases(client, [{**CASE_OK, "id": None}], rows, sleep=sleeps.append) is None
        assert rows[0]["case_id"] == 1 and sleeps == [0.2]

    def test_loop_counts_real_result(self, client: BleClient, board: FakeLedBoard) -> None:
        sleeps: list[float] = []
        stats = loop_case(client, CASE_OK, 3, 0.5, sleep=sleeps.append)
        assert (stats.ok, stats.total, stats.failed) == (3, 3, 0)
        assert sleeps == [0.5, 0.5]
        board.silent = True
        stats = loop_case(client, CASE_OK, 2, 0.0, sleep=sleeps.append)
        assert (stats.ok, stats.failed) == (0, 2)


# ============================================================ run()
def _settings(settings_factory: Any, cases: Path, **extra: Any) -> Any:
    return settings_factory(
        {"link": {"rx_timeout_s": 0.05}, "lcb_ble": {"cases": str(cases), "loop_delay_s": 0.0, **extra}},
        station="l5_lcb_ble",
        app="whitebox.lcb_ble",
    )


def _write_cases(path: Path, rows: list[dict[str, Any]]) -> Path:
    keys = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    return path


def test_run_end_to_end_pass(settings_factory: Any, make_transport: Any, fake_factory: Any,
                             board: FakeLedBoard, tmp_path: Path) -> None:
    cases = _write_cases(tmp_path / "cases.csv", [{**CASE_OK, "delay_after": 0}, {**CASE_OK, "id": 2, "delay_after": 0}])
    out = tmp_path / "run"
    code = lcb_ble.run(_settings(settings_factory, cases, loop_count=2), make_ctx(out),
                       connect=ble_connector(board, make_transport, fake_factory))
    assert code == 0
    rows = list(csv.DictReader((out / "lcb_ble.csv").open(encoding="utf-8-sig")))
    assert [r["verdict"] for r in rows] == ["PASS", "PASS"]
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["通过"] == 2 and summary["压力循环(成功/总数)"] == "2/2"
    assert (out / "lcb_ble.html").read_text(encoding="utf-8").count('data-verdict="PASS"') == 2
    assert not fake_factory.last.is_open


def test_run_with_bundled_testcases_detects_mismatch(settings_factory: Any, make_transport: Any,
                                                     fake_factory: Any, board: FakeLedBoard,
                                                     tmp_path: Path) -> None:
    """自带用例第 3 条的期望值与设置值不一致，修复断言后应判 FAIL。"""
    settings = settings_factory(
        {"link": {"rx_timeout_s": 0.05}, "lcb_ble": {"loop_count": 0}},
        station="l5_lcb_ble", app="whitebox.lcb_ble",
    )
    code = lcb_ble.run(settings, make_ctx(tmp_path), connect=ble_connector(board, make_transport, fake_factory))
    assert code == 1
    rows = list(csv.DictReader((tmp_path / "lcb_ble.csv").open(encoding="utf-8-sig")))
    assert [r["verdict"] for r in rows] == ["PASS", "PASS", "FAIL"]
    assert "screen_on: 实际=1, 期望=0" in rows[2]["expect_detail"]


def test_run_loop_failure_fails_exit_code(settings_factory: Any, make_transport: Any, fake_factory: Any,
                                          board: FakeLedBoard, tmp_path: Path) -> None:
    cases = _write_cases(tmp_path / "cases.csv", [{**CASE_OK, "delay_after": 0}])
    orig = board.respond
    calls = {"n": 0}

    def flaky(frame: bytes) -> bytes | None:
        calls["n"] += 1
        return orig(frame) if calls["n"] <= 2 else None  # 用例本身的写+读成功，压力循环无应答

    board.respond = flaky  # type: ignore[method-assign]
    code = lcb_ble.run(_settings(settings_factory, cases, loop_count=1), make_ctx(tmp_path),
                       connect=ble_connector(board, make_transport, fake_factory))
    assert code == 1


def test_missing_cases_file_is_config_error(settings_factory: Any, tmp_path: Path) -> None:
    def never(*_a: Any) -> Any:  # pragma: no cover - 用例加载失败时不应打开串口
        raise AssertionError("不应连接设备")

    with pytest.raises(ConfigError, match="用例文件不存在"):
        lcb_ble.run(_settings(settings_factory, tmp_path / "nope.xlsx"), make_ctx(tmp_path), connect=never)


def test_config_validation() -> None:
    with pytest.raises(ConfigError):
        from_dict(LcbBleConfig, {"loop_count": -1}, "lcb_ble")
    with pytest.raises(ConfigError):
        from_dict(BleLinkConfig, {"dev_id": 0x100}, "link")
    assert LcbBleConfig().cases == resources_dir() / "testcases" / "testcases.xlsx"


@pytest.mark.hardware
@pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows 加载 L5 ppx_ble.dll")
def test_real_dll_formats_led_frame() -> None:
    from ppx_testkit.core.protocol.ble_client import BleCodecV1
    from ppx_testkit.core.protocol.dll_loader import PpxDll
    from ppx_testkit.core.protocol.ppx_types import looks_like_frame

    codec = BleCodecV1(PpxDll(BleLinkConfig().dll))
    assert looks_like_frame(codec.format(0x60, Msg.READ, 8, 1))

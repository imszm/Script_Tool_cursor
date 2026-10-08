"""RegionClient / BleClient / ShadowHeartbeat：用假 codec 替代 DLL，在 Linux 上验证收发、重试与异常处理。"""

from __future__ import annotations

import ctypes
import sys
import threading
from pathlib import Path

import pytest
import serial

from ppx_testkit.core.protocol.ble_client import BleClient, BleResult
from ppx_testkit.core.protocol.dll_loader import PpxDll
from ppx_testkit.core.protocol.heartbeat import ShadowHeartbeat, ShadowReg
from ppx_testkit.core.protocol.ppx_types import (
    PPX_PARSE_OK,
    BleDataV1,
    BleMsg,
    Msg,
    RegionDataV1,
    RegV1,
)
from ppx_testkit.core.protocol.region_client import ParsedFrame, RegionClient
from ppx_testkit.exceptions import (
    DeviceExceptionResponse,
    DllLoadError,
    FrameParseError,
    ProtocolError,
    SerialDisconnectedError,
    SerialTimeoutError,
)
from ppx_testkit.utils.paths import resources_dir


def frame(*payload: int) -> bytes:
    body = bytes(payload)
    return b"\xa5" + body + bytes(max(0, 7 - len(body))) + b"\x55"


class FakeRegionCodec:
    """请求帧 = A5 id cmd reg nums 00.. 55；应答第 2 字节为 cmd，第 4 字节作为 motor_speed。"""

    def __init__(self) -> None:
        self.data = RegionDataV1()
        self.formatted: list[tuple[int, int, int, int]] = []
        self.parse_fail_times = 0

    def tx_data(self) -> RegionDataV1:
        return self.data

    def format(self, dev_id: int, cmd: int, reg_addr: int, reg_nums: int) -> bytes:
        self.formatted.append((dev_id, cmd, reg_addr, reg_nums))
        return frame(dev_id, cmd, reg_addr, reg_nums)

    def parse(self, raw: bytes, dev_id: int) -> ParsedFrame:
        if self.parse_fail_times:
            self.parse_fail_times -= 1
            raise FrameParseError("bad crc")
        cmd = raw[2]
        if cmd & 0xC0 == 0xC0:
            raise DeviceExceptionResponse(f"cmd=0x{cmd:02X}")
        out = RegionDataV1()
        out.motor_speed = raw[4]
        return ParsedFrame(cmd=cmd, reg_addr=raw[3], data=out)


def echo_device(speed: int = 42, cmd_or: int = 0x80):
    def respond(req: bytes) -> bytes:
        return frame(req[1], req[2] | cmd_or, req[3], speed)

    return respond


@pytest.fixture
def region(make_transport, fake_factory):
    t = make_transport()
    fake_factory.last.responder = echo_device()
    codec = FakeRegionCodec()
    client = RegionClient(codec, t, rx_timeout_s=0.1, retries=2, retry_delay_s=0)
    return client, codec, fake_factory.last


def test_region_read(region) -> None:
    client, codec, fake = region
    assert client.read_field(RegV1.MOTOR_SPEED, "motor_speed") == 42
    assert codec.formatted[-1] == (0x20, Msg.READ, RegV1.MOTOR_SPEED, 1)
    client.read(RegV1.MODEL, nums=3)
    assert codec.formatted[-1][1] == Msg.MULTREAD


def test_region_write_sets_tx_fields(region) -> None:
    client, codec, fake = region
    resp = client.write(RegV1.TARGET_SPEED, {"target_speed": -300})
    assert codec.data.target_speed == -300 and resp is not None and resp.cmd == Msg.WRITE | 0x80
    with pytest.raises(ProtocolError, match="没有字段"):
        client.write(RegV1.TARGET_SPEED, {"no_such": 1})


def test_region_write_without_response(region) -> None:
    client, codec, fake = region
    fake.responder = None
    assert client.write(RegV1.RUN_MODE, {"run_mode": 7}, expect_response=False) is None
    assert fake.tx[-1][2] == Msg.WRITE


def test_region_retries_parse_errors(region) -> None:
    client, codec, fake = region
    codec.parse_fail_times = 1
    assert client.read_field(RegV1.MOTOR_SPEED, "motor_speed") == 42
    assert len(fake.tx) == 2


def test_region_timeout_after_retries(region) -> None:
    client, codec, fake = region
    fake.responder = None
    with pytest.raises(SerialTimeoutError, match="无应答"):
        client.read(RegV1.MOTOR_SPEED)
    assert len(fake.tx) == 2
    assert client.try_read_field(RegV1.MOTOR_SPEED, "motor_speed") is None


def test_region_exception_response_not_retried(region) -> None:
    client, codec, fake = region
    fake.responder = echo_device(cmd_or=0xC0)
    with pytest.raises(DeviceExceptionResponse):
        client.read(RegV1.MOTOR_SPEED)
    assert len(fake.tx) == 1


def test_region_disconnect_propagates(region) -> None:
    client, codec, fake = region
    fake.fail_on["write"] = serial.SerialException("unplugged")
    with pytest.raises(SerialDisconnectedError):
        client.read(RegV1.MOTOR_SPEED)
    assert client.try_read_field(RegV1.MOTOR_SPEED, "motor_speed") is None


# ---------------------------------------------------------------- BLE
class FakeBleCodec:
    def __init__(self) -> None:
        self.g_data = BleDataV1()
        self.status = PPX_PARSE_OK
        self.rx_cmd = 0x81
        self.device_led: dict[str, int] = {}

    def format(self, dev_id: int, cmd: int, reg_addr: int, reg_nums: int) -> bytes:
        return frame(dev_id, cmd, reg_addr)

    def parse(self, raw: bytes, dev_id: int) -> tuple[BleMsg, int]:
        for k, v in self.device_led.items():
            setattr(self.g_data.led_msg, k, v)
        return BleMsg(id=dev_id, cmd=self.rx_cmd), self.status


@pytest.fixture
def ble(make_transport, fake_factory):
    t = make_transport()
    fake_factory.last.responder = lambda req: frame(0x60, 0x81)
    codec = FakeBleCodec()
    return BleClient(codec, t, rx_timeout_s=0.1), codec, fake_factory.last


def test_ble_set_and_read_led(ble) -> None:
    client, codec, _ = ble
    res = client.set_led({"digital": 88, "turn_left": 2})
    assert res.ok and codec.g_data.led_msg.digital == 88
    codec.device_led = {"ring": 1}
    res = client.read_led()
    assert res.ok and res.led is not None and res.led["ring"] == 1 and res.led["digital"] == 88
    assert client.data_snapshot()["led_msg"]["turn_left"] == 2


def test_ble_unknown_field(ble) -> None:
    client, _, _ = ble
    with pytest.raises(ProtocolError):
        client.set_led({"bogus": 1})


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda c, f: setattr(f, "responder", None), "应答超时"),
        (lambda c, f: setattr(c, "status", 0), "解析失败"),
        (lambda c, f: setattr(c, "rx_cmd", 0xC1), "异常帧"),
        (lambda c, f: f.fail_on.update(write=serial.SerialException("gone")), "SerialDisconnectedError"),
    ],
)
def test_ble_failures_return_result(ble, setup, error) -> None:
    client, codec, fake = ble
    setup(codec, fake)
    res = client.read_led()
    assert isinstance(res, BleResult) and not res.ok and error in (res.error or "") and res.led is None


# ---------------------------------------------------------------- heartbeat
class RecordingClient:
    def __init__(self, fail: bool = False) -> None:
        self.writes: list[tuple[int, dict]] = []
        self.fail = fail
        self.event = threading.Event()

    def write(self, reg: int, fields: dict, *, expect_response: bool = True, label: str = "") -> None:
        assert expect_response is False
        self.event.set()
        if self.fail:
            raise SerialTimeoutError("COMX", "x")
        self.writes.append((reg, dict(fields)))


REGS = [
    ShadowReg(RegV1.RUN_MODE, "run_mode", skip_when_zero=True),
    ShadowReg(RegV1.RT_SETTING, "rt_setting"),
    ShadowReg(RegV1.TARGET_SPEED, "target_speed"),
    ShadowReg(RegV1.DAT_SETTING, "dat_setting", skip_when_zero=True),
]


def test_heartbeat_tick_skips_zero() -> None:
    c = RecordingClient()
    hb = ShadowHeartbeat(c, REGS)  # type: ignore[arg-type]
    hb.tick()
    assert [r for r, _ in c.writes] == [RegV1.RT_SETTING, RegV1.TARGET_SPEED]
    hb.set(RegV1.RUN_MODE, 7)
    hb.set(RegV1.TARGET_SPEED, 100)
    c.writes.clear()
    hb.tick()
    assert c.writes == [
        (RegV1.RUN_MODE, {"run_mode": 7}),
        (RegV1.RT_SETTING, {"rt_setting": 0}),
        (RegV1.TARGET_SPEED, {"target_speed": 100}),
    ]
    with pytest.raises(KeyError):
        hb.set(RegV1.GEARS, 1)


def test_heartbeat_failure_threshold() -> None:
    c = RecordingClient(fail=True)
    hb = ShadowHeartbeat(c, REGS, max_consecutive_failures=3)  # type: ignore[arg-type]
    hb.tick()
    assert hb.consecutive_failures == 2 and not hb.failed_event.is_set()
    hb.tick()
    assert hb.failed_event.is_set() and hb.total_failures == 4


def test_heartbeat_thread_start_pause_stop() -> None:
    c = RecordingClient()
    hb = ShadowHeartbeat(c, REGS, period_s=0.005)  # type: ignore[arg-type]
    hb.start()
    assert c.event.wait(1.0)
    with hb.paused(settle_s=0.02):
        n = len(c.writes)
        threading.Event().wait(0.03)
        assert len(c.writes) == n
    hb.stop()
    assert not hb._thread.is_alive()  # type: ignore[union-attr]


# ---------------------------------------------------------------- DLL loader
def test_dll_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DllLoadError, match="不存在"):
        PpxDll(tmp_path / "nope.dll")


@pytest.mark.skipif(sys.platform == "win32", reason="仅验证非 Windows 平台给出明确错误")
def test_dll_wrong_platform_gives_hint() -> None:
    with pytest.raises(DllLoadError, match="位"):
        PpxDll(resources_dir() / "dll" / "l5" / "ppx_region.dll")


@pytest.mark.hardware
@pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows 加载 DLL")
@pytest.mark.parametrize("variant", ["l5", "r3"])
def test_real_dll_exports(variant: str) -> None:
    dll = PpxDll(resources_dir() / "dll" / variant / "ppx_region.dll")
    dll.bind("ppx_com_region_format", [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_uint16)
    dll.bind("ppx_com_region_parse", [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_void_p], ctypes.c_int)

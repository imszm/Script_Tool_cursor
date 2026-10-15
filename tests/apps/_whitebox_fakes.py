"""白盒测试应用共用的假 codec / 假设备（不加载 DLL，可在 Linux 上运行）。

帧格式（仅用于测试）::

    A5 | dev_id | cmd | reg | nums | <数据结构体原始字节> | 55

假 codec 把整个数据结构体放进帧里，假设备按 reg..reg+nums-1 读写对应字段，
从而让真实的 RegionClient / BleClient / ShadowHeartbeat / SerialTransport 走完整收发链路。
"""

from __future__ import annotations

import contextlib
import ctypes
import datetime as _dt
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ppx_testkit.core.protocol.ble_client import BleClient
from ppx_testkit.core.protocol.ppx_types import (
    LED_FIELDS_V1,
    PPX_PARSE_OK,
    REG_FIELD_V1,
    BleDataV1,
    BleMsg,
    LedMsgV1,
    Msg,
    RegionDataV1,
    RegV1,
    is_exception_cmd,
)
from ppx_testkit.core.protocol.region_client import ParsedFrame, RegionClient
from ppx_testkit.core.serial.transport import SerialTransport
from ppx_testkit.exceptions import DeviceExceptionResponse, FrameParseError
from ppx_testkit.logger import RunContext

HEAD, END = 0xA5, 0x55
READ_CMDS = (Msg.READ, Msg.MULTREAD)
WRITE_CMDS = (Msg.WRITE, Msg.MULTWRITE)


def make_ctx(run_dir: Path, station: str = "test_station") -> RunContext:
    return RunContext(station=station, run_id="test", run_dir=run_dir, started_at=_dt.datetime.now())


def _frame(dev: int, cmd: int, reg: int, nums: int, payload: bytes) -> bytes:
    return bytes([HEAD, dev, cmd, reg, nums]) + payload + bytes([END])


def _split(frame: bytes) -> tuple[int, int, int, int, bytes]:
    return frame[1], frame[2], frame[3], frame[4], frame[5:-1]


# ============================================================ region（MCB）
class FakeRegionCodec:
    def __init__(self) -> None:
        self.g_data = RegionDataV1()

    def tx_data(self) -> RegionDataV1:
        return self.g_data

    def format(self, dev_id: int, cmd: int, reg: int, reg_nums: int) -> bytes:
        return _frame(dev_id, cmd, reg, reg_nums, bytes(self.g_data))

    def parse(self, frame: bytes, dev_id: int) -> ParsedFrame:
        if len(frame) != 6 + ctypes.sizeof(RegionDataV1):
            raise FrameParseError(f"假 codec: 帧长度错误 {len(frame)}")
        _dev, cmd, reg, nums, payload = _split(frame)
        if is_exception_cmd(cmd):
            raise DeviceExceptionResponse(f"假 codec: 异常帧 cmd=0x{cmd:02X}")
        if (cmd & 0x3F) in READ_CMDS:
            incoming = RegionDataV1.from_buffer_copy(payload)
            for r in range(reg, reg + nums):
                name = REG_FIELD_V1.get(r)
                if name:
                    setattr(self.g_data, name, getattr(incoming, name))
        return ParsedFrame(cmd=cmd, reg_addr=reg, data=RegionDataV1.from_buffer_copy(self.g_data))


class FakeMcbDevice:
    """模拟 L5 MCB 的寄存器行为。

    * 写 RT_SETTING 且含 0x8000 时清除错误码（``clearable_error=False`` 则清不掉）；
    * TEST 模式且目标转速非零时，每次读转速依次返回 ``speed_ramp``，用完后返回 ``-target``；
    * ``offline=True`` 时不应答。
    """

    def __init__(self) -> None:
        self.data = RegionDataV1()
        self.data.hw_version = 0x0102
        self.data.bus_voltage = 360
        self.clearable_error = True
        self.speed_ramp: list[int] = [0, -150]
        self.reach_target = True
        self.offline = False
        self.requests: list[tuple[int, int, int]] = []
        self.writes: list[tuple[int, dict[str, Any]]] = []

    # ---- 寄存器语义
    def apply_write(self, reg: int, values: dict[str, Any]) -> None:
        self.writes.append((reg, dict(values)))
        for name, value in values.items():
            setattr(self.data, name, value)
        if reg == RegV1.RT_SETTING and self.data.rt_setting & 0x8000 and self.clearable_error:
            self.data.mcu_errcode = 0

    def before_read(self, reg: int) -> None:
        if reg != RegV1.MOTOR_SPEED:
            return
        if self.data.run_mode == 7 and self.data.target_speed != 0:
            if self.speed_ramp:
                self.data.motor_speed = self.speed_ramp.pop(0)
            elif self.reach_target:
                self.data.motor_speed = -self.data.target_speed
            else:
                self.data.motor_speed = -10
        else:
            self.data.motor_speed = 0

    # ---- 串口应答
    def respond(self, frame: bytes) -> bytes | None:
        dev, cmd, reg, nums, payload = _split(frame)
        self.requests.append((cmd, reg, nums))
        if self.offline:
            return None
        if cmd in WRITE_CMDS:
            incoming = RegionDataV1.from_buffer_copy(payload)
            values = {REG_FIELD_V1[r]: getattr(incoming, REG_FIELD_V1[r])
                      for r in range(reg, reg + nums) if r in REG_FIELD_V1}
            self.apply_write(reg, values)
        elif cmd in READ_CMDS:
            self.before_read(reg)
        return _frame(dev, cmd | 0x80, reg, nums, bytes(self.data))


class DirectMcbClient:
    """不经串口、直接操作 FakeMcbDevice 的 RegionLike 实现（用于用例逻辑单测）。"""

    def __init__(self, device: FakeMcbDevice) -> None:
        self.device = device
        self.reads: list[int] = []
        self.write_error: BaseException | None = None

    def try_read_field(self, reg: int, field: str, *, label: str = "") -> Any | None:
        self.reads.append(reg)
        if self.device.offline:
            return None
        self.device.before_read(reg)
        return getattr(self.device.data, field)

    def write(self, reg: int, fields: dict[str, Any], *, nums: int = 1, expect_response: bool = True,
              label: str = "") -> None:
        if self.write_error is not None:
            raise self.write_error
        self.device.apply_write(reg, dict(fields))


class FakeHeartbeat:
    """立即把影子值写入设备的心跳替身。"""

    def __init__(self, client: DirectMcbClient) -> None:
        import threading

        self.client = client
        self.failed_event = threading.Event()
        self.values: dict[int, int] = {}
        self.pause_count = 0
        self.paused_now = False

    def set(self, reg: int, value: int) -> None:
        self.values[reg] = value
        self.client.device.apply_write(reg, {REG_FIELD_V1[reg]: value})

    @contextlib.contextmanager
    def paused(self, settle_s: float = 0.2) -> Iterator[None]:
        self.pause_count += 1
        self.paused_now = True
        try:
            yield
        finally:
            self.paused_now = False


def region_connector(
    device: FakeMcbDevice,
    make_transport: Callable[..., SerialTransport],
    fake_factory: Any,
    *,
    holder: dict[str, Any] | None = None,
) -> Callable[..., contextlib.AbstractContextManager[RegionClient]]:
    """构造可注入 run(connect=...) 的连接器：真实 RegionClient + 假 codec + 假串口。"""

    @contextlib.contextmanager
    def _connect(settings: Any, link: Any) -> Iterator[RegionClient]:
        transport = make_transport()
        fake_factory.last.responder = device.respond
        client = RegionClient(FakeRegionCodec(), transport, dev_id=link.dev_id, rx_timeout_s=link.rx_timeout_s,
                              retries=link.retries, retry_delay_s=link.retry_delay_s)
        if holder is not None:
            holder["transport"] = transport
            holder["serial"] = fake_factory.last
        try:
            yield client
        finally:
            transport.close()

    return _connect


# ============================================================ BLE（灯板）
class FakeBleCodec:
    def __init__(self) -> None:
        self.g_data = BleDataV1()

    def format(self, dev_id: int, cmd: int, reg_addr: int, reg_nums: int) -> bytes:
        return _frame(dev_id, cmd, reg_addr, reg_nums, bytes(self.g_data.led_msg))

    def parse(self, frame: bytes, dev_id: int) -> tuple[BleMsg, int]:
        msg = BleMsg(id=dev_id)
        if len(frame) != 6 + ctypes.sizeof(LedMsgV1):
            return msg, 0
        dev, cmd, reg, nums, payload = _split(frame)
        msg.id, msg.cmd, msg.reg_addr, msg.reg_nums = dev, cmd, reg, nums
        if (cmd & 0x3F) == Msg.READ:
            ctypes.memmove(ctypes.addressof(self.g_data.led_msg), payload, len(payload))
        return msg, PPX_PARSE_OK


class FakeLedBoard:
    """模拟灯板：保存写入的 LED 状态，读取时返回；``force`` 可强制某些字段值（模拟固件异常）。"""

    def __init__(self) -> None:
        self.led = LedMsgV1()
        self.force: dict[str, int] = {}
        self.silent = False
        self.garbage = False
        self.requests: list[tuple[int, int]] = []

    def state(self) -> dict[str, int]:
        return {name: int(getattr(self.led, name)) for name in LED_FIELDS_V1}

    def respond(self, frame: bytes) -> bytes | None:
        dev, cmd, reg, nums, payload = _split(frame)
        self.requests.append((cmd, reg))
        if self.silent:
            return None
        if self.garbage:
            return bytes([HEAD, 0, 0, 0, 0, 0, 0, 0, END])
        if cmd == Msg.WRITE:
            self.led = LedMsgV1.from_buffer_copy(payload)
            for name, value in self.force.items():
                setattr(self.led, name, value)
        return _frame(dev, cmd | 0x80, reg, nums, bytes(self.led))


def ble_connector(
    board: FakeLedBoard,
    make_transport: Callable[..., SerialTransport],
    fake_factory: Any,
) -> Callable[..., contextlib.AbstractContextManager[BleClient]]:
    @contextlib.contextmanager
    def _connect(settings: Any, link: Any) -> Iterator[BleClient]:
        transport = make_transport()
        fake_factory.last.responder = board.respond
        try:
            yield BleClient(FakeBleCodec(), transport, dev_id=link.dev_id, rx_timeout_s=link.rx_timeout_s)
        finally:
            transport.close()

    return _connect

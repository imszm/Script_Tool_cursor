"""L5 CCB/灯板 BLE 协议（v1，经 UART 传输）。"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ppx_testkit.core.protocol.dll_loader import PpxDll
from ppx_testkit.core.protocol.ppx_types import (
    LED_FIELDS_V1,
    PPX_PARSE_OK,
    BleDataV1,
    BleMsg,
    BleRegV1,
    CmdType,
    DevId,
    LedMsgV1,
    Msg,
    is_exception_cmd,
    looks_like_frame,
)
from ppx_testkit.core.serial.transport import SerialTransport
from ppx_testkit.exceptions import (
    DeviceExceptionResponse,
    FrameFormatError,
    FrameParseError,
    ProtocolError,
    SerialTimeoutError,
)

log = logging.getLogger(__name__)


class BleCodecV1:
    def __init__(self, dll: PpxDll) -> None:
        self._format = dll.bind("ppx_com_ble_format", [ctypes.c_int, ctypes.POINTER(BleMsg), ctypes.c_void_p],
                                ctypes.c_uint16)
        self._parse = dll.bind("ppx_com_ble_parse",
                               [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8, ctypes.POINTER(BleMsg)], ctypes.c_int)
        self.g_data: BleDataV1 = dll.global_var(BleDataV1, "g_ppx_ble_data")

    def format(self, dev_id: int, cmd: int, reg_addr: int, reg_nums: int) -> bytes:
        msg = BleMsg(id=dev_id, cmd=cmd, reg_addr=reg_addr, reg_nums=reg_nums)
        buf = ctypes.create_string_buffer(256)
        length = int(self._format(CmdType.REQ, ctypes.byref(msg), buf))
        if length <= 0 or length > 256:
            raise FrameFormatError(f"BLE 组包失败 reg={reg_addr} 返回长度 {length}")
        return buf.raw[:length]

    def parse(self, frame: bytes, dev_id: int) -> tuple[BleMsg, int]:
        if len(frame) > 0xFF:
            raise FrameParseError(f"BLE 单帧不能超过 255 字节，实际 {len(frame)}")
        msg = BleMsg(id=dev_id)
        arr = (ctypes.c_uint8 * len(frame)).from_buffer_copy(frame)
        status = int(self._parse(arr, len(frame), ctypes.byref(msg)))
        return msg, status


@dataclass
class BleResult:
    ok: bool
    response: bytes
    parse_status: int | None
    led: dict[str, int] | None = None
    error: str | None = None


class BleClient:
    def __init__(
        self,
        codec: BleCodecV1,
        transport: SerialTransport,
        *,
        dev_id: int = DevId.BLE,
        rx_timeout_s: float = 1.0,
    ) -> None:
        self.codec = codec
        self.transport = transport
        self.dev_id = dev_id
        self.rx_timeout_s = rx_timeout_s
        self.lock = threading.RLock()

    def _exchange(self, cmd: int, reg: int, timeout_s: float | None) -> BleResult:
        timeout = self.rx_timeout_s if timeout_s is None else timeout_s
        try:
            request = self.codec.format(self.dev_id, cmd, reg, 1)
            resp = self.transport.transact(request, timeout, looks_like_frame, idle_gap_s=0.05)
        except (ProtocolError, SerialTimeoutError) as exc:
            log.error("BLE 收发失败 reg=%d: %s", reg, exc)
            return BleResult(False, b"", None, error=str(exc))
        if not resp:
            log.error("BLE 应答超时 reg=%d (%.2fs)", reg, timeout)
            return BleResult(False, b"", None, error="应答超时")
        msg, status = self.codec.parse(resp, self.dev_id)
        if status != PPX_PARSE_OK:
            log.error("BLE 解析失败 返回码=%d RX=%s", status, resp.hex(" ").upper())
            return BleResult(False, resp, status, error=f"解析失败({status})")
        if is_exception_cmd(msg.cmd):
            err = DeviceExceptionResponse(f"BLE 设备返回异常帧 cmd=0x{msg.cmd:02X}")
            log.error("%s", err)
            return BleResult(False, resp, status, error=str(err))
        return BleResult(True, resp, status)

    def set_led(self, values: Mapping[str, int], *, timeout_s: float | None = None) -> BleResult:
        unknown = set(values) - set(LED_FIELDS_V1)
        if unknown:
            raise ProtocolError(f"未知 LED 字段: {sorted(unknown)}")
        with self.lock:
            led: LedMsgV1 = self.codec.g_data.led_msg
            for name, value in values.items():
                setattr(led, name, int(value))
            log.info("写 LED: %s", dict(values))
            return self._exchange(Msg.WRITE, BleRegV1.LED_MSG, timeout_s)

    def read_led(self, *, timeout_s: float | None = None) -> BleResult:
        with self.lock:
            result = self._exchange(Msg.READ, BleRegV1.LED_MSG, timeout_s)
            if result.ok:
                led = self.codec.g_data.led_msg
                result.led = {name: int(getattr(led, name)) for name in LED_FIELDS_V1}
                log.info("读 LED: %s", result.led)
            return result

    def data_snapshot(self) -> dict[str, Any]:
        from ppx_testkit.core.protocol.ppx_types import struct_to_dict

        with self.lock:
            return struct_to_dict(BleDataV1.from_buffer_copy(self.codec.g_data))

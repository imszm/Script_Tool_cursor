"""MCB 寄存器读写（region 协议）。

编解码（DLL 相关）与收发（串口相关）分离：

* :class:`RegionCodecV1` / :class:`RegionCodecV2` 封装 DLL 调用，只做 bytes <-> 结构体；
* :class:`RegionClient` 负责加锁、收发、重试和异常转换，可用假 codec 做单元测试。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ppx_testkit.core.protocol.dll_loader import PpxDll
from ppx_testkit.core.protocol.ppx_types import (
    PPX_PARSE_OK,
    CmdType,
    DevId,
    Msg,
    PacketDataV2,
    RegionCtrlV2,
    RegionDataV1,
    RegionMsg,
    is_exception_cmd,
    looks_like_frame,
)
from ppx_testkit.core.serial.transport import SerialTransport
from ppx_testkit.exceptions import (
    DeviceExceptionResponse,
    FrameFormatError,
    FrameParseError,
    HardwareError,
    ProtocolError,
    SerialTimeoutError,
)

log = logging.getLogger(__name__)

TX_BUFFER_SIZE = 512


@dataclass
class ParsedFrame:
    cmd: int
    reg_addr: int
    data: ctypes.Structure


class RegionCodec(Protocol):
    def tx_data(self) -> ctypes.Structure: ...

    def format(self, dev_id: int, cmd: int, reg_addr: int, reg_nums: int) -> bytes: ...

    def parse(self, frame: bytes, dev_id: int) -> ParsedFrame: ...


class RegionCodecV1:
    """L5：数据存放在 DLL 全局变量 g_ppx_region_data 中。"""

    def __init__(self, dll: PpxDll) -> None:
        self._format = dll.bind("ppx_com_region_format", [ctypes.c_int, ctypes.POINTER(RegionMsg), ctypes.c_void_p],
                                ctypes.c_uint16)
        self._parse = dll.bind("ppx_com_region_parse",
                               [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8, ctypes.POINTER(RegionMsg)], ctypes.c_int)
        self.g_data: RegionDataV1 = dll.global_var(RegionDataV1, "g_ppx_region_data")

    def tx_data(self) -> RegionDataV1:
        return self.g_data

    def format(self, dev_id: int, cmd: int, reg_addr: int, reg_nums: int) -> bytes:
        msg = RegionMsg(id=dev_id, cmd=cmd, reg_addr=reg_addr, reg_nums=reg_nums)
        buf = ctypes.create_string_buffer(TX_BUFFER_SIZE)
        length = int(self._format(CmdType.REQ, ctypes.byref(msg), buf))
        if length <= 0 or length > TX_BUFFER_SIZE:
            raise FrameFormatError(f"组包失败 reg={reg_addr} 返回长度 {length}")
        return buf.raw[:length]

    def parse(self, frame: bytes, dev_id: int) -> ParsedFrame:
        if len(frame) > 0xFF:
            raise FrameParseError(f"v1 协议单帧不能超过 255 字节，实际 {len(frame)}")
        msg = RegionMsg(id=dev_id)
        arr = (ctypes.c_uint8 * len(frame)).from_buffer_copy(frame)
        status = int(self._parse(arr, len(frame), ctypes.byref(msg)))
        if status != PPX_PARSE_OK:
            raise FrameParseError(f"region 解析失败（返回码 {status}）: {frame.hex(' ').upper()}")
        if is_exception_cmd(msg.cmd):
            raise DeviceExceptionResponse(
                f"设备返回异常帧 cmd=0x{msg.cmd:02X} excp=({msg.reg_excp.parse_status},"
                f"{msg.reg_excp.cmd_status},{msg.reg_excp.data_status})"
            )
        return ParsedFrame(cmd=msg.cmd, reg_addr=msg.reg_addr, data=RegionDataV1.from_buffer_copy(self.g_data))


class RegionCodecV2:
    """R3：无全局变量，format/parse 使用 ppx_region_ctrl_t。"""

    def __init__(self, dll: PpxDll) -> None:
        self._format = dll.bind("ppx_com_region_format",
                                [ctypes.c_int, ctypes.POINTER(RegionCtrlV2), ctypes.c_void_p], ctypes.c_uint16)
        self._packet_parse = dll.bind("ppx_com_packet_parse",
                                      [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint16, ctypes.POINTER(PacketDataV2)],
                                      ctypes.c_int)
        self._region_parse = dll.bind("ppx_com_region_parse",
                                      [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint16, ctypes.POINTER(RegionCtrlV2)],
                                      ctypes.c_int)
        self._tx = RegionCtrlV2()

    def tx_data(self) -> ctypes.Structure:
        return self._tx.data

    def format(self, dev_id: int, cmd: int, reg_addr: int, reg_nums: int) -> bytes:
        self._tx.msg.id = dev_id
        self._tx.msg.cmd = cmd
        self._tx.msg.reg_addr = reg_addr
        self._tx.msg.reg_nums = reg_nums
        buf = (ctypes.c_uint8 * TX_BUFFER_SIZE)()
        length = int(self._format(CmdType.REQ, ctypes.byref(self._tx), buf))
        if length <= 0 or length > TX_BUFFER_SIZE:
            raise FrameFormatError(f"组包失败 reg={reg_addr} 返回长度 {length}")
        return bytes(buf[:length])

    def parse(self, frame: bytes, dev_id: int) -> ParsedFrame:
        raw = (ctypes.c_uint8 * len(frame)).from_buffer_copy(frame)
        packet = PacketDataV2()
        status = int(self._packet_parse(raw, len(frame), ctypes.byref(packet)))
        if status != PPX_PARSE_OK:
            raise FrameParseError(f"链路层解析失败（返回码 {status}）: {frame.hex(' ').upper()}")
        if is_exception_cmd(packet.cmd):
            raise DeviceExceptionResponse(f"设备返回异常帧 cmd=0x{packet.cmd:02X}")
        rx = RegionCtrlV2()
        rx.msg.id = packet.id
        rx.msg.cmd = packet.cmd
        data_len = min(int(packet.data_len), len(packet.data))
        status = int(self._region_parse(packet.data, data_len, ctypes.byref(rx)))
        if status != PPX_PARSE_OK:
            # 与旧脚本一致：净荷解析失败时退回用整帧再试一次
            status = int(self._region_parse(raw, len(frame), ctypes.byref(rx)))
        if status != PPX_PARSE_OK:
            raise FrameParseError(f"寄存器层解析失败（返回码 {status}）")
        return ParsedFrame(cmd=rx.msg.cmd, reg_addr=rx.msg.reg_addr, data=type(rx.data).from_buffer_copy(rx.data))


class RegionClient:
    def __init__(
        self,
        codec: RegionCodec,
        transport: SerialTransport,
        *,
        dev_id: int = DevId.MCB,
        rx_timeout_s: float = 0.3,
        retries: int = 2,
        retry_delay_s: float = 0.05,
        name: str = "mcb",
    ) -> None:
        self.codec = codec
        self.transport = transport
        self.dev_id = dev_id
        self.rx_timeout_s = rx_timeout_s
        self.retries = max(1, retries)
        self.retry_delay_s = retry_delay_s
        self.lock = threading.RLock()
        self.log = logging.getLogger(f"ppx_testkit.protocol.{name}")

    # ------------------------------------------------------------ 底层
    def _exchange(self, cmd: int, reg: int, nums: int, label: str, *, expect_response: bool = True) -> ParsedFrame | None:
        last_exc: BaseException | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with self.lock:
                    request = self.codec.format(self.dev_id, cmd, reg, nums)
                    self.log.debug("[%s] TX reg=%d nums=%d %s", label, reg, nums, request.hex(" ").upper())
                    if not expect_response:
                        self.transport.write(request)
                        return None
                    resp = self.transport.transact(request, self.rx_timeout_s, looks_like_frame, idle_gap_s=0.03)
                    if not resp:
                        raise SerialTimeoutError(self.transport.port, f"[{label}] {self.rx_timeout_s}s 内无应答")
                    self.log.debug("[%s] RX %s", label, resp.hex(" ").upper())
                    return self.codec.parse(resp, self.dev_id)
            except DeviceExceptionResponse:
                raise
            except (ProtocolError, SerialTimeoutError) as exc:
                last_exc = exc
                self.log.warning("[%s] 第 %d/%d 次失败: %s", label, attempt, self.retries, exc)
                if attempt < self.retries:
                    time.sleep(self.retry_delay_s)
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------ 读
    def read(self, reg: int, nums: int = 1, *, label: str = "") -> ctypes.Structure:
        cmd = Msg.READ if nums <= 1 else Msg.MULTREAD
        frame = self._exchange(cmd, reg, nums, label or f"读寄存器{reg}")
        assert frame is not None
        return frame.data

    def read_field(self, reg: int, field: str, *, label: str = "") -> Any:
        return getattr(self.read(reg, label=label), field)

    def try_read_field(self, reg: int, field: str, *, label: str = "") -> Any | None:
        """读取失败返回 None（只吞掉通信/协议类异常，并记录日志）。"""
        try:
            return self.read_field(reg, field, label=label)
        except (HardwareError, ProtocolError) as exc:
            self.log.error("读取寄存器 %d(%s) 失败: %s", reg, field, exc)
            return None

    # ------------------------------------------------------------ 写
    def write(
        self,
        reg: int,
        fields: Mapping[str, Any],
        *,
        nums: int = 1,
        expect_response: bool = True,
        label: str = "",
    ) -> ParsedFrame | None:
        cmd = Msg.WRITE if nums <= 1 else Msg.MULTWRITE
        with self.lock:
            data = self.codec.tx_data()
            for name, value in fields.items():
                if not hasattr(data, name):
                    raise ProtocolError(f"寄存器数据结构没有字段 '{name}'")
                setattr(data, name, value)
            return self._exchange(cmd, reg, nums, label or f"写寄存器{reg}", expect_response=expect_response)

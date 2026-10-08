"""具体继电器 / 舵机驱动实现。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ppx_testkit.core.relay.base import RelayDriver
from ppx_testkit.exceptions import ConfigError, HardwareError, RelayError
from ppx_testkit.settings import parse_bytes

if TYPE_CHECKING:
    from ppx_testkit.core.serial.transport import SerialTransport

log = logging.getLogger(__name__)


class CommandRelay(RelayDriver):
    def channel_frame(self, channel: int, on: bool) -> bytes:
        cmds = self.cfg.channels.get(channel)
        if cmds is None:
            raise RelayError(f"继电器未配置通道 {channel}，可用通道: {sorted(self.cfg.channels)}")
        return parse_bytes(cmds.on if on else cmds.off, f"relay.channels.{channel}")


def icse_a0_frame(channel: int, on: bool) -> bytes:
    """ICSE 模块帧：A0 <ch> <state> <(A0+ch+state) & 0xFF>。

    >>> icse_a0_frame(2, True).hex(" ").upper()
    'A0 02 01 A3'
    """
    if not 1 <= channel <= 0xFF:
        raise ConfigError(f"ICSE 通道号非法: {channel}")
    state = 0x01 if on else 0x00
    return bytes([0xA0, channel, state, (0xA0 + channel + state) & 0xFF])


class IcseA0Relay(RelayDriver):
    def channel_frame(self, channel: int, on: bool) -> bytes:
        cmds = self.cfg.channels.get(channel)
        if cmds is not None:
            return parse_bytes(cmds.on if on else cmds.off, f"relay.channels.{channel}")
        return icse_a0_frame(channel, on)


def servo_command(servo_id: int, position: int, time_ms: int) -> bytes:
    """总线舵机 ASCII 指令。

    >>> servo_command(0, 2500, 1000)
    b'#000P2500T1000!'
    """
    if not 0 <= servo_id <= 254:
        raise ConfigError(f"舵机 ID 非法: {servo_id}")
    if not 500 <= position <= 2500:
        raise ConfigError(f"舵机位置超出 500~2500: {position}")
    if not 0 <= time_ms <= 9999:
        raise ConfigError(f"舵机动作时间超出 0~9999ms: {time_ms}")
    return f"#{servo_id:03d}P{position:04d}T{time_ms:04d}!".encode("ascii")


class ServoDriver:
    """总线舵机：默认每条指令独立开关串口（与旧脚本一致，避免长连接被干扰后卡死）。"""

    def __init__(
        self,
        transport: SerialTransport,
        *,
        servo_id: int = 0,
        retries: int = 2,
        retry_delay_s: float = 0.5,
        open_per_command: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.servo_id = servo_id
        self.retries = max(1, retries)
        self.retry_delay_s = retry_delay_s
        self.open_per_command = open_per_command
        self._sleep = sleep

    def move(self, position: int, time_ms: int) -> None:
        frame = servo_command(self.servo_id, position, time_ms)
        last_exc: BaseException | None = None
        for attempt in range(1, self.retries + 1):
            try:
                if not self.transport.is_open:
                    self.transport.open()
                self.transport.write(frame)
                log.info("舵机 -> %s", frame.decode("ascii"))
                return
            except HardwareError as exc:
                last_exc = exc
                log.warning("舵机指令发送失败 (第 %d/%d 次): %s", attempt, self.retries, exc)
                self.transport.close()
                if attempt < self.retries:
                    self._sleep(self.retry_delay_s)
            finally:
                if self.open_per_command:
                    self.transport.close()
        raise RelayError(f"舵机指令 {frame!r} 发送失败: {last_exc}") from last_exc

    def close(self) -> None:
        self.transport.close()

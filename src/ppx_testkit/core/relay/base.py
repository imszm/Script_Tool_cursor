"""继电器驱动抽象与配置。

三类硬件：
* ``command``：单字节/定长指令继电器（0x50 闭合、0x4F 断开 ……），极性完全由配置决定；
* ``icse_a0``：ICSE 多通道模块，帧 ``A0 <ch> <state> <sum>``；
* ``servo``：总线舵机，ASCII 指令 ``#000P2500T1000!``（见 :class:`ServoDriver`）。
"""

from __future__ import annotations

import abc
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ppx_testkit.exceptions import ConfigError, HardwareError, RelayError
from ppx_testkit.settings import parse_bytes

if TYPE_CHECKING:
    from ppx_testkit.core.serial.transport import SerialTransport

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelCommands:
    on: Any
    off: Any


@dataclass(frozen=True)
class RelayConfig:
    type: Literal["command", "icse_a0"] = "command"
    channels: dict[int, ChannelCommands] = field(default_factory=dict)
    commands: dict[str, Any] = field(default_factory=dict)
    repeat: int = 1
    repeat_gap_s: float = 0.05
    post_write_delay_s: float = 0.1
    drain_response: bool = True
    open_per_command: bool = False
    send_retries: int = 1

    def __post_init__(self) -> None:
        if self.repeat < 1 or self.send_retries < 1:
            raise ConfigError("relay.repeat / relay.send_retries 至少为 1")
        if self.type == "command" and not self.channels and not self.commands:
            raise ConfigError("command 型继电器必须配置 channels 或 commands")
        for ch, cmds in self.channels.items():
            parse_bytes(cmds.on, f"relay.channels.{ch}.on")
            parse_bytes(cmds.off, f"relay.channels.{ch}.off")
        for name, value in self.commands.items():
            parse_bytes(value, f"relay.commands.{name}")


class RelayDriver(abc.ABC):
    """继电器统一接口。所有发送失败均抛出 :class:`RelayError`。"""

    def __init__(
        self,
        transport: SerialTransport,
        cfg: RelayConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.cfg = cfg
        self._sleep = sleep
        self.state: dict[int, bool] = {}

    # ---------------------------------------------------------- 子类实现
    @abc.abstractmethod
    def channel_frame(self, channel: int, on: bool) -> bytes: ...

    def channel_list(self) -> list[int]:
        return sorted(self.cfg.channels) or [1]

    # ---------------------------------------------------------- 公共逻辑
    def send_raw(self, data: bytes, label: str) -> None:
        last_exc: BaseException | None = None
        for attempt in range(1, self.cfg.send_retries + 1):
            opened_here = False
            try:
                if not self.transport.is_open:
                    self.transport.open()
                    opened_here = True
                for i in range(self.cfg.repeat):
                    self.transport.write(data)
                    if i + 1 < self.cfg.repeat:
                        self._sleep(self.cfg.repeat_gap_s)
                if self.cfg.post_write_delay_s > 0:
                    self._sleep(self.cfg.post_write_delay_s)
                if self.cfg.drain_response:
                    self.transport.read_available()
                log.debug("继电器指令 %s 已发送: %s", label, data.hex(" ").upper())
                return
            except HardwareError as exc:
                last_exc = exc
                log.warning("继电器指令 %s 发送失败 (第 %d/%d 次): %s", label, attempt, self.cfg.send_retries, exc)
                self.transport.close()
                opened_here = False
            finally:
                if opened_here and self.cfg.open_per_command:
                    self.transport.close()
        raise RelayError(f"继电器指令 {label} 发送失败: {last_exc}") from last_exc

    def send(self, name: str) -> None:
        if name not in self.cfg.commands:
            raise RelayError(f"未配置的继电器命名指令 '{name}'，可用: {sorted(self.cfg.commands)}")
        self.send_raw(parse_bytes(self.cfg.commands[name]), name)

    def on(self, channel: int = 1) -> None:
        self.send_raw(self.channel_frame(channel, True), f"CH{channel}-ON")
        self.state[channel] = True
        log.info("继电器 CH%d -> ON", channel)

    def off(self, channel: int = 1) -> None:
        self.send_raw(self.channel_frame(channel, False), f"CH{channel}-OFF")
        self.state[channel] = False
        log.info("继电器 CH%d -> OFF", channel)

    def press(self, channel: int = 1, hold_s: float = 0.3) -> None:
        """按下-保持-松开；即使保持期间出错也会尝试松开。"""
        self.on(channel)
        try:
            self._sleep(hold_s)
        finally:
            self.off(channel)

    def all_off(self) -> None:
        """急停/退出时调用：尽力断开所有通道，单个失败不影响其余通道。"""
        for ch in self.channel_list():
            try:
                self.off(ch)
            except RelayError:
                log.exception("关闭继电器 CH%d 失败", ch)


def build_relay(cfg: RelayConfig, transport: SerialTransport, **kwargs: Any) -> RelayDriver:
    from ppx_testkit.core.relay.drivers import CommandRelay, IcseA0Relay

    if cfg.type == "command":
        return CommandRelay(transport, cfg, **kwargs)
    if cfg.type == "icse_a0":
        return IcseA0Relay(transport, cfg, **kwargs)
    raise ConfigError(f"未知继电器类型: {cfg.type}")

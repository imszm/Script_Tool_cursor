"""LRD / BLE 灯板单次点灯调试。

迁移自 ``Tool/LRD调试程序.py``：按配置写入一组 LED 字段，再读回比对。
通信失败（含串口断开）记为失败并关闭串口，不把底层异常抛到进程外。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ppx_testkit.apps.whitebox.lcb_ble import BleConnector, BleLinkConfig, LedClientLike, open_ble_client
from ppx_testkit.core.protocol.ppx_types import LED_FIELDS_V1
from ppx_testkit.exceptions import ConfigError, HardwareError, ProtocolError, SerialDisconnectedError
from ppx_testkit.logger import RunContext, get_logger
from ppx_testkit.settings import AppSettings

log = get_logger(__name__)


@dataclass(frozen=True)
class LrdLedConfig:
    screen_on: int = 1
    brightness: int = 7
    digital: int = 88
    logo: int = 2
    rim_state: int = 1
    rdygo: int = 1
    turn_left: int = 2
    turn_right: int = 2
    ring: int = 2

    def values(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in LED_FIELDS_V1}

    def __post_init__(self) -> None:
        for name, value in self.values().items():
            if value < 0:
                raise ConfigError(f"lrd.{name} 不能为负数: {value}")


def apply_and_verify(client: LedClientLike, cfg: LrdLedConfig) -> tuple[bool, str]:
    """写 LED 并回读。返回 (是否一致, 说明)。通信失败返回 False，不抛异常。"""
    wanted = cfg.values()
    try:
        written = client.set_led(wanted)
        if not written.ok:
            return False, f"写 LED 失败: {written.error}"
        read = client.read_led()
    except SerialDisconnectedError as exc:
        return False, f"串口断开: {exc}"
    except (HardwareError, ProtocolError) as exc:
        return False, f"通信/协议异常: {exc}"
    if not read.ok or read.led is None:
        return False, f"读 LED 失败: {read.error}"
    mismatches = [f"{name}: 实际={read.led.get(name)}, 期望={value}"
                  for name, value in wanted.items() if read.led.get(name) != value]
    if mismatches:
        return False, "回读不一致: " + "; ".join(mismatches)
    log.info("LED 回读一致: %s", read.led)
    return True, "回读一致"


def run(settings: AppSettings, ctx: RunContext, *, connect: BleConnector = open_ble_client) -> int:
    link = settings.section("link", BleLinkConfig, required=False)
    cfg = settings.section("lrd", LrdLedConfig, required=False)
    log.info("LRD 点灯调试 | DLL: %s | 串口端点: serial.%s | %s", link.dll, link.serial, cfg.values())
    try:
        with connect(settings, link) as client:
            ok, detail = apply_and_verify(client, cfg)
    except (HardwareError, ProtocolError, ConfigError) as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
        log.error("LRD 调试未能完成: %s", detail)
    ctx.write_text("lrd_result.txt", detail + "\n")
    log.log(logging.INFO if ok else logging.ERROR, "LRD 调试结果: %s", detail)
    return 0 if ok else 1

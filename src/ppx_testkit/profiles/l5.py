"""L5 机型的 DLL 路径和默认协议参数。"""

from __future__ import annotations

from pathlib import Path

from ppx_testkit.core.protocol.heartbeat import ShadowReg
from ppx_testkit.core.protocol.ppx_types import DevId, RegV1
from ppx_testkit.profiles.base import ProtocolProfile

L5 = ProtocolProfile(
    model="l5",
    version="v1",
    dll_subdir="l5",
    region_dll="ppx_region.dll",
    ble_dll="ppx_ble.dll",
    dev_id=int(DevId.MCB),
    ble_dev_id=int(DevId.BLE),
    rx_timeout_s=0.3,
    retries=3,
    retry_delay_s=0.1,
    baudrate=460800,
)


def region_dll() -> Path:
    """返回 L5 ppx_region.dll 的绝对路径。

    :return: resources/dll/l5/ppx_region.dll。
    """
    return L5.dll_file("region")


def ble_dll() -> Path:
    """返回 L5 ppx_ble.dll 的绝对路径。

    :return: resources/dll/l5/ppx_ble.dll。
    """
    return L5.dll_file("ble")


def mcb_shadow_regs() -> list[ShadowReg]:
    """L5 MCB 测试模式下周期刷新的控制寄存器。

    顺序与旧脚本心跳一致：RUN_MODE（为零则跳过）、RT_SETTING、TARGET_SPEED、DAT_SETTING（为零则跳过）。

    :return: 影子寄存器列表。调用方每次拿到一份新列表。
    """
    return [
        ShadowReg(RegV1.RUN_MODE, "run_mode", skip_when_zero=True),
        ShadowReg(RegV1.RT_SETTING, "rt_setting"),
        ShadowReg(RegV1.TARGET_SPEED, "target_speed"),
        ShadowReg(RegV1.DAT_SETTING, "dat_setting", skip_when_zero=True),
    ]

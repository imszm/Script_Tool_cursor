"""L5 机型的 DLL 路径和默认协议参数。"""

from __future__ import annotations

from pathlib import Path

from ppx_testkit.core.protocol.ppx_types import DevId
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

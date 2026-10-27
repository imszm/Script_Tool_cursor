"""机型协议参数。只描述路径和默认值，不加载 DLL。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ppx_testkit.exceptions import ConfigError
from ppx_testkit.utils.paths import resources_dir

ProtocolVersion = Literal["v1", "v2"]
DllKind = Literal["region", "ble"]


@dataclass(frozen=True)
class ProtocolProfile:
    """一个机型的 DLL 位置和默认通信参数。

    :param model: 机型名，如 ``l5`` / ``r3``。
    :param version: region 协议版本。v1 为 L5，v2 为 R3。
    :param dll_subdir: ``resources/dll`` 下的子目录。
    :param region_dll: region DLL 文件名。
    :param ble_dll: BLE DLL 文件名；没有时为 None。
    :param dev_id: 默认 region 设备节点。
    :param ble_dev_id: 默认 BLE 节点；没有 BLE 时为 None。
    :param rx_timeout_s: 默认接收超时（秒）。
    :param retries: 默认收发重试次数。
    :param retry_delay_s: 默认重试间隔（秒）。
    :param baudrate: 默认波特率。
    """

    model: str
    version: ProtocolVersion
    dll_subdir: str
    region_dll: str
    ble_dll: str | None = None
    dev_id: int = 0x20
    ble_dev_id: int | None = None
    rx_timeout_s: float = 0.3
    retries: int = 3
    retry_delay_s: float = 0.1
    baudrate: int = 460800

    def dll_file(self, kind: DllKind = "region") -> Path:
        """解析某种 DLL 的绝对路径。

        :param kind: ``region`` 或 ``ble``。
        :return: ``resources/dll/<机型>/<文件名>``。
        :raises ConfigError: 该机型没有请求的 DLL。
        """
        filename = self.region_dll if kind == "region" else self.ble_dll
        if not filename:
            raise ConfigError(f"{self.model} 没有 {kind} DLL")
        return resources_dir() / "dll" / self.dll_subdir / filename

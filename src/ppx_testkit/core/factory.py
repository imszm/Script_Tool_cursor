"""由配置构建常用硬件对象，供各应用共享，避免每个应用重复解析配置。

约定的配置结构::

    serial:
      <name>:                # 如 relay / device / mcb
        baudrate: 115200
        match: {port: COM5}  # 或 description_contains: [CH340]
    relay:   {...}           # RelayConfig；灯具工位用 relays.<name>
    keywords: {...}          # KeywordConfig
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

from ppx_testkit.core.monitor.keyword_rules import KeywordConfig, KeywordEvaluator
from ppx_testkit.core.monitor.line_reader import DeviceLogMonitor
from ppx_testkit.core.protocol.dll_loader import PpxDll
from ppx_testkit.core.protocol.region_client import RegionClient, RegionCodecV1, RegionCodecV2
from ppx_testkit.core.relay.base import RelayConfig, RelayDriver, build_relay
from ppx_testkit.core.relay.drivers import ServoDriver
from ppx_testkit.core.serial.port_finder import PortFinder
from ppx_testkit.core.serial.transport import SerialEndpoint, SerialFactory, SerialTransport, open_endpoint
from ppx_testkit.settings import AppSettings

ProtocolVersion = Literal["v1", "v2"]


def endpoint(settings: AppSettings, name: str) -> SerialEndpoint:
    """读取 ``serial.<name>`` 并校验为 :class:`SerialEndpoint`。"""
    return settings.section(f"serial.{name}", SerialEndpoint)


def open_serial(
    settings: AppSettings,
    name: str,
    *,
    exclude: Iterable[str] = (),
    finder: PortFinder | None = None,
    factory: SerialFactory | None = None,
    auto_open: bool = True,
) -> SerialTransport:
    """定位并打开 ``serial.<name>`` 端口；失败抛出 PortNotFoundError / SerialOpenError。"""
    return open_endpoint(
        endpoint(settings, name), name=name, finder=finder, exclude=exclude, factory=factory, auto_open=auto_open
    )


def open_ports(
    settings: AppSettings,
    specs: Sequence[tuple[str, bool]],
    *,
    factory: SerialFactory | None = None,
    finder: PortFinder | None = None,
) -> dict[str, SerialTransport]:
    """按 ``(串口名, 是否立即打开)`` 依次定位并打开 ``serial.<名>``。

    后打开的端口会排除已占用的端口号，避免自动识别时两路匹配到同一个口；
    任一端口失败时关闭已打开的端口再抛出原异常。
    """
    opened: dict[str, SerialTransport] = {}
    try:
        for name, auto_open in specs:
            opened[name] = open_serial(
                settings,
                name,
                exclude=[t.port for t in opened.values()],
                finder=finder,
                factory=factory,
                auto_open=auto_open,
            )
    except BaseException:
        close_ports(*opened.values())
        raise
    return opened


def close_ports(*transports: SerialTransport | None) -> None:
    """关闭全部串口。SerialTransport.close 幂等且不抛异常，可放在 finally 中。"""
    for transport in transports:
        if transport is not None:
            transport.close()


def relay_from_settings(
    settings: AppSettings, transport: SerialTransport, *, key: str = "relay", **kwargs: Any
) -> RelayDriver:
    """按 ``relay`` 或 ``relays.<name>`` 构建继电器驱动。"""
    return build_relay(settings.section(key, RelayConfig), transport, **kwargs)


def keyword_config(settings: AppSettings, key: str = "keywords") -> KeywordConfig:
    """读取关键字段；缺省时使用空规则。"""
    return settings.section(key, KeywordConfig, required=False)


def keyword_evaluator(settings: AppSettings, key: str = "keywords") -> KeywordEvaluator:
    """由关键字配置构造纯逻辑判定器。"""
    return KeywordEvaluator(keyword_config(settings, key))


def build_monitor(
    settings: AppSettings,
    transport: SerialTransport,
    *,
    name: str = "device",
    no_data_warning_s: float | None = None,
) -> DeviceLogMonitor:
    """以 ``serial.<name>`` 的编码设置与 ``keywords`` 段构建设备日志监听器。"""
    ep = endpoint(settings, name)
    return DeviceLogMonitor(
        transport,
        keyword_evaluator(settings),
        encoding=ep.encoding,
        errors=ep.encoding_errors,
        raw_source=name,
        no_data_warning_s=no_data_warning_s,
    )


def servo_from_settings(settings: AppSettings, transport: SerialTransport) -> ServoDriver:
    """按舵机工位 ``test`` 段构建总线舵机驱动（每条指令独立开关串口）。"""
    from ppx_testkit.apps.servo.servo_nfc import ServoNfcConfig

    cfg = settings.section("test", ServoNfcConfig)
    return ServoDriver(
        transport,
        servo_id=cfg.servo_id,
        retries=cfg.servo_retries,
        retry_delay_s=cfg.servo_retry_delay_s,
        open_per_command=True,
    )


def build_region_client(
    codec: Any,
    transport: SerialTransport,
    *,
    dev_id: int,
    rx_timeout_s: float,
    retries: int,
    retry_delay_s: float,
    name: str,
) -> RegionClient:
    """用已加载的 codec 和已打开的串口组装 RegionClient。"""
    return RegionClient(
        codec,
        transport,
        dev_id=dev_id,
        rx_timeout_s=rx_timeout_s,
        retries=retries,
        retry_delay_s=retry_delay_s,
        name=name,
    )


def make_region_codec(dll: Path, version: ProtocolVersion) -> RegionCodecV1 | RegionCodecV2:
    """按协议版本加载 region DLL。v1 为 L5，v2 为 R3，两者不能混用。"""
    loaded = PpxDll(dll)
    if version == "v1":
        return RegionCodecV1(loaded)
    return RegionCodecV2(loaded)


@contextlib.contextmanager
def region_client_from_settings(
    settings: AppSettings,
    *,
    serial_name: str,
    dll: Path,
    version: ProtocolVersion,
    dev_id: int,
    rx_timeout_s: float,
    retries: int,
    retry_delay_s: float,
    codec: Any | None = None,
    serial_factory: SerialFactory | None = None,
    exclude: Iterable[str] = (),
) -> Iterator[RegionClient]:
    """打开 region 会话：先准备 codec，再打开串口，退出时关闭串口。

    ``codec`` 非空时不再加载 DLL，供单测注入假编解码器。
    """
    active = codec if codec is not None else make_region_codec(dll, version)
    transport = open_serial(settings, serial_name, factory=serial_factory, exclude=exclude)
    try:
        yield build_region_client(
            active,
            transport,
            dev_id=dev_id,
            rx_timeout_s=rx_timeout_s,
            retries=retries,
            retry_delay_s=retry_delay_s,
            name=serial_name,
        )
    finally:
        transport.close()


@contextlib.contextmanager
def ble_client_from_settings(
    settings: AppSettings,
    *,
    serial_name: str,
    dll: Path,
    dev_id: int,
    rx_timeout_s: float,
    serial_factory: SerialFactory | None = None,
) -> Iterator[Any]:
    """打开 L5 BLE 会话；退出时关闭串口。"""
    from ppx_testkit.core.protocol.ble_client import BleClient, BleCodecV1

    codec = BleCodecV1(PpxDll(dll))
    transport = open_serial(settings, serial_name, factory=serial_factory)
    try:
        yield BleClient(codec, transport, dev_id=dev_id, rx_timeout_s=rx_timeout_s)
    finally:
        transport.close()

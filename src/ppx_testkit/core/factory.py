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
import importlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

from ppx_testkit.core.monitor.keyword_rules import KeywordConfig, KeywordEvaluator
from ppx_testkit.core.monitor.line_reader import DeviceLogMonitor
from ppx_testkit.core.protocol.dll_loader import PpxDll
from ppx_testkit.core.protocol.region_client import RegionClient, RegionCodecV1, RegionCodecV2
from ppx_testkit.core.registry import lookup_registered
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


def build_chunk_monitor(
    settings: AppSettings,
    transport: SerialTransport,
    cfg: Any,
    *,
    name: str = "device",
    finder: PortFinder | None = None,
) -> Any:
    """按块读取 ``serial.<name>``。供高速设备日志使用，调用方需 ``start`` / ``stop``。

    :param cfg: 含 chunk_read_bytes、drain_grace_s、read_idle_s、success_digit_boundary、reconnect_delay_s。
    :return: :class:`ppx_testkit.core.monitor.chunk_reader.ChunkLogMonitor`。
    """
    from ppx_testkit.core.monitor.chunk_reader import ChunkLogMonitor

    ep = endpoint(settings, name)
    active_finder = finder or PortFinder()

    def resolve_port() -> str:
        return active_finder.find(ep.match, name=name)

    return ChunkLogMonitor(
        transport,
        keyword_config(settings),
        encoding=ep.encoding,
        errors=ep.encoding_errors,
        raw_source=name,
        read_bytes=cfg.chunk_read_bytes,
        drain_grace_s=cfg.drain_grace_s,
        read_idle_s=cfg.read_idle_s,
        digit_boundary=cfg.success_digit_boundary,
        reconnect_delay_s=cfg.reconnect_delay_s,
        resolve_port=resolve_port,
    )


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


def resolve_runner(app: str) -> Callable[..., Any] | None:
    """解析一次运行的入口。已注册的工位或工具优先，否则回退到旧的应用模块。

    回退路径仍是 ``ppx_testkit.apps.<app>.run``。这里不新增按机型或工位名分支的字典。
    现有 YAML 工位都还没注册，因此继续走这条导入。

    :param app: 配置字段 ``app``，例如 ``charge.charge_cycle``。
    :return: 可调用的 ``run``；回退模块存在但没有 ``run`` 时返回 None。
    :raises ImportError: 未注册且 ``ppx_testkit.apps.<app>`` 无法导入。
    """
    meta = lookup_registered(app)
    if meta is not None:
        return _callable_from_registered(meta.target)
    module = importlib.import_module(f"ppx_testkit.apps.{app}")
    runner = getattr(module, "run", None)
    if callable(runner):
        return runner
    return None


def _callable_from_registered(target: Any) -> Callable[..., Any] | None:
    """从注册对象取出可调用入口。

    普通函数直接返回。类或实例则取 ``run``，以保持 ``run(settings, ctx)`` 的旧调用方式。

    :param target: ``StationMeta.target``。
    :return: 可调用对象；无法识别时返回 None。
    """
    if isinstance(target, type):
        runner = getattr(target, "run", None)
        return runner if callable(runner) else None
    if callable(target):
        return target
    runner = getattr(target, "run", None)
    return runner if callable(runner) else None

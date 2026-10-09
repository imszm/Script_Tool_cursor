"""由配置构建常用硬件对象，供各应用共享，避免每个应用重复解析配置。

约定的配置结构::

    serial:
      <name>:                # 如 relay / device / mcb
        baudrate: 115200
        match: {port: COM5}  # 或 description_contains: [CH340]
    relay:   {...}           # RelayConfig
    keywords: {...}          # KeywordConfig
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ppx_testkit.core.monitor.keyword_rules import KeywordConfig, KeywordEvaluator
from ppx_testkit.core.relay.base import RelayConfig, RelayDriver, build_relay
from ppx_testkit.core.serial.port_finder import PortFinder
from ppx_testkit.core.serial.transport import SerialEndpoint, SerialFactory, SerialTransport, open_endpoint
from ppx_testkit.settings import AppSettings


def endpoint(settings: AppSettings, name: str) -> SerialEndpoint:
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


def relay_from_settings(
    settings: AppSettings, transport: SerialTransport, *, key: str = "relay", **kwargs: Any
) -> RelayDriver:
    return build_relay(settings.section(key, RelayConfig), transport, **kwargs)


def keyword_config(settings: AppSettings, key: str = "keywords") -> KeywordConfig:
    return settings.section(key, KeywordConfig, required=False)


def keyword_evaluator(settings: AppSettings, key: str = "keywords") -> KeywordEvaluator:
    return KeywordEvaluator(keyword_config(settings, key))

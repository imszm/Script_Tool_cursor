"""串口定位：显式端口优先，其次按描述 / 设备名 / HWID / VID:PID 匹配。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ppx_testkit.exceptions import ConfigError, PortNotFoundError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortMatcher:
    port: str | None = None
    description_contains: list[str] = field(default_factory=list)
    device_contains: list[str] = field(default_factory=list)
    hwid_contains: list[str] = field(default_factory=list)
    vid: int | None = None
    pid: int | None = None

    def __post_init__(self) -> None:
        if (self.pid is not None) and (self.vid is None):
            raise ConfigError("PortMatcher: 指定 pid 时必须同时指定 vid")

    @property
    def is_explicit(self) -> bool:
        return bool(self.port)

    def has_rules(self) -> bool:
        return bool(self.description_contains or self.device_contains or self.hwid_contains or self.vid is not None)

    def matches(self, info: Any) -> bool:
        desc = (getattr(info, "description", "") or "").lower()
        device = (getattr(info, "device", "") or "").lower()
        hwid = (getattr(info, "hwid", "") or "").lower()
        if self.description_contains and not any(k.lower() in desc for k in self.description_contains):
            return False
        if self.device_contains and not any(k.lower() in device for k in self.device_contains):
            return False
        if self.hwid_contains and not any(k.lower() in hwid for k in self.hwid_contains):
            return False
        if self.vid is not None and getattr(info, "vid", None) != self.vid:
            return False
        if self.pid is not None and getattr(info, "pid", None) != self.pid:
            return False
        return True

    def describe(self) -> str:
        if self.port:
            return f"port={self.port}"
        parts = []
        if self.description_contains:
            parts.append(f"描述含{self.description_contains}")
        if self.device_contains:
            parts.append(f"设备名含{self.device_contains}")
        if self.hwid_contains:
            parts.append(f"HWID含{self.hwid_contains}")
        if self.vid is not None:
            parts.append(f"VID={self.vid:#06x}" + (f",PID={self.pid:#06x}" if self.pid is not None else ""))
        return " 且 ".join(parts) or "<无规则>"


def _default_lister() -> Sequence[Any]:
    from serial.tools import list_ports

    return list(list_ports.comports())


class PortFinder:
    def __init__(self, lister: Callable[[], Iterable[Any]] | None = None) -> None:
        self._lister = lister or _default_lister

    def list_ports(self) -> list[Any]:
        try:
            return sorted(self._lister(), key=lambda p: getattr(p, "device", ""))
        except Exception as exc:  # noqa: BLE001 - 枚举失败统一转换为 PortNotFoundError
            raise PortNotFoundError(None, f"枚举系统串口失败: {exc}") from exc

    def find(self, matcher: PortMatcher, *, exclude: Iterable[str] = (), name: str = "") -> str:
        if matcher.is_explicit:
            return str(matcher.port)
        if not matcher.has_rules():
            raise ConfigError(f"串口 '{name}' 既没有指定 port，也没有任何匹配规则")

        excluded = {e.lower() for e in exclude}
        ports = self.list_ports()
        candidates = [p for p in ports if matcher.matches(p) and p.device.lower() not in excluded]
        if not candidates:
            available = "; ".join(f"{p.device}({p.description})" for p in ports) or "<无>"
            raise PortNotFoundError(None, f"找不到串口 '{name}'（规则: {matcher.describe()}）。当前系统串口: {available}")
        if len(candidates) > 1:
            log.warning(
                "串口 '%s' 匹配到多个候选 %s，使用 %s；建议在 config/local.yaml 中显式指定 port",
                name, [c.device for c in candidates], candidates[0].device,
            )
        chosen = candidates[0]
        log.info("串口 '%s' -> %s (%s)", name, chosen.device, chosen.description)
        return str(chosen.device)

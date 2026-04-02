from __future__ import annotations

from dataclasses import dataclass

try:
    import serial  # type: ignore
    import serial.tools.list_ports  # type: ignore
except Exception:  # pragma: no cover
    serial = None  # type: ignore


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str


def list_ports() -> list[PortInfo]:
    if serial is None:
        raise RuntimeError("pyserial is required. Please install: pip install pyserial")
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append(PortInfo(device=p.device, description=p.description))
    return ports


def detect_port(keyword: str) -> str | None:
    key = (keyword or "").lower()
    if not key:
        return None
    for p in list_ports():
        if key in p.device.lower() or key in (p.description or "").lower():
            return p.device
    return None


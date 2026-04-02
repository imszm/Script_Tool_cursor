from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from script_tool.drivers.serial_driver import SerialConfig, SerialDriver


DriverKey = Literal["relay", "device", "relay_ccb"]


@dataclass(frozen=True)
class SerialDriverFactory:
    config: dict[str, Any]

    def create(self, key: DriverKey) -> SerialDriver:
        s = self.config["serial"]
        timeout_s = float(s.get("timeout_s", 0.1))

        if key == "relay":
            return SerialDriver(
                SerialConfig(
                    port=str(s["relay_port"]),
                    baudrate=int(s["baudrate_relay"]),
                    timeout_s=timeout_s,
                ),
                name="relay",
            )

        if key == "device":
            return SerialDriver(
                SerialConfig(
                    port=str(s["device_port"]),
                    baudrate=int(s["baudrate_device"]),
                    timeout_s=timeout_s,
                ),
                name="device",
            )

        if key == "relay_ccb":
            return SerialDriver(
                SerialConfig(
                    port=str(s["relay_ccb_port"]),
                    baudrate=int(s["baudrate_relay"]),
                    timeout_s=timeout_s,
                ),
                name="relay_ccb",
            )

        raise ValueError(f"unknown driver key: {key}")


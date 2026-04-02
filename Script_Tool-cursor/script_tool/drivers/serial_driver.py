from __future__ import annotations

import logging
import time
from dataclasses import dataclass

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None  # type: ignore


@dataclass(frozen=True)
class SerialConfig:
    port: str
    baudrate: int
    timeout_s: float = 0.1


class SerialDriver:
    """
    通用串口驱动：支持继电器控制(Write)和设备日志监听(Read)。
    """

    def __init__(self, cfg: SerialConfig, name: str):
        self.cfg = cfg
        self.name = name
        self.ser = None
        self.logger = logging.getLogger(f"script_tool.driver.{name}")

    def connect(self, retries: int = 2, retry_delay_s: float = 0.5) -> None:
        if serial is None:
            raise RuntimeError("pyserial is required. Please install: pip install pyserial")

        last_err: Exception | None = None
        for attempt in range(1, int(retries) + 2):
            try:
                self.ser = serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=self.cfg.timeout_s)
                self.logger.info(f"[{self.name}] Connected {self.cfg.port} @ {self.cfg.baudrate}")
                return
            except Exception as e:
                last_err = e
                self.logger.warning(f"[{self.name}] Connect attempt {attempt} failed: {e}")
                time.sleep(float(retry_delay_s))
        raise RuntimeError(f"[{self.name}] Connect failed after retries: {last_err}") from last_err

    def ensure_connected(self) -> None:
        if self.ser is None or not getattr(self.ser, "is_open", False):
            self.connect()

    def send_bytes(self, cmd: bytes, desc: str = "") -> None:
        self.ensure_connected()
        try:
            self.ser.write(cmd)
        except Exception as e:
            raise RuntimeError(f"[{self.name}] Send failed ({desc}): {e}") from e

    def send_hex_list(self, hex_list: list[int], desc: str = "") -> None:
        self.send_bytes(bytes(hex_list), desc=desc)

    def send_ascii(self, text: str, desc: str = "") -> None:
        self.send_bytes(text.encode("ascii", errors="ignore"), desc=desc)

    def read_line(self, encoding: str = "utf-8") -> str | None:
        self.ensure_connected()
        try:
            if getattr(self.ser, "in_waiting", 0):
                return self.ser.readline().decode(encoding, errors="ignore").strip()
        except Exception as e:
            raise RuntimeError(f"[{self.name}] Read line failed: {e}") from e
        return None

    def read_buffer(self, encoding: str = "utf-8") -> str:
        self.ensure_connected()
        try:
            waiting = int(getattr(self.ser, "in_waiting", 0))
            if waiting <= 0:
                return ""
            raw = self.ser.read(waiting)
            return raw.decode(encoding, errors="ignore")
        except Exception as e:
            raise RuntimeError(f"[{self.name}] Read buffer failed: {e}") from e

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def close(self) -> None:
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            self.logger.exception(f"[{self.name}] Close failed")


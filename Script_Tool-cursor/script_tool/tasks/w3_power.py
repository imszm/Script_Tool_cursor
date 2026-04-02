from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from script_tool.core.context import RunContext
from script_tool.drivers.factory import SerialDriverFactory
from script_tool.drivers.serial_driver import SerialDriver


@dataclass
class W3PowerTask:
    name: str = "w3-power"

    def __init__(self, args: Any, config: dict[str, Any]):
        self.args = args
        self.config = config

    def setup(self, ctx: RunContext) -> None:
        f = SerialDriverFactory(self.config)
        relay = f.create("relay")
        device = f.create("device")
        relay.connect()
        device.connect()
        ctx.drivers["relay"] = relay
        ctx.drivers["device"] = device

        cmds = self.config["commands"]
        relay.send_hex_list(cmds["hex_release_off"], desc="reset_release")
        relay.send_hex_list(cmds["hex_enable"], desc="enable")
        time.sleep(1.0)

    def run(self, ctx: RunContext) -> int:
        relay: SerialDriver = ctx.drivers["relay"]
        dut: SerialDriver = ctx.drivers["device"]
        cmds = self.config["commands"]
        kw = self.config["keywords"]

        loops = int(self.args.loops)
        press_on_s = float(getattr(self.args, "press_on_s", 2.5))
        press_off_s = float(getattr(self.args, "press_off_s", 1.0))
        monitor_s = float(getattr(self.args, "monitor_s", 10.0))

        def relay_action(duration: float, action: str) -> None:
            relay.send_hex_list(cmds["hex_press_on"], desc=f"{action}_press")
            start = time.time()
            while time.time() - start < duration:
                time.sleep(0.05)
            relay.send_hex_list(cmds["hex_release_off"], desc=f"{action}_release")

        for i in range(1, loops + 1):
            ctx.logger.info(f"=== W3 Cycle {i}/{loops} ===")

            relay_action(press_on_s, "power_on")

            end_t = time.time() + monitor_s
            while time.time() < end_t:
                line = dut.read_line(encoding="utf-8")
                if not line:
                    continue
                if kw["w3_stop"] in line:
                    ctx.logger.error(f"Stop keyword detected: {line}")
                    return 2
                if kw["w3_error"] in line.lower():
                    ctx.logger.error(f"Communication loss: {line}")
                    return 3

            relay_action(press_off_s, "power_off")
            time.sleep(3.0)

        return 0

    def teardown(self, ctx: RunContext) -> None:
        # 安全松开
        relay = ctx.drivers.get("relay")
        if relay:
            try:
                relay.send_hex_list(self.config["commands"]["hex_release_off"], desc="final_release")
            except Exception:
                ctx.logger.exception("Failed to release relay")


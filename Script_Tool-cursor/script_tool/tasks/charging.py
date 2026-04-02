from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

from script_tool.core.context import RunContext
from script_tool.drivers.factory import SerialDriverFactory
from script_tool.drivers.serial_driver import SerialDriver


@dataclass
class ChargingTask:
    name: str = "charging"

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

        relay.send_hex_list(self.config["commands"]["hex_enable"], desc="enable")
        time.sleep(1.0)

    def run(self, ctx: RunContext) -> int:
        relay: SerialDriver = ctx.drivers["relay"]
        dut: SerialDriver = ctx.drivers["device"]
        cmds = self.config["commands"]
        kw = self.config["keywords"]

        loops = int(self.args.loops)
        success = 0

        for i in range(1, loops + 1):
            ctx.logger.info(f"--- Charging Cycle {i}/{loops} ---")

            relay.send_hex_list(cmds["hex_release_off"], desc="charge_on")
            time.sleep(random.uniform(3.0, 5.0))
            log1 = dut.read_buffer(encoding="utf-8")

            relay.send_hex_list(cmds["hex_press_on"], desc="charge_off")
            time.sleep(5.0)
            log2 = dut.read_buffer(encoding="utf-8")

            full = (log1 + log2).replace(" ", "").lower()
            found_success = any(str(k).replace(" ", "").lower() in full for k in kw["charge_success"])
            found_error = any(str(k).replace(" ", "").lower() in full for k in kw["charge_error"])

            if found_error:
                ctx.logger.error("Found fatal keyword (assertion failed).")
                return 3
            if found_success:
                success += 1
                ctx.logger.info("Result: PASS")
            else:
                ctx.logger.warning("Result: FAIL (no success keyword)")

        ctx.logger.info(f"Charging finished: {success}/{loops}")
        return 0

    def teardown(self, ctx: RunContext) -> None:
        relay = ctx.drivers.get("relay")
        if relay:
            try:
                relay.send_hex_list(self.config["commands"]["hex_press_on"], desc="final_off")
            except Exception:
                ctx.logger.exception("Failed to turn off relay")


from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from script_tool.core.context import RunContext
from script_tool.drivers.factory import SerialDriverFactory
from script_tool.drivers.serial_driver import SerialDriver


@dataclass
class FixtureTurnSignalTask:
    name: str = "fixture-turn-signal"

    def __init__(self, args: Any, config: dict[str, Any]):
        self.args = args
        self.config = config

        # 来自 `治具&上位机压力测试工具/治具工具软件压力测试（带继电器版）-V1.3.py`
        self.cmd = {
            "K2_ON": bytes.fromhex("A0 02 01 A3"),
            "K2_OFF": bytes.fromhex("A0 02 00 A2"),
            "K3_ON": bytes.fromhex("A0 03 01 A4"),
            "K3_OFF": bytes.fromhex("A0 03 00 A3"),
        }

    def setup(self, ctx: RunContext) -> None:
        f = SerialDriverFactory(self.config)
        relay = f.create("relay")
        relay.connect()
        ctx.drivers["relay"] = relay

        relay.send_bytes(self.cmd["K2_OFF"], desc="init_k2_off")
        relay.send_bytes(self.cmd["K3_OFF"], desc="init_k3_off")
        time.sleep(0.5)

    def run(self, ctx: RunContext) -> int:
        relay: SerialDriver = ctx.drivers["relay"]

        loops = int(self.args.loops)
        press_time = float(getattr(self.args, "press_time", 0.6))
        release_time = float(getattr(self.args, "release_time", 0.2))
        interval = float(getattr(self.args, "interval", 0.5))
        light_on_time = float(getattr(self.args, "light_on_time", 0.8))

        success = 0
        state = "LEFT"

        for i in range(1, loops + 1):
            ctx.logger.info(f"[{i}/{loops}] state={state}")
            try:
                relay.send_bytes(self.cmd["K2_OFF"], desc="cycle_k2_off")
                relay.send_bytes(self.cmd["K3_OFF"], desc="cycle_k3_off")
                time.sleep(0.3)

                time.sleep(press_time)
                if state == "LEFT":
                    relay.send_bytes(self.cmd["K2_ON"], desc="left_on")
                    relay.send_bytes(self.cmd["K3_OFF"], desc="right_off")
                else:
                    relay.send_bytes(self.cmd["K3_ON"], desc="right_on")
                    relay.send_bytes(self.cmd["K2_OFF"], desc="left_off")

                time.sleep(light_on_time)
                time.sleep(release_time)

                relay.send_bytes(self.cmd["K2_OFF"], desc="all_off_k2")
                relay.send_bytes(self.cmd["K3_OFF"], desc="all_off_k3")
                time.sleep(interval)

                success += 1
                state = "RIGHT" if state == "LEFT" else "LEFT"
            except Exception as e:
                ctx.logger.error(f"Cycle failed: {e}")
                return 3

        ctx.logger.info(f"Done: {success}/{loops}")
        return 0

    def teardown(self, ctx: RunContext) -> None:
        relay = ctx.drivers.get("relay")
        if relay:
            try:
                relay.send_bytes(self.cmd["K2_OFF"], desc="final_k2_off")
                relay.send_bytes(self.cmd["K3_OFF"], desc="final_k3_off")
            except Exception:
                ctx.logger.exception("Failed to final-off relays")


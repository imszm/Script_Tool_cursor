from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from script_tool.core.context import RunContext
from script_tool.drivers.factory import SerialDriverFactory
from script_tool.drivers.serial_driver import SerialDriver
from script_tool.drivers.ui_automation import connect_window_by_title_re, screenshot
from script_tool.drivers.vision import detect_pass_fail_by_points

try:
    import win32api  # type: ignore
    import win32con  # type: ignore
except Exception:  # pragma: no cover
    win32api = None  # type: ignore
    win32con = None  # type: ignore

@dataclass
class CcbSmtTask:
    name: str = "ccb-smt"

    def __init__(self, args: Any, config: dict[str, Any]):
        self.args = args
        self.config = config

    def setup(self, ctx: RunContext) -> None:
        if win32api is None or win32con is None:
            raise RuntimeError("pywin32 is required. Please install: pip install pywin32")

        f = SerialDriverFactory(self.config)
        relay = f.create("relay_ccb")
        relay.connect()
        ctx.drivers["relay"] = relay

    def _fast_click(self, x: int, y: int) -> None:
        win32api.SetCursorPos((int(x), int(y)))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)

    def run(self, ctx: RunContext) -> int:
        relay: SerialDriver = ctx.drivers["relay"]
        pc = self.config["pc_tool"]
        cmds = self.config["commands"]

        title_re = pc["ccb_title_regex"]
        prefix = pc["ccb_serial_prefix"]

        try:
            h = connect_window_by_title_re(title_re, timeout_s=10.0)
            win = h.win
            win.set_focus()
        except Exception as e:
            ctx.logger.error(f"Failed to connect CCB app: {e}")
            return 2

        loops = int(self.args.loops)
        for i in range(loops):
            sn = f"{prefix}{str(i).zfill(4)}"
            ctx.logger.info(f"SN: {sn}")

            # relay power cycle
            relay.send_ascii(str(cmds["ascii_off"]), desc="power_off")
            time.sleep(2.0)
            relay.send_ascii(str(cmds["ascii_on"]), desc="power_on")
            time.sleep(2.0)

            try:
                edit = win.child_window(auto_id="Widget.lineEditSerialNumber", control_type="Edit")
                edit.set_edit_text(sn)
                win.type_keys("{ENTER}")
            except Exception:
                ctx.logger.exception("Failed to input SN")
                continue

            time.sleep(5.0)
            c1 = pc["ccb_coords"]["pass_light"]
            c2 = pc["ccb_coords"]["pass_horn"]
            self._fast_click(int(c1[0]), int(c1[1]))
            time.sleep(0.5)
            self._fast_click(int(c2[0]), int(c2[1]))

            ctx.logger.info("Waiting for result color...")
            res = "TIMEOUT"
            for _ in range(80):
                status = detect_pass_fail_by_points(
                    [(int(x), int(y)) for (x, y) in pc["ccb_check_points"]]
                )
                if status:
                    res = status
                    break
                time.sleep(1.0)

            ctx.logger.info(f"Result: {res}")
            if res == "FAIL":
                out = ctx.paths.artifacts_dir / f"ccb_fail_{sn}.png"
                screenshot(win, out)
                return 3

        return 0

    def teardown(self, ctx: RunContext) -> None:
        pass


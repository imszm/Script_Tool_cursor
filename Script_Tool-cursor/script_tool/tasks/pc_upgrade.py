from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from script_tool.core.context import RunContext, RunPaths
from script_tool.drivers.ui_automation import connect_window_by_title, screenshot


@dataclass
class PcUpgradeTask:
    name: str = "pc-upgrade"

    def __init__(self, args: Any, config: dict[str, Any], paths: RunPaths):
        self.args = args
        self.config = config
        self.paths = paths

    def setup(self, ctx: RunContext) -> None:
        (self.paths.artifacts_dir / "screenshots").mkdir(parents=True, exist_ok=True)

    def run(self, ctx: RunContext) -> int:
        pc = self.config["pc_tool"]
        title = pc["upgrade_app_title"]
        wait_s = float(pc["upgrade_wait_time_s"])

        try:
            ctx.logger.info(f"Connecting app: {title}")
            h = connect_window_by_title(title, timeout_s=10.0)
            win = h.win
        except Exception as e:
            ctx.logger.error(f"Connect failed. Please start the app first: {e}")
            return 2

        btn = win.child_window(auto_id=pc["upgrade_btn_id"], control_type="Button")
        log_box = win.child_window(auto_id=pc["upgrade_log_id"], control_type="Edit")

        loops = int(self.args.loops)
        for i in range(1, loops + 1):
            ctx.logger.info(f"--- PC Upgrade Cycle {i}/{loops} ---")
            old_len = len(log_box.get_value())

            btn.click()
            ctx.logger.info(f"Waiting {wait_s} seconds...")
            time.sleep(wait_s)

            full = log_box.get_value()
            new = full[old_len:]
            if ("失败" in new) or ("超时" in new) or ("fail" in new.lower()) or ("timeout" in new.lower()):
                ctx.logger.error("Upgrade failed, saving screenshot.")
                out = self.paths.artifacts_dir / "screenshots" / f"upgrade_fail_{i}.png"
                screenshot(win, out)
                return 3

            ctx.logger.info("Result: PASS")

        return 0

    def teardown(self, ctx: RunContext) -> None:
        pass


from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

from script_tool.core.context import RunContext, RunPaths
from script_tool.drivers.ui_automation import connect_window_by_title_re


@dataclass
class W3PcToolStressTask:
    name: str = "w3-pc-tool-stress"

    def __init__(self, args: Any, config: dict[str, Any], paths: RunPaths):
        self.args = args
        self.config = config
        self.paths = paths

    def setup(self, ctx: RunContext) -> None:
        (self.paths.artifacts_dir / "screenshots").mkdir(parents=True, exist_ok=True)

    def run(self, ctx: RunContext) -> int:
        # 这个任务来自 `总装测试工具/PC_tool_工具软件压力测试-V1.2.py`
        # 当前先做“可工程化复用”的骨架：连接窗口 + 循环执行（后续可逐按钮拆分成步骤配置）
        title_re = "W3 PCTOOL.*"
        try:
            h = connect_window_by_title_re(title_re, timeout_s=10.0)
            dlg = h.win
            dlg.wait("ready", timeout=5)  # type: ignore[attr-defined]
        except Exception as e:
            ctx.logger.error(f"Connect W3 PCTOOL failed: {e}")
            return 2

        loops = int(self.args.loops)
        ctx.logger.info(f"W3 PCTOOL stress loops={loops}")

        for i in range(1, loops + 1):
            ctx.logger.info(f"--- Cycle {i}/{loops} ---")
            # 先保守：仅验证窗口仍存在，避免“闪退无限重连”
            try:
                if not dlg.exists(timeout=1):
                    ctx.logger.error("PCTOOL window disappeared (crash?)")
                    return 3
            except Exception:
                ctx.logger.exception("Check window failed")
                return 3

            # TODO: 后续把 V1.2 脚本的各按钮动作映射为可配置步骤
            time.sleep(random.uniform(0.2, 0.6))

        return 0

    def teardown(self, ctx: RunContext) -> None:
        pass


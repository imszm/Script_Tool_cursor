from __future__ import annotations

from collections.abc import Callable
from typing import Any

from script_tool.core.context import RunPaths
from script_tool.tasks.w3_power import W3PowerTask
from script_tool.tasks.charging import ChargingTask
from script_tool.tasks.pc_upgrade import PcUpgradeTask
from script_tool.tasks.ccb_smt import CcbSmtTask
from script_tool.tasks.w3_pc_tool_stress import W3PcToolStressTask
from script_tool.tasks.fixture_turn_signal import FixtureTurnSignalTask
from script_tool.tasks.ccb_smt_fuzzy import CcbSmtFuzzyTask


TaskFactory = Callable[[Any, dict[str, Any], RunPaths], Any]


def build_registry() -> dict[str, Callable[..., Any]]:
    return {
        "w3-power": lambda args, config, paths: W3PowerTask(args=args, config=config),
        "charging": lambda args, config, paths: ChargingTask(args=args, config=config),
        "pc-upgrade": lambda args, config, paths: PcUpgradeTask(args=args, config=config, paths=paths),
        "ccb-smt": lambda args, config, paths: CcbSmtTask(args=args, config=config),
        "w3-pc-tool-stress": lambda args, config, paths: W3PcToolStressTask(args=args, config=config, paths=paths),
        "fixture-turn-signal": lambda args, config, paths: FixtureTurnSignalTask(args=args, config=config),
        "ccb-smt-fuzzy": lambda args, config, paths: CcbSmtFuzzyTask(args=args, config=config),
    }


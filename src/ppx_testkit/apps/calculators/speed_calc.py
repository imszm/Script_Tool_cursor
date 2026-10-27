"""兼容壳。车速计算器已迁到 ``ppx_testkit.tools.calculators.speed_calc``。"""

from ppx_testkit.tools.calculators.speed_calc import (
    INCH_TO_M,
    KM_PER_MILE,
    SpeedCalcConfig,
    SpeedCase,
    SpeedResult,
    compute_speed,
    evaluate_text_inputs,
    format_result,
    run,
    run_cli,
    validate_inputs,
)

__all__ = [
    "INCH_TO_M",
    "KM_PER_MILE",
    "SpeedCalcConfig",
    "SpeedCase",
    "SpeedResult",
    "compute_speed",
    "evaluate_text_inputs",
    "format_result",
    "run",
    "run_cli",
    "validate_inputs",
]

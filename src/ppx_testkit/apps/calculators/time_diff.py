"""兼容壳。时间差计算器已迁到 ``ppx_testkit.tools.calculators.time_diff``。"""

from ppx_testkit.tools.calculators.time_diff import (
    DEFAULT_FORMATS,
    FORMAT_HINT,
    TimeDiffConfig,
    TimeDiffResult,
    TimePair,
    format_hours,
    parse_time,
    run,
    run_cli,
    time_diff,
    time_diff_hours,
)

__all__ = [
    "DEFAULT_FORMATS",
    "FORMAT_HINT",
    "TimeDiffConfig",
    "TimeDiffResult",
    "TimePair",
    "format_hours",
    "parse_time",
    "run",
    "run_cli",
    "time_diff",
    "time_diff_hours",
]

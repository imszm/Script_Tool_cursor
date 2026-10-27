"""兼容壳。P3 通道状态机已迁到 ``ppx_testkit.profiles.p3``。"""

from ppx_testkit.profiles.p3 import (
    FAIL,
    PASS,
    WAIT_DATABASE,
    WAIT_FINAL_PASS,
    WAIT_STATUS,
    ChannelState,
    PhaseClock,
    advance,
    check_timeout,
    fail,
    green_decision,
    register_green,
)

__all__ = [
    "FAIL",
    "PASS",
    "WAIT_DATABASE",
    "WAIT_FINAL_PASS",
    "WAIT_STATUS",
    "ChannelState",
    "PhaseClock",
    "advance",
    "check_timeout",
    "fail",
    "green_decision",
    "register_green",
]

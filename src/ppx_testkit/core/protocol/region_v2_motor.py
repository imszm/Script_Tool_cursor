"""兼容壳。R3 电机寄存器操作已迁到 ``ppx_testkit.profiles.r3``。"""

from ppx_testkit.profiles.r3 import (
    CLR_ERRCODE_BIT,
    MotorLink,
    RegionClientLike,
    call_reconnect,
    has_reconnect,
    read_regs,
    write_brake_state,
    write_clear_error,
    write_motion,
    write_run_mode,
    write_speed_only,
)

__all__ = [
    "CLR_ERRCODE_BIT",
    "MotorLink",
    "RegionClientLike",
    "call_reconnect",
    "has_reconnect",
    "read_regs",
    "write_brake_state",
    "write_clear_error",
    "write_motion",
    "write_run_mode",
    "write_speed_only",
]

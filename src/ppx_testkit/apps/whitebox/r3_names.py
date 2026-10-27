"""兼容壳。R3 名称、菜单和日志器已迁到 ``ppx_testkit.profiles.r3``。"""

from ppx_testkit.profiles.r3 import BRAKE_NAMES, MENU, MODE_NAMES, MOTOR_STOP_BIT, STABILITY_CASES, log

__all__ = ["BRAKE_NAMES", "MENU", "MODE_NAMES", "MOTOR_STOP_BIT", "STABILITY_CASES", "log"]

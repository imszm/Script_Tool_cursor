"""R3 MCB 白盒用例共享的名称、菜单和日志器。

单独放在这里，供编排入口和用例模块共同引用，避免互相导入。
"""

from __future__ import annotations

from ppx_testkit.core.protocol.ppx_types import BrakeStateV2, DatSettingV2
from ppx_testkit.logger import get_logger

log = get_logger("ppx_testkit.apps.whitebox.r3_mcb")

# 与旧脚本一致：MOTOR_STOP 位（1<<10）写入 rt_setting 寄存器
MOTOR_STOP_BIT = int(DatSettingV2.MOTOR_STOP)

MODE_NAMES: dict[int, str] = {
    0: "空闲", 1: "设置", 2: "骑行", 3: "锁车", 4: "助力推行", 5: "紧急刹车", 8: "康复助行",
    9: "康复训练", 10: "延迟关刹", 11: "立即关刹", 12: "电磁刹解锁",
}
BRAKE_NAMES: dict[int, str] = {
    BrakeStateV2.CLOSED: "闭合(锁定)", BrakeStateV2.OPENING: "解锁中", BrakeStateV2.OPENED: "已解锁",
}

STABILITY_CASES = (
    "long_run", "burst", "idle_recovery", "mode_switch", "ramp_repeat", "speed_jump", "noise", "health_monitor",
)

MENU = (
    "\n==================================================\n"
    "  MCB 电机控制菜单 (四轮代步车/轮椅测试)\n"
    "==================================================\n"
    "  0. 读取电机状态\n"
    "  1. 启动电机 (低速)\n"
    "  2. 启动电机 (中速)\n"
    "  3. 启动电机 (自定义速度)\n"
    "  4. 停止电机\n"
    "  5. 速度斜坡测试\n"
    "  6. 设置目标电流\n"
    "  7. 清除故障码\n"
    "  8. 切换运行模式\n"
    "  9. 读取电磁刹状态\n"
    "  a. 执行全自动稳定性测试\n"
    "  q. 退出\n"
    "--------------------------------------------------"
)

"""R3 机型适配：DLL 路径、默认协议参数、名称枚举和 Region V2 电机操作。

名称与菜单仍使用原来的白盒日志器。电机通信日志仍写入
ppx_testkit.core.protocol.region_v2_motor，避免旧日志过滤失效。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from ppx_testkit.core.protocol.ppx_types import BrakeStateV2, DatSettingV2, DevId, RegV2, RtSetting
from ppx_testkit.exceptions import HardwareError, ProtocolError, SerialDisconnectedError
from ppx_testkit.logger import get_logger
from ppx_testkit.profiles.base import ProtocolProfile

R3 = ProtocolProfile(
    model="r3",
    version="v2",
    dll_subdir="r3",
    region_dll="ppx_region.dll",
    dev_id=int(DevId.MCB),
    rx_timeout_s=0.3,
    retries=3,
    retry_delay_s=0.1,
    baudrate=460800,
)


def region_dll() -> Path:
    """返回 R3 ppx_region.dll 的绝对路径。

    :return: resources/dll/r3/ppx_region.dll。
    """
    return R3.dll_file("region")


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


_motor_log = logging.getLogger("ppx_testkit.core.protocol.region_v2_motor")

CLR_ERRCODE_BIT = int(RtSetting.CLR_ERRCODE)


class RegionClientLike(Protocol):
    """电机门面依赖的 RegionClient 子集。"""

    transport: Any

    def read(self, reg: int, nums: int = 1, *, label: str = "") -> Any: ...

    def write(
        self,
        reg: int,
        fields: Mapping[str, Any],
        *,
        nums: int = 1,
        expect_response: bool = True,
        label: str = "",
    ) -> Any: ...


def has_reconnect(client: Any) -> bool:
    """串口对象是否提供 reconnect。没有时由调用方决定是原样抛出还是熔断。"""
    transport = getattr(client, "transport", None)
    return transport is not None and hasattr(transport, "reconnect")


def call_reconnect(client: Any, delay_s: float) -> None:
    """按同一端口重连。调用前应先用 :func:`has_reconnect` 确认。失败抛出串口异常。"""
    client.transport.reconnect(delay_s=delay_s)


def write_clear_error(client: RegionClientLike, *, label: str) -> Any:
    """写 RT_SETTING 的清故障位。"""
    return client.write(RegV2.RT_SETTING, {"rt_setting": CLR_ERRCODE_BIT}, label=label)


def write_run_mode(client: RegionClientLike, mode: int, *, label: str) -> Any:
    """写运行模式寄存器。"""
    return client.write(RegV2.RUN_MODE, {"run_mode": mode}, label=label)


def write_motion(
    client: RegionClientLike,
    speed: int,
    accel: int,
    current: int,
    *,
    label: str,
) -> Any:
    """一次写入目标转速、加速度、电流（reg_nums=3）。"""
    return client.write(
        RegV2.TARGET_SPEED,
        {"target_speed": speed, "target_accel": accel, "target_current": current},
        nums=3,
        label=label,
    )


def write_speed_only(client: RegionClientLike, speed: int, *, label: str) -> Any:
    """只写目标转速。"""
    return client.write(RegV2.TARGET_SPEED, {"target_speed": speed}, label=label)


def write_brake_state(client: RegionClientLike, state: int, *, label: str) -> Any:
    """写电磁刹状态寄存器。"""
    return client.write(RegV2.BRAKE_STATE, {"brake_state": state}, label=label)


def read_regs(client: RegionClientLike, reg: int, nums: int = 1, *, label: str = "") -> Any:
    """读一段连续寄存器。"""
    return client.read(reg, nums, label=label)


class MotorLink:
    """对 RegionClient 的业务封装：通信失败返回 False/None，串口断开时重连一次。"""

    def __init__(
        self,
        client: RegionClientLike,
        label: str,
        *,
        retries: int = 3,
        retry_delay_s: float = 0.1,
        reconnect_delay_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.label = label
        self.retries = max(1, retries)
        self.retry_delay_s = retry_delay_s
        self.reconnect_delay_s = reconnect_delay_s
        self._sleep = sleep

    def _reconnect(self, exc: SerialDisconnectedError) -> None:
        if not has_reconnect(self.client):
            raise exc
        _motor_log.warning("%s串口断开，尝试重连: %s", self.label, exc)
        call_reconnect(self.client, self.reconnect_delay_s)  # 失败抛出 SerialOpenError，由上层终止测试
        _motor_log.info("%s串口重连成功", self.label)

    def _try(self, desc: str, fn: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, fn()
        except SerialDisconnectedError as exc:
            self._reconnect(exc)
        except (HardwareError, ProtocolError) as exc:
            _motor_log.warning("%s %s 失败: %s", self.label, desc, exc)
        return False, None

    def command(self, desc: str, fn: Callable[[], Any]) -> bool:
        """执行一条指令，失败时按 retries 重试。返回是否最终成功。"""
        for i in range(1, self.retries + 1):
            ok, _ = self._try(desc, fn)
            if ok:
                _motor_log.info("%s %s 成功", self.label, desc)
                return True
            _motor_log.warning("%s %s 失败，重试 %d/%d", self.label, desc, i, self.retries)
            if i < self.retries and self.retry_delay_s > 0:
                self._sleep(self.retry_delay_s)
        _motor_log.error("%s %s 重试 %d 次后仍失败", self.label, desc, self.retries)
        return False

    def clear_error(self) -> bool:
        """清除错误码。通信失败返回 False。"""
        ok, _ = self._try(
            "清除错误码",
            lambda: write_clear_error(self.client, label=f"{self.label}清错"),
        )
        return ok

    def set_run_mode(self, mode: int) -> bool:
        """切换运行模式。"""
        return self.command(
            f"切换模式{mode}",
            lambda: write_run_mode(self.client, mode, label=f"{self.label}模式{mode}"),
        )

    def set_speed(self, speed: int, accel: int, current: int, *, retry: bool = True) -> bool:
        """下发转速、加速度和电流。``retry=False`` 时只尝试一次。"""
        desc = f"设置转速{speed}/加速度{accel}/电流{current}"

        def _do() -> Any:
            return write_motion(self.client, speed, accel, current, label=f"{self.label}转速{speed}")

        if retry:
            return self.command(desc, _do)
        return self._try(desc, _do)[0]

    def write_brake(self, state: int) -> bool:
        """写刹车状态。"""
        return self.command(
            f"写刹车状态{state}",
            lambda: write_brake_state(self.client, state, label=f"{self.label}刹车{state}"),
        )

    def read_error_code(self) -> int | None:
        """读取故障码。失败返回 None。"""
        ok, data = self._try(
            "读错误码",
            lambda: read_regs(self.client, RegV2.MCU_ERRCODE, 1, label=f"{self.label}错误码"),
        )
        return int(data.mcu_errcode) if ok else None

    def read_brake_state(self) -> int | None:
        """读取刹车状态。失败返回 None。"""
        ok, data = self._try(
            "读刹车状态",
            lambda: read_regs(self.client, RegV2.BRAKE_STATE, 1, label=f"{self.label}刹车"),
        )
        return int(data.brake_state) if ok else None

    def read_temperatures(self) -> tuple[int, int] | None:
        """读取 MOSFET 温度和电机温度。失败返回 None。"""
        ok, data = self._try(
            "读温度",
            lambda: read_regs(self.client, RegV2.MOSFET_TEMP, 2, label=f"{self.label}温度"),
        )
        return (int(data.mosfet_temp), int(data.motor_temp)) if ok else None

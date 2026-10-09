"""R3 MCB 电机控制白盒 / 稳定性测试（v2 region 协议）。

迁移自 ``R3_MCB白盒测试/PPX_MCB_V2.PY``，保留原有寄存器序列、时序与判定阈值，
端口、DLL、用例参数、阈值全部来自 ``config/stations/r3_mcb_whitebox.yaml`` 的
``protocol`` / ``r3_mcb`` 段。

``r3_mcb.action`` 对应旧脚本的命令行开关::

    interactive  交互菜单（旧脚本默认）
    full         全自动白盒 + 稳定性测试（--fulltest）
    stress       仅稳定性压力测试（--stress）
    status       读取电机状态（--status）
    stop         停止电机（--stop）
    start        以 start_speed_rpm 启动电机并保持运转（--auto）
    ramp         斜坡测试（--ramp）

异常策略：单步通信/协议失败（HardwareError / ProtocolError）只判定该步 FAIL；
串口断开时尝试重连一次，失败则熔断（TestAbort）。无论正常结束还是异常退出，
驱动过电机的动作都会执行安全停机（速度归零 + 空闲模式），串口由 :func:`run` 在 finally 中关闭。
"""

from __future__ import annotations

import builtins
import datetime as _dt
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ppx_testkit.core.factory import open_serial
from ppx_testkit.core.protocol.dll_loader import PpxDll
from ppx_testkit.core.protocol.ppx_types import BrakeStateV2, DatSettingV2, DevId, ModeV2, RegV2, RtSetting
from ppx_testkit.core.protocol.region_client import RegionClient, RegionCodecV2
from ppx_testkit.core.report.writers import write_csv, write_html
from ppx_testkit.exceptions import (
    ConfigError,
    DeviceExceptionResponse,
    HardwareError,
    ProtocolError,
    SerialDisconnectedError,
    TestAbort,
    ToolError,
)
from ppx_testkit.logger import RunContext, get_logger
from ppx_testkit.settings import AppSettings

log = get_logger(__name__)

# 与旧脚本一致：MOTOR_STOP 位（1<<10）写入 rt_setting 寄存器
MOTOR_STOP_BIT = int(DatSettingV2.MOTOR_STOP)
CLR_ERRCODE_BIT = int(RtSetting.CLR_ERRCODE)

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
Action = Literal["interactive", "full", "stress", "status", "stop", "start", "ramp"]
_MOTION_ACTIONS = {"interactive", "full", "stress", "ramp"}

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


# ============================================================ 配置
@dataclass(frozen=True)
class R3ProtocolConfig:
    dll_path: Path
    dev_id: int = DevId.MCB
    rx_timeout_s: float = 0.3
    retries: int = 2
    retry_delay_s: float = 0.05

    def __post_init__(self) -> None:
        if self.rx_timeout_s <= 0:
            raise ConfigError("protocol.rx_timeout_s 必须 > 0")
        if self.retries < 1:
            raise ConfigError("protocol.retries 至少为 1")
        if self.retry_delay_s < 0:
            raise ConfigError("protocol.retry_delay_s 不能为负数")


@dataclass(frozen=True)
class RampProfile:
    max_speed: int = 500
    step: int = 50
    dwell_s: float = 1.0

    def __post_init__(self) -> None:
        if self.step <= 0:
            raise ConfigError(f"斜坡步进必须 > 0，实际 {self.step}")
        if self.max_speed < 0:
            raise ConfigError(f"斜坡最大速度不能为负数，实际 {self.max_speed}")
        if self.dwell_s < 0:
            raise ConfigError("斜坡停留时间不能为负数")


@dataclass(frozen=True)
class ModeStep:
    mode: int
    name: str


def _default_mode_steps() -> list[ModeStep]:
    return [ModeStep(0, "空闲"), ModeStep(3, "锁车"), ModeStep(4, "助力推行"), ModeStep(10, "延迟关刹"),
            ModeStep(11, "立即关刹"), ModeStep(12, "电磁刹解锁"), ModeStep(0, "恢复空闲")]


@dataclass(frozen=True)
class StatusLimits:
    """全状态寄存器扫描的合理范围（闭区间）。"""

    bus_voltage_v: tuple[float, float] = (0.0, 60.0)
    bus_current_a: tuple[float, float] = (0.0, 30.0)
    target_speed: tuple[int, int] = (-10000, 10000)
    target_accel: tuple[int, int] = (0, 20000)
    motor_speed: tuple[int, int] = (-10000, 10000)


@dataclass(frozen=True)
class BasicTests:
    enabled: bool = True
    mode_steps: list[ModeStep] = field(default_factory=_default_mode_steps)
    speed_points: list[int] = field(default_factory=lambda: [100, 300, 600])
    speed_tol_abs: int = 10
    speed_tol_ratio: float = 0.1
    current_points: list[int] = field(default_factory=lambda: [50, 100])  # 单位 0.1A
    current_tol: int = 5
    ramp: RampProfile = field(default_factory=lambda: RampProfile(300, 100, 0.8))
    boundary_speed: int = -100
    invalid_read_reg: int = 0xFF
    invalid_write_reg: int = 0x00
    invalid_write_value: int = 0x12
    readonly_write_value: int = 1234
    invalid_mode: int = 13
    stop_flag_speed: int = 200
    stop_flag_abs: int = 10
    stop_flag_ratio: float = 0.5
    rapid_count: int = 10
    rapid_interval_s: float = 0.05
    status_scan_nums: int = 25
    status_limits: StatusLimits = field(default_factory=StatusLimits)
    response_time_max_ms: float = 200.0


def _default_noise() -> list[bytes]:
    return [bytes([0x00, 0x00, 0x00]), bytes([0xA5, 0x00, 0x00, 0x00, 0x55]), bytes([0xFF] * 20),
            bytes([0xA5, 0x20, 0x80, 0x00, 0x00, 0x55])]


@dataclass(frozen=True)
class StabilityTests:
    enabled: bool = True
    cases: list[str] = field(default_factory=lambda: list(STABILITY_CASES))
    crash_threshold: int = 5
    heartbeat_interval_s: float = 0.5
    long_run_cycles: int = 100
    long_run_speed: int = 250
    long_run_max_fail_ratio: float = 0.05
    burst_count: int = 200
    burst_interval_s: float = 0.01
    burst_max_fail_pct: float = 5.0
    idle_wait_s: float = 5.0
    mode_switch_rounds: int = 10
    mode_switch_modes: list[int] = field(default_factory=lambda: [0, 2, 3, 4])
    mode_switch_max_fail_ratio: float = 0.1
    ramp_cycles: int = 5
    ramp: RampProfile = field(default_factory=lambda: RampProfile(300, 100, 0.3))
    ramp_max_fail_ratio: float = 0.2
    jump_pairs: list[tuple[int, int]] = field(default_factory=lambda: [(0, 600), (-200, 200), (0, 800), (-100, 500)])
    jump_repeat: int = 10
    jump_interval_s: float = 0.03
    jump_max_fail_ratio: float = 0.05
    noise_packets: list[bytes] = field(default_factory=_default_noise)
    noise_settle_s: float = 0.1
    noise_interval_s: float = 0.2
    monitor_duration_s: float = 30.0
    monitor_speed: int = 200
    monitor_max_fail_pct: float = 2.0

    def __post_init__(self) -> None:
        unknown = [c for c in self.cases if c not in STABILITY_CASES]
        if unknown:
            raise ConfigError(f"未知稳定性用例 {unknown}，可选: {list(STABILITY_CASES)}")
        if self.crash_threshold < 1:
            raise ConfigError("crash_threshold 至少为 1")


@dataclass(frozen=True)
class R3McbConfig:
    action: Action = "interactive"
    clear_error_on_start: bool = True
    #: 正常结束时，对会驱动电机的动作（interactive/full/stress/ramp）执行安全停机；异常退出时总是停机
    safe_stop_on_exit: bool = True
    start_speed_rpm: int = 200
    low_speed_rpm: int = 100
    mid_speed_rpm: int = 300
    target_accel: int = 500
    ramp: RampProfile = field(default_factory=RampProfile)
    #: 旧脚本各步骤之间的固定等待（0.1~0.3s）统一乘以该系数
    delay_scale: float = 1.0
    reconnect_delay_s: float = 1.0
    basic: BasicTests = field(default_factory=BasicTests)
    stability: StabilityTests = field(default_factory=StabilityTests)

    def __post_init__(self) -> None:
        if self.delay_scale < 0:
            raise ConfigError("r3_mcb.delay_scale 不能为负数")


# ============================================================ 客户端协议
class RegionClientLike(Protocol):
    transport: Any

    def read(self, reg: int, nums: int = 1, *, label: str = "") -> Any: ...

    def write(self, reg: int, fields: Mapping[str, Any], *, nums: int = 1, expect_response: bool = True,
              label: str = "") -> Any: ...


# ============================================================ 测试器
class R3McbTester:
    def __init__(
        self,
        client: RegionClientLike,
        cfg: R3McbConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        input_fn: Callable[[str], str] = builtins.input,
        raw_write: Callable[[bytes], Any] | None = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._input = input_fn
        self._raw_write = raw_write
        self.results: list[dict[str, Any]] = []
        self.crash_detected = False
        self.consecutive_failures = 0
        self.stress_total = 0
        self.stress_failures = 0

    # ------------------------------------------------------------ 基础设施
    def _pause(self, seconds: float) -> None:
        if seconds > 0 and self.cfg.delay_scale > 0:
            self._sleep(seconds * self.cfg.delay_scale)

    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds)

    def _reconnect(self, exc: SerialDisconnectedError) -> None:
        transport = getattr(self.client, "transport", None)
        if transport is None or not hasattr(transport, "reconnect"):
            raise TestAbort(f"MCB 串口断开且无法重连: {exc}") from exc
        log.warning("MCB 串口断开，%.1fs 后尝试重连: %s", self.cfg.reconnect_delay_s, exc)
        try:
            transport.reconnect(delay_s=self.cfg.reconnect_delay_s)
        except HardwareError as exc2:
            raise TestAbort(f"MCB 串口断开且重连失败: {exc2}") from exc2
        log.info("MCB 串口重连成功")

    def _read(self, reg: int, nums: int = 1, label: str = "") -> Any | None:
        """读寄存器；通信/协议失败返回 None（判定由调用方完成）。"""
        try:
            return self.client.read(reg, nums, label=label)
        except SerialDisconnectedError as exc:
            self._reconnect(exc)
        except (HardwareError, ProtocolError) as exc:
            log.warning("[%s] 读寄存器 %d 失败: %s", label or "读", reg, exc)
        return None

    def _write(self, reg: int, fields: Mapping[str, Any], nums: int = 1, label: str = "") -> bool:
        try:
            self.client.write(reg, fields, nums=nums, label=label)
            log.info("[%s] 写入成功", label or f"写寄存器{reg}")
            return True
        except SerialDisconnectedError as exc:
            self._reconnect(exc)
        except (HardwareError, ProtocolError) as exc:
            log.warning("[%s] 写寄存器 %d 失败: %s", label or "写", reg, exc)
        return False

    def _write_expect_reject(self, reg: int, fields: Mapping[str, Any], label: str) -> tuple[bool, str]:
        """期望 MCB 返回异常帧的写入：返回 (是否通过, 说明)。"""
        try:
            self.client.write(reg, fields, label=label)
        except DeviceExceptionResponse:
            return True, "MCB返回异常帧，写入被拒绝"
        except SerialDisconnectedError as exc:
            self._reconnect(exc)
            return False, "通信失败"
        except ProtocolError as exc:
            return False, f"解析失败:{exc}"
        except HardwareError as exc:
            log.warning("[%s] 通信失败: %s", label, exc)
            return False, "通信失败"
        return False, "MCB未拒绝(固件缺陷)"

    def _record(self, name: str, passed: bool, details: str = "") -> bool:
        self.results.append({
            "index": len(self.results) + 1,
            "name": name,
            "verdict": "PASS" if passed else "FAIL",
            "details": details,
            "timestamp": _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3],
        })
        log.info("[TEST RESULT] %s | %s | %s", "PASS" if passed else "FAIL", name, details)
        return passed

    # ------------------------------------------------------------ 业务指令
    def clear_error_code(self) -> bool:
        log.info("--- 清除故障码 ---")
        return self._write(RegV2.RT_SETTING, {"rt_setting": CLR_ERRCODE_BIT}, label="清除故障码")

    def read_motor_status(self) -> bool:
        log.info("--- 读取电机状态 ---")
        data = self._read(RegV2.MCU_ERRCODE, 4, "读取状态")
        if data is None:
            return False
        log.info("--- 当前电机状态 ---")
        log.info("故障码: %s", data.mcu_errcode)
        log.info("转速: %s rpm", data.motor_speed)
        log.info("母线电压: %.1f V", data.bus_voltage * 0.1)
        log.info("母线电流: %.1f A", data.bus_current * 0.1)
        return True

    def read_brake_state(self) -> int | None:
        data = self._read(RegV2.BRAKE_STATE, 1, "读取刹车")
        if data is None:
            return None
        state = int(data.brake_state)
        log.info("电磁刹状态: %d -> %s", state, BRAKE_NAMES.get(state, "未知"))
        return state

    def set_run_mode(self, mode: int) -> bool:
        label = f"设置模式({MODE_NAMES.get(mode, f'未知{mode}')})"
        log.info("--- %s ---", label)
        return self._write(RegV2.RUN_MODE, {"run_mode": mode}, label=label)

    def _speed_block(self, speed: int, current: int, label: str) -> bool:
        return self._write(RegV2.TARGET_SPEED, {
            "target_speed": speed, "target_accel": self.cfg.target_accel, "target_current": current,
        }, nums=3, label=label)

    def set_target_speed(self, speed_rpm: int) -> bool:
        log.info("--- 设置目标速度: %d rpm ---", speed_rpm)
        return self._speed_block(speed_rpm, 0, f"速度{speed_rpm}rpm")

    def set_target_current(self, current_01a: int, speed_rpm: int = 0) -> bool:
        log.info("--- 设置电流: %.1fA ---", current_01a * 0.1)
        return self._speed_block(speed_rpm, current_01a, f"电流{current_01a * 0.1:.1f}A")

    def _write_speed_only(self, speed: int, label: str) -> bool:
        return self._write(RegV2.TARGET_SPEED, {"target_speed": speed}, label=label)

    def start_motor_speed_mode(self, target_speed_rpm: int) -> bool:
        log.info("=" * 50)
        log.info("=== 启动电机（速度模式）目标: %d rpm ===", target_speed_rpm)
        log.info("=" * 50)
        self.clear_error_code()
        self._pause(0.1)
        self.read_motor_status()
        self._pause(0.1)
        self.set_run_mode(ModeV2.IDLE)
        self._pause(0.1)
        self._speed_block(target_speed_rpm, 0, "预设速度")
        self._pause(0.1)
        if not self.set_run_mode(ModeV2.RUNNING):
            log.error("切换骑行模式失败。")
            return False
        self._pause(0.3)
        self.read_motor_status()
        return True

    def stop_motor(self) -> bool:
        log.info("=== 停止电机 ===")
        ok = self._speed_block(0, 0, "速度归零")
        self._pause(0.1)
        ok = self.set_run_mode(ModeV2.IDLE) and ok
        self._pause(0.2)
        self.read_motor_status()
        log.info("=== 电机已停止 ===" if ok else "=== 停止电机指令未全部成功 ===")
        return ok

    def safe_stop(self) -> None:
        """退出时的安全停机：尽力而为，任何异常只记录，不向上抛出。"""
        log.info(">>> 安全停机（速度归零 + 空闲模式）")
        try:
            self.stop_motor()
        except ToolError as exc:
            log.error("安全停机失败（请人工确认电机已停止）: %s", exc)

    def ramp_test(self, max_speed: int, step: int, dwell: float) -> bool:
        if step <= 0:
            raise ValueError(f"步进必须 > 0，实际 {step}")
        log.info("=== 斜坡测试: 0->%d rpm (步进%d, 停留%ss) ===", max_speed, step, dwell)
        self.clear_error_code()
        self._pause(0.1)
        self._speed_block(0, 0, "初始参数")
        self._pause(0.1)
        if not self.set_run_mode(ModeV2.RUNNING):
            log.error("切入骑行失败。")
            return False
        speeds = list(range(0, max_speed + 1, step))
        if speeds[-1] != max_speed:
            speeds.append(max_speed)
        speeds_desc = list(range(max_speed - step, -1, -step))
        if not speeds_desc or speeds_desc[-1] != 0:
            speeds_desc.append(0)
        for s in speeds + speeds_desc:
            log.info(">>> 速度: %d rpm", s)
            self._write_speed_only(s, f"速度{s}")
            self._wait(dwell)
        self.set_run_mode(ModeV2.IDLE)
        self._pause(0.2)
        self.read_motor_status()
        log.info("=== 斜坡测试完成 ===")
        return True

    # ------------------------------------------------------------ 基础功能用例
    def test_clear_error(self) -> bool:
        return self._record("清除故障码", self.clear_error_code(), "写入CLR_ERRCODE")

    def test_read_state(self) -> bool:
        return self._record("读取电机状态", self.read_motor_status(), "寄存器5~8")

    def test_brake_state(self) -> bool:
        state = self.read_brake_state()
        return self._record("读取电磁刹状态", state is not None, f"状态码:{state}")

    def test_mode_switch(self, mode: int, name: str) -> bool:
        log.info("=== 测试: 切换%s ===", name)
        self.set_run_mode(mode)
        self._pause(0.1)
        data = self._read(RegV2.RUN_MODE, 1, f"验证{name}")
        if data is not None and int(data.run_mode) == mode:
            return self._record(f"模式切换:{name}", True, f"回读={mode}")
        actual = data.run_mode if data is not None else "N/A"
        return self._record(f"模式切换:{name}", False, f"期望{mode},实际{actual}")

    def test_all_modes(self) -> bool:
        all_ok = True
        for step in self.cfg.basic.mode_steps:
            all_ok = self.test_mode_switch(step.mode, step.name) and all_ok
            self._pause(0.15)
        return all_ok

    def test_speed_control(self, speed: int) -> bool:
        b = self.cfg.basic
        log.info("=== 测试: 速度控制 %drpm ===", speed)
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        self.set_target_speed(speed)
        self._pause(0.3)
        name = f"速度控制{speed}rpm"
        data = self._read(RegV2.TARGET_SPEED, 2, f"验证速度{speed}")
        if data is None:
            return self._record(name, False, "回读失败")
        actual = int(data.target_speed)
        if abs(actual - speed) <= max(b.speed_tol_abs, speed * b.speed_tol_ratio):
            return self._record(name, True, f"回读={actual}")
        return self._record(name, False, f"期望{speed},实际{actual}")

    def test_current_control(self, current_01a: int) -> bool:
        log.info("=== 测试: 电流控制 %.1fA ===", current_01a * 0.1)
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        self.set_target_current(current_01a)
        self._pause(0.2)
        name = f"电流控制{current_01a * 0.1:.1f}A"
        data = self._read(RegV2.TARGET_CUR, 1, f"验证电流{current_01a * 0.1:.1f}A")
        if data is None:
            return self._record(name, False, "回读失败")
        actual = int(data.target_current)
        if abs(actual - current_01a) <= self.cfg.basic.current_tol:
            return self._record(name, True, f"回读={actual * 0.1:.1f}A")
        return self._record(name, False, f"期望{current_01a},实际{actual}")

    def test_ramp_profile(self) -> bool:
        r = self.cfg.basic.ramp
        ok = self.ramp_test(r.max_speed, r.step, r.dwell_s)
        return self._record("斜坡测试", ok, f"0->{r.max_speed}->0")

    def test_param_boundary(self) -> bool:
        b = self.cfg.basic
        all_ok = True
        name = f"边界:负速度{b.boundary_speed}"
        if self.set_target_speed(b.boundary_speed):
            self._record(name, True, "设置成功")
        else:
            all_ok = self._record(name, False, "拒绝或失败")
        self._pause(0.1)
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        if self.set_target_current(0):
            self._record("边界:零电流", True, "设置成功")
        else:
            all_ok = self._record("边界:零电流", False, "设置失败")
        self._pause(0.1)
        self.set_run_mode(ModeV2.IDLE)
        return all_ok

    def test_read_invalid_register(self) -> bool:
        reg = self.cfg.basic.invalid_read_reg
        log.info("=== 测试: 读取非法寄存器 (0x%02X) ===", reg)
        passed = self._read(reg, 1, "非法读") is None
        return self._record("读取非法寄存器", passed, "预期异常" if passed else "意外返回数据")

    def test_write_invalid_register(self) -> bool:
        b = self.cfg.basic
        log.info("=== 测试: 写入非法寄存器 (0x%02X) ===", b.invalid_write_reg)
        passed, detail = self._write_expect_reject(b.invalid_write_reg, {"id_num": b.invalid_write_value}, "非法写")
        return self._record("写入非法寄存器", passed, detail)

    def test_write_readonly_reg(self) -> bool:
        log.info("=== 测试: 写入只读寄存器 (motor_speed) ===")
        passed, detail = self._write_expect_reject(
            RegV2.MOTOR_SPEED, {"motor_speed": self.cfg.basic.readonly_write_value}, "写只读")
        return self._record("写入只读寄存器", passed, detail)

    def test_invalid_mode_number(self) -> bool:
        mode = self.cfg.basic.invalid_mode
        log.info("=== 测试: 无效运行模式 (%d) ===", mode)
        passed, detail = self._write_expect_reject(RegV2.RUN_MODE, {"run_mode": mode}, f"设置模式(未知{mode})")
        detail = detail.replace("写入被拒绝", "模式被拒绝")
        return self._record("无效运行模式", passed, detail)

    def test_flag_motor_stop(self) -> bool:
        b = self.cfg.basic
        log.info("=== 测试: 电机停止标志位 ===")
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        self.set_target_speed(b.stop_flag_speed)
        self._pause(0.3)
        before = self._read(RegV2.MOTOR_SPEED, 1, "停止前速度")
        if before is None:
            self.stop_motor()
            return self._record("电机停止标志位", False, "无法读取速度")
        speed_before = int(before.motor_speed)
        log.info("停止前速度: %d rpm", speed_before)
        self._write(RegV2.RT_SETTING, {"rt_setting": MOTOR_STOP_BIT}, label="MOTOR_STOP")
        self._pause(0.3)
        after = self._read(RegV2.MOTOR_SPEED, 1, "停止后速度")
        if after is None:
            self.stop_motor()
            return self._record("电机停止标志位", False, "无法读取速度")
        speed_after = int(after.motor_speed)
        log.info("停止后速度: %d rpm", speed_after)
        reduced = abs(speed_after) < max(b.stop_flag_abs, abs(speed_before) * b.stop_flag_ratio)
        self.stop_motor()
        return self._record("电机停止标志位", reduced, f"速度从{speed_before}->{speed_after}")

    def test_rapid_commands(self) -> bool:
        b = self.cfg.basic
        log.info("=== 测试: 指令洪泛稳定性 ===")
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        all_ok = True
        for i in range(b.rapid_count):
            all_ok = self.set_target_speed(50 + (i * 30) % 500) and all_ok
            self._wait(b.rapid_interval_s)
        final_ok = self.read_motor_status()
        self.set_run_mode(ModeV2.IDLE)
        all_ok = all_ok and final_ok
        return self._record("指令洪泛稳定性", all_ok, f"连续{b.rapid_count}次快速写" if all_ok else "通信失败")

    def test_read_all_status_registers(self) -> bool:
        b = self.cfg.basic
        lim = b.status_limits
        log.info("=== 测试: 全状态寄存器扫描 ===")
        data = self._read(RegV2.MCU_ERRCODE, b.status_scan_nums, "全状态")
        if data is None:
            return self._record("全状态寄存器扫描", False, "读取失败")
        checks = {
            "bus_voltage": lim.bus_voltage_v[0] <= data.bus_voltage * 0.1 <= lim.bus_voltage_v[1],
            "bus_current": lim.bus_current_a[0] <= data.bus_current * 0.1 <= lim.bus_current_a[1],
            "target_speed": lim.target_speed[0] <= data.target_speed <= lim.target_speed[1],
            "target_accel": lim.target_accel[0] <= data.target_accel <= lim.target_accel[1],
            "motor_speed": lim.motor_speed[0] <= data.motor_speed <= lim.motor_speed[1],
        }
        bad = [k for k, ok in checks.items() if not ok]
        return self._record("全状态寄存器扫描", not bad, "字段合理" if not bad else f"字段异常:{bad}")

    def test_response_time(self) -> bool:
        log.info("=== 测试: 通信响应时间 ===")
        start = self._clock()
        data = self._read(RegV2.MCU_ERRCODE, 1, "计时读")
        elapsed_ms = (self._clock() - start) * 1000
        if data is None:
            return self._record("通信响应时间", False, "读失败")
        passed = elapsed_ms < self.cfg.basic.response_time_max_ms
        return self._record("通信响应时间", passed, f"{elapsed_ms:.1f} ms" + ("" if passed else "(超时)"))

    # ------------------------------------------------------------ 稳定性辅助
    def _health_check(self) -> bool:
        """读故障码寄存器；连续失败达到阈值时标记疑似死机。"""
        if self._read(RegV2.MCU_ERRCODE, 1, "健康检查") is None:
            self.consecutive_failures += 1
            log.warning("健康检查失败，连续失败次数: %d", self.consecutive_failures)
            if self.consecutive_failures >= self.cfg.stability.crash_threshold:
                if not self.crash_detected:
                    log.critical("!!! 疑似 MCB 死机/无响应（连续%d次健康检查失败）!!!", self.consecutive_failures)
                self.crash_detected = True
            return False
        self.consecutive_failures = 0
        return True

    def _reset_stability_stats(self) -> None:
        self.crash_detected = False
        self.consecutive_failures = 0
        self.stress_total = 0
        self.stress_failures = 0

    def _stress_write(self, label: str, reg: int, fields: Mapping[str, Any]) -> bool:
        self.stress_total += 1
        ok = self._write(reg, fields, label=label)
        if not ok:
            self.stress_failures += 1
        return ok

    def _stress_read(self, label: str, reg: int) -> Any | None:
        self.stress_total += 1
        data = self._read(reg, 1, label)
        if data is None:
            self.stress_failures += 1
        return data

    def _prepare_stability(self, *, clear: bool = True) -> None:
        self.stop_motor()
        self._pause(0.3)
        if clear:
            self.clear_error_code()
            self._pause(0.1)

    def _finish_stability(self) -> None:
        self.stop_motor()
        self._pause(0.2)

    @staticmethod
    def _crash_suffix(crashed: bool, text: str = " | 死机") -> str:
        return text if crashed else ""

    # ------------------------------------------------------------ 稳定性用例
    def stability_long_run(self) -> bool:
        st = self.cfg.stability
        cycles = st.long_run_cycles
        log.info("=== 稳定性测试: 长时间运行 %d 个周期 ===", cycles)
        self._prepare_stability()
        self._reset_stability_stats()
        self._speed_block(st.long_run_speed, 0, "长跑预设")
        self._pause(0.1)
        if not self.set_run_mode(ModeV2.RUNNING):
            self._record("长时间运行稳定性", False, "无法进入骑行模式")
            self.stop_motor()
            return False
        failures = 0
        for cycle in range(1, cycles + 1):
            if self.crash_detected:
                log.critical("第 %d/%d 周期检测到死机，终止测试。", cycle, cycles)
                break
            if not self._health_check():
                failures += 1
            if cycle % 20 == 0:
                log.info("长时间运行进度: %d/%d 周期, 当前累计失败: %d", cycle, cycles, failures)
            self._wait(st.heartbeat_interval_s)
        self._finish_stability()
        crashed = self.crash_detected
        passed = (not crashed) and failures <= cycles * st.long_run_max_fail_ratio
        detail = f"{cycles}周期, 失败{failures}次" + self._crash_suffix(crashed, " | 检测到死机")
        return self._record("长时间运行稳定性", passed, detail)

    def stability_burst(self) -> bool:
        st = self.cfg.stability
        log.info("=== 稳定性测试: 指令洪泛压力 %d 次 ===", st.burst_count)
        self._prepare_stability()
        self._reset_stability_stats()
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        for i in range(st.burst_count):
            if self.crash_detected:
                break
            if i % 3 == 0:
                speed = (i % 600) + 50
                self._stress_write(f"洪泛速度{speed}", RegV2.TARGET_SPEED, {"target_speed": speed})
            elif i % 3 == 1:
                self._stress_read("洪泛读状态", RegV2.MCU_ERRCODE)
            else:
                self._health_check()
            self._wait(st.burst_interval_s)
        self._finish_stability()
        crashed = self.crash_detected
        rate = self.stress_failures / max(1, self.stress_total) * 100
        passed = (not crashed) and rate <= st.burst_max_fail_pct
        detail = f"{self.stress_total}次, 失败{self.stress_failures}({rate:.1f}%)" + self._crash_suffix(crashed)
        return self._record("指令洪泛压力", passed, detail)

    def stability_idle_recovery(self) -> bool:
        st = self.cfg.stability
        log.info("=== 稳定性测试: 空闲恢复 (等待 %ss) ===", st.idle_wait_s)
        self._prepare_stability()
        log.info("进入空闲状态，等待 %s 秒...", st.idle_wait_s)
        self._wait(st.idle_wait_s)
        self._reset_stability_stats()
        ok1 = self._health_check()
        ok2 = self._health_check()
        ok3 = self._stress_read("空闲后读全状态", RegV2.MCU_ERRCODE) is not None
        passed = ok1 and ok2 and ok3
        detail = "正常" if passed else f"健康检查失败(ok1={ok1},ok2={ok2},ok3={ok3})"
        return self._record("长时间空闲后恢复", passed, detail)

    def stability_mode_switch(self) -> bool:
        st = self.cfg.stability
        rounds = st.mode_switch_rounds
        log.info("=== 稳定性测试: 模式反复切换 %d 轮 ===", rounds)
        self._prepare_stability()
        self._reset_stability_stats()
        failures = 0
        for rnd in range(rounds):
            if self.crash_detected:
                break
            for mode in st.mode_switch_modes:
                if not self.set_run_mode(mode):
                    failures += 1
                self._pause(0.08)
                if not self._health_check():
                    failures += 1
                self._pause(0.05)
            if (rnd + 1) % 5 == 0:
                log.info("模式切换进度: %d/%d 轮, 失败: %d", rnd + 1, rounds, failures)
        self._finish_stability()
        crashed = self.crash_detected
        # 与旧脚本一致：阈值按轮数（而非总切换次数）计算
        passed = (not crashed) and failures <= rounds * st.mode_switch_max_fail_ratio
        total = rounds * len(st.mode_switch_modes)
        detail = f"{rounds}轮共{total}次切换, 失败{failures}次" + self._crash_suffix(crashed)
        return self._record("模式反复切换压力", passed, detail)

    def stability_ramp_repeat(self) -> bool:
        st = self.cfg.stability
        cycles = st.ramp_cycles
        log.info("=== 稳定性测试: 斜坡往复 %d 次 ===", cycles)
        self._prepare_stability()
        self._reset_stability_stats()
        failures = 0
        for cyc in range(1, cycles + 1):
            if self.crash_detected:
                break
            log.info("斜坡往复 第 %d/%d 次", cyc, cycles)
            if not self.ramp_test(st.ramp.max_speed, st.ramp.step, st.ramp.dwell_s):
                failures += 1
            self._pause(0.2)
            if not self._health_check():
                failures += 1
            self._pause(0.1)
        self._finish_stability()
        crashed = self.crash_detected
        passed = (not crashed) and failures <= cycles * st.ramp_max_fail_ratio
        detail = f"{cycles}次往复, 失败{failures}次" + self._crash_suffix(crashed)
        return self._record("斜坡往复循环", passed, detail)

    def stability_speed_jump(self) -> bool:
        st = self.cfg.stability
        log.info("=== 稳定性测试: 边界速度跳变 ===")
        self._prepare_stability()
        self._reset_stability_stats()
        if not self.set_run_mode(ModeV2.RUNNING):
            return self._record("边界速度跳变", False, "无法进入骑行")
        self._pause(0.1)
        failures = 0
        for low, high in st.jump_pairs:
            if self.crash_detected:
                break
            for _ in range(st.jump_repeat):
                if not self._stress_write(f"跳变{high}", RegV2.TARGET_SPEED, {"target_speed": high}):
                    failures += 1
                self._wait(st.jump_interval_s)
                if not self._stress_write(f"跳变{low}", RegV2.TARGET_SPEED, {"target_speed": low}):
                    failures += 1
                self._wait(st.jump_interval_s)
            self._health_check()
        self._finish_stability()
        crashed = self.crash_detected
        total_ops = len(st.jump_pairs) * st.jump_repeat * 2
        passed = (not crashed) and failures <= total_ops * st.jump_max_fail_ratio
        detail = f"{total_ops}次跳变, 失败{failures}次" + self._crash_suffix(crashed)
        return self._record("边界速度跳变", passed, detail)

    def _send_raw(self, data: bytes) -> None:
        if self._raw_write is not None:
            self._raw_write(data)
            return
        self.client.transport.write(data)

    def stability_noise(self) -> bool:
        st = self.cfg.stability
        log.info("=== 稳定性测试: 通信噪声容限 ===")
        self._prepare_stability()
        transport = getattr(self.client, "transport", None)
        if self._raw_write is None and (transport is None or not transport.is_open):
            return self._record("通信噪声容限", False, "串口未打开")
        all_recovered = True
        for idx, garbage in enumerate(st.noise_packets):
            try:
                log.info("发送噪声包 #%d: %s", idx, garbage.hex(" ").upper())
                self._send_raw(garbage)
                self._wait(st.noise_settle_s)
            except SerialDisconnectedError as exc:
                self._reconnect(exc)
            except HardwareError as exc:
                log.error("发送噪声包失败: %s", exc)
            if not self._health_check():
                log.warning("噪声包 #%d 后通信异常", idx)
                all_recovered = False
            self._wait(st.noise_interval_s)
        final_ok = self.read_motor_status()
        passed = all_recovered and final_ok
        return self._record("通信噪声容限", passed, "MCB正常恢复" if passed else "MCB通信异常/死机")

    def stability_health_monitor(self) -> bool:
        st = self.cfg.stability
        log.info("=== 稳定性测试: 持续心跳监控 %ss ===", st.monitor_duration_s)
        self.stop_motor()
        self._pause(0.3)
        self._reset_stability_stats()
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        self._stress_write(f"心跳速度{st.monitor_speed}", RegV2.TARGET_SPEED, {"target_speed": st.monitor_speed})
        start = self._clock()
        sent = failed = 0
        while self._clock() - start < st.monitor_duration_s:
            if self.crash_detected:
                break
            sent += 1
            if not self._health_check():
                failed += 1
            self._wait(st.heartbeat_interval_s)
        self._finish_stability()
        crashed = self.crash_detected
        rate = failed / max(1, sent) * 100
        passed = (not crashed) and rate <= st.monitor_max_fail_pct
        detail = f"{st.monitor_duration_s:g}s内{sent}次心跳, 失败{failed}({rate:.1f}%)" + self._crash_suffix(crashed)
        return self._record("持续心跳监控", passed, detail)

    # ------------------------------------------------------------ 流程
    def run_basic(self) -> None:
        b = self.cfg.basic
        self.test_clear_error()
        self._pause(0.1)
        self.test_read_state()
        self.test_brake_state()
        self._pause(0.2)
        self.test_all_modes()
        self._pause(0.2)
        for sp in b.speed_points:
            self.test_speed_control(sp)
            self._pause(0.2)
        for cur in b.current_points:
            self.test_current_control(cur)
            self._pause(0.2)
        self.test_ramp_profile()
        self._pause(0.2)
        self.test_param_boundary()
        self._pause(0.2)
        self.test_read_invalid_register()
        self.test_write_invalid_register()
        self.test_write_readonly_reg()
        self.test_invalid_mode_number()
        self.test_flag_motor_stop()
        self.test_rapid_commands()
        self.test_read_all_status_registers()
        self.test_response_time()

    def run_stability(self) -> None:
        st = self.cfg.stability
        log.info(">>> 阶段二: 深度稳定性压力测试 <<<")
        log.info("配置: 长跑%d周期 | 洪泛%d次 | 空闲%ss | 斜坡%d轮 | 模式%d轮 | 用例 %s",
                 st.long_run_cycles, st.burst_count, st.idle_wait_s, st.ramp_cycles, st.mode_switch_rounds, st.cases)
        cases: dict[str, Callable[[], bool]] = {
            "long_run": self.stability_long_run,
            "burst": self.stability_burst,
            "idle_recovery": self.stability_idle_recovery,
            "mode_switch": self.stability_mode_switch,
            "ramp_repeat": self.stability_ramp_repeat,
            "speed_jump": self.stability_speed_jump,
            "noise": self.stability_noise,
            "health_monitor": self.stability_health_monitor,
        }
        for name in st.cases:
            cases[name]()
            self._pause(0.3)

    def run_full(self) -> bool:
        log.info("=" * 60)
        log.info("=== 全自动白盒稳定性测试 ===")
        log.info("=" * 60)
        self.results = []
        log.info(">>> 阶段一: 基础连接与功能快速检查 <<<")
        if not self.read_motor_status():
            log.critical("基础连接失败，终止测试。")
            self._record("基础连接验证", False, "无法读取状态")
            return False
        self._record("基础连接验证", True, "OK")
        self._pause(0.2)
        if self.cfg.basic.enabled:
            self.run_basic()
        if self.cfg.stability.enabled:
            self.run_stability()
        log.info(">>> 最终安全停止 <<<")
        self.stop_motor()
        self._pause(0.3)
        self._record("最终安全停止", self.read_motor_status(), "空闲状态")
        return self.report()

    def run_stress(self) -> bool:
        log.info("=== 仅执行稳定性压力测试 ===")
        self.results = []
        self.run_stability()
        self.stop_motor()
        return self.report()

    def report(self) -> bool:
        total = len(self.results)
        passed = sum(1 for r in self.results if r["verdict"] == "PASS")
        failed = total - passed
        log.info("=" * 60)
        log.info("=== 稳定性测试报告 ===")
        log.info("用例总数: %d | 通过: %d | 失败: %d | 通过率: %.1f%%",
                 total, passed, failed, (passed / total * 100) if total else 0.0)
        for r in self.results:
            log.info("  [%s] #%02d %s - %s", r["verdict"], r["index"], r["name"], r["details"])
        if failed:
            log.warning("存在 %d 个失败用例", failed)
            for r in self.results:
                if r["verdict"] == "FAIL":
                    log.info("  * %s | %s | %s", r["name"], r["details"], r["timestamp"])
        else:
            log.info("所有用例通过，MCB稳定性良好。")
        log.info("=" * 60)
        return total > 0 and failed == 0

    # ------------------------------------------------------------ 交互
    def interactive(self) -> None:
        while True:
            log.info(MENU)
            try:
                ch = self._input("请选择操作: ").strip().lower()
                if ch == "q":
                    log.info("退出程序。")
                    return
                self._dispatch(ch)
            except EOFError:
                log.info("输入结束，退出交互模式。")
                return
            except ValueError as exc:
                log.error("输入错误: %s", exc)
            except TestAbort:
                raise
            except ToolError as exc:
                log.error("交互操作失败: %s", exc)

    def _dispatch(self, ch: str) -> None:
        cfg = self.cfg
        if ch == "0":
            self.read_motor_status()
        elif ch == "1":
            self.start_motor_speed_mode(cfg.low_speed_rpm)
        elif ch == "2":
            self.start_motor_speed_mode(cfg.mid_speed_rpm)
        elif ch == "3":
            self.start_motor_speed_mode(int(self._input("目标速度 (rpm): ")))
        elif ch == "4":
            self.stop_motor()
        elif ch == "5":
            r = cfg.ramp
            mx = self._input(f"最大速度 (默认{r.max_speed}): ").strip()
            st = self._input(f"步进 (默认{r.step}): ").strip()
            dw = self._input(f"停留秒数 (默认{r.dwell_s}): ").strip()
            self.ramp_test(int(mx) if mx else r.max_speed, int(st) if st else r.step, float(dw) if dw else r.dwell_s)
        elif ch == "6":
            cur_a = float(self._input("目标电流 (A): "))
            self.set_target_current(int(cur_a * 10))
        elif ch == "7":
            self.clear_error_code()
        elif ch == "8":
            log.info("运行模式: 0-空闲 2-骑行 4-推行 10-延迟关刹 11-立即关刹 12-解锁")
            self.set_run_mode(int(self._input("模式编号: ")))
        elif ch == "9":
            self.read_brake_state()
        elif ch == "a":
            self.run_full()
        else:
            log.warning("未知选项。")

    # ------------------------------------------------------------ 入口
    def execute(self) -> int:
        cfg = self.cfg
        action = cfg.action
        log.info("R3 MCB 白盒测试 动作=%s", action)
        ok = False
        abnormal = True
        try:
            if cfg.clear_error_on_start:
                self.clear_error_code()
                self._pause(0.1)
            if action == "full":
                ok = self.run_full()
            elif action == "stress":
                ok = self.run_stress()
            elif action == "status":
                ok = self.read_motor_status()
            elif action == "stop":
                ok = self.stop_motor()
            elif action == "start":
                ok = self.start_motor_speed_mode(cfg.start_speed_rpm)
            elif action == "ramp":
                ok = self.ramp_test(cfg.ramp.max_speed, cfg.ramp.step, cfg.ramp.dwell_s)
            else:
                self.interactive()
                ok = True
            abnormal = False
        except TestAbort as exc:
            log.error("测试熔断: %s", exc.reason)
            self._record("测试熔断", False, exc.reason)
        finally:
            if abnormal or (cfg.safe_stop_on_exit and action in _MOTION_ACTIONS):
                self.safe_stop()
        return 0 if ok else 1


# ============================================================ 应用入口
CodecFactory = Callable[[Path], Any]


def _default_codec(dll_path: Path) -> RegionCodecV2:
    return RegionCodecV2(PpxDll(dll_path))


def write_reports(ctx: RunContext, tester: R3McbTester, *, code: int, action: str) -> None:
    passed = sum(1 for r in tester.results if r["verdict"] == "PASS")
    summary = {
        "动作": action,
        "用例总数": len(tester.results),
        "通过": passed,
        "失败": len(tester.results) - passed,
        "退出码": code,
    }
    ctx.write_json("summary.json", summary)
    if not tester.results or ctx.run_dir is None:
        return
    columns = ["index", "name", "verdict", "details", "timestamp"]
    write_csv(ctx.run_dir / "r3_mcb_results.csv", tester.results, columns)
    write_html(ctx.run_dir / "r3_mcb_report.html", title="R3 MCB 白盒测试报告", summary=summary,
               rows=tester.results, columns=columns)


def run(
    settings: AppSettings,
    ctx: RunContext,
    *,
    codec_factory: CodecFactory | None = None,
    serial_factory: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    input_fn: Callable[[str], str] = builtins.input,
) -> int:
    proto = settings.section("protocol", R3ProtocolConfig)
    cfg = settings.section("r3_mcb", R3McbConfig)
    codec = (codec_factory or _default_codec)(proto.dll_path)
    transport = open_serial(settings, "mcb", factory=serial_factory)
    tester: R3McbTester | None = None
    code = 1
    try:
        client = RegionClient(codec, transport, dev_id=proto.dev_id, rx_timeout_s=proto.rx_timeout_s,
                              retries=proto.retries, retry_delay_s=proto.retry_delay_s, name="mcb")
        tester = R3McbTester(client, cfg, sleep=sleep, clock=clock, input_fn=input_fn)
        code = tester.execute()
        return code
    finally:
        transport.close()
        if tester is not None:
            write_reports(ctx, tester, code=code, action=cfg.action)
        log.info("R3 MCB 白盒测试结束，退出码 %d", code)

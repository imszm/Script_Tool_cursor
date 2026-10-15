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

from ppx_testkit.apps.whitebox.r3_basic import R3BasicCases
from ppx_testkit.apps.whitebox.r3_menu import R3MenuCases
from ppx_testkit.apps.whitebox.r3_names import BRAKE_NAMES, MODE_NAMES, STABILITY_CASES, log
from ppx_testkit.apps.whitebox.r3_stability import R3StabilityCases
from ppx_testkit.core.factory import region_client_from_settings
from ppx_testkit.core.protocol.dll_loader import PpxDll
from ppx_testkit.core.protocol.ppx_types import DevId, ModeV2, RegV2
from ppx_testkit.core.protocol.region_client import RegionCodecV2
from ppx_testkit.core.protocol.region_v2_motor import (
    call_reconnect,
    has_reconnect,
    read_regs,
    write_clear_error,
    write_motion,
    write_run_mode,
    write_speed_only,
)
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
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

Action = Literal["interactive", "full", "stress", "status", "stop", "start", "ramp"]
_MOTION_ACTIONS = {"interactive", "full", "stress", "ramp"}

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
class R3McbTester(R3BasicCases, R3StabilityCases, R3MenuCases):
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
        if not has_reconnect(self.client):
            raise TestAbort(f"MCB 串口断开且无法重连: {exc}") from exc
        log.warning("MCB 串口断开，%.1fs 后尝试重连: %s", self.cfg.reconnect_delay_s, exc)
        try:
            call_reconnect(self.client, self.cfg.reconnect_delay_s)
        except HardwareError as exc2:
            raise TestAbort(f"MCB 串口断开且重连失败: {exc2}") from exc2
        log.info("MCB 串口重连成功")

    def _fetch(self, reg: int, label: str, fn: Callable[[], Any]) -> Any | None:
        """执行读操作；通信/协议失败返回 None。"""
        try:
            return fn()
        except SerialDisconnectedError as exc:
            self._reconnect(exc)
        except (HardwareError, ProtocolError) as exc:
            log.warning("[%s] 读寄存器 %d 失败: %s", label or "读", reg, exc)
        return None

    def _read(self, reg: int, nums: int = 1, label: str = "") -> Any | None:
        """读寄存器；通信/协议失败返回 None（判定由调用方完成）。"""
        return self._fetch(reg, label, lambda: read_regs(self.client, reg, nums, label=label))

    def _commit(self, reg: int, label: str, fn: Callable[[], Any]) -> bool:
        """执行写操作并保留原有成功/失败日志。"""
        try:
            fn()
            log.info("[%s] 写入成功", label or f"写寄存器{reg}")
            return True
        except SerialDisconnectedError as exc:
            self._reconnect(exc)
        except (HardwareError, ProtocolError) as exc:
            log.warning("[%s] 写寄存器 %d 失败: %s", label or "写", reg, exc)
        return False

    def _write(self, reg: int, fields: Mapping[str, Any], nums: int = 1, label: str = "") -> bool:
        return self._commit(reg, label, lambda: self.client.write(reg, fields, nums=nums, label=label))

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
        return self._commit(
            RegV2.RT_SETTING,
            "清除故障码",
            lambda: write_clear_error(self.client, label="清除故障码"),
        )

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
        return self._commit(RegV2.RUN_MODE, label, lambda: write_run_mode(self.client, mode, label=label))

    def _speed_block(self, speed: int, current: int, label: str) -> bool:
        return self._commit(
            RegV2.TARGET_SPEED,
            label,
            lambda: write_motion(self.client, speed, self.cfg.target_accel, current, label=label),
        )

    def set_target_speed(self, speed_rpm: int) -> bool:
        log.info("--- 设置目标速度: %d rpm ---", speed_rpm)
        return self._speed_block(speed_rpm, 0, f"速度{speed_rpm}rpm")

    def set_target_current(self, current_01a: int, speed_rpm: int = 0) -> bool:
        log.info("--- 设置电流: %.1fA ---", current_01a * 0.1)
        return self._speed_block(speed_rpm, current_01a, f"电流{current_01a * 0.1:.1f}A")

    def _write_speed_only(self, speed: int, label: str) -> bool:
        return self._commit(RegV2.TARGET_SPEED, label, lambda: write_speed_only(self.client, speed, label=label))

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
























    @staticmethod












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
    tester: R3McbTester | None = None
    code = 1
    try:
        with region_client_from_settings(
            settings,
            serial_name="mcb",
            dll=proto.dll_path,
            version="v2",
            dev_id=proto.dev_id,
            rx_timeout_s=proto.rx_timeout_s,
            retries=proto.retries,
            retry_delay_s=proto.retry_delay_s,
            codec=codec,
            serial_factory=serial_factory,
        ) as client:
            tester = R3McbTester(client, cfg, sleep=sleep, clock=clock, input_fn=input_fn)
            code = tester.execute()
            return code
    finally:
        if tester is not None:
            write_reports(ctx, tester, code=code, action=cfg.action)
        log.info("R3 MCB 白盒测试结束，退出码 %d", code)

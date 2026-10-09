"""L5 MCB 白盒测试（迁移自 ``mcb_V1.4.6（白盒测试正式版）.py``）。

用例清单（与旧脚本一致）::

    [Case 1] 通信链路测试          读取 HW 版本 > 0                         （关键）
    [Case 2] 运行环境安全检查      母线电压 >= 下限；错误码为 0，否则尝试清除  （关键）
    [Case 3] 状态机切换            影子 RUN_MODE=TEST、DAT_SETTING=0x20 后回读  （关键）
    [Case 4] 参数寄存器读写        暂停心跳，写 GEAR 后回读
    [Case 5] IO 控制               暂停心跳，写 RT_SETTING 灯位后回读，再清零
    [Case 6] 动力回路响应          软启动加速度 + 目标转速，|实际转速| 进入容差即通过

关键用例失败时跳过后续用例（旧脚本直接 teardown 退出）。
测试期间 :class:`ShadowHeartbeat` 每 ``heartbeat.period_s`` 秒刷新 RUN_MODE(非零)、
RT_SETTING、TARGET_SPEED、DAT_SETTING(非零)。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ppx_testkit.apps.whitebox._mcb_common import (
    FAIL,
    PASS,
    SKIP,
    Connector,
    McbLinkConfig,
    RegionLike,
    count_verdicts,
    mcb_shadow_regs,
    open_region_client,
    write_raw,
    write_reports,
)
from ppx_testkit.core.protocol.heartbeat import ShadowHeartbeat
from ppx_testkit.core.protocol.ppx_types import REG_FIELD_V1, ModeV1, RegV1, RtSetting
from ppx_testkit.exceptions import ConfigError, HardwareError, ProtocolError, SerialDisconnectedError
from ppx_testkit.logger import RunContext, get_logger
from ppx_testkit.settings import AppSettings

log = get_logger(__name__)

REPORT_STEM = "mcb_whitebox"
REPORT_COLUMNS = ["case_id", "name", "verdict", "detail", "measured", "elapsed_s", "timestamp"]

RawWriter = Callable[[int, Mapping[str, Any], int], None]

_DURATION_FIELDS = (
    "setup_settle_s", "teardown_settle_s", "hb_pause_settle_s", "ping_retry_delay_s", "clear_err_hold_s",
    "clear_err_settle_s", "mode_settle_s", "gear_settle_s", "light_settle_s", "acceleration_settle_s",
    "speed_poll_interval_s", "speed_err_clear_hold_s",
)


# ============================================================ 配置
@dataclass(frozen=True)
class HeartbeatConfig:
    period_s: float = 0.15
    max_consecutive_failures: int = 20
    stop_timeout_s: float = 2.0

    def __post_init__(self) -> None:
        if self.period_s <= 0:
            raise ConfigError("heartbeat.period_s 必须 > 0")
        if self.max_consecutive_failures < 1:
            raise ConfigError("heartbeat.max_consecutive_failures 至少为 1")
        if self.stop_timeout_s <= 0:
            raise ConfigError("heartbeat.stop_timeout_s 必须 > 0")


@dataclass(frozen=True)
class McbWhiteboxConfig:
    stop_on_critical_fail: bool = True
    setup_settle_s: float = 0.5
    teardown_settle_s: float = 0.5
    hb_pause_settle_s: float = 0.2
    # Case 1
    ping_attempts: int = 5
    ping_retry_delay_s: float = 0.1
    # Case 2
    voltage_scale: float = 0.1
    min_bus_voltage_v: float = 30.0
    clear_err_value: int = int(RtSetting.CLR_ERRCODE)
    clear_err_hold_s: float = 0.5
    clear_err_settle_s: float = 0.2
    # Case 3
    test_run_mode: int = int(ModeV1.TEST)
    test_dat_setting: int = 0x20
    mode_settle_s: float = 0.5
    # Case 4
    gear_value: int = 2
    gear_settle_s: float = 0.2
    # Case 5
    light_mask: int = int(RtSetting.RIGHT_LED_ON | RtSetting.LEFT_LED_ON)
    light_settle_s: float = 0.3
    # Case 6
    acceleration: int = 100
    acceleration_nums: int = 2
    acceleration_settle_s: float = 0.2
    target_rpm: int = 300
    rpm_tolerance: int = 50
    speed_poll_count: int = 15
    speed_poll_interval_s: float = 0.2
    speed_err_clear_hold_s: float = 0.2

    def __post_init__(self) -> None:
        for name in _DURATION_FIELDS:
            if getattr(self, name) < 0:
                raise ConfigError(f"mcb_whitebox.{name} 不能为负数")
        if self.ping_attempts < 1:
            raise ConfigError("mcb_whitebox.ping_attempts 至少为 1")
        if self.speed_poll_count < 1:
            raise ConfigError("mcb_whitebox.speed_poll_count 至少为 1")
        if self.acceleration_nums < 1:
            raise ConfigError("mcb_whitebox.acceleration_nums 至少为 1")
        if self.rpm_tolerance <= 0:
            raise ConfigError("mcb_whitebox.rpm_tolerance 必须 > 0")
        if self.voltage_scale <= 0:
            raise ConfigError("mcb_whitebox.voltage_scale 必须 > 0")
        if self.light_mask <= 0:
            raise ConfigError("mcb_whitebox.light_mask 必须 > 0")


# ============================================================ 用例
class HeartbeatLike(Protocol):
    failed_event: threading.Event

    def set(self, reg: int, value: int) -> None: ...

    def paused(self, settle_s: float = 0.2) -> contextlib.AbstractContextManager[None]: ...


@dataclass
class CaseOutcome:
    verdict: str
    detail: str
    measured: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseSpec:
    case_id: int
    name: str
    method: str
    critical: bool


CASES: tuple[CaseSpec, ...] = (
    CaseSpec(1, "通信链路测试", "case_link", True),
    CaseSpec(2, "运行环境安全检查", "case_environment", True),
    CaseSpec(3, "状态机切换(IDLE->TEST)", "case_mode_switch", True),
    CaseSpec(4, "参数寄存器读写(Gear)", "case_gear_rw", False),
    CaseSpec(5, "IO 控制(灯光)", "case_light_io", False),
    CaseSpec(6, "动力回路响应(软启动闭环)", "case_power_loop", False),
)


class McbWhiteboxTester:
    def __init__(
        self,
        client: RegionLike,
        heartbeat: HeartbeatLike,
        cfg: McbWhiteboxConfig,
        *,
        raw_write: RawWriter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.hb = heartbeat
        self.cfg = cfg
        self.sleep = sleep
        self.clock = clock
        self._raw_write = raw_write or self._default_raw_write
        self.rows: list[dict[str, Any]] = []
        self.abort_reason: str | None = None

    def _default_raw_write(self, reg: int, fields: Mapping[str, Any], nums: int) -> None:
        if nums == 1:
            self.client.write(reg, fields, expect_response=False, label=f"写{REG_FIELD_V1.get(reg, reg)}")
        else:
            write_raw(self.client, reg, fields, nums=nums, label=f"写{REG_FIELD_V1.get(reg, reg)}")

    # ------------------------------------------------------------ 基础操作
    def _read(self, reg: int) -> Any | None:
        name = REG_FIELD_V1[reg]
        return self.client.try_read_field(reg, name, label=f"读{name}")

    def _clear_error(self, hold_s: float, settle_s: float) -> None:
        self.hb.set(RegV1.RT_SETTING, self.cfg.clear_err_value)
        self.sleep(hold_s)
        self.hb.set(RegV1.RT_SETTING, 0)
        if settle_s > 0:
            self.sleep(settle_s)

    # ------------------------------------------------------------ Case 1
    def case_link(self) -> CaseOutcome:
        ver = None
        for attempt in range(1, self.cfg.ping_attempts + 1):
            ver = self._read(RegV1.HW_VERSION)
            if ver is not None and ver > 0:
                return CaseOutcome(PASS, f"HW Ver {ver}", {"hw_version": ver, "attempts": attempt})
            log.warning("读取硬件版本失败/无效 (第 %d/%d 次): %r", attempt, self.cfg.ping_attempts, ver)
            if attempt < self.cfg.ping_attempts:
                self.sleep(self.cfg.ping_retry_delay_s)
        return CaseOutcome(FAIL, "通信断开：无法读取有效硬件版本", {"hw_version": ver})

    # ------------------------------------------------------------ Case 2
    def case_environment(self) -> CaseOutcome:
        raw_volt = self._read(RegV1.BUS_VOLTAGE)
        if raw_volt is None:
            return CaseOutcome(FAIL, "无法读取母线电压")
        volt = raw_volt * self.cfg.voltage_scale
        measured: dict[str, Any] = {"bus_voltage_v": round(volt, 2)}
        log.info("  -> 电压: %.1fV", volt)
        if volt < self.cfg.min_bus_voltage_v:
            return CaseOutcome(FAIL, f"母线电压 {volt:.1f}V 低于下限 {self.cfg.min_bus_voltage_v:.1f}V", measured)

        err = self._read(RegV1.MCU_ERRCODE)
        measured["mcu_errcode"] = err
        if err is None:
            return CaseOutcome(FAIL, "无法读取错误码", measured)
        if err == 0:
            return CaseOutcome(PASS, f"电压 {volt:.1f}V，无错误", measured)

        log.warning("检测到错误码 0x%06X，尝试清除...", err)
        self._clear_error(self.cfg.clear_err_hold_s, self.cfg.clear_err_settle_s)
        err_new = self._read(RegV1.MCU_ERRCODE)
        measured["mcu_errcode_after_clear"] = err_new
        if err_new == 0:
            return CaseOutcome(PASS, f"错误码 0x{err:06X} 已清除", measured)
        if err_new is None:
            return CaseOutcome(FAIL, f"清除错误码 0x{err:06X} 后无法回读", measured)
        return CaseOutcome(FAIL, f"无法清除错误码 0x{err_new:06X}", measured)

    # ------------------------------------------------------------ Case 3
    def case_mode_switch(self) -> CaseOutcome:
        self.hb.set(RegV1.RUN_MODE, self.cfg.test_run_mode)
        self.hb.set(RegV1.DAT_SETTING, self.cfg.test_dat_setting)
        self.sleep(self.cfg.mode_settle_s)
        mode = self._read(RegV1.RUN_MODE)
        measured = {"run_mode": mode}
        if mode == self.cfg.test_run_mode:
            return CaseOutcome(PASS, f"已进入 TEST 模式 (Mode={mode})", measured)
        return CaseOutcome(FAIL, f"模式切换失败: Mode={mode}，期望 {self.cfg.test_run_mode}", measured)

    # ------------------------------------------------------------ Case 4
    def case_gear_rw(self) -> CaseOutcome:
        with self.hb.paused(self.cfg.hb_pause_settle_s):
            self._raw_write(RegV1.GEARS, {"gear": self.cfg.gear_value}, 1)
            self.sleep(self.cfg.gear_settle_s)
            value = self._read(RegV1.GEARS)
        measured = {"gear": value}
        if value == self.cfg.gear_value:
            return CaseOutcome(PASS, f"Gear 回读 {value}", measured)
        return CaseOutcome(FAIL, f"Gear 回读 {value}，期望 {self.cfg.gear_value}", measured)

    # ------------------------------------------------------------ Case 5
    def case_light_io(self) -> CaseOutcome:
        mask = self.cfg.light_mask
        with self.hb.paused(self.cfg.hb_pause_settle_s):
            try:
                self._raw_write(RegV1.RT_SETTING, {"rt_setting": mask}, 1)
                self.sleep(self.cfg.light_settle_s)
                rt_read = self._read(RegV1.RT_SETTING)
            finally:
                try:
                    self._raw_write(RegV1.RT_SETTING, {"rt_setting": 0}, 1)
                except (HardwareError, ProtocolError) as exc:
                    log.error("恢复 RT_SETTING=0 失败（心跳恢复后会继续刷新为 0）: %s", exc)
        measured = {"rt_setting": rt_read}
        if rt_read is None:
            return CaseOutcome(FAIL, "无法回读 RT_SETTING", measured)
        if (rt_read & mask) == mask:
            return CaseOutcome(PASS, f"灯光位 0x{mask:04X} 已置位 (RT=0x{rt_read:04X})", measured)
        return CaseOutcome(FAIL, f"灯光位未置位: RT=0x{rt_read:04X}，期望包含 0x{mask:04X}", measured)

    # ------------------------------------------------------------ Case 6
    def case_power_loop(self) -> CaseOutcome:
        cfg = self.cfg
        log.info("  -> 设置 Acc = %d (软启动)", cfg.acceleration)
        self._raw_write(RegV1.ACCERATION, {"acceration": cfg.acceleration}, cfg.acceleration_nums)
        self.sleep(cfg.acceleration_settle_s)

        log.info("  -> 目标: %d RPM", cfg.target_rpm)
        reached = False
        final_rpm: int | None = None
        max_abs_rpm = 0
        samples = 0
        err_clears = 0
        self.hb.set(RegV1.TARGET_SPEED, cfg.target_rpm)
        try:
            for i in range(1, cfg.speed_poll_count + 1):
                speed = self._read(RegV1.MOTOR_SPEED)
                if speed is not None:
                    samples += 1
                    final_rpm = speed
                    max_abs_rpm = max(max_abs_rpm, abs(speed))
                    log.info("  [%d/%d] 实际转速 %d RPM", i, cfg.speed_poll_count, speed)
                else:
                    log.warning("  [%d/%d] 读取转速失败", i, cfg.speed_poll_count)

                err = self._read(RegV1.MCU_ERRCODE)
                if err:
                    err_clears += 1
                    log.warning("  运行中检测到错误码 0x%06X，尝试清除", err)
                    self._clear_error(cfg.speed_err_clear_hold_s, 0.0)

                # 转速反馈为负属电机相序定义，取绝对值比较
                if speed is not None and abs(abs(speed) - cfg.target_rpm) < cfg.rpm_tolerance:
                    reached = True
                    break
                self.sleep(cfg.speed_poll_interval_s)
        finally:
            self.hb.set(RegV1.TARGET_SPEED, 0)

        measured = {"final_rpm": final_rpm, "max_abs_rpm": max_abs_rpm, "samples": samples, "err_clears": err_clears}
        if reached:
            return CaseOutcome(PASS, f"响应正常 (实际 {final_rpm} RPM，误差 < {cfg.rpm_tolerance})", measured)
        return CaseOutcome(FAIL, f"未达到目标 {cfg.target_rpm}±{cfg.rpm_tolerance} RPM，最终读数 {final_rpm}", measured)

    # ------------------------------------------------------------ 编排
    def _record(self, spec: CaseSpec, outcome: CaseOutcome, elapsed_s: float) -> dict[str, Any]:
        row = {
            "case_id": spec.case_id,
            "name": spec.name,
            "verdict": outcome.verdict,
            "detail": outcome.detail,
            "measured": outcome.measured,
            "elapsed_s": round(elapsed_s, 3),
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        self.rows.append(row)
        return row

    def run_case(self, spec: CaseSpec) -> CaseOutcome:
        if self.abort_reason is None and self.hb.failed_event.is_set():
            self.abort_reason = "心跳连续写失败"
        if self.abort_reason is not None:
            outcome = CaseOutcome(SKIP, f"已跳过：{self.abort_reason}")
            self._record(spec, outcome, 0.0)
            log.warning("[Case %d] %s -> SKIP (%s)", spec.case_id, spec.name, self.abort_reason)
            return outcome

        log.info("[Case %d] %s", spec.case_id, spec.name)
        start = self.clock()
        try:
            outcome = getattr(self, spec.method)()
        except SerialDisconnectedError as exc:
            outcome = CaseOutcome(FAIL, f"串口断开: {exc}")
            self.abort_reason = "串口断开"
        except (HardwareError, ProtocolError) as exc:
            outcome = CaseOutcome(FAIL, f"通信/协议异常: {exc}")
        elapsed = self.clock() - start

        level = logging.INFO if outcome.verdict == PASS else logging.ERROR
        log.log(level, "  [%s] %s", outcome.verdict, outcome.detail)
        self._record(spec, outcome, elapsed)
        if outcome.verdict == FAIL and spec.critical and self.cfg.stop_on_critical_fail and self.abort_reason is None:
            self.abort_reason = f"关键用例 Case {spec.case_id} 失败"
        return outcome

    def run_all(self, cases: tuple[CaseSpec, ...] = CASES) -> list[dict[str, Any]]:
        for spec in cases:
            self.run_case(spec)
        return self.rows

    def teardown(self) -> None:
        """停转、灭灯。影子值先归零，再直接下发一次，不依赖心跳线程是否来得及刷新。"""
        try:
            self.hb.set(RegV1.TARGET_SPEED, 0)
            self.hb.set(RegV1.RT_SETTING, 0)
            self._raw_write(RegV1.TARGET_SPEED, {"target_speed": 0}, 1)
            self._raw_write(RegV1.RT_SETTING, {"rt_setting": 0}, 1)
            self.sleep(self.cfg.teardown_settle_s)
        except Exception:  # noqa: BLE001 - teardown 必须走完，后续还要停心跳/关串口
            log.exception("teardown 下发安全值时发生异常")


# ============================================================ 入口
def run(settings: AppSettings, ctx: RunContext, *, connect: Connector = open_region_client) -> int:
    link = settings.section("link", McbLinkConfig, required=False)
    hb_cfg = settings.section("heartbeat", HeartbeatConfig, required=False)
    cfg = settings.section("mcb_whitebox", McbWhiteboxConfig, required=False)

    log.info("=" * 60)
    log.info("MCB 白盒测试 | DLL: %s | 串口端点: serial.%s", link.dll, link.serial)
    log.info("=" * 60)

    tester: McbWhiteboxTester | None = None
    heartbeat: ShadowHeartbeat | None = None
    started = time.monotonic()
    try:
        with connect(settings, link) as client:
            heartbeat = ShadowHeartbeat(
                client, mcb_shadow_regs(), period_s=hb_cfg.period_s,
                max_consecutive_failures=hb_cfg.max_consecutive_failures,
            )
            tester = McbWhiteboxTester(client, heartbeat, cfg)
            heartbeat.start()
            try:
                time.sleep(cfg.setup_settle_s)
                tester.run_all()
            finally:
                tester.teardown()
                heartbeat.stop(hb_cfg.stop_timeout_s)
    finally:
        rows = tester.rows if tester else []
        counts = count_verdicts(rows)
        summary = {
            "工位": settings.station,
            "用例总数": len(CASES),
            "已执行": len(rows),
            "通过": counts[PASS],
            "失败": counts[FAIL],
            "跳过": counts[SKIP],
            "中止原因": tester.abort_reason if tester else "未能建立连接",
            "心跳写失败次数": heartbeat.total_failures if heartbeat else 0,
            "DLL": str(link.dll),
            "耗时(s)": round(time.monotonic() - started, 1),
        }
        write_reports(ctx, REPORT_STEM, title="MCB 白盒测试报告", rows=rows, columns=REPORT_COLUMNS,
                      summary=summary)
        log.info("测试结束：通过 %d / 失败 %d / 跳过 %d", counts[PASS], counts[FAIL], counts[SKIP])

    all_pass = len(rows) == len(CASES) and counts[PASS] == len(CASES)
    return 0 if all_pass else 1

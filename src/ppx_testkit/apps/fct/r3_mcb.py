"""R3 MCB SMT FCT 压力测试（继电器控制治具供电）。

迁移自 ``R3系列总装测试工具/R3_MCB_SMT.py``。每轮流程::

    点击“开始测试” -> 等待界面清空 -> 每秒轮询（每次先点击 OK 关闭提示弹窗）
    -> 全绿 PASS / 出现红色 FAIL / 超时 TIMEOUT
    -> PASS/FAIL 后固定等待 post_verdict_wait_s
    -> FAIL/TIMEOUT/异常 执行恢复：继电器断电-上电 -> 上位机串口关闭再打开

继电器指令失败只记录错误、不中断压测（与旧脚本一致）；退出时始终释放继电器串口。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ppx_testkit.apps.fct.common import (
    ColorScreen,
    ColorVerdict,
    Point,
    ScreenSettings,
    build_screen,
    is_failsafe,
    poll_decision,
    probe_all,
    require_non_negative,
    save_failure_screenshot,
    validate_points,
    write_reports,
)
from ppx_testkit.core.factory import open_serial, relay_from_settings
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.exceptions import ConfigError, HardwareError, TestAbort
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)

Outcome = Literal["PASS", "FAIL", "TIMEOUT"]


class PowerRelay(Protocol):
    def on(self, channel: int = 1) -> None: ...

    def off(self, channel: int = 1) -> None: ...


@dataclass(frozen=True)
class McbFctConfig:
    start_button: Point
    ok_button: Point
    serial_toggle: Point
    check_points: list[Point]
    cycles: int = 9999
    relay_channel: int = 1
    ui_reset_delay_s: float = 2.0
    max_wait_polls: int = 100
    poll_half_s: float = 0.5
    color_margin: int = 30
    post_verdict_wait_s: float = 100.0
    post_result_delay_s: float = 1.0
    serial_toggle_delay_s: float = 3.0
    power_off_s: float = 3.0
    reboot_wait_s: float = 2.0
    error_retry_delay_s: float = 5.0
    power_on_at_start: bool = True
    power_off_on_exit: bool = False
    stop_on_fail: bool = False
    screenshot_on_fail: bool = True
    screen: ScreenSettings = field(default_factory=ScreenSettings)

    def __post_init__(self) -> None:
        if self.cycles <= 0 or self.max_wait_polls <= 0:
            raise ConfigError("fct.cycles / fct.max_wait_polls 必须 > 0")
        validate_points(self.check_points, "fct.check_points")
        validate_points([self.start_button, self.ok_button, self.serial_toggle], "fct 按钮坐标")
        require_non_negative({
            "fct.ui_reset_delay_s": self.ui_reset_delay_s,
            "fct.poll_half_s": self.poll_half_s,
            "fct.post_verdict_wait_s": self.post_verdict_wait_s,
            "fct.post_result_delay_s": self.post_result_delay_s,
            "fct.serial_toggle_delay_s": self.serial_toggle_delay_s,
            "fct.power_off_s": self.power_off_s,
            "fct.reboot_wait_s": self.reboot_wait_s,
            "fct.error_retry_delay_s": self.error_retry_delay_s,
        })


def needs_recovery(outcome: Outcome) -> bool:
    return outcome != "PASS"


def outcome_detail(outcome: Outcome, verdict: ColorVerdict | None, max_wait_polls: int) -> str:
    if outcome == "PASS":
        return ""
    if outcome == "FAIL":
        return f"检测到红色异常点位: {verdict.not_green if verdict else []}"
    return f"达到等待上限 {max_wait_polls} 秒，未能全绿" + (f"，未绿坐标: {verdict.not_green}" if verdict else "")


class R3McbFctCycle:
    def __init__(self, cfg: McbFctConfig, screen: ColorScreen, relay: PowerRelay | None, *,
                 transport: Any = None, ctx: RunContext | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.screen = screen
        self.relay = relay
        self.transport = transport
        self.ctx = ctx
        self._sleep = sleep
        self.rows: list[dict[str, Any]] = []
        self.relay_errors = 0

    # ------------------------------------------------------------ StressCycle
    def setup(self) -> None:
        log.info("开始执行 R3 MCB FCT 自动化测试，判定点位 %d 个", len(self.cfg.check_points))
        if self.cfg.power_on_at_start:
            log.info("初始化阶段: 开启继电器以保持治具供电状态...")
            self._relay(True)

    def run_cycle(self, index: int) -> CycleResult:
        try:
            outcome, verdict, polls = self._execute()
        except TestAbort:
            raise
        except Exception as exc:  # noqa: BLE001 - 与旧脚本一致：异常计失败并执行恢复流程
            if is_failsafe(exc):
                raise
            log.error("第 %d 次循环中发生未知异常: %s", index, exc, exc_info=True)
            self._on_fail(index)
            detail = f"循环异常: {exc}"
            self._record(index, "ERROR", detail, 0)
            log.warning("未知异常触发治具安全重启恢复流程...")
            self.recover()
            log.info("跳过当前错误，等待 %.1f 秒后继续下一次执行...", self.cfg.error_retry_delay_s)
            self._sleep(self.cfg.error_retry_delay_s)
            return CycleResult(index, False, detail)

        detail = outcome_detail(outcome, verdict, self.cfg.max_wait_polls)
        if outcome == "PASS":
            log.info("本次测试最终结果: [成功]")
        else:
            log.error("本次测试最终结果: [失败] %s", detail)
            self._on_fail(index)
        if outcome in ("PASS", "FAIL"):
            log.info("已判定为 %s，固定等待 %.0f 秒...", outcome, self.cfg.post_verdict_wait_s)
            self._sleep(self.cfg.post_verdict_wait_s)
        self._record(index, outcome, detail, polls)

        if needs_recovery(outcome):
            log.warning("执行判定失败后的异常恢复步骤...")
            self.recover()
            log.info("失败恢复步骤执行完毕，即将进入下一轮循环触发测试。")
        else:
            self._sleep(self.cfg.post_result_delay_s)
        return CycleResult(index, outcome == "PASS", detail, data={"outcome": outcome, "polls": polls})

    def teardown(self, summary: RunSummary) -> None:
        summary.extra["继电器指令失败次数"] = self.relay_errors
        try:
            if self.cfg.power_off_on_exit and not summary.keep_power and self.relay is not None:
                log.info("退出阶段: 继电器断电")
                self._relay(False)
        finally:
            if self.transport is not None:
                self.transport.close()

    # ------------------------------------------------------------ 流程
    def _execute(self) -> tuple[Outcome, ColorVerdict | None, int]:
        cfg = self.cfg
        log.info("点击 开始测试 按钮，坐标 %s", cfg.start_button)
        self.screen.click(cfg.start_button, label="开始测试")
        log.info("延迟缓冲: 等待 %.1f 秒，确保上位机已清空上一次的测试结果...", cfg.ui_reset_delay_s)
        self._sleep(cfg.ui_reset_delay_s)

        log.info("检测阶段: 正在动态监测测试状态，最大等待 %d 秒...", cfg.max_wait_polls)
        verdict: ColorVerdict | None = None
        for sec in range(cfg.max_wait_polls):
            self.screen.click(cfg.ok_button, label="OK")
            self._sleep(cfg.poll_half_s)
            verdict = probe_all(self.screen, cfg.check_points, drift=0, margin=cfg.color_margin)
            decision = poll_decision(verdict, fail_on_red=True)
            if decision == "PASS":
                log.info("通知: 用时约 %d 秒，检测到所有点位变绿，锁定结果。", sec + 1)
                return "PASS", verdict, sec + 1
            if decision == "FAIL":
                log.warning("通知: 用时约 %d 秒，检测到红色异常点位 %s，锁定结果。", sec + 1, verdict.not_green)
                return "FAIL", verdict, sec + 1
            self._sleep(cfg.poll_half_s)
        return "TIMEOUT", verdict, cfg.max_wait_polls

    def recover(self) -> None:
        """继电器断电-上电，再重启上位机软件串口。任何一步失败都只记录，继续后续步骤。"""
        cfg = self.cfg
        log.info("恢复步骤1: 继电器断电，保持 %.1f 秒...", cfg.power_off_s)
        self._relay(False)
        self._sleep(cfg.power_off_s)
        log.info("恢复步骤1: 继电器重新通电，等待治具启动 %.1f 秒...", cfg.reboot_wait_s)
        self._relay(True)
        self._sleep(cfg.reboot_wait_s)
        self.toggle_software_serial()

    def toggle_software_serial(self) -> None:
        cfg = self.cfg
        try:
            log.info("恢复步骤2: 重启上位机串口，坐标 %s", cfg.serial_toggle)
            self.screen.click(cfg.serial_toggle, label="关闭串口")
            self._sleep(cfg.serial_toggle_delay_s)
            self.screen.click(cfg.serial_toggle, label="打开串口")
            self._sleep(cfg.serial_toggle_delay_s)
        except Exception as exc:  # noqa: BLE001
            if is_failsafe(exc):
                raise
            log.error("重启上位机串口时发生错误: %s", exc, exc_info=True)

    def _relay(self, on: bool) -> bool:
        if self.relay is None:
            log.warning("未配置继电器，跳过%s", "上电" if on else "断电")
            return False
        try:
            if on:
                self.relay.on(self.cfg.relay_channel)
            else:
                self.relay.off(self.cfg.relay_channel)
            return True
        except HardwareError as exc:
            self.relay_errors += 1
            log.error("继电器%s失败: %s", "上电" if on else "断电", exc)
            return False

    def _on_fail(self, index: int) -> None:
        if self.cfg.screenshot_on_fail:
            save_failure_screenshot(self.screen, self.ctx, f"fail_cycle{index:05d}.png")

    def _record(self, index: int, outcome: str, detail: str, polls: int) -> None:
        self.rows.append({"cycle": index, "verdict": "PASS" if outcome == "PASS" else "FAIL",
                          "outcome": outcome, "polls": polls, "detail": detail})


def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("fct", McbFctConfig)
    screen = build_screen(cfg.screen)
    transport = open_serial(settings, "relay", auto_open=False)
    try:
        relay = relay_from_settings(settings, transport)
        cycle = R3McbFctCycle(cfg, screen, relay, transport=transport, ctx=ctx)
        summary = StressRunner(cycle, cfg.cycles, station=settings.station, stop_on_fail=cfg.stop_on_fail).run()
    finally:
        transport.close()
    write_reports(ctx, "report", title=f"{settings.station} FCT 测试报告", rows=cycle.rows,
                  summary=summary.to_dict(), columns=["cycle", "verdict", "outcome", "polls", "detail"])
    return 0 if summary.ok else 1

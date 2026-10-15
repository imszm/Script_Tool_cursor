"""R3 LEB SMT（非继电器版）FCT 压力测试。

迁移自 ``R3系列总装测试工具/R3_LEB_SMT_非继电器.py``。每轮流程::

    点击上位机获取焦点 -> 输入 SN + 回车 -> 等待 -> 点击确认 -> 等待界面清空
    -> 每秒轮询判定点位（满 ok_button_after_polls 秒后每秒点击一次 OK）
    -> 全绿 PASS / 超时 FAIL -> SN 末尾数字 +1

本轮发生异常时计为失败、等待后继续下一轮，且 SN 不递增（与旧脚本一致）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.exceptions import ConfigError, TestAbort
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings
from ppx_testkit.utils.sn import increment_serial

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LebFctConfig:
    initial_sn: str
    app_focus: Point
    confirm_button: Point
    ok_button: Point
    check_points: list[Point]
    cycles: int = 9999
    focus_settle_s: float = 0.5
    type_interval_s: float = 0.02
    after_enter_wait_s: float = 10.0
    ui_reset_delay_s: float = 6.0
    max_wait_polls: int = 92
    ok_button_after_polls: int = 10
    poll_half_s: float = 0.5
    drift_px: int = 25
    drift_step_px: int = 5
    color_margin: int = 30
    post_result_delay_s: float = 2.0
    error_retry_delay_s: float = 5.0
    stop_on_fail: bool = False
    screenshot_on_fail: bool = True
    screen: ScreenSettings = field(default_factory=ScreenSettings)

    def __post_init__(self) -> None:
        if self.cycles <= 0 or self.max_wait_polls <= 0:
            raise ConfigError("fct.cycles / fct.max_wait_polls 必须 > 0")
        if self.drift_px < 0 or self.drift_step_px <= 0:
            raise ConfigError("fct.drift_px 不能为负，fct.drift_step_px 必须 > 0")
        validate_points(self.check_points, "fct.check_points")
        validate_points([self.app_focus, self.confirm_button, self.ok_button], "fct 按钮坐标")
        require_non_negative({
            "fct.focus_settle_s": self.focus_settle_s,
            "fct.after_enter_wait_s": self.after_enter_wait_s,
            "fct.ui_reset_delay_s": self.ui_reset_delay_s,
            "fct.poll_half_s": self.poll_half_s,
            "fct.post_result_delay_s": self.post_result_delay_s,
            "fct.error_retry_delay_s": self.error_retry_delay_s,
            "fct.ok_button_after_polls": self.ok_button_after_polls,
        })
        try:
            increment_serial(self.initial_sn)
        except ValueError as exc:
            raise ConfigError(f"fct.initial_sn 非法: {exc}") from exc


def timeout_detail(verdict: ColorVerdict | None, max_wait_polls: int) -> str:
    if verdict is None:
        return f"达到检测上限 {max_wait_polls} 秒，未获取到判定结果"
    if verdict.has_red:
        return f"达到检测上限 {max_wait_polls} 秒，存在红色异常点位: {verdict.not_green}"
    return f"达到检测上限 {max_wait_polls} 秒，未能全绿，未绿坐标: {verdict.not_green}"


class R3LebFctCycle:
    """实现 StressCycle 协议；所有界面动作通过注入的 screen 完成。"""

    def __init__(self, cfg: LebFctConfig, screen: ColorScreen, *, ctx: RunContext | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.screen = screen
        self.ctx = ctx
        self._sleep = sleep
        self.current_sn = cfg.initial_sn
        self.rows: list[dict[str, Any]] = []

    # ------------------------------------------------------------ StressCycle
    def setup(self) -> None:
        log.info("开始执行 R3 LEB FCT 自动化测试，起始序列号 [%s]，判定点位 %d 个",
                 self.current_sn, len(self.cfg.check_points))

    def run_cycle(self, index: int) -> CycleResult:
        sn = self.current_sn
        try:
            passed, detail, polls = self._execute(sn)
        except TestAbort:
            raise
        except Exception as exc:  # noqa: BLE001 - 与旧脚本一致：单轮异常计失败后继续
            if is_failsafe(exc):
                raise
            log.error("第 %d 次循环中发生未知异常: %s", index, exc, exc_info=True)
            self._on_fail(index)
            self._record(index, sn, False, f"循环异常: {exc}", 0)
            log.info("跳过当前错误，等待 %.1f 秒后继续下一次执行...", self.cfg.error_retry_delay_s)
            self._sleep(self.cfg.error_retry_delay_s)
            return CycleResult(index, False, f"循环异常: {exc}", data={"sn": sn})

        if passed:
            log.info("本次测试最终结果: [成功]")
        else:
            log.error("本次测试最终结果: [失败] (%s)", detail)
            self._on_fail(index)
        self._record(index, sn, passed, detail, polls)

        next_sn = increment_serial(sn)
        log.info("序列号更新: [%s] -> [%s]", sn, next_sn)
        self.current_sn = next_sn
        self._sleep(self.cfg.post_result_delay_s)
        return CycleResult(index, passed, detail, data={"sn": sn, "polls": polls})

    def teardown(self, summary: RunSummary) -> None:
        summary.extra["下一个序列号"] = self.current_sn

    # ------------------------------------------------------------ 流程
    def _execute(self, sn: str) -> tuple[bool, str, int]:
        cfg = self.cfg
        log.info("步骤0: 点击上位机激活窗口，坐标 %s", cfg.app_focus)
        self.screen.click(cfg.app_focus, label="上位机窗口")
        self._sleep(cfg.focus_settle_s)

        log.info("步骤1/2: 键盘输入序列号 [%s] 并回车", sn)
        self.screen.type_text(sn, interval=cfg.type_interval_s, enter=True)
        self._sleep(cfg.after_enter_wait_s)

        log.info("步骤3: 点击确认按钮，坐标 %s", cfg.confirm_button)
        self.screen.click(cfg.confirm_button, label="确认")
        log.info("延迟缓冲: 等待 %.1f 秒，确保上位机已清空上一次的测试结果...", cfg.ui_reset_delay_s)
        self._sleep(cfg.ui_reset_delay_s)

        log.info("步骤4: 正在动态监测测试状态，最大等待 %d 秒...", cfg.max_wait_polls)
        verdict: ColorVerdict | None = None
        for sec in range(cfg.max_wait_polls):
            if sec >= cfg.ok_button_after_polls:
                self._click_ok()
            self._sleep(cfg.poll_half_s)
            verdict = probe_all(self.screen, cfg.check_points, drift=cfg.drift_px,
                                step=cfg.drift_step_px, margin=cfg.color_margin)
            if poll_decision(verdict, fail_on_red=False) == "PASS":
                log.info("通知: 用时约 %d 秒，检测到所有点位变绿，提前结束本次等待。", sec + 1)
                return True, "", sec + 1
            log.debug("第 %d 秒：%s尚未全绿，继续等待...", sec + 1, "检测到红色过渡状态，" if verdict.has_red else "")
            self._sleep(cfg.poll_half_s)
        return False, timeout_detail(verdict, cfg.max_wait_polls), cfg.max_wait_polls

    def _click_ok(self) -> None:
        try:
            self.screen.click(self.cfg.ok_button, label="OK")
        except Exception as exc:  # noqa: BLE001 - 点击 OK 失败不影响判定
            if is_failsafe(exc):
                raise
            log.debug("点击OK按钮时发生异常: %s", exc)

    def _on_fail(self, index: int) -> None:
        if self.cfg.screenshot_on_fail:
            save_failure_screenshot(self.screen, self.ctx, f"fail_cycle{index:05d}.png")

    def _record(self, index: int, sn: str, passed: bool, detail: str, polls: int) -> None:
        self.rows.append({"cycle": index, "sn": sn, "verdict": "PASS" if passed else "FAIL",
                          "polls": polls, "detail": detail})


def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("fct", LebFctConfig)
    screen = build_screen(cfg.screen)
    cycle = R3LebFctCycle(cfg, screen, ctx=ctx)
    summary = StressRunner(cycle, cfg.cycles, station=settings.station, stop_on_fail=cfg.stop_on_fail).run()
    write_reports(ctx, "report", title=f"{settings.station} FCT 测试报告", rows=cycle.rows,
                  summary=summary.to_dict(), columns=["cycle", "sn", "verdict", "polls", "detail"])
    return 0 if summary.ok else 1

"""R3 整机组装升级压力测试（图像识别 + 锚点偏移，不依赖绝对坐标）。

迁移自 ``R3系列总装测试工具/R3组装升级.py``。每个 VIN 的流程::

    定位“VIN S/N”标签 -> 点击其右侧输入框 -> 清空 -> 输入 VIN + 回车
    -> 等待扫码弹窗出现 -> 依次输入左电机 / 右电机 / 电池 SN（各自回车）
    -> 图像定位并点击确认 -> 等待上位机组装及固件升级完成

任一图像在超时内未找到：本 VIN 计失败，直接进入下一个 VIN（与旧脚本一致）；
特征图读取失败等其他异常会终止整个测试。
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ppx_testkit.core.report.writers import write_csv, write_html
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.exceptions import ConfigError, ElementNotFoundError
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)

Point = tuple[int, int]


class AssemblyScreen(Protocol):
    def click_image(self, image: Path, timeout_s: float, *, offset: Point = (0, 0), confidence: float = 0.8,
                    poll_s: float = 0.5) -> Point: ...

    def wait_image(self, image: Path, timeout_s: float, *, confidence: float = 0.8, poll_s: float = 0.5) -> Any: ...

    def clear_focused_input(self, settle_s: float = 0.3) -> None: ...

    def type_text(self, text: str, *, interval: float = 0.02, enter: bool = False) -> None: ...

    def save_screenshot(self, path: Path) -> Path | None: ...


@dataclass(frozen=True)
class ImageSettings:
    vin_anchor: Path
    popup_window: Path
    confirm_button: Path

    def missing(self) -> list[Path]:
        return [p for p in (self.vin_anchor, self.popup_window, self.confirm_button) if not p.is_file()]


@dataclass(frozen=True)
class ScreenSettings:
    pause_s: float | None = None
    failsafe: bool = True


@dataclass(frozen=True)
class AssemblyConfig:
    base_vin: str
    motor_left_sn: str
    motor_right_sn: str
    battery_sn: str
    images: ImageSettings
    start_suffix: int = 1
    max_suffix: int = 99999
    suffix_width: int = 5
    vin_input_offset: Point = (200, 0)
    confidence: float = 0.8
    image_poll_s: float = 0.5
    anchor_timeout_s: float = 5.0
    popup_timeout_s: float = 181.0
    confirm_timeout_s: float = 5.0
    start_delay_s: float = 1.0
    after_vin_click_s: float = 1.0
    clear_settle_s: float = 0.3
    type_interval_s: float = 0.02
    popup_settle_s: float = 1.0
    after_sn_enter_s: float = 0.5
    upgrade_wait_s: float = 241.0
    stop_on_fail: bool = False
    screenshot_on_fail: bool = True
    screen: ScreenSettings = field(default_factory=ScreenSettings)

    def __post_init__(self) -> None:
        if self.suffix_width <= 0:
            raise ConfigError("assembly.suffix_width 必须 > 0")
        if self.start_suffix < 0 or self.max_suffix < self.start_suffix:
            raise ConfigError(f"assembly 后缀范围非法: {self.start_suffix}~{self.max_suffix}")
        if not 0 < self.confidence <= 1:
            raise ConfigError(f"assembly.confidence 应在 (0, 1] 内，实际 {self.confidence}")
        for name in ("anchor_timeout_s", "popup_timeout_s", "confirm_timeout_s", "image_poll_s"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"assembly.{name} 必须 > 0")
        for name in ("start_delay_s", "after_vin_click_s", "clear_settle_s", "type_interval_s",
                     "popup_settle_s", "after_sn_enter_s", "upgrade_wait_s"):
            if getattr(self, name) < 0:
                raise ConfigError(f"assembly.{name} 不能为负数")

    @property
    def total_cycles(self) -> int:
        return self.max_suffix - self.start_suffix + 1


def make_vin(base: str, suffix: int, width: int) -> str:
    """``base + 零填充后缀``；后缀超出位宽视为配置错误，避免生成长度异常的 VIN。"""
    text = f"{suffix:0{width}d}"
    if len(text) > width:
        raise ConfigError(f"VIN 后缀 {suffix} 超出 {width} 位")
    return f"{base}{text}"


class R3AssemblyUpgradeCycle:
    def __init__(self, cfg: AssemblyConfig, screen: AssemblyScreen, *, ctx: RunContext | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.screen = screen
        self.ctx = ctx
        self._sleep = sleep
        self.rows: list[dict[str, Any]] = []
        self.completed = 0

    def vin_for(self, index: int) -> str:
        return make_vin(self.cfg.base_vin, self.cfg.start_suffix + index - 1, self.cfg.suffix_width)

    # ------------------------------------------------------------ StressCycle
    def setup(self) -> None:
        make_vin(self.cfg.base_vin, self.cfg.max_suffix, self.cfg.suffix_width)
        log.info("自动化测试已启动，VIN 范围 %s ~ %s。紧急停止：将鼠标移动到屏幕角落。",
                 self.vin_for(1), self.vin_for(self.cfg.total_cycles))
        log.info("%.1f 秒后开始执行，请将焦点切换至目标上位机软件窗口...", self.cfg.start_delay_s)
        self._sleep(self.cfg.start_delay_s)

    def run_cycle(self, index: int) -> CycleResult:
        vin = self.vin_for(index)
        log.info("========== 开始执行测试循环，当前 VIN SN: %s ==========", vin)
        step = ""
        try:
            step = "定位 VIN 标签并点击输入框"
            self._click_vin_input()
            step = "输入 VIN"
            self._input_vin(vin)
            step = "等待扫码弹窗"
            self._wait_popup()
            step = "输入部件 SN"
            self._input_part_sns()
            step = "点击确认按钮"
            self._click_confirm()
        except ElementNotFoundError as exc:
            detail = f"{step}失败: {exc}"
            log.error("VIN %s %s，跳过本次，进入下一次循环。", vin, detail)
            self._on_fail(index, vin)
            self._record(index, vin, False, detail)
            return CycleResult(index, False, detail, data={"vin": vin})

        log.info("等待 %.0f 秒，等待上位机组装及固件升级完成...", self.cfg.upgrade_wait_s)
        self._sleep(self.cfg.upgrade_wait_s)
        self.completed += 1
        log.info("========== VIN SN: %s 测试循环执行完毕。当前已完成总次数: %d ==========", vin, self.completed)
        self._record(index, vin, True, "")
        return CycleResult(index, True, data={"vin": vin})

    def teardown(self, summary: RunSummary) -> None:
        summary.extra["成功执行次数"] = self.completed
        log.info("自动化测试任务结束，总计成功执行次数: %d", self.completed)

    # ------------------------------------------------------------ 步骤
    def _click_vin_input(self) -> None:
        cfg = self.cfg
        target = self.screen.click_image(cfg.images.vin_anchor, cfg.anchor_timeout_s, offset=cfg.vin_input_offset,
                                         confidence=cfg.confidence, poll_s=cfg.image_poll_s)
        log.info("已点击 VIN 输入框 %s（锚点偏移 %s）", target, cfg.vin_input_offset)
        self._sleep(cfg.after_vin_click_s)

    def _input_vin(self, vin: str) -> None:
        log.info("全选并删除，确保输入框清空...")
        self.screen.clear_focused_input(self.cfg.clear_settle_s)
        log.info("写入新的 VIN SN: %s", vin)
        self.screen.type_text(vin, interval=self.cfg.type_interval_s, enter=True)

    def _wait_popup(self) -> None:
        cfg = self.cfg
        log.info("开始检测扫码弹窗状态，最长等待 %.0f 秒...", cfg.popup_timeout_s)
        self.screen.wait_image(cfg.images.popup_window, cfg.popup_timeout_s, confidence=cfg.confidence,
                               poll_s=cfg.image_poll_s)
        log.info("弹窗已就位，%.1f 秒后开始执行后续动作...", cfg.popup_settle_s)
        self._sleep(cfg.popup_settle_s)

    def _input_part_sns(self) -> None:
        cfg = self.cfg
        for label, sn in (("左电机", cfg.motor_left_sn), ("右电机", cfg.motor_right_sn), ("电池", cfg.battery_sn)):
            log.info("输入%s序列号: %s", label, sn)
            self.screen.type_text(sn, interval=cfg.type_interval_s, enter=True)
            self._sleep(cfg.after_sn_enter_s)

    def _click_confirm(self) -> None:
        cfg = self.cfg
        log.info("准备点击确认按钮...")
        self.screen.click_image(cfg.images.confirm_button, cfg.confirm_timeout_s, confidence=cfg.confidence,
                                poll_s=cfg.image_poll_s)

    def _on_fail(self, index: int, vin: str) -> None:
        if not self.cfg.screenshot_on_fail or self.ctx is None:
            return
        path = self.ctx.artifact(f"fail_{index:05d}_{vin}_{_dt.datetime.now():%H%M%S}.png")
        if path is None:
            return
        try:
            self.screen.save_screenshot(path)
        except Exception:  # noqa: BLE001 - 截图失败不影响流程
            log.exception("保存失败截图异常: %s", path)

    def _record(self, index: int, vin: str, passed: bool, detail: str) -> None:
        self.rows.append({"cycle": index, "vin": vin, "verdict": "PASS" if passed else "FAIL", "detail": detail})


def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("assembly", AssemblyConfig)
    missing = cfg.images.missing()
    if missing:
        raise ConfigError(f"特征图不存在: {[str(p) for p in missing]}")
    from ppx_testkit.core.gui.screen import Screen

    screen = Screen(pause_s=cfg.screen.pause_s, failsafe=cfg.screen.failsafe)
    cycle = R3AssemblyUpgradeCycle(cfg, screen, ctx=ctx)
    summary = StressRunner(cycle, cfg.total_cycles, station=settings.station, stop_on_fail=cfg.stop_on_fail).run()
    columns = ["cycle", "vin", "verdict", "detail"]
    csv_path, html_path = ctx.artifact("report.csv"), ctx.artifact("report.html")
    if csv_path is not None:
        write_csv(csv_path, cycle.rows, columns)
    if html_path is not None:
        write_html(html_path, title=f"{settings.station} 组装升级报告", summary=summary.to_dict(),
                   rows=cycle.rows, columns=columns)
    return 0 if summary.ok else 1

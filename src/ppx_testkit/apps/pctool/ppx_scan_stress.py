"""PPX PCTOOL 产测扫码压力：点 VIN 框、回车弹出扫码窗、逐条回车录入、再点确认。

单轮流程::

    激活「PPX PCTOOL」-> 点击 VIN 输入框 -> 全选清空 -> 输入车架号并回车
    -> 等待扫码弹窗 -> 按从上到下的顺序输入条码，每条回车
    -> 优先按按钮标题点击「确认」，点不到则用配置坐标
    -> 等待产测流程结束，进入下一轮

五项条码不再逐个点击输入框：弹窗打开后焦点在第一格，回车会跳到下一格，和扫码枪一致。
``vin_click`` / ``confirm_click`` 填屏幕绝对坐标（``mouse_locate`` 打出来的 X,Y）。
``follow_window`` 为 true 时，以第一轮看到的窗口左上角为原点，之后窗口被拖走，点击跟着平移。
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ppx_testkit.apps.pctool.common import write_reports
from ppx_testkit.core.gui.screen import ScreenSettings, is_failsafe
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.exceptions import ConfigError, TestAbort, WindowNotFoundError
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)

Point = tuple[int, int]
Rect = tuple[int, int, int, int]

# 工位 yaml 里的占位坐标。没改过就在开跑时提醒，避免直接点到错误位置。
_PLACEHOLDER_VIN: Point = (500, 168)
_PLACEHOLDER_CONFIRM: Point = (620, 470)


def track_point(configured: Point, origin_now: Point, origin_locked: Point | None) -> tuple[Point, Point]:
    """把配置里的屏幕绝对坐标换成跟随窗口的点击点。

    第一次见到该窗口时，配置坐标就是当前屏幕上的点，并锁定窗口左上角。
    之后窗口平移了多少，点击点也平移多少。

    :param configured: yaml 里的屏幕绝对坐标。
    :param origin_now: 当前窗口左上角 ``(left, top)``。
    :param origin_locked: 已锁定的左上角；None 表示还没标定。
    :return: ``(本轮点击点, 锁定后的左上角)``。
    """
    if origin_locked is None:
        return configured, origin_now
    dx = origin_now[0] - origin_locked[0]
    dy = origin_now[1] - origin_locked[1]
    return (configured[0] + dx, configured[1] + dy), origin_locked


# ============================================================ 配置
@dataclass(frozen=True)
class ScanItem:
    """扫码弹窗里的一行：标签只用于日志，text 是实际键入的条码。"""

    label: str
    text: str

    def __post_init__(self) -> None:
        if not self.label or not str(self.text):
            raise ConfigError("条码的 label 和 text 都不能为空")


@dataclass(frozen=True)
class PpxScanStressConfig:
    window_title: str = "PPX PCTOOL"
    dialog_title: str = "扫码"
    confirm_button: str = "确认"
    vin: str = "P30101RZKUS6900002"
    barcodes: list[ScanItem] = field(default_factory=list)
    vin_click: Point = _PLACEHOLDER_VIN
    confirm_click: Point = _PLACEHOLDER_CONFIRM
    first_field_click: Point | None = None
    follow_window: bool = True
    confirm_by_name: bool = True
    require_dialog: bool = True
    clear_before_type: bool = True
    cycles: int = 100
    startup_delay_s: float = 3.0
    after_click_s: float = 0.2
    popup_wait_s: float = 2.0
    after_enter_s: float = 0.4
    settle_wait_s: float = 20.0
    type_interval_s: float = 0.03
    confirm_name_timeout_s: float = 2.0
    connect_timeout_s: float = 5.0
    screen: ScreenSettings = field(default_factory=ScreenSettings)

    def __post_init__(self) -> None:
        if self.cycles <= 0:
            raise ConfigError(f"stress.cycles 必须 > 0，实际 {self.cycles}")
        if not self.window_title:
            raise ConfigError("stress.window_title 不能为空")
        if not self.dialog_title:
            raise ConfigError("stress.dialog_title 不能为空")
        if self.confirm_by_name and not self.confirm_button:
            raise ConfigError("stress.confirm_by_name 为 true 时 confirm_button 不能为空")
        if not self.vin:
            raise ConfigError("stress.vin 不能为空")
        if not self.barcodes:
            raise ConfigError("stress.barcodes 至少要有一条")
        if self.connect_timeout_s <= 0:
            raise ConfigError("stress.connect_timeout_s 必须 > 0")
        for name in (
            "startup_delay_s",
            "after_click_s",
            "popup_wait_s",
            "after_enter_s",
            "settle_wait_s",
            "type_interval_s",
            "confirm_name_timeout_s",
        ):
            if getattr(self, name) < 0:
                raise ConfigError(f"stress.{name} 不能为负数，实际 {getattr(self, name)}")


# ============================================================ 依赖
class ClickScreen(Protocol):
    """键鼠操作。测试里用假对象替换，实机用 core Screen。"""

    def click(self, point: Point, *, label: str = "") -> None:
        """点击屏幕坐标。"""

    def hotkey(self, *keys: str) -> None:
        """按下组合键。"""

    def press(self, key: str) -> None:
        """按下单个键。"""

    def type_text(self, text: str, *, interval: float = 0.02, enter: bool = False) -> None:
        """逐字输入。``enter`` 为 True 时末尾再按回车。"""

    def save_screenshot(self, path: Path) -> Path | None:
        """保存全屏截图。失败返回 None。"""


class WindowGeometry(Protocol):
    """窗口位置与前台切换。"""

    def rect(self, title: str, *, exact: bool = False) -> Rect | None:
        """返回匹配窗口的 ``(left, top, right, bottom)``，没有则 None。"""

    def activate(self, title: str) -> bool:
        """尝试把窗口拉到前台。"""


class ButtonClicker(Protocol):
    """按弹窗标题和按钮文字点击。找不到时返回 False，由坐标点击接手。"""

    def click_button(self, title_re: str, button: str, timeout_s: float) -> bool:
        """点击按钮。成功返回 True。"""


class NullButtonClicker:
    """不连接控件，确认按钮始终走坐标。"""

    def click_button(self, title_re: str, button: str, timeout_s: float) -> bool:
        """始终点不到按钮。

        :param title_re: 弹窗标题正则，此处忽略。
        :param button: 按钮文字，此处忽略。
        :param timeout_s: 等待秒数，此处忽略。
        :return: 恒为 False。
        """
        return False


class AppButtonClicker:
    """用已连接的 AppWindow 按标题点击弹窗按钮。"""

    def __init__(self, window: Any) -> None:
        self._window = window

    def click_button(self, title_re: str, button: str, timeout_s: float) -> bool:
        """点击标题匹配的弹窗按钮。

        :param title_re: 弹窗标题正则。
        :param button: 按钮文字，例如「确认」。
        :param timeout_s: 最长等待秒数。
        :return: 是否已点击。异常时返回 False。
        """
        try:
            return bool(self._window.confirm_dialog(title_re, button, timeout_s))
        except Exception as exc:  # noqa: BLE001 - 控件点击失败时改走坐标
            log.warning("按名称点击 %s / %s 失败: %s", title_re, button, exc)
            return False


def open_button_clicker(cfg: PpxScanStressConfig) -> ButtonClicker:
    """连接上位机，供按按钮标题点击「确认」。连不上时退回坐标点击。

    :param cfg: 工位配置。
    :return: 按名称点击的对象；``confirm_by_name`` 为 false 或连接失败时为占位对象。
    """
    if not cfg.confirm_by_name:
        return NullButtonClicker()
    from ppx_testkit.core.gui.window import AppWindow

    title_re = f".*{re.escape(cfg.window_title)}.*"
    last_error = ""
    for backend in ("uia", "win32"):
        window = AppWindow(title_re=title_re, backend=backend, connect_timeout_s=cfg.connect_timeout_s)
        try:
            window.connect()
        except WindowNotFoundError as exc:
            last_error = str(exc)
            log.info("后端 %s 未连上 %s", backend, cfg.window_title)
            continue
        log.info("已用 %s 连接 %s，确认按钮优先按标题点击", backend, cfg.window_title)
        return AppButtonClicker(window)
    log.warning("无法按控件连接窗口（%s），确认按钮只使用坐标", last_error)
    return NullButtonClicker()


# ============================================================ 流程
class PpxScanStressCycle:
    """一轮：VIN 回车 -> 五条条码各回车一次 -> 确认 -> 等待产测结束。"""

    def __init__(
        self,
        cfg: PpxScanStressConfig,
        screen: ClickScreen,
        windows: WindowGeometry,
        buttons: ButtonClicker,
        *,
        ctx: RunContext | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.screen = screen
        self.windows = windows
        self.buttons = buttons
        self.ctx = ctx
        self._sleep = sleep
        self.rows: list[dict[str, Any]] = []
        self._origins: dict[str, Point] = {}
        self._logged_confirm_fallback = False

    def setup(self) -> None:
        """确认上位机窗口已打开，并留出切到该窗口的时间。窗口不存在时抛出 WindowNotFoundError。"""
        rect = self.windows.rect(self.cfg.window_title)
        if rect is None:
            raise WindowNotFoundError(f"未找到标题包含「{self.cfg.window_title}」的窗口，请先打开上位机。")
        log.info(
            "窗口矩形 left=%d top=%d right=%d bottom=%d。VIN 坐标 %s，确认坐标 %s。",
            rect[0], rect[1], rect[2], rect[3], self.cfg.vin_click, self.cfg.confirm_click,
        )
        if self.cfg.vin_click == _PLACEHOLDER_VIN or self.cfg.confirm_click == _PLACEHOLDER_CONFIRM:
            log.warning(
                "vin_click 或 confirm_click 仍是占位坐标。请先运行 mouse_locate，把屏幕上的 X,Y 写进 "
                "config/stations/ppx_pctool_stress.yaml，否则会点偏。"
            )
        log.info(
            "%.0f 秒后开始。请让上位机保持在最前面，输入法切到英文。停止：Ctrl+C，或把鼠标移到屏幕左上角。",
            self.cfg.startup_delay_s,
        )
        self._sleep(self.cfg.startup_delay_s)

    def run_cycle(self, index: int) -> CycleResult:
        """执行一轮扫码录入。窗口消失或扫码弹窗没出现时熔断。

        :param index: 轮次，从 1 开始。
        :return: 成功时的轮次结果。失败抛出异常，由执行器终止。
        """
        try:
            self._one_cycle()
        except Exception as exc:  # noqa: BLE001 - 记下现场后原样抛出，由执行器终止
            if not is_failsafe(exc):
                self.rows.append({"cycle": index, "verdict": "FAIL", "detail": str(exc)})
                self._screenshot(index)
            raise
        self.rows.append({"cycle": index, "verdict": "PASS", "detail": ""})
        return CycleResult(index, True)

    def teardown(self, summary: RunSummary) -> None:
        """把完成轮次写进总结。不关闭上位机。

        :param summary: 执行器汇总，本方法只补充计数字段。
        """
        summary.extra["完成轮次"] = summary.passed
        log.info("产测扫码压力结束，完成 %d 轮", summary.passed)

    def _one_cycle(self) -> None:
        """执行一轮的点击和输入。窗口或弹窗缺失时抛出 TestAbort。"""
        cfg = self.cfg
        self.windows.activate(cfg.window_title)
        # 最小化窗口被拉起后位置会变，必须在激活之后再读矩形。
        main = self._require_main()
        vin_point = self._resolve("vin", cfg.vin_click, (main[0], main[1]))
        self.screen.click(vin_point, label="VIN输入框")
        self._sleep(cfg.after_click_s)
        if cfg.clear_before_type:
            self.screen.hotkey("ctrl", "a")
            self.screen.press("backspace")
        log.info("输入车架号并回车: %s", cfg.vin)
        self.screen.type_text(cfg.vin, interval=cfg.type_interval_s, enter=True)
        self._sleep(cfg.popup_wait_s)
        dialog = self.windows.rect(cfg.dialog_title)
        if cfg.require_dialog and dialog is None:
            raise TestAbort(
                f"等待 {cfg.popup_wait_s:g} 秒后未出现标题包含「{cfg.dialog_title}」的扫码窗口。"
                "若弹窗标题不同，请改 stress.dialog_title；"
                "若弹窗没有独立窗口标题，把 stress.require_dialog 设为 false。"
            )
        if cfg.first_field_click is not None:
            origin = self._anchor_origin(dialog)
            point = self._resolve("first_field", cfg.first_field_click, origin)
            self.screen.click(point, label="扫码第一格")
            self._sleep(cfg.after_click_s)
        for item in cfg.barcodes:
            log.info("输入%s: %s", item.label, item.text)
            self.screen.type_text(item.text, interval=cfg.type_interval_s, enter=True)
            self._sleep(cfg.after_enter_s)
        self._confirm(dialog)
        log.info("等待 %.0f 秒，等产测流程结束", cfg.settle_wait_s)
        self._sleep(cfg.settle_wait_s)

    def _require_main(self) -> Rect:
        """读取主窗口矩形。不存在时熔断。

        :return: ``(left, top, right, bottom)``。
        """
        rect = self.windows.rect(self.cfg.window_title)
        if rect is None:
            raise TestAbort(f"未找到标题包含「{self.cfg.window_title}」的窗口，上位机可能已关闭。")
        return rect

    def _anchor_origin(self, dialog: Rect | None) -> Point:
        """确认按钮和第一格跟随的窗口左上角：优先扫码弹窗，否则主窗口。

        :param dialog: 扫码弹窗矩形；没有时为 None。
        :return: ``(left, top)``。都找不到时为 ``(0, 0)``。
        """
        if dialog is not None:
            return dialog[0], dialog[1]
        main = self.windows.rect(self.cfg.window_title)
        if main is None:
            return (0, 0)
        return main[0], main[1]

    def _resolve(self, key: str, configured: Point, origin: Point) -> Point:
        """按是否跟随窗口，算出本轮要点的屏幕坐标。

        :param key: 锚点名。VIN、第一格、确认各自记住自己的原点。
        :param configured: yaml 里的屏幕绝对坐标。
        :param origin: 当前锚点窗口左上角。
        :return: 实际点击坐标。
        """
        if not self.cfg.follow_window:
            log.info("%s 使用屏幕绝对坐标 %s", key, configured)
            return configured
        point, locked = track_point(configured, origin, self._origins.get(key))
        self._origins[key] = locked
        log.info("%s 点击 %s（窗口左上角 %s）", key, point, origin)
        return point

    def _confirm(self, dialog: Rect | None) -> None:
        """先按按钮标题点击确认，失败再用坐标。

        :param dialog: 扫码弹窗矩形。点击前会再读一次，避免录入过程中弹窗挪了位置。
        """
        cfg = self.cfg
        fresh = self.windows.rect(cfg.dialog_title)
        if fresh is not None:
            dialog = fresh
        title_re = f".*{re.escape(cfg.dialog_title)}.*"
        if self.buttons.click_button(title_re, cfg.confirm_button, cfg.confirm_name_timeout_s):
            log.info("已按按钮标题点击「%s」", cfg.confirm_button)
            return
        if cfg.confirm_by_name and not self._logged_confirm_fallback:
            log.warning(
                "没能按标题点到「%s」，改用坐标 %s。若这个坐标是占位，请改 confirm_click；"
                "若控件点不到，把 confirm_by_name 设为 false，可省掉每轮的等待。",
                cfg.confirm_button,
                cfg.confirm_click,
            )
            self._logged_confirm_fallback = True
        point = self._resolve("confirm", cfg.confirm_click, self._anchor_origin(dialog))
        self.screen.click(point, label=cfg.confirm_button)

    def _screenshot(self, index: int) -> None:
        """失败时保存全屏截图。没有运行目录或截图失败时只记日志。

        :param index: 失败轮次。
        """
        if self.ctx is None:
            return
        path = self.ctx.artifact(f"error_cycle{index:05d}_{_dt.datetime.now():%H%M%S}.png")
        if path is None:
            return
        try:
            self.screen.save_screenshot(path)
        except Exception:  # noqa: BLE001 - 截图失败不影响结果
            log.exception("保存截图失败: %s", path)


def run(settings: AppSettings, ctx: RunContext) -> int:
    """按工位配置循环点击 PPX PCTOOL 的 VIN 与扫码确认。

    :param settings: 合并后的工位配置。
    :param ctx: 本轮日志目录。
    :return: 全部通过为 0，否则为 1。
    """
    cfg = settings.section("stress", PpxScanStressConfig)
    from ppx_testkit.core.gui.process import TitleWindowProbe
    from ppx_testkit.core.gui.screen import Screen

    screen = Screen(pause_s=cfg.screen.pause_s, failsafe=cfg.screen.failsafe)
    cycle = PpxScanStressCycle(cfg, screen, TitleWindowProbe(), open_button_clicker(cfg), ctx=ctx)
    summary = StressRunner(cycle, cfg.cycles, station=settings.station).run()
    write_reports(
        ctx,
        "report",
        title=f"{settings.station} 产测扫码压力报告",
        rows=cycle.rows,
        summary=summary.to_dict(),
        columns=["cycle", "verdict", "detail"],
    )
    return 0 if summary.ok else 1

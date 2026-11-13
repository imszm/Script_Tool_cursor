"""循环键入条码，并按皮皮熊 CCB 右下角大字统计成功率。

大字三种颜色对应三种状态：绿色「通过」、红色「不通过」、黑色「测试中」。
旁边的「通过: N / 不通过: N」会滞后，测试刚开始时仍显示上一轮的数字，不能用来判断这一轮。
标题栏也可能还写着「测试中」，而大字已经变成红色「不通过」，所以只看大字。

回车后先等到大字变成「测试中」，确认新一轮已经开始，再等到「通过」或「不通过」。
一出现结果就键入下一条，不再固定睡满等待时间。``wait_s`` 只是看到「测试中」之后的最长等待。
失败大约要 100 秒才变成「不通过」，所以上限要大于 100 秒。

运行::

    ppx-test run barcode_type_loop

开始前有倒计时，请把光标点进目标输入框，并让 CCB 窗口的右下角露在前面。
Ctrl+C 或网页停止会在当前等待分段结束后退出。
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ppx_testkit.core.gui.screen import is_green, is_red
from ppx_testkit.core.registry import register_tool
from ppx_testkit.core.runner.result import RunSummary
from ppx_testkit.exceptions import ConfigError, WindowNotFoundError
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings
from ppx_testkit.utils.timing import responsive_sleep

log = logging.getLogger(__name__)

Status = Literal["pass", "fail", "running", "unknown"]
CycleVerdict = Literal["pass", "fail", "timeout", "not_started"]

# 大字笔画和边框的颜色比例。三个阈值都来自 V1.11.1 窗口截图：
# 通过约 15% 绿色，不通过约 17% 红色，测试中约 13% 近黑，空白背景接近 0。
_COLOR_MARGIN = 40
_DARK_MAX = 60
_PASS_RATIO = 0.04
_FAIL_RATIO = 0.04
_RUNNING_RATIO = 0.04
# 连续这么多轮都没进入「测试中」，就不再往下键入。焦点丢了或结果区被挡住时，继续跑满 1000 轮没有意义。
_START_MISS_LIMIT = 3

_STATUS_LABEL = {"pass": "通过", "fail": "不通过", "running": "测试中", "unknown": "未识别"}
_VERDICT_LABEL = {"pass": "通过", "fail": "不通过", "timeout": "超时", "not_started": "未开始"}


class TextTyper(Protocol):
    """逐键输入。``enter`` 为 True 时，字符送完后再按一次回车。"""

    def __call__(self, text: str, *, interval: float = 0.03, enter: bool = False) -> None:
        """把 ``text`` 送进当前焦点。

        :param text: 要键入的原文。
        :param interval: 两个字符之间的等待秒数。
        :param enter: 是否在末尾按回车。
        """


class StatusReader(Protocol):
    """读取上位机右下角大字的当前状态。"""

    def read(self) -> str:
        """返回 ``pass`` / ``fail`` / ``running`` / ``unknown``。

        :return: 识别到的状态。读不到窗口或颜色不够时为 ``unknown``。
        """


@dataclass(frozen=True)
class TypeLoopConfig:
    """条码键入循环的次数、结果区域和等待上限。"""

    text: str = "2020005002R309GD006900364"
    cycles: int = 1000
    # 看到「测试中」之后，最多再等这么久。通过大约 40 秒，不通过大约 100 秒。
    wait_s: float = 120.0
    startup_delay_s: float = 5.0
    type_interval_s: float = 0.03
    poll_s: float = 0.5
    # 回车后这么久还没变成「测试中」，认为这一轮没有开始。
    arm_timeout_s: float = 20.0
    window_title: str = "皮皮熊R3_CCB"
    # 相对整个窗口的左、上、右、下。对准右下角结果大字，避开左侧绿色 PASS 圆点。
    verdict_roi: tuple[float, float, float, float] = (0.70, 0.78, 0.98, 0.96)

    def __post_init__(self) -> None:
        if not str(self.text):
            raise ConfigError("type_loop.text 不能为空")
        if not str(self.window_title).strip():
            raise ConfigError("type_loop.window_title 不能为空")
        if self.cycles <= 0:
            raise ConfigError(f"type_loop.cycles 必须 > 0，实际 {self.cycles}")
        for name in ("wait_s", "startup_delay_s", "type_interval_s", "poll_s", "arm_timeout_s"):
            if getattr(self, name) < 0:
                raise ConfigError(f"type_loop.{name} 不能为负数，实际 {getattr(self, name)}")
        if self.wait_s <= 0 or self.poll_s <= 0 or self.arm_timeout_s <= 0:
            raise ConfigError("type_loop.wait_s、poll_s、arm_timeout_s 必须 > 0")
        left, top, right, bottom = self.verdict_roi
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ConfigError(
                "type_loop.verdict_roi 必须是 0 到 1 之间、左上小于右下的四个比例，"
                f"实际 {self.verdict_roi}"
            )


@dataclass
class TypeLoopResult:
    """循环结束后的统计，以及每一轮的记录。"""

    summary: RunSummary
    rows: list[dict[str, Any]] = field(default_factory=list)


def classify_stamp(samples: Sequence[tuple[int, int, int]]) -> Status:
    """根据右下角大字区域的像素判断这一轮状态。

    绿色占优是「通过」，红色占优是「不通过」，几乎没有红绿但有近黑笔画是「测试中」。
    空白或颜色不够时返回 ``unknown``，调用方继续等，不把上一轮的结果当成这一轮。

    :param samples: RGB 像素。只应包含大字区域，不要把左侧 PASS 圆点算进来。
    :return: ``pass`` / ``fail`` / ``running`` / ``unknown``。
    """
    total = len(samples)
    if total == 0:
        return "unknown"
    green = red = dark = 0
    for rgb in samples:
        if is_green(rgb, _COLOR_MARGIN) and rgb[1] > 90:
            green += 1
        elif is_red(rgb, _COLOR_MARGIN) and rgb[0] > 90:
            red += 1
        elif max(rgb[:3]) < _DARK_MAX:
            dark += 1
    green_ratio = green / total
    red_ratio = red / total
    dark_ratio = dark / total
    if green_ratio >= _PASS_RATIO and green_ratio > red_ratio * 3:
        return "pass"
    if red_ratio >= _FAIL_RATIO and red_ratio > green_ratio * 3:
        return "fail"
    if dark_ratio >= _RUNNING_RATIO and green_ratio < 0.02 and red_ratio < 0.02:
        return "running"
    return "unknown"


def stamp_pixels(image: Any, roi: tuple[float, float, float, float]) -> list[tuple[int, int, int]]:
    """按窗口比例裁出大字区域，并取出 RGB。

    :param image: 带 ``size``、``crop``、``getdata`` 的图像，例如 PIL Image。
    :param roi: ``(左, 上, 右, 下)``，都是相对宽高的 0 到 1。
    :return: 裁剪区域内的 RGB 三元组。区域退化时为空列表。
    """
    width, height = image.size
    left, top, right, bottom = roi
    x0 = min(width, max(0, int(width * left)))
    y0 = min(height, max(0, int(height * top)))
    x1 = min(width, max(0, int(width * right)))
    y1 = min(height, max(0, int(height * bottom)))
    if x1 <= x0 or y1 <= y0:
        return []
    pixels: list[tuple[int, int, int]] = []
    for pixel in image.crop((x0, y0, x1, y1)).getdata():
        if isinstance(pixel, int):
            pixels.append((pixel, pixel, pixel))
        else:
            pixels.append((int(pixel[0]), int(pixel[1]), int(pixel[2])))
    return pixels


def grab_window(rect: tuple[int, int, int, int]) -> Any:
    """截取窗口在屏幕上的矩形。窗口被别的窗口挡住时，截到的是挡在前面的内容。

    :param rect: ``(left, top, right, bottom)`` 屏幕坐标。
    :return: pyautogui 截图。
    :raises GuiError: 尺寸无效，或当前环境没有 pyautogui。
    """
    from ppx_testkit.exceptions import GuiError

    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise GuiError(f"窗口尺寸无效: {rect}")
    try:
        import pyautogui  # type: ignore[import-untyped]
    except ImportError as exc:
        raise GuiError("需要 pyautogui 才能截取上位机结果（pip install .[gui]）") from exc
    return pyautogui.screenshot(region=(left, top, width, height))


class WindowStampReader:
    """从标题匹配的窗口里读取右下角大字。"""

    def __init__(
        self,
        title: str,
        roi: tuple[float, float, float, float],
        probe: Any,
        grab: Callable[[tuple[int, int, int, int]], Any],
    ) -> None:
        """保存窗口标题、裁剪比例和截图函数。

        :param title: 窗口标题片段，包含即可。
        :param roi: 大字区域相对窗口的比例。
        :param probe: 提供 ``rect(title)``，返回屏幕矩形或 None。
        :param grab: 把屏幕矩形截成图像。
        """
        self.title = title
        self.roi = roi
        self.probe = probe
        self.grab = grab
        self._warned = False

    def read(self) -> Status:
        """截一张当前窗口并判断大字颜色。

        :return: ``pass`` / ``fail`` / ``running`` / ``unknown``。
        """
        try:
            rect = self.probe.rect(self.title)
            if rect is None:
                self._warn(f"找不到标题包含「{self.title}」的窗口")
                return "unknown"
            return classify_stamp(stamp_pixels(self.grab(rect), self.roi))
        except Exception as exc:  # noqa: BLE001 - 单次截图失败不能打断整轮压测
            self._warn(f"读取上位机结果失败: {exc}")
            return "unknown"

    def _warn(self, message: str) -> None:
        """同一种读取失败只在 info 之外警告一次，避免每 0.5 秒刷一行。

        :param message: 失败原因。
        """
        if self._warned:
            log.debug("%s", message)
            return
        self._warned = True
        log.warning("%s", message)


def wait_until_settled(
    read: Callable[[], str],
    sleep: Callable[[float], None],
    *,
    wait_s: float,
    poll_s: float,
    clock: Callable[[], float],
) -> str:
    """开始键入前，如果大字正停在「测试中」，先等它变成通过或不通过。

    这一轮不是本次键入触发的，结果不计入成功率。本来就不是「测试中」时立刻返回。
    ``sleep`` 必须让 ``clock`` 往前走，否则等待不会结束。

    :param read: 读取当前大字状态。
    :param sleep: 两次查看之间的等待。
    :param wait_s: 最长等待秒数。
    :param poll_s: 查看间隔秒数。
    :param clock: 单调时钟，测试里可以人为往前拨。
    :return: 当前或等到的状态。一直是「测试中」时返回 ``timeout``。
    """
    start = clock()
    status = read()
    if status != "running":
        return status
    log.info("上位机正在测试中，先等这一轮结束，不计入本次成功率")
    while True:
        if clock() - start >= wait_s:
            return "timeout"
        sleep(poll_s)
        status = read()
        if status in ("pass", "fail"):
            return status


def await_cycle_verdict(
    read: Callable[[], str],
    sleep: Callable[[float], None],
    *,
    index: int,
    total: int,
    arm_s: float,
    wait_s: float,
    poll_s: float,
    clock: Callable[[], float],
) -> CycleVerdict:
    """等这一轮先进入「测试中」，再等「通过」或「不通过」。

    回车后屏幕上可能还留着上一轮的绿色「通过」。在看到「测试中」之前，
    通过和不通过都忽略。看到结果立刻返回，不等满 ``wait_s``。
    ``sleep`` 必须让 ``clock`` 往前走。

    :param read: 读取当前大字状态。
    :param sleep: 两次查看之间的等待。
    :param index: 当前轮次，从 1 开始，只用于日志。
    :param total: 计划轮数。
    :param arm_s: 回车后多久还没进入「测试中」就放弃这一轮。
    :param wait_s: 进入「测试中」之后，最多再等多少秒。
    :param poll_s: 查看间隔秒数。
    :param clock: 单调时钟。
    :return: ``pass`` / ``fail`` / ``timeout`` / ``not_started``。
    """
    start = clock()
    armed_at: float | None = None
    last_log = start
    while True:
        status = read()
        now = clock()
        if armed_at is None:
            if status == "running":
                armed_at = now
                log.info("第 %d/%d 轮已进入测试中", index, total)
            elif now - start >= arm_s:
                return "not_started"
        elif status in ("pass", "fail"):
            return status
        elif now - armed_at >= wait_s:
            return "timeout"
        if now - last_log >= 10:
            log.info(
                "第 %d/%d 轮仍在等待，已 %.0f 秒，当前%s",
                index,
                total,
                now - start,
                _STATUS_LABEL.get(status, status),
            )
            last_log = now
        sleep(poll_s)


def _detail(verdict: CycleVerdict, cfg: TypeLoopConfig) -> str:
    """把这一轮结论写成报告里的一句说明。

    :param verdict: 等待函数返回的结论。
    :param cfg: 用来带上实际的等待上限。
    :return: 中文说明。
    """
    if verdict == "pass":
        return "右下角为通过"
    if verdict == "fail":
        return "右下角为不通过"
    if verdict == "timeout":
        return f"看到测试中后 {cfg.wait_s:g} 秒仍未出现通过或不通过"
    return f"{cfg.arm_timeout_s:g} 秒内没有进入测试中"


def run_type_loop(
    cfg: TypeLoopConfig,
    *,
    type_text: TextTyper,
    read_status: StatusReader,
    sleep: Callable[[float], None] = responsive_sleep,
    clock: Callable[[], float] | None = None,
    station: str = "barcode_type_loop",
) -> TypeLoopResult:
    """重复键入条码，按上位机大字统计通过率。

    :param cfg: 条码、轮数、结果区域和等待上限。
    :param type_text: 实际送键函数。测试里换成记录调用的假对象。
    :param read_status: 读取右下角大字。测试里换成按脚本返回的假对象。
    :param sleep: 可中断等待。主线程长等待应能响应停止信号，并且要让 ``clock`` 前进。
    :param clock: 单调时钟。空则用 ``time.monotonic``。
    :param station: 写入统计摘要的工位名。
    :return: 汇总和逐轮记录。用户中途停止时 ``summary.interrupted`` 为 True。
    """
    now = clock or time.monotonic
    summary = RunSummary(station=station, target_cycles=cfg.cycles)
    rows: list[dict[str, Any]] = []
    result = TypeLoopResult(summary=summary, rows=rows)
    started = now()
    log.info(
        "将键入 %s 并回车，共 %d 轮。进入「测试中」后，出现「通过」或「不通过」立刻下一轮，最长再等 %.0f 秒",
        cfg.text,
        cfg.cycles,
        cfg.wait_s,
    )
    completed = 0
    misses = 0
    try:
        if cfg.startup_delay_s > 0:
            log.info("请在 %.0f 秒内点击目标输入框，并保持右下角结果可见", cfg.startup_delay_s)
            sleep(cfg.startup_delay_s)
        settled = wait_until_settled(
            read_status.read, sleep, wait_s=cfg.wait_s, poll_s=cfg.poll_s, clock=now,
        )
        if settled == "timeout":
            summary.error = "开始时上位机一直显示测试中，未键入条码"
            log.error(summary.error)
        else:
            log.info("开始键入前，右下角是%s", _STATUS_LABEL.get(settled, settled))
        if summary.error is None:
            for index in range(1, cfg.cycles + 1):
                log.info("第 %d/%d 轮：键入并回车", index, cfg.cycles)
                cycle_started = now()
                type_text(cfg.text, interval=cfg.type_interval_s, enter=True)
                verdict = await_cycle_verdict(
                    read_status.read,
                    sleep,
                    index=index,
                    total=cfg.cycles,
                    arm_s=cfg.arm_timeout_s,
                    wait_s=cfg.wait_s,
                    poll_s=cfg.poll_s,
                    clock=now,
                )
                elapsed = now() - cycle_started
                passed = verdict == "pass"
                summary.executed += 1
                if passed:
                    summary.passed += 1
                    misses = 0
                else:
                    summary.failed += 1
                    misses = misses + 1 if verdict == "not_started" else 0
                detail = _detail(verdict, cfg)
                rows.append({
                    "cycle": index,
                    "verdict": "PASS" if passed else "FAIL",
                    "detail": detail,
                    "duration_s": round(elapsed, 1),
                })
                completed = index
                log.info(
                    "第 %d/%d 轮 %s（%.0f 秒）%s | 通过 %d / 失败 %d | 通过率 %.1f%%",
                    index,
                    cfg.cycles,
                    _VERDICT_LABEL[verdict],
                    elapsed,
                    detail,
                    summary.passed,
                    summary.failed,
                    summary.pass_rate,
                )
                if misses >= _START_MISS_LIMIT:
                    summary.aborted = True
                    summary.abort_reason = (
                        f"连续 {misses} 轮没有进入测试中。"
                        "请确认光标仍在输入框内，且皮皮熊 CCB 右下角没有被挡住。"
                    )
                    log.error(summary.abort_reason)
                    break
    except KeyboardInterrupt:
        summary.interrupted = True
        log.info("已停止，完成 %d/%d 轮", completed, cfg.cycles)
    else:
        if summary.aborted:
            log.info("提前结束，完成 %d/%d 轮", completed, cfg.cycles)
        elif summary.error is None:
            log.info("全部完成，共 %d 轮", completed)
    summary.duration_s = now() - started
    summary.ended_at = _dt.datetime.now().isoformat(timespec="seconds")
    log.info("\n%s", summary.render())
    return result


@register_tool("pctool.barcode_type_loop", description="条码键入循环")
def run(settings: AppSettings, ctx: RunContext) -> int:
    """读取工位配置，按 CCB 右下角大字循环键入并统计通过率。

    :param settings: 合并后的工位配置。
    :param ctx: 本次运行目录，日志和报告写在这里。
    :return: 全部通过为 0，有失败或无法开始为 1，用户中断为 130。
    """
    cfg = settings.section("type_loop", TypeLoopConfig, required=False)
    from ppx_testkit.apps.pctool.common import write_reports
    from ppx_testkit.core.gui.process import TitleWindowProbe
    from ppx_testkit.core.gui.screen import type_literal

    if ctx.run_dir is not None:
        log.info("日志目录: %s", ctx.run_dir)
    probe = TitleWindowProbe()
    if probe.rect(cfg.window_title) is None:
        raise WindowNotFoundError(
            f"找不到标题包含「{cfg.window_title}」的窗口。请先打开皮皮熊 CCB，并保持右下角结果可见。"
        )
    reader = WindowStampReader(cfg.window_title, cfg.verdict_roi, probe, grab_window)
    outcome = run_type_loop(
        cfg,
        type_text=type_literal,
        read_status=reader,
        sleep=responsive_sleep,
        station=settings.station,
    )
    write_reports(
        ctx,
        "report",
        title=f"{settings.station} 条码键入",
        rows=outcome.rows,
        summary=outcome.summary.to_dict(),
        columns=["cycle", "verdict", "detail", "duration_s"],
        summary_name="summary.json",
    )
    return outcome.summary.exit_code

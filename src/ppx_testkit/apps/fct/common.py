"""FCT 应用公共部分：像素颜色判定（纯函数）、取色探针、报告输出。"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ppx_testkit.core.gui.screen import is_green, is_red
from ppx_testkit.core.report.writers import write_csv, write_html
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.logger import RunContext

log = logging.getLogger(__name__)

Point = tuple[int, int]
PollDecision = Literal["PASS", "FAIL", "WAIT"]


class ColorScreen(Protocol):
    """FCT 流程需要的屏幕能力（core Screen 满足该协议，测试中可替换为假对象）。"""

    def click(self, point: Point, *, label: str = "") -> None: ...

    def type_text(self, text: str, *, interval: float = 0.02, enter: bool = False) -> None: ...

    def press(self, key: str) -> None: ...

    def pixel(self, point: Point) -> tuple[int, int, int]: ...

    def scan_colors(self, center: Point, drift: int = 0, step: int = 5, margin: int = 30) -> tuple[bool, bool]: ...

    def save_screenshot(self, path: Path) -> Path | None: ...


@dataclass(frozen=True)
class ScreenSettings:
    """pyautogui 全局参数。"""

    pause_s: float | None = None
    failsafe: bool = True


def build_screen(cfg: ScreenSettings) -> Any:
    from ppx_testkit.core.gui.screen import Screen

    return Screen(pause_s=cfg.pause_s, failsafe=cfg.failsafe)


def is_failsafe(exc: BaseException) -> bool:
    """pyautogui.FailSafeException（鼠标移到屏幕角落）必须向上抛出，交由执行器停止测试。"""
    return type(exc).__name__ == "FailSafeException"


# ------------------------------------------------------------ 颜色判定（纯函数）
@dataclass(frozen=True)
class PointSample:
    point: Point
    green: bool
    red: bool


@dataclass(frozen=True)
class ColorVerdict:
    all_green: bool
    has_red: bool
    not_green: list[Point] = field(default_factory=list)


def classify_points(samples: Sequence[PointSample]) -> ColorVerdict:
    """所有点位均为绿色才算全绿；非绿点位（含红色）全部记录。"""
    not_green: list[Point] = []
    has_red = False
    for s in samples:
        if s.green:
            continue
        not_green.append(s.point)
        if s.red:
            has_red = True
    return ColorVerdict(all_green=not not_green and bool(samples), has_red=has_red, not_green=not_green)


def poll_decision(verdict: ColorVerdict, *, fail_on_red: bool) -> PollDecision:
    """单次轮询的判定：全绿 PASS；``fail_on_red`` 时出现红色立即 FAIL；否则继续等待。"""
    if verdict.all_green:
        return "PASS"
    if fail_on_red and verdict.has_red:
        return "FAIL"
    return "WAIT"


def validate_points(points: Sequence[Point], name: str) -> None:
    if not points:
        raise ConfigError(f"{name} 至少需要配置一个坐标")
    for p in points:
        if p[0] < 0 or p[1] < 0:
            raise ConfigError(f"{name} 中存在负坐标: {p}")


def require_non_negative(values: Mapping[str, float]) -> None:
    for name, value in values.items():
        if value < 0:
            raise ConfigError(f"配置项 {name} 不能为负数，实际 {value}")


# ------------------------------------------------------------ 屏幕取色
def probe_point(screen: ColorScreen, point: Point, *, drift: int, step: int, margin: int) -> PointSample:
    """检测单个点位颜色。

    ``drift > 0`` 时在周边区域扫描；区域扫描异常则降级为单像素检测，
    单像素也失败时视为“非绿非红”（与旧脚本一致，不中断本轮测试）。
    """
    if drift > 0:
        try:
            green, red = screen.scan_colors(point, drift=drift, step=step, margin=margin)
            return PointSample(point, green, red)
        except Exception as exc:  # noqa: BLE001 - 截图/取色异常类型众多，降级处理
            if is_failsafe(exc):
                raise
            log.warning("区域像素扫描发生异常，尝试降级为单像素点检测。坐标: %s, 异常信息: %s", point, exc)
    try:
        rgb = screen.pixel(point)
    except Exception as exc:  # noqa: BLE001
        if is_failsafe(exc):
            raise
        log.error("单像素点检测失败，坐标: %s, 错误: %s", point, exc)
        return PointSample(point, False, False)
    return PointSample(point, is_green(rgb, margin), is_red(rgb, margin))


def probe_all(screen: ColorScreen, points: Sequence[Point], *, drift: int = 0, step: int = 5,
              margin: int = 30) -> ColorVerdict:
    return classify_points([probe_point(screen, p, drift=drift, step=step, margin=margin) for p in points])


# ------------------------------------------------------------ 产物
def save_failure_screenshot(screen: Any, ctx: RunContext | None, name: str) -> Path | None:
    """失败截图写入本次运行目录；截图失败只记录日志。"""
    if ctx is None:
        return None
    path = ctx.artifact(name)
    if path is None:
        return None
    try:
        return screen.save_screenshot(path)
    except Exception:  # noqa: BLE001 - 截图失败不能影响测试流程
        log.exception("保存失败截图异常: %s", path)
        return None


def write_reports(ctx: RunContext | None, name: str, *, title: str, rows: Sequence[Mapping[str, Any]],
                  summary: Mapping[str, Any], columns: Sequence[str] | None = None) -> None:
    """输出 CSV + HTML 明细报告到运行目录。"""
    if ctx is None or ctx.run_dir is None:
        return
    csv_path = ctx.artifact(f"{name}.csv")
    html_path = ctx.artifact(f"{name}.html")
    if csv_path is not None:
        write_csv(csv_path, rows, columns)
    if html_path is not None:
        write_html(html_path, title=title, summary=summary, rows=rows, columns=columns)

"""屏幕识别与鼠标键盘操作（pyautogui / OpenCV 延迟导入）。

颜色判定等纯函数可在无图形环境下单元测试；涉及屏幕的方法只在 Windows 工位上运行。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ppx_testkit.exceptions import ElementNotFoundError, GuiError

log = logging.getLogger(__name__)

Point = tuple[int, int]


def is_green(rgb: Sequence[int], margin: int = 30) -> bool:
    r, g, b = rgb[:3]
    return g > r + margin and g > b + margin


def is_red(rgb: Sequence[int], margin: int = 30) -> bool:
    r, g, b = rgb[:3]
    return r > g + margin and r > b + margin


def drift_points(center: Point, drift: int, step: int = 5) -> list[Point]:
    """以 center 为中心、±drift 范围内按 step 采样的点（含中心，中心优先）。"""
    cx, cy = center
    pts = [center]
    if drift <= 0:
        return pts
    for dx in range(-drift, drift + 1, step):
        for dy in range(-drift, drift + 1, step):
            if (dx, dy) != (0, 0):
                pts.append((cx + dx, cy + dy))
    return pts


def point_to_roi(point: Point, x_radius: int, y_radius: int) -> tuple[int, int, int, int]:
    x, y = point
    return x - x_radius, y - y_radius, x + x_radius, y + y_radius


def clip_roi(roi: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = roi
    return max(0, x1), max(0, y1), min(width, x2), min(height, y2)


@dataclass(frozen=True)
class GreenDetection:
    found: bool
    ratio: float
    max_area: int


class Screen:
    """对 pyautogui 的薄封装，便于在测试中替换。"""

    def __init__(self, *, pause_s: float | None = None, failsafe: bool = True) -> None:
        try:
            import pyautogui  # type: ignore[import-untyped]
        except Exception as exc:  # noqa: BLE001 - 无显示环境下 pyautogui 可能抛出各种异常
            raise GuiError(f"无法加载 pyautogui（需要图形桌面环境，pip install .[gui]）: {exc}") from exc
        self.pg = pyautogui
        self.pg.FAILSAFE = failsafe
        if pause_s is not None:
            self.pg.PAUSE = pause_s

    # ------------------------------------------------------------ 鼠标键盘
    def click(self, point: Point, *, label: str = "") -> None:
        log.info("点击 %s(%d, %d)", f"{label} " if label else "", *point)
        self.pg.click(*point)

    def type_text(self, text: str, *, interval: float = 0.02, enter: bool = False) -> None:
        log.info("输入文本: %s", text)
        self.pg.typewrite(text, interval=interval)
        if enter:
            self.pg.press("enter")

    def hotkey(self, *keys: str) -> None:
        self.pg.hotkey(*keys)

    def press(self, key: str) -> None:
        self.pg.press(key)

    def clear_focused_input(self, settle_s: float = 0.3) -> None:
        self.pg.hotkey("ctrl", "a")
        time.sleep(settle_s)
        self.pg.press("delete")
        time.sleep(settle_s)

    # ------------------------------------------------------------ 像素
    def pixel(self, point: Point) -> tuple[int, int, int]:
        rgb = self.pg.pixel(*point)
        return int(rgb[0]), int(rgb[1]), int(rgb[2])

    def scan_colors(self, center: Point, drift: int = 0, step: int = 5, margin: int = 30) -> tuple[bool, bool]:
        """在 center 周边扫描，返回 (是否有绿色, 是否有红色)。"""
        shot = self.pg.screenshot()
        w, h = shot.size
        green = red = False
        for x, y in drift_points(center, drift, step):
            if not (0 <= x < w and 0 <= y < h):
                continue
            rgb = shot.getpixel((x, y))
            green = green or is_green(rgb, margin)
            red = red or is_red(rgb, margin)
            if green and red:
                break
        return green, red

    def screenshot_array(self) -> Any:
        import numpy as np  # type: ignore[import-untyped]

        return np.array(self.pg.screenshot())

    def save_screenshot(self, path: Path) -> Path | None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.pg.screenshot(str(path))
            log.info("已保存截图: %s", path)
            return path
        except Exception:  # noqa: BLE001 - 截图失败不能影响测试流程
            log.exception("保存截图失败: %s", path)
            return None

    # ------------------------------------------------------------ 图像
    def _poll(self, timeout_s: float, poll_s: float, finder: Callable[[], Any], what: str) -> Any:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            try:
                found = finder()
                if found is not None:
                    log.info("已定位 %s，耗时 %.2fs", what, time.monotonic() - t0)
                    return found
            except self.pg.ImageNotFoundException:
                pass
            except OSError as exc:
                raise GuiError(f"读取特征图失败 {what}: {exc}") from exc
            time.sleep(poll_s)
        raise ElementNotFoundError(f"{timeout_s:.0f}s 内未找到 {what}")

    def wait_image(self, image: Path, timeout_s: float, *, confidence: float = 0.8, poll_s: float = 0.5) -> Any:
        return self._poll(timeout_s, poll_s, lambda: self.pg.locateOnScreen(str(image), confidence=confidence),
                          f"图像 {image.name}")

    def click_image(
        self,
        image: Path,
        timeout_s: float,
        *,
        offset: Point = (0, 0),
        confidence: float = 0.8,
        poll_s: float = 0.5,
    ) -> Point:
        center = self._poll(timeout_s, poll_s,
                            lambda: self.pg.locateCenterOnScreen(str(image), confidence=confidence),
                            f"图像 {image.name}")
        target = (int(center.x + offset[0]), int(center.y + offset[1]))
        self.click(target, label=image.stem)
        return target


def detect_green(image: Any, roi: tuple[int, int, int, int], hsv_lower: Sequence[int], hsv_upper: Sequence[int],
                 *, ratio_threshold: float | None = None, min_area: int | None = None) -> GreenDetection:
    """HSV 阈值 + 连通域面积判断 ROI 内是否存在绿色指示灯（P3 FCT 用）。"""
    import cv2  # type: ignore[import-untyped]
    import numpy as np  # type: ignore[import-untyped]

    h, w = image.shape[:2]
    x1, y1, x2, y2 = clip_roi(roi, w, h)
    if x2 <= x1 or y2 <= y1:
        return GreenDetection(False, 0.0, 0)
    crop = image[y1:y2, x1:x2]
    hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, np.array(hsv_lower, dtype=np.uint8), np.array(hsv_upper, dtype=np.uint8))
    total = mask.shape[0] * mask.shape[1]
    green_pixels = int(cv2.countNonZero(mask))
    ratio = green_pixels / total if total else 0.0
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    max_area = int(np.max(stats[1:, cv2.CC_STAT_AREA])) if n > 1 else 0
    found = True
    if min_area is not None:
        found = found and max_area >= min_area
    if ratio_threshold is not None:
        found = found and ratio >= ratio_threshold
    if min_area is None and ratio_threshold is None:
        found = green_pixels > 0
    return GreenDetection(found, ratio, max_area)

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


@dataclass(frozen=True)
class ScreenSettings:
    """pyautogui 全局参数，FCT / 总装 / W3 点击压测共用。"""

    pause_s: float | None = None
    failsafe: bool = True


def set_clipboard_text(text: str, retries: int = 5) -> None:
    """把 Unicode 文本写入系统剪贴板，供 Ctrl+V 原样粘贴。

    逐字输入在长时间压测后可能卡住 Ctrl/Shift，或被中文输入法在开头插入空格。
    剪贴板粘贴不会经过输入法。

    :param text: 要写入的原文。
    :param retries: 剪贴板被别的程序占用时的重试次数。
    :raises GuiError: 多次尝试后仍无法写入。
    """
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    cf_unicodetext = 13
    gmem_moveable = 0x0002
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = ctypes.c_int
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

    payload = text.encode("utf-16-le") + b"\x00\x00"
    last_error = "剪贴板被占用"
    for _ in range(max(1, retries)):
        if not user32.OpenClipboard(None):
            time.sleep(0.05)
            continue
        try:
            if not user32.EmptyClipboard():
                last_error = "无法清空剪贴板"
                continue
            handle = kernel32.GlobalAlloc(gmem_moveable, len(payload))
            if not handle:
                raise GuiError("剪贴板内存分配失败")
            locked = kernel32.GlobalLock(handle)
            if not locked:
                kernel32.GlobalFree(handle)
                raise GuiError("剪贴板内存锁定失败")
            try:
                ctypes.memmove(locked, payload, len(payload))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(cf_unicodetext, handle):
                kernel32.GlobalFree(handle)
                last_error = "写入剪贴板失败"
                continue
            return
        finally:
            user32.CloseClipboard()
    raise GuiError(last_error)


# 扫码框不认 Ctrl+V。加号在当前键盘布局上是 Shift + VK_OEM_PLUS，必须成对按下再抬起。
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D
_MODIFIER_VKS = (VK_SHIFT, VK_CONTROL, VK_MENU, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)
KeyEvent = tuple[str, int]


def key_chord(vk_scan: int) -> tuple[int, bool, bool, bool] | None:
    """把 VkKeyScan 的返回值拆成虚拟键和修饰键。

    :param vk_scan: ``VkKeyScanW`` 的返回值。低字节是虚拟键，0x100/0x200/0x400 分别表示 Shift/Ctrl/Alt。
    :return: ``(虚拟键, 是否 Shift, 是否 Ctrl, 是否 Alt)``。无法映射时返回 None。
    """
    if vk_scan < 0:
        return None
    vk = vk_scan & 0xFF
    if vk == 0xFF:
        return None
    return vk, bool(vk_scan & 0x100), bool(vk_scan & 0x200), bool(vk_scan & 0x400)


def chord_events(vk: int, shift: bool, ctrl: bool, alt: bool) -> list[KeyEvent]:
    """生成一次按键的按下和抬起。修饰键一定成对抬起，避免 Shift 卡住后把后面的数字打错。

    :param vk: 字符对应的虚拟键。
    :param shift: 是否需要 Shift。``+`` 和大写字母为 True。
    :param ctrl: 是否需要 Ctrl。
    :param alt: 是否需要 Alt。
    :return: ``("down", 虚拟键)`` 与 ``("up", 虚拟键)`` 的序列。
    """
    mods: list[int] = []
    if shift:
        mods.append(VK_SHIFT)
    if ctrl:
        mods.append(VK_CONTROL)
    if alt:
        mods.append(VK_MENU)
    sequence = [*mods, vk]
    return [("down", key) for key in sequence] + [("up", key) for key in reversed(sequence)]


def modifier_release_events() -> list[KeyEvent]:
    """只抬起可能卡住的修饰键，不额外按下。"""
    return [("up", vk) for vk in _MODIFIER_VKS]


def unicode_events(char: str) -> list[KeyEvent]:
    """键盘布局映射不到该字符时，改用 UTF-16 码元。

    :param char: 单个字符。
    :return: ``("unicode", 码元)`` 列表。
    """
    data = char.encode("utf-16-le")
    return [("unicode", int.from_bytes(data[i:i + 2], "little")) for i in range(0, len(data), 2)]


def _disable_foreground_ime() -> None:
    """关掉前台窗口的中文输入法。失败只记日志，不中断输入。"""
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        imm32 = ctypes.WinDLL("imm32", use_last_error=True)
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return
        imm32.ImmGetContext.argtypes = [ctypes.c_void_p]
        imm32.ImmGetContext.restype = ctypes.c_void_p
        himc = imm32.ImmGetContext(hwnd)
        if not himc:
            return
        try:
            imm32.ImmSetOpenStatus.argtypes = [ctypes.c_void_p, ctypes.c_int]
            imm32.ImmSetOpenStatus(himc, 0)
        finally:
            imm32.ImmReleaseContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            imm32.ImmReleaseContext(hwnd, himc)
    except Exception:  # noqa: BLE001 - 没有输入法上下文时继续逐键输入
        log.debug("关闭前台输入法失败", exc_info=True)


def _keybd_event(vk: int, *, up: bool) -> None:
    """用 keybd_event 按下或抬起虚拟键，并带上硬件扫描码。

    这套 Qt5 上位机不接收没有扫描码的 SendInput，扫码框里会一个字都没有。
    keybd_event 和真实键盘、pyautogui 的回车走的是同一条路径。

    :param vk: 虚拟键，例如 ``VK_RETURN`` 或字母键。
    :param up: True 为抬起，False 为按下。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.MapVirtualKeyW.restype = wintypes.UINT
    scan = int(user32.MapVirtualKeyW(vk, 0)) & 0xFF
    flags = 0x0002 if up else 0
    user32.keybd_event(vk & 0xFF, scan, flags, 0)


def _send_key_events(events: list[KeyEvent], *, gap_s: float = 0.0) -> None:
    """把按键事件送进当前焦点。按下和抬起之间可留一点间隔，方便 Qt 收到完整按键。

    :param events: ``chord_events`` 或 ``unicode_events`` 的结果。
    :param gap_s: 相邻两个事件之间的等待秒数。松开修饰键时传 0。
    :raises GuiError: 出现无法识别的事件。
    """
    if not events:
        return
    for kind, code in events:
        if kind == "down":
            _keybd_event(code, up=False)
        elif kind == "up":
            _keybd_event(code, up=True)
        elif kind == "unicode":
            _send_unicode_char(code)
        else:
            raise GuiError(f"未知按键事件: {kind}")
        if gap_s > 0:
            time.sleep(gap_s)


def _send_unicode_char(code: int) -> None:
    """键盘布局映射不到时，用 Unicode 扫描码补一个字符。

    :param code: UTF-16 码元。
    :raises GuiError: 系统没有接收该字符。
    """
    import ctypes
    from ctypes import wintypes

    key_up = 0x0002
    key_unicode = 0x0004
    extra = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KeyBdInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", extra),
        ]

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", extra),
        ]

    class HardwareInput(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [("ki", KeyBdInput), ("mi", MouseInput), ("hi", HardwareInput)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", InputUnion)]

    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    if ctypes.sizeof(Input) != expected:
        raise GuiError(f"按键结构体大小是 {ctypes.sizeof(Input)}，系统需要 {expected}")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    batch = (Input * 2)()
    for index, flags in enumerate((key_unicode, key_unicode | key_up)):
        batch[index].type = 1
        batch[index].u.ki.wScan = code
        batch[index].u.ki.dwFlags = flags
    sent = int(user32.SendInput(2, batch, ctypes.sizeof(Input)))
    if sent != 2:
        error = ctypes.get_last_error()
        raise GuiError(f"Unicode 输入失败，送出 {sent}/2，错误码 {error}")


def type_literal(text: str, *, interval: float = 0.03, enter: bool = False) -> None:
    """按当前键盘布局逐键输入，最后可以补一次回车。

    ``+`` 会先按下 Shift，敲对应键，再立刻松开，避免后面的数字变成符号。
    扫码框只把「一串按键 + 回车」当成一条条码，回车之后焦点才会进下一格。

    :param text: 要输入的原文。
    :param interval: 两个字符之间的等待秒数。
    :param enter: 全部字符送出后是否按回车。
    :raises GuiError: 出现无法送出的按键。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
    user32.VkKeyScanW.restype = ctypes.c_short
    _disable_foreground_ime()
    _send_key_events(modifier_release_events())
    for char in text:
        chord = key_chord(int(user32.VkKeyScanW(char)))
        events = chord_events(*chord) if chord is not None else unicode_events(char)
        _send_key_events(events, gap_s=0.012)
        if interval > 0:
            time.sleep(interval)
    _send_key_events(modifier_release_events())
    if enter:
        time.sleep(0.05)
        _send_key_events(chord_events(VK_RETURN, False, False, False), gap_s=0.012)


def is_failsafe(exc: BaseException) -> bool:
    """pyautogui 把鼠标移到角落时抛出的 FailSafeException，必须交给执行器停止测试。"""
    return type(exc).__name__ == "FailSafeException"


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

    def mouse_position(self) -> Point:
        """读取当前鼠标坐标。失败时抛出 GuiError。"""
        try:
            x, y = self.pg.position()
        except Exception as exc:  # noqa: BLE001 - pyautogui 在无桌面时异常类型不固定
            raise GuiError(f"读取鼠标位置失败: {exc}") from exc
        return int(x), int(y)

    def type_text(self, text: str, *, interval: float = 0.02, enter: bool = False) -> None:
        log.info("输入文本: %s", text)
        self.pg.typewrite(text, interval=interval)
        if enter:
            self.pg.press("enter")

    def hotkey(self, *keys: str) -> None:
        self.pg.hotkey(*keys)

    def press(self, key: str) -> None:
        self.pg.press(key)

    def release_modifiers(self) -> None:
        """松开可能卡住的 Ctrl、Shift、Alt，避免下一轮把车架号打错。"""
        for key in ("ctrl", "shift", "alt", "win"):
            try:
                self.pg.keyUp(key)
            except Exception:  # noqa: BLE001 - 某个修饰键本来就没按下
                log.debug("松开 %s 失败", key, exc_info=True)

    def clear_focused_input(self, settle_s: float = 0.05) -> None:
        """全选并删除当前输入框。做两遍，避免第一次焦点还没落上时留下旧字符。

        :param settle_s: 全选和删除之间的等待秒数。
        """
        for _ in range(2):
            self.pg.hotkey("ctrl", "a")
            time.sleep(settle_s)
            self.pg.press("delete")
            time.sleep(settle_s)

    def replace_text(
        self,
        text: str,
        *,
        enter: bool = False,
        clear: bool = True,
        literal: bool = False,
        interval: float = 0.03,
    ) -> None:
        """清空当前输入框后写入原文。

        ``literal`` 为 False 时粘贴剪贴板，车架号走这条路径，避免输入法插入空格。
        ``literal`` 为 True 时逐键输入。扫码框不接受 Ctrl+V，条码里的 ``+`` 也必须按 Shift 组合键送出。

        :param text: 要写入的内容，调用方应去掉首尾空白。
        :param enter: 写入后是否按回车。
        :param clear: 是否先清空输入框。重复录入时必须清空，否则会把上一次的字符留在前面。
        :param literal: True 时逐键输入，False 时粘贴。
        :param interval: 逐键输入时两个字符之间的等待秒数。粘贴时忽略。
        """
        self.release_modifiers()
        if clear:
            self.clear_focused_input()
        if literal:
            log.info("逐键输入: %s", text)
            type_literal(text, interval=interval, enter=enter)
            return
        log.info("粘贴文本: %s", text)
        try:
            set_clipboard_text(text)
        except GuiError:
            log.exception("剪贴板不可用，改为逐字输入: %s", text)
            self.type_text(text, enter=enter)
            return
        # 粘贴前再全选一次，把残留的空格也替换掉。
        self.pg.hotkey("ctrl", "a")
        time.sleep(0.05)
        self.pg.hotkey("ctrl", "v")
        if enter:
            time.sleep(0.05)
            self.pg.press("enter")

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

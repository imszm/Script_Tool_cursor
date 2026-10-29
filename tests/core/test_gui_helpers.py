from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from ppx_testkit.apps.pctool.common import resolve_click
from ppx_testkit.apps.pctool.w3_pctool_stress import ClickAction, W3PcToolStressCycle, W3StressConfig
from ppx_testkit.core.gui.process import TitleWindowProbe
from ppx_testkit.core.gui.screen import (
    VK_SHIFT,
    Screen,
    chord_events,
    client_to_screen,
    clip_roi,
    drift_points,
    is_green,
    is_red,
    key_chord,
    point_to_roi,
    screen_to_client,
    unicode_events,
)
from ppx_testkit.core.gui.window import normalize_window
from ppx_testkit.exceptions import GuiError


def test_color_verdicts() -> None:
    assert is_green((10, 200, 30)) and not is_green((10, 50, 30))
    assert is_red((220, 20, 10)) and not is_red((100, 90, 80))
    assert not is_green((0, 25, 0))
    assert is_green((0, 25, 0), margin=20)


def test_drift_points() -> None:
    assert drift_points((10, 10), 0) == [(10, 10)]
    pts = drift_points((10, 10), 5, step=5)
    assert pts[0] == (10, 10) and len(pts) == 9 and (5, 15) in pts and pts.count((10, 10)) == 1


def test_plus_chord_presses_shift_and_releases_it() -> None:
    # 本机 VkKeyScan('+') 的结果：VK_OEM_PLUS(187) 加 Shift。
    chord = key_chord(187 | 0x100)
    assert chord == (187, True, False, False)
    assert chord_events(*chord) == [
        ("down", VK_SHIFT),
        ("down", 187),
        ("up", 187),
        ("up", VK_SHIFT),
    ]
    assert key_chord(-1) is None
    assert unicode_events("+") == [("unicode", 0x2B)]


def test_titles_lists_visible_top_level_names(monkeypatch: pytest.MonkeyPatch) -> None:
    class Element:
        def __init__(self, name: str) -> None:
            self.name = name

    def find_elements(**kwargs: object) -> list[Element]:
        assert kwargs.get("visible_only") is True
        return [Element("PPX PCTOOL V2.5.00"), Element(""), Element("PPX PCTOOL V2.5.00"), Element("Cursor")]

    findwindows = types.ModuleType("pywinauto.findwindows")
    findwindows.find_elements = find_elements  # type: ignore[attr-defined]
    package = types.ModuleType("pywinauto")
    package.findwindows = findwindows  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywinauto", package)
    monkeypatch.setitem(sys.modules, "pywinauto.findwindows", findwindows)
    assert TitleWindowProbe().titles() == ["PPX PCTOOL V2.5.00", "Cursor"]


def test_client_origin_round_trip() -> None:
    assert client_to_screen((100, 200), (10, 20)) == (110, 220)
    assert screen_to_client((100, 200), (110, 220)) == (10, 20)
    assert resolve_click((10, 20), (100, 200), space="screen") == (10, 20)
    assert resolve_click((10, 20), None, space="client") == (10, 20)
    assert resolve_click((10, 20), (100, 200), space="client") == (110, 220)


class _Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    def width(self) -> int:
        return self.right - self.left

    def height(self) -> int:
        return self.bottom - self.top


class _Window:
    def __init__(self) -> None:
        self.minimized = True
        self.focused = False
        self.moved: tuple[int, int, int, int] | None = None
        self.outer = _Rect(50, 60, 850, 660)

    def is_minimized(self) -> bool:
        return self.minimized

    def restore(self) -> None:
        self.minimized = False

    def rectangle(self) -> _Rect:
        return self.outer

    def move_window(self, left: int, top: int, width: int, height: int, repaint: bool = True) -> None:
        self.moved = (left, top, width, height)
        self.outer = _Rect(left, top, left + width, top + height)

    def set_focus(self) -> None:
        self.focused = True

    def client_to_screen(self, point: tuple[int, int]) -> tuple[int, int]:
        return self.outer.left + 8 + point[0], self.outer.top + 30 + point[1]

    def client_rect(self) -> _Rect:
        return _Rect(0, 0, 400, 300)


def test_normalize_window_restores_resizes_and_focuses() -> None:
    window = _Window()
    box = normalize_window(window, size=(640, 480))
    assert window.minimized is False and window.focused is True
    assert window.moved == (50, 60, 640, 480)
    assert box == (58, 90, 458, 390)


def test_w3_screen_coordinates_stay_absolute() -> None:
    cfg = W3StressConfig(
        window_title="W3 PCTOOL",
        actions=[ClickAction(point=(1685, 324), label="开始升级", wait_s=0)],
        cycles=1,
        coordinate_space="screen",
    )
    screen_clicks: list[tuple[int, int]] = []

    class _Screen:
        def click(self, point: tuple[int, int], *, label: str = "") -> None:
            screen_clicks.append(point)

        def hotkey(self, *_keys: str) -> None:
            return None

        def press(self, _key: str) -> None:
            return None

        def type_text(self, _text: str, *, interval: float = 0.02, enter: bool = False) -> None:
            return None

        def save_screenshot(self, _path: object) -> None:
            return None

    class _Probe:
        def exists(self, _title: str) -> bool:
            return True

        def prepare(self, _title: str, *, size: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
            return (10, 20, 800, 600)

    class _Process:
        def kill_matching(self, _keyword: str, _timeout_s: float) -> list[str]:
            return []

        def launch(self, _program: str) -> None:
            return None

    cycle = W3PcToolStressCycle(cfg, _Screen(), _Probe(), _Process(), sleep=lambda _s: None)  # type: ignore[arg-type]
    cycle._client_origin = cycle._prepare_window()
    cycle._perform(cfg.actions[0])
    assert screen_clicks == [(1685, 324)]

    client_cfg = W3StressConfig(
        window_title="W3 PCTOOL",
        actions=[ClickAction(point=(15, 25), label="相对", wait_s=0)],
        cycles=1,
        coordinate_space="client",
    )
    screen_clicks.clear()
    client_cycle = W3PcToolStressCycle(client_cfg, _Screen(), _Probe(), _Process(), sleep=lambda _s: None)  # type: ignore[arg-type]
    client_cycle._client_origin = client_cycle._prepare_window()
    client_cycle._perform(client_cfg.actions[0])
    assert screen_clicks == [(25, 45)]


def test_roi_helpers() -> None:
    assert point_to_roi((50, 40), 10, 5) == (40, 35, 60, 45)
    assert clip_roi((-5, -5, 2000, 30), 1920, 1080) == (0, 0, 1920, 30)


@pytest.mark.skipif(importlib.util.find_spec("pyautogui") is not None, reason="已安装 pyautogui")
def test_screen_without_pyautogui_raises_gui_error() -> None:
    with pytest.raises(GuiError, match="pyautogui"):
        Screen()

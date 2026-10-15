from __future__ import annotations

import importlib.util

import pytest

from ppx_testkit.core.gui.screen import Screen, clip_roi, drift_points, is_green, is_red, point_to_roi
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


def test_roi_helpers() -> None:
    assert point_to_roi((50, 40), 10, 5) == (40, 35, 60, 45)
    assert clip_roi((-5, -5, 2000, 30), 1920, 1080) == (0, 0, 1920, 30)


@pytest.mark.skipif(importlib.util.find_spec("pyautogui") is not None, reason="已安装 pyautogui")
def test_screen_without_pyautogui_raises_gui_error() -> None:
    with pytest.raises(GuiError, match="pyautogui"):
        Screen()

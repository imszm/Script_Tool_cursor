from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

try:
    from PIL import ImageGrab  # type: ignore
except Exception:  # pragma: no cover
    ImageGrab = None  # type: ignore


@dataclass(frozen=True)
class Rgb:
    r: int
    g: int
    b: int


def grab_screen():
    if ImageGrab is None:
        raise RuntimeError("Pillow is required. Please install: pip install pillow")
    return ImageGrab.grab()


def detect_pass_fail_by_points(points: Iterable[tuple[int, int]]) -> str | None:
    """
    Return: 'PASS' | 'FAIL' | None
    Heuristic consistent with legacy scripts.
    """
    screen = grab_screen()
    for (x, y) in points:
        r, g, b = screen.getpixel((int(x), int(y)))
        if r > 200 and g < 100 and b < 100:
            return "FAIL"
        if g > 140 and g > r + 30 and g > b + 30:
            return "PASS"
    return None


def detect_pass_fail_fuzzy(points: Iterable[tuple[int, int]], search_radius: int = 10, step: int = 2) -> str | None:
    screen = grab_screen()
    width, height = screen.size
    for (cx, cy) in points:
        for x in range(int(cx) - search_radius, int(cx) + search_radius, int(step)):
            for y in range(int(cy) - search_radius, int(cy) + search_radius, int(step)):
                if x < 0 or x >= width or y < 0 or y >= height:
                    continue
                r, g, b = screen.getpixel((int(x), int(y)))
                if r > 200 and g < 100 and b < 100:
                    return "FAIL"
                if g > 140 and g > r + 30 and g > b + 30:
                    return "PASS"
    return None


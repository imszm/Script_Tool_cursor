from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from pywinauto.application import Application  # type: ignore
except Exception:  # pragma: no cover
    Application = None  # type: ignore


@dataclass(frozen=True)
class UiAppHandle:
    app: Any
    win: Any


def connect_window_by_title(title: str, timeout_s: float = 10.0) -> UiAppHandle:
    if Application is None:
        raise RuntimeError("pywinauto is required. Please install: pip install pywinauto")
    app = Application(backend="uia").connect(title=title, timeout=float(timeout_s))
    win = app.window(title=title)
    return UiAppHandle(app=app, win=win)


def connect_window_by_title_re(title_re: str, timeout_s: float = 10.0) -> UiAppHandle:
    if Application is None:
        raise RuntimeError("pywinauto is required. Please install: pip install pywinauto")
    app = Application(backend="uia").connect(title_re=title_re, timeout=float(timeout_s))
    win = app.window(title_re=title_re)
    return UiAppHandle(app=app, win=win)


def screenshot(win: Any, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    win.capture_as_image().save(str(out_path))
    return out_path


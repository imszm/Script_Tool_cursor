"""操作员提示弹窗（Windows MessageBox，其他平台降级为日志）。"""

from __future__ import annotations

import datetime
import logging
import threading

log = logging.getLogger(__name__)

try:  # pragma: no cover - 仅 Windows 可用
    import win32api  # type: ignore[import-not-found]
    import win32con  # type: ignore[import-not-found]

    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


def show_message(message: str, title: str = "提示", *, blocking: bool = True, error: bool = False) -> None:
    """弹出提示框。blocking=False 时在守护线程中弹窗，不阻塞测试流程。"""
    level = logging.ERROR if error else logging.INFO
    log.log(level, "[弹窗][%s] %s", title, message)
    if not HAS_WIN32:
        return

    def _show() -> None:
        try:
            stamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
            icon = win32con.MB_ICONERROR if error else win32con.MB_ICONINFORMATION
            win32api.MessageBox(0, str(message), f"{title} {stamp}", icon)
        except Exception:  # noqa: BLE001 - 弹窗失败不能影响测试主流程
            log.exception("Win32 弹窗调用失败")

    if blocking:
        _show()
    else:
        threading.Thread(target=_show, name="notify", daemon=True).start()

"""Windows 应用窗口自动化（pywinauto 延迟导入）。"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from ppx_testkit.exceptions import ElementNotFoundError, GuiError, WindowNotFoundError

log = logging.getLogger(__name__)


def _bounds(wrapper: Any) -> Any:
    """读取窗口外框。``rectangle`` 可能是方法或属性。"""
    rect = wrapper.rectangle
    return rect() if callable(rect) else rect


def normalize_window(wrapper: Any, *, size: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """取消最小化，可选固定外框尺寸，再置前。返回客户区的屏幕矩形。

    左上角保持不动，只在传入 ``size`` 时改宽高。读不到客户区时退回窗口外框。
    激活失败只记日志，仍返回矩形，避免一次焦点切换失败就中断点击。

    :param wrapper: pywinauto 窗口包装，至少要能读矩形。
    :param size: ``(宽, 高)`` 像素。空则不改尺寸。
    :return: ``(left, top, right, bottom)``，客户区映射到屏幕坐标。
    :raises GuiError: 要求固定尺寸但宽或高不是正数。
    """
    is_minimized = getattr(wrapper, "is_minimized", None)
    if callable(is_minimized) and is_minimized():
        restore = getattr(wrapper, "restore", None)
        if callable(restore):
            try:
                restore()
                log.info("窗口已从最小化恢复")
            except Exception as exc:  # noqa: BLE001 - 恢复失败时仍尝试激活
                log.warning("恢复最小化窗口失败: %s", exc)
    if size is not None:
        width, height = size
        if width <= 0 or height <= 0:
            raise GuiError(f"窗口尺寸必须为正数，实际 {size}")
        rect = _bounds(wrapper)
        move = getattr(wrapper, "move_window", None)
        if callable(move):
            try:
                move(int(rect.left), int(rect.top), int(width), int(height), repaint=True)
                log.info("窗口尺寸已固定为 %d x %d", width, height)
            except Exception as exc:  # noqa: BLE001 - 尺寸固定失败时继续用当前外框
                log.warning("固定窗口尺寸失败: %s", exc)
    focus = getattr(wrapper, "set_focus", None)
    if callable(focus):
        try:
            focus()
        except Exception as exc:  # noqa: BLE001 - 前台策略可能拒绝
            log.warning("激活窗口失败: %s", exc)
    return client_box(wrapper)


def client_box(wrapper: Any) -> tuple[int, int, int, int]:
    """客户区左上角映射到屏幕坐标后的矩形。

    :param wrapper: 提供 ``client_to_screen`` / ``client_rect``，或至少提供 ``rectangle``。
    :return: ``(left, top, right, bottom)``。
    :raises GuiError: 两种矩形都读不到。
    """
    try:
        origin = wrapper.client_to_screen((0, 0))
        client = wrapper.client_rect()
        width = client.width() if callable(getattr(client, "width", None)) else int(client.right - client.left)
        height = client.height() if callable(getattr(client, "height", None)) else int(client.bottom - client.top)
        left, top = int(origin[0]), int(origin[1])
        return left, top, left + int(width), top + int(height)
    except Exception as exc:  # noqa: BLE001 - 退回窗口外框
        log.debug("读取客户区失败，改用窗口外框: %s", exc)
    try:
        rect = _bounds(wrapper)
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception as exc:  # noqa: BLE001
        raise GuiError(f"读取窗口位置失败: {exc}") from exc


def _pywinauto() -> Any:
    try:
        import pywinauto  # type: ignore[import-not-found]
    except ImportError as exc:
        raise GuiError("需要 Windows 环境并安装 pywinauto（pip install .[win]）") from exc
    return pywinauto


class AppWindow:
    def __init__(self, *, title: str | None = None, title_re: str | None = None, backend: str = "uia",
                 connect_timeout_s: float = 10.0) -> None:
        if not (title or title_re):
            raise GuiError("AppWindow 需要 title 或 title_re")
        self.title = title
        self.title_re = title_re
        self.backend = backend
        self.connect_timeout_s = connect_timeout_s
        self.app: Any = None
        self.win: Any = None

    def _criteria(self) -> dict[str, Any]:
        return {"title": self.title} if self.title else {"title_re": self.title_re}

    def connect(self) -> AppWindow:
        pw = _pywinauto()
        try:
            self.app = pw.Application(backend=self.backend).connect(timeout=self.connect_timeout_s, **self._criteria())
            self.win = self.app.window(**self._criteria())
            self.win.wait("visible", timeout=self.connect_timeout_s)
        except Exception as exc:  # noqa: BLE001 - pywinauto 异常类型众多
            raise WindowNotFoundError(f"找不到窗口 {self._criteria()}: {exc}") from exc
        log.info("已连接窗口 %s", self._criteria())
        return self

    def is_alive(self) -> bool:
        try:
            return bool(self.win is not None and self.win.exists(timeout=1))
        except Exception:  # noqa: BLE001
            return False

    def control(self, auto_id: str, control_type: str | None = None) -> Any:
        if self.win is None:
            raise WindowNotFoundError("窗口尚未连接")
        kwargs: dict[str, Any] = {"auto_id": auto_id}
        if control_type:
            kwargs["control_type"] = control_type
        ctrl = self.win.child_window(**kwargs)
        try:
            ctrl.wait("exists", timeout=5)
        except Exception as exc:  # noqa: BLE001
            raise ElementNotFoundError(f"找不到控件 auto_id={auto_id}: {exc}") from exc
        return ctrl

    def prepare(self, *, size: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
        """连接之后：恢复最小化、可选固定尺寸、置前，并返回客户区屏幕矩形。

        :param size: 要固定的外框宽高。空则不改尺寸。
        :return: 客户区 ``(left, top, right, bottom)``。
        :raises WindowNotFoundError: 尚未连接。
        """
        if self.win is None:
            raise WindowNotFoundError("窗口尚未连接")
        target = self.win
        wrapper = getattr(target, "wrapper_object", None)
        if callable(wrapper):
            target = wrapper()
        return normalize_window(target, size=size)

    def click(self, auto_id: str) -> None:
        try:
            self.control(auto_id).click_input()
            log.info("点击控件 %s", auto_id)
        except ElementNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GuiError(f"点击控件 {auto_id} 失败: {exc}") from exc

    def set_text(self, auto_id: str, text: str) -> None:
        try:
            ctrl = self.control(auto_id)
            ctrl.set_edit_text(text)
            log.info("控件 %s 输入: %s", auto_id, text)
        except ElementNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GuiError(f"控件 {auto_id} 输入失败: {exc}") from exc

    def text(self, auto_id: str) -> str:
        ctrl = self.control(auto_id)
        try:
            return str(ctrl.window_text() or ctrl.get_value())
        except Exception:  # noqa: BLE001 - 部分控件不支持 get_value
            return str(ctrl.window_text())

    def confirm_dialog(self, title_re: str, button: str, timeout_s: float = 5.0) -> bool:
        if self.app is None:
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                dlg = self.app.window(title_re=title_re)
                if dlg.exists(timeout=0.5):
                    dlg.child_window(title=button).click_input()
                    log.info("已确认弹窗 %s -> %s", title_re, button)
                    return True
            except Exception as exc:  # noqa: BLE001
                log.debug("查找弹窗 %s 失败: %s", title_re, exc)
            time.sleep(0.3)
        return False

    def capture(self, path: Path) -> Path | None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.win.capture_as_image().save(str(path))
            log.info("窗口截图已保存: %s", path)
            return path
        except Exception:  # noqa: BLE001
            log.exception("窗口截图失败: %s", path)
            return None

    def dump_controls(self) -> str:
        if self.win is None:
            raise WindowNotFoundError("窗口尚未连接")
        lines = []
        for ctrl in self.win.descendants():
            info = ctrl.element_info
            lines.append(f"{info.control_type}\ttext={info.name!r}\tauto_id={info.automation_id!r}")
        return "\n".join(lines)


def launch_process(path: Path, cwd: Path | None = None) -> subprocess.Popen[bytes]:
    if not path.exists():
        raise GuiError(f"程序不存在: {path}")
    log.info("启动程序: %s", path)
    return subprocess.Popen([str(path)], cwd=str(cwd or path.parent))


def process_running(name: str) -> bool:
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError as exc:
        raise GuiError("需要安装 psutil（pip install .[gui]）") from exc
    target = name.lower()
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() == target:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False

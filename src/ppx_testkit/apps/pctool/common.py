"""PC 工具应用公共部分：窗口配置、控件定位、对 core AppWindow 的补充封装、报告输出。

core :class:`AppWindow` 只支持按 ``auto_id`` 查找控件，且主窗口与连接条件必须相同；
旧脚本还需要：按标题查找按钮（如“开始”）、UIA Invoke 点击、读取 Edit 的 value、
以及“按进程标题连接、按另一个标题取主窗口”。这些在 :class:`PcToolWindow` 中补齐。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ppx_testkit.core.report.writers import write_csv, write_html
from ppx_testkit.exceptions import ConfigError, ElementNotFoundError, GuiError, WindowNotFoundError
from ppx_testkit.logger import RunContext

log = logging.getLogger(__name__)


def is_failsafe(exc: BaseException) -> bool:
    return type(exc).__name__ == "FailSafeException"


@dataclass(frozen=True)
class ControlRef:
    """控件定位条件：auto_id 与 title 至少其一。"""

    auto_id: str | None = None
    title: str | None = None
    control_type: str | None = None

    def __post_init__(self) -> None:
        if not (self.auto_id or self.title):
            raise ConfigError("控件定位需要 auto_id 或 title")

    def criteria(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.auto_id:
            out["auto_id"] = self.auto_id
        if self.title:
            out["title"] = self.title
        if self.control_type:
            out["control_type"] = self.control_type
        return out

    def describe(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.criteria().items())


@dataclass(frozen=True)
class WindowConfig:
    title: str | None = None
    title_re: str | None = None
    window_title: str | None = None
    window_title_re: str | None = None
    backend: str = "uia"
    connect_timeout_s: float = 10.0
    control_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if not (self.title or self.title_re):
            raise ConfigError("window.title 与 window.title_re 至少配置一个")
        if self.connect_timeout_s <= 0 or self.control_timeout_s <= 0:
            raise ConfigError("window.connect_timeout_s / control_timeout_s 必须 > 0")

    def window_criteria(self) -> dict[str, str] | None:
        if self.window_title:
            return {"title": self.window_title}
        if self.window_title_re:
            return {"title_re": self.window_title_re}
        return None

    def describe(self) -> str:
        return self.title or self.title_re or ""


class ToolWindow(Protocol):
    """压测流程需要的窗口能力；测试中用假对象替换。"""

    def connect(self) -> Any: ...

    def is_alive(self) -> bool: ...

    def click(self, ref: ControlRef, *, invoke: bool = False) -> None: ...

    def set_text(self, ref: ControlRef, text: str) -> None: ...

    def read_value(self, ref: ControlRef) -> str: ...

    def confirm_dialog(self, title_re: str, button: str, timeout_s: float = 5.0) -> bool: ...

    def capture(self, path: Path) -> Path | None: ...

    def dump_controls(self) -> str: ...


class PcToolWindow:
    """基于 core AppWindow 的窗口封装（pywinauto 仅在 connect 时延迟导入）。"""

    def __init__(self, cfg: WindowConfig) -> None:
        from ppx_testkit.core.gui.window import AppWindow

        self.cfg = cfg
        self._aw = AppWindow(title=cfg.title, title_re=cfg.title_re, backend=cfg.backend,
                             connect_timeout_s=cfg.connect_timeout_s)

    def connect(self) -> PcToolWindow:
        self._aw.connect()
        criteria = self.cfg.window_criteria()
        if criteria:
            try:
                win = self._aw.app.window(**criteria)
                win.wait("visible", timeout=self.cfg.connect_timeout_s)
            except Exception as exc:  # noqa: BLE001 - pywinauto 异常类型众多
                raise WindowNotFoundError(f"找不到主窗口 {criteria}: {exc}") from exc
            self._aw.win = win
            log.info("已切换到主窗口 %s", criteria)
        return self

    def is_alive(self) -> bool:
        return self._aw.is_alive()

    def _control(self, ref: ControlRef) -> Any:
        if self._aw.win is None:
            raise WindowNotFoundError("窗口尚未连接")
        try:
            ctrl = self._aw.win.child_window(**ref.criteria())
            ctrl.wait("exists", timeout=self.cfg.control_timeout_s)
        except Exception as exc:  # noqa: BLE001
            raise ElementNotFoundError(f"找不到控件 {ref.describe()}: {exc}") from exc
        return ctrl

    def click(self, ref: ControlRef, *, invoke: bool = False) -> None:
        ctrl = self._control(ref)
        try:
            if invoke:
                ctrl.click()
            else:
                ctrl.click_input()
        except Exception as exc:  # noqa: BLE001
            raise GuiError(f"点击控件 {ref.describe()} 失败: {exc}") from exc
        log.info("点击控件 %s", ref.describe())

    def set_text(self, ref: ControlRef, text: str) -> None:
        ctrl = self._control(ref)
        try:
            ctrl.set_edit_text(text)
        except Exception as exc:  # noqa: BLE001
            raise GuiError(f"控件 {ref.describe()} 输入失败: {exc}") from exc
        log.info("控件 %s 输入: %s", ref.describe(), text)

    def read_value(self, ref: ControlRef) -> str:
        """优先读取 UIA Value（Edit 的实际内容），不支持时退回 window_text。"""
        ctrl = self._control(ref)
        try:
            return str(ctrl.get_value() or "")
        except Exception:  # noqa: BLE001 - 部分控件不支持 ValuePattern
            try:
                return str(ctrl.window_text() or "")
            except Exception as exc:  # noqa: BLE001
                raise GuiError(f"读取控件 {ref.describe()} 内容失败: {exc}") from exc

    def confirm_dialog(self, title_re: str, button: str, timeout_s: float = 5.0) -> bool:
        return self._aw.confirm_dialog(title_re, button, timeout_s)

    def capture(self, path: Path) -> Path | None:
        return self._aw.capture(path)

    def dump_controls(self) -> str:
        return self._aw.dump_controls()


def capture_window(window: Any, ctx: RunContext | None, name: str) -> Path | None:
    """窗口截图写入运行目录；失败只记录日志。"""
    if ctx is None:
        return None
    path = ctx.artifact(name)
    if path is None:
        return None
    try:
        return window.capture(path)
    except Exception:  # noqa: BLE001 - 截图失败不能影响测试流程
        log.exception("窗口截图异常: %s", path)
        return None


def write_reports(ctx: RunContext | None, name: str, *, title: str, rows: Sequence[Mapping[str, Any]],
                  summary: Mapping[str, Any], columns: Sequence[str] | None = None, html: bool = True) -> None:
    if ctx is None or ctx.run_dir is None:
        return
    csv_path = ctx.artifact(f"{name}.csv")
    if csv_path is not None:
        write_csv(csv_path, rows, columns)
    html_path = ctx.artifact(f"{name}.html")
    if html and html_path is not None:
        write_html(html_path, title=title, summary=summary, rows=rows, columns=columns)

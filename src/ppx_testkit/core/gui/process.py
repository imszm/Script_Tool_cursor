"""窗口是否存在、以及按名称结束/启动进程。

pywinauto 与 psutil 都延迟导入，没有图形环境时核心包仍可导入。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from ppx_testkit.exceptions import GuiError

log = logging.getLogger(__name__)


class TitleWindowProbe:
    """按标题查找顶层窗口：是否存在、矩形、以及拉到前台。"""

    def exists(self, title: str) -> bool:
        """标题中包含 ``title`` 时返回 True。枚举失败抛出 GuiError。

        :param title: 窗口标题片段。
        :return: 至少有一个可见顶层窗口匹配时为 True。
        """
        return bool(self._find(title))

    def rect(self, title: str, *, exact: bool = False) -> tuple[int, int, int, int] | None:
        """返回第一个匹配窗口的屏幕矩形。

        :param title: 窗口标题；``exact`` 为 False 时按包含匹配。
        :param exact: True 时标题必须全文一致。
        :return: ``(left, top, right, bottom)``。找不到返回 None。枚举失败抛出 GuiError。
        """
        elements = self._find(title, exact=exact)
        if not elements:
            return None
        if len(elements) > 1:
            log.warning("标题 %r 匹配到 %d 个窗口，使用第一个", title, len(elements))
        try:
            bounds = elements[0].rectangle
        except Exception as exc:  # noqa: BLE001 - 不同后端的矩形属性可能抛出
            raise GuiError(f"读取窗口 {title!r} 位置失败: {exc}") from exc
        return int(bounds.left), int(bounds.top), int(bounds.right), int(bounds.bottom)

    def activate(self, title: str) -> bool:
        """把匹配窗口带到前台，让随后的键盘输入进这个窗口。

        :param title: 窗口标题片段。
        :return: 是否已调用 set_focus。失败只记日志并返回 False，不中断测试。
        """
        try:
            elements = self._find(title)
        except GuiError as exc:
            log.warning("激活窗口 %s 失败: %s", title, exc)
            return False
        if not elements:
            log.warning("找不到窗口，无法激活: %s", title)
            return False
        try:
            from pywinauto.controls.hwndwrapper import HwndWrapper  # type: ignore[import-not-found]

            HwndWrapper(elements[0].handle).set_focus()
        except Exception as exc:  # noqa: BLE001 - 前台切换受系统焦点策略影响
            log.warning("激活窗口 %s 失败: %s", title, exc)
            return False
        log.info("已激活窗口: %s", title)
        return True

    def _find(self, title: str, *, exact: bool = False) -> list[Any]:
        """枚举可见顶层窗口。

        :param title: 标题全文或片段。
        :param exact: True 时使用全文匹配。
        :return: pywinauto 元素列表。找不到时为空列表。枚举失败抛出 GuiError。
        """
        try:
            from pywinauto import findwindows  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GuiError("需要 Windows 环境并安装 pywinauto（pip install .[win]）") from exc
        pattern = f"^{re.escape(title)}$" if exact else f".*{re.escape(title)}.*"
        try:
            return list(findwindows.find_elements(title_re=pattern, top_level_only=True))
        except Exception as exc:  # noqa: BLE001 - 枚举窗口失败统一转换
            raise GuiError(f"枚举窗口失败: {exc}") from exc


class PsutilProcessControl:
    """按进程名关键字结束进程，并用系统方式重新启动程序。"""

    def kill_matching(self, keyword: str, timeout_s: float) -> list[str]:
        """结束名称包含 keyword 的进程，返回仍未退出的进程名。"""
        try:
            import psutil  # type: ignore[import-untyped]
        except ImportError as exc:
            raise GuiError("需要安装 psutil（pip install .[gui]）") from exc
        for proc in psutil.process_iter(["pid", "name"]):
            name = proc.info.get("name") or ""
            if keyword not in name:
                continue
            try:
                log.info("发现进程: %s (PID: %s), 尝试终止...", name, proc.info.get("pid"))
                proc.terminate()
                proc.wait(timeout=timeout_s)
                log.info("已成功终止进程: %s", name)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired) as exc:
                log.warning("终止进程 %s 失败: %s", name, exc)
        remaining: list[str] = []
        for proc in psutil.process_iter(["name"]):
            name = proc.info.get("name") or ""
            if keyword in name:
                remaining.append(name)
        return remaining

    def launch(self, program: str) -> None:
        """启动程序。Windows 使用 os.startfile，其他平台退回 core 的进程启动。"""
        startfile = getattr(os, "startfile", None)
        try:
            if startfile is not None:
                startfile(program)
            else:
                from ppx_testkit.core.gui.window import launch_process

                launch_process(Path(program))
        except OSError as exc:
            raise GuiError(f"启动程序失败 {program}: {exc}") from exc

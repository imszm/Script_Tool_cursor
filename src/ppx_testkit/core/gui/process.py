"""窗口是否存在、以及按名称结束/启动进程。

pywinauto 与 psutil 都延迟导入，没有图形环境时核心包仍可导入。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ppx_testkit.exceptions import GuiError

log = logging.getLogger(__name__)


class TitleWindowProbe:
    """按标题包含关系判断顶层窗口是否存在。"""

    def exists(self, title: str) -> bool:
        """标题中包含 ``title`` 时返回 True。枚举失败抛出 GuiError。"""
        try:
            from pywinauto import findwindows  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GuiError("需要 Windows 环境并安装 pywinauto（pip install .[win]）") from exc
        try:
            return bool(findwindows.find_elements(title_re=f".*{re.escape(title)}.*", top_level_only=True))
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

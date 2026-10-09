"""W3 PCTOOL 组装生产工具压力测试：按绝对坐标依次点击按钮，窗口消失时重启软件。

迁移自 ``Tool/组装生产工具压力测试.py``。每轮流程::

    检查窗口是否存在（标题包含匹配）-> 不存在则结束残留进程并重新启动软件
    -> 依次执行 actions：点击坐标（可选：再次点击、全选清空、输入文本）-> 等待
       每个动作前都检查窗口，窗口消失判定闪退并终止

旧脚本中任何异常都会停止整个测试，这里保持一致（重启失败 / 闪退 -> 熔断，其他异常 -> 执行器终止）。
``count_valid`` 用于复现旧脚本的“有效点击次数”统计口径。
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ppx_testkit.apps.pctool.common import is_failsafe, write_reports
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.exceptions import ConfigError, GuiError, TestAbort
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)

Point = tuple[int, int]


# ============================================================ 配置
@dataclass(frozen=True)
class ClickAction:
    point: Point
    label: str
    wait_s: float = 0.0
    count_valid: bool = True
    input_text: str | None = None
    input_settle_s: float = 1.0
    type_interval_s: float = 0.1

    def __post_init__(self) -> None:
        if self.point[0] < 0 or self.point[1] < 0:
            raise ConfigError(f"动作 {self.label} 坐标非法: {self.point}")
        if self.wait_s < 0 or self.input_settle_s < 0 or self.type_interval_s < 0:
            raise ConfigError(f"动作 {self.label} 的等待时间不能为负数")


@dataclass(frozen=True)
class ScreenSettings:
    pause_s: float | None = None
    failsafe: bool = True


@dataclass(frozen=True)
class W3StressConfig:
    window_title: str
    actions: list[ClickAction]
    cycles: int = 30000
    program_path: str | None = None
    process_keyword: str = "EW01_PCTOOL"
    kill_timeout_s: float = 5.0
    restart_wait_s: float = 10.0
    screenshot_on_error: bool = True
    screen: ScreenSettings = field(default_factory=ScreenSettings)

    def __post_init__(self) -> None:
        if self.cycles <= 0:
            raise ConfigError("stress.cycles 必须 > 0")
        if not self.actions:
            raise ConfigError("stress.actions 至少需要一个动作")
        if not self.window_title:
            raise ConfigError("stress.window_title 不能为空")


# ============================================================ 依赖协议
class ClickScreen(Protocol):
    def click(self, point: Point, *, label: str = "") -> None: ...

    def hotkey(self, *keys: str) -> None: ...

    def press(self, key: str) -> None: ...

    def type_text(self, text: str, *, interval: float = 0.02, enter: bool = False) -> None: ...

    def save_screenshot(self, path: Path) -> Path | None: ...


class WindowProbe(Protocol):
    def exists(self, title: str) -> bool: ...


class ProcessControl(Protocol):
    def kill_matching(self, keyword: str, timeout_s: float) -> list[str]:
        """结束名称包含 keyword 的进程，返回仍未退出的进程名。"""

    def launch(self, program: str) -> None: ...


# ============================================================ 真实实现（Windows）
class TitleWindowProbe:
    """按“标题包含”判断窗口是否存在（等价于 pygetwindow.getWindowsWithTitle）。"""

    def exists(self, title: str) -> bool:
        try:
            from pywinauto import findwindows  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GuiError("需要 Windows 环境并安装 pywinauto（pip install .[win]）") from exc
        try:
            return bool(findwindows.find_elements(title_re=f".*{re.escape(title)}.*", top_level_only=True))
        except Exception as exc:  # noqa: BLE001 - 枚举窗口失败统一转换
            raise GuiError(f"枚举窗口失败: {exc}") from exc


class PsutilProcessControl:
    def kill_matching(self, keyword: str, timeout_s: float) -> list[str]:
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
        remaining = []
        for proc in psutil.process_iter(["name"]):
            name = proc.info.get("name") or ""
            if keyword in name:
                remaining.append(name)
        return remaining

    def launch(self, program: str) -> None:
        startfile = getattr(os, "startfile", None)
        try:
            if startfile is not None:
                startfile(program)
            else:
                from ppx_testkit.core.gui.window import launch_process

                launch_process(Path(program))
        except OSError as exc:
            raise GuiError(f"启动程序失败 {program}: {exc}") from exc


# ============================================================ 流程
class W3PcToolStressCycle:
    def __init__(self, cfg: W3StressConfig, screen: ClickScreen, probe: WindowProbe, process: ProcessControl, *,
                 ctx: RunContext | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.screen = screen
        self.probe = probe
        self.process = process
        self.ctx = ctx
        self._sleep = sleep
        self.click_count = 0
        self.valid_click_count = 0
        self.restarts = 0
        self.rows: list[dict[str, Any]] = []

    def setup(self) -> None:
        log.info("W3 PCTOOL 压力测试开始，目标窗口 '%s'，每轮 %d 个动作", self.cfg.window_title, len(self.cfg.actions))

    def run_cycle(self, index: int) -> CycleResult:
        try:
            if not self.probe.exists(self.cfg.window_title):
                log.warning("检测到目标窗口已关闭，可能是软件闪退，尝试重新启动软件...")
                if not self.restart_software():
                    raise TestAbort("软件重新启动失败，脚本停止执行。")
            for action in self.cfg.actions:
                if not self.probe.exists(self.cfg.window_title):
                    raise TestAbort(f"执行“{action.label}”前检测到目标窗口已关闭，可能是软件闪退。")
                self._perform(action)
        except Exception as exc:  # noqa: BLE001 - 记录现场后原样抛出，由执行器终止测试
            if not is_failsafe(exc):
                self.rows.append({"cycle": index, "verdict": "FAIL", "detail": str(exc)})
                self._screenshot(index)
            raise
        self.rows.append({"cycle": index, "verdict": "PASS", "detail": ""})
        return CycleResult(index, True, data={"clicks": self.click_count})

    def teardown(self, summary: RunSummary) -> None:
        summary.extra["总点击次数"] = self.click_count
        summary.extra["有效点击次数"] = self.valid_click_count
        summary.extra["软件重启次数"] = self.restarts
        log.info("脚本结束，总点击次数：%d，有效点击次数：%d", self.click_count, self.valid_click_count)

    # ------------------------------------------------------------ 步骤
    def _perform(self, action: ClickAction) -> None:
        self.screen.click(action.point, label=action.label)
        self.click_count += 1
        log.info("第 %d 次点击，位置 %s，操作：%s", self.click_count, action.point, action.label)
        if action.count_valid:
            self.valid_click_count += 1
            log.info("有效点击次数更新为：%d", self.valid_click_count)
        if action.input_text is not None:
            self._sleep(action.input_settle_s)
            self.screen.click(action.point, label=action.label)
            self.screen.hotkey("ctrl", "a")
            self.screen.press("backspace")
            self.screen.type_text(action.input_text, interval=action.type_interval_s)
        self._sleep(action.wait_s)

    def restart_software(self) -> bool:
        cfg = self.cfg
        if not cfg.program_path:
            log.error("未配置 stress.program_path，无法重新启动软件")
            return False
        try:
            log.info("检查是否有正在运行的目标软件（进程名包含 %s）...", cfg.process_keyword)
            remaining = self.process.kill_matching(cfg.process_keyword, cfg.kill_timeout_s)
            if remaining:
                raise GuiError(f"进程未能终止: {remaining}")
            log.info("尝试启动目标软件: %s", cfg.program_path)
            self.process.launch(cfg.program_path)
            self._sleep(cfg.restart_wait_s)
            if not self.probe.exists(cfg.window_title):
                raise GuiError("目标软件启动后未检测到窗口。")
        except (GuiError, OSError) as exc:
            log.error("重新启动软件失败: %s", exc)
            return False
        self.restarts += 1
        log.info("目标软件启动成功。")
        return True

    def _screenshot(self, index: int) -> None:
        if not self.cfg.screenshot_on_error or self.ctx is None:
            return
        path = self.ctx.artifact(f"error_cycle{index:05d}_{_dt.datetime.now():%H%M%S}.png")
        if path is None:
            return
        try:
            self.screen.save_screenshot(path)
        except Exception:  # noqa: BLE001 - 截图失败不影响结果
            log.exception("保存截图失败: %s", path)


def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("stress", W3StressConfig)
    from ppx_testkit.core.gui.screen import Screen

    screen = Screen(pause_s=cfg.screen.pause_s, failsafe=cfg.screen.failsafe)
    cycle = W3PcToolStressCycle(cfg, screen, TitleWindowProbe(), PsutilProcessControl(), ctx=ctx)
    summary = StressRunner(cycle, cfg.cycles, station=settings.station).run()
    write_reports(ctx, "report", title=f"{settings.station} 压力测试报告", rows=cycle.rows,
                  summary=summary.to_dict(), columns=["cycle", "verdict", "detail"])
    return 0 if summary.ok else 1

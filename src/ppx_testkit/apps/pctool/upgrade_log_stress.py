"""PCTOOL 升级压力测试：点击“升级”后等待固定时长，按日志框新增内容判定结果。

迁移自 ``Tool/升级工具压力自动化测试 - 可截图版本.py``。每轮流程::

    读取日志框当前内容长度 -> 点击升级按钮 -> 等待 cycle_wait_s
    -> 截取本轮新增日志 -> 含任一失败关键字（默认“失败”“超时”）判 FAIL

FAIL 时保存窗口截图；默认首次失败即终止测试（stop_on_fail，与旧脚本一致）。
每轮结果（时间 / 结果 / 详情）实时写入运行目录下的 test_log.csv。
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ppx_testkit.apps.pctool.common import (
    ControlRef,
    PcToolWindow,
    ToolWindow,
    WindowConfig,
    capture_window,
    write_reports,
)
from ppx_testkit.core.report.writers import write_csv
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)

NO_OUTPUT = "(本轮无日志输出)"
CSV_COLUMNS = ["时间", "结果", "详情"]


@dataclass(frozen=True)
class UpgradeLogConfig:
    upgrade_button: ControlRef
    log_edit: ControlRef
    cycles: int = 1_000_000
    cycle_wait_s: float = 170.0
    fail_keywords: list[str] = field(default_factory=lambda: ["失败", "超时"])
    invoke_click: bool = True
    stop_on_fail: bool = True
    screenshot_on_fail: bool = True

    def __post_init__(self) -> None:
        if self.cycles <= 0:
            raise ConfigError("stress.cycles 必须 > 0")
        if self.cycle_wait_s < 0:
            raise ConfigError("stress.cycle_wait_s 不能为负数")
        if not [k for k in self.fail_keywords if k]:
            raise ConfigError("stress.fail_keywords 至少配置一个非空关键字")


def new_log_part(old: str, new: str) -> str:
    """本轮新增日志：新内容比旧内容长时取增量，否则视为无输出（日志被清空也按无输出处理）。"""
    return new[len(old):] if len(new) > len(old) else ""


def classify_log(part: str, fail_keywords: Sequence[str]) -> tuple[bool, list[str]]:
    """返回 (是否通过, 命中的失败关键字)。"""
    hits = [k for k in fail_keywords if k and k in part]
    return not hits, hits


class UpgradeLogStressCycle:
    def __init__(self, cfg: UpgradeLogConfig, window: ToolWindow, *, ctx: RunContext | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], _dt.datetime] = _dt.datetime.now) -> None:
        self.cfg = cfg
        self.window = window
        self.ctx = ctx
        self._sleep = sleep
        self._now = now
        self.rows: list[dict[str, Any]] = []

    def setup(self) -> None:
        log.info("正在连接程序…")
        self.window.connect()
        log.info("开始压力测试… Ctrl+C 可停止")

    def run_cycle(self, index: int) -> CycleResult:
        cfg = self.cfg
        started = self._now().strftime("%Y-%m-%d %H:%M:%S")
        old_log = self.window.read_value(cfg.log_edit)
        self.window.click(cfg.upgrade_button, invoke=cfg.invoke_click)
        log.info("已点击升级，等待 %.0f 秒…", cfg.cycle_wait_s)
        self._sleep(cfg.cycle_wait_s)

        part = new_log_part(old_log, self.window.read_value(cfg.log_edit))
        if not part.strip():
            part = NO_OUTPUT
        passed, hits = classify_log(part, cfg.fail_keywords)
        self.rows.append({"时间": started, "结果": "成功" if passed else "失败", "详情": part})
        self._flush_csv()

        if passed:
            log.info("本轮成功")
            return CycleResult(index, True, data={"log": part})
        log.error("检测到失败关键字 %s，本轮日志:\n%s", hits, part)
        if cfg.screenshot_on_fail:
            capture_window(self.window, self.ctx, f"fail_{self._now():%Y%m%d_%H%M%S}.png")
        return CycleResult(index, False, f"日志含失败关键字 {hits}", data={"log": part})

    def teardown(self, summary: RunSummary) -> None:
        self._flush_csv()

    def _flush_csv(self) -> None:
        if self.ctx is None:
            return
        path = self.ctx.artifact("test_log.csv")
        if path is not None:
            write_csv(path, self.rows, CSV_COLUMNS)


def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("stress", UpgradeLogConfig)
    window = PcToolWindow(settings.section("window", WindowConfig))
    cycle = UpgradeLogStressCycle(cfg, window, ctx=ctx)
    summary = StressRunner(cycle, cfg.cycles, station=settings.station, stop_on_fail=cfg.stop_on_fail).run()
    report_rows = [{"cycle": i, "verdict": "PASS" if r["结果"] == "成功" else "FAIL", **r}
                   for i, r in enumerate(cycle.rows, start=1)]
    write_reports(ctx, "report", title=f"{settings.station} 升级压力测试报告", rows=report_rows,
                  summary=summary.to_dict(), columns=["cycle", "时间", "verdict", "详情"])
    return 0 if summary.ok else 1

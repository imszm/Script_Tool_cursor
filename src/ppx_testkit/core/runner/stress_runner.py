"""通用压测执行器。

统一负责：循环计数、统计、熔断、Ctrl+C、未知异常、``teardown`` 必达、summary.json 落盘。
具体动作由实现 :class:`StressCycle` 的应用提供。
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from typing import Protocol

from ppx_testkit.core.runner.result import CycleResult, RunSummary
from ppx_testkit.exceptions import TestAbort, ToolError
from ppx_testkit.logger import current_context
from ppx_testkit.utils.timing import sleep as interruptible_sleep

log = logging.getLogger(__name__)


class StressCycle(Protocol):
    def setup(self) -> None:
        """打开资源、预检。抛出异常则不进入循环。"""

    def run_cycle(self, index: int) -> CycleResult:
        """执行第 index 轮（从 1 开始）。需要熔断时抛出 TestAbort。"""

    def teardown(self, summary: RunSummary) -> None:
        """释放资源；根据 summary.keep_power 决定是否断电。必须幂等。"""


def _is_failsafe(exc: BaseException) -> bool:
    return type(exc).__name__ == "FailSafeException"


class StressRunner:
    def __init__(
        self,
        cycle: StressCycle,
        total: int,
        *,
        station: str,
        stop_on_fail: bool = False,
        fail_keeps_power: bool = False,
        max_consecutive_failures: int | None = None,
        interval_s: float = 0.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        if total <= 0:
            raise ValueError("total 必须 > 0")
        self.cycle = cycle
        self.total = total
        self.station = station
        self.stop_on_fail = stop_on_fail
        self.fail_keeps_power = fail_keeps_power
        self.max_consecutive_failures = max_consecutive_failures
        self.interval_s = interval_s
        self.stop_event = stop_event or threading.Event()
        self.summary = RunSummary(station=station, target_cycles=total)

    def run(self) -> RunSummary:
        s = self.summary
        t0 = time.monotonic()
        consecutive_failures = 0
        setup_done = False
        try:
            log.info("====== [%s] 准备测试环境 ======", self.station)
            self.cycle.setup()
            setup_done = True
            log.info("====== [%s] 压测开始，目标 %d 轮 ======", self.station, self.total)
            for i in range(1, self.total + 1):
                if self.stop_event.is_set():
                    s.interrupted = True
                    log.warning("收到停止信号，结束于第 %d 轮之前", i)
                    break
                log.info("--- 第 %d/%d 轮 ---", i, self.total)
                c0 = time.monotonic()
                result = self.cycle.run_cycle(i)
                result.duration_s = result.duration_s or (time.monotonic() - c0)
                s.executed += 1
                if result.passed:
                    s.passed += 1
                    consecutive_failures = 0
                    log.info("第 %d 轮 PASS %s(%.2fs) | 累计通过率 %.2f%% (%d/%d)",
                             i, f"{result.detail} " if result.detail else "", result.duration_s,
                             s.pass_rate, s.passed, s.executed)
                else:
                    s.failed += 1
                    consecutive_failures += 1
                    log.error("第 %d 轮 FAIL: %s | 累计通过率 %.2f%% (%d/%d)",
                              i, result.detail, s.pass_rate, s.passed, s.executed)
                    if self.stop_on_fail:
                        raise TestAbort(f"第 {i} 轮失败: {result.detail}", keep_power=self.fail_keeps_power)
                    if self.max_consecutive_failures and consecutive_failures >= self.max_consecutive_failures:
                        raise TestAbort(f"连续失败 {consecutive_failures} 轮", keep_power=self.fail_keeps_power)
                if i < self.total and self.interval_s > 0:
                    if not interruptible_sleep(self.interval_s, self.stop_event):
                        s.interrupted = True
                        break
        except TestAbort as exc:
            s.aborted = True
            s.abort_reason = exc.reason
            s.keep_power = exc.keep_power
            log.error("测试熔断: %s%s", exc.reason, "（保留现场，不断电）" if exc.keep_power else "")
        except KeyboardInterrupt:
            s.interrupted = True
            log.warning("收到 Ctrl+C，停止测试")
        except ToolError as exc:
            s.error = f"{type(exc).__name__}: {exc}"
            log.error("测试因硬件/配置故障终止: %s", exc, exc_info=True)
        except Exception as exc:  # noqa: BLE001 - 执行器兜底，确保 teardown 与总结一定执行
            if _is_failsafe(exc):
                s.interrupted = True
                log.error("触发 pyautogui 防故障机制（鼠标移到屏幕角落），测试停止")
            else:
                s.error = f"{type(exc).__name__}: {exc}"
                log.critical("测试发生未预期异常", exc_info=True)
        finally:
            if not setup_done:
                log.error("环境准备阶段失败，未进入压测循环")
            try:
                self.cycle.teardown(s)
            except Exception:  # noqa: BLE001 - teardown 异常只记录，不覆盖原始结果
                log.exception("teardown 执行异常")
            s.duration_s = time.monotonic() - t0
            s.ended_at = _dt.datetime.now().isoformat(timespec="seconds")
            log.info("\n%s", s.render())
            ctx = current_context()
            if ctx is not None:
                ctx.write_json("summary.json", s.to_dict())
        return s

"""继电器模拟电源键开关机压力测试（W3）。

对应旧脚本 ``Tool/W3继电器开关机压力测试.py``，工位配置 ``config/stations/w3_power_button.yaml``。

继电器串在电源键上（NC 常闭接法）：``relay.channels.<ch>.on`` = 按下，``off`` = 松开（安全状态）。

单轮流程::

    长按 press_on_s（开机） -> 监听 hold_s -> 短按 press_off_s（关机） -> 监听 after_off_watch_s

* 设备日志命中 ``keywords.abort``（默认 exact 区分大小写匹配，与旧脚本一致）-> 熔断，
  不再按关机键（保留现场）；
* 没有成功判定关键字：未熔断、继电器与串口均正常即视为本轮通过；
* 退出时（任何原因）都会发送“松开”，保证电源键不会一直处于按下状态。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ppx_testkit.apps.power_cycle._common import (
    build_monitor,
    close_ports,
    finish,
    open_ports,
    require_non_negative,
    require_runner_options,
    try_reconnect,
)
from ppx_testkit.core.factory import keyword_config
from ppx_testkit.core.monitor.line_reader import DeviceLogMonitor, MonitorResult
from ppx_testkit.core.relay.base import RelayConfig, RelayDriver, build_relay
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.core.serial.port_finder import PortFinder
from ppx_testkit.core.serial.transport import SerialFactory
from ppx_testkit.exceptions import ConfigError, HardwareError, RelayError, TestAbort
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)

SECTION = "test"


@dataclass(frozen=True)
class PowerButtonConfig:
    cycles: int = 500000
    relay_channel: int = 1
    press_on_s: float = 2.5             # 开机长按时长
    press_off_s: float = 1.0            # 关机短按时长
    hold_s: float = 10.0                # 开机后保持监听时长
    after_off_watch_s: float = 3.0      # 关机后继续监听时长
    init_release_wait_s: float = 1.0    # 初始化：先松开并等待
    init_commands: list[str] = field(default_factory=list)  # 初始化依次发送的 relay.commands 命名指令
    init_command_wait_s: float = 1.0
    reconnect_delay_s: float = 3.0
    stop_on_fail: bool = True
    max_consecutive_failures: int | None = None
    interval_s: float = 0.0
    notify: bool = True

    def __post_init__(self) -> None:
        require_runner_options(SECTION, self.cycles, self.max_consecutive_failures)
        require_non_negative(
            SECTION,
            press_on_s=self.press_on_s,
            press_off_s=self.press_off_s,
            hold_s=self.hold_s,
            after_off_watch_s=self.after_off_watch_s,
            init_release_wait_s=self.init_release_wait_s,
            init_command_wait_s=self.init_command_wait_s,
            reconnect_delay_s=self.reconnect_delay_s,
            interval_s=self.interval_s,
        )
        if any(not name.strip() for name in self.init_commands):
            raise ConfigError(f"{SECTION}.init_commands 中存在空指令名")


class PowerButtonCycle:
    def __init__(
        self,
        cfg: PowerButtonConfig,
        relay: RelayDriver,
        monitor: DeviceLogMonitor,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.relay = relay
        self.monitor = monitor
        self._sleep = sleep
        self._closed = False
        self.disconnects = 0

    # ------------------------------------------------------------ StressCycle
    def setup(self) -> None:
        missing = [n for n in self.cfg.init_commands if n not in self.relay.cfg.commands]
        if missing:
            raise ConfigError(f"test.init_commands 引用了未在 relay.commands 中定义的指令: {missing}")
        ch = self.cfg.relay_channel
        log.info("初始化：强制松开电源键（继电器 CH%d OFF）", ch)
        self.relay.off(ch)
        self._sleep(self.cfg.init_release_wait_s)
        for name in self.cfg.init_commands:
            log.info("初始化：发送继电器指令 '%s'", name)
            self.relay.send(name)
            self._sleep(self.cfg.init_command_wait_s)
        log.info("继电器初始化完成，状态: 松开（安全）")
        log.info("开机长按 %.1fs，关机短按 %.1fs，开机保持监听 %.1fs",
                 self.cfg.press_on_s, self.cfg.press_off_s, self.cfg.hold_s)

    def run_cycle(self, index: int) -> CycleResult:
        cfg = self.cfg
        self.monitor.evaluator.reset_cycle()

        failed = self._press(cfg.press_on_s, "开机")
        if failed:
            return CycleResult(index, False, failed)

        log.info("系统运行，保持待机监听 %.1fs...", cfg.hold_s)
        failed = self._watch(index, cfg.hold_s, "开机保持")
        if failed:
            return CycleResult(index, False, failed)

        failed = self._press(cfg.press_off_s, "关机")
        if failed:
            return CycleResult(index, False, failed)

        failed = self._watch(index, cfg.after_off_watch_s, "关机后")
        if failed:
            return CycleResult(index, False, failed)
        return CycleResult(index, True, "开关机动作完成，未命中熔断关键字")

    def teardown(self, summary: RunSummary) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if summary.keep_power:
                log.warning("【现场保留】停止按键动作，设备维持当前开/关机状态")
            log.info("退出：松开电源键")
            try:
                self.relay.all_off()
            except HardwareError as exc:
                log.error("退出时松开电源键失败: %s", exc)
        finally:
            close_ports(self.relay.transport, self.monitor.transport)
            summary.extra.update({
                "异常关键字次数": self.monitor.evaluator.exception_count,
                "设备串口断连次数": self.disconnects,
            })

    # ------------------------------------------------------------ 内部
    def _press(self, hold_s: float, action: str) -> str | None:
        """按下-保持-松开；失败时尽力松开并返回失败描述。"""
        ch = self.cfg.relay_channel
        log.info("【%s】按下电源键，保持 %.1fs", action, hold_s)
        try:
            self.relay.press(ch, hold_s)
        except RelayError as exc:
            log.error("【%s】按键动作失败: %s，尝试松开", action, exc)
            try:
                self.relay.off(ch)
            except RelayError as exc2:
                log.error("松开电源键失败: %s", exc2)
            return f"{action}按键继电器动作失败: {exc}"
        log.info("【%s】松开电源键", action)
        return None

    def _watch(self, index: int, duration_s: float, stage: str) -> str | None:
        if duration_s <= 0:
            return None
        result: MonitorResult = self.monitor.watch(duration_s)
        if result.aborted:
            raise TestAbort(f"第 {index} 轮{stage}监听熔断: {result.abort_reason}",
                            keep_power=result.abort_keep_power)
        if result.disconnected:
            self.disconnects += 1
            if not try_reconnect(self.monitor.transport, self.cfg.reconnect_delay_s):
                raise TestAbort(f"第 {index} 轮{stage}监听时设备串口断开且重连失败: {result.disconnect_error}",
                                keep_power=True)
            return f"{stage}监听时设备串口断开（已重连）: {result.disconnect_error}"
        return None


def run(
    settings: AppSettings,
    ctx: RunContext,
    *,
    factory: SerialFactory | None = None,
    finder: PortFinder | None = None,
) -> int:
    cfg = settings.section(SECTION, PowerButtonConfig)
    relay_cfg = settings.section("relay", RelayConfig)
    keyword_config(settings)
    log.info("运行目录: %s", ctx.run_dir or "<仅控制台>")

    ports = open_ports(
        settings, [("relay", not relay_cfg.open_per_command), ("device", True)], factory=factory, finder=finder
    )
    try:
        relay = build_relay(relay_cfg, ports["relay"])
        monitor = build_monitor(settings, ports["device"])
        cycle = PowerButtonCycle(cfg, relay, monitor)
        summary = StressRunner(
            cycle,
            cfg.cycles,
            station=settings.station,
            stop_on_fail=cfg.stop_on_fail,
            fail_keeps_power=True,
            max_consecutive_failures=cfg.max_consecutive_failures,
            interval_s=cfg.interval_s,
        ).run()
    finally:
        close_ports(*ports.values())
    return finish(summary, title="W3 电源键开关机压测", notify=cfg.notify)

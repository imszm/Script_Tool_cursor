"""继电器充电通断压力测试。

合并旧脚本（两者流程一致，仅成功关键字不同）::

    Tool/继电器充电压力测试_L5.py -> config/stations/l5_charge_cycle.yaml
    Tool/继电器充电压力测试_R3.py -> config/stations/r3_charge_cycle.yaml

单轮流程::

    清空设备缓冲 -> 继电器闭合（开始充电）
      -> 监听 charge_on_s，命中成功关键字立即结束监听
      -> 若成功：继续闭合并监听 post_success_hold_s（状态维稳）
      -> 继电器断开（切断充电）-> 监听 off_reset_s（等待 BMS 状态机复位）
      -> 以“闭合阶段是否命中成功关键字”判定 PASS / FAIL

任何阶段命中熔断规则即 TestAbort；旧脚本熔断后会断开继电器，因此配置中
``rate_rules[].keep_power`` 默认为 false。抗干扰的“每条指令连发两次”由 ``relay.repeat: 2`` 表达。
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from ppx_testkit.apps.power_cycle._common import (
    build_monitor,
    close_ports,
    finish,
    open_ports,
    pick_duration,
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
class ChargeCycleConfig:
    cycles: int = 500000
    relay_channel: int = 1
    charge_on_s: float = 25.0              # 闭合后等待成功关键字的最长时间（随机模式下为下限）
    charge_on_max_s: float | None = None   # 设置后在 [charge_on_s, charge_on_max_s] 内随机
    post_success_hold_s: float = 3.0       # 命中成功后继续保持闭合并监听
    off_reset_s: float = 10.0              # 断开后静置监听（BMS 复位）
    initial_off_s: float | None = 10.0     # 压测前先断开并等待；为空则跳过
    flush_before_on: bool = True
    reconnect_delay_s: float = 3.0
    disconnect_keeps_power: bool = False   # 设备串口断开且重连失败熔断时是否保持继电器状态
    stop_on_fail: bool = False
    max_consecutive_failures: int | None = None
    interval_s: float = 0.0
    notify: bool = True

    def __post_init__(self) -> None:
        require_runner_options(SECTION, self.cycles, self.max_consecutive_failures)
        require_non_negative(
            SECTION,
            charge_on_s=self.charge_on_s,
            post_success_hold_s=self.post_success_hold_s,
            off_reset_s=self.off_reset_s,
            initial_off_s=self.initial_off_s,
            reconnect_delay_s=self.reconnect_delay_s,
            interval_s=self.interval_s,
        )
        if self.charge_on_max_s is not None and self.charge_on_max_s < self.charge_on_s:
            raise ConfigError(f"{SECTION}.charge_on_max_s 不能小于 charge_on_s")


class ChargeCycle:
    def __init__(
        self,
        cfg: ChargeCycleConfig,
        relay: RelayDriver,
        monitor: DeviceLogMonitor,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg
        self.relay = relay
        self.monitor = monitor
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._closed = False
        self.disconnects = 0

    # ------------------------------------------------------------ StressCycle
    def setup(self) -> None:
        if self.cfg.initial_off_s is not None:
            log.info("环境初始清洗：继电器断开（物理断电重置），等待 %.1fs", self.cfg.initial_off_s)
            self.relay.off(self.cfg.relay_channel)
            self._sleep(self.cfg.initial_off_s)
        self.monitor.flush_input()

    def run_cycle(self, index: int) -> CycleResult:
        cfg = self.cfg
        ch = cfg.relay_channel
        evaluator = self.monitor.evaluator
        limit_s = pick_duration(cfg.charge_on_s, cfg.charge_on_max_s, self._rng)
        log.info("第 %d 轮：最大充电监听 %.1fs", index, limit_s)

        evaluator.reset_cycle()
        if cfg.flush_before_on:
            self.monitor.flush_input()

        log.info("继电器 ON（开始充电）")
        try:
            self.relay.on(ch)
        except RelayError as exc:
            return CycleResult(index, False, f"继电器闭合失败: {exc}")

        result = self.monitor.watch(limit_s, stop_on_success=True)
        self._check_watch(index, result, "充电监听")
        success = evaluator.cycle_success
        if success:
            log.info("捕获目标关键字 '%s'（%.1fs），状态维稳，保持闭合 %.1fs",
                     success, result.success_after_s or result.elapsed_s, cfg.post_success_hold_s)
            if cfg.post_success_hold_s > 0:
                self._check_watch(index, self.monitor.watch(cfg.post_success_hold_s), "维稳监听")

        log.info("继电器 OFF（切断充电）")
        try:
            self.relay.off(ch)
        except RelayError as exc:
            return CycleResult(index, False, f"继电器断开失败: {exc}")

        log.info("物理断电静置 %.1fs（等待 BMS 状态机完全复位）", cfg.off_reset_s)
        if cfg.off_reset_s > 0:
            self._check_watch(index, self.monitor.watch(cfg.off_reset_s), "断电静置监听")

        if success:
            return CycleResult(index, True, f"成功关键字 '{success}'",
                               data={"success_keyword": success, "success_after_s": result.success_after_s})
        return CycleResult(index, False, f"{limit_s:.1f}s 内未检测到成功关键字，设备可能未进入充电状态或响应过慢")

    def teardown(self, summary: RunSummary) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if summary.keep_power:
                log.warning("【现场保留】继电器保持当前状态（不断开充电回路）")
            else:
                log.info("测试结束，执行环境安全隔离：断开继电器")
                try:
                    self.relay.all_off()
                except HardwareError as exc:
                    log.error("退出时断开继电器失败: %s", exc)
        finally:
            close_ports(self.relay.transport, self.monitor.transport)
            summary.extra.update({
                "异常关键字次数": self.monitor.evaluator.exception_count,
                "设备串口断连次数": self.disconnects,
            })

    # ------------------------------------------------------------ 内部
    def _check_watch(self, index: int, result: MonitorResult, stage: str) -> None:
        if result.aborted:
            raise TestAbort(f"第 {index} 轮{stage}熔断: {result.abort_reason}", keep_power=result.abort_keep_power)
        if result.disconnected:
            self.disconnects += 1
            if not try_reconnect(self.monitor.transport, self.cfg.reconnect_delay_s):
                raise TestAbort(f"第 {index} 轮{stage}时设备串口断开且重连失败: {result.disconnect_error}",
                                keep_power=self.cfg.disconnect_keeps_power)


def run(
    settings: AppSettings,
    ctx: RunContext,
    *,
    factory: SerialFactory | None = None,
    finder: PortFinder | None = None,
) -> int:
    cfg = settings.section(SECTION, ChargeCycleConfig)
    relay_cfg = settings.section("relay", RelayConfig)
    keyword_config(settings)
    log.info("运行目录: %s", ctx.run_dir or "<仅控制台>")

    ports = open_ports(
        settings, [("relay", not relay_cfg.open_per_command), ("device", True)], factory=factory, finder=finder
    )
    try:
        relay = build_relay(relay_cfg, ports["relay"])
        monitor = build_monitor(settings, ports["device"])
        cycle = ChargeCycle(cfg, relay, monitor)
        summary = StressRunner(
            cycle,
            cfg.cycles,
            station=settings.station,
            stop_on_fail=cfg.stop_on_fail,
            max_consecutive_failures=cfg.max_consecutive_failures,
            interval_s=cfg.interval_s,
        ).run()
    finally:
        close_ports(*ports.values())
    return finish(summary, title="继电器充电压测", notify=cfg.notify)

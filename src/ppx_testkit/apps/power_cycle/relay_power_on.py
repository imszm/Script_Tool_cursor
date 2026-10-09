"""继电器上下电开关机压力测试（通用版）。

合并以下旧脚本，差异全部由工位配置表达::

    Tool/继电器开关机压力测试.py    -> config/stations/relay_power_cycle.yaml
    Tool/NFC开关机异常关键字检测.py -> config/stations/nfc_power_cycle.yaml
    Tool/把手自动化开关.py          -> config/stations/handle_power_cycle.yaml

单轮流程（``test.monitor_device: true``）::

    [清空设备缓冲] -> 继电器上电 -> 监听设备日志 power_on_s
      ├─ 命中熔断规则 -> TestAbort（是否保留现场由关键字规则 keep_power 决定）
      ├─ 命中成功关键字 -> 断电 -> 等待 power_off_s -> [断电后补充监听] -> PASS
      └─ 未命中
           ├─ fail_keeps_power: true  -> 不断电，FAIL（配合 stop_on_fail 熔断并保留现场）
           └─ fail_keeps_power: false -> 断电 -> 等待 -> [补充监听]，仍未命中则 FAIL

``test.monitor_device: false`` 时不打开设备串口，只做“上电-保持-断电-等待”的纯继电器循环。

退出约定：熔断且 keep_power 时不断电（保留现场）；其余情况（含 Ctrl+C、程序异常）
在 teardown 中尽力断开全部继电器通道。
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
from ppx_testkit.exceptions import ConfigError, HardwareError, RelayError, SerialPortError, TestAbort
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)

SECTION = "test"


@dataclass(frozen=True)
class RelayPowerCycleConfig:
    cycles: int = 200000
    relay_channel: int = 1
    power_on_s: float = 5.0                # 上电并监听的时长（随机模式下为下限）
    power_on_max_s: float | None = None    # 设置后上电时长在 [power_on_s, power_on_max_s] 内随机
    power_off_s: float = 5.0               # 断电等待（电容放电）时长
    initial_off_s: float | None = None     # 压测前先断电并等待该时长；为空则跳过
    monitor_device: bool = True            # false：不打开设备串口，纯继电器开关
    flush_before_on: bool = True           # 每轮上电前清空设备串口缓冲
    stop_on_success: bool = False          # 命中成功关键字后立即结束上电监听
    post_off_watch_s: float = 0.0          # 断电等待后再补充监听的时长（计入成功判定）
    reset_rates_each_cycle: bool = False   # 每轮开始清空频率熔断计数
    precheck: bool = False                 # 压测前上电一次确认设备串口有数据（零数据预判）
    precheck_timeout_s: float = 8.0
    precheck_poll_s: float = 0.05
    no_data_warning_s: float | None = None  # 上电后该时长仍无字节则输出“串口静默预警”
    reconnect_delay_s: float = 3.0
    fail_keeps_power: bool = False         # 判定失败时不断电（保留现场）
    stop_on_fail: bool = False
    max_consecutive_failures: int | None = None
    interval_s: float = 0.0
    notify: bool = True                    # 结束时弹窗提示（仅 Windows）

    def __post_init__(self) -> None:
        require_runner_options(SECTION, self.cycles, self.max_consecutive_failures)
        require_non_negative(
            SECTION,
            power_on_s=self.power_on_s,
            power_off_s=self.power_off_s,
            initial_off_s=self.initial_off_s,
            post_off_watch_s=self.post_off_watch_s,
            precheck_timeout_s=self.precheck_timeout_s,
            no_data_warning_s=self.no_data_warning_s,
            reconnect_delay_s=self.reconnect_delay_s,
            interval_s=self.interval_s,
        )
        if self.precheck_poll_s <= 0:
            raise ConfigError(f"{SECTION}.precheck_poll_s 必须 > 0")
        if self.power_on_max_s is not None and self.power_on_max_s < self.power_on_s:
            raise ConfigError(f"{SECTION}.power_on_max_s 不能小于 power_on_s")
        if self.fail_keeps_power and not self.stop_on_fail:
            raise ConfigError(f"{SECTION}.fail_keeps_power 为 true（失败不断电保留现场）时必须同时设置 stop_on_fail: true")
        if not self.monitor_device and (self.precheck or self.post_off_watch_s > 0 or self.fail_keeps_power):
            raise ConfigError(
                f"{SECTION}.monitor_device 为 false 时不能启用 precheck / post_off_watch_s / fail_keeps_power"
            )


class RelayPowerCycle:
    """实现 :class:`~ppx_testkit.core.runner.StressCycle`；所有硬件对象由外部注入，便于假串口测试。"""

    def __init__(
        self,
        cfg: RelayPowerCycleConfig,
        relay: RelayDriver,
        monitor: DeviceLogMonitor | None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cfg.monitor_device and monitor is None:
            raise ConfigError("test.monitor_device 为 true 时必须提供设备日志监听器")
        self.cfg = cfg
        self.relay = relay
        self.monitor = monitor if cfg.monitor_device else None
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._clock = clock
        self._closed = False
        self.disconnects = 0
        self.zero_data_failures = 0

    # ------------------------------------------------------------ StressCycle
    def setup(self) -> None:
        ch = self.cfg.relay_channel
        if self.cfg.initial_off_s is not None:
            log.info("初始状态复位：继电器 CH%d 断电，等待 %.1fs", ch, self.cfg.initial_off_s)
            self.relay.off(ch)
            self._sleep(self.cfg.initial_off_s)
        if self.cfg.precheck and self.monitor is not None:
            self._precheck(self.monitor)

    def run_cycle(self, index: int) -> CycleResult:
        on_s = pick_duration(self.cfg.power_on_s, self.cfg.power_on_max_s, self._rng)
        log.info("第 %d 轮：上电监听 %.2fs，断电 %.2fs", index, on_s, self.cfg.power_off_s)
        if self.monitor is None:
            return self._relay_only_cycle(index, on_s)
        return self._monitored_cycle(index, on_s, self.monitor)

    def teardown(self, summary: RunSummary) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if summary.keep_power:
                log.warning("【现场保留】继电器保持当前状态（不断电），请排查完毕后手动断电")
            else:
                log.info("测试结束，断开继电器（断电）")
                try:
                    self.relay.all_off()
                except HardwareError as exc:
                    log.error("退出时断开继电器失败: %s", exc)
        finally:
            close_ports(self.relay.transport, self.monitor.transport if self.monitor else None)
            summary.extra.update(self.stats())

    # ------------------------------------------------------------ 统计
    def stats(self) -> dict[str, int]:
        if self.monitor is None:
            return {}
        return {
            "异常关键字次数": self.monitor.evaluator.exception_count,
            "零数据失败次数": self.zero_data_failures,
            "设备串口断连次数": self.disconnects,
        }

    # ------------------------------------------------------------ 内部
    def _relay_only_cycle(self, index: int, on_s: float) -> CycleResult:
        ch = self.cfg.relay_channel
        try:
            self.relay.on(ch)
            self._sleep(on_s)
            self.relay.off(ch)
        except RelayError as exc:
            return CycleResult(index, False, f"继电器动作失败: {exc}")
        self._sleep(self.cfg.power_off_s)
        return CycleResult(index, True, "继电器开关完成（不做设备日志判定）")

    def _monitored_cycle(self, index: int, on_s: float, monitor: DeviceLogMonitor) -> CycleResult:
        cfg = self.cfg
        ch = cfg.relay_channel
        evaluator = monitor.evaluator
        if cfg.reset_rates_each_cycle:
            evaluator.reset_rates()
        evaluator.reset_cycle()
        if cfg.flush_before_on:
            monitor.flush_input()

        try:
            self.relay.on(ch)
        except RelayError as exc:
            return CycleResult(index, False, f"继电器上电失败: {exc}")

        result = monitor.watch(on_s, stop_on_success=cfg.stop_on_success)
        lines = list(result.lines)
        self._check_watch(index, result, "上电监听", monitor)
        success = evaluator.cycle_success

        if success is None and cfg.fail_keeps_power:
            return self._fail(index, lines, on_s, monitor, kept_power=True)

        try:
            self.relay.off(ch)
        except RelayError as exc:
            return CycleResult(index, False, f"继电器断电失败: {exc}")
        self._sleep(cfg.power_off_s)

        if cfg.post_off_watch_s > 0:
            extra = monitor.watch(cfg.post_off_watch_s)
            lines.extend(extra.lines)
            self._check_watch(index, extra, "断电后监听", monitor)
            success = success or evaluator.cycle_success

        if success:
            data = {"success_keyword": success, "success_after_s": result.success_after_s}
            return CycleResult(index, True, f"命中成功关键字 '{success}'", data=data)
        return self._fail(index, lines, on_s, monitor, kept_power=False)

    def _check_watch(self, index: int, result: MonitorResult, stage: str, monitor: DeviceLogMonitor) -> None:
        if result.aborted:
            raise TestAbort(f"第 {index} 轮{stage}熔断: {result.abort_reason}", keep_power=result.abort_keep_power)
        if result.disconnected:
            self.disconnects += 1
            if not try_reconnect(monitor.transport, self.cfg.reconnect_delay_s):
                raise TestAbort(
                    f"第 {index} 轮{stage}时设备串口断开且重连失败: {result.disconnect_error}",
                    keep_power=self.cfg.fail_keeps_power,
                )

    def _fail(
        self, index: int, lines: list[str], on_s: float, monitor: DeviceLogMonitor, *, kept_power: bool
    ) -> CycleResult:
        if not lines:
            self.zero_data_failures += 1
            fail_type = "零数据故障"
            detail = (
                f"{on_s:.1f}s 监听窗口内未从 {monitor.transport.port} 收到任何有效数据"
                "（检查: ①设备 UART-TX 接线 ②串口号是否正确 ③继电器极性，必要时对调 relay.channels 的 on/off）"
            )
        else:
            fail_type = "关键字未命中"
            detail = f"收到 {len(lines)} 行串口数据但未匹配任何成功关键字（检查 keywords.success 或设备启动序列）"
        if kept_power:
            log.error("【现场保留】继电器保持闭合，设备持续上电，请即时排查！")
        return CycleResult(index, False, f"[{fail_type}] {detail}", data={"fail_type": fail_type, "lines": len(lines)})

    def _precheck(self, monitor: DeviceLogMonitor) -> None:
        cfg = self.cfg
        ch = cfg.relay_channel
        port = monitor.transport.port
        log.info("串口连通性预验证：上电，等待最多 %.1fs 确认 %s 能收到数据...", cfg.precheck_timeout_s, port)
        monitor.flush_input()
        received_after: float | None = None
        self.relay.on(ch)
        try:
            t0 = self._clock()
            while True:
                if monitor.transport.in_waiting > 0:
                    received_after = self._clock() - t0
                    break
                if self._clock() - t0 >= cfg.precheck_timeout_s:
                    break
                self._sleep(cfg.precheck_poll_s)
        except SerialPortError as exc:
            log.error("连通性验证过程中设备串口异常: %s", exc)
        finally:
            # 无论结果如何都断电复位
            self.relay.off(ch)

        if received_after is None:
            raise TestAbort(
                f"串口连通性验证失败：上电 {cfg.precheck_timeout_s:.1f}s 内 {port} 未收到任何数据"
                "（检查: ①TX/RX 接线 ②串口号 ③继电器极性），压测未启动",
                keep_power=False,
            )
        log.info("连通性验证通过：上电 %.2fs 后收到首字节数据，串口链路正常", received_after)
        log.info("等待 %.1fs 电容放电后进入压测循环...", cfg.power_off_s)
        self._sleep(cfg.power_off_s)
        monitor.flush_input()


def run(
    settings: AppSettings,
    ctx: RunContext,
    *,
    factory: SerialFactory | None = None,
    finder: PortFinder | None = None,
) -> int:
    cfg = settings.section(SECTION, RelayPowerCycleConfig)
    relay_cfg = settings.section("relay", RelayConfig)
    specs = [("relay", not relay_cfg.open_per_command)]
    if cfg.monitor_device:
        keyword_config(settings)
        specs.append(("device", True))
    log.info("运行目录: %s", ctx.run_dir or "<仅控制台>")

    ports = open_ports(settings, specs, factory=factory, finder=finder)
    try:
        relay = build_relay(relay_cfg, ports["relay"])
        monitor = (
            build_monitor(settings, ports["device"], no_data_warning_s=cfg.no_data_warning_s)
            if cfg.monitor_device
            else None
        )
        cycle = RelayPowerCycle(cfg, relay, monitor)
        summary = StressRunner(
            cycle,
            cfg.cycles,
            station=settings.station,
            stop_on_fail=cfg.stop_on_fail,
            fail_keeps_power=cfg.fail_keeps_power,
            max_consecutive_failures=cfg.max_consecutive_failures,
            interval_s=cfg.interval_s,
        ).run()
    finally:
        close_ports(*ports.values())
    return finish(summary, title="继电器开关机压测", notify=cfg.notify)

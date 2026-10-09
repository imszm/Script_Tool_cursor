"""舵机刷 NFC 卡开关机压力测试。

对应旧脚本 ``Tool/舵机压力测试_V1_1.py``，工位配置 ``config/stations/servo_nfc.yaml``。

* 舵机（总线舵机 ASCII 协议）带动 NFC 卡下压 / 抬起，每条指令独立开关串口；
* 设备日志由 :class:`BackgroundLogReader` 后台线程持续读取，与舵机动作并行：
  ``keywords.abort`` 命中即熔断、``keywords.rate_rules`` 按“N 秒内 M 次”熔断，
  熔断时立即打断当前等待；
* 每轮结束按 ``test.status_on`` / ``test.status_off`` 在本轮日志中**最后一次出现**的位置
  判定设备最终状态，与期望状态（上一状态取反）比较得出 PASS / FAIL，
  并无论成败都把当前状态同步为日志反馈的真实状态；
* 退出时（任何原因）把舵机抬起，不影响设备供电，保留现场。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

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
from ppx_testkit.core.monitor.line_reader import BackgroundLogReader
from ppx_testkit.core.relay.drivers import ServoDriver, servo_command
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.core.serial.port_finder import PortFinder
from ppx_testkit.core.serial.transport import SerialFactory
from ppx_testkit.exceptions import ConfigError, HardwareError, RelayError, TestAbort
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings
from ppx_testkit.utils.ansi import normalize, strip_ansi

log = logging.getLogger(__name__)

SECTION = "test"

Status = Literal["on", "off"]
STATUS_LABEL: dict[str, str] = {"on": "开机", "off": "关机"}


def _opposite(status: Status) -> Status:
    return "off" if status == "on" else "on"


@dataclass(frozen=True)
class ServoNfcConfig:
    cycles: int = 10000
    initial_status: Status = "off"
    servo_id: int = 0
    low_position: int = 2500            # 下压刷卡位置
    high_position: int = 500            # 抬起复位位置
    move_time_ms: int = 1000            # 舵机动作时间（同时计入等待）
    low_stay_on_s: float = 2.1          # 期望“开机”时最低点停留
    low_stay_off_s: float = 2.0         # 期望“关机”时最低点停留
    high_stay_s: float = 3.5            # 最高点停留
    settle_s: float = 1.0               # 抬起后额外等待日志吐完
    gap_min_s: float = 0.5              # 两轮之间随机间隔
    gap_max_s: float = 1.5
    startup_wait_s: float = 2.0         # 启动日志线程后等待
    status_on: list[str] = field(default_factory=list)
    status_off: list[str] = field(default_factory=list)
    status_match_mode: Literal["exact", "normalized"] = "exact"
    servo_retries: int = 2
    servo_retry_delay_s: float = 0.1
    buffer_lines: int = 5000            # 单轮最多保留的日志行数（用于状态判定）
    reconnect_interval_s: float = 3.0
    fail_tail_lines: int = 10           # 失败时输出本轮日志末尾行数
    stop_on_fail: bool = False
    max_consecutive_failures: int | None = None
    notify: bool = True

    def __post_init__(self) -> None:
        require_runner_options(SECTION, self.cycles, self.max_consecutive_failures)
        require_non_negative(
            SECTION,
            low_stay_on_s=self.low_stay_on_s,
            low_stay_off_s=self.low_stay_off_s,
            high_stay_s=self.high_stay_s,
            settle_s=self.settle_s,
            gap_min_s=self.gap_min_s,
            gap_max_s=self.gap_max_s,
            startup_wait_s=self.startup_wait_s,
            servo_retry_delay_s=self.servo_retry_delay_s,
            reconnect_interval_s=self.reconnect_interval_s,
            fail_tail_lines=self.fail_tail_lines,
        )
        if self.gap_max_s < self.gap_min_s:
            raise ConfigError(f"{SECTION}.gap_max_s 不能小于 gap_min_s")
        if not self.status_on or not self.status_off:
            raise ConfigError(f"{SECTION}.status_on / status_off 不能为空")
        if any(not k.strip() for k in (*self.status_on, *self.status_off)):
            raise ConfigError(f"{SECTION}.status_on / status_off 中存在空字符串")
        if self.servo_retries < 1 or self.buffer_lines < 1:
            raise ConfigError(f"{SECTION}.servo_retries / buffer_lines 至少为 1")
        # 提前校验舵机 ID / 位置 / 时间范围，非法时抛 ConfigError
        servo_command(self.servo_id, self.low_position, self.move_time_ms)
        servo_command(self.servo_id, self.high_position, self.move_time_ms)


class ServoNfcCycle:
    def __init__(
        self,
        cfg: ServoNfcConfig,
        servo: ServoDriver,
        reader: BackgroundLogReader,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg
        self.servo = servo
        self.reader = reader
        self._rng = rng or random.Random()
        self.current: Status = cfg.initial_status
        self._started = False
        self._closed = False

    # ------------------------------------------------------------ StressCycle
    def setup(self) -> None:
        self.reader.monitor.flush_input()
        self.reader.start()
        self._started = True
        log.info("设备日志后台监听已启动，等待 %.1fs", self.cfg.startup_wait_s)
        self._wait(self.cfg.startup_wait_s)
        log.info("初始化舵机位置（抬起）...")
        self._move(self.cfg.high_position, self.cfg.high_stay_s, "抬起复位")
        log.info("初始设备状态: %s", STATUS_LABEL[self.current])

    def run_cycle(self, index: int) -> CycleResult:
        cfg = self.cfg
        if index > 1:
            self._wait(self._rng.uniform(cfg.gap_min_s, cfg.gap_max_s))

        target = _opposite(self.current)
        log.info("当前状态: %s -> 期望状态: %s", STATUS_LABEL[self.current], STATUS_LABEL[target])
        # 每轮动作前清空本轮日志缓冲，保证判定不受历史日志污染
        self.reader.snapshot(clear=True)

        stay = cfg.low_stay_on_s if target == "on" else cfg.low_stay_off_s
        try:
            self._move(cfg.low_position, stay, "下压刷卡")
            self._move(cfg.high_position, cfg.high_stay_s, "抬起复位")
        except RelayError as exc:
            return CycleResult(index, False, f"舵机指令发送失败: {exc}")
        self._wait(cfg.settle_s)

        lines = self.reader.snapshot()
        passed, final = self.judge(lines, self.current)
        previous, self.current = self.current, final
        data = {"previous": previous, "target": target, "final": final, "lines": len(lines)}
        if passed:
            return CycleResult(index, True, f"设备已{STATUS_LABEL[final]}", data=data)

        log.debug("--- 失败时段日志片段（末尾 %d 行）Start ---", cfg.fail_tail_lines)
        for line in lines[-cfg.fail_tail_lines:] if cfg.fail_tail_lines else []:
            log.debug(line)
        log.debug("--- 失败时段日志片段 End ---")
        return CycleResult(index, False, f"期望{STATUS_LABEL[target]}，实际{STATUS_LABEL[final]}", data=data)

    def teardown(self, summary: RunSummary) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if summary.aborted:
                log.critical("发现致命异常，终止测试；执行舵机抬起以保留 BUG 现场")
            try:
                self.servo.move(self.cfg.high_position, self.cfg.move_time_ms)
                log.info("舵机已抬起复位")
            except HardwareError as exc:
                log.error("退出时舵机抬起失败: %s", exc)
        finally:
            self.reader.stop()
            close_ports(self.reader.monitor.transport)
            self.servo.close()
            summary.extra.update({
                "最终设备状态": STATUS_LABEL[self.current],
                "异常关键字次数": self.reader.monitor.evaluator.exception_count,
            })

    # ------------------------------------------------------------ 判定
    def judge(self, lines: Sequence[str], current: Status) -> tuple[bool, Status]:
        """按状态关键字最后一次出现的位置判定最终状态；都未出现则视为维持原状态（失败）。"""
        prep = normalize if self.cfg.status_match_mode == "normalized" else strip_ansi
        text = "\n".join(prep(line) for line in lines)
        pos_on = max(text.rfind(prep(k)) for k in self.cfg.status_on)
        pos_off = max(text.rfind(prep(k)) for k in self.cfg.status_off)
        if pos_on == -1 and pos_off == -1:
            return False, current
        if pos_on > pos_off:
            final: Status = "on"
        elif pos_off > pos_on:
            final = "off"
        else:
            final = current
        return final == _opposite(current), final

    # ------------------------------------------------------------ 内部
    def _move(self, position: int, stay_s: float, label: str) -> None:
        log.info("舵机%s -> P%d，停留 %.1fs", label, position, stay_s)
        self.servo.move(position, self.cfg.move_time_ms)
        self._wait(self.cfg.move_time_ms / 1000.0 + stay_s)

    def _wait(self, seconds: float) -> None:
        """可被熔断事件立即打断的等待；熔断或监听线程失效时抛出 TestAbort。"""
        if seconds > 0:
            self.reader.abort_event.wait(seconds)
        self._raise_if_stopped()

    def _raise_if_stopped(self) -> None:
        reader = self.reader
        if reader.abort_event.is_set():
            raise TestAbort(f"实时日志捕获到致命异常: {reader.abort_reason}", keep_power=reader.abort_keep_power)
        if reader.failed_event.is_set() or (self._started and not reader.is_alive()):
            raise TestAbort("设备日志监听线程已退出（串口异常且无法恢复），停止测试", keep_power=True)


def run(
    settings: AppSettings,
    ctx: RunContext,
    *,
    factory: SerialFactory | None = None,
    finder: PortFinder | None = None,
) -> int:
    cfg = settings.section(SECTION, ServoNfcConfig)
    keyword_config(settings)
    log.info("运行目录: %s", ctx.run_dir or "<仅控制台>")

    ports = open_ports(settings, [("device", True), ("servo", False)], factory=factory, finder=finder)
    try:
        device = ports["device"]
        monitor = build_monitor(settings, device)
        reader = BackgroundLogReader(
            monitor,
            reconnect=lambda: try_reconnect(device, 0.0),
            reconnect_interval_s=cfg.reconnect_interval_s,
            recent_lines=cfg.buffer_lines,
        )
        servo = ServoDriver(
            ports["servo"],
            servo_id=cfg.servo_id,
            retries=cfg.servo_retries,
            retry_delay_s=cfg.servo_retry_delay_s,
            open_per_command=True,
        )
        cycle = ServoNfcCycle(cfg, servo, reader)
        summary = StressRunner(
            cycle,
            cfg.cycles,
            station=settings.station,
            stop_on_fail=cfg.stop_on_fail,
            fail_keeps_power=True,
            max_consecutive_failures=cfg.max_consecutive_failures,
        ).run()
    finally:
        close_ports(*ports.values())
    return finish(summary, title="舵机 NFC 开关机压测", notify=cfg.notify)

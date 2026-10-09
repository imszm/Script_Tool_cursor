"""灯光类压测（转向灯 / 大灯）公共逻辑。

旧脚本本质上都是“按固定时序驱动继电器”：开/关某一路、模拟按键、发送单字节开关指令。
这里统一为 *配置驱动的步骤序列*，并在执行时给每个动作打时间戳（标记 mark），
由 :class:`IntervalMeter` 实测亮灯/熄灭/切换间隔，按 :class:`IntervalCheck` 判定是否落在规范区间内。

纯计算（步骤校验、间隔配对、判定、统计）与硬件 IO（串口/继电器，通过参数注入）分离，便于单元测试。

配置结构（``lamp`` 段，见 config/stations/turn_signal.yaml）::

    lamp:
      variant: formal              # 选择方案，可 --set lamp.variant=left_spec 切换
      cycles: null                 # 可选：覆盖所选方案的循环次数
      variants:
        formal:
          serial: relay_rtp        # 对应 serial.<name>
          relay: relays.rtp        # 对应继电器配置段（RelayConfig）
          cycles: 10000
          setup_steps:    [...]
          cycle_steps:    [...]
          teardown_steps: [...]
          checks:         [...]

步骤动作：

* ``ch_on`` / ``ch_off``：继电器通道开/关（不用 on/off：YAML 会把它们解析成布尔值），自动产生标记 ``ch<N>_on`` / ``ch<N>_off``；
* ``press``：按下-保持 ``hold_s``-松开，自动产生 ``ch<N>_on`` 与 ``ch<N>_off``；
* ``command``：发送继电器配置中的命名指令（``relay.commands``）；
* ``raw``：发送任意字节（写法同 :func:`ppx_testkit.settings.parse_bytes`）；
* ``wait``：仅等待。

每个步骤执行动作后记录自动标记与 ``mark``（若配置），然后等待 ``wait_s``。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ppx_testkit.core.factory import endpoint as serial_endpoint
from ppx_testkit.core.factory import open_serial
from ppx_testkit.core.relay.base import RelayConfig, RelayDriver, build_relay
from ppx_testkit.core.report.writers import write_csv, write_html
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.core.serial.transport import SerialFactory, SerialTransport
from ppx_testkit.exceptions import ConfigError, HardwareError
from ppx_testkit.logger import RunContext, get_logger
from ppx_testkit.settings import AppSettings, parse_bytes

log = get_logger(__name__)

StepAction = Literal["ch_on", "ch_off", "press", "command", "raw", "wait"]
_CHANNEL_ACTIONS = ("ch_on", "ch_off", "press")


# ============================================================ 配置
@dataclass(frozen=True)
class LampStep:
    action: StepAction
    channel: int | None = None
    command: str | None = None
    data: Any = None
    hold_s: float = 0.3
    wait_s: float = 0.0
    mark: str | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if self.action in _CHANNEL_ACTIONS:
            if self.channel is None:
                raise ConfigError(f"步骤 {self.action} 必须指定 channel")
            if not 1 <= self.channel <= 0xFF:
                raise ConfigError(f"步骤 {self.action} 的通道号非法: {self.channel}")
        if self.action == "command" and not self.command:
            raise ConfigError("步骤 command 必须指定 command（继电器命名指令）")
        if self.action == "raw":
            if self.data is None:
                raise ConfigError("步骤 raw 必须指定 data（待发送字节）")
            parse_bytes(self.data, "lamp.step.data")
        if self.hold_s < 0 or self.wait_s < 0:
            raise ConfigError(f"步骤 {self.action} 的 hold_s / wait_s 不能为负数")
        if self.mark is not None and not self.mark.strip():
            raise ConfigError("步骤 mark 不能为空字符串")

    def auto_marks(self) -> tuple[str, ...]:
        if self.action == "ch_on":
            return (f"ch{self.channel}_on",)
        if self.action == "ch_off":
            return (f"ch{self.channel}_off",)
        if self.action == "press":
            return (f"ch{self.channel}_on", f"ch{self.channel}_off")
        return ()

    def marks(self) -> tuple[str, ...]:
        return self.auto_marks() + ((self.mark,) if self.mark else ())

    def payload(self) -> bytes:
        return parse_bytes(self.data, "lamp.step.data")

    def describe(self) -> str:
        if self.label:
            return self.label
        if self.action in _CHANNEL_ACTIONS:
            return f"{self.action} CH{self.channel}"
        if self.action == "command":
            return f"指令 {self.command}"
        if self.action == "raw":
            return f"原始字节 {self.payload().hex(' ').upper()}"
        return f"等待 {self.wait_s}s"


@dataclass(frozen=True)
class IntervalCheck:
    """从标记 ``start`` 到其后第一个 ``end`` 的间隔，应落在 [min_s, max_s] 内。

    ``start == end`` 时按出现顺序两两配对（第 1/2 次、第 3/4 次 ……）。
    ``enforce=False`` 只记录实测值（非规范间隔方案），不参与判定。
    """

    name: str
    start: str
    end: str
    min_s: float = 0.0
    max_s: float | None = None
    enforce: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ConfigError("间隔检查 name 不能为空")
        if self.min_s < 0:
            raise ConfigError(f"间隔检查 '{self.name}' 的 min_s 不能为负数")
        if self.max_s is not None and self.max_s < self.min_s:
            raise ConfigError(f"间隔检查 '{self.name}' 的 max_s({self.max_s}) 小于 min_s({self.min_s})")

    def within(self, duration_s: float) -> bool:
        if duration_s < self.min_s:
            return False
        return self.max_s is None or duration_s <= self.max_s

    def describe_range(self) -> str:
        upper = "∞" if self.max_s is None else f"{self.max_s:.3f}"
        return f"[{self.min_s:.3f}, {upper}]s"


@dataclass(frozen=True)
class LampProfile:
    cycle_steps: list[LampStep]
    description: str = ""
    serial: str = "relay"
    relay: str = "relay"
    cycles: int = 10
    setup_steps: list[LampStep] = field(default_factory=list)
    teardown_steps: list[LampStep] = field(default_factory=list)
    checks: list[IntervalCheck] = field(default_factory=list)
    stop_on_fail: bool = False
    max_consecutive_failures: int | None = 3

    def __post_init__(self) -> None:
        if self.cycles < 1:
            raise ConfigError(f"cycles 至少为 1，实际 {self.cycles}")
        if not self.cycle_steps:
            raise ConfigError("cycle_steps 不能为空")
        if self.max_consecutive_failures is not None and self.max_consecutive_failures < 1:
            raise ConfigError("max_consecutive_failures 至少为 1（或设为 null 关闭）")
        names = [c.name for c in self.checks]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ConfigError(f"间隔检查名称重复: {dup}")
        # 间隔只在循环内计量（准备阶段结束后计量器清零），故标记必须来自 cycle_steps
        available = cycle_marks(self.cycle_steps)
        for c in self.checks:
            missing = [m for m in (c.start, c.end) if m not in available]
            if missing:
                raise ConfigError(
                    f"间隔检查 '{c.name}' 引用的标记 {missing} 不会在 cycle_steps 中产生；可用标记: {sorted(available)}"
                )

    def all_steps(self) -> list[LampStep]:
        return [*self.setup_steps, *self.cycle_steps, *self.teardown_steps]


@dataclass(frozen=True)
class LampConfig:
    variant: str
    variants: dict[str, LampProfile]
    cycles: int | None = None

    def __post_init__(self) -> None:
        if not self.variants:
            raise ConfigError("lamp.variants 不能为空")
        if self.variant not in self.variants:
            raise ConfigError(f"lamp.variant '{self.variant}' 不存在，可用方案: {sorted(self.variants)}")
        if self.cycles is not None and self.cycles < 1:
            raise ConfigError(f"lamp.cycles 至少为 1，实际 {self.cycles}")

    def profile(self) -> LampProfile:
        return self.variants[self.variant]

    def total_cycles(self) -> int:
        return self.cycles if self.cycles is not None else self.profile().cycles


def cycle_marks(steps: Iterable[LampStep]) -> set[str]:
    return {m for s in steps for m in s.marks()}


def validate_profile_against_relay(profile: LampProfile, relay_cfg: RelayConfig) -> None:
    """在打开串口之前确认步骤引用的命名指令 / 通道都在继电器配置中存在。"""
    for step in profile.all_steps():
        if step.action == "command" and step.command not in relay_cfg.commands:
            raise ConfigError(
                f"步骤引用了未配置的继电器指令 '{step.command}'（继电器段 {profile.relay}），"
                f"可用: {sorted(relay_cfg.commands)}"
            )
        if step.action in _CHANNEL_ACTIONS and relay_cfg.type == "command" and step.channel not in relay_cfg.channels:
            raise ConfigError(
                f"步骤引用了未配置的继电器通道 {step.channel}（继电器段 {profile.relay}），"
                f"可用: {sorted(relay_cfg.channels)}"
            )


# ============================================================ 间隔计量与判定（纯逻辑）
class IntervalMeter:
    """流式间隔计量：逐个喂入 (时间戳, 标记)，返回本次完成的 (检查项, 间隔秒)。

    采用流式而非保存全部事件，万次循环也不会累积内存，且 ``start == end`` 的奇偶配对不会因截断而错位。
    """

    def __init__(self, checks: Sequence[IntervalCheck]) -> None:
        self.checks = list(checks)
        self._pending: dict[str, float | None] = {c.name: None for c in self.checks}

    def reset(self) -> None:
        for name in self._pending:
            self._pending[name] = None

    def feed(self, t: float, mark: str) -> list[tuple[IntervalCheck, float]]:
        done: list[tuple[IntervalCheck, float]] = []
        for c in self.checks:
            started = self._pending[c.name]
            if started is not None and mark == c.end:
                done.append((c, t - started))
                self._pending[c.name] = None
                continue
            if mark == c.start:
                # 重复出现的 start 以最近一次为准
                self._pending[c.name] = t
        return done


def pair_intervals(events: Iterable[tuple[float, str]], start: str, end: str) -> list[float]:
    """离线版本：对完整事件序列计算 start->end 的全部间隔。"""
    meter = IntervalMeter([IntervalCheck(name="_", start=start, end=end)])
    out: list[float] = []
    for t, mark in events:
        out.extend(d for _, d in meter.feed(t, mark))
    return out


@dataclass
class CheckOutcome:
    name: str
    enforce: bool
    durations: list[float] = field(default_factory=list)
    violations: list[float] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not (self.enforce and self.violations)


def evaluate_intervals(
    measured: Iterable[tuple[IntervalCheck, float]], checks: Sequence[IntervalCheck]
) -> list[CheckOutcome]:
    """把一轮内完成的间隔按检查项归类并判定。未测到的检查项返回空 durations（不视为失败）。"""
    outcomes = {c.name: CheckOutcome(name=c.name, enforce=c.enforce) for c in checks}
    for check, duration in measured:
        oc = outcomes[check.name]
        oc.durations.append(duration)
        if not check.within(duration):
            oc.violations.append(duration)
    return [outcomes[c.name] for c in checks]


@dataclass
class IntervalStats:
    name: str
    range_text: str
    enforce: bool
    count: int = 0
    violations: int = 0
    min_s: float | None = None
    max_s: float | None = None
    total_s: float = 0.0

    def add(self, duration_s: float, ok: bool) -> None:
        self.count += 1
        self.total_s += duration_s
        self.min_s = duration_s if self.min_s is None else min(self.min_s, duration_s)
        self.max_s = duration_s if self.max_s is None else max(self.max_s, duration_s)
        if not ok:
            self.violations += 1

    @property
    def mean_s(self) -> float | None:
        return self.total_s / self.count if self.count else None

    def render(self) -> str:
        if not self.count:
            return f"规范 {self.range_text} | 未采集到样本"
        mode = "判定" if self.enforce else "仅记录"
        return (
            f"规范 {self.range_text}({mode}) | n={self.count} 最小={self.min_s:.3f}s 最大={self.max_s:.3f}s "
            f"平均={self.mean_s:.3f}s 超限={self.violations}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "range": self.range_text,
            "enforce": self.enforce,
            "count": self.count,
            "violations": self.violations,
            "min_s": None if self.min_s is None else round(self.min_s, 4),
            "max_s": None if self.max_s is None else round(self.max_s, 4),
            "mean_s": None if self.mean_s is None else round(self.mean_s, 4),
        }


def cycle_verdict(outcomes: Sequence[CheckOutcome], checks: Sequence[IntervalCheck]) -> tuple[bool, str]:
    """返回 (是否通过, 说明文字)。"""
    by_name = {c.name: c for c in checks}
    problems = []
    parts = []
    for oc in outcomes:
        if oc.durations:
            parts.append(f"{oc.name}=" + "/".join(f"{d:.3f}" for d in oc.durations) + "s")
        if not oc.passed:
            rng = by_name[oc.name].describe_range()
            problems.append(f"{oc.name} 超出规范 {rng}: " + "/".join(f"{d:.3f}s" for d in oc.violations))
    if problems:
        return False, "；".join(problems)
    return True, " ".join(parts)


# ============================================================ 执行（硬件 IO 注入）
class LampCycle:
    """实现 :class:`ppx_testkit.core.runner.StressCycle`。"""

    def __init__(
        self,
        profile: LampProfile,
        *,
        open_transport: Callable[[], SerialTransport],
        make_relay: Callable[[SerialTransport], RelayDriver],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.profile = profile
        self._open_transport = open_transport
        self._make_relay = make_relay
        self._sleep = sleep
        self._clock = clock
        self.transport: SerialTransport | None = None
        self.relay: RelayDriver | None = None
        self.meter = IntervalMeter(profile.checks)
        self.stats = {
            c.name: IntervalStats(name=c.name, range_text=c.describe_range(), enforce=c.enforce)
            for c in profile.checks
        }
        self.rows: list[dict[str, Any]] = []
        self._measured: list[tuple[IntervalCheck, float]] = []

    # ---------------------------------------------------------- StressCycle
    def setup(self) -> None:
        self.transport = self._open_transport()
        self.relay = self._make_relay(self.transport)
        if self.profile.setup_steps:
            log.info("执行准备步骤（%d 步）", len(self.profile.setup_steps))
            self._run_steps(self.profile.setup_steps)
        self.meter.reset()

    def run_cycle(self, index: int) -> CycleResult:
        self._measured = []
        t0 = self._clock()
        try:
            self._run_steps(self.profile.cycle_steps)
        except HardwareError as exc:
            log.error("第 %d 轮串口/继电器异常: %s", index, exc)
            self.meter.reset()
            self._release_channels()
            result = CycleResult(index, False, f"串口/继电器异常: {exc}", duration_s=self._clock() - t0,
                                 data={"error": str(exc)})
            self._add_row(result, [])
            return result

        outcomes = evaluate_intervals(self._measured, self.profile.checks)
        by_name = {c.name: c for c in self.profile.checks}
        for oc in outcomes:
            check = by_name[oc.name]
            for d in oc.durations:
                self.stats[oc.name].add(d, check.within(d))
        passed, detail = cycle_verdict(outcomes, self.profile.checks)
        result = CycleResult(
            index,
            passed,
            detail,
            duration_s=self._clock() - t0,
            data={"intervals": {oc.name: [round(d, 4) for d in oc.durations] for oc in outcomes}},
        )
        self._add_row(result, outcomes)
        return result

    def teardown(self, summary: RunSummary) -> None:
        try:
            if self.relay is not None:
                for step in self.profile.teardown_steps:
                    try:
                        self._execute(step)
                    except HardwareError:
                        log.exception("收尾步骤 [%s] 执行失败（继续执行后续收尾）", step.describe())
                self._release_channels()
        finally:
            if self.transport is not None:
                self.transport.close()

    # ---------------------------------------------------------- 内部
    def _run_steps(self, steps: Sequence[LampStep]) -> None:
        for step in steps:
            self._execute(step)

    def _mark(self, mark: str) -> None:
        t = self._clock()
        log.debug("标记 %s @ %.4f", mark, t)
        self._measured.extend(self.meter.feed(t, mark))

    def _execute(self, step: LampStep) -> None:
        relay = self.relay
        if relay is None:
            raise HardwareError("继电器尚未初始化")
        action = step.action
        if action == "ch_on":
            relay.on(int(step.channel or 0))
        elif action == "ch_off":
            relay.off(int(step.channel or 0))
        elif action == "press":
            self._press(relay, int(step.channel or 0), step.hold_s)
        elif action == "command":
            relay.send(str(step.command))
        elif action == "raw":
            relay.send_raw(step.payload(), step.label or "raw")

        if action != "press":  # press 在 _press 内部按真实时刻打标记
            for m in step.auto_marks():
                self._mark(m)
        if step.mark:
            self._mark(step.mark)
        if step.wait_s > 0:
            self._sleep(step.wait_s)

    def _press(self, relay: RelayDriver, channel: int, hold_s: float) -> None:
        relay.on(channel)
        self._mark(f"ch{channel}_on")
        try:
            self._sleep(hold_s)
        except BaseException:
            # Ctrl+C 等中断：尽力松开后原样抛出，不让松开失败掩盖中断
            self._safe_off(relay, channel)
            raise
        relay.off(channel)
        self._mark(f"ch{channel}_off")

    def _safe_off(self, relay: RelayDriver, channel: int) -> None:
        try:
            relay.off(channel)
        except HardwareError:
            log.exception("松开继电器 CH%d 失败", channel)

    def _release_channels(self) -> None:
        """把本次运行中处于闭合状态的通道全部断开（单个失败不影响其余通道）。"""
        relay = self.relay
        if relay is None:
            return
        for ch, on in sorted(relay.state.items()):
            if on:
                log.warning("继电器 CH%d 仍处于闭合状态，尝试断开", ch)
                self._safe_off(relay, ch)

    def _add_row(self, result: CycleResult, outcomes: Sequence[CheckOutcome]) -> None:
        row: dict[str, Any] = {
            "cycle": result.index,
            "verdict": "PASS" if result.passed else "FAIL",
            "duration_s": round(result.duration_s, 3),
            "detail": result.detail,
        }
        for oc in outcomes:
            row[oc.name] = "/".join(f"{d:.3f}" for d in oc.durations)
        self.rows.append(row)


# ============================================================ 应用入口
def run_lamp(
    settings: AppSettings,
    ctx: RunContext,
    *,
    title: str,
    section: str = "lamp",
    serial_factory: SerialFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    cfg = settings.section(section, LampConfig)
    profile = cfg.profile()
    relay_cfg = settings.section(profile.relay, RelayConfig)
    validate_profile_against_relay(profile, relay_cfg)
    serial_endpoint(settings, profile.serial)  # 提前校验串口配置，配置错误不进入硬件流程
    total = cfg.total_cycles()

    log.info("%s | 方案: %s | %s | 循环 %d 次", title, cfg.variant, profile.description, total)
    for c in profile.checks:
        log.info("间隔检查 [%s]: %s -> %s，规范 %s%s", c.name, c.start, c.end, c.describe_range(),
                 "" if c.enforce else "（仅记录）")

    cycle = LampCycle(
        profile,
        open_transport=lambda: open_serial(settings, profile.serial, factory=serial_factory),
        make_relay=lambda t: build_relay(relay_cfg, t, sleep=sleep),
        sleep=sleep,
        clock=clock,
    )
    runner = StressRunner(
        cycle,
        total,
        station=settings.station,
        stop_on_fail=profile.stop_on_fail,
        max_consecutive_failures=profile.max_consecutive_failures,
    )
    summary = runner.run()

    summary.extra["方案"] = f"{cfg.variant} ({profile.description})" if profile.description else cfg.variant
    for name, st in cycle.stats.items():
        summary.extra[f"间隔[{name}]"] = st.render()
        if st.enforce and st.count == 0 and summary.executed:
            log.warning("间隔检查 [%s] 未采集到任何样本，请确认标记配置与循环次数", name)
    for name, st in cycle.stats.items():
        log.info("间隔统计 [%s]: %s", name, st.render())

    _write_reports(ctx, title, cfg.variant, summary, cycle)
    return 0 if summary.ok else 1


def _write_reports(ctx: RunContext, title: str, variant: str, summary: RunSummary, cycle: LampCycle) -> None:
    ctx.write_json("summary.json", summary.to_dict())
    ctx.write_json("interval_stats.json", [st.to_dict() for st in cycle.stats.values()])
    columns = ["cycle", "verdict", "duration_s", *cycle.stats.keys(), "detail"]
    csv_path = ctx.artifact("lamp_cycles.csv")
    if csv_path is not None:
        write_csv(csv_path, cycle.rows, columns)
    html_path = ctx.artifact("report.html")
    if html_path is not None:
        head = {
            "方案": variant,
            "执行/目标": f"{summary.executed}/{summary.target_cycles}",
            "通过/失败": f"{summary.passed}/{summary.failed}",
            "通过率": f"{summary.pass_rate:.2f}%",
            "结论": "PASS" if summary.ok else "FAIL",
        }
        head.update({f"间隔[{k}]": st.render() for k, st in cycle.stats.items()})
        write_html(html_path, title=title, summary=head, rows=cycle.rows, columns=columns)

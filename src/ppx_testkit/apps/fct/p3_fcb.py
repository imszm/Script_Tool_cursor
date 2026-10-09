"""P3 FCB 四通道 SMT FCT 自动测试。

迁移自 ``P3系列SMT&组装升级测试工具/P3系列SMT_FCT测试工具.py``。每轮流程::

    键盘依次输入 N 个 SN（焦点需预先在第 1 个输入框）
    -> 并行监控各通道状态机：
       WAIT_STATUS（5 个状态灯连续 N 帧全绿 -> 点击“通过”）
       -> WAIT_DATABASE（数据库灯连续 N 帧绿）
       -> WAIT_FINAL_PASS（左侧最终 PASS 区域连续 N 帧绿）-> PASS
       任一阶段超时或通道总超时 -> FAIL（保存截图）
    -> 统计整机 / 通道成功率（可持久化到 JSON，跨批次累计）

绿色判定：``占比 >= 阈值`` **或** ``最大连通域面积 >= min_green_area``（与旧脚本一致）。
本轮出现程序异常时，整机与全部通道计 FAIL 并终止测试。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ppx_testkit.apps.fct.common import (
    Point,
    ScreenSettings,
    build_screen,
    is_failsafe,
    require_non_negative,
    save_failure_screenshot,
    write_reports,
)
from ppx_testkit.core.gui.screen import point_to_roi
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.exceptions import ConfigError, TestAbort
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings
from ppx_testkit.utils.sn import serial_sequence

log = logging.getLogger(__name__)

Roi = tuple[int, int, int, int]

WAIT_STATUS = "WAIT_STATUS"
WAIT_DATABASE = "WAIT_DATABASE"
WAIT_FINAL_PASS = "WAIT_FINAL_PASS"
PASS = "PASS"
FAIL = "FAIL"


# ============================================================ 配置
@dataclass(frozen=True)
class ChannelLayout:
    pass_button: Point
    status_points: dict[str, Point]
    database_point: Point
    final_pass_roi: Roi

    def __post_init__(self) -> None:
        if not self.status_points:
            raise ConfigError("channels[].status_points 至少需要一个状态灯")
        x1, y1, x2, y2 = self.final_pass_roi
        if x2 <= x1 or y2 <= y1:
            raise ConfigError(f"channels[].final_pass_roi 非法: {self.final_pass_roi}")


@dataclass(frozen=True)
class P3FctConfig:
    sn_base: str
    channels: list[ChannelLayout]
    loop_count: int = 1
    between_rounds_delay_s: float = 3.0
    sn_suffix_width: int = 6
    advance_sn_each_round: bool = False
    start_delay_s: float = 5.0
    scan_start_delay_s: float = 2.0
    type_interval_s: float = 0.01
    scan_after_enter_delay_s: float = 0.5
    pass_click_settle_s: float = 0.3
    hsv_lower: tuple[int, int, int] = (35, 80, 60)
    hsv_upper: tuple[int, int, int] = (90, 255, 255)
    status_x_radius: int = 22
    status_y_radius: int = 45
    database_x_radius: int = 22
    database_y_radius: int = 45
    dot_green_ratio: float = 0.20
    pass_green_ratio: float = 0.20
    min_green_area: int = 180
    green_confirm_count: int = 3
    poll_interval_s: float = 0.2
    status_timeout_s: float = 120.0
    database_timeout_s: float = 30.0
    final_pass_timeout_s: float = 30.0
    channel_total_timeout_s: float = 180.0
    status_log_interval_s: float = 1.0
    save_timeout_screenshot: bool = True
    save_layout_screenshot: bool = True
    debug_recognition: bool = True
    statistics_file: Path | None = None
    screen: ScreenSettings = field(default_factory=ScreenSettings)

    def __post_init__(self) -> None:
        if not self.channels:
            raise ConfigError("fct.channels 至少需要一个通道")
        if self.loop_count <= 0 or self.green_confirm_count <= 0:
            raise ConfigError("fct.loop_count / fct.green_confirm_count 必须 > 0")
        if self.sn_suffix_width <= 0 or len(self.sn_base) < self.sn_suffix_width:
            raise ConfigError("fct.sn_base 长度必须 >= fct.sn_suffix_width，且 sn_suffix_width > 0")
        if not self.sn_base[-self.sn_suffix_width:].isdigit():
            raise ConfigError(f"fct.sn_base 末尾 {self.sn_suffix_width} 位必须为数字: {self.sn_base}")
        for lo, hi in zip(self.hsv_lower, self.hsv_upper, strict=True):
            if not 0 <= lo <= hi <= 255:
                raise ConfigError(f"HSV 阈值非法: lower={self.hsv_lower} upper={self.hsv_upper}")
        require_non_negative({
            "fct.between_rounds_delay_s": self.between_rounds_delay_s,
            "fct.start_delay_s": self.start_delay_s,
            "fct.poll_interval_s": self.poll_interval_s,
            "fct.status_timeout_s": self.status_timeout_s,
            "fct.database_timeout_s": self.database_timeout_s,
            "fct.final_pass_timeout_s": self.final_pass_timeout_s,
            "fct.channel_total_timeout_s": self.channel_total_timeout_s,
        })

    @property
    def channel_count(self) -> int:
        return len(self.channels)

    def layout(self, channel: int) -> ChannelLayout:
        return self.channels[channel - 1]


# ============================================================ 纯逻辑：SN / 绿色判定 / 状态机
def generate_sn_list(base: str, width: int, count: int, round_offset: int = 0) -> list[str]:
    """``base`` 末尾 ``width`` 位为起始编号，连续生成 ``count`` 个 SN。"""
    prefix = base[:-width]
    start = int(base[-width:]) + round_offset
    return list(serial_sequence(prefix, start, start + count - 1, width))


def green_decision(ratio: float, max_area: int, ratio_threshold: float, min_area: int) -> bool:
    return ratio >= ratio_threshold or max_area >= min_area


@dataclass(frozen=True)
class GreenResult:
    green: bool
    ratio: float
    area: int


class GreenDetector(Protocol):
    def __call__(self, image: Any, roi: Roi, ratio_threshold: float) -> GreenResult: ...


def make_cv_detector(cfg: P3FctConfig) -> GreenDetector:
    """基于 core.detect_green（OpenCV HSV + 连通域）的检测器；只取占比与面积，判定规则由本模块决定。"""
    from ppx_testkit.core.gui.screen import detect_green

    def _detect(image: Any, roi: Roi, ratio_threshold: float) -> GreenResult:
        d = detect_green(image, roi, cfg.hsv_lower, cfg.hsv_upper)
        return GreenResult(green_decision(d.ratio, d.max_area, ratio_threshold, cfg.min_green_area), d.ratio,
                           d.max_area)

    return _detect


@dataclass
class ChannelState:
    channel: int
    state: str = WAIT_STATUS
    start_time: float = 0.0
    phase_start_time: float = 0.0
    green_streak: int = 0
    fail_reason: str = ""
    pass_button_clicked: bool = False

    @property
    def done(self) -> bool:
        return self.state in (PASS, FAIL)


_PHASE_TIMEOUTS = {
    WAIT_STATUS: ("status_timeout_s", "5个状态灯全部变绿等待超时", "WAIT_STATUS_TIMEOUT"),
    WAIT_DATABASE: ("database_timeout_s", "数据库绿灯等待超时", "WAIT_DATABASE_TIMEOUT"),
    WAIT_FINAL_PASS: ("final_pass_timeout_s", "左侧最终PASS等待超时", "WAIT_FINAL_PASS_TIMEOUT"),
}


def check_timeout(state: ChannelState, now: float, cfg: P3FctConfig) -> tuple[str, str] | None:
    """返回 (失败原因, 截图标签)；未超时返回 None。总超时优先于阶段超时。"""
    if now - state.start_time >= cfg.channel_total_timeout_s:
        return f"通道总测试超时：{cfg.channel_total_timeout_s:g}秒", "CHANNEL_TOTAL_TIMEOUT"
    spec = _PHASE_TIMEOUTS.get(state.state)
    if spec is None:
        return None
    attr, reason, tag = spec
    if now - state.phase_start_time >= getattr(cfg, attr):
        return reason, tag
    return None


def register_green(state: ChannelState, green: bool, confirm_count: int) -> bool:
    """连续 confirm_count 帧为绿才确认；任一帧非绿清零。"""
    if green:
        state.green_streak += 1
        return state.green_streak >= confirm_count
    state.green_streak = 0
    return False


def advance(state: ChannelState, next_state: str, now: float) -> None:
    state.state = next_state
    state.phase_start_time = now
    state.green_streak = 0


def fail(state: ChannelState, reason: str) -> None:
    state.state = FAIL
    state.fail_reason = reason


# ============================================================ 纯逻辑：统计
def create_empty_statistics(channel_count: int) -> dict[str, Any]:
    return {
        "total_rounds": 0,
        "machine": {"pass": 0, "fail": 0},
        "channels": {str(c): {"pass": 0, "fail": 0} for c in range(1, channel_count + 1)},
    }


def normalize_statistics(data: Any, channel_count: int) -> dict[str, Any] | None:
    """校验历史统计结构；与当前通道数不一致或字段缺失时返回 None。"""
    try:
        stats = {
            "total_rounds": int(data["total_rounds"]),
            "machine": {"pass": int(data["machine"]["pass"]), "fail": int(data["machine"]["fail"])},
            "channels": {
                str(c): {"pass": int(data["channels"][str(c)]["pass"]), "fail": int(data["channels"][str(c)]["fail"])}
                for c in range(1, channel_count + 1)
            },
        }
    except (KeyError, TypeError, ValueError):
        return None
    if len(data["channels"]) != channel_count:
        return None
    return stats


def update_statistics(stats: dict[str, Any], states: Mapping[int, ChannelState]) -> bool:
    """按本轮结果累计，返回整机是否 PASS。"""
    stats["total_rounds"] += 1
    machine_pass = True
    for channel, state in sorted(states.items()):
        key = str(channel)
        if state.state == PASS:
            stats["channels"][key]["pass"] += 1
        else:
            stats["channels"][key]["fail"] += 1
            machine_pass = False
    stats["machine"]["pass" if machine_pass else "fail"] += 1
    return machine_pass


def record_round_error(stats: dict[str, Any], channel_count: int) -> None:
    """程序异常：整机与全部通道计 FAIL，保证统计基数一致。"""
    stats["total_rounds"] += 1
    stats["machine"]["fail"] += 1
    for c in range(1, channel_count + 1):
        stats["channels"][str(c)]["fail"] += 1


def calculate_rate(success: int, failure: int) -> tuple[float, float]:
    total = success + failure
    if total <= 0:
        return 0.0, 0.0
    return success / total * 100, failure / total * 100


def render_statistics(stats: Mapping[str, Any], channel_count: int) -> str:
    m_pass, m_fail = stats["machine"]["pass"], stats["machine"]["fail"]
    m_rate, m_frate = calculate_rate(m_pass, m_fail)
    lines = [
        "#" * 80,
        "                  自动测试统计报告",
        "#" * 80,
        f"总测试轮数：{stats['total_rounds']}",
        "-------------------- 整机统计 --------------------",
        f"整机成功次数：{m_pass} | 整机失败次数：{m_fail}",
        f"整机成功率：{m_rate:.2f}% | 整机失败率：{m_frate:.2f}%",
        "-------------------- 通道统计 --------------------",
    ]
    total_pass = total_fail = 0
    for c in range(1, channel_count + 1):
        c_pass, c_fail = stats["channels"][str(c)]["pass"], stats["channels"][str(c)]["fail"]
        total_pass += c_pass
        total_fail += c_fail
        r, fr = calculate_rate(c_pass, c_fail)
        lines.append(f"通道{c}：PASS={c_pass} | FAIL={c_fail} | 成功率={r:.2f}% | 失败率={fr:.2f}%")
    tr, tfr = calculate_rate(total_pass, total_fail)
    lines += [
        "-------------------- 汇总统计 --------------------",
        f"总通道测试次数：{stats['total_rounds'] * channel_count}",
        f"总通道PASS次数：{total_pass} | 总通道FAIL次数：{total_fail}",
        f"总通道成功率：{tr:.2f}% | 总通道失败率：{tfr:.2f}%",
        "#" * 80,
    ]
    return "\n".join(lines)


# ============================================================ 界面流程
class FctScreen(Protocol):
    def click(self, point: Point, *, label: str = "") -> None: ...

    def type_text(self, text: str, *, interval: float = 0.02, enter: bool = False) -> None: ...

    def screenshot_array(self) -> Any: ...

    def save_screenshot(self, path: Path) -> Path | None: ...


class P3FctRound:
    """实现 StressCycle：一轮 = 扫码 + 四通道并行监控。"""

    def __init__(self, cfg: P3FctConfig, screen: FctScreen, detector: GreenDetector, *,
                 ctx: RunContext | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.screen = screen
        self.detector = detector
        self.ctx = ctx
        self._sleep = sleep
        self._clock = clock
        self.statistics = create_empty_statistics(cfg.channel_count)
        self.rows: list[dict[str, Any]] = []
        self._round = 0

    # ------------------------------------------------------------ StressCycle
    def setup(self) -> None:
        cfg = self.cfg
        log.info("皮皮熊 FCB %d 通道自动测试 | 计划轮数 %d | SN 起始值 %s", cfg.channel_count, cfg.loop_count, cfg.sn_base)
        for idx, sn in enumerate(self.sn_list(1), start=1):
            log.info("通道%d：%s", idx, sn)
        log.info("%.0f 秒后开始测试。不会自动点击 SN 输入框，请确认焦点在第 1 个框。", cfg.start_delay_s)
        self._sleep(cfg.start_delay_s)
        if cfg.save_layout_screenshot:
            self.save_layout_screenshot()
        self.statistics = self.load_statistics()

    def run_cycle(self, index: int) -> CycleResult:
        self._round = index
        sn_list = self.sn_list(index)
        try:
            self.scan_barcodes(sn_list)
            states = self.monitor_channels()
        except TestAbort:
            raise
        except Exception as exc:  # noqa: BLE001 - 与旧脚本一致：异常计整机/全通道 FAIL 后终止
            if is_failsafe(exc):
                raise
            log.exception("第%d轮出现异常退出：%s", index, exc)
            record_round_error(self.statistics, self.cfg.channel_count)
            self.save_statistics()
            for ch, sn in enumerate(sn_list, start=1):
                self._record(index, ch, sn, FAIL, f"程序异常: {exc}")
            raise TestAbort(f"第{index}轮程序异常: {exc}") from exc

        machine_pass = update_statistics(self.statistics, states)
        self.save_statistics()
        self._log_round(index, states, machine_pass)
        reasons = []
        for ch, sn in enumerate(sn_list, start=1):
            st = states[ch]
            self._record(index, ch, sn, st.state, st.fail_reason)
            if st.state != PASS:
                reasons.append(f"CH{ch}:{st.fail_reason}")
        return CycleResult(index, machine_pass, "; ".join(reasons),
                           data={"channels": {ch: st.state for ch, st in states.items()}})

    def teardown(self, summary: RunSummary) -> None:
        summary.extra["整机统计"] = self.statistics["machine"]
        log.info("\n%s", render_statistics(self.statistics, self.cfg.channel_count))
        if self.ctx is not None:
            self.ctx.write_json("statistics.json", self.statistics)

    # ------------------------------------------------------------ 步骤
    def sn_list(self, round_index: int) -> list[str]:
        offset = (round_index - 1) * self.cfg.channel_count if self.cfg.advance_sn_each_round else 0
        return generate_sn_list(self.cfg.sn_base, self.cfg.sn_suffix_width, self.cfg.channel_count, offset)

    def scan_barcodes(self, sn_list: Sequence[str]) -> None:
        log.info("开始 %d 通道扫码", len(sn_list))
        self._sleep(self.cfg.scan_start_delay_s)
        for channel, sn in enumerate(sn_list, start=1):
            log.info("通道%d：输入SN -> %s", channel, sn)
            self.screen.type_text(sn, interval=self.cfg.type_interval_s, enter=True)
            self._sleep(self.cfg.scan_after_enter_delay_s)
        log.info("%d 个 SN 全部输入完成", len(sn_list))

    def monitor_channels(self) -> dict[int, ChannelState]:
        cfg = self.cfg
        log.info("开始 %d 通道并行状态监控", cfg.channel_count)
        now = self._clock()
        states = {c: ChannelState(c, start_time=now, phase_start_time=now) for c in range(1, cfg.channel_count + 1)}
        last_status_log = dict.fromkeys(states, float("-inf"))

        while not all(s.done for s in states.values()):
            image = self.screen.screenshot_array()
            now = self._clock()
            for channel, state in states.items():
                if state.done:
                    continue
                timeout = check_timeout(state, now, cfg)
                if timeout is not None:
                    reason, tag = timeout
                    fail(state, reason)
                    log.error("通道%d：%s", channel, reason)
                    if cfg.save_timeout_screenshot:
                        save_failure_screenshot(self.screen, self.ctx, self._shot_name(channel, tag))
                    continue
                self._step(state, image, now, last_status_log)
            if not all(s.done for s in states.values()):
                self._sleep(cfg.poll_interval_s)
        return states

    def _step(self, state: ChannelState, image: Any, now: float, last_status_log: dict[int, float]) -> None:
        cfg = self.cfg
        channel = state.channel
        layout = cfg.layout(channel)

        if state.state == WAIT_STATUS:
            results = {
                name: self.detector(image, point_to_roi(pt, cfg.status_x_radius, cfg.status_y_radius),
                                    cfg.dot_green_ratio)
                for name, pt in layout.status_points.items()
            }
            if cfg.debug_recognition and now - last_status_log[channel] >= cfg.status_log_interval_s:
                last_status_log[channel] = now
                log.info("通道%d状态：%s", channel, " | ".join(
                    f"{n}={'绿' if r.green else '灰'}(ratio={r.ratio:.2f},area={r.area})" for n, r in results.items()))
            all_green = all(r.green for r in results.values())
            if register_green(state, all_green, cfg.green_confirm_count):
                log.info("通道%d：所有状态灯PASS，尝试点击通过", channel)
                try:
                    self.screen.click(layout.pass_button, label=f"通道{channel}通过")
                    self._sleep(cfg.pass_click_settle_s)
                except Exception as exc:  # noqa: BLE001 - 点击失败只影响本通道
                    if is_failsafe(exc):
                        raise
                    fail(state, f"点击通过按钮异常：{exc}")
                    log.exception("通道%d：%s", channel, state.fail_reason)
                    return
                state.pass_button_clicked = True
                advance(state, WAIT_DATABASE, now)
            elif all_green:
                log.info("通道%d：状态灯全部检测为绿色 [%d/%d]", channel, state.green_streak, cfg.green_confirm_count)
            return

        if state.state == WAIT_DATABASE:
            roi = point_to_roi(layout.database_point, cfg.database_x_radius, cfg.database_y_radius)
            r = self.detector(image, roi, cfg.dot_green_ratio)
            if register_green(state, r.green, cfg.green_confirm_count):
                log.info("通道%d：数据库PASS", channel)
                advance(state, WAIT_FINAL_PASS, now)
            elif r.green:
                log.info("通道%d：数据库检测为绿色 [%d/%d]", channel, state.green_streak, cfg.green_confirm_count)
            return

        if state.state == WAIT_FINAL_PASS:
            r = self.detector(image, layout.final_pass_roi, cfg.pass_green_ratio)
            if register_green(state, r.green, cfg.green_confirm_count):
                state.state = PASS
                log.info("通道%d：================ PASS ================", channel)
            elif r.green:
                log.info("通道%d：最终PASS检测为绿色 [%d/%d]", channel, state.green_streak, cfg.green_confirm_count)

    # ------------------------------------------------------------ 统计持久化
    def load_statistics(self) -> dict[str, Any]:
        path = self.cfg.statistics_file
        count = self.cfg.channel_count
        if path is None or not path.exists():
            return create_empty_statistics(count)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.exception("统计文件读取失败，重置为新表：%s", exc)
            return create_empty_statistics(count)
        stats = normalize_statistics(data, count)
        if stats is None:
            log.warning("统计文件 %s 结构不符（通道数应为 %d），重置为新表", path, count)
            return create_empty_statistics(count)
        log.info("已读取历史统计数据: %s", path)
        return stats

    def save_statistics(self) -> None:
        path = self.cfg.statistics_file
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.statistics, ensure_ascii=False, indent=4), encoding="utf-8")
        except OSError as exc:
            log.exception("统计文件保存失败：%s", exc)

    # ------------------------------------------------------------ 调试输出
    def save_layout_screenshot(self) -> None:
        """绘制各识别区域边框并保存，辅助校准坐标（需要 OpenCV）。"""
        if self.ctx is None:
            return
        path = self.ctx.artifact(f"layout_{_dt.datetime.now():%Y%m%d_%H%M%S}.png")
        if path is None:
            return
        try:
            import cv2  # type: ignore[import-untyped]

            cfg = self.cfg
            image = cv2.cvtColor(self.screen.screenshot_array(), cv2.COLOR_RGB2BGR)

            def box(roi: Roi, label: str, color: tuple[int, int, int]) -> None:
                x1, y1, x2, y2 = roi
                cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
                cv2.putText(image, label, (x1, max(10, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1,
                            cv2.LINE_AA)

            for ch in range(1, cfg.channel_count + 1):
                layout = cfg.layout(ch)
                for name, pt in layout.status_points.items():
                    box(point_to_roi(pt, cfg.status_x_radius, cfg.status_y_radius), f"CH{ch}-{name}", (0, 0, 255))
                box(point_to_roi(layout.database_point, cfg.database_x_radius, cfg.database_y_radius),
                    f"CH{ch}-DB", (255, 0, 0))
                box(layout.final_pass_roi, f"CH{ch}-FINAL", (0, 255, 255))
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), image)
            log.info("ROI调试截图已保存：%s", path)
        except Exception as exc:  # noqa: BLE001 - 调试图失败不影响测试
            if is_failsafe(exc):
                raise
            log.exception("保存ROI调试图失败：%s", exc)

    def _shot_name(self, channel: int, tag: str) -> str:
        return f"round{self._round:04d}_CH{channel}_{tag}_{_dt.datetime.now():%Y%m%d_%H%M%S}.png"

    def _log_round(self, index: int, states: Mapping[int, ChannelState], machine_pass: bool) -> None:
        log.info("=" * 70)
        log.info("第%d轮测试结果", index)
        for ch, st in sorted(states.items()):
            if st.state == PASS:
                log.info("通道%d：PASS", ch)
            else:
                log.error("通道%d：FAIL, 失败原因：%s", ch, st.fail_reason)
        (log.info if machine_pass else log.error)("整机结果：%s", "PASS" if machine_pass else "FAIL")
        log.info("=" * 70)

    def _record(self, index: int, channel: int, sn: str, state: str, reason: str) -> None:
        self.rows.append({"round": index, "channel": channel, "sn": sn,
                          "verdict": "PASS" if state == PASS else "FAIL", "fail_reason": reason})


def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("fct", P3FctConfig)
    screen = build_screen(cfg.screen)
    cycle = P3FctRound(cfg, screen, make_cv_detector(cfg), ctx=ctx)
    summary = StressRunner(cycle, cfg.loop_count, station=settings.station,
                           interval_s=cfg.between_rounds_delay_s).run()
    write_reports(ctx, "report", title=f"{settings.station} 多通道 FCT 测试报告", rows=cycle.rows,
                  summary=summary.to_dict(), columns=["round", "channel", "sn", "verdict", "fail_reason"])
    return 0 if summary.ok else 1

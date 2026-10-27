"""P3 四通道状态机与绿色判定（纯函数，不依赖 OpenCV 或屏幕）。

P3 工位不使用 ppx_region.dll。通道阶段名和超时规则集中在这里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

WAIT_STATUS = "WAIT_STATUS"
WAIT_DATABASE = "WAIT_DATABASE"
WAIT_FINAL_PASS = "WAIT_FINAL_PASS"
PASS = "PASS"
FAIL = "FAIL"


class PhaseClock(Protocol):
    """超时判定需要的配置字段。P3FctConfig 满足该协议。"""

    channel_total_timeout_s: float
    status_timeout_s: float
    database_timeout_s: float
    final_pass_timeout_s: float


def green_decision(ratio: float, max_area: int, ratio_threshold: float, min_area: int) -> bool:
    """占比或最大连通域任一达到阈值即视为绿色。"""
    return ratio >= ratio_threshold or max_area >= min_area


@dataclass
class ChannelState:
    """单个通道在一轮测试中的状态。"""

    channel: int
    state: str = WAIT_STATUS
    start_time: float = 0.0
    phase_start_time: float = 0.0
    green_streak: int = 0
    fail_reason: str = ""
    pass_button_clicked: bool = False

    @property
    def done(self) -> bool:
        """已经 PASS 或 FAIL 时不再推进。"""
        return self.state in (PASS, FAIL)


_PHASE_TIMEOUTS = {
    WAIT_STATUS: ("status_timeout_s", "5个状态灯全部变绿等待超时", "WAIT_STATUS_TIMEOUT"),
    WAIT_DATABASE: ("database_timeout_s", "数据库绿灯等待超时", "WAIT_DATABASE_TIMEOUT"),
    WAIT_FINAL_PASS: ("final_pass_timeout_s", "左侧最终PASS等待超时", "WAIT_FINAL_PASS_TIMEOUT"),
}


def check_timeout(state: ChannelState, now: float, cfg: PhaseClock) -> tuple[str, str] | None:
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
    """进入下一阶段并清零连续绿帧计数。"""
    state.state = next_state
    state.phase_start_time = now
    state.green_streak = 0


def fail(state: ChannelState, reason: str) -> None:
    """把通道标为失败并记下原因。"""
    state.state = FAIL
    state.fail_reason = reason

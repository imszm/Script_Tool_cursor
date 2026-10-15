"""设备日志关键字判定（纯逻辑，无 IO）。

匹配模式
--------
* ``normalized``（默认）：关键字与日志行都去 ANSI、转小写、去全部空白后做子串匹配。
  与旧脚本的 ``lower().replace(" ", "")`` 以及舵机脚本的 ``\\s*`` 模糊正则等价。
* ``exact``：去 ANSI 后区分大小写的子串匹配（W3 开关机脚本的旧行为）。

成功关键字除逐行匹配外，还会在“跨行拼接缓冲区”中匹配，以容忍设备日志被拆行。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from ppx_testkit.core.monitor.rate_limiter import SlidingWindowCounter
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.utils.ansi import normalize, strip_ansi


@dataclass(frozen=True)
class RateRule:
    keyword: str
    window_s: float
    count: int
    severity: Literal["error", "critical"] = "error"
    keep_power: bool = True

    def __post_init__(self) -> None:
        if not self.keyword.strip():
            raise ConfigError("rate_rules.keyword 不能为空")
        if self.window_s <= 0 or self.count <= 0:
            raise ConfigError(f"rate_rules '{self.keyword}': window_s 与 count 必须 > 0")


@dataclass(frozen=True)
class KeywordConfig:
    success: list[str] = field(default_factory=list)
    exception: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    abort: list[str] = field(default_factory=list)
    abort_keep_power: bool = True
    rate_rules: list[RateRule] = field(default_factory=list)
    match_mode: Literal["normalized", "exact"] = "normalized"
    concat_buffer_max: int = 2048

    def __post_init__(self) -> None:
        if self.concat_buffer_max < 0:
            raise ConfigError("concat_buffer_max 不能为负数")
        for group in (self.success, self.exception, self.info, self.abort):
            if any(not kw.strip() for kw in group):
                raise ConfigError("关键字列表中存在空字符串")


@dataclass
class LineVerdict:
    line: str
    success: str | None = None
    success_source: Literal["line", "buffer"] | None = None
    exceptions: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)
    abort_reason: str | None = None
    abort_keep_power: bool = True

    @property
    def should_abort(self) -> bool:
        return self.abort_reason is not None


class KeywordEvaluator:
    def __init__(self, cfg: KeywordConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self._prep = normalize if cfg.match_mode == "normalized" else strip_ansi
        self._success = [(kw, self._prep(kw)) for kw in cfg.success]
        self._exception = [(kw, self._prep(kw)) for kw in cfg.exception]
        self._info = [(kw, self._prep(kw)) for kw in cfg.info]
        self._abort = [(kw, self._prep(kw)) for kw in cfg.abort]
        self._rates = [(rule, self._prep(rule.keyword), SlidingWindowCounter(rule.window_s, rule.count, clock))
                       for rule in cfg.rate_rules]
        self._buffer = ""
        self.cycle_success: str | None = None
        self.exception_count = 0

    def reset_cycle(self) -> None:
        """新一轮开始：清空跨行缓冲与本轮成功状态（频率计数跨轮保留）。"""
        self._buffer = ""
        self.cycle_success = None

    def reset_rates(self) -> None:
        for _, _, counter in self._rates:
            counter.reset()

    def feed(self, line: str) -> LineVerdict:
        verdict = LineVerdict(line=line)
        text = self._prep(line)
        if not text:
            return verdict

        verdict.infos = [kw for kw, k in self._info if k in text]
        verdict.exceptions = [kw for kw, k in self._exception if k in text]
        self.exception_count += len(verdict.exceptions)

        for kw, k in self._abort:
            if k in text:
                verdict.abort_reason = f"命中致命关键字 '{kw}'"
                verdict.abort_keep_power = self.cfg.abort_keep_power
                break

        if verdict.abort_reason is None:
            for rule, k, counter in self._rates:
                if k in text and counter.hit():
                    level = "致命" if rule.severity == "critical" else "频繁错误"
                    verdict.abort_reason = (
                        f"{level}熔断：{rule.window_s:g} 秒内检测到 {rule.count} 次 '{rule.keyword}'"
                    )
                    verdict.abort_keep_power = rule.keep_power
                    break

        if self.cycle_success is None and self._success:
            for kw, k in self._success:
                if k in text:
                    verdict.success, verdict.success_source = kw, "line"
                    break
            if self.cfg.concat_buffer_max > 0:
                self._buffer = (self._buffer + text)[-self.cfg.concat_buffer_max :]
                if verdict.success is None:
                    for kw, k in self._success:
                        if k in self._buffer:
                            verdict.success, verdict.success_source = kw, "buffer"
                            break
            if verdict.success is not None:
                self.cycle_success = verdict.success

        return verdict

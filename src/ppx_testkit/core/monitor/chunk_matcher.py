"""按数据块匹配设备日志关键字（纯逻辑，无串口 IO）。

R3 充电日志大约每 100ms 输出一大块，关键字可能被切在两次 read() 中间，
也可能夹着空格、Tab 或换行。这里先去掉全部空白再匹配，并保留上一包的尾部，
使断点前后仍能拼上。成功关键字若以数字结尾，后面不能再跟数字，
避免 ``num: 1`` 误命中 ``num: 10``。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ppx_testkit.core.monitor.keyword_rules import KeywordConfig
from ppx_testkit.core.monitor.rate_limiter import SlidingWindowCounter
from ppx_testkit.utils.ansi import normalize

_WS_RE = re.compile(r"\s+")


def compile_success(keyword: str, *, digit_boundary: bool) -> re.Pattern[str]:
    """把成功关键字编译成去空白后的正则。

    :param keyword: 配置里的原文，允许含空格。
    :param digit_boundary: 为真且关键字以数字结尾时，禁止后面再接数字。
    :return: 在压缩文本上搜索的正则。
    """
    compact = normalize(keyword)
    pattern = re.escape(compact)
    if digit_boundary and compact and compact[-1].isdigit():
        pattern += r"(?!\d)"
    return re.compile(pattern)


@dataclass
class ChunkVerdict:
    """一块文本产生的新事件。``success`` 只在本块首次命中时有值。"""

    success: str | None = None
    exceptions: list[str] = field(default_factory=list)
    abort_reason: str | None = None
    abort_keep_power: bool = False
    snippet: str = ""


class ChunkMatcher:
    """跨块关键字判定。频率计数跨轮保留，成功状态在 :meth:`reset_cycle` 时清空。"""

    def __init__(
        self,
        cfg: KeywordConfig,
        *,
        digit_boundary: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.digit_boundary = digit_boundary
        self._success = [(kw, compile_success(kw, digit_boundary=digit_boundary)) for kw in cfg.success]
        self._exception = [normalize(kw) for kw in cfg.exception]
        self._abort = [normalize(kw) for kw in cfg.abort]
        self._rates = [
            (rule, normalize(rule.keyword), SlidingWindowCounter(rule.window_s, rule.count, clock))
            for rule in cfg.rate_rules
        ]
        lengths = [len(normalize(kw)) for kw in (*cfg.success, *cfg.exception, *cfg.abort)]
        lengths.extend(len(normalize(rule.keyword)) for rule in cfg.rate_rules)
        self._keep = max(lengths, default=1) - 1
        self._tail = ""
        self.accept_success = False
        self.success: str | None = None
        self.exception_count = 0

    def reset_cycle(self) -> None:
        """新一轮充电监听开始：丢掉残块，并重新接收成功关键字。频率计数不动。"""
        self._tail = ""
        self.success = None
        self.accept_success = True

    def clear_tail(self) -> None:
        """串口重连后丢掉半个关键字，避免和下一包拼错。"""
        self._tail = ""

    def feed(self, text: str) -> ChunkVerdict:
        """吃进一块已去掉 ANSI 的文本。

        :param text: 本包文本，可含空白和换行。
        :return: 本包新出现的成功词、异常词和熔断原因。
        """
        verdict = ChunkVerdict(snippet=" ".join(text.split())[:180])
        compact_new = _WS_RE.sub("", text).lower()
        if not compact_new and not self._tail:
            return verdict
        region = self._tail + compact_new
        tail_len = len(self._tail)

        if self.accept_success and self.success is None:
            found_kw: str | None = None
            found_at: int | None = None
            for kw, pattern in self._success:
                for matched in pattern.finditer(region):
                    if matched.end() <= tail_len:
                        continue
                    if found_at is None or matched.start() < found_at:
                        found_at = matched.start()
                        found_kw = kw
                    break
            if found_kw is not None:
                self.success = found_kw
                verdict.success = found_kw

        for kw in self._exception:
            start = 0
            while kw:
                idx = region.find(kw, start)
                if idx < 0:
                    break
                if idx + len(kw) > tail_len:
                    self.exception_count += 1
                    verdict.exceptions.append(kw)
                start = idx + len(kw)

        if verdict.abort_reason is None:
            for kw in self._abort:
                idx = region.find(kw) if kw else -1
                if idx >= 0 and idx + len(kw) > tail_len:
                    verdict.abort_reason = f"命中致命关键字 '{kw}'"
                    verdict.abort_keep_power = self.cfg.abort_keep_power
                    break

        if verdict.abort_reason is None:
            for rule, kw, counter in self._rates:
                if not kw:
                    continue
                start = 0
                fresh = 0
                while True:
                    idx = region.find(kw, start)
                    if idx < 0:
                        break
                    if idx + len(kw) > tail_len:
                        fresh += 1
                    start = idx + len(kw)
                triggered = False
                for _ in range(fresh):
                    triggered = counter.hit() or triggered
                if triggered:
                    level = "致命" if rule.severity == "critical" else "频繁错误"
                    verdict.abort_reason = (
                        f"{level}熔断：{rule.window_s:g} 秒内检测到 {rule.count} 次 '{rule.keyword}'"
                    )
                    verdict.abort_keep_power = rule.keep_power
                    break

        self._tail = region[-self._keep :] if self._keep > 0 else ""
        return verdict

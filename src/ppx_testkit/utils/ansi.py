"""设备日志文本清洗。"""

from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_WS_RE = re.compile(r"\s+")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def normalize(text: str) -> str:
    """关键字匹配用的规范化：去 ANSI、转小写、去掉全部空白字符。"""
    return _WS_RE.sub("", strip_ansi(text)).lower()


def clean_line(raw: str) -> str:
    """原始日志落盘用：去 ANSI 与行尾换行，保留行内空格。"""
    return strip_ansi(raw).strip("\r\n")


# 行尾残缺的 ANSI 序列（ESC 被两次 read 切开时，整段序列还不能删）
_ANSI_PREFIX_RE = re.compile(r"\x1B(?:\[[0-?]*[ -/]*)?")


class AnsiStreamFilter:
    """按块去掉 ANSI 颜色码。

    序列如果刚好被两次 read() 从中间切开，先暂存到下一包再删，
    避免半截 ESC 留在关键字中间导致匹配失败。

    :param text: 本包新到的文本（可含上一包暂存的半截序列）。
    :return: 已去掉完整 ANSI 序列的文本。
    """

    def __init__(self) -> None:
        self._carry = ""

    def reset(self) -> None:
        """丢掉暂存的半截序列。换轮清空或串口重连后调用。"""
        self._carry = ""

    def feed(self, text: str) -> str:
        data = self._carry + text
        self._carry = ""
        if not data:
            return ""
        esc = data.rfind("\x1b")
        if esc != -1:
            tail = data[esc:]
            if len(tail) <= 32 and _ANSI_PREFIX_RE.fullmatch(tail):
                self._carry = tail
                data = data[:esc]
        return ANSI_RE.sub("", data)

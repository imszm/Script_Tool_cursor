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

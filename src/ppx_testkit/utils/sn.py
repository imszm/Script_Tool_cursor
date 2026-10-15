"""序列号 / VIN 生成。"""

from __future__ import annotations

import re
from collections.abc import Iterator

_TRAILING_DIGITS = re.compile(r"^(.*?)(\d+)$")


def increment_serial(sn: str, step: int = 1) -> str:
    """末尾数字段 +step，保持位宽；溢出时按实际位数扩展。

    >>> increment_serial("2022005002R000GD006400001")
    '2022005002R000GD006400002'
    """
    m = _TRAILING_DIGITS.match(sn)
    if not m:
        raise ValueError(f"序列号末尾没有数字段，无法递增: {sn!r}")
    prefix, digits = m.groups()
    value = int(digits) + step
    if value < 0:
        raise ValueError(f"序列号递增后为负数: {sn!r} + {step}")
    return f"{prefix}{value:0{len(digits)}d}"


def serial_sequence(prefix: str, start: int, stop: int, width: int) -> Iterator[str]:
    """生成 ``prefix + 零填充数字``，闭区间 [start, stop]。"""
    if width <= 0:
        raise ValueError("width 必须 > 0")
    if start > stop:
        raise ValueError(f"start({start}) 不能大于 stop({stop})")
    for n in range(start, stop + 1):
        yield f"{prefix}{n:0{width}d}"


def serial_batch(base: str, count: int) -> list[str]:
    """从 base 开始连续生成 count 个序列号（含 base 本身）。"""
    if count <= 0:
        return []
    out = [base]
    for _ in range(count - 1):
        out.append(increment_serial(out[-1]))
    return out

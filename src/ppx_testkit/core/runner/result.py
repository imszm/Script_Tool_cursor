"""执行结果数据结构。"""

from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CycleResult:
    index: int
    passed: bool
    detail: str = ""
    duration_s: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunSummary:
    station: str
    target_cycles: int
    executed: int = 0
    passed: int = 0
    failed: int = 0
    aborted: bool = False
    abort_reason: str | None = None
    keep_power: bool = False
    interrupted: bool = False
    error: str | None = None
    started_at: str = field(default_factory=lambda: _dt.datetime.now().isoformat(timespec="seconds"))
    ended_at: str | None = None
    duration_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.executed * 100.0) if self.executed else 0.0

    @property
    def ok(self) -> bool:
        return not (self.aborted or self.interrupted or self.error) and self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pass_rate"] = round(self.pass_rate, 2)
        d["ok"] = self.ok
        return d

    def render(self) -> str:
        lines = [
            "=" * 15 + " 测试统计总结 " + "=" * 15,
            f"  工位:            {self.station}",
            f"  目标循环:        {self.target_cycles}",
            f"  已执行:          {self.executed}",
            f"  通过 / 失败:     {self.passed} / {self.failed}  (通过率 {self.pass_rate:.2f}%)",
        ]
        for k, v in self.extra.items():
            lines.append(f"  {k}: {v}")
        if self.aborted:
            lines.append(f"  熔断原因:        {self.abort_reason}")
            lines.append(f"  现场保留(不断电): {'是' if self.keep_power else '否'}")
        if self.interrupted:
            lines.append("  手动中断:        是")
        if self.error:
            lines.append(f"  程序异常:        {self.error}")
        lines.append(f"  总耗时:          {self.duration_s:.1f} 秒")
        lines.append("=" * 44)
        return "\n".join(lines)

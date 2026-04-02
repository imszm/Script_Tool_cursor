from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class RunSummary:
    task: str
    started_at: str
    finished_at: str
    exit_code: int
    meta: dict[str, Any]


@dataclass
class FailureEvent:
    task: str
    at: str
    kind: str
    message: str
    meta: dict[str, Any]


def write_summary(run_dir: Path, summary: RunSummary) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "summary.json"
    path.write_text(json.dumps(asdict(summary), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_failure_event(artifacts_dir: Path, event: FailureEvent) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / f"failure_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(asdict(event), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


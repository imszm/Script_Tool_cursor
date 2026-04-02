from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import logging


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    logs_dir: Path
    artifacts_dir: Path


@dataclass
class RunContext:
    logger: logging.Logger
    paths: RunPaths
    config: dict[str, Any]
    drivers: dict[str, Any]


from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


def setup_logger(run_dir: Path, logger_name: str = "script_tool") -> logging.Logger:
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    file_path = logs_dir / f"full_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    fh = logging.FileHandler(file_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    err_path = logs_dir / f"exception_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    eh = logging.FileHandler(err_path, encoding="utf-8")
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fmt)
    logger.addHandler(eh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


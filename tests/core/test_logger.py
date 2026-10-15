from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

from ppx_testkit.logger import (
    current_context,
    get_logger,
    get_raw_logger,
    setup_logging,
    shutdown_logging,
)
from ppx_testkit.settings import LoggingSettings


def _settings(tmp_path: Path, **kw: object) -> LoggingSettings:
    return LoggingSettings(root_dir=str(tmp_path / "logs"), **kw)  # type: ignore[arg-type]


def test_run_directory_and_files(tmp_path: Path) -> None:
    ctx = setup_logging("st1", _settings(tmp_path), console=False)
    assert ctx.run_dir is not None and ctx.run_dir.parent == tmp_path / "logs" / "st1"
    assert current_context() is ctx

    log = get_logger("ppx_testkit.test")
    log.debug("调试信息")
    log.warning("警告信息")
    get_raw_logger("device").info("RAW 设备输出")
    ctx.write_json("summary.json", {"ok": True, "中文": 1})
    shutdown_logging()

    full = ctx.full_log.read_text(encoding="utf-8")  # type: ignore[union-attr]
    err = ctx.error_log.read_text(encoding="utf-8")  # type: ignore[union-attr]
    raw = ctx.raw_log.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "调试信息" in full and "警告信息" in full
    assert "调试信息" not in err and "警告信息" in err
    assert "RAW 设备输出" in raw and "RAW 设备输出" not in full
    assert json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8")) == {"ok": True, "中文": 1}


def test_idempotent_and_shutdown_restores_root(tmp_path: Path) -> None:
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    before_hooks = (sys.excepthook, threading.excepthook)
    ctx1 = setup_logging("a", _settings(tmp_path), console=False)
    ctx2 = setup_logging("b", _settings(tmp_path), console=False)
    assert ctx1 is ctx2
    assert sys.excepthook is not before_hooks[0]
    shutdown_logging()
    shutdown_logging()
    assert root.handlers == before_handlers and root.level == before_level
    assert current_context() is None
    assert (sys.excepthook, threading.excepthook) == before_hooks


def test_same_second_runs_get_unique_dirs(tmp_path: Path) -> None:
    ctx1 = setup_logging("st", _settings(tmp_path), console=False)
    shutdown_logging()
    ctx2 = setup_logging("st", _settings(tmp_path), console=False)
    shutdown_logging()
    assert ctx1.run_dir != ctx2.run_dir


def test_fallback_to_temp_dir(tmp_path: Path, monkeypatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "tmpfallback"))
    ctx = setup_logging("st", LoggingSettings(root_dir=str(blocker / "logs")), console=False)
    assert ctx.run_dir is not None and "tmpfallback" in str(ctx.run_dir)
    assert ctx.fallback_reason and "已降级" in ctx.fallback_reason


def test_thread_exceptions_are_logged(tmp_path: Path) -> None:
    ctx = setup_logging("st", _settings(tmp_path), console=False)

    def boom() -> None:
        raise RuntimeError("线程炸了")

    t = threading.Thread(target=boom, name="worker-x")
    t.start()
    t.join()
    shutdown_logging()
    text = ctx.full_log.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "worker-x" in text and "线程炸了" in text


def test_file_level_respected(tmp_path: Path) -> None:
    ctx = setup_logging("st", _settings(tmp_path, file_level="INFO"), console=False)
    get_logger("x").debug("不应出现")
    get_logger("x").info("应出现")
    shutdown_logging()
    text = ctx.full_log.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "应出现" in text and "不应出现" not in text

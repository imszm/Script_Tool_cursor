"""全局日志初始化。

每次运行生成独立目录 ``<root_dir>/<station>/<YYYYmmdd_HHMMSS>/``::

    full.log         全程完整日志（DEBUG 及以上，所有模块、串口收发十六进制、异常堆栈）
    error.log        WARNING 及以上
    device_raw.log   被测设备原始串口输出（按大小轮转，不进入控制台）
    config.yaml      本次生效配置快照（由 CLI 写入）
    summary.json     运行统计（由执行器写入）

业务模块只需 ``log = get_logger(__name__)``，不要自行添加 handler。
所有 handler 通过 QueueHandler/QueueListener 在后台线程写盘，多线程安全且不阻塞串口读取。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import logging.handlers
import queue
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ppx_testkit.settings import LoggingSettings
from ppx_testkit.utils.paths import resolve_path

RAW_LOGGER_ROOT = "ppx.raw"

FILE_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(threadName)s | %(name)s:%(lineno)d | %(message)s"
CONSOLE_FORMAT = "%(asctime)s | %(levelname)-7s | %(message)s"
RAW_FORMAT = "[%(asctime)s.%(msecs)03d] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class RunContext:
    station: str
    run_id: str
    run_dir: Path | None
    started_at: _dt.datetime
    full_log: Path | None = None
    error_log: Path | None = None
    raw_log: Path | None = None
    fallback_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def artifact(self, name: str) -> Path | None:
        return self.run_dir / name if self.run_dir else None

    def write_text(self, name: str, content: str) -> Path | None:
        path = self.artifact(name)
        if path is None:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return path
        except OSError:
            logging.getLogger(__name__).exception("写入运行产物失败: %s", path)
            return None

    def write_json(self, name: str, obj: Any) -> Path | None:
        return self.write_text(name, json.dumps(obj, ensure_ascii=False, indent=2, default=str))


class _State:
    lock = threading.Lock()
    ctx: RunContext | None = None
    listeners: list[logging.handlers.QueueListener] = []
    root_handlers_backup: list[logging.Handler] = []
    root_level_backup: int = logging.WARNING
    raw_handlers: list[logging.Handler] = []
    prev_sys_hook: Any = None
    prev_thread_hook: Any = None


def _make_run_dir(root: Path, station: str, run_id: str) -> Path:
    run_dir = root / station / run_id
    suffix = 1
    while run_dir.exists():
        run_dir = root / station / f"{run_id}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _safe_console_stream() -> Any:
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    return stream


def setup_logging(
    station: str,
    settings: LoggingSettings | None = None,
    *,
    console: bool = True,
) -> RunContext:
    """初始化全局日志。进程内重复调用直接返回已有上下文。"""
    settings = settings or LoggingSettings()
    with _State.lock:
        if _State.ctx is not None:
            return _State.ctx

        started = _dt.datetime.now()
        run_id = started.strftime("%Y%m%d_%H%M%S")
        run_dir: Path | None = None
        fallback_reason: str | None = None

        candidates = [resolve_path(settings.root_dir), Path(tempfile.gettempdir()) / "ppx_testkit_logs"]
        for candidate in candidates:
            try:
                run_dir = _make_run_dir(candidate, station, run_id)
                break
            except OSError as exc:
                fallback_reason = f"无法创建日志目录 {candidate}: {exc}"
                sys.stderr.write(f"[logger] {fallback_reason}\n")
        if run_dir is not None and fallback_reason and run_dir.parents[1] != candidates[0]:
            fallback_reason += f"；已降级到 {run_dir}"

        file_fmt = logging.Formatter(FILE_FORMAT, DATE_FORMAT)
        handlers: list[logging.Handler] = []

        if console:
            ch = logging.StreamHandler(_safe_console_stream())
            ch.setLevel(settings.console_level.upper())
            ch.setFormatter(logging.Formatter(CONSOLE_FORMAT, DATE_FORMAT))
            handlers.append(ch)

        ctx = RunContext(station=station, run_id=run_id, run_dir=run_dir, started_at=started)
        raw_handler: logging.Handler | None = None
        if run_dir is not None:
            try:
                ctx.full_log = run_dir / "full.log"
                fh = logging.FileHandler(ctx.full_log, encoding="utf-8")
                fh.setLevel(settings.file_level.upper())
                fh.setFormatter(file_fmt)
                handlers.append(fh)

                ctx.error_log = run_dir / "error.log"
                eh = logging.FileHandler(ctx.error_log, encoding="utf-8")
                eh.setLevel(logging.WARNING)
                eh.setFormatter(file_fmt)
                handlers.append(eh)

                ctx.raw_log = run_dir / "device_raw.log"
                raw_handler = logging.handlers.RotatingFileHandler(
                    ctx.raw_log,
                    maxBytes=settings.raw_max_bytes,
                    backupCount=settings.raw_backup_count,
                    encoding="utf-8",
                )
                raw_handler.setFormatter(logging.Formatter(RAW_FORMAT, DATE_FORMAT))
            except OSError as exc:
                fallback_reason = f"日志文件创建失败，仅输出到控制台: {exc}"
                sys.stderr.write(f"[logger] {fallback_reason}\n")
        ctx.fallback_reason = fallback_reason

        main_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
        main_listener = logging.handlers.QueueListener(main_queue, *handlers, respect_handler_level=True)
        main_listener.start()
        _State.listeners = [main_listener]

        root = logging.getLogger()
        _State.root_handlers_backup = list(root.handlers)
        _State.root_level_backup = root.level
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(logging.handlers.QueueHandler(main_queue))
        root.setLevel(logging.DEBUG)

        raw_root = logging.getLogger(RAW_LOGGER_ROOT)
        raw_root.propagate = False
        raw_root.setLevel(logging.DEBUG)
        for h in list(raw_root.handlers):
            raw_root.removeHandler(h)
        if raw_handler is not None:
            raw_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
            raw_listener = logging.handlers.QueueListener(raw_queue, raw_handler)
            raw_listener.start()
            _State.listeners.append(raw_listener)
            qh = logging.handlers.QueueHandler(raw_queue)
            raw_root.addHandler(qh)
            _State.raw_handlers = [qh, raw_handler]
        else:
            raw_root.addHandler(logging.NullHandler())

        logging.captureWarnings(True)
        _install_excepthooks()

        _State.ctx = ctx

    log = logging.getLogger(__name__)
    log.info("日志系统初始化完成：工位=%s，目录=%s", station, run_dir or "<仅控制台>")
    if fallback_reason:
        log.warning(fallback_reason)
    return ctx


def _install_excepthooks() -> None:
    _State.prev_sys_hook = sys.excepthook
    _State.prev_thread_hook = threading.excepthook

    def sys_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            _State.prev_sys_hook(exc_type, exc, tb)
            return
        logging.getLogger("ppx_testkit.uncaught").critical("未捕获异常", exc_info=(exc_type, exc, tb))

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        logging.getLogger("ppx_testkit.uncaught").critical(
            "线程 %s 未捕获异常", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
        )

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def get_raw_logger(source: str = "device") -> logging.Logger:
    """设备原始输出专用 logger：只写 device_raw.log，不进控制台与 full.log。"""
    return logging.getLogger(f"{RAW_LOGGER_ROOT}.{source}")


def current_context() -> RunContext | None:
    return _State.ctx


def shutdown_logging() -> None:
    """停止后台写盘线程、关闭文件并恢复 root logger（主要用于退出与测试）。"""
    with _State.lock:
        if _State.ctx is None:
            return
        for listener in _State.listeners:
            try:
                listener.stop()
            except Exception:  # noqa: BLE001 - 退出阶段不能再抛异常
                pass
            for h in listener.handlers:
                try:
                    h.flush()
                    h.close()
                except Exception:  # noqa: BLE001
                    pass
        _State.listeners = []

        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in _State.root_handlers_backup:
            root.addHandler(h)
        root.setLevel(_State.root_level_backup)

        raw_root = logging.getLogger(RAW_LOGGER_ROOT)
        for h in list(raw_root.handlers):
            raw_root.removeHandler(h)
        _State.raw_handlers = []

        if _State.prev_sys_hook is not None:
            sys.excepthook = _State.prev_sys_hook
        if _State.prev_thread_hook is not None:
            threading.excepthook = _State.prev_thread_hook
        logging.captureWarnings(False)
        _State.ctx = None

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from script_tool.config.loader import apply_overrides, load_config
from script_tool.core.context import RunContext, RunPaths
from script_tool.core.logging import setup_logger
from script_tool.core.reporting import RunSummary, now_iso, write_summary
from script_tool.drivers.port_detect import detect_port, list_ports
from script_tool.tasks.registry import build_registry


@dataclass(frozen=True)
class CommonArgs:
    config: str | None
    out_dir: str | None


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="script_tool", description="Script Tool (engineering edition)")
    p.add_argument("--config", help="Path to JSON config file", default=None)
    p.add_argument("--out-dir", help="Output base directory (default: runs/YYYYMMDD_HHMMSS)", default=None)

    sub = p.add_subparsers(dest="cmd", required=True)

    # 通用串口参数（按任务需要使用）
    def add_serial_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--relay-port", default=None)
        sp.add_argument("--relay-keyword", default=None, help="Auto-detect relay port by keyword (device/description)")
        sp.add_argument("--device-port", default=None)
        sp.add_argument("--device-keyword", default=None, help="Auto-detect device port by keyword (device/description)")
        sp.add_argument("--relay-ccb-port", default=None)
        sp.add_argument("--relay-ccb-keyword", default=None, help="Auto-detect CCB relay port by keyword")
        sp.add_argument("--baudrate-relay", type=int, default=None)
        sp.add_argument("--baudrate-device", type=int, default=None)
        sp.add_argument("--list-ports", action="store_true", help="List serial ports and exit")

    # loops
    def add_loops(sp: argparse.ArgumentParser, default: int = 10) -> None:
        sp.add_argument("--loops", type=int, default=default)

    # 子命令（任务注册表中也会校验）
    w3 = sub.add_parser("w3-power", help="W3 relay power cycle test")
    add_serial_args(w3)
    add_loops(w3, default=10)
    w3.add_argument("--press-on-s", type=float, default=2.5)
    w3.add_argument("--press-off-s", type=float, default=1.0)
    w3.add_argument("--monitor-s", type=float, default=10.0)

    charging = sub.add_parser("charging", help="Charging relay test")
    add_serial_args(charging)
    add_loops(charging, default=10)

    pc_upgrade = sub.add_parser("pc-upgrade", help="PC upgrade tool automation")
    add_loops(pc_upgrade, default=10)

    ccb = sub.add_parser("ccb-smt", help="CCB SMT automation")
    add_serial_args(ccb)
    add_loops(ccb, default=10)

    w3_pc = sub.add_parser("w3-pc-tool-stress", help="W3 PCTOOL stress test automation")
    add_loops(w3_pc, default=10)

    fixture_ts = sub.add_parser("fixture-turn-signal", help="Fixture relay turn-signal test")
    add_serial_args(fixture_ts)
    add_loops(fixture_ts, default=50)
    fixture_ts.add_argument("--press-time", type=float, default=0.6)
    fixture_ts.add_argument("--release-time", type=float, default=0.2)
    fixture_ts.add_argument("--interval", type=float, default=0.5)
    fixture_ts.add_argument("--light-on-time", type=float, default=0.8)

    ccb_fuzzy = sub.add_parser("ccb-smt-fuzzy", help="CCB SMT fuzzy vision + relay variant")
    add_serial_args(ccb_fuzzy)
    add_loops(ccb_fuzzy, default=100)

    return p


def _make_run_paths(out_dir: str | None, task_name: str) -> RunPaths:
    if out_dir:
        run_dir = Path(out_dir)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = Path("runs") / f"{stamp}_{task_name}"

    logs_dir = run_dir / "logs"
    artifacts_dir = run_dir / "artifacts"
    logs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return RunPaths(run_dir=run_dir, logs_dir=logs_dir, artifacts_dir=artifacts_dir)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "list_ports", False):
        for p in list_ports():
            print(f"{p.device}\t{p.description}")
        return 0

    registry = build_registry()
    task_factory = registry.get(args.cmd)
    if task_factory is None:
        parser.error(f"Unknown command: {args.cmd}")

    cfg = load_config(args.config)

    overrides: dict = {"serial": {}, "pc_tool": {}}
    for key in ["relay_port", "device_port", "relay_ccb_port", "baudrate_relay", "baudrate_device"]:
        v = getattr(args, key, None)
        if v is not None:
            overrides["serial"][key] = v

    # keyword auto-detect fallback
    if getattr(args, "relay_port", None) is None and getattr(args, "relay_keyword", None):
        p = detect_port(str(args.relay_keyword))
        if p:
            overrides["serial"]["relay_port"] = p
    if getattr(args, "device_port", None) is None and getattr(args, "device_keyword", None):
        p = detect_port(str(args.device_keyword))
        if p:
            overrides["serial"]["device_port"] = p
    if getattr(args, "relay_ccb_port", None) is None and getattr(args, "relay_ccb_keyword", None):
        p = detect_port(str(args.relay_ccb_keyword))
        if p:
            overrides["serial"]["relay_ccb_port"] = p
    cfg = apply_overrides(cfg, overrides)

    paths = _make_run_paths(args.out_dir, args.cmd)
    logger = setup_logger(paths.run_dir, logger_name="script_tool")

    started = now_iso()
    exit_code = 1
    task = task_factory(args=args, config=cfg, paths=paths)
    ctx = RunContext(logger=logger, paths=paths, config=cfg, drivers={})

    try:
        task.setup(ctx)
        exit_code = int(task.run(ctx))
        task.teardown(ctx)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        exit_code = 130
    except Exception as e:
        logger.exception(f"Unhandled error: {e}")
        exit_code = 1
    finally:
        # 尽量安全关闭驱动
        for d in list(ctx.drivers.values()):
            try:
                close = getattr(d, "close", None)
                if callable(close):
                    close()
            except Exception:
                logger.exception("Failed to close driver")

        finished = now_iso()
        write_summary(
            paths.run_dir,
            RunSummary(
                task=args.cmd,
                started_at=started,
                finished_at=finished,
                exit_code=exit_code,
                meta={"out_dir": str(paths.run_dir)},
            ),
        )

    return exit_code


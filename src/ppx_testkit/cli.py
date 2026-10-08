"""统一命令行入口。

    ppx-test list                         列出全部工位
    ppx-test ports                        列出系统串口
    ppx-test show <station>               打印合并后的生效配置
    ppx-test run <station> [选项]         运行工位

run 选项：
    --set a.b=value       覆盖任意配置项（可重复）
    --port relay=COM5     等价于 --set serial.relay.match.port=COM5（可重复）
    --no-local            忽略 config/local.yaml

应用模块约定：``ppx_testkit.apps.<app>`` 中实现 ``run(settings, ctx) -> int``。
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from collections.abc import Sequence
from typing import Any

from ppx_testkit import __version__
from ppx_testkit.exceptions import ConfigError, ToolError
from ppx_testkit.logger import setup_logging, shutdown_logging
from ppx_testkit.settings import (
    AppSettings,
    deep_merge,
    list_stations,
    load_settings,
    parse_set_args,
    station_file,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_HARDWARE = 3
EXIT_INTERRUPTED = 130


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ppx-test", description="嵌入式整机自动化测试工具集")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出全部工位")
    sub.add_parser("ports", help="列出系统串口")

    show = sub.add_parser("show", help="打印合并后的生效配置")
    _add_station_args(show)

    run = sub.add_parser("run", help="运行工位")
    _add_station_args(run)
    return p


def _add_station_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("station", help="工位名（config/stations/<名称>.yaml）或 yaml 文件路径")
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE", help="覆盖配置项")
    p.add_argument("--port", dest="ports", action="append", default=[], metavar="NAME=PORT",
                   help="指定串口，如 relay=COM5")
    p.add_argument("--no-local", action="store_true", help="忽略 config/local.yaml")


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    result = parse_set_args(args.sets)
    port_sets = []
    for item in args.ports:
        if "=" not in item:
            raise ConfigError(f"--port 参数格式应为 NAME=PORT，实际为 {item!r}")
        name, port = item.split("=", 1)
        port_sets.append(f"serial.{name.strip()}.match.port={port.strip()}")
    return deep_merge(result, parse_set_args(port_sets))


def _load(args: argparse.Namespace) -> AppSettings:
    return load_settings(args.station, overrides=_overrides(args), use_local=not args.no_local)


def cmd_list() -> int:
    from ppx_testkit.settings import _read_yaml  # 仅用于读取描述

    for name in list_stations():
        try:
            data = _read_yaml(station_file(name))
            desc = data.get("description", "")
        except ConfigError as exc:
            desc = f"<配置错误: {exc}>"
        print(f"{name:32s} {desc}")
    return EXIT_OK


def cmd_ports() -> int:
    from ppx_testkit.core.serial.port_finder import PortFinder

    for p in PortFinder().list_ports():
        print(f"{p.device:12s} {p.description}  [{p.hwid}]")
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    settings = _load(args)
    print(f"# 工位: {settings.station}  应用: {settings.app}")
    print(f"# 配置来源: {' < '.join(settings.sources)}")
    print(settings.to_yaml())
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    log = logging.getLogger("ppx_testkit.cli")
    try:
        settings = _load(args)
    except ConfigError as exc:
        setup_logging(str(args.station).replace("/", "_").replace("\\", "_"))
        log.error("配置错误: %s", exc)
        return EXIT_CONFIG

    ctx = setup_logging(settings.station, settings.logging)
    log.info("工位: %s | 应用: %s | %s", settings.station, settings.app, settings.description)
    log.info("配置来源: %s", " < ".join(settings.sources))
    ctx.write_text("config.yaml", settings.to_yaml())

    try:
        module = importlib.import_module(f"ppx_testkit.apps.{settings.app}")
    except ImportError as exc:
        log.error("无法加载应用模块 ppx_testkit.apps.%s: %s", settings.app, exc, exc_info=True)
        return EXIT_CONFIG
    runner = getattr(module, "run", None)
    if not callable(runner):
        log.error("应用模块 %s 未实现 run(settings, ctx)", module.__name__)
        return EXIT_CONFIG

    try:
        code = runner(settings, ctx)
        return int(code or EXIT_OK)
    except ConfigError as exc:
        log.error("配置错误: %s", exc)
        return EXIT_CONFIG
    except ToolError as exc:
        log.error("运行失败: %s", exc, exc_info=True)
        return EXIT_HARDWARE
    except KeyboardInterrupt:
        log.warning("用户中断")
        return EXIT_INTERRUPTED
    except Exception:  # noqa: BLE001 - CLI 兜底，保证异常写入 full.log
        log.critical("应用发生未预期异常", exc_info=True)
        return EXIT_FAIL


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "list":
            return cmd_list()
        if args.command == "ports":
            return cmd_ports()
        if args.command == "show":
            return cmd_show(args)
        return cmd_run(args)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    finally:
        shutdown_logging()


if __name__ == "__main__":
    sys.exit(main())

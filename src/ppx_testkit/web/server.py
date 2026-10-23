"""只监听 127.0.0.1 的网页服务。页面和接口都在本进程里，测试在子进程里跑。"""

from __future__ import annotations

import json
import logging
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ppx_testkit.exceptions import ConfigError
from ppx_testkit.settings import load_settings
from ppx_testkit.utils.paths import resolve_path
from ppx_testkit.web.catalog import build_run_args, save_local_ports, station_catalog
from ppx_testkit.web.session import RunSession

log = logging.getLogger(__name__)


def _page_path() -> Path:
    """网页文件。开发时和安装后都从包目录读取。"""
    return Path(__file__).resolve().parent / "static" / "index.html"


class ConsoleServer(ThreadingHTTPServer):
    """把运行会话挂在服务器上，请求处理函数从这里取。"""

    def __init__(self, address: tuple[str, int], session: RunSession) -> None:
        self.session = session
        super().__init__(address, ConsoleHandler)


class ConsoleHandler(BaseHTTPRequestHandler):
    server: ConsoleServer

    def do_GET(self) -> None:  # noqa: N802 - 标准库要求这个名字
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._send(200, _page_path().read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/stations":
            self._json(200, {"stations": station_catalog()})
            return
        if path == "/api/ports":
            self._json(200, {"ports": _list_ports()})
            return
        if path == "/api/status":
            self._json(200, self.server.session.snapshot())
            return
        if path == "/api/log":
            query = parse_qs(urlparse(self.path).query)
            raw = query.get("offset", ["0"])[0]
            try:
                offset = int(raw)
            except ValueError:
                offset = 0
            self._json(200, self.server.session.log_from(max(0, offset)))
            return
        self._json(404, {"error": "没有这个地址"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/start":
                self._json(200, _start(self.server.session, body))
                return
            if path == "/api/stop":
                self.server.session.stop()
                self._json(200, self.server.session.snapshot())
                return
            self._json(404, {"error": "没有这个地址"})
        except ConfigError as exc:
            code = 409 if "已有测试在运行" in str(exc) else 400
            self._json(code, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 网页接口兜底，避免线程把异常吞掉
            log.exception("网页请求失败")
            self._json(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            raise ConfigError("请求过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"请求不是 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("请求必须是 JSON 对象")
        return data

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        self._send(code, body, "application/json; charset=utf-8")

    def _send(self, code: int, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def _list_ports() -> list[dict[str, str]]:
    from ppx_testkit.core.serial.port_finder import PortFinder

    try:
        found = PortFinder().list_ports()
    except Exception as exc:  # noqa: BLE001 - 没装串口驱动时页面仍要打开
        log.warning("枚举串口失败: %s", exc)
        return []
    return [{"device": p.device, "description": p.description or ""} for p in found]


def _start(session: RunSession, body: dict[str, Any]) -> dict[str, Any]:
    """校验表单、记下串口、启动一场测试。

    :param body: ``{"station", "ports": {名字: 端口}, "cycles": 整数或 null}``。
    :return: 启动后的状态。
    """
    station = str(body.get("station") or "")
    chosen = _known_station(station)
    if chosen.get("error"):
        raise ConfigError(str(chosen["error"]))
    ports = body.get("ports") or {}
    if not isinstance(ports, dict):
        raise ConfigError("ports 必须是对象")
    known = {item["name"] for item in chosen["serial"]}
    clean: dict[str, str] = {}
    for name, port in ports.items():
        if name not in known:
            raise ConfigError(f"工位 {station} 没有串口 {name}")
        clean[str(name)] = str(port or "")
    cycles = body.get("cycles")
    if cycles in ("", None):
        cycles = None
    elif isinstance(cycles, bool) or not isinstance(cycles, int):
        raise ConfigError("轮次必须是整数")
    if any(port.strip() for port in clean.values()):
        save_local_ports(station, clean)
    args = build_run_args(station, clean, cycles, chosen.get("cycle_key"))
    settings = load_settings(station, use_local=True, environ={})
    log_root = resolve_path(settings.logging.root_dir) / settings.station
    session.start(station, args, log_root)
    log.info("网页启动工位 %s: %s", station, " ".join(args[1:]))
    return session.snapshot()


def _known_station(station: str) -> dict[str, Any]:
    for row in station_catalog():
        if row["name"] == station:
            return row
    raise ConfigError(f"找不到工位 {station}")


def serve(port: int = 8765, *, open_browser: bool = True) -> int:
    """启动网页并阻塞，直到 Ctrl+C。只绑定 127.0.0.1。

    :param port: 本机端口，默认 8765。
    :param open_browser: 为真时用系统浏览器打开页面。
    :return: 0 表示正常关掉。端口被占用时向 stderr 写原因并返回 2。
    """
    host = "127.0.0.1"
    session = RunSession()
    try:
        httpd = ConsoleServer((host, port), session)
    except OSError as exc:
        print(f"无法启动网页运行台: {exc}", file=sys.stderr)
        return 2
    url = f"http://{host}:{port}/"
    print(f"网页运行台 {url}")
    print("关闭本窗口即停止网页。正在跑的测试请先在页面上点停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("网页运行台收到中断")
    finally:
        httpd.shutdown()
        session.stop(grace_s=3)
        httpd.server_close()
    return 0

"""只监听 127.0.0.1 的网页服务。页面和接口都在本进程里，测试在子进程里跑。

接口会改写 local.yaml 并启动带电压测，所以只接受 Host 指向本机端口的请求；POST 还要求
Origin 为本页（或不带 Origin 的本机脚本），``/api/start`` 只收 application/json，
其他网页无法跨站触发开始或停止。
"""

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
from ppx_testkit.web.session import DEFAULT_STOP_GRACE_S, RunSession

log = logging.getLogger(__name__)


def _page_path() -> Path:
    """网页文件。开发时和安装后都从包目录读取。"""
    return Path(__file__).resolve().parent / "static" / "index.html"


class ConsoleServer(ThreadingHTTPServer):
    """把运行会话挂在服务器上，请求处理函数从这里取。"""

    def __init__(
        self,
        address: tuple[str, int],
        session: RunSession,
        *,
        stop_grace_s: float = DEFAULT_STOP_GRACE_S,
    ) -> None:
        """绑定地址并挂上会话。

        :param address: ``(主机, 端口)``，端口为 0 时由系统分配。
        :param session: 当前唯一的运行会话。
        :param stop_grace_s: 点「停止」后等待工位收尾的秒数。
        """
        self.session = session
        self.stop_grace_s = stop_grace_s
        super().__init__(address, ConsoleHandler)


class ConsoleHandler(BaseHTTPRequestHandler):
    server: ConsoleServer

    def do_GET(self) -> None:  # noqa: N802 - 标准库要求这个名字
        if not self._host_ok():
            self._reject(403, "只接受本机地址访问")
            return
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
        if not self._host_ok() or not self._origin_ok():
            self._reject(403, "只接受本页发起的请求")
            return
        path = urlparse(self.path).path
        if path == "/api/start" and not self._is_json():
            self._reject(415, "请求必须是 application/json")
            return
        try:
            body = self._read_json()
            if path == "/api/start":
                self._json(200, _start(self.server.session, body))
                return
            if path == "/api/stop":
                self.server.session.stop(self.server.stop_grace_s)
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

    def _allowed_hosts(self) -> set[str]:
        """本机网页的 ``主机:端口`` 写法。端口为 80 时浏览器会省略端口。

        :return: 小写的 Host 取值集合。
        """
        port = int(self.server.server_address[1])
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts |= {"127.0.0.1", "localhost"}
        return hosts

    def _host_ok(self) -> bool:
        """Host 头必须指向本机网页端口，挡住 DNS rebinding。

        :return: 是否允许。
        """
        return (self.headers.get("Host") or "").strip().lower() in self._allowed_hosts()

    def _origin_ok(self) -> bool:
        """浏览器带了 Origin 时必须是本页，挡住其他网页跨站调用；没有 Origin 的本机脚本放行。

        :return: 是否允许。
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin.strip().lower() in {f"http://{host}" for host in self._allowed_hosts()}

    def _is_json(self) -> bool:
        """请求体是否声明为 JSON。跨站的「简单请求」只能用 text/plain 等类型。

        :return: Content-Type 是 application/json 时为 True。
        """
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return ctype == "application/json"

    def _reject(self, code: int, message: str) -> None:
        """记一条警告并返回错误，不读请求体。

        :param code: HTTP 状态码。
        :param message: 返回给调用方的说明。
        """
        log.warning("拒绝请求 %s %s（Host=%r，Origin=%r）: %s", self.command, self.path,
                    self.headers.get("Host"), self.headers.get("Origin"), message)
        self._json(code, {"error": message})

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


def serve(port: int = 8765, *, open_browser: bool = True, stop_grace_s: float | None = None) -> int:
    """启动网页并阻塞，直到 Ctrl+C。只绑定 127.0.0.1。

    :param port: 本机端口，默认 8765。
    :param open_browser: 为真时用系统浏览器打开页面。
    :param stop_grace_s: 停止测试时等待工位收尾的秒数；None 用 :data:`DEFAULT_STOP_GRACE_S`。
    :return: 0 表示正常关掉。端口被占用时向 stderr 写原因并返回 2。
    """
    host = "127.0.0.1"
    grace = DEFAULT_STOP_GRACE_S if stop_grace_s is None else stop_grace_s
    session = RunSession()
    try:
        httpd = ConsoleServer((host, port), session, stop_grace_s=grace)
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
        if session.running():
            log.warning("网页运行台退出，正在停止测试并等待工位收尾（最多 %.0f 秒）", grace)
        session.stop(grace_s=grace)
        httpd.server_close()
    return 0

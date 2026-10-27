"""把工位配置整理成网页需要的表单，并拼出一次运行的命令参数。

不改工位 yaml。串口写回 config/local.yaml；轮次只出现在这一次的 --set 里。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

from ppx_testkit.core.registry import lookup_registered
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.settings import _read_yaml, list_stations, load_settings
from ppx_testkit.utils.paths import config_dir

# 这些工位点「开始」之后仍要键盘或会再弹出窗口，页面上单独提示。
CAUTIONS: dict[str, str] = {
    "relay_probe": "这个工位默认每一步都等回车。网页已关掉键盘输入，开始后可能马上结束。",
    "r3_mcb_whitebox": "这个工位默认是键盘菜单。网页不能替你按键。",
    "speed_calc": "开始后会再弹出车速计算窗口。",
    "time_diff": "开始后会再弹出时间差窗口。",
    "ppx_pctool_stress": (
        "开始后会控制鼠标和键盘操作 PPX PCTOOL。先在工位配置里改好 vin_click 和 confirm_click，"
        "上位机窗口露在最前面，输入法切到英文。扫码确认 20 秒后还会点一次力矩校验的确认。"
        "鼠标移到屏幕左上角可紧急停止。"
    ),
}

_CYCLE_SECTIONS = ("test", "stress", "lamp")
_PORT_RE = re.compile(r"^[A-Za-z0-9_.:/\\-]{1,64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_STATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}(?:/[A-Za-z0-9_-]{1,64})*$")


def _caution_for(station: str, app: str | None = None) -> str | None:
    """网页提示。注册信息里写了 caution 时优先，否则回退到 :data:`CAUTIONS`。

    :param station: 工位名。
    :param app: 配置里的应用路径，可空。
    :return: 提示文案；没有则 None。
    """
    for key in (station, app or ""):
        if not key:
            continue
        meta = lookup_registered(key)
        if meta is not None and meta.caution:
            return meta.caution
    return CAUTIONS.get(station)


def station_catalog() -> list[dict[str, Any]]:
    """列出全部工位：说明、串口名、当前口、轮次字段。单个工位读失败时带上 error，不拖垮整份列表。

    :return: 按工位名排序的字典列表。
    """
    rows: list[dict[str, Any]] = []
    for name in list_stations():
        try:
            settings = load_settings(name, use_local=True, environ={})
        except ConfigError as exc:
            rows.append({"name": name, "description": "", "error": str(exc), "serial": [], "caution": _caution_for(name)})
            continue
        data = dict(settings.data)
        cycle_key, cycle_default = cycle_field(data)
        rows.append({
            "name": settings.station,
            "description": settings.description,
            "app": settings.app,
            "serial": serial_fields(data),
            "cycle_key": cycle_key,
            "cycle_default": cycle_default,
            "caution": _caution_for(settings.station, settings.app),
            "error": None,
        })
    return rows


def serial_fields(data: dict[str, Any]) -> list[dict[str, str]]:
    """从合并后的配置取出 serial.<名字>.match.port。

    :param data: load_settings 的原始映射。
    :return: ``[{"name": "relay", "port": "COM3"}]``，没有口时 port 为空串。
    """
    serial = data.get("serial")
    if not isinstance(serial, dict):
        return []
    fields: list[dict[str, str]] = []
    for name, spec in serial.items():
        match = spec.get("match") if isinstance(spec, dict) else None
        port = ""
        if isinstance(match, dict) and match.get("port"):
            port = str(match["port"])
        fields.append({"name": str(name), "port": port})
    return fields


def cycle_field(data: dict[str, Any]) -> tuple[str | None, int | None]:
    """找到这一次运行可以覆盖的轮次字段。

    只认 ``test.cycles``、``stress.cycles``、``lamp.cycles``。字段在但值不是整数（例如 null）时仍返回键，默认值为 None。

    :return: (点号路径, 当前整数或 None)。都没有时 ``(None, None)``。
    """
    for section in _CYCLE_SECTIONS:
        block = data.get(section)
        if isinstance(block, dict) and "cycles" in block:
            value = block.get("cycles")
            current = value if isinstance(value, int) and not isinstance(value, bool) else None
            return f"{section}.cycles", current
    return None, None


def build_run_args(station: str, ports: dict[str, str], cycles: int | None, cycle_key: str | None) -> list[str]:
    """拼出一次 ``python -m ppx_testkit run`` 的参数列表。不经过 shell。

    :param station: 工位名。
    :param ports: 串口名到端口号，空字符串跳过。
    :param cycles: 只对这一次有效的轮次；None 表示沿用 yaml。
    :param cycle_key: 如 ``test.cycles``。cycles 有值时必须给出。
    :return: 传给子进程的 argv。
    """
    if not _STATION_RE.fullmatch(station):
        raise ConfigError(f"非法工位名: {station!r}")
    args = [sys.executable, "-m", "ppx_testkit", "run", station]
    for name in sorted(ports):
        port = ports[name].strip()
        if not port:
            continue
        if not _NAME_RE.fullmatch(name):
            raise ConfigError(f"非法串口名: {name!r}")
        if not _PORT_RE.fullmatch(port):
            raise ConfigError(f"串口 {name} 的端口号无效: {port!r}")
        args.extend(["--port", f"{name}={port}"])
    if cycles is not None:
        if not cycle_key or cycle_key not in {f"{s}.cycles" for s in _CYCLE_SECTIONS}:
            raise ConfigError("这个工位没有可覆盖的轮次")
        if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles <= 0 or cycles > 10_000_000:
            raise ConfigError(f"轮次必须是 1 到 10000000 的整数，实际 {cycles!r}")
        args.extend(["--set", f"{cycle_key}={cycles}"])
    return args


def save_local_ports(station: str, ports: dict[str, str], path: Path | None = None) -> Path:
    """把串口写进 local.yaml 的 stations.<工位>.serial.<名字>.match.port。

    已有的其他键保留。文件不存在就新建。注释在写回时会丢掉，因为标准库 YAML 不保留注释。

    :param station: 工位名。
    :param ports: 要保存的口；空字符串不写。
    :param path: 默认 config/local.yaml。
    :return: 写入的文件路径。
    """
    if not _STATION_RE.fullmatch(station):
        raise ConfigError(f"非法工位名: {station!r}")
    target = path or (config_dir() / "local.yaml")
    current: dict[str, Any] = {}
    if target.exists():
        current = _read_yaml(target)
    stations = current.setdefault("stations", {})
    if not isinstance(stations, dict):
        raise ConfigError("local.yaml 的 stations 必须是映射")
    block = stations.setdefault(station, {})
    if not isinstance(block, dict):
        raise ConfigError(f"local.yaml 里 stations.{station} 必须是映射")
    serial = block.setdefault("serial", {})
    if not isinstance(serial, dict):
        raise ConfigError(f"local.yaml 里 stations.{station}.serial 必须是映射")
    for name, port in ports.items():
        text = port.strip()
        if not text:
            continue
        if not _NAME_RE.fullmatch(name) or not _PORT_RE.fullmatch(text):
            raise ConfigError(f"无法保存串口 {name}={text!r}")
        endpoint = serial.setdefault(name, {})
        if not isinstance(endpoint, dict):
            endpoint = {}
            serial[name] = endpoint
        match = endpoint.setdefault("match", {})
        if not isinstance(match, dict):
            match = {}
            endpoint["match"] = match
        match["port"] = text
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(current, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target

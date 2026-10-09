"""配置加载与校验。

配置优先级（后者覆盖前者）::

    dataclass 默认值
      < config/default.yaml
      < config/stations/<station>.yaml（可用 ``include:`` 引入公共片段）
      < config/local.yaml（本机 COM 口等，不入库；``stations.<station>`` 段只作用于该工位）
      < 环境变量 PPX__A__B=value
      < 命令行 --set a.b=value

所有段落最终通过 :func:`from_dict` 转换为 *冻结* 的 dataclass，
类型不符、缺少必填项或出现未知字段都会抛出 :class:`ConfigError`。
"""

from __future__ import annotations

import copy
import dataclasses
import os
import types
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

import yaml

from ppx_testkit.exceptions import ConfigError
from ppx_testkit.utils.paths import config_dir, resolve_path

T = TypeVar("T")

ENV_PREFIX = "PPX__"
_MISSING = object()


# ============================================================ 通用 dataclass 构建
def from_dict(cls: type[T], data: Mapping[str, Any] | None, path: str = "") -> T:
    """把映射转换为 dataclass 实例，并做严格的类型校验。"""
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls!r} 不是 dataclass")
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"配置项 '{path or cls.__name__}' 应为映射(dict)，实际为 {type(data).__name__}")

    hints = get_type_hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls) if f.init}
    unknown = set(data) - set(fields)
    if unknown:
        raise ConfigError(
            f"配置项 '{path or cls.__name__}' 存在未知字段: {sorted(unknown)}；可用字段: {sorted(fields)}"
        )

    kwargs: dict[str, Any] = {}
    for name, f in fields.items():
        sub_path = f"{path}.{name}" if path else name
        if name in data:
            kwargs[name] = _coerce(data[name], hints[name], sub_path)
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise ConfigError(f"缺少必填配置项 '{sub_path}'")

    try:
        return cls(**kwargs)
    except ConfigError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 '{path or cls.__name__}' 校验失败: {exc}") from exc


def _type_name(tp: Any) -> str:
    return getattr(tp, "__name__", None) or str(tp)


def _coerce(value: Any, tp: Any, path: str) -> Any:
    if tp is Any or tp is object:
        return value

    origin = get_origin(tp)

    if origin in (Union, types.UnionType):
        args = get_args(tp)
        if value is None:
            if type(None) in args:
                return None
            raise ConfigError(f"配置项 '{path}' 不能为空")
        errors = []
        for arg in args:
            if arg is type(None):
                continue
            try:
                return _coerce(value, arg, path)
            except ConfigError as exc:
                errors.append(str(exc))
        raise ConfigError(f"配置项 '{path}' 的值 {value!r} 不符合 {tp}: {'; '.join(errors)}")

    if origin is Literal:
        allowed = get_args(tp)
        if value not in allowed:
            raise ConfigError(f"配置项 '{path}' 只能取 {list(allowed)}，实际为 {value!r}")
        return value

    if origin in (list, Iterable, Sequence) or tp is list:
        (item_tp,) = get_args(tp) or (Any,)
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"配置项 '{path}' 应为列表，实际为 {type(value).__name__}")
        return [_coerce(v, item_tp, f"{path}[{i}]") for i, v in enumerate(value)]

    if origin is tuple or tp is tuple:
        args = get_args(tp)
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"配置项 '{path}' 应为列表/元组，实际为 {type(value).__name__}")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_coerce(v, args[0], f"{path}[{i}]") for i, v in enumerate(value))
        if args and len(args) != len(value):
            raise ConfigError(f"配置项 '{path}' 应包含 {len(args)} 个元素，实际 {len(value)} 个")
        if not args:
            return tuple(value)
        return tuple(_coerce(v, a, f"{path}[{i}]") for i, (v, a) in enumerate(zip(value, args, strict=True)))

    if origin in (dict, Mapping) or tp is dict:
        k_tp, v_tp = get_args(tp) or (Any, Any)
        if not isinstance(value, Mapping):
            raise ConfigError(f"配置项 '{path}' 应为映射，实际为 {type(value).__name__}")
        return {_coerce(k, k_tp, f"{path}<key>"): _coerce(v, v_tp, f"{path}.{k}") for k, v in value.items()}

    if dataclasses.is_dataclass(tp):
        if isinstance(value, tp):
            return value
        return from_dict(tp, value, path)

    if tp is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "yes", "no", "1", "0"}:
            return value.strip().lower() in {"true", "yes", "1"}
        raise ConfigError(f"配置项 '{path}' 应为布尔值，实际为 {value!r}")

    if tp is int:
        if isinstance(value, bool):
            raise ConfigError(f"配置项 '{path}' 应为整数，实际为布尔值")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip(), 0)
            except ValueError:
                pass
        raise ConfigError(f"配置项 '{path}' 应为整数，实际为 {value!r}")

    if tp is float:
        if isinstance(value, bool):
            raise ConfigError(f"配置项 '{path}' 应为数字，实际为布尔值")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
        raise ConfigError(f"配置项 '{path}' 应为数字，实际为 {value!r}")

    if tp is str:
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return str(value)
        raise ConfigError(f"配置项 '{path}' 应为字符串，实际为 {value!r}")

    if tp is bytes:
        return parse_bytes(value, path)

    if tp is Path:
        if isinstance(value, (str, os.PathLike)):
            return resolve_path(value)
        raise ConfigError(f"配置项 '{path}' 应为路径字符串，实际为 {value!r}")

    if isinstance(tp, type) and isinstance(value, tp):
        return value

    raise ConfigError(f"配置项 '{path}' 不支持的类型 {_type_name(tp)}（值 {value!r}）")


def parse_bytes(value: Any, path: str = "") -> bytes:
    """把配置中的字节写法统一转换为 bytes。

    支持：整数 ``0x50``、整数列表 ``[0xA0, 0x02]``、十六进制字符串 ``"A0 02 01 A3"``、
    以及带 ``ascii:`` 前缀的文本 ``"ascii:#000P2500T1000!"``。
    """
    try:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bool):
            raise ValueError("布尔值不能作为字节")
        if isinstance(value, int):
            return bytes([value])
        if isinstance(value, (list, tuple)):
            return bytes(int(v, 0) if isinstance(v, str) else int(v) for v in value)
        if isinstance(value, str):
            if value.startswith("ascii:"):
                return value[len("ascii:") :].encode("ascii")
            return bytes.fromhex(value.replace("0x", "").replace(",", " "))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"配置项 '{path}' 无法解析为字节序列: {value!r} ({exc})") from exc
    raise ConfigError(f"配置项 '{path}' 无法解析为字节序列: {value!r}")


# ============================================================ 全局配置段
@dataclass(frozen=True)
class LoggingSettings:
    root_dir: str = "logs"
    console_level: str = "INFO"
    file_level: str = "DEBUG"
    raw_max_bytes: int = 50 * 1024 * 1024
    raw_backup_count: int = 20

    def __post_init__(self) -> None:
        for lvl in (self.console_level, self.file_level):
            if lvl.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                raise ConfigError(f"非法日志级别: {lvl}")
        if self.raw_max_bytes <= 0 or self.raw_backup_count < 0:
            raise ConfigError("raw_max_bytes 必须 > 0，raw_backup_count 必须 >= 0")


@dataclass(frozen=True)
class AppSettings:
    station: str
    app: str
    description: str
    logging: LoggingSettings
    data: Mapping[str, Any] = field(repr=False)
    sources: tuple[str, ...] = ()

    def raw(self, key: str, default: Any = _MISSING) -> Any:
        """按点号路径读取原始配置值（返回深拷贝）。"""
        node: Any = self.data
        for part in key.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            elif default is _MISSING:
                raise ConfigError(f"缺少配置项 '{key}'（工位 {self.station}）")
            else:
                return default
        return copy.deepcopy(node)

    def section(self, key: str, cls: type[T], *, required: bool = True) -> T:
        value = self.raw(key, None)
        if value is None:
            if required:
                raise ConfigError(f"缺少配置段 '{key}'（工位 {self.station}）")
            return from_dict(cls, {}, key)
        return from_dict(cls, value, key)

    def has(self, key: str) -> bool:
        return self.raw(key, None) is not None

    def to_yaml(self) -> str:
        return yaml.safe_dump(_plain(self.data), allow_unicode=True, sort_keys=False)


def _plain(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, bytes):
        return obj.hex(" ").upper()
    if isinstance(obj, Path):
        return str(obj)
    return obj


# ============================================================ 加载
def deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并；映射逐层合并，其余类型（含列表）整体覆盖。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(dict(result[key]), value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 语法错误: {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"读取配置文件失败: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射: {path}")
    return data


def _load_with_includes(path: Path, sources: list[str], _stack: tuple[Path, ...] = ()) -> dict[str, Any]:
    path = path.resolve()
    if path in _stack:
        chain = " -> ".join(str(p) for p in (*_stack, path))
        raise ConfigError(f"配置 include 出现循环引用: {chain}")
    data = _read_yaml(path)
    includes = data.pop("include", []) or []
    if isinstance(includes, str):
        includes = [includes]
    merged: dict[str, Any] = {}
    for inc in includes:
        inc_path = resolve_path(inc, base=path.parent)
        merged = deep_merge(merged, _load_with_includes(inc_path, sources, (*_stack, path)))
    sources.append(str(path))
    return deep_merge(merged, data)


def _env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        set_dotted(result, parts, _parse_scalar(raw))
    return result


def _parse_scalar(raw: str) -> Any:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def set_dotted(target: dict[str, Any], parts: list[str], value: Any) -> None:
    node = target
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def parse_set_args(items: Iterable[str]) -> dict[str, Any]:
    """解析命令行 ``--set a.b=value`` 列表。"""
    result: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ConfigError(f"--set 参数格式应为 key=value，实际为 {item!r}")
        key, raw = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"--set 参数缺少 key: {item!r}")
        set_dotted(result, key.split("."), _parse_scalar(raw))
    return result


def station_file(station: str) -> Path:
    candidate = Path(station)
    if candidate.suffix in {".yaml", ".yml"} and candidate.exists():
        return candidate.resolve()
    path = config_dir() / "stations" / f"{station}.yaml"
    if not path.exists():
        raise ConfigError(f"找不到工位配置 '{station}'（期望文件 {path}）。可用工位: {', '.join(list_stations())}")
    return path


def list_stations() -> list[str]:
    d = config_dir() / "stations"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def load_settings(
    station: str,
    *,
    overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    use_local: bool = True,
) -> AppSettings:
    sources: list[str] = []
    merged: dict[str, Any] = {}

    default_path = config_dir() / "default.yaml"
    if default_path.exists():
        merged = deep_merge(merged, _load_with_includes(default_path, sources))

    st_path = station_file(station)
    merged = deep_merge(merged, _load_with_includes(st_path, sources))

    local_path = config_dir() / "local.yaml"
    if use_local and local_path.exists():
        local = _load_with_includes(local_path, sources)
        per_station = local.pop("stations", {}) or {}
        merged = deep_merge(merged, local)
        station_name = merged.get("station") or st_path.stem
        if isinstance(per_station, Mapping) and isinstance(per_station.get(station_name), Mapping):
            merged = deep_merge(merged, per_station[station_name])

    env = _env_overrides(os.environ if environ is None else environ)
    if env:
        merged = deep_merge(merged, env)
        sources.append("<env>")
    if overrides:
        merged = deep_merge(merged, overrides)
        sources.append("<cli>")

    station_name = str(merged.get("station") or st_path.stem)
    app = merged.get("app")
    if not app or not isinstance(app, str):
        raise ConfigError(f"工位配置 {st_path} 缺少 'app' 字段（应用模块路径，如 power_cycle.relay_power_on）")

    logging_settings = from_dict(LoggingSettings, merged.get("logging") or {}, "logging")
    return AppSettings(
        station=station_name,
        app=app,
        description=str(merged.get("description", "")),
        logging=logging_settings,
        data=types.MappingProxyType(merged),
        sources=tuple(sources),
    )

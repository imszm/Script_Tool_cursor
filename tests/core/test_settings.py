from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from ppx_testkit.exceptions import ConfigError
from ppx_testkit.settings import (
    LoggingSettings,
    deep_merge,
    from_dict,
    list_stations,
    load_settings,
    parse_bytes,
    parse_set_args,
)


@dataclass(frozen=True)
class Inner:
    port: str | None = None
    vid: int | None = None


@dataclass(frozen=True)
class Outer:
    name: str
    count: int = 1
    ratio: float = 0.5
    enabled: bool = True
    mode: Literal["a", "b"] = "a"
    tags: list[str] = field(default_factory=list)
    pair: tuple[int, int] = (0, 0)
    inner: Inner = field(default_factory=Inner)
    table: dict[int, str] = field(default_factory=dict)
    cmd: bytes = b""
    path: Path | None = None


class TestFromDict:
    def test_full_conversion(self, project_tmp: Path) -> None:
        obj = from_dict(
            Outer,
            {
                "name": "x",
                "count": "0x10",
                "ratio": 2,
                "enabled": "yes",
                "mode": "b",
                "tags": ["t1", 2],
                "pair": [1, 2],
                "inner": {"port": "COM5", "vid": "0x1A86"},
                "table": {"1": "on"},
                "cmd": "A0 01 01 A2",
                "path": "resources/x.dll",
            },
        )
        assert obj.count == 16 and obj.ratio == 2.0 and obj.enabled is True
        assert obj.tags == ["t1", "2"] and obj.pair == (1, 2)
        assert obj.inner == Inner(port="COM5", vid=0x1A86)
        assert obj.table == {1: "on"}
        assert obj.cmd == bytes([0xA0, 0x01, 0x01, 0xA2])
        assert obj.path == project_tmp / "resources" / "x.dll"

    def test_missing_required(self) -> None:
        with pytest.raises(ConfigError, match="缺少必填配置项 'name'"):
            from_dict(Outer, {})

    def test_unknown_field_lists_available(self) -> None:
        with pytest.raises(ConfigError, match="未知字段.*'nmae'"):
            from_dict(Outer, {"name": "x", "nmae": "typo"})

    @pytest.mark.parametrize(
        ("data", "msg"),
        [
            ({"name": "x", "count": True}, "整数"),
            ({"name": "x", "count": "abc"}, "整数"),
            ({"name": "x", "enabled": "maybe"}, "布尔"),
            ({"name": "x", "mode": "c"}, "只能取"),
            ({"name": "x", "tags": "notalist"}, "列表"),
            ({"name": "x", "pair": [1]}, "2 个元素"),
            ({"name": "x", "inner": {"bad": 1}}, "inner"),
            ({"name": "x", "cmd": "ZZ"}, "字节"),
        ],
    )
    def test_type_errors(self, data: dict, msg: str) -> None:
        with pytest.raises(ConfigError, match=msg):
            from_dict(Outer, data)

    def test_post_init_validation_wrapped(self) -> None:
        with pytest.raises(ConfigError, match="非法日志级别"):
            from_dict(LoggingSettings, {"console_level": "LOUD"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0x50, b"\x50"),
        ([0xA0, "0x02", 1], b"\xa0\x02\x01"),
        ("A0 02 01 A3", b"\xa0\x02\x01\xa3"),
        ("0xA0,0x02", b"\xa0\x02"),
        ("ascii:#000P2500T1000!", b"#000P2500T1000!"),
        (b"\x01", b"\x01"),
    ],
)
def test_parse_bytes(value: object, expected: bytes) -> None:
    assert parse_bytes(value) == expected


def test_parse_bytes_rejects_bool() -> None:
    with pytest.raises(ConfigError):
        parse_bytes(True)


def test_deep_merge_does_not_mutate() -> None:
    base = {"a": {"b": 1, "c": [1]}, "x": 1}
    out = deep_merge(base, {"a": {"b": 2, "c": [9]}})
    assert out == {"a": {"b": 2, "c": [9]}, "x": 1}
    assert base == {"a": {"b": 1, "c": [1]}, "x": 1}


def test_parse_set_args() -> None:
    assert parse_set_args(["a.b=3", "a.c=true", "d=COM5"]) == {"a": {"b": 3, "c": True}, "d": "COM5"}
    with pytest.raises(ConfigError):
        parse_set_args(["novalue"])
    with pytest.raises(ConfigError):
        parse_set_args(["=1"])


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestLoadSettings:
    def test_priority_chain(self, project_tmp: Path) -> None:
        cfg = project_tmp / "config"
        _write(cfg / "default.yaml", "logging: {console_level: WARNING}\nserial: {relay: {baudrate: 9600}}\n")
        _write(cfg / "common" / "relay.yaml", "relay: {repeat: 2}\nserial: {relay: {timeout: 0.2}}\n")
        _write(
            cfg / "stations" / "demo.yaml",
            "include: ../common/relay.yaml\napp: power_cycle.relay_power_on\ndescription: 演示\n"
            "serial: {relay: {match: {description_contains: [CH340]}}}\ntest: {cycles: 10}\n",
        )
        _write(
            cfg / "local.yaml",
            "serial: {relay: {baudrate: 115200}}\nstations: {demo: {serial: {relay: {match: {port: COM7}}}}, other: {x: 1}}\n",
        )
        s = load_settings("demo", environ={"PPX__TEST__CYCLES": "20", "OTHER": "1"}, overrides={"relay": {"repeat": 3}})
        assert s.station == "demo" and s.app == "power_cycle.relay_power_on" and s.description == "演示"
        assert s.logging.console_level == "WARNING"
        assert s.raw("serial.relay") == {
            "baudrate": 115200,
            "timeout": 0.2,
            "match": {"description_contains": ["CH340"], "port": "COM7"},
        }
        assert s.raw("test.cycles") == 20
        assert s.raw("relay.repeat") == 3
        assert "x" not in s.data
        assert s.sources[-2:] == ("<env>", "<cli>")

    def test_use_local_false(self, project_tmp: Path) -> None:
        _write(project_tmp / "config" / "stations" / "demo.yaml", "app: a.b\nk: 1\n")
        _write(project_tmp / "config" / "local.yaml", "k: 2\n")
        assert load_settings("demo", environ={}, use_local=False).raw("k") == 1
        assert load_settings("demo", environ={}).raw("k") == 2

    def test_missing_app(self, project_tmp: Path) -> None:
        _write(project_tmp / "config" / "stations" / "demo.yaml", "k: 1\n")
        with pytest.raises(ConfigError, match="缺少 'app'"):
            load_settings("demo", environ={})

    def test_unknown_station_lists_available(self, project_tmp: Path) -> None:
        _write(project_tmp / "config" / "stations" / "known.yaml", "app: a\n")
        assert list_stations() == ["known"]
        with pytest.raises(ConfigError, match="可用工位: known"):
            load_settings("nope", environ={})

    def test_yaml_syntax_error(self, project_tmp: Path) -> None:
        _write(project_tmp / "config" / "stations" / "bad.yaml", "app: [unclosed\n")
        with pytest.raises(ConfigError, match="YAML 语法错误"):
            load_settings("bad", environ={})

    def test_include_cycle(self, project_tmp: Path) -> None:
        st = project_tmp / "config" / "stations"
        _write(st / "a.yaml", "include: b.yaml\napp: x\n")
        _write(st / "b.yaml", "include: a.yaml\n")
        with pytest.raises(ConfigError, match="循环引用"):
            load_settings("a", environ={})

    def test_raw_section_has(self, project_tmp: Path) -> None:
        _write(project_tmp / "config" / "stations" / "demo.yaml", "app: x\ninner: {port: COM1}\n")
        s = load_settings("demo", environ={})
        assert s.section("inner", Inner) == Inner(port="COM1")
        assert s.section("absent", Inner, required=False) == Inner()
        with pytest.raises(ConfigError, match="缺少配置段"):
            s.section("absent", Inner)
        assert s.has("inner.port") and not s.has("inner.vid")
        assert s.raw("nope.deep", 5) == 5
        copy = s.raw("inner")
        copy["port"] = "changed"
        assert s.raw("inner.port") == "COM1"
        assert "port: COM1" in s.to_yaml()


def test_real_default_config_loads() -> None:
    """仓库内的 default.yaml 必须可被解析。"""
    import yaml

    from ppx_testkit.utils.paths import config_dir

    data = yaml.safe_load((config_dir() / "default.yaml").read_text(encoding="utf-8"))
    from_dict(LoggingSettings, data["logging"], "logging")

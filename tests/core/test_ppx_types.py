"""ctypes 定义必须与 C 头文件逐字段一致（字段名、顺序、宽度、位域）。

直接解析 ``resources/include/{l5,r3}`` 下的头文件做比对，头文件更新后本测试会第一时间报警。
"""

from __future__ import annotations

import ctypes
import re
from pathlib import Path

import pytest

from ppx_testkit.core.protocol import ppx_types as T
from ppx_testkit.utils.paths import resources_dir

C_TYPES = {
    "uint8_t": ctypes.c_uint8, "int8_t": ctypes.c_int8, "uint16_t": ctypes.c_uint16,
    "int16_t": ctypes.c_int16, "uint32_t": ctypes.c_uint32, "int32_t": ctypes.c_int32,
}
# 嵌套结构体本身也由本文件逐字段校验，这里直接取其 ctypes 尺寸
STRUCT_TYPES = {
    ("l5", "ppx_led_msg_t"): T.LedMsgV1,
    ("l5", "ppx_region_excp_t"): T.RegionExcp,
    ("r3", "ppx_region_excp_t"): T.RegionExcp,
    ("r3", "ppx_region_msg_t"): T.RegionMsg,
    ("r3", "ppx_region_data_t"): T.RegionDataV2,
}


def _include(variant: str) -> Path:
    return resources_dir() / "include" / variant


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _macros(variant: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for h in _include(variant).glob("*.h"):
        for name, value in re.findall(r"#define\s+(\w+)\s+\(\s*(\d+)\s*\)", h.read_text(encoding="utf-8", errors="replace")):
            out[name] = int(value)
    return out


def _block(variant: str, header: str, typedef: str, kind: str) -> str:
    text = _strip_comments((_include(variant) / header).read_text(encoding="utf-8", errors="replace"))
    m = re.search(rf"typedef\s+{kind}\s*\{{([^{{}}]*)\}}\s*{typedef}\s*;", text, flags=re.S)
    assert m, f"{header} 中找不到 {typedef}"
    return m.group(1)


def c_struct_fields(variant: str, header: str, typedef: str) -> list[tuple[str, int, int | None]]:
    """返回 [(字段名, 字节宽度, 位域宽度或 None)]。"""
    macros = _macros(variant)
    fields = []
    for decl in _block(variant, header, typedef, "struct").split(";"):
        decl = " ".join(decl.split())
        if not decl:
            continue
        m = re.fullmatch(r"(\w+)\s+(\w+)\s*(?:\[(\w+)\])?\s*(?::\s*(\d+))?", decl)
        assert m, f"无法解析声明: {decl!r}"
        ctype, name, dim, bits = m.groups()
        tp = C_TYPES.get(ctype) or STRUCT_TYPES[(variant, ctype)]
        size = ctypes.sizeof(tp)
        if dim:
            size *= int(dim) if dim.isdigit() else macros[dim]
        fields.append((name, size, int(bits) if bits else None))
    return fields


def c_enum(variant: str, header: str, typedef: str) -> list[tuple[str, int]]:
    out, value = [], -1
    for item in _block(variant, header, typedef, "enum").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, expr = (p.strip() for p in item.partition("="))
        value = int(expr, 0) if expr else value + 1
        out.append((name, value))
    return out


def py_fields(cls: type[ctypes.Structure]) -> list[tuple[str, int, int | None]]:
    out = []
    for f in cls._fields_:
        name, tp = f[0], f[1]
        bits = f[2] if len(f) > 2 else None
        out.append((name, ctypes.sizeof(tp), bits))
    return out


@pytest.mark.parametrize(
    ("variant", "header", "typedef", "cls"),
    [
        ("l5", "ppx_region.h", "ppx_region_data_t", T.RegionDataV1),
        ("l5", "ppx_region.h", "ppx_region_msg_t", T.RegionMsg),
        ("l5", "ppx_region.h", "ppx_region_excp_t", T.RegionExcp),
        ("l5", "ppx_ble.h", "ppx_ble_data_t", T.BleDataV1),
        ("l5", "ppx_ble.h", "ppx_led_msg_t", T.LedMsgV1),
        ("l5", "ppx_ble.h", "ppx_ble_msg_t", T.BleMsg),
        ("r3", "ppx_region.h", "ppx_region_data_t", T.RegionDataV2),
        ("r3", "ppx_region.h", "ppx_region_msg_t", T.RegionMsg),
        ("r3", "ppx_region.h", "ppx_region_ctrl_t", T.RegionCtrlV2),
        ("r3", "ppx_packet.h", "ppx_packet_data_t", T.PacketDataV2),
    ],
)
def test_struct_matches_header(variant: str, header: str, typedef: str, cls: type[ctypes.Structure]) -> None:
    assert py_fields(cls) == c_struct_fields(variant, header, typedef)


def test_packed_sizes() -> None:
    for cls, fields in ((T.RegionDataV1, ("l5", "ppx_region.h", "ppx_region_data_t")),
                        (T.RegionDataV2, ("r3", "ppx_region.h", "ppx_region_data_t")),
                        (T.BleDataV1, ("l5", "ppx_ble.h", "ppx_ble_data_t"))):
        assert ctypes.sizeof(cls) == sum(size for _, size, _ in c_struct_fields(*fields))
    assert ctypes.sizeof(T.RegionMsg) == 8


def test_led_msg_layout_follows_c_bitfield_rules() -> None:
    """头文件注释写的是 64bit，但 uint32_t 位域不能跨存储单元（GCC/MSVC 一致），实际为 12 字节。"""

    class FromHeader(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint32, b) for n, _, b in c_struct_fields("l5", "ppx_ble.h", "ppx_led_msg_t")]

    assert ctypes.sizeof(T.LedMsgV1) == ctypes.sizeof(FromHeader) == 12
    assert T.LedMsgV1.digital.offset == 4 and T.LedMsgV1.rsvd_data.offset == 8


def test_constants_match_headers() -> None:
    l5, r3 = _macros("l5"), _macros("r3")
    assert (l5["PPX_SW_VER_SIZE"], r3["PPX_SW_VER_SIZE"]) == (T.PPX_SW_VER_SIZE_V1, T.PPX_SW_VER_SIZE_V2)
    assert (l5["PPX_DATA_BUF_SIZE"], r3["PPX_DATA_BUF_SIZE"]) == (T.PPX_DATA_BUF_SIZE_V1, T.PPX_DATA_BUF_SIZE_V2)
    assert l5["PPX_MODEL_SIZE"] == r3["PPX_MODEL_SIZE"] == T.PPX_MODEL_SIZE
    assert l5["PPX_SN_SIZE"] == r3["PPX_SN_SIZE"] == T.PPX_SN_SIZE
    assert l5["PPX_PACKET_MIN_SIZE"] == T.PPX_PACKET_MIN_SIZE


def _norm(name: str, prefix: str) -> str:
    name = name.removeprefix(prefix).removesuffix("_REG")
    return name.replace("VESRION", "VERSION")


@pytest.mark.parametrize(
    ("variant", "header", "typedef", "enum", "prefix"),
    [
        ("l5", "ppx_region.h", "ppx_region_reg_t", T.RegV1, "PPX_"),
        ("r3", "ppx_region.h", "ppx_region_reg_t", T.RegV2, "PPX_"),
        ("l5", "ppx_ble.h", "ppx_ble_reg_t", T.BleRegV1, "PPX_BLE_"),
    ],
)
def test_register_enum_matches_header(variant, header, typedef, enum, prefix) -> None:
    sentinels = {"PPX_MAX_REGION_REG", "PPX_BLE_MAX_REG"}
    c_items = [(_norm(n, prefix), v) for n, v in c_enum(variant, header, typedef) if n not in sentinels]
    assert [(m.name, m.value) for m in enum] == c_items


@pytest.mark.parametrize(
    ("mapping", "cls"),
    [(T.REG_FIELD_V1, T.RegionDataV1), (T.REG_FIELD_V2, T.RegionDataV2)],
)
def test_reg_field_maps_point_to_real_fields(mapping: dict[int, str], cls: type[ctypes.Structure]) -> None:
    names = {f[0] for f in cls._fields_}
    assert set(mapping.values()) <= names


def test_frame_helpers_and_strings() -> None:
    assert T.is_exception_cmd(0xC3) and not T.is_exception_cmd(0x83)
    assert T.looks_like_frame(bytes([0xA5, 0, 0, 0, 0, 0, 0, 0, 0x55]))
    assert not T.looks_like_frame(bytes([0xA5, 0x55]))
    data = T.RegionDataV1()
    data.model[:4] = list(b"L5PX")
    data.hw_version = 0x0102
    d = T.struct_to_dict(data)
    assert d["model"] == "L5PX" and d["hw_version"] == 0x0102


def test_led_bitfields_roundtrip() -> None:
    led = T.LedMsgV1()
    led.digital = 100
    led.turn_left = 2
    led.ring = 1
    raw = bytes(led)
    back = T.LedMsgV1.from_buffer_copy(raw)
    assert (back.digital, back.turn_left, back.ring) == (100, 2, 1)

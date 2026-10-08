"""测试用例文件加载（xlsx / csv），以及单元格值的宽松转换。"""

from __future__ import annotations

import csv
import io
import logging
import math
from pathlib import Path
from typing import Any

from ppx_testkit.exceptions import ConfigError

log = logging.getLogger(__name__)

CSV_ENCODINGS = ("utf-8-sig", "gb18030")


def detect_encoding(raw: bytes) -> str:
    for enc in CSV_ENCODINGS:
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    try:
        import chardet  # type: ignore[import-untyped]

        guess = chardet.detect(raw).get("encoding")
        if guess:
            return str(guess)
    except ImportError:
        pass
    return "latin-1"


def _read_csv(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    enc = detect_encoding(raw)
    log.info("CSV 用例编码识别为 %s: %s", enc, path)
    reader = csv.DictReader(io.StringIO(raw.decode(enc, errors="replace")))
    return [{(k or "").strip(): v for k, v in row.items()} for row in reader]


def _read_excel(path: Path) -> list[dict[str, Any]]:
    try:
        import pandas as pd  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ConfigError("读取 xlsx 用例需要安装 pandas 与 openpyxl（pip install .[report]）") from exc
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]
    rows = df.to_dict(orient="records")
    return [{k: (None if _is_nan(v) else v) for k, v in row.items()} for row in rows]


def _is_nan(v: Any) -> bool:
    return isinstance(v, float) and math.isnan(v)


def load_cases(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ConfigError(f"用例文件不存在: {path}")
    ext = path.suffix.lower()
    try:
        if ext in (".xlsx", ".xls"):
            rows = _read_excel(path)
        elif ext == ".csv":
            rows = _read_csv(path)
        else:
            raise ConfigError(f"不支持的用例文件格式: {ext}（仅支持 .xlsx/.xls/.csv）")
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - pandas/csv 异常类型众多，统一转换
        raise ConfigError(f"读取用例文件失败: {path}: {exc}") from exc
    rows = [r for r in rows if any(v not in (None, "") for v in r.values())]
    if not rows:
        raise ConfigError(f"用例文件为空: {path}")
    log.info("已加载 %d 条用例: %s", len(rows), path)
    return rows


def to_int(value: Any) -> int | None:
    if value is None or _is_nan(value):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s, 0)
        except ValueError:
            pass
        try:
            return int(float(s))
        except ValueError:
            return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> float | None:
    if value is None or _is_nan(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_TRUE = {"1", "true", "t", "yes", "y", "on", "是", "开"}
_FALSE = {"0", "false", "f", "no", "n", "off", "否", "关"}


def to_bool_int(value: Any) -> int | None:
    if value is None or _is_nan(value):
        return None
    if isinstance(value, (int, float)):
        return 1 if value != 0 else 0
    s = str(value).strip().lower()
    if not s:
        return None
    if s in _TRUE:
        return 1
    if s in _FALSE:
        return 0
    f = to_float(s)
    return None if f is None else (1 if f != 0 else 0)

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from script_tool.config.defaults import DEFAULT_CONFIG


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)  # type: ignore[index]
        else:
            dst[k] = v
    return dst


def load_config(path: str | None) -> dict[str, Any]:
    cfg = deepcopy(DEFAULT_CONFIG)
    if not path:
        return cfg

    p = Path(path)
    raw: str | None = None
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8"):
        try:
            raw = p.read_text(encoding=enc)
            break
        except Exception as e:
            last_err = e
    if raw is None:
        raise RuntimeError(f"Failed to read config file: {p} ({last_err})") from last_err

    # 先支持 JSON（零依赖）；后续如需 YAML 再加依赖
    user_cfg = json.loads(raw)
    if not isinstance(user_cfg, dict):
        raise ValueError("config file must be a JSON object")

    return _deep_merge(cfg, user_cfg)


def apply_overrides(cfg: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    return _deep_merge(cfg, overrides)


"""项目根目录与资源路径解析。

查找顺序：
1. 环境变量 ``PPX_TESTKIT_ROOT``；
2. PyInstaller 打包后的 ``sys._MEIPASS``；
3. 从本文件向上查找同时包含 ``config/`` 与 ``pyproject.toml`` 的目录；
4. 当前工作目录。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

ENV_ROOT = "PPX_TESTKIT_ROOT"


@lru_cache(maxsize=1)
def project_root() -> Path:
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).resolve()

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config").is_dir() and (parent / "pyproject.toml").is_file():
            return parent

    return Path.cwd().resolve()


def config_dir() -> Path:
    return project_root() / "config"


def resources_dir() -> Path:
    return project_root() / "resources"


def resolve_path(value: str | os.PathLike[str], base: Path | None = None) -> Path:
    """把配置里的相对路径解析为绝对路径（默认相对项目根目录）。"""
    p = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if p.is_absolute():
        return p
    return ((base or project_root()) / p).resolve()

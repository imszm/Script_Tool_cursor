"""PPX 协议 DLL 的通用加载。

机型目录、文件名和默认节点在 ``ppx_testkit.profiles``。本模块只按调用方给出的路径加载，
不区分 L5 / R3。
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import sys
from pathlib import Path
from typing import Any

from ppx_testkit.exceptions import DllLoadError

log = logging.getLogger(__name__)


class PpxDll:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.lib = self._load()

    def _load(self) -> ctypes.CDLL:
        if not self.path.is_file():
            raise DllLoadError(f"DLL 文件不存在: {self.path}")
        dll_dir_handle = None
        try:
            if sys.platform == "win32":
                dll_dir_handle = os.add_dll_directory(str(self.path.parent))
                lib = ctypes.CDLL(str(self.path), winmode=0)
            else:
                lib = ctypes.CDLL(str(self.path))
        except OSError as exc:
            bits = struct.calcsize("P") * 8
            raise DllLoadError(
                f"加载 DLL 失败: {self.path}: {exc}。当前 Python 为 {bits} 位，"
                "请确认 DLL 位数一致且运行于 Windows"
            ) from exc
        finally:
            if dll_dir_handle is not None:
                dll_dir_handle.close()
        log.info("已加载 DLL: %s", self.path)
        return lib

    def bind(self, name: str, argtypes: list[Any], restype: Any) -> Any:
        try:
            fn = getattr(self.lib, name)
        except AttributeError as exc:
            raise DllLoadError(f"DLL {self.path.name} 缺少导出函数 {name}") from exc
        fn.argtypes = argtypes
        fn.restype = restype
        return fn

    def global_var(self, ctype: Any, name: str) -> Any:
        try:
            return ctype.in_dll(self.lib, name)
        except ValueError as exc:
            raise DllLoadError(f"DLL {self.path.name} 缺少全局变量 {name}") from exc

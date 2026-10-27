"""机型协议与常量。L5 / R3 的 DLL 路径从这里取，加载仍由 core.protocol.dll_loader 完成。"""

from ppx_testkit.profiles.l5 import L5
from ppx_testkit.profiles.r3 import R3

__all__ = ["L5", "R3"]

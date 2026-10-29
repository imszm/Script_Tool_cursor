"""机型协议与常量。

L5 / R3 的 DLL 路径从这里取，加载仍由 core.protocol.dll_loader 完成。
W3 没有 region DLL，只登记上位机身份。P3 的通道状态机在 ``p3.py``，不进这张表。
"""

from ppx_testkit.exceptions import ConfigError
from ppx_testkit.profiles.base import ProtocolProfile
from ppx_testkit.profiles.l5 import L5
from ppx_testkit.profiles.r3 import R3
from ppx_testkit.profiles.w3 import W3, W3Model

MODELS: dict[str, object] = {
    L5.model: L5,
    R3.model: R3,
    W3.model: W3,
}

__all__ = ["L5", "MODELS", "R3", "W3", "W3Model", "get_model"]


def get_model(name: str) -> ProtocolProfile | W3Model:
    """按机型名取出已注册的 profile。

    :param name: ``l5``、``r3`` 或 ``w3``，大小写不敏感。
    :return: 对应的 :class:`ProtocolProfile` 或 :class:`W3Model`。
    :raises ConfigError: 未注册的机型名。
    """
    key = name.strip().lower()
    try:
        return MODELS[key]
    except KeyError as exc:
        known = ", ".join(sorted(MODELS))
        raise ConfigError(f"未知机型 {name!r}，已注册: {known}") from exc

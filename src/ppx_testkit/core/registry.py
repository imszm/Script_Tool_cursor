"""工位与工具的自注册表。

新入口用装饰器登记。尚未迁移的 YAML 工位不在这里，调用方继续走原来的导入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

Kind = Literal["station", "tool"]


@dataclass(frozen=True)
class StationMeta:
    """一条已注册入口。

    :param name: 注册名，工位名或 ``app`` 路径。
    :param target: 被装饰的类或函数。
    :param kind: ``station`` 或 ``tool``。
    :param description: 简短说明。
    :param caution: 网页提示；空字符串表示继续用 catalog 里的旧文案。
    """

    name: str
    target: Any
    kind: Kind
    description: str = ""
    caution: str = ""


STATION_REGISTRY: dict[str, StationMeta] = {}
TOOL_REGISTRY: dict[str, StationMeta] = {}


def register_station(
    name: str,
    *,
    description: str = "",
    caution: str = "",
) -> Callable[[F], F]:
    """登记受控工位。同名再次导入时覆盖为最新对象。

    :param name: 注册名。
    :param description: 说明。
    :param caution: 非空时网页优先显示这段提示。
    :return: 装饰器，原样返回被装饰对象。
    """

    def decorate(target: F) -> F:
        _store(STATION_REGISTRY, name, target, "station", description, caution)
        return target

    return decorate


def register_tool(
    name: str,
    *,
    description: str = "",
    caution: str = "",
) -> Callable[[F], F]:
    """登记不进压测报告的辅助工具。

    :param name: 注册名。
    :param description: 说明。
    :param caution: 非空时网页优先显示这段提示。
    :return: 装饰器，原样返回被装饰对象。
    """

    def decorate(target: F) -> F:
        _store(TOOL_REGISTRY, name, target, "tool", description, caution)
        return target

    return decorate


def lookup_registered(name: str) -> StationMeta | None:
    """按名称查找已注册入口。先工位，后工具。

    :param name: 工位名或 app 路径。
    :return: 命中的元数据；都没有时返回 None。
    """
    return STATION_REGISTRY.get(name) or TOOL_REGISTRY.get(name)


def _store(
    registry: dict[str, StationMeta],
    name: str,
    target: Any,
    kind: Kind,
    description: str,
    caution: str,
) -> None:
    """写入注册表。

    :param registry: ``STATION_REGISTRY`` 或 ``TOOL_REGISTRY``。
    :param name: 注册名，不能为空白。
    :param target: 被装饰对象。
    :param kind: 入口类别。
    :param description: 说明。
    :param caution: 网页提示。
    :raises ValueError: 注册名为空。
    """
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("注册名不能为空")
    registry[cleaned] = StationMeta(
        name=cleaned,
        target=target,
        kind=kind,
        description=description,
        caution=caution,
    )

"""W3 机型身份。

W3 不使用 ppx_region.dll。上位机窗口和进程名给点击压测做默认值；
电源键时序、关键字和点击坐标仍在工位 yaml，这里不复制一份。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class W3PcTool:
    """W3 PCTOOL 的窗口标题和进程名。

    与 ``config/stations/_fragments/w3_pctool_clicks.yaml`` 中已提交的值一致。
    工位 yaml 写了同名字段时以 yaml 为准。

    :param window_title: 窗口标题包含匹配用的字符串。
    :param process_keyword: 闪退重启时按进程名包含关系结束旧进程。
    """

    window_title: str = "W3 PCTOOL V5.4.00"
    process_keyword: str = "EW01_PCTOOL"


@dataclass(frozen=True)
class W3Model:
    """W3 机型。没有 region / BLE DLL。

    :param model: 注册名 ``w3``。
    :param pctool: 上位机身份。
    """

    model: str = "w3"
    pctool: W3PcTool = field(default_factory=W3PcTool)


W3 = W3Model()

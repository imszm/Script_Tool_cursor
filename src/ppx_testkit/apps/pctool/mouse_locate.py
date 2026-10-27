"""兼容壳。鼠标坐标采样已迁到 ``ppx_testkit.tools.mouse_locate``。"""

from ppx_testkit.tools.mouse_locate import LocateConfig, PositionSource, ScreenPosition, locate_loop, run

__all__ = ["LocateConfig", "PositionSource", "ScreenPosition", "locate_loop", "run"]

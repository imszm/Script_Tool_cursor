"""兼容壳。LRD 点灯调试已迁到 ``ppx_testkit.tools.lrd_debug``。"""

from ppx_testkit.tools.lrd_debug import LrdLedConfig, apply_and_verify, run

__all__ = ["LrdLedConfig", "apply_and_verify", "run"]

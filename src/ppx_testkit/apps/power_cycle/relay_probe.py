"""兼容壳。继电器探测已迁到 ``ppx_testkit.tools.relay_probe``。"""

from ppx_testkit.tools.relay_probe import SECTION, ProbeStep, RelayProbeConfig, probe, run

__all__ = ["SECTION", "ProbeStep", "RelayProbeConfig", "probe", "run"]

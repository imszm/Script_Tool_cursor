"""应用层：每个模块对应一个（或一类）测试工位。

约定：模块提供 ``run(settings: AppSettings, ctx: RunContext) -> int``，返回进程退出码。
应用只负责“读配置 -> 组装 core 对象 -> 编排流程”，不直接操作 pyserial / ctypes。
"""

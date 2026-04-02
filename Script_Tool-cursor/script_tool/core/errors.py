from __future__ import annotations


class ScriptToolError(RuntimeError):
    pass


class DriverError(ScriptToolError):
    pass


class DriverConnectError(DriverError):
    pass


class UiError(ScriptToolError):
    pass


class UiConnectError(UiError):
    pass


class VisionError(ScriptToolError):
    pass


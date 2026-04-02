from __future__ import annotations

from typing import Protocol

from script_tool.core.context import RunContext


class Task(Protocol):
    name: str

    def setup(self, ctx: RunContext) -> None: ...

    def run(self, ctx: RunContext) -> int: ...

    def teardown(self, ctx: RunContext) -> None: ...


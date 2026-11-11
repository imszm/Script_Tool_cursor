"""分层守卫：core、profiles、apps、web 不得反向依赖，也不得使用相对导入。"""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "ppx_testkit"

# 每一层禁止导入的模块。命中模块本身或其子模块都算违规。
_FORBIDDEN: dict[str, tuple[str, ...]] = {
    "core": (
        "ppx_testkit.apps",
        "ppx_testkit.profiles",
        "ppx_testkit.web",
        "ppx_testkit.cli",
        "ppx_testkit.station_schema",
    ),
    "profiles": (
        "ppx_testkit.apps",
        "ppx_testkit.web",
        "ppx_testkit.cli",
        "ppx_testkit.station_schema",
    ),
    "apps": (
        "ppx_testkit.web",
        "ppx_testkit.cli",
        "serial",
        "ctypes",
    ),
    "web": (
        "ppx_testkit.apps",
        "ppx_testkit.core.protocol",
        "ppx_testkit.core.relay",
        "serial",
        "ctypes",
    ),
}


def _imported_modules(path: Path) -> list[tuple[int, str, bool]]:
    """收集一个源文件里的导入，包括函数内部的导入。

    :param path: Python 源文件。
    :return: ``(行号, 模块名, 是否相对导入)``。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name, False))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                module = "." * node.level + (node.module or "")
                found.append((node.lineno, module, True))
            elif node.module:
                found.append((node.lineno, node.module, False))
    return found


def _matches(name: str, banned: str) -> bool:
    """模块名是否等于禁止项，或落在它的子模块里。

    :param name: 实际导入的模块名。
    :param banned: 该层禁止的模块。
    :return: 命中时为 True。
    """
    return name == banned or name.startswith(banned + ".")


def test_layers_do_not_import_forbidden_modules() -> None:
    """四层源码没有相对导入，也没有导入该层禁止的模块。"""
    violations: list[str] = []
    for layer, banned in _FORBIDDEN.items():
        root = _PACKAGE / layer
        for path in sorted(root.rglob("*.py")):
            for lineno, name, relative in _imported_modules(path):
                rel = path.relative_to(_PACKAGE)
                if relative:
                    violations.append(f"{rel}:{lineno} 相对导入 {name}")
                    continue
                for item in banned:
                    if _matches(name, item):
                        violations.append(f"{rel}:{lineno} 禁止导入 {name}")
                        break
    assert not violations, "\n".join(violations)

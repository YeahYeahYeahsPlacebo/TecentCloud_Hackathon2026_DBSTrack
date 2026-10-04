"""
Constraint #2 (CONTEXT.md): the parser and validator must not import the
ledger. They must not import the gateway either, since the gateway can
write to the ledger.

This statically inspects the AST import nodes of every .py file under
backend/parser and backend/validator. Nothing is imported or executed.
The folders hold no code yet; the scaffold still runs, and the checker
itself is tested against synthetic sources so it cannot pass vacuously.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

GUARDED_PACKAGES = ["backend/parser", "backend/validator"]
FORBIDDEN_MODULES = ["backend.ledger", "backend.gateway"]


def _is_forbidden(module: str) -> bool:
    return any(module == f or module.startswith(f + ".") for f in FORBIDDEN_MODULES)


def _resolve_relative(package: str, module: str | None, level: int) -> str:
    """Turn `from ..x import y` inside `package` into an absolute module name."""
    parts = package.split(".")
    if level > len(parts):
        return module or ""
    base = parts[: len(parts) - level + 1]
    return ".".join(base + ([module] if module else []))


def forbidden_imports(source: str, module_name: str) -> list[str]:
    """Return the forbidden modules imported by *source*.

    *module_name* is the dotted name of the file, e.g. "backend.parser.core",
    used to resolve relative imports.
    """
    package = module_name.rsplit(".", 1)[0]
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if _is_forbidden(a.name)]
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(package, node.module, node.level) if node.level else (node.module or "")
            if _is_forbidden(base):
                found.append(base)
            else:
                # `from backend import ledger`
                found += [f"{base}.{a.name}" for a in node.names if _is_forbidden(f"{base}.{a.name}")]
        elif isinstance(node, ast.Call):
            # importlib.import_module("backend.ledger") / __import__("backend.ledger")
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _is_forbidden(arg.value):
                    found.append(arg.value)
    return found


def _guarded_files() -> list[Path]:
    return sorted(p for pkg in GUARDED_PACKAGES for p in (ROOT / pkg).rglob("*.py"))


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


# ── The real check ─────────────────────────────────────────────────────


def test_guarded_packages_exist():
    for pkg in GUARDED_PACKAGES:
        assert (ROOT / pkg).is_dir(), f"{pkg} is missing; the import guard would check nothing"


@pytest.mark.parametrize("path", _guarded_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_ledger_or_gateway_imports(path: Path):
    found = forbidden_imports(path.read_text(encoding="utf-8"), _module_name(path))
    assert not found, f"{path.relative_to(ROOT)} imports {found}"


# ── The checker itself ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        "import backend.ledger",
        "import backend.ledger.store as s",
        "from backend.ledger import write",
        "from backend.gateway.execute import run",
        "from backend import ledger",
        "from backend import models, gateway",
        "from ..ledger import write",
        "from ..gateway.execute import run",
        "import importlib\nimportlib.import_module('backend.ledger')",
        "__import__('backend.gateway')",
        "def f():\n    from backend.ledger import x",
    ],
)
def test_checker_flags_forbidden_imports(source: str):
    assert forbidden_imports(source, "backend.parser.core")


@pytest.mark.parametrize(
    "source",
    [
        "import json",
        "from backend.models.draft import TransactionDraft",
        "from backend.canonical import draft_hash",
        "from . import prompts",
        "from .ledger_notes import x",  # backend.parser.ledger_notes, not the ledger
        "import backend.ledgerish",
    ],
)
def test_checker_allows_other_imports(source: str):
    assert forbidden_imports(source, "backend.parser.core") == []

"""Enforce documentation and annotation rules beyond public lint visibility."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "node_modules",
}


def python_files(root: Path) -> list[Path]:
    """Discover repository Python files without traversing dependencies or build output."""
    paths = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in EXCLUDED_DIRECTORIES]
        paths.extend(Path(directory) / name for name in filenames if name.endswith(".py"))
    return sorted(paths)


def assigned_fields(target: ast.expr) -> list[str]:
    """Extract class or self-field names from simple and destructuring assignments."""
    if isinstance(target, ast.Name):
        return [target.id]
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    ):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in assigned_fields(element)]
    return []


def field_violations(node: ast.ClassDef) -> list[str]:
    """Require annotations for declared class fields and directly assigned instance fields."""
    annotated = set()
    assigned = set()
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign):
            annotated.update(assigned_fields(statement.target))
        elif isinstance(statement, ast.Assign):
            assigned.update(
                name for target in statement.targets for name in assigned_fields(target)
            )
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(statement):
                if isinstance(inner, ast.AnnAssign) and isinstance(inner.target, ast.Attribute):
                    annotated.update(assigned_fields(inner.target))
                elif isinstance(inner, ast.Assign):
                    for target in inner.targets:
                        for child in ast.walk(target):
                            if isinstance(child, ast.Attribute):
                                assigned.update(assigned_fields(child))
    return [
        f"{node.name}: field {name} lacks a type annotation"
        for name in sorted(assigned - annotated)
    ]


def definition_violations(source: str) -> list[str]:
    """Report missing docstrings and annotations, including private and nested definitions."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        label = f"{node.name}:{node.lineno}"
        if not (ast.get_docstring(node) or "").strip():
            violations.append(f"{label}: missing docstring")
        if isinstance(node, ast.ClassDef):
            violations.extend(field_violations(node))
            continue
        if node.returns is None:
            violations.append(f"{label}: missing return annotation")
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        for index, argument in enumerate(arguments):
            receiver = (
                index == 0
                and argument.arg in {"self", "cls"}
                and isinstance(parents.get(node), ast.ClassDef)
            )
            if argument.annotation is None and not receiver:
                violations.append(f"{label}: parameter {argument.arg} lacks a type annotation")
    return violations


@pytest.mark.parametrize("path", python_files(ROOT), ids=lambda path: str(path.relative_to(ROOT)))
def test_python_definitions_follow_agent_rules(path: Path) -> None:
    """Keep every owned Python module compliant, including tests and nested helpers."""
    violations = definition_violations(path.read_text(encoding="utf-8"))
    assert not violations, "\n".join(violations)


def test_checker_detects_private_nested_and_constructor_omissions() -> None:
    """Prevent private visibility or nesting from bypassing the repository standards."""
    violations = definition_violations("""
class Example:
    def __init__(self, value):
        self.value = value
    async def _hidden(self, *args, **kwargs):
        def nested(item):
            return item
        return nested(args)
""")
    assert any("Example" in item and "missing docstring" in item for item in violations)
    assert any("field value" in item for item in violations)
    assert any("__init__" in item and "missing return" in item for item in violations)
    assert any("_hidden" in item and "parameter kwargs" in item for item in violations)
    assert any("nested" in item and "parameter item" in item for item in violations)


def test_checker_accepts_documented_typed_definitions() -> None:
    """Allow typed methods, generator contracts, and conventional receiver parameters."""
    assert not definition_violations('''
class Example:
    """Hold an explicitly typed value."""
    value: str
    def __init__(self, value: str) -> None:
        """Store the supplied value."""
        self.value = value
    async def values(self) -> AsyncIterator[str]:
        """Yield the stored value."""
        yield self.value
''')

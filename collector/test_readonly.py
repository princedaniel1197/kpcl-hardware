"""The acquisition path is read-only. (§303, §315)

CLAUDE.md, rule 4: "There is no write method anywhere in the collector package.
Not commented out, not behind a flag, not unused. A test asserts its absence."

This is that test. It inspects the package's own source rather than trusting a
convention, because the guarantee has to survive somebody adding a helper "just
for testing" six months from now.

Note what is deliberately NOT excused: a commented-out call still fails. A
comment is one keystroke from being code, and the rule says not commented out.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).parent

# OPC UA write surface. `set_value` and `set_attribute` are asyncua's older
# aliases for write_value/write_attribute and are just as much a write.
# Creating or deleting nodes and references changes the server's address space,
# which is a write to the source in every sense that matters (Part 4, the
# NodeManagement service set), so those are forbidden too.
FORBIDDEN_CALLS = {
    "write_value", "write_attribute", "write_params", "write_attributes",
    "set_value", "set_attribute", "set_writable", "set_read_only",
    "write_data_type_definition", "write_array_dimensions", "write_value_rank",
    "call_method", "call",
    "add_nodes", "add_variable", "add_object", "add_method", "add_folder",
    "add_property", "add_object_type", "add_variable_type", "add_data_type",
    "add_reference_type", "add_references", "add_reference",
    "delete_nodes", "delete_references", "delete_reference",
    "history_update", "update_history",
}


def source_files() -> list[Path]:
    """Every Python file in the package, at any depth. A subpackage added later
    must not escape inspection because the search only looked one level down."""
    return sorted(p for p in PACKAGE.rglob("*.py")
                  if not p.name.startswith("test_") and "__pycache__" not in p.parts)


def test_the_search_descends_into_subpackages(tmp_path, monkeypatch):
    """The guard on the guard, for depth: a file one directory down is found."""
    nested = tmp_path / "util"
    nested.mkdir()
    (nested / "helper.py").write_text("x = 1\n")
    import sys
    monkeypatch.setattr(sys.modules[__name__], "PACKAGE", tmp_path)
    assert [p.name for p in source_files()] == ["helper.py"]


def test_there_are_source_files_to_check():
    """A guard on the guard: if this ever collects nothing, the rest of this
    module would pass vacuously."""
    files = source_files()
    assert len(files) >= 6, f"only found {[f.name for f in files]}"


def test_no_opcua_write_call_exists_anywhere_in_the_package():
    offenders = []
    for path in source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else None)
            if name in FORBIDDEN_CALLS:
                offenders.append(f"{path.name}:{node.lineno} calls {name}()")
    assert not offenders, (
        "the acquisition path must contain no write call (§303, §315):\n  "
        + "\n  ".join(offenders))


def test_no_write_call_is_hiding_in_a_comment_or_string():
    """Not commented out, says the rule. A commented-out write is a write
    waiting to be uncommented."""
    offenders = []
    for path in source_files():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if not (stripped.startswith("#") or '"' in line or "'" in line):
                continue
            for forbidden in FORBIDDEN_CALLS:
                # Allow prose that names the method; forbid anything that looks
                # like a call.
                if re.search(rf"\b{forbidden}\s*\(", line):
                    offenders.append(f"{path.name}:{number}: {stripped[:80]}")
    assert not offenders, (
        "a write call appears in a comment or string literal:\n  "
        + "\n  ".join(offenders))


def test_no_function_in_the_package_is_named_like_a_write():
    offenders = []
    allowed = {"write", "_write", "write_reading"}   # writes to the ARCHIVE
    for path in source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in FORBIDDEN_CALLS and node.name not in allowed:
                    offenders.append(f"{path.name}:{node.lineno} defines {node.name}")
    assert not offenders, "\n  ".join(offenders)


def test_the_session_module_imports_no_write_helper():
    """asyncua's write surface must not even be imported into the session."""
    session = (PACKAGE / "session.py").read_text()
    tree = ast.parse(session)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not (imported & FORBIDDEN_CALLS)


def test_the_test_itself_would_catch_a_violation(tmp_path):
    """A test that cannot fail proves nothing. This plants a write call in a
    throwaway file and checks the same AST rule flags it."""
    planted = tmp_path / "violation.py"
    planted.write_text("async def go(node):\n    await node.write_value(1.0)\n")
    tree = ast.parse(planted.read_text())
    found = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in FORBIDDEN_CALLS]
    assert found, "the detection rule failed to catch a planted write_value()"

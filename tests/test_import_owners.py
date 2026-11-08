import ast
from pathlib import Path

import pytest

import aigis_ps

SOURCE = Path(aigis_ps.__file__).resolve().parent
TESTS = Path(__file__).resolve().parent
OWNERS = frozenset(("local_io", "native_numbers", "quest_modes"))
TREES = {path.stem: ast.parse(path.read_bytes()) for path in SOURCE.glob("*.py")}


def _defined(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else (node.target,):
                names.update(sub.id for sub in ast.walk(target) if isinstance(sub, ast.Name))
    return names


DEFINED = {module: _defined(tree) for module, tree in TREES.items()}


def _imported_module(node, test):
    if test:
        prefix = "aigis_ps."
        return node.module[len(prefix):] if node.level == 0 and node.module and node.module.startswith(prefix) else None
    return node.module if node.level == 1 else None


def _origin(module, name, seen=frozenset()):
    if name in DEFINED.get(module, ()) or module in seen:
        return module
    for node in TREES[module].body:
        if isinstance(node, ast.ImportFrom) and _imported_module(node, False) in TREES:
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return _origin(node.module, alias.name, seen | {module})
    return module


def _routes(tree, test):
    for node in tree.body:
        module = _imported_module(node, test) if isinstance(node, ast.ImportFrom) else None
        if module not in TREES or module in OWNERS:
            continue
        for alias in node.names:
            owner = _origin(module, alias.name)
            if owner in OWNERS and owner != module:
                yield module, alias.name, owner


@pytest.mark.parametrize("source, routed", [
    ("from aigis_ps.player_state import int32", [("player_state", "int32", "native_numbers")]),
    ("from aigis_ps.storage import Store, deserialize", [("storage", "deserialize", "local_io")]),
    ("from aigis_ps.quest_service import CLEAR_FLAG, QuestService", [("quest_service", "CLEAR_FLAG", "quest_modes")]),
    ("from aigis_ps.native_numbers import int32\nfrom aigis_ps.quest_modes import CLEAR_FLAG", []),
])
def test_the_owner_guard_follows_names_back_to_their_defining_module(source, routed):
    assert list(_routes(ast.parse(source), True)) == routed


def test_shared_primitives_are_imported_from_the_module_that_defines_them():
    found = [(path.name, *route) for path in SOURCE.glob("*.py") for route in _routes(TREES[path.stem], False)]
    for path in TESTS.glob("test_*.py"):
        # A module's own test may read the names that module imports for itself.
        found += [(path.name, *route) for route in _routes(ast.parse(path.read_bytes()), True)
                  if path.stem != f"test_{route[0]}"]
    assert found == []

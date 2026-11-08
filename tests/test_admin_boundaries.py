import ast
from collections.abc import Mapping
from functools import lru_cache
import inspect
from pathlib import Path
import sqlite3
import textwrap
from typing import get_args

import pytest

import aigis_ps
from aigis_ps.quest_modes import MODE_MODULES
import aigis_ps.storage as storage
from aigis_ps.storage import Store


SOURCE = Path(aigis_ps.__file__).resolve().parent
PACKAGE = aigis_ps.__name__
# quest_modes imports its mode modules by name at run time, so no static import leads the game to them.
IMPORTED_BY_NAME = {"quest_modes": frozenset(MODE_MODULES)}
ADMIN_PREFIX = "admin_"
KERNEL_MODULES = frozenset(("unit_removal", "unit_growth_edit", "wallet_money", "item_classes", "quest_record_admin",
                            "config_sets", "config_registry", "generation"))
KERNEL_PREFIXES = ("letstd_",)
CORE_MODULES = frozenset(("admin_command", "admin_commit", "admin_registry", "admin_diff"))
GAME_ENTRY = "cli"
FORBIDDEN_MODULES = ("sqlite3", "urllib", "http.client")
FORBIDDEN_FUNCTIONS = frozenset(("encode_wire", "prepare_request"))
ENVELOPE = "RequestEnvelope"
ALWAYS_FORBIDDEN = frozenset(("update_state", "connection"))
STORE_READS = frozenset(("profile", "recent_requests", "find_admin_operation", "list_admin_operations",
                         "list_admin_failures", "list_generations", "read_backup"))
# Listing backups prunes expired automatic copies first, so it is an allowed admin write rather than a read.
ADMIN_WRITES = frozenset(("commit_admin", "record_admin_failure", "backup", "list_backups", "prune_backups"))
RAW_CONNECTION = "connection"
DYNAMIC_IMPORTS = frozenset(("__import__", "import_module"))
DYNAMIC_CODE = frozenset(("exec", "eval"))
BUILTINS = "builtins"
REFLECTION = frozenset(("getattr", "setattr", "hasattr", "delattr", "methodcaller", "attrgetter", "__getattribute__",
                        "getattr_static"))
ADMIN_MODULES = f"{ADMIN_PREFIX}*.py"
OWN_RECEIVERS = frozenset(("self", "cls"))


def hands_out_connections(member) -> bool:
    annotation = inspect.signature(member, eval_str=True).return_annotation
    return sqlite3.Connection in (annotation, *get_args(annotation))


def connection_accessors() -> frozenset[str]:
    # Private members count too: any Store member typed to yield a connection gives raw SQL access.
    return frozenset(name for name, member in inspect.getmembers(Store, inspect.isfunction)
                     if not name.startswith("__") and hands_out_connections(member))


def store_writes() -> frozenset[str]:
    methods = {name for name, member in inspect.getmembers(Store) if callable(member) and not name.startswith("_")}
    raw_writers = {name for name, member in inspect.getmembers(storage, inspect.isfunction)
                   if member.__module__ == storage.__name__ and not name.startswith("_")
                   and next(iter(inspect.signature(member).parameters), None) == RAW_CONNECTION}
    return frozenset(methods - STORE_READS - ADMIN_WRITES) | raw_writers | ALWAYS_FORBIDDEN | connection_accessors()


def classified(name: str) -> bool:
    return name.startswith(ADMIN_PREFIX) or name in KERNEL_MODULES or name.startswith(KERNEL_PREFIXES)


def internal_imports(tree: ast.Module) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[1] for alias in node.names
                         if alias.name.split(".")[0] == PACKAGE and "." in alias.name)
        elif isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".") if node.module else []
            if node.level == 0:
                if not parts or parts[0] != PACKAGE:
                    continue
                parts = parts[1:]
            elif node.level > 1:
                continue
            found.update(parts[:1] if parts else (alias.name for alias in node.names))
    return found


@lru_cache(maxsize=1)
def project_graph() -> dict[str, frozenset[str]]:
    sources = {path.stem: path for path in SOURCE.glob("*.py") if path.stem != "__init__"}
    return {name: frozenset((internal_imports(ast.parse(path.read_bytes(), str(path)))
                             | IMPORTED_BY_NAME.get(name, frozenset())) & sources.keys())
            for name, path in sources.items()}


def game_modules(graph: Mapping[str, frozenset[str]]) -> set[str]:
    reached, pending = set(), [GAME_ENTRY] if GAME_ENTRY in graph else []
    while pending:
        name = pending.pop()
        if name in reached or classified(name):
            continue
        reached.add(name)
        pending.extend(graph.get(name, ()))
    return reached


def scanned_modules(graph: Mapping[str, frozenset[str]]) -> set[str]:
    # Modules the game server reaches without admin code are established; anything else admin code imports is scanned.
    game = game_modules(graph)
    found, pending = set(), sorted(name for name in graph if classified(name))
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        pending.extend(child for child in graph.get(name, ()) if classified(child) or child not in game)
    return found


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{dotted(node.func)}()"
    return ""


def forbidden_module(name: str) -> bool:
    return any(name == module or name.startswith(module + ".") for module in FORBIDDEN_MODULES)


def named(node: ast.AST, names: frozenset[str]) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names


def violations(source: str, banned_attributes: frozenset[str]) -> list[str]:
    banned_names = banned_attributes | FORBIDDEN_FUNCTIONS | {ENVELOPE}
    found = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            found.extend(f"{line}: imports {alias.name}" for alias in node.names if forbidden_module(alias.name))
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and any(
                    forbidden_module(name) for name in (node.module, *(f"{node.module}.{alias.name}" for alias in node.names))):
                found.append(f"{line}: imports from {node.module}")
            found.extend(f"{line}: imports {alias.name}" for alias in node.names
                         if alias.name in FORBIDDEN_FUNCTIONS | banned_attributes)
        if isinstance(node, ast.Call):
            target = dotted(node.func)
            name = target.rsplit(".", 1)[-1]
            if name == ENVELOPE:
                found.append(f"{line}: constructs {ENVELOPE}")
            if name == "decode" and "registry" in target.lower():
                found.append(f"{line}: decodes through {target}")
            if (name in DYNAMIC_IMPORTS and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str) and forbidden_module(node.args[0].value)):
                found.append(f"{line}: imports {node.args[0].value} dynamically")
            if name in DYNAMIC_CODE and target in (name, f"{BUILTINS}.{name}"):
                found.append(f"{line}: runs code from text through {target}")
            if name in REFLECTION and any(named(argument, banned_names) for argument in node.args):
                found.append(f"{line}: reaches a forbidden name through {target}")
        if isinstance(node, ast.Subscript) and named(node.slice, banned_names):
            found.append(f"{line}: looks up {node.slice.value!r} by key")
        if isinstance(node, ast.Attribute) and node.attr in banned_attributes | FORBIDDEN_FUNCTIONS:
            found.append(f"{line}: uses .{node.attr}")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_FUNCTIONS:
            found.append(f"{line}: uses {node.id}")
    return found


def private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def private_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and private(node.value)


def private_members(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        if (isinstance(node, ast.Attribute) and private(node.attr)
                and not (isinstance(node.value, ast.Name) and node.value.id in OWN_RECEIVERS)):
            found.append(f"{line}: uses .{node.attr}")
        if isinstance(node, ast.ImportFrom):
            found.extend(f"{line}: imports {alias.name}" for alias in node.names if private(alias.name))
        if (isinstance(node, ast.Call) and dotted(node.func).rsplit(".", 1)[-1] in REFLECTION
                and any(private_literal(argument) for argument in node.args)):
            found.append(f"{line}: reaches a private member through {dotted(node.func)}")
    return found


def test_scan_starts_from_every_admin_module_and_existing_kernel():
    graph = project_graph()
    expected = {name for name in graph if classified(name)}
    scanned = scanned_modules(graph)
    assert scanned and CORE_MODULES <= expected <= scanned


def test_scanned_modules_stay_inside_the_admin_boundary():
    banned = store_writes()
    problems = [f"{name}.py:{issue}" for name in sorted(scanned_modules(project_graph()))
                for issue in violations((SOURCE / f"{name}.py").read_text(encoding="utf-8"), banned)]
    assert problems == []


def test_admin_modules_call_no_private_service_members():
    paths = sorted(SOURCE.glob(ADMIN_MODULES))
    problems = [f"{path.name}:{issue}" for path in paths
                for issue in private_members(path.read_text(encoding="utf-8"))]
    assert CORE_MODULES <= {path.stem for path in paths} and problems == []


def test_mode_modules_imported_by_name_belong_to_the_game():
    graph = project_graph()
    modes = set(MODE_MODULES)
    assert modes <= graph["quest_modes"] and modes <= game_modules(graph)
    assert not modes & scanned_modules(graph)


def test_import_graph_follows_helpers_only_admin_code_reaches():
    graph = {"cli": frozenset({"app", "presents", "admin_app"}), "app": frozenset({"storage"}), "storage": frozenset(),
             "presents": frozenset({"wallet_money", "storage"}), "wallet_money": frozenset(),
             "admin_app": frozenset({"admin_helpers_shared", "console_helper"}), "admin_helpers_shared": frozenset(),
             "console_helper": frozenset({"console_deep", "storage"}), "console_deep": frozenset(),
             "admin_cmd_wallet": frozenset({"presents", "wallet_money", "letstd_rules"}), "letstd_rules": frozenset(),
             "letstd_unused": frozenset()}
    assert scanned_modules(graph) == {"admin_app", "admin_helpers_shared", "console_helper", "console_deep",
                                      "admin_cmd_wallet", "wallet_money", "letstd_rules", "letstd_unused"}


def test_store_write_methods_come_from_the_store_itself():
    writes = store_writes()
    assert {"update_state", "record_request", "connection"} <= writes
    assert {name for name in ("ensure_admin_tables", "insert_admin_operation", "insert_backup") if hasattr(storage, name)} <= writes
    accessors = connection_accessors()
    assert "connection" in accessors and accessors <= writes
    assert {name for name in ("_admin_transaction",) if hasattr(Store, name)} <= accessors
    assert not writes & (ADMIN_WRITES | STORE_READS)


@pytest.mark.parametrize("sample", [
    """
    def read(store):
        with store.connection() as connection:
            return connection
    """,
    """
    def write(store, state):
        getattr(store, "update_state")(state, 0)
    """,
    "import sqlite3\n",
    "import urllib.parse\n",
    "from urllib.request import urlopen\n",
    "import http.client\n",
    "from http import client\n",
    """
    def write(store, state):
        store.update_state(state, 0)
    """,
    """
    def log(store):
        store.record_request(None, "route", 0, 0, 200)
    """,
    """
    def fake(registry, route, wire):
        return registry.decode(route, wire)
    """,
    """
    def fake(self, route, wire):
        return self.registry.decode(route, wire)
    """,
    "from .protocol import encode_wire\n",
    """
    from . import protocol

    def fake(xml, key):
        return protocol.prepare_request(xml, key)
    """,
    """
    from .protocol import RequestEnvelope

    def fake(profile, xml, document):
        return RequestEnvelope(profile, xml, document, "0" * 64)
    """,
    """
    import importlib

    database = importlib.import_module("sqlite3")
    """,
    "network = __import__('urllib.request')\n",
    """
    import operator

    writer = operator.methodcaller("update_state", {}, 0)
    """,
    """
    def write(store, state):
        vars(type(store))["update_state"](store, state, 0)
    """,
    """
    def opened(store):
        return hasattr(store, "connection")
    """,
    """
    def write(store):
        with store._admin_transaction() as handle:
            handle.execute("DELETE FROM profiles")
    """,
    """
    def write(store, state):
        object.__getattribute__(store, "update_state")(state, 0)
    """,
    """
    import inspect

    def opened(store):
        return inspect.getattr_static(store, "connection")
    """,
    "exec('import sqlite3')\n",
    """
    import builtins

    writer = builtins.eval("store.update_" + "state")
    """,
])
def test_boundary_scanner_rejects_forbidden_patterns(sample):
    assert violations(textwrap.dedent(sample), store_writes())


def test_boundary_scanner_rejects_raw_connection_writers():
    banned = store_writes()
    store_members = {name for name, _ in inspect.getmembers(Store)}
    for name in sorted(banned - store_members):
        assert violations(f"from .storage import {name}\n", banned)
        assert violations(f"from . import storage\n\nstorage.{name}(None)\n", banned)


def test_boundary_scanner_allows_the_admin_store_interface():
    sample = textwrap.dedent("""
        import ast
        from http import HTTPStatus
        import importlib

        def commit(store, data, name, headers, text):
            store.commit_admin("op", "digest", 0, None, {})
            store.record_admin_failure({})
            store.backup()
            module = importlib.import_module(name)
            keep_alive = headers.get("connection")
            literal = ast.literal_eval(text)
            return data.decode("utf-8"), store.profile(), HTTPStatus.CONFLICT, module, keep_alive, literal
    """)
    assert violations(sample, store_writes()) == []


@pytest.mark.parametrize("sample", [
    "rewards._validated(state)\n",
    "self.services.missions._preflight_snapshot(state, now)\n",
    "service(bundle, 'votes')._state(state)\n",
    "PresentService._history(state, 0)\n",
    "from .event_pass_service import _identity\n",
    "getattr(players, '_quest_reservations')(state)\n",
])
def test_private_member_scanner_rejects_reaching_into_services(sample):
    assert private_members(sample)


def test_private_member_scanner_allows_own_members_and_dunders():
    sample = textwrap.dedent("""
        from __future__ import annotations

        class Reader:
            def read(self, value):
                return self._cache, type(value).__name__, value.__dict__, getattr(value, "content_version", None)

            @classmethod
            def build(cls):
                return cls._registry
    """)
    assert private_members(sample) == []

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
import importlib
import pkgutil
import re
from types import MappingProxyType, ModuleType
from typing import Any

from pydantic import BaseModel

from .admin_command import DOMAIN_NAME, MAX_BATCH, AdminCommand, AdminError, Batch, BatchStep, validate_params_model


COMMAND_PREFIX = "admin_cmd_"
VIEW_PREFIX = "admin_views_"
ROUTE_PREFIX = "admin_routes_"
COMMAND_FIELDS = frozenset(("operation", "params"))
REGISTRY_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z")


def _problem(step: int, location: tuple, kind: str, message: str) -> dict:
    return {"step": step, "loc": list(location), "type": kind, "msg": message}


@dataclass(frozen=True)
class AdminView:
    name: str
    domain: str
    read: Callable[..., Any]
    params_model: type[BaseModel] | None = None
    live: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not REGISTRY_NAME.fullmatch(self.name):
            raise ValueError("Admin views use lowercase dotted names")
        if not isinstance(self.domain, str) or not DOMAIN_NAME.fullmatch(self.domain) or not callable(self.read):
            raise ValueError(f"Admin view {self.name} needs a domain and a read callable")
        if self.params_model is not None:
            validate_params_model(self.params_model)
        if type(self.live) is not bool:
            raise ValueError(f"Admin view {self.name} marks live answers with a bool")

    def describe(self) -> dict:
        return {"name": self.name, "domain": self.domain, "live": self.live,
                "params_schema": None if self.params_model is None else self.params_model.model_json_schema()}


@dataclass(frozen=True)
class AdminRoute:
    name: str
    build: Callable[..., Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not REGISTRY_NAME.fullmatch(self.name) or not callable(self.build):
            raise ValueError("Admin routes need a lowercase dotted name and a build callable")


def shares_writes(owner: AdminCommand, command: AdminCommand) -> bool:
    return command.group is not None and owner.group == command.group


class CommandRegistry:
    def __init__(self, preflight_domains: Mapping[str, Any]):
        covered = frozenset(preflight_domains)
        if any(not isinstance(name, str) or not DOMAIN_NAME.fullmatch(name) for name in covered):
            raise ValueError("Preflight domain tables are keyed by domain names")
        self._covered = covered
        self._commands: dict[str, AdminCommand] = {}
        self._owners: dict[str, AdminCommand] = {}

    def add(self, command: AdminCommand) -> None:
        if not isinstance(command, AdminCommand):
            raise ValueError("Only admin commands can be registered")
        operation = command.operation
        if operation in self._commands:
            raise ValueError(f"Duplicate admin command: {operation}")
        # The preflight table already decides every domain it maps, so a command's exemption there would only mislead.
        mapped = sorted(command.preflight_none.keys() & self._covered)
        if mapped:
            raise ValueError(f"{operation} exempts domains that have a preflight: {', '.join(mapped)}")
        uncovered = sorted(command.touched - self._covered - set(command.preflight_none))
        if uncovered:
            raise ValueError(f"{operation} touches domains without preflight or an explicit none reason: {', '.join(uncovered)}")
        owned = sorted(name for name in command.writes
                       if name in self._owners and not shares_writes(self._owners[name], command))
        if owned:
            raise ValueError(f"{operation} writes fields owned by other commands: "
                             + ", ".join(f"{name} ({self._owners[name].operation})" for name in owned))
        self._commands[operation] = command
        for name in command.writes:
            self._owners.setdefault(name, command)

    def get(self, operation: object) -> AdminCommand:
        command = self._commands.get(operation) if isinstance(operation, str) else None
        if command is None:
            raise AdminError("admin_command_unknown", "Unknown admin command", HTTPStatus.NOT_FOUND)
        return command

    def __contains__(self, operation: object) -> bool:
        return operation in self._commands

    def __iter__(self) -> Iterator[AdminCommand]:
        return iter([self._commands[name] for name in sorted(self._commands)])

    def __len__(self) -> int:
        return len(self._commands)

    def field_owners(self) -> Mapping[str, str]:
        return MappingProxyType({name: self._owners[name].operation for name in sorted(self._owners)})

    def describe(self) -> list[dict]:
        return [command.describe() for command in self]

    def batch(self, raw: object) -> Batch:
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or not 1 <= len(raw) <= MAX_BATCH:
            raise AdminError("admin_batch_invalid", f"A batch needs between 1 and {MAX_BATCH} commands",
                             HTTPStatus.UNPROCESSABLE_ENTITY)
        steps, problems = [], []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping) or set(item) != COMMAND_FIELDS or not isinstance(item["operation"], str):
                problems.append(_problem(index, (), "command_invalid", "Each command has exactly operation and params"))
                continue
            command = self._commands.get(item["operation"])
            if command is None:
                problems.append(_problem(index, ("operation",), "command_unknown", "Unknown admin command"))
                continue
            try:
                steps.append(BatchStep(command, command.parse(item["params"])))
            except AdminError as error:
                details = error.fields or ({"loc": [], "type": error.code, "msg": str(error)},)
                problems.extend(_problem(index, ("params", *detail["loc"]), detail["type"], detail["msg"]) for detail in details)
        if problems:
            raise AdminError("admin_params_invalid", "The batch contains invalid commands",
                             HTTPStatus.UNPROCESSABLE_ENTITY, fields=problems)
        return Batch(tuple(steps))


class ViewRegistry:
    def __init__(self):
        self._views: dict[str, AdminView] = {}

    def add(self, view: AdminView) -> None:
        if not isinstance(view, AdminView):
            raise ValueError("Only admin views can be registered")
        if view.name in self._views:
            raise ValueError(f"Duplicate admin view: {view.name}")
        self._views[view.name] = view

    def get(self, name: object) -> AdminView:
        view = self._views.get(name) if isinstance(name, str) else None
        if view is None:
            raise AdminError("admin_view_unknown", "Unknown admin view", HTTPStatus.NOT_FOUND)
        return view

    def __contains__(self, name: object) -> bool:
        return name in self._views

    def __iter__(self) -> Iterator[AdminView]:
        return iter([self._views[name] for name in sorted(self._views)])

    def __len__(self) -> int:
        return len(self._views)

    def describe(self) -> list[dict]:
        return [view.describe() for view in self]


class RouteRegistry:
    def __init__(self):
        self._routes: dict[str, AdminRoute] = {}

    def add(self, route: AdminRoute) -> None:
        if not isinstance(route, AdminRoute):
            raise ValueError("Only admin routes can be registered")
        if route.name in self._routes:
            raise ValueError(f"Duplicate admin route: {route.name}")
        self._routes[route.name] = route

    def __contains__(self, name: object) -> bool:
        return name in self._routes

    def __iter__(self) -> Iterator[AdminRoute]:
        return iter([self._routes[name] for name in sorted(self._routes)])

    def __len__(self) -> int:
        return len(self._routes)


@dataclass(frozen=True)
class AdminCatalog:
    commands: CommandRegistry
    views: ViewRegistry
    routes: RouteRegistry
    modules: tuple[str, ...]


def discover(preflight_domains: Mapping[str, Any], *, package: ModuleType | None = None) -> AdminCatalog:
    package = importlib.import_module(__package__) if package is None else package
    commands, views, routes = CommandRegistry(preflight_domains), ViewRegistry(), RouteRegistry()
    targets = ((COMMAND_PREFIX, commands), (VIEW_PREFIX, views), (ROUTE_PREFIX, routes))
    loaded = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda module: module.name):
        registry = next((target for prefix, target in targets if info.name.startswith(prefix)), None)
        if registry is None:
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        register = getattr(module, "register", None)
        if not callable(register):
            raise ValueError(f"Admin module {info.name} must export register(registry)")
        register(registry)
        loaded.append(info.name)
    return AdminCatalog(commands, views, routes, tuple(loaded))

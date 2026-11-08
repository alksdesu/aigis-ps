from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path
import threading
from typing import Any

from pydantic import Field

from .admin_app import admin_page
from .admin_command import CommandParams
from .admin_config_switch import EMPTY_SET, PointerState, latest_switch, master_state, staged_set
from .admin_diff import pointer
from .admin_fields import field_dictionary, ledger_status
from .admin_registry import ViewRegistry
from .admin_views import (MAX_PAGE_ITEMS, MAX_POINTER_CHARS, CursorParams, PageParams, ViewContext, page,
                          parse_pointer, redact_at, redacted_view, resolve_pointer, secret_path, service)
from .app import compact_readiness
from .config_registry import ConfigError
from .config_sets import SET_ID_LENGTH, ConfigSets, config_identity
from .generation import GenerationIdentity
from .local_io import deserialize, read_bounded
from .masterdata import MasterDataStore
from .protocol import ProtocolRegistry
from .storage import ADMIN_PAGE_SIZE, MAX_ADMIN_PAGE_SIZE


RUNTIME, STATE, HISTORY = "runtime", "state", "history"
CLIENT_RUNTIME = "client"
CLIENT_MANIFEST = "client-manifest.json"
# The client preparation tool refuses manifests above this size.
MAX_CLIENT_MANIFEST_BYTES = 64 * 1024 * 1024
CLIENT_OVERLAYS = ("event_pass_revival", "event_login_revival", "purchased_login", "mission_windows", "command_texts",
                   "shop_window_closure")
JSON_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"), (dict, "object"),
              (list, "array"))


class RequestsParams(CommandParams):
    limit: int = Field(default=ADMIN_PAGE_SIZE, ge=1, le=MAX_ADMIN_PAGE_SIZE)


class BrowseParams(PageParams):
    pointer: str = Field(default="", max_length=MAX_POINTER_CHARS)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def versions_match(left: str | None, right: str | None) -> bool | None:
    return None if left is None or right is None else left == right


def generation_view(context: ViewContext, params: None) -> dict:
    leased, services = context.generation.generation, context.services
    return {"id": leased.id, "identity": leased.identity.to_dict(), "gate_issues": list(leased.gate_issues),
            "runtime": services.runtime.status(), "client": services.gate.status()}


def identified_set(digest: str, sets: ConfigSets) -> str | None:
    # Generations carry the resolved configuration digest, which names a stored set only without CLI overrides.
    if digest == config_identity({}):
        return EMPTY_SET
    try:
        config_set = sets.load(digest[:SET_ID_LENGTH])
    except ConfigError:
        return None
    return config_set.identifier if config_identity(config_set.explicit) == digest else None


def _unreadable_config(digest: str, running: str | None, error: ConfigError) -> dict:
    # Without a readable pointer the next start is unknown, so nothing can be reported as staged.
    return {"running": {"digest": digest, "set": running}, "active": PointerState.unreadable(error),
            "staged": {"known": False, "set": None}}


def config_state(data_dir: Path, digest: str, latest: Mapping[str, Any] | None) -> dict:
    try:
        sets = ConfigSets(data_dir)
    except ConfigError as error:
        return _unreadable_config(digest, None, error)
    running = identified_set(digest, sets)
    try:
        pointer = PointerState.read(sets)
    except ConfigError as error:
        return _unreadable_config(digest, running, error)
    return {"running": {"digest": digest, "set": running}, "active": pointer.describe(latest),
            "staged": {"known": running is not None,
                       "set": None if running is None else staged_set(pointer, latest, running)}}


def config_view(context: ViewContext, params: None) -> dict:
    identity, services = context.generation.generation.identity, context.services
    masters = MasterDataStore.for_data_dir(services.data_dir)
    return {**config_state(services.data_dir, identity.config_set, latest_switch(services.store)),
            "masterdata": master_state(services.store, masters, identity.master_snapshot).describe()}


def readiness_view(context: ViewContext, params: None) -> dict:
    return {"domains": compact_readiness(context.generation.generation.live_status())}


def unimplemented_operations(registry: ProtocolRegistry, handlers: Mapping, readiness: Mapping) -> list:
    missing = [number for number, contract in registry.by_registration.items()
               if not any(key in handlers for key in contract.registration_keys) and contract.name not in handlers]
    bootstrap = readiness.get("bootstrap")
    if isinstance(bootstrap, Mapping):
        missing.extend(bootstrap.get("unimplemented_operations", ()))
    return sorted(set(missing), key=str)


def operations_view(context: ViewContext, params: None) -> dict:
    leased, registry = context.generation.generation, ProtocolRegistry.load()
    return {"unimplemented": unimplemented_operations(registry, leased.handlers, leased.readiness),
            "configured_handlers": sorted(map(str, leased.handlers)),
            "registered_operations": len(registry.by_registration)}


def requests_view(context: ViewContext, params: RequestsParams) -> dict:
    return {"requests": context.services.store.recent_requests(params.limit)}


def _overlay(name: str, record: Any) -> tuple[str, bool, str | None]:
    rules_hash = _text(record.get("rules_hash")) if isinstance(record, dict) else None
    return name, record is not None, rules_hash


@dataclass(frozen=True)
class ManifestFacts:
    schema_version: int | None
    prepared_at: str | None
    client: str | None
    resources: str | None
    overlays: tuple[tuple[str, bool, str | None], ...]

    @classmethod
    def parse(cls, data: bytes) -> ManifestFacts:
        manifest = deserialize(data)
        if not isinstance(manifest, dict):
            raise ValueError("Client manifests are JSON objects")
        version, snapshot = manifest.get("schema_version"), manifest.get("resource_snapshot")
        return cls(schema_version=version if type(version) is int else None,
                   prepared_at=_text(manifest.get("prepared_at")), client=_text(manifest.get("source_sha256")),
                   resources=_text(snapshot.get("version")) if isinstance(snapshot, dict) else None,
                   overlays=tuple(_overlay(name, manifest.get(name)) for name in CLIENT_OVERLAYS))


class ManifestCache:
    # A prepared manifest lists every client file and parses into tens of MiB, so only changed content is parsed again.
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: tuple[str, ManifestFacts] | None = None

    def read(self, location: Path, root: Path) -> ManifestFacts | None:
        if not location.is_file():
            return None
        data = read_bounded(location, MAX_CLIENT_MANIFEST_BYTES, root=root)
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            last = self._last
        if last is None or last[0] != digest:
            last = digest, ManifestFacts.parse(data)
            with self._lock:
                self._last = last
        return last[1]


CLIENT_MANIFESTS = ManifestCache()


def client_manifest_versions(data_dir: Path, identity: GenerationIdentity, bundle: Any) -> dict:
    # prepare-client keeps the client runtime beside the default server data directory.
    root = Path(data_dir).parent
    location = root / CLIENT_RUNTIME / CLIENT_MANIFEST
    try:
        facts = CLIENT_MANIFESTS.read(location, root)
    except (OSError, ValueError, RecursionError):
        return {"path": str(location), "present": True, "valid": False}
    if facts is None:
        return {"path": str(location), "present": False, "valid": None}
    native = getattr(bundle, "native_cdn", None)
    served = None if native is None else native.status().get("version")
    return {"path": str(location), "present": True, "valid": True, "schema_version": facts.schema_version,
            "prepared_at": facts.prepared_at,
            "client": {"prepared": facts.client, "generation": identity.client_fingerprint,
                       "matches": versions_match(facts.client, identity.client_fingerprint)},
            "resources": {"prepared": facts.resources, "served": served,
                          "generation": identity.effective_resource_version,
                          "matches": versions_match(facts.resources, served)},
            "overlays": {name: {"present": present, "rules_hash": rules_hash}
                         for name, present, rules_hash in facts.overlays}}


def masterdata_versions(identity: GenerationIdentity, bundle: Any) -> dict:
    masters = getattr(bundle, "masters", None)
    disk = None if masters is None else masters.status()
    active = _text(disk.get("snapshot")) if isinstance(disk, dict) and disk.get("ready") is True else None
    return {"in_use": identity.master_snapshot, "disk": disk,
            "matches": versions_match(identity.master_snapshot, active)}


def versions_view(context: ViewContext, params: None) -> dict:
    leased = context.generation.generation
    return {"client_manifest": client_manifest_versions(context.services.data_dir, leased.identity, leased.bundle),
            "masterdata": masterdata_versions(leased.identity, leased.bundle)}


def json_type(value: Any) -> str:
    return "null" if value is None else next(name for kind, name in JSON_TYPES if isinstance(value, kind))


def _inline(container: dict | list) -> bool:
    members = container.values() if isinstance(container, dict) else container
    return len(container) <= MAX_PAGE_ITEMS and not any(isinstance(member, (dict, list)) for member in members)


def _element(path: tuple[str, ...], value: Any) -> dict:
    # Nested or long containers are summarized so one page stays bounded; browsing into them pages their members.
    if isinstance(value, (dict, list)) and not _inline(value) and not secret_path(path):
        return {"type": json_type(value), "size": len(value)}
    shown = redact_at(path, value)
    return {"type": json_type(shown), "value": shown}


def browse(state: dict, path: tuple[str, ...], params: PageParams) -> dict:
    node = resolve_pointer(state, path)
    location = {"pointer": pointer(path), "parent": pointer(path[:-1]) if path else None}
    if secret_path(path) or not isinstance(node, (dict, list)):
        return {**location, **_element(path, node)}
    if isinstance(node, dict):
        listing = page(list(node.items()), params, lambda item: {"key": item[0], **_element((*path, item[0]), item[1])})
    else:
        listing = page(list(enumerate(node)), params,
                       lambda item: {"index": item[0], **_element((*path, str(item[0])), item[1])})
    return {**location, "type": json_type(node), **listing}


def browse_view(context: ViewContext, params: BrowseParams) -> dict:
    path = parse_pointer(params.pointer)
    result = browse(context.state, path, params)
    if path:
        [result["ledger"]] = ledger_status(context.state, context.generation.bundle, keys=path[:1])
    else:
        result["ledgers"] = ledger_status(context.state, context.generation.bundle)
    return result


def fields_view(context: ViewContext, params: None) -> dict:
    return field_dictionary(service(context.generation.bundle, "bootstrap"),
                            context.services.catalog.commands.field_owners())


def audit_view(context: ViewContext, params: CursorParams) -> dict:
    return admin_page(context.services.store.list_admin_operations(params.cursor, params.limit))


def failures_view(context: ViewContext, params: CursorParams) -> dict:
    return admin_page(context.services.store.list_admin_failures(params.cursor, params.limit))


def generations_view(context: ViewContext, params: CursorParams) -> dict:
    return admin_page(context.services.store.list_generations(params.cursor, params.limit))


VIEWS = (
    # Runtime status, files on disk and the history tables change without a new save revision.
    redacted_view("runtime.generation", RUNTIME, generation_view, live=True),
    redacted_view("runtime.config", RUNTIME, config_view, live=True),
    redacted_view("runtime.readiness", RUNTIME, readiness_view, live=True),
    redacted_view("runtime.operations", RUNTIME, operations_view),
    redacted_view("runtime.requests", RUNTIME, requests_view, RequestsParams, live=True),
    redacted_view("runtime.versions", RUNTIME, versions_view, live=True),
    redacted_view("state.browse", STATE, browse_view, BrowseParams),
    redacted_view("state.fields", STATE, fields_view),
    redacted_view("history.audit", HISTORY, audit_view, CursorParams, live=True),
    redacted_view("history.failures", HISTORY, failures_view, CursorParams, live=True),
    redacted_view("history.generations", HISTORY, generations_view, CursorParams, live=True),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)

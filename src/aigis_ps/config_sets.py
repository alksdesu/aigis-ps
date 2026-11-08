from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
from pathlib import Path
import re
from types import MappingProxyType
import uuid

from .config_registry import (REGISTRY, ConfigError, ConfigKey, ConfigRegistry, StagedDocument, checked_config_path,
                              document_name, parse_document)
from .local_io import atomic_replace, exclusive_file, read_bounded
from .masterdata import json_bytes
from .storage import serialize, timestamp


CONFIG_DIRECTORY = "config"
SETS_DIRECTORY = "sets"
STAGING_DIRECTORY = "staging"
ACTIVE_POINTER = "active.json"
MANIFEST_NAME = "manifest.json"
LOCK_NAME = "operation.lock"
SET_SCHEMA = 1
SET_ID_LENGTH = 24
MAX_MANIFEST_BYTES = 64 * 1024
MAX_POINTER_BYTES = 4096
MAX_TEXT_FIELD = 128
# Switch sequences are mirrored in SQLite INTEGER columns.
MAX_SEQUENCE = 2 ** 63 - 1
MANIFEST_FIELDS = frozenset(("schema_version", "set", "keys", "source", "parent", "created_at"))
EXPLICIT_FIELDS = frozenset(("mode", "sha256", "content_version"))
POINTER_FIELDS = frozenset(("schema_version", "set", "manifest_sha256", "sequence"))
_SET_ID = re.compile(rf"[a-f0-9]{{{SET_ID_LENGTH}}}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_SOURCE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")


class KeyMode(StrEnum):
    DEFAULT = "default"
    EXPLICIT = "explicit"


class ConfigSource(StrEnum):
    DEFAULT = "default"
    SET = "set"
    CLI = "cli"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def config_identity(explicit: Mapping[str, str]) -> str:
    return _digest(serialize({"schema_version": SET_SCHEMA, "keys": dict(explicit)}).encode("utf-8"))


def _sequence(value) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SEQUENCE:
        raise ConfigError("config_sequence_invalid", "Switch sequences are nonnegative 64-bit integers")
    return value


def _bounded_text(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT_FIELD


@dataclass(frozen=True)
class ConfigSet:
    identifier: str
    directory: Path
    explicit: Mapping[str, str]
    content_versions: Mapping[str, str]
    source: str
    parent: str | None
    created_at: str
    manifest_sha256: str

    def path(self, key: str) -> Path:
        if key not in self.explicit:
            raise KeyError(key)
        return self.directory / document_name(key)

    def describe(self) -> dict:
        return {"id": self.identifier, "source": self.source, "parent": self.parent, "created_at": self.created_at,
                "manifest_sha256": self.manifest_sha256, "explicit": list(self.explicit),
                "content_versions": dict(self.content_versions)}


@dataclass(frozen=True)
class ActivePointer:
    set_id: str
    sequence: int
    manifest_sha256: str
    digest: str

    def describe(self) -> dict:
        return {"set": self.set_id, "sequence": self.sequence, "manifest_sha256": self.manifest_sha256,
                "digest": self.digest}


def _pointer_bytes(config_set: ConfigSet, sequence: int) -> bytes:
    return json_bytes({"schema_version": SET_SCHEMA, "set": config_set.identifier,
                       "manifest_sha256": config_set.manifest_sha256, "sequence": sequence})


def _parse_pointer(data: bytes) -> ActivePointer:
    value = parse_document(data, limit=MAX_POINTER_BYTES)
    if (set(value) != POINTER_FIELDS or type(value["schema_version"]) is not int
            or value["schema_version"] != SET_SCHEMA or not isinstance(value["set"], str)
            or not _SET_ID.fullmatch(value["set"]) or not isinstance(value["manifest_sha256"], str)
            or not _SHA256.fullmatch(value["manifest_sha256"])):
        raise ValueError("The active pointer does not name a configuration set")
    return ActivePointer(value["set"], _sequence(value["sequence"]), value["manifest_sha256"], _digest(data))


@dataclass(frozen=True)
class ResolvedEntry:
    spec: ConfigKey
    source: ConfigSource
    path: Path | None
    sha256: str | None
    shadowed: bool = False

    @property
    def mode(self) -> KeyMode:
        return KeyMode.DEFAULT if self.path is None else KeyMode.EXPLICIT

    @property
    def read_only(self) -> bool:
        return self.source is ConfigSource.CLI

    def load(self):
        return self.spec.load(self.path)

    def describe(self) -> dict:
        result = {**self.spec.describe(), "source": self.source, "mode": self.mode,
                  "path": None if self.path is None else str(self.path), "sha256": self.sha256,
                  "shadowed": self.shadowed, "read_only": self.read_only, "effective": None,
                  "content_version": None, "error": None}
        try:
            rules = self.load()
            result.update(effective=self.spec.normalize(rules), content_version=self.spec.content_version(rules))
        except ConfigError as error:
            result["error"] = {"code": error.code, "message": str(error)}
        result["default"] = self.spec.normalize(self.spec.default())
        return result


@dataclass(frozen=True)
class ResolvedConfig:
    pointer: ActivePointer | None
    config_set: ConfigSet | None
    entries: Mapping[str, ResolvedEntry]

    @property
    def overridden(self) -> tuple[str, ...]:
        return tuple(key for key, entry in self.entries.items() if entry.source is ConfigSource.CLI)

    @property
    def digest(self) -> str:
        return config_identity({key: entry.sha256 for key, entry in self.entries.items() if entry.path is not None})

    def arguments(self) -> dict[str, Path | None]:
        return {entry.spec.dest: entry.path for entry in self.entries.values()}

    def load_rules(self) -> dict[str, object]:
        return {key: entry.load() for key, entry in self.entries.items()}

    def effective(self) -> dict[str, dict | None]:
        return {key: self.entries[key].spec.normalize(rules) for key, rules in self.load_rules().items()}

    def describe(self) -> dict:
        return {"schema_version": SET_SCHEMA,
                "active": None if self.pointer is None else self.pointer.describe(),
                "set": None if self.config_set is None else self.config_set.describe(),
                "digest": self.digest, "overridden": list(self.overridden),
                "keys": [entry.describe() for entry in self.entries.values()]}


class PendingActivation:
    def __init__(self, owner: ConfigSets, path: Path, data: bytes, expected: str | None):
        self._owner, self.path, self.expected = owner, path, expected
        self.pointer = _parse_pointer(data)
        self.published = False

    def publish(self) -> ActivePointer:
        if self.published:
            return self.pointer
        owner = self._owner
        with owner._locked():
            owner._check_pointer(self.expected, self.pointer.sequence)
            if owner.load(self.pointer.set_id).manifest_sha256 != self.pointer.manifest_sha256:
                raise ConfigError("config_set_invalid", "The staged pointer no longer matches its configuration set", 503)
            atomic_replace(owner._path(self.path), owner._path(owner.active))
        self.published = True
        return self.pointer

    def discard(self) -> None:
        if not self.published:
            self._owner._path(self.path).unlink(missing_ok=True)

    def __enter__(self) -> PendingActivation:
        return self

    def __exit__(self, *exc_info) -> None:
        self.discard()


class ConfigSets:
    def __init__(self, data_dir: Path, *, registry: ConfigRegistry = REGISTRY):
        self.data_dir = checked_config_path(Path(data_dir))
        self.registry = registry
        self.root = self._path(self.data_dir / CONFIG_DIRECTORY)
        self.sets = self._path(self.root / SETS_DIRECTORY)
        self.staging = self._path(self.root / STAGING_DIRECTORY)
        self.active = self._path(self.root / ACTIVE_POINTER)

    def _path(self, path: Path) -> Path:
        return checked_config_path(path, self.data_dir)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock = self._path(self.root / LOCK_NAME)
        with ExitStack() as stack:
            try:
                stack.enter_context(exclusive_file(lock))
            except ValueError as error:
                raise ConfigError("config_pointer_busy", "Another configuration switch is in progress", 409) from error
            yield

    def create(self, changes: Mapping[str, bytes | Mapping | None], *, source: str, base: str | None = None) -> ConfigSet:
        if not isinstance(changes, Mapping):
            raise ConfigError("config_changes_invalid", "Configuration changes map keys to documents or None")
        if not isinstance(source, str) or not _SOURCE.fullmatch(source):
            raise ConfigError("config_source_invalid", "Configuration set sources are short lowercase identifiers")
        for key in changes:
            self.registry.require(key)
        parent = None if base is None else self.load(base)
        documents = {} if parent is None else {key: self.read_document(parent, key) for key in parent.explicit}
        for key, document in changes.items():
            if document is None:
                documents.pop(key, None)
            else:
                documents[key] = document
        self._path(self.staging).mkdir(parents=True, exist_ok=True)
        staging = self._path(self.staging / uuid.uuid4().hex)
        staging.mkdir()
        written = []
        try:
            staged = {}
            for key in sorted(documents):
                staged[key] = self.registry[key].stage(documents[key], staging, root=self.data_dir)
                written.append(staged[key].path)
            explicit = {key: item.sha256 for key, item in staged.items()}
            identifier = config_identity(explicit)[:SET_ID_LENGTH]
            manifest = self._path(staging / MANIFEST_NAME)
            with manifest.open("xb") as stream:
                written.append(manifest)
                stream.write(json_bytes(self._manifest(identifier, staged, parent, source)))
            self._path(self.sets).mkdir(parents=True, exist_ok=True)
            destination = self._path(self.sets / identifier)
            if not destination.exists():
                try:
                    staging.rename(destination)
                except OSError:
                    if not destination.exists():
                        raise
            existing = self.load(identifier)
            if dict(existing.explicit) != explicit:
                raise ConfigError("config_set_invalid", f"Immutable configuration set {identifier} has changed", 503)
            return existing
        finally:
            if staging.exists():
                for path in written:
                    self._path(path).unlink(missing_ok=True)
                self._path(staging).rmdir()

    def _manifest(self, identifier: str, staged: Mapping[str, StagedDocument], parent: ConfigSet | None,
                  source: str) -> dict:
        keys = {}
        for key in self.registry:
            item = staged.get(key)
            keys[key] = {"mode": KeyMode.DEFAULT} if item is None else {
                "mode": KeyMode.EXPLICIT, "sha256": item.sha256, "content_version": item.content_version}
        return {"schema_version": SET_SCHEMA, "set": identifier, "keys": keys, "source": source,
                "parent": None if parent is None else parent.identifier, "created_at": timestamp()}

    def load(self, identifier: str) -> ConfigSet:
        if not isinstance(identifier, str) or not _SET_ID.fullmatch(identifier):
            raise ConfigError("config_set_unknown", "Configuration sets are named by 24 lowercase hexadecimal digits", 404)
        directory = self._path(self.sets / identifier)
        manifest = self._path(directory / MANIFEST_NAME)
        if not manifest.is_file():
            raise ConfigError("config_set_unknown", f"Configuration set {identifier} does not exist", 404)
        try:
            raw = read_bounded(manifest, MAX_MANIFEST_BYTES, root=self.data_dir)
            config_set = self._validated(identifier, directory, raw)
            for key, sha256 in config_set.explicit.items():
                if _digest(read_bounded(config_set.path(key), self.registry[key].max_bytes, root=self.data_dir)) != sha256:
                    raise ValueError(f"{key} differs from its manifest")
        except ValueError as error:
            raise ConfigError("config_set_invalid", f"Configuration set {identifier} is invalid: {error}", 503) from error
        return config_set

    def _validated(self, identifier: str, directory: Path, raw: bytes) -> ConfigSet:
        manifest = parse_document(raw, limit=MAX_MANIFEST_BYTES)
        if (set(manifest) != MANIFEST_FIELDS
                or type(manifest["schema_version"]) is not int or manifest["schema_version"] != SET_SCHEMA
                or manifest["set"] != identifier or not isinstance(manifest["keys"], dict)):
            raise ValueError("the manifest identity or structure is unsupported")
        explicit, versions = {}, {}
        for key, entry in manifest["keys"].items():
            if key not in self.registry:
                raise ValueError(f"{key!r} is not a known configuration key")
            if entry == {"mode": KeyMode.DEFAULT}:
                continue
            if (not isinstance(entry, dict) or set(entry) != EXPLICIT_FIELDS or entry["mode"] != KeyMode.EXPLICIT
                    or not isinstance(entry["sha256"], str) or not _SHA256.fullmatch(entry["sha256"])
                    or not _bounded_text(entry["content_version"])):
                raise ValueError(f"the {key} entry is malformed")
            explicit[key], versions[key] = entry["sha256"], entry["content_version"]
        source, parent, created_at = manifest["source"], manifest["parent"], manifest["created_at"]
        if (not isinstance(source, str) or not _SOURCE.fullmatch(source)
                or parent is not None and (not isinstance(parent, str) or not _SET_ID.fullmatch(parent))
                or not _bounded_text(created_at)):
            raise ValueError("the provenance fields are malformed")
        datetime.fromisoformat(created_at)
        if config_identity(explicit)[:SET_ID_LENGTH] != identifier:
            raise ValueError("the identity does not match the explicit documents")
        return ConfigSet(identifier, directory, MappingProxyType(dict(sorted(explicit.items()))),
                         MappingProxyType(dict(sorted(versions.items()))), source, parent, created_at, _digest(raw))

    def read_document(self, config_set: ConfigSet, key: str) -> bytes:
        path = self._path(config_set.path(key))
        try:
            data = read_bounded(path, self.registry[key].max_bytes, root=self.data_dir)
        except ValueError as error:
            raise ConfigError("config_set_invalid", f"Configuration set {config_set.identifier} cannot provide {key}: {error}",
                              503) from error
        if _digest(data) != config_set.explicit[key]:
            raise ConfigError("config_set_invalid", f"Configuration set {config_set.identifier} has a changed {key}", 503)
        return data

    def pointer_digest(self) -> str | None:
        path = self._path(self.active)
        if not path.exists():
            return None
        try:
            return _digest(read_bounded(path, MAX_POINTER_BYTES, root=self.data_dir))
        except ValueError as error:
            raise ConfigError("config_pointer_invalid", f"The active configuration pointer is unreadable: {error}",
                              503) from error

    def _read_active(self) -> tuple[ActivePointer | None, ConfigSet | None]:
        path = self._path(self.active)
        if not path.exists():
            return None, None
        try:
            pointer = _parse_pointer(read_bounded(path, MAX_POINTER_BYTES, root=self.data_dir))
            config_set = self.load(pointer.set_id)
            if config_set.manifest_sha256 != pointer.manifest_sha256:
                raise ValueError("its configuration set manifest has changed")
        except ValueError as error:
            raise ConfigError("config_pointer_invalid", f"The active configuration pointer is invalid: {error}", 503) from error
        return pointer, config_set

    def read_active(self) -> ActivePointer | None:
        return self._read_active()[0]

    def _check_pointer(self, expected: str | None, sequence: int) -> None:
        if self.pointer_digest() != expected:
            raise ConfigError("config_pointer_conflict", "The active configuration pointer has changed", 409)
        try:
            current = self.read_active()
        except ConfigError:
            # The caller named this unreadable pointer by digest, so replacing it is an explicit repair.
            return
        if current is not None and sequence < current.sequence:
            raise ConfigError("config_sequence_regressed", "Switch sequences cannot move backwards", 409)

    def prepare_activation(self, identifier: str, sequence: int, *, expected: str | None) -> PendingActivation:
        config_set = self.load(identifier)
        self._check_pointer(expected, _sequence(sequence))
        data = _pointer_bytes(config_set, sequence)
        self._path(self.staging).mkdir(parents=True, exist_ok=True)
        path = self._path(self.staging / f"active-{uuid.uuid4().hex}.json")
        with path.open("xb") as stream:
            stream.write(data)
        return PendingActivation(self, path, data, expected)

    def activate(self, identifier: str, sequence: int, *, expected: str | None) -> ActivePointer:
        with self.prepare_activation(identifier, sequence, expected=expected) as pending:
            return pending.publish()

    def _override(self, spec: ConfigKey, value) -> tuple[Path, str]:
        try:
            path = Path(value)
            return path, _digest(read_bounded(path, spec.max_bytes))
        except (TypeError, ValueError, OSError) as error:
            raise ConfigError("config_override_invalid", f"{spec.cli_flag} cannot be read: {error}", key=spec.key) from error

    def resolve(self, args) -> ResolvedConfig:
        pointer, config_set = self._read_active()
        return self._resolved(args, pointer, config_set)

    def resolve_candidate(self, identifier: str, args) -> ResolvedConfig:
        return self._resolved(args, None, self.load(identifier))

    def _resolved(self, args, pointer: ActivePointer | None, config_set: ConfigSet | None) -> ResolvedConfig:
        entries = {}
        for key, spec in self.registry.items():
            explicit = config_set is not None and key in config_set.explicit
            override = getattr(args, spec.dest, None)
            if override is not None:
                path, sha256 = self._override(spec, override)
                entries[key] = ResolvedEntry(spec, ConfigSource.CLI, path, sha256, shadowed=explicit)
            elif explicit:
                entries[key] = ResolvedEntry(spec, ConfigSource.SET, self._path(config_set.path(key)),
                                             config_set.explicit[key])
            else:
                entries[key] = ResolvedEntry(spec, ConfigSource.DEFAULT, None, None)
        return ResolvedConfig(pointer, config_set, MappingProxyType(entries))

    def describe(self, args) -> dict:
        return {"directory": str(self.root), **self.resolve(args).describe()}


def resolve(args, *, registry: ConfigRegistry = REGISTRY) -> ResolvedConfig:
    return ConfigSets(args.data_dir, registry=registry).resolve(args)

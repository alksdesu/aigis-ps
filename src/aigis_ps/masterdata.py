from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType

from .alxml import dumps
from .errors import ProtocolError
from .local_io import atomic_replace, checked_path, deserialize, exclusive_file, read_bounded, read_json_object
from .public_tables import MASTER_IDS, MASTER_LIMITS, validate_public_master


MASTER_TABLES = MASTER_IDS
MASTER_DIRECTORY = "masterdata"
SNAPSHOTS_DIRECTORY = "snapshots"
ACTIVE_POINTER = "active.json"
MANIFEST_NAME = "manifest.json"
POINTER_LOCK = "pointer.lock"
PENDING_PREFIX = "pending-"
POINTER_TEMPORARY = "active-{token}.tmp"
CAPTURE_SCHEMA = 1
SNAPSHOT_SCHEMA = 1
SNAPSHOT_ID_LENGTH = 24
MAX_MANIFEST_BYTES = 256 * 1024
MAX_POINTER_BYTES = 4096
# Imports and activations hold the pointer lock only while they replace one small file, so contenders wait briefly.
POINTER_LOCK_SECONDS = 2.0
POINTER_RETRY_SECONDS = 0.01
TABLE_SUMMARY_FIELDS = ("rows", "columns", "full_schema", "bytes", "sha256", "source_sha256")
CAPTURE_SUMMARY_FIELDS = ("rows", "columns", "bytes")
CAPTURE_TIME_FIELDS = ("started_utc", "finished_utc")
_UNREADABLE = (ValueError, OSError, RecursionError)
_SNAPSHOT_ID = re.compile(rf"[a-f0-9]{{{SNAPSHOT_ID_LENGTH}}}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def is_snapshot_id(value: object) -> bool:
    return isinstance(value, str) and _SNAPSHOT_ID.fullmatch(value) is not None


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def snapshot_identifier(client_hash: str, source_digests: Mapping[str, str]) -> str:
    tables = {name: source_digests[name] for name in MASTER_TABLES}
    return digest(json_bytes({"input": client_hash, "tables": tables}))[:SNAPSHOT_ID_LENGTH]


def table_summary(entry: Mapping[str, object]) -> dict:
    return {name: entry.get(name) for name in TABLE_SUMMARY_FIELDS}


def _integer(value: object) -> int | None:
    return value if type(value) is int else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _invalid(message: str) -> ProtocolError:
    return ProtocolError("masterdata_invalid", message, HTTPStatus.SERVICE_UNAVAILABLE)


def _unknown(message: str) -> ProtocolError:
    return ProtocolError("masterdata_snapshot_unknown", message, HTTPStatus.NOT_FOUND)


@dataclass(frozen=True)
class MasterSnapshot:
    identifier: str
    client_hash: str
    documents: Mapping[str, bytes]

    def __post_init__(self) -> None:
        documents = dict(self.documents)
        if set(documents) != set(MASTER_TABLES) or any(not isinstance(value, bytes) for value in documents.values()):
            raise ValueError("A master snapshot requires both immutable public documents")
        object.__setattr__(self, "documents", MappingProxyType(documents))

    def get_xml(self, name: str) -> bytes:
        return self.documents[name]


@dataclass(frozen=True)
class MasterPointer:
    snapshot: str
    manifest_sha256: str
    digest: str

    def describe(self) -> dict:
        return {"snapshot": self.snapshot, "manifest_sha256": self.manifest_sha256, "digest": self.digest}


class MasterDataStore:
    def __init__(self, directory: Path, *, expected_hash: str | None = None, schema_fields: dict[str, list[str]] | None = None):
        self.directory = checked_path(directory)
        contracts = json.loads(files("aigis_ps").joinpath("data/protocol-contracts.json").read_text(encoding="utf-8"))
        self.expected_hash = expected_hash or contracts["input_sha256"]
        if schema_fields is None:
            schemas = json.loads(files("aigis_ps").joinpath("data/master-schemas.json").read_text(encoding="utf-8"))
            schema_fields = {name: [field["name"] for field in schema["fields"]] for name, schema in schemas["tables"].items()}
        self.schema_fields = schema_fields

    @classmethod
    def for_data_dir(cls, data_dir: Path, **options) -> MasterDataStore:
        return cls(Path(data_dir) / MASTER_DIRECTORY, **options)

    def import_capture(self, capture_dir: Path, *, activate: bool = True) -> dict:
        source = checked_path(capture_dir)
        _, entries = self._capture_tables(source)
        parsed, raw_tables, served_tables = {}, {}, {}
        for name, entry in entries.items():
            raw = read_bounded(source / entry["path"], MASTER_LIMITS.max_bytes, root=source)
            if digest(raw) != entry["sha256"] or type(entry.get("bytes")) is not int or len(raw) != entry["bytes"]:
                raise ValueError("Captured table does not match its manifest")
            document, metadata = validate_public_master(name, raw, self.schema_fields)
            canonical = dumps(document, limits=MASTER_LIMITS)
            parsed[name] = {**metadata, "source_sha256": digest(raw), "sha256": digest(canonical),
                            "bytes": len(canonical), "path": f"{name}.xml"}
            raw_tables[name], served_tables[name] = raw, canonical
        sources = {name: parsed[name]["source_sha256"] for name in MASTER_TABLES}
        snapshot_id = snapshot_identifier(self.expected_hash, sources)
        manifest = {"schema_version": SNAPSHOT_SCHEMA, "snapshot": snapshot_id, "input_sha256": self.expected_hash,
                    "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tables": parsed}
        snapshots = self._snapshot_root()
        snapshots.mkdir(parents=True, exist_ok=True)
        destination = checked_path(snapshots / snapshot_id, self.directory)
        if destination.exists():
            manifest = self._existing(destination, snapshot_id, parsed)
        else:
            staging = checked_path(snapshots / (PENDING_PREFIX + uuid.uuid4().hex), self.directory)
            staging.mkdir()
            written = []
            try:
                for name in MASTER_TABLES:
                    for filename, data in ((f"{name}.xml", served_tables[name]), (f"{name}.source.xml", raw_tables[name])):
                        path = checked_path(staging / filename, staging)
                        with path.open("xb") as stream:
                            written.append(path)
                            stream.write(data)
                path = checked_path(staging / MANIFEST_NAME, staging)
                with path.open("xb") as stream:
                    written.append(path)
                    stream.write(json_bytes(manifest))
                try:
                    checked_path(destination, self.directory)
                    staging.rename(destination)
                except OSError:
                    if not destination.exists():
                        raise
                    manifest = self._existing(destination, snapshot_id, parsed)
            finally:
                if staging.exists():
                    for path in written:
                        checked_path(path, staging).unlink(missing_ok=True)
                    checked_path(staging, snapshots).rmdir()
        if activate:
            raw_manifest = read_bounded(destination / MANIFEST_NAME, MAX_MANIFEST_BYTES, root=self.directory)
            with self._locked():
                self._write_pointer(snapshot_id, digest(raw_manifest))
        return manifest

    def inspect_capture(self, capture_dir: Path) -> dict:
        try:
            capture, entries = self._capture_tables(checked_path(capture_dir))
            sources = {name: entry["sha256"] for name, entry in entries.items()}
            identifier = snapshot_identifier(self.expected_hash, sources)
            imported = (self._snapshot_path(identifier) / MANIFEST_NAME).is_file()
        except _UNREADABLE as error:
            return {"importable": False, "error": str(error), "snapshot": None, "imported": False,
                    **dict.fromkeys(CAPTURE_TIME_FIELDS), "tables": None}
        return {"importable": True, "error": None, "snapshot": identifier, "imported": imported,
                **{name: _text(capture.get(name)) for name in CAPTURE_TIME_FIELDS},
                "tables": {name: {field: _integer(entry.get(field)) for field in CAPTURE_SUMMARY_FIELDS}
                           for name, entry in entries.items()}}

    def _capture_tables(self, source: Path) -> tuple[dict, dict[str, dict]]:
        capture = read_json_object(source / MANIFEST_NAME, root=source)
        if (type(capture.get("schema_version")) is not int or capture["schema_version"] != CAPTURE_SCHEMA
                or capture.get("complete") is not True):
            raise ValueError("Capture must contain both completed master tables")
        if capture.get("input_sha256") != self.expected_hash or not isinstance(capture.get("tables"), dict):
            raise ValueError("Capture client fingerprint or table index is invalid")
        entries = {}
        for name in MASTER_TABLES:
            entry = capture["tables"].get(name)
            if not isinstance(entry, dict):
                raise ValueError(f"Capture is missing {name}")
            if entry.get("path") != f"{name}.xml":
                raise ValueError("Captured table path leaves its fixed filename contract")
            if not is_sha256(entry.get("sha256")):
                raise ValueError("Captured table digest is invalid")
            entries[name] = entry
        return capture, entries

    def _snapshot_root(self) -> Path:
        return checked_path(self.directory / SNAPSHOTS_DIRECTORY, self.directory)

    def _snapshot_path(self, identifier: object) -> Path:
        if not is_snapshot_id(identifier):
            raise ValueError("Invalid master snapshot identifier")
        return checked_path(self.directory / SNAPSHOTS_DIRECTORY / identifier, self.directory)

    def _pointer_path(self) -> Path:
        return checked_path(self.directory / ACTIVE_POINTER, self.directory)

    def _manifest(self, directory: Path, identifier: str) -> tuple[bytes, dict]:
        raw = read_bounded(directory / MANIFEST_NAME, MAX_MANIFEST_BYTES, root=self.directory)
        manifest = deserialize(raw)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != SNAPSHOT_SCHEMA or manifest.get("snapshot") != identifier or manifest.get("input_sha256") != self.expected_hash or not isinstance(manifest.get("tables"), dict):
            raise ValueError("Snapshot identity or structure mismatch")
        for name in MASTER_TABLES:
            entry = manifest["tables"].get(name)
            if not isinstance(entry, dict) or entry.get("path") != f"{name}.xml" or type(entry.get("bytes")) is not int or not 0 < entry["bytes"] <= MASTER_LIMITS.max_bytes:
                raise ValueError("Invalid master table metadata")
            if not all(is_sha256(entry.get(key)) for key in ("sha256", "source_sha256")):
                raise ValueError("Invalid master table digest")
        return raw, manifest

    def _table_xml(self, directory: Path, manifest: dict, name: str) -> bytes:
        raw = read_bounded(directory / f"{name}.xml", MASTER_LIMITS.max_bytes, root=self.directory)
        entry = manifest["tables"][name]
        if len(raw) != entry["bytes"] or digest(raw) != entry["sha256"]:
            raise ValueError("Master content hash mismatch")
        return raw

    def _existing(self, destination: Path, identifier: str, tables: dict) -> dict:
        _, existing = self._manifest(destination, identifier)
        if existing["tables"] != tables:
            raise ValueError("Existing immutable master snapshot has changed")
        for name in MASTER_TABLES:
            self._table_xml(destination, existing, name)
            source = read_bounded(destination / f"{name}.source.xml", MASTER_LIMITS.max_bytes, root=self.directory)
            if digest(source) != tables[name]["source_sha256"]:
                raise ValueError("Existing original master content has changed")
        return existing

    def _read_pointer(self) -> tuple[MasterPointer, Path, dict] | None:
        path = self._pointer_path()
        if not path.exists():
            return None
        data = read_bounded(path, MAX_POINTER_BYTES, root=self.directory)
        pointer = deserialize(data)
        if not isinstance(pointer, dict):
            raise ValueError("Local JSON data must be an object")
        identifier, manifest_sha256 = pointer.get("snapshot"), pointer.get("manifest_sha256")
        directory = self._snapshot_path(identifier)
        raw, manifest = self._manifest(directory, identifier)
        if digest(raw) != manifest_sha256:
            raise ValueError("Snapshot manifest hash mismatch")
        return MasterPointer(identifier, manifest_sha256, digest(data)), directory, manifest

    def _active(self) -> tuple[Path, dict]:
        try:
            found = self._read_pointer()
        except _UNREADABLE as error:
            raise _invalid("Unable to verify the active master snapshot") from error
        if found is None:
            raise ProtocolError("masterdata_missing", "Capture and import current CardData and QuestTable first",
                                HTTPStatus.SERVICE_UNAVAILABLE)
        _, directory, manifest = found
        return directory, manifest

    def _stored(self, identifier: object) -> tuple[Path, bytes, dict]:
        if not is_snapshot_id(identifier):
            raise _unknown("Master snapshots are named by 24 lowercase hexadecimal digits")
        try:
            directory = self._snapshot_path(identifier)
            present = (directory / MANIFEST_NAME).is_file()
        except _UNREADABLE as error:
            raise _invalid(f"Master snapshot {identifier} cannot be read") from error
        if not present:
            raise _unknown(f"Master snapshot {identifier} does not exist")
        try:
            raw, manifest = self._manifest(directory, identifier)
        except _UNREADABLE as error:
            raise _invalid(f"Master snapshot {identifier} has an invalid manifest") from error
        return directory, raw, manifest

    def _located(self, identifier: str | None) -> tuple[Path, dict]:
        if identifier is None:
            return self._active()
        directory, _, manifest = self._stored(identifier)
        return directory, manifest

    def get_xml(self, name: str) -> bytes:
        if name not in MASTER_TABLES:
            raise KeyError(name)
        directory, manifest = self._active()
        try:
            return self._table_xml(directory, manifest, name)
        except (ValueError, OSError) as error:
            raise _invalid("Master content differs from the imported snapshot") from error

    def snapshot(self, identifier: str | None = None) -> MasterSnapshot:
        directory, manifest = self._located(identifier)
        try:
            documents = {name: self._table_xml(directory, manifest, name) for name in MASTER_TABLES}
            return MasterSnapshot(manifest["snapshot"], self.expected_hash, documents)
        except (ValueError, OSError) as error:
            raise _invalid("Unable to read one consistent public master snapshot") from error

    def status(self, identifier: str | None = None) -> dict:
        try:
            directory, manifest = self._located(identifier)
            for name in MASTER_TABLES:
                self._table_xml(directory, manifest, name)
        except ProtocolError as error:
            return {"ready": False, "code": error.code, "message": str(error), "required": list(MASTER_TABLES)}
        except (ValueError, OSError):
            return {"ready": False, "code": "masterdata_invalid", "message": "Master content differs from the imported snapshot", "required": list(MASTER_TABLES)}
        return {"ready": True, "snapshot": manifest["snapshot"], "tables": manifest["tables"]}

    def pointer(self) -> MasterPointer | None:
        try:
            found = self._read_pointer()
        except _UNREADABLE as error:
            raise _invalid("Unable to verify the active master snapshot") from error
        return None if found is None else found[0]

    def pointer_digest(self) -> str | None:
        try:
            path = self._pointer_path()
            return digest(read_bounded(path, MAX_POINTER_BYTES, root=self.directory)) if path.exists() else None
        except _UNREADABLE as error:
            raise _invalid("The active master snapshot pointer is unreadable") from error

    def list_snapshots(self) -> tuple[dict, ...]:
        try:
            root = self._snapshot_root()
            names = [path.name for path in root.iterdir() if is_snapshot_id(path.name)] if root.is_dir() else []
        except _UNREADABLE as error:
            raise _invalid("The master snapshot directory cannot be listed") from error
        listed = [self._listing(name) for name in names]
        return tuple(sorted(listed, key=lambda entry: (entry["imported_at"] or "", entry["snapshot"]), reverse=True))

    def _listing(self, identifier: str) -> dict:
        try:
            _, manifest = self._manifest(self._snapshot_path(identifier), identifier)
        except _UNREADABLE as error:
            return {"snapshot": identifier, "valid": False, "error": str(error), "imported_at": None, "tables": None}
        return {"snapshot": identifier, "valid": True, "error": None, "imported_at": _text(manifest.get("imported_at")),
                "tables": {name: table_summary(manifest["tables"][name]) for name in MASTER_TABLES}}

    def activate(self, identifier: str, *, expected: str | None) -> MasterPointer:
        directory, raw, manifest = self._stored(identifier)
        try:
            for name in MASTER_TABLES:
                self._table_xml(directory, manifest, name)
        except _UNREADABLE as error:
            raise _invalid(f"Master snapshot {identifier} differs from its manifest") from error
        with self._locked():
            if self.pointer_digest() != expected:
                raise ProtocolError("masterdata_pointer_conflict", "The active master snapshot pointer has changed",
                                    HTTPStatus.CONFLICT)
            return self._write_pointer(identifier, digest(raw))

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock = checked_path(self.directory / POINTER_LOCK, self.directory)
        deadline = time.monotonic() + POINTER_LOCK_SECONDS
        while True:
            with ExitStack() as stack:
                try:
                    stack.enter_context(exclusive_file(lock))
                except ValueError as error:
                    if time.monotonic() >= deadline:
                        raise ProtocolError("masterdata_pointer_busy", "Another master snapshot activation is "
                                            "in progress", HTTPStatus.CONFLICT) from error
                else:
                    yield
                    return
            time.sleep(POINTER_RETRY_SECONDS)

    def _write_pointer(self, identifier: str, manifest_sha256: str) -> MasterPointer:
        data = json_bytes({"snapshot": identifier, "manifest_sha256": manifest_sha256})
        temporary = checked_path(self.directory / POINTER_TEMPORARY.format(token=uuid.uuid4().hex), self.directory)
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
            atomic_replace(temporary, self._pointer_path())
        finally:
            checked_path(temporary, self.directory).unlink(missing_ok=True)
        return MasterPointer(identifier, manifest_sha256, digest(data))

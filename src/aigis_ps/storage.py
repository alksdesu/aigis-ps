from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .errors import ProtocolError
from .local_io import checked_path
# Re-exported for the modules that import the strict JSON reader from here.
from .local_io import deserialize as deserialize
from .protocol import RequestEnvelope


APPLICATION_ID = 0x41494753
SCHEMA_VERSION = 1
MAX_REVISION = 0x7FFFFFFFFFFFFFFF
MAX_STATE_BYTES = 512 * 1024 * 1024
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
ADMIN_ACTOR = "local-admin"
ADMIN_FAILURE_LIMIT = 2000
ADMIN_PAGE_SIZE = 50
MAX_ADMIN_PAGE_SIZE = 200
MAX_AUDIT_FIELD_BYTES = 1024 * 1024
MAX_FAILURE_MESSAGE = 1000
MAX_FAILURE_DETAIL_BYTES = 64 * 1024
AUTO_BACKUP_KIND = "auto"
BACKUP_KINDS = frozenset((AUTO_BACKUP_KIND, "manual"))
BACKUP_PREFIX = "admin"
AUTO_BACKUP_KEEP = 1
AUTO_BACKUP_LIFETIME = timedelta(days=1)
MAX_BACKUP_REASON = 200
BACKUP_PAGES = 256
BACKUP_RETRY_SECONDS = 0.01
BACKUP_TIMEOUT_SECONDS = 30
BACKUP_FAILED = "admin_backup_failed"
CHECKPOINT_BUSY_TIMEOUT_SECONDS = 0
RECORD_FIELDS = frozenset(("kind", "operations", "targets", "diff", "result"))
RECORD_OPTIONAL_FIELDS = frozenset(("actor", "plan_digest", "generation_id", "content_versions", "switch"))
RECORD_CLAIM_FIELDS = frozenset(("revision_before", "revision_after", "state_sha256_before", "state_sha256_after"))
SWITCH_FIELDS = frozenset(("config_set", "tier"))
SWITCH_OPTIONAL_FIELDS = frozenset(("generation_id",))
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:-]{0,127}\Z")
_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_AUTO_BACKUP_NAME = re.compile(rf"{BACKUP_PREFIX}-{AUTO_BACKUP_KIND}-[0-9a-f]{{32}}\.sqlite3\Z")
_HTTP_STATUSES = range(100, 600)
_PRIMARY_RESULT_MASK = 0xFF
_BUSY_RESULTS = frozenset((sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
_PROFILE_FIELDS = ("id", "player_id", "user_key", "method_key", "state_json", "revision", "created_at", "updated_at")
_PROFILE_TEXT_FIELDS = ("player_id", "user_key", "method_key", "created_at", "updated_at")
_OPERATION_FIELDS = ("op_id", "sequence", "kind", "actor", "operations_json", "payload_digest", "plan_digest", "targets",
                     "revision_before", "revision_after", "state_sha256_before", "state_sha256_after", "diff_json",
                     "generation_id", "content_versions_json", "result_json", "created_at")
_FAILURE_FIELDS = ("id", "op_id", "kind", "actor", "error_code", "status", "message", "detail_json", "created_at")
_GENERATION_FIELDS = ("sequence", "config_set", "tier", "generation_id", "op_id", "created_at")
_BACKUP_FIELDS = ("id", "kind", "reason", "op_id", "path", "sha256", "revision", "created_at")
_JSON_COLUMNS = {"operations_json": "operations", "targets": "targets", "diff_json": "diff",
                 "content_versions_json": "content_versions", "result_json": "result", "detail_json": "detail"}
# The store keeps each of these record fields as one JSON column of at most MAX_AUDIT_FIELD_BYTES.
AUDIT_JSON_FIELDS = tuple(name for name in _JSON_COLUMNS.values() if name != "detail")
_ADMIN_SCHEMA = {
    "admin_operations": """CREATE TABLE IF NOT EXISTS admin_operations (
        op_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL, actor TEXT NOT NULL,
        operations_json TEXT NOT NULL, payload_digest TEXT NOT NULL, plan_digest TEXT, targets TEXT NOT NULL,
        revision_before INTEGER NOT NULL, revision_after INTEGER NOT NULL,
        state_sha256_before TEXT NOT NULL, state_sha256_after TEXT NOT NULL, diff_json TEXT NOT NULL,
        generation_id TEXT, content_versions_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
    "admin_failures": """CREATE TABLE IF NOT EXISTS admin_failures (
        id INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT, kind TEXT NOT NULL, actor TEXT NOT NULL,
        error_code TEXT NOT NULL, status INTEGER NOT NULL, message TEXT NOT NULL, detail_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "config_generations": """CREATE TABLE IF NOT EXISTS config_generations (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, config_set TEXT NOT NULL, tier TEXT NOT NULL,
        generation_id TEXT, op_id TEXT UNIQUE, created_at TEXT NOT NULL
    )""",
    "backups": """CREATE TABLE IF NOT EXISTS backups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, reason TEXT NOT NULL, op_id TEXT,
        path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL
    )""",
}
ADMIN_TABLES = tuple(_ADMIN_SCHEMA)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp() -> str:
    return utc_now().isoformat(timespec="seconds")


def serialize(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(value: object) -> str:
    return _text_digest(serialize(value))


def admin_payload_digest(op_id: str, plan_digest: str) -> str:
    return _digest({"op_id": _token(op_id, "operation ID"), "plan_digest": _hash(plan_digest, "plan digest")})


@dataclass(frozen=True)
class PlayerProfile:
    player_id: str
    user_key: str
    method_key: str
    state: dict
    revision: int


class AdminOperationConflict(ProtocolError):
    def __init__(self, op_id: str):
        super().__init__("admin_operation_conflict", "The operation ID was already used for a different request", 409)
        self.op_id = op_id


class RevisionConflict(ProtocolError):
    def __init__(self, current_revision: int):
        super().__init__("admin_revision_conflict", "Profile revision changed; preview the operation again", 409)
        self.current_revision = current_revision


@dataclass(frozen=True)
class AdminCommit:
    operation: dict
    replayed: bool
    generation: dict | None


@dataclass(frozen=True)
class AdminPage:
    items: tuple[dict, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class BackupSnapshot:
    path: Path
    sha256: str
    profile: PlayerProfile


def _token(value, label, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"Admin {label} is invalid")
    return value


def _name(value, label):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"Admin {label} is invalid")
    return value


def _hash(value, label, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"Admin {label} must be a SHA-256 digest")
    return value


def _revision(value):
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise ValueError("Admin revisions must be stored profile revisions")
    return value


def _audit_json(value, label, kinds=object):
    if not isinstance(value, kinds):
        raise TypeError(f"Admin {label} has the wrong JSON type")
    encoded = serialize(value)
    if len(encoded.encode("utf-8")) > MAX_AUDIT_FIELD_BYTES:
        raise ValueError(f"Admin {label} exceeds the audit size limit")
    return encoded


def _fields(value, required, optional, label):
    if not isinstance(value, Mapping):
        raise TypeError(f"Admin {label} must be a mapping")
    names = set(value)
    if not required <= names or names - required - optional:
        raise ValueError(f"Admin {label} has missing or unknown fields")
    return value


def _reason(value):
    if not isinstance(value, str) or not 0 < len(value) <= MAX_BACKUP_REASON or not value.isprintable():
        raise ValueError("Backup reasons must be one printable line")
    return value


def _backup_kind(value):
    if not isinstance(value, str) or value not in BACKUP_KINDS:
        raise ValueError("Backups must be registered as auto or manual")
    return value


def _created(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Admin records require a creation time")
    return value


def _failure_detail(value):
    encoded = serialize(value)
    size = len(encoded.encode("utf-8"))
    return encoded if size <= MAX_FAILURE_DETAIL_BYTES else serialize({"truncated": True, "bytes": size})


def _switch_columns(switch) -> dict | None:
    if switch is None:
        return None
    _fields(switch, SWITCH_FIELDS, SWITCH_OPTIONAL_FIELDS, "generation switch")
    return {"config_set": _token(switch["config_set"], "config set"), "tier": _name(switch["tier"], "generation tier"),
            "generation_id": _token(switch.get("generation_id"), "generation ID", optional=True)}


def _prepare_operation(op_id, payload_digest, record) -> tuple[dict, dict | None, dict]:
    _fields(record, RECORD_FIELDS, RECORD_OPTIONAL_FIELDS | RECORD_CLAIM_FIELDS, "record")
    operations = _audit_json(record["operations"], "operations", (list, tuple))
    if not record["operations"]:
        raise ValueError("Admin operations must list at least one command")
    columns = {"op_id": _token(op_id, "operation ID"), "kind": _name(record["kind"], "kind"),
               "actor": _name(record.get("actor", ADMIN_ACTOR), "actor"), "operations_json": operations,
               "payload_digest": _hash(payload_digest, "payload digest"),
               "plan_digest": _hash(record.get("plan_digest"), "plan digest", optional=True),
               "targets": _audit_json(record["targets"], "targets", (list, tuple)),
               "diff_json": _audit_json(record["diff"], "diff"),
               "generation_id": _token(record.get("generation_id"), "generation ID", optional=True),
               "content_versions_json": _audit_json(record.get("content_versions", {}), "content versions", dict),
               "result_json": _audit_json(record["result"], "result")}
    claims = {name: record[name] for name in RECORD_CLAIM_FIELDS & set(record)}
    return columns, _switch_columns(record.get("switch")), claims


def _decode(fields, row) -> dict:
    return {_JSON_COLUMNS.get(name, name): json.loads(value) if name in _JSON_COLUMNS else value
            for name, value in zip(fields, row)}


def _table_exists(connection, table) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _select(connection, table, fields, key, value) -> dict | None:
    row = connection.execute(f"SELECT {', '.join(fields)} FROM {table} WHERE {key}=?", (value,)).fetchone()
    return None if row is None else _decode(fields, row)


def _insert(connection, table, values) -> int:
    names = tuple(values)
    return connection.execute(f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' * len(names))})",
                              tuple(values.values())).lastrowid


def _replay(connection, operation, payload_digest) -> AdminCommit:
    if operation["payload_digest"] != payload_digest:
        raise AdminOperationConflict(operation["op_id"])
    return AdminCommit(operation, True, _select(connection, "config_generations", _GENERATION_FIELDS, "op_id", operation["op_id"]))


def _insert_generation(connection, columns, op_id, created_at) -> dict:
    if op_id is not None:
        existing = _select(connection, "config_generations", _GENERATION_FIELDS, "op_id", op_id)
        if existing is not None:
            if any(existing[name] != value for name, value in columns.items()):
                raise AdminOperationConflict(op_id)
            return existing
    sequence = _insert(connection, "config_generations", {**columns, "op_id": op_id, "created_at": _created(created_at)})
    return _select(connection, "config_generations", _GENERATION_FIELDS, "sequence", sequence)


def _write_operation(connection, columns, switch, claims, *, revision_before, revision_after, state_sha256_before,
                     state_sha256_after, created_at) -> AdminCommit:
    actual = {"revision_before": _revision(revision_before), "revision_after": _revision(revision_after),
              "state_sha256_before": _hash(state_sha256_before, "state digest"),
              "state_sha256_after": _hash(state_sha256_after, "state digest")}
    if revision_after - revision_before not in (0, 1):
        raise ValueError("Admin operations change the profile revision by at most one")
    if any(type(value) is not type(actual[name]) or value != actual[name] for name, value in claims.items()):
        raise ValueError("Admin record disagrees with the stored profile change")
    op_id = columns["op_id"]
    if _select(connection, "admin_operations", ("op_id",), "op_id", op_id) is not None:
        raise AdminOperationConflict(op_id)
    sequence = connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM admin_operations").fetchone()[0]
    _insert(connection, "admin_operations", {**columns, "sequence": sequence, **actual, "created_at": _created(created_at)})
    generation = None if switch is None else _insert_generation(connection, switch, op_id, created_at)
    return AdminCommit(_select(connection, "admin_operations", _OPERATION_FIELDS, "op_id", op_id), False, generation)


def ensure_admin_tables(connection: sqlite3.Connection) -> None:
    # user_version stays 1: the previous server only rejects newer versions and ignores unknown tables.
    if not connection.in_transaction:
        raise ValueError("Admin tables are created only inside an admin write transaction")
    for statement in _ADMIN_SCHEMA.values():
        connection.execute(statement)


def insert_admin_operation(connection: sqlite3.Connection, op_id: str, payload_digest: str, record: Mapping, *,
                           revision_before: int, revision_after: int, state_sha256_before: str,
                           state_sha256_after: str, created_at: str) -> AdminCommit:
    columns, switch, claims = _prepare_operation(op_id, payload_digest, record)
    return _write_operation(connection, columns, switch, claims, revision_before=revision_before,
                            revision_after=revision_after, state_sha256_before=state_sha256_before,
                            state_sha256_after=state_sha256_after, created_at=created_at)


def _backup_expired(created_at: str, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(created_at) >= AUTO_BACKUP_LIFETIME
    except (TypeError, ValueError):
        return False


def _discard_backup_file(text: str, root: Path) -> bool:
    try:
        path = checked_path(Path(text), root)
    except (OSError, ValueError, TypeError):
        return True
    if _AUTO_BACKUP_NAME.fullmatch(path.name) is None:
        return True
    if _sidecars(path):
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def prune_auto_backups(connection: sqlite3.Connection, root: Path, now: datetime) -> tuple[dict, ...]:
    # Only files this store named inside its own directory are deleted; anything else loses its registration alone.
    rows = connection.execute(f"SELECT {', '.join(_BACKUP_FIELDS)} FROM backups WHERE kind=? ORDER BY id DESC",
                              (AUTO_BACKUP_KIND,)).fetchall()
    removed = []
    for index, row in enumerate(_decode(_BACKUP_FIELDS, row) for row in rows):
        if index < AUTO_BACKUP_KEEP and not _backup_expired(row["created_at"], now):
            continue
        if _discard_backup_file(row["path"], root):
            connection.execute("DELETE FROM backups WHERE id=?", (row["id"],))
            removed.append(row)
    return tuple(removed)


def insert_backup(connection: sqlite3.Connection, *, kind: str, reason: str, op_id: str | None, path: Path,
                  sha256: str, revision: int, created_at: str) -> dict:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("Backups are registered by absolute path")
    identifier = _insert(connection, "backups", {
        "kind": _backup_kind(kind), "reason": _reason(reason), "op_id": _token(op_id, "operation ID", optional=True),
        "path": str(path), "sha256": _hash(sha256, "backup digest"), "revision": _revision(revision),
        "created_at": _created(created_at)})
    return _select(connection, "backups", _BACKUP_FIELDS, "id", identifier)


class Store:
    def __init__(self, path: Path, initial_state: dict | None = None, *, clock=utc_now):
        self.clock = clock
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            app_id = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if (app_id not in (0, APPLICATION_ID)) or (app_id == 0 and tables):
                raise ValueError("Database belongs to another application")
            if version > SCHEMA_VERSION:
                raise ValueError("Database was created by a newer server")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute("""CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY CHECK(id=1), player_id TEXT NOT NULL,
                    user_key TEXT NOT NULL, method_key TEXT NOT NULL,
                    state_json TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )""")
                connection.execute("""CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                    route TEXT NOT NULL, profile TEXT, request_digest TEXT,
                    wire_bytes INTEGER NOT NULL, response_bytes INTEGER NOT NULL,
                    status INTEGER NOT NULL, error_code TEXT,
                    structure_json TEXT NOT NULL
                )""")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                now = timestamp()
                connection.execute(
                    "INSERT OR IGNORE INTO profiles VALUES (1,?,?,?,?,0,?,?)",
                    ("1", secrets.token_hex(16), secrets.token_hex(16), serialize(initial_state or {}), now, now),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _profile(row: sqlite3.Row) -> PlayerProfile:
        return PlayerProfile(row["player_id"], row["user_key"], row["method_key"], json.loads(row["state_json"]), row["revision"])

    def profile(self) -> PlayerProfile:
        with self.connection() as connection:
            return self._profile(connection.execute("SELECT * FROM profiles WHERE id=1").fetchone())

    def update_state(self, state: dict, expected_revision: int) -> PlayerProfile:
        encoded = serialize(state)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = connection.execute(
                    "UPDATE profiles SET state_json=?, revision=revision+1, updated_at=? WHERE id=1 AND revision=?",
                    (encoded, timestamp(), expected_revision),
                )
                if result.rowcount != 1:
                    raise ValueError("Profile revision changed; reload before updating")
                profile = self._profile(connection.execute("SELECT * FROM profiles WHERE id=1").fetchone())
                connection.commit()
                return profile
            except BaseException:
                connection.rollback()
                raise

    def record_request(
        self,
        envelope: RequestEnvelope | None,
        route: str,
        wire_bytes: int,
        response_bytes: int,
        status: int,
        error_code: str | None = None,
    ) -> None:
        structure = envelope.structure() if envelope else {}
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO requests VALUES(NULL,?,?,?,?,?,?,?,?,?)",
                    (timestamp(), route[:128], envelope.profile.identity if envelope else None,
                     envelope.digest if envelope else None, wire_bytes, response_bytes, status,
                     error_code, serialize(structure)),
                )
                connection.execute("DELETE FROM requests WHERE id <= (SELECT id FROM requests ORDER BY id DESC LIMIT 1 OFFSET 1000)")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def recent_requests(self, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 200:
            raise ValueError("Request limit must be between 1 and 200")
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["structure"] = json.loads(value.pop("structure_json"))
            result.append(value)
        return result

    @contextmanager
    def _admin_transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                ensure_admin_tables(connection)
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _page(self, table, fields, key, cursor, limit) -> AdminPage:
        if type(limit) is not int or not 1 <= limit <= MAX_ADMIN_PAGE_SIZE:
            raise ValueError(f"Admin page size must be between 1 and {MAX_ADMIN_PAGE_SIZE}")
        if cursor is not None and (type(cursor) is not int or cursor < 1):
            raise ValueError("Admin page cursors must be positive integers")
        query = f"SELECT {', '.join(fields)} FROM {table}"
        with self.connection() as connection:
            if not _table_exists(connection, table):
                return AdminPage((), None)
            if cursor is None:
                rows = connection.execute(f"{query} ORDER BY {key} DESC LIMIT ?", (limit + 1,)).fetchall()
            else:
                rows = connection.execute(f"{query} WHERE {key} < ? ORDER BY {key} DESC LIMIT ?", (cursor, limit + 1)).fetchall()
        items = tuple(_decode(fields, row) for row in rows[:limit])
        return AdminPage(items, items[-1][key] if len(rows) > limit else None)

    def find_admin_operation(self, op_id: str) -> dict | None:
        _token(op_id, "operation ID")
        with self.connection() as connection:
            if not _table_exists(connection, "admin_operations"):
                return None
            return _select(connection, "admin_operations", _OPERATION_FIELDS, "op_id", op_id)

    def commit_admin(self, op_id: str, payload_digest: str, expected_revision: int | None, state: dict | None,
                     record: Mapping) -> AdminCommit:
        columns, switch, claims = _prepare_operation(op_id, payload_digest, record)
        if expected_revision is not None:
            _revision(expected_revision)
        elif state is not None:
            raise ValueError("Admin state changes require an expected revision")
        if state is not None and not isinstance(state, dict):
            raise TypeError("Admin state must be a JSON object")
        encoded = None if state is None else serialize(state)
        with self._admin_transaction() as connection:
            existing = _select(connection, "admin_operations", _OPERATION_FIELDS, "op_id", op_id)
            if existing is not None:
                return _replay(connection, existing, payload_digest)
            current = connection.execute("SELECT revision, state_json FROM profiles WHERE id=1").fetchone()
            revision = current["revision"]
            if expected_revision is not None and revision != expected_revision:
                raise RevisionConflict(revision)
            before = after = _digest(json.loads(current["state_json"]))
            revision_after, created = revision, timestamp()
            if encoded is not None:
                if revision >= MAX_REVISION:
                    raise ProtocolError("admin_revision_exhausted", "The profile revision cannot be incremented", 409)
                updated = connection.execute(
                    "UPDATE profiles SET state_json=?, revision=revision+1, updated_at=? WHERE id=1 AND revision=?",
                    (encoded, created, revision))
                if updated.rowcount != 1:
                    raise RevisionConflict(revision)
                after, revision_after = _text_digest(encoded), revision + 1
            return _write_operation(connection, columns, switch, claims, revision_before=revision,
                                    revision_after=revision_after, state_sha256_before=before,
                                    state_sha256_after=after, created_at=created)

    def record_admin_failure(self, kind: str, error_code: str, status: int, message: str, *, op_id: str | None = None,
                             detail: object = None, actor: str = ADMIN_ACTOR) -> dict:
        if type(status) is not int or status not in _HTTP_STATUSES:
            raise ValueError("Admin failure status must be an HTTP status code")
        if not isinstance(message, str):
            raise TypeError("Admin failure messages must be text")
        values = {"op_id": _token(op_id, "operation ID", optional=True), "kind": _name(kind, "kind"),
                  "actor": _name(actor, "actor"), "error_code": _name(error_code, "error code"), "status": status,
                  "message": message[:MAX_FAILURE_MESSAGE], "detail_json": _failure_detail(detail), "created_at": timestamp()}
        with self._admin_transaction() as connection:
            identifier = _insert(connection, "admin_failures", values)
            connection.execute("DELETE FROM admin_failures WHERE id <= "
                               "(SELECT id FROM admin_failures ORDER BY id DESC LIMIT 1 OFFSET ?)", (ADMIN_FAILURE_LIMIT,))
            return _select(connection, "admin_failures", _FAILURE_FIELDS, "id", identifier)

    def list_admin_operations(self, cursor: int | None = None, limit: int = ADMIN_PAGE_SIZE) -> AdminPage:
        return self._page("admin_operations", _OPERATION_FIELDS, "sequence", cursor, limit)

    def list_admin_failures(self, cursor: int | None = None, limit: int = ADMIN_PAGE_SIZE) -> AdminPage:
        return self._page("admin_failures", _FAILURE_FIELDS, "id", cursor, limit)

    def record_generation(self, config_set: str, tier: str, *, generation_id: str | None = None,
                          op_id: str | None = None) -> dict:
        columns = _switch_columns({"config_set": config_set, "tier": tier, "generation_id": generation_id})
        _token(op_id, "operation ID", optional=True)
        with self._admin_transaction() as connection:
            return _insert_generation(connection, columns, op_id, timestamp())

    def list_generations(self, cursor: int | None = None, limit: int = ADMIN_PAGE_SIZE) -> AdminPage:
        return self._page("config_generations", _GENERATION_FIELDS, "sequence", cursor, limit)

    def backup(self, directory: Path, reason: str, op_id: str | None = None, *, kind: str) -> dict:
        kind, reason, op_id = _backup_kind(kind), _reason(reason), _token(op_id, "operation ID", optional=True)
        path = self._backup_path(directory, kind)
        try:
            with self.connection() as source:
                source.execute("BEGIN")
                try:
                    _copy_database(source, path, _profile_rows(source))
                finally:
                    source.rollback()
            snapshot = read_backup(path, root=self.path.parent)
            created = self._now()
            with self._admin_transaction() as connection:
                registered = insert_backup(connection, kind=kind, reason=reason, op_id=op_id, path=snapshot.path,
                                           sha256=snapshot.sha256, revision=snapshot.profile.revision,
                                           created_at=created.isoformat(timespec="seconds"))
                prune_auto_backups(connection, self.path.parent, created)
                return registered
        except (ProtocolError, sqlite3.Error, OSError) as error:
            path.unlink(missing_ok=True)
            if isinstance(error, ProtocolError) and error.code == BACKUP_FAILED:
                raise
            raise ProtocolError(BACKUP_FAILED, "The online backup could not be verified and registered", 503) from error
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def list_backups(self, cursor: int | None = None, limit: int = ADMIN_PAGE_SIZE) -> AdminPage:
        self.prune_backups()
        return self._page("backups", _BACKUP_FIELDS, "id", cursor, limit)

    def prune_backups(self) -> tuple[dict, ...]:
        now = self._now()
        with self.connection() as connection:
            if not _table_exists(connection, "backups"):
                return ()
        with self._admin_transaction() as connection:
            return prune_auto_backups(connection, self.path.parent, now)

    def _now(self) -> datetime:
        current = self.clock()
        if not isinstance(current, datetime) or current.utcoffset() is None:
            raise ValueError("The store clock must return an aware datetime")
        return current.astimezone(timezone.utc)

    def _backup_path(self, directory, kind) -> Path:
        try:
            directory = checked_path(Path(directory), self.path.parent)
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir():
                raise ValueError("Backup destination is not a directory")
            path = checked_path(directory / f"{BACKUP_PREFIX}-{kind}-{uuid.uuid4().hex}.sqlite3", directory)
            with path.open("xb"):
                pass
            return path
        except (OSError, ValueError, TypeError) as error:
            raise ProtocolError("admin_backup_path_invalid", "Backups must be new files inside the data directory", 400) from error


def _sidecars(path) -> tuple[str, ...]:
    return tuple(suffix for suffix in SIDECAR_SUFFIXES if Path(str(path) + suffix).exists())


def _stamp(path) -> tuple[int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def _file_sha256(path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _database_identity(connection) -> tuple[int, int]:
    return (connection.execute("PRAGMA application_id").fetchone()[0],
            connection.execute("PRAGMA user_version").fetchone()[0])


def _profile_rows(connection) -> tuple[tuple, ...]:
    return tuple(tuple(row) for row in connection.execute(
        f"SELECT {', '.join(_PROFILE_FIELDS)} FROM profiles ORDER BY id").fetchall())


def _stored_profile(rows) -> PlayerProfile:
    if len(rows) != 1:
        raise ValueError("A database must hold the single local profile")
    value = dict(zip(_PROFILE_FIELDS, rows[0]))
    if (value["id"] != 1 or type(value["revision"]) is not int or not 0 <= value["revision"] <= MAX_REVISION
            or any(not isinstance(value[name], str) or not value[name] for name in _PROFILE_TEXT_FIELDS)
            or not isinstance(value["state_json"], str) or len(value["state_json"].encode("utf-8")) > MAX_STATE_BYTES):
        raise ValueError("Stored profile metadata is invalid")
    state = deserialize(value["state_json"])
    if not isinstance(state, dict):
        raise ValueError("Stored player state must be an object")
    return PlayerProfile(value["player_id"], value["user_key"], value["method_key"], state, value["revision"])


def _copy_database(source, path, expected) -> None:
    destination = sqlite3.connect(path, isolation_level=None)
    try:
        deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise ProtocolError(BACKUP_FAILED, "The consistent backup did not complete in time", 503)
        source.backup(destination, pages=BACKUP_PAGES, progress=progress, sleep=BACKUP_RETRY_SECONDS)
        # The copy inherits WAL mode, and read-only openers of a WAL file leave sidecars that block later reads.
        destination.execute("PRAGMA journal_mode=DELETE")
        if _profile_rows(destination) != expected:
            raise ProtocolError(BACKUP_FAILED, "The backup does not match the live profile snapshot", 503)
    finally:
        destination.close()


def read_backup(path: Path, *, root: Path | None = None) -> BackupSnapshot:
    try:
        path = checked_path(Path(path), root)
        if not path.is_file():
            raise ValueError("Missing backup file")
    except (OSError, ValueError, TypeError) as error:
        raise ProtocolError("admin_backup_path_invalid", "Backups must be regular local files in the permitted directory", 400) from error
    if _sidecars(path):
        raise ProtocolError("admin_backup_busy", "Journal files beside the backup must be cleared before it is read", 409)
    try:
        stamp, digest = _stamp(path), _file_sha256(path)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, isolation_level=None)
        try:
            connection.execute("PRAGMA query_only=ON")
            if _database_identity(connection) != (APPLICATION_ID, SCHEMA_VERSION):
                raise ValueError("Backup application or schema does not match this server")
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("Backup failed integrity validation")
            profile = _stored_profile(_profile_rows(connection))
        finally:
            connection.close()
        if _stamp(path) != stamp or _sidecars(path):
            raise ValueError("Backup changed while it was read")
    except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError) as error:
        raise ProtocolError("admin_backup_invalid", "The backup is not a valid local server database", 400) from error
    return BackupSnapshot(path, digest, profile)


def checkpoint_idle(path: Path) -> bool:
    try:
        path = checked_path(Path(path))
        if not path.is_file():
            raise ValueError("Missing database")
    except (OSError, ValueError, TypeError) as error:
        raise ProtocolError("admin_database_path_invalid", "Checkpoints require a regular local database path", 400) from error
    try:
        # mode=rw: a database removed after the path check must not be recreated as an empty file.
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=CHECKPOINT_BUSY_TIMEOUT_SECONDS,
                                     isolation_level=None)
        try:
            if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise ProtocolError("admin_database_invalid", "The database belongs to another application", 400)
            busy = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", None)
        if code is not None and (code & _PRIMARY_RESULT_MASK) in _BUSY_RESULTS:
            return False
        raise ProtocolError("admin_database_invalid", "The database could not be checkpointed safely", 400) from error
    return busy == 0 and not _sidecars(path)

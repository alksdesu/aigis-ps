from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .energy_clock import CLOCK_SOURCE_REQUIRED, MARKER_KEY, convert_state, source_offset_valid, validate_clock
from .errors import ProtocolError
from .local_io import checked_path, deserialize
from .storage import (
    APPLICATION_ID, AUTO_BACKUP_KIND, BACKUP_PAGES, BACKUP_RETRY_SECONDS, BACKUP_TIMEOUT_SECONDS, MAX_REVISION,
    MAX_STATE_BYTES, SCHEMA_VERSION, SIDECAR_SUFFIXES, admin_payload_digest, ensure_admin_tables,
    insert_admin_operation, insert_backup, serialize, timestamp,
)


ANCHORS = ("CharismaChangeDate", "CharismaChangeTime", "ActionPointChangeDate", "ActionPointChangeTime")
HISTORIES = ("recovery_history", "quest_state", "present_claim_history", "trade_receipts", "trade_history",
             "gift_state", "evolution_rewards", "training_history", "evolution_history", "barracks_history")
PLAN_FIELDS = frozenset(("schema_version", "operation", "database", "database_identity_sha256",
    "expected_revision", "state_sha256", "account_identity_sha256", "source_offset_minutes",
    "candidate_sha256", "clock_before", "clock_after", "anchors", "summary", "plan_sha256"))
AUDIT_KIND = "energy_migration"
AUDIT_TARGETS = (f"/{MARKER_KEY}", *(f"/tables/game_param/0/{name}" for name in ANCHORS))
AUDIT_DIFF_FIELDS = ("anchors", "clock_before", "clock_after")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class _Snapshot:
    state: dict
    raw_state: str
    revision: int
    identity: tuple
    file_identity: str


def _error(code, message, status=503):
    raise ProtocolError(code, message, status)


def _digest(value):
    return hashlib.sha256(serialize(value).encode("utf-8")).hexdigest()


def _offset(value):
    if not source_offset_valid(value):
        _error("energy_clock_source_invalid", "Source offset must be an explicit native minute offset", 400)


def database_path(value, *, existing=True):
    try:
        path = checked_path(Path(value))
        if existing and not path.is_file():
            raise ValueError("Missing database")
        return path
    except (OSError, ValueError, TypeError) as error:
        raise ProtocolError("energy_migration_path_invalid", "Migration requires a regular local database path", 400) from error


def _file_identity(path):
    info = path.stat()
    return _digest((info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", None)))


def _offline(path):
    if any(Path(str(path) + suffix).exists() for suffix in SIDECAR_SUFFIXES):
        _error("energy_migration_database_busy", "Stop the service and close database connections before migration", 409)


def _stamp(path):
    info = path.stat()
    return _file_identity(path), info.st_size, info.st_mtime_ns


def _unchanged(path, stamp):
    _offline(path)
    if _stamp(path) != stamp:
        _error("energy_migration_stale", "Database file changed during offline inspection", 409)


def _read_snapshot(connection, file_identity):
    if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
        _error("energy_migration_database_invalid", "Database application or schema does not match this server")
    if connection.execute("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='trigger')").fetchone()[0]:
        _error("energy_migration_database_invalid", "Migration does not support databases with custom triggers")
    rows = connection.execute("SELECT id,player_id,user_key,method_key,state_json,revision,created_at,updated_at FROM profiles").fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        _error("energy_migration_database_invalid", "Migration requires the single local profile")
    row = rows[0]
    if (type(row[5]) is not int or not 0 <= row[5] <= MAX_REVISION
            or any(not isinstance(row[index], str) or not row[index] for index in (1, 2, 3, 6, 7))
            or not isinstance(row[4], str) or len(row[4].encode("utf-8")) > MAX_STATE_BYTES):
        _error("energy_migration_database_invalid", "Stored profile metadata is invalid")
    state = deserialize(row[4])
    if not isinstance(state, dict):
        _error("energy_migration_database_invalid", "Stored player state must be an object")
    return _Snapshot(state, row[4], row[5], (row[0], row[1], row[2], row[3], row[6]), file_identity)


@contextmanager
def read_database(path):
    _offline(path)
    stamp = _stamp(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("BEGIN")
        snapshot = _read_snapshot(connection, stamp[0])
        _unchanged(path, stamp)
        yield connection, snapshot
        _unchanged(path, stamp)
    finally:
        connection.rollback()
        connection.close()


def _anchor_row(state):
    rows = state.get("tables", {}).get("game_param") if isinstance(state.get("tables"), dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        _error("energy_migration_state_invalid", "Migration requires one game parameter row")
    if any(name not in rows[0] for name in ANCHORS):
        _error("energy_migration_state_invalid", "Migration requires all four energy anchors")
    return rows[0]


def _convert(snapshot, offset):
    before = deepcopy(snapshot.state)
    candidate = convert_state(before, offset)
    if before != snapshot.state or not isinstance(candidate, dict):
        _error("energy_migration_scope_invalid", "Clock conversion changed its source state")
    validate_clock(candidate)
    original = _anchor_row(snapshot.state)
    restored = deepcopy(candidate)
    row = _anchor_row(restored)
    for name in ANCHORS:
        row[name] = original[name]
    if MARKER_KEY in snapshot.state:
        restored[MARKER_KEY] = deepcopy(snapshot.state[MARKER_KEY])
    else:
        restored.pop(MARKER_KEY, None)
    if restored != snapshot.state:
        _error("energy_migration_scope_invalid", "Clock conversion changed data outside the energy anchors")
    return candidate


def _build_plan(path, snapshot, offset):
    candidate = _convert(snapshot, offset)
    if candidate != snapshot.state and snapshot.revision == MAX_REVISION:
        _error("energy_migration_revision_exhausted", "The profile revision cannot be incremented", 409)
    before, after = _anchor_row(snapshot.state), _anchor_row(candidate)
    quest = snapshot.state.get("quest_state")
    player = snapshot.state.get("player_state")
    units = player.get("units") if isinstance(player, dict) else None
    result = {"schema_version": 1, "operation": "energy_clock_migration", "database": str(path),
              "database_identity_sha256": snapshot.file_identity, "expected_revision": snapshot.revision,
              "state_sha256": _digest(snapshot.state), "account_identity_sha256": _digest(snapshot.identity),
              "source_offset_minutes": offset, "candidate_sha256": _digest(candidate),
              "clock_before": deepcopy(snapshot.state.get(MARKER_KEY)), "clock_after": deepcopy(candidate[MARKER_KEY]),
              "anchors": {name: {"before": before[name], "after": after[name]} for name in ANCHORS},
              "summary": {"changes_required": candidate != snapshot.state,
                          "has_active_quest": isinstance(quest, dict) and quest.get("active") is not None,
                          "unit_rows": len(units) if isinstance(units, list) else None,
                          "retained_history_sections": [name for name in HISTORIES if name in snapshot.state],
                          "service_must_be_stopped": True}}
    result["plan_sha256"] = _digest(result)
    return result, candidate


def plan_migration(database: Path, source_offset_minutes: int) -> dict:
    _offset(source_offset_minutes)
    path = database_path(database)
    try:
        with read_database(path) as (_, snapshot):
            result, _ = _build_plan(path, snapshot, source_offset_minutes)
            return result
    except ProtocolError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError) as error:
        raise ProtocolError("energy_migration_database_invalid", "Database could not be inspected safely", 503) from error


def inspect_database(database: Path) -> dict:
    result = {"path": None, "exists": False, "ready": False}
    try:
        path = database_path(database, existing=False)
        result = {"path": str(path), "exists": path.exists(), "ready": False}
        if not path.exists():
            return {**result, "ready": True, "status": "new_database"}
        path = database_path(path)
        with read_database(path) as (_, snapshot):
            validate_clock(snapshot.state)
        return {**result, "ready": True, "status": "ready"}
    except ProtocolError as error:
        status = ("migration_required" if error.code == CLOCK_SOURCE_REQUIRED else
                  "stop_service_required" if error.code == "energy_migration_database_busy" else "invalid")
        return {**result, "status": status, "error": error.code}
    except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError):
        return {**result, "status": "invalid", "error": "energy_migration_database_invalid"}


def _validated_plan(plan):
    try:
        if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
            raise ValueError("Plan fields are invalid")
        value = deepcopy(plan)
        if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["operation"] != "energy_clock_migration":
            raise ValueError("Plan schema is invalid")
        if not isinstance(value["database"], str) or not 0 < len(value["database"]) <= 32768:
            raise ValueError("Plan path is invalid")
        if any(not isinstance(value[name], str) or not _HASH.fullmatch(value[name]) for name in
               ("database_identity_sha256", "state_sha256", "account_identity_sha256", "candidate_sha256", "plan_sha256")):
            raise ValueError("Plan digest is invalid")
        if type(value["expected_revision"]) is not int or not 0 <= value["expected_revision"] <= MAX_REVISION:
            raise ValueError("Plan revision is invalid")
        _offset(value["source_offset_minutes"])
        if _digest({name: item for name, item in value.items() if name != "plan_sha256"}) != value["plan_sha256"]:
            raise ValueError("Plan digest does not match its contents")
        path = database_path(value["database"])
        if str(path) != value["database"]:
            raise ValueError("Plan database path is not canonical")
        return value, path
    except ProtocolError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise ProtocolError("energy_migration_plan_invalid", "Migration plan is malformed or changed", 400) from error


def _already_applied(path, snapshot, plan):
    if (snapshot.revision != plan["expected_revision"] + 1 or snapshot.file_identity != plan["database_identity_sha256"]
            or _digest(snapshot.state) != plan["candidate_sha256"] or _digest(snapshot.identity) != plan["account_identity_sha256"]):
        return False
    try:
        previous = deepcopy(snapshot.state)
        row = _anchor_row(previous)
        for name in ANCHORS:
            row[name] = plan["anchors"][name]["before"]
        if plan["clock_before"] is None:
            previous.pop(MARKER_KEY, None)
        else:
            previous[MARKER_KEY] = deepcopy(plan["clock_before"])
        reconstructed = replace(snapshot, state=previous, revision=plan["expected_revision"])
        expected, candidate = _build_plan(path, reconstructed, plan["source_offset_minutes"])
        return expected == plan and candidate == snapshot.state
    except (ProtocolError, ValueError, TypeError, KeyError):
        return False


def _result(path, snapshot, status, backup=None, backup_digest=None):
    result = {"status": status, "database": str(path), "revision": snapshot.revision,
              "state_sha256": _digest(snapshot.state), "backup": str(backup) if backup is not None else None}
    if backup is not None:
        result["backup_sha256"] = backup_digest
    return result


def _record_audit(connection, plan, current, committed, backup, backup_digest, result):
    op_id, created = plan["plan_sha256"], timestamp()
    ensure_admin_tables(connection)
    insert_backup(connection, kind=AUTO_BACKUP_KIND, reason=plan["operation"], op_id=op_id, path=backup,
                  sha256=backup_digest, revision=current.revision, created_at=created)
    record = {"kind": AUDIT_KIND, "plan_digest": op_id, "targets": list(AUDIT_TARGETS), "result": result,
              "operations": [{"operation": plan["operation"],
                              "params": {"source_offset_minutes": plan["source_offset_minutes"]}}],
              "diff": {name: plan[name] for name in AUDIT_DIFF_FIELDS}}
    insert_admin_operation(connection, op_id, admin_payload_digest(op_id, op_id), record,
                           revision_before=current.revision, revision_after=committed.revision,
                           state_sha256_before=plan["state_sha256"], state_sha256_after=plan["candidate_sha256"],
                           created_at=created)


def _backup(source, snapshot, directory):
    directory = checked_path(Path(directory))
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ValueError("Backup destination is not a directory")
    path = checked_path(directory / f"energy-clock-{uuid.uuid4().hex}.sqlite3", root=directory)
    with path.open("xb"):
        pass
    destination = sqlite3.connect(path, timeout=1, isolation_level=None)
    try:
        deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                _error("energy_migration_backup_failed", "The consistent backup did not complete in time")
        source.backup(destination, pages=BACKUP_PAGES, progress=progress, sleep=BACKUP_RETRY_SECONDS)
        # The copy inherits WAL mode, and read-only openers of a WAL file leave sidecars that block later reads.
        destination.execute("PRAGMA journal_mode=DELETE")
        if destination.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            _error("energy_migration_backup_failed", "The backup did not pass integrity validation")
        copied = _read_snapshot(destination, snapshot.file_identity)
        if copied != snapshot:
            _error("energy_migration_backup_failed", "The backup does not match the selected profile snapshot")
    finally:
        destination.close()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return path, digest


def apply_migration(plan: dict, *, preflight: Callable[[dict], None], backup_directory: Path) -> dict:
    value, path = _validated_plan(plan)
    if not callable(preflight):
        _error("energy_migration_preflight_invalid", "Migration requires a candidate preflight callback", 400)
    try:
        with read_database(path) as (source, snapshot):
            expected, candidate = _build_plan(path, snapshot, value["source_offset_minutes"])
            if expected != value:
                if _already_applied(path, snapshot, value):
                    return _result(path, snapshot, "already_applied")
                _error("energy_migration_stale", "Database no longer matches the exact migration plan", 409)
            checked = deepcopy(candidate)
            preflight(checked)
            if checked != candidate:
                _error("energy_migration_preflight_invalid", "Preflight must not change the migration candidate")
            if candidate == snapshot.state:
                return _result(path, snapshot, "unchanged")
            backup, backup_digest = _backup(source, snapshot, backup_directory)
        _offline(path)
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1, isolation_level=None)
        try:
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("BEGIN IMMEDIATE")
            current = _read_snapshot(connection, _file_identity(path))
            actual, _ = _build_plan(path, current, value["source_offset_minutes"])
            if actual != value:
                _error("energy_migration_stale", "Database changed after the backup was prepared", 409)
            encoded = serialize(candidate)
            updated = connection.execute(
                "UPDATE profiles SET state_json=?,revision=revision+1,updated_at=? WHERE id=1 AND revision=? AND state_json=?",
                (encoded, timestamp(), current.revision, current.raw_state))
            if updated.rowcount != 1:
                _error("energy_migration_stale", "Profile changed while applying the migration", 409)
            committed = _read_snapshot(connection, _file_identity(path))
            if (committed.state != candidate or committed.identity != current.identity
                    or committed.revision != current.revision + 1 or committed.file_identity != value["database_identity_sha256"]):
                _error("energy_migration_scope_invalid", "The migration did not preserve the expected profile identity")
            result = _result(path, committed, "applied", backup, backup_digest)
            _record_audit(connection, value, current, committed, backup, backup_digest, result)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return result
    except ProtocolError:
        raise
    except sqlite3.OperationalError as error:
        code = "energy_migration_database_busy" if "locked" in str(error).lower() else "energy_migration_database_invalid"
        raise ProtocolError(code, "Database migration could not acquire a safe offline transaction", 409) from error
    except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError) as error:
        raise ProtocolError("energy_migration_database_invalid", "Database migration failed without committing the candidate", 503) from error

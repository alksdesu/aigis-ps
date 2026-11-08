from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re

from .admin_commit import InitializerRestartCheck
from .admin_diff import DEFAULT_DIFF_LIMITS, diff_digest, diff_states, json_digest, pointer, render_diff
from .admin_offline import MAX_PATH_TEXT, offline_database, read_current, require_offline
from .energy_clock import instant
from .errors import ProtocolError
from .login_cooking_state import LEDGER_KEY, migrate_cooking, read_cooking
from .native_numbers import int32
from .storage import AUTO_BACKUP_KIND, MAX_REVISION, BackupSnapshot, Store, admin_payload_digest


PLAN_SCHEMA = 1
OPERATION = "login_cooking_migrate_offline"
AUDIT_KIND = "login_cooking_offline"
AUDIT_TARGETS = (pointer((LEDGER_KEY,)),)
BACKUP_REASON = "before offline login cooking migration"
COOKING_CURRENT = "admin_login_cooking_current"
PLAN_INVALID = "admin_cooking_plan_invalid"
PLAN_STALE = "admin_cooking_plan_stale"
PLAN_FIELDS = frozenset(("schema_version", "operation", "database", "expected_revision", "state_sha256", "player_id",
                         "border", "migrated_at", "cooking_before", "cooking_after", "target_state_sha256",
                         "diff_sha256", "diff", "plan_sha256"))
PLAN_DIGESTS = ("state_sha256", "target_state_sha256", "diff_sha256", "plan_sha256")
PLAN_OBJECTS = ("cooking_before", "cooking_after", "diff")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _require_callbacks(initializer, preflight) -> None:
    if not callable(initializer) or not callable(preflight):
        raise ProtocolError("admin_offline_callbacks_invalid",
                            "Offline cooking migration needs a state initializer and a preflight", 400)


def _stale(reason: str) -> ProtocolError:
    return ProtocolError(PLAN_STALE, f"{reason}; plan the migration again", 409)


def _migrated(state: dict, now: datetime, border: int, initializer, preflight) -> dict:
    migrated = migrate_cooking(state, now, border=border)
    if read_cooking(migrated) == read_cooking(state):
        raise ProtocolError(COOKING_CURRENT, "The cooking ledger already uses this border and needs no quota recount "
                            "this month", 409)
    # The next start initializes this save, so the plan proves it is accepted exactly as migrated.
    InitializerRestartCheck(initializer).check(migrated)
    checked = deepcopy(migrated)
    preflight(checked)
    if checked != migrated:
        raise ProtocolError("admin_offline_preflight_invalid", "Preflight must not change the migrated state", 503)
    return migrated


def _build_plan(database: Path, current: BackupSnapshot, now: datetime, border: int, migrated: dict) -> dict:
    if current.profile.revision >= MAX_REVISION:
        raise ProtocolError("admin_revision_exhausted", "The profile revision cannot be incremented", 409)
    state = current.profile.state
    entries = diff_states(state, migrated)
    plan = {"schema_version": PLAN_SCHEMA, "operation": OPERATION, "database": str(database),
            "expected_revision": current.profile.revision, "state_sha256": json_digest(state),
            "player_id": current.profile.player_id, "border": border, "migrated_at": now.isoformat(),
            "cooking_before": read_cooking(state), "cooking_after": read_cooking(migrated),
            "target_state_sha256": json_digest(migrated), "diff_sha256": diff_digest(entries),
            "diff": render_diff(entries, DEFAULT_DIFF_LIMITS)}
    plan["plan_sha256"] = json_digest(plan)
    return plan


def plan_cooking_migration(database: Path, *, border: int, now: datetime, initializer: Callable[[dict], dict],
                           preflight: Callable[[dict], None], checkpoint: bool = False) -> dict:
    _require_callbacks(initializer, preflight)
    moment = instant(now)
    path = offline_database(database)
    require_offline(path, checkpoint=checkpoint)
    current = read_current(path)
    migrated = _migrated(current.profile.state, moment, border, initializer, preflight)
    return _build_plan(path, current, moment, border, migrated)


def _validated_plan(plan) -> tuple[dict, Path, datetime]:
    try:
        if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
            raise ValueError("Plan fields are invalid")
        value = deepcopy(plan)
        if type(value["schema_version"]) is not int or value["schema_version"] != PLAN_SCHEMA or value["operation"] != OPERATION:
            raise ValueError("Plan schema is invalid")
        if not isinstance(value["database"], str) or not 0 < len(value["database"]) <= MAX_PATH_TEXT:
            raise ValueError("Plan database path is invalid")
        if any(not isinstance(value[name], str) or not _HASH.fullmatch(value[name]) for name in PLAN_DIGESTS):
            raise ValueError("Plan digests are invalid")
        if type(value["expected_revision"]) is not int or not 0 <= value["expected_revision"] <= MAX_REVISION:
            raise ValueError("Plan revision is invalid")
        if not isinstance(value["player_id"], str) or not value["player_id"]:
            raise ValueError("Plan account is invalid")
        if not int32(value["border"]) or value["border"] < 0:
            raise ValueError("Plan border is invalid")
        if any(not isinstance(value[name], dict) for name in PLAN_OBJECTS):
            raise ValueError("Plan ledgers are invalid")
        now = datetime.fromisoformat(value["migrated_at"]) if isinstance(value["migrated_at"], str) else None
        if now is None or now.utcoffset() is None or now.isoformat() != value["migrated_at"]:
            raise ValueError("Plan instant is invalid")
        if json_digest({name: item for name, item in value.items() if name != "plan_sha256"}) != value["plan_sha256"]:
            raise ValueError("Plan digest does not match its contents")
        path = offline_database(value["database"])
        if str(path) != value["database"]:
            raise ValueError("Plan database path is not canonical")
        return value, path, now
    except ProtocolError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise ProtocolError(PLAN_INVALID, "The cooking migration plan is malformed or was changed", 400) from error


def _outcome(status: str, path: Path, revision: int, state_sha256: str, backup: dict | None = None) -> dict:
    return {"status": status, "database": str(path), "revision": revision, "state_sha256": state_sha256,
            "backup_before_migration": backup}


def apply_cooking_migration(plan: dict, *, border: int, initializer: Callable[[dict], dict],
                            preflight: Callable[[dict], None], backup_directory: Path, checkpoint: bool = False) -> dict:
    _require_callbacks(initializer, preflight)
    value, path, now = _validated_plan(plan)
    if value["border"] != border:
        raise _stale("The resources now name a different cooking border")
    require_offline(path, checkpoint=checkpoint)
    current = read_current(path)
    op_id = value["plan_sha256"]
    applied = (current.profile.revision == value["expected_revision"] + 1
               and json_digest(current.profile.state) == value["target_state_sha256"])
    if applied and Store(path).find_admin_operation(op_id) is not None:
        return _outcome("already_applied", path, current.profile.revision, value["target_state_sha256"])
    if current.profile.revision != value["expected_revision"] or json_digest(current.profile.state) != value["state_sha256"]:
        raise _stale("The database changed after the plan was made")
    migrated = _migrated(current.profile.state, now, border, initializer, preflight)
    if _build_plan(path, current, now, border, migrated) != value:
        raise _stale("The migration differs from the plan")
    store = Store(path)
    backup = store.backup(backup_directory, BACKUP_REASON, op_id, kind=AUTO_BACKUP_KIND)
    record = {"kind": AUDIT_KIND, "plan_digest": op_id, "targets": list(AUDIT_TARGETS), "diff": value["diff"],
              "operations": [{"operation": OPERATION, "params": {"border": border, "migrated_at": value["migrated_at"]}}],
              "result": {"backup_before_migration": backup, "cooking_before": value["cooking_before"],
                         "cooking_after": value["cooking_after"]}}
    commit = store.commit_admin(op_id, admin_payload_digest(op_id, op_id), value["expected_revision"], migrated, record)
    operation = commit.operation
    return _outcome("already_applied" if commit.replayed else "applied", path, operation["revision_after"],
                    operation["state_sha256_after"], backup)

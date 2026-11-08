from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
import re

from .admin_commit import InitializerRestartCheck
from .admin_diff import DEFAULT_DIFF_LIMITS, diff_digest, diff_states, json_digest, render_diff
from .energy_clock import CLOCK_SOURCE_REQUIRED, validate_clock
from .errors import ProtocolError
from .local_io import checked_path
from .storage import (AUTO_BACKUP_KIND, MAX_REVISION, SIDECAR_SUFFIXES, BackupSnapshot, Store, admin_payload_digest,
                      checkpoint_idle, read_backup)


PLAN_SCHEMA = 1
OPERATION = "admin_restore_offline"
AUDIT_KIND = "restore_offline"
AUDIT_TARGETS = ("/",)
BACKUP_REASON = "before offline restore"
CROSS_ACCOUNT_PHRASE = "restore another account"
RESTORE_CLOCK_REQUIRED = "admin_restore_clock_required"
RESTORE_CLOCK_MESSAGE = ("Convert the backup's energy clock with energy-clock-plan and energy-clock-apply "
                         "before restoring it")
MAX_PATH_TEXT = 32768
PLAN_FIELDS = frozenset(("schema_version", "operation", "database", "expected_revision", "state_sha256", "player_id",
                         "backup", "backup_sha256", "backup_revision", "backup_player_id", "target_state_sha256",
                         "diff_sha256", "diff", "cross_account", "plan_sha256"))
PLAN_DIGESTS = ("state_sha256", "backup_sha256", "target_state_sha256", "diff_sha256", "plan_sha256")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _require_callbacks(initializer, preflight) -> None:
    if not callable(initializer) or not callable(preflight):
        raise ProtocolError("admin_offline_callbacks_invalid", "Offline restore needs a state initializer and a preflight", 400)


def offline_database(value) -> Path:
    try:
        path = checked_path(Path(value))
        if not path.is_file():
            raise ValueError("Missing database")
    except (OSError, ValueError, TypeError) as error:
        raise ProtocolError("admin_offline_path_invalid", "Offline commands need the existing local database file", 400) from error
    return path


def require_offline(path: Path, *, checkpoint: bool) -> None:
    if not any(Path(f"{path}{suffix}").exists() for suffix in SIDECAR_SUFFIXES):
        return
    if not checkpoint:
        raise ProtocolError("admin_offline_database_busy", "Journal files beside the database mean it may still be open; "
                            "stop the server, then retry with the checkpoint option", 409)
    if not checkpoint_idle(path):
        raise ProtocolError("admin_offline_database_busy", "The database is still in use; stop the server first", 409)


def read_current(path: Path) -> BackupSnapshot:
    try:
        return read_backup(path)
    except ProtocolError as error:
        raise ProtocolError("admin_offline_database_invalid", "The local database could not be read safely", 503) from error


def _read_target(value, database: Path) -> BackupSnapshot:
    return read_backup(Path(value), root=database.parent)


def require_clock_marker(state: dict, refusal: type[ProtocolError] = ProtocolError) -> None:
    try:
        validate_clock(state)
    except ProtocolError as error:
        if error.code != CLOCK_SOURCE_REQUIRED:
            raise
        raise refusal(RESTORE_CLOCK_REQUIRED, RESTORE_CLOCK_MESSAGE, 409) from error


def _validated_target(snapshot: BackupSnapshot, initializer, preflight) -> dict:
    state = snapshot.profile.state
    require_clock_marker(state)
    InitializerRestartCheck(initializer).check(state)
    checked = deepcopy(state)
    preflight(checked)
    if checked != state:
        raise ProtocolError("admin_offline_preflight_invalid", "Preflight must not change the restore target", 503)
    return state


def _build_plan(database: Path, current: BackupSnapshot, target: BackupSnapshot) -> dict:
    before, after = json_digest(current.profile.state), json_digest(target.profile.state)
    if before != after and current.profile.revision >= MAX_REVISION:
        raise ProtocolError("admin_revision_exhausted", "The profile revision cannot be incremented", 409)
    entries = diff_states(current.profile.state, target.profile.state)
    plan = {"schema_version": PLAN_SCHEMA, "operation": OPERATION, "database": str(database),
            "expected_revision": current.profile.revision, "state_sha256": before,
            "player_id": current.profile.player_id, "backup": str(target.path), "backup_sha256": target.sha256,
            "backup_revision": target.profile.revision, "backup_player_id": target.profile.player_id,
            "target_state_sha256": after, "diff_sha256": diff_digest(entries),
            "diff": render_diff(entries, DEFAULT_DIFF_LIMITS),
            "cross_account": current.profile.player_id != target.profile.player_id}
    plan["plan_sha256"] = json_digest(plan)
    return plan


def plan_restore(database: Path, backup: Path, *, initializer: Callable[[dict], dict],
                 preflight: Callable[[dict], None], checkpoint: bool = False) -> dict:
    _require_callbacks(initializer, preflight)
    path = offline_database(database)
    require_offline(path, checkpoint=checkpoint)
    current, target = read_current(path), _read_target(backup, path)
    _validated_target(target, initializer, preflight)
    return _build_plan(path, current, target)


def _validated_plan(plan) -> tuple[dict, Path]:
    try:
        if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
            raise ValueError("Plan fields are invalid")
        value = deepcopy(plan)
        if type(value["schema_version"]) is not int or value["schema_version"] != PLAN_SCHEMA or value["operation"] != OPERATION:
            raise ValueError("Plan schema is invalid")
        if any(not isinstance(value[name], str) or not 0 < len(value[name]) <= MAX_PATH_TEXT for name in ("database", "backup")):
            raise ValueError("Plan paths are invalid")
        if any(not isinstance(value[name], str) or not _HASH.fullmatch(value[name]) for name in PLAN_DIGESTS):
            raise ValueError("Plan digests are invalid")
        if any(type(value[name]) is not int or not 0 <= value[name] <= MAX_REVISION
               for name in ("expected_revision", "backup_revision")):
            raise ValueError("Plan revisions are invalid")
        if any(not isinstance(value[name], str) or not value[name] for name in ("player_id", "backup_player_id")):
            raise ValueError("Plan accounts are invalid")
        if type(value["cross_account"]) is not bool or value["cross_account"] != (value["player_id"] != value["backup_player_id"]):
            raise ValueError("Plan account comparison is inconsistent")
        if not isinstance(value["diff"], dict):
            raise ValueError("Plan diff is invalid")
        if json_digest({name: item for name, item in value.items() if name != "plan_sha256"}) != value["plan_sha256"]:
            raise ValueError("Plan digest does not match its contents")
        path = offline_database(value["database"])
        if str(path) != value["database"]:
            raise ValueError("Plan database path is not canonical")
        return value, path
    except ProtocolError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise ProtocolError("admin_restore_plan_invalid", "The restore plan is malformed or was changed", 400) from error


def _outcome(status: str, path: Path, revision: int, state_sha256: str, backup: dict | None = None) -> dict:
    return {"status": status, "database": str(path), "revision": revision, "state_sha256": state_sha256,
            "backup_before_restore": backup}


def apply_restore(plan: dict, *, initializer: Callable[[dict], dict], preflight: Callable[[dict], None],
                  backup_directory: Path, confirm: str | None = None, checkpoint: bool = False) -> dict:
    _require_callbacks(initializer, preflight)
    value, path = _validated_plan(plan)
    if value["cross_account"] and confirm != CROSS_ACCOUNT_PHRASE:
        raise ProtocolError("admin_restore_confirmation_required",
                            f'Restoring another account\'s backup needs the confirmation phrase "{CROSS_ACCOUNT_PHRASE}"', 409)
    require_offline(path, checkpoint=checkpoint)
    current = read_current(path)
    op_id = value["plan_sha256"]
    changed = value["state_sha256"] != value["target_state_sha256"]
    advanced = current.profile.revision - value["expected_revision"]
    # Saves that committed an unchanged restore with a new revision still count it as applied.
    restored = ((advanced == 1 or (advanced == 0 and not changed))
                and json_digest(current.profile.state) == value["target_state_sha256"])
    if restored and Store(path).find_admin_operation(op_id) is not None:
        return _outcome("already_applied", path, current.profile.revision, value["target_state_sha256"])
    target = _read_target(value["backup"], path)
    if _build_plan(path, current, target) != value:
        raise ProtocolError("admin_restore_stale",
                            "The database or the backup changed after the plan was made; plan the restore again", 409)
    state = _validated_target(target, initializer, preflight)
    store = Store(path)
    # Only one automatic backup is kept, so a restore that leaves the state as it is must not replace it.
    backup = store.backup(backup_directory, BACKUP_REASON, op_id, kind=AUTO_BACKUP_KIND) if changed else None
    record = {"kind": AUDIT_KIND, "plan_digest": op_id, "targets": list(AUDIT_TARGETS), "diff": value["diff"],
              "operations": [{"operation": OPERATION, "params": {
                  "backup": value["backup"], "backup_sha256": value["backup_sha256"],
                  "backup_revision": value["backup_revision"], "cross_account": value["cross_account"]}}],
              "result": {"backup_before_restore": backup}}
    commit = store.commit_admin(op_id, admin_payload_digest(op_id, op_id), value["expected_revision"],
                                state if changed else None, record)
    operation = commit.operation
    return _outcome("already_applied" if commit.replayed else "applied", path, operation["revision_after"],
                    operation["state_sha256_after"], backup)

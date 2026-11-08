from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from aigis_ps.admin_command import AdminError
from aigis_ps.admin_offline import (AUDIT_KIND, AUDIT_TARGETS, CROSS_ACCOUNT_PHRASE, OPERATION, RESTORE_CLOCK_MESSAGE,
                                    RESTORE_CLOCK_REQUIRED, apply_restore, plan_restore, require_clock_marker)
from aigis_ps.energy_clock import CLOCK_SOURCE_REQUIRED, mark_new
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import AUTO_BACKUP_KIND, MAX_REVISION, Store, admin_payload_digest, read_backup


NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
DATABASE = "aigis.sqlite3"
PATH_INVALID = "admin_offline_path_invalid"
MISSING_DATABASE = "Offline commands need the existing local database file"


def clocked(note):
    return mark_new({"bootstrap_schema": 1, "tables": {"game_param": [{"Money": 100}]}, "note": note}, NOW)


def unchanged(state):
    return state


def accept(state):
    return None


def error_of(action):
    with pytest.raises(ProtocolError) as caught:
        action()
    return caught.value.code, caught.value.status


def plan(data, backup, **options):
    options.setdefault("initializer", unchanged)
    options.setdefault("preflight", accept)
    return plan_restore(data / DATABASE, backup, **options)


def apply(data, value, **options):
    options.setdefault("initializer", unchanged)
    options.setdefault("preflight", accept)
    return apply_restore(value, backup_directory=data / "backups", **options)


def automatic(store):
    return [item for item in store.list_backups().items if item["kind"] == AUTO_BACKUP_KIND]


@pytest.fixture
def setup(tmp_path):
    data = tmp_path / "data"
    store = Store(data / DATABASE, clocked("backup"))
    backup = Path(store.backup(data / "backups", "manual snapshot", kind="manual")["path"])
    store.update_state(clocked("current"), store.profile().revision)
    return data, store, backup


def test_plan_and_apply_restore_the_backup_state(setup):
    data, store, backup = setup
    before = store.profile()
    value = plan(data, backup)
    assert value["expected_revision"] == before.revision and value["backup"] == str(backup)
    assert value["cross_account"] is False and value["diff"]["entries"]
    result = apply(data, value)
    after = store.profile()
    assert result["status"] == "applied" and result["revision"] == after.revision == before.revision + 1
    assert after.state == clocked("backup")
    operation = store.find_admin_operation(value["plan_sha256"])
    assert operation["kind"] == "restore_offline" and operation["revision_before"] == before.revision
    [snapshot] = automatic(store)
    assert snapshot["op_id"] == value["plan_sha256"] and snapshot["revision"] == before.revision
    assert read_backup(Path(snapshot["path"]), root=data).profile.state == before.state
    assert result["backup_before_restore"] == operation["result"]["backup_before_restore"] == snapshot


def test_applying_the_same_plan_again_reports_it_as_already_applied(setup):
    data, store, backup = setup
    value = plan(data, backup)
    apply(data, value)
    revision = store.profile().revision
    assert apply(data, value)["status"] == "already_applied"
    assert store.profile().revision == revision and len(automatic(store)) == 1


def test_restoring_the_current_state_writes_only_the_audit(setup):
    data, store, _ = setup
    current = Path(store.backup(data / "backups", "current snapshot", kind="manual")["path"])
    before, files = store.profile(), set((data / "backups").iterdir())
    value = plan(data, current)
    assert value["state_sha256"] == value["target_state_sha256"] and value["diff"]["entries"] == []
    result = apply(data, value)
    assert (result["status"], result["revision"], result["backup_before_restore"]) == ("applied", before.revision, None)
    [operation] = store.list_admin_operations().items
    assert (operation["op_id"], operation["revision_before"], operation["revision_after"]) == (
        value["plan_sha256"], before.revision, before.revision)
    assert operation["result"] == {"backup_before_restore": None}
    assert apply(data, value)["status"] == "already_applied"
    assert store.profile() == before and len(store.list_admin_operations().items) == 1
    assert set((data / "backups").iterdir()) == files and automatic(store) == []


def test_unchanged_restores_committed_with_a_new_revision_stay_applied(setup):
    data, store, _ = setup
    current = Path(store.backup(data / "backups", "current snapshot", kind="manual")["path"])
    value = plan(data, current)
    op_id = value["plan_sha256"]
    record = {"kind": AUDIT_KIND, "plan_digest": op_id, "targets": list(AUDIT_TARGETS), "diff": value["diff"],
              "operations": [{"operation": OPERATION, "params": {}}], "result": {"backup_before_restore": None}}
    store.commit_admin(op_id, admin_payload_digest(op_id, op_id), value["expected_revision"], store.profile().state,
                       record)
    before = store.profile()
    assert before.revision == value["expected_revision"] + 1
    result = apply(data, value)
    assert (result["status"], result["revision"], result["backup_before_restore"]) == (
        "already_applied", before.revision, None)
    assert store.profile() == before and automatic(store) == []


def test_only_restores_that_change_the_state_need_a_revision_left(setup):
    data, store, backup = setup
    current = Path(store.backup(data / "backups", "current snapshot", kind="manual")["path"])
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE profiles SET revision=? WHERE id=1", (MAX_REVISION,))
    connection.close()
    assert error_of(lambda: plan(data, backup)) == ("admin_revision_exhausted", 409)
    result = apply(data, plan(data, current))
    assert (result["status"], result["revision"], result["backup_before_restore"]) == ("applied", MAX_REVISION, None)
    assert store.profile().revision == MAX_REVISION and automatic(store) == []


def test_journal_files_block_until_a_checkpoint_is_requested(setup):
    data, store, backup = setup
    for suffix in ("-wal", "-shm"):
        Path(f"{data / DATABASE}{suffix}").write_bytes(b"")
    assert error_of(lambda: plan(data, backup)) == ("admin_offline_database_busy", 409)
    value = plan(data, backup, checkpoint=True)
    assert not any(Path(f"{data / DATABASE}{suffix}").exists() for suffix in ("-wal", "-shm"))
    assert apply(data, value)["status"] == "applied"


def test_a_database_in_use_stays_busy_even_with_a_checkpoint(setup):
    data, store, backup = setup
    reader = sqlite3.connect(data / DATABASE, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT revision FROM profiles").fetchone()
        assert error_of(lambda: plan(data, backup, checkpoint=True)) == ("admin_offline_database_busy", 409)
    finally:
        reader.close()


def test_a_database_changed_after_planning_invalidates_the_plan(setup):
    data, store, backup = setup
    value = plan(data, backup)
    store.update_state(clocked("later"), store.profile().revision)
    revision = store.profile().revision
    assert error_of(lambda: apply(data, value)) == ("admin_restore_stale", 409)
    assert store.profile().revision == revision and automatic(store) == []


def test_another_accounts_backup_needs_the_confirmation_phrase(setup):
    data, store, _ = setup
    other = Store(data / "other" / DATABASE, clocked("other account"))
    with sqlite3.connect(other.path) as connection:
        connection.execute("UPDATE profiles SET player_id='2' WHERE id=1")
    connection.close()
    backup = Path(other.backup(data / "other" / "backups", "other account", kind="manual")["path"])
    value = plan(data, backup)
    assert value["cross_account"] is True and value["backup_player_id"] == "2"
    assert error_of(lambda: apply(data, value)) == ("admin_restore_confirmation_required", 409)
    assert error_of(lambda: apply(data, value, confirm="yes")) == ("admin_restore_confirmation_required", 409)
    assert apply(data, value, confirm=CROSS_ACCOUNT_PHRASE)["status"] == "applied"
    profile = store.profile()
    assert profile.state == clocked("other account") and profile.player_id == "1"


def test_restart_incompatible_targets_and_failed_preflights_write_nothing(setup):
    data, store, backup = setup
    revision = store.profile().revision
    assert error_of(lambda: plan(data, backup, initializer=lambda state: {**state, "extra": True})) == (
        "admin_restart_incompatible", 409)
    value = plan(data, backup)

    def failing(state):
        raise ProtocolError("admin_preflight_failed", "The present box response is too large", 409)

    def mutating(state):
        state["note"] = "changed"

    assert error_of(lambda: apply(data, value, preflight=failing)) == ("admin_preflight_failed", 409)
    assert error_of(lambda: apply(data, value, preflight=mutating)) == ("admin_offline_preflight_invalid", 503)
    assert error_of(lambda: apply(data, value, initializer=lambda state: 1 / 0)) == ("admin_restart_incompatible", 409)
    assert store.profile().revision == revision and automatic(store) == []


def test_backups_without_an_energy_clock_must_be_converted_first(tmp_path):
    data = tmp_path / "data"
    store = Store(data / DATABASE, {"bootstrap_schema": 1, "tables": {"game_param": [{}]}})
    backup = Path(store.backup(data / "backups", "legacy", kind="manual")["path"])
    assert error_of(lambda: plan(data, backup)) == (RESTORE_CLOCK_REQUIRED, 409)


@pytest.mark.parametrize("refusal", [ProtocolError, AdminError])
def test_online_and_offline_restores_refuse_unconverted_clocks_alike(refusal):
    with pytest.raises(refusal) as caught:
        require_clock_marker({"bootstrap_schema": 1, "tables": {"game_param": [{}]}}, refusal)
    assert type(caught.value) is refusal and caught.value.__cause__.code == CLOCK_SOURCE_REQUIRED
    assert (caught.value.code, str(caught.value), caught.value.status) == (
        "admin_restore_clock_required", RESTORE_CLOCK_MESSAGE, 409)


@pytest.mark.parametrize("refusal", [ProtocolError, AdminError])
def test_restores_pass_other_clock_failures_through(refusal):
    state = {**clocked("broken"), "energy_clock": None}
    with pytest.raises(ProtocolError) as caught:
        require_clock_marker(state, refusal)
    assert type(caught.value) is ProtocolError
    assert (caught.value.code, caught.value.status) == ("energy_clock_invalid", 503)
    require_clock_marker(clocked("marked"), refusal)


def test_tampered_plans_fail_closed(setup):
    data, store, backup = setup
    value = plan(data, backup)
    for change in ({"expected_revision": value["expected_revision"] + 1}, {"cross_account": True},
                   {"backup": str(data / "elsewhere.sqlite3")}, {"extra": 1}):
        assert error_of(lambda: apply(data, {**value, **change})) == ("admin_restore_plan_invalid", 400)
    assert error_of(lambda: apply(data, {key: item for key, item in value.items() if key != "diff"})) == (
        "admin_restore_plan_invalid", 400)


def test_missing_or_foreign_files_are_rejected(setup, tmp_path):
    data, store, backup = setup
    value = plan(data, backup)
    backup.unlink()
    assert error_of(lambda: apply(data, value)) == ("admin_backup_path_invalid", 400)
    outside = Path(Store(tmp_path / "outside" / DATABASE, clocked("outside")).backup(
        tmp_path / "outside" / "backups", "outside", kind="manual")["path"])
    assert error_of(lambda: plan(data, outside)) == ("admin_backup_path_invalid", 400)
    with pytest.raises(ProtocolError) as missing:
        plan_restore(data / "missing.sqlite3", backup, initializer=unchanged, preflight=accept)
    assert (missing.value.code, missing.value.status, str(missing.value)) == (PATH_INVALID, 400, MISSING_DATABASE)
    assert error_of(lambda: plan(data, backup, initializer=None)) == ("admin_offline_callbacks_invalid", 400)

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from aigis_ps.energy_clock import anchor_seconds, convert_state, energy_seconds, mark_new, set_anchor, validate_clock
from aigis_ps.energy_migration import ANCHORS, apply_migration, inspect_database, plan_migration
from aigis_ps.errors import ProtocolError
from aigis_ps.local_io import MAX_JSON_DEPTH
from aigis_ps.storage import (
    ADMIN_ACTOR, ADMIN_TABLES, AUTO_BACKUP_KIND, SIDECAR_SUFFIXES, Store, admin_payload_digest,
    read_backup, serialize,
)
from test_storage import NONFINITE, SCANNER_DEPTH, nested


@pytest.fixture
def case(tmp_path):
    row = {"NowCharisma": 10, "NowActionPoint": 2, "Money": 100, "Experience": 0}
    set_anchor(row, 739555 * 86400 + 23 * 3600 + 59 * 60 + 59)
    set_anchor(row, 739555 * 86400 + 12 * 3600 + 17, "ActionPoint")
    state = {"bootstrap_schema": 1, "tables": {"game_param": [row], "presents": [{"opaque": 1}]},
             "player_state": {"units": [{"opaque": 2}]},
             "quest_state": {"active": {"unit_reservations": [{"card_id": 3, "level": 1, "count": 1}]},
                             "history": [{"response": "never-print-this-response"}], "last_client_time": 123},
             "recovery_history": {"receipts": [{"response": "another-private-response"}], "seen_keys": ["receipt-key"]},
             "custom_progress": {"keep": [1, 2, 3]}}
    path = tmp_path / "aigis.sqlite3"
    store = Store(path, state)
    return SimpleNamespace(path=path, store=store, state=state, backups=tmp_path / "backups")


def files(path):
    return {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in path.parent.iterdir() if item.is_file()}


def backup_state(path):
    connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    try:
        return json.loads(connection.execute("SELECT state_json FROM profiles WHERE id=1").fetchone()[0])
    finally:
        connection.close()


def update(case, transform):
    before = case.store.profile()
    state = deepcopy(before.state)
    transform(state)
    return case.store.update_state(state, before.revision)


def fail_unchanged(case, action, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as error:
        action()
    assert error.value.code == code
    assert case.store.profile() == before


def resign(plan):
    plan["plan_sha256"] = hashlib.sha256(serialize({key: value for key, value in plan.items()
                                                  if key != "plan_sha256"}).encode()).hexdigest()
    return plan


def test_plan_is_read_only_and_contains_no_private_state(case):
    before, identity = files(case.path), case.store.profile()
    plan = plan_migration(case.path, 480)
    assert files(case.path) == before
    assert case.store.profile() == identity
    encoded = json.dumps(plan)
    for value in (identity.user_key, identity.method_key, "never-print-this-response", "another-private-response", "receipt-key"):
        assert value not in encoded
    assert "state_json" not in encoded
    assert set(plan["anchors"]) == set(ANCHORS)
    assert plan["expected_revision"] == identity.revision
    assert plan["summary"]["has_active_quest"] is True
    assert plan["summary"]["changes_required"] is True
    assert plan["database"] == str(case.path.resolve())


def test_apply_preserves_all_history_and_creates_exact_backup(case):
    plan, seen = plan_migration(case.path, 480), []
    before = case.store.profile()
    def preflight(state):
        validate_clock(state)
        seen.append(deepcopy(state))
    result = apply_migration(plan, preflight=preflight, backup_directory=case.backups)
    after = case.store.profile()
    assert result["status"] == "applied" and result["revision"] == before.revision + 1
    assert after.state == convert_state(before.state, 480) == seen[0]
    assert after.user_key == before.user_key and after.method_key == before.method_key
    assert after.state["quest_state"] == before.state["quest_state"]
    assert after.state["recovery_history"] == before.state["recovery_history"]
    assert after.state["player_state"] == before.state["player_state"]
    assert backup_state(result["backup"]) == before.state
    assert hashlib.sha256(Path(result["backup"]).read_bytes()).hexdigest() == result["backup_sha256"]
    old_row, new_row = before.state["tables"]["game_param"][0], after.state["tables"]["game_param"][0]
    assert new_row["CharismaChangeDate"] == old_row["CharismaChangeDate"] + 1
    for prefix in ("Charisma", "ActionPoint"):
        assert anchor_seconds(new_row, prefix) - anchor_seconds(old_row, prefix) == 3600
    assert new_row["NowCharisma"] == 10 and new_row["NowActionPoint"] == 2


def test_old_plan_reapply_is_idempotent_without_new_backup_or_preflight(case):
    plan = plan_migration(case.path, 480)
    apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)
    before, backups = case.store.profile(), list(case.backups.iterdir())
    def should_not_run(state):
        raise AssertionError("Already-applied plan must not repeat the action")
    result = apply_migration(plan, preflight=should_not_run, backup_directory=case.backups)
    assert result["status"] == "already_applied" and result["backup"] is None
    assert case.store.profile() == before and list(case.backups.iterdir()) == backups


def test_marked_jst_plan_is_noop_and_does_not_create_backup_directory(case):
    update(case, lambda state: state.update(mark_new(state, datetime(2026, 11, 1, 1, tzinfo=timezone.utc))))
    plan = plan_migration(case.path, 480)
    assert plan["summary"]["changes_required"] is False
    before = case.store.profile()
    result = apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)
    assert result["status"] == "unchanged" and not case.backups.exists()
    assert case.store.profile() == before


@pytest.mark.parametrize("offset", [480, 540, 0, -480, 840, -840])
def test_explicit_source_offsets_use_shared_clock_conversion(case, offset):
    walls = (datetime(2026, 11, 1, 23, 59, 59), datetime(2026, 11, 1, 12, 0, 17))
    expected = {}
    def old_producer(state):
        row = state["tables"]["game_param"][0]
        for prefix, wall in zip(("Charisma", "ActionPoint"), walls):
            midnight = wall.replace(hour=0, minute=0, second=0)
            epoch_seconds = int((midnight - datetime(1970, 1, 1)).total_seconds()) + 9 * 3600 - offset * 60
            source_day = int(epoch_seconds / 86400) + 719162
            set_anchor(row, source_day * 86400 + wall.hour * 3600 + wall.minute * 60 + wall.second, prefix)
            expected[prefix] = energy_seconds(wall.replace(tzinfo=timezone(timedelta(minutes=offset))))
    update(case, old_producer)
    plan = plan_migration(case.path, offset)
    apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)
    after = case.store.profile().state
    for prefix, seconds in expected.items():
        assert anchor_seconds(after["tables"]["game_param"][0], prefix) == seconds


@pytest.mark.parametrize("offset", [None, True, "480", 480.0, 841, -841])
def test_source_must_be_explicit_bounded_integer(case, offset):
    fail_unchanged(case, lambda: plan_migration(case.path, offset), "energy_clock_source_invalid")


def test_zero_unknown_anchor_is_not_guessed(case):
    update(case, lambda state: set_anchor(state["tables"]["game_param"][0], 0))
    fail_unchanged(case, lambda: plan_migration(case.path, 480), "energy_clock_source_required")


@pytest.mark.parametrize("offset", [480, 840])
def test_legacy_epoch_day_ambiguity_is_not_silently_migrated(case, offset):
    update(case, lambda state: set_anchor(state["tables"]["game_param"][0], 719162 * 86400 + 43200))
    fail_unchanged(case, lambda: plan_migration(case.path, offset), "energy_clock_source_ambiguous")


def test_missing_database_inspection_does_not_create_anything(tmp_path):
    path = tmp_path / "missing" / "aigis.sqlite3"
    result = inspect_database(path)
    assert result["status"] == "new_database" and result["ready"] is True and result["exists"] is False
    assert not path.parent.exists()


def test_inspection_distinguishes_unmarked_marked_and_invalid_marker(case):
    assert inspect_database(case.path)["status"] == "migration_required"
    assert inspect_database(case.path)["ready"] is False
    plan = plan_migration(case.path, 480)
    apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)
    assert inspect_database(case.path)["status"] == "ready"
    update(case, lambda state: state["energy_clock"].update(utc_offset_minutes=480))
    invalid = inspect_database(case.path)
    assert invalid["ready"] is False and invalid["error"] == "energy_clock_invalid"


def test_inspection_never_creates_wal_sidecars(case):
    before = files(case.path)
    assert inspect_database(case.path)["status"] == "migration_required"
    assert files(case.path) == before


@pytest.mark.parametrize("pragma,value", [("application_id", 0), ("user_version", 2)])
def test_other_application_or_schema_is_rejected(case, pragma, value):
    connection = sqlite3.connect(case.path)
    try:
        connection.execute(f"PRAGMA {pragma}={value}")
        connection.commit()
    finally:
        connection.close()
    before = files(case.path)
    with pytest.raises(ProtocolError) as error:
        plan_migration(case.path, 480)
    assert error.value.code == "energy_migration_database_invalid"
    assert files(case.path) == before
    assert inspect_database(case.path)["ready"] is False


@pytest.mark.parametrize("value", [*NONFINITE, "[" * MAX_JSON_DEPTH + "]" * MAX_JSON_DEPTH,
                                   "[" * SCANNER_DEPTH + "]" * SCANNER_DEPTH],
                         ids=[*NONFINITE, "nested", "recursion"])
def test_unsafe_saved_json_is_refused_by_every_entry_point_without_writing(case, value):
    plan = plan_migration(case.path, 480)
    connection = sqlite3.connect(case.path)
    try:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1",
                           (serialize(case.state)[:-1] + ',"unsafe":' + value + "}",))
        connection.commit()
    finally:
        connection.close()
    before = files(case.path)
    for action in (lambda: plan_migration(case.path, 480),
                   lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert (error.value.code, error.value.status) == ("energy_migration_database_invalid", 503)
    inspected = inspect_database(case.path)
    assert (inspected["status"], inspected["error"]) == ("invalid", "energy_migration_database_invalid")
    assert files(case.path) == before and not case.backups.exists()


def test_the_deepest_permitted_save_still_migrates_and_passes_the_startup_check(case):
    deepest = nested(MAX_JSON_DEPTH - 1)
    update(case, lambda state: state.update(deep=deepest))
    result = apply_migration(plan_migration(case.path, 480), preflight=validate_clock, backup_directory=case.backups)
    assert result["status"] == "applied" and read_backup(Path(result["backup"])).profile.state["deep"] == deepest
    assert inspect_database(case.path)["status"] == "ready" and case.store.profile().state["deep"] == deepest


def test_existing_wal_requires_stopped_service_without_reading_stale_main_file(case):
    connection = sqlite3.connect(case.path, isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE profiles SET updated_at=updated_at WHERE id=1")
        assert Path(str(case.path) + "-wal").exists()
        with pytest.raises(ProtocolError) as error:
            plan_migration(case.path, 480)
        assert error.value.code == "energy_migration_database_busy"
        assert inspect_database(case.path)["status"] == "stop_service_required"
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize("mutation", ["extra", "missing", "bad-hash", "bool-version", "bool-revision"])
def test_malformed_plan_is_rejected_before_backup(case, mutation):
    plan = plan_migration(case.path, 480)
    if mutation == "extra":
        plan["unapproved"] = True
    elif mutation == "missing":
        del plan["anchors"]
    elif mutation == "bad-hash":
        plan["plan_sha256"] = "0" * 64
    elif mutation == "bool-version":
        plan["schema_version"] = True
    else:
        plan["expected_revision"] = True
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "energy_migration_plan_invalid")
    assert not case.backups.exists()


@pytest.mark.parametrize("field", ["state_sha256", "candidate_sha256", "account_identity_sha256", "database_identity_sha256",
                                   "expected_revision", "anchors", "summary", "clock_after", "source_offset_minutes"])
def test_rehashed_but_inconsistent_plan_is_still_rejected(case, field):
    plan = plan_migration(case.path, 480)
    if field.endswith("sha256"):
        plan[field] = "f" * 64
    elif field == "expected_revision":
        plan[field] += 1
    elif field == "anchors":
        plan[field]["CharismaChangeDate"]["after"] += 1
    elif field == "summary":
        plan[field]["unit_rows"] = 999
    elif field == "clock_after":
        plan[field]["origin"] = "new_local"
    else:
        plan[field] = 540
    resign(plan)
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "energy_migration_stale")
    assert not case.backups.exists()


def test_new_revision_or_changed_full_state_invalidates_plan(case):
    plan = plan_migration(case.path, 480)
    update(case, lambda state: state["custom_progress"].update(extra=4))
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "energy_migration_stale")


def test_full_state_change_without_revision_also_invalidates_plan(case):
    plan = plan_migration(case.path, 480)
    saved = case.store.profile().state
    saved["custom_progress"]["keep"].append(4)
    connection = sqlite3.connect(case.path)
    try:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (serialize(saved),))
        connection.commit()
    finally:
        connection.close()
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "energy_migration_stale")


def test_account_change_without_revision_invalidates_plan(case):
    plan = plan_migration(case.path, 480)
    connection = sqlite3.connect(case.path)
    try:
        connection.execute("UPDATE profiles SET method_key='test-replacement-key' WHERE id=1")
        connection.commit()
    finally:
        connection.close()
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "energy_migration_stale")


def test_path_replacement_with_identical_bytes_invalidates_plan(case, tmp_path):
    plan = plan_migration(case.path, 480)
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(case.path.read_bytes())
    os.replace(replacement, case.path)
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "energy_migration_stale")


def test_failing_preflight_leaves_no_backup_or_state_changes(case):
    def fail(state):
        raise ProtocolError("fixture_capacity", "Native response cannot fit", 503)
    plan = plan_migration(case.path, 480)
    fail_unchanged(case, lambda: apply_migration(plan, preflight=fail, backup_directory=case.backups), "fixture_capacity")
    assert not case.backups.exists()


def test_mutating_preflight_is_rejected(case):
    def mutate(state):
        state["tables"]["game_param"][0]["NowCharisma"] = 20
    plan = plan_migration(case.path, 480)
    fail_unchanged(case, lambda: apply_migration(plan, preflight=mutate, backup_directory=case.backups),
                   "energy_migration_preflight_invalid")
    assert not case.backups.exists()


def test_conversion_cannot_modify_unrelated_state(case, monkeypatch):
    def wrong(state, offset):
        candidate = convert_state(state, offset)
        candidate["custom_progress"] = {}
        return candidate
    monkeypatch.setattr("aigis_ps.energy_migration.convert_state", wrong)
    fail_unchanged(case, lambda: plan_migration(case.path, 480), "energy_migration_scope_invalid")


def test_file_change_while_planning_is_rejected(case, monkeypatch):
    import aigis_ps.energy_migration as module
    original = module._convert
    def touched(snapshot, offset):
        result = original(snapshot, offset)
        stat = case.path.stat()
        os.utime(case.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        return result
    monkeypatch.setattr(module, "_convert", touched)
    fail_unchanged(case, lambda: plan_migration(case.path, 480), "energy_migration_stale")


def test_backup_failure_does_not_enter_write_transaction(case, monkeypatch):
    plan = plan_migration(case.path, 480)
    def broken(*args):
        raise ProtocolError("energy_migration_backup_failed", "Backup failed", 503)
    monkeypatch.setattr("aigis_ps.energy_migration._backup", broken)
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "energy_migration_backup_failed")


def test_consistent_backup_includes_request_log_without_changing_it(case):
    case.store.record_request(None, "test-route", 10, 20, 200)
    before = case.store.recent_requests()
    result = apply_migration(plan_migration(case.path, 480), preflight=validate_clock, backup_directory=case.backups)
    connection = sqlite3.connect(Path(result["backup"]).as_uri() + "?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT count(*) FROM requests").fetchone()[0] == len(before)
    finally:
        connection.close()
    assert case.store.recent_requests() == before


def test_custom_database_trigger_is_not_executed_by_migration(case):
    connection = sqlite3.connect(case.path)
    try:
        connection.execute("CREATE TRIGGER altered AFTER UPDATE ON profiles BEGIN DELETE FROM requests; END")
        connection.commit()
    finally:
        connection.close()
    fail_unchanged(case, lambda: plan_migration(case.path, 480), "energy_migration_database_invalid")


@pytest.mark.parametrize("revision", [0x7FFFFFFFFFFFFFFE, 0x7FFFFFFFFFFFFFFF])
def test_revision_limit_is_checked_without_partial_write(case, revision):
    connection = sqlite3.connect(case.path)
    try:
        connection.execute("UPDATE profiles SET revision=? WHERE id=1", (revision,))
        connection.commit()
    finally:
        connection.close()
    if revision == 0x7FFFFFFFFFFFFFFF:
        fail_unchanged(case, lambda: plan_migration(case.path, 480), "energy_migration_revision_exhausted")
    else:
        plan = plan_migration(case.path, 480)
        result = apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)
        assert result["revision"] == 0x7FFFFFFFFFFFFFFF
        assert apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)["status"] == "already_applied"


def test_backup_never_overwrites_existing_file(case, monkeypatch):
    case.backups.mkdir()
    existing = case.backups / "energy-clock-fixed.sqlite3"
    existing.write_bytes(b"existing-backup")
    monkeypatch.setattr("aigis_ps.energy_migration.uuid.uuid4", lambda: SimpleNamespace(hex="fixed"))
    plan = plan_migration(case.path, 480)
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "energy_migration_database_invalid")
    assert existing.read_bytes() == b"existing-backup"


def test_failure_after_update_rolls_back_transaction_and_keeps_backup(case, monkeypatch):
    import aigis_ps.energy_migration as module
    plan = plan_migration(case.path, 480)
    original, count = module._read_snapshot, []
    def fail(connection, file_identity):
        count.append(1)
        if len(count) == 4:
            raise ProtocolError("fixture_after_update", "Post-update validation failed", 503)
        return original(connection, file_identity)
    monkeypatch.setattr(module, "_read_snapshot", fail)
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "fixture_after_update")
    assert len(list(case.backups.glob("*.sqlite3"))) == 1


def test_change_after_backup_is_rechecked_inside_write_transaction(case, monkeypatch):
    import aigis_ps.energy_migration as module
    plan = plan_migration(case.path, 480)
    original, changed = module.sqlite3.connect, []
    def race(database, *args, **kwargs):
        if isinstance(database, str) and database.endswith("?mode=rw") and not changed:
            changed.append(update(case, lambda state: state["custom_progress"].update(raced=True)))
        return original(database, *args, **kwargs)
    monkeypatch.setattr(module.sqlite3, "connect", race)
    with pytest.raises(ProtocolError) as error:
        apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)
    assert error.value.code == "energy_migration_stale"
    assert case.store.profile() == changed[0]
    assert len(list(case.backups.glob("*.sqlite3"))) == 1


def table_names(path):
    connection = sqlite3.connect(path)
    try:
        return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not name.startswith("sqlite_")}
    finally:
        connection.close()


def earlier_admin_history(case):
    op_id = str(uuid.uuid4())
    committed = case.store.commit_admin(op_id, admin_payload_digest(op_id, "c" * 64), None, None, {
        "kind": "config", "operations": [{"operation": "fixture.audit"}], "targets": [], "diff": None,
        "result": {"status": "recorded"}})
    case.store.record_admin_failure("command", "plan_expired", 409, "Plan expired")
    return committed.operation


def corrupt_index(path):
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE fixture_rows(a INTEGER, b INTEGER)")
        connection.execute("CREATE INDEX fixture_rows_b ON fixture_rows(b)")
        connection.executemany("INSERT INTO fixture_rows VALUES(?, ?)", [(index, -index) for index in range(1, 4)])
        connection.commit()
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute("UPDATE sqlite_master SET sql='CREATE INDEX fixture_rows_b ON fixture_rows(a)' "
                           "WHERE name='fixture_rows_b'")
        connection.commit()
    finally:
        connection.close()


def test_backup_that_fails_integrity_validation_blocks_the_migration(case):
    corrupt_index(case.path)
    plan = plan_migration(case.path, 480)
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "energy_migration_backup_failed")
    assert table_names(case.path) == {"profiles", "requests", "fixture_rows"}
    assert inspect_database(case.path)["status"] == "migration_required"


def test_apply_records_one_audit_and_registers_its_backup_in_the_migration_transaction(case):
    plan, before = plan_migration(case.path, 480), case.store.profile()
    result = apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)
    operation = case.store.find_admin_operation(plan["plan_sha256"])
    assert (operation["kind"], operation["actor"], operation["plan_digest"]) == (
        "energy_migration", ADMIN_ACTOR, plan["plan_sha256"])
    assert operation["payload_digest"] == admin_payload_digest(plan["plan_sha256"], plan["plan_sha256"])
    assert (operation["revision_before"], operation["revision_after"]) == (before.revision, before.revision + 1)
    assert (operation["state_sha256_before"], operation["state_sha256_after"]) == (
        plan["state_sha256"], plan["candidate_sha256"])
    assert operation["operations"] == [{"operation": "energy_clock_migration", "params": {"source_offset_minutes": 480}}]
    assert operation["targets"] == ["/energy_clock", *(f"/tables/game_param/0/{name}" for name in ANCHORS)]
    assert operation["diff"] == {name: plan[name] for name in ("anchors", "clock_before", "clock_after")}
    assert operation["result"] == result and operation["generation_id"] is None and operation["content_versions"] == {}
    encoded = serialize(operation)
    for value in (before.user_key, before.method_key, "never-print-this-response", "another-private-response", "receipt-key"):
        assert value not in encoded
    (backup,) = case.store.list_backups().items
    assert (backup["kind"], backup["reason"], backup["op_id"], backup["revision"]) == (
        AUTO_BACKUP_KIND, "energy_clock_migration", plan["plan_sha256"], before.revision)
    assert (backup["path"], backup["sha256"], backup["created_at"]) == (
        result["backup"], result["backup_sha256"], operation["created_at"])
    assert table_names(case.path) == {"profiles", "requests", *ADMIN_TABLES}
    assert inspect_database(case.path)["status"] == "ready"


def test_migration_backup_is_a_sealed_snapshot_readable_by_the_admin_reader(case):
    before = case.store.profile()
    result = apply_migration(plan_migration(case.path, 480), preflight=validate_clock, backup_directory=case.backups)
    backup = Path(result["backup"])
    assert backup_state(backup) == before.state
    assert not any(Path(str(backup) + suffix).exists() for suffix in SIDECAR_SUFFIXES)
    snapshot = read_backup(backup)
    assert (snapshot.path, snapshot.sha256, snapshot.profile) == (backup, result["backup_sha256"], before)


def test_already_applied_plan_records_no_second_audit_or_backup(case):
    plan = plan_migration(case.path, 480)
    apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)
    assert apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)["status"] == "already_applied"
    assert len(case.store.list_admin_operations().items) == 1 and len(case.store.list_backups().items) == 1


def test_unchanged_plan_creates_no_admin_tables(case):
    update(case, lambda state: state.update(mark_new(state, datetime(2026, 11, 1, 1, tzinfo=timezone.utc))))
    assert apply_migration(plan_migration(case.path, 480), preflight=validate_clock,
                           backup_directory=case.backups)["status"] == "unchanged"
    assert table_names(case.path) == {"profiles", "requests"}


def test_database_with_admin_tables_still_plans_and_migrates(case):
    earlier = earlier_admin_history(case)
    plan = plan_migration(case.path, 480)
    result = apply_migration(plan, preflight=validate_clock, backup_directory=case.backups)
    assert result["status"] == "applied" and result["revision"] == earlier["revision_after"] + 1
    assert case.store.list_admin_operations().items == (case.store.find_admin_operation(plan["plan_sha256"]), earlier)
    assert [failure["error_code"] for failure in case.store.list_admin_failures().items] == ["plan_expired"]


def test_database_with_admin_tables_and_newer_schema_is_still_rejected(case):
    earlier_admin_history(case)
    connection = sqlite3.connect(case.path)
    try:
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    finally:
        connection.close()
    fail_unchanged(case, lambda: plan_migration(case.path, 480), "energy_migration_database_invalid")
    assert inspect_database(case.path)["ready"] is False


def test_rolled_back_migration_leaves_no_audit_or_backup_registration(case, monkeypatch):
    import aigis_ps.energy_migration as module
    plan = plan_migration(case.path, 480)
    original, count = module._read_snapshot, []
    def fail(connection, file_identity):
        count.append(1)
        if len(count) == 4:
            raise ProtocolError("fixture_after_update", "Post-update validation failed", 503)
        return original(connection, file_identity)
    monkeypatch.setattr(module, "_read_snapshot", fail)
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "fixture_after_update")
    assert table_names(case.path) == {"profiles", "requests"}
    assert len(list(case.backups.glob("*.sqlite3"))) == 1


def test_audit_write_failure_rolls_back_the_migrated_state(case, monkeypatch):
    def broken(*args, **kwargs):
        raise ProtocolError("fixture_audit_failed", "Audit insert failed", 503)
    monkeypatch.setattr("aigis_ps.energy_migration.insert_admin_operation", broken)
    plan = plan_migration(case.path, 480)
    fail_unchanged(case, lambda: apply_migration(plan, preflight=validate_clock, backup_directory=case.backups),
                   "fixture_audit_failed")
    assert table_names(case.path) == {"profiles", "requests"}
    assert inspect_database(case.path)["status"] == "migration_required"

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import threading
import uuid

import pytest

import aigis_ps.storage as storage
from aigis_ps.errors import ProtocolError
from aigis_ps.local_io import MAX_JSON_DEPTH
from aigis_ps.storage import (
    ADMIN_ACTOR, ADMIN_FAILURE_LIMIT, ADMIN_TABLES, APPLICATION_ID, AUTO_BACKUP_KIND, AUTO_BACKUP_LIFETIME,
    MAX_ADMIN_PAGE_SIZE, MAX_AUDIT_FIELD_BYTES, MAX_BACKUP_REASON, MAX_FAILURE_DETAIL_BYTES, MAX_FAILURE_MESSAGE,
    MAX_REVISION, RECORD_FIELDS, SCHEMA_VERSION, SIDECAR_SUFFIXES, AdminOperationConflict,
    RevisionConflict, Store, admin_payload_digest, checkpoint_idle, ensure_admin_tables, read_backup, serialize,
)
from test_storage import NONFINITE, SCANNER_DEPTH, nested


PLAN = "a" * 64
OTHER_PLAN = "b" * 64
CONFIG_SET = "0123456789abcdef01234567"
GENERATION = "c" * 24
CORE_TABLES = {"profiles", "requests"}
CORRUPT_TABLE = "fixture_rows"
LOCK_WAIT_SECONDS = 0.2
NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def operation_id():
    return str(uuid.uuid4())


def sha(value):
    return hashlib.sha256(serialize(value).encode("utf-8")).hexdigest()


def record(**changes):
    return {"kind": "command", "operations": [{"operation": "fixture.set", "params": {"progress": 1}}],
            "targets": ["/progress"], "diff": {"entries": [{"path": "/progress", "before": 0, "after": 1}]},
            "result": {"status": "applied"}, "plan_digest": PLAN, "content_versions": {"fixture": "v1"}, **changes}


def commit(store, op_id, state, revision, *, plan=PLAN, **changes):
    return store.commit_admin(op_id, admin_payload_digest(op_id, plan), revision, state, record(plan_digest=plan, **changes))


def query(path, sql):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def tables(path):
    return {name for (name,) in query(path, "SELECT name FROM sqlite_master WHERE type='table'")
            if not name.startswith("sqlite_")}


def core_schema(path):
    return dict(query(path, "SELECT name, sql FROM sqlite_master WHERE name IN ('profiles', 'requests')"))


def sidecars(path):
    return [suffix for suffix in SIDECAR_SUFFIXES if Path(str(path) + suffix).exists()]


def execute(path, sql, parameters=()):
    connection = sqlite3.connect(path)
    try:
        connection.execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()


def foreign_database(path):
    execute(path, "CREATE TABLE unrelated(value TEXT)")
    return path


def corrupt_index(path):
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"CREATE TABLE {CORRUPT_TABLE}(a INTEGER, b INTEGER)")
        connection.execute(f"CREATE INDEX {CORRUPT_TABLE}_b ON {CORRUPT_TABLE}(b)")
        connection.executemany(f"INSERT INTO {CORRUPT_TABLE} VALUES(?, ?)", [(index, -index) for index in range(1, 4)])
        connection.commit()
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute("UPDATE sqlite_master SET sql=? WHERE name=?",
                           (f"CREATE INDEX {CORRUPT_TABLE}_b ON {CORRUPT_TABLE}(a)", f"{CORRUPT_TABLE}_b"))
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "aigis.sqlite3", {"progress": 0})


def test_reopening_and_reading_keep_a_version_one_database_without_admin_tables(store):
    Store(store.path)
    assert store.find_admin_operation(operation_id()) is None
    for page in (store.list_admin_operations(), store.list_admin_failures(), store.list_generations(), store.list_backups()):
        assert page.items == () and page.next_cursor is None
    assert tables(store.path) == CORE_TABLES
    assert query(store.path, "PRAGMA user_version") == [(SCHEMA_VERSION,)] == [(1,)]


def test_first_commit_creates_admin_tables_inside_its_own_transaction(store):
    op_id = operation_id()
    with pytest.raises(RevisionConflict):
        commit(store, op_id, {"progress": 1}, 5)
    assert tables(store.path) == CORE_TABLES
    committed = commit(store, op_id, {"progress": 1}, 0)
    operation = committed.operation
    assert tables(store.path) == CORE_TABLES | set(ADMIN_TABLES)
    assert committed.replayed is False and committed.generation is None
    assert store.profile().state == {"progress": 1} and store.profile().revision == 1
    assert (operation["sequence"], operation["revision_before"], operation["revision_after"]) == (1, 0, 1)
    assert (operation["state_sha256_before"], operation["state_sha256_after"]) == (sha({"progress": 0}), sha({"progress": 1}))
    assert operation["payload_digest"] == admin_payload_digest(op_id, PLAN) and operation["plan_digest"] == PLAN
    assert (operation["op_id"], operation["kind"], operation["actor"]) == (op_id, "command", ADMIN_ACTOR)
    assert operation["operations"] == record()["operations"] and operation["targets"] == ["/progress"]
    assert operation["diff"] == record()["diff"] and operation["result"] == {"status": "applied"}
    assert operation["content_versions"] == {"fixture": "v1"} and operation["generation_id"] is None
    assert store.find_admin_operation(op_id) == operation


def test_record_shaped_by_the_commit_pipeline_is_accepted_when_its_claims_hold(store):
    op_id, state = operation_id(), {"progress": 1}
    claimed = record(actor=ADMIN_ACTOR, generation_id=GENERATION, revision_before=0, revision_after=1,
                     state_sha256_before=sha({"progress": 0}), state_sha256_after=sha(state))
    operation = store.commit_admin(op_id, admin_payload_digest(op_id, PLAN), 0, state, claimed).operation
    assert {name: operation[name] for name in ("revision_before", "revision_after", "state_sha256_before",
                                                "state_sha256_after", "generation_id")} == {
        name: claimed[name] for name in ("revision_before", "revision_after", "state_sha256_before",
                                         "state_sha256_after", "generation_id")}


@pytest.mark.parametrize("claim", [{"revision_before": 7}, {"revision_after": 0}, {"revision_after": True},
                                   {"state_sha256_before": "f" * 64}, {"state_sha256_after": "f" * 64}])
def test_record_claims_that_disagree_with_the_store_roll_back(store, claim):
    op_id = operation_id()
    with pytest.raises(ValueError, match="disagrees"):
        store.commit_admin(op_id, admin_payload_digest(op_id, PLAN), 0, {"progress": 1}, record(**claim))
    assert store.profile().revision == 0 and tables(store.path) == CORE_TABLES


def test_failed_audit_write_rolls_back_state_and_created_tables(store, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("fixture audit failure")
    monkeypatch.setattr(storage, "_write_operation", fail)
    with pytest.raises(sqlite3.OperationalError):
        commit(store, operation_id(), {"progress": 1}, 0)
    assert store.profile().revision == 0 and store.profile().state == {"progress": 0}
    assert tables(store.path) == CORE_TABLES


def test_admin_tables_keep_every_condition_the_previous_server_checks(store, tmp_path):
    expected = core_schema(Store(tmp_path / "fresh.sqlite3").path)
    commit(store, operation_id(), {"progress": 1}, 0, kind="config", switch={"config_set": CONFIG_SET, "tier": "gated"})
    store.record_admin_failure("command", "plan_expired", 409, "Plan expired")
    store.backup(tmp_path / "backups", "compatibility", kind="manual")
    assert tables(store.path) == CORE_TABLES | set(ADMIN_TABLES)
    app_id = query(store.path, "PRAGMA application_id")[0][0]
    version = query(store.path, "PRAGMA user_version")[0][0]
    names = query(store.path, "SELECT name FROM sqlite_master WHERE type='table'")
    assert not ((app_id not in (0, APPLICATION_ID)) or (app_id == 0 and names))
    assert version <= SCHEMA_VERSION and version == 1
    assert query(store.path, "SELECT name FROM sqlite_master WHERE type='trigger'") == []
    assert core_schema(store.path) == expected
    reopened = Store(store.path)
    assert reopened.profile() == store.profile()
    assert reopened.update_state({"progress": 2}, reopened.profile().revision).revision == 2
    reopened.record_request(None, "fixture-route", 1, 2, 200)
    assert reopened.recent_requests()[0]["route"] == "fixture-route"


def test_revision_conflict_reports_the_current_revision_and_writes_nothing(store):
    commit(store, operation_id(), {"progress": 1}, 0)
    before, op_id = store.profile(), operation_id()
    with pytest.raises(RevisionConflict) as error:
        commit(store, op_id, {"progress": 9}, 0)
    assert error.value.current_revision == 1
    assert (error.value.code, error.value.status) == ("admin_revision_conflict", 409)
    assert store.profile() == before and store.find_admin_operation(op_id) is None
    assert len(store.list_admin_operations().items) == 1


def test_replayed_operation_returns_the_recorded_result_without_another_revision(store):
    op_id = operation_id()
    first = commit(store, op_id, {"progress": 1}, 0)
    replay = commit(store, op_id, {"progress": 2}, 0)
    assert replay.replayed is True and replay.operation == first.operation
    assert store.profile().revision == 1 and store.profile().state == {"progress": 1}
    assert len(store.list_admin_operations().items) == 1


def test_reused_operation_id_with_another_payload_is_rejected(store):
    op_id = operation_id()
    commit(store, op_id, {"progress": 1}, 0)
    before = store.profile()
    with pytest.raises(AdminOperationConflict) as error:
        commit(store, op_id, {"progress": 1}, 1, plan=OTHER_PLAN)
    assert (error.value.code, error.value.status, error.value.op_id) == ("admin_operation_conflict", 409, op_id)
    assert store.profile() == before and store.find_admin_operation(op_id)["plan_digest"] == PLAN


def test_concurrent_commits_with_one_operation_id_apply_once(store):
    op_id, barrier, results, errors = operation_id(), threading.Barrier(4), [], []
    def attempt():
        barrier.wait()
        try:
            results.append(commit(store, op_id, {"progress": 1}, 0))
        except BaseException as error:
            errors.append(error)
    workers = [threading.Thread(target=attempt) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    assert sorted(result.replayed for result in results) == [False, True, True, True]
    assert all(result.operation == results[0].operation for result in results)
    assert store.profile().revision == 1 and len(store.list_admin_operations().items) == 1


def test_commit_waits_for_an_inflight_game_write_and_reports_its_revision(store):
    store.record_admin_failure("command", "plan_expired", 409, "Plan expired")
    game, outcome = sqlite3.connect(store.path, isolation_level=None), []
    def attempt():
        try:
            outcome.append(commit(store, operation_id(), {"progress": 9}, 0))
        except BaseException as error:
            outcome.append(error)
    worker = threading.Thread(target=attempt)
    try:
        game.execute("BEGIN IMMEDIATE")
        game.execute("UPDATE profiles SET revision=revision+1 WHERE id=1")
        worker.start()
        worker.join(LOCK_WAIT_SECONDS)
        assert worker.is_alive()
        game.commit()
        worker.join()
    finally:
        game.close()
    (error,) = outcome
    assert isinstance(error, RevisionConflict) and error.current_revision == 1
    assert store.profile().state == {"progress": 0} and store.list_admin_operations().items == ()


def test_state_free_commit_is_audited_without_changing_the_revision(store):
    op_id = operation_id()
    operation = store.commit_admin(op_id, admin_payload_digest(op_id, PLAN), None, None, record(kind="config")).operation
    assert store.profile().revision == 0
    assert operation["revision_before"] == operation["revision_after"] == 0
    assert operation["state_sha256_before"] == operation["state_sha256_after"] == sha({"progress": 0})
    checked = operation_id()
    with pytest.raises(RevisionConflict) as error:
        store.commit_admin(checked, admin_payload_digest(checked, PLAN), 3, None, record(kind="config"))
    assert error.value.current_revision == 0
    unchanged = operation_id()
    assert store.commit_admin(unchanged, admin_payload_digest(unchanged, PLAN), 0, None, record()).operation["revision_after"] == 0
    unchecked = operation_id()
    with pytest.raises(ValueError, match="expected revision"):
        store.commit_admin(unchecked, admin_payload_digest(unchecked, PLAN), None, {"progress": 1}, record())
    assert store.profile().state == {"progress": 0}


def test_exhausted_revision_rejects_state_changes_but_still_audits_state_free_commits(store):
    execute(store.path, "UPDATE profiles SET revision=? WHERE id=1", (MAX_REVISION,))
    op_id = operation_id()
    with pytest.raises(ProtocolError) as error:
        commit(store, op_id, {"progress": 1}, MAX_REVISION)
    assert (error.value.code, error.value.status) == ("admin_revision_exhausted", 409)
    assert store.profile().state == {"progress": 0} and tables(store.path) == CORE_TABLES
    audited = commit(store, op_id, None, MAX_REVISION).operation
    assert audited["revision_before"] == audited["revision_after"] == MAX_REVISION


@pytest.mark.parametrize("changes,error", [
    ({"kind": "Bad Kind"}, ValueError),
    ({"actor": ""}, ValueError),
    ({"unexpected": True}, ValueError),
    ({"operations": []}, ValueError),
    ({"operations": {"operation": "fixture.set"}}, TypeError),
    ({"targets": "/progress"}, TypeError),
    ({"diff": {"value": float("nan")}}, ValueError),
    ({"diff": {"blob": "x" * MAX_AUDIT_FIELD_BYTES}}, ValueError),
    ({"result": {"value": object()}}, TypeError),
    ({"plan_digest": "not-a-digest"}, ValueError),
    ({"generation_id": "bad id"}, ValueError),
    ({"content_versions": ["v1"]}, TypeError),
    ({"switch": (CONFIG_SET, "hot")}, TypeError),
    ({"switch": {"config_set": "bad set", "tier": "hot"}}, ValueError),
    ({"switch": {"config_set": CONFIG_SET}}, ValueError),
])
def test_invalid_audit_records_are_rejected_before_any_write(store, changes, error):
    op_id = operation_id()
    with pytest.raises(error):
        store.commit_admin(op_id, admin_payload_digest(op_id, PLAN), 0, {"progress": 1}, record(**changes))
    assert store.profile().revision == 0 and tables(store.path) == CORE_TABLES


@pytest.mark.parametrize("missing", sorted(RECORD_FIELDS))
def test_records_missing_a_required_field_are_rejected(store, missing):
    op_id, incomplete = operation_id(), record()
    del incomplete[missing]
    with pytest.raises(ValueError, match="missing or unknown"):
        store.commit_admin(op_id, admin_payload_digest(op_id, PLAN), 0, {"progress": 1}, incomplete)
    assert tables(store.path) == CORE_TABLES


@pytest.mark.parametrize("op_id,payload,revision,state,record_value", [
    ("", PLAN, 0, {"progress": 1}, record()),
    ("bad id", PLAN, 0, {"progress": 1}, record()),
    ("fixture-op", "short", 0, {"progress": 1}, record()),
    ("fixture-op", PLAN, True, {"progress": 1}, record()),
    ("fixture-op", PLAN, -1, {"progress": 1}, record()),
    ("fixture-op", PLAN, 0, ["progress"], record()),
    ("fixture-op", PLAN, 0, {"progress": 1}, [("kind", "command")]),
])
def test_invalid_commit_arguments_are_rejected_before_any_write(store, op_id, payload, revision, state, record_value):
    with pytest.raises((ValueError, TypeError)):
        store.commit_admin(op_id, payload, revision, state, record_value)
    assert store.profile().revision == 0 and tables(store.path) == CORE_TABLES


def test_payload_digest_binds_operation_and_plan():
    op_id = operation_id()
    assert admin_payload_digest(op_id, PLAN) == sha({"op_id": op_id, "plan_digest": PLAN})
    assert len({admin_payload_digest(op_id, PLAN), admin_payload_digest(op_id, OTHER_PLAN),
                admin_payload_digest(operation_id(), PLAN)}) == 3
    with pytest.raises(ValueError):
        admin_payload_digest(op_id, "short")


def test_failed_attempts_keep_only_the_newest_limit(store):
    assert ADMIN_FAILURE_LIMIT == 2000
    first = store.record_admin_failure("command", "plan_expired", 409, "Plan expired", op_id=operation_id())
    connection = sqlite3.connect(store.path)
    try:
        connection.executemany(
            "INSERT INTO admin_failures(op_id,kind,actor,error_code,status,message,detail_json,created_at) "
            "VALUES(NULL,'command',?,'seeded',409,'seeded','null',?)",
            [(ADMIN_ACTOR, first["created_at"])] * (ADMIN_FAILURE_LIMIT - 1))
        connection.commit()
    finally:
        connection.close()
    newest = [store.record_admin_failure("command", "plan_changed", 409, f"attempt {index}") for index in range(3)]
    assert query(store.path, "SELECT COUNT(*), MIN(id) FROM admin_failures") == [(ADMIN_FAILURE_LIMIT, first["id"] + 3)]
    assert store.list_admin_failures(limit=3).items == tuple(reversed(newest))


def test_failure_records_bound_their_message_and_detail(store):
    long = store.record_admin_failure("command", "admin_preflight_failed", 503, "x" * (MAX_FAILURE_MESSAGE + 5),
                                      detail={"blob": "y" * MAX_FAILURE_DETAIL_BYTES})
    assert len(long["message"]) == MAX_FAILURE_MESSAGE
    assert long["detail"]["truncated"] is True and long["detail"]["bytes"] > MAX_FAILURE_DETAIL_BYTES
    short = store.record_admin_failure("restore", "admin_backup_invalid", 400, "Invalid backup", detail={"reason": "schema"})
    assert (short["kind"], short["error_code"], short["status"], short["actor"]) == (
        "restore", "admin_backup_invalid", 400, ADMIN_ACTOR)
    assert short["detail"] == {"reason": "schema"} and short["op_id"] is None
    for arguments in (("command", "Bad Code", 409, "m"), ("command", "plan_expired", 99, "m"),
                      ("command", "plan_expired", True, "m"), ("Bad Kind", "plan_expired", 409, "m")):
        with pytest.raises(ValueError):
            store.record_admin_failure(*arguments)
    assert len(store.list_admin_failures().items) == 2


def test_generation_switches_are_numbered_listed_and_idempotent_by_operation(store):
    op_id = operation_id()
    first = store.record_generation(CONFIG_SET, "hot", generation_id=GENERATION, op_id=op_id)
    second = store.record_generation("f" * 24, "content")
    assert (first["sequence"], second["sequence"]) == (1, 2) and second["op_id"] is None
    assert (first["config_set"], first["tier"], first["generation_id"], first["op_id"]) == (CONFIG_SET, "hot", GENERATION, op_id)
    assert store.record_generation(CONFIG_SET, "hot", generation_id=GENERATION, op_id=op_id) == first
    with pytest.raises(AdminOperationConflict):
        store.record_generation("f" * 24, "hot", op_id=op_id)
    for config_set, tier in (("bad set", "hot"), (CONFIG_SET, "Hot Tier"), (None, "hot")):
        with pytest.raises(ValueError):
            store.record_generation(config_set, tier)
    assert store.list_generations().items == (second, first)
    assert store.list_generations(limit=1).next_cursor == second["sequence"]


def test_gated_switch_commits_state_audit_and_generation_together(store):
    op_id, switch = operation_id(), {"config_set": CONFIG_SET, "tier": "gated", "generation_id": GENERATION}
    with pytest.raises(RevisionConflict):
        commit(store, op_id, {"progress": 1}, 4, kind="config", switch=switch)
    assert store.list_generations().items == ()
    committed = commit(store, op_id, {"progress": 1}, 0, kind="config", switch=switch)
    generation = committed.generation
    assert (generation["sequence"], generation["config_set"], generation["tier"], generation["op_id"]) == (
        1, CONFIG_SET, "gated", op_id)
    assert generation["generation_id"] == GENERATION and generation["created_at"] == committed.operation["created_at"]
    replay = commit(store, op_id, {"progress": 1}, 0, kind="config", switch=switch)
    assert replay.replayed is True and replay.generation == generation
    assert store.list_generations().items == (generation,)


def test_operation_pages_follow_a_stable_cursor(store):
    for _ in range(5):
        op_id = operation_id()
        store.commit_admin(op_id, admin_payload_digest(op_id, PLAN), None, None, record())
    first = store.list_admin_operations(limit=2)
    second = store.list_admin_operations(first.next_cursor, 2)
    third = store.list_admin_operations(second.next_cursor, 2)
    assert [item["sequence"] for page in (first, second, third) for item in page.items] == [5, 4, 3, 2, 1]
    assert third.next_cursor is None
    for cursor, limit in ((None, 0), (None, MAX_ADMIN_PAGE_SIZE + 1), (None, True), (0, 10), (True, 10)):
        with pytest.raises(ValueError):
            store.list_admin_operations(cursor, limit)


def test_backup_copies_a_verified_snapshot_and_registers_it(store, tmp_path):
    op_id = operation_id()
    commit(store, op_id, {"progress": 1}, 0)
    store.record_request(None, "fixture-route", 1, 2, 200)
    registered = store.backup(tmp_path / "backups", "before restore", op_id, kind=AUTO_BACKUP_KIND)
    path = Path(registered["path"])
    assert path.parent == tmp_path / "backups" and path.name.startswith(f"admin-{AUTO_BACKUP_KIND}-")
    assert (registered["kind"], registered["reason"], registered["op_id"], registered["revision"]) == (
        AUTO_BACKUP_KIND, "before restore", op_id, 1)
    assert registered["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest() and registered["created_at"]
    assert store.list_backups().items == (registered,)
    snapshot = read_backup(path, root=tmp_path)
    assert snapshot.path == path and snapshot.sha256 == registered["sha256"] and snapshot.profile == store.profile()
    copied = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        assert copied.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert copied.execute("SELECT COUNT(*) FROM requests").fetchone() == (1,)
        assert copied.execute("SELECT COUNT(*) FROM admin_operations").fetchone() == (1,)
    finally:
        copied.close()
    assert sidecars(path) == []


def test_backup_keeps_the_snapshot_it_verified_while_the_game_writes(store, tmp_path, monkeypatch):
    original, raced = storage._profile_rows, []
    def racing(connection):
        rows = original(connection)
        if not raced:
            raced.append(store.update_state({"progress": 5}, 0))
        return rows
    monkeypatch.setattr(storage, "_profile_rows", racing)
    registered = store.backup(tmp_path / "backups", "racing write", kind="manual")
    assert registered["revision"] == 0 and raced[0].revision == 1
    assert read_backup(Path(registered["path"])).profile.state == {"progress": 0}
    assert store.profile().state == {"progress": 5}


def test_backup_that_fails_verification_leaves_no_file_or_registration(store, tmp_path, monkeypatch):
    original, calls = storage._profile_rows, []
    def diverging(connection):
        calls.append(original(connection))
        return calls[0] if len(calls) == 1 else ()
    monkeypatch.setattr(storage, "_profile_rows", diverging)
    with pytest.raises(ProtocolError) as error:
        store.backup(tmp_path / "backups", "diverging copy", kind="manual")
    assert (error.value.code, error.value.status) == ("admin_backup_failed", 503)
    assert list((tmp_path / "backups").iterdir()) == []
    assert tables(store.path) == CORE_TABLES


def test_backup_rejects_other_directories_kinds_and_reasons(store, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    with pytest.raises(ProtocolError) as error:
        store.backup(outside, "outside", kind="manual")
    assert (error.value.code, error.value.status) == ("admin_backup_path_invalid", 400) and not outside.exists()
    for kind, reason in (("weekly", "reason"), (AUTO_BACKUP_KIND, ""), (AUTO_BACKUP_KIND, "two\nlines"),
                         (AUTO_BACKUP_KIND, "x" * (MAX_BACKUP_REASON + 1))):
        with pytest.raises(ValueError):
            store.backup(tmp_path / "backups", reason, kind=kind)
    with pytest.raises(ValueError):
        store.backup(tmp_path / "backups", "reason", "bad id", kind=AUTO_BACKUP_KIND)
    assert not (tmp_path / "backups").exists() and tables(store.path) == CORE_TABLES


def at(store, moment):
    store.clock = lambda: moment
    return store


def backup_paths(store):
    return [Path(item["path"]) for item in store.list_backups().items]


def test_only_the_newest_automatic_backup_is_kept(store, tmp_path):
    at(store, NOW)
    first = Path(store.backup(tmp_path / "backups", "first", kind=AUTO_BACKUP_KIND)["path"])
    second = Path(store.backup(tmp_path / "backups", "second", kind=AUTO_BACKUP_KIND)["path"])
    assert not first.exists() and second.is_file()
    assert backup_paths(store) == [second]


def test_an_automatic_backup_older_than_a_day_is_removed_even_when_it_is_the_only_one(store, tmp_path):
    at(store, NOW)
    path = Path(store.backup(tmp_path / "backups", "only", kind=AUTO_BACKUP_KIND)["path"])
    at(store, NOW + AUTO_BACKUP_LIFETIME - timedelta(seconds=1))
    assert backup_paths(store) == [path]
    at(store, NOW + AUTO_BACKUP_LIFETIME)
    assert backup_paths(store) == [] and not path.exists()


def test_manual_backups_survive_both_rules(store, tmp_path):
    at(store, NOW)
    manual = Path(store.backup(tmp_path / "backups", "kept by hand", kind="manual")["path"])
    store.backup(tmp_path / "backups", "first", kind=AUTO_BACKUP_KIND)
    store.backup(tmp_path / "backups", "second", kind=AUTO_BACKUP_KIND)
    at(store, NOW + 10 * AUTO_BACKUP_LIFETIME)
    removed = store.prune_backups()
    assert [item["reason"] for item in removed] == ["second"]
    assert backup_paths(store) == [manual] and manual.is_file()


def test_missing_or_foreign_files_only_lose_their_registration(store, tmp_path):
    at(store, NOW)
    outside = tmp_path.parent / f"{tmp_path.name}-outside" / f"admin-auto-{'1' * 32}.sqlite3"
    outside.parent.mkdir()
    outside.write_bytes(b"keep")
    targets = (outside, store.path, tmp_path / "backups" / f"admin-auto-{'0' * 32}.sqlite3")
    for target in targets:
        registered = store.backup(tmp_path / "backups", "redirected", kind=AUTO_BACKUP_KIND)
        Path(registered["path"]).unlink()
        execute(store.path, "UPDATE backups SET path=? WHERE id=?", (str(target), registered["id"]))
        at(store, NOW + AUTO_BACKUP_LIFETIME)
        assert backup_paths(store) == []
        at(store, NOW)
    assert outside.read_bytes() == b"keep" and store.profile().state == {"progress": 0}


def test_an_open_backup_waits_for_a_later_prune(store, tmp_path):
    at(store, NOW)
    path = Path(store.backup(tmp_path / "backups", "open", kind=AUTO_BACKUP_KIND)["path"])
    journal = Path(str(path) + "-journal")
    journal.write_bytes(b"")
    at(store, NOW + AUTO_BACKUP_LIFETIME)
    assert backup_paths(store) == [path] and path.is_file()
    journal.unlink()
    assert backup_paths(store) == [] and not path.exists()


def test_pruning_a_database_without_admin_tables_creates_nothing(store):
    assert store.prune_backups() == () and store.list_backups().items == ()
    assert tables(store.path) == CORE_TABLES


@pytest.mark.parametrize("moment", [datetime(2026, 11, 2, 12), "2026-11-02T12:00:00+00:00"])
def test_the_store_clock_must_be_an_aware_datetime(store, tmp_path, moment):
    store.backup(tmp_path / "backups", "existing", kind="manual")
    with pytest.raises(ValueError):
        at(store, moment).prune_backups()


def test_read_backup_rejects_foreign_other_version_malformed_busy_and_outside_files(store, tmp_path):
    unversioned = Store(tmp_path / "unversioned.sqlite3").path
    execute(unversioned, "PRAGMA user_version=0")
    newer = Store(tmp_path / "newer.sqlite3").path
    execute(newer, "PRAGMA user_version=2")
    duplicated = Store(tmp_path / "duplicated.sqlite3").path
    execute(duplicated, "UPDATE profiles SET state_json=? WHERE id=1", ('{"progress":1,"progress":2}',))
    listed = Store(tmp_path / "listed.sqlite3").path
    execute(listed, "UPDATE profiles SET state_json=? WHERE id=1", ("[1]",))
    keyless = Store(tmp_path / "keyless.sqlite3").path
    execute(keyless, "UPDATE profiles SET user_key='' WHERE id=1")
    for path in (foreign_database(tmp_path / "foreign.sqlite3"), unversioned, newer, duplicated, listed, keyless):
        with pytest.raises(ProtocolError) as error:
            read_backup(path)
        assert (error.value.code, error.value.status) == ("admin_backup_invalid", 400)
    valid = Path(store.backup(tmp_path / "backups", "valid", kind="manual")["path"])
    Path(str(valid) + "-wal").write_bytes(b"")
    with pytest.raises(ProtocolError) as error:
        read_backup(valid)
    assert (error.value.code, error.value.status) == ("admin_backup_busy", 409)
    Path(str(valid) + "-wal").unlink()
    for path, root in ((valid, tmp_path / "elsewhere"), (tmp_path / "missing.sqlite3", None)):
        with pytest.raises(ProtocolError) as error:
            read_backup(path, root=root)
        assert error.value.code == "admin_backup_path_invalid"
    assert read_backup(valid, root=tmp_path).profile == store.profile()


@pytest.mark.parametrize("value", [*NONFINITE, "[" * MAX_JSON_DEPTH + "]" * MAX_JSON_DEPTH,
                                   "[" * SCANNER_DEPTH + "]" * SCANNER_DEPTH],
                         ids=[*NONFINITE, "nested", "recursion"])
def test_read_backup_rejects_nonfinite_numbers_and_excessive_nesting(tmp_path, value):
    path = Store(tmp_path / "unsafe.sqlite3").path
    execute(path, "UPDATE profiles SET state_json=? WHERE id=1", ('{"progress":' + value + "}",))
    with pytest.raises(ProtocolError) as error:
        read_backup(path)
    assert (error.value.code, error.value.status) == ("admin_backup_invalid", 400)


def test_backups_keep_the_deepest_permitted_state_and_refuse_a_deeper_live_state(tmp_path):
    deepest = {"progress": nested(MAX_JSON_DEPTH - 1)}
    registered = Store(tmp_path / "deepest.sqlite3", deepest).backup(tmp_path / "backups", "deepest", kind="manual")
    assert read_backup(Path(registered["path"]), root=tmp_path).profile.state == deepest
    deeper = Store(tmp_path / "deeper.sqlite3", {"progress": nested(MAX_JSON_DEPTH)})
    with pytest.raises(ProtocolError) as error:
        deeper.backup(tmp_path / "refused", "deeper", kind="manual")
    assert (error.value.code, error.value.status) == ("admin_backup_failed", 503)
    assert list((tmp_path / "refused").iterdir()) == [] and deeper.list_backups().items == ()
    assert deeper.profile().state == {"progress": nested(MAX_JSON_DEPTH)}


def test_integrity_failures_block_backup_reads_and_new_backups(store, tmp_path):
    corrupt_index(store.path)
    assert store.profile().state == {"progress": 0}
    with pytest.raises(ProtocolError) as error:
        read_backup(store.path)
    assert (error.value.code, error.value.status) == ("admin_backup_invalid", 400)
    with pytest.raises(ProtocolError) as error:
        store.backup(tmp_path / "backups", "corrupt source", kind="manual")
    assert (error.value.code, error.value.status) == ("admin_backup_failed", 503)
    assert list((tmp_path / "backups").iterdir()) == [] and tables(store.path) == CORE_TABLES | {CORRUPT_TABLE}


def test_checkpoint_idle_reports_unfinished_while_another_connection_holds_the_log(store):
    writer = sqlite3.connect(store.path, isolation_level=None)
    reader = sqlite3.connect(store.path, isolation_level=None)
    try:
        writer.execute("UPDATE profiles SET updated_at=? WHERE id=1", ("fixture-first-write",))
        reader.execute("BEGIN")
        reader.execute("SELECT revision FROM profiles").fetchone()
        writer.execute("UPDATE profiles SET updated_at=? WHERE id=1", ("fixture-second-write",))
        assert Path(str(store.path) + "-wal").stat().st_size > 0
        assert checkpoint_idle(store.path) is False
        reader.rollback()
        assert checkpoint_idle(store.path) is False and Path(str(store.path) + "-wal").stat().st_size == 0
    finally:
        reader.close()
        writer.close()
    assert checkpoint_idle(store.path) is True and sidecars(store.path) == []
    assert query(store.path, "SELECT updated_at FROM profiles") == [("fixture-second-write",)]


def test_checkpoint_idle_never_recreates_a_database_removed_after_the_check(tmp_path, monkeypatch):
    removed = tmp_path / "removed.sqlite3"
    monkeypatch.setattr(Path, "is_file", lambda path: True)
    with pytest.raises(ProtocolError) as error:
        checkpoint_idle(removed)
    assert (error.value.code, error.value.status) == ("admin_database_invalid", 400)
    assert not removed.exists()


def test_checkpoint_idle_clears_stale_sidecars_and_rejects_foreign_files(store, tmp_path):
    for suffix in ("-wal", "-shm"):
        Path(str(store.path) + suffix).write_bytes(b"")
    assert checkpoint_idle(store.path) is True and sidecars(store.path) == []
    assert store.profile().state == {"progress": 0}
    with pytest.raises(ProtocolError) as error:
        checkpoint_idle(foreign_database(tmp_path / "foreign.sqlite3"))
    assert (error.value.code, error.value.status) == ("admin_database_invalid", 400)
    with pytest.raises(ProtocolError) as error:
        checkpoint_idle(tmp_path / "missing.sqlite3")
    assert error.value.code == "admin_database_path_invalid" and not (tmp_path / "missing.sqlite3").exists()


def test_admin_tables_are_created_only_inside_a_write_transaction(store):
    connection = sqlite3.connect(store.path, isolation_level=None)
    try:
        with pytest.raises(ValueError, match="transaction"):
            ensure_admin_tables(connection)
        connection.execute("BEGIN IMMEDIATE")
        ensure_admin_tables(connection)
        ensure_admin_tables(connection)
        connection.rollback()
    finally:
        connection.close()
    assert tables(store.path) == CORE_TABLES

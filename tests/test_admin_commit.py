from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from pydantic import Field

from aigis_ps import admin_commit
from aigis_ps.admin_command import (AdminCommand, AdminError, Batch, BatchStep, CommandParams, CommandResult,
                                    NoopPreflight, NoopRestartCheck, PlanStore, PreflightReport, require_op_id)
from aigis_ps.admin_commit import (AUDIT_DIFF_LIMITS, AUDIT_SKIPPED, AUDIT_TOO_LARGE, BACKUP_FAILED, COMMAND_KIND,
                                   PREVIEW_OP_ID, CommitPipeline, InitializerRestartCheck, backup_reason,
                                   commit_audited, require_auditable, requires_backup, takes_backup)
from aigis_ps.admin_diff import json_digest
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import (_JSON_COLUMNS, ADMIN_ACTOR, AUDIT_JSON_FIELDS, AUTO_BACKUP_KIND, MAX_AUDIT_FIELD_BYTES,
                              MAX_BACKUP_REASON, AdminCommit, AdminOperationConflict, PlayerProfile, RevisionConflict,
                              Store, admin_payload_digest, read_backup, serialize)


NOW = datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc)
BACKUPS = Path("backups")
LARGE_DEBIT = 5


class AmountParams(CommandParams):
    amount: int = Field(ge=0)


class UnitParams(CommandParams):
    unique_id: int
    exp: int


class NoteParams(CommandParams):
    note: str


class FakeStore:
    def __init__(self, state, revision=0, *, raise_on_duplicate=False):
        self.state, self.revision = deepcopy(state), revision
        self.raise_on_duplicate = raise_on_duplicate
        self.operations, self.calls, self.before_commit = {}, [], None
        self.backups, self.backup_error = [], None

    def backup(self, directory, reason, op_id=None, *, kind):
        if self.backup_error is not None:
            raise self.backup_error
        number = len(self.backups) + 1
        row = {"id": number, "kind": kind, "reason": reason, "op_id": op_id, "path": str(directory / f"copy-{number}.sqlite3"),
               "sha256": "b" * 64, "revision": self.revision, "created_at": NOW.isoformat()}
        self.backups.append({"row": row, "state": deepcopy(self.state), "commits_before": len(self.calls)})
        return row

    def profile(self):
        return PlayerProfile("1", "user-key", "method-key", json.loads(serialize(self.state)), self.revision)

    def find_admin_operation(self, op_id):
        record = self.operations.get(op_id)
        return None if record is None else json.loads(serialize(record))

    def commit_admin(self, op_id, payload_digest, expected_revision, state, record):
        hook, self.before_commit = self.before_commit, None
        if hook is not None:
            hook()
        self.calls.append({"state": state, "record": record})
        existing = self.operations.get(op_id)
        if existing is not None:
            if existing["payload_digest"] != payload_digest or self.raise_on_duplicate:
                raise AdminOperationConflict(op_id)
            return AdminCommit(json.loads(serialize(existing)), True, None)
        if expected_revision != self.revision:
            raise RevisionConflict(self.revision)
        if state is not None:
            self.state, self.revision = json.loads(serialize(state)), self.revision + 1
        self.operations[op_id] = {"op_id": op_id, "payload_digest": payload_digest, **json.loads(serialize(record))}
        return AdminCommit(json.loads(serialize(self.operations[op_id])), False, None)


@dataclass
class FakeGeneration:
    id: str = "gen-a"
    bundle: object = None
    content_versions: dict = field(default_factory=lambda: {"wallet": "v1"})
    clock: datetime = NOW

    def now(self):
        return self.clock

    def acquisition_day(self, now):
        return now.astimezone(JST).date().toordinal() - 1


class FakeRuntime:
    def __init__(self, generation=None):
        self.generation = generation or FakeGeneration()
        self.active = 0

    @contextmanager
    def lease(self):
        self.active += 1
        try:
            yield self.generation
        finally:
            self.active -= 1


class SwitchablePreflight:
    is_noop = False

    def __init__(self):
        self.failing, self.calls = False, []

    def check(self, before, candidate, domains, generation):
        self.calls.append(sorted(domains))
        if self.failing:
            raise ProtocolError("admin_preflight_failed", "Response 137 could not be encoded", 409)
        return PreflightReport({domain: (137,) for domain in domains})


def credit(state, params, context):
    updated = deepcopy(state)
    updated["wallet"] += params.amount
    return CommandResult(updated, {"credited": params.amount})


def debit(state, params, context):
    updated = deepcopy(state)
    updated["wallet"] -= params.amount
    return CommandResult(updated, {"debited": params.amount})


def edit_unit(state, params, context):
    updated = deepcopy(state)
    unit = next(row for row in updated["player_state"]["units"] if row["UniqueID"] == params.unique_id)
    unit["Exp"] = params.exp
    return CommandResult(updated)


def padded_credit(state, params, context):
    return CommandResult({**state, "wallet": state["wallet"] + 1}, {"padding": "x" * params.amount})


def note(state, params, context):
    return CommandResult({**state, "notes": [*state.get("notes", ()), params.note]})


def debit_phrase(params):
    return f"DEBIT {params.amount}"


CREDIT = AdminCommand("wallet.credit", "wallet", AmountParams, credit, frozenset({"wallet"}))
DEBIT = AdminCommand("wallet.debit", "wallet", AmountParams, debit, frozenset({"wallet"}), destructive=True,
                     confirm_phrase=debit_phrase)
SCALED_DEBIT = AdminCommand("wallet.scaled_debit", "wallet", AmountParams, debit, frozenset({"wallet"}),
                            destructive=lambda params: params.amount >= LARGE_DEBIT, confirm_phrase=debit_phrase)
UNIT_EDIT = AdminCommand("units.edit_growth", "units", UnitParams, edit_unit, frozenset({"units"}))
PADDED_CREDIT = AdminCommand("wallet.padded_credit", "wallet", AmountParams, padded_credit, frozenset({"wallet"}))
NOTE = AdminCommand("wallet.note", "wallet", NoteParams, note, frozenset({"wallet"}))


def batch(*steps):
    return Batch(tuple(BatchStep(command, command.params_model(**params)) for command, params in steps))


def credit_batch(amount=5):
    return batch((CREDIT, {"amount": amount}))


def pipeline(state=None, *, store=None, runtime=None, preflight=None, restart_check=None, plans=None,
             backup_directory=BACKUPS):
    store = store or FakeStore(state if state is not None else {"wallet": 0})
    runtime = runtime or FakeRuntime()
    plans = plans or PlanStore()
    return CommitPipeline(store, plans, preflight or SwitchablePreflight(),
                          restart_check or InitializerRestartCheck(lambda value: value), runtime,
                          backup_directory=backup_directory), store, runtime, plans


def commit(subject, plan, op_id=None, **changes):
    request = {"plan_id": plan.plan_id, "plan_digest": plan.plan_digest, "expected_revision": plan.expected_revision,
               "op_id": op_id or str(uuid.uuid4())}
    request.update(changes)
    return subject.commit(**request)


def test_preview_then_commit_applies_the_batch_once():
    subject, store, runtime, plans = pipeline({"wallet": 1, "other": [1, 2]})
    plan = subject.preview(credit_batch(5))
    assert plan.committable and plan.plan_id and len(plans) == 1 and store.state["wallet"] == 1
    description = plan.describe()
    assert description["diff"]["entries"] == [{"op": "replace", "path": "/wallet", "before": 1, "after": 6}]
    assert description["preflight"] == {"ok": True, "readback": {"wallet": [137]}} and description["restart_check"] == {"ok": True}
    assert description["audit"] == {"ok": True}
    assert description["context"]["acquisition_day"] == NOW.astimezone(JST).date().toordinal() - 1
    assert description["steps"] == [{"step": 0, "operation": "wallet.credit", "effects": {"credited": 5}, "warnings": []}]
    op_id = str(uuid.uuid4())
    outcome = commit(subject, plan, op_id)
    assert outcome.replayed is False and store.state == {"wallet": 6, "other": [1, 2]} and store.revision == 1
    assert outcome.to_dict() == {**outcome.result, "replayed": False}
    assert outcome.result["revision_before"] == 0 and outcome.result["revision_after"] == 1
    assert outcome.result["readback"] == {"wallet": [137]} and outcome.result["op_id"] == op_id
    record = store.operations[op_id]
    assert record["payload_digest"] == admin_payload_digest(op_id, plan.plan_digest)
    assert record["kind"] == COMMAND_KIND and record["actor"] == ADMIN_ACTOR and record["targets"] == ["wallet"]
    assert record["operations"] == [{"operation": "wallet.credit", "params": {"amount": 5}}]
    assert record["state_sha256_before"] == json_digest({"wallet": 1, "other": [1, 2]})
    assert record["state_sha256_after"] == json_digest(store.state)
    assert record["generation_id"] == "gen-a" and record["content_versions"] == {"wallet": "v1"}
    assert record["diff"]["entries"] == description["diff"]["entries"] and record["result"] == outcome.result
    assert len(plans) == 0 and runtime.active == 0


def test_retry_after_success_is_replayed_even_without_the_plan():
    subject, store, runtime, plans = pipeline()
    plan = subject.preview(credit_batch())
    op_id = str(uuid.uuid4())
    first = commit(subject, plan, op_id)
    restarted = CommitPipeline(store, PlanStore(), SwitchablePreflight(), InitializerRestartCheck(lambda value: value), runtime,
                               backup_directory=BACKUPS)
    for retry_subject in (subject, restarted):
        again = commit(retry_subject, plan, op_id)
        assert again.replayed is True and again.result == first.result
    assert store.state["wallet"] == 5 and store.revision == 1 and len(store.calls) == 1


def test_evicted_plan_retry_is_replayed():
    subject, store, _, plans = pipeline(plans=PlanStore(capacity=1))
    plan = subject.preview(credit_batch())
    op_id = str(uuid.uuid4())
    first = commit(subject, plan, op_id)
    subject.preview(credit_batch(1))
    assert commit(subject, plan, op_id).result == first.result and store.revision == 1


def test_reusing_an_operation_id_for_another_plan_conflicts():
    subject, store, _, _ = pipeline()
    op_id = str(uuid.uuid4())
    commit(subject, subject.preview(credit_batch(5)), op_id)
    other = subject.preview(credit_batch(7))
    with pytest.raises(AdminError) as caught:
        commit(subject, other, op_id)
    assert caught.value.code == "admin_operation_conflict" and caught.value.status == 409
    assert store.state["wallet"] == 5 and store.revision == 1


def test_replay_that_differs_from_the_preview_is_rejected():
    counter = {"value": 0}

    def drifting(state, params, context):
        counter["value"] += 1
        return CommandResult({**state, "wallet": state["wallet"] + counter["value"]})

    command = AdminCommand("wallet.drift", "wallet", AmountParams, drifting, frozenset({"wallet"}))
    subject, store, _, _ = pipeline()
    plan = subject.preview(batch((command, {"amount": 0})))
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "plan_changed" and caught.value.status == 409
    assert store.calls == [] and store.revision == 0


def test_commands_must_not_depend_on_the_operation_id():
    def stamping(state, params, context):
        return CommandResult({**state, "stamp": context.op_id})

    subject, store, _, _ = pipeline()
    plan = subject.preview(batch((AdminCommand("wallet.stamp", "wallet", AmountParams, stamping, frozenset({"wallet"})),
                                  {"amount": 0})))
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "plan_changed" and store.calls == []


def test_replay_uses_the_frozen_preview_time():
    def stamp_time(state, params, context):
        return CommandResult({**state, "stamped_at": context.now_utc.isoformat(), "day": context.acquisition_day})

    subject, store, runtime, _ = pipeline()
    plan = subject.preview(batch((AdminCommand("wallet.time", "wallet", AmountParams, stamp_time, frozenset({"wallet"})),
                                  {"amount": 0})))
    runtime.generation.clock = NOW + timedelta(minutes=3)
    commit(subject, plan)
    assert store.state["stamped_at"] == NOW.isoformat()


def test_requests_that_do_not_match_the_plan_are_rejected():
    subject, store, _, _ = pipeline()
    plan = subject.preview(credit_batch())
    for changes in ({"plan_digest": "f" * 64}, {"expected_revision": plan.expected_revision + 1}):
        with pytest.raises(AdminError) as caught:
            commit(subject, plan, **changes)
        assert caught.value.code == "plan_changed"
    with pytest.raises(AdminError) as caught:
        commit(subject, plan, plan_id="unknown")
    assert caught.value.code == "plan_expired" and caught.value.status == 409
    for changes, code in (({"op_id": "not-a-uuid"}, "admin_op_id_invalid"), ({"plan_digest": "XYZ"}, "admin_plan_digest_invalid"),
                          ({"expected_revision": -1}, "admin_revision_invalid"), ({"expected_revision": True}, "admin_revision_invalid")):
        with pytest.raises(AdminError) as caught:
            commit(subject, plan, **changes)
        assert caught.value.code == code and caught.value.status == 422
    assert store.calls == []


def test_revision_change_after_preview_returns_the_current_revision():
    subject, store, _, _ = pipeline()
    plan = subject.preview(credit_batch())
    store.revision = 4
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_revision_conflict" and caught.value.current_revision == 4
    assert caught.value.payload()["current_revision"] == 4 and store.calls == []


def test_concurrent_revision_change_during_commit_is_reported():
    subject, store, _, _ = pipeline()
    plan = subject.preview(credit_batch())

    def game_write():
        store.state, store.revision = {"wallet": 50}, store.revision + 1

    store.before_commit = game_write
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_revision_conflict" and caught.value.current_revision == 1
    assert isinstance(caught.value.__cause__, RevisionConflict)
    assert store.state == {"wallet": 50} and store.operations == {}


@pytest.mark.parametrize("raise_on_duplicate", [False, True])
def test_concurrent_duplicate_commit_applies_once(raise_on_duplicate):
    store = FakeStore({"wallet": 0}, raise_on_duplicate=raise_on_duplicate)
    subject, _, _, _ = pipeline(store=store)
    plan = subject.preview(credit_batch())
    op_id = str(uuid.uuid4())
    inner = {}
    store.before_commit = lambda: inner.setdefault("outcome", commit(subject, plan, op_id))
    outcome = commit(subject, plan, op_id)
    assert inner["outcome"].replayed is False and outcome.result == inner["outcome"].result
    assert outcome.replayed is True
    assert store.state == {"wallet": 5} and store.revision == 1 and len(store.operations) == 1


def test_unexplained_store_failures_propagate():
    subject, store, _, _ = pipeline()
    plan = subject.preview(credit_batch())

    def broken(*args):
        raise OSError("disk full")

    store.commit_admin = broken
    with pytest.raises(OSError):
        commit(subject, plan)


def test_preflight_failure_at_commit_writes_nothing():
    preflight = SwitchablePreflight()
    subject, store, _, _ = pipeline(preflight=preflight)
    plan = subject.preview(credit_batch())
    preflight.failing = True
    with pytest.raises(ProtocolError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_preflight_failed" and store.calls == [] and store.state == {"wallet": 0}
    assert preflight.calls == [["wallet"], ["wallet"]]


def test_preview_reports_preflight_failures_without_storing_the_plan():
    preflight = SwitchablePreflight()
    preflight.failing = True
    subject, store, _, plans = pipeline(preflight=preflight)
    plan = subject.preview(credit_batch())
    assert plan.committable is False and plan.plan_id is None and len(plans) == 0
    assert plan.preflight == {"ok": False, "error": {"code": "admin_preflight_failed", "message": "Response 137 could not be encoded"}}
    assert plan.audit == AUDIT_SKIPPED == {"ok": False, "skipped": "preflight"}


def test_restart_incompatibility_blocks_commit_and_lists_the_difference():
    rewrite = {"enabled": False}

    def initializer(state):
        return {**state, "migrated": True} if rewrite["enabled"] else state

    subject, store, _, _ = pipeline(restart_check=InitializerRestartCheck(initializer))
    plan = subject.preview(credit_batch())
    rewrite["enabled"] = True
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_restart_incompatible" and caught.value.status == 409
    assert caught.value.details["diff"]["entries"] == [{"op": "add", "path": "/migrated", "after": True}]
    assert store.calls == [] and store.state == {"wallet": 0}
    preview = subject.preview(credit_batch())
    assert preview.committable is False and preview.restart_check["error"]["code"] == "admin_restart_incompatible"
    assert preview.restart_check["error"]["details"]["diff"]["entries"][0]["path"] == "/migrated"


def test_restart_check_reports_initializer_errors():
    def unavailable(state):
        raise ProtocolError("vote_service_unavailable", "Existing vote history requires a verified service", 503)

    with pytest.raises(AdminError) as caught:
        InitializerRestartCheck(unavailable).check({"wallet": 1})
    assert caught.value.code == "admin_restart_incompatible"
    assert caught.value.details == {"error": {"code": "vote_service_unavailable",
                                              "message": "Existing vote history requires a verified service"}}
    with pytest.raises(AdminError) as caught:
        InitializerRestartCheck(lambda state: {**state, "bad": object()}).check({"wallet": 1})
    assert caught.value.details == {"error": {"code": "internal_error", "message": "TypeError"}}


def test_restart_check_defaults_to_the_leased_generation_initializer():
    seen = []
    generation = FakeGeneration(bundle=SimpleNamespace(initialize=lambda state: seen.append(state) or state))
    InitializerRestartCheck().check({"wallet": 1}, generation)
    assert seen == [{"wallet": 1}]
    with pytest.raises(ValueError):
        InitializerRestartCheck().check({"wallet": 1})
    with pytest.raises(ValueError):
        InitializerRestartCheck("initialize")


def test_restart_check_compares_the_stored_json_form():
    InitializerRestartCheck(lambda state: {**state, "pair": (1, 2)}).check({"wallet": 1, "pair": [1, 2]})
    with pytest.raises(AdminError):
        InitializerRestartCheck(lambda state: {**state, "flag": 1}).check({"wallet": 1, "flag": True})
    seen = []
    InitializerRestartCheck(lambda state: seen.append(state) or state).check({"wallet": 1, "pair": (1, 2)})
    assert seen == [{"wallet": 1, "pair": [1, 2]}]


def test_noop_markers_tell_real_checks_apart():
    assert NoopPreflight.is_noop and NoopRestartCheck.is_noop and InitializerRestartCheck.is_noop is False
    assert SwitchablePreflight.is_noop is False


@pytest.mark.parametrize("change, fields", [
    ({"id": "gen-b"}, ["generation_id"]),
    ({"content_versions": {"wallet": "v2"}}, ["content_versions"]),
    ({"clock": NOW + timedelta(days=1)}, ["acquisition_day", "jst_date"]),
])
def test_stale_generation_content_or_day_blocks_commit(change, fields):
    subject, store, runtime, _ = pipeline()
    plan = subject.preview(credit_batch())
    for name, value in change.items():
        setattr(runtime.generation, name, value)
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "plan_stale" and caught.value.details == {"changed": fields}
    assert store.calls == []


def test_jst_midnight_is_a_plan_boundary():
    late = datetime(2026, 11, 2, 14, 59, 30, tzinfo=timezone.utc)
    subject, store, runtime, _ = pipeline(runtime=FakeRuntime(FakeGeneration(clock=late)))
    plan = subject.preview(credit_batch())
    runtime.generation.clock = late + timedelta(minutes=1)
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "plan_stale" and "jst_date" in caught.value.details["changed"]


def test_destructive_commit_needs_the_confirmation_phrase():
    subject, store, _, _ = pipeline({"wallet": 10})
    plan = subject.preview(batch((DEBIT, {"amount": 4})))
    assert plan.describe()["confirmations"] == [{"step": 0, "operation": "wallet.debit", "phrase": "DEBIT 4"}]
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_confirmation_required" and store.calls == []
    commit(subject, plan, confirmations=["DEBIT 4"])
    assert store.state == {"wallet": 6}


def test_unchanged_state_records_audit_without_a_new_revision():
    subject, store, _, _ = pipeline({"wallet": 3})
    plan = subject.preview(credit_batch(0))
    outcome = commit(subject, plan)
    assert store.calls[0]["state"] is None and store.revision == 0 and store.state == {"wallet": 3}
    assert outcome.result["revision_before"] == outcome.result["revision_after"] == 0
    assert plan.describe()["diff"] == {"entries": [], "total": 0, "truncated": False, "omitted": 0}


def test_unavailable_commands_are_refused_before_preview():
    gated = AdminCommand("wallet.gated", "wallet", AmountParams, credit, frozenset({"wallet"}),
                         unavailable=lambda services: "Wallet service is unavailable")
    subject, store, _, plans = pipeline()
    with pytest.raises(AdminError) as caught:
        subject.preview(batch((gated, {"amount": 1})))
    assert caught.value.code == "admin_command_unavailable" and caught.value.status == 503 and len(plans) == 0


def test_command_failures_during_preview_commit_nothing():
    def rejecting(state, params, context):
        raise ProtocolError("unit_missing", "Unit does not exist", 404)

    command = AdminCommand("units.reject", "units", AmountParams, rejecting, frozenset({"units"}))
    subject, store, _, plans = pipeline()
    with pytest.raises(AdminError) as caught:
        subject.preview(batch((CREDIT, {"amount": 1}), (command, {"amount": 1})))
    assert caught.value.code == "unit_missing" and caught.value.status == 404 and caught.value.details["step"] == 1
    assert store.state == {"wallet": 0} and len(plans) == 0


def test_commands_receive_the_leased_generation_services():
    seen = []

    def inspect(state, params, context):
        seen.append(context.services)
        return CommandResult(deepcopy(state))

    services = SimpleNamespace(name="bundle")
    subject, _, runtime, _ = pipeline(runtime=FakeRuntime(FakeGeneration(bundle=services)))
    plan = subject.preview(batch((AdminCommand("wallet.inspect", "wallet", AmountParams, inspect, frozenset({"wallet"})),
                                  {"amount": 0})))
    commit(subject, plan)
    assert seen == [services, services] and plan.context.services is None


def test_pipeline_rejects_incomplete_collaborators():
    store, plans = FakeStore({"wallet": 0}), PlanStore()
    preflight, restart, runtime = SwitchablePreflight(), InitializerRestartCheck(lambda value: value), FakeRuntime()
    without_backup = SimpleNamespace(profile=store.profile, find_admin_operation=store.find_admin_operation,
                                     commit_admin=store.commit_admin)
    for arguments in ((object(), plans, preflight, restart, runtime), (store, object(), preflight, restart, runtime),
                      (store, plans, object(), restart, runtime), (store, plans, preflight, restart, object()),
                      (store, plans, SimpleNamespace(check=lambda *args: None), restart, runtime),
                      (without_backup, plans, preflight, restart, runtime)):
        with pytest.raises(ValueError):
            CommitPipeline(*arguments, backup_directory=BACKUPS)
    with pytest.raises(ValueError):
        CommitPipeline(store, plans, preflight, restart, runtime, backup_directory=str(BACKUPS))


def large_save(units, items):
    return {"player_state": {"units": [{"CardID": 1 + index % 300, "ClassID": 1, "Exp": index, "GetIndex": index + 1,
                                        "UniqueID": index + 1, "Love": 0} for index in range(units)]},
            "tables": {"possessions": [{"ItemID": f"Item{index:05d}", "Count": index} for index in range(items)]},
            "login_reward_history": {"schema_version": 2,
                                     "receipts": [{"request": f"{index:064x}", "response": "<?xml version=\"1.0\"?>\n<DA/>"}
                                                  for index in range(16)],
                                     "processed_keys": [f"{index:064x}" for index in range(256)]}}


def test_large_saves_round_trip_through_preview_and_commit():
    state = large_save(5000, 4000)
    subject, store, _, _ = pipeline(state)
    plan = subject.preview(batch((UNIT_EDIT, {"unique_id": 4321, "exp": 999999})))
    assert plan.describe()["diff"]["entries"] == [{
        "op": "replace", "path": "/player_state/units", "key": {"UniqueID": 4321}, "label": {"GetIndex": 4321, "CardID": 121},
        "field": "/Exp", "before": 4320, "after": 999999}]
    outcome = commit(subject, plan)
    assert outcome.result["revision_after"] == 1
    assert store.state["player_state"]["units"][4320]["Exp"] == 999999
    assert store.state["tables"] == state["tables"]
    record_diff = next(iter(store.operations.values()))["diff"]
    assert len(serialize(record_diff).encode()) <= AUDIT_DIFF_LIMITS.max_bytes + 512


def test_audit_diffs_are_bounded_and_redacted():
    def rewrite_receipts(state, params, context):
        return CommandResult({**state, "ledger": [{"user_key": "secret", "value": index} for index in range(params.amount)]})

    command = AdminCommand("wallet.rewrite", "wallet", AmountParams, rewrite_receipts, frozenset({"wallet"}))
    subject, store, _, _ = pipeline({"wallet": 0, "ledger": []})
    plan = subject.preview(batch((command, {"amount": 400})))
    commit(subject, plan)
    record = next(iter(store.operations.values()))
    assert record["diff"]["total"] == 400 and record["diff"]["truncated"] is True
    assert len(record["diff"]["entries"]) <= AUDIT_DIFF_LIMITS.max_entries
    assert "secret" not in serialize(record) and "secret" not in serialize(plan.describe())


def sqlite_pipeline(tmp_path, state):
    return pipeline(store=Store(tmp_path / "admin.sqlite3", state), backup_directory=tmp_path / "backups")


def test_pipeline_commits_and_replays_through_the_sqlite_store(tmp_path):
    subject, store, _, _ = sqlite_pipeline(tmp_path, {"wallet": 1})
    plan = subject.preview(credit_batch(5))
    op_id = str(uuid.uuid4())
    outcome = commit(subject, plan, op_id)
    profile = store.profile()
    assert profile.state == {"wallet": 6} and profile.revision == 1 and outcome.replayed is False
    record = store.find_admin_operation(op_id)
    assert record["result"] == outcome.result and record["payload_digest"] == admin_payload_digest(op_id, plan.plan_digest)
    assert (record["revision_before"], record["revision_after"], record["kind"], record["actor"]) == (0, 1, COMMAND_KIND, ADMIN_ACTOR)
    assert record["state_sha256_after"] == json_digest(profile.state) and record["generation_id"] == "gen-a"
    assert record["content_versions"] == {"wallet": "v1"} and record["targets"] == ["wallet"]
    again = commit(subject, plan, op_id)
    assert again.replayed is True and again.result == outcome.result and store.profile().revision == 1
    with pytest.raises(AdminError) as caught:
        commit(subject, subject.preview(credit_batch(2)), op_id)
    assert caught.value.code == "admin_operation_conflict" and store.profile().state == {"wallet": 6}


def test_sqlite_store_reports_revision_conflicts_and_audits_unchanged_state(tmp_path):
    subject, store, _, _ = sqlite_pipeline(tmp_path, {"wallet": 3})
    plan = subject.preview(credit_batch(1))
    store.update_state({"wallet": 9}, 0)
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_revision_conflict" and caught.value.current_revision == 1
    outcome = commit(subject, subject.preview(credit_batch(0)))
    newest = store.list_admin_operations().items[0]
    assert outcome.result["revision_before"] == outcome.result["revision_after"] == 1 and store.profile().revision == 1
    assert newest["state_sha256_before"] == newest["state_sha256_after"] == json_digest({"wallet": 9})


def test_sqlite_store_answers_a_duplicate_commit_race_once(tmp_path):
    subject, store, _, _ = sqlite_pipeline(tmp_path, {"wallet": 0})
    plan = subject.preview(credit_batch())
    op_id, original, inner = str(uuid.uuid4()), store.commit_admin, {}

    def racing(*arguments):
        store.commit_admin = original
        inner["outcome"] = commit(subject, plan, op_id)
        return original(*arguments)

    store.commit_admin = racing
    outcome = commit(subject, plan, op_id)
    assert inner["outcome"].replayed is False and outcome.replayed is True and outcome.result == inner["outcome"].result
    assert store.profile().state == {"wallet": 5} and store.profile().revision == 1
    assert len(store.list_admin_operations().items) == 1


def test_pipeline_errors_can_be_recorded_as_admin_failures(tmp_path):
    subject, store, _, _ = sqlite_pipeline(tmp_path, {"wallet": 0})
    plan = subject.preview(batch((DEBIT, {"amount": 1})))
    op_id = str(uuid.uuid4())
    with pytest.raises(AdminError) as caught:
        commit(subject, plan, op_id)
    error = caught.value
    assert type(error.status) is int
    failure = store.record_admin_failure(COMMAND_KIND, error.code, error.status, str(error), op_id=op_id,
                                         detail=error.payload())
    assert (failure["error_code"], failure["status"], failure["op_id"]) == ("admin_confirmation_required", 422, op_id)
    assert failure["detail"] == error.payload() and store.profile().revision == 0


def test_backups_are_required_for_batches_and_destructive_commands():
    assert requires_backup(credit_batch()) is False
    assert requires_backup(batch((DEBIT, {"amount": 1}))) is True
    assert requires_backup(batch((CREDIT, {"amount": 1}), (CREDIT, {"amount": 2}))) is True
    assert requires_backup(batch((SCALED_DEBIT, {"amount": LARGE_DEBIT - 1}))) is False
    assert requires_backup(batch((SCALED_DEBIT, {"amount": LARGE_DEBIT}))) is True
    assert backup_reason(batch((DEBIT, {"amount": 1}))) == "before wallet.debit"
    assert backup_reason(batch((CREDIT, {"amount": 1}), (DEBIT, {"amount": 1}), (CREDIT, {"amount": 2}))) == \
        "before wallet.credit and 2 more"
    long_name = "wallet." + "a" * (MAX_BACKUP_REASON * 2)
    command = AdminCommand(long_name, "wallet", AmountParams, credit, frozenset({"wallet"}), destructive=True,
                           confirm_phrase=lambda params: "CONFIRM")
    assert len(backup_reason(batch((command, {"amount": 1})))) == MAX_BACKUP_REASON


def test_single_ordinary_command_takes_no_backup():
    subject, store, _, _ = pipeline()
    outcome = commit(subject, subject.preview(credit_batch()))
    assert store.backups == [] and outcome.result["backup_before_commit"] is None


def test_destructive_command_is_backed_up_before_it_commits():
    subject, store, _, _ = pipeline({"wallet": 10})
    plan = subject.preview(batch((DEBIT, {"amount": 4})))
    op_id = str(uuid.uuid4())
    outcome = commit(subject, plan, op_id, confirmations=["DEBIT 4"])
    [taken] = store.backups
    assert taken["row"]["kind"] == AUTO_BACKUP_KIND and taken["row"]["op_id"] == op_id
    assert taken["row"]["reason"] == "before wallet.debit" and taken["row"]["path"] == str(BACKUPS / "copy-1.sqlite3")
    assert taken["state"] == {"wallet": 10} and taken["commits_before"] == 0
    assert outcome.result["backup_before_commit"] == taken["row"] and store.state == {"wallet": 6}
    assert store.operations[op_id]["result"]["backup_before_commit"] == taken["row"]


def test_multi_step_batch_is_backed_up_before_it_commits():
    subject, store, _, _ = pipeline()
    outcome = commit(subject, subject.preview(batch((CREDIT, {"amount": 1}), (CREDIT, {"amount": 2}))))
    [taken] = store.backups
    assert taken["row"]["reason"] == "before wallet.credit and 1 more" and taken["state"] == {"wallet": 0}
    assert outcome.result["backup_before_commit"]["id"] == 1 and store.state == {"wallet": 3}


@pytest.mark.parametrize("failure, code", [("confirmation", "admin_confirmation_required"),
                                           ("revision", "admin_revision_conflict"),
                                           ("preflight", "admin_preflight_failed"),
                                           ("restart", "admin_restart_incompatible")])
def test_commits_that_fail_their_checks_take_no_backup(failure, code):
    preflight, rewrite = SwitchablePreflight(), {"enabled": False}
    restart = InitializerRestartCheck(lambda state: {**state, "migrated": True} if rewrite["enabled"] else state)
    subject, store, _, _ = pipeline({"wallet": 10}, preflight=preflight, restart_check=restart)
    plan = subject.preview(batch((DEBIT, {"amount": 4})))
    if failure == "revision":
        store.revision += 1
    preflight.failing = failure == "preflight"
    rewrite["enabled"] = failure == "restart"
    with pytest.raises(ProtocolError) as caught:
        commit(subject, plan, confirmations=[] if failure == "confirmation" else ["DEBIT 4"])
    assert caught.value.code == code and store.backups == [] and store.calls == []


def test_replayed_operation_takes_no_new_backup():
    subject, store, _, _ = pipeline({"wallet": 10})
    plan = subject.preview(batch((DEBIT, {"amount": 4})))
    op_id = str(uuid.uuid4())
    first = commit(subject, plan, op_id, confirmations=["DEBIT 4"])
    again = commit(subject, plan, op_id, confirmations=["DEBIT 4"])
    assert again.replayed is True and again.result == first.result and len(store.backups) == 1


def test_failed_backup_aborts_the_commit_with_nothing_written():
    subject, store, _, plans = pipeline({"wallet": 10})
    plan = subject.preview(batch((DEBIT, {"amount": 4})))
    store.backup_error = ProtocolError("admin_backup_failed", "The online backup could not be verified and registered", 503)
    with pytest.raises(AdminError) as caught:
        commit(subject, plan, confirmations=["DEBIT 4"])
    assert caught.value.code == BACKUP_FAILED and caught.value.status == 503
    assert caught.value.details == {"error": {"code": "admin_backup_failed",
                                              "message": "The online backup could not be verified and registered"}}
    assert store.calls == [] and store.operations == {} and store.state == {"wallet": 10} and len(plans) == 1


@pytest.mark.parametrize("steps", [((DEBIT, {"amount": 0}),), ((CREDIT, {"amount": 0}), (CREDIT, {"amount": 0}))])
def test_commits_that_change_nothing_keep_the_previous_automatic_backup(steps):
    subject, store, _, _ = pipeline({"wallet": 10})
    commit(subject, subject.preview(batch((DEBIT, {"amount": 1}))), confirmations=["DEBIT 1"])
    [earlier] = store.backups
    plan = subject.preview(batch(*steps))
    assert requires_backup(plan.batch) and plan.changed is False and takes_backup(plan) is False
    outcome = commit(subject, plan, confirmations=[item.phrase for item in plan.confirmations])
    assert store.backups == [earlier] and outcome.result["backup_before_commit"] is None
    assert store.calls[-1]["state"] is None and store.revision == 1 and store.state == {"wallet": 9}


def test_changed_destructive_and_batch_plans_take_the_backup():
    subject, _, _, _ = pipeline({"wallet": 10})
    assert takes_backup(subject.preview(batch((DEBIT, {"amount": 1})))) is True
    assert takes_backup(subject.preview(batch((CREDIT, {"amount": 1}), (CREDIT, {"amount": 0})))) is True
    assert takes_backup(subject.preview(credit_batch(1))) is False


def test_parameter_decided_commands_confirm_and_back_up_only_when_destructive():
    subject, store, _, _ = pipeline({"wallet": 10})
    small = subject.preview(batch((SCALED_DEBIT, {"amount": 1})))
    assert small.confirmations == () and takes_backup(small) is False
    commit(subject, small)
    assert store.backups == [] and store.state == {"wallet": 9}
    large = subject.preview(batch((SCALED_DEBIT, {"amount": LARGE_DEBIT})))
    assert [item.phrase for item in large.confirmations] == [f"DEBIT {LARGE_DEBIT}"] and takes_backup(large) is True
    with pytest.raises(AdminError) as caught:
        commit(subject, large)
    assert caught.value.code == "admin_confirmation_required" and store.backups == []
    outcome = commit(subject, large, confirmations=[f"DEBIT {LARGE_DEBIT}"])
    [taken] = store.backups
    assert taken["state"] == {"wallet": 9} and outcome.result["backup_before_commit"] == taken["row"]
    assert store.state == {"wallet": 9 - LARGE_DEBIT}


@pytest.mark.parametrize("steps, field", [
    (((PADDED_CREDIT, {"amount": MAX_AUDIT_FIELD_BYTES}), (CREDIT, {"amount": 1})), "result"),
    (((NOTE, {"note": "n" * MAX_AUDIT_FIELD_BYTES}), (CREDIT, {"amount": 1})), "operations"),
])
def test_oversized_audit_records_are_refused_at_preview(steps, field):
    subject, store, _, plans = pipeline({"wallet": 0})
    plan = subject.preview(batch(*steps))
    assert plan.committable is False and plan.plan_id is None and len(plans) == 0
    assert plan.preflight["ok"] is True and plan.restart_check == {"ok": True} and plan.audit["ok"] is False
    error, details = plan.audit["error"], plan.audit["error"]["details"]
    assert error["code"] == AUDIT_TOO_LARGE and list(details["fields"]) == [field]
    assert details["fields"][field] > MAX_AUDIT_FIELD_BYTES and details["limit"] == MAX_AUDIT_FIELD_BYTES
    assert plan.describe()["audit"] == plan.audit and store.backups == [] and store.calls == []


def test_previews_refuse_plans_whose_audit_record_the_store_would_reject(monkeypatch):
    monkeypatch.setattr(admin_commit, "MAX_AUDIT_FIELD_BYTES", 1)
    subject, store, _, plans = pipeline()
    plan = subject.preview(credit_batch())
    assert plan.committable is False and plan.plan_id is None and len(plans) == 0
    assert plan.audit["ok"] is False and plan.audit["error"]["code"] == AUDIT_TOO_LARGE
    assert store.calls == [] and store.backups == []


def test_previews_measure_the_record_with_a_canonical_placeholder_operation_id():
    subject, _, _, plans = pipeline()
    plan = subject.preview(credit_batch())
    assert plan.committable and plan.audit == {"ok": True} and len(plans) == 1
    assert require_op_id(PREVIEW_OP_ID) == PREVIEW_OP_ID


def test_records_that_outgrow_the_limit_after_preview_are_refused_before_any_backup(monkeypatch):
    subject, store, _, plans = pipeline({"wallet": 0})
    plan = subject.preview(batch((CREDIT, {"amount": 1}), (CREDIT, {"amount": 1})))
    assert plan.committable and takes_backup(plan)
    monkeypatch.setattr(admin_commit, "MAX_AUDIT_FIELD_BYTES", 1)
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert (caught.value.code, caught.value.status) == (AUDIT_TOO_LARGE, 422)
    assert tuple(caught.value.details["fields"]) == AUDIT_JSON_FIELDS and caught.value.details["limit"] == 1
    assert store.backups == [] and store.calls == [] and store.state == {"wallet": 0} and len(plans) == 1


def test_the_backup_row_is_measured_with_the_stored_result():
    subject, store, _, _ = pipeline({"wallet": 10})
    plan = subject.preview(batch((DEBIT, {"amount": 4})))
    registered = store.backup
    store.backup = lambda *arguments, **options: {**registered(*arguments, **options), "path": "p" * MAX_AUDIT_FIELD_BYTES}
    with pytest.raises(AdminError) as caught:
        commit(subject, plan, confirmations=["DEBIT 4"])
    assert caught.value.code == AUDIT_TOO_LARGE and list(caught.value.details["fields"]) == ["result"]
    assert len(store.backups) == 1 and store.calls == [] and store.state == {"wallet": 10}


def audit_record(result):
    return {"kind": COMMAND_KIND, "operations": [{"operation": "wallet.note", "params": {}}], "targets": ["wallet"],
            "diff": {"entries": []}, "result": result}


def test_audit_fields_are_the_json_columns_of_the_operation_table():
    assert AUDIT_JSON_FIELDS == tuple(name for name in _JSON_COLUMNS.values() if name != "detail") == (
        "operations", "targets", "diff", "content_versions", "result")


def test_audit_size_check_matches_the_store_limit(tmp_path):
    store = Store(tmp_path / "admin.sqlite3", {"wallet": 0})
    # A JSON string adds its two quotes to the stored bytes.
    largest, oversized = "x" * (MAX_AUDIT_FIELD_BYTES - 2), "x" * (MAX_AUDIT_FIELD_BYTES - 1)
    assert require_auditable(audit_record(largest)) == audit_record(largest)
    store.commit_admin(str(uuid.uuid4()), "a" * 64, 0, None, audit_record(largest))
    with pytest.raises(ValueError, match="audit size limit"):
        store.commit_admin(str(uuid.uuid4()), "a" * 64, 0, None, audit_record(oversized))
    with pytest.raises(AdminError) as caught:
        commit_audited(store, str(uuid.uuid4()), "a" * 64, 0, None, audit_record(oversized))
    assert caught.value.details == {"fields": {"result": MAX_AUDIT_FIELD_BYTES + 1}, "limit": MAX_AUDIT_FIELD_BYTES}
    assert len(store.list_admin_operations().items) == 1 and store.profile().revision == 0


def test_sqlite_destructive_commit_registers_the_automatic_backup(tmp_path):
    subject, store, _, _ = sqlite_pipeline(tmp_path, {"wallet": 10})
    plan = subject.preview(batch((DEBIT, {"amount": 4})))
    op_id = str(uuid.uuid4())
    outcome = commit(subject, plan, op_id, confirmations=["DEBIT 4"])
    [registered] = store.list_backups().items
    assert (registered["kind"], registered["op_id"], registered["reason"]) == (AUTO_BACKUP_KIND, op_id, "before wallet.debit")
    assert outcome.result["backup_before_commit"] == registered
    snapshot = read_backup(Path(registered["path"]), root=tmp_path)
    assert snapshot.profile.state == {"wallet": 10} and snapshot.profile.revision == 0
    assert store.profile().state == {"wallet": 6} and store.find_admin_operation(op_id)["result"] == outcome.result


def test_sqlite_failed_backup_writes_nothing_and_can_be_recorded(tmp_path):
    subject, store, _, _ = sqlite_pipeline(tmp_path, {"wallet": 10})
    (tmp_path / "backups").write_bytes(b"not a directory")
    plan = subject.preview(batch((DEBIT, {"amount": 4})))
    op_id = str(uuid.uuid4())
    with pytest.raises(AdminError) as caught:
        commit(subject, plan, op_id, confirmations=["DEBIT 4"])
    error = caught.value
    assert error.code == BACKUP_FAILED and error.details["error"]["code"] == "admin_backup_path_invalid"
    assert store.profile().state == {"wallet": 10} and store.profile().revision == 0
    assert store.list_admin_operations().items == () and store.find_admin_operation(op_id) is None
    failure = store.record_admin_failure(COMMAND_KIND, error.code, error.status, str(error), op_id=op_id,
                                         detail=error.payload())
    assert (failure["error_code"], failure["status"], failure["op_id"]) == (BACKUP_FAILED, 503, op_id)
    assert store.list_backups().items == ()

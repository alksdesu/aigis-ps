import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import uuid

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import Field

from aigis_ps import admin_commit, admin_routes_backup, admin_routes_state
from aigis_ps.admin_app import BACKUP_DIRECTORY, EXPORT_DIRECTORY, LeasedGeneration, create_admin_app
from aigis_ps.admin_command import AdminCommand, AdminError, CommandParams, CommandResult, PlanStore, PreflightReport
from aigis_ps.admin_commit import AUDIT_TOO_LARGE, BACKUP_FAILED, InitializerRestartCheck
from aigis_ps.admin_diff import REDACTED, json_digest
from aigis_ps.admin_gates import (CLIENT_QUIET_SECONDS, MAX_RESTART_NOTICES, ClientGate, active_battle,
                                  require_no_active_battle)
from aigis_ps.admin_offline import CROSS_ACCOUNT_PHRASE
from aigis_ps.admin_registry import (AdminCatalog, AdminRoute, AdminView, CommandRegistry, RouteRegistry, ViewRegistry,
                                     discover)
from aigis_ps.admin_security import API_ROOT, CONTENT_SECURITY_POLICY
from aigis_ps.business_calendar import civil_day
from aigis_ps.energy_clock import JST, mark_new
from aigis_ps.errors import ProtocolError
from aigis_ps.generation import Generation, GenerationIdentity, GenerationRuntime
from aigis_ps.storage import AUTO_BACKUP_KIND, MAX_AUDIT_FIELD_BYTES, MAX_REVISION, Store, read_backup


NOW = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)
PORT = 18104
GAME_PORT = 18103
TOKEN = "k" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = f"http://127.0.0.1:{PORT}"
DATABASE = "aigis.sqlite3"
SHORT = 0.2
SECRET = "do-not-leak-0123456789"
DOMAINS = {"wallet": "137"}
LARGE_DEBIT = 5
SYSTEM_RECEIPT = f'<DA><SYSTEM><A1 T="S"><V>ExchangeKey</V></A1><A2 T="S"><V>{SECRET}</V></A2></SYSTEM></DA>'
# alxml.loads reads each of these ahead of the root element.
XML_PROLOGS = ("\ufeff", '\ufeff<?xml version="1.0"?>\n', " \r\n\t", "<!-- saved receipt -->\n", "<?receipt kept?>")


class Monotonic:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Wall:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class AmountParams(CommandParams):
    amount: int = Field(ge=0)


class SummaryParams(CommandParams):
    scale: int = Field(default=1, ge=1)


def credit(state, params, context):
    return CommandResult({**state, "wallet": state["wallet"] + params.amount}, {"credited": params.amount})


def debit(state, params, context):
    if state["wallet"] < params.amount:
        raise ProtocolError("wallet_insufficient", "The wallet cannot cover this debit", 409)
    return CommandResult({**state, "wallet": state["wallet"] - params.amount}, {"debited": params.amount})


def padded_credit(state, params, context):
    return CommandResult({**state, "wallet": state["wallet"] + 1}, {"padding": "x" * params.amount})


def debit_phrase(params):
    return f"DEBIT {params.amount}"


CREDIT = AdminCommand("wallet.credit", "wallet", AmountParams, credit, frozenset({"wallet"}),
                      writes=frozenset({"game_param.Money"}))
DEBIT = AdminCommand("wallet.debit", "wallet", AmountParams, debit, frozenset({"wallet"}), destructive=True,
                     confirm_phrase=debit_phrase)
SCALED_DEBIT = AdminCommand("wallet.scaled_debit", "wallet", AmountParams, debit, frozenset({"wallet"}),
                            destructive=lambda params: params.amount >= LARGE_DEBIT, confirm_phrase=debit_phrase)
GATED = AdminCommand("wallet.gated", "wallet", AmountParams, credit, frozenset({"wallet"}),
                     unavailable=lambda services: "Wallet service is offline")
PADDED_CREDIT = AdminCommand("wallet.padded_credit", "wallet", AmountParams, padded_credit, frozenset({"wallet"}))


def summary(context, params):
    return {"wallet": context.state["wallet"] * params.scale, "revision": context.revision,
            "generation": context.generation.id, "now": context.now.isoformat(),
            "leases": context.services.runtime.status()["current"]["leases"]}


CLOCK = AdminView("wallet.clock", "wallet", lambda context, params: {"now": context.now.isoformat()}, live=True)


class ReadbackPreflight:
    is_noop = False

    def __init__(self):
        self.calls = []

    def check(self, before, candidate, domains, generation):
        self.calls.append(sorted(domains))
        return PreflightReport({domain: (137,) for domain in domains})


def clocked(**fields):
    return mark_new({"bootstrap_schema": 1, "tables": {"game_param": [{"Money": 100}]}, "wallet": 0, **fields}, NOW)


def generation(wall, *, config_set="first"):
    bundle = SimpleNamespace(bootstrap=SimpleNamespace(clock=wall), initialize=lambda state: state)
    identity = GenerationIdentity(code_digest="c" * 64, config_set=config_set, content_versions={"wallet": "w1"})
    readiness = {"bootstrap": {"startup_ready": True}, "wallet": {"ready": True, "user_key": SECRET}}
    return Generation(identity, bundle, {}, readiness)


def catalog(*extra_commands, extra_views=()):
    commands = CommandRegistry(DOMAINS)
    for command in (CREDIT, DEBIT, GATED, *extra_commands):
        commands.add(command)
    views = ViewRegistry()
    for view in (AdminView("wallet.summary", "wallet", summary, SummaryParams),
                 AdminView("wallet.plain", "wallet", lambda context, params: {"params": params}), *extra_views):
        views.add(view)
    routes = RouteRegistry()
    admin_routes_state.register(routes)
    admin_routes_backup.register(routes)
    return AdminCatalog(commands, views, routes, ("admin_routes_backup", "admin_routes_state"))


@dataclass
class Console:
    client: TestClient
    store: Store
    runtime: GenerationRuntime
    clock: Monotonic
    wall: Wall
    data: Path
    preflight: ReadbackPreflight

    def get(self, path, *, headers=None, params=None):
        return self.client.get(API_ROOT + path, headers={**AUTH, **(headers or {})}, params=params)

    def post(self, path, body):
        return self.client.post(API_ROOT + path, json=body, headers=AUTH)


def build_console(tmp_path, *, state=None, restart_check=None, ui_directory=None, admin_catalog=None):
    data = tmp_path / "data"
    wall, clock = Wall(), Monotonic()
    store = Store(data / DATABASE, clocked() if state is None else state, clock=wall)
    runtime = GenerationRuntime(generation(wall), clock=clock, admit_timeout=SHORT, drain_timeout=SHORT,
                                draining_limit=SHORT)
    preflight = ReadbackPreflight()
    application = create_admin_app(
        runtime, store, data_dir=data, admin_host="127.0.0.1", admin_port=PORT, game_port=GAME_PORT, token=TOKEN,
        preflight=preflight, restart_check=restart_check or InitializerRestartCheck(), plan_store=PlanStore(),
        preflight_domains=DOMAINS, catalog=admin_catalog or catalog(), ui_directory=ui_directory or tmp_path / "ui",
        clock=clock)
    return Console(TestClient(application, base_url=BASE), store, runtime, clock, wall, data, preflight)


@pytest.fixture
def console(tmp_path):
    return build_console(tmp_path)


def error_of(response):
    return response.json()["error"]


def plan_commands(console, *commands):
    response = console.post("/plans", {"commands": [{"operation": name, "params": params} for name, params in commands]})
    assert response.status_code == 200, response.text
    return response.json()


def commit_body(plan, op_id=None, **changes):
    body = {"plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "expected_revision": plan["expected_revision"],
            "op_id": op_id or str(uuid.uuid4())}
    body.update(changes)
    return body


def failures(console):
    return console.get("/audit/failures").json()["items"]


def snapshot(console, reason="manual snapshot"):
    return console.store.backup(console.store.path.parent / BACKUP_DIRECTORY, reason, kind="manual")["path"]


def advance_state(console, **changes):
    profile = console.store.profile()
    console.store.update_state({**profile.state, **changes}, profile.revision)


def file_backup(console, name, state, *, player_id=None):
    path = console.store.path.parent / BACKUP_DIRECTORY / name
    Store(path, state)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        if player_id is not None:
            connection.execute("UPDATE profiles SET player_id=?", (player_id,))
        connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        connection.close()
    return str(path)


def preview_restore(console, backup):
    response = console.post("/restores/preview", {"backup": backup})
    assert response.status_code == 200, response.text
    return response.json()


def restore_body(plan, *, op_id=None, client_closed=True, confirmations=None):
    return {"backup": plan["backup"]["path"], "restore_digest": plan["restore_digest"],
            "expected_revision": plan["expected_revision"], "op_id": op_id or str(uuid.uuid4()),
            "client_closed": client_closed,
            "confirmations": [item["phrase"] for item in plan["confirmations"]] if confirmations is None else confirmations}


def battle_journal():
    return {"schema_version": 1, "next_sequence": 2, "last_client_time": None, "history": [],
            "active": {"sequence": 1, "quest_id": 100, "start_key": "a" * 64, "master_snapshot": "snapshot-1",
                       "resource_version": "resource-1"}}


def test_api_requires_the_token_and_answers_with_security_headers(console):
    refused = console.client.get(API_ROOT + "/session")
    assert refused.status_code == 401 and error_of(refused)["code"] == "admin_unauthorized"
    response = console.get("/session")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["cache-control"] == "no-store" and response.headers["x-content-type-options"] == "nosniff"
    foreign = console.get("/session", headers={"host": "evil.example:18104"})
    assert foreign.status_code == 400 and error_of(foreign)["code"] == "admin_host_invalid"
    game = console.client.post(API_ROOT + "/plans", json={"commands": []},
                               headers={**AUTH, "origin": f"http://127.0.0.1:{GAME_PORT}"})
    assert game.status_code == 403 and error_of(game)["code"] == "admin_origin_denied"
    localhost = console.client.get(API_ROOT + "/session", headers={**AUTH, "host": f"localhost:{PORT}",
                                                                   "origin": f"http://localhost:{PORT}"})
    assert localhost.status_code == 200


def test_unknown_routes_and_methods_use_the_admin_error_shape(console):
    missing = console.get("/missing")
    assert missing.status_code == 404 and error_of(missing)["code"] == "admin_not_found"
    wrong = console.post("/session", {})
    assert wrong.status_code == 405 and error_of(wrong)["code"] == "admin_method_not_allowed"
    options = console.client.options(API_ROOT + "/plans", headers=AUTH)
    assert options.status_code == 405 and error_of(options)["code"] == "admin_method_not_allowed"


def test_session_reports_identity_time_and_checks(console):
    body = console.get("/session").json()
    profile = console.store.profile()
    assert body["actor"] == "local-admin" and body["player_id"] == "1" and body["revision"] == 0
    assert body["server_now"] == {"utc": NOW.isoformat(), "jst": NOW.astimezone(JST).isoformat()}
    assert body["generation_id"] == console.runtime.current.id
    assert body["checks"] == {"preflight_noop": False, "restart_check_noop": False}
    assert body["admin"] == {"host": "127.0.0.1", "port": PORT, "game_port": GAME_PORT,
                             "console": f"http://127.0.0.1:{PORT}/admin/"}
    assert body["client"]["quiet"] is True and body["client"]["restart_required"] is False
    text = json.dumps(body)
    assert profile.user_key not in text and profile.method_key not in text


def test_runtime_and_readiness_report_the_running_generation(console):
    runtime = console.get("/runtime").json()
    assert runtime["runtime"]["current"]["id"] == console.runtime.current.id and runtime["runtime"]["fenced"] is False
    assert runtime["identity"]["config_set"] == "first" and runtime["identity"]["content_versions"] == {"wallet": "w1"}
    assert runtime["readiness_domains"] == ["bootstrap", "wallet"] and runtime["gate_issues"] == []
    readiness = console.get("/readiness/wallet").json()
    assert readiness == {"domain": "wallet", "readiness": {"ready": True, "user_key": "[redacted]"}}
    unknown = console.get("/readiness/unknown")
    assert unknown.status_code == 404 and error_of(unknown)["code"] == "admin_readiness_unknown"


def test_command_catalog_lists_availability_schemas_and_field_owners(console):
    body = console.get("/commands").json()
    commands = {item["operation"]: item for item in body["commands"]}
    assert list(commands) == ["wallet.credit", "wallet.debit", "wallet.gated"]
    assert commands["wallet.credit"]["available"] is True and commands["wallet.credit"]["unavailable_reason"] is None
    assert commands["wallet.gated"]["available"] is False
    assert commands["wallet.gated"]["unavailable_reason"] == "Wallet service is offline"
    assert commands["wallet.debit"]["destructive"] is True
    assert commands["wallet.debit"]["params_schema"]["properties"]["amount"]["type"] == "integer"
    assert body["field_owners"] == {"game_param.Money": "wallet.credit"}


def test_plan_then_commit_applies_once_and_is_audited(console):
    plan = plan_commands(console, ("wallet.credit", {"amount": 5}))
    assert plan["committable"] is True and plan["plan_id"] and plan["auto_backup"] is False
    assert plan["diff"]["entries"] == [{"op": "replace", "path": "/wallet", "before": 0, "after": 5}]
    assert plan["preflight"] == {"ok": True, "readback": {"wallet": [137]}} and plan["restart_check"] == {"ok": True}
    assert plan["audit"] == {"ok": True}
    op_id = str(uuid.uuid4())
    response = console.post("/commits", commit_body(plan, op_id))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["replayed"] is False and result["op_id"] == op_id and result["kind"] == "command"
    assert (result["revision_before"], result["revision_after"]) == (0, 1) and result["backup_before_commit"] is None
    assert result["readback"] == {"wallet": [137]} and console.store.profile().state["wallet"] == 5
    [record] = console.get("/audit").json()["items"]
    assert record["op_id"] == op_id and record["kind"] == "command" and record["targets"] == ["wallet"]
    assert record["result"] == {name: value for name, value in result.items() if name != "replayed"}
    assert console.store.list_backups().items == () and failures(console) == []
    assert console.runtime.status()["current"]["leases"] == 0


def test_plans_freeze_the_leased_generation_clock(console):
    plan = plan_commands(console, ("wallet.credit", {"amount": 1}))
    assert plan["context"]["now"] == NOW.isoformat() and plan["generation_id"] == console.runtime.current.id
    assert plan["context"]["acquisition_day"] == civil_day(NOW)
    assert plan["context"]["content_versions"] == {"wallet": "w1"}


def test_retrying_an_operation_id_never_applies_twice(console):
    plan = plan_commands(console, ("wallet.credit", {"amount": 5}))
    op_id = str(uuid.uuid4())
    first = console.post("/commits", commit_body(plan, op_id)).json()
    again = console.post("/commits", commit_body(plan, op_id)).json()
    assert again == {**first, "replayed": True}
    assert console.store.profile().state["wallet"] == 5 and console.store.profile().revision == 1
    other = plan_commands(console, ("wallet.credit", {"amount": 2}))
    conflict = console.post("/commits", commit_body(other, op_id))
    assert conflict.status_code == 409 and error_of(conflict)["code"] == "admin_operation_conflict"
    assert len(console.get("/audit").json()["items"]) == 1


def test_revision_conflict_returns_the_current_revision_and_is_recorded(console):
    plan = plan_commands(console, ("wallet.credit", {"amount": 5}))
    advance_state(console, wallet=50)
    op_id = str(uuid.uuid4())
    response = console.post("/commits", commit_body(plan, op_id))
    assert response.status_code == 409
    assert error_of(response)["code"] == "admin_revision_conflict" and error_of(response)["current_revision"] == 1
    assert console.store.profile().state["wallet"] == 50
    [failure] = failures(console)
    assert (failure["kind"], failure["error_code"], failure["status"], failure["op_id"]) == (
        "command", "admin_revision_conflict", 409, op_id)


def test_destructive_and_batch_commits_back_up_first(tmp_path):
    console = build_console(tmp_path, state=clocked(wallet=10))
    plan = plan_commands(console, ("wallet.debit", {"amount": 4}))
    assert plan["auto_backup"] is True and plan["confirmations"] == [
        {"step": 0, "operation": "wallet.debit", "phrase": "DEBIT 4"}]
    op_id = str(uuid.uuid4())
    result = console.post("/commits", commit_body(plan, op_id, confirmations=["DEBIT 4"])).json()
    first = result["backup_before_commit"]
    assert (first["kind"], first["op_id"], first["reason"]) == (AUTO_BACKUP_KIND, op_id, "before wallet.debit")
    assert read_backup(Path(first["path"]), root=console.store.path.parent).profile.state["wallet"] == 10
    assert console.store.profile().state["wallet"] == 6
    batch = plan_commands(console, ("wallet.credit", {"amount": 1}), ("wallet.credit", {"amount": 2}))
    assert batch["auto_backup"] is True
    second = console.post("/commits", commit_body(batch)).json()["backup_before_commit"]
    assert second["reason"] == "before wallet.credit and 1 more"
    assert console.get("/backups").json()["items"] == [second]
    assert not Path(first["path"]).exists() and Path(second["path"]).exists()
    assert console.store.profile().state["wallet"] == 9


def test_destructive_plans_that_change_nothing_keep_the_automatic_backup(tmp_path):
    console = build_console(tmp_path, state=clocked(wallet=10))
    first = plan_commands(console, ("wallet.debit", {"amount": 4}))
    earlier = console.post("/commits", commit_body(first, confirmations=["DEBIT 4"])).json()["backup_before_commit"]
    plan = plan_commands(console, ("wallet.debit", {"amount": 0}))
    assert plan["auto_backup"] is False and plan["state_sha256_before"] == plan["state_sha256_after"]
    assert plan["confirmations"] == [{"step": 0, "operation": "wallet.debit", "phrase": "DEBIT 0"}]
    result = console.post("/commits", commit_body(plan, confirmations=["DEBIT 0"])).json()
    assert result["backup_before_commit"] is None and result["revision_before"] == result["revision_after"] == 1
    assert console.get("/backups").json()["items"] == [earlier] and Path(earlier["path"]).exists()


def test_parameter_decided_commands_confirm_and_back_up_only_for_destructive_parameters(tmp_path):
    console = build_console(tmp_path, state=clocked(wallet=10), admin_catalog=catalog(SCALED_DEBIT))
    listed = {item["operation"]: item for item in console.get("/commands").json()["commands"]}
    assert {name: (item["destructive"], item["destructive_by_params"], item["group"]) for name, item in listed.items()} == {
        "wallet.credit": (False, False, None), "wallet.debit": (True, False, None), "wallet.gated": (False, False, None),
        "wallet.scaled_debit": (True, True, None)}
    small = plan_commands(console, ("wallet.scaled_debit", {"amount": LARGE_DEBIT - 1}))
    assert small["confirmations"] == [] and small["auto_backup"] is False
    assert console.post("/commits", commit_body(small)).json()["backup_before_commit"] is None
    large = plan_commands(console, ("wallet.scaled_debit", {"amount": LARGE_DEBIT}))
    phrase = f"DEBIT {LARGE_DEBIT}"
    assert large["confirmations"] == [{"step": 0, "operation": "wallet.scaled_debit", "phrase": phrase}]
    assert large["auto_backup"] is True
    refused = console.post("/commits", commit_body(large))
    assert refused.status_code == 422 and error_of(refused)["code"] == "admin_confirmation_required"
    result = console.post("/commits", commit_body(large, confirmations=[phrase])).json()
    assert result["backup_before_commit"]["kind"] == AUTO_BACKUP_KIND
    assert console.store.profile().state["wallet"] == 10 - (LARGE_DEBIT - 1) - LARGE_DEBIT


def test_oversized_audit_records_are_refused_at_preview(tmp_path):
    console = build_console(tmp_path, admin_catalog=catalog(PADDED_CREDIT))
    plan = plan_commands(console, ("wallet.padded_credit", {"amount": MAX_AUDIT_FIELD_BYTES}))
    assert plan["committable"] is False and plan["plan_id"] is None
    assert plan["audit"]["ok"] is False and plan["audit"]["error"]["code"] == AUDIT_TOO_LARGE
    assert plan["audit"]["error"]["details"]["fields"]["result"] > MAX_AUDIT_FIELD_BYTES
    assert console.get("/audit").json()["items"] == [] and console.store.profile().revision == 0


def test_audit_records_that_outgrow_the_limit_after_preview_are_refused_and_recorded(console, monkeypatch):
    plan = plan_commands(console, ("wallet.credit", {"amount": 5}))
    assert plan["committable"] is True
    monkeypatch.setattr(admin_commit, "MAX_AUDIT_FIELD_BYTES", 1)
    op_id = str(uuid.uuid4())
    response = console.post("/commits", commit_body(plan, op_id))
    assert response.status_code == 422 and error_of(response)["code"] == AUDIT_TOO_LARGE
    [failure] = failures(console)
    assert (failure["kind"], failure["error_code"], failure["status"], failure["op_id"]) == (
        "command", AUDIT_TOO_LARGE, 422, op_id)
    assert console.get("/audit").json()["items"] == [] and console.store.profile().revision == 0


def test_failed_automatic_backup_writes_nothing_and_is_recorded(tmp_path):
    console = build_console(tmp_path, state=clocked(wallet=10))
    (console.store.path.parent / BACKUP_DIRECTORY).write_bytes(b"not a directory")
    plan = plan_commands(console, ("wallet.debit", {"amount": 4}))
    op_id = str(uuid.uuid4())
    response = console.post("/commits", commit_body(plan, op_id, confirmations=["DEBIT 4"]))
    assert response.status_code == 503 and error_of(response)["code"] == BACKUP_FAILED
    assert error_of(response)["details"]["error"]["code"] == "admin_backup_path_invalid"
    profile = console.store.profile()
    assert profile.state["wallet"] == 10 and profile.revision == 0 and console.get("/audit").json()["items"] == []
    [failure] = failures(console)
    assert (failure["error_code"], failure["status"], failure["op_id"]) == (BACKUP_FAILED, 503, op_id)


def test_commit_input_errors_are_recorded_as_failures(tmp_path):
    console = build_console(tmp_path, state=clocked(wallet=10))
    plan = plan_commands(console, ("wallet.debit", {"amount": 1}))
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    missing = console.post("/commits", commit_body(plan, first))
    assert missing.status_code == 422 and error_of(missing)["code"] == "admin_confirmation_required"
    assert error_of(missing)["details"]["required"] == [{"step": 0, "operation": "wallet.debit", "phrase": "DEBIT 1"}]
    extra = console.post("/commits", commit_body(plan, second, unexpected=True))
    assert extra.status_code == 422 and error_of(extra)["code"] == "admin_params_invalid"
    assert error_of(extra)["fields"][0]["loc"] == ["unexpected"]
    invalid = console.post("/commits", commit_body(plan, "not-a-uuid"))
    assert invalid.status_code == 422 and error_of(invalid)["code"] == "admin_op_id_invalid"
    recorded = [(item["error_code"], item["op_id"]) for item in reversed(failures(console))]
    assert recorded == [("admin_confirmation_required", first), ("admin_params_invalid", second),
                        ("admin_op_id_invalid", None)]
    assert console.store.profile().revision == 0


def test_plan_errors_are_answered_without_failure_records(tmp_path):
    console = build_console(tmp_path, state=clocked(wallet=1))
    unknown = console.post("/plans", {"commands": [{"operation": "wallet.unknown", "params": {}}]})
    assert unknown.status_code == 422 and error_of(unknown)["fields"][0]["loc"] == ["operation"]
    extra = console.post("/plans", {"commands": [], "unexpected": 1})
    assert extra.status_code == 422 and error_of(extra)["code"] == "admin_params_invalid"
    empty = console.post("/plans", {"commands": []})
    assert empty.status_code == 422 and error_of(empty)["code"] == "admin_batch_invalid"
    rejected = console.post("/plans", {"commands": [{"operation": "wallet.debit", "params": {"amount": 5}}]})
    assert rejected.status_code == 409 and error_of(rejected)["code"] == "wallet_insufficient"
    assert error_of(rejected)["details"] == {"step": 0, "operation": "wallet.debit"}
    gated = console.post("/plans", {"commands": [{"operation": "wallet.gated", "params": {"amount": 1}}]})
    assert gated.status_code == 503 and error_of(gated)["code"] == "admin_command_unavailable"
    assert failures(console) == [] and console.store.profile().revision == 0


def test_audit_and_request_pages_are_bounded(console):
    for amount in (1, 2):
        console.post("/commits", commit_body(plan_commands(console, ("wallet.credit", {"amount": amount}))))
    page = console.get("/audit", params={"limit": 1}).json()
    assert len(page["items"]) == 1 and page["next_cursor"] is not None
    rest = console.get("/audit", params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    for path in ("/audit", "/audit/failures", "/backups"):
        for params in ({"limit": 0}, {"limit": 201}, {"cursor": 0}, {"cursor": MAX_REVISION + 1}):
            response = console.get(path, params=params)
            assert response.status_code == 422 and error_of(response)["code"] == "admin_params_invalid"
        assert console.get(path, params={"cursor": MAX_REVISION}).status_code == 200
    console.store.record_request(None, "115", 10, 20, 200)
    [entry] = console.get("/requests", params={"limit": 5}).json()["requests"]
    assert (entry["route"], entry["wire_bytes"], entry["response_bytes"], entry["status"]) == ("115", 10, 20, 200)
    assert console.get("/requests", params={"limit": 0}).status_code == 422


def test_views_answer_with_snapshot_metadata_and_etags(tmp_path):
    console = build_console(tmp_path, state=clocked(wallet=2))
    generation_id = console.runtime.current.id
    response = console.get("/views/wallet.summary", params={"params": json.dumps({"scale": 3})})
    assert response.status_code == 200
    etag = f'"0:{generation_id}"'
    assert response.headers["etag"] == etag
    assert response.json() == {"view": "wallet.summary", "revision": 0, "generation_id": generation_id,
                               "server_now": {"utc": NOW.isoformat(), "jst": NOW.astimezone(JST).isoformat()},
                               "data": {"wallet": 6, "revision": 0, "generation": generation_id, "now": NOW.isoformat(),
                                        "leases": 1}}
    assert console.get("/views/wallet.summary").json()["data"]["wallet"] == 2
    cached = console.get("/views/wallet.summary", headers={"if-none-match": etag})
    assert cached.status_code == 304 and cached.headers["etag"] == etag and cached.content == b""
    assert console.get("/views/wallet.summary", headers={"if-none-match": f"W/{etag}"}).status_code == 304
    advance_state(console, wallet=4)
    fresh = console.get("/views/wallet.summary", headers={"if-none-match": etag})
    assert fresh.status_code == 200 and fresh.headers["etag"] == f'"1:{generation_id}"'
    assert console.get("/views/wallet.plain").json()["data"] == {"params": None}


def test_live_views_send_no_etag_and_never_answer_not_modified(tmp_path):
    console = build_console(tmp_path, admin_catalog=catalog(extra_views=(CLOCK,)))
    etag = f'"0:{console.runtime.current.id}"'
    first = console.get("/views/wallet.clock")
    assert first.status_code == 200 and "etag" not in first.headers and first.json()["data"] == {"now": NOW.isoformat()}
    later = NOW + timedelta(minutes=5)
    console.wall.now = later
    for header in (etag, f"W/{etag}", "*"):
        response = console.get("/views/wallet.clock", headers={"if-none-match": header})
        assert response.status_code == 200 and "etag" not in response.headers
        assert response.json()["data"] == {"now": later.isoformat()} and response.json()["revision"] == 0
    assert console.get("/views/wallet.summary", headers={"if-none-match": etag}).status_code == 304


@pytest.mark.parametrize("name, params, status, code", [
    ("wallet.summary", '{"scale":1,"scale":2}', 400, "admin_json_duplicate_key"),
    ("wallet.summary", '{"scale":1,"extra":1}', 422, "admin_params_invalid"),
    ("wallet.summary", '{"scale":"2"}', 422, "admin_params_invalid"),
    ("wallet.summary", "x" * (8 * 1024 + 1), 422, "admin_params_invalid"),
    ("wallet.plain", "{}", 422, "admin_params_invalid"),
    ("wallet.unknown", None, 404, "admin_view_unknown"),
])
def test_view_parameters_are_strict(console, name, params, status, code):
    response = console.get(f"/views/{name}", params=None if params is None else {"params": params})
    assert response.status_code == status and error_of(response)["code"] == code


def test_manual_backups_are_created_and_listed(console):
    response = console.post("/backups", {"reason": "before experiment"})
    assert response.status_code == 200, response.text
    backup = response.json()["backup"]
    assert (backup["kind"], backup["reason"], backup["op_id"], backup["revision"]) == ("manual", "before experiment", None, 0)
    assert Path(backup["path"]).parent == console.store.path.parent / BACKUP_DIRECTORY
    listing = console.get("/backups").json()
    assert listing["items"] == [backup] and listing["next_cursor"] is None
    assert listing["retention"] == {"auto_keep": 1, "auto_lifetime_seconds": 86400, "manual_pruned": False}
    for body in ({"reason": "x", "extra": 1}, {"reason": ""}, {"reason": "line\nbreak"}, {"reason": "x" * 201}, {}):
        rejected = console.post("/backups", body)
        assert rejected.status_code == 422 and error_of(rejected)["code"] == "admin_params_invalid"
    assert {item["kind"] for item in failures(console)} == {"backup"} and len(failures(console)) == 5


def test_listing_prunes_expired_automatic_backups_but_keeps_manual_ones(tmp_path):
    console = build_console(tmp_path, state=clocked(wallet=10))
    manual = console.post("/backups", {"reason": "keep me"}).json()["backup"]
    plan = plan_commands(console, ("wallet.debit", {"amount": 1}))
    automatic = console.post("/commits", commit_body(plan, confirmations=["DEBIT 1"])).json()["backup_before_commit"]
    assert [item["kind"] for item in console.get("/backups").json()["items"]] == [AUTO_BACKUP_KIND, "manual"]
    console.wall.now += timedelta(days=1)
    assert console.get("/backups").json()["items"] == [manual]
    assert not Path(automatic["path"]).exists() and Path(manual["path"]).exists()


def test_restore_preview_reports_the_plan_and_its_checks(console):
    backup = snapshot(console)
    advance_state(console, wallet=50)
    plan = preview_restore(console, backup)
    assert plan["committable"] is True and plan["expected_revision"] == 1 and plan["cross_account"] is False
    assert plan["confirmations"] == [{"step": 0, "operation": "admin.restore", "phrase": "RESTORE 1"}]
    assert plan["diff"]["entries"] == [{"op": "replace", "path": "/wallet", "before": 50, "after": 0}]
    assert plan["checks"] == {"energy_clock": {"ok": True}, "battle": {"ok": True}, "restart_check": {"ok": True},
                              "preflight": {"ok": True, "readback": {"wallet": [137]}}, "audit": {"ok": True}}
    assert plan["auto_backup"] is True
    assert plan["backup"]["path"] == backup and plan["backup"]["revision"] == 0 and plan["backup"]["player_id"] == "1"
    assert plan["generation_id"] == console.runtime.current.id and plan["client"]["quiet"] is True
    relative = os.path.relpath(backup, console.store.path.parent)
    assert preview_restore(console, relative)["restore_digest"] == plan["restore_digest"]
    assert console.store.profile().revision == 1 and console.store.list_backups().items[0]["kind"] == "manual"


def test_restore_requires_a_closed_and_quiet_client(console):
    backup = snapshot(console)
    advance_state(console, wallet=50)
    plan = preview_restore(console, backup)
    unconfirmed = console.post("/restores", restore_body(plan, client_closed=False))
    assert unconfirmed.status_code == 422 and error_of(unconfirmed)["code"] == "admin_client_closed_required"
    console.runtime.observe_request("115")
    console.clock.advance(CLIENT_QUIET_SECONDS - 1)
    busy = console.post("/restores", restore_body(plan))
    assert busy.status_code == 409 and error_of(busy)["code"] == "admin_client_active"
    assert error_of(busy)["details"] == {"idle_seconds": CLIENT_QUIET_SECONDS - 1, "quiet_seconds": CLIENT_QUIET_SECONDS}
    assert console.store.profile().state["wallet"] == 50 and console.store.list_backups().items[0]["kind"] == "manual"
    console.clock.advance(1)
    assert console.post("/restores", restore_body(plan)).status_code == 200
    assert [item["kind"] for item in failures(console)] == ["restore", "restore"]


def test_restore_requires_the_exact_confirmation_phrase(console):
    backup = snapshot(console)
    advance_state(console, wallet=50)
    plan = preview_restore(console, backup)
    for phrases in ([], ["RESTORE 0"], ["RESTORE 1", "extra"]):
        response = console.post("/restores", restore_body(plan, confirmations=phrases))
        assert response.status_code == 422 and error_of(response)["code"] == "admin_confirmation_required"
        assert error_of(response)["details"]["required"] == [{"step": 0, "operation": "admin.restore", "phrase": "RESTORE 1"}]
    assert console.store.profile().revision == 1
    assert [item["kind"] for item in console.store.list_backups().items] == ["manual"]


def test_restore_backs_up_first_and_commits_inside_the_fence(console):
    backup = snapshot(console)
    advance_state(console, wallet=50)
    plan = preview_restore(console, backup)
    calls = []

    def observed(name, original):
        def call(*arguments, **options):
            calls.append((name, console.runtime.status()["fenced"]))
            return original(*arguments, **options)

        return call

    console.store.backup = observed("backup", console.store.backup)
    console.store.commit_admin = observed("commit", console.store.commit_admin)
    op_id = str(uuid.uuid4())
    response = console.post("/restores", restore_body(plan, op_id=op_id))
    assert response.status_code == 200, response.text
    result = response.json()
    assert calls == [("backup", True), ("commit", True)] and result["replayed"] is False and result["kind"] == "restore"
    assert (result["revision_before"], result["revision_after"]) == (1, 2) and result["cross_account"] is False
    assert result["backup"] == plan["backup"] and result["client_restart_required"] is True
    assert result["readback"] == {"wallet": [137]}
    profile = console.store.profile()
    assert profile.revision == 2 and profile.state == read_backup(Path(backup), root=console.store.path.parent).profile.state
    taken = result["backup_before_restore"]
    assert (taken["kind"], taken["op_id"], taken["reason"], taken["revision"]) == (AUTO_BACKUP_KIND, op_id, "before restore", 1)
    assert read_backup(Path(taken["path"]), root=console.store.path.parent).profile.state["wallet"] == 50
    [record] = console.get("/audit").json()["items"]
    assert (record["op_id"], record["kind"], record["targets"], record["plan_digest"]) == (
        op_id, "restore", ["/"], plan["restore_digest"])
    assert record["operations"] == [{"operation": "admin.restore", "params": {
        "backup": backup, "backup_sha256": plan["backup"]["sha256"], "backup_revision": 0, "cross_account": False}}]
    assert record["state_sha256_after"] == json_digest(profile.state)
    assert console.get("/session").json()["client"]["restart_required"] is True
    again = console.post("/restores", restore_body(plan, op_id=op_id)).json()
    assert again == {**result, "replayed": True} and console.store.profile().revision == 2
    assert [item["id"] for item in console.store.list_backups().items if item["kind"] == AUTO_BACKUP_KIND] == [taken["id"]]
    console.clock.advance(1)
    console.runtime.observe_request("31")
    assert console.get("/session").json()["client"]["restart_required"] is False
    assert console.runtime.status()["fenced"] is False and console.runtime.status()["current"]["leases"] == 0


def test_failed_restore_backup_writes_nothing_and_is_recorded(console):
    backup = snapshot(console)
    advance_state(console, wallet=50)
    plan = preview_restore(console, backup)

    def failing(*arguments, **options):
        raise ProtocolError("admin_backup_failed", "The online backup could not be verified and registered", 503)

    console.store.backup = failing
    op_id = str(uuid.uuid4())
    response = console.post("/restores", restore_body(plan, op_id=op_id))
    assert response.status_code == 503 and error_of(response)["code"] == BACKUP_FAILED
    profile = console.store.profile()
    assert profile.state["wallet"] == 50 and profile.revision == 1 and console.get("/audit").json()["items"] == []
    [failure] = failures(console)
    assert (failure["kind"], failure["error_code"], failure["status"], failure["op_id"]) == (
        "restore", BACKUP_FAILED, 503, op_id)
    assert console.runtime.status()["fenced"] is False
    assert console.get("/session").json()["client"]["restart_required"] is False


def test_restoring_an_identical_state_is_audited_without_a_new_revision_or_backup(console):
    plan = preview_restore(console, snapshot(console))
    assert plan["diff"]["total"] == 0 and plan["state_sha256_before"] == plan["state_sha256_after"]
    assert plan["committable"] is True and plan["auto_backup"] is False
    listed = console.get("/backups").json()
    result = console.post("/restores", restore_body(plan)).json()
    assert (result["revision_before"], result["revision_after"]) == (0, 0) and result["client_restart_required"] is False
    assert result["backup_before_restore"] is None and console.store.profile().revision == 0
    assert console.get("/backups").json() == listed
    assert console.get("/session").json()["client"]["restart_required"] is False
    assert [item["kind"] for item in console.get("/audit").json()["items"]] == ["restore"]


def test_restore_previews_report_audit_records_the_store_would_reject(console, monkeypatch):
    backup = snapshot(console)
    advance_state(console, wallet=50)
    monkeypatch.setattr(admin_commit, "MAX_AUDIT_FIELD_BYTES", 1)
    plan = preview_restore(console, backup)
    assert plan["committable"] is False and plan["checks"]["audit"]["ok"] is False
    assert plan["checks"]["audit"]["error"]["code"] == AUDIT_TOO_LARGE
    assert all(check["ok"] for name, check in plan["checks"].items() if name != "audit")


def test_restore_previews_skip_the_audit_measure_when_the_preflight_fails(console):
    backup = snapshot(console)
    advance_state(console, wallet=50)

    def failing(*arguments):
        raise ProtocolError("admin_preflight_failed", "Response 137 could not be encoded", 409)

    console.preflight.check = failing
    plan = preview_restore(console, backup)
    assert plan["committable"] is False and plan["checks"]["preflight"]["error"]["code"] == "admin_preflight_failed"
    assert plan["checks"]["audit"] == {"ok": False, "skipped": "preflight"}


def test_restore_audit_records_are_measured_before_the_automatic_backup(console, monkeypatch):
    backup = snapshot(console)
    advance_state(console, wallet=50)
    plan = preview_restore(console, backup)
    assert plan["committable"] is True and plan["auto_backup"] is True
    listed = console.get("/backups").json()
    monkeypatch.setattr(admin_commit, "MAX_AUDIT_FIELD_BYTES", 1)
    op_id = str(uuid.uuid4())
    response = console.post("/restores", restore_body(plan, op_id=op_id))
    assert response.status_code == 422 and error_of(response)["code"] == AUDIT_TOO_LARGE
    assert console.get("/backups").json() == listed and console.store.profile().revision == 1
    assert console.store.profile().state["wallet"] == 50 and console.get("/audit").json()["items"] == []
    [failure] = failures(console)
    assert (failure["kind"], failure["error_code"], failure["status"], failure["op_id"]) == (
        "restore", AUDIT_TOO_LARGE, 422, op_id)
    assert console.runtime.status()["fenced"] is False


def test_restore_operation_ids_cannot_be_reused_for_other_plans(console):
    plan = plan_commands(console, ("wallet.credit", {"amount": 1}))
    op_id = str(uuid.uuid4())
    console.post("/commits", commit_body(plan, op_id))
    backup = snapshot(console)
    restore = console.post("/restores", restore_body(preview_restore(console, backup), op_id=op_id))
    assert restore.status_code == 409 and error_of(restore)["code"] == "admin_operation_conflict"


def test_cross_account_restore_needs_the_extra_phrase(console):
    foreign = file_backup(console, "foreign.sqlite3", clocked(wallet=7), player_id="2")
    plan = preview_restore(console, foreign)
    assert plan["cross_account"] is True and plan["backup"]["player_id"] == "2"
    assert [item["phrase"] for item in plan["confirmations"]] == ["RESTORE 0", CROSS_ACCOUNT_PHRASE]
    partial = console.post("/restores", restore_body(plan, confirmations=["RESTORE 0"]))
    assert partial.status_code == 422 and error_of(partial)["code"] == "admin_confirmation_required"
    response = console.post("/restores", restore_body(plan))
    assert response.status_code == 200 and response.json()["cross_account"] is True
    profile = console.store.profile()
    assert profile.state["wallet"] == 7 and profile.player_id == "1" and profile.revision == 1


def test_backups_with_a_battle_in_progress_must_be_aborted_first(console):
    backup = file_backup(console, "battle.sqlite3", clocked(wallet=3, quest_state=battle_journal()))
    plan = preview_restore(console, backup)
    assert plan["committable"] is False and plan["checks"]["battle"]["ok"] is False
    error = plan["checks"]["battle"]["error"]
    assert error["code"] == "admin_battle_abort_required"
    assert error["details"]["battle"] == {"quest_id": 100, "sequence": 1, "start_key": "a" * 64,
                                          "master_snapshot": "snapshot-1", "resource_version": "resource-1"}
    response = console.post("/restores", restore_body(plan))
    assert response.status_code == 409 and error_of(response)["code"] == "admin_battle_abort_required"
    assert console.store.profile().revision == 0 and console.store.list_backups().items == ()


def test_backups_without_an_energy_clock_marker_are_refused(console):
    legacy = {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": 1}]}, "wallet": 9}
    plan = preview_restore(console, file_backup(console, "legacy.sqlite3", legacy))
    assert plan["committable"] is False and plan["checks"]["energy_clock"]["error"]["code"] == "admin_restore_clock_required"
    response = console.post("/restores", restore_body(plan))
    assert response.status_code == 409 and error_of(response)["code"] == "admin_restore_clock_required"
    assert console.store.profile().state["wallet"] == 0 and console.store.list_backups().items == ()


def test_restore_paths_must_stay_inside_the_data_directory(console, tmp_path):
    outside = tmp_path / "outside" / "copy.sqlite3"
    Store(outside, clocked(wallet=1))
    export = console.post("/exports", {}).json()["export"]["path"]
    for backup, code in ((str(outside), "admin_backup_path_invalid"), (os.path.join("..", "outside", "copy.sqlite3"),
                                                                       "admin_backup_path_invalid"),
                         (DATABASE, "admin_backup_path_invalid"), (str(console.store.path), "admin_backup_path_invalid"),
                         (os.path.join(BACKUP_DIRECTORY, "missing.sqlite3"), "admin_backup_path_invalid"),
                         (export, "admin_backup_invalid")):
        response = console.post("/restores/preview", {"backup": backup})
        assert response.status_code == 400 and error_of(response)["code"] == code, backup
    assert console.post("/restores/preview", {"backup": ""}).status_code == 422


def test_restore_plans_go_stale_when_the_state_or_digest_changes(console):
    backup = snapshot(console)
    advance_state(console, wallet=50)
    plan = preview_restore(console, backup)
    forged = console.post("/restores", {**restore_body(plan), "restore_digest": "0" * 64})
    assert forged.status_code == 409 and error_of(forged)["code"] == "plan_changed"
    malformed = console.post("/restores", {**restore_body(plan), "restore_digest": "XYZ"})
    assert malformed.status_code == 422 and error_of(malformed)["code"] == "admin_plan_digest_invalid"
    advance_state(console, wallet=60)
    stale = console.post("/restores", restore_body(plan))
    assert stale.status_code == 409 and error_of(stale)["code"] == "admin_revision_conflict"
    assert error_of(stale)["current_revision"] == 2 and console.store.profile().state["wallet"] == 60
    assert [item["kind"] for item in console.store.list_backups().items] == ["manual"]


def test_restore_refuses_a_generation_switch_after_the_preview(console):
    backup = snapshot(console)
    advance_state(console, wallet=50)
    plan = preview_restore(console, backup)
    console.runtime.swap(generation(console.wall, config_set="second"), console.runtime.current.id)
    switched = console.post("/restores", restore_body(plan))
    assert switched.status_code == 409 and error_of(switched)["code"] == "plan_changed"
    assert console.store.profile().state["wallet"] == 50 and console.get("/audit").json()["items"] == []


@pytest.mark.parametrize("race, code", [("fence_busy", "generation_switch_busy"), ("generation_switch", "plan_stale"),
                                        ("game_write", "admin_revision_conflict")])
def test_restores_refused_at_the_fence_keep_their_automatic_source(tmp_path, race, code):
    console = build_console(tmp_path, state=clocked(wallet=10))
    debit = plan_commands(console, ("wallet.debit", {"amount": 4}))
    source = console.post("/commits", commit_body(debit, confirmations=["DEBIT 4"])).json()["backup_before_commit"]
    plan = preview_restore(console, source["path"])
    checked = console.preflight.check

    def racing(*arguments):
        if race == "generation_switch":
            console.runtime.swap(generation(console.wall, config_set="raced"), console.runtime.current.id)
        elif race == "game_write":
            advance_state(console, wallet=99)
        return checked(*arguments)

    def busy():
        raise ProtocolError("generation_switch_busy", "Another generation switch is in progress", 409)

    console.preflight.check = racing
    if race == "fence_busy":
        console.runtime.fence = busy
    response = console.post("/restores", restore_body(plan))
    assert response.status_code == 409 and error_of(response)["code"] == code
    assert Path(source["path"]).exists() and [item["id"] for item in console.store.list_backups().items] == [source["id"]]
    assert [item["kind"] for item in console.get("/audit").json()["items"]] == ["command"]
    assert console.runtime.status()["fenced"] is False and console.runtime.status()["current"]["leases"] == 0


def test_restart_incompatible_backups_are_not_restored(tmp_path):
    console = build_console(tmp_path, restart_check=InitializerRestartCheck(lambda state: {**state, "migrated": True}))
    backup = snapshot(console)
    advance_state(console, wallet=50)
    plan = preview_restore(console, backup)
    assert plan["committable"] is False
    assert plan["checks"]["restart_check"]["error"]["code"] == "admin_restart_incompatible"
    response = console.post("/restores", restore_body(plan))
    assert response.status_code == 409 and error_of(response)["code"] == "admin_restart_incompatible"
    assert console.store.profile().revision == 1 and [item["kind"] for item in console.store.list_backups().items] == ["manual"]


def test_exports_are_redacted_and_cannot_be_imported(tmp_path):
    state = clocked(ledger={"user_key": SECRET, "receipts": [{"response": f'<?xml version="1.0"?><DA>{SECRET}</DA>'}]},
                    settings={"bootstrap_token": SECRET})
    console = build_console(tmp_path, state=state)
    response = console.post("/exports", {})
    assert response.status_code == 200, response.text
    export = response.json()["export"]
    path = Path(export["path"])
    assert path.parent == console.store.path.parent / EXPORT_DIRECTORY and path.suffix == ".json"
    raw = path.read_bytes()
    assert len(raw) == export["bytes"] and b"\r" not in raw and SECRET.encode() not in raw
    document = json.loads(raw)
    assert document["format"] == "aigis-ps-admin-export" and document["importable"] is False
    assert document["state_sha256"] == json_digest(console.store.profile().state) and document["revision"] == 0
    assert document["state"]["ledger"]["user_key"] == "[redacted]" and document["state"]["wallet"] == 0
    assert document["state"]["ledger"]["receipts"][0]["response"]["redacted"] == "response"
    assert sorted(item.name for item in path.parent.iterdir()) == [path.name]
    restore = console.post("/restores/preview", {"backup": str(path)})
    assert restore.status_code == 400 and error_of(restore)["code"] == "admin_backup_invalid"
    assert console.post("/exports", {"unexpected": 1}).status_code == 422


@pytest.mark.parametrize("prolog", XML_PROLOGS)
def test_exports_mask_receipts_after_any_prolog_the_xml_reader_accepts(tmp_path, prolog):
    xml = prolog + SYSTEM_RECEIPT
    encoded = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    console = build_console(tmp_path, state=clocked(ledger={"receipts": [{"response": xml}, {"response": encoded}],
                                                            "notes": [xml]}))
    raw = Path(console.post("/exports", {}).json()["export"]["path"]).read_bytes()
    assert SECRET.encode() not in raw
    ledger = json.loads(raw)["state"]["ledger"]
    assert all(REDACTED in value for value in (*(item["response"] for item in ledger["receipts"]), *ledger["notes"]))


def test_console_pages_are_served_without_a_token_and_listed(tmp_path):
    ui = tmp_path / "ui"
    (ui / "pages").mkdir(parents=True)
    (ui / "index.html").write_bytes(b"<!doctype html><title>console</title>\n")
    (ui / "app.js").write_bytes(b"export {};\n")
    for name in ("status.js", "audit.js", "Bad.js", "notes.txt"):
        (ui / "pages" / name).write_bytes(b"export {};\n")
    console = build_console(tmp_path, ui_directory=ui)
    page = console.client.get("/admin/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY and page.headers["cache-control"] == "no-store"
    redirect = console.client.get("/admin", follow_redirects=False)
    assert redirect.status_code == 307 and redirect.headers["location"].endswith("/admin/")
    script = console.client.get("/admin/static/app.js")
    assert script.status_code == 200 and script.headers["cache-control"] == "no-store"
    assert console.get("/ui-pages").json() == {"pages": [
        {"name": "audit", "module": "/admin/static/pages/audit.js"},
        {"name": "status", "module": "/admin/static/pages/status.js"}]}
    assert console.client.get(API_ROOT + "/ui-pages").status_code == 401
    assert console.client.get("/admin/static/missing.js").status_code == 404
    for name in ("%7C", "a%3Fb", "%01", "x" * 300):
        refused = console.client.get(f"/admin/static/{name}")
        assert refused.status_code == 404 and error_of(refused)["code"] == "admin_not_found"
        assert refused.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_missing_console_pages_answer_not_found(console):
    for path in ("/admin/", "/admin/static/app.js"):
        response = console.client.get(path)
        assert response.status_code == 404 and error_of(response)["code"] == "admin_ui_missing"
    assert console.get("/ui-pages").json() == {"pages": []}


@pytest.fixture
def route_package(tmp_path, monkeypatch):
    name = f"admin_route_fixture_{uuid.uuid4().hex}"
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_bytes(b"")
    for module in ("admin_routes_state", "admin_routes_backup"):
        (root / f"{module}.py").write_bytes(f"from aigis_ps.{module} import register\n".encode("utf-8"))
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    yield importlib.import_module(name)
    for module_name in [module_name for module_name in sys.modules if module_name.split(".")[0] == name]:
        del sys.modules[module_name]


def test_route_modules_register_through_discovery(tmp_path, route_package):
    discovered = discover({}, package=route_package)
    assert [route.name for route in discovered.routes] == ["backup", "state"]
    console = build_console(tmp_path, admin_catalog=discovered)
    assert console.get("/session").status_code == 200 and console.get("/backups").status_code == 200
    assert console.get("/commands").json() == {"commands": [], "field_owners": {}}


def router_with(path):
    router = APIRouter()

    @router.get(path)
    def handler():
        return {}

    return router


def test_create_admin_app_rejects_invalid_configuration(tmp_path):
    data = tmp_path / "data"
    store = Store(data / DATABASE, clocked())
    runtime = GenerationRuntime(generation(Wall()))
    valid = {"data_dir": data, "admin_host": "127.0.0.1", "admin_port": PORT, "game_port": GAME_PORT, "token": TOKEN,
             "preflight": ReadbackPreflight(), "restart_check": InitializerRestartCheck(), "plan_store": PlanStore(),
             "preflight_domains": DOMAINS, "catalog": catalog(), "ui_directory": tmp_path / "ui"}
    create_admin_app(runtime, store, **valid)
    for changes in ({"admin_host": "0.0.0.0"}, {"admin_host": "localhost"}, {"admin_port": GAME_PORT},
                    {"data_dir": tmp_path / "elsewhere"}, {"token": "short"}, {"plan_store": object()},
                    {"preflight": object()}):
        with pytest.raises(ValueError):
            create_admin_app(runtime, store, **{**valid, **changes})
    with pytest.raises(ValueError):
        create_admin_app(object(), store, **valid)
    for name, build, message in (("clash", lambda services: router_with("/session"), "repeats"),
                                 ("console", lambda services: router_with("/ui-pages"), "repeats"),
                                 ("broken", lambda services: None, "APIRouter")):
        clashing = catalog()
        clashing.routes.add(AdminRoute(name, build))
        with pytest.raises(ValueError, match=message):
            create_admin_app(runtime, store, **{**valid, "catalog": clashing})


def test_leased_generation_derives_time_like_the_game_services():
    wall = Wall()
    leased = LeasedGeneration(generation(wall))
    now = leased.now()
    assert now == NOW and now.utcoffset() is not None
    assert leased.acquisition_day(now) == civil_day(wall())
    assert leased.id == leased.generation.id and leased.content_versions == {"wallet": "w1"}
    assert leased.bundle is leased.generation.bundle
    wall.now = datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc)
    assert leased.acquisition_day(leased.now()) == civil_day(wall()) == datetime(2026, 11, 3).toordinal() - 1
    wall.now = datetime(2026, 11, 2, 23, 30)
    assert leased.now() == datetime(2026, 11, 2, 23, 30, tzinfo=JST)


def gate_parts():
    clock = Monotonic()
    runtime = GenerationRuntime(generation(Wall()), clock=clock, admit_timeout=SHORT, drain_timeout=SHORT,
                                draining_limit=SHORT)
    return runtime, clock, ClientGate(runtime, clock=clock)


def test_client_gate_requires_acknowledgement_and_a_quiet_client():
    runtime, clock, gate = gate_parts()
    for value in (False, None, "yes", 1):
        with pytest.raises(AdminError) as caught:
            gate.require_closed(value)
        assert caught.value.code == "admin_client_closed_required" and caught.value.status == 422
    gate.require_closed(True)
    runtime.observe_request("115")
    clock.advance(CLIENT_QUIET_SECONDS - 1)
    with pytest.raises(AdminError) as caught:
        gate.require_closed(True)
    assert caught.value.code == "admin_client_active" and caught.value.status == 409
    assert gate.quiet() is False and gate.status()["quiet"] is False
    clock.advance(1)
    gate.require_closed(True)
    assert gate.quiet() is True and gate.status()["idle_seconds"] == CLIENT_QUIET_SECONDS


def test_restart_notices_last_until_the_next_game_request():
    runtime, clock, gate = gate_parts()
    status = gate.status()
    assert status["restart_required"] is False and status["session_observed_since_switch"] is None
    runtime.observe_request("115")
    clock.advance(5)
    gate.notice_restart("restore")
    assert [notice["reason"] for notice in gate.status()["notices"]] == ["restore"]
    clock.advance(5)
    assert gate.status()["restart_required"] is True
    runtime.observe_request("31")
    assert gate.status()["restart_required"] is False and gate.status()["notices"] == []
    for _ in range(MAX_RESTART_NOTICES + 4):
        clock.advance(1)
        gate.notice_restart("content_switch")
    assert len(gate.status()["notices"]) == MAX_RESTART_NOTICES
    for invalid in ("", "x" * 65, "line\nbreak", None):
        with pytest.raises(ValueError):
            gate.notice_restart(invalid)


def test_client_gate_rejects_invalid_configuration():
    runtime, clock, _ = gate_parts()
    for arguments, options in (((object(),), {}), ((runtime,), {"quiet_seconds": 0}),
                               ((runtime,), {"quiet_seconds": True}), ((runtime,), {"clock": 1})):
        with pytest.raises(ValueError):
            ClientGate(*arguments, **options)


def test_battle_checks_read_the_raw_active_entry():
    assert active_battle({}) is None and active_battle({"quest_state": {"active": None}}) is None
    assert active_battle({"quest_state": "corrupt"}) is None
    require_no_active_battle({"quest_state": {"active": None}}, source="backup")
    journal = battle_journal()
    assert active_battle({"quest_state": journal}) == journal["active"]
    with pytest.raises(AdminError) as caught:
        require_no_active_battle({"quest_state": journal}, source="backup")
    assert caught.value.code == "admin_battle_abort_required" and caught.value.status == 409
    assert caught.value.details == {"source": "backup", "battle": {
        "quest_id": 100, "sequence": 1, "start_key": "a" * 64, "master_snapshot": "snapshot-1",
        "resource_version": "resource-1"}}
    with pytest.raises(AdminError) as caught:
        require_no_active_battle({"quest_state": {"active": 1}}, source="player state")
    assert caught.value.details == {"source": "player state", "battle": {}}

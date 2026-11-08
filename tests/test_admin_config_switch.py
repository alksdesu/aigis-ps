from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from functools import partial
import json
from pathlib import Path
import threading
import time
from types import MappingProxyType, SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aigis_ps import admin_commit, admin_config_switch, admin_routes_config, admin_routes_state
from aigis_ps.admin_app import create_admin_app, server_time
from aigis_ps.admin_command import AdminError, PlanStore, PreflightReport
from aigis_ps.admin_commit import InitializerRestartCheck
from aigis_ps.admin_config_switch import CONTENT_PHRASE, EMPTY_SET, ConfigSwitch, config_switch, install, roll_forward
from aigis_ps.admin_diff import json_digest
from aigis_ps.admin_gates import CLIENT_QUIET_SECONDS
from aigis_ps.admin_registry import AdminCatalog, CommandRegistry, RouteRegistry, ViewRegistry
from aigis_ps.admin_security import API_ROOT
from aigis_ps.business_calendar import month_key
from aigis_ps.config_registry import REGISTRY, ConfigError, TimeSemantics
from aigis_ps.config_sets import ACTIVE_POINTER, LOCK_NAME, MAX_POINTER_BYTES, ConfigSets, resolve
from aigis_ps.energy_clock import JST, anchor_seconds, mark_new
from aigis_ps.errors import ProtocolError
from aigis_ps.generation import CANDIDATE_TTL_SECONDS, Generation, GenerationIdentity, GenerationInputs, GenerationRuntime
from aigis_ps.local_io import exclusive_file
from aigis_ps.login_cooking_state import (CRYSTALS_PER_COOKING, MAX_COOKING, advance_cooking, initialize_cooking,
                                          read_cooking)
from aigis_ps.login_reward_state import initialize_login_rewards, read_login_rewards
from aigis_ps.startup_diagnostics import StartupDiagnostics
from aigis_ps.storage import AUTO_BACKUP_KIND, RevisionConflict, Store, read_backup
from aigis_ps.tower_state import DOMAIN as TOWER_STATE, TowerRules
from aigis_ps.tower_trade_state import REBIND_OPERATION
from test_login_cooking_state import BORDER, base as cooking_base


NOW = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
PORT, GAME_PORT = 18104, 18103
TOKEN = "s" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = f"http://127.0.0.1:{PORT}"
DATABASE = "aigis.sqlite3"
SHORT, WAIT = 0.2, 5.0
DOMAINS = {"main_quests": "quest_record"}
RANKS = ({"NextExp": 1000, "CharismaMax": 50}, {"NextExp": 2000, "CharismaMax": 60})
EXAMPLES = Path(__file__).resolve().parents[1] / "config"
GOLD = {"quest-rules": {"gold_percent": 50}}
NEVER_RESET = {"trade-rules": {"reset_type_1": "never"}}
CHARISMA = {"quest-rules": {"charisma_recovery_seconds": 60}}
GATE_ISSUE = "The native resource view is not ready"
COOKING_PERIOD, NEXT_MONTH = "2026-11", datetime(2026, 12, 1, 4, tzinfo=JST)
COOKING_SPENT, ISSUED_SPENT = 625, 280
RAISED_BORDER, TIGHT_BORDER = 400, 270
RAISED_QUOTA = (COOKING_SPENT - RAISED_BORDER) // CRYSTALS_PER_COOKING
DEFAULT_BEGINNER, RAISED_BEGINNER, LOWERED_BEGINNER = 7, 14, 3
STAMPED_DAY = date(2026, 11, 2)
TOWER_HOURS = ("generation_hour", "replenish_hour")
SHIFTED_HOUR = 5
TOWER_STAMP = "towers_settled"
# The stand-in tower service ignores the saved progress, so only its presence matters to the switch.
TOWER_PROGRESS = {TOWER_STATE: {"towers": {}}}
TOWER_PRODUCT_REUSED = "tower_trade_product_reused"
REUSED_TOWER_PRODUCTS = (12038, 12039)
TOWER_TRADE_OPERATIONS = (65, 69)
TOWER_TRADE_CLIENT = "shows a communication error and resends the same request until the server accepts it"
TOWER_HISTORY_INVALID = "tower_trade_history_invalid"


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


class Crash(BaseException):
    pass


def initialize(policy, state):
    if policy == "unconfigured":
        raise ProtocolError("trade_policy_unavailable", "Unconfigured resets reject the saved purchases", 503)
    return {**state, "trade_policy": policy}


def restless(policy, state):
    return {**initialize(policy, state), "initialized": state.get("initialized", 0) + 1}


def cooked(border, initializer, state):
    # Like the login reward service, a generation loads a cooking ledger frozen at another border; only 115 refuses it.
    return initializer(initialize_cooking(state, border=border))


def rewarded(total, initializer, state):
    # Like the login reward service, a generation loads a ledger frozen at another beginner total; only 115 refuses it.
    return initializer(initialize_login_rewards(state, NOW, beginner_count=total))


class Deferred:
    def __init__(self):
        self.pending = []

    def __call__(self, target):
        self.pending.append(target)

    def run(self):
        while self.pending:
            self.pending.pop(0)()


@dataclass(frozen=True)
class StampingTowers:
    rules: TowerRules

    def settle(self, state, now):
        hours = {name: getattr(self.rules, name) for name in TOWER_HOURS}
        return {**state, TOWER_STAMP: {**hours, "settled_at": now.isoformat()}}, ()


@dataclass(frozen=True)
class ReusingTowerTrades:
    reused: tuple[int, ...] = ()
    failure: str | None = None

    def reuse_report(self, state):
        if self.failure is not None:
            raise ProtocolError(self.failure, "The stand-in tower history is unreadable", 503)
        return {"reused_products": list(self.reused), "refused_operations": list(TOWER_TRADE_OPERATIONS),
                "repair": REBIND_OPERATION, "client": TOWER_TRADE_CLIENT}


@dataclass
class World:
    wall: Wall
    resource_version: str = "resources-1"
    gate_issues: tuple[str, ...] = ()
    failure: Exception | None = None
    initializer: Callable = initialize
    cooking_border: int | None = None
    beginner_total: int | None = None
    towers: bool = False
    tower_trades: ReusingTowerTrades | None = None
    built: list = field(default_factory=list)

    def build(self, inputs, progress):
        progress("loading rules")
        if self.failure is not None:
            raise self.failure
        rules = inputs.config.load_rules()
        quest, trade = rules["quest-rules"], rules["trade-rules"]
        purchased = REGISTRY["purchased-login-rules"].normalize(rules["purchased-login-rules"])
        windows = REGISTRY["mission-windows"].content_version(rules["mission-windows"])
        versions = {"main_quests": REGISTRY["quest-rules"].content_version(quest),
                    "trades": REGISTRY["trade-rules"].content_version(trade)}
        # The window file is a resource like the prepared client files, so every consumer domain carries its version.
        for domain in REGISTRY["mission-windows"].domains:
            versions[domain] = json_digest({"carried": versions.get(domain), "windows": windows})
        identity = GenerationIdentity(
            code_digest="c" * 64, config_set=inputs.config.digest, content_versions=versions,
            master_snapshot="snapshot-1", base_resource_version=self.resource_version,
            effective_resource_version=json_digest({"base": self.resource_version, "purchased": purchased,
                                                    "windows": windows}))
        initializer, login_rewards = partial(self.initializer, trade.reset_type_1), None
        if self.cooking_border is not None:
            initializer = partial(cooked, self.cooking_border, initializer)
        if self.beginner_total is not None:
            initializer = partial(rewarded, self.beginner_total, initializer)
        if self.cooking_border is not None or self.beginner_total is not None:
            login_rewards = SimpleNamespace(cooking_border=self.cooking_border,
                                            beginner_total=DEFAULT_BEGINNER if self.beginner_total is None
                                            else self.beginner_total)
        towers = StampingTowers(rules["tower-rules"]) if self.towers else None
        bundle = SimpleNamespace(bootstrap=SimpleNamespace(clock=self.wall, rank_rows=list(RANKS)),
                                 initialize=initializer, login_rewards=login_rewards, towers=towers,
                                 tower_trades=self.tower_trades)
        readiness = {"startup_ready": True, "main_quests": {"available": True},
                     "trades": {"ready": trade.reset_type_2 != "unconfigured"}}
        self.built.append(inputs)
        return Generation(identity, bundle, {}, readiness, self.gate_issues)


class ReadbackPreflight:
    is_noop = False

    def __init__(self):
        self.generations, self.failure = [], None

    def check(self, before, candidate, domains, generation):
        self.generations.append(generation.id)
        if self.failure is not None:
            raise self.failure
        return PreflightReport({domain: (137,) for domain in domains})


def player_state(**fields):
    row = {"Money": 100, "Experience": 0, "Rank": 1, "NowCharisma": 10, "NowActionPoint": 5, "CharismaChangeDate": 0,
           "CharismaChangeTime": 0.0, "ActionPointChangeDate": 0, "ActionPointChangeTime": 0.0}
    return mark_new({"bootstrap_schema": 1, "tables": {"game_param": [row]}, "trade_policy": "month", **fields}, NOW)


def cooking_player(*, spent=COOKING_SPENT, now=NOW):
    # The saved trade policy already matches the default rules, so only the cooking ledger can need a migration.
    return {**initialize_cooking(cooking_base(now=now, previous=spent), border=BORDER), "trade_policy": "month"}


def beginner_player(*, total=DEFAULT_BEGINNER, claimed=0):
    state = player_state()
    state["tables"]["game_param2"] = [{"LastLoginDate": 0, "LogInStampCount": 0}]
    # Only a new account can hold beginner claims, and its stamps must match the mirrored game_param2 row.
    state = initialize_login_rewards(state, NOW, new_account=claimed > 0, beginner_count=total)
    if claimed:
        stamped = STAMPED_DAY
        state["login_rewards"].update(beginner_claimed=claimed, last_claim_day=stamped.isoformat(),
                                      stamp_month=month_key(stamped), stamp_count=1)
        state["tables"]["game_param2"][0].update(LastLoginDate=stamped.toordinal() - 1, LogInStampCount=1)
    return state


def battle_journal(*, active=True):
    battle = {"sequence": 1, "quest_id": 100, "start_key": "a" * 64, "master_snapshot": "snapshot-1",
              "resource_version": "resource-1"}
    return {"schema_version": 1, "next_sequence": 2, "last_client_time": None, "history": [],
            "active": battle if active else None}


def arguments(data, **overrides):
    return Namespace(data_dir=data, **{spec.dest: overrides.get(spec.dest) for spec in REGISTRY.values()})


def synchronous(target):
    target()


@dataclass
class Console:
    client: TestClient
    application: FastAPI
    store: Store
    runtime: GenerationRuntime
    world: World
    preflight: ReadbackPreflight
    clock: Monotonic
    wall: Wall
    data: Path
    args: Namespace

    @property
    def switch(self) -> ConfigSwitch:
        return config_switch(self.application)

    @property
    def sets(self) -> ConfigSets:
        return ConfigSets(self.data)

    def get(self, path, **options):
        return self.client.get(API_ROOT + path, headers=AUTH, **options)

    def post(self, path, body):
        return self.client.post(API_ROOT + path, json=body, headers=AUTH)


def build_console(tmp_path, *, state=None, overrides=None, drain_timeout=SHORT, installed=True, start=synchronous,
                  cooking_border=None, beginner_total=None, towers=False):
    data = tmp_path / "data"
    wall, clock = Wall(), Monotonic()
    world = World(wall, cooking_border=cooking_border, beginner_total=beginner_total, towers=towers)
    preflight = ReadbackPreflight()
    store = Store(data / DATABASE, player_state() if state is None else state, clock=wall)
    args = arguments(data, **(overrides or {}))
    inputs = GenerationInputs(resolve(args), data, tmp_path / "assets", tmp_path / "resources", "127.0.0.1", GAME_PORT,
                              StartupDiagnostics())
    runtime = GenerationRuntime(world.build(inputs, lambda message: None), clock=clock, admit_timeout=SHORT,
                                drain_timeout=drain_timeout, draining_limit=SHORT, start=start)
    routes = RouteRegistry()
    admin_routes_state.register(routes)
    admin_routes_config.register(routes)
    catalog = AdminCatalog(CommandRegistry(DOMAINS), ViewRegistry(), routes, ("admin_routes_config", "admin_routes_state"))
    application = create_admin_app(runtime, store, data_dir=data, admin_host="127.0.0.1", admin_port=PORT,
                                   game_port=GAME_PORT, token=TOKEN, preflight=preflight,
                                   restart_check=InitializerRestartCheck(), plan_store=PlanStore(),
                                   preflight_domains=DOMAINS, catalog=catalog, ui_directory=tmp_path / "ui", clock=clock)
    if installed:
        install(application, inputs=inputs, builder=world.build)
    return Console(TestClient(application, base_url=BASE), application, store, runtime, world, preflight, clock, wall,
                   data, args)


def operation():
    return str(uuid.uuid4())


def error_of(response):
    return response.json()["error"]


def validate(console, changes, *, op_id=None, **body):
    response = console.post("/config/validations", {"changes": changes, "op_id": op_id or operation(), **body})
    assert response.status_code == 200, response.text
    return response.json()


def report_for(console, job_id):
    response = console.get(f"/jobs/{job_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job"]["state"] == "succeeded" and body["report"] is not None, body
    return body["report"]


def validated(console, changes, **body):
    return report_for(console, validate(console, changes, **body)["job"]["job_id"])


def refreshed(console, report):
    return report_for(console, report["job_id"])


def switch_body(report, *, op_id=None, mode=None, client_closed=None, confirmations=None):
    body = {"report_id": report["report_id"], "op_id": op_id or operation()}
    if mode is not None:
        body["mode"] = mode
    if client_closed is not None or confirmations is not None:
        body["confirm"] = {"client_closed": bool(client_closed), "confirmations": list(confirmations or ())}
    return body


def switched(console, report, **options):
    response = console.post("/config/switches", switch_body(report, **options))
    assert response.status_code == 200, response.text
    return response.json()


def history(console):
    return [(row["sequence"], row["config_set"], row["tier"]) for row in reversed(console.store.list_generations().items)]


def audit(console):
    return console.get("/audit").json()["items"]


def failures(console):
    return console.get("/audit/failures").json()["items"]


def advance_state(console, **changes):
    profile = console.store.profile()
    console.store.update_state({**profile.state, **changes}, profile.revision)


def purchased():
    return json.loads((EXAMPLES / "purchased-login-rules.example.json").read_bytes())


def holding(runtime):
    entered, release, seen = threading.Event(), threading.Event(), []

    def request():
        with runtime.lease() as generation:
            seen.append(generation.id)
            entered.set()
            release.wait(WAIT)

    thread = threading.Thread(target=request)
    thread.start()
    assert entered.wait(WAIT)
    return thread, release, seen


def during_fence(console, action, **switch):
    thread, release, _ = holding(console.runtime)
    outcome = {}

    def run():
        try:
            outcome["result"] = console.switch.switch(op_id=operation(), **switch)
        except ProtocolError as error:
            outcome["error"] = error

    worker = threading.Thread(target=run)
    worker.start()
    try:
        deadline = time.monotonic() + WAIT
        while not console.runtime.status()["fenced"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert console.runtime.status()["fenced"] is True
        action()
    finally:
        release.set()
        thread.join(WAIT)
        worker.join(WAIT)
    assert console.runtime.status()["fenced"] is False
    return outcome


def test_quest_rule_changes_switch_hot_while_earlier_requests_finish_on_the_old_generation(tmp_path):
    console = build_console(tmp_path)
    old = console.runtime.current.id
    report = validated(console, GOLD)
    assert report["tier"] == "hot" and report["default_mode"] == "apply"
    assert report["reasons"] == {"identity": [], "accumulating": [], "initialize": False, "cooking": False,
                                 "beginner": False}
    assert report["migration"]["cooking"] is None
    [change] = report["keys"]
    assert change["key"] == "quest-rules" and change["source"] == {"before": "default", "after": "set"}
    assert (change["rules"]["before"]["gold_percent"], change["rules"]["after"]["gold_percent"]) == (100, 50)
    assert report["receipts_invalidated"] == ["main_quests"] and set(report["content_versions"]) == {"main_quests"}
    assert report["requirements"] == {"backup": False, "fence": False, "client_closed": False}
    assert report["modes"]["apply"] == {"allowed": True, "blockers": [], "confirmations": []}
    assert report["checks"]["preflight"] == {"ok": True, "readback": {"main_quests": [137]}}
    assert report["checks"]["audit"] == {"ok": True}
    assert report["notices"] == []
    thread, release, seen = holding(console.runtime)
    try:
        result = switched(console, report)
        status = console.runtime.status()
        assert status["current"]["id"] == report["generations"]["candidate"]["id"] != old
        assert status["draining"]["id"] == old and status["draining"]["leases"] == 1
    finally:
        release.set()
        thread.join(WAIT)
    assert seen == [old] and console.runtime.status()["draining"] is None
    set_id = report["validation"]["config_set"]["id"]
    assert (result["tier"], result["mode"], result["replayed"], result["config_set"]) == ("hot", "apply", False, set_id)
    assert result["pointer"]["published"] is True and result["pointer"]["active"]["sequence"] == 1
    assert history(console) == [(1, set_id, "hot")]
    active = console.sets.read_active()
    assert (active.set_id, active.sequence) == (set_id, 1)
    [record] = audit(console)
    assert (record["op_id"], record["kind"], record["targets"]) == (result["op_id"], "config", ["quest-rules"])
    assert record["revision_before"] == record["revision_after"] == 0 and record["generation_id"] == old
    assert console.store.list_backups().items == () and console.store.profile().revision == 0


def test_switch_operation_ids_replay_without_switching_twice(tmp_path):
    console = build_console(tmp_path)
    report = validated(console, GOLD)
    op_id = operation()
    first = switched(console, report, op_id=op_id)
    again = switched(console, report, op_id=op_id)
    assert {**again, "pointer": None} == {**first, "replayed": True, "pointer": None}
    assert again["pointer"]["active"] == first["pointer"]["active"]
    assert len(history(console)) == 1 and len(audit(console)) == 1
    retired = console.post("/config/switches", switch_body(report))
    assert retired.status_code == 409 and error_of(retired)["code"] == "config_report_expired"
    other = validated(console, {"quest-rules": {"gold_percent": 60}})
    conflict = console.post("/config/switches", switch_body(other, op_id=op_id))
    assert conflict.status_code == 409 and error_of(conflict)["code"] == "admin_operation_conflict"
    assert console.runtime.current.id == first["generation_after"]


def test_switches_replay_operations_committed_before_they_took_the_switch_lock(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    report = validated(console, GOLD)
    op_id = operation()
    first = switched(console, report, op_id=op_id)
    recorded, lookups = admin_config_switch.recorded_outcome, []

    def racing(store, operation_id, payload):
        lookups.append(operation_id)
        return None if len(lookups) == 1 else recorded(store, operation_id, payload)

    monkeypatch.setattr(admin_config_switch, "recorded_outcome", racing)
    again = switched(console, report, op_id=op_id)
    assert again["replayed"] is True and again["generation_after"] == first["generation_after"]
    assert lookups == [op_id, op_id] and len(history(console)) == 1 and len(audit(console)) == 1


def test_initialize_changes_switch_gated_inside_the_fence_with_the_audit(tmp_path):
    console = build_console(tmp_path)
    report = validated(console, NEVER_RESET)
    assert report["tier"] == "gated" and report["reasons"]["initialize"] is True
    assert report["migration"]["diff"]["entries"] == [
        {"op": "replace", "path": "/trade_policy", "before": "month", "after": "never"}]
    assert report["requirements"] == {"backup": True, "fence": True, "client_closed": False}
    calls = []

    def observed(name, original):
        def call(*arguments, **options):
            calls.append((name, console.runtime.status()["fenced"]))
            return original(*arguments, **options)

        return call

    console.store.backup = observed("backup", console.store.backup)
    console.store.commit_admin = observed("commit", console.store.commit_admin)
    op_id = operation()
    result = switched(console, report, op_id=op_id)
    assert calls == [("backup", False), ("commit", True)]
    profile = console.store.profile()
    assert profile.state["trade_policy"] == "never" and profile.revision == 1
    assert (result["tier"], result["revision_before"], result["revision_after"]) == ("gated", 0, 1)
    assert result["state_sha256_after"] == json_digest(profile.state) and result["readback"] == {"main_quests": [137]}
    [record] = audit(console)
    assert (record["op_id"], record["revision_before"], record["revision_after"]) == (op_id, 0, 1)
    assert record["state_sha256_after"] == json_digest(profile.state)
    assert record["diff"]["entries"] == report["migration"]["diff"]["entries"]
    [row] = console.store.list_generations().items
    assert (row["op_id"], row["tier"], row["generation_id"]) == (op_id, "gated", report["generations"]["candidate"]["id"])
    backup = result["backup_before_switch"]
    assert (backup["kind"], backup["op_id"], backup["revision"]) == (AUTO_BACKUP_KIND, op_id, 0)
    assert backup["reason"] == f"before gated switch to config set {result['config_set']}"
    assert read_backup(Path(backup["path"]), root=console.data).profile.state["trade_policy"] == "month"
    assert console.runtime.current.id == result["generation_after"] and console.runtime.status()["fenced"] is False
    assert console.preflight.generations[-1] == result["generation_after"]


def test_charisma_rate_changes_wait_for_battles_and_settle_at_the_previous_rate(tmp_path):
    console = build_console(tmp_path, state=player_state(quest_state=battle_journal()))
    hot = validated(console, GOLD)
    assert hot["battle"]["in_progress"] is True and hot["battle"]["required"] is False
    assert hot["modes"]["apply"]["allowed"] is True
    report = validated(console, CHARISMA)
    assert report["tier"] == "gated" and report["reasons"]["accumulating"] == ["quest-rules.charisma_recovery_seconds"]
    assert report["reasons"]["initialize"] is False
    assert report["battle"]["in_progress"] is True and report["battle"]["required"] is True
    assert [item["code"] for item in report["modes"]["apply"]["blockers"]] == ["admin_battle_abort_required"]
    assert [item["code"] for item in report["modes"]["stage"]["blockers"]] == ["config_stage_unsupported"]
    refused = console.post("/config/switches", switch_body(report))
    assert refused.status_code == 409 and error_of(refused)["code"] == "admin_battle_abort_required"
    assert console.store.list_backups().items == () and history(console) == []
    advance_state(console, quest_state=battle_journal(active=False))
    console.wall.now += timedelta(minutes=10)
    fresh = refreshed(console, report)
    assert fresh["report_id"] != report["report_id"] and fresh["modes"]["apply"]["allowed"] is True
    assert fresh["migration"]["changed"] is True and fresh["reasons"]["initialize"] is False
    [energy] = fresh["migration"]["energy"]
    assert (energy["key"], energy["field"], energy["before"], energy["after"]) == (
        "quest-rules", "charisma_recovery_seconds", 180, 60)
    result = switched(console, fresh)
    state = console.store.profile().state
    [row] = state["tables"]["game_param"]
    start = anchor_seconds(player_state()["tables"]["game_param"][0])
    assert row["NowCharisma"] == 13 and anchor_seconds(row) == start + 3 * 180
    assert result["energy"][0]["settled_at"]["utc"] == (NOW + timedelta(minutes=10)).isoformat()
    assert state["quest_state"]["active"] is None and result["revision_after"] == 2


def test_time_accumulating_rules_without_a_migration_cannot_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_config_switch, "ACCUMULATING_MIGRATIONS", MappingProxyType({}))
    console = build_console(tmp_path)
    report = validated(console, CHARISMA)
    error = report["checks"]["migration"]["error"]
    assert error["code"] == "config_migration_unsupported"
    assert error["details"] == {"key": "quest-rules", "field": "charisma_recovery_seconds"}
    assert not any(mode["allowed"] for mode in report["modes"].values())
    refused = console.post("/config/switches", switch_body(report))
    assert refused.status_code == 409 and error_of(refused)["code"] == "config_migration_unsupported"


def test_every_time_accumulating_rule_settles_through_a_migration():
    migrations = admin_config_switch.ACCUMULATING_MIGRATIONS
    assert {name: migrations.get(("tower-rules", name)) for name in TOWER_HOURS} == dict.fromkeys(
        TOWER_HOURS, admin_config_switch.settle_towers)
    assert all(spec.time_semantics is not TimeSemantics.TIME_ACCUMULATING for spec in REGISTRY.values())
    accumulating = {(spec.key, name) for spec in REGISTRY.values() for name in spec.field_semantics
                    if spec.semantics_for(name) is TimeSemantics.TIME_ACCUMULATING}
    assert accumulating == set(migrations)


@pytest.mark.parametrize("name", TOWER_HOURS)
def test_tower_hour_changes_settle_the_tower_clock_under_the_previous_hours(tmp_path, name):
    console = build_console(tmp_path, state=player_state(**TOWER_PROGRESS), towers=True)
    previous = getattr(TowerRules(), name)
    report = validated(console, {"tower-rules": {name: SHIFTED_HOUR}})
    assert report["tier"] == "gated" and report["reasons"]["accumulating"] == [f"tower-rules.{name}"]
    assert report["migration"]["changed"] is True and report["requirements"]["backup"] is True
    expected, _ = console.runtime.current.bundle.towers.settle(console.store.profile().state, NOW)
    result = switched(console, report)
    [energy] = result["energy"]
    assert (energy["key"], energy["field"], energy["before"], energy["after"]) == (
        "tower-rules", name, previous, SHIFTED_HOUR)
    state = console.store.profile().state
    assert state == expected and state[TOWER_STAMP][name] == previous != SHIFTED_HOUR
    assert result["revision_after"] == 1 and result["backup_before_switch"]["kind"] == AUTO_BACKUP_KIND


@pytest.mark.parametrize("state, towers", [
    (TOWER_PROGRESS, False),
    ({}, True),
], ids=["without_tower_service", "without_tower_progress"])
def test_tower_hour_changes_leave_saves_without_a_tower_clock_unchanged(tmp_path, state, towers):
    console = build_console(tmp_path, state=player_state(**state), towers=towers)
    before = console.store.profile()
    report = validated(console, {"tower-rules": {"generation_hour": SHIFTED_HOUR}})
    assert report["tier"] == "gated" and report["migration"]["changed"] is False
    assert report["requirements"] == {"backup": False, "fence": True, "client_closed": False}
    result = switched(console, report)
    assert [(item["key"], item["field"]) for item in result["energy"]] == [("tower-rules", "generation_hour")]
    assert (result["revision_after"], result["backup_before_switch"]) == (0, None)
    assert console.store.profile() == before and console.store.list_backups().items == ()


def test_cooking_ledgers_at_the_candidate_border_switch_hot(tmp_path):
    console = build_console(tmp_path, state=cooking_player(), cooking_border=BORDER)
    report = validated(console, GOLD)
    assert (report["tier"], report["reasons"]["cooking"], report["migration"]["cooking"]) == ("hot", False, None)
    assert report["modes"]["stage"]["allowed"] is True
    result = switched(console, report)
    assert (result["tier"], console.store.profile().revision) == ("hot", 0)
    assert read_cooking(console.store.profile().state) == read_cooking(cooking_player())


def test_saves_without_a_cooking_ledger_need_no_cooking_migration():
    candidate = SimpleNamespace(bundle=SimpleNamespace(login_rewards=SimpleNamespace(cooking_border=RAISED_BORDER)))
    state = player_state()
    assert admin_config_switch.migrate_cooking_ledger(state, candidate, NOW) == (state, None)


def test_cooking_border_changes_migrate_the_ledger_inside_the_fence(tmp_path):
    console = build_console(tmp_path, state=cooking_player(), cooking_border=BORDER)
    console.world.cooking_border = RAISED_BORDER
    report = validated(console, GOLD)
    assert report["tier"] == "gated" and report["reasons"] == {
        "identity": [], "accumulating": [], "initialize": False, "cooking": True, "beginner": False}
    assert report["migration"]["cooking"] == {
        "period": COOKING_PERIOD, "issued": 0, "border": {"before": BORDER, "after": RAISED_BORDER},
        "quota": {"before": MAX_COOKING, "after": RAISED_QUOTA}, "migrated_at": server_time(NOW)}
    assert report["checks"]["migration"] == report["checks"]["restart_check"] == {"ok": True}
    assert report["modes"]["apply"]["allowed"] is True
    [blocker] = report["modes"]["stage"]["blockers"]
    assert (blocker["code"], blocker["details"]) == (
        "config_stage_unsupported", {"border": {"before": BORDER, "after": RAISED_BORDER}})
    staged = console.post("/config/switches", switch_body(report, mode="stage"))
    assert staged.status_code == 409 and error_of(staged)["code"] == "config_stage_unsupported"
    assert history(console) == [] and console.sets.read_active() is None
    result = switched(console, report)
    assert (result["tier"], result["revision_before"], result["revision_after"]) == ("gated", 0, 1)
    assert result["cooking"] == report["migration"]["cooking"]
    assert result["backup_before_switch"]["kind"] == AUTO_BACKUP_KIND
    assert read_cooking(console.store.profile().state) == {
        **read_cooking(cooking_player()), "border": RAISED_BORDER, "quota": RAISED_QUOTA}
    [record] = audit(console)
    assert record["diff"]["entries"] == report["migration"]["diff"]["entries"]


def test_cooking_ledgers_of_an_ended_month_change_only_their_border(tmp_path):
    console = build_console(tmp_path, state=cooking_player(), cooking_border=BORDER)
    console.world.cooking_border, console.wall.now = RAISED_BORDER, NEXT_MONTH
    report = validated(console, GOLD)
    cooking = report["migration"]["cooking"]
    assert (cooking["period"], cooking["quota"]) == (COOKING_PERIOD, {"before": MAX_COOKING, "after": MAX_COOKING})
    switched(console, report)
    assert read_cooking(console.store.profile().state) == {**read_cooking(cooking_player()), "border": RAISED_BORDER}


def issued_above_the_tight_quota() -> dict:
    yesterday = NOW - timedelta(days=1)
    state, _ = advance_cooking(cooking_player(spent=ISSUED_SPENT, now=yesterday), yesterday, border=BORDER,
                               eligible=True)
    state, _ = advance_cooking(state, NOW, border=BORDER, eligible=True)
    assert read_cooking(state)["issued"] > (ISSUED_SPENT - TIGHT_BORDER) // CRYSTALS_PER_COOKING
    return state


def saved_after_the_switch_clock() -> dict:
    return cooking_player(now=NOW + timedelta(hours=1))


@pytest.mark.parametrize("saved, border, code", [
    (issued_above_the_tight_quota, TIGHT_BORDER, "login_cooking_quota_below_issued"),
    (saved_after_the_switch_clock, RAISED_BORDER, "login_cooking_clock_backwards"),
], ids=["issued_above_quota", "clock_behind_save"])
def test_refused_cooking_migrations_block_every_mode(tmp_path, saved, border, code):
    console = build_console(tmp_path, state=saved(), cooking_border=BORDER)
    console.world.cooking_border = border
    report = validated(console, GOLD)
    assert report["checks"]["migration"]["error"]["code"] == code
    assert report["checks"]["restart_check"] == {"ok": True}
    assert report["migration"]["cooking"] is None and not any(mode["allowed"] for mode in report["modes"].values())
    refused = console.post("/config/switches", switch_body(report))
    assert refused.status_code == 409 and error_of(refused)["code"] == code
    assert history(console) == [] and console.store.list_backups().items == ()
    assert console.store.profile().revision == 0


def test_resource_updates_carry_the_cooking_migration_into_the_content_switch(tmp_path):
    console = build_console(tmp_path, state=cooking_player(), cooking_border=BORDER)
    console.world.resource_version, console.world.cooking_border = "resources-2", RAISED_BORDER
    report = validated(console, {})
    assert report["tier"] == "content" and report["reasons"]["cooking"] is True
    assert report["checks"]["migration"] == report["checks"]["restart_check"] == {"ok": True}
    assert "config_stage_unsupported" in {item["code"] for item in report["modes"]["stage"]["blockers"]}
    result = switched(console, report, mode="apply", client_closed=True,
                      confirmations=[CONTENT_PHRASE.format(revision=0)])
    assert (result["tier"], result["revision_after"]) == ("content", 1)
    assert result["cooking"] == report["migration"]["cooking"]
    assert read_cooking(console.store.profile().state)["border"] == RAISED_BORDER


def test_beginner_ledgers_at_the_candidate_total_switch_hot(tmp_path):
    console = build_console(tmp_path, state=beginner_player(), beginner_total=DEFAULT_BEGINNER)
    report = validated(console, GOLD)
    assert (report["tier"], report["reasons"]["beginner"], report["migration"]["beginner"]) == ("hot", False, None)
    result = switched(console, report)
    assert (result["tier"], console.store.profile().revision) == ("hot", 0)
    assert read_login_rewards(console.store.profile().state) == read_login_rewards(beginner_player())


def test_saves_without_a_login_reward_ledger_need_no_beginner_migration():
    candidate = SimpleNamespace(bundle=SimpleNamespace(login_rewards=SimpleNamespace(beginner_total=RAISED_BEGINNER)))
    state = player_state()
    assert admin_config_switch.rebase_beginner_ledger(state, candidate) == (state, None)


def test_beginner_total_changes_rebase_the_ledger_inside_the_fence(tmp_path):
    console = build_console(tmp_path, state=beginner_player(), beginner_total=DEFAULT_BEGINNER)
    console.world.beginner_total = RAISED_BEGINNER
    report = validated(console, GOLD)
    assert report["tier"] == "gated" and report["reasons"] == {
        "identity": [], "accumulating": [], "initialize": False, "cooking": False, "beginner": True}
    assert report["migration"]["beginner"] == {
        "total": {"before": DEFAULT_BEGINNER, "after": RAISED_BEGINNER}, "claimed": {"before": 0, "after": 0}}
    assert report["checks"]["migration"] == report["checks"]["restart_check"] == {"ok": True}
    assert report["modes"]["apply"]["allowed"] is True
    [blocker] = report["modes"]["stage"]["blockers"]
    assert (blocker["code"], blocker["details"]) == (
        "config_stage_unsupported", {"total": {"before": DEFAULT_BEGINNER, "after": RAISED_BEGINNER}})
    staged = console.post("/config/switches", switch_body(report, mode="stage"))
    assert staged.status_code == 409 and error_of(staged)["code"] == "config_stage_unsupported"
    assert history(console) == [] and console.sets.read_active() is None
    result = switched(console, report)
    assert (result["tier"], result["revision_before"], result["revision_after"]) == ("gated", 0, 1)
    assert result["beginner"] == report["migration"]["beginner"]
    assert result["backup_before_switch"]["kind"] == AUTO_BACKUP_KIND
    ledger = read_login_rewards(console.store.profile().state)
    assert (ledger["beginner_total"], ledger["beginner_claimed"]) == (RAISED_BEGINNER, 0)
    [record] = audit(console)
    assert record["diff"]["entries"] == report["migration"]["diff"]["entries"]


def test_a_lowered_beginner_total_keeps_only_the_claims_it_still_covers(tmp_path):
    console = build_console(tmp_path, state=beginner_player(claimed=5), beginner_total=DEFAULT_BEGINNER)
    console.world.beginner_total = LOWERED_BEGINNER
    report = validated(console, GOLD)
    assert report["migration"]["beginner"] == {
        "total": {"before": DEFAULT_BEGINNER, "after": LOWERED_BEGINNER},
        "claimed": {"before": 5, "after": LOWERED_BEGINNER}}
    switched(console, report)
    ledger = read_login_rewards(console.store.profile().state)
    assert (ledger["beginner_total"], ledger["beginner_claimed"]) == (LOWERED_BEGINNER, LOWERED_BEGINNER)


def test_resource_updates_carry_the_beginner_rebase_into_the_content_switch(tmp_path):
    console = build_console(tmp_path, state=beginner_player(), beginner_total=DEFAULT_BEGINNER)
    console.world.resource_version, console.world.beginner_total = "resources-2", RAISED_BEGINNER
    report = validated(console, {})
    assert report["tier"] == "content" and report["reasons"]["beginner"] is True
    assert report["checks"]["migration"] == report["checks"]["restart_check"] == {"ok": True}
    assert "config_stage_unsupported" in {item["code"] for item in report["modes"]["stage"]["blockers"]}
    result = switched(console, report, mode="apply", client_closed=True,
                      confirmations=[CONTENT_PHRASE.format(revision=0)])
    assert (result["tier"], result["revision_after"]) == ("content", 1)
    assert result["beginner"] == report["migration"]["beginner"]
    assert read_login_rewards(console.store.profile().state)["beginner_total"] == RAISED_BEGINNER


def test_reused_tower_products_are_noticed_without_changing_the_tier_or_the_modes(tmp_path):
    console = build_console(tmp_path)
    console.world.tower_trades = ReusingTowerTrades(REUSED_TOWER_PRODUCTS)
    report = validated(console, GOLD)
    assert report["notices"] == [{"code": TOWER_PRODUCT_REUSED, "reused_products": list(REUSED_TOWER_PRODUCTS),
                                  "refused_operations": list(TOWER_TRADE_OPERATIONS), "repair": REBIND_OPERATION,
                                  "client": TOWER_TRADE_CLIENT}]
    assert report["tier"] == "hot" and report["checks"]["restart_check"] == {"ok": True}
    assert report["modes"]["apply"] == {"allowed": True, "blockers": [], "confirmations": []}
    assert report["modes"]["stage"]["allowed"] is True
    result = switched(console, report)
    assert (result["tier"], console.store.profile().revision) == ("hot", 0)


@pytest.mark.parametrize("reused, failure", [((), None), (REUSED_TOWER_PRODUCTS, TOWER_HISTORY_INVALID)],
                         ids=["none_reused", "unreadable"])
def test_candidates_without_a_readable_reuse_carry_no_notice(tmp_path, reused, failure):
    console = build_console(tmp_path)
    console.world.tower_trades = ReusingTowerTrades(reused, failure)
    report = validated(console, GOLD)
    assert report["notices"] == [] and report["tier"] == "hot"
    assert report["modes"]["apply"] == {"allowed": True, "blockers": [], "confirmations": []}


def test_oversized_switch_records_are_refused_before_their_backup(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    report = validated(console, NEVER_RESET)
    monkeypatch.setattr(admin_commit, "MAX_AUDIT_FIELD_BYTES", 1)
    refused = console.post("/config/switches", switch_body(report))
    assert refused.status_code == 422 and error_of(refused)["code"] == admin_commit.AUDIT_TOO_LARGE
    assert console.store.list_backups().items == () and history(console) == []
    assert console.store.profile().revision == 0
    assert console.runtime.current.id == report["generations"]["current"]["id"]


def test_reports_measure_the_switch_record_and_block_every_mode_when_it_is_oversized(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    monkeypatch.setattr(admin_commit, "MAX_AUDIT_FIELD_BYTES", 1)
    report = validated(console, NEVER_RESET)
    error = report["checks"]["audit"]["error"]
    assert report["checks"]["audit"]["ok"] is False and error["code"] == admin_commit.AUDIT_TOO_LARGE
    assert report["modes"]["apply"]["blockers"] == report["modes"]["stage"]["blockers"] == [error]
    assert not any(mode["allowed"] for mode in report["modes"].values())
    for mode in ("apply", "stage"):
        refused = console.post("/config/switches", switch_body(report, mode=mode))
        assert refused.status_code == 422 and error_of(refused)["code"] == admin_commit.AUDIT_TOO_LARGE
    assert console.store.list_backups().items == () and history(console) == []
    assert console.store.profile().revision == 0 and console.sets.read_active() is None


def test_report_ids_leave_the_audit_measurement_out(tmp_path, monkeypatch):
    create, reports = admin_config_switch.SwitchReport.create, []

    def measured_twice(**options):
        reports.append(create(**options))
        with monkeypatch.context() as patch:
            patch.setattr(admin_commit, "MAX_AUDIT_FIELD_BYTES", 1)
            reports.append(create(**options))
        return reports[0]

    monkeypatch.setattr(admin_config_switch.SwitchReport, "create", measured_twice)
    report = validated(build_console(tmp_path), NEVER_RESET)
    fitting, oversized = reports
    assert fitting.report_id == oversized.report_id == report["report_id"]
    assert fitting.view["checks"]["audit"] == {"ok": True}
    assert oversized.view["checks"]["audit"]["error"]["code"] == admin_commit.AUDIT_TOO_LARGE


def test_reports_measure_the_record_the_default_mode_checks_before_its_backup(tmp_path, monkeypatch):
    previewed, applied = [], []
    preview, auditable = admin_config_switch.preview_audit, admin_config_switch.require_auditable

    def previewing(readback, record):
        previewed.append(record(readback))
        return preview(readback, record)

    def requiring(record):
        applied.append(record)
        return auditable(record)

    monkeypatch.setattr(admin_config_switch, "preview_audit", previewing)
    monkeypatch.setattr(admin_config_switch, "require_auditable", requiring)
    console = build_console(tmp_path)
    report = validated(console, NEVER_RESET)
    op_id = operation()
    switched(console, report, op_id=op_id, mode=report["default_mode"])
    [measured], [checked] = previewed, applied
    assert measured["result"]["op_id"] == admin_commit.PREVIEW_OP_ID
    assert {**measured, "result": {**measured["result"], "op_id": op_id}} == checked


def test_mission_window_changes_switch_content_for_every_consumer_domain(tmp_path):
    console = build_console(tmp_path, state=player_state(quest_state=battle_journal()))
    spec = REGISTRY["mission-windows"]
    windows = json.loads((EXAMPLES / "mission-windows.example.json").read_bytes())
    report = validated(console, {spec.key: windows})
    assert report["tier"] == "content" and report["default_mode"] == "stage"
    assert report["reasons"]["identity"] == ["effective_resource_version"]
    [change] = report["keys"]
    assert change["key"] == spec.key and change["resource_hint"] is True
    document = Path(report["validation"]["directory"]) / f"{spec.key}.json"
    actions = report["client_actions"]
    assert actions["restart_client"] is True
    assert {"name": spec.ps_parameter, "value": str(document)} in actions["prepare_client"]["arguments"]
    assert f"-{spec.ps_parameter} " in actions["prepare_client"]["command"]
    assert set(report["receipts_invalidated"]) >= set(spec.domains)
    assert set(report["content_versions"]) == set(report["receipts_invalidated"])
    assert report["battle"]["in_progress"] is True and report["battle"]["required"] is True
    assert [item["code"] for item in report["modes"]["apply"]["blockers"]] == ["admin_battle_abort_required"]
    assert report["modes"]["stage"]["allowed"] is True
    phrase = CONTENT_PHRASE.format(revision=0)
    refused = console.post("/config/switches", switch_body(report, mode="apply", client_closed=True,
                                                           confirmations=[phrase]))
    assert refused.status_code == 409 and error_of(refused)["code"] == "admin_battle_abort_required"
    assert history(console) == [] and console.store.list_backups().items == ()
    staged = switched(console, report)
    assert (staged["mode"], staged["tier"], staged["receipts_invalidated"]) == ("stage", "content", [])
    assert history(console) == [(1, report["validation"]["config_set"]["id"], "staged")]


@pytest.mark.parametrize("changes, options", [
    (GOLD, {}),
    (NEVER_RESET, {}),
    (GOLD, {"mode": "stage"}),
], ids=["hot", "fenced", "stage"])
def test_a_refused_commit_returns_the_candidate_for_the_retry(tmp_path, monkeypatch, changes, options):
    console = build_console(tmp_path)
    report = validated(console, changes)
    candidate_id = report["generations"]["candidate"]["id"]

    def refusing(*arguments, **keywords):
        raise AdminError(admin_commit.AUDIT_TOO_LARGE, "The audit record does not fit its limits", 422)

    with monkeypatch.context() as patch:
        patch.setattr(admin_config_switch, "commit_audited", refusing)
        refused = console.post("/config/switches", switch_body(report, **options))
    assert refused.status_code == 422 and error_of(refused)["code"] == admin_commit.AUDIT_TOO_LARGE
    candidate = console.runtime.candidate()
    assert candidate is not None and candidate.id == candidate_id
    assert console.runtime.status()["fenced"] is False and history(console) == []
    result = switched(console, report, **options)
    assert result["replayed"] is False and console.runtime.candidate() is None
    with pytest.raises(ProtocolError) as caught:
        console.runtime.take_candidate(candidate_id)
    assert (caught.value.code, caught.value.status) == ("generation_candidate_missing", 409)
    expected = candidate_id if options.get("mode") != "stage" else report["generations"]["current"]["id"]
    assert result["generation_after"] == expected and console.runtime.current.id == expected


def test_a_commit_race_in_the_fence_returns_the_candidate(tmp_path):
    console = build_console(tmp_path)
    report = validated(console, NEVER_RESET)
    candidate_id = report["generations"]["candidate"]["id"]
    commit = console.store.commit_admin

    def racing(*arguments, **keywords):
        raise RevisionConflict(console.store.profile().revision)

    console.store.commit_admin = racing
    refused = console.post("/config/switches", switch_body(report))
    assert refused.status_code == 409 and error_of(refused)["code"] == "admin_revision_conflict"
    candidate = console.runtime.candidate()
    assert candidate is not None and candidate.id == candidate_id
    assert console.runtime.status()["fenced"] is False
    console.store.commit_admin = commit
    result = switched(console, report)
    assert result["generation_after"] == candidate_id and console.runtime.current.id == candidate_id
    assert console.runtime.candidate() is None


def test_content_changes_stage_by_default_and_apply_only_with_a_closed_quiet_client(tmp_path):
    console = build_console(tmp_path)
    report = validated(console, {"purchased-login-rules": purchased()})
    set_id, directory = report["validation"]["config_set"]["id"], Path(report["validation"]["directory"])
    assert report["tier"] == "content" and report["default_mode"] == "stage"
    assert report["reasons"]["identity"] == ["effective_resource_version"] and report["checks"]["audit"] == {"ok": True}
    document = directory / "purchased-login-rules.json"
    actions = report["client_actions"]
    assert actions["restart_client"] is True and actions["config_set"] == str(directory)
    assert actions["prepare_client"]["arguments"] == [{"name": "PurchasedLoginRulesFile", "value": str(document)}]
    assert actions["prepare_client"]["command"] == f"client/prepare-client.ps1 -PurchasedLoginRulesFile '{document}'"
    phrase = CONTENT_PHRASE.format(revision=0)
    assert report["modes"]["apply"]["confirmations"] == [
        {"step": 0, "operation": "config.switch.content", "phrase": phrase}]
    running = console.runtime.current.id
    staged = switched(console, report)
    assert (staged["mode"], staged["tier"], staged["generation_after"]) == ("stage", "content", running)
    assert console.runtime.current.id == running and console.sets.read_active().set_id == set_id
    assert history(console) == [(1, set_id, "staged")] and console.store.list_backups().items == ()
    view = console.get("/config").json()
    assert view["staged"] == {"set": set_id, "keys": ["purchased-login-rules"]} and view["running"]["set"] == EMPTY_SET
    assert console.get(f"/jobs/{report['job_id']}").json()["candidate_expired"] is True
    again = validated(console, {"purchased-login-rules": purchased()})
    assert again["staged"] == {"set": set_id, "is_candidate": True}
    assert [item["code"] for item in again["modes"]["stage"]["blockers"]] == ["config_unchanged"]
    assert again["migration"]["changed"] is False
    assert again["requirements"] == {"backup": False, "fence": True, "client_closed": True}
    unconfirmed = console.post("/config/switches", switch_body(again, mode="apply"))
    assert unconfirmed.status_code == 422 and error_of(unconfirmed)["code"] == "admin_client_closed_required"
    console.runtime.observe_request("115")
    console.clock.advance(CLIENT_QUIET_SECONDS - 1)
    busy = console.post("/config/switches", switch_body(again, mode="apply", client_closed=True, confirmations=[phrase]))
    assert busy.status_code == 409 and error_of(busy)["code"] == "admin_client_active"
    console.clock.advance(1)
    missing = console.post("/config/switches", switch_body(again, mode="apply", client_closed=True))
    assert missing.status_code == 422 and error_of(missing)["code"] == "admin_confirmation_required"
    assert console.store.list_backups().items == () and console.runtime.current.id == running
    applied = switched(console, again, mode="apply", client_closed=True, confirmations=[phrase])
    assert (applied["mode"], applied["tier"]) == ("apply", "content")
    assert console.runtime.current.id == applied["generation_after"] != running
    assert (applied["revision_before"], applied["revision_after"], applied["backup_before_switch"]) == (0, 0, None)
    assert console.store.list_backups().items == () and console.store.profile().revision == 0
    assert history(console) == [(1, set_id, "staged"), (2, set_id, "content")]
    warning = console.get("/config").json()["client_warning"]
    assert warning["config_set"] == set_id and warning["client_actions"]["restart_client"] is True
    console.runtime.observe_request("115")
    assert console.get("/config").json()["client_warning"] is not None
    console.runtime.observe_request("31")
    view = console.get("/config").json()
    assert view["client_warning"] is None and view["staged"] is None and view["running"]["set"] == set_id
    assert [item["kind"] for item in failures(console)] == ["config"] * 3


def test_a_restarted_client_clears_the_content_warning_before_a_later_swap(tmp_path):
    console = build_console(tmp_path)
    content = validated(console, {"purchased-login-rules": purchased()})
    switched(console, content, mode="apply", client_closed=True, confirmations=[CONTENT_PHRASE.format(revision=0)])
    console.runtime.observe_request("31")
    assert switched(console, validated(console, GOLD))["tier"] == "hot"
    assert console.runtime.session_observed_since_switch() is False
    assert console.get("/config").json()["client_warning"] is None


def test_running_content_switches_wait_for_battles_but_staging_does_not(tmp_path):
    console = build_console(tmp_path, state=player_state(quest_state=battle_journal()))
    report = validated(console, {"purchased-login-rules": purchased()})
    assert report["battle"]["in_progress"] is True and report["battle"]["required"] is True
    assert [item["code"] for item in report["modes"]["apply"]["blockers"]] == ["admin_battle_abort_required"]
    assert report["modes"]["stage"]["allowed"] is True
    phrase = CONTENT_PHRASE.format(revision=0)
    refused = console.post("/config/switches", switch_body(report, mode="apply", client_closed=True,
                                                          confirmations=[phrase]))
    assert refused.status_code == 409 and error_of(refused)["code"] == "admin_battle_abort_required"
    assert console.store.list_backups().items == () and history(console) == []
    assert switched(console, report)["mode"] == "stage"


def test_switching_refuses_a_generation_replaced_outside_the_console(tmp_path):
    console = build_console(tmp_path)
    report = validated(console, GOLD)
    console.world.resource_version = "resources-2"
    stranger = console.world.build(console.world.built[0], lambda message: None)
    console.runtime.swap(stranger, console.runtime.current.id)
    moved = console.post("/config/switches", switch_body(report))
    assert moved.status_code == 409 and error_of(moved)["details"] == {"changed": ["generation_id"]}
    for response in (console.post("/config/validations", {"changes": GOLD, "op_id": operation()}),
                     console.get(f"/jobs/{report['job_id']}")):
        assert response.status_code == 409 and error_of(response)["code"] == "generation_changed"
    assert history(console) == []


def test_fenced_switches_refuse_a_generation_replaced_after_their_backup(tmp_path):
    console = build_console(tmp_path)
    report = validated(console, NEVER_RESET)
    backup = console.store.backup

    def replaced(*arguments, **options):
        registered = backup(*arguments, **options)
        console.world.resource_version = "resources-2"
        stranger = console.world.build(console.world.built[0], lambda message: None)
        console.runtime.swap(stranger, console.runtime.current.id)
        return registered

    console.store.backup = replaced
    moved = console.post("/config/switches", switch_body(report))
    assert moved.status_code == 409 and error_of(moved)["code"] == "config_report_stale"
    assert error_of(moved)["details"] == {"changed": ["generation_id"]}
    assert history(console) == [] and console.store.profile().revision == 0
    assert console.runtime.status()["fenced"] is False


def test_fenced_switches_refuse_a_state_change_their_skipped_backup_missed(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    report = validated(console, NEVER_RESET)
    checked, seen = ConfigSwitch._checked, []

    def unchanged_before_the_fence(self, *arguments):
        assessment = checked(self, *arguments)
        seen.append(assessment.changed)
        return assessment if len(seen) > 1 else replace(assessment, state_sha256_after=assessment.state_sha256_before)

    monkeypatch.setattr(ConfigSwitch, "_checked", unchanged_before_the_fence)
    before = console.store.profile()
    refused = console.post("/config/switches", switch_body(report))
    assert refused.status_code == 409 and error_of(refused)["code"] == "config_report_stale"
    assert error_of(refused)["details"] == {"changed": ["state_sha256"]} and seen == [True, True]
    assert console.store.profile() == before and console.store.list_backups().items == () and history(console) == []
    assert console.runtime.status()["fenced"] is False
    monkeypatch.undo()
    assert switched(console, report)["backup_before_switch"]["kind"] == AUTO_BACKUP_KIND


def test_unstaging_refuses_a_running_set_a_concurrent_switch_replaced(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    staged = switched(console, validated(console, GOLD), mode="stage")["config_set"]
    apply = validated(console, GOLD)
    recorded, raced = admin_config_switch.recorded_outcome, []

    def racing(store, operation_id, payload):
        if not raced:
            raced.append(operation_id)
            console.switch.switch(report_id=apply["report_id"], op_id=operation())
        return recorded(store, operation_id, payload)

    monkeypatch.setattr(admin_config_switch, "recorded_outcome", racing)
    moved = console.post("/config/unstage", {"staged_set": staged, "op_id": operation()})
    assert moved.status_code == 409 and error_of(moved)["details"] == {"changed": ["running_set"]}
    assert [row[2] for row in history(console)] == ["staged", "hot"] and console.sets.read_active().set_id == staged


def test_staged_configurations_block_other_switches_until_the_staging_is_undone(tmp_path):
    console = build_console(tmp_path)
    content = validated(console, {"purchased-login-rules": purchased()})
    staged = content["validation"]["config_set"]["id"]
    switched(console, content)
    hot = validated(console, GOLD)
    assert hot["staged"] == {"set": staged, "is_candidate": False}
    assert hot["modes"]["apply"]["blockers"][0]["code"] == "config_staged_pending"
    refused = console.post("/config/switches", switch_body(hot))
    assert refused.status_code == 409 and error_of(refused)["code"] == "config_staged_pending"
    assert error_of(refused)["details"] == {"staged_set": staged}
    wrong = console.post("/config/unstage", {"staged_set": "f" * 24, "op_id": operation()})
    assert wrong.status_code == 409 and error_of(wrong)["code"] == "config_staged_changed"
    op_id = operation()
    undone = console.post("/config/unstage", {"staged_set": staged, "op_id": op_id})
    assert undone.status_code == 200, undone.text
    body = undone.json()
    assert (body["staged_set"], body["config_set"], body["keys"]) == (staged, EMPTY_SET, ["purchased-login-rules"])
    assert body["pointer"]["published"] is True and console.sets.read_active().set_id == EMPTY_SET
    assert history(console)[-1] == (2, EMPTY_SET, "unstaged")
    assert console.post("/config/unstage", {"staged_set": staged, "op_id": op_id}).json()["replayed"] is True
    nothing = console.post("/config/unstage", {"staged_set": staged, "op_id": operation()})
    assert nothing.status_code == 409 and error_of(nothing)["code"] == "config_not_staged"
    malformed = console.post("/config/unstage", {"staged_set": "../sets", "op_id": operation()})
    assert malformed.status_code == 422 and error_of(malformed)["code"] == "admin_params_invalid"
    fresh = refreshed(console, hot)
    assert fresh["staged"] is None and fresh["modes"]["apply"]["allowed"] is True
    assert switched(console, fresh)["tier"] == "hot"
    assert [row[2] for row in history(console)] == ["staged", "unstaged", "hot"]


def test_configuration_sets_that_cannot_be_written_are_reported(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    content = validated(console, {"purchased-login-rules": purchased()})
    staged = content["validation"]["config_set"]["id"]
    switched(console, content)

    def unwritable(self, *arguments, **options):
        raise PermissionError("The data directory is read-only")

    monkeypatch.setattr(ConfigSets, "create", unwritable)
    for response in (console.post("/config/validations", {"changes": GOLD, "op_id": operation()}),
                     console.post("/config/unstage", {"staged_set": staged, "op_id": operation()})):
        assert response.status_code == 503 and error_of(response)["code"] == "config_set_unwritable"
    assert console.sets.read_active().set_id == staged and [row[2] for row in history(console)] == ["staged"]
    assert [(item["kind"], item["error_code"]) for item in failures(console)] == [("config", "config_set_unwritable")]


def test_command_line_overrides_stay_read_only(tmp_path):
    override = tmp_path / "quest-rules.json"
    override.write_bytes(b'{"gold_percent": 75}')
    console = build_console(tmp_path, overrides={"quest_rules": override})
    refused = console.post("/config/validations", {"changes": GOLD, "op_id": operation()})
    assert refused.status_code == 409 and error_of(refused)["code"] == "config_key_overridden"
    assert [item["loc"] for item in error_of(refused)["fields"]] == [["changes", "quest-rules"]]
    view = console.get("/config").json()["running"]["config"]
    keys = {entry["key"]: entry for entry in view["keys"]}
    assert view["overridden"] == ["quest-rules"] and keys["quest-rules"]["read_only"] is True
    report = validated(console, {"trade-rules": {"reset_type_2": "never"}})
    assert [change["key"] for change in report["keys"]] == ["trade-rules"] and report["overridden"] == ["quest-rules"]
    candidate = console.world.built[-1].config.entries["quest-rules"]
    assert (candidate.source, candidate.path) == ("cli", override)
    override.write_bytes(b'{"gold_percent": 80}')
    drifted = console.post("/config/validations", {"changes": {"trade-rules": {"reset_type_2": "unconfigured"}},
                                                   "op_id": operation()})
    assert drifted.status_code == 409 and error_of(drifted)["code"] == "config_override_changed"
    assert error_of(drifted)["details"] == {"keys": ["quest-rules"]}


def test_roll_forward_moves_a_pointer_left_behind_after_the_commit(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    report = validated(console, NEVER_RESET)

    def crash(self, *arguments, **options):
        raise Crash

    with monkeypatch.context() as patch:
        patch.setattr(ConfigSets, "activate", crash)
        with pytest.raises(Crash):
            console.switch.switch(report_id=report["report_id"], op_id=operation())
    set_id = report["validation"]["config_set"]["id"]
    assert history(console) == [(1, set_id, "gated")] and console.sets.read_active() is None
    assert console.store.profile().state["trade_policy"] == "never" and console.runtime.status()["fenced"] is False
    assert resolve(console.args).config_set is None
    outcome = roll_forward(console.store, console.data)
    assert outcome["rolled_forward"] is True and outcome["previous"]["active"] is None
    assert (outcome["pointer"]["set"], outcome["pointer"]["active"]["sequence"]) == (set_id, 1)
    assert resolve(console.args).config_set.identifier == set_id
    assert roll_forward(console.store, console.data)["rolled_forward"] is False


def test_roll_forward_follows_only_newer_committed_switches(tmp_path):
    data = tmp_path / "data"
    store = Store(data / DATABASE, player_state())
    sets = ConfigSets(data)
    first = sets.create({"trade-rules": {"reset_type_1": "never"}}, source="console")
    second = sets.create({"trade-rules": {"reset_type_2": "never"}}, source="console")
    untouched = roll_forward(store, data)
    assert (untouched["rolled_forward"], untouched["switch"]) == (False, None) and sets.read_active() is None
    store.record_generation(first.identifier, "gated", op_id=operation())
    (sets.root / ACTIVE_POINTER).write_bytes(b'{"set": "broken"}')
    repaired = roll_forward(store, data)
    assert repaired["rolled_forward"] is True and repaired["previous"]["error"]["code"] == "config_pointer_invalid"
    assert (sets.read_active().set_id, sets.read_active().sequence) == (first.identifier, 1)
    store.record_generation(second.identifier, "hot", op_id=operation())
    assert roll_forward(store, data)["pointer"]["active"]["sequence"] == 2
    assert sets.read_active().set_id == second.identifier
    ahead = sets.activate(first.identifier, 9, expected=sets.pointer_digest())
    store.record_generation("f" * 24, "hot")
    kept = roll_forward(store, data)
    assert kept["rolled_forward"] is False and sets.read_active() == ahead
    (sets.root / ACTIVE_POINTER).unlink()
    with pytest.raises(ConfigError) as caught:
        roll_forward(store, data)
    assert caught.value.code == "config_set_unknown"
    with pytest.raises(ValueError):
        roll_forward(store, tmp_path / "elsewhere")


def test_concurrent_switches_are_refused_while_one_waits_in_the_fence(tmp_path):
    console = build_console(tmp_path, drain_timeout=WAIT)
    earlier, done = validated(console, GOLD), operation()
    finished = switched(console, earlier, op_id=done)
    report = validated(console, NEVER_RESET)

    def contend():
        busy = console.post("/config/switches", switch_body(report))
        assert busy.status_code == 409 and error_of(busy)["code"] == "config_switch_busy"
        unstage = console.post("/config/unstage", {"staged_set": EMPTY_SET, "op_id": operation()})
        assert unstage.status_code == 409 and error_of(unstage)["code"] == "config_switch_busy"
        replay = console.post("/config/switches", switch_body(earlier, op_id=done))
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["generation_after"] == finished["generation_after"]

    outcome = during_fence(console, contend, report_id=report["report_id"])
    assert outcome["result"]["tier"] == "gated" and [row[2] for row in history(console)] == ["hot", "gated"]
    assert console.get("/config").json()["switching"] is False


def test_fenced_switches_recheck_what_draining_requests_changed(tmp_path):
    console = build_console(tmp_path, drain_timeout=WAIT)
    gated = validated(console, NEVER_RESET)
    written = during_fence(console, lambda: advance_state(console, wallet=1), report_id=gated["report_id"])
    assert written["error"].code == "admin_revision_conflict" and history(console) == []
    assert switched(console, refreshed(console, gated))["revision_before"] == 1
    content = validated(console, {"purchased-login-rules": purchased()})
    phrase = CONTENT_PHRASE.format(revision=content["revision"])
    restarted = during_fence(console, lambda: console.runtime.observe_request("31"), report_id=content["report_id"],
                             mode="apply", client_closed=True, confirmations=[phrase])
    assert restarted["error"].code == "admin_client_active"
    assert [row[2] for row in history(console)] == ["gated"]
    assert console.runtime.current.id == content["generations"]["current"]["id"]


def test_reports_name_serve_gate_issues_availability_regressions_and_rejected_states(tmp_path):
    console = build_console(tmp_path)
    regression = validated(console, {"trade-rules": {"reset_type_2": "unconfigured"}})
    assert regression["tier"] == "hot" and regression["modes"]["apply"]["allowed"] is True
    assert regression["availability"] == {"regressions": ["trades"],
                                          "changes": {"trades": {"before": True, "after": False}}}
    console.world.gate_issues = (GATE_ISSUE,)
    blocked = validated(console, GOLD)
    assert blocked["checks"]["serve_gate"]["error"]["details"] == {"gate_issues": [GATE_ISSUE]}
    assert blocked["generations"]["candidate"]["gate_issues"] == [GATE_ISSUE]
    assert all(mode["blockers"][0]["code"] == "config_candidate_blocked" for mode in blocked["modes"].values())
    refused = console.post("/config/switches", switch_body(blocked))
    assert refused.status_code == 409 and error_of(refused)["code"] == "config_candidate_blocked"
    console.world.gate_issues = ()
    rejected = validated(console, {"trade-rules": {"reset_type_1": "unconfigured"}})
    error = rejected["checks"]["restart_check"]["error"]
    assert error["code"] == "admin_restart_incompatible" and error["details"]["error"]["code"] == "trade_policy_unavailable"
    assert rejected["checks"]["preflight"]["ok"] is True
    assert not any(mode["allowed"] for mode in rejected["modes"].values())
    console.world.initializer = restless
    rewritten = validated(console, GOLD)["checks"]["restart_check"]["error"]
    assert rewritten["code"] == "admin_restart_incompatible"
    assert rewritten["details"]["diff"]["entries"] == [{"op": "replace", "path": "/initialized", "before": 1, "after": 2}]
    console.world.initializer = initialize
    console.preflight.failure = ProtocolError("admin_preflight_failed", "Response 36 cannot be encoded", 409)
    unreadable = validated(console, GOLD)
    assert unreadable["checks"]["preflight"]["error"]["code"] == "admin_preflight_failed"
    assert unreadable["checks"]["audit"] == admin_commit.AUDIT_SKIPPED
    assert all([item["code"] for item in mode["blockers"]] == ["admin_preflight_failed"]
               for mode in unreadable["modes"].values())
    denied = console.post("/config/switches", switch_body(unreadable))
    assert denied.status_code == 409 and error_of(denied)["code"] == "admin_preflight_failed"
    assert history(console) == [] and console.store.profile().revision == 0


@pytest.mark.parametrize("body, status, code", [
    ({"changes": {"quest-rules": {"gold_percent": -1}}}, 400, "config_rules_invalid"),
    ({"changes": {"unknown-rules": {}}}, 400, "config_key_unknown"),
    ({"changes": {"rules": {"bootstrap_token": "x"}}}, 400, "config_secret_field"),
    ({"changes": {}, "base_set": "0" * 24}, 404, "config_set_unknown"),
    ({"changes": {}, "base_set": "../sets"}, 422, "admin_params_invalid"),
    ({"changes": {"quest-rules": [1]}}, 422, "admin_params_invalid"),
    ({"changes": {}, "unexpected": 1}, 422, "admin_params_invalid"),
    ({"changes": {}, "op_id": "not-a-uuid"}, 422, "admin_op_id_invalid"),
], ids=("rules", "unknown_key", "secret", "unknown_base", "malformed_base", "document_shape", "extra", "op_id"))
def test_validation_requests_are_strict(tmp_path, body, status, code):
    console = build_console(tmp_path)
    response = console.post("/config/validations", {"op_id": operation(), **body})
    assert response.status_code == status and error_of(response)["code"] == code
    assert len(console.world.built) == 1 and console.get("/config").json()["validation"] is None


def test_validation_errors_name_the_key_and_operation_ids_are_idempotent(tmp_path):
    console = build_console(tmp_path)
    invalid = console.post("/config/validations", {"changes": {"quest-rules": {"gold_percent": -1}}, "op_id": operation()})
    error = error_of(invalid)
    assert error["fields"] == [{"loc": ["changes", "quest-rules"], "type": "config_rules_invalid", "msg": error["message"]}]
    assert error["details"] == {"key": "quest-rules"}
    op_id = operation()
    first = validate(console, GOLD, op_id=op_id)
    assert first["job"]["progress"] == ["loading rules"] and first["validation"]["keys"] == ["quest-rules"]
    assert validate(console, GOLD, op_id=op_id) == first and len(console.world.built) == 2
    conflict = console.post("/config/validations", {"changes": NEVER_RESET, "op_id": op_id})
    assert conflict.status_code == 409 and error_of(conflict)["code"] == "admin_operation_conflict"
    unknown = console.get("/jobs/" + "0" * 32)
    assert unknown.status_code == 404 and error_of(unknown)["code"] == "config_job_unknown"
    second = validate(console, NEVER_RESET)
    assert console.get(f"/jobs/{first['job']['job_id']}").status_code == 404
    assert console.get("/config").json()["validation"]["validation"]["job_id"] == second["job"]["job_id"]


def test_validation_jobs_report_running_failed_and_busy_builds(tmp_path):
    deferred = Deferred()
    console = build_console(tmp_path, start=deferred)
    job_id = validate(console, GOLD)["job"]["job_id"]
    running = console.get(f"/jobs/{job_id}").json()
    assert running["job"]["state"] == "running" and running["report"] is None
    busy = console.post("/config/validations", {"changes": NEVER_RESET, "op_id": operation()})
    assert busy.status_code == 409 and error_of(busy)["code"] == "generation_build_busy"
    deferred.run()
    report = report_for(console, job_id)
    assert report["tier"] == "hot" and report["client"]["quiet"] is True
    console.runtime.observe_request("115")
    again = refreshed(console, report)
    assert again["report_id"] == report["report_id"] and again["client"]["quiet"] is False
    console.world.failure = ProtocolError("masterdata_missing", "Master data is not installed", 503)
    failed_id = validate(console, NEVER_RESET)["job"]["job_id"]
    deferred.run()
    failed = console.get(f"/jobs/{failed_id}").json()
    assert failed["job"]["state"] == "failed" and failed["report"] is None
    assert failed["job"]["error"] == {"code": "masterdata_missing", "message": "Master data is not installed"}
    expired = console.post("/config/switches", switch_body(report))
    assert expired.status_code == 409 and error_of(expired)["code"] == "config_report_expired"


def test_switches_refuse_reports_that_no_longer_match(tmp_path):
    console = build_console(tmp_path)
    report = validated(console, GOLD)
    sets = console.sets
    sets.activate(sets.create({}, source="console").identifier, 0, expected=None)
    moved = console.post("/config/switches", switch_body(report))
    assert moved.status_code == 409 and error_of(moved)["code"] == "config_report_stale"
    assert error_of(moved)["details"] == {"changed": ["pointer"]}
    (sets.root / ACTIVE_POINTER).unlink()
    expired = console.post("/config/switches", {"report_id": "0" * 64, "op_id": operation()})
    assert expired.status_code == 409 and error_of(expired)["code"] == "config_report_expired"
    malformed = console.post("/config/switches", {"report_id": "XYZ", "op_id": operation()})
    assert malformed.status_code == 422 and error_of(malformed)["code"] == "admin_plan_digest_invalid"
    gated = validated(console, NEVER_RESET)
    advance_state(console, wallet=1)
    conflict = console.post("/config/switches", switch_body(gated))
    assert conflict.status_code == 409 and error_of(conflict)["code"] == "admin_revision_conflict"
    assert error_of(conflict)["current_revision"] == 1 and console.store.list_backups().items == ()
    fresh = refreshed(console, gated)
    console.clock.advance(CANDIDATE_TTL_SECONDS + 1)
    missing = console.post("/config/switches", switch_body(fresh))
    assert missing.status_code == 409 and error_of(missing)["code"] == "generation_candidate_missing"
    assert console.get(f"/jobs/{fresh['job_id']}").json()["candidate_expired"] is True
    assert history(console) == [] and [item["kind"] for item in failures(console)] == ["config"] * 5


def test_switches_refuse_a_tier_the_state_no_longer_matches(tmp_path):
    console = build_console(tmp_path)
    report = validated(console, GOLD)
    advance_state(console, trade_policy="never")
    for mode in ("apply", "stage"):
        changed = console.post("/config/switches", switch_body(report, mode=mode))
        assert changed.status_code == 409 and error_of(changed)["code"] == "config_tier_changed"
        assert error_of(changed)["details"] == {"reported": "hot", "current": "gated"}
    assert history(console) == [] and console.runtime.current.id == report["generations"]["current"]["id"]
    assert console.sets.read_active() is None


def test_switches_refuse_a_candidate_identical_to_the_running_generation(tmp_path):
    console = build_console(tmp_path)
    report = validated(console, {})
    assert report["generations"]["candidate"]["id"] == report["generations"]["current"]["id"]
    assert all([item["code"] for item in mode["blockers"]] == ["config_unchanged"] for mode in report["modes"].values())
    refused = console.post("/config/switches", switch_body(report))
    assert refused.status_code == 409 and error_of(refused)["code"] == "config_unchanged"
    assert history(console) == [] and console.sets.read_active() is None


def test_reports_are_cached_for_a_bounded_number_of_states(tmp_path):
    console = build_console(tmp_path)
    first = validated(console, GOLD)
    for _ in range(admin_config_switch.REPORT_CAPACITY):
        advance_state(console, wallet=console.store.profile().revision)
        refreshed(console, first)
    evicted = console.post("/config/switches", switch_body(first))
    assert evicted.status_code == 409 and error_of(evicted)["code"] == "config_report_expired"


def test_hot_switches_wait_until_the_previous_generation_drains(tmp_path):
    console = build_console(tmp_path)
    first = validated(console, GOLD)
    thread, release, _ = holding(console.runtime)
    try:
        switched(console, first)
        second = validated(console, {"quest-rules": {"gold_percent": 60}})
        draining = console.post("/config/switches", switch_body(second))
        assert draining.status_code == 409 and error_of(draining)["code"] == "generation_draining"
        assert history(console) == [(1, first["validation"]["config_set"]["id"], "hot")]
    finally:
        release.set()
        thread.join(WAIT)
    assert switched(console, second)["tier"] == "hot" and len(history(console)) == 2


def test_hot_switches_swap_before_a_fence_requested_during_their_commit(tmp_path):
    console = build_console(tmp_path, drain_timeout=WAIT)
    report = validated(console, GOLD)
    commit, fenced, workers = console.store.commit_admin, [], []

    def restore():
        with console.runtime.fence() as generation:
            fenced.append(generation.id)

    def committing(*arguments, **options):
        worker = threading.Thread(target=restore)
        workers.append(worker)
        worker.start()
        deadline = time.monotonic() + WAIT
        while not console.runtime.status()["fenced"] and time.monotonic() < deadline:
            time.sleep(0.01)
        return commit(*arguments, **options)

    console.store.commit_admin = committing
    result = switched(console, report)
    [worker] = workers
    worker.join(WAIT)
    assert fenced == [result["generation_after"]] and console.runtime.status()["fenced"] is False


def test_switches_refuse_a_pointer_ahead_of_the_switch_history(tmp_path):
    console = build_console(tmp_path)
    sets = console.sets
    sets.activate(sets.create({}, source="console").identifier, 7, expected=None)
    report = validated(console, GOLD)
    refused = console.post("/config/switches", switch_body(report))
    assert refused.status_code == 409 and error_of(refused)["code"] == "config_sequence_regressed"
    assert error_of(refused)["details"] == {"pointer_sequence": 7, "next_sequence": 1}
    assert history(console) == [] and console.runtime.current.id == report["generations"]["current"]["id"]
    ahead = sets.create(NEVER_RESET, source="console").identifier
    sets.activate(ahead, 8, expected=sets.pointer_digest())
    unstage = console.post("/config/unstage", {"staged_set": ahead, "op_id": operation()})
    assert unstage.status_code == 409 and error_of(unstage)["code"] == "config_sequence_regressed"
    assert history(console) == [] and sets.read_active().set_id == ahead


def test_unpublished_pointers_are_reported_and_the_next_switch_moves_them(tmp_path):
    console = build_console(tmp_path)
    report = validated(console, GOLD)
    set_id = report["validation"]["config_set"]["id"]
    with exclusive_file(console.sets.root / LOCK_NAME):
        result = switched(console, report)
    assert result["pointer"]["published"] is False and result["pointer"]["publish_error"]["code"] == "config_pointer_busy"
    assert console.runtime.current.id == result["generation_after"] and console.sets.read_active() is None
    view = console.get("/config").json()
    assert view["active"]["roll_forward_pending"] is True and view["active"]["next_start"] == set_id
    assert view["staged"] is None
    follow = validated(console, {"quest-rules": {"gold_percent": 60}})
    assert follow["modes"]["apply"]["allowed"] is True
    assert switched(console, follow)["pointer"]["published"] is True and console.sets.read_active().sequence == 2


def test_config_view_reports_running_active_history_and_validation(tmp_path):
    console = build_console(tmp_path)
    view = console.get("/config").json()
    assert view["running"]["set"] == EMPTY_SET and view["running"]["generation_id"] == console.runtime.current.id
    assert view["active"]["active"] is None and view["active"]["roll_forward_pending"] is False
    assert view["staged"] is None and view["history"] == {"items": [], "next_cursor": None}
    assert view["validation"] is None and view["client_warning"] is None and view["switching"] is False
    assert "active" not in view["running"]["config"] and len(view["running"]["config"]["keys"]) == len(REGISTRY)
    report = validated(console, GOLD)
    result = switched(console, report)
    view = console.get("/config").json()
    assert view["running"]["set"] == result["config_set"] == view["active"]["set"]
    assert [item["tier"] for item in view["history"]["items"]] == ["hot"]
    assert view["validation"]["validation"]["job_id"] == report["job_id"]
    keys = {entry["key"]: entry for entry in view["running"]["config"]["keys"]}
    assert keys["quest-rules"]["effective"]["gold_percent"] == 50 and keys["quest-rules"]["source"] == "set"
    (console.sets.root / ACTIVE_POINTER).write_bytes(b" " * (MAX_POINTER_BYTES + 1))
    broken = console.get("/config").json()
    assert broken["active"]["error"]["code"] == "config_pointer_invalid" and broken["staged"] is None
    assert (broken["active"]["roll_forward_pending"], broken["active"]["next_start"]) == (None, None)


def test_configuration_routes_need_the_installed_switch(tmp_path):
    console = build_console(tmp_path, installed=False)
    for response in (console.get("/config"), console.get("/jobs/" + "0" * 32),
                     console.post("/config/validations", {"changes": {}, "op_id": operation()}),
                     console.post("/config/switches", {"report_id": "0" * 64, "op_id": operation()})):
        assert response.status_code == 503 and error_of(response)["code"] == "config_switch_unavailable"
    assert [item["kind"] for item in failures(console)] == ["config"]


def test_install_requires_an_admin_application_and_matching_inputs(tmp_path):
    console = build_console(tmp_path, installed=False)
    inputs = console.world.built[0]
    with pytest.raises(ValueError):
        install(FastAPI(), inputs=inputs, builder=console.world.build)
    with pytest.raises(ValueError):
        install(console.application, inputs=replace(inputs, data_dir=tmp_path / "elsewhere"), builder=console.world.build)
    with pytest.raises(ValueError):
        install(console.application, inputs=inputs, builder=None)
    override = tmp_path / "quest-rules.json"
    override.write_bytes(b'{"gold_percent": 75}')
    stranger = replace(inputs, config=resolve(arguments(console.data, quest_rules=override)))
    with pytest.raises(ValueError):
        install(console.application, inputs=stranger, builder=console.world.build)
    switch = install(console.application, inputs=inputs, builder=console.world.build)
    assert config_switch(console.application) is switch

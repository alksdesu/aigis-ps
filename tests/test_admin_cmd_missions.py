import json
from collections.abc import Callable
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import cache
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from aigis_ps import admin_cmd_missions
from aigis_ps.admin_cmd_missions import (CLAIM_MISSIONS, CLAIM_PASS, COMMANDS, GRANT_POINTS, MISSIONS_SKIPPED,
                                         NOTHING_CLAIMABLE, NOTHING_REACHED, PASS_INACTIVE, POINTS_CAPPED,
                                         POINTS_DISCARDED, REWARDS_UNSUPPORTED)
from aigis_ps.admin_command import (COMMAND_UNAVAILABLE, AdminError, Batch, BatchStep, CommandContext, PlanStore,
                                    missing_service)
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_fields import PASS_LEDGER as PASS_JOURNAL
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.alxml import Column, Container
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_service import DELIVERY_KEY as PASS_DELIVERY, HISTORY_NAMESPACE as PASS_HISTORY, EventPassService
from aigis_ps.mission_service import (HISTORY_NAMESPACE as MISSION_HISTORY, MAX_MISSIONS,
                                      PASS_DELIVERY_KEY as MISSION_DELIVERY, MissionService)
from aigis_ps.mission_state import JOURNAL_KEY as MISSION_PROGRESS
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.request_ledger import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION as LEDGER_SCHEMA
from aigis_ps.storage import PlayerProfile, serialize
from test_admin_cmd_events import logged_warnings
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_seams import METHOD_KEY, PLAYER_ID, USER_KEY, MemoryStore, canonical
from test_event_pass_revival_cli import golden_revived, revived
from test_quest_service import request


GOLDEN = Path(__file__).resolve().parent / "golden" / "p25b"
MISSION_GOLDEN, PASS_GOLDEN = GOLDEN / "missions.json", GOLDEN / "event_passes.json"
NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
NEXT_DAY = NOW + timedelta(days=1)
LOAD, CLAIM, CLAIM_GROUP, MARK_READ = 54, 58, 59, 60
PASS_READ, PASS_CLAIM = 158, 159
MISSION_REGISTRATIONS, PASS_REGISTRATIONS = (LOAD, CLAIM, CLAIM_GROUP, MARK_READ), (PASS_READ, PASS_CLAIM)
SCOPE, SCOPE_FIELDS, PASS_FIELD = "GL", ("ID", "PT", "TabGroupId", "TaskGroupId"), "EPI"
PASS_ID, PREMIUM_ID, CLOSED_PASS = "event_pass_20261015", "event_pass_premium_20261015", "event_pass_20260611"
FOREIGN_KEY = "c" * 32
SENTINEL = "sentinel"
EXISTING_MAIL = {"kind": "gold", "amount": 1, "description": "既存"}
DAILY, MONTHLY, ATTRIBUTE, LIMITED = (1, 1), (2, 2), (4, 4), (5, 5)
SEQUENCE, ATTRIBUTE_GROUPS = 100, 4
# The daily login mission mails entrust tickets and carries 2 EP of the revived pass.
LOGIN_MISSION, DAILY_GOLD, UNFINISHED_DAILY = 1611, 1605, 1605
# Mission 1634 mails a ten-pull ticket whose pool is disabled, so its delivery answers 501.
UNSUPPORTED_MISSION, FIRST_SEQUENCE, UNKNOWN_MISSION = 1634, 10001, 999999
MONTHLY_UNIT = 1632
QUEST_CLEARS = 30
# The login base finishes the daily login mission; the busy base also finishes every daily, monthly and limited one.
LOGIN_BASE, BUSY_BASE = "login", "busy"
# Full points reach both tracks of the revived pass; the normal base holds 30 EP and no premium item.
FULL_BASE, NORMAL_BASE, EMPTY_BASE = "full", "normal", "empty"
FULL_POINTS, NORMAL_POINTS, EP_STACK = 200, 30, 999
OPERATIONS = ("event_pass.claim", "event_pass.grant_points", "missions.claim")
CONSOLE_GENERATION = "gen-missions"
# Missions 1607 and 1608 finish with the daily page and each carries 4 EP of the revived pass.
POINT_MISSION, NEXT_POINT_MISSION = 1607, 1608
DAILY_PAGE = (LOGIN_MISSION, DAILY_GOLD, 1606, POINT_MISSION, NEXT_POINT_MISSION, 1609, 1610)
PASSIVE_ITEM = "gift_0002_ruby"
MISSION_READBACK = {"missions": [54], "possessions": [129, 137], "presents": [42]}
POINT_READBACK, PASS_READBACK = {"possessions": [129, 137]}, {"event_pass": [137, 158], "presents": [42]}


class GoldenStore(MemoryStore):
    def overwrite(self, state: dict) -> None:
        # Stands in for an offline edit of the saved JSON, which leaves the stored revision alone.
        self._state = json.loads(serialize(state))


@dataclass(frozen=True)
class Step:
    registration: int
    columns: tuple = ()
    tick: int = 1000
    user_key: str = USER_KEY
    method_key: str = METHOD_KEY
    now: datetime = NOW
    prepare: Callable[[Any, dict], None] | None = None
    rewrite: Callable[[Any, dict], None] | None = None

    def envelope(self):
        profile = PlayerProfile(PLAYER_ID, self.user_key, self.method_key, {}, 0)
        return request(SimpleNamespace(profile=lambda: profile), self.registration, self.columns, tick=self.tick)

    def key(self) -> str:
        envelope = self.envelope()
        return sha256(f"{self.registration}:{envelope.profile.route}:{envelope.profile.st}:".encode()
                      + envelope.xml).hexdigest()


@dataclass(frozen=True)
class Case:
    steps: tuple[Step, ...]
    base: str
    stored: Callable[[Any, dict, tuple[Step, ...]], dict] | None = None


@dataclass(frozen=True)
class Family:
    golden: Path
    registrations: tuple[int, ...]
    build: Callable[[Any, SimpleNamespace], Any]
    handlers: Callable[[Any], dict]


def frozen_bootstrap(bundle, clock: SimpleNamespace):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: clock.now
    return bootstrap


def mission_service(bundle, clock: SimpleNamespace) -> MissionService:
    source = bundle.missions
    return MissionService(frozen_bootstrap(bundle, clock), bundle.player_state, bundle.presents, source.catalog,
                          progress=source.progress, skins=source.skins, fame=source.fame,
                          fame_status=source.fame_status, ep_bindings=source.ep_bindings,
                          pass_points=source.pass_points)


def pass_service(bundle, clock: SimpleNamespace) -> EventPassService:
    source = bundle.event_passes
    return EventPassService(frozen_bootstrap(bundle, clock), bundle.player_state, bundle.presents, bundle.possessions,
                            source.catalog, automation_rules=source.automation_rules)


def mission_handlers(service: MissionService) -> dict:
    # 54 is read without its startup wrapper, whose shared load records would leak between cases.
    return {**service.handlers(), LOAD: service.read}


def pass_handlers(service: EventPassService) -> dict:
    return service.handlers()


MISSIONS = Family(MISSION_GOLDEN, MISSION_REGISTRATIONS, mission_service, mission_handlers)
PASSES = Family(PASS_GOLDEN, PASS_REGISTRATIONS, pass_service, pass_handlers)


def initial(bundle, clock: SimpleNamespace) -> dict:
    return bundle.initialize(frozen_bootstrap(bundle, clock).initial_state())


def finish_daily(bundle, service: MissionService, state: dict) -> dict:
    state, units = bundle.player_state.credit_units(state, [{"card_id": 7, "level": 1, "count": 1}])
    before, after = deepcopy(units[0]), deepcopy(units[0])
    after["Love"] = 1
    state["player_state"]["units"][-1] = after
    state = service.record(state, "gift", {"before": before, "after": after}, NOW)
    before, after = deepcopy(after), deepcopy(after)
    after["Exp"] = bundle.player_state.experience_for_level(7, 2)
    state["player_state"]["units"][-1] = after
    state = service.record(state, "training", {"before": before, "after": after, "level": 2}, NOW)
    for _ in range(QUEST_CLEARS):
        state = service.record(state, "quest_clear", {"quest_id": 1001, "quest_type": 0, "stars": 3, "hard": False},
                               NOW)
    return state


def mission_bases(bundle) -> dict:
    clock = SimpleNamespace(now=NOW)
    service = mission_service(bundle, clock)
    login = service.record(initial(bundle, clock), "login", {}, NOW)
    return {name: json.loads(serialize(state))
            for name, state in ((LOGIN_BASE, login), (BUSY_BASE, finish_daily(bundle, service, login)))}


def pass_bases(bundle) -> dict:
    empty = initial(bundle, SimpleNamespace(now=NOW))
    full = bundle.possessions.credit(empty, {PASS_ID: FULL_POINTS, PREMIUM_ID: 1})
    normal = bundle.possessions.credit(empty, {PASS_ID: NORMAL_POINTS})
    return {name: json.loads(serialize(state))
            for name, state in ((FULL_BASE, full), (NORMAL_BASE, normal), (EMPTY_BASE, empty))}


def mailbox(start: dict, final: dict) -> dict:
    # A full mailbox would repeat every stored row in each case, so unchanged rows are pinned by digest instead.
    stored = {row["PresentID"] for row in start["tables"]["presents"]}
    rows = final["tables"]["presents"]
    return {"count": len(rows), "added": [row for row in rows if row["PresentID"] not in stored],
            "sha256": sha256(canonical(rows).encode()).hexdigest()}


def changes(start: dict, final: dict) -> dict:
    keys = sorted((start.keys() | final.keys()) - {"tables"})
    tables = sorted((start["tables"].keys() | final["tables"].keys()) - {"presents"})
    return {"keys": {key: final[key] for key in keys if key in final and canonical(start.get(key)) != canonical(final[key])},
            "removed": [key for key in keys if key not in final],
            "tables": {name: final["tables"].get(name) for name in tables
                       if canonical(start["tables"].get(name)) != canonical(final["tables"].get(name))},
            "presents": mailbox(start, final)}


def outcome(bundle, family: Family, base: dict, case: Case) -> dict:
    start = deepcopy(base)
    if case.stored is not None:
        start = case.stored(bundle, start, case.steps)
    clock = SimpleNamespace(now=NOW)
    service, store, steps = family.build(bundle, clock), GoldenStore(start), []
    handlers = family.handlers(service)
    for step in case.steps:
        clock.now = step.now
        if step.prepare is not None:
            profile = store.profile()
            step.prepare(bundle, profile.state)
            store.update_state(profile.state, profile.revision)
        if step.rewrite is not None:
            changed = store.profile().state
            step.rewrite(bundle, changed)
            store.overwrite(changed)
        before, envelope = store.profile(), step.envelope()
        with logged_warnings() as warnings:
            try:
                result = {"response": handlers[step.registration](envelope, store).decode("utf-8")}
            except ProtocolError as error:
                result = {"error": {"code": error.code, "message": str(error), "status": error.status}}
        after = store.profile()
        steps.append({**result, "request": envelope.xml.decode("utf-8"), "revisions": after.revision - before.revision,
                      "warnings": list(warnings)})
    return {"steps": steps, "state": changes(start, store.profile().state)}


def scope(identifier: int = 0, page: tuple[int, int] = DAILY, group: int = 0, *, fields=SCOPE_FIELDS) -> tuple:
    values = dict(zip(SCOPE_FIELDS, (identifier, *page, group)))
    return (Container(SCOPE, tuple(Column(name, "I", (values[name],)) for name in fields)),)


def load(**options) -> Step:
    return Step(LOAD, **options)


def claim(identifier: int, page: tuple[int, int] = DAILY, group: int = 0, **options) -> Step:
    return Step(CLAIM, scope(identifier, page, group), **options)


def claim_page(page: tuple[int, int] = DAILY, group: int = 0, **options) -> Step:
    return Step(CLAIM_GROUP, scope(0, page, group), **options)


def mark_page(page: tuple[int, int] = DAILY, group: int = 0, **options) -> Step:
    return Step(MARK_READ, scope(0, page, group), **options)


def read_passes(**options) -> Step:
    return Step(PASS_READ, **options)


def claim_pass(*identifiers: str, **options) -> Step:
    return Step(PASS_CLAIM, (Column(PASS_FIELD, "S", identifiers),), **options)


def foreign(**options) -> dict:
    return {"user_key": FOREIGN_KEY, **options}


def touch(bundle, state: dict) -> None:
    state[SENTINEL] = state.get(SENTINEL, 0) + 1


def grant(item_id: str, count: int) -> Callable[[Any, dict], None]:
    def prepare(bundle, state: dict) -> None:
        state.update(bundle.possessions.credit(state, {item_id: count}))

    return prepare


def legacy(namespace: str, *dropped: str) -> Callable[[Any, dict], None]:
    def rewrite(bundle, state: dict) -> None:
        history = state[namespace]
        state[namespace] = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 1000,
                            "seen_keys": list(history["processed_keys"]),
                            "receipts": [{**{key: value for key, value in entry.items() if key not in dropped},
                                          "client_time": 1000} for entry in history["receipts"]]}

    return rewrite


def full_mailbox(bundle, state: dict, steps) -> dict:
    return bundle.presents.enqueue(state, [EXISTING_MAIL] * MAX_PRESENTS)[0]


def exhausted_identities(bundle, state: dict, steps) -> dict:
    state.update(presents_schema=1, last_present_id=INT32_MAX)
    return state


def corrupted(key: str, **values) -> Callable[[Any, dict, tuple], dict]:
    def store(bundle, state: dict, steps) -> dict:
        state[key] = {**state.get(key, {}), **values}
        return state

    return store


def holding(item_id: str, count: int) -> Callable[[Any, dict, tuple], dict]:
    def store(bundle, state: dict, steps) -> dict:
        return bundle.possessions.credit(state, {item_id: count})

    return store


def stacked(*stores: Callable[[Any, dict, tuple], dict]) -> Callable[[Any, dict, tuple], dict]:
    def store(bundle, state: dict, steps) -> dict:
        for each in stores:
            state = each(bundle, state, steps)
        return state

    return store


def processed(namespace: str) -> Callable[[Any, dict, tuple], dict]:
    # The first request is already processed while its receipt is gone, as after eviction.
    def store(bundle, state: dict, steps) -> dict:
        state[namespace] = {"schema_version": LEDGER_SCHEMA, "receipts": [], "processed_keys": [steps[0].key()]}
        return state

    return store


MISSION_CASES = {
    "load_login": Case((load(),), LOGIN_BASE),
    "load_busy": Case((load(),), BUSY_BASE),
    "load_wrong_user_key": Case((load(**foreign()),), LOGIN_BASE),
    "load_progress_invalid": Case((load(),), LOGIN_BASE, corrupted(MISSION_PROGRESS, schema_version=99)),
    "claim_login_with_points": Case((claim(LOGIN_MISSION),), LOGIN_BASE),
    "claim_then_load": Case((claim(LOGIN_MISSION), load(tick=1001)), LOGIN_BASE),
    "claim_gold_without_points": Case((claim(DAILY_GOLD),), BUSY_BASE),
    "claim_monthly_unit": Case((claim(MONTHLY_UNIT, MONTHLY),), BUSY_BASE),
    "claim_daily_page": Case((claim_page(),), BUSY_BASE),
    "claim_daily_page_then_load": Case((claim_page(), load(tick=1001)), BUSY_BASE),
    "claim_monthly_page_keeps_unsupported": Case((claim_page(MONTHLY), load(tick=1001)), BUSY_BASE),
    "claim_unsupported_only": Case((claim(UNSUPPORTED_MISSION, MONTHLY),), BUSY_BASE),
    "claim_attribute_page": Case((claim_page(ATTRIBUTE),), BUSY_BASE),
    "claim_attribute_group": Case((claim_page((ATTRIBUTE_GROUPS, 0), 1),), BUSY_BASE),
    "claim_limited_page": Case((claim_page(LIMITED),), BUSY_BASE),
    "claim_sequence": Case((claim(FIRST_SEQUENCE, (SEQUENCE, 0)),), BUSY_BASE),
    "claim_unfinished": Case((claim(UNFINISHED_DAILY),), LOGIN_BASE),
    "claim_claimed_again": Case((claim(LOGIN_MISSION), claim(LOGIN_MISSION, tick=1001)), LOGIN_BASE),
    "claim_page_after_single": Case((claim(LOGIN_MISSION), claim_page(tick=1001)), BUSY_BASE),
    "claim_unknown_mission": Case((claim(UNKNOWN_MISSION),), LOGIN_BASE),
    "claim_other_page": Case((claim(LOGIN_MISSION, MONTHLY),), LOGIN_BASE),
    "claim_unsupported_page": Case((claim_page((9, 9)),), LOGIN_BASE),
    "claim_single_with_group": Case((claim(LOGIN_MISSION, DAILY, 1),), LOGIN_BASE),
    "claim_negative_scope": Case((claim(-1),), LOGIN_BASE),
    "claim_page_with_mission": Case((Step(CLAIM_GROUP, scope(LOGIN_MISSION)),), LOGIN_BASE),
    "claim_missing_scope_field": Case((Step(CLAIM, scope(LOGIN_MISSION, fields=SCOPE_FIELDS[:3])),), LOGIN_BASE),
    "claim_without_scope": Case((Step(CLAIM),), LOGIN_BASE),
    "claim_wrong_user_key": Case((claim(LOGIN_MISSION, **foreign()),), LOGIN_BASE),
    "claim_wrong_method_key": Case((claim(LOGIN_MISSION, method_key=FOREIGN_KEY),), LOGIN_BASE),
    "priority_fields_before_identity": Case((Step(CLAIM, **foreign()),), LOGIN_BASE),
    "priority_identity_before_scope": Case((claim(-1, **foreign()),), LOGIN_BASE),
    "priority_scope_before_history": Case((claim(UNKNOWN_MISSION),), LOGIN_BASE,
                                          corrupted(MISSION_HISTORY, schema_version=99)),
    "priority_history_before_delivery": Case((claim(LOGIN_MISSION),), LOGIN_BASE,
                                             stacked(corrupted(MISSION_HISTORY, schema_version=99),
                                                     corrupted(MISSION_DELIVERY, schema_version=2))),
    "claim_mailbox_full": Case((claim(LOGIN_MISSION),), LOGIN_BASE, full_mailbox),
    "claim_page_mailbox_full": Case((claim_page(),), BUSY_BASE, full_mailbox),
    "claim_identity_exhausted": Case((claim(LOGIN_MISSION),), LOGIN_BASE, exhausted_identities),
    "claim_history_invalid": Case((claim(LOGIN_MISSION),), LOGIN_BASE, corrupted(MISSION_HISTORY, schema_version=99)),
    "claim_delivery_invalid": Case((claim(LOGIN_MISSION),), LOGIN_BASE,
                                   corrupted(MISSION_DELIVERY, schema_version=2)),
    "claim_progress_invalid": Case((claim(LOGIN_MISSION),), LOGIN_BASE, corrupted(MISSION_PROGRESS, schema_version=99)),
    "claim_points_capped": Case((claim(LOGIN_MISSION),), LOGIN_BASE, holding(PASS_ID, EP_STACK - 1)),
    "claim_points_full": Case((claim(LOGIN_MISSION),), LOGIN_BASE, holding(PASS_ID, EP_STACK)),
    "claim_replay": Case((claim(LOGIN_MISSION), claim(LOGIN_MISSION)), LOGIN_BASE),
    "claim_page_replay": Case((claim_page(), claim_page()), BUSY_BASE),
    "claim_replay_after_load": Case((claim_page(), load(tick=1001), claim_page()), BUSY_BASE),
    "claim_replay_after_change": Case((claim(LOGIN_MISSION), claim(LOGIN_MISSION, prepare=touch)), LOGIN_BASE),
    "claim_replay_next_day": Case((claim(LOGIN_MISSION), claim(LOGIN_MISSION, now=NEXT_DAY)), LOGIN_BASE),
    "claim_processed_without_receipt": Case((claim(LOGIN_MISSION),), LOGIN_BASE, processed(MISSION_HISTORY)),
    "claim_legacy_receipt_replay": Case((claim(DAILY_GOLD), claim(DAILY_GOLD, rewrite=legacy(MISSION_HISTORY,
                                                                                          "event_pass_points"))),
                                        BUSY_BASE),
    "mark_daily_page": Case((mark_page(),), BUSY_BASE),
    "mark_daily_page_twice": Case((mark_page(), mark_page(tick=1001)), BUSY_BASE),
    "mark_replay": Case((mark_page(), mark_page()), BUSY_BASE),
    "mark_monthly_page": Case((mark_page(MONTHLY),), BUSY_BASE),
    "mark_attribute_group": Case((mark_page((ATTRIBUTE_GROUPS, 0), 1),), BUSY_BASE),
    "claim_after_mark": Case((mark_page(), claim_page(tick=1001)), BUSY_BASE),
}

PASS_CASES = {
    "read_full": Case((read_passes(),), FULL_BASE),
    "read_wrong_user_key": Case((read_passes(**foreign()),), FULL_BASE),
    "claim_both_tracks": Case((claim_pass(PASS_ID),), FULL_BASE),
    "claim_then_read": Case((claim_pass(PASS_ID), read_passes(tick=1001)), FULL_BASE),
    "claim_normal_track": Case((claim_pass(PASS_ID),), NORMAL_BASE),
    "claim_premium_later": Case((claim_pass(PASS_ID), claim_pass(PASS_ID, tick=1001, prepare=grant(PREMIUM_ID, 1))),
                                NORMAL_BASE),
    "claim_more_points_later": Case((claim_pass(PASS_ID), claim_pass(PASS_ID, tick=1001,
                                                                     prepare=grant(PASS_ID, NORMAL_POINTS))),
                                    NORMAL_BASE),
    "claim_nothing_reached": Case((claim_pass(PASS_ID),), EMPTY_BASE),
    "claim_twice": Case((claim_pass(PASS_ID), claim_pass(PASS_ID, tick=1001)), NORMAL_BASE),
    "claim_closed_pass": Case((claim_pass(CLOSED_PASS),), FULL_BASE),
    "claim_unknown_pass": Case((claim_pass("event_pass_missing"),), FULL_BASE),
    "claim_premium_identity": Case((claim_pass(PREMIUM_ID),), FULL_BASE),
    "claim_two_passes": Case((claim_pass(PASS_ID, CLOSED_PASS),), FULL_BASE),
    "claim_empty_identity": Case((claim_pass(""),), FULL_BASE),
    "claim_oversized_identity": Case((claim_pass("p" * 300),), FULL_BASE),
    "claim_without_identity": Case((Step(PASS_CLAIM),), FULL_BASE),
    "claim_wrong_user_key": Case((claim_pass(PASS_ID, **foreign()),), FULL_BASE),
    "claim_wrong_method_key": Case((claim_pass(PASS_ID, method_key=FOREIGN_KEY),), FULL_BASE),
    "priority_unknown_before_identity": Case((claim_pass("event_pass_missing", **foreign()),), FULL_BASE),
    "priority_identity_before_history": Case((claim_pass(PASS_ID, **foreign()),), FULL_BASE,
                                             corrupted(PASS_HISTORY, schema_version=99)),
    "priority_history_before_window": Case((claim_pass(CLOSED_PASS),), FULL_BASE,
                                           corrupted(PASS_HISTORY, schema_version=99)),
    "claim_mailbox_full": Case((claim_pass(PASS_ID),), FULL_BASE, full_mailbox),
    "claim_identity_exhausted": Case((claim_pass(PASS_ID),), FULL_BASE, exhausted_identities),
    "claim_history_invalid": Case((claim_pass(PASS_ID),), FULL_BASE, corrupted(PASS_HISTORY, schema_version=99)),
    "claim_delivery_without_receipt": Case((claim_pass(PASS_ID),), FULL_BASE,
                                           corrupted(PASS_DELIVERY, schema_version=1)),
    "claim_journal_invalid": Case((claim_pass(PASS_ID),), FULL_BASE, corrupted(PASS_JOURNAL, schema_version=99)),
    "claim_replay": Case((claim_pass(PASS_ID), claim_pass(PASS_ID)), NORMAL_BASE),
    "claim_replay_after_read": Case((claim_pass(PASS_ID), read_passes(tick=1001), claim_pass(PASS_ID)), NORMAL_BASE),
    "claim_replay_after_change": Case((claim_pass(PASS_ID), claim_pass(PASS_ID, prepare=touch)), NORMAL_BASE),
    "claim_processed_without_receipt": Case((claim_pass(PASS_ID),), NORMAL_BASE, processed(PASS_HISTORY)),
    "claim_legacy_receipt_replay": Case((claim_pass(PASS_ID), claim_pass(PASS_ID, rewrite=legacy(PASS_HISTORY))),
                                        NORMAL_BASE),
}

FAMILIES = ((MISSIONS, MISSION_CASES, mission_bases), (PASSES, PASS_CASES, pass_bases))


@cache
def recorded(path: Path) -> dict:
    return json.loads(path.read_bytes())


def live_base(family: Family, name: str) -> dict:
    return deepcopy(recorded(family.golden)["bases"][name])


@pytest.mark.parametrize("family, cases", [(family, cases) for family, cases, _ in FAMILIES],
                         ids=("missions", "event_passes"))
def test_goldens_cover_every_case_with_successes_and_refusals(family, cases):
    golden = recorded(family.golden)
    assert golden["registrations"] == list(family.registrations) and golden["cases"].keys() == cases.keys()
    steps = [step for case in golden["cases"].values() for step in case["steps"]]
    assert {step["error"]["status"] for step in steps if "error" in step} >= {400, 409, 501, 503}
    assert any(step["revisions"] == 0 and "response" in step for step in steps)
    assert any(step["revisions"] == 1 for step in steps)
    assert any(step["warnings"] for step in steps if "response" in step)


@pytest.mark.parametrize("name", MISSION_CASES)
def test_mission_requests_match_the_recorded_golden_bytes(golden_revived, name):
    case = MISSION_CASES[name]
    result = outcome(golden_revived, MISSIONS, live_base(MISSIONS, case.base), case)
    assert canonical(result) == canonical(recorded(MISSION_GOLDEN)["cases"][name])


@pytest.mark.parametrize("name", PASS_CASES)
def test_pass_requests_match_the_recorded_golden_bytes(golden_revived, name):
    case = PASS_CASES[name]
    result = outcome(golden_revived, PASSES, live_base(PASSES, case.base), case)
    assert canonical(result) == canonical(recorded(PASS_GOLDEN)["cases"][name])


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


def registry() -> CommandRegistry:
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_missions.register(commands)
    return commands


def context(services, now: datetime = NOW) -> CommandContext:
    return CommandContext(now=now, acquisition_day=now.date().toordinal() - 1, revision=0,
                          generation_id=CONSOLE_GENERATION, services=services)


def run(services, command, state: dict, raw: dict):
    outcome = Batch((BatchStep(command, command.parse(raw)),)).apply(state, context(services))
    return outcome.state, outcome.steps[0]


def refused(services, command, state: dict, raw: dict) -> AdminError:
    with pytest.raises(AdminError) as caught:
        run(services, command, state, raw)
    return caught.value


def console(services, state: dict, preflight):
    store = FakeStore(state)
    generation = FakeGeneration(id=CONSOLE_GENERATION, bundle=services, clock=NOW)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def preview(pipeline, command, params: dict):
    return pipeline.preview(registry().batch([{"operation": command.operation, "params": params}]))


def commit(pipeline, plan):
    return pipeline.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest,
                           expected_revision=plan.expected_revision, op_id=str(uuid.uuid4()))


def payload(error: ProtocolError) -> dict:
    return {"code": error.code, "message": str(error), "status": error.status}


def native_error(family: Family, name: str) -> dict:
    return recorded(family.golden)["cases"][name]["steps"][0]["error"]


def native_receipt(family: Family, name: str, namespace: str) -> dict:
    return recorded(family.golden)["cases"][name]["state"]["keys"][namespace]["receipts"][-1]


def native_state(family: Family, name: str, *receipts: str) -> dict:
    # The console commits what the native request commits, without its receipt and delivery marker.
    slices = recorded(family.golden)["cases"][name]["state"]
    return {**slices, "keys": {key: value for key, value in slices["keys"].items() if key not in receipts}}


def stored_base(bundle, family: Family, cases: dict, name: str) -> dict:
    case = cases[name]
    base = live_base(family, case.base)
    return base if case.stored is None else case.stored(bundle, base, case.steps)


def held(state: dict, item_id: str) -> int:
    return next((row["Count"] for row in state["tables"]["possessions"] if row["ItemID"] == item_id), 0)


def test_commands_register_through_discovery():
    found = discover(PREFLIGHT_DOMAINS)
    assert "admin_cmd_missions" in found.modules
    assert set(OPERATIONS) <= {command.operation for command in found.commands}
    assert [command.operation for command in registry()] == list(OPERATIONS)


def test_commands_touch_mapped_domains_and_take_no_confirmation():
    assert {command.operation: sorted(command.touched) for command in COMMANDS} == {
        "missions.claim": ["missions", "possessions", "presents"], "event_pass.grant_points": ["possessions"],
        "event_pass.claim": ["event_pass", "presents"]}
    for command in COMMANDS:
        assert command.domain == "progress" and command.touched <= set(PREFLIGHT_DOMAINS), command.operation
        assert command.destructive is False and not command.preflight_none and not command.writes, command.operation


@pytest.mark.parametrize("command, params", [
    (CLAIM_MISSIONS, {}), (CLAIM_MISSIONS, {"ids": []}), (CLAIM_MISSIONS, {"ids": [0]}),
    (CLAIM_MISSIONS, {"ids": [-1]}), (CLAIM_MISSIONS, {"ids": [True]}), (CLAIM_MISSIONS, {"ids": [str(LOGIN_MISSION)]}),
    (CLAIM_MISSIONS, {"ids": [float(LOGIN_MISSION)]}), (CLAIM_MISSIONS, {"ids": [INT32_MAX + 1]}),
    (CLAIM_MISSIONS, {"ids": [LOGIN_MISSION, LOGIN_MISSION]}),
    (CLAIM_MISSIONS, {"ids": list(range(1, MAX_MISSIONS + 2))}),
    (CLAIM_MISSIONS, {"ids": [LOGIN_MISSION], "PT": DAILY[0]}),
    (GRANT_POINTS, {"pass_id": PASS_ID}), (GRANT_POINTS, {"amount": 1}), (GRANT_POINTS, {"pass_id": "", "amount": 1}),
    (GRANT_POINTS, {"pass_id": "p" * (STORED_ITEM_NAME_BYTES + 1), "amount": 1}),
    (GRANT_POINTS, {"pass_id": 1, "amount": 1}), (GRANT_POINTS, {"pass_id": PASS_ID, "amount": 0}),
    (GRANT_POINTS, {"pass_id": PASS_ID, "amount": -1}), (GRANT_POINTS, {"pass_id": PASS_ID, "amount": True}),
    (GRANT_POINTS, {"pass_id": PASS_ID, "amount": "5"}), (GRANT_POINTS, {"pass_id": PASS_ID, "amount": 1.0}),
    (GRANT_POINTS, {"pass_id": PASS_ID, "amount": INT32_MAX + 1}),
    (GRANT_POINTS, {"pass_id": PASS_ID, "amount": 1, "premium": True}),
    (CLAIM_PASS, {}), (CLAIM_PASS, {"pass_id": ""}), (CLAIM_PASS, {"pass_id": [PASS_ID]}),
    (CLAIM_PASS, {"pass_id": PASS_ID, "amount": 1}),
])
def test_parameters_are_strict(command, params):
    with pytest.raises(AdminError) as caught:
        command.parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_commands_name_the_service_they_are_missing(revived):
    empty = SimpleNamespace()
    assert CLAIM_MISSIONS.availability(empty) == missing_service("missions")
    assert GRANT_POINTS.availability(empty) == CLAIM_PASS.availability(empty) == missing_service("event_passes")
    assert [command.operation for command in COMMANDS if command.availability(revived) is not None] == []
    state = live_base(PASSES, EMPTY_BASE)
    for command, raw in ((CLAIM_MISSIONS, {"ids": [LOGIN_MISSION]}), (GRANT_POINTS, {"pass_id": PASS_ID, "amount": 1}),
                         (CLAIM_PASS, {"pass_id": PASS_ID})):
        error = refused(empty, command, state, raw)
        assert (error.code, error.status) == (COMMAND_UNAVAILABLE, 503), command.operation


@pytest.mark.parametrize("ids", [(UNKNOWN_MISSION,), (0,), (-1,), (LOGIN_MISSION, LOGIN_MISSION), (INT32_MAX + 1,),
                                 (True,)])
def test_selections_refuse_missions_the_catalog_never_lists(revived, ids):
    state = live_base(MISSIONS, LOGIN_BASE)
    with pytest.raises(ProtocolError) as caught:
        revived.missions.select_missions(state, ids, NOW)
    assert (caught.value.code, caught.value.status) == ("mission_selection_invalid", 400)
    assert state == live_base(MISSIONS, LOGIN_BASE)


def test_selections_read_the_54_snapshot_without_saving(revived):
    state = live_base(MISSIONS, BUSY_BASE)
    requested = (LOGIN_MISSION, FIRST_SEQUENCE, UNSUPPORTED_MISSION)
    selection = revived.missions.select_missions(state, requested, NOW)
    assert state == live_base(MISSIONS, BUSY_BASE)
    assert selection.requested == requested and selection.shown == requested
    assert (selection.candidates, selection.skipped) == ((LOGIN_MISSION, UNSUPPORTED_MISSION), (FIRST_SEQUENCE,))
    assert {identifier: row["Status"] for identifier, row in selection.rows.items()} == {
        LOGIN_MISSION: 1, FIRST_SEQUENCE: 0, UNSUPPORTED_MISSION: 1}


def test_granting_writes_neither_receipts_nor_the_delivery_marker(revived):
    missions, state = revived.missions, live_base(MISSIONS, LOGIN_BASE)
    claim = missions.grant_missions(missions.select_missions(state, (LOGIN_MISSION,), NOW), NOW)
    native = native_receipt(MISSIONS, "claim_login_with_points", MISSION_HISTORY)
    assert (list(claim.claimed), list(claim.mail_ids), list(claim.point_audit)) == (
        native["ids"], native["mail_ids"], native["event_pass_points"])
    assert claim.unsupported == () and MISSION_HISTORY not in claim.state and MISSION_DELIVERY not in claim.state


@pytest.mark.parametrize("name", ["claim_login_with_points", "claim_gold_without_points", "claim_monthly_unit",
                                  "claim_daily_page", "claim_monthly_page_keeps_unsupported", "claim_attribute_page",
                                  "claim_limited_page", "claim_points_capped", "claim_points_full"])
def test_mission_claims_commit_what_58_and_59_commit_without_receipts(revived, name):
    start = stored_base(revived, MISSIONS, MISSION_CASES, name)
    ids = native_receipt(MISSIONS, name, MISSION_HISTORY)["ids"]
    claimed, step = run(revived, CLAIM_MISSIONS, start, {"ids": ids})
    assert canonical(changes(start, claimed)) == canonical(native_state(MISSIONS, name, MISSION_HISTORY, MISSION_DELIVERY))
    assert [reward["ID"] for reward in step.effects["claimed"]] == ids


def test_daily_page_order_is_the_order_59_claims_in():
    assert list(DAILY_PAGE) == native_receipt(MISSIONS, "claim_daily_page", MISSION_HISTORY)["ids"]


def test_claimed_missions_read_back_through_54_as_the_native_claim_leaves_them(revived):
    claimed, _ = run(revived, CLAIM_MISSIONS, live_base(MISSIONS, LOGIN_BASE), {"ids": [LOGIN_MISSION]})
    reread = outcome(revived, MISSIONS, claimed, Case((load(tick=1001),), LOGIN_BASE))
    native = recorded(MISSION_GOLDEN)["cases"]["claim_then_load"]["steps"][1]
    assert reread["steps"][0]["response"] == native["response"]
    again, step = run(revived, CLAIM_MISSIONS, claimed, {"ids": [LOGIN_MISSION]})
    assert again == claimed and step.warnings == (NOTHING_CLAIMABLE,)
    assert step.effects["skipped"] == [{"ID": LOGIN_MISSION, "Status": 2, "Read": 0, "Progress": 1}]


def test_claims_skip_what_the_snapshot_cannot_claim_and_keep_unsupported_rewards(revived):
    start = live_base(MISSIONS, BUSY_BASE)
    monthly = native_receipt(MISSIONS, "claim_monthly_page_keeps_unsupported", MISSION_HISTORY)["ids"]
    claimed, step = run(revived, CLAIM_MISSIONS, start, {"ids": [*monthly, UNSUPPORTED_MISSION, FIRST_SEQUENCE]})
    native = native_state(MISSIONS, "claim_monthly_page_keeps_unsupported", MISSION_HISTORY, MISSION_DELIVERY)
    assert canonical(changes(start, claimed)) == canonical(native)
    assert step.effects["unsupported"] == [UNSUPPORTED_MISSION] and step.effects["unavailable"] == []
    assert [row["ID"] for row in step.effects["skipped"]] == [FIRST_SEQUENCE]
    assert step.warnings == (MISSIONS_SKIPPED, REWARDS_UNSUPPORTED)


def test_claims_with_nothing_finished_save_nothing(revived):
    start = live_base(MISSIONS, LOGIN_BASE)
    unchanged, step = run(revived, CLAIM_MISSIONS, start, {"ids": [UNFINISHED_DAILY]})
    assert unchanged == start and step.warnings == (NOTHING_CLAIMABLE,)
    assert step.effects["claimed"] == [] and step.effects["mail_ids"] == []
    assert step.effects["mailbox"] == {"before": 0, "after": 0, "capacity": MAX_PRESENTS}


def test_claims_report_discarded_ep_as_the_native_claim_records_it(revived):
    start = stored_base(revived, MISSIONS, MISSION_CASES, "claim_points_capped")
    _, step = run(revived, CLAIM_MISSIONS, start, {"ids": [LOGIN_MISSION]})
    native = native_receipt(MISSIONS, "claim_points_capped", MISSION_HISTORY)["event_pass_points"]
    assert step.effects["event_pass_points"] == native and step.warnings == (POINTS_DISCARDED,)


@pytest.mark.parametrize("name, ids", [("claim_unsupported_only", (UNSUPPORTED_MISSION,)),
                                       ("claim_mailbox_full", (LOGIN_MISSION,)), ("claim_page_mailbox_full", DAILY_PAGE),
                                       ("claim_identity_exhausted", (LOGIN_MISSION,)),
                                       ("claim_progress_invalid", (LOGIN_MISSION,))])
def test_mission_claim_refusals_keep_the_native_codes(revived, name, ids):
    start = stored_base(revived, MISSIONS, MISSION_CASES, name)
    error = refused(revived, CLAIM_MISSIONS, start, {"ids": list(ids)})
    assert payload(error) == native_error(MISSIONS, name)


def test_native_claims_keep_their_receipts_after_console_claims(revived):
    service, store = mission_service(revived, SimpleNamespace(now=NOW)), GoldenStore(live_base(MISSIONS, BUSY_BASE))
    first = claim(LOGIN_MISSION)
    service.handlers()[CLAIM](first.envelope(), store)
    native = store.profile()
    claimed, step = run(revived, CLAIM_MISSIONS, native.state, {"ids": [POINT_MISSION]})
    assert claimed[MISSION_HISTORY] == native.state[MISSION_HISTORY]
    assert claimed[MISSION_DELIVERY] == native.state[MISSION_DELIVERY]
    assert [(entry["before"], entry["after"]) for entry in step.effects["event_pass_points"]] == [(2, 6)]
    store.update_state(claimed, native.revision)
    with pytest.raises(ProtocolError) as caught:
        service.handlers()[CLAIM](first.envelope(), store)
    assert (caught.value.code, caught.value.status) == ("mission_retry_stale", 409)
    service.handlers()[CLAIM](claim(NEXT_POINT_MISSION, tick=1001).envelope(), store)
    marker = store.profile().state[MISSION_DELIVERY]["audit"]
    assert [(entry["before"], entry["after"]) for entry in marker] == [(6, 10)]


def test_mission_claims_preview_with_readback_and_commit_without_a_receipt(revived, preflight):
    store, pipeline = console(revived, live_base(MISSIONS, BUSY_BASE), preflight)
    plan = preview(pipeline, CLAIM_MISSIONS, {"ids": list(DAILY_PAGE)})
    assert plan.committable and plan.restart_check == {"ok": True} and plan.confirmations == ()
    assert plan.preflight["readback"] == MISSION_READBACK
    committed = commit(pipeline, plan)
    assert store.revision == 1 and committed.result["backup_before_commit"] is None
    assert MISSION_HISTORY not in store.state and MISSION_DELIVERY not in store.state
    record = store.calls[-1]["record"]
    assert record["targets"] == sorted(MISSION_READBACK) and record["result"]["readback"] == MISSION_READBACK


@pytest.mark.parametrize("name", ["claim_both_tracks", "claim_normal_track"])
def test_pass_claims_commit_what_159_commits_without_receipts(revived, name):
    start = stored_base(revived, PASSES, PASS_CASES, name)
    claimed, step = run(revived, CLAIM_PASS, start, {"pass_id": PASS_ID})
    assert canonical(changes(start, claimed)) == canonical(native_state(PASSES, name, PASS_HISTORY, PASS_DELIVERY))
    assert step.effects["mail_ids"] == native_receipt(PASSES, name, PASS_HISTORY)["mail_ids"]
    delivered = [mail for item in step.effects["delivered"] for mail in item["mail_ids"]]
    assert delivered == step.effects["mail_ids"]


def test_claimed_passes_read_back_through_158_as_159_leaves_them(revived):
    claimed, step = run(revived, CLAIM_PASS, live_base(PASSES, FULL_BASE), {"pass_id": PASS_ID})
    reread = outcome(revived, PASSES, claimed, Case((read_passes(tick=1001),), FULL_BASE))
    assert reread["steps"][0]["response"] == recorded(PASS_GOLDEN)["cases"]["claim_then_read"]["steps"][1]["response"]
    top = max(reward.required_point for reward in revived.event_passes.catalog.get(PASS_ID).rewards)
    assert step.effects["received"] == {"before": {"normal": 0, "premium": 0}, "after": {"normal": top, "premium": top}}


def test_pass_claims_with_nothing_left_save_nothing(revived):
    empty = live_base(PASSES, EMPTY_BASE)
    unchanged, step = run(revived, CLAIM_PASS, empty, {"pass_id": PASS_ID})
    assert unchanged == empty and step.warnings == (NOTHING_REACHED,) and step.effects["delivered"] == []
    claimed, _ = run(revived, CLAIM_PASS, live_base(PASSES, NORMAL_BASE), {"pass_id": PASS_ID})
    again, step = run(revived, CLAIM_PASS, claimed, {"pass_id": PASS_ID})
    assert again == claimed and step.warnings == (NOTHING_REACHED,)
    watermarks = {"normal": NORMAL_POINTS, "premium": 0}
    assert step.effects["received"] == {"before": watermarks, "after": watermarks}


@pytest.mark.parametrize("name, pass_id", [("claim_closed_pass", CLOSED_PASS), ("claim_unknown_pass", "event_pass_missing"),
                                           ("claim_premium_identity", PREMIUM_ID), ("claim_mailbox_full", PASS_ID),
                                           ("claim_identity_exhausted", PASS_ID), ("claim_journal_invalid", PASS_ID)])
def test_pass_claim_refusals_keep_the_native_codes(revived, name, pass_id):
    error = refused(revived, CLAIM_PASS, stored_base(revived, PASSES, PASS_CASES, name), {"pass_id": pass_id})
    assert payload(error) == native_error(PASSES, name)


def test_point_grants_credit_the_pass_item(revived):
    normal = live_base(PASSES, NORMAL_BASE)
    granted, step = run(revived, GRANT_POINTS, normal, {"pass_id": PASS_ID, "amount": NORMAL_POINTS})
    assert dict(step.effects) == {"pass_id": PASS_ID, "requested": NORMAL_POINTS, "credited": NORMAL_POINTS,
                                  "before": NORMAL_POINTS, "after": 2 * NORMAL_POINTS, "capped": 0, "inactive": 0,
                                  "stack": EP_STACK}
    assert step.warnings == () and held(granted, PASS_ID) == 2 * NORMAL_POINTS


@pytest.mark.parametrize("count, pass_id", [(0, PASS_ID), (EP_STACK - 1, PASS_ID), (EP_STACK, PASS_ID), (0, CLOSED_PASS)])
def test_point_grants_follow_the_mission_ep_rule(revived, count, pass_id):
    state = live_base(PASSES, EMPTY_BASE)
    if count:
        state = revived.possessions.credit(state, {pass_id: count})
    definition = replace(revived.missions.catalog.by_id[LOGIN_MISSION], event_pass_item_id=pass_id)
    awarded, audit = revived.missions.pass_points.award(state, [definition], NOW)
    granted, grant = revived.event_passes.grant_points(state, pass_id, definition.event_pass_point, NOW)
    assert audit == [grant] and granted == awarded


def test_point_grants_discard_what_mission_ep_discards(revived):
    full = live_base(PASSES, FULL_BASE)
    capped, step = run(revived, GRANT_POINTS, full, {"pass_id": PASS_ID, "amount": EP_STACK})
    assert (step.effects["credited"], step.effects["capped"]) == (EP_STACK - FULL_POINTS, FULL_POINTS)
    assert step.warnings == (POINTS_CAPPED,) and held(capped, PASS_ID) == EP_STACK
    closed, step = run(revived, GRANT_POINTS, full, {"pass_id": CLOSED_PASS, "amount": NORMAL_POINTS})
    assert closed == full and (step.effects["inactive"], step.warnings) == (NORMAL_POINTS, (PASS_INACTIVE,))


@pytest.mark.parametrize("pass_id", ["event_pass_missing", PREMIUM_ID])
def test_point_grants_take_only_ordinary_passes(revived, pass_id):
    error = refused(revived, GRANT_POINTS, live_base(PASSES, EMPTY_BASE), {"pass_id": pass_id, "amount": 1})
    assert payload(error) == native_error(PASSES, "claim_unknown_pass")


def test_point_grants_keep_received_rewards_and_open_only_the_next_thresholds(revived):
    claimed, _ = run(revived, CLAIM_PASS, live_base(PASSES, NORMAL_BASE), {"pass_id": PASS_ID})
    granted, _ = run(revived, GRANT_POINTS, claimed, {"pass_id": PASS_ID, "amount": NORMAL_POINTS})
    assert granted[PASS_JOURNAL] == claimed[PASS_JOURNAL]
    assert granted["tables"]["event_pass_status"] == claimed["tables"]["event_pass_status"]
    _, step = run(revived, CLAIM_PASS, granted, {"pass_id": PASS_ID})
    assert [(item["track"], item["point"]) for item in step.effects["delivered"]] == [
        ("normal", 40), ("normal", 50), ("normal", 60)]


def test_native_pass_claims_keep_working_around_console_changes(revived):
    service = pass_service(revived, SimpleNamespace(now=NOW))
    claimed, _ = run(revived, CLAIM_PASS, live_base(PASSES, NORMAL_BASE), {"pass_id": PASS_ID})
    granted, _ = run(revived, GRANT_POINTS, claimed, {"pass_id": PASS_ID, "amount": NORMAL_POINTS})
    assert PASS_HISTORY not in granted and PASS_DELIVERY not in granted
    store, first = GoldenStore(granted), claim_pass(PASS_ID)
    response = service.claim(first.envelope(), store)
    assert service.claim(first.envelope(), store) == response
    native = store.profile()
    regranted, _ = run(revived, GRANT_POINTS, native.state, {"pass_id": PASS_ID, "amount": NORMAL_POINTS})
    reclaimed, step = run(revived, CLAIM_PASS, regranted, {"pass_id": PASS_ID})
    assert reclaimed[PASS_HISTORY] == native.state[PASS_HISTORY] and reclaimed[PASS_DELIVERY] == native.state[PASS_DELIVERY]
    assert [item["point"] for item in step.effects["delivered"]] == [70, 80, 90]
    store.update_state(reclaimed, native.revision)
    with pytest.raises(ProtocolError) as caught:
        service.claim(first.envelope(), store)
    assert (caught.value.code, caught.value.status) == ("event_pass_retry_stale", 409)
    before = store.profile()
    service.claim(claim_pass(PASS_ID, tick=1001).envelope(), store)
    assert store.profile().revision == before.revision


def test_pass_commands_preview_with_readback_and_commit_without_receipts(revived, preflight):
    store, pipeline = console(revived, live_base(PASSES, NORMAL_BASE), preflight)
    plan = preview(pipeline, GRANT_POINTS, {"pass_id": PASS_ID, "amount": NORMAL_POINTS})
    assert plan.committable and plan.restart_check == {"ok": True} and plan.preflight["readback"] == POINT_READBACK
    commit(pipeline, plan)
    plan = preview(pipeline, CLAIM_PASS, {"pass_id": PASS_ID})
    assert plan.committable and plan.restart_check == {"ok": True} and plan.preflight["readback"] == PASS_READBACK
    commit(pipeline, plan)
    assert store.revision == 2 and held(store.state, PASS_ID) == 2 * NORMAL_POINTS
    assert PASS_HISTORY not in store.state and PASS_DELIVERY not in store.state
    assert [call["record"]["targets"] for call in store.calls] == [sorted(POINT_READBACK), sorted(PASS_READBACK)]


def test_skin_acquisitions_without_a_skin_catalog_compare_validated_counts(revived):
    source = revived.missions
    missions = MissionService(source.bootstrap, revived.player_state, revived.presents, source.catalog)
    state = live_base(MISSIONS, LOGIN_BASE)
    skin = sorted(revived.presents.item_catalog.skin_item_ids)[0]
    with pytest.raises(ProtocolError) as caught:
        missions.record_acquisitions(state, revived.possessions.credit(state, {skin: 1}), NOW)
    assert (caught.value.code, caught.value.status) == ("mission_skin_catalog_unavailable", 503)
    stocked = revived.possessions.credit(state, {PASSIVE_ITEM: 1})
    assert missions.record_acquisitions(state, stocked, NOW) is stocked
    damaged = deepcopy(stocked)
    damaged["tables"]["possessions"].append({"ItemID": skin})
    with pytest.raises(ProtocolError) as caught:
        missions.record_acquisitions(state, damaged, NOW)
    assert (caught.value.code, caught.value.status) == ("possession_state_invalid", 503)
    detached = copy(missions)
    detached.presents = copy(revived.presents)
    detached.presents.possessions = None
    with pytest.raises(ProtocolError) as caught:
        detached.record_acquisitions(state, stocked, NOW)
    assert caught.value.code == "mission_skin_catalog_unavailable"

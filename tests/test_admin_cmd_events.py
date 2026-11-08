import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import cache
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from aigis_ps import admin_cmd_events
from aigis_ps.admin_cmd_events import (CLAIM_BOARD, COMMANDS, COOKING_PHRASE, COOKING_UNCHANGED, LOGIN_COOKING,
                                       MIGRATE_COOKING, NOTHING_CLAIMABLE, REWARDS_SKIPPED, REWARDS_UNSUPPORTED)
from aigis_ps.admin_command import AdminError, CommandContext, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.alxml import Column, Container
from aigis_ps.crystal_spending import PROJECTION_CAP, settle_spending
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.login_cooking_state import (CRYSTALS_PER_COOKING, EXPLICIT_LEGACY, MAX_COOKING, advance_cooking,
                                          initialize_cooking, migrate_cooking, read_cooking, sync_cooking)
from aigis_ps.login_reward_service import COOKING_MIGRATION, COOKING_MIGRATION_PHRASE
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.request_ledger import HISTORY_LIMIT, LEGACY_SCHEMA_VERSION
from aigis_ps.storage import AUTO_BACKUP_KIND, PlayerProfile, serialize
from aigis_ps.sugoroku_service import (CAPACITY_REWARD, CLAIMED, HISTORY_NAMESPACE as BOARD_HISTORY, MAX_REWARDS,
                                       SugorokuService)
from aigis_ps.sugoroku_state import snapshot_sugoroku
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_seams import METHOD_KEY, PLAYER_ID, USER_KEY, MemoryStore, canonical
from test_login_cooking_state import (BORDER, NOW as COOKING_NOW, base as cooking_base, fail, fields as cooking_fields,
                                      seeded, unrelated)
from test_quest_service import bundle, golden_bundle, request
from test_sugoroku_service import statuses


GOLDEN = Path(__file__).resolve().parent / "golden" / "p25a"
BOARD_GOLDEN = GOLDEN / "sugoroku.json"
BOARD_NOW = datetime(2026, 11, 10, 12, tzinfo=JST)
NEXT_MONTH = datetime(2026, 12, 1, 4, tzinfo=JST)
BOARD_RECOVERY_SECONDS = 180
READ, CLAIM, MARK_READ = 62, 63, 64
BOARD_REGISTRATIONS = (READ, CLAIM, MARK_READ)
REWARD, REWARD_ID = "REWARD", "ID"
LOGGER = "aigis_ps"
FOREIGN_KEY = "c" * 32
SENTINEL = "sentinel"
# The spent base reaches every active reward; the early base reaches only the first four thresholds.
SPENT_BASE, EARLY_BASE = "spent", "early"
BASE_SPENDING = {SPENT_BASE: 800, EARLY_BASE: 20}
UNIT_REWARD, ITEM_REWARD, GOLD_REWARD, SECOND_UNIT, SECOND_ITEM = 2, 3, 42, 4, 6
# Rewards 1 and 10 mail ten-pull tickets whose pools are incomplete, so their delivery answers 501.
TICKET_REWARDS = (1, 10)
CAPACITY_REWARDS = (15, 20, 23, 52, 29, 53, 35, 38, 54)
EXPIRED_REWARD, UNKNOWN_REWARD = 26, 999999
CAPACITY_FIELDS = ("ExtendedMaxCharisma", "ExtendedMaxCharismaNextMonth", "ExtendedMaxStamina",
                   "ExtendedMaxStaminaNextMonth")
BOARD_KEYS = ("sugoroku", BOARD_HISTORY, "monthly_capacity", "presents_schema", "last_present_id")
EXISTING_MAIL = {"kind": "gold", "amount": 1, "description": "既存"}
BOARD_DOMAINS, COOKING_DOMAINS = {"sugoroku", "presents", "game_param"}, {LOGIN_COOKING}
CLAIM_READBACK = {"game_param": [33, 34, 35], "presents": [42]}
COOKING_READBACK = {LOGIN_COOKING: [33, 35, 42]}
CURRENT_PERIOD, OLDER_PERIOD, NEXT_PERIOD = "2026-11", "2026-09", "2026-12"
CONSOLE_GENERATION = "gen-events"
SPENDING_BASELINE, LEGACY_SPENDING = 625, 265
RAISED_BORDER, LOWERED_BORDER, TIGHT_BORDER = 400, 200, 270
RAISED_QUOTA = (SPENDING_BASELINE - RAISED_BORDER) // CRYSTALS_PER_COOKING
LEGACY_QUOTA = (LEGACY_SPENDING - LOWERED_BORDER) // CRYSTALS_PER_COOKING
LEGACY_ISSUED, OLDER_ISSUED = 5, 20
SECOND_DAY = COOKING_NOW + timedelta(days=1)


class BoardStore(MemoryStore):
    def overwrite(self, state: dict) -> None:
        # Stands in for an offline edit of the saved JSON, which leaves the stored revision alone.
        self._state = json.loads(serialize(state))


@dataclass(frozen=True)
class BoardStep:
    registration: int
    ids: tuple[int, ...] | None = ()
    tick: int = 1000
    user_key: str = USER_KEY
    method_key: str = METHOD_KEY
    now: datetime = BOARD_NOW
    prepare: Callable[[dict], None] | None = None
    rewrite: Callable[[dict], None] | None = None

    def columns(self) -> tuple:
        if self.registration == READ or self.ids is None:
            return ()
        return (Container(REWARD, (Column(REWARD_ID, "I", self.ids),) if self.ids else ()),)

    def envelope(self):
        profile = PlayerProfile(PLAYER_ID, self.user_key, self.method_key, {}, 0)
        return request(SimpleNamespace(profile=lambda: profile), self.registration, self.columns(), tick=self.tick)


@dataclass(frozen=True)
class BoardCase:
    steps: tuple[BoardStep, ...]
    base: str = SPENT_BASE
    stored: Callable[[Any, dict], dict] | None = None


class WarningLog(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def logged_warnings() -> Iterator[list[str]]:
    logger, handler = logging.getLogger(LOGGER), WarningLog()
    level = logger.level
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def board_service(bundle, clock: SimpleNamespace) -> SugorokuService:
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: clock.now
    return SugorokuService(bootstrap, bundle.player_state, bundle.presents, bundle.sugoroku.catalog,
                           recovery_seconds=BOARD_RECOVERY_SECONDS)


def mailbox(start: dict, final: dict) -> dict:
    # A full mailbox would repeat every stored row in each case, so unchanged rows are pinned by digest instead.
    stored = {row["PresentID"] for row in start["tables"]["presents"]}
    rows = final["tables"]["presents"]
    return {"count": len(rows), "added": [row for row in rows if row["PresentID"] not in stored],
            "sha256": sha256(canonical(rows).encode()).hexdigest()}


def board_inside(start: dict, final: dict) -> dict:
    return {**{key: final.get(key) for key in BOARD_KEYS}, "presents": mailbox(start, final),
            "capacity": {name: final["tables"]["game_param"][0][name] for name in CAPACITY_FIELDS}}


def board_outside(state: dict) -> dict:
    rest = {key: value for key, value in deepcopy(state).items() if key not in BOARD_KEYS}
    rest["tables"].pop("presents")
    for name in CAPACITY_FIELDS:
        rest["tables"]["game_param"][0].pop(name)
    return rest


def board_outcome(bundle, base: dict, case: BoardCase) -> dict:
    start = deepcopy(base)
    if case.stored is not None:
        start = case.stored(bundle, start)
    clock = SimpleNamespace(now=BOARD_NOW)
    service, store, steps = board_service(bundle, clock), BoardStore(start), []
    for step in case.steps:
        clock.now = step.now
        if step.prepare is not None:
            profile = store.profile()
            step.prepare(profile.state)
            store.update_state(profile.state, profile.revision)
        if step.rewrite is not None:
            changed = store.profile().state
            step.rewrite(changed)
            store.overwrite(changed)
        before, envelope = store.profile(), step.envelope()
        with logged_warnings() as warnings:
            try:
                result = {"response": service.handlers()[step.registration](envelope, store).decode("utf-8")}
            except ProtocolError as error:
                result = {"error": {"code": error.code, "message": str(error), "status": error.status}}
        after = store.profile()
        assert board_outside(after.state) == board_outside(before.state)
        steps.append({**result, "request": envelope.xml.decode("utf-8"), "revisions": after.revision - before.revision,
                      "warnings": list(warnings)})
    return {"steps": steps, "state": board_inside(start, store.profile().state)}


@cache
def recorded(path: Path) -> dict:
    return json.loads(path.read_bytes())


def live_base(name: str) -> dict:
    return deepcopy(recorded(BOARD_GOLDEN)["bases"][name])


def read(**options) -> BoardStep:
    return BoardStep(READ, **options)


def claim(*ids: int, **options) -> BoardStep:
    return BoardStep(CLAIM, ids, **options)


def mark(*ids: int, **options) -> BoardStep:
    return BoardStep(MARK_READ, ids, **options)


def touch(state: dict) -> None:
    state[SENTINEL] = state.get(SENTINEL, 0) + 1


def legacy_history(state: dict) -> None:
    history = state[BOARD_HISTORY]
    state[BOARD_HISTORY] = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 1000,
                            "seen_keys": list(history["processed_keys"]),
                            "receipts": [{**{key: value for key, value in entry.items() if key != "granted"},
                                          "client_time": 1000} for entry in history["receipts"]]}


def full_mailbox(bundle, state: dict) -> dict:
    return bundle.presents.enqueue(state, [EXISTING_MAIL] * MAX_PRESENTS)[0]


def exhausted_identities(bundle, state: dict) -> dict:
    state.update(presents_schema=1, last_present_id=INT32_MAX)
    return state


def next_capacity(headroom: int, *, rank_bound: bool) -> Callable[[Any, dict], dict]:
    def store(bundle, state: dict) -> dict:
        row = state["tables"]["game_param"][0]
        ceiling = INT32_MAX - (bundle.bootstrap.rank_rows[row["Rank"] - 1]["CharismaMax"] if rank_bound else 0)
        state["monthly_capacity"]["next"] = row["ExtendedMaxCharismaNextMonth"] = ceiling - headroom
        return state

    return store


def corrupted(key: str, **values) -> Callable[[Any, dict], dict]:
    def store(bundle, state: dict) -> dict:
        state[key] = {**state.get(key, {}), **values}
        return state

    return store


def spending_rolled(bundle, state: dict) -> dict:
    return settle_spending(state, NEXT_MONTH)


def wrong_key(**options) -> dict:
    return {"user_key": FOREIGN_KEY, **options}


BOARD_CASES = {
    "read_board": BoardCase((read(),)),
    "read_after_claim": BoardCase((claim(UNIT_REWARD), read(tick=1001))),
    "claim_unit": BoardCase((claim(UNIT_REWARD),)),
    "claim_item": BoardCase((claim(ITEM_REWARD),)),
    "claim_gold": BoardCase((claim(GOLD_REWARD),)),
    "claim_capacity": BoardCase((claim(*CAPACITY_REWARDS),)),
    "claim_capacity_mail_and_ticket": BoardCase((claim(CAPACITY_REWARDS[0], GOLD_REWARD, TICKET_REWARDS[0]),)),
    "claim_mixed_kinds": BoardCase((claim(UNIT_REWARD, ITEM_REWARD, GOLD_REWARD, *CAPACITY_REWARDS[:2], SECOND_UNIT,
                                          SECOND_ITEM),)),
    "claim_keeps_tickets_reached": BoardCase((claim(UNIT_REWARD, *TICKET_REWARDS), read(tick=1001))),
    "claim_skips_unknown_and_expired": BoardCase((claim(UNKNOWN_REWARD, UNIT_REWARD, EXPIRED_REWARD),)),
    "claim_tier_with_claimed": BoardCase((claim(UNIT_REWARD), claim(UNIT_REWARD, ITEM_REWARD, tick=1001))),
    "claim_after_mark_read": BoardCase((mark(UNIT_REWARD, ITEM_REWARD), claim(UNIT_REWARD, tick=1001))),
    "claim_empty_selection": BoardCase((claim(),)),
    "claim_unknown_only": BoardCase((claim(UNKNOWN_REWARD),)),
    "claim_expired_only": BoardCase((claim(EXPIRED_REWARD),)),
    "claim_claimed_only": BoardCase((claim(UNIT_REWARD), claim(UNIT_REWARD, tick=1001))),
    "claim_ticket_only": BoardCase((claim(TICKET_REWARDS[0]),)),
    "claim_tickets_only": BoardCase((claim(*TICKET_REWARDS),)),
    "claim_unknown_and_ticket": BoardCase((claim(UNKNOWN_REWARD, TICKET_REWARDS[1]),)),
    "claim_zero_id": BoardCase((claim(0),)),
    "claim_negative_id": BoardCase((claim(-1),)),
    "claim_duplicate_ids": BoardCase((claim(UNIT_REWARD, UNIT_REWARD),)),
    "claim_without_reward_group": BoardCase((BoardStep(CLAIM, None),)),
    "claim_wrong_user_key": BoardCase((claim(UNIT_REWARD, **wrong_key()),)),
    "claim_wrong_method_key": BoardCase((claim(UNIT_REWARD, method_key=FOREIGN_KEY),)),
    "priority_request_before_identity": BoardCase((claim(0, **wrong_key()),)),
    "priority_identity_before_state": BoardCase((claim(UNIT_REWARD, **wrong_key()),),
                                                stored=corrupted("sugoroku", schema_version=2)),
    "claim_mailbox_full": BoardCase((claim(UNIT_REWARD),), stored=full_mailbox),
    "claim_capacity_with_mailbox_full": BoardCase((claim(CAPACITY_REWARDS[0], UNIT_REWARD),), stored=full_mailbox),
    "claim_capacity_alone_with_mailbox_full": BoardCase((claim(CAPACITY_REWARDS[0]),), stored=full_mailbox),
    "claim_identity_exhausted": BoardCase((claim(UNIT_REWARD),), stored=exhausted_identities),
    "claim_capacity_past_native_field": BoardCase((claim(CAPACITY_REWARDS[0]),),
                                                  stored=next_capacity(5, rank_bound=True)),
    "claim_capacity_past_ledger": BoardCase((claim(CAPACITY_REWARDS[0]),), stored=next_capacity(5, rank_bound=False)),
    "claim_board_state_invalid": BoardCase((claim(UNIT_REWARD),), stored=corrupted("sugoroku", schema_version=2)),
    "claim_capacity_state_invalid": BoardCase((claim(UNIT_REWARD),),
                                              stored=corrupted("monthly_capacity", schema_version=2)),
    "claim_history_invalid": BoardCase((claim(UNIT_REWARD),), stored=corrupted(BOARD_HISTORY, schema_version=99)),
    "claim_month_refresh_required": BoardCase((claim(UNIT_REWARD, now=NEXT_MONTH),)),
    "claim_capacity_refresh_required": BoardCase((claim(UNIT_REWARD, now=NEXT_MONTH),), stored=spending_rolled),
    "claim_clock_backwards": BoardCase((claim(UNIT_REWARD, now=BOARD_NOW - timedelta(hours=1)),)),
    "claim_replay": BoardCase((claim(UNIT_REWARD, *TICKET_REWARDS), claim(UNIT_REWARD, *TICKET_REWARDS))),
    "claim_replay_after_change": BoardCase((claim(UNIT_REWARD), claim(UNIT_REWARD, prepare=touch))),
    "claim_processed_after_eviction": BoardCase((
        claim(UNIT_REWARD), *(mark(ITEM_REWARD, tick=1001 + offset) for offset in range(HISTORY_LIMIT)),
        claim(UNIT_REWARD))),
    "claim_legacy_receipt_replay": BoardCase((
        claim(UNIT_REWARD, TICKET_REWARDS[0]), claim(UNIT_REWARD, TICKET_REWARDS[0], rewrite=legacy_history),
        claim(ITEM_REWARD, tick=999))),
    "mark_read": BoardCase((mark(UNIT_REWARD, ITEM_REWARD),)),
    "mark_read_empty": BoardCase((mark(), mark())),
    "mark_read_unknown": BoardCase((mark(UNKNOWN_REWARD),)),
    "mark_read_after_claim": BoardCase((claim(UNIT_REWARD), mark(UNIT_REWARD, ITEM_REWARD, tick=1001))),
    "early_read": BoardCase((read(),), base=EARLY_BASE),
    "early_claim_unreached": BoardCase((claim(GOLD_REWARD),), base=EARLY_BASE),
    "early_claim_reached_and_unreached": BoardCase((claim(UNIT_REWARD, GOLD_REWARD),), base=EARLY_BASE),
    "early_mark_unreached": BoardCase((mark(GOLD_REWARD),), base=EARLY_BASE),
}


def test_board_golden_covers_every_case_with_successes_and_refusals():
    golden = recorded(BOARD_GOLDEN)
    assert golden["registrations"] == list(BOARD_REGISTRATIONS) and golden["cases"].keys() == BOARD_CASES.keys()
    assert golden["bases"].keys() == BASE_SPENDING.keys()
    steps = [step for case in golden["cases"].values() for step in case["steps"]]
    assert {step["error"]["status"] for step in steps if "error" in step} >= {400, 409, 501, 503}
    assert any(step["revisions"] == 0 and "response" in step for step in steps)
    assert any(step["warnings"] for step in steps if "response" in step)


@pytest.mark.parametrize("name", BOARD_CASES)
def test_board_requests_match_the_recorded_golden_bytes(golden_bundle, name):
    case = BOARD_CASES[name]
    result = board_outcome(golden_bundle, live_base(case.base), case)
    assert canonical(result) == canonical(recorded(BOARD_GOLDEN)["cases"][name])


def board(bundle) -> SugorokuService:
    return board_service(bundle, SimpleNamespace(now=BOARD_NOW))


def without_receipts(slices: dict) -> dict:
    return {key: value for key, value in slices.items() if key != BOARD_HISTORY}


def native_error(name: str) -> dict:
    return recorded(BOARD_GOLDEN)["cases"][name]["steps"][0]["error"]


def payload(error: ProtocolError) -> dict:
    return {"code": error.code, "message": str(error), "status": error.status}


def test_selection_reads_the_synchronized_board_without_saving(bundle):
    service, state = board(bundle), live_base(SPENT_BASE)
    requested = (UNKNOWN_REWARD, UNIT_REWARD, EXPIRED_REWARD, ITEM_REWARD)
    selection = service.select_rewards(state, requested, BOARD_NOW)
    assert state == live_base(SPENT_BASE)
    assert selection.requested == requested and selection.shown == selection.candidates == (UNIT_REWARD, ITEM_REWARD)
    assert selection.skipped == (UNKNOWN_REWARD, EXPIRED_REWARD)
    rows = {row["ID"]: row["Status"] for row in snapshot_sugoroku(selection.state, service.catalog, BOARD_NOW)}
    assert rows[UNIT_REWARD] == rows[ITEM_REWARD] == 0
    # The native 62 saves exactly this synchronization, so a selection starts from the board the client reads.
    read_board = recorded(BOARD_GOLDEN)["cases"]["read_board"]["state"]
    assert canonical(board_inside(state, selection.state)) == canonical(read_board)


def test_selection_keeps_claimed_rewards_shown_but_skipped(bundle):
    service = board(bundle)
    first = service.grant_rewards(service.select_rewards(live_base(SPENT_BASE), (UNIT_REWARD,), BOARD_NOW), BOARD_NOW)
    again = service.select_rewards(first.state, (UNIT_REWARD, ITEM_REWARD), BOARD_NOW)
    assert again.shown == (UNIT_REWARD, ITEM_REWARD)
    assert (again.candidates, again.skipped) == ((ITEM_REWARD,), (UNIT_REWARD,))


@pytest.mark.parametrize("ids", [(0,), (-1,), (UNIT_REWARD, UNIT_REWARD), (INT32_MAX + 1,), (True,)])
def test_selection_refuses_identities_the_board_never_holds(bundle, ids):
    state = live_base(SPENT_BASE)
    fail(state, lambda: board(bundle).select_rewards(state, ids, BOARD_NOW), "sugoroku_selection_invalid")


def test_granting_commits_what_the_native_claim_commits_without_its_receipt(bundle):
    service, state = board(bundle), live_base(SPENT_BASE)
    ids = (CAPACITY_REWARDS[0], GOLD_REWARD, TICKET_REWARDS[0])
    granted = service.grant_rewards(service.select_rewards(state, ids, BOARD_NOW), BOARD_NOW)
    native = recorded(BOARD_GOLDEN)["cases"]["claim_capacity_mail_and_ticket"]["state"]
    assert canonical(without_receipts(board_inside(state, granted.state))) == canonical(without_receipts(native))
    assert BOARD_HISTORY not in granted.state
    assert (granted.granted, granted.unsupported) == ((CAPACITY_REWARDS[0], GOLD_REWARD), (TICKET_REWARDS[0],))
    assert [reward["kind"] for reward in granted.rewards] == [CAPACITY_REWARD, "gold"]
    assert granted.deliveries[CAPACITY_REWARDS[0]] == ()
    mailed = [row["PresentID"] for row in native["presents"]["added"]]
    assert granted.mail_ids == list(granted.deliveries[GOLD_REWARD]) == mailed


def test_granting_keeps_the_native_refusal_when_nothing_can_be_delivered(bundle):
    service = board(bundle)
    selection = service.select_rewards(live_base(SPENT_BASE), TICKET_REWARDS, BOARD_NOW)
    with pytest.raises(ProtocolError) as caught:
        service.grant_rewards(selection, BOARD_NOW)
    assert payload(caught.value) == native_error("claim_tickets_only")


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_events.register(commands)
    return commands


def console(services, state, preflight, *, now=BOARD_NOW):
    store = FakeStore(state)
    generation = FakeGeneration(id=CONSOLE_GENERATION, bundle=services, clock=now)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def batch(registry, command, params):
    return registry.batch([{"operation": command.operation, "params": params}])


def commit(subject, plan, confirmations=()):
    return subject.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=plan.expected_revision,
                          op_id=str(uuid.uuid4()), confirmations=confirmations)


def preview_error(subject, commands, code, status):
    with pytest.raises(AdminError) as caught:
        subject.preview(commands)
    assert (caught.value.code, caught.value.status) == (code, status)
    return caught.value


def test_event_commands_register_against_the_preflight_table():
    catalog = discover(PREFLIGHT_DOMAINS)
    assert "admin_cmd_events" in catalog.modules
    assert {command.operation for command in COMMANDS} <= {command.operation for command in catalog.commands}
    assert (CLAIM_BOARD.touched, CLAIM_BOARD.destructive) == (BOARD_DOMAINS, False)
    assert (MIGRATE_COOKING.touched, MIGRATE_COOKING.destructive) == (COOKING_DOMAINS, True)
    assert not any(command.writes or command.preflight_none for command in COMMANDS)
    assert MIGRATE_COOKING.phrase(MIGRATE_COOKING.parse({})) == COOKING_PHRASE


def test_cooking_migration_takes_the_name_and_phrase_the_115_warning_gives():
    assert MIGRATE_COOKING.operation is COOKING_MIGRATION
    assert COOKING_PHRASE is COOKING_MIGRATION_PHRASE


@pytest.mark.parametrize("command, params", [
    (CLAIM_BOARD, {}), (CLAIM_BOARD, {"ids": []}), (CLAIM_BOARD, {"ids": [0]}), (CLAIM_BOARD, {"ids": [-1]}),
    (CLAIM_BOARD, {"ids": [INT32_MAX + 1]}), (CLAIM_BOARD, {"ids": [True]}), (CLAIM_BOARD, {"ids": ["2"]}),
    (CLAIM_BOARD, {"ids": [2.0]}), (CLAIM_BOARD, {"ids": [UNIT_REWARD, UNIT_REWARD]}),
    (CLAIM_BOARD, {"ids": list(range(1, MAX_REWARDS + 2))}), (CLAIM_BOARD, {"ids": [UNIT_REWARD], "all": True}),
    (MIGRATE_COOKING, {"border": RAISED_BORDER}), (MIGRATE_COOKING, []),
])
def test_event_parameters_are_strict(command, params):
    with pytest.raises(AdminError) as caught:
        command.parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_claim_plan_mails_the_rewards_and_commits_the_native_board_state(bundle, preflight, registry):
    state = live_base(SPENT_BASE)
    store, subject = console(bundle, state, preflight)
    ids = [CAPACITY_REWARDS[0], GOLD_REWARD, TICKET_REWARDS[0], UNKNOWN_REWARD]
    plan = subject.preview(batch(registry, CLAIM_BOARD, {"ids": ids}))
    assert plan.committable and plan.preflight["readback"] == CLAIM_READBACK and not plan.confirmations
    step, catalog = plan.steps[0], bundle.sugoroku.catalog
    capacity, gold = (catalog.by_id[identifier].count for identifier in (CAPACITY_REWARDS[0], GOLD_REWARD))
    assert step.warnings == (REWARDS_SKIPPED, REWARDS_UNSUPPORTED)
    assert dict(step.effects) == {
        "period": CURRENT_PERIOD,
        "claimed": [{"reward_id": CAPACITY_REWARDS[0], "kind": CAPACITY_REWARD, "amount": capacity, "mail_ids": []},
                    {"reward_id": GOLD_REWARD, "kind": "gold", "amount": gold, "mail_ids": [1]}],
        "already_claimed": [], "unavailable": [UNKNOWN_REWARD], "unsupported": [TICKET_REWARDS[0]],
        "mailbox": {"before": 0, "after": 1, "capacity": MAX_PRESENTS},
        "next_month_capacity": {"before": 0, "after": capacity}}
    outcome = commit(subject, plan)
    assert (store.revision, outcome.result["revision_after"], store.backups) == (1, 1, [])
    native = board_outcome(bundle, state, BoardCase((claim(*ids),)))["state"]
    assert canonical(without_receipts(board_inside(state, store.state))) == canonical(without_receipts(native))
    assert BOARD_HISTORY not in store.state


def test_client_resend_after_a_console_claim_reads_the_claimed_rows(bundle, preflight, registry):
    store, subject = console(bundle, live_base(SPENT_BASE), preflight)
    commit(subject, subject.preview(batch(registry, CLAIM_BOARD, {"ids": [UNIT_REWARD]})))
    game = BoardStore(store.state)
    before = game.profile()
    response = board(bundle).claim(claim(UNIT_REWARD).envelope(), game)
    assert statuses(response) == {UNIT_REWARD: CLAIMED} and game.profile() == before


@pytest.mark.parametrize("ids", [[UNKNOWN_REWARD], [EXPIRED_REWARD], [UNKNOWN_REWARD, EXPIRED_REWARD]])
def test_claim_with_nothing_to_deliver_plans_no_change(bundle, preflight, registry, ids):
    state = live_base(SPENT_BASE)
    store, subject = console(bundle, state, preflight)
    plan = subject.preview(batch(registry, CLAIM_BOARD, {"ids": ids}))
    step = plan.steps[0]
    assert plan.committable and plan.state_sha256_before == plan.state_sha256_after
    assert (step.warnings, step.effects["claimed"], step.effects["unavailable"]) == ((NOTHING_CLAIMABLE,), [], ids)
    commit(subject, plan)
    assert store.revision == 0 and canonical(store.state) == canonical(state)


def test_claim_of_claimed_rewards_reports_them_without_another_mail(bundle, preflight, registry):
    store, subject = console(bundle, live_base(SPENT_BASE), preflight)
    commit(subject, subject.preview(batch(registry, CLAIM_BOARD, {"ids": [UNIT_REWARD]})))
    plan = subject.preview(batch(registry, CLAIM_BOARD, {"ids": [UNIT_REWARD]}))
    step = plan.steps[0]
    assert step.warnings == (NOTHING_CLAIMABLE,) and step.effects["already_claimed"] == [UNIT_REWARD]
    assert step.effects["mailbox"] == {"before": 1, "after": 1, "capacity": MAX_PRESENTS}
    assert plan.state_sha256_before == plan.state_sha256_after


@pytest.mark.parametrize("ids, native_case, stored, now", [
    (list(TICKET_REWARDS), "claim_tickets_only", None, BOARD_NOW),
    ([UNIT_REWARD], "claim_mailbox_full", full_mailbox, BOARD_NOW),
    ([CAPACITY_REWARDS[0]], "claim_capacity_past_native_field", next_capacity(5, rank_bound=True), BOARD_NOW),
    ([UNIT_REWARD], "claim_month_refresh_required", None, NEXT_MONTH),
    ([UNIT_REWARD], "claim_board_state_invalid", corrupted("sugoroku", schema_version=2), BOARD_NOW),
], ids=["undeliverable", "mailbox_full", "capacity_overflow", "month_refresh", "board_invalid"])
def test_claim_refusals_match_the_native_board(bundle, preflight, registry, ids, native_case, stored, now):
    state = live_base(SPENT_BASE)
    store, subject = console(bundle, state if stored is None else stored(bundle, state), preflight, now=now)
    native = native_error(native_case)
    error = preview_error(subject, batch(registry, CLAIM_BOARD, {"ids": ids}), native["code"], native["status"])
    assert str(error) == native["message"] and error.details == {"step": 0, "operation": CLAIM_BOARD.operation}
    assert store.revision == 0


@pytest.mark.parametrize("command, service, params", [
    (CLAIM_BOARD, "sugoroku", {"ids": [UNIT_REWARD]}), (MIGRATE_COOKING, "login_rewards", {}),
])
def test_commands_are_unavailable_without_their_services(bundle, preflight, registry, command, service, params):
    _, subject = console(replace(bundle, **{service: None}), live_base(SPENT_BASE), preflight)
    error = preview_error(subject, batch(registry, command, params), "admin_command_unavailable", 503)
    assert str(error) == missing_service(service)


def with_previous_spending(state: dict, previous: int) -> dict:
    ledger = state["crystal_spending"]
    ledger["previous_spent"] = previous
    state["tables"]["game_param2"][0]["StampCardGrade"] = ((min(previous, PROJECTION_CAP) << 16)
                                                            | min(ledger["current_spent"], PROJECTION_CAP))
    return state


def running_border(bundle) -> int:
    return bundle.login_rewards.cooking_border


def unchanged_effects(state: dict) -> dict:
    ledger, spending = read_cooking(state), state["crystal_spending"]
    return {"period": ledger["period"], "spending_period": spending["period"],
            "previous_spent": spending["previous_spent"], "baseline_source": ledger["baseline_source"],
            "issued": ledger["issued"], "border": {"before": ledger["border"], "after": ledger["border"]},
            "quota": {"before": ledger["quota"], "after": ledger["quota"]},
            "remaining": ledger["quota"] - ledger["issued"]}


def unchanged_commit(subject, registry, store, state) -> None:
    plan = subject.preview(batch(registry, MIGRATE_COOKING, {}))
    step = plan.steps[0]
    assert plan.committable and plan.state_sha256_before == plan.state_sha256_after
    assert (step.warnings, dict(step.effects)) == ((COOKING_UNCHANGED,), unchanged_effects(state))
    commit(subject, plan, (COOKING_PHRASE,))
    # An unchanged ledger is only audited, so the single automatic backup and the revision stay as they were.
    assert [call["state"] for call in store.calls] == [None]
    assert (store.revision, store.backups) == (0, []) and canonical(store.state) == canonical(state)


def test_migration_of_a_ledger_that_already_matches_saves_nothing(bundle, preflight, registry):
    state = live_base(SPENT_BASE)
    store, subject = console(bundle, state, preflight)
    unchanged_commit(subject, registry, store, state)


def test_migration_leaves_an_ended_month_to_the_next_login(bundle, preflight, registry):
    border = running_border(bundle)
    state = with_previous_spending(live_base(SPENT_BASE), SPENDING_BASELINE)
    store, subject = console(bundle, state, preflight, now=NEXT_MONTH)
    unchanged_commit(subject, registry, store, state)
    # The next login settles spending first, so the ended month's stale quota never reaches a same-month check.
    reset = read_cooking(sync_cooking(settle_spending(state, NEXT_MONTH), NEXT_MONTH, border=border))
    assert (reset["period"], reset["issued"]) == (NEXT_PERIOD, 0)


def test_migration_rederives_the_quota_after_the_spending_baseline_changes(bundle, preflight, registry):
    border = running_border(bundle)
    state = with_previous_spending(live_base(SPENT_BASE), SPENDING_BASELINE)
    fail(state, lambda: sync_cooking(state, BOARD_NOW, border=border), "login_cooking_invalid")
    store, subject = console(bundle, state, preflight)
    plan = subject.preview(batch(registry, MIGRATE_COOKING, {}))
    assert plan.committable and plan.preflight["readback"] == COOKING_READBACK
    assert [item.phrase for item in plan.confirmations] == [COOKING_PHRASE]
    before = read_cooking(state)
    assert dict(plan.steps[0].effects) == {
        "period": CURRENT_PERIOD, "spending_period": CURRENT_PERIOD, "previous_spent": SPENDING_BASELINE,
        "baseline_source": before["baseline_source"], "issued": 0, "border": {"before": border, "after": border},
        "quota": {"before": before["quota"], "after": MAX_COOKING}, "remaining": MAX_COOKING}
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_confirmation_required" and store.revision == 0
    commit(subject, plan, (COOKING_PHRASE,))
    assert store.revision == 1 and [backup["row"]["kind"] for backup in store.backups] == [AUTO_BACKUP_KIND]
    assert read_cooking(store.state) == {**before, "quota": MAX_COOKING}
    assert cooking_fields(store.state) == cooking_fields(state)
    assert canonical(sync_cooking(store.state, BOARD_NOW, border=border)) == canonical(store.state)


def test_migration_never_takes_back_cookings_already_issued(bundle, preflight, registry):
    border = running_border(bundle)
    state = migrate_cooking(with_previous_spending(live_base(SPENT_BASE), SPENDING_BASELINE), BOARD_NOW, border=border)
    state, issued = advance_cooking(state, BOARD_NOW, border=border, eligible=True)
    assert issued
    store, subject = console(bundle, with_previous_spending(state, border), preflight)
    preview_error(subject, batch(registry, MIGRATE_COOKING, {}), "login_cooking_quota_below_issued", 409)
    assert store.revision == 0


def test_migration_moves_the_ledger_to_the_running_border(bundle):
    state = with_previous_spending(live_base(SPENT_BASE), SPENDING_BASELINE)
    services = SimpleNamespace(login_rewards=SimpleNamespace(cooking_border=RAISED_BORDER))
    context = CommandContext(now=BOARD_NOW, acquisition_day=BOARD_NOW.date().toordinal() - 1, revision=0,
                             generation_id=CONSOLE_GENERATION, services=services)
    result = MIGRATE_COOKING.apply(state, MIGRATE_COOKING.parse({}), context)
    assert canonical(result.state) == canonical(migrate_cooking(state, BOARD_NOW, border=RAISED_BORDER))
    assert result.effects["border"] == {"before": read_cooking(state)["border"], "after": RAISED_BORDER}
    assert result.effects["quota"]["after"] == RAISED_QUOTA and result.warnings == ()


def test_border_migration_rederives_the_current_quota_and_keeps_issued_records():
    state, _ = advance_cooking(seeded(previous=SPENDING_BASELINE), COOKING_NOW, border=BORDER, eligible=True)
    migrated = migrate_cooking(state, COOKING_NOW, border=RAISED_BORDER)
    assert read_cooking(migrated) == {**read_cooking(state), "border": RAISED_BORDER, "quota": RAISED_QUOTA}
    assert cooking_fields(migrated) == cooking_fields(state) and unrelated(migrated) == unrelated(state)
    fail(migrated, lambda: sync_cooking(migrated, COOKING_NOW, border=BORDER), "login_cooking_rules_changed")
    assert sync_cooking(migrated, COOKING_NOW, border=RAISED_BORDER) == migrated
    assert migrate_cooking(migrated, COOKING_NOW, border=RAISED_BORDER) == migrated


def test_spending_baseline_migration_recomputes_the_same_month_quota():
    state = with_previous_spending(seeded(), SPENDING_BASELINE)
    fail(state, lambda: sync_cooking(state, COOKING_NOW, border=BORDER), "login_cooking_invalid")
    migrated = migrate_cooking(state, COOKING_NOW, border=BORDER)
    assert read_cooking(migrated) == {**read_cooking(state), "quota": MAX_COOKING}
    assert sync_cooking(migrated, COOKING_NOW, border=BORDER) == migrated


def fully_issued() -> dict:
    state, _ = advance_cooking(seeded(), COOKING_NOW, border=BORDER, eligible=True)
    state, _ = advance_cooking(state, SECOND_DAY, border=BORDER, eligible=True)
    assert read_cooking(state)["issued"] == read_cooking(state)["quota"]
    return state


def test_issued_cookings_above_the_new_quota_refuse_the_migration():
    state = fully_issued()
    fail(state, lambda: migrate_cooking(state, SECOND_DAY, border=TIGHT_BORDER), "login_cooking_quota_below_issued")


def test_ended_months_change_only_their_border_even_above_the_new_quota():
    state = fully_issued()
    # The spending ledger stays on the ended month while the server is stopped, so only the clock shows the boundary.
    migrated = migrate_cooking(state, NEXT_MONTH, border=TIGHT_BORDER)
    assert read_cooking(migrated) == {**read_cooking(state), "border": TIGHT_BORDER}
    assert cooking_fields(migrated) == cooking_fields(state) and unrelated(migrated) == unrelated(state)
    reset = read_cooking(sync_cooking(settle_spending(migrated, NEXT_MONTH), NEXT_MONTH, border=TIGHT_BORDER))
    assert (reset["period"], reset["issued"], reset["border"]) == (NEXT_PERIOD, 0, TIGHT_BORDER)


def test_migration_refuses_a_clock_before_the_saved_state():
    state = fully_issued()
    fail(state, lambda: migrate_cooking(state, COOKING_NOW, border=RAISED_BORDER), "login_cooking_clock_backwards")


def test_explicit_legacy_baselines_keep_their_over_quota_count():
    state = cooking_base(previous=LEGACY_SPENDING)
    state["tables"]["game_param2"][0].update(HomeCookingReceivedAmount=LEGACY_ISSUED,
                                             HomeCookingReceivedDate=date(2026, 11, 8).toordinal() - 1)
    legacy = initialize_cooking(state, border=BORDER, legacy_period=CURRENT_PERIOD)
    migrated = migrate_cooking(legacy, COOKING_NOW, border=LOWERED_BORDER)
    assert LEGACY_QUOTA < LEGACY_ISSUED and read_cooking(legacy)["baseline_source"] == EXPLICIT_LEGACY
    assert read_cooking(migrated) == {**read_cooking(legacy), "border": LOWERED_BORDER, "quota": LEGACY_QUOTA}
    assert cooking_fields(migrated) == cooking_fields(legacy)


def test_older_month_ledgers_change_only_their_border():
    state = cooking_base(previous=SPENDING_BASELINE)
    state["tables"]["game_param2"][0].update(HomeCookingReceivedAmount=OLDER_ISSUED,
                                             HomeCookingReceivedDate=date(2026, 10, 1).toordinal() - 1)
    legacy = initialize_cooking(state, border=BORDER, legacy_period=OLDER_PERIOD)
    migrated = migrate_cooking(legacy, COOKING_NOW, border=RAISED_BORDER)
    assert read_cooking(migrated) == {**read_cooking(legacy), "border": RAISED_BORDER}
    reset = read_cooking(sync_cooking(migrated, COOKING_NOW, border=RAISED_BORDER))
    assert (reset["period"], reset["quota"], reset["issued"]) == (CURRENT_PERIOD, RAISED_QUOTA, 0)


@pytest.mark.parametrize("value", [-1, True, 1.5, None, "250", INT32_MAX + 1])
def test_migration_rejects_invalid_borders(value):
    state = seeded()
    fail(state, lambda: migrate_cooking(state, COOKING_NOW, border=value), "login_cooking_border_invalid")


def test_migration_needs_a_saved_ledger():
    state = cooking_base()
    fail(state, lambda: migrate_cooking(state, COOKING_NOW, border=BORDER), "login_cooking_required")

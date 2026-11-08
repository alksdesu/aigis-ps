from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from aigis_ps import admin_cmd_votes
from aigis_ps.admin_cmd_economy import TICKET_GRANT
from aigis_ps.admin_cmd_votes import COMMANDS, GRANT_TICKETS, TICKETS_CAPPED, TICKETS_REASON, quest_tickets
from aigis_ps.admin_command import (COMMAND_UNAVAILABLE, AdminError, Batch, BatchStep, CommandContext, PlanStore,
                                    missing_service)
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.battle_events import NOT_ATTACHED, BattleEventRewards
from aigis_ps.energy_clock import JST
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES
from aigis_ps.vote_quest_tickets import TICKET_CAP, VoteQuestTickets
from aigis_ps.vote_tickets import DAILY_ITEM
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_quest_service import bundle
from test_vote_quest_tickets import EXPIRED_AT, FINAL, FIRST, SECOND


GENERATION = "gen-votes"
NOW = datetime(2026, 11, 5, 12, tzinfo=JST)
ACQUISITION_DAY = 700_000
GRANTED, NEAR_CAP = 10, TICKET_CAP - 4
EXPIRY_LEDGER = "vote_expiry"
OTHER_ITEM = "gift_0001_quartz"
POSSESSION_READBACK = {"possessions": [129, 137]}


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture
def save(bundle):
    return bundle.initialize(bundle.bootstrap.initial_state())


def registry() -> CommandRegistry:
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_votes.register(commands)
    return commands


def run(services, state: dict, raw: dict):
    context = CommandContext(now=NOW, acquisition_day=ACQUISITION_DAY, revision=0, generation_id=GENERATION,
                             services=services)
    outcome = Batch((BatchStep(GRANT_TICKETS, GRANT_TICKETS.parse(raw)),)).apply(state, context)
    return outcome.state, outcome.steps[0]


def refused(services, state: dict, raw: dict) -> AdminError:
    with pytest.raises(AdminError) as caught:
        run(services, state, raw)
    return caught.value


def console(services, state: dict, preflight):
    store = FakeStore(state)
    generation = FakeGeneration(id=GENERATION, bundle=services, clock=NOW)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def held(state: dict, item_id: str) -> int:
    return next((row["Count"] for row in state["tables"]["possessions"] if row["ItemID"] == item_id), 0)


def test_ticket_grant_registers_through_discovery():
    found = discover(PREFLIGHT_DOMAINS)
    assert "admin_cmd_votes" in found.modules and TICKET_GRANT in found.commands
    assert COMMANDS == (GRANT_TICKETS,) and [command.operation for command in registry()] == [TICKET_GRANT]


def test_ticket_grants_touch_only_possessions_and_take_no_confirmation():
    assert (GRANT_TICKETS.domain, GRANT_TICKETS.touched) == ("votes", frozenset({"possessions"}))
    assert GRANT_TICKETS.touched <= set(PREFLIGHT_DOMAINS)
    assert GRANT_TICKETS.destructive is False and not GRANT_TICKETS.writes and not GRANT_TICKETS.preflight_none


@pytest.mark.parametrize("params", [
    {}, {"item_id": FIRST}, {"count": 1}, {"item_id": "", "count": 1},
    {"item_id": "t" * (STORED_ITEM_NAME_BYTES + 1), "count": 1}, {"item_id": 1, "count": 1},
    {"item_id": FIRST, "count": 0}, {"item_id": FIRST, "count": -1}, {"item_id": FIRST, "count": True},
    {"item_id": FIRST, "count": 1.0}, {"item_id": FIRST, "count": "1"}, {"item_id": FIRST, "count": INT32_MAX + 1},
    {"item_id": FIRST, "count": 1, "group_id": 1},
])
def test_ticket_grant_parameters_are_strict(params):
    with pytest.raises(AdminError) as caught:
        GRANT_TICKETS.parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_the_quest_ticket_provider_is_read_from_the_battle_event_rewards(bundle):
    provider = bundle.quests.event_rewards.vote_quest_tickets
    assert isinstance(provider, VoteQuestTickets) and quest_tickets(bundle) is provider
    rewards = BattleEventRewards()
    assert rewards.vote_quest_tickets is None
    rewards.attach_vote_tickets(provider)
    assert rewards.vote_quest_tickets is provider
    rewards.attach_vote_tickets(None, NOT_ATTACHED)
    assert rewards.vote_quest_tickets is None
    with pytest.raises(AttributeError):
        rewards.vote_quest_tickets = provider


def test_ticket_grants_name_what_they_are_missing(bundle, save):
    detached = SimpleNamespace(quests=SimpleNamespace(event_rewards=BattleEventRewards()))
    assert GRANT_TICKETS.availability(SimpleNamespace()) == missing_service("quests")
    assert GRANT_TICKETS.availability(detached) == TICKETS_REASON
    assert GRANT_TICKETS.availability(bundle) is None
    for services, reason in ((SimpleNamespace(), missing_service("quests")), (detached, TICKETS_REASON)):
        error = refused(services, save, {"item_id": FIRST, "count": 1})
        assert (error.code, error.status, str(error)) == (COMMAND_UNAVAILABLE, 503, reason)


def test_ticket_grants_credit_a_vote_group_ticket(bundle, save):
    granted, step = run(bundle, save, {"item_id": FIRST, "count": GRANTED})
    assert held(granted, FIRST) == GRANTED and step.warnings == ()
    assert dict(step.effects) == {"item_id": FIRST, "requested": GRANTED, "credited": GRANTED, "discarded": 0,
                                  "before": 0, "after": GRANTED, "cap": TICKET_CAP}
    other, _ = run(bundle, granted, {"item_id": FINAL, "count": 3})
    assert (held(other, FIRST), held(other, FINAL)) == (GRANTED, 3)


def test_ticket_grants_stop_at_the_cap_and_report_the_discard(bundle, save):
    stocked = bundle.possessions.credit(save, {FIRST: NEAR_CAP})
    capped, step = run(bundle, stocked, {"item_id": FIRST, "count": GRANTED})
    room = TICKET_CAP - NEAR_CAP
    assert held(capped, FIRST) == TICKET_CAP and step.warnings == (TICKETS_CAPPED,)
    assert (step.effects["credited"], step.effects["discarded"]) == (room, GRANTED - room)
    full, step = run(bundle, capped, {"item_id": FIRST, "count": 1})
    assert full == capped and (step.effects["credited"], step.effects["discarded"]) == (0, 1)
    assert step.warnings == (TICKETS_CAPPED,)


def test_ticket_grants_refuse_an_expired_group_with_its_native_code(bundle, save):
    closed, _ = quest_tickets(bundle).expiry.settle(bundle.possessions.credit(save, {FIRST: 2}), EXPIRED_AT)
    assert FIRST in closed[EXPIRY_LEDGER]["items"]
    error = refused(bundle, closed, {"item_id": FIRST, "count": 1})
    assert (error.code, error.status) == ("vote_ticket_closed", 409)
    granted, _ = run(bundle, closed, {"item_id": SECOND, "count": 1})
    assert held(granted, SECOND) == 1


@pytest.mark.parametrize("item_id", [DAILY_ITEM, OTHER_ITEM, "missing_item"])
def test_ticket_grants_refuse_items_outside_the_vote_groups_with_their_native_code(bundle, save, item_id):
    error = refused(bundle, save, {"item_id": item_id, "count": 1})
    assert (error.code, error.status) == ("vote_quest_ticket_item_invalid", 400)


def test_ticket_grants_preview_with_readback_and_commit_an_audited_revision(bundle, preflight, save):
    store, pipeline = console(bundle, save, preflight)
    plan = pipeline.preview(registry().batch([{"operation": TICKET_GRANT, "params": {"item_id": FIRST,
                                                                                     "count": GRANTED}}]))
    assert plan.committable and plan.restart_check == {"ok": True} and plan.confirmations == ()
    assert plan.preflight["readback"] == POSSESSION_READBACK and plan.audit == {"ok": True}
    committed = pipeline.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest,
                                expected_revision=plan.expected_revision, op_id=str(uuid.uuid4()))
    assert store.revision == 1 and held(store.state, FIRST) == GRANTED
    assert committed.result["backup_before_commit"] is None
    record = store.calls[-1]["record"]
    assert record["operations"] == [{"operation": TICKET_GRANT, "params": {"item_id": FIRST, "count": GRANTED}}]
    assert record["targets"] == ["possessions"] and record["result"]["readback"] == POSSESSION_READBACK
    assert record["result"]["steps"][0]["effects"]["credited"] == GRANTED

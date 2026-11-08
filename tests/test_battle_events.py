from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from aigis_ps import battle_events
from aigis_ps.battle_events import (
    ABSENT_PROVIDER_ERRORS, ENTRUST, EVENT_PARAM_LIMIT, NOT_ATTACHED, RESULT, UNAVAILABLE, BattleEventContext,
    BattleEventGrant, BattleEventRewards,
)
from aigis_ps.energy_clock import JST, mark_new
from aigis_ps.errors import ProtocolError
from aigis_ps.vote_quest_tickets import DEPENDENCIES_UNAVAILABLE, SETUP_ERRORS, VoteTicketGrant
from aigis_ps.vote_rules import VoteRules
from test_quest_service import bundle
from test_vote_catalog import build, vote_sources
from test_vote_quest_tickets import BEFORE_REVIVAL, FIRST, REVIVAL, TODAY, expired, isolated, quest_tickets


NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
RATE = 15
DROP = "trade_item_001000"
PLAYER = {"Money": 1, "Experience": 1, "Rank": 1, "NowCharisma": 0, "NowActionPoint": 0, "CharismaChangeDate": 0,
          "CharismaChangeTime": 0.0, "ActionPointChangeDate": 0, "ActionPointChangeTime": 0.0}
FIELDS = {"source": RESULT, "quest_id": 101, "quest_type": 0, "charisma_per_run": 25, "runs": 1, "event_points": 0,
          "now": NOW, "rewrite_event_param": True}
CONTEXT_INVALID = ("battle_event_context_invalid", 503)
GAME_PARAM_INVALID = ("quest_economy_state", 503)
UNLISTED_ERROR = "fixture_provider_fault"


class Provider:
    content_version = "a" * 64

    def __init__(self):
        self.calls = []

    def evaluate(self, state, *, charisma_per_run, runs, now):
        self.calls.append((charisma_per_run, runs, now))
        count = charisma_per_run // RATE * runs
        return VoteTicketGrant(FIRST, count, "issued" if count else "below_rate")

    def status(self, now=None):
        return {"ready": True, "now": None if now is None else now.isoformat()}


def player(event_param=0):
    return mark_new({"bootstrap_schema": 1, "tables": {"game_param": [{**PLAYER, "EventParam": event_param}]}}, NOW)


def context(source=RESULT, *, charisma=25, runs=1, points=0, now=NOW):
    return BattleEventContext(source, 101, 0, charisma, runs, points, now, source == RESULT)


def attached(provider):
    rewards = BattleEventRewards()
    rewards.attach_vote_tickets(provider)
    return rewards


def rejected(action):
    with pytest.raises(ProtocolError) as caught:
        action()
    return caught.value.code, caught.value.status


def test_runs_reach_the_ticket_provider_and_only_positive_counts_become_increments():
    provider = Provider()
    rewards, state = attached(provider), player()
    issued = rewards.evaluate(state, context(ENTRUST, charisma=25, runs=3))
    below = rewards.evaluate(state, context(RESULT, charisma=14, runs=3))
    free = rewards.evaluate(state, context(RESULT, charisma=0))
    assert (dict(issued.items), dict(below.items), dict(free.items)) == ({FIRST: 3}, {}, {})
    assert issued.receipt["vote_tickets"] == {"error": None, "item_id": FIRST, "count": 3, "reason": "issued"}
    assert below.receipt["vote_tickets"] == {"error": None, "item_id": FIRST, "count": 0, "reason": "below_rate"}
    assert provider.calls == [(25, 3, NOW), (14, 3, NOW), (0, 1, NOW)]
    assert issued.receipt["items"] == {FIRST: 3} and below.receipt["items"] == {}
    with pytest.raises(TypeError):
        issued.items[FIRST] = 4
    with pytest.raises(FrozenInstanceError):
        issued.event_param = 0


@pytest.mark.parametrize("previous", [0, 5, 1799, 1800, 1900, 0x7FFFFFFF])
@pytest.mark.parametrize("points", [0, 1, 700, 1800, 0x7FFFFFFF])
def test_results_rewrite_event_param_on_every_settlement_up_to_the_cap(previous, points):
    grant = BattleEventRewards().evaluate(player(previous), context(RESULT, points=points))
    value = min(previous + points, EVENT_PARAM_LIMIT)
    changed = bool(points) or value != previous
    assert (grant.event_param, grant.event_param_changed) == (value, changed)
    assert grant.receipt["event_param"] == {"rewritten": True, "points": points, "previous": previous,
                                            "value": value, "changed": changed}


def test_results_without_event_points_still_clamp_and_report_the_counter():
    rewards = BattleEventRewards()
    clamped, kept = rewards.evaluate(player(1900), context(RESULT)), rewards.evaluate(player(5), context(RESULT))
    assert (clamped.event_param, clamped.event_param_changed, kept.event_param, kept.event_param_changed) == (
        EVENT_PARAM_LIMIT, True, 5, False)
    capped = rewards.evaluate(player(EVENT_PARAM_LIMIT), context(RESULT, points=700))
    assert (capped.event_param, capped.event_param_changed) == (EVENT_PARAM_LIMIT, True)
    assert EVENT_PARAM_LIMIT == 1800


def test_entrust_rewrites_event_param_only_for_event_points():
    rewards = BattleEventRewards()
    untouched = rewards.evaluate(player(1900), context(ENTRUST, runs=3))
    assert (untouched.event_param, untouched.event_param_changed) == (None, False)
    assert untouched.receipt["event_param"] == {"rewritten": False, "points": 0, "previous": None, "value": None,
                                                "changed": False}
    corrupt = player()
    corrupt["tables"]["game_param"][0]["EventParam"] = -1
    assert rewards.evaluate(corrupt, context(ENTRUST)).event_param is None
    for previous, points, value in ((1790, 20, EVENT_PARAM_LIMIT), (EVENT_PARAM_LIMIT, 5, EVENT_PARAM_LIMIT),
                                    (0, 7, 7)):
        grant = rewards.evaluate(player(previous), context(ENTRUST, points=points, runs=3))
        assert (grant.event_param, grant.event_param_changed) == (value, True)


@pytest.mark.parametrize("value", [-1, 2 ** 31, True, 1.5, None, "5"])
def test_a_rewritten_event_param_must_be_a_native_counter(value):
    state = player()
    state["tables"]["game_param"][0]["EventParam"] = value
    for event in (context(RESULT), context(ENTRUST, points=1)):
        assert rejected(lambda: BattleEventRewards().evaluate(state, event)) == GAME_PARAM_INVALID


def test_a_rewritten_event_param_requires_one_game_param_row_that_stores_it():
    duplicated, missing = player(), player()
    duplicated["tables"]["game_param"].append(dict(duplicated["tables"]["game_param"][0]))
    del missing["tables"]["game_param"][0]["EventParam"]
    for state in (duplicated, missing):
        assert rejected(lambda: BattleEventRewards().evaluate(state, context(RESULT))) == GAME_PARAM_INVALID
        assert BattleEventRewards().evaluate(state, context(ENTRUST)).event_param is None


def test_missing_provider_reports_its_reason_and_issues_no_tickets():
    unattached, failed, working = BattleEventRewards(), BattleEventRewards(), attached(Provider())
    failed.attach_vote_tickets(None, "vote_quest_ticket_source_invalid")
    for rewards, error in ((unattached, NOT_ATTACHED), (failed, "vote_quest_ticket_source_invalid")):
        grant = rewards.evaluate(player(), context(ENTRUST, charisma=30, runs=3))
        assert dict(grant.items) == {}
        assert grant.receipt["vote_tickets"] == {"error": error, "item_id": None, "count": 0, "reason": UNAVAILABLE}
        assert rewards.status(NOW)["vote_tickets"] == {"attached": False, "error": error, "status": None}
    assert working.status(NOW.replace(tzinfo=None))["vote_tickets"] == {
        "attached": True, "error": None, "status": {"ready": True, "now": NOW.isoformat()}}
    assert len({unattached.content_version, failed.content_version, working.content_version}) == 3
    working.attach_vote_tickets(None, "vote_quest_ticket_source_invalid")
    assert working.content_version == failed.content_version


def missing(error):
    rewards = BattleEventRewards()
    rewards.attach_vote_tickets(None, error)
    return rewards


def test_absent_provider_reasons_are_the_unattached_default_and_every_provider_setup_failure():
    # Receipts embed these codes, so renaming one strands the receipts committed under it.
    assert ABSENT_PROVIDER_ERRORS == (NOT_ATTACHED, DEPENDENCIES_UNAVAILABLE, *SETUP_ERRORS) == (
        "vote_quest_tickets_not_attached", "vote_quest_ticket_dependencies_unavailable",
        "vote_quest_ticket_source_invalid", "vote_quest_ticket_definition_invalid",
        "vote_quest_ticket_dependencies_changed", "vote_catalog_changed", "vote_candidates_unresolved",
        "vote_expiry_dependencies_changed")


def test_replayable_versions_cover_every_absent_reason_before_and_after_the_provider_attaches():
    absent = {missing(error).content_version for error in ABSENT_PROVIDER_ERRORS}
    assert len(absent) == len(ABSENT_PROVIDER_ERRORS)
    working = BattleEventRewards()
    detached = working.content_version
    assert working.replayable_content_versions() == absent
    working.attach_vote_tickets(Provider())
    assert detached in working.replayable_content_versions()
    assert working.replayable_content_versions() == {working.content_version, *absent}
    unlisted = missing(UNLISTED_ERROR)
    assert unlisted.replayable_content_versions() == {unlisted.content_version, *absent}
    assert unlisted.content_version not in working.replayable_content_versions()
    replaced = Provider()
    replaced.content_version = "b" * 64
    assert working.content_version not in attached(replaced).replayable_content_versions()
    assert isinstance(working.replayable_content_versions(), frozenset)


def test_receipt_charges_overflow_to_the_event_increments_first():
    rewards = attached(Provider())
    grant = rewards.evaluate(player(), context(RESULT, charisma=45))
    assert dict(grant.items) == {FIRST: 3}
    shares = [({FIRST: 5, DROP: 2}, {FIRST: 3}), ({FIRST: 2}, {FIRST: 2}), ({DROP: 4}, {}), ({}, {})]
    for discarded, tickets in shares:
        receipt = rewards.receipt(grant, discarded)
        assert receipt == {**grant.receipt, "discarded": {"vote_tickets": tickets}}
        assert json.loads(json.dumps(receipt)) == receipt
    receipt["vote_tickets"]["count"] = 0
    assert grant.receipt["vote_tickets"]["count"] == 3


@pytest.mark.parametrize("discarded", [{FIRST: 0}, {FIRST: -1}, {FIRST: True}, {"": 1}, {1: 1}, [(FIRST, 1)], None])
def test_receipt_requires_a_grant_and_positive_discards(discarded):
    rewards = attached(Provider())
    grant = rewards.evaluate(player(), context(RESULT, charisma=45))
    assert rejected(lambda: rewards.receipt(grant, discarded)) == ("battle_event_receipt_invalid", 503)
    assert rejected(lambda: rewards.receipt(grant.receipt, {})) == ("battle_event_receipt_invalid", 503)


def test_preview_matches_settlement_and_never_touches_the_callers_state():
    class Writer(Provider):
        def evaluate(self, state, **request):
            state["tables"]["game_param"][0]["EventParam"] = 0
            return super().evaluate(state, **request)
    rewards, state = attached(Provider()), player(1900)
    before, event = deepcopy(state), context(RESULT, charisma=30, points=3)
    assert rewards.preview(state, event) == rewards.evaluate(state, event)
    assert state == before
    rewards.attach_vote_tickets(Writer())
    assert rewards.preview(state, event).event_param == 3
    assert state == before


def test_provider_failures_fail_the_settlement_instead_of_dropping_tickets():
    class Broken(Provider):
        def evaluate(self, state, **request):
            raise ProtocolError("vote_expiry_state_invalid", "Stored vote expiry state is invalid", 503)

    class Foreign(Provider):
        def evaluate(self, state, **request):
            return {"item_id": FIRST, "count": 1, "reason": "issued"}
    for provider, failure in ((Broken(), ("vote_expiry_state_invalid", 503)),
                              (Foreign(), ("battle_event_provider_invalid", 503))):
        rewards = attached(provider)
        for action in (rewards.evaluate, rewards.preview):
            assert rejected(lambda: action(player(), context(RESULT, charisma=30))) == failure


@pytest.mark.parametrize("field,value", [("source", "battle"), ("source", None), ("source", ["result"]),
    ("quest_id", 0), ("quest_id", True), ("quest_id", 2 ** 31), ("quest_type", -1), ("quest_type", 1.0),
    ("charisma_per_run", -1), ("charisma_per_run", 2 ** 31), ("runs", 0), ("runs", True), ("event_points", -1),
    ("event_points", None), ("now", "2026-11-02"), ("now", None), ("rewrite_event_param", 1),
    ("rewrite_event_param", False)])
def test_contexts_require_a_known_source_and_native_values(field, value):
    assert rejected(lambda: BattleEventContext(**{**FIELDS, field: value})) == CONTEXT_INVALID


def test_contexts_tie_the_rewrite_rule_to_their_source_and_read_naive_clocks_as_jst():
    assert rejected(lambda: BattleEventContext(**{**FIELDS, "source": ENTRUST})) == CONTEXT_INVALID
    entrust = BattleEventContext(**{**FIELDS, "source": ENTRUST, "rewrite_event_param": False, "runs": 3})
    naive = BattleEventContext(**{**FIELDS, "now": NOW.replace(tzinfo=None)})
    assert naive.now == NOW and naive.now.utcoffset() == NOW.utcoffset() and entrust.runs == 3
    with pytest.raises(FrozenInstanceError):
        naive.runs = 2
    assert rejected(lambda: BattleEventRewards().evaluate(player(), FIELDS)) == CONTEXT_INVALID


@pytest.mark.parametrize("items,event_param,changed,receipt", [
    ({FIRST: 0}, None, False, {}), ({FIRST: -1}, None, False, {}), ({FIRST: True}, None, False, {}),
    ({FIRST: 2 ** 31}, None, False, {}), ({"": 1}, None, False, {}), ({1: 1}, None, False, {}),
    ([(FIRST, 1)], None, False, {}), ({}, EVENT_PARAM_LIMIT + 1, True, {}), ({}, -1, True, {}),
    ({}, True, True, {}), ({}, None, True, {}), ({}, 5, 1, {}), ({}, None, False, []),
    ({}, None, False, {"value": float("nan")}), ({}, None, False, {"value": object()})])
def test_grants_hold_positive_increments_a_capped_counter_and_a_json_receipt(items, event_param, changed, receipt):
    assert rejected(lambda: BattleEventGrant(items, event_param, changed, receipt)) == (
        "battle_event_grant_invalid", 503)


def test_grants_keep_private_copies_of_their_increments_and_receipt():
    items, receipt = {FIRST: 2}, {"context": {"runs": (1, 2)}}
    grant = BattleEventGrant(items, EVENT_PARAM_LIMIT, True, receipt)
    items[FIRST], receipt["context"]["runs"] = 5, None
    assert (dict(grant.items), grant.receipt) == ({FIRST: 2}, {"context": {"runs": [1, 2]}})


@pytest.mark.parametrize("provider,error", [(None, None), (None, ""), (None, "Source Invalid"), (None, 503),
    (None, "a" * 129), (Provider(), "vote_quest_ticket_source_invalid"), (object(), None),
    (SimpleNamespace(evaluate=print, status=print, content_version=None), None),
    (SimpleNamespace(evaluate=None, status=print, content_version="a" * 64), None)])
def test_attaching_requires_a_provider_or_its_error_code(provider, error):
    rewards = BattleEventRewards()
    with pytest.raises(ValueError):
        rewards.attach_vote_tickets(provider, error)
    assert rewards.status()["vote_tickets"]["error"] == NOT_ATTACHED


def test_status_is_json_and_versions_follow_only_the_provider_and_rules():
    rewards = BattleEventRewards()
    status = rewards.status(NOW.replace(tzinfo=None))
    assert json.loads(json.dumps(status)) == status
    assert set(status) == {"schema", "content_version", "providers", "vote_tickets", "event_param", "rules", "now",
                           "native_tested"}
    assert ([status[key] for key in ("schema", "providers", "now", "native_tested")]
            == [1, ["vote_tickets", "event_param"], "2026-11-02T12:00:00+09:00", False])
    assert status["event_param"]["limit"] == EVENT_PARAM_LIMIT and status["content_version"] == rewards.content_version
    assert BattleEventRewards().content_version == rewards.content_version
    first, second = Provider(), Provider()
    second.content_version = "b" * 64
    versions = []
    for provider in (first, Provider(), second):
        rewards.attach_vote_tickets(provider)
        versions.append(rewards.content_version)
    assert versions[0] == versions[1] != versions[2]
    grant = rewards.evaluate(player(), context(RESULT, now=NOW.replace(microsecond=1)))
    assert grant.receipt["content_version"] == versions[2] and grant.receipt["schema"] == 1
    assert grant.receipt["context"] == {**FIELDS, "now": "2026-11-02T12:00:00+09:00"}
    assert rejected(lambda: rewards.status("2026-11-02")) == ("battle_event_clock_invalid", 503)
    assert status["rules"]["ticket_clock"] == "result_attempt_start_entrust_execution"


def test_versions_cover_the_schema_and_the_event_param_rules(monkeypatch):
    rewards = BattleEventRewards()
    versions = [rewards.content_version]
    monkeypatch.setattr(battle_events, "EVENT_PARAM_RULES", {**battle_events.EVENT_PARAM_RULES, "limit": 0})
    versions.append(rewards.content_version)
    monkeypatch.setattr(battle_events, "SCHEMA", battle_events.SCHEMA + 1)
    versions.append(rewards.content_version)
    assert len(set(versions)) == len(versions)


def test_versions_ignore_the_status_rules(monkeypatch):
    rewards = BattleEventRewards()
    version = rewards.content_version
    monkeypatch.setattr(battle_events, "RULES", {**battle_events.RULES, "ticket_clock": "result_settlement"})
    assert rewards.content_version == version


@pytest.fixture(scope="module")
def real(bundle, vote_sources):
    services = isolated(bundle)
    shifted = build(vote_sources, VoteRules(revival_start=REVIVAL))
    return SimpleNamespace(services=services, initial=bundle.player_state.initialize(bundle.bootstrap.initial_state()),
                           shifted=quest_tickets(services, shifted, vote_sources),
                           default=quest_tickets(services, build(vote_sources), vote_sources))


def test_real_quest_tickets_count_runs_inside_the_shifted_ticket_window(real):
    rewards, before = attached(real.shifted), deepcopy(real.initial)
    for source in (RESULT, ENTRUST):
        grant = rewards.evaluate(real.initial, context(source, charisma=25, runs=3, now=TODAY))
        assert dict(grant.items) == {FIRST: 3}
        assert grant.receipt["vote_tickets"] == {"error": None, "item_id": FIRST, "count": 3, "reason": "issued"}
    free = rewards.evaluate(real.initial, context(RESULT, charisma=0, now=TODAY))
    assert dict(free.items) == {}
    assert free.receipt["vote_tickets"] == {"error": None, "item_id": FIRST, "count": 0, "reason": "zero_charisma"}
    outside = rewards.evaluate(real.initial, context(RESULT, charisma=25, runs=3, now=BEFORE_REVIVAL))
    unshifted = attached(real.default).evaluate(real.initial, context(ENTRUST, charisma=25, runs=3, now=TODAY))
    for grant in (outside, unshifted):
        assert dict(grant.items) == {}
        assert grant.receipt["vote_tickets"] == {"error": None, "item_id": None, "count": 0, "reason": "no_window"}
    assert real.initial == before
    status = rewards.status(TODAY)
    assert json.loads(json.dumps(status)) == status
    assert status["vote_tickets"]["status"]["current_group"] == {"group_id": 1, "item_id": FIRST}
    assert rewards.content_version != attached(real.default).content_version


def test_real_closed_ticket_identity_lets_the_battle_settle_without_tickets(real):
    state = expired(SimpleNamespace(default=real.default), real.services, real.initial, "settled")
    grant = attached(real.shifted).evaluate(state, context(RESULT, charisma=30, now=TODAY))
    assert dict(grant.items) == {}
    assert grant.receipt["vote_tickets"] == {"error": None, "item_id": FIRST, "count": 0,
                                             "reason": "vote_ticket_closed"}
    assert (grant.event_param, grant.event_param_changed) == (0, False)


def test_real_capped_credit_reports_the_ticket_overflow_by_provider(real):
    possessions = real.services.possessions
    drop_stack = possessions.stack(DROP)
    state = possessions.credit(real.initial, {FIRST: 998, DROP: drop_stack - 1})
    rewards = attached(real.shifted)
    grant = rewards.evaluate(state, context(RESULT, charisma=45, now=TODAY))
    credited, discarded = possessions.credit_capped(state, {DROP: 5, **grant.items})
    assert discarded == {FIRST: 2, DROP: 4}
    assert rewards.receipt(grant, discarded)["discarded"] == {"vote_tickets": {FIRST: 2}}
    assert real.shifted.expiry.initialize(credited) == credited

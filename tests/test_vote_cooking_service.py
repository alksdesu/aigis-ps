from copy import deepcopy
from datetime import timedelta
import json

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.vote_cooking_rules import VoteCookingRules, load_vote_cooking_rules
from aigis_ps.vote_cooking_service import VoteCookingService
from aigis_ps.vote_state import read_vote_history, record_vote
from test_quest_service import bundle
from test_vote_catalog import vote_sources, build
from test_vote_transactions import catalog, make_case, NOW


@pytest.fixture
def factory(make_case):
    def create(rules=None):
        case = make_case()
        assert case.bundle.vote_cooking is not None, case.bundle.vote_cooking_error
        case.cooking = VoteCookingService(case.catalog, case.bundle.vote_cooking.recipes, case.presents, rules)
        return case
    return create


def progress(case, state, card, count, *, group=None, now=NOW, source=None):
    group = group or case.group
    old = next((row["count"] for row in read_vote_history(state)
                if row["itemId"] == group.item_id and row["cardId"] == card), 0)
    after = record_vote(state, item_id=group.item_id, group_id=group.group_id, card_id=card,
                        count=old + count, definition=case.transactions.rewards.group_definition(group))
    return (source or case.cooking).record_vote(state, after, group, now)


def entry(case, state, group=None):
    return state["vote_cooking"]["groups"][(group or case.group).item_id]


def test_only_new_votes_count_and_ties_select_lowest_card(factory):
    case = factory()
    cards = sorted(candidate.card_id for candidate in case.catalog.candidates if candidate.group_id == 1)[:3]
    state = case.store.profile().state
    old = record_vote(state, item_id=case.group.item_id, group_id=1, card_id=cards[2], count=100,
                      definition=case.transactions.rewards.group_definition(case.group))
    assert case.cooking.initialize(old) == old and "vote_cooking" not in old
    assert case.cooking.settle(old, case.group.result_start) == (old, [])
    tracked = progress(case, old, cards[1], 3)
    tracked = progress(case, tracked, cards[0], 3)
    assert entry(case, tracked)["baseline"] == {str(cards[2]): 100}
    before, grants = case.cooking.settle(tracked, case.group.result_start - timedelta(microseconds=1))
    assert before == tracked and grants == []
    delivered, grants = case.cooking.settle(tracked, case.group.result_start)
    assert entry(case, delivered)["winner"] == cards[0]
    assert entry(case, delivered)["winning_votes"] == 3
    assert grants[0]["card_id"] == cards[0] and grants[0]["receive_text_id"] == 5028
    assert case.presents.rows(delivered) == [{"PresentID": 1, "Type": 21, "Content": cards[0],
                                            "Description": case.cooking.recipes.description}]
    assert delivered["tables"]["game_param"] == old["tables"]["game_param"]
    assert case.cooking.settle(delivered, case.group.result_start) == (delivered, [])
    case.cooking.validate_receipt(delivered, grants)


def test_home_cooking_zero_is_valid_and_claimed_mail_keeps_proof(factory):
    case = factory()
    card = next(row.card_id for row in case.catalog.candidates if row.group_id == 1
                and case.cooking.recipes.definition(row.card_id).cooking_kind == 0)
    state = progress(case, case.store.profile().state, card, 1)
    delivered, grants = case.cooking.settle(state, case.group.result_start)
    claimed = deepcopy(delivered)
    claimed["tables"]["presents"] = []
    assert case.cooking.initialize(claimed) == claimed
    case.cooking.validate_receipt(claimed, grants)
    assert case.cooking.settle(claimed, case.group.result_start + timedelta(days=1)) == (claimed, [])


def test_capacity_defers_whole_group_then_delivers_copies_once(factory):
    case = factory(VoteCookingRules(copies=2))
    state = progress(case, case.store.profile().state, case.candidate.card_id, 1)
    full, _ = case.presents.enqueue(state, [{"kind": "gold", "amount": 1, "description": "fixture"}] * 499)
    pending, grants = case.cooking.settle(full, case.group.result_start)
    assert not grants and entry(case, pending)["reason"] == "present_capacity"
    assert pending["tables"] == full["tables"] and pending["last_present_id"] == full["last_present_id"]
    freed = deepcopy(pending)
    freed["tables"]["presents"].pop()
    delivered, grants = case.cooking.settle(freed, case.group.result_start + timedelta(seconds=1))
    assert len(case.presents.rows(delivered)) == 500
    assert grants[0]["count"] == 2 and grants[0]["mail_ids"] == [500, 501]
    assert case.cooking.settle(delivered, case.group.result_start + timedelta(days=1)) == (delivered, [])


def test_disabled_rules_do_not_erase_earned_votes_or_count_disabled_votes(factory):
    case = factory()
    state = case.store.profile().state
    disabled = VoteCookingService(case.catalog, case.cooking.recipes, case.presents, VoteCookingRules(enabled=False))
    first = progress(case, state, case.candidate.card_id, 10, source=disabled)
    assert "vote_cooking" not in first
    tracked = progress(case, first, case.candidate.card_id, 2)
    ignored = progress(case, tracked, case.candidate.card_id, 20, source=disabled)
    assert entry(case, ignored)["baseline"][str(case.candidate.card_id)] == 30
    settled, grants = disabled.settle(ignored, case.group.result_start)
    assert grants and entry(case, settled)["winning_votes"] == 2


def test_minimum_and_no_eligible_candidate_freeze_without_mail(factory):
    case = factory(VoteCookingRules(minimum_votes=3))
    state = progress(case, case.store.profile().state, case.candidate.card_id, 2)
    after, grants = case.cooking.settle(state, case.group.result_start)
    assert grants == [] and entry(case, after)["status"] == "ineligible"
    assert entry(case, after)["winner"] is None
    assert case.cooking.settle(after, case.group.result_start + timedelta(days=1)) == (after, [])


def test_groups_deliver_separately_including_same_card_in_final(factory):
    case = factory()
    final = case.catalog.groups[-1]
    state = progress(case, case.store.profile().state, case.candidate.card_id, 2)
    state = progress(case, state, case.candidate.card_id, 1, group=final, now=final.vote_start)
    first, grants = case.cooking.settle(state, final.result_start)
    assert [grant["group_id"] for grant in grants] == [1]
    assert entry(case, first, final)["status"] == "pending"
    second, grants = case.cooking.settle(first, final.result_start)
    assert [grant["group_id"] for grant in grants] == [4]
    assert len(case.presents.rows(second)) == 2
    assert case.cooking.settle(second, final.result_start) == (second, [])


def test_window_and_configuration_changes_preserve_frozen_entitlement(factory, vote_sources):
    from aigis_ps.vote_rules import VoteRules
    case = factory()
    state = progress(case, case.store.profile().state, case.candidate.card_id, 1)
    moved = build(vote_sources, VoteRules(revival_start="2026-12-01T15:00:00+09:00"))
    changed = VoteCookingService(moved, case.cooking.recipes, case.presents,
                                 VoteCookingRules(enabled=False, minimum_votes=999, copies=10))
    after, grants = changed.settle(state, case.group.result_start)
    assert grants[0]["count"] == 1 and entry(case, after)["winning_votes"] == 1
    assert changed.settle(after, moved.groups[0].result_start) == (after, [])


@pytest.mark.parametrize("corruption", ("baseline", "counts", "time", "status", "winner", "source", "copies", "extra",
                                       "changed_copies", "minimum_votes", "event", "description", "text_id", "catalog"))
def test_invalid_ledger_is_rejected_without_mutation(factory, corruption):
    case = factory()
    state = progress(case, case.store.profile().state, case.candidate.card_id, 1)
    damaged = deepcopy(state)
    row = entry(case, damaged)
    if corruption == "baseline": row["baseline"][str(case.candidate.card_id)] = 2
    if corruption == "counts": row["counts"][str(case.candidate.card_id)] = 2
    if corruption == "time": row["last_time"] = "2026-11-01"
    if corruption == "status": row["status"] = "delivered"
    if corruption == "winner": row["winner"] = case.candidate.card_id
    if corruption == "source": row["source"] = "unknown"
    if corruption == "copies": row["copies"] = True
    if corruption == "extra": row["reward"] = 999
    if corruption == "changed_copies": row["copies"] = 10
    if corruption == "minimum_votes": row["minimum_votes"] = 999
    if corruption == "event": row["event_id"] += 1
    if corruption == "description": row["description"] = "changed"
    if corruption == "text_id": row["receive_text_id"] += 1
    if corruption == "catalog": row["source_catalog"] = "a" * 64
    expected = deepcopy(damaged)
    with pytest.raises(ProtocolError): case.cooking.settle(damaged, case.group.result_start)
    assert damaged == expected


@pytest.mark.parametrize("change", ("reuse", "wrong_card", "quantity", "text", "missing", "unknown"))
def test_grants_and_mail_receipts_are_checked(factory, change):
    case = factory()
    state = progress(case, case.store.profile().state, case.candidate.card_id, 1)
    after, grants = case.cooking.settle(state, case.group.result_start)
    damaged = deepcopy(grants)
    if change == "reuse": damaged += deepcopy(damaged)
    if change == "wrong_card": damaged[0]["card_id"] += 1
    if change == "quantity": damaged[0]["count"] = True
    if change == "text": damaged[0]["receive_text_id"] += 1
    if change == "missing": damaged[0]["mail_ids"] = []
    if change == "unknown": damaged[0]["reward"] = 1
    with pytest.raises(ProtocolError): case.cooking.validate_receipt(after, damaged)


def test_clock_reversal_and_late_vote_do_not_change_state(factory):
    case = factory()
    state = progress(case, case.store.profile().state, case.candidate.card_id, 1)
    before = deepcopy(state)
    with pytest.raises(ProtocolError, match="clock precedes"):
        case.cooking.settle(state, NOW - timedelta(seconds=1))
    with pytest.raises(ProtocolError, match="original open rules"):
        progress(case, state, case.candidate.card_id, 1, now=case.group.vote_end)
    assert state == before


@pytest.mark.parametrize("value", ({"enabled": 1}, {"minimum_votes": True}, {"minimum_votes": 0},
                                  {"minimum_votes": 1000}, {"copies": 0}, {"copies": 11}))
def test_rules_reject_unsafe_or_ambiguous_values(value):
    with pytest.raises(ValueError): VoteCookingRules(**value)


def test_rule_file_is_strict_and_preserves_configurable_values(tmp_path):
    path = tmp_path / "vote-cooking.json"
    path.write_text(json.dumps({"enabled": False, "minimum_votes": 10, "copies": 2}), encoding="utf-8-sig")
    assert load_vote_cooking_rules(path) == VoteCookingRules(False, 10, 2)
    for malformed in ('{"copies":1,"copies":2}', '{"unknown":1}', '[]'):
        path.write_text(malformed, encoding="utf-8")
        with pytest.raises(ValueError): load_vote_cooking_rules(path)

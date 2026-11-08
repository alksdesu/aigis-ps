from copy import copy, deepcopy
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_catalog import ITEM_RESOURCE_NAME
from aigis_ps.present_items import PresentItemCatalog
from aigis_ps.vote_rewards import VoteRewardService, group_total
from aigis_ps.vote_state import read_vote_history, record_vote
from test_quest_service import bundle
from test_vote_catalog import vote_sources, build, resolved_rules


FIXED = [5, 10, 20, 30, 40, 50, 75, 100, 125, 150, 175, 200]


@pytest.fixture(scope="module")
def catalog(vote_sources):
    source = build(vote_sources)
    return build(vote_sources, resolved_rules(source, revival_start="2026-11-01T15:00:00+09:00"))


@pytest.fixture
def case(bundle, catalog):
    presents = copy(bundle.presents)
    presents._templates = dict(presents._templates)
    helper = VoteRewardService(catalog, presents)
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    return SimpleNamespace(catalog=catalog, presents=presents, helper=helper, state=state)


def proposal(case, state, count, *, group_id=1, index=0):
    group = case.catalog.groups_by_id[group_id]
    candidate = [item for item in case.catalog.candidates if item.group_id == group_id][index]
    current = next((row["count"] for row in read_vote_history(state)
                    if row["itemId"] == group.item_id and row["cardId"] == candidate.card_id), 0)
    after = record_vote(state, item_id=group.item_id, group_id=group_id, card_id=candidate.card_id,
                        count=current + count, definition=case.helper.group_definition(group))
    return after, group


def vote(case, state, count, **kwargs):
    after, group = proposal(case, state, count, **kwargs)
    return case.helper.apply_vote(state, after, group)


def item_entry(case, state, group_id=1):
    return state["vote_rewards"]["items"][case.catalog.groups_by_id[group_id].item_id]


def assault(case, state, group_id=1):
    return case.helper.assault_response(state, None if group_id is None else case.catalog.groups_by_id[group_id])


def items_without_emblems(presents):
    return PresentItemCatalog(presents.possessions, presents.players.read_asset(ITEM_RESOURCE_NAME))


def test_empty_initialization_does_not_invent_rewards_or_assault(case):
    source = deepcopy(case.state)
    assert case.helper.initialize(source) == source
    assert "vote_rewards" not in source
    assert assault(case, source) == Container("ASSAULT", (Column("Remain", "I", (0,)), Column("Next", "I", (15,))))
    assert assault(case, source, None).children[1].values == (0,)


def test_legacy_group_baseline_has_no_backfill_and_carries_partial_assault_progress(case):
    source, group = proposal(case, case.state, 100)
    before = deepcopy(source)
    initialized = case.helper.initialize(source)
    entry = item_entry(case, initialized)
    assert entry == {"group_id": 1, "event_id": None, "definition": None,
                     "baseline_count": 100, "count": 100, "deliveries": {}}
    assert case.helper.initialize(initialized) == initialized
    updated, awards, ids = vote(case, initialized, 5)
    assert awards == ids == []
    assert assault(case, updated).children == (Column("Remain", "I", (1,)), Column("Next", "I", (15,)))
    assert updated["tables"]["presents"] == before["tables"]["presents"]
    assert source == before


def test_fixed_and_repeat_thresholds_are_delivered_once_after_base(case):
    before = deepcopy(case.state)
    first, awards, ids = vote(case, case.state, 299)
    assert [row["Count"] for row in awards] == FIXED
    assert len(ids) == len(FIXED) and all(row["Repeat"] == 0 for row in awards)
    assert sorted(map(int, item_entry(case, first)["deliveries"])) == FIXED
    assert first["player_state"] == before["player_state"]
    assert first["tables"]["possessions"] == before["tables"]["possessions"]
    second, awards2, ids2 = vote(case, first, 1)
    assert [row["Count"] for row in awards2] == [300]
    third, awards3, ids3 = vote(case, second, 250)
    assert [row["Count"] for row in awards3] == [400, 500]
    assert len(set(ids + ids2 + ids3)) == 15
    assert group_total(third, case.catalog.groups[0].item_id) == 550
    assert len(item_entry(case, third)["deliveries"]) == 15
    case.helper.validate_delivery(third, case.catalog.groups[0].item_id, awards3, ids3)
    assert case.state == before


def test_group_rewards_sum_candidates_but_do_not_mix_other_ticket_groups(case):
    first, _, _ = vote(case, case.state, 4)
    second, awards, ids = vote(case, first, 1, index=1)
    assert [row["Count"] for row in awards] == [5]
    third, awards2, ids2 = vote(case, second, 5, group_id=2)
    assert [row["GroupID"] for row in awards2] == [2]
    assert ids != ids2 and group_total(third, case.catalog.groups[0].item_id) == 5
    assert group_total(third, case.catalog.groups[1].item_id) == 5
    assert len(third["vote_rewards"]["items"]) == 2


def test_same_transition_is_deterministic_but_already_applied_state_cannot_reaward(case):
    after, group = proposal(case, case.state, 15)
    first = case.helper.apply_vote(case.state, after, group)
    assert case.helper.apply_vote(case.state, after, group) == first
    with pytest.raises(ProtocolError) as caught:
        case.helper.apply_vote(first[0], first[0], group)
    assert caught.value.code == "vote_reward_transition_invalid"


def test_mail_claim_matches_display_and_keeps_permanent_receipt_proof(case, bundle):
    updated, awards, ids = vote(case, case.state, 200)
    claimed, units, crystals = case.presents.claim(updated, ids, acquisition_day=739921)
    assert crystals == 0 and not claimed["tables"]["presents"]
    unit_awards = [row for row in awards if row["CardID"]]
    assert [unit["CardID"] for unit in units] == [row["CardID"] for row in unit_awards]
    for unit, row in zip(units, unit_awards):
        assert unit["ClassChangeCount"] == row["ClassChange"]
        assert unit["SkillLevel"] == row["SkillLevel"] == 1
        assert unit["Love"] == row["Likable"] == 0
        assert unit["CostModMixValue"] == row["Cost"] == 0
    item_counts = {row["ItemID"]: row["Count"] for row in claimed["tables"]["possessions"]}
    assert item_counts["gift_0001_quartz"] == 5
    case.helper.validate_delivery(claimed, case.catalog.groups[0].item_id, awards, ids)
    assert claimed["vote_rewards"] == updated["vote_rewards"]


def test_assault_uses_each_group_floor_delta_and_shared_remaining_cap(case):
    first, _, _ = vote(case, case.state, 14)
    second, _, _ = vote(case, first, 14, group_id=2)
    assert assault(case, second).children[0].values == (0,)
    third, _, _ = vote(case, second, 1, index=1)
    fourth, _, _ = vote(case, third, 1, group_id=2)
    assert assault(case, fourth).children == (Column("Remain", "I", (2,)), Column("Next", "I", (15,)))
    capped, _, _ = vote(case, fourth, 15000)
    entitlement = capped["vote_rewards"]["assault"]["16"]
    assert entitlement["earned"] == 999 and entitlement["discarded"] == 3 and entitlement["spent"] == 0
    assert assault(case, capped, None).children == (Column("Remain", "I", (999,)), Column("Next", "I", (0,)))


def test_mailbox_capacity_rolls_back_the_entire_threshold_batch(case):
    filled, _ = case.presents.enqueue(case.state, [{"kind": "gold", "amount": 1, "description": "fixture"}] * 499)
    after, group = proposal(case, filled, 10)
    before, proposed = deepcopy(filled), deepcopy(after)
    with pytest.raises(ProtocolError) as caught:
        case.helper.apply_vote(filled, after, group)
    assert caught.value.code == "present_capacity"
    assert filled == before and after == proposed


@pytest.mark.parametrize("failure", ["partial", "money", "identity", "content"])
def test_mail_provider_cannot_partially_deliver_or_change_unrelated_state(case, monkeypatch, failure):
    original = case.presents.enqueue_groups
    def broken(state, rewards):
        updated, groups = original(state, rewards)
        if failure == "partial":
            groups.pop()
        elif failure == "money":
            updated["tables"]["game_param"][0]["Money"] += 1
        elif failure == "identity":
            groups[0][0]["PresentID"] += 100
        else:
            groups[0][0]["Content"] += 1
        return updated, groups
    monkeypatch.setattr(case.presents, "enqueue_groups", broken)
    before = deepcopy(case.state)
    with pytest.raises(ProtocolError) as caught:
        vote(case, case.state, 10)
    assert caught.value.code == "vote_reward_delivery_invalid"
    assert case.state == before


@pytest.mark.parametrize("alteration", ["two_candidates", "other_group", "wrong_definition", "reward_history"])
def test_only_one_candidate_increment_can_feed_the_reward_transaction(case, alteration):
    before, _, _ = vote(case, case.state, 4)
    after, group = proposal(case, before, 1)
    if alteration == "two_candidates":
        after, _ = proposal(case, after, 1, index=1)
    elif alteration == "other_group":
        group = case.catalog.groups[1]
    elif alteration == "wrong_definition":
        changed = after["tables"]["vote_history"][0]
        after["vote_state"]["entries"][str(changed["key"])]["definition"] = "b" * 64
    else:
        after["vote_rewards"]["items"][group.item_id]["count"] += 1
    saved = deepcopy(before), deepcopy(after)
    with pytest.raises(ProtocolError):
        case.helper.apply_vote(before, after, group)
    assert (before, after) == saved


@pytest.mark.parametrize("alteration", ["unknown_field", "count", "group", "baseline", "mail_reuse", "threshold",
                                        "assault_earned", "assault_spent", "assault_interval", "missing_event"])
def test_corrupt_permanent_ledger_is_rejected_without_reset(case, alteration):
    updated, _, _ = vote(case, case.state, 30)
    entry = item_entry(case, updated)
    event = updated["vote_rewards"]["assault"]["16"]
    if alteration == "unknown_field":
        updated["vote_rewards"]["extra"] = 0
    elif alteration == "count":
        entry["count"] += 1
    elif alteration == "group":
        entry["group_id"] = 3
    elif alteration == "baseline":
        entry["baseline_count"] = 40
    elif alteration == "mail_reuse":
        entry["deliveries"]["10"]["mail_ids"] = entry["deliveries"]["5"]["mail_ids"][:]
    elif alteration == "threshold":
        entry["deliveries"]["31"] = entry["deliveries"].pop("30")
    elif alteration == "assault_earned":
        event["earned"] += 1
    elif alteration == "assault_spent":
        event["spent"] = 1
    elif alteration == "assault_interval":
        event["earned_votes"] = 0
    else:
        updated["vote_rewards"]["assault"] = {}
    before = deepcopy(updated)
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(updated)
    assert caught.value.code == "vote_rewards_state_invalid"
    assert updated == before


@pytest.mark.parametrize("alteration", ["wrong_item", "wrong_group", "wrong_count", "wrong_amount", "other_mail", "reverse", "orphan_mail"])
def test_receipt_cannot_reassign_rewards_or_mail_from_another_source(case, alteration):
    first, awards, ids = vote(case, case.state, 10)
    second, _, other_ids = vote(case, first, 10, group_id=2)
    item = case.catalog.groups[0].item_id
    selected, mail = deepcopy(awards), ids[:]
    if alteration == "wrong_item":
        item = case.catalog.groups[1].item_id
    elif alteration == "wrong_group":
        selected[0]["GroupID"] = 2
    elif alteration == "wrong_count":
        selected[0]["Count"] = 7
    elif alteration == "wrong_amount":
        selected[0]["Amount"] += 1
    elif alteration == "other_mail":
        mail = other_ids
    elif alteration == "reverse":
        selected.reverse()
        mail.reverse()
    else:
        selected = []
    for verify_current in (True, False):
        with pytest.raises(ProtocolError) as caught:
            case.helper.validate_delivery(second, item, selected, mail, verify_current=verify_current)
        assert caught.value.code == "vote_reward_receipt_invalid"
    case.helper.validate_delivery(second, item, [], [])


def test_window_and_enable_changes_preserve_semantics_but_reward_changes_block(case, vote_sources):
    state, awards, ids = vote(case, case.state, 15)
    original = build(vote_sources)
    moved_catalog = build(vote_sources, resolved_rules(original, enabled=False,
        revival_start="2027-01-01T15:00:00+09:00"))
    moved = VoteRewardService(moved_catalog, case.presents)
    assert moved.group_definition(moved_catalog.groups[0]) == case.helper.group_definition(case.catalog.groups[0])
    assert moved.initialize(state) == state
    moved.validate_delivery(state, moved_catalog.groups[0].item_id, awards, ids)
    changed_catalog = build(vote_sources, resolved_rules(original, skill_fairy_template_id=840,
        revival_start="2026-11-01T15:00:00+09:00"))
    changed = VoteRewardService(changed_catalog, case.presents)
    initialized = changed.initialize(state)
    changed.validate_delivery(initialized, changed_catalog.groups[0].item_id, awards, ids, verify_current=False)
    with pytest.raises(ProtocolError) as stale:
        changed.validate_delivery(initialized, changed_catalog.groups[0].item_id, awards, ids)
    assert stale.value.code == "vote_reward_receipt_invalid"
    candidate = next(row for row in changed_catalog.candidates if row.group_id == 1)
    after = deepcopy(initialized)
    row = next(row for row in after["tables"]["vote_history"] if row["cardId"] == candidate.card_id)
    row["count"] += 1
    after["vote_state"]["entries"][str(row["key"])].update(count=row["count"], definition=changed.group_definition(changed_catalog.groups[0]))
    with pytest.raises(ProtocolError) as caught:
        changed.apply_vote(initialized, after, changed_catalog.groups[0])
    assert caught.value.code == "vote_reward_definition_changed"


def test_large_jump_is_bounded_before_allocating_millions_of_rewards(case):
    before = deepcopy(case.state)
    with pytest.raises(ProtocolError) as caught:
        vote(case, case.state, 2**31 - 1)
    assert caught.value.code == "vote_reward_capacity"
    assert case.state == before


def test_removed_delivered_threshold_cannot_be_reearned(case):
    state, _, _ = vote(case, case.state, 10)
    item_entry(case, state)["deliveries"].pop("5")
    with pytest.raises(ProtocolError) as caught:
        vote(case, state, 1)
    assert caught.value.code == "vote_rewards_state_invalid"


def test_changed_initializer_source_invalidates_reward_dependency(case, monkeypatch):
    players = copy(case.presents.players)
    case.presents.players = players
    current = players._initialization()
    changed = copy(current)
    changed.content_version = "f" * 64
    monkeypatch.setattr(players, "_initialization", lambda: changed)
    with pytest.raises(ProtocolError) as caught:
        case.helper.reward_row(case.catalog.rewards[0])
    assert caught.value.code == "vote_rewards_dependencies_changed"


def test_replayable_versions_follow_every_version_the_item_catalog_replays(case):
    items = case.presents.item_catalog
    versions = case.helper.replayable_content_versions()
    assert case.helper.content_version_for(items.content_version) == case.helper.content_version
    assert versions == {case.helper.content_version_for(version) for version in items.replayable_content_versions()}


def test_rewards_committed_before_emblem_mail_attached_keep_a_replayable_version(case):
    presents = copy(case.presents)
    presents.item_catalog = items_without_emblems(case.presents)
    earlier = VoteRewardService(case.catalog, presents)
    assert case.presents.item_catalog.emblem_item_ids and earlier.content_version != case.helper.content_version
    assert earlier.replayable_content_versions() == {earlier.content_version}
    assert earlier.content_version in case.helper.replayable_content_versions()


@pytest.mark.parametrize("field,value", [("Count", 99), ("GroupID", 2), ("Repeat", 100),
    ("Amount", True), ("SkillLevel", 0), ("ItemID", "wrong"), ("CardID", -1), ("Extra", 0)])
def test_frozen_reward_rows_require_complete_native_semantics(case, field, value):
    state, _, _ = vote(case, case.state, 5)
    item_entry(case, state)["deliveries"]["5"]["award_row"][field] = value
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(state)
    assert caught.value.code == "vote_rewards_state_invalid"
    assert state == before


@pytest.mark.parametrize("item", [None, [], "", "x" * 257, "\x00"])
def test_invalid_receipt_item_is_rejected_even_without_awards(case, item):
    with pytest.raises(ProtocolError) as caught:
        case.helper.validate_delivery(case.state, item, [], [], verify_current=False)
    assert caught.value.code == "vote_reward_receipt_invalid"


def test_frozen_group_semantics_cannot_be_replaced_in_place(case):
    group = case.catalog.groups[0]
    definition = case.helper.group_definition(group)
    with pytest.raises(TypeError):
        case.helper._group_definitions[group.group_id] = "b" * 64
    updated, awards, ids = vote(case, case.state, 5)
    assert item_entry(case, updated)["definition"] == definition
    case.helper.validate_delivery(updated, group.item_id, awards, ids)

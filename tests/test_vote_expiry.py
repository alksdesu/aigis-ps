from copy import copy, deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.presents import PRESENT_CAPACITY
from aigis_ps.vote_expiry import (ARCHIVED_KEY, CLOSED, DEPENDENCIES_CHANGED, DESCRIPTION, NOTIFICATION_KEY, PENDING,
                                  REOPEN_UNNEEDED, SETTLED, TICKET_CLOSED, VoteAssaultExpiryService,
                                  VoteExpiryNotifications, VoteExpiryService, _definition)
import aigis_ps.vote_expiry as expiry_module
from aigis_ps.vote_rewards import VoteRewardService
from aigis_ps.vote_rules import VoteRules
from aigis_ps.vote_state import record_vote
from test_present_items import item_table
from test_quest_service import bundle
from test_vote_catalog import vote_sources, build, rows
from test_vote_rewards import items_without_emblems


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
LATER = NOW + timedelta(hours=1)
ITEM = "item_vote_016001"
REAPPEARED = 2


@pytest.fixture(scope="module")
def catalogs(vote_sources):
    return SimpleNamespace(default=build(vote_sources), disabled=build(vote_sources, VoteRules(enabled=False)),
        revival=build(vote_sources, VoteRules(revival_start="2026-11-01T15:00:00+09:00")))


@pytest.fixture
def case(bundle, catalogs):
    possessions = copy(bundle.possessions)
    presents = copy(bundle.presents)
    presents.possessions = possessions
    catalog = catalogs.default
    return SimpleNamespace(catalog=catalog, possessions=possessions, presents=presents,
        helper=VoteExpiryService(catalog, presents, possessions),
        state=bundle.player_state.initialize(bundle.bootstrap.initial_state()))


def stock(case, state, **amounts):
    return case.possessions.credit(state, amounts)


def counts(state):
    return {row["ItemID"]: row["Count"] for row in state["tables"]["possessions"]}


def filled(case, state, amount=500):
    return case.presents.enqueue(state, [{"kind": "gold", "amount": 1, "description": "fixture"}] * amount)[0]


def pending(case):
    source = filled(case, stock(case, case.state, **{ITEM: 3}))
    result, report = case.helper.settle(source, NOW)
    assert report["pending"] == [{"item_id": ITEM, "reason": PRESENT_CAPACITY}]
    return result


def refuse_copies(monkeypatch):
    def copied(value):
        pytest.fail("Reading a journal must not copy the save")
    monkeypatch.setattr(expiry_module, "deepcopy", copied)


def test_expired_catalog_does_not_create_records_for_unparticipated_items(case):
    before = deepcopy(case.state)
    assert case.helper.initialize(case.state) == before
    assert case.helper.settle(case.state, NOW) == (before, {"grants": [], "pending": [], "closed": []})
    assert "vote_expiry" not in before
    assert case.helper.status()["gold_per_ticket"] == 50


def test_ledger_reads_the_validated_journal_without_copying_the_save(case, monkeypatch):
    waiting = pending(case)
    settled, _ = case.helper.settle(stock(case, case.state, **{ITEM: 2}), NOW)
    states = (case.state, waiting, settled)
    before = deepcopy(states)
    refuse_copies(monkeypatch)
    assert case.helper.ledger(case.state) == {}
    for state in (waiting, settled):
        assert case.helper.ledger(state) == case.helper._state(state)[2]["items"] == state["vote_expiry"]["items"]
    assert states == before


def test_expiry_debits_once_and_sends_gold_without_direct_balance_change(case):
    source = stock(case, case.state, **{ITEM: 7})
    source["tables"]["possessions"][0]["Enabled"] = 0
    before = deepcopy(source)
    result, report = case.helper.settle(source, NOW)
    grant = {"item_id": ITEM, "group_id": 1, "amount": 7, "rate": 50, "mail_ids": [1]}
    assert report == {"grants": [grant], "pending": [], "closed": []}
    assert result["tables"]["presents"] == [{"PresentID": 1, "Type": 2, "Content": 350, "Description": DESCRIPTION}]
    assert result["tables"]["possessions"] == [{"ItemID": ITEM, "Count": 0, "Enabled": 0}]
    assert result["tables"]["game_param"] == before["tables"]["game_param"]
    assert result["player_state"] == before["player_state"]
    assert source == before
    assert case.helper.settle(result, NOW + timedelta(days=1)) == (
        result, {"grants": [], "pending": [], "closed": []})
    record = result["vote_expiry"]["items"][ITEM]
    assert record["source_window_end"] == record["window_end"] == "2026-08-30T07:00:00+00:00"
    assert record["settled_at"] == record["observed_at"]


def test_gold_claim_increases_balance_and_keeps_expiry_proof(case):
    source = stock(case, case.state, **{ITEM: 2})
    converted, report = case.helper.settle(source, NOW)
    claimed, units, crystals = case.presents.claim(converted, report["grants"][0]["mail_ids"], acquisition_day=739921)
    assert units == [] and crystals == 0 and not claimed["tables"]["presents"]
    assert claimed["tables"]["game_param"][0]["Money"] == source["tables"]["game_param"][0]["Money"] + 100
    assert case.helper.initialize(claimed) == claimed
    assert case.helper.settle(claimed, NOW + timedelta(seconds=1))[1]["grants"] == []


@pytest.mark.parametrize("participation", ["zero_inventory", "native_history"])
def test_zero_stock_closes_only_a_participated_identity(case, participation):
    source = deepcopy(case.state)
    if participation == "zero_inventory":
        source["tables"]["possessions"].append({"ItemID": ITEM, "Count": 0})
    else:
        source = record_vote(source, item_id=ITEM, group_id=1, card_id=1, count=7, definition="a" * 64)
    result, report = case.helper.settle(source, NOW)
    assert report == {"grants": [], "pending": [], "closed": [ITEM]}
    assert set(result["vote_expiry"]["items"]) == {ITEM}
    record = result["vote_expiry"]["items"][ITEM]
    assert record["status"] == "closed" and record["amount"] == 0 and record["mail_ids"] == []
    assert result["tables"] == source["tables"]


def test_daily_and_external_event_ticket_types_are_untouched(case):
    source = stock(case, case.state, item_vote_008001=15, item_10000000=1)
    result, report = case.helper.settle(source, NOW)
    assert result == source and report == {"grants": [], "pending": [], "closed": []}


@pytest.mark.parametrize("offset,converted", [(-1, False), (0, True), (1, True)])
def test_revival_settles_at_the_exclusive_vote_end_not_dispersion_end(case, catalogs, offset, converted):
    helper = VoteExpiryService(catalogs.revival, case.presents, case.possessions)
    group = catalogs.revival.groups[0]
    source = stock(case, case.state, **{ITEM: 4})
    result, report = helper.settle(source, group.vote_end + timedelta(seconds=offset))
    assert bool(report["grants"]) is converted
    if converted:
        assert counts(result)[ITEM] == 0
        record = result["vote_expiry"]["items"][ITEM]
        assert record["source_window_end"] != record["window_end"]
    else:
        assert result == source
    assert helper.settle(source, group.dispersion_end)[0] == source


def test_all_groups_convert_independently_with_unique_mail_ids(case):
    source = stock(case, case.state, **{group.item_id: group.group_id for group in case.catalog.groups})
    result, report = case.helper.settle(source, NOW)
    assert [grant["amount"] for grant in report["grants"]] == [1, 2, 3, 4]
    assert [grant["mail_ids"] for grant in report["grants"]] == [[1], [2], [3], [4]]
    assert [row["Content"] for row in result["tables"]["presents"]] == [50, 100, 150, 200]
    assert all(value == 0 for value in counts(result).values())


def test_full_mailbox_freezes_pending_stock_without_debit_then_retries(case):
    result = pending(case)
    assert counts(result)[ITEM] == 3
    record = deepcopy(result["vote_expiry"]["items"][ITEM])
    assert record["status"] == "pending" and record["amount"] == 3 and record["mail_ids"] == []
    freed, _, _ = case.presents.claim(result, [1], acquisition_day=739921)
    settled, report = case.helper.settle(freed, NOW + timedelta(days=1))
    assert report["grants"][0]["mail_ids"] == [501]
    assert counts(settled)[ITEM] == 0 and len(settled["tables"]["presents"]) == 500
    assert settled["vote_expiry"]["items"][ITEM]["observed_at"] == record["observed_at"]


def test_one_group_capacity_failure_preserves_another_successful_conversion(case):
    second = case.catalog.groups[1].item_id
    source = filled(case, stock(case, case.state, **{ITEM: 1, second: 2}), amount=499)
    result, report = case.helper.settle(source, NOW)
    assert [grant["item_id"] for grant in report["grants"]] == [ITEM]
    assert report["pending"] == [{"item_id": second, "reason": PRESENT_CAPACITY}]
    assert counts(result)[ITEM] == 0 and counts(result)[second] == 2
    assert result["tables"]["presents"][-1]["Content"] == 50


def test_disabled_rules_skip_new_qualifications_but_retry_observed_pending(case, catalogs):
    disabled = VoteExpiryService(catalogs.disabled, case.presents, case.possessions)
    source = stock(case, case.state, **{ITEM: 2})
    assert disabled.settle(source, NOW)[0] == source
    waiting = pending(case)
    freed, _, _ = case.presents.claim(waiting, [1], acquisition_day=739921)
    result, report = disabled.settle(freed, NOW + timedelta(seconds=1))
    assert report["grants"][0]["amount"] == 3 and counts(result)[ITEM] == 0


def test_revival_shift_never_reopens_closed_identity_or_moves_frozen_pending(case, catalogs):
    source = stock(case, case.state, **{ITEM: 2})
    settled, _ = case.helper.settle(source, NOW)
    moved = VoteExpiryService(catalogs.revival, case.presents, case.possessions)
    assert moved.settle(settled, NOW + timedelta(days=20))[0] == settled
    waiting = pending(case)
    freed, _, _ = case.presents.claim(waiting, [1], acquisition_day=739921)
    result, report = moved.settle(freed, NOW + timedelta(seconds=1))
    assert report["grants"] and result["vote_expiry"]["items"][ITEM]["window_end"] == waiting["vote_expiry"]["items"][ITEM]["window_end"]


@pytest.mark.parametrize("initial_count", [0, 1])
def test_reappearing_ticket_after_close_requires_explicit_migration(case, initial_count):
    source = stock(case, case.state, **{ITEM: 1})
    if initial_count == 0:
        source = case.possessions.debit(source, {ITEM: 1})
    closed, _ = case.helper.settle(source, NOW)
    changed = stock(case, closed, **{ITEM: 1})
    before = deepcopy(changed)
    for action in (lambda: case.helper.initialize(changed), lambda: case.helper.settle(changed, NOW + timedelta(days=1))):
        with pytest.raises(ProtocolError) as caught:
            action()
        assert caught.value.code == "vote_expiry_already_closed"
    assert changed == before


@pytest.mark.parametrize("delta", [-1, 1])
def test_pending_stock_cannot_be_replaced_or_silently_requoted(case, delta):
    waiting = pending(case)
    changed = stock(case, waiting, **{ITEM: 1}) if delta == 1 else case.possessions.debit(waiting, {ITEM: 1})
    before = deepcopy(changed)
    with pytest.raises(ProtocolError) as caught:
        case.helper.settle(changed, NOW + timedelta(days=1))
    assert caught.value.code == "vote_expiry_inventory_changed"
    assert changed == before


@pytest.mark.parametrize("provider", ["inventory", "mail_shape", "mail_content", "mail_identity", "mail_side_effect"])
def test_provider_failure_never_commits_debit_or_forged_mail(case, monkeypatch, provider):
    source = stock(case, case.state, **{ITEM: 4})
    before = deepcopy(source)
    if provider == "inventory":
        original = case.possessions.debit
        def bad(state, cost):
            result = original(state, cost)
            result["tables"]["game_param"][0]["Money"] += 1
            return result
        monkeypatch.setattr(case.possessions, "debit", bad)
    else:
        original = case.presents.enqueue_groups
        def bad(state, rewards):
            result, groups = original(state, rewards)
            if provider == "mail_shape":
                groups = []
            elif provider == "mail_content":
                groups[0][0]["Content"] += 1
            elif provider == "mail_identity":
                groups[0][0]["PresentID"] += 1
            else:
                result["tables"]["game_param"][0]["Money"] += 1
            return result, groups
        monkeypatch.setattr(case.presents, "enqueue_groups", bad)
    with pytest.raises(ProtocolError) as caught:
        case.helper.settle(source, NOW)
    assert caught.value.code == ("vote_expiry_inventory_invalid" if provider == "inventory" else "vote_expiry_delivery_invalid")
    assert source == before


@pytest.mark.parametrize("field,value", [("amount", True), ("amount", -1), ("amount", 1000),
    ("rate", 51), ("group_id", 0), ("definition", "b" * 64), ("window_end", "2026-08-30T16:00:00+09:00"),
    ("observed_at", True), ("settled_at", None), ("reason", "unexpected"), ("mail_ids", [True]),
    ("mail_ids", [1, 2]), ("status", "unknown"), ("extra", 1)])
def test_invalid_permanent_record_is_rejected_without_rebuilding(case, field, value):
    state, _ = case.helper.settle(stock(case, case.state, **{ITEM: 1}), NOW)
    state["vote_expiry"]["items"][ITEM][field] = value
    before = deepcopy(state)
    for read in (case.helper.initialize, case.helper.ledger):
        with pytest.raises(ProtocolError) as caught:
            read(state)
        assert caught.value.code == "vote_expiry_state_invalid"
    assert state == before


def test_duplicate_mail_and_changed_remaining_mail_are_rejected(case):
    source = stock(case, case.state, **{group.item_id: 1 for group in case.catalog.groups[:2]})
    state, _ = case.helper.settle(source, NOW)
    duplicate = deepcopy(state)
    duplicate["vote_expiry"]["items"][case.catalog.groups[1].item_id]["mail_ids"] = [1]
    changed = deepcopy(state)
    changed["tables"]["presents"][0]["Content"] += 1
    for candidate in (duplicate, changed):
        with pytest.raises(ProtocolError) as caught:
            case.helper.initialize(candidate)
        assert caught.value.code == "vote_expiry_state_invalid"


def test_clock_before_first_observation_does_not_retry_pending(case):
    state = pending(case)
    with pytest.raises(ProtocolError) as caught:
        case.helper.settle(state, NOW - timedelta(seconds=1))
    assert caught.value.code == "vote_expiry_clock_reversed"


def test_changed_conversion_text_is_not_silently_treated_as_fifty_gold(case, vote_sources):
    changed = dict(vote_sources)
    texts = rows(changed, "SystemText.atb")
    found = 0
    for row in texts:
        if "1枚50ゴールドへと変換されます。" in row["Data_Text"]:
            row["Data_Text"] = row["Data_Text"].replace("1枚50ゴールドへと変換されます。", "1枚60ゴールドへと変換されます。")
            found += 1
    assert found >= 2
    changed["SystemText.atb"] = item_table(texts, (("Recode_Index", 1), ("Data_Text", 32)))
    catalog = build(changed)
    with pytest.raises(ProtocolError) as caught:
        VoteExpiryService(catalog, case.presents, case.possessions)
    assert caught.value.code == "vote_expiry_source_invalid"


def test_source_snapshot_change_invalidates_dependencies(case):
    case.possessions.asset_sha256 = "f" * 64
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(case.state)
    assert caught.value.code == "vote_expiry_dependencies_changed"


def test_readiness_does_not_build_the_possession_status(case, monkeypatch):
    monkeypatch.setattr(case.possessions, "status", lambda: pytest.fail("Readiness must not build the possession status"))
    assert case.helper.status()["ready"] is True
    assert case.helper.initialize(case.state) == case.state
    monkeypatch.setattr(case.possessions, "issues", {"item_definitions": "unavailable"})
    assert case.helper.status()["error"] == DEPENDENCIES_CHANGED


def test_assault_versions_follow_every_reward_version_a_receipt_may_carry(bundle):
    notices = bundle.vote_assault_notifier
    assault = notices.expiry
    reward_versions = assault.rewards.replayable_content_versions()
    assert assault.content_version_for(assault.rewards.content_version) == assault.content_version
    assert assault.replayable_content_versions() == set(map(assault.content_version_for, reward_versions))
    assert notices.content_version_for(assault.content_version) == notices.content_version
    assert notices.replayable_content_versions() == set(map(notices.content_version_for, assault.replayable_content_versions()))
    assert len(notices.replayable_content_versions()) == len(reward_versions)
    presents = copy(assault.presents)
    presents.item_catalog = items_without_emblems(assault.presents)
    earlier = VoteAssaultExpiryService(VoteRewardService(assault.catalog, presents))
    assert earlier.content_version != assault.content_version
    assert earlier.content_version in assault.replayable_content_versions()
    assert notices.content_version_for(earlier.content_version) in notices.replayable_content_versions()
    for version_for, code in ((assault.content_version_for, "vote_assault_expiry_dependencies_invalid"),
                              (notices.content_version_for, "vote_assault_notifications_unavailable")):
        with pytest.raises(ProtocolError) as caught:
            version_for(None)
        assert caught.value.code == code


def test_future_mail_identity_cannot_survive_claimed_mail_validation(case):
    state, _ = case.helper.settle(stock(case, case.state, **{ITEM: 1}), NOW)
    state["tables"]["presents"] = []
    state["vote_expiry"]["items"][ITEM]["mail_ids"] = [state["last_present_id"] + 1]
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(state)
    assert caught.value.code == "vote_expiry_state_invalid"


def test_non_vote_inventory_cannot_be_recast_as_an_expired_ticket(case):
    source = stock(case, case.state, **{ITEM: 1})
    state, _ = case.helper.settle(case.possessions.debit(source, {ITEM: 1}), NOW)
    item = "gift_0001_quartz"
    record = state["vote_expiry"]["items"].pop(ITEM)
    record["definition"] = _definition(item, record["group_id"], record["rate"], record["source_window_end"])
    state["vote_expiry"]["items"][item] = record
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(state)
    assert caught.value.code == "vote_expiry_state_invalid"


def test_frozen_old_ticket_pending_retries_without_creating_new_external_eligibility(case):
    waiting = pending(case)
    old_item = "item_vote_008001"
    next(row for row in waiting["tables"]["possessions"] if row["ItemID"] == ITEM)["ItemID"] = old_item
    record = waiting["vote_expiry"]["items"].pop(ITEM)
    record["source_window_end"] = record["window_end"] = "2020-01-01T07:00:00+00:00"
    record["definition"] = _definition(old_item, record["group_id"], record["rate"], record["source_window_end"])
    waiting["vote_expiry"]["items"][old_item] = record
    freed, _, _ = case.presents.claim(waiting, [1], acquisition_day=739921)
    result, report = case.helper.settle(freed, NOW + timedelta(seconds=1))
    assert report["grants"][0]["item_id"] == old_item
    assert counts(result)[old_item] == 0
    assert set(result["vote_expiry"]["items"]) == {old_item}


@pytest.mark.parametrize("status", ["pending", "settled", "closed"])
def test_any_expiry_marker_blocks_later_vote_with_daily_or_revived_tickets(case, catalogs, status):
    if status == "pending":
        state = pending(case)
    else:
        source = stock(case, case.state, **{ITEM: 1})
        if status == "closed":
            source = case.possessions.debit(source, {ITEM: 1})
        state, _ = case.helper.settle(source, NOW)
    moved = VoteExpiryService(catalogs.revival, case.presents, case.possessions)
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        moved.require_open(state, catalogs.revival.groups[0])
    assert (caught.value.code, caught.value.status) == (TICKET_CLOSED, 409)
    assert state == before


def test_ledger_statuses_keep_the_spelling_of_earlier_saves():
    assert (PENDING, SETTLED, CLOSED) == ("pending", "settled", "closed")


def test_open_eligibility_rejects_unknown_groups_and_preserves_unobserved_state(case):
    before = deepcopy(case.state)
    assert case.helper.require_open(case.state, case.catalog.groups[0]) is None
    with pytest.raises(ProtocolError) as caught:
        case.helper.require_open(case.state, None)
    assert caught.value.code == "vote_expiry_group_invalid"
    assert case.state == before


def stamp(moment):
    return int(moment.timestamp() * 1000)


def reappeared(case, status="settled"):
    source = stock(case, case.state, **{ITEM: 1})
    if status == "closed":
        source = case.possessions.debit(source, {ITEM: 1})
    closed, _ = case.helper.settle(source, NOW)
    return closed, stock(case, closed, **{ITEM: REAPPEARED})


@pytest.mark.parametrize("status", ["settled", "closed"])
def test_reopening_archives_the_completed_instance_and_converts_the_new_stock(case, status):
    closed, changed = reappeared(case, status)
    before = deepcopy(changed)
    record = closed["vote_expiry"]["items"][ITEM]
    reopened, report = case.helper.reopen_settled(changed, ITEM, LATER)
    assert changed == before and record["status"] == status
    assert ITEM not in case.helper.ledger(reopened)
    archived = {"item_id": ITEM, **record, "archived_at": stamp(LATER)}
    assert case.helper.archived(reopened) == [archived]
    assert report == {"archived": archived, "notice": None, "reappeared": []}
    assert case.helper.initialize(reopened) == reopened and counts(reopened)[ITEM] == REAPPEARED
    assert {key: value for key, value in reopened.items() if key != "vote_expiry"} == {
        key: value for key, value in changed.items() if key != "vote_expiry"}
    mail = reopened.get("last_present_id", 0) + 1
    converted, report = case.helper.settle(reopened, LATER)
    assert report == {"grants": [{"item_id": ITEM, "group_id": 1, "amount": REAPPEARED, "rate": 50, "mail_ids": [mail]}],
                      "pending": [], "closed": []}
    assert counts(converted)[ITEM] == 0 and case.helper.archived(converted) == case.helper.archived(reopened)
    current, archived = case.helper.settled_conversions(converted)
    assert current == {ITEM: {"gold": REAPPEARED * 50, "mail_ids": [mail], "settled_at": stamp(LATER)}}
    assert list(archived) == record["mail_ids"]
    assert case.helper.settle(converted, LATER + timedelta(days=1)) == (
        converted, {"grants": [], "pending": [], "closed": []})


def test_reopening_needs_a_completed_instance_whose_stock_reappeared(case):
    closed, changed = reappeared(case)
    second = case.catalog.groups[1].item_id
    for state, item in ((case.state, ITEM), (closed, ITEM), (pending(case), ITEM), (closed, second), (changed, second)):
        before = deepcopy(state)
        with pytest.raises(ProtocolError) as caught:
            case.helper.reopen_settled(state, item, LATER)
        assert (caught.value.code, caught.value.status) == (REOPEN_UNNEEDED, 409)
        assert state == before


def test_tickets_that_reappeared_together_reopen_one_after_another(case):
    second = case.catalog.groups[1].item_id
    closed, _ = case.helper.settle(stock(case, case.state, **{ITEM: 1, second: 1}), NOW)
    changed = stock(case, closed, **{ITEM: REAPPEARED, second: REAPPEARED})
    first, report = case.helper.reopen_settled(changed, second, LATER)
    assert report["archived"]["item_id"] == second and report["reappeared"] == [ITEM]
    # The other ticket's reappeared stock still blocks the save until that ticket itself is reopened.
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(first)
    assert caught.value.code == "vote_expiry_already_closed"
    both, report = case.helper.reopen_settled(first, ITEM, LATER)
    assert report["reappeared"] == [] and case.helper.initialize(both) == both
    assert [record["item_id"] for record in case.helper.archived(both)] == [second, ITEM]
    converted, report = case.helper.settle(both, LATER)
    assert sorted((grant["item_id"], grant["amount"]) for grant in report["grants"]) == [
        (ITEM, REAPPEARED), (second, REAPPEARED)]


def test_reopening_and_later_observations_cannot_precede_the_saved_instants(case):
    closed, changed = reappeared(case)
    with pytest.raises(ProtocolError) as caught:
        case.helper.reopen_settled(changed, ITEM, NOW - timedelta(seconds=1))
    assert (caught.value.code, caught.value.status) == ("vote_expiry_clock_reversed", 409)
    reopened, _ = case.helper.reopen_settled(changed, ITEM, LATER)
    with pytest.raises(ProtocolError) as caught:
        case.helper.settle(reopened, LATER - timedelta(seconds=1))
    assert (caught.value.code, caught.value.status) == ("vote_expiry_clock_reversed", 409)
    notified, notices = VoteExpiryNotifications(case.helper).settle(closed, LATER + timedelta(hours=1))
    announced = stock(case, notified, **{ITEM: REAPPEARED})
    with pytest.raises(ProtocolError) as caught:
        case.helper.reopen_settled(announced, ITEM, LATER)
    assert (caught.value.code, caught.value.status) == ("vote_expiry_clock_reversed", 409)
    assert case.helper.reopen_settled(announced, ITEM, LATER + timedelta(hours=1))[1]["notice"] == notices[0]


def test_a_reopened_ticket_can_close_again_and_reopen_once_more(case):
    _, changed = reappeared(case)
    converted, _ = case.helper.settle(case.helper.reopen_settled(changed, ITEM, LATER)[0], LATER)
    again, _ = case.helper.reopen_settled(stock(case, converted, **{ITEM: 1}), ITEM, LATER + timedelta(hours=1))
    assert [record["archived_at"] for record in case.helper.archived(again)] == [
        stamp(LATER), stamp(LATER + timedelta(hours=1))]
    assert case.helper.initialize(again) == again
    again["vote_expiry"][ARCHIVED_KEY][1]["observed_at"] = stamp(LATER) - 1
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(again)
    assert caught.value.code == "vote_expiry_state_invalid"


ARCHIVE_TAMPERS = {
    "status": lambda record: record.update(status="pending"),
    "early": lambda record: record.update(archived_at=record["settled_at"] - 1),
    "definition": lambda record: record.update(definition="b" * 64),
    "identity": lambda record: record.pop("item_id"),
    "foreign": lambda record: record.update(item_id="gift_0001_quartz"),
    "mail": lambda record: record.update(mail_ids=[999]),
}


@pytest.mark.parametrize("damage", sorted(ARCHIVE_TAMPERS))
def test_archived_records_keep_their_permanent_proof(case, damage):
    _, changed = reappeared(case)
    reopened, _ = case.helper.reopen_settled(changed, ITEM, LATER)
    ARCHIVE_TAMPERS[damage](reopened["vote_expiry"][ARCHIVED_KEY][0])
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(reopened)
    assert caught.value.code == "vote_expiry_state_invalid"


def test_an_empty_archive_or_an_instance_observed_before_it_is_rejected(case):
    _, changed = reappeared(case)
    reopened, _ = case.helper.reopen_settled(changed, ITEM, LATER)
    emptied = deepcopy(reopened)
    emptied["vote_expiry"][ARCHIVED_KEY] = []
    converted, _ = case.helper.settle(reopened, LATER + timedelta(seconds=5))
    converted["vote_expiry"]["items"][ITEM]["observed_at"] = stamp(LATER) - 1
    for broken in (emptied, converted):
        with pytest.raises(ProtocolError) as caught:
            case.helper.initialize(broken)
        assert caught.value.code == "vote_expiry_state_invalid"


def test_reopening_moves_the_announced_notice_into_the_archive(case):
    closed, _ = reappeared(case)
    notifier = VoteExpiryNotifications(case.helper)
    notified, notices = notifier.settle(closed, NOW + timedelta(seconds=1))
    reopened, report = case.helper.reopen_settled(stock(case, notified, **{ITEM: REAPPEARED}), ITEM, LATER)
    assert notifier.records(reopened) == {} and notifier.archived(reopened) == notices == [report["notice"]]
    assert reopened[NOTIFICATION_KEY] == {"schema_version": 1, "records": {}, ARCHIVED_KEY: notices}
    assert notifier.count(reopened) == 1 and notifier.initialize(reopened) == reopened
    notifier.validate_receipt(reopened, notices, 1)
    converted, report = case.helper.settle(reopened, LATER)
    announced, fresh = notifier.settle(converted, LATER)
    assert fresh == [{"item_id": ITEM, "gold": REAPPEARED * 50, "mail_ids": report["grants"][0]["mail_ids"],
                      "notified_at": stamp(LATER)}]
    assert notifier.count(announced) == 2 and notifier.archived(announced) == notices
    notifier.validate_receipt(announced, fresh, 2, exact=True)
    notifier.validate_receipt(announced, notices, 1)
    assert notifier.settle(announced, LATER + timedelta(days=1)) == (announced, [])


def test_an_unannounced_conversion_is_archived_without_a_notice(case):
    _, changed = reappeared(case)
    notifier = VoteExpiryNotifications(case.helper)
    reopened, report = case.helper.reopen_settled(changed, ITEM, LATER)
    assert NOTIFICATION_KEY not in reopened and notifier.count(reopened) == 0 and report["notice"] is None
    converted, report = case.helper.settle(reopened, LATER)
    _, notices = notifier.settle(converted, LATER)
    assert [notice["mail_ids"] for notice in notices] == [report["grants"][0]["mail_ids"]]


NOTICE_TAMPERS = {
    "gold": lambda notices: notices[0].update(gold=1),
    "item": lambda notices: notices[0].update(item_id="item_vote_016002"),
    "late": lambda notices: notices[0].update(notified_at=stamp(LATER) + 1),
    "mail": lambda notices: notices[0].update(mail_ids=[999]),
    "repeated": lambda notices: notices.append(dict(notices[0])),
}


@pytest.mark.parametrize("damage", sorted(NOTICE_TAMPERS))
def test_archived_notices_must_match_their_archived_conversion(case, damage):
    closed, _ = reappeared(case)
    notifier = VoteExpiryNotifications(case.helper)
    notified, _ = notifier.settle(closed, NOW + timedelta(seconds=1))
    reopened, _ = case.helper.reopen_settled(stock(case, notified, **{ITEM: REAPPEARED}), ITEM, LATER)
    NOTICE_TAMPERS[damage](reopened[NOTIFICATION_KEY][ARCHIVED_KEY])
    with pytest.raises(ProtocolError) as caught:
        notifier.initialize(reopened)
    assert (caught.value.code, caught.value.status) == ("vote_expiry_notifications_invalid", 503)

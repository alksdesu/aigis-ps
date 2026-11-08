from copy import copy, deepcopy
from datetime import timedelta

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.vote_cooking_notifications import VoteCookingNotifications
from aigis_ps.vote_cooking_rules import VoteCookingRules
from aigis_ps.vote_cooking_service import VoteCookingService
from test_quest_service import bundle
from test_vote_catalog import vote_sources, build
from test_vote_cooking_service import factory, progress, entry
from test_vote_cooking_catalog import relocated_catalog
from test_vote_service import table_rows
from test_vote_transactions import catalog, make_case, NOW


def ready(case):
    return progress(case, case.store.profile().state, case.candidate.card_id, 1)


def test_new_delivery_and_notification_share_one_pure_state_change(factory):
    case = factory()
    source = VoteCookingNotifications(case.cooking)
    state = ready(case)
    original = deepcopy(state)
    version = case.cooking.content_version
    updated, deliveries, notices = source.settle(state, case.group.result_start)
    assert state == original
    assert deliveries == notices and len(notices) == 1
    assert entry(case, updated)["status"] == "delivered"
    record = updated["vote_cooking_notifications"]["records"][case.group.item_id]
    assert record["grant"] == notices[0] and record["source"] == source.content_version
    assert updated["tables"]["game_param2"] == state["tables"]["game_param2"]
    assert table_rows(source.response(notices)) == [{"CardId": case.candidate.card_id, "Count": 1, "ReceiveTextID": 5028}]
    assert case.cooking.content_version == version
    assert source.settle(updated, case.group.result_start) == (updated, [], [])
    assert table_rows(source.response([])) == []


def test_old_delivery_receives_notice_without_second_mail(factory):
    case = factory()
    old, grants = case.cooking.settle(ready(case), case.group.result_start)
    before = deepcopy(old)
    source = VoteCookingNotifications(case.cooking)
    assert source.initialize(old) == old and "vote_cooking_notifications" not in old
    updated, deliveries, notices = source.settle(old, case.group.result_start + timedelta(seconds=1))
    assert old == before and deliveries == [] and notices == grants
    assert updated["tables"] == old["tables"]
    assert updated["last_present_id"] == old["last_present_id"]
    assert source.settle(updated, case.group.result_start + timedelta(days=1)) == (updated, [], [])


@pytest.mark.parametrize("notified", [False, True])
def test_resource_upgrade_preserves_deliveries_and_only_projects_current_notice_text(factory, notified):
    case = factory()
    case.presents.vote_cooking_catalog = relocated_catalog(case.cooking.recipes, 5006)
    case.cooking = VoteCookingService(case.catalog, case.presents.vote_cooking_catalog, case.presents)
    old_source = VoteCookingNotifications(case.cooking)
    old, grants = case.cooking.settle(ready(case), case.group.result_start)
    if notified:
        old, _, notices = old_source.settle(old, case.group.result_start)
        assert notices == grants
    before = deepcopy(old)
    recipes = relocated_catalog(case.cooking.recipes)
    presents = copy(case.presents)
    presents.vote_cooking_catalog = recipes
    cooking = VoteCookingService(case.catalog, recipes, presents)
    source = VoteCookingNotifications(cooking)
    assert source.initialize(old) == old
    cooking.validate_receipt(old, grants)
    updated, deliveries, notices = source.settle(old, case.group.result_start + timedelta(seconds=1))
    assert old == before and deliveries == []
    assert updated["vote_cooking"] == old["vote_cooking"]
    assert updated["tables"] == old["tables"] and updated["last_present_id"] == old["last_present_id"]
    assert notices == ([] if notified else grants)
    if notices:
        assert notices[0]["receive_text_id"] == 5006
        assert table_rows(source.response(notices))[0]["ReceiveTextID"] == 5028
        assert updated["vote_cooking_notifications"]["records"][case.group.item_id]["grant"] == grants[0]
    else:
        assert updated["vote_cooking_notifications"] == old["vote_cooking_notifications"]
    assert source.settle(updated, case.group.result_start + timedelta(days=1)) == (updated, [], [])
    corrupted = deepcopy(grants)
    corrupted[0]["receive_text_id"] = 5028
    with pytest.raises(ProtocolError):
        cooking.validate_receipt(updated, corrupted)


def test_claimed_mail_keeps_delayed_notification_and_permanent_proof(factory):
    case = factory()
    old, grants = case.cooking.settle(ready(case), case.group.result_start)
    old["tables"]["presents"] = []
    source = VoteCookingNotifications(case.cooking)
    updated, deliveries, notices = source.settle(old, case.group.result_start)
    assert deliveries == [] and notices == grants
    assert case.presents.rows(updated) == []
    rebuilt = VoteCookingNotifications(case.cooking)
    assert rebuilt.initialize(updated) == updated
    rebuilt.validate_receipt(updated, [], notices)
    assert rebuilt.settle(updated, case.group.result_start + timedelta(days=1)) == (updated, [], [])


def test_multiple_groups_deliver_and_notify_in_stable_order(factory):
    case = factory()
    final = case.catalog.groups[-1]
    state = progress(case, ready(case), case.candidate.card_id, 1, group=final, now=final.vote_start)
    old, first = case.cooking.settle(state, final.result_start)
    source = VoteCookingNotifications(case.cooking)
    updated, deliveries, notices = source.settle(old, final.result_start)
    assert deliveries[0]["group_id"] == 4 and notices == first
    second, delivery2, notices2 = source.settle(updated, final.result_start)
    assert delivery2 == [] and notices2 == deliveries
    assert len(case.presents.rows(second)) == 2
    assert source.settle(second, final.result_start) == (second, [], [])


def test_full_mailbox_has_no_notification_until_actual_delivery(factory):
    case = factory(VoteCookingRules(copies=2))
    state, _ = case.presents.enqueue(ready(case), [{"kind": "gold", "amount": 1, "description": "fixture"}] * 499)
    source = VoteCookingNotifications(case.cooking)
    pending, deliveries, notices = source.settle(state, case.group.result_start)
    assert deliveries == notices == []
    assert entry(case, pending)["status"] == "pending"
    assert "vote_cooking_notifications" not in pending
    pending["tables"]["presents"].pop()
    updated, deliveries, notices = source.settle(pending, case.group.result_start)
    assert deliveries == notices and notices[0]["count"] == 2
    assert notices[0]["mail_ids"] == [500, 501]


def test_old_unnotified_delivery_can_notify_while_another_group_waits_for_space(factory):
    case = factory()
    final = case.catalog.groups[-1]
    state = progress(case, ready(case), case.candidate.card_id, 1, group=final, now=final.vote_start)
    delivered, grants = case.cooking.settle(state, final.result_start)
    full, _ = case.presents.enqueue(delivered, [{"kind": "gold", "amount": 1, "description": "fixture"}] * 499)
    source = VoteCookingNotifications(case.cooking)
    updated, deliveries, notices = source.settle(full, final.result_start)
    assert deliveries == [] and notices == grants
    assert entry(case, updated, final)["status"] == "pending"
    assert len(case.presents.rows(updated)) == 500


def test_disabled_new_policy_does_not_erase_delivered_notification(factory):
    case = factory()
    old, grants = case.cooking.settle(ready(case), case.group.result_start)
    disabled = VoteCookingService(case.catalog, case.cooking.recipes, case.presents,
                                  VoteCookingRules(enabled=False, minimum_votes=999, copies=10))
    source = VoteCookingNotifications(disabled)
    updated, deliveries, notices = source.settle(old, case.group.result_start)
    assert deliveries == [] and notices == grants
    assert source.settle(updated, case.group.result_start) == (updated, [], [])


@pytest.mark.parametrize("fault", ("missing_cooking", "unknown_field", "schema", "empty_records", "time",
                                  "backdated", "source", "grant_card", "grant_count", "item", "last_time"))
def test_damaged_notification_state_is_rejected(factory, fault):
    case = factory()
    source = VoteCookingNotifications(case.cooking)
    state, deliveries, notices = source.settle(ready(case), case.group.result_start)
    damaged = deepcopy(state)
    ledger = damaged["vote_cooking_notifications"]
    record = ledger["records"][case.group.item_id]
    if fault == "missing_cooking": del damaged["vote_cooking"]
    if fault == "unknown_field": record["bonus"] = 2
    if fault == "schema": ledger["schema_version"] = True
    if fault == "empty_records": ledger["records"] = {}
    if fault == "time": record["notified_at"] = "2026-11-01"
    if fault == "backdated": record["notified_at"] = entry(case, damaged)["started_at"]
    if fault == "source": record["source"] = "unknown"
    if fault == "grant_card": record["grant"]["card_id"] += 1
    if fault == "grant_count": record["grant"]["count"] = True
    if fault == "item": ledger["records"]["item_vote_016002"] = ledger["records"].pop(case.group.item_id)
    if fault == "last_time": ledger["last_time"] = "2999-01-01T00:00:00.000000+00:00"
    before = deepcopy(damaged)
    with pytest.raises(ProtocolError): source.initialize(damaged)
    with pytest.raises(ProtocolError): source.settle(damaged, case.group.result_start)
    assert damaged == before


def test_receipt_requires_notification_domain_even_after_mail_is_gone(factory):
    case = factory()
    source = VoteCookingNotifications(case.cooking)
    state, deliveries, notices = source.settle(ready(case), case.group.result_start)
    damaged = deepcopy(state)
    del damaged["vote_cooking_notifications"]
    with pytest.raises(ProtocolError, match="permanent notification proof"):
        source.validate_receipt(damaged, deliveries, notices)


def test_duplicate_notices_and_mail_ids_are_not_accepted(factory):
    case = factory()
    source = VoteCookingNotifications(case.cooking)
    state, deliveries, notices = source.settle(ready(case), case.group.result_start)
    for bad in (notices * 2, [{**notices[0], "mail_ids": [0]}], [{**notices[0], "receive_text_id": 1}]):
        with pytest.raises(ProtocolError): source.validate_receipt(state, deliveries, bad)
    with pytest.raises(ProtocolError): source.response(notices * 2)


def test_no_eligible_history_is_unchanged_and_clock_must_not_go_back(factory):
    case = factory()
    source = VoteCookingNotifications(case.cooking)
    empty = case.store.profile().state
    assert source.settle(empty, NOW) == (empty, [], [])
    tracked = ready(case)
    assert source.settle(tracked, NOW) == (tracked, [], [])
    state, _, _ = source.settle(tracked, case.group.result_start + timedelta(days=1))
    before = deepcopy(state)
    with pytest.raises(ProtocolError, match="backwards"):
        source.settle(state, case.group.result_start)
    assert state == before


def test_dependency_replacement_is_rejected(factory):
    case = factory()
    source = VoteCookingNotifications(case.cooking)
    source.content_version = "a" * 64
    with pytest.raises(ProtocolError, match="dependencies changed"):
        source.initialize(case.store.profile().state)

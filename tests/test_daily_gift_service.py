from copy import copy, deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aigis_ps.daily_gift_rules import DailyGiftRules
from aigis_ps.daily_gift_service import DailyGiftService, daily_gift_definition
from aigis_ps.daily_gift_state import DELIVERED, PENDING, read_daily_gifts
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES
from aigis_ps.presents import PRESENT_CAPACITY
from test_daily_gift_catalog import daily_sources, make
from test_event_login_service import assert_gift_box_receipts_replay
from test_event_pass_service import accept_stored_item, item_catalogs
from test_quest_service import bundle


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture(scope="module")
def initial(bundle):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    return bundle.initialize(bootstrap.initial_state())


@pytest.fixture
def factory(bundle, initial, daily_sources):
    def build(*, ids=(1,), enabled=True, changes=None, revive=True, end="2026-11-03"):
        rules = DailyGiftRules(enabled, ids, {key: {"start_date": "2026-11-01", "end_date": end}
                              for key in ids} if revive and ids is not None else {})
        catalog = make(daily_sources, rules, changes)
        presents = copy(bundle.presents)
        return SimpleNamespace(catalog=catalog, presents=presents, state=deepcopy(initial),
                               service=DailyGiftService(catalog, presents, rules))
    return build


def records(state, gift_id=1):
    return read_daily_gifts(state)["gifts"].get(str(gift_id), {})


def fail(code, status):
    def action(*args, **kwargs):
        raise ProtocolError(code, "Injected failure", status)
    return action


def test_current_expired_directory_and_disabled_source_do_not_create_history(factory):
    for case in (factory(ids=None, revive=False), factory(enabled=False)):
        assert case.service.settle(case.state, NOW) == (case.state, [])
        assert case.service.initialize(case.state) == case.state


def test_daily_eligibility_is_independent_of_other_login_fields(factory):
    case = factory()
    before = deepcopy(case.state)
    result, grants = case.service.settle(case.state, NOW)
    assert case.state == before
    assert grants[0]["gift_id"] == 1 and grants[0]["earned_day"] == "2026-11-01"
    assert case.presents.rows(result)[-1]["Type"] == 0
    assert case.service.settle(result, NOW + timedelta(hours=1)) == (result, [])
    assert result["tables"]["game_param2"] == before["tables"]["game_param2"]
    assert result["player_state"] == before["player_state"]


@pytest.mark.parametrize("moment,expected", [(datetime(2026, 11, 1, 3, 59, tzinfo=JST), 0),
    (datetime(2026, 11, 1, 4, tzinfo=JST), 1), (datetime(2026, 11, 4, 3, 59, tzinfo=JST), 1),
    (datetime(2026, 11, 4, 4, tzinfo=JST), 0)])
def test_inclusive_source_dates_use_jst_four_am(factory, moment, expected):
    case = factory()
    _, grants = case.service.settle(case.state, moment)
    assert len(grants) == expected


def test_missing_days_are_not_filled_and_reopened_service_preserves_claims(factory):
    case = factory()
    first, _ = case.service.settle(case.state, NOW)
    current = factory()
    result, grants = current.service.settle(first, NOW + timedelta(days=2))
    assert list(records(result)) == ["2026-11-01", "2026-11-03"]
    assert len(grants) == 1
    assert current.service.settle(result, NOW + timedelta(days=2)) == (result, [])


def test_pending_accumulates_observed_days_and_survives_expiry_selection_and_disable(factory, monkeypatch):
    case = factory()
    monkeypatch.setattr(case.presents, "enqueue_groups", fail(PRESENT_CAPACITY, 409))
    first, _ = case.service.settle(case.state, NOW)
    pending, grants = case.service.settle(first, NOW + timedelta(days=1))
    assert grants == [] and len(records(pending)) == 2
    changed = factory(ids=(15,), enabled=False)
    result, grants = changed.service.settle(pending, NOW + timedelta(days=30))
    assert [grant["earned_day"] for grant in grants] == ["2026-11-01", "2026-11-02"]
    assert all(grant["delivery_day"] == "2026-12-01" for grant in grants)
    assert changed.service.settle(result, NOW + timedelta(days=31)) == (result, [])


@pytest.mark.parametrize("gift_id,kind,content", [(1, 0, 1), (15, 1, 360), (57, 2, 280000), (73, 22, 1)])
def test_real_reward_shapes_include_zero_recovery_item(factory, gift_id, kind, content):
    case = factory(ids=(gift_id,))
    result, grants = case.service.settle(case.state, NOW)
    assert len(grants) == 1
    mail = case.presents.rows(result)[-1]
    assert mail["Type"] == kind and mail["Content"] == content
    assert mail["Description"] == case.catalog.get(gift_id).description
    if gift_id == 73:
        assert mail["ItemID"] == "item_cook_10006"


def test_real_source_reward_mapping_preserves_missing_ticket_pools(factory):
    case = factory(ids=None, revive=False)
    counts = {"supported": 0, "pending": 0}
    for campaign in case.catalog.campaigns:
        try:
            rewards = case.service._rewards(campaign)
            case.presents._prepare_reward_rows(rewards, 0)
            counts["supported"] += 1
        except ProtocolError as error:
            assert error.status == 501
            assert campaign.rewards[0].item_type == 22 and campaign.rewards[0].parameter == 0
            counts["pending"] += 1
    assert counts == {"supported": 314, "pending": 5}


@pytest.mark.parametrize("change", [{"Type_Gift": 7}, {"TypeCondition": 1}, {"ParamCondition": 1},
    {"Type_Item": 99}, {"Param_Gift": 0}, {"Param_Gift": -1}, {"Type_Item": 1, "Param_Gift": 2147483647},
    {"Type_Item": 22, "ID_Item": "gift_0000_bouquet", "Param_Gift": 0}])
def test_unsupported_reward_keeps_earned_day_pending(factory, change):
    case = factory(changes={0: change})
    result, grants = case.service.settle(case.state, NOW)
    assert not grants and not case.presents.rows(result)
    assert records(result)["2026-11-01"]["status"] == PENDING


def test_item_beyond_the_native_cell_stays_pending_instead_of_failing_115(factory, monkeypatch):
    item = "x" * STORED_ITEM_NAME_BYTES
    case = factory(changes={0: {"Type_Item": 22, "ID_Item": item, "Param_Gift": 1}})
    accept_stored_item(monkeypatch, case.presents, item)
    result, grants = case.service.settle(case.state, NOW)
    assert not grants and not case.presents.rows(result)
    assert records(result)["2026-11-01"]["reason"] == "daily_gift_reward_unsupported"


def test_whole_gift_group_is_atomic_and_other_ids_continue(factory):
    changes = {1: {"ID_DailyGift": 1, "ID_Gift": 2, "ID_SystemText": 425,
        "Days_Begin": 20140531, "Days_End": 20140604, "Type_Item": 99}}
    case = factory(ids=(1, 15), changes=changes)
    result, grants = case.service.settle(case.state, NOW)
    assert [grant["gift_id"] for grant in grants] == [15]
    assert records(result)["2026-11-01"]["status"] == PENDING
    assert len(case.presents.rows(result)) == 1


def test_callback_receives_cumulative_grants_and_cannot_mutate_candidate(factory):
    case = factory(ids=(1, 15))
    observed = []
    result, grants = case.service.settle(case.state, NOW, validate=lambda candidate, values: observed.append(
        ([grant["gift_id"] for grant in values], len(case.presents.rows(candidate)))))
    assert observed == [([1], 1), ([1, 15], 2)]
    assert len(grants) == 2 and len(case.presents.rows(result)) == 2
    def mutate(candidate, values):
        candidate["tables"]["game_param"][0]["Money"] += 1
    with pytest.raises(ProtocolError) as error:
        case.service.settle(case.state, NOW, validate=mutate)
    assert error.value.code == "daily_gift_validation_invalid" and "daily_gift" not in case.state


def test_response_capacity_defers_and_unexpected_failure_rolls_back(factory):
    case = factory()
    result, grants = case.service.settle(case.state, NOW, validate=fail("login_reward_response_capacity", 503))
    assert not grants and records(result)["2026-11-01"]["reason"] == "login_reward_response_capacity"
    with pytest.raises(ProtocolError) as error:
        case.service.settle(case.state, NOW, validate=fail("bad_provider", 503))
    assert error.value.code == "bad_provider" and "daily_gift" not in case.state


def test_source_definition_change_keeps_pending_and_historical_receipts(factory, monkeypatch):
    case = factory()
    original = case.presents.enqueue_groups
    monkeypatch.setattr(case.presents, "enqueue_groups", fail(PRESENT_CAPACITY, 409))
    pending, _ = case.service.settle(case.state, NOW)
    changed = factory(changes={0: {"Param_Gift": 2}})
    result, grants = changed.service.settle(pending, NOW)
    assert not grants and records(result)["2026-11-01"]["reason"] == "daily_gift_definition_changed"
    monkeypatch.setattr(case.presents, "enqueue_groups", original)
    delivered, grants = case.service.settle(pending, NOW)
    changed.service.validate_receipt(delivered, grants, verify_current=False)
    with pytest.raises(ProtocolError):
        changed.service.validate_receipt(delivered, grants)
    moved = factory(end="2026-11-20")
    assert daily_gift_definition(case.catalog.get(1)) == daily_gift_definition(moved.catalog.get(1))
    assert moved.service.settle(delivered, NOW) == (delivered, [])


def test_retired_pending_is_retained_without_blocking_current_gift(factory, monkeypatch):
    case = factory()
    monkeypatch.setattr(case.presents, "enqueue_groups", fail(PRESENT_CAPACITY, 409))
    pending, _ = case.service.settle(case.state, NOW)
    changed = factory(ids=(15,), changes={0: {"ID_DailyGift": 999}})
    result, grants = changed.service.settle(pending, NOW)
    assert [grant["gift_id"] for grant in grants] == [15]
    assert records(result)["2026-11-01"]["reason"] == "daily_gift_source_unavailable"


def test_mail_candidate_and_receipt_check_real_reward_not_only_state_hash(factory, monkeypatch):
    case = factory()
    original = case.presents.enqueue_groups
    def wrong(source, groups):
        result, mailed = original(source, groups)
        result["tables"]["presents"][-1]["Content"] += 1
        mailed[0][-1]["Content"] += 1
        return result, mailed
    monkeypatch.setattr(case.presents, "enqueue_groups", wrong)
    with pytest.raises(ProtocolError) as error:
        case.service.settle(case.state, NOW)
    assert error.value.code == "daily_gift_delivery_invalid"
    monkeypatch.setattr(case.presents, "enqueue_groups", original)
    result, grants = case.service.settle(case.state, NOW)
    altered = deepcopy(result)
    altered["tables"]["presents"][-1]["Content"] += 1
    with pytest.raises(ProtocolError):
        case.service.validate_receipt(altered, grants)
    result["tables"]["presents"] = []
    case.service.validate_receipt(result, grants, verify_current=False)


def test_duplicate_inner_gift_entries_remain_distinct_rewards(factory):
    changes = {1: {"ID_DailyGift": 1, "ID_Gift": 1, "ID_SystemText": 425,
        "Days_Begin": 20140531, "Days_End": 20140604, "Param_Gift": 2}}
    case = factory(changes=changes)
    result, grants = case.service.settle(case.state, NOW)
    assert [reward.gift_entry_id for reward in case.catalog.get(1).rewards] == [1, 1]
    assert [mail["Content"] for mail in case.presents.rows(result)] == [1, 2]
    assert len(grants) == 1 and len(grants[0]["mail_ids"]) == 2
    case.service.validate_receipt(result, grants)


def test_real_mailbox_capacity_preserves_entire_gift_group(factory):
    changes = {1: {"ID_DailyGift": 1, "ID_Gift": 2, "ID_SystemText": 425,
        "Days_Begin": 20140531, "Days_End": 20140604}}
    case = factory(changes=changes)
    full, _ = case.presents.enqueue(case.state, [{"kind": "gold", "amount": 1, "description": "capacity"}] * 499)
    result, grants = case.service.settle(full, NOW)
    assert not grants and len(case.presents.rows(result)) == 499
    assert records(result)["2026-11-01"]["reason"] == PRESENT_CAPACITY
    assert result["last_present_id"] == full["last_present_id"]


def test_verified_zero_ticket_uses_one_item_and_complete_own_pool(factory):
    case = factory(changes={0: {"Type_Item": 22, "ID_Item": "item_smn_ticket_85143", "Param_Gift": 0}})
    result, grants = case.service.settle(case.state, NOW)
    assert len(grants) == 1
    assert case.presents.rows(result)[-1] == {"PresentID": result["last_present_id"], "Type": 22,
        "Content": 1, "ItemID": "item_smn_ticket_85143", "Description": case.catalog.get(1).description}


def test_ledger_statuses_keep_the_spelling_of_earlier_saves():
    assert (PENDING, DELIVERED) == ("pending", "delivered")


def test_mail_identity_exhaustion_is_recoverable_without_partial_award(factory):
    case = factory()
    case.state.update(presents_schema=1, last_present_id=0x7FFFFFFF)
    result, grants = case.service.settle(case.state, NOW)
    assert not grants and not case.presents.rows(result)
    assert records(result)["2026-11-01"]["reason"] == "present_identity_exhausted"


def test_mail_provider_cannot_change_unrelated_state(factory, monkeypatch):
    case = factory()
    original = case.presents.enqueue_groups
    def changed(source, groups):
        result, rows = original(source, groups)
        result["tables"]["game_param"][0]["Money"] += 1
        return result, rows
    monkeypatch.setattr(case.presents, "enqueue_groups", changed)
    with pytest.raises(ProtocolError) as error:
        case.service.settle(case.state, NOW)
    assert error.value.code == "daily_gift_delivery_invalid"


def test_receipts_of_every_gift_box_configuration_replay_and_others_stay_stale(factory, item_catalogs):
    case = factory()
    service = assert_gift_box_receipts_replay(
        lambda presents: DailyGiftService(case.catalog, presents, case.service.rules),
        case.presents, item_catalogs, "daily_gift_dependencies_invalid")
    assert factory(ids=(15,)).service.content_version not in service.replayable_content_versions()

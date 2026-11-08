from copy import copy, deepcopy
from datetime import datetime, timedelta
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES
from aigis_ps.present_recovery import PresentRecoveryDefinition
from aigis_ps.purchased_login_rules import PurchasedLoginRules
from aigis_ps.purchased_login_service import DESCRIPTION, PurchasedLoginService, combine_login_notifications
from test_event_login_service import assert_gift_box_receipts_replay
from test_event_pass_service import item_catalogs
from test_purchased_login_catalog import make, sources, event_resource
from test_quest_service import bundle


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
REQUEST = "a" * 64


@pytest.fixture(scope="module")
def initial(bundle):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    return bundle.initialize(bootstrap.initial_state())


@pytest.fixture
def factory(bundle, initial, make):
    def build(*, enabled=True, selection=(129,), limit=1, changes=None, bought=True, now=NOW):
        rules = PurchasedLoginRules(enabled, selection, limit)
        catalog, data, _ = make(rules, changes)
        presents = copy(bundle.presents)
        service = PurchasedLoginService(catalog, presents, rules)
        state = deepcopy(initial)
        offer = catalog.get("local_login_129")
        entry = None
        if bought:
            state, entry = service.acquire(state, offer, REQUEST, now)
        return SimpleNamespace(service=service, catalog=catalog, data=data, state=state,
                               presents=presents, offer=offer, entry=entry, initial=deepcopy(initial))
    return build


def record(state, number=1):
    return state["purchased_login"]["purchases"][str(number)]


def reject(case, action, code):
    before = deepcopy(case.state)
    with pytest.raises(ProtocolError) as error:
        action()
    assert error.value.code == code
    assert case.state == before


def rows(node):
    return [dict(zip((column.name for column in node.children), values))
            for values in zip(*(column.values for column in node.children))]


def test_purchase_only_creates_qualification_and_fixed_proof(factory):
    case = factory()
    assert case.state["tables"] == case.initial["tables"]
    assert case.entry["purchase_id"] == 1 and case.entry["purchase_time"] == "2026/11/01 12:00:00"
    assert case.entry["start_day"] == "2026-11-02" and case.entry["end_day"] == "2026-12-02"
    assert case.service.validate_purchase_receipt(case.state, case.offer, REQUEST, 1) == case.entry
    assert case.service.acquire(case.state, case.offer, REQUEST, None) == (case.state, case.entry)
    assert case.service.settle(case.state, NOW) == (case.state, [])


def test_no_qualification_never_receives_rewards(factory):
    case = factory(bought=False)
    assert case.service.initialize(case.state) == case.state
    assert case.service.settle(case.state, NOW + timedelta(days=90)) == (case.state, [])


def test_next_day_real_reward_carries_original_purchase_time(factory):
    case = factory()
    result, grants = case.service.settle(case.state, NOW + timedelta(days=1))
    assert grants[0]["purchase_id"] == 1 and grants[0]["login_count"] == 1
    assert grants[0]["purchase_time"] == "2026/11/01 12:00:00"
    assert case.presents.rows(result) == [{"PresentID": 1, "Type": 0, "Content": 3,
                                          "Description": "購入パックログインボーナス"}]
    assert case.service.response(grants) == Container("EBNS", (Column("LoginBonusID", "I", (129,)),
        Column("LoginCount", "I", (1,)), Column("PurchaseTime", "S", ("2026/11/01 12:00:00",))))
    assert case.service.settle(result, NOW + timedelta(days=1, hours=1)) == (result, [])


@pytest.mark.parametrize("hour,minute,first", [(0, 0, "2026-11-01"), (3, 59, "2026-11-01"), (4, 0, "2026-11-02")])
def test_purchase_day_four_am_boundary(factory, hour, minute, first):
    now = NOW.replace(hour=hour, minute=minute)
    case = factory(now=now)
    assert case.entry["start_day"] == first
    cutoff = datetime.fromisoformat(first).replace(hour=4, tzinfo=JST)
    assert case.service.settle(case.state, cutoff - timedelta(seconds=1)) == (case.state, [])
    _, grants = case.service.settle(case.state, cutoff)
    assert grants[0]["login_count"] == 1


def test_source_start_clamp_does_not_extend_duration(factory, sources, event_resource):
    from aigis_ps.event_login_catalog import EventLoginCatalog
    index = EventLoginCatalog(lambda _: event_resource).get(129).source_index
    future_start = datetime(2026, 11, 10).date().toordinal() - 1
    case = factory(changes={index: {"StartDate": future_start}})
    assert case.entry["start_day"] == "2026-11-10"
    assert case.entry["end_day"] == "2026-12-02"


def test_purchase_limit_and_active_instance_are_separate_guards(factory):
    once = factory()
    reject(once, lambda: once.service.acquire(once.state, once.offer, "b"*64, NOW + timedelta(days=45)),
           "purchased_login_purchase_limit")
    twice = factory(limit=2)
    reject(twice, lambda: twice.service.acquire(twice.state, twice.offer, "b"*64, NOW + timedelta(days=1)),
           "purchased_login_purchase_active")
    newer, entry = twice.service.acquire(twice.state, twice.offer, "b"*64, NOW + timedelta(days=45))
    assert entry["purchase_id"] == 2 and len(newer["purchased_login"]["purchases"]) == 2
    assert record(newer) == record(twice.state)


def test_pending_prevents_repeat_purchase_and_survives_expiration(factory, monkeypatch):
    case = factory(limit=2)
    original = case.presents.enqueue_groups
    monkeypatch.setattr(case.presents, "enqueue_groups", lambda *_: (_ for _ in ()).throw(
        ProtocolError("present_capacity", "Full", 409)))
    pending, grants = case.service.settle(case.state, NOW + timedelta(days=1))
    assert not grants and record(pending)["pending"]["ordinal"] == 1
    reject(case, lambda: case.service.acquire(pending, case.offer, "b"*64, NOW + timedelta(days=45)),
           "purchased_login_purchase_active")
    monkeypatch.setattr(case.presents, "enqueue_groups", original)
    result, grants = case.service.settle(pending, NOW + timedelta(days=45))
    assert grants[0]["earned_day"] == "2026-11-02" and grants[0]["purchase_time"] == case.entry["purchase_time"]
    assert case.service.settle(result, NOW + timedelta(days=46)) == (result, [])


def test_disabled_sales_still_honor_existing_qualification(factory):
    active = factory()
    disabled = factory(enabled=False, bought=False)
    result, grants = disabled.service.settle(active.state, NOW + timedelta(days=1))
    assert grants[0]["campaign_id"] == 129
    assert len(disabled.presents.rows(result)) == 1
    reject(disabled, lambda: disabled.service.acquire(disabled.state, disabled.offer, REQUEST, NOW),
           "purchased_login_sale_unavailable")


def test_ppf_empty_default_selected_and_owned_outside_selection(factory):
    disabled = factory(enabled=False, bought=False)
    assert rows(disabled.service.ppf(disabled.state)) == []
    active = factory()
    selected = rows(active.service.ppf(active.initial))
    assert [row["PackageID"] for row in selected] == [active.offer.flag_id]
    assert selected[0]["PurchaseFlag"] == 0
    owned = rows(disabled.service.ppf(active.state))
    assert owned == [{"RecordIndex": active.offer.flag_id, "PackageID": active.offer.flag_id,
        "PurchaseFlag": 1, "PurchaseStartDateTime": 0.0, "PurchaseEndDateTime": 0.0}]
    other = factory(selection=(136,), bought=False)
    assert {row["PackageID"] for row in rows(other.service.ppf(active.state))} == {
        active.offer.flag_id, other.catalog.get("local_login_136").flag_id}


def test_five_day_schedule_completes_without_skipped_day_backfill(factory):
    case = factory()
    result = case.state
    for count, day in enumerate((1, 3, 5, 7, 9), 1):
        result, grants = case.service.settle(result, NOW + timedelta(days=day))
        assert grants[0]["login_count"] == count
    assert case.service.settle(result, NOW + timedelta(days=10)) == (result, [])
    assert len(case.presents.rows(result)) == 5


def test_fatal_or_mutating_validator_does_not_commit_partial_state(factory):
    case = factory()
    def fatal(*_):
        raise ProtocolError("test_failure", "Fatal", 503)
    reject(case, lambda: case.service.settle(case.state, NOW + timedelta(days=1), fatal), "test_failure")
    def changed(candidate, grants):
        candidate["bad"] = True
    reject(case, lambda: case.service.settle(case.state, NOW + timedelta(days=1), changed),
           "purchased_login_validation_invalid")


@pytest.mark.parametrize("field,value", [("request", "b"*64), ("sku", "another"), ("flag_id", 1)])
def test_purchase_proof_cannot_be_forged(factory, field, value):
    case = factory()
    bad = deepcopy(case.state)
    record(bad)[field] = value
    reject(case, lambda: case.service.validate_purchase_receipt(bad, case.offer, REQUEST, 1),
           "purchased_login_purchase_invalid")


def test_receipt_requires_current_reward_and_purchase_time(factory):
    case = factory()
    result, grants = case.service.settle(case.state, NOW + timedelta(days=1))
    bad = deepcopy(result)
    bad["tables"]["presents"][0]["Content"] = 999
    reject(case, lambda: case.service.validate_receipt(bad, grants), "purchased_login_receipt_invalid")
    altered = deepcopy(grants)
    altered[0]["purchase_time"] = "2026/11/02 12:00:00"
    reject(case, lambda: case.service.validate_receipt(result, altered), "purchased_login_state_invalid")
    result["tables"]["presents"] = []
    case.service.validate_receipt(result, grants, verify_current=False)
    assert case.service.initialize(result) == result


def test_three_source_notification_merge_checks_identity_and_mail(factory):
    case = factory()
    _, purchased = case.service.settle(case.state, NOW + timedelta(days=1))
    public = {key: value for key, value in purchased[0].items() if key != "purchase_id"}
    public.update(campaign_id=135, purchase_time="", mail_ids=[2])
    comeback = {**public, "campaign_id": 6, "round_id": 1, "notify": True, "mail_ids": [3]}
    result = combine_login_notifications([public], [comeback], purchased)
    assert result.children[0].values == (6, 129, 135)
    comeback["notify"] = False
    assert combine_login_notifications([public], [comeback], purchased).children[0].values == (129, 135)
    public["mail_ids"] = [1]
    reject(case, lambda: combine_login_notifications([public], [], purchased), "login_notification_duplicate_mail")


@pytest.mark.parametrize("item,parameter", [("x" * 40, 0), ("item_cook_logbo_00004", 1)])
def test_unverified_reward_metadata_is_pending_without_partial_grant(factory, item, parameter):
    original = factory(bought=False)
    index = original.offer.campaign.source_index
    case = factory(changes={index: {"ItemType": 22, "ItemID": item, "ItemParam": 1, "_Param1": parameter}})
    result, grants = case.service.settle(case.state, NOW + timedelta(days=1))
    assert not grants and case.presents.rows(result) == []
    assert record(result)["pending"]["reason"] == "purchased_login_reward_unsupported"


def test_recovery_item_beyond_the_native_cell_stays_pending_instead_of_failing_115(factory, monkeypatch):
    item = "x" * STORED_ITEM_NAME_BYTES
    index = factory(bought=False).offer.campaign.source_index
    case = factory(changes={index: {"ItemType": 22, "ItemID": item, "ItemParam": 1, "_Param1": 0}})
    recovery = case.presents.recovery_catalog
    # Real recovery names stay short, so only a declared stored-bound one reaches the mail writer's native cell check.
    monkeypatch.setattr(recovery, "_items", MappingProxyType({**recovery._items, item: PresentRecoveryDefinition(item)}))
    with pytest.raises(ProtocolError) as caught:
        case.presents.preview([{"kind": "charisma_recovery", "item_id": item, "description": DESCRIPTION}], 0)
    assert (caught.value.code, caught.value.status) == ("present_item_unreplayable", 503)
    result, grants = case.service.settle(case.state, NOW + timedelta(days=1))
    assert not grants and case.presents.rows(result) == []
    assert record(result)["pending"]["reason"] == "purchased_login_reward_unsupported"


def test_receipts_of_every_gift_box_configuration_replay_and_others_stay_stale(factory, item_catalogs):
    case = factory(bought=False)
    service = assert_gift_box_receipts_replay(
        lambda presents: PurchasedLoginService(case.catalog, presents, case.service.rules),
        case.presents, item_catalogs, "purchased_login_dependencies_invalid")
    assert factory(selection=(136,), bought=False).service.content_version not in service.replayable_content_versions()

from copy import copy, deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_catalog import EventLoginCatalog, RESOURCE_NAME
from aigis_ps.event_login_rules import EventLoginRules
from aigis_ps.event_login_service import EventLoginService, GiftBoxVersions, event_login_definition
from aigis_ps.event_login_state import begin_event_login, read_event_logins
from aigis_ps.login_reward_state import RESPONSE_CAPACITY as LOGIN_REWARD_RESPONSE_CAPACITY
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES
from aigis_ps.presents import GIFT_BOX_DEPENDENCIES, ITEM_CATALOG
from aigis_ps.table_cell_patch import patch_table_cells
from test_event_pass_service import accept_stored_item, item_catalogs
from test_quest_service import bundle


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture(scope="module")
def source(bundle):
    raw = bundle.native_cdn.assets.read(RESOURCE_NAME)[0]
    catalog = EventLoginCatalog(lambda _: raw)
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    state = bundle.initialize(bootstrap.initial_state())
    assert "event_login" not in state
    return raw, catalog, state


@pytest.fixture
def factory(bundle, source):
    raw, catalog, state = source
    def make(*, updates=None, ids=(135,), enabled=True):
        actual = EventLoginCatalog(lambda _: patch_table_cells(raw, updates)) if updates else catalog
        presents = copy(bundle.presents)
        service = EventLoginService(actual, presents, EventLoginRules(enabled=enabled, campaign_ids=ids))
        return SimpleNamespace(service=service, state=deepcopy(state), catalog=actual, presents=presents)
    return make


def deliveries(state, identifier=135):
    return read_event_logins(state)["campaigns"][str(identifier)]


def reject(case, action, code):
    before = deepcopy(case.state)
    with pytest.raises(ProtocolError) as caught:
        action()
    assert caught.value.code == code
    assert case.state == before


def wired_gift_box(presents, configuration):
    wired = copy(presents)
    for name, version in configuration.items():
        setattr(wired, name, None if version is None else SimpleNamespace(content_version=version))
    return wired


def assert_gift_box_receipts_replay(build, presents, item_catalogs, invalid):
    current = copy(presents)
    current.item_catalog = item_catalogs.mailed
    assert all(getattr(current, name) is not None for name in GIFT_BOX_DEPENDENCIES)
    service = build(current)
    configurations = current.gift_box_configurations()
    committed = [build(wired_gift_box(current, configuration)).content_version for configuration in configurations]
    assert committed == [service.content_version_for(configuration) for configuration in configurations]
    assert committed[0] == service.content_version
    assert service.replayable_content_versions() == frozenset(committed)
    assert len(set(committed)) == len(configurations)
    for name in GIFT_BOX_DEPENDENCIES:
        detached = copy(current)
        setattr(detached, name, None)
        assert build(detached).content_version in committed[1:]
    earlier = copy(current)
    earlier.item_catalog = item_catalogs.plain
    assert build(earlier).content_version in committed[1:]
    for malformed in ({}, {**configurations[0], "tickets": "not a digest"}):
        with pytest.raises(ProtocolError) as caught:
            service.content_version_for(malformed)
        assert (caught.value.code, caught.value.status) == (invalid, 503)
    for name in GIFT_BOX_DEPENDENCIES:
        unversioned = copy(current)
        setattr(unversioned, name, SimpleNamespace())
        with pytest.raises(ProtocolError) as caught:
            build(unversioned)
        assert (caught.value.code, caught.value.status) == (invalid, 503)
    changed = build(copy(current))
    changed.presents.tickets = SimpleNamespace()
    status = changed.status()
    assert (status["ready"], status["error"]) == (False, invalid)
    return service


def test_real_135_first_reward_and_same_day_idempotence(factory):
    case = factory()
    before = deepcopy(case.state)
    result, grants = case.service.settle(case.state, NOW)
    grant, = grants
    assert grant["campaign_id"] == 135 and grant["login_count"] == 1
    assert grant["earned_day"] == grant["delivery_day"] == "2026-11-01"
    assert grant["mail_ids"] == [1]
    assert case.presents.rows(result) == [{"PresentID": 1, "Type": 1, "Content": 1016,
                                         "Description": "イベントログインボーナス"}]
    assert case.state == before
    assert case.service.settle(result, NOW + timedelta(hours=1)) == (result, [])
    assert case.service.response(grants) == Container("EBNS", (Column("LoginBonusID", "I", (135,)),
        Column("LoginCount", "I", (1,)), Column("PurchaseTime", "S", ("",))))


def test_all_ten_real_rewards_follow_source_order_without_wallet_or_unit_changes(factory):
    case = factory()
    start = datetime(2026, 10, 22, 4, tzinfo=JST)
    result = deepcopy(case.state)
    observed = []
    for day in range(10):
        result, grants = case.service.settle(result, start + timedelta(days=day))
        assert grants[0]["login_count"] == day + 1
        observed.append(case.presents.rows(result)[-1]["Content"])
    assert observed == [reward.item_param for reward in case.catalog.get(135).rewards]
    assert case.service.settle(result, start + timedelta(days=10)) == (result, [])
    assert result["player_state"] == case.state["player_state"]
    assert result["tables"]["crystals"] == case.state["tables"]["crystals"]


def test_jst_day_rollover_advances_once_without_backfill(factory):
    case = factory()
    result, _ = case.service.settle(case.state, NOW)
    before_cutoff = datetime(2026, 11, 2, 3, 59, 59, tzinfo=JST)
    assert case.service.settle(result, before_cutoff) == (result, [])
    result, grants = case.service.settle(result, before_cutoff + timedelta(seconds=1))
    assert grants[0]["login_count"] == 2
    result, grants = case.service.settle(result, NOW + timedelta(days=4))
    assert grants[0]["login_count"] == 3


def test_inactive_and_disabled_sources_do_not_create_empty_journals(factory):
    case = factory()
    assert case.service.settle(case.state, datetime(2026, 11, 6, 4, tzinfo=JST)) == (case.state, [])
    disabled = factory(enabled=False)
    assert disabled.service.settle(disabled.state, None) == (disabled.state, [])


@pytest.mark.parametrize("failure", ["present_capacity", "summon_ticket_pool_disabled"])
def test_recoverable_failure_is_pending_and_delivered_after_expiry(factory, monkeypatch, failure):
    case = factory()
    original = case.presents.enqueue_groups
    def unavailable(*_):
        raise ProtocolError(failure, "Unavailable", 409 if failure == "present_capacity" else 501)
    monkeypatch.setattr(case.presents, "enqueue_groups", unavailable)
    pending, grants = case.service.settle(case.state, NOW)
    assert grants == [] and case.presents.rows(pending) == []
    assert deliveries(pending)["pending"]["reason"] == failure
    monkeypatch.setattr(case.presents, "enqueue_groups", original)
    delivered, grants = case.service.settle(pending, datetime(2026, 11, 7, 12, tzinfo=JST))
    assert grants[0]["earned_day"] == "2026-11-01" and grants[0]["delivery_day"] == "2026-11-07"
    assert case.service.settle(delivered, datetime(2026, 11, 8, 12, tzinfo=JST)) == (delivered, [])


def test_multiple_rewards_for_one_ordinal_are_delivered_together(factory, source, monkeypatch):
    first = source[1].get(135).source_index
    case = factory(updates={first + 1: {"LoginCount": 0}, **{
        first + index: {"LoginCount": index} for index in range(2, 10)}})
    original = case.presents.enqueue_groups
    def limited(*_):
        raise ProtocolError("present_capacity", "No space", 409)
    monkeypatch.setattr(case.presents, "enqueue_groups", limited)
    pending, grants = case.service.settle(case.state, NOW)
    assert not grants and not case.presents.rows(pending)
    monkeypatch.setattr(case.presents, "enqueue_groups", original)
    result, grants = case.service.settle(pending, NOW)
    assert len(grants[0]["mail_ids"]) == 2
    assert [row["Content"] for row in case.presents.rows(result)] == [1016, 1080]


def test_one_campaign_failure_does_not_block_another(factory, source, monkeypatch):
    second = source[1].get(134).source_index
    case = factory(updates={second: {"EndDate": NOW.date().toordinal() + 9}}, ids=(134, 135))
    def validate(candidate, grants):
        if grants[-1]["campaign_id"] == 134:
            raise ProtocolError(LOGIN_REWARD_RESPONSE_CAPACITY, "Combined response too large", 503)
    result, grants = case.service.settle(case.state, NOW, validate)
    assert [grant["campaign_id"] for grant in grants] == [135]
    assert deliveries(result, 134)["pending"]["reason"] == LOGIN_REWARD_RESPONSE_CAPACITY
    assert len(case.presents.rows(result)) == 1


@pytest.mark.parametrize("failure", ["state_corrupt", "dependency_changed"])
def test_fatal_failures_preserve_original_state(factory, monkeypatch, failure):
    case = factory()
    def broken(*_):
        raise ProtocolError(failure, "Invalid state", 503)
    monkeypatch.setattr(case.presents, "enqueue_groups", broken)
    reject(case, lambda: case.service.settle(case.state, NOW), failure)


def test_callback_cannot_change_candidate_state(factory):
    case = factory()
    def altered(candidate, grants):
        candidate["unrelated"] = True
    reject(case, lambda: case.service.settle(case.state, NOW, altered), "event_login_validation_invalid")


@pytest.mark.parametrize("kind", ["unrelated", "reward"])
def test_mail_provider_cannot_change_unrelated_state_or_reward(factory, monkeypatch, kind):
    case = factory()
    original = case.presents.enqueue_groups
    def altered(*args):
        candidate, grouped = original(*args)
        if kind == "unrelated":
            candidate["unrelated"] = True
        else:
            grouped[0][0]["Content"] = 1080
            candidate["tables"]["presents"][-1]["Content"] = 1080
        return candidate, grouped
    monkeypatch.setattr(case.presents, "enqueue_groups", altered)
    reject(case, lambda: case.service.settle(case.state, NOW), "event_login_delivery_invalid")


def test_old_pending_is_preserved_when_campaign_disappears(factory):
    case = factory(ids=None)
    pending = begin_event_login(case.state, 2000, 1, "a" * 64, NOW)
    result, grants = case.service.settle(pending, NOW)
    assert any(grant["campaign_id"] == 135 for grant in grants)
    assert deliveries(result, 2000)["pending"]["reason"] == "event_login_campaign_unavailable"


def test_pending_definition_change_is_not_silently_replaced(factory, source):
    old = factory()
    pending = begin_event_login(old.state, 135, 1, event_login_definition(old.catalog.get(135), 1), NOW)
    current = factory(updates={source[1].get(135).source_index: {"ItemParam": 1080}})
    result, grants = current.service.settle(pending, NOW)
    assert grants == [] and not current.presents.rows(result)
    assert deliveries(result)["pending"]["reason"] == "event_login_definition_changed"


def test_receipt_uses_current_reward_definition_and_present_content(factory):
    case = factory()
    result, grants = case.service.settle(case.state, NOW)
    forged = deepcopy(grants)
    forged[0]["definition"] = "b" * 64
    candidate = deepcopy(result)
    candidate["event_login"]["campaigns"]["135"]["deliveries"]["1"]["definition"] = "b" * 64
    reject(case, lambda: case.service.validate_receipt(candidate, forged), "event_login_receipt_invalid")
    candidate = deepcopy(result)
    candidate["tables"]["presents"][0]["Content"] = 1080
    reject(case, lambda: case.service.validate_receipt(candidate, grants), "event_login_receipt_invalid")


def test_received_mail_and_changed_catalog_do_not_block_historical_initialization(factory, source):
    old = factory()
    result, grants = old.service.settle(old.state, NOW)
    result["tables"]["presents"] = []
    old.service.validate_receipt(result, grants)
    current = factory(updates={source[1].get(135).source_index: {"ItemParam": 1080}})
    assert current.service.initialize(result) == result
    current.service.validate_receipt(result, grants, verify_current=False)
    reject(current, lambda: current.service.validate_receipt(result, grants), "event_login_receipt_invalid")


def test_disabled_service_preserves_pending_and_delivery_history(factory):
    case = factory()
    pending = begin_event_login(case.state, 135, 1, event_login_definition(case.catalog.get(135), 1), NOW)
    disabled = factory(enabled=False)
    assert disabled.service.settle(pending, NOW + timedelta(days=30)) == (pending, [])


def test_malformed_pending_is_not_ignored_by_empty_receipt_validation(factory):
    case = factory()
    pending = begin_event_login(case.state, 135, 1, event_login_definition(case.catalog.get(135), 1), NOW)
    pending["event_login"]["campaigns"]["135"]["pending"]["ordinal"] = 4
    reject(case, lambda: case.service.validate_receipt(pending, []), "event_login_state_invalid")


def test_real_selection_ticket_uses_source_config_and_single_coupon_mail(factory):
    case = factory(ids=(55,))
    result, grants = case.service.settle(case.state, datetime(2024, 11, 8, 12, tzinfo=JST))
    assert grants[0]["login_count"] == 1
    assert case.presents.rows(result) == [{"PresentID": 1, "Type": 20, "Content": 1,
        "SelectionConfigID": 148, "Description": "イベントログインボーナス"}]


@pytest.mark.parametrize("amount", [2, 501])
def test_recovery_items_split_into_individual_mail_or_preserve_capacity_pending(factory, source, amount):
    first = source[1].get(135).source_index
    case = factory(updates={first: {"ItemType": 22, "ItemParam": amount, "ItemID": "item_cook_logbo_00004"}})
    result, grants = case.service.settle(case.state, NOW)
    if amount == 2:
        assert len(grants[0]["mail_ids"]) == 2
        assert all(row["Type"] == 22 and row["Content"] == 1 for row in case.presents.rows(result))
    else:
        assert not grants and not case.presents.rows(result)
        assert deliveries(result)["pending"]["reason"] == "present_capacity"


def test_item_beyond_the_native_cell_stays_pending_instead_of_failing_115(factory, source, monkeypatch):
    item = "x" * STORED_ITEM_NAME_BYTES
    first = source[1].get(135).source_index
    case = factory(updates={first: {"ItemType": 22, "ItemParam": 1, "ItemID": item}})
    accept_stored_item(monkeypatch, case.presents, item)
    result, grants = case.service.settle(case.state, NOW)
    assert not grants and not case.presents.rows(result)
    assert deliveries(result)["pending"]["reason"] == "event_login_reward_unsupported"


def test_gift_box_versions_read_the_current_wiring_of_the_mailbox_on_lookup(bundle):
    presents = copy(bundle.presents)
    versions = GiftBoxVersions(presents)
    wiring = dict(presents.gift_box_configurations()[0])
    assert tuple(versions) == GIFT_BOX_DEPENDENCIES and dict(versions) == wiring
    presents.item_tickets = None
    assert dict(versions) == {**wiring, "item_tickets": None}
    assert dict(GiftBoxVersions(SimpleNamespace())) == dict.fromkeys(GIFT_BOX_DEPENDENCIES)
    unversioned = GiftBoxVersions(SimpleNamespace(tickets=SimpleNamespace()))
    assert "tickets" in unversioned and "vote_cooking_catalog" not in unversioned
    assert unversioned[ITEM_CATALOG] is None
    with pytest.raises(AttributeError):
        unversioned["tickets"]
    with pytest.raises(KeyError):
        unversioned["vote_cooking_catalog"]


def test_receipts_of_every_gift_box_configuration_replay_and_others_stay_stale(factory, item_catalogs):
    case = factory()
    service = assert_gift_box_receipts_replay(
        lambda presents: EventLoginService(case.catalog, presents, case.service.rules),
        case.presents, item_catalogs, "event_login_dependencies_invalid")
    assert factory(ids=(134,)).service.content_version not in service.replayable_content_versions()

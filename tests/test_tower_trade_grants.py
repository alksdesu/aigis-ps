from copy import copy, deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.tower_trade_catalog import TowerTradeCatalog
import aigis_ps.tower_trade_grants as module
from aigis_ps.tower_trade_grants import TowerTradeGrants
from test_quest_service import bundle


@pytest.fixture(scope="module")
def resources(bundle):
    catalog = TowerTradeCatalog(lambda name: bundle.native_cdn.assets.read(name)[0],
        bundle.possessions, bundle.gifts.catalog, emblems=bundle.presents.emblem_catalog)
    return SimpleNamespace(bundle=bundle, catalog=catalog)


@pytest.fixture
def case(resources):
    players = copy(resources.bundle.player_state)
    helper = TowerTradeGrants(players, resources.bundle.possessions, resources.catalog)
    state = players.initialize(resources.bundle.bootstrap.initial_state())
    state["sentinel"] = {"nested": [1, 2]}
    return SimpleNamespace(players=players, possessions=resources.bundle.possessions,
        catalog=resources.catalog, helper=helper, state=state,
        unit=next(product for product in resources.catalog.products if product.card_id),
        item=next(product for product in resources.catalog.products
                  if product.item_effect_type == 4 and not product.unsupported_reason))


def fail(case, product, quantity=1, code=None):
    before = deepcopy(case.state)
    with pytest.raises(ProtocolError) as caught:
        case.helper.apply(case.state, product, quantity)
    assert case.state == before
    if code:
        assert caught.value.code == code
    return caught.value


def test_current_catalog_reports_every_supported_product_and_each_blocked_voucher(case):
    status = case.helper.status()
    assert status["ready"] is True and status["supported_products"] == 164
    vouchers = [product for product in case.catalog.products if product.item_effect_type == 2]
    assert len(vouchers) == 6
    assert set(status["unavailable"]) == {product.shop_id for product in vouchers}
    assert set(status["unavailable"].values()) == {"summon_pool_unverified"}
    assert all(product.item_stack == 0 for product in vouchers)
    assert status["unit_quantity_limit"] == 100 and status["native_tested"] is False
    assert len(status["content_version"]) == 64


def test_all_ninety_unit_products_use_verified_current_initialization(case):
    products = [product for product in case.catalog.products if product.card_id]
    assert len(products) == 90
    before = deepcopy(case.state)
    for product in products:
        updated, units, changed = case.helper.apply(case.state, product, 1)
        initial = case.players.initial_unit_values(product.card_id, 0, evolution_count=0)
        assert units == [case.helper.units.expected_unit(product.card_id, initial, 1, 2)], product.shop_id
        assert updated["player_state"]["units"][-1] == units[0]
        assert changed == []
        assert updated["tables"] == before["tables"]
        assert case.state == before


def test_all_seventy_four_supported_items_credit_only_selected_possession(case):
    products = [product for product in case.catalog.products if product.item_id and not product.unsupported_reason]
    assert len(products) == 74
    assert sum(product.item_effect_type == 10 for product in products) == 66
    before = deepcopy(case.state)
    for product in products:
        assert product.item_stack > 0
        case.helper.require_supported(product)
        updated, units, changed = case.helper.apply(case.state, product, 1)
        expected = case.possessions.credit(before, {product.item_id: 1})
        assert updated == expected, product.shop_id
        assert units == [] and changed == [product.item_id]
        assert case.state == before


def test_six_incomplete_summon_vouchers_fail_before_any_grant(case, monkeypatch):
    monkeypatch.setattr(case.possessions, "credit", lambda *_: pytest.fail("unverified item reached credit"))
    monkeypatch.setattr(case.players, "credit_units", lambda *_: pytest.fail("unverified item reached unit grant"))
    for product in case.catalog.products:
        if product.item_effect_type == 2:
            with pytest.raises(ProtocolError) as caught:
                case.helper.require_supported(product)
            assert caught.value.code == "tower_trade_grant_unsupported"
            assert caught.value.status == 501
            fail(case, product, code="tower_trade_grant_unsupported")


@pytest.mark.parametrize("quantity", [None, 0, -1, True, False, 1.0, "1", 2**31, [], {}])
def test_invalid_quantities_fail_without_calling_provider(case, monkeypatch, quantity):
    monkeypatch.setattr(case.players, "credit_units", lambda *_: pytest.fail("bad quantity reached unit credit"))
    monkeypatch.setattr(case.possessions, "credit", lambda *_: pytest.fail("bad quantity reached inventory credit"))
    fail(case, case.unit, quantity, "tower_trade_grant_invalid")
    fail(case, case.item, quantity, "tower_trade_grant_invalid")


@pytest.mark.parametrize("change", [None, {"shop_id": 9999999}, {"card_id": 17}, {"item_id": "fake"},
    {"tiers": ((1, 1),)}, {"unsupported_reason": "fake"}, {"shop_id": True}])
def test_copied_or_altered_products_are_never_catalog_authority(case, change):
    product = replace(case.unit, **(change or {}))
    fail(case, product, code="tower_trade_grant_product_invalid")


@pytest.mark.parametrize("product", [None, 1, {}, SimpleNamespace(shop_id=10000)])
def test_unknown_product_shapes_are_rejected(case, product):
    fail(case, product, code="tower_trade_grant_product_invalid")


def test_unit_limit_and_identity_high_water_marks(case):
    case.state["player_state"]["battle"]["UX"] = 300
    case.state["player_state"]["last_unique_id"] = 600
    updated, units, changed = case.helper.apply(case.state, case.unit, 100)
    assert len(units) == 100 and changed == []
    assert units[0]["GetIndex"] == 301 and units[-1]["UniqueID"] == 700
    assert updated["player_state"]["battle"]["UX"] == 400
    fail(case, case.unit, 101, "tower_trade_grant_invalid")


def test_barracks_capacity_rejects_whole_unit_batch(case, monkeypatch):
    case.players.capacity_provider = lambda _: 1
    monkeypatch.setattr(case.players, "credit_units", lambda *_: pytest.fail("full barracks reached credit"))
    fail(case, case.unit, 2, "unit_roster_capacity")


def test_active_battle_reward_reservations_keep_their_unit_identity_space(case, monkeypatch):
    case.state["player_state"]["last_unique_id"] = 0x7FFFFFFE
    case.state["quest_state"] = {"active": {"unit_reservations": [
        {"card_id": case.unit.card_id, "level": 0, "count": 1}]}}
    monkeypatch.setattr(case.players, "credit_units", lambda *_: pytest.fail("reserved identity reached credit"))
    fail(case, case.unit, code="unit_roster_capacity")


def test_legacy_active_battle_without_reservations_cannot_receive_units(case):
    case.state["quest_state"] = {"active": {}}
    fail(case, case.unit, code="quest_reservation_unavailable")


def test_item_stack_boundary_and_enabled_are_preserved(case):
    case.state["tables"]["possessions"].append(
        {"ItemID": case.item.item_id, "Count": case.item.item_stack - 1, "Enabled": 1})
    updated, units, changed = case.helper.apply(case.state, case.item, 1)
    assert updated["tables"]["possessions"][-1] == {
        "ItemID": case.item.item_id, "Count": case.item.item_stack, "Enabled": 1}
    assert units == [] and changed == [case.item.item_id]
    fail(case, case.item, 2, "possession_stack_exceeded")


def test_old_over_stack_balance_is_readable_but_not_creditable(case):
    case.state["tables"]["possessions"].append({"ItemID": case.item.item_id, "Count": 0x7FFFFFFF})
    case.possessions.response(case.state)
    fail(case, case.item, 1, "possession_stack_exceeded")


@pytest.mark.parametrize("held", [0, 1])
def test_only_a_new_item_row_is_refused_at_the_possession_item_limit(case, monkeypatch, held):
    rows = case.state["tables"]["possessions"]
    rows[:] = [{"ItemID": currency, "Count": 1} for currency in case.catalog.currencies]
    monkeypatch.setattr(module, "MAX_POSSESSION_ITEMS", len(rows))
    fail(case, case.item, code="possession_grant_invalid")
    rows.append({"ItemID": case.item.item_id, "Count": held})
    monkeypatch.setattr(module, "MAX_POSSESSION_ITEMS", len(rows))
    updated, units, changed = case.helper.apply(case.state, case.item, 1)
    assert updated["tables"]["possessions"] == [*rows[:-1], {"ItemID": case.item.item_id, "Count": held + 1}]
    assert (units, changed) == ([], [case.item.item_id])


def test_readiness_never_builds_the_possession_status(case, monkeypatch):
    monkeypatch.setattr(case.possessions, "status", lambda: pytest.fail("readiness must not build the possession status"))
    helper = TowerTradeGrants(case.players, case.possessions, case.catalog)
    assert helper.status()["ready"] is True
    updated, units, changed = helper.apply(case.state, case.item, 1)
    assert updated == case.possessions.credit(case.state, {case.item.item_id: 1})
    assert (units, changed) == ([], [case.item.item_id])


def test_unready_possessions_are_changed_dependencies(case, monkeypatch):
    monkeypatch.setattr(case.possessions, "is_ready", lambda: False)
    monkeypatch.setattr(case.possessions, "credit", lambda *_: pytest.fail("unready inventory reached credit"))
    fail(case, case.item, code="tower_trade_grant_dependencies_changed")
    fail(case, case.unit, code="tower_trade_grant_dependencies_changed")
    assert case.helper.status()["error"] == "tower_trade_grant_dependencies_changed"


@pytest.mark.parametrize("fault", ["top", "unit", "other_item", "count", "enabled", "float", "missing"])
def test_corrupt_inventory_provider_cannot_change_unrelated_state(case, monkeypatch, fault):
    original = case.possessions.credit
    def credit(state, grants):
        updated = original(state, grants)
        if fault == "top":
            updated["unexpected"] = 1
        elif fault == "unit":
            updated["player_state"]["units"][0]["Love"] += 1
        elif fault == "other_item":
            updated["tables"]["possessions"][0]["Count"] += 1
        elif fault == "count":
            updated["tables"]["possessions"][-1]["Count"] += 1
        elif fault == "enabled":
            updated["tables"]["possessions"][-1]["Enabled"] = 1
        elif fault == "float":
            updated["sentinel"]["nested"][0] = 1.0
        else:
            del updated["sentinel"]
        return updated
    monkeypatch.setattr(case.possessions, "credit", credit)
    fail(case, case.item, code="tower_trade_grant_result_invalid")


def test_corrupt_unit_provider_is_rejected_by_shared_grant_validation(case, monkeypatch):
    original = case.players.credit_units
    def credit(state, specs):
        updated, units = original(state, specs)
        updated["sentinel"]["nested"].clear()
        return updated, units
    monkeypatch.setattr(case.players, "credit_units", credit)
    fail(case, case.unit, code="tower_trade_grant_result_invalid")


@pytest.mark.parametrize("kind", ["unit", "item"])
def test_provider_failure_cannot_mutate_callers_input(case, monkeypatch, kind):
    def broken(state, _):
        state["sentinel"]["nested"].clear()
        raise ProtocolError("injected", "Injected provider failure", 503)
    monkeypatch.setattr(case.players if kind == "unit" else case.possessions,
                        "credit_units" if kind == "unit" else "credit", broken)
    fail(case, getattr(case, kind), code="injected")


@pytest.mark.parametrize("kind", ["unit", "item"])
def test_returned_state_and_units_do_not_alias_caller(case, kind):
    before = deepcopy(case.state)
    updated, units, changed = case.helper.apply(case.state, getattr(case, kind), 1)
    updated["sentinel"]["nested"].clear()
    updated["player_state"]["units"][0]["Love"] = 10
    if units:
        units[0]["Love"] = 20
        assert updated["player_state"]["units"][-1]["Love"] == 0
    changed.append("irrelevant")
    assert case.state == before


def test_changed_dependency_during_inventory_credit_invalidates_result(case, monkeypatch):
    original = case.possessions.credit
    def credit(state, grants):
        updated = original(state, grants)
        monkeypatch.setattr(case.possessions, "asset_sha256", "f" * 64)
        return updated
    monkeypatch.setattr(case.possessions, "credit", credit)
    fail(case, case.item, code="tower_trade_grant_dependencies_changed")
    assert case.helper.status()["ready"] is False


def test_shared_catalog_inventory_is_required(resources):
    with pytest.raises(ProtocolError) as caught:
        TowerTradeGrants(resources.bundle.player_state, copy(resources.bundle.possessions), resources.catalog)
    assert caught.value.code == "tower_trade_grant_dependencies_invalid"

from copy import deepcopy
from dataclasses import replace
import struct
from types import SimpleNamespace

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.alxml import dumps, loads
from aigis_ps.player_state import UNIT_FIELDS
from aigis_ps.protocol import ProtocolRegistry
from aigis_ps.public_tables import MASTER_LIMITS
from aigis_ps.trade_catalog import TradeCatalog
from aigis_ps.trade_grants import TradeUnitGrants
from test_quest_service import bundle


@pytest.fixture(scope="module")
def components(bundle):
    catalog = TradeCatalog(bundle.gifts.catalog, bundle.possessions,
                           lambda name: bundle.native_cdn.assets.read(name)[0],
                           resource_version=bundle.gifts.catalog.resource_version)
    grants = TradeUnitGrants(bundle.player_state)
    return SimpleNamespace(bundle=bundle, catalog=catalog, grants=grants, players=bundle.player_state,
                           cards=bundle.gifts.catalog)


def initial(components):
    return components.players.initialize(components.bundle.bootstrap.initial_state())


def ordinary(components):
    return next(product for product in components.catalog.products
                if product.ordinary and product.product_type == 1 and product.unit_evolution_count == 0)


def fails_without_mutation(components, state, product, count, code):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        components.grants.grant(state, product, count)
    assert error.value.code == code
    assert state == before


@pytest.mark.parametrize("shop_id,card_id,class_id,skill_id", [(768, 2079, 141420, 2800), (769, 1989, 150920, 2703)])
def test_two_awakened_products_keep_stage_class_skill_and_zero_growth(components, shop_id, card_id, class_id, skill_id):
    state = initial(components)
    before = deepcopy(state)
    product = components.catalog.product(shop_id)
    assert product.unit_evolution_count == 2
    updated, units = components.grants.grant(state, product, 1)
    assert len(units) == 1
    unit = units[0]
    assert (unit["CardID"], unit["ClassID"], unit["ClassChangeCount"], unit["SettingSkillID"]) == (card_id, class_id, 2, skill_id)
    assert (unit["Exp"], unit["SkillLevel"], unit["SkillEvolved"], unit["Love"], unit["CostModMixValue"]) == (0, 1, 0, 0, 0)
    assert (unit["GeneralPurposeFlags"], unit["SkillExperience"], unit["CostExperience"], unit["ClassChangeStatusMod"]) == (1, 0, 0, 0)
    assert set(unit) == {name for name, _ in UNIT_FIELDS}
    assert updated["player_state"]["units"][-1] == unit
    assert state == before


def test_all_current_ordinary_unit_product_initializers(components):
    products = [product for product in components.catalog.products if product.ordinary and product.product_type == 1]
    assert len(products) == 1536
    assert {product.shop_id for product in products if product.unit_evolution_count} == {768, 769}
    state = initial(components)
    before = deepcopy(state)
    stages = {0: 0, 2: 0}
    for product in products:
        updated, units = components.grants.grant(state, product, 1)
        unit = units[0]
        card = components.cards.card(product.card_id)
        expected_stage = max(product.unit_evolution_count, 2 if card["Blood"] == 3 or card["ExcludeFameDisplayFirstClass"] else 0)
        expected_class = components.players.classes[card["InitClassID"]]["JobChange"] if expected_stage == 2 else card["InitClassID"]
        expected_skill = card["ClassLV0SkillID"]
        assert (unit["ClassID"], unit["ClassChangeCount"], unit["SettingSkillID"]) == (expected_class, expected_stage, expected_skill), product.shop_id
        assert (unit["Exp"], unit["SkillLevel"], unit["Love"], unit["CostModMixValue"], unit["SkillEvolved"]) == (0, 1, 0, 0, 0), product.shop_id
        assert updated["player_state"]["units"][-1] == unit
        assert state == before
        stages[unit["ClassChangeCount"]] += 1
    assert stages == {0: 1419, 2: 117}


def test_grant_is_pure_and_does_not_implement_request_replay(components):
    state = initial(components)
    product = ordinary(components)
    first, first_units = components.grants.grant(state, product, 2)
    repeated, repeated_units = components.grants.grant(state, product, 2)
    assert first == repeated and first_units == repeated_units
    assert set(first) == set(state)
    second, second_units = components.grants.grant(first, product, 1)
    assert second_units[0]["GetIndex"] > first_units[-1]["GetIndex"]
    assert second_units[0]["UniqueID"] > first_units[-1]["UniqueID"]
    assert len(second["player_state"]["units"]) == len(state["player_state"]["units"]) + 3


def test_candidate_mutations_do_not_alias_input_state(components):
    state = initial(components)
    state["unrelated"] = {"nested": [1, 2]}
    before = deepcopy(state)
    updated, units = components.grants.grant(state, ordinary(components), 1)
    updated["unrelated"]["nested"].append(3)
    updated["player_state"]["units"][0]["Love"] = 100
    units[0]["SkillLevel"] = 2
    assert state == before


def test_native_hero_minimum_stage_overrides_zero_product_metadata(components):
    product = components.catalog.product(348)
    card = components.cards.card(product.card_id)
    assert product.card_id == 1468 and product.unit_evolution_count == 0 and card["Blood"] == 3
    _, units = components.grants.grant(initial(components), product, 1)
    assert (units[0]["ClassChangeCount"], units[0]["ClassID"]) == (2, 150920)


def test_product_level_and_initial_stage_reach_shared_initializer(components):
    product = replace(components.catalog.product(768), unit_base_level=99, unit_skill_level=1)
    expected = components.players.initial_unit_values(product.card_id, 99, evolution_count=2)
    _, units = components.grants.grant(initial(components), product, 1)
    assert units[0]["Exp"] == expected.experience > 0
    assert units[0]["ClassID"] == expected.class_id
    assert units[0]["ClassChangeCount"] == expected.stage == 2
    assert units[0]["SkillLevel"] == 1


@pytest.mark.parametrize("count", [True, 0, -1, 1.0, 4097])
def test_invalid_quantities_are_rejected(components, count):
    fails_without_mutation(components, initial(components), ordinary(components), count, "trade_grant_invalid")


@pytest.mark.parametrize("changes", [
    {"unit_evolution_count": 1}, {"unit_evolution_count": 3}, {"unit_evolution_count": 4},
    {"unit_skill_level": 2}, {"unit_skill_evolution": 1}, {"unit_cost_modify": 1},
    {"unit_init_trust": 100}, {"unit_base_level": -1}, {"unit_base_level": True},
    {"unit_evolution_count": False}, {"unit_skill_level": True}, {"unit_skill_evolution": False},
    {"unit_cost_modify": False}, {"unit_init_trust": False},
])
def test_unverified_initialization_variants_are_rejected(components, changes):
    product = replace(ordinary(components), **changes)
    fails_without_mutation(components, initial(components), product, 1, "trade_grant_unsupported")


def test_item_product_and_special_summon_are_not_direct_unit_grants(components):
    item = next(product for product in components.catalog.products if product.ordinary and product.product_type == 3)
    special = components.catalog.product(15)
    assert special.product_type == 2 and special.card_id == 10000 and not special.ordinary
    for product in (item, special):
        fails_without_mutation(components, initial(components), product, 1, "trade_grant_invalid")


def test_second_prince_product_keeps_the_unsupported_initialization_error(components):
    product = replace(ordinary(components), card_id=309)
    fails_without_mutation(components, initial(components), product, 1, "trade_grant_unsupported")


def test_awakened_card80_product_follows_its_classic_class_route(components):
    product = replace(components.catalog.product(768), card_id=80)
    _, units = components.grants.grant(initial(components), product, 1)
    unit = units[0]
    assert (unit["CardID"], unit["ClassID"], unit["ClassChangeCount"], unit["SettingSkillID"], unit["Exp"]) == (80, 730, 2, 47, 0)


def test_primary_barracks_space_is_required_before_allocating_identity(components, monkeypatch):
    state = initial(components)
    monkeypatch.setattr(components.players, "capacity_provider", lambda _: 0)
    fails_without_mutation(components, state, ordinary(components), 1, "unit_roster_capacity")


def test_native_identity_overflow_preserves_input(components):
    state = initial(components)
    state["player_state"]["last_unique_id"] = 0x7FFFFFFF
    fails_without_mutation(components, state, ordinary(components), 1, "unit_roster_capacity")


def test_final_unit_wire_capacity_is_checked_before_return(components, monkeypatch):
    limited = replace(ProtocolRegistry.load().by_registration[41], response_limit=1)
    monkeypatch.setattr(components.players, "_unit_response_profile", limited)
    fails_without_mutation(components, initial(components), components.catalog.product(768), 1, "unit_roster_capacity")


def test_existing_battle_reward_reservations_are_retained_and_preflighted(components, monkeypatch):
    state = initial(components)
    reservation = {"card_id": 3, "level": 1, "count": 1}
    state["quest_state"] = {"active": {"unit_reservations": [reservation]}}
    before = deepcopy(state)
    calls = []
    original = components.players.credit_units

    def record(candidate, grants):
        calls.append(deepcopy(grants))
        return original(candidate, grants)

    monkeypatch.setattr(components.players, "credit_units", record)
    updated, units = components.grants.grant(state, components.catalog.product(768), 1)
    assert [reservation] in calls
    assert updated["quest_state"] == before["quest_state"]
    assert len(updated["player_state"]["units"]) == len(before["player_state"]["units"]) + 1
    assert state == before


def test_changed_blood_resource_requires_new_grant_builder(components, monkeypatch):
    state = initial(components)
    raw = components.bundle.native_cdn.assets.read("PlayerBloodType.atb")[0]
    raw = decompress_all4(raw) if raw.startswith(b"ALL4") else raw
    table = parse_table(raw)
    result = bytearray(raw)
    field = next(field for field in table["fields"] if field["name"] == "_SystemTextID")
    struct.pack_into("<i", result, table["data_offset"] + field["offset"], table["rows"][0]["_SystemTextID"] + 1)
    reader = components.players.asset_reader
    monkeypatch.setattr(components.players, "asset_reader", lambda name: bytes(result) if name == "PlayerBloodType.atb" else reader(name))
    fails_without_mutation(components, state, ordinary(components), 1, "trade_grant_content_changed")
    changed = TradeUnitGrants(components.players)
    assert changed.content_version != components.grants.content_version
    _, units = changed.grant(state, components.catalog.product(348), 1)
    assert (units[0]["ClassChangeCount"], units[0]["ClassID"]) == (2, 150920)


def test_changed_public_master_requires_new_grant_builder_and_uses_new_values(components, monkeypatch):
    state = initial(components)
    product = components.catalog.product(348)
    provider = components.players.master_provider
    document = loads(provider("CardData"), limits=MASTER_LIMITS)
    columns = {column.name: column for column in document.children}
    index = columns["CardID"].values.index(product.card_id)
    skills = list(columns["ClassLV0SkillID"].values)
    skills[index] += 1
    new_column = replace(columns["ClassLV0SkillID"], values=tuple(skills))
    updated = replace(document, children=tuple(new_column if column.name == new_column.name else column
                                               for column in document.children))
    raw = dumps(updated, limits=MASTER_LIMITS)
    monkeypatch.setattr(components.players, "master_provider", lambda name: raw if name == "CardData" else provider(name))
    fails_without_mutation(components, state, product, 1, "trade_grant_content_changed")
    changed = TradeUnitGrants(components.players)
    assert changed.content_version != components.grants.content_version
    _, units = changed.grant(state, product, 1)
    assert units[0]["SettingSkillID"] == skills[index]

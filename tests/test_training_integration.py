from copy import copy
from itertools import product
import struct
from types import MappingProxyType

import lz4.frame
import pytest
from fastapi.testclient import TestClient

from aigis_ps.al_format import decompress_all4
from aigis_ps.alxml import loads
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.fairy_farm import FairyFarmService
from aigis_ps.player_state import PRINCE_CARD_IDS
from aigis_ps.protocol import decode_wire, encode_wire
from aigis_ps.storage import Store
from aigis_ps.training_catalog import TrainingCatalog, TrainingRules
from aigis_ps.training_math import derived_level
from aigis_ps.training_protocol import parse_training
from test_quest_service import bundle
from test_training_service import message


@pytest.fixture
def prepared(bundle, tmp_path):
    from types import SimpleNamespace
    assert bundle.training is not None, bundle.training_error
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, units = bundle.player_state.credit_units(state, [{"card_id": 3, "level": 1, "count": 3}])
    state["tables"]["game_param"][0]["Money"] = 100000
    return SimpleNamespace(bundle=bundle, service=bundle.training, catalog=bundle.training.catalog,
                           store=Store(tmp_path / "training.sqlite3", state), units=units)


def calculate(prepared, *, catalog=None, target=None):
    profile = prepared.store.profile()
    units = profile.state["player_state"]["units"]
    return (catalog or prepared.catalog).quote(profile.state["player_state"], profile.state["tables"]["config"][0],
        parse_training(message(prepared)), target or units[1], units[2:], state=profile.state)


def test_real_same_card_cost_and_skill_use_distinct_threshold_tables(prepared):
    quote = calculate(prepared)
    assert quote.price == 400
    assert dict(quote.values) == {"Exp": 200, "SkillLevel": 2, "CostModMixValue": -1,
                                  "SkillExperience": 2, "CostExperience": 0}


@pytest.mark.parametrize("card_id", sorted(PRINCE_CARD_IDS))
def test_catalog_refuses_prince_targets_on_its_own(prepared, card_id):
    units = prepared.store.profile().state["player_state"]["units"]
    card = prepared.catalog.cards.card(card_id)
    prince = next(unit for unit in units if unit["CardID"] in PRINCE_CARD_IDS)
    target = {**prince, "CardID": card_id, "ClassID": card["InitClassID"], "SettingSkillID": card["ClassLV0SkillID"]}
    with pytest.raises(ProtocolError) as caught:
        calculate(prepared, target=target)
    assert caught.value.code == "training_target_ineligible"


def test_old_zero_skill_level_uses_the_native_effective_minimum(prepared):
    profile = prepared.store.profile()
    profile.state["player_state"]["units"][1]["SkillLevel"] = 0
    prepared.store.update_state(profile.state, profile.revision)
    quote = calculate(prepared)
    assert quote.values["SkillLevel"] == 2 and quote.values["SkillExperience"] == 2


def test_different_cards_with_matching_skills_grow_skill_without_reducing_cost(prepared):
    profile = prepared.store.profile()
    state, units = prepared.bundle.player_state.credit_units(profile.state, [
        {"card_id": 5, "level": 1, "count": 1}, {"card_id": 124, "level": 1, "count": 1}])
    prepared.store.update_state(state, profile.revision)
    prepared.units = units
    prepared.service.integrate(message(prepared), prepared.store)
    target = next(unit for unit in prepared.store.profile().state["player_state"]["units"]
                  if unit["GetIndex"] == units[0]["GetIndex"])
    assert target["SkillLevel"] == 2 and target["SkillExperience"] == 0
    assert target["CostModMixValue"] == 0 and target["CostExperience"] == 0


def test_retain_overflow_preserves_experience_and_caps_only_display_level(prepared):
    profile = prepared.store.profile()
    target = profile.state["player_state"]["units"][1]
    data = prepared.catalog.describe(target)
    thresholds, cap = data.thresholds, data.cap
    target["Exp"] = thresholds[cap - 1] + 10
    prepared.store.update_state(profile.state, profile.revision)
    quote = calculate(prepared)
    assert quote.values["Exp"] == target["Exp"] + 200
    assert derived_level(quote.values["Exp"], thresholds, cap) == cap
    assert quote.price == 2 * (160 + 40 * cap)


def test_optional_clamp_rule_never_removes_preexisting_excess_experience(prepared):
    old = prepared.catalog
    catalog = TrainingCatalog(old.cards, prepared.bundle.player_state,
        lambda name: prepared.bundle.native_cdn.assets.read(name)[0], old.aliases,
        rules=TrainingRules(retain_overflow_experience=False))
    profile = prepared.store.profile()
    target = profile.state["player_state"]["units"][1]
    data = catalog.describe(target)
    target["Exp"] = data.thresholds[data.cap - 1] + 10
    prepared.store.update_state(profile.state, profile.revision)
    assert calculate(prepared, catalog=catalog).values["Exp"] == target["Exp"]
    assert catalog.content_version != old.content_version


def test_experience_int32_overflow_is_rejected_before_mutation(prepared):
    profile = prepared.store.profile()
    profile.state["player_state"]["units"][1]["Exp"] = 0x7fffffff
    prepared.store.update_state(profile.state, profile.revision)
    before = prepared.store.profile()
    with pytest.raises(ProtocolError) as caught:
        prepared.service.integrate(message(prepared), prepared.store)
    assert caught.value.code == "training_experience_capacity"
    assert prepared.store.profile() == before


@pytest.mark.parametrize("card_id,gain", [(54, 500), (55, 1250), (56, 500), (57, 500), (58, 500), (59, 500)])
def test_fairy_experience_uses_matching_rarity_multiplier(prepared, card_id, gain):
    profile = prepared.store.profile()
    state, units = prepared.bundle.player_state.credit_units(profile.state, [{"card_id": card_id, "level": 1, "count": 1}])
    prepared.store.update_state(state, profile.revision)
    prepared.service.integrate(message(prepared, materials=units), prepared.store)
    assert prepared.store.profile().state["player_state"]["units"][1]["Exp"] == gain


@pytest.mark.parametrize("domain", ["fame_depository", "fairy_farm"])
def test_existing_depository_is_not_ignored_for_bonus_computation(prepared, domain):
    profile = prepared.store.profile()
    profile.state["tables"][domain] = [{"CardID": 416}]
    prepared.store.update_state(profile.state, profile.revision)
    with pytest.raises(ProtocolError) as caught:
        calculate(prepared)
    assert caught.value.code == ("fame_state_invalid" if domain == "fame_depository" else "fairy_farm_state_invalid")


def test_http_synthesis_reload_and_persistent_retry_use_real_resources(prepared, tmp_path):
    bundle = prepared.bundle
    initial = prepared.store.profile().state
    def application():
        return create_app(tmp_path / "http", initial_state=initial,
            handlers=bundle.bootstrap.handlers() | bundle.player_state.handlers() | bundle.training.handlers())
    def post(client, envelope):
        result = client.post("/api/" + envelope.profile.route,
                             content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert result.status_code == 200, result.text
        return result.content, loads(decode_wire(result.content, envelope.profile.response_key))
    app = application()
    from test_quest_service import request
    with TestClient(app, base_url="http://127.0.0.1") as client:
        prepared.store = app.state.store
        envelope = message(prepared)
        wire, result = post(client, envelope)
        assert next(child for child in result.children if child.name == "PPU").children[1].values == (200,)
        before = app.state.store.profile()
        _, roster = post(client, request(prepared.store, 41, ()))
        columns = {child.name: child.values for child in roster.children}
        assert columns["A1"] == (1, 3) and columns["A4"] == (0, 200)
        assert columns["B2"] == (0, 2) and columns["AA"] == (0, -1)
        assert post(client, envelope)[0] == wire
        assert app.state.store.profile() == before
    reopened = application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == before


def overridden(prepared, overrides):
    profile = prepared.store.profile()
    profile.state["unit_dynamic_overrides"] = overrides
    prepared.store.update_state(profile.state, profile.revision)
    return prepared.store.profile()


def refused(prepared, code="training_recipe_unsupported"):
    before = prepared.store.profile()
    with pytest.raises(ProtocolError) as caught:
        prepared.service.integrate(message(prepared), prepared.store)
    assert (caught.value.code, caught.value.status) == (code, 501)
    assert prepared.store.profile() == before


@pytest.mark.parametrize("overrides", [None, {}, []])
def test_absent_or_empty_dynamic_overrides_do_not_block_training(prepared, overrides):
    before = overridden(prepared, overrides)
    prepared.service.integrate(message(prepared), prepared.store)
    assert prepared.store.profile().revision == before.revision + 1


@pytest.mark.parametrize("overrides", [{"unit": 1}, [1]])
def test_battle_only_dynamic_overrides_refuse_training_without_side_effects(prepared, overrides):
    overridden(prepared, overrides)
    refused(prepared)


def test_current_character_abilities_never_reach_influence_214(prepared):
    catalog = prepared.catalog
    cards = {identifier for card_id in catalog.cards.card_ids
             for identifier in (card_id, catalog.aliases.resolve(card_id).image_card_id)}
    configs = {catalog._ability_list[catalog.cards.card(card_id)[field]]["_ConfigID"]
               for card_id in cards for field in ("Ability", "Ability_Default")}
    assert not catalog.effects.find(tuple(configs), 214)


def test_resource_ability_reaching_influence_214_refuses_training(prepared, monkeypatch):
    catalog = prepared.catalog
    config = next(key for key, effects in catalog.effects.abilities.items() if any(effect.kind == 214 for effect in effects))
    index = catalog.cards.card(prepared.units[1]["CardID"])["Ability"]
    rows = list(catalog._ability_list)
    rows[index] = MappingProxyType({**rows[index], "_ConfigID": config})
    monkeypatch.setattr(catalog, "_ability_list", tuple(rows))
    refused(prepared)


def test_current_conditional_training_effects_use_only_supported_predicates(prepared):
    catalog = prepared.catalog
    target = catalog.describe(prepared.store.profile().state["player_state"]["units"][1])
    effects = [effect for effects in catalog.effects.abilities.values() for effect in effects if effect.kind == 138]
    assert not [effect for effect in effects if any((effect.activation, effect.extension, effect.unchanged_condition))]
    commands = [effect.command.strip() for effect in effects if effect.command.strip()]
    expressions = [command for command in commands if command not in ("1", "1;", "0", "0;")]
    assert (len(commands), len(expressions)) == (56, 5)
    for command in expressions:
        assert catalog._condition(command, target) in (True, False)


def repacked(raw):
    if raw.startswith(b"ALL4"):
        return decompress_all4(raw)
    return struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)


@pytest.mark.parametrize("name", ["AbilityConfig.atb", "AbilityList.atb", "UnitSpecialty.atb"])
def test_effect_resource_identity_is_part_of_the_training_content_version(prepared, name):
    old = prepared.catalog
    read = lambda resource: prepared.bundle.native_cdn.assets.read(resource)[0]
    def build(reader):
        return TrainingCatalog(old.cards, prepared.bundle.player_state, reader, old.aliases, rules=old.rules,
                               fairy_farm=old.fairy_farm, silver_house=old.silver_house)
    baseline = build(read)
    changed = build(lambda resource: repacked(read(resource)) if resource == name else read(resource))
    before, after = baseline.status()["resource_sha256"], changed.status()["resource_sha256"]
    assert {key for key in before if before[key] != after[key]} == {name}
    assert changed.content_version != baseline.content_version


def test_replayable_versions_cover_every_warehouse_configuration(prepared):
    old, bundle = prepared.catalog, prepared.bundle
    read = lambda resource: bundle.native_cdn.assets.read(resource)[0]
    assert old.fairy_farm.barracks is not None and old.silver_house is not None
    unbarracked = FairyFarmService(bundle.bootstrap, bundle.player_state, bundle.gifts, read)
    catalogs = {(farm, house): TrainingCatalog(old.cards, bundle.player_state, read, old.aliases, rules=old.rules,
                                               fairy_farm=farm, silver_house=house)
                for farm, house in product((old.fairy_farm, unbarracked, None), (old.silver_house, None))}
    versions = {catalog.content_version for catalog in catalogs.values()}
    assert len(versions) == len(catalogs) and old.content_version in versions
    assert old.replayable_content_versions() == versions
    assert catalogs[unbarracked, old.silver_house].replayable_content_versions() < versions
    bare = catalogs[None, None]
    assert bare.replayable_content_versions() == {bare.content_version}


def test_replayable_versions_read_the_warehouse_versions_once(prepared, monkeypatch):
    versions = prepared.catalog.replayable_content_versions()
    monkeypatch.setattr(prepared.catalog.fairy_farm, "replayable_content_versions",
                        lambda: pytest.fail("Training must keep the warehouse versions it derived"))
    assert prepared.catalog.replayable_content_versions() == versions


def test_catalog_status_reports_native_exclusions_and_resource_tripwires(prepared):
    status = prepared.catalog.status()
    assert "unsupported" not in status
    assert (len(status["native_exclusions"]), len(status["resource_tripwires"])) == (1, 2)


def test_growth_tables_are_read_only_views_of_the_current_tables(prepared):
    catalog = copy(prepared.catalog)
    assert (catalog.classes, catalog.rarities) == (catalog._classes, catalog._rarities)
    assert (catalog.skills, catalog.experience) == (catalog._skills, catalog._experience)
    assert type(catalog.skills) is type(catalog.experience) is tuple
    for view in (catalog.classes, catalog.rarities):
        with pytest.raises(TypeError):
            view[0] = {}
    catalog._classes, catalog._rarities, catalog._skills, catalog._experience = {}, {}, [], [0]
    assert (catalog.classes, catalog.rarities, catalog.skills, catalog.experience) == ({}, {}, (), (0,))

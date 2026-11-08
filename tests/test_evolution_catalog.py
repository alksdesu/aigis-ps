from copy import copy, deepcopy
from dataclasses import FrozenInstanceError

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.evolution_catalog import AWAKENING_EXCLUDED_CARD_IDS, EvolutionCatalog, EvolutionRules
from aigis_ps.evolution_materials import EvolutionMaterials
from aigis_ps.player_state import UNIT_BOOLEANS, UNIT_FIELDS
from test_quest_service import bundle


@pytest.fixture(scope="module")
def catalog(bundle):
    growth = bundle.training.catalog
    materials = EvolutionMaterials(growth.cards, growth.classes, growth.rarities)
    return EvolutionCatalog(growth, bundle.possessions, materials)


def unit(catalog, card_id=7, *, level=1, stage=0, love=100):
    card = catalog.cards.card(card_id)
    row = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
    row.update(CardID=card_id, ClassID=catalog.route(card, stage)["ClassID"], ClassChangeCount=stage,
        SettingSkillID=card["ClassLV0SkillID"] if stage == 0 else card["ClassLV1SkillID"],
        Love=love, SkillLevel=1, UniqueID=card_id + 2, GetIndex=card_id + 1, GeneralPurposeFlags=1,
        OrganizationIndex=-1)
    data = catalog.growth.describe(row)
    row["Exp"] = data.thresholds[min(level, data.cap) - 1]
    return row


def materials(catalog, target, operation):
    result = []
    for alternatives in catalog.materials.slots(catalog.growth.describe(target), operation):
        selected = None
        for identifier in catalog.cards.card_ids:
            card = catalog.cards.card(identifier)
            cls = card["InitClassID"]
            if any(condition._matches((identifier, cls, card["Rare"], 0, 30)) for condition in alternatives):
                selected = unit(catalog, identifier, level=30, love=0)
                break
        assert selected is not None
        result.append(selected)
    return result


@pytest.mark.parametrize("mode", ["overflow", "total", "reset"])
def test_first_awake_experience_policy_and_unchanged_growth(catalog, mode):
    configured = EvolutionCatalog(catalog.growth, catalog.possessions, catalog.materials,
                                  rules=EvolutionRules(first_awake_experience=mode))
    target = unit(configured, level=999)
    target["Exp"] += 123
    target.update(CostModMixValue=-1, SkillExperience=2, CostExperience=1, _DisplayType=2)
    before = deepcopy(target)
    quote = configured.quote(target, materials(configured, target, 7), 7)
    assert quote.values == {"ClassID": 120, "ClassChangeCount": 2,
        "Exp": {"overflow": 123, "total": target["Exp"], "reset": 0}[mode], "_DisplayType": 0}
    assert quote.price == 100000 and quote.reward_card_id is None
    assert target == before
    assert configured.content_version == catalog.content_version if mode == "overflow" else configured.content_version != catalog.content_version


def test_content_versions_are_derived_from_the_growth_versions(catalog):
    growth = catalog.growth
    assert catalog.content_version_for(growth.content_version) == catalog.content_version
    assert catalog.replayable_content_versions() == {catalog.content_version_for(version)
                                                     for version in growth.replayable_content_versions()}
    assert catalog.content_version in catalog.replayable_content_versions()


def test_replayable_versions_follow_the_growth_set(catalog):
    versions = frozenset(("a" * 64, "b" * 64))
    growth = copy(catalog.growth)
    growth.content_version, growth.replayable_content_versions = "a" * 64, lambda: versions
    other = EvolutionCatalog(growth, catalog.possessions, catalog.materials)
    assert other.content_version == catalog.content_version_for("a" * 64) != catalog.content_version
    assert other.replayable_content_versions() == {catalog.content_version_for(version) for version in versions}


def test_exact_cap_has_no_excess_to_carry(catalog):
    target = unit(catalog, level=999)
    assert catalog.quote(target, materials(catalog, target, 7), 7).values["Exp"] == 0


def test_awakening_quotes_read_every_unit_through_the_public_growth_description(catalog):
    target = unit(catalog, level=999)
    selected = materials(catalog, target, 7)
    growth, described = copy(catalog.growth), []

    def recorded(row):
        described.append(row)
        return catalog.growth.describe(row)

    growth.describe = recorded
    quote = EvolutionCatalog(growth, catalog.possessions, catalog.materials).quote(target, selected, 7)
    assert quote == catalog.quote(target, selected, 7)
    assert described == [target, *selected, {**target, **quote.values}]


@pytest.mark.parametrize("card_id", sorted(AWAKENING_EXCLUDED_CARD_IDS))
def test_awakening_menus_exclude_the_princes_and_the_classic_route_card_by_identity(catalog, monkeypatch, card_id):
    assert AWAKENING_EXCLUDED_CARD_IDS == {1, 80, 309}
    target = unit(catalog, level=999)
    selected = materials(catalog, target, 7)
    lookup = catalog.cards.card
    ordinary_route = {**lookup(target["CardID"]), "CardID": card_id}
    monkeypatch.setattr(catalog.cards, "card", lambda identifier: ordinary_route if identifier == card_id else lookup(identifier))
    target["CardID"] = card_id
    with pytest.raises(ProtocolError) as caught:
        catalog.quote(target, selected, 7)
    assert caught.value.code == "evolution_target_ineligible"


@pytest.mark.parametrize("changes", [{"Love": 99}, {"Exp": 0}, {"ClassChangeCount": 2}, {"ClassID": 120}])
def test_first_awake_rejects_unmet_requirements(catalog, changes):
    target = unit(catalog, level=999)
    selected = materials(catalog, target, 7)
    target.update(changes)
    with pytest.raises(ProtocolError) as caught:
        catalog.quote(target, selected, 7)
    assert caught.value.code == "evolution_target_ineligible"


def test_second_awake_does_not_require_max_level_and_preserves_experience(catalog):
    target = unit(catalog, stage=2)
    target["Exp"] = 23
    quote = catalog.quote(target, materials(catalog, target, 9), 9, 2)
    assert quote.values == {"ClassID": catalog.classes[120]["AwakeType2"], "ClassChangeCount": 4,
                            "Exp": 23, "_DisplayType": 0}
    with pytest.raises(ProtocolError) as caught:
        catalog.quote(target, materials(catalog, target, 9), 9, 1)
    assert caught.value.code == "evolution_target_ineligible"


def test_black_second_awake_identifies_the_original_reward_card(catalog):
    target = unit(catalog, 17, stage=2)
    for branch in (1, 2):
        quote = catalog.quote(target, materials(catalog, target, 9), 9, branch)
        assert quote.reward_card_id == 17 and quote.price == 300000


def test_skill_awake_only_updates_the_native_receiver_fields(catalog):
    target = unit(catalog, stage=2, level=31)
    target["SkillLevel"] = catalog.growth.describe(target).skill_max
    target.update(SkillExperience=8, CostExperience=3, GeneralPurposeFlags=3)
    before = deepcopy(target)
    fairy = unit(catalog, 234)
    quote = catalog.quote(target, [fairy], 8)
    assert dict(quote.values) == {"SettingSkillID": 218, "GeneralPurposeFlags": 7, "SkillEvolved": 1}
    assert target == before and quote.material_assignment == (0,)


@pytest.mark.parametrize("changes", [{"SkillLevel": 1}, {"SkillEvolved": 1}, {"GeneralPurposeFlags": 5}, {"ClassChangeCount": 0}])
def test_skill_awake_cannot_be_bought_again_after_switching_back(catalog, changes):
    target = unit(catalog, stage=2)
    target["SkillLevel"] = catalog.growth.describe(target).skill_max
    target.update(changes)
    with pytest.raises(ProtocolError) as caught:
        catalog.quote(target, [unit(catalog, 234)], 8)
    assert caught.value.code == "evolution_target_ineligible"


def test_wrong_skill_fairy_is_rejected(catalog):
    target = unit(catalog, stage=2)
    target["SkillLevel"] = catalog.growth.describe(target).skill_max
    with pytest.raises(ProtocolError) as caught:
        catalog.quote(target, [unit(catalog, 136)], 8)
    assert caught.value.code == "evolution_material_mismatch"


def test_duplicate_orb_codes_are_charged_once(catalog):
    rarity = dict(catalog.rarities[6])
    cls = dict(catalog.classes[120])
    cls.update(Data_ExtraAwakeOrb1=10000100, Data_ExtraAwakeOrb2=10000100)
    price, costs = catalog.costs(rarity, cls)
    assert price == 300000 and dict(costs) == {"evolve_orb_100001": 3}


def test_skill_orbs_follow_real_reverse_route_instead_of_current_class(catalog):
    target = unit(catalog, 188, stage=2)
    source = catalog.skill_orb_class(catalog.classes[target["ClassID"]], 2)
    assert target["ClassID"] == 11530 and source["ClassID"] == 11500
    assert catalog.classes[11530]["Data_ExtraAwakeOrb1"] == 0
    assert {source["Data_ExtraAwakeOrb1"], source["Data_ExtraAwakeOrb2"]} == {10000200, 10000500}
    target = unit(catalog, 7, stage=4)
    assert catalog.skill_orb_class(catalog.classes[target["ClassID"]], 4)["ClassID"] == 100


def test_reverse_parent_search_keeps_resource_order_and_missing_parent_fallback(catalog, monkeypatch):
    def row(identifier, change=0, one=0, two=0):
        return {"ClassID": identifier, "JobChange": change, "AwakeType1": one, "AwakeType2": two}
    classes = {900: row(900, 500), 100: row(100, 500), 500: row(500, one=600, two=601),
               600: row(600), 601: row(601), 700: row(700)}
    monkeypatch.setattr(catalog, "classes", classes)
    assert catalog.skill_orb_class(classes[500], 2)["ClassID"] == 900
    assert catalog.skill_orb_class(classes[600], 3)["ClassID"] == 900
    assert catalog.skill_orb_class(classes[601], 4)["ClassID"] == 900
    assert catalog.skill_orb_class(classes[700], 2) is classes[700]
    del classes[900]
    del classes[100]
    assert catalog.skill_orb_class(classes[600], 3)["ClassID"] == 500


@pytest.mark.parametrize("operation,branch", [(6, 0), (7.0, 0), (8, 1), (9, 0), (9, True), (9, 3)])
def test_invalid_operations_fail_before_any_calculation(catalog, operation, branch):
    with pytest.raises(ProtocolError) as caught:
        catalog.quote({}, [], operation, branch)
    assert caught.value.code == "evolution_operation_invalid"


@pytest.mark.parametrize("value", [None, True, 1, "anything", [], {}])
def test_unknown_experience_policy_is_rejected(value):
    with pytest.raises(ValueError):
        EvolutionRules(first_awake_experience=value)


def test_rules_and_quote_are_immutable(catalog):
    with pytest.raises(FrozenInstanceError):
        catalog.rules.first_awake_experience = "reset"
    target = unit(catalog, level=999)
    quote = catalog.quote(target, materials(catalog, target, 7), 7)
    with pytest.raises(TypeError):
        quote.values["Exp"] = -1
    with pytest.raises(TypeError):
        quote.items["anything"] = 1


def test_class_and_rarity_tables_are_read_only_views_of_the_growth_catalog(catalog):
    assert (catalog.classes, catalog.rarities) == (catalog.growth.classes, catalog.growth.rarities)
    for table in (catalog.classes, catalog.rarities):
        with pytest.raises(TypeError):
            table[0] = {}

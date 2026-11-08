from copy import copy, deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.card_status import CARD_STATUS_FIELDS
from aigis_ps.errors import ProtocolError
from aigis_ps.evolution_catalog import EvolutionCatalog
from aigis_ps.fairy_farm import FairyFarmService
from aigis_ps.fame_catalog import LIMITS, FameCatalog
from aigis_ps.player_state import MAX_PLAYER_UNITS, SKILL_SWITCH_FLAG, UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.system_literals import decode_command
from aigis_ps.training_catalog import TrainingCatalog
from aigis_ps.training_math import derived_level, experience_threshold
from test_image_aliases import command_table, encode
from test_quest_service import bundle


DEPOSIT_MENU = "OnCommandMenuUnitLibraryDeposit.atb"


@pytest.fixture(scope="module")
def catalog(bundle):
    return FameCatalog(bundle.training.catalog, bundle.evolution.catalog,
                       lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture(scope="module")
def unbarracked_farm(bundle):
    return FairyFarmService(bundle.bootstrap, bundle.player_state, bundle.gifts,
                            lambda name: bundle.native_cdn.assets.read(name)[0])


def unit(catalog, card_id, stage=None):
    if stage is None:
        stage = catalog.stages(card_id)[0]
    result = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
    result.update(CardID=card_id, GetIndex=17, UniqueID=1234, OrganizationIndex=-1, OrganizationIndex2=-1)
    result.update(catalog.maximum_values(card_id, stage))
    return result


def status_row(card_id, **changes):
    result = dict.fromkeys(CARD_STATUS_FIELDS, 0)
    result.update(CardID=card_id, **changes)
    return result


def test_complete_real_card_and_history_sets(catalog):
    assert len(catalog.card_ids) == 1312
    assert catalog.card_ids is catalog.eligible_card_ids
    assert len(catalog.counted_card_ids) == 1314
    assert len(catalog.support_card_ids) == 53
    assert len(catalog.card_ids & catalog.support_card_ids) == 51
    assert catalog.counted_card_ids - catalog.card_ids == {2695, 2696}
    assert (catalog.deposit_limit, catalog.deposit_batch_limit, catalog.rank_min) == (99, 10, 50)
    assert catalog.status()["deposit_batch_limit"] == 10 and catalog.status()["native_excluded_storage_stages"] == [1]
    assert catalog.snapshot == catalog.growth.cards.snapshot
    assert catalog.status()["ready"]
    for card_id in catalog.support_card_ids:
        assert 100 <= catalog.growth.cards.card(card_id)["Kind"] <= 120


def test_every_real_eligible_card_and_storage_stage_has_valid_maximum(catalog):
    checked = 0
    fields = {name for name, _ in UNIT_FIELDS}
    for card_id in sorted(catalog.card_ids):
        stages = catalog.stages(card_id)
        assert stages and len(set(stages)) == len(stages) and 1 not in stages, card_id
        for stage in stages:
            values = catalog.maximum_values(card_id, stage)
            assert set(values) == fields - {"CardID", "GetIndex", "UniqueID", "OrganizationIndex", "OrganizationIndex2"}
            assert all(type(value) is (bool if name in UNIT_BOOLEANS else int) for name, value in values.items())
            assert all(-0x80000000 <= value <= 0x7fffffff for name, value in values.items() if name not in UNIT_BOOLEANS)
            maximum = unit(catalog, card_id, stage)
            assert catalog.eligible(maximum), (card_id, stage, values)
            assert maximum["CanFame"] and maximum["Famed"]
            assert maximum["GeneralPurposeFlags"] in (1, 5)
            assert maximum["ClassChangeCount"] == stage
            assert maximum["CostModMixValue"] == -catalog.growth.cards.card(card_id)["CostDecValue"]
            assert maximum["SkillExperience"] == maximum["CostExperience"] == maximum["ClassChangeStatusMod"] == 0
            assert maximum["_DisplayType"] == maximum["WatchLoveEv"] == 0
            catalog.require_eligible(maximum)
            checked += 1
    assert checked > 1312


def test_all_real_stage_routes_and_float32_experience_match_shared_math(catalog):
    for card_id in catalog.card_ids:
        card = catalog.growth.cards.card(card_id)
        rarity = catalog.growth.rarities[card["Rare"] + 1]
        for stage in catalog.stages(card_id):
            values = catalog.maximum_values(card_id, stage)
            cls = catalog.evolution.route(card, stage)
            cap = min(cls["MaxLevel"], rarity[f"LevelCap{stage:02}"])
            expected = experience_threshold(catalog.growth.experience[min(cap, 99) - 1], rarity["RequireExpRatio"])
            assert values["ClassID"] == cls["ClassID"] and values["Exp"] == expected


@pytest.mark.parametrize("card_id", (1, 80, 309, 2695, 2696))
def test_known_exclusions_cannot_create_new_fame(catalog, card_id):
    result = unit(catalog, 17)
    result.update(CardID=card_id, ClassID=catalog.growth.cards.card(card_id)["InitClassID"])
    assert not catalog.eligible(result)
    assert catalog.stages(card_id) == ()
    with pytest.raises(ProtocolError) as failure:
        catalog.maximum_values(card_id, 0)
    assert failure.value.code == "fame_stage_unsupported"


def test_unit_flags_do_not_override_server_eligibility(catalog):
    maximum = unit(catalog, 17)
    before = deepcopy(maximum)
    maximum.update(CanFame=False, Famed=False, OrganizationIndex=7, OrganizationIndex2=9,
                   GeneralPurposeFlags=maximum["GeneralPurposeFlags"] | 2 | 0x20)
    assert catalog.eligible(maximum)
    maximum.update(Exp=0, CanFame=True, Famed=True)
    assert not catalog.eligible(maximum)
    assert before["Exp"] > 0


@pytest.mark.parametrize("field", ("Exp", "Love", "CostModMixValue", "ClassChangeCount", "ClassID"))
def test_each_growth_gate_blocks_incomplete_units(catalog, field):
    result = unit(catalog, 17)
    if field == "Exp":
        result[field] -= 1
    elif field == "Love":
        result[field] -= 1
    elif field == "CostModMixValue":
        result[field] += 1
    elif field == "ClassChangeCount":
        result[field] = 2
    else:
        result[field] = catalog.growth.cards.card(17)["InitClassID"]
    assert not catalog.eligible(result)
    with pytest.raises(ProtocolError) as failure:
        catalog.require_eligible(result)
    assert failure.value.code == "fame_ineligible" and failure.value.status == 409


def test_awakened_skill_accepts_original_skill_level_but_needs_unlock(catalog):
    result = unit(catalog, 17)
    card = catalog.growth.cards.card(17)
    normal_max = catalog.growth.skills[card["ClassLV0SkillID"]]["LevelMax"]
    assert result["SkillLevel"] == max(1, normal_max)
    result["SkillLevel"] = 1
    assert catalog.eligible(result)
    result["SkillEvolved"] = 0
    assert catalog.eligible(result)
    result["GeneralPurposeFlags"] &= ~SKILL_SWITCH_FLAG
    assert not catalog.eligible(result)


def test_nonawakening_skill_requires_ordinary_maximum(catalog):
    skills = catalog.growth.skills
    card_id = next(key for key in catalog.card_ids if not catalog._facts[key]["skill_awakened"]
                   and skills[catalog.growth.cards.card(key)["ClassLV0SkillID"]]["LevelMax"] > 1)
    result = unit(catalog, card_id)
    assert catalog.eligible(result)
    result["SkillLevel"] -= 1
    assert not catalog.eligible(result)


def test_zero_skill_is_normalized_to_at_least_one(catalog):
    card_id = next(key for key in catalog.card_ids if catalog.growth.cards.card(key)["ClassLV0SkillID"] == 0)
    result = unit(catalog, card_id)
    assert result["SkillLevel"] == 1
    result["SkillLevel"] = 0
    assert catalog.eligible(result)


def test_card99_uses_its_own_valid_awakened_route(catalog):
    for stage in catalog.stages(99):
        values = catalog.maximum_values(99, stage)
        assert values["ClassID"] == catalog.evolution.route(catalog.growth.cards.card(99), stage)["ClassID"]
        assert catalog.eligible(unit(catalog, 99, stage))


def test_current_alias_cards_do_not_overlap_eligible_set(catalog):
    found = 0
    for card_id in catalog.card_ids:
        image = catalog.growth.aliases.resolve(card_id).image_card_id
        if image != card_id and catalog._facts[card_id]["skill_awakened"]:
            for stage in catalog.stages(card_id):
                values = catalog.maximum_values(card_id, stage)
                assert values["SettingSkillID"] == catalog.growth.cards.card(image)["EvoSkillID"]
            found += 1
    assert found == 0


def test_image_alias_branch_uses_effective_evolved_skill(catalog, bundle):
    growth, evolution = copy(catalog.growth), copy(catalog.evolution)
    aliases = growth.aliases
    growth.aliases = SimpleNamespace(status=aliases.status,
        resolve=lambda key: replace(aliases.resolve(key), image_card_id=7) if key == 17 else aliases.resolve(key))
    evolution.growth = growth
    other = FameCatalog(growth, evolution, lambda name: bundle.native_cdn.assets.read(name)[0])
    assert other.maximum_values(17, 3)["SettingSkillID"] == growth.cards.card(7)["EvoSkillID"]
    assert other.content_version != catalog.content_version


@pytest.mark.parametrize("field,value", (("Exp", True), ("Exp", -1), ("Exp", 0x80000000),
                                        ("Love", "150"), ("SkillEvolved", True), ("CardID", 999999)))
def test_bad_unit_values_fail_explicitly(catalog, field, value):
    result = unit(catalog, 17)
    result[field] = value
    with pytest.raises(ProtocolError):
        catalog.eligible(result)


def test_stage_one_never_qualifies_a_card_that_requires_evolution(catalog):
    result = unit(catalog, 7)
    result["ClassChangeCount"] = 1
    assert not catalog.eligible(result)
    with pytest.raises(ProtocolError):
        catalog.maximum_values(7, 1)
    assert not catalog.registered([status_row(7, B1=1700000000)], 7, 1)


def test_classic_stage_one_is_capped_by_the_units_own_class_without_a_storage_bucket(catalog):
    card = catalog.growth.cards.card(8)
    rarity = catalog.growth.rarities[card["Rare"] + 1]
    thresholds = [experience_threshold(value, rarity["RequireExpRatio"]) for value in catalog.growth.experience]
    def classic(class_id, level):
        result = unit(catalog, 8, 0)
        result.update(ClassChangeCount=1, ClassID=class_id, Exp=thresholds[level - 1])
        return result
    job = catalog.growth.classes[card["InitClassID"]]["JobChange"]
    cap = min(catalog.growth.classes[job]["MaxLevel"], rarity["LevelCap01"])
    assert catalog.eligible(classic(job, cap)) and not catalog.eligible(classic(job, cap - 1))
    low = next(row for _, row in sorted(catalog.growth.classes.items()) if 1 < row["MaxLevel"] < cap)
    assert derived_level(thresholds[low["MaxLevel"] - 1], thresholds, cap) == low["MaxLevel"]
    assert catalog.eligible(classic(low["ClassID"], low["MaxLevel"]))
    assert not catalog.eligible(classic(low["ClassID"], low["MaxLevel"] - 1))
    assert catalog.stages(8) == (0,)
    with pytest.raises(ProtocolError):
        catalog.maximum_values(8, 1)
    rows = [status_row(8, B1=1700000000)]
    assert not catalog.registered(rows, 8, 1) and not catalog.registered(rows, 8, 0)
    assert catalog.counts(rows)["total"] == 1


def deposit_menu(bundle, change):
    raw = bundle.native_cdn.assets.read(DEPOSIT_MENU)[0]
    rows = parse_table(decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw, LIMITS)["rows"]
    return command_table([(row["_ID"], encode(change(decode_command(row["_Cmnd"])))) for row in rows])


def with_deposit_menu(bundle, table):
    return FameCatalog(bundle.training.catalog, bundle.evolution.catalog,
                       lambda name: table if name == DEPOSIT_MENU else bundle.native_cdn.assets.read(name)[0])


def test_deposit_batch_limit_follows_the_menu_multi_select(bundle, catalog):
    other = with_deposit_menu(bundle, deposit_menu(bundle, lambda command: command.replace(
        '"MaxMultiSelect=10"', '"MaxMultiSelect=7"')))
    assert other.deposit_batch_limit == 7 and other.deposit_limit == catalog.deposit_limit
    assert other.content_version != catalog.content_version


@pytest.mark.parametrize("replacement", ('', '"MaxMultiSelect=0"', f'"MaxMultiSelect={MAX_PLAYER_UNITS + 1}"',
                                         '"MaxMultiSelect=10","MaxMultiSelect=10"'))
def test_missing_or_ambiguous_menu_multi_select_fails_closed(bundle, replacement):
    table = deposit_menu(bundle, lambda command: command.replace('"MaxMultiSelect=10"', replacement))
    with pytest.raises(ProtocolError) as failure:
        with_deposit_menu(bundle, table)
    assert failure.value.code == "fame_catalog_invalid"


@pytest.mark.parametrize("recorded,stage,expected", ((0, 0, True), (2, 0, True), (3, 0, True), (4, 0, True),
    (0, 2, False), (2, 2, True), (3, 2, True), (4, 2, True), (3, 3, True), (4, 3, False),
    (3, 4, False), (4, 4, True), (1, 0, False)))
def test_registration_inheritance_without_backfilling_dates(catalog, recorded, stage, expected):
    rows = [status_row(17, **{f"B{recorded}": 1700000000})]
    before = deepcopy(rows)
    assert catalog.registered(rows, 17, stage) is expected
    assert rows == before


def test_counts_keep_separate_stages_and_historical_support_cards(catalog):
    support = next(iter(catalog.card_ids & catalog.support_card_ids))
    rows = [status_row(17, B3=1, B4=2), status_row(support, B0=3), status_row(2695, B0=4),
            status_row(2696, B0=-1), status_row(1, B0=9)]
    assert catalog.counts(rows) == {"total": 5, "support": 3}
    assert catalog.counts([]) == {"total": 0, "support": 0}
    assert not catalog.registered([], 17, 3)


@pytest.mark.parametrize("kind", ("duplicate", "missing", "bool", "unknown", "not_list"))
def test_bad_card_status_rows_are_rejected(catalog, kind):
    rows = [status_row(17, B3=1)]
    if kind == "duplicate":
        rows *= 2
    elif kind == "missing":
        rows[0].pop("B0")
    elif kind == "bool":
        rows[0]["B0"] = True
    elif kind == "unknown":
        rows[0]["CardID"] = 999999
    else:
        rows = {}
    with pytest.raises(ProtocolError):
        catalog.counts(rows)


def test_output_is_independent_and_catalog_sets_are_immutable(catalog):
    first = catalog.maximum_values(17, 3)
    second = catalog.maximum_values(17, 3)
    first["Exp"] = 1
    assert second["Exp"] != first["Exp"]
    assert isinstance(catalog.card_ids, frozenset)
    with pytest.raises(TypeError):
        catalog._cards[17]["Rare"] = 0
    with pytest.raises(AttributeError):
        catalog.deposit_limit = 999
    with pytest.raises(AttributeError):
        catalog.card_ids = frozenset()


def test_dependency_fingerprint_change_fails_before_using_cached_values(catalog, monkeypatch):
    monkeypatch.setattr(catalog.growth, "content_version", "f" * 64)
    assert not catalog.status()["ready"]
    with pytest.raises(ProtocolError) as failure:
        catalog.maximum_values(17, 3)
    assert failure.value.code == "fame_catalog_changed"


def test_public_master_swap_is_detected(catalog, monkeypatch):
    monkeypatch.setattr(catalog.growth.cards, "snapshot", replace(catalog.snapshot, identifier="different"))
    assert not catalog.status()["ready"]


@pytest.mark.parametrize("change", (None, "growth", "snapshot"))
def test_readiness_check_reaches_the_status_conclusion(catalog, monkeypatch, change):
    if change == "growth":
        monkeypatch.setattr(catalog.growth, "content_version", "f" * 64)
    elif change == "snapshot":
        monkeypatch.setattr(catalog.growth.cards, "snapshot", replace(catalog.snapshot, identifier="different"))
    status = catalog.status()
    assert catalog.is_ready() is status["ready"] is (change is None)
    assert status["error"] == (None if change is None else "fame_catalog_changed")


@pytest.mark.parametrize("fairy,silver", (("absent", True), ("attached", False), ("absent", False),
                                          ("unbarracked", True), ("unbarracked", False)))
def test_replayable_versions_include_growth_configurations_without_warehouses(bundle, catalog, unbarracked_farm,
                                                                              fairy, silver):
    growth, read = catalog.growth, lambda name: bundle.native_cdn.assets.read(name)[0]
    assert growth.fairy_farm.barracks is not None and unbarracked_farm.barracks is None
    farms = {"attached": growth.fairy_farm, "unbarracked": unbarracked_farm, "absent": None}
    variant = TrainingCatalog(growth.cards, bundle.player_state, read, growth.aliases, rules=growth.rules,
                              fairy_farm=farms[fairy], silver_house=growth.silver_house if silver else None)
    evolution = EvolutionCatalog(variant, catalog.evolution.possessions, catalog.evolution.materials,
                                 rules=catalog.evolution.rules)
    committed = FameCatalog(variant, evolution, read).content_version
    assert committed != catalog.content_version
    assert {committed, catalog.content_version} <= catalog.replayable_content_versions()


def test_replayable_versions_are_digested_once_per_growth_version(catalog, monkeypatch):
    import aigis_ps.fame_catalog as module
    versions = catalog.replayable_content_versions()
    assert len(versions) == len(catalog.growth.replayable_content_versions())
    monkeypatch.setattr(module, "_digest", lambda _: pytest.fail("Fame must keep the replayable versions it derived"))
    assert catalog.replayable_content_versions() == versions


@pytest.mark.parametrize("kind", ("card", "initial_class", "rarity", "skill", "experience"))
def test_selected_resource_changes_cannot_use_frozen_results(catalog, monkeypatch, kind):
    growth = catalog.growth
    if kind == "card":
        original = growth.cards.card
        monkeypatch.setattr(growth.cards, "card", lambda key: {**original(key), "BonusType": 0} if key == 17 else original(key))
    elif kind == "initial_class":
        key = growth.cards.card(17)["InitClassID"]
        monkeypatch.setattr(growth, "_classes", {**growth._classes, key: {**growth._classes[key], "MaxLevel": 1}})
    elif kind == "rarity":
        key = growth.cards.card(17)["Rare"] + 1
        monkeypatch.setattr(growth, "_rarities", {**growth._rarities, key: {**growth._rarities[key], "LevelCap03": 1}})
    elif kind == "skill":
        key = growth.cards.card(17)["ClassLV0SkillID"]
        skills = list(growth._skills)
        skills[key] = {**skills[key], "LevelMax": 1}
        monkeypatch.setattr(growth, "_skills", tuple(skills))
    else:
        monkeypatch.setattr(growth, "_experience", (0, *growth._experience[2:]))
    with pytest.raises(ProtocolError) as failure:
        catalog.maximum_values(17, 3)
    assert failure.value.status == 503


def test_unrelated_training_eligibility_is_not_used(catalog, monkeypatch):
    monkeypatch.setattr(catalog.growth, "describe", lambda *_: pytest.fail("Fame must not use training restrictions"))
    assert catalog.eligible(unit(catalog, 17))


def test_missing_resource_prevents_catalog_construction(bundle):
    with pytest.raises(ProtocolError) as failure:
        FameCatalog(bundle.training.catalog, bundle.evolution.catalog, lambda _: b"bad")
    assert failure.value.code == "fame_catalog_invalid"

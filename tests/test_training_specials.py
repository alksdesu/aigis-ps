from copy import deepcopy
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import Store
from test_quest_service import bundle, request
from test_training_service import columns, message


@pytest.fixture
def make_case(bundle, tmp_path):
    sequence = 0
    def create(target_id, material_ids, **changes):
        nonlocal sequence
        sequence += 1
        state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
        state, units = bundle.player_state.credit_units(state,
            [{"card_id": card_id, "level": 1, "count": 1} for card_id in (target_id, *material_ids)])
        state["tables"]["game_param"][0]["Money"] = 1000000
        state["player_state"]["units"][1].update(changes)
        return SimpleNamespace(bundle=bundle, service=bundle.training, catalog=bundle.training.catalog,
            store=Store(tmp_path / f"special-{sequence}.sqlite3", state), units=units)
    return create


def result(case):
    case.service.integrate(message(case), case.store)
    return case.store.profile().state["player_state"]["units"][1]


def failure(case, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.integrate(message(case), case.store)
    assert caught.value.code == code
    assert case.store.profile() == before


def test_zero_base_experience_skill_material_keeps_threshold_remainder(make_case):
    unit = result(make_case(3, [60]))
    assert (unit["Exp"], unit["SkillLevel"], unit["SkillExperience"]) == (0, 2, 1)
    assert unit["CostModMixValue"] == 0


def test_empty_rating_command_uses_native_allow_default(make_case):
    unit = result(make_case(3, [456]))
    assert unit["SkillLevel"] == 4 and unit["SkillExperience"] == 0


def test_cost_material_uses_target_override_table(make_case):
    unit = result(make_case(3, [97]))
    assert (unit["Exp"], unit["CostModMixValue"], unit["CostExperience"]) == (0, -1, 4)


def test_exclusive_direct_level_material_clamps_and_clears_remainder(make_case):
    unit = result(make_case(3, [303], SkillExperience=1))
    assert unit["SkillLevel"] == 5 and unit["SkillExperience"] == 0
    failure(make_case(3, [303, 303]), "training_material_exclusive")


def test_rarity_limited_skill_material_uses_group_not_item_gender(make_case):
    unit = result(make_case(8, [901]))
    assert unit["SkillLevel"] == 2 and unit["SkillExperience"] == 1
    failure(make_case(3, [901]), "training_material_rarity")


def test_dual_material_keeps_growing_cost_when_skill_is_maximum(make_case):
    unit = result(make_case(8, [1408], SkillLevel=5))
    assert unit["SkillLevel"] == 5
    assert (unit["CostModMixValue"], unit["CostExperience"]) == (-2, 2)


def test_prefix_rejects_redundant_skill_material_but_accepts_remainder_growth(make_case):
    failure(make_case(3, [60, 60], SkillLevel=4, SkillExperience=8), "training_material_redundant")
    unit = result(make_case(3, [60, 60], SkillLevel=4))
    assert unit["SkillLevel"] == 4 and unit["SkillExperience"] == 6


def test_child_material_applies_to_target_without_matching_skill_or_category(make_case):
    case = make_case(2, [2029])
    assert not case.catalog._related(case.catalog.cards.card(2), case.catalog.cards.card(2029))
    unit = result(case)
    assert (unit["CostModMixValue"], unit["CostExperience"], unit["SkillExperience"]) == (-1, 0, 0)


def test_child_exception_uses_resource_values_instead_of_fixed_twelve_and_eight(make_case):
    from aigis_ps.training_recipe import contributions
    case = make_case(2688, [2713])
    state = case.store.profile().state["player_state"]["units"]
    target, material = (case.catalog.describe(unit) for unit in state[1:])
    assert contributions(target, material, case.catalog.effects, child_skill=True, child_cost=True) == (0, 2, 0, 2)
    result(case)


def test_class_match_suppression_removes_only_that_materials_bonus(make_case):
    unit = result(make_case(6, [170]))
    assert unit["Exp"] == 100


def test_original_card_relationships_are_symmetric_and_ignore_zero_categories(make_case):
    case = make_case(3, [60])
    cards = case.catalog.cards
    for a, b in ((1, 2), (1, 20), (203, 317), (903, 904)):
        assert case.catalog._related(cards.card(a), cards.card(b))
        assert case.catalog._related(cards.card(b), cards.card(a))
    assert not case.catalog._related(cards.card(15), cards.card(15))


def test_owned_bonus_uses_one_maximum_source_and_rejects_forged_percent(make_case):
    case = make_case(8, [56])
    profile = case.store.profile()
    state, sources = case.bundle.player_state.credit_units(profile.state, [{"card_id": 416, "level": 1, "count": 2}])
    case.store.update_state(state, profile.revision)
    failure(case, "training_bonus_changed")
    def boosted(percent):
        return request(case.store, 5, (Container("BASE", columns(case.units[:1])), Container("APPEND", columns(case.units[1:])),
            Container("CMB", (Column("A1", "I", (416,)), Column("A7", "I", (sources[-1]["GetIndex"],)),
                              Column("B0", "I", (percent,))))))
    before = case.store.profile()
    with pytest.raises(ProtocolError, match="bonus"):
        case.service.integrate(boosted(20), case.store)
    assert case.store.profile() == before
    case.service.integrate(boosted(10), case.store)
    assert case.store.profile().state["player_state"]["units"][1]["Exp"] == 1375


@pytest.mark.parametrize("target,material", [(3, 2037), (5, 2038), (1252, 1654)])
def test_current_conditional_materials_use_native_predicates(make_case, target, material):
    unit = result(make_case(target, [material]))
    assert unit["SkillLevel"] > 1 or unit["SkillExperience"] > 0


def test_chibi_condition_uses_hashed_name_tables(make_case):
    ordinary = make_case(3, [1550])
    failure(ordinary, "training_material_condition")
    cards = ordinary.catalog.cards
    card_id = next(identifier for identifier in cards.card_ids if cards.card(identifier)["Genus"] == 108
        and cards.card(identifier)["InitClassID"] >= 100 and cards.card(identifier)["ClassLV0SkillID"] > 0)
    unit = result(make_case(card_id, [1550]))
    assert unit["SkillLevel"] > 1


def test_evolved_skill_and_class_cap_are_derived_from_persisted_state(make_case):
    case = make_case(7, [56])
    profile = case.store.profile()
    target = profile.state["player_state"]["units"][1]
    card = case.catalog.cards.card(7)
    target.update(ClassChangeCount=2, GeneralPurposeFlags=target["GeneralPurposeFlags"] | 4)
    case.store.update_state(profile.state, profile.revision)
    view = case.catalog.describe(target)
    assert view.skill_id == card["EvoSkillID"]
    assert view.cap == min(view.class_row["MaxLevel"], view.rarity["LevelCap02"])
    result(case)

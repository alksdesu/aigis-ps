from copy import deepcopy
from types import SimpleNamespace

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.training_effects import TrainingEffects
from aigis_ps.training_recipe import contributions, permits_zero_experience, validate_materials


def unit(card_id=1, *, skill=10, initial=1, current=1, config=0, **changes):
    card = {"CardID": card_id, "InitClassID": initial, "CostDecValue": 3,
            "BuildSkillLevel": 1, "BuildSkillExperience": 2,
            "ChildSkillLevel": 3, "ChildSkillExperience": 4,
            "BuildCostLevel": 5, "BuildCostExperience": 6,
            "ChildCostLevel": 7, "ChildCostExperience": 8,
            "_TypeRace": 10, "Genus": 20, "Assign": 30, "Identity": 40, "Blood": 50}
    return SimpleNamespace(card=card, effective_card=deepcopy(card), class_row={"ClassID": current},
                           initial_config=config, skill_id=skill, skill_max=5,
                           rarity={"RarityGroupID": 3}, level=1,
                           unit={"SkillLevel": 1, "CostModMixValue": 0}, **changes)


def effect(kind, parameters=(0, 0, 0, 0), **changes):
    return {"_ConfigID": 1, "_InvokeType": 1, "_TargetType": 1, "_InfluenceType": kind,
            **dict(zip(("_Param1", "_Param2", "_Param3", "_Param4"), parameters)),
            "_Command": "", "_ActivateCommand": "", "ExtendProperty": "", "NoChangeCondition": "", **changes}


def effects(*rows):
    return TrainingEffects(rows, [])


@pytest.mark.parametrize("target_skill,material_skill,same_card,expected", [
    (10, 10, False, (1, 2, 0, 0)), (10, 11, True, (0, 0, 5, 6)),
    (10, 10, True, (1, 2, 5, 6)), (10, 11, False, (0, 0, 0, 0)),
    (0, 0, False, (1, 2, 0, 0)),
])
def test_native_base_contributions_compare_skill_and_original_card_separately(
        target_skill, material_skill, same_card, expected):
    assert contributions(unit(skill=target_skill), unit(1 if same_card else 2, skill=material_skill), effects()) == expected


def test_child_replaces_base_but_keeps_special_skill_and_ability_additions():
    target, material = unit(skill=58), unit(2, skill=58, config=1)
    table = effects(effect(99, (11, 12, 0, 0)), effect(104, (13, 14, 0, 0)))
    assert contributions(target, material, table, child_skill=True, child_cost=True) == (16, 17, 21, 21)


@pytest.mark.parametrize("initial,current,expected", [(16, 1, (1, 2, 0, 0)), (1, 16, (2, 4, 0, 0)),
                                                         (1, 56, (2, 4, 0, 0))])
def test_skill_fallback_uses_current_class(initial, current, expected):
    assert contributions(unit(), unit(2, initial=initial, current=current), effects()) == expected


def test_same_special_skill_and_class_fallback_all_stack():
    assert contributions(unit(skill=58), unit(2, skill=58, current=16), effects()) == (3, 6, 0, 0)


@pytest.mark.parametrize("mode,related,expected", [(0, False, (8, 6)), (1, False, (1, 2)),
                                                   (1, True, (8, 6)), (2, True, (1, 2)), (-1, True, (1, 2))])
def test_ability_pair_swaps_parameters_and_existing_effect_suppresses_class_fallback(mode, related, expected):
    result = contributions(unit(), unit(2, config=1, current=16), effects(effect(99, (4, 7, mode, 99))), related=related)
    assert result == (*expected, 0, 0)


def test_special_cost_skill_bypasses_ability_and_stacks_with_child():
    table = effects(effect(104, (99, 99, 0, 0), _Command="Unknown();"))
    assert contributions(unit(skill=10), unit(2, skill=73, config=1), table, child_cost=True) == (0, 0, 12, 14)


def test_effects_use_material_initial_configuration_only():
    target, material = unit(config=999), unit(2, config=1)
    assert contributions(target, material, effects(effect(99, (6, 0, 0, 0)))) == (1, 8, 0, 0)


@pytest.mark.parametrize("kind", [99, 104])
@pytest.mark.parametrize("parameters", [(-1, 0, 0, 0), (0, -1, 0, 0)])
def test_negative_active_ability_contributions_are_explicitly_unsupported(kind, parameters):
    with pytest.raises(ProtocolError, match="Negative"):
        contributions(unit(), unit(2, config=1), effects(effect(kind, parameters)))


@pytest.mark.parametrize("field,value", [("BuildSkillLevel", True), ("BuildSkillExperience", -1),
                                          ("BuildSkillExperience", 0x80000000), ("CardID", 0)])
def test_invalid_card_fields_do_not_change_input(field, value):
    target, material = unit(), unit(2)
    material.card[field] = value
    before = deepcopy((target, material))
    with pytest.raises(ProtocolError):
        contributions(target, material, effects())
    assert (target, material) == before


def test_contribution_overflow_and_duplicate_effects_fail_without_mutation():
    target, material = unit(skill=58), unit(2, skill=58)
    material.card["BuildSkillExperience"] = 0x7fffffff
    before = deepcopy(material)
    with pytest.raises(ProtocolError, match="int32"):
        contributions(target, material, effects())
    assert material == before
    material.initial_config = 1
    with pytest.raises(ProtocolError, match="Multiple"):
        contributions(unit(), material, effects(effect(99), effect(99)))


@pytest.mark.parametrize("option", ["child_skill", "child_cost", "related"])
def test_relationship_flags_are_strict_booleans(option):
    with pytest.raises(ProtocolError):
        contributions(unit(), unit(2), effects(), **{option: 1})


@pytest.mark.parametrize("initial,kind", [(7, None), (15, None), (16, None), (1, 99)])
def test_skill_only_material_rejects_full_or_absent_skill(initial, kind):
    table = effects(effect(kind)) if kind else effects()
    material = unit(2, initial=initial, config=1 if kind else 0)
    for skill_id, level in ((10, 5), (0, 0)):
        target = unit(skill=skill_id)
        target.unit["SkillLevel"] = level
        with pytest.raises(ProtocolError, match="cannot improve"):
            validate_materials(target, [material], table)


@pytest.mark.parametrize("initial,kind", [(9, None), (1, 104)])
def test_cost_only_material_requires_remaining_cost_reduction(initial, kind):
    target = unit()
    target.unit["CostModMixValue"] = -3
    material = unit(2, initial=initial, config=1 if kind else 0)
    with pytest.raises(ProtocolError, match="cannot improve"):
        validate_materials(target, [material], effects(effect(kind)) if kind else effects())


@pytest.mark.parametrize("skill_full,cost_full", [(False, True), (True, False), (False, False), (True, True)])
def test_dual_material_requires_either_skill_or_cost_room(skill_full, cost_full):
    target, material = unit(), unit(2, config=1)
    target.unit.update(SkillLevel=5 if skill_full else 1, CostModMixValue=-3 if cost_full else 0)
    table = effects(effect(99), effect(104))
    if skill_full and cost_full:
        with pytest.raises(ProtocolError):
            validate_materials(target, [material], table)
    else:
        validate_materials(target, [material], table)


def test_ordinary_material_not_rejected_when_skill_and_cost_are_full():
    target = unit()
    target.unit.update(SkillLevel=5, CostModMixValue=-3)
    validate_materials(target, [unit(2)], effects())


@pytest.mark.parametrize("initial,kind", [(17, None), (18, None), (1, 103)])
def test_rarity_restriction_uses_group(initial, kind):
    target, material = unit(), unit(2, initial=initial, config=1 if kind else 0)
    target.rarity["StarsCount"], material.rarity["StarsCount"] = 3, 8
    table = effects(effect(kind)) if kind else effects()
    validate_materials(target, [material], table)
    material.rarity["RarityGroupID"] = 4
    with pytest.raises(ProtocolError, match="same rarity group"):
        validate_materials(target, [material], table)


def test_exclusivity_is_symmetric_and_current_explicit_empty_config_has_no_fallback():
    target, ordinary, exclusive = unit(), unit(2, initial=7), unit(3, config=1)
    table = effects(effect(100))
    validate_materials(target, [exclusive], table)
    validate_materials(target, [ordinary, unit(4)], table)
    for batch in ([ordinary, exclusive], [exclusive, ordinary]):
        with pytest.raises(ProtocolError, match="alone"):
            validate_materials(target, batch, table)
    with pytest.raises(ProtocolError, match="alone"):
        validate_materials(target, [ordinary, unit(4)], table, exclusive_classes=(7, 9, 13, 15, 16))


@pytest.mark.parametrize("kind,field", [(139, "_TypeRace"), (140, "Genus"), (141, "Assign"),
                                         (142, "Identity"), (143, "Blood")])
def test_target_condition_uses_effective_card_and_nonzero_true(kind, field):
    target, material = unit(), unit(2, config=1)
    target.card[field], target.effective_card[field] = 99, 77
    validate_materials(target, [material], effects(effect(kind, (77, -2, 0, 0))))
    with pytest.raises(ProtocolError, match="condition"):
        validate_materials(target, [material], effects(effect(kind, (99, 1, 0, 0))))


def test_last_row_wins_within_condition_kind_but_all_kinds_must_allow():
    target, material = unit(), unit(2, config=1)
    reject, allow = effect(140, (20, 0, 1, 0)), effect(140, (20, 1, 0, 0))
    validate_materials(target, [material], effects(reject, allow))
    with pytest.raises(ProtocolError):
        validate_materials(target, [material], effects(allow, reject))
    with pytest.raises(ProtocolError):
        validate_materials(target, [material], effects(allow, effect(143, (50, 0, 1, 0))))


@pytest.mark.parametrize("command,allowed", [("", True), ("0", True), (" 0; ", True), ("1", False), ("1;", False)])
def test_138_true_means_forbidden(command, allowed):
    operation = lambda: validate_materials(unit(), [unit(2, config=1)], effects(effect(138, _Command=command)))
    if allowed:
        operation()
    else:
        with pytest.raises(ProtocolError, match="forbids"):
            operation()


def test_first_nonempty_138_command_is_used_and_unknown_command_needs_callback():
    target, material = unit(), unit(2, config=1)
    table = effects(effect(138), effect(138, _Command="First();"), effect(138, _Command="1;"))
    with pytest.raises(ProtocolError, match="interpreter"):
        validate_materials(target, [material], table)
    called = []
    validate_materials(target, [material], table, condition=lambda text, context: called.append((text, context)) or False)
    assert called == [("First();", target)]
    with pytest.raises(ProtocolError, match="boolean"):
        validate_materials(target, [material], table, condition=lambda *_: 0)


@pytest.mark.parametrize("kind,qualifier", [(99, "_Command"), (100, "_Command"), (103, "_ActivateCommand"),
                                           (138, "ExtendProperty"), (140, "NoChangeCondition")])
def test_unverified_effect_qualifiers_do_not_silently_allow_material(kind, qualifier):
    with pytest.raises(ProtocolError):
        validate_materials(unit(), [unit(2, config=1)], effects(effect(kind, **{qualifier: "Unknown();"})))


@pytest.mark.parametrize("initial", [7, 9, 15, 16, 18])
def test_zero_experience_class_fallback(initial):
    assert permits_zero_experience([unit(2, initial=initial)], effects())


def test_zero_experience_requires_every_material_and_uses_initial_class_or_101():
    table = effects(effect(101))
    special, plain = unit(2, config=1), unit(3, current=7)
    assert permits_zero_experience([special, unit(4, initial=9)], table)
    assert not permits_zero_experience([special, plain], table)
    assert not permits_zero_experience([], table)


@pytest.mark.parametrize("operation", [
    lambda: validate_materials(unit(), [], effects()),
    lambda: validate_materials(unit(), iter([unit(2)]), effects()),
    lambda: validate_materials(unit(), [unit(2)], effects(), exclusive_classes=(True,)),
    lambda: validate_materials(unit(), [unit(2)], effects(), condition=False),
    lambda: permits_zero_experience(None, effects()),
])
def test_invalid_policy_arguments_fail_explicitly(operation):
    with pytest.raises(ProtocolError):
        operation()


def test_all_readers_leave_target_and_materials_unchanged():
    target, material = unit(), unit(2, config=1)
    table = effects(effect(101), effect(99, (6, 0, 0, 0)))
    before = deepcopy((target, material))
    contributions(target, material, table)
    validate_materials(target, [material], table)
    assert permits_zero_experience([material], table)
    assert (target, material) == before

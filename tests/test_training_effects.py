import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.training_effects import TrainingEffects, material_experience, total_experience


def effect(config=1, kind=214, **changes):
    return {"_ConfigID": config, "_InvokeType": 1, "_TargetType": 1, "_InfluenceType": kind,
            "_Param1": 250, "_Param2": 1, "_Param3": 0, "_Param4": 0,
            "_Command": "", "_ActivateCommand": "", "ExtendProperty": "", "NoChangeCondition": "", **changes}


def specialty(kind, card=1, **changes):
    return {"ID_Card": card, "Type_Specialty": kind, "Value_Specialty": 1,
            "Value_Param1": 0, "Value_Param2": 0, "Value_Param3": 0, "Value_Param4": 0, "Command": "", **changes}


def test_passive_effects_resolve_continuations_and_exclude_other_invocations():
    table = TrainingEffects([effect(), effect(0, 105, _InvokeType=0, _TargetType=0, _Param1=10, _Param2=0),
                             effect(2, 105, _InvokeType=2, _Param1=999, _Param2=0)], [])
    assert table.ownership_bonus((1,)) == 10
    assert table.ownership_bonus((2,)) == 0
    assert table.rarity_multiplier((1, 1), 3, 3) == 250
    assert table.rarity_multiplier((1,), 3, 4) == 100


def test_irrelevant_specialty_does_not_disable_ordinary_training():
    table = TrainingEffects([], [specialty(1), specialty(31, card=2)])
    assert not table.suppress_class_bonus(1)
    assert table.suppress_class_bonus(2)
    assert table.suppress_class_bonus(170)


@pytest.mark.parametrize("kind,target,material,expected", [
    (8, (3, 3), (3, 3), 200), (8, (3, 3), (4, 4), 150),
    (9, (3, 3), (4, 4), 200), (10, (3, 0), (4, 4), 200),
    (10, (3, 3), (4, 0), 150),
])
def test_material_multiplier_uses_distinct_group_and_star_conditions(kind, target, material, expected):
    table = TrainingEffects([], [specialty(kind)])
    assert table.material_multiplier(1, dict(zip(("RarityGroupID", "StarsCount"), target)),
                                     dict(zip(("RarityGroupID", "StarsCount"), material)), 0) == expected


def test_skill_type_100_uses_material_stars_then_rarity_group():
    table = TrainingEffects([], [])
    zero = {"RarityGroupID": 2, "StarsCount": 0}
    normal = {"RarityGroupID": 2, "StarsCount": 2}
    assert table.material_multiplier(1, normal, zero, 100) == 150
    assert table.material_multiplier(1, zero, zero, 100) == 200
    assert table.material_multiplier(1, zero, normal, 100) == 200


def test_native_integer_truncations_precede_global_float_multiplier():
    assert material_experience(333, 5, 3, 150, 250) == 1272
    assert material_experience(500, 0, 1, 100, 250) == 1250
    assert total_experience(500, 20, (150, 200), 10) == 1716


@pytest.mark.parametrize("operation", [
    lambda: TrainingEffects([effect(_Command="Unknown();")], []).rarity_multiplier((1,), 3, 3),
    lambda: TrainingEffects([], [specialty(8), specialty(9, card=0)]).material_multiplier(
        1, {"RarityGroupID": 1, "StarsCount": 1}, {"RarityGroupID": 1, "StarsCount": 1}, 0),
    lambda: material_experience(0x7fffffff, 0, 1, 200, 100),
    lambda: total_experience(0x7fffffff, 1, (100,), 0),
])
def test_unknown_conditions_missing_references_and_overflow_do_not_become_default_success(operation):
    with pytest.raises(ProtocolError):
        operation()


def test_native_absent_ability_configuration_has_no_matching_effect():
    table = TrainingEffects([], [])
    assert table.find((500,), 214) == ()
    assert table.ownership_bonus((500,)) == 0

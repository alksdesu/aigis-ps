from __future__ import annotations

from .errors import ProtocolError
from .native_numbers import INT32_MIN, int32


CONDITION_FIELDS = {139: "_TypeRace", 140: "Genus", 141: "Assign", 142: "Identity", 143: "Blood"}


def _invalid(message):
    raise ProtocolError("training_recipe_invalid", message, 503)


def _unsupported(message):
    raise ProtocolError("training_recipe_unsupported", message, 501)


def _field(row, name, minimum=0):
    try:
        value = row[name]
    except (KeyError, TypeError) as error:
        raise ProtocolError("training_recipe_invalid", f"Training recipe is missing {name}", 503) from error
    if not int32(value) or value < minimum:
        _invalid(f"Training recipe requires a valid {name}")
    return value


def _pair(card, prefix):
    return _field(card, prefix + "Level"), _field(card, prefix + "Experience")


def _sum_pairs(*pairs):
    result = tuple(sum(pair[index] for pair in pairs) for index in (0, 1))
    if any(not int32(value) or value < 0 for value in result):
        raise ProtocolError("training_experience_capacity", "Training contribution exceeds signed int32", 409)
    return result


def _ability_pair(material, effects, kind, related):
    effect = effects.single((material.initial_config,), kind)
    if effect is None:
        return None
    experience, direct, mode, _ = effect.static()
    if mode not in (0, 1) or mode == 1 and not related:
        return 0, 0
    if not int32(experience) or not int32(direct) or experience < 0 or direct < 0:
        _unsupported("Negative or invalid training contributions are not supported")
    return direct, experience


def contributions(target, material, effects, *, child_skill=False, child_cost=False, related=False):
    if any(type(value) is not bool for value in (child_skill, child_cost, related)):
        _invalid("Training relationship decisions must be booleans")
    if not int32(target.skill_id) or not int32(material.skill_id) or min(target.skill_id, material.skill_id) < 0:
        _invalid("Effective skill IDs must be nonnegative int32 values")
    target_id = _field(target.card, "CardID", 1)
    material_id = _field(material.card, "CardID", 1)
    skill = (0, 0)
    if child_skill:
        skill = _pair(material.card, "ChildSkill")
    elif target.skill_id == material.skill_id:
        skill = _pair(material.card, "BuildSkill")
    if material.skill_id == 58:
        skill = _sum_pairs(skill, _pair(material.card, "BuildSkill"))
    ability_skill = _ability_pair(material, effects, 99, related)
    if ability_skill is None:
        ability_skill = _pair(material.card, "BuildSkill") if _field(material.class_row, "ClassID", 1) in (16, 56) else (0, 0)
    skill = _sum_pairs(skill, ability_skill)
    cost = (0, 0)
    if child_cost:
        cost = _pair(material.card, "ChildCost")
    elif target_id == material_id:
        cost = _pair(material.card, "BuildCost")
    ability_cost = (_pair(material.card, "BuildCost") if material.skill_id == 73 else
                    _ability_pair(material, effects, 104, related) or (0, 0))
    cost = _sum_pairs(cost, ability_cost)
    return *skill, *cost


def _has_effect(material, effects, kind):
    found = effects.find((material.initial_config,), kind)
    for effect in found:
        effect.static()
    return bool(found)


def _condition_result(command, target, condition):
    command = command.strip()
    if command in ("1", "1;"):
        return True
    if command in ("0", "0;"):
        return False
    if condition is None:
        _unsupported("This material requires an integration condition interpreter")
    result = condition(command, target)
    if type(result) is not bool:
        _invalid("Integration condition interpreter must return a boolean")
    return result


def _validate_conditions(target, material, effects, condition):
    for kind, field in CONDITION_FIELDS.items():
        allowed = True
        for effect in effects.find((material.initial_config,), kind):
            expected, matching, different, _ = effect.static()
            actual = _field(target.effective_card, field, INT32_MIN)
            allowed = bool(matching if actual == expected else different)
        if not allowed:
            raise ProtocolError("training_material_condition", "The target does not satisfy this material's condition", 409)
    for effect in effects.find((material.initial_config,), 138):
        if any((effect.activation, effect.extension, effect.unchanged_condition)):
            _unsupported("This integration condition has unsupported qualifiers")
        if effect.command.strip():
            if _condition_result(effect.command, target, condition):
                raise ProtocolError("training_material_condition", "This material forbids the selected target", 409)
            break


def validate_materials(target, materials, effects, *, exclusive_classes=(), condition=None):
    if not isinstance(materials, (list, tuple)) or not materials:
        _invalid("Training material validation requires a nonempty sequence")
    try:
        classes = tuple(exclusive_classes)
    except TypeError as error:
        raise ProtocolError("training_recipe_invalid", "Exclusive classes must be a sequence", 503) from error
    if any(not int32(value) or value <= 0 for value in classes):
        _invalid("Exclusive class identifiers must be positive int32 values")
    if condition is not None and not callable(condition):
        _invalid("Integration condition interpreter must be callable")
    if not int32(target.skill_id) or target.skill_id < 0 or not int32(target.skill_max) or target.skill_max < 0:
        _invalid("Target skill bounds must be nonnegative int32 values")
    can_skill = target.skill_id != 0 and _field(target.unit, "SkillLevel") < target.skill_max
    can_cost = _field(target.unit, "CostModMixValue", INT32_MIN) > -_field(target.card, "CostDecValue")
    for material in materials:
        initial_class = _field(material.card, "InitClassID", 1)
        skill_material = _has_effect(material, effects, 99) or initial_class in (7, 15, 16)
        cost_material = _has_effect(material, effects, 104) or initial_class == 9
        if skill_material and cost_material:
            cannot_grow = not can_skill and not can_cost
        else:
            cannot_grow = skill_material and not can_skill or cost_material and not can_cost
        if cannot_grow:
            raise ProtocolError("training_material_maximum", "This material cannot improve the target's skill or cost", 409)
        if (_has_effect(material, effects, 103) or initial_class in (17, 18)) and (
                _field(target.rarity, "RarityGroupID") != _field(material.rarity, "RarityGroupID")):
            raise ProtocolError("training_material_rarity", "This material requires the same rarity group", 409)
        if (_has_effect(material, effects, 100) or initial_class in classes) and len(materials) != 1:
            raise ProtocolError("training_material_exclusive", "This material must be used alone", 409)
        _validate_conditions(target, material, effects, condition)


def permits_zero_experience(materials, effects):
    if not isinstance(materials, (list, tuple)):
        _invalid("Zero-experience validation requires a material sequence")
    return bool(materials) and all(_has_effect(material, effects, 101) or
        _field(material.card, "InitClassID", 1) in (7, 9, 15, 16, 18) for material in materials)

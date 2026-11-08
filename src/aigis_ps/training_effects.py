from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .errors import ProtocolError
from .native_numbers import INT32_MIN, float32, int32


def unsupported(message):
    raise ProtocolError("training_effect_unsupported", message, 501)


def _integer(value, minimum=INT32_MIN):
    if not int32(value) or value < minimum:
        raise ValueError("Training effects require native integer fields")
    return value


def _amount(value):
    if not int32(value) or value < 0:
        raise ProtocolError("training_experience_capacity", "Training experience exceeds the native field", 409)
    return value


@dataclass(frozen=True)
class AbilityEffect:
    kind: int
    parameters: tuple[int, int, int, int]
    command: str
    activation: str
    extension: str
    unchanged_condition: str

    def static(self):
        if any((self.command, self.activation, self.extension, self.unchanged_condition)):
            unsupported("This training effect has an unsupported condition")
        return self.parameters


class TrainingEffects:
    def __init__(self, ability_rows, specialty_rows):
        abilities, specialties = {}, {}
        config = invocation = target = influence = 0
        try:
            for row in ability_rows:
                config = _integer(row["_ConfigID"], 0) or config
                invocation = _integer(row["_InvokeType"], 0) or invocation
                target = _integer(row["_TargetType"], 0) or target
                influence = _integer(row["_InfluenceType"], 0) or influence
                if not config or not invocation or not target or not influence:
                    raise ValueError("Ability continuation precedes a complete group")
                parameters = tuple(_integer(row[f"_Param{index}"]) for index in range(1, 5))
                texts = tuple(row[name] for name in ("_Command", "_ActivateCommand", "ExtendProperty", "NoChangeCondition"))
                if any(not isinstance(text, str) or len(text.encode("utf-8")) > 65536 for text in texts):
                    raise ValueError("Ability conditions must be bounded text")
                abilities.setdefault(config, [])
                if invocation == target == 1:
                    abilities[config].append(AbilityEffect(influence, parameters, *texts))
            card_id = 0
            for row in specialty_rows:
                card_id = _integer(row["ID_Card"], 0) or card_id
                if not card_id:
                    raise ValueError("Specialty continuation precedes a card")
                _integer(row["Type_Specialty"], 0)
                for name in ("Value_Specialty", "Value_Param1", "Value_Param2", "Value_Param3", "Value_Param4"):
                    _integer(row[name])
                if not isinstance(row["Command"], str):
                    raise ValueError("Specialty command must be text")
                specialties.setdefault(card_id, []).append(MappingProxyType(dict(row)))
        except (KeyError, TypeError, ValueError) as error:
            raise ProtocolError("training_effect_catalog_invalid", "Training effects do not satisfy their native schemas", 503) from error
        self.abilities = MappingProxyType({key: tuple(value) for key, value in abilities.items()})
        self.specialties = MappingProxyType({key: tuple(value) for key, value in specialties.items()})

    def find(self, config_ids, kind):
        result = []
        for config_id in dict.fromkeys(config_ids):
            _integer(config_id, 0)
            if not config_id:
                continue
            result.extend(effect for effect in self.abilities.get(config_id, ()) if effect.kind == kind)
        return tuple(result)

    def single(self, config_ids, kind):
        effects = self.find(config_ids, kind)
        if len(effects) > 1:
            unsupported("Multiple effects for this training rule need an explicit composition")
        return effects[0] if effects else None

    def rarity_multiplier(self, config_ids, target_group, material_group):
        result = 100
        for effect in self.find(config_ids, 214):
            percent, same_group, third, fourth = effect.static()
            if percent < 0 or same_group not in (0, 1) or third or fourth:
                unsupported("This rarity multiplier has unsupported parameters")
            if not same_group or target_group == material_group:
                result = max(result, percent)
        return result

    def ownership_bonus(self, config_ids):
        effect = self.single(config_ids, 105)
        if effect is None:
            return 0
        percent, second, third, fourth = effect.static()
        if percent < 0 or second or third or fourth:
            unsupported("This ownership bonus has unsupported parameters")
        return percent

    def suppress_class_bonus(self, card_id):
        if card_id == 170:
            return True
        for row in self.specialties.get(card_id, ()):
            if row["Type_Specialty"] == 31:
                if row["Command"]:
                    unsupported("Conditional class-match bonuses are not implemented")
                return row["Value_Specialty"] != 0
        return False

    def material_multiplier(self, card_id, target_rarity, material_rarity, skill_type):
        kinds = []
        for row in self.specialties.get(card_id, ()):
            if row["Type_Specialty"] not in (8, 9, 10):
                continue
            if row["Command"]:
                unsupported("Conditional material multipliers are not implemented")
            kinds.append(row["Type_Specialty"])
        if len(kinds) > 1:
            unsupported("Multiple specialty multipliers require a verified selection order")
        kind = kinds[0] if kinds else 0
        same_group = target_rarity["RarityGroupID"] == material_rarity["RarityGroupID"]
        if kind == 8:
            return 200 if same_group else 150
        if kind == 9:
            return 200
        if kind == 10:
            return 200 if target_rarity["StarsCount"] == 0 else 150
        if skill_type == 100:
            if material_rarity["StarsCount"] == 0:
                return 200 if target_rarity["StarsCount"] == 0 else 150
            return 200 if same_group else 150
        return 100


def material_experience(card_exp, class_exp, level, combine_percent=100, rarity_percent=100):
    for value in (card_exp, class_exp, level, combine_percent, rarity_percent):
        _amount(value)
    card_value = _amount(combine_percent * card_exp) // 100
    class_value = _amount(class_exp * max(level - 1, 0))
    return _amount(rarity_percent * _amount(card_value + class_value)) // 100


def total_experience(base, class_bonus, multipliers, ownership_percent=0):
    base = _amount(base)
    class_bonus = _amount(class_bonus)
    _amount(ownership_percent)
    multiplier = float32(100)
    for value in multipliers:
        _amount(value)
        multiplier = float32(float32(float32(value) * multiplier) / float32(100))
    scaled = int(float32(float32(float32(_amount(base + class_bonus)) * multiplier) / float32(100)))
    return _amount(_amount(100 + ownership_percent) * _amount(scaled)) // 100

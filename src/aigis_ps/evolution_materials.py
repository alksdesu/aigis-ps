from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from .errors import ProtocolError
from .native_numbers import INT32_MIN, int32


EVOLUTION_MATERIALS_INVALID = "evolution_materials_invalid"


def _invalid(message):
    raise ProtocolError(EVOLUTION_MATERIALS_INVALID, message, 503)


def _integer(value, minimum=INT32_MIN):
    if not int32(value) or value < minimum:
        _invalid("Evolution material fields require bounded native integers")
    return value


def _field(row, name, minimum=INT32_MIN):
    if not isinstance(row, Mapping) or name not in row:
        _invalid(f"Evolution material data is missing {name}")
    return _integer(row[name], minimum)


def _attribute(unit, name):
    try:
        return getattr(unit, name)
    except AttributeError as error:
        raise ProtocolError(EVOLUTION_MATERIALS_INVALID, f"Evolution unit is missing {name}", 503) from error


def _optional(value):
    return value if value > 0 else None


@dataclass(frozen=True)
class MaterialCondition:
    class_id: int | None = None
    class_type: int | None = None
    rarity: int | None = None
    stage: int | None = None
    min_level: int | None = None
    card_id: int | None = None

    def __post_init__(self):
        for value in (self.class_id, self.class_type, self.rarity, self.stage, self.min_level, self.card_id):
            if value is not None:
                _integer(value, 0)

    def _matches(self, material):
        card_id, class_id, rarity, stage, level = material
        if self.card_id is not None and card_id == self.card_id:
            return True
        return ((self.class_id is None or class_id == self.class_id)
                and (self.class_type is None or class_id // 100 == self.class_type)
                and (self.rarity is None or rarity == self.rarity)
                and (self.stage is None or stage == self.stage)
                and (self.min_level is None or level >= self.min_level))


class EvolutionMaterials:
    def __init__(self, cards, classes, rarities):
        class_fields = tuple(f"JobChangeMaterial{i}" for i in range(1, 4)) + tuple(
            f"_LowRareEvolveMaterial_{i:02}" for i in range(1, 4))
        rarity_fields = ("ConfigGroupID", "HighRarity", "RequireFairyCardIdOfEvolve1", "RequireFairyCardIdOfEvolve2") + tuple(
            f"EvolutionRequire{field}{i:02}" for i in range(3) for field in ("Rarity", "LV", "CC", "GeneralCardId"))
        try:
            if not callable(cards.card) or not isinstance(classes, Mapping) or not isinstance(rarities, Mapping):
                _invalid("Evolution materials require card, class and rarity catalogs")
            card_rows = {}
            for card_id in cards.card_ids:
                _integer(card_id, 1)
                if card_id in card_rows:
                    _invalid("Evolution card identifiers are duplicated")
                row = cards.card(card_id)
                if _field(row, "CardID", 1) != card_id:
                    _invalid("Evolution card identity differs from its lookup key")
                card_rows[card_id] = MappingProxyType({"CardID": card_id, "InitClassID": _field(row, "InitClassID", 1),
                                                      "Rare": _field(row, "Rare", 0)})
            self._cards = MappingProxyType(card_rows)
            self._classes = self._rows(classes, "ClassID", class_fields)
            self._rarities = self._rows(rarities, "ConfigID", rarity_fields)
            if not self._cards or not self._classes or not self._rarities:
                _invalid("Evolution material catalogs must not be empty")
            groups = {}
            for config_id, row in sorted(self._rarities.items()):
                group_id = _field(row, "ConfigGroupID", 0)
                groups.setdefault(group_id, []).append(config_id)
                for operation in (1, 2):
                    fairy_id = row[f"RequireFairyCardIdOfEvolve{operation}"]
                    if fairy_id > 0 and (fairy_id not in self._cards or
                            self._cards[fairy_id]["InitClassID"] not in self._classes):
                        _invalid("Evolution fairy template or initial class is missing")
                for i in range(3):
                    card_id = row[f"EvolutionRequireGeneralCardId{i:02}"]
                    if card_id > 0 and card_id not in self._cards:
                        _invalid("Evolution replacement card is missing")
            self._groups = MappingProxyType({key: tuple(values) for key, values in groups.items()})
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, ProtocolError) and error.code == EVOLUTION_MATERIALS_INVALID:
                raise
            raise ProtocolError(EVOLUTION_MATERIALS_INVALID, "Evolution resources do not satisfy the material contract", 503) from error

    @staticmethod
    def _rows(rows, identity, fields):
        result = {}
        for key, row in rows.items():
            _integer(key, 1)
            if _field(row, identity, 1) != key:
                _invalid("Evolution resource identity differs from its lookup key")
            result[key] = MappingProxyType({identity: key, **{name: _field(row, name) for name in fields}})
        return MappingProxyType(result)

    def _descriptor(self, unit):
        card = _attribute(unit, "card")
        card_id = _field(card, "CardID", 1)
        rarity = _field(card, "Rare", 0)
        class_id = _field(_attribute(unit, "class_row"), "ClassID", 1)
        config_id = _field(_attribute(unit, "rarity"), "ConfigID", 1)
        if (card_id not in self._cards or self._cards[card_id]["Rare"] != rarity
                or class_id not in self._classes or config_id != rarity + 1 or config_id not in self._rarities):
            _invalid("Evolution unit does not match the verified catalogs")
        return card_id, class_id, rarity

    def slots(self, target, operation) -> tuple[tuple[MaterialCondition, ...], ...]:
        if type(operation) is not int or operation not in (7, 9):
            _invalid("Evolution material slots support operations 7 and 9")
        _, class_id, rarity = self._descriptor(target)
        cls = self._classes[class_id]
        cfg = self._rarities[rarity + 1]
        slots = []
        for i in range(3):
            required_class = cls[f"JobChangeMaterial{i + 1}"]
            low_class = cls[f"_LowRareEvolveMaterial_{i + 1:02}"]
            if not cfg["HighRarity"] and low_class:
                required_class = low_class
            required_class = required_class or class_id
            class_type = required_class // 100 if required_class >= 0 else -((-required_class) // 100)
            alternatives = []
            for config_id in self._groups[cfg["ConfigGroupID"]]:
                row = self._rarities[config_id]
                material_rarity = row[f"EvolutionRequireRarity{i:02}"]
                alternatives.append(MaterialCondition(class_type=class_type if class_type >= 0 else None,
                    rarity=material_rarity if material_rarity >= 0 else None,
                    stage=_optional(row[f"EvolutionRequireCC{i:02}"]),
                    min_level=_optional(row[f"EvolutionRequireLV{i:02}"]),
                    card_id=_optional(row[f"EvolutionRequireGeneralCardId{i:02}"])))
            slots.append(tuple(alternatives))
        fairy_id = cfg[f"RequireFairyCardIdOfEvolve{1 if operation == 7 else 2}"]
        if fairy_id not in self._cards:
            _invalid("This rarity has no evolution fairy template")
        fairy = self._cards[fairy_id]
        slots.append((MaterialCondition(class_id=fairy["InitClassID"], rarity=fairy["Rare"]),))
        return tuple(slots)

    def validate(self, target, materials, operation) -> tuple[int, ...]:
        slots = self.slots(target, operation)
        if not isinstance(materials, Sequence) or isinstance(materials, (str, bytes, bytearray)):
            _invalid("Evolution materials require a sequence of unit descriptions")
        if len(materials) != len(slots):
            raise ProtocolError("evolution_materials_mismatch", "Evolution requires exactly four materials", 409)
        values = [(*self._descriptor(material), _field(_attribute(material, "unit"), "ClassChangeCount", 0),
                   _integer(_attribute(material, "level"), 1)) for material in materials]
        matches = [tuple(index for index, material in enumerate(values) if any(
            condition._matches(material) for condition in alternatives)) for alternatives in slots]

        def assign(slot, used):
            if slot == len(slots):
                return ()
            for index in matches[slot]:
                if used & (1 << index):
                    continue
                remaining = assign(slot + 1, used | (1 << index))
                if remaining is not None:
                    return (index, *remaining)
            return None

        # The wire omits slot keys, so accept a complete assignment without relying on row order.
        result = assign(0, 0)
        if result is None:
            raise ProtocolError("evolution_materials_mismatch", "Materials cannot fill every evolution slot", 409)
        return result

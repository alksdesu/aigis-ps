from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
from types import MappingProxyType

from .errors import ProtocolError
from .native_numbers import int32
from .player_state import DISPLAY_FIELD, PRINCE_CARD_IDS, SKILL_SWITCH_FLAG
from .unit_initialization import CLASSIC_ROUTE_CARD_ID


# The native awakening menus also exclude IsCardID(80), whose classic route has no ordinary awakening.
AWAKENING_EXCLUDED_CARD_IDS = PRINCE_CARD_IDS | {CLASSIC_ROUTE_CARD_ID}


@dataclass(frozen=True)
class EvolutionRules:
    first_awake_experience: str = "overflow"

    def __post_init__(self):
        if self.first_awake_experience not in ("overflow", "total", "reset"):
            raise ValueError("First awakening experience policy must be overflow, total or reset")


@dataclass(frozen=True)
class EvolutionQuote:
    price: int
    items: Mapping[str, int]
    values: Mapping[str, int]
    material_assignment: tuple[int, ...]
    reward_card_id: int | None


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Evolution values must be bounded native integers")
    return value


def _ineligible(message):
    raise ProtocolError("evolution_target_ineligible", message, 409)


def first_awake_experience(experience: int, cap_experience: int, policy: str) -> int:
    _integer(experience)
    _integer(cap_experience)
    if policy not in ("overflow", "total", "reset"):
        raise ValueError("Unknown first awakening experience policy")
    return {"reset": 0, "total": experience, "overflow": max(0, experience - cap_experience)}[policy]


class EvolutionCatalog:
    def __init__(self, growth, possessions, materials, *, rules=EvolutionRules()):
        self.growth, self.possessions, self.materials, self.rules = growth, possessions, materials, rules
        self.cards = growth.cards
        self.classes = growth.classes
        self.rarities = growth.rarities
        self.content_version = self.content_version_for(growth.content_version)

    def content_version_for(self, growth_version):
        content = {"growth": growth_version, "rules": asdict(self.rules), "calculation_version": 2}
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    def replayable_content_versions(self):
        return frozenset(map(self.content_version_for, self.growth.replayable_content_versions()))

    def status(self):
        return {"content_version": self.content_version, "rules": asdict(self.rules),
                "native_tested": False, "platform_locks": "Current PC predicates are inactive",
                "classic_class_change": "Inactive in the current native client"}

    def route(self, card, stage):
        if card["CardID"] == CLASSIC_ROUTE_CARD_ID or not int32(stage) or not 0 <= stage <= 4:
            _ineligible("This unit has no supported ordinary evolution route")
        current = self.classes[_integer(card["InitClassID"], 1)]
        if stage >= 2:
            next_id = _integer(current["JobChange"])
            if next_id:
                current = self.classes[next_id]
        if stage >= 3:
            pattern = _integer(card["_AwakePattern"])
            if not pattern & (1 << (stage - 3)):
                _ineligible("Selected second awakening branch is unavailable for this card")
            current = self.classes[_integer(current[f"AwakeType{stage - 2}"], 1)]
        return current

    def costs(self, rarity, class_row):
        price = _integer(rarity["EvolutionPrice"], 1)
        count = _integer(rarity["EvolutionRequireOrb"])
        items = {}
        for field in ("Data_ExtraAwakeOrb1", "Data_ExtraAwakeOrb2"):
            code = _integer(class_row[field])
            if code and count:
                item = f"evolve_orb_{code // 100:06d}"
                if self.possessions.stack(item) <= 0:
                    raise ValueError("Evolution orb requires a positive item stack")
                items[item] = count
        return price, MappingProxyType(items)

    def skill_orb_class(self, class_row, stage):
        field = "JobChange" if stage <= 2 else {3: "AwakeType1", 4: "AwakeType2"}.get(stage)
        if field is None:
            raise ValueError("Skill awakening requires a supported class stage")
        parent = next((row for row in self.classes.values() if row[field] == class_row["ClassID"]), None)
        if parent is None:
            return class_row
        if stage >= 3:
            parent = next((row for row in self.classes.values() if row["JobChange"] == parent["ClassID"]), parent)
        return parent

    def quote(self, target, material_units, operation, branch=0):
        if (type(operation) is not int or operation not in (7, 8, 9) or type(branch) is not int
                or (operation != 9 and branch != 0) or (operation == 9 and branch not in (1, 2))):
            raise ProtocolError("evolution_operation_invalid", "Expected a current awakening operation and branch")
        try:
            view = self.growth.describe(target)
            card, rarity = view.card, view.rarity
            stage = _integer(target["ClassChangeCount"])
            if card["CardID"] in AWAKENING_EXCLUDED_CARD_IDS or not _integer(rarity["EvolutionConfig"]):
                _ineligible("This unit cannot use the current awakening operations")
            if self.route(card, stage)["ClassID"] != target["ClassID"]:
                _ineligible("Saved class does not match the card's awakening route")
            if operation == 7:
                if stage != 0 or view.level < view.cap or target["Love"] < 100:
                    _ineligible("First awakening requires an unevolved unit at its level cap and full affection")
                next_id = _integer(view.class_row["JobChange"])
                if not next_id:
                    _ineligible("First awakening class is unavailable")
                experience = first_awake_experience(target["Exp"], view.thresholds[view.cap - 1], self.rules.first_awake_experience)
                values = {"ClassID": next_id, "ClassChangeCount": 2, "Exp": experience, DISPLAY_FIELD: 0}
            elif operation == 9:
                if stage != 2 or target["Love"] < 100:
                    _ineligible("Second awakening requires the first awakened stage and full affection")
                values = {"ClassID": self.route(card, branch + 2)["ClassID"], "ClassChangeCount": branch + 2,
                          "Exp": target["Exp"], DISPLAY_FIELD: 0}
            else:
                if (stage < 2 or not _integer(card["EvoSkillID"]) or card["EvoSkillID"] == card["ClassLV1SkillID"]
                        or target["SkillEvolved"] or target["GeneralPurposeFlags"] & SKILL_SWITCH_FLAG
                        or view.skill_max <= 0 or view.unit["SkillLevel"] < view.skill_max):
                    _ineligible("Skill awakening requires an eligible fully leveled skill that has never awakened")
                values = {"SettingSkillID": card["EvoSkillID"],
                          "GeneralPurposeFlags": target["GeneralPurposeFlags"] | SKILL_SWITCH_FLAG, "SkillEvolved": 1}
            material_views = tuple(self.growth.describe(unit) for unit in material_units)
            if operation in (7, 9):
                assignment = self.materials.validate(view, material_views, operation)
            else:
                fairy = self.cards.card(_integer(rarity["RequireFairyCardIdOfSkillEvolve"], 1))
                if (len(material_views) != 1 or material_views[0].class_row["ClassID"] != fairy["InitClassID"]
                        or material_views[0].card["Rare"] != fairy["Rare"]):
                    raise ProtocolError("evolution_material_mismatch", "Skill awakening requires its configured fairy", 409)
                assignment = (0,)
            orb_class = self.skill_orb_class(view.class_row, stage) if operation == 8 else view.class_row
            price, items = self.costs(rarity, orb_class)
            self.growth.describe({**target, **values})
            return EvolutionQuote(price, items, MappingProxyType(values), assignment,
                                  card["CardID"] if operation == 9 and card["Rare"] == 5 else None)
        except ProtocolError:
            raise
        except (ValueError, KeyError, TypeError, IndexError) as error:
            raise ProtocolError("evolution_catalog_invalid", "Evolution resources or unit state differ from the verified contract", 503) from error

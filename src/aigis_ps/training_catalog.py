from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, astuple, dataclass
from functools import cached_property
import hashlib
from itertools import product
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError
from .fame_state import owned_fame_cards
from .native_numbers import float32, int32
from .player_state import PRINCE_CARD_IDS, SKILL_SWITCH_FLAG
from .training_math import advance_threshold, derived_level, experience_threshold, integration_price
from .training_effects import TrainingEffects, material_experience, total_experience
from .training_recipe import contributions, permits_zero_experience, validate_materials
from .training_conditions import ConditionExpression, name_index, named_values
from .system_literals import decode_command, rating_command_id, system_literals


LIMITS = ParseLimits(max_input_bytes=64 * 1024 * 1024, max_decompressed_bytes=64 * 1024 * 1024)
RESTRICTED_MATERIAL_CLASSES = frozenset((8, 11, 14, 30, 32, 39, 43))
# Fame registrations and fairy stock are counted per card, so their bonus names no barracks instance.
UNHELD_GET_INDEX = -1


@dataclass(frozen=True)
class TrainingRules:
    retain_overflow_experience: bool = True

    def __post_init__(self):
        if type(self.retain_overflow_experience) is not bool:
            raise ValueError("Training overflow policy must be boolean")


@dataclass(frozen=True)
class TrainingQuote:
    price: int
    values: Mapping[str, int]


@dataclass(frozen=True)
class TrainingUnit:
    card: Mapping
    effective_card: Mapping
    class_row: Mapping
    rarity: Mapping
    level: int
    thresholds: tuple[int, ...]
    cap: int
    skill_id: int
    skill_max: int
    skill_type: int
    initial_config: int
    ability_configs: tuple[int, ...]
    unit: Mapping


@dataclass(frozen=True)
class TrainingPreview:
    values: Mapping[str, int]
    gain: int
    skill: tuple[int, int]
    cost: tuple[int, int]


@dataclass(frozen=True)
class TrainingBoost:
    card_id: int
    get_index: int
    percent: int


NO_BOOST = TrainingBoost(0, 0, 0)


@dataclass(frozen=True)
class OwnershipBonus:
    percent: int
    sources: frozenset[TrainingBoost]

    @property
    def source(self) -> TrainingBoost:
        # Every source carries the same maximum percentage, so any of them yields the same quote.
        return min(self.sources, key=astuple)


def requested_boost(request) -> TrainingBoost:
    return TrainingBoost(request.boost_card_id, request.boost_get_index, request.boost_percent)


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _unsupported(message):
    raise ProtocolError("training_recipe_unsupported", message, 501)


def _digest(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


@contextmanager
def _growth_references():
    try:
        yield
    except ProtocolError:
        raise
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise ProtocolError("training_growth_reference", "Unit growth state or resource references are invalid", 503) from error


def _groups(rows, key):
    result, current = {}, None
    for row in rows:
        identifier = _integer(row[key])
        if identifier:
            if identifier in result:
                raise ValueError("Duplicate group identifier in training resources")
            current = identifier
            result[current] = []
        if current is None:
            raise ValueError("Training resource starts with an unowned continuation row")
        result[current].append(MappingProxyType(row))
    return {key: tuple(value) for key, value in result.items()}


class TrainingCatalog:
    def __init__(self, cards, players, asset_reader, aliases, *, rules=TrainingRules(), fairy_farm=None, silver_house=None):
        self.cards, self.aliases, self.rules = cards, aliases, rules
        self.fairy_farm = fairy_farm
        self.silver_house = silver_house
        self._reader = asset_reader
        self._hashes = {}
        self._classes = {key: MappingProxyType(dict(row)) for key, row in players.classes.items()}
        try:
            self._specialties = _groups(self._table("UnitSpecialty.atb"), "ID_Card")
            self._abilities = _groups(self._table("AbilityConfig.atb"), "_ConfigID")
            self.effects = TrainingEffects([dict(row) for rows in self._abilities.values() for row in rows],
                                           [dict(row) for rows in self._specialties.values() for row in rows])
            self._ability_list = tuple(MappingProxyType(row) for row in self._table("AbilityList.atb"))
            self._skills = tuple(MappingProxyType(row) for row in self._table("SkillList.atb"))
            self._skill_exp = self._thresholds("SkillExpTable.atb", 1, 16)
            self._cost_exp = self._thresholds("CostExpTable.atb", 0, 5)
            rarities = self._table("cfgRarity.atb")
            self._rarities = {_integer(row["ConfigID"], 1): MappingProxyType(row) for row in rarities}
            if len(self._rarities) != len(rarities):
                raise ValueError("Duplicate rarity identifier")
            rows = self._table("ExperienceTable.atb")
            if any(row["Level"] != index + 1 for index, row in enumerate(rows)):
                raise ValueError("Unordered experience levels")
            self._experience = tuple(_integer(row["Exp"]) for row in rows)
            if not self._experience or self._experience[0] != 0 or any(
                    left >= right for left, right in zip(self._experience, self._experience[1:])):
                raise ValueError("Invalid cumulative experience thresholds")
            for skill in self._skills:
                _integer(skill["LevelMax"])
            self._combine = {}
            for row in self._table("cfgcmbcmp.atb"):
                rarity = _integer(row["_Rarity"])
                if rarity in self._combine:
                    raise ValueError("Duplicate combine rarity")
                percentages = []
                for name in ("_Male", "_Female"):
                    value = row[name]
                    if type(value) not in (int, float) or not 0 <= value <= 100000:
                        raise ValueError("Invalid combine multiplier")
                    percentages.append(_integer(int(float32(value * 100))))
                self._combine[rarity] = tuple(percentages)
            self._combine_ignore = {_integer(row["_CardID"]) for row in self._table("cfgcmbignuni.atb")}
            settings = system_literals(self._table("OnSystemCommand.atb"),
                                       {"ExclusiveCombineClassIds": "7,9,13,15,16"})
            self.exclusive_classes = self._id_list(settings["ExclusiveCombineClassIds"])
            self._genus_names = name_index(self._table("PlayerGenusType.atb"))
            self._identity_names = name_index(self._table("PlayerIdentityType.atb"))
            self._children = {card_id: self._id_list(cards.card(card_id)["ChildCardID"])
                              for card_id in cards.card_ids}
            if any(set(children) - set(cards.card_ids) for children in self._children.values()):
                raise ValueError("Child material references a missing card")
            commands = {}
            for row in self._table("OnCommand.atb"):
                commands.setdefault(row["_ID"], []).append(row["_Cmnd"])
            self._rating_restricted = set()
            for card_id in cards.card_ids:
                entries = commands.get(rating_command_id(card_id), ())
                if len(entries) > 1 or (entries and decode_command(entries[0]).strip()):
                    self._rating_restricted.add(card_id)
            self._content = {"master": cards.snapshot.identifier, "client": cards.snapshot.client_hash,
                             "calculation_version": 2,
                             "fairy_farm": fairy_farm.content_version if fairy_farm is not None else None,
                             "silver_house": silver_house.content_version if silver_house is not None else None,
                             "version": cards.resource_version, "resources": self._hashes,
                             "classes": self._classes_digest(), "rules": asdict(rules),
                             "aliases": aliases.status()["resource_sha256"]}
            self.content_version = _digest(self._content)
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise ProtocolError("training_catalog_invalid", "Training resources do not satisfy the verified contract", 503) from error

    @property
    def classes(self):
        return MappingProxyType(self._classes)

    @property
    def rarities(self):
        return MappingProxyType(self._rarities)

    @property
    def skills(self):
        return tuple(self._skills)

    @property
    def experience(self):
        return tuple(self._experience)

    def replayable_content_versions(self):
        return frozenset((self.content_version, *self._absent_versions))

    @cached_property
    def _absent_versions(self):
        # Receipts committed while a warehouse, or an optional input of its own, was absent keep that digest.
        farms = {None} if self.fairy_farm is None else {None, *self.fairy_farm.replayable_content_versions()}
        houses = {None} if self.silver_house is None else {None, self.silver_house.content_version}
        current = self._content["fairy_farm"], self._content["silver_house"]
        return tuple(_digest({**self._content, "fairy_farm": farm, "silver_house": house})
                     for farm, house in product(farms, houses) if (farm, house) != current)

    def _classes_digest(self):
        rows = {key: dict(row) for key, row in self._classes.items()}
        return hashlib.sha256(json.dumps(rows, sort_keys=True, allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _id_list(text):
        if text in ("", "null"):
            return ()
        if not isinstance(text, str) or len(text) > 65536 or not re.fullmatch(r"[0-9]+(?:,[0-9]+)*,?", text):
            raise ValueError("Expected a comma-separated identifier list")
        result = tuple(_integer(int(value), 1) for value in text.rstrip(",").split(","))
        if len(set(result)) != len(result):
            raise ValueError("Identifier list contains duplicates")
        return result

    def _table(self, name):
        raw = self._reader(name)
        if not isinstance(raw, bytes) or len(raw) > LIMITS.max_input_bytes:
            raise ValueError("Training resource is not bounded bytes")
        self._hashes[name] = hashlib.sha256(raw).hexdigest()
        payload = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
        return parse_table(payload, LIMITS)["rows"]

    def _thresholds(self, name, first, last):
        result = {}
        for row in self._table(name):
            key = _integer(row["ConfigId"], 1)
            if key in result:
                raise ValueError("Duplicate growth configuration")
            result[key] = MappingProxyType({level: _integer(row[f"Lv{level}"], -1) for level in range(first, last + 1)})
        return result

    def status(self):
        return {"ready": True, "content_version": self.content_version,
                "resource_sha256": dict(self._hashes), "rules": asdict(self.rules),
                "scope": "Persisted class stages 0..4, native material rules and current conditional recipes",
                "child_relations": sum(map(len, self._children.values())),
                "fairy_farm_enabled": self.fairy_farm is not None,
                "silver_house_enabled": self.silver_house is not None,
                "rating_conditions_unavailable": sorted(self._rating_restricted),
                "native_exclusions": ["Temporary unit overrides, which exist only inside native battles"],
                "resource_tripwires": ["Influence 214 in character abilities, which no current ability reaches",
                                       "Effect conditions or system expressions that current resources do not use"],
                "official_server_rules": False}

    def describe(self, unit):
        card = self.cards.card(unit["CardID"])
        effective = self.cards.card(self.aliases.resolve(card["CardID"]).image_card_id)
        cls = self._classes[unit["ClassID"]]
        initial = self._classes[card["InitClassID"]]
        cc = _integer(unit["ClassChangeCount"])
        if cc > 4:
            _unsupported("This class-change level has no verified growth table")
        rarity = self._rarities[_integer(card["Rare"]) + 1]
        cap = min(_integer(cls["MaxLevel"], 1), _integer(rarity[f"LevelCap{cc:02}"], 1))
        thresholds = tuple(experience_threshold(exp, _integer(rarity["RequireExpRatio"], 1)) for exp in self._experience)
        level = derived_level(unit["Exp"], thresholds, cap)
        skill_id = card["EvoSkillID"] if cc >= 2 and unit["GeneralPurposeFlags"] & SKILL_SWITCH_FLAG else unit["SettingSkillID"]
        skill = self._skills[_integer(skill_id)]
        configs = {_integer(cls["ClassAbility1"])}
        for variant in (card, effective):
            for field in ("Ability", "Ability_Default"):
                ability = self._ability_list[_integer(variant[field])]
                config_id = _integer(ability["_ConfigID"])
                if self.effects.find((config_id,), 214):
                    _unsupported("This character ability needs a verified active-variant selection")
        if card["CardID"] in self._rating_restricted:
            _unsupported("This unit has an unverified rating restriction")
        effective_unit = {**unit, "SkillLevel": max(1, unit["SkillLevel"])}
        return TrainingUnit(card, effective, cls, rarity, level, thresholds, cap,
                            skill_id, _integer(skill["LevelMax"]), _integer(skill["SkillType"]),
                            _integer(initial["ClassAbility1"]), tuple(configs), MappingProxyType(effective_unit))

    @staticmethod
    def _related(left, right):
        fields = ("Assign", "Genus", "Blood")
        if any(left[field] != 0 and left[field] == right[field] for field in fields):
            return True
        fields = ("Identity", "Identity2", "Identity3", "Identity4", "Identity5", "Identity6", "IdentityEVT")
        return bool(({left[field] for field in fields} & {right[field] for field in fields}) - {0})

    def _condition(self, command, target):
        def predicate(name, arguments):
            if name == "IsGenus":
                return target.effective_card["Genus"] in named_values(arguments, self._genus_names)
            if name == "IsIdentitiy":
                identities = {target.effective_card[field] for field in
                    ("Identity", "Identity2", "Identity3", "Identity4", "Identity5", "Identity6", "IdentityEVT")}
                return bool(identities & set(named_values(arguments, self._identity_names)))
            if name in ("IsCardID", "IsClassType") and arguments and all(type(value) is int for value in arguments):
                value = target.card["CardID"] if name == "IsCardID" else target.class_row["ClassID"] // 100
                return value in arguments
            if name in ("IsVanguard", "IsRearguard") and not arguments:
                return bool(target.class_row["BattleStyle"] & (1 if name == "IsVanguard" else 2))
            _unsupported("This training condition requires an unsupported predicate")
        return ConditionExpression(command, predicate).evaluate()

    def _combine_percent(self, card):
        if card["CardID"] in self._combine_ignore or card["Kind"] not in (0, 1):
            return 100
        return self._combine.get(card["Rare"], (100, 100))[card["Kind"]]

    def _ownership_source(self, card_id, get_index):
        card = self.cards.card(card_id)
        percent = self.effects.ownership_bonus((self._classes[card["InitClassID"]]["ClassAbility1"],))
        return TrainingBoost(card["CardID"], get_index, percent) if percent else None

    def ownership_bonus(self, player, state):
        with _growth_references():
            candidates = {self._ownership_source(card_id, UNHELD_GET_INDEX)
                          for card_id in owned_fame_cards(state, self.cards.card_ids)}
            candidates.update(self._ownership_source(owned["CardID"], owned["GetIndex"]) for owned in player["units"])
            if self.fairy_farm is None:
                if state["tables"].get("fairy_farm", []) != []:
                    _unsupported("Fairy-farm ownership must be decoded before applying its bonus")
            else:
                candidates.update(self._ownership_source(owned["CardID"], UNHELD_GET_INDEX)
                                  for owned in self.fairy_farm.read(state) if owned["Count"] > 0)
            candidates.discard(None)
            maximum = max((source.percent for source in candidates), default=0)
            sources = (frozenset(source for source in candidates if source.percent == maximum) if maximum
                       else frozenset((NO_BOOST,)))
            return OwnershipBonus(maximum, sources)

    @staticmethod
    def _require_boost(bonus, boost):
        if boost not in bonus.sources:
            raise ProtocolError("training_bonus_changed", "Training bonus no longer matches the owned sources", 409)

    def quote_materials(self, player, target, materials, *, state, boost=None):
        with _growth_references():
            if state.get("unit_dynamic_overrides"):
                _unsupported("Temporary unit overrides require a verified state adapter")
            bonus = self.ownership_bonus(player, state)
            # Without a named source the quote takes the maximum bonus, which is the one the native menu offers.
            if boost is not None:
                self._require_boost(bonus, boost)
            return self._quote(target, materials, bonus.percent)

    def quote(self, player, config, request, target, materials, *, state):
        # Native 5 requests name the bonus source their client showed; the configuration row plays no part.
        return self.quote_materials(player, target, materials, state=state, boost=requested_boost(request))

    def _quote(self, target, materials, bonus):
        if not 1 <= len(materials) <= 10:
            raise ProtocolError("training_material_count", "Training requires between one and ten materials")
        base = self.describe(target)
        if (base.card["CardID"] in PRINCE_CARD_IDS or base.class_row["ClassID"] // 100 == 0
                or 100 <= base.effective_card["Kind"] <= 120):
            raise ProtocolError("training_target_ineligible", "This unit cannot be a training target", 409)
        selected = tuple(self.describe(material) for material in materials)
        if any(item.class_row["ClassID"] in RESTRICTED_MATERIAL_CLASSES for item in selected):
            raise ProtocolError("training_material_ineligible", "This class cannot be used as training material", 409)
        validate_materials(base, selected, self.effects, exclusive_classes=self.exclusive_classes, condition=self._condition)
        previous = self._calculate(base, (), bonus)
        for count in range(1, len(selected) + 1):
            current = self._calculate(base, selected[:count], bonus)
            if self._redundant_selection(base, previous, current):
                raise ProtocolError("training_material_redundant", "A selected material cannot improve the current training preview", 409)
            previous = current
        if current.gain <= 0 and not permits_zero_experience(selected, self.effects):
            raise ProtocolError("training_no_experience", "Selected materials cannot provide training experience", 409)
        return TrainingQuote(integration_price(base.level, len(selected)), current.values)

    @staticmethod
    def _redundant_selection(target, previous, current):
        def growth_warning(original, maximum, before, after, before_state, after_state):
            if after == (-1, 0) or after <= before:
                return False
            return original >= maximum or (before_state == after_state and after_state[0] >= maximum)
        skill_warning = growth_warning(target.unit["SkillLevel"], target.skill_max, previous.skill, current.skill,
            (previous.values["SkillLevel"], previous.values["SkillExperience"]),
            (current.values["SkillLevel"], current.values["SkillExperience"]))
        cost_warning = growth_warning(-target.unit["CostModMixValue"], target.card["CostDecValue"], previous.cost, current.cost,
            (-previous.values["CostModMixValue"], previous.values["CostExperience"]),
            (-current.values["CostModMixValue"], current.values["CostExperience"]))
        has_skill, has_cost = current.skill != previous.skill, current.cost != previous.cost
        warning = (skill_warning and cost_warning if has_skill and has_cost else
                   skill_warning if has_skill else cost_warning if has_cost else False)
        if not warning:
            return False
        if target.level >= target.cap or previous.gain == current.gain:
            return True
        if previous.gain > current.gain:
            return False
        before_level = derived_level(target.unit["Exp"] + previous.gain, target.thresholds, target.cap)
        after_level = derived_level(target.unit["Exp"] + current.gain, target.thresholds, target.cap)
        return before_level == after_level and before_level >= target.cap

    def _calculate(self, target, materials, bonus):
        total = class_bonus = skill_exp = skill_levels = cost_exp = cost_levels = 0
        multipliers = []
        children = self._children[target.card["CardID"]]
        for item in materials:
            factor = self.effects.rarity_multiplier(item.ability_configs, target.rarity["RarityGroupID"], item.rarity["RarityGroupID"])
            total += material_experience(item.card["BuildExp"], item.class_row["BuildExp"], item.level,
                                         self._combine_percent(item.card), factor)
            if (item.class_row["ClassID"] // 100 == target.class_row["ClassID"] // 100
                    and not self.effects.suppress_class_bonus(item.card["CardID"])):
                class_bonus += item.rarity["ClassMatchBonus"]
            multipliers.append(self.effects.material_multiplier(item.card["CardID"], target.rarity, item.rarity, item.skill_type))
            child = item.card["CardID"] in children
            skill_direct, skill_gain, cost_direct, cost_gain = contributions(target, item, self.effects,
                child_skill=child, child_cost=child, related=self._related(target.card, item.card))
            skill_exp += skill_gain
            skill_levels += skill_direct
            cost_exp += cost_gain
            cost_levels += cost_direct
        gain = total_experience(total, class_bonus, multipliers, bonus)
        experience = target.unit["Exp"] + gain
        if not self.rules.retain_overflow_experience:
            experience = max(target.unit["Exp"], min(experience, target.thresholds[target.cap - 1]))
        if not int32(experience):
            raise ProtocolError("training_experience_capacity", "Accumulated experience exceeds the native field", 409)
        skill_level, skill_left = target.unit["SkillLevel"], 0
        if target.skill_max:
            config_id = target.card["SkillExperienceTableReferenceId"] or target.skill_max
            skill_level, skill_left = advance_threshold(skill_level, target.unit["SkillExperience"], target.skill_max,
                self._skill_exp[config_id], gained_exp=skill_exp, direct_levels=skill_levels)
        config_id = target.card["CostExperienceTableReferenceId"] or target.card["CostDecValue"] + 1
        cost_level, cost_left = advance_threshold(-target.unit["CostModMixValue"], target.unit["CostExperience"],
            target.card["CostDecValue"], self._cost_exp[config_id], gained_exp=cost_exp, direct_levels=cost_levels)
        return TrainingPreview(MappingProxyType({
            "Exp": experience, "SkillLevel": skill_level, "CostModMixValue": -cost_level,
            "SkillExperience": skill_left, "CostExperience": cost_left}), gain,
            (skill_levels, skill_exp), (cost_levels, cost_exp))

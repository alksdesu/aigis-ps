from __future__ import annotations

from collections.abc import Mapping
from functools import cached_property
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .card_status import CARD_STATUS_FIELDS, MAX_CARD_STATUS_ROWS
from .errors import ProtocolError, caught
from .native_numbers import INT32_MAX, INT32_MIN, int32
from .player_state import DISPLAY_FIELD, MAX_PLAYER_UNITS, SKILL_SWITCH_FLAG
from .system_literals import command_id, decode_command
from .training_math import derived_level, experience_threshold


LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024)
STAGES = range(5)
CLASSIC_CHANGE_STAGE = 1
CARD_FIELDS = ("CardID", "InitClassID", "Rare", "Kind", "InHallOfFamer", "InLibrary", "SellPrice",
               "BonusType", "BonusType3", "CostDecValue", "ClassLV0SkillID", "ClassLV1SkillID",
               "EvoSkillID", "_AwakePattern", "Blood", "ExcludeFameDisplayFirstClass")
CLASS_FIELDS = ("ClassID", "MaxLevel", "JobChange", "AwakeType1", "AwakeType2")
RARITY_FIELDS = ("ConfigID", "RequireExpRatio", "EvolutionConfig", "LevelCap00", "LevelCap01",
                 "LevelCap02", "LevelCap03", "LevelCap04")
UNIT_FIELDS = ("CardID", "ClassID", "ClassChangeCount", "Exp", "Love", "SkillLevel", "SettingSkillID",
               "GeneralPurposeFlags", "SkillEvolved", "CostModMixValue")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _selected(row, names):
    if not isinstance(row, Mapping):
        raise ValueError("Expected a resource row")
    return {name: _integer(row[name], INT32_MIN if name == "ExcludeFameDisplayFirstClass" else 0) for name in names}


class FameCatalog:
    def __init__(self, growth, evolution, asset_reader):
        self.growth, self.evolution = growth, evolution
        try:
            self.snapshot = growth.cards.snapshot
            if evolution.growth is not growth or not callable(asset_reader):
                raise ValueError("Fame requires one consistent growth resource view")
            self._versions = self._dependency_versions()
            self._cards = MappingProxyType({key: MappingProxyType(_selected(growth.cards.card(key), CARD_FIELDS))
                                           for key in growth.cards.card_ids})
            if any(key != row["CardID"] for key, row in self._cards.items()):
                raise ValueError("Fame card keys must match their identifiers")
            self._classes = MappingProxyType({key: MappingProxyType(_selected(row, CLASS_FIELDS))
                                             for key, row in growth.classes.items()})
            self._rarities = MappingProxyType({key: MappingProxyType(_selected(row, RARITY_FIELDS))
                                              for key, row in growth.rarities.items()})
            self._skills = tuple(_integer(row["LevelMax"]) for row in growth.skills)
            self._experience = tuple(_integer(value) for value in growth.experience)
            self._aliases = MappingProxyType({key: growth.aliases.resolve(key).image_card_id for key in self._cards})
            if not self._cards or not self._skills or not self._experience:
                raise ValueError("Fame resources must not be empty")
            hashes = {}

            def table(name):
                raw = asset_reader(name)
                if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Fame resource must be bounded bytes")
                hashes[name] = hashlib.sha256(raw).hexdigest()
                return parse_table(decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw, LIMITS)["rows"]

            blood = table("PlayerBloodType.atb")
            hero_key = command_id("英傑")
            heroes = [row for row in blood if row["_ResID"] == hero_key]
            if len(heroes) != 1:
                raise ValueError("Fame display requires one hero blood definition")
            self._hero_blood = _integer(heroes[0]["_TypeID"])
            deposit = [re.sub(r"\s+", "", decode_command(row["_Cmnd"]))
                       for row in table("OnCommandMenuUnitLibraryDeposit.atb")]
            if deposit.count("(IsFameRegisterd()&&CanFame())&&!IsOrganized();") != 1:
                raise ValueError("Fame deposit eligibility expression changed")
            self._deposit_limit = self._menu_literal(deposit, "DepositLimit", INT32_MAX)
            self._deposit_batch_limit = self._menu_literal(deposit, "MaxMultiSelect", MAX_PLAYER_UNITS)
            portal = [re.sub(r"\s+", "", decode_command(row["_Cmnd"]))
                      for row in table("OnCommandMenuUnitLibraryPortal.atb")]
            rank_expression = re.compile(r'if\(GetRank\(\)>=([0-9]+)\)\{SetUpdaterFunc\("Func_UnitLibraryPortal"\);\}'
                                         r'else\{CMenuTask_ShowDialog\("Text=FameOpenCondition"\);\}')
            ranks = [int(match.group(1)) for command in portal if (match := rank_expression.fullmatch(command))]
            if len(ranks) != 1 or not 1 <= ranks[0] <= INT32_MAX:
                raise ValueError("Fame rank condition is missing or ambiguous")
            self._rank_min = ranks[0]
            self._card_ids = frozenset(key for key, row in self._cards.items() if row["InHallOfFamer"] & 0xff)
            self._counted_card_ids = frozenset(key for key, row in self._cards.items()
                if row["InLibrary"] & 0xff or (row["SellPrice"] > 0 and row["InHallOfFamer"] & 0xff))
            self._support_card_ids = frozenset(key for key in self.counted_card_ids if 100 <= self._cards[key]["Kind"] <= 120)
            self._hashes = MappingProxyType(hashes)
            self._facts = {}
            for key in self.counted_card_ids | self.card_ids:
                self._facts[key] = MappingProxyType(self._build_facts(key))
            self._facts = MappingProxyType(self._facts)
            self._content = {"version": 2, "dependencies": self._versions, "resources": hashes,
                             "cards": {key: dict(row) for key, row in self._cards.items()},
                             "classes": {key: dict(row) for key, row in self._classes.items()},
                             "rarities": {key: dict(row) for key, row in self._rarities.items()},
                             "skills": self._skills, "experience": self._experience, "aliases": dict(self._aliases),
                             "deposit_batch_limit": self._deposit_batch_limit,
                             "withdrawal": "maximum_growth_zero_remainders",
                             "classic_stage_one": "eligible_by_own_class_without_storage"}
            self.content_version = _digest(self._content)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, OSError) as error:
            raise ProtocolError("fame_catalog_invalid", "Fame resources differ from the verified contract", 503) from error

    @staticmethod
    def _menu_literal(commands, name, maximum):
        values = [int(value) for command in commands for value in re.findall(rf'"{name}=([0-9]+)"', command)]
        if len(values) != 1 or not 1 <= values[0] <= maximum:
            raise ValueError(f"Fame deposit menu {name} is missing or ambiguous")
        return values[0]

    @property
    def card_ids(self):
        return self._card_ids

    @property
    def eligible_card_ids(self):
        return self._card_ids

    @property
    def counted_card_ids(self):
        return self._counted_card_ids

    @property
    def support_card_ids(self):
        return self._support_card_ids

    @property
    def deposit_limit(self):
        return self._deposit_limit

    @property
    def deposit_batch_limit(self):
        return self._deposit_batch_limit

    @property
    def rank_min(self):
        return self._rank_min

    def _dependency_versions(self):
        versions = {"growth": self.growth.content_version, "evolution": self.evolution.content_version,
                    "aliases": _digest(self.growth.aliases.status()["resource_sha256"])}
        if any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in versions.values()):
            raise ValueError("Fame dependencies need content fingerprints")
        return versions

    def replayable_content_versions(self):
        return frozenset((self.content_version, *self._absent_versions))

    @cached_property
    def _absent_versions(self):
        # Optional growth inputs reach fame only through the growth and evolution digests.
        dependencies = ({**self._versions, "growth": growth, "evolution": self.evolution.content_version_for(growth)}
                        for growth in self.growth.replayable_content_versions() if growth != self._versions["growth"])
        return tuple(_digest({**self._content, "dependencies": versions}) for versions in dependencies)

    def _ready(self):
        try:
            if (self._dependency_versions() != self._versions
                    or self.growth.cards.snapshot != self.snapshot
                    or set(self.growth.cards.card_ids) != self._cards.keys()
                    or self.growth.experience != self._experience):
                raise ValueError("Fame dependencies changed")
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ProtocolError("fame_catalog_changed", "Fame dependencies changed after setup", 503) from error

    def is_ready(self):
        return caught(self._ready) is None

    def _card(self, card_id):
        if not int32(card_id) or card_id not in self._cards:
            raise ProtocolError("fame_card_unknown", "Fame card is not in the verified master", 503)
        row = self._cards[card_id]
        try:
            if _selected(self.growth.cards.card(card_id), CARD_FIELDS) != row:
                raise ValueError("Changed fame card")
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ProtocolError("fame_catalog_changed", "Fame card definition changed after setup", 503) from error
        return row

    def _class(self, class_id):
        row = self._classes[class_id]
        if row["ClassID"] != class_id or _selected(self.growth.classes[class_id], CLASS_FIELDS) != row:
            raise ValueError("Fame class definition changed")
        return row

    def _rarity(self, card):
        key = card["Rare"] + 1
        row = self._rarities[key]
        if row["ConfigID"] != key or _selected(self.growth.rarities[key], RARITY_FIELDS) != row:
            raise ValueError("Fame rarity definition changed")
        return row

    def _skill_max(self, skill_id):
        _integer(skill_id)
        if skill_id >= len(self._skills) or self.growth.skills[skill_id]["LevelMax"] != self._skills[skill_id]:
            raise ValueError("Fame skill definition changed or is missing")
        return self._skills[skill_id]

    def _route(self, card, stage):
        result = self.evolution.route(card, stage)
        return self._class(result["ClassID"])

    def _build_facts(self, card_id):
        card = self._card(card_id)
        initial, rarity = self._class(card["InitClassID"]), self._rarity(card)
        first = bool(initial["JobChange"] and rarity["EvolutionConfig"])
        advanced = self._class(initial["JobChange"]) if first else initial
        second = bool(card["_AwakePattern"] and rarity["EvolutionConfig"]
                      and (advanced["AwakeType1"] or advanced["AwakeType2"]))
        branches = tuple(stage for stage in (3, 4) if second and card["_AwakePattern"] & (1 << (stage - 3))
                         and advanced[f"AwakeType{stage - 2}"])
        for stage in branches:
            self._route(card, stage)
        storage = branches if card["_AwakePattern"] else ((2,) if first else (0,))
        display = (() if card["Blood"] == self._hero_blood or card["ExcludeFameDisplayFirstClass"] & 0xff else (0,))
        display += ((2,) if first else ()) + branches
        skill = bool(card["EvoSkillID"] and card["EvoSkillID"] != card["ClassLV1SkillID"] and rarity["EvolutionConfig"])
        for name in ("ClassLV0SkillID", "ClassLV1SkillID", "EvoSkillID"):
            self._skill_max(card[name])
        trust = 0 if not card["BonusType"] else 150 if card["BonusType3"] else 100
        return {"storage": storage, "display": display, "required_stage": 3 if second else 2 if first else 0,
                "skill_awakened": skill, "trust": trust}

    def _facts_for(self, card_id):
        try:
            if self._build_facts(card_id) != self._facts[card_id]:
                raise ValueError("Changed fame stage definitions")
            return self._facts[card_id]
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            raise ProtocolError("fame_catalog_changed", "Fame stage definitions changed after setup", 503) from error

    def stages(self, card_id):
        self._ready()
        self._card(card_id)
        return self._facts_for(card_id)["storage"] if card_id in self.card_ids else ()

    def _thresholds(self, card, stage, class_row):
        rarity = self._rarity(card)
        cap = min(_integer(class_row["MaxLevel"], 1), _integer(rarity[f"LevelCap{stage:02d}"], 1))
        thresholds = tuple(experience_threshold(value, rarity["RequireExpRatio"]) for value in self._experience)
        if cap > len(thresholds):
            raise ValueError("Fame level cap exceeds the experience table")
        return cap, thresholds

    def eligible(self, unit):
        self._ready()
        try:
            if not isinstance(unit, Mapping) or any(not int32(unit.get(name)) for name in UNIT_FIELDS):
                raise ValueError("Fame eligibility requires typed persisted unit fields")
            if any(unit[name] < 0 for name in ("Exp", "Love", "SkillLevel", "SettingSkillID", "SkillEvolved")):
                raise ValueError("Fame unit growth cannot be negative")
            card = self._card(unit["CardID"])
            if card["CardID"] not in self.card_ids:
                return False
            stage = unit["ClassChangeCount"]
            if stage not in STAGES:
                return False
            facts = self._facts_for(card["CardID"])
            if (3 if stage == 4 else stage) < facts["required_stage"]:
                return False
            if stage == CLASSIC_CHANGE_STAGE:
                # Native 4D1190 caps legacy class changes by the unit's own class; no evolution route reaches stage one.
                expected = self._class(unit["ClassID"])
            else:
                if stage not in facts["display"]:
                    return False
                expected = self._route(card, stage)
                if unit["ClassID"] != expected["ClassID"]:
                    return False
            cap, thresholds = self._thresholds(card, stage, expected)
            skill_ready = bool(unit["SkillEvolved"] or unit["GeneralPurposeFlags"] & SKILL_SWITCH_FLAG)
            if not facts["skill_awakened"]:
                skill_id = (card["EvoSkillID"] if stage >= 2 and unit["GeneralPurposeFlags"] & SKILL_SWITCH_FLAG
                            else unit["SettingSkillID"])
                skill_ready |= max(1, unit["SkillLevel"]) >= self._skill_max(skill_id)
            return (unit["Love"] >= facts["trust"] and derived_level(unit["Exp"], thresholds, cap) >= cap
                    and skill_ready and -unit["CostModMixValue"] >= card["CostDecValue"])
        except ProtocolError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            raise ProtocolError("fame_unit_invalid", "Fame unit or growth references are invalid", 503) from error

    def require_eligible(self, unit):
        if not self.eligible(unit):
            raise ProtocolError("fame_ineligible", "Unit has not reached the required fame growth state", 409)

    def _status_rows(self, rows):
        if not isinstance(rows, (tuple, list)) or len(rows) > MAX_CARD_STATUS_ROWS:
            raise ProtocolError("fame_status_invalid", "Fame requires bounded card-status rows", 503)
        result = {}
        for row in rows:
            if (not isinstance(row, Mapping) or set(row) != set(CARD_STATUS_FIELDS)
                    or any(not int32(value) for value in row.values())
                    or row["CardID"] not in self._cards or row["CardID"] in result):
                raise ProtocolError("fame_status_invalid", "Fame card-status rows are invalid or duplicated", 503)
            result[row["CardID"]] = row
            self._card(row["CardID"])
        return result

    def registered(self, card_status_rows, card_id, stage):
        self._ready()
        self._card(card_id)
        rows = self._status_rows(card_status_rows)
        if not int32(stage) or stage not in STAGES:
            raise ProtocolError("fame_stage_invalid", "Fame stage is outside the native range")
        facts = self._facts_for(card_id) if card_id in self._facts else None
        if facts is None or stage not in facts["display"] or card_id not in rows:
            return False
        accepted = {0: (0, 2, 3, 4), 1: (1,), 2: (2, 3, 4), 3: (3,), 4: (4,)}[stage]
        return any(rows[card_id][f"B{value}"] != 0 for value in accepted)

    def counts(self, card_status_rows):
        self._ready()
        rows = self._status_rows(card_status_rows)
        values = {key: sum(row[f"B{stage}"] != 0 for stage in STAGES)
                  for key, row in rows.items() if key in self.counted_card_ids}
        return {"total": sum(values.values()), "support": sum(value for key, value in values.items() if key in self.support_card_ids)}

    def maximum_values(self, card_id, stage):
        if stage not in self.stages(card_id) or not int32(stage):
            raise ProtocolError("fame_stage_unsupported", "This card has no supported withdrawal stage", 501)
        try:
            card, facts = self._card(card_id), self._facts[card_id]
            cls = self._route(card, stage)
            cap, thresholds = self._thresholds(card, stage, cls)
            awakened = facts["skill_awakened"] and stage >= 2
            image_id = self.growth.aliases.resolve(card_id).image_card_id
            if image_id != self._aliases[card_id]:
                raise ValueError("Fame image alias changed")
            skill_id = self._card(image_id)["EvoSkillID"] if awakened else card["ClassLV0SkillID"]
            self._skill_max(skill_id)
            result = {"ClassID": cls["ClassID"], "ClassChangeCount": stage, "Exp": thresholds[min(99, cap) - 1],
                    "Love": facts["trust"], "SkillLevel": max(1, min(99, self._skill_max(card["ClassLV0SkillID"]))),
                    "SettingSkillID": skill_id,
                    "CostModMixValue": -card["CostDecValue"], "SkillEvolved": int(awakened),
                    "GeneralPurposeFlags": 1 | SKILL_SWITCH_FLAG if awakened else 1,
                    "SkillExperience": 0, "CostExperience": 0, "ClassChangeStatusMod": 0,
                    DISPLAY_FIELD: 0, "WatchLoveEv": 0, "Famed": True}
            result["CanFame"] = self.eligible({"CardID": card_id, **result})
            return result
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            raise ProtocolError("fame_catalog_invalid", "Fame withdrawal references are invalid", 503) from error

    def status(self):
        failure = caught(self._ready)
        return {"ready": failure is None, "error": None if failure is None else failure.code,
                "content_version": self.content_version,
                "eligible_cards": len(self.card_ids), "counted_cards": len(self.counted_card_ids),
                "support_cards": len(self.support_card_ids), "deposit_limit": self.deposit_limit,
                "deposit_batch_limit": self.deposit_batch_limit,
                "rank_min": self.rank_min, "resource_sha256": dict(self._hashes),
                "native_tested": False, "native_excluded_storage_stages": [CLASSIC_CHANGE_STAGE],
                "classic_stage_one": "Registers when the card needs no evolution, capped by the unit's own class; "
                                     "the native deposit and withdrawal lists never offer it",
                "withdrawal_policy": "Maximum native preview growth, zero remainders, first barracks, fresh identity"}

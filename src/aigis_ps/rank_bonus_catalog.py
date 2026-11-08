from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .native_numbers import int32
from .possessions import MAX_ITEM_NAME_BYTES


REWARD_RESOURCE = "RewardConfig.atb"
RANK_RESOURCE = "cfgRnkbns.atb"
REWARD_FIELDS = {name: 1 for name in ("ID_Reward", "ID_Incentive", "Type_Condition", "Param_Condition",
    "Param_ValidCondition", "Param_Valid1", "Param_Valid2", "Type_MainIncentive", "Type_SubIncentive", "Param_Incentive")}
REWARD_FIELDS["ID_Item"] = 32
RANK_FIELDS = {name: 1 for name in ("_ID", "_Rank", "_SysTxtID", "_FlagID")}
LIMITS = ParseLimits(max_input_bytes=16 * 1024 * 1024, max_decompressed_bytes=16 * 1024 * 1024,
                    max_rows=100000)
MAX_BONUSES = 4096


def _text(value, maximum, *, empty=True):
    if type(value) is not str or not empty and not value or len(value.encode("utf-8")) > maximum:
        raise ValueError("Reward text must be a bounded string")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


@dataclass(frozen=True)
class RewardDefinition:
    reward_id: int
    incentive_id: int
    condition_type: int
    condition_parameter: int
    valid_condition: int
    valid1: int
    valid2: int
    reward_main_type: int
    reward_sub_type: int
    parameter: int
    item_id: str
    source_index: int


@dataclass(frozen=True)
class RankBonus:
    bonus_id: int
    rank: int
    display_text_id: int
    tutorial_flag_id: int
    reward_main_type: int
    reward_sub_type: int
    parameter: int
    item_id: str
    incentive_id: int
    source_index: int
    config_index: int
    unsupported_reason: str | None = field(init=False)

    def __post_init__(self):
        if (any(not int32(getattr(self, name)) for name in ("bonus_id", "rank", "display_text_id",
                "tutorial_flag_id", "reward_main_type", "reward_sub_type", "parameter", "incentive_id",
                "source_index", "config_index")) or self.bonus_id <= 0 or self.rank <= 0
                or self.display_text_id <= 0 or self.incentive_id <= 0
                or not 0 <= self.tutorial_flag_id < 32 or self.source_index < 0 or self.config_index < 0):
            raise ValueError("Rank bonus fields require valid native integers")
        _text(self.item_id, MAX_ITEM_NAME_BYTES)
        reason = None
        if self.reward_main_type != 5:
            reason = "unverified_main_type"
        elif self.reward_sub_type not in (0, 1, 16, 20, 22):
            reason = "unverified_sub_type"
        elif self.reward_sub_type == 22:
            if not self.item_id:
                reason = "missing_item_identity"
        elif self.item_id:
            reason = "unexpected_item_identity"
        elif self.parameter <= 0:
            reason = "nonpositive_reward_parameter"
        object.__setattr__(self, "unsupported_reason", reason)

    def to_present_reward(self, description: str) -> dict:
        if self.unsupported_reason is not None:
            raise ProtocolError("rank_bonus_reward_unsupported", "Rank reward shape is not verified", 501)
        try:
            _text(description, 4096)
        except (ValueError, TypeError, UnicodeError, ProtocolError) as error:
            raise ProtocolError("rank_bonus_description_invalid", "Rank reward description is invalid") from error
        if self.reward_sub_type == 0:
            reward = {"kind": "free_crystal", "amount": self.parameter}
        elif self.reward_sub_type == 1:
            reward = {"kind": "unit", "template_id": self.parameter, "count": 1}
        elif self.reward_sub_type == 16:
            reward = {"kind": "summon_ticket", "pool_id": self.parameter, "count": 1}
        elif self.reward_sub_type == 20:
            reward = {"kind": "selection_ticket", "config_id": self.parameter, "count": 1}
        else:
            reward = {"kind": "item", "item_id": self.item_id, "amount": max(1, self.parameter)}
        return {**reward, "description": description}


class RankBonusCatalog:
    def __init__(self, asset_reader):
        try:
            if not callable(asset_reader):
                raise ValueError("Rank rewards require a resource reader")
            hashes = {}

            def table(name, schema):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Rank resources require bounded immutable bytes")
                data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                parsed = parse_table(data, LIMITS)
                fields = {entry["name"]: entry["type_code"] for entry in parsed["fields"]}
                if len(fields) != len(parsed["fields"]) or fields != schema:
                    raise ValueError("Rank resource fields differ from the native contract")
                for row in parsed["rows"]:
                    if any(not int32(row[name]) for name, kind in schema.items() if kind == 1):
                        raise ValueError("Rank fields must contain native integers")
                hashes[name] = hashlib.sha256(raw).hexdigest()
                return parsed["rows"]

            grouped, current, seen = {}, None, set()
            for index, row in enumerate(table(REWARD_RESOURCE, REWARD_FIELDS)):
                identifier, incentive = row["ID_Reward"], row["ID_Incentive"]
                if identifier < 0 or incentive <= 0:
                    raise ValueError("Reward identities must be positive")
                if identifier:
                    if identifier in grouped:
                        raise ValueError("Reward group identities must be unique")
                    current, seen = identifier, set()
                    grouped[current] = []
                if current is None or incentive in seen:
                    raise ValueError("Reward continuations require an unambiguous group and incentive")
                seen.add(incentive)
                grouped[current].append(RewardDefinition(current, incentive, row["Type_Condition"],
                    row["Param_Condition"], row["Param_ValidCondition"], row["Param_Valid1"], row["Param_Valid2"],
                    row["Type_MainIncentive"], row["Type_SubIncentive"], row["Param_Incentive"],
                    _text(row["ID_Item"], MAX_ITEM_NAME_BYTES), index))
            if not grouped:
                raise ValueError("Reward definitions must be nonempty")
            bonuses, identifiers = [], set()
            for index, row in enumerate(table(RANK_RESOURCE, RANK_FIELDS)):
                identifier, rank = row["_ID"], row["_Rank"]
                if (identifier <= 0 or rank <= 0 or row["_SysTxtID"] <= 0 or not 0 <= row["_FlagID"] < 32
                        or identifier in identifiers or len(bonuses) >= MAX_BONUSES):
                    raise ValueError("Rank display identity or metadata is invalid")
                group = grouped.get(identifier, ())
                if len(group) != 1:
                    raise ValueError("Rank display must reference exactly one reward")
                reward = group[0]
                if (reward.condition_type != 10 or reward.condition_parameter != rank
                        or any((reward.valid_condition, reward.valid1, reward.valid2))):
                    raise ValueError("Rank display and reward condition disagree")
                identifiers.add(identifier)
                bonuses.append(RankBonus(identifier, rank, row["_SysTxtID"], row["_FlagID"],
                    reward.reward_main_type, reward.reward_sub_type, reward.parameter, reward.item_id,
                    reward.incentive_id, reward.source_index, index))
            if not bonuses:
                raise ValueError("Rank display definitions must be nonempty")
            self._groups = MappingProxyType({key: tuple(rows) for key, rows in grouped.items()})
            self._bonuses = tuple(sorted(bonuses, key=lambda bonus: (bonus.rank, bonus.config_index)))
            self._by_id = MappingProxyType({bonus.bonus_id: bonus for bonus in self._bonuses})
            self._hashes = MappingProxyType(hashes)
            self._content_version = hashlib.sha256(json.dumps({"policy": 1, "resources": hashes,
                "eligibility": "cfgRnkbns_join_condition10_crossing", "order": "rank_then_display_source",
                "tutorial_flag": "display_only", "item_count": "max_one_parameter",
                "subtypes": [0, 1, 16, 20, 22]}, sort_keys=True).encode()).hexdigest()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("rank_bonus_catalog_invalid", "Rank reward resources differ from the verified contract", 503) from error

    @property
    def groups(self):
        return self._groups

    @property
    def bonuses(self):
        return self._bonuses

    @property
    def by_id(self):
        return self._by_id

    @property
    def content_version(self):
        return self._content_version

    def between(self, old_rank: int, new_rank: int) -> tuple[RankBonus, ...]:
        if not int32(old_rank) or not int32(new_rank) or not 0 <= old_rank <= new_rank:
            raise ProtocolError("rank_bonus_range_invalid", "Rank rewards require an ordered nonnegative rank interval")
        return tuple(bonus for bonus in self._bonuses if old_rank < bonus.rank <= new_rank)

    def status(self):
        excluded = sorted(identifier for identifier, rows in self._groups.items()
                          if identifier not in self._by_id and any(row.condition_type == 10 for row in rows))
        return {"ready": True, "error": None, "bonuses": len(self._bonuses), "reward_groups": len(self._groups),
                "reward_rows": sum(len(rows) for rows in self._groups.values()),
                "condition_counts": dict(Counter(row.condition_type for rows in self._groups.values() for row in rows)),
                "excluded_rank_reward_ids": excluded,
                "unsupported_bonus_ids": [bonus.bonus_id for bonus in self._bonuses if bonus.unsupported_reason],
                "resource_hashes": dict(self._hashes), "content_version": self.content_version,
                "scope": "Current rank display definitions; activity triggers are not enabled",
                "tutorial_flag_policy": "Display metadata, not claim history"}

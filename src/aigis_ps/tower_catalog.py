from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
import hashlib
import json
import math
import re
from types import MappingProxyType

from .alxml import Column, Container
from .errors import ProtocolError
from .mission_tags import mission_tables
from .native_numbers import INT32_MAX, INT32_MIN, float32
from .player_state import PRINCE_CARD_IDS
from .quest_catalog import integer
from .quest_modes import MODE_SPECS, OPEN, MissionWindow, quest_cleared
from .quest_protocol import AVATAR_INDEX_OFFSETS


SCHEMA_VERSION = 1
TOWER_TYPE = 14
# OnCommandMissionTypeTower opens the tower portal only once IsCleardQuest(1004) holds.
GATE_QUEST = 1004
CONFIG_TABLE, FLOOR_TABLE = mission_tables(MODE_SPECS[TOWER_TYPE].mission_category)
# 4641E0 loads the shared TowerScoreCalcConfig, not the per-tower copies.
SCORING_TABLE = "TowerScoreCalcConfig.atb"
SCORE_REWARD_TABLE = "TowerScoreRewardConfig{:06d}.atb"
STAR_REWARD_TABLE = "TowerTotalStarRewardConfig{:06d}.atb"
GENERATION_TABLE = "TowerCrystalGenerationConfig{:06d}.atb"
ITEM_TABLE = "ItemConfigTable.atb"
TEMPLATE_TABLE = "PresentUnitData.atb"
# 901240 keys generation rows by Score, so the -1 first row covers every TotalScore from zero.
FIRST_GENERATION_SCORE = -1
# Tower crystals are the EffectType 6 currencies that the tower exchange charges.
CRYSTAL_EFFECT = 6
# RewardType 1 names an item by GeneralItemID and 2 a unit by CardID; 9590D0 labels 3 systxt_0031, the divine crystal.
ITEM_REWARD, UNIT_REWARD, CRYSTAL_REWARD = 1, 2, 3
SCORE_SOURCE, STAR_SOURCE = "score", "star"
# SystemText 4600 names floor tiers 階層スコア報酬; 4601 mails total-star tiers without a label of their own.
DESCRIPTIONS = MappingProxyType({SCORE_SOURCE: "階層スコア報酬", STAR_SOURCE: "塔合計★数報酬"})
UNKNOWN_REWARD = "tower_reward_type_unknown"
MISSING_TEMPLATE = "tower_reward_template_unknown"
STAR_POINTS = ("StarCountPoint1", "StarCountPoint2", "StarCountPoint3")
COST_TERM, MEMBER_TERM, SALLY_TERM = "ConsumedCost", "MemberCount", "SallyCount"
INTEGER_RESULTS = ("ConsumeCost", "StarScore", "Score", "TotalScore")
FLOAT_RESULTS = ("MemberCount", "ConsumeCostScore", "MemberCountScore", "SallyCount", "SallyCountScore")
FLOAT32_DIGITS = 9
SUPPORT_TEXT = re.compile(r"(?:[0-9]{1,9}(?:\.[0-9]{0,9})?|\.[0-9]{1,9})\Z")
SCORING_POLICY = ("floor(StarCountPoint x cost x members x sallies) on the shortest decimal of each float32 factor; "
                  "a term is 1 when Factor1 or Factor2 is not positive; members count BL formation rows below UX+1000 "
                  "plus AL rows at the support factor; sallies count non-token records with guests at the support factor")
REWARD_POLICY = ("Score groups follow 95B3C0 continuation rows; units use the lowest PresentUnitData template of the card; "
                 "RewardType 3 mails Count free divine crystals; delivery is judged when a tier is reached, never at "
                 "construction")
# Claim receipts saved while RewardType 3 tiers waited unpaid name the catalog under this policy.
LEGACY_REWARD_POLICY = ("Score groups follow 95B3C0 continuation rows; units use the lowest PresentUnitData template of "
                        "the card; RewardType 3 stays unknown; delivery is judged when a tier is reached, never at "
                        "construction")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def require_clock(moment) -> datetime:
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ProtocolError("tower_clock_invalid", "Tower clocks require an aware datetime", 503)
    return moment


def _score_invalid(message: str) -> ProtocolError:
    return ProtocolError("tower_score_invalid", message, 400)


def _text(value) -> str:
    if type(value) is not str or not value:
        raise ValueError("Tower resources require nonempty identifiers")
    return value


def _decimal(value) -> Fraction:
    # Float32 cells carry authored decimals; scoring on the shortest decimal keeps 2.5 - 0.1 x 5 at exactly 2.
    if type(value) is not float:
        raise ValueError("Tower score factors must be float32 cells")
    for digits in range(1, FLOAT32_DIGITS + 1):
        text = f"{value:.{digits}g}"
        if float32(float(text)) == value:
            return Fraction(text)
    raise ValueError("Tower score factors must hold float32 values")


def _support(value) -> Fraction:
    if type(value) is not str or not SUPPORT_TEXT.fullmatch(value):
        raise ValueError("Tower support factors must be unsigned decimal text")
    return Fraction(value)


def _window(window: MissionWindow) -> dict:
    return {"enable": window.enable, "start": None if window.start is None else window.start.isoformat(),
            "end": None if window.end is None else window.end.isoformat()}


@dataclass(frozen=True)
class ScoreTerm:
    factor: Fraction
    slope: Fraction
    minimum: Fraction
    maximum: Fraction

    @classmethod
    def from_row(cls, row: Mapping, prefix: str) -> ScoreTerm:
        term = cls(_decimal(row[f"{prefix}Factor1"]), _decimal(row[f"{prefix}Factor2"]),
                   _decimal(row[f"Min{prefix}Point"]), _decimal(row[f"Max{prefix}Point"]))
        if term.minimum < 0 or term.maximum < 0 or 0 < term.maximum < term.minimum:
            raise ValueError("Tower score term bounds must be nonnegative and ordered")
        return term

    @property
    def enabled(self) -> bool:
        return self.factor > 0 and self.slope > 0

    def point(self, amount: Fraction) -> Fraction:
        if not self.enabled:
            return Fraction(1)
        value = max(self.minimum, self.factor - self.slope * amount)
        return min(value, self.maximum) if self.maximum > 0 else value


@dataclass(frozen=True)
class FloorScoring:
    quest_id: int
    star_points: tuple[int, ...]
    cost: ScoreTerm
    member: ScoreTerm
    sally: ScoreTerm
    member_support: Fraction
    sally_support: Fraction
    minimum_score: int
    maximum_score: int

    @classmethod
    def from_row(cls, row: Mapping) -> FloorScoring:
        scoring = cls(integer(row["QuestID"], minimum=1), tuple(integer(row[name]) for name in STAR_POINTS),
                      ScoreTerm.from_row(row, COST_TERM), ScoreTerm.from_row(row, MEMBER_TERM),
                      ScoreTerm.from_row(row, SALLY_TERM), _support(row[f"{MEMBER_TERM}FactorSupport"]),
                      _support(row[f"{SALLY_TERM}FactorSupport"]), integer(row["MinScore"]), integer(row["MaxScore"]))
        if 0 < scoring.maximum_score < scoring.minimum_score:
            raise ValueError("Tower score bounds must be ordered")
        return scoring

    def bounded(self, score: int) -> int:
        if self.minimum_score > 0:
            score = max(score, self.minimum_score)
        return min(score, self.maximum_score) if self.maximum_score > 0 else score

    @property
    def ceiling(self) -> int:
        # Every term falls as its count grows, so a zero count gives the highest score a floor can record.
        zero = Fraction(0)
        return self.bounded(math.floor(max(self.star_points) * self.cost.point(zero) * self.member.point(zero)
                                       * self.sally.point(zero)))


@dataclass(frozen=True)
class TowerScore:
    consume_cost: int
    star_score: int
    score: int
    member_count: float
    consume_cost_score: float
    member_count_score: float
    sally_count: float
    sally_count_score: float

    def result(self, total_score: int) -> Container:
        # A68FE0 reads TowerResult by column name: four integer columns and five float columns.
        if type(total_score) is not int or not 0 <= total_score <= INT32_MAX:
            raise ProtocolError("tower_score_invalid", "Tower totals must be native nonnegative integers", 503)
        integers = (self.consume_cost, self.star_score, self.score, total_score)
        floats = (self.member_count, self.consume_cost_score, self.member_count_score, self.sally_count,
                  self.sally_count_score)
        return Container("TowerResult", tuple(Column(name, "I", (value,)) for name, value in zip(INTEGER_RESULTS, integers))
                         + tuple(Column(name, "F", (value,)) for name, value in zip(FLOAT_RESULTS, floats)))


@dataclass(frozen=True)
class GenerationTier:
    score: int
    count: int
    limit: int


@dataclass(frozen=True)
class TowerReward:
    source: str
    key: tuple[int, ...]
    threshold: int
    reward_type: int
    count: int
    item_id: str | None = None
    card_id: int | None = None
    template_id: int | None = None
    unsupported: str | None = None

    def present(self) -> dict:
        if self.unsupported is not None:
            raise ProtocolError(self.unsupported, "Tower reward has no supported present form", 501)
        description = DESCRIPTIONS[self.source]
        if self.reward_type == ITEM_REWARD:
            return {"kind": "item", "item_id": self.item_id, "amount": self.count, "description": description}
        if self.reward_type == CRYSTAL_REWARD:
            return {"kind": "free_crystal", "amount": self.count, "description": description}
        return {"kind": "unit", "template_id": self.template_id, "count": self.count, "description": description}

    def entry(self, reason: str) -> dict:
        return {"source": self.source, "key": list(self.key), "reason": reason}


@dataclass(frozen=True)
class TowerDefinition:
    mission_id: int
    crystal_item_id: str
    consume_item_id: str
    daily_add: int
    top_floor: int
    disclose_quest: int
    floors: tuple[int, ...]
    score_rewards: Mapping[int, tuple[TowerReward, ...]]
    star_rewards: tuple[TowerReward, ...]
    generation: tuple[GenerationTier, ...]

    def _tier_index(self, total: int) -> int:
        if type(total) is not int or total < 0:
            raise ProtocolError("tower_score_invalid", "Tower totals must be native nonnegative integers", 503)
        return bisect_right([tier.score for tier in self.generation], total) - 1

    def generation_tier(self, total: int) -> GenerationTier:
        return self.generation[self._tier_index(total)]

    def stack_bound(self, total: int) -> int:
        # A lowered total keeps any stock that a higher tier had already allowed.
        return max(tier.limit for tier in self.generation[self._tier_index(total):])

    def total_stars(self, records: Mapping) -> int:
        # 8CD430 sums MaxRank over the tower's own floors.
        return sum((records.get(floor) or {}).get("MaxRank", 0) for floor in self.floors)

    def reward(self, source: str, key: tuple[int, ...]) -> TowerReward | None:
        # Score groups of floors outside the tower stay in its table but are never tiers a record can hold.
        if source == SCORE_SOURCE and len(key) == 2 and key[0] in self.floors:
            tiers = self.score_rewards.get(key[0], ())
        elif source == STAR_SOURCE and len(key) == 1:
            tiers = self.star_rewards
        else:
            return None
        return next((tier for tier in tiers if tier.key == key), None)


def _templates(rows: Sequence[Mapping]) -> Mapping[int, int]:
    lowest, seen = {}, set()
    for row in rows:
        present_id, card_id = integer(row["PresentID"]), integer(row["CardID"])
        if present_id in seen:
            raise ValueError("Present unit templates must be unique")
        seen.add(present_id)
        if present_id:
            lowest[card_id] = min(present_id, lowest.get(card_id, present_id))
    return MappingProxyType(lowest)


def _reward(source: str, key: tuple[int, ...], threshold: int, row: Mapping, templates: Mapping[int, int]) -> TowerReward:
    kind, count, card_id, item_id = integer(row["RewardType"]), integer(row["Count"]), integer(row["CardID"]), row["GeneralItemID"]
    if type(item_id) is not str:
        raise ValueError("Tower reward items must be text cells")
    if kind == ITEM_REWARD:
        if not item_id or count <= 0:
            raise ValueError("Tower item rewards require an item and a positive count")
        return TowerReward(source, key, threshold, kind, count, item_id=item_id)
    if kind == UNIT_REWARD:
        if card_id <= 0 or count <= 0:
            raise ValueError("Tower unit rewards require a card and a positive count")
        template = templates.get(card_id)
        return TowerReward(source, key, threshold, kind, count, card_id=card_id, template_id=template,
                           unsupported=None if template is not None else MISSING_TEMPLATE)
    if kind == CRYSTAL_REWARD:
        if count <= 0 or card_id or item_id:
            raise ValueError("Tower crystal rewards require a positive count without a card or an item")
        return TowerReward(source, key, threshold, kind, count)
    # 9590D0 names only RewardTypes 1 to 3, so any other type grants nothing known.
    return TowerReward(source, key, threshold, kind, count, unsupported=UNKNOWN_REWARD)


def _score_rewards(rows: Sequence[Mapping], templates: Mapping[int, int]) -> Mapping[int, tuple[TowerReward, ...]]:
    groups: dict[int, list[TowerReward]] = {}
    floor = None
    for row in rows:
        integer(row["ID"])
        head = integer(row["QuestID"])
        # 95B3C0 opens a group at each nonzero QuestID and files the following QuestID 0 rows under it.
        if head:
            if head in groups:
                raise ValueError("Tower score groups must not reopen a floor")
            floor, groups[head] = head, []
        elif floor is None:
            raise ValueError("Tower score continuation rows require a preceding floor")
        tiers = groups[floor]
        reward_id, required = integer(row["RewardID"]), integer(row["RequiredScore"])
        if any(tier.key[1] == reward_id for tier in tiers) or tiers and required <= tiers[-1].threshold:
            raise ValueError("Tower score tiers require distinct rewards and increasing scores")
        tiers.append(_reward(SCORE_SOURCE, (floor, reward_id), required, row, templates))
    return MappingProxyType({key: tuple(value) for key, value in groups.items()})


def _star_rewards(rows: Sequence[Mapping], templates: Mapping[int, int]) -> tuple[TowerReward, ...]:
    tiers: list[TowerReward] = []
    for row in rows:
        required = integer(row["RequiredStarCount"], minimum=1)
        if tiers and required <= tiers[-1].threshold:
            raise ValueError("Tower star tiers require increasing star counts")
        tiers.append(_reward(STAR_SOURCE, (required,), required, row, templates))
    return tuple(tiers)


def _generation(rows: Sequence[Mapping]) -> tuple[GenerationTier, ...]:
    tiers = tuple(GenerationTier(integer(row["Score"], minimum=INT32_MIN), integer(row["GenerationCount"]),
                                 integer(row["PossessionLimit"])) for row in rows)
    if not tiers or tiers[0].score != FIRST_GENERATION_SCORE or any(
            lower.score >= upper.score for lower, upper in zip(tiers, tiers[1:])):
        raise ValueError("Tower generation rows require strictly increasing scores from -1")
    return tiers


class TowerCatalog:
    def __init__(self, quests, possessions):
        self.quests, self.possessions = quests, possessions
        try:
            if (not possessions.is_ready()
                    or quests.resource_sha256(ITEM_TABLE) != possessions.asset_sha256):
                raise ValueError("Tower items require the same verified definitions as possessions")
            self.windows = quests.mission_windows(TOWER_TYPE)
            effects = {row["ItemID"]: integer(row["EffectType"]) for row in quests.resource_rows(ITEM_TABLE)}
            templates = _templates(quests.resource_rows(TEMPLATE_TABLE))
            scoring: dict[int, FloorScoring] = {}
            for row in quests.resource_rows(SCORING_TABLE):
                floor = FloorScoring.from_row(row)
                if floor.quest_id in scoring:
                    raise ValueError("Tower floors require one scoring row each")
                scoring[floor.quest_id] = floor
            towers: dict[int, TowerDefinition] = {}
            owners: dict[int, TowerDefinition] = {}
            for row in quests.resource_rows(CONFIG_TABLE):
                tower = self._tower(row, effects, templates, scoring)
                if tower.mission_id in towers or owners.keys() & set(tower.floors):
                    raise ValueError("Each tower and each floor must be configured once")
                towers[tower.mission_id] = tower
                owners.update(dict.fromkeys(tower.floors, tower))
            if not towers:
                raise ValueError("Tower resources configure no tower")
            self.towers = tuple(towers[key] for key in sorted(towers))
            self.by_mission = MappingProxyType({tower.mission_id: tower for tower in self.towers})
            self.by_floor = MappingProxyType(dict(sorted(owners.items())))
            self._scoring = MappingProxyType({floor: scoring[floor] for floor in self.by_floor})
            names = (CONFIG_TABLE, FLOOR_TABLE, SCORING_TABLE, ITEM_TABLE, TEMPLATE_TABLE) + tuple(
                pattern.format(tower.mission_id) for tower in self.towers
                for pattern in (SCORE_REWARD_TABLE, STAR_REWARD_TABLE, GENERATION_TABLE))
            self.source_hashes = MappingProxyType({name: quests.resource_sha256(name) for name in names})
            content = {
                "schema": SCHEMA_VERSION, "resource_version": quests.resource_version,
                "master": quests.snapshot.identifier, "sources": dict(self.source_hashes),
                "windows": {str(tower.mission_id): _window(self.windows.mission(tower.mission_id)) for tower in self.towers},
                "scoring": SCORING_POLICY, "gate_quest": GATE_QUEST}
            self.content_version = _digest({**content, "rewards": REWARD_POLICY})
            self.legacy_content_version = _digest({**content, "rewards": LEGACY_REWARD_POLICY})
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError, ProtocolError) as error:
            raise ProtocolError("tower_catalog_invalid", "Tower resources differ from the verified contract", 503) from error

    def _tower(self, row: Mapping, effects: Mapping[str, int], templates: Mapping[int, int],
               scoring: Mapping[int, FloorScoring]) -> TowerDefinition:
        mission_id = integer(row["MissionID"], minimum=1)
        crystal, consume = _text(row["TowerCrystalID"]), _text(row["ConsumeItemID"])
        if (effects.get(crystal) != CRYSTAL_EFFECT or self.possessions.stack(crystal) <= 0
                or self.possessions.stack(consume) <= 0):
            raise ValueError("Tower crystals and challenge items require positive stacks")
        disclose = integer(row["DiscloseQuestID"])
        if disclose and self.quests.quest_type(disclose) is None:
            raise ValueError("Tower disclosure must name a known quest")
        floors = self.windows.members(mission_id)
        if not floors or any(self.quests.quest_type(floor) != TOWER_TYPE or floor not in scoring for floor in floors):
            raise ValueError("Tower floors must be scored Type 14 quests")
        if sum(scoring[floor].ceiling for floor in floors) > INT32_MAX:
            raise ValueError("Tower totals must fit the native TotalScore")
        return TowerDefinition(mission_id, crystal, consume, integer(row["DailyAutoAddCount"]), integer(row["TopFloor"]),
                               disclose, floors,
                               _score_rewards(self.quests.resource_rows(SCORE_REWARD_TABLE.format(mission_id)), templates),
                               _star_rewards(self.quests.resource_rows(STAR_REWARD_TABLE.format(mission_id)), templates),
                               _generation(self.quests.resource_rows(GENERATION_TABLE.format(mission_id))))

    def tower(self, mission_id: int) -> TowerDefinition:
        tower = self.by_mission.get(mission_id) if type(mission_id) is int else None
        if tower is None:
            raise ProtocolError("tower_request_invalid", "Tower mission is not configured", 400)
        return tower

    def floor_tower(self, quest_id: int) -> TowerDefinition:
        tower = self.by_floor.get(quest_id) if type(quest_id) is int else None
        if tower is None:
            raise ProtocolError("tower_request_invalid", "Quest is not a configured tower floor", 400)
        return tower

    def scoring(self, quest_id: int) -> FloorScoring:
        self.floor_tower(quest_id)
        return self._scoring[quest_id]

    @staticmethod
    def disclosed(tower: TowerDefinition, records: Mapping) -> bool:
        # 7F0020 opens a tower once its DiscloseQuestID is zero or cleared.
        return not tower.disclose_quest or quest_cleared(records, tower.disclose_quest)

    def accessible(self, tower: TowerDefinition, records: Mapping, now: datetime) -> bool:
        window = self.windows.mission(tower.mission_id)
        return (window.state(require_clock(now)) == OPEN and window.listed and quest_cleared(records, GATE_QUEST)
                and self.disclosed(tower, records))

    def score_floor(self, quest_id: int, stars: int, *, sallies, members, visitors, ux: int, rules) -> TowerScore:
        scoring = self.scoring(quest_id)
        if type(stars) is not int or not 1 <= stars <= len(scoring.star_points):
            raise _score_invalid("Tower results require one to three stars")
        if type(ux) is not int or not 0 <= ux <= INT32_MAX:
            raise _score_invalid("Tower results require the native unit index ceiling")
        if type(getattr(rules, "member_count_prince", None)) is not bool:
            raise ProtocolError("tower_rules_invalid", "Tower scoring requires verified tower rules", 503)
        try:
            sorties = tuple((integer(sally.sally_cost), sally.token_unit, sally.guest_unit) for sally in sallies)
            formation = tuple((integer(member.card_id, minimum=INT32_MIN), integer(member.get_index, minimum=INT32_MIN),
                               integer(member.slot, minimum=INT32_MIN)) for member in members)
            supporters = {integer(visitor.get_index, minimum=INT32_MIN) for visitor in visitors}
        except (AttributeError, TypeError, ValueError) as error:
            raise _score_invalid("Tower results require native formation and sally records") from error
        if any(type(token) is not bool or type(guest) is not bool for _, token, guest in sorties):
            raise _score_invalid("Tower sally unit flags must be booleans")
        consume_cost = sum(cost for cost, _, _ in sorties)
        if consume_cost > INT32_MAX:
            raise _score_invalid("Tower sally costs exceed the native range")
        avatars = ux + AVATAR_INDEX_OFFSETS.start
        counted = {get_index for card_id, get_index, slot in formation if slot >= 0 and get_index < avatars
                   and (rules.member_count_prince or card_id not in PRINCE_CARD_IDS)}
        # SystemText 6273 counts every support unit as the floor's support factor in both members and sallies.
        member_count = len(counted) + scoring.member_support * len(supporters)
        sally_count = sum((scoring.sally_support if guest else Fraction(1) for _, token, guest in sorties if not token),
                          Fraction(0))
        cost_point = scoring.cost.point(Fraction(consume_cost))
        member_point = scoring.member.point(member_count)
        sally_point = scoring.sally.point(sally_count)
        star_score = scoring.star_points[stars - 1]
        score = scoring.bounded(math.floor(star_score * cost_point * member_point * sally_point))
        return TowerScore(consume_cost, star_score, score, float(member_count), float(cost_point), float(member_point),
                          float(sally_count), float(sally_point))

    def _summary(self, tower: TowerDefinition) -> dict:
        tiers = [tier for group in tower.score_rewards.values() for tier in group] + list(tower.star_rewards)
        return {"mission_id": tower.mission_id, "floors": len(tower.floors), "first_floor": tower.floors[0],
                "last_floor": tower.floors[-1], "top_floor": tower.top_floor, "crystal_item_id": tower.crystal_item_id,
                "consume_item_id": tower.consume_item_id, "daily_add": tower.daily_add,
                "disclose_quest": tower.disclose_quest, "score_groups": len(tower.score_rewards),
                "score_tiers": sum(len(group) for group in tower.score_rewards.values()),
                "star_tiers": len(tower.star_rewards), "generation_tiers": len(tower.generation),
                "unsupported_tiers": dict(Counter(tier.unsupported for tier in tiers if tier.unsupported)),
                "window": _window(self.windows.mission(tower.mission_id))}

    def status(self) -> dict:
        return {"ready": True, "gate_quest": GATE_QUEST, "towers": [self._summary(tower) for tower in self.towers],
                "scoring_policy": SCORING_POLICY, "reward_policy": REWARD_POLICY,
                "source_hashes": dict(self.source_hashes), "content_version": self.content_version,
                "native_tested": False}

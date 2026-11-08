from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
import math
from types import MappingProxyType

from .alxml import Column, Container, ValueType
from .energy_clock import anchor_seconds, energy_seconds, set_anchor, validate_clock
from .errors import ProtocolError
from .monthly_capacity import read_capacity, settle_capacity
from .native_numbers import INT32_MAX, float32, int32
from .player_state import PRINCE_CARD_IDS
from .wallet_money import GOLD_LIMIT, money_after_capped_credit


SECONDS_PER_DAY = 86400
MAX_RANK_ROWS = 10000
GAME_PARAM_FIELDS = (
    "Money", "Experience", "Rank", "NowCharisma", "NowActionPoint",
    "CharismaChangeDate", "CharismaChangeTime", "ActionPointChangeDate", "ActionPointChangeTime",
)
TIME_FIELDS = frozenset(("CharismaChangeTime", "ActionPointChangeTime"))
EXPERIENCE_BOOST = 1.2
PERCENT = 100
GOLD_POLICY = "public_star_ability_money_up_rates"
UNRATED_STARS = 0
# Public sources: two-star clears pay 1.2 times their gold and three-star clears 1.5 times; score results judge no stars.
STAR_GOLD_RATES = MappingProxyType({UNRATED_STARS: Fraction(1), 1: Fraction(1), 2: Fraction(6, 5), 3: Fraction(3, 2)})
MONEY_UP_RATE = Fraction(3, 2)
REWARD_GOLD, GOLD_GET_PLUS = 78, 88
# The gold abilities act from the unit itself (78) or on every sortie member (88), never through a condition.
GOLD_SCOPES = MappingProxyType({REWARD_GOLD: (1, 1), GOLD_GET_PLUS: (1, 2)})
GOLD_CONDITION_FIELDS = ("_Command", "_ActivateCommand", "ExtendProperty", "NoChangeCondition")
GOLD_PARAMETERS = tuple(f"_Param{index}" for index in range(1, 5))
ABILITY_VARIANTS = ("Ability_Default", "Ability")
AWAKENED_STAGE = 2
# OnCommand 89659152 switches the Ability on early only for this rarity, from a level the server does not track.
ABILITY_AWAKENING_RARITY = 2
DEFAULT_PRINCE = 1
# 4C96C0 reports the awakened stage for every prince but card 1, and 4D5110 then reads that card's own Ability.
FIXED_STAGE_PRINCES = PRINCE_CARD_IDS - {DEFAULT_PRINCE}


def _nonnegative_int32(value: object) -> bool:
    return int32(value) and value >= 0


def _row(state: dict) -> dict:
    if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
        raise ProtocolError("quest_economy_state", "GameParam storage schema is missing or unsupported", 503)
    tables = state.get("tables")
    rows = tables.get("game_param") if isinstance(tables, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ProtocolError("quest_economy_state", "Expected one persisted GameParam row", 503)
    row = rows[0]
    for name in GAME_PARAM_FIELDS:
        value = row.get(name)
        if name in TIME_FIELDS:
            if (type(value) not in (int, float) or not 0 <= value < 1
                    or not math.isfinite(value) or not 0 <= float32(value) < 1):
                raise ProtocolError("quest_economy_state", "GameParam times must fit float32 fractions of a day", 503)
        elif not _nonnegative_int32(value):
            raise ProtocolError("quest_economy_state", "GameParam quantities and dates must be nonnegative int32 values", 503)
    if row["Rank"] == 0 or row["Money"] == row["Experience"] == 0:
        raise ProtocolError("quest_economy_state", "GameParam cannot satisfy the native player-state contract", 503)
    if not _nonnegative_int32(row.get("ExtendedMaxCharisma", 0)):
        raise ProtocolError("quest_economy_state", "Extended charisma capacity must be a nonnegative int32 value", 503)
    validate_clock(state)
    return row


def _ranks(rank_rows, row: dict) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if not isinstance(rank_rows, (list, tuple)) or not 1 <= len(rank_rows) <= MAX_RANK_ROWS:
        raise ProtocolError("quest_rank_data", "Expected bounded native rank data", 503)
    thresholds, capacities = [], []
    threshold = 0
    for rank in rank_rows:
        if (not isinstance(rank, dict) or not _nonnegative_int32(rank.get("NextExp")) or rank["NextExp"] == 0
                or not _nonnegative_int32(rank.get("CharismaMax"))):
            raise ProtocolError("quest_rank_data", "Rank data requires positive NextExp and nonnegative CharismaMax", 503)
        threshold += rank["NextExp"]
        thresholds.append(threshold)
        capacities.append(rank["CharismaMax"])
    if (row["Experience"] >= thresholds[-1]
            or row["Rank"] != bisect_right(thresholds, row["Experience"]) + 1):
        raise ProtocolError("quest_economy_state", "Player rank and experience disagree with native rank data", 503)
    return tuple(thresholds), tuple(capacities)


def _capacity(row: dict, capacities: tuple[int, ...], rank: int) -> int:
    result = capacities[rank - 1] + row.get("ExtendedMaxCharisma", 0)
    if result > INT32_MAX:
        raise ProtocolError("quest_economy_state", "Charisma capacity exceeds the native int32 field", 503)
    return result


def _now_seconds(now: datetime) -> int:
    try:
        return energy_seconds(now)
    except (OSError, OverflowError, ValueError) as error:
        raise ProtocolError("quest_economy_argument", "Quest time cannot be represented by the native clock") from error


def _anchor(row: dict) -> int:
    return anchor_seconds(row)


def _set_anchor(row: dict, seconds: int) -> None:
    set_anchor(row, seconds)


def _amount(value: int, name: str) -> None:
    if not _nonnegative_int32(value):
        raise ProtocolError("quest_economy_argument", f"{name} must be a nonnegative int32 value")


def _boosted(amount: int, boost: float) -> int:
    return min(int(float32(float32(amount) * float32(boost))), INT32_MAX)


def boosted_experience(experience: int) -> int:
    _amount(experience, "Experience reward")
    # 45A7D0 previews an unexpired ExperienceUpCloseDateTime bonus as (int)((float)RankExp * 1.2f).
    return _boosted(experience, EXPERIENCE_BOOST)


@dataclass(frozen=True)
class GoldPercents:
    reward: int = 0
    event: int = 0

    def strongest(self, other: GoldPercents) -> GoldPercents:
        # Gold abilities of one kind never stack, so the strongest one of each kind applies.
        return GoldPercents(max(self.reward, other.reward), max(self.event, other.event))


NO_GOLD_ABILITY = GoldPercents()


def quest_gold(gold: int, *, gold_percent: int, events: int, gold_per_event: int, stars: int,
               percents: GoldPercents, money_up: bool) -> int:
    for value, name in ((gold, "Quest gold"), (gold_percent, "Gold percent"), (events, "Gold event count"),
                        (gold_per_event, "Gold per event"), (percents.reward, "Reward gold percent"),
                        (percents.event, "Gold event percent")):
        _amount(value, name)
    if type(stars) is not int or stars not in STAR_GOLD_RATES or type(money_up) is not bool:
        raise ProtocolError("quest_economy_argument", "Gold rates require a native star count and a MoneyUp flag")
    per_event = Fraction(gold_per_event * (PERCENT + percents.event), PERCENT)
    total = ((Fraction(gold * gold_percent, PERCENT) + events * per_event) * STAR_GOLD_RATES[stars]
             * Fraction(PERCENT + percents.reward, PERCENT) * (MONEY_UP_RATE if money_up else 1))
    # The client only displays the credited difference, so the rates multiply exactly and floor once.
    return min(math.floor(total), INT32_MAX)


def _gold_index(value: object) -> int:
    if not _nonnegative_int32(value):
        raise ValueError("Gold ability tables need nonnegative native indexes")
    return value


def _gold_effects(configs: Iterable[Mapping]) -> dict[int, GoldPercents | None]:
    found: dict[int, dict[int, int | None]] = {}
    config = invocation = target = influence = 0
    for row in configs:
        config = _gold_index(row["_ConfigID"]) or config
        invocation = _gold_index(row["_InvokeType"]) or invocation
        target = _gold_index(row["_TargetType"]) or target
        influence = _gold_index(row["_InfluenceType"]) or influence
        if not config:
            raise ValueError("Ability continuation precedes a complete group")
        if influence not in GOLD_SCOPES:
            continue
        parameters = tuple(row[name] for name in GOLD_PARAMETERS)
        conditions = tuple(row[name] for name in GOLD_CONDITION_FIELDS)
        if not all(int32(value) for value in parameters) or not all(isinstance(text, str) for text in conditions):
            raise ValueError("Gold ability rows need native parameters and condition texts")
        effects = found.setdefault(config, {})
        supported = (influence not in effects and (invocation, target) == GOLD_SCOPES[influence]
                     and parameters[0] >= 0 and not any(parameters[1:]) and not any(conditions))
        effects[influence] = parameters[0] if supported else None
    return {config: None if None in effects.values() else GoldPercents(effects.get(REWARD_GOLD, 0),
                                                                       effects.get(GOLD_GET_PLUS, 0))
            for config, effects in found.items()}


class GoldAbilities:
    def __init__(self, cards: Iterable[Mapping], abilities: Sequence[Mapping], configs: Iterable[Mapping]):
        try:
            effects = _gold_effects(configs)
            config_ids = tuple(_gold_index(row["_ConfigID"]) for row in abilities)
            variants, rarities = {}, {}
            for card in cards:
                card_id = _gold_index(card["CardID"])
                if card_id in variants:
                    raise ValueError("Gold abilities need distinct card identifiers")
                rarities[card_id] = _gold_index(card["Rare"])
                # 4D5270 reads configuration 0 for an AbilityList row past the table.
                variants[card_id] = tuple(
                    effects.get(config_ids[index] if index < len(config_ids) else 0, NO_GOLD_ABILITY)
                    for index in map(_gold_index, (card[field] for field in ABILITY_VARIANTS)))
        except (KeyError, TypeError, ValueError) as error:
            raise ProtocolError("quest_gold_abilities_invalid",
                                "Gold abilities require verified card and ability tables", 503) from error
        self._variants = MappingProxyType(variants)
        self._rarities = MappingProxyType(rarities)

    def unit(self, unit: Mapping, image_card_id: int) -> GoldPercents:
        card_id = unit["CardID"]
        if card_id not in self._variants or image_card_id not in self._variants:
            raise ProtocolError("quest_gold_abilities_invalid", "Gold abilities do not list this unit's card", 503)
        default, awakened = self._variants[image_card_id]
        if card_id in FIXED_STAGE_PRINCES:
            selected = self._variants[card_id][1]
        else:
            selected = default if unit["ClassChangeCount"] < AWAKENED_STAGE else awakened
        if selected is None or (self._rarities[card_id] == ABILITY_AWAKENING_RARITY and selected != awakened):
            raise ProtocolError("quest_gold_ability_unsupported", "This unit's gold ability needs a verified rule", 501)
        return selected

    def team(self, units: Iterable[Mapping], image_card: Callable[[int], int]) -> GoldPercents:
        percents = NO_GOLD_ABILITY
        for unit in units:
            percents = percents.strongest(self.unit(unit, image_card(unit["CardID"])))
        return percents

    def status(self) -> dict:
        gold = [card_id for card_id, variants in self._variants.items() if variants != (NO_GOLD_ABILITY,) * 2]
        return {"cards": len(gold),
                "unsupported_cards": sorted(card_id for card_id in gold if None in self._variants[card_id]),
                "unmodeled_awakening_cards": sorted(
                    card_id for card_id in gold if self._rarities[card_id] == ABILITY_AWAKENING_RARITY
                    and self._variants[card_id][0] != self._variants[card_id][1])}


def _settle_energy(state: dict, rank_rows, now: datetime, *, recovery_seconds: int = 180) -> dict:
    row = _row(state)
    _, capacities = _ranks(rank_rows, row)
    capacity = _capacity(row, capacities, row["Rank"])
    if not _nonnegative_int32(recovery_seconds) or recovery_seconds == 0:
        raise ProtocolError("quest_economy_argument", "Recovery interval must be a positive int32 number of seconds")
    current, anchor = _now_seconds(now), _anchor(row)
    updated = deepcopy(state)
    target = updated["tables"]["game_param"][0]
    if current < anchor:
        return updated
    if row["NowCharisma"] >= capacity:
        _set_anchor(target, current)
        return updated
    recovered = (current - anchor) // recovery_seconds
    if recovered:
        target["NowCharisma"] = min(capacity, row["NowCharisma"] + recovered)
        _set_anchor(target, current if target["NowCharisma"] == capacity else anchor + recovered * recovery_seconds)
    return updated


def settle_energy(state: dict, rank_rows, now: datetime, *, recovery_seconds: int = 180) -> dict:
    if not isinstance(state, dict) or "monthly_capacity" not in state:
        return _settle_energy(state, rank_rows, now, recovery_seconds=recovery_seconds)
    return settle_capacity(state, now, lambda candidate, at: _settle_energy(
        candidate, rank_rows, at, recovery_seconds=recovery_seconds))


def require_quest_energy(state: dict, rank_rows, now: datetime, *, charisma: int, action_points: int,
                         recovery_seconds: int = 180) -> dict:
    _amount(charisma, "Charisma cost")
    _amount(action_points, "Action-point cost")
    updated = settle_energy(state, rank_rows, now, recovery_seconds=recovery_seconds)
    row = updated["tables"]["game_param"][0]
    if row["NowCharisma"] < charisma or row["NowActionPoint"] < action_points:
        raise ProtocolError("quest_energy_insufficient", "Quest costs exceed the available energy", 409)
    return updated


def debit_quest(state: dict, rank_rows, now: datetime, *, charisma: int, action_points: int,
                recovery_seconds: int = 180) -> dict:
    updated = require_quest_energy(state, rank_rows, now, charisma=charisma, action_points=action_points,
                                   recovery_seconds=recovery_seconds)
    row = updated["tables"]["game_param"][0]
    row["NowCharisma"] -= charisma
    row["NowActionPoint"] -= action_points
    return updated


def charge_quest(state: dict, rank_rows, now: datetime, *, charisma: int, action_points: int,
                 recovery_seconds: int = 180) -> tuple[dict, dict[str, int]]:
    _amount(charisma, "Charisma cost")
    _amount(action_points, "Action-point cost")
    updated = settle_energy(state, rank_rows, now, recovery_seconds=recovery_seconds)
    row = updated["tables"]["game_param"][0]
    # A clear settles even when the balance was spent below its frozen cost, so the charge stops at zero.
    charged = {"charisma": min(row["NowCharisma"], charisma), "action_points": min(row["NowActionPoint"], action_points)}
    row["NowCharisma"] -= charged["charisma"]
    row["NowActionPoint"] -= charged["action_points"]
    return updated, charged


def credit_quest(state: dict, rank_rows, now: datetime, *, experience: int, money: int,
                 refill_on_rank_up: bool = True, recovery_seconds: int = 180) -> dict:
    _amount(experience, "Experience reward")
    _amount(money, "Money reward")
    if type(refill_on_rank_up) is not bool:
        raise ProtocolError("quest_economy_argument", "Rank-up refill policy must be boolean")
    if isinstance(state, dict) and "monthly_capacity" in state:
        state = settle_energy(state, rank_rows, now, recovery_seconds=recovery_seconds)
    row = _row(state)
    thresholds, capacities = _ranks(rank_rows, row)
    _capacity(row, capacities, row["Rank"])
    current = _now_seconds(now)
    balance = money_after_capped_credit(state, money, cap=GOLD_LIMIT)
    new_experience = min(row["Experience"] + experience, thresholds[-1] - 1, INT32_MAX)
    rank = bisect_right(thresholds, new_experience) + 1
    capacity = _capacity(row, capacities, rank)
    updated = deepcopy(state)
    target = updated["tables"]["game_param"][0]
    target.update(Money=balance, Experience=new_experience, Rank=rank)
    if rank > row["Rank"] and refill_on_rank_up:
        target["NowCharisma"] = max(row["NowCharisma"], capacity)
        _set_anchor(target, max(current, _anchor(row)))
    return updated


def game_param_response(state: dict, *, event_param: bool = False) -> Container:
    row = _row(state)
    values = tuple(format(float32(row[name]), ".9g") if name in TIME_FIELDS else str(row[name]) for name in GAME_PARAM_FIELDS)
    keys = tuple(f"A{index}" for index in range(1, 10))
    if event_param:
        if not _nonnegative_int32(row.get("EventParam")):
            raise ProtocolError("quest_economy_state", "GameParam EventParam must be a nonnegative int32 value", 503)
        keys += ("AF",)
        values += (str(row["EventParam"]),)
    if "monthly_capacity" in state:
        read_capacity(state)
        keys += ("ExtendedMaxCharisma", "ExtendedMaxCharismaNextMonth")
        values += tuple(str(row[key]) for key in keys[-2:])
    return Container("PGP", (
        Column("A1", ValueType.STRING, keys),
        Column("A2", ValueType.STRING, values),
    ))

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
from operator import attrgetter
from pathlib import Path
import re
from types import MappingProxyType

from .bootstrap import MAX_QUEST_RECORDS
from .business_calendar import civil_day
from .errors import ProtocolError
from .local_io import deserialize, read_bounded
from .native_clock import NATIVE_EPOCH
from .native_numbers import INT32_MAX, INT32_MIN, int32
from .player_state import PRINCE_CARD_IDS, UNIT_FIELDS
from .request_ledger import RequestLedger
from .storage import MAX_REVISION
from .tower_catalog import SCORE_SOURCE, STAR_SOURCE, TowerCatalog, TowerDefinition, TowerReward, require_clock


DOMAIN = "tower_state"
SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSION = 1
TEAM_SCHEMA_VERSION = 2
MAX_RULE_BYTES = 16 * 1024
HOURS_PER_DAY = 24
HIGH_SCORE_POLICIES = frozenset(("self", "omit"))
DOMAIN_FIELDS = frozenset(("schema_version", "consume", "towers", "receipts", "charge"))
CHARGE_FIELDS = frozenset(("quest_id",))
# 7FF4C0 starts a floor only while ConsumeItemID count > 0; notice 9370 (2022-06-16) charges it on the settling clear.
CHALLENGE_COST = 1
LEGACY_TOWER_FIELDS = frozenset(("floors", "total", "stack", "generation_day", "score_rewards", "star_rewards",
                                 "deferred"))
TOWER_FIELDS = LEGACY_TOWER_FIELDS | {"teams"}
# A66E60 reads HighScoreTeam by column name from A1 through B0 and then B1 when present, never B2 to B5.
TEAM_FIELDS = UNIT_FIELDS[:UNIT_FIELDS.index(("SkillEvolved", "B1")) + 1]
TEAM_NAMES = frozenset(name for name, _ in TEAM_FIELDS)
# 7F5CD0 shows at most 15 HighScoreTeam rows beside the prince cell that A54330 adds itself.
MAX_TEAM_ROWS = 15
CONSUME_FIELDS = frozenset(("day",))
DEFERRED_FIELDS = frozenset(("source", "key", "reason"))
SOURCES = frozenset((SCORE_SOURCE, STAR_SOURCE))
RECEIPTS = "receipts"
RECEIPT_FIELDS = ("request", "content", "revision", "response")
RECEIPT_RESPONSE_LIMIT = 64 * 1024
MAX_REASON_BYTES = 128
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")
LEDGER = RequestLedger(RECEIPTS, "tower_state_invalid", "Tower claim receipts are inconsistent",
                       fields=RECEIPT_FIELDS, response_limit=RECEIPT_RESPONSE_LIMIT)


@dataclass(frozen=True)
class TowerRules:
    # Official aigis1000.jp notices 6956 and 10938: crystals and challenge refills both land at 04:00 JST.
    generation_hour: int = 4
    replenish_hour: int = 4
    first_access_settles_day: bool = True
    high_score: str = "self"
    # The public 182500-point sample and wikiwiki agree the prince pays his cost but never counts as a member.
    member_count_prince: bool = False

    def __post_init__(self):
        if any(type(value) is not int or not 0 <= value < HOURS_PER_DAY
               for value in (self.generation_hour, self.replenish_hour)):
            raise ValueError("Tower day boundaries must be whole JST hours")
        if type(self.first_access_settles_day) is not bool or type(self.member_count_prince) is not bool:
            raise ValueError("Tower rule switches must be booleans")
        if type(self.high_score) is not str or self.high_score not in HIGH_SCORE_POLICIES:
            raise ValueError("Tower high score policy must be self or omit")

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def content_version(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_tower_rules(path) -> TowerRules:
    if path is None:
        return TowerRules()
    values = deserialize(read_bounded(Path(path), MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(values, dict) or set(values) - TowerRules.__dataclass_fields__.keys():
        raise ValueError("Tower rules contain unknown fields")
    return TowerRules(**values)


def day_key(moment: datetime, hour: int) -> int:
    moment = require_clock(moment)
    try:
        # A tower day starts at the given JST hour, so shifting back by that hour lands on the civil JST day.
        return civil_day(moment - timedelta(hours=hour))
    except (OverflowError, ProtocolError) as error:
        raise ProtocolError("tower_clock_invalid", "Tower clocks must fall within the client calendar", 503) from error


def _boundary(day: int, hour: int) -> str:
    return (NATIVE_EPOCH + timedelta(days=day + 1, hours=hour)).isoformat()


def _invalid(message: str):
    raise ProtocolError("tower_state_invalid", message, 503)


def _identifier(key) -> int | None:
    return int(key) if type(key) is str and _ID.fullmatch(key) and int(key) <= INT32_MAX else None


def _native(value, minimum: int = 0) -> bool:
    return int32(value) and value >= minimum


def _reason(value) -> bool:
    try:
        return type(value) is str and 0 < len(value.encode("utf-8")) <= MAX_REASON_BYTES
    except UnicodeError:
        return False


def _rules(rules) -> TowerRules:
    if not isinstance(rules, TowerRules):
        raise ProtocolError("tower_rules_invalid", "Tower settlement requires verified tower rules", 503)
    return rules


def _record_map(records) -> Mapping:
    if not isinstance(records, Mapping):
        raise ProtocolError("tower_records_invalid", "Tower settlement requires quest records keyed by quest", 503)
    return records


def _deliverable(deliverable) -> Callable[[dict], str | None]:
    if not callable(deliverable):
        raise ProtocolError("tower_deliverable_invalid", "Tower rewards require a live delivery check", 503)
    return deliverable


def _empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "consume": {}, "towers": {}, "charge": None, RECEIPTS: LEDGER.read({}, 0)}


def _new_tower(generation_day: int) -> dict:
    return {"floors": {}, "total": 0, "stack": 0, "generation_day": generation_day, "score_rewards": {},
            "star_rewards": [], "deferred": [], "teams": {}}


def _tier_key(source: str, key: Sequence[int]) -> tuple[str, tuple[int, ...]]:
    return source, tuple(key)


def _claimed_keys(tower: TowerDefinition, record: dict) -> set[tuple[str, tuple[int, ...]]]:
    score_rewards, star_rewards = record["score_rewards"], record["star_rewards"]
    if not isinstance(score_rewards, dict) or not isinstance(star_rewards, list):
        _invalid("Claimed tower rewards have an unsupported shape")
    keys = []
    for key, reward_ids in score_rewards.items():
        floor = _identifier(key)
        if floor not in tower.floors or not isinstance(reward_ids, list) or not reward_ids:
            _invalid("Claimed floor rewards must list rewards of the tower's own floors")
        keys.extend(_tier_key(SCORE_SOURCE, (floor, reward_id)) for reward_id in reward_ids)
    keys.extend(_tier_key(STAR_SOURCE, (required,)) for required in star_rewards)
    if (any(any(type(part) is not int for part in key) or tower.reward(source, key) is None for source, key in keys)
            or len(set(keys)) != len(keys)):
        _invalid("Claimed tower rewards must be distinct configured tiers")
    return set(keys)


def _deferred_keys(record: dict) -> set[tuple[str, tuple[int, ...]]]:
    return {_tier_key(entry["source"], entry["key"]) for entry in record["deferred"]}


def _team(rows) -> bool:
    return (isinstance(rows, list) and len(rows) <= MAX_TEAM_ROWS
            and all(isinstance(row, dict) and set(row) == TEAM_NAMES and all(int32(value) for value in row.values())
                    and row["CardID"] > 0 and row["CardID"] not in PRINCE_CARD_IDS and row["UniqueID"] > 0
                    for row in rows)
            and len({row["UniqueID"] for row in rows}) == len(rows))


def _validate_tower(tower: TowerDefinition, record) -> None:
    if not isinstance(record, dict) or set(record) != TOWER_FIELDS:
        _invalid("Tower records have an unsupported shape")
    floors, total, stack = record["floors"], record["total"], record["stack"]
    if not isinstance(floors, dict) or any(_identifier(key) not in tower.floors or not _native(best, 1)
                                           for key, best in floors.items()):
        _invalid("Tower floor bests must be positive scores of the tower's own floors")
    if not _native(total) or total != sum(floors.values()):
        _invalid("Tower totals must equal the sum of their floor bests")
    if not _native(stack) or stack > tower.stack_bound(total):
        _invalid("Tower crystal stock exceeds what its generation tiers allow")
    if not _native(record["generation_day"]):
        _invalid("Tower generation days must be native day numbers")
    claimed, pending = _claimed_keys(tower, record), set()
    if not isinstance(record["deferred"], list):
        _invalid("Deferred tower rewards must be a list")
    for entry in record["deferred"]:
        if (not isinstance(entry, dict) or set(entry) != DEFERRED_FIELDS or type(entry["source"]) is not str
                or entry["source"] not in SOURCES or not isinstance(entry["key"], list)
                or any(type(part) is not int for part in entry["key"]) or not _reason(entry["reason"])):
            _invalid("Deferred tower rewards have an unsupported shape")
        key = _tier_key(entry["source"], entry["key"])
        if key in pending or key in claimed or tower.reward(*key) is None:
            _invalid("Deferred tower rewards must be distinct unclaimed tiers")
        pending.add(key)
    teams = record["teams"]
    if not isinstance(teams, dict) or any(key not in floors or not _team(rows) for key, rows in teams.items()):
        _invalid("High score teams must hold bounded non-prince unit rows of floors with a best")


def _upgrade(domain: dict) -> dict:
    towers = domain["towers"]
    if not isinstance(towers, dict) or any(not isinstance(record, dict) or set(record) != LEGACY_TOWER_FIELDS
                                           for record in towers.values()):
        _invalid("Schema 1 tower records have an unsupported shape")
    # Schema 1 towers predate high score teams, so they read as holding none.
    return {**domain, "schema_version": TEAM_SCHEMA_VERSION,
            "towers": {key: {**record, "teams": {}} for key, record in towers.items()}}


def _upgrade_charge(domain: dict) -> dict:
    # Saves older than settlement charges paid their challenge item at the start, so none is owed.
    return {**domain, "schema_version": SCHEMA_VERSION, "charge": None}


def _validate_charge(charge, catalog: TowerCatalog) -> None:
    if charge is None:
        return
    if (not isinstance(charge, dict) or set(charge) != CHARGE_FIELDS
            or charge["quest_id"] not in catalog.by_floor):
        _invalid("Pending tower charges must name a configured tower floor")


def read_state(state: dict, catalog: TowerCatalog, *, revision: int = MAX_REVISION) -> dict:
    if not isinstance(state, dict):
        _invalid("Tower state requires a saved profile")
    if DOMAIN not in state:
        return _empty()
    domain = state[DOMAIN]
    if not isinstance(domain, dict) or type(domain.get("schema_version")) is not int:
        _invalid("Tower state schema is unsupported")
    version = domain["schema_version"]
    if version in (LEGACY_SCHEMA_VERSION, TEAM_SCHEMA_VERSION):
        if set(domain) != DOMAIN_FIELDS - {"charge"}:
            _invalid("Tower state schema is unsupported")
        if version == LEGACY_SCHEMA_VERSION:
            domain = _upgrade(domain)
        domain = _upgrade_charge(domain)
    elif version != SCHEMA_VERSION or set(domain) != DOMAIN_FIELDS:
        _invalid("Tower state schema is unsupported")
    _validate_charge(domain["charge"], catalog)
    consume, towers = domain["consume"], domain["towers"]
    items = {tower.consume_item_id for tower in catalog.towers}
    if not isinstance(consume, dict) or any(
            item_id not in items or not isinstance(entry, dict) or set(entry) != CONSUME_FIELDS
            or not _native(entry["day"]) for item_id, entry in consume.items()):
        _invalid("Tower challenge refill days are invalid")
    if not isinstance(towers, dict):
        _invalid("Tower records must be keyed by tower")
    for key, record in towers.items():
        tower = catalog.by_mission.get(_identifier(key))
        if tower is None:
            _invalid("Tower state names a tower that is not configured")
        _validate_tower(tower, record)
    return {"schema_version": SCHEMA_VERSION, "consume": deepcopy(consume), "towers": deepcopy(towers),
            "charge": deepcopy(domain["charge"]), RECEIPTS: LEDGER.read(domain, revision)}


def _ordered(mapping: Mapping) -> dict:
    return {key: mapping[key] for key in sorted(mapping, key=lambda key: (len(key), key))}


def _canonical(domain: dict) -> dict:
    towers = {key: {**record, "floors": _ordered(record["floors"]),
                    "score_rewards": {floor: sorted(ids) for floor, ids in _ordered(record["score_rewards"]).items()},
                    "star_rewards": sorted(record["star_rewards"]), "teams": _ordered(record["teams"])}
              for key, record in _ordered(domain["towers"]).items()}
    return {"schema_version": SCHEMA_VERSION, "consume": _ordered(domain["consume"]), "towers": towers,
            "charge": domain["charge"], RECEIPTS: domain[RECEIPTS]}


def _stored(updated: dict, catalog: TowerCatalog, domain: dict) -> dict:
    updated[DOMAIN] = domain
    updated[DOMAIN] = _canonical(read_state(updated, catalog))
    return updated


def write_state(state: dict, catalog: TowerCatalog, domain: dict) -> dict:
    return _stored(deepcopy(state), catalog, domain)


def _settled(domain: dict, tower: TowerDefinition) -> dict:
    record = domain["towers"].get(str(tower.mission_id))
    if record is None:
        _invalid("Tower results and rewards require a settled tower record")
    return record


def settle_clock(state: dict, catalog: TowerCatalog, rules: TowerRules, records: Mapping,
                 now: datetime) -> tuple[dict, tuple[str, ...]]:
    rules, records = _rules(rules), _record_map(records)
    generation_day, replenish_day = day_key(now, rules.generation_hour), day_key(now, rules.replenish_hour)
    domain = read_state(state, catalog)
    before = deepcopy(domain)
    opened = tuple(tower for tower in catalog.towers if catalog.accessible(tower, records, now))
    # A tower's first settlement backdates both day keys, so a new player can challenge on the first day.
    lag = 1 if rules.first_access_settles_day else 0
    refills: dict[str, int] = {}
    for tower in opened:
        record = domain["towers"].setdefault(str(tower.mission_id), _new_tower(generation_day - lag))
        elapsed = generation_day - record["generation_day"]
        if elapsed > 0:
            tier = tower.generation_tier(record["total"])
            if record["stack"] < tier.limit:
                record["stack"] = min(tier.limit, record["stack"] + elapsed * tier.count)
            record["generation_day"] = generation_day
        refills[tower.consume_item_id] = refills.get(tower.consume_item_id, 0) + tower.daily_add
    grants: dict[str, int] = {}
    for item_id, daily in sorted(refills.items()):
        if not daily:
            continue
        entry = domain["consume"].setdefault(item_id, {"day": replenish_day - lag})
        elapsed = replenish_day - entry["day"]
        if elapsed > 0:
            grants[item_id] = min(elapsed * daily, catalog.possessions.stack(item_id))
            entry["day"] = replenish_day
    if domain == before:
        return state, ()
    updated, discarded = catalog.possessions.credit_capped(state, grants) if grants else (deepcopy(state), {})
    changed = tuple(item_id for item_id, amount in grants.items() if discarded.get(item_id, 0) < amount)
    return _stored(updated, catalog, domain), changed


def reserve_charge(state: dict, catalog: TowerCatalog, quest_id: int) -> dict:
    catalog.floor_tower(quest_id)
    domain = read_state(state, catalog)
    domain["charge"] = {"quest_id": quest_id}
    return write_state(state, catalog, domain)


def release_charge(state: dict, catalog: TowerCatalog, quest_id: int) -> tuple[dict, int]:
    catalog.floor_tower(quest_id)
    domain = read_state(state, catalog)
    charge = domain["charge"]
    # A start without a reservation was charged at the start itself, so its settlement owes nothing.
    if charge is None or charge["quest_id"] != quest_id:
        return state, 0
    domain["charge"] = None
    return write_state(state, catalog, domain), CHALLENGE_COST


def team_rows(units: Sequence[Mapping], members) -> list[dict]:
    # Only formation rows (A8 >= 0) matching a saved row are real units; 4CF530 copies one under an avatar GetIndex.
    saved = {(unit["GetIndex"], unit["UniqueID"], unit["CardID"]): unit for unit in units}
    rows: dict[int, dict] = {}
    for member in sorted(members, key=attrgetter("slot")):
        unit = saved.get((member.get_index, member.unique_id, member.card_id))
        # A54330 adds its own prince row, so a saved prince would show again as an ordinary unit.
        if member.slot >= 0 and unit is not None and unit["CardID"] not in PRINCE_CARD_IDS:
            rows.setdefault(unit["UniqueID"], {name: unit[name] for name, _ in TEAM_FIELDS})
    return list(rows.values())[:MAX_TEAM_ROWS]


def record_floor(state: dict, catalog: TowerCatalog, mission: int, quest_id: int, score: int, *,
                 team: Sequence[Mapping] | None = None) -> tuple[dict, int, int, int, int]:
    tower = catalog.floor_tower(quest_id)
    if tower.mission_id != mission:
        raise ProtocolError("tower_request_invalid", "Quest is not a floor of this tower", 400)
    if not _native(score):
        raise ProtocolError("tower_score_invalid", "Tower floor scores must be native nonnegative integers", 503)
    domain = read_state(state, catalog)
    record = _settled(domain, tower)
    key = str(quest_id)
    old_best, old_total = record["floors"].get(key, 0), record["total"]
    if score <= old_best:
        return state, old_best, old_best, old_total, old_total
    record["floors"][key] = score
    record["total"] = old_total - old_best + score
    # The saved team belongs to the run that set the best, so a new best replaces it or, without a team, drops it.
    if team is None:
        record["teams"].pop(key, None)
    else:
        record["teams"][key] = [dict(row) for row in team]
    return write_state(state, catalog, domain), old_best, score, old_total, record["total"]


def _delivery(reward: TowerReward, deliverable) -> str | None:
    if reward.unsupported is not None:
        return reward.unsupported
    reason = deliverable(reward.present())
    if reason is not None and not _reason(reason):
        raise ProtocolError("tower_deliverable_invalid", "Tower delivery checks must return None or an error code", 503)
    return reason


def _crossed(tower: TowerDefinition, record: dict, records: Mapping) -> tuple[TowerReward, ...]:
    handled = _claimed_keys(tower, record) | _deferred_keys(record)
    stars = tower.total_stars(records)
    floors = sorted((int(key), best) for key, best in record["floors"].items())
    reached = [tier for floor, best in floors for tier in tower.score_rewards.get(floor, ()) if tier.threshold <= best]
    reached += [tier for tier in tower.star_rewards if tier.threshold <= stars]
    return tuple(tier for tier in reached if (tier.source, tier.key) not in handled)


def _claim_tier(record: dict, reward: TowerReward) -> None:
    if reward.source == SCORE_SOURCE:
        floor, reward_id = reward.key
        record["score_rewards"].setdefault(str(floor), []).append(reward_id)
    else:
        record["star_rewards"].append(reward.threshold)


def rewards_due(state: dict, catalog: TowerCatalog, mission: int, records: Mapping,
                deliverable) -> tuple[dict, tuple[dict, ...], tuple[dict, ...]]:
    deliverable, records = _deliverable(deliverable), _record_map(records)
    tower = catalog.tower(mission)
    domain = read_state(state, catalog)
    before = deepcopy(domain)
    record = _settled(domain, tower)
    presents = []
    waiting, record["deferred"] = record["deferred"], []
    retried = [tower.reward(entry["source"], tuple(entry["key"])) for entry in waiting]
    for reward in (*retried, *_crossed(tower, {**record, "deferred": waiting}, records)):
        reason = _delivery(reward, deliverable)
        if reason is None:
            presents.append(reward.present())
            _claim_tier(record, reward)
        else:
            record["deferred"].append(reward.entry(reason))
    deferred = tuple(deepcopy(record["deferred"]))
    if domain == before:
        return state, (), deferred
    return write_state(state, catalog, domain), tuple(presents), deferred


def claim(state: dict, catalog: TowerCatalog, missions, held) -> tuple[dict, dict[str, int]]:
    if not isinstance(missions, (list, tuple)) or not 0 < len(missions) <= len(catalog.towers):
        raise ProtocolError("tower_request_invalid", "Tower claims require between one and every tower", 400)
    towers = tuple(catalog.tower(mission) for mission in missions)
    if len({tower.mission_id for tower in towers}) != len(towers):
        raise ProtocolError("tower_request_invalid", "Tower claims must not repeat a tower", 400)
    if not isinstance(held, Mapping) or any(type(item_id) is not str or not _native(count)
                                            for item_id, count in held.items()):
        raise ProtocolError("tower_holdings_invalid", "Tower claims require nonnegative item holdings", 503)
    domain = read_state(state, catalog)
    grants: dict[str, int] = {}
    for tower in towers:
        record = domain["towers"].get(str(tower.mission_id))
        if record is None:
            continue
        item_id = tower.crystal_item_id
        room = max(0, catalog.possessions.stack(item_id) - held.get(item_id, 0) - grants.get(item_id, 0))
        amount = min(record["stack"], room)
        if amount:
            record["stack"] -= amount
            grants[item_id] = grants.get(item_id, 0) + amount
    return (write_state(state, catalog, domain) if grants else state), grants


def saved_records(state: dict) -> Mapping[int, Mapping]:
    tables = state.get("tables") if isinstance(state, dict) else None
    rows = tables.get("quest_records", []) if isinstance(tables, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_QUEST_RECORDS or any(
            not isinstance(row, dict) or not _native(row.get("QuestID"), 1) or not _native(row.get("MaxRank"))
            or not _native(row.get("AchieveFlag"), INT32_MIN) for row in rows):
        raise ProtocolError("tower_records_invalid", "Tower settlement requires saved quest records", 503)
    records = {row["QuestID"]: row for row in rows}
    if len(records) != len(rows):
        raise ProtocolError("tower_records_invalid", "Saved quest records repeat a quest", 503)
    return MappingProxyType(records)


def _tower_view(tower: TowerDefinition, record: dict | None, records: Mapping, accessible: bool,
                generation_day: int, deliverable) -> dict:
    total = 0 if record is None else record["total"]
    tier = tower.generation_tier(total)
    view = {"mission_id": tower.mission_id, "accessible": accessible, "disclosed": TowerCatalog.disclosed(tower, records),
            "started": record is not None, "total_score": total, "total_stars": tower.total_stars(records),
            "generation": {"score": tier.score, "count": tier.count, "limit": tier.limit,
                           "stack_bound": tower.stack_bound(total)}}
    if record is None:
        return view
    deferred = [dict(entry) if deliverable is None else
                {**entry, "current_reason": _delivery(tower.reward(entry["source"], tuple(entry["key"])), deliverable)}
                for entry in record["deferred"]]
    view.update(floors=record["floors"], stack=record["stack"], generation_day=record["generation_day"],
                pending_generation_days=max(0, generation_day - record["generation_day"]),
                score_rewards=record["score_rewards"], star_rewards=record["star_rewards"], deferred=deferred,
                due=[{"source": reward.source, "key": list(reward.key), "threshold": reward.threshold}
                     for reward in _crossed(tower, record, records)],
                teams={floor: [row["CardID"] for row in rows] for floor, rows in record["teams"].items()})
    return view


def describe(state: dict, catalog: TowerCatalog, rules: TowerRules, now: datetime, deliverable=None) -> dict:
    rules = _rules(rules)
    if deliverable is not None:
        _deliverable(deliverable)
    generation_day, replenish_day = day_key(now, rules.generation_hour), day_key(now, rules.replenish_hour)
    domain = read_state(state, catalog)
    records = saved_records(state)
    held = catalog.possessions.counts(state)
    opened = {tower.mission_id for tower in catalog.towers if catalog.accessible(tower, records, now)}
    towers = [_tower_view(tower, domain["towers"].get(str(tower.mission_id)), records, tower.mission_id in opened,
                          generation_day, deliverable) for tower in catalog.towers]
    consume = []
    for item_id in sorted({tower.consume_item_id for tower in catalog.towers}):
        entry = domain["consume"].get(item_id)
        consume.append({"item_id": item_id, "held": held.get(item_id, 0),
                        "stack": catalog.possessions.stack(item_id),
                        "daily_add": sum(tower.daily_add for tower in catalog.towers
                                         if tower.consume_item_id == item_id and tower.mission_id in opened),
                        "day": None if entry is None else entry["day"],
                        "pending_days": None if entry is None else max(0, replenish_day - entry["day"])})
    return {"started": DOMAIN in state, "rules": rules.to_dict(), "rules_version": rules.content_version,
            "content_version": catalog.content_version, "generation_day": generation_day,
            "replenish_day": replenish_day, "next_generation_at": _boundary(generation_day, rules.generation_hour),
            "next_replenish_at": _boundary(replenish_day, rules.replenish_hour), "towers": towers,
            "consume": consume, "charge": domain["charge"], "receipts": len(domain[RECEIPTS]["receipts"])}

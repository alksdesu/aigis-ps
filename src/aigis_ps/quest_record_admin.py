from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict

from .bootstrap import MAX_QUEST_RECORDS, QUEST_RECORD_COUNTERS as COUNTER_FIELDS
from .crystal_wallet import credit_free_crystals, free_crystals_fit
from .errors import ProtocolError
from .native_numbers import UINT32_MASK, int32, signed32
from .public_tables import MASTER_IDS
from .quest_record_service import RECORD_TABLE, UNPLAYED
from .quest_service import FIRST_STARS, STAR_PRESENT_TEXT


QUEST_MASTER = "QuestTable"
QUEST_ID = MASTER_IDS[QUEST_MASTER]
EDITABLE_FIELDS = ("MaxRank", "AchieveFlag", "RemainAssault", "FirstDefeatDate", "Shown")
ARCHIVE_KEY = "quest_record_archive"
ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_FIELDS = frozenset((QUEST_ID, *EDITABLE_FIELDS))
DIFFICULTIES = {False: "normal", True: "hard"}


def _conflict(code: str, message: str) -> ProtocolError:
    return ProtocolError(code, message, 409)


def _invalid_archive() -> ProtocolError:
    return ProtocolError("quest_record_archive_invalid", "The quest record archive is inconsistent", 503)


def quest_records(state: dict, bootstrap) -> dict[int, dict]:
    rows = bootstrap.table_rows(state, RECORD_TABLE)
    records = {row[QUEST_ID]: row for row in rows}
    if len(records) != len(rows):
        raise ProtocolError("quest_state_invalid", "Quest records contain duplicate identifiers", 503)
    return records


def master_quest_ids(bootstrap) -> frozenset[int]:
    _, master = bootstrap.public_master(QUEST_MASTER)
    return frozenset(next(column.values for column in master.children if column.name == QUEST_ID))


def _written(state: dict, bootstrap, records: Mapping[int, dict], **extra) -> dict:
    updated = {**state, "tables": {**state["tables"], RECORD_TABLE: list(records.values())}, **extra}
    bootstrap.table_rows(updated, RECORD_TABLE)
    return updated


def edit_record(state: dict, bootstrap, quest_id: int, changes: Mapping[str, int | bool]) -> tuple[dict, dict | None, dict]:
    if not changes or set(changes) - set(EDITABLE_FIELDS):
        raise ProtocolError("quest_record_edit_invalid", "Record edits change only the five editable record fields")
    if quest_id not in master_quest_ids(bootstrap):
        raise ProtocolError("quest_not_found", "Quest is absent from the fixed master snapshot", 404)
    records = quest_records(state, bootstrap)
    before = records.get(quest_id)
    row = dict(before) if before is not None else {QUEST_ID: quest_id, **UNPLAYED}
    for name, value in changes.items():
        row[name] = signed32(value) if name == "AchieveFlag" else value
    if row == before:
        raise _conflict("quest_record_unchanged", "The quest record already holds these values")
    return _written(state, bootstrap, {**records, quest_id: row}), before, row


def read_archive(state: dict) -> list[dict]:
    saved = state.get(ARCHIVE_KEY)
    if saved is None:
        return []
    if (not isinstance(saved, dict) or set(saved) != {"schema_version", "rows"}
            or saved["schema_version"] != ARCHIVE_SCHEMA_VERSION or not isinstance(saved["rows"], list)
            or len(saved["rows"]) > MAX_QUEST_RECORDS):
        raise _invalid_archive()
    seen = set()
    for row in saved["rows"]:
        if (not isinstance(row, dict) or set(row) != ARCHIVE_FIELDS or type(row["Shown"]) is not bool
                or not int32(row[QUEST_ID]) or row[QUEST_ID] <= 0 or row[QUEST_ID] in seen
                or any(not int32(row[name]) or row[name] < 0 for name in COUNTER_FIELDS)):
            raise _invalid_archive()
        seen.add(row[QUEST_ID])
    return deepcopy(saved["rows"])


def _archive_value(rows: list[dict]) -> dict:
    return {"schema_version": ARCHIVE_SCHEMA_VERSION, "rows": rows}


def archive_missing(state: dict, bootstrap, master_ids: frozenset[int]) -> tuple[dict, list[dict]]:
    if not master_ids or any(not int32(quest_id) or quest_id <= 0 for quest_id in master_ids):
        raise ProtocolError("quest_record_archive_invalid", "Archiving requires the target master quest identifiers", 503)
    records = quest_records(state, bootstrap)
    archived = read_archive(state)
    moved = [row for quest_id, row in records.items() if quest_id not in master_ids]
    if not moved:
        return state, []
    held = {row[QUEST_ID] for row in archived}
    if held & {row[QUEST_ID] for row in moved}:
        raise _conflict("quest_record_archive_conflict", "The archive already holds a record with this quest ID")
    kept = {quest_id: row for quest_id, row in records.items() if quest_id in master_ids}
    return _written(state, bootstrap, kept, **{ARCHIVE_KEY: _archive_value([*archived, *moved])}), moved


def restore_archived(state: dict, bootstrap, quest_ids: Iterable[int],
                     master_ids: frozenset[int]) -> tuple[dict, list[dict], list[int]]:
    requested = list(quest_ids)
    archived = read_archive(state)
    held = {row[QUEST_ID]: row for row in archived}
    missing = [quest_id for quest_id in requested if quest_id not in held]
    if missing:
        raise ProtocolError("quest_record_archive_missing", "The archive holds no record for these quest IDs", 404)
    records = quest_records(state, bootstrap)
    present = [quest_id for quest_id in requested if quest_id in records]
    if present:
        raise _conflict("quest_record_exists", "A saved quest record already exists for these quest IDs")
    restorable = [quest_id for quest_id in requested if quest_id in master_ids]
    skipped = [quest_id for quest_id in requested if quest_id not in master_ids]
    if not restorable:
        return state, [], skipped
    restored = [held[quest_id] for quest_id in restorable]
    remaining = [row for row in archived if row[QUEST_ID] not in set(restorable)]
    extra = {ARCHIVE_KEY: _archive_value(remaining)} if remaining else {}
    updated = _written(state, bootstrap, {**records, **{row[QUEST_ID]: row for row in restored}}, **extra)
    if not remaining:
        updated.pop(ARCHIVE_KEY, None)
    return updated, restored, skipped


def _star_amount(reward: dict, rules) -> int:
    return reward["count"] if reward["count"] > 0 else rules.first_three_star_crystals


def grant_clear_rewards(state: dict, *, quests, missions, rank_rewards, players, possessions, presents,
                        quest_id: int, stars: int, hard: bool, previous_flags: int, current_flags: int,
                        now, acquisition_day: int) -> tuple[dict, dict, tuple[str, ...]]:
    mode = quests.catalog.mode(quest_id)
    if mode.score_mode:
        raise _conflict("quest_record_rewards_unsupported", "Score quests settle their rewards through their score hook")
    difficulty = DIFFICULTIES[hard]
    star_count, star_flag = FIRST_STARS[difficulty]
    first_star = (stars == star_count and not previous_flags & UINT32_MASK & star_flag
                  and bool(current_flags & UINT32_MASK & star_flag))
    rewards = mode.star_rewards_hard if hard else mode.star_rewards_normal
    granted = [asdict(reward) for reward in rewards] if first_star else []
    if any(reward["kind"] == "unsupported" for reward in granted):
        raise ProtocolError("quest_star_reward_unsupported", "This quest's first star reward type is not implemented", 501)
    quest, rules = quests.catalog.get(quest_id), quests.rules
    updated, warnings = deepcopy(state), []
    unit_grants = [{"card_id": reward["card_id"], "level": quest.unit_level, "count": reward["count"]}
                   for reward in granted if reward["kind"] == "unit"]
    updated, new_units = players.credit_units(updated, unit_grants)
    item_grants = {reward["item_id"]: reward["count"] for reward in granted if reward["kind"] == "item"}
    discarded = {}
    if item_grants:
        updated, discarded = possessions.credit_capped(updated, item_grants)
        if discarded:
            warnings.append("Star item rewards beyond their stack were discarded, as a battle result discards them")
    crystals = sum(_star_amount(reward, rules) for reward in granted if reward["kind"] == "crystal")
    wallet_crystals, present_ids = 0, []
    if crystals:
        if free_crystals_fit(updated, crystals):
            updated = credit_free_crystals(updated, crystals, acquisition_day)
            wallet_crystals = crystals
        else:
            reward = {"kind": "free_crystal", "amount": crystals, "description": STAR_PRESENT_TEXT[difficulty]}
            updated, added = presents.enqueue(updated, [reward])
            present_ids = [row["PresentID"] for row in added]
    updated = missions.record(updated, "quest_clear", {
        "quest_id": quest_id, "quest_type": mode.quest_type, "stars": stars, "hard": hard,
        "team": [], "score_mode": False, "kills": 0}, now)
    updated, rank_mail_ids = rank_rewards.apply(state, updated)
    receipt = {"stars": stars, "hard": hard, "first_star": first_star, "star_rewards": granted,
               "units": [{"CardID": unit["CardID"], "UniqueID": unit["UniqueID"]} for unit in new_units],
               "items": dict(item_grants), "discarded": dict(discarded),
               "crystals": wallet_crystals, "crystal_presents": present_ids, "rank_mail_ids": list(rank_mail_ids)}
    return updated, receipt, tuple(warnings)

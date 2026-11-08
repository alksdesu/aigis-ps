from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import re

from . import quest_modes
from .business_calendar import DAY_TEXT, MONTH_TEXT, business_date, month_key
from .energy_clock import JST
from .errors import ProtocolError
from .native_clock import MAX_DAY_ONE_MILLISECONDS, UTC_DAY_ONE, day_one_milliseconds
from .native_numbers import int32
from .player_state import UNIT_BOOLEANS, UNIT_FIELDS
from .possessions import MAX_ITEM_NAME_BYTES, MAX_POSSESSION_ITEMS, STORED_ITEM_NAME_BYTES
from .quest_clear_spec import NEUTRAL_TEAM, QUEST_CLEAR_CONDITION, QuestClearSpecs
from .quest_protocol import MAX_KILLS, MAX_TEAM_ROWS


JOURNAL_KEY = "mission_state"
SKIN_HISTORY_KEY = "skin_acquisition_history"
SUPERSEDED_KEY = "superseded"
REBUILT, DELETED, SUPERSEDED = "rebuilt", "deleted", "superseded"
DAILY_PERIOD = 1
MONTHLY_PERIOD = 2
CYCLIC_PERIODS = frozenset((DAILY_PERIOD, MONTHLY_PERIOD))
SEQUENTIAL_PERIOD = 100
PERMANENT_PERIODS = frozenset((3, 4, 5, SEQUENTIAL_PERIOD))
PERIOD_TYPES = CYCLIC_PERIODS | PERMANENT_PERIODS
SUPPORTED_TYPES = frozenset((1, 2, 3, 4, 5, 7, 8, 9, 12, 13, 14, 15, 17))
MAX_ENTRIES = 65535
MAX_INSTANCES = 65535
DGP_DEFINITION = "1cada7d12c262be33d326d92dec0111161f47b28fc5bfbecccdb007d924a7303"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_JOURNAL_FIELDS = frozenset(("schema_version", "entries", "last_time", "last_login_day"))
_ENTRY_FIELDS = frozenset(("condition_id", "period_type", "period", "definition_version", "goal",
                           "progress", "status", "read", "uids"))
_REPORT_FIELDS = ("period_type", "definition_version", "goal", "progress", "status", "read")
QUEST_CLEAR_FIELDS = frozenset(("quest_id", "quest_type", "stars", "hard"))
QUEST_CLEAR_OPTIONAL = frozenset(("mission_id", "team", "score_mode", "kills"))
TEAM_MEMBER_FIELDS = frozenset(("card_id", "get_index", "unique_id"))


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _invalid(message="Mission progress state is invalid"):
    raise ProtocolError("mission_state_invalid", message, 503)


def _clock(now):
    if not isinstance(now, datetime):
        raise ProtocolError("mission_clock_invalid", "Mission time requires a datetime")
    try:
        if now.tzinfo is not None and now.utcoffset() is None:
            raise ValueError("Mission timezone has no offset")
        local = now.replace(tzinfo=JST) if now.tzinfo is None else now.astimezone(JST)
        return local, day_one_milliseconds(local), business_date(now)
    except (ValueError, OverflowError, ProtocolError) as error:
        raise ProtocolError("mission_clock_invalid", "Mission time is outside the supported calendar") from error


def _valid_day(value):
    if not isinstance(value, str) or not DAY_TEXT.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _valid_period(kind, value):
    if not isinstance(value, str):
        return False
    if kind == DAILY_PERIOD:
        return value.startswith("day:") and _valid_day(value[4:])
    if kind == MONTHLY_PERIOD:
        return value.startswith("month:") and bool(MONTH_TEXT.fullmatch(value[6:])) and _valid_day(value[6:] + "-01")
    return kind in PERMANENT_PERIODS and value == "permanent"


def period_key(period_type, day):
    if period_type == DAILY_PERIOD:
        return "day:" + day.isoformat()
    if period_type == MONTHLY_PERIOD:
        return "month:" + month_key(day)
    return "permanent"


def _valid_entry(entry):
    return (isinstance(entry, dict) and set(entry) == _ENTRY_FIELDS
            and _integer(entry["condition_id"], 1) and type(entry["period_type"]) is int
            and entry["period_type"] in PERIOD_TYPES and _valid_period(entry["period_type"], entry["period"])
            and isinstance(entry["definition_version"], str)
            and _HASH.fullmatch(entry["definition_version"]) is not None
            and _integer(entry["goal"], 1) and _integer(entry["progress"]) and entry["progress"] <= entry["goal"]
            and type(entry["status"]) is int and entry["status"] in (0, 1, 2)
            and type(entry["read"]) is bool and not (entry["status"] == 0 and entry["read"])
            and (entry["status"] == 0) == (entry["progress"] < entry["goal"])
            and isinstance(entry["uids"], list) and len(entry["uids"]) <= min(MAX_INSTANCES, entry["progress"])
            and all(_integer(uid, 1) for uid in entry["uids"]) and entry["uids"] == sorted(set(entry["uids"])))


def _after_watermark(entry, day):
    return entry["period_type"] in CYCLIC_PERIODS and entry["period"] > period_key(entry["period_type"], day)


def _goal(definition):
    field = {10: "in_hallof_fame_sum", 11: "purchased_skin_sum", 16: "in_hallof_fame_support_sum"}.get(
        definition.condition_type, "clear_count_condition")
    value = getattr(definition, field)
    if not _integer(value, 1):
        raise ProtocolError("mission_definition_invalid", "Mission goal must be a positive native integer", 503)
    return value


def _definition_changed(entry, definition, goal):
    return entry["status"] != 2 and (entry["definition_version"] != definition.definition_version
                                     or entry["goal"] != goal)


def _rebuilt(entry, definition, goal):
    progress = min(entry["progress"], goal)
    return {**entry, "definition_version": definition.definition_version, "goal": goal, "progress": progress,
            "status": int(progress == goal), "read": False, "uids": entry["uids"][:progress]}


def _change(action, before, after):
    return {"condition_id": before["condition_id"], "period": before["period"], "action": action,
            **{field: {"before": before[field], "after": after.get(field)} for field in _REPORT_FIELDS}}


def _ids(values):
    if (not isinstance(values, (tuple, list)) or len(values) > MAX_ENTRIES
            or any(not _integer(value, 1) for value in values) or len(set(values)) != len(values)):
        raise ProtocolError("mission_selection_invalid", "Mission selection requires distinct positive IDs")
    return tuple(values)


def _item_ids(values, maximum=MAX_ITEM_NAME_BYTES):
    try:
        valid = (isinstance(values, (tuple, list, frozenset)) and len(values) <= MAX_POSSESSION_ITEMS
            and all(isinstance(value, str) and value and "\0" not in value
                    and len(value.encode("utf-8")) <= maximum for value in values)
            and len(set(values)) == len(values))
    except UnicodeError:
        valid = False
    if not valid:
        raise ProtocolError("mission_skin_items_invalid", "Skin history requires distinct bounded item names", 503)
    return frozenset(values)


def _skin_history(state, baseline=()):
    if SKIN_HISTORY_KEY not in state:
        return {"schema_version": 1, "seen": sorted(_item_ids(baseline))}
    history = state[SKIN_HISTORY_KEY]
    if (not isinstance(history, dict) or set(history) != {"schema_version", "seen"}
            or type(history["schema_version"]) is not int or history["schema_version"] != 1
            or not isinstance(history["seen"], list)):
        _invalid("Skin acquisition history is invalid")
    seen = _item_ids(history["seen"], STORED_ITEM_NAME_BYTES)
    if history["seen"] != sorted(seen):
        _invalid("Skin acquisition history must be canonical")
    return deepcopy(history)


def _team_valid(team):
    if not isinstance(team, list) or len(team) > MAX_TEAM_ROWS:
        return False
    if any(not isinstance(member, dict) or set(member) != TEAM_MEMBER_FIELDS or not _integer(member["card_id"], 1)
           or not _integer(member["get_index"]) or not _integer(member["unique_id"], 1) for member in team):
        return False
    return len({(member["get_index"], member["unique_id"]) for member in team}) == len(team)


def _target_in_text_only(definition):
    # Beginner steps name their quest in fields; one without a QuestID keeps its target in ConditionText only.
    return definition.period_type == SEQUENTIAL_PERIOD and not definition.quest_id


def _fame_keys(values):
    if (not isinstance(values, (list, tuple)) or len(values) > MAX_ENTRIES
            or any(not isinstance(value, (tuple, list)) or len(value) != 2
                   or not _integer(value[0], 1) or not _integer(value[1]) or value[1] > 4 for value in values)):
        raise ProtocolError("mission_fame_event_invalid", "Fame registration requires verified card-stage pairs", 503)
    result = frozenset(tuple(value) for value in values)
    if len(result) != len(values):
        raise ProtocolError("mission_fame_event_invalid", "Fame registration pairs must be distinct", 503)
    return result


class MissionProgress:
    def __init__(self, catalog, *, dgp_login=False, skin_items=(), fame_cards=(), fame_support_cards=(), clear_specs=None):
        self.catalog = catalog
        self._skin_items = _item_ids(skin_items)
        self._fame_cards = frozenset(_ids(list(fame_cards)))
        self._fame_support_cards = frozenset(_ids(list(fame_support_cards)))
        if not self._fame_support_cards <= self._fame_cards:
            raise ValueError("Support fame cards must belong to the verified history set")
        if type(dgp_login) is not bool:
            raise ValueError("DGP login capability must be boolean")
        self._dgp_login = dgp_login
        if not isinstance(catalog.content_version, str) or not _HASH.fullmatch(catalog.content_version):
            raise ValueError("Mission progress requires a verified catalog")
        if clear_specs is not None and (not isinstance(clear_specs, QuestClearSpecs) or clear_specs.ids != {
                row.condition_id for row in catalog.definitions if row.condition_type == QUEST_CLEAR_CONDITION}):
            raise ValueError("Quest clear specs must cover exactly the catalog's quest clear missions")
        self._clear_specs = clear_specs
        self._catalog_version = catalog.content_version
        self.content_version = self.content_version_for(self._catalog_version)
        self.legacy_window_content_version = self.content_version_for(catalog.legacy_window_content_version)

    def content_version_for(self, catalog_version):
        return hashlib.sha256(json.dumps({"catalog": catalog_version, "policy": 1,
            "clear_specs": None if self._clear_specs is None else self._clear_specs.digest,
            "quest_clear": "claimed_specs_then_untyped_specs_then_shared_without_spec_then_stars",
            "periods": "jst_04_daily_monthly", "sequence": "claimed_prefix_no_backfill",
            "aggregate": "all_dependencies_status_at_least_one", "instances": "unique_id",
            "definition_change": "claimed_preserved_unclaimed_rejected", "dgp_login": self._dgp_login,
            "dgp_definition": DGP_DEFINITION, "skin_items": sorted(self._skin_items),
            "skin_policy": "distinct_items_preserve_existing_baseline",
            "fame_cards": sorted(self._fame_cards), "fame_support_cards": sorted(self._fame_support_cards),
            "fame_policy": "new_card_status_stage_registrations_no_backfill"}, sort_keys=True).encode()).hexdigest()

    @property
    def dgp_login(self):
        return self._dgp_login

    @property
    def skin_items(self):
        return self._skin_items

    @property
    def fame_cards(self):
        return self._fame_cards

    @property
    def fame_support_cards(self):
        return self._fame_support_cards

    @property
    def clear_specs(self):
        return self._clear_specs

    def status(self):
        supported = SUPPORTED_TYPES | ({11} if self.skin_items else set()) | ({10, 16} if self.fame_cards else set())
        return {"ready": self.catalog.content_version == self._catalog_version,
                "content_version": self.content_version, "supported_periods": sorted(PERIOD_TYPES),
                "supported_types": sorted(supported), "unsupported_types": sorted({10, 11, 16} - supported),
                "skin_item_count": len(self.skin_items),
                "skin_name_policy": (f"Skin items and acquisition events need names within the {MAX_ITEM_NAME_BYTES}-byte "
                                     f"native cell; saved acquisition history up to {STORED_ITEM_NAME_BYTES} bytes stays readable"),
                "fame_card_count": len(self.fame_cards), "fame_support_count": len(self.fame_support_cards),
                "quest_scope": "The policy of the cleared quest's type answers for specs derived for that type, including "
                               "Genus/Assign formation rules over saved BL units; a spec typed for other quest types never "
                               "counts; an untyped spec applies its own quest, member limit and formation rule; without a "
                               "spec QuestType -2 counts every clear and QuestType 0 counts main quests; a PT100 step "
                               "without a QuestID counts only through a policy; ClearRank and HardClearRank always apply",
                "clear_specs": None if self.clear_specs is None else self.clear_specs.status(),
                "periods": "Daily JST 04:00; monthly day 1 JST 04:00; otherwise permanent",
                "sequence": "Only the first unclaimed PT100 mission receives new events",
                "definition_changes": "Unclaimed rows of a changed definition wait for an explicit rebuild; "
                                      "a period type change deletes the old unclaimed rows and keeps claimed ones "
                                      "as superseded history",
                "dgp_login": self.dgp_login,
                "dgp_policy": "Explicit local capability for the verified PC client and pinned mission definition",
                "maximum_history_entries": MAX_ENTRIES, "maximum_instances_per_entry": MAX_INSTANCES}

    def initialize(self, state):
        if not isinstance(state, dict):
            _invalid()
        result = deepcopy(state)
        _skin_history(result)
        if JOURNAL_KEY not in result:
            result[JOURNAL_KEY] = {"schema_version": 1, "entries": [], "last_time": None, "last_login_day": None}
            return result
        journal = result[JOURNAL_KEY]
        if (not isinstance(journal, dict) or not _JOURNAL_FIELDS <= set(journal) <= _JOURNAL_FIELDS | {SUPERSEDED_KEY}
                or type(journal["schema_version"]) is not int or journal["schema_version"] != 1
                or not isinstance(journal["entries"], list) or len(journal["entries"]) > MAX_ENTRIES):
            _invalid()
        superseded = journal.get(SUPERSEDED_KEY, [])
        if not isinstance(superseded, list) or len(superseded) > MAX_ENTRIES:
            _invalid("Superseded mission history is invalid")
        last = journal["last_time"]
        if last is not None and (type(last) is not int or not 0 <= last <= MAX_DAY_ONE_MILLISECONDS):
            _invalid("Mission time watermark is invalid")
        login = journal["last_login_day"]
        if login is not None and (not _valid_day(login) or last is None):
            _invalid("Mission login watermark is invalid")
        if (journal["entries"] or superseded) and last is None:
            _invalid("Mission entries require a time watermark")
        day = None
        if last is not None:
            try:
                _, _, day = _clock(UTC_DAY_ONE + timedelta(milliseconds=last))
            except (OverflowError, ProtocolError):
                _invalid("Mission time watermark is outside the supported calendar")
            if login is not None and login > day.isoformat():
                _invalid("Mission login day exceeds its time watermark")
        keys, policies = set(), {}
        for entry in journal["entries"]:
            if not _valid_entry(entry):
                _invalid()
            key = entry["condition_id"], entry["period"]
            if key in keys or policies.setdefault(entry["condition_id"], entry["period_type"]) != entry["period_type"]:
                _invalid("Mission history contains duplicate entries or conflicting periods")
            keys.add(key)
            if _after_watermark(entry, day):
                _invalid("Mission entry exceeds its time watermark")
        # A period type that changes back can archive the same condition and period again.
        for entry in superseded:
            if not _valid_entry(entry) or entry["status"] != 2:
                _invalid("Superseded mission history is invalid")
            if _after_watermark(entry, day):
                _invalid("Mission entry exceeds its time watermark")
        return result

    def _prepare(self, state, now):
        if self.catalog.content_version != self._catalog_version:
            raise ProtocolError("mission_catalog_changed", "Mission catalog changed after progress setup", 503)
        result = self.initialize(state)
        local, timestamp, day = _clock(now)
        journal = result[JOURNAL_KEY]
        if journal["last_time"] is not None and timestamp < journal["last_time"]:
            raise ProtocolError("mission_clock_backwards", "Mission time precedes the latest recorded progress", 409)
        active = {row.condition_id: row for row in self.catalog.active(local) if row.period_type in PERIOD_TYPES}
        return result, local, timestamp, day, active

    def _context(self, state, now):
        result, local, timestamp, day, active = self._prepare(state, now)
        entries = result[JOURNAL_KEY]["entries"]
        index = {(row["condition_id"], row["period"]): row for row in entries}
        policies = {row["condition_id"]: row["period_type"] for row in entries}
        for definition in active.values():
            if policies.get(definition.condition_id, definition.period_type) != definition.period_type:
                raise ProtocolError("mission_definition_changed", "Mission period changed while history exists", 503)
            self._entry(index, definition, day)
        return result, local, timestamp, day, active, index

    @staticmethod
    def _entry(index, definition, day):
        key = definition.condition_id, period_key(definition.period_type, day)
        entry = index.get(key)
        goal = _goal(definition)
        if entry is not None:
            if _definition_changed(entry, definition, goal):
                raise ProtocolError("mission_definition_changed", "An unclaimed mission definition changed within its period", 503)
            return entry
        return {"condition_id": definition.condition_id, "period_type": definition.period_type,
                "period": key[1], "definition_version": definition.definition_version, "goal": goal,
                "progress": 0, "status": 0, "read": False, "uids": []}

    def _current(self, index, day):
        for identifier in self.catalog.sequence:
            definition = self.catalog.by_id[identifier]
            if self._entry(index, definition, day)["status"] != 2:
                return identifier
        return None

    @staticmethod
    def _put(index, entry):
        key = entry["condition_id"], entry["period"]
        if key not in index and len(index) >= MAX_ENTRIES:
            raise ProtocolError("mission_history_capacity", "Mission history cannot discard older entries", 503)
        index[key] = entry

    def _aggregate(self, index, active, local, day):
        current = self._current(index, day)
        visiting, visited = set(), set()

        def visit(definition):
            key = definition.condition_id
            if key in visiting:
                raise ProtocolError("mission_dependencies_invalid", "Mission dependencies are cyclic", 503)
            if key in visited:
                return
            visiting.add(key)
            if definition.condition_type in (3, 17) and (definition.period_type != SEQUENTIAL_PERIOD or key == current):
                dependencies = self.catalog.dependencies(definition, local)
                if not dependencies or any(value not in active for value in dependencies):
                    raise ProtocolError("mission_dependencies_invalid", "Mission dependencies are empty or inactive", 503)
                for identifier in dependencies:
                    visit(active[identifier])
                entry = self._entry(index, definition, day)
                if entry["status"] == 0 and all(self._entry(index, active[value], day)["status"] >= 1 for value in dependencies):
                    entry.update(progress=entry["goal"], status=1)
                    self._put(index, entry)
            visiting.remove(key)
            visited.add(key)

        for definition in active.values():
            visit(definition)

    def _finish(self, result, index, timestamp, original, *, changed=False):
        journal = result[JOURNAL_KEY]
        journal["entries"] = [index[key] for key in sorted(index)]
        if changed or journal != original:
            journal["last_time"] = timestamp
        return result

    def snapshot(self, state, now):
        _, local, _, day, active, index = self._context(state, now)
        self._aggregate(index, active, local, day)
        current = self._current(index, day)
        result = []
        for identifier, definition in sorted(active.items()):
            entry = self._entry(index, definition, day)
            locked = definition.period_type == SEQUENTIAL_PERIOD and identifier != current and entry["status"] != 2
            result.append({"ID": identifier, "Status": 0 if locked else entry["status"],
                "Read": 0 if locked else int(entry["read"]),
                "Progress": 0 if locked else min(_goal(definition), entry["progress"])})
        return tuple(result)

    @staticmethod
    def _payload(kind, payload):
        expected = {"login": set(), "training": {"before", "after", "level"}, "gift": {"before", "after"},
                    "evolution": {"before", "after", "operation"},
                    "quest_clear": QUEST_CLEAR_FIELDS,
                    "skin_acquired": {"previous_items", "current_items"},
                    "fame_registered": {"registrations"}}
        optional = {"quest_clear": QUEST_CLEAR_OPTIONAL}
        if not isinstance(kind, str) or kind not in expected:
            raise ProtocolError("mission_event_unsupported", "This mission event is not supported")
        if not isinstance(payload, dict) or not expected[kind] <= set(payload) <= expected[kind] | optional.get(kind, set()):
            raise ProtocolError("mission_event_invalid", "Mission event fields are invalid", 503)
        if kind == "skin_acquired":
            _item_ids(payload["previous_items"])
            _item_ids(payload["current_items"])
        if kind == "fame_registered":
            _fame_keys(payload["registrations"])
        if kind in ("training", "gift", "evolution"):
            for key in ("before", "after"):
                unit = payload[key]
                if (not isinstance(unit, dict) or set(unit) != {name for name, _ in UNIT_FIELDS}
                        or any(type(value) is not bool if name in UNIT_BOOLEANS else not int32(value) for name, value in unit.items())
                        or any(unit[name] <= 0 for name in ("CardID", "ClassID", "UniqueID"))
                        or any(unit[name] < 0 for name in ("GetIndex", "Exp", "Love", "SkillLevel", "ClassChangeCount", "SkillEvolved",
                                                         "SettingSkillID", "SkillExperience", "CostExperience"))):
                    raise ProtocolError("mission_event_invalid", "Mission event unit fields are invalid", 503)
            if any(payload["before"][name] != payload["after"][name] for name in ("CardID", "GetIndex", "UniqueID")):
                raise ProtocolError("mission_event_invalid", "Mission events require the same unit instance", 503)
        if kind == "training" and not _integer(payload["level"], 1):
            raise ProtocolError("mission_event_invalid", "Training events require a derived positive unit level", 503)
        if kind == "evolution" and (type(payload["operation"]) is not int or payload["operation"] not in (7, 8, 9)):
            raise ProtocolError("mission_event_invalid", "Evolution events require operation 7, 8 or 9", 503)
        if kind == "quest_clear":
            score_mode, mission_id, team = payload.get("score_mode", False), payload.get("mission_id"), payload.get("team")
            # Score settlements report a result without stars; every other clear reports at least one.
            if (not _integer(payload["quest_id"], 1) or not _integer(payload["quest_type"])
                    or type(payload["hard"]) is not bool or type(score_mode) is not bool
                    or not _integer(payload["stars"], 0 if score_mode else 1) or payload["stars"] > 4
                    or (mission_id is not None and not _integer(mission_id, 1))
                    or ("kills" in payload and (not _integer(payload["kills"]) or payload["kills"] > MAX_KILLS))
                    or (team is not None and not _team_valid(team))):
                raise ProtocolError("mission_event_invalid", "Quest completion fields are invalid", 503)

    @staticmethod
    def _shared_quest_match(definition, payload):
        if any(getattr(definition, name) != neutral for name, neutral in NEUTRAL_TEAM.items()):
            return False
        if definition.quest_id not in (0, payload["quest_id"]):
            return False
        if definition.quest_type == -2:
            return not _target_in_text_only(definition)
        return definition.quest_type == 0 and payload["quest_type"] == 0

    def _quest_clear_match(self, definition, payload):
        spec = None if self.clear_specs is None else self.clear_specs.get(definition.condition_id)
        if spec is not None and not spec.resolved:
            return False
        matched = quest_modes.policy(payload["quest_type"]).mission_matcher(definition, payload, spec)
        if matched is not None:
            return matched
        if spec is None:
            return self._shared_quest_match(definition, payload)
        if spec.quest_types is not None:
            # A typed spec counts only through the policy that claims its quest types.
            return False
        return not _target_in_text_only(definition) and spec.evaluate(payload)

    @staticmethod
    def _stars_satisfied(definition, payload):
        # The client never reads HardClearRankCondition, but a nonzero value can only mean a four-star result.
        if definition.hard_clear_rank_condition and not (
                payload["hard"] and payload["stars"] >= definition.hard_clear_rank_condition):
            return False
        return payload["stars"] >= definition.clear_rank_condition

    def _matches(self, definition, kind, payload):
        condition = definition.condition_type
        if kind == "login":
            if definition.condition_id == 1031 or definition.condition_text == 5725:
                if (definition.condition_id != 1031 or condition != 1 or definition.period_type != 4
                        or definition.condition_text != 5725 or definition.definition_version != DGP_DEFINITION):
                    raise ProtocolError("mission_definition_changed", "DGP login mission differs from the verified definition", 503)
                return self.dgp_login
            return condition in (1, 9)
        if kind == "quest_clear":
            return (condition == QUEST_CLEAR_CONDITION and self._quest_clear_match(definition, payload)
                    and self._stars_satisfied(definition, payload))
        before, after = payload["before"], payload["after"]
        if kind == "gift":
            return condition == 5 and after["Love"] > before["Love"]
        if kind == "training":
            if condition == 4:
                return True
            border = definition.border_value
            return _integer(border, 1) and ((condition == 12 and payload["level"] >= border)
                or (condition == 13 and after["SkillLevel"] >= border)
                or (condition == 14 and after["CostModMixValue"] <= -border))
        operation = payload["operation"]
        return ((condition == 7 and operation == 7 and before["ClassChangeCount"] < 2 and after["ClassChangeCount"] == 2)
            or (condition == 8 and operation == 9 and before["ClassChangeCount"] == 2 and after["ClassChangeCount"] in (3, 4))
            or (condition == 15 and operation == 8 and before["SkillEvolved"] == 0 and after["SkillEvolved"] == 1))

    def record(self, state, kind, payload, now):
        self._payload(kind, payload)
        result, local, timestamp, day, active, index = self._context(state, now)
        original = deepcopy(result[JOURNAL_KEY])
        current = self._current(index, day)
        increment, skins_changed = 1, False
        fame_total, fame_support = 0, 0
        if kind == "fame_registered":
            keys = _fame_keys(payload["registrations"])
            if any(card_id not in self.fame_cards for card_id, _ in keys):
                raise ProtocolError("mission_fame_event_unknown", "Fame registration uses an unverified card", 503)
            fame_total = len(keys)
            fame_support = sum(card_id in self.fame_support_cards for card_id, _ in keys)
        if kind == "skin_acquired":
            previous, owned = _item_ids(payload["previous_items"]), _item_ids(payload["current_items"])
            if not (previous | owned) <= self.skin_items:
                raise ProtocolError("mission_skin_items_unknown", "Skin acquisition does not match the verified catalog", 503)
            history = _skin_history(result, sorted(previous))
            seen = frozenset(history["seen"]) | previous
            increment = len(owned - seen)
            history["seen"] = sorted(seen | owned)
            _item_ids(history["seen"], STORED_ITEM_NAME_BYTES)
            if history["seen"] or SKIN_HISTORY_KEY in result:
                skins_changed = history != result.get(SKIN_HISTORY_KEY)
                result[SKIN_HISTORY_KEY] = history
        repeated_login = kind == "login" and original["last_login_day"] == day.isoformat()
        if kind == "login":
            result[JOURNAL_KEY]["last_login_day"] = day.isoformat()
        for identifier, definition in active.items():
            if repeated_login or (definition.period_type == SEQUENTIAL_PERIOD and identifier != current):
                continue
            entry = self._entry(index, definition, day)
            step = ({10: fame_total, 16: fame_support}.get(definition.condition_type, 0)
                    if kind == "fame_registered" else increment)
            if entry["status"] or not step:
                continue
            matches = (True if kind == "fame_registered" else definition.condition_type == 11
                       if kind == "skin_acquired" else self._matches(definition, kind, payload))
            if not matches:
                continue
            if definition.condition_type in (5, 7, 8, 12, 13, 14, 15):
                uid = payload["after"]["UniqueID"]
                if uid in entry["uids"]:
                    continue
                if len(entry["uids"]) >= MAX_INSTANCES:
                    raise ProtocolError("mission_instance_capacity", "Mission instance history is full", 503)
                entry["uids"] = sorted([*entry["uids"], uid])
            entry["progress"] = min(entry["goal"], entry["progress"] + step)
            entry["status"] = int(entry["progress"] == entry["goal"])
            self._put(index, entry)
        self._aggregate(index, active, local, day)
        return self._finish(result, index, timestamp, original, changed=skins_changed)

    def claim(self, state, ids, now):
        selected = _ids(ids)
        result, local, timestamp, day, active, index = self._context(state, now)
        original = deepcopy(result[JOURNAL_KEY])
        self._aggregate(index, active, local, day)
        current = self._current(index, day)
        entries = []
        for identifier in selected:
            if identifier not in active:
                raise ProtocolError("mission_inactive", "Mission is not currently active", 409)
            definition = active[identifier]
            if definition.period_type == SEQUENTIAL_PERIOD and identifier != current:
                raise ProtocolError("mission_sequence_locked", "Only the current sequential mission can be claimed", 409)
            entry = self._entry(index, definition, day)
            if entry["status"] != 1:
                raise ProtocolError("mission_not_ready", "Mission is not ready to claim", 409)
            entries.append(entry)
        for entry in entries:
            entry["status"] = 2
            self._put(index, entry)
        return self._finish(result, index, timestamp, original)

    def mark_read(self, state, ids, now):
        selected = _ids(ids)
        result, local, timestamp, day, active, index = self._context(state, now)
        original = deepcopy(result[JOURNAL_KEY])
        self._aggregate(index, active, local, day)
        for identifier in selected:
            if identifier not in active:
                raise ProtocolError("mission_inactive", "Mission is not currently active", 409)
            entry = self._entry(index, active[identifier], day)
            # 6F8740 marks the whole page read locally, claimed missions included; unfinished ones never keep a read flag.
            if entry["status"] and not entry["read"]:
                entry["read"] = True
                self._put(index, entry)
        return self._finish(result, index, timestamp, original)

    def rebuild_changed(self, state, now):
        result, _, timestamp, day, active = self._prepare(state, now)
        journal = result[JOURNAL_KEY]
        original = deepcopy(journal)
        index = {(row["condition_id"], row["period"]): row for row in journal["entries"]}
        histories = {}
        for row in journal["entries"]:
            histories.setdefault(row["condition_id"], []).append(row)
        # The beginner sequence reads its steps' current rows even outside their validity dates.
        evaluated = {**{identifier: self.catalog.by_id[identifier] for identifier in self.catalog.sequence}, **active}
        report, superseded = [], []
        for identifier, definition in sorted(evaluated.items()):
            goal = _goal(definition)
            history = sorted(histories.get(identifier, ()), key=lambda row: row["period"])
            current = index.get((identifier, period_key(definition.period_type, day)))
            changed = current is not None and _definition_changed(current, definition, goal)
            retyped = any(row["period_type"] != definition.period_type for row in history)
            # Only active definitions check period types; an inactive step counts only where its current row blocks.
            if retyped and (identifier in active or changed):
                successor = {"period_type": definition.period_type,
                             "definition_version": definition.definition_version, "goal": goal}
                for row in history:
                    del index[identifier, row["period"]]
                    claimed = row["status"] == 2
                    if claimed:
                        superseded.append(row)
                    report.append(_change(SUPERSEDED if claimed else DELETED, row, successor))
            elif changed:
                rebuilt = _rebuilt(current, definition, goal)
                index[identifier, current["period"]] = rebuilt
                report.append(_change(REBUILT, current, rebuilt))
        if not report:
            return state, []
        if superseded:
            archived = [*journal.get(SUPERSEDED_KEY, ()), *superseded]
            if len(archived) > MAX_ENTRIES:
                raise ProtocolError("mission_history_capacity",
                                    "Superseded mission history cannot discard older entries", 503)
            journal[SUPERSEDED_KEY] = archived
        return self._finish(result, index, timestamp, original), report

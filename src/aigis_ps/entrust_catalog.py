from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import IntEnum, StrEnum, auto
import functools
import hashlib
import json
import threading
from types import MappingProxyType

from . import quest_modes
from .al_format import parse_table
from .alxml import Column, ValueType, loads
from .bootstrap import MAX_QUEST_RECORDS
from .energy_clock import JST
from .errors import ProtocolError
from .growth_campaign import CAMPAIGN_RESOURCE, GrowthCampaign
from .mission_tags import STATE_NAMES, TAG_LOOKUP, MissionTagIndex, mission_tables
from .native_commands import (
    SCOPE_SEPARATOR, CommandAmbiguous, CommandForm, CommandHit, MenuRoute, NativeCommandIndex, form_of,
)
from .native_numbers import int32
from .possessions import STORED_ITEM_NAME_BYTES
from .public_tables import MASTER_LIMITS
from .quest_catalog import QuestCatalog, QuestDefinition, QuestMode, Reward
from .quest_modes import (
    MISSION_CATEGORIES, MODE_SPECS, OPEN, SLOT_FLAGS, MissionSelector, MissionWindow, MissionWindows, ModePolicy,
    require_platform, special_condition_satisfied,
)


FIELDS = {"QuestID": ValueType.INT, "Type": ValueType.INT, "DisableEntrust": ValueType.INT,
          "ConsignmentTicketConsumptionCount": ValueType.INT, "ConsignmentTicketID": ValueType.STRING}
TICKET_FIELDS = ("ConsignmentTicketConsumptionCount", "DisableEntrust", "ConsignmentTicketID")
QUEST_MASTER, CARD_MASTER = "QuestTable", "CardData"
CONDITION_RESOURCE = "SpecialMissionPlayCondition.atb"
DROP_RESOURCE, ITEM_RESOURCE = "ItemDropConfigTable.atb", "ItemConfigTable.atb"
GRANTABLE_REWARDS = frozenset(("unit", "item", "event_param"))
AUDITED_TYPES = quest_modes.ENTRUST_TYPES
AUDITED_CATEGORIES = tuple(sorted({category for quest_type in AUDITED_TYPES
                                   for category in MODE_SPECS[quest_type].categories}))
TYPE_GATE = "HasEntrustByType_{}"
MENU_GATE = "IsEntrust_{}"
# 6791D0 evaluates HasEntrustByType only for nonzero quest types.
UNGATED_TYPES = frozenset((0,))
# Native type gates outside the audited scope are only reported, so a one-byte scan bounds the search.
GATE_SCAN_TYPES = range(256)
# QuestCatalog keys entry digests by map archive and entry member.
ENTRY_LABEL = "{}/Entry{:02d}.atb"
TICKET_NULLS = ("", "null")
MODE_UNSUPPORTED = "quest_mode_unsupported"
# Only deterministic refusals are cached; source failures retry like QuestCatalog.get.
RETRIED_STATUS = 503
CONTENT_SCHEMA = 4
# Entrust receipts digested under the three-hook parity rule stay replayable.
LEGACY_PARITY_HOOKS = ("check_start", "prepare_start", "settle")
STATUS_LIMIT = 32
REQUIRED_MAX_RANK = 3
# Only EntrustCatalog.check seals a readiness check, so a hand-built one cannot skip the test.
_CHECK_SEAL = object()
RULES = MappingProxyType({
    "scope": "audited_types_passing_the_native_type_gate_with_enabled_policy_and_declared_parity",
    "eligibility": "MaxRank_at_least_3_each_nonzero_treasure_bit_and_special_conditions",
    "special_conditions": "battle_start_rules", "offer": "every_candidate_reward_grants_units_items_or_event_points",
    "candidates": "record_raw_MaxRank_selects_prize_candidates_like_a_battle_start",
    "menu": "updater_scope_chain_first_hit_literal_true_or_open_mission_tag",
    "mission_choice": "lowest_ticket_waiving_mission_else_lowest_mission",
    "ticket_null": "empty_or_literal_null", "disable": "any_nonzero",
    "orphan": "quests_without_listing_missions_excluded", "validation": "on_demand", "degrade": "per_scope",
})


@dataclass(frozen=True)
class EntrustExclusion:
    code: str
    message: str
    status: int

    def error(self):
        return ProtocolError(self.code, self.message, self.status)


REWARD_UNSUPPORTED = EntrustExclusion("entrust_reward_unsupported", "Entrust reward has no verified grant path", 501)
NOT_OFFERED = EntrustExclusion("entrust_not_offered", "Entrust does not offer this quest type", 409)
NO_MISSION = EntrustExclusion("entrust_no_mission", "No mission of the quest type lists this quest", 409)
MENU_CLOSED = EntrustExclusion("entrust_menu_closed", "The entrust menu is closed for every playable mission", 409)


def source_invalid(message: str) -> EntrustExclusion:
    return EntrustExclusion("entrust_source_invalid", message, 503)


class ScopeState(StrEnum):
    OFFERED = "offered"
    PENDING = "pending"
    REFUSED = "refused"


class Blocker(StrEnum):
    NOT_REGISTERED = "not_registered"
    MISSION_WINDOW = "mission_window"
    MENU_CLOSED = "entrust_menu_closed"
    DAY_CLOSED = "day_closed"
    MODE_ENTRY_CLOSED = "mode_entry_closed"
    SOURCE_INVALID = "source_invalid"


# Ordered by how far a mission got, so a blocked quest reports the furthest stage any of its missions reached.
class _Reach(IntEnum):
    NOT_REGISTERED = auto()
    MISSION_WINDOW = auto()
    MENU_CLOSED = auto()


@dataclass(frozen=True)
class TypeScope:
    quest_type: int
    audited: bool
    gate: CommandHit | None
    gate_form: CommandForm
    gate_error: CommandAmbiguous | None
    policy: ModePolicy
    settings: Mapping[str, object]
    blocker: str | None
    windows_error: str | None
    state: ScopeState
    exclusion: EntrustExclusion | None

    @property
    def enabled(self) -> bool:
        return self.settings["enabled"]

    @property
    def reason(self) -> str | None:
        return self.settings["reason"]


@dataclass(frozen=True)
class MenuGate:
    category: int
    route: MenuRoute | None
    scope: str | None
    hit: CommandHit | None
    form: CommandForm
    error: CommandAmbiguous | None


@dataclass(frozen=True)
class EntrustListing:
    quest_type: int
    ticket_item_id: str | None
    ticket_count: int
    disabled: bool
    mission_ids: tuple[int, ...]


@dataclass(frozen=True)
class EntrustDefinition:
    quest: QuestDefinition
    mode: QuestMode
    quest_type: int
    ticket_item_id: str | None
    ticket_count: int
    disabled: bool

    @property
    def quest_id(self):
        return self.quest.quest_id

    def reward_codes(self, max_rank: int) -> tuple[int, ...]:
        return _reward_codes(self.quest, self.quest.candidates_for(max_rank))

    @property
    def all_reward_codes(self) -> tuple[int, ...]:
        return _reward_codes(self.quest, self.quest.candidates)

    @property
    def entry_label(self):
        return ENTRY_LABEL.format(self.mode.map_archive, self.quest.entry_no)


@dataclass(frozen=True, eq=False)
class CatalogCheck:
    catalog: EntrustCatalog
    seal: object = field(repr=False)


@dataclass(frozen=True)
class _PreviewCatalog:
    # Mode entry gates read the tag index through the quest catalog, so a preview substitutes its own there.
    quests: QuestCatalog
    tags: MissionTagIndex

    def mission_tags(self) -> MissionTagIndex:
        return self.tags

    def __getattr__(self, name: str):
        return getattr(self.quests, name)


def _reward_codes(quest: QuestDefinition, candidates) -> tuple[int, ...]:
    return tuple(code for candidate in candidates if (code := quest.reward_code(candidate)))


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _clock(now) -> datetime:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ProtocolError("quest_clock_invalid", "Entrust availability requires an aware datetime", 503)
    return now


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(JST).isoformat(timespec="microseconds")


def _attempt(function: Callable, *args) -> object:
    try:
        return function(*args)
    except ProtocolError as error:
        return {"error": error.code}


def _master_rows(raw: bytes) -> dict[int, dict]:
    document = loads(raw, limits=MASTER_LIMITS)
    if any(not isinstance(column, Column) for column in document.children):
        raise ValueError("Entrust quest master requires scalar columns")
    columns = {column.name: column for column in document.children}
    if (len(columns) != len(document.children) or any(name not in columns or columns[name].type is not kind
            for name, kind in FIELDS.items()) or len({len(column.values) for column in columns.values()}) != 1):
        raise ValueError("Entrust fields must be aligned native master columns")
    rows = {}
    for values in zip(*(columns[name].values for name in FIELDS)):
        row = dict(zip(FIELDS, values))
        identifier, kind = row["QuestID"], row["Type"]
        if not int32(identifier) or identifier <= 0 or identifier in rows or not int32(kind) or kind < 0:
            raise ValueError("Entrust master identities or quest types are invalid")
        rows[identifier] = row
    return dict(sorted(rows.items()))


def _grouped(quest_types: Mapping[int, int]) -> Mapping[int, tuple[int, ...]]:
    grouped = {}
    for quest_id, quest_type in quest_types.items():
        grouped.setdefault(quest_type, []).append(quest_id)
    return MappingProxyType({quest_type: tuple(quest_ids) for quest_type, quest_ids in sorted(grouped.items())})


def _not_offered(quest_type: int) -> EntrustExclusion:
    return replace(NOT_OFFERED, message=f"{NOT_OFFERED.message}: {quest_type}")


def _judgement(quest_type: int, form: CommandForm, fault: CommandAmbiguous | None, settings: Mapping[str, object],
               blocker: str | None) -> tuple[ScopeState, EntrustExclusion | None]:
    gated = quest_type not in UNGATED_TYPES
    if quest_type not in AUDITED_TYPES:
        return ScopeState.REFUSED, _not_offered(quest_type)
    if gated and fault is not None:
        return ScopeState.PENDING, source_invalid(f"The native entrust gate of quest type {quest_type} is ambiguous")
    if gated and form is not CommandForm.LITERAL_TRUE:
        return ScopeState.REFUSED, _not_offered(quest_type)
    if not settings["enabled"]:
        return ScopeState.PENDING, EntrustExclusion(MODE_UNSUPPORTED, f"Quest mode is not implemented: {settings['reason']}", 501)
    if blocker is not None:
        return ScopeState.PENDING, EntrustExclusion(MODE_UNSUPPORTED, blocker, 501)
    return ScopeState.OFFERED, None


def _policy_unchanged(scope: TypeScope) -> bool:
    policy = quest_modes.policy(scope.quest_type)
    return policy is scope.policy and policy.status() == scope.settings and policy.entrust_blocker() == scope.blocker


def _selected_types(quest_types) -> tuple[int, ...]:
    if quest_types is None:
        return tuple(sorted(AUDITED_TYPES))
    selected = None if isinstance(quest_types, (str, bytes)) or not isinstance(quest_types, Iterable) else tuple(quest_types)
    if selected is None or any(not int32(quest_type) or quest_type < 0 for quest_type in selected):
        raise ValueError("Entrust validation takes nonnegative native quest types")
    return tuple(sorted(set(selected)))


def _location(hit: CommandHit | None) -> dict:
    if hit is None:
        return {"table": None, "row": None, "id": None, "text": None}
    return {"table": hit.table, "row": hit.row, "id": hit.identifier, "text": hit.text}


def _command_record(hit: CommandHit | None, form: CommandForm, fault: CommandAmbiguous | None) -> dict:
    return {**_location(hit), "form": form, "error": None if fault is None else fault.diagnostic()}


def _route_record(route: MenuRoute | None) -> dict | None:
    return None if route is None else {**_location(route.hit), "func": route.func, "chain": list(route.chain)}


def _gate_record(gate: MenuGate) -> dict:
    return {"route": _route_record(gate.route), "scope": gate.scope,
            "menu_gate": _command_record(gate.hit, gate.form, gate.error)}


def _policy_record(scope: TypeScope) -> dict:
    return {**scope.settings, "entrust_blocker": scope.blocker}


def _exclusion_record(exclusion: EntrustExclusion | None) -> dict | None:
    return None if exclusion is None else {"code": exclusion.code, "status": exclusion.status, "reason": exclusion.message}


def _scope_record(scope: TypeScope) -> dict:
    return {"audited": scope.audited, "state": scope.state, "exclusion": _exclusion_record(scope.exclusion),
            "native_gate": _command_record(scope.gate, scope.gate_form, scope.gate_error),
            "policy": _policy_record(scope), "windows_error": scope.windows_error}


def _listing_record(listing: EntrustListing) -> list:
    return [listing.quest_type, listing.ticket_item_id, listing.ticket_count, listing.disabled, list(listing.mission_ids)]


def _window_record(mission: MissionWindow) -> dict:
    return {"mission_id": mission.mission_id, "category": mission.category, "enable": mission.enable,
            "start": _iso(mission.start), "end": _iso(mission.end), "request_quests": list(mission.request_quests),
            "request_rank": mission.request_rank, "key_quest": mission.key_quest, "tag": mission.tag,
            "tag_span": mission.tag_span}


def _bounded(quest_ids: list[int]) -> dict:
    return {"count": len(quest_ids), "quests": quest_ids[:STATUS_LIMIT]}


def _unreported(progress: dict) -> None:
    return None


def _progress_record(quest_type: int, done: int, total: int) -> dict:
    return {"quest_type": quest_type, "done": done, "total": total}


class EntrustCatalog:
    def __init__(self, quests):
        self.quests = quests
        # Builds take this lock before QuestCatalog's, never the reverse.
        self._lock = threading.RLock()
        self._products: dict[int, EntrustDefinition] = {}
        self._entries: dict[int, tuple[str, str]] = {}
        self._rewards: dict[int, Reward] = {}
        self._reward_stacks: dict[str, int] = {}
        self._failures: dict[int, EntrustExclusion] = {}
        self._transient: Counter[tuple[int, str]] = Counter()
        try:
            if not isinstance(quests, QuestCatalog):
                raise ValueError("Entrust requires the verified ordinary quest catalog")
            self.possessions = quests._possessions
            if not self.possessions.is_ready():
                raise ValueError("Entrust requires verified possession definitions")
            self.resource_version = quests.resource_version
            self._master = quests.snapshot
            self._conditions = quests.special_conditions()
            raw = self._master.get_xml(QUEST_MASTER)
            rows = _master_rows(raw)
            self._quest_types = MappingProxyType({quest_id: row["Type"] for quest_id, row in rows.items()})
            self._main_ids = tuple(quest_id for quest_id, quest_type in self._quest_types.items() if quest_type == 0)
            if (self._main_ids != quests.all_ids
                    or any(quests.quest_type(quest_id) != quest_type for quest_id, quest_type in self._quest_types.items())):
                raise ValueError("Entrust and quest catalog selections disagree")
            drop_raw = quests._asset(DROP_RESOURCE)
            if parse_table(drop_raw)["rows"] != list(quests._drop_table()):
                raise ValueError("Entrust reward definitions changed during setup")
            self._by_type = _grouped(self._quest_types)
            self.commands = NativeCommandIndex(quests._asset)
            self.scopes, self._windows = self._type_scopes(quest_modes.ENTRUST_PARITY_HOOKS)
            self.menu_gates = MappingProxyType({category: self._menu_gate(category) for category in MISSION_CATEGORIES})
            self._tags, self._tags_error = self._mission_tags()
            self.growth = self._growth_campaign()
            self._index, self.excluded, self._ticket_stacks = self._static_index(rows, self.scopes, self._windows)
            self.source_hashes, self.source_errors = self._sources(raw, drop_raw)
            self.content_version = self._content_version()
            self.legacy_content_version = self._legacy_content_version(rows)
            self._snapshot = tuple(getattr(self, name) for name in self._snapshot_names())
            self._ready()
        except (AttributeError, OSError, KeyError, TypeError, ValueError, OverflowError, ProtocolError) as error:
            raise ProtocolError("entrust_catalog_invalid", "Entrust resources differ from the verified main quest contract", 503) from error

    @staticmethod
    def _snapshot_names():
        return ("quests", "possessions", "resource_version", "commands", "scopes", "menu_gates", "growth", "excluded",
                "source_hashes", "source_errors", "content_version", "legacy_content_version", "_master", "_conditions",
                "_quest_types", "_main_ids", "_by_type", "_windows", "_tags", "_tags_error", "_index", "_ticket_stacks",
                "_products", "_entries", "_rewards", "_reward_stacks", "_failures", "_transient")

    def _lookup(self, name: str) -> tuple[CommandHit | None, CommandAmbiguous | None]:
        try:
            return self.commands.lookup(name), None
        except CommandAmbiguous as fault:
            return None, fault

    def _type_scopes(self, hooks: tuple[str, ...]) -> tuple[Mapping[int, TypeScope], Mapping[int, MissionWindows]]:
        known = set(MODE_SPECS) | set(self._quest_types.values())
        scopes, windows = {}, {}
        for quest_type in sorted(known.union(GATE_SCAN_TYPES)):
            hit, fault = self._lookup(TYPE_GATE.format(quest_type))
            if quest_type not in known and hit is None and fault is None:
                continue
            scopes[quest_type], found = self._type_scope(quest_type, hit, fault, hooks)
            if found is not None:
                windows[quest_type] = found
        return MappingProxyType(scopes), MappingProxyType(windows)

    def _type_scope(self, quest_type: int, hit: CommandHit | None, fault: CommandAmbiguous | None,
                    hooks: tuple[str, ...]) -> tuple[TypeScope, MissionWindows | None]:
        policy = quest_modes.policy(quest_type)
        settings, blocker, form = MappingProxyType(policy.status()), policy.entrust_blocker(hooks), form_of(hit)
        state, exclusion = _judgement(quest_type, form, fault, settings, blocker)
        windows, windows_error = None, None
        if state is ScopeState.OFFERED:
            try:
                windows = self.quests.mission_windows(quest_type)
            except ProtocolError as error:
                windows_error = error.code
                state, exclusion = ScopeState.PENDING, source_invalid(
                    f"Mission windows of quest type {quest_type} are unavailable: {error.code}")
        return TypeScope(quest_type, quest_type in AUDITED_TYPES, hit, form, fault, policy, settings, blocker,
                         windows_error, state, exclusion), windows

    def _menu_gate(self, category: int) -> MenuGate:
        route, hit, fault = None, None, None
        try:
            route = self.commands.quest_menu(category)
            hit = self.commands.resolve(route.chain, MENU_GATE.format(category))
        except CommandAmbiguous as error:
            fault = error
        scope = None if hit is None else hit.name.rpartition(SCOPE_SEPARATOR)[0]
        return MenuGate(category, route, scope, hit, form_of(hit), fault)

    def _mission_tags(self) -> tuple[MissionTagIndex | None, str | None]:
        try:
            return self.quests.mission_tags(), None
        except ProtocolError as error:
            return None, error.code

    def _growth_campaign(self) -> GrowthCampaign:
        try:
            rows = self.quests.resource_rows(CAMPAIGN_RESOURCE)
        except ProtocolError as error:
            return GrowthCampaign.unavailable(error.code)
        return GrowthCampaign.from_rows(rows, source_sha256=self.quests.resource_sha256(CAMPAIGN_RESOURCE))

    def _stack(self, item_id: str) -> int:
        try:
            return self.possessions.stack(item_id)
        except ProtocolError:
            return 0

    def _listing(self, row: Mapping, missions: tuple[MissionWindow, ...]) -> EntrustListing:
        count, disabled, ticket = (row[name] for name in TICKET_FIELDS)
        if not int32(count) or count < 0 or not int32(disabled) or disabled < 0:
            raise ValueError("Entrust ticket counts and disabled flags must be nonnegative native integers")
        if type(ticket) is not str or len(ticket.encode("utf-8")) > STORED_ITEM_NAME_BYTES or "\0" in ticket:
            raise ValueError("Entrust ticket identifiers must be bounded native text")
        item_id = None if ticket in TICKET_NULLS else ticket
        if count and item_id is None:
            raise ValueError("A consuming entrust requires a canonical ticket item")
        if item_id is not None and self._stack(item_id) <= 0:
            raise ValueError("Entrust tickets require a verified positive stack limit")
        return EntrustListing(row["Type"], item_id, count, bool(disabled), tuple(mission.mission_id for mission in missions))

    def _static_index(self, rows: Mapping[int, Mapping], scopes: Mapping[int, TypeScope],
                      windows: Mapping[int, MissionWindows]) -> tuple[Mapping[int, EntrustListing],
                                                                      Mapping[int, EntrustExclusion], Mapping[str, int]]:
        index, excluded, stacks = {}, {}, {}
        for quest_id, row in rows.items():
            scope = scopes[row["Type"]]
            if scope.state is ScopeState.REFUSED:
                continue
            if scope.state is ScopeState.PENDING:
                excluded[quest_id] = scope.exclusion
                continue
            missions = windows[row["Type"]].missions(quest_id)
            if not missions:
                excluded[quest_id] = NO_MISSION
                continue
            try:
                listing = self._listing(row, missions)
            except ValueError as error:
                excluded[quest_id] = source_invalid(f"Entrust fields of quest {quest_id} are invalid: {error}")
                continue
            if listing.ticket_item_id is not None:
                stacks[listing.ticket_item_id] = self._stack(listing.ticket_item_id)
            index[quest_id] = listing
        return MappingProxyType(index), MappingProxyType(excluded), MappingProxyType(dict(sorted(stacks.items())))

    def _sources(self, raw: bytes, drop_raw: bytes) -> tuple[Mapping[str, str | None], Mapping[str, str]]:
        hashes, errors = {}, {}
        for name in (name for category in AUDITED_CATEGORIES for name in mission_tables(category)):
            try:
                hashes[name] = self.quests.resource_sha256(name)
            except ProtocolError as error:
                hashes[name], errors[name] = None, error.code
        hashes.update({QUEST_MASTER: hashlib.sha256(raw).hexdigest(),
                       CARD_MASTER: hashlib.sha256(self._master.get_xml(CARD_MASTER)).hexdigest(),
                       DROP_RESOURCE: hashlib.sha256(drop_raw).hexdigest(),
                       CONDITION_RESOURCE: self.quests.resource_sha256(CONDITION_RESOURCE),
                       ITEM_RESOURCE: self.possessions.asset_sha256, CAMPAIGN_RESOURCE: self.growth.source_sha256})
        return MappingProxyType(dict(sorted(hashes.items()))), MappingProxyType(errors)

    def _content_version(self) -> str:
        return self._content_digest(self.scopes, self._windows, self._index, self.excluded)

    def _legacy_content_version(self, rows: Mapping[int, Mapping]) -> str:
        scopes, windows = self._type_scopes(LEGACY_PARITY_HOOKS)
        index, excluded, _ = self._static_index(rows, scopes, windows)
        return self._content_digest(scopes, windows, index, excluded)

    def _content_digest(self, scopes: Mapping[int, TypeScope], windows: Mapping[int, MissionWindows],
                        index: Mapping[int, EntrustListing], excluded: Mapping[int, EntrustExclusion]) -> str:
        return _digest({
            "schema": CONTENT_SCHEMA, "resource_version": self.resource_version, "master": self._master.identifier,
            "resources": dict(self.source_hashes), "resource_errors": dict(self.source_errors),
            "commands": self.commands.content_version,
            "scopes": {quest_type: _scope_record(scope) for quest_type, scope in scopes.items()},
            "menus": {category: _gate_record(gate) for category, gate in self.menu_gates.items()},
            "growth": self.growth.content_version,
            "mission_tags": self._tags.content_version if self._tags is not None else {"error": self._tags_error},
            "missions": {quest_type: [_window_record(mission) for mission in found.ordered()]
                         for quest_type, found in windows.items()},
            "index": {quest_id: _listing_record(listing) for quest_id, listing in index.items()},
            "excluded": {quest_id: exclusion.code for quest_id, exclusion in excluded.items()},
            "rules": dict(RULES)})

    def _ready(self):
        try:
            if any(getattr(self, name) is not value for name, value in zip(self._snapshot_names(), self._snapshot)):
                raise ValueError("Entrust catalog components were replaced")
            with self._lock:
                products, entries = dict(self._products), dict(self._entries)
                rewards, stacks = dict(self._rewards), {**self._ticket_stacks, **self._reward_stacks}
            quests, labels = self.quests, dict(entries.values())
            if (quests.snapshot is not self._master or quests.resource_version != self.resource_version
                    or quests._possessions is not self.possessions
                    or not self.possessions.is_ready()
                    or self.possessions.asset_sha256 != self.source_hashes[ITEM_RESOURCE]
                    or quests.all_ids != self._main_ids
                    or quests.special_conditions() is not self._conditions
                    or self._tags is not None and quests.mission_tags() is not self._tags
                    or any(quests.mission_windows(quest_type) is not windows for quest_type, windows in self._windows.items())
                    or not all(_policy_unchanged(scope) for scope in self.scopes.values() if scope.audited)
                    or any(quests.get(quest_id) is not product.quest or quests.mode(quest_id) is not product.mode
                           for quest_id, product in products.items())
                    or quests.entry_sha256(labels) != labels
                    or any(quests.decode_reward(code) != reward for code, reward in rewards.items())
                    or any(self.possessions.stack(name) != stack for name, stack in stacks.items())):
                raise ValueError("Entrust catalog dependencies changed")
        except (AttributeError, KeyError, TypeError, ValueError, ProtocolError) as error:
            raise ProtocolError("entrust_catalog_changed", "Entrust definitions changed after setup", 503) from error

    @property
    def ticket_item_ids(self) -> frozenset[str]:
        return frozenset(self._ticket_stacks)

    def replayable_content_versions(self) -> frozenset[str]:
        return frozenset((self.content_version, self.legacy_content_version))

    def check(self) -> CatalogCheck:
        self._ready()
        return CatalogCheck(self, _CHECK_SEAL)

    def _checked(self, check: CatalogCheck | None) -> None:
        # A request checks the catalog once and hands that check to each lookup instead of rechecking it.
        if check is None:
            self._ready()
        elif type(check) is not CatalogCheck or check.catalog is not self or check.seal is not _CHECK_SEAL:
            raise ProtocolError("entrust_check_invalid", "Entrust lookups take a readiness check of this catalog", 503)

    def product(self, quest_id, *, check: CatalogCheck | None = None) -> EntrustDefinition:
        self._checked(check)
        return self._product(quest_id)

    def _product(self, quest_id) -> EntrustDefinition:
        quest_type = self._quest_types.get(quest_id) if type(quest_id) is int else None
        if quest_type is None:
            raise ProtocolError("quest_not_found", "Quest is absent from the fixed master snapshot", 404)
        exclusion = self.scopes[quest_type].exclusion or self.excluded.get(quest_id)
        if exclusion is not None:
            raise exclusion.error()
        with self._lock:
            if quest_id in self._products:
                return self._products[quest_id]
            if quest_id in self._failures:
                raise self._failures[quest_id].error()
            return self._build(quest_id, self._index[quest_id])

    def _build(self, quest_id: int, listing: EntrustListing) -> EntrustDefinition:
        try:
            product = EntrustDefinition(self.quests.get(quest_id), self.quests.mode(quest_id), listing.quest_type,
                                        listing.ticket_item_id, listing.ticket_count, listing.disabled)
            # Prizes a record's MaxRank still withholds are verified now, so no later rank meets an unchecked reward.
            rewards = {code: self.quests.decode_reward(code) for code in product.all_reward_codes}
            if any(reward.kind not in GRANTABLE_REWARDS for reward in rewards.values()):
                raise REWARD_UNSUPPORTED.error()
        except ProtocolError as error:
            if error.status < RETRIED_STATUS:
                self._failures[quest_id] = EntrustExclusion(error.code, str(error), error.status)
            else:
                self._transient[listing.quest_type, error.code] += 1
            raise
        label = product.entry_label
        entry = self.quests.entry_sha256((label,))[label]
        if entry is None:
            raise ProtocolError("entrust_catalog_changed", "Entrust definitions changed after setup", 503)
        stacks = {reward.item_id: self.possessions.stack(reward.item_id)
                  for reward in rewards.values() if reward.item_id is not None}
        self._products[quest_id], self._entries[quest_id] = product, (label, entry)
        self._rewards.update(rewards)
        self._reward_stacks.update(stacks)
        return product

    def _frozen(self, product) -> EntrustDefinition:
        with self._lock:
            built = self._products.get(product.quest_id) if type(product) is EntrustDefinition else None
        if built is not product:
            raise ProtocolError("entrust_product_invalid", "Entrust requires its frozen catalog definition", 503)
        return product

    def validate(self, quest_types=None, *, progress: Callable[[dict], None] | None = None) -> dict:
        self._ready()
        selected = _selected_types(quest_types)
        if progress is not None and not callable(progress):
            raise ValueError("Entrust validation reports progress to a callable")
        report = _unreported if progress is None else progress
        result = {}
        for quest_type in selected:
            quest_ids = self._by_type.get(quest_type, ())
            built, excluded = 0, Counter()
            report(_progress_record(quest_type, 0, len(quest_ids)))
            for done, quest_id in enumerate(quest_ids, 1):
                try:
                    self._product(quest_id)
                    built += 1
                except ProtocolError as error:
                    excluded[error.code] += 1
                # Reported outside the build lock, so a slow listener never delays an entrust request.
                report(_progress_record(quest_type, done, len(quest_ids)))
            result[quest_type] = {"products": built, "excluded": dict(sorted(excluded.items()))}
        return result

    def require_eligible(self, state, product, *, check: CatalogCheck | None = None):
        self._checked(check)
        self._frozen(product)
        if product.disabled:
            raise ProtocolError("entrust_disabled", "Entrust is disabled for this quest", 409)
        tables = state.get("tables") if isinstance(state, dict) else None
        rows = tables.get("quest_records") if isinstance(tables, dict) else None
        if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1
                or not isinstance(rows, list) or len(rows) > MAX_QUEST_RECORDS):
            raise ProtocolError("entrust_state_invalid", "Entrust requires bounded persisted quest records", 503)
        indexed = {}
        for row in rows:
            if (not isinstance(row, dict) or not {"QuestID", "MaxRank", "AchieveFlag"} <= row.keys()
                    or not int32(row["QuestID"]) or row["QuestID"] <= 0 or row["QuestID"] in indexed
                    or not int32(row["MaxRank"]) or row["MaxRank"] < 0 or not int32(row["AchieveFlag"])):
                raise ProtocolError("entrust_state_invalid", "Entrust quest record identities, rank or flags are invalid", 503)
            indexed[row["QuestID"]] = row
        record = indexed.get(product.quest_id)
        required = sum(bit for code, bit in zip(product.quest.treasures, SLOT_FLAGS) if code)
        if record is None or record["MaxRank"] < REQUIRED_MAX_RANK or (record["AchieveFlag"] & required) != required:
            raise ProtocolError("entrust_not_eligible", "Entrust requires three stars and every nonempty treasure collected", 409)
        if not special_condition_satisfied(indexed, product.quest_id, self._conditions):
            raise ProtocolError("entrust_special_condition", "Entrust requires its related quest conditions", 409)

    def menu_open(self, mission: MissionWindow, *, now: datetime, platform_code: int) -> bool:
        return self._menu_open(mission, _clock(now), require_platform(platform_code), self._tags)

    def _menu_open(self, mission: MissionWindow, now: datetime, platform: int, tags: MissionTagIndex | None) -> bool:
        if not isinstance(mission, MissionWindow) or mission.category not in self.menu_gates:
            raise ValueError("Entrust menu gates judge a mission window of a native mission category")
        gate = self.menu_gates[mission.category]
        if gate.error is not None:
            raise ProtocolError("entrust_source_invalid",
                                f"The entrust menu gate of mission category {gate.category} is ambiguous", 503)
        if gate.form is not CommandForm.OPEN_MISSION_TAG:
            return gate.form is CommandForm.LITERAL_TRUE
        if tags is None:
            raise ProtocolError("entrust_source_invalid",
                                f"Tag menu gates require the global mission registry: {self._tags_error}", 503)
        return tags.is_open(TAG_LOOKUP, mission.tag, now=now, platform_code=platform)

    def selector(self, product, *, now: datetime, platform_code: int) -> MissionSelector:
        product = self._frozen(product)
        clock, platform = _clock(now), require_platform(platform_code)
        accepts = functools.partial(self._menu_open, now=clock, platform=platform, tags=self._tags)
        choose = self._waiving_first(product, clock) if product.ticket_count else None
        return MissionSelector(accepts, MENU_CLOSED.code, MENU_CLOSED.message, choose)

    def _waiving_first(self, product: EntrustDefinition, now: datetime) -> Callable:
        # Preferring a waiving mission can undercharge against 77A620's current mission but never overcharges.
        def choose(playable: tuple[MissionWindow, ...]) -> MissionWindow:
            return next((mission for mission in playable if self._waived(product, mission, now)), playable[0])
        return choose

    def _waived(self, product: EntrustDefinition, mission: MissionWindow, now: datetime) -> bool:
        if not isinstance(mission, MissionWindow) or mission.mission_id not in self._index[product.quest_id].mission_ids:
            raise ValueError("Ticket waivers are judged for a mission that lists the quest")
        # Ticketless quests never consult the growth campaign.
        return bool(product.ticket_count) and self.growth.free(mission.mission_id, product.quest_id, now)

    def ticket_free(self, product, mission: MissionWindow, now: datetime) -> bool:
        return self._waived(self._frozen(product), mission, _clock(now))

    def ticket_cost(self, product, mission: MissionWindow, quantity: int, now: datetime) -> int:
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("Ticket costs require a positive run count")
        return 0 if self.ticket_free(product, mission, now) else product.ticket_count * quantity

    def availability(self, *, now: datetime, platform_code: int, tags: MissionTagIndex | None = None) -> dict:
        self._ready()
        clock, platform = _clock(now), require_platform(platform_code)
        if tags is not None and not isinstance(tags, MissionTagIndex):
            raise ValueError("Availability previews take a mission tag index")
        resolver = self._tags if tags is None else tags
        gate = self._memoized_gate(clock, platform, resolver)
        entry = self._memoized_entry(clock, platform, self.quests if tags is None else _PreviewCatalog(self.quests, tags))
        types = {}
        for quest_type, scope in self.scopes.items():
            if scope.state is ScopeState.OFFERED:
                lookup = self._windows[quest_type].mission if resolver is None else resolver.registry.mission
                types[quest_type] = self._type_availability(scope, lookup, gate, entry, clock, platform)
        return {"clock": _iso(clock), "platform_code": platform, "preview": tags is not None, "types": types,
                "pending": {quest_type: scope.exclusion.code for quest_type, scope in self.scopes.items()
                            if scope.state is ScopeState.PENDING}}

    def _memoized_gate(self, now: datetime, platform: int,
                       tags: MissionTagIndex | None) -> Callable[[MissionWindow], bool | None]:
        outcomes = {}

        def gate(mission: MissionWindow) -> bool | None:
            key = (mission.category, mission.tag)
            if key not in outcomes:
                try:
                    outcomes[key] = self._menu_open(mission, now, platform, tags)
                except ProtocolError:
                    outcomes[key] = None
            return outcomes[key]
        return gate

    @staticmethod
    def _memoized_entry(now: datetime, platform: int, quests) -> Callable[[TypeScope], bool | None]:
        outcomes = {}

        def entry(scope: TypeScope) -> bool | None:
            if scope.quest_type not in outcomes:
                try:
                    outcomes[scope.quest_type] = scope.policy.entry_open(quests, now=now, platform_code=platform)
                except ProtocolError:
                    outcomes[scope.quest_type] = None
            return outcomes[scope.quest_type]
        return entry

    def _type_availability(self, scope: TypeScope, lookup: Callable[[int], MissionWindow],
                           gate: Callable[[MissionWindow], bool | None], entry: Callable[[TypeScope], bool | None],
                           now: datetime, platform: int) -> dict:
        indexed = opened = free = 0
        blocked: dict[Blocker, list[int]] = {}
        for quest_id in self._by_type.get(scope.quest_type, ()):
            listing = self._index.get(quest_id)
            if listing is None:
                continue
            indexed += 1
            blocker, waived = self._quest_availability(quest_id, listing, scope, lookup, gate, entry, now, platform)
            if blocker is None:
                opened, free = opened + 1, free + waived
            else:
                blocked.setdefault(blocker, []).append(quest_id)
        return {"indexed": indexed, "open": opened, "free": free,
                "blocked": {blocker: _bounded(quest_ids) for blocker, quest_ids in sorted(blocked.items())}}

    def _quest_availability(self, quest_id: int, listing: EntrustListing, scope: TypeScope,
                            lookup: Callable[[int], MissionWindow], gate: Callable[[MissionWindow], bool | None],
                            entry: Callable[[TypeScope], bool | None], now: datetime,
                            platform: int) -> tuple[Blocker | None, bool]:
        reached, passed = _Reach.NOT_REGISTERED, []
        for mission_id in listing.mission_ids:
            try:
                mission = lookup(mission_id)
            except ProtocolError:
                return Blocker.SOURCE_INVALID, False
            if not mission.enabled(platform):
                continue
            if mission.state(now) != OPEN:
                reached = max(reached, _Reach.MISSION_WINDOW)
                continue
            accepted = gate(mission)
            if accepted is None:
                return Blocker.SOURCE_INVALID, False
            if not accepted:
                reached = max(reached, _Reach.MENU_CLOSED)
                continue
            passed.append(mission)
            if not listing.ticket_count:
                break
        if not passed:
            return Blocker[reached.name], False
        try:
            # Entrust admission runs check_start, which judges the JST day before the mode entry gate.
            if not scope.policy.day_open(self.quests, quest_id, now):
                return Blocker.DAY_CLOSED, False
            entered = entry(scope)
            if not entered:
                return (Blocker.SOURCE_INVALID if entered is None else Blocker.MODE_ENTRY_CLOSED), False
            return None, bool(listing.ticket_count) and any(self.growth.free(mission.mission_id, quest_id, now)
                                                            for mission in passed)
        except ProtocolError:
            return Blocker.SOURCE_INVALID, False

    def describe(self, quest_id, *, now: datetime | None = None, platform_code: int | None = None) -> dict:
        self._ready()
        quest_type = self._quest_types.get(quest_id) if type(quest_id) is int else None
        if quest_type is None:
            raise ProtocolError("quest_not_found", "Quest is absent from the fixed master snapshot", 404)
        live = None if now is None and platform_code is None else (_clock(now), require_platform(platform_code))
        scope, listing, missions = self.scopes[quest_type], self._index.get(quest_id), self._members(quest_type, quest_id)
        result = {"quest_id": quest_id, "quest_type": quest_type, "type_state": scope.state,
                  "exclusion": _exclusion_record(scope.exclusion or self.excluded.get(quest_id)),
                  "ticket": None if listing is None else {"item": listing.ticket_item_id, "per_run": listing.ticket_count},
                  "disabled": None if listing is None else listing.disabled,
                  "missions": [self._mission_record(mission) for mission in missions],
                  "product": None if listing is None else self._product_record(quest_id)}
        if live is not None:
            result["live"] = self._live_record(scope, quest_id, listing, missions, *live)
        return result

    def _members(self, quest_type: int, quest_id: int) -> tuple[MissionWindow, ...]:
        windows, spec = self._windows.get(quest_type), MODE_SPECS.get(quest_type)
        if windows is not None:
            return windows.missions(quest_id)
        if self._tags is None or spec is None:
            return ()
        return tuple(mission for mission in self._tags.registry.missions(quest_id) if mission.category in spec.categories)

    def _mission_record(self, mission: MissionWindow) -> dict:
        gate = self.menu_gates[mission.category]
        return {**_window_record(mission), "route": None if gate.route is None else gate.route.func,
                "scope": gate.scope, "menu_gate": gate.form, "menu_error": None if gate.error is None else gate.error.code}

    def _product_record(self, quest_id: int) -> dict:
        try:
            product = self._product(quest_id)
        except ProtocolError as error:
            return {"built": False, "code": error.code, "status": error.status}
        return {"built": True, "entry_label": product.entry_label, "reward_codes": sorted(set(product.all_reward_codes)),
                "conditional": product.quest.conditional, "candidates": len(product.quest.candidates)}

    def _live_record(self, scope: TypeScope, quest_id: int, listing: EntrustListing | None,
                     missions: tuple[MissionWindow, ...], now: datetime, platform: int) -> dict:
        records = []
        for mission in missions:
            owner = None
            if self.menu_gates[mission.category].form is CommandForm.OPEN_MISSION_TAG and self._tags is not None:
                owner = self._tags.tag_mission(mission.tag, platform)
            waived = None
            if listing is not None and listing.ticket_count:
                waived = _attempt(self.growth.free, mission.mission_id, quest_id, now)
            records.append({"mission_id": mission.mission_id, "registered": mission.enabled(platform),
                            "state": STATE_NAMES[mission.state(now)],
                            "menu": _attempt(self._menu_open, mission, now, platform, self._tags),
                            "tag_owner": None if owner is None else owner.mission_id, "free": waived})
        entry_open = functools.partial(scope.policy.entry_open, self.quests, now=now, platform_code=platform)
        return {"clock": _iso(now), "platform_code": platform, "missions": records,
                "day_open": _attempt(scope.policy.day_open, self.quests, quest_id, now),
                "entry_open": _attempt(entry_open)}

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        with self._lock:
            built, failures, transient = frozenset(self._products), dict(self._failures), Counter(self._transient)
        scopes = tuple(self.scopes.values())
        refused = [scope for scope in scopes if scope.state is ScopeState.REFUSED]
        return {"ready": error is None, "error": error, "schema": CONTENT_SCHEMA, "content_version": self.content_version,
                "resource_version": self.resource_version, "audited_types": sorted(AUDITED_TYPES),
                "offered_types": [scope.quest_type for scope in scopes if scope.state is ScopeState.OFFERED],
                "pending_types": {scope.quest_type: _exclusion_record(scope.exclusion)
                                  for scope in scopes if scope.state is ScopeState.PENDING},
                "unaudited_native_offers": [scope.quest_type for scope in scopes
                                            if not scope.audited and scope.gate_form is CommandForm.LITERAL_TRUE],
                "types": {quest_type: self._type_status(self.scopes[quest_type], built, failures, transient)
                          for quest_type in sorted(AUDITED_TYPES)},
                "refused_type_count": len(refused),
                "refused_types": {scope.quest_type: {"quests": len(self._by_type.get(scope.quest_type, ())),
                                                     "code": scope.exclusion.code, "native_gate": scope.gate_form}
                                  for scope in refused[:STATUS_LIMIT]},
                "commands": self._commands_status(), "growth_campaign": self.growth.status(),
                "mission_tags": {"available": self._tags is not None, "error": self._tags_error,
                                 "content_version": None if self._tags is None else self._tags.content_version},
                "sources": {"resources": dict(self.source_hashes), "errors": dict(self.source_errors)},
                "native_tested": False}

    def _type_status(self, scope: TypeScope, built: frozenset[int], failures: Mapping[int, EntrustExclusion],
                     transient: Counter) -> dict:
        quest_ids = self._by_type.get(scope.quest_type, ())
        listings = {quest_id: self._index[quest_id] for quest_id in quest_ids if quest_id in self._index}
        excluded: dict[str, list[int]] = {}
        for quest_id in quest_ids:
            exclusion = self.excluded.get(quest_id) or failures.get(quest_id)
            if exclusion is not None:
                excluded.setdefault(exclusion.code, []).append(quest_id)
        validated = sum(quest_id in built for quest_id in listings)
        failed = sum(quest_id in failures for quest_id in listings)
        disabled = [quest_id for quest_id, listing in listings.items() if listing.disabled]
        return {"state": scope.state, "exclusion": _exclusion_record(scope.exclusion), "quests": len(quest_ids),
                "native_gate": _command_record(scope.gate, scope.gate_form, scope.gate_error),
                "policy": _policy_record(scope), "windows_error": scope.windows_error,
                "categories": {category: self._category_status(scope.quest_type, category)
                               for category in MODE_SPECS[scope.quest_type].categories},
                "indexed": len(listings), "validated": validated, "all_validated": validated + failed == len(listings),
                "excluded": {code: _bounded(ids) for code, ids in sorted(excluded.items())},
                "transient_failures": {code: count for (quest_type, code), count in sorted(transient.items())
                                       if quest_type == scope.quest_type},
                "disabled": _bounded(disabled),
                "ticket": {"items": dict(sorted(Counter(listing.ticket_item_id for listing in listings.values()
                                                        if listing.ticket_item_id is not None).items())),
                           "per_run": dict(sorted(Counter(listing.ticket_count for listing in listings.values()).items()))}}

    def _category_status(self, quest_type: int, category: int) -> dict:
        windows = self._windows.get(quest_type)
        missions = None if windows is None else [mission for mission in windows.ordered() if mission.category == category]
        return {**_gate_record(self.menu_gates[category]), "missions": None if missions is None else len(missions),
                "registered": None if missions is None else sum(mission.listed for mission in missions)}

    def _commands_status(self) -> dict:
        status = self.commands.status()
        return {"tables": status["tables"], "rows": status["rows"], "missing_tables": status["missing"],
                "invalid_tables": status["invalid"], "duplicate_count": status["duplicate_count"],
                "duplicates": status["duplicates"], "content_version": status["content_version"]}

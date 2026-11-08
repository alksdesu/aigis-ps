import json
import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import uuid
from xml.etree import ElementTree as ET

import pytest

from aigis_ps import admin_cmd_squads
from aigis_ps.admin_cmd_squads import (AUTO_SELECT, AUTO_SELECT_WRITES, COMMANDS, LOCK_MISSING, METADATA_REQUIRED, SET_LOCK,
                                       SORT, SORT_WRITES, SQUAD_SAVE, UNSHOWN_MEMBER)
from aigis_ps.admin_command import AdminError, Batch, BatchStep, CommandContext, PlanStore
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.current_squad import CurrentSquadService, Selection
from aigis_ps.energy_clock import JST, instant
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import (BARRACKS_SHIFT, EMPTY_SQUAD_SLOT, GIFT_TUTORIAL_FLAG, LOCK_FLAG, MAX_SQUAD_ROWS,
                                   SQUAD_FIELDS, SQUAD_TUTORIAL_FLAGS, PlayerStateService, squad_address, squad_group,
                                   squad_position)
from aigis_ps.protocol import ProtocolRegistry, RequestEnvelope, encode_wire, prepare_request
from aigis_ps.storage import AUTO_BACKUP_KIND
from aigis_ps.unit_actions import UnitActionService
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_seams import METHOD_KEY, PLAYER_ID, USER_KEY, MemoryStore
from test_bootstrap import asset_root
from test_player_state import RAID_GROUP, assets, master
from test_unit_removal import bundle, new_save


GOLDEN = Path(__file__).resolve().parent / "golden" / "p20"
GOLDEN_FILES = {73: "squad-save", 74: "squad-import", 120: "current-squad-save", 119: "unit-locks", 25: "sort-save",
                27: "auto-select-save"}
REGISTRY = ProtocolRegistry.load()
PROFILES = REGISTRY.by_registration
SAVED_AT = datetime(2026, 11, 3, 12)
CLIENT_TIME = 1000
IDENTITY = (PLAYER_ID, USER_KEY, METHOD_KEY)
OTHER_PLAYER, OTHER_KEY = "2", "c" * 32
HISTORY = "current_squad_history"
TITLE_VERSION = "c" * 64
TITLE_COUNT, LOCKED_TITLE, UNSUPPORTED_TITLE = 8, 4, 5
SECOND_BARRACKS, SIGN_BIT = 1 << BARRACKS_SHIFT, -0x80000000
# GetIndex and GeneralPurposeFlags of the units added next to the prince.
ROSTER = ((1, 1), (2, 1 | LOCK_FLAG), (3, 1 | SECOND_BARRACKS), (4, SIGN_BIT | 0x101))
FREE, LOCKED, STORED, SIGNED = (index for index, _ in ROSTER)
CONSUMED, HIGHEST = 5, 6
NEVER_ISSUED = HIGHEST + 1
FIRST, SECOND, RENAMED, RAID_NAME = "第一隊", "第二隊", "改名", "討伐"
SIGNED_GROUP, FILLER_GROUP, NEW_GROUP, LAST_TEAM = 200, 100, 6, 19
LONG_NAME = "長" * 42
SORT_VALUES = ("010203", "0001", "1010")
AUTO_SELECT_VALUES = (513, -0x80000000)


class Titles:
    def __init__(self):
        self.content_version = TITLE_VERSION

    def status(self) -> dict:
        return {"ready": True}

    def is_ready(self) -> bool:
        return True

    def require_eligible(self, index: int, state: dict) -> int:
        if type(index) is not int or not 0 <= index < TITLE_COUNT:
            raise ProtocolError("emblem_unknown", "Title index is not in the verified catalog")
        if index == UNSUPPORTED_TITLE:
            raise ProtocolError("emblem_unsupported", "Title expression or effect requires an additional verified rule", 501)
        if index == LOCKED_TITLE:
            raise ProtocolError("emblem_locked", "Title acquisition condition is not satisfied", 409)
        return index


@dataclass
class World:
    bootstrap: BootstrapService
    players: PlayerStateService
    titles: Titles
    squads: CurrentSquadService
    actions: UnitActionService

    def handler(self, registration: int) -> Callable:
        return (self.players.handlers() | self.squads.handlers() | self.actions.handlers())[registration]


def golden_world(root: Path, resources: dict[str, bytes], cards: bytes) -> World:
    bootstrap = BootstrapService(root, clock=lambda: SAVED_AT, asset_version="a" * 40)
    titles = Titles()
    players = PlayerStateService(root, lambda name: cards, asset_reader=resources.__getitem__,
                                 squad_emblem_validator=titles.require_eligible)
    return World(bootstrap, players, titles, CurrentSquadService(bootstrap, players, titles, resources.__getitem__),
                 UnitActionService(bootstrap, players))


def squad_row(group: int, team: int = 0, slot: int = 1, unit: int = EMPTY_SQUAD_SLOT, name: str = "", flags: int = 0) -> dict:
    address = (group << 24) | (team << 16) | slot
    return {"Address": address - (1 << 32) if address >= 1 << 31 else address, "UnitIndex": unit, "Name": name, "Flags": flags}


def stored_squads() -> list[dict]:
    return [squad_row(0, slot=1, unit=FREE, name=FIRST), squad_row(0, slot=2, unit=LOCKED, name=FIRST),
            squad_row(0, slot=3, name=FIRST), squad_row(1, slot=1, unit=SIGNED, name=SECOND, flags=1),
            squad_row(RAID_GROUP, team=0, slot=1, unit=FREE), squad_row(RAID_GROUP, team=1, slot=1, unit=LOCKED),
            squad_row(RAID_GROUP, team=2, slot=1)]


def golden_state(world: World) -> dict:
    state = world.players.initialize(world.bootstrap.initial_state())
    state["tables"]["config"][0]["MenuTutorialFlags"] = GIFT_TUTORIAL_FLAG
    player = state["player_state"]
    prince = player["units"][0]
    player["units"].extend({**prince, "CardID": 2, "ClassID": 20, "SettingSkillID": 3, "GetIndex": index,
                            "UniqueID": index + 1, "GeneralPurposeFlags": flags} for index, flags in ROSTER)
    player["battle"]["UX"] = HIGHEST
    player.update(last_unique_id=HIGHEST + 1, legacy_squad_imported=True, squads=stored_squads())
    return state


def envelope(registration: int, columns: tuple, identity: tuple[str, str, str] = IDENTITY) -> RequestEnvelope:
    wire = PROFILES[registration]
    player, user, method = identity
    common = (Column("UI", "S", (player,)), Column("UK", "S", (user,)), Column("MK", "S", (method,)),
              Column("CT", "I", (CLIENT_TIME,)), Column("PFN", "S", ("dmm-r18",)), Column("PlatformCode", "I", (1,)),
              Column("GPVI", "S", ("unkown",)), Column("GPOT", "S", ("unkown",)))
    xml = prepare_request(dumps(Container(children=common + tuple(columns))), wire.st)
    return REGISTRY.decode(wire.route, encode_wire(xml, wire.request_key))


def request_key(request: RequestEnvelope) -> str:
    profile = request.profile
    return sha256(f"120:{profile.route}:{profile.st}:".encode() + request.xml).hexdigest()


@dataclass(frozen=True)
class GoldenCase:
    columns: tuple = ()
    identity: tuple[str, str, str] = IDENTITY
    registration: int | None = None
    mutate: Callable[[ET.Element], None] | None = None
    prepare: Callable[[dict], None] | None = None
    change: Callable[[World], None] | None = None
    processed: bool = False
    conflicts: int = 0
    response_limit: int | None = None
    replays: int = 0
    between: Callable[[dict], None] | None = None

    def envelope(self, registration: int) -> RequestEnvelope:
        request = envelope(self.registration or registration, self.columns, self.identity)
        if self.mutate is not None:
            self.mutate(request.document)
        if self.response_limit is not None:
            request = replace(request, profile=replace(request.profile, response_limit=self.response_limit))
        return request


class GoldenStore(MemoryStore):
    def __init__(self, state: dict, conflicts: int = 0):
        super().__init__(state)
        self.conflicts = conflicts

    def update_state(self, state: dict, expected_revision: int):
        if self.conflicts:
            self.conflicts -= 1
            raise ValueError("Profile revision changed; reload before updating")
        return super().update_state(state, expected_revision)


class Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(f"{record.levelname}: {record.getMessage()}")


def answer(handler: Callable, request: RequestEnvelope, store: GoldenStore) -> dict:
    logger, captured = logging.getLogger("aigis_ps"), Warnings()
    level = logger.level
    logger.addHandler(captured)
    logger.setLevel(logging.WARNING)
    try:
        result = {"response": handler(request, store).decode("utf-8")}
    except ProtocolError as error:
        result = {"error": {"code": error.code, "message": str(error), "status": error.status}}
    finally:
        logger.removeHandler(captured)
        logger.setLevel(level)
    if captured.messages:
        result["logs"] = captured.messages
    return result


def canonical(value: object) -> str:
    # Plain equality treats True as 1, so states are compared through JSON text.
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def tracked(state: dict) -> dict:
    tables = state.get("tables")
    return {"player_state": state.get("player_state"), HISTORY: state.get(HISTORY),
            "config": tables.get("config") if isinstance(tables, dict) else None}


def untracked(state: dict) -> dict:
    return {key: {name: rows for name, rows in value.items() if name != "config"} if key == "tables" else value
            for key, value in state.items() if key not in ("player_state", HISTORY)}


def golden_outcome(inputs: tuple, registration: int, case: GoldenCase) -> dict:
    world = golden_world(*inputs)
    if case.change is not None:
        case.change(world)
    request = case.envelope(registration)
    state = golden_state(world)
    if case.prepare is not None:
        case.prepare(state)
    if case.processed:
        state[HISTORY] = {"schema_version": 2, "receipts": [], "processed_keys": [request_key(request)]}
    store = GoldenStore(state, case.conflicts)
    before = store.profile()
    handler = world.handler(registration)
    calls = [answer(handler, request, store)]
    for _ in range(case.replays):
        if case.between is not None:
            current = store.profile()
            changed = deepcopy(current.state)
            case.between(changed)
            store.update_state(changed, current.revision)
        calls.append(answer(handler, request, store))
    after = store.profile()
    assert canonical(untracked(after.state)) == canonical(untracked(before.state))
    start, end = tracked(before.state), tracked(after.state)
    return {"request": ET.tostring(request.document, encoding="unicode"), "calls": calls,
            "revisions": after.revision - before.revision,
            "changed": {name: value for name, value in end.items() if canonical(value) != canonical(start[name])}}


def adding(tag: str, kind: str = "I", value: str = "99", parent: str | None = None) -> Callable[[ET.Element], None]:
    def mutate(document: ET.Element) -> None:
        node = ET.SubElement(document if parent is None else document.find(parent), tag, T=kind)
        ET.SubElement(node, "V").text = value
    return mutate


def dropping(tag: str, parent: str | None = None) -> Callable[[ET.Element], None]:
    def mutate(document: ET.Element) -> None:
        container = document if parent is None else document.find(parent)
        container.remove(container.find(tag))
    return mutate


def retyping(path: str, kind: str, value: str | None = None) -> Callable[[ET.Element], None]:
    def mutate(document: ET.Element) -> None:
        node = document.find(path)
        node.set("T", kind)
        if value is not None:
            node.find("V").text = value
    return mutate


def rewriting(path: str, value: str) -> Callable[[ET.Element], None]:
    def mutate(document: ET.Element) -> None:
        document.find(path).text = value
    return mutate


def repeating(path: str) -> Callable[[ET.Element], None]:
    def mutate(document: ET.Element) -> None:
        node = document.find(path)
        ET.SubElement(node, "V").text = node.find("V").text
    return mutate


def duplicating(tag: str) -> Callable[[ET.Element], None]:
    def mutate(document: ET.Element) -> None:
        document.append(deepcopy(document.find(tag)))
    return mutate


def root_attribute(document: ET.Element) -> None:
    document.set("T", "I")


def both(*mutations: Callable) -> Callable:
    def combined(target) -> None:
        for mutation in mutations:
            mutation(target)
    return combined


def squad_columns(rows: list[dict] = (), *, tutorial: int | None = None) -> tuple:
    columns = tuple(Column(wire, kind, tuple(row[name] for row in rows)) for name, wire, kind in SQUAD_FIELDS) if rows else ()
    return columns + ((Column("TL", "I", (tutorial,)),) if tutorial is not None else ())


def selection(group: int, name: str, title: int, *, selector: str = "OI", tutorial: int | None = None,
              extra: tuple = ()) -> tuple:
    columns = (Column(selector, "I", (group,)), Column("A3", "S", (name,)), Column("A4", "I", (title,)))
    return columns + ((Column("TL", "I", (tutorial,)),) if tutorial is not None else ()) + extra


def lock_columns(rows: tuple[tuple[int, int], ...]) -> tuple:
    return (Container("PU", (Column("A7", "I", tuple(index for index, _ in rows)),
                             Column("AE", "I", tuple(flags for _, flags in rows)))),)


def sort_columns(values: tuple[str, str, str]) -> tuple:
    return tuple(Column(wire, "S", (value,)) for wire, value in zip(("F1", "F4", "F5"), values))


def auto_select_columns(values: tuple[int, int]) -> tuple:
    return tuple(Column(wire, "I", (value,)) for wire, value in zip(("F6", "FB"), values))


def player(state: dict) -> dict:
    return state["player_state"]


def configuration(state: dict) -> dict:
    return state["tables"]["config"][0]


def invalid_battle(state: dict) -> None:
    player(state)["battle"]["UX"] = 0


def stored_unknown_member(state: dict) -> None:
    player(state)["squads"].append(squad_row(9, slot=1, unit=NEVER_ISSUED))


def invalid_tutorial_flags(state: dict) -> None:
    configuration(state)["MenuTutorialFlags"] = True


def saved_tutorial(state: dict) -> None:
    configuration(state)["MenuTutorialFlags"] = GIFT_TUTORIAL_FLAG | SQUAD_TUTORIAL_FLAGS


def locked_second_title(state: dict) -> None:
    for row in player(state)["squads"]:
        if row["Name"] == SECOND:
            row["Flags"] = LOCKED_TITLE


def filled_squads(total: int) -> Callable[[dict], None]:
    def fill(state: dict) -> None:
        squads = player(state)["squads"]
        squads.extend(squad_row(FILLER_GROUP, team=index // 16, slot=index % 16) for index in range(total - len(squads)))
    return fill


def pending_import(state: dict) -> None:
    player(state).update(legacy_squad_imported=False, squads=[])


def legacy_members(state: dict) -> None:
    player(state)["legacy_squad_imported"] = False
    player(state)["units"][1]["OrganizationIndex2"] = 1


def legacy_without_group_zero(state: dict) -> None:
    legacy_members(state)
    player(state)["squads"] = [row for row in player(state)["squads"] if row["Name"] != FIRST]


def long_named_squad(state: dict) -> None:
    player(state)["squads"].append(squad_row(2, slot=1, name=LONG_NAME, flags=1))


def invalid_history(state: dict) -> None:
    state[HISTORY] = {"schema_version": 3}


def raised_experience(state: dict) -> None:
    player(state)["units"][1]["Exp"] += 1


def retitled_catalog(world: World) -> None:
    world.titles.content_version = "d" * 64


def small_configuration_reply(world: World) -> None:
    world.actions._configuration_profile = replace(world.actions._configuration_profile, response_limit=1)


def missing_configuration(state: dict) -> None:
    state["tables"]["config"] = []


def textual_volume(state: dict) -> None:
    configuration(state)["_VolumeBgm"] = "80"


def stored_sort(state: dict) -> None:
    configuration(state).update(_FlagSort=SORT_VALUES[0], _FlagMissionFilter=SORT_VALUES[1], TypeOfSortOrder=SORT_VALUES[2])


def boolean_auto_select(state: dict) -> None:
    configuration(state)["AutoSelectConfig"] = True


REPLACED = [squad_row(1, slot=1, unit=FREE, name=RENAMED, flags=2), squad_row(1, slot=2, name=RENAMED, flags=2),
            squad_row(1, team=1, slot=1, unit=SIGNED, name=RENAMED, flags=2)]
CONFLICTING = [squad_row(1, slot=1, name="甲"), squad_row(1, slot=2, name="乙")]
DUPLICATED = [squad_row(1, slot=1, unit=FREE), squad_row(1, slot=1)]
BARRACKS_MEMBER = [squad_row(1, slot=1, unit=STORED, name=SECOND, flags=1)]
LOCKED_TITLE_ROW = [squad_row(1, slot=1, unit=FREE, name=SECOND, flags=LOCKED_TITLE)]
WRONG_USER = (PLAYER_ID, OTHER_KEY, METHOD_KEY)
WRONG_METHOD = (PLAYER_ID, USER_KEY, OTHER_KEY)

SQUAD_SAVE_CASES = {
    "replace_group": GoldenCase(squad_columns(REPLACED)),
    "replace_two_groups": GoldenCase(squad_columns([squad_row(0, slot=1, unit=SIGNED, name=FIRST),
                                                    squad_row(NEW_GROUP, slot=1, unit=FREE, name="第七隊", flags=3)])),
    "raid_group": GoldenCase(squad_columns([squad_row(RAID_GROUP, team=team, slot=1, unit=unit, name=RAID_NAME, flags=6)
                                            for team, unit in ((0, LOCKED), (1, FREE), (2, SIGNED))])),
    "signed_address": GoldenCase(squad_columns([squad_row(SIGNED_GROUP, slot=1, unit=FREE)])),
    "prince_member": GoldenCase(squad_columns([squad_row(2, slot=1, unit=0, name="王子")])),
    "unchanged_rows": GoldenCase(squad_columns(stored_squads()[4:])),
    "same_rows_move_last": GoldenCase(squad_columns(stored_squads()[:3])),
    "kept_locked_title": GoldenCase(squad_columns(LOCKED_TITLE_ROW), prepare=locked_second_title),
    "tutorial_only": GoldenCase(squad_columns(tutorial=1)),
    "tutorial_already_saved": GoldenCase(squad_columns(tutorial=1), prepare=saved_tutorial),
    "tutorial_with_rows": GoldenCase(squad_columns([squad_row(3, slot=1, unit=LOCKED, name="教程")], tutorial=1)),
    "empty_request": GoldenCase(),
    "consumed_members": GoldenCase(squad_columns([squad_row(0, slot=1, unit=FREE, name=FIRST),
                                                  squad_row(0, slot=2, unit=CONSUMED, name=FIRST),
                                                  squad_row(1, slot=1, unit=HIGHEST, name=SECOND, flags=1)])),
    "conflict_then_commit": GoldenCase(squad_columns(REPLACED), conflicts=1),
    "wrong_player": GoldenCase(squad_columns(REPLACED), identity=(OTHER_PLAYER, USER_KEY, METHOD_KEY)),
    "wrong_user_key": GoldenCase(squad_columns(REPLACED), identity=WRONG_USER),
    "wrong_method_key": GoldenCase(squad_columns(REPLACED), identity=WRONG_METHOD),
    "tutorial_zero": GoldenCase(squad_columns(REPLACED, tutorial=0)),
    "tutorial_text": GoldenCase(squad_columns(REPLACED, tutorial=1), mutate=retyping("TL", "S")),
    "tutorial_twice": GoldenCase(squad_columns(REPLACED, tutorial=1), mutate=repeating("TL")),
    "unknown_field": GoldenCase(squad_columns(REPLACED), mutate=adding("A2")),
    "root_attribute": GoldenCase(squad_columns(REPLACED), mutate=root_attribute),
    "missing_column": GoldenCase(squad_columns(REPLACED), mutate=dropping("A4")),
    "column_lengths_differ": GoldenCase(squad_columns(REPLACED), mutate=repeating("A1")),
    "column_type": GoldenCase(squad_columns(REPLACED), mutate=retyping("A0", "S")),
    "metadata_conflict": GoldenCase(squad_columns(CONFLICTING)),
    "never_issued_member": GoldenCase(squad_columns([squad_row(1, slot=1, unit=NEVER_ISSUED)])),
    "negative_member": GoldenCase(squad_columns([squad_row(1, slot=1, unit=-2)])),
    "duplicate_address": GoldenCase(squad_columns(DUPLICATED)),
    "reserved_group": GoldenCase(squad_columns([squad_row(255, slot=1)])),
    "slot_out_of_range": GoldenCase(squad_columns([squad_row(1, slot=16)])),
    "long_name": GoldenCase(squad_columns([squad_row(1, slot=1, name="n" * 129)])),
    "new_title_locked": GoldenCase(squad_columns(LOCKED_TITLE_ROW)),
    "new_title_unknown": GoldenCase(squad_columns([squad_row(1, slot=1, name=SECOND, flags=TITLE_COUNT)])),
    "new_title_unsupported": GoldenCase(squad_columns([squad_row(1, slot=1, name=SECOND, flags=UNSUPPORTED_TITLE)])),
    "stored_unit_member": GoldenCase(squad_columns(BARRACKS_MEMBER)),
    "stored_battle_invalid": GoldenCase(squad_columns(REPLACED), prepare=invalid_battle),
    "stored_squads_invalid": GoldenCase(squad_columns(REPLACED), prepare=stored_unknown_member),
    "tutorial_configuration_invalid": GoldenCase(squad_columns(REPLACED, tutorial=1), prepare=invalid_tutorial_flags),
    "response_too_large": GoldenCase(squad_columns(REPLACED), response_limit=8),
    "merged_rows_over_limit": GoldenCase(squad_columns([squad_row(NEW_GROUP, slot=1)]),
                                         prepare=filled_squads(MAX_SQUAD_ROWS)),
    "conflicts_exhausted": GoldenCase(squad_columns(REPLACED), conflicts=3),
    "priority_identity_before_tutorial": GoldenCase(squad_columns(REPLACED, tutorial=2), identity=WRONG_USER),
    "priority_tutorial_before_shape": GoldenCase(squad_columns(REPLACED, tutorial=2), mutate=adding("A2")),
    "priority_shape_before_metadata": GoldenCase(squad_columns(CONFLICTING), mutate=adding("A2")),
    "priority_metadata_before_stored_state": GoldenCase(squad_columns(CONFLICTING), prepare=invalid_battle),
    "priority_stored_state_before_rows": GoldenCase(squad_columns(DUPLICATED), prepare=invalid_battle),
    "priority_member_before_title": GoldenCase(squad_columns([squad_row(1, slot=1, unit=NEVER_ISSUED, flags=LOCKED_TITLE)])),
    "priority_title_before_barracks": GoldenCase(squad_columns([squad_row(1, slot=1, unit=STORED, flags=LOCKED_TITLE)])),
    "priority_barracks_before_configuration": GoldenCase(squad_columns(BARRACKS_MEMBER, tutorial=1),
                                                         prepare=invalid_tutorial_flags),
    "priority_configuration_before_response_size": GoldenCase(squad_columns(REPLACED, tutorial=1),
                                                              prepare=invalid_tutorial_flags, response_limit=8),
}

LEGACY_ROWS = [squad_row(0, slot=1, unit=FREE), squad_row(0, slot=2, unit=LOCKED), squad_row(0, slot=2),
               squad_row(0, slot=3), squad_row(1, slot=1)]
IMPORTED_ROW = [squad_row(0, slot=1, unit=SIGNED, name=FIRST)]

SQUAD_IMPORT_CASES = {
    "native_padding": GoldenCase(squad_columns(LEGACY_ROWS), prepare=pending_import),
    "single_member": GoldenCase(squad_columns(IMPORTED_ROW), prepare=pending_import),
    "stored_unit_member": GoldenCase(squad_columns([squad_row(0, slot=1, unit=STORED)]), prepare=pending_import),
    "already_imported": GoldenCase(squad_columns(IMPORTED_ROW)),
    "already_imported_unknown_member": GoldenCase(squad_columns([squad_row(0, slot=1, unit=NEVER_ISSUED)])),
    "already_imported_response_size": GoldenCase(squad_columns(IMPORTED_ROW), response_limit=8),
    "conflict_then_commit": GoldenCase(squad_columns(LEGACY_ROWS), prepare=pending_import, conflicts=1),
    "tutorial_field": GoldenCase(squad_columns(IMPORTED_ROW, tutorial=1), prepare=pending_import),
    "no_rows": GoldenCase(prepare=pending_import),
    "unknown_field": GoldenCase(squad_columns(IMPORTED_ROW), prepare=pending_import, mutate=adding("A2")),
    "two_members_one_address": GoldenCase(squad_columns([squad_row(0, slot=1, unit=FREE), squad_row(0, slot=1, unit=LOCKED)]),
                                          prepare=pending_import),
    "padding_before_member": GoldenCase(squad_columns([squad_row(0, slot=1), squad_row(0, slot=1, unit=FREE)]),
                                        prepare=pending_import),
    "padding_metadata_differs": GoldenCase(squad_columns([squad_row(0, slot=1, unit=FREE), squad_row(0, slot=1, name="x")]),
                                           prepare=pending_import),
    "three_rows_one_address": GoldenCase(squad_columns([squad_row(0, slot=1, unit=FREE), squad_row(0, slot=1),
                                                        squad_row(0, slot=1)]), prepare=pending_import),
    "consumed_member": GoldenCase(squad_columns([squad_row(0, slot=1, unit=CONSUMED)]), prepare=pending_import),
    "wrong_user_key": GoldenCase(squad_columns(IMPORTED_ROW), prepare=pending_import, identity=WRONG_USER),
    "stored_battle_invalid": GoldenCase(squad_columns(IMPORTED_ROW), prepare=both(pending_import, invalid_battle)),
    "response_too_large": GoldenCase(squad_columns(IMPORTED_ROW), prepare=pending_import, response_limit=8),
    "conflicts_exhausted": GoldenCase(squad_columns(IMPORTED_ROW), prepare=pending_import, conflicts=3),
    "priority_identity_before_shape": GoldenCase(squad_columns(IMPORTED_ROW), prepare=pending_import, identity=WRONG_USER,
                                                 mutate=adding("A2")),
    "priority_shape_before_imported": GoldenCase(squad_columns(IMPORTED_ROW), mutate=adding("A2")),
    "priority_stored_state_before_rows": GoldenCase(squad_columns(DUPLICATED), prepare=both(pending_import, invalid_battle)),
}

REPLAYED = selection(4, "再送", 0)
LEGACY_BRANCH = (Container("PU", (Column("A7", "I", (FREE,)), Column("AE", "I", (0,)))),)

CURRENT_SQUAD_CASES = {
    "new_group": GoldenCase(selection(LAST_TEAM, "最終隊", 0)),
    "existing_group": GoldenCase(selection(1, "変更", 2)),
    "existing_name": GoldenCase(selection(0, FIRST, 0)),
    "existing_long_name": GoldenCase(selection(2, LONG_NAME, 1), prepare=long_named_squad),
    "empty_name_new_group": GoldenCase(selection(7, "", 0)),
    "raid_group": GoldenCase(selection(RAID_GROUP, RAID_NAME, 1, selector="RTN")),
    "with_tutorial": GoldenCase(selection(3, "教程", 0, tutorial=1)),
    "legacy_with_group_zero": GoldenCase(selection(3, "移行", 0), prepare=legacy_members),
    "legacy_without_members": GoldenCase(selection(0, "", 0), prepare=pending_import),
    "exact_replay": GoldenCase(REPLAYED, replays=1),
    "replay_after_change": GoldenCase(REPLAYED, replays=1, between=raised_experience),
    "processed_without_receipt": GoldenCase(REPLAYED, processed=True),
    "history_invalid": GoldenCase(REPLAYED, prepare=invalid_history),
    "conflict_then_commit": GoldenCase(REPLAYED, conflicts=1),
    "legacy_branch": GoldenCase(selection(1, SECOND, 1, extra=LEGACY_BRANCH)),
    "both_selectors": GoldenCase(selection(1, SECOND, 1, extra=(Column("RTN", "I", (RAID_GROUP,)),))),
    "missing_selector": GoldenCase(selection(1, SECOND, 1), mutate=dropping("OI")),
    "ordinary_out_of_range": GoldenCase(selection(LAST_TEAM + 1, SECOND, 1)),
    "raid_group_differs": GoldenCase(selection(RAID_GROUP + 1, RAID_NAME, 1, selector="RTN")),
    "name_too_many_characters": GoldenCase(selection(8, "x" * 13, 0)),
    "name_forbidden_quote": GoldenCase(selection(8, "can't", 0)),
    "name_over_byte_limit": GoldenCase(selection(8, "長" * 43, 0)),
    "tutorial_two": GoldenCase(selection(8, "教程", 0, tutorial=2)),
    "missing_title": GoldenCase(selection(8, "称号", 0), mutate=dropping("A4")),
    "legacy_pending": GoldenCase(selection(3, "移行", 0), prepare=legacy_without_group_zero),
    "title_locked": GoldenCase(selection(1, SECOND, LOCKED_TITLE)),
    "title_unknown": GoldenCase(selection(1, SECOND, TITLE_COUNT)),
    "title_unsupported": GoldenCase(selection(1, SECOND, UNSUPPORTED_TITLE)),
    "wrong_user_key": GoldenCase(REPLAYED, identity=WRONG_USER),
    "wrong_method_key": GoldenCase(REPLAYED, identity=WRONG_METHOD),
    "stored_battle_invalid": GoldenCase(REPLAYED, prepare=invalid_battle),
    "tutorial_configuration_invalid": GoldenCase(selection(3, "教程", 0, tutorial=1), prepare=invalid_tutorial_flags),
    "response_too_large": GoldenCase(REPLAYED, response_limit=8),
    "squad_rows_over_limit": GoldenCase(selection(LAST_TEAM, "最終隊", 0), prepare=filled_squads(MAX_SQUAD_ROWS - 6)),
    "dependencies_changed": GoldenCase(REPLAYED, change=retitled_catalog),
    "conflicts_exhausted": GoldenCase(REPLAYED, conflicts=3),
    "priority_dependencies_before_request": GoldenCase(selection(1, SECOND, 1), mutate=dropping("OI"),
                                                       change=retitled_catalog),
    "priority_request_before_identity": GoldenCase(selection(1, SECOND, 1), mutate=dropping("OI"), identity=WRONG_USER),
    "priority_branch_before_selector": GoldenCase(selection(1, SECOND, 1, extra=LEGACY_BRANCH), mutate=dropping("OI")),
    "priority_identity_before_history": GoldenCase(REPLAYED, prepare=invalid_history, identity=WRONG_USER),
    "priority_history_before_state": GoldenCase(REPLAYED, prepare=both(invalid_history, invalid_battle)),
    "priority_name_before_legacy": GoldenCase(selection(3, "x" * 13, 0), prepare=legacy_without_group_zero),
    "priority_legacy_before_title": GoldenCase(selection(3, "移行", LOCKED_TITLE), prepare=legacy_without_group_zero),
    "priority_title_before_configuration": GoldenCase(selection(1, SECOND, LOCKED_TITLE, tutorial=1),
                                                      prepare=invalid_tutorial_flags),
    "priority_configuration_before_response_size": GoldenCase(selection(3, "教程", 0, tutorial=1),
                                                              prepare=invalid_tutorial_flags, response_limit=8),
}

UNIT_LOCK_CASES = {
    "lock": GoldenCase(lock_columns(((FREE, 0x103),))),
    "unlock": GoldenCase(lock_columns(((LOCKED, 0),))),
    "prince_and_other_barracks": GoldenCase(lock_columns(((0, LOCK_FLAG), (STORED, 0x13)))),
    "signed_flags": GoldenCase(lock_columns(((SIGNED, LOCK_FLAG),))),
    "already_locked": GoldenCase(lock_columns(((LOCKED, LOCK_FLAG),))),
    "empty_map": GoldenCase((Container("PU"),)),
    "empty_columns": GoldenCase(lock_columns(())),
    "conflict_then_commit": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), conflicts=1),
    "unknown_unit": GoldenCase(lock_columns(((FREE, LOCK_FLAG), (NEVER_ISSUED, LOCK_FLAG)))),
    "consumed_unit": GoldenCase(lock_columns(((CONSUMED, LOCK_FLAG),))),
    "duplicate_index": GoldenCase(lock_columns(((FREE, LOCK_FLAG), (FREE, 0)))),
    "negative_index": GoldenCase(lock_columns(((-1, LOCK_FLAG),))),
    "missing_flags": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), mutate=dropping("AE", "PU")),
    "rows_differ": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), mutate=repeating("PU/A7")),
    "extra_column": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), mutate=adding("A4", parent="PU")),
    "missing_map": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), mutate=dropping("PU")),
    "duplicate_map": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), mutate=duplicating("PU")),
    "flags_text": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), mutate=retyping("PU/AE", "S")),
    "wrong_user_key": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), identity=WRONG_USER),
    "wrong_method_key": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), identity=WRONG_METHOD),
    "stored_battle_invalid": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), prepare=invalid_battle),
    "response_too_large": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), response_limit=8),
    "conflicts_exhausted": GoldenCase(lock_columns(((FREE, LOCK_FLAG),)), conflicts=3),
    "priority_request_before_identity": GoldenCase(lock_columns(((FREE, LOCK_FLAG), (FREE, 0))), identity=WRONG_USER),
    "priority_identity_before_ownership": GoldenCase(lock_columns(((NEVER_ISSUED, LOCK_FLAG),)), identity=WRONG_USER),
    "priority_stored_state_before_ownership": GoldenCase(lock_columns(((NEVER_ISSUED, LOCK_FLAG),)), prepare=invalid_battle),
}

SORT_CASES = {
    "sort": GoldenCase(sort_columns(SORT_VALUES)),
    "empty": GoldenCase(sort_columns(("", "", ""))),
    "maximum": GoldenCase(sort_columns(("1" * 127, "0" * 31, "2" * 127))),
    "multibyte": GoldenCase(sort_columns(("猫" * 42, "猫" * 10, "猫"))),
    "unchanged": GoldenCase(sort_columns(SORT_VALUES), prepare=stored_sort),
    "conflict_then_commit": GoldenCase(sort_columns(SORT_VALUES), conflicts=1),
    "sort_overflow": GoldenCase(sort_columns(("1" * 128, "", ""))),
    "filter_overflow": GoldenCase(sort_columns(("", "1" * 32, ""))),
    "order_overflow": GoldenCase(sort_columns(("", "", "1" * 128))),
    "multibyte_overflow": GoldenCase(sort_columns(("猫" * 43, "", ""))),
    "missing_field": GoldenCase(sort_columns(SORT_VALUES), mutate=dropping("F5")),
    "unknown_field": GoldenCase(sort_columns(SORT_VALUES), mutate=adding("F2", "S", "1")),
    "integer_field": GoldenCase(sort_columns(SORT_VALUES), mutate=retyping("F5", "I", "1")),
    "filter_registration": GoldenCase(sort_columns(SORT_VALUES), registration=26),
    "wrong_user_key": GoldenCase(sort_columns(SORT_VALUES), identity=WRONG_USER),
    "wrong_method_key": GoldenCase(sort_columns(SORT_VALUES), identity=WRONG_METHOD),
    "configuration_missing": GoldenCase(sort_columns(SORT_VALUES), prepare=missing_configuration),
    "configuration_text_volume": GoldenCase(sort_columns(SORT_VALUES), prepare=textual_volume),
    "response_too_large": GoldenCase(sort_columns(SORT_VALUES), response_limit=8),
    "conflicts_exhausted": GoldenCase(sort_columns(SORT_VALUES), conflicts=3),
    "priority_request_before_identity": GoldenCase(sort_columns(("1" * 128, "", "")), identity=WRONG_USER),
    "priority_identity_before_configuration": GoldenCase(sort_columns(SORT_VALUES), identity=WRONG_USER,
                                                         prepare=missing_configuration),
}

AUTO_SELECT_CASES = {
    "values": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES)),
    "extremes": GoldenCase(auto_select_columns((0x7FFFFFFF, -1))),
    "unchanged": GoldenCase(auto_select_columns((0, 0))),
    "conflict_then_commit": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), conflicts=1),
    "overflow": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), mutate=rewriting("F6/V", "2147483648")),
    "missing_field": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), mutate=dropping("FB")),
    "boolean_field": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), mutate=retyping("F6", "B", "1")),
    "unknown_field": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), mutate=adding("F5")),
    "sort_registration": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), registration=25),
    "wrong_user_key": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), identity=WRONG_USER),
    "stored_value_boolean": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), prepare=boolean_auto_select),
    "response_too_large": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), response_limit=8),
    "configuration_reply_too_large": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), change=small_configuration_reply),
    "conflicts_exhausted": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), conflicts=3),
    "priority_request_before_identity": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), identity=WRONG_USER,
                                                   mutate=dropping("FB")),
    "priority_stored_value_before_reply_size": GoldenCase(auto_select_columns(AUTO_SELECT_VALUES), prepare=boolean_auto_select,
                                                          change=small_configuration_reply),
}

CASES = {73: SQUAD_SAVE_CASES, 74: SQUAD_IMPORT_CASES, 120: CURRENT_SQUAD_CASES, 119: UNIT_LOCK_CASES, 25: SORT_CASES,
         27: AUTO_SELECT_CASES}
GOLDEN_CASES = [(registration, name) for registration, cases in CASES.items() for name in cases]


def golden_file(registration: int) -> Path:
    return GOLDEN / f"{GOLDEN_FILES[registration]}.json"


@cache
def recorded(registration: int) -> dict:
    return json.loads(golden_file(registration).read_bytes())


@pytest.fixture
def inputs(asset_root, assets, master):
    return asset_root, assets[1], master


def test_goldens_cover_every_case_with_answers_and_refusals():
    for registration, cases in CASES.items():
        golden = recorded(registration)
        assert golden["registration"] == registration and golden["cases"].keys() == cases.keys()
        calls = [call for outcome in golden["cases"].values() for call in outcome["calls"]]
        assert any("response" in call for call in calls) and any("error" in call for call in calls)


@pytest.mark.parametrize("registration,name", GOLDEN_CASES, ids=[f"{registration}-{name}" for registration, name in GOLDEN_CASES])
def test_game_requests_match_the_recorded_goldens(inputs, registration, name):
    outcome = golden_outcome(inputs, registration, CASES[registration][name])
    assert canonical(outcome) == canonical(recorded(registration)["cases"][name])


NOW = datetime(2026, 11, 3, 12, tzinfo=JST)
OPERATIONS = ("config.auto_select.set", "config.sort.set", "squads.save", "units.set_lock")
SQUAD_READBACK, UNIT_READBACK, CONFIG_READBACK = {"squads": [39, 72]}, {"units": [41]}, {"config": [36]}
SORT_WIRES, AUTO_SELECT_WIRES = ("F1", "F4", "F5"), ("F6", "FB")


@pytest.fixture
def world(inputs):
    return golden_world(*inputs)


def restarted(world: World) -> Callable[[dict], dict]:
    def initialize(state: dict) -> dict:
        world.bootstrap.table_rows(state, "config")
        return world.players.initialize(state)
    return initialize


@pytest.fixture
def services(world):
    return SimpleNamespace(bootstrap=world.bootstrap, player_state=world.players, current_squad=world.squads,
                           unit_actions=world.actions, initialize=restarted(world))


@pytest.fixture
def state(world):
    return golden_state(world)


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_squads.register(commands)
    return commands


def run(command, state: dict, raw: dict, services) -> tuple:
    context = CommandContext(now=NOW, acquisition_day=1, revision=0, generation_id="gen-squads", services=services)
    outcome = Batch((BatchStep(command, command.parse(raw)),)).apply(state, context)
    return outcome.state, outcome.steps[0]


def refused(command, state: dict, raw: dict, services) -> AdminError:
    before = deepcopy(state)
    with pytest.raises(AdminError) as caught:
        run(command, state, raw, services)
    assert state == before
    return caught.value


def console(services, state: dict, preflight) -> tuple:
    store = FakeStore(state)
    generation = FakeGeneration(id="gen-squads", bundle=services, content_versions={},
                                clock=instant(services.bootstrap.clock()))
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def commit(subject, plan):
    return subject.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=plan.expected_revision,
                          op_id=str(uuid.uuid4()), confirmations=())


def members(*slots: tuple[int, int, int | None]) -> list[dict]:
    return [{"team": team, "slot": slot, "get_index": index} for team, slot, index in slots]


def choice(index: int, locked: bool) -> dict:
    return {"get_index": index, "unique_id": index + 1, "locked": locked}


def rows_of(state: dict, group: int) -> list[dict]:
    return [row for row in player(state)["squads"] if squad_group(row) == group]


def flags_of(state: dict) -> dict[int, int]:
    return {unit["GetIndex"]: unit["GeneralPurposeFlags"] for unit in player(state)["units"]}


def test_squad_commands_register_under_their_preflight_domains(registry):
    catalog = discover(PREFLIGHT_DOMAINS)
    assert {command.operation for command in COMMANDS} <= {command.operation for command in catalog.commands}
    assert "admin_cmd_squads" in catalog.modules
    assert [command.operation for command in registry] == list(OPERATIONS)
    assert {command.operation: sorted(command.touched) for command in COMMANDS} == {
        "squads.save": ["squads"], "units.set_lock": ["units"], "config.sort.set": ["config"],
        "config.auto_select.set": ["config"]}
    assert not any(command.destructive or command.preflight_none for command in COMMANDS)
    assert {command.operation: command.domain for command in COMMANDS} == {
        "squads.save": "units", "units.set_lock": "units", "config.sort.set": "settings", "config.auto_select.set": "settings"}


def test_only_the_preference_commands_claim_configuration_fields(registry):
    assert SORT_WRITES == {"config._FlagSort", "config._FlagMissionFilter", "config.TypeOfSortOrder"}
    assert AUTO_SELECT_WRITES == {"config.AutoSelectConfig", "config.IntegrationAutoSelectConfig"}
    assert dict(registry.field_owners()) == {**dict.fromkeys(SORT_WRITES, SORT.operation),
                                             **dict.fromkeys(AUTO_SELECT_WRITES, AUTO_SELECT.operation)}
    # Several game flows write squad and unit rows, so neither command claims their fields.
    assert not SQUAD_SAVE.writes and not SET_LOCK.writes


@pytest.mark.parametrize("command,params", [
    (SQUAD_SAVE, {"group": 1}), (SQUAD_SAVE, {"group": 255, "select": True}), (SQUAD_SAVE, {"group": -1, "select": True}),
    (SQUAD_SAVE, {"group": 1, "select": 1}), (SQUAD_SAVE, {"group": 1, "select": True, "tutorial": 1}),
    (SQUAD_SAVE, {"group": 1, "members": []}), (SQUAD_SAVE, {"group": 1, "members": members((0, 16, None))}),
    (SQUAD_SAVE, {"group": 1, "members": members((256, 1, None))}), (SQUAD_SAVE, {"group": 1, "members": members((0, 1, -1))}),
    (SQUAD_SAVE, {"group": 1, "members": members((0, 1, None), (0, 1, FREE))}),
    (SQUAD_SAVE, {"group": 1, "members": members((0, 1, FREE), (0, 2, FREE))}),
    (SQUAD_SAVE, {"group": 1, "members": [{"team": 0, "slot": 1, "unit": FREE}]}),
    (SQUAD_SAVE, {"group": 1, "metadata": {"name": "n" * 129, "title": 0}}),
    (SQUAD_SAVE, {"group": 1, "metadata": {"name": "\x01", "title": 0}}),
    (SQUAD_SAVE, {"group": 1, "metadata": {"name": "名", "title": -1}}), (SQUAD_SAVE, {"group": 1, "metadata": {"name": "名"}}),
    (SET_LOCK, {"units": []}), (SET_LOCK, {"units": [{"get_index": FREE, "unique_id": FREE + 1}]}),
    (SET_LOCK, {"units": [{"get_index": FREE, "unique_id": FREE + 1, "locked": 1}]}),
    (SET_LOCK, {"units": [{"get_index": FREE, "unique_id": 0, "locked": True}]}),
    (SET_LOCK, {"units": [choice(FREE, True), choice(FREE, False)]}),
    (SET_LOCK, {"units": [choice(FREE, True), {**choice(LOCKED, False), "unique_id": FREE + 1}]}),
    (SORT, {"F1": "", "F4": ""}), (SORT, {"F1": "", "F4": "", "F5": "", "F2": ""}), (SORT, {"F1": 1, "F4": "", "F5": ""}),
    (SORT, {"F1": "猫" * 43, "F4": "", "F5": ""}), (SORT, {"F1": "", "F4": "1" * 32, "F5": ""}),
    (SORT, {"F1": "", "F4": "", "F5": "2" * 128}), (SORT, {"F1": "\uffff", "F4": "", "F5": ""}),
    (SORT, {"F1": "", "F4": "\x0b", "F5": ""}),
    (AUTO_SELECT, {"F6": 0}), (AUTO_SELECT, {"F6": 0x80000000, "FB": 0}), (AUTO_SELECT, {"F6": 0, "FB": -0x80000001}),
    (AUTO_SELECT, {"F6": True, "FB": 0}), (AUTO_SELECT, {"F6": 0, "FB": 0, "F7": 0}),
])
def test_parameters_are_strict(command, params):
    with pytest.raises(AdminError) as caught:
        command.parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_squad_save_sets_metadata_members_and_selection_in_one_step(state, services):
    raw = {"group": 1, "metadata": {"name": RENAMED, "title": 2}, "select": True,
           "members": members((0, 1, FREE), (0, 2, None), (1, 1, UNSHOWN_MEMBER))}
    updated, step = run(SQUAD_SAVE, state, raw, services)
    assert rows_of(updated, 1) == [squad_row(1, slot=1, unit=FREE, name=RENAMED, flags=2), squad_row(1, slot=2, name=RENAMED, flags=2),
                                   squad_row(1, team=1, slot=1, unit=UNSHOWN_MEMBER, name=RENAMED, flags=2)]
    assert player(updated)["battle"] == {**player(state)["battle"], "TN": 1}
    assert ([row for row in player(updated)["squads"] if squad_group(row) != 1]
            == [row for row in player(state)["squads"] if squad_group(row) != 1])
    # Console saves leave the 120 receipts and the TL tutorial bits to the client's own saves.
    assert HISTORY not in updated and configuration(updated) == configuration(state)
    assert step.effects["group"] == 1 and step.effects["column"] == "TN"
    assert step.effects["before"] == {"selected": False, "metadata": [{"name": SECOND, "title": 1}],
                                      "members": [{"team": 0, "slot": 1, "GetIndex": SIGNED}]}
    assert step.effects["after"] == {"selected": True, "metadata": [{"name": RENAMED, "title": 2}],
                                     "members": [{"team": 0, "slot": 1, "GetIndex": FREE}, {"team": 0, "slot": 2, "GetIndex": None},
                                                 {"team": 1, "slot": 1, "GetIndex": UNSHOWN_MEMBER}]}
    assert step.warnings == (f"Team 1 slot 1 holds GetIndex {UNSHOWN_MEMBER}, which the client leaves out",)


def test_metadata_and_selection_reuse_the_current_squad_kernel(world, state, services):
    updated, _ = run(SQUAD_SAVE, state, {"group": NEW_GROUP, "metadata": {"name": "第七隊", "title": 3}, "select": True}, services)
    assert canonical(updated) == canonical(world.squads.apply(state, Selection(NEW_GROUP, "第七隊", 3, False)))
    assert rows_of(updated, NEW_GROUP) == [squad_row(NEW_GROUP, slot=slot, name="第七隊", flags=3) for slot in range(1, 16)]


def test_members_reuse_the_squad_save_kernel_with_the_stored_metadata(world, state, services):
    updated, step = run(SQUAD_SAVE, state, {"group": 1, "members": members((0, 1, FREE), (1, 2, None))}, services)
    rows = [squad_row(1, slot=1, unit=FREE, name=SECOND, flags=1), squad_row(1, team=1, slot=2, name=SECOND, flags=1)]
    assert canonical(updated) == canonical(world.players.replace_squads(state, rows).state)
    assert step.effects["after"]["metadata"] == [{"name": SECOND, "title": 1}] and step.warnings == ()


def test_members_of_a_squad_without_one_stored_name_need_metadata(state, services):
    error = refused(SQUAD_SAVE, state, {"group": NEW_GROUP, "members": members((0, 1, FREE))}, services)
    assert (error.code, error.status) == (METADATA_REQUIRED, 422) and error.fields[0]["loc"] == ["metadata"]
    player(state)["squads"].append(squad_row(1, slot=2, name="別名", flags=1))
    assert refused(SQUAD_SAVE, state, {"group": 1, "members": members((0, 1, FREE))}, services).code == METADATA_REQUIRED
    updated, _ = run(SQUAD_SAVE, state, {"group": 1, "metadata": {"name": SECOND, "title": 1},
                                         "members": members((0, 1, FREE))}, services)
    assert rows_of(updated, 1) == [squad_row(1, slot=1, unit=FREE, name=SECOND, flags=1)]


@pytest.mark.parametrize("index,code,status", [
    (CONSUMED, "squad_unit_missing", 400), (HIGHEST, "squad_unit_missing", 400), (NEVER_ISSUED, "squad_unit_missing", 400),
    (STORED, "squad_unit_in_barracks", 409)])
def test_members_must_be_owned_units_of_the_first_barracks(state, services, index, code, status):
    error = refused(SQUAD_SAVE, state, {"group": 1, "members": members((0, 1, index))}, services)
    assert (error.code, error.status) == (code, status)


@pytest.mark.parametrize("raw", [{"group": 3, "select": True}, {"group": 3, "metadata": {"name": "移行", "title": 0}},
                                 {"group": 1, "members": members((0, 1, FREE))}], ids=["selection", "metadata", "members"])
def test_every_part_waits_for_the_pending_legacy_import(state, services, raw):
    legacy_without_group_zero(state)
    error = refused(SQUAD_SAVE, state, raw, services)
    assert (error.code, error.status) == ("current_squad_legacy_pending", 409)


def test_selection_alone_changes_only_the_current_team(state, services):
    updated, step = run(SQUAD_SAVE, state, {"group": LAST_TEAM, "select": True}, services)
    assert player(updated) == {**player(state), "battle": {**player(state)["battle"], "TN": LAST_TEAM}}
    assert step.effects["before"] == {"selected": False, "metadata": [], "members": []}
    assert step.effects["after"] == {"selected": True, "metadata": [], "members": []}
    raid, step = run(SQUAD_SAVE, state, {"group": RAID_GROUP, "select": True}, services)
    assert step.effects["column"] == "RTN" and raid == state


def test_selection_closes_a_legacy_import_that_has_nothing_to_move(state, services):
    pending_import(state)
    updated, _ = run(SQUAD_SAVE, state, {"group": 2, "select": True}, services)
    assert player(updated)["legacy_squad_imported"] is True and player(updated)["squads"] == []
    assert player(updated)["battle"]["TN"] == 2


@pytest.mark.parametrize("part", [{"select": True}, {"metadata": {"name": "名", "title": 0}},
                                  {"members": members((0, 1, FREE))}], ids=["selection", "metadata", "members"])
@pytest.mark.parametrize("group", [LAST_TEAM + 1, RAID_GROUP + 1, SIGNED_GROUP])
def test_squad_groups_follow_the_teams_of_the_current_client(state, services, group, part):
    error = refused(SQUAD_SAVE, state, {"group": group, **part}, services)
    assert (error.code, error.status) == ("invalid_request_fields", 400)


@pytest.mark.parametrize("metadata,code,status", [
    ({"name": "x" * 13, "title": 0}, "invalid_request_fields", 400), ({"name": "a\\b", "title": 0}, "invalid_request_fields", 400),
    ({"name": SECOND, "title": LOCKED_TITLE}, "emblem_locked", 409),
    ({"name": SECOND, "title": UNSUPPORTED_TITLE}, "emblem_unsupported", 501),
    ({"name": SECOND, "title": TITLE_COUNT}, "emblem_unknown", 400)])
def test_metadata_follows_the_native_name_and_title_rules(state, services, metadata, code, status):
    error = refused(SQUAD_SAVE, state, {"group": 1, "metadata": metadata}, services)
    assert (error.code, error.status) == (code, status)


def test_a_stored_long_name_can_be_kept(state, services):
    long_named_squad(state)
    updated, _ = run(SQUAD_SAVE, state, {"group": 2, "metadata": {"name": LONG_NAME, "title": 2}}, services)
    assert {(row["Name"], row["Flags"]) for row in rows_of(updated, 2)} == {(LONG_NAME, 2)}


def test_set_lock_applies_the_native_kernel_and_reports_each_unit(world, state, services):
    raw = {"units": [choice(FREE, True), choice(LOCKED, False), choice(SIGNED, True)]}
    updated, step = run(SET_LOCK, state, raw, services)
    assert canonical(updated) == canonical(world.actions.set_locks(state, {FREE: True, LOCKED: False, SIGNED: True}).state)
    flags = flags_of(updated)
    assert (flags[FREE], flags[LOCKED], flags[SIGNED]) == (1 | LOCK_FLAG, 1, SIGN_BIT | 0x101 | LOCK_FLAG)
    assert [(entry["unit"]["GetIndex"], entry["locked"]) for entry in step.effects["units"]] == [
        (FREE, {"before": False, "after": True}), (LOCKED, {"before": True, "after": False}),
        (SIGNED, {"before": False, "after": True})]


def test_set_lock_reaches_units_in_other_barracks_and_keeps_their_bits(state, services):
    updated, _ = run(SET_LOCK, state, {"units": [choice(STORED, True), choice(0, True)]}, services)
    assert (flags_of(updated)[STORED], flags_of(updated)[0]) == (1 | SECOND_BARRACKS | LOCK_FLAG, LOCK_FLAG)
    unchanged, step = run(SET_LOCK, updated, {"units": [choice(STORED, True)]}, services)
    assert unchanged == updated and step.effects["units"][0]["locked"] == {"before": True, "after": True}


@pytest.mark.parametrize("selection", [{"get_index": FREE, "unique_id": FREE}, {"get_index": CONSUMED, "unique_id": CONSUMED + 1},
                                       {"get_index": NEVER_ISSUED, "unique_id": NEVER_ISSUED + 1}],
                         ids=["stale_unique_id", "consumed", "never_issued"])
def test_set_lock_refuses_selections_that_miss_an_owned_unit(state, services, selection):
    error = refused(SET_LOCK, state, {"units": [choice(LOCKED, False), {**selection, "locked": True}]}, services)
    assert (error.code, error.status) == (LOCK_MISSING, 409)
    assert error.details["get_index"] == [selection["get_index"]]


def test_sort_set_applies_the_native_kernel_and_reports_changed_fields(world, state, services):
    values = {"F1": "0102", "F4": "", "F5": "1"}
    updated, step = run(SORT, state, values, services)
    assert canonical(updated) == canonical(world.actions.set_sort(state, values))
    assert step.effects["fields"] == [{"field": "_FlagSort", "wire": "F1", "before": "", "after": "0102"},
                                      {"field": "TypeOfSortOrder", "wire": "F5", "before": "", "after": "1"}]
    assert canonical(untracked(updated)) == canonical(untracked(state)) and player(updated) == player(state)


@pytest.mark.parametrize("values", [("1" * 127, "0" * 31, "2" * 127), ("猫" * 42, "猫" * 10, "")], ids=["ascii", "multibyte"])
def test_sort_set_accepts_the_longest_native_strings(state, services, values):
    updated, _ = run(SORT, state, dict(zip(SORT_WIRES, values)), services)
    row = configuration(updated)
    assert (row["_FlagSort"], row["_FlagMissionFilter"], row["TypeOfSortOrder"]) == values


def test_auto_select_set_replaces_both_values_with_every_bit(world, state, services):
    values = {"F6": -0x80000000, "FB": 0x7FFFFFFF}
    updated, step = run(AUTO_SELECT, state, values, services)
    assert canonical(updated) == canonical(world.actions.set_auto_select(state, values))
    assert (configuration(updated)["AutoSelectConfig"], configuration(updated)["IntegrationAutoSelectConfig"]) == (
        -0x80000000, 0x7FFFFFFF)
    assert [change["wire"] for change in step.effects["fields"]] == list(AUTO_SELECT_WIRES)
    unchanged, step = run(AUTO_SELECT, updated, values, services)
    assert unchanged == updated and step.effects["fields"] == []


@pytest.mark.parametrize("command,values", [(SORT, dict.fromkeys(SORT_WIRES, "")), (AUTO_SELECT, dict.fromkeys(AUTO_SELECT_WIRES, 0))],
                         ids=["sort", "auto_select"])
def test_preference_commands_refuse_a_corrupt_configuration(state, services, command, values):
    textual_volume(state)
    error = refused(command, state, values, services)
    assert (error.code, error.status) == ("invalid_bootstrap_value", 503)


@pytest.mark.parametrize("values", [dict.fromkeys(SORT_WIRES[:2], ""), dict.fromkeys((*SORT_WIRES, "F6"), ""),
                                    [("F1", "")], None], ids=["partial", "extra", "pairs", "none"])
def test_preference_kernels_replace_only_complete_field_groups(world, state, values):
    with pytest.raises(ValueError):
        world.actions.set_sort(state, values)
    with pytest.raises(ValueError):
        world.actions.set_auto_select(state, {"F6": 0})


@pytest.mark.parametrize("operation,params,readback", [
    ("squads.save", {"group": 1, "metadata": {"name": RENAMED, "title": 2}, "select": True}, SQUAD_READBACK),
    ("units.set_lock", {"units": [choice(FREE, True)]}, UNIT_READBACK),
    ("config.sort.set", {"F1": "0102", "F4": "", "F5": ""}, CONFIG_READBACK),
    ("config.auto_select.set", {"F6": 513, "FB": -1}, CONFIG_READBACK),
], ids=["squads", "locks", "sort", "auto_select"])
def test_commands_plan_and_commit_with_their_client_readback(services, state, preflight, registry, operation, params, readback):
    store, subject = console(services, state, preflight)
    plan = subject.preview(registry.batch([{"operation": operation, "params": params}]))
    assert plan.committable and plan.preflight["readback"] == readback and plan.restart_check == {"ok": True}
    outcome = commit(subject, plan)
    assert outcome.result["readback"] == readback and outcome.result["backup_before_commit"] is None
    assert store.revision == 1 and store.backups == []


def test_a_batch_of_squad_edits_backs_up_the_save_first(services, state, preflight, registry):
    store, subject = console(services, state, preflight)
    plan = subject.preview(registry.batch([
        {"operation": "squads.save", "params": {"group": NEW_GROUP, "metadata": {"name": "第七隊", "title": 0},
                                                "members": members((0, 1, FREE))}},
        {"operation": "units.set_lock", "params": {"units": [choice(FREE, True)]}}]))
    outcome = commit(subject, plan)
    assert len(store.backups) == 1 and store.backups[0]["commits_before"] == 0
    assert outcome.result["backup_before_commit"]["kind"] == AUTO_BACKUP_KIND
    assert outcome.result["readback"] == {**SQUAD_READBACK, **UNIT_READBACK} and store.revision == 1
    assert rows_of(store.state, NEW_GROUP) == [squad_row(NEW_GROUP, slot=1, unit=FREE, name="第七隊")]
    assert flags_of(store.state)[FREE] == 1 | LOCK_FLAG


def test_squad_saves_need_the_current_squad_service(services, state, preflight, registry):
    _, subject = console(SimpleNamespace(**{**vars(services), "current_squad": None}), state, preflight)
    with pytest.raises(AdminError) as caught:
        subject.preview(registry.batch([{"operation": "squads.save", "params": {"group": 1, "select": True}}]))
    assert (caught.value.code, caught.value.status) == ("admin_command_unavailable", 503)


def test_squad_addresses_round_trip_through_signed_int32():
    for group, team, slot in ((0, 0, 0), (1, 2, 15), (127, 255, 15), (128, 0, 1), (SIGNED_GROUP, 3, 7), (254, 255, 15)):
        row = {"Address": squad_address(group, team, slot)}
        assert row["Address"] == squad_row(group, team, slot)["Address"] and -(1 << 31) <= row["Address"] < 1 << 31
        assert (squad_group(row), squad_position(row)) == (group, (team, slot))


def test_current_squad_columns_follow_the_client_teams(world):
    assert [world.squads.column(group) for group in (0, LAST_TEAM, RAID_GROUP)] == ["TN", "TN", "RTN"]
    for group in (LAST_TEAM + 1, RAID_GROUP + 1, True, -1):
        with pytest.raises(ProtocolError) as caught:
            world.squads.column(group)
        assert caught.value.code == "invalid_request_fields"


@pytest.mark.parametrize("group", [LAST_TEAM + 1, RAID_GROUP + 1, SIGNED_GROUP])
def test_metadata_kernel_keeps_to_the_client_teams(world, state, group):
    with pytest.raises(ProtocolError) as caught:
        world.squads.set_metadata(state, group, RENAMED, 0)
    assert (caught.value.code, caught.value.status) == ("invalid_request_fields", 400)


def test_squad_replacement_empties_consumed_slots_only_for_the_client(world, state):
    rows = [squad_row(0, slot=1, unit=FREE, name=FIRST), squad_row(0, slot=2, unit=CONSUMED, name=FIRST)]
    replacement = world.players.replace_squads(state, rows)
    assert replacement.vacated == (CONSUMED,) and rows_of(replacement.state, 0)[1]["UnitIndex"] == EMPTY_SQUAD_SLOT
    with pytest.raises(ProtocolError) as caught:
        world.players.replace_squads(state, rows, vacate_consumed=False)
    assert (caught.value.code, caught.value.status) == ("squad_unit_missing", 400)


def test_legacy_import_kernel_leaves_an_imported_save_as_it_is(world, state):
    assert world.players.import_legacy_squads(state, LEGACY_ROWS) is state
    pending_import(state)
    imported = world.players.import_legacy_squads(state, LEGACY_ROWS)
    assert player(imported)["legacy_squad_imported"] is True and player(state)["legacy_squad_imported"] is False
    assert player(imported)["squads"] == [row for index, row in enumerate(LEGACY_ROWS) if index != 2]


def test_kernels_leave_their_input_untouched(world, state):
    before = deepcopy(state)
    world.players.replace_squads(state, REPLACED, tutorial=True)
    world.squads.set_metadata(state, 1, RENAMED, 2)
    world.squads.select(state, LAST_TEAM)
    world.squads.apply(state, Selection(LAST_TEAM, "最終隊", 0, True))
    world.actions.set_locks(state, {FREE: True})
    world.actions.set_sort(state, dict(zip(SORT_WIRES, SORT_VALUES)))
    world.actions.set_auto_select(state, dict(zip(AUTO_SELECT_WIRES, AUTO_SELECT_VALUES)))
    assert state == before


def test_live_squad_commands_pass_the_real_preflight_and_restart_check(bundle, preflight, registry):
    state = new_save(bundle)
    prince = state["player_state"]["units"][0]
    store = FakeStore(state)
    generation = FakeGeneration(id="gen-live-squads", bundle=bundle, content_versions={},
                                clock=instant(bundle.bootstrap.clock()))
    subject = CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                             backup_directory=Path("backups"))
    plan = subject.preview(registry.batch([
        {"operation": "squads.save", "params": {"group": LAST_TEAM, "metadata": {"name": "控制台", "title": 0}, "select": True,
                                                "members": members(*((0, slot, None) for slot in range(1, 16)))}},
        {"operation": "units.set_lock", "params": {"units": [{"get_index": prince["GetIndex"], "unique_id": prince["UniqueID"],
                                                              "locked": True}]}},
        {"operation": "config.sort.set", "params": {"F1": "0102", "F4": "01", "F5": "1"}},
        {"operation": "config.auto_select.set", "params": {"F6": 513, "FB": -1}}]))
    assert plan.committable and plan.preflight["readback"] == {**CONFIG_READBACK, **SQUAD_READBACK, **UNIT_READBACK}
    commit(subject, plan)
    assert store.revision == 1 and len(store.backups) == 1
    assert player(store.state)["battle"]["TN"] == LAST_TEAM and player(store.state)["legacy_squad_imported"] is True
    assert rows_of(store.state, LAST_TEAM) == [squad_row(LAST_TEAM, slot=slot, name="控制台") for slot in range(1, 16)]
    assert player(store.state)["units"][0]["GeneralPurposeFlags"] & LOCK_FLAG
    assert (configuration(store.state)["_FlagSort"], configuration(store.state)["AutoSelectConfig"]) == ("0102", 513)

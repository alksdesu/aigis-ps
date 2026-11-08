import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps import admin_cmd_display
from aigis_ps.admin_cmd_display import (CLEAR_EVENT_VIEW, COMMANDS, DISPLAY_TARGET_MISSING, EVENT_VIEW_WRITES,
                                        FILTER_WRITES, NOTHING_PENDING, SET_BLESSINGS, SET_DISPLAY, SET_EVENT_VIEW,
                                        VIEW_EVENT_GROUP)
from aigis_ps.admin_command import missing_service
from aigis_ps.admin_commit import InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.alxml import Column, Container
from aigis_ps.blessing_service import FILTER_FIELD, HISTORY_NAMESPACE as BLESSING_HISTORY
from aigis_ps.energy_clock import instant
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import DISPLAY_FIELD
from aigis_ps.request_ledger import HISTORY_LIMIT
from aigis_ps.storage import PlayerProfile, Store
from aigis_ps.unit_detail import HISTORY_NAMESPACE as UNIT_DETAIL_HISTORY
from aigis_ps.view_events import (EVENT_VIEW_FIELD, HISTORY_NAMESPACE as VIEW_EVENT_HISTORY, MAX_REQUEST_BYTES,
                                  NO_REQUEST)
from test_admin_cmd_units import commands, commit, console, preview_error, selector
from test_admin_commit import FakeGeneration, FakeRuntime, pipeline
from test_admin_seams import METHOD_KEY, PLAYER_ID, USER_KEY, MemoryStore, canonical
from test_quest_service import bundle, golden_bundle, request
from test_unit_removal import unit_at


GOLDEN = Path(__file__).resolve().parent / "golden" / "p21"
UNIT_DETAIL_GOLDEN, BLESSING_GOLDEN, VIEW_EVENT_GOLDEN = (
    GOLDEN / "unit-detail.json", GOLDEN / "blessings.json", GOLDEN / "view-events.json")
UNIT_DETAIL, BLESSING_SETTINGS, SAVE_VIEW, CLEAR_VIEW = 10, 148, 23, 24
FOREIGN_KEY = "c" * 32
STALE_VERSION = "0" * 64
SENTINEL = "sentinel"
EVENT_VIEW = "CardId=56,Id=1,Mode=0,Male=0"
EVENT_VIEW_PARAMS = {"CardId": 56, "Id": 1, "Mode": 0, "Male": 0}
UNKNOWN_EPISODE = "CardId=56,Id=4,Mode=0,Male=0"
BLESSINGS = ("battle_god_bless_001001", "battle_god_bless_001002", "battle_god_bless_001003")
FIRST, SECOND, THIRD = BLESSINGS
UNOWNED_BLESSING, OTHER_ITEM, OTHER_ITEM_COUNT = "battle_god_bless_001005", "gift_0002_ruby", 8
INVALID_FILTER = 9
# Card 29 has every face and an item skin, 416 a free skin with smartphone art, 22 both awakening branches.
SKIN_CARD, FREE_SKIN_CARD, AWAKENED_CARD = 29, 416, 22
SKIN_ITEM = "get_skin_00001"
SKIN_UNIT, FREE_SKIN_UNIT, AWAKENED_UNIT, MISSING_UNIT = 1, 2, 3, 99
BASE_UNITS = {SKIN_UNIT: (SKIN_CARD, 2), FREE_SKIN_UNIT: (FREE_SKIN_CARD, 0), AWAKENED_UNIT: (AWAKENED_CARD, 3)}
FIRST_FACE, EVOLVED_FACE, AWAKENED_FACE, SKIN_DISPLAY, UNKNOWN_DISPLAY = 1, 2, 3, 5, 9
# Smartphone art variants are the base display type plus 16.
SMARTPHONE = 16
OPERATIONS = ("units.set_display", "blessings.set", "view_event.set", "view_event.clear")
UNIT_READBACK, CONFIG_READBACK = {"units": [41]}, {"config": [36]}
BLESSING_READBACK = {"config": [36], "possessions": [129, 137]}
ENABLED = "Enabled"


@dataclass(frozen=True)
class Step:
    registration: int
    columns: tuple
    tick: int = 1000
    user_key: str = USER_KEY
    method_key: str = METHOD_KEY
    prepare: Callable[[dict], None] | None = None

    def envelope(self):
        profile = PlayerProfile(PLAYER_ID, self.user_key, self.method_key, {}, 0)
        return request(SimpleNamespace(profile=lambda: profile), self.registration, self.columns, tick=self.tick)


@dataclass(frozen=True)
class Case:
    steps: tuple[Step, ...]
    stored: Callable[[dict], None] | None = None


@dataclass(frozen=True)
class Slices:
    keys: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    config: tuple[str, ...] = ()
    units: bool = False

    def inside(self, start: dict, final: dict) -> dict:
        tables = final["tables"]
        result = {key: final.get(key) for key in self.keys} | {name: tables.get(name) for name in self.tables}
        if self.config:
            result["config"] = [{name: row.get(name) for name in self.config} for row in tables["config"]]
        if self.units:
            before = {unit["GetIndex"]: unit for unit in start["player_state"]["units"]}
            result["units"] = [unit for unit in final["player_state"]["units"] if before.get(unit["GetIndex"]) != unit]
        return result

    def outside(self, state: dict) -> dict:
        rest = {key: value for key, value in deepcopy(state).items() if key not in self.keys}
        for name in self.tables:
            rest["tables"].pop(name, None)
        for row in rest["tables"]["config"]:
            for name in self.config:
                row.pop(name, None)
        if self.units:
            rest["player_state"].pop("units")
        return rest


def outcome(save: Callable, base: dict, case: Case, slices: Slices) -> dict:
    start = deepcopy(base)
    if case.stored is not None:
        case.stored(start)
    store = MemoryStore(start)
    steps = []
    for step in case.steps:
        if step.prepare is not None:
            profile = store.profile()
            changed = deepcopy(profile.state)
            step.prepare(changed)
            store.update_state(changed, profile.revision)
        before, envelope = store.profile(), step.envelope()
        try:
            result = {"response": save(envelope, store).decode("utf-8")}
        except ProtocolError as error:
            result = {"error": {"code": error.code, "message": str(error), "status": error.status}}
        after = store.profile()
        assert slices.outside(after.state) == slices.outside(before.state)
        steps.append({**result, "request": envelope.xml.decode("utf-8"), "revisions": after.revision - before.revision})
    return {"steps": steps, "state": slices.inside(start, store.profile().state)}


@cache
def recorded(path: Path) -> dict:
    return json.loads(path.read_bytes())


def touch(state: dict) -> None:
    state[SENTINEL] = state.get(SENTINEL, 0) + 1


def without_config(state: dict) -> None:
    state["tables"]["config"] = []


def invalid_history(namespace: str) -> Callable[[dict], None]:
    def corrupt(state: dict) -> None:
        state[namespace] = {"schema_version": 3}

    return corrupt


def configured(name: str, value: object) -> Callable[[dict], None]:
    def store(state: dict) -> None:
        state["tables"]["config"][0][name] = value

    return store


def unsupported_player(state: dict) -> None:
    state["player_state"]["schema_version"] = 2


def f8(value: str) -> tuple:
    return (Column("F8", "S", (value,)),)


def view(value: str = EVENT_VIEW, **options) -> Step:
    return Step(SAVE_VIEW, f8(value), **options)


def clear(value: str = NO_REQUEST, **options) -> Step:
    return Step(CLEAR_VIEW, f8(value), **options)


VIEW_EVENT_SLICES = Slices(keys=(VIEW_EVENT_HISTORY,), config=(EVENT_VIEW_FIELD,))
VIEW_EVENT_CASES = {
    "save": Case((view(),)),
    "save_any_field_order": Case((view("Male=0,Mode=0,Id=1,CardId=56"),)),
    "save_third_episode_mode_one": Case((view("CardId=56,Id=3,Mode=1,Male=1"),)),
    "save_same_value_twice": Case((view(), view(tick=1001))),
    "replay": Case((view(), view())),
    "clear_saved": Case((view(), clear(tick=1001))),
    "clear_empty": Case((clear(),)),
    "clear_with_value": Case((clear(EVENT_VIEW),)),
    "unknown_episode": Case((view(UNKNOWN_EPISODE),)),
    "empty_save": Case((view(""),)),
    "overlong_save": Case((view("x" * (MAX_REQUEST_BYTES + 1)),)),
    "missing_value": Case((Step(SAVE_VIEW, ()),)),
    "duplicate_value": Case((Step(SAVE_VIEW, f8(EVENT_VIEW) * 2),)),
    "wrong_user_key": Case((view(user_key=FOREIGN_KEY),)),
    "wrong_method_key": Case((view(method_key=FOREIGN_KEY),)),
    "priority_request_before_identity": Case((view(UNKNOWN_EPISODE, user_key=FOREIGN_KEY),)),
    "priority_identity_before_state": Case((view(user_key=FOREIGN_KEY),), stored=without_config),
    "stored_config_missing": Case((view(),), stored=without_config),
    "stored_value_overlong": Case((clear(),), stored=configured(EVENT_VIEW_FIELD, "x" * (MAX_REQUEST_BYTES + 1))),
    "history_invalid": Case((view(),), stored=invalid_history(VIEW_EVENT_HISTORY)),
    "stale_after_change": Case((view(), view(prepare=touch))),
    "processed_after_eviction": Case((*(view(tick=1000 + offset) for offset in range(HISTORY_LIMIT + 1)), view())),
}


def bgb(*nodes) -> tuple:
    return (Container("BGB", tuple(nodes)),)


def fi(value: int) -> Column:
    return Column("FI", "I", (value,))


def names(*items: str) -> Column:
    return Column("ItemID", "S", items)


def flags(*values: int) -> Column:
    return Column("Enable", "I", values)


def settings(chosen: dict[str, int]) -> tuple[Column, Column]:
    return names(*chosen), flags(*chosen.values())


def bless(*nodes, **options) -> Step:
    return Step(BLESSING_SETTINGS, bgb(*nodes), **options)


def possession(item: str, **values) -> Callable[[dict], None]:
    def store(state: dict) -> None:
        rows = state["tables"]["possessions"]
        row = next((row for row in rows if row["ItemID"] == item), None)
        if row is None:
            rows.append({"ItemID": item, "Count": 0, **values})
        else:
            row.update(values)

    return store


BLESSING_SLICES = Slices(keys=(BLESSING_HISTORY,), tables=("possessions",), config=(FILTER_FIELD,))
BLESSING_CASES = {
    "filter_only": Case((bless(fi(2)),)),
    "enable_with_filter": Case((bless(fi(3), *settings({FIRST: 1, SECOND: 1})),)),
    "mixed_settings": Case((bless(fi(1), *settings({FIRST: 1, SECOND: 0})),), stored=possession(SECOND, Enabled=1)),
    "every_owned_blessing": Case((bless(*settings(dict.fromkeys(BLESSINGS, 1))),)),
    "noop_empty": Case((bless(),)),
    "noop_same_filter": Case((bless(fi(0)),)),
    "noop_paired_empty": Case((bless(names(), flags()),)),
    "noop_disable_unset": Case((bless(*settings({FIRST: 0})),)),
    "noop_repeat_after_change": Case((bless(*settings({FIRST: 1})), bless(*settings({FIRST: 1}), tick=1001))),
    "replay": Case((bless(*settings({FIRST: 1})), bless(*settings({FIRST: 1})))),
    "filter_out_of_range": Case((bless(fi(4)),)),
    "filter_negative": Case((bless(fi(-1)),)),
    "names_without_flags": Case((bless(names(FIRST)),)),
    "flag_two": Case((bless(*settings({FIRST: 2})),)),
    "duplicate_names": Case((bless(names(FIRST, FIRST), flags(1, 1)),)),
    "empty_name": Case((bless(names(""), flags(1)),)),
    "count_mismatch": Case((bless(names(FIRST, SECOND), flags(1)),)),
    "unknown_field": Case((bless(Column("XX", "I", (1,))),)),
    "missing_container": Case((Step(BLESSING_SETTINGS, ()),)),
    "unknown_item": Case((bless(*settings({OTHER_ITEM: 1})),)),
    "not_owned": Case((bless(*settings({UNOWNED_BLESSING: 1})),)),
    "zero_count": Case((bless(*settings({THIRD: 1})),), stored=possession(THIRD, Count=0)),
    "stored_filter_invalid": Case((bless(fi(1)),), stored=configured(FILTER_FIELD, INVALID_FILTER)),
    "wrong_user_key": Case((bless(fi(1), user_key=FOREIGN_KEY),)),
    "wrong_method_key": Case((bless(fi(1), method_key=FOREIGN_KEY),)),
    "priority_request_before_identity": Case((bless(fi(4), user_key=FOREIGN_KEY),)),
    "priority_ownership_before_history": Case((bless(*settings({UNOWNED_BLESSING: 1})),),
                                              stored=invalid_history(BLESSING_HISTORY)),
    "history_invalid": Case((bless(fi(1)),), stored=invalid_history(BLESSING_HISTORY)),
    "stale_after_change": Case((bless(*settings({FIRST: 1})), bless(*settings({FIRST: 1}), prepare=touch))),
    "processed_after_eviction": Case((*(bless(*settings({FIRST: (offset + 1) % 2}), tick=1000 + offset)
                                        for offset in range(HISTORY_LIMIT + 1)), bless(*settings({FIRST: 1})))),
}


def pu(index: int, display: int, toggle: int) -> tuple:
    columns = (Column("A7", "I", (index,)), Column("B0", "I", (display,)), Column("SC", "I", (toggle,)))
    return (Container("PU", columns),)


def detail(index: int = SKIN_UNIT, display: int = 0, toggle: int = 0, **options) -> Step:
    return Step(UNIT_DETAIL, pu(index, display, toggle), **options)


def displayed(index: int, display: int) -> Callable[[dict], None]:
    def store(state: dict) -> None:
        unit_at(state, index)[DISPLAY_FIELD] = display

    return store


UNIT_DETAIL_SLICES = Slices(keys=(UNIT_DETAIL_HISTORY,), units=True)
UNIT_DETAIL_CASES = {
    "default_display": Case((detail(),)),
    "first_face": Case((detail(display=FIRST_FACE),)),
    "evolved_face": Case((detail(display=EVOLVED_FACE),)),
    "smartphone_face": Case((detail(display=FIRST_FACE + SMARTPHONE),)),
    "smartphone_evolved_face": Case((detail(display=EVOLVED_FACE + SMARTPHONE),)),
    "awakened_face_before_awakening": Case((detail(display=AWAKENED_FACE),)),
    "awakened_face": Case((detail(AWAKENED_UNIT, AWAKENED_FACE),)),
    "owned_skin": Case((detail(display=SKIN_DISPLAY),), stored=possession(SKIN_ITEM, Count=1)),
    "unowned_skin": Case((detail(display=SKIN_DISPLAY),)),
    "skin_without_smartphone_art": Case((detail(display=SKIN_DISPLAY + SMARTPHONE),),
                                        stored=possession(SKIN_ITEM, Count=1)),
    "free_skin": Case((detail(FREE_SKIN_UNIT, SKIN_DISPLAY),)),
    "free_skin_smartphone_art": Case((detail(FREE_SKIN_UNIT, SKIN_DISPLAY + SMARTPHONE),)),
    "unknown_display": Case((detail(display=UNKNOWN_DISPLAY),)),
    "negative_display": Case((detail(display=-1),)),
    "kept_display": Case((detail(display=AWAKENED_FACE),), stored=displayed(SKIN_UNIT, AWAKENED_FACE)),
    "reset_display": Case((detail(display=0),), stored=displayed(SKIN_UNIT, EVOLVED_FACE)),
    "toggle_skill": Case((detail(toggle=1),)),
    "toggle_back": Case((detail(toggle=1), detail(toggle=1, tick=1001))),
    "toggle_with_display": Case((detail(display=FIRST_FACE, toggle=1),)),
    "toggle_awakened": Case((detail(AWAKENED_UNIT, toggle=1),)),
    "toggle_unevolved": Case((detail(FREE_SKIN_UNIT, toggle=1),)),
    "missing_unit": Case((detail(MISSING_UNIT),)),
    "negative_index": Case((detail(-1),)),
    "switch_two": Case((detail(toggle=2),)),
    "missing_switch": Case((Step(UNIT_DETAIL, (Container("PU", (Column("A7", "I", (SKIN_UNIT,)),
                                                                 Column("B0", "I", (0,)))),)),)),
    "wrong_user_key": Case((detail(user_key=FOREIGN_KEY),)),
    "wrong_method_key": Case((detail(method_key=FOREIGN_KEY),)),
    "priority_request_before_identity": Case((detail(-1, user_key=FOREIGN_KEY),)),
    "replay": Case((detail(display=FIRST_FACE), detail(display=FIRST_FACE))),
    "stale_after_change": Case((detail(display=FIRST_FACE), detail(display=FIRST_FACE, prepare=touch))),
    "history_invalid": Case((detail(),), stored=invalid_history(UNIT_DETAIL_HISTORY)),
    "priority_history_before_missing_unit": Case((detail(MISSING_UNIT),), stored=invalid_history(UNIT_DETAIL_HISTORY)),
    "processed_after_eviction": Case((*(detail(toggle=1, tick=1000 + offset) for offset in range(HISTORY_LIMIT + 1)),
                                      detail(toggle=1))),
    "stored_player_invalid": Case((detail(),), stored=unsupported_player),
}
GOLDEN_SETS = {
    UNIT_DETAIL_GOLDEN: ((UNIT_DETAIL,), UNIT_DETAIL_CASES, UNIT_DETAIL_SLICES),
    BLESSING_GOLDEN: ((BLESSING_SETTINGS,), BLESSING_CASES, BLESSING_SLICES),
    VIEW_EVENT_GOLDEN: ((SAVE_VIEW, CLEAR_VIEW), VIEW_EVENT_CASES, VIEW_EVENT_SLICES),
}


def golden_handlers(services) -> dict:
    return {UNIT_DETAIL_GOLDEN: services.unit_detail.save, BLESSING_GOLDEN: services.blessings.save,
            VIEW_EVENT_GOLDEN: services.view_events.save}


def test_golden_bases_hold_the_units_and_items_the_cases_name():
    units = recorded(UNIT_DETAIL_GOLDEN)["base_state"]
    assert {index: (unit_at(units, index)["CardID"], unit_at(units, index)["ClassChangeCount"])
            for index in BASE_UNITS} == BASE_UNITS
    assert MISSING_UNIT not in {unit["GetIndex"] for unit in units["player_state"]["units"]}
    stored = {row["ItemID"]: row["Count"] for row in recorded(BLESSING_GOLDEN)["base_state"]["tables"]["possessions"]}
    assert stored == {**dict.fromkeys(BLESSINGS, 1), OTHER_ITEM: OTHER_ITEM_COUNT} and UNOWNED_BLESSING not in stored


@pytest.mark.parametrize("path", GOLDEN_SETS, ids=lambda path: path.stem)
def test_goldens_cover_every_case_with_successes_and_refusals(path):
    registrations, cases, _ = GOLDEN_SETS[path]
    golden = recorded(path)
    assert golden["registrations"] == list(registrations) and golden["cases"].keys() == cases.keys()
    steps = [step for case in golden["cases"].values() for step in case["steps"]]
    assert any("response" in step for step in steps) and any("error" in step for step in steps)
    assert {step["error"]["status"] for step in steps if "error" in step} >= {400, 403, 409, 503}


@pytest.mark.parametrize("path, name", [(path, name) for path, (_, cases, _) in GOLDEN_SETS.items() for name in cases],
                         ids=lambda value: value.stem if isinstance(value, Path) else value)
def test_native_saves_match_the_recorded_golden_bytes(golden_bundle, path, name):
    _, cases, slices = GOLDEN_SETS[path]
    golden = recorded(path)
    result = outcome(golden_handlers(golden_bundle)[path], golden["base_state"], cases[name], slices)
    assert canonical(result) == canonical(golden["cases"][name])


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_display.register(commands)
    return commands


def live_save(services, path: Path) -> dict:
    return services.initialize(deepcopy(recorded(path)["base_state"]))


def without(state: dict, namespace: str) -> dict:
    return {key: value for key, value in state.items() if key != namespace}


def failure(error: ProtocolError) -> dict:
    return {"error": [error.code, str(error), error.status]}


def native_result(save: Callable, state: dict, step: Step, namespace: str) -> dict:
    store = MemoryStore(state)
    try:
        save(step.envelope(), store)
    except ProtocolError as error:
        return failure(error)
    return {"state": without(store.profile().state, namespace)}


def kernel_result(call: Callable[[dict], dict], state: dict) -> dict:
    before = deepcopy(state)
    try:
        result = {"state": call(state)}
    except ProtocolError as error:
        result = failure(error)
    assert state == before
    return result


def pending(state: dict) -> str:
    return state["tables"]["config"][0][EVENT_VIEW_FIELD]


def possessions_by_item(state: dict) -> dict:
    return {row["ItemID"]: row for row in state["tables"]["possessions"]}


def test_display_commands_register_against_the_preflight_table():
    catalog = discover(PREFLIGHT_DOMAINS)
    assert set(OPERATIONS) <= {command.operation for command in catalog.commands}
    assert "admin_cmd_display" in catalog.modules
    assert [(command.operation, command.domain, sorted(command.touched)) for command in COMMANDS] == [
        ("units.set_display", "units", ["units"]), ("blessings.set", "settings", ["config", "possessions"]),
        ("view_event.set", "settings", ["config"]), ("view_event.clear", "settings", ["config"])]
    assert not any(command.destructive or command.preflight_none for command in COMMANDS)
    owners = catalog.commands.field_owners()
    assert {field: owners[field] for field in FILTER_WRITES | EVENT_VIEW_WRITES} == {
        f"config.{FILTER_FIELD}": "blessings.set", f"config.{EVENT_VIEW_FIELD}": "view_event.set"}
    # Unit rows belong to many game flows, so the display command claims none of their fields.
    assert not SET_DISPLAY.writes
    assert SET_EVENT_VIEW.writes == CLEAR_EVENT_VIEW.writes == EVENT_VIEW_WRITES
    assert SET_EVENT_VIEW.group == CLEAR_EVENT_VIEW.group == VIEW_EVENT_GROUP


def test_both_event_view_commands_write_the_request_and_commands_outside_their_group_cannot():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    commands.add(SET_EVENT_VIEW)
    commands.add(CLEAR_EVENT_VIEW)
    assert dict(commands.field_owners()) == {f"config.{EVENT_VIEW_FIELD}": SET_EVENT_VIEW.operation}
    with pytest.raises(ValueError, match=SET_EVENT_VIEW.operation):
        commands.add(replace(CLEAR_EVENT_VIEW, operation="view_events.clear", group=None))


@pytest.mark.parametrize("command, params", [
    (SET_DISPLAY, {"get_index": 1, "unique_id": 2}), (SET_DISPLAY, {"get_index": 1, "unique_id": 2, "display": -1}),
    (SET_DISPLAY, {"get_index": 1, "unique_id": 2, "display": 1.0}),
    (SET_DISPLAY, {"get_index": 1, "unique_id": 2, "display": 1, "SC": 1}),
    (SET_DISPLAY, {"get_index": 1, "unique_id": 0, "display": 1}),
    (SET_BLESSINGS, {}), (SET_BLESSINGS, {"FI": True}), (SET_BLESSINGS, {"FI": "1"}),
    (SET_BLESSINGS, {"settings": [{"ItemID": FIRST, "Enable": 1}, {"ItemID": FIRST, "Enable": 0}]}),
    (SET_BLESSINGS, {"settings": [{"ItemID": "", "Enable": 1}]}), (SET_BLESSINGS, {"settings": [{"ItemID": FIRST}]}),
    (SET_BLESSINGS, {"settings": [{"ItemID": FIRST, "Enable": False}]}),
    (SET_EVENT_VIEW, {"CardId": 56, "Id": 1, "Mode": 0}), (SET_EVENT_VIEW, {**EVENT_VIEW_PARAMS, "Id": "1"}),
    (SET_EVENT_VIEW, {**EVENT_VIEW_PARAMS, "F8": EVENT_VIEW}), (CLEAR_EVENT_VIEW, {"F8": NO_REQUEST}),
])
def test_parameters_are_strict(command, params):
    with pytest.raises(ProtocolError) as caught:
        command.parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_event_view_parameters_compose_the_native_text_in_key_order():
    assert admin_cmd_display.request_text(SET_EVENT_VIEW.parse(dict(reversed(EVENT_VIEW_PARAMS.items())))) == EVENT_VIEW


def test_display_plans_and_commits_only_the_absolute_display(bundle, preflight, registry):
    state = live_save(bundle, UNIT_DETAIL_GOLDEN)
    unit = unit_at(state, SKIN_UNIT)
    store, subject = console(bundle, state, preflight)
    plan = subject.preview(commands(registry, ("units.set_display", {**selector(unit), "display": EVOLVED_FACE})))
    assert plan.committable and plan.preflight["readback"] == UNIT_READBACK
    assert plan.steps[0].effects["display"] == {"before": unit[DISPLAY_FIELD], "after": EVOLVED_FACE}
    assert plan.steps[0].effects["unit"]["UniqueID"] == unit["UniqueID"]
    outcome = commit(subject, plan)
    assert outcome.result["readback"] == UNIT_READBACK and outcome.result["backup_before_commit"] is None
    assert unit_at(store.state, SKIN_UNIT) == {**unit, DISPLAY_FIELD: EVOLVED_FACE}
    assert UNIT_DETAIL_HISTORY not in store.state and store.revision == 1


DISPLAY_CHOICES = {
    "default": (SKIN_UNIT, 0, None),
    "first_face": (SKIN_UNIT, FIRST_FACE, None),
    "evolved_face": (SKIN_UNIT, EVOLVED_FACE, None),
    "smartphone_face": (SKIN_UNIT, FIRST_FACE + SMARTPHONE, None),
    "smartphone_evolved_face": (SKIN_UNIT, EVOLVED_FACE + SMARTPHONE, None),
    "awakened_face_before_awakening": (SKIN_UNIT, AWAKENED_FACE, None),
    "awakened_face": (AWAKENED_UNIT, AWAKENED_FACE, None),
    "unowned_skin": (SKIN_UNIT, SKIN_DISPLAY, None),
    "owned_skin": (SKIN_UNIT, SKIN_DISPLAY, possession(SKIN_ITEM, Count=1)),
    "skin_without_smartphone_art": (SKIN_UNIT, SKIN_DISPLAY + SMARTPHONE, possession(SKIN_ITEM, Count=1)),
    "free_skin": (FREE_SKIN_UNIT, SKIN_DISPLAY, None),
    "free_skin_smartphone_art": (FREE_SKIN_UNIT, SKIN_DISPLAY + SMARTPHONE, None),
    "unknown_display": (SKIN_UNIT, UNKNOWN_DISPLAY, None),
    "kept_display": (SKIN_UNIT, AWAKENED_FACE, displayed(SKIN_UNIT, AWAKENED_FACE)),
    "missing_unit": (MISSING_UNIT, 0, None),
    "invalid_player": (SKIN_UNIT, FIRST_FACE, unsupported_player),
}


@pytest.mark.parametrize("name", DISPLAY_CHOICES)
def test_display_kernel_matches_the_native_save_without_its_receipt(bundle, name):
    index, display, stored = DISPLAY_CHOICES[name]
    state = live_save(bundle, UNIT_DETAIL_GOLDEN)
    if stored is not None:
        stored(state)
    expected = native_result(bundle.unit_detail.save, state, detail(index, display), UNIT_DETAIL_HISTORY)
    result = kernel_result(lambda current: bundle.unit_detail.set_display(current, index, display), state)
    assert canonical(result) == canonical(expected)


@pytest.mark.parametrize("index, display", [(-1, 0), ("1", 0), (SKIN_UNIT, True), (SKIN_UNIT, 1 << 31)])
def test_display_kernel_requires_native_selection_values(bundle, index, display):
    with pytest.raises(ValueError):
        bundle.unit_detail.set_display(live_save(bundle, UNIT_DETAIL_GOLDEN), index, display)


@pytest.mark.parametrize("change", [{"unique_id": 99}, {"get_index": MISSING_UNIT}], ids=["unique_id", "get_index"])
def test_display_refuses_a_target_other_than_the_owned_unit(bundle, preflight, registry, change):
    state = live_save(bundle, UNIT_DETAIL_GOLDEN)
    _, subject = console(bundle, state, preflight)
    params = {**selector(unit_at(state, SKIN_UNIT)), "display": FIRST_FACE, **change}
    error = preview_error(subject, commands(registry, ("units.set_display", params)), DISPLAY_TARGET_MISSING, 409)
    assert error.details == {"step": 0, "operation": "units.set_display"}


def test_display_refusals_keep_the_native_code(bundle, preflight, registry):
    state = live_save(bundle, UNIT_DETAIL_GOLDEN)
    _, subject = console(bundle, state, preflight)
    params = {**selector(unit_at(state, SKIN_UNIT)), "display": AWAKENED_FACE}
    preview_error(subject, commands(registry, ("units.set_display", params)), "unit_detail_display_unsupported", 501)


def test_blessings_plan_and_commit_the_filter_and_enabled_flags(bundle, preflight, registry):
    store, subject = console(bundle, live_save(bundle, BLESSING_GOLDEN), preflight)
    params = {"FI": 2, "settings": [{"ItemID": FIRST, "Enable": 1}, {"ItemID": SECOND, "Enable": 0}]}
    plan = subject.preview(commands(registry, ("blessings.set", params)))
    assert plan.committable and plan.preflight["readback"] == BLESSING_READBACK
    assert plan.steps[0].effects == {"filter": {"before": 0, "after": 2}, "settings": [
        {"ItemID": FIRST, "before": 0, "after": 1}, {"ItemID": SECOND, "before": 0, "after": 0}]}
    commit(subject, plan)
    saved = possessions_by_item(store.state)
    assert store.state["tables"]["config"][0][FILTER_FIELD] == 2
    assert saved[FIRST][ENABLED] == 1 and ENABLED not in saved[SECOND]
    assert BLESSING_HISTORY not in store.state and store.revision == 1


def test_unchanged_blessings_commit_an_audit_without_a_revision(bundle, preflight, registry):
    store, subject = console(bundle, live_save(bundle, BLESSING_GOLDEN), preflight)
    unchanged = {"FI": 0, "settings": [{"ItemID": FIRST, "Enable": 0}]}
    plan = subject.preview(commands(registry, ("blessings.set", unchanged)))
    outcome = commit(subject, plan)
    assert outcome.result["revision_before"] == outcome.result["revision_after"] == store.revision == 0


BLESSING_CHOICES = {
    "filter": (2, {}), "enable": (None, {FIRST: 1, SECOND: 1}), "mixed": (3, {FIRST: 1, SECOND: 0}),
    "unchanged": (0, {FIRST: 0}), "filter_out_of_range": (4, {}), "filter_negative": (-1, {FIRST: 1}),
    "flag_two": (None, {FIRST: 2}), "empty_name": (None, {"": 1}),
    "unknown_item": (None, {OTHER_ITEM: 1}), "not_owned": (1, {UNOWNED_BLESSING: 1}),
}


@pytest.mark.parametrize("stored", [None, configured(FILTER_FIELD, INVALID_FILTER), unsupported_player],
                         ids=["valid", "stored_filter_invalid", "invalid_player"])
@pytest.mark.parametrize("name", BLESSING_CHOICES)
def test_blessing_kernel_matches_the_native_save_without_its_receipt(bundle, name, stored):
    selected_filter, chosen = BLESSING_CHOICES[name]
    state = live_save(bundle, BLESSING_GOLDEN)
    if stored is not None:
        stored(state)
    nodes = ((fi(selected_filter),) if selected_filter is not None else ()) + (settings(chosen) if chosen else ())
    expected = native_result(bundle.blessings.save, state, bless(*nodes), BLESSING_HISTORY)
    result = kernel_result(lambda current: bundle.blessings.set_settings(current, selected_filter, chosen), state)
    assert canonical(result) == canonical(expected)


@pytest.mark.parametrize("selected_filter, chosen", [
    (True, {}), ("1", {}), (None, {FIRST: True}), (None, {1: 1}), (None, [(FIRST, 1)]),
])
def test_blessing_kernel_refuses_values_the_native_parser_never_produces(bundle, selected_filter, chosen):
    state = live_save(bundle, BLESSING_GOLDEN)
    result = kernel_result(lambda current: bundle.blessings.set_settings(current, selected_filter, chosen), state)
    assert (result["error"][0], result["error"][2]) == ("invalid_request_fields", 400)


@pytest.mark.parametrize("params, code, status", [
    ({"FI": 4}, "invalid_request_fields", 400),
    ({"settings": [{"ItemID": OTHER_ITEM, "Enable": 1}]}, "blessing_item_unknown", 400),
    ({"settings": [{"ItemID": UNOWNED_BLESSING, "Enable": 1}]}, "blessing_not_owned", 409),
    ({"settings": [{"ItemID": FIRST, "Enable": 2}]}, "invalid_request_fields", 400),
])
def test_blessing_refusals_keep_the_kernel_codes(bundle, preflight, registry, params, code, status):
    _, subject = console(bundle, live_save(bundle, BLESSING_GOLDEN), preflight)
    preview_error(subject, commands(registry, ("blessings.set", params)), code, status)


def test_event_view_commands_set_and_clear_the_pointer_without_receipts(bundle, preflight, registry):
    store, subject = console(bundle, live_save(bundle, VIEW_EVENT_GOLDEN), preflight)
    plan = subject.preview(commands(registry, ("view_event.set", EVENT_VIEW_PARAMS)))
    assert plan.committable and plan.preflight["readback"] == CONFIG_READBACK
    assert plan.steps[0].effects == {"before": NO_REQUEST, "after": EVENT_VIEW}
    commit(subject, plan)
    assert pending(store.state) == EVENT_VIEW and VIEW_EVENT_HISTORY not in store.state and store.revision == 1
    plan = subject.preview(commands(registry, ("view_event.clear", {})))
    assert plan.steps[0].effects == {"before": EVENT_VIEW, "after": NO_REQUEST} and not plan.steps[0].warnings
    commit(subject, plan)
    assert pending(store.state) == NO_REQUEST and store.revision == 2
    plan = subject.preview(commands(registry, ("view_event.clear", {})))
    assert plan.steps[0].warnings == (NOTHING_PENDING,)
    assert commit(subject, plan).result["revision_after"] == store.revision == 2


def test_event_view_refusals_keep_the_native_code(bundle, preflight, registry):
    _, subject = console(bundle, live_save(bundle, VIEW_EVENT_GOLDEN), preflight)
    preview_error(subject, commands(registry, ("view_event.set", {**EVENT_VIEW_PARAMS, "Id": 4})),
                  "view_event_request_invalid", 400)


EVENT_VIEW_CHOICES = {
    "save": view(), "third_episode_mode_one": view("CardId=56,Id=3,Mode=1,Male=1"),
    "unknown_episode": view(UNKNOWN_EPISODE), "empty_save": view(""), "clear": clear(),
}


def view_kernel(services, step: Step) -> Callable[[dict], dict]:
    if step.registration == CLEAR_VIEW:
        return services.view_events.clear_request
    value = step.columns[0].values[0]
    return lambda state: services.view_events.set_request(state, value)


@pytest.mark.parametrize("stored", [None, configured(EVENT_VIEW_FIELD, EVENT_VIEW), without_config],
                         ids=["no_request", "pending_request", "config_missing"])
@pytest.mark.parametrize("name", EVENT_VIEW_CHOICES)
def test_event_view_kernels_match_the_native_saves_without_receipts(bundle, name, stored):
    state = live_save(bundle, VIEW_EVENT_GOLDEN)
    if stored is not None:
        stored(state)
    step = EVENT_VIEW_CHOICES[name]
    expected = native_result(bundle.view_events.save, state, step, VIEW_EVENT_HISTORY)
    assert canonical(kernel_result(view_kernel(bundle, step), state)) == canonical(expected)


def blessing_kernel(services, step: Step) -> Callable[[dict], dict]:
    supplied = {column.name: column.values for column in step.columns[0].children}
    selected_filter = supplied["FI"][0] if "FI" in supplied else None
    chosen = dict(zip(supplied.get("ItemID", ()), supplied.get("Enable", ())))
    return lambda state: services.blessings.set_settings(state, selected_filter, chosen)


UNREADY_CHOICES = {
    "view_events": (VIEW_EVENT_GOLDEN, view(UNKNOWN_EPISODE), VIEW_EVENT_HISTORY, view_kernel),
    "blessings": (BLESSING_GOLDEN, bless(fi(INVALID_FILTER), *settings({FIRST: 2})), BLESSING_HISTORY, blessing_kernel),
}


@pytest.mark.parametrize("service", UNREADY_CHOICES)
def test_kernels_check_their_dependencies_before_the_request_values(bundle, monkeypatch, service):
    path, step, namespace, kernel = UNREADY_CHOICES[service]
    state = live_save(bundle, path)
    monkeypatch.setattr(getattr(bundle, service), "content_version", STALE_VERSION)
    expected = native_result(golden_handlers(bundle)[path], state, step, namespace)
    assert expected["error"][2] == 503
    assert canonical(kernel_result(kernel(bundle, step), state)) == canonical(expected)


def test_a_console_write_turns_an_earlier_game_receipt_stale(bundle, preflight, registry, tmp_path):
    store = Store(tmp_path / "save.sqlite3", live_save(bundle, VIEW_EVENT_GOLDEN))
    envelope = request(store, SAVE_VIEW, f8(EVENT_VIEW))
    bundle.view_events.save(envelope, store)
    generation = FakeGeneration(id="gen-display", bundle=bundle, clock=instant(bundle.bootstrap.clock()))
    subject, *_ = pipeline(store=store, runtime=FakeRuntime(generation), preflight=preflight,
                           restart_check=InitializerRestartCheck())
    commit(subject, subject.preview(commands(registry, ("view_event.clear", {}))))
    saved = store.profile()
    assert pending(saved.state) == NO_REQUEST and saved.revision == 2
    assert [receipt["value"] for receipt in saved.state[VIEW_EVENT_HISTORY]["receipts"]] == [EVENT_VIEW]
    with pytest.raises(ProtocolError) as caught:
        bundle.view_events.save(envelope, store)
    assert (caught.value.code, caught.value.status) == ("view_event_retry_stale", 409)


@pytest.mark.parametrize("operation, service, params", [
    ("units.set_display", "unit_detail", {"get_index": SKIN_UNIT, "unique_id": 2, "display": FIRST_FACE}),
    ("blessings.set", "blessings", {"FI": 1}), ("view_event.set", "view_events", EVENT_VIEW_PARAMS),
    ("view_event.clear", "view_events", {}),
])
def test_commands_need_their_game_service(bundle, preflight, registry, operation, service, params):
    _, subject = console(replace(bundle, **{service: None}), live_save(bundle, UNIT_DETAIL_GOLDEN), preflight)
    error = preview_error(subject, commands(registry, (operation, params)), "admin_command_unavailable", 503)
    assert str(error) == missing_service(service)

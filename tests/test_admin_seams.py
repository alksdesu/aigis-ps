import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.bootstrap import (MULTI_ROW_TABLES, STARTUP_COLLECTION_LIMITS, STARTUP_COLLECTIONS, TABLES, BootstrapService,
                                _RequestIntegers, configuration_updates)
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import (MAX_SQUAD_NAME_BYTES, MAX_SQUAD_ROWS, MAX_SQUAD_SLOT, RESERVED_SQUAD_GROUP,
                                   PlayerStateService, squad_address)
from aigis_ps.protocol import ProtocolRegistry, RequestEnvelope
from aigis_ps.request_fields import COMMON_FIELDS
from aigis_ps.storage import PlayerProfile, serialize
from test_bootstrap import asset_root, request
from test_player_state import assets, master, row as squad_row


GOLDEN = Path(__file__).resolve().parent / "golden" / "p03a"
CONFIGURATION_GOLDEN = GOLDEN / "configuration-save.json"
CONFIGURATION_REGISTRATIONS = (21, 22)
PROFILES = ProtocolRegistry.load().by_registration
SAVED_AT = datetime(2026, 10, 31, 12, 30)
PLAYER_ID, USER_KEY, METHOD_KEY = "1", "a" * 32, "b" * 32
SOUND_OPTIONS = {"A2": 0, "A3": 1, "C2": 37, "C3": 72, "F9": 1, "FA": 0, "FD": 1}
OPTION_GROUP_ERROR = ("invalid_configuration_request", "Expected tutorial flags or the complete sound option group", 400)


def field(tag: str, value: object, kind: str = "I") -> str:
    return f'<{tag} T="{kind}"><V>{value}</V></{tag}>'


IDENTITY = (("UI", PLAYER_ID), ("UK", USER_KEY), ("MK", METHOD_KEY))
HEADER = (field("CT", 0), field("PFN", "dmm-r18", "S"), field("PlatformCode", 1),
          field("GPVI", "unkown", "S"), field("GPOT", "unkown", "S"))
TRANSPORT = ("<VN>5.0.0</VN>", "<ST>3023</ST>", "<HS>0</HS>")
STRING_C2 = '<C2 T="S"><V>37</V></C2>'


def sound_fields(**changes: object) -> tuple[str, ...]:
    return tuple(field(tag, value) for tag, value in {**SOUND_OPTIONS, **changes}.items())


def missing_config_row(state: dict) -> None:
    state["tables"]["config"] = []


def incomplete_config_row(state: dict) -> None:
    del state["tables"]["config"][0]["UseSe"]


def unsupported_bootstrap_schema(state: dict) -> None:
    state["bootstrap_schema"] = 2


def textual_volume(state: dict) -> None:
    state["tables"]["config"][0]["_VolumeBgm"] = "80"


def integer_switch(state: dict) -> None:
    state["tables"]["config"][0]["UseBgm"] = 1


def oversized_user_name(state: dict) -> None:
    state["tables"]["config"][0]["UserName"] = "n" * 40


@dataclass(frozen=True)
class SaveCase:
    fields: tuple[str, ...]
    identity: tuple[tuple[str, str], ...] = IDENTITY
    opening: str = "<DA>"
    stored: tuple[tuple[str, object], ...] = ()
    corrupt: Callable[[dict], None] | None = None

    def xml(self) -> str:
        return self.opening + "".join((*(field(tag, value, "S") for tag, value in self.identity), *self.fields)) + "</DA>"

    def envelope(self, registration: int) -> RequestEnvelope:
        payload = self.xml().encode("utf-8")
        return RequestEnvelope(PROFILES[registration], payload, ET.fromstring(payload), sha256(payload).hexdigest())


SAVE_CASES = {
    "tutorial_flags": SaveCase((field("A4", 7),)),
    "tutorial_flags_zero": SaveCase((field("A4", 0),)),
    "tutorial_flags_minimum": SaveCase((field("A4", -0x80000000),)),
    "tutorial_flags_maximum": SaveCase((field("A4", 0x7FFFFFFF),)),
    "tutorial_flags_plus_sign": SaveCase((field("A4", "+7"),)),
    "tutorial_flags_leading_zeros": SaveCase((field("A4", "00000000007"),)),
    "tutorial_flags_negative_zero": SaveCase((field("A4", "-0"),)),
    "tutorial_flags_with_header": SaveCase((*HEADER, field("A4", 5), *TRANSPORT)),
    "tutorial_flags_pretty_printed": SaveCase(("\n  " + field("A4", 3) + "\n",), opening="<DA>\n  "),
    "tutorial_flags_keep_sound": SaveCase((field("A4", 3),), stored=(
        ("UseBgm", False), ("_VolumeBgm", 85), ("_VolumeSe", 15), ("BattlePlaceMagnetFlag", 1),
        ("BattleAcceleratorFlag", 2), ("BattleDClickSkillDisableFlag", -1))),
    "sound_options": SaveCase(sound_fields()),
    "sound_options_switched": SaveCase(sound_fields(A2=1, A3=0, C2=-0x80000000, C3=0x7FFFFFFF, F9=-1, FA=2, FD=0)),
    "sound_options_reordered": SaveCase(tuple(reversed(sound_fields()))),
    "sound_options_keep_tutorial": SaveCase(sound_fields(), stored=(("MenuTutorialFlags", 0x40000007),)),
    "sound_options_with_header": SaveCase((*HEADER, *sound_fields(), *TRANSPORT)),
    "empty": SaveCase(()),
    "tutorial_with_unknown": SaveCase((field("A4", 1), field("A5", 20))),
    "tutorial_with_sound": SaveCase((field("A4", 1), *sound_fields())),
    "unknown_only": SaveCase((field("A5", 20),)),
    "sound_partial": SaveCase((field("C2", 5),)),
    "sound_missing_field": SaveCase(sound_fields()[:-1]),
    "sound_with_unknown": SaveCase((*sound_fields(), field("F7", 1))),
    "duplicate_tutorial": SaveCase((field("A4", 1), field("A4", 1))),
    "duplicate_sound_field": SaveCase((*sound_fields(), field("C2", 5))),
    "tutorial_string_type": SaveCase((field("A4", 1, "S"),)),
    "tutorial_boolean_type": SaveCase(('<A4 T="B"><V>true</V></A4>',)),
    "tutorial_untyped": SaveCase(("<A4><V>1</V></A4>",)),
    "tutorial_extra_attribute": SaveCase(('<A4 T="I" N="x"><V>1</V></A4>',)),
    "tutorial_two_values": SaveCase(('<A4 T="I"><V>1</V><V>2</V></A4>',)),
    "tutorial_no_value": SaveCase(('<A4 T="I"></A4>',)),
    "tutorial_text_before_value": SaveCase(('<A4 T="I">extra<V>1</V></A4>',)),
    "tutorial_trailing_text": SaveCase(('<A4 T="I"><V>1</V></A4>tail',)),
    "tutorial_value_tail": SaveCase(('<A4 T="I"><V>1</V>extra</A4>',)),
    "tutorial_nested_value": SaveCase(('<A4 T="I"><V><N>1</N></V></A4>',)),
    "tutorial_value_attribute": SaveCase(('<A4 T="I"><V extra="x">1</V></A4>',)),
    "tutorial_wrong_value_tag": SaveCase(('<A4 T="I"><Value>1</Value></A4>',)),
    "tutorial_empty_value": SaveCase(('<A4 T="I"><V></V></A4>',)),
    "tutorial_spaced_value": SaveCase(('<A4 T="I"><V> 7</V></A4>',)),
    "tutorial_long_text": SaveCase((field("A4", "+00000000007"),)),
    "tutorial_unicode_digit": SaveCase((field("A4", "٧"),)),
    "tutorial_hex_text": SaveCase((field("A4", "0x7"),)),
    "tutorial_decimal_text": SaveCase((field("A4", "7.0"),)),
    "tutorial_overflow": SaveCase((field("A4", 0x80000000),)),
    "tutorial_underflow": SaveCase((field("A4", -0x80000001),)),
    "sound_bgm_two": SaveCase(sound_fields(A2=2)),
    "sound_se_negative": SaveCase(sound_fields(A3=-1)),
    "sound_volume_overflow": SaveCase(sound_fields(C2=0x80000000)),
    "sound_flag_underflow": SaveCase(sound_fields(FD=-0x80000001)),
    "priority_group_before_format": SaveCase((field("A4", 1, "S"), field("A5", 20))),
    "priority_range_before_later_format": SaveCase(tuple(STRING_C2 if item.startswith("<C2") else item
                                                         for item in sound_fields(A2=2))),
    "priority_format_before_later_range": SaveCase((STRING_C2, *(item for item in sound_fields(A2=2)
                                                                 if not item.startswith("<C2")))),
    "priority_first_range_wins": SaveCase(sound_fields(A2=2, C3=0x80000000)),
    "priority_first_range_wins_reversed": SaveCase(tuple(reversed(sound_fields(A2=2, C3=0x80000000)))),
    "priority_identity_before_request": SaveCase((), identity=(("UI", PLAYER_ID), ("UK", "c" * 32), ("MK", METHOD_KEY))),
    "priority_method_key_before_request": SaveCase((field("A5", 20),),
                                                   identity=(("UI", PLAYER_ID), ("UK", USER_KEY), ("MK", "c" * 32))),
    "priority_request_before_state": SaveCase((field("A4", 1, "S"),), corrupt=missing_config_row),
    "priority_root_before_group": SaveCase((), opening='<DA T="I">'),
    "root_attribute": SaveCase((field("A4", 1),), opening='<DA N="x">'),
    "root_text": SaveCase((field("A4", 1),), opening="<DA>stray"),
    "wrong_player": SaveCase((field("A4", 1),), identity=(("UI", "2"), ("UK", USER_KEY), ("MK", METHOD_KEY))),
    "wrong_user_key": SaveCase((field("A4", 1),), identity=(("UI", PLAYER_ID), ("UK", "c" * 32), ("MK", METHOD_KEY))),
    "wrong_method_key": SaveCase((field("A4", 1),), identity=(("UI", PLAYER_ID), ("UK", USER_KEY), ("MK", "c" * 32))),
    "missing_method_key": SaveCase((field("A4", 1),), identity=(("UI", PLAYER_ID), ("UK", USER_KEY))),
    "duplicate_player": SaveCase((field("A4", 1),), identity=(("UI", PLAYER_ID), *IDENTITY)),
    "stored_config_missing": SaveCase((field("A4", 1),), corrupt=missing_config_row),
    "stored_config_incomplete": SaveCase(sound_fields(), corrupt=incomplete_config_row),
    "stored_bootstrap_schema": SaveCase((field("A4", 1),), corrupt=unsupported_bootstrap_schema),
    "stored_volume_text": SaveCase((field("A4", 1),), corrupt=textual_volume),
    "stored_switch_integer": SaveCase(sound_fields(), corrupt=integer_switch),
    "stored_user_name_too_long": SaveCase((field("A4", 1),), corrupt=oversized_user_name),
}
VALUE_REJECTIONS = (
    "empty", "tutorial_with_unknown", "tutorial_with_sound", "unknown_only", "sound_partial", "sound_missing_field",
    "sound_with_unknown", "tutorial_overflow", "tutorial_underflow", "sound_bgm_two", "sound_se_negative",
    "sound_volume_overflow", "sound_flag_underflow", "priority_first_range_wins", "priority_first_range_wins_reversed",
)


class MemoryStore:
    # A fixed identity keeps the SYSTEM ExchangeKey inside the recorded responses stable.
    def __init__(self, state: dict):
        self._state, self._revision = json.loads(serialize(state)), 0

    def profile(self) -> PlayerProfile:
        return PlayerProfile(PLAYER_ID, USER_KEY, METHOD_KEY, deepcopy(self._state), self._revision)

    def update_state(self, state: dict, expected_revision: int) -> PlayerProfile:
        if expected_revision != self._revision:
            raise ValueError("Profile revision changed; reload before updating")
        self._state, self._revision = json.loads(serialize(state)), self._revision + 1
        return self.profile()


def golden_bootstrap(root: Path) -> BootstrapService:
    return BootstrapService(root, clock=lambda: SAVED_AT, asset_version="a" * 40)


def without_configuration(state: dict) -> dict:
    tables = state.get("tables")
    return {**state, "tables": {name: rows for name, rows in tables.items() if name != "config"}} if isinstance(tables, dict) else state


def save_outcome(service: BootstrapService, case: SaveCase, registration: int) -> dict:
    state = service.initial_state()
    state["tables"]["config"][0].update(case.stored)
    if case.corrupt is not None:
        case.corrupt(state)
    store = MemoryStore(state)
    before = store.profile()
    try:
        result = {"response": service.handlers()[registration](case.envelope(registration), store).decode("utf-8")}
    except ProtocolError as error:
        result = {"error": {"code": error.code, "message": str(error), "status": error.status}}
    after = store.profile()
    assert without_configuration(after.state) == without_configuration(before.state)
    return {**result, "request": case.xml(), "revisions": after.revision - before.revision,
            "config": after.state["tables"].get("config")}


@cache
def recorded_saves() -> dict:
    return json.loads(CONFIGURATION_GOLDEN.read_bytes())


def canonical(value: object) -> str:
    # Plain equality treats True as 1, so stored switch types are compared through JSON text.
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def request_values(case: SaveCase) -> dict:
    return {node.tag: int(node.find("V").text) for node in ET.fromstring(case.xml()) if node.tag not in COMMON_FIELDS}


def failure(call: Callable[[], object]) -> tuple[str, str, int]:
    with pytest.raises(ProtocolError) as caught:
        call()
    return caught.value.code, str(caught.value), caught.value.status


def profile_of(state) -> PlayerProfile:
    return PlayerProfile(PLAYER_ID, USER_KEY, METHOD_KEY, state, 0)


def populated_state(service: BootstrapService) -> dict:
    state = service.initial_state()
    state["tables"]["crystals"] = [{"FreeFlag": True, "Num": 5, "Date": 1}, {"FreeFlag": False, "Num": 3, "Date": 2}]
    state["tables"]["quest_records"] = [{"QuestID": 7, "MaxRank": 3, "AchieveFlag": 1, "RemainAssault": 0,
                                         "FirstDefeatDate": 9, "Shown": True}]
    return state


@pytest.fixture
def bootstrap(asset_root):
    return golden_bootstrap(asset_root)


@pytest.fixture
def players(assets, master):
    return PlayerStateService(assets[0], lambda name: master)


def test_configuration_golden_covers_every_case_for_both_registrations():
    recorded = recorded_saves()
    assert recorded["registrations"] == list(CONFIGURATION_REGISTRATIONS)
    assert recorded["cases"].keys() == SAVE_CASES.keys()
    outcomes = recorded["cases"].values()
    assert {"tutorial_flags", "sound_options"} <= {name for name, outcome in recorded["cases"].items() if "response" in outcome}
    assert {outcome["error"]["code"] for outcome in outcomes if "error" in outcome} >= {
        "invalid_configuration_request", "invalid_identity", "invalid_method_key", "bootstrap_state_unavailable"}


@pytest.mark.parametrize("registration", CONFIGURATION_REGISTRATIONS)
@pytest.mark.parametrize("name", SAVE_CASES)
def test_configuration_save_matches_recorded_golden_bytes(bootstrap, name, registration):
    assert canonical(save_outcome(bootstrap, SAVE_CASES[name], registration)) == canonical(recorded_saves()["cases"][name])


def test_configuration_updates_equal_the_request_parser_for_every_accepted_golden():
    accepted = [name for name, outcome in recorded_saves()["cases"].items() if "response" in outcome]
    assert accepted
    for name in accepted:
        expected = BootstrapService._configuration_updates(SAVE_CASES[name].envelope(21))
        updates = configuration_updates(request_values(SAVE_CASES[name]))
        assert canonical(list(updates.items())) == canonical(list(expected.items()))


@pytest.mark.parametrize("name", VALUE_REJECTIONS)
def test_configuration_updates_reject_with_the_recorded_golden_error(name):
    error = recorded_saves()["cases"][name]["error"]
    assert failure(lambda: configuration_updates(request_values(SAVE_CASES[name]))) == (
        error["code"], error["message"], error["status"])


def test_configuration_updates_convert_switches_in_input_order_without_mutating_input():
    values = {"FD": 1, "FA": 0, "F9": -1, "C3": 72, "C2": 0x7FFFFFFF, "A3": 0, "A2": 1}
    original = dict(values)
    updates = configuration_updates(values)
    assert values == original
    assert list(updates.items()) == [
        ("BattleDClickSkillDisableFlag", 1), ("BattleAcceleratorFlag", 0), ("BattlePlaceMagnetFlag", -1),
        ("_VolumeSe", 72), ("_VolumeBgm", 0x7FFFFFFF), ("UseSe", False), ("UseBgm", True)]
    assert type(updates["UseSe"]) is bool and type(updates["UseBgm"]) is bool
    assert configuration_updates({"A4": -0x80000000}) == {"MenuTutorialFlags": -0x80000000}


@pytest.mark.parametrize("values", [None, [("A4", 1)], "A4", 7, {}, {"A4": 1, "A5": 1}, {"C2": 1}, {1: 1}],
                         ids=["none", "pairs", "text", "number", "empty", "unknown", "partial", "integer_key"])
def test_configuration_updates_require_one_complete_option_group(values):
    assert failure(lambda: configuration_updates(values)) == OPTION_GROUP_ERROR


def test_request_values_answer_membership_without_parsing():
    values = _RequestIntegers([ET.fromstring(STRING_C2)])
    assert "C2" in values and "A2" not in values and list(values) == ["C2"]
    assert failure(lambda: values["C2"]) == ("invalid_configuration_request", "Expected one integer value in C2", 400)


@pytest.mark.parametrize("wire,value", [("A4", True), ("A4", "7"), ("A4", 7.0), ("A2", True), ("C2", None)])
def test_configuration_updates_require_plain_integers(wire, value):
    values = {"A4": value} if wire == "A4" else {**SOUND_OPTIONS, wire: value}
    assert failure(lambda: configuration_updates(values)) == (
        "invalid_configuration_request", f"Expected one integer value in {wire}", 400)


@pytest.mark.parametrize("table", TABLES)
def test_table_rows_equal_private_rows_as_detached_copies(bootstrap, table):
    state = populated_state(bootstrap)
    before = deepcopy(state)
    private = bootstrap._rows(profile_of(state), table, single=table not in MULTI_ROW_TABLES)
    rows = bootstrap.table_rows(state, table)
    assert rows and rows == private
    assert all(copy is not original for copy, original in zip(rows, private))
    rows[0][next(iter(rows[0]))] = "changed"
    rows.append({})
    assert state == before


def test_table_rows_follow_native_table_cardinality(bootstrap):
    state = bootstrap.initial_state()
    assert bootstrap.table_rows(state, "crystals") == [] == bootstrap.table_rows(state, "quest_records")
    state["tables"]["config"].append(dict(state["tables"]["config"][0]))
    assert failure(lambda: bootstrap.table_rows(state, "config")) == failure(
        lambda: bootstrap._rows(profile_of(state), "config"))


def replaced_state(state: dict) -> list:
    return []


def unsupported_schema(state: dict) -> dict:
    return {**state, "bootstrap_schema": 2}


def listed_tables(state: dict) -> dict:
    return {**state, "tables": []}


def without_config_table(state: dict) -> dict:
    state["tables"].pop("config")
    return state


def extra_config_field(state: dict) -> dict:
    state["tables"]["config"][0]["Extra"] = 1
    return state


def overflowing_time(state: dict) -> dict:
    state["tables"]["game_param"][0]["CharismaChangeTime"] = 1.5
    return state


def noncanonical_datetime(state: dict) -> dict:
    state["tables"]["game_param2"][0]["GameStartDateTime"] = "2026/1/1 00:00:00"
    return state


def textual_crystals(state: dict) -> dict:
    state["tables"]["crystals"][0]["Num"] = "5"
    return state


def excess_quest_records(state: dict) -> dict:
    state["tables"]["quest_records"] = state["tables"]["quest_records"] * 65536
    return state


def unchanged(state: dict) -> dict:
    return state


@pytest.mark.parametrize("table,corrupt", [
    ("config", replaced_state), ("config", unsupported_schema), ("config", listed_tables),
    ("config", without_config_table), ("config", extra_config_field), ("game_param", overflowing_time),
    ("game_param2", noncanonical_datetime), ("crystals", textual_crystals), ("quest_records", excess_quest_records),
    ("presents", unchanged), ("card_status", unchanged),
], ids=lambda value: value if isinstance(value, str) else value.__name__)
def test_table_rows_reject_with_the_private_error(bootstrap, table, corrupt):
    state = corrupt(populated_state(bootstrap))
    expected = failure(lambda: bootstrap._rows(profile_of(state), table, single=table not in MULTI_ROW_TABLES))
    assert failure(lambda: bootstrap.table_rows(state, table)) == expected
    assert expected[2] == 503


def test_system_container_equals_the_private_system_block(bootstrap):
    profile = profile_of(bootstrap.initial_state())
    container = bootstrap.system_container(profile)
    assert container == bootstrap._system(profile)
    assert container.name == "SYSTEM" and container.children[1].values == (METHOD_KEY, "0")


def test_possessions_container_equals_the_private_inventory(bootstrap):
    state = bootstrap.initial_state()
    assert bootstrap.possessions_container(profile_of(state)) == bootstrap._possessions(profile_of(state)) == Container("POSSESSION")
    inventory = Container("POSSESSION", (Column("A1", "S", ("item_1",)), Column("A2", "I", (3,))))
    seen = []
    bootstrap.possession_provider = lambda current: seen.append(current) or inventory
    assert bootstrap.possessions_container(profile_of(state)) == bootstrap._possessions(profile_of(state)) == inventory
    assert seen == [state, state]


@pytest.mark.parametrize("provider,rows", [
    (None, [{"ItemID": "item_1", "Count": 1}]),
    (lambda state: Container("WRONG"), []),
    (lambda state: Container("POSSESSION", index="1"), []),
], ids=["nonempty_without_provider", "wrong_container", "indexed_container"])
def test_possessions_container_rejects_with_the_private_error(bootstrap, provider, rows):
    bootstrap.possession_provider = provider
    state = bootstrap.initial_state()
    state["tables"]["possessions"] = rows
    expected = failure(lambda: bootstrap._possessions(profile_of(state)))
    assert failure(lambda: bootstrap.possessions_container(profile_of(state))) == expected


STARTUP_PROVIDERS = {
    "possession_provider": "POSSESSION", "filter_provider": "PlayerFilterParam", "peruse_provider": "PEV",
    "exchange_history_provider": "ExchangeHistory", "universal_flag_provider": "GF", "fame_provider": "FameDepository",
    "background_provider": "PRM", "event_pass_provider": "EventPassStatus", "vote_provider": "PVH",
}


def provide_startup_collections(service: BootstrapService) -> None:
    for attribute, wire in STARTUP_PROVIDERS.items():
        setattr(service, attribute, lambda state, wire=wire: Container(wire, (Column("A1", "I", (len(state["tables"]),)),)))


@pytest.mark.parametrize("provided", [False, True], ids=["stored_empty", "providers"])
def test_configuration_container_rebuilds_the_private_startup_response(bootstrap, provided):
    if provided:
        provide_startup_collections(bootstrap)
    state = bootstrap.initial_state()
    before = deepcopy(state)
    profile = profile_of(state)
    container = bootstrap.configuration_container(state)
    assert container.name == "DA" and tuple(child.name for child in container.children) == tuple(STARTUP_COLLECTIONS)
    rebuilt = dumps(Container(children=(bootstrap.system_container(profile), *container.children)), limits=STARTUP_COLLECTION_LIMITS)
    assert rebuilt == bootstrap._configuration_response(profile)
    store = MemoryStore(state)
    assert rebuilt == bootstrap.handlers()[137](request(137, store), store)
    assert state == before


def nonempty_recommendations(service: BootstrapService, state: dict) -> None:
    state["tables"]["recommendations"] = [{"ItemID": "item_1"}]


def unprovided_votes(service: BootstrapService, state: dict) -> None:
    provide_startup_collections(service)
    service.vote_provider = None
    state["tables"]["vote_history"] = [{"VoteID": 1}]


def wrong_filters(service: BootstrapService, state: dict) -> None:
    provide_startup_collections(service)
    service.filter_provider = lambda current: Container("PGC")


def wrong_inventory_before_filters(service: BootstrapService, state: dict) -> None:
    wrong_filters(service, state)
    service.possession_provider = lambda current: Container("POSSESSION", index="0")


def recommendations_before_inventory(service: BootstrapService, state: dict) -> None:
    wrong_inventory_before_filters(service, state)
    nonempty_recommendations(service, state)


def unsupported_startup_state(service: BootstrapService, state: dict) -> None:
    state["bootstrap_schema"] = 2


@pytest.mark.parametrize("corrupt,code", [
    (nonempty_recommendations, "collection_rows_unavailable"), (unprovided_votes, "collection_rows_unavailable"),
    (wrong_filters, "filter_provider_invalid"), (wrong_inventory_before_filters, "possession_provider_invalid"),
    (recommendations_before_inventory, "collection_rows_unavailable"), (unsupported_startup_state, "bootstrap_state_unavailable"),
], ids=lambda value: getattr(value, "__name__", value))
def test_configuration_container_rejects_with_the_private_error(bootstrap, corrupt, code):
    state = bootstrap.initial_state()
    corrupt(bootstrap, state)
    expected = failure(lambda: bootstrap._configuration_response(profile_of(state)))
    assert failure(lambda: bootstrap.configuration_container(state)) == expected
    assert expected[0] == code and expected[2] == 503


@pytest.mark.parametrize("references", [True, False])
def test_validate_returns_the_validated_player(players, references):
    state = players.initialize({})
    before = deepcopy(state)
    player = players.validate(state, references=references)
    assert player is state["player_state"]
    assert players.validate_player(state["player_state"], references=references) is player
    assert state == before


def test_validate_checks_card_references_only_when_asked(players):
    state = players.initialize({})
    state["player_state"]["units"][0]["CardID"] = 99
    expected = failure(lambda: players.validate_player(state["player_state"]))
    assert expected[0] == "player_reference_invalid"
    assert failure(lambda: players.validate(state)) == expected
    assert failure(lambda: players.available_unit_slots(state)) == expected
    assert players.validate(state, references=False) is state["player_state"]


@pytest.mark.parametrize("state", [[], None, "player_state"], ids=["list", "none", "text"])
def test_validate_requires_a_state_object(players, state):
    expected = ("player_state_schema", "Player state must be an object", 503)
    assert failure(lambda: players.validate(state)) == expected
    assert failure(lambda: players.available_unit_slots(state)) == expected


def without_player(state: dict) -> None:
    state.pop("player_state")


def unsupported_player_schema(state: dict) -> None:
    state["player_state"]["schema_version"] = 2


def empty_roster(state: dict) -> None:
    state["player_state"]["units"] = []


def duplicate_unit(state: dict) -> None:
    state["player_state"]["units"].append(dict(state["player_state"]["units"][0]))


def stale_battle_index(state: dict) -> None:
    state["player_state"]["units"][0]["GetIndex"] = 5


def missing_import_marker(state: dict) -> None:
    del state["player_state"]["legacy_squad_imported"]


def stored_squad_without_member(state: dict) -> None:
    state["player_state"]["squads"] = [squad_row(unit=9)]


@pytest.mark.parametrize("references", [True, False])
@pytest.mark.parametrize("corrupt", [without_player, unsupported_player_schema, empty_roster, duplicate_unit,
                                     stale_battle_index, missing_import_marker, stored_squad_without_member],
                         ids=lambda corrupt: corrupt.__name__)
def test_validate_rejects_with_the_validated_player_error(players, corrupt, references):
    state = players.initialize({})
    corrupt(state)
    expected = failure(lambda: players.validate_player(state.get("player_state"), references=references))
    assert failure(lambda: players.validate(state, references=references)) == expected
    assert expected[2] == 503


SQUAD_REJECTIONS = {
    "not_a_list": (None, "Squad rows must be a bounded list"),
    "too_many_rows": ([squad_row(address=group << 24) for group in range(MAX_SQUAD_ROWS + 1)],
                      "Squad rows must be a bounded list"),
    "missing_column": ([{"Address": 0, "UnitIndex": 0, "Name": ""}], "Squad row columns are incomplete"),
    "textual_flags": ([squad_row(flags="0")], "Squad identifiers and flags must be int32"),
    "reserved_group": ([squad_row(address=squad_address(RESERVED_SQUAD_GROUP, 0, 0))],
                       "Invalid or duplicate native squad address"),
    "slot_out_of_range": ([squad_row(address=MAX_SQUAD_SLOT + 1)], "Invalid or duplicate native squad address"),
    "duplicate_address": ([squad_row(), squad_row()], "Invalid or duplicate native squad address"),
    "unknown_member": ([squad_row(unit=5)], "Squad member does not reference an owned GetIndex"),
    "oversized_name": ([squad_row(name="n" * (MAX_SQUAD_NAME_BYTES + 1))], "Squad name exceeds the local text limit"),
}


@pytest.mark.parametrize("stored", [False, True], ids=["request", "stored"])
@pytest.mark.parametrize("name", SQUAD_REJECTIONS)
def test_validate_squads_rejects_with_the_saved_player_error(players, name, stored):
    rows, reason = SQUAD_REJECTIONS[name]
    code, message, status = failure(lambda: players.validate_player({**players.initialize({})["player_state"], "squads": rows}))
    assert (message, status) == (reason, 503)
    assert failure(lambda: players.validate_squads(rows, {0}, stored=stored)) == (code, message, status if stored else 400)


def test_validate_squads_accepts_valid_rows_and_defaults_to_request_status(players):
    rows = [squad_row(), squad_row(address=1, unit=-1), squad_row(address=1 << 24, unit=0)]
    before = deepcopy(rows)
    assert players.validate_squads(rows, {0}) is None
    assert rows == before
    assert failure(lambda: players.validate_squads([squad_row(unit=5)], {0}))[2] == 400

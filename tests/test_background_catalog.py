from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.asset_service import ASSET_CHANGED, ASSET_NOT_CACHED, ASSET_TOO_LARGE, IndexedAssetService
from aigis_ps.background_catalog import (AUDIT_KINDS, BackgroundCatalog, EXECUTABLE_CATEGORIES, EXECUTABLE_SOURCE,
    FIELDS, FIXED_OUTSIDE_GLOBAL, LEGACY_WINDOW_RULE, LIMITS, NON_NORMALIZED_KEY, PAGE_BACKGROUND, PRIVATE_ROOM_MISMATCH,
    RESOURCE_NAME, SCRIPT_SOURCE, UNKNOWN_NAME, WINDOW_RULE)
from aigis_ps.background_state import MAX_CONFIG_BYTES
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.gift_catalog import GiftCatalog
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.native_clock import parse_native_date
from aigis_ps.native_commands import ABSENT_TABLE, REGISTERED_TABLES
from aigis_ps.player_state import MAX_PLAYER_UNITS, UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.possessions import PossessionService
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.system_literals import command_id, encode_command
from test_present_items import item_table
from test_public_tables import digested


OTHERS = "OnCommandMenuOthers.atb"
SHOP = "OnCommandMenuShop.atb"
ASSEMBLE = "CMenuEtcExchangeBg::OnAssemble"
PRE_ASSEMBLE = "CMenuEtcExchangeBg::OnPreAssemble"
NATIVE_FILTER = ('MnObj_SetParam( "GetBackButtonTitleID=49" );\nMnObj_SetParam("FilterType=Category" );\n'
                 'MnObj_SetParam("FilterItem=通常,季節背景,人気投票,システム固定" );')
COMMAND_SCHEMA = (("_ID", 1), ("_Cmnd", 32))
BARRACKS = -95774812


def command_table(*commands):
    return item_table([{"_ID": command_id(name), "_Cmnd": encode_command(text)} for name, text in commands], COMMAND_SCHEMA)


def native_commands(text=NATIVE_FILTER, name=ASSEMBLE):
    return {OTHERS: command_table((name, text))}


def filter_script(kind="Category", items="通常,季節背景,人気投票,システム固定"):
    return f'MnObj_SetParam("FilterType={kind}");\nMnObj_SetParam("FilterItem={items}");'


def reader(payload, commands):
    def read(name):
        if name == RESOURCE_NAME:
            return payload
        if name in commands:
            return commands[name]
        raise ProtocolError(ABSENT_TABLE, "Resource is not in the current public index", 404)
    return read


class Cards:
    def __init__(self, rows=None):
        self.rows = rows or {1: {"CardID": 1, "OriginID": 0}, 10: {"CardID": 10, "OriginID": 0},
                             20: {"CardID": 20, "OriginID": 10}, 30: {"CardID": 30, "OriginID": 20}}
        self.card_ids = tuple(self.rows)
        self.raw = json.dumps(self.rows, sort_keys=True).encode()
        self.snapshot = SimpleNamespace(get_xml=lambda _: self.raw)

    def card(self, key):
        return self.rows[key]


def background(name="system_default", **changes):
    result = {"ID": command_id(name), "TextID": 0, "Category": -1524803586, "BackGround": -5,
        "RoomID": 0, "BgmID": -10, "CardID": 0, "DefineName": name,
        "Begin": "2001/01/01 00:00:00", "End": ""}
    result.update(changes)
    return result


def default_rows():
    return [background(), background("system_fix", Category=463396108),
        background("private_one", Category=-1224326894, CardID=10),
        background("event_one", Category=-1777172756, Begin="2026/10/01 10:00:00", End="2026/11/01 12:00:00"),
        background("internal", Category=163175264), background("system_unit", Category=BARRACKS)]


def component(rows=None, *, cards=None, payload=None, commands=None):
    cards = cards or Cards()
    payload = item_table(default_rows() if rows is None else rows, tuple(FIELDS.items())) if payload is None else payload
    catalog = BackgroundCatalog(cards, reader(payload, {} if commands is None else commands))
    return SimpleNamespace(catalog=catalog, cards=cards, payload=payload)


def unit(card_id=10, index=7, **changes):
    result = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
    result.update(CardID=card_id, ClassID=100, GetIndex=index, UniqueID=index + 1, SkillLevel=1,
                  OrganizationIndex=-1, OrganizationIndex2=-1)
    result.update(changes)
    return result


def player(*units):
    return {"schema_version": 1, "units": list(units) or [unit()]}


def available(catalog, when, name="event_one"):
    try:
        catalog.selection(name, 0, when)
    except ProtocolError as error:
        if error.code != "background_unavailable":
            raise
        return False
    return True


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    client_hash = CURRENT_CLIENT_SHA256
    metadata, entries = ResourceLibrary(project / "runtime/resources", client_hash=client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(project.parent / "aigisr_exe", asset_reader=reader)
    snapshot = MasterDataStore(project / "runtime/server/masterdata", expected_hash=client_hash).snapshot()
    cards = GiftCatalog(snapshot, reader, possessions, resource_version=metadata["version"])
    return SimpleNamespace(catalog=BackgroundCatalog(cards, reader), cards=cards, reader=reader)


def test_current_catalog_preserves_all_definitions_and_normal_scope(current):
    catalog = current.catalog
    assert len(catalog.definitions) == 53
    assert catalog.status()["selectable_count"] == 43
    assert catalog.status()["ready"] is True
    assert catalog.snapshot is current.cards.snapshot
    assert sum(row.card_id > 0 for row in catalog.definitions) == 15
    assert catalog.get("system_fix").is_fixed
    assert all(row.begin is None or row.begin.tzinfo == JST for row in catalog.definitions)
    for definition in catalog.definitions:
        if definition.category not in catalog.categories:
            with pytest.raises(ProtocolError) as error:
                catalog.selection(definition.name, 0, datetime(2026, 11, 1))
            assert (error.value.code, error.value.status) == ("background_unsupported", 501)
            assert "another native page" in str(error.value)


def test_current_private_rooms_require_matching_guide_and_no_other_ownership(current):
    for definition in current.catalog.definitions:
        if definition.card_id:
            assert current.catalog.selection(definition.name, definition.card_id, datetime(2026, 11, 1)) == (0, definition.card_id)
            with pytest.raises(ProtocolError) as error:
                current.catalog.selection(definition.name, 0, datetime(2026, 11, 1))
            assert error.value.code == "background_guide_mismatch"


def test_current_origin_reference_is_used_before_background_selection(current):
    candidates = [current.cards.card(key) for key in current.cards.card_ids if current.cards.card(key)["OriginID"]]
    assert candidates
    original = candidates[0]
    assert current.catalog.guide_id(player(unit(original["CardID"])), {"_GuideUnitIndex": 7}) == original["OriginID"]


@pytest.mark.parametrize("name,guide,expected", [
    ("system_default", 0, (0,)), ("system_default", 20, (0, 20)),
    ("system_fix", 10, (0,)), ("private_one", 10, (0, 10)), ("event_one", 0, (0,)),
])
def test_native_selection_keys(name, guide, expected):
    assert component().catalog.selection(name, guide, datetime(2026, 11, 1, 11)) == expected


def test_additional_rows_in_verified_categories_are_not_rejected_by_fixed_counts():
    catalog = component([background("new_default"), background("new_fixed_category", Category=463396108)]).catalog
    assert catalog.status()["selectable_count"] == 2
    assert catalog.selection("new_default", 10, datetime(2026, 1, 1)) == (0, 10)
    assert catalog.selection("new_fixed_category", 10, datetime(2026, 1, 1)) == (0, 10)


@pytest.mark.parametrize("name", ["x" * 255, "界" * 85], ids=["ascii", "utf8"])
def test_name_byte_limit_accepts_exact_shared_boundary(name):
    assert len(name.encode("utf-8")) == MAX_CONFIG_BYTES == 255
    catalog = component([background(name)]).catalog
    assert catalog.get(name).name == name
    assert catalog.selection(name, 0, datetime(2026, 1, 1)) == (0,)


@pytest.mark.parametrize("name", ["x" * 256, "界" * 85 + "x"], ids=["ascii", "utf8"])
def test_name_byte_limit_rejects_one_byte_over_shared_boundary(name):
    assert len(name.encode("utf-8")) == MAX_CONFIG_BYTES + 1
    with pytest.raises(ProtocolError) as error:
        component([background(name)])
    assert error.value.code == "background_catalog_invalid"


@pytest.mark.parametrize("when,accepted", [
    (datetime(2026, 10, 1, 9, 59, 59), False), (datetime(2026, 10, 1, 9, 59, 59, 999999), False),
    (datetime(2026, 10, 1, 10), True), (datetime(2026, 10, 1, 10, 0, 0, 500000), True),
    (datetime(2026, 11, 1, 12), True), (datetime(2026, 11, 1, 12, 0, 0, 500000), True),
    (datetime(2026, 11, 1, 12, 0, 0, 999999), True), (datetime(2026, 11, 1, 12, 0, 1), False),
])
def test_both_boundaries_include_their_whole_client_clock_second(when, accepted):
    assert available(component().catalog, when) is accepted


def test_bounds_and_clock_share_the_client_float32_grid():
    opening, closing = "2026/10/01 06:00:07", "2026/11/01 12:00:08"
    catalog = component([background("event_one", Category=-1777172756, Begin=opening, End=closing)]).catalog
    definition = catalog.get("event_one")
    assert (definition.begin, definition.end) == (parse_native_date(opening), parse_native_date(closing))
    assert definition.begin - datetime(2026, 10, 1, 6, 0, 7, tzinfo=JST) == timedelta(milliseconds=4)
    assert datetime(2026, 11, 1, 12, 0, 8, tzinfo=JST) - definition.end == timedelta(milliseconds=4)
    assert [available(catalog, when) for when in (
        datetime(2026, 10, 1, 6, 0, 6, 999999), datetime(2026, 10, 1, 6, 0, 7),
        datetime(2026, 11, 1, 12, 0, 8, 999999), datetime(2026, 11, 1, 12, 0, 9))] == [False, True, True, False]


def test_aware_time_preserves_instant_and_naive_is_jst():
    catalog = component().catalog
    utc = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    assert catalog.selection("event_one", 0, utc) == catalog.selection("event_one", 0, datetime(2026, 11, 1, 12))
    assert catalog.selection("event_one", 0, utc.astimezone(timezone(timedelta(hours=-8)))) == (0,)
    assert catalog.selection("event_one", 0, utc + timedelta(microseconds=999999)) == (0,)
    with pytest.raises(ProtocolError):
        catalog.selection("event_one", 0, utc + timedelta(seconds=1))


@pytest.mark.parametrize("when", [None, 0, True, "2026/11/01", datetime.max.replace(tzinfo=timezone.utc)])
def test_invalid_clocks_do_not_become_open_windows(when):
    with pytest.raises(ProtocolError) as error:
        component().catalog.selection("system_default", 0, when)
    assert error.value.code == "background_clock_invalid"


def test_empty_calendar_endpoints_are_unbounded():
    catalog = component([background(Begin="", End="")]).catalog
    assert catalog.selection("system_default", 0, datetime(1, 1, 1)) == (0,)
    assert catalog.selection("system_default", 0, datetime(9999, 12, 31, 23, 59, 59)) == (0,)


@pytest.mark.parametrize("value", [None, True, 0, [], {}, "unknown", ""])
def test_get_rejects_unknown_names(value):
    with pytest.raises(ProtocolError) as error:
        component().catalog.get(value)
    assert error.value.code == "background_unknown"


@pytest.mark.parametrize("value", [True, False, -1, 2**31, "10", None, 999])
def test_selection_rejects_invalid_guide_identity(value):
    with pytest.raises(ProtocolError) as error:
        component().catalog.selection("system_default", value, datetime(2026, 1, 1))
    assert error.value.code == "background_guide_invalid"


def test_guide_identity_is_one_original_card_origin_hop_not_an_image_alias():
    case = component()
    rows = player(unit(1, 0), unit(30, 7, GeneralPurposeFlags=-2147483610, Love=0, SkillEvolved=0))
    config = {"_GuideUnitIndex": 7, "unrelated": [1]}
    before = deepcopy((rows, config))
    assert case.catalog.guide_id(rows, config) == 20
    assert (rows, config) == before
    assert case.catalog.guide_id(rows, {"_GuideUnitIndex": 999}) == 0
    assert case.catalog.guide_id(rows, {"_GuideUnitIndex": 0}) == 1


@pytest.mark.parametrize("config", [{}, {"C1": 7}, {"_GuideUnitIndex": True}, {"_GuideUnitIndex": -1},
                                     {"_GuideUnitIndex": "7"}, {"_GuideUnitIndex": 2**31}, None])
def test_invalid_saved_guide_config_is_not_silently_cleared(config):
    with pytest.raises(ProtocolError) as error:
        component().catalog.guide_id(player(), config)
    assert error.value.code == "background_state_invalid"


@pytest.mark.parametrize("state", [None, {}, {"schema_version": True, "units": [unit()]},
    {"schema_version": 2, "units": [unit()]}, {"schema_version": 1, "units": []},
    {"schema_version": 1, "units": (unit(),)}, player(unit(), unit()),
    player(unit(), unit(20, 8, UniqueID=8)), player(unit(), unit(999, 8)),
    player(unit(), unit(10, 8, Exp=True)), player(unit(), unit(10, 8, Famed=1)),
    player(unit(), unit(10, 8, SkillExperience=-1)), player(unit(), {"CardID": 10, "GetIndex": 8}),
    {"schema_version": 1, "units": [unit()] * (MAX_PLAYER_UNITS + 1)},
])
def test_invalid_roster_is_checked_even_when_requested_index_is_missing(state):
    with pytest.raises(ProtocolError) as error:
        component().catalog.guide_id(state, {"_GuideUnitIndex": 999})
    assert error.value.code == "background_state_invalid"


def test_metadata_and_definitions_are_immutable():
    case = component()
    catalog = case.catalog
    with pytest.raises(FrozenInstanceError):
        catalog.definitions[0].name = "changed"
    with pytest.raises(TypeError):
        catalog.by_name["new"] = catalog.definitions[0]
    for name in ("content_version", "snapshot", "definitions", "by_name", "categories"):
        with pytest.raises(AttributeError):
            setattr(catalog, name, None)
    status = catalog.status()
    status["resource_sha256"][RESOURCE_NAME] = "bad"
    status["categories"]["values"].append(BARRACKS)
    assert catalog.status()["resource_sha256"][RESOURCE_NAME] != "bad"
    assert BARRACKS not in catalog.status()["categories"]["values"]


@pytest.mark.parametrize("change", ["raw", "origin", "ids", "card_id", "missing_card"])
def test_dependency_changes_are_detected_by_every_public_operation(change):
    case = component()
    if change == "raw":
        case.cards.raw += b"changed"
    elif change == "origin":
        case.cards.rows[20]["OriginID"] = 0
    elif change == "ids":
        case.cards.card_ids += (99,)
    elif change == "card_id":
        case.cards.rows[20]["CardID"] = 10
    else:
        del case.cards.rows[20]
    for action in (lambda: case.catalog.get("system_default"),
                   lambda: case.catalog.selection("system_default", 0, datetime(2026, 1, 1)),
                   lambda: case.catalog.guide_id(player(), {"_GuideUnitIndex": 7}),
                   lambda: case.catalog.audit([])):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "background_catalog_changed"
    assert case.catalog.is_ready() is case.catalog.status()["ready"] is False


@pytest.mark.parametrize("changed", [False, True])
def test_readiness_matches_status_without_building_it(monkeypatch, changed):
    case = component()
    if changed:
        case.cards.rows[20]["OriginID"] = 0
    ready = case.catalog.status()["ready"]
    assert ready is (not changed)
    monkeypatch.setattr(case.catalog, "status", lambda: pytest.fail("Readiness built the full catalog status"))
    assert case.catalog.is_ready() is ready


def test_card_master_is_digested_once_per_bytes_object(digested):
    case = component()
    verified = case.cards.raw
    for _ in range(2):
        assert case.catalog.is_ready() is case.catalog.status()["ready"] is True
    assert len(digested) == 1 and digested[0] is verified
    case.cards.raw = bytes(bytearray(verified))
    for _ in range(2):
        assert case.catalog.get("system_default").name == "system_default"
    assert len(digested) == 2 and digested[1] is case.cards.raw
    case.cards.raw = verified + b"changed"
    for _ in range(2):
        with pytest.raises(ProtocolError) as error:
            case.catalog.get("system_default")
        assert (error.value.code, error.value.status) == ("background_catalog_changed", 503)
    assert len(digested) == 3 and digested[2] is case.cards.raw
    case.cards.raw = verified
    assert case.catalog.status()["ready"] is True and len(digested) == 4 and digested[3] is verified


@pytest.mark.parametrize("change", [{"ID": 123}, {"CardID": -1}, {"CardID": 999}, {"RoomID": -1},
    {"Begin": "2026/02/30 00:00:00"}, {"Begin": "2026/01/01"}, {"Begin": "2026/01-01 00:00:00"},
    {"Begin": "2026/01/01 00:00:00Z"}, {"Begin": "2026/01/01 24:00:00"},
    {"Begin": "2026/11/02 00:00:00", "End": "2026/11/01 00:00:00"},
    {"DefineName": ""}, {"DefineName": "x" * 513}, {"DefineName": "bad\x01"},
])
def test_invalid_resource_rows_fail_construction(change):
    with pytest.raises(ProtocolError) as error:
        component([background(**change)])
    assert error.value.code == "background_catalog_invalid"


@pytest.mark.parametrize("rows", [[], [background(), background()],
    [background("system_fix")], [background("system_fix", Category=463396108, CardID=10)]])
def test_empty_duplicate_or_inconsistent_fixed_definitions_are_rejected(rows):
    with pytest.raises(ProtocolError) as error:
        component(rows)
    assert error.value.code == "background_catalog_invalid"


@pytest.mark.parametrize("field", [name for name, kind in FIELDS.items() if kind == 1])
def test_boolean_resource_values_are_not_native_integers(monkeypatch, field):
    row = background()
    row[field] = False
    parsed = {"fields": [{"name": name, "type_code": kind} for name, kind in FIELDS.items()], "rows": [row]}
    monkeypatch.setattr("aigis_ps.background_catalog.parse_table", lambda *args: parsed)
    with pytest.raises(ProtocolError) as error:
        component(payload=b"synthetic")
    assert error.value.code == "background_catalog_invalid"


@pytest.mark.parametrize("payload", [b"", b"broken", None, "ALTB", bytearray(b"ALTB")])
def test_resources_require_complete_immutable_bytes(payload):
    cards = Cards()
    with pytest.raises(ProtocolError) as error:
        BackgroundCatalog(cards, lambda _: payload)
    assert error.value.code == "background_catalog_invalid"


def test_resource_size_bound_precedes_parsing():
    with pytest.raises(ProtocolError) as error:
        component(payload=b"x" * (LIMITS.max_input_bytes + 1))
    assert error.value.code == "background_catalog_invalid"


@pytest.mark.parametrize("mutate", [
    lambda c: setattr(c, "raw", "not-bytes"), lambda c: setattr(c, "card_ids", (1, 1)),
    lambda c: setattr(c, "card_ids", (True,)), lambda c: c.rows[20].update(OriginID=True),
    lambda c: c.rows[20].update(OriginID=-1), lambda c: c.rows[20].update(OriginID=999),
    lambda c: c.rows[20].update(CardID=30),
])
def test_invalid_master_identity_and_origin_are_rejected(mutate):
    cards = Cards()
    mutate(cards)
    with pytest.raises(ProtocolError) as error:
        component(cards=cards)
    assert error.value.code == "background_catalog_invalid"


def test_content_version_is_deterministic_and_includes_master_and_resource():
    case = component()
    assert component().catalog.content_version == case.catalog.content_version
    changed = component([background(BgmID=99)])
    assert changed.catalog.content_version != component([background()]).catalog.content_version
    cards = Cards()
    cards.raw += b"version"
    assert component(cards=cards).catalog.content_version != case.catalog.content_version
    cards = Cards()
    cards.rows[20]["OriginID"] = 0
    assert component(cards=cards).catalog.content_version != case.catalog.content_version


def test_client_clock_window_rule_keeps_the_jst_rule_version_replayable(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("aigis_ps.background_catalog.WINDOW_RULE", LEGACY_WINDOW_RULE)
        earlier = component().catalog.content_version
    case = component()
    current, status = case.catalog, case.catalog.status()
    categories = status["categories"]
    committed = {"rule": "background-selection-v2", "resource": status["resource_sha256"][RESOURCE_NAME],
        "master": status["master_sha256"], "origins": {key: row["OriginID"] for key, row in case.cards.rows.items()},
        "categories": categories["values"], "category_source": categories["source"],
        "filter_scripts": [[script["table"], script["row"]] for script in categories["scripts"]],
        "commands": categories["command_content_version"], "fixed_name": "system_fix", "name_bytes": MAX_CONFIG_BYTES,
        "window": "inclusive_jst", "guide": "get_index_then_one_origin_id_hop"}
    assert earlier == hashlib.sha256(json.dumps(committed, sort_keys=True).encode()).hexdigest()
    assert status["window"] == WINDOW_RULE == "inclusive_native_clock"
    assert LEGACY_WINDOW_RULE == "inclusive_jst" and earlier != current.content_version
    assert current.replayable_content_versions() == frozenset((current.content_version, earlier))
    assert component([background(BgmID=99)]).catalog.content_version not in current.replayable_content_versions()


def test_native_all4_resource_is_supported():
    case = component()
    compressed = lz4.frame.compress(case.payload)
    packed = struct.pack("<4sHHI", b"ALL4", 1, 0, len(case.payload)) + compressed
    catalog = component(payload=packed).catalog
    assert catalog.definitions == case.catalog.definitions
    assert catalog.content_version != case.catalog.content_version


def test_missing_wrong_and_duplicate_schema_columns_are_rejected():
    base = list(FIELDS.items())
    for schema in (base[:-1], [(name, 32 if name == "CardID" else kind) for name, kind in base], base + [base[0]]):
        with pytest.raises(ProtocolError) as error:
            component(payload=item_table([background()], tuple(schema)))
        assert error.value.code == "background_catalog_invalid"


def decoded(raw):
    return lz4.frame.decompress(raw[12:]) if raw.startswith(b"ALL4") else raw


def test_current_categories_come_from_the_exchange_menu_assembly_script(current):
    status = current.catalog.status()["categories"]
    assert current.catalog.categories == EXECUTABLE_CATEGORIES
    assert status["source"] == SCRIPT_SOURCE and status["values"] == sorted(EXECUTABLE_CATEGORIES)
    assert [(script["name"], script["table"], script["row"]) for script in status["scripts"]] == [(ASSEMBLE, OTHERS, 62)]
    assert status["scripts"][0]["sha256"] == hashlib.sha256(decoded(current.reader(OTHERS))).hexdigest()
    assert status["command_tables"] == len(REGISTERED_TABLES) and status["absent_command_tables"] == []


@pytest.mark.parametrize("commands", [
    {}, native_commands('MnObj_SetParam("FilterType=Category");'), native_commands('MnObj_SetParam("FilterItem=通常");'),
    native_commands(filter_script(items=",,")), native_commands(filter_script(items="")),
    native_commands('MnObj_SetParam( "GetBackButtonTitleID=49" );'), native_commands(""),
    native_commands('MnObj_SetParam("FilterType=CardID");'), native_commands('MnObj_SetParam("FilterItem=未知, 12");'),
    native_commands(filter_script(kind="CardID", items=",,")),
], ids=["absent_tables", "type_only", "items_only", "only_separators", "empty_items", "unrelated_parameter", "empty_script",
        "other_type_only", "unverifiable_items_only", "other_type_without_items"])
def test_unset_or_empty_filter_keeps_the_executable_categories(commands):
    catalog = component(commands=commands).catalog
    status = catalog.status()
    assert catalog.categories == EXECUTABLE_CATEGORIES and status["categories"]["source"] == EXECUTABLE_SOURCE
    assert status["selectable_count"] == 4
    assert status["categories"]["absent_command_tables"] == sorted(set(REGISTERED_TABLES) - set(commands))


def test_native_script_shape_derives_the_same_four_categories():
    catalog = component(commands=native_commands()).catalog
    assert catalog.categories == EXECUTABLE_CATEGORIES
    assert catalog.status()["categories"]["source"] == SCRIPT_SOURCE


def test_assembly_script_without_filter_parameters_may_run_other_statements():
    commands = {OTHERS: command_table((PRE_ASSEMBLE, 'if( $Open != 0 ){ PlaySE("open"); } 1;'),
                                      (ASSEMBLE, filter_script(items="通常")))}
    catalog = component(commands=commands).catalog
    assert catalog.categories == {command_id("通常")}
    assert [script["name"] for script in catalog.status()["categories"]["scripts"]] == [PRE_ASSEMBLE, ASSEMBLE]


def test_common_menu_scope_and_later_assignments_follow_native_order():
    common = component(commands=native_commands(filter_script(items="通常"), name="CmnMenu::OnAssemble")).catalog
    assert common.categories == {command_id("通常")}
    both = component(commands={OTHERS: command_table((PRE_ASSEMBLE, filter_script(items="通常")),
                                                     (ASSEMBLE, 'MnObj_SetParam("FilterItem=季節背景,通常,通常");'))}).catalog
    assert both.categories == {command_id("季節背景"), command_id("通常")}
    assert [script["name"] for script in both.status()["categories"]["scripts"]] == [PRE_ASSEMBLE, ASSEMBLE]
    repeated = component(commands=native_commands(filter_script(items="通常") + '\nMnObj_SetParam("FilterItem=人気投票");'))
    assert repeated.catalog.categories == {command_id("人気投票")}


def test_resource_script_can_open_or_close_categories_for_saving():
    catalog = component(commands=native_commands(filter_script(items="通常,兵舎"))).catalog
    assert catalog.categories == {command_id("通常"), BARRACKS} and catalog.status()["selectable_count"] == 2
    assert catalog.selection("system_unit", 10, datetime(2026, 1, 1)) == (0, 10)
    with pytest.raises(ProtocolError) as error:
        catalog.selection("system_fix", 0, datetime(2026, 1, 1))
    assert (error.value.code, error.value.status) == ("background_unsupported", 501)


def test_lookalike_words_in_other_commands_are_not_filter_writes():
    others = command_table(("CMenuOverlap::OnAssemble", 'AddFilterLayout("id=0","text=SystemName_FilterType_01");'),
                           ("CHistorySelectMenu::Next", 'CMenuTask_CallCommand("IncrementFilterType");'),
                           ("CMenuShop::OnAssemble", 'MnObj_SetParam("Title=FilterItem"); Wait(11721512720);'))
    catalog = component(commands={**native_commands(), SHOP: others}).catalog
    assert catalog.categories == EXECUTABLE_CATEGORIES


@pytest.mark.parametrize("commands", [
    native_commands(filter_script(kind="CardID")), native_commands(filter_script(items="通常,未知")),
    native_commands(filter_script(items="通常,463396108")), native_commands(filter_script(items="通常, 季節背景")),
    native_commands(filter_script(items="通常,季節背景 ")), native_commands("MnObj_SetParam($Filter);"),
    native_commands(filter_script() + '\nSetSystemStateParam("CMenuEtcExchangeBg::FilterItem", "兵舎");'),
    native_commands('MnObj_SetParam(" FilterItem=通常");'), native_commands('MnObj_SetParam("FilterItem");'),
    native_commands('MnObj_SetParam("CMenuEtcExchangeBg::FilterItem=通常");'),
    {OTHERS: command_table((ASSEMBLE, NATIVE_FILTER), (ASSEMBLE, NATIVE_FILTER))},
    {**native_commands(), SHOP: command_table(("CMenuShop::Open", 'SetSystemStateParam("CMenuEtcExchangeBg::FilterType", "Category");'))},
    {**native_commands(), SHOP: command_table(("CMenuShop::Open", 'SetSystemStateParam(1172151272, "兵舎");'))},
    {**native_commands(), SHOP: command_table(("CMenuShop::Open", 'SetSystemStateParam(-703010337, "Category");'))},
    {OTHERS: command_table((ASSEMBLE, NATIVE_FILTER), ("CmnMenu::OnAssemble", 'MnObj_SetParam("FilterItem =兵舎");'))},
    {**native_commands(), SHOP: b"not a command table"},
    {OTHERS: item_table([background()], tuple(FIELDS.items()))},
    native_commands("if( $Open != 0 ){ " + filter_script(items="通常,兵舎") + " } 1;"),
    native_commands(filter_script() + '\nPlaySE("open");'),
], ids=["other_column", "unknown_name", "numeric_item", "leading_space", "trailing_space", "nonliteral",
        "state_write_in_script", "spaced_key", "missing_value", "full_key_parameter", "duplicate_row",
        "other_row_full_key", "other_row_item_crc", "other_row_type_crc", "shadowed_common_row",
        "unreadable_table", "wrong_columns", "conditional_assignment", "assignment_beside_other_statement"])
def test_unverifiable_filter_sources_fail_closed(commands):
    with pytest.raises(ProtocolError) as error:
        component(commands=commands)
    assert (error.value.code, error.value.status) == ("background_catalog_invalid", 503)


@pytest.mark.parametrize("item", ["12", "-3.5", " 季節背景", "季節背景 ", "　通常"])
def test_numeric_or_padded_items_fail_closed_even_when_their_hash_is_a_category(item):
    rows = [*default_rows(), background("lookalike", Category=command_id(item))]
    with pytest.raises(ProtocolError) as error:
        component(rows, commands=native_commands(filter_script(items="通常," + item)))
    assert error.value.code == "background_catalog_invalid"
    assert component(rows, commands=native_commands(filter_script(items="通常,季節背景"))).catalog.categories == {
        command_id("通常"), command_id("季節背景")}


@pytest.mark.parametrize("code,status", [(ASSET_NOT_CACHED, 404), (ASSET_CHANGED, 409), (ASSET_TOO_LARGE, 413)])
def test_only_tables_absent_from_the_index_are_skipped(code, status):
    present = reader(item_table(default_rows(), tuple(FIELDS.items())), native_commands())
    assert SHOP in BackgroundCatalog(Cards(), present).status()["categories"]["absent_command_tables"]

    def read(name):
        if name == SHOP:
            raise ProtocolError(code, "Indexed resource is unavailable", status)
        return present(name)
    with pytest.raises(ProtocolError) as error:
        BackgroundCatalog(Cards(), read)
    assert (error.value.code, error.value.status) == ("background_catalog_invalid", 503)


def test_content_version_tracks_command_tables_and_category_source():
    base = component(commands=native_commands()).catalog
    assert component(commands=native_commands()).catalog.content_version == base.content_version
    unrelated = component(commands={**native_commands(), SHOP: command_table(("CMenuShop::Open", "1;"))}).catalog
    fallback = component().catalog
    opened = component(commands=native_commands(filter_script(items="通常,兵舎"))).catalog
    assert unrelated.categories == fallback.categories == base.categories
    assert len({base.content_version, unrelated.content_version, fallback.content_version, opened.content_version}) == 4


def saved(card_id, name):
    return {"CardID": card_id, "ConfigID": name, "SaveTime": "2026/11/01 12:00:00"}


def test_audit_annotates_abnormal_rows_without_changing_them():
    catalog = component(commands=native_commands()).catalog
    normal = [saved(0, "system_default"), saved(0, "private_one"), saved(10, "private_one"), saved(0, "system_fix"),
              saved(1, "event_one"), saved(20, "system_default")]
    abnormal = [saved(0, "retired_name"), saved(0, "system_unit"), saved(10, "system_fix"),
                saved(999, "system_default"), saved(1, "private_one"), saved(30, "internal")]
    rows = normal + abnormal
    before = deepcopy(rows)
    assert catalog.audit(rows) == (
        {"CardID": 0, "ConfigID": "retired_name", "kinds": [UNKNOWN_NAME]},
        {"CardID": 0, "ConfigID": "system_unit", "kinds": [PAGE_BACKGROUND]},
        {"CardID": 10, "ConfigID": "system_fix", "kinds": [FIXED_OUTSIDE_GLOBAL]},
        {"CardID": 999, "ConfigID": "system_default", "kinds": [NON_NORMALIZED_KEY]},
        {"CardID": 1, "ConfigID": "private_one", "kinds": [PRIVATE_ROOM_MISMATCH]},
        {"CardID": 30, "ConfigID": "internal", "kinds": [PAGE_BACKGROUND, NON_NORMALIZED_KEY]})
    assert rows == before
    assert set(AUDIT_KINDS) == {kind for finding in catalog.audit(rows) for kind in finding["kinds"]}


def test_audit_accepts_every_key_the_guide_lookup_writes():
    catalog = component(commands=native_commands()).catalog
    keys = {catalog.guide_id(player(unit(card)), {"_GuideUnitIndex": 7}) for card in Cards().card_ids}
    assert keys == {1, 10, 20}
    assert catalog.audit([saved(key, "system_default") for key in sorted(keys)]) == ()
    assert catalog.audit([saved(30, "system_default")]) == (
        {"CardID": 30, "ConfigID": "system_default", "kinds": [NON_NORMALIZED_KEY]},)


def test_audit_follows_the_derived_category_set():
    opened = component(commands=native_commands(filter_script(items="通常,兵舎"))).catalog
    assert opened.audit([saved(0, "system_unit"), saved(0, "system_fix")]) == (
        {"CardID": 0, "ConfigID": "system_fix", "kinds": [PAGE_BACKGROUND]},)

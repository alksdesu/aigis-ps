from collections import Counter
from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import struct

import pytest

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.alxml import Container, ValueType, dumps, loads
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.drop_bonus import (
    RESOURCE, WEEKDAYS, DropBonus, DropBonusGrant, DropBonusTable, DropBonusUnavailable, apply, bonus_container,
    bonus_grant,
)
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.native_calendar import DAYS_PER_WEEK, SUNDAY, native_day, native_weekday
from aigis_ps.native_clock import NATIVE_EPOCH
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.public_tables import validate_public_master
from aigis_ps.resource_library import ResourceLibrary
from test_al_format import schema_bytes
from test_business_calendar import FIRST_CLIENT_DAY, LAST_CLIENT_DAY


INT32, FLOAT32 = 1, 4
PARAMETER_SIZES = {INT32: 8, FLOAT32: 12}
PACKING = {INT32: "i", FLOAT32: "f"}
CASTS = {INT32: int, FLOAT32: float}
NATIVE_COLUMNS = (("QuestID", INT32), ("ItemType", INT32), ("ItemID", INT32), ("Operation", INT32),
                  ("SunValue", FLOAT32), ("MonValue", FLOAT32), ("TueValue", FLOAT32), ("WedValue", FLOAT32),
                  ("ThuValue", FLOAT32), ("FriValue", FLOAT32), ("SatValue", FLOAT32))
VALUE_COLUMNS = tuple(name for name, code in NATIVE_COLUMNS if code == FLOAT32)
TABLE_INVALID = "quest_drop_bonus_table_invalid"
INVALID, UNSUPPORTED = ("quest_drop_bonus_invalid", 503), ("quest_drop_bonus_unsupported", 501)
COUNT_KEYS = ("rows", "quests", "indexed_quests", "duplicate_rows", "ignored_groups", "orphan_rows")
SOURCE, OTHER_SOURCE = "a" * 64, "b" * 64
CRYSTAL, CRYSTAL_TEXT, CRYSTAL_TYPE = "trade_item_002000", 1415358870, 1
RUBY, RUBY_TEXT = "gift_0002_ruby", 7
ITEMS = ({"ItemID": CRYSTAL, "TextID": CRYSTAL_TEXT}, {"ItemID": RUBY, "TextID": RUBY_TEXT})
QUESTS = frozenset(range(1, 10))
ORPHAN = 500
BANNER = (0.0, 1.5, 0.0, 1.5, 0.0, 0.0, 1.5)
DOUBLE = (2.0,) * 7
NAN, INF = float("nan"), float("inf")
FLOAT32_MAX = float.fromhex("0x1.fffffep+127")
MONDAY = datetime(2026, 11, 2, 12, tzinfo=JST)
SUNDAY_NIGHT = datetime(2026, 11, 1, 23, 59, 59, tzinfo=JST)
SECOND = timedelta(seconds=1)
UNIX_EPOCH_DAY = 719162
FIRST_CLIENT_MOMENT = datetime.combine(FIRST_CLIENT_DAY, time(), JST)
LAST_CLIENT_MOMENT = datetime.combine(LAST_CLIENT_DAY, time(23, 59, 59), JST)
ZONES = (timezone(timedelta(hours=-10)), timezone.utc, JST, timezone(timedelta(hours=14)))
CONSECUTIVE_PAIR = 7902
SEPARATED_PAIRS = (4156, *range(6196, 6214), 9044)
REPRODUCE_QUEST, MAIN_QUEST, ORPHAN_QUEST = 9032, 1001, 6774
MAIN_TYPE, REPRODUCE_TYPE, HISTORY_TYPE = 0, 8, 16


def row(quest_id, *, item_type=CRYSTAL_TYPE, item_id=0, operation=1, values=BANNER):
    return {"QuestID": quest_id, "ItemType": item_type, "ItemID": item_id, "Operation": operation,
            **dict(zip(VALUE_COLUMNS, values))}


def table_bytes(rows, columns=NATIVE_COLUMNS):
    stride = 4 * len(columns)
    schema = schema_bytes([(name, 4 * index, code, b"\0" * PARAMETER_SIZES[code])
                           for index, (name, code) in enumerate(columns)], stride)
    record = struct.Struct("<" + "".join(PACKING[code] for _, code in columns))
    return (struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(rows), 20, 20 + len(schema), stride, b"dbon") + schema
            + b"".join(record.pack(*(CASTS[code](values.get(name, 0)) for name, code in columns)) for values in rows))


def build(*rows, columns=NATIVE_COLUMNS, items=ITEMS, source=SOURCE):
    return DropBonusTable.from_table(parse_table(table_bytes(rows, columns)), quest_ids=QUESTS, item_rows=list(items),
                                     sha256=source)


def bonus(quest_id, values=BANNER):
    return DropBonus(quest_id, CRYSTAL_TYPE, CRYSTAL, 1, values)


def counts(table):
    status = table.status()
    return {key: status[key] for key in COUNT_KEYS}


def fail(code, status, callback):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == (code, status)


@pytest.mark.parametrize(("moment", "civil", "weekday"), [
    (MONDAY, date(2026, 11, 2), 2),
    (SUNDAY_NIGHT, date(2026, 11, 1), 1),
    (SUNDAY_NIGHT + SECOND, date(2026, 11, 2), 2),
    (datetime(2026, 11, 7, 23, 59, 59, tzinfo=JST), date(2026, 11, 7), 7),
    (datetime(2026, 11, 1, 14, 59, 59, tzinfo=timezone.utc), date(2026, 11, 1), 1),
    (datetime(2026, 11, 1, 15, tzinfo=timezone.utc), date(2026, 11, 2), 2),
    (datetime(2026, 11, 2, 1, tzinfo=timezone(timedelta(hours=10))), date(2026, 11, 2), 2),
])
def test_native_calendar_counts_jst_days_and_numbers_weekdays_from_sunday(moment, civil, weekday):
    assert native_day(moment) == civil.toordinal() - 1
    assert native_weekday(moment) == weekday


def test_native_days_start_on_the_first_client_day():
    assert native_day(datetime(1970, 1, 1, tzinfo=JST)) == UNIX_EPOCH_DAY
    assert native_day(datetime(1970, 1, 1, tzinfo=timezone.utc)) == UNIX_EPOCH_DAY
    fail("quest_clock_invalid", 503, lambda: native_day(datetime(1, 1, 1, tzinfo=JST)))


@pytest.mark.parametrize("moment", [
    FIRST_CLIENT_MOMENT, SUNDAY_NIGHT, SUNDAY_NIGHT + SECOND, datetime(2028, 2, 29, 23, 59, 59, tzinfo=JST),
    datetime(2028, 3, 1, tzinfo=JST), LAST_CLIENT_MOMENT,
])
def test_native_calendar_matches_the_jst_date_formula_inside_the_client_calendar(moment):
    for shown in (moment.astimezone(zone) for zone in ZONES):
        assert native_day(shown) == (shown.astimezone(JST).date() - NATIVE_EPOCH.date()).days
        assert native_weekday(shown) == shown.astimezone(JST).isoweekday() % DAYS_PER_WEEK + SUNDAY


@pytest.mark.parametrize("moment", [
    MONDAY.replace(tzinfo=None), date(2026, 11, 2), None, MONDAY.isoformat(),
    datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=10))), datetime.max.replace(tzinfo=timezone.utc),
    FIRST_CLIENT_MOMENT - SECOND, LAST_CLIENT_MOMENT + SECOND,
])
def test_native_calendar_refuses_naive_unrepresentable_or_out_of_calendar_moments(moment):
    for function in (native_day, native_weekday):
        fail("quest_clock_invalid", 503, lambda: function(moment))


def test_consecutive_rows_form_the_group_and_its_first_row_per_item_type_wins():
    table = build(row(1), row(1, values=DOUBLE), row(2), row(3, values=DOUBLE), row(2, values=DOUBLE), row(3))
    assert (table.lookup(1), table.lookup(2), table.lookup(3)) == (bonus(1), bonus(2), bonus(3, DOUBLE))
    assert counts(table) == {"rows": 6, "quests": 3, "indexed_quests": 3, "duplicate_rows": 1, "ignored_groups": 2,
                             "orphan_rows": 0}


def test_rows_the_client_skips_are_counted_but_never_validated():
    table = build(row(1), row(1, operation=2, values=(NAN,) * 7), row(2), row(1, item_type=2),
                  row(ORPHAN, item_id=5, values=(INF,) * 7))
    assert (table.lookup(1), table.lookup(2), table.lookup(ORPHAN)) == (bonus(1), bonus(2), None)
    assert table.quest_ids == {1, 2} and table.status()["failures"] == {}
    assert counts(table) == {"rows": 5, "quests": 3, "indexed_quests": 2, "duplicate_rows": 1, "ignored_groups": 1,
                             "orphan_rows": 1}


@pytest.mark.parametrize(("rows", "fault"), [
    ((row(2, values=(NAN, *BANNER[1:])),), INVALID),
    ((row(2, values=(*BANNER[:-1], INF)),), INVALID),
    ((row(2, values=(0.0, -1.5, *BANNER[2:])),), INVALID),
    ((row(2, operation=2),), UNSUPPORTED),
    ((row(2, item_id=5),), UNSUPPORTED),
    ((row(2, item_type=2),), UNSUPPORTED),
    ((row(2), row(2, item_type=2)), UNSUPPORTED),
    ((row(2, item_type=2), row(2, values=(NAN,) * 7)), INVALID),
])
def test_a_faulty_first_group_closes_only_its_own_quest(rows, fault):
    table = build(row(1), *rows, row(3, values=DOUBLE))
    assert (table.lookup(1), table.lookup(3)) == (bonus(1), bonus(3, DOUBLE))
    fail(*fault, lambda: table.lookup(2))
    assert table.available and table.quest_ids == {1, 2, 3}
    assert table.status()["failures"] == {2: fault[0]}


@pytest.mark.parametrize("items", [
    (),
    ({"ItemID": RUBY, "TextID": RUBY_TEXT},),
    ({"ItemID": CRYSTAL, "TextID": CRYSTAL_TEXT}, {"ItemID": "trade_item_002001", "TextID": CRYSTAL_TEXT}),
    ({"ItemID": "", "TextID": CRYSTAL_TEXT},),
    ({"ItemID": CRYSTAL, "TextID": str(CRYSTAL_TEXT)},),
])
def test_item_type_one_must_name_exactly_one_item_config_row(items):
    table = build(row(1), row(2, values=DOUBLE), items=items)
    for quest_id in (1, 2):
        fail(*UNSUPPORTED, lambda: table.lookup(quest_id))
    assert table.status()["failures"] == {1: UNSUPPORTED[0], 2: UNSUPPORTED[0]}


@pytest.mark.parametrize("columns", [
    NATIVE_COLUMNS[:-1],
    (*NATIVE_COLUMNS[:4], ("SunValue", INT32), *NATIVE_COLUMNS[5:]),
    (("QuestID", FLOAT32), *NATIVE_COLUMNS[1:]),
    (("Quest", INT32), *NATIVE_COLUMNS[1:]),
    (NATIVE_COLUMNS[1], NATIVE_COLUMNS[0], *NATIVE_COLUMNS[2:]),
    (*NATIVE_COLUMNS, ("Extra", INT32)),
])
def test_a_table_outside_the_native_layout_is_unavailable_and_grants_no_bonus(columns):
    with pytest.raises(DropBonusUnavailable) as caught:
        build(row(1), columns=columns)
    assert caught.value.code == TABLE_INVALID
    table = DropBonusTable.unavailable(caught.value.code, str(caught.value), sha256=SOURCE)
    assert not table.available and table.lookup(1) is None and table.quest_ids == frozenset()
    status = table.status()
    assert status["error"] == {"code": TABLE_INVALID, "message": str(caught.value)} and status["sha256"] == SOURCE
    assert counts(table) == dict.fromkeys(COUNT_KEYS, 0)
    assert (status["failures"], status["weekday_profiles"]) == ({}, [])
    assert bonus_grant(table.lookup(1), MONDAY, {CRYSTAL: 3}) == DropBonusGrant(0.0, {})


def _parsed_variants():
    parsed = parse_table(table_bytes([row(1)]))
    native = parsed["rows"][0]
    return [None, "ALTB", {}, {"fields": parsed["fields"]}, {"fields": [], "rows": []},
            {**parsed, "rows": [{**native, "QuestID": "1"}]},
            {**parsed, "rows": [{**native, "ItemType": 2 ** 31}]},
            {**parsed, "rows": [{name: value for name, value in native.items() if name != "SatValue"}]},
            {**parsed, "rows": [list(native.values())]}]


@pytest.mark.parametrize("parsed", _parsed_variants())
def test_a_parsed_table_that_does_not_match_its_columns_is_unavailable(parsed):
    with pytest.raises(DropBonusUnavailable) as caught:
        DropBonusTable.from_table(parsed, quest_ids=QUESTS, item_rows=list(ITEMS), sha256=SOURCE)
    assert caught.value.code == TABLE_INVALID


@pytest.mark.parametrize("change", [
    {"sha256": "A" * 64}, {"sha256": None}, {"sha256": "a" * 63}, {"quest_ids": "123"}, {"quest_ids": [1, "2"]},
    {"quest_ids": [True]}, {"quest_ids": None}, {"item_rows": None}, {"item_rows": {"ItemID": CRYSTAL}},
])
def test_table_construction_requires_native_arguments(change):
    arguments = {"quest_ids": QUESTS, "item_rows": list(ITEMS), "sha256": SOURCE} | change
    with pytest.raises(ValueError) as caught:
        DropBonusTable.from_table(parse_table(table_bytes([row(1)])), **arguments)
    assert not isinstance(caught.value, DropBonusUnavailable)


@pytest.mark.parametrize(("code", "detail", "source"), [
    ("", "missing", None), ("quest_asset_unavailable", "", None), (None, "missing", None),
    ("quest_asset_unavailable", None, None), ("quest_asset_unavailable", "missing", "A" * 64),
])
def test_an_unavailable_table_requires_a_reason(code, detail, source):
    with pytest.raises(ValueError):
        DropBonusTable.unavailable(code, detail, sha256=source)


def test_an_empty_native_table_is_available_without_bonuses():
    table = build()
    assert table.available and table.lookup(1) is None and table.quest_ids == frozenset()
    assert counts(table) == dict.fromkeys(COUNT_KEYS, 0)


@pytest.mark.parametrize("value", ["1", 1.0, True, None, 2 ** 31])
def test_lookup_requires_a_native_quest_identifier(value):
    with pytest.raises(ValueError):
        build(row(1)).lookup(value)


def test_status_reports_profiles_and_a_content_version_that_follows_the_effective_table():
    table = build(row(1), row(2), row(3, values=DOUBLE), row(4, operation=2))
    status = table.status()
    assert status["available"] and status["error"] is None and status["sha256"] == SOURCE
    assert status["weekday_profiles"] == [
        {"item_id": CRYSTAL, "values": dict(zip(WEEKDAYS, BANNER)), "quests": 2},
        {"item_id": CRYSTAL, "values": dict(zip(WEEKDAYS, DOUBLE)), "quests": 1}]
    assert status["content_version"] == table.content_version
    assert json.loads(json.dumps(status, allow_nan=False))["failures"] == {"4": UNSUPPORTED[0]}
    same = build(row(1), row(2), row(3, values=DOUBLE), row(4, operation=2))
    assert same.content_version == table.content_version
    for other in (build(row(1), row(2), row(3), row(4, operation=2)), build(row(1), row(2), row(3, values=DOUBLE)),
                  build(row(1), row(2), row(3, values=DOUBLE), row(4, operation=2), source=OTHER_SOURCE),
                  DropBonusTable.unavailable(TABLE_INVALID, "broken", sha256=SOURCE)):
        assert other.content_version != table.content_version


@pytest.mark.parametrize(("value", "base", "credited"), [
    (1.5, 3, 4), (1.5, 5, 7), (1.5, 8, 12), (0, 6, 6), (0.0, 6, 6), (-0.0, 6, 6), (0.5, 4, 4), (1.5, 0, 0),
    (1.5, 1, 1), (2.0, 9999, 19998), (4 / 3, 3, 3), (2.0, INT32_MAX // 2, INT32_MAX - 1),
    (2.0, INT32_MAX // 2 + 1, INT32_MAX), (2.0 ** 40, 9999, INT32_MAX),
])
def test_apply_floors_the_exact_product_up_to_int32_max_and_never_lowers_the_count(value, base, credited):
    assert apply(value, base) == credited


@pytest.mark.parametrize(("value", "base"), [(NAN, 3), (INF, 3), (True, 3), ("1.5", 3), (1.5, -1), (1.5, 2.0),
                                             (1.5, True)])
def test_apply_refuses_values_outside_its_contract(value, base):
    with pytest.raises(ValueError):
        apply(value, base)


def test_bonus_grant_multiplies_the_summed_slot_drops_of_its_item_once():
    grant = bonus_grant(bonus(1), MONDAY, {CRYSTAL: 3 + 5, RUBY: 2})
    assert grant == DropBonusGrant(1.5, {CRYSTAL: 4}, (bonus_container(1.5, CRYSTAL_TYPE),))
    assert json.loads(json.dumps(grant.receipt(), allow_nan=False)) == {"value": 1.5, "increments": {CRYSTAL: 4}}


def test_bonus_grant_credits_the_largest_float32_value_up_to_int32_max():
    largest = bonus(1, (FLOAT32_MAX,) * 7)
    grant = bonus_grant(largest, MONDAY, {CRYSTAL: 3, RUBY: 2})
    assert json.loads(json.dumps(grant.receipt(), allow_nan=False)) == {
        "value": FLOAT32_MAX, "increments": {CRYSTAL: INT32_MAX - 3}}
    assert bonus_grant(largest, MONDAY, {CRYSTAL: INT32_MAX}).increments == {}


def test_a_positive_day_reports_bonus_even_without_the_bonus_item():
    grant = bonus_grant(bonus(1), MONDAY, {RUBY: 2})
    assert (grant.value, dict(grant.increments), grant.children) == (1.5, {}, (bonus_container(1.5, CRYSTAL_TYPE),))
    assert bonus_grant(bonus(1), MONDAY, {CRYSTAL: 1}).increments == {}


@pytest.mark.parametrize(("subject", "moment"), [
    (bonus(1), SUNDAY_NIGHT), (None, MONDAY), (bonus(1, (0.0,) * 7), MONDAY), (bonus(1, (-0.0,) * 7), MONDAY),
])
def test_a_zero_day_or_a_quest_without_rows_grants_nothing(subject, moment):
    grant = bonus_grant(subject, moment, {CRYSTAL: 3})
    assert (grant.value, dict(grant.increments), grant.children) == (0.0, {}, ())
    assert grant.receipt() == {"value": 0.0, "increments": {}}


def test_the_bonus_day_turns_at_jst_midnight():
    subject = bonus(1)
    assert subject.value_on(SUNDAY_NIGHT) == 0.0 and subject.value_on(SUNDAY_NIGHT + SECOND) == 1.5
    assert subject.value_on(datetime(2026, 11, 1, 15, tzinfo=timezone.utc)) == 1.5
    assert [subject.value_on(MONDAY + timedelta(days=offset)) for offset in range(-1, 6)] == list(BANNER)


def test_bonus_grant_refuses_arguments_outside_its_contract():
    for subject, items in (({"quest_id": 1}, {CRYSTAL: 3}), (bonus(1), [(CRYSTAL, 3)]), (bonus(1), {CRYSTAL: 3.0}),
                           (bonus(1), {CRYSTAL: -1})):
        with pytest.raises(ValueError):
            bonus_grant(subject, MONDAY, items)
    fail("quest_clock_invalid", 503, lambda: bonus_grant(bonus(1), MONDAY.replace(tzinfo=None), {CRYSTAL: 3}))


def test_bonus_container_carries_the_value_as_a_float_column():
    data = dumps(Container("DA", (bonus_container(1.5, CRYSTAL_TYPE),)))
    assert b'<BONUS><ItemType T="I"><V>1</V></ItemType><Value T="F"><V>1.5</V></Value></BONUS>' in data
    (parsed,) = loads(data).children
    assert [(column.name, column.type, column.values) for column in parsed.children] == [
        ("ItemType", ValueType.INT, (1,)), ("Value", ValueType.FLOAT, (1.5,))]


@pytest.mark.parametrize("change", [
    {"weekday_values": BANNER[:-1]}, {"weekday_values": (*BANNER, 1.5)}, {"weekday_values": (NAN, *BANNER[1:])},
    {"weekday_values": (-1.5, *BANNER[1:])}, {"weekday_values": (1, *BANNER[1:])},
    {"weekday_values": (1.1, *BANNER[1:])}, {"weekday_values": "1.5"}, {"item_id": ""}, {"item_id": None},
    {"quest_id": "1"}, {"item_type": True}, {"operation": 2 ** 31},
])
def test_drop_bonus_records_hold_native_identifiers_and_seven_float32_values(change):
    fields = {"quest_id": 1, "item_type": CRYSTAL_TYPE, "item_id": CRYSTAL, "operation": 1, "weekday_values": BANNER}
    with pytest.raises(ValueError):
        DropBonus(**(fields | change))


def test_bonus_records_and_grants_are_immutable():
    subject = DropBonus(1, CRYSTAL_TYPE, CRYSTAL, 1, list(BANNER))
    assert subject == bonus(1) and subject.weekday_values == BANNER
    with pytest.raises(FrozenInstanceError):
        subject.weekday_values = DOUBLE
    grant = bonus_grant(subject, MONDAY, {CRYSTAL: 2})
    with pytest.raises(TypeError):
        grant.increments[CRYSTAL] = 9
    with pytest.raises(FrozenInstanceError):
        grant.value = 2.0


@pytest.fixture(scope="module")
def real():
    project = Path(__file__).resolve().parents[1]
    executable = project.parent / "aigisr_exe"
    if not (project / "runtime/server/masterdata/active.json").is_file() or not executable.is_dir():
        pytest.skip("Verified local master snapshot and client resources are required")
    master = MasterDataStore(project / "runtime/server/masterdata").snapshot()
    _, entries = ResourceLibrary(project / "runtime/resources", client_hash=master.client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", executable)

    def resource(name):
        data = assets.read(name)[0]
        return decompress_all4(data) if data.startswith(b"ALL4") else data

    schemas = json.loads(files("aigis_ps").joinpath("data/master-schemas.json").read_text("utf-8"))
    fields = {name: [field["name"] for field in table["fields"]] for name, table in schemas["tables"].items()}
    document, _ = validate_public_master("QuestTable", master.get_xml("QuestTable"), fields)
    columns = {column.name: column.values for column in document.children}
    types = dict(zip(columns["QuestID"], columns["Type"]))
    raw = resource(RESOURCE)
    parsed, items = parse_table(raw), parse_table(resource("ItemConfigTable.atb"))
    table = DropBonusTable.from_table(parsed, quest_ids=types, item_rows=items["rows"],
                                      sha256=hashlib.sha256(raw).hexdigest())
    return table, types, parsed["rows"]


def test_real_table_indexes_quest_table_rows_and_counts_the_rest(real):
    table, types, _ = real
    status = table.status()
    assert status["available"] and status["error"] is None and status["failures"] == {}
    assert counts(table) == {"rows": 2141, "quests": 2120, "indexed_quests": 2022, "duplicate_rows": 1,
                             "ignored_groups": 20, "orphan_rows": 98}
    assert Counter(types[quest_id] for quest_id in table.quest_ids) == {HISTORY_TYPE: 2003, REPRODUCE_TYPE: 19}
    assert status["weekday_profiles"] == [{"item_id": CRYSTAL, "values": dict(zip(WEEKDAYS, BANNER)), "quests": 2022}]


def test_real_double_row_quests_keep_their_first_consecutive_group(real):
    table, _, rows = real
    totals = Counter(row["QuestID"] for row in rows)
    consecutive = {left["QuestID"] for left, right in zip(rows, rows[1:]) if left["QuestID"] == right["QuestID"]}
    assert {quest_id for quest_id, total in totals.items() if total > 1} == {CONSECUTIVE_PAIR, *SEPARATED_PAIRS}
    assert max(totals.values()) == 2 and consecutive == {CONSECUTIVE_PAIR}
    for quest_id in (CONSECUTIVE_PAIR, *SEPARATED_PAIRS):
        assert table.lookup(quest_id) == bonus(quest_id)


def test_real_bonuses_multiply_carved_crystals_on_monday_wednesday_and_saturday(real):
    table, types, _ = real
    subject = table.lookup(REPRODUCE_QUEST)
    assert types[REPRODUCE_QUEST] == REPRODUCE_TYPE and subject == bonus(REPRODUCE_QUEST)
    assert [subject.value_on(MONDAY + timedelta(days=offset)) for offset in range(-1, 6)] == list(BANNER)
    assert bonus_grant(subject, MONDAY, {CRYSTAL: 3}).receipt() == {"value": 1.5, "increments": {CRYSTAL: 1}}
    assert bonus_grant(subject, SUNDAY_NIGHT, {CRYSTAL: 3}).receipt() == {"value": 0.0, "increments": {}}
    assert types[MAIN_QUEST] == MAIN_TYPE and table.lookup(MAIN_QUEST) is None
    assert ORPHAN_QUEST not in types and ORPHAN_QUEST not in table.quest_ids and table.lookup(ORPHAN_QUEST) is None

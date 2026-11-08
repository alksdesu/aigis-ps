from dataclasses import dataclass
import hashlib
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.asset_service import ASSET_CHANGED, ASSET_NOT_CACHED, IndexedAssetService
from aigis_ps.errors import ProtocolError
from aigis_ps.native_commands import ABSENT_TABLE, REGISTERED_TABLES, STATUS_LIMIT
from aigis_ps.resource_fetch import ResourceEntry
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.system_literals import command_id, encode_command
from aigis_ps.universal_flags import MAX_FLAG_NAME_BYTES, flag_hash
from aigis_ps.universal_flags_catalog import (
    CATALOG_CONFLICT, CATALOG_INVALID, COMMAND_COLUMN, COMMAND_KEY, EMPTY_NAME, FAMILIES, FILTER_COMMAND,
    FILTER_GROUP, HISTORY_GROUP, HOME_TRIGGER, MISSION_HISTORY, MISSION_PORTAL, MISSION_PORTAL_FAMILY,
    NONMONOTONIC_VALUE, OVERSIZED_NAME, PAYMENT_BONUS, PROTECTED_PREFIXES, RECOMMEND_KEY, RECOMMEND_MENU,
    RECOMMENDED_READ, ROTATION_TRIGGER, SCRIPT, SCRIPT_COLUMNS, SKIN_SHOP, SOURCE_FIELDS, FlagSource,
    UniversalFlagsCatalog)
from test_present_items import item_table


SCHEMAS = {
    HOME_TRIGGER: (("ID", 1), ("DateOpen", 32), ("DateClose", 32), ("Expression", 32), ("Cmnd", 32)),
    RECOMMEND_MENU: (("ConfigID", 1), ("RegistID", 1), ("DisplayText", 32), ("CompleteExpression", 32),
                     ("AfterCommand", 32), ("DateOpen", 32), ("DateClose", 32), ("TriggerGroup", 32),
                     ("UniversalFlag", 32)),
    MISSION_PORTAL: (("RecordIndex", 1), ("ID", 1), ("TargetNode", 32), ("FlagName", 32)),
    MISSION_HISTORY: (("RecordIndex", 1), ("GroupId", 1), ("DisclosureConditionDays", 1)),
    ROTATION_TRIGGER: (("ID", 1), ("Command", 32)),
    FILTER_COMMAND: (("FilterCommandConfigHashId", 1), ("SaveId", 1), ("Expression", 32)),
    FILTER_GROUP: (("ConfigId", 1), ("DisplayExpression", 32), ("ExecuteExpression", 32)),
    SKIN_SHOP: (("ButtonImageId", 1), ("PurchaseFlag", 1)),
    PAYMENT_BONUS: (("ConfigId", 1), ("RequiredAmount", 1)),
}
COMMAND_SCHEMA = (("_ID", 1), (COMMAND_COLUMN, 32))
PORTAL_COMMANDS = "OnCommandMissionPortal.atb"
ENCODED = {(table, column): key for table, column, key in SCRIPT_COLUMNS} | {
    (RECOMMEND_MENU, column): RECOMMEND_KEY for column in ("DisplayText", "TriggerGroup", "UniversalFlag")}
HOME_COMMAND = ('SetUniversalFlags("Flags=OpenUnitLibrarySystem","Value=1");\nSaveUniversalFlags();\n'
                'CMenuTask_ShowDialog("Text=FameOpenComplete");')
HOME_EXPRESSION = '(GetRank() >= 50) && !IsEnableUniversalFlags("Flags=OpenUnitLibrarySystem")'
COLLIDING = ("plumless", "buckeroo")
PAYMENT_ALIAS = ("PaymentBonusObtained:id=1", "bonus_alias_01140_fVTz")
CLIENT = CURRENT_CLIENT_SHA256


@dataclass(frozen=True)
class Raw:
    text: str


def masked(text, key):
    mask = zlib.crc32(key.encode()).to_bytes(4, "big")
    return bytes(value ^ mask[index % 4] for index, value in enumerate(text.encode())).hex()


def encode(table, column, value):
    if isinstance(value, Raw):
        return value.text
    key = ENCODED.get((table, column), COMMAND_KEY if column == COMMAND_COLUMN else None)
    if key is None or not value:
        return value
    return encode_command(value) if key == COMMAND_KEY else masked(value, key)


def recommend(group, flag, **changes):
    row = {"ConfigID": command_id(flag or group), "RegistID": 1, "DisplayText": "名声召喚\n最高グレードで召喚",
           "CompleteExpression": "IsThereUpperClassOfGlory() || !CanSummonOfGlory();",
           "AfterCommand": 'SetUpdaterFunc( "CMenuGlory::Func_Init" );', "DateOpen": "2019/02/21 00:00:00",
           "DateClose": "2019/03/21 00:00:00", "TriggerGroup": group, "UniversalFlag": flag}
    row.update(changes)
    return row


def portal(index, type_name, flag):
    return {"RecordIndex": index, "ID": command_id(type_name), "TargetNode": f"SP{49 + index}", "FlagName": flag}


def history(index, group, days):
    return {"RecordIndex": index, "GroupId": group, "DisclosureConditionDays": days}


def home(command=HOME_COMMAND, expression=HOME_EXPRESSION, identifier=1):
    return {"ID": identifier, "DateOpen": "2024/11/14 12:00:00", "DateClose": "", "Expression": expression, "Cmnd": command}


def plain_rows(**changes):
    rows = {
        HOME_TRIGGER: [home()],
        RECOMMEND_MENU: [recommend("召喚[名声]", "test_flag_002"), recommend("限定商品", "test_flag_011")],
        MISSION_PORTAL: [portal(4, "History", "mission_portal_sp53_20230921"), portal(5, "GoddessRevival", "")],
        MISSION_HISTORY: [history(1, 1, 1), history(2, 1, 1), history(3, 3, 60)],
        ROTATION_TRIGGER: [{"ID": 1, "Command": 'CMenuTask_CallCommand("SetAlreadyFlag","typeName=History");'}],
        FILTER_COMMAND: [{"FilterCommandConfigHashId": 1, "SaveId": 0, "Expression": 'IsRating("ALL")'}],
        FILTER_GROUP: [{"ConfigId": 1, "DisplayExpression": "0;", "ExecuteExpression": "1;"}],
        SKIN_SHOP: [{"ButtonImageId": 1, "PurchaseFlag": flag_hash("PurchasedSkin_0001")},
                    {"ButtonImageId": 2, "PurchaseFlag": flag_hash("PurchasedSkin_0002")}],
        PAYMENT_BONUS: [{"ConfigId": 1, "RequiredAmount": 500}, {"ConfigId": 2, "RequiredAmount": 500}],
    }
    rows.update(changes)
    return rows


def command_tables(*entries):
    return {PORTAL_COMMANDS: list(entries) or [("CMissionPortalMenu::OnCommand", "SaveUniversalFlags();")]}


def build(table, rows, schema=None):
    return item_table([{column: encode(table, column, value) for column, value in row.items()} for row in rows],
                      schema or SCHEMAS[table])


def resources(rows=None, commands=None):
    payloads = {table: build(table, table_rows) for table, table_rows in (rows or plain_rows()).items()}
    for table, entries in (command_tables() if commands is None else commands).items():
        payloads[table] = item_table([{"_ID": command_id(name), COMMAND_COLUMN: encode(table, COMMAND_COLUMN, text)}
                                      for name, text in entries], COMMAND_SCHEMA)
    return payloads


def reader(payloads):
    def read(name):
        if name in payloads:
            return payloads[name]
        raise ProtocolError(ABSENT_TABLE, "Resource is not in the current public index", 404)
    return read


def indexed_reader(directory, payloads, uncached=()):
    cache = directory / ".cache"
    cache.mkdir(parents=True)
    entries = [ResourceEntry(name, "a" * 40, hashlib.md5(raw).hexdigest(), 5, len(raw))
               for name, raw in payloads.items()]
    for entry in entries:
        (cache / entry.cache_key).write_bytes(payloads[entry.name])
    entries += [ResourceEntry(name, "a" * 40, hashlib.md5(name.encode()).hexdigest(), 5, len(name))
                for name in uncached]
    assets = IndexedAssetService(entries, directory, directory)
    return lambda name: assets.read(name)[0]


def catalog(rows=None, commands=None):
    return UniversalFlagsCatalog(reader(resources(rows, commands)))


def failure(payloads, code=CATALOG_INVALID):
    with pytest.raises(ProtocolError) as error:
        UniversalFlagsCatalog(reader(payloads))
    assert error.value.code == code and error.value.status == 503
    return error.value


def scripted(text, column="Cmnd"):
    row = home(command=text if column == "Cmnd" else HOME_COMMAND, expression=text if column == "Expression" else "")
    return plain_rows(**{HOME_TRIGGER: [row]})


def names(result, family):
    return {name for name, definition in result.definitions.items() if family in definition.families}


def test_tables_derive_each_family_from_its_native_writer():
    result = catalog()
    assert set(result.definitions) == {"OpenUnitLibrarySystem", "mission_portal_sp53_20230921",
                                       "AppeardHistoryGroupFlag_1", "AppeardHistoryGroupFlag_3",
                                       "test_flag_002", "test_flag_011"}
    assert all(definition.values == frozenset((1,)) for definition in result.definitions.values())
    assert result.get("OpenUnitLibrarySystem").sources == (FlagSource(SCRIPT, HOME_TRIGGER, "Cmnd", 0),)
    assert result.get("mission_portal_sp53_20230921").sources == (
        FlagSource(MISSION_PORTAL_FAMILY, MISSION_PORTAL, "FlagName", 0),)
    assert result.get("AppeardHistoryGroupFlag_1").sources == (FlagSource(HISTORY_GROUP, MISSION_HISTORY, "GroupId", 0),)
    assert result.get("AppeardHistoryGroupFlag_3").sources == (FlagSource(HISTORY_GROUP, MISSION_HISTORY, "GroupId", 2),)
    assert result.get("test_flag_011").sources == (FlagSource(RECOMMENDED_READ, RECOMMEND_MENU, "UniversalFlag", 1),)
    status = result.status()
    assert status["ready"] is True and status["name_count"] == 6
    assert status["families"] == {
        SCRIPT: {"count": 1, "names": ["OpenUnitLibrarySystem"]},
        MISSION_PORTAL_FAMILY: {"count": 1, "names": ["mission_portal_sp53_20230921"]},
        HISTORY_GROUP: {"count": 2, "names": ["AppeardHistoryGroupFlag_1", "AppeardHistoryGroupFlag_3"]},
        RECOMMENDED_READ: {"count": 2, "names": ["test_flag_002", "test_flag_011"]}}
    assert list(status["families"]) == list(FAMILIES)
    assert status["dynamic_writers"] == {"count": 0, "positions": []}
    assert status["excluded_writes"] == {"count": 0, "entries": []}
    assert status["protected"] == {"prefixes": list(PROTECTED_PREFIXES), "skin_purchase_count": 2,
                                   "payment_bonus_names": ["PaymentBonusObtained:id=1", "PaymentBonusObtained:id=2"]}
    assert status["command_tables"] == 1 and len(status["absent_command_tables"]) == len(REGISTERED_TABLES) - 1
    assert PORTAL_COMMANDS not in status["absent_command_tables"]
    assert set(status["resource_sha256"]) == set(SOURCE_FIELDS)
    assert status["script_columns"] == [list(column) for column in SCRIPT_COLUMNS]


def test_recommended_read_flags_cover_every_row_and_ignore_display_columns():
    hidden = 'SetUniversalFlags("Flags=FromDisplay","Value=1");'
    rows = [recommend("召喚[プレミアム]", "test_flag_000", DateClose="2019/03/21 00:00:00"),
            recommend(hidden, "test_flag_013", DisplayText=hidden), recommend("召喚[プレミアム2]", "")]
    result = catalog(plain_rows(**{RECOMMEND_MENU: rows}))
    assert names(result, RECOMMENDED_READ) == {"test_flag_000", "test_flag_013"}
    assert "FromDisplay" not in result.definitions
    assert result.status()["excluded_writes"] == {"count": 1, "entries": [
        {"family": RECOMMENDED_READ, "table": RECOMMEND_MENU, "column": "UniversalFlag", "row": 2,
         "reason": EMPTY_NAME, "name": ""}]}


def test_portal_rows_without_flag_names_are_already_read_and_never_written():
    result = catalog(plain_rows(**{MISSION_PORTAL: [portal(1, "Event", ""), portal(2, "Vote", "")]}))
    assert names(result, MISSION_PORTAL_FAMILY) == set()
    assert result.status()["excluded_writes"]["count"] == 0


def test_history_groups_use_the_decimal_group_and_its_first_row():
    rows = [history(1, 12, 330), history(2, -4, 1), history(3, 12, 330), history(4, 0, 1)]
    result = catalog(plain_rows(**{MISSION_HISTORY: rows}))
    assert names(result, HISTORY_GROUP) == {"AppeardHistoryGroupFlag_12", "AppeardHistoryGroupFlag_-4",
                                            "AppeardHistoryGroupFlag_0"}
    assert result.get("AppeardHistoryGroupFlag_12").sources == (FlagSource(HISTORY_GROUP, MISSION_HISTORY, "GroupId", 0),)


@pytest.mark.parametrize("text,expected", [
    ('SetUniversalFlags("Flags=A","Value=1");', {"A": {1}}),
    ('SetUniversalFlags ( "Value=2" ,\n\t"Flags=A" ) ;', {"A": {2}}),
    ('if( IsX() ){\r\n  SetUniversalFlags("Flags=A","Value=-1");\n}', {"A": {-1}}),
    ('SetUniversalFlags("Flags=A","Value=1");SetUniversalFlags("Flags=A","Value=3");', {"A": {1, 3}}),
    ('SetUniversalFlags("Flags=Key=1","Value=2147483647");', {"Key=1": {2147483647}}),
    ('SetUniversalFlags("Flags=既読","Value=-2147483648");', {"既読": {-2147483648}}),
])
def test_literal_writes_keep_their_names_and_values(text, expected):
    result = catalog(scripted(text))
    assert {name: set(result.get(name).values) for name in names(result, SCRIPT)} == expected
    assert result.status()["dynamic_writers"]["count"] == 0


@pytest.mark.parametrize("text", [
    'SetUniversalFlags($Name,"Value=1");', 'SetUniversalFlags("Flags=" + $Name,"Value=1");',
    'SetUniversalFlags("Flags=A");', 'SetUniversalFlags("Flags=A","Value=01");',
    'SetUniversalFlags("Flags=A","Value=+1");', 'SetUniversalFlags("Flags=A","Value= 1");',
    'SetUniversalFlags("Flags=A","Value=2147483648");', 'SetUniversalFlags("Flags=A","Value=1","Save=1");',
    'SetUniversalFlags("Flags=A","Flags=B");', 'SetUniversalFlags("Flags=A","Mode=1");',
    'SetUniversalFlags("Flags=A\\"B","Value=1");', 'SetUniversalFlags("Flags","Value=1");',
    'CMenuTask_CallCommand("SetUniversalFlags","Flags=A");', 'SetUniversalFlags;',
])
def test_nonliteral_writes_are_reported_and_excluded(text):
    result = catalog(scripted(text))
    assert names(result, SCRIPT) == set()
    assert result.status()["dynamic_writers"] == {"count": 1, "positions": [
        {"family": SCRIPT, "table": HOME_TRIGGER, "column": "Cmnd", "row": 0}]}


@pytest.mark.parametrize("text", [
    'IsEnableUniversalFlags("Flags=A");', 'GetUniversalFlags("Flags=A");', "SaveUniversalFlags();",
    'ResetUniversalFlags("Flags=A","Value=1");', 'SetUniversalFlagsEx("Flags=A","Value=1");',
    'PreSetUniversalFlags("Flags=A","Value=1");', '_SetUniversalFlags("Flags=A","Value=1");',
    'Menu2SetUniversalFlags("Flags=A","Value=1");', 'MarkAsReadOfRecommendedFlags("Group=限定商品");',
])
def test_other_flag_commands_are_neither_writers_nor_dynamic(text):
    result = catalog(scripted(text))
    assert names(result, SCRIPT) == set()
    assert result.status()["dynamic_writers"]["count"] == 0


@pytest.mark.parametrize("name,value,reason", [
    ("", 1, EMPTY_NAME), ("A", 0, NONMONOTONIC_VALUE),
    ("x" * (MAX_FLAG_NAME_BYTES + 1), 1, OVERSIZED_NAME), ("界" * 85 + "xx", 1, OVERSIZED_NAME),
])
def test_unstorable_or_resetting_writes_are_excluded_and_reported(name, value, reason):
    result = catalog(scripted(f'SetUniversalFlags("Flags={name}","Value={value}");'))
    assert names(result, SCRIPT) == set()
    assert result.status()["excluded_writes"] == {"count": 1, "entries": [
        {"family": SCRIPT, "table": HOME_TRIGGER, "column": "Cmnd", "row": 0, "reason": reason,
         "name": None if reason == OVERSIZED_NAME else name}]}


@pytest.mark.parametrize("name", ["x" * MAX_FLAG_NAME_BYTES, "界" * 85])
def test_names_filling_the_native_cell_stay_writable(name):
    assert len(name.encode("utf-8")) == MAX_FLAG_NAME_BYTES
    assert catalog(scripted(f'SetUniversalFlags("Flags={name}","Value=1");')).get(name).values == {1}


def test_zero_literal_does_not_remove_the_same_name_from_other_writers():
    rows = plain_rows(**{HOME_TRIGGER: [home('SetUniversalFlags("Flags=test_flag_002","Value=0");')]})
    result = catalog(rows)
    assert result.get("test_flag_002").values == {1}
    assert result.status()["excluded_writes"]["entries"][0]["reason"] == NONMONOTONIC_VALUE


def test_every_script_column_is_scanned_with_its_own_key():
    def write(label):
        return f'SetUniversalFlags("Flags={label}","Value=1");'

    rows = plain_rows(**{
        HOME_TRIGGER: [home(command=write("home_command"), expression=write("home_expression"))],
        RECOMMEND_MENU: [recommend("限定商品", "test_flag_011", CompleteExpression=write("recommend_complete"),
                                   AfterCommand=write("recommend_after"))],
        ROTATION_TRIGGER: [{"ID": 1, "Command": write("rotation")}],
        FILTER_COMMAND: [{"FilterCommandConfigHashId": 1, "SaveId": 0, "Expression": write("filter_command")}],
        FILTER_GROUP: [{"ConfigId": 1, "DisplayExpression": write("filter_display"),
                        "ExecuteExpression": write("filter_execute")}],
    })
    result = catalog(rows, command_tables(("CMissionPortalMenu::OnCommand", "Wait();"),
                                          ("CMissionPortalMenu::OnSecond", write("command_table"))))
    expected = {"home_command": (HOME_TRIGGER, "Cmnd", 0), "home_expression": (HOME_TRIGGER, "Expression", 0),
                "recommend_complete": (RECOMMEND_MENU, "CompleteExpression", 0),
                "recommend_after": (RECOMMEND_MENU, "AfterCommand", 0), "rotation": (ROTATION_TRIGGER, "Command", 0),
                "filter_command": (FILTER_COMMAND, "Expression", 0), "filter_display": (FILTER_GROUP, "DisplayExpression", 0),
                "filter_execute": (FILTER_GROUP, "ExecuteExpression", 0),
                "command_table": (PORTAL_COMMANDS, COMMAND_COLUMN, 1)}
    assert {name: result.get(name).sources for name in names(result, SCRIPT)} == {
        name: (FlagSource(SCRIPT, *position),) for name, position in expected.items()}


def test_command_key_is_the_crc_of_its_name_like_the_other_keys():
    for text in ("SaveUniversalFlags();", "", 'SetUniversalFlags("Flags=既読","Value=1");'):
        assert masked(text, COMMAND_KEY) == encode_command(text)


@pytest.mark.parametrize("table,column,value", [
    (HOME_TRIGGER, "Cmnd", Raw("zz")), (HOME_TRIGGER, "Expression", Raw("abc")),
    (HOME_TRIGGER, "Cmnd", Raw("2f b9")), (RECOMMEND_MENU, "UniversalFlag", Raw(masked("x", "HomeTrigger")[:1])),
    (RECOMMEND_MENU, "UniversalFlag", Raw(bytes(byte ^ mask for byte, mask in zip(b"\xff\xfe", b"\x36\x09")).hex())),
    (RECOMMEND_MENU, "AfterCommand", "Wait();\x01"), (FILTER_GROUP, "ExecuteExpression", "1;\x7f"),
    (MISSION_PORTAL, "FlagName", "mission\x1bportal"), (MISSION_PORTAL, "FlagName", "mission\x85portal"),
    (ROTATION_TRIGGER, "Command", Raw("0g")),
])
def test_damaged_or_mismatched_text_fails_closed(table, column, value):
    rows = plain_rows()
    rows[table] = [dict(rows[table][0], **{column: value})]
    failure(resources(rows))


def test_damaged_command_cells_fail_closed():
    payloads = resources()
    payloads[PORTAL_COMMANDS] = item_table([{"_ID": 1, COMMAND_COLUMN: "not-hex"}], COMMAND_SCHEMA)
    failure(payloads)


def flipped(table):
    field = next(iter(SOURCE_FIELDS[table]))
    schema = tuple((name, 32 if code == 1 else 1) if name == field else (name, code) for name, code in SCHEMAS[table])
    row = {name: "" if code == 32 else 0 for name, code in schema}
    return item_table([row], schema)


@pytest.mark.parametrize("table", list(SOURCE_FIELDS))
@pytest.mark.parametrize("damage", ["missing", "flipped", "truncated", "text", "missing_column"])
def test_any_unreadable_source_table_fails_closed(table, damage):
    payloads = resources()
    if damage == "missing":
        del payloads[table]
    elif damage == "flipped":
        payloads[table] = flipped(table)
    elif damage == "truncated":
        payloads[table] = payloads[table][:40]
    elif damage == "text":
        payloads[table] = payloads[table].decode("latin-1")
    else:
        field = next(iter(SOURCE_FIELDS[table]))
        schema = tuple(entry for entry in SCHEMAS[table] if entry[0] != field)
        payloads[table] = item_table([{name: "" if code == 32 else 0 for name, code in schema}], schema)
    failure(payloads)


def test_reader_must_be_callable():
    with pytest.raises(ProtocolError) as error:
        UniversalFlagsCatalog(None)
    assert error.value.code == CATALOG_INVALID


def test_compressed_sources_are_read_like_the_client():
    payloads = resources()
    packed = {name: struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
              for name, raw in payloads.items()}
    assert decompress_all4(packed[HOME_TRIGGER]) == payloads[HOME_TRIGGER]
    plain, compressed = UniversalFlagsCatalog(reader(payloads)), UniversalFlagsCatalog(reader(packed))
    assert dict(plain.definitions) == dict(compressed.definitions)
    assert compressed.content_version != plain.content_version


def test_absent_command_tables_are_skipped_like_the_native_loader():
    result = catalog(commands={})
    assert result.status()["command_tables"] == 0
    assert result.status()["absent_command_tables"] == sorted(REGISTERED_TABLES)
    assert "OpenUnitLibrarySystem" in result.definitions


def test_indexed_command_tables_without_a_cached_copy_make_the_catalog_unavailable(tmp_path):
    payloads = resources()
    unindexed = next(name for name in REGISTERED_TABLES if name not in payloads)
    absent = UniversalFlagsCatalog(indexed_reader(tmp_path / "absent", payloads))
    assert unindexed in absent.status()["absent_command_tables"]
    assert absent.content_version == catalog().content_version
    for table in (unindexed, PORTAL_COMMANDS):
        read = indexed_reader(tmp_path / table, {name: raw for name, raw in payloads.items() if name != table}, (table,))
        with pytest.raises(ProtocolError) as cause:
            read(table)
        assert (cause.value.code, cause.value.status) == (ASSET_NOT_CACHED, 404)
        with pytest.raises(ProtocolError) as error:
            UniversalFlagsCatalog(read)
        assert (error.value.code, error.value.status) == (CATALOG_INVALID, 503)


def test_every_built_catalog_is_ready():
    result = catalog()
    assert result.is_ready() is result.status()["ready"] is True


@pytest.mark.parametrize("damage", ["broken", "columns", "changed"])
def test_damaged_command_tables_fail_closed(damage):
    payloads = resources()
    if damage == "broken":
        payloads[PORTAL_COMMANDS] = b"ALTB" + bytes([1])
    elif damage == "columns":
        payloads[PORTAL_COMMANDS] = item_table([{"_ID": "x", COMMAND_COLUMN: ""}], (("_ID", 32), (COMMAND_COLUMN, 32)))
    read = reader(payloads)

    def changed(name):
        if name == PORTAL_COMMANDS:
            raise ProtocolError(ASSET_CHANGED, "Cached resource differs from the active public index", 409)
        return read(name)

    with pytest.raises(ProtocolError) as error:
        UniversalFlagsCatalog(changed if damage == "changed" else read)
    assert error.value.code == CATALOG_INVALID


@pytest.mark.parametrize("changes", [
    {HOME_TRIGGER: [home('SetUniversalFlags("Flags=PurchasedSkin_0099","Value=1");')]},
    {MISSION_PORTAL: [portal(1, "Event", "PaymentBonusObtained:id=9")]},
    {RECOMMEND_MENU: [recommend("限定商品", "PurchasedSkin_0001")]},
    {SKIN_SHOP: [{"ButtonImageId": 1, "PurchaseFlag": flag_hash("test_flag_002")}]},
    {MISSION_PORTAL: [portal(1, "Event", COLLIDING[0])],
     RECOMMEND_MENU: [recommend("限定商品", COLLIDING[1])]},
    {HOME_TRIGGER: [home(f'SetUniversalFlags("Flags={COLLIDING[0]}","Value=1");'
                         f'SetUniversalFlags("Flags={COLLIDING[1]}","Value=1");')]},
    {MISSION_PORTAL: [portal(1, "Event", PAYMENT_ALIAS[1])]},
])
def test_protected_overlaps_and_hash_collisions_fail_closed(changes):
    assert flag_hash(COLLIDING[0]) == flag_hash(COLLIDING[1])
    assert flag_hash(PAYMENT_ALIAS[0]) == flag_hash(PAYMENT_ALIAS[1])
    failure(resources(plain_rows(**changes)), CATALOG_CONFLICT)


def test_one_name_from_several_writers_merges_its_sources():
    rows = plain_rows(**{MISSION_PORTAL: [portal(1, "Event", "test_flag_002")],
                         HOME_TRIGGER: [home('SetUniversalFlags("Flags=test_flag_002","Value=7");')]})
    definition = catalog(rows).get("test_flag_002")
    assert definition.values == {1, 7}
    assert definition.families == {SCRIPT, MISSION_PORTAL_FAMILY, RECOMMENDED_READ}
    assert definition.sources == (FlagSource(SCRIPT, HOME_TRIGGER, "Cmnd", 0),
                                  FlagSource(MISSION_PORTAL_FAMILY, MISSION_PORTAL, "FlagName", 0),
                                  FlagSource(RECOMMENDED_READ, RECOMMEND_MENU, "UniversalFlag", 0))


def test_protection_covers_prefixes_and_native_hashes():
    rows = plain_rows(**{SKIN_SHOP: [{"ButtonImageId": 1, "PurchaseFlag": flag_hash(COLLIDING[0])}]})
    result = catalog(rows)
    for name in ("PurchasedSkin_0001", "PurchasedSkin_9999", "PaymentBonusObtained:id=1",
                 "PaymentBonusObtained:id=42", COLLIDING[0], COLLIDING[1], PAYMENT_ALIAS[1]):
        assert result.protected(name)
    for name in ("OpenUnitLibrarySystem", "test_flag_002", "purchasedskin_0001", "", "Payment"):
        assert not result.protected(name)
    assert result.protected(5) and result.get(5) is None and result.get("missing") is None


def test_content_version_tracks_every_source_and_command_table():
    base = catalog()
    assert catalog().content_version == base.content_version
    for table in SOURCE_FIELDS:
        rows = plain_rows()
        rows[table] = rows[table] + rows[table][-1:]
        assert catalog(rows).content_version != base.content_version, table
    assert catalog(commands=command_tables(("CMissionPortalMenu::OnCommand", "Wait();"))).content_version != base.content_version
    assert catalog(commands={}).content_version != base.content_version


def test_status_bounds_its_diagnostic_lists():
    rows = plain_rows(**{HOME_TRIGGER: [home('SetUniversalFlags($Name,"Value=1");', identifier=index)
                                        for index in range(STATUS_LIMIT + 8)],
                         RECOMMEND_MENU: [recommend("限定商品", "", ConfigID=index) for index in range(STATUS_LIMIT + 3)],
                         MISSION_HISTORY: [history(group, group, 1) for group in range(STATUS_LIMIT + 5)]})
    status = catalog(rows).status()
    groups = sorted(f"AppeardHistoryGroupFlag_{group}" for group in range(STATUS_LIMIT + 5))
    assert status["families"][HISTORY_GROUP] == {"count": STATUS_LIMIT + 5, "names": groups[:STATUS_LIMIT]}
    assert status["dynamic_writers"]["count"] == STATUS_LIMIT + 8
    assert [entry["row"] for entry in status["dynamic_writers"]["positions"]] == list(range(STATUS_LIMIT))
    assert status["excluded_writes"]["count"] == STATUS_LIMIT + 3
    assert len(status["excluded_writes"]["entries"]) == STATUS_LIMIT


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    _, entries = ResourceLibrary(project / "runtime/resources", client_hash=CLIENT).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    read = lambda name: assets.read(name)[0]

    def rows(name):
        raw = read(name)
        return parse_table(decompress_all4(raw) if raw.startswith(b"ALL4") else raw)["rows"]

    return SimpleNamespace(catalog=UniversalFlagsCatalog(read), rows=rows)


def test_current_resources_derive_the_native_name_domain(current):
    result = current.catalog
    status = result.status()
    assert status["name_count"] == 43
    assert {family: info["count"] for family, info in status["families"].items()} == {
        SCRIPT: 2, MISSION_PORTAL_FAMILY: 13, HISTORY_GROUP: 12, RECOMMENDED_READ: 16}
    assert names(result, SCRIPT) == {"OpenUnitLibrarySystem", "OpenGoddessRevival"}
    assert result.get("OpenUnitLibrarySystem").sources == (FlagSource(SCRIPT, HOME_TRIGGER, "Cmnd", 26),)
    assert result.get("OpenGoddessRevival").sources == (FlagSource(SCRIPT, HOME_TRIGGER, "Cmnd", 33),)
    assert names(result, RECOMMENDED_READ) == {f"test_flag_{number:03d}" for number in range(16)}
    assert names(result, HISTORY_GROUP) == {f"AppeardHistoryGroupFlag_{group}" for group in range(1, 13)}
    assert names(result, MISSION_PORTAL_FAMILY) == {
        *(f"mission_portal_sp{node}_20230921" for node in (50, 51, 52, 53, 54, 57, 58, 59, 60, 61, 62)),
        "mission_portal_sp55_20260326", "mission_portal_sp56_20231228"}
    assert all(definition.values == frozenset((1,)) for definition in result.definitions.values())
    assert all(name.isascii() and len(name) <= 28 for name in result.definitions)
    assert status["dynamic_writers"]["count"] == 0 and status["excluded_writes"]["count"] == 0
    assert status["command_tables"] == len(REGISTERED_TABLES) == 106 and status["absent_command_tables"] == []


def test_current_protected_flags_match_the_skin_shop_and_payment_bonus(current):
    result = current.catalog
    skins = current.rows(SKIN_SHOP)
    payments = current.rows(PAYMENT_BONUS)
    assert len(skins) == result.status()["protected"]["skin_purchase_count"] == 38
    for row in skins:
        name = f"PurchasedSkin_{row['ButtonImageId']:04d}"
        assert flag_hash(name) == row["PurchaseFlag"] and result.protected(name)
    payment_names = [f"PaymentBonusObtained:id={row['ConfigId']}" for row in payments]
    assert result.status()["protected"]["payment_bonus_names"] == sorted(payment_names) and len(payment_names) == 3
    assert all(map(result.protected, payment_names))
    hashes = [*map(flag_hash, result.definitions), *(row["PurchaseFlag"] for row in skins), *map(flag_hash, payment_names)]
    assert len(hashes) == len(set(hashes)) == 84
    assert not any(map(result.protected, result.definitions))

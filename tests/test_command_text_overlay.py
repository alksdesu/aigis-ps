import hashlib
import json
from pathlib import Path

import pytest

from aigis_ps.al_format import parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.command_text_overlay import (
    AMBIGUOUS, CLOSING_POLICY, MAX_OVERRIDES, MAX_RULE_BYTES, CommandTextOverrides, RowKey, build_command_text_overlay,
    load_command_text_overrides, parse_row_key,
)
from aigis_ps.entrust_catalog import EntrustCatalog, ScopeState
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.native_commands import CommandForm
from aigis_ps.possessions import PossessionService
from aigis_ps.quest_catalog import QuestCatalog
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.system_literals import command_id, decode_command, encode_command
from aigis_ps.table_cell_patch import MAX_STRING_BYTES
from test_entrust_catalog import (
    DAILY_QUEST, DAILY_TYPE, EMPIRE_MISSION, EMPIRE_TYPE, GENERAL_ROUTE, HISTORY_MISSION, HISTORY_QUEST, HISTORY_TYPE,
    MODE_MENU, NOW, PLATFORM, QUEST_COMMON, SYSTEM_COMMANDS, TOWER_ROUTE, TYPE_GATES, fail, mission, modes,
)
from test_native_commands import commands
from test_quest_catalog import setup, table


PROJECT = Path(__file__).resolve().parents[1]
EXAMPLE = PROJECT / "config/command-text-overrides.example.json"
COMMANDS = "OnCommand.atb"
DAILY_GATE, REPRODUCE_GATE = "HasEntrustByType_2", "HasEntrustByType_8"
HISTORY_MENU, HISTORY_ROUTE_NAME = "FCMenuQuestTypeList::IsEntrust_14", "CmnMenu::DoReturn_Type_14"
GENERAL_HISTORY_MENU = "CMenuQuestGeneral::IsEntrust_14"
UNRELATED, UNRELATED_TEXT = "Unrelated", 'SetUpdaterFunc( "Func_Unrelated" );'
# Two hex digits encode each byte of command text.
LONGEST_TEXT = MAX_STRING_BYTES // 2
REAL_DAILY_GATE_ROW = 30


def key(name):
    return str(command_id(name))


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def rows(payload):
    return parse_table(payload)["rows"]


def unread(name):
    raise AssertionError(f"{name} was read without command text overrides")


def overlay_of(resources, tables):
    return build_command_text_overlay(resources.__getitem__, CommandTextOverrides(tables))


def overlaid(setup, tables, **options):
    baseline = modes(setup, **options)
    resources = setup[0]
    overlay = overlay_of(resources, tables)
    effective = resources | dict(overlay.replacements)
    shared = baseline.quests
    quests = QuestCatalog(shared.snapshot, effective.__getitem__, possessions=shared._possessions,
                          resource_version=shared.resource_version)
    return baseline, overlay, EntrustCatalog(quests)


def gates(overlay):
    return [change.to_dict() for change in overlay.gates]


def test_row_keys_are_canonical_identifiers_or_row_numbers():
    assert parse_row_key("0") == RowKey(False, 0) and parse_row_key("-2147483648") == RowKey(False, -2 ** 31)
    assert parse_row_key(key(DAILY_GATE)) == RowKey(False, command_id(DAILY_GATE))
    assert parse_row_key("#0") == RowKey(True, 0) and parse_row_key("#65534") == RowKey(True, 65534)
    for value in ("-0", "01", "+1", " 1", "1 ", "1.0", "2147483648", "-2147483649", "#", "#01", "#-1", "# 1",
                  "#100000", "", 1, None):
        with pytest.raises(ValueError, match="canonical _ID or by #row"):
            parse_row_key(value)


def test_overrides_normalize_into_registration_and_row_order():
    current = CommandTextOverrides({COMMANDS: {"#2": UNRELATED_TEXT, "17": "0;"}, SYSTEM_COMMANDS: {"-5": "1;\n\t0;"},
                                    "OnCrystalInventory.atb": {}})
    assert list(current.tables) == [SYSTEM_COMMANDS, COMMANDS] and list(current.tables[COMMANDS]) == ["17", "#2"]
    assert current.count == 3
    assert current.to_dict() == {"tables": {SYSTEM_COMMANDS: {"-5": "1;\n\t0;"}, COMMANDS: {"17": "0;", "#2": UNRELATED_TEXT}}}
    assert CommandTextOverrides(**current.to_dict()) == current and len(current.content_version) == 64
    assert CommandTextOverrides({SYSTEM_COMMANDS: {"-5": "0;"}}).content_version != current.content_version
    assert CommandTextOverrides().to_dict() == {"tables": {}} and CommandTextOverrides().count == 0
    assert CommandTextOverrides({COMMANDS: {"1": "委任(" + "x" * (LONGEST_TEXT - 9) + ");"}}).count == 1
    with pytest.raises(TypeError):
        current.tables[COMMANDS]["17"] = "1;"


@pytest.mark.parametrize("tables, reason", [
    ([], "mapping of command tables"),
    ({"Unknown.atb": {"1": "0;"}}, "unregistered command table: Unknown.atb"),
    ({"OnSystemCommand": {"1": "0;"}}, "unregistered command table"),
    ({SYSTEM_COMMANDS: ["0;"]}, "row mapping"),
    ({SYSTEM_COMMANDS: {"01": "0;"}}, "canonical _ID or by #row"),
    ({SYSTEM_COMMANDS: {"1": 0}}, "control characters"),
    ({SYSTEM_COMMANDS: {"1": "0;\0"}}, "control characters"),
    ({SYSTEM_COMMANDS: {"1": "0;\x1b"}}, "control characters"),
    ({SYSTEM_COMMANDS: {"1": "0;\x85"}}, "control characters"),
    ({SYSTEM_COMMANDS: {"1": "\ud800"}}, "encode as UTF-8"),
    ({SYSTEM_COMMANDS: {"1": "x" * (LONGEST_TEXT + 1)}}, "native string cell"),
    ({SYSTEM_COMMANDS: {"1": "委" * (LONGEST_TEXT // 3 + 1)}}, "native string cell"),
    ({SYSTEM_COMMANDS: {str(index): "0;" for index in range(MAX_OVERRIDES + 1)}}, f"at most {MAX_OVERRIDES} rows"),
])
def test_invalid_overrides_are_refused(tables, reason):
    with pytest.raises(ValueError, match=reason):
        CommandTextOverrides(tables)


def test_rule_files_are_strict_bounded_and_optional(tmp_path):
    assert load_command_text_overrides(None) is None
    path, document = tmp_path / "command-text-overrides.json", {"tables": {SYSTEM_COMMANDS: {key(DAILY_GATE): "0;"}}}
    path.write_bytes("\ufeff".encode() + json.dumps(document).encode())
    assert load_command_text_overrides(path).to_dict() == document
    padded = json.dumps(document).encode().ljust(MAX_RULE_BYTES)
    path.write_bytes(padded)
    assert load_command_text_overrides(path).to_dict() == document
    path.write_bytes(padded + b" ")
    with pytest.raises(ValueError):
        load_command_text_overrides(path)
    for raw in (b"[]", b"{}", b'{"tables": []}', b'{"tables": {}, "extra": {}}', b'{"tables": {"OnSystemCommand.atb": []}}',
                b'{"tables": {}, "tables": {}}', b'{"tables": {"OnSystemCommand.atb": {"1": "\\ud800"}}}',
                b'{"tables": {"OnSystemCommand.atb": {"1": NaN}}}', b'{"tables": {'):
        path.write_bytes(raw)
        with pytest.raises(ValueError):
            load_command_text_overrides(path)


def test_the_example_closes_only_the_daily_type_gate():
    assert load_command_text_overrides(EXAMPLE).to_dict() == {"tables": {SYSTEM_COMMANDS: {key(DAILY_GATE): "0;"}}}


def test_overlay_patches_only_the_addressed_command_cells():
    source = commands(("First", "1;"), (UNRELATED, UNRELATED_TEXT), ("Third", "0;"))
    current = CommandTextOverrides({COMMANDS: {key("First"): "0;", "#2": "0;"}})
    overlay = build_command_text_overlay({COMMANDS: source}.__getitem__, current)
    assert set(overlay.replacements) == {COMMANDS} and overlay.targets == (COMMANDS,)
    before, after = rows(source), rows(overlay.replacements[COMMANDS])
    assert [row["_ID"] for row in after] == [row["_ID"] for row in before] and after[1:] == before[1:]
    assert after[0]["_Cmnd"] == encode_command("0;") and decode_command(after[0]["_Cmnd"]) == "0;"
    assert [record.to_dict() for record in overlay.rows] == [
        {"table": COMMANDS, "key": key("First"), "row": 0, "id": command_id("First"), "before": CommandForm.LITERAL_TRUE,
         "after": CommandForm.LITERAL_FALSE, "changed": True},
        {"table": COMMANDS, "key": "#2", "row": 2, "id": command_id("Third"), "before": CommandForm.LITERAL_FALSE,
         "after": CommandForm.LITERAL_FALSE, "changed": False}]
    metadata = overlay.metadata
    assert metadata == {"schema_version": 1, "rules": current.to_dict(), "rules_hash": current.content_version,
                        "policy": CLOSING_POLICY,
                        "tables": {COMMANDS: {"source_sha256": sha(source),
                                              "effective_sha256": sha(overlay.replacements[COMMANDS])}},
                        "rows": [record.to_dict() for record in overlay.rows], "entrust_gates": []}
    json.dumps(metadata)
    again = build_command_text_overlay({COMMANDS: overlay.replacements[COMMANDS]}.__getitem__, current)
    assert again.replacements == {} and not any(record.changed for record in again.rows)


def test_unchanged_texts_and_empty_overrides_patch_nothing():
    source = commands((DAILY_GATE, "1;"))
    overlay = overlay_of({SYSTEM_COMMANDS: source}, {SYSTEM_COMMANDS: {key(DAILY_GATE): "1;"}})
    assert overlay.replacements == {} and overlay.gates == () and not overlay.rows[0].changed
    assert overlay.metadata["tables"] == {SYSTEM_COMMANDS: {"source_sha256": sha(source), "effective_sha256": sha(source)}}
    for current in (None, CommandTextOverrides()):
        empty = build_command_text_overlay(unread, current)
        assert (empty.replacements, empty.targets, empty.rows, empty.gates) == ({}, (), (), ())
    assert build_command_text_overlay(unread, None).metadata["rules"] is None
    with pytest.raises(ValueError, match="verified overrides"):
        build_command_text_overlay(unread, {"tables": {}})
    with pytest.raises(ValueError, match="resource reader"):
        build_command_text_overlay(None, CommandTextOverrides({SYSTEM_COMMANDS: {"1": "0;"}}))


@pytest.mark.parametrize("tables, reason", [
    ({COMMANDS: {key("Absent"): "0;"}}, "matches 0 rows of OnCommand.atb$"),
    ({COMMANDS: {"#3": "0;"}}, "#3 matches 0 rows of OnCommand.atb$"),
    ({COMMANDS: {key("First"): "0;"}}, "matches 2 rows of OnCommand.atb; address one by #row"),
    ({COMMANDS: {key("Third"): "0;", "#2": "0;"}}, "and #2 address one row of OnCommand.atb"),
    ({"OnCommandMenuShop.atb": {"#0": "0;"}}, r"unavailable command table: OnCommandMenuShop.atb \(command_table_unreadable\)"),
    ({"OnCommandMenuOthers.atb": {"#0": "0;"}}, r"unavailable command table: OnCommandMenuOthers.atb \(command_columns_invalid\)"),
], ids=["absent id", "absent row", "duplicated id", "same row twice", "missing table", "foreign table"])
def test_rows_must_exist_once_in_an_available_command_table(tables, reason):
    resources = {COMMANDS: commands(("First", "1;"), ("First", "0;"), ("Third", "0;")),
                 "OnCommandMenuOthers.atb": table((("_ID", 1), ("Text", 32)), [{"_ID": 1, "Text": "x"}])}
    with pytest.raises(ValueError, match=reason):
        overlay_of(resources, tables)


def test_row_numbers_address_one_of_duplicated_identifiers():
    source = commands(("First", "1;"), ("First", "0;"))
    chosen = overlay_of({COMMANDS: source}, {COMMANDS: {"#0": "0;"}})
    assert [decode_command(row["_Cmnd"]) for row in rows(chosen.replacements[COMMANDS])] == ["0;", "0;"]
    assert [(record.row, record.identifier) for record in chosen.rows] == [(0, command_id("First"))]


def test_closing_the_daily_type_gate_refuses_daily_entrust(setup):
    baseline, overlay, subject = overlaid(setup, {SYSTEM_COMMANDS: {key(DAILY_GATE): "0;"}})
    assert baseline.scopes[DAILY_TYPE].state is ScopeState.OFFERED
    scope = subject.scopes[DAILY_TYPE]
    assert (scope.state, scope.exclusion.code, scope.gate_form) == (ScopeState.REFUSED, "entrust_not_offered",
                                                                      CommandForm.LITERAL_FALSE)
    fail("entrust_not_offered", lambda: subject.product(DAILY_QUEST), 409)
    assert subject.status()["offered_types"] == [0, HISTORY_TYPE, EMPIRE_TYPE]
    assert gates(overlay) == [{"gate": DAILY_GATE, "before": CommandForm.LITERAL_TRUE, "after": CommandForm.LITERAL_FALSE}]
    assert subject.commands.content_version != baseline.commands.content_version
    assert subject.content_version != baseline.content_version


def test_closing_the_history_menu_gate_blocks_its_missions_only(setup):
    baseline, overlay, subject = overlaid(setup, {QUEST_COMMON: {key(HISTORY_MENU): "0;"}})
    assert (baseline.menu_gates[14].form, subject.menu_gates[14].form) == (CommandForm.OPEN_MISSION_TAG,
                                                                           CommandForm.LITERAL_FALSE)
    assert baseline.menu_open(mission(baseline, HISTORY_TYPE, HISTORY_MISSION), now=NOW, platform_code=PLATFORM)
    assert not subject.menu_open(mission(subject, HISTORY_TYPE, HISTORY_MISSION), now=NOW, platform_code=PLATFORM)
    assert subject.menu_open(mission(subject, EMPIRE_TYPE, EMPIRE_MISSION), now=NOW, platform_code=PLATFORM)
    history = subject.availability(now=NOW, platform_code=PLATFORM)["types"][HISTORY_TYPE]
    assert (history["open"], history["blocked"]) == (0, {"entrust_menu_closed": {"count": 1, "quests": [HISTORY_QUEST]}})
    assert baseline.availability(now=NOW, platform_code=PLATFORM)["types"][HISTORY_TYPE]["open"] == 1
    assert gates(overlay) == [{"gate": "IsEntrust_14", "before": CommandForm.OPEN_MISSION_TAG,
                               "after": CommandForm.LITERAL_FALSE}]


@pytest.mark.parametrize("tables, opened", [
    ({SYSTEM_COMMANDS: {key(REPRODUCE_GATE): "1;"}}, [f"{SYSTEM_COMMANDS} {key(REPRODUCE_GATE)}", REPRODUCE_GATE]),
    ({QUEST_COMMON: {key(HISTORY_MENU): "1;"}}, [f"{QUEST_COMMON} {key(HISTORY_MENU)}", "IsEntrust_14"]),
    ({QUEST_COMMON: {key(HISTORY_ROUTE_NAME): GENERAL_ROUTE}}, ["IsEntrust_14"]),
    ({SYSTEM_COMMANDS: {key(UNRELATED): "1;"}}, [f"{SYSTEM_COMMANDS} {key(UNRELATED)}"]),
], ids=["type gate", "menu gate", "menu route", "other command"])
def test_overrides_never_open_a_gate_further(tables, opened):
    resources = {SYSTEM_COMMANDS: commands(*TYPE_GATES, (REPRODUCE_GATE, "0;"), (UNRELATED, UNRELATED_TEXT)),
                 QUEST_COMMON: commands(*MODE_MENU, (GENERAL_HISTORY_MENU, "1;"))}
    with pytest.raises(ValueError, match="may only close native gates") as caught:
        overlay_of(resources, tables)
    assert str(caught.value).endswith(": " + ", ".join(opened))


def test_routes_may_move_a_menu_gate_onto_a_closed_scope():
    resources = {QUEST_COMMON: commands(*MODE_MENU, (GENERAL_HISTORY_MENU, "0;"), (GENERAL_HISTORY_MENU, "1;"))}
    tower = overlay_of(resources, {QUEST_COMMON: {key(HISTORY_ROUTE_NAME): TOWER_ROUTE}})
    assert gates(tower) == [{"gate": "IsEntrust_14", "before": CommandForm.OPEN_MISSION_TAG, "after": CommandForm.ABSENT}]
    general = overlay_of(resources, {QUEST_COMMON: {key(HISTORY_ROUTE_NAME): GENERAL_ROUTE}})
    assert gates(general) == [{"gate": "IsEntrust_14", "before": CommandForm.OPEN_MISSION_TAG, "after": AMBIGUOUS}]


@pytest.fixture(scope="module")
def native():
    resource_root, executable = PROJECT / "runtime/resources", PROJECT.parent / "aigisr_exe"
    try:
        master = MasterDataStore(PROJECT / "runtime/server/masterdata").snapshot()
        manifest, entries = ResourceLibrary(resource_root, client_hash=master.client_hash).load()
    except (OSError, ValueError):
        pytest.skip("Verified local client resources and public masters are required")
    assets = IndexedAssetService(entries, resource_root, executable)
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(executable, asset_reader=reader)

    def quests(read=reader):
        return QuestCatalog(master, read, possessions=possessions, resource_version=manifest["version"])
    return reader, quests


def test_native_example_refuses_daily_entrust_and_keeps_every_other_offer(native):
    reader, quests = native
    overlay = build_command_text_overlay(reader, load_command_text_overrides(EXAMPLE))
    assert set(overlay.replacements) == {SYSTEM_COMMANDS}
    assert [(record.row, record.before, record.after) for record in overlay.rows] == [
        (REAL_DAILY_GATE_ROW, CommandForm.LITERAL_TRUE, CommandForm.LITERAL_FALSE)]
    assert gates(overlay) == [{"gate": DAILY_GATE, "before": CommandForm.LITERAL_TRUE, "after": CommandForm.LITERAL_FALSE}]
    plain = EntrustCatalog(quests())
    subject = EntrustCatalog(quests(lambda name: overlay.replacements[name] if name in overlay.replacements
                                    else reader(name)))
    status = subject.status()
    assert status["offered_types"] == [quest_type for quest_type in plain.status()["offered_types"] if quest_type != DAILY_TYPE]
    assert status["refused_types"][DAILY_TYPE]["code"] == "entrust_not_offered"
    assert status["types"][DAILY_TYPE]["native_gate"]["row"] == REAL_DAILY_GATE_ROW
    assert subject.content_version != plain.content_version


def test_native_menu_gates_refuse_to_open_further(native):
    reader, _ = native
    with pytest.raises(ValueError, match="IsEntrust_14$"):
        build_command_text_overlay(reader, CommandTextOverrides({QUEST_COMMON: {key(HISTORY_MENU): "1;"}}))
    closed = build_command_text_overlay(reader, CommandTextOverrides({QUEST_COMMON: {key(HISTORY_MENU): "0;"}}))
    assert gates(closed) == [{"gate": "IsEntrust_14", "before": CommandForm.OPEN_MISSION_TAG,
                              "after": CommandForm.LITERAL_FALSE}]

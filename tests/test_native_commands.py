import ast
from dataclasses import FrozenInstanceError
import hashlib
from importlib.resources import files
import json
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest

import aigis_ps
from aigis_ps import (
    barracks_service, blessing_catalog, current_squad, emblem_catalog, fairy_farm, fame_catalog, guide_service,
    image_aliases, item_summon_catalog, package_catalog, select_ticket_catalog, shop_growth, silver_house,
    silver_trade, training_catalog, unit_capacity, unit_detail_options, unit_sale_catalog,
)
from aigis_ps.al_format import parse_table
from aigis_ps.alxml import loads
from aigis_ps.asset_service import ASSET_CHANGED, ASSET_NOT_CACHED, ASSET_NOT_FOUND, IndexedAssetService
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.native_commands import (
    ABSENT_TABLE, MENU_SCOPE_CHAINS, REGISTERED_TABLES, STATUS_LIMIT, CommandAmbiguous, CommandForm, CommandHit,
    MenuRoute, NativeCommandIndex, command_form, form_of,
)
from aigis_ps.possessions import PossessionService
from aigis_ps.public_tables import MASTER_LIMITS
from aigis_ps.quest_catalog import QuestCatalog
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.system_literals import RATING_COMMAND, command_id, encode_command
from test_quest_catalog import TEXT_KIND, table


INT_KIND = 1
COMMAND_FIELDS = (("_ID", INT_KIND), ("_Cmnd", TEXT_KIND))
SYSTEM, COMMAND, QUEST_COMMON = "OnSystemCommand.atb", "OnCommand.atb", "OnCommandMenuQuestCommon.atb"
GENERAL_MENU, HISTORY_MENU = "Func_GeneralQuestMenu", "Func_HistoryQuestMenu"
GENERAL, HISTORY = MENU_SCOPE_CHAINS[GENERAL_MENU], MENU_SCOPE_CHAINS[HISTORY_MENU]
OPEN_TAG = 'IsOpenMission( "Tag" , $Tag );'
UNREADABLE = "command_table_unreadable"
COLUMNS_INVALID = "command_columns_invalid"
NOT_A_TABLE = b"NOPE" + bytes(12)
# The reading modules spell these tables inline.
PRESENT_LIST, EVENT_MENU = "OnCommandMenuPresentList.atb", "OnCommandMenuEvent.atb"
GROWTH_SELECT, SILVER_HOUSE = "OnCommandMenuUnitGrowthSelect.atb", "OnCommandMenuUnitSilverHouse.atb"
BLESS_LIST = "OnCommandMenuBattleGodBlessList.atb"
LIBRARY_MENUS = ("OnCommandMenuUnitLibraryDeposit.atb", "OnCommandMenuUnitLibraryPortal.atb")
SOURCE = Path(aigis_ps.__file__).resolve().parent


def commands(*entries):
    return table(COMMAND_FIELDS, [{"_ID": command_id(name), "_Cmnd": encode_command(text)} for name, text in entries])


def reader(resources):
    def read(name):
        if name not in resources:
            raise ProtocolError("quest_asset_unavailable", f"Verified quest resource is unavailable: {name}", 503)
        return resources[name]
    return read


def index(resources, **options):
    return NativeCommandIndex(reader(resources), **options)


class SingleTableRead(NamedTuple):
    module: ModuleType
    table: str
    names: tuple[str, ...] = ()
    per_card: tuple[str, ...] = ()
    identifiers: tuple[int, ...] = ()
    # Content scans, byte pins and fingerprints depend on every row of their table, not on one looked-up key.
    whole_table: bool = False

    def command_keys(self, card_ids, table_keys):
        keys = {name: command_id(name) for name in self.names}
        keys.update((name, command_id(name)) for pattern in self.per_card for name in map(pattern.format, card_ids))
        keys.update((key, key) for key in (*self.identifiers, *(table_keys if self.whole_table else ())))
        return keys


SINGLE_TABLE_READS = (
    SingleTableRead(barracks_service, COMMAND, per_card=(RATING_COMMAND,), identifiers=(barracks_service.PRINCE_RULE_COMMAND,)),
    *(SingleTableRead(barracks_service, name, whole_table=True) for name in barracks_service._COMMAND_HASHES),
    SingleTableRead(blessing_catalog, BLESS_LIST, whole_table=True),
    *(SingleTableRead(current_squad, name, whole_table=True)
      for name in (*current_squad.ORDINARY_TEAM_COMMANDS, *current_squad.RAID_TEAM_COMMANDS)),
    SingleTableRead(emblem_catalog, PRESENT_LIST, names=(emblem_catalog.RECEIVE_COMMAND,)),
    SingleTableRead(fairy_farm, SYSTEM, whole_table=True),
    *(SingleTableRead(fame_catalog, name, whole_table=True) for name in LIBRARY_MENUS),
    SingleTableRead(guide_service, EVENT_MENU, names=(guide_service.ELIGIBILITY_COMMAND,)),
    SingleTableRead(guide_service, COMMAND, per_card=(RATING_COMMAND,)),
    SingleTableRead(image_aliases, image_aliases.RESOURCE_NAME,
                    per_card=tuple(f"{prefix}_{{}}" for prefix in image_aliases.PREFIXES)),
    SingleTableRead(item_summon_catalog, PRESENT_LIST, names=(item_summon_catalog.RECEIVE_COMMAND,)),
    SingleTableRead(package_catalog, package_catalog.COMMAND_RESOURCE, whole_table=True),
    SingleTableRead(select_ticket_catalog, select_ticket_catalog.RATING_RESOURCE, per_card=(RATING_COMMAND,)),
    SingleTableRead(shop_growth, GROWTH_SELECT, names=(shop_growth.MENU_NAME,)),
    SingleTableRead(shop_growth, COMMAND, per_card=(RATING_COMMAND,)),
    SingleTableRead(silver_house, SILVER_HOUSE, whole_table=True),
    SingleTableRead(silver_trade, COMMAND, per_card=(RATING_COMMAND,)),
    SingleTableRead(training_catalog, COMMAND, per_card=(RATING_COMMAND,)),
    SingleTableRead(training_catalog, SYSTEM, whole_table=True),
    SingleTableRead(unit_capacity, COMMAND, identifiers=(unit_capacity.CLEAR_LIMIT_COMMAND,)),
    SingleTableRead(unit_capacity, SYSTEM, whole_table=True),
    SingleTableRead(unit_detail_options, SYSTEM, identifiers=(unit_detail_options.SMARTPHONE_IMAGE_COMMAND,)),
    SingleTableRead(unit_sale_catalog, unit_sale_catalog.AUTO_RESOURCE, names=(unit_sale_catalog.AUTO_COMMAND,)),
    SingleTableRead(unit_sale_catalog, unit_sale_catalog.RATING_RESOURCE, per_card=(RATING_COMMAND,)),
)


def command_positions(read):
    table_keys, positions = {}, {}
    for name in REGISTERED_TABLES:
        table_keys[name] = tuple(row["_ID"] for row in parse_table(read(name))["rows"])
        for row, key in enumerate(table_keys[name]):
            positions.setdefault(key, []).append((name, row))
    return table_keys, positions


def named_command_tables():
    registered, named = frozenset(REGISTERED_TABLES), {}
    for path in SOURCE.glob("*.py"):
        tables = {node.value for node in ast.walk(ast.parse(path.read_bytes(), str(path)))
                  if isinstance(node, ast.Constant) and node.value in registered}
        if tables:
            named[f"{aigis_ps.__name__}.{path.stem}"] = tables
    return named


def single_table_divergences(reads, table_keys, positions, card_ids):
    # 91DEE0 returns the first hit over all tables, so a read key may have one row at most, in its read table.
    divergent = {}
    for entry in reads:
        for label, key in entry.command_keys(card_ids, table_keys[entry.table]).items():
            places = positions.get(key, [])
            if len(places) > 1 or any(name != entry.table for name, _ in places):
                divergent.setdefault((entry.module.__name__, entry.table), {})[label] = places
    return divergent


@pytest.mark.parametrize(("text", "form"), [
    ("1;", CommandForm.LITERAL_TRUE), (" 1 ;\n", CommandForm.LITERAL_TRUE), ("\t0;\r\n", CommandForm.LITERAL_FALSE),
    (OPEN_TAG, CommandForm.OPEN_MISSION_TAG), ('IsOpenMission("Tag",$Tag);', CommandForm.OPEN_MISSION_TAG),
    ('\nIsOpenMission (\t"Tag",\n$Tag ) ;  ', CommandForm.OPEN_MISSION_TAG),
    ("", CommandForm.UNSUPPORTED), (" \n", CommandForm.UNSUPPORTED), ("1", CommandForm.UNSUPPORTED),
    ("1;1;", CommandForm.UNSUPPORTED), ("2;", CommandForm.UNSUPPORTED), ("01;", CommandForm.UNSUPPORTED),
    ('IsOpenMission("ID",$ID);', CommandForm.UNSUPPORTED), ('IsOpenMission("Tag",302);', CommandForm.UNSUPPORTED),
    ('IsOpenMission("Ta g",$Tag);', CommandForm.UNSUPPORTED), ('Is OpenMission("Tag",$Tag);', CommandForm.UNSUPPORTED),
    ('IsOpenMission("Tag",$ Tag);', CommandForm.UNSUPPORTED), ("1;\u3000", CommandForm.UNSUPPORTED),
    ('isopenmission("Tag",$Tag);', CommandForm.UNSUPPORTED),
])
def test_forms_ignore_ascii_whitespace_only_between_tokens(text, form):
    assert command_form(text) is form
    hit = index({SYSTEM: commands(("Probe", text))}).lookup("Probe")
    assert hit.text == text and hit.form is form and form_of(hit) is form
    assert json.dumps({"form": hit.form}) == json.dumps({"form": form.value})


def test_absent_and_undecodable_commands_have_distinct_false_forms():
    assert form_of(None) is CommandForm.ABSENT
    assert command_form(None) is CommandForm.UNSUPPORTED


def test_lookup_reports_exact_row_identity_and_misses_absent_keys():
    subject = index({SYSTEM: commands(("Other", "0;"), ("HasEntrustByType_2", "1;"))})
    hit = subject.lookup("HasEntrustByType_2")
    assert hit == CommandHit("HasEntrustByType_2", SYSTEM, 1, command_id("HasEntrustByType_2"), "1;")
    assert subject.lookup("HasEntrustByType_1") is None
    with pytest.raises(FrozenInstanceError):
        hit.text = "0;"
    with pytest.raises(ValueError):
        subject.lookup(None)


def test_duplicate_identifiers_within_or_across_tables_are_ambiguous_only_for_their_key():
    subject = index({SYSTEM: commands(("Twice", "1;"), ("Once", "1;"), ("Twice", "0;")),
                     QUEST_COMMON: commands(("Spread", "1;")), COMMAND: commands(("Spread", "1;"))})
    with pytest.raises(CommandAmbiguous) as within:
        subject.lookup("Twice")
    assert within.value.positions == ((SYSTEM, 0), (SYSTEM, 2))
    assert [hit.text for hit in within.value.hits] == ["1;", "0;"]
    with pytest.raises(CommandAmbiguous) as across:
        subject.lookup("Spread")
    assert isinstance(across.value, ValueError) and across.value.code == "native_command_ambiguous"
    assert across.value.diagnostic() == {"code": "native_command_ambiguous", "message": str(across.value), "name": "Spread",
                                         "id": command_id("Spread"), "occurrences": 2,
                                         "positions": [[COMMAND, 0], [QUEST_COMMON, 0]]}
    assert subject.lookup("Once").row == 1
    expected = {command_id("Twice"): ((SYSTEM, 0), (SYSTEM, 2)), command_id("Spread"): ((COMMAND, 0), (QUEST_COMMON, 0))}
    assert subject.duplicates() == expected and list(subject.duplicates()) == sorted(expected)
    assert subject.status()["duplicate_count"] == 2
    assert subject.status()["duplicates"] == [{"id": key, "occurrences": 2, "positions": [list(place) for place in places]}
                                              for key, places in sorted(expected.items())]


def test_unreadable_tables_are_skipped_like_absent_native_files():
    resources = {SYSTEM: commands(("HasEntrustByType_2", "1;"))}
    subject = index(resources)
    assert subject.tables == REGISTERED_TABLES and subject.loaded == (SYSTEM,)
    assert subject.missing == {name: "quest_asset_unavailable" for name in REGISTERED_TABLES[1:]}
    assert subject.source_sha256 == {SYSTEM: hashlib.sha256(resources[SYSTEM]).hexdigest(),
                                     **{name: None for name in REGISTERED_TABLES[1:]}}
    status = subject.status()
    assert (status["tables"], status["rows"], len(status["missing"]), status["invalid"]) == (1, 1, 105, {})
    assert subject.lookup("HasEntrustByType_2").form is CommandForm.LITERAL_TRUE


def test_uncached_tables_are_reported_as_unavailable_and_digested_as_absent():
    resources = {SYSTEM: commands(("HasEntrustByType_2", "1;"))}

    def built(code):
        def read(name):
            if name in resources:
                return resources[name]
            raise ProtocolError(code if name == QUEST_COMMON else ASSET_NOT_FOUND, f"Fixture resource {name}", 404)
        return NativeCommandIndex(read)

    absent, uncached, changed = built(ASSET_NOT_FOUND), built(ASSET_NOT_CACHED), built(ASSET_CHANGED)
    assert ABSENT_TABLE == ASSET_NOT_FOUND == "asset_not_found"
    assert absent.missing == {name: ABSENT_TABLE for name in REGISTERED_TABLES if name not in resources}
    assert uncached.missing == {**absent.missing, QUEST_COMMON: ASSET_NOT_CACHED} == uncached.status()["missing"]
    assert uncached.loaded == absent.loaded == (SYSTEM,)
    assert uncached.content_version == absent.content_version != changed.content_version


@pytest.mark.parametrize("outcome", [OSError("gone"), KeyError(SYSTEM), ValueError("broken"), "text", bytearray(b"ALTB")])
def test_reader_failures_without_codes_and_non_bytes_count_as_unreadable(outcome):
    def read(name):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    subject = NativeCommandIndex(read, tables=(SYSTEM,))
    assert subject.missing == {SYSTEM: UNREADABLE} and subject.loaded == () and subject.source_sha256 == {SYSTEM: None}


@pytest.mark.parametrize(("payload", "reason"), [
    (table((("_ID", TEXT_KIND), ("_Cmnd", TEXT_KIND)), [{"_ID": "Hidden", "_Cmnd": encode_command("1;")}]), COLUMNS_INVALID),
    (table((("_ID", INT_KIND), ("_Cmnd", INT_KIND)), [{"_ID": command_id("Hidden"), "_Cmnd": 1}]), COLUMNS_INVALID),
    (table((("_ID", INT_KIND),), [{"_ID": command_id("Hidden")}]), COLUMNS_INVALID),
    (table((("ID", INT_KIND), ("_Cmnd", TEXT_KIND)), [{"ID": command_id("Hidden"), "_Cmnd": encode_command("1;")}]),
     COLUMNS_INVALID),
    (NOT_A_TABLE, "unsupported_header"),
])
def test_tables_without_named_native_command_columns_are_skipped(payload, reason):
    subject = index({SYSTEM: payload, QUEST_COMMON: commands(("Visible", "1;"))})
    assert subject.invalid == {SYSTEM: reason} and subject.status()["invalid"] == {SYSTEM: reason}
    assert subject.loaded == (QUEST_COMMON,) and SYSTEM not in subject.missing
    assert subject.lookup("Hidden") is None and subject.lookup("Visible").table == QUEST_COMMON
    assert subject.source_sha256[SYSTEM] == hashlib.sha256(payload).hexdigest()


def test_extra_columns_are_allowed_beside_the_named_command_columns():
    fields = (("Note", TEXT_KIND), ("_Cmnd", TEXT_KIND), ("Extra", INT_KIND), ("_ID", INT_KIND))
    payload = table(fields, [{"Note": "x", "_Cmnd": encode_command("1;"), "Extra": 7, "_ID": command_id("Wide")}])
    subject = index({SYSTEM: payload})
    assert subject.invalid == {} and subject.lookup("Wide") == CommandHit("Wide", SYSTEM, 0, command_id("Wide"), "1;")


@pytest.mark.parametrize("encoded", ["zz", "abc", "01"])
def test_undecodable_commands_fail_closed_without_breaking_the_index(encoded):
    payload = table(COMMAND_FIELDS, [{"_ID": command_id("Broken"), "_Cmnd": encoded},
                                     {"_ID": command_id("Fine"), "_Cmnd": encode_command("1;")}])
    subject = index({SYSTEM: payload})
    broken = subject.lookup("Broken")
    assert broken.text is None and broken.form is CommandForm.UNSUPPORTED
    assert subject.lookup("Fine").form is CommandForm.LITERAL_TRUE


def test_null_command_strings_fail_closed():
    hit = index({SYSTEM: table(COMMAND_FIELDS, [{"_ID": command_id("Null"), "_Cmnd": None}])}).lookup("Null")
    assert (hit.row, hit.text, hit.form) == (0, None, CommandForm.UNSUPPORTED)


def test_resolve_takes_the_first_scope_hit_so_a_false_override_shadows_the_fallback():
    subject = index({QUEST_COMMON: commands(("FCMenuQuestTypeList::IsEntrust_14", "1;"),
                                            ("CHistoryQuestMenu::IsEntrust_14", "0;"))})
    shadow = subject.resolve(HISTORY, "IsEntrust_14")
    assert (shadow.name, shadow.row, shadow.form) == ("CHistoryQuestMenu::IsEntrust_14", 1, CommandForm.LITERAL_FALSE)
    fallback = subject.resolve(GENERAL, "IsEntrust_14")
    assert (fallback.name, fallback.row, fallback.form) == ("FCMenuQuestTypeList::IsEntrust_14", 0, CommandForm.LITERAL_TRUE)
    assert subject.resolve(GENERAL, "IsEntrust_2") is None and subject.resolve((), "IsEntrust_14") is None


def test_resolve_stops_at_the_first_hit_and_reports_ambiguity_reached_before_it():
    subject = index({SYSTEM: commands(("CmnMenu::IsEntrust_0", "1;")), QUEST_COMMON: commands(
        ("CmnMenu::IsEntrust_0", "0;"), ("FCMenuQuestTypeList::IsEntrust_0", "1;"),
        ("CHistoryQuestMenu::IsEntrust_1", "1;"), ("CHistoryQuestMenu::IsEntrust_1", "1;"))})
    assert subject.resolve(GENERAL, "IsEntrust_0").name == "FCMenuQuestTypeList::IsEntrust_0"
    for scopes, name in ((HISTORY, "IsEntrust_1"), (("CmnMenu",), "IsEntrust_0")):
        with pytest.raises(CommandAmbiguous):
            subject.resolve(scopes, name)


@pytest.mark.parametrize(("scopes", "name"), [
    ("CmnMenu", "IsEntrust_0"), (None, "IsEntrust_0"), (("CmnMenu", None), "IsEntrust_0"), (("",), "IsEntrust_0"),
    (GENERAL, 0), (GENERAL, None),
])
def test_resolve_rejects_malformed_scope_chains_or_nontext_names(scopes, name):
    with pytest.raises(ValueError):
        index({}).resolve(scopes, name)


def test_quest_menu_maps_updater_functions_to_known_scope_chains():
    subject = index({QUEST_COMMON: commands(
        ("CmnMenu::DoReturn_Type_0", f'SetUpdaterFunc( "{GENERAL_MENU}" );'),
        ("CmnMenu::DoReturn_Type_14", f'SetUpdaterFunc("{HISTORY_MENU}");'),
        ("CmnMenu::DoReturn_Type_2", 'SetUpdaterFunc( "Func_SubjugationQuestMenu" );'),
        ("CmnMenu::DoReturn_Type_9", ""),
        ("CmnMenu::DoReturn_Type_5", 'SetUpdaterFunc("Func_General QuestMenu");'),
        ("CmnMenu::DoReturn_Type_6", f'SetUpdaterFunc("{GENERAL_MENU}"); Other();'),
        ("CmnMenu::DoReturn_Type_7", "SetUpdaterFunc(Func_GeneralQuestMenu);"),
        ("CmnMenu::DoReturn_Type_8", 'SetUpdaterFunc( "Func_General\u3000" );'))})
    general = subject.quest_menu(0)
    assert general == MenuRoute(0, subject.lookup("CmnMenu::DoReturn_Type_0"), GENERAL_MENU, GENERAL)
    assert general.chain == ("CMenuQuestGeneral", "FCMenuQuestTypeList", "CmnMenu")
    assert subject.quest_menu(14).chain == ("CHistoryQuestMenu", "FCMenuQuestTypeList", "CmnMenu")
    subjugation = subject.quest_menu(2)
    assert (subjugation.hit.row, subjugation.func, subjugation.chain) == (2, "Func_SubjugationQuestMenu", ())
    for category in (9, 5, 6, 7, 8):
        route = subject.quest_menu(category)
        assert route.hit is not None and (route.func, route.chain) == (None, ())
    assert subject.quest_menu(11) == MenuRoute(11, None, None, ())


def test_quest_menu_reports_route_ambiguity_and_rejects_nonnative_categories():
    subject = index({QUEST_COMMON: commands(("CmnMenu::DoReturn_Type_0", f'SetUpdaterFunc("{GENERAL_MENU}");')),
                     SYSTEM: commands(("CmnMenu::DoReturn_Type_0", f'SetUpdaterFunc("{HISTORY_MENU}");'))})
    with pytest.raises(CommandAmbiguous):
        subject.quest_menu(0)
    for category in (True, "0", 0.0, None):
        with pytest.raises(ValueError):
            subject.quest_menu(category)


def test_content_version_follows_bytes_missing_codes_and_invalid_reasons():
    def unreadable(name):
        raise OSError(name)

    base = {SYSTEM: commands(("HasEntrustByType_2", "1;"))}
    version = index(base).content_version
    assert index(dict(base)).content_version == version
    variants = {
        index({SYSTEM: commands(("HasEntrustByType_2", "0;"))}).content_version,
        index({}).content_version,
        NativeCommandIndex(unreadable).content_version,
        index({SYSTEM: NOT_A_TABLE}).content_version,
        index({SYSTEM: table((("_ID", INT_KIND),), [])}).content_version,
        index({**base, QUEST_COMMON: commands()}).content_version,
        index(base, tables=REGISTERED_TABLES[:1]).content_version,
    }
    assert version not in variants and len(variants) == 7


def test_every_registered_table_present_but_empty_reports_no_degradation():
    subject = index({name: commands() for name in REGISTERED_TABLES})
    assert subject.loaded == REGISTERED_TABLES
    assert subject.status() == {"tables": 106, "rows": 0, "missing": {}, "invalid": {}, "duplicate_count": 0,
                                "duplicates": [], "content_version": subject.content_version}
    assert all(subject.lookup(f"HasEntrustByType_{quest_type}") is None for quest_type in range(256))


def test_status_and_ambiguity_diagnostics_bound_duplicate_reports():
    pairs = [(f"Dup{number}", "1;") for number in range(STATUS_LIMIT + 8) for _ in range(2)]
    subject = index({SYSTEM: commands(*pairs), QUEST_COMMON: commands(*[("Many", "1;")] * (STATUS_LIMIT + 8))})
    status = subject.status()
    assert status["duplicate_count"] == len(subject.duplicates()) == STATUS_LIMIT + 9
    assert [entry["id"] for entry in status["duplicates"]] == sorted(subject.duplicates())[:STATUS_LIMIT]
    many = index({SYSTEM: commands(*[("Many", "1;")] * (STATUS_LIMIT + 8))}).status()["duplicates"]
    assert [(entry["occurrences"], len(entry["positions"])) for entry in many] == [(STATUS_LIMIT + 8, STATUS_LIMIT)]
    with pytest.raises(CommandAmbiguous) as error:
        subject.lookup("Many")
    diagnostic = error.value.diagnostic()
    assert len(error.value.positions) == diagnostic["occurrences"] == STATUS_LIMIT + 8
    assert diagnostic["positions"] == [list(position) for position in error.value.positions[:STATUS_LIMIT]]


def test_registered_tables_keep_native_registration_order():
    document = json.loads(files("aigis_ps").joinpath("data/command-tables.json").read_text("utf-8"))
    assert (document["loader"], document["register"]) == ("91CDA0", "902520")
    assert tuple(document["tables"]) == REGISTERED_TABLES and len(set(REGISTERED_TABLES)) == 106
    assert REGISTERED_TABLES[:2] == (SYSTEM, COMMAND) and REGISTERED_TABLES[9] == QUEST_COMMON
    assert REGISTERED_TABLES[-1] == "OnCrystalInventory.atb"


@pytest.mark.parametrize("tables", [SYSTEM, None, (), (SYSTEM, SYSTEM), ("../x.atb",), ("x.txt",), (".atb",), (1,),
                                    ("",)])
def test_table_lists_must_name_unique_native_tables(tables):
    with pytest.raises(ValueError):
        index({}, tables=tables)


def test_reader_must_be_callable():
    with pytest.raises(ValueError):
        NativeCommandIndex(None)


def test_single_table_reads_cover_every_command_table_a_module_names():
    listed = {}
    for entry in SINGLE_TABLE_READS:
        listed.setdefault(entry.module.__name__, set()).add(entry.table)
    assert listed == named_command_tables()


def test_single_table_divergences_flag_shared_keys_and_keys_held_by_another_table():
    resources = {name: commands() for name in REGISTERED_TABLES}
    resources.update({SYSTEM: commands(("Scanned", "1;"), ("Shared", "1;")),
                      COMMAND: commands(("Fixed", "1;"), ("Rated_1", "1;"), ("Twice", "1;"), ("Twice", "0;")),
                      QUEST_COMMON: commands(("Shared", "0;"), ("Rated_2", "1;"))})
    scanner, lookup = ModuleType("scanner"), ModuleType("lookup")
    reads = (SingleTableRead(scanner, SYSTEM, whole_table=True),
             SingleTableRead(lookup, COMMAND, names=("Fixed", "Absent", "Twice"), per_card=("Rated_{}",),
                             identifiers=(command_id("Shared"),)))
    shared = [(SYSTEM, 1), (QUEST_COMMON, 0)]
    assert single_table_divergences(reads, *command_positions(reader(resources)), (1, 2, 3)) == {
        ("scanner", SYSTEM): {command_id("Shared"): shared},
        ("lookup", COMMAND): {"Twice": [(COMMAND, 2), (COMMAND, 3)], "Rated_2": [(QUEST_COMMON, 1)],
                              command_id("Shared"): shared},
    }


@pytest.fixture(scope="module")
def real():
    project = Path(__file__).resolve().parents[1]
    if not (project / "runtime/server/masterdata/active.json").is_file() or not (project.parent / "aigisr_exe").is_dir():
        pytest.skip("Verified local master snapshot and client resources are required")
    master = MasterDataStore(project / "runtime/server/masterdata").snapshot()
    manifest, entries = ResourceLibrary(project / "runtime/resources", client_hash=master.client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    read_asset = lambda name: assets.read(name)[0]
    possessions = PossessionService(project.parent / "aigisr_exe", asset_reader=read_asset)
    quests = QuestCatalog(master, read_asset, possessions=possessions, resource_version=manifest["version"])
    return quests, NativeCommandIndex(quests._asset)


def test_real_registered_tables_are_all_plain_command_tables(real):
    quests, subject = real
    assert subject.missing == {} and subject.invalid == {} and subject.loaded == REGISTERED_TABLES
    assert all(subject.source_sha256.values()) and subject.status()["tables"] == 106 and subject.status()["rows"] == 4718
    for name in REGISTERED_TABLES:
        fields = parse_table(quests._asset(name))["fields"]
        assert [(field["name"], field["type_name"]) for field in fields] == [("_ID", "int32"), ("_Cmnd", "string_ref")]
    assert subject.duplicates() == {
        -906346476: (("OnCommandMenuUnitCheck.atb", 0), ("OnCommandMenuUnitGrowthSelect.atb", 0)),
        431188304: (("OnCommandMenuUnitLibrary.atb", 59), ("OnCommandMenuUnitLibrary.atb", 60))}


def test_real_type_gates_exist_only_for_the_native_offer_rows(real):
    _, subject = real
    expected = {2: (30, 1512763177), 3: (31, 757981119), 8: (32, -1157691849), 16: (33, 1954388820),
                17: (34, 58371010), 18: (40, -1815759277)}
    hits = {quest_type: subject.lookup(f"HasEntrustByType_{quest_type}") for quest_type in range(256)}
    assert {quest_type for quest_type, hit in hits.items() if hit is not None} == expected.keys()
    for quest_type, (row, identifier) in expected.items():
        hit = hits[quest_type]
        assert (hit.table, hit.row, hit.identifier, hit.text, hit.form) == (SYSTEM, row, identifier, "1;", CommandForm.LITERAL_TRUE)


def test_real_menu_gates_live_on_the_quest_type_list_scope(real):
    _, subject = real
    rows = {0: (34, CommandForm.LITERAL_TRUE), 5: (125, CommandForm.LITERAL_TRUE), 1: (126, CommandForm.LITERAL_TRUE),
            3: (127, CommandForm.LITERAL_TRUE), 4: (128, CommandForm.OPEN_MISSION_TAG),
            14: (129, CommandForm.OPEN_MISSION_TAG), 15: (130, CommandForm.OPEN_MISSION_TAG),
            16: (143, CommandForm.OPEN_MISSION_TAG)}
    for category in range(18):
        hit = subject.lookup(f"FCMenuQuestTypeList::IsEntrust_{category}")
        assert (None if hit is None else (hit.table, hit.row, hit.form)) == (
            (QUEST_COMMON, *rows[category]) if category in rows else None)
        assert all(subject.lookup(f"{scope}::IsEntrust_{category}") is None
                   for scope in ("CMenuQuestGeneral", "CHistoryQuestMenu", "CmnMenu"))
        for chain in (GENERAL, HISTORY):
            assert subject.resolve(chain, f"IsEntrust_{category}") == hit
    assert subject.lookup("FCMenuQuestTypeList::IsEntrust_0").identifier == 2052496187
    assert subject.lookup("FCMenuQuestTypeList::IsEntrust_14").text == OPEN_TAG


def test_real_quest_menu_routes(real):
    _, subject = real
    general = (0, 1, 3, 4, 5, 6, 7, 8, 10, 13, 15, 16, 17)
    for category in general:
        route = subject.quest_menu(category)
        assert (route.func, route.chain) == (GENERAL_MENU, GENERAL)
    expected = {14: (13, HISTORY_MENU, HISTORY), 2: (2, "Func_SubjugationQuestMenu", ()),
                12: (11, "Func_TowerTopMenu", ()), 9: (9, None, ())}
    for category, (row, func, chain) in expected.items():
        route = subject.quest_menu(category)
        assert (route.hit.table, route.hit.row, route.func, route.chain) == (QUEST_COMMON, row, func, chain)
    assert subject.quest_menu(9).hit.text == ""
    assert subject.quest_menu(11) == MenuRoute(11, None, None, ())
    assert [subject.quest_menu(category).hit.row for category in (0, 10, 13, 17)] == [0, 10, 12, 16]


def test_real_single_table_reads_match_the_native_search_over_every_table(real):
    quests, subject = real
    table_keys, positions = command_positions(quests._asset)
    assert {key: tuple(places) for key, places in positions.items() if len(places) > 1} == subject.duplicates()
    # Every per-card reader checks the master cards or a subset of them.
    document = loads(quests.snapshot.get_xml("CardData"), limits=MASTER_LIMITS)
    cards = next(column.values for column in document.children if column.name == "CardID")
    divergent = single_table_divergences(SINGLE_TABLE_READS, table_keys, positions, cards)
    assert not divergent, f"Single-table command reads no longer match the native search over every table: {divergent}"

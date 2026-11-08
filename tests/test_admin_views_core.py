import ast
import base64
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import textwrap
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import aigis_ps
from aigis_ps import admin_routes_state, admin_views_account, admin_views_runtime, business_calendar
from aigis_ps.admin_app import LeasedGeneration, create_admin_app
from aigis_ps.admin_command import (DOMAIN_NAME, AdminCommand, AdminError, CommandParams, CommandResult, NoopPreflight,
                                    NoopRestartCheck, PlanStore)
from aigis_ps.admin_config_switch import EMPTY_SET
from aigis_ps.admin_diff import REDACTED, ResponseView, is_xml_document, json_digest, redact
from aigis_ps.admin_fields import (BIT_TABLES, NATIVE_DAY_FIELDS, STATE_KEYS, TYPE_DATE, TYPE_DATETIME, TYPE_TIME, Bit,
                                   BitLevel, BitTable, StateKey, anchors, bit_masks, decode_field, decode_value,
                                   epoch_milliseconds, field_dictionary, ledger_status, logical_day, native_date)
from aigis_ps.admin_registry import AdminCatalog, CommandRegistry, RouteRegistry, ViewRegistry
from aigis_ps.admin_security import API_ROOT
from aigis_ps.admin_views import (MAX_PAGE_ITEMS, PROJECTION_NOTE, PageParams, page, parse_pointer, projected,
                                  redact_at, redacted_view, resolve_pointer, service)
from aigis_ps.admin_views_account import rank_status
from aigis_ps.admin_views_runtime import (ManifestCache, ManifestFacts, config_view, identified_set, json_type,
                                          unimplemented_operations)
from aigis_ps.alxml import loads
from aigis_ps.app import create_app
from aigis_ps.assets import asset_key
from aigis_ps.bootstrap import PGP2_FIELDS, STARTUP_COLLECTIONS, TABLES, WIRE_TYPES, BootstrapService
from aigis_ps.config_registry import ConfigError
from aigis_ps.config_sets import (ACTIVE_POINTER, CONFIG_DIRECTORY, MAX_POINTER_BYTES, SET_ID_LENGTH, ConfigSets,
                                  config_identity)
from aigis_ps.config_state import CONFIGURATION_FIELDS
from aigis_ps.crystal_spending import initialize_spending
from aigis_ps.crystal_wallet import credit_free_crystals
from aigis_ps.energy_clock import JST, energy_seconds, set_anchor
from aigis_ps.errors import ProtocolError
from aigis_ps.generation import Generation, GenerationIdentity, GenerationRuntime
from aigis_ps.login_cooking_state import (LEDGER_KEY as LOGIN_COOKING_KEY, RULES_CHANGED as COOKING_RULES_CHANGED,
                                          initialize_cooking)
from aigis_ps.login_reward_state import LEDGER_KEY as LOGIN_REWARD_KEY, RULES_CHANGED, initialize_login_rewards
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.monthly_capacity import initialize_capacity
from aigis_ps.native_calendar import native_day
from aigis_ps.native_numbers import INT32_MAX, float32
from aigis_ps.player_state import (CLIENT_RUNTIME_FIELDS, LOCK_FLAG, SQUAD_FIELDS, SQUAD_TUTORIAL_FLAGS, UNIT_BOOLEANS,
                                   UNIT_FIELDS, PlayerStateService)
from aigis_ps.protocol import ProtocolRegistry
from aigis_ps.quest_record_admin import ARCHIVE_KEY
from aigis_ps.quest_service import QuestRules
from aigis_ps.rank_reward_state import initialize_rank_rewards
from aigis_ps.storage import MAX_REVISION, Store
from test_bootstrap import asset_root
from test_player_state import assets, master
from test_quest_service import bundle


SOURCE = Path(aigis_ps.__file__).resolve().parent
NOW = datetime(2026, 11, 2, 12, 30)
NOW_JST = NOW.replace(tzinfo=JST)
PORT, GAME_PORT = 18104, 18103
TOKEN = "v" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = f"http://127.0.0.1:{PORT}"
DATABASE = "aigis.sqlite3"
RECOVERY = 120
CLIENT = "c1" * 32
SNAPSHOT, STAGED_SNAPSHOT = "5a" * 12, "6b" * 12
# Without a master directory nothing is on disk, so nothing waits for the next start.
MASTERDATA = {"in_use": SNAPSHOT, "disk": None, "error": None, "roll_forward_pending": False, "next_start": None,
              "staged": None}
LIVE_VIEWS = ("account.projection", "history.audit", "history.failures", "history.generations", "runtime.config",
              "runtime.generation", "runtime.readiness", "runtime.requests", "runtime.versions")
RESOURCE_VERSION = "e" * 40
DEFAULT_DIGEST = config_identity({})
SECRET = "do-not-leak-0123456789"
SYSTEM_RECEIPT = f'<DA><SYSTEM><A1 T="S"><V>ExchangeKey</V></A1><A2 T="S"><V>{SECRET}</V></A2></SYSTEM></DA>'
TRADE_RULES = b'{"reset_type_1":"never","reset_type_2":"window"}'
WRITE_ON_READ_ATTRIBUTES = frozenset(("quest_record", "_collection_profile", "_monthly_profile"))
WRITE_ON_READ_SERVICES = frozenset(("votes", "sugoroku"))
READ = "read"
VIEW_MODULES = ("admin_views_account", "admin_views_runtime")
ROUNDS_TO_WHOLE_DAY = 1 - 2 ** -30
CORRUPTED = "corrupted"
UNREAD_CLAIMS = {
    "startup_collections_schema": "bootstrap", "created_local": "bootstrap", "gift_state": "gifts",
    "unit_dynamic_overrides": "units", "background_history": "backgrounds", "barracks_history": "barracks",
    "blessing_history": "blessings", "current_squad_history": "current_squad", "entrust_history": "entrust",
    "event_pass_history": "event_passes", "evolution_history": "evolution", "fairy_farm_history": "fairy_farm",
    "fame_history": "fame", "item_summon_history": "item_summon_tickets", "login_reward_history": "login_rewards",
    "mission_history": "missions", "orb_exchange_history": "orb_exchange", "package_history": "packages",
    "rank_reward_history": "rank_rewards", "recovery_history": "recovery", "select_ticket_history": "selection_tickets",
    "serial_code_history": "serial_codes", "silver_house_history": "silver_house", "skin_shop_history": "skin_shop",
    "sugoroku_history": "sugoroku", "summon_ticket_history": "summon_tickets", "tower_trade_receipts": "tower_trades",
    "trade_receipts": "trades", "training_history": "training", "unit_detail_history": "unit_detail",
    "unit_sale_history": "unit_sales", "view_event_history": "view_events", "vote_transactions": "votes",
    "vote_last_action": "votes", "present_claim_history": "presents", "present_claim_history_schema": "presents",
    "quest_state": "main_quests", "mission_state": "missions", "skin_acquisition_history": "missions",
    "mission_pass_delivery": "missions", "package_delivery": "packages", "login_reward_delivery": "login_rewards",
    "rank_reward_delivery": "rank_rewards"}
EVENT_READERS = {
    LOGIN_REWARD_KEY: ("login_rewards", "login_rewards_invalid"),
    LOGIN_COOKING_KEY: ("login_rewards", "login_cooking_invalid"),
    "event_pass_automation": ("event_passes", "event_pass_automation_invalid"),
    "event_pass_delivery": ("event_passes", "event_pass_history_invalid"),
    "mission_ep_bindings": ("missions", "mission_ep_bindings_invalid"),
    "package_purchases": ("packages", "package_purchase_state_invalid"),
    "trade_history": ("trades", "trade_history_invalid"),
    "tower_trade_history": ("tower_trades", "tower_trade_history_invalid"),
    "tower_state": ("towers", "tower_state_invalid")}
VOTE_READERS = {
    "vote_rewards": ("votes", "vote_rewards_state_invalid"), "vote_tickets": ("votes", "vote_ticket_state_invalid"),
    "vote_finalists": ("votes", "vote_finalists_state_invalid"), "vote_expiry": ("votes", "vote_expiry_state_invalid"),
    "vote_assault_expiry": ("votes", "vote_assault_expiry_state_invalid"),
    "vote_expiry_notifications": ("votes", "vote_expiry_notifications_invalid"),
    "vote_assault_notifications": ("votes", "vote_assault_notifications_invalid"),
    "vote_cooking": ("vote_cooking", "vote_cooking_state_invalid"),
    "vote_cooking_notifications": ("vote_cooking", "vote_cooking_notifications_invalid")}
SERVICELESS_READERS = frozenset(("event_pass_automation", "event_pass_delivery", "mission_ep_bindings"))


class SoundParams(CommandParams):
    use_bgm: bool


def keep(state, params, context):
    return CommandResult(state)


SOUND = AdminCommand("config.sound.set", "config", SoundParams, keep, frozenset({"config"}),
                     writes=frozenset({"config.UseBgm", "config.UseSe"}))


class Tripwire:
    def __init__(self, name):
        self.name = name

    def __call__(self, *arguments, **options):
        raise AssertionError(f"Admin views must not call the persisting read {self.name}")


@dataclass
class Status:
    value: dict

    def status(self):
        return self.value


def views():
    registry = ViewRegistry()
    admin_views_runtime.register(registry)
    admin_views_account.register(registry)
    return registry


def catalog():
    commands = CommandRegistry({"config": 36})
    commands.add(SOUND)
    routes = RouteRegistry()
    admin_routes_state.register(routes)
    return AdminCatalog(commands, views(), routes, ("admin_routes_state", *VIEW_MODULES))


@pytest.fixture
def bootstrap(asset_root):
    service = BootstrapService(asset_root, clock=lambda: NOW, asset_version="a" * 40)
    for name in WRITE_ON_READ_ATTRIBUTES:
        setattr(service, name, Tripwire(f"bootstrap.{name}"))
    return service


@pytest.fixture
def players(assets, master):
    return PlayerStateService(assets[0], lambda name: master)


def account_state(bootstrap, players):
    state = initialize_capacity(initialize_spending(bootstrap.initial_state(), NOW_JST, origin="new_local"))
    return players.initialize(state)


@dataclass
class Console:
    client: TestClient
    store: Store
    runtime: GenerationRuntime
    data: Path
    bundle: SimpleNamespace
    diagnostics: object

    def get(self, name, params=None, headers=None):
        query = None if params is None else {"params": json.dumps(params)}
        return self.client.get(f"{API_ROOT}/views/{name}", params=query, headers={**AUTH, **(headers or {})})

    def read(self, name, params=None):
        response = self.get(name, params)
        assert response.status_code == 200, response.text
        return response.json()["data"]


def generation_for(bundle, readiness, *, config_set=DEFAULT_DIGEST):
    identity = GenerationIdentity(code_digest="d" * 64, config_set=config_set, content_versions={"bootstrap": "b1"},
                                  client_fingerprint=CLIENT, master_snapshot=SNAPSHOT,
                                  base_resource_version=RESOURCE_VERSION, effective_resource_version=RESOURCE_VERSION)
    return Generation(identity, bundle, bundle.bootstrap.handlers(), readiness, gate_issues=("fixture_gate",),
                      diagnostics=bundle.bootstrap.startup_diagnostics)


def build_console(tmp_path, bootstrap, players, *, state=None, config_set=DEFAULT_DIGEST, **services):
    bundle = SimpleNamespace(bootstrap=bootstrap, player_state=players, initialize=lambda value: value,
                             quests=SimpleNamespace(rules=QuestRules(charisma_recovery_seconds=RECOVERY)),
                             masters=Status({"ready": True, "snapshot": SNAPSHOT, "tables": {}}),
                             native_cdn=Status({"version": RESOURCE_VERSION, "ready": True}),
                             votes=SimpleNamespace(read=Tripwire("VoteService.read")),
                             sugoroku=SimpleNamespace(read=Tripwire("SugorokuService.read")))
    for name, value in services.items():
        setattr(bundle, name, value)
    readiness = {"bootstrap": bootstrap.status(), "wallet": {"ready": True, "user_key": SECRET, "detail": {"depth": 1}}}
    runtime = GenerationRuntime(generation_for(bundle, readiness, config_set=config_set))
    data = tmp_path / "data"
    store = Store(data / DATABASE, account_state(bootstrap, players) if state is None else state)
    application = create_admin_app(runtime, store, data_dir=data, admin_host="127.0.0.1", admin_port=PORT,
                                   game_port=GAME_PORT, token=TOKEN, preflight=NoopPreflight(),
                                   restart_check=NoopRestartCheck(), plan_store=PlanStore(), catalog=catalog(),
                                   ui_directory=tmp_path / "ui")
    return Console(TestClient(application, base_url=BASE), store, runtime, data, bundle,
                   bootstrap.startup_diagnostics)


@pytest.fixture
def console(tmp_path, bootstrap, players):
    return build_console(tmp_path, bootstrap, players)


def error_of(action):
    with pytest.raises(ProtocolError) as caught:
        action()
    return caught.value.code, caught.value.status


def test_views_register_under_dotted_names_with_strict_parameters():
    registry = views()
    assert [view.name for view in registry] == [
        "account.projection", "account.summary", "history.audit", "history.failures", "history.generations",
        "runtime.config", "runtime.generation", "runtime.operations", "runtime.readiness", "runtime.requests",
        "runtime.versions", "state.browse", "state.fields"]
    assert {view.domain for view in registry} == {"account", "history", "runtime", "state"}
    with pytest.raises(ValueError):
        admin_views_runtime.register(registry)


def test_every_view_reads_one_snapshot_and_writes_nothing(console):
    before = console.store.profile()
    generation_id = console.runtime.current.id
    for view in views():
        response = console.get(view.name)
        assert response.status_code == 200, (view.name, response.text)
        body = response.json()
        assert (body["view"], body["revision"], body["generation_id"]) == (view.name, before.revision, generation_id)
        assert set(body["server_now"]) == {"utc", "jst"} and SECRET not in response.text
    after = console.store.profile()
    assert (after.revision, json_digest(after.state)) == (before.revision, json_digest(before.state))
    assert console.runtime.status()["current"]["leases"] == 0
    etag = f'"{before.revision}:{generation_id}"'
    assert console.get("account.summary").headers["etag"] == etag
    assert console.get("account.summary", headers={"if-none-match": etag}).status_code == 304


def test_live_views_are_never_answered_from_a_cache(console):
    assert tuple(view.name for view in views() if view.live) == LIVE_VIEWS
    assert all(view.describe()["live"] is (view.name in LIVE_VIEWS) for view in views())
    etag = f'"{console.store.profile().revision}:{console.runtime.current.id}"'
    for name in LIVE_VIEWS:
        response = console.get(name, headers={"if-none-match": etag})
        assert response.status_code == 200 and "etag" not in response.headers, name


def receiver_names(node):
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            names.add(child.value)
    return names


def persisting_reads(source):
    found = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Attribute) and node.attr in WRITE_ON_READ_ATTRIBUTES:
            found.append(f"{line}: uses .{node.attr}")
        elif isinstance(node, ast.Attribute) and node.attr == READ and receiver_names(node.value) & WRITE_ON_READ_SERVICES:
            found.append(f"{line}: reads through {sorted(receiver_names(node.value) & WRITE_ON_READ_SERVICES)}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in WRITE_ON_READ_ATTRIBUTES:
            found.append(f"{line}: names {node.value!r}")
    return found


def view_sources():
    return sorted(path for path in SOURCE.glob("admin_*.py")
                  if path.stem in ("admin_views", "admin_fields") or path.stem.startswith("admin_views_"))


def test_registered_views_redact_their_results():
    assert all(callable(getattr(view.read, "__wrapped__", None)) for view in views())
    leaking = redacted_view("fixture.leak", "fixture", lambda context, params: {"nested": [{"method_key": SECRET}]})
    assert leaking.read(None, None) == {"nested": [{"method_key": REDACTED}]}


def test_view_sources_never_reach_persisting_reads():
    sources = view_sources()
    assert {path.stem for path in sources} >= {"admin_views", "admin_fields", *VIEW_MODULES}
    assert [f"{path.name}:{issue}" for path in sources for issue in persisting_reads(path.read_text("utf-8"))] == []


@pytest.mark.parametrize("sample", [
    "bundle.bootstrap.quest_record(envelope, store)",
    "bootstrap._collection_profile(envelope, store)",
    "getattr(bootstrap, '_monthly_profile')(envelope, store)",
    "bundle.votes.read(envelope, store)",
    "service(bundle, 'sugoroku').read(envelope, store)",
    "self.votes.read(envelope, store)",
])
def test_persisting_read_scanner_rejects_samples(sample):
    assert persisting_reads(sample)


def test_persisting_read_scanner_allows_pure_readers():
    assert persisting_reads(textwrap.dedent("""
        rows = bundle.card_status.read(state)
        history = read_vote_history(state)
        board = read_sugoroku(state)
        records = bootstrap.table_rows(state, "quest_records")
    """)) == []


@pytest.mark.parametrize("text, expected", [
    ("", ()), ("/", ("",)), ("/tables/game_param/0", ("tables", "game_param", "0")),
    ("/a~1b/~0c/~01", ("a/b", "~c", "~1")), ("//x", ("", "x")),
])
def test_pointers_follow_rfc_6901(text, expected):
    assert parse_pointer(text) == expected


@pytest.mark.parametrize("text", ["tables", "/~", "/a~2", "/a~/b", "#/tables"])
def test_malformed_pointers_are_rejected(text):
    assert error_of(lambda: parse_pointer(text)) == ("admin_pointer_invalid", 422)


def test_resolution_names_the_first_missing_location():
    document = {"a": [1, {"b": None}], "": {"x": 2}}
    assert resolve_pointer(document, ("a", "1", "b")) is None
    assert resolve_pointer(document, ("", "x")) == 2
    for path, missing in ((("a", "01"), "/a/01"), (("a", "2"), "/a/2"), (("a", "-"), "/a/-"),
                          (("a", "0", "x"), "/a/0/x"), (("z", "y"), "/z")):
        with pytest.raises(AdminError) as caught:
            resolve_pointer(document, path)
        assert (caught.value.code, caught.value.status) == ("admin_pointer_missing", 404)
        assert str(caught.value).endswith(missing)


def test_pages_report_their_window():
    items = list(range(7))
    assert page(items, PageParams(offset=0, limit=5)) == {"items": [0, 1, 2, 3, 4], "total": 7, "offset": 0, "limit": 5,
                                                          "next_offset": 5}
    assert page(items, PageParams(offset=5, limit=5), lambda item: item * 10)["items"] == [50, 60]
    assert page(items, PageParams(offset=9))["items"] == [] and page(items, PageParams(offset=9))["next_offset"] is None
    for invalid in ({"limit": MAX_PAGE_ITEMS + 1}, {"limit": 0}, {"offset": -1}, {"offset": "1"}, {"extra": 1}):
        with pytest.raises(ValidationError):
            PageParams(**invalid)


def test_addressed_nodes_are_redacted_as_members_of_their_parent():
    xml = '<?xml version="1.0"?>' + SYSTEM_RECEIPT
    assert redact_at(("user_key",), SECRET) == REDACTED
    assert redact_at(("method_key", "nested"), {"x": SECRET}) == REDACTED
    assert redact_at(("receipts", "response"), xml)["redacted"] == "response"
    assert SECRET not in json.dumps(redact_at((), {"deep": {"bootstrap_token": SECRET, "response": xml}}))
    assert redact_at(("plain",), {"value": 1}) == {"value": 1}


@pytest.mark.parametrize("prolog", ["", "\ufeff", '\ufeff<?xml version="1.0"?>\n', " \r\n\t", "<!-- saved receipt -->\n",
                                    "<?receipt kept?>", "\ufeff <!-- a --> <?b c?>\n",
                                    '<?xml version="1.0"?>\n<!-- after the declaration -->\n'])
def test_receipts_are_redacted_after_any_prolog_the_xml_reader_accepts(prolog):
    xml = prolog + SYSTEM_RECEIPT
    raw = xml.encode("utf-8")
    assert loads(raw).children[0].name == "SYSTEM" and is_xml_document(xml) and is_xml_document(raw)
    digest = hashlib.sha256(raw).hexdigest()
    for value, encoding in ((xml, "text"), (base64.b64encode(raw).decode("ascii"), "base64")):
        assert redact_at(("receipts", "response"), value) == {"redacted": "response", "encoding": encoding,
                                                               "bytes": len(raw), "sha256": digest}
    assert redact({"notes": [xml]}) == {"notes": [{"redacted": "response", "encoding": "text", "bytes": len(raw),
                                                   "sha256": digest}]}
    masked = redact({"response": xml}, responses=ResponseView.MASKED)["response"]
    assert REDACTED in masked and SECRET not in masked and loads(masked.encode("utf-8")).children[0].name == "SYSTEM"


@pytest.mark.parametrize("text", ["plain note", " leading space", "\ufeffplain", "<!-- unclosed comment",
                                  "<?unclosed instruction", "<!-- note -->text", "<html><DA/></html>", "x<DA/>", "<", ""])
def test_text_that_only_resembles_a_receipt_is_kept(text):
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    assert not is_xml_document(text) and not is_xml_document(text.encode("utf-8"))
    assert redact({"note": text, "response": text}) == {"note": text, "response": text}
    assert redact({"response": encoded}) == {"response": encoded}


def test_projections_are_marked_and_services_are_required():
    assert projected(value=1) == {"projected": True, "persisted": False, "note": PROJECTION_NOTE, "value": 1}
    bundle = SimpleNamespace(bootstrap="ready", quests=None)
    assert service(bundle, "bootstrap") == "ready"
    for name in ("quests", "votes"):
        assert error_of(lambda: service(bundle, name)) == ("admin_view_service_unavailable", 503)


@pytest.mark.parametrize("type_code, value, expected", [
    (TYPE_DATE, 0, {"date": None}),
    (TYPE_DATE, date(2026, 11, 2).toordinal() - 1, {"date": "2026-11-02"}),
    (TYPE_DATE, INT32_MAX, {"invalid": True}),
    (TYPE_TIME, 0, {"seconds": 0, "time": "00:00:00"}),
    (TYPE_TIME, 0.5, {"seconds": 43200, "time": "12:00:00"}),
    (TYPE_TIME, 45296 / 86400, {"seconds": 45296, "time": "12:34:56"}),
    (TYPE_TIME, float32(1 - 2 ** -24), {"seconds": 86400, "time": "24:00:00"}),
    (TYPE_TIME, (43203.5 - 0.0005) / 86400, {"seconds": 43204, "time": "12:00:04"}),
    (TYPE_TIME, "noon", {"invalid": True}),
    (TYPE_DATETIME, "", {"datetime": None}),
    (TYPE_DATETIME, "2026/11/02 12:30:00", {"datetime": "2026-11-02T12:30:00+09:00"}),
    (TYPE_DATETIME, "2026-11-02 12:30:00", {"invalid": True}),
    (1, 5, {}), (5, True, {}), (33, "name", {}),
])
def test_values_decode_by_native_type(type_code, value, expected):
    assert decode_value(type_code, value) == expected


def test_native_days_round_trip_with_the_native_calendar():
    assert native_date(native_day(NOW_JST)) == NOW_JST.date()
    assert native_date(0) is None


def test_integer_day_columns_and_bit_fields_decode_through_the_dictionary():
    assert NATIVE_DAY_FIELDS == {"crystals.Date", "quest_records.FirstDefeatDate", "game_param2.HomeCookingReceivedDate"}
    today = native_day(NOW_JST)
    assert decode_field("crystals", "Date", 1, today) == {"value": today, "date": "2026-11-02"}
    assert decode_field("crystals", "Num", 1, today) == {"value": today}
    cooked = date(2026, 11, 2).toordinal() - 1
    assert decode_field("game_param2", "HomeCookingReceivedDate", 1, cooked) == {"value": cooked, "date": "2026-11-02"}
    assert decode_field("game_param2", "HomeCookingReceivedDate", 1, 0) == {"value": 0, "date": None}
    decoded = decode_field("game_param2", "_EvCmb", 1, 1 | 8)
    assert decoded["bits"] == [{"bit": 0, "name": "legacy_event_flags_converted", "level": BitLevel.VERIFIED, "set": True},
                               {"bit": 3, "name": None, "level": BitLevel.UNKNOWN, "set": True}]
    assert decode_field("game_param2", "_EvCmb", 1, "1") == {"value": "1"}


def test_flag_words_list_the_bits_the_game_sets():
    assert bit_masks(0) == () and bit_masks(SQUAD_TUTORIAL_FLAGS) == (2, 4) and bit_masks(1 << 31 | 1) == (1, 1 << 31)
    assert [(bit.mask, bit.name) for bit in BIT_TABLES["config.MenuTutorialFlags"].bits] == [
        (1, "gift_tutorial"), (2, "squad_tutorial"), (4, "squad_tutorial")]
    assert [bit.name for bit in BIT_TABLES["card_status.A7"].bits] == ["first_episode", "second_episode",
                                                                     "harlem_episode"]
    assert [bit.mask for bit in BIT_TABLES["units.GeneralPurposeFlags"].bits] == [LOCK_FLAG, 4]


def test_bit_tables_decode_every_bit_of_an_int32():
    table = BIT_TABLES["game_param2._EvCmb"]
    assert table.decode(0) == [{"bit": 0, "name": "legacy_event_flags_converted", "level": BitLevel.VERIFIED, "set": False}]
    decoded = table.decode(-1)
    assert len(decoded) == 32 and all(bit["set"] for bit in decoded)
    assert [bit["bit"] for bit in decoded if bit["level"] is BitLevel.UNKNOWN] == list(range(1, 32))
    named = BitTable((Bit(1 << 31, "high", BitLevel.NAMED), Bit(2, "low", BitLevel.VERIFIED)))
    assert named.describe() == [{"bit": 1, "name": "low", "level": BitLevel.VERIFIED},
                                {"bit": 31, "name": "high", "level": BitLevel.NAMED}]


@pytest.mark.parametrize("mask, name, level", [
    (3, "two_bits", BitLevel.VERIFIED), (0, "none", BitLevel.VERIFIED), (1 << 32, "wide", BitLevel.VERIFIED),
    (-1, "negative", BitLevel.VERIFIED), (True, "boolean", BitLevel.VERIFIED), (1, "", BitLevel.VERIFIED),
    (1, "guess", BitLevel.UNKNOWN),
])
def test_bit_meanings_name_one_documented_bit(mask, name, level):
    with pytest.raises(ValueError):
        Bit(mask, name, level)


def test_bit_tables_reject_repeated_or_empty_meanings():
    for bits in ((), (Bit(1, "a", BitLevel.VERIFIED), Bit(1, "b", BitLevel.NAMED)), ("bit",)):
        with pytest.raises(ValueError):
            BitTable(bits)


def test_energy_anchors_become_jst_instants(bootstrap):
    row = bootstrap.initial_state()["tables"]["game_param"][0]
    instant = {"seconds": energy_seconds(NOW_JST), "jst": NOW_JST.isoformat()}
    assert anchors(row) == {"Charisma": instant, "ActionPoint": instant}
    row.update(CharismaChangeDate=0, CharismaChangeTime=0.0, ActionPointChangeDate=INT32_MAX)
    decoded = anchors(row)
    assert decoded["Charisma"] == {"seconds": 0, "jst": None}
    assert decoded["ActionPoint"]["invalid"] is True and decoded["ActionPoint"]["jst"] is None
    row.update(CharismaChangeTime=ROUNDS_TO_WHOLE_DAY, ActionPointChangeDate=-1)
    unreadable = {"seconds": None, "jst": None, "invalid": True}
    assert anchors(row) == {"Charisma": unreadable, "ActionPoint": unreadable}


@pytest.mark.parametrize("moment, expected", [
    (datetime(2026, 11, 2, 3, 59, 59, tzinfo=JST), date(2026, 11, 1)),
    (datetime(2026, 11, 2, 4, 0, tzinfo=JST), date(2026, 11, 2)),
    (datetime(2026, 11, 1, 19, 0, tzinfo=timezone.utc), date(2026, 11, 2)),
    (datetime(2026, 11, 1, 18, 59, tzinfo=timezone.utc), date(2026, 11, 1)),
    (datetime(2026, 11, 2, 4, 0), date(2026, 11, 2)),
])
def test_logical_day_turns_at_four_in_the_morning_jst(moment, expected):
    assert logical_day(moment) == business_calendar.business_date(moment) == expected


def test_epoch_milliseconds_decode_ledger_clocks():
    assert epoch_milliseconds(0) == "1970-01-01T00:00:00+00:00"
    assert epoch_milliseconds(1_700_000_000_123) == "2023-11-14T22:13:20.123000+00:00"
    assert epoch_milliseconds(2 ** 62) is None


def test_field_dictionary_assembles_the_native_contracts(asset_root):
    (asset_root / asset_key(TABLES["reproduction"].resource)).unlink()
    service = BootstrapService(asset_root, clock=lambda: NOW, asset_version="a" * 40)
    dictionary = field_dictionary(service, {"config.UseBgm": "config.sound.set", "units.Exp": "units.edit_growth"})
    assert list(dictionary["tables"]) == list(TABLES)
    for table, spec in TABLES.items():
        entry = dictionary["tables"][table]
        assert [(field["field"], field["type_code"], field["wire"]) for field in entry["fields"]] == list(spec.fields)
        assert all(field["value_type"] == WIRE_TYPES[field["type_code"]].value for field in entry["fields"])
        assert entry["verified"] is (table != "reproduction") and (entry["issue"] is None) is (table != "reproduction")
        assert entry["resource"] == spec.resource and entry["single_row"] is (table not in ("crystals", "quest_records"))
    config = {field["field"]: field for field in dictionary["tables"]["config"]["fields"]}
    assert config["UseBgm"]["owner"] == "config.sound.set" and config["UseSe"]["owner"] is None
    assert config["UserName"]["size"] == 40
    assert {field["size"] for field in dictionary["tables"]["reproduction"]["fields"]} == {None}
    assert [field["field"] for field in dictionary["tables"]["crystals"]["fields"] if field["native_day"]] == ["Date"]
    assert [field["field"] for field in dictionary["tables"]["game_param2"]["fields"] if field["native_day"]] == [
        "HomeCookingReceivedDate"]
    assert [field["field"] for field in dictionary["tables"]["game_param2"]["fields"] if field["bits"]] == ["_EvCmb"]
    assert [field["field"] for field in dictionary["tables"]["config"]["fields"] if field["bits"]] == [
        "MenuTutorialFlags"]
    assert [unit["field"] for unit in dictionary["units"] if unit["bits"]] == ["GeneralPurposeFlags"]
    assert dictionary["collections"] == [{"container": key, "table": value} for key, value in STARTUP_COLLECTIONS.items()]
    assert [(unit["field"], unit["wire"], unit["boolean"]) for unit in dictionary["units"]] == [
        (name, wire, name in UNIT_BOOLEANS) for name, wire in UNIT_FIELDS]
    assert {unit["field"]: unit["owner"] for unit in dictionary["units"]}["Exp"] == "units.edit_growth"
    assert [field["field"] for field in dictionary["client_runtime"]] == list(CLIENT_RUNTIME_FIELDS)
    assert all(field["client_derived"] and not field["persisted"] for field in dictionary["client_runtime"])
    assert [(field["field"], field["wire"], field["value_type"]) for field in dictionary["squads"]] == [
        (name, wire, kind.value) for name, wire, kind in SQUAD_FIELDS]
    assert {field["field"]: (field["wire"], field["owner"]) for field in dictionary["configuration"]}["UseBgm"] == (
        "A2", "config.sound.set")
    assert len(dictionary["configuration"]) == len(CONFIGURATION_FIELDS)
    assert dictionary["pgp2"] == [{"wire": wire, "field": name} for wire, name in PGP2_FIELDS]
    assert dictionary["state_keys"] == {key: claim.domain for key, claim in STATE_KEYS.items()}
    assert dictionary["owners"] == {"config.UseBgm": "config.sound.set", "units.Exp": "units.edit_growth"}


def test_state_keys_claim_domains_and_readers():
    assert all(DOMAIN_NAME.fullmatch(claim.domain) for claim in STATE_KEYS.values())
    assert {key: claim.domain for key, claim in STATE_KEYS.items() if claim.read is None} == UNREAD_CLAIMS
    readers = {**EVENT_READERS, **VOTE_READERS}
    assert {key: STATE_KEYS[key].domain for key in readers} == {key: domain for key, (domain, _) in readers.items()}
    assert len({STATE_KEYS[key].read for key in readers}) == len(readers)
    named = {"serial_code_redemptions": "serial_codes", "evolution_rewards": "evolution_rewards",
             LOGIN_REWARD_KEY: "login_rewards", ARCHIVE_KEY: "quest_records"}
    assert {key: STATE_KEYS[key].domain for key in named} == named
    assert STATE_KEYS[ARCHIVE_KEY].read is not None
    assert STATE_KEYS["bootstrap_schema"].read is STATE_KEYS["tables"].read
    assert STATE_KEYS["presents_schema"].read is STATE_KEYS["last_present_id"].read


def full_state(bootstrap, players):
    state = initialize_rank_rewards(account_state(bootstrap, players), new_account=True)
    return initialize_login_rewards(state, NOW_JST, new_account=True)


def beginner_frozen_at(state, total):
    return {**state, LOGIN_REWARD_KEY: {**state[LOGIN_REWARD_KEY], "beginner_total": total}}


def cooking_frozen_at(state, border):
    return {**state, LOGIN_COOKING_KEY: {**state[LOGIN_COOKING_KEY], "border": border}}


def test_ledger_status_runs_each_claimed_reader_once(bootstrap, players, bundle):
    rewards = bundle.login_rewards
    state = {**beginner_frozen_at(full_state(bootstrap, players), rewards.beginner_total),
             "fixture_unclaimed": {"raw": True}, "fixture_counted": 1, "fixture_shared": 2}
    calls = []

    def counted(value, bundle):
        calls.append(1)

    claims = {**STATE_KEYS, "fixture_counted": StateKey("fixture", counted),
              "fixture_shared": StateKey("fixture", counted)}
    services = SimpleNamespace(bootstrap=bootstrap, player_state=players, login_rewards=rewards)
    entries = {entry["key"]: entry for entry in ledger_status(state, services, claims)}
    assert list(entries) == list(state) and calls == [1]
    assert entries["fixture_shared"] == {"key": "fixture_shared", "claimed": True, "domain": "fixture", "checked": True,
                                         "valid": True, "error": None}
    assert entries["fixture_unclaimed"] == {"key": "fixture_unclaimed", "claimed": False, "domain": None, "checked": False}
    assert entries["created_local"] == {"key": "created_local", "claimed": True, "domain": "bootstrap", "checked": False}
    checked = {key for key, entry in entries.items() if entry["checked"]}
    assert checked >= {"bootstrap_schema", "tables", "energy_clock", business_calendar.MARKER_KEY, "crystal_spending",
                       "monthly_capacity", "player_state", "rank_rewards", LOGIN_REWARD_KEY}
    assert {key: entry["error"] for key, entry in entries.items() if entry["checked"] and not entry["valid"]} == {}
    assert ledger_status(state, services, claims, keys=("fixture_counted", "energy_clock")) == [
        entries["fixture_counted"], entries["energy_clock"]]
    assert calls == [1, 1]


def test_ledger_status_reports_each_broken_ledger(bootstrap, players):
    state = full_state(bootstrap, players)
    bundle = SimpleNamespace(bootstrap=bootstrap, player_state=players)
    for key, claim in STATE_KEYS.items():
        if claim.read is None:
            continue
        broken = {**state, key: CORRUPTED}
        [entry] = [entry for entry in ledger_status(broken, bundle) if entry["key"] == key]
        assert entry["valid"] is False and entry["error"]["code"] != "internal_error", (key, entry)
    failing = {"fixture_failing": StateKey("fixture", lambda value, bundle: 1 / 0)}
    [entry] = ledger_status({"fixture_failing": 1}, bundle, failing)
    assert entry["error"] == {"code": "internal_error", "message": "ZeroDivisionError"}


def test_new_saves_claim_every_key_and_readers_reach_the_owning_services(bundle):
    state = bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))
    entries = ledger_status(state, bundle)
    assert [entry["key"] for entry in entries if not entry["claimed"]] == []
    assert {entry["key"]: entry["error"] for entry in entries if entry["checked"] and not entry["valid"]} == {}
    voting = bundle.votes is not None and bundle.votes.transactions is not None
    readers = {**EVENT_READERS, **(VOTE_READERS if voting else {})}
    refused = {key: ledger_status({**state, key: CORRUPTED}, bundle, keys=(key,))[0]["error"]["code"] for key in readers}
    assert refused == {key: code for key, (_, code) in readers.items()}


def test_state_browser_reports_a_login_ledger_frozen_at_another_beginner_total(tmp_path, bootstrap, players, bundle):
    rewards, state = bundle.login_rewards, full_state(bootstrap, players)
    changed = beginner_frozen_at(state, rewards.beginner_total + 1)
    console = build_console(tmp_path, bootstrap, players, state=changed, login_rewards=rewards)
    ledger = console.read("state.browse", {"pointer": f"/{LOGIN_REWARD_KEY}"})["ledger"]
    assert (ledger["domain"], ledger["valid"], ledger["error"]["code"]) == (
        "login_rewards", False, RULES_CHANGED)
    matching = beginner_frozen_at(state, rewards.beginner_total)
    [entry] = ledger_status(matching, SimpleNamespace(login_rewards=rewards), keys=(LOGIN_REWARD_KEY,))
    assert (entry["valid"], entry["error"]) == (True, None)


def test_state_browser_reports_a_cooking_ledger_frozen_at_another_border(tmp_path, bootstrap, players, bundle):
    rewards = bundle.login_rewards
    state = initialize_cooking(full_state(bootstrap, players), border=rewards.cooking_border)
    changed = cooking_frozen_at(state, rewards.cooking_border + 1)
    console = build_console(tmp_path, bootstrap, players, state=changed, login_rewards=rewards)
    ledger = console.read("state.browse", {"pointer": f"/{LOGIN_COOKING_KEY}"})["ledger"]
    assert (ledger["domain"], ledger["valid"], ledger["error"]["code"]) == (
        "login_rewards", False, COOKING_RULES_CHANGED)
    [entry] = ledger_status(state, SimpleNamespace(login_rewards=rewards), keys=(LOGIN_COOKING_KEY,))
    assert (entry["valid"], entry["error"]) == (True, None)
    [unserved] = ledger_status(changed, SimpleNamespace(), keys=(LOGIN_COOKING_KEY,))
    assert (unserved["valid"], unserved["error"]["code"]) == (False, "admin_view_service_unavailable")


def test_ledger_status_names_missing_services(bootstrap, players):
    state = full_state(bootstrap, players)
    entries = {entry["key"]: entry for entry in ledger_status(state, SimpleNamespace())}
    assert entries["player_state"]["error"]["code"] == "admin_view_service_unavailable"
    assert entries["energy_clock"]["valid"] is True
    readers = {**EVENT_READERS, **VOTE_READERS}
    missing = {entry["key"]: entry["error"] for entry in ledger_status(state, SimpleNamespace(), keys=readers)}
    assert {key for key, error in missing.items() if error is None} == SERVICELESS_READERS
    assert {error["code"] for error in missing.values() if error is not None} == {"admin_view_service_unavailable"}


def test_schema_markers_are_checked_by_their_collection_readers(bootstrap, players):
    codes = {"universal_flags_schema": "universal_flags_invalid", "backgrounds_schema": "background_state_invalid",
             "perused_events_schema": "peruse_state_invalid", "filter_settings_schema": "filter_state_invalid",
             "vote_state": "vote_state_invalid"}
    state = {**full_state(bootstrap, players), **dict.fromkeys(codes, 1),
             "vote_state": {"schema_version": 1, "entries": {}}}
    bundle = SimpleNamespace(bootstrap=bootstrap, player_state=players)
    assert all(entry["valid"] for entry in ledger_status(state, bundle, keys=codes))
    for key, code in codes.items():
        [entry] = ledger_status({**state, key: 2}, bundle, keys=(key,))
        assert entry["error"]["code"] == code, key
    assert {key: STATE_KEYS[key].domain for key in codes} == {
        "universal_flags_schema": "bootstrap", "backgrounds_schema": "backgrounds", "perused_events_schema": "peruse",
        "filter_settings_schema": "filters", "vote_state": "votes"}


@pytest.mark.parametrize("change, error", [
    (lambda state: None, None),
    (lambda state: state.pop(business_calendar.MARKER_KEY), None),
    (lambda state: state[business_calendar.MARKER_KEY].update(origin="converted"), "business_calendar_invalid"),
], ids=["marked", "legacy", "invalid"])
def test_business_calendar_claim_accepts_legacy_saves_and_reports_invalid_markers(bootstrap, players, change, error):
    state = account_state(bootstrap, players)
    change(state)
    [entry] = ledger_status(state, SimpleNamespace(), keys=(business_calendar.MARKER_KEY,))
    assert (entry["claimed"], entry["domain"], entry["checked"]) == (True, "account", True)
    assert entry["valid"] is (error is None) and (entry["error"] or {}).get("code") == error
    bundle = SimpleNamespace(bootstrap=bootstrap, player_state=players)
    listed = {item["key"]: item for item in ledger_status(state, bundle)}
    assert (business_calendar.MARKER_KEY in listed) is (business_calendar.MARKER_KEY in state)
    assert [key for key, item in listed.items() if item["checked"] and not item["valid"]] == (
        [] if error is None else [business_calendar.MARKER_KEY])


def test_service_collections_are_read_through_their_services():
    calls = []

    def refusing(name):
        def read(state):
            calls.append(name)
            raise ProtocolError(f"{name}_fixture_invalid", "Refused for the fixture", 503)
        return read

    bundle = SimpleNamespace(fame_repository=SimpleNamespace(read=refusing("fame")),
                             fairy_farm=SimpleNamespace(read=refusing("fairy_farm")),
                             silver_house=SimpleNamespace(read=refusing("silver_house")),
                             presents=SimpleNamespace(rows=refusing("presents")))
    state = {"fame_depository_schema": 1, "fairy_farm_schema": 1, "silver_house_schema": 1, "presents_schema": 1,
             "last_present_id": 3}
    entries = {entry["key"]: (entry["domain"], entry["error"]["code"]) for entry in ledger_status(state, bundle)}
    assert entries == {"fame_depository_schema": ("fame", "fame_fixture_invalid"),
                       "fairy_farm_schema": ("fairy_farm", "fairy_farm_fixture_invalid"),
                       "silver_house_schema": ("silver_house", "silver_house_fixture_invalid"),
                       "presents_schema": ("presents", "presents_fixture_invalid"),
                       "last_present_id": ("presents", "presents_fixture_invalid")}
    assert sorted(calls) == ["fairy_farm", "fame", "presents", "silver_house"]
    missing = {entry["key"]: entry["error"]["code"] for entry in ledger_status(state, SimpleNamespace())}
    assert set(missing.values()) == {"admin_view_service_unavailable"} and set(missing) == set(state)


def test_state_browser_pages_lists_and_summarizes_nested_values(tmp_path, bootstrap, players):
    rows = [{"index": index, "label": f"row {index}"} for index in range(600)]
    state = {**account_state(bootstrap, players), "fixture_rows": rows, "fixture_ids": list(range(MAX_PAGE_ITEMS + 1)),
             "fixture_small": list(range(MAX_PAGE_ITEMS))}
    console = build_console(tmp_path, bootstrap, players, state=state)
    first = console.read("state.browse", {"pointer": "/fixture_rows", "limit": MAX_PAGE_ITEMS})
    assert (first["type"], first["total"], len(first["items"]), first["next_offset"]) == ("array", 600, 500, 500)
    assert first["items"][0] == {"index": 0, "type": "object", "value": rows[0]}
    assert first["ledger"] == {"key": "fixture_rows", "claimed": False, "domain": None, "checked": False}
    rest = console.read("state.browse", {"pointer": "/fixture_rows", "offset": 500, "limit": MAX_PAGE_ITEMS})
    assert [item["index"] for item in rest["items"]] == list(range(500, 600)) and rest["next_offset"] is None
    assert console.get("state.browse", {"pointer": "/fixture_rows", "limit": MAX_PAGE_ITEMS + 1}).status_code == 422
    root = console.read("state.browse")
    assert (root["pointer"], root["parent"], root["type"]) == ("", None, "object")
    members = {item["key"]: item for item in root["items"]}
    assert members["tables"] == {"key": "tables", "type": "object", "size": len(console.store.profile().state["tables"])}
    assert members["bootstrap_schema"] == {"key": "bootstrap_schema", "type": "integer", "value": 1}
    assert members["fixture_ids"] == {"key": "fixture_ids", "type": "array", "size": MAX_PAGE_ITEMS + 1}
    assert members["fixture_small"] == {"key": "fixture_small", "type": "array", "value": list(range(MAX_PAGE_ITEMS))}
    ids = console.read("state.browse", {"pointer": "/fixture_ids", "offset": MAX_PAGE_ITEMS})
    assert ids["items"] == [{"index": MAX_PAGE_ITEMS, "type": "integer", "value": MAX_PAGE_ITEMS}]
    assert {entry["key"] for entry in root["ledgers"]} == set(console.store.profile().state)
    leaf = console.read("state.browse", {"pointer": "/tables/game_param/0/Money"})
    assert leaf == {"pointer": "/tables/game_param/0/Money", "parent": "/tables/game_param/0", "type": "integer",
                    "value": 1000, "ledger": {"key": "tables", "claimed": True, "domain": "bootstrap", "checked": True,
                                              "valid": True, "error": None}}
    assert "ledgers" not in leaf and "ledger" not in root
    missing = console.get("state.browse", {"pointer": "/tables/absent"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "admin_pointer_missing"
    invalid = console.get("state.browse", {"pointer": "tables"})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "admin_pointer_invalid"


@pytest.mark.parametrize("value, expected", [
    (None, "null"), (True, "boolean"), (0, "integer"), (0.5, "number"), ("", "string"), ({}, "object"), ([], "array"),
])
def test_browsed_values_name_their_json_type(value, expected):
    assert json_type(value) == expected


def test_state_browser_redacts_receipts_and_secret_fields(tmp_path, bootstrap, players):
    xml = '<?xml version="1.0"?>' + SYSTEM_RECEIPT
    encoded = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    receipts = {"response": encoded, "user_key": SECRET, "entries": [{"response": xml, "method_key": SECRET}],
                "nested": {"deeper": {"bootstrap_token": SECRET}}, "method_key": {"fixture_issued": [SECRET]}}
    console = build_console(tmp_path, bootstrap, players,
                            state={**account_state(bootstrap, players), "fixture_receipts": receipts})
    for location in ("", "/fixture_receipts", "/fixture_receipts/response", "/fixture_receipts/user_key",
                     "/fixture_receipts/entries", "/fixture_receipts/entries/0", "/fixture_receipts/nested/deeper",
                     "/fixture_receipts/entries/0/response", "/fixture_receipts/method_key",
                     "/fixture_receipts/method_key/fixture_issued"):
        response = console.get("state.browse", {"pointer": location})
        assert response.status_code == 200 and SECRET not in response.text, location
        assert "fixture_issued" not in response.text or location == "/fixture_receipts/method_key/fixture_issued"
    assert console.read("state.browse", {"pointer": "/fixture_receipts/user_key"})["value"] == REDACTED
    hidden = console.read("state.browse", {"pointer": "/fixture_receipts/method_key"})
    assert hidden == {"pointer": "/fixture_receipts/method_key", "parent": "/fixture_receipts", "type": "string",
                      "value": REDACTED, "ledger": {"key": "fixture_receipts", "claimed": False, "domain": None,
                                                    "checked": False}}
    receipt = console.read("state.browse", {"pointer": "/fixture_receipts/response"})
    assert receipt["type"] == "object" and receipt["value"]["encoding"] == "base64"
    members = {item["key"]: item for item in console.read("state.browse", {"pointer": "/fixture_receipts"})["items"]}
    assert members["user_key"] == {"key": "user_key", "type": "string", "value": REDACTED}
    assert members["nested"] == {"key": "nested", "type": "object", "size": 1}


def test_account_summary_decodes_game_param_and_ledgers(tmp_path, bootstrap, players):
    day = native_day(NOW_JST)
    state = credit_free_crystals(account_state(bootstrap, players), 5, day)
    console = build_console(tmp_path, bootstrap, players, state=state)
    summary = console.read("account.summary")
    game = summary["game_param"]
    assert game["ok"] is True and game["fields"]["Money"] == {"wire": "A1", "type_code": 1, "value": 1000}
    assert game["fields"]["CharismaChangeDate"]["date"] == "2026-11-02"
    assert game["fields"]["CharismaChangeTime"]["time"] == "12:30:00"
    assert game["anchors"]["Charisma"]["jst"] == NOW_JST.isoformat()
    assert game["rank"] == {"charisma_max": 60, "charisma_capacity": 60, "experience_floor": 0, "experience_next": 16}
    assert summary["crystals"]["totals"] == {"paid": 0, "free": 5, "total": 5}
    [bucket] = summary["crystals"]["buckets"]["items"]
    assert bucket["Date"] == {"wire": "Date", "type_code": 1, "value": day, "date": "2026-11-02"}
    assert summary["spending"]["ledger"]["baseline_source"] == "new_local"
    assert summary["spending"]["last_time_utc"] == NOW_JST.astimezone(timezone.utc).isoformat()
    assert summary["capacity"]["ledger"]["current"] == 0 and summary["capacity"]["ok"] is True
    assert summary["energy_clock"] == {"ok": True, "marker": state["energy_clock"]}
    assert summary["business_calendar"] == {"ok": True, "marker": dict(business_calendar.MARKER)}


def test_account_summary_pages_crystal_buckets(tmp_path, bootstrap, players):
    day, state = native_day(NOW_JST), account_state(bootstrap, players)
    for earlier in range(3):
        state = credit_free_crystals(state, 1, day - earlier)
    console = build_console(tmp_path, bootstrap, players, state=state)
    buckets = console.read("account.summary", {"limit": 2})["crystals"]["buckets"]
    assert [item["Date"]["value"] for item in buckets["items"]] == [day, day - 1]
    assert (buckets["total"], buckets["offset"], buckets["next_offset"]) == (3, 0, 2)
    rest = console.read("account.summary", {"offset": 2})["crystals"]
    assert [item["Date"]["value"] for item in rest["buckets"]["items"]] == [day - 2] and rest["totals"]["free"] == 3
    assert console.get("account.summary", {"limit": MAX_PAGE_ITEMS + 1}).status_code == 422


def test_rank_status_stays_inside_the_rank_table(bootstrap):
    rows = bootstrap.rank_rows
    top = rank_status(rows, {"Rank": len(rows), "ExtendedMaxCharisma": 3})
    assert top["charisma_capacity"] == rows[-1]["CharismaMax"] + 3
    assert top["experience_next"] == sum(entry["NextExp"] for entry in rows)
    assert top["experience_floor"] == top["experience_next"] - rows[-1]["NextExp"]
    for rank in (0, len(rows) + 1):
        assert rank_status(rows, {"Rank": rank, "ExtendedMaxCharisma": 0}) is None


def test_account_summary_reports_each_unreadable_ledger(tmp_path, bootstrap, players):
    state = players.initialize(bootstrap.initial_state())
    state["energy_clock"]["origin"] = "unknown"
    state[business_calendar.MARKER_KEY]["origin"] = "unknown"
    summary = build_console(tmp_path, bootstrap, players, state=state).read("account.summary")
    assert summary["game_param"]["ok"] is True and summary["crystals"]["ok"] is True
    expected = {"spending": "crystal_spending_required", "capacity": "capacity_baseline_required",
                "energy_clock": "energy_clock_invalid", "business_calendar": "business_calendar_invalid"}
    assert not any(summary[name]["ok"] for name in expected)
    assert {name: summary[name]["error"]["code"] for name in expected} == expected


def test_account_summary_reports_an_unmarked_save_as_a_legacy_calendar(tmp_path, bootstrap, players):
    state = account_state(bootstrap, players)
    del state[business_calendar.MARKER_KEY]
    summary = build_console(tmp_path, bootstrap, players, state=state).read("account.summary")
    assert summary["business_calendar"] == {"ok": True, "marker": None}
    assert all(summary[name]["ok"] for name in ("game_param", "crystals", "spending", "capacity", "energy_clock"))


def test_account_summary_decodes_game_param_beside_unreadable_anchors(tmp_path, bootstrap, players):
    state = account_state(bootstrap, players)
    state["tables"]["game_param"][0]["CharismaChangeTime"] = ROUNDS_TO_WHOLE_DAY
    summary = build_console(tmp_path, bootstrap, players, state=state).read("account.summary")
    game = summary["game_param"]
    assert game["ok"] is True and game["fields"]["CharismaChangeTime"]["time"] == "24:00:00"
    assert game["anchors"]["Charisma"] == {"seconds": None, "jst": None, "invalid": True}
    assert game["anchors"]["ActionPoint"]["jst"] == NOW_JST.isoformat()
    assert summary["energy_clock"]["error"]["code"] == "energy_clock_anchor_invalid"


def test_energy_projection_estimates_without_persisting(tmp_path, bootstrap, players):
    state = account_state(bootstrap, players)
    row = state["tables"]["game_param"][0]
    row["NowCharisma"] = 50
    set_anchor(row, energy_seconds(NOW_JST) - 600)
    console = build_console(tmp_path, bootstrap, players, state=state)
    before = console.store.profile()
    result = console.read("account.projection")
    energy = result["energy"]
    assert (energy["ok"], energy["projected"], energy["persisted"], energy["note"]) == (True, True, False, PROJECTION_NOTE)
    assert energy["recovery_seconds"] == RECOVERY and energy["charisma"] == {"before": 50, "after": 55}
    assert energy["anchors"]["before"]["Charisma"]["jst"] == (NOW_JST - timedelta(minutes=10)).isoformat()
    assert energy["anchors"]["after"]["Charisma"]["jst"] == NOW_JST.isoformat()
    assert energy["as_of"]["jst"] == NOW_JST.isoformat() and energy["capacity"]["ok"] is True
    leased = LeasedGeneration(console.runtime.current)
    acquisition = leased.acquisition_day(leased.now())
    assert result["calendar"] == {"logical_day": "2026-11-02", "acquisition_day": acquisition,
                                  "acquisition_date": native_date(acquisition).isoformat()}
    assert console.read("account.projection") == result
    after = console.store.profile()
    assert (after.revision, after.state) == (before.revision, before.state)


def test_projection_calendar_turns_the_logical_day_at_four_jst(tmp_path, asset_root, players):
    early = BootstrapService(asset_root, clock=lambda: datetime(2026, 11, 2, 3, 59), asset_version="a" * 40)
    calendar = build_console(tmp_path, early, players).read("account.projection")["calendar"]
    assert calendar["logical_day"] == "2026-11-01"


def test_energy_projection_settles_a_private_copy(bootstrap, players, monkeypatch):
    def settle_in_place(state, rank_rows, now, *, recovery_seconds):
        state["tables"]["game_param"][0]["NowCharisma"] += 1
        return state

    monkeypatch.setattr(admin_views_account, "settle_energy", settle_in_place)
    state = account_state(bootstrap, players)
    digest, charisma = json_digest(state), state["tables"]["game_param"][0]["NowCharisma"]
    quests = SimpleNamespace(rules=QuestRules(charisma_recovery_seconds=RECOVERY))
    projection = admin_views_account.energy_projection(SimpleNamespace(bootstrap=bootstrap, quests=quests), state, NOW_JST)
    assert projection["charisma"] == {"before": charisma, "after": charisma + 1}
    assert json_digest(state) == digest


def test_energy_projection_uses_the_generation_recovery_rate(tmp_path, bootstrap, players):
    state = account_state(bootstrap, players)
    row = state["tables"]["game_param"][0]
    row["NowCharisma"] = 50
    set_anchor(row, energy_seconds(NOW_JST) - 600)
    recovery = SimpleNamespace(recovery_seconds=300)
    console = build_console(tmp_path, bootstrap, players, state=state, quests=None, recovery=recovery)
    assert console.read("account.projection")["energy"]["charisma"] == {"before": 50, "after": 52}
    console.bundle.recovery = None
    energy = console.read("account.projection")["energy"]
    assert (energy["ok"], energy["error"]["code"]) == (False, "admin_view_service_unavailable")


def config_context(console, digest):
    current = console.runtime.current
    generation = replace(current, identity=replace(current.identity, config_set=digest))
    return SimpleNamespace(generation=SimpleNamespace(generation=generation),
                           services=SimpleNamespace(store=console.store, data_dir=console.data))


def test_runtime_config_reports_running_active_and_staged(console):
    assert console.read("runtime.config") == {
        "running": {"digest": DEFAULT_DIGEST, "set": EMPTY_SET},
        "active": {"digest": None, "active": None, "set": EMPTY_SET, "error": None, "roll_forward_pending": False,
                   "next_start": EMPTY_SET},
        "staged": {"known": True, "set": None}, "masterdata": MASTERDATA}
    sets = ConfigSets(console.data)
    created = sets.create({"trade-rules": TRADE_RULES}, source="console")
    pointer = sets.activate(created.identifier, 0, expected=None)
    view = console.read("runtime.config")
    assert view["active"]["active"] == pointer.describe() and view["active"]["next_start"] == created.identifier
    assert view["staged"] == {"known": True, "set": created.identifier}
    running = config_view(config_context(console, config_identity(created.explicit)), None)
    assert running["running"]["set"] == created.identifier and running["staged"] == {"known": True, "set": None}
    console.store.record_generation(EMPTY_SET, "hot")
    behind = config_view(config_context(console, config_identity(created.explicit)), None)
    assert (behind["active"]["roll_forward_pending"], behind["active"]["next_start"]) == (True, EMPTY_SET)
    assert behind["staged"] == {"known": True, "set": EMPTY_SET}
    overridden = config_view(config_context(console, "f" * 64), None)
    assert overridden["running"] == {"digest": "f" * 64, "set": None}
    assert overridden["staged"] == {"known": False, "set": None} and overridden["masterdata"] == MASTERDATA


def test_runtime_config_shows_the_master_snapshot_waiting_for_the_next_start(console, monkeypatch):
    calls = []

    def staged(store, masters, in_use):
        calls.append((store, masters.directory, in_use))
        return SimpleNamespace(describe=lambda: {**MASTERDATA, "next_start": STAGED_SNAPSHOT, "staged": STAGED_SNAPSHOT})

    monkeypatch.setattr(admin_views_runtime, "master_state", staged)
    assert console.read("runtime.config")["masterdata"]["staged"] == STAGED_SNAPSHOT
    assert calls == [(console.store, MasterDataStore.for_data_dir(console.data).directory, SNAPSHOT)]


def test_runtime_config_reports_unreadable_pointers(console):
    active = console.data / CONFIG_DIRECTORY / ACTIVE_POINTER
    active.parent.mkdir(parents=True)
    active.write_bytes(b"not json")
    broken = console.read("runtime.config")
    assert broken["active"]["error"]["code"] == "config_pointer_invalid" and broken["active"]["set"] is None
    assert broken["masterdata"] == MASTERDATA
    assert broken["staged"] == {"known": True, "set": None}
    active.write_bytes(b" " * (MAX_POINTER_BYTES + 1))
    oversized = console.read("runtime.config")
    assert oversized["active"]["error"]["code"] == "config_pointer_invalid"
    assert oversized["staged"] == {"known": False, "set": None}
    assert oversized["running"] == {"digest": DEFAULT_DIGEST, "set": EMPTY_SET}


def test_runtime_config_reports_unusable_data_directories(console, monkeypatch):
    def rejected(data_dir):
        raise ConfigError("config_path_rejected", "Links and junctions are not accepted for local data", 503)

    monkeypatch.setattr(admin_views_runtime, "ConfigSets", rejected)
    assert console.read("runtime.config") == {
        "running": {"digest": DEFAULT_DIGEST, "set": None},
        "active": {"digest": None, "active": None, "set": None, "roll_forward_pending": None, "next_start": None,
                   "error": {"code": "config_path_rejected",
                             "message": "Links and junctions are not accepted for local data"}},
        "staged": {"known": False, "set": None}, "masterdata": MASTERDATA}


def test_identified_sets_require_the_full_configuration_digest(console):
    sets = ConfigSets(console.data)
    created = sets.create({"trade-rules": TRADE_RULES}, source="console")
    digest = config_identity(created.explicit)
    assert identified_set(DEFAULT_DIGEST, sets) == EMPTY_SET
    assert identified_set(digest, sets) == created.identifier
    assert identified_set(created.identifier + "0" * (len(digest) - SET_ID_LENGTH), sets) is None
    assert identified_set(digest[:SET_ID_LENGTH], sets) is None
    assert identified_set("first", sets) is None


def test_runtime_versions_compare_client_manifest_and_master_snapshot(console):
    manifest_path = console.data.parent / "client" / "client-manifest.json"
    versions = console.read("runtime.versions")
    assert versions["client_manifest"] == {"path": str(manifest_path), "present": False, "valid": None}
    assert versions["masterdata"] == {"in_use": SNAPSHOT, "disk": {"ready": True, "snapshot": SNAPSHOT, "tables": {}},
                                      "matches": True}
    manifest_path.parent.mkdir()
    manifest = {"schema_version": 2, "prepared_at": "2026-11-01T00:00:00+00:00", "source_sha256": CLIENT,
                "resource_snapshot": {"version": RESOURCE_VERSION}, "event_login_revival": {"rules_hash": "h" * 64},
                "files": [{"path": "x"}] * 3}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    prepared = console.read("runtime.versions")["client_manifest"]
    assert prepared["client"] == {"prepared": CLIENT, "generation": CLIENT, "matches": True}
    assert prepared["resources"] == {"prepared": RESOURCE_VERSION, "served": RESOURCE_VERSION,
                                     "generation": RESOURCE_VERSION, "matches": True}
    assert prepared["overlays"] == {"event_pass_revival": {"present": False, "rules_hash": None},
                                    "event_login_revival": {"present": True, "rules_hash": "h" * 64},
                                    "purchased_login": {"present": False, "rules_hash": None},
                                    "mission_windows": {"present": False, "rules_hash": None},
                                    "command_texts": {"present": False, "rules_hash": None},
                                    "shop_window_closure": {"present": False, "rules_hash": None}}
    closed = {**manifest, "shop_window_closure": {"schema_version": 1, "products": [{"shop_id": 1}]}}
    manifest_path.write_text(json.dumps(closed), encoding="utf-8")
    assert console.read("runtime.versions")["client_manifest"]["overlays"]["shop_window_closure"] == {
        "present": True, "rules_hash": None}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    console.bundle.native_cdn = Status({"version": "f" * 40})
    console.bundle.masters = Status({"ready": False, "snapshot": SNAPSHOT, "code": "masterdata_invalid"})
    changed = console.read("runtime.versions")
    assert changed["client_manifest"]["resources"]["matches"] is False and changed["masterdata"]["matches"] is None
    console.bundle.masters = Status({"ready": True, "snapshot": "6b" * 12, "tables": {}})
    assert console.read("runtime.versions")["masterdata"]["matches"] is False
    console.bundle.native_cdn = None
    del console.bundle.masters
    absent = console.read("runtime.versions")
    assert absent["client_manifest"]["resources"]["served"] is None and absent["masterdata"]["disk"] is None
    other_client = "d0" * 32
    manifest_path.write_text(json.dumps({**manifest, "source_sha256": other_client}), encoding="utf-8")
    assert console.read("runtime.versions")["client_manifest"]["client"] == {"prepared": other_client,
                                                                           "generation": CLIENT, "matches": False}
    for broken in (b"{not json", b"[" * 100_000, b"[]", b'{"schema_version": 2, "schema_version": 2}',
                   b'{"schema_version": 2, "prepared_at": NaN}'):
        manifest_path.write_bytes(broken)
        assert console.read("runtime.versions")["client_manifest"] == {"path": str(manifest_path), "present": True,
                                                                         "valid": False}


def test_client_manifests_are_parsed_once_per_content(console, monkeypatch):
    monkeypatch.setattr(admin_views_runtime, "CLIENT_MANIFESTS", ManifestCache())
    parsed, original = [], ManifestFacts.parse

    def counted(cls, data):
        parsed.append(len(data))
        return original(data)

    monkeypatch.setattr(ManifestFacts, "parse", classmethod(counted))
    manifest_path = console.data.parent / "client" / "client-manifest.json"
    manifest_path.parent.mkdir()
    manifest = {"schema_version": 2, "source_sha256": CLIENT, "resource_snapshot": {"version": RESOURCE_VERSION},
                "files": [{"path": "x"}] * 3}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    first = console.read("runtime.versions")["client_manifest"]
    assert console.read("runtime.versions")["client_manifest"] == first and len(parsed) == 1
    manifest_path.write_text(json.dumps({**manifest, "source_sha256": "d0" * 32}), encoding="utf-8")
    assert console.read("runtime.versions")["client_manifest"]["client"]["matches"] is False and len(parsed) == 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert console.read("runtime.versions")["client_manifest"] == first and len(parsed) == 3


def test_manifest_facts_keep_only_well_typed_values():
    loose = ManifestFacts.parse(b'{"schema_version": "2", "resource_snapshot": [], "prepared_at": 1, '
                                b'"purchased_login": null, "event_pass_revival": {"rules_hash": 7}}')
    assert loose == ManifestFacts(schema_version=None, prepared_at=None, client=None, resources=None,
                                  overlays=(("event_pass_revival", True, None), ("event_login_revival", False, None),
                                            ("purchased_login", False, None), ("mission_windows", False, None),
                                            ("command_texts", False, None), ("shop_window_closure", False, None)))


def local_status(directory, handlers, readiness):
    application = create_app(directory, handlers=handlers, status_provider=lambda: dict(readiness))
    return TestClient(application, base_url=f"http://127.0.0.1:{GAME_PORT}").get("/local/status").json()


def test_unimplemented_operations_match_the_local_status_route(tmp_path, console):
    generation, registry = console.runtime.current, ProtocolRegistry.load()
    operations = console.read("runtime.operations")
    status = local_status(tmp_path / "game", generation.handlers, generation.readiness)
    assert operations["unimplemented"] == status["unimplemented_operations"]
    assert operations["configured_handlers"] == status["configured_handlers"]
    assert operations["registered_operations"] == len(registry.by_registration)
    readiness = {"bootstrap": {"unimplemented_operations": ["fixture", 7]}}
    assert unimplemented_operations(registry, generation.handlers, readiness)[-1] == "fixture"
    named = {next(iter(registry.by_registration.values())).name: keep}
    expected = local_status(tmp_path / "named", named, {})["unimplemented_operations"]
    assert unimplemented_operations(registry, named, {}) == expected
    assert len(expected) < len(registry.by_registration)


def test_runtime_generation_and_readiness_read_live_status(console):
    generation = console.runtime.current
    view = console.read("runtime.generation")
    assert (view["id"], view["identity"], view["gate_issues"]) == (generation.id, generation.identity.to_dict(),
                                                                   ["fixture_gate"])
    assert view["runtime"]["current"] == {"id": generation.id, "leases": 1}
    assert view["client"]["restart_required"] is False
    readiness = console.read("runtime.readiness")["domains"]
    assert readiness["wallet"] == {"ready": True, "user_key": REDACTED}
    assert readiness["bootstrap"]["startup_diagnostics"] == {"attention_required": False, "loads": {}}
    with pytest.raises(ProtocolError):
        with console.diagnostics.load(36):
            raise ProtocolError("fixture_refused", "Refused for the fixture", 503)
    loads = console.read("runtime.readiness")["domains"]["bootstrap"]["startup_diagnostics"]["loads"]
    assert loads["36"]["outcome"] == "refused"


def test_requests_and_history_views_page_store_records(console):
    for route in ("115", "116", "137"):
        console.store.record_request(None, route, 10, 20, 200)
    requests = console.read("runtime.requests", {"limit": 2})["requests"]
    assert [entry["route"] for entry in requests] == ["137", "116"]
    for limit in (0, 201):
        assert console.get("runtime.requests", {"limit": limit}).status_code == 422
    recorded = console.store.recent_requests
    console.store.recent_requests = lambda limit: [*recorded(limit), {"route": "fixture", "user_key": SECRET}]
    leaked = console.get("runtime.requests")
    assert SECRET not in leaked.text and leaked.json()["data"]["requests"][-1]["user_key"] == REDACTED
    del console.store.recent_requests
    for tier in ("hot", "gated", "content"):
        console.store.record_generation(EMPTY_SET, tier)
    first = console.read("history.generations", {"limit": 2})
    assert [item["tier"] for item in first["items"]] == ["content", "gated"] and first["next_cursor"] == 2
    rest = console.read("history.generations", {"cursor": first["next_cursor"], "limit": 2})
    assert [item["tier"] for item in rest["items"]] == ["hot"] and rest["next_cursor"] is None
    console.store.record_admin_failure("command", "admin_fixture", 409, "Fixture failure", detail={"user_key": SECRET})
    failures = console.get("history.failures")
    assert SECRET not in failures.text and failures.json()["data"]["items"][0]["detail"]["user_key"] == REDACTED
    console.store.commit_admin(str(uuid.uuid4()), "a" * 64, None, None, {
        "kind": "command", "operations": [{"operation": "config.sound.set"}], "targets": [],
        "diff": {"method_key": SECRET}, "result": {}})
    audit = console.get("history.audit")
    assert SECRET not in audit.text and audit.json()["data"]["items"][0]["diff"]["method_key"] == REDACTED
    for name in ("history.audit", "history.failures", "history.generations"):
        assert console.get(name, {"cursor": 0}).status_code == 422
        assert console.get(name, {"cursor": MAX_REVISION + 1}).status_code == 422
        assert console.read(name, {"cursor": MAX_REVISION})["next_cursor"] is None


def test_field_view_attaches_the_catalog_field_owners(console):
    fields = console.read("state.fields")
    assert fields["owners"] == {"config.UseBgm": "config.sound.set", "config.UseSe": "config.sound.set"}
    assert {field["field"]: field["owner"] for field in fields["configuration"]}["UseSe"] == "config.sound.set"
    assert all(entry["verified"] for entry in fields["tables"].values())


def test_views_take_no_unknown_parameters(console):
    assert console.get("runtime.generation", {}).status_code == 422
    assert console.get("state.browse", {"pointer": "", "depth": 1}).status_code == 422
    assert console.get("state.browse", {"pointer": "/" + "x" * 1024}).status_code == 422

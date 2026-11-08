from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps import admin_routes_state, admin_views_events, admin_views_rules, admin_views_votes
from aigis_ps.admin_app import create_admin_app
from aigis_ps.admin_cmd_events import MIGRATE_COOKING
from aigis_ps.admin_cmd_migrations import (REBASE_BEGINNER, REBIND_EVENT_PASS, RESOLVE_COMEBACK_LOGIN,
                                           RESOLVE_DAILY_GIFT, RESOLVE_EVENT_LOGIN, RESOLVE_LOGIN_STAMPS,
                                           RESOLVE_PURCHASED_LOGIN)
from aigis_ps.admin_command import NoopPreflight, NoopRestartCheck, PlanStore
from aigis_ps.admin_config_switch import EMPTY_SET
from aigis_ps.admin_diff import REDACTED, json_digest
from aigis_ps.admin_fields import ledger_status
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS
from aigis_ps.admin_registry import AdminCatalog, CommandRegistry, RouteRegistry, ViewRegistry, discover
from aigis_ps.admin_views import MAX_PAGE_ITEMS, PROJECTION_NOTE, PageParams
from aigis_ps.admin_views_events import (BOARD_READ, COMEBACK_BAND_UNAVAILABLE, DAILY_GIFT_SOURCE_UNAVAILABLE,
                                         DEFINITION_MISSING, PACKAGE_OFFER_UNKNOWN, PURCHASED_CAMPAIGN_UNAVAILABLE,
                                         PURCHASED_REWARD_MISSING, TOWER_TRADE_STATE, Readback)
from aigis_ps.admin_views_rules import RUNNING_UNKNOWN
from aigis_ps.admin_views_votes import FROZEN, LIVE, RECORDED, VOTE_GROUP_MISSING, VOTE_READ, VOTE_SUBMIT
from aigis_ps.business_calendar import business_month
from aigis_ps.comeback_login_service import comeback_reward_definition
from aigis_ps.comeback_login_state import (begin_comeback_reward, begin_comeback_round, complete_comeback_reward,
                                           defer_comeback_reward, observe_comeback_login, void_comeback_reward)
from aigis_ps.config_registry import REGISTRY, ConfigError, ConfigRegistry
from aigis_ps.config_sets import ConfigSets, config_identity
from aigis_ps.daily_gift_service import VOIDED as DAILY_GIFT_VOIDED, daily_gift_definition
from aigis_ps.daily_gift_state import begin_daily_gift, complete_daily_gift, defer_daily_gift
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_service import event_login_definition
from aigis_ps.event_login_state import (LEDGER_KEY as EVENT_LOGIN_KEY, begin_event_login, complete_event_login,
                                        defer_event_login, void_event_login)
from aigis_ps.event_pass_service import AUTOMATION_KEY as PASS_AUTOMATION, pass_identity, reward_semantics
from aigis_ps.event_pass_state import DEFINITION_CHANGED as PASS_CHANGED, mark_event_pass, rebind_definition
from aigis_ps.generation import Generation, GenerationIdentity, GenerationRuntime
from aigis_ps.login_cooking_state import LEDGER_KEY as COOKING_KEY, initialize_cooking
from aigis_ps.login_reward_service import OFFLINE_COOKING_MIGRATION
from aigis_ps.login_reward_state import LEDGER_KEY as LOGIN_REWARD_KEY, advance_login_rewards, initialize_login_rewards
from aigis_ps.login_stamp_pending import MAX_PENDING_STAMPS, PENDING_KEY, hold_stamp
from aigis_ps.protocol import ProtocolRegistry
from aigis_ps.purchased_login_state import (add_purchased_login, begin_purchased_reward, defer_purchased_reward,
                                            purchase_window, purchased_login_clock, purchased_login_identity,
                                            purchased_login_time, void_purchased_reward)
from aigis_ps.storage import Store
from aigis_ps.sugoroku_state import initialize_sugoroku, mark_sugoroku, sync_sugoroku
from aigis_ps.tower_state import DOMAIN as TOWER_STATE, SCHEMA_VERSION as TOWER_SCHEMA
from aigis_ps.tower_trade_state import TowerTradeHistory
from aigis_ps.trade_history import TradeHistory, TradeRules
from aigis_ps.vote_cooking_notifications import VoteCookingNotifications
from aigis_ps.vote_expiry import ASSAULT_GOLD_PER_RIGHT, ASSAULT_KEY, ASSAULT_NOTIFICATION_KEY, NOTIFICATION_KEY
from aigis_ps.vote_state import initialize_vote_state, record_vote, vote_key
from test_admin_views_core import (BASE, CLIENT, DATABASE, DEFAULT_DIGEST, GAME_PORT, NOW_JST, PORT, RESOURCE_VERSION,
                                   SECRET, SNAPSHOT, TOKEN, WRITE_ON_READ_ATTRIBUTES, Console, Status, Tripwire,
                                   account_state, bootstrap, generation_for, persisting_reads, players, view_sources)
from test_bootstrap import asset_root
from test_player_state import assets, master
from test_quest_service import bundle


MODULES = (admin_views_events, admin_views_rules, admin_views_votes)
MODULE_NAMES = ("admin_views_events", "admin_views_rules", "admin_views_votes")
EVENT_NAMES = tuple(f"events.{name}" for name in (
    "event_login", "comeback_login", "purchased_login", "daily_gift", "login_rewards", "event_pass",
    "mission_ep_bindings", "sugoroku", "packages", "trades", "tower_trades"))
VOTE_NAMES = tuple(f"votes.{name}" for name in ("history", "rewards", "tickets", "rankings", "expiry", "assault",
                                                "cooking", "receipts"))
LIVE_NAMES = ("events.comeback_login", "events.purchased_login", "events.sugoroku", "events.tower_trades",
              "events.trades", "rules.config")
RULE_NAMES = ("rules.config",)
ALL_NAMES = (*EVENT_NAMES, *VOTE_NAMES, *RULE_NAMES)
VIEWS = {view.name: view for module in MODULES for view in module.VIEWS}
DAY = timedelta(days=1)
FIRST, SECOND, LATER = NOW_JST - 2 * DAY, NOW_JST - DAY, NOW_JST + 30 * DAY
FIRST_DAY, SECOND_DAY, NOW_DAY, LATER_DAY = "2026-10-31", "2026-11-01", "2026-11-02", "2026-12-02"
NOW_MONTH, LATER_MONTH, EARLIER_MONTH = "2026-11", "2026-12", "2026-10"
MONTH_END = datetime(2026, 12, 1, 3, 59, 59, 999999, tzinfo=JST)
MONTH_START = datetime(2026, 12, 1, 4, tzinfo=JST)
EARLIER = datetime(2026, 10, 15, tzinfo=JST)
BEFORE_CALENDAR = datetime(1960, 1, 1, tzinfo=JST)
HASH_A, HASH_B, HASH_C, HASH_D, HASH_E = ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)
CAPACITY = "present_capacity"
VOTE_ITEM, FINAL_ITEM, LOST_ITEM = "item_vote_016001", "item_vote_016004", "item_vote_015001"
UNKNOWN_DIGEST = "f" * 64
FIXTURE_KEY = "fixture-rules"
SERVICE_UNAVAILABLE = "admin_view_service_unavailable"


def view_catalog():
    views = ViewRegistry()
    for module in MODULES:
        module.register(views)
    routes = RouteRegistry()
    admin_routes_state.register(routes)
    return AdminCatalog(CommandRegistry({}), views, routes, ("admin_routes_state", *MODULE_NAMES))


def console_for(tmp_path, bundle, state, generation=None):
    generation = generation or generation_for(bundle, {"bootstrap": bundle.bootstrap.status()})
    runtime = GenerationRuntime(generation)
    data = tmp_path / "data"
    store = Store(data / DATABASE, state)
    application = create_admin_app(runtime, store, data_dir=data, admin_host="127.0.0.1", admin_port=PORT,
                                   game_port=GAME_PORT, token=TOKEN, preflight=NoopPreflight(),
                                   restart_check=NoopRestartCheck(), plan_store=PlanStore(), catalog=view_catalog(),
                                   ui_directory=tmp_path / "ui")
    return Console(TestClient(application, base_url=BASE), store, runtime, data, bundle,
                   bundle.bootstrap.startup_diagnostics)


def stub_bundle(bootstrap, players, **services):
    bundle = SimpleNamespace(bootstrap=bootstrap, player_state=players, initialize=lambda value: value,
                             masters=Status({"ready": True, "snapshot": SNAPSHOT, "tables": {}}),
                             native_cdn=Status({"version": RESOURCE_VERSION, "ready": True}),
                             votes=SimpleNamespace(read=Tripwire("VoteService.read")),
                             sugoroku=SimpleNamespace(read=Tripwire("SugorokuService.read"), catalog=board_catalog(),
                                                      prepare_month=lambda state, now: deepcopy(state)))
    for name, value in services.items():
        setattr(bundle, name, value)
    return bundle


def read(name, state, *, params=None, revision=0, now=NOW_JST, **services):
    context = SimpleNamespace(state=state, generation=SimpleNamespace(bundle=SimpleNamespace(**services)),
                              now=now, revision=revision)
    result = VIEWS[name].read(context, PageParams() if params is None else params)
    # Views answer through JSONResponse, so every result must survive the same encoding.
    return json.loads(json.dumps(result, allow_nan=False))


def entries(result):
    return {json.dumps(item["key"], sort_keys=True): item for item in result["entries"]["items"]}


def keyed(result, **key):
    return entries(result)[json.dumps(key, sort_keys=True)]


def state_of(result):
    return result["definition"]["state"]


def refusing(code, status=503):
    def refuse(*arguments, **options):
        raise ProtocolError(code, "Refused for the fixture", status)
    return refuse


class Calls(list):
    def record(self, name, result):
        def call(*arguments):
            self.append((name, arguments))
            return result(*arguments) if callable(result) else result
        return call

    def names(self):
        return [name for name, _ in self]


@pytest.fixture
def base(bootstrap, players):
    return account_state(bootstrap, players)


def test_views_register_under_their_domains_with_page_parameters():
    registry = ViewRegistry()
    for module in MODULES:
        module.register(registry)
    assert sorted(view.name for view in registry) == sorted(ALL_NAMES)
    assert {view.domain for view in registry} == {"events", "votes", "rules"}
    assert all(view.params_model is PageParams for view in registry if view.domain != "rules")
    assert registry.get("rules.config").params_model is None
    assert tuple(view.name for view in registry if view.live) == LIVE_NAMES
    assert all(callable(getattr(view.read, "__wrapped__", None)) for view in registry)
    with pytest.raises(ValueError):
        admin_views_votes.register(registry)
    catalog = discover(PREFLIGHT_DOMAINS)
    assert set(MODULE_NAMES) <= set(catalog.modules)
    assert all(name in catalog.views for name in ALL_NAMES)


def test_view_sources_never_reach_persisting_reads():
    sources = {path.stem: path for path in view_sources()}
    assert set(MODULE_NAMES) <= set(sources)
    assert [f"{name}:{issue}" for name in MODULE_NAMES
            for issue in persisting_reads(sources[name].read_text("utf-8"))] == []


def test_readbacks_name_registered_client_requests():
    registry = ProtocolRegistry.load()
    routes = {profile.name for profile in registry.profiles if not profile.registration_keys}
    described = {ledger.name: ledger.readback.describe()
                 for ledger in (*admin_views_events.LEDGERS, *admin_views_votes.LEDGERS)}
    for name, readback in described.items():
        assert readback["requests"], name
        assert all(request in registry.by_registration if isinstance(request, int) else request in routes
                   for request in readback["requests"]), name
    assert described["sugoroku"]["requests"] == [BOARD_READ] and described["sugoroku"]["none_reasons"]
    assert described["tower_trades"]["requests"] == [TOWER_TRADE_STATE]
    assert described["trades"]["requests"] == [137]
    assert described["event_login"]["requests"] == [42]
    assert described["history"]["requests"] == [VOTE_READ, 137]
    assert described["receipts"]["requests"] == [VOTE_SUBMIT]
    with pytest.raises(ValueError):
        Readback()
    with pytest.raises(ValueError):
        Readback(("not_a_domain",))


def test_every_view_reads_one_snapshot_and_writes_nothing(tmp_path, bootstrap, players):
    # The stub vote receipt holds a method key, so the secret check below has something real to catch.
    bundle = stub_bundle(bootstrap, players, **vote_services(Calls()))
    console = console_for(tmp_path, bundle, account_state(bootstrap, players))
    before = console.store.profile()
    generation_id = console.runtime.current.id
    for name in ALL_NAMES:
        response = console.get(name)
        assert response.status_code == 200, (name, response.text)
        body = response.json()
        assert (body["view"], body["revision"], body["generation_id"]) == (name, before.revision, generation_id)
        assert set(body["server_now"]) == {"utc", "jst"} and SECRET not in response.text
    after = console.store.profile()
    assert (after.revision, json_digest(after.state)) == (before.revision, json_digest(before.state))
    assert console.runtime.status()["current"]["leases"] == 0
    login = console.read("events.event_login")
    assert login["service"] == {"name": "event_logins", "available": False, "content_version": None}
    assert login["ledger"] == {"keys": {EVENT_LOGIN_KEY: False}, "ok": True, "last_time": None, "last_time_utc": None}
    history = console.read("votes.history")
    assert history["ledger"]["ok"] is True and history["service"]["available"] is True
    [receipt] = console.read("votes.receipts")["entries"]["items"]
    assert receipt["detail"]["method_key"] == REDACTED and receipt["detail"]["response"]["redacted"] == "response"


def test_entries_page_within_the_view_limit(tmp_path, bootstrap, players, base):
    state = deepcopy(base)
    for card in range(1, 8):
        state = record_vote(state, item_id=VOTE_ITEM, group_id=1, card_id=card, count=card, definition=HASH_A)
    console = console_for(tmp_path, stub_bundle(bootstrap, players), state)
    window = console.read("votes.history", {"offset": 2, "limit": 3})["entries"]
    assert (window["total"], window["offset"], window["limit"], window["next_offset"]) == (7, 2, 3, 5)
    assert [item["key"]["card_id"] for item in window["items"]] == [3, 4, 5]
    assert console.read("votes.history", {"offset": 6, "limit": 3})["entries"]["next_offset"] is None
    assert console.get("votes.history", {"limit": MAX_PAGE_ITEMS + 1}).status_code == 422
    assert console.get("votes.history", {"offset": -1}).status_code == 422
    assert console.get("rules.config", {}).status_code == 422


def event_campaign(identifier, amount=100):
    def rewards_for(ordinal):
        return [SimpleNamespace(ordinal=ordinal, config_id=identifier, item_type=2, item_id="", item_param=amount,
                                param1=0, evolution_count=0, pause_period=0)]
    return SimpleNamespace(campaign_id=identifier, login_bonus_type=1, rewards_for=rewards_for)


class EventCatalog:
    def __init__(self, campaigns):
        self.campaigns = campaigns

    def require_public(self, campaign_id):
        if campaign_id not in self.campaigns:
            raise ProtocolError("event_login_campaign_unknown", "Campaign is absent from the fixture", 404)
        return self.campaigns[campaign_id]


def test_event_logins_list_deliveries_pending_and_definition_state(base):
    seven, eight = event_campaign(7), event_campaign(8)
    state = begin_event_login(base, 7, 1, event_login_definition(seven, 1), FIRST)
    state, _ = complete_event_login(state, 7, [11], FIRST)
    state = begin_event_login(state, 8, 1, event_login_definition(eight, 1), FIRST)
    state, _ = complete_event_login(state, 8, [12], FIRST)
    state = begin_event_login(state, 7, 2, event_login_definition(seven, 2), SECOND)
    state = defer_event_login(state, 7, CAPACITY)
    view = read("events.event_login", state, event_logins=SimpleNamespace(catalog=EventCatalog({7: seven})))
    delivered = keyed(view, campaign_id=7, ordinal=1)
    assert (delivered["status"], state_of(delivered)) == ("delivered", "matching")
    assert delivered["detail"] == {"earned_day": FIRST_DAY, "delivery_day": FIRST_DAY, "mail_ids": [11]}
    pending = keyed(view, campaign_id=7, ordinal=2)
    assert (pending["status"], pending["reason"], state_of(pending)) == ("pending", CAPACITY, "matching")
    assert pending["detail"] == {"earned_day": SECOND_DAY}
    missing = keyed(view, campaign_id=8, ordinal=1)
    assert (state_of(missing), missing["definition"]["error"]["code"]) == (
        "unavailable", "event_login_campaign_unknown")
    assert view["summary"] == {"statuses": {"delivered": 2, "pending": 1}, "reasons": {CAPACITY: 1},
                               "definitions": {"matching": 2, "unavailable": 1}}
    assert view["ledger"]["last_time_utc"] is not None and view["ledger"]["keys"] == {EVENT_LOGIN_KEY: True}
    changed = read("events.event_login", state,
                   event_logins=SimpleNamespace(catalog=EventCatalog({7: event_campaign(7, 200)})))
    assert state_of(keyed(changed, campaign_id=7, ordinal=1)) == "changed"


def test_voided_event_logins_are_counted_apart_from_deliveries(base):
    seven = event_campaign(7)
    state = begin_event_login(base, 7, 1, event_login_definition(seven, 1), FIRST)
    state, _ = complete_event_login(state, 7, [11], FIRST)
    state = void_event_login(begin_event_login(state, 7, 2, event_login_definition(seven, 2), SECOND), 7)
    view = read("events.event_login", state, event_logins=SimpleNamespace(catalog=EventCatalog({7: seven})))
    voided = keyed(view, campaign_id=7, ordinal=2)
    assert (voided["status"], state_of(voided), voided["detail"]["mail_ids"]) == ("voided", "matching", [])
    assert keyed(view, campaign_id=7, ordinal=1)["status"] == "delivered"
    assert view["summary"]["statuses"] == {"delivered": 1, "voided": 1} and "repair" not in view


def test_pending_event_logins_of_a_changed_campaign_name_their_resolution(base):
    seven = event_campaign(7)
    state = defer_event_login(begin_event_login(base, 7, 1, event_login_definition(seven, 1), FIRST), 7, CAPACITY)

    def view(found):
        return read("events.event_login", state, event_logins=SimpleNamespace(catalog=EventCatalog(found)))

    assert "repair" not in view({7: seven})
    expected = {"command": RESOLVE_EVENT_LOGIN.operation, "targets": [{"campaign_id": 7}]}
    assert view({7: event_campaign(7, 200)})["repair"] == view({})["repair"] == expected
    assert "repair" not in read("events.event_login", state)


def comeback_band(campaign_id, pause, amount=100):
    def rewards_for(ordinal):
        return [SimpleNamespace(ordinal=ordinal, config_id=campaign_id, item_type=2, item_id="", item_param=amount,
                                param1=0, evolution_count=0, pause_period=pause)]
    return SimpleNamespace(campaign_id=campaign_id, pause_period=pause, rewards_for=rewards_for)


def comeback_catalog(bands, campaign_id=5):
    return SimpleNamespace(catalog=SimpleNamespace(campaign_id=campaign_id, bands=bands,
                                                   require_band=lambda pause: bands[pause]))


def test_comeback_rounds_schedule_every_frozen_ordinal(base):
    band = comeback_band(5, 0)
    state, _ = observe_comeback_login(base, FIRST)
    state = begin_comeback_round(state, FIRST, campaign_id=5, pause_period=0, absence_days=10, expiration=7,
                                 reward_definitions=[comeback_reward_definition(band, ordinal)
                                                     for ordinal in (1, 2, 3)])
    state = begin_comeback_reward(state, 1, FIRST)
    state, _ = complete_comeback_reward(state, 1, [31], FIRST)
    state, _ = observe_comeback_login(state, SECOND)
    state = defer_comeback_reward(begin_comeback_reward(state, 1, SECOND), 1, CAPACITY)
    view = read("events.comeback_login", state, comeback_logins=comeback_catalog({0: band}))
    rows = [keyed(view, round_id=1, campaign_id=5, ordinal=ordinal) for ordinal in (1, 2, 3)]
    assert [(row["status"], row["reason"], state_of(row)) for row in rows] == [
        ("delivered", None, "matching"), ("pending", CAPACITY, "matching"), ("scheduled", None, "matching")]
    assert rows[0]["detail"]["notify"] is True and rows[2]["detail"] == {}
    [summary] = view["rounds"]
    assert (summary["round_id"], summary["current"], summary["ordinals"], summary["delivered"]) == (1, True, 3, 1)
    assert view["observation"]["current_round_id"] == 1 and view["ledger"]["logical_day"] == NOW_DAY
    moved = read("events.comeback_login", state, comeback_logins=comeback_catalog({3: comeback_band(5, 3)}))
    assert {(state_of(row), row["definition"]["error"]["code"]) for row in moved["entries"]["items"]} == {
        ("unavailable", COMEBACK_BAND_UNAVAILABLE)}
    ended = read("events.comeback_login", state, now=LATER, comeback_logins=comeback_catalog({0: band}))
    assert [keyed(ended, round_id=1, campaign_id=5, ordinal=ordinal)["status"] for ordinal in (1, 2, 3)] == [
        "delivered", "pending", "lapsed"]
    assert ended["summary"]["statuses"] == {"delivered": 1, "pending": 1, "lapsed": 1}
    assert ended["ledger"]["logical_day"] == LATER_DAY


def test_comeback_rounds_name_the_resolution_of_a_moved_band_and_count_voided_ordinals(base):
    band, moved = comeback_band(5, 0), comeback_catalog({3: comeback_band(5, 3)})
    state, _ = observe_comeback_login(base, FIRST)
    state = begin_comeback_round(state, FIRST, campaign_id=5, pause_period=0, absence_days=10, expiration=7,
                                 reward_definitions=[comeback_reward_definition(band, ordinal)
                                                     for ordinal in (1, 2, 3)])
    state = defer_comeback_reward(begin_comeback_reward(state, 1, FIRST), 1, CAPACITY)
    assert "repair" not in read("events.comeback_login", state, comeback_logins=comeback_catalog({0: band}))
    assert read("events.comeback_login", state, comeback_logins=moved)["repair"] == {
        "command": RESOLVE_COMEBACK_LOGIN.operation, "targets": [{"round_id": 1}]}
    voided = read("events.comeback_login", void_comeback_reward(state, 1), comeback_logins=moved)
    [summary] = voided["rounds"]
    assert (summary["delivered"], summary["voided"]) == (0, 1) and "repair" not in voided
    assert keyed(voided, round_id=1, campaign_id=5, ordinal=1)["status"] == "voided"


def purchase_entry(campaign, now):
    _, purchased_at = purchased_login_clock(now)
    start, end = purchase_window(purchased_at, None, None, 7)
    return {"campaign_id": campaign.campaign_id, "identity": purchased_login_identity(campaign.campaign_id),
            "sku": "fixture_sku", "package_id": 1, "flag_id": 1, "request": HASH_A, "purchased_at": purchased_at,
            "purchase_time": purchased_login_time(purchased_at), "source_start": None, "source_end": None,
            "expiration": 7, "start_day": start, "end_day": end,
            "reward_definitions": [event_login_definition(campaign, ordinal) for ordinal in (1, 2)],
            "deliveries": {}, "pending": None}


def test_purchased_logins_report_missing_sale_campaigns(base):
    campaign = event_campaign(9)
    state, purchase_id = add_purchased_login(base, purchase_entry(campaign, FIRST), FIRST)
    state = defer_purchased_reward(begin_purchased_reward(state, purchase_id, SECOND), purchase_id, CAPACITY)

    def offering(found):
        return SimpleNamespace(catalog=SimpleNamespace(by_campaign_id=found))

    view = read("events.purchased_login", state, purchased_logins=offering({9: SimpleNamespace(campaign=campaign)}))
    first, second = (keyed(view, purchase_id=1, campaign_id=9, ordinal=ordinal) for ordinal in (1, 2))
    assert (first["status"], first["reason"], state_of(first)) == ("pending", CAPACITY, "matching")
    assert (second["status"], state_of(second)) == ("scheduled", "matching")
    [purchase] = view["purchases"]
    assert (purchase["sku"], purchase["ordinals"], purchase["delivered"]) == ("fixture_sku", 2, 0)
    assert (view["ledger"]["next_purchase_id"], view["ledger"]["logical_day"]) == (2, NOW_DAY)
    ended = read("events.purchased_login", state, now=LATER,
                 purchased_logins=offering({9: SimpleNamespace(campaign=campaign)}))
    assert [keyed(ended, purchase_id=1, campaign_id=9, ordinal=ordinal)["status"] for ordinal in (1, 2)] == [
        "pending", "lapsed"]
    gone = read("events.purchased_login", state, purchased_logins=offering({}))
    assert {row["definition"]["error"]["code"] for row in gone["entries"]["items"]} == {PURCHASED_CAMPAIGN_UNAVAILABLE}
    shortened = replace_rewards(campaign, 1)
    short = read("events.purchased_login", state, purchased_logins=offering({9: SimpleNamespace(campaign=shortened)}))
    assert state_of(keyed(short, purchase_id=1, campaign_id=9, ordinal=1)) == "matching"
    missing_reward = keyed(short, purchase_id=1, campaign_id=9, ordinal=2)
    assert missing_reward["definition"]["error"]["code"] == PURCHASED_REWARD_MISSING


def test_purchased_logins_name_the_resolution_of_a_withdrawn_campaign_and_count_voided_ordinals(base):
    campaign = event_campaign(9)
    state, purchase_id = add_purchased_login(base, purchase_entry(campaign, FIRST), FIRST)
    state = defer_purchased_reward(begin_purchased_reward(state, purchase_id, SECOND), purchase_id, CAPACITY)

    def view(saved, found):
        return read("events.purchased_login", saved,
                    purchased_logins=SimpleNamespace(catalog=SimpleNamespace(by_campaign_id=found)))

    assert "repair" not in view(state, {9: SimpleNamespace(campaign=campaign)})
    assert view(state, {})["repair"] == {"command": RESOLVE_PURCHASED_LOGIN.operation,
                                         "targets": [{"purchase_id": purchase_id}]}
    voided = view(void_purchased_reward(state, purchase_id), {})
    [purchase] = voided["purchases"]
    assert (purchase["delivered"], purchase["voided"]) == (0, 1) and "repair" not in voided


def replace_rewards(campaign, last):
    rewards_for = campaign.rewards_for
    return SimpleNamespace(campaign_id=campaign.campaign_id, login_bonus_type=campaign.login_bonus_type,
                           rewards_for=lambda ordinal: rewards_for(ordinal) if ordinal <= last else [])


def fixture_gift():
    return SimpleNamespace(gift_id=3, description="fixture gift", rewards=[SimpleNamespace(
        gift_entry_id=1, gift_type=0, item_type=2, parameter=100, item_id="", condition_type=0, condition_parameter=0)])


def test_daily_gifts_separate_pending_from_delivered(base):
    gift = fixture_gift()
    definition = daily_gift_definition(gift)
    state = begin_daily_gift(base, 3, definition, "2026-10-30", "2026-11-05", FIRST)
    state, _ = complete_daily_gift(state, 3, FIRST_DAY, [21], FIRST)
    state = defer_daily_gift(begin_daily_gift(state, 3, definition, "2026-10-30", "2026-11-05", SECOND), 3,
                             SECOND_DAY, CAPACITY)
    view = read("events.daily_gift", state, daily_gifts=SimpleNamespace(catalog=SimpleNamespace(by_id={3: gift})))
    delivered, pending = keyed(view, gift_id=3, earned_day=FIRST_DAY), keyed(view, gift_id=3, earned_day=SECOND_DAY)
    assert (delivered["status"], state_of(delivered), delivered["detail"]["mail_ids"]) == (
        "delivered", "matching", [21])
    assert (pending["status"], pending["reason"], state_of(pending)) == ("pending", CAPACITY, "matching")
    assert set(pending["detail"]) == {"window_start", "window_end", "delivery_day", "mail_ids"}
    removed = read("events.daily_gift", state, daily_gifts=SimpleNamespace(catalog=SimpleNamespace(by_id={})))
    assert {row["definition"]["error"]["code"] for row in removed["entries"]["items"]} == {
        DAILY_GIFT_SOURCE_UNAVAILABLE}


def test_daily_gifts_name_the_resolution_of_a_removed_gift_and_list_voided_days(base):
    gift = fixture_gift()
    state = begin_daily_gift(base, 3, daily_gift_definition(gift), "2026-10-30", "2026-11-05", SECOND)
    state = defer_daily_gift(state, 3, SECOND_DAY, CAPACITY)

    def view(saved, found):
        return read("events.daily_gift", saved, daily_gifts=SimpleNamespace(catalog=SimpleNamespace(by_id=found)))

    assert "repair" not in view(state, {3: gift})
    assert view(state, {})["repair"] == {"command": RESOLVE_DAILY_GIFT.operation, "targets": [{"gift_id": 3}]}
    voided = view(defer_daily_gift(state, 3, SECOND_DAY, DAILY_GIFT_VOIDED), {})
    row = keyed(voided, gift_id=3, earned_day=SECOND_DAY)
    assert (row["status"], row["reason"]) == ("voided", DAILY_GIFT_VOIDED) and "repair" not in voided
    assert voided["summary"]["statuses"] == {"voided": 1}


def test_login_rewards_check_held_stamps_and_frozen_rules(base):
    state = initialize_login_rewards(base, FIRST, new_account=True)
    state, advance = advance_login_rewards(state, SECOND)
    state = hold_stamp(state, day=advance.day, grade=0, month=advance.month, ordinal=advance.ordinal,
                       definition=HASH_A, reason="login_reward_month_unavailable")
    state = initialize_cooking(state, border=3)
    calls = Calls()
    repair = {"repair": "fixture.rebase", "refused_operations": [115], "client": "fixture client"}
    migration = {"repair": "fixture.migrate", "offline_repair": ["fixture-plan", "fixture-apply"],
                 "refused_operations": [115], "client": "fixture client"}
    capacity = {"count": 1, "capacity": 30, "remaining": 29, "warning": True}

    def rewards(total, border, definition=HASH_A):
        return SimpleNamespace(catalog=SimpleNamespace(stamp=lambda grade, month, ordinal: SimpleNamespace(
            definition=definition)), beginner_total=total, cooking_border=border,
            beginner_rules=calls.record("rules", repair), cooking_rules=calls.record("cooking", migration),
            pending_stamps=calls.record("stamps", capacity))

    view = read("events.login_rewards", state, login_rewards=rewards(7, 3))
    held = keyed(view, day=advance.day)
    assert (held["status"], held["reason"], state_of(held)) == ("pending", "login_reward_month_unavailable", "matching")
    assert held["detail"] == {"grade": 0, "month": advance.month, "ordinal": advance.ordinal}
    assert {name: check["state"] for name, check in view["frozen_rules"].items()} == {
        "beginner_total": "matching", "cooking_border": "matching"}
    assert view["progress"]["ok"] and view["cooking"]["ledger"]["border"] == 3
    assert view["ledger"]["keys"] == {LOGIN_REWARD_KEY: True, PENDING_KEY: True, COOKING_KEY: True}
    assert view["pending_stamps"] == {"ok": True, **capacity}
    assert not {"repair", "cooking_repair", "stamp_repair"} & set(view) and calls.names() == ["stamps"]
    changed = read("events.login_rewards", state, login_rewards=rewards(5, 4, HASH_B))
    assert state_of(keyed(changed, day=advance.day)) == "changed"
    assert {name: check["state"] for name, check in changed["frozen_rules"].items()} == {
        "beginner_total": "changed", "cooking_border": "changed"}
    assert changed["repair"] == {"command": "fixture.rebase", "refused_operations": [115], "client": "fixture client"}
    assert changed["cooking_repair"] == {"command": "fixture.migrate", "offline": ["fixture-plan", "fixture-apply"],
                                         "refused_operations": [115], "client": "fixture client"}
    assert changed["stamp_repair"] == {"command": RESOLVE_LOGIN_STAMPS.operation, "targets": [{"month": NOW_MONTH}]}
    assert calls.names() == ["stamps", "rules", "cooking", "stamps"]
    assert all(arguments[0] is state for _, arguments in calls)
    moved_border = read("events.login_rewards", state, login_rewards=rewards(7, 4))
    assert not {"repair", "stamp_repair"} & set(moved_border)
    assert moved_border["cooking_repair"] == changed["cooking_repair"] and calls.names()[4:] == ["cooking", "stamps"]
    unserved = read("events.login_rewards", state)
    assert not {"repair", "cooking_repair", "stamp_repair"} & set(unserved)
    assert (unserved["pending_stamps"]["ok"], unserved["pending_stamps"]["error"]["code"]) == (
        False, SERVICE_UNAVAILABLE)


def test_login_rewards_name_the_repair_of_a_changed_beginner_total(bundle):
    rewards = bundle.login_rewards
    state = bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))
    assert "repair" not in read("events.login_rewards", state, login_rewards=rewards)
    changed = deepcopy(state)
    changed[LOGIN_REWARD_KEY]["beginner_total"] = rewards.beginner_total + 1
    view = read("events.login_rewards", changed, login_rewards=rewards)
    assert view["progress"]["ok"] and view["frozen_rules"]["beginner_total"]["state"] == "changed"
    assert view["repair"] == {"command": REBASE_BEGINNER.operation, "refused_operations": [115],
                              "client": rewards.beginner_rules(changed)["client"]}


def test_login_rewards_name_the_migration_of_a_changed_cooking_border_and_the_stamp_capacity(bundle):
    rewards = bundle.login_rewards
    state = bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))
    view = read("events.login_rewards", state, login_rewards=rewards)
    assert "cooking_repair" not in view
    assert view["pending_stamps"] == {"ok": True, "count": 0, "capacity": MAX_PENDING_STAMPS,
                                      "remaining": MAX_PENDING_STAMPS, "warning": False}
    changed = deepcopy(state)
    changed[COOKING_KEY]["border"] = rewards.cooking_border + 1
    view = read("events.login_rewards", changed, login_rewards=rewards)
    assert view["cooking"]["ok"] and view["frozen_rules"]["cooking_border"]["state"] == "changed"
    assert view["cooking_repair"] == {"command": MIGRATE_COOKING.operation, "offline": list(OFFLINE_COOKING_MIGRATION),
                                      "refused_operations": [115], "client": rewards.cooking_rules(changed)["client"]}
    assert "repair" not in view


@pytest.mark.parametrize("named, command", [
    (admin_views_events.EVENT_LOGIN_REPAIR, RESOLVE_EVENT_LOGIN),
    (admin_views_events.COMEBACK_LOGIN_REPAIR, RESOLVE_COMEBACK_LOGIN),
    (admin_views_events.PURCHASED_LOGIN_REPAIR, RESOLVE_PURCHASED_LOGIN),
    (admin_views_events.DAILY_GIFT_REPAIR, RESOLVE_DAILY_GIFT),
    (admin_views_events.STAMP_REPAIR, RESOLVE_LOGIN_STAMPS),
    (admin_views_events.EVENT_PASS_REPAIR, REBIND_EVENT_PASS),
])
def test_repair_sections_name_the_registered_command_from_one_source(named, command):
    assert named is command.operation


def pass_reward(identifier, point, premium, param=100):
    return SimpleNamespace(reward_id=identifier, required_point=point, premium=premium, reward_item_type=2,
                           param=param, item_id="")


def pass_catalog(*entries):
    found = {entry.item_id: entry for entry in entries}

    def get(pass_id):
        if pass_id not in found:
            raise ProtocolError("event_pass_unknown", "Event pass is not in the fixture", 501)
        return found[pass_id]

    return SimpleNamespace(catalog=SimpleNamespace(get=get))


def test_event_passes_list_track_deliveries_and_automation_pending(base):
    normal, premium = pass_reward(1, 10, False), pass_reward(2, 20, True)
    entry = SimpleNamespace(item_id="pass_a", premium_item_id="pass_a_premium", rewards=[normal, premium])
    state = mark_event_pass(base, "pass_a", definition=pass_identity(entry),
                            normal_deliveries={10: {"definition": reward_semantics([normal]), "reward_ids": [1],
                                                    "mail_ids": [41]}},
                            premium_deliveries={20: {"definition": reward_semantics([premium]), "reward_ids": [2],
                                                     "mail_ids": [42]}})
    state[PASS_AUTOMATION] = {"schema_version": 1, "pending": {"pass_a": CAPACITY}}
    view = read("events.event_pass", state, event_passes=pass_catalog(entry))
    first, second = keyed(view, pass_id="pass_a", track="normal", point=10), keyed(view, pass_id="pass_a",
                                                                                   track="premium", point=20)
    assert (first["status"], state_of(first), first["detail"]) == ("delivered", "matching",
                                                                  {"reward_ids": [1], "mail_ids": [41]})
    assert state_of(second) == "matching"
    waiting = keyed(view, pass_id="pass_a")
    assert (waiting["status"], waiting["reason"], state_of(waiting)) == ("pending", CAPACITY, "matching")
    [summary] = view["passes"]
    assert summary["received"] == {"normal": 10, "premium": 20} and summary["baseline"] == {"normal": 0, "premium": 0}
    assert summary["identity"]["state"] == "matching" and view["automation"] == {"ok": True}
    assert summary["archived"] == []
    altered = SimpleNamespace(item_id="pass_a", premium_item_id="pass_a_premium",
                              rewards=[pass_reward(1, 10, False, 5)])
    changed = read("events.event_pass", state, event_passes=pass_catalog(altered))
    assert state_of(keyed(changed, pass_id="pass_a", track="normal", point=10)) == "changed"
    lost = keyed(changed, pass_id="pass_a", track="premium", point=20)
    assert (state_of(lost), lost["definition"]["error"]["code"]) == ("unavailable", DEFINITION_MISSING)
    state[PASS_AUTOMATION]["pending"]["pass_a"] = "bad reason!"
    assert read("events.event_pass", state, event_passes=pass_catalog(entry))["automation"]["error"]["code"] == (
        "event_pass_automation_invalid")


def test_event_pass_summaries_list_archived_identities_in_rebinding_order(base):
    normal = pass_reward(1, 10, False)
    entry = SimpleNamespace(item_id="pass_a", premium_item_id="pass_a_premium", rewards=[normal])
    state = mark_event_pass(base, "pass_a", definition=pass_identity(entry),
                            normal_deliveries={10: {"definition": reward_semantics([normal]), "reward_ids": [1],
                                                    "mail_ids": [41]}})
    rebound = rebind_definition(state, "pass_a", HASH_E)
    [summary] = read("events.event_pass", rebound, event_passes=pass_catalog(entry))["passes"]
    assert summary["archived"] == [pass_identity(entry)] and summary["identity"]["frozen"] == HASH_E
    again = rebind_definition(rebound, "pass_a", HASH_D)
    [summary] = read("events.event_pass", again, event_passes=pass_catalog(entry))["passes"]
    assert summary["archived"] == [pass_identity(entry), HASH_E]


def reissued(entry):
    return SimpleNamespace(item_id=entry.item_id, premium_item_id=f"{entry.premium_item_id}_reissued",
                           rewards=entry.rewards)


def test_event_passes_whose_identity_changed_name_their_rebind(base):
    normal = pass_reward(1, 10, False)
    first, second = (SimpleNamespace(item_id=name, premium_item_id=f"{name}_premium", rewards=[normal])
                     for name in ("pass_a", "pass_b"))
    moved_first, moved_second = map(reissued, (first, second))
    state = base
    for entry, mail in ((first, 41), (second, 42)):
        state = mark_event_pass(state, entry.item_id, definition=pass_identity(entry),
                                normal_deliveries={10: {"definition": reward_semantics([normal]), "reward_ids": [1],
                                                        "mail_ids": [mail]}})
    state[PASS_AUTOMATION] = {"schema_version": 1, "pending": {"pass_a": PASS_CHANGED}}

    def view(*entries, saved=state):
        return read("events.event_pass", saved, event_passes=pass_catalog(*entries))

    expected = {"command": REBIND_EVENT_PASS.operation, "targets": [{"pass_id": "pass_a"}]}
    assert view(moved_first, second)["repair"] == expected
    assert view(moved_first, moved_second)["repair"]["targets"] == [{"pass_id": "pass_a"}, {"pass_id": "pass_b"}]
    assert expected["command"] in discover(PREFLIGHT_DOMAINS).commands
    # The rebind answers a matching identity as unneeded and a pass outside the catalog as unknown.
    assert "repair" not in view(first, second)
    left = view(second)
    assert "repair" not in left and state_of(keyed(left, pass_id="pass_a")) == "unavailable"
    assert keyed(left, pass_id="pass_a")["reason"] == PASS_CHANGED
    assert "repair" not in read("events.event_pass", state)
    rebound = rebind_definition(state, "pass_a", pass_identity(moved_first))
    assert "repair" not in view(moved_first, second, saved=rebound)


def test_mission_bindings_compare_stored_with_prepared_bindings(base):
    stored = {"1001": {"base_version": HASH_A, "item_id": "ep_a", "point": 5},
              "1002": {"base_version": HASH_B, "item_id": "ep_b", "point": 3}}
    state = {**deepcopy(base), "mission_ep_bindings": {"schema_version": 1, "bindings": stored}}
    seen = []

    def prepare(value):
        seen.append(value)
        prepared = deepcopy(value)
        prepared["mission_ep_bindings"]["bindings"].update({
            "1001": {"base_version": HASH_A, "item_id": "ep_a", "point": 8},
            "1003": {"base_version": HASH_C, "item_id": "ep_c", "point": 1}})
        return prepared

    bindings = SimpleNamespace(prepare=prepare, changed_ids=(1001, 1003))
    view = read("events.mission_ep_bindings", state, missions=SimpleNamespace(ep_bindings=bindings))
    assert [(item["key"]["condition_id"], state_of(item)) for item in view["entries"]["items"]] == [
        (1001, "changed"), (1002, "matching"), (1003, "unfrozen")]
    assert view["projection"] == {"projected": True, "persisted": False, "note": PROJECTION_NOTE,
                                  "changed_missions": [1001, 1003], "bindings_after": 3}
    assert seen == [state] and state["mission_ep_bindings"]["bindings"] == stored
    refused = read("events.mission_ep_bindings", state,
                   missions=SimpleNamespace(ep_bindings=SimpleNamespace(
                       prepare=refusing("mission_ep_bindings_invalid"))))
    assert refused["ledger"]["error"]["code"] == "mission_ep_bindings_invalid" and refused["entries"]["total"] == 0
    assert read("events.mission_ep_bindings", state, missions=SimpleNamespace(ep_bindings=None))["ledger"]["error"][
        "code"] == SERVICE_UNAVAILABLE


def board_catalog():
    rewards = (SimpleNamespace(reward_id=1, consume_crystal=0), SimpleNamespace(reward_id=2, consume_crystal=0))
    return SimpleNamespace(active=lambda now: rewards, by_id={1: SimpleNamespace(unsupported_reason=None)})


def test_sugoroku_projects_once_on_a_private_copy(base):
    catalog = board_catalog()
    state = mark_sugoroku(sync_sugoroku(base, catalog, NOW_JST), [1], claimed=True, deliveries={1: [51]})
    calls = Calls()

    def prepare_month(value, now):
        calls.append(("prepare_month", (value is state, now)))
        return deepcopy(value)

    board = SimpleNamespace(catalog=catalog, prepare_month=prepare_month)
    view = read("events.sugoroku", state, sugoroku=board)
    assert calls == [("prepare_month", (False, NOW_JST))]
    claimed, reached = keyed(view, period=NOW_MONTH, reward_id=1), keyed(view, period=NOW_MONTH, reward_id=2)
    assert (claimed["status"], claimed["detail"]) == ("delivered", {"board_status": 2, "seen": True, "mail_ids": [51],
                                                                    "reward": {"present": True,
                                                                               "unsupported_reason": None}})
    assert (reached["status"], reached["reason"], reached["detail"]["seen"]) == ("unclaimed", None, False)
    assert reached["detail"]["reward"] == {"present": False, "unsupported_reason": None}
    projection = view["projection"]
    assert (projection["ok"], projection["persisted"], projection["period"]) == (True, False, NOW_MONTH)
    assert projection["rows"] == [{"ID": 1, "Status": 2}, {"ID": 2, "Status": 0}]
    assert (view["ledger"]["baselines"], view["ledger"]["current_period"]) == ({NOW_MONTH: 0}, NOW_MONTH)
    month_later = read("events.sugoroku", state, now=LATER, sugoroku=board)
    assert [(item["key"]["reward_id"], item["status"]) for item in month_later["entries"]["items"]] == [
        (1, "delivered"), (2, "lapsed")]
    assert month_later["ledger"]["current_period"] == LATER_MONTH
    for moment, period, waiting in ((MONTH_END, NOW_MONTH, "unclaimed"), (MONTH_START, LATER_MONTH, "lapsed")):
        boundary = read("events.sugoroku", state, now=moment, sugoroku=board)
        assert boundary["ledger"]["current_period"] == period == business_month(moment)
        assert keyed(boundary, period=NOW_MONTH, reward_id=2)["status"] == waiting
    fresh = {key: value for key, value in state.items() if key != "sugoroku"}
    absent = read("events.sugoroku", fresh, sugoroku=board)
    assert absent["ledger"] == {"keys": {"sugoroku": False}, "ok": True} and absent["projection"]["ok"]
    broken = {**fresh, "sugoroku": {"schema_version": 1}}
    assert read("events.sugoroku", broken, sugoroku=board)["ledger"]["error"]["code"] == "sugoroku_state_invalid"
    missing = read("events.sugoroku", initialize_sugoroku(base))
    assert missing["catalog"]["error"]["code"] == SERVICE_UNAVAILABLE
    assert missing["projection"]["error"]["code"] == SERVICE_UNAVAILABLE


def package_service(identities, offers):
    def purchases_read(state):
        entries = state.get("package_purchases", {}).get("entries", {})
        for name, record in entries.items():
            if name in identities and record["identity"] != identities[name]:
                raise ProtocolError("package_purchase_identity_changed", "Saved package entitlement identity changed",
                                    503)
        return deepcopy(entries)

    return SimpleNamespace(purchases=SimpleNamespace(read=purchases_read),
                           catalog=SimpleNamespace(by_normal_id=offers))


def test_packages_diagnose_each_refused_member(base):
    entries_ = {"pass_a": {"identity": HASH_A, "count": 1, "source": "legacy_owned", "request": None},
                "pass_b": {"identity": HASH_B, "count": 1, "source": "local_free", "request": HASH_C},
                "pass_c": {"identity": HASH_C, "count": 1, "source": "local_free", "request": HASH_D}}
    state = {**deepcopy(base), "package_purchases": {"schema_version": 1, "entries": entries_}}
    offers = {"pass_a": SimpleNamespace(definition_hash=HASH_A), "pass_b": SimpleNamespace(definition_hash=HASH_B)}
    view = read("events.packages", state, packages=package_service({"pass_a": HASH_A, "pass_b": HASH_B}, offers))
    assert [(item["key"]["normal_pass_id"], item["status"], state_of(item)) for item in view["entries"]["items"]] == [
        ("pass_a", "baseline", "matching"), ("pass_b", "delivered", "matching"), ("pass_c", "delivered", "unavailable")]
    assert keyed(view, normal_pass_id="pass_c")["definition"]["error"]["code"] == PACKAGE_OFFER_UNKNOWN
    assert keyed(view, normal_pass_id="pass_b")["detail"] == {"count": 1, "source": "local_free", "request": HASH_C}
    assert "diagnosis" not in view
    changed = package_service({"pass_a": HASH_E, "pass_b": HASH_B, "pass_c": HASH_E}, offers)
    refused = read("events.packages", state, packages=changed, params=PageParams(limit=1))
    assert refused["ledger"]["error"]["code"] == "package_purchase_identity_changed"
    assert refused["entries"]["total"] == 0
    diagnosis = refused["diagnosis"]
    assert (diagnosis["total"], diagnosis["next_offset"]) == (2, 1)
    assert diagnosis["items"] == [{"key": "pass_a", "error": {"code": "package_purchase_identity_changed",
                                                              "message": "Saved package entitlement identity changed"}}]
    later = read("events.packages", state, packages=changed, params=PageParams(offset=1, limit=1))
    assert [item["key"] for item in later["diagnosis"]["items"]] == ["pass_c"]
    assert "diagnosis" not in read("events.packages", state)


class TradeCatalog:
    def __init__(self, products):
        self.products = products

    def product(self, shop_id):
        if shop_id not in self.products:
            raise ProtocolError("trade_product_unknown", "Trade product is absent from the fixture", 404)
        return self.products[shop_id]


def trade_product(shop_id, reset_type, data_restrict):
    return SimpleNamespace(shop_id=shop_id, reset_type=reset_type, start_at=None, end_at=None,
                           data_restrict=data_restrict)


def test_trades_compare_reset_periods_with_served_counts(base):
    products = {"1": {"period": "month:2026-10", "count": 3, "lifetime_count": 5},
                "2": {"period": "never", "count": 2, "lifetime_count": 2},
                "3": {"period": "never", "count": 1, "lifetime_count": 1}}
    state = deepcopy(base)
    state["trade_history"] = {"schema_version": 1, "products": products, "last_server_time": 1}
    state["tables"]["exchange_history"] = [{"ShopItemID": int(key), "ExchangeCount": row["count"]}
                                           for key, row in products.items()]
    catalog = TradeCatalog({1: trade_product(1, 1, 5), 3: trade_product(3, 0, 5)})
    history = TradeHistory(catalog, TradeRules(reset_type_1="month", reset_type_2="window"))
    view = read("events.trades", state, trades=SimpleNamespace(history=history))
    monthly, removed, lasting = (keyed(view, shop_item_id=shop) for shop in (1, 2, 3))
    assert monthly["definition"] is None and view["summary"]["definitions"] == {}
    assert monthly["detail"] == {"count": 3, "lifetime_count": 5, "served_count": 0, "period": {
        "state": "changed", "frozen": "month:2026-10", "current": "month:2026-11", "error": None}}
    assert removed["detail"]["period"]["error"]["code"] == "trade_product_unknown"
    assert (removed["detail"]["period"]["state"], removed["detail"]["served_count"]) == ("unavailable", 2)
    assert (lasting["detail"]["period"]["state"], lasting["detail"]["served_count"]) == ("matching", 1)
    assert view["ledger"]["journal"] is True and view["ledger"]["served"] == {"ok": True}
    history.response = refusing("trade_history_invalid")
    refused = read("events.trades", state, trades=SimpleNamespace(history=history))
    assert refused["ledger"]["served"]["error"]["code"] == "trade_history_invalid"
    assert keyed(refused, shop_item_id=1)["detail"]["served_count"] is None


def tower_product(shop_id):
    return SimpleNamespace(shop_id=shop_id, card_id=0, item_id=f"tower_item_{shop_id}", currency_item_id="tower_coin",
                           item_effect_type=0)


def tower_history(*products):
    found = {product.shop_id: product for product in products}

    def product(shop_id):
        if shop_id not in found:
            raise ProtocolError("tower_trade_product_unknown", "Tower product is absent from the fixture", 404)
        return found[shop_id]

    return TowerTradeHistory(SimpleNamespace(content_version=HASH_D, _ready=lambda: None, by_id=found, product=product))


def test_tower_trades_diagnose_reused_product_identities(base):
    first, second = tower_product(1), tower_product(2)
    records = {"1": {"count": 2, "source": TowerTradeHistory.source(first)},
               "2": {"count": 1, "source": HASH_B},
               "3": {"count": 4, "source": HASH_A}}
    state = {**deepcopy(base), "tower_trade_history": {"schema_version": 1, "records": records}}
    history = tower_history(first, second)
    refused = read("events.tower_trades", state, tower_trades=SimpleNamespace(history=history))
    assert refused["ledger"]["error"]["code"] == "tower_trade_product_reused"
    assert [item["key"] for item in refused["diagnosis"]["items"]] == ["2"]
    records["2"]["source"] = TowerTradeHistory.source(second)
    view = read("events.tower_trades", state, tower_trades=SimpleNamespace(history=history))
    # Counts saved before the monthly reset read as lifetime totals, so this month starts from zero.
    assert [(item["key"]["shop_id"], state_of(item), item["detail"]) for item in view["entries"]["items"]] == [
        (1, "matching", {"count": 0, "period": None, "lifetime": 2}),
        (2, "matching", {"count": 0, "period": None, "lifetime": 1}),
        (3, "unavailable", {"count": 0, "period": None, "lifetime": 4})]
    assert keyed(view, shop_id=3)["definition"]["error"]["code"] == "tower_trade_product_unknown"
    assert (view["ledger"]["current_period"], view["ledger"]["clock"]) == (NOW_MONTH, {"ok": True})
    assert "diagnosis" not in view
    unread = read("events.tower_trades", state)
    assert unread["ledger"]["error"]["code"] == SERVICE_UNAVAILABLE and "diagnosis" not in unread


def test_tower_trades_show_this_months_count_beside_the_saved_month_and_lifetime(base):
    first, second = tower_product(1), tower_product(2)
    records = {"1": {"source": TowerTradeHistory.source(first), "period": NOW_MONTH, "count": 2, "lifetime": 5},
               "2": {"source": TowerTradeHistory.source(second), "period": EARLIER_MONTH, "count": 1, "lifetime": 1}}
    state = {**deepcopy(base), "tower_trade_history": {"schema_version": 2, "records": records}}
    trades = SimpleNamespace(history=tower_history(first, second))
    # A clock set back to an earlier month keeps the later saved month's count.
    for moment, period, current in ((EARLIER, EARLIER_MONTH, (2, 1)), (NOW_JST, NOW_MONTH, (2, 0)),
                                    (MONTH_END, NOW_MONTH, (2, 0)), (MONTH_START, LATER_MONTH, (0, 0))):
        view = read("events.tower_trades", state, now=moment, tower_trades=trades)
        assert (view["ledger"]["current_period"], view["ledger"]["clock"]) == (period, {"ok": True})
        assert [(item["key"]["shop_id"], item["detail"]) for item in view["entries"]["items"]] == [
            (1, {"count": current[0], "period": NOW_MONTH, "lifetime": 5}),
            (2, {"count": current[1], "period": EARLIER_MONTH, "lifetime": 1})]
    stopped = read("events.tower_trades", state, now=BEFORE_CALENDAR, tower_trades=trades)
    assert stopped["ledger"]["current_period"] is None
    assert stopped["ledger"]["clock"]["error"]["code"] == "tower_trade_clock_invalid"
    assert [item["detail"]["count"] for item in stopped["entries"]["items"]] == [None, None]
    assert VIEWS["events.tower_trades"].live is True


def vote_state(base):
    state = deepcopy(base)
    state["tables"]["vote_history"] = [{"key": vote_key(VOTE_ITEM, 101), "cardId": 101, "groupId": 1, "count": 2,
                                        "itemId": VOTE_ITEM}]
    state = initialize_vote_state(state)
    state = record_vote(state, item_id=VOTE_ITEM, group_id=1, card_id=102, count=3, definition=HASH_A)
    return record_vote(state, item_id=LOST_ITEM, group_id=1, card_id=103, count=1, definition=HASH_B)


def assault_entry(earned, spent, status, reason, settled_at, mail_ids):
    return {"definition": HASH_A, "source_window_end": FIRST_DAY, "window_end": SECOND_DAY,
            "observed_at": "2026-11-01T04:00:00+09:00", "earned": earned, "spent": spent, "amount": earned - spent,
            "rate": ASSAULT_GOLD_PER_RIGHT, "status": status, "reason": reason, "settled_at": settled_at,
            "mail_ids": mail_ids}


ASSAULT_LEDGER = {"schema_version": 1, "events": {
    "16": assault_entry(5, 1, "settled", None, "2026-11-01T05:00:00+09:00", [63]),
    "9": assault_entry(2, 0, "pending", CAPACITY, None, [])}}


def vote_services(calls):
    group = SimpleNamespace(group_id=1, item_id=VOTE_ITEM, final=False)
    final = SimpleNamespace(group_id=4, item_id=FINAL_ITEM, final=True)
    catalog = SimpleNamespace(groups=(group, final))
    delivery = {"definition": HASH_C, "mail_ids": [61], "award_row": {"GroupID": 1, "Count": 3}}
    rewards_ledger = {"schema_version": 1, "assault": {"16": {"earned": 1}}, "items": {VOTE_ITEM: {
        "group_id": 1, "event_id": 16, "definition": HASH_A, "baseline_count": 2, "count": 5,
        "deliveries": {"3": delivery}}}}
    snapshot = {"event_id": 16, "group_id": 1, "final": False, "definition": HASH_D, "source_sha256": HASH_E,
                "window_end": "2026-08-30T16:00:00+09:00", "frozen_at": "2026-08-30T16:00:01+09:00",
                "origin": "first_observation_baseline", "counts": [{"CardID": 102, "Count": 3},
                                                                  {"CardID": 101, "Count": 2}],
                "qualified_ids": [102]}
    expiry_items = {
        VOTE_ITEM: {"group_id": 1, "definition": HASH_A, "status": "pending", "reason": "awaiting_delivery",
                    "amount": 4, "mail_ids": []},
        FINAL_ITEM: {"group_id": 4, "definition": HASH_B, "status": "settled", "reason": None, "amount": 2,
                     "mail_ids": [62]}}
    cooking_groups = {VOTE_ITEM: {"group_id": 1, "status": "pending", "reason": "vote_cooking_recipe_changed",
                                  "source": HASH_B, "definition": HASH_C, "winner": 102}}
    receipt = {"request": HASH_A, "content": HASH_B, "state": HASH_C, "revision": 3, "group_id": 1,
               "response": "<?xml version=\"1.0\"?><DA/>", "method_key": SECRET}
    transactions = SimpleNamespace(
        rewards=SimpleNamespace(catalog=catalog, group_definition=lambda value: HASH_A if value is group else HASH_E,
                                validated=calls.record("rewards", lambda state: ({VOTE_ITEM: {"group_id": 1,
                                                                                              "count": 5}},
                                                                                 rewards_ledger))),
        tickets=SimpleNamespace(ticket_status=calls.record("tickets", lambda state: {
            "item_id": "item_10000000", "count": 1, "ticket_day": "2026-11-02", "status": "available",
            "reason": None})),
        finalists=SimpleNamespace(catalog=catalog, content_version=HASH_D,
                                  validated=calls.record("finalists", lambda state: {VOTE_ITEM: snapshot}),
                                  ranking=calls.record("ranking", lambda state, value: ({"CardID": 102, "Count": 1},))),
        expiry=SimpleNamespace(ledger=calls.record("expiry", lambda state: expiry_items)),
        assault_expiry=SimpleNamespace(ledger=calls.record(
            "assault", lambda state: state.get(ASSAULT_KEY, {}).get("events", {}))),
        history=calls.record("receipts", lambda state, revision: {"schema_version": 2, "receipts": [receipt],
                                                                   "processed_keys": [HASH_A, HASH_B]}))
    return {"votes": SimpleNamespace(read=Tripwire("VoteService.read"), transactions=transactions,
                                     content_version=HASH_E),
            "vote_cooking": SimpleNamespace(ledger=calls.record("cooking", lambda state: cooking_groups),
                                            recipes=SimpleNamespace(content_version=HASH_C)),
            "vote_cooking_notifier": SimpleNamespace(ledger=calls.record("cooking_notices", lambda state: {}))}


def test_vote_ledgers_are_read_through_their_own_readers(base):
    state, calls = vote_state(base), Calls()
    before = deepcopy(state)
    services = vote_services(calls)
    history = read("votes.history", state, **services)
    baseline, bound, lost = (keyed(history, item_id=item, card_id=card)
                             for item, card in ((VOTE_ITEM, 101), (VOTE_ITEM, 102), (LOST_ITEM, 103)))
    assert (baseline["status"], state_of(baseline), baseline["detail"]) == (
        "baseline", "unfrozen", {"group_id": 1, "count": 2, "baseline_count": 2})
    assert (bound["status"], state_of(bound), bound["detail"]["baseline_count"]) == ("bound", "matching", 0)
    assert (state_of(lost), lost["definition"]["error"]["code"]) == ("unavailable", VOTE_GROUP_MISSING)
    assert history["ledger"]["journal"] is True
    rewards = read("votes.rewards", state, **services)
    [delivery] = rewards["entries"]["items"]
    assert (delivery["key"], delivery["status"], delivery["detail"]["mail_ids"]) == (
        {"item_id": VOTE_ITEM, "point": 3}, "delivered", [61])
    [item] = rewards["items"]
    assert (item["deliveries"], item["definition"]["state"], item["count"]) == (1, "matching", 5)
    assert rewards["assault"] == {"16": {"earned": 1}} and rewards["ledger"]["stored"] is True
    tickets = read("votes.tickets", state, **services)
    [daily] = tickets["entries"]["items"]
    assert (daily["status"], daily["detail"]) == ("available", {"count": 1, "ticket_day": "2026-11-02"})
    rankings = read("votes.rankings", state, **services)
    assert [(item["key"]["group_id"], item["key"]["card_id"], item["status"], item["detail"]["count"])
            for item in rankings["entries"]["items"]] == [(1, 102, FROZEN, 3), (1, 101, FROZEN, 2), (4, 102, LIVE, 1)]
    assert keyed(rankings, group_id=1, card_id=102)["detail"]["qualified"] is True
    assert rankings["frozen"][0]["definition"]["state"] == "matching" and "counts" not in rankings["frozen"][0]
    assert rankings["live"] == [{"group_id": 4, "item_id": FINAL_ITEM, "final": True, "ok": True}]
    expiry = read("votes.expiry", state, **services)
    assert [(item["key"]["item_id"], item["status"], item["reason"]) for item in expiry["entries"]["items"]] == [
        (VOTE_ITEM, "pending", "awaiting_delivery"), (FINAL_ITEM, "settled", None)]
    assert expiry["notifications"]["error"]["code"] == SERVICE_UNAVAILABLE
    proof = {FINAL_ITEM: {"gold": 100, "mail_ids": [62], "notified_at": 1}}
    notifier = SimpleNamespace(records=calls.record("expiry_notices", lambda state: proof))
    notified = read("votes.expiry", state, rank_rewards=SimpleNamespace(vote_expiry_notifier=notifier), **services)
    assert notified["notifications"] == {"ok": True, "records": proof}
    assert read("votes.assault", state, **services)["entries"]["total"] == 0
    converted = {**state, ASSAULT_KEY: ASSAULT_LEDGER}
    converted_before = deepcopy(converted)
    assault = read("votes.assault", converted, **services)
    assert dict(calls)["assault"][0] is converted and converted == converted_before
    assert [(item["key"], item["status"], item["reason"], item["detail"]["gold"])
            for item in assault["entries"]["items"]] == [
        ({"event_id": 9}, "pending", CAPACITY, 2 * ASSAULT_GOLD_PER_RIGHT),
        ({"event_id": 16}, "settled", None, 4 * ASSAULT_GOLD_PER_RIGHT)]
    assert keyed(assault, event_id=16)["detail"]["mail_ids"] == [63] and assault["ledger"]["ok"] is True
    assert assault["ledger"]["keys"] == {ASSAULT_KEY: True, ASSAULT_NOTIFICATION_KEY: False}
    assert assault["notifications"]["error"]["code"] == SERVICE_UNAVAILABLE
    announcements = {"16": {"mission_id": 576, "gold": 4 * ASSAULT_GOLD_PER_RIGHT, "mail_ids": [63], "notified_at": 1}}
    notices = SimpleNamespace(records=calls.record("assault_notices", lambda state: announcements))
    announced = read("votes.assault", converted, rank_rewards=SimpleNamespace(vote_assault_notifier=notices),
                     **services)
    assert announced["notifications"] == {"ok": True, "records": announcements} and converted == converted_before
    cooking = read("votes.cooking", state, **services)
    [group] = cooking["entries"]["items"]
    assert (group["status"], group["reason"], state_of(group)) == ("pending", "vote_cooking_recipe_changed", "changed")
    assert group["detail"]["winner"] == 102 and cooking["notifications"] == {"ok": True, "records": {}}
    receipts = read("votes.receipts", state, revision=9, **services)
    [recorded] = receipts["entries"]["items"]
    assert (recorded["status"], recorded["detail"]["method_key"]) == (RECORDED, REDACTED)
    assert recorded["detail"]["response"]["redacted"] == "response" and receipts["ledger"]["processed_keys"] == 2
    assert dict(calls)["receipts"] == (state, 9)
    assert sorted(set(calls.names())) == ["assault", "assault_notices", "cooking", "cooking_notices", "expiry",
                                          "expiry_notices", "finalists", "ranking", "receipts", "rewards",
                                          "tickets"]
    assert state == before


def test_cooking_notices_read_the_cooking_ledger_without_initializing_it(base):
    state, calls = deepcopy(base), Calls()
    before = deepcopy(state)
    cooking = SimpleNamespace(presents=SimpleNamespace(), content_version=HASH_A,
                              recipes=SimpleNamespace(content_version=HASH_C), _ready=lambda: None,
                              ledger=calls.record("cooking", lambda value: {}),
                              initialize=lambda value: pytest.fail("Reading cooking notices must not copy the save"))
    view = read("votes.cooking", state, vote_cooking=cooking, vote_cooking_notifier=VoteCookingNotifications(cooking))
    assert view["notifications"] == {"ok": True, "records": {}} and view["entries"]["total"] == 0
    assert [arguments[0] is state for _, arguments in calls] == [True, True] and state == before


def test_vote_views_report_unreadable_or_missing_services(base):
    state, calls = vote_state(base), Calls()
    services = vote_services(calls)
    transactions = services["votes"].transactions
    transactions.rewards.validated = refusing("vote_rewards_state_invalid")
    transactions.finalists.ranking = refusing("vote_finalists_definition_changed")
    transactions.finalists.validated = lambda value: {}
    assert read("votes.rewards", state, **services)["ledger"]["error"]["code"] == "vote_rewards_state_invalid"
    rankings = read("votes.rankings", state, **services)
    assert rankings["entries"]["total"] == 0
    assert {group["error"]["code"] for group in rankings["live"]} == {"vote_finalists_definition_changed"}
    transactions.assault_expiry.ledger = refusing("vote_assault_expiry_rights_changed")
    refused = read("votes.assault", {**state, ASSAULT_KEY: ASSAULT_LEDGER}, **services)
    assert refused["ledger"]["error"]["code"] == "vote_assault_expiry_rights_changed"
    assert refused["entries"]["total"] == 0
    services["votes"].transactions = None
    for name in VOTE_NAMES:
        if name not in ("votes.history", "votes.cooking"):
            assert read(name, state, **services)["ledger"]["error"]["code"] == SERVICE_UNAVAILABLE
    unchecked = read("votes.history", state, **services)
    assert unchecked["ledger"]["ok"] is True
    assert {item["definition"]["error"]["code"] for item in unchecked["entries"]["items"]} == {
        SERVICE_UNAVAILABLE}
    corrupt = deepcopy(state)
    corrupt["vote_state"]["entries"].popitem()
    assert read("votes.history", corrupt, **services)["ledger"]["error"]["code"] == "vote_state_invalid"
    assert read("votes.cooking", state)["ledger"]["error"]["code"] == SERVICE_UNAVAILABLE
    services["vote_cooking_notifier"] = None
    assert read("votes.cooking", state, **services)["notifications"]["error"]["code"] == (
        SERVICE_UNAVAILABLE)


def test_state_readers_hand_the_save_itself_to_the_view_services(base):
    state, calls = vote_state(base), Calls()
    before = deepcopy(state)
    notifiers = SimpleNamespace(vote_expiry_notifier=SimpleNamespace(records=calls.record("expiry_notices", {})),
                                vote_assault_notifier=SimpleNamespace(records=calls.record("assault_notices", {})))
    tower_catalog = SimpleNamespace(towers=(), by_mission=SimpleNamespace(get=calls.record("towers", None)))
    bundle = SimpleNamespace(
        **vote_services(calls), rank_rewards=notifiers,
        trades=SimpleNamespace(history=SimpleNamespace(records=calls.record("trades", {})), clock=lambda: NOW_JST),
        tower_trades=SimpleNamespace(history=SimpleNamespace(records=calls.record("tower_trades", {}))),
        packages=SimpleNamespace(purchases=SimpleNamespace(read=calls.record("packages", {}))),
        towers=SimpleNamespace(catalog=tower_catalog))
    keys = ("vote_rewards", "vote_tickets", "vote_finalists", "vote_expiry", NOTIFICATION_KEY, ASSAULT_KEY,
            ASSAULT_NOTIFICATION_KEY, "vote_cooking", "vote_cooking_notifications", "trade_history",
            "tower_trade_history", "package_purchases", TOWER_STATE)
    assert [(entry["key"], entry["valid"]) for entry in ledger_status(state, bundle, keys=keys)] == [
        (key, True) for key in keys]
    assert sorted(calls.names()) == ["assault", "assault_notices", "cooking", "cooking_notices", "expiry",
                                     "expiry_notices", "finalists", "packages", "rewards", "tickets", "tower_trades",
                                     "trades"]
    assert all(arguments[0] is state for _, arguments in calls) and dict(calls)["trades"] == (state, NOW_JST)
    unconfigured = {"schema_version": TOWER_SCHEMA, "consume": {}, "towers": {"1": {}}, "receipts": {}, "charge": None}
    [tower] = ledger_status({**state, TOWER_STATE: unconfigured}, bundle, keys=(TOWER_STATE,))
    assert tower["error"]["code"] == "tower_state_invalid" and calls[-1] == ("towers", (1,))
    assert state == before


def rules_console(tmp_path, bootstrap, players, *, config_set=DEFAULT_DIGEST, base_version=RESOURCE_VERSION):
    bundle = stub_bundle(bootstrap, players)
    identity = GenerationIdentity(code_digest="d" * 64, config_set=config_set,
                                  content_versions={"bootstrap": "b1", "trades": "t1"}, client_fingerprint=CLIENT,
                                  master_snapshot=SNAPSHOT, base_resource_version=base_version,
                                  effective_resource_version=RESOURCE_VERSION)
    generation = Generation(identity, bundle, bootstrap.handlers(), {"bootstrap": bootstrap.status()})
    return console_for(tmp_path, bundle, account_state(bootstrap, players), generation)


def test_rules_view_describes_every_registered_key_with_effective_defaults(tmp_path, bootstrap, players):
    view = rules_console(tmp_path, bootstrap, players).read("rules.config")
    assert view["running"] == {"digest": DEFAULT_DIGEST, "set": EMPTY_SET, "known": True, "reason": None, "error": None}
    assert [entry["key"] for entry in view["keys"]] == list(REGISTRY)
    assert ("letstd-rewards" in [entry["key"] for entry in view["keys"]]) == ("letstd-rewards" in REGISTRY)
    for entry in view["keys"]:
        spec = REGISTRY[entry["key"]]
        assert entry["source"] == "default" and entry["mode"] == "default" and entry["error"] is None
        assert entry["effective"] == entry["default"] == spec.normalize(spec.default())
        assert {name: entry[name] for name in spec.describe()} == json.loads(json.dumps(spec.describe()))
    assert "bootstrap_token" not in view["keys"][0]["effective"]


def test_rules_view_resolves_the_running_stored_set(tmp_path, bootstrap, players):
    data = tmp_path / "data"
    data.mkdir()
    stored = ConfigSets(data).create({"trade-rules": {"reset_type_1": "never"}}, source="console")
    console = rules_console(tmp_path, bootstrap, players, config_set=config_identity(stored.explicit))
    view = console.read("rules.config")
    assert view["running"] == {"digest": config_identity(stored.explicit), "set": stored.identifier, "known": True,
                               "reason": None, "error": None}
    trades = next(entry for entry in view["keys"] if entry["key"] == "trade-rules")
    assert (trades["source"], trades["mode"]) == ("set", "explicit")
    assert trades["effective"] == {"reset_type_1": "never", "reset_type_2": "window"}
    assert trades["default"] == {"reset_type_1": "month", "reset_type_2": "window"}
    assert {entry["source"] for entry in view["keys"] if entry["key"] != "trade-rules"} == {"default"}


def test_rules_view_marks_unknown_running_digests(tmp_path, bootstrap, players, monkeypatch):
    view = rules_console(tmp_path, bootstrap, players, config_set=UNKNOWN_DIGEST).read("rules.config")
    assert view["running"] == {"digest": UNKNOWN_DIGEST, "set": None, "known": False, "reason": RUNNING_UNKNOWN,
                               "error": None}
    assert [entry["key"] for entry in view["keys"]] == list(REGISTRY)
    assert all("effective" not in entry and entry["default"] == REGISTRY[entry["key"]].normalize(
        REGISTRY[entry["key"]].default()) for entry in view["keys"])

    def unusable(*arguments, **options):
        raise ConfigError("config_path_invalid", "The fixture data directory is unusable", 503)

    monkeypatch.setattr(admin_views_rules, "ConfigSets", unusable)
    failed = rules_console(tmp_path / "again", bootstrap, players).read("rules.config")
    assert failed["running"]["error"]["code"] == "config_path_invalid" and failed["running"]["known"] is False


def test_rules_view_follows_registry_additions(tmp_path, bootstrap, players, monkeypatch):
    added = replace(REGISTRY["trade-rules"], key=FIXTURE_KEY, cli_flag=f"--{FIXTURE_KEY}",
                    ps_parameter="FixtureRulesFile")
    monkeypatch.setattr(admin_views_rules, "REGISTRY", ConfigRegistry((*REGISTRY.values(), added)))
    known = rules_console(tmp_path, bootstrap, players).read("rules.config")
    assert [entry["key"] for entry in known["keys"]] == [*REGISTRY, FIXTURE_KEY]
    assert known["keys"][-1]["effective"] == {"reset_type_1": "month", "reset_type_2": "window"}
    unknown = rules_console(tmp_path / "unknown", bootstrap, players, config_set=UNKNOWN_DIGEST).read("rules.config")
    assert [entry["key"] for entry in unknown["keys"]] == [*REGISTRY, FIXTURE_KEY]


def test_rules_view_compares_master_resource_and_manifest_versions(tmp_path, bootstrap, players):
    view = rules_console(tmp_path, bootstrap, players, base_version="b" * 40).read("rules.config")
    versions = view["versions"]
    assert versions["resources"] == {"base": "b" * 40, "effective": RESOURCE_VERSION, "served": RESOURCE_VERSION,
                                     "overlaid": True, "matches": True}
    assert versions["masterdata"]["in_use"] == SNAPSHOT and versions["masterdata"]["matches"] is True
    assert versions["client_manifest"]["present"] is False
    assert versions["content"] == {"bootstrap": "b1", "trades": "t1"}
    plain = rules_console(tmp_path / "plain", bootstrap, players).read("rules.config")["versions"]["resources"]
    assert plain["overlaid"] is False


@pytest.fixture
def live(bundle, tmp_path, monkeypatch):
    for name in WRITE_ON_READ_ATTRIBUTES:
        monkeypatch.setattr(bundle.bootstrap, name, Tripwire(f"bootstrap.{name}"))
    for name, label in (("votes", "VoteService.read"), ("sugoroku", "SugorokuService.read")):
        if getattr(bundle, name) is not None:
            monkeypatch.setattr(getattr(bundle, name), "read", Tripwire(label))
    clocked = copy(bundle.bootstrap)
    clocked.clock = lambda: NOW_JST
    state = bundle.initialize(bundle.player_state.initialize(clocked.initial_state()))
    votes = bundle.votes
    if votes is not None and votes.transactions is not None:
        group = votes.catalog.groups[0]
        candidate = next(entry for entry in votes.catalog.candidates if entry.group_id == group.group_id)
        state = record_vote(state, item_id=group.item_id, group_id=group.group_id, card_id=candidate.card_id, count=3,
                            definition=votes.transactions.rewards.group_definition(group))
    services = replace(bundle, bootstrap=clocked)
    generation = generation_for(services, {"bootstrap": clocked.status()})
    return SimpleNamespace(console=console_for(tmp_path, services, state, generation), bundle=bundle)


def test_live_views_read_the_real_services_without_writing(live):
    console = live.console
    before = console.store.profile()
    for name in ALL_NAMES:
        response = console.get(name)
        assert response.status_code == 200, (name, response.text)
    events = {name: console.read(name) for name in EVENT_NAMES}
    for name, view in events.items():
        assert view["ledger"]["ok"] is True and view["service"]["available"] is True, (name, view["ledger"])
    projection = events["events.sugoroku"]["projection"]
    assert (projection["ok"], projection["projected"], projection["persisted"]) == (True, True, False)
    assert events["events.mission_ep_bindings"]["projection"]["persisted"] is False
    if live.bundle.votes is not None and live.bundle.votes.transactions is not None:
        votes = {name: console.read(name, {"limit": MAX_PAGE_ITEMS}) for name in VOTE_NAMES}
        assert all(view["ledger"]["ok"] for view in votes.values()), {
            name: view["ledger"] for name, view in votes.items()}
        [vote] = votes["votes.history"]["entries"]["items"]
        assert (vote["status"], state_of(vote), vote["detail"]["count"]) == ("bound", "matching", 3)
        live_rows = [item for item in votes["votes.rankings"]["entries"]["items"] if item["key"]["card_id"] == vote[
            "key"]["card_id"]]
        assert [(item["status"], item["detail"]["count"]) for item in live_rows] == [(LIVE, 3)]
    rules = console.read("rules.config")
    assert rules["running"]["known"] is True and [entry["key"] for entry in rules["keys"]] == list(REGISTRY)
    assert all(entry["effective"] == entry["default"] for entry in rules["keys"])
    after = console.store.profile()
    assert (after.revision, json_digest(after.state)) == (before.revision, json_digest(before.state))
    assert console.runtime.status()["current"]["leases"] == 0

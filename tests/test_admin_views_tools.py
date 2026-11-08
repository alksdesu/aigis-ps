from copy import copy, deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aigis_ps import admin_routes_state, admin_views_tools
from aigis_ps.admin_app import create_admin_app
from aigis_ps.admin_command import NoopPreflight, NoopRestartCheck, PlanStore, validate_params_model
from aigis_ps.admin_diff import REDACTED, json_digest
from aigis_ps.admin_fields import anchors
from aigis_ps.admin_registry import AdminCatalog, CommandRegistry, RouteRegistry, ViewRegistry
from aigis_ps.admin_views import MAX_PAGE_ITEMS, PROJECTION_NOTE
from aigis_ps.admin_views_tools import (EVOLUTION_REGISTRATIONS, SINGLE_PURCHASE, VIEWS, EvolutionOperation,
                                        EvolutionParams, PresentPreviewParams, UnitSaleParams)
from aigis_ps.alxml import Column, Container
from aigis_ps.background_catalog import BackgroundDefinition
from aigis_ps.emblem_catalog import EmblemTitle
from aigis_ps.energy_clock import JST, PREFIXES, energy_seconds, set_anchor
from aigis_ps.errors import ProtocolError
from aigis_ps.evolution_catalog import EvolutionQuote
from aigis_ps.evolution_materials import MaterialCondition
from aigis_ps.evolution_protocol import MAX_EVOLUTION_MATERIALS
from aigis_ps.generation import GenerationRuntime
from aigis_ps.item_summon_catalog import ItemSummonDefinition, LotteryRule
from aigis_ps.orb_exchange import OrbDefinition, OrbExchangeQuote
from aigis_ps.player_state import BARRACKS_SHIFT, LOCK_FLAG, UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.storage import Store
from aigis_ps.summon_pool_catalog import SummonCandidate, SummonPool
from aigis_ps.trade_catalog import TradeQuote
from aigis_ps.training_catalog import NO_BOOST, OwnershipBonus, TrainingQuote
from aigis_ps.unit_capacity import BarracksUsage
from aigis_ps.unit_reference import FIRST_BARRACKS
from aigis_ps.unit_sale_catalog import SaleQuote
from aigis_ps.wallet_money import GOLD_LIMIT, money_after_capped_credit
from test_admin_views_core import (BASE, DATABASE, GAME_PORT, NOW_JST, PORT, SECRET, TOKEN, VIEW_MODULES,
                                   WRITE_ON_READ_ATTRIBUTES, Console, Tripwire, account_state, bootstrap,
                                   generation_for, persisting_reads, players, view_sources)
from test_bootstrap import asset_root
from test_evolution_catalog import materials, unit
from test_player_state import assets, master
from test_quest_service import bundle


TOOL_NAMES = ("tools.backgrounds", "tools.emblems", "tools.energy_clock", "tools.evolution", "tools.fame",
              "tools.growth_shop", "tools.item_ticket", "tools.orb_exchange", "tools.presents", "tools.silver_trade",
              "tools.summon_pool", "tools.summon_pools", "tools.training", "tools.unit_capacity", "tools.unit_sale",
              "tools.unit_skins")
PAGED = ("tools.backgrounds", "tools.emblems", "tools.fame", "tools.summon_pools", "tools.summon_pool")
# Only the bootstrap tables feed the clock conversion, so it is the one tool that needs no other service.
SERVICE_FREE = frozenset(("tools.energy_clock",))
PARAMS = {
    "tools.orb_exchange": {"item_id": "orb_target", "base_id": "orb_source"},
    "tools.evolution": {"target_get_index": 1, "operation": "first_awakening",
                        "materials": [{"source": "barracks", "get_index": 2}, {"source": "fairy_farm", "card_id": 2}]},
    "tools.training": {"target_get_index": 1,
                       "materials": [{"source": "barracks", "get_index": 2}, {"source": "fairy_farm", "card_id": 2}]},
    "tools.unit_sale": {"get_indexes": [1, 2]},
    "tools.silver_trade": {"shop_id": 29, "material_get_index": 1},
    "tools.growth_shop": {"shop_id": 2000, "target_get_index": 2},
    "tools.unit_skins": {"card_id": 2},
    "tools.backgrounds": {},
    "tools.emblems": {},
    "tools.fame": {},
    "tools.unit_capacity": None,
    "tools.summon_pools": {},
    "tools.summon_pool": {"pool_id": 7},
    "tools.item_ticket": {"item_id": "ticket_item"},
    "tools.presents": {"rewards": [{"kind": "gold", "amount": 10}]},
    "tools.energy_clock": {},
}
NOTE_FIELDS = ("projected", "persisted", "note")
BLANK_UNIT = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
FIXTURE_PRODUCT = SimpleNamespace(shop_id=29, data_restrict=3, start_at=datetime(2026, 1, 1, tzinfo=JST), end_at=None,
                                  card_id=2, item_id="")
SKIN_DISPLAYS = frozenset((5, 2, 21))
# Differs from the fixture usage remainder, so the view must report the capacity hook itself.
BARRACKS_AVAILABLE = 150
FIXTURE_USAGE = BarracksUsage((200, 250), (3, 1))
SECOND_BARRACKS, LOCKED_BARRACKS = FIRST_BARRACKS + 1, len(FIXTURE_USAGE.capacities)
GROWTH_TOOLS = (("tools.evolution", "evolution"), ("tools.training", "training"))
INCOMPLETE_POOL, COMPLETE_POOL = 7, 9
LIVE_NOW = datetime(2026, 11, 2, 12, 30, tzinfo=JST)
LIVE_MONEY = 1_000_000
ORBS = tuple(f"evolve_orb_10000{index}" for index in range(1, 8))
SILVER_PAYMENT, GROWTH_PAYMENT = "trade_item_001000", "trade_item_005000"
SILVER_PRODUCT, GROWTH_PRODUCT = 29, 2004
SILVER_CARD, GROWTH_CARD = 8, 7
SKIN_CARD, SKIN_ITEM = 26, "get_skin_00020"
COMPLETE_LIVE_POOL = 350
DISABLED_TICKET = "item_sample_smn_0001"


@dataclass(frozen=True)
class LeakyDefinition:
    item_id: str
    method_key: str


def tool_catalog():
    views = ViewRegistry()
    admin_views_tools.register(views)
    routes = RouteRegistry()
    admin_routes_state.register(routes)
    return AdminCatalog(CommandRegistry({}), views, routes, ("admin_routes_state", "admin_views_tools"))


def unit_row(card_id, get_index, class_id):
    return {**BLANK_UNIT, "CardID": card_id, "ClassID": class_id, "GetIndex": get_index, "UniqueID": get_index + 1,
            "SkillLevel": 1, "OrganizationIndex": -1, "GeneralPurposeFlags": 1}


def with_units(state, count):
    player = state["player_state"]
    player["units"].extend(unit_row(2, index, 20) for index in range(1, count + 1))
    player["battle"]["UX"] = count
    player["last_unique_id"] = count + 1
    return state


def stored_in(state, barracks, *get_indexes):
    for unit in state["player_state"]["units"]:
        if unit["GetIndex"] in get_indexes:
            unit["GeneralPurposeFlags"] |= barracks << BARRACKS_SHIFT
    return state


def without_note(section):
    assert {name: section[name] for name in NOTE_FIELDS} == {"projected": True, "persisted": False,
                                                            "note": PROJECTION_NOTE}
    return {name: value for name, value in section.items() if name not in (*NOTE_FIELDS, "ok")}


def refused(code, status=409):
    def fail(*arguments, **options):
        raise ProtocolError(code, "Refused for the fixture", status)
    return fail


class Calls(list):
    def record(self, name, result):
        def call(*arguments, **options):
            self.append((name, arguments, options))
            return result(*arguments, **options) if callable(result) else result
        return call

    def only(self, name):
        [(_, arguments, options)] = [entry for entry in self if entry[0] == name]
        return arguments, options

    def names(self):
        return [entry[0] for entry in self]


def possession_response(counts):
    def response(state, item_ids):
        names = tuple(item_ids)
        return Container("POSSESSION", (Column("ItemID", "S", names),
                                        Column("Count", "I", tuple(counts.get(name, 0) for name in names))))
    return response


def fixture_services(calls):
    conditions = ((MaterialCondition(class_type=0, rarity=2), MaterialCondition(card_id=2)),
                  (MaterialCondition(class_id=20, rarity=2),))
    evolution_quote = EvolutionQuote(5000, {"evolve_orb_100001": 2}, {"ClassID": 20, "ClassChangeCount": 2, "Exp": 7},
                                     (1, 0), None)
    evolution = SimpleNamespace(
        catalog=SimpleNamespace(quote=calls.record("evolution.quote", evolution_quote),
                                cards=SimpleNamespace(card=lambda card_id: {"CardID": card_id, "Rare": 2}),
                                classes={10: {"ClassID": 10}, 20: {"ClassID": 20}}, rarities={3: {"ConfigID": 3}},
                                materials=SimpleNamespace(slots=calls.record("evolution.slots", conditions))),
        possessions=SimpleNamespace(response=possession_response({"evolve_orb_100001": 9})),
        fairy_farm=SimpleNamespace(material=calls.record("fairy_farm.material", {**unit_row(2, 0, 20), "UniqueID": 0})),
        silver_house=None, barracks=None)
    training_bonus = OwnershipBonus(0, frozenset((NO_BOOST,)))
    training_quote = TrainingQuote(400, {"Exp": 7, "SkillLevel": 1, "CostModMixValue": 0, "SkillExperience": 0,
                                         "CostExperience": 0})
    training = SimpleNamespace(
        catalog=SimpleNamespace(ownership_bonus=calls.record("training.bonus", training_bonus),
                                quote_materials=calls.record("training.quote", training_quote),
                                describe=lambda unit: SimpleNamespace(level=unit["Exp"] + 1, cap=99)),
        fairy_farm=evolution.fairy_farm, silver_house=None, barracks=None)
    trade_quote = TradeQuote(FIXTURE_PRODUCT, 1, 2, 30, 30, 1, "possession", SILVER_PAYMENT, 106, 0, 40)
    trades = SimpleNamespace(
        history=SimpleNamespace(period=calls.record("history.period", "month:2026-11"),
                                records=calls.record("history.records", {"29": {"count": 2}}),
                                count=calls.record("history.count", 2)),
        silver=SimpleNamespace(product=calls.record("silver.product", FIXTURE_PRODUCT),
                               quote=calls.record("silver.quote", trade_quote),
                               material=calls.record("silver.material", lambda player, product, index, unique, state:
                                                     next(row for row in player["units"] if row["GetIndex"] == index))),
        growth_shop=SimpleNamespace(product=calls.record("growth.product", FIXTURE_PRODUCT),
                                    quote=calls.record("growth.quote", replace(trade_quote, registration=113)),
                                    values=calls.record("growth.values", {"ClassChangeCount": 2, "Love": 100})))
    definitions = (BackgroundDefinition("system_default", 0, 1, datetime(2001, 1, 1, tzinfo=JST), None, False),
                   BackgroundDefinition("event_room", 2, 4, None, datetime(2026, 1, 1, tzinfo=JST), False))
    titles = tuple(EmblemTitle(index, f"title {index}", "0xe52a3bdd(0)", "", 1, 1, 1, 10, 0, 0, 0, "", 0, "rank",
                               index, None) for index in range(3))
    pools = {COMPLETE_POOL: SummonPool(COMPLETE_POOL, 11, (SummonCandidate(2, 30, 0, 75.0, 3),
                                                           SummonCandidate(2, 1, 0, 25.0, 1)), 4, 100.0, True),
             INCOMPLETE_POOL: SummonPool(INCOMPLETE_POOL, 12, (SummonCandidate(2, 30, 0, 40.0, 1),), 1, 40.0, False)}

    def complete(pool_id):
        if not pools[pool_id].complete:
            raise ProtocolError("summon_pool_incomplete", "Summon pool probabilities are incomplete", 501)
        return pools[pool_id]

    def selection(name, guide, now):
        if name != "system_default":
            raise ProtocolError("background_unavailable", "Background is outside its availability window", 409)
        return 0, guide

    return {
        "orb_exchange": SimpleNamespace(definitions=(OrbDefinition("orb_target", 100, 5, 1, 999),),
                                        quote=calls.record("orb.quote", OrbExchangeQuote(
                                            "orb_target", "orb_source", 100, 5, 1, 900, 3, 0))),
        "evolution": evolution,
        "training": training,
        "sales": SimpleNamespace(catalog=SimpleNamespace(quote=calls.record("sale.quote", SaleQuote(600, 2)),
                                                         auto_card_ids=(1,), rainbow_item="trade_item_003000",
                                                         check_rating=lambda card_id: None)),
        "trades": trades,
        "unit_detail": SimpleNamespace(smartphone_images=True, skin_catalog=SimpleNamespace(
            allowed_displays=calls.record("skins.allowed_displays", SKIN_DISPLAYS))),
        "backgrounds": SimpleNamespace(catalog=SimpleNamespace(
            definitions=definitions, categories=frozenset((1,)), guide_id=calls.record("background.guide", 1),
            selection=selection)),
        "presents": SimpleNamespace(
            emblem_catalog=SimpleNamespace(titles=titles, require_eligible=lambda index, state: (
                refused("emblem_locked")() if index == 1 else titles[index])),
            rows=lambda state: [{"PresentID": 1}],
            preview=calls.record("presents.preview", lambda rewards, existing: [
                {"Type": 2, "Content": reward["amount"], "Description": reward["description"]} for reward in rewards])),
        "fame_repository": SimpleNamespace(catalog=SimpleNamespace(require_eligible=lambda unit: (
            None if unit["CardID"] == 1 else refused("fame_ineligible")()))),
        "unit_capacity": SimpleNamespace(usage=lambda state: FIXTURE_USAGE, available=lambda state: BARRACKS_AVAILABLE),
        "summon_pools": SimpleNamespace(pools=pools, get=lambda pool_id: pools[pool_id], require_complete=complete),
        "item_summon_tickets": SimpleNamespace(
            catalog=SimpleNamespace(definition=lambda item_id: ItemSummonDefinition(
                item_id, COMPLETE_POOL, 10, None, LotteryRule(4, 1, 6, 2, 1))),
            validate_item=refused("summon_ticket_pool_disabled", 501)),
    }


def tools_console(tmp_path, bundle, state):
    runtime = GenerationRuntime(generation_for(bundle, {"bootstrap": bundle.bootstrap.status()}))
    data = tmp_path / "data"
    store = Store(data / DATABASE, state)
    application = create_admin_app(runtime, store, data_dir=data, admin_host="127.0.0.1", admin_port=PORT,
                                   game_port=GAME_PORT, token=TOKEN, preflight=NoopPreflight(),
                                   restart_check=NoopRestartCheck(), plan_store=PlanStore(), catalog=tool_catalog(),
                                   ui_directory=tmp_path / "ui")
    return Console(TestClient(application, base_url=BASE), store, runtime, data, bundle,
                   bundle.bootstrap.startup_diagnostics)


def build_tools_console(tmp_path, bootstrap, players, *, state=None, **services):
    bundle = SimpleNamespace(bootstrap=bootstrap, player_state=players, initialize=lambda value: value,
                             votes=SimpleNamespace(read=Tripwire("VoteService.read")),
                             sugoroku=SimpleNamespace(read=Tripwire("SugorokuService.read")), **services)
    return tools_console(tmp_path, bundle, with_units(account_state(bootstrap, players), 2) if state is None else state)


@pytest.fixture
def calls():
    return Calls()


@pytest.fixture
def tools(tmp_path, bootstrap, players, calls):
    return build_tools_console(tmp_path, bootstrap, players, **fixture_services(calls))


def test_tools_register_strict_views_in_their_own_domain():
    registry = ViewRegistry()
    admin_views_tools.register(registry)
    assert tuple(view.name for view in registry) == TOOL_NAMES and len(VIEWS) == len(TOOL_NAMES)
    assert {view.domain for view in registry} == {"tools"}
    assert all(view.live and view.describe()["live"] for view in registry)
    for view in registry:
        assert callable(view.read.__wrapped__.__wrapped__)
        if view.params_model is not None:
            validate_params_model(view.params_model)
        json.dumps(view.describe())
    with pytest.raises(ValueError):
        admin_views_tools.register(registry)


def test_tool_sources_never_reach_persisting_reads():
    sources = {path.stem: path for path in view_sources()}
    assert "admin_views_tools" in sources and set(VIEW_MODULES) <= sources.keys()
    assert persisting_reads(sources["admin_views_tools"].read_text("utf-8")) == []


def test_every_tool_reads_one_snapshot_and_writes_nothing(tools):
    before = tools.store.profile()
    generation_id = tools.runtime.current.id
    for name in TOOL_NAMES:
        response = tools.get(name, PARAMS[name])
        assert response.status_code == 200, (name, response.text)
        body = response.json()
        assert (body["view"], body["revision"], body["generation_id"]) == (name, before.revision, generation_id)
        assert body["data"]["ok"] is True, (name, body["data"])
    after = tools.store.profile()
    assert (after.revision, json_digest(after.state)) == (before.revision, json_digest(before.state))
    assert tools.runtime.status()["current"]["leases"] == 0


def test_tools_report_missing_services_as_results(tmp_path, bootstrap, players):
    console = build_tools_console(tmp_path, bootstrap, players)
    for name in TOOL_NAMES:
        data = console.read(name, PARAMS[name])
        if name in SERVICE_FREE:
            assert data["ok"] is True and data["conversion"]["ok"] is True, name
            continue
        assert (data["ok"], data["error"]["code"]) == (False, "admin_view_service_unavailable"), name


def test_tool_results_are_redacted(tmp_path, bootstrap, players, calls):
    services = fixture_services(calls)
    services["orb_exchange"].definitions = (LeakyDefinition("orb_target", SECRET),)
    services["presents"].preview = lambda rewards, existing: [{"Type": 2, "user_key": SECRET}]
    console = build_tools_console(tmp_path, bootstrap, players, **services)
    for name in ("tools.orb_exchange", "tools.presents"):
        response = console.get(name, PARAMS[name])
        assert response.status_code == 200 and SECRET not in response.text, name
    assert console.read("tools.orb_exchange", PARAMS["tools.orb_exchange"])["definitions"] == [
        {"item_id": "orb_target", "method_key": REDACTED}]
    assert console.read("tools.presents", PARAMS["tools.presents"])["rows"] == [{"Type": 2, "user_key": REDACTED}]


@pytest.mark.parametrize("name", PAGED)
def test_paged_tools_bound_their_pages(tools, name):
    base = PARAMS[name]
    for invalid in ({"limit": MAX_PAGE_ITEMS + 1}, {"limit": 0}, {"offset": -1}, {"offset": "1"}):
        assert tools.get(name, {**base, **invalid}).status_code == 422, invalid
    first = tools.read(name, {**base, "limit": 1})
    listing = next(value for value in first.values() if isinstance(value, dict) and "items" in value)
    assert (len(listing["items"]), listing["offset"], listing["limit"]) == (1, 0, 1)
    assert listing["next_offset"] == (1 if listing["total"] > 1 else None)
    rest = tools.read(name, {**base, "offset": listing["total"]})
    assert next(value for value in rest.values() if isinstance(value, dict) and "items" in value)["items"] == []


@pytest.mark.parametrize("name, params", [
    ("tools.orb_exchange", {"item_id": "a"}),
    ("tools.orb_exchange", {"item_id": "", "base_id": "b"}),
    ("tools.orb_exchange", {"item_id": "a", "base_id": "b", "extra": 1}),
    ("tools.unit_sale", {"get_indexes": []}),
    ("tools.unit_sale", {"get_indexes": [1, 1]}),
    ("tools.unit_sale", {"get_indexes": [-1]}),
    ("tools.unit_sale", {"get_indexes": ["1"]}),
    ("tools.evolution", {"target_get_index": 1, "operation": "class_change"}),
    ("tools.evolution", {"target_get_index": 1, "operation": "first_awakening", "branch": 3}),
    ("tools.evolution", {"target_get_index": 1, "operation": "first_awakening",
                         "materials": [{"source": "barracks", "get_index": 1}]}),
    ("tools.evolution", {"target_get_index": 1, "operation": "first_awakening",
                         "materials": [{"source": "barracks", "get_index": 2}] * 2}),
    ("tools.evolution", {"target_get_index": 1, "operation": "first_awakening",
                         "materials": [{"source": "barracks", "card_id": 2}]}),
    ("tools.evolution", {"target_get_index": 1, "operation": "first_awakening",
                         "materials": [{"source": "silver_house", "get_index": 2}]}),
    ("tools.evolution", {"target_get_index": 1, "operation": "first_awakening",
                         "materials": [{"source": "warehouse", "card_id": 2}]}),
    ("tools.evolution", {"target_get_index": 1, "operation": "first_awakening",
                         "materials": [{"source": "fairy_farm", "card_id": 2}] * (MAX_EVOLUTION_MATERIALS + 1)}),
    ("tools.silver_trade", {"shop_id": 0}),
    ("tools.growth_shop", {"shop_id": 2000, "target_get_index": -1}),
    ("tools.unit_skins", {"card_id": True}),
    ("tools.summon_pool", {}),
    ("tools.item_ticket", {"item_id": "x" * 257}),
    ("tools.presents", {"rewards": []}),
    ("tools.presents", {"rewards": [{"kind": "crystal", "amount": 1}]}),
    ("tools.presents", {"rewards": [{"kind": "gold", "amount": 0}]}),
    ("tools.presents", {"rewards": [{"kind": "gold", "amount": 1, "item_id": "x"}]}),
    ("tools.presents", {"rewards": [{"kind": "unit", "template_id": 2}]}),
    ("tools.energy_clock", {"at": "2026-11-02T12:30:00"}),
    ("tools.energy_clock", {"at": 1}),
    ("tools.unit_capacity", {}),
])
def test_tool_parameters_are_strict(tools, name, params):
    response = tools.get(name, params)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "admin_params_invalid"


def test_parameter_models_bound_lists_and_name_each_unit_once():
    for invalid in ({"get_indexes": [3, 3]}, {"get_indexes": list(range(4097))}):
        with pytest.raises(ValidationError):
            UnitSaleParams.model_validate_json(json.dumps(invalid))
    gold = {"kind": "gold", "amount": 1}
    with pytest.raises(ValidationError):
        PresentPreviewParams.model_validate_json(json.dumps({"rewards": [gold] * (MAX_PRESENTS + 1)}))
    assert len(PresentPreviewParams.model_validate_json(json.dumps({"rewards": [gold] * MAX_PRESENTS})).rewards) == (
        MAX_PRESENTS)
    with pytest.raises(ValidationError):
        EvolutionParams.model_validate_json(json.dumps({"target_get_index": 1, "operation": "first_awakening",
                                                        "materials": [{"source": "barracks", "get_index": 1}]}))
    allowed = EvolutionParams.model_validate_json(json.dumps({
        "target_get_index": 1, "operation": "second_awakening", "branch": 2,
        "materials": [{"source": "silver_house", "card_id": 2}] * MAX_EVOLUTION_MATERIALS}))
    assert [material.source for material in allowed.materials] == ["silver_house"] * MAX_EVOLUTION_MATERIALS


def test_orb_exchange_quotes_the_snapshot(tools, calls):
    data = tools.read("tools.orb_exchange", PARAMS["tools.orb_exchange"])
    assert data["definitions"] == [{"item_id": "orb_target", "price": 100, "amount_required": 5, "amount_created": 1,
                                    "stack": 999}]
    assert without_note(data["quote"]) == asdict(OrbExchangeQuote("orb_target", "orb_source", 100, 5, 1, 900, 3, 0))
    assert calls.only("orb.quote") == ((tools.store.profile().state, "orb_target", "orb_source"), {})


def test_orb_exchange_refusals_stay_results(tools):
    tools.bundle.orb_exchange.quote = refused("orb_exchange_money_insufficient")
    data = tools.read("tools.orb_exchange", PARAMS["tools.orb_exchange"])
    assert data["ok"] is True and len(data["definitions"]) == 1
    assert data["quote"] == {"ok": False, "error": {"code": "orb_exchange_money_insufficient",
                                                    "message": "Refused for the fixture"}}


def test_evolution_quotes_a_private_target_and_the_resolved_materials(tools, calls):
    def mutating(target, chosen, operation, branch):
        target["Exp"] = -1
        return EvolutionQuote(5000, {"evolve_orb_100001": 2}, {"ClassChangeCount": 2, "Exp": 7}, (1, 0), None)

    tools.bundle.evolution.catalog.quote = calls.record("evolution.quote", mutating)
    data = tools.read("tools.evolution", PARAMS["tools.evolution"])
    assert data["target"] == {"GetIndex": 1, "UniqueID": 2, "CardID": 2, "ClassID": 20, "ClassChangeCount": 0,
                              "barracks": FIRST_BARRACKS, "storage_supported": True}
    assert data["materials"] == [{"source": "barracks", "GetIndex": 2, "UniqueID": 3, "CardID": 2, "ClassID": 20,
                                  "ClassChangeCount": 0, "barracks": FIRST_BARRACKS, "storage_supported": True},
                                 {"source": "fairy_farm", "GetIndex": 0, "UniqueID": 0, "CardID": 2, "ClassID": 20,
                                  "ClassChangeCount": 0}]
    assert data["slots"] == {"ok": True, "slots": [
        [asdict(MaterialCondition(class_type=0, rarity=2)), asdict(MaterialCondition(card_id=2))],
        [asdict(MaterialCondition(class_id=20, rarity=2))]]}
    assert without_note(data["quote"]) == {
        "price": 5000, "money": 1000, "items": {"evolve_orb_100001": {"cost": 2, "held": 9}},
        "changes": {"ClassChangeCount": {"before": 0, "after": 2}, "Exp": {"before": 0, "after": 7}},
        "material_assignment": [1, 0], "reward_card_id": None}
    (target, chosen, operation, branch), _ = calls.only("evolution.quote")
    assert (target["Exp"], operation, branch) == (-1, 7, 0)
    assert [row["GetIndex"] for row in chosen] == [2, 0]
    assert chosen[1] == {**unit_row(2, 0, 20), "UniqueID": 0}
    (described, slot_operation), _ = calls.only("evolution.slots")
    assert (described.card["CardID"], described.class_row, described.rarity, slot_operation) == (
        2, {"ClassID": 20}, {"ConfigID": 3}, 7)
    assert calls.only("fairy_farm.material") == ((2,), {})


def test_evolution_quotes_copies_so_warehouse_templates_stay_unchanged(tools):
    def mutating(target, chosen, operation, branch):
        for row in chosen:
            row["ClassChangeCount"] = 4
        return EvolutionQuote(5000, {"evolve_orb_100001": 2}, {"ClassChangeCount": 2, "Exp": 7}, (1, 0), None)

    tools.bundle.evolution.catalog.quote = mutating
    first = tools.read("tools.evolution", PARAMS["tools.evolution"])
    assert first["quote"]["ok"] is True
    assert tools.read("tools.evolution", PARAMS["tools.evolution"])["materials"] == first["materials"]


@pytest.mark.parametrize("operation, registration", sorted(EVOLUTION_REGISTRATIONS.items()))
def test_evolution_operations_use_native_registrations(tools, calls, operation, registration):
    branch = 1 if operation == EvolutionOperation.SECOND_AWAKENING else 0
    data = tools.read("tools.evolution", {**PARAMS["tools.evolution"], "operation": operation, "branch": branch})
    (_, _, sent, sent_branch), _ = calls.only("evolution.quote")
    assert (sent, sent_branch) == (registration, branch)
    slotted = operation != EvolutionOperation.SKILL_AWAKENING
    assert (data["slots"] is None) is not slotted
    assert [entry[1][1] for entry in calls if entry[0] == "evolution.slots"] == ([registration] if slotted else [])


def test_evolution_reports_unresolved_inputs(tools):
    missing = tools.read("tools.evolution", {**PARAMS["tools.evolution"], "target_get_index": 9})
    assert missing == {"ok": False, "error": {"code": "admin_tool_unit_missing",
                                              "message": "No owned unit has GetIndex 9"}}
    silver = tools.read("tools.evolution", {**PARAMS["tools.evolution"],
                                            "materials": [{"source": "silver_house", "card_id": 2}]})
    assert silver["error"]["code"] == "admin_view_service_unavailable"
    tools.bundle.evolution.catalog.classes = {}
    unmatched = tools.read("tools.evolution", PARAMS["tools.evolution"])
    assert unmatched["ok"] is True and unmatched["quote"]["ok"] is True
    assert unmatched["slots"]["error"]["code"] == "evolution_materials_invalid"


def test_training_quotes_copies_and_names_locked_barracks_materials(tmp_path, bootstrap, players, calls):
    state = with_units(account_state(bootstrap, players), 2)
    next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == 2)["GeneralPurposeFlags"] |= LOCK_FLAG
    services = fixture_services(calls)
    quoted = services["training"].catalog.quote_materials

    def mutating(player, target, materials, **options):
        for row in (target, *materials):
            row["ClassChangeCount"] = 4
        return quoted(player, target, materials, **options)

    services["training"].catalog.quote_materials = mutating
    console = build_tools_console(tmp_path, bootstrap, players, state=state, **services)
    first = console.read("tools.training", PARAMS["tools.training"])
    assert first["quote"]["ok"] is True and first["protection"]["ok"] is False
    assert first["protection"]["error"]["code"] == "training_material_protected"
    assert first["protection"]["error"]["details"] == {"protected": [{"GetIndex": 2, "protections": ["locked"]}]}
    second = console.read("tools.training", PARAMS["tools.training"])
    assert (second["target"], second["materials"]) == (first["target"], first["materials"])
    assert first["target"]["ClassChangeCount"] == 0


def storage_of(entry):
    return entry["barracks"], entry["storage_supported"]


@pytest.fixture
def stored_preview(tmp_path, bootstrap, players, calls):
    def read(name, growth, barracks, get_index, *, attached):
        services = fixture_services(calls)
        services[growth].barracks = services["unit_capacity"] if attached else None
        state = stored_in(with_units(account_state(bootstrap, players), 2), barracks, get_index)
        console = build_tools_console(tmp_path / f"{barracks}-{attached}", bootstrap, players, state=state, **services)
        return console.read(name, PARAMS[name])

    return read


@pytest.mark.parametrize("name, growth", GROWTH_TOOLS)
def test_growth_targets_outside_the_first_barracks_are_unsupported(stored_preview, name, growth):
    target = PARAMS[name]["target_get_index"]
    first, stored = (stored_preview(name, growth, barracks, target, attached=True)
                     for barracks in (FIRST_BARRACKS, SECOND_BARRACKS))
    assert (storage_of(first["target"]), storage_of(stored["target"])) == (
        (FIRST_BARRACKS, True), (SECOND_BARRACKS, False))
    assert (stored["protection"], stored["quote"]) == (first["protection"], first["quote"])


@pytest.mark.parametrize("name, growth", GROWTH_TOOLS)
def test_growth_barracks_materials_follow_the_service_barracks_rule(stored_preview, name, growth):
    material = PARAMS[name]["materials"][0]["get_index"]
    cases = ((FIRST_BARRACKS, False, True), (SECOND_BARRACKS, False, False), (SECOND_BARRACKS, True, True),
             (LOCKED_BARRACKS, True, False))
    previews = [stored_preview(name, growth, barracks, material, attached=attached) for barracks, attached, _ in cases]
    assert [storage_of(data["materials"][0]) for data in previews] == [
        (barracks, supported) for barracks, _, supported in cases]
    assert all({"barracks", "storage_supported"}.isdisjoint(data["materials"][1]) for data in previews)
    first = previews[0]
    assert all((data["protection"], data["quote"]) == (first["protection"], first["quote"]) for data in previews)


def test_unit_sale_reports_rating_and_automatic_eligibility(tools, calls):
    restricted = refused("unit_sale_rating_unsupported", 501)
    tools.bundle.sales.catalog.check_rating = calls.record(
        "sale.rating", lambda card_id: restricted() if card_id == 2 else None)
    data = tools.read("tools.unit_sale", {"get_indexes": [2, 0]})
    assert [(unit["GetIndex"], unit["automatic"]) for unit in data["units"]] == [(2, False), (0, True)]
    assert data["units"][0]["rating"]["error"]["code"] == "unit_sale_rating_unsupported"
    assert data["units"][1]["rating"] == {"ok": True}
    assert [entry[1] for entry in calls if entry[0] == "sale.rating"] == [(2,), (1,)]
    balance = tools.store.profile().state["tables"]["game_param"][0]["Money"]
    assert without_note(data["quote"]) == {"gold": 600, "rainbow": 2, "rainbow_item": "trade_item_003000",
                                           "money": {"before": balance, "after": balance + 600, "discarded": 0}}
    (units,), _ = calls.only("sale.quote")
    assert [row["GetIndex"] for row in units] == [2, 0]
    assert tools.read("tools.unit_sale", {"get_indexes": [3]})["error"]["code"] == "admin_tool_unit_missing"


def test_unit_sale_names_the_protections_the_manual_sale_refuses(tmp_path, bootstrap, players, calls):
    state = with_units(account_state(bootstrap, players), 2)
    next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == 2)["GeneralPurposeFlags"] |= LOCK_FLAG
    console = build_tools_console(tmp_path, bootstrap, players, state=state, **fixture_services(calls))
    free, locked, prince = console.read("tools.unit_sale", {"get_indexes": [1, 2, 0]})["units"]
    assert (free["protections"], locked["protections"]) == ([], ["locked"])
    assert prince["protections"][0] == "prince"


def test_unit_sale_marks_units_outside_the_first_barracks(tmp_path, bootstrap, players, calls):
    state = with_units(account_state(bootstrap, players), 2)
    moved = next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == 2)
    moved["GeneralPurposeFlags"] |= 1 << BARRACKS_SHIFT
    console = build_tools_console(tmp_path, bootstrap, players, state=state, **fixture_services(calls))
    first, stored = console.read("tools.unit_sale", {"get_indexes": [1, 2]})["units"]
    assert (first["barracks"], first["storage_supported"]) == (FIRST_BARRACKS, True)
    assert (stored["barracks"], stored["storage_supported"]) == (1, False)


@pytest.mark.parametrize("balance, after, discarded", [(GOLD_LIMIT - 100, GOLD_LIMIT, 500),
                                                       (GOLD_LIMIT + 5, GOLD_LIMIT + 5, 600)])
def test_unit_sale_projects_gold_discarded_over_the_cap(tmp_path, bootstrap, players, calls, balance, after,
                                                        discarded):
    state = with_units(account_state(bootstrap, players), 2)
    state["tables"]["game_param"][0]["Money"] = balance
    console = build_tools_console(tmp_path, bootstrap, players, state=state, **fixture_services(calls))
    quote = without_note(console.read("tools.unit_sale", PARAMS["tools.unit_sale"])["quote"])
    assert (quote["gold"], quote["money"]) == (600, {"before": balance, "after": after, "discarded": discarded})


def test_silver_trade_counts_purchases_before_quoting(tools, calls):
    data = tools.read("tools.silver_trade", PARAMS["tools.silver_trade"])
    assert without_note(data["quote"]) == {
        "shop_id": 29, "period": "month:2026-11", "exchange_count": 2, "limit": 3,
        "window": {"start": "2026-01-01T00:00:00+09:00", "end": None}, "unit_price": 30, "total_price": 30,
        "ownership_discount": 0, "currency": {"kind": "possession", "item_id": SILVER_PAYMENT, "balance": 40},
        "reward": {"card_id": 2, "item_id": "", "amount": 1}}
    assert data["material"] == {"ok": True, "GetIndex": 1, "UniqueID": 2, "CardID": 2, "ClassID": 20,
                                "ClassChangeCount": 0, "barracks": FIRST_BARRACKS, "storage_supported": True}
    state = tools.store.profile().state
    assert calls.only("history.period") == ((FIXTURE_PRODUCT, NOW_JST), {})
    assert calls.only("history.records") == ((state, NOW_JST), {})
    assert calls.only("history.count") == (({"29": {"count": 2}}, FIXTURE_PRODUCT, NOW_JST), {})
    assert calls.only("silver.quote") == ((state, 29, SINGLE_PURCHASE, 2, NOW_JST), {})
    (player, product, index, unique), options = calls.only("silver.material")
    assert (player, product, index, unique, options) == (state["player_state"], FIXTURE_PRODUCT, 1, 2, {"state": state})
    assert tools.read("tools.silver_trade", {"shop_id": 29})["material"] is None
    assert tools.read("tools.silver_trade", {"shop_id": 29, "material_get_index": 9})["error"]["code"] == (
        "admin_tool_unit_missing")


def test_silver_trades_report_the_native_barracks_gate(tools):
    assert tools.read("tools.silver_trade", {"shop_id": 29})["barracks"] == {"ok": True}
    tools.bundle.player_state.capacity_provider = lambda state: 0
    full = tools.read("tools.silver_trade", {"shop_id": 29})
    assert full["barracks"]["error"]["code"] == "unit_roster_capacity" and full["quote"]["ok"] is True


def test_unconfigured_reset_policies_refuse_like_the_trade_service(tools, calls):
    tools.bundle.trades.history.period = lambda product, now: "unconfigured"
    data = tools.read("tools.growth_shop", PARAMS["tools.growth_shop"])
    assert data["quote"]["error"] == {"code": "trade_reset_unconfigured",
                                      "message": "The local reset policy for this product has not been configured"}
    assert calls.names()[:2] == ["history.records", "growth.product"]
    assert not {"growth.quote", "history.count"} & set(calls.names())
    assert data["values"]["ok"] is True


def test_trade_previews_validate_the_history_before_the_product(tools):
    trades = tools.bundle.trades
    trades.history.records = refused("trade_history_invalid", 503)
    trades.silver.product = trades.growth_shop.product = refused("trade_product_unknown")
    for name in ("tools.silver_trade", "tools.growth_shop"):
        quote = tools.read(name, {"shop_id": PARAMS[name]["shop_id"]})["quote"]
        assert quote["error"]["code"] == "trade_history_invalid", name


def test_growth_shop_projects_the_target_changes(tools, calls):
    data = tools.read("tools.growth_shop", PARAMS["tools.growth_shop"])
    assert without_note(data["quote"])["exchange_count"] == 2
    assert calls.only("growth.quote")[0][2:4] == (SINGLE_PURCHASE, 2)
    assert without_note(data["values"]) == {
        "target": {"GetIndex": 2, "UniqueID": 3, "CardID": 2, "ClassID": 20, "ClassChangeCount": 0,
                   "barracks": FIRST_BARRACKS, "storage_supported": True},
        "changes": {"ClassChangeCount": {"before": 0, "after": 2}, "Love": {"before": 0, "after": 100}}}
    state = tools.store.profile().state
    assert calls.only("growth.values") == ((state["player_state"], FIXTURE_PRODUCT, 2), {"state": state})
    assert tools.read("tools.growth_shop", {"shop_id": 2000})["values"] is None
    tools.bundle.trades.growth_shop.values = refused("shop_growth_target_missing")
    assert tools.read("tools.growth_shop", {"shop_id": 2000, "target_get_index": 9})["values"]["error"]["code"] == (
        "shop_growth_target_missing")


def test_trade_units_outside_the_first_barracks_are_unsupported(tmp_path, bootstrap, players, calls):
    silver, growth = PARAMS["tools.silver_trade"], PARAMS["tools.growth_shop"]
    state = stored_in(with_units(account_state(bootstrap, players), 2), SECOND_BARRACKS,
                      silver["material_get_index"], growth["target_get_index"])
    console = build_tools_console(tmp_path, bootstrap, players, state=state, **fixture_services(calls))
    material = console.read("tools.silver_trade", silver)["material"]
    target = without_note(console.read("tools.growth_shop", growth)["values"])["target"]
    assert storage_of(material) == storage_of(target) == (SECOND_BARRACKS, False)


def test_unit_skins_follow_the_unit_detail_image_setting(tools, calls):
    assert tools.read("tools.unit_skins", {"card_id": 2}) == {"ok": True, "card_id": 2, "smartphone_images": True,
                                                              "allowed_displays": sorted(SKIN_DISPLAYS)}
    assert calls.only("skins.allowed_displays") == ((2, tools.store.profile().state), {"smartphone_images": True})
    tools.bundle.unit_detail.smartphone_images = False
    tools.read("tools.unit_skins", {"card_id": 2})
    assert calls[-1][2] == {"smartphone_images": False}
    tools.bundle.unit_detail.skin_catalog = None
    assert tools.read("tools.unit_skins", {"card_id": 2})["error"]["code"] == "admin_view_service_unavailable"


def test_backgrounds_preview_each_selection_for_the_current_guide(tools, calls):
    data = tools.read("tools.backgrounds")
    assert data["guide_card_id"] == 1
    default, room = data["definitions"]["items"]
    assert default == {"name": "system_default", "card_id": 0, "category": 1, "selectable": True, "is_fixed": False,
                       "begin": "2001-01-01T00:00:00+09:00", "end": None, "selection": {"ok": True, "card_ids": [0, 1]}}
    assert (room["card_id"], room["selectable"], room["begin"], room["end"], room["selection"]["error"]["code"]) == (
        2, False, None, "2026-01-01T00:00:00+09:00", "background_unavailable")
    state = tools.store.profile().state
    assert calls.only("background.guide") == ((state["player_state"], state["tables"]["config"][0]), {})


def test_emblem_titles_report_each_eligibility(tools):
    titles = tools.read("tools.emblems")["titles"]
    assert [title["index"] for title in titles["items"]] == [0, 1, 2] and titles["total"] == 3
    assert [title["eligibility"]["ok"] for title in titles["items"]] == [True, False, True]
    assert titles["items"][1]["eligibility"]["error"]["code"] == "emblem_locked"
    assert (titles["items"][2]["predicate"], titles["items"][2]["argument"]) == ("rank", 2)


def test_fame_lists_units_in_get_index_order(tmp_path, bootstrap, players, calls):
    state = with_units(account_state(bootstrap, players), 2)
    state["player_state"]["units"].reverse()
    console = build_tools_console(tmp_path, bootstrap, players, state=state, **fixture_services(calls))
    units = console.read("tools.fame", {"offset": 1})["units"]
    assert [unit["GetIndex"] for unit in units["items"]] == [1, 2] and units["total"] == 3
    assert {unit["eligibility"]["error"]["code"] for unit in units["items"]} == {"fame_ineligible"}
    assert console.read("tools.fame", {"limit": 1})["units"]["items"][0]["eligibility"] == {"ok": True}


def test_unit_capacity_reports_barracks_and_grantable_slots(tools, players):
    data = tools.read("tools.unit_capacity")
    assert data["barracks"] == {"ok": True, "capacities": [200, 250], "counts": [3, 1],
                                "primary_available": BARRACKS_AVAILABLE}
    assert data["unit_slots"] == {"ok": True, "available": players.available_unit_slots(tools.store.profile().state)}
    tools.bundle.unit_capacity.usage = refused("unit_capacity_barracks_unsupported")
    refusal = tools.read("tools.unit_capacity")
    assert refusal["barracks"]["error"]["code"] == "unit_capacity_barracks_unsupported"
    assert refusal["unit_slots"]["ok"] is True


def test_summon_pools_keep_raw_percentages_without_normalizing(tools):
    pools = tools.read("tools.summon_pools")["pools"]
    assert [pool["pool_id"] for pool in pools["items"]] == [INCOMPLETE_POOL, COMPLETE_POOL]
    assert pools["items"][0] == {
        "pool_id": INCOMPLETE_POOL, "name_id": 12, "candidate_count": 1, "total_weight": 1, "percent_sum": 40.0,
        "complete": False, "drawable": {"ok": False, "error": {"code": "summon_pool_incomplete",
                                                               "message": "Summon pool probabilities are incomplete"}}}
    incomplete = tools.read("tools.summon_pool", {"pool_id": INCOMPLETE_POOL})
    assert incomplete["candidates"]["items"] == [{"card_id": 2, "level": 30, "evolution_count": 0, "percent": 40.0,
                                                  "weight": 1, "draw_probability": None}]
    complete = tools.read("tools.summon_pool", {"pool_id": COMPLETE_POOL, "offset": 1})
    assert (complete["drawable"], complete["candidate_count"], complete["total_weight"]) == ({"ok": True}, 2, 4)
    assert complete["candidates"]["items"] == [{"card_id": 2, "level": 1, "evolution_count": 0, "percent": 25.0,
                                                "weight": 1, "draw_probability": 0.25}]
    tools.bundle.summon_pools.get = refused("summon_pool_unknown", 501)
    assert tools.read("tools.summon_pool", {"pool_id": 1})["error"]["code"] == "summon_pool_unknown"


def test_item_tickets_show_the_definition_beside_the_delivery_check(tools):
    data = tools.read("tools.item_ticket", {"item_id": "ticket_item"})
    assert data["definition"] == {"ok": True, "item_id": "ticket_item", "pool_id": COMPLETE_POOL, "count": 10,
                                  "unsupported_reason": None, "rule": {"rule_id": 4, "rule_type": 1, "rarity_id": 6,
                                                                      "rarity_group": 2, "ensured_count": 1}}
    assert data["delivery"]["error"]["code"] == "summon_ticket_pool_disabled"
    tickets = tools.bundle.item_summon_tickets
    tickets.validate_item = lambda item_id: ItemSummonDefinition(item_id, COMPLETE_POOL, 10, None)
    tickets.catalog.definition = refused("item_summon_unknown", 501)
    unknown = tools.read("tools.item_ticket", {"item_id": "ticket_item"})
    assert unknown["definition"]["error"]["code"] == "item_summon_unknown" and unknown["delivery"] == {"ok": True}


def test_present_preview_passes_service_shaped_rewards(tools, calls):
    params = {"rewards": [{"kind": "gold", "amount": 10, "description": "local"},
                          {"kind": "charisma_recovery"},
                          {"kind": "charisma_recovery", "item_id": "item_recovery", "description": "drink"}]}
    tools.bundle.presents.preview = calls.record("presents.preview", lambda rewards, existing: [{"Type": 2}] * 3)
    data = tools.read("tools.presents", params)
    assert without_note(data) == {"existing": 1, "added": 3, "capacity": MAX_PRESENTS, "rows": [{"Type": 2}] * 3}
    (rewards, existing), _ = calls.only("presents.preview")
    assert existing == 1 and rewards == [{"kind": "gold", "amount": 10, "description": "local"},
                                         {"kind": "charisma_recovery", "description": ""},
                                         {"kind": "charisma_recovery", "item_id": "item_recovery",
                                          "description": "drink"}]
    tools.bundle.presents.preview = refused("present_capacity")
    assert tools.read("tools.presents", params) == {"ok": False, "error": {"code": "present_capacity",
                                                                           "message": "Refused for the fixture"}}


def test_present_rewards_cover_every_service_kind():
    kinds = {"unit": {"template_id": 2, "count": 1}, "free_crystal": {"amount": 5}, "gold": {"amount": 5},
             "item": {"item_id": "item", "amount": 1}, "legacy_item": {"present_type": 3, "amount": 1},
             "summon_ticket": {"pool_id": 350, "count": 2}, "selection_ticket": {"config_id": 1, "count": 1},
             "charisma_recovery": {}, "vote_cooking": {"card_id": 2}}
    params = PresentPreviewParams.model_validate_json(json.dumps(
        {"rewards": [{"kind": kind, **fields} for kind, fields in kinds.items()]}))
    assert [reward.model_dump(exclude_none=True) for reward in params.rewards] == [
        {"kind": kind, **fields, "description": ""} for kind, fields in kinds.items()]


def test_energy_clock_converts_instants_into_native_anchors(tools):
    moment = datetime(2026, 11, 2, 3, 30, 5, tzinfo=timezone.utc)
    data = tools.read("tools.energy_clock", {"at": moment.isoformat()})
    state = tools.store.profile().state
    assert data["marker"] == {"ok": True, "marker": state["energy_clock"]}
    conversion = without_note(data["conversion"])
    seconds, expected = energy_seconds(moment), {}
    for prefix in PREFIXES:
        set_anchor(expected, seconds, prefix)
    assert conversion["instant"] == {"utc": "2026-11-02T03:30:05+00:00", "jst": "2026-11-02T12:30:05+09:00"}
    assert (conversion["seconds"], conversion["fields"]) == (seconds, expected)
    assert conversion["decoded"] == {prefix: {"seconds": seconds, "jst": "2026-11-02T12:30:05+09:00"}
                                     for prefix in PREFIXES}
    assert conversion["current"] == anchors(state["tables"]["game_param"][0])
    current = without_note(tools.read("tools.energy_clock")["conversion"])
    assert current["instant"]["jst"] == NOW_JST.isoformat() and current["seconds"] == energy_seconds(NOW_JST)


def test_energy_clock_reports_unreadable_saves(tmp_path, bootstrap, players, calls):
    state = with_units(account_state(bootstrap, players), 2)
    state["energy_clock"]["origin"] = "unknown"
    console = build_tools_console(tmp_path, bootstrap, players, state=state, **fixture_services(calls))
    data = console.read("tools.energy_clock")
    assert data["marker"]["error"]["code"] == "energy_clock_invalid" and data["conversion"]["ok"] is True
    ancient = console.read("tools.energy_clock", {"at": "0001-01-01T00:00:00+14:00"})
    assert ancient["conversion"]["error"]["code"] == "energy_clock_argument"


@pytest.fixture
def live(bundle, tmp_path, monkeypatch):
    for name in WRITE_ON_READ_ATTRIBUTES:
        monkeypatch.setattr(bundle.bootstrap, name, Tripwire(f"bootstrap.{name}"))
    for name, label in (("votes", "VoteService.read"), ("sugoroku", "SugorokuService.read")):
        if getattr(bundle, name) is not None:
            monkeypatch.setattr(getattr(bundle, name), "read", Tripwire(label))
    clocked = copy(bundle.bootstrap)
    clocked.clock = lambda: LIVE_NOW
    catalog = bundle.evolution.catalog
    target = unit(catalog, level=999)
    target["Exp"] += 123
    state = bundle.player_state.initialize(clocked.initial_state())
    rows = [deepcopy(row) for row in (target, *materials(catalog, target, 7))]
    for index, row in enumerate(rows, 1):
        row.update(GetIndex=index, UniqueID=index + 1)
    player = state["player_state"]
    player["units"].extend(rows)
    player["battle"]["UX"], player["last_unique_id"] = len(rows), len(rows) + 1
    state, extra = bundle.player_state.credit_units(state, [{"card_id": SILVER_CARD, "level": 1, "count": 1},
                                                            {"card_id": GROWTH_CARD, "level": 1, "count": 1}])
    state["tables"]["game_param"][0]["Money"] = LIVE_MONEY
    costs = catalog.quote(rows[0], rows[1:], 7).items
    state = bundle.possessions.credit(state, {**dict.fromkeys(ORBS, 100), SILVER_PAYMENT: 40, GROWTH_PAYMENT: 20,
                                              SKIN_ITEM: 1, **{name: count * 2 for name, count in costs.items()}})
    records = {row["QuestID"]: row for row in state["tables"]["quest_records"]}
    for identifier in bundle.trades.silver.product(SILVER_PRODUCT).disclosure_quest_ids:
        records[identifier] = {"QuestID": identifier, "MaxRank": 3, "AchieveFlag": 0x01000000, "RemainAssault": 0,
                               "FirstDefeatDate": 0, "Shown": True}
    state["tables"]["quest_records"] = list(records.values())
    console = tools_console(tmp_path, replace(bundle, bootstrap=clocked), state)
    return SimpleNamespace(console=console, bundle=bundle, state=console.store.profile().state, rows=rows,
                           silver=extra[0], growth=extra[1])


def outcome(call):
    try:
        call()
    except ProtocolError as error:
        return {"ok": False, "code": error.code}
    return {"ok": True}


def verdict(section):
    return {"ok": True} if section["ok"] else {"ok": False, "code": section["error"]["code"]}


def test_live_tools_read_the_real_services_without_writing(live):
    console = live.console
    before = console.store.profile()
    chosen = [{"source": "barracks", "get_index": row["GetIndex"]} for row in live.rows[1:]]
    requests = {
        "tools.orb_exchange": {"item_id": ORBS[0], "base_id": ORBS[1]},
        "tools.evolution": {"target_get_index": live.rows[0]["GetIndex"], "operation": "first_awakening",
                            "materials": chosen},
        "tools.training": {"target_get_index": live.rows[0]["GetIndex"], "materials": chosen[:1]},
        "tools.unit_sale": {"get_indexes": [live.silver["GetIndex"], live.growth["GetIndex"]]},
        "tools.silver_trade": {"shop_id": SILVER_PRODUCT, "material_get_index": live.silver["GetIndex"]},
        "tools.growth_shop": {"shop_id": GROWTH_PRODUCT, "target_get_index": live.growth["GetIndex"]},
        "tools.unit_skins": {"card_id": SKIN_CARD},
        "tools.backgrounds": {"limit": MAX_PAGE_ITEMS},
        "tools.emblems": {"limit": MAX_PAGE_ITEMS},
        "tools.fame": {},
        "tools.unit_capacity": None,
        "tools.summon_pools": {"limit": 5},
        "tools.summon_pool": {"pool_id": COMPLETE_LIVE_POOL, "limit": 5},
        "tools.item_ticket": {"item_id": DISABLED_TICKET},
        "tools.presents": {"rewards": [{"kind": "gold", "amount": 100},
                                       {"kind": "summon_ticket", "pool_id": COMPLETE_LIVE_POOL, "count": 2}]},
        "tools.energy_clock": {"at": LIVE_NOW.isoformat()},
    }
    assert set(requests) == set(TOOL_NAMES)
    for name, params in requests.items():
        response = console.get(name, params)
        assert response.status_code == 200, (name, response.text)
        body = response.json()
        assert body["server_now"]["jst"] == LIVE_NOW.isoformat() and body["data"]["ok"] is True, (name, body["data"])
    after = console.store.profile()
    assert (after.revision, json_digest(after.state)) == (before.revision, json_digest(before.state))
    assert console.runtime.status()["current"]["leases"] == 0


def test_live_quotes_match_the_service_hooks(live):
    bundle, state, read = live.bundle, live.state, live.console.read
    orb = read("tools.orb_exchange", {"item_id": ORBS[0], "base_id": ORBS[1]})
    assert without_note(orb["quote"]) == asdict(bundle.orb_exchange.quote(state, ORBS[0], ORBS[1]))
    assert orb["definitions"] == [asdict(definition) for definition in bundle.orb_exchange.definitions]
    catalog, target, chosen = bundle.evolution.catalog, live.rows[0], live.rows[1:]
    evolution = read("tools.evolution", {"target_get_index": target["GetIndex"], "operation": "first_awakening",
                                         "materials": [{"source": "barracks", "get_index": row["GetIndex"]}
                                                       for row in chosen]})
    expected = catalog.quote(target, chosen, 7)
    quote = without_note(evolution["quote"])
    assert (quote["price"], quote["material_assignment"], quote["money"]) == (
        expected.price, list(expected.material_assignment), LIVE_MONEY)
    assert {name: change["after"] for name, change in quote["changes"].items()} == dict(expected.values)
    assert quote["items"] == {name: {"cost": cost, "held": cost * 2} for name, cost in expected.items.items()}
    slots = catalog.materials.slots(catalog.growth.describe(target), 7)
    assert evolution["slots"]["slots"] == [[asdict(condition) for condition in slot] for slot in slots]
    sale = read("tools.unit_sale", {"get_indexes": [live.silver["GetIndex"], live.growth["GetIndex"]]})
    proceeds = bundle.sales.catalog.quote([live.silver, live.growth])
    credited = money_after_capped_credit(state, proceeds.gold, cap=GOLD_LIMIT)
    assert without_note(sale["quote"]) == {**asdict(proceeds), "rainbow_item": bundle.sales.catalog.rainbow_item,
                                           "money": {"before": LIVE_MONEY, "after": credited,
                                                     "discarded": LIVE_MONEY + proceeds.gold - credited}}
    trades = bundle.trades
    for name, shop_id, shop, field, selected in (
            ("tools.silver_trade", SILVER_PRODUCT, trades.silver, "material_get_index", live.silver),
            ("tools.growth_shop", GROWTH_PRODUCT, trades.growth_shop, "target_get_index", live.growth)):
        data = read(name, {"shop_id": shop_id, field: selected["GetIndex"]})
        product = shop.product(shop_id)
        count = trades.history.count(trades.history.records(state, LIVE_NOW), product, LIVE_NOW)
        expected = shop.quote(state, shop_id, SINGLE_PURCHASE, count, LIVE_NOW)
        quote = without_note(data["quote"])
        assert (quote["unit_price"], quote["total_price"], quote["currency"]["balance"], quote["exchange_count"],
                quote["period"]) == (expected.unit_price, expected.total_price, expected.currency_balance,
                                     expected.exchange_count, trades.history.period(product, LIVE_NOW))
    material = read("tools.silver_trade", {"shop_id": SILVER_PRODUCT, "material_get_index": live.silver["GetIndex"]})
    assert material["material"] == {"ok": True, **{name: live.silver[name] for name in
                                                   ("GetIndex", "UniqueID", "CardID", "ClassID", "ClassChangeCount")},
                                    "barracks": FIRST_BARRACKS, "storage_supported": True}
    assert verdict(material["barracks"]) == outcome(
        lambda: bundle.player_state.require_barracks_space(state, SINGLE_PURCHASE))
    values = read("tools.growth_shop", {"shop_id": GROWTH_PRODUCT, "target_get_index": live.growth["GetIndex"]})
    expected = trades.growth_shop.values(state["player_state"], trades.growth_shop.product(GROWTH_PRODUCT),
                                         live.growth["GetIndex"], state=state)
    changed = without_note(values["values"])["changes"]
    assert {name: change["after"] for name, change in changed.items()} == dict(expected)


def test_live_eligibility_matches_the_service_hooks(live):
    bundle, state, read = live.bundle, live.state, live.console.read
    detail = bundle.unit_detail
    skins = read("tools.unit_skins", {"card_id": SKIN_CARD})["allowed_displays"]
    unlocked = detail.skin_catalog.allowed_displays(SKIN_CARD, state, smartphone_images=detail.smartphone_images)
    locked = detail.skin_catalog.allowed_displays(SKIN_CARD, bundle.possessions.debit(state, {SKIN_ITEM: 1}),
                                                  smartphone_images=detail.smartphone_images)
    assert skins == sorted(unlocked) and unlocked != locked
    catalog = bundle.backgrounds.catalog
    listing = read("tools.backgrounds", {"limit": MAX_PAGE_ITEMS})
    guide = catalog.guide_id(state["player_state"], state["tables"]["config"][0])
    assert listing["guide_card_id"] == guide and listing["definitions"]["total"] == len(catalog.definitions)
    for entry, definition in zip(listing["definitions"]["items"], catalog.definitions, strict=True):
        assert verdict(entry["selection"]) == outcome(lambda: catalog.selection(definition.name, guide, LIVE_NOW))
        if entry["selection"]["ok"]:
            assert entry["selection"]["card_ids"] == list(catalog.selection(definition.name, guide, LIVE_NOW))
    emblems = bundle.presents.emblem_catalog
    titles = read("tools.emblems", {"limit": MAX_PAGE_ITEMS})["titles"]["items"]
    for entry, title in zip(titles, emblems.titles, strict=True):
        assert verdict(entry["eligibility"]) == outcome(lambda: emblems.require_eligible(title.index, state))
    assert {entry["eligibility"]["ok"] for entry in titles} == {True, False}
    units = {row["GetIndex"]: row for row in state["player_state"]["units"]}
    fame = read("tools.fame")["units"]["items"]
    assert [entry["GetIndex"] for entry in fame] == sorted(units)
    for entry in fame:
        assert verdict(entry["eligibility"]) == outcome(
            lambda: bundle.fame_repository.catalog.require_eligible(units[entry["GetIndex"]]))
    capacity = read("tools.unit_capacity")
    usage = bundle.unit_capacity.usage(state)
    assert capacity["barracks"] == {"ok": True, "capacities": list(usage.capacities), "counts": list(usage.counts),
                                    "primary_available": bundle.unit_capacity.available(state)}
    assert capacity["unit_slots"] == {"ok": True, "available": bundle.player_state.available_unit_slots(state)}


def test_live_catalog_previews_match_the_service_hooks(live):
    bundle, state, read = live.bundle, live.state, live.console.read
    pools = bundle.summon_pools
    listing = read("tools.summon_pools", {"limit": 5})["pools"]
    assert [entry["pool_id"] for entry in listing["items"]] == sorted(pools.pools)[:5]
    assert listing["total"] == len(pools.pools)
    for entry in listing["items"]:
        pool = pools.get(entry["pool_id"])
        assert (entry["percent_sum"], entry["complete"], verdict(entry["drawable"])) == (
            pool.percent_sum, pool.complete, outcome(lambda: pools.require_complete(pool.pool_id)))
    incomplete = next(pool_id for pool_id, pool in sorted(pools.pools.items()) if not pool.complete)
    raw = read("tools.summon_pool", {"pool_id": incomplete})["candidates"]["items"]
    assert [entry["percent"] for entry in raw] == [candidate.percent for candidate in pools.get(incomplete).candidates]
    assert {entry["draw_probability"] for entry in raw} == {None}
    complete = read("tools.summon_pool", {"pool_id": COMPLETE_LIVE_POOL, "limit": MAX_PAGE_ITEMS})
    pool = pools.get(COMPLETE_LIVE_POOL)
    assert complete["candidates"]["total"] == len(pool.candidates) and complete["drawable"] == {"ok": True}
    assert [entry["draw_probability"] for entry in complete["candidates"]["items"]] == [
        candidate.weight / pool.total_weight for candidate in pool.candidates[:MAX_PAGE_ITEMS]]
    tickets = bundle.item_summon_tickets
    enabled = tickets.status()["enabled_items"][0]
    assert read("tools.item_ticket", {"item_id": enabled})["delivery"] == {"ok": True}
    disabled = read("tools.item_ticket", {"item_id": DISABLED_TICKET})
    assert verdict(disabled["delivery"]) == outcome(lambda: tickets.validate_item(DISABLED_TICKET))
    assert disabled["definition"] == {"ok": True, **asdict(tickets.catalog.definition(DISABLED_TICKET))}
    rewards = [{"kind": "gold", "amount": 100, "description": ""},
               {"kind": "summon_ticket", "pool_id": COMPLETE_LIVE_POOL, "count": 2, "description": ""}]
    preview = without_note(read("tools.presents", {"rewards": rewards}))
    existing = len(bundle.presents.rows(state))
    assert preview == {"existing": existing, "added": 3, "capacity": MAX_PRESENTS,
                       "rows": bundle.presents.preview(rewards, existing)}
    clock = without_note(read("tools.energy_clock")["conversion"])
    assert clock["seconds"] == energy_seconds(LIVE_NOW)
    assert {anchor["seconds"] for anchor in clock["decoded"].values()} == {clock["seconds"]}
    assert clock["current"] == anchors(state["tables"]["game_param"][0])

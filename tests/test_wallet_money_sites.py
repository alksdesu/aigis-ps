from collections.abc import Callable, Mapping
from copy import copy, deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import cache
import json
from pathlib import Path

import pytest

from aigis_ps.alxml import Column
from aigis_ps.crystal_wallet import (FREE_CRYSTAL_LIMIT, PAID_CRYSTAL_LIMIT, TOTAL_CRYSTAL_LIMIT,
                                     claim_crystal_space, claim_crystals_fit, free_crystals_fit)
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.orb_exchange import ORB_CATEGORY, OrbExchangeService
from aigis_ps.possessions import PossessionService
from aigis_ps.present_items import FIELDS as ITEM_FIELDS
from aigis_ps.presents import TEMPLATE_CARDS, PresentService
from aigis_ps.trade_history import TradeRules
from aigis_ps.trade_service import TradeService
from aigis_ps.wallet_money import GOLD_LIMIT
from test_admin_seams import MemoryStore
from test_present_gold import gold
from test_present_items import components, item_table, row as item_row
from test_presents import DAY, asset_root, assets, claim_request, crystal, growth_setup, presents, queue, state, unit
from test_trade_catalog import catalog as trade_catalog, name_hash, product, resources, table
from test_unit_actions import request


GOLDEN = Path(__file__).resolve().parent / "golden" / "p24"
BATCH_CLAIM = 117
ORB_EXCHANGE = 139
ORBS = ("evolve_orb_100001", "evolve_orb_100002")
ORB_PRICE, ORB_REQUIRED, ORB_CREATED, ORB_STACK = 15000, 5, 1, 999
ORB_FIELDS = {"ItemID": 1, "ItemName": 32, "Category": 1, "Price": 1, "AmountOfRequire": 1, "AmountOfCreate": 1}
COIN, TRADE_ITEM = "coin", "reward"
COIN_STACK, TRADE_ITEM_STACK = 10000, 99
PACK_TICKET, PACK_RECEIVE_ACTION = "pack_ticket", 3
EXTRA_TEMPLATE = max(TEMPLATE_CARDS) + 1
MONEY_SHOP, CRYSTAL_SHOP, COIN_SHOP = 1, 2, 3
MONEY_TRADE, CRYSTAL_TRADE, COIN_TRADE = 7, 8, 4
MONEY_PRICE, CRYSTAL_PRICE, COIN_PRICE = 100, 10, 50
STARTING_MONEY = 100_000
STOCK = ((COIN, 1000), (ORBS[0], 10), (ORBS[1], 10))
TRADE_NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
# The RankUp fixture from test_bootstrap gives rank 1 the experience range 0..15.
RANK_ONE_EXPERIENCE, OUT_OF_RANK_EXPERIENCE = 5, 100
NATIVE_REFUSALS = frozenset(("present_gold_capacity", "present_crystal_capacity", "orb_exchange_money_insufficient",
                             "orb_exchange_native_balance", "trade_currency_insufficient", "trade_native_balance"))


@dataclass(frozen=True)
class Save:
    money: object = None
    experience: int | None = None
    crystals: tuple[tuple[bool, int], ...] | None = None
    stock: tuple[tuple[str, int], ...] | None = None

    def apply(self, store: MemoryStore) -> None:
        profile = store.profile()
        saved = profile.state
        game = saved["tables"]["game_param"][0]
        if self.money is not None:
            game["Money"] = self.money
        if self.experience is not None:
            game["Experience"] = self.experience
        if self.crystals is not None:
            saved["tables"]["crystals"] = [{"FreeFlag": free, "Num": amount, "Date": DAY} for free, amount in self.crystals]
        if self.stock is not None:
            saved["tables"]["possessions"] = [{"ItemID": name, "Count": count} for name, count in self.stock]
        store.update_state(saved, profile.revision)


@dataclass(frozen=True)
class ClaimCase:
    rewards: tuple[dict, ...]
    save: Save = Save()
    number: int = 1
    order: tuple[int, ...] | None = None
    replay: bool = False


@dataclass(frozen=True)
class ExchangeCase:
    save: Save = Save()
    item: str = ORBS[0]
    base: str = ORBS[1]
    replay: bool = False


@dataclass(frozen=True)
class TradeCase:
    shop: int
    save: Save = Save()
    quantity: int = 1
    replay: bool = False


@dataclass(frozen=True)
class MoneySites:
    presents: PresentService
    exchange: OrbExchangeService
    trades: TradeService
    state: dict


class ItemOnlyGrants:
    content_version = "b" * 64

    def grant(self, state, product, count):
        raise AssertionError("These trades only sell items")


def item_payload() -> bytes:
    return item_table([item_row(COIN, Stack=COIN_STACK), item_row(TRADE_ITEM, Stack=TRADE_ITEM_STACK),
                       *(item_row(name, Stack=ORB_STACK, Category=ORB_CATEGORY) for name in ORBS)])


def orb_payload() -> bytes:
    return table([{"ItemID": name_hash(name), "ItemName": name, "Category": 0, "Price": ORB_PRICE,
                   "AmountOfRequire": ORB_REQUIRED, "AmountOfCreate": ORB_CREATED} for name in ORBS], ORB_FIELDS)


def shop_payloads() -> dict[str, bytes]:
    shops = [product(shop, Type=trade, ProductType=3, CardID=0, ItemID=TRADE_ITEM, RequiredValue=price)
             for shop, trade, price in ((MONEY_SHOP, MONEY_TRADE, MONEY_PRICE), (CRYSTAL_SHOP, CRYSTAL_TRADE, CRYSTAL_PRICE),
                                        (COIN_SHOP, COIN_TRADE, COIN_PRICE))]
    return {**resources(shops), "ItemConfigTable.atb": item_payload(), "ItemOrbExchangeConfig.atb": orb_payload()}


def money_sites(presents: PresentService, state: dict) -> MoneySites:
    payloads = shop_payloads()
    possessions = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    bootstrap = copy(presents.bootstrap)
    bootstrap.possession_provider = possessions.startup_response
    exchange = OrbExchangeService(bootstrap, presents.players, possessions, payloads.__getitem__)
    trades = TradeService(bootstrap, presents.players, possessions, trade_catalog(payloads, possessions=possessions),
                          grants=ItemOnlyGrants(), rules=TradeRules(), clock=lambda: TRADE_NOW)
    base = deepcopy(state)
    base["tables"]["game_param"][0]["Money"] = STARTING_MONEY
    base["tables"]["possessions"] = [{"ItemID": name, "Count": count} for name, count in STOCK]
    return MoneySites(presents, exchange, trades, base)


def snapshot(state: dict) -> dict:
    tables = state["tables"]
    game = tables["game_param"][0]
    return {"money": game["Money"], "experience": game["Experience"], "crystals": tables["crystals"],
            "possessions": tables["possessions"], "presents": [row["PresentID"] for row in tables["presents"]],
            "units": len(state["player_state"]["units"])}


def attempt(call: Callable[[], bytes], store: MemoryStore) -> dict:
    before = store.profile()
    try:
        result = {"response": call().decode("utf-8")}
    except ProtocolError as error:
        result = {"error": {"code": error.code, "message": str(error), "status": error.status}}
    after = store.profile()
    return {**result, "revisions": after.revision - before.revision, "save": snapshot(after.state)}


def outcome(call: Callable[[], bytes], store: MemoryStore, replay: bool) -> dict:
    first = attempt(call, store)
    return {**first, "replay": attempt(call, store)} if replay else first


def claim_outcome(sites: MoneySites, case: ClaimCase) -> dict:
    store = MemoryStore(sites.state)
    identifiers = queue(sites.presents, store, list(case.rewards))
    case.save.apply(store)
    selected = identifiers if case.order is None else [identifiers[index] for index in case.order]
    envelope = claim_request(store, selected, case.number)
    return outcome(lambda: sites.presents.receive(envelope, store), store, case.replay)


def exchange_outcome(sites: MoneySites, case: ExchangeCase) -> dict:
    store = MemoryStore(sites.state)
    case.save.apply(store)
    envelope = request(store, ORB_EXCHANGE, (Column("ITEM", "S", (case.item,)), Column("BASE", "S", (case.base,))))
    return outcome(lambda: sites.exchange.exchange(envelope, store), store, case.replay)


def trade_outcome(sites: MoneySites, case: TradeCase) -> dict:
    store = MemoryStore(sites.state)
    case.save.apply(store)
    registration = sites.trades.catalog.product(case.shop).registration
    envelope = request(store, registration, (Column("SHOP", "I", (case.shop,)), Column("NUM", "I", (case.quantity,))))
    return outcome(lambda: sites.trades.purchase(envelope, store), store, case.replay)


CLAIM_CASES = {
    "gold": ClaimCase((gold(10),), Save(money=GOLD_LIMIT - 10), replay=True),
    "gold_two_mails_reach_the_cap": ClaimCase((gold(4), gold(6)), Save(money=GOLD_LIMIT - 10)),
    "gold_batch": ClaimCase((gold(10),), Save(money=GOLD_LIMIT - 10), number=BATCH_CLAIM, replay=True),
    "gold_over_cap": ClaimCase((gold(1),), Save(money=GOLD_LIMIT)),
    "gold_two_over_cap": ClaimCase((gold(2),), Save(money=GOLD_LIMIT - 1)),
    "gold_balance_above_cap": ClaimCase((gold(1),), Save(money=GOLD_LIMIT + 1)),
    "gold_int32_mail": ClaimCase((gold(INT32_MAX),), Save(money=1)),
    "gold_mails_beyond_int32": ClaimCase((gold(INT32_MAX), gold(INT32_MAX)), Save(money=1)),
    "gold_mixed_mail_over_cap": ClaimCase((unit(), crystal(), gold(1)), Save(money=GOLD_LIMIT)),
    "gold_batch_request_order": ClaimCase((gold(3), gold(2), gold(1)), Save(money=GOLD_LIMIT - 3),
                                          number=BATCH_CLAIM, order=(2, 1, 0)),
    "gold_batch_balance_above_cap": ClaimCase((gold(1),), Save(money=GOLD_LIMIT + 5), number=BATCH_CLAIM, replay=True),
    "gold_batch_full_wallet_claims_crystals": ClaimCase((gold(1), crystal(2)), Save(money=GOLD_LIMIT), number=BATCH_CLAIM),
    "gold_batch_skips_int32_mail": ClaimCase((gold(INT32_MAX), gold(5)), Save(money=1), number=BATCH_CLAIM),
    "gold_negative_balance": ClaimCase((gold(1),), Save(money=-1)),
    "gold_boolean_balance": ClaimCase((gold(1),), Save(money=True)),
    "gold_text_balance": ClaimCase((gold(1),), Save(money="100")),
    "gold_balance_beyond_int32": ClaimCase((gold(1),), Save(money=INT32_MAX + 1)),
    "gold_new_account_balance": ClaimCase((gold(1),), Save(money=0)),
    "gold_rank_mismatch": ClaimCase((gold(1),), Save(experience=OUT_OF_RANK_EXPERIENCE)),
    "gold_batch_negative_balance": ClaimCase((gold(1),), Save(money=-1), number=BATCH_CLAIM),
    "crystal": ClaimCase((crystal(3),), replay=True),
    "crystal_batch": ClaimCase((crystal(3),), number=BATCH_CLAIM, replay=True),
    "crystal_paid_over_cap": ClaimCase((crystal(1),), Save(crystals=((False, PAID_CRYSTAL_LIMIT + 1),))),
    "crystal_free_at_cap": ClaimCase((crystal(1),), Save(crystals=((False, PAID_CRYSTAL_LIMIT), (True, FREE_CRYSTAL_LIMIT)))),
    "crystal_free_sum_over_cap": ClaimCase((crystal(2),), Save(crystals=((False, 1), (True, FREE_CRYSTAL_LIMIT - 1)))),
    "crystal_exact_capacity": ClaimCase((crystal(1),), Save(crystals=((False, PAID_CRYSTAL_LIMIT),
                                                                      (True, FREE_CRYSTAL_LIMIT - 1)))),
    "crystal_mails_beyond_int32": ClaimCase((crystal(INT32_MAX), crystal(INT32_MAX))),
    "crystal_checked_before_gold": ClaimCase((unit(), crystal(1), gold(1)),
                                             Save(money=GOLD_LIMIT, crystals=((True, FREE_CRYSTAL_LIMIT),))),
    "crystal_batch_request_order": ClaimCase((crystal(3), crystal(4), crystal(2)),
                                             Save(crystals=((True, FREE_CRYSTAL_LIMIT - 5),)), number=BATCH_CLAIM),
    "crystal_batch_paid_over_cap": ClaimCase((crystal(1),), Save(crystals=((False, PAID_CRYSTAL_LIMIT + 1),)),
                                             number=BATCH_CLAIM),
    "crystal_batch_skips_int32_mail": ClaimCase((crystal(5), crystal(INT32_MAX)), number=BATCH_CLAIM),
    "crystal_duplicate_bucket": ClaimCase((crystal(1),), Save(crystals=((True, 1), (True, 2)))),
    "crystal_batch_duplicate_bucket": ClaimCase((crystal(1),), Save(crystals=((True, 1), (True, 2))), number=BATCH_CLAIM),
    "crystal_and_gold_batch": ClaimCase((crystal(2), gold(3)), Save(money=GOLD_LIMIT - 3), number=BATCH_CLAIM),
}
EXCHANGE_CASES = {
    "exchange": ExchangeCase(replay=True),
    "exchange_spends_all_money_with_experience": ExchangeCase(Save(money=ORB_PRICE, experience=RANK_ONE_EXPERIENCE)),
    "exchange_money_insufficient": ExchangeCase(Save(money=ORB_PRICE - 1)),
    "exchange_money_before_material": ExchangeCase(Save(money=ORB_PRICE - 1, stock=((ORBS[0], 10), (ORBS[1], 0)))),
    "exchange_native_balance": ExchangeCase(Save(money=ORB_PRICE)),
    "exchange_material_insufficient": ExchangeCase(Save(stock=((ORBS[0], 10), (ORBS[1], ORB_REQUIRED - 1)))),
    "exchange_stack_exceeded": ExchangeCase(Save(stock=((ORBS[0], ORB_STACK), (ORBS[1], 10)))),
    "exchange_same_item": ExchangeCase(base=ORBS[0]),
    "exchange_unknown_item": ExchangeCase(item="unknown"),
    "exchange_negative_money": ExchangeCase(Save(money=-1)),
    "exchange_rank_mismatch": ExchangeCase(Save(experience=OUT_OF_RANK_EXPERIENCE)),
}
TRADE_CASES = {
    "money_trade": TradeCase(MONEY_SHOP, replay=True),
    "money_trade_quantity": TradeCase(MONEY_SHOP, quantity=3),
    "money_trade_spends_all_money_with_experience": TradeCase(MONEY_SHOP, Save(money=MONEY_PRICE,
                                                                                experience=RANK_ONE_EXPERIENCE)),
    "money_trade_insufficient": TradeCase(MONEY_SHOP, Save(money=MONEY_PRICE - 1)),
    "money_trade_native_balance": TradeCase(MONEY_SHOP, Save(money=MONEY_PRICE)),
    "money_trade_negative_money": TradeCase(MONEY_SHOP, Save(money=-1)),
    "money_trade_rank_mismatch": TradeCase(MONEY_SHOP, Save(experience=OUT_OF_RANK_EXPERIENCE)),
    "crystal_trade": TradeCase(CRYSTAL_SHOP, Save(crystals=((True, 5), (False, 100))), replay=True),
    "crystal_trade_insufficient": TradeCase(CRYSTAL_SHOP, Save(crystals=((True, CRYSTAL_PRICE - 1),))),
    "coin_trade": TradeCase(COIN_SHOP, replay=True),
    "coin_trade_insufficient": TradeCase(COIN_SHOP, Save(stock=((COIN, COIN_PRICE - 1),))),
}
SUITES = {"present-claims": (CLAIM_CASES, claim_outcome), "orb-exchange": (EXCHANGE_CASES, exchange_outcome),
          "trades": (TRADE_CASES, trade_outcome)}


@cache
def recorded(suite: str) -> dict:
    return json.loads((GOLDEN / f"{suite}.json").read_bytes())["cases"]


def canonical(value: object) -> str:
    # Plain equality treats True as 1, so saved balances are compared through JSON text.
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def crystal_state(paid: int, free: int) -> dict:
    return {"bootstrap_schema": 1, "tables": {"crystals": [{"FreeFlag": False, "Num": paid, "Date": DAY},
                                                          {"FreeFlag": True, "Num": free, "Date": DAY}]}}


@pytest.fixture
def sites(presents, state):
    return money_sites(presents, state)


def test_golden_covers_every_case_and_native_refusal():
    for suite, (cases, _) in SUITES.items():
        assert recorded(suite).keys() == cases.keys()
    codes = {result["error"]["code"] for suite in SUITES for result in recorded(suite).values() if "error" in result}
    assert NATIVE_REFUSALS <= codes


@pytest.mark.parametrize("suite,name", [(suite, name) for suite, (cases, _) in SUITES.items() for name in cases])
def test_money_sites_match_the_recorded_golden(sites, suite, name):
    cases, run = SUITES[suite]
    assert canonical(run(sites, cases[name])) == canonical(recorded(suite)[name])


@pytest.mark.parametrize("suite", ["orb-exchange", "trades"])
def test_exchange_and_trade_payments_never_raise_money(suite):
    cases, _ = SUITES[suite]
    paid = {name: result for name, result in recorded(suite).items() if "response" in result and result["revisions"]}
    assert paid
    for name, result in paid.items():
        saved = cases[name].save.money
        assert result["save"]["money"] <= (STARTING_MONEY if saved is None else saved)


@pytest.mark.parametrize("paid,free,amount,fits", [
    (PAID_CRYSTAL_LIMIT, FREE_CRYSTAL_LIMIT - 1, 1, True), (0, 0, FREE_CRYSTAL_LIMIT, True),
    (PAID_CRYSTAL_LIMIT + 1, 0, 1, False), (0, FREE_CRYSTAL_LIMIT, 1, False), (1, FREE_CRYSTAL_LIMIT - 1, 2, False),
    (0, 0, FREE_CRYSTAL_LIMIT + 1, False), (0, 0, INT32_MAX + 1, False)])
def test_claim_capacity_requires_every_wallet_cap(paid, free, amount, fits):
    wallet = crystal_state(paid, free)
    assert claim_crystals_fit(wallet, amount) is fits
    assert (amount <= claim_crystal_space(wallet)) is fits


@pytest.mark.parametrize("paid,free,space", [(0, 0, FREE_CRYSTAL_LIMIT), (PAID_CRYSTAL_LIMIT, 600, 400),
                                             (0, FREE_CRYSTAL_LIMIT + 500, 0), (TOTAL_CRYSTAL_LIMIT, 0, 0)])
def test_claim_space_never_turns_negative_for_balances_above_a_cap(paid, free, space):
    assert claim_crystal_space(crystal_state(paid, free)) == space


def test_claim_capacity_is_stricter_than_the_settlement_choice():
    wallet = crystal_state(PAID_CRYSTAL_LIMIT + 1, 0)
    assert free_crystals_fit(wallet, 1) and not claim_crystals_fit(wallet, 1)


@pytest.mark.parametrize("amount", [True, -1, 1.5, "1", None])
def test_claim_capacity_rejects_non_integer_amounts(amount):
    with pytest.raises(ProtocolError) as caught:
        claim_crystals_fit(crystal_state(0, 0), amount)
    assert caught.value.code == "invalid_crystal_credit"


@pytest.mark.parametrize("check", [lambda wallet: claim_crystals_fit(wallet, 1), claim_crystal_space])
def test_claim_capacity_requires_a_valid_wallet(check):
    wallet = crystal_state(0, 0)
    wallet["tables"]["crystals"].append({"FreeFlag": True, "Num": 1, "Date": DAY})
    with pytest.raises(ProtocolError) as caught:
        check(wallet)
    assert caught.value.code == "invalid_crystal_state"


def test_present_status_reports_the_shared_wallet_limits(presents):
    status = presents.status()
    assert status["wallet_limits"] == {"paid": 1000, "free": 1000, "total": 2000}
    assert status["gold_limit"] == GOLD_LIMIT == 99_999_999


def test_present_templates_are_a_read_only_view_with_unchanged_digests(presents):
    view = presents.templates
    assert isinstance(view, Mapping) and view == TEMPLATE_CARDS
    assert json.dumps(dict(view), sort_keys=True) == json.dumps(presents._templates, sort_keys=True)
    with pytest.raises(TypeError):
        view[2] = TEMPLATE_CARDS[3]


def test_present_templates_follow_replaced_templates(presents):
    assert EXTRA_TEMPLATE not in presents.templates
    presents._templates = {**TEMPLATE_CARDS, EXTRA_TEMPLATE: TEMPLATE_CARDS[2]}
    assert presents.templates[EXTRA_TEMPLATE] == TEMPLATE_CARDS[2]


def test_present_item_rows_are_the_verified_read_only_definitions():
    catalog = components([item_row(), item_row(COIN, Stack=COIN_STACK, EffectType=6),
                          item_row(PACK_TICKET, ReceiveAction=PACK_RECEIVE_ACTION)]).catalog
    rows = catalog.rows
    assert set(rows) == {"gift_0002_ruby", COIN, PACK_TICKET} and all(set(row) == set(ITEM_FIELDS) for row in rows.values())
    assert (rows[COIN]["Stack"], rows[COIN]["EffectType"]) == (COIN_STACK, 6)
    assert rows[PACK_TICKET]["ReceiveAction"] == PACK_RECEIVE_ACTION and catalog.status()["item_count"] == 2
    with pytest.raises(TypeError):
        rows[COIN]["Stack"] = 1
    with pytest.raises(TypeError):
        rows["unknown"] = rows[COIN]

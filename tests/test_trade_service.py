from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from operator import attrgetter
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, Limits, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.request_ledger import LEGACY_SCHEMA_VERSION, PROCESSED_LIMIT, SCHEMA_VERSION
from aigis_ps.storage import Store
from aigis_ps.trade_catalog import SHOP_RESOURCE
from aigis_ps.trade_history import TradeRules
from aigis_ps.trade_service import HISTORY_LIMIT, TradeService
from test_quest_service import bundle, request
from test_trade_catalog import native_catalog


class Grants:
    content_version = "b" * 64

    def __init__(self, players):
        self.players, self.calls = players, []

    def grant(self, state, product, count):
        self.calls.append((product.shop_id, count))
        self.players.require_unit_space(state, count)
        return self.players.credit_units(state, [{"card_id": product.card_id, "level": 1, "count": count}])


@pytest.fixture
def case(bundle, native_catalog, tmp_path):
    catalog = copy(native_catalog)
    base = next(product for product in catalog.products if product.ordinary and product.product_type == 1)
    products = {}
    definitions = ((1, 104, "possession", "trade_item_001000", 1),
        (2, 105, "possession", "trade_item_002000", 1),
        (4, 107, "possession", "trade_item_003000", 3),
        (5, 109, "possession", "gift_0003_diamond", 3),
        (6, 110, "possession", "gift_1003_millenniumWine", 3),
        (7, 111, "money", None, 3), (8, 112, "crystals", None, 1),
        (9, 113, "possession", "trade_item_003000", 3))
    for offset, (kind, registration, currency_kind, item, product_type) in enumerate(definitions, 1):
        identifier = 100000 + offset
        products[identifier] = replace(base, shop_id=identifier, product_type=product_type, trade_type=kind,
            shop_type=0, card_id=7 if product_type == 1 else 0, item_id="" if product_type == 1 else "gift_0002_ruby",
            adding_count=1 if product_type == 1 else 2, data_restrict=100, reset_type=0, tiers=((1, 10),),
            start_at=None, end_at=None, disclosure_quest_ids=(), buy_condition_item_id="", single_buy_only=False,
            unit_base_level=0, unit_cost_modify=0, unit_evolution_count=0, unit_skill_level=0,
            unit_skill_evolution=0, unit_init_trust=0, currency_kind=currency_kind, currency_item_id=item,
            registration=registration)
    products[100009] = replace(products[100003], shop_id=100009, reset_type=1, data_restrict=1)
    products[100010] = replace(products[100003], shop_id=100010, reset_type=2, data_restrict=1)
    products[100011] = replace(products[100003], shop_id=100011, data_restrict=0)
    catalog._products = MappingProxyType(products)
    catalog._discounts = MappingProxyType({})
    catalog.content_version = "a" * 64
    clock = SimpleNamespace(now=datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: clock.now
    saved = bundle.player_state.initialize(bootstrap.initial_state())
    saved["tables"]["game_param"][0]["Money"] = 1000
    saved["tables"]["possessions"] = [{"ItemID": item, "Count": 1000} for item in (
        "trade_item_001000", "trade_item_002000", "trade_item_003000", "gift_0003_diamond", "gift_1003_millenniumWine")]
    saved["tables"]["crystals"] = [{"FreeFlag": True, "Num": 5, "Date": 700000},
        {"FreeFlag": False, "Num": 100, "Date": 700001}]
    saved["retained_progress"] = {"completed": [1, 2, 3]}
    grants = Grants(bundle.player_state)
    service = TradeService(bootstrap, bundle.player_state, bundle.possessions, catalog, grants=grants,
                           rules=TradeRules("month", "window"), clock=lambda: clock.now)
    bootstrap.exchange_history_provider = service.history_response
    path = tmp_path / "trades.sqlite3"
    return SimpleNamespace(bundle=bundle, catalog=catalog, service=service, grants=grants, clock=clock,
                           store=Store(path, saved), path=path)


def message(case, shop=100003, quantity=1, *, tick=1000, number=None, columns=None):
    if number is None:
        number = case.catalog.product(shop).registration
    return request(case.store, number, (Column("SHOP", "I", (shop,)), Column("NUM", "I", (quantity,)))
                   if columns is None else columns, tick=tick)


def mutate(case, action):
    profile = case.store.profile()
    candidate = deepcopy(profile.state)
    action(candidate)
    case.store.update_state(candidate, profile.revision)


def overwrite(case, action):
    state = case.store.profile().state
    action(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def legacy_receipts(state):
    history = state["trade_receipts"]
    receipts = [{**entry, "client_time": 5000} for entry in history["receipts"]]
    state["trade_receipts"] = {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": receipts,
                               "last_client_time": 5000, "seen_keys": [receipts[-1]["request"]]}


def fail(case, envelope, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.purchase(envelope, case.store)
    assert caught.value.code == code
    assert case.store.profile() == before


def nodes(xml):
    return {node.name: node for node in loads(xml).children}


def counts(state):
    return {row["ItemID"]: row["Count"] for row in state["tables"]["possessions"]}


def full_service(case):
    bundle, trades = case.bundle, case.bundle.trades
    return TradeService(case.service.bootstrap, bundle.player_state, bundle.possessions, trades.catalog,
                        grants=case.grants, rules=case.service.rules, clock=lambda: case.clock.now,
                        silver=trades.silver, gifts=bundle.gifts, evolution_rewards=bundle.evolution_rewards,
                        growth_shop=trades.growth_shop)


def identities(service):
    return service._content_version(), service._content_version(106), service._content_version(113, grow=True)


def resources_before_upgrade():
    return {"fixture-before-upgrade": {SHOP_RESOURCE: "e" * 64}}


def commit_before_upgrade(case, envelope):
    [(version, changed)] = resources_before_upgrade().items()
    case.catalog.content_version = case.catalog.content_version_for(version, changed)
    response = case.service.purchase(envelope, case.store)
    catalog = copy(case.catalog)
    catalog.content_version = catalog.content_version_for(catalog.resource_version, {})
    catalog._earlier_resources, catalog._replayable = resources_before_upgrade, None
    upgraded = TradeService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions, catalog,
                            grants=case.grants, rules=case.service.rules, clock=lambda: case.clock.now)
    return response, upgraded


def test_status_shared_handler_and_pure_initialize(case):
    handlers = case.service.handlers()
    assert set(handlers) == {104, 105, 107, 109, 110, 111, 112, 113}
    assert len({id(handler) for handler in handlers.values()}) == 1
    assert case.service.status()["ready"] and case.service.status()["unconfigured_resets"] == []
    saved = case.store.profile().state
    saved["tables"]["exchange_history"] = [{"ShopItemID": 100003, "ExchangeCount": 2},
        {"ShopItemID": 200000, "ExchangeCount": 8}]
    original = deepcopy(saved)
    updated = case.service.initialize(saved)
    assert saved == original and updated is not saved
    assert updated["trade_history"]["products"]["200000"]["count"] == 8
    response = case.service.history_response(updated)
    assert response.children == (Column("ShopItemID", "I", (100003, 200000)), Column("ExchangeCount", "I", (2, 8)))


def test_readiness_checks_never_build_a_dependency_status(case, monkeypatch):
    expected = identities(full_service(case))
    bundle = case.bundle
    for provider in (bundle.possessions, bundle.trades.catalog, bundle.trades.silver, bundle.trades.growth_shop,
                     bundle.gifts, bundle.evolution_rewards):
        monkeypatch.setattr(provider, "status", lambda: pytest.fail("Trade readiness built a full status"))
    assert identities(full_service(case)) == expected


@pytest.mark.parametrize("name,code", [("possessions", "trade_dependencies_invalid"),
                                       ("trades.catalog", "trade_dependencies_invalid"),
                                       ("trades.silver", "silver_trade_unavailable"),
                                       ("trades.growth_shop", "shop_growth_unavailable")])
def test_each_unready_dependency_refuses_the_service(case, monkeypatch, name, code):
    monkeypatch.setattr(attrgetter(name)(case.bundle), "is_ready", lambda: False)
    with pytest.raises(ProtocolError) as caught:
        full_service(case)
    assert (caught.value.code, caught.value.status) == (code, 503)


@pytest.mark.parametrize("shop", list(range(100001, 100009)))
def test_all_eight_operations_pay_grant_and_record_atomically(case, shop):
    before = case.store.profile()
    product = case.catalog.product(shop)
    response = nodes(case.service.purchase(message(case, shop), case.store))
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert after.state["retained_progress"] == before.state["retained_progress"]
    assert after.state["trade_history"]["products"][str(shop)]["count"] == 1
    assert after.state["trade_history"]["products"][str(shop)]["lifetime_count"] == 1
    assert len(after.state["trade_receipts"]["receipts"]) == 1
    assert response["ExchangeHistory"].children == (Column("ShopItemID", "I", (shop,)), Column("ExchangeCount", "I", (1,)))
    if product.currency_kind == "possession":
        assert counts(after.state)[product.currency_item_id] == counts(before.state)[product.currency_item_id] - 10
    elif product.currency_kind == "money":
        assert after.state["tables"]["game_param"][0]["Money"] == 990
        assert response["PGP"].children == (Column("A1", "I", (990,)),)
    else:
        assert [row["Num"] for row in after.state["tables"]["crystals"]] == [0, 95]
        assert response["PFC"].children[-1] == Column("Num", "I", (0,))
        assert response["PPC"].children[-1] == Column("Num", "I", (95,))
    if product.product_type == 1:
        original_count = len(before.state["player_state"]["units"])
        new_units = after.state["player_state"]["units"][original_count:]
        assert case.grants.calls == [(shop, 1)] and len(new_units) == 1
        assert response["PBP"].children == (Column("A1", "S", ("UX",)),
            Column("A2", "I", (after.state["player_state"]["battle"]["UX"],)))
        assert response["PPU"].children == tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I",
            tuple(unit[name] for unit in new_units)) for name, alias in UNIT_FIELDS)
    else:
        assert counts(after.state)[product.item_id] == 2
        assert case.grants.calls == [] and "PBP" not in response
        assert after.state["player_state"] == before.state["player_state"]
        assert len(response["PPU"].children) == 21 and all(not column.values for column in response["PPU"].children)


def test_payment_and_reward_same_item_are_merged_to_final_absolute_count(case):
    product = replace(case.catalog.product(100003), item_id="trade_item_003000")
    case.catalog._products = MappingProxyType({**case.catalog._products, 100003: product})
    response = nodes(case.service.purchase(message(case), case.store))
    assert response["POSSESSION"].children == (Column("ItemID", "S", ("trade_item_003000",)), Column("Count", "I", (992,)),
                                               Column("Enabled", "I", (0,)))


def test_grant_receives_actual_amount_once(case):
    product = replace(case.catalog.product(100001), adding_count=2)
    case.catalog._products = MappingProxyType({**case.catalog._products, 100001: product})
    before = len(case.store.profile().state["player_state"]["units"])
    case.service.purchase(message(case, 100001, quantity=3), case.store)
    assert case.grants.calls == [(100001, 6)]
    assert len(case.store.profile().state["player_state"]["units"]) == before + 6


def test_unlimited_native_count_zero_with_real_lifetime_accumulation(case):
    case.service.purchase(message(case, 100011, quantity=2), case.store)
    response = nodes(case.service.purchase(message(case, 100011, quantity=3, tick=1001), case.store))
    record = case.store.profile().state["trade_history"]["products"]["100011"]
    assert record["lifetime_count"] == 5 and record["count"] == 5
    assert response["ExchangeHistory"].children[-1] == Column("ExchangeCount", "I", (0,))


def test_unconfigured_reset_blocks_purchase_but_keeps_history_readable(case):
    case.service = TradeService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions,
        case.catalog, grants=case.grants, rules=TradeRules("unconfigured", "unconfigured"),
        clock=lambda: case.clock.now)
    for shop in (100009, 100010):
        fail(case, message(case, shop), "trade_reset_unconfigured")
    mutate(case, lambda state: state["tables"].update(exchange_history=[{"ShopItemID": 100009, "ExchangeCount": 1}]))
    assert case.service.history_response(case.store.profile().state).children[-1] == Column("ExchangeCount", "I", (1,))
    fail(case, message(case, 100009), "trade_reset_unconfigured")
    assert case.grants.calls == []


def test_month_change_returns_zero_for_old_product_and_preserves_lifetime(case):
    case.clock.now = datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc)
    old = message(case, 100009)
    case.service.purchase(old, case.store)
    case.clock.now += timedelta(minutes=2)
    response = nodes(case.service.purchase(message(case, 100003, tick=1001), case.store))
    assert response["ExchangeHistory"].children == (Column("ShopItemID", "I", (100003, 100009)),
        Column("ExchangeCount", "I", (1, 0)))
    case.service.purchase(message(case, 100009, tick=1002), case.store)
    assert case.store.profile().state["trade_history"]["products"]["100009"]["lifetime_count"] == 2
    fail(case, old, "trade_retry_stale")


def test_exact_old_request_replays_after_month_change_without_new_charge(case):
    case.clock.now = datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc)
    envelope = message(case, 100009)
    response = case.service.purchase(envelope, case.store)
    committed = case.store.profile()
    case.clock.now += timedelta(days=1)
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == committed


def test_unknown_old_products_survive_new_purchase_and_full_history_response(case):
    mutate(case, lambda state: state["tables"].update(exchange_history=[{"ShopItemID": 200000, "ExchangeCount": 5}]))
    response = nodes(case.service.purchase(message(case), case.store))
    assert response["ExchangeHistory"].children == (Column("ShopItemID", "I", (100003, 200000)),
        Column("ExchangeCount", "I", (1, 5)))


def test_zero_money_and_zero_experience_guard(case):
    mutate(case, lambda state: state["tables"]["game_param"][0].update(Money=10, Experience=0))
    fail(case, message(case, 100006), "trade_native_balance")


def test_insufficient_currency_and_item_capacity_leave_history_untouched(case):
    mutate(case, lambda state: state["tables"].update(possessions=[]))
    fail(case, message(case, 100001), "trade_currency_insufficient")
    assert case.grants.calls == []
    mutate(case, lambda state: state["tables"].update(possessions=[
        {"ItemID": "trade_item_003000", "Count": 100},
        {"ItemID": "gift_0002_ruby", "Count": case.bundle.possessions.stack("gift_0002_ruby")}]))
    fail(case, message(case), "trade_item_capacity")


def test_failing_grant_rolls_back_payment_and_purchase_count(case, monkeypatch):
    def failure(state, product, count):
        state["tables"]["game_param"][0]["Money"] = 1
        raise ProtocolError("fixture_grant", "grant unavailable", 503)

    monkeypatch.setattr(case.grants, "grant", failure)
    fail(case, message(case, 100001), "fixture_grant")


def test_bad_grant_result_is_not_saved(case, monkeypatch):
    monkeypatch.setattr(case.grants, "grant", lambda state, product, count: (state, []))
    fail(case, message(case, 100001), "trade_grant_result_invalid")


@pytest.mark.parametrize("change", [
    lambda state: state["tables"]["game_param"][0].update(Money=999999),
    lambda state: state["tables"].update(possessions=[]),
    lambda state: state["trade_history"]["products"]["100001"].update(count=0),
    lambda state: state.pop("retained_progress"),
])
def test_unit_helper_cannot_change_payment_history_or_other_state(case, monkeypatch, change):
    original = case.grants.grant

    def broken(state, product, count):
        updated, units = original(state, product, count)
        change(updated)
        return updated, units

    monkeypatch.setattr(case.grants, "grant", broken)
    fail(case, message(case, 100001), "trade_grant_result_invalid")


@pytest.mark.parametrize("columns", [(), (Column("SHOP", "I", (100003,)),),
    (Column("SHOP", "S", ("100003",)), Column("NUM", "I", (1,))),
    (Column("SHOP", "I", (100003,)), Column("NUM", "I", (1, 2))),
    (Column("SHOP", "I", (100003,)), Column("NUM", "B", (True,))),
    (Column("SHOP", "I", (100003,)), Column("NUM", "I", (0,))),
    (Column("SHOP", "I", (-1,)), Column("NUM", "I", (1,))),
    (Column("SHOP", "I", (100003,)), Column("NUM", "I", (1,)), Column("PRICE", "I", (0,))),
    (Container("SHOP", (Column("ID", "I", (100003,)),)), Column("NUM", "I", (1,)))])
def test_request_shape_is_exact(case, columns):
    fail(case, message(case, columns=columns), "invalid_request_fields")


def test_operation_must_match_product_currency(case):
    fail(case, message(case, 100003, number=104), "trade_registration_mismatch")


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_credentials_are_checked_before_trade(case, field):
    envelope = message(case)
    envelope.document.find(f"{field}/V").text = "other"
    fail(case, envelope, "invalid_method_key" if field == "MK" else "invalid_identity")


def test_exact_retry_after_reopen_does_not_grant_twice(case):
    envelope = message(case, 100001)
    response = case.service.purchase(envelope, case.store)
    committed = case.store.profile()
    case.store = Store(case.path)
    service = TradeService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions,
        case.catalog, grants=Grants(case.bundle.player_state), rules=case.service.rules, clock=lambda: case.clock.now)
    assert service.purchase(envelope, case.store) == response
    assert case.store.profile() == committed and service.grants.calls == []


def test_receipt_replay_requires_same_revision_and_content(case):
    envelope = message(case)
    case.service.purchase(envelope, case.store)
    old = case.grants.content_version
    case.grants.content_version = "c" * 64
    fail(case, envelope, "trade_retry_stale")
    case.grants.content_version = old
    mutate(case, lambda state: state.update(other_progress=1))
    fail(case, envelope, "trade_retry_stale")


def test_receipt_replay_revalidates_player_and_rules(case, monkeypatch):
    envelope = message(case)
    case.service.purchase(envelope, case.store)
    other = TradeService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions,
        case.catalog, grants=case.grants, rules=TradeRules("never", "never"), clock=lambda: case.clock.now)
    original = case.service
    case.service = other
    fail(case, envelope, "trade_retry_stale")
    case.service = original

    def invalid_player(state, **_):
        raise ProtocolError("fixture_player", "bad player", 503)

    monkeypatch.setattr(case.bundle.player_state, "validate", invalid_player)
    fail(case, envelope, "fixture_player")


@pytest.mark.parametrize("shop", list(range(100001, 100009)))
def test_receipts_committed_before_a_resource_upgrade_replay_their_exact_bytes(case, shop):
    envelope = message(case, shop)
    response, upgraded = commit_before_upgrade(case, envelope)
    committed, calls = case.store.profile(), list(case.grants.calls)
    [receipt] = committed.state["trade_receipts"]["receipts"]
    assert receipt["content"] != upgraded._content_version(case.catalog.product(shop).registration)
    assert upgraded.purchase(envelope, case.store) == response
    assert case.store.profile() == committed and case.grants.calls == calls


@pytest.mark.parametrize("forge", [lambda service: "f" * 64, lambda service: service.catalog.content_version,
                                   lambda service: service._content_version(111)])
def test_receipt_contents_no_earlier_resource_produced_stay_stale_after_an_upgrade(case, forge):
    envelope = message(case)
    _, upgraded = commit_before_upgrade(case, envelope)
    forged = forge(case.service)
    overwrite(case, lambda state: state["trade_receipts"]["receipts"][0].update(content=forged))
    case.service = upgraded
    fail(case, envelope, "trade_retry_stale")


def test_receipts_from_an_earlier_resource_still_require_their_committed_revision(case):
    envelope = message(case)
    _, upgraded = commit_before_upgrade(case, envelope)
    mutate(case, lambda state: state.update(other_progress=1))
    case.service = upgraded
    fail(case, envelope, "trade_retry_stale")


def test_client_clock_rollback_is_a_new_purchase_while_exact_repeats_stay_rejected(case):
    for tick in (0x7FFFFFFF, -0x80000000, 86400, 1000):
        case.service.purchase(message(case, 100001, tick=tick), case.store)
    fail(case, message(case, 100001, tick=86400), "trade_retry_stale")
    saved = case.store.profile().state
    assert saved["trade_history"]["products"]["100001"]["lifetime_count"] == 4
    assert saved["trade_receipts"]["schema_version"] == SCHEMA_VERSION
    assert len(saved["trade_receipts"]["processed_keys"]) == 4


def test_backwards_server_clock_is_rejected_before_payment(case):
    case.service.purchase(message(case), case.store)
    case.clock.now -= timedelta(seconds=1)
    fail(case, message(case, tick=1001), "trade_clock_backwards")


def test_without_its_own_clock_the_service_reads_the_bootstrap_clock(case):
    bootstrap = case.service.bootstrap
    case.service = TradeService(bootstrap, case.bundle.player_state, case.bundle.possessions, case.catalog,
                                grants=case.grants, rules=case.service.rules)
    assert case.service.clock is bootstrap.clock
    case.service.purchase(message(case), case.store)
    case.clock.now -= timedelta(seconds=1)
    fail(case, message(case, tick=1001), "trade_clock_backwards")


def test_evicted_request_is_still_rejected_by_its_processed_digest(case):
    first = message(case, 100001)
    for offset in range(HISTORY_LIMIT + 1):
        case.service.purchase(message(case, 100001, tick=1000 + offset), case.store)
    history = case.store.profile().state["trade_receipts"]
    assert len(history["receipts"]) == HISTORY_LIMIT and len(history["processed_keys"]) == HISTORY_LIMIT + 1
    fail(case, first, "trade_request_stale")


def test_legacy_watermark_receipts_upgrade_and_keep_replaying(case):
    envelope = message(case, 100001, tick=5000)
    response = case.service.purchase(envelope, case.store)
    overwrite(case, legacy_receipts)
    before = case.store.profile()
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == before
    case.service.purchase(message(case, 100001, tick=4999), case.store)
    history = case.store.profile().state["trade_receipts"]
    assert history["schema_version"] == SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]
    assert all(set(entry) == {"request", "content", "revision", "response", "operation"} for entry in history["receipts"])


def test_full_legacy_second_upgrades_and_accepts_an_earlier_client_time(case):
    seen = [hashlib.sha256(str(index).encode()).hexdigest() for index in range(PROCESSED_LIMIT)]
    mutate(case, lambda state: state.update(trade_receipts={"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [],
                                                            "last_client_time": 1000, "seen_keys": seen}))
    case.service.purchase(message(case, 100001, tick=999), case.store)
    history = case.store.profile().state["trade_receipts"]
    assert history["schema_version"] == SCHEMA_VERSION and history["processed_keys"][:-1] == seen[1:]


@pytest.mark.parametrize("receipt", [None, {}, {"schema_version": True},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "last_client_time": None, "seen_keys": ["a" * 64]},
    {"schema_version": SCHEMA_VERSION, "receipts": [], "processed_keys": ["a"]},
    {"schema_version": SCHEMA_VERSION, "receipts": [], "processed_keys": [], "last_client_time": 1000}])
def test_bad_receipt_state_cannot_be_overwritten(case, receipt):
    mutate(case, lambda state: state.update(trade_receipts=receipt))
    fail(case, message(case), "trade_receipts_invalid")


def test_native_balance_refusal_is_logged_because_the_client_resends_it(case, caplog):
    mutate(case, lambda state: state["tables"]["game_param"][0].update(Money=10, Experience=0))
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fail(case, message(case, 100006), "trade_native_balance")
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        "Trade registered:111 refused with trade_native_balance; the native client shows a communication error "
        "and resends the same request until the server accepts it"]


def test_parallel_exact_requests_commit_one_transaction(case):
    envelope = message(case, 100001)
    revision = case.store.profile().revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: case.service.purchase(envelope, case.store), range(2)))
    assert responses[0] == responses[1] and case.store.profile().revision == revision + 1


def test_concurrent_distinct_requests_cannot_exceed_last_available_purchase(case):
    envelopes = [message(case, 100009, tick=tick) for tick in (1000, 1001)]

    def purchase(envelope):
        try:
            return case.service.purchase(envelope, case.store)
        except ProtocolError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(purchase, envelopes))
    assert sum(isinstance(value, bytes) for value in outcomes) == 1
    assert set(value for value in outcomes if isinstance(value, str)) == {"trade_limit_reached"}
    saved = case.store.profile().state
    assert saved["trade_history"]["products"]["100009"]["lifetime_count"] == 1
    assert counts(saved)["trade_item_003000"] == 990 and counts(saved)["gift_0002_ruby"] == 2


def test_malformed_matching_saved_response_cannot_reapply_trade(case):
    envelope = message(case)
    case.service.purchase(envelope, case.store)
    profile = case.store.profile()
    saved = deepcopy(profile.state)
    receipt = saved["trade_receipts"]["receipts"][0]
    receipt.update(response="<broken", revision=profile.revision + 1)
    case.store.update_state(saved, profile.revision)
    fail(case, envelope, "trade_receipts_invalid")


@pytest.mark.parametrize("field,value", [("operation", 999), ("operation", True), ("operation", "104"),
                                         ("client_time", 1000), ("response", "")])
def test_receipt_fields_outside_the_ledger_contract_are_never_reset(case, field, value):
    case.service.purchase(message(case), case.store)
    mutate(case, lambda state: state["trade_receipts"]["receipts"][0].update({field: value}))
    fail(case, message(case, tick=1001), "trade_receipts_invalid")


def test_unit_space_failure_does_not_spend_or_increment_history(case, monkeypatch):
    def full(state, count):
        raise ProtocolError("unit_roster_capacity", "no free unit slots", 409)

    monkeypatch.setattr(case.bundle.player_state, "require_unit_space", full)
    fail(case, message(case, 100001), "unit_roster_capacity")


def test_cas_retry_preserves_other_progress_and_recalculates_current_balance(case, monkeypatch):
    original = case.store.update_state
    calls = []

    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = case.store.profile()
            saved = deepcopy(current.state)
            saved["concurrent_progress"] = True
            original(saved, current.revision)
            raise ValueError("changed")
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", update)
    case.service.purchase(message(case), case.store)
    assert len(calls) == 2 and case.store.profile().state["concurrent_progress"] is True
    assert counts(case.store.profile().state)["trade_item_003000"] == 990


def test_three_conflicts_leave_all_state_unchanged(case, monkeypatch):
    calls = []

    def conflict(state, revision):
        calls.append(revision)
        raise ValueError("changed")

    monkeypatch.setattr(case.store, "update_state", conflict)
    fail(case, message(case), "profile_changed")
    assert len(calls) == 3


@pytest.mark.parametrize("number", [34, 41, 43, 44, 137, 107])
def test_each_native_package_is_preflighted_before_commit(case, monkeypatch, number):
    import aigis_ps.trade_service as module

    original = module.encode_response

    def too_large(profile, xml):
        if profile.registration_keys == (number,):
            raise ProtocolError("fixture_capacity", "too large", 503)
        return original(profile, xml)

    monkeypatch.setattr(module, "encode_response", too_large)
    fail(case, message(case), "fixture_capacity")


def test_xml_and_receipt_limits_fail_without_writes(case, monkeypatch):
    monkeypatch.setattr("aigis_ps.trade_service.TRADE_LIMITS", Limits(max_values=1))
    fail(case, message(case), "trade_response_invalid")
    monkeypatch.undo()
    monkeypatch.setattr("aigis_ps.trade_service.RESPONSE_LIMIT", 1)
    fail(case, message(case), "trade_response_capacity")


def test_preflight_uses_fixed_transaction_time_without_mutating_shared_provider(case):
    provider = case.service.bootstrap.exchange_history_provider
    calls = []

    def clock():
        calls.append(1)
        return case.clock.now

    case.service.clock = clock
    case.service.purchase(message(case), case.store)
    assert len(calls) == 1
    assert case.service.bootstrap.exchange_history_provider is provider

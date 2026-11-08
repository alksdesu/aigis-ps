from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

import aigis_ps.request_ledger as ledger
import aigis_ps.tower_trade_service as module
from aigis_ps.admin_cmd_tower_trade import REBIND_PRODUCT
from aigis_ps.alxml import Column, loads
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.storage import Store
from aigis_ps.tower_trade_grants import TowerTradeGrants
from aigis_ps.tower_trade_service import HISTORY_LIMIT, LEDGER_KEY, RECEIPT_SCHEMA_VERSION, TowerTradeService
from aigis_ps.tower_trade_state import REBIND_OPERATION, REBIND_UNNEEDED, history_digest
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
PERIOD, NEXT_PERIOD = "2026-11", "2026-12"
MONTH_END = datetime(2026, 12, 1, 3, 59, tzinfo=JST)
MONTH_START = datetime(2026, 12, 1, 4, tzinfo=JST)
TIERED, LIMITED, STACKED, OTHER_STACKED = 10000, 10001, 12038, 12039
REUSED_SOURCE = "f" * 64
FORGED_CONTENT = "e" * 64


@pytest.fixture
def case(bundle, tmp_path):
    assert bundle.tower_trades is not None, bundle.tower_trade_error
    catalog = bundle.tower_trades.catalog
    players, bootstrap = copy(bundle.player_state), copy(bundle.bootstrap)
    clock = SimpleNamespace(now=NOW)
    bootstrap.clock = lambda: clock.now
    grants = TowerTradeGrants(players, bundle.possessions, catalog)
    service = TowerTradeService(bootstrap, players, bundle.possessions, catalog,
                               grants=grants, clock=lambda: clock.now)
    state = players.initialize(bootstrap.initial_state())
    state["tables"]["possessions"] = [{"ItemID": item, "Count": 999999, "Enabled": 0}
                                       for item in catalog.currencies]
    state["retained_progress"] = {"nested": [1, 2]}
    state = service.initialize(state)
    path = tmp_path / "tower.sqlite3"
    return SimpleNamespace(bundle=bundle, catalog=catalog, players=players, bootstrap=bootstrap,
        grants=grants, service=service, clock=clock, path=path, store=Store(path, state))


def message(case, shop=10000, quantity=1, *, tick=1000, columns=None):
    fields = (Column("SHOP", "I", (shop,)), Column("NUM", "I", (quantity,)))
    return request(case.store, 69, fields if columns is None else columns, tick=tick)


def read_message(case, currency=None, *, tick=1000):
    return request(case.store, 65, (Column("ITEM", "S", (currency or case.catalog.currencies[0],)),), tick=tick)


def mutate(case, action):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    action(state)
    case.store.update_state(state, profile.revision)


def overwrite(case, action):
    state = case.store.profile().state
    action(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def legacy_receipts(state):
    receipts = state["tower_trade_receipts"]
    rows = [{**entry, "client_time": 5000} for entry in receipts[LEDGER_KEY]["receipts"]]
    state["tower_trade_receipts"] = {"schema_version": 1, "receipts": rows, "last_client_time": 5000,
                                     "seen_keys": [rows[-1]["request"]], "history_digest": receipts["history_digest"]}


def nodes(xml):
    return {node.name: node for node in loads(xml).children}


def trade_counts(xml):
    return nodes(xml)["TowerTradeHistory"].children[1].values


def never_reset_version(service):
    content = {"catalog": service.catalog.content_version, "grants": service.grants.content_version,
               "history": service.history.legacy_content_version, "policy": 1}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def never_reset(version):
    def rewrite(state):
        # The never-reset history saved lifetime totals as counts and bound its receipts to that form.
        history = state["tower_trade_history"]
        state["tower_trade_history"] = {"schema_version": 1, "records": {
            key: {"count": row["lifetime"], "source": row["source"]} for key, row in history["records"].items()}}
        receipts = state["tower_trade_receipts"]
        receipts["history_digest"] = history_digest(state)
        for row in receipts[LEDGER_KEY]["receipts"]:
            row["content"] = version
    return rewrite


def reuse(shop):
    def rewrite(state):
        # A resource that gives the ID to another reward leaves the saved identity behind, with matching receipts.
        state["tower_trade_history"]["records"][str(shop)]["source"] = REUSED_SOURCE
        state["tower_trade_receipts"]["history_digest"] = history_digest(state)
    return rewrite


def counts(state):
    return {row["ItemID"]: row["Count"] for row in state["tables"]["possessions"]}


def fail(case, envelope, code=None, *, read=False):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        (case.service.state if read else case.service.purchase)(envelope, case.store)
    assert case.store.profile() == before
    if code:
        assert caught.value.code == code
    return caught.value


def test_cli_wires_both_operations_and_initializer_is_pure(case):
    assert set(case.bundle.tower_trades.handlers()) == {65, 69}
    status = case.service.status()
    assert status["ready"] is True and status["history"]["reset_policy"] == "business_month"
    original = case.store.profile().state
    before = deepcopy(original)
    assert case.service.initialize(original) == original
    assert original == before
    assert "tower_trade_receipts" not in original and "tower_trade_history" not in original


@pytest.mark.parametrize("currency_index", [0, 1, 2])
def test_empty_history_read_is_typed_and_never_writes(case, currency_index, monkeypatch):
    monkeypatch.setattr(case.store, "update_state", lambda *_: pytest.fail("read attempted write"))
    before = case.store.profile()
    result = nodes(case.service.state(read_message(case, case.catalog.currencies[currency_index]), case.store))
    assert set(result) == {"SYSTEM", "TowerTradeHistory"}
    assert result["TowerTradeHistory"].children == (Column("ID", "I", ()), Column("TradeCount", "I", ()))
    assert case.store.profile() == before


@pytest.mark.parametrize("currency_index", [0, 1, 2])
def test_all_three_currencies_pay_once_and_return_native_unit_delta(case, currency_index):
    currency = case.catalog.currencies[currency_index]
    product = next(product for product in case.catalog.products if product.currency_item_id == currency
                   and product.card_id and not product.disclose_emblem_ids and not product.disclose_item_id)
    before = case.store.profile()
    quote = case.service.history.quote(before.state, product, 1, NOW)
    result = nodes(case.service.purchase(message(case, product.shop_id), case.store))
    after = case.store.profile()
    units = after.state["player_state"]["units"][len(before.state["player_state"]["units"]):]
    assert after.revision == before.revision + 1 and len(units) == 1
    assert units[0]["CardID"] == product.card_id
    assert after.state["retained_progress"] == before.state["retained_progress"]
    assert after.state["tower_trade_history"]["records"][str(product.shop_id)]["count"] == 1
    assert set(result) == {"SYSTEM", "TowerTradeHistory", "PBP", "PPU", "POSSESSION"}
    assert result["TowerTradeHistory"].children == (Column("ID", "I", (product.shop_id,)), Column("TradeCount", "I", (1,)))
    assert result["PBP"].children == (Column("A1", "S", ("UX",)), Column("A2", "I", (1,)))
    assert result["PPU"].children == tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I",
        tuple(unit[name] for unit in units)) for name, alias in UNIT_FIELDS)
    assert result["POSSESSION"].children == (Column("ItemID", "S", (currency,)),
        Column("Count", "I", (999999 - quote.total_price,)), Column("Enabled", "I", (0,)))
    assert counts(after.state) == dict(counts(before.state), **{currency: 999999 - quote.total_price})


def test_inventory_purchase_keeps_units_and_returns_typed_empty_ppu(case):
    product = case.catalog.product(12038)
    before = case.store.profile()
    quote = case.service.history.quote(before.state, product, 3, NOW)
    result = nodes(case.service.purchase(message(case, product.shop_id, 3), case.store))
    after = case.store.profile()
    assert after.state["player_state"] == before.state["player_state"]
    assert counts(after.state)[product.item_id] == 3
    assert counts(after.state)[product.currency_item_id] == 999999 - quote.total_price
    assert "PBP" not in result and "PPT" not in result
    assert result["PPU"].children == tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I", ())
                                          for name, alias in UNIT_FIELDS)
    assert result["POSSESSION"].children == (Column("ItemID", "S", (product.currency_item_id, product.item_id)),
        Column("Count", "I", (999999 - quote.total_price, 3)), Column("Enabled", "I", (0, 0)))


def test_each_currency_read_returns_complete_history_without_replacing_other_categories(case, monkeypatch):
    for offset, identifier in enumerate((10000, 10001, 12038)):
        case.service.purchase(message(case, identifier, tick=1000 + offset), case.store)
    before = case.store.profile()
    monkeypatch.setattr(case.store, "update_state", lambda *_: pytest.fail("history read attempted write"))
    for currency in case.catalog.currencies:
        result = nodes(case.service.state(read_message(case, currency), case.store))
        assert result["TowerTradeHistory"].children == (Column("ID", "I", (10000, 10001, 12038)),
                                                       Column("TradeCount", "I", (1, 1, 1)))
    assert case.store.profile() == before


def test_counts_restart_when_the_business_month_turns(case):
    case.clock.now = MONTH_END
    first = case.service.purchase(message(case, LIMITED), case.store)
    assert nodes(first)["TowerTradeHistory"].children == (Column("ID", "I", (LIMITED,)),
                                                          Column("TradeCount", "I", (1,)))
    fail(case, message(case, LIMITED, tick=1001), "tower_trade_limit_exceeded")
    assert trade_counts(case.service.state(read_message(case), case.store)) == (1,)
    case.clock.now = MONTH_START
    before = case.store.profile()
    assert trade_counts(case.service.state(read_message(case), case.store)) == (0,)
    assert case.store.profile() == before
    assert trade_counts(case.service.purchase(message(case, LIMITED, tick=1002), case.store)) == (1,)
    state = case.store.profile().state
    assert state["tower_trade_history"]["records"][str(LIMITED)] == {
        "source": case.service.history.source(case.catalog.product(LIMITED)), "period": NEXT_PERIOD, "count": 1,
        "lifetime": 2}
    assert [row["purchased_count"] for row in state["tower_trade_receipts"][LEDGER_KEY]["receipts"]] == [1, 2]


def test_without_its_own_clock_the_service_reads_the_bootstrap_clock(case):
    case.service = TowerTradeService(case.bootstrap, case.players, case.bundle.possessions, case.catalog,
                                     grants=case.grants)
    assert case.service.clock is case.bootstrap.clock
    case.clock.now = MONTH_END
    case.service.purchase(message(case, LIMITED), case.store)
    case.clock.now = MONTH_START
    assert trade_counts(case.service.state(read_message(case), case.store)) == (0,)


def test_price_tiers_follow_this_months_purchase_ordinal(case):
    currency = case.catalog.product(TIERED).currency_item_id
    paid = []
    for tick, moment in enumerate((MONTH_END, MONTH_END, MONTH_START), 1000):
        case.clock.now = moment
        before = counts(case.store.profile().state)[currency]
        case.service.purchase(message(case, TIERED, tick=tick), case.store)
        paid.append(before - counts(case.store.profile().state)[currency])
    assert paid == [20000, 15000, 20000]


def test_exact_retry_answers_the_month_it_was_bought_in_without_reading_the_clock(case):
    case.clock.now = MONTH_END
    envelope = message(case, LIMITED)
    response = case.service.purchase(envelope, case.store)
    committed = case.store.profile()
    case.service.clock = lambda: pytest.fail("retry read clock")
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == committed


def test_a_clock_set_back_before_the_saved_month_keeps_its_count_and_refuses_that_product(case):
    case.clock.now = MONTH_START
    case.service.purchase(message(case, LIMITED), case.store)
    case.clock.now = MONTH_END
    assert trade_counts(case.service.state(read_message(case), case.store)) == (1,)
    fail(case, message(case, LIMITED, tick=1001), "tower_trade_clock_backwards")
    envelope = message(case, STACKED, tick=1002)
    response = case.service.purchase(envelope, case.store)
    assert trade_counts(response) == (1, 1)
    records = case.store.profile().state["tower_trade_history"]["records"]
    assert (records[str(LIMITED)]["period"], records[str(STACKED)]["period"]) == (NEXT_PERIOD, PERIOD)
    case.clock.now = MONTH_START
    assert case.service.purchase(envelope, case.store) == response


def test_six_unsupported_vouchers_fail_before_debit(case, monkeypatch):
    monkeypatch.setattr(case.service.possessions, "debit", lambda *_: pytest.fail("unsupported voucher spent currency"))
    vouchers = [product for product in case.catalog.products if product.unsupported_reason]
    assert len(vouchers) == 6
    for product in vouchers:
        fail(case, message(case, product.shop_id), "tower_trade_grant_unsupported")


def test_refusals_are_logged_because_the_native_client_resends_both_requests(case, caplog):
    voucher = next(product for product in case.catalog.products if product.unsupported_reason)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fail(case, message(case, voucher.shop_id), "tower_trade_grant_unsupported")
        fail(case, read_message(case, "unknown"), "invalid_request_fields", read=True)
    aftermath = "the native client shows a communication error and resends the same request until the server accepts it"
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        f"Tower trade 69 refused with tower_trade_grant_unsupported; {aftermath}",
        f"Tower trade 65 refused with invalid_request_fields; {aftermath}"]


def test_history_quote_refusal_is_logged_when_delivery_checks_pass(case, monkeypatch, caplog):
    voucher = next(product for product in case.catalog.products if product.unsupported_reason)
    monkeypatch.setattr(case.grants, "require_supported", lambda product: None)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fail(case, message(case, voucher.shop_id), "tower_trade_unsupported")
    assert any(record.getMessage().startswith("Tower trade 69 refused with tower_trade_unsupported;")
               for record in caplog.records if record.name == "aigis_ps")


def test_insufficient_currency_and_full_barracks_never_write(case):
    case.players.capacity_provider = lambda _: 0
    fail(case, message(case), "unit_roster_capacity")
    case.players.capacity_provider = lambda _: 100
    mutate(case, lambda state: state["tables"].update(possessions=[]))
    fail(case, message(case), "possession_insufficient")


def test_inventory_capacity_rejects_debit_and_history_together(case):
    product = case.catalog.product(12038)
    mutate(case, lambda state: state["tables"]["possessions"].append(
        {"ItemID": product.item_id, "Count": product.item_stack}))
    fail(case, message(case, product.shop_id), "possession_stack_exceeded")


def test_exact_retry_after_reopen_returns_identical_response_without_clock_or_grant(case, monkeypatch):
    envelope = message(case)
    response = case.service.purchase(envelope, case.store)
    committed = case.store.profile()
    case.store = Store(case.path)
    monkeypatch.setattr(case.grants, "apply", lambda *_: pytest.fail("retry granted again"))
    case.service.clock = lambda: pytest.fail("retry read clock")
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == committed


def test_exact_retry_rejects_intervening_save_revision(case):
    envelope = message(case)
    case.service.purchase(envelope, case.store)
    mutate(case, lambda state: state["retained_progress"]["nested"].append(3))
    fail(case, envelope, "tower_trade_retry_stale")


def test_exact_retry_rejects_changed_quantity_after_earlier_receipt_eviction(case, monkeypatch):
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    case.service.purchase(message(case, 12038), case.store)
    second = message(case, 12038, tick=1001)
    case.service.purchase(second, case.store)
    profile = case.store.profile()
    def corrupt(state):
        receipts = state["tower_trade_receipts"][LEDGER_KEY]["receipts"]
        assert len(receipts) == 1 and receipts[-1]["purchased_count"] == 2 and receipts[-1]["quantity"] == 1
        receipts[-1].update(quantity=2, revision=profile.revision + 1)
    mutate(case, corrupt)
    fail(case, second, "tower_trade_receipts_invalid")


def test_same_second_requests_and_client_clock_rollback_are_new_purchases(case):
    first = message(case, 12038, quantity=1)
    case.service.purchase(first, case.store)
    second = message(case, 12038, quantity=2)
    result = case.service.purchase(second, case.store)
    assert counts(case.store.profile().state)[case.catalog.product(12038).item_id] == 3
    assert case.service.purchase(second, case.store) == result
    fail(case, first, "tower_trade_retry_stale")
    case.service.purchase(message(case, 12039, tick=999), case.store)
    assert counts(case.store.profile().state)[case.catalog.product(12039).item_id] == 1


def test_client_clock_is_not_a_request_order(case):
    for tick in (0x7FFFFFFF, -0x80000000, 86400):
        case.service.purchase(message(case, 12038, tick=tick), case.store)
    fail(case, message(case, 12038, tick=-0x80000000), "tower_trade_retry_stale")
    state = case.store.profile().state
    assert case.service.history.count(state, case.catalog.product(12038), NOW) == 3
    assert state["tower_trade_receipts"]["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert len(state["tower_trade_receipts"][LEDGER_KEY]["processed_keys"]) == 3


def test_evicted_receipt_is_still_blocked_by_its_processed_digest(case):
    first = message(case, 12038)
    for offset in range(HISTORY_LIMIT + 1):
        case.service.purchase(message(case, 12038, tick=1000 + offset), case.store)
    requests = case.store.profile().state["tower_trade_receipts"][LEDGER_KEY]
    assert len(requests["receipts"]) == HISTORY_LIMIT and len(requests["processed_keys"]) == HISTORY_LIMIT + 1
    fail(case, first, "tower_trade_request_stale")


def test_legacy_watermark_receipts_upgrade_and_keep_replaying(case):
    envelope = message(case, 12038, tick=5000)
    response = case.service.purchase(envelope, case.store)
    overwrite(case, legacy_receipts)
    assert case.service.initialize(case.store.profile().state) == case.store.profile().state
    before = case.store.profile()
    assert case.service.purchase(envelope, case.store) == response
    assert case.service.state(read_message(case), case.store)
    assert case.store.profile() == before
    case.service.purchase(message(case, 12039, tick=4999), case.store)
    receipts = case.store.profile().state["tower_trade_receipts"]
    assert set(receipts) == {"schema_version", "history_digest", LEDGER_KEY}
    assert receipts["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert receipts[LEDGER_KEY]["processed_keys"] == [entry["request"] for entry in receipts[LEDGER_KEY]["receipts"]]
    assert all("client_time" not in entry for entry in receipts[LEDGER_KEY]["receipts"])


def test_receipts_of_the_never_reset_version_replay_their_committed_bytes(case):
    legacy = never_reset_version(case.service)
    assert case.service.replayable_content_versions() == {case.service.content_version, legacy}
    envelope = message(case, STACKED, quantity=2)
    response = case.service.purchase(envelope, case.store)
    overwrite(case, never_reset(legacy))
    saved = case.store.profile()
    assert saved.state["tower_trade_history"]["schema_version"] == 1
    assert case.service.initialize(saved.state) == saved.state
    case.service.clock = lambda: pytest.fail("retry read clock")
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == saved
    case.service.clock = lambda: case.clock.now
    assert trade_counts(case.service.state(read_message(case), case.store)) == (0,)
    assert case.store.profile() == saved


def test_forged_receipt_versions_are_stale_and_never_replayed(case):
    envelope = message(case, STACKED)
    case.service.purchase(envelope, case.store)
    overwrite(case, lambda state: state["tower_trade_receipts"][LEDGER_KEY]["receipts"][0].update(
        content=FORGED_CONTENT))
    fail(case, envelope, "tower_trade_retry_stale")


def test_the_first_purchase_after_the_upgrade_continues_the_lifetime_total(case):
    legacy = never_reset_version(case.service)
    case.service.purchase(message(case, STACKED, quantity=2), case.store)
    overwrite(case, never_reset(legacy))
    case.service.purchase(message(case, STACKED, tick=1001), case.store)
    state = case.store.profile().state
    assert state["tower_trade_history"] == {"schema_version": 2, "records": {str(STACKED): {
        "source": case.service.history.source(case.catalog.product(STACKED)), "period": PERIOD, "count": 1,
        "lifetime": 3}}}
    receipts = state["tower_trade_receipts"][LEDGER_KEY]["receipts"]
    assert [(entry["content"] == legacy, entry["purchased_count"]) for entry in receipts] == [(True, 2), (False, 3)]
    assert state["tower_trade_receipts"]["history_digest"] == history_digest(state)


@pytest.mark.parametrize("damage", ["digest", "watermark", "extra", "schema", "processed"])
def test_corrupt_legacy_or_current_receipt_wrappers_are_never_reset(case, damage):
    case.service.purchase(message(case, 12038, tick=5000), case.store)
    def corrupt(state):
        if damage == "digest":
            legacy_receipts(state)
            state["tower_trade_receipts"]["history_digest"] = "f" * 64
        elif damage == "watermark":
            legacy_receipts(state)
            state["tower_trade_receipts"]["last_client_time"] = True
        elif damage == "extra":
            state["tower_trade_receipts"]["last_client_time"] = 5000
        elif damage == "schema":
            state["tower_trade_receipts"]["schema_version"] = 1
        else:
            state["tower_trade_receipts"][LEDGER_KEY]["processed_keys"].clear()
    mutate(case, corrupt)
    fail(case, message(case, 12039, tick=5001), "tower_trade_receipts_invalid")
    fail(case, read_message(case), "tower_trade_receipts_invalid", read=True)
    with pytest.raises(ProtocolError) as caught:
        case.service.initialize(case.store.profile().state)
    assert caught.value.code == "tower_trade_receipts_invalid"


@pytest.mark.parametrize("damage", ["receipts", "history", "record", "empty_receipts", "source", "count", "xml"])
def test_partial_deletion_or_corruption_cannot_restart_purchase_tracking(case, damage):
    case.service.purchase(message(case), case.store)
    def corrupt(state):
        if damage == "receipts":
            state.pop("tower_trade_receipts")
        elif damage == "history":
            state.pop("tower_trade_history")
        elif damage == "record":
            state["tower_trade_history"]["records"].clear()
        elif damage == "empty_receipts":
            state["tower_trade_receipts"][LEDGER_KEY]["receipts"].clear()
        elif damage == "source":
            state["tower_trade_history"]["records"]["10000"]["source"] = "f" * 64
        elif damage == "count":
            state["tower_trade_history"]["records"]["10000"]["count"] = 0
        else:
            state["tower_trade_receipts"][LEDGER_KEY]["receipts"][0]["response"] = "<broken"
    mutate(case, corrupt)
    assert fail(case, message(case, 12038, tick=1001)).status == 503
    assert fail(case, read_message(case), read=True).status == 503


@pytest.mark.parametrize("field,value", [("quantity", 0), ("quantity", 2), ("shop_id", True),
    ("purchased_count", 2 ** 31), ("unit_indices", None), ("unit_indices", [-1]),
    ("unit_indices", [5, 5]), ("unit_indices", list(range(101))), ("client_time", 1000)])
def test_receipt_shapes_outside_the_ledger_contract_are_never_reset(case, field, value):
    case.service.purchase(message(case), case.store)
    mutate(case, lambda state: state["tower_trade_receipts"][LEDGER_KEY]["receipts"][0].update({field: value}))
    fail(case, message(case, 12038, tick=1001), "tower_trade_receipts_invalid")
    fail(case, read_message(case), "tower_trade_receipts_invalid", read=True)


def test_deleting_history_after_its_receipt_was_evicted_is_detected(case, monkeypatch):
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    case.service.purchase(message(case, 12038), case.store)
    case.service.purchase(message(case, 12039, tick=1001), case.store)
    assert len(case.store.profile().state["tower_trade_receipts"][LEDGER_KEY]["receipts"]) == 1
    mutate(case, lambda state: state["tower_trade_history"]["records"].pop("12038"))
    fail(case, message(case, 12040, tick=1002), "tower_trade_receipts_invalid")
    fail(case, read_message(case), "tower_trade_receipts_invalid", read=True)


def test_a_reused_identity_still_loads_but_refuses_both_requests(case):
    case.service.purchase(message(case, STACKED, quantity=2), case.store)
    mutate(case, reuse(STACKED))
    saved = case.store.profile()
    assert case.service.initialize(saved.state) == saved.state
    assert case.service.history.reused(saved.state) == (STACKED,)
    fail(case, read_message(case), "tower_trade_product_reused", read=True)
    fail(case, message(case, OTHER_STACKED, tick=1001), "tower_trade_product_reused")


def test_reuse_report_names_each_reused_product_and_its_repair_without_writing(case):
    assert REBIND_PRODUCT.operation is module.REBIND_OPERATION is REBIND_OPERATION
    assert REBIND_OPERATION in case.service.status()["history"]["reused_products"]
    fresh = case.store.profile().state
    assert "tower_trade_history" not in fresh
    assert case.service.reuse_report(fresh) == {
        "reused_products": [], "refused_operations": [65, 69], "repair": REBIND_PRODUCT.operation,
        "client": module._REFUSAL_AFTERMATH}
    case.service.purchase(message(case, STACKED), case.store)
    case.service.purchase(message(case, OTHER_STACKED, tick=1001), case.store)
    assert case.service.reuse_report(case.store.profile().state)["reused_products"] == []
    for shop in (OTHER_STACKED, STACKED):
        mutate(case, reuse(shop))
    saved = case.store.profile()
    before = deepcopy(saved.state)
    report = case.service.reuse_report(saved.state)
    assert saved.state == before and json.loads(json.dumps(report)) == report
    assert report["reused_products"] == [STACKED, OTHER_STACKED]
    assert report["refused_operations"] == sorted(case.service.handlers())
    rebound = case.service.rebind_product(saved.state, saved.revision, STACKED, False)
    assert case.service.reuse_report(rebound)["reused_products"] == [OTHER_STACKED]


def test_reuse_report_raises_for_unreadable_history_and_changed_content(case, monkeypatch):
    broken = deepcopy(case.store.profile().state)
    broken["tower_trade_history"] = {"schema_version": 0, "records": {}}
    with pytest.raises(ProtocolError) as caught:
        case.service.reuse_report(broken)
    assert caught.value.code == "tower_trade_history_invalid"
    monkeypatch.setattr(case.grants, "content_version", FORGED_CONTENT)
    with pytest.raises(ProtocolError) as caught:
        case.service.reuse_report(case.store.profile().state)
    assert caught.value.code == "tower_trade_grant_dependencies_changed"


@pytest.mark.parametrize("carry", [True, False])
def test_rebinding_rewrites_the_record_and_the_receipt_digest(case, carry):
    case.service.purchase(message(case, STACKED, quantity=2), case.store)
    mutate(case, reuse(STACKED))
    saved = case.store.profile()
    rebound = case.service.rebind_product(saved.state, saved.revision, STACKED, carry)
    assert case.store.profile() == saved
    record = case.service.history.records(rebound)[STACKED]
    assert (record.source, record.period, record.count, record.lifetime) == (
        case.service.history.source(case.catalog.product(STACKED)), PERIOD if carry else None, 2 if carry else 0, 2)
    assert rebound["tower_trade_receipts"]["history_digest"] == history_digest(rebound)
    assert rebound["tower_trade_receipts"][LEDGER_KEY] == saved.state["tower_trade_receipts"][LEDGER_KEY]
    case.store.update_state(rebound, saved.revision)
    assert trade_counts(case.service.state(read_message(case), case.store)) == ((2,) if carry else (0,))
    purchased = case.service.purchase(message(case, STACKED, tick=1001), case.store)
    assert trade_counts(purchased) == ((3,) if carry else (1,))
    assert case.store.profile().state["tower_trade_history"]["records"][str(STACKED)]["lifetime"] == 3


def test_rebinding_refuses_unknown_products_records_needing_no_change_and_broken_receipts(case):
    case.service.purchase(message(case, STACKED), case.store)
    saved = case.store.profile()
    for shop, code, status in ((99999999, "tower_trade_product_unknown", 404), (STACKED, REBIND_UNNEEDED, 409),
                               (OTHER_STACKED, REBIND_UNNEEDED, 409)):
        with pytest.raises(ProtocolError) as caught:
            case.service.rebind_product(saved.state, saved.revision, shop, True)
        assert (caught.value.code, caught.value.status) == (code, status)
    broken = deepcopy(saved.state)
    reuse(STACKED)(broken)
    broken["tower_trade_receipts"]["history_digest"] = "0" * 64
    with pytest.raises(ProtocolError) as caught:
        case.service.rebind_product(broken, saved.revision, STACKED, True)
    assert caught.value.code == "tower_trade_receipts_invalid"


def test_dependency_drift_rejects_new_purchase_and_cached_retry(case, monkeypatch):
    old = message(case)
    case.service.purchase(old, case.store)
    monkeypatch.setattr(case.grants, "content_version", "f" * 64)
    fail(case, old, "tower_trade_grant_dependencies_changed")
    fail(case, message(case, 12038, tick=1001), "tower_trade_grant_dependencies_changed")


def test_cas_conflict_reloads_balance_and_preserves_unrelated_progress(case, monkeypatch):
    original = case.store.update_state
    calls = []
    product = case.catalog.product(12038)
    price = case.service.history.quote(case.store.profile().state, product, 1, NOW).total_price
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            profile = case.store.profile()
            changed = deepcopy(profile.state)
            changed["concurrent_progress"] = True
            next(row for row in changed["tables"]["possessions"]
                 if row["ItemID"] == product.currency_item_id)["Count"] -= 123
            original(changed, profile.revision)
            raise ValueError("changed")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    case.service.purchase(message(case, product.shop_id), case.store)
    final = case.store.profile().state
    assert len(calls) == 2 and final["concurrent_progress"] is True
    assert counts(final)[product.currency_item_id] == 999999 - 123 - price
    assert counts(final)[product.item_id] == 1


def test_three_cas_conflicts_leave_candidate_uncommitted(case, monkeypatch):
    calls = []
    def conflict(*_):
        calls.append(1)
        raise ValueError("changed")
    monkeypatch.setattr(case.store, "update_state", conflict)
    fail(case, message(case), "profile_changed")
    assert len(calls) == 3


def synchronize_first_two_commits(case, monkeypatch):
    original, barrier, lock = case.store.update_state, Barrier(2, timeout=30), Lock()
    calls = []
    def update(state, revision):
        with lock:
            calls.append(revision)
            current = len(calls)
        if current <= 2:
            barrier.wait()
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    return calls


def test_parallel_identical_requests_commit_exactly_one_reward(case, monkeypatch):
    envelope = message(case)
    before = case.store.profile()
    calls = synchronize_first_two_commits(case, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: case.service.purchase(envelope, case.store), range(2)))
    after = case.store.profile()
    assert results[0] == results[1]
    assert len(calls) == 2 and after.revision == before.revision + 1
    assert len(after.state["player_state"]["units"]) == len(before.state["player_state"]["units"]) + 1


def test_parallel_distinct_requests_recheck_last_remaining_purchase(case, monkeypatch):
    product = case.catalog.product(10001)
    assert product.limitation == 1
    before = case.store.profile()
    calls = synchronize_first_two_commits(case, monkeypatch)
    def purchase(tick):
        try:
            return case.service.purchase(message(case, product.shop_id, tick=tick), case.store)
        except ProtocolError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(purchase, (1000, 1001)))
    assert sum(isinstance(result, bytes) for result in results) == 1
    assert set(result for result in results if isinstance(result, str)) == {"tower_trade_limit_exceeded"}
    assert len(calls) == 2 and case.store.profile().revision == before.revision + 1
    assert case.service.history.count(case.store.profile().state, product, NOW) == 1


def test_preflight_failure_leaves_payment_reward_and_history_unwritten(case, monkeypatch):
    def failure(*_):
        raise ProtocolError("injected_preflight", "Injected preflight failure", 503)
    monkeypatch.setattr(case.service, "_preflight", failure)
    fail(case, message(case), "injected_preflight")


@pytest.mark.parametrize("number", [41, 129, 137, 69])
def test_every_native_encoding_failure_aborts_transaction(case, monkeypatch, number):
    original = module.encode_response
    def encode(profile, xml):
        if profile.registration_keys == (number,):
            raise ProtocolError("injected_encoding", "Injected native response overflow", 503)
        return original(profile, xml)
    monkeypatch.setattr(module, "encode_response", encode)
    fail(case, message(case), "injected_encoding")


@pytest.mark.parametrize("columns", [(), (Column("SHOP", "I", (10000,)),),
    (Column("SHOP", "S", ("10000",)), Column("NUM", "I", (1,))),
    (Column("SHOP", "I", (10000,)), Column("NUM", "I", (1, 2))),
    (Column("SHOP", "I", (10000,)), Column("NUM", "I", (0,))),
    (Column("SHOP", "I", (10000,)), Column("NUM", "I", (1,)), Column("PRICE", "I", (0,)))])
def test_request_shape_is_exact(case, columns):
    fail(case, message(case, columns=columns), "invalid_request_fields")


def test_unknown_product_and_currency_never_write(case):
    fail(case, message(case, 99999999), "tower_trade_product_unknown")
    fail(case, read_message(case, "unknown"), "invalid_request_fields", read=True)


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_credentials_are_checked_before_purchase(case, field):
    envelope = message(case)
    envelope.document.find(f"{field}/V").text = "other"
    fail(case, envelope, "invalid_method_key" if field == "MK" else "invalid_identity")

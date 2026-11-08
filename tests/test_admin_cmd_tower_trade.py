from copy import copy, deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps import admin_cmd_tower_trade
from aigis_ps.admin_cmd_tower_trade import REBIND_PRODUCT, STILL_REUSED, TOWER_TRADE, UNREAD_REASON
from aigis_ps.admin_command import AdminError, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.alxml import Column
from aigis_ps.energy_clock import JST
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.storage import AUTO_BACKUP_KIND
from aigis_ps.tower_trade_state import REBIND_UNNEEDED, history_digest
from test_admin_cmd_storage import commit, planned, preview_error
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_seams import MemoryStore
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 10, 12, tzinfo=JST)
PERIOD = "2026-11"
PURCHASE, FIRST_TICK, FUNDS = 69, 1000, 100000
STACKED, OTHER_STACKED, UNKNOWN_PRODUCT = 12038, 12039, 99999999
REUSED_SOURCE = "f" * 64
OPERATION = REBIND_PRODUCT.operation


def phrase(product_id):
    return f"REBIND {product_id}"


def reuse(state, shop):
    # A resource that gives the ID to another reward leaves the saved identity behind, with matching receipts.
    changed = deepcopy(state)
    changed["tower_trade_history"]["records"][str(shop)]["source"] = REUSED_SOURCE
    changed["tower_trade_receipts"]["history_digest"] = history_digest(changed)
    return changed


def rebinding(product_id, carry):
    return {"operation": OPERATION, "params": {"product_id": product_id, "carry": carry}}


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_tower_trade.register(commands)
    return commands


@pytest.fixture
def trades(bundle, monkeypatch):
    assert bundle.tower_trades is not None, bundle.tower_trade_error
    monkeypatch.setattr(bundle.tower_trades, "clock", lambda: NOW)
    return bundle.tower_trades


@pytest.fixture
def saves(bundle, trades):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW

    def purchased(purchases):
        # Console commits must survive a restart, so the save starts from the form a restart keeps.
        state = bundle.initialize(bundle.player_state.initialize(bootstrap.initial_state()))
        store = MemoryStore(bundle.possessions.credit(state, dict.fromkeys(trades.catalog.currencies, FUNDS)))
        for tick, (shop, quantity) in enumerate(purchases, FIRST_TICK):
            columns = (Column("SHOP", "I", (shop,)), Column("NUM", "I", (quantity,)))
            trades.purchase(request(store, PURCHASE, columns, tick=tick), store)
        return store.profile()

    return purchased


def console(bundle, state, revision, preflight):
    store = FakeStore(state, revision)
    generation = FakeGeneration(id="gen-tower-trade", bundle=bundle, clock=NOW)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def test_rebinding_registers_on_the_events_page_with_an_explicit_unread_reason(registry):
    assert [command.operation for command in registry] == [OPERATION]
    catalog = discover(PREFLIGHT_DOMAINS)
    assert "admin_cmd_tower_trade" in catalog.modules and OPERATION in catalog.commands
    assert (REBIND_PRODUCT.domain, REBIND_PRODUCT.touched) == ("events", frozenset({TOWER_TRADE}))
    assert TOWER_TRADE not in PREFLIGHT_DOMAINS and dict(REBIND_PRODUCT.preflight_none) == {TOWER_TRADE: UNREAD_REASON}
    assert REBIND_PRODUCT.destructive is True and not REBIND_PRODUCT.writes
    assert REBIND_PRODUCT.phrase(REBIND_PRODUCT.parse({"product_id": STACKED, "carry": False})) == phrase(STACKED)
    assert REBIND_PRODUCT.availability(SimpleNamespace(tower_trades=None)) == missing_service("tower_trades")
    with pytest.raises(ValueError):
        admin_cmd_tower_trade.register(registry)


@pytest.mark.parametrize("params", [
    {"product_id": 0, "carry": True}, {"product_id": -1, "carry": True}, {"product_id": True, "carry": True},
    {"product_id": str(STACKED), "carry": True}, {"product_id": float(STACKED), "carry": True},
    {"product_id": INT32_MAX + 1, "carry": True}, {"product_id": STACKED}, {"carry": True},
    {"product_id": STACKED, "carry": 1}, {"product_id": STACKED, "carry": "true"},
    {"product_id": STACKED, "carry": True, "count": 0},
])
def test_parameters_are_strict(registry, params):
    with pytest.raises(AdminError) as caught:
        registry.get(OPERATION).parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


@pytest.mark.parametrize("carry", [True, False])
def test_rebinding_commits_behind_its_phrase_and_one_automatic_backup(bundle, trades, saves, preflight, registry,
                                                                     carry):
    saved = saves([(STACKED, 2)])
    reused = reuse(saved.state, STACKED)
    assert bundle.initialize(deepcopy(reused)) == reused
    store, pipeline = console(bundle, reused, saved.revision, preflight)
    plan = planned(pipeline, registry, OPERATION, {"product_id": STACKED, "carry": carry})
    assert [item.describe() for item in plan.confirmations] == [{"step": 0, "operation": OPERATION,
                                                                 "phrase": phrase(STACKED)}]
    assert plan.preflight == {"ok": True, "readback": {}} and plan.restart_check == {"ok": True}
    source = trades.history.source(trades.catalog.product(STACKED))
    [step] = plan.describe()["steps"]
    assert step["warnings"] == [] and step["effects"] == {
        "product_id": STACKED, "carry": carry, "period": PERIOD, "source": {"before": REUSED_SOURCE, "after": source},
        "saved_period": {"before": PERIOD, "after": PERIOD if carry else None},
        "count": {"before": 2, "after": 2 if carry else 0}, "lifetime": {"before": 2, "after": 2}, "still_reused": []}
    with pytest.raises(AdminError) as caught:
        commit(pipeline, plan)
    assert caught.value.code == "admin_confirmation_required" and store.calls == []
    commit(pipeline, plan, (phrase(STACKED),))
    [backup] = store.backups
    assert backup["row"]["kind"] == AUTO_BACKUP_KIND and backup["state"] == reused
    record = trades.history.records(store.state)[STACKED]
    assert (record.source, record.period, record.count, record.lifetime) == (
        source, PERIOD if carry else None, 2 if carry else 0, 2)
    assert store.state["tower_trade_receipts"]["history_digest"] == history_digest(store.state)
    assert bundle.initialize(deepcopy(store.state)) == store.state


def test_rebinding_refuses_unknown_products_and_records_needing_no_change(bundle, saves, preflight, registry):
    saved = saves([(STACKED, 1)])
    store, pipeline = console(bundle, saved.state, saved.revision, preflight)
    for product_id, code, status in ((UNKNOWN_PRODUCT, "tower_trade_product_unknown", 404),
                                     (STACKED, REBIND_UNNEEDED, 409), (OTHER_STACKED, REBIND_UNNEEDED, 409)):
        preview_error(pipeline, registry, OPERATION, {"product_id": product_id, "carry": True}, code, status)
    assert store.calls == [] and store.backups == []


def test_each_reused_product_is_rebound_by_its_own_step(bundle, trades, saves, preflight, registry):
    saved = saves([(STACKED, 1), (OTHER_STACKED, 1)])
    reused = reuse(reuse(saved.state, STACKED), OTHER_STACKED)
    store, pipeline = console(bundle, reused, saved.revision, preflight)
    single = planned(pipeline, registry, OPERATION, {"product_id": STACKED, "carry": True})
    [step] = single.describe()["steps"]
    assert (step["effects"]["still_reused"], step["warnings"]) == ([OTHER_STACKED], [STILL_REUSED])
    both = pipeline.preview(registry.batch([rebinding(STACKED, True), rebinding(OTHER_STACKED, False)]))
    assert both.committable and [item.phrase for item in both.confirmations] == [phrase(STACKED), phrase(OTHER_STACKED)]
    assert [step["effects"]["still_reused"] for step in both.describe()["steps"]] == [[OTHER_STACKED], []]
    commit(pipeline, both, tuple(item.phrase for item in both.confirmations))
    assert trades.history.reused(store.state) == () and len(store.backups) == 1
    assert {shop: (record.period, record.count) for shop, record in trades.history.records(store.state).items()} == {
        STACKED: (PERIOD, 1), OTHER_STACKED: (None, 0)}

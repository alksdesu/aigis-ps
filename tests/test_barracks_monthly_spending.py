from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import replace
from datetime import datetime
import json
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.crystal_spending import read_spending, spend_crystals, spending_response
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import Store
from test_barracks_service import service
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def case(bundle, service, tmp_path):
    own_service = copy(service)
    own_service.bootstrap = copy(service.bootstrap)
    clock = SimpleNamespace(now=NOW)
    own_service.bootstrap.clock = lambda: clock.now
    initial = bundle.player_state.initialize(own_service.bootstrap.initial_state())
    initial, added = bundle.player_state.credit_units(initial, [{"card_id": 7, "level": 1, "count": 1}])
    initial["tables"]["game_param"][0].update(Rank=25,
        Experience=sum(row["NextExp"] for row in bundle.bootstrap.rank_rows[:24]))
    initial["tables"]["crystals"] = [{"FreeFlag": False, "Num": 50, "Date": 700000},
        {"FreeFlag": True, "Num": 30, "Date": 700001}]
    initial["sentinel"] = {"nested": ["unchanged"]}
    return SimpleNamespace(service=own_service, store=Store(tmp_path / "barracks-monthly.sqlite3", initial),
                           added=added, clock=clock)


def message(case, *, tick=1000, transfer=False):
    if not transfer:
        return request(case.store, 17, (), tick=tick)
    return request(case.store, 118, (Column("DEST", "I", (1,)), Container("BASE", (
        Column("A7", "I", (case.added[0]["GetIndex"],)), Column("AB", "I", (case.added[0]["UniqueID"],))))), tick=tick)


def change(case, callback):
    profile = case.store.profile()
    callback(profile.state)
    case.store.update_state(profile.state, profile.revision)


def overwrite(case, callback):
    state = case.store.profile().state
    callback(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def fail(case, envelope, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.handlers()[envelope.profile.registration_keys[0]](envelope, case.store)
    assert caught.value.code == code
    assert case.store.profile() == before


def groups(raw):
    return {node.name: node for node in loads(raw).children}


def test_purchase_consumes_thirty_and_counts_thirty_in_one_revision(case):
    before = case.store.profile()
    raw = case.service.purchase(message(case), case.store)
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert wallet_totals(before.state)["total"] - wallet_totals(after.state)["total"] == 30
    assert read_spending(after.state)["current_spent"] == 30
    assert groups(raw)["PGP2"] == spending_response(after.state)
    assert groups(raw)["PGC"].children[1].values == (str(1 << EXTRA_SHIFT),)
    assert after.state["player_state"] == before.state["player_state"]
    receipt = after.state["barracks_history"]["receipts"][0]
    assert receipt["content"] == case.service.purchase_content_version != case.service.content_version
    full = groups(case.service.bootstrap.game_param2(request(case.store, 35, ()), case.store))
    assert full["C1"].values == (30,)


def test_second_purchase_crosses_month_without_double_counting_previous_month(case):
    case.service.purchase(message(case), case.store)
    case.clock.now = datetime(2026, 12, 1, 4, tzinfo=JST)
    case.service.purchase(message(case, tick=1001), case.store)
    after = case.store.profile()
    ledger = read_spending(after.state)
    assert ledger["period"] == "2026-12" and ledger["previous_spent"] == ledger["current_spent"] == 30
    assert wallet_totals(after.state)["total"] == 20
    fail(case, message(case, tick=1002), "barracks_purchase_limit")


def test_exact_purchase_retry_reopen_never_reads_clock_or_recounts(case):
    envelope = message(case)
    raw = case.service.purchase(envelope, case.store)
    saved = case.store.profile()
    def forbidden():
        raise AssertionError("Cached purchase must not ask for the time")
    case.service.bootstrap.clock = forbidden
    assert case.service.purchase(envelope, Store(case.store.path)) == raw
    assert case.store.profile() == saved


def test_transfer_preserves_legacy_content_and_never_touches_monthly_state(case):
    def prepare(state):
        state.pop("crystal_spending", None)
        state["tables"]["game_param2"][0]["StampCardGrade"] = 100
        state["tables"]["config"][0][PURCHASE_FIELD] = 1 << EXTRA_SHIFT
    change(case, prepare)
    before = case.store.profile()
    def forbidden():
        raise AssertionError("Moving units must not read the spending clock")
    case.service.bootstrap.clock = forbidden
    envelope = message(case, transfer=True)
    raw = case.service.transfer(envelope, case.store)
    saved = case.store.profile()
    assert set(groups(raw)) == {"SYSTEM", "PPU"}
    assert "crystal_spending" not in saved.state
    assert saved.state["tables"]["game_param2"] == before.state["tables"]["game_param2"]
    assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert saved.state["barracks_history"]["receipts"][0]["content"] == case.service.content_version
    assert case.service.transfer(envelope, Store(case.store.path)) == raw
    assert case.store.profile() == saved


def test_old_purchase_receipt_stays_stale_and_its_payment_is_not_replayed(case):
    envelope = message(case)
    case.service.purchase(envelope, case.store)
    def old(state):
        state.pop("crystal_spending")
        state["tables"]["game_param2"][0]["StampCardGrade"] = 0
        receipt = state["barracks_history"]["receipts"][0]
        receipt["content"] = case.service.content_version
        document = loads(receipt["response"].encode())
        receipt["response"] = dumps(Container(children=tuple(node for node in document.children if node.name != "PGP2"))).decode()
    overwrite(case, old)
    fail(case, envelope, "barracks_retry_stale")
    case.service.purchase(message(case, tick=1001), case.store)
    assert read_spending(case.store.profile().state)["current_spent"] == 30
    assert wallet_totals(case.store.profile().state)["total"] == 20


@pytest.mark.parametrize("kind", ["missing", "value", "type", "duplicate", "wallet", "counter", "projection", "other_game2"])
def test_cached_purchase_canonical_c1_and_wallet_are_validated(case, kind):
    envelope = message(case)
    case.service.purchase(envelope, case.store)
    def corrupt(state):
        if kind == "counter":
            state["crystal_spending"]["current_spent"] = 31
            return
        if kind == "projection":
            state["tables"]["game_param2"][0]["StampCardGrade"] = 31
            return
        if kind == "other_game2":
            state["tables"]["game_param2"][0]["LogInStampCount"] = True
            return
        receipt = state["barracks_history"]["receipts"][0]
        document = loads(receipt["response"].encode())
        nodes = list(document.children)
        pgp2 = next(node for node in nodes if node.name == "PGP2")
        if kind == "missing":
            nodes.remove(pgp2)
        elif kind == "duplicate":
            nodes.append(pgp2)
        elif kind == "wallet":
            wallet = next(node for node in nodes if node.name == "PPC")
            columns = tuple(Column(node.name, node.type, (49,)) if node.name == "Num" else node for node in wallet.children)
            nodes[nodes.index(wallet)] = Container("PPC", columns)
        else:
            nodes[nodes.index(pgp2)] = Container("PGP2", (Column("A1", "S", ("C1",)),
                Column("A2", "I" if kind == "type" else "S", (30 if kind == "type" else "31",))))
        receipt["response"] = dumps(Container(children=tuple(nodes))).decode()
    overwrite(case, corrupt)
    fail(case, envelope, "barracks_history_invalid")


@pytest.mark.parametrize("number", [35, 36, 41, 43, 44])
def test_all_changed_readback_capacity_checks_leave_purchase_and_spending_uncommitted(case, number):
    if number == 36:
        case.service._config_profile = replace(case.service._config_profile, response_limit=1)
    elif number == 41:
        case.service._unit_profile = replace(case.service._unit_profile, response_limit=1)
    else:
        case.service._purchase_profiles = dict(case.service._purchase_profiles)
        case.service._purchase_profiles[number] = replace(case.service._purchase_profiles[number], response_limit=1)
    fail(case, message(case), "response_too_large")


def test_cached_purchase_still_preflights_full_gameparam2(case):
    envelope = message(case)
    case.service.purchase(envelope, case.store)
    case.service._purchase_profiles = dict(case.service._purchase_profiles)
    case.service._purchase_profiles[35] = replace(case.service._purchase_profiles[35], response_limit=1)
    fail(case, envelope, "barracks_history_invalid")


@pytest.mark.parametrize("kind", ["rank", "balance", "configuration", "baseline"])
def test_rejected_purchase_does_not_record_any_spending(case, kind):
    if kind == "rank":
        change(case, lambda state: state["tables"]["game_param"][0].update(Rank=1, Experience=0))
        code = "barracks_rank_required"
    elif kind == "balance":
        change(case, lambda state: state["tables"].update(crystals=[{"FreeFlag": True, "Date": 1, "Num": 29}]))
        code = "crystal_insufficient"
    elif kind == "configuration":
        change(case, lambda state: state["tables"]["config"][0].update({PURCHASE_FIELD: True}))
        code = "unit_capacity_state_invalid"
    else:
        def legacy(state):
            state.pop("crystal_spending", None)
            state["tables"]["game_param2"][0]["StampCardGrade"] = 50
        change(case, legacy)
        code = "crystal_spending_baseline_required"
    fail(case, message(case), code)


def test_cas_retry_preserves_another_payment_and_captures_clock_once(case, monkeypatch):
    original = case.store.update_state
    writes, reads = [], []
    def clock():
        reads.append(1)
        return NOW
    case.service.bootstrap.clock = clock
    def conflict(state, revision):
        writes.append(revision)
        if len(writes) == 1:
            current = case.store.profile()
            original(spend_crystals(current.state, 2, NOW), current.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    case.service.purchase(message(case), case.store)
    assert len(writes) == 2 and len(reads) == 1
    assert read_spending(case.store.profile().state)["current_spent"] == 32
    assert wallet_totals(case.store.profile().state)["total"] == 48


def test_purchase_and_cached_validation_preserve_existing_wallet_metadata(case):
    def metadata(state):
        for row in state["tables"]["crystals"]:
            row["origin"] = {"keep": [1, 2]}
    change(case, metadata)
    envelope = message(case)
    response = case.service.purchase(envelope, case.store)
    saved = case.store.profile()
    assert all(row["origin"] == {"keep": [1, 2]} for row in saved.state["tables"]["crystals"])
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == saved


def test_overlapping_same_purchase_counts_only_one_thirty_crystal_payment(case, monkeypatch):
    original = case.store.update_state
    barrier, lock, writes = Barrier(2), Lock(), []
    def overlap(state, revision):
        with lock:
            writes.append(revision)
            wait = len(writes) <= 2
        if wait:
            barrier.wait(timeout=10)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", overlap)
    before = case.store.profile()
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: case.service.purchase(envelope, case.store), range(2)))
    assert responses[0] == responses[1]
    assert case.store.profile().revision == before.revision + 1
    assert read_spending(case.store.profile().state)["current_spent"] == 30
    assert wallet_totals(case.store.profile().state)["total"] == 50

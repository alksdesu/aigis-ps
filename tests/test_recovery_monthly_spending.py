from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
import json
from threading import Barrier, Lock

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.crystal_spending import read_spending, spend_crystals, spending_response
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.recovery_service import RecoveryService
from aigis_ps.storage import Store
from test_recovery_service import NOW, asset_root, case, change, fail, message


def overwrite(case, callback):
    state = case.store.profile().state
    callback(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def groups(raw):
    return {node.name: node for node in loads(raw).children}


def test_one_crystal_recovery_counts_once_and_updates_both_native_reads(case):
    before = case.store.profile()
    raw = case.service.recover(message(case), case.store)
    after = case.store.profile()
    assert wallet_totals(before.state)["total"] - wallet_totals(after.state)["total"] == 1
    assert read_spending(after.state)["current_spent"] == 1
    assert groups(raw)["PGP2"] == spending_response(after.state)
    assert after.state["tables"]["game_param2"][0]["StampCardGrade"] == 1
    assert after.state["tables"]["game_param2"][0]["LastLoginDate"] == before.state["tables"]["game_param2"][0]["LastLoginDate"]
    assert after.state["recovery_history"]["receipts"][-1]["content"] == case.service.purchase_content_version
    assert case.service.content_version != case.service.purchase_content_version
    flat = groups(case.bootstrap.game_param2(message(case, number=35), case.store))
    assert flat["C1"].values == (1,)
    keyed = groups(case.bootstrap.game_param2_fields(message(case, number=33), case.store))["PGP2"]
    assert dict(zip(keyed.children[0].values, keyed.children[1].values))["C1"] == "1"


def test_new_month_rotates_only_prior_spend_then_counts_current_payment(case):
    case.service.recover(message(case), case.store)
    case.clock.now = datetime(2026, 12, 1, 4)
    raw = case.service.recover(message(case, 1001), case.store)
    ledger = read_spending(case.store.profile().state)
    assert ledger["period"] == "2026-12"
    assert ledger["previous_spent"] == ledger["current_spent"] == 1
    assert groups(raw)["PGP2"].children[1].values == (str((1 << 16) | 1),)


def test_cached_recovery_never_reads_clock_or_rotates_a_month_after_reopen(case):
    envelope = message(case)
    raw = case.service.recover(envelope, case.store)
    saved = case.store.profile()
    def forbidden():
        raise AssertionError("A cached recovery must not read the clock")
    case.service.clock = forbidden
    assert case.service.recover(envelope, case.store) == raw
    reopened = Store(case.store.path)
    restored = RecoveryService(case.bootstrap, clock=forbidden)
    assert restored.recover(envelope, reopened) == raw
    assert reopened.profile() == saved


def test_old_paid_receipt_is_stale_without_counting_its_prior_payment_again(case):
    envelope = message(case)
    case.service.recover(envelope, case.store)
    def old_state(state):
        state.pop("crystal_spending")
        state["tables"]["game_param2"][0]["StampCardGrade"] = 0
        receipt = state["recovery_history"]["receipts"][0]
        receipt["content"] = case.service.content_version
        root = loads(receipt["response"].encode())
        receipt["response"] = dumps(Container(children=tuple(node for node in root.children if node.name != "PGP2"))).decode()
    overwrite(case, old_state)
    fail(case, envelope, "recovery_retry_stale")
    case.service.recover(message(case, 1001), case.store)
    assert wallet_totals(case.store.profile().state)["total"] == 98
    assert read_spending(case.store.profile().state)["current_spent"] == 1


@pytest.mark.parametrize("kind", ["missing", "value", "type", "duplicate", "wallet", "counter", "projection", "other_game2"])
def test_cached_c1_wallet_and_state_must_be_canonical(case, kind):
    envelope = message(case)
    case.service.recover(envelope, case.store)
    def corrupt(state):
        if kind == "counter":
            state["crystal_spending"]["current_spent"] = 2
            return
        if kind == "projection":
            state["tables"]["game_param2"][0]["StampCardGrade"] = 2
            return
        if kind == "other_game2":
            state["tables"]["game_param2"][0]["LogInStampCount"] = True
            return
        receipt = state["recovery_history"]["receipts"][0]
        root = loads(receipt["response"].encode())
        nodes = list(root.children)
        pgp2 = next(node for node in nodes if node.name == "PGP2")
        if kind == "missing":
            nodes.remove(pgp2)
        elif kind == "duplicate":
            nodes.append(pgp2)
        elif kind == "wallet":
            wallet = next(node for node in nodes if node.name == "PFC")
            fields = tuple(Column(node.name, node.type, (98,)) if node.name == "Num" else node for node in wallet.children)
            nodes[nodes.index(wallet)] = Container("PFC", fields)
        else:
            nodes[nodes.index(pgp2)] = Container("PGP2", (Column("A1", "S", ("C1",)),
                Column("A2", "I" if kind == "type" else "S", (1 if kind == "type" else "2",))))
        receipt["response"] = dumps(Container(children=tuple(nodes))).decode()
    overwrite(case, corrupt)
    fail(case, envelope, "recovery_history_invalid")


@pytest.mark.parametrize("cached", [False, True])
def test_full_gameparam2_capacity_failure_is_atomic(case, cached):
    envelope = message(case)
    if cached:
        case.service.recover(envelope, case.store)
    case.service._load_profiles[35] = replace(case.service._load_profiles[35], response_limit=1)
    fail(case, envelope, "recovery_history_invalid" if cached else "response_too_large")


@pytest.mark.parametrize("kind", ["balance", "recovery_cap", "missing_baseline"])
def test_failed_recovery_does_not_save_new_or_rotated_spending(case, kind):
    if kind == "balance":
        change(case, lambda state: state["tables"].update(crystals=[]))
        code = "crystal_insufficient"
    elif kind == "recovery_cap":
        change(case, lambda state: state["tables"]["game_param"][0].update(NowCharisma=2000))
        code = "recovery_cap_exceeded"
    else:
        def legacy(state):
            state.pop("crystal_spending", None)
            state["tables"]["game_param2"][0]["StampCardGrade"] = 50
        change(case, legacy)
        code = "crystal_spending_baseline_required"
    case.clock.now = datetime(2026, 12, 1, 4)
    fail(case, message(case), code)


def test_free_item_restore_never_initializes_or_changes_spending(case):
    source = case.store.profile().state
    source.pop("crystal_spending", None)
    updated = case.service.restore_state(source, NOW)
    assert "crystal_spending" not in updated
    assert updated["tables"]["crystals"] == source["tables"]["crystals"]
    assert updated["tables"]["game_param2"] == source["tables"]["game_param2"]
    paid = spend_crystals(source, 3, NOW)
    restored = case.service.restore_state(paid, NOW)
    assert read_spending(restored) == read_spending(paid)
    assert restored["tables"]["crystals"] == paid["tables"]["crystals"]


def test_cas_retry_preserves_another_consumer_and_uses_one_instant(case, monkeypatch):
    original = case.store.update_state
    calls, clocks = [], []
    def clock():
        clocks.append(1)
        return NOW
    case.service.clock = clock
    def conflict(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = case.store.profile()
            original(spend_crystals(current.state, 2, NOW), current.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    case.service.recover(message(case), case.store)
    assert len(calls) == 2 and len(clocks) == 1
    assert read_spending(case.store.profile().state)["current_spent"] == 3
    assert wallet_totals(case.store.profile().state)["total"] == 97


def test_concurrent_identical_recoveries_commit_one_payment_and_one_count(case, monkeypatch):
    original = case.store.update_state
    barrier, lock, calls = Barrier(2), Lock(), []
    def overlap(state, revision):
        with lock:
            calls.append(revision)
            wait = len(calls) <= 2
        if wait:
            barrier.wait(timeout=10)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", overlap)
    envelope = message(case)
    before = case.store.profile()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: case.service.recover(envelope, case.store), range(2)))
    assert results[0] == results[1]
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert read_spending(after.state)["current_spent"] == 1
    assert wallet_totals(after.state)["total"] == 99

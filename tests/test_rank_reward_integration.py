from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import datetime
import json
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import decode_wire, encode_wire
from aigis_ps.rank_rewards import RankRewardService
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
PENDING_IDS = {"143", "144", "145", "180"}


def set_rank(state, bootstrap, rank):
    state["tables"]["game_param"][0].update(
        Rank=rank, Experience=sum(row["NextExp"] for row in bootstrap.rank_rows[:rank - 1]))


@pytest.fixture
def factory(bundle, tmp_path):
    assert bundle.rank_rewards is not None, bundle.rank_reward_error
    serial = 0

    def build(rank=1, *, legacy=False, mutate=None):
        nonlocal serial
        serial += 1
        local = copy(bundle)
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        bootstrap.clock = lambda: NOW
        presents.bootstrap = bootstrap
        bootstrap.present_provider = presents.response
        # Rank-only: production 116 also dispatches pass, login and vote rewards via the shared mail service
        service = RankRewardService(bootstrap, bundle.player_state, presents, bundle.rank_rewards.catalog)
        local.bootstrap, local.presents, local.rank_rewards = bootstrap, presents, service
        state = local.initialize(bootstrap.initial_state())
        set_rank(state, bootstrap, rank)
        if legacy:
            state.pop("rank_rewards", None)
        if mutate is not None:
            mutate(state)
        state["integration_sentinel"] = {"keep": [1, 2]}
        directory = tmp_path / str(serial)
        handlers = bootstrap.handlers() | local.player_state.handlers() | presents.handlers() | service.handlers()

        def application():
            return create_app(directory, initial_state=state, handlers=handlers,
                              state_initializer=local.initialize)

        app = application()
        return SimpleNamespace(bundle=local, bootstrap=bootstrap, presents=presents, service=service,
            state=state, app=app, store=app.state.store, application=application, directory=directory)

    return build


def message(case, tick=1000, *, shop=0, number=0, premium=0, extra=(), omit=()):
    columns = [Column("SHOP", "I", (shop,)), Column("NUM", "I", (number,)),
               Column("DmmPremiumReceiverId", "I", (premium,))]
    return request(case.store, 116, [column for column in columns if column.name not in omit] + list(extra), tick=tick)


def post(client, envelope, expected=200):
    response = client.post("/api/" + envelope.profile.route,
                           content=encode_wire(envelope.xml, envelope.profile.request_key))
    assert response.status_code == expected, response.text
    if expected != 200:
        return response
    document = loads(decode_wire(response.content, envelope.profile.response_key))
    assert len({node.name for node in document.children}) == len(document.children)
    return response.content, {node.name: node for node in document.children}


def mail_ids(nodes):
    if "PPT" not in nodes:
        return ()
    return next(column.values for column in nodes["PPT"].children if column.name == "PresentID")


def entries(case):
    return case.store.profile().state["rank_rewards"]["entries"]


def stored_mails(case):
    return case.store.profile().state["tables"]["presents"]


def replace_without_revision(case, state):
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def reject_unchanged(case, client, envelope, status, code=None):
    before = case.store.profile()
    response = post(client, envelope, status)
    if code is not None:
        assert response.json()["error"]["code"] == code
    assert case.store.profile() == before
    return response


def test_new_account_starts_at_zero_and_empty_delivery_replays_after_reopen(factory):
    case = factory()
    ledger = case.store.profile().state["rank_rewards"]
    assert ledger["baseline_rank"] == ledger["observed_rank"] == 0
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, nodes = post(client, envelope)
        assert set(nodes) == {"SYSTEM"}
        saved = case.store.profile()
        assert saved.state["rank_rewards"]["observed_rank"] == 1
        assert saved.state["rank_rewards"]["entries"] == {}
        assert saved.state["rank_reward_delivery"]["mail_ids"] == []
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
    reopened = case.application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
    assert reopened.state.store.profile() == saved


def test_real_rank_twenty_five_delivers_two_selection_configs_and_preserves_inventory(factory):
    case = factory(25)
    before = case.store.profile()
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, nodes = post(client, envelope)
        assert set(nodes) == {"SYSTEM", "PPT"}
        saved = case.store.profile()
        expected = {str(bonus.bonus_id) for bonus in case.service.catalog.between(0, 25)}
        assert set(entries(case)) == expected
        assert {key for key, entry in entries(case).items() if entry["status"] == "pending"} == {"143", "144", "180"}
        by_id = {row["PresentID"]: row for row in stored_mails(case)}
        for bonus_id, config_id in (("78", 1), ("107", 2)):
            identifiers = entries(case)[bonus_id]["mail_ids"]
            assert len(identifiers) == 1
            row = by_id[identifiers[0]]
            assert (row["Type"], row["Content"], row["SelectionConfigID"]) == (20, 1, config_id)
        assert set(mail_ids(nodes)) == set(by_id)
        assert saved.state["rank_reward_delivery"]["mail_ids"] == list(mail_ids(nodes))
        assert saved.state["player_state"] == before.state["player_state"]
        assert saved.state["integration_sentinel"] == before.state["integration_sentinel"]
        for name, value in before.state["tables"].items():
            if name != "presents":
                assert saved.state["tables"][name] == value
        _, listing = post(client, request(case.store, 42, ()))
        listed = {column.name: column for column in listing["PPT"].children}
        assert all(listed[column.name] == column for column in nodes["PPT"].children)
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved


def test_old_account_baselines_current_rank_and_only_new_crossings_are_eligible(factory):
    case = factory(25, legacy=True)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, nodes = post(client, message(case))
        assert set(nodes) == {"SYSTEM"}
        old = case.store.profile()
        assert old.state["rank_rewards"] == {"schema_version": 1, "baseline_rank": 25,
                                             "observed_rank": 25, "entries": {}}
        assert stored_mails(case) == []
        advanced = deepcopy(old.state)
        set_rank(advanced, case.bootstrap, 30)
        case.store.update_state(advanced, old.revision)
        _, nodes = post(client, message(case, 1001))
        assert set(entries(case)) == {str(bonus.bonus_id) for bonus in case.service.catalog.between(25, 30)}
        assert entries(case)["9"]["status"] == "delivered"
        assert all(row["Type"] != 20 for row in stored_mails(case))
        assert set(mail_ids(nodes)) == {row["PresentID"] for row in stored_mails(case)}


def test_full_current_catalog_retains_four_missing_pools_without_blocking_other_rewards(factory):
    case = factory(1100)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, nodes = post(client, message(case))
        rows = entries(case)
        assert len(rows) == 118
        assert {key for key, entry in rows.items() if entry["status"] == "pending"} == PENDING_IDS
        assert sum(entry["status"] == "delivered" for entry in rows.values()) == 114
        assert len(stored_mails(case)) == len(mail_ids(nodes)) == 114
        assert {"94", "129", "179"}.isdisjoint(rows)
        for key in PENDING_IDS:
            assert rows[key]["reason"] and rows[key]["mail_ids"] == []
        initial_mails = deepcopy(stored_mails(case))
        _, second = post(client, message(case, 1001))
        assert set(second) == {"SYSTEM"}
        assert stored_mails(case) == initial_mails
        assert case.store.profile().state["rank_reward_delivery"]["mail_ids"] == []
    reopened = case.application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert set(post(client, message(case, 1002))[1]) == {"SYSTEM"}
    assert reopened.state.store.profile().state["tables"]["presents"] == initial_mails


@pytest.mark.parametrize("kwargs,status", [
    ({"shop": 1}, 400), ({"number": 1}, 400), ({"premium": 1}, 501),
    ({"omit": ("SHOP",)}, 400), ({"omit": ("NUM",)}, 400),
    ({"omit": ("DmmPremiumReceiverId",)}, 400),
    ({"extra": (Column("UnknownReward", "I", (1,)),)}, 400),
    ({"extra": (Column("SHOP", "I", (0,)),)}, 400),
    ({"extra": (Container("RT"),)}, 400),
])
def test_invalid_rank_claim_requests_never_observe_or_deliver(factory, kwargs, status):
    case = factory(25)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject_unchanged(case, client, message(case, **kwargs), status)


def test_dmm_membership_rewards_stay_unsupported_without_observing_or_delivering(factory):
    case = factory(25)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject_unchanged(case, client, message(case, premium=1), 501, "rank_reward_membership_unsupported")


def test_rank_and_experience_mismatch_is_rejected_before_any_delivery(factory):
    case = factory(25)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state["tables"]["game_param"][0]["Experience"] = 0
    case.store.update_state(state, profile.revision)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject_unchanged(case, client, message(case), 503, "invalid_player_rank")


@pytest.mark.parametrize("method", ["seed_new", "initialize"])
def test_rank_initialization_rejects_inconsistent_real_player_state(factory, method):
    case = factory(25)
    state = deepcopy(case.store.profile().state)
    state["tables"]["game_param"][0]["Experience"] = 0
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        getattr(case.service, method)(state)
    assert caught.value.code == "invalid_player_rank"
    assert state == before


def test_cached_receipt_with_matching_hash_still_validates_rank_and_experience(factory):
    import aigis_ps.rank_rewards as module

    case = factory(25)
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        state = deepcopy(case.store.profile().state)
        state["tables"]["game_param"][0]["Experience"] = 0
        receipt = state["rank_reward_history"]["receipts"][-1]
        receipt["state"] = module._state_digest(state)
        replace_without_revision(case, state)
        reject_unchanged(case, client, envelope, 503, "invalid_player_rank")


def test_cached_receipt_rejects_rank_below_observed_even_with_matching_state_hash(factory):
    import aigis_ps.rank_rewards as module

    case = factory(25)
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        state = deepcopy(case.store.profile().state)
        ledger = deepcopy(state["rank_rewards"])
        delivery = deepcopy(state["rank_reward_delivery"])
        assert ledger["observed_rank"] == 25
        set_rank(state, case.bootstrap, 20)
        receipt = state["rank_reward_history"]["receipts"][-1]
        receipt["state"] = module._state_digest(state)
        assert state["rank_rewards"] == ledger
        assert state["rank_reward_delivery"] == delivery
        replace_without_revision(case, state)
        reject_unchanged(case, client, envelope, 409, "rank_reward_rank_backwards")


def test_claiming_rank_selection_ticket_does_not_make_reward_deliverable_again(factory):
    case = factory(10)
    original = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, original)
        identifier = entries(case)["78"]["mail_ids"][0]
        card_id = case.presents.selection_tickets.catalog.get(1).candidates[0].card_id
        selecting = request(case.store, 1, (Container("RT", (Container("PT2", (
            Column("PresentID", "I", (identifier,)), Column("SelectID", "I", (1,)),
            Column("SelectCardID", "I", (card_id,)))),)),), tick=1001)
        _, selected = post(client, selecting)
        assert selected["PPU"].children[0].values == (card_id,)
        reject_unchanged(case, client, original, 409)
        _, response = post(client, message(case, 1002))
        assert set(response) == {"SYSTEM"}
        assert entries(case)["78"]["mail_ids"] == [identifier]
        assert all(row["PresentID"] != identifier for row in stored_mails(case))


def test_same_request_concurrently_returns_one_atomic_delivery(factory, monkeypatch):
    case = factory(25)
    original_update = case.store.update_state
    gate, lock, count = Barrier(2), Lock(), 0

    def synchronized(state, expected_revision):
        nonlocal count
        with lock:
            count += 1
            first = count <= 2
        if first:
            gate.wait(timeout=10)
        return original_update(state, expected_revision)

    monkeypatch.setattr(case.store, "update_state", synchronized)
    before, envelope = case.store.profile(), message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(post, client, envelope) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
    assert results[0][0] == results[1][0]
    saved = case.store.profile()
    assert saved.revision == before.revision + 1
    identifiers = [identifier for entry in entries(case).values() for identifier in entry["mail_ids"]]
    assert len(identifiers) == len(set(identifiers)) == len(stored_mails(case))


def test_cas_retry_preserves_concurrent_unrelated_state_without_duplicate_mail(factory, monkeypatch):
    case = factory(25)
    original_update = case.store.update_state
    count = 0

    def race(state, expected_revision):
        nonlocal count
        count += 1
        if count == 1:
            latest = case.store.profile()
            external = deepcopy(latest.state)
            external["concurrent_sentinel"] = {"retained": True}
            original_update(external, latest.revision)
        return original_update(state, expected_revision)

    monkeypatch.setattr(case.store, "update_state", race)
    before = case.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, nodes = post(client, message(case))
    saved = case.store.profile()
    assert count == 2 and saved.revision == before.revision + 2
    assert saved.state["concurrent_sentinel"] == {"retained": True}
    assert len(stored_mails(case)) == len(set(mail_ids(nodes)))
    assert set(mail_ids(nodes)) == {row["PresentID"] for row in stored_mails(case)}


def test_forged_receipt_mail_ids_and_response_do_not_replace_independent_delivery(factory):
    case = factory(25)
    profile = case.store.profile()
    updated, unrelated = case.presents.enqueue(profile.state, [
        {"kind": "gold", "amount": 7, "description": "unrelated reward"}])
    case.store.update_state(updated, profile.revision)
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        saved = case.store.profile()
        state = deepcopy(saved.state)
        receipt = state["rank_reward_history"]["receipts"][-1]
        receipt["mail_ids"] = [unrelated[0]["PresentID"]]
        receipt["response"] = case.service.response(saved, saved.state, receipt["mail_ids"]).decode("utf-8")
        assert state["rank_reward_delivery"]["mail_ids"] != receipt["mail_ids"]
        replace_without_revision(case, state)
        reject_unchanged(case, client, envelope, 503)


def test_delivery_mismatch_is_rejected_even_with_recomputed_receipt_state_digest(factory):
    import aigis_ps.rank_rewards as module

    case = factory(25)
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        state = deepcopy(case.store.profile().state)
        state["rank_reward_delivery"]["mail_ids"] = []
        receipt = state["rank_reward_history"]["receipts"][-1]
        receipt["state"] = module._state_digest(state)
        replace_without_revision(case, state)
        reject_unchanged(case, client, envelope, 503)


@pytest.mark.parametrize("failure", ["after_first_enqueue", "full_listing", "response"])
def test_delivery_faults_do_not_save_partial_mail_or_eligibility(factory, monkeypatch, failure):
    case = factory(25)
    enqueue, preflight = case.presents.enqueue, case.service._preflight

    def fail():
        raise ProtocolError("rank_integration_fault", "Injected transaction failure", 503)

    if failure == "after_first_enqueue":
        def broken_enqueue(state, rewards):
            if state["tables"]["presents"]:
                fail()
            return enqueue(state, rewards)
        monkeypatch.setattr(case.presents, "enqueue", broken_enqueue)
    elif failure == "full_listing":
        def broken_preflight(state, profile=None):
            if state["tables"]["presents"]:
                fail()
            return preflight(state, profile)
        monkeypatch.setattr(case.service, "_preflight", broken_preflight)
    else:
        monkeypatch.setattr(case.service, "response", lambda *args: fail())
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject_unchanged(case, client, message(case), 503, "rank_integration_fault")


def test_evicted_old_request_cannot_redeliver_or_overwrite_latest_manifest(factory, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    case = factory()
    original = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, original)
        for tick in range(1001, 1018):
            assert set(post(client, message(case, tick))[1]) == {"SYSTEM"}
        assert len(case.store.profile().state["rank_reward_history"]["receipts"]) == 16
        reject_unchanged(case, client, original, 409, "rank_reward_request_stale")
    assert any("request 116" in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)


def test_watermark_history_upgrades_and_accepts_an_earlier_client_time(factory):
    case = factory(25)
    envelope = message(case, 2000)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, _ = post(client, envelope)
        state = deepcopy(case.store.profile().state)
        history = state["rank_reward_history"]
        state["rank_reward_history"] = {"schema_version": 1, "last_client_time": 2000,
            "seen_keys": list(history["processed_keys"]),
            "receipts": [{**entry, "client_time": 2000} for entry in history["receipts"]]}
        replace_without_revision(case, state)
        saved = case.store.profile()
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        assert set(post(client, message(case, 1000))[1]) == {"SYSTEM"}
    history = case.store.profile().state["rank_reward_history"]
    assert history["schema_version"] == 2
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]


def test_valid_but_old_receipt_content_version_is_stale_without_redelivery(factory):
    case = factory(25)
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        state = deepcopy(case.store.profile().state)
        state["rank_reward_history"]["receipts"][-1]["content"] = "0" * 64
        replace_without_revision(case, state)
        reject_unchanged(case, client, envelope, 409, "rank_reward_retry_stale")


@pytest.mark.parametrize("change", ["bad_ledger", "bad_receipt", "bad_state"])
def test_corrupt_rank_history_never_redelivers(factory, change):
    case = factory(25)
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        state = deepcopy(case.store.profile().state)
        if change == "bad_ledger":
            state["rank_rewards"]["entries"]["78"]["status"] = "unclaimed"
        elif change == "bad_receipt":
            state["rank_reward_history"]["receipts"][-1]["response"] = "\ud800"
        else:
            state["integration_sentinel"]["keep"].append(3)
        replace_without_revision(case, state)
        reject_unchanged(case, client, envelope, 503)

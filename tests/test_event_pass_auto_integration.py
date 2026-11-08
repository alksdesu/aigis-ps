from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
import hashlib
import json
from threading import Barrier, Lock
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_rules import EventPassAutomationRules
from aigis_ps.event_pass_service import AUTOMATION_KEY, REBIND_OPERATION, EventPassService, pass_identity
from aigis_ps.event_pass_state import DEFINITION_CHANGED, LEDGER_KEY as PASS_LEDGER, rebind_definition
from aigis_ps.presents import MAX_PRESENTS
import aigis_ps.rank_rewards as rank_module
from aigis_ps.rank_rewards import (DELIVERY_KEY as RANK_DELIVERY, HISTORY_NAMESPACE as RANK_HISTORY,
                                   RankRewardService)
from test_event_pass_integration import NOW, post, rows
from test_quest_service import bundle, request


EARLIER_IDENTITY = hashlib.sha256(b"earlier event pass identity").hexdigest()


@pytest.fixture
def factory(bundle, tmp_path):
    serial = 0

    def build(*, all_passes=False, points=25, premium=True, active=False, legacy=False, rules=None):
        nonlocal serial
        serial += 1
        assert bundle.event_passes is not None, bundle.event_pass_error
        assert bundle.rank_rewards.event_passes is bundle.event_passes
        local = copy(bundle)
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        bootstrap.clock = lambda: NOW
        presents.bootstrap = bootstrap
        bootstrap.present_provider = presents.response
        local.bootstrap, local.presents = bootstrap, presents
        case = SimpleNamespace(bundle=local, bootstrap=bootstrap, presents=presents,
                               directory=tmp_path / str(serial), state=None)

        def configure(*, active=False, legacy=False, rules=None):
            catalog = copy(bundle.event_passes.catalog)
            if active:
                catalog._passes = tuple(replace(entry, status=1) for entry in catalog.passes)
                catalog._by_id = MappingProxyType({entry.item_id: entry for entry in catalog.passes})
                catalog._content_version = hashlib.sha256((catalog.content_version + ":test-active").encode()).hexdigest()
            passes = EventPassService(bootstrap, local.player_state, presents, local.possessions,
                                     catalog, automation_rules=rules)
            rank = RankRewardService(bootstrap, local.player_state, presents, bundle.rank_rewards.catalog,
                                     event_passes=None if legacy else passes)
            local.event_passes, local.rank_rewards = passes, rank
            bootstrap.event_pass_provider = passes.response
            case.passes, case.service, case.entry = passes, rank, catalog.passes[0]

        def application():
            handlers = bootstrap.handlers() | local.player_state.handlers() | presents.handlers()
            handlers |= local.event_passes.handlers() | local.rank_rewards.handlers()
            return create_app(case.directory, initial_state=case.state,
                              state_initializer=local.initialize, handlers=handlers)

        configure(active=active, legacy=legacy, rules=rules)
        state = local.initialize(bootstrap.initial_state())
        grants = {}
        for entry in case.passes.catalog.passes if all_passes else (case.entry,):
            if points:
                grants[entry.item_id] = points
            if premium:
                grants[entry.premium_item_id] = 1
        if grants:
            state = local.possessions.credit(state, grants)
        state["integration_sentinel"] = {"keep": [1, 2]}
        case.state, case.configure, case.application = state, configure, application
        case.app = application()
        case.store = case.app.state.store
        return case

    return build


def login(case, tick=1000):
    return request(case.store, 116, (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)),
                                   Column("DmmPremiumReceiverId", "I", (0,))), tick=tick)


def manual(case, tick=1000):
    return request(case.store, 159, (Column("EPI", "S", (case.entry.item_id,)),), tick=tick)


def reopen(case):
    case.app = case.application()
    case.store = case.app.state.store


def reject(case, client, envelope, status, code=None):
    before = case.store.profile()
    reply = post(client, envelope, status)
    if code is not None:
        assert reply.json()["error"]["code"] == code
    assert case.store.profile() == before


def overwrite(case, state):
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def test_real_six_closed_passes_deliver_all_264_rewards_with_category_seven(factory):
    case = factory(all_passes=True, points=200)
    initial = case.store.profile()
    envelope = login(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "PPT", "EventPassStatus"}
        saved = case.store.profile()
        marker = saved.state[RANK_DELIVERY]
        assert marker["schema_version"] == 2 and marker["rank_mail_ids"] == []
        assert set(marker["event_pass_mail_ids"]) == {entry.item_id for entry in case.passes.catalog.passes}
        all_rewards = [reward for entry in saved.state[PASS_LEDGER]["passes"].values()
                       for track in entry["deliveries"].values() for delivery in track.values()
                       for reward in delivery["reward_ids"]]
        assert len(all_rewards) == len(set(all_rewards)) == 264
        assert set(all_rewards) == {reward.reward_id for entry in case.passes.catalog.passes for reward in entry.rewards}
        mails = rows(response["PPT"])
        assert all(row["A9"] == 7 for row in mails)
        assert {column.name: column.type.value for column in response["PPT"].children}["A9"] == "I"
        assert marker["mail_ids"] == [row["PresentID"] for row in mails]
        assert saved.state["tables"]["possessions"] == initial.state["tables"]["possessions"]
        assert saved.state["tables"]["crystals"] == initial.state["tables"]["crystals"]
        assert saved.state["tables"]["game_param"] == initial.state["tables"]["game_param"]
        assert saved.state["player_state"] == initial.state["player_state"]
        assert saved.state["integration_sentinel"] == initial.state["integration_sentinel"]
        assert AUTOMATION_KEY not in saved.state
        _, listing = post(client, request(case.store, 42, ()))
        listed = {column.name: column for column in listing["PPT"].children}
        assert all(listed[column.name] == column for column in response["PPT"].children)
        _, database = post(client, request(case.store, 137, ()))
        assert database["EventPassStatus"] == response["EventPassStatus"]
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
    reopen(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, listing = post(client, request(case.store, 42, ()))
        listed = {column.name: column for column in listing["PPT"].children}
        assert all(listed[column.name] == column for column in response["PPT"].children)
        assert post(client, envelope)[0] == wire
        _, repeated = post(client, login(case, 1001))
        assert set(repeated) == {"SYSTEM"}
        assert case.presents.rows(case.store.profile().state) == case.presents.rows(saved.state)


def test_manual_normal_then_automatic_premium_share_one_delivery_history(factory):
    case = factory(active=True, premium=False)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, manual(case))
        first = case.store.profile()
        normal = deepcopy(first.state[PASS_LEDGER]["passes"][case.entry.item_id]["deliveries"]["normal"])
        old_ids = {row["PresentID"] for row in case.presents.rows(first.state)}
        updated = case.bundle.possessions.credit(first.state, {case.entry.premium_item_id: 1})
        case.store.update_state(updated, first.revision)
    case.configure()
    reopen(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, response = post(client, login(case, 1001))
        saved = case.store.profile()
        journal = saved.state[PASS_LEDGER]["passes"][case.entry.item_id]
        assert journal["deliveries"]["normal"] == normal
        assert rows(response["EventPassStatus"]) == [{"ID": case.entry.item_id,
            "ReceivedNormalPoint": 20, "ReceivedPremiumPoint": 20}]
        assert not old_ids.intersection(row["PresentID"] for row in rows(response["PPT"]))
        _, listing = post(client, request(case.store, 42, ()))
        categories = {row["PresentID"]: row["A9"] for row in rows(listing["PPT"])}
        assert all(categories[identifier] == 0 for identifier in old_ids)
        assert all(row["A9"] == 7 for row in rows(response["PPT"]))
    case.configure(active=True)
    reopen(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        before = case.store.profile()
        _, status = post(client, manual(case, 1002))
        assert set(status) == {"SYSTEM", "EventPassStatus"}
        assert rows(status["EventPassStatus"]) == [{"ID": case.entry.item_id,
            "ReceivedNormalPoint": 20, "ReceivedPremiumPoint": 20}]
        assert case.store.profile() == before


def test_a_changed_pass_identity_refuses_159_while_116_keeps_it_pending_until_rebound(factory, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    case = factory(active=True, premium=False)
    pass_id = case.entry.item_id
    top = max(reward.required_point for reward in case.entry.rewards if not reward.premium)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, manual(case))
        claimed = case.store.profile()
        forged = deepcopy(claimed.state)
        forged[PASS_LEDGER]["passes"][pass_id]["definition"] = EARLIER_IDENTITY
        forged = case.bundle.possessions.credit(forged, {pass_id: top})
        case.store.update_state(forged, claimed.revision)
        reject(case, client, manual(case, 1001), 503, DEFINITION_CHANGED)
    refused = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert any("159" in message and pass_id in message and REBIND_OPERATION in message for message in refused)
    caplog.clear()
    case.configure()
    reopen(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, response = post(client, login(case, 1002))
        held = case.store.profile()
        assert set(response) == {"SYSTEM"}
        assert held.state[AUTOMATION_KEY]["pending"] == {pass_id: DEFINITION_CHANGED}
        assert held.state[PASS_LEDGER] == forged[PASS_LEDGER]
        assert held.state["tables"]["event_pass_status"] == forged["tables"]["event_pass_status"]
        assert held.state["tables"]["presents"] == forged["tables"]["presents"]
        assert any(pass_id in record.getMessage() and REBIND_OPERATION in record.getMessage()
                   and record.levelname == "WARNING" for record in caplog.records)
        rebound = rebind_definition(held.state, pass_id, pass_identity(case.entry))
        case.store.update_state(rebound, held.revision)
        _, delivered = post(client, login(case, 1003))
        saved = case.store.profile()
        assert saved.state[AUTOMATION_KEY]["pending"] == {}
        assert rows(delivered["EventPassStatus"]) == [{"ID": pass_id, "ReceivedNormalPoint": top,
                                                       "ReceivedPremiumPoint": 0}]
        assert all(row["A9"] == 7 for row in rows(delivered["PPT"]))


def test_rank_and_automatic_rewards_keep_distinct_notification_categories(factory):
    case = factory()
    before = case.store.profile()
    state = deepcopy(before.state)
    state["tables"]["game_param"][0].update(
        Rank=10, Experience=sum(row["NextExp"] for row in case.bootstrap.rank_rows[:9]))
    case.store.update_state(state, before.revision)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, response = post(client, login(case))
    saved = case.store.profile()
    marker = saved.state[RANK_DELIVERY]
    rank_ids = set(marker["rank_mail_ids"])
    pass_ids = {identifier for identifiers in marker["event_pass_mail_ids"].values()
                for identifier in identifiers}
    assert rank_ids and pass_ids and not rank_ids.intersection(pass_ids)
    categories = {row["PresentID"]: row["A9"] for row in rows(response["PPT"])}
    assert all(categories[identifier] == 0 for identifier in rank_ids)
    assert all(categories[identifier] == 7 for identifier in pass_ids)
    assert set(marker["mail_ids"]) == rank_ids | pass_ids


def test_legacy_marker_one_replays_without_running_new_automation(factory, monkeypatch):
    case = factory(legacy=True)
    envelope = login(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM"}
        saved = case.store.profile()
        assert saved.state[RANK_DELIVERY]["schema_version"] == 1
    case.configure()
    reopen(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        def no_clock():
            raise AssertionError("Legacy receipt must not perform automatic settlement")
        monkeypatch.setattr(case.bootstrap, "clock", no_clock)
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        monkeypatch.setattr(case.bootstrap, "clock", lambda: NOW)
        _, response = post(client, login(case, 1001))
        assert "EventPassStatus" in response
        assert case.store.profile().state[RANK_DELIVERY]["schema_version"] == 2


def test_full_mailbox_keeps_entire_pass_pending_until_a_new_request_has_space(factory):
    case = factory()
    before = case.store.profile()
    crowded, additions = case.presents.enqueue(before.state,
        [{"kind": "gold", "amount": 1, "description": "existing"}] * (MAX_PRESENTS - 1))
    case.store.update_state(crowded, before.revision)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        envelope = login(case)
        _, response = post(client, envelope)
        assert set(response) == {"SYSTEM"}
        pending = case.store.profile()
        assert pending.state[AUTOMATION_KEY]["pending"] == {case.entry.item_id: "present_capacity"}
        assert pending.state["tables"]["event_pass_status"] == []
        assert pending.state[PASS_LEDGER]["passes"] == {}
        assert pending.state["tables"]["presents"] == crowded["tables"]["presents"]
        assert pending.state["last_present_id"] == crowded["last_present_id"]
        selection = Container("RT", (Container("PT2", (Column("PresentID", "I",
            tuple(row["PresentID"] for row in additions)),)),))
        post(client, request(case.store, 117, (selection,), tick=1001))
        reject(case, client, envelope, 409, "rank_reward_retry_stale")
        _, delivered = post(client, login(case, 1002))
        saved = case.store.profile()
        assert saved.state[AUTOMATION_KEY]["pending"] == {}
        assert all(row["A9"] == 7 for row in rows(delivered["PPT"]))
        assert rows(delivered["EventPassStatus"])[0]["ReceivedNormalPoint"] == 20
        assert saved.state["tables"]["possessions"] == crowded["tables"]["possessions"]


def test_automatic_receipt_replay_does_not_read_clock_or_repeat_settlement(factory, monkeypatch):
    case = factory()
    envelope = login(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, _ = post(client, envelope)
        saved = case.store.profile()
        def no_clock():
            raise AssertionError("Current automatic receipt must not read time")
        monkeypatch.setattr(case.bootstrap, "clock", no_clock)
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved


def test_disabled_automatic_rule_preserves_ep_and_manual_history(factory):
    case = factory(rules=EventPassAutomationRules(enabled=False))
    before = case.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, response = post(client, login(case))
    after = case.store.profile()
    assert set(response) == {"SYSTEM"}
    assert after.state["tables"]["event_pass_status"] == []
    assert after.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert after.state[PASS_LEDGER] == before.state[PASS_LEDGER]


@pytest.mark.parametrize("stage", ["notification", "137"])
def test_nonrecoverable_failure_does_not_commit_rank_or_pass_state(factory, monkeypatch, stage):
    case = factory()
    if stage == "notification":
        def fail(*args):
            raise ProtocolError("injected_auto_failure", "Notification write failed", 503)
        monkeypatch.setattr(case.presents, "set_notification_category", fail)
    else:
        original = case.bootstrap._configuration_response
        def fail(profile):
            if profile.state["tables"]["event_pass_status"]:
                raise ProtocolError("injected_auto_failure", "Startup encoding failed", 503)
            return original(profile)
        monkeypatch.setattr(case.bootstrap, "_configuration_response", fail)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject(case, client, login(case), 503, "injected_auto_failure")


def test_cas_retry_freezes_time_and_preserves_external_change(factory, monkeypatch):
    case = factory()
    before = case.store.profile()
    ticks, conflicts = [], []
    monkeypatch.setattr(case.bootstrap, "clock", lambda: ticks.append(NOW) or NOW)
    original = case.store.update_state
    def update(state, revision):
        if not conflicts:
            conflicts.append(True)
            concurrent = deepcopy(case.store.profile().state)
            concurrent["concurrent_sentinel"] = [1, 2]
            original(concurrent, revision)
            raise ValueError("conflict")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, response = post(client, login(case))
    saved = case.store.profile()
    assert ticks == [NOW] and saved.revision == before.revision + 2
    assert saved.state["concurrent_sentinel"] == [1, 2]
    assert len(saved.state[RANK_HISTORY]["receipts"]) == 1
    assert saved.state[RANK_DELIVERY]["mail_ids"] == [row["PresentID"] for row in rows(response["PPT"])]


def test_concurrent_same_116_commits_one_pass_delivery(factory, monkeypatch):
    case = factory()
    before = case.store.profile()
    barrier, lock, arrivals = Barrier(2), Lock(), 0
    original = case.store.update_state
    def update(state, revision):
        nonlocal arrivals
        with lock:
            arrivals += 1
            wait = arrivals <= 2
        if wait:
            barrier.wait(timeout=15)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    envelope = login(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: post(client, envelope)[0], range(2)))
    saved = case.store.profile()
    assert results[0] == results[1] and saved.revision == before.revision + 1
    assert len(saved.state[RANK_HISTORY]["receipts"]) == 1
    assert len(case.presents.rows(saved.state)) == len(saved.state[RANK_DELIVERY]["mail_ids"])


@pytest.mark.parametrize("tamper", ["receipt_subset", "wrong_pass", "rank_overlap", "category"])
def test_forged_mixed_delivery_or_current_mail_is_rejected(factory, tamper):
    case = factory()
    envelope = login(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        saved = case.store.profile()
        state = deepcopy(saved.state)
        marker = state[RANK_DELIVERY]
        receipt = state[RANK_HISTORY]["receipts"][-1]
        if tamper == "receipt_subset":
            receipt["mail_ids"] = receipt["mail_ids"][:1]
            receipt["response"] = case.service.response(saved, state, receipt["mail_ids"],
                event_pass_mail_ids=marker["event_pass_mail_ids"]).decode()
        elif tamper == "wrong_pass":
            marker["event_pass_mail_ids"][case.passes.catalog.passes[1].item_id] = marker["event_pass_mail_ids"].pop(case.entry.item_id)
        elif tamper == "rank_overlap":
            marker["rank_mail_ids"] = marker["mail_ids"][:1]
        else:
            state["tables"]["presents"][0]["NotificationCategory"] = 0
        receipt["state"] = rank_module._state_digest(state)
        overwrite(case, state)
        reject(case, client, envelope, 503)

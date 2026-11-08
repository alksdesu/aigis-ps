from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from threading import Barrier, Lock
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
import aigis_ps.event_pass_service as module
from aigis_ps.event_pass_service import EventPassService
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.protocol import decode_wire, encode_wire
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def factory(bundle, tmp_path):
    serial = 0

    def build(index=0, *, active=True, points=200, premium=True, historical=False):
        nonlocal serial
        serial += 1
        assert bundle.event_passes is not None, bundle.event_pass_error
        local = copy(bundle)
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        bootstrap.clock = lambda: NOW
        presents.bootstrap = bootstrap
        recovery = copy(presents.recovery)
        recovery.bootstrap, recovery.clock = bootstrap, bootstrap.clock
        presents.recovery = recovery
        tickets = copy(bundle.item_summon_tickets)
        tickets.bootstrap, tickets.presents = bootstrap, presents
        tickets.randbelow = lambda _: 0
        presents.item_tickets = tickets
        catalog = copy(bundle.event_passes.catalog)
        if active:
            catalog._passes = tuple(replace(entry, status=1) for entry in catalog.passes)
            catalog._by_id = MappingProxyType({entry.item_id: entry for entry in catalog.passes})
            catalog._content_version = hashlib.sha256((catalog.content_version + ":test-active").encode()).hexdigest()
        service = EventPassService(bootstrap, bundle.player_state, presents, bundle.possessions, catalog)
        bootstrap.present_provider, bootstrap.event_pass_provider = presents.response, service.response
        local.bootstrap, local.presents, local.event_passes = bootstrap, presents, service
        local.item_summon_tickets = tickets
        state = bootstrap.initial_state()
        if historical:
            state["tables"]["event_pass_status"] = [
                {"ID": "event_pass_retired", "ReceivedNormalPoint": 90, "ReceivedPremiumPoint": 50}]
        state = local.initialize(state)
        entry = catalog.passes[index]
        grants = {entry.item_id: points} if points else {}
        if premium:
            grants[entry.premium_item_id] = 1
        if grants:
            state = bundle.possessions.credit(state, grants)
        state["integration_sentinel"] = {"keep": [1, 2]}
        directory = tmp_path / str(serial)
        handlers = bootstrap.handlers() | local.player_state.handlers() | presents.handlers() | service.handlers()

        def application():
            return create_app(directory, initial_state=state, state_initializer=local.initialize, handlers=handlers)

        app = application()
        return SimpleNamespace(bundle=local, bootstrap=bootstrap, presents=presents, catalog=catalog,
            service=service, entry=entry, app=app, store=app.state.store, application=application)

    return build


def message(case, tick=1000, *, identifiers=None, columns=None):
    if columns is None:
        columns = (Column("EPI", "S", (case.entry.item_id,) if identifiers is None else identifiers),)
    return request(case.store, 159, columns, tick=tick)


def mail_request(case, identifier, tick=2000):
    return request(case.store, 1, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", (identifier,)),)),)),), tick=tick)


def post(client, envelope, expected=200):
    reply = client.post("/api/" + envelope.profile.route,
                        content=encode_wire(envelope.xml, envelope.profile.request_key))
    assert reply.status_code == expected, reply.text
    if expected != 200:
        return reply
    parsed = loads(decode_wire(reply.content, envelope.profile.response_key))
    return reply.content, {node.name: node for node in parsed.children}


def reject(case, client, envelope, status, code=None):
    before = case.store.profile()
    reply = post(client, envelope, status)
    if code is not None:
        assert reply.json()["error"]["code"] == code
    assert case.store.profile() == before


def rows(node):
    return [dict(zip((column.name for column in node.children), values))
            for values in zip(*(column.values for column in node.children))]


def credit(case, grants):
    before = case.store.profile()
    case.store.update_state(case.bundle.possessions.credit(before.state, grants), before.revision)


def overwrite(case, state):
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


@pytest.mark.parametrize("index", range(6))
def test_original_six_passes_are_inactive_without_losing_stored_inventory(factory, index):
    case = factory(index, active=False)
    assert case.entry.status == 2
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject(case, client, message(case), 409, "event_pass_inactive")
        before = case.store.profile()
        _, status = post(client, request(case.store, 158, ()))
        assert set(status) == {"SYSTEM", "EventPassStatus"}
        assert rows(status["EventPassStatus"]) == []
        assert case.store.profile() == before


@pytest.mark.parametrize("index", range(6))
def test_all_real_reward_rows_deliver_both_tracks_and_round_trip(factory, index):
    case = factory(index, historical=True)
    before = case.store.profile()
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "PPT", "EventPassStatus"}
        expected = max(reward.required_point for reward in case.entry.rewards)
        assert rows(response["EventPassStatus"]) == [{"ID": case.entry.item_id,
            "ReceivedNormalPoint": expected, "ReceivedPremiumPoint": expected}]
        saved = case.store.profile()
        journal = saved.state["event_pass_state"]["passes"][case.entry.item_id]["deliveries"]
        all_rewards = [identifier for track in journal.values() for delivery in track.values()
                       for identifier in delivery["reward_ids"]]
        assert len(all_rewards) == len(set(all_rewards)) == len(case.entry.rewards)
        assert set(all_rewards) == {reward.reward_id for reward in case.entry.rewards}
        identifiers = [row["PresentID"] for row in rows(response["PPT"])]
        assert saved.state["event_pass_delivery"]["mail_ids"] == identifiers
        assert len(identifiers) == len(set(identifiers))
        assert saved.state["tables"]["possessions"] == before.state["tables"]["possessions"]
        assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
        assert saved.state["tables"]["game_param"] == before.state["tables"]["game_param"]
        assert saved.state["player_state"] == before.state["player_state"]
        assert saved.state["integration_sentinel"] == before.state["integration_sentinel"]
        _, listing = post(client, request(case.store, 42, ()))
        listed = {column.name: column for column in listing["PPT"].children}
        assert all(listed[column.name] == column for column in response["PPT"].children)
        _, status = post(client, request(case.store, 158, ()))
        _, database = post(client, request(case.store, 137, ()))
        assert status["EventPassStatus"] == database["EventPassStatus"]
        assert rows(status["EventPassStatus"])[0] == {"ID": "event_pass_retired",
            "ReceivedNormalPoint": 90, "ReceivedPremiumPoint": 50}
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
    reopened = case.application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
    assert reopened.state.store.profile() == saved


def test_late_premium_unlock_backfills_only_premium_without_consuming_points(factory):
    case = factory(points=25, premium=False)
    initial = case.store.profile().state["tables"]["possessions"]
    old_packet = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, response = post(client, old_packet)
        assert rows(response["EventPassStatus"])[0] == {"ID": case.entry.item_id,
            "ReceivedNormalPoint": 20, "ReceivedPremiumPoint": 0}
        first = case.store.profile()
        normal = deepcopy(first.state["event_pass_state"]["passes"][case.entry.item_id]["deliveries"]["normal"])
        credit(case, {case.entry.premium_item_id: 1})
        _, response = post(client, message(case, 1001))
        assert rows(response["EventPassStatus"])[0]["ReceivedPremiumPoint"] == 20
        saved = case.store.profile()
        assert saved.state["event_pass_state"]["passes"][case.entry.item_id]["deliveries"]["normal"] == normal
        assert next(row for row in saved.state["tables"]["possessions"] if row["ItemID"] == case.entry.item_id) == initial[0]
        reject(case, client, old_packet, 409, "event_pass_retry_stale")
        before = case.store.profile()
        wire, status = post(client, message(case, 1002))
        assert set(status) == {"SYSTEM", "EventPassStatus"}
        assert rows(status["EventPassStatus"]) == [{"ID": case.entry.item_id,
            "ReceivedNormalPoint": 20, "ReceivedPremiumPoint": 20}]
        assert post(client, message(case, 1002))[0] == wire
        assert case.store.profile() == before


def test_unreached_threshold_replies_with_empty_status_over_http(factory):
    case = factory(points=0, premium=True)
    before = case.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, status = post(client, message(case))
        assert set(status) == {"SYSTEM", "EventPassStatus"}
        assert rows(status["EventPassStatus"]) == []
        assert client.get("/local/requests").json()["requests"][0]["status"] == 200
    assert case.store.profile() == before


def test_menu_naming_the_first_active_pass_while_a_later_one_is_claimable_reads_status(factory):
    points = 25
    case = factory(1, points=points, premium=False)
    first = min(entry.item_id for entry in case.catalog.passes if case.catalog.is_eligible(entry, NOW))
    assert first != case.entry.item_id
    reached = max(reward.required_point for reward in case.entry.rewards
                  if not reward.premium and reward.required_point <= points)
    before = case.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, status = post(client, message(case, identifiers=(first,)))
        assert set(status) == {"SYSTEM", "EventPassStatus"}
        assert rows(status["EventPassStatus"]) == []
        assert case.store.profile() == before
        _, claimed = post(client, message(case, 1001))
        assert set(claimed) == {"SYSTEM", "PPT", "EventPassStatus"}
        assert rows(claimed["EventPassStatus"]) == [{"ID": case.entry.item_id,
            "ReceivedNormalPoint": reached, "ReceivedPremiumPoint": 0}]


def test_real_cooking_and_summon_rewards_remain_mail_until_individually_claimed(factory):
    case = factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, message(case))
        issued = case.presents.rows(case.store.profile().state)
        cooking = next(row for row in issued if row["Type"] == 22
                       and case.presents.recovery_catalog.supports_item(row["ItemID"]))
        ticket = next(row for row in issued if row["Type"] == 22
                      and case.presents.item_tickets.catalog.supports_item(row["ItemID"]))
        assert cooking["Content"] == ticket["Content"] == 1
        old = case.store.profile()
        _, restored = post(client, mail_request(case, cooking["PresentID"]))
        assert "PGP" in restored
        after_cooking = case.store.profile()
        assert after_cooking.state["tables"]["game_param"][0]["NowCharisma"] > old.state["tables"]["game_param"][0]["NowCharisma"]
        assert after_cooking.state["tables"]["crystals"] == old.state["tables"]["crystals"]
        definition = case.presents.item_tickets.validate_item(ticket["ItemID"])
        _, summoned = post(client, mail_request(case, ticket["PresentID"], 2001))
        fields = {field.name: field.values for field in summoned["PPU"].children}
        assert len(fields) == 21 and len(fields["A7"]) == definition.count
        saved = case.store.profile()
        assert len(saved.state["player_state"]["units"]) == len(old.state["player_state"]["units"]) + definition.count
        assert saved.state["event_pass_state"] == old.state["event_pass_state"]
        _, listing = post(client, request(case.store, 42, ()))
        remaining = {row["PresentID"] for row in rows(listing["PPT"])}
        assert remaining == {row["PresentID"] for row in issued} - {cooking["PresentID"], ticket["PresentID"]}


@pytest.mark.parametrize("fault", ["after_first_group", "42", "137", "response"])
def test_every_delivery_failure_rolls_back_mail_watermarks_and_inventory(factory, monkeypatch, fault):
    case = factory(points=25)
    if fault == "after_first_group":
        original = case.presents._prepare_reward_rows
        calls = 0

        def prepare(rewards, existing_count):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ProtocolError("injected_delivery", "Injected second group failure", 503)
            return original(rewards, existing_count)

        monkeypatch.setattr(case.presents, "_prepare_reward_rows", prepare)
    elif fault == "response":
        def fail(*args):
            raise ProtocolError("injected_delivery", "Injected response failure", 503)
        monkeypatch.setattr(case.service, "_response", fail)
    else:
        original = module.encode_response

        def encode(profile, xml):
            if int(fault) in profile.registration_keys:
                raise ProtocolError("injected_delivery", "Injected readback failure", 503)
            return original(profile, xml)

        monkeypatch.setattr(module, "encode_response", encode)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject(case, client, message(case), 503, "injected_delivery")
    if fault == "after_first_group":
        assert calls == 2


def test_same_threshold_group_cannot_partially_fill_the_last_mailbox_slot(factory):
    case = factory(points=10, premium=False)
    group = [reward for reward in case.entry.rewards if not reward.premium and reward.required_point == 10]
    assert len(group) >= 2
    before = case.store.profile()
    state, _ = case.presents.enqueue(before.state, [{"kind": "gold", "amount": 1, "description": "capacity"}] * (MAX_PRESENTS - 1))
    case.store.update_state(state, before.revision)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject(case, client, message(case), 409, "present_capacity")


def test_exact_retry_does_not_read_clock_but_new_action_after_end_is_inactive(factory, monkeypatch):
    case = factory(points=10)
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, _ = post(client, envelope)
        saved = case.store.profile()

        def no_clock():
            raise AssertionError("Cached delivery must not read time")

        monkeypatch.setattr(case.bootstrap, "clock", no_clock)
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        monkeypatch.setattr(case.bootstrap, "clock", lambda: case.entry.end_at + timedelta(seconds=1))
        reject(case, client, message(case, 1001), 409, "event_pass_inactive")


def test_cas_retry_uses_one_clock_and_preserves_concurrent_state(factory, monkeypatch):
    case = factory(points=25)
    calls, conflicts = [], []
    monkeypatch.setattr(case.bootstrap, "clock", lambda: calls.append(NOW) or NOW)
    original = case.store.update_state

    def update(state, revision):
        if not conflicts:
            conflicts.append(True)
            before = case.store.profile()
            concurrent = deepcopy(before.state)
            concurrent["concurrent_sentinel"] = {"keep": True}
            original(concurrent, before.revision)
            raise ValueError("simulated compare-and-swap conflict")
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", update)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, response = post(client, message(case))
    saved = case.store.profile()
    assert calls == [NOW] and conflicts == [True]
    assert saved.state["concurrent_sentinel"] == {"keep": True}
    assert len(saved.state["event_pass_history"]["receipts"]) == 1
    assert len(case.presents.rows(saved.state)) == len(rows(response["PPT"]))


def test_concurrent_identical_claims_commit_one_delivery(factory, monkeypatch):
    case = factory(points=25)
    barrier, lock = Barrier(2), Lock()
    original = case.store.update_state
    arrivals = 0

    def update(state, revision):
        nonlocal arrivals
        with lock:
            arrivals += 1
            wait = arrivals <= 2
        if wait:
            barrier.wait(timeout=15)
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", update)
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: post(client, envelope)[0], range(2)))
    assert results[0] == results[1]
    saved = case.store.profile()
    assert len(saved.state["event_pass_history"]["receipts"]) == 1
    assert len(case.presents.rows(saved.state)) == len(saved.state["event_pass_delivery"]["mail_ids"])


@pytest.mark.parametrize("tamper", ["receipt_subset", "marker_subset", "watermark"])
def test_forged_delivery_or_receipt_cannot_be_replayed(factory, tamper):
    case = factory(points=25)
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        saved = case.store.profile()
        state = deepcopy(saved.state)
        receipt = state["event_pass_history"]["receipts"][-1]
        if tamper == "receipt_subset":
            receipt["mail_ids"] = receipt["mail_ids"][:1]
            receipt["response"] = case.service._response(saved, state, case.entry.item_id, receipt["mail_ids"]).decode()
        elif tamper == "marker_subset":
            state["event_pass_delivery"]["mail_ids"] = state["event_pass_delivery"]["mail_ids"][:1]
            receipt["state"] = module._state_digest(state)
        else:
            state["tables"]["event_pass_status"][0]["ReceivedNormalPoint"] = 0
            receipt["state"] = module._state_digest(state)
        overwrite(case, state)
        reject(case, client, envelope, 503)


@pytest.mark.parametrize("shape", ["missing", "empty", "duplicate_id", "two_passes", "integer", "unknown_field", "duplicate_column"])
def test_strict_claim_shape_is_rejected_without_writes(factory, shape):
    case = factory(points=10)
    epi = Column("EPI", "S", (case.entry.item_id,))
    columns = {
        "missing": (), "empty": (Column("EPI", "S", ()),),
        "duplicate_id": (Column("EPI", "S", (case.entry.item_id,) * 2),),
        "two_passes": (Column("EPI", "S", (case.entry.item_id, case.catalog.passes[1].item_id)),),
        "integer": (Column("EPI", "I", (1,)),),
        "unknown_field": (epi, Column("RewardID", "I", (1,))),
        "duplicate_column": (epi, epi),
    }[shape]
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject(case, client, message(case, columns=columns), 400)


def test_status_read_has_no_business_fields_and_unknown_pass_cannot_be_claimed(factory):
    case = factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject(case, client, request(case.store, 158, (Column("EPI", "S", (case.entry.item_id,)),)), 400)
        reject(case, client, message(case, identifiers=("event_pass_missing",)), 501, "event_pass_unknown")

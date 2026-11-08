from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
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
from aigis_ps.login_reward_service import LoginRewardService
import aigis_ps.login_reward_service as module
from aigis_ps.vote_cooking_service import validate_vote_cooking_grants
from test_evolution_integration import post
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


class SyntheticCookingSource:
    content_version = "b" * 64

    def __init__(self, presents, *, eligible=True):
        self.presents, self.eligible = presents, eligible
        self.times = []

    def _ready(self):
        pass

    def initialize(self, state):
        return deepcopy(state)

    def status(self):
        return {"ready": True}

    def is_ready(self):
        return True

    def settle(self, state, now):
        self.times.append(now)
        updated = deepcopy(state)
        if not self.eligible or updated.get("vote_cooking", {}).get("status") == "delivered":
            return updated, []
        try:
            candidate, rows = self.presents.enqueue(updated, [
                {"kind": "gold", "amount": 1, "description": "integration fixture"}])
        except ProtocolError as error:
            if error.code != "present_capacity":
                raise
            updated["vote_cooking"] = {"status": "pending", "grants": []}
            return updated, []
        grant = {"item_id": "item_vote_016001", "group_id": 1, "definition": "a" * 64,
                 "card_id": 10, "count": 1, "receive_text_id": 1,
                 "mail_ids": [row["PresentID"] for row in rows]}
        candidate["vote_cooking"] = {"status": "delivered", "grants": [deepcopy(grant)]}
        return candidate, [grant]

    def validate_receipt(self, state, grants):
        validate_vote_cooking_grants(grants)
        frozen = state.get("vote_cooking", {}).get("grants", [])
        if any(grant not in frozen for grant in grants):
            raise ProtocolError("vote_cooking_receipt_invalid", "Fixture proof differs from its ledger", 503)

    def response(self, grants):
        return Container("VoteGiftParam", (
            Column("CardId", "I", tuple(grant["card_id"] for grant in grants)),
            Column("Count", "I", tuple(grant["count"] for grant in grants)),
            Column("ReceiveTextID", "I", tuple(grant["receive_text_id"] for grant in grants))))


@pytest.fixture
def factory(bundle, tmp_path, monkeypatch):
    serial = 0

    def build(*, eligible=True, level=0, helper=True):
        nonlocal serial
        serial += 1
        clock = SimpleNamespace(now=NOW, calls=0)

        def now():
            clock.calls += 1
            return clock.now

        monkeypatch.setattr(bundle.bootstrap, "clock", now)
        options = {name: getattr(bundle, name) for threshold, name in (
            (2, "event_logins"), (3, "comeback_logins"), (4, "purchased_logins"), (5, "daily_gifts"))
            if level >= threshold}
        source = SyntheticCookingSource(bundle.presents, eligible=eligible)

        def service(*, cooking=helper):
            return LoginRewardService(bundle.bootstrap, bundle.player_state, bundle.presents,
                bundle.login_rewards.catalog, clock=now, **options,
                **({"vote_cooking": source} if cooking else {}))

        current = service()
        initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
        path = tmp_path / f"vote-cooking-{serial}"

        def application():
            return create_app(path, initial_state=initial, state_initializer=lambda state: current.initialize(state, NOW),
                handlers=bundle.bootstrap.handlers() | current.handlers())

        app = application()
        return SimpleNamespace(service=current, source=source, clock=clock, app=app, store=app.state.store,
                               application=application, make_service=service, bundle=bundle)

    return build


def message(case, tick=1000):
    return request(case.store, 115, (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,))), tick=tick)


def response_groups(payload):
    return {node.name: node for node in loads(payload).children}


def test_source_is_atomic_with_login_mail_notification_and_restart(factory):
    case = factory()
    before = case.store.profile()
    clock_calls = case.clock.calls
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, reply = post(client, envelope)
        saved = case.store.profile()
        assert saved.revision == before.revision + 1
        assert saved.state["login_reward_delivery"]["schema_version"] == 6
        grant = saved.state["vote_cooking"]["grants"][0]
        assert saved.state["login_reward_history"]["receipts"][-1]["sources"]["vote_cooking_grants"] == [grant]
        assert grant["mail_ids"][0] in reply["PPT"].children[0].values
        assert reply["VoteGiftParam"] == case.source.response([grant])
        assert saved.state["tables"]["game_param"] == before.state["tables"]["game_param"]
        assert len(case.source.times) == 1 and case.source.times[0] == NOW
        assert case.clock.calls == clock_calls + 1
        assert post(client, envelope)[0] == wire and case.store.profile() == saved
    reopened = case.application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
    assert reopened.state.store.profile() == saved and len(case.source.times) == 1


@pytest.mark.parametrize("level", range(1, 6))
def test_every_previous_login_schema_replays_without_new_source_or_clock(factory, level, monkeypatch):
    case = factory(level=level, helper=False)
    envelope = message(case)
    old = case.service
    wire = old.receive(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["login_reward_delivery"]["schema_version"] == level
    current = case.make_service(cooking=True)
    assert current._daily_receive_content_version == old.receive_content_version

    def unexpected(*args, **kwargs):
        raise AssertionError("Legacy exact replay must not consult the new cooking source")

    for name in ("_ready", "initialize", "settle", "validate_receipt", "response"):
        monkeypatch.setattr(case.source, name, unexpected)
    monkeypatch.setattr(current, "clock", unexpected)
    assert current.receive(envelope, case.store) == wire
    assert old.receive(envelope, case.store) == wire
    assert case.store.profile() == saved


def test_empty_source_clears_notification_without_creating_ledger(factory):
    case = factory(eligible=False)
    envelope = message(case)
    payload = case.service.receive(envelope, case.store)
    saved = case.store.profile()
    assert response_groups(payload)["VoteGiftParam"] == case.source.response([])
    assert "vote_cooking" not in saved.state
    assert saved.state["login_reward_delivery"]["vote_cooking_grants"] == []
    assert saved.state["login_reward_delivery"]["daily_gift_grants"] == []
    assert case.service.receive(envelope, case.store) == payload
    assert case.store.profile() == saved


def test_new_request_does_not_deliver_again_or_repeat_notification(factory):
    case = factory()
    case.service.receive(message(case), case.store)
    before = case.store.profile().state
    payload = case.service.receive(message(case, 1001), case.store)
    after = case.store.profile().state
    assert after["tables"]["presents"] == before["tables"]["presents"]
    assert after["vote_cooking"] == before["vote_cooking"]
    assert after["login_reward_delivery"]["vote_cooking_grants"] == []
    assert response_groups(payload)["VoteGiftParam"] == case.source.response([])


def test_full_mailbox_retains_pending_then_delivers_after_space_is_available(factory):
    case = factory(eligible=False)
    case.service.receive(message(case), case.store)
    saved = case.store.profile()
    state, _ = case.bundle.presents.enqueue(saved.state,
        [{"kind": "gold", "amount": 1, "description": "capacity fixture"}]
        * (500 - len(saved.state["tables"]["presents"])))
    case.store.update_state(state, saved.revision)
    case.source.eligible = True
    payload = case.service.receive(message(case, 1001), case.store)
    pending = case.store.profile()
    assert pending.state["vote_cooking"]["status"] == "pending"
    assert len(pending.state["tables"]["presents"]) == 500
    assert response_groups(payload)["VoteGiftParam"] == case.source.response([])
    state = deepcopy(pending.state)
    state["tables"]["presents"].pop()
    case.store.update_state(state, pending.revision)
    payload = case.service.receive(message(case, 1002), case.store)
    assert case.store.profile().state["vote_cooking"]["status"] == "delivered"
    assert len(case.store.profile().state["tables"]["presents"]) == 500
    assert "VoteGiftParam" in response_groups(payload)


@pytest.mark.parametrize("failure", ("source", "notification", "wrong_container", "encoding"))
def test_late_source_or_wire_failure_rolls_back_all_login_rewards(factory, monkeypatch, failure):
    case = factory()
    before = case.store.profile()

    def broken(*args, **kwargs):
        raise ProtocolError("vote_cooking_test_failure", "Injected source failure", 503)

    if failure == "source":
        monkeypatch.setattr(case.source, "settle", broken)
    elif failure == "notification":
        monkeypatch.setattr(case.source, "response", broken)
    elif failure == "wrong_container":
        monkeypatch.setattr(case.source, "response", lambda grants: Container("WrongGiftParam"))
    else:
        original = module.encode_response

        def encode(profile, payload):
            if profile.registration_keys == (115,) and b"VoteGiftParam" in payload:
                broken()
            return original(profile, payload)

        monkeypatch.setattr(module, "encode_response", encode)
    with pytest.raises(ProtocolError):
        case.service.receive(message(case), case.store)
    assert case.store.profile() == before


def test_source_mutation_and_missing_owner_are_rejected(factory):
    case = factory()
    case.source.content_version = "c" * 64
    before = case.store.profile()
    with pytest.raises(ProtocolError, match="dependencies changed"):
        case.service.receive(message(case), case.store)
    assert case.store.profile() == before
    case.source.content_version = "b" * 64
    envelope = message(case)
    case.service.receive(envelope, case.store)
    ownerless = case.make_service(cooking=False)
    with pytest.raises(ProtocolError) as error:
        ownerless.receive(envelope, case.store)
    assert error.value.code == "vote_cooking_service_unavailable"
    with pytest.raises(ProtocolError) as error:
        ownerless.initialize(case.store.profile().state, NOW)
    assert error.value.code == "vote_cooking_service_unavailable"


@pytest.mark.parametrize("field,value", (("card_id", 11), ("item_id", "item_vote_016002"), ("receive_text_id", 2)))
def test_rehashed_receipt_cannot_change_frozen_cooking_source(factory, field, value):
    case = factory()
    envelope = message(case)
    case.service.receive(envelope, case.store)
    saved = case.store.profile()
    state = deepcopy(saved.state)
    state["login_reward_delivery"]["vote_cooking_grants"][0][field] = value
    receipt = state["login_reward_history"]["receipts"][-1]
    receipt["sources"]["vote_cooking_grants"][0][field] = value
    receipt["state"] = module._state_digest(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    with pytest.raises(ProtocolError):
        case.service.receive(envelope, case.store)


def test_duplicate_mail_id_between_base_and_cooking_is_rejected(factory):
    case = factory()
    envelope = message(case)
    case.service.receive(envelope, case.store)
    saved = case.store.profile()
    state = deepcopy(saved.state)
    marker = state["login_reward_delivery"]
    marker["vote_cooking_grants"][0]["mail_ids"] = marker["base_mail_ids"][:1]
    receipt = state["login_reward_history"]["receipts"][-1]
    receipt["sources"]["vote_cooking_grants"] = deepcopy(marker["vote_cooking_grants"])
    receipt["state"] = module._state_digest(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    with pytest.raises(ProtocolError) as error:
        case.service.receive(envelope, case.store)
    assert error.value.code == "login_reward_history_invalid"


@pytest.mark.parametrize("failure", ("missing_column", "wrong_type", "wrong_value", "extra_row", "wrong_order"))
def test_notification_must_match_every_typed_grant_field(factory, monkeypatch, failure):
    case = factory()
    before = case.store.profile()
    original = case.source.response

    def malformed(grants):
        columns = list(original(grants).children)
        if failure == "missing_column":
            columns.pop()
        elif failure == "wrong_type":
            columns[0] = Column("CardId", "S", (str(grants[0]["card_id"]),))
        elif failure == "wrong_value":
            columns[0] = Column("CardId", "I", (grants[0]["card_id"] + 1,))
        elif failure == "extra_row":
            columns = [Column(column.name, "I", column.values * 2) for column in columns]
        else:
            columns.reverse()
        return Container("VoteGiftParam", tuple(columns))

    monkeypatch.setattr(case.source, "response", malformed)
    with pytest.raises(ProtocolError) as error:
        case.service.receive(message(case), case.store)
    assert error.value.code == "login_vote_cooking_response_invalid"
    assert case.store.profile() == before


@pytest.mark.parametrize("field,value", (("count", True), ("count", 0), ("mail_ids", [1, 1]), ("receive_text_id", "1")))
def test_malformed_grants_roll_back_before_login_commit(factory, monkeypatch, field, value):
    case = factory()
    original = case.source.settle
    before = case.store.profile()

    def malformed(state, now):
        updated, grants = original(state, now)
        grants[0][field] = value
        return updated, grants

    monkeypatch.setattr(case.source, "settle", malformed)
    with pytest.raises(ProtocolError):
        case.service.receive(message(case), case.store)
    assert case.store.profile() == before


def test_rank_rewards_without_tracked_votes_do_not_deliver_unrelated_cooking(factory):
    case = factory(level=5)
    columns = (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)), Column("DmmPremiumReceiverId", "I", (0,)))
    envelope = request(case.store, 116, columns)
    payload = case.bundle.rank_rewards.receive(envelope, case.store)
    assert all(not column.values for column in response_groups(payload)["VoteGiftParam"].children)
    assert "vote_cooking" not in case.store.profile().state
    assert case.source.times == []
    case.service.receive(message(case), case.store)
    assert len(case.source.times) == 1
    assert case.store.profile().state["vote_cooking"]["status"] == "delivered"


def test_concurrent_exact_login_requests_share_one_delivery_and_response(factory, monkeypatch):
    case = factory()
    original = case.store.update_state
    barrier, lock = Barrier(2), Lock()
    calls = 0

    def raced(state, revision):
        nonlocal calls
        with lock:
            calls += 1
            wait = calls <= 2
        if wait:
            barrier.wait(timeout=20)
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", raced)
    before = case.store.profile()
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(case.service.receive, envelope, case.store) for _ in range(2)]
        replies = [future.result(timeout=30) for future in futures]
    saved = case.store.profile()
    assert replies[0] == replies[1]
    assert saved.revision == before.revision + 1
    grants = saved.state["vote_cooking"]["grants"]
    assert len(grants) == 1 and len(grants[0]["mail_ids"]) == 1
    assert len(saved.state["login_reward_history"]["receipts"]) == 1

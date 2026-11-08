from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, loads
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.login_reward_service import LoginRewardService
from aigis_ps.vote_tickets import DAILY_ITEM, VoteTicketService
import aigis_ps.login_reward_service as module
from test_evolution_integration import post
from test_quest_service import bundle, request
from test_vote_catalog import vote_sources, build
from test_vote_service import table_rows
from test_vote_transactions import catalog, NOW


@pytest.fixture
def factory(bundle, catalog, tmp_path, monkeypatch):
    serial = 0
    def create(*, level=1, helper=True, now=NOW):
        nonlocal serial
        serial += 1
        clock = SimpleNamespace(now=now, calls=0)
        def current():
            clock.calls += 1
            return clock.now
        monkeypatch.setattr(bundle.bootstrap, "clock", current)
        tickets = VoteTicketService(bundle.possessions, catalog)
        options = {name: getattr(bundle, name) for threshold, name in (
            (2, "event_logins"), (3, "comeback_logins"), (4, "purchased_logins"),
            (5, "daily_gifts"), (6, "vote_cooking")) if level >= threshold}
        def service(*, enabled=helper):
            return LoginRewardService(bundle.bootstrap, bundle.player_state, bundle.presents,
                bundle.login_rewards.catalog, clock=current, **options,
                **({"vote_tickets": tickets} if enabled else {}))
        login = service()
        initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
        initial = bundle.possessions.credit(initial, {"gift_0000_bouquet": 7})
        path = tmp_path / f"tickets-{serial}"
        def application():
            return create_app(path, initial_state=initial, state_initializer=lambda state: login.initialize(state, now),
                handlers=bundle.bootstrap.handlers() | login.handlers())
        app = application()
        return SimpleNamespace(service=login, tickets=tickets, bundle=bundle, clock=clock, catalog=catalog,
            app=app, store=app.state.store, reopen=application, make_service=service)
    return create


def message(case, tick=1000):
    return request(case.store, 115, (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,))), tick=tick)


def groups(payload):
    return {child.name: child for child in loads(payload).children}


def inventory(payload):
    return {row["ItemID"]: row["Count"] for row in table_rows(groups(payload)["POSSESSION"])}


def test_login_sends_full_inventory_and_persists_exact_ticket_receipt(factory):
    case = factory()
    before = case.store.profile()
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, reply = post(client, envelope)
        saved = case.store.profile()
        assert saved.revision == before.revision + 1
        assert {row["ItemID"]: row["Count"] for row in table_rows(reply["POSSESSION"])} == {
            "gift_0000_bouquet": 7, DAILY_ITEM: 1}
        marker = saved.state["login_reward_delivery"]
        assert marker["schema_version"] == 7
        assert marker["vote_ticket_observation"] == saved.state["vote_tickets"]
        assert marker["vote_cooking_grants"] == []
        assert table_rows(reply["VoteGiftParam"]) == []
        assert "vote_history" not in saved.state and "vote_expiry" not in saved.state
        assert saved.state["tables"]["vote_history"] == before.state["tables"]["vote_history"]
        calls = case.clock.calls
        assert post(client, envelope)[0] == wire and case.store.profile() == saved
        assert case.clock.calls == calls
    with TestClient(case.reopen(), base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire and case.store.profile() == saved


def test_same_day_does_not_refill_consumed_daily_ticket(factory):
    case = factory()
    case.service.receive(message(case), case.store)
    profile = case.store.profile()
    state, daily, ordinary = case.tickets.consume(profile.state, case.catalog.groups[0].item_id, 1, case.clock.now)
    assert (daily, ordinary) == (1, 0)
    case.store.update_state(state, profile.revision)
    payload = case.service.receive(message(case, 1001), case.store)
    assert inventory(payload)[DAILY_ITEM] == 0
    assert case.store.profile().state["vote_tickets"]["status"] == "consumed"
    case.clock.now = datetime(2026, 11, 2, 4, tzinfo=JST)
    payload = case.service.receive(message(case, 1002), case.store)
    assert inventory(payload)[DAILY_ITEM] == 1
    assert case.store.profile().state["vote_tickets"]["ticket_day"] == "2026-11-02"


def test_unused_daily_ticket_expires_with_explicit_zero_inventory(factory):
    case = factory()
    case.service.receive(message(case), case.store)
    case.clock.now = case.catalog.event.end_at + timedelta(days=1)
    payload = case.service.receive(message(case, 1001), case.store)
    saved = case.store.profile()
    assert inventory(payload)[DAILY_ITEM] == 0
    assert saved.state["vote_tickets"]["status"] == "expired"
    assert saved.state["login_reward_delivery"]["vote_ticket_observation"] == saved.state["vote_tickets"]


def test_outside_event_keeps_absent_ledger_and_returns_other_inventory(factory):
    case = factory(now=NOW - timedelta(hours=2))
    payload = case.service.receive(message(case), case.store)
    saved = case.store.profile()
    assert inventory(payload) == {"gift_0000_bouquet": 7}
    assert "vote_tickets" not in saved.state
    assert saved.state["login_reward_delivery"]["vote_ticket_observation"] is None
    assert case.service.receive(message(case), case.store) == payload


@pytest.mark.parametrize("level", range(1, 7))
def test_old_login_schemas_keep_exact_response_without_ticket_source(factory, monkeypatch, level):
    case = factory(level=level, helper=False)
    envelope = message(case)
    response = case.service.receive(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["login_reward_delivery"]["schema_version"] == level
    current = case.make_service(enabled=True)
    assert current._cooking_receive_content_version == case.service.receive_content_version
    def unexpected(*args, **kwargs):
        pytest.fail("Old login retry must not consult the new daily ticket source")
    for name in ("_ready", "initialize", "observe", "validate_observation"):
        monkeypatch.setattr(case.tickets, name, unexpected)
    monkeypatch.setattr(current, "clock", unexpected)
    assert current.receive(envelope, case.store) == response
    assert case.store.profile() == saved


@pytest.mark.parametrize("registration", (35, 42, 115, 129, 137))
def test_response_failure_rolls_back_daily_ticket_and_login_rewards(factory, monkeypatch, registration):
    case = factory()
    before = case.store.profile()
    original = module.encode_response
    def fail(profile, response):
        if profile.registration_keys == (registration,):
            raise ProtocolError("ticket_test_failure", "Ticket wire failure", 503)
        return original(profile, response)
    monkeypatch.setattr(module, "encode_response", fail)
    with pytest.raises(ProtocolError, match="Ticket wire failure"):
        case.service.receive(message(case), case.store)
    assert case.store.profile() == before


def test_full_mailbox_rolls_back_new_day_and_ticket_then_allows_same_day_refresh(factory):
    case = factory(now=NOW - timedelta(hours=2))
    case.service.receive(message(case), case.store)
    saved = case.store.profile()
    state, _ = case.bundle.presents.enqueue(saved.state,
        [{"kind": "gold", "amount": 1, "description": "capacity fixture"}] * (500 - len(case.bundle.presents.rows(saved.state))))
    case.store.update_state(state, saved.revision)
    case.clock.now = NOW
    payload = case.service.receive(message(case, 1001), case.store)
    assert inventory(payload)[DAILY_ITEM] == 1
    assert len(case.bundle.presents.rows(case.store.profile().state)) == 500
    case.clock.now = datetime(2026, 11, 2, 4, tzinfo=JST)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.receive(message(case, 1002), case.store)
    assert caught.value.code == "present_capacity"
    assert case.store.profile() == before


def test_ticket_observation_requires_same_service_and_permanent_ledger(factory):
    case = factory()
    case.service.receive(message(case), case.store)
    saved = case.store.profile()
    without = case.make_service(enabled=False)
    with pytest.raises(ProtocolError, match="requires its service"):
        without.initialize(saved.state, NOW)
    damaged = deepcopy(saved.state)
    del damaged["vote_tickets"]
    with pytest.raises(ProtocolError): case.service.initialize(damaged, NOW)
    damaged = deepcopy(saved.state)
    del damaged["login_reward_delivery"]["vote_ticket_observation"]
    with pytest.raises(ProtocolError): case.service.initialize(damaged, NOW)


@pytest.mark.parametrize("field,value", (("schema_version", True), ("status", "unknown"), ("ticket_day", "bad"),
                                       ("origin", "unknown"), ("last_time", True)))
def test_malformed_ticket_receipts_fail_without_mutation(factory, field, value):
    case = factory()
    case.service.receive(message(case), case.store)
    saved = case.store.profile()
    damaged = deepcopy(saved.state)
    damaged["login_reward_history"]["receipts"][-1]["sources"]["vote_ticket_observation"][field] = value
    before = deepcopy(damaged)
    with pytest.raises(ProtocolError): case.service._receipts(damaged, saved.revision)
    assert damaged == before


def test_inventory_provider_mismatch_rolls_back_login_and_tickets(factory, monkeypatch):
    case = factory()
    before = case.store.profile()
    monkeypatch.setattr(case.bundle.bootstrap, "possession_provider", lambda state: case.bundle.possessions.response(state, ("gift_0000_bouquet",)))
    with pytest.raises(ProtocolError, match="Startup inventory differs"):
        case.service.receive(message(case), case.store)
    assert case.store.profile() == before

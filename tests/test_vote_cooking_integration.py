from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.login_reward_service import LoginRewardService
from aigis_ps.vote_cooking_service import VoteCookingService
from aigis_ps.vote_service import VoteService
import aigis_ps.login_reward_service as login_module
import aigis_ps.vote_transactions as vote_module
from test_evolution_integration import post
from test_present_recovery_integration import claim
from test_quest_service import bundle, request
from test_vote_catalog import vote_sources, build
from test_vote_service import table_rows
from test_vote_transactions import catalog, make_case, vote, NOW


def attach(case):
    case.cooking = VoteCookingService(case.catalog, case.bundle.vote_cooking.recipes, case.presents)
    case.service = VoteService(case.bootstrap, case.bundle.player_state, case.catalog,
        possessions=case.bundle.possessions, presents=case.presents, cooking=case.cooking)
    case.transactions = case.service.transactions
    case.bootstrap.vote_provider = case.service.response
    case.login = LoginRewardService(case.bootstrap, case.bundle.player_state, case.presents,
        case.bundle.login_rewards.catalog, clock=case.bootstrap.clock, vote_cooking=case.cooking)
    return case


@pytest.fixture
def factory(make_case):
    def create():
        case = attach(make_case())
        def application():
            return create_app(case.store.path.parent, initial_state=case.store.profile().state,
                state_initializer=lambda state: case.login.initialize(case.service.initialize(state), case.clock.now),
                handlers=case.bootstrap.handlers() | case.presents.handlers() | case.service.handlers() | case.login.handlers())
        case.reopen = application
        case.app = application()
        case.store = case.app.state.store
        return case
    return create


def login(case, tick=2000):
    return request(case.store, 115, (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,))), tick=tick)


def test_real_vote_login_notification_claim_and_restart(factory, monkeypatch):
    case = factory()
    card = next(row.card_id for row in case.catalog.candidates if row.group_id == 1
                and case.cooking.recipes.definition(row.card_id).cooking_kind == 0)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        envelope = vote(case, 1, card_id=card)
        wire, _ = post(client, envelope)
        voted = case.store.profile()
        journal = voted.state["vote_cooking"]["groups"][case.group.item_id]
        assert journal["counts"] == {str(card): 1}
        assert voted.state["vote_last_action"]["cooking_definition"] == journal["definition"]
        assert post(client, envelope)[0] == wire and case.store.profile() == voted
        _, early = post(client, login(case))
        assert table_rows(early["VoteGiftParam"]) == []
        case.clock.now = case.group.result_start
        message = login(case, tick=2001)
        wire, result = post(client, message)
        saved = case.store.profile()
        grant = saved.state["login_reward_delivery"]["vote_cooking_grants"][0]
        assert table_rows(result["VoteGiftParam"]) == [{"CardId": card, "Count": 1, "ReceiveTextID": 5028}]
        assert any(row["PresentID"] == grant["mail_ids"][0] and row["A1"] == 21 and row["A2"] == card
                   for row in table_rows(result["PPT"]))
        for field in ("HomeCookingReceivedAmount", "HomeCookingReceivedDate"):
            assert saved.state["tables"]["game_param2"][0][field] == voted.state["tables"]["game_param2"][0][field]
        assert post(client, message)[0] == wire and case.store.profile() == saved
    reopened = case.reopen()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, message)[0] == wire
        before = case.store.profile()
        current = before.state["tables"]["game_param"][0]["NowCharisma"]
        maximum = case.bootstrap.rank_rows[0]["CharismaMax"]
        envelope = claim(case.store, 1, grant["mail_ids"], tick=3000)
        wire, result = post(client, envelope)
        assert "PGP" in result
        claimed = case.store.profile()
        assert claimed.state["tables"]["game_param"][0]["NowCharisma"] == current + maximum
        assert post(client, envelope)[0] == wire and case.store.profile() == claimed
        _, listed = post(client, request(case.store, 42, (), tick=3001))
        assert grant["mail_ids"][0] not in [row["PresentID"] for row in table_rows(listed["PPT"])]
        case.cooking.validate_receipt(claimed.state, [grant])
        _, again = post(client, login(case, tick=3002))
        assert table_rows(again["VoteGiftParam"]) == []
        assert not any(row["Type"] == 21 for row in case.presents.rows(case.store.profile().state))


@pytest.mark.parametrize("registration", (42, 57, 129, 137))
def test_vote_encoding_failure_rolls_back_cooking_and_vote(factory, monkeypatch, registration):
    case = factory()
    before = case.store.profile()
    original = vote_module.encode_response
    def fail(profile, response):
        if profile.registration_keys == (registration,):
            raise ProtocolError("fixture_encoding_failure", "Fixture failure", 503)
        return original(profile, response)
    monkeypatch.setattr(vote_module, "encode_response", fail)
    with pytest.raises(ProtocolError, match="Fixture failure"):
        case.transactions.submit(vote(case, 1), case.store)
    assert case.store.profile() == before and "vote_cooking" not in before.state


@pytest.mark.parametrize("registration", (35, 42, 115))
def test_login_encoding_failure_rolls_back_eligibility_mail_and_notice(factory, monkeypatch, registration):
    case = factory()
    case.transactions.submit(vote(case, 1), case.store)
    case.clock.now = case.group.result_start
    before = case.store.profile()
    original = login_module.encode_response
    def fail(profile, response):
        if profile.registration_keys == (registration,):
            raise ProtocolError("fixture_encoding_failure", "Fixture failure", 503)
        return original(profile, response)
    monkeypatch.setattr(login_module, "encode_response", fail)
    with pytest.raises(ProtocolError, match="Fixture failure"):
        case.login.receive(login(case), case.store)
    assert case.store.profile() == before


def test_previous_vote_receipt_replays_after_enabling_cooking(make_case, monkeypatch):
    case = make_case()
    envelope = vote(case, 1)
    response = case.transactions.submit(envelope, case.store)
    before = case.store.profile()
    committed = before.state["vote_transactions"]["receipts"][-1]["content"]
    assert committed == case.transactions.content_version
    attach(case)
    assert committed != case.transactions.content_version
    monkeypatch.setattr(case.bootstrap, "clock", lambda: pytest.fail("Old retry must not observe the new feature"))
    assert case.transactions.submit(envelope, case.store) == response
    assert case.store.profile() == before and "vote_cooking" not in before.state


@pytest.mark.parametrize("schema", ("_closing_content", "_legacy_content"))
def test_earlier_schema_receipts_replay_after_enabling_cooking(make_case, monkeypatch, schema):
    case = make_case()
    envelope = vote(case, 1)
    response = case.transactions.submit(envelope, case.store)
    profile = case.store.profile()
    earlier = deepcopy(profile.state)
    earlier["vote_transactions"]["receipts"][-1].update(content=getattr(case.transactions, schema)(),
                                                         revision=profile.revision + 1)
    case.store.update_state(earlier, profile.revision)
    before = case.store.profile()
    attach(case)
    monkeypatch.setattr(case.bootstrap, "clock", lambda: pytest.fail("An earlier receipt must not observe time"))
    assert case.transactions.submit(envelope, case.store) == response
    assert case.store.profile() == before and "vote_cooking" not in before.state


def test_lost_cooking_domain_cannot_replay_or_initialize(factory):
    case = factory()
    envelope = vote(case, 1)
    case.transactions.submit(envelope, case.store)
    before = case.store.profile()
    damaged = deepcopy(before.state)
    del damaged["vote_cooking"]
    with pytest.raises(ProtocolError, match="eligibility proof"):
        case.service.initialize(damaged)
    missing = VoteService(case.bootstrap, case.bundle.player_state, case.catalog,
        possessions=case.bundle.possessions, presents=case.presents)
    with pytest.raises(ProtocolError, match="requires its service"):
        missing.initialize(before.state)


@pytest.mark.parametrize("duplicate", (False, True))
def test_concurrent_login_requests_deliver_only_one_group(factory, monkeypatch, duplicate):
    case = factory()
    case.transactions.submit(vote(case, 1), case.store)
    case.clock.now = case.group.result_start
    first = login(case)
    second = first if duplicate else login(case, tick=2001)
    update = case.store.update_state
    barrier, lock, count = Barrier(2), Lock(), 0
    def synchronized(state, revision):
        nonlocal count
        with lock:
            count += 1
            wait = count <= 2
        if wait: barrier.wait(timeout=20)
        return update(state, revision)
    monkeypatch.setattr(case.store, "update_state", synchronized)
    def receive(envelope):
        try:
            return case.login.receive(envelope, case.store)
        except ProtocolError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(receive, (first, second)))
    saved = case.store.profile().state
    assert len([row for row in case.presents.rows(saved) if row["Type"] == 21]) == 1
    assert saved["vote_cooking"]["groups"][case.group.item_id]["status"] == "delivered"
    assert any(isinstance(result, bytes) for result in results)
    if duplicate: assert results[0] == results[1]

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, loads
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.login_reward_service import LoginRewardService
from aigis_ps.rank_rewards import RankRewardService
from aigis_ps.vote_cooking_service import VoteCookingService
from aigis_ps.vote_cooking_notifications import VoteCookingNotifications
from aigis_ps.vote_service import VoteService
from aigis_ps.vote_tickets import DAILY_ITEM
from test_evolution_integration import post
from test_present_recovery_integration import claim
from test_quest_service import bundle, request
from test_vote_catalog import vote_sources, build
from test_vote_service import table_rows
from test_vote_transactions import catalog, make_case, vote, counts
from test_vote_cooking_catalog import relocated_catalog


@pytest.fixture
def factory(make_case):
    def create():
        case = make_case()
        case.cooking = VoteCookingService(case.catalog, case.bundle.vote_cooking.recipes, case.presents)
        case.notifier = VoteCookingNotifications(case.cooking)
        case.service = VoteService(case.bootstrap, case.bundle.player_state, case.catalog,
            possessions=case.bundle.possessions, presents=case.presents, cooking=case.cooking)
        case.transactions = case.service.transactions
        case.bootstrap.vote_provider = case.service.response
        case.tickets = case.transactions.tickets
        case.login = LoginRewardService(case.bootstrap, case.bundle.player_state, case.presents,
            case.bundle.login_rewards.catalog, clock=case.bootstrap.clock, vote_cooking=case.cooking,
            vote_tickets=case.tickets)
        case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents,
            case.bundle.rank_rewards.catalog, vote_tickets=case.tickets, vote_cooking_notifier=case.notifier)
        def initialize(state):
            return case.rank.initialize(case.login.initialize(case.service.initialize(state), case.clock.now))
        def application():
            return create_app(case.store.path.parent, initial_state=case.store.profile().state, state_initializer=initialize,
                handlers=case.bootstrap.handlers() | case.presents.handlers() | case.service.handlers()
                    | case.login.handlers() | case.rank.handlers())
        case.reopen = application
        case.app = application()
        case.store = case.app.state.store
        return case
    return create


def reward_request(case, number, tick=2000):
    fields = (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)))
    if number == 116:
        fields += (Column("DmmPremiumReceiverId", "I", (0,)),)
    return request(case.store, number, fields, tick=tick)


def reply_counts(table):
    return {row["ItemID"]: row["Count"] for row in table_rows(table)}


def upgrade_cooking(case, receive_text_id=5028):
    recipes = relocated_catalog(case.cooking.recipes, receive_text_id)
    case.presents.vote_cooking_catalog = recipes
    case.cooking = VoteCookingService(case.catalog, recipes, case.presents)
    case.notifier = VoteCookingNotifications(case.cooking)
    case.service = VoteService(case.bootstrap, case.bundle.player_state, case.catalog,
        possessions=case.bundle.possessions, presents=case.presents, cooking=case.cooking)
    case.transactions = case.service.transactions
    case.bootstrap.vote_provider = case.service.response
    case.tickets = case.transactions.tickets
    case.login = LoginRewardService(case.bootstrap, case.bundle.player_state, case.presents,
        case.bundle.login_rewards.catalog, clock=case.bootstrap.clock, vote_cooking=case.cooking,
        vote_tickets=case.tickets)
    case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents,
        case.bundle.rank_rewards.catalog, vote_tickets=case.tickets, vote_cooking_notifier=case.notifier)


def cooking_rows(payload):
    group = next(group for group in loads(payload).children if group.name == "VoteGiftParam")
    return table_rows(group)


def test_old_login_delivery_notifies_with_new_text_without_rewriting_its_proof(factory):
    case = factory()
    upgrade_cooking(case, 5006)
    case.transactions.submit(vote(case, 1), case.store)
    case.clock.now = case.group.result_start
    original_request = reward_request(case, 115)
    original_wire = case.login.receive(original_request, case.store)
    saved = case.store.profile()
    assert cooking_rows(original_wire)[0]["ReceiveTextID"] == 5006
    assert case.login.receive(original_request, case.store) == original_wire
    upgrade_cooking(case)
    with pytest.raises(ProtocolError) as error:
        case.login.receive(original_request, case.store)
    assert error.value.code == "login_reward_retry_stale"
    assert case.store.profile() == saved
    envelope = reward_request(case, 116, tick=2001)
    payload = case.rank.receive(envelope, case.store)
    assert cooking_rows(payload)[0]["ReceiveTextID"] == 5028
    delivered = case.store.profile()
    assert delivered.state["vote_cooking"] == saved.state["vote_cooking"]
    notice = delivered.state["vote_cooking_notifications"]["records"][case.group.item_id]["grant"]
    assert notice["receive_text_id"] == 5006
    assert case.rank.receive(envelope, case.store) == payload
    assert case.store.profile() == delivered
    assert cooking_rows(case.rank.receive(reward_request(case, 116, tick=2002), case.store)) == []
    assert len([row for row in case.presents.rows(case.store.profile().state) if row["Type"] == 21]) == 1


def test_already_notified_delivery_is_not_resent_after_text_resource_upgrade(factory):
    case = factory()
    upgrade_cooking(case, 5006)
    case.transactions.submit(vote(case, 1), case.store)
    case.clock.now = case.group.result_start
    envelope = reward_request(case, 116)
    payload = case.rank.receive(envelope, case.store)
    saved = case.store.profile()
    assert cooking_rows(payload)[0]["ReceiveTextID"] == 5006
    assert case.rank.receive(envelope, case.store) == payload
    upgrade_cooking(case)
    with pytest.raises(ProtocolError) as error:
        case.rank.receive(envelope, case.store)
    assert error.value.code == "rank_reward_retry_stale"
    assert case.store.profile() == saved
    assert cooking_rows(case.rank.receive(reward_request(case, 116, tick=2001), case.store)) == []
    updated = case.store.profile().state
    assert updated["vote_cooking"] == saved.state["vote_cooking"]
    assert updated["vote_cooking_notifications"] == saved.state["vote_cooking_notifications"]
    assert len([row for row in case.presents.rows(updated) if row["Type"] == 21]) == 1


def test_new_login_delivery_uses_current_text_and_retries_exactly(factory):
    case = factory()
    upgrade_cooking(case)
    case.transactions.submit(vote(case, 1), case.store)
    case.clock.now = case.group.result_start
    envelope = reward_request(case, 115)
    payload = case.login.receive(envelope, case.store)
    saved = case.store.profile()
    assert cooking_rows(payload)[0]["ReceiveTextID"] == 5028
    grant = saved.state["login_reward_delivery"]["vote_cooking_grants"][0]
    assert grant["receive_text_id"] == 5028
    assert case.login.receive(envelope, case.store) == payload
    assert case.store.profile() == saved


def test_login_vote_rank_and_startup_share_one_daily_ticket(factory):
    case = factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, first = post(client, reward_request(case, 115))
        assert reply_counts(first["POSSESSION"])[DAILY_ITEM] == 1
        assert "vote_finalists" not in case.store.profile().state and "vote_expiry" not in case.store.profile().state
        post(client, vote(case, 1))
        _, later = post(client, reward_request(case, 116))
        assert reply_counts(later["POSSESSION"])[DAILY_ITEM] == 0
        _, start = post(client, request(case.store, 137, (), tick=2001))
        assert reply_counts(start["POSSESSION"])[DAILY_ITEM] == 0
        case.clock.now += timedelta(days=1)
        _, refreshed = post(client, reward_request(case, 116, tick=2002))
        assert reply_counts(refreshed["POSSESSION"])[DAILY_ITEM] == 1
        post(client, vote(case, 1, tick=2003))
        _, after = post(client, reward_request(case, 115, tick=2004))
        assert reply_counts(after["POSSESSION"])[DAILY_ITEM] == 0
    assert counts(case)[case.group.item_id] == 999


@pytest.mark.parametrize("first", (115, 116))
def test_cooking_delivered_once_and_notified_by_116_then_cleared(factory, first, monkeypatch):
    case = factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, vote(case, 1))
        case.clock.now = case.group.result_start
        _, initial = post(client, reward_request(case, first))
        delivered = case.store.profile().state
        mails = [row for row in case.presents.rows(delivered) if row["Type"] == 21]
        assert len(mails) == 1
        if first == 115:
            assert "vote_cooking_notifications" not in delivered
        envelope = reward_request(case, 116, tick=2001)
        wire, notice = post(client, envelope)
        rows = table_rows(notice["VoteGiftParam"])
        assert rows == ([] if first == 116 else [{"CardId": case.candidate.card_id, "Count": 1, "ReceiveTextID": 5028}])
        stable = case.store.profile()
        assert post(client, envelope)[0] == wire and case.store.profile() == stable
        _, again = post(client, reward_request(case, 116, tick=2002))
        assert table_rows(again["VoteGiftParam"]) == []
        assert len([row for row in case.presents.rows(case.store.profile().state) if row["Type"] == 21]) == 1
    reopened = case.reopen()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, result = post(client, reward_request(case, 116, tick=2003))
        assert table_rows(result["VoteGiftParam"]) == []


def test_claimed_115_mail_can_be_notified_without_sending_a_missing_present(factory):
    case = factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, vote(case, 1))
        case.clock.now = case.group.result_start
        post(client, reward_request(case, 115))
        mail = next(row for row in case.presents.rows(case.store.profile().state) if row["Type"] == 21)
        post(client, claim(case.store, 1, [mail["PresentID"]], tick=2001))
        _, notified = post(client, reward_request(case, 116, tick=2002))
        assert len(table_rows(notified["VoteGiftParam"])) == 1
        if "PPT" in notified:
            assert mail["PresentID"] not in [row["PresentID"] for row in table_rows(notified["PPT"])]
        assert not any(row["Type"] == 21 for row in case.presents.rows(case.store.profile().state))


def test_concurrent_115_116_share_ticket_mail_and_notice(factory, monkeypatch):
    case = factory()
    case.transactions.submit(vote(case, 1), case.store)
    case.clock.now = case.group.result_start
    original = case.store.update_state
    barrier, lock, calls = Barrier(2), Lock(), 0
    def synchronized(state, revision):
        nonlocal calls
        with lock:
            calls += 1
            wait = calls <= 2
        if wait: barrier.wait(timeout=20)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", synchronized)
    requests = [(case.login, reward_request(case, 115)), (case.rank, reward_request(case, 116))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.receive, envelope, case.store) for service, envelope in requests]
        assert all(isinstance(future.result(timeout=45), bytes) for future in futures)
    state = case.store.profile().state
    assert counts(case)[DAILY_ITEM] == 1
    assert len([row for row in case.presents.rows(state) if row["Type"] == 21]) == 1
    assert len(state["vote_cooking_notifications"]["records"]) == 1


@pytest.mark.parametrize("empty_followup", (False, True))
def test_lost_notification_proof_is_rejected_by_receipt_owner(factory, empty_followup):
    case = factory()
    case.transactions.submit(vote(case, 1), case.store)
    case.clock.now = case.group.result_start
    case.rank.receive(reward_request(case, 116), case.store)
    if empty_followup:
        case.rank.receive(reward_request(case, 116, tick=2001), case.store)
        assert case.store.profile().state["rank_reward_delivery"]["vote_cooking_notifications"] == []
    state = deepcopy(case.store.profile().state)
    del state["vote_cooking_notifications"]
    with pytest.raises(ProtocolError): case.rank.initialize(state)


@pytest.mark.parametrize("number", (115, 116))
def test_new_exact_retry_does_not_observe_new_day(factory, monkeypatch, number):
    case = factory()
    service = case.login if number == 115 else case.rank
    envelope = reward_request(case, number)
    wire = service.receive(envelope, case.store)
    before = case.store.profile()
    def forbidden():
        pytest.fail("An exact retry must not read a new clock")
    monkeypatch.setattr(case.bootstrap, "clock", forbidden)
    monkeypatch.setattr(case.login, "clock", forbidden)
    assert service.receive(envelope, case.store) == wire and case.store.profile() == before


def test_cli_injects_the_same_ticket_and_notifier_dependencies(bundle):
    assert bundle.login_rewards.vote_tickets is bundle.votes.transactions.tickets
    assert bundle.rank_rewards.vote_tickets is bundle.votes.transactions.tickets
    assert bundle.rank_rewards.vote_cooking_notifier is bundle.vote_cooking_notifier
    assert bundle.vote_cooking_notifier.cooking is bundle.vote_cooking

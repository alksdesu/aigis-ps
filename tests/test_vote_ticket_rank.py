from copy import copy, deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.rank_rewards import RankRewardService
import aigis_ps.rank_rewards as module
from aigis_ps.vote_cooking_service import validate_vote_cooking_grants
from test_evolution_integration import post
from test_quest_service import bundle, request
from test_vote_catalog import vote_sources, build
from test_vote_service import table_rows
from test_vote_transactions import catalog, make_case, NOW


class CookingNotifier:
    content_version = "a" * 64

    def __init__(self, presents, *, available=True):
        self.presents, self.available = presents, available
        self.calls = 0

    def _ready(self):
        pass

    def initialize(self, state):
        return deepcopy(state)

    def settle(self, state, now):
        self.calls += 1
        updated = deepcopy(state)
        record = updated.get("fixture_cooking")
        deliveries = []
        if record is None and self.available:
            updated, mail = self.presents.enqueue(updated, [{"kind": "gold", "amount": 1, "description": "fixture"}])
            grant = {"item_id": "item_vote_016001", "group_id": 1, "definition": "b" * 64,
                     "card_id": 10, "count": 1, "receive_text_id": 5006, "mail_ids": [mail[0]["PresentID"]]}
            record = {"grant": grant, "notified": False}
            updated["fixture_cooking"] = record
            deliveries.append(deepcopy(grant))
        notices = []
        if record is not None and not record["notified"]:
            record["notified"] = True
            notices.append(deepcopy(record["grant"]))
            updated.setdefault("vote_cooking_notifications", {"schema_version": 1, "records": {}})["records"][
                record["grant"]["item_id"]] = deepcopy(record["grant"])
        return updated, deliveries, notices

    def validate_receipt(self, state, deliveries, notices):
        validate_vote_cooking_grants(deliveries)
        validate_vote_cooking_grants(notices)
        record = state.get("fixture_cooking")
        for grant in deliveries + notices:
            if record is None or grant != record["grant"] or not record["notified"]:
                raise ProtocolError("fixture_proof_invalid", "Cooking notice proof differs", 503)

    def response(self, notices):
        return Container("VoteGiftParam", tuple(Column(name, "I", tuple(grant[key] for grant in notices))
            for name, key in (("CardId", "card_id"), ("Count", "count"), ("ReceiveTextID", "receive_text_id"))))


@pytest.fixture
def factory(make_case):
    def create(*, tickets=True, notifier=True, available=True, level=0):
        case = make_case(tickets=0)
        source = CookingNotifier(case.presents, available=available)
        options = {name: getattr(case.bundle, name) for threshold, name in (
            (2, "event_passes"), (3, "event_logins"), (4, "comeback_logins"),
            (5, "purchased_logins"), (6, "daily_gifts")) if level >= threshold}
        for name in ("event_logins", "comeback_logins", "purchased_logins", "daily_gifts"):
            if name in options:
                options[name] = copy(options[name])
                options[name].presents = case.presents
        if "event_passes" in options:
            options["event_passes"] = copy(options["event_passes"])
            options["event_passes"].presents = case.presents

        def service(*, with_tickets=tickets, with_notifier=notifier):
            return RankRewardService(case.bootstrap, case.bundle.player_state, case.presents,
                case.bundle.rank_rewards.catalog, **options,
                vote_tickets=case.transactions.tickets if with_tickets else None,
                vote_cooking_notifier=source if with_notifier else None)

        case.rank = service()
        case.notifier = source
        case.make_rank = service

        def application():
            return create_app(case.store.path.parent, initial_state=case.store.profile().state,
                state_initializer=case.rank.initialize,
                handlers=case.bootstrap.handlers() | case.presents.handlers() | case.rank.handlers())

        case.application = application
        case.app = application()
        case.store = case.app.state.store
        return case
    return create


def receive(case, tick=1000):
    return request(case.store, 116, (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)),
        Column("DmmPremiumReceiverId", "I", (0,))), tick=tick)


def groups(payload):
    return {child.name: child for child in loads(payload).children}


@pytest.mark.parametrize("tickets,notifier", ((True, True), (True, False), (False, True)))
def test_optional_sources_commit_atomic_and_restart_retry(factory, tickets, notifier, monkeypatch):
    case = factory(tickets=tickets, notifier=notifier)
    before = case.store.profile()
    envelope = receive(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, reply = post(client, envelope)
        saved = case.store.profile()
        marker = saved.state["rank_reward_delivery"]
        assert saved.revision == before.revision + 1 and marker["schema_version"] == 7
        assert ("POSSESSION" in reply) == tickets
        assert ("VoteGiftParam" in reply) == notifier
        if tickets:
            assert table_rows(reply["POSSESSION"]) == [{"ItemID": "item_10000000", "Count": 1, "Enabled": 0}]
            assert marker["vote_ticket_observation"] == saved.state["vote_tickets"]
        else:
            assert marker["vote_ticket_observation"] is None
        if notifier:
            assert table_rows(reply["VoteGiftParam"]) == [{"CardId": 10, "Count": 1, "ReceiveTextID": 5006}]
            assert marker["vote_cooking_deliveries"] == marker["vote_cooking_notifications"]
            assert marker["vote_cooking_notification_count"] == 1
            assert reply["PPT"].children[0].values == (1,)
        else:
            assert marker["vote_cooking_deliveries"] == marker["vote_cooking_notifications"] == []
            assert marker["vote_cooking_notification_count"] == 0
        monkeypatch.setattr(case.bootstrap, "clock", lambda: pytest.fail("Retry must not observe another instant"))
        assert post(client, envelope)[0] == wire and case.store.profile() == saved
    reopened = case.application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire and case.store.profile() == saved


def test_old_notice_can_be_shown_after_mail_was_claimed_without_new_ppt(factory):
    case = factory(tickets=False)
    before = case.store.profile()
    seeded, _, notices = case.notifier.settle(before.state, NOW)
    seeded["fixture_cooking"]["notified"] = False
    seeded["tables"]["presents"] = []
    case.store.update_state(seeded, before.revision)
    result = groups(case.rank.receive(receive(case), case.store))
    assert "PPT" not in result
    assert result["VoteGiftParam"] == case.notifier.response(notices)
    marker = case.store.profile().state["rank_reward_delivery"]
    assert marker["mail_ids"] == marker["vote_cooking_deliveries"] == []
    assert marker["vote_cooking_notifications"] == notices
    again = groups(case.rank.receive(receive(case, 1001), case.store))
    assert table_rows(again["VoteGiftParam"]) == []
    assert not case.presents.rows(case.store.profile().state)


def test_daily_ticket_consumption_next_day_and_expiry_keep_enabled(factory):
    case = factory(notifier=False)
    before = case.store.profile()
    state = case.bundle.possessions.credit(before.state, {"battle_god_bless_001001": 1})
    state["tables"]["possessions"][0]["Enabled"] = 1
    case.store.update_state(state, before.revision)
    case.rank.receive(receive(case), case.store)
    saved = case.store.profile()
    spent, _, _ = case.transactions.tickets.consume(saved.state, case.group.item_id, 1, NOW)
    case.store.update_state(spent, saved.revision)
    same_day = groups(case.rank.receive(receive(case, 1001), case.store))
    assert next(row for row in table_rows(same_day["POSSESSION"]) if row["ItemID"] == "item_10000000")["Count"] == 0
    case.clock.now = NOW + timedelta(days=1)
    next_day = groups(case.rank.receive(receive(case, 1002), case.store))
    assert next(row for row in table_rows(next_day["POSSESSION"]) if row["ItemID"] == "item_10000000")["Count"] == 1
    case.clock.now = case.catalog.event.end_at + timedelta(days=1)
    expired = groups(case.rank.receive(receive(case, 1003), case.store))
    rows = table_rows(expired["POSSESSION"])
    assert next(row for row in rows if row["ItemID"] == "item_10000000")["Count"] == 0
    assert next(row for row in rows if row["ItemID"] == "battle_god_bless_001001")["Enabled"] == 1


@pytest.mark.parametrize("level", range(1, 7))
def test_all_previous_schemas_replay_without_new_services_or_clock(factory, level, monkeypatch):
    case = factory(tickets=False, notifier=False, level=level)
    envelope = receive(case)
    payload = case.rank.receive(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["rank_reward_delivery"]["schema_version"] == level
    upgraded = case.make_rank(with_tickets=True, with_notifier=True)
    monkeypatch.setattr(case.bootstrap, "clock", lambda: pytest.fail("Old retry must not read the new clock"))
    monkeypatch.setattr(upgraded, "_vote_ready", lambda: pytest.fail("Old retry must not read new dependencies"))
    assert upgraded.receive(envelope, case.store) == payload
    assert case.store.profile() == saved


@pytest.mark.parametrize("registration", (42, 116, 129, 137))
def test_response_encoding_failure_rolls_back_tickets_mail_and_notice(factory, monkeypatch, registration):
    case = factory()
    before = case.store.profile()
    original = module.encode_response
    def failure(profile, payload):
        if profile.registration_keys == (registration,):
            raise ProtocolError("fixture_failure", "Fixture encoding failure", 503)
        return original(profile, payload)
    monkeypatch.setattr(module, "encode_response", failure)
    with pytest.raises(ProtocolError, match="Fixture encoding failure"):
        case.rank.receive(receive(case), case.store)
    assert case.store.profile() == before


@pytest.mark.parametrize("bad", ("table", "name", "type", "value", "duplicate"))
def test_bad_notification_rolls_back_entire_116(factory, monkeypatch, bad):
    case = factory()
    before = case.store.profile()
    original = case.notifier.response
    def corrupt(notices):
        correct = original(notices)
        children = list(correct.children)
        if bad == "table": return Container("Wrong", correct.children)
        if bad == "name": children[0] = Column("CardID", "I", children[0].values)
        if bad == "type": children[0] = Column("CardId", "S", tuple(str(value) for value in children[0].values))
        if bad == "value": children[0] = Column("CardId", "I", (11,))
        if bad == "duplicate": children.append(children[0])
        return Container("VoteGiftParam", tuple(children))
    monkeypatch.setattr(case.notifier, "response", corrupt)
    with pytest.raises(ProtocolError, match="differs from its proof"):
        case.rank.receive(receive(case), case.store)
    assert case.store.profile() == before


@pytest.mark.parametrize("field", ("vote_ticket_observation", "vote_cooking_deliveries", "vote_cooking_notifications"))
def test_corrupted_new_receipt_is_rejected(factory, field):
    case = factory()
    case.rank.receive(receive(case), case.store)
    state = case.store.profile().state
    damaged = deepcopy(state)
    if field == "vote_ticket_observation":
        damaged["rank_reward_delivery"][field]["ticket_day"] = "wrong"
    else:
        damaged["rank_reward_delivery"][field][0]["card_id"] += 1
    with pytest.raises(ProtocolError): case.rank.initialize(damaged)


def test_inactive_event_returns_empty_notice_without_manufacturing_ticket(factory):
    case = factory(available=False)
    case.clock.now = case.catalog.event.start_at - timedelta(days=1)
    before = case.store.profile()
    result = groups(case.rank.receive(receive(case), case.store))
    after = case.store.profile()
    assert "vote_tickets" not in after.state and table_rows(result["POSSESSION"]) == []
    assert table_rows(result["VoteGiftParam"]) == []
    assert after.state["tables"] == before.state["tables"]


@pytest.mark.parametrize("remove", ("domain", "record"))
def test_empty_latest_notification_keeps_permanent_history_witness(factory, remove):
    case = factory()
    case.rank.receive(receive(case), case.store)
    case.rank.receive(receive(case, 1001), case.store)
    state = case.store.profile().state
    marker = state["rank_reward_delivery"]
    assert marker["vote_cooking_notifications"] == [] and marker["vote_cooking_notification_count"] == 1
    damaged = deepcopy(state)
    if remove == "domain":
        del damaged["vote_cooking_notifications"]
    else:
        damaged["vote_cooking_notifications"]["records"].clear()
    with pytest.raises(ProtocolError, match="lost permanent proof"):
        case.rank.initialize(damaged)

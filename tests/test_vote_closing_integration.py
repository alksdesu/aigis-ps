from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier, Lock

import pytest

from aigis_ps.alxml import loads
from aigis_ps.errors import ProtocolError
from aigis_ps.vote_expiry import ASSAULT_KEY
from aigis_ps.vote_rules import VoteRules
from aigis_ps.vote_state import read_vote_history, record_vote
import aigis_ps.vote_service as service_module
import aigis_ps.vote_transactions as transaction_module
from test_quest_service import bundle, request
from test_vote_catalog import vote_sources, build
from test_vote_service import table_rows
from test_vote_transactions import catalog, make_case, vote, counts


def tables(response):
    return {child.name: child for child in loads(response, limits=service_module.REPLY_LIMITS).children}


def credit(case, **items):
    profile = case.store.profile()
    case.store.update_state(case.bundle.possessions.credit(profile.state, items), profile.revision)


def test_directory_freezes_qualifiers_without_mutating_inventory_or_mail(make_case):
    case = make_case(tickets=7)
    case.clock.now = case.catalog.groups[-1].vote_start
    before = case.store.profile()
    first = tables(case.service.read(request(case.store, 55, ()), case.store))
    saved = case.store.profile()
    assert saved.revision == before.revision + 1
    assert saved.state["tables"] == before.state["tables"]
    assert set(saved.state["vote_finalists"]["groups"]) == {group.item_id for group in case.catalog.groups[:3]}
    rows = table_rows(first["VCC"])
    assert len(rows) == 1912
    assert len({(row["GroupID"], row["CardID"]) for row in rows}) == len(rows)
    card = case.candidate.card_id
    assert {row["GroupID"] for row in rows if row["CardID"] == card} == {1, 4}
    case.service.read(request(case.store, 55, (), tick=1001), case.store)
    assert case.store.profile() == saved


def test_final_vote_converts_old_tickets_and_replays_exactly(make_case, monkeypatch):
    case = make_case(tickets=7)
    final = case.catalog.groups[-1]
    credit(case, **{final.item_id: 10})
    case.clock.now = final.vote_start
    envelope = vote(case, 5, group_id=4)
    response = case.transactions.submit(envelope, case.store)
    result = tables(response)
    assert counts(case)[case.group.item_id] == 0
    assert counts(case)[final.item_id] == 6
    assert counts(case)["item_10000000"] == 0
    assert {row["ItemID"]: row["Count"] for row in table_rows(result["POSSESSION"])} == counts(case)
    assert [(row["GroupID"], row["Count"]) for row in table_rows(result["VRC"])] == [(4, 5)]
    saved = case.store.profile()
    expired = saved.state["vote_expiry"]["items"][case.group.item_id]
    assert expired["status"] == "settled" and expired["amount"] == 7
    assert any(row["Type"] == 2 and row["Content"] == 350 for row in case.presents.rows(saved.state))
    assert len(table_rows(result["PPT"])) == 2
    monkeypatch.setattr(case.bootstrap, "clock", lambda: pytest.fail("Retry must retain the committed observation"))
    assert case.transactions.submit(envelope, case.store) == response
    reopened = case.reopen()
    assert case.transactions.submit(envelope, reopened.state.store) == response
    assert case.store.profile() == saved


def test_final_qualification_excludes_lower_ranked_card_and_keeps_group_identity(make_case):
    case = make_case(tickets=0)
    profile = case.store.profile()
    selected = [candidate for candidate in case.catalog.candidates if candidate.group_id == 1][:11]
    state = profile.state
    for index, candidate in enumerate(selected):
        state = record_vote(state, item_id=case.group.item_id, group_id=1, card_id=candidate.card_id,
                            count=2 if index < 10 else 1, definition=case.transactions.rewards.group_definition(case.group))
    state = case.service.initialize(state)
    final = case.catalog.groups[-1]
    state = case.bundle.possessions.credit(state, {final.item_id: 5})
    case.store.update_state(state, profile.revision)
    case.clock.now = final.vote_start
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.transactions.submit(vote(case, 1, card_id=selected[-1].card_id, group_id=4), case.store)
    assert caught.value.code == "vote_candidate_invalid"
    assert case.store.profile() == before
    case.transactions.submit(vote(case, 1, card_id=selected[0].card_id, group_id=4), case.store)
    rows = [row for row in read_vote_history(case.store.profile().state) if row["cardId"] == selected[0].card_id]
    assert {row["groupId"] for row in rows} == {1, 4}
    assert len({row["key"] for row in rows}) == 2


def test_final_result_snapshot_and_native_visibility_windows(make_case):
    case = make_case(tickets=0)
    final, event = case.catalog.groups[-1], case.catalog.event
    case.clock.now = final.vote_start
    case.transactions.submit(vote(case, 1, group_id=4), case.store)
    case.clock.now = event.result_start
    result = tables(case.service.read(request(case.store, 55, (), tick=1001), case.store))
    event_row = table_rows(result["VEC"])[0]
    assert event_row["DateClose"] == event_row["DateCloseResult"]
    assert event_row["DateCloseAnnouncement"] == event_row["DateOpenResult"]
    record = case.store.profile().state["vote_finalists"]["groups"][final.item_id]
    assert record["counts"][0] == {"CardID": case.candidate.card_id, "Count": 1}
    saved = case.store.profile()
    case.clock.now += timedelta(hours=1)
    case.service.read(request(case.store, 55, (), tick=1002), case.store)
    assert case.store.profile() == saved


def test_default_history_does_not_freeze_an_unplayed_revival(make_case, vote_sources):
    case = make_case(tickets=0, selected_catalog=build(vote_sources))
    before = case.store.profile()
    case.transactions.startup(request(case.store, 137, ()), case.store)
    assert case.store.profile() == before
    assert "vote_finalists" not in before.state and "vote_expiry" not in before.state


def test_shifted_revival_cannot_reopen_frozen_group(make_case, vote_sources):
    case = make_case(tickets=0)
    case.clock.now = case.group.vote_end
    case.service.read(request(case.store, 55, ()), case.store)
    source = build(vote_sources, VoteRules(revival_start="2026-11-07T15:00:00+09:00"))
    moved = make_case(state=case.store.profile().state, selected_catalog=source, now=source.event.start_at)
    before = moved.store.profile()
    with pytest.raises(ProtocolError) as caught:
        moved.transactions.submit(vote(moved, 1), moved.store)
    assert caught.value.code == "vote_group_frozen"
    assert moved.store.profile() == before


def test_expired_identity_without_vote_history_cannot_reopen_with_daily_ticket(make_case, vote_sources):
    case = make_case(tickets=3, selected_catalog=build(vote_sources))
    case.transactions.startup(request(case.store, 137, ()), case.store)
    assert "vote_finalists" not in case.store.profile().state
    source = build(vote_sources, VoteRules(revival_start="2026-11-07T15:00:00+09:00"))
    moved = make_case(state=case.store.profile().state, selected_catalog=source, now=source.event.start_at)
    before = moved.store.profile()
    with pytest.raises(ProtocolError) as caught:
        moved.transactions.submit(vote(moved, 1), moved.store)
    assert caught.value.code == "vote_ticket_closed"
    assert moved.store.profile() == before


@pytest.mark.parametrize("registration", (55, 137))
def test_failed_encoding_does_not_commit_closing_side_effects(make_case, monkeypatch, registration):
    case = make_case(tickets=8)
    case.clock.now = case.catalog.groups[-1].vote_start
    before = case.store.profile()
    target = service_module if registration == 55 else transaction_module
    original = target.encode_response
    def fail(profile, response):
        if profile.registration_keys == (registration,):
            raise ProtocolError("fixture_encoding_failure", "Fixture failure", 503)
        return original(profile, response)
    monkeypatch.setattr(target, "encode_response", fail)
    handler = case.service.read if registration == 55 else case.transactions.startup
    with pytest.raises(ProtocolError, match="Fixture failure"):
        handler(request(case.store, registration, ()), case.store)
    assert case.store.profile() == before


def test_legacy_committed_receipt_still_replays_without_observing_time(make_case, monkeypatch):
    case = make_case()
    envelope = vote(case)
    response = case.transactions.submit(envelope, case.store)
    profile = case.store.profile()
    legacy = deepcopy(profile.state)
    receipt = legacy["vote_transactions"]["receipts"][-1]
    receipt["content"] = case.transactions._legacy_content()
    receipt["revision"] = profile.revision + 1
    case.store.update_state(legacy, profile.revision)
    monkeypatch.setattr(case.bootstrap, "clock", lambda: pytest.fail("Legacy retry must not observe time"))
    assert case.transactions.submit(envelope, case.store) == response


def test_mail_failure_rolls_back_old_group_expiry_and_final_vote_together(make_case):
    case = make_case(tickets=8)
    final = case.catalog.groups[-1]
    credit(case, **{final.item_id: 8})
    profile = case.store.profile()
    full, _ = case.presents.enqueue(profile.state, [{"kind": "gold", "amount": 1, "description": "fixture"}] * 499)
    case.store.update_state(full, profile.revision)
    case.clock.now = final.vote_start
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.transactions.submit(vote(case, 5, group_id=4), case.store)
    assert caught.value.code == "present_capacity"
    assert case.store.profile() == before


def test_concurrent_closing_observations_create_one_conversion(make_case, monkeypatch):
    case = make_case(tickets=8)
    case.clock.now = case.group.vote_end
    original = case.store.update_state
    barrier, lock, calls = Barrier(2), Lock(), 0
    def update(state, revision):
        nonlocal calls
        with lock:
            calls += 1
            wait = calls <= 2
        if wait:
            barrier.wait(timeout=20)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    envelope = request(case.store, 137, ())
    before = case.store.profile()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: case.transactions.startup(envelope, case.store), range(2)))
    assert results[0] == results[1]
    assert case.store.profile().revision == before.revision + 1
    assert len(case.presents.rows(case.store.profile().state)) == 1
    assert counts(case)[case.group.item_id] == 0


def test_final_repeat_rewards_start_after_three_hundred_and_survive_closing(make_case):
    case = make_case(tickets=0)
    final = case.catalog.groups[-1]
    credit(case, **{final.item_id: 500})
    case.clock.now = final.vote_start
    first = tables(case.transactions.submit(vote(case, 300, group_id=4), case.store))
    assert [row["Count"] for row in table_rows(first["VRC"])] == [
        5, 10, 20, 30, 40, 50, 75, 100, 125, 150, 175, 200, 225, 250, 275, 300]
    second = tables(case.transactions.submit(vote(case, 100, group_id=4, tick=1001), case.store))
    assert [(row["GroupID"], row["Count"], row["Repeat"]) for row in table_rows(second["VRC"])] == [(4, 400, 0)]
    before = case.store.profile()
    assert counts(case)[final.item_id] == 101
    case.clock.now = final.vote_end
    case.transactions.startup(request(case.store, 137, (), tick=1002), case.store)
    closed = case.store.profile()
    assert closed.state["vote_rewards"] == before.state["vote_rewards"]
    assert closed.state["vote_expiry"]["items"][final.item_id]["amount"] == 101
    # The final vote end also converts the 26 challenge rights from 400 final votes.
    rights = closed.state[ASSAULT_KEY]["events"]["16"]
    assert (rights["status"], rights["amount"]) == ("settled", 26)
    assert len(case.presents.rows(closed.state)) == len(case.presents.rows(before.state)) + 2
    case.service.initialize(closed.state)


@pytest.mark.parametrize("remove", ("all", "first_group"))
def test_committed_final_vote_requires_its_permanent_qualification(make_case, remove):
    case = make_case(tickets=0)
    case.clock.now = case.catalog.groups[-1].vote_start
    case.transactions.submit(vote(case, 1, group_id=4), case.store)
    state = deepcopy(case.store.profile().state)
    if remove == "all":
        del state["vote_finalists"]
    else:
        del state["vote_finalists"]["groups"][case.group.item_id]
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        case.service.initialize(state)
    assert state == before

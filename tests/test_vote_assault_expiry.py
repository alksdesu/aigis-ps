from copy import copy, deepcopy
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import loads
from aigis_ps.errors import ProtocolError
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.vote_expiry import (ASSAULT_DESCRIPTION, ASSAULT_GOLD_PER_RIGHT, ASSAULT_KEY, GOLD_PER_TICKET,
                                  VoteAssaultExpiryService, VoteExpiryService, _assault_definition,
                                  converted_assault_rights)
from aigis_ps.vote_rewards import VoteRewardService
from aigis_ps.vote_rules import VoteRules
from aigis_ps.vote_service import REPLY_LIMITS
from aigis_ps.vote_state import read_vote_history, record_vote
from aigis_ps.vote_transactions import VoteTransactions
from test_present_items import item_table
from test_quest_service import bundle, request
from test_vote_catalog import build, rows, vote_sources
from test_vote_cooking_integration import factory
from test_vote_expiry import refuse_copies
from test_vote_service import table_rows
from test_vote_transactions import catalog, make_case, vote


REVIVAL = "2026-11-01T15:00:00+09:00"
EVENT = "16"
CLAIM_DAY = 739921
QUIET = {"grants": [], "pending": []}
RATE_TEXT = "挑戦権1回につき3000Gへと変換されます。"


@pytest.fixture(scope="module")
def catalogs(vote_sources):
    return SimpleNamespace(revival=build(vote_sources, VoteRules(revival_start=REVIVAL)),
                           disabled=build(vote_sources, VoteRules(revival_start=REVIVAL, enabled=False)),
                           moved=build(vote_sources, VoteRules(revival_start="2026-11-11T15:00:00+09:00")))


@pytest.fixture
def case(bundle, catalogs):
    presents = copy(bundle.presents)
    rewards = VoteRewardService(catalogs.revival, presents)
    return SimpleNamespace(catalog=catalogs.revival, presents=presents, possessions=bundle.possessions, rewards=rewards,
                           helper=VoteAssaultExpiryService(rewards), end=catalogs.revival.event.end_at,
                           state=bundle.player_state.initialize(bundle.bootstrap.initial_state()))


def earn(case, state, count):
    group = case.catalog.groups[0]
    candidate = next(item for item in case.catalog.candidates if item.group_id == group.group_id)
    current = next((row["count"] for row in read_vote_history(state)
                    if row["itemId"] == group.item_id and row["cardId"] == candidate.card_id), 0)
    after = record_vote(state, item_id=group.item_id, group_id=group.group_id, card_id=candidate.card_id,
                        count=current + count, definition=case.rewards.group_definition(group))
    return case.rewards.apply_vote(state, after, group)[0]


def remain(case, state):
    return case.rewards.assault_response(state).children[0].values[0]


def utc(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def filled(case, state):
    space = MAX_PRESENTS - len(case.presents.rows(state))
    return case.presents.enqueue(state, [{"kind": "gold", "amount": 1, "description": "capacity"}] * space)[0]


def freed(case, state):
    capacity = next(row["PresentID"] for row in case.presents.rows(state) if row["Description"] == "capacity")
    return case.presents.claim(state, [capacity], acquisition_day=CLAIM_DAY)[0]


def pending(case, rights=3):
    result, report = case.helper.settle(filled(case, earn(case, case.state, rights * 15)), case.end)
    assert report == {"grants": [], "pending": [{"event_id": 16, "reason": "present_capacity"}]}
    return result


def settled(case, rights=2):
    return case.helper.settle(earn(case, case.state, rights * 15), case.end)


def tables(response):
    return {child.name: child for child in loads(response, limits=REPLY_LIMITS).children}


def test_rights_wait_for_the_final_vote_end(case):
    state = earn(case, case.state, 30)
    before = deepcopy(state)
    for moment in (case.catalog.groups[0].vote_end, case.end - timedelta(seconds=1)):
        assert case.helper.settle(state, moment) == (before, QUIET)
    assert state == before and remain(case, state) == 2


def test_final_vote_end_mails_each_unused_right_as_three_thousand_gold_once(case):
    state = earn(case, case.state, 30)
    before = deepcopy(state)
    result, report = case.helper.settle(state, case.end)
    identifier = state["last_present_id"] + 1
    assert report == {"grants": [{"event_id": 16, "amount": 2, "rate": ASSAULT_GOLD_PER_RIGHT, "mail_ids": [identifier]}],
                      "pending": []}
    assert case.presents.rows(result)[-1] == {"PresentID": identifier, "Type": 2, "Content": 6000,
                                              "Description": ASSAULT_DESCRIPTION}
    record = result[ASSAULT_KEY]["events"][EVENT]
    source_end = utc(case.catalog.event.source_end)
    assert record == {"definition": _assault_definition(16, ASSAULT_GOLD_PER_RIGHT, source_end),
                      "source_window_end": source_end, "window_end": utc(case.end), "observed_at": record["observed_at"],
                      "earned": 2, "spent": 0, "amount": 2, "rate": ASSAULT_GOLD_PER_RIGHT, "status": "settled",
                      "reason": None, "settled_at": record["observed_at"], "mail_ids": [identifier]}
    assert record["source_window_end"] != record["window_end"]
    assert result["vote_rewards"] == before["vote_rewards"] and result["player_state"] == before["player_state"]
    assert result["tables"]["game_param"] == before["tables"]["game_param"]
    assert state == before and remain(case, state) == 2 and remain(case, result) == 0
    assert converted_assault_rights(result, 16) == 2 and converted_assault_rights(state, 16) == 0
    assert case.helper.settle(result, case.end + timedelta(days=1)) == (result, QUIET)
    assert case.helper.initialize(result) == result


def test_claimed_gold_raises_the_balance_and_keeps_the_conversion_proof(case):
    converted, report = settled(case, rights=1)
    claimed, units, crystals = case.presents.claim(converted, report["grants"][0]["mail_ids"], acquisition_day=CLAIM_DAY)
    assert units == [] and crystals == 0
    money = converted["tables"]["game_param"][0]["Money"]
    assert claimed["tables"]["game_param"][0]["Money"] == money + ASSAULT_GOLD_PER_RIGHT
    assert claimed[ASSAULT_KEY] == converted[ASSAULT_KEY]
    assert case.helper.initialize(claimed) == claimed and remain(case, claimed) == 0
    assert case.helper.settle(claimed, case.end + timedelta(seconds=1)) == (claimed, QUIET)


@pytest.mark.parametrize("votes", (0, 14))
def test_zero_unused_rights_leave_no_record(case, votes):
    state = case.state if not votes else earn(case, case.state, votes)
    assert case.helper.settle(state, case.end) == (state, QUIET)
    assert ASSAULT_KEY not in state and case.helper.conversions(state) == {}


def test_ledger_reads_the_validated_journal_without_copying_the_save(case, monkeypatch):
    earned, waiting, (converted, _) = earn(case, case.state, 30), pending(case), settled(case)
    states = (case.state, earned, waiting, converted)
    before = deepcopy(states)
    refuse_copies(monkeypatch)
    assert case.helper.ledger(case.state) == case.helper.ledger(earned) == {}
    for state in (waiting, converted):
        assert case.helper.ledger(state) == case.helper._state(state)[1]["events"] == state[ASSAULT_KEY]["events"]
    assert states == before


def test_full_mailbox_keeps_rights_pending_until_a_retry_lands(case):
    waiting = pending(case)
    record = deepcopy(waiting[ASSAULT_KEY]["events"][EVENT])
    assert (record["status"], record["reason"], record["mail_ids"], record["amount"]) == ("pending", "present_capacity", [], 3)
    assert remain(case, waiting) == 3 and case.helper.conversions(waiting) == {}
    assert case.helper.settle(waiting, case.end + timedelta(hours=1)) == (waiting, {
        "grants": [], "pending": [{"event_id": 16, "reason": "present_capacity"}]})
    result, report = case.helper.settle(freed(case, waiting), case.end + timedelta(days=1))
    assert [grant["amount"] for grant in report["grants"]] == [3] and report["pending"] == []
    final = result[ASSAULT_KEY]["events"][EVENT]
    assert final["observed_at"] == record["observed_at"] and final["settled_at"] > record["observed_at"]
    assert final["mail_ids"] == [waiting["last_present_id"] + 1] and remain(case, result) == 0
    assert case.presents.rows(result)[-1]["Content"] == 9000


def test_disabled_rules_skip_new_conversions_but_retry_pending(case, catalogs):
    state = earn(case, case.state, 30)
    disabled = VoteAssaultExpiryService(VoteRewardService(catalogs.disabled, case.presents))
    assert disabled.settle(state, case.end) == (state, QUIET)
    result, report = disabled.settle(freed(case, pending(case)), case.end + timedelta(seconds=1))
    assert report["grants"][0]["amount"] == 3 and result[ASSAULT_KEY]["events"][EVENT]["status"] == "settled"


def test_revival_shift_neither_repeats_nor_moves_a_frozen_conversion(case, catalogs):
    moved = VoteAssaultExpiryService(VoteRewardService(catalogs.moved, case.presents))
    converted, _ = settled(case)
    later = catalogs.moved.event.end_at + timedelta(days=1)
    assert moved.settle(converted, later) == (converted, QUIET)
    waiting = pending(case)
    result, report = moved.settle(freed(case, waiting), case.end + timedelta(seconds=1))
    assert report["grants"]
    assert result[ASSAULT_KEY]["events"][EVENT]["window_end"] == waiting[ASSAULT_KEY]["events"][EVENT]["window_end"]


def test_clock_before_saved_progress_refuses_without_writing(case):
    for state in (pending(case), settled(case)[0]):
        before = deepcopy(state)
        with pytest.raises(ProtocolError) as caught:
            case.helper.settle(state, case.end - timedelta(seconds=1))
        assert (caught.value.code, caught.value.status) == ("vote_assault_expiry_clock_reversed", 409)
        assert state == before


ENTRY_TAMPERS = (("amount", 3), ("amount", True), ("earned", -1), ("rate", 50), ("definition", "b" * 64),
                 ("window_end", "2026-11-16T04:00:00+09:00"), ("observed_at", True), ("observed_at", 0),
                 ("settled_at", None), ("reason", "unexpected"), ("mail_ids", [True]), ("mail_ids", [1, 2]),
                 ("status", "closed"), ("extra", 1))
LEDGER_TAMPERS = {
    "version": lambda ledger: ledger.update(schema_version=2),
    "empty": lambda ledger: ledger["events"].clear(),
    "zero_key": lambda ledger: ledger["events"].update({"0": ledger["events"].pop(EVENT)}),
    "text_key": lambda ledger: ledger["events"].update({"event": ledger["events"].pop(EVENT)}),
    "extra": lambda ledger: ledger.update(source="local"),
}


@pytest.mark.parametrize("field,value", ENTRY_TAMPERS)
def test_invalid_permanent_record_is_rejected_without_rebuilding(case, field, value):
    state, _ = settled(case)
    state[ASSAULT_KEY]["events"][EVENT][field] = value
    before = deepcopy(state)
    for action in (lambda: case.helper.initialize(state), lambda: case.helper.ledger(state),
                   lambda: case.helper.settle(state, case.end + timedelta(days=1)),
                   lambda: case.rewards.assault_response(state)):
        with pytest.raises(ProtocolError) as caught:
            action()
        assert (caught.value.code, caught.value.status) == ("vote_assault_expiry_state_invalid", 503)
    assert state == before


@pytest.mark.parametrize("damage", sorted(LEDGER_TAMPERS))
def test_invalid_conversion_journal_is_rejected(case, damage):
    state, _ = settled(case)
    LEDGER_TAMPERS[damage](state[ASSAULT_KEY])
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(state)
    assert (caught.value.code, caught.value.status) == ("vote_assault_expiry_state_invalid", 503)


def test_record_claiming_other_rights_fails_closed(case):
    state, _ = settled(case)
    state[ASSAULT_KEY]["events"][EVENT].update(earned=3, amount=3)
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(state)
    assert (caught.value.code, caught.value.status) == ("vote_assault_expiry_rights_changed", 503)
    with pytest.raises(ProtocolError) as caught:
        case.rewards.assault_response(state)
    assert (caught.value.code, caught.value.status) == ("vote_rewards_state_invalid", 503)


def test_conversion_without_saved_rights_is_rejected(case):
    state, _ = settled(case)
    del state["vote_rewards"]
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(state)
    assert caught.value.code == "vote_assault_expiry_state_invalid"


def test_settled_mail_identity_and_remaining_mail_are_verified(case):
    state, _ = settled(case)
    identifier = state[ASSAULT_KEY]["events"][EVENT]["mail_ids"][0]
    changed = deepcopy(state)
    next(row for row in changed["tables"]["presents"] if row["PresentID"] == identifier)["Content"] += 1
    future = deepcopy(state)
    future["tables"]["presents"] = [row for row in future["tables"]["presents"] if row["PresentID"] != identifier]
    future[ASSAULT_KEY]["events"][EVENT]["mail_ids"] = [future["last_present_id"] + 1]
    for candidate in (changed, future):
        with pytest.raises(ProtocolError) as caught:
            case.helper.initialize(candidate)
        assert caught.value.code == "vote_assault_expiry_state_invalid"


@pytest.mark.parametrize("provider", ("mail_shape", "mail_content", "mail_identity", "mail_side_effect"))
def test_mail_provider_failure_never_commits_forged_gold(case, monkeypatch, provider):
    source = earn(case, case.state, 30)
    before = deepcopy(source)
    original = case.presents.enqueue_groups

    def forged(state, rewards):
        result, groups = original(state, rewards)
        if provider == "mail_shape":
            groups = []
        elif provider == "mail_content":
            groups[0][0]["Content"] += 1
        elif provider == "mail_identity":
            groups[0][0]["PresentID"] += 1
        else:
            result["tables"]["game_param"][0]["Money"] += 1
        return result, groups

    monkeypatch.setattr(case.presents, "enqueue_groups", forged)
    with pytest.raises(ProtocolError) as caught:
        case.helper.settle(source, case.end)
    assert (caught.value.code, caught.value.status) == ("vote_assault_expiry_delivery_invalid", 503)
    assert source == before


def test_changed_conversion_text_is_not_treated_as_three_thousand_gold(case, vote_sources):
    changed = dict(vote_sources)
    texts = rows(changed, "SystemText.atb")
    found = 0
    for row in texts:
        if RATE_TEXT in row["Data_Text"]:
            row["Data_Text"] = row["Data_Text"].replace(RATE_TEXT, RATE_TEXT.replace("3000", "4000"))
            found += 1
    assert found == 1
    changed["SystemText.atb"] = item_table(texts, (("Recode_Index", 1), ("Data_Text", 32)))
    rewards = VoteRewardService(build(changed, VoteRules(revival_start=REVIVAL)), case.presents)
    with pytest.raises(ProtocolError) as caught:
        VoteAssaultExpiryService(rewards)
    assert (caught.value.code, caught.value.status) == ("vote_assault_expiry_source_invalid", 503)


def test_detached_mailbox_reports_changed_dependencies(case):
    case.helper.presents = copy(case.presents)
    assert (case.helper.status()["ready"], case.helper.status()["error"]) == (False, "vote_assault_expiry_dependencies_changed")
    with pytest.raises(ProtocolError) as caught:
        case.helper.initialize(case.state)
    assert caught.value.code == "vote_assault_expiry_dependencies_changed"


def test_conversions_list_only_settled_gold(case):
    assert case.helper.conversions(pending(case)) == {}
    state, report = settled(case)
    record = state[ASSAULT_KEY]["events"][EVENT]
    assert case.helper.conversions(state) == {EVENT: {"gold": 6000, "mail_ids": report["grants"][0]["mail_ids"],
                                                      "settled_at": record["settled_at"]}}


def test_ticket_expiry_conversions_never_include_challenge_rights(case):
    expiry = VoteExpiryService(case.catalog, case.presents, case.possessions)
    group = case.catalog.groups[0]
    stocked = case.possessions.credit(earn(case, case.state, 30), {group.item_id: 4})
    tickets, _ = expiry.settle(stocked, case.end)
    both, report = case.helper.settle(tickets, case.end)
    assert [grant["amount"] for grant in report["grants"]] == [2]
    conversions = expiry.conversions(both)
    assert list(conversions) == [group.item_id] and conversions[group.item_id]["gold"] == 4 * GOLD_PER_TICKET
    assert conversions[group.item_id]["mail_ids"] != report["grants"][0]["mail_ids"]
    assert case.helper.conversions(both)[EVENT]["gold"] == 2 * ASSAULT_GOLD_PER_RIGHT


@pytest.mark.parametrize("status", ("pending", "settled"))
def test_converted_event_refuses_new_rights(case, status):
    assert case.helper.require_open(earn(case, case.state, 30)) is None
    state = pending(case) if status == "pending" else settled(case)[0]
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        case.helper.require_open(state)
    assert (caught.value.code, caught.value.status) == ("vote_assault_closed", 409)
    assert state == before


def test_status_reports_the_verified_rate(case):
    status = case.helper.status()
    assert status["ready"] is True and status["gold_per_right"] == ASSAULT_GOLD_PER_RIGHT
    assert status["content_version"] == case.helper.content_version and status["native_tested"] is False


def test_startup_after_the_final_vote_end_converts_rights_and_reads_zero_remaining(make_case):
    case = make_case()
    case.transactions.submit(vote(case, 30), case.store)
    voted = case.store.profile()
    case.clock.now = case.catalog.event.end_at
    case.transactions.startup(request(case.store, 137, (), tick=1001), case.store)
    converted = case.store.profile()
    record = converted.state[ASSAULT_KEY]["events"][EVENT]
    assert (record["status"], record["amount"], record["observed_at"]) == ("settled", 2, record["settled_at"])
    mail = next(row for row in case.presents.rows(converted.state) if row["PresentID"] == record["mail_ids"][0])
    assert mail == {"PresentID": record["mail_ids"][0], "Type": 2, "Content": 6000, "Description": ASSAULT_DESCRIPTION}
    assert converted.state["vote_rewards"] == voted.state["vote_rewards"]
    assert case.service.initialize(deepcopy(converted.state)) == converted.state
    directory = tables(case.service.read(request(case.store, 55, (), tick=1002), case.store))
    assert table_rows(directory["ASSAULT"]) == [{"Remain": 0, "Next": 0}]
    assert case.store.profile() == converted
    case.transactions.startup(request(case.store, 137, (), tick=1003), case.store)
    assert case.store.profile() == converted


def test_directory_read_after_the_final_vote_end_leaves_conversion_to_startup(make_case):
    case = make_case()
    case.transactions.submit(vote(case, 30), case.store)
    mail = case.presents.rows(case.store.profile().state)
    case.clock.now = case.catalog.event.end_at
    directory = tables(case.service.read(request(case.store, 55, (), tick=1001), case.store))
    assert table_rows(directory["ASSAULT"]) == [{"Remain": 2, "Next": 0}]
    state = case.store.profile().state
    assert ASSAULT_KEY not in state and case.presents.rows(state) == mail


def test_receipts_committed_before_the_conversion_step_replay_exactly(make_case, monkeypatch):
    case = make_case()
    envelope = vote(case)
    response = case.transactions.submit(envelope, case.store)
    profile = case.store.profile()
    earlier = deepcopy(profile.state)
    receipt = earlier["vote_transactions"]["receipts"][-1]
    assert receipt["content"] == case.transactions.content_version != case.transactions._cooking_content()
    receipt["content"] = case.transactions._cooking_content()
    receipt["revision"] = profile.revision + 1
    case.store.update_state(earlier, profile.revision)
    monkeypatch.setattr(case.bootstrap, "clock", lambda: pytest.fail("An earlier receipt must not observe time"))
    assert case.transactions.submit(envelope, case.store) == response


def test_cooking_receipts_committed_before_the_conversion_step_replay_exactly(factory, monkeypatch):
    case = factory()
    voting = case.transactions
    assert len(voting._replayable_contents()) == 5
    envelope = vote(case)
    response = voting.submit(envelope, case.store)
    profile = case.store.profile()
    earlier = deepcopy(profile.state)
    earlier["vote_transactions"]["receipts"][-1].update(content=voting._cooking_content(), revision=profile.revision + 1)
    case.store.update_state(earlier, profile.revision)
    monkeypatch.setattr(case.bootstrap, "clock", lambda: pytest.fail("An earlier receipt must not observe time"))
    assert voting.submit(envelope, case.store) == response


def test_transactions_require_the_conversion_of_their_own_rewards(make_case, catalog):
    case = make_case()
    voting = case.transactions
    foreign = VoteAssaultExpiryService(VoteRewardService(catalog, copy(case.presents)))
    with pytest.raises(ProtocolError) as caught:
        VoteTransactions(case.service, voting.possessions, voting.rewards, voting.tickets, voting.finalists,
                         voting.expiry, assault_expiry=foreign)
    assert caught.value.code == "vote_transaction_dependencies_changed"
    assert voting.assault_expiry.rewards is voting.rewards

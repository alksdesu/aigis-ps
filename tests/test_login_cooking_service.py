from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import partial
from threading import Barrier, Lock
from types import SimpleNamespace
import hashlib

import pytest

import aigis_ps.login_reward_service as login_module
from aigis_ps.admin_cmd_events import COOKING_PHRASE, MIGRATE_COOKING
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.crystal_spending import initialize_spending, read_spending, settle_spending
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.login_cooking_state import (CRYSTALS_PER_COOKING, RULES_CHANGED, SENDER_FIELD, advance_cooking,
                                          initialize_cooking, migrate_cooking, read_cooking)
from aigis_ps.login_reward_service import (AFFECTION_SENDER, COOKING_DESCRIPTION, FALLBACK_NAME, FALLBACK_SENDER,
                                           FEMALE_KIND, ITEM_SENDER, OFFLINE_COOKING_MIGRATION, CookingSenders,
                                           LoginRewardRules, LoginRewardService, cooking_sender)
from aigis_ps.login_reward_state import initialize_login_rewards, read_login_rewards
from aigis_ps.login_stamp_pending import read_pending_stamps
from aigis_ps.player_state import BARRACKS_SHIFT
from aigis_ps.presents import ITEM_CATALOG
from aigis_ps.startup_diagnostics import StartupDiagnostics
from aigis_ps.storage import Store
from aigis_ps.vote_cooking_catalog import MAX_CARD_ID
from test_login_reward_service import change, factory, fail, groups, message
from test_quest_service import bundle


NOW = datetime(2026, 11, 2, 12, 0, 0, 500000, tzinfo=JST)
MONTH_END, MONTH_START = datetime(2026, 11, 30, 12, tzinfo=JST), datetime(2026, 12, 1, 4, tzinfo=JST)
CHANGED_BORDER = 200
DEFAULT_ITEM = "item_cook_logbo_00004"
PLAYER = "local-player"
# CardData.Kind of these cards is 1, 1, 1, 0 and 2 (a spirit).
FEMALE, TOP_FEMALE, TIED_FEMALE, MALE, SPIRIT = 16, 17, 18, 3, 54
TOP_UNITS = ((FEMALE, 50, 0), (TOP_FEMALE, 120, 2), (MALE, 150, 0), (SPIRIT, 150, 1))
# Digests the service produced before rules named a sender; the item sender keeps them.
ITEM_DIGESTS = {
    LoginRewardRules(): "bbdceee8b799eaaffb3f8663a0f515e23eae9af884917e4e7e7efd790885d7ad",
    LoginRewardRules("2026-10", "item_cook_logbo_00001", "2026-09"):
        "77a81c8e13faad924fd3264ace0f4199e39e9c04b4f4dd9cf89beadfca49817f"}
SENDER_KINDS = {1: 0, FALLBACK_SENDER: FEMALE_KIND, 200: FEMALE_KIND, 201: 2}
SENDER_NAMES = {1: "王子", FALLBACK_SENDER: FALLBACK_NAME, 200: "ベラ", 201: "アイナ"}


def cooking_case(factory, *, previous=265, new=True, now=NOW, same_day=False):
    case = factory(now=now, new=new)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    if "crystal_spending" not in state:
        state = initialize_spending(state, now)
    if "login_rewards" not in state:
        state = initialize_login_rewards(state, now)
    state["crystal_spending"]["previous_spent"] = previous
    state["tables"]["game_param2"][0]["StampCardGrade"] = min(previous, 32767) << 16
    last = now.date() if same_day else now.date() - timedelta(days=1)
    state["login_rewards"].update(last_claim_day=last.isoformat(), stamp_month=last.strftime("%Y-%m"), stamp_count=1)
    state["tables"]["game_param2"][0].update(LastLoginDate=last.toordinal() - 1, LogInStampCount=1)
    state.pop("login_cooking", None)
    state = initialize_cooking(state, border=case.service.cooking_border)
    case.store.update_state(state, profile.revision)
    case.clock.calls = 0
    return case


@pytest.fixture
def case(factory):
    return cooking_case(factory)


def native(state):
    row = state["tables"]["game_param2"][0]
    return row["HomeCookingReceivedAmount"], row["HomeCookingReceivedDate"]


def cooking_mails(case):
    return [row for row in case.presents.rows(case.store.profile().state)
            if row["Type"] == 22 and row.get("ItemID", "").startswith("item_cook_logbo_")]


def rebuild(case, rules=None, catalog=None):
    return LoginRewardService(case.bootstrap, case.service.players, case.presents,
        case.catalog if catalog is None else catalog, clock=case.clock,
        rules=case.service.rules if rules is None else rules)


def held(players, state, units):
    result, _ = players.credit_units(state, [{"card_id": card, "level": 1, "count": 1} for card, _, _ in units])
    for unit, (_, love, barracks) in zip(result["player_state"]["units"][-len(units):], units):
        unit.update(Love=love, GeneralPurposeFlags=unit["GeneralPurposeFlags"] | barracks << BARRACKS_SHIFT)
    return result


def with_border(case, border=CHANGED_BORDER):
    catalog = copy(case.catalog)
    catalog._grades = tuple(replace(grade, cook_gain_border=border if grade.cook_gain_border else 0)
                            for grade in catalog.grades)
    return rebuild(case, catalog=catalog)


def sender_case(factory, sender, units=TOP_UNITS, *, previous=265):
    case = cooking_case(factory, previous=previous)
    profile = case.store.profile()
    case.store.update_state(held(case.service.players, profile.state, units), profile.revision)
    case.service = rebuild(case, replace(case.service.rules, cooking_sender=sender))
    return case


def login_cooking(case):
    return [row for row in case.presents.rows(case.store.profile().state) if row["Description"] == COOKING_DESCRIPTION]


def unit(card, love, identity):
    return {"CardID": card, "Love": love, "UniqueID": identity}


def days(count):
    return [(date(2026, 11, 1) + timedelta(days=offset)).isoformat() for offset in range(count)]


def card_kinds(kinds, kind_type="I"):
    values = tuple(kinds.values()) if kind_type == "I" else tuple(map(str, kinds.values()))
    return dumps(Container(children=(Column("CardID", "I", tuple(kinds)), Column("Kind", kind_type, values))))


def character_recipes(cards, names):
    definitions = {card: SimpleNamespace(name=name) for card, name in names.items()}
    return SimpleNamespace(resource_hashes={"CardData": hashlib.sha256(cards).hexdigest()}, definitions=definitions,
                           definition=definitions.__getitem__)


def test_stamp_beginner_and_cooking_commit_together_without_restoring_energy(case):
    before = case.store.profile()
    result = groups(case.service.receive(message(case), case.store))
    after = case.store.profile()
    assert set(result) == {"SYSTEM", "PGP2", "PPT", "BBNS"}
    assert result["PGP2"].children[0].values == ("A5", "A6", "C1", "CC", "CD")
    assert result["PGP2"].children[1].values[-2:] == ("1", str(NOW.date().toordinal() - 1))
    rows = case.presents.rows(after.state)
    assert [(row["Type"], row["Content"]) for row in rows] == [(1, 6), (0, 3), (22, 1)]
    assert rows[-1]["ItemID"] == DEFAULT_ITEM
    assert read_login_rewards(after.state)["stamp_count"] == 2
    assert read_login_rewards(after.state)["beginner_claimed"] == 1
    assert read_cooking(after.state)["issued"] == 1
    assert after.revision == before.revision + 1
    for name in ("game_param", "crystals", "possessions"):
        assert after.state["tables"][name] == before.state["tables"][name]
    assert after.state["player_state"] == before.state["player_state"]


def test_mailbox_shortage_rolls_back_all_three_reward_states(case):
    profile = case.store.profile()
    state, _ = case.presents.enqueue(profile.state,
        [{"kind": "free_crystal", "amount": 1, "description": "stored"}] * 498)
    case.store.update_state(state, profile.revision)
    fail(case, code="present_capacity")
    assert read_login_rewards(case.store.profile().state)["stamp_count"] == 1
    assert read_login_rewards(case.store.profile().state)["beginner_claimed"] == 0
    assert read_cooking(case.store.profile().state)["issued"] == 0


@pytest.mark.parametrize("number", [35, 42, 115])
def test_encoding_failure_is_atomic_for_all_rewards(case, number):
    case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    fail(case, code="response_too_large")
    assert native(case.store.profile().state) == (0, 0)


def test_failed_mail_enqueue_cannot_partially_commit_cooking(case, monkeypatch):
    observed = []
    def enqueue(state, rewards):
        observed.append((read_cooking(state)["issued"], read_login_rewards(state)["stamp_count"], len(rewards)))
        raise ProtocolError("present_capacity", "Injected full mailbox", 409)
    monkeypatch.setattr(case.presents, "enqueue", enqueue)
    fail(case, code="present_capacity")
    assert observed == [(1, 2, 3)]


def test_cas_retry_uses_one_instant_and_preserves_concurrent_change(case, monkeypatch):
    original = case.store.update_state
    attempts = []
    def write(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            profile = case.store.profile()
            other = deepcopy(profile.state)
            other["sentinel"]["keep"].append(2)
            original(other, profile.revision)
            case.clock.now += timedelta(days=1)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", write)
    case.service.receive(message(case), case.store)
    after = case.store.profile()
    assert len(attempts) == 2 and case.clock.calls == 1
    assert after.state["sentinel"]["keep"] == [1, 2]
    assert read_cooking(after.state)["last_claim_day"] == "2026-11-02"
    assert len(cooking_mails(case)) == 1 and len(case.presents.rows(after.state)) == 3


def test_concurrent_same_request_commits_one_cooking_mail(case, monkeypatch):
    original = case.store.update_state
    barrier, lock, calls = Barrier(2), Lock(), []
    def write(state, revision):
        with lock:
            calls.append(revision)
            wait = len(calls) <= 2
        if wait:
            barrier.wait(timeout=30)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", write)
    before = case.store.profile()
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: case.service.receive(envelope, case.store), range(2)))
    assert responses[0] == responses[1]
    assert case.store.profile().revision == before.revision + 1
    assert len(cooking_mails(case)) == 1 and len(case.presents.rows(case.store.profile().state)) == 3


def test_three_cas_failures_leave_all_counters_and_mail_unchanged(case, monkeypatch):
    attempts = []
    def write(*_):
        attempts.append(1)
        raise ValueError("conflict")
    monkeypatch.setattr(case.store, "update_state", write)
    fail(case, code="profile_changed")
    assert len(attempts) == 3 and case.clock.calls == 1


def test_same_day_new_request_is_receipted_but_does_not_issue_more(case):
    first = message(case)
    case.service.receive(first, case.store)
    before = case.store.profile()
    response = groups(case.service.receive(message(case, tick=1001), case.store))
    after = case.store.profile()
    assert set(response) == {"SYSTEM", "PGP2"}
    assert after.revision == before.revision + 1
    assert after.state["tables"]["presents"] == before.state["tables"]["presents"]
    assert native(after.state) == native(before.state)
    assert after.state["login_reward_history"]["receipts"][-1]["mail_ids"] == []


def test_exact_reopen_retry_does_not_read_clock_or_send_again(case):
    envelope = message(case)
    result = case.service.receive(envelope, case.store)
    before = case.store.profile()
    service = rebuild(case)
    service.clock = lambda: pytest.fail("replay read clock")
    assert service.receive(envelope, Store(case.store.path)) == result
    assert case.store.profile() == before


def test_old_successful_login_with_no_cooking_ledger_is_not_backfilled(factory):
    case = cooking_case(factory, new=False, same_day=True)
    change(case, lambda state: state.pop("login_cooking"))
    response = groups(case.service.receive(message(case), case.store))
    assert set(response) == {"SYSTEM", "PGP2"}
    assert case.presents.rows(case.store.profile().state) == []
    assert native(case.store.profile().state) == (0, 0)


def test_exhausted_cooking_does_not_block_next_stamp_or_beginner(case):
    case.service.receive(message(case), case.store)
    assert len(cooking_mails(case)) == 1
    case.clock.now += timedelta(days=1)
    response = groups(case.service.receive(message(case, tick=1001), case.store))
    assert {"PPT", "BBNS"}.issubset(response)
    assert len(cooking_mails(case)) == 1
    assert len(case.presents.rows(case.store.profile().state)) == 5
    assert read_login_rewards(case.store.profile().state)["stamp_count"] == 3
    assert read_cooking(case.store.profile().state)["issued"] == 1


@pytest.mark.parametrize("item", ["item_cook_logbo_00001", "item_cook_logbo_00002", "item_cook_logbo_00003"])
def test_configured_verified_cooking_item_is_delivered(case, item):
    case.service = rebuild(case, LoginRewardRules(cooking_item_id=item))
    case.service.receive(message(case), case.store)
    assert cooking_mails(case)[0]["ItemID"] == item


@pytest.mark.parametrize("item", ["unknown_item", "gift_0000_bouquet", "item_smn_ticket_19138"])
def test_non_recovery_item_is_not_allowed_as_cooking(case, item):
    before = case.store.profile()
    with pytest.raises(ProtocolError):
        rebuild(case, LoginRewardRules(cooking_item_id=item))
    assert case.store.profile() == before


def test_changed_cooking_item_invalidates_old_receipt(case):
    envelope = message(case)
    case.service.receive(envelope, case.store)
    case.service = rebuild(case, LoginRewardRules(cooking_item_id="item_cook_logbo_00001"))
    fail(case, envelope, "login_reward_retry_stale")


def test_changed_border_loads_but_refuses_115_with_a_warning_until_migrated(case, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    saved_border = case.service.cooking_border
    case.service.receive(message(case), case.store)
    case.clock.now += timedelta(days=1)
    case.service = with_border(case)
    saved = case.store.profile().state
    assert case.service.initialize(saved) == saved
    assert case.service.cooking_rules(saved) == {
        "saved_border": saved_border, "current_border": CHANGED_BORDER, "changed": True, "refused_operations": [115],
        "repair": MIGRATE_COOKING.operation, "offline_repair": list(OFFLINE_COOKING_MIGRATION),
        "client": login_module._REFUSAL_AFTERMATH}
    with pytest.raises(ProtocolError) as caught:
        case.service.current_cooking(saved)
    assert (caught.value.code, caught.value.status) == (RULES_CHANGED, 503)
    assert fail(case, message(case, tick=1001), RULES_CHANGED).status == 503
    assert read_cooking(case.store.profile().state)["issued"] == 1 and len(cooking_mails(case)) == 1
    warnings = [record.getMessage() for record in caplog.records
                if record.levelname == "WARNING" and "request 115" in record.getMessage()]
    assert len(warnings) == 1 and all(text in warnings[0] for text in (
        RULES_CHANGED, f"cooking border {saved_border}", f"use {CHANGED_BORDER};", login_module._REFUSAL_AFTERMATH,
        MIGRATE_COOKING.operation, COOKING_PHRASE, *OFFLINE_COOKING_MIGRATION))
    profile = case.store.profile()
    case.store.update_state(migrate_cooking(profile.state, case.clock.now, border=CHANGED_BORDER), profile.revision)
    migrated = case.store.profile().state
    assert case.service.current_cooking(migrated) == read_cooking(migrated)
    assert case.service.cooking_rules(migrated) == {
        "saved_border": CHANGED_BORDER, "current_border": CHANGED_BORDER, "changed": False}
    case.service.receive(message(case, tick=1002), case.store)
    assert read_cooking(case.store.profile().state)["issued"] == 2 and len(cooking_mails(case)) == 2


def test_a_save_without_its_cooking_ledger_has_no_border_to_migrate(case):
    state = {key: value for key, value in case.store.profile().state.items() if key != "login_cooking"}
    assert case.service.cooking_rules(state) == {
        "saved_border": None, "current_border": case.service.cooking_border, "changed": False}
    with pytest.raises(ProtocolError) as caught:
        case.service.current_cooking(state)
    assert caught.value.code == "login_cooking_required"


def test_changed_border_still_settles_the_month_without_touching_cooking(factory):
    case = cooking_case(factory, new=False, now=MONTH_END)
    case.service.receive(message(case), case.store)
    before = case.store.profile()
    case.service = with_border(case)
    candidate = case.service.prepare_month(before.state, MONTH_START)
    assert read_spending(candidate) == read_spending(settle_spending(before.state, MONTH_START))
    assert read_spending(candidate)["period"] == "2026-12"
    assert read_cooking(candidate) == read_cooking(before.state) and native(candidate) == native(before.state)
    assert case.store.profile() == before


def test_changed_border_does_not_degrade_the_monthly_parameter_loads(factory):
    case = cooking_case(factory, new=False, now=MONTH_END)
    case.service.receive(message(case), case.store)
    before = case.store.profile()
    case.service = with_border(case)
    case.bootstrap.startup_diagnostics = StartupDiagnostics()
    case.bootstrap.monthly_state_provider = case.service.prepare_month
    case.clock.now = MONTH_START
    case.bootstrap.handlers()[35](message(case, columns=(), number=35), case.store)
    after = case.store.profile()
    assert case.bootstrap.status()["startup_diagnostics"] == {"attention_required": False, "loads": {}}
    assert after.revision == before.revision + 1 and read_spending(after.state)["period"] == "2026-12"
    assert after.state["login_cooking"] == before.state["login_cooking"]


def test_missing_unique_positive_border_rejects_service(case):
    catalog = copy(case.catalog)
    catalog._grades = tuple(replace(grade, cook_gain_border=0) for grade in catalog.grades)
    with pytest.raises(ProtocolError) as caught:
        rebuild(case, catalog=catalog)
    assert caught.value.code == "login_reward_cooking_catalog_invalid"


@pytest.mark.parametrize("field", ["content", "state", "response", "mail_ids", "beginner_ordinal"])
def test_changed_receipt_cannot_replay_or_resend_rewards(case, field):
    envelope = message(case)
    case.service.receive(envelope, case.store)
    def corrupt(state):
        entry = state["login_reward_history"]["receipts"][0]
        entry[field] = {"content": "0" * 64, "state": "0" * 64, "response": "bad",
                        "mail_ids": [1, 2], "beginner_ordinal": 2}[field]
    change(case, corrupt, revision=False)
    fail(case, envelope, "login_reward_retry_stale" if field == "content" else "login_reward_history_invalid")


@pytest.mark.parametrize("field,value", [("HomeCookingReceivedAmount", 0), ("HomeCookingReceivedDate", 0)])
def test_native_cooking_projection_corruption_is_rejected(case, field, value):
    envelope = message(case)
    case.service.receive(envelope, case.store)
    change(case, lambda state: state["tables"]["game_param2"][0].update({field: value}), revision=False)
    fail(case, envelope, "login_reward_history_invalid")


def test_old_policy_receipt_is_stale_and_does_not_upgrade_in_place(case):
    envelope = message(case)
    case.service.receive(envelope, case.store)
    change(case, lambda state: state["login_reward_history"]["receipts"][0].update(content="1" * 64), revision=False)
    case.service.clock = lambda: pytest.fail("old receipt read clock")
    fail(case, envelope, "login_reward_retry_stale")


def test_nonzero_legacy_cooking_requires_explicit_month(factory):
    case = cooking_case(factory, new=False)
    def legacy(state):
        state.pop("login_cooking")
        state["tables"]["game_param2"][0].update(HomeCookingReceivedAmount=1,
            HomeCookingReceivedDate=date(2026, 11, 1).toordinal() - 1)
    change(case, legacy)
    fail(case, code="login_cooking_baseline_required")
    case.service = rebuild(case, LoginRewardRules(legacy_cooking_period="2026-11"))
    result = groups(case.service.receive(message(case), case.store))
    assert "PPT" in result and not cooking_mails(case)
    assert native(case.store.profile().state) == (1, date(2026, 11, 1).toordinal() - 1)


def test_older_legacy_cooking_month_resets_count_only_after_sync(factory):
    case = cooking_case(factory, new=False)
    def legacy(state):
        state.pop("login_cooking")
        state["tables"]["game_param2"][0].update(HomeCookingReceivedAmount=25,
            HomeCookingReceivedDate=date(2026, 10, 1).toordinal() - 1)
    change(case, legacy)
    case.service = rebuild(case, LoginRewardRules(legacy_cooking_period="2026-09"))
    initialized = case.service.initialize(case.store.profile().state, case.clock.now)
    assert read_cooking(initialized)["issued"] == 25 and read_cooking(initialized)["period"] == "2026-09"
    case.service.receive(message(case), case.store)
    assert native(case.store.profile().state) == (1, NOW.date().toordinal() - 1)
    assert len(cooking_mails(case)) == 1


def test_prepare_month_resets_issued_preserves_last_civil_date_and_does_not_issue(factory):
    case = cooking_case(factory, new=False, now=datetime(2026, 11, 30, 12, tzinfo=JST))
    case.service.receive(message(case), case.store)
    before = case.store.profile()
    now = datetime(2026, 12, 1, 4, tzinfo=JST)
    candidate = case.service.prepare_month(before.state, now)
    assert read_cooking(candidate)["period"] == "2026-12"
    assert native(candidate) == (0, native(before.state)[1])
    assert candidate["tables"]["presents"] == before.state["tables"]["presents"]
    assert case.store.profile() == before


def test_prepare_month_same_month_does_not_change_state_or_read_service_clock(case):
    before = case.store.profile()
    case.service.clock = lambda: pytest.fail("prepare_month read implicit clock")
    candidate = case.service.prepare_month(before.state, NOW + timedelta(hours=1))
    assert candidate == before.state and candidate is not before.state


def test_positive_cooking_is_issued_while_the_missing_month_stamp_waits(factory):
    case = cooking_case(factory, now=datetime(2027, 1, 2, 12, tzinfo=JST))
    case.service.receive(message(case), case.store)
    after = case.store.profile().state
    assert read_cooking(after)["issued"] == 1
    assert [(row["Type"], row.get("ItemID")) for row in after["tables"]["presents"]] == [
        (0, None), (22, case.service.rules.cooking_item_id)]
    [held] = read_pending_stamps(after)
    assert (held["day"], held["ordinal"], held["reason"]) == ("2027-01-02", 2, "login_reward_month_unavailable")


def test_changed_previous_month_total_cannot_silently_expand_quota(case):
    def alter(state):
        state["crystal_spending"]["previous_spent"] = 625
        state["tables"]["game_param2"][0]["StampCardGrade"] = 625 << 16
    change(case, alter)
    fail(case, code="login_cooking_invalid")


def test_current_month_spending_does_not_change_current_cooking_quota(case):
    def alter(state):
        state["crystal_spending"]["current_spent"] = 10000
        state["tables"]["game_param2"][0]["StampCardGrade"] |= 10000
    change(case, alter)
    case.service.receive(message(case), case.store)
    assert read_cooking(case.store.profile().state)["quota"] == 1
    assert read_spending(case.store.profile().state)["current_spent"] == 10000


def test_later_ledger_clock_blocks_new_request_without_mail(case):
    case.service.receive(message(case), case.store)
    case.clock.now -= timedelta(milliseconds=1)
    fail(case, message(case, tick=1001), "login_rewards_clock_backwards")


def test_affection_ties_draw_one_unit_by_a_reproducible_seed():
    units = [unit(101, 150, 3), unit(102, 100, 1), unit(103, 120, 2), unit(104, 99, 4), unit(201, 150, 5)]
    female = frozenset((101, 102, 103, 104))

    def draw(roster):
        return [cooking_sender(roster, female, player=PLAYER, day=day, issue=issue)
                for issue, day in enumerate(days(25), 1)]

    drawn = draw(units)
    assert set(drawn) == {101, 102, 103}
    assert draw(units[::-1]) == drawn == draw(units)


def test_affection_of_100_and_150_ties():
    female = frozenset((101, 102))
    pair = [unit(101, 150, 1), unit(102, 100, 2)]
    assert {cooking_sender(pair, female, player=PLAYER, day=day, issue=1) for day in days(25)} == {101, 102}
    below = [unit(101, 150, 1), unit(102, 99, 2)]
    assert {cooking_sender(below, female, player=PLAYER, day=day, issue=1) for day in days(25)} == {101}


@pytest.mark.parametrize("units", [[], [unit(201, 150, 1), unit(202, 0, 2)]])
def test_without_a_female_unit_anna_sends(units):
    assert cooking_sender(units, frozenset((101,)), player=PLAYER, day=days(1)[0], issue=1) == FALLBACK_SENDER


@pytest.mark.parametrize("barracks", [1, 2])
def test_affection_mail_comes_from_the_top_female_unit_of_any_barracks(factory, barracks):
    case = sender_case(factory, AFFECTION_SENDER, ((FEMALE, 99, 0), (TOP_FEMALE, 100, barracks), (MALE, 150, 0)))
    assert {FEMALE, TOP_FEMALE, TIED_FEMALE, FALLBACK_SENDER} <= case.service._senders.female
    assert not {MALE, SPIRIT} & case.service._senders.female
    status = case.service.status()
    assert status["ready"] and status["cooking"]["sender"] == AFFECTION_SENDER
    result = groups(case.service.receive(message(case), case.store))
    [mail] = login_cooking(case)
    assert (mail["Type"], mail["Content"]) == (21, TOP_FEMALE)
    assert read_cooking(case.store.profile().state)[SENDER_FIELD] == TOP_FEMALE
    listed = {column.name: column.values for column in result["PPT"].children}
    assert (listed["A1"][-1], listed["A2"][-1]) == (21, TOP_FEMALE)


def test_affection_ties_across_barracks_draw_one_of_them(factory):
    case = sender_case(factory, AFFECTION_SENDER, ((FEMALE, 50, 0), (TOP_FEMALE, 150, 1), (TIED_FEMALE, 100, 2)))
    case.service.receive(message(case), case.store)
    [mail] = login_cooking(case)
    profile = case.store.profile()
    assert mail["Content"] in (TOP_FEMALE, TIED_FEMALE)
    assert mail["Content"] == cooking_sender(profile.state["player_state"]["units"], case.service._senders.female,
                                             player=profile.player_id, day=NOW.date().isoformat(), issue=1)


def test_without_a_female_unit_anna_sends_the_mail(factory):
    case = sender_case(factory, AFFECTION_SENDER, ((MALE, 150, 0), (SPIRIT, 100, 1)))
    case.service.receive(message(case), case.store)
    [mail] = login_cooking(case)
    assert (mail["Type"], mail["Content"]) == (21, FALLBACK_SENDER)
    assert read_cooking(case.store.profile().state)[SENDER_FIELD] == FALLBACK_SENDER


def test_item_sender_keeps_the_digest_of_rules_without_a_sender():
    versions = {ITEM_CATALOG: "4" * 64, "tickets": "5" * 64, "recovery_catalog": "6" * 64}
    owner = SimpleNamespace(catalog=SimpleNamespace(content_version="1" * 64), presents=SimpleNamespace(
        asset_sha256="2" * 64, recovery=SimpleNamespace(content_version="3" * 64), templates={2: 56, 3: 57}),
        _version=versions.__getitem__, _beginner_total=7, _cooking_border=250)
    owner._sender_content = partial(LoginRewardService._sender_content, owner)

    def digest(rules, recipes="7" * 64):
        owner.rules, owner._senders = rules, SimpleNamespace(recipes=SimpleNamespace(content_version=recipes))
        return LoginRewardService._content(owner)

    for rules, recorded in ITEM_DIGESTS.items():
        assert rules == replace(rules, cooking_sender=ITEM_SENDER)
        assert rules.fingerprint().keys() == {"legacy_spending_period", "cooking_item_id", "legacy_cooking_period"}
        assert digest(rules) == digest(rules, "8" * 64) == recorded
        affection = replace(rules, cooking_sender=AFFECTION_SENDER)
        assert len({recorded, digest(affection), digest(affection, "8" * 64)}) == 3


def test_item_sender_keeps_the_live_content_and_mail(case):
    item, affection = (rebuild(case, replace(case.service.rules, cooking_sender=sender)).content_version
                       for sender in (ITEM_SENDER, AFFECTION_SENDER))
    assert item == case.service.content_version != affection
    case.service.receive(message(case), case.store)
    [mail] = login_cooking(case)
    assert (mail["Type"], mail["ItemID"]) == (22, DEFAULT_ITEM)
    assert SENDER_FIELD not in read_cooking(case.store.profile().state)


@pytest.mark.parametrize("sender", ["secretary", "", None, 1])
def test_unknown_cooking_sender_is_refused(sender):
    with pytest.raises(ValueError):
        LoginRewardRules(cooking_sender=sender)


def test_affection_sender_requires_verified_character_cooking(case):
    missing = copy(case.presents)
    missing.vote_cooking_catalog = None
    with pytest.raises(ProtocolError) as caught:
        LoginRewardService(case.bootstrap, case.service.players, missing, case.catalog, clock=case.clock,
                           rules=LoginRewardRules(cooking_sender=AFFECTION_SENDER))
    assert (caught.value.code, caught.value.status) == ("login_cooking_sender_unavailable", 503)
    players = copy(case.service.players)
    players.master_provider = lambda name: b"<DA></DA>"
    with pytest.raises(ProtocolError) as caught:
        LoginRewardService(case.bootstrap, players, case.presents, case.catalog, clock=case.clock,
                           rules=LoginRewardRules(cooking_sender=AFFECTION_SENDER))
    assert (caught.value.code, caught.value.status) == ("login_cooking_sender_catalog_invalid", 503)


def test_senders_are_the_female_kinds_of_the_verified_card_data():
    cards = card_kinds(SENDER_KINDS)
    assert CookingSenders.verified(character_recipes(cards, SENDER_NAMES), cards).female == {FALLBACK_SENDER, 200}


@pytest.mark.parametrize("kinds,names,kind_type", [
    ({**SENDER_KINDS, FALLBACK_SENDER: 0}, SENDER_NAMES, "I"),
    (SENDER_KINDS, {**SENDER_NAMES, FALLBACK_SENDER: "アンナ"}, "I"),
    (SENDER_KINDS, {card: name for card, name in SENDER_NAMES.items() if card != 200}, "I"),
    (SENDER_KINDS, SENDER_NAMES, "S")])
def test_card_data_without_anna_or_cooking_for_every_female_unit_is_refused(kinds, names, kind_type):
    cards = card_kinds(kinds, kind_type)
    with pytest.raises(ProtocolError) as caught:
        CookingSenders.verified(character_recipes(cards, names), cards)
    assert (caught.value.code, caught.value.status) == ("login_cooking_sender_catalog_invalid", 503)


def test_swapped_character_cooking_is_refused_after_setup(factory):
    case = sender_case(factory, AFFECTION_SENDER)
    case.presents.vote_cooking_catalog = copy(case.presents.vote_cooking_catalog)
    fail(case, code="login_reward_dependencies_changed")


def test_item_delivery_drops_the_sender_an_earlier_delivery_recorded(factory):
    case = sender_case(factory, AFFECTION_SENDER, previous=280)
    case.service.receive(message(case), case.store)
    assert read_cooking(case.store.profile().state)[SENDER_FIELD] == TOP_FEMALE
    case.service = rebuild(case, replace(case.service.rules, cooking_sender=ITEM_SENDER))
    case.clock.now += timedelta(days=1)
    case.service.receive(message(case, tick=1001), case.store)
    ledger = read_cooking(case.store.profile().state)
    assert ledger["issued"] == 2 and SENDER_FIELD not in ledger
    assert [(row["Type"], row["Content"]) for row in login_cooking(case)] == [(21, TOP_FEMALE), (22, 1)]


def test_month_refresh_and_border_migration_keep_the_recorded_sender(factory):
    case = sender_case(factory, AFFECTION_SENDER)
    case.service.receive(message(case), case.store)
    state, border = case.store.profile().state, case.service.cooking_border
    assert initialize_cooking(state, border=border) == state
    refreshed = case.service.prepare_month(state, datetime(2026, 12, 1, 4, tzinfo=JST))
    migrated = migrate_cooking(state, NOW, border=border - CRYSTALS_PER_COOKING)
    assert [(read_cooking(candidate)["issued"], read_cooking(candidate)[SENDER_FIELD])
            for candidate in (refreshed, migrated)] == [(0, TOP_FEMALE), (1, TOP_FEMALE)]


@pytest.mark.parametrize("value", [0, MAX_CARD_ID + 1, True, str(TOP_FEMALE)])
def test_recorded_sender_must_be_a_card_identity(factory, value):
    case = sender_case(factory, AFFECTION_SENDER)
    case.service.receive(message(case), case.store)
    state = deepcopy(case.store.profile().state)
    state["login_cooking"][SENDER_FIELD] = value
    with pytest.raises(ProtocolError) as caught:
        read_cooking(state)
    assert (caught.value.code, caught.value.status) == ("login_cooking_invalid", 503)


def test_sender_without_a_delivery_is_invalid(case):
    state = deepcopy(case.store.profile().state)
    assert read_cooking(state)["last_claim_day"] is None
    state["login_cooking"][SENDER_FIELD] = TOP_FEMALE
    with pytest.raises(ProtocolError) as caught:
        read_cooking(state)
    assert (caught.value.code, caught.value.status) == ("login_cooking_invalid", 503)


def test_cooking_sender_is_chosen_by_a_callable(case):
    with pytest.raises(ProtocolError) as caught:
        advance_cooking(case.store.profile().state, NOW, border=case.service.cooking_border, eligible=True,
                        sender=TOP_FEMALE)
    assert caught.value.code == "login_cooking_sender_invalid"

from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from threading import Barrier, Lock
from types import MappingProxyType, SimpleNamespace
from xml.etree import ElementTree as ET
import hashlib
import json

import pytest

import aigis_ps.login_reward_service as login_module
import aigis_ps.request_ledger as ledger
from aigis_ps.admin_cmd_migrations import REBASE_BEGINNER
from aigis_ps.alxml import Column, loads
from aigis_ps.crystal_spending import read_spending
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_rules import EventLoginRules
from aigis_ps.event_login_service import EventLoginService
from aigis_ps.login_reward_catalog import LoginReward
from aigis_ps.login_reward_service import SOURCES, STAMP_DESCRIPTION, LoginRewardRules, LoginRewardService
from aigis_ps.login_reward_state import LEDGER_KEY, RESPONSE_CAPACITY, RULES_CHANGED, read_login_rewards, rebase_beginner
from aigis_ps.login_stamp_pending import (CAPACITY_EXCEEDED, MAX_PENDING_STAMPS, PENDING_KEY,
                                          SCHEMA_VERSION as STAMP_SCHEMA, read_pending_stamps)
from aigis_ps.protocol import RESPONSE_TOO_LARGE
from aigis_ps.storage import Store
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, 0, 0, 500000, tzinfo=JST)
MONTH_UNAVAILABLE = "login_reward_month_unavailable"
STAMPS_FULL = "login_stamp_pending_capacity"


class Clock:
    def __init__(self, now):
        self.now, self.calls = now, 0

    def __call__(self):
        self.calls += 1
        return self.now


@pytest.fixture
def factory(bundle, tmp_path):
    assert bundle.login_rewards is not None
    serial = 0

    def build(*, now=NOW, new=True, old_day=None, old_count=0, legacy_spending=None):
        nonlocal serial
        serial += 1
        bootstrap, presents, catalog = copy(bundle.bootstrap), copy(bundle.presents), copy(bundle.login_rewards.catalog)
        clock = Clock(now)
        bootstrap.clock = clock
        presents.bootstrap = bootstrap
        service = LoginRewardService(bootstrap, bundle.player_state, presents, catalog, clock=clock,
            rules=LoginRewardRules(legacy_spending))
        bootstrap.login_reward_seed_provider = service.seed_new if new else None
        initial = bundle.player_state.initialize(bootstrap.initial_state())
        if old_day is not None:
            initial["tables"]["game_param2"][0].update(LastLoginDate=old_day.toordinal() - 1, LogInStampCount=old_count)
        initial["sentinel"] = {"keep": [1]}
        clock.calls = 0
        return SimpleNamespace(service=service, bootstrap=bootstrap, presents=presents, catalog=catalog, clock=clock,
            store=Store(tmp_path / f"login-{serial}.sqlite3", initial))

    return build


@pytest.fixture
def case(factory):
    return factory()


def message(case, *, tick=1000, columns=None, number=115):
    columns = (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,))) if columns is None else columns
    return request(case.store, number, columns, tick=tick)


def change(case, callback, *, revision=True):
    profile = case.store.profile()
    updated = deepcopy(profile.state)
    callback(updated)
    if revision:
        case.store.update_state(updated, profile.revision)
    else:
        with case.store.connection() as connection:
            connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(updated),))


def fail(case, envelope=None, code=None):
    envelope = message(case) if envelope is None else envelope
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.receive(envelope, case.store)
    assert case.store.profile() == before
    if code:
        assert caught.value.code == code
    return caught.value


def groups(payload):
    return {node.name: node for node in loads(payload).children}


def stamp_variant(catalog, *, copies=(), drop=(), overrides=None):
    # Stands in for updated resources: copies October into other months, drops months or swaps single positions.
    variant, overrides = copy(catalog), overrides or {}
    stamps, months = {}, {}
    for grade, entries in catalog._stamps.items():
        updated = {key: reward for key, reward in entries.items() if key[0] not in drop}
        for month in copies:
            updated.update({(month, ordinal): replace(reward, record_id=month * 100 + ordinal)
                            for (origin, ordinal), reward in entries.items() if origin == 10})
        updated.update({(month, ordinal): reward for (owner, month, ordinal), reward in overrides.items() if owner == grade})
        stamps[grade] = MappingProxyType(updated)
        months[grade] = tuple(sorted({month for month, _ in updated}))
    variant._stamps, variant._months = MappingProxyType(stamps), MappingProxyType(months)
    marker = json.dumps([catalog.content_version, list(copies), list(drop), sorted(map(str, overrides.items()))])
    variant._content_version = hashlib.sha256(marker.encode()).hexdigest()
    return variant


def restart(case, catalog, **services):
    case.catalog = catalog
    case.service = LoginRewardService(case.bootstrap, case.service.players, case.presents, catalog, clock=case.clock,
                                      rules=case.service.rules, **services)
    return case


def pending(day, grade, ordinal, reason, definition=None):
    return {"day": day, "grade": grade, "month": int(day[5:7]), "ordinal": ordinal, "definition": definition,
            "reason": reason}


def stamp_rows(case, grade, month, ordinal):
    return case.presents.preview([case.catalog.stamp(grade, month, ordinal).to_present_reward(STAMP_DESCRIPTION)], 0)


def held_ledger(state, count):
    held = read_pending_stamps(state)
    first = date.fromisoformat(held[0]["day"] if held else read_login_rewards(state)["last_claim_day"])
    earlier = [first - timedelta(days=offset) for offset in range(count - len(held), 0, -1)]
    return {"schema_version": STAMP_SCHEMA,
            "entries": [*(pending(day.isoformat(), 0, day.day, MONTH_UNAVAILABLE) for day in earlier), *held]}


def test_seed_new_then_first_login_enqueues_ticket_and_beginner_without_crediting_wallet(case):
    before = case.store.profile()
    assert read_login_rewards(before.state)["beginner_eligible"]
    assert read_spending(before.state)["baseline_source"] == "new_local"
    response = groups(case.service.receive(message(case), case.store))
    after = case.store.profile()
    assert set(response) == {"SYSTEM", "PGP2", "PPT", "BBNS"}
    assert response["PGP2"].children[0].values == ("A5", "A6", "C1", "CC", "CD")
    assert all(column.type == "S" for column in response["PGP2"].children)
    assert [column.values for column in response["BBNS"].children] == [(1,), (6,)]
    rows = after.state["tables"]["presents"]
    assert [(row["Type"], row["Content"], row.get("SummonID")) for row in rows] == [(16, 1, 350), (0, 3, None)]
    assert after.state["player_state"] == before.state["player_state"]
    assert after.state["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert after.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert after.revision == before.revision + 1


def test_old_zero_account_gets_ordinary_stamp_without_beginner(factory):
    case = factory(new=False)
    response = groups(case.service.receive(message(case), case.store))
    assert "BBNS" not in response
    assert len(case.store.profile().state["tables"]["presents"]) == 1
    assert read_login_rewards(case.store.profile().state)["baseline_source"] == "legacy"
    assert not read_login_rewards(case.store.profile().state)["beginner_eligible"]


def test_existing_native_day_is_not_claimed_again_on_legacy_initialization(factory):
    case = factory(new=False, now=NOW.replace(day=10), old_day=date(2026, 11, 10), old_count=5)
    response = groups(case.service.receive(message(case), case.store))
    assert set(response) == {"SYSTEM", "PGP2"}
    assert case.store.profile().state["tables"]["presents"] == []
    assert read_login_rewards(case.store.profile().state)["stamp_count"] == 5
    assert len(case.store.profile().state["login_reward_history"]["receipts"]) == 1


def test_new_ct_same_day_is_a_receipted_no_reward_action(case):
    first_request = message(case)
    case.service.receive(first_request, case.store)
    first = case.store.profile()
    case.clock.now += timedelta(hours=1)
    second_request = message(case, tick=1001)
    response = groups(case.service.receive(second_request, case.store))
    second = case.store.profile()
    assert set(response) == {"SYSTEM", "PGP2"}
    assert second.state["tables"]["presents"] == first.state["tables"]["presents"]
    assert second.state["last_present_id"] == first.state["last_present_id"]
    assert second.revision == first.revision + 1
    assert second.state["login_reward_history"]["receipts"][-1]["mail_ids"] == []
    fail(case, first_request, "login_reward_retry_stale")


def test_next_day_and_skipped_days_add_one_stamp_each(case):
    case.service.receive(message(case), case.store)
    case.clock.now += timedelta(days=1)
    case.service.receive(message(case, tick=1001), case.store)
    assert read_login_rewards(case.store.profile().state)["stamp_count"] == 2
    case.clock.now += timedelta(days=8)
    case.service.receive(message(case, tick=1002), case.store)
    assert read_login_rewards(case.store.profile().state)["stamp_count"] == 3
    assert read_login_rewards(case.store.profile().state)["beginner_claimed"] == 3
    assert len(case.store.profile().state["tables"]["presents"]) == 6


def test_four_am_month_boundary_resets_stamp_and_rolls_spending(factory):
    case = factory(now=datetime(2026, 11, 1, 3, 59, 59, tzinfo=JST))
    case.service.receive(message(case), case.store)
    assert read_login_rewards(case.store.profile().state)["stamp_month"] == "2026-10"
    case.clock.now = datetime(2026, 11, 1, 4, tzinfo=JST)
    case.service.receive(message(case, tick=1001), case.store)
    ledger = read_login_rewards(case.store.profile().state)
    assert (ledger["stamp_month"], ledger["stamp_count"], ledger["beginner_claimed"]) == ("2026-11", 1, 2)
    assert read_spending(case.store.profile().state)["period"] == "2026-11"


def test_seven_newbie_rewards_do_not_become_daily_forever(case):
    for day in range(8):
        case.clock.now = NOW + timedelta(days=day)
        response = groups(case.service.receive(message(case, tick=1000 + day), case.store))
        assert ("BBNS" in response) is (day < 7)
    ledger = read_login_rewards(case.store.profile().state)
    assert ledger["beginner_claimed"] == 7 and ledger["stamp_count"] == 8
    assert len(case.store.profile().state["tables"]["presents"]) == 15


def test_missing_month_holds_the_stamp_while_progress_and_other_rewards_settle(factory):
    case = factory(now=datetime(2027, 1, 1, 4, tzinfo=JST))
    envelope = message(case)
    payload = case.service.receive(envelope, case.store)
    response, after = groups(payload), case.store.profile()
    progress = read_login_rewards(after.state)
    assert (progress["stamp_month"], progress["stamp_count"], progress["beginner_claimed"]) == ("2027-01", 1, 1)
    assert [column.values for column in response["BBNS"].children] == [(1,), (6,)]
    assert [(row["Type"], row["Content"]) for row in after.state["tables"]["presents"]] == [(0, 3)]
    assert read_pending_stamps(after.state) == [pending("2027-01-01", 0, 1, "login_reward_month_unavailable")]
    case.service.clock = lambda: pytest.fail("Exact retry read the clock")
    assert case.service.receive(envelope, case.store) == payload
    assert case.store.profile() == after


def test_held_stamp_is_mailed_once_after_the_month_table_arrives(factory):
    case = factory(now=datetime(2027, 1, 1, 4, tzinfo=JST))
    case.service.receive(message(case), case.store)
    restart(case, stamp_variant(case.catalog, copies=(1,)))
    case.clock.now += timedelta(hours=2)
    response = groups(case.service.receive(message(case, tick=1001), case.store))
    after = case.store.profile()
    rows = after.state["tables"]["presents"]
    assert PENDING_KEY not in after.state and "PPT" in response and "BBNS" not in response
    assert [{key: value for key, value in row.items() if key != "PresentID"} for row in rows[1:]] == stamp_rows(case, 0, 1, 1)
    assert after.state["login_reward_history"]["receipts"][-1]["mail_ids"] == [row["PresentID"] for row in rows[1:]]
    assert read_login_rewards(after.state)["stamp_count"] == 1
    case.clock.now += timedelta(hours=1)
    assert "PPT" not in groups(case.service.receive(message(case, tick=1002), case.store))
    assert case.store.profile().state["tables"]["presents"] == rows


def test_new_day_mails_today_before_the_stamps_released_from_earlier_days(factory):
    case = factory(new=False, now=datetime(2027, 1, 1, 4, tzinfo=JST))
    case.service.receive(message(case), case.store)
    restart(case, stamp_variant(case.catalog, copies=(1,)))
    case.clock.now += timedelta(days=1)
    case.service.receive(message(case, tick=1001), case.store)
    state = case.store.profile().state
    rows = [{key: value for key, value in row.items() if key != "PresentID"} for row in state["tables"]["presents"]]
    assert rows == [*stamp_rows(case, 0, 1, 2), *stamp_rows(case, 0, 1, 1)]
    assert state["login_reward_history"]["receipts"][-1]["mail_ids"] == [row["PresentID"] for row in state["tables"]["presents"]]
    assert PENDING_KEY not in state


@pytest.mark.parametrize("previous", [50, 100, 150, 250, 265, 800])
def test_high_grade_stamps_with_unavailable_pools_wait_at_their_own_grade(factory, previous):
    case = factory(new=False, legacy_spending="2026-11")
    change(case, lambda state: state["tables"]["game_param2"][0].update(StampCardGrade=previous << 16))
    case.service.receive(message(case), case.store)
    after = case.store.profile()
    grade = case.catalog.grade(previous, NOW)
    presents = after.state["tables"]["presents"]
    assert grade > 0 and all(row.get("ItemID") == case.service.rules.cooking_item_id for row in presents)
    assert read_pending_stamps(after.state) == [pending("2026-11-01", grade, 1, "summon_ticket_pool_disabled",
                                                        case.catalog.stamp(grade, 11, 1).definition)]
    case.clock.now += timedelta(hours=1)
    case.service.receive(message(case, tick=1001), case.store)
    assert read_pending_stamps(case.store.profile().state) == read_pending_stamps(after.state)
    assert case.store.profile().state["tables"]["presents"] == presents


def test_changed_definition_of_a_held_stamp_is_not_silently_mailed(factory):
    case = factory(new=False, legacy_spending="2026-11")
    change(case, lambda state: state["tables"]["game_param2"][0].update(StampCardGrade=265 << 16))
    case.service.receive(message(case), case.store)
    grade = case.catalog.grade(265, NOW)
    original = case.catalog.stamp(grade, 11, 1).definition
    restart(case, stamp_variant(case.catalog, overrides={(grade, 11, 1): LoginReward(0, "", 5, 0, 0, record_id=1101)}))
    case.clock.now += timedelta(hours=1)
    case.service.receive(message(case, tick=1001), case.store)
    after = case.store.profile()
    assert all(row.get("ItemID") == case.service.rules.cooking_item_id for row in after.state["tables"]["presents"])
    assert read_pending_stamps(after.state) == [pending("2026-11-01", grade, 1, "login_reward_definition_changed", original)]


def test_full_mailbox_keeps_a_released_stamp_waiting_without_failing_today(factory):
    case = factory(new=False, now=datetime(2027, 1, 1, 4, tzinfo=JST))
    case.service.receive(message(case), case.store)
    restart(case, stamp_variant(case.catalog, copies=(1,)))
    profile = case.store.profile()
    filler = login_module.MAX_PRESENTS - len(stamp_rows(case, 0, 1, 2))
    updated, _ = case.presents.enqueue(profile.state, [{"kind": "free_crystal", "amount": 1, "description": "old"}] * filler)
    case.store.update_state(updated, profile.revision)
    case.clock.now += timedelta(days=1)
    case.service.receive(message(case, tick=1001), case.store)
    after = case.store.profile()
    assert len(after.state["tables"]["presents"]) == login_module.MAX_PRESENTS
    assert read_pending_stamps(after.state) == [pending("2027-01-01", 0, 1, "present_capacity")]
    assert read_login_rewards(after.state)["stamp_count"] == 2


def test_release_that_overflows_the_response_keeps_waiting(factory, monkeypatch):
    case = factory(new=False, now=datetime(2027, 1, 1, 4, tzinfo=JST))
    case.service.receive(message(case), case.store)
    restart(case, stamp_variant(case.catalog, copies=(1,)))
    original = case.service._response

    def bounded(profile, state, mail_ids, *args, **kwargs):
        if mail_ids:
            raise ProtocolError(RESPONSE_CAPACITY, "Login reward response exceeds its receipt capacity", 503)
        return original(profile, state, mail_ids, *args, **kwargs)

    monkeypatch.setattr(case.service, "_response", bounded)
    case.clock.now += timedelta(hours=1)
    response = groups(case.service.receive(message(case, tick=1001), case.store))
    after = case.store.profile()
    assert "PPT" not in response and after.state["tables"]["presents"] == []
    assert read_pending_stamps(after.state) == [pending("2027-01-01", 0, 1, RESPONSE_CAPACITY)]


def test_releases_per_request_are_bounded_and_resume_on_the_next_request(factory, monkeypatch):
    case = factory(new=False, now=datetime(2027, 1, 1, 4, tzinfo=JST))
    for day in range(3):
        case.service.receive(message(case, tick=1000 + day), case.store)
        case.clock.now += timedelta(days=1)
    case.clock.now -= timedelta(days=1)
    assert [entry["day"] for entry in read_pending_stamps(case.store.profile().state)] == [
        "2027-01-01", "2027-01-02", "2027-01-03"]
    monkeypatch.setattr(login_module, "MAX_STAMP_RELEASES", 2)
    restart(case, stamp_variant(case.catalog, copies=(1,)))
    case.service.receive(message(case, tick=1010), case.store)
    assert [entry["day"] for entry in read_pending_stamps(case.store.profile().state)] == ["2027-01-03"]
    case.service.receive(message(case, tick=1011), case.store)
    rows = [{key: value for key, value in row.items() if key != "PresentID"}
            for row in case.store.profile().state["tables"]["presents"]]
    assert PENDING_KEY not in case.store.profile().state
    assert rows == [*stamp_rows(case, 0, 1, 1), *stamp_rows(case, 0, 1, 2), *stamp_rows(case, 0, 1, 3)]


def test_event_logins_settle_in_the_same_request_that_holds_the_stamp(factory, bundle):
    case = factory()
    assert bundle.event_logins is not None, bundle.event_login_error
    events = EventLoginService(bundle.event_logins.catalog, case.presents,
                               EventLoginRules(enabled=True, campaign_ids=(135,)))
    restart(case, stamp_variant(case.catalog, drop=(11,)), event_logins=events)
    response = groups(case.service.receive(message(case), case.store))
    after = case.store.profile()
    assert response["EBNS"].children[0].values == (135,) and "BBNS" in response
    assert after.state["event_login"]["campaigns"]["135"]["pending"] is None
    assert read_pending_stamps(after.state) == [pending("2026-11-01", 0, 1, "login_reward_month_unavailable")]


@pytest.mark.parametrize("mode", ["schema", "future_day", "ordinal", "order", "empty", "reason"])
def test_corrupted_pending_stamps_fail_closed(factory, mode):
    case = factory(now=datetime(2027, 1, 1, 4, tzinfo=JST))
    case.service.receive(message(case), case.store)
    case.clock.now += timedelta(days=1)

    def corrupt(state):
        ledger_state = state[PENDING_KEY]
        entry = ledger_state["entries"][0]
        if mode == "schema":
            ledger_state["schema_version"] = 2
        elif mode == "future_day":
            entry["day"] = "2027-01-02"
        elif mode == "ordinal":
            entry["ordinal"] = 2
        elif mode == "order":
            ledger_state["entries"] = [entry, dict(entry)]
        elif mode == "empty":
            ledger_state["entries"] = []
        else:
            entry["reason"] = "Not a code"
    change(case, corrupt, revision=False)
    fail(case, message(case, tick=1001), "login_stamp_pending_invalid")
    with pytest.raises(ProtocolError) as caught:
        case.service.initialize(case.store.profile().state)
    assert caught.value.code == "login_stamp_pending_invalid"


def test_old_nonzero_spending_requires_explicit_period(factory):
    case = factory(new=False)
    change(case, lambda state: state["tables"]["game_param2"][0].update(StampCardGrade=1))
    fail(case, code="crystal_spending_baseline_required")


def test_explicit_old_spending_baseline_rolls_without_inventing_intervening_months(factory):
    case = factory(new=False, legacy_spending="2026-09")
    change(case, lambda state: state["tables"]["game_param2"][0].update(StampCardGrade=100))
    case.service.receive(message(case), case.store)
    assert read_spending(case.store.profile().state)["previous_spent"] == 0
    assert read_spending(case.store.profile().state)["current_spent"] == 0


def test_receipt_replay_does_not_read_clock_or_reallocate_mail(case):
    envelope = message(case)
    response = case.service.receive(envelope, case.store)
    before = case.store.profile()
    case.service.clock = lambda: pytest.fail("Exact retry read the clock")
    assert case.service.receive(envelope, Store(case.store.path)) == response
    assert case.store.profile() == before


def test_same_day_no_reward_receipt_cannot_be_replayed_as_next_days_claim(case):
    case.service.receive(message(case), case.store)
    no_reward = message(case, tick=1001)
    response = case.service.receive(no_reward, case.store)
    before = case.store.profile()
    case.clock.now += timedelta(days=1)
    assert case.service.receive(no_reward, case.store) == response
    assert case.store.profile() == before
    case.service.receive(message(case, tick=1002), case.store)
    assert read_login_rewards(case.store.profile().state)["stamp_count"] == 2


def test_initialization_of_existing_ledgers_preserves_exact_receipt_state(case):
    case.service.receive(message(case), case.store)
    before = case.store.profile()
    assert case.service.initialize(before.state, NOW + timedelta(days=30)) == before.state


def test_initialization_reads_the_clock_only_for_a_state_without_its_login_ledger(case):
    state = case.store.profile().state
    assert case.service.initialize(state) == state and case.clock.calls == 0
    initialized = case.service.initialize({key: value for key, value in state.items() if key != LEDGER_KEY})
    assert case.clock.calls == 1 and read_login_rewards(initialized)["baseline_source"] == "legacy"


@pytest.mark.parametrize("number", [35, 42, 115])
def test_all_response_capacity_failures_roll_back_progress_and_mail(case, number):
    case.service._profiles = dict(case.service._profiles)
    case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    fail(case, code=RESPONSE_TOO_LARGE)


def test_mail_capacity_failure_does_not_consume_today(case):
    profile = case.store.profile()
    updated, _ = case.presents.enqueue(profile.state,
        [{"kind": "free_crystal", "amount": 1, "description": "old"}] * 499)
    case.store.update_state(updated, profile.revision)
    fail(case, code="present_capacity")


def test_clock_rollback_is_atomic_for_a_new_request(case):
    case.service.receive(message(case), case.store)
    case.clock.now -= timedelta(milliseconds=1)
    fail(case, message(case, tick=1001), "login_rewards_clock_backwards")


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_authentication_failure_does_not_read_reward_clock(case, field):
    envelope = message(case)
    envelope.document.find(field + "/V").text = "wrong"
    fail(case, envelope)
    assert case.clock.calls == 0


@pytest.mark.parametrize("columns", [(), (Column("SHOP", "I", (0,)),),
    (Column("SHOP", "I", (1,)), Column("NUM", "I", (0,))),
    (Column("SHOP", "I", (0,)), Column("NUM", "I", (1,))),
    (Column("SHOP", "S", ("0",)), Column("NUM", "I", (0,))),
    (Column("SHOP", "I", (0, 0)), Column("NUM", "I", (0,)))])
def test_native_zero_shop_num_shape_is_strict(case, columns):
    fail(case, message(case, columns=columns), "invalid_request_fields")


def test_unknown_business_field_is_rejected(case):
    envelope = message(case)
    ET.SubElement(envelope.document, "Date")
    fail(case, envelope, "invalid_request_fields")


@pytest.mark.parametrize("mode", ["response", "state", "mail_ids", "beginner", "schema", "native_count"])
def test_corrupted_receipt_or_progress_cannot_be_replayed(case, mode):
    envelope = message(case)
    case.service.receive(envelope, case.store)
    def corrupt(state):
        history = state["login_reward_history"]
        entry = history["receipts"][0]
        if mode == "response":
            entry["response"] = "broken"
        elif mode == "state":
            entry["state"] = "0" * 64
        elif mode == "mail_ids":
            entry["mail_ids"] = []
        elif mode == "beginner":
            entry["beginner_ordinal"] = 2
        elif mode == "schema":
            history["schema_version"] = True
        else:
            state["tables"]["game_param2"][0]["LogInStampCount"] = 9
    change(case, corrupt, revision=False)
    fail(case, envelope)


@pytest.mark.parametrize("cached", [False, True])
def test_catalog_version_change_is_not_silently_used(case, cached):
    envelope = message(case)
    if cached:
        case.service.receive(envelope, case.store)
    case.catalog._content_version = "0" * 64
    fail(case, envelope, "login_reward_dependencies_changed")


def test_rules_change_is_detected(case):
    case.service.rules = LoginRewardRules("2026-10")
    fail(case, code="login_reward_dependencies_changed")


def test_readiness_never_builds_the_catalog_or_ticket_status(case, monkeypatch):
    for owner in (case.catalog, case.presents.tickets):
        monkeypatch.setattr(owner, "status", lambda: pytest.fail("Readiness must not build a full status"))
    case.service._ready()
    case.service.receive(message(case), case.store)
    assert len(case.store.profile().state["login_reward_history"]["receipts"]) == 1


@pytest.mark.parametrize("owner", ["catalog", "tickets"])
def test_unready_catalog_or_tickets_refuse_with_the_dependency_code(case, monkeypatch, owner):
    monkeypatch.setattr(case.catalog if owner == "catalog" else case.presents.tickets, "is_ready", lambda: False)
    assert fail(case, code="login_reward_dependencies_changed").status == 503
    assert case.service.status()["error"] == "login_reward_dependencies_changed"


def test_frozen_cooking_border_and_beginner_total_are_read_only_status_values(case):
    status = case.service.status()
    assert case.service.cooking_border == status["cooking"]["border"] > 0
    assert case.service.beginner_total == status["beginner_total"] > 0
    for name in ("cooking_border", "beginner_total"):
        with pytest.raises(AttributeError):
            setattr(case.service, name, 1)


def test_a_changed_beginner_total_loads_but_refuses_115_with_a_warning_until_rebased(case, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    total = case.service.beginner_total
    change(case, lambda state: state[LEDGER_KEY].update(beginner_total=total + 1))
    saved = case.store.profile().state
    assert case.service.initialize(saved) == saved
    assert case.service.beginner_rules(saved) == {
        "saved_total": total + 1, "current_total": total, "changed": True, "refused_operations": [115],
        "repair": REBASE_BEGINNER.operation, "client": login_module._REFUSAL_AFTERMATH}
    with pytest.raises(ProtocolError) as caught:
        case.service.current_ledger(saved)
    assert (caught.value.code, caught.value.status) == (RULES_CHANGED, 503)
    assert fail(case, code=RULES_CHANGED).status == 503
    warnings = [record.getMessage() for record in caplog.records
                if record.levelname == "WARNING" and "request 115" in record.getMessage()]
    assert len(warnings) == 1 and all(text in warnings[0] for text in (
        RULES_CHANGED, f"froze {total + 1} beginner", f"give {total};", login_module._REFUSAL_AFTERMATH,
        REBASE_BEGINNER.operation))
    profile = case.store.profile()
    case.store.update_state(rebase_beginner(profile.state, beginner_count=total), profile.revision)
    rebased = case.store.profile().state
    assert case.service.current_ledger(rebased) == read_login_rewards(rebased)
    assert case.service.beginner_rules(rebased) == {"saved_total": total, "current_total": total, "changed": False}
    case.service.receive(message(case), case.store)
    assert read_login_rewards(case.store.profile().state)["beginner_claimed"] == 1


def test_a_save_without_its_login_ledger_has_no_beginner_total_to_rebase(case):
    state = {key: value for key, value in case.store.profile().state.items() if key != LEDGER_KEY}
    assert case.service.beginner_rules(state) == {
        "saved_total": None, "current_total": case.service.beginner_total, "changed": False}
    with pytest.raises(ProtocolError) as caught:
        case.service.current_ledger(state)
    assert caught.value.code == "login_rewards_required"


@pytest.mark.parametrize("remaining, warning", [(30, False), (29, True)])
def test_pending_stamps_warn_about_a_month_before_their_capacity(case, remaining, warning):
    case.service.receive(message(case), case.store)
    state = case.store.profile().state
    held = {**state, PENDING_KEY: held_ledger(state, MAX_PENDING_STAMPS - remaining)}
    assert case.service.pending_stamps(held) == {"count": MAX_PENDING_STAMPS - remaining,
                                                 "capacity": MAX_PENDING_STAMPS, "remaining": remaining,
                                                 "warning": warning}


def test_full_pending_stamps_refuse_115_on_a_day_whose_stamp_must_wait(factory):
    case = factory(new=False, now=datetime(2027, 1, 1, 4, tzinfo=JST))
    case.service.receive(message(case), case.store)
    change(case, lambda state: state.update({PENDING_KEY: held_ledger(state, MAX_PENDING_STAMPS)}))
    assert case.service.pending_stamps(case.store.profile().state) == {
        "count": MAX_PENDING_STAMPS, "capacity": MAX_PENDING_STAMPS, "remaining": 0, "warning": True,
        "refused_operations": [115], "code": STAMPS_FULL}
    assert login_module.PENDING_STAMPS_FULL is CAPACITY_EXCEEDED
    case.clock.now += timedelta(days=1)
    assert fail(case, message(case, tick=1001), STAMPS_FULL).status == 503


def test_saved_report_joins_the_beginner_cooking_and_stamp_reports(case):
    case.service.receive(message(case), case.store)
    state = case.store.profile().state
    total, border = case.service.beginner_total, case.service.cooking_border
    assert case.service.saved_report(state) == {
        "saved_total": total, "current_total": total, "changed": False,
        "cooking": {"saved_border": border, "current_border": border, "changed": False},
        "pending_stamps": {"count": 0, "capacity": MAX_PENDING_STAMPS, "remaining": MAX_PENDING_STAMPS,
                           "warning": False}}
    changed = {**state, LEDGER_KEY: {**state[LEDGER_KEY], "beginner_total": total + 1}}
    assert case.service.saved_report(changed) == {**case.service.beginner_rules(changed),
                                                  "cooking": case.service.cooking_rules(changed),
                                                  "pending_stamps": case.service.pending_stamps(changed)}
    assert case.service.saved_report(changed)["changed"] is True


def test_concurrent_unrelated_write_is_preserved_and_clock_read_once(case, monkeypatch):
    original = case.store.update_state
    attempts = []
    def conflict(updated, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            profile = case.store.profile()
            other = deepcopy(profile.state)
            other["sentinel"]["keep"].append(2)
            original(other, revision)
        return original(updated, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    case.service.receive(message(case), case.store)
    assert len(attempts) == 2 and case.clock.calls == 1
    assert case.store.profile().state["sentinel"]["keep"] == [1, 2]
    assert len(case.store.profile().state["tables"]["presents"]) == 2


def test_concurrent_identical_request_commits_one_reward_pair(case, monkeypatch):
    original = case.store.update_state
    barrier, lock, attempts = Barrier(2), Lock(), []
    def concurrent(updated, revision):
        with lock:
            attempts.append(revision)
            waiting = len(attempts) <= 2
        if waiting:
            barrier.wait(timeout=30)
        return original(updated, revision)
    monkeypatch.setattr(case.store, "update_state", concurrent)
    envelope = message(case)
    before = case.store.profile()
    with ThreadPoolExecutor(max_workers=2) as executor:
        replies = list(executor.map(lambda _: case.service.receive(envelope, case.store), range(2)))
    assert replies[0] == replies[1]
    after = case.store.profile()
    assert after.revision == before.revision + 1 and len(after.state["tables"]["presents"]) == 2


def test_three_cas_failures_leave_no_ledger_advance_or_mail(case, monkeypatch):
    attempts = []
    def conflict(*args):
        attempts.append(1)
        raise ValueError("conflict")
    monkeypatch.setattr(case.store, "update_state", conflict)
    fail(case, code="profile_changed")
    assert len(attempts) == 3 and case.clock.calls == 1


def test_evicted_request_stays_stale_across_day_change(case, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    first = message(case)
    case.service.receive(first, case.store)
    case.service.receive(message(case, tick=1001), case.store)
    case.clock.now += timedelta(days=1)
    fail(case, first, "login_reward_request_stale")
    assert any("request 115" in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)


def test_rolled_back_client_clock_still_claims_each_new_day(case):
    for tick in (0x7FFFFFFF, -0x80000000, 0x7FFFFFFE):
        case.service.receive(message(case, tick=tick), case.store)
        case.clock.now += timedelta(days=1)
    assert read_login_rewards(case.store.profile().state)["stamp_count"] == 3
    assert len(case.store.profile().state["login_reward_history"]["processed_keys"]) == 3


def test_watermark_receipts_nest_their_sources_and_still_replay(case):
    envelope = message(case, tick=2000)
    response = case.service.receive(envelope, case.store)

    def legacy(state):
        history = state["login_reward_history"]
        receipts = [{**{key: value for key, value in entry.items() if key != "sources"}, **entry["sources"],
                     "client_time": 2000} for entry in history["receipts"]]
        state["login_reward_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
                                         "seen_keys": list(history["processed_keys"]), "receipts": receipts}
    change(case, legacy, revision=False)
    saved = case.store.profile()
    assert case.service.receive(envelope, case.store) == response
    assert case.store.profile() == saved
    case.clock.now += timedelta(days=1)
    case.service.receive(message(case, tick=1000), case.store)
    history = case.store.profile().state["login_reward_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]
    assert all(set(entry) == {"request", "content", "state", "revision", "response", "mail_ids",
                              "beginner_ordinal", "sources"} for entry in history["receipts"])


@pytest.mark.parametrize("count", range(len(SOURCES) + 1))
def test_recorded_watermark_receipts_of_every_fixed_schema_read_with_nested_sources(case, count):
    sources = {name: None if name == "vote_ticket_observation" else [] for name in SOURCES[:count]}
    common = {"request": "1" * 64, "content": "2" * 64, "state": "3" * 64, "revision": 1, "response": "<DA/>"}
    recorded = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000, "seen_keys": ["1" * 64],
                "receipts": [{**common, "client_time": 2000, "mail_ids": [], "beginner_ordinal": 0, **sources}]}
    assert case.service._receipts({"login_reward_history": recorded}, 1) == {
        "schema_version": ledger.SCHEMA_VERSION, "processed_keys": ["1" * 64],
        "receipts": [{**common, "mail_ids": [], "beginner_ordinal": 0, "sources": sources}]}


def test_recorded_watermark_receipt_with_event_grants_replays_exactly(factory, bundle):
    case = factory()
    assert bundle.event_logins is not None, bundle.event_login_error
    events = EventLoginService(bundle.event_logins.catalog, case.presents,
                               EventLoginRules(enabled=True, campaign_ids=(135,)))
    restart(case, case.catalog, event_logins=events)
    envelope = message(case, tick=2000)
    response = case.service.receive(envelope, case.store)
    current = case.store.profile().state["login_reward_history"]
    [receipt] = current["receipts"]
    assert receipt["sources"]["event_login_grants"]

    def recorded(state):
        state["login_reward_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
            "seen_keys": [receipt["request"]], "receipts": [{
                **{name: receipt[name] for name in ("request", "content", "state", "revision", "response")},
                "client_time": 2000, "mail_ids": receipt["mail_ids"], "beginner_ordinal": receipt["beginner_ordinal"],
                **receipt["sources"]}]}
    change(case, recorded, revision=False)
    saved = case.store.profile()
    assert case.service._receipts(saved.state, saved.revision) == current
    assert case.service.receive(envelope, case.store) == response
    assert case.store.profile() == saved


@pytest.mark.parametrize("count,attribute", enumerate((
    "content_version", "_public_receive_content_version", "_comeback_receive_content_version",
    "_purchased_receive_content_version", "_daily_receive_content_version",
    "_cooking_receive_content_version", "receive_content_version")))
def test_receipt_content_follows_the_delivery_schema_of_its_sources(bundle, count, attribute):
    service = bundle.login_rewards
    assert service._receipt_content(dict.fromkeys(SOURCES[:count])) == getattr(service, attribute)


def test_utc_and_naive_jst_clock_views_choose_same_claim_day(factory):
    utc = factory(now=NOW.astimezone(timezone.utc))
    naive = factory(now=NOW.replace(tzinfo=None))
    utc.service.receive(message(utc), utc.store)
    naive.service.receive(message(naive), naive.store)
    assert read_login_rewards(utc.store.profile().state) == read_login_rewards(naive.store.profile().state)

from copy import copy, deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST, anchor_seconds, energy_seconds
from aigis_ps.login_cooking_state import SENDER_FIELD, read_cooking
from aigis_ps.login_reward_service import AFFECTION_SENDER, ITEM_SENDER, LoginRewardRules, LoginRewardService
from aigis_ps.login_reward_state import read_login_rewards
from aigis_ps.login_stamp_pending import read_pending_stamps
from aigis_ps.recovery_service import RecoveryService
from test_evolution_integration import post
from test_login_cooking_service import FEMALE, MALE, TOP_FEMALE, held
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
DEFAULT_ITEM = "item_cook_logbo_00004"


class Clock:
    def __init__(self, now):
        self.now, self.calls = now, 0

    def __call__(self):
        self.calls += 1
        return self.now


@pytest.fixture
def factory(bundle, tmp_path):
    sequence = 0

    def build(*, now=NOW, previous=265, current=0, count=1, old_day=None,
              item_id=DEFAULT_ITEM, old_cooking=0, old_civil=None,
              cooking_period=None, initialize=True, new=False, sender=ITEM_SENDER, units=()):
        nonlocal sequence
        sequence += 1
        local, bootstrap, presents = copy(bundle), copy(bundle.bootstrap), copy(bundle.presents)
        clock = Clock(now)
        bootstrap.clock = clock
        presents.bootstrap = bootstrap
        presents.recovery = RecoveryService(bootstrap, rules=bundle.recovery.rules,
                                           recovery_seconds=bundle.recovery.recovery_seconds)
        service = LoginRewardService(bootstrap, bundle.player_state, presents, bundle.login_rewards.catalog,
            rules=LoginRewardRules(legacy_spending_period=(now - timedelta(hours=4)).strftime("%Y-%m"),
                                  cooking_item_id=item_id, legacy_cooking_period=cooking_period,
                                  cooking_sender=sender))
        bootstrap.login_reward_seed_provider = service.seed_new if new else None
        bootstrap.present_provider = presents.response
        local.bootstrap, local.presents, local.login_rewards = bootstrap, presents, service
        state = bootstrap.initial_state()
        row = state["tables"]["game_param2"][0]
        if not new:
            claimed = old_day or (now - timedelta(hours=4, days=1)).date()
            row.update(LastLoginDate=claimed.toordinal() - 1 if count else 0, LogInStampCount=count,
                       StampCardGrade=(previous << 16) | current,
                       HomeCookingReceivedAmount=old_cooking,
                       HomeCookingReceivedDate=old_civil.toordinal() - 1 if old_civil else 0)
        state["tables"]["game_param"][0]["NowCharisma"] = 20
        state["tables"]["crystals"] = [{"FreeFlag": True, "Date": 1, "Num": 3},
                                        {"FreeFlag": False, "Date": 1, "Num": 4}]
        state["sentinel"] = {"unrelated": [1, 2]}
        if units:
            state = held(local.player_state, local.player_state.initialize(state), units)
        state = local.initialize(state) if initialize else local.player_state.initialize(state)
        case = SimpleNamespace(bundle=local, bootstrap=bootstrap, presents=presents, service=service,
                               clock=clock, state=state, path=tmp_path / str(sequence), initialize=initialize)
        return case

    return build


def application(case):
    return create_app(case.path, initial_state=case.state,
        state_initializer=case.bundle.initialize if case.initialize else None,
        handlers=case.bootstrap.handlers() | case.service.handlers() | case.presents.handlers())


def reopen(case):
    presents = copy(case.presents)
    presents.bootstrap = case.bootstrap
    case.presents = case.bundle.presents = presents
    case.service = LoginRewardService(case.bootstrap, case.bundle.player_state, presents, case.service.catalog,
                                     clock=case.clock, rules=case.service.rules)
    case.bundle.login_rewards = case.service
    case.bootstrap.present_provider = presents.response
    return application(case)


def login(store, tick=1000):
    return request(store, 115, (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,))), tick=tick)


def claim(store, identifier, *, tick=1001, registration=1):
    return request(store, registration, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", (identifier,)),)),)),), tick=tick)


def pairs(node):
    return dict(zip(node.children[0].values, node.children[1].values))


def columns(node):
    return {column.name: column.values for column in node.children}


def cooking_mail(case, state):
    return [row for row in case.presents.rows(state)
            if row["Type"] == 22 and row.get("ItemID") == case.service.rules.cooking_item_id]


def character_mail(case, state):
    return [row for row in case.presents.rows(state) if row["Type"] == 21]


def reject(client, store, envelope, status, code):
    before = store.profile()
    response = post(client, envelope, status)
    assert response.json()["error"]["code"] == code
    assert store.profile() == before


def test_default_cooking_login_mail_single_recovery_and_restart(factory):
    case = factory()
    app = application(case)
    before = app.state.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = login(app.state.store)
        wire, result = post(client, envelope)
        values = pairs(result["PGP2"])
        assert values == {"A5": str(NOW.date().toordinal() - 1), "A6": "2", "C1": str(265 << 16),
                          "CC": "1", "CD": str(NOW.date().toordinal() - 1)}
        assert set(result) == {"SYSTEM", "PGP2", "PPT"}
        assert all(column.type == "S" for column in result["PGP2"].children)
        saved = app.state.store.profile()
        assert saved.state["tables"]["game_param"] == before.state["tables"]["game_param"]
        assert saved.state["player_state"] == before.state["player_state"]
        assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
        assert saved.state["tables"]["possessions"] == before.state["tables"]["possessions"]
        mail, = cooking_mail(case, saved.state)
        assert (mail["Type"], mail["Content"], mail["ItemID"]) == (22, 1, DEFAULT_ITEM)
        _, listed = post(client, request(app.state.store, 42, ()))
        index = columns(listed["PPT"])["PresentID"].index(mail["PresentID"])
        assert columns(listed["PPT"])["A1"][index] == 22
        assert columns(listed["PPT"])["A2"][index] == 1
        assert columns(listed["PPT"])["A6"][index] == DEFAULT_ITEM
        assert post(client, envelope)[0] == wire
        assert app.state.store.profile() == saved
    app = reopen(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        calls = case.clock.calls
        assert post(client, envelope)[0] == wire
        assert case.clock.calls == calls
        assert app.state.store.profile() == saved
        eat = claim(app.state.store, mail["PresentID"])
        eaten_wire, eaten = post(client, eat)
        assert set(eaten) == {"SYSTEM", "PGP", "PPU"}
        assert len(eaten["PPU"].children) == 21
        assert all(not column.values for column in eaten["PPU"].children)
        final = app.state.store.profile()
        expected = 20 + case.bootstrap.rank_rows[0]["CharismaMax"]
        assert final.state["tables"]["game_param"][0]["NowCharisma"] == expected
        assert int(pairs(eaten["PGP"])["A4"]) == expected
        assert anchor_seconds(final.state["tables"]["game_param"][0]) == energy_seconds(NOW)
        assert not cooking_mail(case, final.state)
        assert read_cooking(final.state) == read_cooking(saved.state)
        for table in ("crystals", "possessions", "perused_events"):
            assert final.state["tables"][table] == before.state["tables"][table]
        for number in (35, 33):
            _, loaded = post(client, request(app.state.store, number, ()))
            values = {key: str(loaded[key].values[0]) for key in ("CC", "CD")} if number == 35 else pairs(loaded["PGP2"])
            assert (values["CC"], values["CD"]) == ("1", str(NOW.date().toordinal() - 1))
        assert app.state.store.profile() == final
    app = reopen(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert post(client, eat)[0] == eaten_wire
        assert app.state.store.profile() == final
        reject(client, app.state.store, envelope, 409, "login_reward_retry_stale")


@pytest.mark.parametrize("item_id", ["item_cook_logbo_00001", "item_cook_logbo_00003", "item_sample_cook_001"])
def test_configured_verified_effect11_item_keeps_one_recovery_without_event_flags(factory, item_id):
    case = factory(item_id=item_id)
    app = application(case)
    before = app.state.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, login(app.state.store))
        mail, = cooking_mail(case, app.state.store.profile().state)
        assert mail["Content"] == 1 and mail["ItemID"] == item_id
        post(client, claim(app.state.store, mail["PresentID"]))
        final = app.state.store.profile().state
        assert final["tables"]["game_param"][0]["NowCharisma"] == 20 + case.bootstrap.rank_rows[0]["CharismaMax"]
        assert final["tables"]["perused_events"] == before.state["tables"]["perused_events"]
        assert final["tables"]["possessions"] == before.state["tables"]["possessions"]


@pytest.mark.parametrize("previous,quota", [(250, 0), (264, 0), (265, 1), (280, 2), (625, 25)])
def test_threshold_is_monthly_quota_and_new_ct_same_day_never_adds_second_cooking(factory, previous, quota):
    case = factory(previous=previous)
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, login(app.state.store))
        first = app.state.store.profile()
        assert read_cooking(first.state)["quota"] == quota
        assert len(cooking_mail(case, first.state)) == min(quota, 1)
        case.clock.now += timedelta(hours=1)
        _, second = post(client, login(app.state.store, 1001))
        assert "PPT" not in second
        saved = app.state.store.profile()
        assert saved.state["tables"]["presents"] == first.state["tables"]["presents"]
        assert read_cooking(saved.state)["issued"] == min(quota, 1)
        assert len(saved.state["login_reward_history"]["receipts"]) == 2


def test_skip_days_does_not_backfill_and_exhausted_quota_does_not_block_stamps(factory):
    case = factory(previous=280)
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for index, day in enumerate((2, 10, 20)):
            case.clock.now = NOW.replace(day=day)
            post(client, login(app.state.store, 1000 + index))
            state = app.state.store.profile().state
            assert read_login_rewards(state)["stamp_count"] == 2 + index
            assert len(cooking_mail(case, state)) == min(index + 1, 2)
            assert read_cooking(state)["issued"] == min(index + 1, 2)
        assert state["tables"]["game_param2"][0]["HomeCookingReceivedDate"] == date(2026, 11, 10).toordinal() - 1


def test_four_am_allows_two_logical_days_with_the_same_civil_cd(factory):
    now = datetime(2026, 11, 3, 3, 59, 59, 999999, tzinfo=JST)
    case = factory(now=now, previous=280, old_day=date(2026, 11, 1))
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, first = post(client, login(app.state.store))
        first_values = pairs(first["PGP2"])
        assert first_values["A5"] == str(date(2026, 11, 2).toordinal() - 1)
        assert first_values["CD"] == str(date(2026, 11, 3).toordinal() - 1)
        case.clock.now = now.replace(hour=4, minute=0, second=0, microsecond=0)
        _, second = post(client, login(app.state.store, 1001))
        second_values = pairs(second["PGP2"])
        assert second_values["A5"] == second_values["CD"] == first_values["CD"]
        assert (first_values["CC"], second_values["CC"]) == ("1", "2")
        assert len(cooking_mail(case, app.state.store.profile().state)) == 2


@pytest.mark.parametrize("number", [33, 35])
def test_month_end_read_resets_issued_at_four_without_granting_or_clearing_saved_mail(factory, number):
    case = factory(now=datetime(2026, 10, 31, 12, tzinfo=JST), previous=280, current=265)
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, login(app.state.store))
        case.clock.now = datetime(2026, 11, 1, 3, 59, 59, tzinfo=JST)
        post(client, login(app.state.store, 1001))
        saved = app.state.store.profile()
        assert read_cooking(saved.state)["issued"] == 1
        case.clock.now = datetime(2026, 11, 1, 4, tzinfo=JST)
        _, loaded = post(client, request(app.state.store, number, ()))
        values = {key: str(loaded[key].values[0]) for key in ("CC", "CD")} if number == 35 else pairs(loaded["PGP2"])
        assert (values["CC"], values["CD"]) == ("0", str(date(2026, 10, 31).toordinal() - 1))
        after = app.state.store.profile()
        assert after.state["tables"]["presents"] == saved.state["tables"]["presents"]
        assert read_cooking(after.state)["period"] == "2026-11"
        assert read_cooking(after.state)["quota"] == 1
        assert after.state["crystal_spending"]["previous_spent"] == 265
        assert read_login_rewards(after.state) == read_login_rewards(saved.state)
        post(client, request(app.state.store, number, ()))
        assert app.state.store.profile() == after
        post(client, login(app.state.store, 1002))
        [held] = read_pending_stamps(app.state.store.profile().state)
        assert (held["day"], held["reason"]) == ("2026-11-01", "summon_ticket_pool_disabled")
        assert held["grade"] == case.service.catalog.grade(265, case.clock.now)


@pytest.mark.parametrize("previous", [265, 625, 800])
def test_high_grade_missing_first_stamp_pool_waits_at_its_grade_while_cooking_commits(factory, previous):
    case = factory(previous=previous, count=0)
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, login(app.state.store))
        state = app.state.store.profile().state
        assert len(cooking_mail(case, state)) == len(state["tables"]["presents"]) == 1
        assert read_cooking(state)["issued"] == 1
        [held] = read_pending_stamps(state)
        assert (held["ordinal"], held["reason"]) == (1, "summon_ticket_pool_disabled")
        assert held["grade"] == case.service.catalog.grade(previous, case.clock.now)
        assert held["definition"] == case.service.catalog.stamp(held["grade"], 11, 1).definition


def test_nonzero_legacy_cooking_requires_period_and_never_restarts_quota(factory):
    old_civil = date(2026, 11, 1)
    missing = factory(old_cooking=1, old_civil=old_civil, initialize=False)
    app = application(missing)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, app.state.store, login(app.state.store), 503, "login_cooking_baseline_required")
    case = factory(old_cooking=1, old_civil=old_civil, cooking_period="2026-11")
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, login(app.state.store))
        assert pairs(result["PGP2"])["CC"] == "1"
        assert pairs(result["PGP2"])["CD"] == str(old_civil.toordinal() - 1)
        assert not cooking_mail(case, app.state.store.profile().state)
        assert read_cooking(app.state.store.profile().state)["baseline_source"] == "legacy_explicit"


def test_prior_month_legacy_cooking_resets_only_after_explicit_period(factory):
    case = factory(previous=280, old_cooking=2, old_civil=date(2026, 10, 31), cooking_period="2026-10")
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, login(app.state.store))
        assert pairs(result["PGP2"])["CC"] == "1"
        assert pairs(result["PGP2"])["CD"] == str(NOW.date().toordinal() - 1)
        assert len(cooking_mail(case, app.state.store.profile().state)) == 1


@pytest.mark.parametrize("number", [35, 42, 115])
def test_preencoding_failure_rolls_back_stamp_cooking_mail_and_receipt(factory, number):
    case = factory()
    case.service._profiles = dict(case.service._profiles)
    case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, app.state.store, login(app.state.store), 500, "response_too_large")


def test_full_mailbox_does_not_consume_cooking_or_daily_stamp(factory):
    case = factory()
    case.state, _ = case.presents.enqueue(case.state, [
        {"kind": "gold", "amount": 1, "description": "existing"} for _ in range(499)])
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, app.state.store, login(app.state.store), 409, "present_capacity")
        assert read_cooking(app.state.store.profile().state)["issued"] == 0


def test_cooking_remains_single_claim_only(factory):
    case = factory()
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, login(app.state.store))
        mail, = cooking_mail(case, app.state.store.profile().state)
        before = app.state.store.profile()
        _, result = post(client, claim(app.state.store, mail["PresentID"], registration=117))
        assert set(result) == {"SYSTEM", "PPU"} and not result["PPU"].children[0].values
        assert app.state.store.profile() == before


def test_reconfigured_cooking_item_invalidates_old_receipt_without_granting_replacement(factory):
    case = factory()
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = login(app.state.store)
        post(client, envelope)
        saved = app.state.store.profile()
    case.service = LoginRewardService(case.bootstrap, case.bundle.player_state, case.presents,
        case.service.catalog, rules=replace(case.service.rules, cooking_item_id="item_cook_logbo_00001"))
    case.bundle.login_rewards = case.service
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, app.state.store, envelope, 409, "login_reward_retry_stale")
        assert app.state.store.profile() == saved
        assert cooking_mail(case, saved.state) == []


def test_cas_retry_commits_one_stamp_and_one_cooking_and_preserves_concurrent_state(factory, monkeypatch):
    case = factory()
    app = application(case)
    store = app.state.store
    original, attempts = store.update_state, []

    def conflict(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            current = store.profile()
            other = deepcopy(current.state)
            other["sentinel"]["concurrent"] = True
            original(other, current.revision)
            raise ValueError("concurrent save")
        return original(state, revision)

    monkeypatch.setattr(store, "update_state", conflict)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, login(store))
        state = store.profile().state
        assert len(attempts) == 2
        assert len(cooking_mail(case, state)) == 1
        assert read_cooking(state)["issued"] == 1
        assert read_login_rewards(state)["stamp_count"] == 2
        assert len(state["login_reward_history"]["receipts"]) == 1
        assert state["sentinel"]["concurrent"] is True


def test_affection_cooking_mail_restores_one_charisma_and_replays_after_restart(factory):
    case = factory(sender=AFFECTION_SENDER, units=((FEMALE, 40, 0), (TOP_FEMALE, 130, 1), (MALE, 150, 0)))
    app = application(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = login(app.state.store)
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM", "PGP2", "PPT"}
        assert (pairs(result["PGP2"])["CC"], pairs(result["PGP2"])["CD"]) == ("1", str(NOW.date().toordinal() - 1))
        saved = app.state.store.profile()
        mail, = character_mail(case, saved.state)
        assert (mail["Content"], mail["Description"]) == (TOP_FEMALE, "ログインボーナス（追加報酬）")
        assert read_cooking(saved.state)[SENDER_FIELD] == TOP_FEMALE
        assert not cooking_mail(case, saved.state)
        _, listed = post(client, request(app.state.store, 42, ()))
        index = columns(listed["PPT"])["PresentID"].index(mail["PresentID"])
        assert (columns(listed["PPT"])["A1"][index], columns(listed["PPT"])["A2"][index]) == (21, TOP_FEMALE)
        _, batch = post(client, claim(app.state.store, mail["PresentID"], registration=117))
        assert set(batch) == {"SYSTEM", "PPU"} and app.state.store.profile() == saved
        assert post(client, envelope)[0] == wire
    app = reopen(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert app.state.store.profile() == saved
        _, eaten = post(client, claim(app.state.store, mail["PresentID"], tick=1002))
        final = app.state.store.profile()
        expected = 20 + case.bootstrap.rank_rows[0]["CharismaMax"]
        assert final.state["tables"]["game_param"][0]["NowCharisma"] == int(pairs(eaten["PGP"])["A4"]) == expected
        assert not character_mail(case, final.state)
        assert read_cooking(final.state) == read_cooking(saved.state)
        assert final.state["player_state"] == saved.state["player_state"]
        for table in ("crystals", "possessions", "perused_events"):
            assert final.state["tables"][table] == saved.state["tables"][table]
        reject(client, app.state.store, envelope, 409, "login_reward_retry_stale")

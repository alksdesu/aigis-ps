from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.business_calendar import business_day, civil_day, wall_text
from aigis_ps.crystal_wallet import credit_free_crystals, wallet_totals
from aigis_ps.energy_clock import (JST, PREFIXES, anchor_seconds, convert_state, energy_seconds,
                                   set_anchor, validate_clock)
from aigis_ps.presents import PresentService
from aigis_ps.quest_economy import settle_energy
from aigis_ps.quest_modes import CLEAR_FLAG
from aigis_ps.quest_service import QuestService
from aigis_ps.recovery_service import RecoveryService
from test_evolution_integration import post
from test_quest_service import bundle, exit_request, finish_request, request, start_request


UTC8 = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)


def services(bundle, now=NOW):
    clock = SimpleNamespace(now=now)
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: clock.now
    quests = QuestService(bundle.quests.catalog, bootstrap, bundle.player_state, bundle.possessions,
                          rules=replace(bundle.quests.rules, refill_on_rank_up=False), randbelow=lambda _: 0)
    recovery = RecoveryService(bootstrap, recovery_seconds=quests.rules.charisma_recovery_seconds)
    presents = PresentService(bootstrap, bundle.player_state,
                              asset_reader=lambda name: bundle.native_cdn.assets.read(name)[0],
                              possessions=bundle.possessions)
    bootstrap.present_provider = presents.response
    return SimpleNamespace(clock=clock, bootstrap=bootstrap, quests=quests, recovery=recovery,
                           presents=presents, players=bundle.player_state)


def seed(case, *, crystals=3):
    state = case.players.initialize(case.bootstrap.initial_state())
    state["tables"]["game_param"][0]["NowCharisma"] = 20
    if crystals:
        state = credit_free_crystals(state, crystals, civil_day(case.clock.now))
    return state


def application(case, path, state):
    handlers = (case.bootstrap.handlers() | case.players.handlers() | case.quests.handlers()
                | case.recovery.handlers() | case.presents.handlers())
    return create_app(path, initial_state=state, handlers=handlers)


def game(state):
    return state["tables"]["game_param"][0]


def legacy_utc8(state):
    result = deepcopy(state)
    result.pop("energy_clock")
    for prefix in PREFIXES:
        set_anchor(game(result), anchor_seconds(game(state), prefix) - 3600, prefix)
    return result


def migrate(store):
    before = store.profile()
    result = convert_state(before.state, 480)
    return store.update_state(result, before.revision) if result != before.state else before


def reject(client, envelope, store, status, code):
    before = store.profile()
    response = post(client, envelope, expected=status)
    assert response.json()["error"]["code"] == code
    assert store.profile() == before


@pytest.mark.parametrize("offset", [0, 480, 540])
def test_new_game_and_recovery_use_same_instant_across_timezones(bundle, tmp_path, offset):
    moment = datetime(2026, 10, 31, 15, 5, tzinfo=timezone.utc)
    case = services(bundle, moment.astimezone(timezone(timedelta(minutes=offset))))
    state = seed(case)
    expected_day = datetime(2026, 11, 1).toordinal() - 1
    assert game(state)["CharismaChangeDate"] == game(state)["ActionPointChangeDate"] == expected_day
    for prefix in PREFIXES:
        assert anchor_seconds(game(state), prefix) == expected_day * 86400 + 300
    assert state["created_local"] == wall_text(moment) == "2026/11/01 00:05:00"
    assert state["tables"]["game_param2"][0]["GameStartDateTime"] == state["created_local"]
    assert validate_clock(state) is None
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, loaded = post(client, request(app.state.store, 34, ()))
        assert loaded["A6"].values == (expected_day,)
        _, response = post(client, request(app.state.store, 85, ()))
        saved = app.state.store.profile().state
        assert game(saved)["NowCharisma"] == 20 + case.bootstrap.rank_rows[0]["CharismaMax"]
        assert anchor_seconds(game(saved)) == energy_seconds(moment)
        assert wallet_totals(saved)["free"] == 2
        assert {row["Date"] for row in saved["tables"]["crystals"]} == {expected_day}
        pgp = dict(zip(response["PGP"].children[0].values, response["PGP"].children[1].values))
        assert int(pgp["A6"]) == expected_day


@pytest.mark.parametrize("moment", [NOW, datetime(2026, 10, 31, 15, 30, tzinfo=timezone.utc)])
def test_utc8_conversion_preserves_real_elapsed_time_and_is_idempotent(bundle, moment):
    case = services(bundle, moment)
    source = legacy_utc8(seed(case))
    before = deepcopy(source)
    converted = convert_state(source, 480)
    assert source == before
    expected = deepcopy(source)
    expected["energy_clock"] = converted["energy_clock"]
    for prefix in PREFIXES:
        for suffix in ("ChangeDate", "ChangeTime"):
            game(expected)[prefix + suffix] = game(converted)[prefix + suffix]
        assert anchor_seconds(game(converted), prefix) == energy_seconds(moment)
    assert converted == expected
    same = settle_energy(converted, case.bootstrap.rank_rows, moment)
    assert game(same)["NowCharisma"] == 20
    assert game(settle_energy(converted, case.bootstrap.rank_rows, moment + timedelta(seconds=179)))["NowCharisma"] == 20
    assert game(settle_energy(converted, case.bootstrap.rank_rows, moment + timedelta(seconds=180)))["NowCharisma"] == 21
    repeated = convert_state(converted, 480)
    assert repeated == converted and repeated is not converted


def test_unmarked_old_save_cannot_spend_crystals_or_start_quest(bundle, tmp_path):
    case = services(bundle)
    old = legacy_utc8(seed(case))
    app = application(case, tmp_path, old)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, request(app.state.store, 85, ()), app.state.store, 503, "energy_clock_source_required")
        reject(client, start_request(case.quests, app.state.store), app.state.store, 503, "energy_clock_source_required")


def test_old_paid_recovery_receipt_becomes_stale_without_second_charge(bundle, tmp_path):
    case = services(bundle)
    state = seed(case)
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = request(app.state.store, 85, (), tick=1000)
        post(client, envelope)
        paid = app.state.store.profile()
        assert wallet_totals(paid.state)["free"] == 2
        old = legacy_utc8(paid.state)
        old["recovery_history"]["receipts"][-1].update(content="0" * 64, revision=paid.revision + 1)
        old_profile = app.state.store.update_state(old, paid.revision)
        reject(client, envelope, app.state.store, 503, "energy_clock_source_required")
        converted = migrate(app.state.store)
        assert converted.state["recovery_history"] == old_profile.state["recovery_history"]
        assert game(converted.state)["NowCharisma"] == game(paid.state)["NowCharisma"]
        reject(client, envelope, app.state.store, 409, "recovery_retry_stale")
        assert wallet_totals(app.state.store.profile().state)["free"] == 2
        fresh = request(app.state.store, 85, (), tick=1001)
        wire, _ = post(client, fresh)
        saved = app.state.store.profile()
        assert wallet_totals(saved.state)["free"] == 1
        assert game(saved.state)["NowCharisma"] == game(paid.state)["NowCharisma"] + case.bootstrap.rank_rows[0]["CharismaMax"]
    reopened = application(services(bundle), tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, fresh)[0] == wire and reopened.state.store.profile() == saved


def test_migrated_active_battle_resumes_with_its_original_lottery_and_settles_its_charge(bundle, tmp_path):
    case = services(bundle)
    state = seed(case)
    state["tables"]["quest_records"].append({"QuestID": 1001, "MaxRank": 3, "AchieveFlag": CLEAR_FLAG,
                                            "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": True})
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, start_request(case.quests, app.state.store, 1002))
        started = app.state.store.profile()
        active = deepcopy(started.state["quest_state"]["active"])
        assert active["unit_reservations"] and active["selected_mask"] == 7
        assert active["started_at"] == NOW.timestamp()
        app.state.store.update_state(legacy_utc8(started.state), started.revision)
        converted = migrate(app.state.store)
        assert converted.state["quest_state"]["active"] == active
        assert game(converted.state)["NowCharisma"] == 20
    reopened_case = services(bundle)
    reopened = application(reopened_case, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, response = post(client, finish_request(reopened.state.store, 1002, obtained=1, selected=7, stars=1))
        saved = reopened.state.store.profile()
        assert saved.state["quest_state"]["active"] is None
        assert saved.state["quest_state"]["next_sequence"] == started.state["quest_state"]["next_sequence"]
        assert game(saved.state)["NowCharisma"] == 20 - case.quests.catalog.get(1002).charisma
        assert saved.state["quest_state"]["history"][-1]["completed_at"] == NOW.timestamp()
        assert len(saved.state["player_state"]["units"]) == len(started.state["player_state"]["units"]) + 1
        assert response["PPU"].children[0].values == (4,)


def test_exit_duplicate_window_uses_real_unix_seconds_across_timezone_views(bundle, tmp_path):
    case = services(bundle)
    state = seed(case)
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, start_request(case.quests, app.state.store))
        exiting = exit_request(app.state.store)
        post(client, exiting)
        assert app.state.store.profile().state["quest_state"]["history"][-1]["completed_at"] == NOW.timestamp()
        case.clock.now = (NOW + timedelta(seconds=1)).astimezone(UTC8)
        post(client, start_request(case.quests, app.state.store, tick=1002))
        started = app.state.store.profile()
        app.state.store.update_state(legacy_utc8(started.state), started.revision)
        migrated = migrate(app.state.store)
        assert migrated.state["quest_state"] == started.state["quest_state"]
        case.clock.now = (NOW + timedelta(seconds=4)).astimezone(JST)
        post(client, exiting)
        assert app.state.store.profile() == migrated
        case.clock.now = NOW + timedelta(seconds=6)
        post(client, exiting)
        saved = app.state.store.profile().state
        assert saved["quest_state"]["active"] is None
        assert saved["quest_state"]["history"][-1]["completed_at"] == (NOW + timedelta(seconds=6)).timestamp()


def test_quest_rewards_take_jst_days_when_only_jst_has_crossed_midnight(bundle, tmp_path):
    moment = datetime(2026, 10, 31, 15, 5, tzinfo=timezone.utc)
    case = services(bundle, moment)
    state = seed(case, crystals=0)
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, start_request(case.quests, app.state.store))
        post(client, finish_request(app.state.store))
        saved = app.state.store.profile().state
        assert game(saved)["CharismaChangeDate"] == datetime(2026, 11, 1).toordinal() - 1
        assert saved["tables"]["quest_records"][0]["FirstDefeatDate"] == business_day(moment) == (
            datetime(2026, 10, 31).toordinal() - 1)
        assert {row["Date"] for row in saved["tables"]["crystals"]} == {civil_day(moment)} == {
            datetime(2026, 11, 1).toordinal() - 1}
        assert saved["quest_state"]["history"][-1]["completed_at"] == moment.timestamp()


def test_mail_crystals_take_the_jst_civil_day_before_a_utc8_host_midnight(bundle, tmp_path):
    moment = datetime(2026, 10, 31, 15, 30, tzinfo=timezone.utc)
    case = services(bundle, moment)
    calendar_day = civil_day(moment)
    assert calendar_day == datetime(2026, 11, 1).toordinal() - 1
    state, mail = case.presents.enqueue(seed(case, crystals=0), [
        {"kind": "free_crystal", "amount": 2, "description": "日付の確認"}])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        selected = Container("RT", (Container("PT2", (Column("PresentID", "I", (mail[0]["PresentID"],)),)),))
        _, response = post(client, request(app.state.store, 1, (selected,)))
        saved = app.state.store.profile().state
        assert wallet_totals(saved)["free"] == 2
        assert saved["tables"]["crystals"][0]["Date"] == calendar_day
        assert dict(zip(response["PFC"].children[0].values, response["PFC"].children[1].values))["Date"] == calendar_day

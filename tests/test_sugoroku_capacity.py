from copy import copy, deepcopy
from datetime import datetime, timedelta

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.crystal_spending import settle_spending
from aigis_ps.energy_clock import JST, energy_seconds, set_anchor
from aigis_ps.monthly_capacity import read_capacity
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.quest_economy import credit_quest, game_param_response, settle_energy
from aigis_ps.recovery_service import RecoveryService
from test_quest_service import bundle, request
from test_sugoroku_service import action, factory, nodes, statuses, unchanged


def capacity_ids(case):
    return tuple(row.reward_id for row in case.service.catalog.active(case.clock.now) if row.reward_type == 5)


def test_all_nine_capacity_rewards_add_exactly_two_hundred_to_next_month(factory):
    case = factory()
    ids = capacity_ids(case)
    assert len(ids) == 9
    before = case.store.profile()
    envelope = action(case, identifiers=ids)
    response = case.service.claim(envelope, case.store)
    after = case.store.profile()
    assert statuses(response) == dict.fromkeys(ids, 2)
    assert set(nodes(response)) == {"SYSTEM", "REWARD"}
    assert (read_capacity(after.state)["current"], read_capacity(after.state)["next"]) == (0, 200)
    assert after.state["tables"]["presents"] == before.state["tables"]["presents"]
    assert after.state["tables"]["game_param"][0]["NowCharisma"] == before.state["tables"]["game_param"][0]["NowCharisma"]
    assert case.service.claim(envelope, case.store) == response
    assert case.store.profile() == after
    assert statuses(case.service.claim(action(case, identifiers=ids, tick=1001), case.store)) == dict.fromkeys(ids, 2)
    assert case.store.profile() == after


def test_capacity_and_mail_rewards_share_one_transaction(factory):
    case = factory()
    cap_id = capacity_ids(case)[0]
    before = case.store.profile()
    full, _ = case.bundle.presents.enqueue(before.state, [{"kind": "gold", "amount": 1, "description": "既存"}] * MAX_PRESENTS)
    case.store.update_state(full, before.revision)
    unchanged(case, lambda: case.service.claim(action(case, identifiers=(cap_id, 2)), case.store), "present_capacity")
    assert read_capacity(case.store.profile().state)["next"] == 0
    case.store.update_state(before.state, case.store.profile().revision)
    result = case.service.claim(action(case, identifiers=(cap_id, 42, 1), tick=1001), case.store)
    assert statuses(result) == {cap_id: 2, 42: 2, 1: 0}
    assert set(nodes(result)) == {"SYSTEM", "REWARD", "PPT"}
    assert len(case.store.profile().state["tables"]["presents"]) == 1
    assert read_capacity(case.store.profile().state)["next"] == case.service.catalog.by_id[cap_id].count


def test_game_param_refresh_rolls_capacity_then_expires_without_removing_energy(factory):
    case = factory(now=datetime(2026, 11, 30, 12, tzinfo=JST))
    case.service.claim(action(case, identifiers=capacity_ids(case)), case.store)
    state = case.store.profile().state
    rank = state["tables"]["game_param"][0]["Rank"]
    base = case.bootstrap.rank_rows[rank - 1]["CharismaMax"]
    case.clock.now = datetime(2026, 12, 2, 12, tzinfo=JST)
    case.bootstrap.game_param(request(case.store, 34, ()), case.store)
    after = case.store.profile()
    assert (read_capacity(after.state)["current"], read_capacity(after.state)["next"]) == (200, 0)
    assert after.state["tables"]["game_param"][0]["NowCharisma"] == base + 200
    case.clock.now = datetime(2027, 1, 1, 4, tzinfo=JST)
    case.bootstrap.game_param(request(case.store, 34, ()), case.store)
    expired = case.store.profile().state
    assert (read_capacity(expired)["current"], read_capacity(expired)["next"]) == (0, 0)
    assert expired["tables"]["game_param"][0]["NowCharisma"] == base + 200
    response = game_param_response(expired)
    fields = dict(zip(response.children[0].values, response.children[1].values))
    assert fields["ExtendedMaxCharisma"] == fields["ExtendedMaxCharismaNextMonth"] == "0"


def test_capacity_increase_does_not_retroactively_recover_before_month_boundary(factory):
    case = factory(now=datetime(2026, 11, 30, 23, tzinfo=JST))
    case.service.claim(action(case, identifiers=capacity_ids(case)), case.store)
    state = deepcopy(case.store.profile().state)
    row = state["tables"]["game_param"][0]
    base = case.bootstrap.rank_rows[row["Rank"] - 1]["CharismaMax"]
    row["NowCharisma"] = base
    set_anchor(row, energy_seconds(case.clock.now))
    boundary = datetime(2026, 12, 1, 4, tzinfo=JST)
    before_tick = settle_energy(state, case.bootstrap.rank_rows, boundary + timedelta(seconds=179))
    tick = settle_energy(state, case.bootstrap.rank_rows, boundary + timedelta(seconds=180))
    assert before_tick["tables"]["game_param"][0]["NowCharisma"] == base
    assert tick["tables"]["game_param"][0]["NowCharisma"] == base + 1


def test_recovery_and_rank_up_use_the_effective_month_capacity(factory):
    case = factory(now=datetime(2026, 11, 30, 23, tzinfo=JST))
    case.service.claim(action(case, identifiers=capacity_ids(case)), case.store)
    state = case.store.profile().state
    now = datetime(2026, 12, 1, 4, tzinfo=JST)
    recovery = RecoveryService(case.bootstrap)
    current = settle_energy(state, case.bootstrap.rank_rows, now)
    row = current["tables"]["game_param"][0]
    base = case.bootstrap.rank_rows[row["Rank"] - 1]["CharismaMax"]
    restored = recovery.restore_state(state, now)
    assert restored["tables"]["game_param"][0]["NowCharisma"] == row["NowCharisma"] + base + 200
    gained = credit_quest(current, case.bootstrap.rank_rows, now, money=1,
                         experience=case.bootstrap.rank_rows[0]["NextExp"])
    rank = gained["tables"]["game_param"][0]["Rank"]
    assert rank > row["Rank"]
    assert gained["tables"]["game_param"][0]["NowCharisma"] >= case.bootstrap.rank_rows[rank - 1]["CharismaMax"] + 200


def test_skip_two_months_does_not_leave_capacity_active(factory):
    case = factory(now=datetime(2026, 11, 30, 23, tzinfo=JST))
    case.service.claim(action(case, identifiers=capacity_ids(case)), case.store)
    old = case.store.profile().state
    settled = settle_energy(old, case.bootstrap.rank_rows, datetime(2027, 2, 10, 12, tzinfo=JST))
    assert (read_capacity(settled)["period"], read_capacity(settled)["current"], read_capacity(settled)["next"]) == ("2027-02", 0, 0)
    assert read_capacity(old)["next"] == 200


def test_board_requests_refresh_when_spending_rolls_before_capacity(factory):
    case = factory(now=datetime(2026, 11, 30, 23, tzinfo=JST))
    case.clock.now = datetime(2026, 12, 1, 4, tzinfo=JST)
    saved = case.store.profile()
    case.store.update_state(settle_spending(saved.state, case.clock.now), saved.revision)
    unchanged(case, lambda: case.service.read(request(case.store, 62, ()), case.store), "capacity_refresh_required")
    case.bootstrap.game_param(request(case.store, 34, ()), case.store)
    assert statuses(case.service.read(request(case.store, 62, ()), case.store)) == {}


def test_board_rejects_time_before_latest_energy_settlement(factory):
    case = factory()
    saved = case.store.profile()
    later = case.clock.now + timedelta(hours=1)
    case.store.update_state(settle_energy(saved.state, case.bootstrap.rank_rows, later), saved.revision)
    unchanged(case, lambda: case.service.read(request(case.store, 62, ()), case.store), "capacity_clock_backwards")


def test_recovery_mail_uses_new_month_cap_and_replays_without_a_second_recovery(factory):
    case = factory(now=datetime(2026, 11, 30, 23, tzinfo=JST))
    case.service.claim(action(case, identifiers=capacity_ids(case)), case.store)
    present = copy(case.bundle.presents)
    present.bootstrap = case.bootstrap
    present.recovery = RecoveryService(case.bootstrap)
    saved = case.store.profile()
    state, mail = present.enqueue(saved.state, [{"kind": "charisma_recovery", "description": "test"}])
    case.store.update_state(state, saved.revision)
    case.clock.now = datetime(2026, 12, 1, 4, tzinfo=JST)
    expected = present.recovery.restore_state(state, case.clock.now)
    envelope = request(case.store, 1, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", (mail[0]["PresentID"],)),)),)),))
    response = present.receive(envelope, case.store)
    claimed = case.store.profile()
    assert claimed.state["tables"]["game_param"] == expected["tables"]["game_param"]
    assert read_capacity(claimed.state)["current"] == 200
    pgp = nodes(response)["PGP"]
    assert dict(zip(pgp.children[0].values, pgp.children[1].values))["ExtendedMaxCharisma"] == "200"
    assert present.receive(envelope, case.store) == response
    assert case.store.profile() == claimed

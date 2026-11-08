from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import struct

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.bootstrap import BootstrapService, TABLES
from aigis_ps.business_calendar import civil_day
from aigis_ps.errors import ProtocolError
from aigis_ps.energy_clock import ENCODING, JST, energy_seconds, set_anchor
from aigis_ps.quest_economy import (
    NO_GOLD_ABILITY, UNRATED_STARS, GoldPercents, credit_quest, debit_quest, game_param_response, quest_gold,
    settle_energy,
)
from aigis_ps.wallet_money import GOLD_LIMIT


NOW = datetime(2026, 11, 1, 12, 0, 0)
RANKS = [{"CharismaMax": 60, "NextExp": 16}, {"CharismaMax": 63, "NextExp": 27}, {"CharismaMax": 66, "NextExp": 70}]
INT32_MAX = 0x7FFFFFFF
ANCHOR_FIELDS = ("CharismaChangeDate", "CharismaChangeTime")


def anchored(moment):
    row = {}
    set_anchor(row, energy_seconds(moment))
    return row


@pytest.fixture
def state():
    row = {name: 0 for name, _, _ in TABLES["game_param"].fields}
    row.update(Money=1000, Experience=0, Rank=1, NowCharisma=50, NowActionPoint=4, **anchored(NOW),
               ActionPointChangeDate=civil_day(NOW - timedelta(days=2)), ActionPointChangeTime=0.1)
    return {"bootstrap_schema": 1, "tables": {"game_param": [row], "config": [{"MenuTutorialFlags": 7}]},
            "energy_clock": {"schema_version": 1, "encoding": ENCODING, "utc_offset_minutes": 540, "origin": "new_local"},
            "player_state": {"units": [{"GetIndex": 0}]}, "custom": {"values": [1]}}


def player(state):
    return state["tables"]["game_param"][0]


def charisma_anchor(state):
    return {name: player(state)[name] for name in ANCHOR_FIELDS}


def assert_rank_readable(state, ranks=RANKS):
    service = object.__new__(BootstrapService)
    service.rank_rows = ranks
    BootstrapService._check_rank(service, player(state)["Rank"], player(state)["Experience"])


def assert_actions_preserved(before, after, cost=0):
    assert player(after)["NowActionPoint"] == player(before)["NowActionPoint"] - cost
    for name in ("ActionPointChangeDate", "ActionPointChangeTime"):
        assert player(after)[name] == player(before)[name]


def test_recovery_uses_whole_intervals_and_preserves_fractional_progress(state):
    before = deepcopy(state)
    updated = settle_energy(state, RANKS, NOW + timedelta(seconds=359))
    assert player(updated)["NowCharisma"] == 51
    assert charisma_anchor(updated) == anchored(NOW + timedelta(seconds=180))
    again = settle_energy(updated, RANKS, NOW + timedelta(seconds=360))
    assert player(again)["NowCharisma"] == 52
    assert settle_energy(again, RANKS, NOW + timedelta(seconds=360)) == again
    assert_actions_preserved(before, again)
    assert state == before


def test_recovery_has_no_early_tick_due_to_float32_time_rounding(state):
    anchor = datetime(2026, 11, 1, 23, 56, 59)
    player(state).update(anchored(anchor))
    assert player(settle_energy(state, RANKS, anchor + timedelta(seconds=179)))["NowCharisma"] == 50
    updated = settle_energy(state, RANKS, anchor + timedelta(seconds=180))
    assert player(updated)["NowCharisma"] == 51
    assert charisma_anchor(updated) == anchored(anchor + timedelta(seconds=180))


def test_recovery_crosses_midnight_using_native_days_not_unix_dates(state):
    anchor = datetime(2026, 10, 31, 23, 59, 0)
    player(state).update(anchored(anchor))
    updated = settle_energy(state, RANKS, datetime(2026, 11, 1, 0, 5, 1))
    assert player(updated)["NowCharisma"] == 52
    assert player(updated)["CharismaChangeDate"] == civil_day(datetime(2026, 11, 1))
    assert charisma_anchor(updated) == anchored(datetime(2026, 11, 1, 0, 5, 0))


def test_custom_recovery_interval_and_aware_time_match_server_clock(state):
    local = NOW.replace(tzinfo=JST)
    current = (local + timedelta(seconds=60)).astimezone(timezone.utc)
    assert player(settle_energy(state, RANKS, current, recovery_seconds=60))["NowCharisma"] == 51
    assert player(settle_energy(state, RANKS, NOW + timedelta(seconds=179, microseconds=999999)))["NowCharisma"] == 50


@pytest.mark.parametrize("charisma", [60, 80])
def test_full_or_over_cap_energy_does_not_bank_elapsed_time(state, charisma):
    player(state)["NowCharisma"] = charisma
    later = NOW + timedelta(days=2)
    settled = settle_energy(state, RANKS, later)
    assert player(settled)["NowCharisma"] == charisma
    assert charisma_anchor(settled) == anchored(later)
    debited = debit_quest(settled, RANKS, later, charisma=charisma - 50, action_points=0)
    assert player(debited)["NowCharisma"] == 50
    assert player(settle_energy(debited, RANKS, later + timedelta(seconds=179)))["NowCharisma"] == 50
    assert player(settle_energy(debited, RANKS, later + timedelta(seconds=180)))["NowCharisma"] == 51


def test_reaching_cap_discards_unused_elapsed_time_before_debit(state):
    player(state)["NowCharisma"] = 59
    later = NOW + timedelta(seconds=539)
    updated = debit_quest(state, RANKS, later, charisma=10, action_points=0)
    assert player(updated)["NowCharisma"] == 50
    assert charisma_anchor(updated) == anchored(later)
    assert player(settle_energy(updated, RANKS, later + timedelta(seconds=179)))["NowCharisma"] == 50


def test_debit_below_cap_keeps_recovery_remainder_and_only_deducts_action_points(state):
    updated = debit_quest(state, RANKS, NOW + timedelta(seconds=359), charisma=10, action_points=3)
    assert player(updated)["NowCharisma"] == 41
    assert_actions_preserved(state, updated, cost=3)
    assert player(settle_energy(updated, RANKS, NOW + timedelta(seconds=360)))["NowCharisma"] == 42


@pytest.mark.parametrize("charisma", [50, 60, 80])
def test_clock_rollback_cannot_restore_energy_or_move_anchor_back(state, charisma):
    player(state)["NowCharisma"] = charisma
    earlier = NOW - timedelta(days=1)
    updated = settle_energy(state, RANKS, earlier)
    assert updated == state and updated is not state
    debit = debit_quest(state, RANKS, earlier, charisma=1, action_points=0)
    assert player(debit)["NowCharisma"] == charisma - 1
    assert player(debit)["CharismaChangeDate"] == player(state)["CharismaChangeDate"]
    assert player(debit)["CharismaChangeTime"] == player(state)["CharismaChangeTime"]
    assert player(settle_energy(debit, RANKS, NOW))["NowCharisma"] == charisma - 1


def test_current_capacity_extension_applies_without_using_next_month(state):
    player(state).update(ExtendedMaxCharisma=5, ExtendedMaxCharismaNextMonth=200, NowCharisma=64)
    updated = settle_energy(state, RANKS, NOW + timedelta(seconds=1000))
    assert player(updated)["NowCharisma"] == 65
    ranks = [{"CharismaMax": 0, "NextExp": 10}]
    player(state).update(ExtendedMaxCharisma=0, NowCharisma=0)
    assert player(settle_energy(state, ranks, NOW))["NowCharisma"] == 0


@pytest.mark.parametrize("experience,rank", [(0, 1), (15, 1), (16, 2), (42, 2), (43, 3), (112, 3)])
def test_cumulative_next_exp_rank_boundaries_remain_bootstrap_readable(state, experience, rank):
    updated = credit_quest(state, RANKS, NOW, experience=experience, money=7)
    assert player(updated)["Experience"] == experience and player(updated)["Rank"] == rank
    assert player(updated)["Money"] == 1007
    assert_rank_readable(updated)
    assert_actions_preserved(state, updated)


def test_max_rank_experience_clamps_actual_credit_without_repeated_refills(state):
    updated = credit_quest(state, RANKS, NOW, experience=INT32_MAX, money=0)
    assert player(updated)["Experience"] - player(state)["Experience"] == 112
    assert player(updated)["NowCharisma"] == 66
    player(updated)["NowCharisma"] = 1
    again = credit_quest(updated, RANKS, NOW + timedelta(days=1), experience=100, money=2)
    assert player(again)["Experience"] - player(updated)["Experience"] == 0
    assert player(again)["NowCharisma"] == 1
    assert_rank_readable(again)


def test_experience_clamp_also_respects_int32_when_rank_table_is_larger(state):
    ranks = [{"CharismaMax": 60, "NextExp": INT32_MAX}, {"CharismaMax": 63, "NextExp": INT32_MAX}]
    updated = credit_quest(state, ranks, NOW, experience=INT32_MAX, money=0)
    assert player(updated)["Experience"] == INT32_MAX and player(updated)["Rank"] == 2
    assert_rank_readable(updated, ranks)


@pytest.mark.parametrize("charisma,expected", [(10, 68), (80, 80)])
def test_rank_up_refills_only_charisma_without_reducing_over_cap_holdings(state, charisma, expected):
    player(state).update(NowCharisma=charisma, ExtendedMaxCharisma=5)
    later = NOW + timedelta(seconds=60)
    updated = credit_quest(state, RANKS, later, experience=16, money=0)
    assert player(updated)["NowCharisma"] == expected
    assert charisma_anchor(updated) == anchored(later)
    assert_actions_preserved(state, updated)


def test_rank_up_refill_can_be_disabled_and_never_rewinds_a_future_anchor(state):
    updated = credit_quest(state, RANKS, NOW + timedelta(days=1), experience=16, money=0, refill_on_rank_up=False)
    for name in ("NowCharisma", "CharismaChangeDate", "CharismaChangeTime"):
        assert player(updated)[name] == player(state)[name]
    backward = credit_quest(state, RANKS, NOW - timedelta(days=1), experience=16, money=0)
    assert player(backward)["NowCharisma"] == 63
    assert player(backward)["CharismaChangeDate"] == player(state)["CharismaChangeDate"]
    assert player(backward)["CharismaChangeTime"] == player(state)["CharismaChangeTime"]


def test_credit_does_not_implicitly_recover_with_an_unrequested_interval(state):
    updated = credit_quest(state, RANKS, NOW + timedelta(days=1), experience=1, money=0)
    assert player(updated)["NowCharisma"] == 50
    assert player(updated)["CharismaChangeTime"] == player(state)["CharismaChangeTime"]


def test_all_successful_transforms_deepcopy_and_preserve_unrelated_state(state):
    before = deepcopy(state)
    for updated in (settle_energy(state, RANKS, NOW), debit_quest(state, RANKS, NOW, charisma=0, action_points=0),
                    credit_quest(state, RANKS, NOW, experience=0, money=0)):
        assert updated == before and updated is not state
        updated["custom"]["values"].append(2)
        updated["tables"]["config"][0]["MenuTutorialFlags"] = 0
        updated["player_state"]["units"][0]["GetIndex"] = 2
        assert state == before


def test_pgp_uses_native_keys_and_absolute_values_with_float32_time_text(state):
    before = deepcopy(state)
    response = game_param_response(state)
    assert response.name == "PGP"
    assert response.children[0] == Column("A1", "S", tuple(f"A{i}" for i in range(1, 10)))
    assert response.children[1].type.value == "S"
    values = response.children[1].values
    assert values[:5] == ("1000", "0", "1", "50", "4")
    assert values[5] == str(civil_day(NOW)) and values[6] == "0.5"
    assert struct.pack("<f", float(values[8])) == struct.pack("<f", 0.1)
    assert loads(dumps(Container(children=(response,)))).children == (response,)
    assert state == before


@pytest.mark.parametrize("charisma,action_points", [(51, 0), (0, 5), (51, 5)])
def test_insufficient_cost_fails_without_partial_deduction(state, charisma, action_points):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        debit_quest(state, RANKS, NOW, charisma=charisma, action_points=action_points)
    assert error.value.code == "quest_energy_insufficient" and error.value.status == 409
    assert state == before


@pytest.mark.parametrize("value", [True, -1, 2147483648, "1", 1.5, None])
def test_invalid_amounts_are_rejected_without_mutation(state, value):
    before = deepcopy(state)
    for action in (lambda: debit_quest(state, RANKS, NOW, charisma=value, action_points=0),
                   lambda: debit_quest(state, RANKS, NOW, charisma=0, action_points=value),
                   lambda: credit_quest(state, RANKS, NOW, experience=value, money=0),
                   lambda: credit_quest(state, RANKS, NOW, experience=0, money=value)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "quest_economy_argument"
        assert state == before


@pytest.mark.parametrize("interval", [True, False, 0, -1, 180.0, "180", 2147483648])
def test_recovery_interval_requires_positive_int32_seconds(state, interval):
    with pytest.raises(ProtocolError) as error:
        settle_energy(state, RANKS, NOW, recovery_seconds=interval)
    assert error.value.code == "quest_economy_argument"


@pytest.mark.parametrize("bad_state", [None, [], {}, {"bootstrap_schema": True, "tables": {}},
    {"bootstrap_schema": 2, "tables": {}}, {"bootstrap_schema": 1, "tables": []},
    {"bootstrap_schema": 1, "tables": {}}, {"bootstrap_schema": 1, "tables": {"game_param": []}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{}, {}]}}])
def test_invalid_state_shape_is_not_repaired(bad_state):
    before = deepcopy(bad_state)
    for action in (lambda: settle_energy(bad_state, RANKS, NOW), lambda: game_param_response(bad_state)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "quest_economy_state" and error.value.status == 503
        assert bad_state == before


@pytest.mark.parametrize("field,value", [("Money", True), ("Money", -1), ("Experience", 2147483648), ("Rank", 0),
    ("NowCharisma", -1), ("NowActionPoint", True), ("CharismaChangeDate", -1), ("ActionPointChangeDate", "1"),
    ("CharismaChangeTime", True), ("CharismaChangeTime", -0.1), ("CharismaChangeTime", 1),
    ("CharismaChangeTime", 0.9999999999), ("CharismaChangeTime", math.inf), ("CharismaChangeTime", math.nan),
    ("ActionPointChangeTime", math.inf), ("ExtendedMaxCharisma", -1)])
def test_invalid_persisted_values_fail_before_state_changes(state, field, value):
    player(state)[field] = value
    before = deepcopy(state)
    for action in (lambda: settle_energy(state, RANKS, NOW), lambda: credit_quest(state, RANKS, NOW, experience=1, money=1),
                   lambda: game_param_response(state)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "quest_economy_state" and error.value.status == 503
        assert state == before


@pytest.mark.parametrize("ranks", [None, [], [{}], [{"CharismaMax": 60, "NextExp": 0}],
    [{"CharismaMax": -1, "NextExp": 1}], [{"CharismaMax": True, "NextExp": 1}], [{"CharismaMax": 60, "NextExp": True}]])
def test_invalid_rank_data_is_unavailable(state, ranks):
    with pytest.raises(ProtocolError) as error:
        credit_quest(state, ranks, NOW, experience=1, money=1)
    assert error.value.code == "quest_rank_data" and error.value.status == 503


@pytest.mark.parametrize("rank,experience", [(2, 0), (1, 16), (3, 113), (4, 112)])
def test_existing_rank_and_experience_must_match_bootstrap_intervals(state, rank, experience):
    player(state).update(Rank=rank, Experience=experience)
    with pytest.raises(ProtocolError) as error:
        settle_energy(state, RANKS, NOW)
    assert error.value.code == "quest_economy_state"


@pytest.mark.parametrize("money,reward,expected", [
    (GOLD_LIMIT - 10, 7, GOLD_LIMIT - 3), (GOLD_LIMIT - 10, 10, GOLD_LIMIT), (GOLD_LIMIT - 10, 11, GOLD_LIMIT),
    (GOLD_LIMIT - 1, INT32_MAX, GOLD_LIMIT), (GOLD_LIMIT, 1, GOLD_LIMIT), (GOLD_LIMIT + 5, 1, GOLD_LIMIT + 5),
    (INT32_MAX, INT32_MAX, INT32_MAX)])
def test_gold_beyond_the_limit_is_discarded_while_the_rest_of_the_reward_settles(state, money, reward, expected):
    player(state)["Money"] = money
    before = deepcopy(state)
    updated = credit_quest(state, RANKS, NOW, experience=16, money=reward)
    assert player(updated)["Money"] == expected
    assert (player(updated)["Experience"], player(updated)["Rank"], player(updated)["NowCharisma"]) == (16, 2, 63)
    assert_rank_readable(updated)
    assert state == before


def test_charisma_capacity_overflow_is_unavailable(state):
    player(state)["ExtendedMaxCharisma"] = INT32_MAX
    with pytest.raises(ProtocolError) as error:
        settle_energy(state, RANKS, NOW)
    assert error.value.code == "quest_economy_state"


def test_zero_money_and_experience_are_not_silently_accepted(state):
    player(state)["Money"] = 0
    with pytest.raises(ProtocolError) as error:
        credit_quest(state, RANKS, NOW, experience=1, money=1)
    assert error.value.code == "quest_economy_state"


@pytest.mark.parametrize("now", [None, 0, "2026-11-01"])
def test_invalid_clock_arguments_do_not_mutate_state(state, now):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        settle_energy(state, RANKS, now)
    assert error.value.code == "quest_economy_argument" and state == before


def test_refill_policy_requires_bool(state):
    with pytest.raises(ProtocolError) as error:
        credit_quest(state, RANKS, NOW, experience=16, money=0, refill_on_rank_up=1)
    assert error.value.code == "quest_economy_argument"


def settled_gold(gold=450, *, gold_percent=100, events=0, gold_per_event=100, stars=3, percents=NO_GOLD_ABILITY,
                 money_up=False):
    return quest_gold(gold, gold_percent=gold_percent, events=events, gold_per_event=gold_per_event, stars=stars,
                      percents=percents, money_up=money_up)


@pytest.mark.parametrize("stars,money", [(UNRATED_STARS, 450), (1, 450), (2, 540), (3, 675)])
def test_each_star_count_pays_its_public_gold_rate(stars, money):
    assert settled_gold(stars=stars) == money


def test_the_rates_multiply_exactly_and_floor_once():
    # Three stars under MoneyUp pay floor(5 * 2.25) = 11, one more than two successive floors.
    assert settled_gold(5, stars=3, money_up=True) == 11


def test_gold_events_pay_the_strongest_event_percent_before_the_shared_rates():
    percents = GoldPercents(reward=0, event=30)
    assert settled_gold(0, events=2, stars=1, percents=percents) == 260
    assert settled_gold(0, events=2, stars=3, percents=percents) == 390


def test_the_reward_percent_scales_the_base_and_the_gold_events_together():
    percents = GoldPercents(reward=20, event=30)
    assert settled_gold(450, events=2, stars=1, percents=percents) == (450 + 260) * 120 // 100
    assert settled_gold(450, events=2, stars=3, percents=percents, money_up=True) == 1917


def test_the_strongest_percent_of_each_gold_ability_kind_wins_without_stacking():
    assert GoldPercents(20, 0).strongest(GoldPercents(15, 30)) == GoldPercents(20, 30)
    assert NO_GOLD_ABILITY.strongest(NO_GOLD_ABILITY) == NO_GOLD_ABILITY


def test_gold_past_int32_settles_at_the_native_maximum():
    assert settled_gold(INT32_MAX, stars=3, money_up=True) == INT32_MAX


@pytest.mark.parametrize("change", [{"stars": 4}, {"stars": -1}, {"stars": True}, {"money_up": 1},
                                    {"gold": -1}, {"gold_percent": -1}, {"events": -1}, {"gold_per_event": -1},
                                    {"percents": GoldPercents(-1, 0)}, {"percents": GoldPercents(0, -1)}])
def test_gold_rates_require_native_star_counts_flags_and_nonnegative_percents(change):
    with pytest.raises(ProtocolError) as error:
        settled_gold(**change)
    assert error.value.code == "quest_economy_argument"

from copy import copy, deepcopy
from datetime import datetime, timedelta

from aigis_ps.alxml import loads
from aigis_ps.bootstrap import TABLES
from aigis_ps.crystal_spending import initialize_spending
from aigis_ps.energy_clock import ENCODING, JST, energy_seconds, set_anchor
from aigis_ps.monthly_capacity import grant_next_capacity, initialize_capacity
from aigis_ps.quest_economy import credit_quest, settle_energy
from aigis_ps.storage import Store
from aigis_ps.sugoroku_service import SugorokuService
from test_quest_service import bundle, request


RANKS = [{"NextExp": 1000, "CharismaMax": 60}]
NOW = datetime(2026, 11, 10, 12, tzinfo=JST)


def energy_state():
    row = {name: 0 for name, _, _ in TABLES["game_param"].fields}
    row.update(Money=1000, Experience=0, Rank=1, NowCharisma=20, NowActionPoint=12)
    for prefix in ("Charisma", "ActionPoint"):
        set_anchor(row, energy_seconds(NOW), prefix)
    state = {"bootstrap_schema": 1,
        "energy_clock": {"schema_version": 1, "encoding": ENCODING,
                         "utc_offset_minutes": 540, "origin": "new_local"},
        "tables": {"game_param": [row], "game_param2": [{"StampCardGrade": 0, "LogInStampCount": 0}]}}
    return initialize_capacity(initialize_spending(state, NOW, origin="new_local"))


def test_credit_after_custom_settlement_does_not_recover_at_default_interval():
    state = energy_state()
    before = deepcopy(state)
    later = NOW + timedelta(seconds=599)
    settled = settle_energy(state, RANKS, later, recovery_seconds=600)
    assert settled["tables"]["game_param"][0]["NowCharisma"] == 20
    rewarded = credit_quest(settled, RANKS, later, experience=1, money=1,
                            refill_on_rank_up=False, recovery_seconds=600)
    assert rewarded["tables"]["game_param"][0]["NowCharisma"] == 20
    assert rewarded["tables"]["game_param"][0]["Money"] == 1001
    assert rewarded["tables"]["game_param"][0]["Experience"] == 1
    one_tick = settle_energy(rewarded, RANKS, NOW + timedelta(seconds=600), recovery_seconds=600)
    assert one_tick["tables"]["game_param"][0]["NowCharisma"] == 21
    assert state == before


def test_board_month_preparation_and_34_use_frozen_custom_recovery_interval(bundle, tmp_path):
    anchor = datetime(2026, 12, 1, 3, 57, tzinfo=JST)
    later = datetime(2026, 12, 1, 4, 2, tzinfo=JST)
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: anchor
    state = bundle.initialize(bootstrap.initial_state())
    row = state["tables"]["game_param"][0]
    row["NowCharisma"] = 0
    set_anchor(row, energy_seconds(anchor))
    state = grant_next_capacity(state, 20, anchor)
    original = deepcopy(state)
    service = SugorokuService(bootstrap, bundle.player_state, bundle.presents,
                             bundle.sugoroku.catalog, recovery_seconds=600)
    default = SugorokuService(bootstrap, bundle.player_state, bundle.presents,
                             bundle.sugoroku.catalog, recovery_seconds=180)
    assert service.content_version != default.content_version
    prepared = service.prepare_month(state, later)
    assert prepared["tables"]["game_param"][0]["NowCharisma"] == 0
    assert prepared["monthly_capacity"]["current"] == 20
    assert prepared["monthly_capacity"]["next"] == 0
    assert state == original

    bootstrap.clock = lambda: later
    bootstrap.monthly_state_provider = service.prepare_month
    store = Store(tmp_path / "capacity.sqlite3", state)
    payload = bootstrap.game_param(request(store, 34, ()), store)
    columns = {column.name: column for column in loads(payload).children}
    assert columns["A4"].values == (0,)
    assert columns["ExtendedMaxCharisma"].values == (20,)
    assert columns["ExtendedMaxCharismaNextMonth"].values == (0,)
    saved = store.profile()
    assert saved.state == prepared
    assert bootstrap.game_param(request(store, 34, ()), store) == payload
    assert store.profile() == saved

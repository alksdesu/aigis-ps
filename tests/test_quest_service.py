from argparse import Namespace
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from functools import cache
import json
import logging
import math
from pathlib import Path
import re
from threading import Barrier, Lock
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import aigis_ps.event_pass_catalog as event_pass_catalog
import aigis_ps.mission_catalog as mission_catalog
import aigis_ps.quest_service as module
import aigis_ps.resource_view as resource_view
from aigis_ps import quest_modes
from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.battle_events import NOT_ATTACHED, UNAVAILABLE
from aigis_ps.business_calendar import business_day, civil_day
from aigis_ps.cli import services
from aigis_ps.energy_clock import JST, energy_seconds, instant, set_anchor
from aigis_ps.errors import ProtocolError
from aigis_ps.event_mission_rewards import UNSUPPORTED, EventMissionCatalog, Family
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.protocol import ProtocolRegistry, decode_wire, encode_wire, prepare_request
from aigis_ps.quest_catalog import DropCandidate
from aigis_ps.quest_economy import GAME_PARAM_FIELDS, MONEY_UP_RATE, GoldAbilities, boosted_experience
from aigis_ps.quest_mode_challenge import POLICY as CHALLENGE_POLICY
from aigis_ps.quest_mode_goddess import GODDESS_TYPE
from aigis_ps.quest_mode_story import POLICY as STORY_POLICY
from aigis_ps.quest_modes import EPISODE_FLAG, MAX_BOOST, MODE_PROGRESS, PLATFORM_CODES, SLOT_FLAGS, SettleOutcome
from aigis_ps.quest_protocol import AVATAR_INDEX_OFFSETS, MAX_CANDIDATES
from aigis_ps.quest_service import (
    CLEAR_FLAG, JOURNAL_KEY, JOURNAL_SCHEMA_VERSION, LEGACY_JOURNAL_SCHEMA_VERSION, LEGACY_WATERMARK, PROCESSED_KEYS,
    THREE_STAR_FLAG, QuestRules, QuestService, abort, active_identity, journal,
)
from aigis_ps.request_ledger import PROCESSED_LIMIT, REQUEST_ORDER
from aigis_ps.storage import PlayerProfile, Store, serialize
from aigis_ps.subjugation_rules import SUBJUGATION_TYPE


NOW = datetime(2026, 11, 1, 12)
BOOST_OPEN, BOOST_CLOSED = "2026/11/01 12:00:01", "2026/11/01 12:00:00"
REWARD_RULES = (QuestRules(), QuestRules(experience_percent=137, gold_percent=59),
                QuestRules(experience_percent=0, gold_percent=10000))
# 4A1A60 sets RT ATTR bit 0 for an obtained slot and bit 1 for a 7000-7999 prize enemy.
OBTAINED_ATTR, PRIZE_ENEMY_ATTR = 1, 2
# 4A1F40 records at most seven obtained drops per battle.
OBTAINED_LIMIT = 7
BOUQUET, BOUQUET_CODE = "gift_0000_bouquet", 1001
CONDITIONAL_STARS = 3
# Slots 4 and 5 are Treasure columns past the first three, and slot 8 has no Treasure column at all.
WIDE_SLOTS = (1, 4, 5, 8)
STORY_TYPE = 0
GOLDEN = Path(__file__).resolve().parent / "golden" / "p26"
BATTLE_GOLDEN = GOLDEN / "battle.json"
CURRENT_BATTLE_GOLDEN = GOLDEN / "battle-current-client.json"
LEGACY_GOLDEN = GOLDEN / "legacy"
LEGACY_BATTLE_GOLDEN = LEGACY_GOLDEN / BATTLE_GOLDEN.name
GOLDEN_NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
GOLDEN_IDENTITY = ("1", "a" * 32, "b" * 32)
GOLDEN_LOGGER = "aigis_ps"
# Awakened, card 538 adds 20% reward gold and card 316 raises every GoldGet to 130 gold.
GOLD_TEAM = (538, 316)
AWAKENED = 2
EXIT_WINDOW_PASSED = timedelta(seconds=QuestRules().exit_replay_window_seconds + 1)
# Quest 1001 pays 450 gold, 1.5 times that at three stars.
THREE_STAR_GOLD = 675
NATIVE_PLATFORM = 1
# The local client build always sends PlatformCode 9 (A9D020).
LOCAL_CLIENT_PLATFORM = 9
SHORT_CHARISMA = 2
# Settlement charges and the public gold rates are the only recorded differences from the previous build.
BATTLE_SETTLEMENT_CHANGES = frozenset({
    "state.quest_state.history[].charged.action_points", "state.quest_state.history[].charged.charisma",
    "state.quest_state.history[].gold_rates.event_percent", "state.quest_state.history[].gold_rates.money_up",
    "state.quest_state.history[].gold_rates.reward_percent", "state.quest_state.history[].gold_rates.stars",
    "state.quest_state.history[].response.PGP.A1", "state.quest_state.history[].response.REWARD.GOLD",
    "state.tables.game_param[].Money", "state.tables.game_param[].NowCharisma",
    "steps[].response.PGP.A1", "steps[].response.PGP.A4", "steps[].response.REWARD.GOLD",
})
BATTLE_CLIENT_CHANGES = frozenset({
    "state.quest_state.history[].master_snapshot", "state.quest_state.history[].resource_version",
    "state.quest_state.history[].start_key", "state.quest_state.processed_keys[]", "steps[].request",
})


def unclosed_services(args: Namespace):
    # Goldens hold the resource identity served before the always-on shop window closure, which has its own tests.
    original = resource_view.build_shop_window_closure
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(resource_view, "build_shop_window_closure",
                      lambda payload: replace(original(payload), replacements=MappingProxyType({})))
        # Their receipts also keep the window rule digests of that build; replaying those has its own tests.
        patch.setattr(mission_catalog, "WINDOW_RULE", mission_catalog.LEGACY_WINDOW_RULE)
        patch.setattr(event_pass_catalog, "ELIGIBILITY_RULE", event_pass_catalog.LEGACY_ELIGIBILITY_RULE)
        return services(args)


def local_services(build=services):
    project = Path(__file__).resolve().parents[1]
    args = Namespace(asset_dir=project.parent / "aigisr_exe", data_dir=project / "runtime/server",
                     resource_dir=project / "runtime/resources", rules=None)
    if not (args.asset_dir / "Aigis_r.exe").is_file() or not (args.data_dir / "masterdata/active.json").is_file():
        pytest.skip("Verified local client resources and public masters are required")
    result = build(args)
    assert result.quests is not None
    return result


@cache
def unclosed_bundle():
    return local_services(unclosed_services)


@pytest.fixture(scope="module")
def bundle():
    return local_services()


@pytest.fixture(scope="module")
def golden_bundle():
    return unclosed_bundle()


@pytest.fixture
def setup(bundle, tmp_path):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    set_anchor(initial["tables"]["game_param"][0], energy_seconds(NOW))
    store = Store(tmp_path / "save.sqlite3", initial)
    service = QuestService(bundle.quests.catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                           clock=lambda: NOW, randbelow=lambda _: 0)
    return service, store


def request(store, number, columns, *, tick=1000, platform=NATIVE_PLATFORM):
    registry = ProtocolRegistry.load()
    wire = registry.by_registration[number] if isinstance(number, int) else next(p for p in registry.profiles if p.name == number)
    profile = store.profile()
    common = [Column("UI", "S", (profile.player_id,)), Column("UK", "S", (profile.user_key,))]
    if isinstance(number, int):
        common.extend((Column("MK", "S", (profile.method_key,)), Column("CT", "I", (tick,)),
                       Column("PFN", "S", ("dmm-r18",)), Column("PlatformCode", "I", (platform,)),
                       Column("GPVI", "S", ("unkown",)), Column("GPOT", "S", ("unkown",))))
    xml = dumps(Container(children=tuple(common) + tuple(columns)))
    if wire.checksum_required:
        xml = prepare_request(xml, wire.st)
    return registry.decode(wire.route, encode_wire(xml, wire.request_key))


def slot_recorded(flags, slot):
    # 65D900 reads slots 1-5 from their record bits and counts any other slot once the flag word is nonzero.
    return bool(flags & SLOT_FLAGS[slot - 1]) if 1 <= slot <= len(SLOT_FLAGS) else flags != 0


def client_candidates(service, store, qid):
    record = next((row for row in store.profile().state["tables"]["quest_records"] if row["QuestID"] == qid), {})
    flags = record.get("AchieveFlag", 0)
    return [(index, row.slot, int(slot_recorded(flags, row.slot)) | row.special_attr)
            for index, row in enumerate(service.catalog.get(qid).candidates)]


def start_request(service, store, qid=1001, tick=1000, number=81, *, candidates=None, boost=None,
                  platform=NATIVE_PLATFORM):
    if candidates is None:
        candidates = client_candidates(service, store, qid)
    columns = (Column("QI", "I", (qid,)), Column("Rev", "S", (service.catalog.resource_version,)),
               Container("RT", tuple(Column(name, "I", tuple(row[i] for row in candidates))
                                     for i, name in enumerate(("ID", "TYPE", "ATTR")))))
    if boost is not None:
        columns += (Container("Boost", (Column("V", "I", (boost,)),)),)
    return request(store, number, columns, tick=tick, platform=platform)


def finish_request(store, qid=1001, tick=1001, number=83, *, obtained=0, selected=0, stars=3, gold=0, extra=()):
    columns = (Container("RT", (
        Container("LG", (Column("TG", "S", ("finishQuest",)), Column("TT", "S", (f"{qid} {stars} 20",)))),
        Container("DropResult", (Column("DP", "I", (obtained,)), Column("DPLIST", "I", (selected,)), Column("GoldGet", "I", (gold,)))),
    )),) + extra
    return request(store, number, columns, tick=tick)


def exit_request(store, qid=1001, result=-1):
    return request(store, "quest_exit", (Column("TG", "S", ("finishQuest",)), Column("TT", "S", (f"{qid},{result}",))))


def nodes(xml):
    return {child.name: child for child in loads(xml).children}


def save_record(store, qid, *, stars, flags):
    profile = store.profile()
    updated = deepcopy(profile.state)
    updated["tables"]["quest_records"].append({"QuestID": qid, "MaxRank": stars, "AchieveFlag": flags,
                                             "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False})
    store.update_state(updated, profile.revision)


def allow_quest(store, qid):
    save_record(store, qid, stars=3, flags=CLEAR_FLAG)


def assert_error(code, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert error.value.code == code


def assert_refusal(code, message, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert (error.value.code, str(error.value), error.value.status) == (code, message, 409)


def update_config(store, **fields):
    profile = store.profile()
    updated = deepcopy(profile.state)
    updated["tables"]["config"][0].update(fields)
    store.update_state(updated, profile.revision)


def overwrite(store, state):
    # The saved row keeps its revision, so responses cached at that revision can still replay.
    with store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def candidate_rows(quest, attributes):
    return [(index, candidate.slot, attribute)
            for index, (candidate, attribute) in enumerate(zip(quest.candidates, attributes, strict=True))]


def wallet(store) -> dict:
    return store.profile().state["tables"]["game_param"][0]


def wallet_reply(tree) -> dict:
    return dict(zip(GAME_PARAM_FIELDS, tree["PGP"].children[1].values))


def entry_costs(service, qid=1001, runs=1) -> dict:
    quest = service.catalog.get(qid)
    return {"charisma": quest.charisma * runs, "action_points": quest.action_points * runs}


def last_settlement(store) -> dict:
    return store.profile().state["quest_state"]["history"][-1]


def charged_at_start(service, store, qid=1001):
    # Builds before settlement charges took the entry costs at the start and saved no charge marker.
    profile = store.profile()
    state = deepcopy(profile.state)
    del state["quest_state"]["active"]["charge"]
    paid = state["tables"]["game_param"][0]
    costs = entry_costs(service, qid)
    paid.update(NowCharisma=paid["NowCharisma"] - costs["charisma"],
                NowActionPoint=paid["NowActionPoint"] - costs["action_points"])
    return store.update_state(state, profile.revision)


def recruit(bundle, store, card_ids, evolution=0):
    profile = store.profile()
    grants = [{"card_id": card_id, "level": 1, "count": 1, "evolution_count": evolution} for card_id in card_ids]
    store.update_state(bundle.player_state.credit_units(profile.state, grants)[0], profile.revision)


def formation(rows):
    return Container("BL", tuple(Column(wire, "I", tuple(row[index] for row in rows))
                                 for index, wire in enumerate(("A1", "A7", "A8", "AB", "AA"))))


def test_first_main_quest_charges_at_its_clear_then_updates_progression_and_full_wallet(setup):
    service, store = setup
    opening = wallet(store)
    starting = start_request(service, store)
    response = service.start(starting, store)
    started = store.profile()
    assert service.start(starting, store) == response and store.profile() == started
    assert wallet(store)["NowCharisma"] == opening["NowCharisma"]
    assert wallet_reply(nodes(response))["NowCharisma"] == str(opening["NowCharisma"])
    assert started.state["quest_state"]["active"]["charge"] == "settle"
    tree = nodes(response)
    assert tree["REWARD"].children[0].values == tree["REWARD"].children[1].values == (0,)
    assert not tree["PPU"].children and not tree["TREASURE"].children
    finishing = finish_request(store, gold=0x7FFFFFFF)
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    record = completed.state["tables"]["quest_records"][0]
    assert record["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG
    assert record["MaxRank"] == 3
    assert record["FirstDefeatDate"] == business_day(NOW)
    assert completed.state["tables"]["config"][0]["MenuTutorialFlags"] == 0
    assert completed.state["tables"]["game_param"][0]["Money"] == opening["Money"] + THREE_STAR_GOLD
    tree = nodes(response)
    assert tree["REWARD"].children[0].values == (17,)
    assert tree["REWARD"].children[1].values == (THREE_STAR_GOLD,)
    assert tree["PFC"].children[2].values == (1,)
    assert last_settlement(store)["charged"] == entry_costs(service)
    assert not completed.state["quest_state"]["active"]
    assert_error("quest_attempt_closed", lambda: service.start(starting, store))


@pytest.mark.parametrize("refill", [True, False])
def test_a_clear_charges_its_entry_costs_before_the_rank_up_refill(setup, refill):
    service, store = setup
    ranks = service.bootstrap.rank_rows
    opening = wallet(store)
    assert opening["Rank"] == 1 and service.catalog.get(1001).rank_exp >= ranks[0]["NextExp"]
    service.rules = replace(service.rules, refill_on_rank_up=refill)
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store), store))
    charged = opening["NowCharisma"] - entry_costs(service)["charisma"]
    expected = max(charged, ranks[1]["CharismaMax"]) if refill else charged
    assert (wallet(store)["Rank"], wallet(store)["NowCharisma"]) == (2, expected)
    assert wallet_reply(tree)["NowCharisma"] == str(expected)


def test_a_boosted_clear_charges_every_run_once_at_settlement(setup, monkeypatch):
    service, store = setup
    monkeypatch.setattr(STORY_POLICY, "start_boost", lambda context: context.request.boost)
    service.rules = replace(service.rules, refill_on_rank_up=False)
    opening, costs = wallet(store), entry_costs(service, runs=MAX_BOOST)
    service.start(start_request(service, store, boost=MAX_BOOST), store)
    assert wallet(store)["NowCharisma"] == opening["NowCharisma"]
    tree = nodes(service.finish(finish_request(store), store))
    assert wallet(store)["NowCharisma"] == opening["NowCharisma"] - costs["charisma"]
    assert wallet(store)["NowActionPoint"] == opening["NowActionPoint"] - costs["action_points"]
    assert last_settlement(store)["charged"] == costs
    assert tree["REWARD"].children[1].values == (MAX_BOOST * THREE_STAR_GOLD,)


def test_a_clear_short_of_its_frozen_costs_charges_the_balance_to_zero_with_a_warning(setup):
    service, store = setup
    service.rules = replace(service.rules, refill_on_rank_up=False)
    service.start(start_request(service, store), store)
    profile = store.profile()
    spent = deepcopy(profile.state)
    spent["tables"]["game_param"][0]["NowCharisma"] = SHORT_CHARISMA
    store.update_state(spent, profile.revision)
    costs = entry_costs(service)
    with warnings_logged() as warnings:
        tree = nodes(service.finish(finish_request(store), store))
    assert warnings == [f"Quest 1001 settles {costs['charisma'] - SHORT_CHARISMA} charisma and 0 action points short "
                        "of the costs frozen at its start; the balance stops at zero"]
    assert wallet(store)["NowCharisma"] == 0 and wallet_reply(tree)["NowCharisma"] == "0"
    assert last_settlement(store)["charged"] == {"charisma": SHORT_CHARISMA, "action_points": costs["action_points"]}
    assert tree["REWARD"].children[1].values == (THREE_STAR_GOLD,)


def test_attempts_charged_at_their_start_settle_without_a_second_charge(setup):
    service, store = setup
    service.rules = replace(service.rules, refill_on_rank_up=False)
    service.start(start_request(service, store), store)
    paid = charged_at_start(service, store).state["tables"]["game_param"][0]
    tree = nodes(service.finish(finish_request(store), store))
    assert (wallet(store)["NowCharisma"], wallet(store)["NowActionPoint"]) == (paid["NowCharisma"], paid["NowActionPoint"])
    assert last_settlement(store)["charged"] == {"charisma": 0, "action_points": 0}
    assert tree["REWARD"].children[1].values == (THREE_STAR_GOLD,)


@pytest.mark.parametrize("marker", ["start", "", None, 1, True])
def test_unknown_charge_markers_refuse_the_settlement_without_changes(setup, marker):
    service, store = setup
    service.start(start_request(service, store), store)
    profile = store.profile()
    state = deepcopy(profile.state)
    state["quest_state"]["active"]["charge"] = marker
    store.update_state(state, profile.revision)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.finish(finish_request(store), store)
    assert (error.value.code, error.value.status, str(error.value)) == (
        "quest_state_invalid", 503, "Stored battle charge does not match a native start")
    assert store.profile() == before


@pytest.mark.parametrize("minutes", [0, 480, 540])
@pytest.mark.parametrize("hour", [0, 2])
def test_first_clear_takes_the_jst_business_day_and_star_crystals_the_civil_day(setup, minutes, hour):
    service, store = setup
    # Both clocks follow the civil midnight and precede the 04:00 business turn, on either side of a UTC+8 midnight.
    moment = datetime(2026, 11, 1, hour, 5, tzinfo=JST).astimezone(timezone(timedelta(minutes=minutes)))
    service.clock = lambda: moment
    service.start(start_request(service, store), store)
    finishing = finish_request(store)
    response = service.finish(finishing, store)
    completed = store.profile()
    record = completed.state["tables"]["quest_records"][0]
    assert record["FirstDefeatDate"] == business_day(moment) == date(2026, 10, 31).toordinal() - 1
    assert [row["Date"] for row in completed.state["tables"]["crystals"]] == [civil_day(moment)]
    assert nodes(response)["PFC"].children[1].values == (date(2026, 11, 1).toordinal() - 1,)
    service.clock = lambda: moment + timedelta(days=1)
    assert service.finish(finishing, store) == response and store.profile() == completed


def test_repeated_clear_does_not_grant_first_three_star_again(setup):
    service, store = setup
    for tick in (1000, 1002):
        service.start(start_request(service, store, tick=tick), store)
        response = service.finish(finish_request(store, tick=tick + 1), store)
    assert sum(row["Num"] for row in store.profile().state["tables"]["crystals"]) == 1
    assert "PFC" not in nodes(response)


def test_collection_bonus_accumulates_across_runs_and_is_independent_of_stars(setup):
    service, store = setup
    allow_quest(store, 1001)
    first = start_request(service, store, 1002)
    service.start(first, store)
    service.finish(finish_request(store, 1002, obtained=1, selected=7, stars=1), store)
    row = next(row for row in store.profile().state["tables"]["quest_records"] if row["QuestID"] == 1002)
    assert row["AchieveFlag"] & 0x11101 == 0x10000
    assert len(store.profile().state["player_state"]["units"]) == 2
    service.start(start_request(service, store, 1002, tick=1002), store)
    response = service.finish(finish_request(store, 1002, tick=1003, obtained=6, selected=7, stars=1), store)
    saved = store.profile()
    row = next(row for row in saved.state["tables"]["quest_records"] if row["QuestID"] == 1002)
    assert row["AchieveFlag"] & 0x11101 == 0x11101
    assert not row["AchieveFlag"] & THREE_STAR_FLAG and not row["AchieveFlag"] & 0x40000000
    assert len(saved.state["player_state"]["units"]) == 5
    tree = nodes(response)
    assert tree["PPU"].children[0].values == (67, 68, 15)
    assert len(tree["PPU"].children) == 17 and "PFC" not in tree
    assert tree["TreasureInfo"].children[1].values == (67, 68, 15)
    service.start(start_request(service, store, 1002, tick=1004), store)
    response = service.finish(finish_request(store, 1002, tick=1005, obtained=7, selected=7, stars=2), store)
    assert nodes(response)["PPU"].children[0].values == (4, 67, 68)


@pytest.mark.parametrize("result", [-1, 0])
def test_exit_never_charges_rewards_or_clears(setup, result):
    service, store = setup
    opening = wallet(store)
    service.start(start_request(service, store), store)
    before = store.profile()
    exiting = exit_request(store, result=result)
    response = service.exit(exiting, store)
    saved = store.profile()
    assert saved.state["tables"] == before.state["tables"]
    assert saved.state["tables"]["game_param"][0] == opening
    assert saved.state["player_state"] == before.state["player_state"]
    assert saved.state["quest_state"]["active"] is None
    assert service.exit(exiting, store) == response and store.profile() == saved
    assert_error("quest_attempt_missing", lambda: service.finish(finish_request(store), store))


@pytest.mark.parametrize("result", [-1, 0])
def test_exits_of_attempts_charged_at_their_start_refund_nothing(setup, result):
    service, store = setup
    service.start(start_request(service, store), store)
    paid = charged_at_start(service, store)
    service.exit(exit_request(store, result=result), store)
    saved = store.profile()
    assert saved.state["tables"] == paid.state["tables"]
    assert saved.state["quest_state"]["active"] is None
    assert saved.state["quest_state"]["history"][-1]["kind"] == ("failure" if result == 0 else "retreat")


def test_restart_replaces_the_attempt_without_a_charge_and_its_clear_charges_once(setup):
    service, store = setup
    service.rules = replace(service.rules, refill_on_rank_up=False)
    opening = wallet(store)
    first = start_request(service, store)
    service.start(first, store)
    service.start(start_request(service, store, tick=1003), store)
    restarted = store.profile()
    assert restarted.state["tables"]["game_param"][0]["NowCharisma"] == opening["NowCharisma"]
    assert restarted.state["quest_state"]["history"][-1]["kind"] == "superseded"
    assert_error("quest_attempt_closed", lambda: service.start(first, store))
    assert store.profile() == restarted
    service.finish(finish_request(store, tick=1004), store)
    assert wallet(store)["NowCharisma"] == opening["NowCharisma"] - entry_costs(service)["charisma"]
    assert last_settlement(store)["charged"] == entry_costs(service)


def test_short_duplicate_exit_does_not_close_immediately_restarted_attempt(setup):
    service, store = setup
    service.start(start_request(service, store), store)
    exiting = exit_request(store)
    service.exit(exiting, store)
    service.start(start_request(service, store, tick=1002), store)
    started = store.profile()
    service.exit(exiting, store)
    assert store.profile() == started
    service.clock = lambda: NOW + timedelta(seconds=6)
    service.exit(exiting, store)
    assert store.profile().state["quest_state"]["active"] is None


@pytest.mark.parametrize("obtained,selected", [(8, 7), (7, 6), (1, 0), (-1, 7)])
def test_invalid_masks_do_not_mutate_inventory_or_attempt(setup, obtained, selected):
    service, store = setup
    allow_quest(store, 1001)
    service.start(start_request(service, store, 1002), store)
    before = store.profile()
    assert_error("quest_drop_mismatch", lambda: service.finish(finish_request(store, 1002, obtained=obtained, selected=selected), store))
    assert store.profile() == before


def test_zero_drop_rule_and_changed_rules_use_saved_lottery(setup):
    service, store = setup
    allow_quest(store, 1001)
    service.rules = replace(service.rules, drop_basis_points=0, first_three_star_crystals=2)
    response = service.start(start_request(service, store, 1002), store)
    assert nodes(response)["LOTTERY"].children[1].values == (0, 0, 0)
    service.rules = replace(service.rules, drop_basis_points=10000, first_three_star_crystals=50)
    response = service.finish(finish_request(store, 1002), store)
    assert nodes(response)["PFC"].children[2].values == (2,)
    assert len(store.profile().state["player_state"]["units"]) == 1


def test_stale_cached_response_rejected_after_configuration_mutation(setup):
    service, store = setup
    starting = start_request(service, store)
    service.start(starting, store)
    finishing = finish_request(store)
    service.finish(finishing, store)
    saved = store.profile()
    updated = deepcopy(saved.state)
    updated["tables"]["config"][0]["MenuTutorialFlags"] = 1
    store.update_state(updated, saved.revision)
    before = store.profile()
    assert_error("quest_response_stale", lambda: service.finish(finishing, store))
    assert store.profile() == before


@pytest.mark.parametrize("change", ["resources", "removed_quest"])
def test_snapshot_change_refuses_settlement_and_preserves_attempt(setup, monkeypatch, change):
    service, store = setup
    service.start(start_request(service, store), store)
    before = store.profile()
    if change == "resources":
        monkeypatch.setattr(service.catalog, "resource_version", "new-version")
    else:
        monkeypatch.setattr(service.catalog, "snapshot", replace(service.catalog.snapshot, identifier="new-master"))
        monkeypatch.setattr(service.catalog, "get", lambda _: pytest.fail("Removed quests cannot be resolved before the snapshot guard"))
    assert_error("quest_snapshot_changed", lambda: service.finish(finish_request(store), store))
    assert store.profile() == before


def test_locked_quest_and_candidate_mismatch_do_not_charge(setup):
    service, store = setup
    before = store.profile()
    assert_error("quest_locked", lambda: service.start(start_request(service, store, 1002), store))
    assert store.profile() == before
    allow_quest(store, 1001)
    before = store.profile()
    assert_error("quest_candidates_mismatch", lambda: service.start(start_request(service, store, 1002, candidates=[]), store))
    assert store.profile() == before


def test_battle_entry_checks_the_prerequisite_then_the_quest_rank_before_the_mode_rules(setup, monkeypatch):
    service, store = setup
    original, overrides = service.catalog.get, {}
    monkeypatch.setattr(service.catalog, "get", lambda value: overrides.get(value) or original(value))
    overrides[1002] = replace(original(1002), rank=store.profile().state["tables"]["game_param"][0]["Rank"] + 1)
    columns = (Column("QI", "I", (1002,)), Column("Rev", "S", (service.catalog.resource_version,)))
    hard_start = lambda: service.start(request(store, 82, columns), store)
    before = store.profile()
    assert_refusal("quest_locked", "The prerequisite quest has not been cleared", hard_start)
    assert store.profile() == before
    allow_quest(store, 1001)
    before = store.profile()
    assert_refusal("quest_rank_required", "Player rank is below the quest requirement", hard_start)
    overrides.clear()
    assert_refusal("quest_hard_locked", "The four-star challenge requires three stars", hard_start)
    assert store.profile() == before


def test_battle_start_admits_through_the_shared_entry_without_a_mission_selector(setup, monkeypatch):
    service, store = setup
    shared, admitted = quest_modes.admit, []
    def recording(policy, context):
        admitted.append((policy, context, shared(policy, context)))
        return admitted[-1][2]
    monkeypatch.setattr(quest_modes, "admit", recording)
    before = store.profile()
    service.start(start_request(service, store), store)
    [(policy, context, mission)] = admitted
    assert policy is quest_modes.policy(0) and context.selector is None and context.state == before.state
    assert (context.quest, context.request.quest_id, context.rank, context.hard, context.platform_code) == (
        service.catalog.get(1001), 1001, before.state["tables"]["game_param"][0]["Rank"], False, 1)
    assert store.profile().state["quest_state"]["active"]["mission_id"] == mission.mission_id


def test_battle_start_merges_prize_enemy_attributes_with_the_saved_slot_bits(setup, monkeypatch):
    service, store = setup
    allow_quest(store, 1001)
    original = service.catalog.get
    quest = replace(original(1002), candidates=tuple(replace(candidate, special_attr=PRIZE_ENEMY_ATTR)
                                                     for candidate in original(1002).candidates))
    monkeypatch.setattr(service.catalog, "get", lambda value: quest if value == 1002 else original(value))
    assert [candidate.slot for candidate in quest.candidates] == [1, 2, 3]
    unseen = (PRIZE_ENEMY_ATTR,) * 3
    first_saved = (OBTAINED_ATTR | PRIZE_ENEMY_ATTR, PRIZE_ENEMY_ATTR, PRIZE_ENEMY_ATTR)
    before = store.profile()
    assert_error("quest_candidates_mismatch", lambda: service.start(
        start_request(service, store, 1002, candidates=candidate_rows(quest, (0, 0, 0))), store))
    assert store.profile() == before
    service.start(start_request(service, store, 1002, candidates=candidate_rows(quest, unseen)), store)
    save_record(store, 1002, stars=1, flags=CLEAR_FLAG | quest_modes.SLOT_FLAGS[0])
    before = store.profile()
    assert_error("quest_candidates_mismatch", lambda: service.start(
        start_request(service, store, 1002, tick=1002, candidates=candidate_rows(quest, unseen)), store))
    assert store.profile() == before
    service.start(start_request(service, store, 1002, tick=1002, candidates=candidate_rows(quest, first_saved)), store)


def bouquet_quest(service, monkeypatch, candidates):
    original = service.catalog.get
    quest = replace(original(1002), treasures=(BOUQUET_CODE,) * 3 + (0, 0), candidates=tuple(candidates))
    monkeypatch.setattr(service.catalog, "get", lambda value: quest if value == 1002 else original(value))
    return quest


def ordinary_candidates(count):
    return [DropCandidate(index % 3 + 1, index, 0, 2, 0) for index in range(count)]


def test_default_start_candidates_read_every_treasure_slot_bit_and_any_flag_past_them(setup, monkeypatch):
    service, store = setup
    allow_quest(store, 1001)
    bouquet_quest(service, monkeypatch, [DropCandidate(slot, index, 0, 2, 0) for index, slot in enumerate(WIDE_SLOTS)])
    assert [attribute for _, _, attribute in client_candidates(service, store, 1002)] == [0, 0, 0, 0]
    service.start(start_request(service, store, 1002), store)
    save_record(store, 1002, stars=1, flags=CLEAR_FLAG | SLOT_FLAGS[3])
    assert [attribute for _, _, attribute in client_candidates(service, store, 1002)] == [0, 1, 0, 1]
    response = service.start(start_request(service, store, 1002, tick=1002), store)
    assert nodes(response)["LOTTERY"].children[0].values == tuple(range(len(WIDE_SLOTS)))


def test_starts_save_the_record_max_rank_and_results_journal_it_with_their_event_rewards(setup):
    service, store = setup
    service.start(start_request(service, store), store)
    assert store.profile().state["quest_state"]["active"]["candidate_stars"] == 0
    previous = store.profile().state["tables"]["game_param"][0]["EventParam"]
    service.finish(finish_request(store), store)
    settlement = store.profile().state["quest_state"]["history"][-1]
    assert (settlement["candidate_stars"], settlement["drop_bonus"]) == (0, None)
    events = settlement["event_rewards"]
    assert events["vote_tickets"] == {"error": NOT_ATTACHED, "item_id": None, "count": 0, "reason": UNAVAILABLE}
    assert events["event_param"] == {"rewritten": True, "points": 0, "previous": previous, "value": previous,
                                     "changed": False}
    assert (events["items"], events["discarded"]) == ({}, {"vote_tickets": {}})
    service.start(start_request(service, store, tick=1002), store)
    assert store.profile().state["quest_state"]["active"]["candidate_stars"] == 3


def test_status_describes_the_candidate_bonus_and_event_reward_policies(setup):
    service, _ = setup
    status = service.status()
    assert all(isinstance(status[key], str) and status[key] for key in (
        "candidate_policy", "drop_bonus_policy", "event_reward_policy", "event_mission_reward_policy"))
    assert status["event_rewards"] == service.event_rewards.status()
    assert (status["request_order"], status["processed_request_limit"]) == (REQUEST_ORDER, PROCESSED_LIMIT)
    assert isinstance(service.event_missions, EventMissionCatalog) and service.event_missions.quests is service.catalog
    events = status["event_mission_rewards"]
    assert events == service.event_missions.status() and events["ready"] is True and events["refused"] == []


def test_status_reports_the_preflighted_mode_rewards_the_attached_services_cannot_mail(setup, bundle):
    service, _ = setup
    status = service.status()
    assert isinstance(status["mode_reward_delivery_policy"], str) and status["mode_reward_delivery_policy"]
    assert status["mode_reward_delivery"] == {
        SUBJUGATION_TYPE: {"ready": False, "error": "quest_subjugation_unavailable"},
        GODDESS_TYPE: {"ready": False, "error": "quest_present_unavailable"}}
    assert bundle.quests.status()["mode_reward_delivery"] == {
        SUBJUGATION_TYPE: {"ready": True, "undeliverable": []}, GODDESS_TYPE: {"ready": True, "undeliverable": []}}


def test_reward_delivery_reports_skip_disabled_modes_and_refuse_malformed_hook_answers(setup, monkeypatch):
    service, _ = setup
    undeliverable = {"reason": "present_item_unsupported"}
    answers = iter(((undeliverable,), None, [undeliverable]))
    monkeypatch.setattr(STORY_POLICY, "undeliverable_rewards", lambda catalog, services: next(answers))
    reports = [service.status()["mode_reward_delivery"].get(STORY_TYPE) for _ in range(3)]
    assert reports == [{"ready": False, "undeliverable": [undeliverable]}, None,
                       {"ready": False, "error": "quest_mode_hook_invalid"}]
    monkeypatch.setattr(STORY_POLICY, "enabled", False)
    assert STORY_TYPE not in service.status()["mode_reward_delivery"]


def unsettled_rewards(service, monkeypatch, family=Family.EMERGENCY):
    decided = []

    def families(quest_id, mission):
        decided.append((quest_id, mission))
        return frozenset((family,))

    monkeypatch.setattr(service.event_missions, "families", families)
    return decided


def test_battle_starts_refuse_unsettled_event_mission_rewards_after_admission_before_the_energy_check(setup, monkeypatch):
    service, store = setup
    shared, require, admitted, checked = quest_modes.admit, module.require_quest_energy, [], []

    def recording(policy, context):
        admitted.append(shared(policy, context))
        return admitted[-1]

    def checking(state, *args, **costs):
        checked.append(costs)
        return require(state, *args, **costs)

    monkeypatch.setattr(quest_modes, "admit", recording)
    monkeypatch.setattr(module, "require_quest_energy", checking)
    decided = unsettled_rewards(service, monkeypatch)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.start(start_request(service, store), store)
    [mission] = admitted
    assert (error.value.code, error.value.status, str(error.value)) == (
        UNSUPPORTED, 501, f"Quest 1001 shows emergency event rewards in mission {mission.mission_id} that no mode settles")
    assert decided == [(1001, mission)] and checked == [] and store.profile() == before
    monkeypatch.setattr(service.event_missions, "families", lambda quest_id, admitted_mission: frozenset())
    service.start(start_request(service, store), store)
    assert checked == [{**entry_costs(service), "recovery_seconds": service.rules.charisma_recovery_seconds}]
    assert store.profile().state["quest_state"]["active"]["quest_id"] == 1001


def test_exact_start_replays_answer_before_the_event_mission_gate(setup, monkeypatch):
    service, store = setup
    starting = start_request(service, store)
    response = service.start(starting, store)
    started = store.profile()
    decided = unsettled_rewards(service, monkeypatch, Family.AFTER_EVENT)
    assert service.start(starting, store) == response and store.profile() == started and decided == []
    assert_error(UNSUPPORTED, lambda: service.start(start_request(service, store, tick=1002), store))
    assert store.profile() == started and len(decided) == 1


def test_results_refuse_more_obtained_drops_than_one_battle_records(setup, monkeypatch):
    service, store = setup
    allow_quest(store, 1001)
    quest = bouquet_quest(service, monkeypatch, ordinary_candidates(OBTAINED_LIMIT + 1))
    service.start(start_request(service, store, 1002, candidates=candidate_rows(quest, (0,) * len(quest.candidates))), store)
    everything = (1 << len(quest.candidates)) - 1
    assert store.profile().state["quest_state"]["active"]["selected_mask"] == everything
    before = store.profile()
    assert_refusal("quest_drop_mismatch", "Result obtains more drops than one battle records", lambda: service.finish(
        finish_request(store, 1002, obtained=everything, selected=everything), store))
    assert store.profile() == before
    seven = everything >> 1
    tree = nodes(service.finish(finish_request(store, 1002, obtained=seven, selected=everything), store))
    assert dict(zip(tree["TreasureInfo"].children[1].values, tree["TreasureInfo"].children[2].values)) == {
        BOUQUET_CODE: OBTAINED_LIMIT}
    assert tree["POSSESSION"].children[0].values == (BOUQUET,)


def test_the_record_max_rank_decides_whether_a_start_overflows_the_result_mask(setup, monkeypatch):
    service, store = setup
    allow_quest(store, 1001)
    conditional = DropCandidate(1, MAX_CANDIDATES, 0, 2, 0, CONDITIONAL_STARS)
    quest = bouquet_quest(service, monkeypatch, [*ordinary_candidates(MAX_CANDIDATES), conditional])
    rows = [(index, candidate.slot, 0) for index, candidate in enumerate(quest.candidates[:MAX_CANDIDATES])]
    response = service.start(start_request(service, store, 1002, candidates=rows), store)
    assert nodes(response)["LOTTERY"].children[1].values == (1,) * MAX_CANDIDATES
    save_record(store, 1002, stars=CONDITIONAL_STARS, flags=CLEAR_FLAG)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.start(start_request(service, store, 1002, tick=1002, candidates=[]), store)
    assert (error.value.code, error.value.status) == ("quest_candidates_overflow", 501)
    assert store.profile() == before


def test_whole_settlement_rolls_back_when_unit_credit_fails(setup, monkeypatch):
    service, store = setup
    allow_quest(store, 1001)
    service.start(start_request(service, store, 1002), store)
    before = store.profile()
    def fail(*_args, **_kwargs):
        raise ProtocolError("capacity_exhausted", "fixture", 409)
    monkeypatch.setattr(service.players, "credit_units", fail)
    assert_error("capacity_exhausted", lambda: service.finish(finish_request(store, 1002, obtained=7, selected=7), store))
    assert store.profile() == before


def test_damage_display_updates_without_touching_tutorial_flags(setup):
    service, store = setup
    service.start(start_request(service, store), store)
    response = service.finish(finish_request(store, extra=(Container("GC", (Column("F7", "I", (1,)),)),)), store)
    assert store.profile().state["tables"]["config"][0]["DamageDispEnable"] == 1
    assert store.profile().state["tables"]["config"][0]["MenuTutorialFlags"] == 0
    assert "PGC" not in nodes(response)


def test_client_clock_rollback_starts_and_settles_while_processed_requests_stay_refused(setup):
    service, store = setup
    starting, finishing = start_request(service, store, tick=0x7FFFFFFF), finish_request(store, tick=-0x80000000)
    service.start(starting, store)
    response = service.finish(finishing, store)
    assert service.finish(finishing, store) == response
    restarted, settled = start_request(service, store, tick=1000), finish_request(store, tick=1000)
    service.start(restarted, store)
    service.finish(settled, store)
    before = store.profile()
    assert_error("quest_attempt_closed", lambda: service.start(starting, store))
    assert_error("quest_response_stale", lambda: service.finish(finishing, store))
    assert store.profile() == before
    journal = before.state["quest_state"]
    assert journal["schema_version"] == JOURNAL_SCHEMA_VERSION and LEGACY_WATERMARK not in journal
    assert journal[PROCESSED_KEYS] == [QuestService._key(envelope) for envelope in (starting, finishing, restarted, settled)]


def test_requests_evicted_from_the_battle_history_stay_processed(setup, monkeypatch):
    monkeypatch.setattr(module, "HISTORY_LIMIT", 1)
    service, store = setup
    starting, finishing = start_request(service, store), finish_request(store)
    service.start(starting, store)
    service.finish(finishing, store)
    service.start(start_request(service, store, tick=1002), store)
    service.finish(finish_request(store, tick=1003), store)
    before = store.profile()
    assert (len(before.state["quest_state"]["history"]), len(before.state["quest_state"][PROCESSED_KEYS])) == (1, 4)
    assert_refusal("quest_request_stale", "This start request was already processed",
                   lambda: service.start(starting, store))
    assert_refusal("quest_request_stale", "This result was already processed", lambda: service.finish(finishing, store))
    assert store.profile() == before


def test_watermark_journal_upgrades_keeps_its_keys_and_settles_a_result_older_than_its_start(setup):
    service, store = setup
    first, result = start_request(service, store, tick=2000), finish_request(store, tick=2001)
    service.start(first, store)
    service.finish(result, store)
    restarted = start_request(service, store, tick=2002)
    started = service.start(restarted, store)
    state = store.profile().state
    journal = {name: value for name, value in state["quest_state"].items() if name != PROCESSED_KEYS}
    state["quest_state"] = {**journal, "schema_version": LEGACY_JOURNAL_SCHEMA_VERSION, LEGACY_WATERMARK: 2002}
    overwrite(store, state)
    saved = store.profile()
    assert service.start(restarted, store) == started and store.profile() == saved
    assert_error("quest_attempt_closed", lambda: service.start(first, store))
    assert_error("quest_response_stale", lambda: service.finish(result, store))
    late = finish_request(store, tick=1999)
    service.finish(late, store)
    journal = store.profile().state["quest_state"]
    assert journal["schema_version"] == JOURNAL_SCHEMA_VERSION and LEGACY_WATERMARK not in journal
    assert journal[PROCESSED_KEYS] == [QuestService._key(envelope) for envelope in (first, result, restarted, late)]


@pytest.mark.parametrize("journal", [
    {LEGACY_WATERMARK: "2000"}, {PROCESSED_KEYS: []},
    {"schema_version": JOURNAL_SCHEMA_VERSION}, {"schema_version": JOURNAL_SCHEMA_VERSION, PROCESSED_KEYS: [""]},
    {"schema_version": JOURNAL_SCHEMA_VERSION, PROCESSED_KEYS: ["key", "key"]},
    {"schema_version": JOURNAL_SCHEMA_VERSION, PROCESSED_KEYS: [str(index) for index in range(PROCESSED_LIMIT + 1)]},
    {"schema_version": JOURNAL_SCHEMA_VERSION, PROCESSED_KEYS: [], LEGACY_WATERMARK: None},
    {"schema_version": JOURNAL_SCHEMA_VERSION + 1, PROCESSED_KEYS: []},
], ids=["watermark", "legacy_keys", "missing_keys", "empty_key", "repeated_key", "too_many_keys", "both", "future"])
def test_journals_outside_both_request_layouts_are_refused_without_changes(setup, journal):
    service, store = setup
    profile = store.profile()
    state = deepcopy(profile.state)
    state["quest_state"] = {"schema_version": LEGACY_JOURNAL_SCHEMA_VERSION, "next_sequence": 1, "active": None,
                            "history": [], **journal}
    store.update_state(state, profile.revision)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.start(start_request(service, store), store)
    assert (error.value.code, error.value.status) == ("quest_state_invalid", 503)
    assert store.profile() == before


@pytest.mark.parametrize("completed", [False, True])
def test_cached_response_cannot_bypass_changed_catalog(setup, monkeypatch, completed):
    service, store = setup
    starting = start_request(service, store)
    service.start(starting, store)
    finishing = finish_request(store)
    if completed:
        service.finish(finishing, store)
    before = store.profile()
    monkeypatch.setattr(service.catalog, "snapshot", replace(service.catalog.snapshot, identifier="new-master"))
    replay = lambda: service.finish(finishing, store) if completed else service.start(starting, store)
    assert_error("quest_snapshot_changed", replay)
    assert store.profile() == before


def test_native_http_start_finish_survive_reopening_store(setup, bundle, tmp_path):
    service, store = setup
    def application():
        return create_app(tmp_path / "http", handlers=bundle.bootstrap.handlers() | bundle.player_state.handlers() | service.handlers(),
                          initial_state=store.profile().state)
    app = application()
    starting = start_request(service, app.state.store, number=77)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + starting.profile.route, content=encode_wire(starting.xml, starting.profile.request_key))
        assert reply.status_code == 200
        assert "LOTTERY" in nodes(decode_wire(reply.content, starting.profile.response_key))
    app = application()
    finishing = finish_request(app.state.store, number=80)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + finishing.profile.route, content=encode_wire(finishing.xml, finishing.profile.request_key))
        assert reply.status_code == 200
        assert "QR" in nodes(decode_wire(reply.content, finishing.profile.response_key))
    saved = app.state.store.profile()
    assert application().state.store.profile() == saved


def test_concurrent_same_result_commits_rewards_once(setup, monkeypatch):
    service, store = setup
    service.start(start_request(service, store), store)
    finishing = finish_request(store)
    before = store.profile()
    original_update = store.update_state
    barrier, lock, calls = Barrier(2), Lock(), []
    def raced_update(state, revision):
        with lock:
            calls.append(revision)
            wait = len(calls) <= 2
        if wait:
            barrier.wait(timeout=10)
        return original_update(state, revision)
    monkeypatch.setattr(store, "update_state", raced_update)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.finish, finishing, store) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    after = store.profile()
    assert results[0] == results[1]
    assert after.revision == before.revision + 1
    assert after.state["tables"]["game_param"][0]["Money"] == before.state["tables"]["game_param"][0]["Money"] + THREE_STAR_GOLD
    assert after.state["quest_state"]["history"][-1]["charged"] == entry_costs(service)
    assert len(after.state["quest_state"]["history"]) == 1


def test_revision_retry_preserves_unrelated_settings_update(setup, monkeypatch):
    service, store = setup
    service.start(start_request(service, store), store)
    opening = wallet(store)
    original_update = store.update_state
    calls = []
    def concurrent_setting(state, revision):
        if not calls:
            calls.append(revision)
            profile = store.profile()
            changed = deepcopy(profile.state)
            changed["tables"]["config"][0]["MenuTutorialFlags"] = 7
            original_update(changed, profile.revision)
        return original_update(state, revision)
    monkeypatch.setattr(store, "update_state", concurrent_setting)
    service.finish(finish_request(store), store)
    saved = store.profile()
    assert saved.state["tables"]["config"][0]["MenuTutorialFlags"] == 7
    assert saved.state["tables"]["game_param"][0]["Money"] == opening["Money"] + THREE_STAR_GOLD


def money_up_gold(gold, percent):
    # MoneyUp multiplies the exact percentage share and floors once.
    return min(math.floor(Fraction(gold * percent, 100) * MONEY_UP_RATE), INT32_MAX)


@pytest.mark.parametrize("closes,boosted", [(BOOST_OPEN, True), (BOOST_CLOSED, False), ("", False)])
def test_base_rewards_match_the_settlement_formula_for_every_quest_table_reward(setup, closes, boosted):
    service, store = setup
    update_config(store, ExperienceUpCloseDateTime=closes, MoneyUpCloseDateTime=closes)
    catalog, profile, now = service.catalog, store.profile(), instant(NOW)
    template = catalog.get(1001)
    rewards = sorted({(row["RankExp"], row["Gold"]) for row in map(catalog.row, (*catalog.all_ids, *catalog.mode_ids))})
    for rules in REWARD_RULES:
        for rank_exp, gold in rewards:
            quest = replace(template, rank_exp=rank_exp, gold=gold)
            experience, money = rank_exp * rules.experience_percent // 100, gold * rules.gold_percent // 100
            settled = ((boosted_experience(experience), money_up_gold(gold, rules.gold_percent)) if boosted
                       else (experience, money))
            assert service.base_rewards(quest, rules, profile, now) == settled
            assert service.base_rewards(quest, rules, profile, now, experience_up=False) == (experience, settled[1])
            assert service.base_rewards(quest, rules, profile, now, money_up=False) == (settled[0], money)
            assert service.gold_reward(quest, rules, profile, now, money_up=boosted) == settled[1]


@pytest.mark.parametrize("field,boosted", [("ExperienceUpCloseDateTime", (20, 450)), ("MoneyUpCloseDateTime", (17, 675))])
def test_each_campaign_boosts_only_its_own_base_reward(setup, field, boosted):
    service, store = setup
    update_config(store, **{field: BOOST_OPEN})
    assert service.base_rewards(service.catalog.get(1001), QuestRules(), store.profile(), instant(NOW)) == boosted


# The percentage applies first; 4000003 rounds up and 16777221 drops its low bit in float32, unlike an exact 1.2 product.
@pytest.mark.parametrize("rank_exp,percent,experience", [
    (17, 100, 20), (17, 50, 9), (5, 100, 6), (4000003, 100, 4800004), (16777221, 100, 20132664),
    (0x7FFFFFFF, 100, 0x7FFFFFFF),
])
def test_base_rewards_boost_the_local_percentage_with_the_native_float_product(setup, rank_exp, percent, experience):
    service, store = setup
    update_config(store, ExperienceUpCloseDateTime=BOOST_OPEN)
    quest = replace(service.catalog.get(1001), rank_exp=rank_exp)
    rules = QuestRules(experience_percent=percent)
    assert service.base_rewards(quest, rules, store.profile(), instant(NOW)) == (experience, quest.gold)


# Exact multiply, one floor: 450 * 3% keeps its half; 11184813 and 16777217 keep bits a float32 product would drop.
@pytest.mark.parametrize("gold,percent,money", [
    (450, 100, 675), (450, 3, 20), (3, 100, 4), (11184813, 100, 16777219), (16777217, 100, 25165825),
    (0x7FFFFFFF, 100, 0x7FFFFFFF),
])
def test_base_rewards_raise_the_local_gold_percentage_by_the_exact_money_up_rate(setup, gold, percent, money):
    service, store = setup
    update_config(store, MoneyUpCloseDateTime=BOOST_OPEN)
    quest = replace(service.catalog.get(1001), gold=gold)
    rules = QuestRules(gold_percent=percent)
    assert service.base_rewards(quest, rules, store.profile(), instant(NOW)) == (quest.rank_exp, money)


@pytest.mark.parametrize("field", ["ExperienceUpCloseDateTime", "MoneyUpCloseDateTime"])
@pytest.mark.parametrize("closes", ["", BOOST_OPEN])
@pytest.mark.parametrize("campaigns", [{}, {"experience_up": False}, {"money_up": False}])
def test_base_rewards_refuse_a_naive_clock_whatever_the_campaigns(setup, field, closes, campaigns):
    service, store = setup
    update_config(store, **{field: closes})
    with pytest.raises(ProtocolError) as error:
        service.base_rewards(service.catalog.get(1001), QuestRules(), store.profile(), NOW, **campaigns)
    assert (error.value.code, error.value.status) == ("quest_clock_invalid", 503)


@pytest.mark.parametrize("item_ids", [("item_unknown_to_the_item_table",), (BOUQUET, ""), (BOUQUET, None), (7,),
                                      ([BOUQUET],)])
def test_settle_hooks_naming_unknown_items_are_refused_without_changes(setup, monkeypatch, item_ids):
    service, store = setup
    monkeypatch.setattr(quest_modes.policy(0), "settle", lambda context: SettleOutcome(context.state, item_ids=item_ids))
    service.start(start_request(service, store), store)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.finish(finish_request(store), store)
    assert (error.value.code, error.value.status) == ("quest_mode_hook_invalid", 503)
    assert store.profile() == before


def test_settle_hooks_may_list_a_known_item_they_did_not_credit(setup, monkeypatch):
    service, store = setup
    monkeypatch.setattr(quest_modes.policy(0), "settle", lambda context: SettleOutcome(context.state, item_ids=(BOUQUET,)))
    service.start(start_request(service, store), store)
    columns = nodes(service.finish(finish_request(store), store))["POSSESSION"].children
    assert [(column.name, column.values) for column in columns] == [
        ("ItemID", (BOUQUET,)), ("Count", (0,)), ("Enabled", (0,))]


def settle_progress(monkeypatch, progress):
    def settle(context):
        state = deepcopy(context.state)
        state["quest_state"][MODE_PROGRESS] = progress
        return SettleOutcome(state)

    monkeypatch.setattr(STORY_POLICY, "settle", settle)


def test_settle_hooks_rewrite_only_their_own_progress_and_keep_the_other_modes_progress(setup, monkeypatch):
    service, store = setup
    story, other = STORY_POLICY.name, CHALLENGE_POLICY.name
    service.start(start_request(service, store), store)
    profile = store.profile()
    state = deepcopy(profile.state)
    state["quest_state"][MODE_PROGRESS] = {other: {"saved": 1}}
    store.update_state(state, profile.revision)
    settle_progress(monkeypatch, {story: {"saved": 2}, other: {"saved": 3}})
    service.finish(finish_request(store), store)
    assert store.profile().state["quest_state"][MODE_PROGRESS] == {story: {"saved": 2}, other: {"saved": 1}}


@pytest.mark.parametrize("progress", [[{}], {"unregistered": {}}, {STORY_POLICY.name: [2]}],
                         ids=["list", "unregistered", "entry"])
def test_settle_hook_progress_outside_the_journal_shape_refuses_without_changes(setup, monkeypatch, progress):
    service, store = setup
    settle_progress(monkeypatch, progress)
    service.start(start_request(service, store), store)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.finish(finish_request(store), store)
    assert (error.value.code, error.value.status) == ("quest_mode_hook_invalid", 503)
    assert store.profile() == before


def test_settlement_credits_base_rewards_under_the_rules_frozen_at_start(setup):
    service, store = setup
    service.rules = replace(service.rules, experience_percent=200, gold_percent=50)
    service.start(start_request(service, store), store)
    service.rules = QuestRules()
    reward = nodes(service.finish(finish_request(store), store))["REWARD"].children
    # 450 gold at 50% is 225, and three stars pay 1.5 times that, flooring 337.5 once.
    assert (reward[0].values, reward[1].values) == ((34,), (337,))


@pytest.mark.parametrize("change", [{"drop_basis_points": -1}, {"drop_basis_points": 10001},
    {"gold_percent": True}, {"first_three_star_crystals": 101}, {"charisma_recovery_seconds": 0},
    {"refill_on_rank_up": 1}, {"exit_replay_window_seconds": 61}])
def test_local_rules_validate_boundaries(change):
    with pytest.raises(ValueError):
        QuestRules(**change)


def test_starts_count_each_native_platform_code_including_exact_replays_and_later_refusals(setup):
    service, store = setup
    starting = start_request(service, store)
    service.start(starting, store)
    service.start(starting, store)
    locked = start_request(service, store, 1002, tick=1002, platform=LOCAL_CLIENT_PLATFORM)
    assert_error("quest_locked", lambda: service.start(locked, store))
    assert service.status()["platform_codes_seen"] == {
        "counts": {NATIVE_PLATFORM: 2, LOCAL_CLIENT_PLATFORM: 1}, "last": LOCAL_CLIENT_PLATFORM}
    rebuilt = QuestService(service.catalog, service.bootstrap, service.players, service.possessions, clock=lambda: NOW)
    assert rebuilt.status()["platform_codes_seen"] == {"counts": {}, "last": None}


@pytest.mark.parametrize("code", [len(PLATFORM_CODES), -1, INT32_MAX])
def test_starts_refuse_platform_codes_outside_the_native_range_before_counting_them(setup, code):
    service, store = setup
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.start(start_request(service, store, platform=code), store)
    assert (error.value.code, error.value.status) == ("quest_platform_invalid", 400)
    assert store.profile() == before
    assert service.status()["platform_codes_seen"] == {"counts": {}, "last": None}


def test_starts_leave_the_episode_bit_alone_and_results_keep_it_without_ever_adding_it(setup):
    service, store = setup
    before = store.profile()
    service.start(start_request(service, store), store)
    assert store.profile().state["tables"]["quest_records"] == before.state["tables"]["quest_records"]
    service.finish(finish_request(store), store)
    [record] = store.profile().state["tables"]["quest_records"]
    assert record["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG
    profile = store.profile()
    peruse = deepcopy(profile.state)
    peruse["tables"]["quest_records"][0]["AchieveFlag"] |= EPISODE_FLAG
    store.update_state(peruse, profile.revision)
    service.start(start_request(service, store, tick=1002), store)
    assert store.profile().state["tables"]["quest_records"] == peruse["tables"]["quest_records"]
    tree = nodes(service.finish(finish_request(store, tick=1003, stars=1), store))
    [record] = store.profile().state["tables"]["quest_records"]
    assert record["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | EPISODE_FLAG
    assert tree["QR"].children == tuple(service.bootstrap.columns("quest_records", [record]))


def test_the_battle_kernel_reads_closes_and_identifies_attempts_without_touching_the_save(setup):
    service, store = setup
    assert JOURNAL_KEY == "quest_state"
    fresh = store.profile().state
    assert journal(fresh) == {"schema_version": JOURNAL_SCHEMA_VERSION, "next_sequence": 1, PROCESSED_KEYS: [],
                              "active": None, "history": []}
    assert active_identity(fresh) is None
    assert_refusal("quest_attempt_missing", "No active attempt to abort", lambda: abort(fresh, NOW))
    starting = start_request(service, store)
    service.start(starting, store)
    profile = store.profile()
    saved, key = deepcopy(profile.state), QuestService._key(starting)
    active = saved[JOURNAL_KEY]["active"]
    assert journal(profile.state) == saved[JOURNAL_KEY] and journal(profile.state) is not profile.state[JOURNAL_KEY]
    assert active_identity(profile.state) == (service.catalog.snapshot.identifier, service.catalog.resource_version, key)
    aborted = abort(profile.state, NOW)
    assert profile.state == saved
    assert {name: value for name, value in aborted.items() if name != JOURNAL_KEY} == {
        name: value for name, value in saved.items() if name != JOURNAL_KEY}
    closed = aborted[JOURNAL_KEY]
    assert closed["active"] is None and closed[PROCESSED_KEYS] == saved[JOURNAL_KEY][PROCESSED_KEYS]
    assert closed["history"] == [*saved[JOURNAL_KEY]["history"], {
        "kind": "aborted", "quest_id": 1001, "start_key": key, "sequence": active["sequence"],
        "completed_at": instant(NOW).timestamp()}]
    assert active_identity(aborted) is None
    store.update_state(aborted, profile.revision)
    assert_error("quest_attempt_closed", lambda: service.start(starting, store))
    assert_error("quest_attempt_missing", lambda: service.finish(finish_request(store), store))


def test_the_battle_kernel_reads_a_watermark_journal_in_the_processed_key_layout(setup):
    service, store = setup
    starting = start_request(service, store, tick=2000)
    service.start(starting, store)
    state = store.profile().state
    legacy = {name: value for name, value in state[JOURNAL_KEY].items() if name != PROCESSED_KEYS}
    state[JOURNAL_KEY] = {**legacy, "schema_version": LEGACY_JOURNAL_SCHEMA_VERSION, LEGACY_WATERMARK: 2000}
    saved = deepcopy(state)
    current = journal(state)
    assert state == saved
    assert (current["schema_version"], current[PROCESSED_KEYS]) == (JOURNAL_SCHEMA_VERSION, [QuestService._key(starting)])
    assert LEGACY_WATERMARK not in current
    assert active_identity(state)[2] == QuestService._key(starting)


@pytest.mark.parametrize("evolution,percents,money", [(0, (18, 0), 1150), (AWAKENED, (20, 30), 1278)])
def test_sortie_units_pay_the_gold_abilities_their_class_stage_selects(setup, bundle, evolution, percents, money):
    service, store = setup
    recruit(bundle, store, GOLD_TEAM, evolution)
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store, gold=2, extra=(gold_record(2), sortie(store, GOLD_TEAM))), store))
    # (450 + 2 GoldGet at 100 gold raised by the event percent) * 1.5 at three stars * (100 + reward percent) / 100.
    assert tree["REWARD"].children[1].values == (money,)
    assert last_settlement(store)["gold_rates"] == {"stars": 3, "reward_percent": percents[0],
                                                    "event_percent": percents[1], "money_up": False}


def test_stand_ins_visitors_and_mismatched_rows_never_lend_their_gold_abilities(setup, bundle):
    service, store = setup
    recruit(bundle, store, GOLD_TEAM, AWAKENED)
    roster = store.profile().state["player_state"]["units"]
    prince, gold = roster[0], next(unit for unit in roster if unit["CardID"] == GOLD_TEAM[0])
    stand_in = store.profile().state["player_state"]["battle"]["UX"] + AVATAR_INDEX_OFFSETS.start
    rows = [(GOLD_TEAM[0], stand_in, 0, prince["UniqueID"], 0), (GOLD_TEAM[1], 900, 1, 900, 0),
            (GOLD_TEAM[1], gold["GetIndex"], 2, gold["UniqueID"], 0)]
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store, gold=2, extra=(gold_record(2), formation(rows))), store))
    assert tree["REWARD"].children[1].values == (975,)
    assert last_settlement(store)["gold_rates"] == {"stars": 3, "reward_percent": 0, "event_percent": 0,
                                                    "money_up": False}


def test_gold_abilities_follow_the_image_card_an_alias_names(setup, bundle):
    service, store = setup
    recruit(bundle, store, GOLD_TEAM[1:], AWAKENED)
    images = {GOLD_TEAM[1]: GOLD_TEAM[0]}
    service.image_aliases = SimpleNamespace(
        resolve=lambda card_id: SimpleNamespace(image_card_id=images.get(card_id, card_id)))
    assert service.status()["gold_abilities"]["image_aliases"] is True
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store, gold=2, extra=(gold_record(2), sortie(store, GOLD_TEAM))), store))
    # Card 316 takes the awakened ability of card 538: (450 + 200) * 1.5 * 1.2.
    assert tree["REWARD"].children[1].values == (1170,)
    assert last_settlement(store)["gold_rates"]["reward_percent"] == 20


def test_a_sortie_unit_whose_gold_ability_needs_an_unverified_rule_refuses_the_settlement(setup, bundle, monkeypatch):
    service, store = setup
    recruit(bundle, store, GOLD_TEAM[:1], AWAKENED)
    condition = {"_Command": "", "_ActivateCommand": "", "ExtendProperty": "", "NoChangeCondition": ""}
    conditional = {"_ConfigID": 1, "_InvokeType": 1, "_TargetType": 1, "_InfluenceType": 78,
                   "_Param1": 20, "_Param2": 1, "_Param3": 0, "_Param4": 0, **condition}
    cards = [{"CardID": card_id, "Rare": 5, "Ability_Default": 1, "Ability": 1} for card_id in (1, GOLD_TEAM[0])]
    abilities = GoldAbilities(cards, [{"_ConfigID": 0}, {"_ConfigID": 1}], [conditional])
    monkeypatch.setattr(service, "gold_abilities", lambda: abilities)
    service.start(start_request(service, store), store)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.finish(finish_request(store, extra=(sortie(store, GOLD_TEAM),)), store)
    assert (error.value.code, error.value.status) == ("quest_gold_ability_unsupported", 501)
    assert store.profile() == before


def test_status_describes_charges_gold_rates_abilities_and_platform_codes(setup):
    service, _ = setup
    status = service.status()
    assert all(isinstance(status[key], str) and status[key] for key in (
        "restart_policy", "exit_policy", "charge_policy", "boost_policy", "gold_event_policy", "money_policy",
        "gold_ability_policy", "platform_policy"))
    assert status["gold_abilities"] == {"ready": True, **service.gold_abilities().status(), "image_aliases": False}
    assert status["gold_abilities"]["cards"] > 0
    assert status["gold_abilities"]["unsupported_cards"] == status["gold_abilities"]["unmodeled_awakening_cards"] == []
    assert status["platform_codes_seen"] == {"counts": {}, "last": None}


class GoldenStore:
    # A fixed identity keeps the SYSTEM ExchangeKey and every request digest stable between recordings.
    def __init__(self, state: dict, revision: int = 0):
        self._state, self._revision = json.loads(serialize(state)), revision

    def profile(self) -> PlayerProfile:
        return PlayerProfile(*GOLDEN_IDENTITY, deepcopy(self._state), self._revision)

    def update_state(self, state: dict, expected_revision: int) -> PlayerProfile:
        if expected_revision != self._revision:
            raise ValueError("Profile revision changed; reload before updating")
        self._state, self._revision = json.loads(serialize(state)), self._revision + 1
        return self.profile()

    def overwrite(self, state: dict) -> None:
        self._state = json.loads(serialize(state))


class WarningLog(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextmanager
def warnings_logged():
    logger, handler = logging.getLogger(GOLDEN_LOGGER), WarningLog()
    level = logger.level
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


@dataclass(frozen=True)
class GoldenStep:
    handler: int | str
    envelope: Callable
    later: timedelta = timedelta()
    prepare: Callable[[dict], None] | None = None
    rewrite: Callable[[dict], None] | None = None


@dataclass(frozen=True)
class GoldenCase:
    steps: tuple[GoldenStep, ...]
    stored: Callable[[object, dict], dict] | None = None


def golden_base(bundle) -> dict:
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: GOLDEN_NOW
    state = bundle.player_state.initialize(bootstrap.initial_state())
    set_anchor(state["tables"]["game_param"][0], energy_seconds(GOLDEN_NOW))
    return json.loads(serialize(state))


def changed_sections(before: dict, after: dict) -> dict:
    sections = {name: value for name, value in after.items()
                if name != "tables" and (name not in before or before[name] != value)}
    tables = {name: rows for name, rows in after["tables"].items() if before["tables"].get(name) != rows}
    return {"sections": sections, "tables": tables, "removed": sorted(set(before) - set(after))}


def golden_steps(handlers, store, clock, steps, envelope_of) -> list[dict]:
    outcomes = []
    for step in steps:
        clock.now = GOLDEN_NOW + step.later
        if step.prepare is not None:
            profile = store.profile()
            step.prepare(profile.state)
            store.update_state(profile.state, profile.revision)
        if step.rewrite is not None:
            changed = store.profile().state
            step.rewrite(changed)
            store.overwrite(changed)
        before, envelope = store.profile(), envelope_of(step)
        with warnings_logged() as warnings:
            try:
                result = {"response": handlers[step.handler](envelope, store).decode("utf-8")}
            except ProtocolError as error:
                result = {"error": {"code": error.code, "message": str(error), "status": error.status}}
        outcomes.append({**result, "request": envelope.xml.decode("utf-8"),
                         "revisions": store.profile().revision - before.revision, "warnings": list(warnings)})
    return outcomes


@cache
def recorded(path: Path) -> dict:
    return json.loads(path.read_bytes())


def canonical(value: object) -> str:
    # Plain equality treats True as 1, so recorded values are compared through their JSON text.
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def response_values(xml: str, path: str):
    for container in loads(xml.encode("utf-8")).children:
        columns = {column.name: column.values for column in container.children if isinstance(column, Column)}
        if container.name == "PGP":
            yield from ((f"{path}.PGP.{key}", value) for key, value in zip(columns["A1"], columns["A2"]))
        else:
            yield from ((f"{path}.{container.name}.{name}", list(values)) for name, values in columns.items())


def golden_values(value, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{path}.{key}" if path else key
            yield from (response_values(item, name) if key == "response" and isinstance(item, str)
                        else golden_values(item, name))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from golden_values(item, f"{path}[{index}]")
    else:
        yield path, value


def settled_state(bundle, base: dict, case: GoldenCase, outcome: dict) -> dict:
    state = deepcopy(base) if case.stored is None else case.stored(bundle, deepcopy(base))
    changed = deepcopy(outcome["state"])
    for name in changed["removed"]:
        del state[name]
    state.update(changed["sections"])
    state["tables"] = {**state["tables"], **changed["tables"]}
    return state


def recorded_differences(bundle, cases: dict, current: dict, legacy: dict) -> frozenset[str]:
    paths = set()
    for name, case in cases.items():
        before, after = ({key: value for key, value in golden_values({
            "steps": golden["cases"][name]["steps"],
            "state": settled_state(bundle, golden["base"], case, golden["cases"][name])})}
            for golden in (legacy, current))
        paths.update(re.sub(r"\[\d+\]", "[]", key) for key in before.keys() | after.keys()
                     if key not in before or key not in after or before[key] != after[key])
    return frozenset(paths)


def gold_record(count):
    return Container("GG", (Column("I", "I", (count,)),))


def damage_setting(value):
    return Container("GC", (Column("F7", "I", (value,)),))


def sortie(store, card_ids):
    units = [unit for unit in store.profile().state["player_state"]["units"] if unit["CardID"] in card_ids]
    names = {"A1": "CardID", "A7": "GetIndex", "AB": "UniqueID", "AA": "CostModMixValue"}
    columns = {wire: tuple(unit[name] for unit in units) for wire, name in names.items()}
    columns["A8"] = tuple(range(len(units)))
    return Container("BL", tuple(Column(wire, "I", columns[wire]) for wire in ("A1", "A7", "A8", "AB", "AA")))


def starting(qid=1001, tick=1000, number=81, **options):
    return GoldenStep(number, lambda service, store: start_request(service, store, qid, tick, number, **options))


def finishing(qid=1001, tick=1001, number=83, *, team=(), extra=(), **options):
    def envelope(service, store):
        rows = (sortie(store, team),) if team else ()
        return finish_request(store, qid, tick, number, extra=extra + rows, **options)
    return GoldenStep(number, envelope)


def exiting(qid=1001, result=-1):
    return GoldenStep("quest_exit", lambda service, store: exit_request(store, qid, result))


def cleared_first(bundle, state):
    state["tables"]["quest_records"].append({"QuestID": 1001, "MaxRank": 3, "AchieveFlag": CLEAR_FLAG | THREE_STAR_FLAG,
                                             "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False})
    return state


def money_campaign(bundle, state):
    state["tables"]["config"][0]["MoneyUpCloseDateTime"] = BOOST_OPEN
    return state


def four_star_ready(bundle, state):
    quest = bundle.quests.catalog.get(1002)
    slots = sum(bit for code, bit in zip(quest.treasures, SLOT_FLAGS) if code)
    state["tables"]["quest_records"] = [
        {"QuestID": 1001, "MaxRank": 3, "AchieveFlag": CLEAR_FLAG | THREE_STAR_FLAG, "RemainAssault": 0,
         "FirstDefeatDate": 0, "Shown": False},
        {"QuestID": 1002, "MaxRank": 3, "AchieveFlag": CLEAR_FLAG | THREE_STAR_FLAG | slots, "RemainAssault": 0,
         "FirstDefeatDate": 0, "Shown": False}]
    rank = bundle.quests.catalog.mode(1002).hard.rank
    state["tables"]["game_param"][0].update(
        Rank=rank, Experience=sum(row["NextExp"] for row in bundle.bootstrap.rank_rows[:rank - 1]))
    return state


def drained(bundle, state):
    state["tables"]["game_param"][0]["NowCharisma"] = 0
    return state


def gold_team(bundle, state):
    grants = [{"card_id": card_id, "level": 1, "count": 1, "evolution_count": AWAKENED} for card_id in GOLD_TEAM]
    return bundle.player_state.credit_units(state, grants)[0]


def tutorial_touched(state):
    state["tables"]["config"][0]["MenuTutorialFlags"] = 1


BATTLE_CASES = {
    "main_clear": GoldenCase((starting(), starting(), finishing(gold=0x7FFFFFFF), finishing(gold=0x7FFFFFFF), starting(),
                              replace(finishing(gold=0x7FFFFFFF), prepare=tutorial_touched))),
    "paired_profiles": GoldenCase((starting(number=77), finishing(number=80, stars=2))),
    "one_star_gold_events": GoldenCase((starting(), finishing(stars=1, gold=2, extra=(gold_record(2),)))),
    "four_star": GoldenCase((starting(1002, number=82, candidates=[]), finishing(1002, stars=4)), four_star_ready),
    "exits": GoldenCase((starting(), exiting(), exiting(), finishing(), starting(tick=1002), exiting(result=0))),
    "restart": GoldenCase((starting(), starting(tick=1003), starting(), finishing(tick=1004))),
    "exit_window": GoldenCase((starting(), exiting(), starting(tick=1002), exiting(),
                               replace(exiting(), later=EXIT_WINDOW_PASSED))),
    "result_refusals": GoldenCase((finishing(), starting(), finishing(number=80), finishing(stars=5),
                                   finishing(extra=(damage_setting(2),)), finishing(extra=(damage_setting(1),)))),
    "drop_refusals": GoldenCase((starting(1002, candidates=[]), starting(1002), finishing(1002, obtained=8, selected=7),
                                 finishing(1002, obtained=7, selected=7)), cleared_first),
    "money_campaign": GoldenCase((starting(), finishing(gold=3, extra=(gold_record(3),))), money_campaign),
    "gold_team": GoldenCase((starting(), finishing(gold=2, extra=(gold_record(2),), team=GOLD_TEAM)), gold_team),
    "drained": GoldenCase((starting(), starting(1002)), drained),
}


def battle_outcome(bundle, base: dict, case: GoldenCase) -> dict:
    start = deepcopy(base)
    if case.stored is not None:
        start = case.stored(bundle, start)
    clock = SimpleNamespace(now=GOLDEN_NOW)
    service = QuestService(bundle.quests.catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                           clock=lambda: clock.now, randbelow=lambda _: 0)
    store = GoldenStore(start)
    steps = golden_steps(service.handlers(), store, clock, case.steps, lambda step: step.envelope(service, store))
    return {"steps": steps, "state": changed_sections(start, store.profile().state)}


def test_battle_golden_covers_every_case_with_successes_and_refusals():
    golden = recorded(CURRENT_BATTLE_GOLDEN)
    assert golden["cases"].keys() == BATTLE_CASES.keys()
    steps = [step for case in golden["cases"].values() for step in case["steps"]]
    assert any("response" in step for step in steps) and any("error" in step for step in steps)
    assert {step["error"]["code"] for step in steps if "error" in step} >= {
        "quest_attempt_closed", "quest_attempt_missing", "quest_result_profile_mismatch", "quest_result_invalid",
        "quest_request_invalid", "quest_drop_mismatch", "quest_candidates_mismatch", "quest_response_stale",
        "quest_energy_insufficient", "quest_locked"}


@pytest.mark.parametrize("name", BATTLE_CASES)
def test_battle_requests_match_the_recorded_golden_bytes(golden_bundle, name):
    golden = recorded(CURRENT_BATTLE_GOLDEN)
    result = battle_outcome(golden_bundle, golden["base"], BATTLE_CASES[name])
    assert canonical(result) == canonical(golden["cases"][name])


def test_the_battle_golden_differs_from_the_previous_build_only_in_charges_and_gold(golden_bundle):
    differences = recorded_differences(golden_bundle, BATTLE_CASES, recorded(BATTLE_GOLDEN),
                                       recorded(LEGACY_BATTLE_GOLDEN))
    assert differences == BATTLE_SETTLEMENT_CHANGES


def test_client_upgrade_only_changes_battle_snapshot_and_request_identities(golden_bundle):
    previous, current = recorded(BATTLE_GOLDEN), recorded(CURRENT_BATTLE_GOLDEN)
    assert recorded_differences(golden_bundle, BATTLE_CASES, current, previous) == BATTLE_CLIENT_CHANGES
    for name in BATTLE_CASES:
        assert [step.get("response") for step in current["cases"][name]["steps"]] == [
            step.get("response") for step in previous["cases"][name]["steps"]]


@pytest.mark.parametrize("source", [LEGACY_BATTLE_GOLDEN, BATTLE_GOLDEN], ids=["previous-settlement", "previous-client"])
def test_a_settlement_receipt_of_the_previous_build_replays_its_original_bytes(golden_bundle, source):
    saved = recorded(source)
    legacy, current = (recorded(path)["cases"]["paired_profiles"] for path in (source, CURRENT_BATTLE_GOLDEN))
    case = BATTLE_CASES["paired_profiles"]
    state = settled_state(golden_bundle, saved["base"], case, legacy)
    store = GoldenStore(state, revision=sum(step["revisions"] for step in legacy["steps"]))
    service = QuestService(golden_bundle.quests.catalog, golden_bundle.bootstrap, golden_bundle.player_state,
                           golden_bundle.possessions, clock=lambda: GOLDEN_NOW, randbelow=lambda _: 0)
    finishing = case.steps[-1].envelope(service, store)
    assert finishing.xml.decode("utf-8") == legacy["steps"][-1]["request"]
    assert (legacy["steps"][-1]["response"] != current["steps"][-1]["response"]) == (source == LEGACY_BATTLE_GOLDEN)
    assert_error("quest_snapshot_changed", lambda: service.finish(finishing, store))
    assert store.profile().state == state
    receipt = state[JOURNAL_KEY]["history"][-1]
    catalog = copy(golden_bundle.quests.catalog)
    catalog.snapshot = replace(catalog.snapshot, identifier=receipt["master_snapshot"])
    catalog.resource_version = receipt["resource_version"]
    service = QuestService(catalog, golden_bundle.bootstrap, golden_bundle.player_state,
                           golden_bundle.possessions, clock=lambda: GOLDEN_NOW, randbelow=lambda _: 0)
    assert service.finish(finishing, store).decode("utf-8") == legacy["steps"][-1]["response"]
    assert store.profile().state == state

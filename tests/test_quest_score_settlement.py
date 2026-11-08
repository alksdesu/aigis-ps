from dataclasses import replace
from types import SimpleNamespace

import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import Column, Container
from aigis_ps.bootstrap import TABLES
from aigis_ps.business_calendar import business_day
from aigis_ps.energy_clock import instant
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.quest_catalog import QuestCatalog
from aigis_ps.quest_modes import CLEAR_FLAG, MAX_BOOST, UNBOOSTED, ModePolicy, ScoreResult
from aigis_ps.quest_protocol import MAX_KILLS
from aigis_ps.quest_service import FIRST_STARS, LEGACY_MODE_KEYS, MODE_KEYS, QuestService
from aigis_ps.subjugation_rules import BEST_BITS, BEST_LIMIT
from aigis_ps.vote_quest_tickets import TICKETS_PER_RATE
from test_quest_battle_modes import Recorder, charisma, edit_active, hard_start, record, set_rank, update_game_param
from test_quest_service import NOW, assert_error, bundle, finish_request, nodes, request, setup, start_request
from test_quest_settlement import item_counts, last_settlement, possession, ticket_provider
from test_vote_catalog import vote_sources
from test_vote_quest_tickets import FIRST as TICKET, TODAY


SCORE_TYPE, SCORE_QUEST, SCORE_MISSION, SCORE_RANK = 6, 30019, 400587, 5
MAIN_QUEST = 1001
NORMAL_START, NORMAL_RESULT, HARD_START = 81, 83, 82
KILLS, LATER_KILLS = 20, 7
THREE_STARS, FOUR_STARS = FIRST_STARS["normal"][0], FIRST_STARS["hard"][0]
# The open score quest charges and pays nothing, so these synthetic costs make each boosted run observable.
COSTS = {"charisma": 30, "action_points": 2, "rank_exp": 40, "gold": 70}
ENERGY_STOCK = 1000
BOOST_REFUSAL = f"^Mode boost hooks must return a native boost from {UNBOOSTED} to {MAX_BOOST}$"
BOOST_MISMATCH = r"^Mode boost hooks must apply the requested Boost\.V or leave the start unboosted$"
RECORD_FIELDS = {wire: name for name, _, wire in TABLES["quest_records"].fields}


def packed(total, best):
    return total << BEST_BITS | best


class ScoringPolicy(ModePolicy):
    # Packs the running total above the best run, as the native score MaxRank does.
    name = "synthetic_score"
    quest_types = (SCORE_TYPE,)
    enabled = True
    # SCORE_QUEST shows a score ladder, which starts refuse with 501 unless the mode declares it settled.
    event_reward_families = frozenset({"score"})

    def __init__(self):
        self.boost = UNBOOSTED
        self.boosted, self.scored, self.settled = [], [], []

    def start_boost(self, context):
        self.boosted.append(context)
        return self.boost

    def score_result(self, context):
        self.scored.append(context)
        rank, kills = context.record["MaxRank"], context.request.kills
        return ScoreResult(packed((rank >> BEST_BITS) + kills * context.boost, max(rank & BEST_LIMIT, kills)), kills,
                           {"kills_scored": kills})

    def settle(self, context):
        self.settled.append(context)
        return super().settle(context)


@pytest.fixture(scope="module")
def score_catalog(bundle):
    # A private catalog keeps definitions built under the synthetic policy out of the shared one.
    shared = bundle.quests.catalog
    return QuestCatalog(shared.snapshot, lambda name: bundle.native_cdn.assets.read(name)[0],
                        possessions=bundle.possessions, resource_version=shared.resource_version)


def install(monkeypatch, policy):
    registered = quest_modes.policy
    monkeypatch.setattr(quest_modes, "policy",
                        lambda quest_type: policy if quest_type == SCORE_TYPE else registered(quest_type))
    return policy


@pytest.fixture
def scoring(setup, bundle, score_catalog, monkeypatch):
    _, store = setup
    set_rank(bundle, store, SCORE_RANK)
    service = QuestService(score_catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                           clock=lambda: NOW, randbelow=lambda _: 0)
    service.missions = Recorder()
    return SimpleNamespace(service=service, store=store, policy=install(monkeypatch, ScoringPolicy()))


def priced(scene, monkeypatch):
    original = scene.service.catalog.get
    quest = replace(original(SCORE_QUEST), **COSTS)
    monkeypatch.setattr(scene.service.catalog, "get", lambda value: quest if value == SCORE_QUEST else original(value))
    update_game_param(scene.store, NowCharisma=ENERGY_STOCK, NowActionPoint=ENERGY_STOCK)
    return quest


def score_start(scene, tick=1000, *, boost=None):
    return hard_start(scene.service, scene.store, SCORE_QUEST, tick, NORMAL_START, boost=boost)


def score_finish(store, kills=KILLS, *, reported=None, tick=1001):
    second = kills if reported is None else reported
    columns = (Container("RT", (
        Container("LG", (Column("TG", "S", ("finishQuest",)), Column("TT", "S", (f"{SCORE_QUEST} {second} {kills}",)))),
        Container("DropResult", (Column("DP", "I", (0,)), Column("DPLIST", "I", (0,)), Column("GoldGet", "I", (0,)))),
    )),)
    return request(store, NORMAL_RESULT, columns, tick=tick)


def play(scene, kills=KILLS, tick=1000, *, boost=None):
    scene.service.start(score_start(scene, tick, boost=boost), scene.store)
    return nodes(scene.service.finish(score_finish(scene.store, kills, tick=tick + 1), scene.store))


def active(store):
    return store.profile().state["quest_state"]["active"]


def game_param(store):
    return store.profile().state["tables"]["game_param"][0]


def quest_record(tree):
    return {RECORD_FIELDS[column.name]: column.values[0] for column in tree["QR"].children}


def reward(tree):
    return tuple(column.values[0] for column in tree["REWARD"].children)


def base_rewards(scene, quest, now):
    return scene.service.base_rewards(quest, scene.service.rules, scene.store.profile(), instant(now))


def assert_refused(code, status, scene, callback, *, match=None):
    before = scene.store.profile()
    with pytest.raises(ProtocolError, match=match) as error:
        callback()
    assert (error.value.code, error.value.status) == (code, status)
    assert scene.store.profile() == before


@pytest.mark.parametrize("reported", [KILLS - 1, KILLS + 1, 0])
def test_score_results_must_report_the_kill_count_twice(scoring, reported):
    scoring.service.start(score_start(scoring), scoring.store)
    assert_refused("quest_result_invalid", 400, scoring,
                   lambda: scoring.service.finish(score_finish(scoring.store, reported=reported), scoring.store))
    assert not scoring.policy.scored


@pytest.mark.parametrize("kills", [KILLS, THREE_STARS])
def test_a_score_result_writes_the_hook_max_rank_with_only_the_clear_flag(scoring, kills):
    tree = play(scoring, kills)
    row = record(scoring.store, SCORE_QUEST)
    assert (row["MaxRank"], row["AchieveFlag"], row["FirstDefeatDate"]) == (packed(kills, kills), CLEAR_FLAG,
                                                                            business_day(NOW))
    assert {key: quest_record(tree)[key] for key in ("QuestID", "MaxRank", "AchieveFlag")} == {
        "QuestID": SCORE_QUEST, "MaxRank": packed(kills, kills), "AchieveFlag": CLEAR_FLAG}
    assert "PFC" not in tree and "PPT" not in tree and not tree["TreasureInfo"].children[0].values
    assert scoring.service.missions.events == [("quest_clear", {
        "quest_id": SCORE_QUEST, "quest_type": SCORE_TYPE, "stars": 0, "hard": False, "mission_id": SCORE_MISSION,
        "team": [], "score_mode": True, "kills": kills})]
    settlement = last_settlement(scoring.store)
    assert (settlement["stars"], settlement["kills_reported"], settlement["score"], settlement["boost"],
            settlement["score_details"], settlement["star_rewards"]) == (0, kills, kills, UNBOOSTED,
                                                                         {"kills_scored": kills}, [])
    scored, settled = scoring.policy.scored[0], scoring.policy.settled[0]
    assert (scored.record["MaxRank"], scored.boost, scored.mission_id) == (0, UNBOOSTED, SCORE_MISSION)
    assert (settled.stars, settled.score, settled.boost, settled.first_clear, settled.flags) == (
        0, kills, UNBOOSTED, True, CLEAR_FLAG)
    assert settled.previous_record == scored.record


def test_a_second_run_starts_from_the_packed_max_rank_and_settles(scoring):
    play(scoring)
    first = packed(KILLS, KILLS)
    assert first > FOUR_STARS
    scoring.service.start(score_start(scoring, tick=1002), scoring.store)
    assert active(scoring.store)["candidate_stars"] == first
    tree = nodes(scoring.service.finish(score_finish(scoring.store, LATER_KILLS, tick=1003), scoring.store))
    assert quest_record(tree)["MaxRank"] == record(scoring.store, SCORE_QUEST)["MaxRank"] == packed(
        KILLS + LATER_KILLS, KILLS)
    assert last_settlement(scoring.store)["candidate_stars"] == first
    assert scoring.policy.scored[1].record["MaxRank"] == scoring.policy.settled[1].previous_record["MaxRank"] == first
    assert scoring.policy.settled[1].first_clear is False


def test_a_result_with_no_kills_still_settles_as_a_score(scoring):
    play(scoring)
    tree = play(scoring, 0, tick=1002)
    assert quest_record(tree)["MaxRank"] == packed(KILLS, KILLS)
    assert record(scoring.store, SCORE_QUEST)["AchieveFlag"] == CLEAR_FLAG
    assert (last_settlement(scoring.store)["score"], scoring.service.missions.events[-1][1]["kills"]) == (0, 0)


def test_a_boosted_clear_charges_and_credits_every_run(scoring, monkeypatch, ticket_provider):
    quest = priced(scoring, monkeypatch)
    scoring.policy.boost = MAX_BOOST
    scoring.service.clock = lambda: TODAY
    scoring.service.event_rewards.attach_vote_tickets(ticket_provider)
    before = game_param(scoring.store)
    scoring.service.start(score_start(scoring, boost=MAX_BOOST), scoring.store)
    started = game_param(scoring.store)
    assert (started["NowCharisma"], started["NowActionPoint"]) == (before["NowCharisma"], before["NowActionPoint"])
    assert active(scoring.store)["mode"] == {"quest_type": SCORE_TYPE, "difficulty": "normal", "star_rewards": [],
                                             "boost_ignored": None, "boost": MAX_BOOST}
    experience, gold = base_rewards(scoring, quest, TODAY)
    tree = nodes(scoring.service.finish(score_finish(scoring.store), scoring.store))
    settled = game_param(scoring.store)
    assert (before["NowCharisma"] - settled["NowCharisma"], before["NowActionPoint"] - settled["NowActionPoint"]) == (
        quest.charisma * MAX_BOOST, quest.action_points * MAX_BOOST)
    assert experience and gold and reward(tree) == (experience * MAX_BOOST, gold * MAX_BOOST)
    tickets = quest.charisma // ticket_provider.rate * TICKETS_PER_RATE * MAX_BOOST
    assert possession(scoring.store, TICKET) == item_counts(tree)[TICKET] == tickets
    settlement = last_settlement(scoring.store)
    assert (settlement["boost"], settlement["event_rewards"]["context"]["runs"]) == (MAX_BOOST, MAX_BOOST)
    assert quest_record(tree)["MaxRank"] == packed(KILLS * MAX_BOOST, KILLS)
    assert scoring.policy.scored[0].boost == scoring.policy.settled[0].boost == MAX_BOOST


def test_boosted_starts_need_energy_for_every_run(scoring, monkeypatch):
    quest = priced(scoring, monkeypatch)
    scoring.policy.boost = MAX_BOOST
    update_game_param(scoring.store, NowCharisma=quest.charisma * MAX_BOOST - 1)
    assert_refused("quest_energy_insufficient", 409, scoring,
                   lambda: scoring.service.start(score_start(scoring, boost=MAX_BOOST), scoring.store))
    scoring.policy.boost = UNBOOSTED
    scoring.service.start(score_start(scoring, boost=MAX_BOOST), scoring.store)
    assert charisma(scoring.store) == quest.charisma * MAX_BOOST - 1


@pytest.mark.parametrize("boost", [0, MAX_BOOST + 1, True, 2.0, "2", None])
def test_start_boosts_outside_the_native_range_are_refused_without_charging(scoring, monkeypatch, boost):
    priced(scoring, monkeypatch)
    scoring.policy.boost = boost
    assert_refused("quest_mode_hook_invalid", 503, scoring,
                   lambda: scoring.service.start(score_start(scoring, boost=MAX_BOOST), scoring.store),
                   match=BOOST_REFUSAL)


@pytest.mark.parametrize("applied, requested", [
    (MAX_BOOST, None), (MAX_BOOST, MAX_BOOST - 1), (MAX_BOOST - 1, MAX_BOOST),
])
def test_start_boosts_apply_the_requested_boost_or_none_of_it(scoring, monkeypatch, applied, requested):
    priced(scoring, monkeypatch)
    scoring.policy.boost = applied
    assert_refused("quest_mode_hook_invalid", 503, scoring,
                   lambda: scoring.service.start(score_start(scoring, boost=requested), scoring.store),
                   match=BOOST_MISMATCH)


def test_an_unboosted_score_start_keeps_the_legacy_mode_keys_and_the_ignored_boost(scoring, monkeypatch):
    quest = priced(scoring, monkeypatch)
    before = charisma(scoring.store)
    scoring.service.start(score_start(scoring, boost=MAX_BOOST), scoring.store)
    mode = active(scoring.store)["mode"]
    assert set(mode) == LEGACY_MODE_KEYS and mode["boost_ignored"] == MAX_BOOST
    assert charisma(scoring.store) == before
    tree = nodes(scoring.service.finish(score_finish(scoring.store), scoring.store))
    assert before - charisma(scoring.store) == quest.charisma
    assert reward(tree) == base_rewards(scoring, quest, NOW)
    assert last_settlement(scoring.store)["boost"] == scoring.policy.scored[0].boost == UNBOOSTED


def test_a_saved_boost_key_settles_with_its_runs_and_its_legacy_form_settles_unboosted(scoring, monkeypatch):
    quest = priced(scoring, monkeypatch)
    scoring.service.start(score_start(scoring), scoring.store)
    edit_active(scoring.store, lambda attempt: attempt["mode"].update(boost=UNBOOSTED))
    assert set(active(scoring.store)["mode"]) == MODE_KEYS
    scoring.service.finish(score_finish(scoring.store), scoring.store)
    assert last_settlement(scoring.store)["boost"] == UNBOOSTED
    scoring.policy.boost = MAX_BOOST
    scoring.service.start(score_start(scoring, tick=1002, boost=MAX_BOOST), scoring.store)

    def legacy(attempt):
        del attempt["mode"]["boost"]

    edit_active(scoring.store, legacy)
    tree = nodes(scoring.service.finish(score_finish(scoring.store, tick=1003), scoring.store))
    assert reward(tree) == base_rewards(scoring, quest, NOW)
    assert last_settlement(scoring.store)["boost"] == UNBOOSTED


@pytest.mark.parametrize("change", [
    {"boost": 0}, {"boost": MAX_BOOST + 1}, {"boost": True}, {"boost": "2"}, {"boost": 2.0},
    {"boost": MAX_BOOST, "boost_ignored": MAX_BOOST}, {"boost": MAX_BOOST, "extra": 1},
])
def test_tampered_saved_boosts_are_refused(scoring, change):
    scoring.service.start(score_start(scoring), scoring.store)
    edit_active(scoring.store, lambda attempt: attempt["mode"].update(change))
    assert_refused("quest_state_invalid", 503, scoring,
                   lambda: scoring.service.finish(score_finish(scoring.store), scoring.store))


def test_exact_replays_return_the_cached_start_and_result_without_running_the_hooks_again(scoring):
    starting = score_start(scoring)
    response = scoring.service.start(starting, scoring.store)
    started = scoring.store.profile()
    assert scoring.service.start(starting, scoring.store) == response and scoring.store.profile() == started
    finishing = score_finish(scoring.store)
    response = scoring.service.finish(finishing, scoring.store)
    completed = scoring.store.profile()
    assert scoring.service.finish(finishing, scoring.store) == response and scoring.store.profile() == completed
    assert (len(scoring.policy.boosted), len(scoring.policy.scored), len(scoring.policy.settled)) == (1, 1, 1)


def test_score_results_from_a_four_star_attempt_are_refused(scoring):
    scoring.service.start(score_start(scoring), scoring.store)

    def four_star(attempt):
        attempt["registration"] = HARD_START
        attempt["mode"]["difficulty"] = "hard"

    edit_active(scoring.store, four_star)
    assert_refused("quest_result_invalid", 400, scoring,
                   lambda: scoring.service.finish(score_finish(scoring.store), scoring.store))
    assert not scoring.policy.scored


class Unserializable:
    pass


@pytest.mark.parametrize("result", [
    None, (packed(KILLS, KILLS), KILLS, {}), ScoreResult(-1, KILLS, {}), ScoreResult(INT32_MAX + 1, KILLS, {}),
    ScoreResult(True, KILLS, {}), ScoreResult(KILLS, -1, {}), ScoreResult(KILLS, True, {}), ScoreResult(KILLS, 1.0, {}),
    ScoreResult(KILLS, KILLS, [("kills", KILLS)]), ScoreResult(KILLS, KILLS, {"ratio": float("nan")}),
    ScoreResult(KILLS, KILLS, {"value": Unserializable()}), ScoreResult(KILLS, KILLS, {("kills",): KILLS}),
    ScoreResult(KILLS, KILLS, {KILLS: "number", str(KILLS): "text"}),
])
def test_invalid_score_results_are_refused_without_changes(scoring, monkeypatch, result):
    monkeypatch.setattr(scoring.policy, "score_result", lambda context: result)
    scoring.service.start(score_start(scoring), scoring.store)
    assert_refused("quest_mode_hook_invalid", 503, scoring,
                   lambda: scoring.service.finish(score_finish(scoring.store), scoring.store))


def test_score_hook_refusals_leave_the_attempt_open(scoring, monkeypatch):
    def refuse(context):
        raise ProtocolError("quest_score_unsupported", "Synthetic score refusal", 501)

    monkeypatch.setattr(scoring.policy, "score_result", refuse)
    scoring.service.start(score_start(scoring), scoring.store)
    assert_refused("quest_score_unsupported", 501, scoring,
                   lambda: scoring.service.finish(score_finish(scoring.store), scoring.store))
    assert active(scoring.store)["quest_id"] == SCORE_QUEST


def test_score_modes_without_a_score_hook_cannot_settle(scoring, monkeypatch):
    class UnscoredPolicy(ModePolicy):
        name = "synthetic_unscored"
        quest_types = (SCORE_TYPE,)
        enabled = True
        event_reward_families = frozenset({"score"})

    install(monkeypatch, UnscoredPolicy())
    scoring.service.start(score_start(scoring), scoring.store)
    assert_refused("quest_mode_hook_invalid", 503, scoring,
                   lambda: scoring.service.finish(score_finish(scoring.store), scoring.store))


def test_score_details_are_journaled_as_json_and_hooks_get_their_own_record_copy(scoring, monkeypatch):
    play(scoring)
    first = record(scoring.store, SCORE_QUEST)

    def tampering(context):
        context.record["RemainAssault"] = first["RemainAssault"] + 1
        return ScoreResult(packed(KILLS, KILLS), KILLS, {"tiers": (1, 2), 7: "seven"})

    monkeypatch.setattr(scoring.policy, "score_result", tampering)
    play(scoring, tick=1002)
    assert last_settlement(scoring.store)["score_details"] == {"tiers": [1, 2], "7": "seven"}
    assert record(scoring.store, SCORE_QUEST)["RemainAssault"] == first["RemainAssault"]
    assert scoring.policy.settled[1].previous_record == first


def test_mode_hooks_receive_the_possession_tower_and_subjugation_services(scoring, bundle):
    tower, subjugation = object(), object()
    scoring.service.tower, scoring.service.subjugation = tower, subjugation
    play(scoring)
    for context in (scoring.policy.boosted[0], scoring.policy.scored[0], scoring.policy.settled[0]):
        assert (context.services.possessions, context.services.tower, context.services.subjugation) == (
            bundle.possessions, tower, subjugation)


def test_ordinary_settle_hooks_see_the_reported_stars_without_a_score(setup, monkeypatch):
    service, store = setup
    settled, original = [], quest_modes.policy(0).settle

    def recording(context):
        settled.append(context)
        return original(context)

    monkeypatch.setattr(quest_modes.policy(0), "settle", recording)
    service.start(start_request(service, store), store)
    service.finish(finish_request(store), store)
    context = settled[0]
    assert (context.stars, context.score, context.boost) == (THREE_STARS, None, UNBOOSTED)
    assert {key: context.previous_record[key] for key in ("QuestID", "MaxRank", "AchieveFlag")} == {
        "QuestID": MAIN_QUEST, "MaxRank": 0, "AchieveFlag": 0}
    settlement = last_settlement(store)
    assert (settlement["stars"], settlement["score"], settlement["score_details"], settlement["boost"]) == (
        THREE_STARS, None, None, UNBOOSTED)


@pytest.mark.parametrize("stars", [0, FOUR_STARS + 1, MAX_KILLS])
def test_ordinary_results_refuse_star_counts_outside_the_native_set(setup, stars):
    service, store = setup
    service.start(start_request(service, store), store)
    before = store.profile()
    assert_error("quest_result_invalid", lambda: service.finish(finish_request(store, stars=stars), store))
    assert store.profile() == before

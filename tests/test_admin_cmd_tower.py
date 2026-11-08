from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps import admin_cmd_tower, admin_views_towers
from aigis_ps.admin_cmd_tower import (
    ADJUST_STACK, GATE_SOURCE, NOTHING_OPEN, REDELIVER_DEFERRED, RESET_REWARDS, REWARDS_REPAY, SET_FLOOR_BEST,
    SETTLE_NOW, STILL_DEFERRED, TOWERS,
)
from aigis_ps.admin_command import COMMAND_UNAVAILABLE, AdminError, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.energy_clock import JST
from aigis_ps.quest_modes import CLEAR_FLAG
from aigis_ps.storage import AUTO_BACKUP_KIND
from aigis_ps.tower_catalog import GATE_QUEST, STAR_SOURCE
from aigis_ps.tower_service import FLOOR_INFO
from aigis_ps.tower_state import DOMAIN, read_state, rewards_due, saved_records
from test_admin_cmd_storage import commit, planned, preview_error
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_quest_service import bundle
from test_tower_service import (
    CONSUME, DAILY_REFILL, DAY, FIRST_GENERATION, FIRST_LIMIT, GENERAL, HERO, HERO_FLOOR, HERO_SECOND, POOL_CLOSED,
    TICKET_TIER, held, towers,
)


NOW = datetime(2026, 11, 10, 12, tzinfo=JST)
CLOSING = NOW + DAY
BATTLE = "battle"
UNKNOWN_MISSION, UNKNOWN_FLOOR = 999999, 99999
OPERATIONS = tuple(command.operation for command in admin_cmd_tower.COMMANDS)


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_tower.register(commands)
    return commands


def cleared(state, *quests):
    rows = [(quest, 3) if isinstance(quest, int) else quest for quest in quests]
    quest_ids = {quest_id for quest_id, _ in rows}
    kept = [row for row in state["tables"]["quest_records"] if row["QuestID"] not in quest_ids]
    state["tables"]["quest_records"] = kept + [
        {"QuestID": quest_id, "MaxRank": stars, "AchieveFlag": CLEAR_FLAG, "RemainAssault": 0,
         "FirstDefeatDate": 0, "Shown": False} for quest_id, stars in rows]
    return state


@pytest.fixture
def saves(bundle):
    def settled(*quests):
        # Console commits must survive a restart, so the save starts from the form a restart keeps.
        state = bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))
        state = cleared(deepcopy(state), GATE_QUEST, *quests)
        return bundle.towers.settle(state, NOW)[0]

    return settled


def console(bundle, state, preflight, clock=NOW):
    store = FakeStore(state, 7)
    generation = FakeGeneration(id="gen-towers", bundle=bundle, clock=clock)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def test_commands_register_on_the_battle_domain_with_the_tower_preflight(registry):
    assert sorted(command.operation for command in registry) == sorted(OPERATIONS)
    catalog = discover(PREFLIGHT_DOMAINS)
    assert "admin_cmd_tower" in catalog.modules and "admin_views_towers" in catalog.modules
    assert all(operation in catalog.commands for operation in OPERATIONS)
    for command in admin_cmd_tower.COMMANDS:
        assert (command.domain, command.preflight_none, command.writes) == (BATTLE, {}, frozenset())
        assert command.availability(SimpleNamespace(towers=None)) == missing_service(TOWERS)
        assert command.availability(SimpleNamespace(towers=object())) is None
    assert {command.operation: command.touched for command in admin_cmd_tower.COMMANDS} == {
        "towers.floor_best.set": frozenset({"towers"}), "towers.stack.adjust": frozenset({"towers"}),
        "towers.settle_now": frozenset({"towers"}), "towers.deferred.redeliver": frozenset({"towers", "presents"}),
        "towers.rewards.reset": frozenset({"towers"})}
    assert [command.destructive for command in admin_cmd_tower.COMMANDS] == [False, False, False, False, True]
    assert RESET_REWARDS.phrase(RESET_REWARDS.parse({"mission_id": HERO})) == f"RESET TOWER {HERO}"
    with pytest.raises(ValueError):
        admin_cmd_tower.register(registry)


@pytest.mark.parametrize("operation,params", [
    (SET_FLOOR_BEST.operation, {"quest_id": HERO_FLOOR}),
    (SET_FLOOR_BEST.operation, {"quest_id": HERO_FLOOR, "score": -1}),
    (SET_FLOOR_BEST.operation, {"quest_id": HERO_FLOOR, "score": True}),
    (SET_FLOOR_BEST.operation, {"quest_id": 0, "score": 1}),
    (ADJUST_STACK.operation, {"mission_id": HERO, "stack": "1"}),
    (ADJUST_STACK.operation, {"mission_id": HERO}),
    (SETTLE_NOW.operation, {"mission_id": HERO}),
    (REDELIVER_DEFERRED.operation, {}),
    (RESET_REWARDS.operation, {"mission_id": HERO, "sources": []}),
    (RESET_REWARDS.operation, {"mission_id": HERO, "sources": ["bronze"]}),
    (RESET_REWARDS.operation, {"mission_id": HERO, "sources": "score"}),
])
def test_parameters_are_strict(registry, operation, params):
    with pytest.raises(AdminError) as caught:
        registry.get(operation).parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_floor_bests_commit_with_the_tower_readback(bundle, towers, saves, preflight, registry):
    best = 25000
    store, pipeline = console(bundle, saves(), preflight)
    plan = planned(pipeline, registry, SET_FLOOR_BEST.operation, {"quest_id": HERO_FLOOR, "score": best})
    assert plan.preflight == {"ok": True, "readback": {"towers": [FLOOR_INFO]}}
    assert plan.restart_check == {"ok": True} and plan.confirmations == ()
    reached = [tier.key[1] for tier in towers.tower(HERO).score_rewards[HERO_FLOOR] if tier.threshold <= best]
    [step] = plan.describe()["steps"]
    assert step["effects"] == {"quest_id": HERO_FLOOR, "mission_id": HERO,
                               "floor_best": {"before": 0, "after": best},
                               "total_score": {"before": 0, "after": best},
                               "claimed_score_rewards": {"before": [], "after": reached}}
    commit(pipeline, plan)
    record = read_state(store.state, towers)["towers"][str(HERO)]
    assert record["floors"] == {str(HERO_FLOOR): best} and record["total"] == best
    assert len(store.backups) == 0


def test_stack_adjustments_commit_within_the_generation_limit(bundle, towers, saves, preflight, registry):
    store, pipeline = console(bundle, saves(), preflight)
    plan = planned(pipeline, registry, ADJUST_STACK.operation, {"mission_id": HERO, "stack": FIRST_LIMIT})
    [step] = plan.describe()["steps"]
    assert step["effects"] == {"mission_id": HERO, "stack": {"before": FIRST_GENERATION, "after": FIRST_LIMIT},
                               "generation_limit": FIRST_LIMIT}
    commit(pipeline, plan)
    assert read_state(store.state, towers)["towers"][str(HERO)]["stack"] == FIRST_LIMIT
    preview_error(pipeline, registry, ADJUST_STACK.operation, {"mission_id": HERO, "stack": FIRST_LIMIT + 1},
                  "tower_admin_invalid", 400)


def test_settling_now_adds_one_day_and_reports_the_refills(bundle, towers, saves, preflight, registry):
    store, pipeline = console(bundle, saves(), preflight)
    plan = planned(pipeline, registry, SETTLE_NOW.operation, {})
    [step] = plan.describe()["steps"]
    assert step["warnings"] == [] and step["effects"] == {
        "refilled_items": {CONSUME: {"before": DAILY_REFILL, "after": 2 * DAILY_REFILL}},
        "stacks": {str(HERO): {"before": FIRST_GENERATION, "after": 2 * FIRST_GENERATION}}}
    commit(pipeline, plan)
    assert held(store.state, CONSUME) == 2 * DAILY_REFILL
    assert read_state(store.state, towers)["towers"][str(HERO)]["stack"] == 2 * FIRST_GENERATION


def test_settling_now_warns_when_no_tower_is_open(bundle, preflight, registry):
    state = bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))
    state = cleared(deepcopy(state))
    _, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, SETTLE_NOW.operation, {})
    [step] = plan.describe()["steps"]
    assert step["warnings"] == [NOTHING_OPEN] and step["effects"] == {"refilled_items": {}, "stacks": {}}


def test_settling_now_warns_when_every_saved_tower_has_closed(bundle, towers, saves, preflight, registry,
                                                               monkeypatch):
    state = saves(HERO_FLOOR)
    saved = read_state(state, towers)["towers"]
    assert sorted(saved) == sorted(str(tower.mission_id) for tower in towers.towers)
    windows = bundle.towers.catalog.windows
    monkeypatch.setattr(bundle.towers.catalog, "windows",
                        SimpleNamespace(mission=lambda mission_id: replace(windows.mission(mission_id), end=CLOSING)))
    _, pipeline = console(bundle, state, preflight)
    [step] = planned(pipeline, registry, SETTLE_NOW.operation, {}).describe()["steps"]
    assert step["warnings"] == []
    _, pipeline = console(bundle, state, preflight, clock=CLOSING + DAY)
    plan = planned(pipeline, registry, SETTLE_NOW.operation, {})
    [step] = plan.describe()["steps"]
    assert step["warnings"] == [NOTHING_OPEN] and step["effects"] == {
        "refilled_items": {},
        "stacks": {key: {"before": record["stack"], "after": record["stack"]} for key, record in saved.items()}}
    assert plan.state_sha256_before == plan.state_sha256_after and plan.diff["total"] == 0


def test_redelivery_waits_on_closed_pools_and_mails_once_they_open(bundle, towers, saves, preflight, registry,
                                                                   monkeypatch):
    state = saves(HERO_FLOOR, (HERO_SECOND, 2))
    state, presents, deferred = rewards_due(state, towers, HERO, saved_records(state), bundle.towers.deliverable)
    assert deferred == ({"source": STAR_SOURCE, "key": list(TICKET_TIER), "reason": POOL_CLOSED},)
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, REDELIVER_DEFERRED.operation, {"mission_id": HERO})
    [step] = plan.describe()["steps"]
    assert step["warnings"] == [STILL_DEFERRED]
    assert step["effects"] == {"mission_id": HERO, "mailed_present_ids": [], "deferred": list(deferred)}
    assert sorted(plan.preflight["readback"]) == ["presents", "towers"]
    monkeypatch.setattr(bundle.presents.item_tickets, "validate_item", lambda item_id: None)
    opened = planned(pipeline, registry, REDELIVER_DEFERRED.operation, {"mission_id": HERO})
    [step] = opened.describe()["steps"]
    assert step["warnings"] == [] and step["effects"]["deferred"] == []
    [mailed] = step["effects"]["mailed_present_ids"]
    commit(pipeline, opened)
    assert TICKET_TIER[0] in read_state(store.state, towers)["towers"][str(HERO)]["star_rewards"]
    assert any(row["PresentID"] == mailed for row in store.state["tables"]["presents"])


def test_reward_resets_need_their_phrase_take_a_backup_and_warn_about_repaying(bundle, towers, saves, preflight,
                                                                               registry):
    state = saves((HERO_FLOOR, 2))
    state = bundle.towers.set_floor_best(state, HERO_FLOOR, 10000)
    state, _, _ = rewards_due(state, towers, HERO, saved_records(state), lambda reward: None)
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, RESET_REWARDS.operation, {"mission_id": HERO, "sources": [STAR_SOURCE]})
    assert [item.describe() for item in plan.confirmations] == [
        {"step": 0, "operation": RESET_REWARDS.operation, "phrase": f"RESET TOWER {HERO}"}]
    [step] = plan.describe()["steps"]
    assert step["warnings"] == [REWARDS_REPAY] and step["effects"]["sources"] == [STAR_SOURCE]
    assert step["effects"]["reset"] == [{"source": STAR_SOURCE, "key": [1]}, {"source": STAR_SOURCE, "key": [2]}]
    with pytest.raises(AdminError) as caught:
        commit(pipeline, plan)
    assert caught.value.code == "admin_confirmation_required" and store.calls == []
    commit(pipeline, plan, (f"RESET TOWER {HERO}",))
    [backup] = store.backups
    assert backup["row"]["kind"] == AUTO_BACKUP_KIND
    record = read_state(store.state, towers)["towers"][str(HERO)]
    assert record["star_rewards"] == [] and record["score_rewards"]


def test_refusals_pass_the_tower_codes_through(bundle, saves, preflight, registry):
    store, pipeline = console(bundle, saves(), preflight)
    for operation, params, code, status in (
            (SET_FLOOR_BEST.operation, {"quest_id": UNKNOWN_FLOOR, "score": 1}, "tower_request_invalid", 400),
            (SET_FLOOR_BEST.operation, {"quest_id": HERO_FLOOR, "score": 2 ** 31 - 1}, "tower_admin_invalid", 400),
            (ADJUST_STACK.operation, {"mission_id": UNKNOWN_MISSION, "stack": 0}, "tower_request_invalid", 400),
            (ADJUST_STACK.operation, {"mission_id": GENERAL, "stack": 0}, "tower_not_started", 409),
            (REDELIVER_DEFERRED.operation, {"mission_id": GENERAL}, "tower_not_started", 409),
            (RESET_REWARDS.operation, {"mission_id": GENERAL}, "tower_not_started", 409)):
        preview_error(pipeline, registry, operation, params, code, status)
    assert store.calls == [] and store.backups == []


def test_commands_are_unavailable_without_the_tower_service(saves, preflight, registry):
    store = FakeStore(saves(), 7)
    generation = FakeGeneration(id="gen-no-towers", bundle=SimpleNamespace(towers=None), clock=NOW)
    pipeline = CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                              backup_directory=Path("backups"))
    with pytest.raises(AdminError) as caught:
        pipeline.preview(registry.batch([{"operation": SETTLE_NOW.operation, "params": {}}]))
    assert (caught.value.code, caught.value.status) == (COMMAND_UNAVAILABLE, 503)
    assert caught.value.details == {"step": 0, "operation": SETTLE_NOW.operation}
    assert store.calls == []


def test_commands_refuse_while_a_battle_is_in_progress(bundle, saves, preflight, registry):
    state = saves()
    state["quest_state"] = {"active": {"quest_id": HERO_FLOOR, "sequence": 1, "start_key": "k",
                                       "master_snapshot": "m", "resource_version": "r"}}
    _, pipeline = console(bundle, state, preflight)
    error = preview_error(pipeline, registry, SETTLE_NOW.operation, {}, "admin_battle_abort_required", 409)
    assert error.details["source"] == GATE_SOURCE


def test_the_reservation_of_an_interrupted_start_survives_the_console(bundle, towers, saves, preflight, registry):
    state = saves()
    state[DOMAIN]["charge"] = {"quest_id": HERO_FLOOR}
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, ADJUST_STACK.operation, {"mission_id": HERO, "stack": 0})
    commit(pipeline, plan)
    assert read_state(store.state, towers)["charge"] == {"quest_id": HERO_FLOOR}


def test_the_overview_registers_as_a_live_battle_view_of_status_and_state(bundle, saves):
    [view] = admin_views_towers.VIEWS
    assert (view.name, view.domain, view.live, view.params_model) == ("towers.overview", BATTLE, True, None)
    context = SimpleNamespace(generation=SimpleNamespace(bundle=bundle), state=saves(), now=NOW)
    answered = admin_views_towers.overview_view(context, None)
    assert answered["status"]["ok"] and answered["status"]["status"]["ready"] is True
    assert answered["state"]["ok"] and answered["state"]["started"] is True and answered["state"]["charge"] is None
    assert [entry["mission_id"] for entry in answered["state"]["towers"]] == [HERO, GENERAL, 110003]
    unavailable = SimpleNamespace(generation=SimpleNamespace(bundle=SimpleNamespace(towers=None)),
                                  state=saves(), now=NOW)
    with pytest.raises(AdminError) as caught:
        admin_views_towers.overview_view(unavailable, None)
    assert caught.value.code == "admin_view_service_unavailable"

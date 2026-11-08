from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps import admin_cmd_subjugation, admin_views_subjugation
from aigis_ps.admin_cmd_subjugation import (BATTLE, COMMANDS, LOWERED_REDELIVERY_RISK, NO_CATALOG, NOTHING_OWED,
                                            PROGRESS_RESET, REDELIVERY_RISK, RESET_PHRASE, REWARDS_REDELIVER,
                                            WATERMARK_RESYNC)
from aigis_ps.admin_command import AdminError, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, PRESENTS_READ, QUEST_RECORD_ROUTE, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.energy_clock import JST
from aigis_ps.quest_modes import CLEAR_FLAG, MODE_PROGRESS
from aigis_ps.quest_service import JOURNAL_KEY
from aigis_ps.storage import AUTO_BACKUP_KIND
from aigis_ps.subjugation_rules import PROGRESS_NAME, ScorePair, pack, watermark
from test_admin_cmd_storage import commit, planned, preview_error
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_quest_service import bundle


NOW = datetime(2026, 11, 5, 12, tzinfo=JST)
OPEN_MISSION, OTHER_MISSION = 400587, 400183
MEMBER, LAST_MEMBER, OTHER_MEMBER = 30019, 30023, 6402
START_KEY = "s" * 64


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_subjugation.register(commands)
    return commands


@pytest.fixture
def live_state(bundle):
    return bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))


def console(bundle, state, preflight, *, revision=0):
    store = FakeStore(state, revision)
    generation = FakeGeneration(id="gen-subjugation", bundle=bundle, clock=NOW)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def subjugation_record(quest_id, best, total):
    return {"QuestID": quest_id, "MaxRank": pack(best, total), "AchieveFlag": CLEAR_FLAG, "RemainAssault": 0,
            "FirstDefeatDate": 0, "Shown": False}


def with_records(state, *rows):
    replaced = {row["QuestID"] for row in rows}
    kept = [row for row in state["tables"]["quest_records"] if row["QuestID"] not in replaced]
    return {**state, "tables": {**state["tables"], "quest_records": [*kept, *rows]}}


def journal_with(progress=None, active=None):
    journal = {"schema_version": 2, "next_sequence": 1, "processed_keys": [], "history": [], "active": active}
    if progress is not None:
        journal[MODE_PROGRESS] = {PROGRESS_NAME: progress}
    return journal


def test_commands_register_on_the_battle_domain_with_their_preflight(registry):
    assert [command.operation for command in registry] == [
        "subjugation.progress.reset", "subjugation.rewards.redeliver", "subjugation.watermark.resync"]
    assert all(command.domain == BATTLE for command in COMMANDS)
    catalog = discover(PREFLIGHT_DOMAINS)
    assert "admin_cmd_subjugation" in catalog.modules and "admin_views_subjugation" in catalog.modules
    assert all(command.operation in catalog.commands for command in COMMANDS)
    views = {view.name: view for view in catalog.views}
    assert views["battle.subjugation"].live and views["battle.subjugation_progress"].live
    assert views["battle.subjugation"].domain == views["battle.subjugation_progress"].domain == BATTLE
    assert WATERMARK_RESYNC.touched == PROGRESS_RESET.touched == frozenset({"quest_records", JOURNAL_KEY})
    assert REWARDS_REDELIVER.touched == frozenset({"quest_records", JOURNAL_KEY, "presents"})
    assert all(domain in PREFLIGHT_DOMAINS for command in COMMANDS for domain in command.touched)
    assert all(not command.preflight_none for command in COMMANDS)
    assert PROGRESS_RESET.destructive is True
    assert WATERMARK_RESYNC.destructive is False and REWARDS_REDELIVER.destructive is False
    assert PROGRESS_RESET.phrase(PROGRESS_RESET.parse({"mission_id": OPEN_MISSION})) == RESET_PHRASE.format(
        mission_id=OPEN_MISSION)
    for command in COMMANDS:
        assert command.availability(SimpleNamespace(quests=None)) == missing_service("quests")
        assert command.availability(SimpleNamespace(quests=SimpleNamespace(subjugation=None))) == NO_CATALOG
    ready = SimpleNamespace(quests=SimpleNamespace(subjugation=object()), presents=None)
    assert WATERMARK_RESYNC.availability(ready) is None and PROGRESS_RESET.availability(ready) is None
    assert REWARDS_REDELIVER.availability(ready) == missing_service("presents")
    with pytest.raises(ValueError):
        admin_cmd_subjugation.register(registry)


@pytest.mark.parametrize("operation", [command.operation for command in COMMANDS])
@pytest.mark.parametrize("params", [{}, {"mission_id": 0}, {"mission_id": -1}, {"mission_id": "400587"},
                                    {"mission_id": OPEN_MISSION, "extra": 1}])
def test_parameters_are_strict(registry, operation, params):
    with pytest.raises(AdminError) as caught:
        registry.get(operation).parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_resyncing_commits_the_recorded_scores_as_the_watermark(bundle, live_state, preflight, registry):
    state = with_records(live_state, subjugation_record(MEMBER, 300, 600))
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, "subjugation.watermark.resync", {"mission_id": OPEN_MISSION})
    assert plan.confirmations == ()
    assert plan.preflight["readback"] == {"quest_records": [QUEST_RECORD_ROUTE], JOURNAL_KEY: [QUEST_RECORD_ROUTE]}
    [step] = plan.describe()["steps"]
    assert step["effects"] == {"mission_id": OPEN_MISSION,
                               "watermark": {"before": None, "after": {"best": 300, "total": 600}}}
    assert step["warnings"] == []
    commit(pipeline, plan)
    assert store.backups == []
    assert watermark(store.state, OPEN_MISSION) == ScorePair(300, 600)
    again = planned(pipeline, registry, "subjugation.watermark.resync", {"mission_id": OPEN_MISSION})
    assert not again.changed


def test_resyncing_lowers_a_watermark_the_records_fell_below(bundle, live_state, preflight, registry):
    state = with_records(live_state, subjugation_record(MEMBER, 100, 100))
    state = {**state, JOURNAL_KEY: journal_with({str(OPEN_MISSION): {"best": 700, "total": 900}})}
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, "subjugation.watermark.resync", {"mission_id": OPEN_MISSION})
    [step] = plan.describe()["steps"]
    assert step["effects"]["watermark"] == {"before": {"best": 700, "total": 900},
                                            "after": {"best": 100, "total": 100}}
    assert step["warnings"] == [LOWERED_REDELIVERY_RISK]
    committed = commit(pipeline, plan)
    assert [entry["warnings"] for entry in committed.result["steps"]] == [[LOWERED_REDELIVERY_RISK]]
    assert watermark(store.state, OPEN_MISSION) == ScorePair(100, 100)


@pytest.mark.parametrize("held,lowered", [
    ({"best": 100, "total": 100}, False), ({"best": 300, "total": 600}, False),
    ({"best": 700, "total": 100}, True), ({"best": 100, "total": 900}, True),
], ids=["raised", "kept", "best_falls", "total_falls"])
def test_resyncing_warns_of_repeat_mailing_once_any_watermark_field_falls(bundle, live_state, preflight, registry,
                                                                          held, lowered):
    state = with_records(live_state, subjugation_record(MEMBER, 300, 600))
    state = {**state, JOURNAL_KEY: journal_with({str(OPEN_MISSION): held})}
    _, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, "subjugation.watermark.resync", {"mission_id": OPEN_MISSION})
    [step] = plan.describe()["steps"]
    assert step["warnings"] == ([LOWERED_REDELIVERY_RISK] if lowered else [])
    assert step["effects"]["watermark"] == {"before": held, "after": {"best": 300, "total": 600}}


def test_redelivery_commits_the_owed_tiers_and_pushes_the_watermark(bundle, live_state, preflight, registry):
    state = with_records(live_state, subjugation_record(MEMBER, 300, 600))
    state = {**state, JOURNAL_KEY: journal_with({str(OPEN_MISSION): {"best": 100, "total": 100}})}
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, "subjugation.rewards.redeliver", {"mission_id": OPEN_MISSION})
    assert plan.preflight["readback"] == {"presents": [PRESENTS_READ.request], "quest_records": [QUEST_RECORD_ROUTE],
                                          JOURNAL_KEY: [QUEST_RECORD_ROUTE]}
    [step] = plan.describe()["steps"]
    mailed = step["effects"]["mailed"]
    assert step["warnings"] == [] and len(mailed) == 4
    assert step["effects"]["watermark"] == {"before": {"best": 100, "total": 100},
                                            "after": {"best": 300, "total": 600}}
    commit(pipeline, plan)
    assert len(store.state["tables"]["presents"]) == len(live_state["tables"]["presents"]) + len(mailed)
    assert watermark(store.state, OPEN_MISSION) == ScorePair(300, 600)
    covered = planned(pipeline, registry, "subjugation.rewards.redeliver", {"mission_id": OPEN_MISSION})
    [repeat] = covered.describe()["steps"]
    assert repeat["warnings"] == [NOTHING_OWED] and repeat["effects"]["mailed"] == []


def test_redelivery_without_a_watermark_mails_nothing(bundle, live_state, preflight, registry):
    state = with_records(live_state, subjugation_record(MEMBER, 300, 600))
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, "subjugation.rewards.redeliver", {"mission_id": OPEN_MISSION})
    [step] = plan.describe()["steps"]
    assert step["warnings"] == [NOTHING_OWED] and step["effects"]["mailed"] == []
    assert step["effects"]["watermark"] == {"before": None, "after": {"best": 300, "total": 600}}
    commit(pipeline, plan)
    assert len(store.state["tables"]["presents"]) == len(live_state["tables"]["presents"])
    assert watermark(store.state, OPEN_MISSION) == ScorePair(300, 600)


def test_resetting_needs_its_phrase_and_clears_records_and_watermark(bundle, live_state, preflight, registry):
    state = with_records(live_state, subjugation_record(MEMBER, 300, 600), subjugation_record(LAST_MEMBER, 10, 10),
                         subjugation_record(OTHER_MEMBER, 50, 60))
    state = {**state, JOURNAL_KEY: journal_with({str(OPEN_MISSION): {"best": 300, "total": 610}})}
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, "subjugation.progress.reset", {"mission_id": OPEN_MISSION})
    phrase = RESET_PHRASE.format(mission_id=OPEN_MISSION)
    assert [item.phrase for item in plan.confirmations] == [phrase]
    [step] = plan.describe()["steps"]
    assert step["warnings"] == [REDELIVERY_RISK]
    assert step["effects"] == {"mission_id": OPEN_MISSION, "cleared_quests": [MEMBER, LAST_MEMBER],
                               "watermark": {"before": {"best": 300, "total": 610}, "after": None}}
    with pytest.raises(AdminError) as caught:
        commit(pipeline, plan)
    assert caught.value.code == "admin_confirmation_required" and store.calls == []
    commit(pipeline, plan, (phrase,))
    [backup] = store.backups
    assert backup["row"]["kind"] == AUTO_BACKUP_KIND
    records = {row["QuestID"]: row for row in store.state["tables"]["quest_records"]}
    assert records[MEMBER]["MaxRank"] == records[LAST_MEMBER]["MaxRank"] == 0
    assert records[MEMBER]["AchieveFlag"] == CLEAR_FLAG and records[OTHER_MEMBER]["MaxRank"] == pack(50, 60)
    assert watermark(store.state, OPEN_MISSION) is None


def test_commands_refuse_while_the_mission_has_a_battle_in_progress(bundle, live_state, preflight, registry):
    active = {"sequence": 1, "quest_id": MEMBER, "start_key": START_KEY, "mission_id": OPEN_MISSION}
    state = with_records(live_state, subjugation_record(MEMBER, 300, 600))
    busy = {**state, JOURNAL_KEY: journal_with(active=active)}
    _, pipeline = console(bundle, busy, preflight)
    for command in COMMANDS:
        preview_error(pipeline, registry, command.operation, {"mission_id": OPEN_MISSION}, "quest_battle_active", 409)
    elsewhere = {**state, JOURNAL_KEY: journal_with(active={**active, "quest_id": OTHER_MEMBER,
                                                            "mission_id": OTHER_MISSION})}
    _, open_pipeline = console(bundle, elsewhere, preflight)
    assert planned(open_pipeline, registry, "subjugation.watermark.resync", {"mission_id": OPEN_MISSION}).committable


def test_unknown_missions_and_malformed_journals_are_refused(bundle, live_state, preflight, registry):
    _, pipeline = console(bundle, live_state, preflight)
    preview_error(pipeline, registry, "subjugation.watermark.resync", {"mission_id": 4807},
                  "quest_mission_unknown", 409)
    _, broken = console(bundle, {**live_state, JOURNAL_KEY: {"active": 1}}, preflight)
    preview_error(broken, registry, "subjugation.watermark.resync", {"mission_id": OPEN_MISSION},
                  "quest_state_invalid", 503)


def test_the_views_answer_the_catalog_status_and_the_described_save(bundle, live_state):
    views = {view.name: view for view in admin_views_subjugation.VIEWS}
    context = SimpleNamespace(services=None, profile=None, generation=SimpleNamespace(bundle=bundle), now=NOW,
                              state=live_state, revision=0)
    before = deepcopy(live_state)
    status = views["battle.subjugation"].read(context, None)["subjugation"]
    assert status["ok"] is True and status["ready"] is True
    assert status["content_version"] == bundle.quests.subjugation.content_version
    progress = views["battle.subjugation_progress"].read(context, None)["progress"]
    assert progress["ok"] is True
    assert [entry["mission_id"] for entry in progress["missions"]] == list(bundle.quests.subjugation.mission_ids)
    assert live_state == before
    hollow = SimpleNamespace(generation=SimpleNamespace(bundle=SimpleNamespace(
        quests=SimpleNamespace(subjugation=None))), state=live_state, now=NOW)
    with pytest.raises(AdminError) as caught:
        views["battle.subjugation"].read(hollow, None)
    assert caught.value.code == "admin_view_service_unavailable"


def test_the_status_view_reads_the_catalog_at_the_view_clock():
    seen = []
    catalog = SimpleNamespace(status=lambda now=None: seen.append(now) or {"ready": True})
    context = SimpleNamespace(generation=SimpleNamespace(bundle=SimpleNamespace(quests=SimpleNamespace(subjugation=catalog))),
                              now=NOW)
    views = {view.name: view for view in admin_views_subjugation.VIEWS}
    assert views["battle.subjugation"].read(context, None)["subjugation"] == {"ok": True, "ready": True}
    assert seen == [NOW]

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps import admin_cmd_quests
from aigis_ps.admin_cmd_quests import (ABORT_PHRASE, ARCHIVE_PHRASE, BATTLE, COMMANDS, JOURNAL_ABORT, JOURNAL_FLUSH,
                                       NOTHING_MISSING, NOTHING_PENDING, RECORD_EDIT, RECORD_EDIT_WITH_REWARDS,
                                       RECORDS_ARCHIVE, RECORDS_RESTORE, RECORDS_SKIPPED, STILL_WAITING)
from aigis_ps.admin_command import AdminError, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import (PREFLIGHT_DOMAINS, QUEST_RECORD_ROUTE, QUEST_RECORDS_READ, GenerationPreflight,
                                      PreflightCandidate)
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.player_state import PlayerStateService
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.quest_modes import CLEAR_FLAG, THREE_STAR_FLAG
from aigis_ps.quest_record_admin import ARCHIVE_KEY, QUEST_ID, RECORD_TABLE, read_archive
from aigis_ps.quest_service import JOURNAL_KEY
from aigis_ps.storage import AUTO_BACKUP_KIND
from test_admin_cmd_storage import commit, planned, preview_error
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_bootstrap import asset_root, quest_master
from test_player_state import assets, master
from test_quest_record_admin import master_ids, story_quest
from test_quest_service import bundle


NOW = datetime(2026, 11, 5, 12, tzinfo=JST)
SAVED_AT = datetime(2026, 11, 5, 12)
THREE_STAR_FLAGS = CLEAR_FLAG | THREE_STAR_FLAG
FOREIGN_QUEST = 999_999_999
CURRENT_QUESTS = (1001, 1002)
STAGED_QUESTS = (1001,)
STAGED_SNAPSHOT, UNKNOWN_SNAPSHOT = "a1" * 12, "e9" * 12
START_KEY = "s" * 64


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_quests.register(commands)
    return commands


@pytest.fixture
def live_state(bundle):
    return bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))


def console(bundle, state, preflight, *, revision=0):
    store = FakeStore(state, revision)
    generation = FakeGeneration(id="gen-quests", bundle=bundle, clock=NOW)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


class FakeMasters:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def snapshot(self, identifier):
        if identifier not in self.snapshots:
            raise ProtocolError("masterdata_snapshot_unknown", "No master snapshot is stored under this identifier", 404)
        quest_ids = self.snapshots[identifier]
        return SimpleNamespace(identifier=identifier, get_xml=lambda name: quest_master(quest_ids))


@pytest.fixture
def staged(asset_root, assets, master):
    bootstrap = BootstrapService(asset_root, master_provider=lambda name: quest_master(CURRENT_QUESTS),
                                 clock=lambda: SAVED_AT, asset_version="a" * 40)
    services = SimpleNamespace(bootstrap=bootstrap, player_state=PlayerStateService(assets[0], lambda name: master),
                               masters=FakeMasters({STAGED_SNAPSHOT: STAGED_QUESTS}))
    state = services.player_state.initialize(bootstrap.initial_state())
    return services, state


def staged_console(services, state, preflight):
    store = FakeStore(state)
    generation = FakeGeneration(id="gen-staging", bundle=services, clock=NOW)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(lambda value: value),
                                 FakeRuntime(generation), backup_directory=Path("backups"))


def record_row(quest_id, **changes):
    return {QUEST_ID: quest_id, "MaxRank": 0, "AchieveFlag": 0, "RemainAssault": 0, "FirstDefeatDate": 0,
            "Shown": False, **changes}


def battle_journal(quest_id=100):
    return {"schema_version": 2, "next_sequence": 2, "processed_keys": [START_KEY], "history": [],
            "active": {"sequence": 1, "quest_id": quest_id, "start_key": START_KEY,
                       "master_snapshot": "snapshot-1", "resource_version": "resource-1"}}


def pending_journal(copies=3):
    return {"schema_version": 2, "next_sequence": 1, "processed_keys": [], "history": [], "active": None,
            "pending_presents": [{"reward": {"kind": "free_crystal", "amount": 2, "description": "★３報酬"},
                                  "copies": copies}]}


def test_commands_register_on_the_battle_domain_with_their_preflight(registry):
    assert [command.operation for command in registry] == [
        "quests.journal.abort", "quests.journal.flush_pending", "quests.record.edit",
        "quests.record.edit_with_rewards", "quests.records.archive_missing", "quests.records.restore_archived"]
    assert all(command.domain == BATTLE for command in COMMANDS)
    catalog = discover(PREFLIGHT_DOMAINS)
    assert "admin_cmd_quests" in catalog.modules
    assert all(command.operation in catalog.commands for command in COMMANDS)
    assert RECORD_EDIT.touched == RECORDS_ARCHIVE.touched == RECORDS_RESTORE.touched == frozenset({"quest_records"})
    assert JOURNAL_ABORT.touched == frozenset({JOURNAL_KEY})
    assert JOURNAL_FLUSH.touched == frozenset({JOURNAL_KEY, "presents"})
    assert RECORD_EDIT_WITH_REWARDS.touched == frozenset({"quest_records", "missions", "rank_rewards", "presents",
                                                          "possessions", "crystals", "units"})
    assert all(domain in PREFLIGHT_DOMAINS for command in COMMANDS for domain in command.touched)
    assert all(not command.preflight_none for command in COMMANDS)
    assert JOURNAL_ABORT.destructive is True and RECORDS_ARCHIVE.destructive is True
    assert RECORD_EDIT.destructive is False and RECORDS_RESTORE.destructive is False
    assert JOURNAL_ABORT.phrase(JOURNAL_ABORT.parse({})) == ABORT_PHRASE
    assert RECORDS_ARCHIVE.phrase(RECORDS_ARCHIVE.parse({"master_snapshot": STAGED_SNAPSHOT})) == ARCHIVE_PHRASE.format(
        snapshot=STAGED_SNAPSHOT)
    reason = RECORD_EDIT_WITH_REWARDS.availability(SimpleNamespace(quests=None, missions=None, rank_rewards=None))
    assert reason == missing_service("quests")
    with pytest.raises(ValueError):
        admin_cmd_quests.register(registry)


@pytest.mark.parametrize("operation,params", [
    ("quests.record.edit", {}),
    ("quests.record.edit", {"QuestID": 1001}),
    ("quests.record.edit", {"QuestID": 0, "MaxRank": 1}),
    ("quests.record.edit", {"QuestID": 1001, "MaxRank": -1}),
    ("quests.record.edit", {"QuestID": 1001, "AchieveFlag": INT32_MAX + 1}),
    ("quests.record.edit", {"QuestID": 1001, "Shown": 1}),
    ("quests.record.edit", {"QuestID": 1001, "MaxRank": 3, "stars": 3}),
    ("quests.record.edit_with_rewards", {"QuestID": 1001}),
    ("quests.record.edit_with_rewards", {"QuestID": 1001, "stars": 0, "hard": False}),
    ("quests.record.edit_with_rewards", {"QuestID": 1001, "stars": 5, "hard": False}),
    ("quests.record.edit_with_rewards", {"QuestID": 1001, "stars": 3, "hard": "no"}),
    ("quests.journal.abort", {"force": True}),
    ("quests.journal.flush_pending", {"copies": 1}),
    ("quests.records.archive_missing", {}),
    ("quests.records.archive_missing", {"master_snapshot": "XYZ"}),
    ("quests.records.archive_missing", {"master_snapshot": STAGED_SNAPSHOT.upper()}),
    ("quests.records.restore_archived", {"quest_ids": []}),
    ("quests.records.restore_archived", {"quest_ids": [1, 1]}),
    ("quests.records.restore_archived", {"quest_ids": [0]}),
])
def test_parameters_are_strict(registry, operation, params):
    with pytest.raises(AdminError) as caught:
        registry.get(operation).parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_record_edits_commit_once_and_read_back_through_the_quest_record_route(bundle, live_state, preflight,
                                                                               registry, story_quest):
    store, pipeline = console(bundle, live_state, preflight)
    plan = planned(pipeline, registry, "quests.record.edit",
                   {"QuestID": story_quest, "MaxRank": 3, "AchieveFlag": THREE_STAR_FLAGS, "Shown": True})
    assert plan.confirmations == () and plan.preflight["readback"] == {"quest_records": [QUEST_RECORD_ROUTE]}
    [step] = plan.describe()["steps"]
    assert step["effects"] == {"quest_id": story_quest, "record": {
        "before": None,
        "after": record_row(story_quest, MaxRank=3, AchieveFlag=THREE_STAR_FLAGS, Shown=True)}}
    commit(pipeline, plan)
    assert store.backups == []
    row = next(row for row in store.state["tables"][RECORD_TABLE] if row[QUEST_ID] == story_quest)
    assert row == record_row(story_quest, MaxRank=3, AchieveFlag=THREE_STAR_FLAGS, Shown=True)
    assert store.state.get("mission_state") == live_state.get("mission_state")
    assert store.state["tables"]["presents"] == live_state["tables"]["presents"]
    preview_error(pipeline, registry, "quests.record.edit", {"QuestID": story_quest, "Shown": True},
                  "quest_record_unchanged", 409)
    preview_error(pipeline, registry, "quests.record.edit", {"QuestID": FOREIGN_QUEST, "Shown": True},
                  "quest_not_found", 404)


def test_record_edits_with_rewards_pay_the_first_star_in_the_same_commit(bundle, live_state, preflight, registry,
                                                                         story_quest):
    store, pipeline = console(bundle, live_state, preflight)
    plan = planned(pipeline, registry, "quests.record.edit_with_rewards",
                   {"QuestID": story_quest, "MaxRank": 3, "AchieveFlag": THREE_STAR_FLAGS,
                    "stars": 3, "hard": False})
    [step] = plan.describe()["steps"]
    rewards = step["effects"]["rewards"]
    assert rewards["first_star"] is True and rewards["stars"] == 3 and rewards["hard"] is False
    commit(pipeline, plan)
    assert store.state["tables"][RECORD_TABLE][-1][QUEST_ID] == story_quest
    assert store.state.get("mission_state") != live_state.get("mission_state")
    again = planned(pipeline, registry, "quests.record.edit_with_rewards",
                    {"QuestID": story_quest, "stars": 3, "hard": False})
    [repeat] = again.describe()["steps"]
    assert repeat["effects"]["rewards"]["first_star"] is False
    assert repeat["effects"]["rewards"]["star_rewards"] == []


def test_aborting_keeps_the_exit_semantics_behind_its_phrase_and_a_backup(bundle, live_state, preflight, registry):
    state = {**live_state, JOURNAL_KEY: battle_journal()}
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, "quests.journal.abort", {})
    assert [item.phrase for item in plan.confirmations] == [ABORT_PHRASE]
    with pytest.raises(AdminError) as caught:
        commit(pipeline, plan)
    assert caught.value.code == "admin_confirmation_required" and store.calls == []
    commit(pipeline, plan, (ABORT_PHRASE,))
    [backup] = store.backups
    assert backup["row"]["kind"] == AUTO_BACKUP_KIND
    journal = store.state[JOURNAL_KEY]
    assert journal["active"] is None and journal["processed_keys"] == [START_KEY]
    [entry] = journal["history"]
    assert (entry["kind"], entry["quest_id"], entry["start_key"], entry["sequence"]) == ("aborted", 100, START_KEY, 1)
    assert entry["completed_at"] == NOW.timestamp()
    assert store.state["tables"]["game_param"] == live_state["tables"]["game_param"]
    [step] = plan.describe()["steps"]
    assert step["effects"] == {"aborted": {"quest_id": 100, "sequence": 1, "start_key": START_KEY}}


def test_aborting_without_an_active_battle_is_refused(bundle, live_state, preflight, registry):
    _, pipeline = console(bundle, live_state, preflight)
    preview_error(pipeline, registry, "quests.journal.abort", {}, "quest_attempt_missing", 409)


def test_flushing_mails_the_waiting_battle_presents(bundle, live_state, preflight, registry):
    state = {**live_state, JOURNAL_KEY: pending_journal(copies=3)}
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, "quests.journal.flush_pending", {})
    [step] = plan.describe()["steps"]
    assert len(step["effects"]["delivered"]) == 3 and step["effects"]["waiting"] == []
    assert step["warnings"] == []
    commit(pipeline, plan)
    assert "pending_presents" not in store.state[JOURNAL_KEY]
    assert len(store.state["tables"]["presents"]) == len(live_state["tables"]["presents"]) + 3


def test_flushing_keeps_presents_a_full_mailbox_cannot_take(bundle, live_state, preflight, registry):
    filler = [{"kind": "gold", "amount": 1, "description": "filler"}] * MAX_PRESENTS
    full, _ = bundle.presents.enqueue(live_state, filler)
    state = {**full, JOURNAL_KEY: pending_journal(copies=2)}
    store, pipeline = console(bundle, state, preflight)
    plan = planned(pipeline, registry, "quests.journal.flush_pending", {})
    [step] = plan.describe()["steps"]
    assert step["effects"]["delivered"] == []
    assert step["effects"]["waiting"] == pending_journal(copies=2)["pending_presents"]
    assert step["warnings"] == [STILL_WAITING]
    commit(pipeline, plan)
    assert store.state[JOURNAL_KEY]["pending_presents"] == pending_journal(copies=2)["pending_presents"]


def test_flushing_with_nothing_queued_saves_nothing(bundle, live_state, preflight, registry):
    store, pipeline = console(bundle, live_state, preflight)
    plan = planned(pipeline, registry, "quests.journal.flush_pending", {})
    [step] = plan.describe()["steps"]
    assert step["warnings"] == [NOTHING_PENDING] and not plan.changed
    commit(pipeline, plan)
    assert store.state == live_state and store.revision == 0


def archived_state(services, state):
    rows = [record_row(1001, MaxRank=3, AchieveFlag=THREE_STAR_FLAGS), record_row(1002, MaxRank=2, Shown=True)]
    return {**state, "tables": {**state["tables"], RECORD_TABLE: rows}}


def readable(services, state):
    generation = FakeGeneration(id="gen-staging", bundle=services, clock=NOW)
    return QUEST_RECORDS_READ.encode(PreflightCandidate(state, generation))


def test_archiving_and_restoring_round_trip_behind_the_staged_master(staged, preflight, registry):
    services, base = staged
    state = archived_state(services, base)
    assert readable(services, state)
    store, pipeline = staged_console(services, state, preflight)
    plan = planned(pipeline, registry, "quests.records.archive_missing", {"master_snapshot": STAGED_SNAPSHOT})
    assert [item.phrase for item in plan.confirmations] == [ARCHIVE_PHRASE.format(snapshot=STAGED_SNAPSHOT)]
    [step] = plan.describe()["steps"]
    assert step["effects"]["archived"] == [record_row(1002, MaxRank=2, Shown=True)]
    assert step["effects"]["archive_size"] == 1 and step["warnings"] == []
    commit(pipeline, plan, (ARCHIVE_PHRASE.format(snapshot=STAGED_SNAPSHOT),))
    [backup] = store.backups
    assert backup["row"]["kind"] == AUTO_BACKUP_KIND
    assert [row[QUEST_ID] for row in store.state["tables"][RECORD_TABLE]] == [1001]
    assert read_archive(store.state) == [record_row(1002, MaxRank=2, Shown=True)]
    assert readable(services, store.state)
    staged_services = SimpleNamespace(**{**vars(services)})
    staged_services.bootstrap = BootstrapService(services.bootstrap.asset_root,
                                                 master_provider=lambda name: quest_master(STAGED_QUESTS),
                                                 clock=lambda: SAVED_AT, asset_version="a" * 40)
    assert readable(staged_services, store.state)
    switched_store, switched = staged_console(staged_services, store.state, preflight)
    skipped = planned(switched, registry, "quests.records.restore_archived", {"quest_ids": [1002]})
    [skip_step] = skipped.describe()["steps"]
    assert skip_step["effects"] == {"restored": [], "skipped_missing_master": [1002], "archive_size": 1}
    assert skip_step["warnings"] == [RECORDS_SKIPPED]
    outcome = commit(switched, skipped)
    assert read_archive(switched_store.state) == [record_row(1002, MaxRank=2, Shown=True)]
    assert outcome.result["revision_after"] == outcome.result["revision_before"]


def test_restoring_returns_rows_the_running_master_lists(staged, preflight, registry):
    services, base = staged
    state = archived_state(services, base)
    store, pipeline = staged_console(services, state, preflight)
    services.masters.snapshots[STAGED_SNAPSHOT] = (1001, 1003)
    plan = planned(pipeline, registry, "quests.records.archive_missing", {"master_snapshot": STAGED_SNAPSHOT})
    commit(pipeline, plan, (ARCHIVE_PHRASE.format(snapshot=STAGED_SNAPSHOT),))
    assert [row[QUEST_ID] for row in read_archive(store.state)] == [1002]
    restore = planned(pipeline, registry, "quests.records.restore_archived", {"quest_ids": [1002]})
    [step] = restore.describe()["steps"]
    assert step["effects"] == {"restored": [record_row(1002, MaxRank=2, Shown=True)],
                               "skipped_missing_master": [], "archive_size": 0}
    commit(pipeline, restore)
    assert ARCHIVE_KEY not in store.state
    assert record_row(1002, MaxRank=2, Shown=True) in store.state["tables"][RECORD_TABLE]
    assert readable(services, store.state)


def test_archive_and_restore_refusal_codes(staged, preflight, registry):
    services, base = staged
    state = archived_state(services, base)
    _, pipeline = staged_console(services, state, preflight)
    preview_error(pipeline, registry, "quests.records.archive_missing", {"master_snapshot": UNKNOWN_SNAPSHOT},
                  "masterdata_snapshot_unknown", 404)
    preview_error(pipeline, registry, "quests.records.restore_archived", {"quest_ids": [1002]},
                  "quest_record_archive_missing", 404)
    plan = planned(pipeline, registry, "quests.records.archive_missing", {"master_snapshot": STAGED_SNAPSHOT})
    commit(pipeline, plan, (ARCHIVE_PHRASE.format(snapshot=STAGED_SNAPSHOT),))
    nothing = planned(pipeline, registry, "quests.records.archive_missing", {"master_snapshot": STAGED_SNAPSHOT})
    [step] = nothing.describe()["steps"]
    assert step["warnings"] == [NOTHING_MISSING] and step["effects"]["archived"] == []

from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from aigis_ps import admin_views_battle
from aigis_ps.admin_diff import json_digest, redact
from aigis_ps.admin_views_battle import LOCAL_CLIENT_PLATFORM, AvailabilityParams, EntrustPreviewParams, \
    EntrustQuestParams, RecordParams, record_flags
from aigis_ps.admin_registry import ViewRegistry
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.energy_clock import JST
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.player_state import PlayerStateService
from aigis_ps.quest_modes import CLEAR_FLAG, EPISODE_FLAG, HARD_STAR_FLAG, SLOT_FLAGS, THREE_STAR_FLAG
from aigis_ps.quest_record_admin import ARCHIVE_KEY, QUEST_ID, RECORD_TABLE
from aigis_ps.quest_service import JOURNAL_KEY
from aigis_ps.storage import PlayerProfile
from test_bootstrap import asset_root, quest_master
from test_player_state import assets, master
from test_quest_record_admin import master_ids, story_quest
from test_quest_service import bundle


NOW = datetime(2026, 11, 5, 12, tzinfo=JST)
SAVED_AT = datetime(2026, 11, 5, 12)
CURRENT_QUESTS = (1001, 1002)
STAGED_QUESTS = (1001,)
SNAPSHOT, RESOURCES = "snapshot-a", "resource-a"
FOREIGN_QUEST = 999_999_999
VIEWS = {view.name: view for view in admin_views_battle.VIEWS}


@pytest.fixture
def services(asset_root, assets, master):
    bootstrap = BootstrapService(asset_root, master_provider=lambda name: quest_master(CURRENT_QUESTS),
                                 clock=lambda: SAVED_AT, asset_version="a" * 40)
    return SimpleNamespace(bootstrap=bootstrap, player_state=PlayerStateService(assets[0], lambda name: master),
                           quests=SimpleNamespace(catalog=SimpleNamespace(
                               snapshot=SimpleNamespace(identifier=SNAPSHOT), resource_version=RESOURCES)))


@pytest.fixture
def state(services):
    return services.player_state.initialize(services.bootstrap.initial_state())


def view_context(bundle, state, *, revision=0, candidate=None):
    profile = PlayerProfile(player_id="1", user_key="0" * 32, method_key="0" * 32, state=state, revision=revision)
    return SimpleNamespace(services=SimpleNamespace(runtime=SimpleNamespace(candidate=lambda: candidate)),
                           profile=profile, generation=SimpleNamespace(bundle=bundle), now=NOW,
                           state=state, revision=revision)


def read(name, context, params=None):
    before = deepcopy(context.state)
    digest = json_digest(before)
    result = VIEWS[name].read(context, params)
    assert context.state == before and json_digest(context.state) == digest
    return result


def active_journal(snapshot=SNAPSHOT, resources=RESOURCES):
    return {"schema_version": 2, "next_sequence": 3, "processed_keys": ["k" * 64], "history": [
        {"kind": "success", "quest_id": 1001, "sequence": 1, "start_key": "a" * 64, "finish_key": "b" * 64,
         "completed_at": 1.0, "response": "<DA><SYSTEM></SYSTEM></DA>"}],
        "active": {"sequence": 2, "quest_id": 1002, "start_key": "c" * 64, "registration": 77,
                   "started_at": 2.0, "master_snapshot": snapshot, "resource_version": resources,
                   "charge": "settle", "mode": {"quest_type": 0}, "rules": {"gold_percent": 100},
                   "candidate_stars": 0, "selected_mask": 1, "mission_id": 5,
                   "unit_reservations": [{"card_id": 7, "level": 1, "count": 1}],
                   "definition": {"quest_id": 1002}, "response": "<DA><SYSTEM></SYSTEM></DA>"},
        "pending_presents": [{"reward": {"kind": "gold", "amount": 5, "description": "pending"}, "copies": 2}],
        "mode_progress": {"story": {"runs": 1}}}


def test_views_register_under_the_battle_domain():
    registry = ViewRegistry()
    admin_views_battle.register(registry)
    assert [view.name for view in registry] == ["battle.entrust", "battle.entrust_availability",
                                                "battle.entrust_preview", "battle.entrust_quest", "battle.journal",
                                                "battle.record_archive", "battle.records"]
    assert all(view.domain == "battle" for view in registry)
    assert all(callable(getattr(view.read, "__wrapped__", None)) for view in registry)
    live = {view.name for view in registry if view.live}
    assert live == {"battle.entrust", "battle.entrust_availability", "battle.entrust_preview",
                    "battle.entrust_quest", "battle.journal", "battle.record_archive"}


def test_the_journal_view_freezes_the_active_attempt_and_redacts_responses(services, state):
    held = {**state, JOURNAL_KEY: active_journal()}
    result = read("battle.journal", view_context(services, held))["journal"]
    assert result["ok"] is True
    active = result["active"]
    assert (active["quest_id"], active["sequence"], active["start_key"]) == (1002, 2, "c" * 64)
    assert (active["master_snapshot"], active["resource_version"]) == (SNAPSHOT, RESOURCES)
    assert active["charge"] == "settle" and active["mode"] == {"quest_type": 0}
    assert active["rules"] == {"gold_percent": 100}
    assert active["unit_reservations"] == [{"card_id": 7, "level": 1, "count": 1}]
    assert active["matches_running_generation"] is True
    assert "response" not in active
    assert result["pending_presents"] == active_journal()["pending_presents"]
    assert result["mode_progress"] == {"story": {"runs": 1}}
    assert result["processed_keys"] == 1
    [entry] = result["history"]
    assert entry["response"]["redacted"] == "response"


def test_the_journal_view_marks_attempts_outside_the_running_generation(services, state):
    held = {**state, JOURNAL_KEY: active_journal(snapshot="snapshot-b")}
    active = read("battle.journal", view_context(services, held))["journal"]["active"]
    assert active["matches_running_generation"] is False
    services.quests = None
    held = {**state, JOURNAL_KEY: active_journal()}
    active = read("battle.journal", view_context(services, held))["journal"]["active"]
    assert active["matches_running_generation"] is None


def test_the_journal_view_reports_invalid_journals_without_raising(services, state):
    result = read("battle.journal", view_context(services, {**state, JOURNAL_KEY: {"active": 1}}))["journal"]
    assert result["ok"] is False and result["error"]["code"] == "quest_state_invalid"
    empty = read("battle.journal", view_context(services, state))["journal"]
    assert empty["ok"] is True and empty["active"] is None and empty["history"] == []


def test_record_rows_decode_their_achieve_flag_bits(services, state):
    flags = CLEAR_FLAG | THREE_STAR_FLAG | SLOT_FLAGS[0] | SLOT_FLAGS[4] | EPISODE_FLAG
    rows = [{QUEST_ID: 1001, "MaxRank": 3, "AchieveFlag": flags, "RemainAssault": 2, "FirstDefeatDate": 7,
             "Shown": True},
            {QUEST_ID: 1002, "MaxRank": 4, "AchieveFlag": CLEAR_FLAG | HARD_STAR_FLAG, "RemainAssault": 0,
             "FirstDefeatDate": 0, "Shown": False}]
    held = {**state, "tables": {**state["tables"], RECORD_TABLE: rows}}
    listing = read("battle.records", view_context(services, held), RecordParams())
    assert listing["total"] == 2
    first, second = listing["items"]
    assert first["flags"] == {"cleared": True, "three_star": True, "four_star": False, "episode": True,
                              "slots": [1, 5]}
    assert second["flags"] == {"cleared": True, "three_star": False, "four_star": True, "episode": False,
                               "slots": []}
    only = read("battle.records", view_context(services, held), RecordParams(quest_id=1002))
    assert [row[QUEST_ID] for row in only["items"]] == [1002]
    window = read("battle.records", view_context(services, held), RecordParams(offset=1, limit=1))
    assert [row[QUEST_ID] for row in window["items"]] == [1002] and window["next_offset"] is None


def test_record_flags_mask_values_beyond_int32():
    assert admin_views_battle.EPISODE_FLAG is EPISODE_FLAG
    assert record_flags(-(1 << 31) | CLEAR_FLAG)["cleared"] is True
    assert record_flags(INT32_MAX)["slots"] == [1, 2, 3, 4, 5]


def archive_rows(state):
    rows = [{QUEST_ID: 1001, "MaxRank": 1, "AchieveFlag": 0, "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False},
            {QUEST_ID: FOREIGN_QUEST, "MaxRank": 2, "AchieveFlag": 0, "RemainAssault": 0, "FirstDefeatDate": 0,
             "Shown": True}]
    archived = [{QUEST_ID: 1003, "MaxRank": 3, "AchieveFlag": 0, "RemainAssault": 0, "FirstDefeatDate": 0,
                 "Shown": False}]
    return {**state, "tables": {**state["tables"], RECORD_TABLE: rows},
            ARCHIVE_KEY: {"schema_version": 1, "rows": archived}}


def test_the_archive_view_lists_missing_rows_for_the_running_and_staged_masters(services, state, asset_root):
    staged = BootstrapService(asset_root, master_provider=lambda name: quest_master(STAGED_QUESTS),
                              clock=lambda: SAVED_AT, asset_version="a" * 40)
    candidate = SimpleNamespace(id="candidate-1", identity=SimpleNamespace(master_snapshot="5a" * 12),
                                bundle=SimpleNamespace(bootstrap=staged))
    result = read("battle.record_archive", view_context(services, archive_rows(state), candidate=candidate))
    assert result["archived"]["ok"] is True
    assert [row[QUEST_ID] for row in result["archived"]["rows"]] == [1003]
    assert result["running_master"] == {"ok": True, "missing": [FOREIGN_QUEST]}
    staged_report = result["candidate"]
    assert (staged_report["generation_id"], staged_report["master_snapshot"]) == ("candidate-1", "5a" * 12)
    assert staged_report["ok"] is True and staged_report["missing"] == [FOREIGN_QUEST]
    assert staged_report["master_quest_ids"] == len(STAGED_QUESTS)


def test_the_archive_view_reports_the_missing_candidate(services, state):
    result = read("battle.record_archive", view_context(services, archive_rows(state)))
    assert result["candidate"] is None


def test_entrust_views_answer_from_the_live_catalog(bundle, story_quest):
    state = bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))
    context = view_context(bundle, state)
    assert AvailabilityParams().platform_code == LOCAL_CLIENT_PLATFORM == 9
    summary = read("battle.entrust", context)
    assert summary["status"]["ok"] is True and "status" in summary["status"]
    assert summary["history"]["ok"] is True
    availability = read("battle.entrust_availability", context, AvailabilityParams())
    assert availability["ok"] is True and availability["platform_code"] == LOCAL_CLIENT_PLATFORM
    assert availability["preview"] is False and "types" in availability
    described = read("battle.entrust_quest", context, EntrustQuestParams(quest_id=story_quest))
    assert described["ok"] is True and described["quest_id"] == story_quest and described["quest_type"] == 0
    assert "live" in described and described["live"]["platform_code"] == LOCAL_CLIENT_PLATFORM
    preview = read("battle.entrust_preview", context, EntrustPreviewParams(quest_id=story_quest))
    assert preview == {"ok": True, **redact(bundle.entrust.preview(
        context.profile, story_quest, quantity=1, now=NOW, platform_code=LOCAL_CLIENT_PLATFORM))}
    assert preview["quest_type"] == 0

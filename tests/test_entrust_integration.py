from argparse import Namespace
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.cli import services
from aigis_ps.energy_clock import energy_seconds, set_anchor
from aigis_ps.entrust_service import EntrustRules, EntrustService
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_state import SEQUENTIAL_PERIOD, _clock
from aigis_ps.request_ledger import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION
from test_evolution_integration import post
from test_entrust_service import (BONUS_VALUE, CARVED_CRYSTAL, CARVED_TYPE, DAILY_QUEST, HISTORY_PREREQUISITE,
                                  HISTORY_QUEST, MODE_GATE, MONDAY_NOON, TICKET, bonus_values, cleared, disclosed,
                                  eligible_record, hold, possession_counts, treasure_info)
from test_quest_service import bundle, request


PROJECT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
STORY_QUEST = 1001
HISTORY_STEP, DAILY_STEP = 10009, 10002
HISTORY_RANK = 9
# 4125 rolls these prize codes each run; its two 5003 drops carry five carved crystals each.
HISTORY_PRIZES = {85: 1, 126: 1, 5003: 2, 1001654: 1, 58: 2}
# A JST Monday scales the batch total of 30 crystals by the 1.5 of its DropBonusConfig row.
HISTORY_RUNS, HISTORY_CRYSTALS, HISTORY_BONUS_CRYSTALS = 3, 30, 45
PERMANENT, CLAIMED = "permanent", 2
UNSTARTED, ACHIEVED = (0, 0), (1, 1)


def application(bundle, tmp_path, *, rank=1, near_next=False, prepare=None, now=NOW):
    assert bundle.entrust is not None, bundle.entrust_error
    service = EntrustService(bundle.quests, bundle.entrust.catalog, clock=lambda: now, randbelow=lambda _: 0)
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state["tables"]["quest_records"] = [eligible_record(service.catalog.product(STORY_QUEST).quest)]
    wallet = state["tables"]["game_param"][0]
    experience = sum(row["NextExp"] for row in bundle.bootstrap.rank_rows[:rank if near_next else rank - 1])
    wallet.update(Rank=rank, Experience=experience - 1 if near_next else experience, NowCharisma=1000)
    set_anchor(wallet, energy_seconds(now))
    state.pop("rank_rewards", None)
    state = bundle.rank_rewards.initialize(state)
    state = bundle.missions.initialize(state)
    if prepare is not None:
        prepare(state)
    app = create_app(tmp_path, initial_state=state, state_initializer=service.initialize,
        handlers=bundle.bootstrap.handlers() | bundle.player_state.handlers() | bundle.missions.handlers()
            | bundle.presents.handlers() | service.handlers())
    return service, app


def message(store, service, count=3, tick=1000, qid=STORY_QUEST):
    return request(store, 76, (Column("QI", "I", (qid,)), Column("Rev", "S", (service.catalog.resource_version,)),
        Container("Boost", (Column("V", "I", (count,)),))), tick=tick)


def beginner_step(bundle, quest_id, gates, step):
    # Sequential beginner missions only count for the first unclaimed step.
    catalog = bundle.missions.progress.catalog
    earlier = [catalog.by_id[identifier] for identifier in catalog.sequence[:catalog.sequence.index(step)]]
    def prepare(state):
        records = disclosed(state, bundle.quests.catalog, quest_id)["tables"]["quest_records"]
        records.extend([*map(cleared, gates), eligible_record(bundle.entrust.catalog.product(quest_id).quest)])
        hold(state, TICKET, 1)
        journal = state["mission_state"]
        journal["entries"].extend({"condition_id": definition.condition_id, "period_type": SEQUENTIAL_PERIOD,
            "period": PERMANENT, "definition_version": definition.definition_version,
            "goal": definition.clear_count_condition, "progress": definition.clear_count_condition,
            "status": CLAIMED, "read": True, "uids": []} for definition in earlier)
        journal["last_time"] = journal["last_time"] or _clock(NOW)[1]
    return prepare


def step_progress(bundle, state, step):
    row = next(row for row in bundle.missions.progress.snapshot(state, NOW) if row["ID"] == step)
    return row["Progress"], row["Status"]


def test_actual_batch_advances_daily_progress_three_times_without_claiming_rewards(bundle, tmp_path):
    service, app = application(bundle, tmp_path)
    before = app.state.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, reply = post(client, message(app.state.store, service))
        after = app.state.store.profile()
        rows = {row["ID"]: row for row in bundle.missions.progress.snapshot(after.state, NOW)}
        assert [(rows[key]["Progress"], rows[key]["Status"]) for key in (1605, 1606, 1607)] == [(1, 1), (2, 1), (3, 1)]
        assert after.state["tables"]["quest_records"] == before.state["tables"]["quest_records"]
        assert after.state["tables"]["config"] == before.state["tables"]["config"]
        assert after.state["tables"]["crystals"] == before.state["tables"]["crystals"]
        assert "PFC" not in reply
        assert after.revision == before.revision + 1


@pytest.mark.parametrize("quest_id,gates,rank,step", [
    (HISTORY_QUEST, (MODE_GATE, HISTORY_PREREQUISITE), 9, HISTORY_STEP),
    (DAILY_QUEST, (), 5, DAILY_STEP),
])
def test_mode_entrusts_advance_the_beginner_step_of_their_own_quest_type(bundle, tmp_path, quest_id, gates, rank, step):
    service, app = application(bundle, tmp_path, rank=rank, prepare=beginner_step(bundle, quest_id, gates, step))
    before = app.state.store.profile()
    assert step_progress(bundle, before.state, step) == UNSTARTED
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, reply = post(client, message(app.state.store, service, 1, qid=quest_id))
    after = app.state.store.profile()
    assert step_progress(bundle, after.state, step) == ACHIEVED
    assert possession_counts(reply)[TICKET] == 0
    typeless = bundle.missions.record(before.state, "quest_clear",
                                      {"quest_id": quest_id, "quest_type": 0, "stars": 3, "hard": False}, NOW)
    assert step_progress(bundle, typeless, step) == UNSTARTED


def test_a_monday_history_entrust_over_http_credits_the_scaled_crystals_and_returns_bonus(bundle, tmp_path):
    def prepare(state):
        disclosed(state, bundle.quests.catalog, HISTORY_QUEST)
        state["tables"]["quest_records"].extend([*map(cleared, (MODE_GATE, HISTORY_PREREQUISITE)),
                                                 eligible_record(bundle.entrust.catalog.product(HISTORY_QUEST).quest)])
        hold(state, TICKET, HISTORY_RUNS)
    service, app = application(bundle, tmp_path, rank=HISTORY_RANK, prepare=prepare, now=MONDAY_NOON)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, reply = post(client, message(app.state.store, service, HISTORY_RUNS, qid=HISTORY_QUEST))
    saved = app.state.store.profile().state
    held = {row["ItemID"]: row["Count"] for row in saved["tables"]["possessions"]}
    assert held[CARVED_CRYSTAL] == possession_counts(reply)[CARVED_CRYSTAL] == HISTORY_BONUS_CRYSTALS
    assert possession_counts(reply)[TICKET] == 0 and bonus_values(reply) == (CARVED_TYPE, BONUS_VALUE)
    assert treasure_info(reply) == {code: HISTORY_RUNS * count for code, count in HISTORY_PRIZES.items()}
    carved = sum(count * reward.count for code, count in treasure_info(reply).items()
                 if (reward := bundle.quests.catalog.decode_reward(code)).item_id == CARVED_CRYSTAL)
    assert carved == HISTORY_CRYSTALS


def test_native_wire_reopens_and_retries_without_recounting_missions_or_awards(bundle, tmp_path):
    service, app = application(bundle, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(app.state.store, service)
        wire, reply = post(client, envelope)
        saved = app.state.store.profile()
        assert len(reply["PPU"].children) == 21
        assert post(client, envelope)[0] == wire
    _, reopened = application(bundle, tmp_path)
    assert reopened.state.store.profile() == saved
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved
        _, units = post(client, request(reopened.state.store, 41, ()))
        assert len(units) == 21
        assert units["A7"].values == tuple(unit["GetIndex"] for unit in saved.state["player_state"]["units"])
        assert units["A1"].values == tuple(unit["CardID"] for unit in saved.state["player_state"]["units"])


def test_a_saved_watermark_history_reopens_replays_over_the_wire_and_accepts_an_earlier_client_time(bundle, tmp_path):
    service, app = application(bundle, tmp_path)
    envelope = message(app.state.store, service)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, _ = post(client, envelope)
    state = app.state.store.profile().state
    history = state["entrust_history"]
    state["entrust_history"] = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 1000,
        "seen_keys": list(history["processed_keys"]),
        "receipts": [{**receipt, "content": service.legacy_content_version, "client_time": 1000}
                     for receipt in history["receipts"]]}
    with app.state.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    saved = app.state.store.profile()
    _, reopened = application(bundle, tmp_path)
    assert reopened.state.store.profile() == saved
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire and reopened.state.store.profile() == saved
        post(client, message(reopened.state.store, service, 1, tick=999))
    history = reopened.state.store.profile().state["entrust_history"]
    assert history["schema_version"] == SCHEMA_VERSION and len(history["processed_keys"]) == 2


def test_crossing_rank10_delivers_both_real_rank_rewards_once(bundle, tmp_path):
    service, app = application(bundle, tmp_path, rank=9, near_next=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        envelope = message(app.state.store, service, 1)
        _, reply = post(client, envelope)
        after = app.state.store.profile()
        assert after.state["tables"]["game_param"][0]["Rank"] >= 10
        entries = after.state["rank_rewards"]["entries"]
        assert entries["7"]["status"] == entries["78"]["status"] == "delivered"
        awarded = {identifier for key in ("7", "78") for identifier in entries[key]["mail_ids"]}
        assert awarded and {row["PresentID"] for row in bundle.presents.rows(after.state)} >= awarded
        assert "PPT" in reply
        assert post(client, envelope)[1]["PPT"] == reply["PPT"]
        assert app.state.store.profile() == after
        assert after.state["tables"]["quest_records"] == before.state["tables"]["quest_records"]


def test_existing_receipts_require_entrust_dependency_without_changing_save(bundle, tmp_path):
    service, app = application(bundle, tmp_path)
    service.execute(message(app.state.store, service), app.state.store)
    saved = app.state.store.profile().state
    original = deepcopy(saved)
    with pytest.raises(ProtocolError) as caught:
        replace(bundle, entrust=None).initialize(saved)
    assert caught.value.code == "entrust_unavailable"
    assert saved == original


def test_real_mission_failure_rolls_back_all_batch_effects(bundle, tmp_path, monkeypatch):
    service, app = application(bundle, tmp_path)
    before = app.state.store.profile()
    original = service.missions.record
    calls = []
    def failure(state, kind, payload, now):
        calls.append(1)
        if len(calls) == 2:
            raise ProtocolError("mission_failure", "fixture", 503)
        return original(state, kind, payload, now)
    monkeypatch.setattr(service.missions, "record", failure)
    with pytest.raises(ProtocolError) as caught:
        service.execute(message(app.state.store, service), app.state.store)
    assert caught.value.code == "mission_failure" and len(calls) == 2
    assert app.state.store.profile() == before


def test_rank_resource_changes_reject_cached_and_new_requests(bundle, tmp_path, monkeypatch):
    service, app = application(bundle, tmp_path)
    envelope = message(app.state.store, service)
    service.execute(envelope, app.state.store)
    saved = app.state.store.profile()
    ranks = deepcopy(service.bootstrap.rank_rows)
    ranks[0]["CharismaMax"] += 1
    monkeypatch.setattr(service.bootstrap, "rank_rows", ranks)
    for candidate in (envelope, message(app.state.store, service, tick=1001)):
        with pytest.raises(ProtocolError) as caught:
            service.execute(candidate, app.state.store)
        assert caught.value.code == "entrust_dependencies_changed"
        assert app.state.store.profile() == saved


def test_service_builder_applies_the_entrust_rules_file(bundle, tmp_path):
    path = tmp_path / "entrust.json"
    path.write_text('{"experience_up": false}', encoding="utf-8")
    updated = services(Namespace(asset_dir=PROJECT.parent / "aigisr_exe", data_dir=PROJECT / "runtime/server",
                                 resource_dir=PROJECT / "runtime/resources", rules=None, entrust_rules=path))
    assert updated.entrust is not None, updated.entrust_error
    assert (updated.entrust.rules, bundle.entrust.rules) == (EntrustRules(experience_up=False), EntrustRules())
    assert updated.entrust.status()["local_rules"]["entrust"] == {"experience_up": False}
    assert updated.entrust.content_version != bundle.entrust.content_version
    assert EntrustService(updated.quests, updated.entrust.catalog).content_version == bundle.entrust.content_version

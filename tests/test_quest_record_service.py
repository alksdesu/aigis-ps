from copy import deepcopy
from dataclasses import replace
import hashlib
import logging
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, ValueType, loads
from aigis_ps.app import create_app
from aigis_ps.assets import asset_key
from aigis_ps.bootstrap import UNPLAYED_QUEST_RECORD, BootstrapService
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MIN, signed32
from aigis_ps.peruse_state import QUEST_EVENT_RESOURCE, peruse_quest_ids
from aigis_ps.quest_mode_goddess import GODDESS_TYPE
from aigis_ps.quest_modes import CLEAR_FLAG, SLOT_FLAGS, THREE_STAR_FLAG
from aigis_ps.quest_record_service import (
    EPISODE_FLAG, OPERATIONS, PERUSE_QUESTS, REPRODUCE_MISSIONS, SAVE_SHOWN, UNPLAYED, WRITE_ATTEMPTS, QuestRecordService,
)
from aigis_ps.storage import Store
from test_bootstrap import asset_root, invoke, quest_master
from test_evolution_integration import post
from test_peruse_state import archive, command, home_event
from test_quest_service import bundle, finish_request, setup, start_request
from test_unit_actions import request


STORY, HISTORY, DEVIL = 1001, 4290, 4386
GODDESS = (16001, 16002, 16101)
QUEST_TYPES = {STORY: 0, 1002: 0, HISTORY: 16, DEVIL: 9, **dict.fromkeys(GODDESS, GODDESS_TYPE)}
# Old event packages still confirm quests the current QuestTable no longer lists.
RETIRED = 4183
PERUSED = (STORY, HISTORY, DEVIL, RETIRED)
MISSION_COLUMNS = [("MissionID", ValueType.INT), ("TitleID", ValueType.INT), ("QuestID", ValueType.STRING),
                   ("StartDateTime", ValueType.STRING), ("EndDateTime", ValueType.STRING),
                   ("FaceIconID", ValueType.STRING)]
RECORD_COLUMNS = ("QuestID", "A1", "A2", "A3", "A4", "Shown")
FIRST_DEFEAT = 739000
REAL_DEVIL, REAL_GODDESS = 4497, (16001, 16002)


def quest_event(quest_id):
    return home_event(command("PlayBgm", "BgmID=BgmEvent"), command("DoPeruseEvent", _Param1=str(quest_id)))


def event_reader(quest_ids=PERUSED):
    packages = {f"_ev{quest_id:06d}.aar": quest_event(quest_id) for quest_id in quest_ids}
    return {QUEST_EVENT_RESOURCE: archive(packages)}.__getitem__


def catalog(types=QUEST_TYPES):
    return SimpleNamespace(quest_type=types.get)


@pytest.fixture
def bootstrap(asset_root):
    return BootstrapService(asset_root, master_provider=lambda name: quest_master(tuple(QUEST_TYPES)))


@pytest.fixture
def records(bootstrap):
    return QuestRecordService(bootstrap, catalog(), asset_reader=event_reader())


@pytest.fixture
def store(tmp_path, bootstrap):
    return Store(tmp_path / "records.sqlite3", bootstrap.initial_state())


def quests_request(store, number, quest_ids):
    return request(store, number, (Column("QI", "I", tuple(quest_ids)),) if quest_ids else ())


def record(quest_id, **changes):
    return {"QuestID": quest_id, "MaxRank": 0, "AchieveFlag": 0, "RemainAssault": 0, "FirstDefeatDate": 0,
            "Shown": False, **changes}


def save_records(store, *rows, update=None):
    profile = store.profile()
    state = deepcopy(profile.state)
    state["tables"]["quest_records"].extend(rows)
    (update or store.update_state)(state, profile.revision)


def saved(store):
    return {row["QuestID"]: row for row in store.profile().state["tables"]["quest_records"]}


def groups(response):
    return {node.name: node for node in loads(response).children}


def foi6moes(bootstrap, store):
    columns = {column.name: column.values for column in invoke(bootstrap, store, "quest_record").children}
    return {quest_id: dict(zip(RECORD_COLUMNS[1:], values))
            for quest_id, *values in zip(*(columns[name] for name in RECORD_COLUMNS))}


def refused(call, store, code, status):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        call()
    assert (error.value.code, error.value.status) == (code, status)
    assert store.profile() == before


def messages(caplog):
    return [entry.getMessage() for entry in caplog.records if entry.name == "aigis_ps"]


def test_reproduce_missions_answer_zero_daily_missions_without_a_write(records, store):
    before = store.profile()
    tree = groups(records.reproduce_missions(request(store, REPRODUCE_MISSIONS, ()), store))
    assert list(tree) == ["SYSTEM", "Mission"]
    assert tree["SYSTEM"] == records.bootstrap.system_container(before)
    assert [(column.name, column.type) for column in tree["Mission"].children] == MISSION_COLUMNS
    assert all(column.values == () for column in tree["Mission"].children)
    assert store.profile() == before


@pytest.mark.parametrize("field", [Column("QI", "I", (STORY,)), Column("MissionID", "I", (1,))])
def test_reproduce_missions_take_only_the_common_header(records, store, field):
    refused(lambda: records.reproduce_missions(request(store, REPRODUCE_MISSIONS, (field,)), store), store,
            "invalid_request_fields", 400)


@pytest.mark.parametrize("flags", [0, CLEAR_FLAG | THREE_STAR_FLAG | SLOT_FLAGS[0] | 1, INT32_MIN, -1 & ~EPISODE_FLAG])
def test_peruse_sets_only_the_episode_bit_and_repeats_without_a_write(records, store, flags):
    save_records(store, record(HISTORY, MaxRank=3, AchieveFlag=flags, RemainAssault=2, FirstDefeatDate=FIRST_DEFEAT,
                               Shown=True), record(DEVIL, AchieveFlag=CLEAR_FLAG))
    before = store.profile()
    envelope = quests_request(store, PERUSE_QUESTS, (HISTORY,))
    response = records.peruse_quests(envelope, store)
    after = store.profile()
    assert list(groups(response)) == ["SYSTEM"]
    assert after.revision == before.revision + 1
    assert saved(store) == {HISTORY: record(HISTORY, MaxRank=3, AchieveFlag=signed32(flags | EPISODE_FLAG),
                                            RemainAssault=2, FirstDefeatDate=FIRST_DEFEAT, Shown=True),
                            DEVIL: record(DEVIL, AchieveFlag=CLEAR_FLAG)}
    assert records.peruse_quests(envelope, store) == response and store.profile() == after


def test_peruse_of_a_record_holding_the_bit_writes_nothing(records, store):
    save_records(store, record(HISTORY, AchieveFlag=EPISODE_FLAG | CLEAR_FLAG))
    before = store.profile()
    assert list(groups(records.peruse_quests(quests_request(store, PERUSE_QUESTS, (HISTORY,)), store))) == ["SYSTEM"]
    assert store.profile() == before


def test_peruse_starts_missing_records_unplayed_after_the_saved_ones(records, store):
    save_records(store, record(STORY, MaxRank=3, AchieveFlag=CLEAR_FLAG))
    records.peruse_quests(quests_request(store, PERUSE_QUESTS, (DEVIL, HISTORY, DEVIL)), store)
    assert list(saved(store).values()) == [record(STORY, MaxRank=3, AchieveFlag=CLEAR_FLAG),
                                           record(DEVIL, AchieveFlag=EPISODE_FLAG),
                                           record(HISTORY, AchieveFlag=EPISODE_FLAG)]


def test_missing_records_start_from_the_row_the_collection_route_seeds(bootstrap, store):
    assert UNPLAYED is UNPLAYED_QUEST_RECORD
    foi6moes(bootstrap, store)
    assert list(saved(store).values()) == [record(quest_id) for quest_id in QUEST_TYPES]


def test_foi6moes_reads_the_episode_bit_back(records, bootstrap, store):
    records.peruse_quests(quests_request(store, PERUSE_QUESTS, (HISTORY,)), store)
    flags = {quest_id: row["A2"] for quest_id, row in foi6moes(bootstrap, store).items()}
    assert flags == {**dict.fromkeys(QUEST_TYPES, 0), HISTORY: EPISODE_FLAG}


def test_peruse_of_retired_quests_answers_system_and_records_the_listed_ones(records, store):
    before = store.profile()
    assert list(groups(records.peruse_quests(quests_request(store, PERUSE_QUESTS, (RETIRED,)), store))) == ["SYSTEM"]
    assert store.profile() == before
    records.peruse_quests(quests_request(store, PERUSE_QUESTS, (RETIRED, HISTORY)), store)
    assert saved(store) == {HISTORY: record(HISTORY, AchieveFlag=EPISODE_FLAG)}
    assert store.profile().revision == before.revision + 1


@pytest.mark.parametrize("quest_ids", [(9999,), (HISTORY, 9999), (GODDESS[0],), (0,), (-1,), (HISTORY, 1002)])
def test_peruse_refuses_the_whole_request_for_a_quest_no_event_confirms(records, store, caplog, quest_ids):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    refused(lambda: records.peruse_quests(quests_request(store, PERUSE_QUESTS, quest_ids), store), store,
            "peruse_quest_unknown", 404)
    unknown = [quest_id for quest_id in quest_ids if quest_id not in PERUSED]
    assert messages(caplog) == [
        f"Request 84 refused quest events {unknown}: no native event confirms them through DoPeruseEvent"]


def test_peruse_requires_quest_ids(records, store):
    refused(lambda: records.peruse_quests(request(store, PERUSE_QUESTS, ()), store), store,
            "invalid_request_fields", 400)


def test_unavailable_event_resources_refuse_only_peruse(bootstrap, store, caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    service = QuestRecordService(bootstrap, catalog(), asset_reader={}.__getitem__)
    status = service.status()
    assert status["ready"] is False
    assert status["perusable_quests"] == {"ready": False, "error": "peruse_quest_catalog_invalid"}
    refused(lambda: service.peruse_quests(quests_request(store, PERUSE_QUESTS, (HISTORY,)), store), store,
            "peruse_quest_catalog_unavailable", 503)
    assert messages(caplog) == [f"Request 84 refused quest events {[HISTORY]}: quest event resources are "
                                "unavailable (peruse_quest_catalog_invalid)"]
    missions = service.reproduce_missions(request(store, REPRODUCE_MISSIONS, ()), store)
    assert list(groups(missions)) == ["SYSTEM", "Mission"]
    service.save_shown(quests_request(store, SAVE_SHOWN, (GODDESS[0],)), store)
    assert saved(store) == {GODDESS[0]: record(GODDESS[0], Shown=True)}


def test_event_resources_default_to_the_asset_root(bootstrap, asset_root):
    assert QuestRecordService(bootstrap, catalog()).status()["perusable_quests"] == {
        "ready": False, "error": "peruse_quest_catalog_invalid"}
    (asset_root / asset_key(QUEST_EVENT_RESOURCE)).write_bytes(event_reader()(QUEST_EVENT_RESOURCE))
    assert QuestRecordService(bootstrap, catalog()).status()["perusable_quests"]["quest_count"] == len(PERUSED)


def test_shown_marks_goddess_quests_and_answers_their_full_rows(records, store):
    flags = CLEAR_FLAG | SLOT_FLAGS[0]
    save_records(store, record(GODDESS[0], MaxRank=2, AchieveFlag=flags, FirstDefeatDate=FIRST_DEFEAT),
                 record(STORY, MaxRank=3))
    before = store.profile()
    envelope = quests_request(store, SAVE_SHOWN, (GODDESS[1], GODDESS[0], GODDESS[1]))
    response = records.save_shown(envelope, store)
    tree = groups(response)
    assert list(tree) == ["SYSTEM", "QR"]
    assert [(column.name, column.values) for column in tree["QR"].children] == [
        ("QuestID", (GODDESS[1], GODDESS[0])), ("A1", (0, 2)), ("A2", (0, flags)), ("A3", (0, 0)),
        ("A4", (0, FIRST_DEFEAT)), ("Shown", (True, True))]
    after = store.profile()
    assert after.revision == before.revision + 1
    assert saved(store) == {GODDESS[0]: record(GODDESS[0], MaxRank=2, AchieveFlag=flags, FirstDefeatDate=FIRST_DEFEAT,
                                               Shown=True),
                            STORY: record(STORY, MaxRank=3), GODDESS[1]: record(GODDESS[1], Shown=True)}
    assert records.save_shown(envelope, store) == response and store.profile() == after


@pytest.mark.parametrize("quest_ids", [(STORY,), (GODDESS[0], HISTORY), (GODDESS[0], 9999), (RETIRED,)])
def test_shown_refuses_the_whole_request_for_other_quests(records, store, caplog, quest_ids):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    refused(lambda: records.save_shown(quests_request(store, SAVE_SHOWN, quest_ids), store), store,
            "quest_not_found", 404)
    others = [quest_id for quest_id in quest_ids if quest_id not in GODDESS]
    assert messages(caplog) == [f"Request 157 refused quests {others}: they are not goddess revival quests"]


def test_shown_without_quests_answers_empty_records(records, store):
    before = store.profile()
    tree = groups(records.save_shown(request(store, SAVE_SHOWN, ()), store))
    assert [(column.name, column.values) for column in tree["QR"].children] == [(name, ()) for name in RECORD_COLUMNS]
    assert store.profile() == before


def test_foi6moes_reads_shown_back(records, bootstrap, store):
    records.save_shown(quests_request(store, SAVE_SHOWN, GODDESS[:2]), store)
    shown = {quest_id: row["Shown"] for quest_id, row in foi6moes(bootstrap, store).items()}
    assert shown == {**dict.fromkeys(QUEST_TYPES, False), GODDESS[0]: True, GODDESS[1]: True}


@pytest.mark.parametrize("number", [PERUSE_QUESTS, SAVE_SHOWN])
@pytest.mark.parametrize("mutation", ["duplicate", "wrong_type", "empty", "bad_integer", "overflow", "nested",
                                      "unknown", "mixed_text", "missing_header", "wrong_profile"])
def test_malformed_requests_never_write(records, store, number, mutation):
    envelope = quests_request(store, number, (HISTORY if number == PERUSE_QUESTS else GODDESS[0],))
    root, field = envelope.document, envelope.document.find("QI")
    if mutation == "duplicate":
        root.append(deepcopy(field))
    elif mutation == "wrong_type":
        field.set("T", "S")
    elif mutation == "empty":
        field.remove(field[0])
    elif mutation == "bad_integer":
        field[0].text = "4290x"
    elif mutation == "overflow":
        field[0].text = "2147483648"
    elif mutation == "nested":
        ET.SubElement(field[0], "V").text = "1"
    elif mutation == "unknown":
        ET.SubElement(root, "MissionID", T="I")
    elif mutation == "mixed_text":
        field.tail = "junk"
    elif mutation == "missing_header":
        root.remove(root.find("UI"))
    else:
        envelope = replace(envelope, profile=replace(envelope.profile, registration_keys=(76,)))
    refused(lambda: records.handlers()[number](envelope, store), store, "invalid_request_fields", 400)


@pytest.mark.parametrize("field,code", [("UI", "invalid_identity"), ("UK", "invalid_identity"),
                                        ("MK", "invalid_method_key")])
@pytest.mark.parametrize("number,quest_ids", [(PERUSE_QUESTS, (9999,)), (REPRODUCE_MISSIONS, ()),
                                              (SAVE_SHOWN, (STORY,))])
def test_authentication_precedes_the_quest_checks(records, store, field, code, number, quest_ids):
    envelope = quests_request(store, number, quest_ids)
    envelope.document.find(field + "/V").text = "other"
    refused(lambda: records.handlers()[number](envelope, store), store, code, 403)


@pytest.mark.parametrize("number,quest_id", [(PERUSE_QUESTS, HISTORY), (SAVE_SHOWN, GODDESS[0])])
def test_response_capacity_is_checked_before_a_write(records, store, number, quest_id):
    envelope = quests_request(store, number, (quest_id,))
    envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    refused(lambda: records.handlers()[number](envelope, store), store, "response_too_large", 500)


def test_duplicated_saved_records_are_refused_without_a_write(records, store):
    save_records(store, record(HISTORY), record(HISTORY, MaxRank=1))
    refused(lambda: records.peruse_quests(quests_request(store, PERUSE_QUESTS, (HISTORY,)), store), store,
            "quest_state_invalid", 503)


@pytest.mark.parametrize("number,quest_id,expected", [
    (PERUSE_QUESTS, HISTORY, record(HISTORY, MaxRank=3, AchieveFlag=CLEAR_FLAG | EPISODE_FLAG)),
    (SAVE_SHOWN, GODDESS[0], record(GODDESS[0], MaxRank=3, AchieveFlag=CLEAR_FLAG, Shown=True)),
])
def test_retry_keeps_a_concurrent_change_of_the_same_record(records, store, monkeypatch, number, quest_id, expected):
    original, calls = store.update_state, []

    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            save_records(store, record(quest_id, MaxRank=3, AchieveFlag=CLEAR_FLAG), update=original)
        return original(state, revision)

    monkeypatch.setattr(store, "update_state", update)
    records.handlers()[number](quests_request(store, number, (quest_id,)), store)
    assert len(calls) == 2
    assert saved(store) == {quest_id: expected}


def test_retry_that_finds_the_bit_set_writes_nothing_more(records, store, monkeypatch):
    original, calls = store.update_state, []

    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            save_records(store, record(HISTORY, AchieveFlag=EPISODE_FLAG), update=original)
        return original(state, revision)

    monkeypatch.setattr(store, "update_state", update)
    assert list(groups(records.peruse_quests(quests_request(store, PERUSE_QUESTS, (HISTORY,)), store))) == ["SYSTEM"]
    assert len(calls) == 1
    assert saved(store) == {HISTORY: record(HISTORY, AchieveFlag=EPISODE_FLAG)}


@pytest.mark.parametrize("number,quest_id", [(PERUSE_QUESTS, HISTORY), (SAVE_SHOWN, GODDESS[0])])
def test_exhausted_retries_keep_the_save(records, store, monkeypatch, number, quest_id):
    calls = []

    def fail(state, revision):
        calls.append(revision)
        raise ValueError("revision changed")

    monkeypatch.setattr(store, "update_state", fail)
    refused(lambda: records.handlers()[number](quests_request(store, number, (quest_id,)), store), store,
            "profile_changed", 409)
    assert len(calls) == WRITE_ATTEMPTS


def test_status_reports_the_operations_and_the_perusable_quests(records):
    status = records.status()
    digest = hashlib.sha256(",".join(map(str, sorted(PERUSED))).encode("ascii")).hexdigest()
    assert status["ready"] is True and status["state_writes"] is True
    assert status["implemented_operations"] == list(OPERATIONS) == [PERUSE_QUESTS, REPRODUCE_MISSIONS, SAVE_SHOWN]
    assert status["perusable_quests"] == {"ready": True, "quest_count": len(PERUSED), "quest_digest": digest}
    assert set(records.handlers()) == set(OPERATIONS)


def test_native_http_requests_route_to_the_record_service_and_survive_reopening(records, bootstrap, tmp_path):
    def application():
        return create_app(tmp_path / "records-http", initial_state=bootstrap.initial_state(),
                          handlers=bootstrap.handlers() | records.handlers())

    app = application()
    store = app.state.store
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert list(post(client, request(store, REPRODUCE_MISSIONS, ()))[1]) == ["SYSTEM", "Mission"]
        assert list(post(client, quests_request(store, PERUSE_QUESTS, (HISTORY,)))[1]) == ["SYSTEM"]
        assert post(client, quests_request(store, SAVE_SHOWN, (GODDESS[0],)))[1]["QR"].children[-1].values == (True,)
        refusal = post(client, quests_request(store, PERUSE_QUESTS, (9999,)), 404)
        assert refusal.json()["error"]["code"] == "peruse_quest_unknown"
    written = app.state.store.profile()
    reopened = application().state.store
    assert reopened.profile() == written
    assert saved(reopened) == {HISTORY: record(HISTORY, AchieveFlag=EPISODE_FLAG),
                               GODDESS[0]: record(GODDESS[0], Shown=True)}
    assert foi6moes(bootstrap, reopened)[HISTORY]["A2"] == EPISODE_FLAG


@pytest.fixture
def battle(setup, bundle):
    service, battle_store = setup
    records = QuestRecordService(bundle.bootstrap, bundle.quests.catalog, asset_reader=event_reader((STORY,)))
    return service, records, battle_store


@pytest.mark.parametrize("interrupted", ["result", "episode"])
def test_a_result_and_an_episode_confirmation_racing_both_land_in_the_record(battle, monkeypatch, interrupted):
    service, records, battle_store = battle
    service.start(start_request(service, battle_store), battle_store)
    settle = lambda: service.finish(finish_request(battle_store), battle_store)
    peruse = lambda: records.peruse_quests(quests_request(battle_store, PERUSE_QUESTS, (STORY,)), battle_store)
    first, racing = (settle, peruse) if interrupted == "result" else (peruse, settle)
    original, calls = battle_store.update_state, []

    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            racing()
        return original(state, revision)

    monkeypatch.setattr(battle_store, "update_state", update)
    first()
    row = next(row for row in battle_store.profile().state["tables"]["quest_records"] if row["QuestID"] == STORY)
    assert row["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG | EPISODE_FLAG and row["MaxRank"] == 3
    assert len(calls) == 3
    journal = battle_store.profile().state["quest_state"]
    assert journal["active"] is None and journal["history"][-1]["kind"] == "success"


def test_the_served_catalog_and_event_archive_decide_the_recorded_quests(setup, bundle):
    _, battle_store = setup
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    records = QuestRecordService(bundle.bootstrap, bundle.quests.catalog, asset_reader=reader)
    assert records.status()["perusable_quests"]["quest_count"] == len(peruse_quest_ids(reader))
    before = battle_store.profile()
    records.peruse_quests(quests_request(battle_store, PERUSE_QUESTS, (RETIRED,)), battle_store)
    assert battle_store.profile() == before
    records.peruse_quests(quests_request(battle_store, PERUSE_QUESTS, (REAL_DEVIL,)), battle_store)
    records.save_shown(quests_request(battle_store, SAVE_SHOWN, REAL_GODDESS), battle_store)
    refused(lambda: records.peruse_quests(quests_request(battle_store, PERUSE_QUESTS, (STORY,)), battle_store),
            battle_store, "peruse_quest_unknown", 404)
    refused(lambda: records.save_shown(quests_request(battle_store, SAVE_SHOWN, (REAL_DEVIL,)), battle_store),
            battle_store, "quest_not_found", 404)
    rows = foi6moes(bundle.bootstrap, battle_store)
    assert rows[REAL_DEVIL]["A2"] == EPISODE_FLAG and not rows[REAL_DEVIL]["Shown"]
    assert all(rows[quest_id]["Shown"] and rows[quest_id]["A2"] == 0 for quest_id in REAL_GODDESS)
    assert sum(row["A2"] == EPISODE_FLAG for row in rows.values()) == 1

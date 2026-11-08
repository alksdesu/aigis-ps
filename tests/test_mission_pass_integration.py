from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
import json
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_catalog import EventPassCatalog
from aigis_ps.event_pass_revival import EventPassRevivalRules, build_event_pass_revival, PASS_RESOURCE, MISSION_RESOURCE
from aigis_ps.event_pass_service import EventPassService
from aigis_ps.mission_catalog import MissionCatalog
from aigis_ps.mission_ep_bindings import MissionEpBindings
from aigis_ps.mission_pass_points import MissionPassPoints
import aigis_ps.mission_service as module
from aigis_ps.mission_service import MissionService
from aigis_ps.mission_state import MissionProgress
from aigis_ps.presents import MAX_PRESENTS
from test_event_pass_integration import NOW, post, rows
from test_event_pass_revival import resources, PASS_ID, POINTS
from test_quest_service import bundle, request
from test_tower_service import without_enabled


@pytest.fixture
def factory(bundle, resources, tmp_path):
    serial = 0
    payloads, version = resources

    def build(*, count=0, ready="login", expired=False, legacy=None, helper=True):
        nonlocal serial
        serial += 1
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        bootstrap.clock = lambda: NOW
        presents.bootstrap = bootstrap
        rules = EventPassRevivalRules(PASS_ID, POINTS,
            end_at="2026/11/01 11:00:00" if expired else None)
        overlay = build_event_pass_revival(payloads[PASS_RESOURCE], payloads[MISSION_RESOURCE], rules)
        effective = payloads | dict(overlay.replacements)
        base_catalog = MissionCatalog(payloads.__getitem__, resource_version=version)
        catalog = MissionCatalog(effective.__getitem__, resource_version=version)
        bindings = MissionEpBindings(base_catalog, catalog)
        pass_catalog = EventPassCatalog(effective.__getitem__, bundle.possessions)
        passes = EventPassService(bootstrap, bundle.player_state, presents, bundle.possessions, pass_catalog)
        points = MissionPassPoints(passes)
        service = MissionService(bootstrap, bundle.player_state, presents, catalog,
                                 ep_bindings=bindings, pass_points=points if helper else None)
        bootstrap.present_provider, bootstrap.event_pass_provider = presents.response, passes.response
        state = bundle.player_state.initialize(bootstrap.initial_state())
        if count:
            state["tables"]["possessions"].append({"ItemID": PASS_ID, "Count": count, "Enabled": 1})
        if legacy is not None:
            original_progress = MissionProgress(base_catalog)
            state = original_progress.record(state, "login", {}, NOW)
            if legacy == "claimed":
                state = original_progress.claim(state, [1611], NOW)
        state = service.initialize(state)
        if ready is not None:
            state = service.record(state, "login", {}, NOW)
        if ready == "all":
            state, units = bundle.player_state.credit_units(state, [{"card_id": 7, "level": 1, "count": 1}])
            before, after = deepcopy(units[0]), deepcopy(units[0])
            after["Love"] = 1
            state["player_state"]["units"][-1] = after
            state = service.record(state, "gift", {"before": before, "after": after}, NOW)
            before, after = deepcopy(after), deepcopy(after)
            after["Exp"] = bundle.player_state.experience_for_level(7, 2)
            state["player_state"]["units"][-1] = after
            state = service.record(state, "training", {"before": before, "after": after, "level": 2}, NOW)
            for _ in range(3):
                state = service.record(state, "quest_clear", {"quest_id": 1001, "quest_type": 0,
                                      "stars": 3, "hard": False}, NOW)
        state["integration_sentinel"] = {"keep": [1, 2]}
        directory = tmp_path / str(serial)
        handlers = (bootstrap.handlers() | bundle.player_state.handlers() | presents.handlers()
                    | service.handlers() | passes.handlers())
        def application():
            return create_app(directory, initial_state=state, state_initializer=service.initialize, handlers=handlers)
        app = application()
        return SimpleNamespace(bundle=bundle, bootstrap=bootstrap, presents=presents, catalog=catalog,
            base_catalog=base_catalog, bindings=bindings, points=points, passes=passes, service=service,
            state=state, app=app, store=app.state.store, application=application)
    return build


def message(case, operation=58, identifier=1611, tick=1000):
    scope = (("ID", identifier if operation == 58 else 0), ("PT", 1), ("TabGroupId", 1), ("TaskGroupId", 0))
    return request(case.store, operation, (Container("GL", tuple(Column(name, "I", (value,)) for name, value in scope)),), tick=tick)


def balance(case):
    return next((row["Count"] for row in case.store.profile().state["tables"]["possessions"]
                 if row["ItemID"] == PASS_ID), 0)


def reject(case, client, envelope, status, code=None):
    before = case.store.profile()
    response = post(client, envelope, status)
    if code is not None:
        assert response.json()["error"]["code"] == code
    assert case.store.profile() == before


def unchanged(case, client, envelope):
    before = case.store.profile()
    _, response = post(client, envelope)
    assert set(response) == {"SYSTEM", "GLRW"} and response["GLRW"].children == (Column("Value", "I", ()),)
    assert case.store.profile() == before


def overwrite(case, state):
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def test_real_daily_single_claim_credits_only_after_claim_and_replays_after_reopen(factory):
    case = factory(count=3)
    original = case.store.profile()
    assert case.catalog.by_id[1611].event_pass_point == 2
    assert balance(case) == 3
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, request(case.store, 54, ()))
        assert case.store.profile() == original
        _, read = post(client, message(case, 60))
        assert "POSSESSION" not in read and balance(case) == 3
        assert "mission_pass_delivery" not in case.store.profile().state
        envelope = message(case, tick=1001)
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "GLRW", "PPT", "POSSESSION"}
        assert rows(response["POSSESSION"]) == [{"ItemID": PASS_ID, "Count": 5, "Enabled": 1}]
        saved = case.store.profile()
        audit = [{"pass_id": PASS_ID, "requested": 2, "credited": 2, "before": 3,
                  "after": 5, "capped": 0, "inactive": 0}]
        assert saved.state["mission_history"]["receipts"][-1]["event_pass_points"] == audit
        assert saved.state["mission_pass_delivery"]["audit"] == audit
        assert len(case.presents.rows(saved.state)) == 1
        assert saved.state.get("event_pass_state") == original.state.get("event_pass_state")
        assert saved.state["tables"]["event_pass_status"] == []
        assert post(client, envelope)[0] == wire and case.store.profile() == saved
        unchanged(case, client, message(case, tick=1002))
        _, loaded = post(client, request(case.store, 137, ()))
        assert next(row for row in rows(loaded["POSSESSION"]) if row["ItemID"] == PASS_ID)["Count"] == 5
    reopened = case.application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
    assert reopened.state.store.profile() == saved


def test_real_seven_daily_group_rewards_credit_one_aggregated_pass(factory):
    case = factory(ready="all")
    before = case.store.profile()
    assert balance(case) == 0
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, response = post(client, message(case, 59))
    saved = case.store.profile()
    assert set(response["GLRW"].children[0].values) == set(range(1605, 1612))
    assert rows(response["POSSESSION"]) == [{"ItemID": PASS_ID, "Count": sum(POINTS.values()), "Enabled": 0}]
    assert len(case.presents.rows(saved.state)) == 7
    assert len(saved.state["mission_pass_delivery"]["audit"]) == 1
    assert saved.state["player_state"] == before.state["player_state"]
    assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert saved.state["tables"]["event_pass_status"] == []


def test_daily_ep_claims_qualified_normal_pass_rewards_once_without_consuming_points(factory):
    case = factory(ready="all")
    entry = case.passes.catalog.get(PASS_ID)
    expected = tuple(reward for reward in entry.rewards
                     if not reward.premium and reward.required_point <= sum(POINTS.values()))
    assert expected
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, message(case, 59))
        earned = case.store.profile()
        assert balance(case) == 20
        assert len(case.presents.rows(earned.state)) == 7
        assert all(row["ItemID"] != entry.premium_item_id
                   for row in earned.state["tables"]["possessions"])
        envelope = request(case.store, 159, (Column("EPI", "S", (PASS_ID,)),), tick=1001)
        wire, response = post(client, envelope)
        saved = case.store.profile()
        status = [{"ID": PASS_ID, "ReceivedNormalPoint": max(row.required_point for row in expected),
                   "ReceivedPremiumPoint": 0}]
        assert rows(response["EventPassStatus"]) == status
        assert saved.state["tables"]["possessions"] == earned.state["tables"]["possessions"]
        new_mail = rows(response["PPT"])
        assert new_mail and len(case.presents.rows(saved.state)) == 7 + len(new_mail)
        delivered = saved.state["event_pass_state"]["passes"][PASS_ID]["deliveries"]
        assert {reward for track in delivered.values() for group in track.values()
                for reward in group["reward_ids"]} == {row.reward_id for row in expected}
        assert post(client, envelope)[0] == wire and case.store.profile() == saved
        _, loaded = post(client, request(case.store, 137, ()))
        assert rows(loaded["EventPassStatus"]) == status
        assert next(row["Count"] for row in rows(loaded["POSSESSION"])
                    if row["ItemID"] == PASS_ID) == 20
        assert case.store.profile() == saved


@pytest.mark.parametrize("count,expired,credited,capped,inactive", [(998, False, 1, 1, 0),
    (999, False, 0, 2, 0), (1001, False, 0, 2, 0), (0, True, 0, 0, 2)])
def test_saturation_and_closed_window_keep_base_reward_without_ep_debt(factory, count, expired, credited, capped, inactive):
    case = factory(count=count, expired=expired)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        envelope = message(case)
        wire, response = post(client, envelope)
        saved = case.store.profile()
        assert len(case.presents.rows(saved.state)) == 1
        assert balance(case) == count + credited
        assert ("POSSESSION" in response) == (credited > 0)
        assert saved.state["mission_pass_delivery"]["audit"] == [{"pass_id": PASS_ID,
            "requested": 2, "credited": credited, "before": count, "after": count + credited,
            "capped": capped, "inactive": inactive}]
        assert post(client, envelope)[0] == wire and case.store.profile() == saved


@pytest.mark.parametrize("legacy", ["ready", "claimed"])
def test_overlay_migrates_ready_history_but_never_backfills_claimed_rewards(factory, legacy):
    case = factory(ready=None, legacy=legacy)
    original = case.store.profile()
    history = next(row for row in original.state["mission_state"]["entries"] if row["condition_id"] == 1611)
    expected = case.base_catalog if legacy == "claimed" else case.catalog
    assert history["definition_version"] == expected.by_id[1611].definition_version
    assert balance(case) == 0
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        if legacy == "claimed":
            unchanged(case, client, message(case))
            assert case.presents.rows(case.store.profile().state) == []
        else:
            post(client, message(case))
            assert balance(case) == 2


@pytest.mark.parametrize("failure", ["mail", "credit", "137", "response"])
def test_mail_ep_and_mission_state_rollback_together(factory, monkeypatch, failure):
    case = factory()
    if failure == "mail":
        monkeypatch.setattr(case.presents, "enqueue", lambda *_: (_ for _ in ()).throw(
            ProtocolError("injected_mission_ep", "Injected mail failure", 503)))
    elif failure == "credit":
        monkeypatch.setattr(case.points.possessions, "credit", lambda *_: (_ for _ in ()).throw(
            ProtocolError("injected_mission_ep", "Injected inventory failure", 503)))
    else:
        original = module.encode_response
        target = 137 if failure == "137" else 58
        def encode(profile, payload):
            if target in profile.registration_keys:
                raise ProtocolError("injected_mission_ep", "Injected encoding failure", 503)
            return original(profile, payload)
        monkeypatch.setattr(module, "encode_response", encode)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject(case, client, message(case), 503, "injected_mission_ep")


def test_full_mailbox_does_not_claim_task_or_grant_ep(factory):
    case = factory()
    before = case.store.profile()
    crowded, _ = case.presents.enqueue(before.state,
        [{"kind": "gold", "amount": 1, "description": "capacity"}] * MAX_PRESENTS)
    case.store.update_state(crowded, before.revision)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject(case, client, message(case), 409, "present_capacity")


def test_missing_ep_helper_does_not_turn_claim_into_silent_mail_only_success(factory):
    case = factory(helper=False)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reject(case, client, message(case), 501, "mission_reward_unsupported")


def test_cas_retry_recomputes_against_concurrent_ep_without_double_grant(factory, monkeypatch):
    case = factory()
    original = case.store.update_state
    conflicts = []
    def update(state, revision):
        if not conflicts:
            conflicts.append(True)
            previous = case.store.profile()
            external = case.bundle.possessions.credit(previous.state, {PASS_ID: 1})
            original(external, previous.revision)
            raise ValueError("conflict")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, response = post(client, message(case))
    assert balance(case) == 3
    saved = case.store.profile()
    assert len(case.presents.rows(saved.state)) == 1
    assert saved.state["mission_pass_delivery"]["audit"][0]["before"] == 1
    assert rows(response["POSSESSION"])[0]["Count"] == 3


def test_concurrent_same_claim_has_one_mail_and_one_ep_increment(factory, monkeypatch):
    case = factory()
    barrier, lock, calls = Barrier(2), Lock(), 0
    original = case.store.update_state
    def update(state, revision):
        nonlocal calls
        with lock:
            calls += 1
            wait = calls <= 2
        if wait:
            barrier.wait(timeout=15)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(lambda _: post(client, envelope)[0], range(2)))
    assert replies[0] == replies[1] and balance(case) == 2
    assert len(case.presents.rows(case.store.profile().state)) == 1


@pytest.mark.parametrize("tamper", ["marker", "audit", "inventory", "response"])
def test_ep_receipt_requires_matching_marker_arithmetic_and_canonical_inventory(factory, tamper):
    case = factory()
    envelope = message(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        state = deepcopy(case.store.profile().state)
        receipt = state["mission_history"]["receipts"][-1]
        if tamper == "marker":
            state["mission_pass_delivery"]["audit"][0]["credited"] = 1
        elif tamper == "audit":
            receipt["event_pass_points"][0]["before"] = 1
        elif tamper == "inventory":
            next(row for row in state["tables"]["possessions"] if row["ItemID"] == PASS_ID)["Count"] = 3
        else:
            document = loads(receipt["response"].encode("utf-8"))
            children = tuple(replace(child, children=tuple(
                replace(column, values=(999,)) if column.name == "Count" else column for column in child.children))
                if child.name == "POSSESSION" else child for child in document.children)
            receipt["response"] = dumps(replace(document, children=children)).decode()
        receipt["state"] = module._state_digest(state)
        overwrite(case, state)
        reject(case, client, envelope, 503)


def test_two_column_inventory_receipt_replays_its_saved_bytes(factory):
    case = factory()
    envelope = message(case)
    payload = case.service.claim(envelope, case.store)
    legacy = without_enabled(payload)
    possession = next(node for node in loads(legacy).children if node.name == "POSSESSION")
    assert legacy != payload
    assert [column.name for column in possession.children] == ["ItemID", "Count"]
    state = deepcopy(case.store.profile().state)
    state["mission_history"]["receipts"][-1]["response"] = legacy.decode()
    overwrite(case, state)
    saved = case.store.profile()
    assert case.service.claim(envelope, case.store) == legacy
    assert case.store.profile() == saved

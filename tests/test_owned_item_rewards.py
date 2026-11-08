from copy import copy, deepcopy
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_service import MissionService
from test_evolution_integration import post
from test_present_items_integration import claim, counts, fields
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
BLESSING = "battle_god_bless_003025"


@pytest.fixture
def subject(bundle):
    bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
    bootstrap.clock = lambda: NOW
    presents.bootstrap = bootstrap
    skins = bundle.unit_detail.skin_catalog
    missions = MissionService(bootstrap, bundle.player_state, presents, bundle.missions.catalog, skins=skins)
    presents.missions = missions
    bootstrap.present_provider = presents.response
    return bootstrap, presents, missions, skins


def choose_skin(skins):
    return next((card, skin) for card, entries in skins._links.items()
                for skin in entries if skin.item_id in skins.counted_item_ids)


def queued(bundle, subject, item_id, *, existing=0, other=False):
    bootstrap, presents, missions, _ = subject
    state = missions.initialize(bundle.player_state.initialize(bootstrap.initial_state()))
    if existing:
        state = bundle.possessions.credit(state, {item_id: existing})
    grants = [{"kind": "item", "item_id": item_id, "amount": 1, "description": "所持報酬"}]
    if other:
        grants.append({"kind": "gold", "amount": 100, "description": "報酬"})
    return presents.enqueue(state, grants)


def application(subject, path, state):
    bootstrap, presents, missions, _ = subject
    return create_app(path, initial_state=state, handlers=bootstrap.handlers() | presents.handlers() | missions.handlers())


def test_skin_mail_unlocks_display_and_counts_one_acquisition_after_restart(bundle, subject, tmp_path):
    _, presents, missions, skins = subject
    card, skin = choose_skin(skins)
    state, mail = queued(bundle, subject, skin.item_id)
    assert skin.display_type not in skins.allowed_displays(card, state, smartphone_images=True)
    app = application(subject, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, 1, [mail[0]["PresentID"]])
        wire, result = post(client, envelope)
        after = app.state.store.profile()
        assert counts(result["POSSESSION"])[skin.item_id] == 1
        assert skin.display_type in skins.allowed_displays(card, after.state, smartphone_images=True)
        goals = {row.condition_id: row.purchased_skin_sum for row in missions.catalog.definitions if row.condition_type == 11}
        progress = {row["ID"]: row for row in missions.progress.snapshot(after.state, NOW)}
        assert all(progress[key]["Progress"] == 1 for key in goals)
        assert all(progress[key]["Status"] == int(goal == 1) for key, goal in goals.items())
        assert after.state["skin_acquisition_history"]["seen"] == [skin.item_id]
        assert after.state["player_state"] == state["player_state"]
        assert post(client, envelope)[0] == wire and app.state.store.profile() == after
        _, loaded = post(client, request(app.state.store, 137, ()))
        assert counts(loaded["POSSESSION"])[skin.item_id] == 1
    reopened = application(subject, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == after


def test_blessing_mail_is_owned_without_fabricating_activation_or_skin_progress(bundle, subject, tmp_path):
    _, _, missions, _ = subject
    state, mail = queued(bundle, subject, BLESSING)
    app = application(subject, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, claim(app.state.store, 2, [mail[0]["PresentID"]]))
        after = app.state.store.profile().state
        assert counts(result["POSSESSION"]) == {BLESSING: 1}
        assert fields(result["POSSESSION"]) == {"ItemID": (BLESSING,), "Count": (1,), "Enabled": (0,)}
        assert after["tables"]["game_param"] == state["tables"]["game_param"]
        assert "skin_acquisition_history" not in after
        assert all(row["Progress"] == 0 for row in missions.progress.snapshot(after, NOW))


@pytest.mark.parametrize("kind", ["skin", "blessing"])
@pytest.mark.parametrize("operation", [1, 2])
def test_owned_unique_rewards_must_be_claimed_one_at_a_time(bundle, subject, tmp_path, kind, operation):
    item = choose_skin(subject[3])[1].item_id if kind == "skin" else BLESSING
    state, mail = queued(bundle, subject, item, other=True)
    app = application(subject, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, claim(app.state.store, operation, [row["PresentID"] for row in mail]), 409)
        assert response.json()["error"]["code"] == "present_item_single_only"
        assert app.state.store.profile() == before


@pytest.mark.parametrize("kind", ["skin", "blessing"])
@pytest.mark.parametrize("other", [False, True])
def test_batch_claim_keeps_owned_unique_rewards_in_the_mailbox(bundle, subject, tmp_path, kind, other):
    item = choose_skin(subject[3])[1].item_id if kind == "skin" else BLESSING
    state, mail = queued(bundle, subject, item, other=other)
    app = application(subject, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        post(client, claim(app.state.store, 117, [row["PresentID"] for row in mail]))
        after = app.state.store.profile()
    assert after.state["tables"]["presents"] == mail[:1]
    assert after.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert after.state.get("skin_acquisition_history") == before.state.get("skin_acquisition_history")
    money = after.state["tables"]["game_param"][0]["Money"] - before.state["tables"]["game_param"][0]["Money"]
    assert money == (100 if other else 0) and (after == before) is not other


@pytest.mark.parametrize("kind", ["skin", "blessing"])
def test_duplicate_owned_reward_stays_in_mail_without_a_second_count(bundle, subject, tmp_path, kind):
    item = choose_skin(subject[3])[1].item_id if kind == "skin" else BLESSING
    state, mail = queued(bundle, subject, item, existing=1)
    app = application(subject, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, claim(app.state.store, 1, [mail[0]["PresentID"]]), 409)
        assert response.json()["error"]["code"] == "present_item_capacity"
        assert app.state.store.profile() == before


def test_skin_progress_failure_rolls_back_inventory_and_mail(bundle, subject, tmp_path, monkeypatch):
    item = choose_skin(subject[3])[1].item_id
    state, mail = queued(bundle, subject, item)
    app = application(subject, tmp_path, state)

    def failed(*args, **kwargs):
        raise ProtocolError("skin_test_failure", "Skin progress unavailable", 503)

    monkeypatch.setattr(subject[2], "record", failed)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, claim(app.state.store, 1, [mail[0]["PresentID"]]), 503)
        assert response.json()["error"]["code"] == "skin_test_failure"
        assert app.state.store.profile() == before


def test_skin_catalog_absence_cannot_silently_drop_acquisition_progress(bundle, subject, tmp_path):
    bootstrap, presents, _, skins = subject
    item = choose_skin(skins)[1].item_id
    state, mail = queued(bundle, subject, item)
    presents.missions = MissionService(bootstrap, bundle.player_state, presents, bundle.missions.catalog)
    app = application(subject, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, claim(app.state.store, 1, [mail[0]["PresentID"]]), 503)
        assert response.json()["error"]["code"] == "mission_skin_catalog_unavailable"
        assert app.state.store.profile() == before


def test_skin_claim_cas_retry_counts_once_and_preserves_other_changes(bundle, subject, tmp_path, monkeypatch):
    item = choose_skin(subject[3])[1].item_id
    state, mail = queued(bundle, subject, item)
    app = application(subject, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = app.state.store
        update, attempts = store.update_state, []

        def concurrent(candidate, revision):
            attempts.append(revision)
            if len(attempts) == 1:
                before = store.profile()
                other = deepcopy(before.state)
                other["tables"]["game_param"][0]["Money"] += 17
                update(other, before.revision)
                raise ValueError("Concurrent update")
            return update(candidate, revision)

        monkeypatch.setattr(store, "update_state", concurrent)
        post(client, claim(store, 1, [mail[0]["PresentID"]]))
        after = store.profile().state
        assert len(attempts) == 2
        assert after["tables"]["game_param"][0]["Money"] == state["tables"]["game_param"][0]["Money"] + 17
        assert after["skin_acquisition_history"]["seen"] == [item]
        assert all(row["progress"] == 1 for row in after["mission_state"]["entries"])


def test_missing_mission_service_blocks_skin_without_disabling_blessings(bundle, subject, tmp_path):
    item = choose_skin(subject[3])[1].item_id
    state, mail = queued(bundle, subject, item)
    state, more = subject[1].enqueue(state, [
        {"kind": "item", "item_id": BLESSING, "amount": 1, "description": "加護"}])
    subject[1].missions = None
    app = application(subject, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, claim(app.state.store, 1, [mail[0]["PresentID"]]), 503)
        assert response.json()["error"]["code"] == "mission_skin_service_unavailable"
        assert app.state.store.profile() == before
        _, result = post(client, claim(app.state.store, 1, [more[0]["PresentID"]], tick=1001))
        assert counts(result["POSSESSION"]) == {BLESSING: 1}

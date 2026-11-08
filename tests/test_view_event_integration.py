from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from test_evolution_integration import post
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def case(bundle, tmp_path):
    assert bundle.view_events is not None, bundle.view_event_error
    bootstrap, players = copy(bundle.bootstrap), copy(bundle.player_state)
    bootstrap.clock = lambda: NOW
    service = copy(bundle.view_events)
    service.bootstrap, service.players = bootstrap, players
    initial = bundle.initialize(players.initialize(bootstrap.initial_state()))
    initial["sentinel"] = {"keep": [1]}
    return SimpleNamespace(bundle=bundle, bootstrap=bootstrap, players=players, service=service,
                           initial=initial, directory=tmp_path / "http")


def app_for(case, initial=None):
    app = create_app(case.directory, initial_state=case.initial if initial is None else initial,
        state_initializer=case.bundle.initialize,
        handlers=case.bootstrap.handlers() | case.players.handlers() | case.service.handlers())
    case.store = app.state.store
    return app


def message(case, value="", *, number=24, tick=1000, columns=None):
    return request(case.store, number, (Column("F8", "S", (value,)),) if columns is None else columns, tick=tick)


def reject(case, client, envelope, status, code=None):
    before = case.store.profile()
    failure = post(client, envelope, status).json()["error"]
    if code is not None:
        assert failure["code"] == code
    assert case.store.profile() == before


def pointer(case, *, chapter=1, mode=0, male=0, reverse=False):
    cards = case.bundle.gifts.catalog
    card_id = next(identifier for identifier in cards.card_ids if identifier > 1
        and cards.card(identifier)["Kind"] == (0 if male else 1)
        and cards.card(identifier)["BonusType"] != 0 and cards.card(identifier)["LoveEv1"] > 0
        and case.bundle.gifts.aliases.resolve(identifier).event_card_id == identifier)
    fields = [("CardId", card_id), ("Id", chapter), ("Mode", mode), ("Male", male)]
    if reverse:
        fields.reverse()
    return ",".join(f"{key}={value}" for key, value in fields), card_id


def test_both_registrations_use_one_handler_with_distinct_wire_profiles(case):
    handlers = case.service.handlers()
    assert handlers[23] == handlers[24]
    app_for(case)
    save, clear = message(case, number=23), message(case)
    assert save.profile.route == clear.profile.route
    assert save.profile.st != clear.profile.st


def test_empty_clear_is_receipted_and_exact_retry_survives_reopen(case):
    app = app_for(case)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(case)
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM"}
        saved = case.store.profile()
        assert saved.revision == before.revision + 1
        assert saved.state["tables"] == before.state["tables"]
        assert saved.state["player_state"] == before.state["player_state"]
        assert saved.state["sentinel"] == before.state["sentinel"]
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        _, config = post(client, request(case.store, 36, ()))
        assert config["F8"].values == ("",)
    case.service = copy(case.bundle.view_events)
    case.service.bootstrap, case.service.players = case.bootstrap, case.players
    reopened = app_for(case)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved


def test_same_empty_clear_with_new_client_time_is_a_new_receipted_action(case):
    app = app_for(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, message(case))
        first = case.store.profile()
        post(client, message(case, tick=1001))
        second = case.store.profile()
        assert second.revision == first.revision + 1
        assert second.state["tables"] == first.state["tables"]


@pytest.mark.parametrize("number,columns", [
    (23, (Column("F8", "S", ("",)),)),
    (24, (Column("F8", "S", ("not_empty",)),)),
    (24, ()),
    (24, (Column("F8", "I", (0,)),)),
    (24, (Column("F8", "S", ("", "")),)),
    (24, (Column("F8", "S", ("",)), Column("Other", "I", (1,)))),
])
def test_invalid_shapes_or_operation_values_do_not_commit(case, number, columns):
    app = app_for(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(case, client, message(case, number=number, columns=columns), 400)


def test_initialize_and_clear_preserve_unknown_legacy_request_until_explicit_clear(case):
    initial = deepcopy(case.initial)
    initial["tables"]["config"][0]["EventViewRequest"] = "旧" * 85
    before = deepcopy(initial)
    assert case.service.initialize(initial) == before
    assert initial == before
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, config = post(client, request(case.store, 36, ()))
        assert config["F8"].values == ("旧" * 85,)
        post(client, message(case))
        saved = case.store.profile().state
        expected = deepcopy(before)
        expected["tables"]["config"][0]["EventViewRequest"] = ""
        assert {key: value for key, value in saved.items() if key != "view_event_history"} == expected


def test_config_encoding_failure_does_not_clear_or_write_receipt(case):
    initial = deepcopy(case.initial)
    initial["tables"]["config"][0]["EventViewRequest"] = "old_pending_request"
    case.service._configuration_profile = replace(case.service._configuration_profile, response_limit=1)
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(case, client, message(case), 500, "response_too_large")


def test_clear_after_client_clock_rollback_reaches_the_next_startup_read(case):
    app = app_for(case)
    value, _ = pointer(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        later = message(case, value, number=23, tick=1_800_000_000)
        post(client, later)
        post(client, message(case, tick=1_700_000_000))
        _, config = post(client, request(case.store, 36, ()))
        assert config["F8"].values == ("",)
        reject(case, client, later, 409, "view_event_retry_stale")


def test_concurrent_identical_clear_has_one_committed_action(case):
    app = app_for(case)
    before = case.store.profile()
    envelope = message(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: post(client, envelope)[0], range(2)))
        assert results[0] == results[1]
        assert case.store.profile().revision == before.revision + 1
        assert case.store.profile().state["tables"] == before.state["tables"]


@pytest.mark.parametrize("same_field", [False, True])
def test_cas_conflict_preserves_other_writes_and_does_not_overwrite_new_pending_event(case, monkeypatch, same_field):
    app = app_for(case)
    original, attempts, concurrent = case.store.update_state, [], []
    def conflict(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            current = case.store.profile()
            candidate = deepcopy(current.state)
            if same_field:
                candidate["tables"]["config"][0]["EventViewRequest"] = "concurrent_pending_request"
            else:
                candidate["sentinel"]["concurrent"] = True
            original(candidate, current.revision)
            concurrent.append(case.store.profile())
            raise ValueError("concurrent change")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        if same_field:
            response = post(client, message(case), 409)
            assert response.json()["error"]["code"] == "view_event_changed"
            assert case.store.profile() == concurrent[0]
            assert len(attempts) == 1
        else:
            post(client, message(case))
            assert len(attempts) == 2
            assert case.store.profile().state["sentinel"]["concurrent"] is True


@pytest.mark.parametrize("chapter", [1, 2, 3])
@pytest.mark.parametrize("mode,male", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_save_real_history_pointer_without_owned_unit_preserves_all_other_state(case, chapter, mode, male):
    value, card_id = pointer(case, chapter=chapter, mode=mode, male=male)
    initial = case.bundle.card_status.update(case.initial, [{"CardID": card_id, "A7": 7}])
    assert all(unit["CardID"] != card_id for unit in initial["player_state"]["units"])
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(case, value, number=23)
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM"}
        saved = case.store.profile()
        expected = deepcopy(before.state)
        expected["tables"]["config"][0]["EventViewRequest"] = value
        assert {key: data for key, data in saved.state.items() if key != "view_event_history"} == expected
        assert saved.revision == before.revision + 1
        _, config = post(client, request(case.store, 36, ()))
        assert config["F8"].values == (value,)
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        post(client, message(case, tick=1001))
        _, config = post(client, request(case.store, 36, ()))
        assert config["F8"].values == ("",)
        assert case.store.profile().state["tables"]["card_status"] == before.state["tables"]["card_status"]


def test_noncanonical_key_order_is_preserved_through_restart_and_retry(case):
    value, _ = pointer(case, chapter=2, reverse=True)
    app = app_for(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(case, value, number=23)
        wire, _ = post(client, envelope)
        saved = case.store.profile()
        assert saved.state["tables"]["config"][0]["EventViewRequest"] == value
    case.service = copy(case.bundle.view_events)
    case.service.bootstrap, case.service.players = case.bootstrap, case.players
    reopened = app_for(case)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        _, config = post(client, request(case.store, 36, ()))
        assert config["F8"].values == (value,)


def test_old_clear_and_old_save_cannot_overwrite_a_new_pointer(case):
    first, _ = pointer(case)
    second, _ = pointer(case, chapter=2)
    app = app_for(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        empty_clear = message(case, tick=1000)
        post(client, empty_clear)
        first_save = message(case, first, number=23, tick=1001)
        post(client, first_save)
        reject(case, client, empty_clear, 409, "view_event_retry_stale")
        clear = message(case, tick=1002)
        post(client, clear)
        post(client, message(case, second, number=23, tick=1003))
        reject(case, client, clear, 409, "view_event_retry_stale")
        reject(case, client, first_save, 409, "view_event_retry_stale")
        _, config = post(client, request(case.store, 36, ()))
        assert config["F8"].values == (second,)


def test_distinct_save_and_clear_in_one_client_second_share_history_without_being_duplicates(case):
    first, _ = pointer(case)
    second, _ = pointer(case, chapter=3)
    app = app_for(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, message(case, first, number=23))
        clear = message(case)
        post(client, clear)
        post(client, message(case, second, number=23))
        reject(case, client, clear, 409, "view_event_retry_stale")
        assert case.store.profile().state["tables"]["config"][0]["EventViewRequest"] == second


def test_same_pointer_with_new_client_time_is_a_new_persisted_action(case):
    value, _ = pointer(case)
    app = app_for(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, message(case, value, number=23))
        first = case.store.profile()
        post(client, message(case, value, number=23, tick=1001))
        second = case.store.profile()
        assert second.revision == first.revision + 1
        assert second.state["tables"] == first.state["tables"]


def test_concurrent_identical_save_commits_once(case):
    value, _ = pointer(case)
    app = app_for(case)
    before = case.store.profile()
    envelope = message(case, value, number=23)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: post(client, envelope)[0], range(2)))
        assert responses[0] == responses[1]
        assert case.store.profile().revision == before.revision + 1
        assert case.store.profile().state["tables"]["config"][0]["EventViewRequest"] == value


def test_save_config_preflight_failure_keeps_previous_pointer_and_every_other_domain(case):
    value, _ = pointer(case)
    app = app_for(case)
    case.service._configuration_profile = replace(case.service._configuration_profile, response_limit=1)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(case, client, message(case, value, number=23), 500, "response_too_large")

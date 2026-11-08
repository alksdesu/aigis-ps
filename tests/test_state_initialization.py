import pytest

from aigis_ps.app import create_app
from aigis_ps.storage import Store


def test_initializer_preserves_existing_identity_and_progress_and_runs_once(tmp_path):
    original = Store(tmp_path / "aigis.sqlite3", {"money": 900, "tutorial": [1, 2]}).profile()

    def initialize(state):
        state.setdefault("player_state", {"schema": 1, "units": [1]})
        return state

    first = create_app(tmp_path, initial_state={"money": 1000}, state_initializer=initialize).state.store.profile()
    second = create_app(tmp_path, initial_state={"money": 1000}, state_initializer=initialize).state.store.profile()
    assert first.state == {"money": 900, "tutorial": [1, 2], "player_state": {"schema": 1, "units": [1]}}
    assert (first.player_id, first.user_key, first.method_key) == (original.player_id, original.user_key, original.method_key)
    assert first.revision == original.revision + 1
    assert second == first


def test_invalid_existing_schema_is_not_replaced(tmp_path):
    original = Store(tmp_path / "aigis.sqlite3", {"player_state": {"schema": 999}, "money": 900}).profile()

    def reject(state):
        state["money"] = 0
        raise ValueError("Unsupported player schema")

    with pytest.raises(ValueError, match="Unsupported player schema"):
        create_app(tmp_path, state_initializer=reject)
    assert Store(tmp_path / "aigis.sqlite3").profile() == original


@pytest.mark.parametrize("invalid", [None, [], "", 0, False])
def test_invalid_initializer_result_does_not_replace_the_save(tmp_path, invalid):
    original = Store(tmp_path / "aigis.sqlite3", {"money": 900}).profile()
    with pytest.raises(ValueError, match="must return an object"):
        create_app(tmp_path, state_initializer=lambda state: invalid)
    assert Store(tmp_path / "aigis.sqlite3").profile() == original


def test_initializer_reloads_state_after_concurrent_change(tmp_path, monkeypatch):
    original_update = Store.update_state
    conflicted = False

    def update_after_other_request(store, state, revision):
        nonlocal conflicted
        if not conflicted:
            conflicted = True
            current = store.profile()
            original_update(store, {**current.state, "money": 800}, current.revision)
        return original_update(store, state, revision)

    monkeypatch.setattr(Store, "update_state", update_after_other_request)
    application = create_app(tmp_path, initial_state={"money": 900},
                             state_initializer=lambda state: {**state, "player_state": {"schema": 1}})
    assert application.state.store.profile().state == {"money": 800, "player_state": {"schema": 1}}


def test_initializer_stops_after_repeated_conflicts(tmp_path, monkeypatch):
    original = Store(tmp_path / "aigis.sqlite3", {"money": 900}).profile()
    attempts = []

    def conflict(store, state, revision):
        attempts.append(revision)
        raise ValueError("Profile revision changed")

    monkeypatch.setattr(Store, "update_state", conflict)
    with pytest.raises(ValueError, match="Player state changed"):
        create_app(tmp_path, state_initializer=lambda state: {**state, "player_state": {"schema": 1}})
    assert len(attempts) == 3
    assert Store(tmp_path / "aigis.sqlite3").profile() == original

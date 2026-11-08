import sqlite3

import pytest

from aigis_ps.local_io import MAX_JSON_DEPTH, JsonNestingTooDeep
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.storage import Store, deserialize, serialize


NONFINITE = ("NaN", "Infinity", "-Infinity", "1e999", "-1E400")
# Deep enough that the C scanner itself raises RecursionError before any depth check runs.
SCANNER_DEPTH = 100000


def nested(levels):
    value = []
    for _ in range(levels - 1):
        value = [value]
    return value


def test_identity_and_progress_survive_reopening(tmp_path):
    path = tmp_path / "local.sqlite3"
    first = Store(path, {"progress": 0}).profile()
    changed = Store(path).update_state({"progress": 7}, first.revision)
    restored = Store(path, {"progress": 999}).profile()
    assert restored == changed
    assert restored.player_id == first.player_id
    assert restored.user_key == first.user_key
    assert restored.method_key == first.method_key


def test_revision_conflict_does_not_overwrite_progress(tmp_path):
    store = Store(tmp_path / "local.sqlite3")
    store.update_state({"progress": 1}, 0)
    with pytest.raises(ValueError, match="revision"):
        store.update_state({"progress": 2}, 0)
    assert store.profile().state == {"progress": 1}


def test_other_application_database_is_not_migrated(tmp_path):
    path = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(ValueError, match="another application"):
        Store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("unrelated",)]


def test_diagnostics_persist_structure_without_credentials(tmp_path):
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[31]
    xml = prepare_request(b"<DA><UK>do-not-store-me</UK></DA>", profile.st)
    envelope = registry.decode(profile.route, encode_wire(xml, profile.request_key))
    store = Store(tmp_path / "local.sqlite3")
    store.record_request(envelope, profile.route, 50, 40, 200)
    logged = store.recent_requests()
    assert len(logged) == 1 and logged[0]["status"] == 200
    assert "/DA/UK" in logged[0]["structure"]["field_paths"]
    assert "do-not-store-me" not in str(logged)


@pytest.mark.parametrize("number", NONFINITE)
def test_strict_reader_refuses_numbers_the_serializer_cannot_write(number):
    with pytest.raises(ValueError, match="finite"):
        deserialize(f'{{"progress":[{number}]}}')
    with pytest.raises(ValueError):
        serialize({"progress": [float(number)]})


def test_strict_reader_refuses_duplicate_keys():
    with pytest.raises(ValueError, match="Duplicate"):
        deserialize('{"progress":1,"progress":2}')


def test_strict_reader_bounds_nesting_like_the_config_and_admin_readers():
    assert deserialize(serialize(nested(MAX_JSON_DEPTH))) == nested(MAX_JSON_DEPTH)
    for document in (nested(MAX_JSON_DEPTH + 1), {"progress": nested(MAX_JSON_DEPTH)},
                     {"progress": [{"rows": nested(MAX_JSON_DEPTH - 2)}]}):
        with pytest.raises(JsonNestingTooDeep, match="nesting") as error:
            deserialize(serialize(document))
        assert isinstance(error.value, ValueError) and isinstance(error.value, RecursionError)


def test_scanner_recursion_surfaces_unchanged():
    with pytest.raises(RecursionError) as error:
        deserialize("[" * SCANNER_DEPTH + "]" * SCANNER_DEPTH)
    assert not isinstance(error.value, ValueError)


def test_strict_reader_reads_back_everything_the_serializer_writes():
    state = {"progress": 7, "ratio": 1.5, "smallest": 5e-324, "largest": 1.7976931348623157e308, "negative_zero": -0.0,
             "text": "ローカル\u0000", "flags": [True, False, None], "rows": [{"ItemID": 1, "Count": 2 ** 63 - 1}],
             "empty": {}, "deepest": nested(MAX_JSON_DEPTH - 1)}
    assert deserialize(serialize(state)) == state
    assert deserialize(serialize(state).encode("utf-8")) == state

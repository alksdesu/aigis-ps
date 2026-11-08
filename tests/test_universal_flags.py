from copy import deepcopy
import json
from pathlib import Path

import pytest

import aigis_ps.universal_flags as module
from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import NATIVE_CELL_BYTES
from aigis_ps.universal_flags import (MAX_FLAG_NAME_BYTES, STORED_FLAG_NAME_BYTES, flag_hash, flag_value, flags_response,
                                      read_flags, resolve_flag_name, set_flags)


@pytest.fixture
def state():
    return {"bootstrap_schema": 1, "tables": {"universal_flags": [], "other": [1]}, "sentinel": {"nested": [2]}}


def test_legacy_empty_state_remains_unchanged_and_reads_zero(state):
    before = deepcopy(state)
    assert read_flags(state) == []
    assert flags_response(state) == Container("GF")
    assert flag_value(state, "Missing") == 0
    assert set_flags(state, {}) == state
    assert "universal_flags_schema" not in state
    assert state == before


def test_updates_preserve_unrelated_flags_and_zero_is_an_explicit_value(state):
    first = set_flags(state, {"PurchasedSkin_0001": 1, "既読": -2147483648})
    changed = set_flags(first, {"PurchasedSkin_0001": 0, "Other": 2147483647})
    assert changed["universal_flags_schema"] == 1
    assert read_flags(changed) == [{"ID": "PurchasedSkin_0001", "Value": 0},
                                  {"ID": "既読", "Value": -2147483648}, {"ID": "Other", "Value": 2147483647}]
    response = flags_response(changed)
    assert response == Container("GF", (Column("ID", "S", ("PurchasedSkin_0001", "既読", "Other")),
                                        Column("Value", "I", (0, -2147483648, 2147483647))))
    assert loads(dumps(Container(children=(response,)))).children == (response,)
    changed["sentinel"]["nested"].append(3)
    assert state["sentinel"]["nested"] == [2]
    assert flag_value(first, "PurchasedSkin_0001") == 1


def test_subset_is_ordered_and_requires_stored_names(state):
    state = set_flags(state, {"A": 1, "B": 2})
    assert flags_response(state, ["B", "A"]).children[1].values == (2, 1)
    assert flags_response(state, []) == Container("GF")
    with pytest.raises(ProtocolError) as error:
        flags_response(state, ["missing"])
    assert error.value.code == "universal_flag_not_found"


@pytest.mark.parametrize("rows,version", [([{"ID": "A", "Value": 1}], None),
    ([], True), ([], 2), ([{"ID": "A", "Value": True}], 1),
    ([{"ID": "A", "Value": 1, "Extra": 0}], 1), ([{"ID": 1, "Value": 1}], 1),
    ([{"ID": "A", "Value": 1}] * 2, 1), ({}, 1), (None, 1)])
def test_unsupported_or_corrupt_state_is_never_reset(state, rows, version):
    state["tables"]["universal_flags"] = rows
    if version is not None:
        state["universal_flags_schema"] = version
    before = deepcopy(state)
    for callback in (lambda: read_flags(state), lambda: flags_response(state),
                     lambda: set_flags(state, {"B": 1})):
        with pytest.raises(ProtocolError) as error:
            callback()
        assert error.value.code == "universal_flags_invalid"
        assert state == before


@pytest.mark.parametrize("updates", [{"": 1}, {"a\0b": 1}, {"a" * 513: 1}, {1: 1},
    {"A": True}, {"A": "1"}, {"A": 2147483648}, {"A": -2147483649}, [], None])
def test_invalid_update_is_atomic(state, updates):
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        set_flags(state, updates)
    assert state == before


def test_hash_collision_cannot_overwrite_a_different_stored_name(state, monkeypatch):
    monkeypatch.setattr(module, "flag_hash", lambda name: 1)
    state = set_flags(state, {"Original": 9})
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        set_flags(state, {"Different": 1})
    assert error.value.code == "universal_flag_name_conflict"
    assert state == before
    assert resolve_flag_name(state, 1, "Reconstructed") == "Original"


def test_unstored_flag_uses_a_verified_reconstructed_name(state):
    name = "PurchasedSkin_0001"
    assert flag_hash(name) == -1543114182
    assert resolve_flag_name(state, -1543114182, name) == name
    with pytest.raises(ProtocolError, match="must agree"):
        resolve_flag_name(state, 123, name)


def test_capacity_failure_preserves_existing_flags(state, monkeypatch):
    monkeypatch.setattr(module, "MAX_FLAGS", 2)
    state = set_flags(state, {"A": 1, "B": 2})
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        set_flags(state, {"C": 1})
    assert error.value.code == "universal_flag_capacity"
    assert state == before


def test_all_current_skin_names_match_the_three_independent_native_keys():
    root = Path(__file__).resolve().parents[2]
    data = json.loads((root / "aigis_research/permanent-20261101/skin-acquisition-evidence/SkinShop.atb.json").read_text(encoding="utf-8"))
    assert len(data["rows"]) == 38
    for row in data["rows"]:
        number = row["ButtonImageId"]
        assert flag_hash(f"PurchasedSkin_{number:04d}") == row["PurchaseFlag"]
        assert flag_hash(f"Skin{number:04d}") == row["ConfigId"]
        assert flag_hash(f"skin_{number:04d}") == row["SkinId"]


def test_new_flag_names_follow_the_native_cell(state):
    assert MAX_FLAG_NAME_BYTES == NATIVE_CELL_BYTES == 255 and STORED_FLAG_NAME_BYTES == 512
    name = "f" * MAX_FLAG_NAME_BYTES
    assert read_flags(set_flags(state, {name: 1})) == [{"ID": name, "Value": 1}]
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        set_flags(state, {"f" * (MAX_FLAG_NAME_BYTES + 1): 1})
    assert error.value.code == "universal_flag_updates_invalid" and state == before


@pytest.mark.parametrize("name", ["g" * (MAX_FLAG_NAME_BYTES + 1), "旗" * 170 + "gg"])
def test_flags_saved_under_the_older_bound_keep_loading_and_updating(state, name):
    assert len(name.encode("utf-8")) in (MAX_FLAG_NAME_BYTES + 1, STORED_FLAG_NAME_BYTES)
    state["universal_flags_schema"] = 1
    state["tables"]["universal_flags"] = [{"ID": name, "Value": 4}]
    before = deepcopy(state)
    assert read_flags(state) == [{"ID": name, "Value": 4}]
    assert flags_response(state, [name]).children[1].values == (4,)
    assert flag_value(state, name) == 4 and resolve_flag_name(state, flag_hash(name), name) == name
    assert read_flags(set_flags(state, {name: 9, "Other": 1})) == [{"ID": name, "Value": 9}, {"ID": "Other", "Value": 1}]
    assert state == before
    fresh = {"bootstrap_schema": 1, "tables": {"universal_flags": []}}
    with pytest.raises(ProtocolError) as error:
        set_flags(fresh, {name: 1})
    assert error.value.code == "universal_flag_updates_invalid"


def test_stored_flag_names_past_the_older_bound_are_invalid(state):
    state["universal_flags_schema"] = 1
    state["tables"]["universal_flags"] = [{"ID": "h" * (STORED_FLAG_NAME_BYTES + 1), "Value": 1}]
    with pytest.raises(ProtocolError) as error:
        read_flags(state)
    assert error.value.code == "universal_flags_invalid"

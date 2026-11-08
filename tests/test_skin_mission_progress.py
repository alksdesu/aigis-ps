from copy import deepcopy
from datetime import timedelta

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.mission_state import MissionProgress
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from test_mission_catalog import mission
from test_mission_state import NOW, catalog, fail, rows, saved


ITEMS = frozenset(("get_skin_00001", "get_skin_00002", "get_skin_00003"))


def engine():
    return MissionProgress(catalog([
        mission(101, PeriodType=4, ConditionType=11, PurchasedSkinSum=1, DispTabType=7),
        mission(102, PeriodType=4, ConditionType=11, PurchasedSkinSum=2, DispTabType=7),
    ]), skin_items=ITEMS)


def acquire(subject, state, previous=(), current=(), now=NOW):
    return subject.record(state, "skin_acquired", {
        "previous_items": list(previous), "current_items": list(current)}, now)


def test_existing_owned_skins_are_a_baseline_and_only_new_items_count():
    subject = engine()
    state = saved()
    before = deepcopy(state)
    result = acquire(subject, state, ["get_skin_00001"], ["get_skin_00001", "get_skin_00002"])
    assert state == before
    assert rows(subject, result)[101]["Status"] == 1
    assert rows(subject, result)[102]["Progress"] == 1
    assert result["skin_acquisition_history"]["seen"] == ["get_skin_00001", "get_skin_00002"]


def test_retries_removal_and_regrant_cannot_count_the_same_skin_again():
    subject = engine()
    first = acquire(subject, saved(), [], ["get_skin_00001"])
    assert acquire(subject, first, [], ["get_skin_00001"], NOW + timedelta(seconds=1)) == first
    removed = acquire(subject, first, ["get_skin_00001"], [])
    restored = acquire(subject, removed, [], ["get_skin_00001"])
    assert rows(subject, restored)[102]["Progress"] == 1
    next_skin = acquire(subject, restored, ["get_skin_00001"], ["get_skin_00001", "get_skin_00002"])
    assert rows(subject, next_skin)[102]["Status"] == 1
    claimed = subject.claim(next_skin, [102], NOW)
    assert rows(subject, acquire(subject, claimed, [], ITEMS))[102]["Status"] == 2


def test_multiple_distinct_skins_use_one_transaction_and_clamp_goals():
    subject = engine()
    result = acquire(subject, saved(), (), ITEMS)
    assert rows(subject, result)[101]["Progress"] == 1
    assert rows(subject, result)[102]["Progress"] == 2
    assert result["skin_acquisition_history"]["seen"] == sorted(ITEMS)


def test_empty_acquisition_does_not_create_history_or_touch_time():
    subject = engine()
    state = subject.initialize(saved())
    assert acquire(subject, state) == state


@pytest.mark.parametrize("previous,current", [(["unknown"], []), ([], ["unknown"]),
    ([], ["get_skin_00001", "get_skin_00001"]), ([], [True]), ([], [""]), ([], ["a\0b"]), ([], ["\ud800"])])
def test_invalid_or_unverified_skin_events_preserve_original(previous, current):
    subject = engine()
    state = saved()
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        acquire(subject, state, previous, current)
    assert state == before


@pytest.mark.parametrize("history", [{}, {"schema_version": 2, "seen": []},
    {"schema_version": 1, "seen": ["get_skin_00001", "get_skin_00001"]},
    {"schema_version": 1, "seen": ["s" * (STORED_ITEM_NAME_BYTES + 1)]}])
def test_unknown_or_corrupt_history_is_never_reset(history):
    state = {**saved(), "skin_acquisition_history": history}
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        engine().initialize(state)
    assert state == before


@pytest.mark.parametrize("unit", ["x", "投"])
def test_saved_history_keeps_names_written_under_the_stored_bound(unit):
    legacy = unit * (STORED_ITEM_NAME_BYTES // len(unit.encode("utf-8")))
    legacy += "x" * (STORED_ITEM_NAME_BYTES - len(legacy.encode("utf-8")))
    assert len(legacy.encode("utf-8")) == STORED_ITEM_NAME_BYTES
    subject = engine()
    state = {**saved(), "skin_acquisition_history": {"schema_version": 1, "seen": ["get_skin_00001", legacy]}}
    before = deepcopy(state)
    assert subject.initialize(state)["skin_acquisition_history"] == before["skin_acquisition_history"]
    assert rows(subject, state)[101]["Status"] == 0
    result = acquire(subject, state, ["get_skin_00001"], ["get_skin_00001", "get_skin_00002"])
    assert state == before
    assert result["skin_acquisition_history"]["seen"] == ["get_skin_00001", "get_skin_00002", legacy]
    assert rows(subject, result)[101]["Status"] == 1
    assert rows(subject, result)[102]["Progress"] == 1
    oversized = {**saved(), "skin_acquisition_history": {"schema_version": 1, "seen": ["get_skin_00001", legacy + "x"]}}
    fail("mission_skin_items_invalid", lambda: subject.initialize(oversized))


@pytest.mark.parametrize("unit", ["x", "投"])
def test_verified_items_and_new_events_keep_the_native_cell(unit):
    fitting = unit * (MAX_ITEM_NAME_BYTES // len(unit.encode("utf-8")))
    stored = fitting + "x" * (STORED_ITEM_NAME_BYTES - len(fitting.encode("utf-8")))
    assert (len(fitting.encode("utf-8")), len(stored.encode("utf-8"))) == (MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES)
    source = engine().catalog
    subject = MissionProgress(source, skin_items=(*ITEMS, fitting))
    result = acquire(subject, saved(), (), [fitting])
    assert result["skin_acquisition_history"]["seen"] == [fitting]
    assert rows(subject, result)[101]["Status"] == 1
    fail("mission_skin_items_invalid", lambda: MissionProgress(source, skin_items=(*ITEMS, stored)))
    state = saved()
    before = deepcopy(state)
    fail("mission_skin_items_invalid", lambda: acquire(subject, state, (), [stored]))
    fail("mission_skin_items_invalid", lambda: acquire(subject, state, [stored], []))
    assert state == before


def test_catalog_removal_preserves_historical_skin_identifiers():
    old = engine()
    state = acquire(old, saved(), (), ITEMS)
    reduced = MissionProgress(old.catalog, skin_items=("get_skin_00001",))
    assert reduced.initialize(state)["skin_acquisition_history"] == state["skin_acquisition_history"]
    assert reduced.content_version != old.content_version

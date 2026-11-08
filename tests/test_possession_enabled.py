from copy import deepcopy

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.errors import ProtocolError
from test_possessions import GIFTS, service, state


FIRST, SECOND, THIRD = (row[0] for row in GIFTS[:3])
LAST = GIFTS[3][0]
SELECTIONS = (None, [], [FIRST], [SECOND], [THIRD], [SECOND, FIRST], [LAST, THIRD])


def seeded(state):
    state["tables"]["possessions"] = [{"ItemID": FIRST, "Count": 3, "Enabled": 1},
                                       {"ItemID": SECOND, "Count": 2},
                                       {"ItemID": THIRD, "Count": 0, "Enabled": 0}]
    return state


def unflagged(state):
    state["tables"]["possessions"] = [{"ItemID": FIRST, "Count": 3}, {"ItemID": SECOND, "Count": 2}]
    return state


def fields(node):
    return {column.name: column.values for column in node.children}


def earlier(state, selection, include_enabled):
    # Two-column receipts follow this rule: Enabled only when requested or already stored.
    stored = {row["ItemID"]: row for row in state["tables"]["possessions"]}
    names = list(stored) if selection is None else list(selection)
    rows = [stored.get(name, {"ItemID": name, "Count": 0}) for name in names]
    if not rows:
        return Container("POSSESSION")
    columns = (Column("ItemID", "S", tuple(names)), Column("Count", "I", tuple(row["Count"] for row in rows)))
    if include_enabled or any("Enabled" in row for row in rows):
        columns += (Column("Enabled", "I", tuple(row.get("Enabled", 0) for row in rows)),)
    return Container("POSSESSION", columns)


def test_rows_without_a_stored_flag_list_enabled_zero_and_keep_state(service, state):
    state = service.credit(state, {FIRST: 3})
    old = deepcopy(state)
    assert service.response(state) == Container("POSSESSION", (
        Column("ItemID", "S", (FIRST,)), Column("Count", "I", (3,)), Column("Enabled", "I", (0,))))
    assert service.response(state, legacy=True) == Container("POSSESSION", (
        Column("ItemID", "S", (FIRST,)), Column("Count", "I", (3,))))
    assert service.set_enabled(state, {FIRST: 0}) == state
    assert service.response(state, include_enabled=True) == service.response(state)
    assert state == old


def test_startup_and_delta_responses_always_list_enabled(service, state):
    assert service.startup_response(state) == service.response(state) == Container("POSSESSION")
    state = service.credit(state, {FIRST: 3, SECOND: 1})
    before = deepcopy(state)
    assert service.startup_response(state) == service.response(state) == Container("POSSESSION", (
        Column("ItemID", "S", (FIRST, SECOND)), Column("Count", "I", (3, 1)), Column("Enabled", "I", (0, 0))))
    assert fields(service.response(state, [FIRST])) == {"ItemID": (FIRST,), "Count": (3,), "Enabled": (0,)}
    assert "Enabled" not in fields(service.response(state, [FIRST], legacy=True))
    assert state == before


def test_mixed_snapshot_and_selected_rows_keep_their_enabled_values(service, state):
    seeded(state)
    before = deepcopy(state)
    assert fields(service.response(state)) == {
        "ItemID": (FIRST, SECOND, THIRD), "Count": (3, 2, 0), "Enabled": (1, 0, 0)}
    assert fields(service.response(state, [SECOND, FIRST]))["Enabled"] == (0, 1)
    assert fields(service.response(state, [SECOND]))["Enabled"] == (0,)
    assert fields(service.response(state, [THIRD]))["Enabled"] == (0,)
    assert fields(service.response(state, [SECOND, FIRST], legacy=True))["Enabled"] == (0, 1)
    assert "Enabled" not in fields(service.response(state, [SECOND], legacy=True))
    for options in ({}, {"include_enabled": True}, {"legacy": True}):
        assert service.response(state, [], **options) == Container("POSSESSION")
    assert state == before


@pytest.mark.parametrize("prepare", [seeded, unflagged])
@pytest.mark.parametrize("selection", SELECTIONS)
@pytest.mark.parametrize("include_enabled", [False, True])
def test_legacy_rendering_repeats_the_earlier_rule_and_current_tables_always_list_enabled(
        service, state, prepare, selection, include_enabled):
    prepare(state)
    before = deepcopy(state)
    assert service.response(state, selection, include_enabled=include_enabled, legacy=True) == earlier(
        state, selection, include_enabled)
    assert service.response(state, selection, include_enabled=include_enabled) == earlier(state, selection, True)
    assert state == before


@pytest.mark.parametrize("prepare", [seeded, unflagged])
@pytest.mark.parametrize("selection", SELECTIONS)
def test_table_matches_accepts_the_current_and_the_earlier_receipt_shapes(service, state, prepare, selection):
    prepare(state)
    before = deepcopy(state)
    for table in (earlier(state, selection, True), earlier(state, selection, False)):
        assert service.table_matches(table, state, selection) is True
        assert service.table_matches(table, state, None if selection is None else iter(selection)) is True
    assert state == before


def test_table_matches_rejects_tables_neither_shape_produces(service, state):
    unflagged(state)
    selection = [FIRST, SECOND]
    current, legacy = earlier(state, selection, True), earlier(state, selection, False)
    names, counts, flags = current.children
    rejected = (
        Container("POSSESSION"),
        Container("POSSESSION", (names, Column("Count", "I", (3, 1)), flags)),
        Container("POSSESSION", (names, counts, Column("Enabled", "I", (1, 0)))),
        Container("POSSESSION", (Column("ItemID", "S", (SECOND, FIRST)), Column("Count", "I", (2, 3)))),
        Container("POSSESSION", (counts, names, flags)),
        Container("POSSESSION", (names, flags)),
        Container("POSSESSION", (*current.children, Column("Extra", "I", (0, 0)))),
        Container("POSSESSION", (names, Column("Count", "S", ("3", "2")))),
        Container("PPT", legacy.children),
    )
    for table in rejected:
        assert service.table_matches(table, state, selection) is False
    assert service.table_matches(current, state, [FIRST]) is False
    assert service.table_matches(Container("POSSESSION"), state, []) is True


def test_two_column_receipts_fail_once_their_rows_store_a_flag(service, state):
    seeded(state)
    legacy = Container("POSSESSION", (Column("ItemID", "S", (FIRST,)), Column("Count", "I", (3,))))
    assert service.table_matches(legacy, state, [FIRST]) is False
    assert service.table_matches(earlier(state, [FIRST], False), state, [FIRST]) is True


@pytest.mark.parametrize("selection,code", [
    (FIRST, "possession_selection_invalid"), (1, "possession_selection_invalid"),
    (["unknown"], "possession_item_unknown"), ([FIRST, FIRST], "possession_selection_invalid")])
def test_table_matches_validates_selections_like_responses(service, state, selection, code):
    unflagged(state)
    before = deepcopy(state)
    for check in (lambda: service.response(state, selection),
                  lambda: service.table_matches(Container("POSSESSION"), state, selection)):
        with pytest.raises(ProtocolError) as caught:
            check()
        assert (caught.value.code, caught.value.status) == (code, 400)
    assert state == before


def test_credit_and_debit_preserve_flags_on_changed_and_untouched_rows(service, state):
    seeded(state)
    before = deepcopy(state)
    raised = service.credit(state, {FIRST: 2, SECOND: 1, THIRD: 1})
    lowered = service.debit(raised, {FIRST: 5, SECOND: 1})
    assert fields(service.response(lowered)) == {
        "ItemID": (FIRST, SECOND, THIRD), "Count": (0, 2, 1), "Enabled": (1, 0, 0)}
    assert lowered["tables"]["possessions"][1] == {"ItemID": SECOND, "Count": 2}
    restored = service.credit(lowered, {FIRST: 1})
    assert fields(service.response(restored, [FIRST])) == {
        "ItemID": (FIRST,), "Count": (1,), "Enabled": (1,)}
    assert state == before


def test_absolute_settings_only_update_owned_requested_rows(service, state):
    seeded(state)
    before = deepcopy(state)
    result = service.set_enabled(state, {FIRST: 0, SECOND: 1})
    assert result["tables"]["possessions"] == [
        {"ItemID": FIRST, "Count": 3, "Enabled": 0},
        {"ItemID": SECOND, "Count": 2, "Enabled": 1},
        {"ItemID": THIRD, "Count": 0, "Enabled": 0}]
    assert service.set_enabled(result, {FIRST: 0, SECOND: 1}) == result
    result["unrelated"].append(3)
    assert state == before


@pytest.mark.parametrize("enabled", [None, True, False, -1, 2, "1", 1.0, [], {}, 0x80000000])
def test_invalid_stored_enabled_fails_every_path_without_repair(service, state, enabled):
    state["tables"]["possessions"] = [{"ItemID": FIRST, "Count": 1, "Enabled": enabled}]
    before = deepcopy(state)
    for callback in (lambda: service.response(state), lambda: service.response(state, legacy=True),
                     lambda: service.table_matches(Container("POSSESSION"), state, [FIRST]),
                     lambda: service.credit(state, {}), lambda: service.debit(state, {}),
                     lambda: service.set_enabled(state, {})):
        with pytest.raises(ProtocolError) as error:
            callback()
        assert error.value.code == "possession_state_invalid"
        assert state == before


@pytest.mark.parametrize("settings", [None, [], True, {FIRST: True}, {FIRST: -1}, {FIRST: 2},
    {FIRST: "1"}, {FIRST: 0.0}, {THIRD: 1}, {THIRD: 0}, {"unknown": 0},
    {FIRST: 0, THIRD: 1}, {FIRST.upper(): 1}])
def test_invalid_or_unowned_settings_fail_the_whole_operation(service, state, settings):
    seeded(state)
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        service.set_enabled(state, settings)
    assert state == before


@pytest.mark.parametrize("option", ["include_enabled", "legacy"])
@pytest.mark.parametrize("selection", [0, 1, None, "true"])
def test_enabled_wire_options_require_booleans(service, state, option, selection):
    with pytest.raises(ProtocolError) as error:
        service.response(state, **{option: selection})
    assert error.value.code == "possession_selection_invalid"


def test_missing_requested_item_is_explicit_zero_with_flag(service, state):
    seeded(state)
    expected = {"ItemID": (LAST,), "Count": (0,), "Enabled": (0,)}
    assert fields(service.response(state, [LAST])) == expected
    assert fields(service.response(state, [LAST], include_enabled=True)) == expected
    assert fields(service.response(state, [LAST], legacy=True)) == {"ItemID": (LAST,), "Count": (0,)}


def test_empty_settings_return_an_independent_copy(service, state):
    seeded(state)
    updated = service.set_enabled(state, {})
    assert updated == state and updated is not state
    updated["tables"]["possessions"][0]["Enabled"] = 0
    assert state["tables"]["possessions"][0]["Enabled"] == 1

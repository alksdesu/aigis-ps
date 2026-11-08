from copy import deepcopy

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from aigis_ps import vote_state
from aigis_ps.vote_state import (
    initialize_vote_state, read_vote_history, record_vote, vote_history_response, vote_key,
)


ITEM = "item_vote_008001"
DEFINITION = "a" * 64
COLLISION_ITEMS = ("fixture_a95e755f7649f51e", "fixture_a92cc8a101530d68")


def row(item=ITEM, card=1, group=8, count=0):
    return {"key": vote_key(item, card), "cardId": card, "groupId": group, "count": count, "itemId": item}


def state(rows=None):
    return {"bootstrap_schema": 1, "tables": {"vote_history": deepcopy(rows or []), "other": [{"x": [7]}]},
            "old_receipts": {"values": [1, 2]}}


def update(source, **kwargs):
    return record_vote(source, **{"item_id": ITEM, "card_id": 1, "group_id": 8,
                                 "count": 3, "definition": DEFINITION, **kwargs})


def reject_state(source):
    before = deepcopy(source)
    for reader in (read_vote_history, initialize_vote_state, vote_history_response):
        with pytest.raises(ProtocolError) as caught:
            reader(source)
        assert (caught.value.code, caught.value.status) == ("vote_state_invalid", 503)
        assert source == before


@pytest.mark.parametrize(("item", "card", "expected"), [
    (ITEM, 1, -1300054857), (ITEM, 2930, -1624566048), ("投票券", 12, -1633618063),
])
def test_native_key_uses_utf8_item_colon_decimal_card(item, card, expected):
    assert vote_key(item, card) == expected


def test_native_response_preserves_lowercase_fields_types_order_and_signed_keys():
    source = state([row(count=5), row(card=2930, count=13)])
    before = deepcopy(source)
    expected = Container("PVH", (Column("key", "I", (-1300054857, -1624566048)),
        Column("cardId", "I", (1, 2930)), Column("groupId", "I", (8, 8)),
        Column("count", "I", (5, 13)), Column("itemId", "S", (ITEM, ITEM))))
    assert vote_history_response(source) == expected
    assert loads(dumps(Container(children=(expected,)))).children == (expected,)
    assert source == before


def test_empty_initialization_and_zero_update_do_not_create_history():
    source = state()
    assert initialize_vote_state(source) == source
    assert update(source, count=0) == source
    assert vote_history_response(source) == Container("PVH", tuple(
        Column(name, kind, ()) for name, kind in vote_state.FIELDS))


def test_legacy_baseline_is_preserved_and_new_votes_do_not_backfill():
    source = state([row(count=19), row(item="retired_ticket", card=2, group=91, count=4)])
    before = deepcopy(source)
    initialized = initialize_vote_state(source)
    key = str(vote_key(ITEM, 1))
    assert initialized["vote_state"]["entries"][key] == {
        "definition": None, "baseline_count": 19, "count": 19}
    assert initialize_vote_state(initialized) == initialized
    assert update(initialized, count=19) == initialized
    updated = update(initialized, count=22)
    assert updated["tables"]["vote_history"][0]["count"] == 22
    assert updated["vote_state"]["entries"][key] == {
        "definition": DEFINITION, "baseline_count": 19, "count": 22}
    assert updated["tables"]["vote_history"][1] == before["tables"]["vote_history"][1]
    assert updated["old_receipts"] == source["old_receipts"]
    assert source == before


def test_absolute_update_is_idempotent_and_detached_from_inputs():
    source = state()
    first = update(source)
    second = update(first, count=7)
    assert read_vote_history(second) == [row(count=7)]
    assert update(second, count=7) == second
    assert first["tables"]["vote_history"] == [row(count=3)]
    rows = read_vote_history(second)
    rows[0]["count"] = 99
    second["tables"]["other"][0]["x"].append(8)
    assert read_vote_history(second)[0]["count"] == 7
    assert source == state()


@pytest.mark.parametrize("count", [0, 2])
def test_absolute_vote_total_cannot_decrease(count):
    source = update(state())
    with pytest.raises(ProtocolError, match="cannot decrease") as caught:
        update(source, count=count)
    assert caught.value.code == "vote_count_decreased"
    assert source["tables"]["vote_history"] == [row(count=3)]


@pytest.mark.parametrize("count", [3, 4])
def test_bound_definition_cannot_change_even_for_equal_total(count):
    source = update(state())
    with pytest.raises(ProtocolError) as caught:
        update(source, count=count, definition="b" * 64)
    assert caught.value.code == "vote_definition_changed"


def test_multiple_candidates_same_item_share_group_but_have_distinct_keys():
    source = update(state())
    updated = update(source, card_id=2, count=4)
    assert read_vote_history(updated) == [row(count=3), row(card=2, count=4)]
    with pytest.raises(ProtocolError) as caught:
        update(updated, card_id=3, group_id=9)
    assert caught.value.code == "vote_group_changed"


def test_real_crc_collision_is_rejected_in_native_rows_and_new_updates():
    left, right = COLLISION_ITEMS
    assert vote_key(left, 1) == vote_key(right, 1) == 994414806
    reject_state(state([row(left, count=3), row(right, count=7)]))
    source = state([row(left, count=3)])
    with pytest.raises(ProtocolError) as caught:
        update(source, item_id=right, count=4)
    assert caught.value.code == "vote_identity_collision"


@pytest.mark.parametrize(("field", "value"), [
    ("key", 123), ("key", True), ("cardId", 0), ("cardId", False), ("cardId", 2**31),
    ("groupId", 0), ("groupId", -1), ("groupId", True), ("count", -1), ("count", 2**31),
    ("count", True), ("count", "1"), ("itemId", ""), ("itemId", "a" * 257),
    ("itemId", "\x00"), ("itemId", "\ud800"), ("itemId", None),
])
def test_invalid_native_values_are_rejected_without_rewriting(field, value):
    candidate = state([row()])
    candidate["tables"]["vote_history"][0][field] = value
    reject_state(candidate)


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(bootstrap_schema=True),
    lambda s: s.update(bootstrap_schema=2),
    lambda s: s.pop("tables"),
    lambda s: s["tables"].pop("vote_history"),
    lambda s: s["tables"].update(vote_history={}),
    lambda s: s["tables"]["vote_history"][0].update(extra=1),
    lambda s: s["tables"]["vote_history"][0].pop("count"),
    lambda s: s["tables"]["vote_history"].append(row()),
    lambda s: s["tables"]["vote_history"].append(row(card=2, group=9)),
])
def test_native_storage_contract_is_strict(mutation):
    source = state([row()])
    mutation(source)
    reject_state(source)


@pytest.mark.parametrize("mutation", [
    lambda j: j.update(schema_version=True), lambda j: j.update(schema_version=2),
    lambda j: j.update(extra=0), lambda j: j.update(entries={}),
    lambda j: j["entries"].update(unrelated={}),
    lambda j: next(iter(j["entries"].values())).update(count=99),
    lambda j: next(iter(j["entries"].values())).update(baseline_count=4),
    lambda j: next(iter(j["entries"].values())).update(baseline_count=True),
    lambda j: next(iter(j["entries"].values())).update(definition=None),
    lambda j: next(iter(j["entries"].values())).update(definition="bad"),
    lambda j: next(iter(j["entries"].values())).update(extra=0),
])
def test_journal_cannot_be_rebuilt_from_inconsistent_native_rows(mutation):
    source = update(state())
    mutation(source["vote_state"])
    reject_state(source)


@pytest.mark.parametrize("kwargs", [
    {"group_id": 0}, {"group_id": True}, {"count": -1}, {"count": 2**31}, {"count": False},
    {"card_id": 0}, {"card_id": True}, {"item_id": ""}, {"definition": None},
    {"definition": "A" * 64}, {"definition": "a" * 63},
])
def test_invalid_updates_fail_before_mutating_source(kwargs):
    source = state()
    with pytest.raises(ProtocolError) as caught:
        update(source, **kwargs)
    assert caught.value.code == "vote_update_invalid"
    assert source == state()


def test_limits_allow_updating_existing_row_without_allowing_new_identity(monkeypatch):
    monkeypatch.setattr(vote_state, "MAX_VOTE_ROWS", 1)
    source = update(state(), count=2**31 - 1)
    assert read_vote_history(source)[0]["count"] == 2**31 - 1
    assert update(source, count=2**31 - 1) == source
    with pytest.raises(ProtocolError) as caught:
        update(source, card_id=2)
    assert caught.value.code == "vote_history_capacity"
    reject_state(state([row(), row(card=2)]))


@pytest.mark.parametrize("unit", ["a", "投"])
def test_new_rows_need_items_that_fit_the_native_cell(unit):
    fitting = unit * (MAX_ITEM_NAME_BYTES // len(unit.encode("utf-8")))
    oversized = fitting + "a" * (STORED_ITEM_NAME_BYTES - len(fitting.encode("utf-8")))
    assert (len(fitting.encode("utf-8")), len(oversized.encode("utf-8"))) == (MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES)
    assert read_vote_history(update(state(), item_id=fitting))[0]["itemId"] == fitting
    source = state([row()])
    before = deepcopy(source)
    with pytest.raises(ProtocolError) as caught:
        update(source, item_id=oversized)
    assert (caught.value.code, caught.value.status) == ("vote_item_unreplayable", 503)
    assert source == before


def test_stored_rows_keep_the_earlier_item_bound_and_can_still_be_updated():
    stored = "a" * STORED_ITEM_NAME_BYTES
    source = state([row(item=stored, count=2)])
    before = deepcopy(source)
    assert vote_history_response(source).children[-1].values == (stored,)
    updated = update(source, item_id=stored, count=5)
    assert read_vote_history(updated)[0] == {**row(item=stored), "count": 5}
    assert updated["vote_state"]["entries"][str(vote_key(stored, 1))] == {
        "definition": DEFINITION, "baseline_count": 2, "count": 5}
    assert source == before

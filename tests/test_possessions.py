from copy import deepcopy
import struct
import zlib

import lz4.frame
import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.assets import asset_key
from aigis_ps.errors import ProtocolError, caught
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.protocol import NATIVE_CELL_BYTES


GIFTS = (
    ("gift_0002_ruby", 1832285076, 999),
    ("gift_0003_diamond", -1016865595, 999),
    ("gift_1000_beer", -1557561988, 999),
    ("gift_1002_fineLiquor", 134016284, 999),
)
DAMAGED_ROWS = (None, {}, [{"ItemID": GIFTS[0][0]}], [{"ItemID": GIFTS[0][0], "Count": 1, "Unknown": 1}],
                [{"ItemID": "unknown", "Count": 1}], [{"ItemID": 1832285076, "Count": 1}],
                [{"ItemID": GIFTS[0][0], "Count": 1}] * 2)
UNSUPPORTED_SAVES = ({}, {"bootstrap_schema": True, "tables": {"possessions": []}},
                     {"bootstrap_schema": 2, "tables": {"possessions": []}},
                     {"bootstrap_schema": 1, "tables": {}}, {"bootstrap_schema": 1, "tables": []})
UNKNOWN_ITEMS = ("unknown", "gift_1002_fineliquor", GIFTS[0][1], GIFTS[0][0].encode(), None, [GIFTS[0][0]])
UNKNOWN_ITEM_ERROR = ("possession_item_unknown", "Possession ItemID is not a known canonical definition")


def item_table(rows=GIFTS, *, stack_field="Stack"):
    fields = (("ItemID", 0, 32, b""), ("ID", 4, 1, b"\0" * 8), (stack_field, 8, 1, b"\0" * 8))
    schema = bytearray(struct.pack("<4sBBHH", b"ALRD", 1, 0, len(fields), 12))
    for name, offset, kind, parameters in fields:
        encoded = name.encode()
        schema += struct.pack("<HBBBB", offset, kind, len(parameters), len(encoded), 0) + encoded + b"\0\0"
        schema += b"\0" * (-len(schema) % 4) + parameters
        schema += b"\0" * (-len(schema) % 4)
    records, pool = bytearray(), bytearray()
    for name, identifier, stack in rows:
        records += struct.pack("<Iii", len(pool), identifier, stack)
        pool += name.encode() + b"\0"
    start = 28 + len(schema)
    return struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, len(rows), 28, start, 12, len(pool), start + len(records), b"item") + schema + records + pool


@pytest.fixture
def service(tmp_path):
    (tmp_path / asset_key("ItemConfigTable.atb")).write_bytes(item_table())
    return PossessionService(tmp_path)


@pytest.fixture
def state():
    return {"bootstrap_schema": 1, "tables": {"possessions": [], "config": [{"MenuTutorialFlags": 7}]},
            "player_state": {"units": [{"GetIndex": 9}]}, "unrelated": [1, 2]}


def test_item_definitions_and_absolute_response_match_native_strings(service, state):
    assert service.status()["ready"] is True
    assert service.status()["count"] == service.status()["creditable_count"] == 4
    assert service.response(state) == Container("POSSESSION")
    updated = service.credit(state, {name: index + 1 for index, (name, _, _) in enumerate(GIFTS)})
    response = service.response(updated)
    assert response.children == (Column("ItemID", "S", tuple(name for name, _, _ in GIFTS)), Column("Count", "I", (1, 2, 3, 4)),
                                 Column("Enabled", "I", (0, 0, 0, 0)))
    assert loads(dumps(Container(children=(response,)))).children == (response,)
    assert updated["tables"]["possessions"][1]["ItemID"] == "gift_0003_diamond"
    assert state["tables"]["possessions"] == []


def test_credit_copies_state_and_preserves_untouched_items(service, state):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[0][0], "Count": 5}, {"ItemID": GIFTS[2][0], "Count": 3}]
    before = deepcopy(state)
    updated = service.credit(state, {GIFTS[0][0]: 2, GIFTS[3][0]: 1})
    assert updated["tables"]["possessions"] == [{"ItemID": GIFTS[0][0], "Count": 7},
                                                 {"ItemID": GIFTS[2][0], "Count": 3}, {"ItemID": GIFTS[3][0], "Count": 1}]
    assert service.response(updated, [GIFTS[0][0]]).children[1].values == (7,)
    updated["tables"]["config"][0]["MenuTutorialFlags"] = 99
    updated["player_state"]["units"][0]["GetIndex"] = 1
    assert state == before
    unchanged = service.credit(state, {})
    assert unchanged == state and unchanged is not state
    assert unchanged["tables"] is not state["tables"]


def test_selected_items_return_authoritative_zero_and_requested_order(service, state):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[0][0], "Count": 5}]
    response = service.response(state, (name for name in (GIFTS[2][0], GIFTS[0][0])))
    assert response.children == (Column("ItemID", "S", (GIFTS[2][0], GIFTS[0][0])), Column("Count", "I", (0, 5)),
                                 Column("Enabled", "I", (0, 0)))
    assert service.response(state, []) == Container("POSSESSION")
    before = deepcopy(state)
    assert service.response(state) == service.response(state)
    assert state == before


@pytest.mark.parametrize("selection", ["gift_0002_ruby", 1, ["unknown"], [1832285076], ["gift_1002_fineliquor"], [GIFTS[0][0], GIFTS[0][0]]])
def test_invalid_selection_is_explicit(service, state, selection):
    with pytest.raises(ProtocolError) as error:
        service.response(state, selection)
    assert error.value.status == 400


@pytest.mark.parametrize("rows", DAMAGED_ROWS)
def test_invalid_stored_rows_are_not_discarded(service, state, rows):
    state["tables"]["possessions"] = rows
    before = deepcopy(state)
    for action in (lambda: service.response(state), lambda: service.credit(state, {GIFTS[0][0]: 1}),
                   lambda: service.debit(state, {GIFTS[0][0]: 1})):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.status == 503
        assert state == before


@pytest.mark.parametrize("count", [True, -1, 0x80000000, "1", 1.5])
def test_invalid_stored_counts_are_rejected(service, state, count):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[0][0], "Count": count}]
    before = deepcopy(state)
    for action in (lambda: service.response(state), lambda: service.credit(state, {}), lambda: service.debit(state, {})):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "possession_state_invalid"
        assert state == before


@pytest.mark.parametrize("invalid", UNSUPPORTED_SAVES)
def test_unsupported_storage_schema_is_not_initialized(service, invalid):
    before = deepcopy(invalid)
    for action in (lambda: service.response(invalid), lambda: service.credit(invalid, {GIFTS[0][0]: 1}),
                   lambda: service.debit(invalid, {GIFTS[0][0]: 1})):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.status == 503
        assert invalid == before


@pytest.mark.parametrize("grants", [[], {GIFTS[0][0]: True}, {GIFTS[0][0]: 0}, {GIFTS[0][0]: -1},
                                    {GIFTS[0][0]: 0x80000000}, {GIFTS[0][0]: "1"}, {"unknown": 1}, {1832285076: 1}])
def test_invalid_credit_cannot_mutate_state(service, state, grants):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        service.credit(state, grants)
    assert error.value.status == 400
    assert state == before


def test_local_stack_policy_preserves_old_counts_but_rejects_new_overflow(service, state):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[0][0], "Count": 1000}, {"ItemID": GIFTS[2][0], "Count": 0}]
    before = deepcopy(state)
    assert service.response(state).children[1].values == (1000, 0)
    with pytest.raises(ProtocolError) as error:
        service.credit(state, {GIFTS[2][0]: 1, GIFTS[0][0]: 1})
    assert error.value.code == "possession_stack_exceeded"
    assert state == before
    state["tables"]["possessions"][0]["Count"] = 998
    assert service.credit(state, {GIFTS[0][0]: 1})["tables"]["possessions"][0]["Count"] == 999


def test_zero_stack_can_be_read_without_guessing_credit_semantics(tmp_path, state):
    name = "unknown_stack_item"
    identifier = (zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000
    service = PossessionService(tmp_path, asset_reader=lambda _: item_table(((name, identifier, 0),)))
    assert service.status()["ready"] is True and service.status()["unsupported_stack_count"] == 1
    state["tables"]["possessions"] = [{"ItemID": name, "Count": 1234}]
    assert service.response(state).children[1].values == (1234,)
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        service.credit(state, {name: 1})
    assert error.value.code == "possession_stack_unsupported" and error.value.status == 503
    assert state == before


def test_credit_rejects_int32_total_overflow(tmp_path, state):
    name, identifier, _ = GIFTS[0]
    service = PossessionService(tmp_path, asset_reader=lambda _: item_table(((name, identifier, 0x7FFFFFFF),)))
    state["tables"]["possessions"] = [{"ItemID": name, "Count": 0x7FFFFFFF}]
    with pytest.raises(ProtocolError) as error:
        service.credit(state, {name: 1})
    assert error.value.code == "possession_stack_exceeded"
    assert state["tables"]["possessions"][0]["Count"] == 0x7FFFFFFF


@pytest.mark.parametrize("data", [b"bad", "not bytes", item_table(()), item_table((GIFTS[0], GIFTS[0])),
                                  item_table(((GIFTS[0][0], 1, 999),)), item_table(((GIFTS[0][0], GIFTS[0][1], -1),)),
                                  item_table(stack_field="Other")])
def test_invalid_definition_resource_is_unavailable(tmp_path, state, data):
    service = PossessionService(tmp_path, asset_reader=lambda _: data)
    assert service.status()["ready"] is False and service.status()["count"] == 0
    with pytest.raises(ProtocolError) as error:
        service.response(state)
    assert error.value.code == "possession_definitions_unavailable"


def test_missing_resource_and_indexed_compressed_reader(tmp_path):
    assert PossessionService(tmp_path).status()["ready"] is False
    raw = item_table()
    packed = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    calls = []
    service = PossessionService(tmp_path / "absent", asset_reader=lambda name: calls.append(name) or packed)
    assert service.status()["ready"] is True and calls == ["ItemConfigTable.atb"]


def test_debit_copies_full_state_and_preserves_zero_and_untouched_rows(service, state):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[0][0], "Count": 7},
                                        {"ItemID": GIFTS[1][0], "Count": 2},
                                        {"ItemID": GIFTS[2][0], "Count": 3}]
    before = deepcopy(state)
    costs = {GIFTS[0][0]: 4, GIFTS[1][0]: 2}
    updated = service.debit(state, costs)
    expected = deepcopy(state)
    expected["tables"]["possessions"][0]["Count"] = 3
    expected["tables"]["possessions"][1]["Count"] = 0
    assert updated == expected
    assert service.response(updated, costs).children == (
        Column("ItemID", "S", tuple(costs)), Column("Count", "I", (3, 0)), Column("Enabled", "I", (0, 0)))
    assert loads(dumps(Container(children=(service.response(updated),)))).children[0] == service.response(updated)
    updated["tables"]["possessions"][0]["Count"] = 99
    updated["tables"]["config"][0]["MenuTutorialFlags"] = 99
    updated["player_state"]["units"][0]["GetIndex"] = 99
    updated["unrelated"].append(99)
    assert state == before and costs == {GIFTS[0][0]: 4, GIFTS[1][0]: 2}


@pytest.mark.parametrize("count", [None, 0, 4])
def test_insufficient_debit_fails_whole_batch(service, state, count):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[0][0], "Count": 9}]
    if count is not None:
        state["tables"]["possessions"].append({"ItemID": GIFTS[1][0], "Count": count})
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        service.debit(state, {GIFTS[0][0]: 1, GIFTS[1][0]: 5})
    assert error.value.code == "possession_insufficient" and error.value.status == 409
    assert state == before


@pytest.mark.parametrize("amount", [True, False, 0, -1, -0x80000000, 0x80000000, 1.5, "1", None])
def test_invalid_debit_amount_cannot_mutate_state(service, state, amount):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[0][0], "Count": 9}]
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        service.debit(state, {GIFTS[0][0]: amount})
    assert error.value.code == "possession_cost_invalid" and error.value.status == 400
    assert state == before


@pytest.mark.parametrize("costs", [None, [], (), "gift_0002_ruby", {f"item_{i}": 1 for i in range(4097)}])
def test_invalid_debit_mapping_cannot_mutate_state(service, state, costs):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        service.debit(state, costs)
    assert error.value.code == "possession_cost_invalid"
    assert state == before


@pytest.mark.parametrize("item_id", ["unknown", 1832285076, "gift_1002_fineliquor"])
def test_unknown_debit_item_fails_whole_batch(service, state, item_id):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[0][0], "Count": 9}]
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        service.debit(state, {GIFTS[0][0]: 1, item_id: 1})
    assert error.value.code == "possession_item_unknown" and error.value.status == 400
    assert state == before


@pytest.mark.parametrize("count", [0, 0x7FFFFFFF])
def test_empty_debit_is_a_deep_copy(service, state, count):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[0][0], "Count": count}]
    before = deepcopy(state)
    updated = service.debit(state, {})
    assert updated == state and updated is not state
    updated["tables"]["possessions"][0]["Count"] = 1
    updated["unrelated"].append(3)
    assert state == before


@pytest.mark.parametrize("stack,count,cost", [(999, 1001, 1), (0, 1234, 234), (0, 1, 1), (999, 0x7FFFFFFF, 0x7FFFFFFF)])
def test_debit_accepts_old_counts_above_stack_and_zero_stack(tmp_path, state, stack, count, cost):
    name, identifier, _ = GIFTS[0]
    service = PossessionService(tmp_path, asset_reader=lambda _: item_table(((name, identifier, stack),)))
    state["tables"]["possessions"] = [{"ItemID": name, "Count": count}]
    before = deepcopy(state)
    updated = service.debit(state, {name: cost})
    assert updated["tables"]["possessions"] == [{"ItemID": name, "Count": count - cost}]
    assert service.response(updated).children[1].values == (count - cost,)
    assert state == before
    with pytest.raises(ProtocolError) as error:
        service.credit(state, {name: 1})
    assert error.value.code == ("possession_stack_unsupported" if stack == 0 else "possession_stack_exceeded")


def test_debit_rejects_oversized_stored_collection(service, state):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[0][0], "Count": 1}] * 4097
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        service.debit(state, {})
    assert error.value.code == "possession_state_invalid" and error.value.status == 503
    assert state == before


def test_debit_requires_available_definitions_even_when_empty(tmp_path, state):
    service = PossessionService(tmp_path)
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        service.debit(state, {})
    assert error.value.code == "possession_definitions_unavailable" and error.value.status == 503
    assert state == before


def test_capped_battle_credit_treats_zero_stack_as_unbounded(tmp_path, state):
    ticket = "item_smn_ticket_test"
    ticket_hash = (zlib.crc32(ticket.encode()) ^ 0x80000000) - 0x80000000
    gift, gift_hash, _ = GIFTS[0]
    service = PossessionService(tmp_path, asset_reader=lambda _: item_table(((ticket, ticket_hash, 0), (gift, gift_hash, 5))))
    state["tables"]["possessions"] = [{"ItemID": ticket, "Count": 0x7FFFFFFF - 7}, {"ItemID": gift, "Count": 4}]
    before = deepcopy(state)
    updated, discarded = service.credit_capped(state, {ticket: 10, gift: 3})
    assert service.counts(updated) == {ticket: 0x7FFFFFFF, gift: 5}
    assert discarded == {ticket: 3, gift: 2} and state == before


STORED_NAME = "x" * STORED_ITEM_NAME_BYTES
CELL_NAME = "y" * MAX_ITEM_NAME_BYTES


def definition(name, stack=999):
    return name, (zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000, stack


def test_new_rows_need_names_within_the_native_cell(tmp_path, state):
    assert MAX_ITEM_NAME_BYTES == NATIVE_CELL_BYTES == 255 and STORED_ITEM_NAME_BYTES == 256
    service = PossessionService(tmp_path, asset_reader=lambda _: item_table((definition(STORED_NAME), definition(CELL_NAME))))
    status = service.status()
    assert status["ready"] is True and status["count"] == 2 and status["unreplayable_count"] == 1
    credited = service.credit(state, {CELL_NAME: 2})
    assert service.startup_response(credited).children[0].values == (CELL_NAME,)
    before = deepcopy(state)
    for action in (lambda: service.credit(state, {STORED_NAME: 1}), lambda: service.credit_capped(state, {STORED_NAME: 1}),
                   lambda: service.credit(state, {CELL_NAME: 1, STORED_NAME: 1})):
        with pytest.raises(ProtocolError) as error:
            action()
        assert (error.value.code, error.value.status) == ("possession_item_unreplayable", 503)
    assert state == before


def test_stored_rows_past_the_native_cell_keep_loading_and_counting(tmp_path, state):
    service = PossessionService(tmp_path, asset_reader=lambda _: item_table((definition(STORED_NAME),)))
    state["tables"]["possessions"] = [{"ItemID": STORED_NAME, "Count": 5}]
    before = deepcopy(state)
    assert service.startup_response(state).children[0].values == (STORED_NAME,)
    assert service.credit(state, {STORED_NAME: 1})["tables"]["possessions"] == [{"ItemID": STORED_NAME, "Count": 6}]
    updated, discarded = service.credit_capped(state, {STORED_NAME: 2})
    assert updated["tables"]["possessions"] == [{"ItemID": STORED_NAME, "Count": 7}] and discarded == {}
    assert service.debit(state, {STORED_NAME: 5})["tables"]["possessions"] == [{"ItemID": STORED_NAME, "Count": 0}]
    assert service.set_enabled(state, {STORED_NAME: 1})["tables"]["possessions"][0]["Enabled"] == 1
    assert state == before


def test_definition_names_past_the_stored_bound_make_definitions_unavailable(tmp_path):
    service = PossessionService(tmp_path, asset_reader=lambda _: item_table((definition("z" * (STORED_ITEM_NAME_BYTES + 1)),)))
    assert service.status()["ready"] is False and service.status()["count"] == 0


def rejection(action):
    error = caught(action)
    return None if error is None else (error.code, str(error), error.status)


@pytest.mark.parametrize("item_id,expected", [*((name, True) for name, _, _ in GIFTS),
                                              *((item_id, False) for item_id in UNKNOWN_ITEMS)])
def test_known_names_exactly_the_items_stack_accepts(service, item_id, expected):
    assert service.known(item_id) is expected
    assert (rejection(lambda: service.stack(item_id)) is None) is expected


def test_unavailable_definitions_know_and_stack_no_items(tmp_path):
    service = PossessionService(tmp_path, asset_reader=lambda _: b"bad")
    for name, _, _ in GIFTS:
        assert service.known(name) is False
        assert rejection(lambda: service.stack(name)) == (*UNKNOWN_ITEM_ERROR, 400)


def test_stack_returns_each_definition_limit_including_zero(tmp_path):
    rows = (definition("stack_zero", 0), definition("stack_one", 1), definition("stack_full"))
    service = PossessionService(tmp_path, asset_reader=lambda _: item_table(rows))
    assert [service.stack(name) for name, _, _ in rows] == [0, 1, 999]


@pytest.mark.parametrize("stored,status", [(False, 400), (True, 503)], ids=["request", "stored"])
@pytest.mark.parametrize("item_id", UNKNOWN_ITEMS)
def test_stack_rejects_unknown_items_with_the_request_or_stored_status(service, item_id, stored, status):
    assert rejection(lambda: service.stack(item_id, stored=stored)) == (*UNKNOWN_ITEM_ERROR, status)


def test_counts_are_a_detached_copy_in_save_order(service, state):
    state["tables"]["possessions"] = [{"ItemID": GIFTS[2][0], "Count": 3}, {"ItemID": GIFTS[0][0], "Count": 0, "Enabled": 1},
                                      {"ItemID": GIFTS[1][0], "Count": 0x7FFFFFFF}]
    before = deepcopy(state)
    response = service.response(state)
    item_ids, counts = response.children[0].values, response.children[1].values
    current = service.counts(state)
    assert tuple(current) == item_ids and tuple(current.values()) == counts == (3, 0, 0x7FFFFFFF)
    current[GIFTS[2][0]] = 99
    current[GIFTS[3][0]] = 1
    assert state == before
    assert service.counts(state) == dict(zip(item_ids, counts))
    assert service.counts({**state, "tables": {"possessions": []}}) == {}


@pytest.mark.parametrize("rows", DAMAGED_ROWS)
def test_counts_reject_damaged_rows_like_the_inventory_response(service, state, rows):
    state["tables"]["possessions"] = rows
    before = deepcopy(state)
    expected = rejection(lambda: service.response(state))
    assert expected is not None and expected[2] == 503
    assert rejection(lambda: service.counts(state)) == expected
    assert state == before


@pytest.mark.parametrize("invalid", UNSUPPORTED_SAVES)
def test_counts_reject_unsupported_saves_like_the_inventory_response(service, invalid):
    expected = rejection(lambda: service.response(invalid))
    assert expected is not None and (expected[0], expected[2]) == ("possession_state_invalid", 503)
    assert rejection(lambda: service.counts(invalid)) == expected


def test_readiness_is_the_status_readiness_and_gates_stored_rows(service, state, tmp_path):
    broken = PossessionService(tmp_path, asset_reader=lambda _: b"bad")
    assert service.is_ready() is service.status()["ready"] is True
    assert broken.is_ready() is broken.status()["ready"] is False
    unavailable = ("possession_definitions_unavailable", "Verified item definitions are unavailable", 503)
    assert rejection(lambda: broken.counts(state)) == unavailable
    service.issues["item_definitions"] = "Item resource changed"
    assert service.is_ready() is service.status()["ready"] is False
    assert rejection(lambda: service.counts(state)) == rejection(lambda: service.response(state)) == unavailable

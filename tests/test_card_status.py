from copy import deepcopy
from dataclasses import replace
import random

import pytest

from aigis_ps.alxml import Column, Container, ValueType, dumps, loads
from aigis_ps.card_status import (
    CARD_STATUS_FIELDS, CARD_STATUS_LIMITS, FIRST_EPISODE, SECOND_EPISODE, THIRD_EPISODE, CardStatusService,
)
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterSnapshot
from aigis_ps.protocol import encode_response


def snapshot(identifiers=(1, 4, 67), *, columns=None):
    if columns is None:
        columns = (Column("CardID", "I", identifiers), *(Column(name, "I", (0,) * len(identifiers))
                    for name in ("InitClassID", "ClassLV0SkillID", "ClassLV1SkillID")))
    return MasterSnapshot("card-status-fixture", "a" * 64,
                          {"CardData": dumps(Container(children=columns)), "QuestTable": b"<DA></DA>"})


@pytest.fixture
def service():
    return CardStatusService(snapshot())


def row(identifier, **changes):
    result = dict.fromkeys(CARD_STATUS_FIELDS, 0)
    result.update(CardID=identifier, **changes)
    return result


def state(*rows, version=1):
    result = {"tables": {"card_status": list(rows), "other": [{"nested": [5]}]},
              "player_state": {"units": []}}
    if version is not None:
        result["card_status_schema"] = version
    return result


def unpack(container):
    return [dict(zip((column.name for column in container.children), values))
            for values in zip(*(column.values for column in container.children))]


def test_partial_patch_preserves_complete_rows_and_unrelated_state(service):
    original = state(row(4, A1=19, A3=-3, A5=31, A7=0x40000001, B0=1730455000, B3=1730460000), row(67, B4=99))
    before = deepcopy(original)
    changed = service.update(original, [{"CardID": 4, "A7": 0x40000003}])
    assert original == before
    assert changed["tables"]["card_status"][0] == {**before["tables"]["card_status"][0], "A7": 0x40000003}
    assert changed["tables"]["card_status"][1] == before["tables"]["card_status"][1]
    changed["tables"]["other"][0]["nested"].append(6)
    changed["player_state"]["units"].append({"CardID": 99})
    assert original == before
    response = service.response(changed, [4])
    assert response.name == "CardStatus" and tuple(column.name for column in response.children) == CARD_STATUS_FIELDS
    assert all(column.type is ValueType.INT for column in response.children)
    assert unpack(response) == [changed["tables"]["card_status"][0]]


def test_new_record_defaults_only_missing_fields_and_does_not_grant_a_unit(service):
    original = state(version=None)
    changed = service.update(original, [{"CardID": 4, "A7": 1}])
    assert changed["card_status_schema"] == 1
    assert service.read(changed) == [row(4, A7=1)]
    assert changed["player_state"] == {"units": []}
    assert "card_status_schema" not in original and original["tables"]["card_status"] == []


def test_read_is_detached_and_selection_retains_requested_order(service):
    original = state(row(4, A1=9), row(67, A7=4))
    records = service.read(original)
    records[0]["A1"] = 100
    records.append(row(1))
    assert original["tables"]["card_status"] == [row(4, A1=9), row(67, A7=4)]
    assert unpack(service.response(original, (67, 4))) == [row(67, A7=4), row(4, A1=9)]
    assert unpack(service.response(original, [])) == []


def test_flat_response_roundtrips_complete_records_without_default_wrapper(service):
    original = state(row(4, A7=3, B0=1730455000), row(67, A3=-1))
    response = service.flat_response(original)
    decoded = loads(response, limits=CARD_STATUS_LIMITS)
    assert decoded.name == "DA"
    assert tuple(column.name for column in decoded.children) == CARD_STATUS_FIELDS
    assert unpack(decoded) == original["tables"]["card_status"]
    assert b"default" not in response and b"CardStatus" not in response


@pytest.mark.parametrize("version", [None, 1])
def test_empty_legacy_and_current_state_are_accepted_without_write(service, version):
    original = state(version=version)
    before = deepcopy(original)
    assert service.read(original) == []
    assert unpack(loads(service.flat_response(original))) == []
    changed = service.update(original, [])
    assert changed == original == before and changed is not original
    assert changed["tables"] is not original["tables"]
    assert changed["tables"]["other"][0]["nested"] is not original["tables"]["other"][0]["nested"]


@pytest.mark.parametrize("version", [None, 0, 2, True, "1"])
def test_unknown_nonempty_schema_is_rejected_and_preserved(service, version):
    original = state(row(4), version=version)
    before = deepcopy(original)
    for action in (service.read, service.flat_response, lambda value: service.update(value, [{"CardID": 4, "A7": 1}])):
        with pytest.raises(ProtocolError) as error:
            action(original)
        assert error.value.code == "card_status_schema_unavailable"
    assert original == before


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_explicit_invalid_schema_is_not_silently_migrated_when_empty(service, version):
    with pytest.raises(ProtocolError) as error:
        service.update(state(version=version), [{"CardID": 4, "A7": 1}])
    assert error.value.code == "card_status_schema_unavailable"


@pytest.mark.parametrize("original", [None, {}, {"tables": []}, {"tables": {}}, {"tables": {"card_status": {}}},
    state({"CardID": 4, "A7": 1}), state(row(4), row(4)), state(row(999)), state({**row(4), "unknown": 2})])
def test_invalid_stored_records_are_rejected(service, original):
    with pytest.raises(ProtocolError) as error:
        service.read(original)
    assert error.value.code == "card_status_state_invalid"


@pytest.mark.parametrize("field", CARD_STATUS_FIELDS)
@pytest.mark.parametrize("value", [True, "1", 1.0, None, -0x80000001, 0x80000000])
def test_each_stored_field_requires_a_native_integer(service, field, value):
    original = state({**row(4), field: value})
    with pytest.raises(ProtocolError) as error:
        service.read(original)
    assert error.value.code == "card_status_state_invalid"


@pytest.mark.parametrize("patches", [None, {}, (), [None], [{}], [{"A7": 1}], [{"CardID": 4}],
    [{"CardID": 4, "unknown": 2}], [{"CardID": 999, "A7": 1}],
    [{"CardID": 4, "A7": 1}, {"CardID": 4, "B0": 9}]])
def test_invalid_patch_collections_are_atomic(service, patches):
    original = state(row(4, A7=1))
    before = deepcopy(original)
    with pytest.raises(ProtocolError) as error:
        service.update(original, patches)
    assert error.value.code == "card_status_patch_invalid"
    assert original == before


@pytest.mark.parametrize("value", [True, "1", 1.0, None, -0x80000001, 0x80000000])
def test_patch_values_cannot_coerce_into_integers(service, value):
    with pytest.raises(ProtocolError) as error:
        service.update(state(), [{"CardID": 4, "A7": value}])
    assert error.value.code == "card_status_patch_invalid"


def test_later_invalid_patch_cannot_partially_change_source(service):
    original = state(row(4, A7=1))
    before = deepcopy(original)
    with pytest.raises(ProtocolError):
        service.update(original, [{"CardID": 4, "A7": 7}, {"CardID": 67, "B0": True}])
    assert original == before


def test_story_episodes_are_the_native_a7_bits():
    assert (FIRST_EPISODE, SECOND_EPISODE, THIRD_EPISODE) == (1, 2, 4)


def test_raw_integer_boundaries_are_preserved_without_bit_or_timestamp_guessing(service):
    changes = {name: -0x80000000 if index % 2 else 0x7FFFFFFF for index, name in enumerate(CARD_STATUS_FIELDS[1:])}
    updated = service.update(state(), [{"CardID": 4, **changes}])
    assert service.read(updated) == [row(4, **changes)]
    assert service.update(updated, [{"CardID": 4, **changes}]) == updated


@pytest.mark.parametrize("selection", ["4", {4}, [True], [1.0], [4, 4], [0x80000000]])
def test_invalid_response_selection_is_rejected(service, selection):
    with pytest.raises(ProtocolError) as error:
        service.response(state(row(4)), selection)
    assert error.value.code == "card_status_selection_invalid"


@pytest.mark.parametrize("selection", [[67], [999], [0], [-1]])
def test_selecting_absent_record_does_not_create_one(service, selection):
    original = state(row(4))
    with pytest.raises(ProtocolError) as error:
        service.response(original, selection)
    assert error.value.code == "card_status_not_found"
    assert original["tables"]["card_status"] == [row(4)]


@pytest.mark.parametrize("identifier_column", [
    Column("CardID", "I", (4, 4)),
    Column("CardID", "S", ("4",)),
    Column("CardID", "I", (0,)),
    Column("CardID", "I", ()),
])
def test_invalid_master_data_cannot_define_card_ids(identifier_column):
    columns = (identifier_column, *(Column(name, "I", (0,) * len(identifier_column.values))
               for name in ("InitClassID", "ClassLV0SkillID", "ClassLV1SkillID")))
    with pytest.raises(ProtocolError) as error:
        CardStatusService(snapshot(columns=columns))
    assert error.value.code == "card_status_master_invalid"


@pytest.mark.parametrize("columns", [
    (Column("CardID", "I", (4,)),),
    (Container("CardData", (Column("CardID", "I", (4,)),)),),
])
def test_incomplete_or_nested_master_table_is_rejected(columns):
    with pytest.raises(ProtocolError) as error:
        CardStatusService(snapshot(columns=columns))
    assert error.value.code == "card_status_master_invalid"


def test_service_freezes_snapshot_card_ids():
    documents = dict(snapshot().documents)
    fixed = MasterSnapshot("fixed", "a" * 64, documents)
    subject = CardStatusService(fixed)
    documents["CardData"] = snapshot((999,)).get_xml("CardData")
    assert subject.read(state(row(4))) == [row(4)]
    with pytest.raises(ProtocolError):
        subject.update(state(), [{"CardID": 999, "A7": 1}])


def test_capacity_is_preflighted_before_returning_any_updated_state(service):
    original = state(row(4, A7=1))
    before = deepcopy(original)
    service._response_profile = replace(service._response_profile, response_limit=1)
    with pytest.raises(ProtocolError) as error:
        service.update(original, [{"CardID": 4, "A7": 2}])
    assert error.value.code == "card_status_capacity_exceeded" and original == before


def test_full_snapshot_preflight_applies_even_when_one_selected_row_is_small():
    identifiers = tuple(range(1, 10001))
    subject = CardStatusService(snapshot(identifiers))
    rng = random.Random(151)
    original = state(*(row(identifier, **{name: rng.randrange(-0x80000000, 0x80000000)
                                         for name in CARD_STATUS_FIELDS[1:]}) for identifier in identifiers))
    before = deepcopy(original)
    assert len(unpack(subject.response(original, [1]))) == 1
    with pytest.raises(ProtocolError) as error:
        subject.update(original, [{"CardID": 1, "A7": 1}])
    assert error.value.code == "card_status_capacity_exceeded" and original == before


def test_real_native_capacity_accepts_small_complete_snapshot(service):
    updated = service.update(state(), [{"CardID": 4, "A7": 1}, {"CardID": 67, "B0": 1730455000}])
    wire, _ = encode_response(service._response_profile, service.flat_response(updated))
    assert len(wire) <= 0x20000

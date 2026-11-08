from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import Store
from aigis_ps.unit_detail_protocol import UnitDetailRequest, parse_unit_detail
from test_quest_service import request


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "protocol.sqlite3", {})


def message(store, *, index=1, display=0, toggle=1, extra=(), number=10, tick=1000):
    return request(store, number, (Container("PU", (Column("A7", "I", (index,)),
        Column("B0", "I", (display,)), Column("SC", "I", (toggle,)), *extra)),), tick=tick)


def test_real_profile_and_switch_semantics(store):
    envelope = message(store)
    assert envelope.profile.route == "i6ZYUWnd"
    parsed = parse_unit_detail(envelope)
    assert (parsed.get_index, parsed.display_type, parsed.toggle_skill) == (1, 0, True)
    assert parse_unit_detail(message(store, index=0, toggle=0)).toggle_skill is False
    with pytest.raises(FrozenInstanceError):
        parsed.get_index = 2


def test_selection_carries_no_client_clock_and_accepts_every_native_ct(store):
    assert [field.name for field in fields(UnitDetailRequest)] == ["get_index", "display_type", "toggle_skill"]
    for tick in (-0x80000000, -1, 0, 0x7FFFFFFF):
        assert parse_unit_detail(message(store, tick=tick)) == UnitDetailRequest(1, 0, True)


def test_common_ct_header_is_still_validated(store):
    envelope = message(store)
    root = deepcopy(envelope.document)
    root.find("CT/V").text = "later"
    with pytest.raises(ProtocolError):
        parse_unit_detail(replace(envelope, document=root))


@pytest.mark.parametrize("index,toggle", [(-1, 0), (1, -1), (1, 2), (1, 0x7FFFFFFF)])
def test_invalid_selection_is_rejected(store, index, toggle):
    with pytest.raises(ProtocolError):
        parse_unit_detail(message(store, index=index, toggle=toggle))


@pytest.mark.parametrize("extra", [Column("A7", "I", (1,)), Column("A4", "I", (1000000,)),
    Column("AE", "I", (4,)), Column("B1", "I", (0,)), Column("SC", "I", (0,))])
def test_duplicate_and_unsolicited_state_fields_are_rejected(store, extra):
    with pytest.raises(ProtocolError):
        parse_unit_detail(message(store, extra=(extra,)))


@pytest.mark.parametrize("field", ["A7", "B0", "SC"])
@pytest.mark.parametrize("values", [(), (1, 2)])
def test_columns_are_scalar_and_required(store, field, values):
    columns = {"A7": Column("A7", "I", (1,)), "B0": Column("B0", "I", (0,)), "SC": Column("SC", "I", (1,))}
    columns[field] = Column(field, "I", values)
    with pytest.raises(ProtocolError):
        parse_unit_detail(request(store, 10, (Container("PU", tuple(columns.values())),)))
    del columns[field]
    with pytest.raises(ProtocolError):
        parse_unit_detail(request(store, 10, (Container("PU", tuple(columns.values())),)))


@pytest.mark.parametrize("kind,value", [("B", True), ("S", "1"), ("F", 1.0)])
def test_switch_does_not_coerce_noninteger_types(store, kind, value):
    body = Container("PU", (Column("A7", "I", (1,)), Column("B0", "I", (0,)), Column("SC", kind, (value,))))
    with pytest.raises(ProtocolError):
        parse_unit_detail(request(store, 10, (body,)))


def test_other_registration_and_missing_body_are_rejected(store):
    with pytest.raises(ProtocolError):
        parse_unit_detail(message(store, number=119))
    with pytest.raises(ProtocolError):
        parse_unit_detail(request(store, 10, ()))

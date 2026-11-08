from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from xml.etree import ElementTree as ET

import pytest

from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import MAX_PLAYER_UNITS
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.sale_protocol import MAX_MANUAL_SALE_UNITS, SaleRequest, parse_sale
from aigis_ps.unit_reference import UNIT_REFERENCE_COLUMNS, UnitReference, parse_unit_references


def envelope(*, units=((0, 0, 2, 12, 100),), client_time=1, number=3):
    common = tuple(Column(name, kind, (value,)) for name, kind, value in (
        ("UI", "S", "1"), ("UK", "S", "local-account"), ("MK", "S", "local-method"),
        ("CT", "I", client_time), ("PFN", "S", "dmm-r18"), ("PlatformCode", "I", 1),
        ("GPVI", "S", "unkown"), ("GPOT", "S", "unkown")))
    rows = tuple((*row, row[2], row[3]) for row in units)
    append = Container("APPEND", tuple(Column(name, "I", tuple(row[index] for row in rows))
                                      for index, name in enumerate(UNIT_REFERENCE_COLUMNS)))
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[number]
    xml = prepare_request(dumps(Container(children=common + (append,))), profile.st)
    return registry.decode(profile.route, encode_wire(xml, profile.request_key))


def assert_rejected(message):
    before = ET.tostring(message.document)
    with pytest.raises(ProtocolError) as error:
        parse_sale(message)
    assert error.value.code == "invalid_request_fields" and error.value.status == 400
    assert ET.tostring(message.document) == before


@pytest.mark.parametrize("number,route,st", [(3, "pP8JgbjO", 1102), (4, "G4uBbPjZ", 1126)])
def test_native_profiles_preserve_order_and_are_immutable(number, route, st):
    rows = ((0, 0, 9, 22, 77), (0, 0, 2, 12, 533))
    message = envelope(number=number, units=rows, client_time=-0x80000000)
    before = ET.tostring(message.document)
    parsed = parse_sale(message)
    assert message.profile.route == route and message.profile.st == st
    assert parsed == SaleRequest(number, tuple(UnitReference(*row) for row in rows))
    assert ET.tostring(message.document) == before
    with pytest.raises(FrozenInstanceError):
        parsed.registration = 99
    with pytest.raises(FrozenInstanceError):
        parsed.units[0].card_id = 1


def test_training_public_exports_and_private_parser_alias_are_preserved():
    from aigis_ps import training_protocol
    assert training_protocol.UnitReference is UnitReference
    assert training_protocol._references is parse_unit_references


@pytest.mark.parametrize("number,maximum", [(3, MAX_MANUAL_SALE_UNITS), (4, MAX_PLAYER_UNITS)])
def test_exact_row_capacity_and_first_rejected_count(number, maximum):
    rows = tuple((0, 0, index, index + 1, 0x7fffffff) for index in range(maximum))
    parsed = parse_sale(envelope(number=number, units=rows, client_time=0x7fffffff))
    assert len(parsed.units) == maximum and parsed.units[-1] == UnitReference(*rows[-1])
    assert_rejected(envelope(number=number, units=rows + ((0, 0, maximum, maximum + 1, 1),)))


@pytest.mark.parametrize("number", [3, 4])
def test_empty_sale_is_rejected(number):
    assert_rejected(envelope(number=number, units=()))


@pytest.mark.parametrize("number", [3, 4])
@pytest.mark.parametrize("rows", [
    ((0, 0, 2, 12, 100), (0, 0, 2, 13, 101)),
    ((0, 0, 2, 12, 100), (0, 0, 3, 12, 101)),
    ((0, 0, 0, 12, 100), (0, 1, 0, 13, 101)),
])
def test_owned_invite_and_unique_ids_cannot_repeat(number, rows):
    assert_rejected(envelope(number=number, units=rows))


@pytest.mark.parametrize("storage_type", [1, 2, 3, -1, -0x80000000, 0x7fffffff])
def test_nonzero_storage_domains_and_repetitions_are_left_for_service(storage_type):
    row = (storage_type, 0, 0, 0, 77)
    assert parse_sale(envelope(units=(row, row))).units == (UnitReference(*row), UnitReference(*row))


def test_owned_zero_invite_and_nonzero_storage_index_are_preserved():
    row = (0, 2, 0, 1, 1)
    assert parse_sale(envelope(units=(row,))).units == (UnitReference(*row),)


@pytest.mark.parametrize("row", [
    (0, -1, 2, 12, 100), (0, 0, -1, 12, 100), (0, 0, 2, 0, 100),
    (0, 0, 2, -1, 100), (0, 0, 2, 12, 0), (0, 0, 2, 12, -1),
    (1, 1, 0, 0, 100), (1, 0, 1, 0, 100), (1, 0, 0, 1, 100),
    (2, 1, 0, 0, 100), (2, 0, 1, 0, 100), (2, 0, 0, 1, 100),
    (-1, 0, 1, 0, 100),
])
def test_reference_sign_and_aggregate_identity_constraints(row):
    assert_rejected(envelope(units=(row,)))


@pytest.mark.parametrize("field", ["BASE", "CMB", "Money", "Gold", "SellPrice", "Rainbow", "UNKNOWN"])
def test_client_cannot_submit_other_business_fields_or_reward_amounts(field):
    message = envelope()
    ET.SubElement(message.document, field, T="I")
    assert_rejected(message)


@pytest.mark.parametrize("path,mutation", [
    ("APPEND", "missing"), ("APPEND/CARDID", "missing"), ("APPEND/A7", "missing"),
    ("APPEND/A7", "alias_mismatch"), ("APPEND/AB", "alias_mismatch"),
    ("APPEND/CARDID", "unequal"), ("APPEND/STORAGETYPE", "wrong_type"),
    ("APPEND/CARDID", "overflow"), ("APPEND/STORAGETYPE", "underflow"),
    ("APPEND/UNIQUEID", "nondecimal"), ("APPEND/INVITEID", "whitespace"),
    ("APPEND", "duplicate"), ("APPEND/CARDID", "duplicate"),
    ("APPEND/CARDID", "empty"), ("APPEND", "attribute"),
    ("APPEND", "mixed_text"), ("APPEND", "mixed_tail"),
    ("APPEND/CARDID", "extra_attribute"), ("APPEND/CARDID", "mixed_text"),
    ("APPEND/CARDID", "nested"), ("APPEND/CARDID", "value_attribute"),
    ("APPEND/CARDID", "value_tail"), ("APPEND/CARDID", "wrong_value_tag"),
    ("CT", "missing"), ("CT", "wrong_type"), ("CT", "multiple"),
    ("PFN", "multiple"), ("PlatformCode", "overflow"), ("UI", "wrong_type"),
    ("UK", "missing"), ("MK", "multiple"), ("GPVI", "missing"),
    ("GPOT", "missing"), ("VN", "nested"), ("ST", "missing"), ("HS", "attribute"),
])
def test_malformed_columns_and_common_headers_are_rejected_without_mutation(path, mutation):
    message = envelope()
    node = message.document.find(path)
    parent = message.document.find(path.rsplit("/", 1)[0]) if "/" in path else message.document
    if mutation == "missing":
        parent.remove(node)
    elif mutation == "duplicate":
        parent.append(deepcopy(node))
    elif mutation == "wrong_type":
        node.set("T", "S" if node.get("T") != "S" else "I")
    elif mutation == "attribute":
        node.set("T", "I")
    elif mutation == "extra_attribute":
        node.set("extra", "1")
    elif mutation == "mixed_text":
        node.text = "unexpected"
    elif mutation == "mixed_tail":
        node.tail = "unexpected"
    elif mutation == "empty":
        node.remove(node[0])
    elif mutation in ("multiple", "unequal"):
        ET.SubElement(node, "V").text = "1"
    elif mutation == "nested":
        ET.SubElement(node[0] if len(node) else node, "V").text = "1"
    elif mutation == "value_attribute":
        node[0].set("T", "I")
    elif mutation == "value_tail":
        node[0].tail = "unexpected"
    elif mutation == "wrong_value_tag":
        node[0].tag = "VALUE"
    else:
        node[0].text = {"alias_mismatch": "99", "overflow": "2147483648", "underflow": "-2147483649",
                        "nondecimal": "1.0", "whitespace": " 1"}[mutation]
    assert_rejected(message)


def test_unknown_reference_column_is_rejected():
    message = envelope()
    ET.SubElement(message.document.find("APPEND"), "UNKNOWN", T="I")
    assert_rejected(message)


@pytest.mark.parametrize("mutation", ["root_name", "root_attribute", "root_text", "root_tail",
                                     "direct_profile", "other_registration", "mixed_registrations",
                                     "duplicate_registration", "empty_registration"])
def test_profile_and_root_are_strict(mutation):
    message = envelope()
    if mutation == "root_name":
        message.document.tag = "RT"
    elif mutation == "root_attribute":
        message.document.set("T", "I")
    elif mutation == "root_text":
        message.document.text = "unexpected"
    elif mutation == "root_tail":
        message.document.tail = "unexpected"
    else:
        changes = {"direct_profile": {"name": "quest_exit"}, "other_registration": {"registration_keys": (5,)},
                   "mixed_registrations": {"registration_keys": (3, 4)},
                   "duplicate_registration": {"registration_keys": (3, 3)},
                   "empty_registration": {"registration_keys": ()}}[mutation]
        message = replace(message, profile=replace(message.profile, **changes))
    assert_rejected(message)

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from xml.etree import ElementTree as ET

import pytest

from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.training_protocol import MAX_TRAINING_MATERIALS, TrainingRequest, parse_training
from aigis_ps.unit_reference import UNIT_REFERENCE_COLUMNS, UnitReference


def reference_table(name, rows):
    wire_rows = tuple((*row, row[2], row[3]) for row in rows)
    return Container(name, tuple(Column(key, "I", tuple(row[index] for row in wire_rows))
                                 for index, key in enumerate(UNIT_REFERENCE_COLUMNS)))


def envelope(*, base=((0, 0, 1, 11, 100),), materials=((0, 0, 2, 12, 100),), boost=(0, 0, 0),
             client_time=1, number=5):
    common = tuple(Column(name, kind, (value,)) for name, kind, value in (
        ("UI", "S", "1"), ("UK", "S", "local-account"), ("MK", "S", "local-method"),
        ("CT", "I", client_time), ("PFN", "S", "dmm-r18"), ("PlatformCode", "I", 1),
        ("GPVI", "S", "unkown"), ("GPOT", "S", "unkown")))
    body = (reference_table("BASE", base), reference_table("APPEND", materials),
            Container("CMB", tuple(Column(name, "I", (value,)) for name, value in zip(("A1", "A7", "B0"), boost))))
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[number]
    xml = prepare_request(dumps(Container(children=common + body)), profile.st)
    return registry.decode(profile.route, encode_wire(xml, profile.request_key))


def assert_rejected(message):
    before = ET.tostring(message.document)
    with pytest.raises(ProtocolError) as error:
        parse_training(message)
    assert error.value.code == "invalid_request_fields" and error.value.status == 400
    assert ET.tostring(message.document) == before


def test_training_preserves_native_fields_and_input():
    message = envelope(boost=(300, -1, 15), client_time=-0x80000000)
    before = ET.tostring(message.document)
    parsed = parse_training(message)
    assert parsed == TrainingRequest(UnitReference(0, 0, 1, 11, 100), (UnitReference(0, 0, 2, 12, 100),), 300, -1, 15)
    assert ET.tostring(message.document) == before
    with pytest.raises(FrozenInstanceError):
        parsed.boost_percent = 99
    with pytest.raises(FrozenInstanceError):
        parsed.base.card_id = 99


@pytest.mark.parametrize("storage_type", [1, 2, 3, -1, 0x7FFFFFFF])
def test_aggregate_and_unknown_storage_domains_preserve_repeated_rows(storage_type):
    row = (storage_type, 0, 0, 0, 200)
    parsed = parse_training(envelope(materials=(row, row)))
    assert parsed.materials == (UnitReference(*row), UnitReference(*row))


def test_get_index_zero_and_other_storage_index_are_left_for_business_validation():
    parsed = parse_training(envelope(base=((0, 2, 0, 1, 1),), materials=((0, 3, 5, 22, 100),)))
    assert parsed.base == UnitReference(0, 2, 0, 1, 1)
    assert parsed.materials == (UnitReference(0, 3, 5, 22, 100),)


def test_aggregate_base_is_not_confused_with_zero_index_owned_unit():
    parsed = parse_training(envelope(base=((1, 0, 0, 0, 100),), materials=((0, 0, 0, 1, 1),)))
    assert parsed.base.storage_type == 1 and parsed.materials[0].invite_id == 0


def test_ten_material_rows_and_int32_limits_are_preserved():
    rows = tuple((0, 0, index + 2, index + 12, 0x7FFFFFFF) for index in range(MAX_TRAINING_MATERIALS))
    parsed = parse_training(envelope(materials=rows, boost=(0x7FFFFFFF, 0x7FFFFFFF, 0x7FFFFFFF),
                                     client_time=0x7FFFFFFF))
    assert parsed.materials == tuple(UnitReference(*row) for row in rows)
    assert parsed.boost_percent == 0x7FFFFFFF


@pytest.mark.parametrize("materials", [
    (), tuple((0, 0, index + 2, index + 12, 100) for index in range(11)),
    ((0, 0, 2, 12, 100), (0, 0, 2, 13, 101)),
    ((0, 0, 2, 12, 100), (0, 0, 3, 12, 101)),
    ((0, 0, 1, 12, 100),), ((0, 0, 2, 11, 100),),
])
def test_invalid_material_count_duplicate_identity_or_target_overlap(materials):
    assert_rejected(envelope(materials=materials))


@pytest.mark.parametrize("base", [(), ((0, 0, 1, 11, 100), (0, 0, 3, 13, 100))])
def test_base_requires_exactly_one_row(base):
    assert_rejected(envelope(base=base))


@pytest.mark.parametrize("row", [
    (0, -1, 2, 12, 100), (0, 0, -1, 12, 100), (0, 0, 2, 0, 100),
    (0, 0, 2, -1, 100), (0, 0, 2, 12, 0), (0, 0, 2, 12, -1),
    (1, 1, 0, 0, 100), (1, 0, 1, 0, 100), (1, 0, 0, 1, 100),
    (2, 1, 0, 0, 100), (2, 0, 1, 0, 100), (2, 0, 0, 1, 100),
    (3, 0, 1, 0, 100),
])
def test_native_reference_sign_and_aggregate_identity_constraints(row):
    assert_rejected(envelope(materials=(row,)))


@pytest.mark.parametrize("boost", [(-1, 0, 0), (0, -2, 0), (0, 0, -1)])
def test_invalid_boost_range(boost):
    assert_rejected(envelope(boost=boost))


@pytest.mark.parametrize("path,mutation", [
    ("BASE", "missing"), ("APPEND", "missing"), ("CMB", "missing"),
    ("BASE/CARDID", "missing"), ("APPEND/STORAGEINDEX", "missing"), ("CMB/B0", "missing"),
    ("BASE/A7", "alias_mismatch"), ("BASE/AB", "alias_mismatch"),
    ("APPEND/A7", "alias_mismatch"), ("APPEND/AB", "alias_mismatch"),
    ("APPEND/CARDID", "unequal"), ("BASE/CARDID", "wrong_type"),
    ("APPEND/CARDID", "overflow"), ("BASE/CARDID", "underflow"),
    ("APPEND/UNIQUEID", "nondecimal"), ("APPEND/INVITEID", "whitespace"),
    ("BASE", "duplicate"), ("APPEND/CARDID", "duplicate"), ("CMB/A1", "duplicate"),
    ("CMB/A1", "empty"), ("CMB/A7", "multiple"), ("CMB/B0", "wrong_type"),
    ("BASE", "attribute"), ("APPEND", "mixed_text"), ("CMB", "mixed_tail"),
    ("BASE/CARDID", "extra_attribute"), ("CMB/B0", "mixed_text"),
    ("APPEND/CARDID", "nested"), ("BASE/CARDID", "value_attribute"),
    ("CMB/A7", "value_tail"), ("BASE/CARDID", "wrong_value_tag"),
    ("CT", "missing"), ("CT", "wrong_type"), ("PFN", "multiple"),
    ("PlatformCode", "overflow"), ("GPOT", "missing"), ("VN", "nested"),
])
def test_malformed_shape_types_and_aliases_are_rejected(path, mutation):
    message = envelope()
    node = message.document.find(path)
    parent = message.document.find(path.rsplit("/", 1)[0]) if "/" in path else message.document
    if mutation == "missing":
        parent.remove(node)
    elif mutation == "duplicate":
        parent.append(deepcopy(node))
    elif mutation == "wrong_type":
        node.set("T", "S")
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


@pytest.mark.parametrize("path", ["", "BASE", "APPEND", "CMB"])
def test_unknown_business_fields_are_rejected_at_every_container(path):
    message = envelope()
    ET.SubElement(message.document.find(path) if path else message.document, "UNKNOWN", T="I")
    assert_rejected(message)


@pytest.mark.parametrize("mutation", ["root_name", "root_attribute", "root_text", "root_tail",
                                     "direct_profile", "other_registration", "mixed_registrations", "duplicate_registration"])
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
        changes = {"direct_profile": {"name": "quest_exit"}, "other_registration": {"registration_keys": (3,)},
                   "mixed_registrations": {"registration_keys": (5, 3)},
                   "duplicate_registration": {"registration_keys": (5, 5)}}[mutation]
        message = replace(message, profile=replace(message.profile, **changes))
    assert_rejected(message)

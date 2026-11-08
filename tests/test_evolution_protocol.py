from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from xml.etree import ElementTree as ET

import pytest

from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.errors import ProtocolError
from aigis_ps.evolution_protocol import MAX_EVOLUTION_MATERIALS, EvolutionRequest, parse_evolution
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.unit_reference import UNIT_REFERENCE_COLUMNS, UnitReference


def reference_table(name, rows):
    wire_rows = tuple((*row, row[2], row[3]) for row in rows)
    return Container(name, tuple(Column(key, "I", tuple(row[index] for row in wire_rows))
                                 for index, key in enumerate(UNIT_REFERENCE_COLUMNS)))


def envelope(*, operation=7, base=((0, 0, 1, 11, 100),), materials=((0, 0, 2, 12, 200),),
             branch=None, client_time=1):
    common = tuple(Column(name, kind, (value,)) for name, kind, value in (
        ("UI", "S", "1"), ("UK", "S", "local-account"), ("MK", "S", "local-method"),
        ("CT", "I", client_time), ("PFN", "S", "dmm-r18"), ("PlatformCode", "I", 1),
        ("GPVI", "S", "unkown"), ("GPOT", "S", "unkown")))
    business = [reference_table("BASE", base)]
    if materials is not None:
        business.append(reference_table("APPEND", materials))
    if branch is not None:
        business.append(Column("TYPE", "I", (branch,)))
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[operation]
    xml = prepare_request(dumps(Container(children=common + tuple(business))), profile.st)
    return registry.decode(profile.route, encode_wire(xml, profile.request_key))


def assert_rejected(message):
    before = ET.tostring(message.document)
    with pytest.raises(ProtocolError) as caught:
        parse_evolution(message)
    assert caught.value.code == "invalid_request_fields" and caught.value.status == 400
    assert ET.tostring(message.document) == before


@pytest.mark.parametrize("operation,branch", [(7, None), (8, None), (9, 1), (9, 2)])
def test_native_operations_preserve_fields_and_input(operation, branch):
    message = envelope(operation=operation, branch=branch, client_time=-0x80000000)
    before = ET.tostring(message.document)
    parsed = parse_evolution(message)
    assert parsed == EvolutionRequest(operation, UnitReference(0, 0, 1, 11, 100),
                                      (UnitReference(0, 0, 2, 12, 200),), branch or 0)
    assert ET.tostring(message.document) == before
    with pytest.raises(FrozenInstanceError):
        parsed.operation = 9
    with pytest.raises(FrozenInstanceError):
        parsed.base.card_id = 200


@pytest.mark.parametrize("operation,branch", [(7, None), (8, None), (9, 1)])
def test_absent_material_container_means_zero_materials(operation, branch):
    parsed = parse_evolution(envelope(operation=operation, branch=branch, materials=None))
    assert parsed.materials == ()


@pytest.mark.parametrize("operation,branch", [(7, None), (8, None), (9, 2)])
@pytest.mark.parametrize("shape", ["no_columns", "empty_columns"])
def test_present_material_container_cannot_be_empty(operation, branch, shape):
    message = envelope(operation=operation, branch=branch, materials=())
    if shape == "no_columns":
        message.document.find("APPEND").clear()
    assert_rejected(message)


@pytest.mark.parametrize("operation,branch", [(7, None), (8, None), (9, 1)])
def test_four_materials_and_signed_client_time_boundary(operation, branch):
    rows = tuple((0, 0, index + 2, index + 12, 0x7FFFFFFF) for index in range(MAX_EVOLUTION_MATERIALS))
    parsed = parse_evolution(envelope(operation=operation, branch=branch, materials=rows, client_time=0x7FFFFFFF))
    assert parsed.materials == tuple(UnitReference(*row) for row in rows)


@pytest.mark.parametrize("storage_type", [1, 2, 3, -1, 0x7FFFFFFF])
def test_repeated_aggregate_references_and_unknown_domains_remain_explicit(storage_type):
    row = (storage_type, 0, 0, 0, 200)
    parsed = parse_evolution(envelope(materials=(row, row)))
    assert parsed.materials == (UnitReference(*row), UnitReference(*row))


def test_owned_domain_indexes_and_prince_zero_are_left_for_business_validation():
    parsed = parse_evolution(envelope(base=((0, 2, 0, 1, 1),), materials=((0, 3, 5, 22, 100),)))
    assert parsed.base == UnitReference(0, 2, 0, 1, 1)
    assert parsed.materials == (UnitReference(0, 3, 5, 22, 100),)


def test_aggregate_base_does_not_conflict_with_owned_zero_index():
    parsed = parse_evolution(envelope(base=((1, 0, 0, 0, 100),), materials=((0, 0, 0, 1, 1),)))
    assert parsed.base.storage_type == 1 and parsed.materials[0].invite_id == 0


def test_distinct_owned_instances_may_have_the_same_card():
    parsed = parse_evolution(envelope(materials=((0, 0, 2, 12, 100), (0, 0, 3, 13, 100))))
    assert len(parsed.materials) == 2


@pytest.mark.parametrize("materials", [
    tuple((0, 0, index + 2, index + 12, 100) for index in range(5)),
    ((0, 0, 2, 12, 100), (0, 0, 2, 13, 101)),
    ((0, 0, 2, 12, 100), (0, 0, 3, 12, 101)),
    ((0, 0, 1, 12, 100),), ((0, 0, 2, 11, 100),),
])
def test_material_limit_duplicate_identity_and_self_use(materials):
    assert_rejected(envelope(materials=materials))


@pytest.mark.parametrize("base", [(), ((0, 0, 1, 11, 100), (0, 0, 3, 13, 100))])
def test_base_requires_exactly_one_reference(base):
    assert_rejected(envelope(base=base))


@pytest.mark.parametrize("row", [
    (0, -1, 2, 12, 100), (0, 0, -1, 12, 100), (0, 0, 2, 0, 100),
    (0, 0, 2, -1, 100), (0, 0, 2, 12, 0), (0, 0, 2, 12, -1),
    (1, 1, 0, 0, 100), (1, 0, 1, 0, 100), (1, 0, 0, 1, 100),
    (2, 1, 0, 0, 100), (2, 0, 1, 0, 100), (2, 0, 0, 1, 100),
])
def test_shared_reference_constraints_are_applied(row):
    assert_rejected(envelope(materials=(row,)))


@pytest.mark.parametrize("branch", [None, -0x80000000, -1, 0, 3, 0x7FFFFFFF])
def test_second_awakening_requires_branch_one_or_two(branch):
    assert_rejected(envelope(operation=9, branch=branch))


@pytest.mark.parametrize("operation", [7, 8])
def test_other_operations_reject_branch_field(operation):
    assert_rejected(envelope(operation=operation, branch=1))


@pytest.mark.parametrize("path,mutation", [
    ("BASE", "missing"), ("BASE/CARDID", "missing"), ("APPEND/STORAGEINDEX", "missing"),
    ("TYPE", "missing"), ("BASE/A7", "alias_mismatch"), ("BASE/AB", "alias_mismatch"),
    ("APPEND/A7", "alias_mismatch"), ("APPEND/AB", "alias_mismatch"),
    ("APPEND/CARDID", "unequal"), ("BASE/CARDID", "wrong_type"),
    ("APPEND/CARDID", "overflow"), ("BASE/CARDID", "underflow"),
    ("APPEND/UNIQUEID", "nondecimal"), ("APPEND/INVITEID", "whitespace"),
    ("BASE", "duplicate"), ("APPEND/CARDID", "duplicate"), ("TYPE", "duplicate"),
    ("TYPE", "empty"), ("TYPE", "multiple"), ("TYPE", "wrong_type"),
    ("BASE", "attribute"), ("APPEND", "mixed_text"), ("TYPE", "mixed_tail"),
    ("BASE/CARDID", "extra_attribute"), ("TYPE", "mixed_text"),
    ("APPEND/CARDID", "nested"), ("BASE/CARDID", "value_attribute"),
    ("TYPE", "value_tail"), ("BASE/CARDID", "wrong_value_tag"),
    ("CT", "missing"), ("CT", "wrong_type"), ("PFN", "multiple"),
    ("PlatformCode", "overflow"), ("GPOT", "missing"), ("VN", "nested"),
])
def test_malformed_fields_fail_without_mutating_request(path, mutation):
    message = envelope(operation=9, branch=1)
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


@pytest.mark.parametrize("name", ["CMB", "MONEY", "UnknownMutation"])
def test_client_boost_money_and_unknown_root_fields_are_rejected(name):
    message = envelope(materials=None)
    ET.SubElement(message.document, name)
    assert_rejected(message)


@pytest.mark.parametrize("path", ["BASE", "APPEND"])
def test_unknown_reference_columns_are_rejected(path):
    message = envelope()
    ET.SubElement(message.document.find(path), "UNKNOWN", T="I")
    assert_rejected(message)


@pytest.mark.parametrize("mutation", ["root_name", "root_attribute", "root_text", "root_tail",
                                     "direct_profile", "classic_operation", "mixed_operations", "duplicate_operation"])
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
        changes = {"direct_profile": {"name": "quest_exit"}, "classic_operation": {"registration_keys": (6,)},
                   "mixed_operations": {"registration_keys": (7, 8)},
                   "duplicate_operation": {"registration_keys": (7, 7)}}[mutation]
        message = replace(message, profile=replace(message.profile, **changes))
    assert_rejected(message)

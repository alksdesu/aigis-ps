from dataclasses import FrozenInstanceError
from xml.etree import ElementTree as ET

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.errors import ProtocolError
from aigis_ps.gift_protocol import GiftRequest, GiftSelection, parse_gift
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES
from test_unit_actions import actions, asset_root, assets, master, request, service, store


def envelope(store, number=100, *, single=0, slots=(("gift_0002_ruby", 2), (0, 0), (0, 0), (0, 0)), alternative=0):
    def item(name, value):
        return Column(name, "S" if isinstance(value, str) else "I", (value,))
    columns = [Container("BASE", (Column("A7", "I", (1,)), Column("AB", "I", (2,)),
                                   Column("ALTERNATIVE", "I", (alternative,)))), item("ITEM", single)]
    for index, (key, count) in enumerate(slots):
        columns.extend((item(f"ITEM{index}", key), Column(f"NUM{index}", "I", (count,))))
    return request(store, number, columns)


@pytest.mark.parametrize("number", [100, 101])
def test_gift_parser_preserves_native_branch_and_all_slots(store, number):
    message = envelope(store, number)
    before = ET.tostring(message.document)
    parsed = parse_gift(message)
    assert parsed == GiftRequest(number, 1, 2, 0, 0,
                                  (GiftSelection("gift_0002_ruby", 2), GiftSelection(0, 0),
                                   GiftSelection(0, 0), GiftSelection(0, 0)))
    assert ET.tostring(message.document) == before
    with pytest.raises(FrozenInstanceError):
        parsed.unit_index = 3


@pytest.mark.parametrize("single,alternative", [("gift_0003_diamond", 0), (-1016865595, 1), (0, -1)])
def test_gift_parser_keeps_single_item_and_alternative_for_business_validation(store, single, alternative):
    parsed = parse_gift(envelope(store, single=single, alternative=alternative, slots=((0, 0),) * 4))
    assert parsed.single_item == single and parsed.alternative == alternative
    assert all(selection.count == 0 for selection in parsed.selections)


@pytest.mark.parametrize("path,mutation", [
    ("BASE", "missing"), ("BASE/A7", "missing"), ("BASE/AB", "zero"), ("BASE/A7", "negative"),
    ("BASE/ALTERNATIVE", "missing"), ("ITEM", "missing"), ("ITEM", "wrong_type"),
    ("ITEM0", "empty"), ("ITEM0", "multiple"), ("NUM2", "missing"), ("NUM0", "negative"),
    ("NUM1", "overflow"), ("NUM1", "wrong_type"), ("NUM3", "nested"),
    ("BASE", "duplicate"), ("ITEM", "duplicate"), ("CT", "missing"),
])
def test_gift_parser_rejects_ambiguous_or_malformed_request(store, path, mutation):
    message = envelope(store)
    root = message.document
    node = root.find(path)
    parent = root.find(path.rsplit("/", 1)[0]) if "/" in path else root
    if mutation == "missing":
        parent.remove(node)
    elif mutation == "zero":
        node[0].text = "0"
    elif mutation == "negative":
        node[0].text = "-1"
    elif mutation == "wrong_type":
        node.set("T", "B")
    elif mutation == "empty":
        node[0].text = ""
    elif mutation == "multiple":
        ET.SubElement(node, "V").text = "another"
    elif mutation == "overflow":
        node[0].text = "2147483648"
    elif mutation == "nested":
        ET.SubElement(node[0], "V").text = "1"
    elif mutation == "duplicate":
        parent.append(ET.fromstring(ET.tostring(node)))
    with pytest.raises(ProtocolError) as error:
        parse_gift(message)
    assert error.value.code == "invalid_request_fields"


@pytest.mark.parametrize("number", [25, 99])
def test_gift_parser_rejects_unrelated_or_base_menu_registration(store, number):
    with pytest.raises(ProtocolError) as error:
        parse_gift(envelope(store, number))
    assert error.value.code == "invalid_request_fields"


def test_gift_parser_rejects_unknown_business_fields(store):
    message = envelope(store)
    ET.SubElement(message.document.find("BASE"), "A5", T="I")
    with pytest.raises(ProtocolError):
        parse_gift(message)


@pytest.mark.parametrize("name", ["x" * MAX_ITEM_NAME_BYTES, chr(0x1F600) * 63 + "xxx"], ids=["ascii", "utf8"])
def test_gift_item_key_may_fill_the_native_cell(store, name):
    assert len(name.encode("utf-8")) == MAX_ITEM_NAME_BYTES
    assert parse_gift(envelope(store, single=name)).single_item == name


@pytest.mark.parametrize("name", ["x" * (MAX_ITEM_NAME_BYTES + 1), chr(0x1F600) * 64], ids=["ascii", "utf8"])
def test_gift_item_key_uses_canonical_utf8_byte_bound(store, name):
    assert len(name.encode("utf-8")) == MAX_ITEM_NAME_BYTES + 1
    with pytest.raises(ProtocolError) as error:
        parse_gift(envelope(store, single=name))
    assert error.value.code == "invalid_request_fields"

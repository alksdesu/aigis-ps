import struct

import pytest

from aigis_ps.alxml import (AlXmlError, Column, Container, GENERIC_ROW_TABLES, Limits, ROW_TABLE_TYPES, ValueType, dumps,
                            effective_name, loads)


NATIVE_VALUES = {"I": (1, -2), "B": (True, False), "S": ("a", ""), "F": (0.5, -2.0), "D": (1.25, 2.5)}
DROPPED_VALUES = {"DT": "2026/10/31 00:00:00", "I64": 1, "P": "path"}


def test_native_system_table_keeps_parallel_string_arrays():
    document = Container(children=(Container("SYSTEM", (
        Column("A1", ValueType.STRING, ("ExchangeKey", "PaymentTransaction")),
        Column("A2", ValueType.STRING, ("local-only", "0")),
    )),))
    encoded = dumps(document)
    assert encoded.startswith(b'<?xml version="1.0"?>')
    assert b'<A1 T="S"><V>ExchangeKey</V><V>PaymentTransaction</V></A1>' in encoded
    assert loads(encoded) == document


def test_login_columns_keep_native_order_and_types():
    source = b'<?xml version="1.0"?><DA><LGI><A1 T="I"><V>1</V></A1><A2 T="I"><V>7</V></A2><A3 T="I"><V>1</V></A3><A7 T="S"><V>0007</V></A7></LGI></DA>'
    login = loads(source).children[0]
    assert [column.name for column in login.children] == ["A1", "A2", "A3", "A7"]
    assert [column.values for column in login.children] == [(1,), (7,), (1,), ("0007",)]
    assert loads(dumps(Container(children=(login,)))) == Container(children=(login,))


def test_strings_escape_once_and_preserve_empty_and_singleton_arrays():
    document = Container(children=(Column("Text", "S", ("<>&\"'", "日本語", "", "&lt;")), Column("Empty", "I", ())))
    assert loads(dumps(document)) == document
    assert loads(b'<DA><X T="S"><item /></X></DA>').children[0].values == ("",)


def test_long_names_and_container_index_have_explicit_modes():
    document = Container(children=(Container("_12", (Column("P", "P", ("path",), "3"),), "2"),))
    encoded = dumps(document, short_names=False)
    assert b'Index="2"' in encoded and b'Type="Prop"' in encoded
    assert loads(encoded, short_names=False) == document
    assert effective_name(document.children[0]) == "12[2]"
    with pytest.raises(AlXmlError):
        loads(encoded)


@pytest.mark.parametrize("kind,values", [("I", (-2147483648, 2147483647)), ("I64", (-9223372036854775808, 9223372036854775807)), ("B", (True, False)), ("F", (0.5, -2.0)), ("D", (1.25e100,)), ("DT", ("2026/10/31 00:00:00",))])
def test_value_types_round_trip(kind, values):
    document = Container(children=(Column("X", kind, values),))
    assert loads(dumps(document)) == document


@pytest.mark.parametrize("source", [
    b'<!DOCTYPE DA [<!ENTITY x "boom">]><DA><X T="S"><V>&x;</V></X></DA>',
    b'<!DOCTYPE DA SYSTEM "file:///secret"><DA />',
    b'<DA><X>ordinary scalar</X></DA>',
    b'<DA><X T="I">1</X></DA>',
    b'<DA><X T="I"><V>1tail</V></X></DA>',
    b'<DA><X T="I"><V>2147483648</V></X></DA>',
    b'<DA><X T="I64"><V>9223372036854775808</V></X></DA>',
    b'<DA><X T="D"><V>NaN</V></X></DA>',
    b'<DA><X T="B"><V>1</V></X></DA>',
    b'<DA><X T="I" N="Ignored"><V>1</V></X></DA>',
    b'<DA><X T="I"><V><Nested /></V></X></DA>',
    b'<DA><X T="Unknown"><V>1</V></X></DA>',
    b'<DA xmlns="urn:x" />',
])
def test_rejects_ambiguous_or_unsafe_xml(source):
    with pytest.raises(AlXmlError):
        loads(source)


def test_limits_apply_to_parser_and_serializer():
    document = Container(children=(Container("N", (Column("X", "S", ("hello", "world")),)),))
    encoded = dumps(document)
    for limits in (Limits(max_depth=2), Limits(max_elements=3), Limits(max_values=1), Limits(max_text_bytes=4), Limits(max_bytes=10)):
        with pytest.raises(AlXmlError):
            loads(encoded, limits=limits)
        with pytest.raises(AlXmlError):
            dumps(document, limits=limits)


@pytest.mark.parametrize("column", [Column("X", "I", (True,)), Column("X", "I", (2147483648,)), Column("X", "F", (1e100,)), Column("X", "D", (float("inf"),)), Column("X", "S", ("bad\x00text",)), Column("bad name", "I", (1,))])
def test_serializer_checks_native_ranges_and_text(column):
    with pytest.raises(AlXmlError):
        dumps(Container(children=(column,)))


def test_native_name_buffer_limit_counts_utf8_and_index():
    with pytest.raises(AlXmlError):
        dumps(Container(children=(Container("x" * 62, index="1"),)))
    document = Container(children=(Container("日本語", index="1"),))
    assert loads(dumps(document)) == document


def test_float_columns_use_native_float32_precision():
    document = Container(children=(Column("X", "F", (0.1,)),))
    decoded = loads(dumps(document))
    assert decoded.children[0].values == (struct.unpack("<f", struct.pack("<f", 0.1))[0],)
    assert loads(dumps(decoded)) == decoded


def test_utf16_is_not_native_al_xml_input():
    with pytest.raises(AlXmlError):
        loads('<DA />'.encode('utf-16'))


def test_empty_datetime_has_a_native_text_node_and_string_remains_empty():
    document = Container(children=(Column("Time", "DT", ("",)), Column("Text", "S", ("",))))
    data = dumps(document)
    assert b'<Time T="DT"><V>0</V></Time>' in data
    result = loads(data)
    assert result.children[0].values == ("0",)
    assert result.children[1].values == ("",)
    with pytest.raises(AlXmlError):
        dumps(Container(children=(Column("Property", "P", ("",)),)))


def test_row_table_types_are_the_values_native_parsing_keeps():
    assert {kind.value for kind in ROW_TABLE_TYPES} == set(NATIVE_VALUES)
    assert set(ValueType) - ROW_TABLE_TYPES == {ValueType(kind) for kind in DROPPED_VALUES}


@pytest.mark.parametrize("name", sorted(GENERIC_ROW_TABLES))
def test_generic_row_tables_accept_equal_native_value_columns(name):
    group = Container(name, tuple(Column(f"C{kind}", kind, values) for kind, values in NATIVE_VALUES.items()))
    empty = Container(name, tuple(Column(f"C{kind}", kind, ()) for kind in NATIVE_VALUES))
    for document in (Container(children=(group,)), Container(children=(empty,)), Container(children=(Container(name),))):
        assert loads(dumps(document)) == document


@pytest.mark.parametrize("kind", sorted(DROPPED_VALUES))
@pytest.mark.parametrize("position", ["first", "last"])
def test_generic_row_tables_refuse_values_native_parsing_drops(kind, position):
    dropped = Column("Dropped", kind, (DROPPED_VALUES[kind],))
    kept = Column("CardID", "I", (0,))
    group = Container("PRM", (dropped, kept) if position == "first" else (kept, dropped))
    with pytest.raises(AlXmlError, match="keeps only"):
        dumps(Container(children=(group,)))


def test_generic_row_tables_refuse_nested_containers():
    with pytest.raises(AlXmlError, match="keeps only"):
        dumps(Container(children=(Container("PPU", (Container("Nested"), Column("A7", "I", (1,)))),)))


@pytest.mark.parametrize("lengths", [(1, 0), (0, 1), (2, 1), (1, 1, 2)])
def test_generic_row_tables_refuse_columns_of_unequal_length(lengths):
    group = Container("POSSESSION", tuple(Column(f"C{index}", "I", (7,) * length) for index, length in enumerate(lengths)))
    with pytest.raises(AlXmlError, match="equal length"):
        dumps(Container(children=(group,)))


@pytest.mark.parametrize("document", [
    Container(children=(Column("GameStartDateTime", "DT", ("2026/10/31 00:00:00",)), Column("A1", "I", (1, 2)))),
    Container(children=(Container("PGP", (Column("A1", "S", ("A1", "A2")), Column("A2", "S", ("1",)))),)),
    Container(children=(Container("LOTTERY", (Column("Index", "I", (0,)), Column("Item", "I64", ()))),)),
    Container(children=(Container("Outer", (Container("PPU", (Column("A1", "DT", ("0",)), Column("A2", "I", ()))),)),)),
], ids=["root_columns", "mixed_receiver_group", "custom_receiver_group", "nested_name"])
def test_row_table_guard_only_covers_root_groups_of_generic_receivers(document):
    assert loads(dumps(document)) == document

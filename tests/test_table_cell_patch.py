from copy import deepcopy
from datetime import datetime, timedelta
import struct

import lz4.frame
import pytest

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.energy_clock import JST
from aigis_ps.mission_catalog import _date
from aigis_ps.native_clock import NATIVE_EPOCH
import aigis_ps.table_cell_patch as module
from aigis_ps.table_cell_patch import patch_table_cells
from test_al_format import schema_bytes
from test_present_items import item_table


SCHEMA = (("Name", 32), ("Value", 1))
TIMED_SCHEMA = (("Name", 32), ("Value", 1), ("Stamp", 9), ("Flag", 5))
PARAMETER_SIZES = {1: 8, 4: 12, 5: 1, 9: 8, 32: 0}
CELL_FORMATS = {1: "<i", 4: "<f", 5: "<?", 9: "<II", 32: "<I"}
MAX_WORDS = 0xFFFFFFFFFFFFFFFF
OPENING = datetime(2026, 11, 2, 15, tzinfo=JST)


def milliseconds(moment):
    return (moment - NATIVE_EPOCH) // timedelta(milliseconds=1)


def native_table(schema, rows, *, tag=b"test"):
    offsets, position = {}, 0
    for name, code in schema:
        offsets[name] = position
        position += struct.calcsize(CELL_FORMATS[code])
    stride = position + -position % 4
    header = schema_bytes([(name, offsets[name], code, b"\0" * PARAMETER_SIZES[code]) for name, code in schema], stride)
    records, pool = bytearray(), bytearray()
    for row in rows:
        record = bytearray(stride)
        for name, code in schema:
            value = row[name]
            if code == 32:
                value = len(pool)
                pool += row[name].encode() + b"\0"
            words = (value & 0xFFFFFFFF, value >> 32) if code == 9 else (value,)
            struct.pack_into(CELL_FORMATS[code], record, offsets[name], *words)
        records += record
    start = 28 + len(header)
    return struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, len(rows), 28, start, stride, len(pool),
                       start + len(records), tag) + header + records + pool


def payload():
    return item_table([{"Name": "first", "Value": 1}, {"Name": "second", "Value": 2}], SCHEMA)


def timed_payload():
    return native_table(TIMED_SCHEMA, [{"Name": "first", "Value": 1, "Stamp": 0, "Flag": True},
                                       {"Name": "second", "Value": 2, "Stamp": 1 << 40, "Flag": False}])


def test_integer_patch_preserves_every_other_byte():
    raw = payload()
    table = parse_table(raw)
    position = table["data_offset"] + table["record_size"] + table["fields"][1]["offset"]
    updated = patch_table_cells(raw, {1: {"Value": -0x80000000}})
    assert updated[:position] == raw[:position] and updated[position + 4:] == raw[position + 4:]
    assert parse_table(updated)["rows"] == [{"Name": "first", "Value": 1}, {"Name": "second", "Value": -0x80000000}]


def test_string_pool_is_append_only_and_unselected_reference_is_unchanged():
    raw = payload()
    before = parse_table(raw)
    updated = patch_table_cells(raw, {0: {"Name": "新しい名前"}})
    after = parse_table(updated)
    start, size = before["string_pool_offset"], before["string_pool_size"]
    assert updated[start:start + size] == raw[start:start + size]
    assert after["rows"] == [{"Name": "新しい名前", "Value": 1}, {"Name": "second", "Value": 2}]
    assert after["fields"] == before["fields"]
    index = before["data_offset"] + before["record_size"]
    assert updated[index:index + before["record_size"]] == raw[index:index + before["record_size"]]
    assert after["string_pool_size"] == size + len("新しい名前".encode()) + 1


def test_equal_new_strings_share_one_appended_offset():
    raw = payload()
    updated = patch_table_cells(raw, {0: {"Name": "same"}, 1: {"Name": "same"}})
    table = parse_table(updated)
    positions = [table["data_offset"] + index * table["record_size"] for index in range(2)]
    assert struct.unpack_from("<I", updated, positions[0]) == struct.unpack_from("<I", updated, positions[1])
    assert table["string_pool_size"] == parse_table(raw)["string_pool_size"] + 5


@pytest.mark.parametrize("updates", [{}, {0: {}}, {0: {"Name": "first", "Value": 1}}])
def test_noop_returns_original_bytes(updates):
    raw = payload()
    assert patch_table_cells(raw, updates) is raw


def test_reapplying_patch_is_idempotent_and_does_not_mutate_inputs():
    raw = payload()
    updates = {0: {"Name": "", "Value": 0x7FFFFFFF}}
    before = deepcopy(updates)
    once = patch_table_cells(raw, updates)
    assert patch_table_cells(once, updates) is once
    assert updates == before and parse_table(raw)["rows"][0] == {"Name": "first", "Value": 1}


def test_all4_container_preserves_parameters_and_round_trips():
    raw = payload()
    frame = lz4.frame.compress(raw, block_size=lz4.frame.BLOCKSIZE_MAX64KB, block_linked=False,
                               content_checksum=True, block_checksum=True, store_size=True)
    wrapped = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + frame
    updated = patch_table_cells(wrapped, {1: {"Value": 44, "Name": "other"}})
    assert updated.startswith(wrapped[:8])
    old_info, new_info = lz4.frame.get_frame_info(wrapped[12:]), lz4.frame.get_frame_info(updated[12:])
    for field in ("block_size_id", "block_linked", "content_checksum", "block_checksum"):
        assert old_info[field] == new_info[field]
    assert parse_table(decompress_all4(updated))["rows"][1] == {"Name": "other", "Value": 44}


@pytest.mark.parametrize("updates", [{-1: {"Value": 1}}, {2: {"Value": 1}}, {True: {"Value": 1}},
    {"0": {"Value": 1}}, {0: []}, {0: {"Missing": 1}}, {0: {"Value": True}},
    {0: {"Value": 1.0}}, {0: {"Value": -0x80000001}}, {0: {"Value": 0x80000000}},
    {0: {"Name": 1}}, {0: {"Name": "bad\0name"}}, {0: {"Name": "x" * 4097}}])
def test_invalid_cells_are_rejected(updates):
    with pytest.raises(ValueError):
        patch_table_cells(payload(), updates)


@pytest.mark.parametrize("value", [None, [], "table", bytearray(b"ALTB"), b""])
def test_input_requires_immutable_bounded_resource_bytes(value):
    with pytest.raises(ValueError):
        patch_table_cells(value, {})


def test_unknown_tail_is_not_silently_discarded():
    with pytest.raises(ValueError):
        patch_table_cells(payload() + b"unknown", {0: {"Value": 2}})


def test_growth_beyond_resource_limit_is_rejected(monkeypatch):
    raw = payload()
    monkeypatch.setattr(module, "MAX_BYTES", len(raw) + 4)
    with pytest.raises(ValueError):
        patch_table_cells(raw, {0: {"Name": "more than four added bytes"}})


def test_utf8_limit_measures_bytes_not_characters():
    text = "界" * (module.MAX_STRING_BYTES // 3)
    assert parse_table(patch_table_cells(payload(), {0: {"Name": text}}))["rows"][0]["Name"] == text
    with pytest.raises(ValueError):
        patch_table_cells(payload(), {0: {"Name": text + "界"}})


def test_datetime_patch_writes_low_and_high_words_and_preserves_every_other_byte():
    raw = timed_payload()
    table = parse_table(raw)
    stamp = next(field for field in table["fields"] if field["name"] == "Stamp")
    position = table["data_offset"] + table["record_size"] + stamp["offset"]
    value = milliseconds(OPENING)
    updated = patch_table_cells(raw, {1: {"Stamp": value}})
    assert updated[:position] == raw[:position] and updated[position + 8:] == raw[position + 8:]
    assert struct.unpack_from("<II", updated, position) == (value & 0xFFFFFFFF, value >> 32)
    rows = parse_table(updated)["rows"]
    assert rows[1] == {"Name": "second", "Value": 2, "Stamp": [value & 0xFFFFFFFF, value >> 32], "Flag": False}
    assert _date(rows[1]["Stamp"]) == OPENING and rows[0] == table["rows"][0]


@pytest.mark.parametrize("value", [0, 1, 0xFFFFFFFF, 1 << 32, MAX_WORDS])
def test_datetime_cells_accept_the_full_unsigned_word_pair_range(value):
    updated = patch_table_cells(timed_payload(), {0: {"Stamp": value}})
    assert parse_table(updated)["rows"][0]["Stamp"] == [value & 0xFFFFFFFF, value >> 32]


@pytest.mark.parametrize("value", [-1, MAX_WORDS + 1, True, 1.0, [0, 0], (0, 0), "0", None])
def test_datetime_cells_reject_values_outside_native_words(value):
    with pytest.raises(ValueError, match="native field range"):
        patch_table_cells(timed_payload(), {0: {"Stamp": value}})


def test_datetime_noop_and_reapplied_patch_return_the_same_bytes():
    raw = timed_payload()
    assert patch_table_cells(raw, {0: {"Stamp": 0}, 1: {"Stamp": 1 << 40}}) is raw
    updates = {0: {"Stamp": milliseconds(OPENING)}}
    once = patch_table_cells(raw, updates)
    assert once != raw and patch_table_cells(once, updates) is once


def test_datetime_and_integer_cells_patch_together_in_one_round_trip():
    value = milliseconds(OPENING)
    updated = patch_table_cells(timed_payload(), {0: {"Value": -7, "Stamp": value, "Name": "renamed"}, 1: {"Stamp": 0}})
    assert parse_table(updated)["rows"] == [
        {"Name": "renamed", "Value": -7, "Stamp": [value & 0xFFFFFFFF, value >> 32], "Flag": True},
        {"Name": "second", "Value": 2, "Stamp": [0, 0], "Flag": False}]


def test_datetime_patch_round_trips_through_an_all4_container():
    raw = timed_payload()
    frame = lz4.frame.compress(raw, block_size=lz4.frame.BLOCKSIZE_MAX64KB, block_linked=False,
                               content_checksum=True, block_checksum=True, store_size=True)
    wrapped = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + frame
    updated = patch_table_cells(wrapped, {1: {"Stamp": MAX_WORDS}})
    assert updated.startswith(wrapped[:8])
    assert parse_table(decompress_all4(updated))["rows"][1]["Stamp"] == [0xFFFFFFFF, 0xFFFFFFFF]


@pytest.mark.parametrize("schema, name, value", [
    (TIMED_SCHEMA, "Flag", True),
    ((("Name", 32), ("Ratio", 4)), "Ratio", 1),
])
def test_other_native_field_types_stay_unpatchable(schema, name, value):
    rows = [{"Name": "only", "Value": 0, "Stamp": 0, "Flag": False, "Ratio": 0.5}]
    with pytest.raises(ValueError, match="I32, native day, native datetime and string-reference"):
        patch_table_cells(native_table(schema, rows), {0: {name: value}})

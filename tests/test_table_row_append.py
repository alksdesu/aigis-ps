from copy import deepcopy
import struct

import lz4.frame
import pytest

from aigis_ps.al_format import decompress_all4, parse_table
import aigis_ps.table_row_append as module
from aigis_ps.table_row_append import append_table_rows
from test_present_items import item_table


SCHEMA = (("ID", 1), ("Text", 32))


def source():
    return item_table([{"ID": 7, "Text": "original"}, {"ID": 7, "Text": "duplicate key is existing data"}], SCHEMA)


def test_append_preserves_records_schema_and_original_pool_bytes():
    raw = source()
    additions = [{"ID": -0x80000000, "Text": "追加"}, {"ID": 0x7FFFFFFF, "Text": "追加"}]
    snapshot = deepcopy(additions)
    result = append_table_rows(raw, additions)
    before, after = parse_table(raw), parse_table(result)
    start, pool, size = before["data_offset"], before["string_pool_offset"], before["string_pool_size"]
    assert result[start:pool] == raw[start:pool]
    assert result[after["string_pool_offset"]:after["string_pool_offset"] + size] == raw[pool:pool + size]
    assert after["rows"] == before["rows"] + additions
    assert after["fields"] == before["fields"] and after["tag"] == before["tag"]
    assert after["string_pool_size"] == size + len("追加".encode()) + 1
    assert additions == snapshot


@pytest.mark.parametrize("rows", [[], ()])
def test_no_rows_returns_original_immutable_bytes(rows):
    raw = source()
    assert append_table_rows(raw, rows) is raw


def test_all4_wrapper_parameters_and_alignment_are_preserved():
    raw = source()
    raw += bytes((-len(raw)) % 4)
    frame = lz4.frame.compress(raw, block_size=lz4.frame.BLOCKSIZE_MAX64KB, block_linked=False,
                               content_checksum=True, block_checksum=True, store_size=True)
    wrapped = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + frame
    result = append_table_rows(wrapped, [{"ID": 8, "Text": "new"}])
    assert result[:8] == wrapped[:8] and len(decompress_all4(result)) % 4 == 0
    for key in ("block_size_id", "block_linked", "content_checksum", "block_checksum"):
        assert lz4.frame.get_frame_info(result[12:])[key] == lz4.frame.get_frame_info(frame)[key]
    assert parse_table(decompress_all4(result))["rows"][-1] == {"ID": 8, "Text": "new"}


@pytest.mark.parametrize("rows", [None, {}, "rows", [{"ID": 1}], [{"ID": 1, "Text": "x", "Extra": 0}],
    [{"ID": True, "Text": "x"}], [{"ID": 1.0, "Text": "x"}], [{"ID": 0x80000000, "Text": "x"}],
    [{"ID": -0x80000001, "Text": "x"}], [{"ID": 1, "Text": 0}], [{"ID": 1, "Text": "bad\0text"}],
    [{"ID": 1, "Text": "界" * 1366}]])
def test_invalid_rows_fail_atomically(rows):
    raw = source()
    with pytest.raises(ValueError):
        append_table_rows(raw, rows)
    assert raw == source()


@pytest.mark.parametrize("raw", [None, "bad", bytearray(b"ALTB"), b""])
def test_invalid_input_type(raw):
    with pytest.raises(ValueError):
        append_table_rows(raw, [])


def test_unknown_layout_and_trailing_data_are_rejected():
    raw = source()
    with pytest.raises(ValueError):
        append_table_rows(raw + b"unknown", [])
    changed = bytearray(raw)
    changed[5] = 0x1E
    with pytest.raises(ValueError):
        append_table_rows(bytes(changed), [])


def test_native_count_and_size_limits_are_checked_before_return(monkeypatch):
    raw = source()
    monkeypatch.setattr(module, "MAX_ROWS", 2)
    with pytest.raises(ValueError, match="row-count"):
        append_table_rows(raw, [{"ID": 1, "Text": "x"}])
    monkeypatch.setattr(module, "MAX_ROWS", 65535)
    monkeypatch.setattr(module, "MAX_BYTES", len(raw) + 8)
    with pytest.raises(ValueError, match="byte limit"):
        append_table_rows(raw, [{"ID": 1, "Text": "larger than limit"}])


def test_maximum_utf8_text_and_empty_string_are_valid():
    rows = [{"ID": 1, "Text": "x" * 4096}, {"ID": 2, "Text": ""}]
    assert parse_table(append_table_rows(source(), rows))["rows"][-2:] == rows


def test_untagged_string_pool_table_uses_original_section_layout():
    original = source()
    table = parse_table(original)
    raw = bytearray(original[:24] + original[28:])
    raw[5] = 4
    struct.pack_into("<HH", raw, 8, table["schema_offset"] - 4, table["data_offset"] - 4)
    struct.pack_into("<I", raw, 20, table["string_pool_offset"] - 4)
    result = parse_table(append_table_rows(bytes(raw), [{"ID": 8, "Text": "new"}]))
    assert result["tag"] is None and result["flags"] == 4
    assert result["rows"] == table["rows"] + [{"ID": 8, "Text": "new"}]

from __future__ import annotations

from collections.abc import Mapping
import struct

from .al_format import ALFormatError, ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .native_numbers import int32


MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 65535
MAX_ADDITIONS = 1024
MAX_STRING_BYTES = 4096
LIMITS = ParseLimits(max_input_bytes=MAX_BYTES, max_decompressed_bytes=MAX_BYTES,
                     max_rows=MAX_ROWS, max_fields=128, max_cells=3_000_000)


def _invalid(message):
    raise ALFormatError(message, code="table_append_invalid")


def append_table_rows(payload: bytes, rows) -> bytes:
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_BYTES:
        _invalid("Table append requires bounded immutable resource bytes")
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ADDITIONS:
        _invalid("Table additions require a bounded row sequence")
    compressed = payload.startswith(b"ALL4")
    data = decompress_all4(payload, LIMITS) if compressed else payload
    table = parse_table(data, LIMITS)
    if table["flags"] not in (4, 0x14) or table["names_offset"] is not None:
        _invalid("Only native tables with a final string pool are supported")
    pool_start, pool_size = table["string_pool_offset"], table["string_pool_size"]
    pool_end = pool_start + pool_size
    if pool_start != table["data_offset"] + table["record_count"] * table["record_size"] or not pool_size:
        _invalid("The string pool must immediately follow all existing records")
    trailing = data[pool_end:]
    if trailing and trailing != bytes((-pool_end) % 4):
        _invalid("Unknown data follows the string pool")
    fields = {field["name"]: field for field in table["fields"]}
    if len(fields) != len(table["fields"]) or not fields:
        _invalid("Table field names must be unique and nonempty")
    occupied = set()
    for field in fields.values():
        if field["type_code"] not in (1, 32) or field["size"] != 4:
            _invalid("Only I32 and string-reference fields may be appended")
        positions = set(range(field["offset"], field["offset"] + 4))
        if occupied.intersection(positions):
            _invalid("Overlapping table fields cannot be safely appended")
        occupied.update(positions)
    count = table["record_count"] + len(rows)
    if count > MAX_ROWS:
        _invalid("Appended rows exceed the native row-count capacity")
    additions, strings, offsets, expected = bytearray(), bytearray(), {}, []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(fields):
            _invalid("Appended rows must supply exactly the existing fields")
        record = bytearray(table["record_size"])
        for name, field in fields.items():
            value = row[name]
            if field["type_code"] == 1:
                if not int32(value):
                    _invalid("Integer cells require signed int32 values")
                struct.pack_into("<i", record, field["offset"], value)
            else:
                if type(value) is not str or len(value.encode("utf-8")) > MAX_STRING_BYTES:
                    _invalid("String cells require bounded UTF-8 text")
                dumps(Container(children=(Column("Value", "S", (value,)),)))
                if value not in offsets:
                    offsets[value] = pool_size + len(strings)
                    strings.extend(value.encode("utf-8") + b"\0")
                struct.pack_into("<I", record, field["offset"], offsets[value])
        additions.extend(record)
        expected.append(dict(row))
    if not rows:
        return payload
    updated = bytearray(data[:pool_start])
    struct.pack_into("<H", updated, 6, count)
    struct.pack_into("<II", updated, 16, pool_size + len(strings), pool_start + len(additions))
    updated.extend(additions)
    updated.extend(data[pool_start:pool_end])
    updated.extend(strings)
    if trailing or pool_end % 4 == 0:
        updated.extend(bytes((-len(updated)) % 4))
    if len(updated) > MAX_BYTES:
        _invalid("Appended table exceeds the resource byte limit")
    result = bytes(updated)
    verified = parse_table(result, LIMITS)
    if verified["rows"] != table["rows"] + expected or verified["fields"] != table["fields"]:
        _invalid("Appended table failed its complete round-trip check")
    if not compressed:
        return result
    import lz4.frame
    frame = lz4.frame.get_frame_info(payload[12:])
    encoded = lz4.frame.compress(result, block_size=frame["block_size_id"],
        block_linked=frame["block_linked"], content_checksum=frame["content_checksum"],
        block_checksum=frame["block_checksum"], store_size=bool(frame["content_size"]))
    wrapped = payload[:8] + struct.pack("<I", len(result)) + encoded
    if len(wrapped) > MAX_BYTES or decompress_all4(wrapped, LIMITS) != result:
        _invalid("Appended resource failed its container round-trip check")
    return wrapped

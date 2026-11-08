from __future__ import annotations

from collections.abc import Mapping
import struct
from types import MappingProxyType

from .al_format import ALFormatError, ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .native_numbers import INT32_MAX, INT32_MIN, UINT32_MASK


MAX_BYTES = 64 * 1024 * 1024
MAX_CELLS = 65535
MAX_STRING_BYTES = 4096
LIMITS = ParseLimits(max_input_bytes=MAX_BYTES, max_decompressed_bytes=MAX_BYTES,
                     max_rows=65535, max_fields=128, max_cells=3_000_000)
INT32_TYPE, DAY_TYPE, DATETIME_TYPE, STRING_TYPE = 1, 7, 9, 32
WORD_MASK = UINT32_MASK
WORD_BITS = 32
# datetime_raw holds milliseconds since 0001-01-01 JST as little-endian low and high words.
INTEGER_CELLS = MappingProxyType({INT32_TYPE: ("<i", INT32_MIN, INT32_MAX), DAY_TYPE: ("<I", 0, WORD_MASK),
                                  DATETIME_TYPE: ("<II", 0, 0xFFFFFFFFFFFFFFFF)})
CELL_SIZES = MappingProxyType({INT32_TYPE: 4, DAY_TYPE: 4, DATETIME_TYPE: 8, STRING_TYPE: 4})


def _invalid(message):
    raise ALFormatError(message, code="table_patch_invalid")


def _cell(type_code, value):
    if type_code == STRING_TYPE:
        if type(value) is not str or len(value.encode("utf-8")) > MAX_STRING_BYTES:
            _invalid("String table cells require bounded UTF-8 text")
        dumps(Container(children=(Column("Value", "S", (value,)),)))
        return value
    _, minimum, maximum = INTEGER_CELLS[type_code]
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid("Integer table cells must fit their native field range")
    # parse_table decodes datetime_raw as its [low, high] words, so comparisons use that shape.
    return [value & WORD_MASK, value >> WORD_BITS] if type_code == DATETIME_TYPE else value


def patch_table_cells(payload: bytes, updates: Mapping[int, Mapping[str, int | str]]) -> bytes:
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_BYTES:
        _invalid("Table patch requires bounded immutable resource bytes")
    if not isinstance(updates, Mapping) or len(updates) > MAX_CELLS:
        _invalid("Table updates require a bounded row mapping")
    compressed = payload.startswith(b"ALL4")
    data = decompress_all4(payload, LIMITS) if compressed else payload
    table = parse_table(data, LIMITS)
    if table["flags"] not in (4, 0x14) or table["names_offset"] is not None:
        _invalid("Only native tables with a final string pool are supported")
    pool_start, pool_size = table["string_pool_offset"], table["string_pool_size"]
    pool_end = pool_start + pool_size
    records_end = table["data_offset"] + table["record_count"] * table["record_size"]
    if pool_start != records_end or not pool_size:
        _invalid("String pool must directly follow the complete record section")
    trailing = data[pool_end:]
    if trailing and trailing != bytes((-pool_end) % 4):
        _invalid("Unknown data follows the string pool")
    aligned = bool(trailing) or pool_end % 4 == 0
    fields = {field["name"]: field for field in table["fields"]}
    occupied = set()
    for field in fields.values():
        positions = set(range(field["offset"], field["offset"] + field["size"]))
        if occupied.intersection(positions):
            _invalid("Overlapping table fields cannot be safely patched")
        occupied.update(positions)
    expected = [dict(row) for row in table["rows"]]
    changes, count = [], 0
    for index, values in updates.items():
        if type(index) is not int or not 0 <= index < table["record_count"] or not isinstance(values, Mapping):
            _invalid("Table patch row index or field mapping is invalid")
        count += len(values)
        if count > MAX_CELLS:
            _invalid("Table patch exceeds its cell limit")
        for name, value in values.items():
            field = fields.get(name) if type(name) is str else None
            if field is None or CELL_SIZES.get(field["type_code"]) != field["size"]:
                _invalid("Only existing I32, native day, native datetime and string-reference fields may be patched")
            cell = _cell(field["type_code"], value)
            if table["rows"][index][name] != cell:
                changes.append((index, field, cell))
                expected[index][name] = cell
    if not changes:
        return payload
    updated = bytearray(data[:pool_end])
    appended, offsets = bytearray(), {}
    for index, field, cell in changes:
        position = table["data_offset"] + index * table["record_size"] + field["offset"]
        if field["type_code"] == STRING_TYPE:
            if cell not in offsets:
                offsets[cell] = pool_size + len(appended)
                appended.extend(cell.encode("utf-8") + b"\0")
            struct.pack_into("<I", updated, position, offsets[cell])
        else:
            words = cell if field["type_code"] == DATETIME_TYPE else (cell,)
            struct.pack_into(INTEGER_CELLS[field["type_code"]][0], updated, position, *words)
    if appended:
        struct.pack_into("<I", updated, 16, pool_size + len(appended))
    updated.extend(appended)
    updated.extend(bytes((-len(updated)) % 4) if aligned else trailing)
    if len(updated) > MAX_BYTES:
        _invalid("Patched table exceeds the resource size limit")
    result = bytes(updated)
    verified = parse_table(result, LIMITS)
    if verified["rows"] != expected or verified["fields"] != table["fields"]:
        _invalid("Patched table failed its complete round-trip check")
    if not compressed:
        return result
    import lz4.frame
    frame = lz4.frame.get_frame_info(payload[12:])
    encoded = lz4.frame.compress(result, block_size=frame["block_size_id"],
        block_linked=frame["block_linked"], content_checksum=frame["content_checksum"],
        block_checksum=frame["block_checksum"], store_size=bool(frame["content_size"]))
    wrapped = payload[:8] + struct.pack("<I", len(result)) + encoded
    if len(wrapped) > MAX_BYTES or decompress_all4(wrapped, LIMITS) != result:
        _invalid("Patched resource failed its container round-trip check")
    return wrapped

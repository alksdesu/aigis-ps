from __future__ import annotations

import math
import struct
from dataclasses import asdict, dataclass
from typing import Any


MAX_RESOURCE_BYTES = 64 * 1024 * 1024


class ALFormatError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_format", offset: int | None = None):
        super().__init__(message)
        self.code = code
        self.offset = offset

    def diagnostic(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.offset is not None:
            result["offset"] = self.offset
        return result


@dataclass(frozen=True)
class ParseLimits:
    max_input_bytes: int = MAX_RESOURCE_BYTES
    max_decompressed_bytes: int = 128 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_depth: int = 8
    max_members: int = 10000
    max_rows: int = 100000
    max_fields: int = 2048
    max_cells: int = 1000000
    max_text_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(value <= 0 for value in asdict(self).values()):
            raise ValueError("All parse limits must be positive")


@dataclass(frozen=True)
class ArchiveMember:
    index: int
    member_id: int
    offset: int
    size: int
    info: int
    name_hash: int | None
    name: str | None


@dataclass(frozen=True)
class Archive:
    version: int
    flags: int
    members: tuple[ArchiveMember, ...]


@dataclass(frozen=True)
class Field:
    name: str
    alias: str
    offset: int
    type_code: int
    flags: int
    type_name: str
    size: int
    parameters: str


@dataclass(frozen=True)
class Schema:
    version: int
    flags: int
    record_size: int
    fields: tuple[Field, ...]
    end_offset: int


_TYPES = {
    1: ("int32", "i", 8),
    2: ("int16", "h", 4),
    3: ("int8", "b", 2),
    4: ("float32", "f", 12),
    5: ("bool", "B", 1),
    6: ("angle_raw", "I", 8),
    7: ("date_raw", "I", 8),
    8: ("time_raw", "I", 8),
    9: ("datetime_raw", "II", 8),
    10: ("int64", "q", 16),
    11: ("float64", "d", 12),
    32: ("string_ref", "I", 0),
    33: ("fixed_string", "32s", 4),
    64: ("vector3", "fff", 4),
    65: ("color4", "ffff", 0),
    66: ("rect4", "ffff", 0),
}


def _region(data: bytes, offset: int, size: int, end: int | None = None) -> bytes:
    boundary = len(data) if end is None else min(end, len(data))
    if offset < 0 or size < 0 or offset > boundary or size > boundary - offset:
        raise ALFormatError("Read exceeds the available data", code="bounds", offset=offset)
    return data[offset:offset + size]


def _unpack(fmt: str, data: bytes, offset: int, end: int | None = None) -> tuple[Any, ...]:
    return struct.unpack("<" + fmt, _region(data, offset, struct.calcsize("<" + fmt), end))


def _text(raw: bytes, offset: int) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ALFormatError("Invalid UTF-8 text", code="text_encoding", offset=offset) from exc


def _terminated(data: bytes, offset: int, end: int) -> str:
    _region(data, offset, 0, end)
    stop = data.find(b"\0", offset, min(end, len(data)))
    if stop < 0:
        raise ALFormatError("Missing string terminator", code="string_termination", offset=offset)
    return _text(data[offset:stop], offset)


def _input_limit(data: bytes, limits: ParseLimits) -> None:
    if len(data) > limits.max_input_bytes:
        raise ALFormatError("Input exceeds the byte limit", code="input_limit")


def decompress_all4(data: bytes, limits: ParseLimits | None = None) -> bytes:
    limits = limits or ParseLimits()
    _input_limit(data, limits)
    magic, version, reserved, size = _unpack("4sHHI", data, 0)
    if magic != b"ALL4" or version != 1 or reserved != 0:
        raise ALFormatError("Unsupported ALL4 header", code="unsupported_header")
    if size > limits.max_decompressed_bytes:
        raise ALFormatError("ALL4 declared size exceeds the byte limit", code="decompression_limit")
    try:
        import lz4.frame
    except ImportError as exc:
        raise ALFormatError("ALL4 decoding requires the lz4 package", code="missing_dependency") from exc
    decoder = lz4.frame.LZ4FrameDecompressor()
    try:
        result = decoder.decompress(data[12:], max_length=size + 1)
    except (RuntimeError, ValueError) as exc:
        raise ALFormatError("Invalid LZ4 frame", code="lz4_frame") from exc
    if len(result) != size:
        raise ALFormatError("ALL4 decoded length differs from its declaration", code="length_mismatch")
    if not decoder.eof:
        raise ALFormatError("Incomplete or oversized LZ4 frame", code="lz4_frame")
    if decoder.unused_data:
        raise ALFormatError("Trailing data after the LZ4 frame", code="trailing_data")
    return result


def parse_archive(data: bytes, limits: ParseLimits | None = None) -> Archive:
    limits = limits or ParseLimits()
    _input_limit(data, limits)
    magic, version, flags, count = _unpack("4sBBH", data, 0)
    if magic != b"ALAR" or version not in (2, 3):
        raise ALFormatError("Unsupported ALAR header", code="unsupported_header")
    if count > limits.max_members:
        raise ALFormatError("Archive member count exceeds the limit", code="member_limit")
    if version == 2:
        metadata_end = 16 + 16 * count
        _region(data, 0, metadata_end)
        positions = [16 + 16 * index for index in range(count)]
    else:
        metadata_end = 18 + 2 * count
        _region(data, 0, metadata_end)
        shift = 2 if flags & 8 else 0
        positions = [value << shift for value in _unpack("H" * count, data, 18)]
        if len(set(positions)) != count or any(pos < metadata_end for pos in positions):
            raise ALFormatError("Invalid ALAR member offset table", code="archive_index")
    members = []
    ordered_positions = sorted(positions)
    following = {pos: ordered_positions[index + 1] for index, pos in enumerate(ordered_positions[:-1])}
    for index, pos in enumerate(positions):
        member_id, offset, size, info = _unpack("IIII", data, pos)
        _region(data, offset, size)
        if offset < metadata_end:
            raise ALFormatError("Archive data overlaps the index", code="archive_index", offset=offset)
        name = None
        name_hash = None
        if version == 3:
            name_hash, = _unpack("H", data, pos + 16)
            if info & 0x80000000:
                boundary = min(len(data), offset, pos + 18 + 1024, following.get(pos, len(data)))
                name = _terminated(data, pos + 18, boundary)
        elif info & 0x80000000:
            _region(data, offset - 34, 34)
            if offset - 34 < metadata_end:
                raise ALFormatError("Archive name overlaps the index", code="archive_index", offset=offset)
            name_hash, = _unpack("H", data, offset - 2)
            name = _terminated(data, offset - 34, offset - 2)
        members.append(ArchiveMember(index, member_id, offset, size, info, name_hash, name))
    return Archive(version, flags, tuple(members))


def parse_schema(
    data: bytes,
    offset: int = 0,
    end: int | None = None,
    limits: ParseLimits | None = None,
) -> Schema:
    limits = limits or ParseLimits()
    _input_limit(data, limits)
    end = len(data) if end is None else end
    magic, version, flags, count, stride = _unpack("4sBBHH", data, offset, end)
    if magic != b"ALRD" or version != 1 or flags != 0:
        raise ALFormatError("Unsupported ALRD header", code="unsupported_header", offset=offset)
    if count > limits.max_fields:
        raise ALFormatError("Schema field count exceeds the limit", code="field_limit")
    pos = offset + 10
    fields = []
    names: set[str] = set()
    for _ in range(count):
        field_offset, type_code, field_flags, name_length, alias_length = _unpack("HBBBB", data, pos, end)
        pos += 6
        description = _TYPES.get(type_code)
        if description is None:
            raise ALFormatError(f"Unsupported ALRD field type {type_code}", code="unsupported_field", offset=pos - 4)
        strings = []
        for length in (name_length, alias_length):
            raw = _region(data, pos, length + 1, end)
            if raw[-1] != 0 or b"\0" in raw[:-1]:
                raise ALFormatError("Invalid schema string length", code="string_termination", offset=pos)
            strings.append(_text(raw[:-1], pos))
            pos += length + 1
        name, alias = strings
        if not name or name in names:
            raise ALFormatError("Empty or duplicate schema field name", code="field_name", offset=pos)
        names.add(name)
        pos = (pos + 3) & ~3
        type_name, fmt, parameter_size = description
        raw_parameters = _region(data, pos, parameter_size, end)
        parameters = raw_parameters.hex()
        pos = (pos + parameter_size + 3) & ~3
        _region(data, pos, 0, end)
        size = int.from_bytes(raw_parameters, "little") if type_code == 33 else struct.calcsize("<" + fmt)
        if not size or field_offset + size > stride:
            raise ALFormatError(f"Field {name} exceeds the record width", code="field_bounds", offset=field_offset)
        fields.append(Field(name, alias, field_offset, type_code, field_flags, type_name, size, parameters))
    return Schema(version, flags, stride, tuple(fields), pos)


def _finite(value: Any, raw: bytes) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite": str(value), "raw_hex": raw.hex()}
    return value


def parse_table(data: bytes, limits: ParseLimits | None = None) -> dict[str, Any]:
    limits = limits or ParseLimits()
    _input_limit(data, limits)
    magic, version, flags, count, schema_offset, data_offset, stride = _unpack("4sBBHHHI", data, 0)
    if magic != b"ALTB" or version != 1 or flags not in (0, 4, 0x10, 0x14, 0x1E):
        raise ALFormatError("Unsupported ALTB header", code="unsupported_header")
    if count > limits.max_rows:
        raise ALFormatError("Table record count exceeds the limit", code="row_limit")
    header_size = 16
    pool_offset = pool_size = 0
    if flags & 4:
        pool_size, pool_offset = _unpack("II", data, 16)
        header_size += 8
        _region(data, pool_offset, pool_size)
    names_offset = None
    if flags == 0x1E:
        names_offset, = _unpack("I", data, header_size)
        header_size += 4
        _region(data, names_offset, 0)
    tag = None
    if flags & 0x10:
        tag = _text(_region(data, header_size, 4), header_size)
        header_size += 4
    if not header_size <= schema_offset <= data_offset:
        raise ALFormatError("Invalid ALTB section order", code="table_sections")
    schema = parse_schema(data, schema_offset, data_offset, limits)
    if not stride or schema.record_size != stride:
        raise ALFormatError("ALTB and ALRD record widths disagree", code="record_width")
    records_end = data_offset + count * stride
    _region(data, data_offset, count * stride)
    if pool_size and pool_offset < records_end:
        raise ALFormatError("String pool overlaps table records", code="table_sections")
    if count * len(schema.fields) > limits.max_cells:
        raise ALFormatError("Decoded cell count exceeds the limit", code="cell_limit")
    rows = []
    text_bytes = 0
    strings: dict[int, str] = {}
    for row_index in range(count):
        row: dict[str, Any] = {}
        base = data_offset + row_index * stride
        for field in schema.fields:
            raw = _region(data, base + field.offset, field.size, base + stride)
            fmt = str(field.size) + "s" if field.type_code == 33 else _TYPES[field.type_code][1]
            values = struct.unpack("<" + fmt, raw)
            if field.type_code == 32:
                relative = values[0]
                if not pool_size and relative == 0:
                    value: Any = None
                elif relative >= pool_size:
                    raise ALFormatError(f"String reference for {field.name} exceeds the pool", code="string_bounds", offset=base + field.offset)
                else:
                    if relative not in strings:
                        strings[relative] = _terminated(data, pool_offset + relative, pool_offset + pool_size)
                    value = strings[relative]
            elif field.type_code == 33:
                value = _text(raw.split(b"\0", 1)[0], base + field.offset)
            elif field.type_code == 5:
                value = bool(values[0])
            elif len(values) == 1:
                value = _finite(values[0], raw)
            else:
                item_size = field.size // len(values)
                value = [_finite(item, raw[i * item_size:(i + 1) * item_size]) for i, item in enumerate(values)]
            if isinstance(value, str):
                text_bytes += len(value.encode("utf-8"))
                if text_bytes > limits.max_text_bytes:
                    raise ALFormatError("Decoded string data exceeds the limit", code="text_limit")
            row[field.name] = value
        rows.append(row)
    return {
        "kind": "ALTB", "version": version, "flags": flags, "tag": tag,
        "record_count": count, "record_size": stride, "schema_offset": schema_offset,
        "data_offset": data_offset, "string_pool_offset": pool_offset if flags & 4 else None,
        "string_pool_size": pool_size, "names_offset": names_offset,
        "fields": [asdict(field) for field in schema.fields], "rows": rows,
    }

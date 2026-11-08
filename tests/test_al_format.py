import struct

import pytest

from aigis_ps.al_format import ALFormatError, ParseLimits, decompress_all4, parse_archive, parse_schema, parse_table


def schema_bytes(fields, stride):
    data = bytearray(struct.pack("<4sBBHH", b"ALRD", 1, 0, len(fields), stride))
    for name, offset, code, parameters in fields:
        encoded = name.encode("utf-8")
        data += struct.pack("<HBBBB", offset, code, len(parameters), len(encoded), 0)
        data += encoded + b"\0\0"
        data += b"\0" * (-len(data) % 4)
        data += parameters
        data += b"\0" * (-len(data) % 4)
    return bytes(data)


def int_table(values=()):
    schema = schema_bytes([("ID", 0, 1, b"\0" * 8)], 4)
    start = 20 + len(schema)
    header = struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(values), 20, start, 4, b"test")
    return header + schema + b"".join(struct.pack("<i", value) for value in values)


def archive_bytes(payload=b"data", name=b"file.atb", version=3, shifted=False):
    if version == 2:
        start = 80
        result = bytearray(start)
        struct.pack_into("<4sBBH", result, 0, b"ALAR", 2, 0x61, 1)
        struct.pack_into("<IIII", result, 16, 7, start, len(payload), 0x80000001)
        result[start - 34:start - 34 + len(name)] = name
        struct.pack_into("<H", result, start - 2, 0x1234)
    else:
        start = 80
        result = bytearray(start)
        struct.pack_into("<4sBBH", result, 0, b"ALAR", 3, 0x49 if shifted else 0x41, 1)
        struct.pack_into("<HH", result, 16, start, 8 if shifted else 32)
        struct.pack_into("<IIIIH", result, 32, 7, start, len(payload), 0x80000001, 0x1234)
        result[50:50 + len(name)] = name
    return bytes(result) + payload


def test_schema_and_signed_table_values():
    table = parse_table(int_table((-2147483648, -1, 0, 2147483647)))
    assert [row["ID"] for row in table["rows"]] == [-2147483648, -1, 0, 2147483647]
    assert table["tag"] == "test"
    assert table["record_size"] == 4


def test_zero_record_table_retains_schema():
    table = parse_table(int_table())
    assert table["record_count"] == 0
    assert table["rows"] == []
    assert table["fields"][0]["name"] == "ID"


def test_string_pool_and_nonfinite_float_are_lossless_json_values():
    schema = schema_bytes([("Text", 0, 32, b""), ("Value", 4, 4, b"\0" * 12)], 8)
    start = 28 + len(schema)
    pool = "测试".encode() + b"\0"
    header = struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, 1, 28, start, 8, len(pool), start + 8, b"text")
    data = header + schema + struct.pack("<II", 0, 0x7FC00001) + pool
    row = parse_table(data)["rows"][0]
    assert row["Text"] == "测试"
    assert row["Value"] == {"nonfinite": "nan", "raw_hex": "0100c07f"}
    broken = bytearray(data)
    struct.pack_into("<I", broken, start, len(pool))
    with pytest.raises(ALFormatError, match="exceeds the pool"):
        parse_table(bytes(broken))


def test_null_string_without_pool_is_preserved():
    schema = schema_bytes([("Text", 0, 32, b"")], 4)
    start = 20 + len(schema)
    data = struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, 1, 20, start, 4, b"text") + schema + b"\0" * 4
    assert parse_table(data)["rows"] == [{"Text": None}]
    with pytest.raises(ALFormatError) as error:
        parse_table(data[:-4] + struct.pack("<I", 1))
    assert error.value.code == "string_bounds"


def test_repeated_string_references_cannot_bypass_text_limit():
    schema = schema_bytes([("Text", 0, 32, b"")], 4)
    start = 28 + len(schema)
    pool = b"abc\0"
    data = struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, 2, 28, start, 4, 4, start + 8, b"text") + schema + b"\0" * 8 + pool
    with pytest.raises(ALFormatError) as error:
        parse_table(data, ParseLimits(max_text_bytes=5))
    assert error.value.code == "text_limit"


@pytest.mark.parametrize("version,shifted", [(2, False), (3, False), (3, True)])
def test_archive_versions_and_scaled_index(version, shifted):
    data = archive_bytes(version=version, shifted=shifted)
    archive = parse_archive(data)
    member, = archive.members
    assert archive.version == version
    assert member.name == "file.atb"
    assert member.member_id == 7
    assert data[member.offset:member.offset + member.size] == b"data"


def test_archive_unnamed_member():
    data = bytearray(archive_bytes())
    struct.pack_into("<I", data, 44, 1)
    assert parse_archive(bytes(data)).members[0].name is None


def test_archive_bounds_are_rejected_and_member_info_is_preserved():
    broken = bytearray(archive_bytes())
    struct.pack_into("<I", broken, 40, 0xFFFFFFFF)
    with pytest.raises(ALFormatError) as error:
        parse_archive(bytes(broken))
    assert error.value.code == "bounds"
    struct.pack_into("<II", broken, 40, 4, 0x80000002)
    assert parse_archive(bytes(broken)).members[0].info == 0x80000002


def test_inline_strings_use_declared_capacity():
    schema = schema_bytes([("VN", 0, 33, struct.pack("<I", 16))], 16)
    parsed = parse_schema(schema)
    assert parsed.fields[0].size == 16
    start = 20 + len(schema)
    data = struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, 1, 20, start, 16, b"vers") + schema + b"5.0.0".ljust(16, b"\0")
    assert parse_table(data)["rows"] == [{"VN": "5.0.0"}]


def test_archive_bad_index_and_unterminated_name():
    broken = bytearray(archive_bytes())
    struct.pack_into("<H", broken, 18, 4)
    with pytest.raises(ALFormatError) as error:
        parse_archive(bytes(broken))
    assert error.value.code == "archive_index"
    broken = bytearray(archive_bytes())
    broken[50:80] = b"x" * 30
    with pytest.raises(ALFormatError) as error:
        parse_archive(bytes(broken))
    assert error.value.code == "string_termination"


def test_schema_unknown_type_and_duplicate_names():
    with pytest.raises(ALFormatError) as error:
        parse_schema(schema_bytes([("Unknown", 0, 255, b"")], 4))
    assert error.value.code == "unsupported_field"
    duplicated = schema_bytes([("ID", 0, 1, b"\0" * 8), ("ID", 4, 1, b"\0" * 8)], 8)
    with pytest.raises(ALFormatError) as error:
        parse_schema(duplicated)
    assert error.value.code == "field_name"


def test_schema_does_not_silently_truncate_fields():
    schema = schema_bytes([("ID", 0, 1, b"\0" * 8)], 4)
    with pytest.raises(ALFormatError) as error:
        parse_schema(schema[:-1])
    assert error.value.code == "bounds"
    with pytest.raises(ALFormatError) as error:
        parse_table(int_table((1,))[:-1])
    assert error.value.code == "bounds"


def test_table_rejects_schema_overlap_and_width_mismatch():
    data = bytearray(int_table((1,)))
    struct.pack_into("<H", data, 8, 4)
    with pytest.raises(ALFormatError) as error:
        parse_table(bytes(data))
    assert error.value.code == "table_sections"
    data = bytearray(int_table((1,)))
    struct.pack_into("<I", data, 12, 8)
    with pytest.raises(ALFormatError) as error:
        parse_table(bytes(data))
    assert error.value.code == "record_width"


def test_limits_apply_before_parsing_rows_or_allocating_output():
    with pytest.raises(ALFormatError) as error:
        parse_table(int_table((1, 2)), ParseLimits(max_rows=1))
    assert error.value.code == "row_limit"
    header = struct.pack("<4sHHI", b"ALL4", 1, 0, 1000)
    with pytest.raises(ALFormatError) as error:
        decompress_all4(header, ParseLimits(max_decompressed_bytes=100))
    assert error.value.code == "decompression_limit"
    with pytest.raises(ALFormatError) as error:
        parse_table(int_table((1, 2)), ParseLimits(max_cells=1))
    assert error.value.code == "cell_limit"


def test_all4_checks_size_truncation_and_trailing_frames():
    lz4 = pytest.importorskip("lz4.frame")
    content = int_table((7,))
    frame = lz4.compress(content)
    data = struct.pack("<4sHHI", b"ALL4", 1, 0, len(content)) + frame
    assert decompress_all4(data) == content
    for broken in (data[:-3], data + frame):
        with pytest.raises(ALFormatError):
            decompress_all4(broken)
    changed = bytearray(data)
    struct.pack_into("<I", changed, 8, len(content) - 1)
    with pytest.raises(ALFormatError) as error:
        decompress_all4(bytes(changed))
    assert error.value.code == "length_mismatch"

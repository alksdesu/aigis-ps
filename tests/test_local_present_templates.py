import hashlib
from pathlib import Path
import struct

import lz4.frame
import pytest

from aigis_ps.al_format import ALFormatError, decompress_all4, parse_table
from aigis_ps.alxml import loads
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.local_present_templates import FIELDS, LIMITS, LOCAL_TEMPLATE_CARDS, RECORD, build_present_template_overlay
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.public_tables import MASTER_LIMITS
from aigis_ps.resource_library import ResourceLibrary
from test_al_format import schema_bytes


CARDS = frozenset(LOCAL_TEMPLATE_CARDS.values()) | {99}
ORIGINAL = (42, 99, 10, 25, 4, -2, 90, 2, True, 7)


def template(identifier, card_id, **changes):
    row = dict(zip(FIELDS, (identifier, card_id, 0, -1, 1, 0, 0, 0, False, 0)))
    row.update(changes)
    return tuple(row[field] for field in FIELDS)


def table_bytes(rows=(ORIGINAL,), *, fields=None, tag=b"prun"):
    if fields is None:
        fields = [(name, index * 4, 5 if index == 8 else 1, b"\xff" if index == 8 else b"\0" * 8)
                  for index, name in enumerate(FIELDS)]
    schema = schema_bytes(fields, RECORD.size)
    return (struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(rows), 20, 20 + len(schema), RECORD.size, tag)
            + schema + b"".join(RECORD.pack(*row) for row in rows))


def all4(data, **kwargs):
    return struct.pack("<4sHHI", b"ALL4", 1, 0, len(data)) + lz4.frame.compress(data, **kwargs)


def decoded(raw):
    return decompress_all4(raw) if raw.startswith(b"ALL4") else raw


def test_fixed_rows_append_without_changing_original_header_schema_or_records():
    raw = table_bytes()
    before = bytes(raw)
    result = build_present_template_overlay(raw, CARDS)
    original, changed = parse_table(raw), parse_table(result)
    assert result.startswith(b"ALTB") and raw == before
    assert result[:6] == raw[:6] and result[8:len(raw)] == raw[8:]
    assert changed["record_count"] == original["record_count"] + 4
    assert changed["rows"][:1] == original["rows"]
    assert changed["fields"] == original["fields"]
    assert changed["rows"][1:] == [dict(zip(FIELDS, template(identifier, card_id)))
                                   for identifier, card_id in LOCAL_TEMPLATE_CARDS.items()]


def test_input_padding_and_nonstandard_original_values_remain_byte_identical():
    raw = bytearray(table_bytes())
    offset = parse_table(bytes(raw))["data_offset"]
    raw[offset + 33:offset + 36] = b"\x55\xaa\x12"
    source = bytes(raw)
    result = build_present_template_overlay(source, CARDS)
    assert result[8:len(source)] == source[8:]
    assert parse_table(result)["rows"][0] == dict(zip(FIELDS, ORIGINAL))


@pytest.mark.parametrize("compressed", [False, True])
def test_repeated_generation_is_byte_idempotent(compressed):
    raw = all4(table_bytes()) if compressed else table_bytes()
    result = build_present_template_overlay(raw, CARDS)
    assert build_present_template_overlay(result, CARDS) == result
    assert parse_table(decoded(result))["record_count"] == 5


@pytest.mark.parametrize("store_size", [False, True])
@pytest.mark.parametrize("checksums", [False, True])
def test_all4_format_and_frame_settings_are_preserved(store_size, checksums):
    raw = all4(table_bytes(), block_size=lz4.frame.BLOCKSIZE_MAX256KB, block_linked=False,
               content_checksum=checksums, block_checksum=checksums, store_size=store_size)
    result = build_present_template_overlay(raw, CARDS)
    assert result[:8] == raw[:8]
    assert struct.unpack_from("<I", result, 8)[0] == len(decompress_all4(result))
    old, new = lz4.frame.get_frame_info(raw[12:]), lz4.frame.get_frame_info(result[12:])
    for key in ("block_size_id", "block_linked", "content_checksum", "block_checksum", "skippable"):
        assert new[key] == old[key]
    assert bool(new["content_size"]) == bool(old["content_size"]) == store_size
    assert parse_table(decompress_all4(result))["record_count"] == 5


def test_partial_existing_local_rows_only_append_missing_fixed_identifiers():
    expected = list(LOCAL_TEMPLATE_CARDS.items())
    raw = table_bytes((ORIGINAL, template(*expected[0]), template(*expected[2])))
    result = build_present_template_overlay(raw, CARDS)
    assert result[8:len(raw)] == raw[8:]
    assert [row["PresentID"] for row in parse_table(result)["rows"]] == [42, expected[0][0], expected[2][0], expected[1][0], expected[3][0]]


def test_new_official_low_id_template_does_not_replace_reserved_local_identity():
    local_id, card_id = next(iter(LOCAL_TEMPLATE_CARDS.items()))
    raw = table_bytes((template(7, card_id, InitSkillLevel=0), template(local_id, card_id)))
    result = build_present_template_overlay(raw, CARDS)
    rows = parse_table(result)["rows"]
    assert rows[:2] == parse_table(raw)["rows"]
    assert len([row for row in rows if row["CardID"] == card_id]) == 2
    assert len(rows) == 5


@pytest.mark.parametrize("field", FIELDS[1:])
def test_reserved_id_with_different_content_is_rejected(field):
    local_id, card_id = next(iter(LOCAL_TEMPLATE_CARDS.items()))
    value = True if field == "ClassChangeEnable" else 123
    raw = table_bytes((ORIGINAL, template(local_id, card_id, **{field: value})))
    with pytest.raises(ALFormatError) as error:
        build_present_template_overlay(raw, CARDS)
    assert error.value.code == "local_present_id_conflict"


@pytest.mark.parametrize("rows", [(ORIGINAL, ORIGINAL), (template(-1, 99),)])
def test_duplicate_or_negative_existing_template_ids_are_rejected(rows):
    with pytest.raises(ALFormatError) as error:
        build_present_template_overlay(table_bytes(rows), CARDS)
    assert error.value.code == "local_present_ids"


@pytest.mark.parametrize("missing", tuple(LOCAL_TEMPLATE_CARDS.values()))
def test_every_local_target_must_be_known(missing):
    with pytest.raises(ALFormatError) as error:
        build_present_template_overlay(table_bytes(), CARDS - {missing})
    assert error.value.code == "local_present_cards" and str(missing) in str(error.value)


@pytest.mark.parametrize("cards", [None, 1, "1605", b"1605", {1605: True}, iter(CARDS),
    list(CARDS) + [True], list(CARDS) + [1605], list(CARDS) + [0], list(CARDS) + [2 ** 31]])
def test_invalid_known_card_collections_are_rejected(cards):
    with pytest.raises(ALFormatError) as error:
        build_present_template_overlay(table_bytes(), cards)
    assert error.value.code == "local_present_cards"


@pytest.mark.parametrize("change", ["name", "offset", "type", "parameters", "missing", "extra"])
def test_changed_or_nonfixed_schema_is_rejected(change):
    fields = [(name, index * 4, 5 if index == 8 else 1, b"\xff" if index == 8 else b"\0" * 8)
              for index, name in enumerate(FIELDS)]
    if change == "name":
        fields[0] = ("OtherID", *fields[0][1:])
    elif change == "offset":
        fields[1] = ("CardID", 0, 1, b"\0" * 8)
    elif change == "type":
        fields[2] = ("InitExp", 8, 32, b"")
    elif change == "parameters":
        fields[2] = ("InitExp", 8, 1, b"\1" + b"\0" * 7)
    elif change == "missing":
        fields.pop()
    else:
        fields.append(("Extra", 0, 1, b"\0" * 8))
    with pytest.raises(ALFormatError):
        build_present_template_overlay(table_bytes(fields=fields), CARDS)


def test_changed_boolean_flags_and_noncanonical_boolean_are_rejected():
    raw = table_bytes()
    invalid_bool = bytearray(raw)
    invalid_bool[parse_table(raw)["data_offset"] + 32] = 2
    with pytest.raises(ALFormatError):
        build_present_template_overlay(bytes(invalid_bool), CARDS)
    schema = bytearray(raw)
    name_at = schema.index(b"ClassChangeEnable\0")
    schema[name_at - 3] = 8
    with pytest.raises(ALFormatError):
        build_present_template_overlay(bytes(schema), CARDS)


@pytest.mark.parametrize("raw", [None, "ALTB", b"", b"ALTB", bytearray(b"ALTB"), b"ALTB" + b"\0" * 40])
def test_invalid_resource_inputs_are_rejected(raw):
    with pytest.raises(ALFormatError):
        build_present_template_overlay(raw, CARDS)


def test_changed_header_truncation_and_trailing_bytes_are_rejected():
    source = table_bytes()
    flags = bytearray(source)
    flags[5] = 0
    count = bytearray(source)
    struct.pack_into("<H", count, 6, 0)
    for raw in (table_bytes(tag=b"test"), bytes(flags), bytes(count), source[:-1], source + b"\0", all4(source)[:-2]):
        with pytest.raises(ALFormatError):
            build_present_template_overlay(raw, CARDS)


def test_uint16_row_count_cannot_overflow():
    source = table_bytes(tuple(template(index, 99) for index in range(65532)))
    with pytest.raises(ALFormatError) as error:
        build_present_template_overlay(source, CARDS)
    assert error.value.code == "local_present_capacity"


def test_resource_limit_applies_before_parsing():
    with pytest.raises(ALFormatError):
        build_present_template_overlay(b"x" * (LIMITS.max_input_bytes + 1), CARDS)


@pytest.fixture(scope="module")
def real_resource():
    project = Path(__file__).resolve().parents[1]
    master_root = project / "runtime/server/masterdata"
    masters = MasterDataStore(master_root)
    if not (master_root / "active.json").is_file():
        pytest.skip("Verified local public masters are required")
    snapshot = masters.snapshot()
    _, entries = ResourceLibrary(project / "runtime/resources", client_hash=snapshot.client_hash).load()
    raw, _ = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe").read("PresentUnitData.atb")
    cards = next(column.values for column in loads(snapshot.get_xml("CardData"), limits=MASTER_LIMITS).children
                 if column.name == "CardID")
    return raw, cards


def test_real_resource_all_2039_original_rows_and_bytes_remain_intact(real_resource):
    raw, cards = real_resource
    digest = hashlib.sha256(raw).digest()
    original = decompress_all4(raw)
    table = parse_table(original)
    assert table["record_count"] == 2039 and table["tag"] == "prun"
    result = build_present_template_overlay(raw, cards)
    updated = decompress_all4(result)
    parsed = parse_table(updated)
    assert updated[:6] == original[:6] and updated[8:len(original)] == original[8:]
    assert parsed["fields"] == table["fields"] and parsed["rows"][:2039] == table["rows"]
    assert parsed["record_count"] == 2043
    assert parsed["rows"][2039:] == [dict(zip(FIELDS, template(identifier, card_id)))
                                      for identifier, card_id in LOCAL_TEMPLATE_CARDS.items()]
    assert build_present_template_overlay(result, cards) == result
    assert hashlib.sha256(raw).digest() == digest

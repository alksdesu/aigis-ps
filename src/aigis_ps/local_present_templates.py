from __future__ import annotations

from collections.abc import Collection, Mapping
import struct
from types import MappingProxyType

from .al_format import ALFormatError, ParseLimits, decompress_all4, parse_table
from .native_numbers import INT32_MAX


LOCAL_TEMPLATE_CARDS = MappingProxyType({2000000001: 1605, 2000000002: 1777,
                                       2000000003: 1785, 2000000004: 2761})
MAX_ROWS = 65535
LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024,
                     max_rows=MAX_ROWS, max_fields=10, max_cells=MAX_ROWS * 10)
FIELDS = ("PresentID", "CardID", "InitExp", "InitSkillID", "InitSkillLevel", "CostMod", "InitLove",
          "EvolutionCount", "ClassChangeEnable", "ClassChangeStatusMod")
RECORD = struct.Struct("<8i?3xi")


def _fail(message, code="local_present_format"):
    raise ALFormatError(message, code=code)


def _known_cards(cards):
    if (not isinstance(cards, Collection) or isinstance(cards, (str, bytes, Mapping))
            or len(cards) > MAX_ROWS):
        _fail("Known cards must be a bounded collection of CardIDs", "local_present_cards")
    identifiers = set()
    for card_id in cards:
        if type(card_id) is not int or not 0 < card_id <= INT32_MAX or card_id in identifiers:
            _fail("Known CardIDs must be distinct positive int32 values", "local_present_cards")
        identifiers.add(card_id)
    missing = set(LOCAL_TEMPLATE_CARDS.values()) - identifiers
    if missing:
        _fail("Local present targets are absent from verified CardData: " + ", ".join(map(str, sorted(missing))),
              "local_present_cards")


def build_present_template_overlay(raw: bytes, cards: Collection[int]) -> bytes:
    _known_cards(cards)
    if not isinstance(raw, bytes) or not raw or len(raw) > LIMITS.max_input_bytes:
        _fail("Present template input must be bounded resource bytes")
    compressed = raw.startswith(b"ALL4")
    data = decompress_all4(raw, LIMITS) if compressed else raw
    table = parse_table(data, LIMITS)
    if (table["version"] != 1 or table["flags"] != 0x10 or table["tag"] != "prun"
            or table["record_size"] != RECORD.size or table["string_pool_size"]
            or table["names_offset"] is not None or len(table["fields"]) != len(FIELDS)):
        _fail("Present templates require the verified fixed-row prun table")
    for index, (name, field) in enumerate(zip(FIELDS, table["fields"])):
        boolean = index == 8
        expected = (name, index * 4, 5 if boolean else 1, 1 if boolean else 8,
                    1 if boolean else 4, "ff" if boolean else "00" * 8)
        actual = tuple(field[key] for key in ("name", "offset", "type_code", "flags", "size", "parameters"))
        if actual != expected:
            _fail("Present template fields differ from the verified fixed-row schema")
    record_end = table["data_offset"] + table["record_count"] * RECORD.size
    if record_end != len(data):
        _fail("Present template records must occupy the complete final section")
    existing = {}
    for index, row in enumerate(table["rows"]):
        identifier = row["PresentID"]
        if identifier < 0 or identifier in existing:
            _fail("Present template IDs must be unique nonnegative int32 values", "local_present_ids")
        if data[table["data_offset"] + index * RECORD.size + 32] not in (0, 1):
            _fail("Present template boolean is not a canonical native value")
        existing[identifier] = row
    additions = []
    for identifier, card_id in LOCAL_TEMPLATE_CARDS.items():
        values = (identifier, card_id, 0, -1, 1, 0, 0, 0, False, 0)
        expected = dict(zip(FIELDS, values))
        if identifier in existing:
            if existing[identifier] != expected:
                _fail("Reserved local present template ID has conflicting content: " + str(identifier),
                      "local_present_id_conflict")
        else:
            additions.append(RECORD.pack(*values))
    if not additions:
        return raw
    count = table["record_count"] + len(additions)
    if count > MAX_ROWS:
        _fail("Local present templates exceed the native row-count capacity", "local_present_capacity")
    updated = bytearray(data)
    struct.pack_into("<H", updated, 6, count)
    updated.extend(b"".join(additions))
    result = bytes(updated)
    parse_table(result, LIMITS)
    if not compressed:
        return result
    import lz4.frame
    frame = lz4.frame.get_frame_info(raw[12:])
    encoded = lz4.frame.compress(result, block_size=frame["block_size_id"],
                                 block_linked=frame["block_linked"], content_checksum=frame["content_checksum"],
                                 block_checksum=frame["block_checksum"], store_size=bool(frame["content_size"]))
    wrapped = raw[:8] + struct.pack("<I", len(result)) + encoded
    if len(wrapped) > LIMITS.max_input_bytes or decompress_all4(wrapped, LIMITS) != result:
        _fail("Local present output failed its resource-container validation")
    return wrapped

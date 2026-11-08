from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import struct
import zlib

import lz4.frame
import pytest

from aigis_ps.alxml import loads
from aigis_ps.errors import ProtocolError
import aigis_ps.image_aliases as module
from aigis_ps.image_aliases import MAX_ENCODED_COMMAND, ImageAlias, ImageAliasCatalog
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.public_tables import MASTER_LIMITS
from aigis_ps.system_literals import decode_command, encode_command as encode
from test_al_format import schema_bytes
from test_native_numbers import BOUNDARY_VALUES, fits_int32, override_decoded_cell
from test_quest_service import bundle


UNBOUNDED_REASON = "Alias command is not bounded hexadecimal text"
UTF8_REASON = "'utf-8' codec can't decode byte 0xfe in position 0: invalid start byte"


def command_id(name):
    return (zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000


def command_table(rows, *, command_field="_Cmnd"):
    schema = schema_bytes([("_ID", 0, 1, b"\0" * 8), (command_field, 4, 32, b"")], 8)
    records, pool = bytearray(), bytearray()
    for identifier, encoded in rows:
        records += struct.pack("<iI", identifier, len(pool))
        pool += encoded.encode() + b"\0"
    start = 28 + len(schema)
    return struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, len(rows), 28, start, 8,
                       len(pool), start + len(records), b"cmnd") + schema + records + pool


def rules(card_id=10, image_id=20, event_id=20):
    return [(command_id(f"IsExchangeUnit_{card_id}"), encode('IsRating("R18");')),
            (command_id(f"DoExchangeImageCardID_{card_id}"), encode(f'SetSystemStateParam("GlobalExcImageCardID", {image_id});')),
            (command_id(f"DoExchangeEventCardID_{card_id}"), encode(f'SetSystemStateParam("GlobalExcEventCardID", {event_id});'))]


def catalog_for(rows, card_ids=(10, 20, 30)):
    return ImageAliasCatalog(lambda name: command_table(rows), card_ids)


@pytest.fixture(scope="module")
def current_aliases(bundle):
    document = loads(bundle.masters.snapshot().get_xml("CardData"), limits=MASTER_LIMITS)
    names = [column.name for column in document.children]
    cards = {row["CardID"]: row for values in zip(*(column.values for column in document.children))
             if (row := dict(zip(names, values)))}
    catalog = ImageAliasCatalog(lambda name: bundle.native_cdn.assets.read(name)[0], cards)
    return catalog, cards


def test_current_resource_all_twenty_aliases_and_targets(current_aliases):
    catalog, cards = current_aliases
    expected = {497: 512, 498: 513, 499: 514, 500: 553, 501: 515, 684: 692, 685: 693,
                686: 694, 687: 695, 688: 696, 689: 697, 2693: 2694, 2697: 2702, 2698: 2703,
                2699: 2704, 2700: 2705, 2701: 2706, 2707: 2709, 2708: 2710, 2711: 2712}
    assert catalog.status()["count"] == len(expected)
    assert catalog.status()["unsupported"] == {}
    for card_id in cards:
        alias = catalog.resolve(card_id)
        target = expected.get(card_id, card_id)
        assert alias == ImageAlias(card_id, target, target, "R18" if card_id in expected else None)
        assert catalog.resolve(card_id, "ALL").image_card_id == card_id
    for original in (2700, 2701):
        assert cards[original]["Kind"] == 0
        assert cards[catalog.resolve(original).image_card_id]["Kind"] == 1
    for original, target in expected.items():
        for field in ("Rare", "LoveEv1", "BonusType", "BonusType3"):
            assert cards[original][field] == cards[target][field]


def test_conditional_aliases_are_one_hop_and_keep_event_identity_separate():
    catalog = catalog_for(rules(10, 20, 30) + rules(20, 30, 10))
    assert catalog.resolve(10) == ImageAlias(10, 20, 30, "R18")
    assert catalog.resolve(20) == ImageAlias(20, 30, 10, "R18")
    assert catalog.resolve(30) == ImageAlias(30, 30, 30, None)
    assert catalog.resolve(10, rating="ALL") == ImageAlias(10, 10, 10, "R18")


def test_verified_expression_whitespace_and_uppercase_hex():
    rows = [(command_id("IsExchangeUnit_10"), encode(' \r\nIsRating ( "R18" ) ;\t').upper()),
            (command_id("DoExchangeImageCardID_10"), encode(' SetSystemStateParam ( "GlobalExcImageCardID" , 20 ) ; ')),
            (command_id("DoExchangeEventCardID_10"), encode('SetSystemStateParam("GlobalExcEventCardID",30);'))]
    assert catalog_for(rows).resolve(10) == ImageAlias(10, 20, 30, "R18")


@pytest.mark.parametrize("index,command", [
    (0, 'IsRating("ALL");'), (0, 'IsRating("R18") || 1;'), (0, 'IsRating("R18");Other();'),
    (1, 'SetSystemStateParam("GlobalExcImageCardID", 10+10);'),
    (1, 'SetSystemStateParam("GlobalExcEventCardID", 20);'),
    (1, 'SetSystemStateParam("GlobalExcImageCardID", 20);WriteFile();'),
    (1, 'SetSystemStateParam("GlobalExcImageCardID", -1);'),
    (1, 'SetSystemStateParam("GlobalExcImageCardID", 0);'),
    (1, 'SetSystemStateParam("GlobalExcImageCardID", 2147483648);'),
    (2, 'SetSystemStateParam("GlobalExcEventCardID", 999);'),
    (2, ''),
])
def test_unknown_expression_or_target_only_blocks_its_card(index, command):
    broken = rules()
    broken[index] = (broken[index][0], encode(command))
    catalog = catalog_for(broken + rules(20, 30, 30))
    assert catalog.status()["ready"] is True
    assert set(catalog.status()["unsupported"]) == {"10"}
    with pytest.raises(ProtocolError) as caught:
        catalog.resolve(10)
    assert caught.value.code == "image_alias_unsupported" and caught.value.status == 503
    assert catalog.resolve(20).image_card_id == 30
    assert catalog.resolve(30).image_card_id == 30


@pytest.mark.parametrize("mutation", ["missing_condition", "missing_image", "missing_event", "duplicate"])
def test_incomplete_and_duplicate_groups_do_not_block_other_cards(mutation):
    rows = rules()
    if mutation == "duplicate":
        rows.append(rows[1])
    else:
        rows.pop({"missing_condition": 0, "missing_image": 1, "missing_event": 2}[mutation])
    catalog = catalog_for(rows + rules(20, 30, 30))
    with pytest.raises(ProtocolError, match="unsupported"):
        catalog.resolve(10, "ALL")
    assert catalog.resolve(20).event_card_id == 30


@pytest.mark.parametrize("encoded,reason", [("0", UNBOUNDED_REASON), ("zz", UNBOUNDED_REASON),
                                            ("fe 58", UNBOUNDED_REASON), ("00", UTF8_REASON), ("00" * 4097, UNBOUNDED_REASON)],
                         ids=["odd_hex", "invalid_hex", "spaced_hex", "invalid_utf8", "too_long"])
def test_invalid_encoding_is_local_to_the_affected_alias(encoded, reason):
    rows = rules()
    rows[1] = (rows[1][0], encoded)
    catalog = catalog_for(rows)
    assert catalog.status()["unsupported"] == {"10": reason}
    with pytest.raises(ProtocolError, match="unsupported"):
        catalog.resolve(10)
    assert catalog.resolve(20) == ImageAlias(20, 20, 20, None)


@pytest.mark.parametrize("length,reason", [(MAX_ENCODED_COMMAND, None), (MAX_ENCODED_COMMAND + 2, UNBOUNDED_REASON)])
def test_alias_commands_keep_a_tighter_bound_than_the_shared_decoder(length, reason):
    text = 'IsRating("R18");'.ljust(length // 2)
    rows = rules()
    rows[0] = (rows[0][0], encode(text))
    assert len(rows[0][1]) == length and decode_command(rows[0][1]) == text
    catalog = catalog_for(rows)
    if reason is None:
        assert catalog.status()["unsupported"] == {} and catalog.resolve(10) == ImageAlias(10, 20, 20, "R18")
    else:
        assert catalog.status()["unsupported"] == {"10": reason}


@pytest.mark.parametrize("broken,reason", [({0: "00", 1: "zz"}, UTF8_REASON), ({0: "zz", 2: "00"}, UNBOUNDED_REASON)],
                         ids=["utf8_first", "hex_first"])
def test_the_first_undecodable_command_of_a_group_names_its_reason(broken, reason):
    rows = rules()
    for index, encoded in broken.items():
        rows[index] = (rows[index][0], encoded)
    assert catalog_for(rows).status()["unsupported"] == {"10": reason}


@pytest.mark.parametrize("key", BOUNDARY_VALUES)
def test_command_keys_admit_exactly_the_native_int32_range(monkeypatch, key):
    override_decoded_cell(monkeypatch, module, "_ID", key)
    rows = [(command_id("UnrelatedCommand"), encode("1;"))] + rules()
    if fits_int32(key):
        assert catalog_for(rows).resolve(10) == ImageAlias(10, 20, 20, "R18")
    else:
        with pytest.raises(ProtocolError) as caught:
            catalog_for(rows)
        assert caught.value.code == "image_alias_catalog_invalid"


def test_card_identifiers_and_alias_targets_reach_the_native_int32_maximum():
    catalog = catalog_for(rules(10, INT32_MAX, INT32_MAX), (10, INT32_MAX))
    assert catalog.resolve(10) == ImageAlias(10, INT32_MAX, INT32_MAX, "R18")
    assert catalog.resolve(INT32_MAX) == ImageAlias(INT32_MAX, INT32_MAX, INT32_MAX, None)


def test_unrelated_commands_are_not_executed_or_required_to_be_supported():
    rows = rules() + [(command_id("UnrelatedCommand"), "not-an-alias-expression")]
    assert catalog_for(rows).resolve(10).image_card_id == 20
    assert catalog_for([]).resolve(10) == ImageAlias(10, 10, 10, None)


def test_inputs_and_exposed_results_cannot_mutate_catalog():
    card_ids = [10, 20, 30]
    rows = rules()
    before = deepcopy(rows)
    catalog = catalog_for(rows, card_ids)
    card_ids.clear()
    report = catalog.status()
    report["resource_sha256"].clear()
    report["unsupported"]["10"] = "fake"
    assert catalog.status()["resource_sha256"] and catalog.status()["unsupported"] == {}
    with pytest.raises(FrozenInstanceError):
        catalog.resolve(10).image_card_id = 30
    assert rows == before and catalog.resolve(10).image_card_id == 20


def test_all4_and_resource_hashes():
    raw = command_table(rules())
    packed = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    seen = []
    def reader(name):
        seen.append(name)
        return packed
    catalog = ImageAliasCatalog(reader, [10, 20, 30])
    assert seen == ["OnCommand.atb"]
    assert catalog.resolve(10).image_card_id == 20
    assert catalog.status()["resource_sha256"] == {"OnCommand.atb": hashlib.sha256(packed).hexdigest()}
    assert catalog.status()["decoded_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("card_ids", [[], "10", [True, 20], [0, 20], [-1, 20], [10, 10], [0x80000000], None])
def test_invalid_card_domains_fail_catalog_creation(card_ids):
    with pytest.raises(ProtocolError) as caught:
        catalog_for(rules(), card_ids)
    assert caught.value.code == "image_alias_catalog_invalid" and caught.value.status == 503


@pytest.mark.parametrize("payload", [b"bad", "not bytes", b"x" * (8 * 1024 * 1024 + 1)],
                         ids=["bad_magic", "not_bytes", "too_large"])
def test_bad_resource_fails_globally(payload):
    with pytest.raises(ProtocolError, match="native table contract"):
        ImageAliasCatalog(lambda name: payload, [10, 20])


def test_wrong_command_schema_fails_globally():
    with pytest.raises(ProtocolError, match="native table contract"):
        ImageAliasCatalog(lambda name: command_table(rules(), command_field="Command"), [10, 20])


@pytest.mark.parametrize("card_id", [True, False, 0, -1, 999, 0x80000000, "10", None, []])
def test_unknown_or_bad_card_cannot_resolve(card_id):
    with pytest.raises(ProtocolError) as caught:
        catalog_for(rules()).resolve(card_id)
    assert caught.value.code == "image_alias_card_unknown"


@pytest.mark.parametrize("rating", [None, 1, "", "x" * 65])
def test_bad_rating_is_rejected(rating):
    with pytest.raises(ProtocolError) as caught:
        catalog_for(rules()).resolve(10, rating)
    assert caught.value.code == "image_alias_rating_invalid"

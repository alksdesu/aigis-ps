import struct

import pytest

from client import capture_pe


IMAGE_BASE = 0x400000
TEXT_RVA = 0x1000
RELOC_RVA = 0x2000
OPTIONAL = 0x98


def executable(entries=(0x3016,), *, page=TEXT_RVA, block_size=None, directory_size=None):
    entries = (*entries, 0) if len(entries) % 2 else entries
    raw = bytearray(0x600)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 0x3C, 0x80)
    raw[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", raw, 0x84, 0x14C, 2)
    struct.pack_into("<H", raw, 0x94, 0xE0)
    struct.pack_into("<H", raw, OPTIONAL, 0x10B)
    struct.pack_into("<I", raw, OPTIONAL + 28, IMAGE_BASE)
    struct.pack_into("<I", raw, OPTIONAL + 56, 0x3000)
    struct.pack_into("<I", raw, OPTIONAL + 92, 16)
    size = 8 + 2 * len(entries)
    struct.pack_into("<II", raw, OPTIONAL + 136, RELOC_RVA, size if directory_size is None else directory_size)
    for index, (rva, offset) in enumerate(((TEXT_RVA, 0x200), (RELOC_RVA, 0x400))):
        struct.pack_into("<IIII", raw, OPTIONAL + 0xE0 + index * 40 + 8, 0x200, rva, 0x200, offset)
    raw[0x210:0x230] = b"\x90" * 32
    struct.pack_into("<I", raw, 0x216, IMAGE_BASE + 0x1100)
    struct.pack_into("<II", raw, 0x400, page, size if block_size is None else block_size)
    for index, entry in enumerate(entries):
        struct.pack_into("<H", raw, 0x408 + index * 2, entry)
    return raw


def signatures(raw, start=0x1010, length=16):
    offset = 0x200 + start - TEXT_RVA
    return capture_pe.signature_blocks(bytes(raw), [(start, bytes(raw[offset:offset + length]))])


def test_highlow_relocation_keeps_all_signature_bytes_and_exposes_target_rva():
    raw = executable()
    block, = signatures(raw)
    assert block == {"rva": 0x1010, "bytes": list(raw[0x210:0x220]),
                     "relocations": [{"offset": 6, "rva": 0x1100}]}


@pytest.mark.parametrize("length", [7, 8, 9])
def test_truncated_relocation_at_signature_end_is_expanded(length):
    raw = executable()
    block, = signatures(raw, length=length)
    assert block["bytes"] == list(raw[0x210:0x21A])
    assert block["relocations"] == [{"offset": 6, "rva": 0x1100}]


@pytest.mark.parametrize("start", [0x1017, 0x1018, 0x1019])
def test_truncated_relocation_at_signature_start_is_expanded(start):
    raw = executable()
    block, = signatures(raw, start=start, length=8)
    assert block["rva"] == 0x1016
    assert block["bytes"] == list(raw[0x216:0x200 + start - TEXT_RVA + 8])
    assert block["relocations"] == [{"offset": 0, "rva": 0x1100}]


def test_relocation_outside_signature_does_not_change_unrelated_bytes():
    raw = executable()
    block, = signatures(raw, start=0x101A, length=6)
    assert block == {"rva": 0x101A, "bytes": list(raw[0x21A:0x220]), "relocations": []}


def test_absolute_padding_is_ignored_and_hex_prefixes_are_accepted():
    raw = executable((0, 0x3016, 0))
    block, = capture_pe.signature_blocks(bytes(raw), [(0x1010, raw[0x210:0x220].hex())])
    assert block["relocations"] == [{"offset": 6, "rva": 0x1100}]


def test_absent_relocation_directory_preserves_fixed_image_signatures():
    raw = executable()
    struct.pack_into("<II", raw, OPTIONAL + 136, 0, 0)
    assert signatures(raw)[0]["relocations"] == []


def test_static_prefix_mismatch_is_never_hidden_by_relocation():
    raw = executable()
    expected = bytes(raw[0x210:0x220])
    raw[0x216] ^= 1
    with pytest.raises(capture_pe.masters.CaptureError, match="capture_signature_mismatch"):
        capture_pe.signature_blocks(bytes(raw), [(0x1010, expected)])


@pytest.mark.parametrize("entries,code", [
    ((0x1016,), "unsupported_capture_relocation"),
    ((0xA016,), "unsupported_capture_relocation"),
    ((0x3016, 0x3016), "invalid_capture_relocations"),
    ((0x3016, 0x3018), "invalid_capture_relocations"),
])
def test_unknown_duplicate_and_overlapping_relocations_are_rejected(entries, code):
    with pytest.raises(capture_pe.masters.CaptureError, match=code):
        signatures(executable(entries))


@pytest.mark.parametrize("options", [
    {"block_size": 0}, {"block_size": 6}, {"block_size": 9}, {"block_size": 10}, {"block_size": 16},
    {"directory_size": 7}, {"directory_size": 11}, {"page": 0x1001}, {"page": 0x3000},
])
def test_malformed_relocation_blocks_are_rejected(options):
    with pytest.raises(capture_pe.masters.CaptureError, match="invalid_capture_relocations"):
        signatures(executable(**options))


def test_relocation_field_outside_file_backed_section_is_rejected():
    with pytest.raises(capture_pe.masters.CaptureError, match="invalid_executable"):
        signatures(executable((0x31FF,)))


@pytest.mark.parametrize("target", [IMAGE_BASE - 1, IMAGE_BASE + 0x3000])
def test_relocation_targets_outside_image_are_rejected(target):
    raw = executable()
    struct.pack_into("<I", raw, 0x216, target)
    with pytest.raises(capture_pe.masters.CaptureError, match="invalid_capture_relocation_target"):
        signatures(raw)


@pytest.mark.parametrize("offset,value,format", [
    (0x84, 0x8664, "H"), (OPTIONAL, 0x20B, "H"), (0x94, 0x80, "H"),
    (OPTIONAL + 92, 5, "I"), (OPTIONAL + 92, 17, "I"),
    (OPTIONAL + 136, 0, "I"), (OPTIONAL + 136, 0x2FFF, "I"),
    (OPTIONAL + 56, 0, "I"), (OPTIONAL + 28, 0xFFFFF000, "I"),
])
def test_invalid_pe_layout_is_rejected(offset, value, format):
    raw = executable()
    struct.pack_into("<" + format, raw, offset, value)
    with pytest.raises(capture_pe.masters.CaptureError, match="invalid_capture_pe"):
        signatures(raw)


@pytest.mark.parametrize("rva,expected", [(True, b"a"), (-1, b"a"), (0x3000, b"a"),
                                          (0x1010, b""), (0x1010, "zz"), (0x1010, [])])
def test_invalid_signature_inputs_are_rejected(rva, expected):
    with pytest.raises(capture_pe.masters.CaptureError, match="invalid_capture_signature"):
        capture_pe.signature_blocks(bytes(executable()), [(rva, expected)])


@pytest.mark.parametrize("blocks", [None, "signature", b"signature", [1], [(0x1010, b"a", 1)]])
def test_invalid_signature_collection_is_rejected(blocks):
    with pytest.raises(capture_pe.masters.CaptureError, match="invalid_capture_signature"):
        capture_pe.signature_blocks(bytes(executable()), blocks)

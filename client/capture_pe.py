from bisect import bisect_left
from collections.abc import Sequence
import struct

if __package__:
    from . import capture_masters as masters
else:
    import capture_masters as masters


def _layout(raw: bytes) -> tuple[int, int, int, int]:
    try:
        pe, = struct.unpack_from("<I", raw, 0x3C)
        if raw[:2] != b"MZ" or raw[pe:pe + 4] != b"PE\0\0":
            raise ValueError
        machine, sections = struct.unpack_from("<HH", raw, pe + 4)
        optional_size, = struct.unpack_from("<H", raw, pe + 20)
        optional = pe + 24
        magic, = struct.unpack_from("<H", raw, optional)
        if machine != 0x14C or magic != 0x10B or optional_size < 144 or not sections:
            raise ValueError
        if optional + optional_size + sections * 40 > len(raw):
            raise ValueError
        image_base, = struct.unpack_from("<I", raw, optional + 28)
        image_size, = struct.unpack_from("<I", raw, optional + 56)
        directory_count, = struct.unpack_from("<I", raw, optional + 92)
        if not image_size or image_base + image_size > 0x100000000:
            raise ValueError
        if not 6 <= directory_count <= (optional_size - 96) // 8:
            raise ValueError
        relocation_rva, relocation_size = struct.unpack_from("<II", raw, optional + 136)
        if bool(relocation_rva) != bool(relocation_size) or relocation_rva + relocation_size > image_size:
            raise ValueError
        return image_base, image_size, relocation_rva, relocation_size
    except (struct.error, ValueError):
        raise masters.CaptureError("invalid_capture_pe") from None


def _relocations(raw: bytes, image_size: int, rva: int, size: int) -> list[int]:
    if not size:
        return []
    directory = masters._image_bytes(raw, rva, size)
    cursor = 0
    result = set()
    while cursor < len(directory):
        if len(directory) - cursor < 8:
            raise masters.CaptureError("invalid_capture_relocations")
        page, block_size = struct.unpack_from("<II", directory, cursor)
        if page % 0x1000 or page >= image_size or block_size < 8 or block_size % 4 or block_size > len(directory) - cursor:
            raise masters.CaptureError("invalid_capture_relocations")
        for offset in range(cursor + 8, cursor + block_size, 2):
            entry, = struct.unpack_from("<H", directory, offset)
            kind, displacement = entry >> 12, entry & 0xFFF
            if kind == 0:
                continue
            if kind != 3:
                raise masters.CaptureError("unsupported_capture_relocation")
            address = page + displacement
            if address + 4 > image_size or address in result:
                raise masters.CaptureError("invalid_capture_relocations")
            masters._image_bytes(raw, address, 4)
            result.add(address)
        cursor += block_size
    ordered = sorted(result)
    if any(right < left + 4 for left, right in zip(ordered, ordered[1:])):
        raise masters.CaptureError("invalid_capture_relocations")
    return ordered


def signature_blocks(raw: bytes, blocks: Sequence[tuple[int, str | bytes]]) -> list[dict]:
    if not isinstance(raw, bytes):
        raise masters.CaptureError("invalid_capture_pe")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        raise masters.CaptureError("invalid_capture_signature")
    image_base, image_size, relocation_rva, relocation_size = _layout(raw)
    relocations = _relocations(raw, image_size, relocation_rva, relocation_size)
    result = []
    for block in blocks:
        if not isinstance(block, (tuple, list)) or len(block) != 2:
            raise masters.CaptureError("invalid_capture_signature")
        rva, encoded = block
        try:
            expected = bytes.fromhex(encoded) if isinstance(encoded, str) else encoded
        except ValueError:
            raise masters.CaptureError("invalid_capture_signature") from None
        if type(rva) is not int or rva < 0 or not isinstance(expected, bytes) or not expected or rva + len(expected) > image_size:
            raise masters.CaptureError("invalid_capture_signature")
        if masters._image_bytes(raw, rva, len(expected)) != expected:
            raise masters.CaptureError("capture_signature_mismatch")
        start, end = rva, rva + len(expected)
        index = bisect_left(relocations, start - 3)
        selected = []
        while index < len(relocations) and relocations[index] < end:
            address = relocations[index]
            start, end = min(start, address), max(end, address + 4)
            value, = struct.unpack("<I", masters._image_bytes(raw, address, 4))
            target = value - image_base
            if not 0 <= target < image_size:
                raise masters.CaptureError("invalid_capture_relocation_target")
            selected.append((address, target))
            index += 1
        result.append({"rva": start, "bytes": list(masters._image_bytes(raw, start, end - start)),
                       "relocations": [{"offset": address - start, "rva": target} for address, target in selected]})
    return result

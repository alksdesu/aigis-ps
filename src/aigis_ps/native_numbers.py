from __future__ import annotations

import struct


INT32_MIN = -0x80000000
INT32_MAX = 0x7FFFFFFF
UINT32_MASK = 0xFFFFFFFF


def int32(value: object) -> bool:
    return type(value) is int and INT32_MIN <= value <= INT32_MAX


def signed32(value: int) -> int:
    return ((value - INT32_MIN) & UINT32_MASK) + INT32_MIN


def float32(value: int | float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]

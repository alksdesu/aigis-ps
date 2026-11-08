from __future__ import annotations

import json
import re
import zlib


RATING_COMMAND = "IsCanUseRating_{}"
_KEY = bytes.fromhex("fe5866c7")
_MAX_ENCODED_LENGTH = 2 * 1024 * 1024
_INT32_SIGN = 0x80000000
_STRING = r'"(?:[^"\\\x00-\x1f]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})*"'
_NUMBER = r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?'
_ASSIGNMENT = re.compile(rf'\s*SetSystemConfigParam\s*\(\s*({_STRING})\s*,\s*({_STRING}|{_NUMBER})\s*\)\s*;')


def command_id(name):
    if not isinstance(name, str):
        raise ValueError("Native name hashes require text")
    return (zlib.crc32(name.encode("utf-8")) ^ _INT32_SIGN) - _INT32_SIGN


def rating_command_id(card_id: int) -> int:
    return command_id(RATING_COMMAND.format(card_id))


def _masked(data):
    return bytes(value ^ _KEY[index % len(_KEY)] for index, value in enumerate(data))


def decode_command(encoded):
    if (not isinstance(encoded, str) or len(encoded) > _MAX_ENCODED_LENGTH or len(encoded) % 2
            or re.fullmatch(r"[a-fA-F0-9]*", encoded) is None):
        raise ValueError("System command must be bounded hexadecimal text")
    return _masked(bytes.fromhex(encoded)).decode("utf-8")


def encode_command(text):
    if not isinstance(text, str):
        raise ValueError("System command text must be a string")
    encoded = _masked(text.encode("utf-8")).hex()
    if len(encoded) > _MAX_ENCODED_LENGTH:
        raise ValueError("System command exceeds the encoded length bound")
    return encoded


def system_literals(rows, defaults):
    result = dict(defaults)
    seen = set()
    for row in rows:
        text = decode_command(row["_Cmnd"])
        if not any(f'"{name}"' in text for name in defaults):
            continue
        position = 0
        while text[position:].strip():
            match = _ASSIGNMENT.match(text, position)
            if match is None:
                raise ValueError("Requested system setting occurs in a nonliteral command")
            name, value = (json.loads(part) for part in match.groups())
            if name in defaults:
                if name in seen:
                    raise ValueError("Requested system setting has ambiguous assignments")
                result[name] = value
                seen.add(name)
            position = match.end()
    return result

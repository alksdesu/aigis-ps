from __future__ import annotations

import re

from .al_format import ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError
from .system_literals import command_id, decode_command


LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024)
PLATFORM_TAGS = frozenset(("DMM", "R18", "WIN", "DGP"))
SMARTPHONE_IMAGE_COMMAND = command_id("UseSmartPhoneImage")
_PLATFORM = re.compile(r'\s*IsPlatForm\s*\(\s*"([A-Za-z0-9_]+)"\s*\)\s*;\s*')


def smartphone_images_enabled(asset_reader) -> bool:
    try:
        raw = asset_reader("OnSystemCommand.atb")
        if not isinstance(raw, bytes) or len(raw) > LIMITS.max_input_bytes:
            raise ValueError("Platform command table must be bounded bytes")
        data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
        table = parse_table(data, LIMITS)
        fields = {field["name"]: field["type_code"] for field in table["fields"]}
        if len(fields) != len(table["fields"]) or fields != {"_ID": 1, "_Cmnd": 32}:
            raise ValueError("Platform command schema changed")
        rows = [row for row in table["rows"] if row["_ID"] == SMARTPHONE_IMAGE_COMMAND]
        if not rows:
            return False
        if len(rows) != 1:
            raise ValueError("Platform image command is ambiguous")
        command = decode_command(rows[0]["_Cmnd"])
        if not command.strip():
            return True
        match = _PLATFORM.fullmatch(command)
        if match is None:
            raise ValueError("Platform image command has an unsupported expression")
        return match[1] in PLATFORM_TAGS
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise ProtocolError("unit_detail_options_invalid", "Unit image options differ from the verified PC contract", 503) from error

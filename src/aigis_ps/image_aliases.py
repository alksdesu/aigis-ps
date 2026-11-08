from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import hashlib
import re

from .al_format import ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError
from .native_numbers import int32
from .system_literals import command_id, decode_command


RESOURCE_NAME = "OnCommand.atb"
RESOURCE_LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024)
MAX_CARDS = 65536
MAX_ENCODED_COMMAND = 8192
PREFIXES = ("IsExchangeUnit", "DoExchangeImageCardID", "DoExchangeEventCardID")
SPACE = r"[ \t\r\n]*"
CONDITION = re.compile(rf'{SPACE}IsRating{SPACE}\({SPACE}"R18"{SPACE}\){SPACE};{SPACE}')
ASSIGNMENTS = tuple(re.compile(
    rf'{SPACE}SetSystemStateParam{SPACE}\({SPACE}"GlobalExc{field}CardID"{SPACE},{SPACE}([0-9]{{1,10}}){SPACE}\){SPACE};{SPACE}'
) for field in ("Image", "Event"))


@dataclass(frozen=True)
class ImageAlias:
    original_card_id: int
    image_card_id: int
    event_card_id: int
    condition_rating: str | None


def _card_id(value: object) -> bool:
    return int32(value) and value > 0


def _bounded(encoded: str) -> str:
    # The reason text reaches status() per card, and the alias bound is tighter than the shared decoder's.
    if len(encoded) > MAX_ENCODED_COMMAND or len(encoded) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", encoded):
        raise ValueError("Alias command is not bounded hexadecimal text")
    return encoded


class ImageAliasCatalog:
    def __init__(self, asset_reader: Callable[[str], bytes], card_ids: Iterable[int]):
        self._cards: set[int] = set()
        self._aliases: dict[int, ImageAlias] = {}
        self._unsupported: dict[int, str] = {}
        try:
            if not callable(asset_reader) or isinstance(card_ids, (str, bytes)):
                raise ValueError("A resource reader and card identifiers are required")
            for card_id in card_ids:
                if not _card_id(card_id) or card_id in self._cards or len(self._cards) >= MAX_CARDS:
                    raise ValueError("Card identifiers must be bounded, positive and unique")
                self._cards.add(card_id)
            if not self._cards:
                raise ValueError("Card identifiers are empty")
            raw = asset_reader(RESOURCE_NAME)
            if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMITS.max_input_bytes:
                raise ValueError("Alias resource must be bounded bytes")
            self._resource_sha256 = hashlib.sha256(raw).hexdigest()
            payload = decompress_all4(raw, RESOURCE_LIMITS) if raw.startswith(b"ALL4") else raw
            self._decoded_sha256 = hashlib.sha256(payload).hexdigest()
            table = parse_table(payload, RESOURCE_LIMITS)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if len(fields) != len(table["fields"]) or fields != {"_ID": 1, "_Cmnd": 32}:
                raise ValueError("Alias resource does not have the native command schema")
            owners: dict[int, set[int]] = {}
            keys = {}
            for card_id in sorted(self._cards):
                keys[card_id] = tuple(command_id(f"{prefix}_{card_id}") for prefix in PREFIXES)
                for key in keys[card_id]:
                    owners.setdefault(key, set()).add(card_id)
            commands: dict[int, list[str]] = {}
            for row in table["rows"]:
                key, encoded = row["_ID"], row["_Cmnd"]
                if not int32(key) or not isinstance(encoded, str):
                    raise ValueError("Alias command row has invalid field types")
                if key in owners:
                    commands.setdefault(key, []).append(encoded)
            for card_id, card_keys in keys.items():
                if not any(key in commands for key in card_keys):
                    continue
                try:
                    if len(set(card_keys)) != len(PREFIXES) or any(len(owners[key]) != 1 for key in card_keys):
                        raise ValueError("Alias command keys collide")
                    if any(len(commands.get(key, ())) != 1 for key in card_keys):
                        raise ValueError("Alias command group is incomplete or duplicated")
                    condition, image, event = (decode_command(_bounded(commands[key][0])) for key in card_keys)
                    if not CONDITION.fullmatch(condition):
                        raise ValueError("Alias condition is not the verified R18 predicate")
                    targets = []
                    for pattern, command in zip(ASSIGNMENTS, (image, event)):
                        match = pattern.fullmatch(command)
                        if match is None:
                            raise ValueError("Alias assignment contains an unsupported expression")
                        target = int(match.group(1))
                        if not _card_id(target) or target not in self._cards:
                            raise ValueError("Alias target is not a known card")
                        targets.append(target)
                    self._aliases[card_id] = ImageAlias(card_id, *targets, "R18")
                except ValueError as error:
                    self._unsupported[card_id] = str(error)
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise ProtocolError("image_alias_catalog_invalid", "Image alias resources do not satisfy the native table contract", 503) from error

    def resolve(self, card_id: int, rating: str = "R18") -> ImageAlias:
        if not _card_id(card_id) or card_id not in self._cards:
            raise ProtocolError("image_alias_card_unknown", "Image aliases require a known card identifier")
        if not isinstance(rating, str) or not 1 <= len(rating) <= 64:
            raise ProtocolError("image_alias_rating_invalid", "Image aliases require a bounded rating name")
        if card_id in self._unsupported:
            raise ProtocolError("image_alias_unsupported", "This card has an unsupported image alias rule", 503)
        alias = self._aliases.get(card_id)
        if alias is None:
            return ImageAlias(card_id, card_id, card_id, None)
        if rating != alias.condition_rating:
            return ImageAlias(card_id, card_id, card_id, alias.condition_rating)
        return alias

    def status(self) -> dict:
        return {"ready": True, "count": len(self._aliases), "cards": len(self._cards),
                "unsupported": {str(card_id): reason for card_id, reason in sorted(self._unsupported.items())},
                "resource_sha256": {RESOURCE_NAME: self._resource_sha256}, "decoded_sha256": self._decoded_sha256}

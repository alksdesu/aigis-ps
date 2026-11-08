from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType

from .al_format import MAX_RESOURCE_BYTES, ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError
from .native_numbers import int32
from .public_tables import BytesDigest
from .system_literals import decode_command, rating_command_id


RESOURCE_NAME = "SelectTicketConfig.atb"
RATING_RESOURCE = "OnCommand.atb"
FIELDS = {name: 1 for name in ("_ID", "_NameID", "_TexID", "_TexScl", "_CardID", "_Level", "EvolutionCount")}
LIMITS = ParseLimits(max_input_bytes=16 * 1024 * 1024, max_decompressed_bytes=16 * 1024 * 1024,
                    max_rows=100000)
MAX_CONFIGS = 4096
MAX_CANDIDATES = 4096
MAX_MASTER_BYTES = MAX_RESOURCE_BYTES


@dataclass(frozen=True)
class SelectCandidate:
    card_id: int
    level: int
    evolution_count: int
    source_index: int


@dataclass(frozen=True)
class SelectConfig:
    config_id: int
    name_id: int
    candidates: tuple[SelectCandidate, ...]


class SelectTicketCatalog:
    def __init__(self, cards, asset_reader):
        self._cards = cards
        self._card_digest = BytesDigest()
        try:
            if not callable(asset_reader):
                raise ValueError("Selection tickets require a resource reader")
            self._card_ids = self._identities()
            self._master_hash = self._master_digest()
            raw = asset_reader(RESOURCE_NAME)
            if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                raise ValueError("Selection resource must contain bounded immutable bytes")
            data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
            table = parse_table(data, LIMITS)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if len(fields) != len(table["fields"]) or fields != FIELDS:
                raise ValueError("Selection resource fields differ from the native contract")
            grouped, names, current, seen = {}, {}, None, set()
            for index, row in enumerate(table["rows"]):
                if any(not int32(row[name]) for name in FIELDS):
                    raise ValueError("Selection metadata must contain native integers")
                identifier, card_id = row["_ID"], row["_CardID"]
                if (identifier < 0 or card_id <= 1 or card_id not in self._card_ids
                        or row["_Level"] < 0 or row["EvolutionCount"] < 0):
                    raise ValueError("Selection candidate identity or growth is invalid")
                if identifier:
                    if identifier in grouped or len(grouped) >= MAX_CONFIGS:
                        raise ValueError("Selection headers are duplicated or exceed the supported count")
                    current, seen = identifier, set()
                    grouped[current], names[current] = [], row["_NameID"]
                if current is None or len(grouped[current]) >= MAX_CANDIDATES:
                    raise ValueError("Selection continuation has no header or exceeds the candidate limit")
                if card_id in seen:
                    raise ValueError("Selection configuration has ambiguous duplicate cards")
                seen.add(card_id)
                grouped[current].append(SelectCandidate(card_id, row["_Level"], row["EvolutionCount"], index))
            if not grouped:
                raise ValueError("Selection configurations are empty")
            self._configs = MappingProxyType({identifier: SelectConfig(identifier, names[identifier], tuple(rows))
                                             for identifier, rows in grouped.items()})
            rating_raw = asset_reader(RATING_RESOURCE)
            if type(rating_raw) is not bytes or not 0 < len(rating_raw) <= LIMITS.max_input_bytes:
                raise ValueError("Selection ratings require bounded immutable bytes")
            rating_data = decompress_all4(rating_raw, LIMITS) if rating_raw.startswith(b"ALL4") else rating_raw
            rating_table = parse_table(rating_data, LIMITS)
            rating_fields = {field["name"]: field["type_code"] for field in rating_table["fields"]}
            if (len(rating_fields) != len(rating_table["fields"])
                    or rating_fields != {"_ID": 1, "_Cmnd": 32}):
                raise ValueError("Selection rating fields differ from the native contract")
            commands = {}
            for row in rating_table["rows"]:
                key = row["_ID"]
                if not int32(key) or key in commands:
                    raise ValueError("Selection rating identities are invalid or duplicated")
                commands[key] = decode_command(row["_Cmnd"])
            self._restricted_cards = frozenset(candidate.card_id for config in self._configs.values()
                for candidate in config.candidates if commands.get(rating_command_id(candidate.card_id), "").strip())
            self._unsupported_configs = frozenset(identifier for identifier, config in self._configs.items()
                if any(candidate.card_id in self._restricted_cards for candidate in config.candidates))
            self._resource_hash = hashlib.sha256(raw).hexdigest()
            self._rating_hash = hashlib.sha256(rating_raw).hexdigest()
            self._content_version = hashlib.sha256(json.dumps({
                "policy": 1, "resource": self._resource_hash, "rating": self._rating_hash, "master": self._master_hash,
                "card_ids": sorted(self._card_ids), "source_index": "zero_based_resource_row",
                "duplicates": "reject", "initialization": "external_transaction", "nonempty_rating": "unavailable",
            }, sort_keys=True).encode()).hexdigest()
            self.ready()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("select_ticket_catalog_invalid", "Selection resources differ from the verified contract", 503) from error

    def _identities(self):
        values = tuple(self._cards.card_ids)
        if not values or any(not int32(identifier) or identifier <= 0 for identifier in values):
            raise ValueError("Selection tickets require valid master card identities")
        return frozenset(values)

    def _master_digest(self):
        raw = self._cards.snapshot.get_xml("CardData")
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_MASTER_BYTES:
            raise ValueError("Selection master must contain bounded immutable CardData")
        return self._card_digest(raw)

    def ready(self):
        try:
            if self._master_digest() != self._master_hash or self._identities() != self._card_ids:
                raise ValueError("Selection card definitions changed")
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("select_ticket_catalog_changed", "Selection card definitions changed after setup", 503) from error

    @property
    def configs(self):
        return self._configs

    @property
    def content_version(self):
        return self._content_version

    def get(self, config_id: int) -> SelectConfig:
        self.ready()
        if not int32(config_id) or config_id not in self._configs:
            raise ProtocolError("select_ticket_unknown", "Selection configuration is absent from the current resource", 501)
        return self._configs[config_id]

    def require_supported(self, config_id: int) -> SelectConfig:
        config = self.get(config_id)
        if config_id in self._unsupported_configs:
            raise ProtocolError("select_ticket_rating_unsupported", "Selection configuration requires unverified rating rules", 501)
        return config

    def status(self):
        try:
            self.ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "configs": len(self._configs),
                "candidates": sum(len(config.candidates) for config in self._configs.values()),
                "resource_sha256": self._resource_hash, "master_sha256": self._master_hash,
                "rating_sha256": self._rating_hash, "unsupported_config_ids": sorted(self._unsupported_configs),
                "resource_hashes": {RESOURCE_NAME: self._resource_hash, RATING_RESOURCE: self._rating_hash},
                "unverified_rating_cards": sorted(self._restricted_cards),
                "content_version": self.content_version, "source_index": "Zero-based resource row",
                "scope": "Selection candidates; initialization and request ordinals are validated by the transaction"}

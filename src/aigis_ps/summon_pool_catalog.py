from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from types import MappingProxyType

from .al_format import MAX_RESOURCE_BYTES, ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError, caught
from .native_numbers import int32
from .public_tables import BytesDigest


RESOURCE_NAME = "SummonConfig.atb"
LIMITS = ParseLimits(max_input_bytes=16 * 1024 * 1024, max_decompressed_bytes=16 * 1024 * 1024,
                    max_rows=100000)
FIELDS = {name: 1 for name in ("_ID", "_NameID", "_TexID", "_TexScl", "_CardID", "_Level", "EvolutionCount")}
FIELDS["PersonalRatio"] = 4
PERCENT_TOLERANCE = 0.001
MAX_POOLS = 4096
MAX_CANDIDATES = 4096
MAX_MASTER_BYTES = MAX_RESOURCE_BYTES


@dataclass(frozen=True)
class SummonCandidate:
    card_id: int
    level: int
    evolution_count: int
    percent: float
    weight: int


@dataclass(frozen=True)
class SummonPool:
    pool_id: int
    name_id: int
    candidates: tuple[SummonCandidate, ...]
    total_weight: int
    percent_sum: float
    complete: bool


def _pool(identifier, rows):
    ratios = [row["PersonalRatio"].as_integer_ratio() for row in rows]
    denominator = max(value[1] for value in ratios)
    weights = [numerator * (denominator // divisor) for numerator, divisor in ratios]
    divisor = math.gcd(*weights) or 1
    weights = [weight // divisor for weight in weights]
    total = math.fsum(row["PersonalRatio"] for row in rows)
    candidates = tuple(SummonCandidate(row["_CardID"], row["_Level"], row["EvolutionCount"],
                                      row["PersonalRatio"], weight) for row, weight in zip(rows, weights))
    return SummonPool(identifier, rows[0]["_NameID"], candidates, sum(weights), total,
                      abs(total - 100) <= PERCENT_TOLERANCE)


class SummonPoolCatalog:
    def __init__(self, cards, asset_reader):
        self._cards = cards
        self._card_digest = BytesDigest()
        try:
            if not callable(asset_reader):
                raise ValueError("Summon pools require a verified resource reader")
            self._card_ids = frozenset(cards.card_ids)
            if not self._card_ids or any(not int32(identifier) or identifier <= 0 for identifier in self._card_ids):
                raise ValueError("Summon pools require valid master card identities")
            self._master_hash = self._master_digest()
            raw = asset_reader(RESOURCE_NAME)
            if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                raise ValueError("Summon pool resource must be bounded immutable bytes")
            data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
            table = parse_table(data, LIMITS)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if len(fields) != len(table["fields"]) or fields != FIELDS:
                raise ValueError("Summon pool fields differ from the native contract")
            grouped, current = {}, None
            for row in table["rows"]:
                if any(not int32(row[name]) for name, kind in FIELDS.items() if kind == 1):
                    raise ValueError("Summon metadata must contain native integers")
                identifier, percent = row["_ID"], row["PersonalRatio"]
                if (identifier < 0 or row["_CardID"] not in self._card_ids or row["_CardID"] <= 1
                        or row["_Level"] < 0 or row["EvolutionCount"] < 0
                        or type(percent) is not float or not math.isfinite(percent) or not 0 <= percent <= 100):
                    raise ValueError("Summon candidate identity, growth or probability is invalid")
                if identifier:
                    if identifier in grouped or len(grouped) >= MAX_POOLS:
                        raise ValueError("Summon pool headers are duplicated or exceed the supported count")
                    current = identifier
                    grouped[current] = []
                if current is None or len(grouped[current]) >= MAX_CANDIDATES:
                    raise ValueError("Summon continuation has no header or exceeds the candidate limit")
                grouped[current].append(row)
            if not grouped:
                raise ValueError("Summon pool definitions are empty")
            self._pools = MappingProxyType({identifier: _pool(identifier, rows) for identifier, rows in grouped.items()})
            self._resource_hash = hashlib.sha256(raw).hexdigest()
            self._content_version = hashlib.sha256(json.dumps({
                "policy": 1, "resource": self._resource_hash, "master": self._master_hash,
                "percent_tolerance": PERCENT_TOLERANCE, "weights": "exact_binary_float_ratios",
            }, sort_keys=True).encode()).hexdigest()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("summon_pool_catalog_invalid", "Summon pool resources differ from the verified contract", 503) from error

    def _master_digest(self):
        raw = self._cards.snapshot.get_xml("CardData")
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_MASTER_BYTES:
            raise ValueError("Summon master must contain bounded immutable CardData")
        return self._card_digest(raw)

    def _ready(self):
        try:
            if self._master_digest() != self._master_hash or frozenset(self._cards.card_ids) != self._card_ids:
                raise ValueError("Summon card definitions changed")
        except (AttributeError, OSError, ValueError, KeyError, TypeError, ProtocolError) as error:
            raise ProtocolError("summon_pool_catalog_changed", "Summon card definitions changed after setup", 503) from error

    @property
    def content_version(self):
        return self._content_version

    @property
    def pools(self):
        return self._pools

    def get(self, pool_id: int) -> SummonPool:
        self._ready()
        if not int32(pool_id) or pool_id not in self._pools:
            raise ProtocolError("summon_pool_unknown", "Summon pool is absent from the current resource", 501)
        return self._pools[pool_id]

    def require_complete(self, pool_id: int) -> SummonPool:
        pool = self.get(pool_id)
        if not pool.complete or pool.total_weight <= 0:
            raise ProtocolError("summon_pool_incomplete", "Summon pool probabilities are incomplete", 501)
        return pool

    def draw(self, pool_id: int, randbelow) -> SummonCandidate:
        pool = self.require_complete(pool_id)
        if not callable(randbelow):
            raise ValueError("Summon sampling requires a callable random source")
        value = randbelow(pool.total_weight)
        if type(value) is not int or not 0 <= value < pool.total_weight:
            raise ProtocolError("summon_random_invalid", "Random source returned an invalid pool index", 503)
        for candidate in pool.candidates:
            if value < candidate.weight:
                return candidate
            value -= candidate.weight
        raise ProtocolError("summon_pool_invalid", "Summon weights do not cover the selected index", 503)

    def is_ready(self) -> bool:
        return caught(self._ready) is None

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "pools": len(self._pools),
                "complete_probability_pools": sum(pool.complete for pool in self._pools.values()),
                "resource_sha256": self._resource_hash, "master_sha256": self._master_hash,
                "content_version": self.content_version, "percent_tolerance": PERCENT_TOLERANCE,
                "incomplete_policy": "Unavailable; never normalize missing candidates",
                "scope": "Candidate probabilities only; schedules and guarantees are separate"}

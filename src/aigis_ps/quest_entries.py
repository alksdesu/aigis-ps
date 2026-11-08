from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import StrEnum
import re
from types import MappingProxyType
from typing import NamedTuple

from .native_numbers import INT32_MAX, UINT32_MASK, int32


# 9ACFE0 answers GetNum_QuestStar() with the record's raw MaxRank: a nonnegative int32, packed for score quests.
STAR_UNBOUNDED = INT32_MAX
# 67BF70 admits 6000-6999 enemies only while the quest record's MaxRank is below three.
STAGE_VARIANT_MAX_STARS = 2
# 5D5FF0 reads a planned enemy's attributes from Enemy row (EnemyID - 1) % 1000.
ENEMY_ROW_SPAN = 1000
# 5D5FF0 leaves enemies carrying the DCAF74 attributes "置物,分身" out of the planned count.
UNCOUNTED_ATTRIBUTES = frozenset(("置物", "分身"))
# Any other IsAppear script would need the client's expression engine, so it is refused rather than guessed.
_APPEARANCE = re.compile(r"\s*GetNum_QuestStar\(\s*\)\s*(>=|<=|>|<|==)\s*([0-9]{1,10})\s*;?\s*")
_ATTRIBUTE_SEPARATORS = re.compile(r"[,、\s]+")


class UnsupportedAppearance(ValueError):
    pass


class EnemyBand(StrEnum):
    ORDINARY = "ordinary"
    STAGE_VARIANT = "stage_variant"
    SPECIAL = "special"
    DATED = "dated"
    SKIPPED = "skipped"


# 67BF70 compares EnemyID unsigned and admits only rows inside these spans.
_BAND_SPANS = (
    (range(0, 1000), EnemyBand.ORDINARY),
    (range(6000, 7000), EnemyBand.STAGE_VARIANT),
    (range(7000, 8000), EnemyBand.SPECIAL),
    (range(8000, 9000), EnemyBand.DATED),
)


class StarInterval(NamedTuple):
    minimum: int
    maximum: int

    @property
    def empty(self) -> bool:
        return self.minimum > self.maximum

    def covers(self, stars: int) -> bool:
        return self.minimum <= stars <= self.maximum

    def intersection(self, other: StarInterval) -> StarInterval:
        return _closed(max(self.minimum, other.minimum), min(self.maximum, other.maximum))


UNCONDITIONAL = StarInterval(0, STAR_UNBOUNDED)
# A single empty form keeps interval equality the same as set equality.
_EMPTY = StarInterval(1, 0)
_BAND_INTERVALS = MappingProxyType({
    EnemyBand.ORDINARY: UNCONDITIONAL,
    EnemyBand.STAGE_VARIANT: StarInterval(0, STAGE_VARIANT_MAX_STARS),
    EnemyBand.SPECIAL: UNCONDITIONAL,
})
_COMPARISONS: Mapping[str, Callable[[int], tuple[int, int]]] = MappingProxyType({
    ">=": lambda bound: (bound, STAR_UNBOUNDED),
    ">": lambda bound: (bound + 1, STAR_UNBOUNDED),
    "<=": lambda bound: (0, bound),
    "<": lambda bound: (0, bound - 1),
    "==": lambda bound: (bound, bound),
})


def _closed(minimum: int, maximum: int) -> StarInterval:
    return StarInterval(minimum, maximum) if minimum <= maximum else _EMPTY


def classify_enemy(enemy_id: int) -> EnemyBand:
    if not int32(enemy_id):
        raise ValueError("EnemyID must be a native int32")
    unsigned = enemy_id & UINT32_MASK
    return next((band for span, band in _BAND_SPANS if unsigned in span), EnemyBand.SKIPPED)


def appear_interval(text: str | None) -> StarInterval:
    if text is None or text == "":
        return UNCONDITIONAL
    if not isinstance(text, str):
        raise ValueError("IsAppear must be native text")
    match = _APPEARANCE.fullmatch(text)
    if match is None or int(match[2]) > STAR_UNBOUNDED:
        raise UnsupportedAppearance("IsAppear supports only GetNum_QuestStar() compared with an int32 literal")
    return _closed(*_COMPARISONS[match[1]](int(match[2])))


def row_interval(row: Mapping) -> StarInterval | None:
    if not isinstance(row, Mapping):
        raise ValueError("Entry rows must be native table rows")
    band = classify_enemy(row.get("EnemyID"))
    if band is EnemyBand.DATED:
        raise UnsupportedAppearance("Enemies 8000-8999 appear on client-side dates the server cannot replay")
    if band not in _BAND_INTERVALS:
        return None
    interval = _BAND_INTERVALS[band].intersection(appear_interval(row.get("IsAppear")))
    return None if interval.empty else interval


def expected_enemies(entry_rows: Iterable[Mapping], enemy_rows: Sequence[Mapping], max_rank: int) -> int:
    if not int32(max_rank) or max_rank < 0:
        raise ValueError("MaxRank must be a nonnegative native int32")
    if not isinstance(enemy_rows, Sequence) or isinstance(enemy_rows, (str, bytes)):
        raise ValueError("Enemy rows must form a native table")
    total = 0
    for row in entry_rows:
        interval = row_interval(row)
        if interval is None or not interval.covers(max_rank):
            continue
        loop = row.get("Loop")
        if not int32(loop) or loop < 0:
            raise ValueError("Entry Loop must be a nonnegative native int32")
        if _counted(row["EnemyID"], enemy_rows):
            total += loop
    if not int32(total):
        raise ValueError("Planned enemy count exceeds a native int32")
    return total


def _counted(enemy_id: int, enemy_rows: Sequence[Mapping]) -> bool:
    # EnemyID 0 and indices past the Enemy table name no enemy row, so nothing can exclude them.
    if enemy_id < 1:
        return True
    index = (enemy_id - 1) % ENEMY_ROW_SPAN
    if index >= len(enemy_rows):
        return True
    return UNCOUNTED_ATTRIBUTES.isdisjoint(_attributes(enemy_rows[index]))


def _attributes(enemy: Mapping) -> frozenset[str]:
    if not isinstance(enemy, Mapping):
        raise ValueError("Enemy rows must be native table rows")
    text = enemy.get("_Attribute")
    if text is None:
        return frozenset()
    if not isinstance(text, str):
        raise ValueError("Enemy _Attribute must be native text")
    return frozenset(filter(None, _ATTRIBUTE_SEPARATORS.split(text)))

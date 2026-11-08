from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence

from .native_numbers import float32, int32


def _quantity(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Growth quantities must be bounded native integers")
    return value


def experience_threshold(base_exp: int, require_exp_ratio: int) -> int:
    _quantity(base_exp)
    _quantity(require_exp_ratio, 1)
    ratio = float32(float32(require_exp_ratio) / float32(100))
    return _quantity(int(float32(float32(float32(base_exp) * ratio) + float32(0.5))))


def derived_level(exp: int, thresholds: Sequence[int], cap: int) -> int:
    _quantity(exp)
    _quantity(cap, 1)
    if (not isinstance(thresholds, Sequence) or not cap <= len(thresholds) <= 65536
            or any(not int32(value) or value < 0 for value in thresholds)
            or thresholds[0] != 0 or any(left >= right for left, right in zip(thresholds, thresholds[1:]))):
        raise ValueError("Expected strictly increasing cumulative experience thresholds")
    return min(cap, bisect_right(thresholds, exp))


def advance_threshold(level: int, leftover: int, max_level: int, thresholds: Mapping[int, int],
                      gained_exp: int = 0, direct_levels: int = 0) -> tuple[int, int]:
    for value in (level, leftover, max_level, gained_exp, direct_levels):
        _quantity(value)
    if (level > max_level or not isinstance(thresholds, Mapping) or len(thresholds) > 65536
            or any(not int32(key) or key < 0 or not int32(value) or value < -1 for key, value in thresholds.items())):
        raise ValueError("Invalid growth threshold configuration or level")
    total = _quantity(leftover + gained_exp)
    if thresholds.get(level + 1, 0) <= 0:
        return level, 0
    current = level
    while total > 0:
        needed = thresholds.get(current + 1, 0)
        if needed <= 0:
            total = 0
            break
        if total < needed:
            break
        total -= needed
        current += 1
    return min(max_level, _quantity(current + direct_levels)), 0 if direct_levels else total


def integration_price(level: int, count: int) -> int:
    _quantity(level)
    _quantity(count)
    return _quantity(count * (160 + 40 * level if level > 0 else 200))

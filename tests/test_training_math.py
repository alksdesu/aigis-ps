import pytest

from aigis_ps.training_math import advance_threshold, derived_level, experience_threshold, integration_price


def test_native_experience_rounding_and_display_level_do_not_discard_overflow():
    assert experience_threshold(15, 110) == 17
    assert experience_threshold(17, 130) == 22
    assert derived_level(9999, (0, 10, 50, 100, 200), 3) == 3
    assert derived_level(50, (0, 10, 50, 100, 200), 5) == 3


@pytest.mark.parametrize("level,left,exp,direct,expected", [
    (1, 1, 3, 0, (2, 2)), (1, 1, 3, 1, (3, 0)),
    (1, 0, 2, 0, (2, 0)), (1, 0, 6, 0, (3, 0)),
    (1, 0, 100, 0, (3, 0)), (3, 5, 0, 1, (3, 0)),
    (1, 1, 0, 0, (1, 1)), (1, 0, 0, 1, (2, 0)),
])
def test_thresholds_use_leftover_experience_before_direct_levels(level, left, exp, direct, expected):
    assert advance_threshold(level, left, 3, {1: 0, 2: 2, 3: 4, 4: -1}, exp, direct) == expected


def test_missing_next_threshold_stops_growth_including_direct_grants():
    assert advance_threshold(1, 2, 3, {1: 0}, 100, 2) == (1, 0)


def test_native_threshold_consumption_precedes_final_cap():
    assert advance_threshold(0, 0, 1, {1: 2, 2: 4, 3: 6}, 7, 0) == (1, 1)


@pytest.mark.parametrize("level,count,price", [(1, 3, 600), (50, 3, 6480), (0, 2, 400), (100, 0, 0)])
def test_price_uses_pretransaction_level_and_material_count(level, count, price):
    assert integration_price(level, count) == price


@pytest.mark.parametrize("operation", [
    lambda: experience_threshold(True, 100), lambda: experience_threshold(1, -1),
    lambda: experience_threshold(0x7fffffff, 200), lambda: derived_level(1, (0, 10, 10), 3),
    lambda: derived_level(1, (0, 10), 3), lambda: advance_threshold(2, 0, 1, {}),
    lambda: advance_threshold(0, 0x7fffffff, 1, {1: 2}, 1),
    lambda: advance_threshold(0, 0, 1, {1: True}),
    lambda: integration_price(1, True), lambda: integration_price(0x7fffffff, 1),
])
def test_invalid_values_and_integer_overflow_are_rejected(operation):
    with pytest.raises(ValueError):
        operation()

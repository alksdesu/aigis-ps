from copy import deepcopy

import pytest

import aigis_ps.event_pass_state as module
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_state import initialize_event_passes, mark_event_pass, read_event_passes


PASS = "event_pass_20261015"
OTHER = "event_pass_20260611"
IDENTITY = "a" * 64
REWARD = "b" * 64


def state(rows=None):
    return {"bootstrap_schema": 1, "tables": {"event_pass_status": deepcopy(rows or []),
        "possessions": [{"ItemID": PASS, "Count": 20}], "presents": []},
        "sentinel": {"nested": [1]}}


def row(identifier=PASS, normal=0, premium=0):
    return {"ID": identifier, "ReceivedNormalPoint": normal, "ReceivedPremiumPoint": premium}


def delivery(rewards=(1,), mail=(100,), definition=REWARD):
    return {"definition": definition, "reward_ids": list(rewards), "mail_ids": list(mail)}


def grant(original, normal=None, premium=None, *, identifier=PASS, definition=IDENTITY):
    return mark_event_pass(original, identifier, definition=definition,
                           normal_deliveries=normal, premium_deliveries=premium)


def fail_unchanged(original, callback, code=None):
    before = deepcopy(original)
    with pytest.raises(ProtocolError) as caught:
        callback()
    if code:
        assert caught.value.code == code
    assert original == before
    return caught.value


def test_empty_initialization_creates_only_journal_and_is_idempotent():
    original = state()
    saved = deepcopy(original)
    initialized = initialize_event_passes(original)
    assert initialized["event_pass_state"] == {"schema_version": 1, "passes": {}}
    assert read_event_passes(initialized) == []
    assert original == saved
    assert initialize_event_passes(initialized) == initialized
    initialized["sentinel"]["nested"].append(2)
    assert original == saved


def test_legacy_native_watermarks_become_baselines_without_grants():
    original = state([row(normal=20, premium=10), row(OTHER, 99, 88)])
    saved = deepcopy(original)
    initialized = initialize_event_passes(original)
    assert read_event_passes(initialized) == original["tables"]["event_pass_status"]
    entry = initialized["event_pass_state"]["passes"][PASS]
    assert entry == {"definition": None, "baseline_normal": 20, "baseline_premium": 10,
                     "deliveries": {"normal": {}, "premium": {}}}
    assert original == saved
    assert initialized["tables"]["presents"] == []
    assert initialized["tables"]["possessions"] == original["tables"]["possessions"]


def test_unknown_legacy_identity_and_high_watermark_are_preserved():
    original = state([row("retired_未知<&>", 2147483647, 2147483647)])
    initialized = initialize_event_passes(original)
    assert read_event_passes(initialized) == read_event_passes(original)
    assert initialize_event_passes(initialized) == initialized


def test_read_returns_transitive_copy():
    original = state([row(normal=10)])
    result = read_event_passes(original)
    result[0]["ReceivedNormalPoint"] = 123
    assert original["tables"]["event_pass_status"][0]["ReceivedNormalPoint"] == 10


def test_stored_256_byte_identity_stays_loadable_and_can_still_advance():
    legacy = "x" * (module.MAX_ITEM_BYTES + 1)
    original = state([row(legacy, 10)])
    initialized = initialize_event_passes(original)
    assert read_event_passes(initialized) == [row(legacy, 10)]
    updated = grant(initialized, {20: delivery()}, identifier=legacy)
    assert read_event_passes(updated) == [row(legacy, 20)]


def test_new_identity_must_fit_the_native_cell():
    original = initialize_event_passes(state())
    fitting = "x" * module.MAX_ITEM_BYTES
    assert read_event_passes(grant(original, {10: delivery()}, identifier=fitting)) == [row(fitting, 10)]
    error = fail_unchanged(original, lambda: grant(original, {10: delivery()}, identifier=fitting + "x"),
                           module.IDENTITY_UNREPLAYABLE)
    assert error.status == 503


@pytest.mark.parametrize("rows", [
    [row(), row()], [row(normal=-1)], [row(premium=True)], [row(normal=2147483648)],
    [row(identifier="")], [row(identifier="x" * 257)], [row(identifier="坏" * 86)],
    [row(identifier="bad\x00")], [row(identifier="\ud800")], [row(identifier=1)],
    [dict(row(), Extra=1)], [{"ID": PASS, "ReceivedNormalPoint": 0}], [None],
])
def test_invalid_native_rows_are_not_dropped_or_repaired(rows):
    original = state(rows)
    for operation in (read_event_passes, initialize_event_passes):
        fail_unchanged(original, lambda: operation(original), "event_pass_state_invalid")


@pytest.mark.parametrize("kind", ["not_dict", "bootstrap_bool", "missing_tables", "missing_rows", "rows_none"])
def test_invalid_outer_schema_is_rejected(kind):
    original = state()
    if kind == "not_dict":
        original = []
    elif kind == "bootstrap_bool":
        original["bootstrap_schema"] = True
    elif kind == "missing_tables":
        original.pop("tables")
    elif kind == "missing_rows":
        original["tables"].pop("event_pass_status")
    else:
        original["tables"]["event_pass_status"] = None
    fail_unchanged(original, lambda: initialize_event_passes(original), "event_pass_state_invalid")


def test_two_tracks_and_multiple_thresholds_move_only_to_delivered_maximum():
    original = state()
    saved = deepcopy(original)
    normal = {20: delivery((2, 3), (102, 103)), 10: delivery((1,), (101,))}
    premium = {10: delivery((4,), (104,))}
    updated = grant(original, normal, premium)
    assert read_event_passes(updated) == [row(normal=20, premium=10)]
    entry = updated["event_pass_state"]["passes"][PASS]
    assert entry["definition"] == IDENTITY
    assert entry["baseline_normal"] == entry["baseline_premium"] == 0
    assert entry["deliveries"]["normal"] == {"10": normal[10], "20": normal[20]}
    assert entry["deliveries"]["premium"] == {"10": premium[10]}
    assert original == saved
    assert updated["tables"]["possessions"] == saved["tables"]["possessions"]
    assert updated["tables"]["presents"] == []
    normal[10]["mail_ids"].append(999)
    assert entry["deliveries"]["normal"]["10"]["mail_ids"] == [101]


def test_existing_native_rows_keep_order_and_other_fields_are_untouched():
    original = state([row(OTHER, 30, 20), row(normal=10)])
    updated = grant(original, {20: delivery()})
    assert read_event_passes(updated) == [row(OTHER, 30, 20), row(normal=20)]
    assert updated["sentinel"] == original["sentinel"]
    assert updated["tables"]["possessions"] == original["tables"]["possessions"]


def test_identical_delivery_is_idempotent_and_new_higher_delivery_is_allowed():
    first = grant(state(), {10: delivery()})
    assert grant(first, {10: delivery()}) == first
    updated = grant(first, {10: delivery(), 20: delivery((2,), (200,))})
    assert read_event_passes(updated) == [row(normal=20)]
    assert len(updated["event_pass_state"]["passes"][PASS]["deliveries"]["normal"]) == 2


@pytest.mark.parametrize("threshold", [1, 10, 15, 20])
def test_legacy_baseline_cannot_be_backfilled(threshold):
    original = state([row(normal=20)])
    fail_unchanged(original, lambda: grant(original, {threshold: delivery()}), "event_pass_already_received")


def test_new_reward_inserted_below_received_watermark_is_not_backfilled():
    original = grant(state(), {10: delivery(), 30: delivery((3,), (300,))})
    fail_unchanged(original, lambda: grant(original, {20: delivery((2,), (200,))}), "event_pass_already_received")


@pytest.mark.parametrize("change", ["definition", "rewards", "mail", "mail_order"])
def test_existing_threshold_cannot_be_rewritten(change):
    first = delivery((1, 2), (100, 101))
    original = grant(state(), {10: first})
    changed = deepcopy(first)
    if change == "definition":
        changed["definition"] = "c" * 64
    elif change == "rewards":
        changed["reward_ids"] = [1, 3]
    elif change == "mail":
        changed["mail_ids"] = [100, 102]
    else:
        changed["mail_ids"].reverse()
    fail_unchanged(original, lambda: grant(original, {10: changed}), "event_pass_delivery_changed")


def test_pass_identity_drift_does_not_replace_old_definition():
    original = grant(state(), {10: delivery()})
    fail_unchanged(original, lambda: grant(original, {20: delivery((2,), (200,))}, definition="c" * 64),
                   "event_pass_definition_changed")


@pytest.mark.parametrize("target", ["later", "premium", "same_request"])
def test_reward_id_cannot_be_moved_to_a_higher_threshold_or_other_track(target):
    original = state() if target == "same_request" else grant(state(), {10: delivery()})
    if target == "later":
        action = lambda: grant(original, {20: delivery((1,), (200,))})
    elif target == "premium":
        action = lambda: grant(original, premium={20: delivery((1,), (200,))})
    else:
        action = lambda: grant(original, {10: delivery()}, {10: delivery((1,), (200,))})
    fail_unchanged(original, action, "event_pass_delivery_duplicate")


def test_mail_id_is_unique_even_across_different_passes():
    original = grant(state(), {10: delivery()})
    fail_unchanged(original, lambda: grant(original, {10: delivery((2,), (100,))}, identifier=OTHER),
                   "event_pass_delivery_duplicate")
    updated = grant(original, {10: delivery((1,), (200,))}, identifier=OTHER)
    assert len(read_event_passes(updated)) == 2


@pytest.mark.parametrize("bad", [
    {0: delivery()}, {-1: delivery()}, {True: delivery()}, {"10": delivery()}, {2147483648: delivery()},
    {10: {}}, {10: dict(delivery(), extra=1)}, {10: delivery((), (100,))}, {10: delivery((1,), ())},
    {10: delivery((1, 1), (100,))}, {10: delivery((1,), (100, 100))},
    {10: delivery((True,), (100,))}, {10: delivery((1,), (-1,))},
    {10: delivery((1,), (100,), "x")}, {10: dict(delivery(), reward_ids=(1,))}, [],
])
def test_bad_delivery_input_is_rejected_without_modifying_state(bad):
    original = state()
    fail_unchanged(original, lambda: grant(original, bad), "event_pass_selection_invalid")


@pytest.mark.parametrize("definition", [None, "", "A" * 64, "a" * 63, 1])
def test_pass_fingerprint_is_strict(definition):
    original = state()
    fail_unchanged(original, lambda: grant(original, {10: delivery()}, definition=definition),
                   "event_pass_definition_invalid")


@pytest.mark.parametrize("corrupt", [
    "schema", "extra_journal", "missing_pass", "extra_pass", "extra_entry", "definition", "baseline",
    "point_zero", "point_leading_zero", "point_overflow", "below_baseline", "no_identity", "native_watermark",
    "missing_track", "duplicate_reward", "duplicate_mail", "bad_delivery_hash", "empty_mail",
])
def test_corrupt_existing_journal_is_never_silently_rebuilt(corrupt):
    original = grant(state(), {10: delivery()}, {10: delivery((2,), (200,))})
    journal = original["event_pass_state"]
    entry = journal["passes"][PASS]
    normal = entry["deliveries"]["normal"]
    if corrupt == "schema":
        journal["schema_version"] = True
    elif corrupt == "extra_journal":
        journal["future"] = 1
    elif corrupt == "missing_pass":
        journal["passes"].pop(PASS)
    elif corrupt == "extra_pass":
        journal["passes"][OTHER] = deepcopy(entry)
    elif corrupt == "extra_entry":
        entry["future"] = 1
    elif corrupt == "definition":
        entry["definition"] = "bad"
    elif corrupt == "baseline":
        entry["baseline_normal"] = -1
    elif corrupt.startswith("point_"):
        key = {"point_zero": "0", "point_leading_zero": "010", "point_overflow": "2147483648"}[corrupt]
        normal[key] = normal.pop("10")
    elif corrupt == "below_baseline":
        entry["baseline_normal"] = 10
    elif corrupt == "no_identity":
        entry["definition"] = None
    elif corrupt == "native_watermark":
        original["tables"]["event_pass_status"][0]["ReceivedNormalPoint"] = 20
    elif corrupt == "missing_track":
        entry["deliveries"].pop("premium")
    elif corrupt == "duplicate_reward":
        entry["deliveries"]["premium"]["10"]["reward_ids"] = [1]
    elif corrupt == "duplicate_mail":
        entry["deliveries"]["premium"]["10"]["mail_ids"] = [100]
    elif corrupt == "bad_delivery_hash":
        normal["10"]["definition"] = "bad"
    else:
        normal["10"]["mail_ids"] = []
    for action in (lambda: read_event_passes(original), lambda: initialize_event_passes(original),
                   lambda: grant(original, {20: delivery((3,), (300,))})):
        fail_unchanged(original, action, "event_pass_state_invalid")


def test_empty_mark_is_an_isolated_noop_without_implicit_schema():
    original = state([row(normal=10)])
    updated = grant(original, {}, {})
    assert updated == original and "event_pass_state" not in updated
    updated["sentinel"]["nested"].append(2)
    assert original["sentinel"]["nested"] == [1]


@pytest.mark.parametrize("limit", ["MAX_PASSES", "MAX_DELIVERIES", "MAX_MAIL_IDS", "MAX_REWARD_IDS"])
def test_total_storage_limits_fail_atomically(limit, monkeypatch):
    original = grant(state(), {10: delivery()})
    monkeypatch.setattr(module, limit, 1)
    if limit == "MAX_PASSES":
        action = lambda: grant(original, {10: delivery((2,), (200,))}, identifier=OTHER)
    else:
        action = lambda: grant(original, {20: delivery((2,), (200,))})
    error = fail_unchanged(original, action)
    assert error.status == 503


def test_failed_premium_half_cannot_leave_normal_half_recorded():
    original = state()
    fail_unchanged(original, lambda: grant(original, {10: delivery()}, {20: delivery((2,), (100,))}),
                   "event_pass_delivery_duplicate")
    assert original["tables"]["event_pass_status"] == []


def test_existing_initialized_state_is_validated_without_rebaselining():
    original = grant(state([row(normal=5)]), {10: delivery()})
    initialized = initialize_event_passes(original)
    assert initialized == original
    assert initialized["event_pass_state"]["passes"][PASS]["baseline_normal"] == 5
    assert initialized["event_pass_state"]["passes"][PASS]["deliveries"]["normal"]["10"] == delivery()

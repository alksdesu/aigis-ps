from copy import deepcopy
from dataclasses import dataclass, replace

import pytest

from aigis_ps.errors import ProtocolError
import aigis_ps.rank_reward_state as module
from aigis_ps.rank_reward_state import (
    bonus_fingerprint, initialize_rank_rewards, mark_rank_reward,
    observe_rank_rewards, read_rank_rewards,
)


@dataclass(frozen=True)
class Bonus:
    bonus_id: int = 1
    rank: int = 5
    incentive_id: int = 1
    reward_main_type: int = 5
    reward_sub_type: int = 0
    parameter: int = 3
    item_id: str = ""
    source_index: int = 100
    config_index: int = 1
    display_text_id: int = 900
    tutorial_flag_id: int = 3


class Catalog:
    def __init__(self, *bonuses):
        self.bonuses = bonuses or (Bonus(), Bonus(2, 10), Bonus(3, 15), Bonus(4, 20))
        self.calls = []

    def between(self, old, new):
        self.calls.append((old, new))
        return tuple(bonus for bonus in self.bonuses if old < bonus.rank <= new)


def state(rank=1):
    return {"bootstrap_schema": 1, "tables": {"game_param": [{"Rank": rank, "Experience": 0}],
        "presents": [{"old": [1]}], "universal_flags": [{"ID": "old", "Value": 1}]},
        "other": {"preserve": [1, 2]}, "unknown_outer_schema": 8}


def pending(rank=10):
    return observe_rank_rewards(initialize_rank_rewards(state(rank), new_account=True), Catalog())


def fail(source, callback, code=None):
    before = deepcopy(source)
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert source == before
    if code:
        assert caught.value.code == code
    return caught.value


def test_new_initialization_only_creates_zero_baseline_without_rewards():
    original = state(20)
    initialized = initialize_rank_rewards(original, new_account=True)
    assert read_rank_rewards(initialized) == {"schema_version": 1, "baseline_rank": 0,
        "observed_rank": 0, "entries": {}}
    assert {key: value for key, value in initialized.items() if key != "rank_rewards"} == original
    assert "rank_rewards" not in original


def test_existing_account_has_current_rank_baseline_and_no_backfill():
    catalog = Catalog()
    initialized = initialize_rank_rewards(state(10))
    assert read_rank_rewards(initialized)["baseline_rank"] == 10
    assert observe_rank_rewards(initialized, catalog) == initialized
    assert catalog.calls == []
    initialized["tables"]["game_param"][0]["Rank"] = 20
    updated = observe_rank_rewards(initialized, catalog)
    assert set(read_rank_rewards(updated)["entries"]) == {"3", "4"}
    assert catalog.calls == [(10, 20)]


def test_observation_records_every_crossed_rank_in_one_candidate():
    original = initialize_rank_rewards(state(20), new_account=True)
    catalog = Catalog()
    updated = observe_rank_rewards(original, catalog)
    assert catalog.calls == [(0, 20)]
    ledger = read_rank_rewards(updated)
    assert ledger["observed_rank"] == 20 and len(ledger["entries"]) == 4
    for bonus in catalog.bonuses:
        assert ledger["entries"][str(bonus.bonus_id)] == {"rank": bonus.rank,
            "definition": bonus_fingerprint(bonus), "status": "pending", "mail_ids": [], "reason": None}
    assert original["rank_rewards"]["entries"] == {}


def test_same_rank_observation_is_isolated_noop():
    source = pending()
    updated = observe_rank_rewards(source, Catalog())
    assert updated == source and updated is not source
    updated["other"]["preserve"].append(3)
    assert source["other"]["preserve"] == [1, 2]


def test_pending_failure_reason_can_be_retried_then_marked_delivered():
    source = pending()
    failed = mark_rank_reward(source, 1, reason="summon_pool_incomplete")
    assert failed["rank_rewards"]["entries"]["1"]["status"] == "pending"
    assert failed["rank_rewards"]["entries"]["1"]["mail_ids"] == []
    retried = mark_rank_reward(failed, 1, reason="present_capacity")
    delivered = mark_rank_reward(retried, 1, mail_ids=[31, 32])
    assert delivered["rank_rewards"]["entries"]["1"] == {
        **source["rank_rewards"]["entries"]["1"], "status": "delivered", "reason": None, "mail_ids": [31, 32]}
    assert source["tables"] == delivered["tables"]


def test_delivered_mark_is_idempotent_only_for_exact_original_mail_order():
    source = mark_rank_reward(pending(), 1, mail_ids=[3, 4])
    assert mark_rank_reward(source, 1, mail_ids=(3, 4)) == source
    assert mark_rank_reward(source, 1) == source
    for kwargs in ({"mail_ids": [4, 3]}, {"mail_ids": [5]}, {"reason": "retry"}):
        fail(source, lambda: mark_rank_reward(source, 1, **kwargs), "rank_reward_already_delivered")


def test_distinct_rewards_cannot_claim_same_mail_identity():
    source = mark_rank_reward(pending(), 1, mail_ids=[8])
    fail(source, lambda: mark_rank_reward(source, 2, mail_ids=[8]), "rank_reward_mail_conflict")


def test_pending_mark_without_change_is_noop():
    source = pending()
    assert mark_rank_reward(source, 1) == source


def test_existing_initialization_never_replaces_old_baseline_or_entries():
    source = mark_rank_reward(pending(), 1, mail_ids=[7])
    assert initialize_rank_rewards(source) == source
    assert initialize_rank_rewards(source, new_account=True) == source


def test_rank_regression_is_rejected_without_clearing_earned_history():
    source = pending(20)
    source["tables"]["game_param"][0]["Rank"] = 10
    assert read_rank_rewards(source)["observed_rank"] == 20
    assert initialize_rank_rewards(source) == source
    fail(source, lambda: observe_rank_rewards(source, Catalog()), "rank_reward_rank_backwards")


def test_catalog_change_does_not_overwrite_existing_identity_or_fingerprint():
    source = mark_rank_reward(pending(5), 1, reason="old_unsupported")
    source["tables"]["game_param"][0]["Rank"] = 10
    updated = observe_rank_rewards(source, Catalog(Bonus(1, 10, parameter=99), Bonus(2, 10)))
    assert updated["rank_rewards"]["entries"]["1"] == source["rank_rewards"]["entries"]["1"]
    assert updated["rank_rewards"]["entries"]["2"]["status"] == "pending"


def test_removed_catalog_reward_and_prior_delivery_are_preserved():
    source = mark_rank_reward(pending(5), 1, mail_ids=[2])
    source["tables"]["game_param"][0]["Rank"] = 10
    updated = observe_rank_rewards(source, Catalog(Bonus(2, 10)))
    assert updated["rank_rewards"]["entries"]["1"] == source["rank_rewards"]["entries"]["1"]


def test_new_low_rank_definition_does_not_bypass_saved_observation_baseline():
    source = pending(5)
    source["tables"]["game_param"][0]["Rank"] = 10
    updated = observe_rank_rewards(source, Catalog(Bonus(9, 4), Bonus(2, 10)))
    assert "9" not in updated["rank_rewards"]["entries"]


@pytest.mark.parametrize("changes", [{"source_index": 999}, {"config_index": 500},
    {"display_text_id": 1000}, {"tutorial_flag_id": 25}])
def test_display_order_and_text_changes_do_not_change_semantic_fingerprint(changes):
    assert bonus_fingerprint(replace(Bonus(), **changes)) == bonus_fingerprint(Bonus())


@pytest.mark.parametrize("changes", [{"bonus_id": 2}, {"rank": 10}, {"incentive_id": 2},
    {"reward_main_type": 0}, {"reward_sub_type": 1}, {"parameter": 5}, {"item_id": "item_new"}])
def test_actual_reward_semantics_change_fingerprint(changes):
    assert bonus_fingerprint(replace(Bonus(), **changes)) != bonus_fingerprint(Bonus())


def test_catalog_display_order_does_not_affect_saved_semantic_records():
    source = initialize_rank_rewards(state(10), new_account=True)
    left = observe_rank_rewards(source, Catalog(Bonus(), Bonus(2, 10)))
    right = observe_rank_rewards(source, Catalog(Bonus(2, 10, source_index=999), Bonus(source_index=500)))
    assert left == right


@pytest.mark.parametrize("value", [True, -1, 0, 1.0, None, 2**31])
def test_invalid_actual_rank_is_never_used_as_baseline(value):
    source = state(value)
    fail(source, lambda: initialize_rank_rewards(source), "rank_rewards_invalid")


def test_actual_rank_maximum_and_empty_new_observation_are_valid():
    source = initialize_rank_rewards(state(2**31-1), new_account=True)
    catalog = Catalog(Bonus(10, 2**31-1))
    updated = observe_rank_rewards(source, catalog)
    assert read_rank_rewards(updated)["observed_rank"] == 2**31-1


@pytest.mark.parametrize("new", [0, 1, None, "new"])
def test_new_account_flag_is_strict(new):
    source = state()
    fail(source, lambda: initialize_rank_rewards(source, new_account=new), "rank_reward_origin_invalid")


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("schema_version", True),
    ("baseline_rank", -1), ("baseline_rank", 11), ("observed_rank", True),
    ("observed_rank", 2**31), ("entries", []), ("extra", 1)])
def test_invalid_ledger_is_not_overwritten(field, value):
    source = pending()
    source["rank_rewards"][field] = value
    fail(source, lambda: initialize_rank_rewards(source), "rank_rewards_invalid")


@pytest.mark.parametrize("field,value", [("rank", 0), ("rank", 11), ("rank", True),
    ("definition", "bad"), ("definition", "A"*64), ("status", "claimed"),
    ("status", "delivered"), ("mail_ids", [1]), ("reason", ""), ("reason", "bad\nreason"), ("extra", 0)])
def test_invalid_entry_is_rejected(field, value):
    source = pending()
    source["rank_rewards"]["entries"]["1"][field] = value
    fail(source, lambda: read_rank_rewards(source), "rank_rewards_invalid")


@pytest.mark.parametrize("key", [1, "01", "0", "-1", "2147483648", " 1"])
def test_entry_keys_are_canonical_positive_int32_strings(key):
    source = pending()
    source["rank_rewards"]["entries"][key] = source["rank_rewards"]["entries"].pop("1")
    fail(source, lambda: read_rank_rewards(source), "rank_rewards_invalid")


@pytest.mark.parametrize("mail_ids", [[], [0], [-1], [True], [1, 1], [2**31], "1", [1.0]])
def test_invalid_delivery_identity_is_not_recorded(mail_ids):
    source = pending()
    fail(source, lambda: mark_rank_reward(source, 1, mail_ids=mail_ids), "rank_reward_mark_invalid")


@pytest.mark.parametrize("reason", ["", "x"*257, "密"*86, "bad\x00value", "\ud800", True])
def test_reason_is_bounded_valid_text(reason):
    source = pending()
    fail(source, lambda: mark_rank_reward(source, 1, reason=reason), "rank_reward_mark_invalid")


def test_reason_byte_boundary_and_delivery_reset():
    source = mark_rank_reward(pending(), 1, reason="密"*85 + "x")
    assert len(source["rank_rewards"]["entries"]["1"]["reason"].encode()) == 256
    assert mark_rank_reward(source, 1, mail_ids=[9])["rank_rewards"]["entries"]["1"]["reason"] is None


def test_reason_and_delivery_cannot_be_supplied_together():
    source = pending()
    fail(source, lambda: mark_rank_reward(source, 1, mail_ids=[1], reason="failed"), "rank_reward_mark_invalid")


def test_unknown_reward_cannot_be_added_by_mark():
    source = pending()
    fail(source, lambda: mark_rank_reward(source, 99, mail_ids=[1]), "rank_reward_unknown")


def test_duplicate_mail_id_across_saved_entries_is_invalid():
    source = mark_rank_reward(pending(), 1, mail_ids=[9])
    source["rank_rewards"]["entries"]["2"].update(status="delivered", mail_ids=[9])
    fail(source, lambda: read_rank_rewards(source), "rank_rewards_invalid")


def test_entry_and_total_delivery_limits_do_not_truncate(monkeypatch):
    assert module.MAX_ENTRIES == module.MAX_MAIL_IDS == 65535
    source = pending()
    source["tables"]["game_param"][0]["Rank"] = 15
    monkeypatch.setattr(module, "MAX_ENTRIES", 2)
    fail(source, lambda: observe_rank_rewards(source, Catalog()), "rank_reward_capacity")
    monkeypatch.setattr(module, "MAX_MAIL_IDS", 2)
    source = mark_rank_reward(source, 1, mail_ids=[8, 9])
    fail(source, lambda: mark_rank_reward(source, 2, mail_ids=[10]), "rank_reward_capacity")


@pytest.mark.parametrize("bonuses", [(Bonus(), Bonus()), (Bonus(1, 20),), (Bonus(1, 0),)])
def test_inconsistent_catalog_range_is_rejected(bonuses):
    source = initialize_rank_rewards(state(10), new_account=True)
    catalog = type("Raw", (), {"between": lambda self, old, new: bonuses})()
    fail(source, lambda: observe_rank_rewards(source, catalog), "rank_reward_definition_invalid")


def test_catalog_error_does_not_modify_input_or_observation():
    source = initialize_rank_rewards(state(10), new_account=True)
    class Broken:
        def between(self, old, new):
            raise ProtocolError("rank_catalog_changed", "Changed", 503)
    fail(source, lambda: observe_rank_rewards(source, Broken()), "rank_catalog_changed")


def test_reader_mark_and_initialization_do_not_alias_state():
    source = pending()
    ledger = read_rank_rewards(source)
    ledger["entries"]["1"]["mail_ids"].append(1)
    assert source["rank_rewards"]["entries"]["1"]["mail_ids"] == []
    result = mark_rank_reward(source, 1, reason="waiting")
    result["other"]["preserve"].clear()
    assert source["other"]["preserve"] == [1, 2]

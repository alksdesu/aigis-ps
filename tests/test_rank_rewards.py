from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.alxml import Column, loads
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
import aigis_ps.event_pass_catalog as event_pass_catalog
from aigis_ps.event_pass_catalog import ELIGIBILITY_RULE, LEGACY_ELIGIBILITY_RULE, EventPassCatalog
from aigis_ps.event_pass_service import EventPassService
from aigis_ps.rank_bonus_catalog import RankBonus, RankBonusCatalog
from aigis_ps.presents import PRESENT_CAPACITY
from aigis_ps.rank_reward_state import (DELIVERED, PENDING, RESPONSE_CAPACITY, bonus_fingerprint,
                                        initialize_rank_rewards, observe_rank_rewards, read_rank_rewards)
from aigis_ps.rank_rewards import DEFINITION_CHANGED, RankRewardService
from aigis_ps.storage import Store
from test_event_pass_service import item_catalogs
from test_quest_service import bundle, request
from test_request_fields import native_request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


class Catalog:
    def __init__(self, *bonuses):
        self.bonuses = bonuses
        self.by_id = MappingProxyType({row.bonus_id: row for row in bonuses})
        self.content_version = "a" * 64

    def between(self, low, high):
        return tuple(bonus for bonus in self.bonuses if low < bonus.rank <= high)

    def status(self):
        return {"ready": True, "bonuses": len(self.bonuses), "content_version": self.content_version}


def bonus(identifier=900, rank=2, *, kind=0, parameter=3):
    return RankBonus(identifier, rank, 100, 1, 5, kind, parameter, "", 1, 0, 0)


@pytest.fixture
def factory(bundle):
    def build(catalog=None, **dependencies):
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        bootstrap.clock = lambda: NOW
        presents.bootstrap = bootstrap
        for name, dependency in dependencies.items():
            setattr(presents, name, dependency)
        if catalog is None:
            catalog = RankBonusCatalog(lambda name: bundle.native_cdn.assets.read(name)[0])
        service = RankRewardService(bootstrap, bundle.player_state, presents, catalog)
        return SimpleNamespace(service=service, bootstrap=bootstrap, players=bundle.player_state,
                               presents=presents, catalog=catalog)
    return build


def set_rank(case, state, rank):
    result = deepcopy(state)
    result["tables"]["game_param"][0].update(Rank=rank,
        Experience=sum(row["NextExp"] for row in case.bootstrap.rank_rows[:rank-1]))
    return result


def initial(case, rank=1):
    state = case.players.initialize(case.bootstrap.initial_state())
    state.pop("rank_rewards", None)
    state["sentinel"] = {"keep": [1]}
    return set_rank(case, state, rank)


def fail_apply(case, before, candidate, code=None):
    saved_before, saved_candidate = deepcopy(before), deepcopy(candidate)
    with pytest.raises(ProtocolError) as caught:
        case.service.apply(before, candidate)
    assert before == saved_before and candidate == saved_candidate
    if code:
        assert caught.value.code == code
    return caught.value


def entry(state, identifier):
    return read_rank_rewards(state)["entries"][str(identifier)]


def pending_source(case):
    before = case.service.initialize(initial(case))
    return observe_rank_rewards(set_rank(case, before, 2), case.catalog)


def message(store, *, tick=1000, premium=0, extra=()):
    return request(store, 116, (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)),
                               Column("DmmPremiumReceiverId", "I", (premium,)), *extra), tick=tick)


def test_real_rank10_grants_unit_and_selection_ticket_without_granting_units_directly(factory):
    case = factory()
    before = case.service.initialize(initial(case, 9))
    candidate = set_rank(case, before, 10)
    saved_before, saved_candidate = deepcopy(before), deepcopy(candidate)
    updated, ids = case.service.apply(before, candidate)
    assert isinstance(ids, tuple) and ids
    assert entry(updated, 7)["status"] == entry(updated, 78)["status"] == DELIVERED
    rows = {row["PresentID"]: row for row in case.presents.rows(updated)}
    chosen = [rows[identifier] for identifier in entry(updated, 78)["mail_ids"]]
    assert [(row["Type"], row["SelectionConfigID"], row["Content"]) for row in chosen] == [(20, 1, 1)]
    assert updated["player_state"] == before["player_state"]
    assert before == saved_before and candidate == saved_candidate


def test_real_rank25_has_distinct_selection_configuration(factory):
    case = factory()
    before = case.service.initialize(initial(case, 24))
    updated, ids = case.service.apply(before, set_rank(case, before, 25))
    assert len(ids) == 1 and entry(updated, 107)["status"] == DELIVERED
    assert case.presents.rows(updated)[-1]["SelectionConfigID"] == 2


def test_crossing_multiple_ranks_records_each_earned_definition_once(factory):
    case = factory()
    before = case.service.initialize(initial(case, 9))
    updated, ids = case.service.apply(before, set_rank(case, before, 25))
    expected = {str(row.bonus_id) for row in case.catalog.between(9, 25)}
    assert set(read_rank_rewards(updated)["entries"]) == expected
    assert entry(updated, 78)["status"] == entry(updated, 107)["status"] == DELIVERED
    assert len(set(ids)) == len(ids)
    repeated, next_ids = case.service.apply(updated, updated)
    assert next_ids == ()
    assert repeated == updated


def test_new_account_and_old_account_baselines_are_distinct(factory):
    case = factory(Catalog(bonus()))
    original = initial(case, 2)
    new = case.service.seed_new(original, NOW)
    old = case.service.initialize(original)
    assert read_rank_rewards(new)["baseline_rank"] == 0
    assert read_rank_rewards(old)["baseline_rank"] == 2
    assert case.presents.rows(new) == case.presents.rows(old) == []
    delivered, ids = case.service.apply(new, new)
    assert ids and entry(delivered, 900)["status"] == DELIVERED
    unchanged, ids = case.service.apply(old, old)
    assert not ids and read_rank_rewards(unchanged)["entries"] == {}


def test_existing_state_initialization_does_not_issue_or_erase_pending(factory):
    case = factory(Catalog(bonus()))
    state = pending_source(case)
    initialized = case.service.initialize(state)
    assert initialized == state and initialized is not state


def test_incomplete_pool_error_is_retained_as_pending(factory, monkeypatch):
    case = factory(Catalog(bonus(kind=16, parameter=32)))
    before = case.service.initialize(initial(case))
    def reject(*_):
        raise ProtocolError("summon_pool_incomplete", "Unavailable complete pool", 501)
    monkeypatch.setattr(case.presents, "enqueue", reject)
    updated, ids = case.service.apply(before, set_rank(case, before, 2))
    assert ids == ()
    record = entry(updated, 900)
    assert record["status"] == PENDING and record["mail_ids"] == []
    assert record["reason"] == "summon_pool_incomplete"
    assert updated["tables"]["game_param"][0]["Rank"] == 2


def test_full_mailbox_stays_pending_and_can_be_retried_after_space_is_available(factory):
    case = factory(Catalog(bonus()))
    before = case.service.initialize(initial(case))
    before, _ = case.presents.enqueue(before, [{"kind": "free_crystal", "amount": 1, "description": "old"}] * 500)
    pending, ids = case.service.apply(before, set_rank(case, before, 2))
    assert not ids and entry(pending, 900)["status"] == PENDING
    assert entry(pending, 900)["reason"] == PRESENT_CAPACITY
    freed = deepcopy(pending)
    freed["tables"]["presents"].pop()
    delivered, ids = case.service.apply(pending, freed)
    assert ids and entry(delivered, 900)["status"] == DELIVERED
    assert entry(delivered, 900)["reason"] is None
    assert len(case.presents.rows(delivered)) == 500


@pytest.mark.parametrize("status", [400, 503])
def test_unrecoverable_mail_error_rejects_whole_candidate(factory, monkeypatch, status):
    case = factory(Catalog(bonus()))
    before = case.service.initialize(initial(case))
    def reject(*_):
        raise ProtocolError("broken_reward_source", "Broken", status)
    monkeypatch.setattr(case.presents, "enqueue", reject)
    fail_apply(case, before, set_rank(case, before, 2), "broken_reward_source")


def test_later_fatal_error_rolls_back_earlier_mail_in_same_candidate(factory, monkeypatch):
    case = factory(Catalog(bonus(), bonus(901, 3)))
    before = case.service.initialize(initial(case))
    original = case.presents.enqueue
    calls = []
    def enqueue(state, rewards):
        calls.append(1)
        if len(calls) == 2:
            raise ProtocolError("broken_reward_source", "Broken", 503)
        return original(state, rewards)
    monkeypatch.setattr(case.presents, "enqueue", enqueue)
    fail_apply(case, before, set_rank(case, before, 3), "broken_reward_source")
    assert len(calls) == 2


def test_definition_drift_keeps_original_pending_record(factory):
    original_bonus = bonus()
    case = factory(Catalog(original_bonus))
    pending = pending_source(case)
    changed = factory(Catalog(replace(original_bonus, parameter=99)))
    updated, ids = changed.service.apply(pending, pending)
    assert ids == () and entry(updated, 900)["status"] == PENDING
    assert entry(updated, 900)["definition"] == bonus_fingerprint(original_bonus)
    assert entry(updated, 900)["reason"] == DEFINITION_CHANGED
    assert case.presents.rows(updated) == []


def test_display_order_change_does_not_block_original_pending_delivery(factory):
    original_bonus = bonus()
    case = factory(Catalog(original_bonus))
    pending = pending_source(case)
    changed = factory(Catalog(replace(original_bonus, source_index=900, config_index=800, display_text_id=200)))
    updated, ids = changed.service.apply(pending, pending)
    assert ids and entry(updated, 900)["status"] == DELIVERED


@pytest.mark.parametrize("change", ["observed", "delete", "entries"])
def test_candidate_cannot_modify_the_reward_ledger_outside_parent_apply(factory, change):
    case = factory(Catalog(bonus()))
    before = case.service.initialize(initial(case))
    candidate = set_rank(case, before, 2)
    if change == "observed":
        candidate["rank_rewards"]["observed_rank"] = 2
    elif change == "delete":
        del candidate["rank_rewards"]
    else:
        candidate["rank_rewards"] = initialize_rank_rewards(candidate, new_account=True)["rank_rewards"]
        candidate["rank_rewards"]["baseline_rank"] = 0
    assert fail_apply(case, before, candidate).status in (409, 503)


def test_delivered_reward_is_not_reissued_when_mail_is_no_longer_in_mailbox(factory):
    case = factory(Catalog(bonus()))
    before = case.service.initialize(initial(case))
    issued, ids = case.service.apply(before, set_rank(case, before, 2))
    consumed = deepcopy(issued)
    consumed["tables"]["presents"] = []
    updated, again = case.service.apply(issued, consumed)
    assert again == ()
    assert entry(updated, 900)["mail_ids"] == list(ids)
    assert updated["tables"]["presents"] == []


def test_receive_116_delivers_only_pending_and_replays_without_new_mail(factory, tmp_path):
    case = factory(Catalog(bonus()))
    store = Store(tmp_path / "rank-rewards.sqlite3", pending_source(case))
    envelope = message(store)
    response = case.service.receive(envelope, store)
    names = [child.name for child in loads(response).children]
    assert names == ["SYSTEM", "PPT"]
    before = store.profile()
    assert case.service.receive(envelope, Store(store.path)) == response
    assert store.profile() == before
    assert entry(before.state, 900)["status"] == DELIVERED


def test_no_pending_116_does_not_fake_rankup_or_activity_results(factory, tmp_path):
    case = factory(Catalog(bonus()))
    store = Store(tmp_path / "rank-no-pending.sqlite3", case.service.initialize(initial(case)))
    response = case.service.receive(message(store), store)
    assert [child.name for child in loads(response).children] == ["SYSTEM"]


def test_native_116_with_duplicated_platform_code_is_accepted(factory, tmp_path):
    case = factory(Catalog(bonus()))
    store = Store(tmp_path / "rank-native.sqlite3", case.service.initialize(initial(case)))
    columns = (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)), Column("DmmPremiumReceiverId", "I", (0,)))
    response = case.service.receive(native_request(store.profile(), 116, columns), store)
    assert [child.name for child in loads(response).children] == ["SYSTEM"]
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.receive(native_request(before, 116, columns, tick=1001, platform_codes=(1, 2)), store)
    assert caught.value.code == "invalid_request_fields"
    assert store.profile() == before


def test_116_nonzero_membership_or_untrusted_rank_is_rejected(factory, tmp_path):
    case = factory(Catalog(bonus()))
    store = Store(tmp_path / "rank-request.sqlite3", pending_source(case))
    for envelope in (message(store, premium=1), message(store, extra=(Column("Rank", "I", (100,)),))):
        before = store.profile()
        with pytest.raises(ProtocolError):
            case.service.receive(envelope, store)
        assert store.profile() == before


def test_regressed_before_rank_cannot_be_hidden_by_higher_candidate_rank(factory):
    case = factory(Catalog(bonus(rank=6)))
    observed = case.service.initialize(initial(case, 5))
    before = set_rank(case, observed, 3)
    candidate = set_rank(case, before, 6)
    assert read_rank_rewards(before)["observed_rank"] == 5
    fail_apply(case, before, candidate, "rank_reward_rank_backwards")


def test_same_rank_delivery_uses_config_order_when_only_one_mail_slot_remains(factory):
    first = replace(bonus(20, parameter=20), config_index=0)
    second = replace(bonus(10, parameter=10), config_index=1)
    case = factory(Catalog(first, second))
    before = case.service.initialize(initial(case))
    before, _ = case.presents.enqueue(before,
        [{"kind": "free_crystal", "amount": 1, "description": "old"}] * 499)
    updated, ids = case.service.apply(before, set_rank(case, before, 2))
    assert len(ids) == 1
    assert entry(updated, 20)["status"] == DELIVERED
    assert entry(updated, 10)["status"] == PENDING
    assert entry(updated, 10)["reason"] == PRESENT_CAPACITY
    assert case.presents.rows(updated)[-1]["Content"] == 20


def test_ledger_statuses_keep_the_spelling_of_earlier_saves():
    assert (PENDING, DELIVERED) == ("pending", "delivered")
    # Pending reasons of other ledgers keep this code, so it stays spelled as earlier saves recorded it.
    assert RESPONSE_CAPACITY == "rank_reward_response_capacity"


def test_pass_mail_saved_under_the_earlier_identity_bound_stays_readable():
    from aigis_ps.event_pass_state import MAX_ITEM_BYTES
    legacy = "x" * (MAX_ITEM_BYTES + 1)
    assert RankRewardService._pass_mail_map({legacy: [7, 8]}) == {legacy: [7, 8]}


def test_replayable_versions_are_the_bases_of_every_item_catalog_a_gift_box_committed(factory, item_catalogs):
    case = factory(Catalog(bonus()))
    versions = case.service.replayable_content_versions()
    assert case.service.content_version in versions
    assert len(versions) == len(case.presents.item_catalog.replayable_content_versions()) > 1
    earlier = factory(Catalog(bonus()), item_catalog=item_catalogs.plain).service
    assert earlier.content_version in versions - {case.service.content_version}
    assert earlier.replayable_content_versions() == {earlier.content_version}
    assert factory(Catalog(bonus()), recovery_catalog=None).service.content_version == case.service.content_version
    changed = Catalog(bonus())
    changed.content_version = "b" * 64
    assert factory(changed).service.content_version not in versions


def pass_rank(case, bundle, rule):
    # Rebuilt from the resources under one pass eligibility rule, as that build digested them.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(event_pass_catalog, "ELIGIBILITY_RULE", rule)
        catalog = EventPassCatalog(lambda name: bundle.native_cdn.assets.read(name)[0], bundle.possessions)
    passes = EventPassService(case.bootstrap, case.players, case.presents, bundle.possessions, catalog,
                              automation_rules=bundle.event_passes.automation_rules)
    case.bootstrap.event_pass_provider = passes.response
    return RankRewardService(case.bootstrap, case.players, case.presents, case.catalog, event_passes=passes)


def test_116_receipt_committed_while_pass_windows_compared_jst_instants_replays_its_bytes(factory, bundle, tmp_path):
    case = factory(Catalog(bonus()))
    earlier = pass_rank(case, bundle, LEGACY_ELIGIBILITY_RULE)
    store = Store(tmp_path / "rank-pass-window.sqlite3", pending_source(case))
    envelope = message(store)
    payload = earlier.receive(envelope, store)
    saved = store.profile()
    current = pass_rank(case, bundle, ELIGIBILITY_RULE)
    content = saved.state["rank_reward_history"]["receipts"][-1]["content"]
    assert content == earlier._pass_receive_content_version != current._pass_receive_content_version
    assert current.receive(envelope, Store(store.path)) == payload
    assert store.profile() == saved
    forged = deepcopy(saved.state)
    forged["rank_reward_history"]["receipts"][-1]["content"] = "f" * 64
    with store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(forged),))
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        current.receive(envelope, store)
    assert (caught.value.code, caught.value.status) == ("rank_reward_retry_stale", 409)
    assert store.profile() == before

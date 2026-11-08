from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from threading import Barrier

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.rank_rewards import RankRewardService
from aigis_ps.storage import Store
from test_event_pass_service import baseline, bundle, factory, nodes
from test_rank_rewards import Catalog, bonus, message


@pytest.fixture
def dispatch(factory):
    def build(*, points=10, rank_reward=True, enabled=True):
        case = factory(points, active=False)
        catalog = Catalog(bonus()) if rank_reward else Catalog()
        case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, catalog,
                                      event_passes=case.service if enabled else None)
        case.rank_catalog = catalog
        profile = case.store.profile()
        state = deepcopy(profile.state)
        state["tables"]["game_param"][0].update(Rank=2, Experience=case.bootstrap.rank_rows[0]["NextExp"])
        case.store.update_state(state, profile.revision)
        return case
    return build


def fail(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.rank.receive(envelope, case.store)
    if code:
        assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


def rewrite(case, change, *, keep_revision=False):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    change(state)
    if keep_revision:
        with case.store.connection() as connection:
            connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    else:
        case.store.update_state(state, profile.revision)


def test_rank_and_closed_pass_share_one_commit_and_explicit_sources(dispatch):
    case = dispatch()
    before = case.store.profile()
    result = nodes(case.rank.receive(message(case.store), case.store))
    saved = case.store.profile()
    assert saved.revision == before.revision + 1
    assert set(result) == {"SYSTEM", "PPT", "EventPassStatus"}
    marker = saved.state["rank_reward_delivery"]
    assert marker == {"schema_version": 2, "request": marker["request"], "mail_ids": [1, 2, 3],
                      "rank_mail_ids": [1], "event_pass_mail_ids": {case.entry.item_id: [2, 3]}}
    rows = case.presents.rows(saved.state)
    assert [row.get("NotificationCategory", 0) for row in rows] == [0, 7, 7]
    assert saved.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert saved.state["player_state"] == before.state["player_state"]
    assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert case.clock.calls == 1


def test_apply_does_not_dispatch_auto_or_read_clock(dispatch, monkeypatch):
    case = dispatch()
    monkeypatch.setattr(case.service, "settle_expired", lambda *args, **kwargs: pytest.fail("apply ran automatic rewards"))
    case.bootstrap.clock = lambda: pytest.fail("apply read the clock")
    before = case.store.profile().state
    updated, ids = case.rank.apply(before, before)
    assert ids == (1,)
    assert updated["tables"]["event_pass_status"] == before["tables"]["event_pass_status"]
    assert len(updated["tables"]["presents"]) == 1


def test_old_schema1_receipt_replays_without_auto_dependency_or_clock(dispatch, monkeypatch):
    case = dispatch(enabled=False)
    envelope = message(case.store)
    payload = case.rank.receive(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["rank_reward_delivery"]["schema_version"] == 1
    old_version = case.rank.content_version
    case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
                                  event_passes=case.service)
    assert case.rank.content_version == old_version
    assert case.rank.receive_content_version != old_version
    monkeypatch.setattr(case.service, "_automatic_ready", lambda: pytest.fail("legacy replay touched auto dependency"))
    monkeypatch.setattr(case.service, "settle_expired", lambda *args, **kwargs: pytest.fail("legacy replay reran auto"))
    case.bootstrap.clock = lambda: pytest.fail("legacy replay read clock")
    reopened = Store(case.store.path)
    assert case.rank.receive(envelope, reopened) == payload
    assert reopened.profile() == saved


def test_mixed_replay_reopens_without_dispatch_or_clock(dispatch, monkeypatch):
    case = dispatch()
    envelope = message(case.store)
    payload = case.rank.receive(envelope, case.store)
    saved = case.store.profile()
    case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
                                  event_passes=case.service)
    monkeypatch.setattr(case.service, "settle_expired", lambda *args, **kwargs: pytest.fail("replay reran auto"))
    case.bootstrap.clock = lambda: pytest.fail("replay read clock")
    reopened = Store(case.store.path)
    assert case.rank.receive(envelope, reopened) == payload
    assert reopened.profile() == saved


def test_new_action_after_legacy_receipt_can_dispatch_only_pass(dispatch):
    case = dispatch(enabled=False)
    case.rank.receive(message(case.store), case.store)
    case.rank = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog,
                                  event_passes=case.service)
    result = nodes(case.rank.receive(message(case.store, tick=1001), case.store))
    marker = case.store.profile().state["rank_reward_delivery"]
    assert marker["rank_mail_ids"] == []
    assert marker["event_pass_mail_ids"] == {case.entry.item_id: [2, 3]}
    assert next(column.values for column in result["PPT"].children if column.name == "PresentID") == (2, 3)


def test_no_rewards_still_record_mixed_receipt_and_exact_retry_is_read_only(dispatch, monkeypatch):
    case = dispatch(points=0, rank_reward=False)
    envelope = message(case.store)
    before = case.store.profile()
    response = case.rank.receive(envelope, case.store)
    saved = case.store.profile()
    assert saved.revision == before.revision + 1
    assert set(nodes(response)) == {"SYSTEM"}
    assert saved.state["rank_reward_delivery"]["mail_ids"] == []
    assert saved.state["rank_reward_delivery"]["event_pass_mail_ids"] == {}
    monkeypatch.setattr(case.service, "settle_expired", lambda *args, **kwargs: pytest.fail("empty retry dispatched"))
    case.bootstrap.clock = lambda: pytest.fail("empty retry read clock")
    assert case.rank.receive(envelope, case.store) == response
    assert case.store.profile() == saved


def test_pass_capacity_failure_preserves_rank_and_retries_on_later_request(dispatch):
    case = dispatch()
    profile = case.store.profile()
    state, _ = case.presents.enqueue(profile.state, [{"kind": "unit", "template_id": 6,
                                                    "count": 499, "description": "existing"}])
    case.store.update_state(state, profile.revision)
    result = nodes(case.rank.receive(message(case.store), case.store))
    saved = case.store.profile()
    assert len(case.presents.rows(saved.state)) == 500
    assert saved.state["rank_reward_delivery"]["rank_mail_ids"] == [500]
    assert saved.state["rank_reward_delivery"]["event_pass_mail_ids"] == {}
    assert saved.state["event_pass_automation"]["pending"][case.entry.item_id] == "present_capacity"
    assert "EventPassStatus" not in result
    rewrite(case, lambda state: state["tables"].update(presents=state["tables"]["presents"][2:]))
    case.rank.receive(message(case.store, tick=1001), case.store)
    saved = case.store.profile()
    assert saved.state["rank_reward_delivery"]["rank_mail_ids"] == []
    assert saved.state["rank_reward_delivery"]["event_pass_mail_ids"] == {case.entry.item_id: [501, 502]}
    assert saved.state["event_pass_automation"]["pending"] == {}


@pytest.mark.parametrize("number", [42, 116, 137])
def test_fatal_final_capacity_failure_keeps_every_domain_uncommitted(dispatch, number):
    case = dispatch()
    case.rank._profiles[number] = replace(case.rank._profiles[number], response_limit=1)
    fail(case, message(case.store), "response_too_large")


def test_wrong_137_provider_aborts_rank_and_pass_together(dispatch):
    case = dispatch()
    from aigis_ps.alxml import Container
    case.bootstrap.event_pass_provider = lambda _: Container("EventPassStatus")
    fail(case, message(case.store), "event_pass_provider_inconsistent")


def test_cas_retry_reuses_clock_and_preserves_unrelated_state(dispatch, monkeypatch):
    case = dispatch()
    original = case.store.update_state
    calls = []

    def conflict(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            profile = case.store.profile()
            other = deepcopy(profile.state)
            other["sentinel"]["concurrent"] = 1
            original(other, profile.revision)
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", conflict)
    case.rank.receive(message(case.store), case.store)
    saved = case.store.profile()
    assert len(calls) == 2 and case.clock.calls == 1
    assert saved.state["sentinel"]["concurrent"] == 1
    assert len(case.presents.rows(saved.state)) == 3
    assert saved.state["rank_reward_delivery"]["mail_ids"] == [1, 2, 3]


@pytest.mark.parametrize("tamper", ["rank_steals_pass", "pass_steals_rank", "duplicate", "union", "wrong_pass", "schema"])
def test_source_marker_cannot_relabel_or_drop_delivered_mail(dispatch, tamper):
    case = dispatch()
    envelope = message(case.store)
    case.rank.receive(envelope, case.store)
    def corrupt(state):
        marker = state["rank_reward_delivery"]
        if tamper == "rank_steals_pass":
            marker["rank_mail_ids"], marker["event_pass_mail_ids"][case.entry.item_id] = [1, 2], [3]
        elif tamper == "pass_steals_rank":
            marker["rank_mail_ids"], marker["event_pass_mail_ids"][case.entry.item_id] = [], [1, 2, 3]
        elif tamper == "duplicate":
            marker["event_pass_mail_ids"][case.entry.item_id].append(1)
        elif tamper == "union":
            marker["mail_ids"] = [1, 2]
        elif tamper == "wrong_pass":
            marker["event_pass_mail_ids"] = {"another_pass": [2, 3]}
        else:
            marker["schema_version"] = True
    rewrite(case, corrupt, keep_revision=True)
    fail(case, envelope, "rank_reward_history_invalid")


def test_missing_auto_dependency_cannot_read_mixed_marker_as_legacy(dispatch):
    case = dispatch()
    case.rank.receive(message(case.store), case.store)
    plain = RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.rank_catalog)
    with pytest.raises(ProtocolError) as caught:
        plain.initialize(case.store.profile().state)
    assert caught.value.code == "rank_event_pass_unavailable"


def test_auto_fingerprint_drift_rejects_new_and_cached_mixed_requests(dispatch, monkeypatch):
    case = dispatch()
    envelope = message(case.store)
    case.rank.receive(envelope, case.store)
    monkeypatch.setattr(case.service, "_automation_version", "0" * 64)
    fail(case, envelope, "event_pass_automation_changed")
    fail(case, message(case.store, tick=1001), "event_pass_automation_changed")


def test_pass_map_order_is_canonical_across_storage_round_trip():
    unsorted = {"pass_z": [3, 4], "pass_a": [2]}
    canonical = RankRewardService._pass_mail_map(unsorted)
    assert list(canonical) == ["pass_a", "pass_z"]
    assert RankRewardService._combined_mail_ids([1], canonical) == [1, 2, 3, 4]
    assert RankRewardService._pass_mail_map(json.loads(json.dumps(unsorted, sort_keys=True))) == canonical


def test_same_mixed_request_commits_once_when_concurrent(dispatch):
    case = dispatch()
    envelope = message(case.store)
    before = case.store.profile()
    barrier = Barrier(3)
    def run():
        barrier.wait()
        return case.rank.receive(envelope, case.store)
    with ThreadPoolExecutor(max_workers=3) as executor:
        replies = list(executor.map(lambda _: run(), range(3)))
    assert replies == [replies[0]] * 3
    saved = case.store.profile()
    assert saved.revision == before.revision + 1
    assert len(case.presents.rows(saved.state)) == 3


def test_mixed_cache_revalidates_pending_state_even_after_digest_recomputation(dispatch, monkeypatch):
    from aigis_ps.rank_rewards import _state_digest
    case = dispatch()
    envelope = message(case.store)
    case.rank.receive(envelope, case.store)

    def corrupt(state):
        state["event_pass_automation"] = {"schema_version": 1, "pending": []}
        state["rank_reward_history"]["receipts"][-1]["state"] = _state_digest(state)

    rewrite(case, corrupt, keep_revision=True)
    monkeypatch.setattr(case.service, "settle_expired", lambda *args, **kwargs: pytest.fail("Cache reran automatic settlement"))
    case.bootstrap.clock = lambda: pytest.fail("Cache read the clock")
    fail(case, envelope, "event_pass_automation_invalid")

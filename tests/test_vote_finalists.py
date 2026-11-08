from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.vote_catalog import VoteCandidate, VoteGroup
from aigis_ps.vote_finalists import VoteFinalistService
from aigis_ps.vote_state import record_vote, vote_key
from test_vote_catalog import vote_sources, build


START = datetime(2026, 11, 1, 15, tzinfo=JST)


def catalog(*, start=START, source="a" * 64, enabled=True, unresolved=False):
    groups = []
    for identifier in range(1, 5):
        opened = start + timedelta(days=(identifier - 1) * 3)
        closed = opened + timedelta(days=3)
        window = (opened, closed, opened, closed, closed + timedelta(hours=1))
        groups.append(VoteGroup(identifier, f"item_vote_016{identifier:03}", *window, identifier == 4, *window))
    candidates = tuple(VoteCandidate(group * 100 + index, group, f"card-{group}-{index}", 3)
                       for group in (1, 2, 3) for index in range(1, 13))
    return SimpleNamespace(_ready=lambda: None, groups=tuple(groups),
        groups_by_id=MappingProxyType({group.group_id: group for group in groups}), candidates=candidates,
        source_content_version=source, event=SimpleNamespace(event_id=16),
        rules=SimpleNamespace(enabled=enabled), unresolved_candidates=("unknown",) if unresolved else ())


def state(*entries):
    rows = [{"key": vote_key(item, card), "cardId": card, "groupId": group, "count": count, "itemId": item}
            for item, group, card, count in entries]
    return {"bootstrap_schema": 1, "tables": {"vote_history": rows}, "unrelated": {"values": [1, 2]}}


def votes(group, counts):
    return [(group.item_id, group.group_id, identifier, count) for identifier, count in counts.items()]


def test_initialize_preserves_legacy_and_does_not_freeze_without_observation():
    service = VoteFinalistService(catalog())
    initial = state(("item_vote_016001", 1, 101, 99))
    copy = service.initialize(initial)
    assert copy == initial and copy is not initial and "vote_finalists" not in copy
    assert service.settle(initial, START) == (initial, ())


def test_boundary_freezes_all_candidates_and_is_idempotent():
    source = catalog()
    service = VoteFinalistService(source)
    group = source.groups[0]
    initial = state(*votes(group, {101: 1, 102: 7}))
    original = deepcopy(initial)
    assert service.settle(initial, group.vote_end - timedelta(microseconds=1)) == (initial, ())
    frozen, changed = service.settle(initial, group.vote_end)
    assert initial == original and changed == (group.item_id,)
    saved = frozen["vote_finalists"]["groups"][group.item_id]
    assert saved["origin"] == "first_observation_baseline" and saved["source_sha256"] == source.source_content_version
    assert saved["frozen_at"] == saved["window_end"]
    assert saved["counts"][:2] == [{"CardID": 102, "Count": 7}, {"CardID": 101, "Count": 1}]
    assert len(saved["qualified_ids"]) == 12
    assert service.settle(frozen, group.vote_end + timedelta(hours=1)) == (frozen, ())
    assert service.is_frozen(frozen, 1)
    with pytest.raises(ProtocolError) as failure:
        service.require_open(frozen, 1)
    assert failure.value.code == "vote_group_frozen"


def test_tenth_place_tie_qualifies_every_equal_candidate_in_stable_order():
    source = catalog()
    service = VoteFinalistService(source)
    group = source.groups[0]
    counts = {identifier: 120 - identifier for identifier in range(101, 110)}
    counts.update({110: 5, 111: 5, 112: 4})
    frozen, _ = service.settle(state(*votes(group, counts)), group.vote_end)
    saved = frozen["vote_finalists"]["groups"][group.item_id]
    assert saved["qualified_ids"] == list(range(101, 112))
    assert saved["counts"][-3:] == [{"CardID": 110, "Count": 5}, {"CardID": 111, "Count": 5}, {"CardID": 112, "Count": 4}]


def test_zero_ties_can_qualify_every_candidate_without_inventing_votes():
    source = catalog()
    service = VoteFinalistService(source)
    frozen, changed = service.settle(state(), source.groups[2].vote_end)
    assert changed == tuple(group.item_id for group in source.groups[:3])
    assert len(service.candidates(frozen, source.groups[3].vote_start)) == 36
    assert all(row["Count"] == 0 for group in source.groups[:3] for row in service.ranking(frozen, group))
    assert frozen["tables"]["vote_history"] == []


def test_final_candidates_require_all_preliminary_snapshots_and_start_time():
    source = catalog()
    service = VoteFinalistService(source)
    first, _ = service.settle(state(), source.groups[0].vote_end)
    assert service.candidates(first, source.groups[3].vote_start) == ()
    assert not service.is_candidate(first, 4, 101)
    all_groups, _ = service.settle(first, source.groups[2].vote_end)
    assert service.candidates(all_groups, source.groups[3].vote_start - timedelta(microseconds=1)) == ()
    candidates = service.candidates(all_groups, source.groups[3].vote_start)
    assert len(candidates) == 36 and all(candidate.group_id == 4 for candidate in candidates)
    assert service.is_candidate(all_groups, 4, 101)
    assert service.is_candidate(all_groups, 1, 101)
    assert not service.is_candidate(all_groups, 2, 101)
    assert not service.is_candidate(all_groups, 1, True)
    assert not service.is_candidate(all_groups, 4, 999)


def test_final_results_freeze_separate_item_counts_and_preserve_qualified_roster():
    source = catalog()
    service = VoteFinalistService(source)
    group = source.groups[3]
    preliminary, _ = service.settle(state(), group.vote_start)
    with_votes = record_vote(preliminary, item_id=group.item_id, group_id=4, card_id=102,
                             count=18, definition="b" * 64)
    frozen, changed = service.settle(with_votes, group.vote_end)
    assert changed == (group.item_id,)
    saved = frozen["vote_finalists"]["groups"][group.item_id]
    assert saved["qualified_ids"] == [] and len(saved["counts"]) == 36
    assert service.ranking(frozen, group)[0] == {"CardID": 102, "Count": 18}
    tampered_live = record_vote(frozen, item_id=group.item_id, group_id=4, card_id=102,
                               count=50, definition="b" * 64)
    for read in (lambda: service.ranking(tampered_live, group),
                 lambda: service.settle(tampered_live, group.vote_end + timedelta(days=1))):
        with pytest.raises(ProtocolError) as failure:
            read()
        assert failure.value.code == "vote_finalists_history_changed"


def test_late_legacy_observation_freezes_all_stages_without_awards_or_vote_changes():
    source = catalog()
    service = VoteFinalistService(source)
    initial = state((source.groups[0].item_id, 1, 101, 999), (source.groups[3].item_id, 4, 101, 12))
    frozen, changed = service.settle(initial, source.groups[3].vote_end + timedelta(days=7))
    assert changed == tuple(group.item_id for group in source.groups)
    assert {key: value for key, value in frozen.items() if key != "vote_finalists"} == initial
    assert all(saved["origin"] == "first_observation_baseline" for saved in frozen["vote_finalists"]["groups"].values())


@pytest.mark.parametrize("offset", (-30, 30))
def test_changed_schedule_never_reopens_frozen_groups(offset):
    source = catalog()
    service = VoteFinalistService(source)
    frozen, _ = service.settle(state(), source.groups[0].vote_end)
    changed = VoteFinalistService(catalog(start=START + timedelta(days=offset)))
    assert changed.content_version == service.content_version
    assert changed.initialize(frozen) == frozen
    with pytest.raises(ProtocolError) as failure:
        changed.require_open(frozen, 1)
    assert failure.value.code == "vote_group_frozen"
    assert changed.ranking(frozen, 1) == service.ranking(frozen, 1)


def test_source_changes_preserve_old_snapshots_but_reject_continuation():
    source = catalog()
    first = VoteFinalistService(source)
    frozen, _ = first.settle(state(), source.groups[0].vote_end)
    changed = VoteFinalistService(catalog(source="b" * 64))
    assert changed.initialize(frozen) == frozen
    for action in (lambda: changed.settle(frozen, source.groups[1].vote_end),
                   lambda: changed.ranking(frozen, 1),
                   lambda: changed.candidates(frozen, source.groups[3].vote_start)):
        with pytest.raises(ProtocolError) as failure:
            action()
        assert failure.value.code == "vote_finalists_definition_changed"


def test_disabled_does_not_create_snapshots_and_rollback_is_rejected():
    source = catalog()
    service = VoteFinalistService(source)
    assert VoteFinalistService(catalog(enabled=False)).settle(state(), source.groups[3].vote_end) == (state(), ())
    frozen, _ = service.settle(state(), source.groups[0].vote_end)
    with pytest.raises(ProtocolError) as failure:
        service.settle(frozen, source.groups[0].vote_end - timedelta(microseconds=1))
    assert failure.value.code == "vote_finalists_clock_reversed"


def test_unresolved_candidates_cannot_create_partial_rankings():
    source = catalog(unresolved=True)
    service = VoteFinalistService(source)
    initial = state()
    with pytest.raises(ProtocolError) as failure:
        service.settle(initial, source.groups[0].vote_end)
    assert failure.value.code == "vote_candidates_unresolved" and "vote_finalists" not in initial


@pytest.mark.parametrize("entry", [("item_vote_016001", 1, 999, 5), ("item_vote_016001", 2, 101, 5),
                                  ("item_vote_016004", 4, 999, 5)])
def test_ineligible_live_rows_prevent_freeze_atomically(entry):
    source = catalog()
    service = VoteFinalistService(source)
    initial = state(entry)
    original = deepcopy(initial)
    with pytest.raises(ProtocolError) as failure:
        service.settle(initial, source.groups[3].vote_end)
    assert failure.value.code == "vote_finalists_history_invalid" and initial == original


@pytest.mark.parametrize("mutation", ["schema", "field", "hash", "count", "tie", "order", "date", "future_end", "duplicate", "final_without_preliminary"])
def test_malformed_snapshots_are_rejected(mutation):
    source = catalog()
    service = VoteFinalistService(source)
    frozen, _ = service.settle(state(), source.groups[3].vote_end)
    ledger = frozen["vote_finalists"]
    row = ledger["groups"][source.groups[0].item_id]
    if mutation == "schema": ledger["schema_version"] = True
    if mutation == "field": row["extra"] = 1
    if mutation == "hash": row["definition"] = "broken"
    if mutation == "count": row["counts"][0]["Count"] = True
    if mutation == "tie": row["qualified_ids"].pop()
    if mutation == "order": row["counts"].reverse()
    if mutation == "date": row["frozen_at"] = "2026-11-01"
    if mutation == "future_end": row["window_end"] = "2030-11-01T00:00:00.000000+00:00"
    if mutation == "duplicate": row["counts"][1] = deepcopy(row["counts"][0])
    if mutation == "final_without_preliminary": del ledger["groups"][source.groups[0].item_id]
    with pytest.raises(ProtocolError):
        service.initialize(frozen)


def test_read_views_are_independent_copies_and_unfrozen_counts_are_live():
    source = catalog()
    service = VoteFinalistService(source)
    initial = state((source.groups[0].item_id, 1, 101, 2))
    view = service.ranking(initial, 1)
    view[0]["Count"] = 99
    assert service.ranking(initial, 1)[0]["Count"] == 2
    frozen, _ = service.settle(initial, source.groups[0].vote_end)
    view = service.ranking(frozen, 1)
    view[0]["Count"] = 77
    assert service.ranking(frozen, 1)[0]["Count"] == 2


def test_real_sixteenth_event_resolves_all_956_candidates_and_zero_tie_final(vote_sources):
    source = build(vote_sources)
    service = VoteFinalistService(source)
    frozen, changed = service.settle(state(), source.groups[-1].vote_start)
    assert len(changed) == 3
    assert [len(service.ranking(frozen, identifier)) for identifier in (1, 2, 3)] == [318, 319, 319]
    assert len(service.candidates(frozen, source.groups[-1].vote_start)) == 956
    final, changed = service.settle(frozen, source.groups[-1].vote_end)
    assert changed == (source.groups[-1].item_id,) and len(service.ranking(final, 4)) == 956


def test_live_catalog_mutation_is_rejected():
    source = catalog()
    service = VoteFinalistService(source)
    source.candidates = (replace(source.candidates[0], card_id=999), *source.candidates[1:])
    with pytest.raises(ProtocolError) as failure:
        service.initialize(state())
    assert failure.value.code == "vote_finalists_dependencies_changed"


@pytest.mark.parametrize("removal", ("domain", "first", "second", "third"))
def test_qualification_proof_requires_every_frozen_preliminary_group(removal):
    source = catalog()
    service = VoteFinalistService(source)
    frozen, _ = service.settle(state(), source.groups[3].vote_start)
    assert service.validate_qualification(frozen, 101) is None
    if removal == "domain":
        del frozen["vote_finalists"]
    else:
        index = ("first", "second", "third").index(removal)
        del frozen["vote_finalists"]["groups"][source.groups[index].item_id]
    with pytest.raises(ProtocolError) as failure:
        service.validate_qualification(frozen, 101)
    assert failure.value.code == "vote_finalists_qualification_missing"


@pytest.mark.parametrize("card_id", (112, 999, True, 0, None))
def test_qualification_proof_rejects_eliminated_or_invalid_candidates(card_id):
    source = catalog()
    service = VoteFinalistService(source)
    entries = votes(source.groups[0], {identifier: 1 for identifier in range(101, 111)})
    frozen, _ = service.settle(state(*entries), source.groups[3].vote_start)
    with pytest.raises(ProtocolError) as failure:
        service.validate_qualification(frozen, card_id)
    assert failure.value.code == "vote_finalists_qualification_invalid"


def test_qualification_proof_survives_source_changes_and_saved_item_names():
    source = catalog()
    original = VoteFinalistService(source)
    frozen, _ = original.settle(state(), source.groups[3].vote_start)
    archived = frozen["vote_finalists"]["groups"]
    for group in source.groups[:3]:
        archived[f"archived_{group.item_id}"] = archived.pop(group.item_id)
    changed = VoteFinalistService(catalog(source="b" * 64))
    assert changed.initialize(frozen) == frozen
    assert changed.validate_qualification(frozen, 101) is None
    assert changed.validate_qualification(frozen, 201) is None
    assert changed.validate_qualification(frozen, 301) is None
    with pytest.raises(ProtocolError) as failure:
        changed.ranking(frozen, 1)
    assert failure.value.code == "vote_finalists_definition_changed"

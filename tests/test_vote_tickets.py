from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
import zlib

import pytest

from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import PossessionService
from aigis_ps.vote_catalog import CANDIDATES_UNRESOLVED
from aigis_ps.vote_tickets import (AVAILABLE, BASELINE_ORIGIN, CONSUMED, DAILY_ITEM, EXPIRED, LEDGER_KEY,
    LOCAL_ORIGIN, UNOBSERVED, UNRESOLVED, VoteTicketService, _FIELDS)
from test_login_reward_state import (BUSINESS_EDGES, CLIENT_CALENDAR, OUTSIDE_CALENDAR, epoch_milliseconds,
    representations, shifted_day)
from test_present_items import item_table
from test_vote_catalog import vote_sources, build, resolved_rules


START = datetime(2026, 11, 1, 15, tzinfo=JST)
NOW = START + timedelta(hours=1)
GROUP = "item_vote_016001"


def item(name, **changes):
    row = {field: "" if kind == 32 else 0 for field, kind in _FIELDS.items()}
    row.update(ItemID=name, ID=(zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000,
               Category=1925062086, EffectType=0 if name == DAILY_ITEM else 10,
               Stack=1 if name == DAILY_ITEM else 999, CanAllGet=1)
    row.update(changes)
    return row


def components(*, enabled=True, unresolved=False, start=START, daily_changes=None):
    raw = item_table([item(DAILY_ITEM, **(daily_changes or {}))] +
        [item(f"item_vote_016{group:03}") for group in range(1, 5)], tuple(_FIELDS.items()))
    possessions = PossessionService(Path("."), asset_reader=lambda name: raw)
    groups = tuple(SimpleNamespace(item_id=f"item_vote_016{group:03}",
        vote_start=start + timedelta(days=(group - 1) * 3), vote_end=start + timedelta(days=group * 3, hours=1))
        for group in range(1, 5))
    version = hashlib.sha256(repr((raw, enabled, unresolved, start)).encode()).hexdigest()
    catalog = SimpleNamespace(_ready=lambda: None, _resources=MappingProxyType({"ItemConfigTable.atb": raw}),
        content_version=version, groups=groups, rules=SimpleNamespace(enabled=enabled),
        unresolved_candidates=("unknown",) if unresolved else (),
        event=SimpleNamespace(start_at=start, end_at=start + timedelta(days=12, hours=1)))
    def active(now):
        if not enabled or not catalog.event.start_at <= now < catalog.event.end_at:
            return False
        if unresolved:
            raise ProtocolError(CANDIDATES_UNRESOLVED, "unresolved", 503)
        return True
    catalog.active = active
    return SimpleNamespace(service=VoteTicketService(possessions, catalog), possessions=possessions, catalog=catalog)


def state(*, daily=0, ordinary=0, enabled=None):
    rows = []
    if daily:
        rows.append({"ItemID": DAILY_ITEM, "Count": daily})
        if enabled is not None:
            rows[-1]["Enabled"] = enabled
    if ordinary:
        rows.append({"ItemID": GROUP, "Count": ordinary})
    return {"bootstrap_schema": 1, "tables": {"possessions": rows}, "unrelated": {"values": [1, 2]}}


def count(snapshot, item_id=DAILY_ITEM):
    return next((row["Count"] for row in snapshot["tables"]["possessions"] if row["ItemID"] == item_id), 0)


def test_saved_ticket_spellings_stay_readable_by_old_saves():
    assert (AVAILABLE, CONSUMED, EXPIRED, UNRESOLVED) == ("available", "consumed", "expired", "unresolved")
    assert (LOCAL_ORIGIN, BASELINE_ORIGIN, UNOBSERVED) == ("local", "baseline", "unobserved")
    assert LEDGER_KEY == "vote_tickets"


def test_first_observation_grants_once_and_same_day_reads_do_not_change_state():
    service = components().service
    initial = state()
    saved = deepcopy(initial)
    observed, changed = service.settle(initial, NOW)
    assert initial == saved and count(observed) == 1 and changed == (DAILY_ITEM,)
    assert observed["vote_tickets"]["ticket_day"] == "2026-11-01"
    assert observed["vote_tickets"]["origin"] == "local"
    again, changed = service.settle(observed, NOW + timedelta(hours=1))
    assert again == observed and changed == () and again is not observed
    assert service.initialize(initial) == initial and "vote_tickets" not in service.initialize(initial)


def test_consumed_daily_ticket_is_not_replenished_same_day_and_ordinary_votes_continue():
    service = components().service
    initial = state(ordinary=5)
    consumed, daily, ordinary = service.consume(initial, GROUP, 3, NOW)
    assert (daily, ordinary) == (1, 2) and count(consumed) == 0 and count(consumed, GROUP) == 3
    assert consumed["vote_tickets"]["status"] == "consumed"
    assert service.settle(consumed, NOW + timedelta(minutes=1)) == (consumed, ())
    second, daily, ordinary = service.consume(consumed, GROUP, 2, NOW + timedelta(minutes=2))
    assert (daily, ordinary) == (0, 2) and count(second, GROUP) == 1
    assert second["vote_tickets"]["last_time"] > consumed["vote_tickets"]["last_time"]
    assert {key: value for key, value in second["vote_tickets"].items() if key != "last_time"} == {
        key: value for key, value in consumed["vote_tickets"].items() if key != "last_time"}
    with pytest.raises(ProtocolError) as error:
        service.consume(second, GROUP, 1, NOW + timedelta(minutes=1))
    assert error.value.code == "vote_ticket_clock_reversed"
    assert count(initial, GROUP) == 5


@pytest.mark.parametrize("consume", [False, True])
def test_jst_four_am_replaces_previous_day_ticket_with_exactly_one(consume):
    service = components().service
    issued, _ = service.settle(state(), NOW)
    if consume:
        issued = service.consume(issued, GROUP, 1, NOW)[0]
    before = datetime(2026, 11, 2, 3, 59, 59, tzinfo=JST)
    assert service.settle(issued, before) == (issued, ())
    after, changed = service.settle(issued, before + timedelta(seconds=1))
    assert changed == (DAILY_ITEM,) and count(after) == 1
    assert after["vote_tickets"]["ticket_day"] == "2026-11-02"
    assert after["vote_tickets"]["status"] == "available"


@pytest.mark.parametrize("moment", BUSINESS_EDGES + CLIENT_CALENDAR)
def test_ticket_day_and_watermark_follow_the_shifted_jst_clock_in_every_representation(moment):
    service = components(start=moment - timedelta(hours=1)).service
    for now in representations(moment):
        observed, changed = service.settle(state(), now)
        assert changed == (DAILY_ITEM,)
        assert (observed["vote_tickets"]["ticket_day"], observed["vote_tickets"]["last_time"]) == (
            shifted_day(now).isoformat(), epoch_milliseconds(now))
        assert service.initialize(observed) == observed


@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_instants_outside_the_client_calendar_are_refused(moment):
    service = components(start=moment - timedelta(hours=1)).service
    for now in representations(moment):
        with pytest.raises(ProtocolError) as error:
            service.settle(state(), now)
        assert (error.value.code, error.value.status) == ("vote_ticket_clock_invalid", 400)
    issued = components().service.settle(state(), NOW)[0]
    issued["vote_tickets"].update(last_time=epoch_milliseconds(moment), ticket_day=shifted_day(moment).isoformat())
    with pytest.raises(ProtocolError) as error:
        components().service.initialize(issued)
    assert (error.value.code, error.value.status) == ("vote_ticket_state_invalid", 503)


def test_absence_does_not_accumulate_tickets_and_observation_is_not_tied_to_group():
    service = components().service
    issued, _ = service.settle(state(), NOW)
    later = NOW + timedelta(days=5)
    refreshed, _ = service.settle(issued, later)
    assert count(refreshed) == 1 and refreshed["vote_tickets"]["ticket_day"] == "2026-11-06"
    switched = components(start=START - timedelta(hours=1)).service
    assert switched.settle(refreshed, later + timedelta(minutes=1)) == (refreshed, ())


def test_group_change_same_day_does_not_issue_another_ticket():
    case = components()
    when = case.catalog.groups[1].vote_start + timedelta(minutes=1)
    consumed, _, _ = case.service.consume(state(), GROUP, 1, when)
    assert case.service.settle(consumed, when + timedelta(minutes=1)) == (consumed, ())
    with pytest.raises(ProtocolError) as error:
        case.service.consume(consumed, "item_vote_016002", 1, when + timedelta(minutes=2))
    assert error.value.code == "possession_insufficient"


@pytest.mark.parametrize("when", [START - timedelta(seconds=1), START + timedelta(days=13)])
def test_inactive_empty_state_stays_empty_without_a_journal(when):
    service = components().service
    initial = state()
    assert service.settle(initial, when) == (initial, ())
    assert "vote_tickets" not in initial


@pytest.mark.parametrize("changes", [{"enabled": False}, {"unresolved": True}])
def test_ineligible_catalog_does_not_create_new_ticket(changes):
    service = components(**changes).service
    assert service.settle(state(), NOW) == (state(), ())


def test_first_legacy_ticket_is_adopted_without_credit_and_preserves_enabled():
    service = components().service
    initial = state(daily=1, ordinary=3, enabled=1)
    observed, changed = service.settle(initial, NOW)
    assert changed == () and observed["tables"] == initial["tables"]
    assert observed["vote_tickets"]["origin"] == "baseline"
    consumed, daily, ordinary = service.consume(observed, GROUP, 1, NOW)
    assert (daily, ordinary) == (1, 0)
    assert consumed["tables"]["possessions"][0]["Enabled"] == 1
    assert service.settle(consumed, NOW + timedelta(hours=1)) == (consumed, ())


def test_undated_ticket_is_preserved_and_diagnosed_outside_activity():
    service = components().service
    initial = state(daily=1)
    assert service.initialize(initial) == initial
    assert service.ticket_status(initial)["reason"] == "legacy_ticket_date_unknown"
    observed, changed = service.settle(initial, START - timedelta(days=3))
    assert count(observed) == 1 and changed == ()
    assert observed["vote_tickets"]["ticket_day"] is None
    assert observed["vote_tickets"]["status"] == "unresolved"
    assert service.settle(observed, START - timedelta(days=1)) == (observed, ())
    adopted, changed = service.settle(observed, NOW)
    assert count(adopted) == 1 and changed == ()
    assert adopted["vote_tickets"]["ticket_day"] == "2026-11-01"
    assert service.ticket_status(adopted)["reason"] is None


def test_disabled_same_day_preserves_ticket_but_known_next_day_expiry_is_removed():
    active = components().service
    disabled = components(enabled=False).service
    issued, _ = active.settle(state(), NOW)
    assert disabled.settle(issued, NOW + timedelta(hours=1)) == (issued, ())
    expired, changed = disabled.settle(issued, NOW + timedelta(days=1))
    assert count(expired) == 0 and changed == (DAILY_ITEM,)
    assert expired["vote_tickets"]["ticket_day"] == "2026-11-01"
    assert expired["vote_tickets"]["status"] == "expired"
    assert disabled.settle(expired, NOW + timedelta(days=2)) == (expired, ())
    restored, changed = active.settle(expired, NOW + timedelta(days=2))
    assert count(restored) == 1 and changed == (DAILY_ITEM,)
    assert restored["vote_tickets"]["ticket_day"] == "2026-11-03"


def test_same_day_disabling_and_reenabling_cannot_reset_consumed_eligibility():
    service = components().service
    consumed = service.consume(state(), GROUP, 1, NOW)[0]
    disabled = components(enabled=False).service.settle(consumed, NOW + timedelta(minutes=1))[0]
    changed = components(start=START - timedelta(hours=1)).service
    assert changed.settle(disabled, NOW + timedelta(minutes=2)) == (consumed, ())


def test_clock_reversal_rejects_without_mutation():
    service = components().service
    issued = service.settle(state(), NOW)[0]
    before = deepcopy(issued)
    with pytest.raises(ProtocolError) as error:
        service.settle(issued, NOW - timedelta(milliseconds=1))
    assert error.value.code == "vote_ticket_clock_reversed" and issued == before


def test_unavailable_ordinary_inventory_rolls_back_daily_qualification_too():
    service = components().service
    initial = state(ordinary=1)
    before = deepcopy(initial)
    with pytest.raises(ProtocolError) as error:
        service.consume(initial, GROUP, 3, NOW)
    assert error.value.code == "possession_insufficient" and initial == before
    assert "vote_tickets" not in initial


@pytest.mark.parametrize("value", [0, -1, True, "1", 0x80000000])
def test_consume_rejects_invalid_counts(value):
    service = components().service
    with pytest.raises(ProtocolError) as error:
        service.consume(state(ordinary=10), GROUP, value, NOW)
    assert error.value.code == "vote_ticket_count_invalid"


@pytest.mark.parametrize("group,when,code", [("item_vote_016002", NOW, "vote_group_closed"),
    (DAILY_ITEM, NOW, "vote_ticket_group_invalid"), (GROUP, START - timedelta(seconds=1), "vote_event_closed"),
    (GROUP, START + timedelta(days=3, hours=1), "vote_group_closed")])
def test_consume_requires_selected_groups_actual_voting_window(group, when, code):
    with pytest.raises(ProtocolError) as error:
        components().service.consume(state(ordinary=10), group, 1, when)
    assert error.value.code == code


@pytest.mark.parametrize("patch", [{"schema_version": 2}, {"last_time": True}, {"ticket_day": "2026-11-02"},
    {"status": "consumed"}, {"origin": "unknown"}, {"extra": 0}, {"last_time": 10 ** 30}])
def test_corrupt_journal_is_not_silently_reset(patch):
    service = components().service
    initial = service.settle(state(), NOW)[0]
    initial["vote_tickets"].update(patch)
    before = deepcopy(initial)
    with pytest.raises(ProtocolError) as error:
        service.initialize(initial)
    assert error.value.code == "vote_ticket_state_invalid" and initial == before


def test_overstacked_legacy_day_tickets_are_not_silently_discarded():
    initial = state(daily=2)
    with pytest.raises(ProtocolError) as error:
        components().service.settle(initial, NOW)
    assert error.value.code == "vote_ticket_state_invalid" and count(initial) == 2


@pytest.mark.parametrize("changes", [{"EffectType": 3}, {"Category": 1333766411}, {"Stack": 999},
    {"ReceiveAction": 1}, {"CanAllGet": 0}, {"EventID": "story"}])
def test_day_ticket_cannot_be_replaced_with_reward_placeholder_or_other_behavior(changes):
    with pytest.raises(ProtocolError) as error:
        components(daily_changes=changes)
    assert error.value.code == "vote_ticket_definition_invalid"


def test_changed_inventory_dependency_is_rejected():
    case = components()
    case.possessions._definitions[DAILY_ITEM] = 999
    assert case.service.status()["ready"] is False
    with pytest.raises(ProtocolError) as error:
        case.service.settle(state(), NOW)
    assert error.value.code == "vote_ticket_dependencies_changed"


def test_readiness_does_not_build_the_possession_status(monkeypatch):
    case = components()
    monkeypatch.setattr(case.possessions, "status", lambda: pytest.fail("Readiness must not build the possession status"))
    assert case.service.status()["ready"] is True
    assert case.service.settle(state(), NOW)[1] == (DAILY_ITEM,)
    case.possessions.issues["item_definitions"] = "unavailable"
    assert case.service.status()["error"] == "vote_ticket_dependencies_changed"


def test_inventory_provider_cannot_modify_unrelated_state(monkeypatch):
    case = components()
    original = case.possessions.credit
    def corrupt(snapshot, grants):
        result = original(snapshot, grants)
        result["unrelated"]["values"].append(3)
        return result
    monkeypatch.setattr(case.possessions, "credit", corrupt)
    initial = state()
    with pytest.raises(ProtocolError) as error:
        case.service.settle(initial, NOW)
    assert error.value.code == "vote_ticket_inventory_invalid"
    assert initial == state()


def test_current_resources_use_real_daily_item_and_shared_inventory(vote_sources):
    catalog = build(vote_sources)
    resolved = build(vote_sources, resolved_rules(catalog, revival_start="2026-11-01T15:00:00+09:00"))
    possessions = PossessionService(Path("."), asset_reader=vote_sources.__getitem__)
    service = VoteTicketService(possessions, resolved)
    issued, changed = service.settle(state(), NOW)
    assert changed == (DAILY_ITEM,) and count(issued) == 1
    assert count(issued, "item_replacement_vote_ticket") == 0
    consumed, daily, ordinary = service.consume(issued, resolved.groups[0].item_id, 1, NOW)
    assert (daily, ordinary) == (1, 0) and count(consumed) == 0
    assert service.status()["ready"] is True

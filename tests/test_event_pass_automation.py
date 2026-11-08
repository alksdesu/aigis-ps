from copy import copy, deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.cli import load_event_pass_rules
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_catalog import EventPassReward
from aigis_ps.event_pass_rules import EventPassAutomationRules
import aigis_ps.event_pass_service as module
from aigis_ps.event_pass_service import (AUTOMATION_KEY, DELIVERY_KEY, HISTORY_NAMESPACE, REBIND_OPERATION,
                                         EventPassService, pass_identity)
from aigis_ps.event_pass_state import (DEFINITION_CHANGED, IDENTITY_UNREPLAYABLE, LEDGER_KEY, MAX_ITEM_BYTES,
                                       RESPONSE_CAPACITY, read_event_passes, rebind_definition)
from aigis_ps.item_summon_tickets import ItemSummonTicketService
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.protocol import RESPONSE_TOO_LARGE
from aigis_ps.rank_reward_state import RESPONSE_CAPACITY as RANK_REWARD_RESPONSE_CAPACITY
from aigis_ps.storage import PlayerProfile, Store
from aigis_ps.summon_tickets import SummonTicketRules
from test_event_pass_service import accept_stored_item, baseline, previous_window_build
from test_quest_service import bundle, request
from aigis_ps.alxml import Column


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
EARLIER_IDENTITY = hashlib.sha256(b"earlier event pass identity").hexdigest()


def reward(identifier, point=10, *, premium=False, kind=0, param=1, item=""):
    return EventPassReward(identifier, point, premium, kind, param, item, identifier - 1)


def memory_catalog(original, entries):
    catalog = copy(original)
    catalog._passes = tuple(entries)
    catalog._by_id = MappingProxyType({entry.item_id: entry for entry in entries})
    catalog._content_version = hashlib.sha256(json.dumps([asdict(entry) for entry in entries],
        sort_keys=True, default=str).encode()).hexdigest()
    return catalog


@pytest.fixture
def factory(bundle, baseline):
    def build(*, count=1, points=10, premium=False, rules=None, status=2, definitions=None):
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        bootstrap.clock = lambda: NOW
        entries = tuple(replace(entry, status=status, start_at=NOW - timedelta(days=1),
            end_at=NOW + timedelta(days=1), time_limit="表示専用：日付ではありません",
            rewards=definitions[index] if definitions is not None else (reward(index * 10 + 1),))
            for index, entry in enumerate(bundle.event_passes.catalog.passes[:count]))
        catalog = memory_catalog(bundle.event_passes.catalog, entries)
        service = EventPassService(bootstrap, bundle.player_state, presents, bundle.possessions, catalog,
                                   automation_rules=rules)
        bootstrap.event_pass_provider = service.response
        bootstrap.present_provider = presents.response
        state = deepcopy(baseline)
        state["sentinel"] = {"keep": [1]}
        amounts = {entry.item_id: points for entry in entries if points}
        if premium:
            amounts.update({entry.premium_item_id: 1 for entry in entries})
        state = bundle.possessions.credit(state, amounts)
        return SimpleNamespace(bundle=bundle, bootstrap=bootstrap, presents=presents, catalog=catalog,
            service=service, entries=entries, state=state,
            profile=PlayerProfile("1", "test-user-key", "test-method-key", state, 0))
    return build


def settle(case, *, state=None, now=NOW, validate=None):
    state = case.state if state is None else state
    return case.service.settle_expired(replace(case.profile, state=state), state, now, validate=validate)


def reset_service(case, *, catalog=None, rules=None):
    case.service = EventPassService(case.bootstrap, case.bundle.player_state, case.presents,
        case.bundle.possessions, catalog or case.catalog,
        automation_rules=case.service.automation_rules if rules is None else rules)
    case.catalog = case.service.catalog
    case.bootstrap.event_pass_provider = case.service.response


def test_one_closed_pass_delivers_once_with_category_seven_and_no_direct_credit(factory):
    case = factory(definitions=((reward(1), reward(2, premium=True)),), premium=True)
    original = deepcopy(case.state)
    updated, issued = settle(case)
    assert issued == {case.entries[0].item_id: [1, 2]}
    assert all(row["NotificationCategory"] == 7 for row in case.presents.rows(updated))
    assert updated["tables"]["possessions"] == original["tables"]["possessions"]
    assert updated["tables"]["crystals"] == original["tables"]["crystals"]
    assert updated["player_state"] == original["player_state"]
    assert case.state == original
    repeated, second = settle(case, state=updated)
    assert second == {} and repeated == updated and repeated is not updated


def test_callback_receives_independent_accumulated_delivery_map(factory):
    case = factory(count=2)
    observed = []
    def validate(candidate, issued):
        observed.append(deepcopy(issued))
        assert len(case.presents.rows(candidate)) == len(observed)
        issued.clear()
    updated, issued = settle(case, validate=validate)
    first, second = (entry.item_id for entry in case.entries)
    assert observed == [{first: [1]}, {first: [1], second: [2]}]
    assert issued == {first: [1], second: [2]}
    assert len(case.presents.rows(updated)) == 2


def test_one_pass_failure_does_not_block_another_pass_that_fits(factory):
    case = factory(count=2, definitions=((reward(1), reward(2)), (reward(11),)))
    filled, _ = case.presents.enqueue(case.state, [{"kind": "unit", "template_id": 2,
        "count": MAX_PRESENTS - 1, "description": "existing"}])
    updated, issued = settle(case, state=filled)
    first, second = (entry.item_id for entry in case.entries)
    assert issued == {second: [MAX_PRESENTS]}
    assert updated[AUTOMATION_KEY]["pending"] == {first: "present_capacity"}
    assert {row["ID"] for row in read_event_passes(updated)} == {second}
    assert len(case.presents.rows(updated)) == MAX_PRESENTS
    assert filled["tables"]["event_pass_status"] == []


def test_full_mailbox_pending_recovers_without_reusing_present_ids(factory):
    case = factory()
    filled, _ = case.presents.enqueue(case.state, [{"kind": "unit", "template_id": 2,
        "count": MAX_PRESENTS, "description": "existing"}])
    pending, issued = settle(case, state=filled)
    assert issued == {} and pending[AUTOMATION_KEY]["pending"] == {
        case.entries[0].item_id: "present_capacity"}
    assert pending["tables"]["presents"] == filled["tables"]["presents"]
    pending["tables"]["presents"].clear()
    delivered, issued = settle(case, state=pending)
    assert issued == {case.entries[0].item_id: [MAX_PRESENTS + 1]}
    assert delivered[AUTOMATION_KEY] == {"schema_version": 1, "pending": {}}


def test_disabled_ticket_pool_is_pending_then_delivers_after_explicit_enable(factory):
    case = factory(definitions=((reward(1, kind=22, item="item_smn_ticket_19144"),),))
    original_tickets = case.presents.item_tickets
    case.presents.item_tickets = ItemSummonTicketService(case.bootstrap, case.bundle.player_state,
        case.presents, original_tickets.pools, original_tickets.catalog, rules=SummonTicketRules((428,)))
    reset_service(case)
    pending, issued = settle(case)
    assert issued == {} and pending[AUTOMATION_KEY]["pending"] == {
        case.entries[0].item_id: "summon_ticket_pool_disabled"}
    assert case.presents.rows(pending) == []
    case.presents.item_tickets = original_tickets
    reset_service(case)
    updated, issued = settle(case, state=pending)
    assert issued == {case.entries[0].item_id: [1]}
    assert updated[AUTOMATION_KEY]["pending"] == {}
    assert case.presents.rows(updated)[0]["ItemID"] == "item_smn_ticket_19144"


@pytest.mark.parametrize("status,code", [(409, "capacity"), (501, "unsupported"),
    (503, RESPONSE_TOO_LARGE), (503, RANK_REWARD_RESPONSE_CAPACITY), (503, RESPONSE_CAPACITY)])
def test_recoverable_validation_errors_become_pending_without_partial_mail(factory, status, code):
    case = factory()
    before = deepcopy(case.state)
    def fail(candidate, issued):
        raise ProtocolError(code, "test", status)
    updated, issued = settle(case, validate=fail)
    assert issued == {}
    assert updated[AUTOMATION_KEY]["pending"] == {case.entries[0].item_id: code}
    expected = deepcopy(before)
    expected[AUTOMATION_KEY] = updated[AUTOMATION_KEY]
    assert updated == expected and case.state == before


def test_response_capacity_keeps_the_spelling_of_saved_pending_reasons():
    assert RESPONSE_CAPACITY == "event_pass_response_capacity"


def test_fatal_error_in_later_pass_aborts_whole_result_and_preserves_input(factory):
    case = factory(count=2)
    before = deepcopy(case.state)
    def fail_second(candidate, issued):
        if len(issued) == 2:
            raise ProtocolError("resource_corrupt", "test", 503)
    with pytest.raises(ProtocolError) as error:
        settle(case, validate=fail_second)
    assert error.value.code == "resource_corrupt"
    assert case.state == before


def test_callback_capacity_only_defers_later_pass(factory):
    case = factory(count=2)
    def limit(candidate, issued):
        if len(issued) == 2:
            raise ProtocolError(RANK_REWARD_RESPONSE_CAPACITY, "test", 503)
    updated, issued = settle(case, validate=limit)
    assert issued == {case.entries[0].item_id: [1]}
    assert updated[AUTOMATION_KEY]["pending"] == {
        case.entries[1].item_id: RANK_REWARD_RESPONSE_CAPACITY}


def test_identity_beyond_the_native_cell_stays_pending_without_failing_settlement(factory, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    case = factory(count=2)
    first, second = (entry.item_id for entry in case.entries)
    original = module.mark_event_pass

    def refuse_first(state, identifier, **kwargs):
        if identifier == first:
            raise ProtocolError(IDENTITY_UNREPLAYABLE, "New event passes need identities that fit the native cell", 503)
        return original(state, identifier, **kwargs)

    monkeypatch.setattr(module, "mark_event_pass", refuse_first)
    updated, issued = settle(case)
    assert list(issued) == [second]
    assert updated[AUTOMATION_KEY]["pending"] == {first: IDENTITY_UNREPLAYABLE}
    assert {row["ID"] for row in read_event_passes(updated)} == {second}
    assert any(first in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)


def test_changed_identity_stays_pending_until_rebound_without_failing_settlement(factory, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    case = factory(count=2, definitions=((reward(1), reward(2, 20)), (reward(11), reward(12, 20))))
    first, second = (entry.item_id for entry in case.entries)
    delivered, _ = settle(case)
    forged = deepcopy(delivered)
    forged[LEDGER_KEY]["passes"][first]["definition"] = EARLIER_IDENTITY
    forged = case.bundle.possessions.credit(forged, {first: 10, second: 10})
    held, issued = settle(case, state=forged)
    assert issued == {second: [3]}
    assert held[AUTOMATION_KEY]["pending"] == {first: DEFINITION_CHANGED}
    assert held[LEDGER_KEY]["passes"][first] == forged[LEDGER_KEY]["passes"][first]
    assert read_event_passes(held)[0] == read_event_passes(forged)[0]
    assert any(first in record.getMessage() and REBIND_OPERATION in record.getMessage()
               and record.levelname == "WARNING" for record in caplog.records)
    rebound = rebind_definition(held, first, pass_identity(case.entries[0]))
    settled, issued = settle(case, state=rebound)
    assert issued == {first: [4]} and settled[AUTOMATION_KEY]["pending"] == {}
    assert read_event_passes(settled)[0]["ReceivedNormalPoint"] == 20


def test_reward_item_beyond_the_native_cell_stays_pending_without_failing_settlement(factory, monkeypatch):
    item = "x" * (MAX_ITEM_BYTES + 1)
    case = factory(count=2, definitions=((reward(1, kind=22, item=item),), (reward(11),)))
    accept_stored_item(monkeypatch, case.presents, item)
    first, second = (entry.item_id for entry in case.entries)
    updated, issued = settle(case)
    assert issued == {second: [1]}
    assert updated[AUTOMATION_KEY]["pending"] == {first: "event_pass_reward_unsupported"}
    assert {row["ID"] for row in read_event_passes(updated)} == {second}


@pytest.mark.parametrize("points", [0, 9])
def test_no_attained_threshold_has_no_mail_or_pending(factory, points):
    case = factory(points=points)
    updated, issued = settle(case)
    assert updated == case.state and issued == {}
    assert AUTOMATION_KEY not in updated


def test_nothing_claimable_clears_obsolete_pending_but_preserves_unknown_history(factory):
    case = factory(points=0)
    state = deepcopy(case.state)
    state[AUTOMATION_KEY] = {"schema_version": 1, "pending": {
        case.entries[0].item_id: "present_capacity", "removed_pass": "unsupported"}}
    updated, issued = settle(case, state=state)
    assert issued == {} and updated[AUTOMATION_KEY]["pending"] == {"removed_pass": "unsupported"}


def test_manual_frontier_is_not_paid_again_by_automatic_settlement(factory, tmp_path):
    case = factory(status=1, definitions=((reward(1), reward(2, point=20)),))
    store = Store(tmp_path / "manual.sqlite3", case.state)
    envelope = request(store, 159, (Column("EPI", "S", (case.entries[0].item_id,)),))
    case.service.claim(envelope, store)
    saved = store.profile()
    assert case.presents.rows(saved.state)[0].get("NotificationCategory", 0) == 0
    state = case.bundle.possessions.credit(saved.state, {case.entries[0].item_id: 10})
    closed = memory_catalog(case.catalog, [replace(case.entries[0], status=2)])
    reset_service(case, catalog=closed)
    updated, issued = settle(case, state=state)
    assert issued == {case.entries[0].item_id: [2]}
    assert [row.get("NotificationCategory", 0) for row in case.presents.rows(updated)] == [0, 7]
    assert read_event_passes(updated)[0]["ReceivedNormalPoint"] == 20


def test_late_premium_only_delivers_previously_locked_premium_rewards(factory):
    case = factory(definitions=((reward(1), reward(2, premium=True)),))
    normal, _ = settle(case)
    state = case.bundle.possessions.credit(normal, {case.entries[0].premium_item_id: 1})
    updated, issued = settle(case, state=state)
    assert issued == {case.entries[0].item_id: [2]}
    assert read_event_passes(updated)[0] == {"ID": case.entries[0].item_id,
        "ReceivedNormalPoint": 10, "ReceivedPremiumPoint": 10}


@pytest.mark.parametrize("position,expected", [("before_start", False), ("at_start", True),
    ("at_end", True), ("after_end", True)])
def test_closed_status_has_start_boundary_and_does_not_use_time_limit(factory, position, expected):
    case = factory()
    entry = case.entries[0]
    now = {"before_start": entry.start_at - timedelta(milliseconds=500), "at_start": entry.start_at,
           "at_end": entry.end_at, "after_end": entry.end_at + timedelta(seconds=1)}[position]
    updated, issued = settle(case, now=now)
    assert bool(issued) is expected
    assert bool(case.presents.rows(updated)) is expected


@pytest.mark.parametrize("shift,expected", [
    (-timedelta(milliseconds=500), False), (timedelta(), False), (timedelta(milliseconds=500), False),
    (timedelta(microseconds=999999), False), (timedelta(seconds=1), True)])
def test_no_closed_status_configuration_requires_a_later_client_second(factory, shift, expected):
    case = factory(rules=EventPassAutomationRules(closed_statuses=()))
    updated, issued = settle(case, now=case.entries[0].end_at + shift)
    assert bool(issued) is expected


def test_automation_versions_of_the_previous_window_rule_stay_derivable(factory):
    case = factory()
    previous = EventPassService(case.bootstrap, case.bundle.player_state, case.presents, case.bundle.possessions,
                                previous_window_build(case.catalog), automation_rules=case.service.automation_rules)
    items = case.presents.item_catalog.content_version
    legacy = case.catalog.legacy_window_content_version
    assert case.service.automation_content_version_for(items, legacy) == previous.automation_content_version
    assert case.service.content_version_for(items, legacy) == previous.content_version
    assert previous.content_version in case.service.replayable_content_versions()


def test_jst_utc_and_naive_jst_represent_the_same_boundary(factory):
    case = factory()
    local = case.entries[0].start_at
    results = [settle(case, now=value) for value in (local, local.astimezone(timezone.utc), local.replace(tzinfo=None))]
    assert results[0] == results[1] == results[2]


def test_disabled_automation_preserves_pending_without_delivery_or_callback(factory):
    case = factory(rules=EventPassAutomationRules(enabled=False))
    state = deepcopy(case.state)
    state[AUTOMATION_KEY] = {"schema_version": 1, "pending": {case.entries[0].item_id: "present_capacity"}}
    updated, issued = settle(case, state=state, validate=lambda *_: pytest.fail("Disabled callback"))
    assert updated == state and updated is not state and issued == {}


def test_automation_rules_have_separate_content_identity_and_reject_hot_changes(factory):
    case = factory()
    manual, automatic = case.service.content_version, case.service.automation_content_version
    other = EventPassService(case.bootstrap, case.bundle.player_state, case.presents, case.bundle.possessions,
        case.catalog, automation_rules=EventPassAutomationRules(enabled=False))
    assert other.content_version == manual and other.automation_content_version != automatic
    case.service.automation_rules = EventPassAutomationRules(enabled=False)
    with pytest.raises(ProtocolError) as error:
        settle(case)
    assert error.value.code == "event_pass_automation_changed"


@pytest.mark.parametrize("pending", [None, [], {"schema_version": True, "pending": {}},
    {"schema_version": 2, "pending": {}}, {"schema_version": 1, "pending": []},
    {"schema_version": 1, "pending": {"": "error"}}, {"schema_version": 1, "pending": {"pass": "bad reason"}},
    {"schema_version": 1, "pending": {"pass": 1}}, {"schema_version": 1, "pending": {}, "extra": 1}])
def test_invalid_pending_state_is_not_silently_reset(factory, pending):
    case = factory()
    state = deepcopy(case.state)
    state[AUTOMATION_KEY] = pending
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        settle(case, state=state)
    assert error.value.code == "event_pass_automation_invalid" and state == before


def test_pending_pass_saved_under_the_earlier_bound_stays_readable(factory):
    case = factory()
    legacy = "x" * (MAX_ITEM_BYTES + 1)
    state = deepcopy(case.state)
    state[AUTOMATION_KEY] = {"schema_version": 1, "pending": {legacy: "event_pass_reward_capacity"}}
    updated, _ = settle(case, state=state)
    assert updated[AUTOMATION_KEY]["pending"][legacy] == "event_pass_reward_capacity"


@pytest.mark.parametrize("values", [{"enabled": 1}, {"enabled": None}, {"enabled": "false"},
    {"closed_statuses": None}, {"closed_statuses": "2"}, {"closed_statuses": [True]},
    {"closed_statuses": [0]}, {"closed_statuses": [1]}, {"closed_statuses": [-1]},
    {"closed_statuses": [2, 2]}, {"closed_statuses": [0x80000000]}, {"closed_statuses": list(range(2, 35))}])
def test_rules_reject_ambiguous_or_unbounded_inputs(values):
    with pytest.raises(ValueError):
        EventPassAutomationRules(**values)


def test_rules_freeze_list_inputs_and_allow_empty_status_set():
    source = [5, 2]
    rules = EventPassAutomationRules(closed_statuses=source)
    source.append(9)
    assert rules.closed_statuses == (2, 5)
    assert EventPassAutomationRules(closed_statuses=[]).closed_statuses == ()
    with pytest.raises(FrozenInstanceError):
        rules.enabled = False


@pytest.mark.parametrize("values", [{}, {"enabled": False}, {"closed_statuses": []},
                                   {"enabled": True, "closed_statuses": [3, 2]}])
def test_cli_loads_explicit_rules_without_merging_defaults(tmp_path, values):
    path = tmp_path / "automation.json"
    path.write_text(json.dumps(values), encoding="utf8")
    assert load_event_pass_rules(path) == EventPassAutomationRules(**values)
    assert load_event_pass_rules(None) == EventPassAutomationRules()


@pytest.mark.parametrize("contents", ['[]', 'null', '{"unknown":1}', '{"enabled":0}',
                                      '{"closed_statuses":[1]}', '{"closed_statuses":[2,2]}'])
def test_cli_bad_configuration_is_rejected(tmp_path, contents):
    path = tmp_path / "invalid.json"
    path.write_text(contents, encoding="utf8")
    with pytest.raises(ValueError):
        load_event_pass_rules(path)


def test_missing_service_cannot_drop_existing_pending_state(bundle, baseline):
    state = deepcopy(baseline)
    for name in (LEDGER_KEY, HISTORY_NAMESPACE, DELIVERY_KEY):
        state.pop(name, None)
    state[AUTOMATION_KEY] = {"schema_version": 1, "pending": {"old_pass": "present_capacity"}}
    original = deepcopy(state)
    unavailable = copy(bundle)
    unavailable.event_passes = None
    with pytest.raises(ProtocolError) as error:
        unavailable.initialize(state)
    assert error.value.code == "event_pass_service_unavailable" and state == original

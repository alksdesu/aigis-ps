from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from aigis_ps import letstd_state
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.letstd_state import (DELIVERED, ENDED, LEDGER_KEY, PENDING, SCHEDULED, SOURCE, arm_group, complete_row,
    complete_rows, defer_row, defer_rows, due_rows, group_record, initialize_letstd, letstd_clock, letstd_time,
    read_letstd, rearm_group, schedule, validate_letstd_grants, validate_letstd_receipt)
from aigis_ps.presents import MAX_PRESENTS


NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
RULES, RESOURCE, DEPENDENCY = "1" * 64, "2" * 64, "3" * 64


def base():
    return {"bootstrap_schema": 1, "tables": {}, "keep": [1]}


def spec(reward_id, definition="a", start=NOW - timedelta(days=1), end=None):
    return {"reward_id": reward_id, "definition": definition * 64, "start_at": letstd_clock(start),
            "end_at": None if end is None else letstd_clock(end)}


def default_specs():
    return [spec(4, "d", end=NOW - timedelta(hours=1)), spec(1), spec(3, "c", start=NOW + timedelta(days=3)),
            spec(2, "b")]


def armed(specs=None, partner="oshirore", group=1, now=NOW, state=None):
    return arm_group(base() if state is None else state, partner, group, default_specs() if specs is None else specs,
                     RULES, RESOURCE, now)


def records(state, partner="oshirore", group=1):
    return group_record(read_letstd(state), partner, group)["rows"]


def failure(action, code="letstd_state_invalid", status=503):
    with pytest.raises(ProtocolError) as error:
        action()
    assert (error.value.code, error.value.status) == (code, status)


def test_absent_ledger_reads_empty_and_initialize_copies_without_creating_it():
    state = base()
    assert read_letstd(state) == {"schema_version": 1, "last_time": None, "partners": {}}
    copied = initialize_letstd(state)
    assert copied == state and copied is not state and LEDGER_KEY not in copied
    for invalid in ({}, {"bootstrap_schema": 2, "tables": {}}, {"bootstrap_schema": 1}, [], None):
        failure(lambda: read_letstd(invalid))


def test_clock_uses_jst_for_naive_instants_and_rejects_other_values():
    assert letstd_clock(NOW) == letstd_clock(NOW.replace(tzinfo=None)) == letstd_clock(NOW.astimezone(timezone.utc))
    assert letstd_clock(datetime(1970, 1, 1, 9, tzinfo=JST)) == 0
    for value in (None, "2026-11-02", 0):
        failure(lambda: letstd_clock(value), "letstd_clock_invalid", 400)


def test_arm_freezes_the_row_set_with_time_states():
    before = base()
    state = armed(state=before)
    assert before == base() and set(state) - set(before) == {"letstd"}
    assert (SOURCE, SCHEDULED, PENDING, ENDED, DELIVERED) == ("local_simulation", "scheduled", "pending", "ended",
                                                              "delivered")
    ledger = read_letstd(state)
    record = group_record(ledger, "oshirore", 1)
    timestamp = letstd_clock(NOW)
    assert ledger["last_time"] == timestamp and state["keep"] == [1]
    assert {key: record[key] for key in record if key != "rows"} == {"source": SOURCE, "armed_at": timestamp,
        "rearmed_at": None, "rules_version": RULES, "resource_sha256": RESOURCE}
    assert list(record["rows"]) == ["1", "2", "3", "4"]
    assert {key: row["status"] for key, row in record["rows"].items()} == {
        "1": PENDING, "2": PENDING, "3": SCHEDULED, "4": ENDED}
    assert record["rows"]["1"] == {"definition": "a" * 64, "start_at": letstd_clock(NOW - timedelta(days=1)),
        "end_at": None, "status": PENDING, "reason": None, "dependency_version": None, "mail_ids": [],
        "delivered_at": None, "via": None, "popup": None, "needs_adaptation": False}
    failure(lambda: armed(state=state), "letstd_group_armed", 409)
    both = armed([], group=2, state=state, now=NOW + timedelta(minutes=1))
    assert records(both, group=2) == {} and records(both) == records(state)
    assert group_record(read_letstd(both), "rlyeh", 1) is None


@pytest.mark.parametrize("arguments", [
    ("td_shiro", 1, [spec(1)], RULES, RESOURCE), ("oshirore", 3, [spec(1)], RULES, RESOURCE),
    ("oshirore", True, [spec(1)], RULES, RESOURCE), ("oshirore", 1, [spec(1), spec(1, "b")], RULES, RESOURCE),
    ("oshirore", 1, [{**spec(1), "extra": 1}], RULES, RESOURCE), ("oshirore", 1, [spec(0)], RULES, RESOURCE),
    ("oshirore", 1, [{**spec(1), "definition": "A" * 64}], RULES, RESOURCE),
    ("oshirore", 1, [{**spec(1), "start_at": 1.5}], RULES, RESOURCE), ("oshirore", 1, (spec(1),), "x", RESOURCE),
    ("oshirore", 1, [spec(1)], RULES, None), ("oshirore", 1, {"1": spec(1)}, RULES, RESOURCE)])
def test_arm_rejects_unverified_specifications(arguments):
    failure(lambda: arm_group(base(), *arguments, NOW))


def test_schedule_releases_due_rows_and_ends_windows_missed_before_their_start():
    tomorrow = NOW + timedelta(days=1)
    specs = [spec(1, start=tomorrow), spec(2, "b", start=tomorrow, end=NOW + timedelta(days=2)),
             spec(3, "c", start=NOW + timedelta(days=5))]
    state = armed(specs)
    record = group_record(read_letstd(state), "oshirore", 1)
    assert due_rows(record, letstd_clock(NOW + timedelta(hours=1))) == []
    assert due_rows(record, letstd_clock(NOW + timedelta(days=1))) == [record["rows"]["1"], record["rows"]["2"]]
    assert schedule(state, "oshirore", 1, NOW + timedelta(hours=1)) == state
    released = schedule(state, "oshirore", 1, NOW + timedelta(days=1))
    assert {key: row["status"] for key, row in records(released).items()} == {"1": PENDING, "2": PENDING, "3": SCHEDULED}
    assert read_letstd(released)["last_time"] == letstd_clock(NOW + timedelta(days=1))
    late = schedule(state, "oshirore", 1, NOW + timedelta(days=3))
    assert {key: row["status"] for key, row in records(late).items()} == {"1": PENDING, "2": ENDED, "3": SCHEDULED}
    failure(lambda: schedule(state, "oshirore", 2, NOW), "letstd_group_unarmed", 409)
    failure(lambda: schedule(released, "oshirore", 1, NOW), "letstd_clock_backwards", 409)
    assert letstd_time(read_letstd(released), NOW + timedelta(days=1)) == letstd_clock(NOW + timedelta(days=1))
    assert letstd_time(read_letstd(base()), NOW - timedelta(days=9)) == letstd_clock(NOW - timedelta(days=9))
    failure(lambda: letstd_time(read_letstd(released), NOW), "letstd_clock_backwards", 409)


def test_defer_records_reason_and_dependency_without_moving_the_clock():
    state = armed()
    deferred = defer_row(state, "oshirore", 1, 1, "summon_ticket_pool_disabled", DEPENDENCY)
    assert records(deferred)["1"]["reason"] == "summon_ticket_pool_disabled"
    assert records(deferred)["1"]["dependency_version"] == DEPENDENCY
    assert read_letstd(deferred)["last_time"] == read_letstd(state)["last_time"]
    batch = defer_rows(state, [("oshirore", 1, 1, "present_capacity"), ("oshirore", 1, 2, "present_capacity")], DEPENDENCY)
    assert [records(batch)[key]["reason"] for key in ("1", "2")] == ["present_capacity", "present_capacity"]
    assert defer_rows(state, [], DEPENDENCY) == state
    for arguments in ((1, 3, "present_capacity", DEPENDENCY), (1, 4, "present_capacity", DEPENDENCY),
                      (1, 9, "present_capacity", DEPENDENCY), (2, 1, "present_capacity", DEPENDENCY),
                      (1, 1, "bad reason", DEPENDENCY), (1, 1, "", DEPENDENCY), (1, 1, "present_capacity", "x"),
                      (1, True, "present_capacity", DEPENDENCY)):
        failure(lambda: defer_row(state, "oshirore", *arguments))
    failure(lambda: defer_rows(state, [["oshirore", 1, 1, "present_capacity"]], DEPENDENCY))


def test_complete_delivers_a_pending_row_once_with_its_receipt():
    state = defer_row(armed(), "oshirore", 1, 1, "present_capacity", DEPENDENCY)
    later = NOW + timedelta(hours=1)
    delivered, grant = complete_row(state, "oshirore", 1, 1, [5, 6], later, 115, False)
    row = records(delivered)["1"]
    assert row == {"definition": "a" * 64, "start_at": letstd_clock(NOW - timedelta(days=1)), "end_at": None,
        "status": DELIVERED, "reason": None, "dependency_version": None, "mail_ids": [5, 6],
        "delivered_at": letstd_clock(later), "via": 115, "popup": False, "needs_adaptation": False}
    assert grant == {"partner": "oshirore", "group": 1, "reward_id": 1, "definition": "a" * 64,
        "delivered_at": letstd_clock(later), "via": 115, "popup": False, "mail_ids": [5, 6]}
    assert validate_letstd_grants([grant]) == [5, 6]
    validate_letstd_receipt(delivered, [grant])
    assert read_letstd(delivered)["last_time"] == letstd_clock(later)
    failure(lambda: complete_row(delivered, "oshirore", 1, 1, [7], later, 115, False))
    failure(lambda: complete_row(delivered, "oshirore", 1, 2, [6], later, 115, False))
    failure(lambda: complete_row(delivered, "oshirore", 1, 3, [7], later, 115, False))
    failure(lambda: complete_row(delivered, "oshirore", 1, 2, [7], NOW, 115, False), "letstd_clock_backwards", 409)
    for mail_ids, via, popup in (([8, 7], 115, False), ([], 115, False), ([0], 115, False), ([7], 117, False),
                                 ([7], True, False), ([7], 116, 1), ((7,), 116, True)):
        failure(lambda: complete_row(delivered, "oshirore", 1, 2, mail_ids, later, via, popup))


def test_batch_completion_returns_ordered_grants_of_one_settlement():
    state = armed()
    delivered, grants = complete_rows(state, [("oshirore", 1, 1, [1]), ("oshirore", 1, 2, [2, 3])], NOW, 116, True)
    assert [grant["reward_id"] for grant in grants] == [1, 2]
    assert validate_letstd_grants(grants) == [1, 2, 3]
    validate_letstd_receipt(delivered, grants)
    for deliveries in ([("oshirore", 1, 2, [1]), ("oshirore", 1, 1, [2])],
                       [("oshirore", 1, 1, [2]), ("oshirore", 1, 2, [1])],
                       [("oshirore", 1, 1, [1, 2]), ("oshirore", 1, 2, [2, 3])], [("oshirore", 1, 1, [1])] * 2, []):
        failure(lambda: complete_rows(state, deliveries, NOW, 116, True))


def test_rearm_merges_only_new_rows_and_changed_undelivered_rows():
    state = defer_row(armed(), "oshirore", 1, 2, "summon_ticket_pool_disabled", DEPENDENCY)
    state, _ = complete_row(state, "oshirore", 1, 1, [1], NOW, 115, False)
    later = NOW + timedelta(days=1)
    updated = [spec(5, "e"), spec(1, "f"), spec(2, "b"), spec(3, "9", start=NOW + timedelta(days=4)),
               spec(4, "d", end=NOW - timedelta(hours=1))]
    rearmed = rearm_group(state, "oshirore", 1, updated, "4" * 64, "5" * 64, later)
    record = group_record(read_letstd(rearmed), "oshirore", 1)
    rows, before = record["rows"], records(state)
    assert list(rows) == ["1", "2", "3", "4", "5"]
    assert rows["1"] == {**before["1"], "needs_adaptation": True}
    assert rows["2"] == before["2"] and rows["4"] == before["4"]
    assert rows["3"]["definition"] == "9" * 64 and rows["3"]["status"] == SCHEDULED
    assert rows["3"]["start_at"] == letstd_clock(NOW + timedelta(days=4))
    assert rows["5"]["status"] == PENDING and rows["5"]["definition"] == "e" * 64
    assert (record["armed_at"], record["rearmed_at"]) == (letstd_clock(NOW), letstd_clock(later))
    assert (record["rules_version"], record["resource_sha256"]) == ("4" * 64, "5" * 64)
    restored = rearm_group(rearmed, "oshirore", 1, default_specs(), RULES, RESOURCE, later)
    assert records(restored)["1"]["needs_adaptation"] is False and records(restored)["1"]["status"] == DELIVERED
    assert records(restored)["5"] == rows["5"]
    removed = rearm_group(restored, "oshirore", 1, [spec(2, "b")], RULES, RESOURCE, later)
    assert records(removed)["1"]["needs_adaptation"] is True and set(records(removed)) == {"1", "2", "3", "4", "5"}
    changed_end = rearm_group(state, "oshirore", 1, [spec(4, "8")], RULES, RESOURCE, later)
    assert records(changed_end)["4"]["status"] == PENDING
    failure(lambda: rearm_group(state, "oshirore", 2, updated, RULES, RESOURCE, later), "letstd_group_unarmed", 409)
    failure(lambda: rearm_group(state, "oshirore", 1, updated, RULES, RESOURCE, NOW - timedelta(days=1)),
            "letstd_clock_backwards", 409)


def two_grants():
    state = armed()
    state = armed([spec(7, "e")], partner="rlyeh", group=2, state=state)
    return complete_rows(state, [("oshirore", 1, 1, [3]), ("oshirore", 1, 2, [4, 5]), ("rlyeh", 2, 7, [9])],
                         NOW, 115, True)


@pytest.mark.parametrize("mutate", [
    lambda grants: grants[0].update(partner="td_shiro"), lambda grants: grants[0].update(group=3),
    lambda grants: grants[0].update(group=True), lambda grants: grants[0].update(reward_id=0),
    lambda grants: grants[0].update(definition="x"), lambda grants: grants[0].update(delivered_at="x"),
    lambda grants: grants[0].update(via=117), lambda grants: grants[0].update(popup=1),
    lambda grants: grants[0].update(mail_ids=[]), lambda grants: grants[0].update(mail_ids=[0]),
    lambda grants: grants[0].update(mail_ids=[3, 3]), lambda grants: grants[0].update(extra=1),
    lambda grants: grants[0].pop("popup"), lambda grants: grants.reverse(), lambda grants: grants.append(dict(grants[0])),
    lambda grants: grants[1].update(via=116), lambda grants: grants[1].update(popup=False),
    lambda grants: grants[1].update(delivered_at=grants[1]["delivered_at"] + 1),
    lambda grants: grants[1].update(mail_ids=[3, 4]), lambda grants: grants[2].update(mail_ids=[2])])
def test_grants_require_ordered_rows_of_one_settlement(mutate):
    grants = deepcopy(two_grants()[1])
    assert validate_letstd_grants(grants) == [3, 4, 5, 9]
    mutate(grants)
    failure(lambda: validate_letstd_grants(grants))


def test_grants_are_bounded_by_the_mailbox():
    grants = [{"partner": "oshirore", "group": 1, "reward_id": 1, "definition": "a" * 64, "delivered_at": 0,
               "via": 115, "popup": False, "mail_ids": list(range(1, MAX_PRESENTS + 1))}]
    assert len(validate_letstd_grants(grants)) == MAX_PRESENTS
    grants.append({**grants[0], "reward_id": 2, "mail_ids": [MAX_PRESENTS + 1]})
    failure(lambda: validate_letstd_grants(grants))
    failure(lambda: validate_letstd_grants(tuple(grants[:1])))


@pytest.mark.parametrize("field,value,shared", [("mail_ids", [2], False), ("definition", "f" * 64, False),
    ("popup", False, True), ("via", 116, True), ("delivered_at", 0, True)])
def test_receipt_must_match_the_permanent_delivery(field, value, shared):
    state, grants = two_grants()
    validate_letstd_receipt(state, grants)
    changed = deepcopy(grants)
    for grant in changed if shared else changed[:1]:
        grant[field] = value
    validate_letstd_grants(changed)
    failure(lambda: validate_letstd_receipt(state, changed))


def test_receipt_for_an_undelivered_row_is_rejected():
    state, grants = two_grants()
    pending = {**grants[0], "reward_id": 3}
    failure(lambda: validate_letstd_receipt(state, [pending]))
    failure(lambda: validate_letstd_receipt(armed(), grants))


def ledger_of(state):
    return state[LEDGER_KEY]


def row_of(state, key="1", partner="oshirore", group="1"):
    return ledger_of(state)["partners"][partner][group]["rows"][key]


@pytest.mark.parametrize("mutate", [
    lambda state: ledger_of(state).update(schema_version=2), lambda state: ledger_of(state).update(last_time="x"),
    lambda state: ledger_of(state).update(last_time=None), lambda state: ledger_of(state).update(partners={}),
    lambda state: ledger_of(state).update(extra=1),
    lambda state: ledger_of(state)["partners"].update(td_shiro=ledger_of(state)["partners"]["oshirore"]),
    lambda state: ledger_of(state)["partners"]["oshirore"].update({"3": ledger_of(state)["partners"]["oshirore"]["1"]}),
    lambda state: ledger_of(state)["partners"].update(rlyeh={}),
    lambda state: ledger_of(state)["partners"]["oshirore"]["1"].update(source="partner_service"),
    lambda state: ledger_of(state)["partners"]["oshirore"]["1"].update(armed_at=ledger_of(state)["last_time"] + 1),
    lambda state: ledger_of(state)["partners"]["oshirore"]["1"].update(rearmed_at=0),
    lambda state: ledger_of(state)["partners"]["oshirore"]["1"].update(rules_version="x"),
    lambda state: ledger_of(state)["partners"]["oshirore"]["1"]["rows"].update({"01": row_of(state)}),
    lambda state: ledger_of(state)["partners"]["oshirore"]["1"]["rows"].update({"0": row_of(state)}),
    lambda state: row_of(state).update(status="claimed"), lambda state: row_of(state).update(extra=1),
    lambda state: row_of(state).update(reason="present_capacity"),
    lambda state: row_of(state).update(dependency_version=DEPENDENCY),
    lambda state: row_of(state).update(reason="bad reason", dependency_version=DEPENDENCY),
    lambda state: row_of(state).update(mail_ids=[9]), lambda state: row_of(state).update(needs_adaptation=True),
    lambda state: row_of(state).update(start_at=ledger_of(state)["last_time"] + 1),
    lambda state: row_of(state).update(deepcopy(row_of(state, "2"))),
    lambda state: row_of(state, "3").update(reason="present_capacity", dependency_version=DEPENDENCY),
    lambda state: row_of(state, "4").update(end_at=None),
    lambda state: row_of(state, "4").update(end_at=ledger_of(state)["last_time"]),
    lambda state: row_of(state, "2").update(mail_ids=[]), lambda state: row_of(state, "2").update(mail_ids=[5, 4]),
    lambda state: row_of(state, "2").update(via=True), lambda state: row_of(state, "2").update(popup=None),
    lambda state: row_of(state, "2").update(reason="present_capacity", dependency_version=DEPENDENCY),
    lambda state: row_of(state, "2").update(delivered_at=row_of(state, "2")["start_at"] - 1),
    lambda state: row_of(state, "2").update(delivered_at=ledger_of(state)["last_time"] + 1)])
def test_ledger_corruptions_are_rejected(mutate):
    state, _ = complete_row(armed(), "oshirore", 1, 2, [4], NOW, 115, False)
    read_letstd(state)
    mutate(state)
    failure(lambda: read_letstd(state))


def test_row_and_mail_capacity_fail_as_retryable_journal_capacity(monkeypatch):
    monkeypatch.setattr(letstd_state, "MAX_LEDGER_ROWS", 4)
    state = armed()
    failure(lambda: armed([spec(1)], group=2, state=state), "letstd_journal_capacity", 409)
    monkeypatch.setattr(letstd_state, "MAX_LEDGER_ROWS", 65535)
    monkeypatch.setattr(letstd_state, "MAX_MAIL_IDS", 3)
    state, _ = complete_row(state, "oshirore", 1, 1, [1, 2], NOW, 115, False)
    failure(lambda: complete_row(state, "oshirore", 1, 2, [3, 4], NOW, 115, False), "letstd_journal_capacity", 409)
    complete_row(state, "oshirore", 1, 2, [3], NOW, 115, False)

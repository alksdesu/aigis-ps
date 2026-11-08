from copy import copy, deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.vote_catalog import CANDIDATES_UNRESOLVED, CATALOG_CHANGED, VoteCatalog
from aigis_ps.vote_expiry import DEPENDENCIES_CHANGED as EXPIRY_DEPENDENCIES_CHANGED, VoteExpiryService
from aigis_ps.vote_quest_tickets import (
    DEFINITION_INVALID, DEPENDENCIES_CHANGED, SETUP_ERRORS, SOURCE_INVALID, TEXT_SOURCES, VoteQuestTickets,
    VoteTicketGrant,
)
from aigis_ps.vote_rules import VoteRules
from test_present_items import item_table
from test_quest_service import bundle
from test_vote_catalog import build, rows, vote_sources


REVIVAL = "2026-11-01T15:00:00+09:00"
BEFORE_REVIVAL = datetime(2026, 11, 1, 14, 59, 59, tzinfo=JST)
TODAY = datetime(2026, 11, 2, 12, tzinfo=JST)
EXPIRED_AT = datetime(2026, 11, 1, 12, tzinfo=JST)
SECOND_WINDOW = datetime(2026, 11, 4, 15, tzinfo=JST)
FIRST, SECOND, FINAL = "item_vote_016001", "item_vote_016002", "item_vote_016004"
SOURCES = ("SystemText.atb", "cfgImplantText.atb")
TEXT_SCHEMA = (("Recode_Index", 1), ("Data_Text", 32))
IMPLANT_SCHEMA = (("_ID", 32), ("_TextID", 1))
RATE_TEXT = "消費カリスマ15につき「闘兵券」を1枚"
RATE_SENTENCE = RATE_TEXT + "入手する事ができます。"
CAP_TEXT = "闘兵券の最大所持数は999"
NOTICE = f"{RATE_SENTENCE}\n{CAP_TEXT}になります。"


def isolated(base):
    possessions = copy(base.possessions)
    presents = copy(base.presents)
    presents.possessions = possessions
    return SimpleNamespace(possessions=possessions, presents=presents)


def quest_tickets(services, catalog, sources):
    expiry = VoteExpiryService(catalog, services.presents, services.possessions)
    return VoteQuestTickets(catalog, services.possessions, expiry, sources.__getitem__)


def notice(sources, old, new):
    changed = dict(sources)
    texts = rows(sources, "SystemText.atb")
    changed_rows = [row for row in texts if old in row["Data_Text"]]
    assert len(changed_rows) == len(TEXT_SOURCES)
    for row in changed_rows:
        row["Data_Text"] = row["Data_Text"].replace(old, new)
    changed["SystemText.atb"] = item_table(texts, TEXT_SCHEMA)
    return changed


def counts(state):
    return {row["ItemID"]: row["Count"] for row in state["tables"]["possessions"]}


def rejected(action):
    with pytest.raises(ProtocolError) as caught:
        action()
    return caught.value.code, caught.value.status


@pytest.fixture(scope="module")
def catalogs(vote_sources):
    return SimpleNamespace(default=build(vote_sources), revival=build(vote_sources, VoteRules(revival_start=REVIVAL)),
        disabled=build(vote_sources, VoteRules(enabled=False, revival_start=REVIVAL)),
        unresolved=build(vote_sources, VoteRules(candidate_policy="strict", revival_start=REVIVAL)))


@pytest.fixture(scope="module")
def services(bundle):
    return isolated(bundle)


@pytest.fixture(scope="module")
def tickets(vote_sources, catalogs, services):
    return SimpleNamespace(**{name: quest_tickets(services, getattr(catalogs, name), vote_sources)
                              for name in ("default", "revival", "disabled", "unresolved")})


@pytest.fixture(scope="module")
def initial(bundle):
    return bundle.player_state.initialize(bundle.bootstrap.initial_state())


def expired(tickets, services, initial, status):
    source = deepcopy(initial)
    if status == "pending":
        source = services.possessions.credit(source, {FIRST: 3})
        source = services.presents.enqueue(source, [{"kind": "gold", "amount": 1, "description": "fixture"}] * MAX_PRESENTS)[0]
    elif status == "settled":
        source = services.possessions.credit(source, {FIRST: 2})
    else:
        source["tables"]["possessions"].append({"ItemID": FIRST, "Count": 0})
    state, _ = tickets.default.expiry.settle(source, EXPIRED_AT)
    assert state["vote_expiry"]["items"][FIRST]["status"] == status
    return state


def failing(code):
    def ready():
        raise ProtocolError(code, "Fixture readiness failure", 503)
    return ready


def synthetic(services, groups, texts, pointers, possessions=None, *, ready=lambda: None):
    possessions = services.possessions if possessions is None else possessions
    sources = {"SystemText.atb": item_table([{"Recode_Index": index, "Data_Text": text}
                                             for index, text in enumerate(texts)], TEXT_SCHEMA),
               "cfgImplantText.atb": item_table([{"_ID": name, "_TextID": index} for name, index in pointers], IMPLANT_SCHEMA)}
    catalog = SimpleNamespace(_ready=ready, groups=groups, content_version="c" * 64,
                              resource_hashes={name: hashlib.sha256(raw).hexdigest() for name, raw in sources.items()})
    expiry = SimpleNamespace(_ready=lambda: None, catalog=catalog, possessions=possessions, content_version="e" * 64)
    return VoteQuestTickets(catalog, possessions, expiry, sources.__getitem__)


def test_vote_notice_states_rate_cap_and_contiguous_ticket_windows(tickets):
    provider = tickets.default
    assert (provider.rate, provider.cap) == (15, 999)
    assert dict(provider.text_rows) == {"SystemTextVoteInfomation": 1435, "SystemTextVoteInfomation5": 1431}
    status = provider.status()
    assert ([status[key] for key in ("ready", "error", "rate", "tickets_per_rate", "cap")]
            == [True, None, 15, 1, 999])
    windows = status["groups"]
    assert [(window["group_id"], window["item_id"], window["final"]) for window in windows] == [
        (group_id, f"item_vote_016{group_id:03}", group_id == 4) for group_id in range(1, 5)]
    assert all(left["dispersion_end"] == right["dispersion_start"] for left, right in zip(windows, windows[1:]))
    assert (windows[0]["dispersion_start"], windows[-1]["dispersion_end"]) == (
        "2026-08-27T15:00:00+09:00", "2026-09-10T11:00:00+09:00")
    assert all(window["dispersion_start"] == window["source_dispersion_start"]
               and window["dispersion_end"] == window["source_dispersion_end"] for window in windows)
    assert status["source_hashes"] == {name: provider.catalog.resource_hashes[name] for name in SOURCES}
    assert json.loads(json.dumps(status)) == status


def test_default_rules_keep_the_ended_sixteenth_windows_closed_today(tickets, initial):
    assert tickets.default.ticket_group(TODAY) is None
    assert tickets.default.evaluate(initial, charisma_per_run=30, runs=1, now=TODAY) == VoteTicketGrant(None, 0, "no_window")
    status = tickets.default.status(TODAY)
    assert (status["now"], status["current_group"], status["unavailable_reason"]) == (
        "2026-11-02T12:00:00+09:00", None, "no_window")


def test_revival_shifts_every_ticket_window_and_keeps_the_source_window(tickets):
    original, shifted = tickets.default.status()["groups"], tickets.revival.status()["groups"]
    offset = datetime(2026, 11, 1, 15, tzinfo=JST) - datetime(2026, 8, 27, 15, tzinfo=JST)
    for before, after in zip(original, shifted, strict=True):
        for key in ("dispersion_start", "dispersion_end"):
            assert datetime.fromisoformat(after[key]) == datetime.fromisoformat(before[key]) + offset
            assert after["source_" + key] == before["source_" + key] == before[key]
    assert (shifted[0]["dispersion_start"], shifted[-1]["dispersion_end"]) == (
        "2026-11-01T15:00:00+09:00", "2026-11-15T11:00:00+09:00")


@pytest.mark.parametrize("moment,group_id", [(BEFORE_REVIVAL, None),
    (datetime(2026, 11, 1, 15, tzinfo=JST), 1), (datetime(2026, 11, 4, 14, 59, 59, tzinfo=JST), 1),
    (SECOND_WINDOW, 2), (datetime(2026, 11, 4, 6, tzinfo=timezone.utc), 2), (datetime(2026, 11, 7, 15), 3),
    (datetime(2026, 11, 10, 15, tzinfo=JST), 4), (datetime(2026, 11, 15, 10, 59, 59, tzinfo=JST), 4),
    (datetime(2026, 11, 15, 11, tzinfo=JST), None)])
def test_first_ascending_group_owns_each_half_open_dispersion_window(tickets, initial, moment, group_id):
    group = tickets.revival.ticket_group(moment)
    assert (None if group is None else group.group_id) == group_id
    expected = (VoteTicketGrant(None, 0, "no_window") if group_id is None
                else VoteTicketGrant(f"item_vote_016{group_id:03}", 1, "issued"))
    assert tickets.revival.evaluate(initial, charisma_per_run=15, runs=1, now=moment) == expected


@pytest.mark.parametrize("charisma,count,reason", [(14, 0, "below_rate"), (15, 1, "issued"), (29, 1, "issued"),
                                                   (30, 2, "issued"), (0, 0, "zero_charisma")])
def test_each_run_floors_its_charisma_by_the_notice_rate(tickets, initial, charisma, count, reason):
    assert tickets.revival.evaluate(initial, charisma_per_run=charisma, runs=1, now=TODAY) == VoteTicketGrant(FIRST, count, reason)


def test_runs_multiply_floored_tickets_without_pooling_remainders(tickets, initial):
    evaluate = tickets.revival.evaluate
    grant = evaluate(initial, charisma_per_run=25, runs=3, now=TODAY)
    assert grant == VoteTicketGrant(FIRST, 3, "issued")
    with pytest.raises(FrozenInstanceError):
        grant.count = 5
    assert evaluate(initial, charisma_per_run=14, runs=3, now=TODAY) == VoteTicketGrant(FIRST, 0, "below_rate")
    assert evaluate(initial, charisma_per_run=0, runs=3, now=TODAY) == VoteTicketGrant(FIRST, 0, "zero_charisma")
    assert evaluate(initial, charisma_per_run=0x7FFFFFFF, runs=15, now=TODAY).count == 0x7FFFFFFF // 15 * 15


def test_evaluation_never_changes_state_or_uses_the_vote_event_window(tickets, initial, monkeypatch):
    monkeypatch.setattr(VoteCatalog, "active", lambda self, now: pytest.fail("Ticket windows follow each group's dispersion"))
    state = tickets.revival.possessions.credit(initial, {FIRST: 998})
    before = deepcopy(state)
    assert tickets.revival.evaluate(state, charisma_per_run=30, runs=1, now=TODAY) == VoteTicketGrant(FIRST, 2, "issued")
    status = tickets.revival.status(TODAY, state=state)
    assert (status["current_group"], status["unavailable_reason"], status["state_error"]) == (
        {"group_id": 1, "item_id": FIRST}, None, None)
    assert state == before


@pytest.mark.parametrize("status,forced", [("pending", "vote_expiry_inventory_changed"),
    ("settled", "vote_expiry_already_closed"), ("closed", "vote_expiry_already_closed")])
def test_expiry_record_closes_only_its_group_and_keeps_expiry_inventory_valid(tickets, services, initial, status, forced):
    state = expired(tickets, services, initial, status)
    before = deepcopy(state)
    provider = tickets.revival
    assert provider.evaluate(state, charisma_per_run=30, runs=1, now=TODAY) == VoteTicketGrant(FIRST, 0, "vote_ticket_closed")
    assert provider.expiry.initialize(state) == state
    assert provider.status(TODAY, state=state)["unavailable_reason"] == "vote_ticket_closed"
    assert rejected(lambda: provider.grant(state, FIRST, 1)) == ("vote_ticket_closed", 409)
    assert provider.evaluate(state, charisma_per_run=30, runs=1, now=SECOND_WINDOW) == VoteTicketGrant(SECOND, 2, "issued")
    assert state == before
    assert rejected(lambda: provider.expiry.initialize(services.possessions.credit(state, {FIRST: 1}))) == (forced, 503)


def test_disabled_rules_and_unresolved_candidates_block_open_windows(tickets, initial):
    for provider, reason in ((tickets.disabled, "disabled"), (tickets.unresolved, "candidates_unresolved")):
        assert provider.ticket_group(TODAY).item_id == FIRST
        assert provider.evaluate(initial, charisma_per_run=30, runs=1, now=TODAY) == VoteTicketGrant(None, 0, reason)
        status = provider.status(TODAY, state=initial)
        assert (status["current_group"]["item_id"], status["unavailable_reason"]) == (FIRST, reason)
    assert tickets.unresolved.catalog.unresolved_candidates and not tickets.revival.catalog.unresolved_candidates


def test_unresolved_candidates_matter_only_inside_an_open_window(tickets, initial):
    for provider, reason in ((tickets.disabled, "disabled"), (tickets.unresolved, "no_window")):
        assert provider.evaluate(initial, charisma_per_run=30, runs=1, now=BEFORE_REVIVAL) == VoteTicketGrant(None, 0, reason)
        status = provider.status(BEFORE_REVIVAL, state=initial)
        assert (status["current_group"], status["unavailable_reason"]) == (None, reason)


def test_assembled_server_vote_services_supply_the_provider(bundle, initial):
    votes = bundle.votes
    if votes is None or votes.transactions is None or bundle.native_cdn is None:
        pytest.skip("The assembled server needs vote transactions and its effective resource reader")
    provider = VoteQuestTickets(votes.catalog, bundle.possessions, votes.transactions.expiry,
                                lambda name: bundle.native_cdn.assets.read(name)[0])
    assert (provider.rate, provider.cap, provider.catalog.rules.revival_start) == (15, 999, None)
    assert provider.evaluate(initial, charisma_per_run=30, runs=1, now=TODAY) == VoteTicketGrant(None, 0, "no_window")
    assert provider.status(TODAY, state=initial)["unavailable_reason"] == "no_window"


@pytest.mark.parametrize("name", SOURCES)
@pytest.mark.parametrize("change", ["digest", "missing", "not_bytes"])
def test_sources_must_match_the_vote_catalog_snapshot(tickets, vote_sources, name, change):
    def reader(requested):
        raw = vote_sources[requested]
        if requested != name:
            return raw
        if change == "missing":
            raise FileNotFoundError(requested)
        return raw + b"\0" if change == "digest" else bytearray(raw)
    provider = tickets.default
    assert rejected(lambda: VoteQuestTickets(provider.catalog, provider.possessions, provider.expiry, reader)) == (
        "vote_quest_ticket_source_invalid", 503)


def test_rate_comes_from_the_notice_and_the_catalog_snapshot(bundle, vote_sources, tickets, initial):
    changed = notice(vote_sources, RATE_TEXT, RATE_TEXT.replace("15", "20"))
    provider = tickets.revival
    assert rejected(lambda: VoteQuestTickets(provider.catalog, provider.possessions, provider.expiry, changed.__getitem__)) == (
        "vote_quest_ticket_source_invalid", 503)
    shifted = quest_tickets(isolated(bundle), build(changed, VoteRules(revival_start=REVIVAL)), changed)
    assert shifted.rate == 20 and shifted.content_version != provider.content_version
    assert [shifted.evaluate(initial, charisma_per_run=charisma, runs=1, now=TODAY).count
            for charisma in (19, 20, 39, 40)] == [0, 1, 1, 2]


@pytest.mark.parametrize("old,new", [("「闘兵券」を1枚", "「闘兵券」を2枚"), (CAP_TEXT, "闘兵券の最大所持数は998"),
    (RATE_TEXT, RATE_TEXT.replace("15", "0")), (RATE_TEXT, RATE_TEXT.replace("15", "１５")),
    (RATE_SENTENCE, ""), (RATE_TEXT, RATE_TEXT * 2), (CAP_TEXT, CAP_TEXT * 2)])
def test_changed_notice_contract_is_rejected(bundle, vote_sources, old, new):
    changed = notice(vote_sources, old, new)
    assert rejected(lambda: quest_tickets(isolated(bundle), build(changed), changed)) == (
        "vote_quest_ticket_definition_invalid", 503)


def test_synthetic_notices_require_one_agreeing_text_per_pointer(bundle, catalogs):
    services, groups = isolated(bundle), catalogs.default.groups
    pointers = tuple(zip(TEXT_SOURCES, (0, 1)))
    provider = synthetic(services, groups, (NOTICE, NOTICE), pointers)
    assert (provider.rate, provider.cap, dict(provider.text_rows)) == (15, 999, dict(pointers))
    definition = [((NOTICE, NOTICE.replace("15", "16")), pointers, groups), ((NOTICE, NOTICE), pointers, ()),
                  ((NOTICE, NOTICE), pointers, (groups[0], groups[0])),
                  ((NOTICE, NOTICE), pointers, (replace(groups[0], item_id="item_10000000"),)),
                  ((NOTICE, NOTICE), pointers, (replace(groups[0], item_id="unknown_vote_ticket"),))]
    source = [((NOTICE,), pointers, groups), ((NOTICE, NOTICE), pointers[:1], groups),
              ((NOTICE, NOTICE), (*pointers, (TEXT_SOURCES[0], 1)), groups),
              ((NOTICE, NOTICE), ((TEXT_SOURCES[0], -1), pointers[1]), groups)]
    for code, cases in (("vote_quest_ticket_definition_invalid", definition), ("vote_quest_ticket_source_invalid", source)):
        for texts, links, members in cases:
            assert rejected(lambda: synthetic(services, members, texts, links)) == (code, 503)


def test_stated_cap_must_be_the_verified_capacity_even_when_stacks_agree(bundle, catalogs):
    lowered = NOTICE.replace(CAP_TEXT, CAP_TEXT.replace("999", "998"))
    matching = SimpleNamespace(stack=lambda item_id, stored=False: 998)
    pointers = tuple(zip(TEXT_SOURCES, (0, 1)))
    for possessions in (matching, None):
        assert rejected(lambda: synthetic(isolated(bundle), catalogs.default.groups, (lowered, lowered), pointers,
                                          possessions)) == ("vote_quest_ticket_definition_invalid", 503)


def test_first_ascending_group_wins_where_ticket_windows_overlap(bundle, catalogs):
    first, second, *later = catalogs.default.groups
    widened = replace(second, dispersion_start=first.dispersion_start)
    provider = synthetic(isolated(bundle), (*reversed(later), widened, first), (NOTICE, NOTICE),
                         tuple(zip(TEXT_SOURCES, (0, 1))))
    assert [group.group_id for group in (provider.ticket_group(first.dispersion_start),
                                         provider.ticket_group(first.dispersion_end))] == [first.group_id, second.group_id]


def test_manual_grant_caps_at_the_ticket_stack_and_reports_the_discard(tickets, services, initial):
    state = services.possessions.credit(initial, {FIRST: 995})
    before = deepcopy(state)
    updated, discarded = tickets.default.grant(state, FIRST, 10)
    assert discarded == {FIRST: 6} and counts(updated)[FIRST] == 999 and state == before
    assert tickets.default.expiry.initialize(updated) == updated
    fresh, nothing = tickets.default.grant(initial, FINAL, 3)
    assert nothing == {} and counts(fresh)[FINAL] == 3


@pytest.mark.parametrize("item_id,count,code", [("item_10000000", 1, "vote_quest_ticket_item_invalid"),
    ("gift_0001_quartz", 1, "vote_quest_ticket_item_invalid"), (None, 1, "vote_quest_ticket_item_invalid"),
    (FIRST, 0, "vote_quest_ticket_count_invalid"), (FIRST, -1, "vote_quest_ticket_count_invalid"),
    (FIRST, True, "vote_quest_ticket_count_invalid"), (FIRST, 2 ** 31, "vote_quest_ticket_count_invalid")])
def test_manual_grant_rejects_unknown_items_and_counts(tickets, initial, item_id, count, code):
    assert rejected(lambda: tickets.default.grant(initial, item_id, count)) == (code, 400)


@pytest.mark.parametrize("charisma,runs", [(-1, 1), (True, 1), (2 ** 31, 1), (1.5, 1), (None, 1),
                                           (15, 0), (15, True), (15, -1), (15, 2 ** 31), (0x7FFFFFFF, 16)])
def test_invalid_quest_inputs_are_server_errors(tickets, initial, charisma, runs):
    assert rejected(lambda: tickets.default.evaluate(initial, charisma_per_run=charisma, runs=runs, now=TODAY)) == (
        "vote_quest_ticket_request_invalid", 503)


def test_invalid_clock_is_a_server_error(tickets, initial):
    for action in (lambda: tickets.revival.ticket_group("2026-11-02"), lambda: tickets.revival.status("2026-11-02"),
                   lambda: tickets.revival.evaluate(initial, charisma_per_run=15, runs=1, now="2026-11-02")):
        assert rejected(action) == ("vote_quest_ticket_clock_invalid", 503)


def test_corrupted_expiry_state_is_not_reported_as_closed(tickets, initial):
    broken = deepcopy(initial)
    broken["vote_expiry"] = {"schema_version": 2, "items": {}}
    assert rejected(lambda: tickets.revival.evaluate(broken, charisma_per_run=30, runs=1, now=TODAY)) == (
        "vote_expiry_state_invalid", 503)
    assert tickets.default.evaluate(broken, charisma_per_run=30, runs=1, now=TODAY) == VoteTicketGrant(None, 0, "no_window")
    status = tickets.revival.status(TODAY, state=broken)
    assert (status["unavailable_reason"], status["state_error"]) == (None, "vote_expiry_state_invalid")


def test_dependency_mismatch_and_later_changes_are_rejected(bundle, vote_sources, catalogs, initial):
    services, other = isolated(bundle), isolated(bundle)
    expiry = VoteExpiryService(catalogs.default, services.presents, services.possessions)
    for catalog, possessions in ((catalogs.revival, services.possessions), (catalogs.default, other.possessions),
                                 (None, services.possessions)):
        assert rejected(lambda: VoteQuestTickets(catalog, possessions, expiry, vote_sources.__getitem__)) == (
            "vote_quest_ticket_dependencies_changed", 503)
    provider = VoteQuestTickets(catalogs.default, services.possessions, expiry, vote_sources.__getitem__)
    provider.catalog = catalogs.revival
    assert provider.status()["error"] == "vote_quest_ticket_dependencies_changed"
    assert rejected(lambda: provider.evaluate(initial, charisma_per_run=15, runs=1, now=TODAY)) == (
        "vote_quest_ticket_dependencies_changed", 503)
    provider.catalog = catalogs.default
    assert provider.status()["ready"] is True
    rows_before, provider.text_rows = provider.text_rows, {}
    assert provider.status()["error"] == "vote_quest_ticket_dependencies_changed"
    provider.text_rows = rows_before
    assert provider.status()["ready"] is True
    services.possessions.asset_sha256 = "f" * 64
    assert rejected(lambda: provider.grant(initial, FIRST, 1)) == ("vote_expiry_dependencies_changed", 503)
    assert provider.status()["error"] == "vote_expiry_dependencies_changed"


def test_setup_errors_name_every_way_a_provider_build_fails(bundle, vote_sources, catalogs):
    services, groups, pointers = isolated(bundle), catalogs.default.groups, tuple(zip(TEXT_SOURCES, (0, 1)))
    mismatched = VoteExpiryService(catalogs.default, services.presents, services.possessions)
    replaced = copy(catalogs.default)
    replaced_expiry = VoteExpiryService(replaced, services.presents, services.possessions)
    replaced.candidates = ()
    drifted = isolated(bundle)
    drifted_expiry = VoteExpiryService(catalogs.default, drifted.presents, drifted.possessions)
    drifted.possessions.asset_sha256 = "f" * 64
    builds = {
        SOURCE_INVALID: lambda: synthetic(services, groups, (NOTICE,), pointers),
        DEFINITION_INVALID: lambda: synthetic(services, (), (NOTICE, NOTICE), pointers),
        DEPENDENCIES_CHANGED: lambda: VoteQuestTickets(catalogs.revival, services.possessions, mismatched,
                                                       vote_sources.__getitem__),
        CATALOG_CHANGED: lambda: VoteQuestTickets(replaced, services.possessions, replaced_expiry,
                                                  vote_sources.__getitem__),
        CANDIDATES_UNRESOLVED: lambda: synthetic(services, groups, (NOTICE, NOTICE), pointers,
                                                 ready=failing(CANDIDATES_UNRESOLVED)),
        EXPIRY_DEPENDENCIES_CHANGED: lambda: VoteQuestTickets(catalogs.default, drifted.possessions, drifted_expiry,
                                                              vote_sources.__getitem__)}
    assert sorted(builds) == sorted(SETUP_ERRORS)
    for code, attempt in builds.items():
        assert rejected(attempt) == (code, 503)


def test_provider_reads_sources_once_and_versions_deterministically(tickets, vote_sources, initial):
    reads = []

    def reader(name):
        reads.append(name)
        return vote_sources[name]
    original = tickets.revival
    provider = VoteQuestTickets(original.catalog, original.possessions, original.expiry, reader)
    assert sorted(reads) == sorted(SOURCES)
    provider.status(TODAY, state=initial)
    provider.ticket_group(TODAY)
    provider.evaluate(initial, charisma_per_run=15, runs=1, now=TODAY)
    assert sorted(reads) == sorted(SOURCES)
    assert provider.content_version == original.content_version != tickets.default.content_version
    with pytest.raises(TypeError):
        provider.text_rows["SystemTextVoteInfomation"] = 0


@pytest.mark.parametrize("item_id,count,reason", [(None, 1, "issued"), (FIRST, 0, "issued"), (FIRST, 0, "no_window"),
    (None, 0, "below_rate"), (FIRST, -1, "below_rate"), (FIRST, True, "issued"), (FIRST, 0, "unknown"),
    ("", 0, "zero_charisma"), (FIRST, 2 ** 31, "issued"), (FIRST, 1, "vote_ticket_closed")])
def test_grant_values_keep_item_count_and_reason_consistent(item_id, count, reason):
    with pytest.raises(ValueError):
        VoteTicketGrant(item_id, count, reason)

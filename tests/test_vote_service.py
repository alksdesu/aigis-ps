from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.local_present_templates import LOCAL_TEMPLATE_CARDS
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.startup_diagnostics import StartupDiagnostics
from aigis_ps.vote_expiry import ASSAULT_KEY, ASSAULT_NOTIFICATION_KEY, NOTIFICATION_KEY
from aigis_ps.vote_rules import VoteRules
from aigis_ps.vote_service import VoteService
from aigis_ps.vote_state import record_vote, vote_history_response
from test_event_pass_provider import save_filter
from test_evolution_integration import post
from test_quest_service import bundle, request
from test_vote_catalog import CHOICES_WITH_LOCAL_ALTERNATIVES, vote_sources, build, resolved_rules


REVIVAL = "2026-11-01T15:00:00+09:00"
NOW = datetime(2026, 11, 1, 16, tzinfo=JST)
ASSAULT_BATTLES = "Assault battles"
CANDIDATE_COUNT, GROUP_CANDIDATES = 956, (318, 319, 319)
EXPECTED_FIELDS = {
    "VEC": (("RequiredRank", "I"), ("DateOpen", "S"), ("DateClose", "S"),
            ("DateOpenResult", "S"), ("DateCloseResult", "S"), ("DateOpenAnnouncement", "S"),
            ("DateCloseAnnouncement", "S"), ("VoteLimitCount", "I"), ("EarnedVotes", "I")),
    "VGC": (("GroupID", "I"), ("ItemID", "S"), ("DateOpenDispersion", "S"),
            ("DateCloseDispersion", "S"), ("DateOpenVote", "S"), ("DateCloseVote", "S"), ("Final", "I")),
    "VCC": (("CardID", "I"), ("GroupID", "I"), ("Count", "I")),
    "VRC": (("GroupID", "I"), ("Count", "I"), ("Repeat", "I"), ("ItemID", "S"),
            ("GiftType", "I"), ("Amount", "I"), ("SummonID", "I"), ("CardID", "I"),
            ("UnitLevel", "I"), ("SkillLevel", "I"), ("ClassChange", "I"), ("Likable", "I"), ("Cost", "I")),
}


@pytest.fixture(scope="module")
def catalogs(vote_sources):
    default = build(vote_sources)
    return SimpleNamespace(default=default,
        unresolved=build(vote_sources, VoteRules(candidate_policy="strict", revival_start=REVIVAL)),
        resolved=build(vote_sources, resolved_rules(default, revival_start=REVIVAL)),
        disabled=build(vote_sources, VoteRules(enabled=False, revival_start=REVIVAL)))


@pytest.fixture
def make_case(bundle, tmp_path, catalogs):
    counter = 0
    def create(catalog=None, *, now=NOW, state=None):
        nonlocal counter
        counter += 1
        bootstrap = copy(bundle.bootstrap)
        clock = SimpleNamespace(now=now)
        bootstrap.clock = lambda: clock.now
        service = VoteService(bootstrap, bundle.player_state, catalog or catalogs.resolved)
        bootstrap.vote_provider = service.response
        initial = bundle.player_state.initialize(bootstrap.initial_state()) if state is None else state
        initial = service.initialize(initial)
        directory = tmp_path / str(counter)
        def application():
            return create_app(directory, initial_state=initial, state_initializer=service.initialize,
                handlers=bootstrap.handlers() | service.handlers())
        return SimpleNamespace(service=service, bootstrap=bootstrap, clock=clock, initial=initial,
                               app=application(), reopen=application)
    return create


def table_rows(table):
    return [dict(zip((column.name for column in table.children), values))
            for values in zip(*(column.values for column in table.children))]


def assert_empty_tables(tables):
    assert set(tables) == {"SYSTEM", "VEC", "VGC", "VCC", "VRC"}
    for name, fields in EXPECTED_FIELDS.items():
        assert tables[name] == Container(name, tuple(Column(field, kind, ()) for field, kind in fields))


def altered_request(store, overrides):
    registry = ProtocolRegistry.load()
    wire = registry.by_registration[55]
    account = store.profile()
    common = {"UI": ("S", account.player_id), "UK": ("S", account.user_key),
        "MK": ("S", account.method_key), "CT": ("I", 1000), "PFN": ("S", "dmm-r18"),
        "PlatformCode": ("I", 1), "GPVI": ("S", "unkown"), "GPOT": ("S", "unkown")}
    common.update(overrides)
    xml = dumps(Container(children=tuple(Column(name, kind, (value,)) for name, (kind, value) in common.items())))
    xml = prepare_request(xml, wire.st)
    return registry.decode(wire.route, encode_wire(xml, wire.request_key))


def test_default_historical_schedule_returns_four_typed_empty_tables(make_case, catalogs):
    case = make_case(catalogs.default)
    before = case.app.state.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        envelope = request(case.app.state.store, 55, ())
        wire, tables = post(client, envelope)
        assert_empty_tables(tables)
        assert post(client, envelope)[0] == wire
    assert case.app.state.store.profile() == before
    assert set(case.service.handlers()) == {55}
    assert case.service.status()["native_tested"] is False


def test_unresolved_revival_is_503_without_grant_or_debit(make_case, catalogs, bundle):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state = bundle.possessions.credit(state, {catalogs.unresolved.groups[0].item_id: 21})
    case = make_case(catalogs.unresolved, state=state)
    before = case.app.state.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        result = post(client, request(case.app.state.store, 55, ()), expected=503)
    assert result.json()["error"]["code"] == "vote_candidates_unresolved"
    assert case.app.state.store.profile() == before


def test_complete_revival_configuration_maps_local_votes_and_preserves_assets(make_case, catalogs, bundle):
    catalog = catalogs.resolved
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    first = catalog.candidates[0]
    other = next(candidate for candidate in catalog.candidates if candidate.group_id == 2)
    for candidate, count in ((first, 23), (other, 100)):
        group = catalog.groups_by_id[candidate.group_id]
        state = record_vote(state, item_id=group.item_id, group_id=group.group_id, card_id=candidate.card_id,
                            count=count, definition=catalog.content_version)
    state = record_vote(state, item_id="retired_vote_ticket", group_id=91, card_id=1,
                        count=999, definition="b" * 64)
    state = bundle.possessions.credit(state, {catalog.groups[0].item_id: 27})
    case = make_case(state=state)
    before = case.app.state.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, tables = post(client, request(case.app.state.store, 55, ()))
        assert set(tables) == {"SYSTEM", "VEC", "VGC", "VCC", "VRC"}
        assert {name: len(table_rows(tables[name])) for name in EXPECTED_FIELDS} == {
            "VEC": 1, "VGC": 4, "VCC": CANDIDATE_COUNT, "VRC": 52}
        for name, fields in EXPECTED_FIELDS.items():
            assert tuple((column.name, column.type.value) for column in tables[name].children) == fields
        candidates = table_rows(tables["VCC"])
        assert {row["CardID"]: row["Count"] for row in candidates if row["Count"]} == {
            first.card_id: 23, other.card_id: 100}
        assert tuple(sum(row["GroupID"] == group for row in candidates) for group in (1, 2, 3, 4)) == (*GROUP_CANDIDATES, 0)
        assert [row["ItemID"] for row in table_rows(tables["VGC"])] == [
            "item_vote_016001", "item_vote_016002", "item_vote_016003", "item_vote_016004"]
        rewards = table_rows(tables["VRC"])
        assert {(row["GroupID"], row["Count"], row["Repeat"]) for row in rewards if row["Repeat"]} == {
            (1, 200, 100), (2, 200, 100), (3, 200, 100), (4, 300, 100)}
        assert sum(row["Amount"] == 5 and row["ItemID"] in ("gift_0001_quartz", "gift_0002_ruby") for row in rewards) == 5
        assert {row["CardID"] for row in rewards if row["CardID"]} >= {60, 2871}
        _, startup = post(client, request(case.app.state.store, 137, ()))
        assert startup["PVH"] == vote_history_response(state)
        assert case.app.state.store.profile() == before
    reopened = case.reopen()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, request(reopened.state.store, 55, ()))[0] == wire
        assert post(client, request(reopened.state.store, 137, ()))[1]["PVH"] == vote_history_response(state)
    assert reopened.state.store.profile() == before


@pytest.mark.parametrize("offset,visible", [(-1, False), (0, True), (1, True)])
def test_announcement_start_is_inclusive_and_native_dates_use_jst(make_case, catalogs, offset, visible):
    event = catalogs.resolved.event
    now = (event.announcement_start + timedelta(seconds=offset)).astimezone(timezone.utc)
    case = make_case(now=now)
    before = case.app.state.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, tables = post(client, request(case.app.state.store, 55, ()))
    if not visible:
        assert_empty_tables(tables)
    else:
        event_row = table_rows(tables["VEC"])[0]
        assert event_row["DateOpenAnnouncement"] == "2026/11/16 04:00:00"
        assert event_row["DateCloseAnnouncement"] == "2026/11/16 15:00:00"
        assert event_row["DateOpen"] == "2026/11/01 15:00:00"
        assert event_row["DateClose"] == "2026/11/23 15:00:00"
        assert event_row["DateCloseResult"] == "2026/11/23 15:00:00"
        assert {event_row[key] for key in ("RequiredRank", "VoteLimitCount", "EarnedVotes")} == {1, 999, 15}
    assert case.app.state.store.profile() == before


@pytest.mark.parametrize("point", ["at_start", "at_event_end", "before_announcement_end"])
def test_final_phase_fails_explicitly_until_finalists_are_supported(make_case, catalogs, point):
    catalog = catalogs.resolved
    now = {"at_start": catalog.groups[-1].vote_start, "at_event_end": catalog.event.end_at,
           "before_announcement_end": catalog.event.announcement_end - timedelta(seconds=1)}[point]
    case = make_case(now=now)
    before = case.app.state.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        response = post(client, request(case.app.state.store, 55, ()), expected=501)
    assert response.json()["error"]["code"] == "vote_finalists_unavailable"
    assert case.app.state.store.profile() == before


@pytest.mark.parametrize("offset", [0, 1])
def test_announcement_end_is_exclusive(make_case, catalogs, offset):
    case = make_case(now=catalogs.resolved.event.announcement_end + timedelta(seconds=offset))
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        assert_empty_tables(post(client, request(case.app.state.store, 55, ()))[1])


def test_disabled_revival_does_not_require_resolution_or_change_vote_history(make_case, catalogs, bundle):
    initial = record_vote(bundle.player_state.initialize(bundle.bootstrap.initial_state()),
        item_id="item_vote_016001", group_id=1, card_id=1, count=7, definition="a" * 64)
    case = make_case(catalogs.disabled, state=initial)
    before = case.app.state.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        assert_empty_tables(post(client, request(case.app.state.store, 55, ()))[1])
        assert post(client, request(case.app.state.store, 137, ()))[1]["PVH"] == vote_history_response(initial)
    assert case.app.state.store.profile() == before


@pytest.mark.parametrize("columns", [(Column("VOTEPARAMS", "S", ("1,1,999",)),),
    (Column("CT", "I", (1001,)),), (Container("VCC"),)])
def test_read_rejects_business_fields_duplicates_and_foreign_tables(make_case, columns):
    case = make_case()
    before = case.app.state.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        response = post(client, request(case.app.state.store, 55, columns), expected=400)
    assert response.json()["error"]["code"] == "invalid_request_fields"
    assert case.app.state.store.profile() == before


@pytest.mark.parametrize("overrides,code,status", [
    ({"UI": ("S", "another_player")}, "invalid_identity", 403),
    ({"UK": ("S", "wrong")}, "invalid_identity", 403),
    ({"MK": ("S", "wrong")}, "invalid_method_key", 403),
    ({"CT": ("S", "1000")}, "invalid_request_fields", 400),
])
def test_authentication_and_native_field_types_are_enforced(make_case, overrides, code, status):
    case = make_case()
    before = case.app.state.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        response = post(client, altered_request(case.app.state.store, overrides), expected=status)
    assert response.json()["error"]["code"] == code
    assert case.app.state.store.profile() == before


def test_catalog_mutation_cannot_produce_a_partial_configuration(make_case, vote_sources, catalogs):
    catalog = build(vote_sources, resolved_rules(catalogs.default, revival_start=REVIVAL))
    case = make_case(catalog)
    before = case.app.state.store.profile()
    catalog.candidates = ()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        response = post(client, request(case.app.state.store, 55, ()), expected=503)
    assert response.json()["error"]["code"] == "vote_catalog_changed"
    assert case.app.state.store.profile() == before


def voted_case(make_case, vote_sources, catalogs, bundle):
    catalog = build(vote_sources, resolved_rules(catalogs.default, revival_start=REVIVAL))
    candidate = next(row for row in catalog.candidates if row.group_id == 1)
    state = record_vote(bundle.player_state.initialize(bundle.bootstrap.initial_state()),
        item_id="item_vote_016001", group_id=1, card_id=candidate.card_id, count=7, definition="a" * 64)
    return make_case(catalog, state=state), catalog


def test_137_serves_the_saved_history_while_the_catalog_is_unready(make_case, vote_sources, catalogs, bundle,
                                                                   monkeypatch, caplog):
    case, catalog = voted_case(make_case, vote_sources, catalogs, bundle)
    diagnostics = StartupDiagnostics()
    monkeypatch.setattr(case.bootstrap, "startup_diagnostics", diagnostics)
    app = case.reopen()
    before = app.state.store.profile()
    catalog.candidates = ()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, startup = post(client, request(app.state.store, 137, ()))
        refused = post(client, request(app.state.store, 55, ()), expected=503)
    assert startup["PVH"] == vote_history_response(before.state) and table_rows(startup["PVH"])[0]["count"] == 7
    assert refused.json()["error"]["code"] == "vote_catalog_changed"
    assert app.state.store.profile() == before
    record = diagnostics.status()["loads"]["137"]
    assert record["outcome"] == "degraded" and [issue["code"] for issue in record["issues"]] == ["vote_catalog_changed"]
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        assert case.service.response(before.state) == startup["PVH"]
    assert any("outside a recorded load" in line and "vote_catalog_changed" in line for line in caplog.messages)
    assert diagnostics.status()["loads"]["137"] == record


def test_other_writes_log_the_degraded_vote_snapshot_and_commit(make_case, vote_sources, catalogs, bundle, caplog):
    case, catalog = voted_case(make_case, vote_sources, catalogs, bundle)
    store = case.app.state.store
    before = store.profile()
    catalog.candidates = ()
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    save_filter(case.bootstrap, store)
    after = store.profile()
    assert after.revision == before.revision + 1
    assert vote_history_response(after.state) == vote_history_response(before.state)
    assert any("outside a recorded load" in line and "vote_catalog_changed" in line for line in caplog.messages)


@pytest.mark.parametrize("domain", ["vote_rewards", "vote_tickets", "vote_transactions", "vote_last_action",
                                    "vote_finalists", "vote_expiry", "vote_cooking", NOTIFICATION_KEY,
                                    ASSAULT_KEY, ASSAULT_NOTIFICATION_KEY])
def test_saved_vote_operations_require_the_transaction_service(make_case, bundle, domain):
    case = make_case()
    state = {**bundle.player_state.initialize(bundle.bootstrap.initial_state()), domain: {}}
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        case.service.initialize(state)
    assert (caught.value.code, caught.value.status) == ("vote_transactions_unavailable", 503)
    assert state == before


def test_active_history_must_belong_to_current_candidate_group(make_case, catalogs, bundle):
    candidate = next(row for row in catalogs.resolved.candidates if row.group_id == 2)
    state = record_vote(bundle.player_state.initialize(bundle.bootstrap.initial_state()),
        item_id="item_vote_016001", group_id=1, card_id=candidate.card_id, count=7, definition="a" * 64)
    case = make_case(state=state)
    before = case.app.state.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        response = post(client, request(case.app.state.store, 55, ()), expected=503)
    assert response.json()["error"]["code"] == "vote_candidate_history_invalid"
    assert case.app.state.store.profile() == before


def test_unregistered_mutation_operation_remains_explicitly_unsupported(make_case):
    case = make_case()
    before = case.app.state.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        response = post(client, request(case.app.state.store, 57, ()), expected=501)
    assert response.json()["error"]["code"] == "not_implemented"
    assert case.app.state.store.profile() == before


def test_cli_wires_vote_provider_and_rejects_missing_owner_without_rewriting(bundle):
    assert bundle.votes is not None, bundle.vote_error
    assert bundle.votes.bootstrap is bundle.bootstrap
    assert bundle.votes.players is bundle.player_state
    assert bundle.bootstrap.vote_provider.__self__ is bundle.votes
    assert set(bundle.votes.handlers()) == {55, 57, 137}
    assert cli.service_readiness(bundle)["votes"]["ready"] is True
    catalog = bundle.votes.catalog
    assert len(catalog.candidates) == CANDIDATE_COUNT and not catalog.unresolved_candidates
    assert tuple(sum(candidate.group_id == group for candidate in catalog.candidates) for group in (1, 2, 3)) == GROUP_CANDIDATES
    assert {candidate.name: candidate.card_id for candidate in catalog.candidates
            if candidate.name in CHOICES_WITH_LOCAL_ALTERNATIVES} == CHOICES_WITH_LOCAL_ALTERNATIVES
    assert catalog.excluded_template_ids == tuple(sorted(LOCAL_TEMPLATE_CARDS))
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state = record_vote(initial, item_id="retired_vote", group_id=91, card_id=1,
                        count=7, definition="a" * 64)
    for candidate in (state, {**initial, "vote_state": None}, {**initial, "vote_finalists": None},
                      {**initial, "vote_expiry": None}, {**initial, NOTIFICATION_KEY: None}, {**initial, ASSAULT_KEY: None},
                      {**initial, ASSAULT_NOTIFICATION_KEY: None},
                      {**initial, "tables": {**initial["tables"], "vote_history": state["tables"]["vote_history"]}}):
        before = deepcopy(candidate)
        broken = replace(bundle, votes=None, vote_error="fixture_unavailable")
        with pytest.raises(ProtocolError) as caught:
            broken.initialize(candidate)
        assert caught.value.code == "vote_service_unavailable"
        assert candidate == before


def test_status_reports_the_assault_conversion_beside_the_transactions(bundle, make_case):
    transactions = bundle.votes.transactions
    status = bundle.votes.status()
    assert status["transactions"] is True and status["assault_expiry"] == transactions.assault_expiry.status()
    assert status["assault_expiry"]["ready"] is True and status["unavailable"] == [ASSAULT_BATTLES]
    plain = make_case().service.status()
    assert (plain["transactions"], plain["assault_expiry"]) == (False, None)
    assert plain["unavailable"][-1] == ASSAULT_BATTLES and len(plain["unavailable"]) == len(set(plain["unavailable"]))


def test_doctor_accepts_vote_configuration_without_starting_server(bundle, tmp_path, monkeypatch, capsys):
    observed = []
    monkeypatch.setattr(cli, "services", lambda args: observed.append(args) or bundle)
    path = tmp_path / "local-vote.json"
    assert cli.main(["doctor", "--vote-rules", str(path), "--data-dir", str(tmp_path)]) == 0
    assert observed[0].vote_rules == path
    assert '"votes"' in capsys.readouterr().out

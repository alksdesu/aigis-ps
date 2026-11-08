from copy import copy, deepcopy
from datetime import datetime, timedelta
from threading import Barrier, Lock
from concurrent.futures import ThreadPoolExecutor
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
import aigis_ps.request_ledger as ledger
from aigis_ps.startup_diagnostics import StartupDiagnostics
from aigis_ps.vote_rules import VoteRules
from aigis_ps.vote_service import VoteService
from aigis_ps.vote_state import read_vote_history, record_vote
import aigis_ps.vote_transactions as module
from test_evolution_integration import post
from test_present_recovery_integration import claim
from test_quest_service import bundle, request
from test_request_fields import native_request
from test_tower_service import without_enabled
from test_vote_catalog import vote_sources, build
from test_vote_rewards import items_without_emblems
from test_vote_service import table_rows


NOW = datetime(2026, 11, 1, 16, tzinfo=JST)


@pytest.fixture(scope="module")
def catalog(vote_sources):
    return build(vote_sources, VoteRules(revival_start="2026-11-01T15:00:00+09:00"))


@pytest.fixture
def make_case(bundle, catalog, tmp_path):
    serial = 0
    def create(*, tickets=999, unlocked=True, state=None, selected_catalog=None, now=NOW, item_catalog=None):
        nonlocal serial
        serial += 1
        source = selected_catalog or catalog
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        if item_catalog is not None:
            presents.item_catalog = item_catalog
        clock = SimpleNamespace(now=now, calls=0)
        def current():
            clock.calls += 1
            return clock.now
        bootstrap.clock = current
        presents.bootstrap = bootstrap
        service = VoteService(bootstrap, bundle.player_state, source, possessions=bundle.possessions, presents=presents)
        bootstrap.vote_provider = service.response
        bootstrap.present_provider = presents.response
        initial = bundle.player_state.initialize(bootstrap.initial_state()) if state is None else deepcopy(state)
        if state is None:
            if unlocked:
                initial["tables"]["quest_records"] = [{"QuestID": 1004, "MaxRank": 3, "AchieveFlag": 1,
                    "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False}]
            if tickets:
                initial = bundle.possessions.credit(initial, {source.groups[0].item_id: tickets})
        initial = service.initialize(initial)
        directory = tmp_path / str(serial)
        def application():
            return create_app(directory, initial_state=initial, state_initializer=service.initialize,
                handlers=bootstrap.handlers() | presents.handlers() | service.handlers())
        app = application()
        return SimpleNamespace(service=service, transactions=service.transactions, presents=presents,
            bundle=bundle, catalog=source, clock=clock, bootstrap=bootstrap, app=app, store=app.state.store,
            reopen=application, candidate=source.candidates[0], group=source.groups[0])
    return create


def vote(case, count=5, *, card_id=None, group_id=1, tick=1000, extra=()):
    return request(case.store, 57, (Column("GID", "I", (group_id,)),
        Column("CID", "I", (card_id or case.candidate.card_id,)), Column("CNT", "I", (count,)), *extra), tick=tick)


def counts(case):
    return case.bundle.possessions.counts(case.store.profile().state)


def test_vote_commits_tickets_mail_history_and_exact_restart_retry(make_case, monkeypatch):
    case = make_case()
    before = case.store.profile()
    envelope = vote(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        wire, tables = post(client, envelope)
        assert set(tables) == {"SYSTEM", "PVH", "POSSESSION", "VRC", "PPT", "ASSAULT"}
        assert table_rows(tables["PVH"])[0]["count"] == 5
        assert [row["Count"] for row in table_rows(tables["VRC"])] == [5]
        assert table_rows(tables["ASSAULT"]) == [{"Remain": 0, "Next": 10}]
        assert counts(case)[case.group.item_id] == 995
        assert counts(case)["item_10000000"] == 0
        after = case.store.profile()
        assert after.revision == before.revision + 1
        assert after.state["player_state"] == before.state["player_state"]
        assert len(case.presents.rows(after.state)) == 1
        monkeypatch.setattr(case.bootstrap, "clock", lambda: pytest.fail("Replay must not read a new date"))
        assert post(client, envelope)[0] == wire
    reopened = case.reopen()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
    assert case.store.profile() == after


def test_startup_refreshes_daily_ticket_once_and_keeps_55_read_only(make_case):
    case = make_case(tickets=0)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        before = case.store.profile()
        _, start = post(client, request(case.store, 137, ()))
        assert table_rows(start["POSSESSION"]) == [{"ItemID": "item_10000000", "Count": 1, "Enabled": 0}]
        assert case.store.profile().revision == before.revision + 1
        stable = case.store.profile()
        post(client, request(case.store, 137, (), tick=1001))
        _, directory = post(client, request(case.store, 55, ()))
        assert case.store.profile() == stable
        assert table_rows(directory["ASSAULT"]) == [{"Remain": 0, "Next": 15}]
        assert all(row["SkillLevel"] == 1 for row in table_rows(directory["VRC"]) if row["CardID"])
        post(client, vote(case, 1, tick=1002))
        assert counts(case)["item_10000000"] == 0
        spent = case.store.profile()
        post(client, request(case.store, 137, (), tick=1003))
        assert case.store.profile() == spent
        case.clock.now = datetime(2026, 11, 2, 4, tzinfo=JST)
        post(client, request(case.store, 137, (), tick=1004))
        assert counts(case)["item_10000000"] == 1
        assert read_vote_history(case.store.profile().state)[0]["count"] == 1


def test_fixed_and_repeated_rewards_cross_exact_points_and_claim_actual_unit(make_case):
    case = make_case()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, first = post(client, vote(case, 200))
        assert [row["Count"] for row in table_rows(first["VRC"])] == [5, 10, 20, 30, 40, 50, 75, 100, 125, 150, 175, 200]
        assert table_rows(first["ASSAULT"]) == [{"Remain": 13, "Next": 10}]
        _, second = post(client, vote(case, 100, tick=1001))
        assert [(row["Count"], row["Repeat"], row["CardID"]) for row in table_rows(second["VRC"])] == [(300, 0, 2871)]
        assert table_rows(second["ASSAULT"]) == [{"Remain": 20, "Next": 15}]
        assert counts(case)[case.group.item_id] == 700
        saved = case.store.profile()
        reward_mail = table_rows(second["PPT"])[0]
        claim_wire = claim(case.store, 1, [reward_mail["PresentID"]], tick=1002)
        _, awarded = post(client, claim_wire)
        units = table_rows(awarded["PPU"])
        assert len(units) == 1 and units[0]["A1"] == 2871
        assert units[0]["A6"] == 1
        assert units[0]["A3"] == 0
        assert case.store.profile().state["vote_rewards"] == saved.state["vote_rewards"]
        stale = post(client, vote(case, 100, tick=1001), expected=409)
        assert stale.json()["error"]["code"] == "vote_retry_stale"


@pytest.mark.parametrize("registration", (42, 57, 129, 137))
def test_any_response_encoding_failure_rolls_back_all_assets(make_case, monkeypatch, registration):
    case = make_case()
    before = case.store.profile()
    original = module.encode_response
    def fail(profile, response):
        if profile.registration_keys == (registration,):
            raise ProtocolError("fixture_encoding_failure", "Fixture failure", 503)
        return original(profile, response)
    monkeypatch.setattr(module, "encode_response", fail)
    with pytest.raises(ProtocolError, match="Fixture failure"):
        case.transactions.submit(vote(case), case.store)
    assert case.store.profile() == before


@pytest.mark.parametrize("kind", ("locked", "insufficient", "unknown_card", "wrong_group", "zero", "negative", "oversized", "extra"))
def test_rejected_votes_leave_state_unchanged(make_case, kind):
    case = make_case(unlocked=kind != "locked", tickets=0 if kind == "insufficient" else 999)
    kwargs = {"count": 5}
    if kind == "unknown_card": kwargs["card_id"] = 0x7FFFFFFF
    if kind == "wrong_group": kwargs["group_id"] = 2
    if kind == "zero": kwargs["count"] = 0
    if kind == "negative": kwargs["count"] = -1
    if kind == "oversized": kwargs["count"] = 1000
    if kind == "extra": kwargs["extra"] = (Column("Reward", "I", (999,)),)
    before = case.store.profile()
    with pytest.raises(ProtocolError):
        case.transactions.submit(vote(case, **kwargs), case.store)
    assert case.store.profile() == before


def test_mailbox_capacity_rejects_vote_without_consuming_daily_ticket(make_case):
    case = make_case()
    before = case.store.profile()
    full, _ = case.presents.enqueue(before.state, [{"kind": "free_crystal", "amount": 1, "description": "fixture"}] * 500)
    case.store.update_state(full, before.revision)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.transactions.submit(vote(case), case.store)
    assert caught.value.code == "present_capacity"
    assert case.store.profile() == before


def test_candidate_limit_and_group_boundary_are_enforced(make_case):
    case = make_case()
    case.transactions.submit(vote(case, 999), case.store)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.transactions.submit(vote(case, 1, tick=1001), case.store)
    assert caught.value.code == "vote_candidate_limit"
    case.clock.now = case.group.vote_end
    with pytest.raises(ProtocolError) as caught:
        case.transactions.submit(vote(case, 1, tick=1002), case.store)
    assert caught.value.code == "vote_group_inactive"
    assert case.store.profile() == before


@pytest.mark.parametrize("duplicate", (False, True))
def test_two_concurrent_votes_share_one_daily_ticket_and_reward_threshold(make_case, monkeypatch, duplicate):
    case = make_case()
    first = vote(case, 5)
    second = first if duplicate else vote(case, 5, tick=1001)
    original = case.store.update_state
    barrier, lock, calls = Barrier(2), Lock(), 0
    def update(state, revision):
        nonlocal calls
        with lock:
            calls += 1
            wait = calls <= 2
        if wait: barrier.wait(timeout=20)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    def submit(envelope):
        try:
            return case.transactions.submit(envelope, case.store)
        except ProtocolError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, (first, second)))
    total = read_vote_history(case.store.profile().state)[0]["count"]
    assert total in (5, 10)
    if duplicate:
        assert total == 5 and results[0] == results[1]
    assert counts(case)[case.group.item_id] == 999 - (total - 1)
    assert counts(case)["item_10000000"] == 0
    assert len(case.presents.rows(case.store.profile().state)) == total // 5
    assert all(isinstance(result, bytes) or result == "vote_request_stale" for result in results)


def test_startup_accepts_native_duplicated_platform_code(make_case):
    case = make_case(tickets=0)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, start = post(client, native_request(case.store.profile(), 137))
        assert table_rows(start["POSSESSION"]) == [{"ItemID": "item_10000000", "Count": 1, "Enabled": 0}]
        issued = case.store.profile()
        response = post(client, native_request(issued, 137, tick=1001, platform_codes=(1, 2)), 400)
        assert response.json()["error"]["code"] == "invalid_request_fields"
        assert case.store.profile() == issued


def test_startup_refresh_failure_does_not_issue_ticket(make_case, monkeypatch):
    case = make_case(tickets=0)
    before = case.store.profile()
    monkeypatch.setattr(case.bootstrap, "vote_provider", lambda state: Container("PVH"))
    with pytest.raises(ProtocolError) as caught:
        case.transactions.startup(request(case.store, 137, ()), case.store)
    assert caught.value.code == "vote_provider_inconsistent"
    assert case.store.profile() == before


def test_old_vote_totals_are_a_baseline_without_backfill(make_case, catalog, bundle):
    case = make_case()
    state = case.store.profile().state
    state = record_vote(state, item_id=case.group.item_id, group_id=1, card_id=case.candidate.card_id,
        count=199, definition=case.transactions.rewards.group_definition(case.group))
    legacy = make_case(state=state)
    legacy.transactions.submit(vote(legacy, 1), legacy.store)
    assert len(legacy.presents.rows(legacy.store.profile().state)) == 1
    reward = legacy.store.profile().state["vote_rewards"]["items"][legacy.group.item_id]
    assert reward["baseline_count"] == 199 and set(reward["deliveries"]) == {"200"}
    assert legacy.store.profile().state["vote_rewards"]["assault"]["16"]["earned"] == 0


def test_revival_schedule_change_keeps_old_proofs_and_does_not_repeat_rewards(make_case, vote_sources):
    original = make_case()
    original.transactions.submit(vote(original), original.store)
    saved = original.store.profile().state
    changed = build(vote_sources, VoteRules(revival_start="2026-11-02T15:00:00+09:00"))
    case = make_case(state=saved, selected_catalog=changed, now=NOW + timedelta(days=1))
    case.store = original.store
    assert case.store.profile().state["vote_rewards"] == saved["vote_rewards"]
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.transactions.submit(vote(case), case.store)
    assert caught.value.code == "vote_retry_stale"
    assert case.store.profile() == before
    response = case.transactions.submit(vote(case, tick=1001), case.store)
    assert response
    entry = case.store.profile().state["vote_rewards"]["items"][case.group.item_id]
    assert set(entry["deliveries"]) == {"5", "10"}


@pytest.mark.parametrize("field,value", (("request", []), ("response", "\ud800"), ("revision", True), ("ordinary_used", -1)))
def test_malformed_receipt_is_rejected_before_any_new_vote(make_case, field, value):
    case = make_case()
    case.transactions.submit(vote(case), case.store)
    saved = case.store.profile()
    state = deepcopy(saved.state)
    state["vote_transactions"]["receipts"][-1][field] = value
    if field == "response":
        with pytest.raises(ProtocolError):
            case.transactions.initialize(state)
        return
    case.store.update_state(state, saved.revision)
    before = case.store.profile()
    with pytest.raises(ProtocolError):
        case.transactions.submit(vote(case, tick=1001), case.store)
    assert case.store.profile() == before


def test_receipt_cannot_claim_another_reward_source_after_rehash(make_case):
    case = make_case()
    case.transactions.submit(vote(case), case.store)
    saved = case.store.profile()
    state = deepcopy(saved.state)
    state["vote_transactions"]["receipts"][-1]["award_rows"][0]["Count"] = 10
    state["vote_last_action"]["award_rows"][0]["Count"] = 10
    state["vote_transactions"]["receipts"][-1]["state"] = module._state_digest(state)
    with pytest.raises(ProtocolError) as caught:
        case.transactions.initialize(state)
    assert caught.value.code == "vote_reward_receipt_invalid"


def test_expired_bounded_receipt_cannot_charge_again(make_case):
    case = make_case()
    oldest = vote(case, 1)
    for index in range(18):
        case.transactions.submit(vote(case, 1, tick=1000 + index), case.store)
    before = case.store.profile()
    assert len(before.state["vote_transactions"]["receipts"]) == 16
    with pytest.raises(ProtocolError) as caught:
        case.transactions.submit(oldest, case.store)
    assert caught.value.code == "vote_request_stale"
    assert case.store.profile() == before


def test_two_startup_requests_issue_one_daily_ticket(make_case, monkeypatch):
    case = make_case(tickets=0)
    envelope = request(case.store, 137, ())
    original = case.store.update_state
    barrier, lock, calls = Barrier(2), Lock(), 0
    def update(state, revision):
        nonlocal calls
        with lock:
            calls += 1
            wait = calls <= 2
        if wait: barrier.wait(timeout=20)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    before = case.store.profile()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: case.transactions.startup(envelope, case.store), range(2)))
    assert results[0] == results[1]
    assert case.store.profile().revision == before.revision + 1
    assert counts(case) == {"item_10000000": 1}


@pytest.mark.parametrize("domain", ("vote_rewards", "vote_tickets", "vote_state"))
def test_transaction_ledger_cannot_be_removed_and_recreated_as_a_legacy_baseline(make_case, domain):
    case = make_case()
    case.transactions.submit(vote(case, 1), case.store)
    state = deepcopy(case.store.profile().state)
    state.pop(domain)
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        case.transactions.initialize(state)
    assert caught.value.code == "vote_receipts_invalid"
    assert state == before


def test_startup_and_vote_compete_without_reissuing_consumed_daily_ticket(make_case, monkeypatch):
    case = make_case()
    envelope = vote(case)
    startup = request(case.store, 137, ())
    original = case.store.update_state
    barrier, lock, calls = Barrier(2), Lock(), 0
    def update(state, revision):
        nonlocal calls
        with lock:
            calls += 1
            wait = calls <= 2
        if wait: barrier.wait(timeout=20)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(case.transactions.startup, startup, case.store),
                   executor.submit(case.transactions.submit, envelope, case.store)]
        assert all(future.result() for future in futures)
    assert read_vote_history(case.store.profile().state)[0]["count"] == 5
    assert counts(case)["item_10000000"] == 0
    assert counts(case)[case.group.item_id] == 995
    assert len(case.presents.rows(case.store.profile().state)) == 1


def recorded_startup(case, monkeypatch):
    diagnostics = StartupDiagnostics()
    monkeypatch.setattr(case.bootstrap, "startup_diagnostics", diagnostics)
    monkeypatch.setattr(case.transactions, "startup", diagnostics.wrap(137, case.transactions._startup))
    return diagnostics


def test_unready_transactions_serve_saved_startup_without_refreshing_tickets(make_case, monkeypatch):
    case = make_case(tickets=0)
    diagnostics = recorded_startup(case, monkeypatch)

    def unready():
        raise ProtocolError("vote_rewards_dependencies_changed", "Vote reward dependencies changed after setup", 503)

    monkeypatch.setattr(case.transactions.rewards, "_ready", unready)
    before = case.store.profile()
    response = case.transactions.startup(request(case.store, 137, ()), case.store)
    assert response == case.bootstrap._configuration_response(before)
    assert case.store.profile() == before
    assert counts(case).get("item_10000000", 0) == 0
    record = diagnostics.status()["loads"]["137"]
    assert record["outcome"] == "degraded"
    assert record["issues"] == [{"code": "vote_rewards_dependencies_changed",
                                 "message": "Vote reward dependencies changed after setup"}]
    monkeypatch.undo()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, refreshed = post(client, request(case.store, 137, (), tick=1001))
    assert table_rows(refreshed["POSSESSION"]) == [{"ItemID": "item_10000000", "Count": 1, "Enabled": 0}]


def test_startup_serves_the_saved_vote_history_while_the_catalog_is_unready(make_case, monkeypatch):
    case = make_case(tickets=0)
    diagnostics = recorded_startup(case, monkeypatch)

    def unready():
        raise ProtocolError("vote_catalog_unavailable", "Vote catalog is unavailable", 503)

    monkeypatch.setattr(case.catalog, "_ready", unready)
    before = case.store.profile()
    response = case.transactions.startup(request(case.store, 137, ()), case.store)
    assert response == case.bootstrap._configuration_response(before)
    assert case.store.profile() == before
    assert counts(case).get("item_10000000", 0) == 0
    record = diagnostics.status()["loads"]["137"]
    assert record["outcome"] == "degraded"
    assert record["issues"] == [{"code": "vote_catalog_unavailable", "message": "Vote catalog is unavailable"}]
    with pytest.raises(ProtocolError) as caught:
        case.transactions.submit(vote(case), case.store)
    assert caught.value.code == "vote_catalog_unavailable" and case.store.profile() == before


def test_startup_does_not_require_card_references(make_case, bundle):
    stale = deepcopy(make_case(tickets=0).store.profile().state)
    stale["player_state"]["units"][0]["CardID"] = 0x7FFFFFFF
    case = make_case(state=stale)
    with pytest.raises(ProtocolError) as caught:
        bundle.player_state.validate(case.store.profile().state)
    assert caught.value.code == "player_reference_invalid"
    case.transactions.startup(request(case.store, 137, ()), case.store)
    assert counts(case)["item_10000000"] == 1


def test_startup_conflicts_still_refuse_and_are_recorded(make_case, monkeypatch):
    case = make_case(tickets=0)
    diagnostics = recorded_startup(case, monkeypatch)
    before = case.store.profile()

    def conflict(state, revision):
        raise ValueError("Profile revision changed; reload before updating")

    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as caught:
        case.transactions.startup(request(case.store, 137, ()), case.store)
    assert (caught.value.code, caught.value.status) == ("profile_changed", 409)
    assert case.store.profile() == before
    record = diagnostics.status()["loads"]["137"]
    assert record["outcome"] == "refused"
    assert record["issues"] == [{"code": "profile_changed",
                                 "message": "Player state changed while refreshing vote tickets"}]


def test_client_clock_rollback_votes_again_and_keeps_exact_replays_stale(make_case):
    case = make_case()
    first = vote(case, 1, tick=0x7FFFFFFF)
    case.transactions.submit(first, case.store)
    case.transactions.submit(vote(case, 1, tick=-0x80000000), case.store)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.transactions.submit(first, case.store)
    assert caught.value.code == "vote_retry_stale"
    assert case.store.profile() == before
    history = before.state["vote_transactions"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION and len(history["processed_keys"]) == 2
    assert all("client_time" not in entry and entry["cooking_definition"] is None for entry in history["receipts"])
    assert read_vote_history(before.state)[0]["count"] == 2


def test_vote_refusals_are_logged_with_their_request_number(make_case, caplog):
    case = make_case()
    first = vote(case, 1)
    case.transactions.submit(first, case.store)
    case.transactions.submit(vote(case, 1, tick=1001), case.store)
    before = case.store.profile()
    with caplog.at_level(logging.WARNING, logger="aigis_ps"), pytest.raises(ProtocolError) as caught:
        case.transactions.submit(first, case.store)
    assert caught.value.code == "vote_retry_stale" and case.store.profile() == before
    assert any("Vote 57 refused with vote_retry_stale" in line for line in caplog.messages)


def test_watermark_history_upgrades_and_keeps_replaying_its_receipt(make_case):
    case = make_case()
    envelope = vote(case, tick=2000)
    response = case.transactions.submit(envelope, case.store)
    profile = case.store.profile()
    legacy = deepcopy(profile.state)
    legacy["vote_last_action"].pop("cooking_definition")
    receipts = [{**{key: value for key, value in entry.items() if key != "cooking_definition"},
                 "client_time": 2000, "revision": profile.revision + 1, "state": module._state_digest(legacy)}
                for entry in legacy["vote_transactions"]["receipts"]]
    legacy["vote_transactions"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
        "seen_keys": list(legacy["vote_transactions"]["processed_keys"]), "receipts": receipts}
    assert case.transactions.initialize(legacy) == legacy
    case.store.update_state(legacy, profile.revision)
    before = case.store.profile()
    assert case.transactions.submit(envelope, case.store) == response
    assert case.store.profile() == before
    case.transactions.submit(vote(case, 1, tick=999), case.store)
    upgraded = case.store.profile().state
    assert upgraded["vote_transactions"]["schema_version"] == ledger.SCHEMA_VERSION
    assert [entry["cooking_definition"] for entry in upgraded["vote_transactions"]["receipts"]] == [None, None]
    assert upgraded["vote_transactions"]["processed_keys"] == [entry["request"] for entry in upgraded["vote_transactions"]["receipts"]]
    assert upgraded["vote_last_action"]["cooking_definition"] is None
    assert read_vote_history(upgraded)[0]["count"] == 6


def test_two_column_inventory_receipt_replays_its_saved_bytes(make_case):
    case = make_case()
    envelope = vote(case)
    payload = case.transactions.submit(envelope, case.store)
    legacy = without_enabled(payload)
    possession = next(node for node in loads(legacy).children if node.name == "POSSESSION")
    assert legacy != payload
    assert [column.name for column in possession.children] == ["ItemID", "Count"]
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state["vote_transactions"]["receipts"][-1].update(response=legacy.decode(), revision=profile.revision + 1)
    case.store.update_state(state, profile.revision)
    saved = case.store.profile()
    assert case.transactions.submit(envelope, case.store) == legacy
    assert case.store.profile() == saved


def test_receipt_digest_outside_every_committed_configuration_is_stale(make_case):
    case = make_case()
    envelope = vote(case)
    case.transactions.submit(envelope, case.store)
    profile = case.store.profile()
    forged = deepcopy(profile.state)
    voting = case.transactions
    forged["vote_transactions"]["receipts"][-1].update(content=voting._assault_content(voting._legacy_content()),
                                                        revision=profile.revision + 1)
    case.store.update_state(forged, profile.revision)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        voting.submit(envelope, case.store)
    assert (caught.value.code, caught.value.status) == ("vote_retry_stale", 409)
    assert case.store.profile() == before


def committed_before_emblem_mail(make_case, bundle):
    plain = items_without_emblems(bundle.presents)
    committed = make_case(item_catalog=plain)
    envelope = vote(committed)
    response = committed.transactions.submit(envelope, committed.store)
    attached = make_case(state=committed.store.profile().state)
    return SimpleNamespace(store=committed.store, envelope=envelope, response=response, bootstrap=attached.bootstrap,
                           voting=attached.transactions,
                           earlier=attached.transactions.rewards.content_version_for(plain.content_version))


def test_votes_committed_before_emblem_mail_attached_replay_their_saved_bytes(make_case, bundle, monkeypatch):
    case = committed_before_emblem_mail(make_case, bundle)
    voting, earlier = case.voting, case.earlier
    before = case.store.profile()
    content = before.state["vote_transactions"]["receipts"][-1]["content"]
    assert content == voting._assault_content(voting._cooking_content(earlier), earlier) != voting.content_version
    monkeypatch.setattr(case.bootstrap, "clock", lambda: pytest.fail("Replay must not read a new date"))
    assert voting.submit(case.envelope, case.store) == case.response
    assert case.store.profile() == before


def test_receipt_digest_mixing_item_catalog_versions_is_stale(make_case, bundle):
    case = committed_before_emblem_mail(make_case, bundle)
    voting = case.voting
    profile = case.store.profile()
    forged = deepcopy(profile.state)
    forged["vote_transactions"]["receipts"][-1].update(content=voting._assault_content(
        voting._cooking_content(case.earlier)), revision=profile.revision + 1)
    case.store.update_state(forged, profile.revision)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        voting.submit(case.envelope, case.store)
    assert (caught.value.code, caught.value.status) == ("vote_retry_stale", 409)
    assert case.store.profile() == before

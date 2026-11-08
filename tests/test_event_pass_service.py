from copy import copy, deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import hashlib
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.alxml import Column, loads
from aigis_ps.energy_clock import JST
from aigis_ps.errors import REFUSAL_AFTERMATH, ProtocolError
from aigis_ps.event_pass_catalog import ITEM_RESOURCE_NAME, EventPassReward
import aigis_ps.event_pass_service as module
from aigis_ps.event_pass_service import REBIND_OPERATION, EventPassService
from aigis_ps.event_pass_state import DEFINITION_CHANGED, IDENTITY_UNREPLAYABLE, LEDGER_KEY, read_event_passes
import aigis_ps.request_ledger as ledger
from aigis_ps.present_items import PresentItemCatalog
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.protocol import NATIVE_CELL_BYTES
from aigis_ps.storage import Store
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
COOKING = "item_cook_logbo_00004"
TICKET = "item_smn_ticket_19144"
FORGED = hashlib.sha256(b"forged").hexdigest()


def rewards():
    return tuple(EventPassReward(identifier, point, premium, kind, param, item, identifier - 1)
        for identifier, point, premium, kind, param, item in (
            (1, 10, False, 0, 2, ""), (2, 10, False, 1, 6, ""),
            (3, 20, False, 2, 1000, ""), (4, 20, False, 22, 10, "trade_item_001000"),
            (5, 30, False, 22, 1, COOKING), (6, 30, False, 22, 1, TICKET),
            (7, 10, True, 1, 24, ""), (8, 30, True, 0, 3, "")))


def changed_catalog(original, **updates):
    result = copy(original)
    entry = replace(original.passes[0], **updates)
    result._passes = (entry, *original.passes[1:])
    result._by_id = MappingProxyType({item.item_id: item for item in result._passes})
    result._content_version = hashlib.sha256(json.dumps([asdict(item) for item in result._passes],
        sort_keys=True, default=str).encode()).hexdigest()
    return result


@pytest.fixture(scope="module")
def baseline(bundle):
    assert bundle.event_passes is not None, bundle.event_pass_error
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    return bundle.initialize(bundle.player_state.initialize(bootstrap.initial_state()))


@pytest.fixture(scope="module")
def item_catalogs(bundle):
    raw = bundle.native_cdn.assets.read(ITEM_RESOURCE_NAME)[0]
    emblems = bundle.presents.emblem_catalog
    assert emblems is not None, bundle.presents.emblem_error
    return SimpleNamespace(plain=PresentItemCatalog(bundle.possessions, raw),
                           mailed=PresentItemCatalog(bundle.possessions, raw, emblems=emblems))


@pytest.fixture
def factory(bundle, baseline, tmp_path):
    serial = 0
    def build(points=30, premium=False, *, definitions=None, active=True, legacy=None, items=None):
        nonlocal serial
        serial += 1
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        if items is not None:
            presents.item_catalog = items
        clock = SimpleNamespace(now=NOW, calls=0)
        def current_time():
            clock.calls += 1
            return clock.now
        bootstrap.clock = current_time
        catalog = changed_catalog(bundle.event_passes.catalog, status=1 if active else 2,
            start_at=NOW - timedelta(days=1), end_at=NOW + timedelta(days=1),
            time_limit="2026/11/01 11:00", rewards=rewards() if definitions is None else definitions)
        service = EventPassService(bootstrap, bundle.player_state, presents, bundle.possessions, catalog)
        bootstrap.event_pass_provider = service.response
        bootstrap.present_provider = presents.response
        state = deepcopy(baseline)
        state["sentinel"] = {"keep": [1]}
        entry = catalog.passes[0]
        amounts = {}
        if points:
            amounts[entry.item_id] = points
        if premium:
            amounts[entry.premium_item_id] = 1
        state = bundle.possessions.credit(state, amounts)
        if legacy is not None:
            state["tables"]["event_pass_status"] = [{"ID": entry.item_id,
                "ReceivedNormalPoint": legacy[0], "ReceivedPremiumPoint": legacy[1]}]
            state.pop("event_pass_state", None)
        state = service.initialize(state)
        return SimpleNamespace(bundle=bundle, service=service, bootstrap=bootstrap, presents=presents,
            clock=clock, entry=entry, store=Store(tmp_path / str(serial) / "state.sqlite3", state))
    return build


def action(case, *, tick=1000, identifiers=None, extra=()):
    return request(case.store, 159, (Column("EPI", "S", identifiers if identifiers is not None
        else (case.entry.item_id,)), *extra), tick=tick)


def nodes(response):
    return {node.name: node for node in loads(response).children}


def watermark(case):
    return next(row for row in read_event_passes(case.store.profile().state) if row["ID"] == case.entry.item_id)


def change(case, callback):
    profile = case.store.profile()
    updated = deepcopy(profile.state)
    callback(updated)
    case.store.update_state(updated, profile.revision)


def unchanged(case, callback, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as error:
        callback()
    assert case.store.profile() == before
    if code is not None:
        assert error.value.code == code
    return error.value


def unclaimed(case, envelope):
    before = case.store.profile()
    result = nodes(case.service.claim(envelope, case.store))
    assert tuple(result) == ("SYSTEM", "EventPassStatus")
    assert case.store.profile() == before
    status = result["EventPassStatus"].children
    return [dict(zip((column.name for column in status), values)) for values in zip(*(column.values for column in status))]


def reopen(case, catalog=None):
    case.store = Store(case.store.path)
    case.service = EventPassService(case.bootstrap, case.bundle.player_state, case.presents,
                                   case.bundle.possessions, catalog or case.service.catalog)
    case.bootstrap.event_pass_provider = case.service.response


def accept_stored_item(monkeypatch, presents, item):
    known = presents._item_definition
    # Real item names stay short, so only a declared stored-bound name reaches the mail writer's native cell check.
    monkeypatch.setattr(presents, "_item_definition", lambda row: None if row.get("ItemID") == item else known(row))
    with pytest.raises(ProtocolError) as caught:
        presents.preview([{"kind": "item", "item_id": item, "amount": 1, "description": module.DESCRIPTION}], 0)
    assert (caught.value.code, caught.value.status) == ("present_item_unreplayable", 503)


def test_initialization_is_pure_and_158_does_not_write_or_claim(factory):
    case = factory()
    before = case.store.profile()
    initialized = case.service.initialize(before.state)
    initialized["sentinel"]["keep"].append(2)
    assert before.state["sentinel"] == {"keep": [1]}
    result = nodes(case.service.read(request(case.store, 158, ()), case.store))
    assert set(result) == {"SYSTEM", "EventPassStatus"}
    assert [(column.name, column.type, column.values) for column in result["EventPassStatus"].children] == [
        ("ID", "S", ()), ("ReceivedNormalPoint", "I", ()), ("ReceivedPremiumPoint", "I", ())]
    assert case.store.profile() == before


def test_all_reward_types_enter_mail_atomically_without_direct_grants(factory):
    case = factory(premium=True)
    before = case.store.profile()
    result = nodes(case.service.claim(action(case), case.store))
    after = case.store.profile()
    assert tuple(result) == ("SYSTEM", "PPT", "EventPassStatus")
    assert after.revision == before.revision + 1
    mail = case.presents.rows(after.state)
    assert len(mail) == 8
    assert [(row["Type"], row["Content"]) for row in mail] == [
        (0, 2), (1, 6), (2, 1000), (22, 10), (22, 1), (22, 1), (1, 24), (0, 3)]
    assert [row["ItemID"] for row in mail if row["Type"] == 22] == ["trade_item_001000", COOKING, TICKET]
    assert watermark(case) == {"ID": case.entry.item_id, "ReceivedNormalPoint": 30, "ReceivedPremiumPoint": 30}
    assert after.state["player_state"] == before.state["player_state"]
    for name in ("possessions", "game_param", "crystals", "card_status"):
        assert after.state["tables"][name] == before.state["tables"][name]
    assert after.state["sentinel"] == before.state["sentinel"]
    journal = after.state["event_pass_state"]["passes"][case.entry.item_id]
    assert journal["deliveries"]["normal"]["10"]["reward_ids"] == [1, 2]
    assert journal["deliveries"]["normal"]["10"]["mail_ids"] == [1, 2]


def test_premium_acquired_after_normal_rewards_only_backfills_premium_track(factory):
    case = factory()
    case.service.claim(action(case), case.store)
    before = case.store.profile()
    assert len(case.presents.rows(before.state)) == 6
    state = case.bundle.possessions.credit(before.state, {case.entry.premium_item_id: 1})
    case.store.update_state(state, before.revision)
    case.service.claim(action(case, tick=1001), case.store)
    after = case.store.profile()
    assert len(case.presents.rows(after.state)) == 8
    assert case.presents.rows(after.state)[:6] == case.presents.rows(before.state)
    assert watermark(case)["ReceivedPremiumPoint"] == 30
    assert unclaimed(case, action(case, tick=1002)) == [watermark(case)]


def test_increasing_points_claims_only_new_thresholds_without_consuming_points(factory):
    case = factory(10, premium=True)
    case.service.claim(action(case), case.store)
    assert watermark(case)["ReceivedNormalPoint"] == 10
    assert len(case.presents.rows(case.store.profile().state)) == 3
    before = case.store.profile()
    state = case.bundle.possessions.credit(before.state, {case.entry.item_id: 20})
    case.store.update_state(state, before.revision)
    case.service.claim(action(case, tick=1001), case.store)
    assert len(case.presents.rows(case.store.profile().state)) == 8
    assert next(row["Count"] for row in case.store.profile().state["tables"]["possessions"]
                if row["ItemID"] == case.entry.item_id) == 30


def test_legacy_watermarks_are_baselines_not_retroactive_rewards(factory):
    case = factory(30, premium=True, legacy=(10, 20))
    case.service.claim(action(case), case.store)
    assert len(case.presents.rows(case.store.profile().state)) == 5
    journal = case.store.profile().state["event_pass_state"]["passes"][case.entry.item_id]
    assert journal["baseline_normal"] == 10 and journal["baseline_premium"] == 20
    assert set(journal["deliveries"]["normal"]) == {"20", "30"}
    assert set(journal["deliveries"]["premium"]) == {"30"}


def test_exact_retry_survives_reopen_without_reading_clock(factory):
    case = factory()
    envelope = action(case)
    result = case.service.claim(envelope, case.store)
    before = case.store.profile()
    reopen(case)
    case.bootstrap.clock = lambda: pytest.fail("Exact retry must not read the current clock")
    assert case.service.claim(envelope, case.store) == result
    assert case.store.profile() == before


def test_later_state_change_rejects_old_receipt_without_restoring_mail(factory):
    case = factory()
    envelope = action(case)
    case.service.claim(envelope, case.store)
    change(case, lambda state: state["tables"]["presents"].clear())
    unchanged(case, lambda: case.service.claim(envelope, case.store), "event_pass_retry_stale")
    assert case.presents.rows(case.store.profile().state) == []
    assert unclaimed(case, action(case, tick=1001)) == [watermark(case)]


def test_new_resource_version_rejects_exact_old_receipt(factory):
    case = factory()
    envelope = action(case)
    case.service.claim(envelope, case.store)
    updated = changed_catalog(case.service.catalog, end_at=case.entry.end_at + timedelta(days=1))
    reopen(case, updated)
    unchanged(case, lambda: case.service.claim(envelope, case.store), "event_pass_retry_stale")


def previous_window_build(catalog):
    previous = copy(catalog)
    previous._content_version = catalog.legacy_window_content_version
    return previous


def test_receipt_committed_while_pass_windows_compared_jst_instants_replays_its_exact_bytes(factory):
    case = factory()
    current = case.service.catalog
    reopen(case, previous_window_build(current))
    envelope = action(case)
    response = case.service.claim(envelope, case.store)
    committed = case.store.profile()
    reopen(case, current)
    case.bootstrap.clock = lambda: pytest.fail("Exact retry must not read the current clock")
    assert receipt_content(case) != case.service.content_version
    assert receipt_content(case) in case.service.replayable_content_versions()
    assert case.service.claim(envelope, case.store) == response
    assert case.store.profile() == committed


def test_receipt_of_any_other_pass_catalog_digest_stays_stale(factory):
    case = factory()
    current = case.service.catalog
    other = copy(current)
    other._content_version = FORGED
    reopen(case, other)
    envelope = action(case)
    case.service.claim(envelope, case.store)
    reopen(case, current)
    error = unchanged(case, lambda: case.service.claim(envelope, case.store), "event_pass_retry_stale")
    assert error.status == 409


def test_existing_instance_rejects_dependency_mutation(factory):
    case = factory()
    case.service.catalog._content_version = "0" * 64
    unchanged(case, lambda: case.service.claim(action(case), case.store), "event_pass_dependencies_changed")


def receipt_content(case):
    return case.store.profile().state[module.HISTORY_NAMESPACE]["receipts"][-1]["content"]


def test_versions_for_an_item_catalog_version_are_those_of_a_service_built_over_it(factory, item_catalogs):
    plain = factory(items=item_catalogs.plain).service
    mailed = factory(items=item_catalogs.mailed).service
    assert mailed.content_version != plain.content_version
    for service, items in ((plain, item_catalogs.plain), (mailed, item_catalogs.mailed)):
        assert service.content_version_for(items.content_version) == service.content_version
        assert service.automation_content_version_for(items.content_version) == service.automation_content_version
    version = item_catalogs.plain.content_version
    assert mailed.content_version_for(version) == plain.content_version
    assert mailed.automation_content_version_for(version) == plain.automation_content_version
    catalogs = (mailed.catalog.content_version, mailed.catalog.legacy_window_content_version)
    items = (version, item_catalogs.mailed.content_version)
    assert plain.replayable_content_versions() == {plain.content_version_for(version, catalog) for catalog in catalogs}
    assert mailed.replayable_content_versions() == {
        mailed.content_version_for(item, catalog) for item in items for catalog in catalogs}


@pytest.mark.parametrize("version", [None, "", "A" * 64, 1])
def test_derived_versions_require_an_item_catalog_fingerprint(factory, version):
    service = factory().service
    for derive in (service.content_version_for, service.automation_content_version_for):
        with pytest.raises(ProtocolError) as error:
            derive(version)
        assert (error.value.code, error.value.status) == ("event_pass_dependencies_invalid", 503)


def test_receipt_committed_before_emblem_mail_attached_replays_its_exact_bytes(factory, item_catalogs):
    case = factory(10, items=item_catalogs.plain)
    envelope = action(case)
    response = case.service.claim(envelope, case.store)
    committed = case.store.profile()
    case.presents.item_catalog = item_catalogs.mailed
    reopen(case)
    case.bootstrap.clock = lambda: pytest.fail("Exact retry must not read the current clock")
    assert case.service.content_version != receipt_content(case)
    assert case.service.claim(envelope, case.store) == response
    assert case.store.profile() == committed


def test_forged_receipt_digest_stays_stale_after_emblem_mail_attaches(factory, item_catalogs):
    case = factory(10, items=item_catalogs.plain)
    envelope = action(case)
    case.service.claim(envelope, case.store)
    state = case.store.profile().state
    state[module.HISTORY_NAMESPACE]["receipts"][-1]["content"] = FORGED
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    case.presents.item_catalog = item_catalogs.mailed
    reopen(case)
    error = unchanged(case, lambda: case.service.claim(envelope, case.store), "event_pass_retry_stale")
    assert error.status == 409


def test_receipt_with_emblem_mail_is_stale_once_the_mail_detaches(factory, item_catalogs):
    case = factory(10, items=item_catalogs.mailed)
    envelope = action(case)
    case.service.claim(envelope, case.store)
    case.presents.item_catalog = item_catalogs.plain
    reopen(case)
    error = unchanged(case, lambda: case.service.claim(envelope, case.store), "event_pass_retry_stale")
    assert error.status == 409


def test_delivery_marker_check_is_a_public_reader_that_never_copies_the_save(factory, monkeypatch):
    case = factory(10)
    case.service.claim(action(case), case.store)
    state = case.store.profile().state
    saved = deepcopy(state)
    marker = state["event_pass_delivery"]
    for name in ("initialize_event_passes", "deepcopy"):
        monkeypatch.setattr(module, name, lambda _: pytest.fail("The check copied the whole save"))
    assert EventPassService.validate_delivery(state) is None
    assert EventPassService.validate_delivery(state, marker["request"], marker["pass_id"], marker["mail_ids"]) is None
    assert EventPassService.validate_delivery({key: value for key, value in state.items()
                                               if key != "event_pass_delivery"}) is None
    assert state == saved
    unjournaled = {key: value for key, value in state.items() if key != "event_pass_state"}
    for broken in ({**state, "event_pass_delivery": {**marker, "mail_ids": [9999]}},
                   {**state, "event_pass_delivery": {**marker, "schema_version": 2}},
                   {**state, "event_pass_delivery": None}, unjournaled):
        with pytest.raises(ProtocolError) as error:
            EventPassService.validate_delivery(broken)
        assert (error.value.code, error.value.status) == ("event_pass_history_invalid", 503)
    for broken in ({**state, "event_pass_state": {**state["event_pass_state"], "schema_version": 2}},
                   {**state, "tables": {**state["tables"], "event_pass_status": None}}):
        with pytest.raises(ProtocolError) as error:
            EventPassService.validate_delivery(broken)
        assert (error.value.code, error.value.status) == ("event_pass_state_invalid", 503)
    with pytest.raises(ProtocolError) as error:
        EventPassService.validate_delivery(state, "0" * 64, marker["pass_id"], marker["mail_ids"])
    assert error.value.code == "event_pass_history_invalid"


def test_reward_resource_update_preserves_prior_watermark_and_delivers_only_new_tier(factory):
    case = factory(10)
    case.service.claim(action(case), case.store)
    original = case.presents.rows(case.store.profile().state)
    definitions = (*rewards(), EventPassReward(9, 5, False, 0, 99, "", 8),
                   EventPassReward(10, 40, False, 0, 4, "", 9))
    changed = changed_catalog(case.service.catalog, rewards=definitions)
    reopen(case, changed)
    before = case.store.profile()
    case.store.update_state(case.bundle.possessions.credit(before.state, {case.entry.item_id: 30}), before.revision)
    case.service.claim(action(case, tick=1001), case.store)
    rows = case.presents.rows(case.store.profile().state)
    assert rows[:2] == original
    assert not any(row["Content"] == 99 for row in rows)
    assert rows[-1]["Content"] == 4 and watermark(case)["ReceivedNormalPoint"] == 40


@pytest.mark.parametrize("source", ["closed", "before_start", "after_end"])
def test_inactive_pass_does_not_change_mail_or_watermarks(factory, source):
    case = factory(active=source != "closed")
    if source == "before_start":
        case.clock.now = case.entry.start_at - timedelta(milliseconds=500)
    elif source == "after_end":
        case.clock.now = case.entry.end_at + timedelta(seconds=1)
    unchanged(case, lambda: case.service.claim(action(case), case.store), "event_pass_inactive")


@pytest.mark.parametrize("shift", [timedelta(milliseconds=500), timedelta(microseconds=999999)])
def test_claims_stay_open_for_the_whole_end_second_of_the_client_clock(factory, shift):
    case = factory()
    case.clock.now = case.entry.end_at + shift
    assert tuple(nodes(case.service.claim(action(case), case.store))) == ("SYSTEM", "PPT", "EventPassStatus")
    assert watermark(case)["ReceivedNormalPoint"] == 30


def test_pass_dates_and_the_clock_share_the_client_float32_grid(factory):
    opening, closing = datetime(2026, 11, 1, 6, 0, 7, tzinfo=JST), datetime(2026, 11, 1, 12, 0, 8, tzinfo=JST)
    catalog = changed_catalog(factory().service.catalog, start_at=opening, end_at=closing)
    entry = catalog.passes[0]
    assert (entry.start_at, entry.end_at) == (opening, closing)
    assert (entry.native_start_at - opening, closing - entry.native_end_at) == (timedelta(milliseconds=4),) * 2
    assert catalog.is_eligible(entry, opening) and catalog.is_eligible(entry, closing + timedelta(microseconds=999999))
    assert not catalog.is_eligible(entry, opening - timedelta(microseconds=1))
    assert not catalog.is_eligible(entry, closing + timedelta(seconds=1))


@pytest.mark.parametrize("points", [0, 9])
def test_missing_or_insufficient_points_do_not_advance_any_track(factory, points):
    case = factory(points, premium=True)
    assert unclaimed(case, action(case)) == []


def test_nothing_claimable_reply_is_a_logged_read_without_receipt(factory, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    case = factory(20)
    case.service.claim(action(case), case.store)
    history = case.store.profile().state["event_pass_history"]
    envelope = action(case, tick=2000)
    reply = case.service.claim(envelope, case.store)
    assert unclaimed(case, envelope) == [watermark(case)]
    assert case.service.claim(envelope, case.store) == reply
    assert case.store.profile().state["event_pass_history"] == history
    assert any(case.entry.item_id in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)
    before = case.store.profile()
    case.store.update_state(case.bundle.possessions.credit(before.state, {case.entry.item_id: 10}), before.revision)
    result = nodes(case.service.claim(action(case, tick=1500), case.store))
    assert tuple(result) == ("SYSTEM", "PPT", "EventPassStatus")
    assert watermark(case)["ReceivedNormalPoint"] == 30
    saved = case.store.profile().state["event_pass_history"]
    assert saved["processed_keys"] == [*history["processed_keys"], saved["receipts"][-1]["request"]]


@pytest.mark.parametrize("number", [42, 137, 159])
def test_each_encoded_preflight_failure_rolls_back_every_delivery(factory, monkeypatch, number):
    case = factory(premium=True)
    original = module.encode_response
    target = case.service._profiles[number]
    def fail(profile, payload, *args, **kwargs):
        if profile == target:
            raise ProtocolError("forced_capacity", "Test preflight capacity", 503)
        return original(profile, payload, *args, **kwargs)
    monkeypatch.setattr(module, "encode_response", fail)
    unchanged(case, lambda: case.service.claim(action(case), case.store), "forced_capacity")


def test_later_unsupported_reward_rolls_back_earlier_successful_enqueue(factory):
    bad = EventPassReward(20, 30, False, 22, 1, "item_smn_ticket_10001", 8)
    case = factory(definitions=(*rewards()[:4], bad))
    error = unchanged(case, lambda: case.service.claim(action(case), case.store))
    assert error.status == 501
    assert case.presents.rows(case.store.profile().state) == []


def test_reward_item_beyond_the_native_cell_is_unsupported_instead_of_unreplayable(factory, monkeypatch):
    item = "x" * (NATIVE_CELL_BYTES + 1)
    case = factory(definitions=(*rewards()[:4], EventPassReward(20, 10, False, 22, 1, item, 8)))
    accept_stored_item(monkeypatch, case.presents, item)
    error = unchanged(case, lambda: case.service.claim(action(case), case.store), "event_pass_reward_unsupported")
    assert error.status == 501
    assert case.presents.rows(case.store.profile().state) == []


def test_mailbox_full_rolls_back_watermark_and_present_identity(factory):
    case = factory(10)
    profile = case.store.profile()
    state, _ = case.presents.enqueue(profile.state, [{"kind": "unit", "template_id": 6,
        "count": MAX_PRESENTS, "description": "capacity"}])
    case.store.update_state(state, profile.revision)
    unchanged(case, lambda: case.service.claim(action(case), case.store), "present_capacity")


def test_present_id_exhaustion_is_atomic(factory):
    case = factory(10)
    change(case, lambda state: state.update(presents_schema=1, last_present_id=0x7FFFFFFF))
    unchanged(case, lambda: case.service.claim(action(case), case.store), "present_identity_exhausted")


def test_cooking_amount_splits_single_use_letters(factory):
    case = factory(10, definitions=(EventPassReward(1, 10, False, 22, 3, COOKING, 0),))
    case.service.claim(action(case), case.store)
    rows = case.presents.rows(case.store.profile().state)
    assert len(rows) == 3 and all(row["ItemID"] == COOKING and row["Content"] == 1 for row in rows)


def test_cas_retry_preserves_unrelated_concurrent_write_and_reuses_one_clock(factory, monkeypatch):
    case = factory(10)
    original = case.store.update_state
    calls = []
    def race(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = case.store.profile()
            winner = deepcopy(current.state)
            winner["sentinel"]["keep"].append(2)
            original(winner, current.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", race)
    case.service.claim(action(case), case.store)
    assert len(calls) == 2 and case.clock.calls == 1
    assert case.store.profile().state["sentinel"] == {"keep": [1, 2]}
    assert len(case.presents.rows(case.store.profile().state)) == 2


def test_concurrent_same_request_returns_winner_receipt_without_second_delivery(factory, monkeypatch):
    case = factory(10)
    envelope = action(case)
    original = case.store.update_state
    calls = []
    def winner(state, revision):
        calls.append(1)
        original(state, revision)
        raise ValueError("Another request committed first")
    monkeypatch.setattr(case.store, "update_state", winner)
    response = case.service.claim(envelope, case.store)
    assert len(calls) == 1
    assert case.store.profile().revision == 1
    assert len(nodes(response)["PPT"].children[0].values) == 2
    assert len(case.presents.rows(case.store.profile().state)) == 2


def test_exhausted_cas_retries_never_commit_candidate(factory, monkeypatch):
    case = factory(10)
    calls = []
    def conflict(state, revision):
        calls.append(1)
        raise ValueError("Conflict")
    monkeypatch.setattr(case.store, "update_state", conflict)
    unchanged(case, lambda: case.service.claim(action(case), case.store), "profile_changed")
    assert len(calls) == 3 and case.clock.calls == 1


def test_rolled_back_client_clock_claims_newly_reached_tiers(factory):
    case = factory(10)
    for tick, reached in ((0x7FFFFFFF, 10), (-0x80000000, 20), (0x7FFFFFFE, 30)):
        if reached > 10:
            profile = case.store.profile()
            case.store.update_state(case.bundle.possessions.credit(profile.state, {case.entry.item_id: 10}), profile.revision)
        case.service.claim(action(case, tick=tick), case.store)
        assert watermark(case)["ReceivedNormalPoint"] == reached


def test_receipt_eviction_does_not_allow_old_packet_to_claim_again(factory, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    case = factory(10)
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    old = action(case)
    case.service.claim(old, case.store)
    profile = case.store.profile()
    case.store.update_state(case.bundle.possessions.credit(profile.state, {case.entry.item_id: 10}), profile.revision)
    case.service.claim(action(case, tick=1001), case.store)
    assert len(case.store.profile().state["event_pass_history"]["receipts"]) == 1
    unchanged(case, lambda: case.service.claim(old, case.store), "event_pass_request_stale")
    assert any("request 159" in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)


def test_new_identity_beyond_the_native_cell_is_refused_and_logged(factory, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    case = factory(10)

    def oversized(*_, **__):
        raise ProtocolError(IDENTITY_UNREPLAYABLE, "New event passes need identities that fit the native cell", 503)

    monkeypatch.setattr(module, "mark_event_pass", oversized)
    error = unchanged(case, lambda: case.service.claim(action(case), case.store), IDENTITY_UNREPLAYABLE)
    assert error.status == 503
    assert any("request 159" in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)


def test_a_changed_identity_refusal_names_the_client_aftermath_and_the_console_repair(factory, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    case = factory(10)
    pass_id = case.entry.item_id
    case.service.claim(action(case), case.store)
    change(case, lambda state: state[LEDGER_KEY]["passes"][pass_id].update(definition=FORGED))
    profile = case.store.profile()
    case.store.update_state(case.bundle.possessions.credit(profile.state, {pass_id: 10}), profile.revision)
    error = unchanged(case, lambda: case.service.claim(action(case, tick=1001), case.store), DEFINITION_CHANGED)
    assert error.status == 503
    expected = (f"Refused request 159 for event pass {pass_id} with {DEFINITION_CHANGED}; the native client "
                f"{REFUSAL_AFTERMATH}, so run {REBIND_OPERATION} on the console")
    assert expected in [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]


def test_status_declares_request_order_and_identity_bounds(factory):
    result = factory().service.status()
    assert result["processed_request_limit"] == ledger.PROCESSED_LIMIT
    assert result["request_order"] == ledger.REQUEST_ORDER
    assert f"{NATIVE_CELL_BYTES}-byte native cell" in result["identity_policy"]


def test_watermark_history_upgrades_and_replays_its_receipt(factory):
    case = factory(10)
    envelope = action(case, tick=2000)
    response = case.service.claim(envelope, case.store)
    state = case.store.profile().state
    history = state["event_pass_history"]
    state["event_pass_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
        "seen_keys": list(history["processed_keys"]),
        "receipts": [{**entry, "client_time": 2000} for entry in history["receipts"]]}
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    saved = case.store.profile()
    assert case.service.claim(envelope, case.store) == response
    assert case.store.profile() == saved
    profile = case.store.profile()
    case.store.update_state(case.bundle.possessions.credit(profile.state, {case.entry.item_id: 10}), profile.revision)
    case.service.claim(action(case, tick=1000), case.store)
    history = case.store.profile().state["event_pass_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]
    assert watermark(case)["ReceivedNormalPoint"] == 20


@pytest.mark.parametrize("location", ["native_watermark", "journal", "delivery", "response"])
def test_corrupted_persisted_state_is_not_repaired_or_replayed(factory, location):
    case = factory(10)
    envelope = action(case)
    case.service.claim(envelope, case.store)
    state = deepcopy(case.store.profile().state)
    if location == "native_watermark":
        state["tables"]["event_pass_status"][0]["ReceivedNormalPoint"] += 1
    elif location == "journal":
        state["event_pass_state"]["passes"][case.entry.item_id]["deliveries"]["normal"]["10"]["mail_ids"] = []
    elif location == "delivery":
        state["event_pass_delivery"]["mail_ids"] = [9999]
    else:
        state["event_pass_history"]["receipts"][0]["response"] = "invalid"
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    error = unchanged(case, lambda: case.service.claim(envelope, case.store))
    assert error.status == 503


@pytest.mark.parametrize("selection", [(), ("missing",), ("a", "b"), ("duplicate", "duplicate")])
def test_unknown_empty_and_multi_pass_selection_are_rejected(factory, selection):
    case = factory()
    unchanged(case, lambda: case.service.claim(action(case, identifiers=selection), case.store))


def test_business_fields_are_not_ignored(factory):
    case = factory()
    unchanged(case, lambda: case.service.claim(action(case, extra=(Column("NUM", "I", (1,)),)), case.store))

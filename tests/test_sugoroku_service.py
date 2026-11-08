from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
import logging
from types import SimpleNamespace

import pytest

import aigis_ps.request_ledger as ledger
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.crystal_spending import read_spending, spend_crystals
from aigis_ps.crystal_wallet import credit_free_crystals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.present_items import PresentItemCatalog
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.storage import Store
import aigis_ps.sugoroku_service as module
from aigis_ps.sugoroku_service import SugorokuService
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 10, 12, tzinfo=JST)
FORGED = "f" * 64


@pytest.fixture(scope="module")
def plain_items(bundle):
    mailed = bundle.presents.item_catalog
    plain = PresentItemCatalog(bundle.presents.possessions, bundle.native_cdn.assets.read("ItemConfigTable.atb")[0])
    assert plain.content_version in mailed.replayable_content_versions() - {mailed.content_version}
    return plain


@pytest.fixture
def factory(bundle, tmp_path):
    serial = 0

    def build(spent=800, now=NOW, *, legacy=False):
        nonlocal serial
        serial += 1
        bootstrap = copy(bundle.bootstrap)
        clock = SimpleNamespace(now=now)
        bootstrap.clock = lambda: clock.now
        service = SugorokuService(bootstrap, bundle.player_state, bundle.presents, bundle.sugoroku.catalog)
        bootstrap.monthly_state_provider = service.prepare_month
        state = bootstrap.initial_state()
        state = credit_free_crystals(state, max(1, spent), now.date().toordinal() - 1)
        state = spend_crystals(state, spent, now)
        if legacy:
            state["crystal_spending"]["baseline_source"] = "legacy_explicit"
        state = bundle.initialize(state)
        store = Store(tmp_path / str(serial) / "save.sqlite3", state)
        return SimpleNamespace(service=service, bootstrap=bootstrap, clock=clock, store=store, bundle=bundle)
    return build


def action(case, operation=63, identifiers=(2,), *, tick=1000):
    return request(case.store, operation, (Container("REWARD", (Column("ID", "I", identifiers),)),), tick=tick)


def nodes(payload):
    return {node.name: node for node in loads(payload).children}


def statuses(payload):
    node = nodes(payload)["REWARD"]
    assert tuple((column.name, column.type) for column in node.children) == (("ID", "I"), ("Status", "I"))
    return dict(zip(*(column.values for column in node.children)))


def unchanged(case, callback, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert case.store.profile() == before
    if code is not None:
        assert caught.value.code == code
    return caught.value


def unclaimed(case, envelope, caplog):
    before = case.store.profile()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        response = case.service.claim(envelope, case.store)
    assert set(nodes(response)) == {"SYSTEM", "REWARD"}
    assert case.store.profile() == before
    assert any("Reward board 63 found nothing claimable" in line for line in caplog.messages)
    return statuses(response)


def overwrite(case, state):
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def rebuild(case):
    return SugorokuService(case.bootstrap, case.bundle.player_state, case.bundle.presents, case.bundle.sugoroku.catalog)


def test_read_exposes_only_reached_active_rewards_and_empty_snapshot(factory):
    empty = factory(0)
    response = empty.service.read(request(empty.store, 62, ()), empty.store)
    assert statuses(response) == {} and set(nodes(response)) == {"SYSTEM", "REWARD"}
    case = factory(20)
    response = case.service.read(request(case.store, 62, ()), case.store)
    expected = {row.reward_id: 0 for row in case.service.catalog.active(NOW) if row.consume_crystal <= 20}
    assert statuses(response) == expected
    before = case.store.profile()
    assert case.service.read(request(case.store, 62, ()), case.store) == response
    assert case.store.profile() == before


def test_mark_read_does_not_grant_or_downgrade_claimed_rewards(factory):
    case = factory()
    result = case.service.mark_read(action(case, 64, (2, 3)), case.store)
    assert statuses(result) == {2: 1, 3: 1}
    assert set(nodes(result)) == {"SYSTEM", "REWARD"}
    assert case.store.profile().state["tables"]["presents"] == []
    case.service.claim(action(case, 63, (2,), tick=1001), case.store)
    result = case.service.mark_read(action(case, 64, (2, 3), tick=1002), case.store)
    assert statuses(result) == {2: 2, 3: 1}


def test_every_supported_active_reward_mails_once_without_immediate_payment(factory, caplog):
    case = factory()
    supported = []
    for row in case.service.catalog.active(NOW):
        try:
            reward = case.service._reward(row)
            case.bundle.presents.enqueue(case.store.profile().state, [reward])
        except ProtocolError:
            continue
        supported.append(row.reward_id)
    assert len(supported) == 45
    before = case.store.profile()
    envelope = action(case, identifiers=tuple(supported))
    response = case.service.claim(envelope, case.store)
    saved = case.store.profile()
    assert statuses(response) == dict.fromkeys(supported, 2)
    assert saved.state["player_state"] == before.state["player_state"]
    assert saved.state["tables"]["game_param"] == before.state["tables"]["game_param"]
    assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert saved.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    mail = saved.state["tables"]["presents"]
    assert {row["Type"] for row in mail} == {1, 2, 22}
    assert [row["Content"] for row in mail if row["Type"] == 2] == [30000] * 3
    assert case.service.claim(envelope, case.store) == response
    assert case.store.profile() == saved
    assert unclaimed(case, action(case, identifiers=(supported[0],), tick=1001), caplog) == {supported[0]: 2}


def test_empty_read_acknowledgement_is_a_recorded_noop(factory):
    case = factory()
    envelope = request(case.store, 64, (Container("REWARD", ()),))
    response = case.service.mark_read(envelope, case.store)
    assert statuses(response) == {} and set(nodes(response)) == {"SYSTEM", "REWARD"}
    assert case.store.profile().state["tables"]["presents"] == []
    before = case.store.profile()
    assert case.service.mark_read(envelope, case.store) == response
    assert case.store.profile() == before


def test_empty_claim_is_answered_without_a_receipt_and_never_claims_all(factory, caplog):
    case = factory()
    envelope = request(case.store, 63, (Container("REWARD", ()),))
    assert unclaimed(case, envelope, caplog) == {}
    assert unclaimed(case, envelope, caplog) == {}
    assert "sugoroku_history" not in case.store.profile().state


@pytest.mark.parametrize("identifiers", ((0,), (-1,), (2, 2)))
def test_invalid_identifiers_do_not_change_state(factory, identifiers):
    case = factory()
    unchanged(case, lambda: case.service.claim(action(case, identifiers=identifiers), case.store), "invalid_request_fields")


@pytest.mark.parametrize("identifiers", ((999999,), (26,)))
def test_unknown_and_expired_ids_alone_answer_without_claiming(factory, caplog, identifiers):
    case = factory()
    assert unclaimed(case, action(case, identifiers=identifiers), caplog) == {}


@pytest.mark.parametrize("identifiers", ((2, 26), (999999, 2)))
def test_unknown_and_expired_ids_are_omitted_while_the_rest_is_claimed(factory, caplog, identifiers):
    case = factory()
    envelope = action(case, identifiers=identifiers)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        first = case.service.claim(envelope, case.store)
    assert statuses(first) == {2: 2}
    assert any("skips [" in line for line in caplog.messages)
    saved = case.store.profile()
    receipt = saved.state["sugoroku_history"]["receipts"][-1]
    assert (receipt["ids"], receipt["granted"]) == ([2], [2])
    assert len(saved.state["tables"]["presents"]) == 1
    assert case.service.claim(envelope, case.store) == first and case.store.profile() == saved


def test_unreached_ids_answer_claims_unchanged_but_cannot_be_marked(factory, caplog):
    case = factory(0)
    assert unclaimed(case, action(case), caplog) == {}
    unchanged(case, lambda: case.service.mark_read(action(case, 64), case.store), "sugoroku_reward_unavailable")


def test_tier_selection_claims_its_open_reward_and_reports_the_claimed_one(factory, caplog):
    case = factory()
    case.service.claim(action(case, identifiers=(2,)), case.store)
    envelope = action(case, identifiers=(2, 3), tick=1001)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        response = case.service.claim(envelope, case.store)
    assert statuses(response) == {2: 2, 3: 2}
    saved = case.store.profile()
    receipt = saved.state["sugoroku_history"]["receipts"][-1]
    assert (receipt["ids"], receipt["granted"]) == ([2, 3], [3])
    assert nodes(response)["PPT"].children[0].values == tuple(receipt["mail_ids"])
    assert len(saved.state["tables"]["presents"]) == 2
    assert case.service.claim(envelope, case.store) == response and case.store.profile() == saved
    assert rebuild(case).claim(envelope, Store(case.store.path, {})) == response


@pytest.mark.parametrize("identifiers", ((1,), (10,), (1, 10)))
def test_incomplete_item_tickets_alone_keep_the_refusal(factory, identifiers):
    case = factory()
    error = unchanged(case, lambda: case.service.claim(action(case, identifiers=identifiers), case.store))
    assert error.status == 501


@pytest.mark.parametrize("identifiers", ((2, 1), (1, 2, 10)))
def test_incomplete_item_tickets_stay_reached_while_the_batch_claims_the_rest(factory, identifiers):
    case = factory()
    expected_mail = case.bundle.presents.preview([case.service._reward(case.service.catalog.get(2))], 0)
    envelope = action(case, identifiers=identifiers)
    first = case.service.claim(envelope, case.store)
    saved = case.store.profile()
    assert statuses(first) == {identifier: 2 if identifier == 2 else 0 for identifier in identifiers}
    mail = case.bundle.presents.rows(saved.state)
    assert [{key: value for key, value in row.items() if key != "PresentID"} for row in mail] == expected_mail
    assert nodes(first)["PPT"].children[0].values == tuple(row["PresentID"] for row in mail)
    receipt = saved.state["sugoroku_history"]["receipts"][-1]
    assert receipt["ids"] == list(identifiers) and receipt["mail_ids"] == [row["PresentID"] for row in mail]
    assert case.service.claim(envelope, case.store) == first and case.store.profile() == saved
    unchanged(case, lambda: case.service.claim(action(case, identifiers=(1,), tick=1001), case.store))
    marked = case.service.mark_read(action(case, 64, (1,), tick=1002), case.store)
    assert statuses(marked) == {1: 1}


def test_legacy_baseline_does_not_backfill_or_hide_later_spending(factory):
    case = factory(20, legacy=True)
    response = case.service.read(request(case.store, 62, ()), case.store)
    assert statuses(response) and set(statuses(response).values()) == {2}
    before = case.store.profile()
    updated = credit_free_crystals(before.state, 20, NOW.date().toordinal() - 1)
    updated = spend_crystals(updated, 20, NOW)
    case.store.update_state(updated, before.revision)
    response = case.service.read(request(case.store, 62, ()), case.store)
    for row in case.service.catalog.active(NOW):
        if row.consume_crystal <= 20:
            assert statuses(response)[row.reward_id] == 2
        elif row.consume_crystal <= 40:
            assert statuses(response)[row.reward_id] == 0
    assert case.store.profile().state["tables"]["presents"] == []


def test_full_mailbox_and_identity_exhaustion_are_atomic(factory):
    case = factory()
    before = case.store.profile()
    state, _ = case.bundle.presents.enqueue(before.state, [{"kind": "unit", "template_id":
        case.bundle.presents.initial_unit_template(2), "count": MAX_PRESENTS, "description": "test"}])
    case.store.update_state(state, before.revision)
    unchanged(case, lambda: case.service.claim(action(case), case.store), "present_capacity")
    state["tables"]["presents"] = []
    state["last_present_id"] = 0x7FFFFFFF
    case.store.update_state(state, case.store.profile().revision)
    unchanged(case, lambda: case.service.claim(action(case), case.store), "present_identity_exhausted")


def test_cas_retry_merges_concurrent_read_and_grants_once(factory, monkeypatch):
    case = factory()
    original = case.store.update_state
    seen = []

    def conflict(state, revision):
        seen.append(deepcopy(state))
        if len(seen) == 1:
            competing = case.store.profile()
            concurrent = deepcopy(competing.state)
            concurrent["unrelated"] = {"preserved": True}
            original(concurrent, competing.revision)
            raise ValueError("Concurrent save")
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", conflict)
    response = case.service.claim(action(case), case.store)
    assert len(seen) == 2 and statuses(response) == {2: 2}
    assert case.store.profile().state["unrelated"] == {"preserved": True}
    assert len(case.store.profile().state["tables"]["presents"]) == 1


def test_stale_receipt_never_awards_again_and_an_older_client_time_is_a_new_request(factory):
    case = factory()
    envelope = action(case)
    case.service.claim(envelope, case.store)
    case.service.mark_read(action(case, 64, (3,), tick=1001), case.store)
    unchanged(case, lambda: case.service.claim(envelope, case.store), "sugoroku_retry_stale")
    assert statuses(case.service.claim(action(case, identifiers=(4,), tick=-0x80000000), case.store)) == {4: 2}
    history = case.store.profile().state["sugoroku_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION and len(history["processed_keys"]) == 3
    assert all("client_time" not in entry for entry in history["receipts"])


def test_evicted_receipt_stays_processed(factory, monkeypatch):
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    case = factory()
    first = action(case, identifiers=(2,))
    case.service.claim(first, case.store)
    case.service.claim(action(case, identifiers=(3,), tick=1001), case.store)
    assert len(case.store.profile().state["sugoroku_history"]["receipts"]) == 1
    unchanged(case, lambda: case.service.claim(first, case.store), "sugoroku_request_stale")


def test_watermark_history_upgrades_and_keeps_replaying_its_receipts(factory):
    case = factory()
    envelope = action(case, identifiers=(2, 1))
    response = case.service.claim(envelope, case.store)
    state = deepcopy(case.store.profile().state)
    history = state["sugoroku_history"]
    state["sugoroku_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 1000,
        "seen_keys": list(history["processed_keys"]),
        "receipts": [{**{key: value for key, value in entry.items() if key != "granted"}, "client_time": 1000}
                     for entry in history["receipts"]]}
    overwrite(case, state)
    legacy = case.store.profile()
    assert case.service.claim(envelope, case.store) == response
    assert case.store.profile() == legacy
    assert statuses(case.service.claim(action(case, identifiers=(3,), tick=999), case.store)) == {3: 2}
    upgraded = case.store.profile().state["sugoroku_history"]
    assert upgraded["schema_version"] == ledger.SCHEMA_VERSION
    assert [entry["granted"] for entry in upgraded["receipts"]] == [None, [3]]


@pytest.mark.parametrize("damage", ["foreign", "unclaimed", "read"])
def test_receipt_grants_must_match_the_reward_ledger(factory, damage):
    case = factory()
    envelope = action(case, identifiers=(2, 3)) if damage != "read" else action(case, 64, (2, 3))
    case.service.handlers()[envelope.profile.registration_keys[0]](envelope, case.store)
    state = deepcopy(case.store.profile().state)
    receipt = state["sugoroku_history"]["receipts"][-1]
    if damage == "foreign":
        receipt["granted"] = [2, 4]
    elif damage == "unclaimed":
        state["sugoroku"]["periods"][receipt["period"]]["records"]["3"].update(status=1, mail_ids=[])
        receipt["state"] = module._state_digest(state)
    else:
        receipt["granted"] = [2]
    overwrite(case, state)
    unchanged(case, lambda: case.service.handlers()[envelope.profile.registration_keys[0]](envelope, case.store),
              "sugoroku_history_invalid")


def test_remaining_refusals_are_logged_with_their_request_number(factory, caplog):
    case = factory(0)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        unchanged(case, lambda: case.service.mark_read(action(case, 64), case.store), "sugoroku_reward_unavailable")
    assert any("Reward board 64 refused with sugoroku_reward_unavailable" in line for line in caplog.messages)


@pytest.mark.parametrize("registration", (33, 35))
def test_month_refresh_projects_c1_and_preserves_old_mail(factory, registration):
    case = factory(now=datetime(2026, 11, 30, 12, tzinfo=JST))
    case.service.claim(action(case), case.store)
    old = case.store.profile()
    case.clock.now = datetime(2026, 12, 1, 4, tzinfo=JST)
    unchanged(case, lambda: case.service.read(request(case.store, 62, ()), case.store))
    response = case.bootstrap.handlers()[registration](request(case.store, registration, ()), case.store)
    assert response
    saved = case.store.profile()
    ledger = read_spending(saved.state)
    assert (ledger["period"], ledger["current_spent"], ledger["previous_spent"]) == ("2026-12", 0, 800)
    assert saved.state["tables"]["presents"] == old.state["tables"]["presents"]
    assert statuses(case.service.read(request(case.store, 62, ()), case.store)) == {}
    revision = case.store.profile().revision
    case.bootstrap.handlers()[registration](request(case.store, registration, ()), case.store)
    assert case.store.profile().revision == revision


def test_cross_month_old_claim_and_backwards_clock_are_rejected(factory):
    case = factory()
    envelope = action(case)
    case.service.claim(envelope, case.store)
    case.clock.now = datetime(2026, 12, 1, 4, tzinfo=JST)
    unchanged(case, lambda: case.service.claim(envelope, case.store), "sugoroku_retry_stale")
    case.clock.now = NOW - timedelta(seconds=1)
    unchanged(case, lambda: case.service.read(request(case.store, 62, ()), case.store))


def test_dependency_change_rejects_cached_success(factory):
    case = factory()
    envelope = action(case)
    case.service.claim(envelope, case.store)
    case.service.content_version = FORGED
    unchanged(case, lambda: case.service.claim(envelope, case.store), "sugoroku_dependencies_changed")


def test_reward_receipt_from_before_emblem_mail_replays_its_bytes_after_it_attaches(factory, bundle, monkeypatch,
                                                                                   plain_items):
    case = factory()
    mailed = bundle.presents.item_catalog
    monkeypatch.setattr(bundle.presents, "item_catalog", plain_items)
    committed = rebuild(case)
    envelope = action(case)
    response = committed.claim(envelope, case.store)
    saved = case.store.profile()
    monkeypatch.setattr(bundle.presents, "item_catalog", mailed)
    attached = rebuild(case)
    assert attached.content_version == case.service.content_version != committed.content_version
    assert saved.state["sugoroku_history"]["receipts"][-1]["content"] == committed.content_version
    assert attached.claim(envelope, case.store) == response
    assert case.store.profile() == saved


def test_current_reward_receipt_replays_without_enumerating_item_variants(factory, bundle, monkeypatch):
    case = factory()
    envelope = action(case)
    response = case.service.claim(envelope, case.store)
    saved = case.store.profile()
    monkeypatch.setattr(bundle.presents.item_catalog, "replayable_content_versions",
                        lambda: pytest.fail("A receipt of the current configuration needs no variants"))
    assert case.service.claim(envelope, case.store) == response
    assert case.store.profile() == saved


def test_forged_reward_receipt_digest_stays_stale_after_emblem_mail_attaches(factory, bundle, monkeypatch,
                                                                            plain_items):
    case = factory()
    mailed = bundle.presents.item_catalog
    monkeypatch.setattr(bundle.presents, "item_catalog", plain_items)
    envelope = action(case)
    rebuild(case).claim(envelope, case.store)
    forged = deepcopy(case.store.profile().state)
    forged["sugoroku_history"]["receipts"][-1]["content"] = FORGED
    overwrite(case, forged)
    monkeypatch.setattr(bundle.presents, "item_catalog", mailed)
    attached = rebuild(case)
    unchanged(case, lambda: attached.claim(envelope, case.store), "sugoroku_retry_stale")


def test_reward_receipt_with_emblem_mail_is_stale_once_it_detaches(factory, bundle, monkeypatch, plain_items):
    case = factory()
    envelope = action(case)
    case.service.claim(envelope, case.store)
    monkeypatch.setattr(bundle.presents, "item_catalog", plain_items)
    detached = rebuild(case)
    unchanged(case, lambda: detached.claim(envelope, case.store), "sugoroku_retry_stale")


def test_expired_same_month_retry_is_stale_not_corrupt_history(factory):
    case = factory(now=datetime(2023, 4, 1, 3, 59, 58, tzinfo=JST))
    envelope = action(case, 64, (26,))
    case.service.mark_read(envelope, case.store)
    case.clock.now += timedelta(seconds=1)
    unchanged(case, lambda: case.service.mark_read(envelope, case.store), "sugoroku_retry_stale")


def test_cached_mail_list_must_match_the_reward_ledger(factory):
    case = factory()
    envelope = action(case)
    case.service.claim(envelope, case.store)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    receipt = state["sugoroku_history"]["receipts"][-1]
    receipt["mail_ids"] = []
    receipt["response"] = case.service._response(63, profile, state, NOW, (2,), ()).decode()
    original = case.store.profile
    case.store.profile = lambda: replace(original(), state=state)
    unchanged(case, lambda: case.service.claim(envelope, case.store), "sugoroku_history_invalid")

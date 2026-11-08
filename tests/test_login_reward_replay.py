from copy import copy, deepcopy
from dataclasses import replace

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.login_cooking_state import SENDER_FIELD, read_cooking
from aigis_ps.login_reward_service import (AFFECTION_SENDER, GIFT_BOX_SERVICES, ITEM_SENDER, LETSTD_SOURCE,
                                           LISTED_SCHEMA, SOURCE_SERVICES, SOURCES, LoginRewardService)
from test_event_pass_service import item_catalogs
from test_letstd_catalog import native_sources
from test_letstd_dispatch import CLOSED, OSHIRORE_CAMPAIGN, children, dispatch, login_request, replace_without_revision
from test_login_cooking_service import login_cooking, rebuild, sender_case
from test_login_reward_service import change, factory, fail, message
from test_quest_service import bundle
from test_tower_service import without_enabled
from test_vote_catalog import vote_sources
from test_vote_transactions import NOW, catalog


EARLIER_SERVICES = tuple(SOURCE_SERVICES[name] for name in SOURCES[:-1])
FIXED_CASES = (*((7, name) for name in EARLIER_SERVICES), *((6, name) for name in EARLIER_SERVICES[:-1]))
GIFT_BOX_SCHEMA = 5
ABSENT_TICKETS = (("item_tickets",), ("selection_tickets",), ("item_tickets", "selection_tickets"))
SENDER_SWITCHES = ((ITEM_SENDER, AFFECTION_SENDER), (AFFECTION_SENDER, ITEM_SENDER))
FORGED = "f" * 64


def commit(service, store):
    envelope = login_request(store)
    return envelope, service.receive(envelope, store), store.profile()


def earlier_mailbox(presents, **dependencies):
    earlier = copy(presents)
    for name, dependency in dependencies.items():
        setattr(earlier, name, dependency)
    return earlier


def gift_box_login(dispatch, bundle, presents):
    # Rebuilt on an earlier mailbox, every gift box service fingerprints the dependencies that mailbox had.
    services = {**{name: getattr(bundle, name) for name in GIFT_BOX_SERVICES}, "letstd": dispatch.letstd(CLOSED)}
    return LoginRewardService(bundle.bootstrap, bundle.player_state, presents, bundle.login_rewards.catalog,
                              **{name: type(service)(service.catalog, presents, service.rules)
                                 for name, service in services.items()})


def mixed_digest(base, services):
    mixed = copy(services)
    mixed.content_version = base.content_version
    mixed._record_receive_contents()
    return mixed._letstd_receive_content_version


def assert_replays(service, envelope, payload, store, saved, clock):
    assert service.initialize(deepcopy(saved.state), NOW) == saved.state
    calls = clock.calls
    assert service.receive(envelope, store) == payload
    assert store.profile() == saved and clock.calls == calls


def assert_stale(service, envelope, store, saved, content):
    forged = deepcopy(saved.state)
    forged["login_reward_history"]["receipts"][-1]["content"] = content
    replace_without_revision(store, forged)
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        service.receive(envelope, store)
    assert (caught.value.code, caught.value.status) == ("login_reward_retry_stale", 409)
    assert store.profile() == before


@pytest.mark.parametrize("schema, name", FIXED_CASES)
def test_receipt_replays_after_an_earlier_fixed_service_is_attached(dispatch, schema, name):
    store = dispatch.store()
    earlier = dispatch.login(schema=schema, without=(name,))
    envelope, payload, saved = commit(earlier, store)
    assert saved.state["login_reward_delivery"]["schema_version"] == schema
    attached = dispatch.login(schema=schema)
    assert attached.receive_content_version != earlier.receive_content_version
    assert_replays(attached, envelope, payload, store, saved, dispatch.clock)


@pytest.mark.parametrize("schema", (1, 5, 7))
def test_listed_receipt_replays_after_vote_cooking_is_attached(dispatch, schema):
    store = dispatch.store()
    letstd = dispatch.letstd(OSHIRORE_CAMPAIGN)
    envelope, payload, saved = commit(dispatch.login(letstd, schema, without=("vote_cooking",)), store)
    marker = saved.state["login_reward_delivery"]
    assert (marker["schema_version"], marker["sections"]) == (LISTED_SCHEMA, [*SOURCES[:schema - 1], LETSTD_SOURCE])
    assert marker[LETSTD_SOURCE]
    assert_replays(dispatch.login(letstd, 7), envelope, payload, store, saved, dispatch.clock)


def test_two_column_inventory_receipt_replays_its_saved_bytes(dispatch):
    store = dispatch.store()
    service = dispatch.login(schema=7)
    envelope, payload, saved = commit(service, store)
    legacy = without_enabled(payload)
    assert legacy != payload
    assert [column.name for column in children(legacy)["POSSESSION"].children] == ["ItemID", "Count"]
    rewritten = deepcopy(saved.state)
    rewritten["login_reward_history"]["receipts"][-1]["response"] = legacy.decode()
    replace_without_revision(store, rewritten)
    assert_replays(service, envelope, legacy, store, store.profile(), dispatch.clock)


def test_fixed_digest_without_its_last_source_service_is_stale(dispatch):
    store = dispatch.store()
    service = dispatch.login(schema=7)
    envelope, _, saved = commit(service, store)
    assert_stale(service, envelope, store, saved, service._cooking_receive_content_version)


def test_receipt_of_a_removed_service_is_stale(dispatch):
    store = dispatch.store()
    envelope, _, saved = commit(dispatch.login(schema=7), store)
    assert saved.state["login_reward_delivery"]["purchased_login_grants"] == []
    with pytest.raises(ProtocolError) as caught:
        dispatch.login(schema=7, without=("purchased_logins",)).receive(envelope, store)
    assert (caught.value.code, caught.value.status) == ("login_reward_retry_stale", 409)
    assert store.profile() == saved


def test_listed_digest_with_an_unrecorded_service_is_stale(dispatch):
    store = dispatch.store()
    letstd = dispatch.letstd(CLOSED)
    envelope, _, saved = commit(dispatch.login(letstd), store)
    assert saved.state["login_reward_delivery"]["sections"] == [LETSTD_SOURCE]
    current = dispatch.login(letstd, 7)
    assert_stale(current, envelope, store, saved, current._letstd_receive_content_version)


@pytest.mark.parametrize("absent", ABSENT_TICKETS)
def test_receipt_replays_after_its_absent_ticket_services_attach(dispatch, bundle, absent):
    store = dispatch.store()
    earlier = gift_box_login(dispatch, bundle, earlier_mailbox(bundle.presents, **dict.fromkeys(absent)))
    envelope, payload, saved = commit(earlier, store)
    assert saved.state["login_reward_delivery"]["sections"] == [*SOURCES[:GIFT_BOX_SCHEMA - 1], LETSTD_SOURCE]
    current = dispatch.login(dispatch.letstd(CLOSED), GIFT_BOX_SCHEMA)
    assert saved.state["login_reward_history"]["receipts"][-1]["content"] != current._letstd_receive_content_version
    assert_replays(current, envelope, payload, store, saved, dispatch.clock)


def test_receipt_replays_after_emblem_mail_attaches(dispatch, bundle, item_catalogs):
    store = dispatch.store()
    earlier = gift_box_login(dispatch, bundle, earlier_mailbox(bundle.presents, item_catalog=item_catalogs.plain))
    envelope, payload, saved = commit(earlier, store)
    current = dispatch.login(dispatch.letstd(CLOSED), GIFT_BOX_SCHEMA)
    assert earlier.content_version != current.content_version
    assert_replays(current, envelope, payload, store, saved, dispatch.clock)


@pytest.mark.parametrize("earlier_base", [True, False])
def test_digest_mixing_two_committed_gift_boxes_is_stale(dispatch, bundle, item_catalogs, earlier_base):
    store = dispatch.store()
    earlier = gift_box_login(dispatch, bundle, earlier_mailbox(bundle.presents, item_catalog=item_catalogs.plain))
    envelope, _, saved = commit(earlier, store)
    current = dispatch.login(dispatch.letstd(CLOSED), GIFT_BOX_SCHEMA)
    base, services = (earlier, current) if earlier_base else (current, earlier)
    assert_stale(current, envelope, store, saved, mixed_digest(base, services))


@pytest.mark.parametrize("committed, switched", SENDER_SWITCHES)
def test_receipt_replays_its_bytes_after_the_cooking_sender_switches(factory, committed, switched):
    case = sender_case(factory, committed)
    envelope = message(case)
    payload = case.service.receive(envelope, case.store)
    saved = case.store.profile()
    [mail] = login_cooking(case)
    assert mail["Type"] == (21 if committed == AFFECTION_SENDER else 22)
    assert (SENDER_FIELD in read_cooking(saved.state)) is (committed == AFFECTION_SENDER)
    case.service = rebuild(case, replace(case.service.rules, cooking_sender=switched))
    assert saved.state["login_reward_history"]["receipts"][-1]["content"] != case.service.content_version
    case.service.clock = lambda: pytest.fail("Exact retry consulted current time")
    assert case.service.receive(envelope, case.store) == payload
    assert case.store.profile() == saved


@pytest.mark.parametrize("committed, switched", SENDER_SWITCHES)
def test_forged_digest_stays_stale_across_a_sender_switch(factory, committed, switched):
    case = sender_case(factory, committed)
    envelope = message(case)
    case.service.receive(envelope, case.store)
    change(case, lambda state: state["login_reward_history"]["receipts"][-1].update(content=FORGED), revision=False)
    case.service = rebuild(case, replace(case.service.rules, cooking_sender=switched))
    fail(case, envelope, "login_reward_retry_stale")


def test_affection_receipt_is_stale_once_character_cooking_is_gone(factory):
    case = sender_case(factory, AFFECTION_SENDER)
    envelope = message(case)
    case.service.receive(envelope, case.store)
    case.presents = copy(case.presents)
    case.presents.vote_cooking_catalog = None
    case.service = rebuild(case, replace(case.service.rules, cooking_sender=ITEM_SENDER))
    fail(case, envelope, "login_reward_retry_stale")

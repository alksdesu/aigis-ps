from copy import copy, deepcopy

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_service import EventPassService
from aigis_ps.rank_rewards import (ASSAULT_SCHEMA, ASSAULT_SECTION, EXPIRY_SCHEMA, EXPIRY_SECTION, LETSTD_SECTION,
                                   LISTED_SCHEMA, SECTION_SERVICES, SECTIONS, RankRewardService)
from aigis_ps.vote_expiry import VoteAssaultNotifications, VoteExpiryNotifications
from test_event_pass_service import item_catalogs
from test_letstd_catalog import native_sources
from test_letstd_dispatch import (CLOSED, OSHIRORE_CAMPAIGN, RLYEH_PURCHASE, children, dispatch, rank_request,
                                  replace_without_revision)
from test_quest_service import bundle
from test_tower_service import without_enabled
from test_vote_assault_notifications import assault_sources, convert, flow, rank
from test_vote_catalog import vote_sources
from test_vote_expiry_notifications import letstd_service
from test_vote_transactions import catalog, make_case


EARLIER_SERVICES = tuple(name for section in SECTIONS[:-1] for name in SECTION_SERVICES[section])
FIXED_CASES = (*((7, name) for name in EARLIER_SERVICES), *((6, name) for name in EARLIER_SERVICES[:-1]))
EXTENSION_OPTIONS = {LETSTD_SECTION: "letstd", EXPIRY_SECTION: "expiry", ASSAULT_SECTION: "assault"}
LISTED_SCHEMAS = {LETSTD_SECTION: LISTED_SCHEMA, EXPIRY_SECTION: EXPIRY_SCHEMA, ASSAULT_SECTION: ASSAULT_SCHEMA}
MAIL_SERVICES = ("event_logins", "comeback_logins", "purchased_logins", "daily_gifts")
GIFT_BOX_SCHEMA = 6


def commit(service, store):
    envelope = rank_request(store)
    return envelope, service.receive(envelope, store), store.profile()


def earlier_mailbox(presents, **dependencies):
    earlier = copy(presents)
    for name, dependency in dependencies.items():
        setattr(earlier, name, dependency)
    return earlier


def gift_box_rank(dispatch, bundle, presents, *, passes=True):
    # Rebuilt on an earlier mailbox, every gift box service fingerprints the dependencies that mailbox had.
    services = {**{name: getattr(bundle, name) for name in MAIL_SERVICES}, "letstd": dispatch.letstd(CLOSED)}
    rebuilt = {name: type(service)(service.catalog, presents, service.rules) for name, service in services.items()}
    if passes:
        automatic = bundle.event_passes
        rebuilt["event_passes"] = EventPassService(bundle.bootstrap, bundle.player_state, presents, bundle.possessions,
                                                   automatic.catalog, automation_rules=automatic.automation_rules)
    return RankRewardService(bundle.bootstrap, bundle.player_state, presents, bundle.rank_rewards.catalog, **rebuilt)


def noticed(make_case, assault_sources, **options):
    case = make_case(**options)
    case.notifier = VoteAssaultNotifications(case.transactions.assault_expiry, assault_sources.__getitem__)
    return case


def mixed_digest(base, services, attribute="_letstd_receive_content_version"):
    mixed = copy(services)
    mixed.content_version = base.content_version
    mixed._record_receive_contents()
    return getattr(mixed, attribute)


def assert_replays(service, envelope, payload, store, saved, clock):
    assert service.initialize(deepcopy(saved.state)) == saved.state
    calls = clock.calls
    assert service.receive(envelope, store) == payload
    assert store.profile() == saved and clock.calls == calls


def assert_stale(service, envelope, store, saved, content):
    forged = deepcopy(saved.state)
    forged["rank_reward_history"]["receipts"][-1]["content"] = content
    replace_without_revision(store, forged)
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        service.receive(envelope, store)
    assert (caught.value.code, caught.value.status) == ("rank_reward_retry_stale", 409)
    assert store.profile() == before


@pytest.mark.parametrize("schema, name", FIXED_CASES)
def test_receipt_replays_after_an_earlier_fixed_service_is_attached(dispatch, schema, name):
    store = dispatch.store()
    earlier = dispatch.rank(schema=schema, without=(name,))
    envelope, payload, saved = commit(earlier, store)
    assert saved.state["rank_reward_delivery"]["schema_version"] == schema
    attached = dispatch.rank(schema=schema)
    assert attached.receive_content_version != earlier.receive_content_version
    assert_replays(attached, envelope, payload, store, saved, dispatch.clock)


@pytest.mark.parametrize("name", SECTION_SERVICES["vote_features"])
def test_vote_receipt_replays_after_the_other_vote_service_is_attached(dispatch, bundle, name):
    store = dispatch.store()
    notifier = {"vote_cooking_notifier": bundle.vote_cooking_notifier}
    earlier = dispatch.rank(schema=7, without=(name,), **notifier)
    envelope, payload, saved = commit(earlier, store)
    assert saved.state["rank_reward_delivery"]["schema_version"] == 7
    attached = dispatch.rank(schema=7, **notifier)
    assert attached.receive_content_version != earlier.receive_content_version
    assert_replays(attached, envelope, payload, store, saved, dispatch.clock)


@pytest.mark.parametrize("schema", (1, 6))
def test_listed_receipt_replays_after_services_it_never_recorded_are_attached(dispatch, bundle, schema):
    store = dispatch.store()
    letstd = dispatch.letstd(OSHIRORE_CAMPAIGN)
    envelope, payload, saved = commit(dispatch.rank(letstd, schema), store)
    marker = saved.state["rank_reward_delivery"]
    assert (marker["schema_version"], marker["sections"]) == (LISTED_SCHEMA, [*SECTIONS[:schema - 1], LETSTD_SECTION])
    assert marker[LETSTD_SECTION]
    attached = dispatch.rank(letstd, 7, vote_cooking_notifier=bundle.vote_cooking_notifier)
    assert_replays(attached, envelope, payload, store, saved, dispatch.clock)


@pytest.mark.parametrize("recorded, added", (
    ((ASSAULT_SECTION,), LETSTD_SECTION), ((ASSAULT_SECTION,), EXPIRY_SECTION), ((EXPIRY_SECTION,), LETSTD_SECTION),
    ((EXPIRY_SECTION, ASSAULT_SECTION), LETSTD_SECTION), ((LETSTD_SECTION, ASSAULT_SECTION), EXPIRY_SECTION)))
def test_listed_receipt_replays_after_an_unlisted_extension_is_attached(flow, native_sources, recorded, added):
    case = flow()
    convert(case)
    extensions = {LETSTD_SECTION: letstd_service(case, native_sources, RLYEH_PURCHASE, native_popup=True),
                  EXPIRY_SECTION: VoteExpiryNotifications(case.transactions.expiry), ASSAULT_SECTION: case.notifier}

    def configured(sections):
        return rank(case, tickets=False, **{EXTENSION_OPTIONS[name]: extensions[name] for name in sections})

    envelope, payload, saved = commit(configured(recorded), case.store)
    marker = saved.state["rank_reward_delivery"]
    assert (marker["schema_version"], marker["sections"]) == (LISTED_SCHEMAS[recorded[-1]], list(recorded))
    assert_replays(configured((*recorded, added)), envelope, payload, case.store, saved, case.clock)


def test_two_column_inventory_receipt_replays_its_saved_bytes(dispatch):
    store = dispatch.store()
    service = dispatch.rank(schema=7)
    envelope, payload, saved = commit(service, store)
    legacy = without_enabled(payload)
    assert legacy != payload
    assert [column.name for column in children(legacy)["POSSESSION"].children] == ["ItemID", "Count"]
    rewritten = deepcopy(saved.state)
    rewritten["rank_reward_history"]["receipts"][-1]["response"] = legacy.decode()
    replace_without_revision(store, rewritten)
    assert_replays(service, envelope, legacy, store, store.profile(), dispatch.clock)


def test_fixed_digest_without_its_last_section_service_is_stale(dispatch):
    store = dispatch.store()
    service = dispatch.rank(schema=7)
    envelope, _, saved = commit(service, store)
    assert_stale(service, envelope, store, saved, service._daily_receive_content_version)


def test_receipt_of_a_removed_service_is_stale(dispatch):
    store = dispatch.store()
    envelope, _, saved = commit(dispatch.rank(schema=7), store)
    with pytest.raises(ProtocolError) as caught:
        dispatch.rank(schema=7, without=("purchased_logins",)).receive(envelope, store)
    assert (caught.value.code, caught.value.status) == ("rank_reward_retry_stale", 409)
    assert store.profile() == saved


def test_listed_digest_with_an_unrecorded_service_is_stale(dispatch):
    store = dispatch.store()
    letstd = dispatch.letstd(CLOSED)
    envelope, _, saved = commit(dispatch.rank(letstd), store)
    assert saved.state["rank_reward_delivery"]["sections"] == [LETSTD_SECTION]
    current = dispatch.rank(letstd, 7)
    assert_stale(current, envelope, store, saved, current._letstd_receive_content_version)


def test_receipt_replays_after_the_recovery_catalog_attaches(dispatch, bundle):
    store = dispatch.store()
    # Automatic passes fingerprint the recovery catalog, so a mailbox without it had none.
    earlier = gift_box_rank(dispatch, bundle, earlier_mailbox(bundle.presents, recovery_catalog=None), passes=False)
    envelope, payload, saved = commit(earlier, store)
    marker = saved.state["rank_reward_delivery"]
    assert (marker["sections"], marker["event_pass_mail_ids"]) == ([*SECTIONS[:GIFT_BOX_SCHEMA - 1], LETSTD_SECTION], {})
    current = dispatch.rank(dispatch.letstd(CLOSED), GIFT_BOX_SCHEMA)
    assert saved.state["rank_reward_history"]["receipts"][-1]["content"] != current._letstd_receive_content_version
    assert_replays(current, envelope, payload, store, saved, dispatch.clock)


def test_receipt_replays_after_emblem_mail_attaches(dispatch, bundle, item_catalogs):
    store = dispatch.store()
    earlier = gift_box_rank(dispatch, bundle, earlier_mailbox(bundle.presents, item_catalog=item_catalogs.plain))
    envelope, payload, saved = commit(earlier, store)
    current = dispatch.rank(dispatch.letstd(CLOSED), GIFT_BOX_SCHEMA)
    assert earlier.content_version in current.replayable_content_versions() - {current.content_version}
    assert_replays(current, envelope, payload, store, saved, dispatch.clock)


@pytest.mark.parametrize("earlier_base", [True, False])
def test_digest_mixing_two_committed_gift_boxes_is_stale(dispatch, bundle, item_catalogs, earlier_base):
    store = dispatch.store()
    earlier = gift_box_rank(dispatch, bundle, earlier_mailbox(bundle.presents, item_catalog=item_catalogs.plain))
    envelope, _, saved = commit(earlier, store)
    current = dispatch.rank(dispatch.letstd(CLOSED), GIFT_BOX_SCHEMA)
    base, services = (earlier, current) if earlier_base else (current, earlier)
    assert_stale(current, envelope, store, saved, mixed_digest(base, services))


def test_conversion_notice_receipt_replays_after_emblem_mail_attaches(make_case, assault_sources, item_catalogs):
    committed = noticed(make_case, assault_sources, item_catalog=item_catalogs.plain)
    convert(committed)
    earlier = rank(committed, tickets=False, assault=committed.notifier)
    envelope, payload, saved = commit(earlier, committed.store)
    assert saved.state["rank_reward_delivery"]["sections"] == [ASSAULT_SECTION]
    attached = noticed(make_case, assault_sources, state=saved.state)
    current = rank(attached, tickets=False, assault=attached.notifier)
    assert current._assault_receive_content_version != earlier._assault_receive_content_version
    assert_replays(current, envelope, payload, committed.store, saved, attached.clock)
    for base, services in ((earlier, current), (current, earlier)):
        assert_stale(current, envelope, committed.store, saved,
                     mixed_digest(base, services, "_assault_receive_content_version"))

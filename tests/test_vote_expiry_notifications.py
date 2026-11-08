from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import timedelta
import json
from threading import Barrier, Lock

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.letstd_catalog import LetsTDCatalog
from aigis_ps.letstd_rules import LetsTDRules
from aigis_ps.letstd_service import RESPONSE_NAME, LetsTDService
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.rank_rewards import (DELIVERY_SCHEMAS, EXPIRY_SCHEMA, EXPIRY_SECTION, LETSTD_SECTION, LISTED_SCHEMA,
                                   SECTIONS, RankRewardService)
import aigis_ps.rank_rewards as rank_module
from aigis_ps.vote_expiry import (NOTICE_COLUMN, NOTICE_GROUP, NOTIFICATION_KEY, VoteExpiryNotifications,
                                  VoteExpiryService, validate_vote_expiry_notifications)
from test_letstd_catalog import native_sources
from test_quest_service import bundle, request
from test_vote_catalog import vote_sources
from test_vote_expiry import ITEM, NOW, case, catalogs, filled, pending, refuse_copies, stock
from test_vote_service import table_rows
from test_vote_transactions import catalog, make_case, vote


RANK = (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)), Column("DmmPremiumReceiverId", "I", (0,)))
SECOND = "item_vote_016002"
VALID = {"item_id": ITEM, "gold": 150, "mail_ids": [1], "notified_at": 0}
RLYEH_PURCHASE = {"rlyeh": {"newPurchase": True}}


def rank_request(store, tick=2000):
    return request(store, 116, RANK, tick=tick)


def children(payload):
    return {node.name: node for node in loads(payload).children}


def gold(payload):
    group = children(payload).get(NOTICE_GROUP)
    return None if group is None else table_rows(group)


def replace_without_revision(store, state):
    with store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def rank(case, *, notifier=None, tickets=True, letstd=None):
    return RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.bundle.rank_rewards.catalog,
                             vote_tickets=case.transactions.tickets if tickets else None, vote_expiry_notifier=notifier,
                             letstd=letstd)


def letstd_service(case, sources, partners, *, native_popup=False):
    return LetsTDService(LetsTDCatalog(sources.__getitem__), case.presents,
                         LetsTDRules(partners=partners, native_popup=native_popup))


def startup(case, tick):
    return case.transactions.startup(request(case.store, 137, (), tick=tick), case.store)


def expire(case, tick=1001):
    case.clock.now = case.group.vote_end
    startup(case, tick)
    return case.store.profile().state["vote_expiry"]["items"][case.group.item_id]


@pytest.fixture
def flow(make_case):
    def create(tickets=7, *, notices=True):
        case = make_case(tickets=tickets)
        assert case.bundle.rank_rewards is not None, case.bundle.rank_reward_error
        case.expiry = case.transactions.expiry
        case.notifier = VoteExpiryNotifications(case.expiry) if notices else None
        case.rank = rank(case, notifier=case.notifier)
        return case
    return create


def test_settled_conversions_notify_once_with_their_summed_gold(case):
    converted, report = case.helper.settle(stock(case, case.state, **{
        group.item_id: group.group_id for group in case.catalog.groups}), NOW)
    notifier = VoteExpiryNotifications(case.helper)
    assert notifier.initialize(converted) == converted and notifier.count(converted) == 0
    updated, notices = notifier.settle(converted, NOW + timedelta(seconds=1))
    settled = converted["vote_expiry"]["items"][ITEM]["settled_at"]
    assert notices == [{"item_id": grant["item_id"], "gold": grant["amount"] * grant["rate"], "mail_ids": grant["mail_ids"],
                        "notified_at": settled + 1000} for grant in sorted(report["grants"], key=lambda grant: grant["item_id"])]
    assert {key: value for key, value in updated.items() if key != NOTIFICATION_KEY} == converted
    assert updated[NOTIFICATION_KEY] == {"schema_version": 1, "records": {
        notice["item_id"]: {name: notice[name] for name in ("gold", "mail_ids", "notified_at")} for notice in notices}}
    group = notifier.response(notices)
    assert group == Container(NOTICE_GROUP, (Column(NOTICE_COLUMN, "I", (500,)),))
    assert b"<ItemExchange><Gold" in dumps(Container(children=(group,)))
    assert notifier.settle(updated, NOW + timedelta(days=1)) == (updated, [])
    assert notifier.count(updated) == 4
    notifier.validate_receipt(updated, notices, 4, exact=True)


def test_claimed_conversion_mail_still_notifies_once(case):
    converted, report = case.helper.settle(stock(case, case.state, **{ITEM: 2}), NOW)
    claimed, _, _ = case.presents.claim(converted, report["grants"][0]["mail_ids"], acquisition_day=739921)
    notifier = VoteExpiryNotifications(case.helper)
    updated, notices = notifier.settle(claimed, NOW)
    assert notices == [{"item_id": ITEM, "gold": 100, "mail_ids": report["grants"][0]["mail_ids"],
                        "notified_at": converted["vote_expiry"]["items"][ITEM]["settled_at"]}]
    assert case.presents.rows(updated) == [] and VoteExpiryNotifications(case.helper).initialize(updated) == updated
    assert notifier.settle(updated, NOW + timedelta(days=1)) == (updated, [])


@pytest.mark.parametrize("kind", ("unconverted", "closed", "pending"))
def test_conversion_without_settled_mail_has_no_notice(case, kind):
    if kind == "closed":
        source = deepcopy(case.state)
        source["tables"]["possessions"].append({"ItemID": ITEM, "Count": 0})
        state = case.helper.settle(source, NOW)[0]
        assert state["vote_expiry"]["items"][ITEM]["status"] == "closed"
    else:
        state = pending(case) if kind == "pending" else case.state
    notifier = VoteExpiryNotifications(case.helper)
    assert case.helper.conversions(state) == {}
    assert notifier.settle(state, NOW + timedelta(seconds=1)) == (state, [])
    assert NOTIFICATION_KEY not in state and notifier.count(state) == 0


def test_records_read_the_validated_notices_without_copying_the_save(case, monkeypatch):
    converted, _ = case.helper.settle(stock(case, case.state, **{ITEM: 3}), NOW)
    notifier = VoteExpiryNotifications(case.helper)
    notified, _ = notifier.settle(converted, NOW)
    states = (case.state, converted, notified)
    before = deepcopy(states)
    refuse_copies(monkeypatch)
    assert notifier.records(case.state) == notifier.records(converted) == {}
    assert notifier.records(notified) == notifier._records(notified)[1] == notified[NOTIFICATION_KEY]["records"]
    assert notifier.count(notified) == 1 and states == before


def test_conversions_list_only_settled_mail_with_frozen_gold(case):
    second = case.catalog.groups[1].item_id
    source = filled(case, stock(case, case.state, **{ITEM: 1, second: 2}), amount=499)
    state, report = case.helper.settle(source, NOW)
    assert report["pending"] == [{"item_id": second, "reason": "present_capacity"}]
    record = state["vote_expiry"]["items"][ITEM]
    assert case.helper.conversions(state) == {ITEM: {"gold": 50, "mail_ids": record["mail_ids"],
                                                     "settled_at": record["settled_at"]}}


TAMPERS = {
    "gold": lambda state: state[NOTIFICATION_KEY]["records"][ITEM].update(gold=200),
    "boolean_gold": lambda state: state[NOTIFICATION_KEY]["records"][ITEM].update(gold=True),
    "mail": lambda state: state[NOTIFICATION_KEY]["records"][ITEM].update(mail_ids=[999]),
    "early": lambda state: state[NOTIFICATION_KEY]["records"][ITEM].update(
        notified_at=state[NOTIFICATION_KEY]["records"][ITEM]["notified_at"] - 1),
    "unknown": lambda state: state[NOTIFICATION_KEY]["records"].update({SECOND: dict(state[NOTIFICATION_KEY]["records"][ITEM])}),
    "extra": lambda state: state[NOTIFICATION_KEY]["records"][ITEM].update(source="local"),
    "empty": lambda state: state[NOTIFICATION_KEY]["records"].clear(),
    "version": lambda state: state[NOTIFICATION_KEY].update(schema_version=2),
    "conversion_lost": lambda state: state.pop("vote_expiry"),
}


@pytest.mark.parametrize("damage", sorted(TAMPERS))
def test_notice_records_must_match_their_permanent_conversion(case, damage):
    converted, _ = case.helper.settle(stock(case, case.state, **{ITEM: 3}), NOW)
    notifier = VoteExpiryNotifications(case.helper)
    updated, _ = notifier.settle(converted, NOW)
    broken = deepcopy(updated)
    TAMPERS[damage](broken)
    for action in (lambda: notifier.initialize(broken), lambda: notifier.records(broken),
                   lambda: notifier.settle(broken, NOW + timedelta(days=1))):
        with pytest.raises(ProtocolError) as caught:
            action()
        assert (caught.value.code, caught.value.status) == ("vote_expiry_notifications_invalid", 503)


def test_notice_cannot_predate_its_conversion_or_an_earlier_notice(case):
    converted, _ = case.helper.settle(stock(case, case.state, **{ITEM: 3}), NOW)
    notifier = VoteExpiryNotifications(case.helper)
    with pytest.raises(ProtocolError) as caught:
        notifier.settle(converted, NOW - timedelta(seconds=1))
    assert (caught.value.code, caught.value.status) == ("vote_expiry_notification_clock_reversed", 409)
    second = case.catalog.groups[1].item_id
    notified, _ = notifier.settle(converted, NOW + timedelta(seconds=30))
    later, _ = case.helper.settle(stock(case, notified, **{second: 1}), NOW + timedelta(seconds=20))
    assert notifier.settle(later, NOW + timedelta(seconds=30))[1][0]["item_id"] == second
    with pytest.raises(ProtocolError) as caught:
        notifier.settle(later, NOW + timedelta(seconds=25))
    assert (caught.value.code, caught.value.status) == ("vote_expiry_notification_clock_reversed", 409)


def test_receipt_requires_its_permanent_records_and_count(case):
    converted, _ = case.helper.settle(stock(case, case.state, **{ITEM: 3}), NOW)
    notifier = VoteExpiryNotifications(case.helper)
    updated, notices = notifier.settle(converted, NOW)
    notifier.validate_receipt(updated, notices, 1, exact=True)
    notifier.validate_receipt(updated, [], 1, exact=True)
    notifier.validate_receipt(updated, [], 0)
    for state, count, exact in ((converted, 1, False), (updated, 0, True), (updated, 2, False)):
        with pytest.raises(ProtocolError) as caught:
            notifier.validate_receipt(state, [], count, exact=exact)
        assert (caught.value.code, caught.value.status) == ("vote_expiry_notification_proof_lost", 503)
    with pytest.raises(ProtocolError) as caught:
        notifier.validate_receipt(updated, [dict(notices[0], gold=notices[0]["gold"] + 50)], 1)
    assert caught.value.code == "vote_expiry_notifications_invalid"
    with pytest.raises(ProtocolError) as caught:
        notifier.response([])
    assert caught.value.code == "vote_expiry_notifications_invalid"


MALFORMED = {
    "boolean_gold": ([dict(VALID, gold=True)], 1),
    "zero_gold": ([dict(VALID, gold=0)], 1),
    "two_mails": ([dict(VALID, mail_ids=[1, 2])], 1),
    "tuple_mails": ([dict(VALID, mail_ids=(1,))], 1),
    "duplicate_item": ([VALID, dict(VALID, mail_ids=[2])], 2),
    "mixed_instants": ([VALID, dict(VALID, item_id=SECOND, mail_ids=[2], notified_at=1)], 2),
    "extra_field": ([dict(VALID, source="local")], 1),
    "empty_item": ([dict(VALID, item_id="")], 1),
    "count_below": ([VALID], 0),
    "count_text": ([VALID], "1"),
    "count_unbounded": ([], 4097),
    "gold_overflow": ([dict(VALID, gold=0x7FFFFFFF), dict(VALID, item_id=SECOND, mail_ids=[2], gold=1)], 2),
    "tuple_notices": ((VALID,), 1),
    "unrepresentable_instant": ([dict(VALID, notified_at=10 ** 20)], 1),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_notice_shape_rejects_malformed_values(name):
    notices, count = MALFORMED[name]
    with pytest.raises(ProtocolError) as caught:
        validate_vote_expiry_notifications(notices, count)
    assert (caught.value.code, caught.value.status) == ("vote_expiry_notifications_invalid", 503)


def test_notice_shape_returns_the_native_gold_total():
    assert validate_vote_expiry_notifications([], 0) == 0
    assert validate_vote_expiry_notifications([VALID, dict(VALID, item_id=SECOND, gold=50, mail_ids=[2])], 3) == 200


def test_listed_schemas_accept_only_their_own_extension_layouts():
    recorded = RankRewardService._recorded_sections
    for count in range(len(SECTIONS) + 1):
        fixed = list(SECTIONS[:count])
        for version, tail in ((LISTED_SCHEMA, [LETSTD_SECTION]), (EXPIRY_SCHEMA, [EXPIRY_SECTION]),
                              (EXPIRY_SCHEMA, [LETSTD_SECTION, EXPIRY_SECTION])):
            marker = {"schema_version": version, "sections": fixed + tail}
            assert recorded(marker) == (DELIVERY_SCHEMAS[version], tuple(fixed + tail))
        for version, tail in ((LISTED_SCHEMA, [EXPIRY_SECTION]), (LISTED_SCHEMA, [LETSTD_SECTION, EXPIRY_SECTION]),
                              (EXPIRY_SCHEMA, []), (EXPIRY_SCHEMA, [LETSTD_SECTION]),
                              (EXPIRY_SCHEMA, [EXPIRY_SECTION, LETSTD_SECTION]), (EXPIRY_SCHEMA, [EXPIRY_SECTION] * 2),
                              (EXPIRY_SCHEMA, ["unknown_grants", EXPIRY_SECTION])):
            with pytest.raises(ProtocolError) as caught:
                recorded({"schema_version": version, "sections": fixed + tail})
            assert caught.value.code == "rank_reward_history_invalid"
    with pytest.raises(ProtocolError):
        recorded({"schema_version": EXPIRY_SCHEMA, "sections": [EXPIRY_SECTION, *SECTIONS[:1]]})
    for version in (0, True, str(EXPIRY_SCHEMA), max(DELIVERY_SCHEMAS) + 1):
        with pytest.raises(ProtocolError) as caught:
            recorded({"schema_version": version, "sections": [*SECTIONS, LETSTD_SECTION, EXPIRY_SECTION]})
        assert caught.value.code == "rank_reward_history_invalid"


def test_expiry_digest_chains_after_every_earlier_schema(flow):
    case = flow()
    plain = rank(case)
    for schema in DELIVERY_SCHEMAS.values():
        if schema.version < EXPIRY_SCHEMA:
            assert getattr(case.rank, schema.content) == getattr(plain, schema.content)
    assert plain._expiry_receive_content_version == plain._letstd_receive_content_version
    assert case.rank._assault_receive_content_version == case.rank._expiry_receive_content_version
    assert case.rank._expiry_receive_content_version not in {getattr(plain, schema.content)
                                                             for schema in DELIVERY_SCHEMAS.values()}
    status = case.rank.status()
    assert status["ready"] is True and status["vote_expiry_notifications"] is True
    assert case.notifier.status()["ready"] is True and plain.status()["vote_expiry_notifications"] is False
    moved = VoteExpiryNotifications(case.expiry)
    moved.presents = copy(case.presents)
    assert (moved.status()["ready"], moved.status()["error"]) == (False, "vote_expiry_notifications_changed")


@pytest.mark.parametrize("tickets", (True, False))
def test_first_116_after_startup_conversion_carries_gold_once(flow, tickets):
    case = flow()
    service = rank(case, notifier=case.notifier, tickets=tickets)
    record = expire(case)
    assert record["status"] == "settled" and record["amount"] == 7
    envelope = rank_request(case.store)
    payload = service.receive(envelope, case.store)
    reply = children(payload)
    assert gold(payload) == [{NOTICE_COLUMN: 350}] and list(reply)[-1] == NOTICE_GROUP and "PPT" not in reply
    assert ("POSSESSION" in reply) is tickets
    saved = case.store.profile()
    notice = {"item_id": case.group.item_id, "gold": 350, "mail_ids": record["mail_ids"], "notified_at": record["settled_at"]}
    marker = saved.state["rank_reward_delivery"]
    assert marker["schema_version"] == EXPIRY_SCHEMA and marker["rank_mail_ids"] == []
    assert marker["sections"] == [*(SECTIONS if tickets else ()), EXPIRY_SECTION]
    assert marker["vote_expiry_notifications"] == [notice] and marker["vote_expiry_notification_count"] == 1
    assert saved.state[NOTIFICATION_KEY] == {"schema_version": 1, "records": {
        case.group.item_id: {name: notice[name] for name in ("gold", "mail_ids", "notified_at")}}}
    assert saved.state["rank_reward_history"]["receipts"][-1]["content"] == service._expiry_receive_content_version
    calls = case.clock.calls
    assert service.receive(envelope, case.store) == payload
    assert case.store.profile() == saved and case.clock.calls == calls
    reopened = create_app(case.store.path.parent, initial_state=saved.state,
        state_initializer=lambda state: service.initialize(case.service.initialize(state)),
        handlers=case.bootstrap.handlers() | case.presents.handlers() | case.service.handlers() | service.handlers())
    assert service.receive(envelope, reopened.state.store) == payload and reopened.state.store.profile() == saved
    later = service.receive(rank_request(case.store, 2001), case.store)
    assert gold(later) is None
    current = case.store.profile().state
    assert current["rank_reward_delivery"]["vote_expiry_notifications"] == []
    assert current["rank_reward_delivery"]["vote_expiry_notification_count"] == 1
    assert current[NOTIFICATION_KEY] == saved.state[NOTIFICATION_KEY]


def test_letstd_preflights_carry_the_notice_and_both_extensions_replay(flow, native_sources, monkeypatch):
    case = flow()
    expire(case)
    service = rank(case, notifier=case.notifier, tickets=False,
                   letstd=letstd_service(case, native_sources, RLYEH_PURCHASE, native_popup=True))
    encoded, original = [], rank_module.encode_response

    def recording(profile, payload):
        if 116 in profile.registration_keys:
            encoded.append(payload)
        return original(profile, payload)

    monkeypatch.setattr(rank_module, "encode_response", recording)
    envelope = rank_request(case.store)
    payload = service.receive(envelope, case.store)
    reply, saved = children(payload), case.store.profile()
    marker = saved.state["rank_reward_delivery"]
    assert marker["schema_version"] == EXPIRY_SCHEMA and marker["sections"] == [LETSTD_SECTION, EXPIRY_SECTION]
    assert marker[LETSTD_SECTION] and marker["vote_expiry_notification_count"] == 1
    assert {"PPT", RESPONSE_NAME} <= set(reply) and list(reply)[-1] == NOTICE_GROUP
    # Two final encodes plus at least one LetsTD candidate preflight, each already holding the notice.
    assert len(encoded) > 2 and all(gold(candidate) == [{NOTICE_COLUMN: 350}] for candidate in encoded)
    calls = case.clock.calls
    assert service.receive(envelope, case.store) == payload
    assert case.store.profile() == saved and case.clock.calls == calls


def test_116_before_the_conversion_instant_refuses_without_writing(flow):
    case = flow()
    service = rank(case, notifier=case.notifier, tickets=False)
    expire(case)
    case.clock.now -= timedelta(seconds=1)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        service.receive(rank_request(case.store), case.store)
    assert (caught.value.code, caught.value.status) == ("vote_expiry_notification_clock_reversed", 409)
    assert case.store.profile() == before


def test_expiry_notices_must_share_the_reward_mailbox(flow):
    case = flow()
    expire(case)
    foreign = VoteExpiryNotifications(VoteExpiryService(case.catalog, copy(case.presents), case.bundle.possessions))
    detached = rank(case, notifier=foreign)
    assert (detached.status()["ready"], detached.status()["error"]) == (False, "rank_vote_expiry_changed")
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        detached.receive(rank_request(case.store), case.store)
    assert (caught.value.code, caught.value.status) == ("rank_vote_expiry_changed", 503)
    assert case.store.profile() == before


@pytest.mark.parametrize("participation", ("none", "closed"))
def test_no_converted_gold_means_no_item_exchange_or_proof(flow, participation):
    case = flow(tickets=0)
    if participation == "closed":
        case.transactions.submit(vote(case, 1), case.store)
        assert expire(case)["status"] == "closed"
    payload = case.rank.receive(rank_request(case.store), case.store)
    assert NOTICE_GROUP not in children(payload)
    state = case.store.profile().state
    marker = state["rank_reward_delivery"]
    assert marker["schema_version"] == EXPIRY_SCHEMA and marker["sections"][-1] == EXPIRY_SECTION
    assert marker["vote_expiry_notifications"] == [] and marker["vote_expiry_notification_count"] == 0
    assert NOTIFICATION_KEY not in state


def test_pending_conversion_notifies_only_after_its_mail_lands(flow):
    case = flow(tickets=3)
    profile = case.store.profile()
    fillers = [{"kind": "gold", "amount": 1, "description": "capacity"}] * (MAX_PRESENTS - len(case.presents.rows(profile.state)))
    case.store.update_state(case.presents.enqueue(profile.state, fillers)[0], profile.revision)
    assert expire(case)["status"] == "pending"
    assert NOTICE_GROUP not in children(case.rank.receive(rank_request(case.store), case.store))
    assert NOTIFICATION_KEY not in case.store.profile().state
    profile = case.store.profile()
    freed = deepcopy(profile.state)
    freed["tables"]["presents"] = [row for row in case.presents.rows(freed) if row["Description"] != "capacity"]
    case.store.update_state(freed, profile.revision)
    case.clock.now += timedelta(minutes=1)
    startup(case, 1002)
    assert case.store.profile().state["vote_expiry"]["items"][case.group.item_id]["status"] == "settled"
    assert gold(case.rank.receive(rank_request(case.store, 2001), case.store)) == [{NOTICE_COLUMN: 150}]


def test_vote_57_conversions_notify_their_summed_gold_on_the_next_116(flow):
    case = flow()
    final, second = case.catalog.groups[-1], case.catalog.groups[1]
    profile = case.store.profile()
    case.store.update_state(case.bundle.possessions.credit(profile.state, {final.item_id: 10, second.item_id: 2}),
                            profile.revision)
    case.clock.now = final.vote_start
    case.transactions.submit(vote(case, 5, group_id=4), case.store)
    expiry = case.store.profile().state["vote_expiry"]["items"]
    assert [expiry[group.item_id]["status"] for group in (case.group, second)] == ["settled", "settled"]
    assert gold(case.rank.receive(rank_request(case.store), case.store)) == [{NOTICE_COLUMN: 450}]
    notices = case.store.profile().state["rank_reward_delivery"]["vote_expiry_notifications"]
    assert [(notice["item_id"], notice["gold"]) for notice in notices] == [(case.group.item_id, 350), (second.item_id, 100)]


def replay_old_receipt_then_notify(case, schema, *, tickets, letstd=None):
    old = rank(case, tickets=tickets, letstd=letstd)
    expire(case)
    envelope = rank_request(case.store)
    payload = old.receive(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["rank_reward_delivery"]["schema_version"] == schema
    assert gold(payload) is None
    upgraded = rank(case, notifier=VoteExpiryNotifications(case.expiry), letstd=letstd)
    assert upgraded.initialize(deepcopy(saved.state)) == saved.state
    clock = case.bootstrap.clock
    case.bootstrap.clock = lambda: pytest.fail("An old receipt must not read a new clock")
    assert upgraded.receive(envelope, case.store) == payload and case.store.profile() == saved
    case.bootstrap.clock = clock
    assert gold(upgraded.receive(rank_request(case.store, 2001), case.store)) == [{NOTICE_COLUMN: 350}]
    marker = case.store.profile().state["rank_reward_delivery"]
    assert marker["schema_version"] == EXPIRY_SCHEMA and marker["vote_expiry_notification_count"] == 1
    assert marker["sections"] == [*SECTIONS, *([LETSTD_SECTION] if letstd is not None else []), EXPIRY_SECTION]
    assert gold(upgraded.receive(rank_request(case.store, 2002), case.store)) is None


@pytest.mark.parametrize("tickets,schema", ((False, 1), (True, 7)))
def test_old_receipts_replay_exactly_and_old_conversions_notify_once(flow, tickets, schema):
    replay_old_receipt_then_notify(flow(notices=False), schema, tickets=tickets)


def test_old_listed_letstd_receipts_replay_exactly_and_old_conversions_notify_once(flow, native_sources):
    case = flow(notices=False)
    replay_old_receipt_then_notify(case, LISTED_SCHEMA, tickets=True, letstd=letstd_service(case, native_sources, {}))


@pytest.mark.parametrize("converted", (False, True))
def test_saved_notice_history_requires_the_notice_service(flow, converted):
    case = flow()
    if converted:
        expire(case)
    case.rank.receive(rank_request(case.store), case.store)
    state = case.store.profile().state
    assert (NOTIFICATION_KEY in state) is converted
    plain = rank(case)
    with pytest.raises(ProtocolError) as caught:
        plain.initialize(deepcopy(state))
    assert (caught.value.code, caught.value.status) == ("rank_vote_expiry_unavailable", 503)
    unmarked = {key: value for key, value in state.items() if key != "rank_reward_delivery"}
    if converted:
        with pytest.raises(ProtocolError) as caught:
            plain.initialize(deepcopy(unmarked))
        assert (caught.value.code, caught.value.status) == ("rank_vote_expiry_unavailable", 503)
    else:
        assert plain.initialize(deepcopy(unmarked)) == unmarked


FORGERIES = {
    "domain_removed": (lambda state, item: state.pop(NOTIFICATION_KEY), "vote_expiry_notification_proof_lost"),
    "record_gold": (lambda state, item: state[NOTIFICATION_KEY]["records"][item].update(gold=1),
                    "vote_expiry_notifications_invalid"),
    "marker_gold": (lambda state, item: state["rank_reward_delivery"]["vote_expiry_notifications"][0].update(gold=1),
                    "vote_expiry_notifications_invalid"),
    "marker_count": (lambda state, item: state["rank_reward_delivery"].update(vote_expiry_notification_count="1"),
                     "rank_reward_history_invalid"),
    "section_dropped": (lambda state, item: state["rank_reward_delivery"]["sections"].pop(), "rank_reward_history_invalid"),
}


@pytest.mark.parametrize("forgery", sorted(FORGERIES))
def test_forged_or_lost_notice_proof_fails_closed(flow, forgery):
    case = flow()
    expire(case)
    case.rank.receive(rank_request(case.store), case.store)
    state = deepcopy(case.store.profile().state)
    damage, code = FORGERIES[forgery]
    damage(state, case.group.item_id)
    with pytest.raises(ProtocolError) as caught:
        case.rank.initialize(deepcopy(state))
    assert (caught.value.code, caught.value.status) == (code, 503)
    replace_without_revision(case.store, state)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.rank.receive(rank_request(case.store, 2001), case.store)
    assert caught.value.code == code and case.store.profile() == before


def test_failed_116_encoding_rolls_back_the_notice(flow, monkeypatch):
    case = flow()
    expire(case)
    before = case.store.profile()
    original = rank_module.encode_response

    def refusing(profile, payload):
        if 116 in profile.registration_keys and NOTICE_GROUP.encode() in payload:
            raise ProtocolError("response_too_large", "Injected 116 encoding failure", 500)
        return original(profile, payload)

    monkeypatch.setattr(rank_module, "encode_response", refusing)
    envelope = rank_request(case.store)
    with pytest.raises(ProtocolError):
        case.rank.receive(envelope, case.store)
    assert case.store.profile() == before
    monkeypatch.setattr(rank_module, "encode_response", original)
    assert gold(case.rank.receive(envelope, case.store)) == [{NOTICE_COLUMN: 350}]


def test_concurrent_new_116_requests_notify_once(flow, monkeypatch):
    case = flow()
    expire(case)
    original = case.store.update_state
    barrier, lock, calls = Barrier(2), Lock(), 0

    def synchronized(state, revision):
        nonlocal calls
        with lock:
            calls += 1
            wait = calls <= 2
        if wait:
            barrier.wait(timeout=20)
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", synchronized)
    envelopes = [rank_request(case.store, tick) for tick in (2000, 2001)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(case.rank.receive, envelope, case.store) for envelope in envelopes]
        payloads = [future.result(timeout=45) for future in futures]
    assert sorted(NOTICE_GROUP in children(payload) for payload in payloads) == [False, True]
    state = case.store.profile().state
    assert len(state[NOTIFICATION_KEY]["records"]) == 1
    assert state["rank_reward_delivery"]["vote_expiry_notification_count"] == 1

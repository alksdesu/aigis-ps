from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Lock

import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.app import create_app
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.errors import ProtocolError
from aigis_ps.letstd_service import RESPONSE_NAME
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.rank_rewards import (ASSAULT_SCHEMA, ASSAULT_SECTION, DELIVERY_SCHEMAS, EXPIRY_SCHEMA, EXPIRY_SECTION,
                                   LETSTD_SECTION, LISTED_SCHEMA, SECTIONS, RankRewardService)
import aigis_ps.rank_rewards as rank_module
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.vote_expiry import (ASSAULT_KEY, ASSAULT_NOTIFICATION_KEY, MISSION_SOURCES, NOTICE_COLUMN, NOTICE_GROUP,
                                  RET_COLUMN, RET_GROUP, VoteAssaultExpiryService, VoteAssaultNotifications,
                                  VoteExpiryNotifications, validate_vote_assault_notifications)
from aigis_ps.vote_rewards import VoteRewardService
from test_letstd_catalog import native_sources
from test_present_items import item_table
from test_quest_service import bundle
from test_vote_assault_expiry import EVENT, catalogs, case, freed, pending, settled
from test_vote_catalog import rows, vote_sources
from test_vote_expiry import refuse_copies
from test_vote_expiry_notifications import (RLYEH_PURCHASE, children, gold, letstd_service, rank_request,
                                            replace_without_revision, startup)
from test_vote_service import table_rows
from test_vote_transactions import catalog, make_case, vote


CLIENT = CURRENT_CLIENT_SHA256
# AssaultMissionConfig 410576 lists quest 25000, which EmcMissionConfig 576 covers with Type_Quest 7.
MISSION = 576
WIRE = b'<RET><msid T="I"><V>576</V></msid></RET>'
RECORD_FIELDS = ("mission_id", "gold", "mail_ids", "notified_at")
VALID = {"event_id": 16, "mission_id": MISSION, "gold": 6000, "mail_ids": [1], "notified_at": 0}
CONFIG_SCHEMA = (("MissionID", 1), ("Name", 32))
QUEST_SCHEMA = (("MissionID", 1), ("QuestID", 1))
EMC_SCHEMA = (("ID_Mission", 1), ("ID_QuestFirst", 1), ("ID_QuestLast", 1), ("Type_Quest", 1))


@pytest.fixture(scope="module")
def assault_sources():
    project = Path(__file__).resolve().parents[1]
    _, entries = ResourceLibrary(project / "runtime/resources", client_hash=CLIENT).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    return {name: assets.read(name)[0] for name in MISSION_SOURCES}


def notifier(case, sources):
    return VoteAssaultNotifications(case.helper, sources.__getitem__)


def rank(case, *, assault=None, expiry=None, tickets=True, letstd=None):
    return RankRewardService(case.bootstrap, case.bundle.player_state, case.presents, case.bundle.rank_rewards.catalog,
                             vote_tickets=case.transactions.tickets if tickets else None, vote_expiry_notifier=expiry,
                             vote_assault_notifier=assault, letstd=letstd)


def ret(payload):
    group = children(payload).get(RET_GROUP)
    return None if group is None else table_rows(group)


def convert(case, tick=1001):
    case.transactions.submit(vote(case, 30), case.store)
    case.clock.now = case.catalog.event.end_at
    startup(case, tick)
    return case.store.profile().state[ASSAULT_KEY]["events"][EVENT]


def edited(sources, name, schema, change):
    changed = dict(sources)
    table = rows(changed, name)
    change(table)
    changed[name] = item_table(table, schema)
    return changed


def config_of(table):
    return next(row for row in table if row["MissionID"] == 410576)


@pytest.fixture
def flow(make_case, assault_sources):
    def create(*, notices=True):
        case = make_case()
        assert case.bundle.rank_rewards is not None, case.bundle.rank_reward_error
        case.notifier = (VoteAssaultNotifications(case.transactions.assault_expiry, assault_sources.__getitem__)
                         if notices else None)
        case.rank = rank(case, assault=case.notifier)
        return case
    return create


def test_event_mission_resolves_through_the_assault_tables(case, assault_sources):
    service = notifier(case, assault_sources)
    status = service.status()
    assert service.mission_id == MISSION and status["mission_id"] == MISSION
    assert status["ready"] is True and status["group"] == f"{RET_GROUP}.{RET_COLUMN}" and status["native_tested"] is False
    assert status["expiry_content_version"] == case.helper.content_version


SOURCE_DAMAGE = {
    "renamed": ("AssaultMissionConfig.atb", CONFIG_SCHEMA,
                lambda table: config_of(table).update(Name="第17回人気投票ミッション")),
    "repeated_name": ("AssaultMissionConfig.atb", CONFIG_SCHEMA,
                      lambda table: table.append(dict(config_of(table), MissionID=410999))),
    "two_quests": ("AssaultMissionQuestList.atb", QUEST_SCHEMA,
                   lambda table: table.append({"MissionID": 410576, "QuestID": 25001})),
    "not_assault": ("EmcMissionConfig.atb", EMC_SCHEMA,
                    lambda table: next(row for row in table if row["ID_Mission"] == MISSION).update(Type_Quest=4)),
    "overlapping": ("EmcMissionConfig.atb", EMC_SCHEMA,
                    lambda table: table.append({"ID_Mission": 999, "ID_QuestFirst": 24000, "ID_QuestLast": 26000,
                                                "Type_Quest": 7})),
}


@pytest.mark.parametrize("damage", sorted(SOURCE_DAMAGE))
def test_unverifiable_event_mission_refuses_the_notifier(case, assault_sources, damage):
    name, schema, change = SOURCE_DAMAGE[damage]
    with pytest.raises(ProtocolError) as caught:
        notifier(case, edited(assault_sources, name, schema, change))
    assert (caught.value.code, caught.value.status) == ("vote_assault_notifications_source_invalid", 503)


def test_missing_mission_table_refuses_the_notifier(case, assault_sources):
    partial = {name: data for name, data in assault_sources.items() if name != "EmcMissionConfig.atb"}
    with pytest.raises(ProtocolError) as caught:
        notifier(case, partial)
    assert caught.value.code == "vote_assault_notifications_source_invalid"


def test_settled_conversion_notifies_once_with_its_mission(case, assault_sources):
    converted, report = settled(case)
    service = notifier(case, assault_sources)
    assert service.initialize(converted) == converted and service.count(converted) == 0
    updated, notices = service.settle(converted, case.end + timedelta(seconds=1))
    record = converted[ASSAULT_KEY]["events"][EVENT]
    assert notices == [{"event_id": 16, "mission_id": MISSION, "gold": 6000, "mail_ids": report["grants"][0]["mail_ids"],
                        "notified_at": record["settled_at"] + 1000}]
    assert {key: value for key, value in updated.items() if key != ASSAULT_NOTIFICATION_KEY} == converted
    assert updated[ASSAULT_NOTIFICATION_KEY] == {"schema_version": 1, "records": {
        EVENT: {name: notices[0][name] for name in RECORD_FIELDS}}}
    group = service.response(notices)
    assert group == Container(RET_GROUP, (Column(RET_COLUMN, "I", (MISSION,)),))
    assert WIRE in dumps(Container(children=(group,)))
    assert service.settle(updated, case.end + timedelta(days=1)) == (updated, [])
    assert service.count(updated) == 1
    service.validate_receipt(updated, notices, 1, exact=True)


def test_records_read_the_validated_notices_without_copying_the_save(case, assault_sources, monkeypatch):
    converted, _ = settled(case)
    service = notifier(case, assault_sources)
    notified, _ = service.settle(converted, case.end)
    states = (case.state, converted, notified)
    before = deepcopy(states)
    refuse_copies(monkeypatch)
    assert service.records(case.state) == service.records(converted) == {}
    assert (service.records(notified) == service._records(notified)[1]
            == notified[ASSAULT_NOTIFICATION_KEY]["records"])
    assert service.count(notified) == 1 and states == before


def test_claimed_conversion_mail_still_notifies_once(case, assault_sources):
    converted, report = settled(case)
    claimed = case.presents.claim(converted, report["grants"][0]["mail_ids"], acquisition_day=739921)[0]
    service = notifier(case, assault_sources)
    updated, notices = service.settle(claimed, case.end)
    assert [notice["mail_ids"] for notice in notices] == [report["grants"][0]["mail_ids"]]
    assert notifier(case, assault_sources).initialize(updated) == updated
    assert service.settle(updated, case.end + timedelta(days=1)) == (updated, [])


@pytest.mark.parametrize("kind", ("unconverted", "pending"))
def test_conversion_without_settled_mail_has_no_notice(case, assault_sources, kind):
    state = pending(case) if kind == "pending" else case.state
    service = notifier(case, assault_sources)
    assert service.settle(state, case.end + timedelta(seconds=1)) == (state, [])
    assert ASSAULT_NOTIFICATION_KEY not in state and service.count(state) == 0


def test_pending_conversion_notifies_after_its_retry_lands(case, assault_sources):
    result, _ = case.helper.settle(freed(case, pending(case)), case.end + timedelta(minutes=1))
    _, notices = notifier(case, assault_sources).settle(result, case.end + timedelta(minutes=1))
    assert [(notice["gold"], notice["mission_id"]) for notice in notices] == [(9000, MISSION)]


TAMPERS = {
    "gold": lambda state: state[ASSAULT_NOTIFICATION_KEY]["records"][EVENT].update(gold=1),
    "boolean_gold": lambda state: state[ASSAULT_NOTIFICATION_KEY]["records"][EVENT].update(gold=True),
    "mail": lambda state: state[ASSAULT_NOTIFICATION_KEY]["records"][EVENT].update(mail_ids=[999]),
    "mission": lambda state: state[ASSAULT_NOTIFICATION_KEY]["records"][EVENT].update(mission_id=0),
    "early": lambda state: state[ASSAULT_NOTIFICATION_KEY]["records"][EVENT].update(
        notified_at=state[ASSAULT_KEY]["events"][EVENT]["settled_at"] - 1),
    "unknown": lambda state: state[ASSAULT_NOTIFICATION_KEY]["records"].update(
        {"15": dict(state[ASSAULT_NOTIFICATION_KEY]["records"][EVENT], mission_id=550)}),
    "extra": lambda state: state[ASSAULT_NOTIFICATION_KEY]["records"][EVENT].update(source="local"),
    "empty": lambda state: state[ASSAULT_NOTIFICATION_KEY]["records"].clear(),
    "version": lambda state: state[ASSAULT_NOTIFICATION_KEY].update(schema_version=2),
    "conversion_lost": lambda state: state.pop(ASSAULT_KEY),
}


@pytest.mark.parametrize("damage", sorted(TAMPERS))
def test_notice_records_must_match_their_permanent_conversion(case, assault_sources, damage):
    converted, _ = settled(case)
    service = notifier(case, assault_sources)
    updated, _ = service.settle(converted, case.end)
    broken = deepcopy(updated)
    TAMPERS[damage](broken)
    for action in (lambda: service.initialize(broken), lambda: service.records(broken),
                   lambda: service.settle(broken, case.end + timedelta(days=1))):
        with pytest.raises(ProtocolError) as caught:
            action()
        assert (caught.value.code, caught.value.status) == ("vote_assault_notifications_invalid", 503)


def test_notice_cannot_predate_its_conversion(case, assault_sources):
    converted, _ = settled(case)
    with pytest.raises(ProtocolError) as caught:
        notifier(case, assault_sources).settle(converted, case.end - timedelta(seconds=1))
    assert (caught.value.code, caught.value.status) == ("vote_assault_notification_clock_reversed", 409)


def test_receipt_requires_its_permanent_records_and_count(case, assault_sources):
    converted, _ = settled(case)
    service = notifier(case, assault_sources)
    updated, notices = service.settle(converted, case.end)
    service.validate_receipt(updated, notices, 1, exact=True)
    service.validate_receipt(updated, [], 1, exact=True)
    service.validate_receipt(updated, [], 0)
    for state, count, exact in ((converted, 1, False), (updated, 0, True), (updated, 2, False)):
        with pytest.raises(ProtocolError) as caught:
            service.validate_receipt(state, [], count, exact=exact)
        assert (caught.value.code, caught.value.status) == ("vote_assault_notification_proof_lost", 503)
    with pytest.raises(ProtocolError) as caught:
        service.validate_receipt(updated, [dict(notices[0], mission_id=550)], 1)
    assert caught.value.code == "vote_assault_notifications_invalid"
    with pytest.raises(ProtocolError) as caught:
        service.response([])
    assert caught.value.code == "vote_assault_notifications_invalid"


MALFORMED = {
    "boolean_event": ([dict(VALID, event_id=True)], 1),
    "zero_event": ([dict(VALID, event_id=0)], 1),
    "zero_mission": ([dict(VALID, mission_id=0)], 1),
    "boolean_gold": ([dict(VALID, gold=True)], 1),
    "zero_gold": ([dict(VALID, gold=0)], 1),
    "two_mails": ([dict(VALID, mail_ids=[1, 2])], 1),
    "tuple_mails": ([dict(VALID, mail_ids=(1,))], 1),
    "repeated_event": ([VALID, dict(VALID, mission_id=550, mail_ids=[2])], 2),
    "repeated_mission": ([VALID, dict(VALID, event_id=15, mail_ids=[2])], 2),
    "mixed_instants": ([VALID, dict(VALID, event_id=15, mission_id=550, mail_ids=[2], notified_at=1)], 2),
    "extra_field": ([dict(VALID, source="local")], 1),
    "count_below": ([VALID], 0),
    "count_text": ([VALID], "1"),
    "count_unbounded": ([], 4097),
    "tuple_notices": ((VALID,), 1),
    "unrepresentable_instant": ([dict(VALID, notified_at=10 ** 20)], 1),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_notice_shape_rejects_malformed_values(name):
    notices, count = MALFORMED[name]
    with pytest.raises(ProtocolError) as caught:
        validate_vote_assault_notifications(notices, count)
    assert (caught.value.code, caught.value.status) == ("vote_assault_notifications_invalid", 503)


def test_notice_shape_returns_the_native_mission_rows():
    assert validate_vote_assault_notifications([], 0) == ()
    second = dict(VALID, event_id=15, mission_id=550, mail_ids=[2])
    assert validate_vote_assault_notifications([VALID, second], 3) == (MISSION, 550)


def test_assault_schema_accepts_only_its_own_extension_layouts():
    recorded = RankRewardService._recorded_sections
    for count in range(len(SECTIONS) + 1):
        fixed = list(SECTIONS[:count])
        for tail in ([ASSAULT_SECTION], [LETSTD_SECTION, ASSAULT_SECTION], [EXPIRY_SECTION, ASSAULT_SECTION],
                     [LETSTD_SECTION, EXPIRY_SECTION, ASSAULT_SECTION]):
            marker = {"schema_version": ASSAULT_SCHEMA, "sections": fixed + tail}
            assert recorded(marker) == (DELIVERY_SCHEMAS[ASSAULT_SCHEMA], tuple(fixed + tail))
        for version, tail in ((ASSAULT_SCHEMA, []), (ASSAULT_SCHEMA, [EXPIRY_SECTION]),
                              (ASSAULT_SCHEMA, [ASSAULT_SECTION, EXPIRY_SECTION]), (ASSAULT_SCHEMA, [ASSAULT_SECTION] * 2),
                              (EXPIRY_SCHEMA, [ASSAULT_SECTION]), (EXPIRY_SCHEMA, [EXPIRY_SECTION, ASSAULT_SECTION]),
                              (LISTED_SCHEMA, [ASSAULT_SECTION])):
            with pytest.raises(ProtocolError) as caught:
                recorded({"schema_version": version, "sections": fixed + tail})
            assert caught.value.code == "rank_reward_history_invalid"


def test_assault_digest_chains_after_every_earlier_schema(flow, assault_sources):
    case = flow()
    plain = rank(case)
    for schema in DELIVERY_SCHEMAS.values():
        if schema.version != ASSAULT_SCHEMA:
            assert getattr(case.rank, schema.content) == getattr(plain, schema.content)
    assert plain._assault_receive_content_version == plain._expiry_receive_content_version
    assert case.rank._assault_receive_content_version not in {getattr(plain, schema.content)
                                                              for schema in DELIVERY_SCHEMAS.values()}
    status = case.rank.status()
    assert status["ready"] is True and status["vote_assault_notifications"] is True
    assert case.notifier.status()["ready"] is True and plain.status()["vote_assault_notifications"] is False
    moved = VoteAssaultNotifications(case.transactions.assault_expiry, assault_sources.__getitem__)
    moved.presents = copy(case.presents)
    assert (moved.status()["ready"], moved.status()["error"]) == (False, "vote_assault_notifications_changed")


@pytest.mark.parametrize("tickets", (True, False))
def test_first_116_after_the_conversion_carries_one_ret_row(flow, tickets):
    case = flow()
    service = rank(case, assault=case.notifier, tickets=tickets)
    record = convert(case)
    envelope = rank_request(case.store)
    payload = service.receive(envelope, case.store)
    reply = children(payload)
    assert ret(payload) == [{RET_COLUMN: MISSION}] and list(reply)[-1] == RET_GROUP and WIRE in payload
    assert NOTICE_GROUP not in reply and "PPT" not in reply and ("POSSESSION" in reply) is tickets
    saved = case.store.profile()
    notice = {"event_id": 16, "mission_id": MISSION, "gold": 6000, "mail_ids": record["mail_ids"],
              "notified_at": record["settled_at"]}
    marker = saved.state["rank_reward_delivery"]
    assert marker["schema_version"] == ASSAULT_SCHEMA and marker["rank_mail_ids"] == []
    assert marker["sections"] == [*(SECTIONS if tickets else ()), ASSAULT_SECTION]
    assert marker["vote_assault_notifications"] == [notice] and marker["vote_assault_notification_count"] == 1
    assert saved.state[ASSAULT_NOTIFICATION_KEY] == {"schema_version": 1, "records": {
        EVENT: {name: notice[name] for name in RECORD_FIELDS}}}
    assert saved.state["rank_reward_history"]["receipts"][-1]["content"] == service._assault_receive_content_version
    calls = case.clock.calls
    assert service.receive(envelope, case.store) == payload
    assert case.store.profile() == saved and case.clock.calls == calls
    reopened = create_app(case.store.path.parent, initial_state=saved.state,
        state_initializer=lambda state: service.initialize(case.service.initialize(state)),
        handlers=case.bootstrap.handlers() | case.presents.handlers() | case.service.handlers() | service.handlers())
    assert service.receive(envelope, reopened.state.store) == payload and reopened.state.store.profile() == saved
    later = service.receive(rank_request(case.store, 2001), case.store)
    assert ret(later) is None
    current = case.store.profile().state
    assert current["rank_reward_delivery"]["vote_assault_notifications"] == []
    assert current["rank_reward_delivery"]["vote_assault_notification_count"] == 1
    assert current[ASSAULT_NOTIFICATION_KEY] == saved.state[ASSAULT_NOTIFICATION_KEY]


def test_116_after_the_final_vote_end_leaves_the_conversion_to_startup(flow):
    case = flow()
    case.transactions.submit(vote(case, 30), case.store)
    case.clock.now = case.catalog.event.end_at
    assert ret(case.rank.receive(rank_request(case.store), case.store)) is None
    state = case.store.profile().state
    assert ASSAULT_KEY not in state and ASSAULT_NOTIFICATION_KEY not in state
    assert state["rank_reward_delivery"]["vote_assault_notification_count"] == 0
    startup(case, 1001)
    assert case.store.profile().state[ASSAULT_KEY]["events"][EVENT]["status"] == "settled"
    assert ret(case.rank.receive(rank_request(case.store, 2001), case.store)) == [{RET_COLUMN: MISSION}]


def test_ticket_and_challenge_conversions_notify_in_one_116(flow):
    case = flow()
    service = rank(case, assault=case.notifier, expiry=VoteExpiryNotifications(case.transactions.expiry), tickets=False)
    convert(case)
    tickets = case.store.profile().state["vote_expiry"]["items"][case.group.item_id]
    payload = service.receive(rank_request(case.store), case.store)
    assert list(children(payload))[-2:] == [NOTICE_GROUP, RET_GROUP]
    assert gold(payload) == [{NOTICE_COLUMN: tickets["amount"] * tickets["rate"]}]
    assert ret(payload) == [{RET_COLUMN: MISSION}]
    marker = case.store.profile().state["rank_reward_delivery"]
    assert marker["schema_version"] == ASSAULT_SCHEMA and marker["sections"] == [EXPIRY_SECTION, ASSAULT_SECTION]
    assert marker["vote_expiry_notification_count"] == marker["vote_assault_notification_count"] == 1


def test_letstd_preflights_carry_the_ret_row_and_the_receipt_replays(flow, native_sources, monkeypatch):
    case = flow()
    convert(case)
    service = rank(case, assault=case.notifier, tickets=False,
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
    assert marker["schema_version"] == ASSAULT_SCHEMA and marker["sections"] == [LETSTD_SECTION, ASSAULT_SECTION]
    assert marker[LETSTD_SECTION] and marker["vote_assault_notification_count"] == 1
    assert {"PPT", RESPONSE_NAME} <= set(reply) and list(reply)[-1] == RET_GROUP
    # Two final encodes plus at least one LetsTD candidate preflight, each already holding the RET row.
    assert len(encoded) > 2 and all(ret(candidate) == [{RET_COLUMN: MISSION}] for candidate in encoded)
    calls = case.clock.calls
    assert service.receive(envelope, case.store) == payload
    assert case.store.profile() == saved and case.clock.calls == calls


def replay_old_receipt_then_notify(case, schema, *, tickets, expiry=None):
    old = rank(case, tickets=tickets, expiry=expiry)
    convert(case)
    envelope = rank_request(case.store)
    payload = old.receive(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["rank_reward_delivery"]["schema_version"] == schema and ret(payload) is None
    upgraded = rank(case, tickets=tickets, expiry=expiry, assault=case.notifier)
    assert upgraded.initialize(deepcopy(saved.state)) == saved.state
    clock = case.bootstrap.clock
    case.bootstrap.clock = lambda: pytest.fail("An old receipt must not read a new clock")
    assert upgraded.receive(envelope, case.store) == payload and case.store.profile() == saved
    case.bootstrap.clock = clock
    assert ret(upgraded.receive(rank_request(case.store, 2001), case.store)) == [{RET_COLUMN: MISSION}]
    marker = case.store.profile().state["rank_reward_delivery"]
    assert marker["schema_version"] == ASSAULT_SCHEMA and marker["vote_assault_notification_count"] == 1
    assert marker["sections"][-1] == ASSAULT_SECTION
    assert ret(upgraded.receive(rank_request(case.store, 2002), case.store)) is None


@pytest.mark.parametrize("tickets,schema", ((False, 1), (True, 7)))
def test_old_receipts_replay_exactly_and_the_conversion_notifies_once(flow, tickets, schema):
    replay_old_receipt_then_notify(flow(), schema, tickets=tickets)


def test_old_expiry_receipts_replay_exactly_and_the_conversion_notifies_once(flow):
    case = flow()
    replay_old_receipt_then_notify(case, EXPIRY_SCHEMA, tickets=True,
                                   expiry=VoteExpiryNotifications(case.transactions.expiry))


@pytest.mark.parametrize("converted", (False, True))
def test_saved_assault_notice_history_requires_the_notifier(flow, converted):
    case = flow()
    if converted:
        convert(case)
    case.rank.receive(rank_request(case.store), case.store)
    state = case.store.profile().state
    assert (ASSAULT_NOTIFICATION_KEY in state) is converted
    plain = rank(case)
    with pytest.raises(ProtocolError) as caught:
        plain.initialize(deepcopy(state))
    assert (caught.value.code, caught.value.status) == ("rank_vote_assault_unavailable", 503)
    unmarked = {key: value for key, value in state.items() if key != "rank_reward_delivery"}
    if converted:
        with pytest.raises(ProtocolError) as caught:
            plain.initialize(deepcopy(unmarked))
        assert (caught.value.code, caught.value.status) == ("rank_vote_assault_unavailable", 503)
    else:
        assert plain.initialize(deepcopy(unmarked)) == unmarked


FORGERIES = {
    "domain_removed": (lambda state: state.pop(ASSAULT_NOTIFICATION_KEY), "vote_assault_notification_proof_lost"),
    "record_gold": (lambda state: state[ASSAULT_NOTIFICATION_KEY]["records"][EVENT].update(gold=1),
                    "vote_assault_notifications_invalid"),
    "record_mission": (lambda state: state[ASSAULT_NOTIFICATION_KEY]["records"][EVENT].update(mission_id=550),
                       "vote_assault_notifications_invalid"),
    "marker_mission": (lambda state: state["rank_reward_delivery"]["vote_assault_notifications"][0].update(mission_id=550),
                       "vote_assault_notifications_invalid"),
    "marker_count": (lambda state: state["rank_reward_delivery"].update(vote_assault_notification_count="1"),
                     "rank_reward_history_invalid"),
    "section_dropped": (lambda state: state["rank_reward_delivery"]["sections"].pop(), "rank_reward_history_invalid"),
}


@pytest.mark.parametrize("forgery", sorted(FORGERIES))
def test_forged_or_lost_notice_proof_fails_closed(flow, forgery):
    case = flow()
    convert(case)
    case.rank.receive(rank_request(case.store), case.store)
    state = deepcopy(case.store.profile().state)
    damage, code = FORGERIES[forgery]
    damage(state)
    with pytest.raises(ProtocolError) as caught:
        case.rank.initialize(deepcopy(state))
    assert (caught.value.code, caught.value.status) == (code, 503)
    replace_without_revision(case.store, state)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.rank.receive(rank_request(case.store, 2001), case.store)
    assert caught.value.code == code and case.store.profile() == before


def test_116_before_the_conversion_instant_refuses_without_writing(flow):
    case = flow()
    service = rank(case, assault=case.notifier, tickets=False)
    convert(case)
    case.clock.now -= timedelta(seconds=1)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        service.receive(rank_request(case.store), case.store)
    assert (caught.value.code, caught.value.status) == ("vote_assault_notification_clock_reversed", 409)
    assert case.store.profile() == before


def test_assault_notices_must_share_the_reward_mailbox(flow, assault_sources):
    case = flow()
    convert(case)
    rewards = VoteRewardService(case.catalog, copy(case.presents))
    foreign = VoteAssaultNotifications(VoteAssaultExpiryService(rewards), assault_sources.__getitem__)
    detached = rank(case, assault=foreign)
    assert (detached.status()["ready"], detached.status()["error"]) == (False, "rank_vote_assault_changed")
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        detached.receive(rank_request(case.store), case.store)
    assert (caught.value.code, caught.value.status) == ("rank_vote_assault_changed", 503)
    assert case.store.profile() == before


@pytest.mark.parametrize("votes", (0, 14))
def test_no_converted_rights_means_no_ret_or_proof(flow, votes):
    case = flow()
    if votes:
        case.transactions.submit(vote(case, votes), case.store)
    case.clock.now = case.catalog.event.end_at
    startup(case, 1001)
    payload = case.rank.receive(rank_request(case.store), case.store)
    assert RET_GROUP not in children(payload)
    state = case.store.profile().state
    marker = state["rank_reward_delivery"]
    assert marker["schema_version"] == ASSAULT_SCHEMA and marker["sections"][-1] == ASSAULT_SECTION
    assert marker["vote_assault_notifications"] == [] and marker["vote_assault_notification_count"] == 0
    assert ASSAULT_NOTIFICATION_KEY not in state and ASSAULT_KEY not in state


def test_pending_conversion_notifies_only_after_its_mail_lands(flow):
    case = flow()
    case.transactions.submit(vote(case, 30), case.store)
    profile = case.store.profile()
    fillers = [{"kind": "gold", "amount": 1, "description": "capacity"}] * (MAX_PRESENTS - len(case.presents.rows(profile.state)))
    case.store.update_state(case.presents.enqueue(profile.state, fillers)[0], profile.revision)
    case.clock.now = case.catalog.event.end_at
    startup(case, 1001)
    assert case.store.profile().state[ASSAULT_KEY]["events"][EVENT]["status"] == "pending"
    assert ret(case.rank.receive(rank_request(case.store), case.store)) is None
    assert ASSAULT_NOTIFICATION_KEY not in case.store.profile().state
    profile = case.store.profile()
    released = deepcopy(profile.state)
    released["tables"]["presents"] = [row for row in case.presents.rows(released) if row["Description"] != "capacity"]
    case.store.update_state(released, profile.revision)
    case.clock.now += timedelta(minutes=1)
    startup(case, 1002)
    assert case.store.profile().state[ASSAULT_KEY]["events"][EVENT]["status"] == "settled"
    assert ret(case.rank.receive(rank_request(case.store, 2001), case.store)) == [{RET_COLUMN: MISSION}]


def test_failed_116_encoding_rolls_back_the_ret_notice(flow, monkeypatch):
    case = flow()
    convert(case)
    before = case.store.profile()
    original = rank_module.encode_response

    def refusing(profile, payload):
        if 116 in profile.registration_keys and WIRE in payload:
            raise ProtocolError("response_too_large", "Injected 116 encoding failure", 500)
        return original(profile, payload)

    monkeypatch.setattr(rank_module, "encode_response", refusing)
    envelope = rank_request(case.store)
    with pytest.raises(ProtocolError):
        case.rank.receive(envelope, case.store)
    assert case.store.profile() == before
    monkeypatch.setattr(rank_module, "encode_response", original)
    assert ret(case.rank.receive(envelope, case.store)) == [{RET_COLUMN: MISSION}]


def test_concurrent_new_116_requests_notify_once(flow, monkeypatch):
    case = flow()
    convert(case)
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
    assert sorted(RET_GROUP in children(payload) for payload in payloads) == [False, True]
    state = case.store.profile().state
    assert len(state[ASSAULT_NOTIFICATION_KEY]["records"]) == 1
    assert state["rank_reward_delivery"]["vote_assault_notification_count"] == 1

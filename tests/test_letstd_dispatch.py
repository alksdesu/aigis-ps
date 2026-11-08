from copy import copy, deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, loads
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.letstd_catalog import LetsTDCatalog
from aigis_ps.letstd_rules import GROUPS, PARTNERS, LetsTDRules
from aigis_ps.letstd_service import LOCAL_LABEL, RESPONSE_NAME, LetsTDService
from aigis_ps.letstd_state import DELIVERED, LEDGER_KEY, PENDING
from aigis_ps.login_reward_service import LETSTD_SOURCE, SOURCES, STAMP_DESCRIPTION, LoginRewardService
import aigis_ps.login_reward_service as login_module
from aigis_ps.login_stamp_pending import PENDING_KEY, read_pending_stamps
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.rank_rewards import LETSTD_SECTION, SECTIONS, RankRewardService
import aigis_ps.rank_rewards as rank_module
from aigis_ps.storage import Store
from aigis_ps.vote_tickets import VoteTicketService
from test_letstd_catalog import native_sources
from test_letstd_service import DEFAULT_MAILS, keys, ledger_rows
from test_login_reward_service import stamp_variant
from test_quest_service import bundle, request
from test_vote_catalog import vote_sources
from test_vote_service import table_rows
from test_vote_transactions import NOW, catalog


ALL = {partner: {name: True for name in GROUPS.values()} for partner in PARTNERS}
CLOSED = {}
OSHIRORE_CAMPAIGN = {"oshirore": {"newChallenge": True}}
RLYEH_PURCHASE = {"rlyeh": {"newPurchase": True}}
CAMPAIGN_REWARDS = tuple(range(1, 26))
PURCHASE_REWARDS = (1, 2, 3, 4, 5, 6, 8, 9, 10, 11)
DEFAULT_ROWS = 105
LOGIN_SOURCES = ((2, "event_logins"), (3, "comeback_logins"), (4, "purchased_logins"), (5, "daily_gifts"),
                 (6, "vote_cooking"))
RANK_SOURCES = ((2, "event_passes"), (3, "event_logins"), (4, "comeback_logins"), (5, "purchased_logins"),
                (6, "daily_gifts"))
LOGIN = (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)))
RANK = (*LOGIN, Column("DmmPremiumReceiverId", "I", (0,)))
DAMAGES = ("dropped", "reordered", "unknown", "version", "grants")
# The real login tables stop before 2027-01, so this claim day holds its stamp until a later table arrives.
HELD_STAMP_DAY = datetime(2027, 1, 1, 4, tzinfo=JST)


@pytest.fixture
def dispatch(bundle, native_sources, catalog, monkeypatch, tmp_path):
    clock = SimpleNamespace(now=NOW, calls=0)

    def now():
        clock.calls += 1
        return clock.now

    monkeypatch.setattr(bundle.bootstrap, "clock", now)
    table = LetsTDCatalog(native_sources.__getitem__)
    tickets = VoteTicketService(bundle.possessions, catalog)
    stores = []

    def letstd(partners=ALL, *, native_popup=False):
        return LetsTDService(table, bundle.presents, LetsTDRules(partners=partners, native_popup=native_popup))

    def sources(schema, owners, without, added):
        chosen = {name: getattr(bundle, name) for threshold, name in owners if schema >= threshold}
        if schema >= 7:
            chosen["vote_tickets"] = tickets
        return {name: service for name, service in {**chosen, **added}.items() if name not in without}

    def login(service=None, schema=1, *, without=(), **added):
        return LoginRewardService(bundle.bootstrap, bundle.player_state, bundle.presents, bundle.login_rewards.catalog,
                                  letstd=service, **sources(schema, LOGIN_SOURCES, without, added))

    def rank(service=None, schema=1, *, without=(), **added):
        return RankRewardService(bundle.bootstrap, bundle.player_state, bundle.presents, bundle.rank_rewards.catalog,
                                 letstd=service, **sources(schema, RANK_SOURCES, without, added))

    def store():
        stores.append(Store(tmp_path / f"letstd-dispatch-{len(stores)}.sqlite3",
                            bundle.player_state.initialize(bundle.bootstrap.initial_state())))
        return stores[-1]

    return SimpleNamespace(clock=clock, presents=bundle.presents, letstd=letstd, login=login, rank=rank, store=store)


def login_request(store, tick=1000):
    return request(store, 115, LOGIN, tick=tick)


def rank_request(store, tick=1000):
    return request(store, 116, RANK, tick=tick)


def children(payload):
    return {node.name: node for node in loads(payload).children}


def present_ids(state):
    return {row["PresentID"] for row in state["tables"]["presents"]}


def letstd_mails(dispatch, state):
    return [row for row in dispatch.presents.rows(state) if row["Description"].startswith(LOCAL_LABEL)]


def filled(dispatch, store, free):
    profile = store.profile()
    rewards = [{"kind": "gold", "amount": 1, "description": "capacity"}] * (
        MAX_PRESENTS - len(dispatch.presents.rows(profile.state)) - free)
    state, _ = dispatch.presents.enqueue(profile.state, rewards)
    store.update_state(state, profile.revision)


def emptied(dispatch, store):
    profile = store.profile()
    state = deepcopy(profile.state)
    state["tables"]["presents"] = [row for row in dispatch.presents.rows(state) if row["Description"] != "capacity"]
    store.update_state(state, profile.revision)


def replace_without_revision(store, state):
    with store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def damaged(marker, damage):
    if damage == "dropped":
        marker["sections"].pop()
    elif damage == "reordered":
        marker["sections"].reverse()
    elif damage == "unknown":
        marker["sections"].insert(0, "unknown_grants")
    elif damage == "version":
        marker["schema_version"] = 9
    else:
        marker[LETSTD_SOURCE][0]["mail_ids"] = []


def validations(monkeypatch, module, service, registration):
    encoded, validated = [], []
    original_encode, original_settle = module.encode_response, service.settle

    def recording(profile, payload):
        encoded.append((profile.registration_keys, payload))
        return original_encode(profile, payload)

    def settle(state, now, *, source, validate=None):
        def inspected(candidate, grants):
            start = len(encoded)
            validate(candidate, grants)
            validated.append([payload for keys, payload in encoded[start:] if keys == (registration,)])
        return original_settle(state, now, source=source, validate=inspected)

    monkeypatch.setattr(module, "encode_response", recording)
    monkeypatch.setattr(service, "settle", settle)
    return validated


def test_default_pools_mail_every_row_once_across_rank_and_login(dispatch):
    store = dispatch.store()
    dispatch.rank(dispatch.letstd(OSHIRORE_CAMPAIGN)).receive(rank_request(store), store)
    ranked = store.profile().state["rank_reward_delivery"][LETSTD_SECTION]
    assert keys(ranked) == [("oshirore", 1, reward) for reward in CAMPAIGN_REWARDS]
    opened = dispatch.letstd(ALL)
    dispatch.login(opened).receive(login_request(store), store)
    state = store.profile().state
    logged = state["login_reward_delivery"][LETSTD_SOURCE]
    assert {grant["via"] for grant in ranked} == {116} and {grant["via"] for grant in logged} == {115}
    assert not set(keys(ranked)) & set(keys(logged))
    assert len(ranked) + len(logged) == len(ledger_rows(state)) == DEFAULT_ROWS
    assert {row["status"] for row in ledger_rows(state).values()} == {DELIVERED}
    assert len(letstd_mails(dispatch, state)) == DEFAULT_MAILS
    dispatch.rank(opened).receive(rank_request(store, 1001), store)
    again = store.profile().state
    assert again["rank_reward_delivery"][LETSTD_SECTION] == [] and again[LEDGER_KEY] == state[LEDGER_KEY]
    assert len(letstd_mails(dispatch, again)) == DEFAULT_MAILS


@pytest.mark.parametrize("schema", range(1, 8))
def test_closed_switches_keep_every_login_child_and_old_receipts_replay(dispatch, schema):
    legacy_store, listed_store = dispatch.store(), dispatch.store()
    legacy, listed = dispatch.login(schema=schema), dispatch.login(dispatch.letstd(CLOSED), schema)
    legacy_envelope, listed_envelope = login_request(legacy_store), login_request(listed_store)
    legacy_payload = legacy.receive(legacy_envelope, legacy_store)
    listed_payload = listed.receive(listed_envelope, listed_store)
    assert loads(legacy_payload).children[1:] == loads(listed_payload).children[1:]
    old, new = legacy_store.profile().state, listed_store.profile().state
    old_marker, new_marker = old["login_reward_delivery"], new["login_reward_delivery"]
    assert old_marker["schema_version"] == schema and new_marker["schema_version"] == 8
    assert new_marker["sections"] == [*SOURCES[:schema - 1], LETSTD_SOURCE] and new_marker[LETSTD_SOURCE] == []
    assert new_marker["base_mail_ids"] == old_marker.get("base_mail_ids", old_marker["mail_ids"])
    ignored = {"schema_version", "request", "sections", "base_mail_ids", LETSTD_SOURCE}
    assert ({key: value for key, value in new_marker.items() if key not in ignored}
            == {key: value for key, value in old_marker.items() if key not in ignored})
    assert LEDGER_KEY not in new
    receipt = new["login_reward_history"]["receipts"][-1]
    assert set(receipt["sources"]) == set(new_marker["sections"])
    assert receipt["content"] == listed._letstd_receive_content_version != listed.receive_content_version
    saved = legacy_store.profile()
    upgraded = dispatch.login(dispatch.letstd(ALL, native_popup=True), schema)
    assert upgraded.receive(legacy_envelope, legacy_store) == legacy_payload and legacy_store.profile() == saved
    calls = dispatch.clock.calls
    assert listed.receive(listed_envelope, listed_store) == listed_payload and dispatch.clock.calls == calls


@pytest.mark.parametrize("schema", range(1, 8))
def test_closed_switches_keep_every_rank_child_and_old_receipts_replay(dispatch, schema):
    legacy_store, listed_store = dispatch.store(), dispatch.store()
    legacy, listed = dispatch.rank(schema=schema), dispatch.rank(dispatch.letstd(CLOSED), schema)
    legacy_envelope, listed_envelope = rank_request(legacy_store), rank_request(listed_store)
    legacy_payload = legacy.receive(legacy_envelope, legacy_store)
    listed_payload = listed.receive(listed_envelope, listed_store)
    assert loads(legacy_payload).children[1:] == loads(listed_payload).children[1:]
    old_marker = legacy_store.profile().state["rank_reward_delivery"]
    new_marker = listed_store.profile().state["rank_reward_delivery"]
    fixed = SECTIONS[:schema - 1]
    assert old_marker["schema_version"] == schema and new_marker["schema_version"] == 8
    assert new_marker["sections"] == [*fixed, LETSTD_SECTION] and new_marker[LETSTD_SECTION] == []
    assert new_marker["rank_mail_ids"] == old_marker.get("rank_mail_ids", old_marker["mail_ids"])
    ignored = {"schema_version", "request", "sections", "rank_mail_ids", LETSTD_SECTION}
    assert ({key: value for key, value in new_marker.items() if key not in ignored}
            == {key: value for key, value in old_marker.items() if key not in ignored})
    assert ("vote_ticket_observation" in new_marker) == ("vote_features" in fixed)
    receipt = listed_store.profile().state["rank_reward_history"]["receipts"][-1]
    assert receipt["content"] == listed._letstd_receive_content_version != listed.receive_content_version
    saved = legacy_store.profile()
    upgraded = dispatch.rank(dispatch.letstd(ALL, native_popup=True), schema)
    assert upgraded.receive(legacy_envelope, legacy_store) == legacy_payload and legacy_store.profile() == saved
    calls = dispatch.clock.calls
    assert listed.receive(listed_envelope, listed_store) == listed_payload and dispatch.clock.calls == calls


def test_login_validates_the_full_inventory_response_and_replays_exactly(dispatch, monkeypatch):
    store = dispatch.store()
    service = dispatch.letstd(OSHIRORE_CAMPAIGN, native_popup=True)
    login = dispatch.login(service, schema=7)
    validated = validations(monkeypatch, login_module, service, 115)
    before = present_ids(store.profile().state)
    envelope = login_request(store)
    payload = login.receive(envelope, store)
    assert validated and all(len(payloads) == 1 for payloads in validated) and validated[-1] == [payload]
    assert {"POSSESSION", "PPT", RESPONSE_NAME} <= set(children(validated[0][0]))
    reply, saved = children(payload), store.profile()
    marker = saved.state["login_reward_delivery"]
    assert marker["sections"] == [*SOURCES, LETSTD_SOURCE]
    assert marker["vote_ticket_observation"] == saved.state["vote_tickets"]
    assert {"POSSESSION", "VoteGiftParam"} <= set(reply) and list(reply)[-1] == RESPONSE_NAME
    letstd_ids = [mail for grant in marker[LETSTD_SOURCE] for mail in grant["mail_ids"]]
    recorded = login_module._source_mail_ids({name: marker[name] for name in marker["sections"]})
    assert recorded[-len(letstd_ids):] == letstd_ids and marker["mail_ids"] == [*marker["base_mail_ids"], *recorded]
    assert set(marker["mail_ids"]) == present_ids(saved.state) - before
    assert [row["PresentID"] for row in table_rows(reply["PPT"])] == marker["mail_ids"]
    assert table_rows(reply[RESPONSE_NAME]) == [{"ServiceId": "oshirore",
        "newChallenge": ",".join(map(str, CAMPAIGN_REWARDS)), "newPurchase": ""}]
    calls = dispatch.clock.calls
    assert login.receive(envelope, store) == payload
    assert store.profile() == saved and dispatch.clock.calls == calls


def test_rank_delivers_after_daily_and_vote_sources_and_replays_exactly(dispatch, monkeypatch):
    store = dispatch.store()
    service = dispatch.letstd(RLYEH_PURCHASE, native_popup=True)
    rank = dispatch.rank(service, schema=7)
    validated = validations(monkeypatch, rank_module, service, 116)
    before = present_ids(store.profile().state)
    envelope = rank_request(store)
    payload = rank.receive(envelope, store)
    assert validated and all(len(payloads) == 1 for payloads in validated) and validated[-1] == [payload]
    assert {"POSSESSION", "PPT", RESPONSE_NAME} <= set(children(validated[0][0]))
    reply, saved = children(payload), store.profile()
    marker = saved.state["rank_reward_delivery"]
    assert marker["sections"] == [*SECTIONS, LETSTD_SECTION]
    assert marker["vote_ticket_observation"] == saved.state["vote_tickets"]
    letstd_ids = [mail for grant in marker[LETSTD_SECTION] for mail in grant["mail_ids"]]
    assert letstd_ids and marker["mail_ids"][-len(letstd_ids):] == letstd_ids
    assert set(marker["mail_ids"]) == present_ids(saved.state) - before
    assert [row["PresentID"] for row in table_rows(reply["PPT"])] == marker["mail_ids"]
    assert "POSSESSION" in reply and list(reply)[-1] == RESPONSE_NAME
    assert table_rows(reply[RESPONSE_NAME]) == [{"ServiceId": "rlyeh", "newChallenge": "",
                                                 "newPurchase": ",".join(map(str, PURCHASE_REWARDS))}]
    assert saved.state["rank_reward_history"]["receipts"][-1]["content"] == rank._letstd_receive_content_version
    calls = dispatch.clock.calls
    assert rank.receive(envelope, store) == payload
    assert store.profile() == saved and dispatch.clock.calls == calls


def test_reward_param_appears_only_for_new_rows_with_the_native_popup(dispatch):
    hidden_store, shown_store = dispatch.store(), dispatch.store()
    hidden = children(dispatch.login(dispatch.letstd(RLYEH_PURCHASE)).receive(login_request(hidden_store), hidden_store))
    assert RESPONSE_NAME not in hidden and letstd_mails(dispatch, hidden_store.profile().state)
    popup = dispatch.letstd(RLYEH_PURCHASE, native_popup=True)
    shown = children(dispatch.login(popup).receive(login_request(shown_store), shown_store))
    assert list(shown) == [*hidden, RESPONSE_NAME]
    quiet = children(dispatch.rank(popup).receive(rank_request(shown_store), shown_store))
    assert RESPONSE_NAME not in quiet and "PPT" not in quiet


def test_capacity_failures_leave_rows_pending_until_space_returns(dispatch, monkeypatch):
    store = dispatch.store()
    service = dispatch.letstd(OSHIRORE_CAMPAIGN)
    filled(dispatch, store, 5)
    dispatch.rank(service).receive(rank_request(store), store)
    rows = ledger_rows(store.profile().state)
    assert sorted(key for key, row in rows.items() if row["status"] == DELIVERED) == [
        ("oshirore", 1, reward) for reward in CAMPAIGN_REWARDS[:5]]
    assert {row["reason"] for row in rows.values() if row["status"] == PENDING} == {"present_capacity"}
    emptied(dispatch, store)
    login = dispatch.login(service)
    original = login._response

    def bounded(profile, state, mail_ids, *args, letstd_grants=None, **kwargs):
        if letstd_grants is not None and len(letstd_grants) > 3:
            raise ProtocolError("login_reward_response_capacity", "Injected response capacity", 503)
        return original(profile, state, mail_ids, *args, letstd_grants=letstd_grants, **kwargs)

    monkeypatch.setattr(login, "_response", bounded)
    login.receive(login_request(store), store)
    state = store.profile().state
    assert [grant["reward_id"] for grant in state["login_reward_delivery"][LETSTD_SOURCE]] == [6, 7, 8]
    assert {row["reason"] for row in ledger_rows(state).values() if row["status"] == PENDING} == {
        "login_reward_response_capacity"}
    monkeypatch.setattr(login, "_response", original)
    dispatch.clock.now += timedelta(hours=1)
    login.receive(login_request(store, 1001), store)
    state = store.profile().state
    assert {row["status"] for row in ledger_rows(state).values()} == {DELIVERED}
    assert len(letstd_mails(dispatch, state)) == len(CAMPAIGN_REWARDS)


def test_rules_switch_keeps_startup_month_refresh_and_rank_application_available(dispatch):
    store = dispatch.store()
    rank_envelope, login_envelope = rank_request(store), login_request(store)
    dispatch.rank(dispatch.letstd(OSHIRORE_CAMPAIGN)).receive(rank_envelope, store)
    dispatch.login(dispatch.letstd(RLYEH_PURCHASE)).receive(login_envelope, store)
    profile = store.profile()
    assert profile.state["rank_reward_delivery"][LETSTD_SECTION] and profile.state["login_reward_delivery"][LETSTD_SOURCE]
    switched = dispatch.letstd(ALL, native_popup=True)
    login, rank = dispatch.login(switched), dispatch.rank(switched)
    assert login.initialize(profile.state, NOW) == profile.state
    login.prepare_month(profile.state, NOW + timedelta(days=31))
    assert rank.initialize(profile.state) == profile.state
    updated, mail_ids = rank.apply(profile.state, profile.state, profile)
    assert mail_ids == () and updated[LEDGER_KEY] == profile.state[LEDGER_KEY]
    for service, envelope, code in ((login, login_envelope, "login_reward_retry_stale"),
                                    (rank, rank_envelope, "rank_reward_retry_stale")):
        with pytest.raises(ProtocolError) as error:
            service.receive(envelope, store)
        assert (error.value.code, error.value.status) == (code, 409)
    assert store.profile() == profile


def test_missing_service_loads_listed_history_and_leaves_its_ledger_to_the_server(dispatch):
    store = dispatch.store()
    closed = dispatch.letstd(CLOSED)
    dispatch.rank(closed).receive(rank_request(store), store)
    envelope = login_request(store)
    dispatch.login(closed).receive(envelope, store)
    profile = store.profile()
    login, rank = dispatch.login(), dispatch.rank()
    assert login.initialize(profile.state, NOW) == profile.state
    login.prepare_month(profile.state, NOW + timedelta(days=31))
    assert rank.initialize(profile.state) == profile.state
    rank.apply(profile.state, profile.state, profile)
    with pytest.raises(ProtocolError) as error:
        login.receive(envelope, store)
    assert (error.value.code, error.value.status) == ("login_reward_retry_stale", 409)
    delivered = dispatch.store()
    dispatch.rank(dispatch.letstd(OSHIRORE_CAMPAIGN)).receive(rank_request(delivered), delivered)
    envelope = login_request(delivered)
    dispatch.login(dispatch.letstd(RLYEH_PURCHASE)).receive(envelope, delivered)
    profile = delivered.profile()
    assert profile.state["rank_reward_delivery"][LETSTD_SECTION] and profile.state["login_reward_delivery"][LETSTD_SOURCE]
    assert login.initialize(profile.state, NOW) == profile.state
    assert rank.initialize(profile.state) == profile.state
    rank.apply(profile.state, profile.state, profile)
    with pytest.raises(ProtocolError) as error:
        login.receive(envelope, delivered)
    assert (error.value.code, error.value.status) == ("letstd_unavailable", 503)
    assert delivered.profile() == profile


def test_letstd_must_share_the_reward_mailbox_and_reports_its_status(dispatch):
    shared = dispatch.letstd(OSHIRORE_CAMPAIGN)
    login, rank = dispatch.login(shared), dispatch.rank(shared)
    assert login.status()["ready"] and login.status()["letstd"] == shared.status()
    assert rank.status()["ready"] and rank.status()["letstd"] is True
    foreign = LetsTDService(shared.catalog, copy(dispatch.presents), shared.rules)
    with pytest.raises(ProtocolError) as error:
        dispatch.login(foreign)
    assert (error.value.code, error.value.status) == ("login_letstd_dependencies_invalid", 503)
    detached = dispatch.rank(foreign)
    assert (detached.status()["ready"], detached.status()["error"]) == (False, "rank_letstd_changed")
    store = dispatch.store()
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        detached.receive(rank_request(store), store)
    assert (error.value.code, error.value.status) == ("rank_letstd_changed", 503) and store.profile() == before


@pytest.mark.parametrize("operation", (115, 116))
def test_replay_rejects_a_ledger_that_differs_from_its_receipt(dispatch, operation):
    store = dispatch.store()
    service = dispatch.letstd(RLYEH_PURCHASE)
    if operation == 115:
        owner, envelope, module, history = dispatch.login(service), login_request(store), login_module, "login_reward_history"
    else:
        owner, envelope, module, history = dispatch.rank(service), rank_request(store), rank_module, "rank_reward_history"
    owner.receive(envelope, store)
    state = deepcopy(store.profile().state)
    row = state[LEDGER_KEY]["partners"]["rlyeh"]["2"]["rows"][str(PURCHASE_REWARDS[0])]
    row["popup"] = not row["popup"]
    state[history]["receipts"][-1]["state"] = module._state_digest(state)
    replace_without_revision(store, state)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        owner.receive(envelope, store)
    assert (error.value.code, error.value.status) == ("letstd_state_invalid", 503) and store.profile() == before


@pytest.mark.parametrize("damage", DAMAGES)
def test_tampered_login_sections_fail_closed(dispatch, damage):
    store = dispatch.store()
    login = dispatch.login(dispatch.letstd(RLYEH_PURCHASE), schema=2)
    envelope = login_request(store)
    login.receive(envelope, store)
    state = deepcopy(store.profile().state)
    damaged(state["login_reward_delivery"], damage)
    state["login_reward_history"]["receipts"][-1]["state"] = login_module._state_digest(state)
    replace_without_revision(store, state)
    startup = {"version": None, "grants": "letstd_state_invalid"}.get(damage, "login_reward_history_invalid")
    if startup is None:
        assert login.initialize(state, NOW) == state
    else:
        with pytest.raises(ProtocolError) as error:
            login.initialize(state, NOW)
        assert (error.value.code, error.value.status) == (startup, 503)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        login.receive(envelope, store)
    assert error.value.code == "login_reward_history_invalid" and store.profile() == before


@pytest.mark.parametrize("damage", DAMAGES)
def test_tampered_rank_sections_fail_closed(dispatch, damage):
    store = dispatch.store()
    rank = dispatch.rank(dispatch.letstd(RLYEH_PURCHASE), schema=2)
    envelope = rank_request(store)
    rank.receive(envelope, store)
    state = deepcopy(store.profile().state)
    damaged(state["rank_reward_delivery"], damage)
    replace_without_revision(store, state)
    with pytest.raises(ProtocolError) as error:
        rank.initialize(state)
    assert (error.value.code, error.value.status) == ("rank_reward_history_invalid", 503)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        rank.receive(envelope, store)
    assert error.value.code == "rank_reward_history_invalid" and store.profile() == before


def test_receipt_content_and_shape_follow_the_listed_sources(dispatch):
    login = dispatch.login(dispatch.letstd(RLYEH_PURCHASE), schema=7)
    for count in range(len(SOURCES) + 1):
        listed = {**{name: None if name == "vote_ticket_observation" else [] for name in SOURCES[:count]},
                  LETSTD_SOURCE: []}
        assert login._receipt_content(listed) == login._letstd_receive_content_version
        assert login_module._receipt_valid({"mail_ids": [], "beginner_ordinal": 0, "sources": listed})
    for sources in ({LETSTD_SOURCE: [], "unknown_grants": []}, {SOURCES[1]: [], LETSTD_SOURCE: []},
                    dict.fromkeys(SOURCES[1:3], [])):
        assert not login_module._receipt_valid({"mail_ids": [], "beginner_ordinal": 0, "sources": sources})


@pytest.mark.parametrize("first, second", [(CLOSED, CLOSED), (OSHIRORE_CAMPAIGN, RLYEH_PURCHASE)])
def test_released_login_stamps_share_the_listed_receipt_with_letstd(bundle, dispatch, first, second):
    dispatch.clock.now = HELD_STAMP_DAY
    store = dispatch.store()
    dispatch.login(dispatch.letstd(first)).receive(login_request(store), store)
    [held] = read_pending_stamps(store.profile().state)
    assert held["reason"] == "login_reward_month_unavailable"
    stamps = stamp_variant(bundle.login_rewards.catalog, copies=(1,))
    login = LoginRewardService(bundle.bootstrap, bundle.player_state, bundle.presents, stamps,
                               letstd=dispatch.letstd(second))
    dispatch.clock.now += timedelta(days=1)
    envelope = login_request(store, 1001)
    payload = login.receive(envelope, store)
    after = store.profile()
    marker = after.state["login_reward_delivery"]
    rows = {row["PresentID"]: row for row in after.state["tables"]["presents"]}
    granted = [mail for grant in marker[LETSTD_SOURCE] for mail in grant["mail_ids"]]
    base = [rows[mail]["Description"] for mail in marker["base_mail_ids"]]
    assert PENDING_KEY not in after.state and (marker["schema_version"], marker["sections"]) == (8, [LETSTD_SOURCE])
    assert marker["mail_ids"] == [*marker["base_mail_ids"], *granted] and bool(granted) == (second is RLYEH_PURCHASE)
    assert base.count(STAMP_DESCRIPTION) == 2 and base[-1] == STAMP_DESCRIPTION
    assert login.receive(envelope, store) == payload and store.profile() == after
    assert login.initialize(deepcopy(after.state)) == after.state

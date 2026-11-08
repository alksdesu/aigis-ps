from copy import deepcopy
from datetime import timedelta
import json

import pytest

import aigis_ps.login_reward_service as module
from aigis_ps.errors import ProtocolError
from test_login_reward_service import case, factory, groups, message
from test_quest_service import bundle


def seed_unrelated_mail(case):
    profile = case.store.profile()
    updated, added = case.presents.enqueue(profile.state, [
        {"kind": "free_crystal", "amount": 11, "description": "unrelated existing reward"}])
    case.store.update_state(updated, profile.revision)
    return added[0]["PresentID"]


def replace_without_revision(case, state):
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def assert_invalid_replay(case, envelope):
    before = case.store.profile()
    calls = case.clock.calls
    with pytest.raises(ProtocolError) as caught:
        case.service.receive(envelope, case.store)
    assert caught.value.code == "login_reward_history_invalid"
    assert case.store.profile() == before
    assert case.clock.calls == calls


def test_forging_receipt_mail_ids_and_valid_response_cannot_replace_delivery(case):
    unrelated = seed_unrelated_mail(case)
    envelope = message(case)
    case.service.receive(envelope, case.store)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    receipt = state["login_reward_history"]["receipts"][-1]
    actual_delivery = deepcopy(state["login_reward_delivery"])
    assert unrelated not in actual_delivery["mail_ids"]
    receipt["mail_ids"] = [unrelated]
    receipt["response"] = case.service._response(
        profile, profile.state, [unrelated], receipt["beginner_ordinal"]).decode("utf-8")
    assert state["login_reward_delivery"] == actual_delivery
    assert receipt["state"] == module._state_digest(state)
    replace_without_revision(case, state)
    assert_invalid_replay(case, envelope)


def test_delivery_mismatch_is_rejected_even_when_receipt_state_hash_matches(case):
    unrelated = seed_unrelated_mail(case)
    envelope = message(case)
    case.service.receive(envelope, case.store)
    state = deepcopy(case.store.profile().state)
    receipt = state["login_reward_history"]["receipts"][-1]
    assert state["login_reward_delivery"]["mail_ids"] == receipt["mail_ids"]
    state["login_reward_delivery"]["mail_ids"] = [unrelated]
    receipt["state"] = module._state_digest(state)
    replace_without_revision(case, state)
    assert_invalid_replay(case, envelope)


def test_empty_delivery_overwrites_last_manifest_and_replays_without_new_mail(case):
    case.service.receive(message(case), case.store)
    first = case.store.profile()
    assert first.state["login_reward_delivery"]["mail_ids"]
    envelope = message(case, tick=1001)
    payload = case.service.receive(envelope, case.store)
    saved = case.store.profile()
    receipt = saved.state["login_reward_history"]["receipts"][-1]
    assert saved.state["login_reward_delivery"] == {
        "schema_version": 1, "request": receipt["request"], "mail_ids": [], "beginner_ordinal": 0}
    assert saved.state["tables"]["presents"] == first.state["tables"]["presents"]
    assert saved.state["last_present_id"] == first.state["last_present_id"]
    assert set(groups(payload)) == {"SYSTEM", "PGP2"}
    calls = case.clock.calls
    case.clock.now += timedelta(days=1)
    assert case.service.receive(envelope, case.store) == payload
    assert case.store.profile() == saved
    assert case.clock.calls == calls

from copy import deepcopy
import dataclasses
from datetime import datetime
import inspect
import json

import pytest

from aigis_ps import cli, vote_cooking_notifications, vote_expiry
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.rank_rewards import ASSAULT_SCHEMA, ASSAULT_SECTION, EXPIRY_SECTION, RankRewardService
from aigis_ps.storage import Store
from aigis_ps.vote_expiry import ASSAULT_NOTIFICATION_KEY, NOTIFICATION_KEY
from test_cli import assembled
from test_letstd_dispatch import rank_request
from test_quest_service import bundle


RECEIVED_AT = datetime(2026, 11, 3, 12, tzinfo=JST)
EXPIRY, ASSAULT = "vote_expiry_notifier", "vote_assault_notifier"
NOTIFIERS = {EXPIRY: ("vote_expiry_notifications", "VoteExpiryNotifications", "expiry"),
             ASSAULT: ("vote_assault_notifications", "VoteAssaultNotifications", "assault_expiry")}
REFUSED = {EXPIRY: "vote_expiry_notices_refused", ASSAULT: "vote_assault_notices_refused"}
SAVED_NOTICES = ((EXPIRY, NOTIFICATION_KEY, "vote_expiry_notifications_unavailable", "vote_expiry_notifications_invalid"),
                 (ASSAULT, ASSAULT_NOTIFICATION_KEY, "vote_assault_notifications_unavailable",
                  "vote_assault_notifications_invalid"))
RECEIPT_REFUSALS = ((EXPIRY, "rank_vote_expiry_unavailable"), (ASSAULT, "rank_vote_assault_unavailable"))
COOKING_REFUSED = "vote_cooking_notices_refused"
STARTUP_REFUSAL = "Required startup data is unavailable; run doctor for details"


@pytest.fixture(scope="module")
def refused():
    def refuse(code):
        def build(*args):
            raise ProtocolError(code, "Vote notices are refused for this test", 503)
        return build

    with pytest.MonkeyPatch.context() as patch:
        for field, (_, constructor, _) in NOTIFIERS.items():
            patch.setattr(vote_expiry, constructor, refuse(REFUSED[field]))
        return cli.services(assembled())


@pytest.fixture
def received(monkeypatch, tmp_path):
    def receive(services, name):
        monkeypatch.setattr(services.bootstrap, "clock", lambda: RECEIVED_AT)
        store = Store(tmp_path / f"{name}.sqlite3", services.initialize(services.bootstrap.initial_state()))
        services.rank_rewards.receive(rank_request(store), store)
        return store.profile().state

    return receive


def refusal(action):
    with pytest.raises(ProtocolError) as caught:
        action()
    return caught.value.code, caught.value.status


def without(services, field):
    rank = services.rank_rewards
    # Every optional service is a keyword-only parameter kept under its own name, so the copy follows new ones.
    optional = [name for name, parameter in inspect.signature(RankRewardService).parameters.items()
                if parameter.kind is parameter.KEYWORD_ONLY]
    variant = RankRewardService(rank.bootstrap, rank.players, rank.presents, rank.catalog,
                                **{name: getattr(rank, name) for name in optional if name != field})
    return dataclasses.replace(services, rank_rewards=variant, **{field: None})


@pytest.mark.parametrize("field", list(NOTIFIERS))
def test_the_notifiers_reach_116_over_the_vote_transactions(bundle, field):
    domain, _, source = NOTIFIERS[field]
    notifier = getattr(bundle, field)
    assert notifier is not None, getattr(bundle, field + "_error")
    assert getattr(bundle, field + "_error") is None
    assert notifier.expiry is getattr(bundle.votes.transactions, source)
    assert getattr(bundle.rank_rewards, field) is notifier and notifier.presents is bundle.presents
    ready = cli.service_readiness(bundle)
    assert ready[domain] == notifier.status() and ready[domain]["ready"] is True
    assert ready["rank_rewards"]["ready"] is True and ready["rank_rewards"][domain] is True


def test_doctor_reports_the_missing_notifier_dependencies_without_resources(tmp_path, capsys):
    data = tmp_path / "server"
    assert cli.main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "missing")]) == 0
    readiness = json.loads(capsys.readouterr().out)["readiness"]
    for domain, *_ in NOTIFIERS.values():
        assert readiness[domain] == {"ready": False, "error": f"{domain}_dependencies_unavailable"}
    assert not data.exists()


def test_refused_notifiers_leave_116_ready_without_their_sections(bundle, refused, received):
    ready = cli.service_readiness(refused)
    for field, (domain, *_) in NOTIFIERS.items():
        assert (getattr(refused, field), getattr(refused, field + "_error")) == (None, REFUSED[field])
        assert getattr(refused.rank_rewards, field) is None and ready["rank_rewards"][domain] is False
        assert ready[domain] == {"ready": False, "error": REFUSED[field]}
    assert ready["rank_rewards"]["ready"] is True
    assert ready["startup_ready"] == cli.service_readiness(bundle)["startup_ready"]
    saved = received(refused, "unwired")
    assert not {EXPIRY_SECTION, ASSAULT_SECTION} & set(saved["rank_reward_delivery"]["sections"])
    assert bundle.initialize(deepcopy(saved)) == saved == refused.initialize(deepcopy(saved))


def test_wired_receipts_record_both_notice_sections_and_require_each_notifier(bundle, received):
    saved = received(bundle, "wired")
    marker = saved["rank_reward_delivery"]
    assert marker["schema_version"] == ASSAULT_SCHEMA
    assert marker["sections"][-2:] == [EXPIRY_SECTION, ASSAULT_SECTION]
    assert marker[EXPIRY_SECTION] == marker[ASSAULT_SECTION] == []
    assert bundle.initialize(deepcopy(saved)) == saved
    for field, code in RECEIPT_REFUSALS:
        before = deepcopy(saved)
        assert refusal(lambda: without(bundle, field).initialize(saved)) == (code, 503)
        assert saved == before


@pytest.mark.parametrize("field,key,unavailable,invalid", SAVED_NOTICES)
def test_saved_notices_require_their_notifier(bundle, field, key, unavailable, invalid):
    state = {**bundle.initialize(bundle.bootstrap.initial_state()), key: {}}
    before = deepcopy(state)
    assert refusal(lambda: dataclasses.replace(bundle, **{field: None}).initialize(state)) == (unavailable, 503)
    assert refusal(lambda: bundle.initialize(state)) == (invalid, 503)
    assert state == before


def test_a_cooking_notifier_that_fails_to_build_withholds_startup():
    def refuse(_):
        raise ProtocolError(COOKING_REFUSED, "Cooking notices are refused for this test", 503)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(vote_cooking_notifications, "VoteCookingNotifications", refuse)
        refused = cli.services(assembled())
    assert refused.vote_cooking is not None, refused.vote_cooking_error
    assert (refused.vote_cooking_notifier, refused.vote_cooking_notifier_error) == (None, COOKING_REFUSED)
    ready = cli.service_readiness(refused)
    assert ready["vote_cooking_notifications"] == {"ready": False, "error": COOKING_REFUSED}
    assert ready["startup_ready"] is False
    assert STARTUP_REFUSAL in cli.serve_gate(refused, refused.masters.expected_hash, ready)


def test_an_absent_cooking_service_keeps_the_startup_baseline(bundle):
    absent = dataclasses.replace(bundle, vote_cooking=None, vote_cooking_error="vote_cooking_dependencies_unavailable",
                                 vote_cooking_notifier=None,
                                 vote_cooking_notifier_error="vote_cooking_notifications_dependencies_unavailable")
    baseline = cli.service_readiness(bundle)
    ready = cli.service_readiness(absent)
    assert ready["startup_ready"] == baseline["startup_ready"]
    assert (cli.serve_gate(absent, absent.masters.expected_hash, ready)
            == cli.serve_gate(bundle, bundle.masters.expected_hash, baseline))

from copy import copy, deepcopy
import json

import pytest

from aigis_ps.bootstrap import BootstrapService
from aigis_ps.cli import load_login_rules, service_readiness
from aigis_ps.login_reward_service import AFFECTION_SENDER
from aigis_ps.login_reward_state import LEDGER_KEY
from test_bootstrap import asset_root
from test_quest_service import bundle


def test_login_seed_provider_must_be_callable(asset_root):
    with pytest.raises(ValueError, match="seed provider must be callable"):
        BootstrapService(asset_root, login_reward_seed_provider=False)


def test_existing_login_ledgers_are_initialized_without_reading_clock(bundle):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    service = copy(bundle.login_rewards)
    def unavailable():
        raise AssertionError("Existing ledgers do not require a clock")
    service.clock = unavailable
    assert service.initialize(state) == state
    assert state["login_rewards"]["beginner_eligible"] is True
    assert not state["tables"]["presents"]
    assert state["crystal_spending"]["baseline_source"] == "new_local"


def test_login_rules_validate_explicit_legacy_period(tmp_path):
    assert load_login_rules(None).legacy_spending_period is None
    path = tmp_path / "login.json"
    path.write_text(json.dumps({"legacy_spending_period": "2026-10"}), encoding="utf-8")
    assert load_login_rules(path).legacy_spending_period == "2026-10"
    for value in ({"legacy_spending_period": True}, {"legacy_spending_period": "2026-13"},
                  {"legacy_spending_period": "2026-1"}, {"unknown": 1}):
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError):
            load_login_rules(path)


def test_login_rules_load_the_cooking_sender(tmp_path):
    path = tmp_path / "login.json"
    path.write_text(json.dumps({"cooking_sender": AFFECTION_SENDER}), encoding="utf-8")
    assert load_login_rules(path).cooking_sender == AFFECTION_SENDER
    for value in ("secretary", "", None, 1):
        path.write_text(json.dumps({"cooking_sender": value}), encoding="utf-8")
        with pytest.raises(ValueError):
            load_login_rules(path)


def test_a_save_frozen_at_another_beginner_total_still_starts(bundle):
    saved = deepcopy(bundle.initialize(bundle.bootstrap.initial_state()))
    total = bundle.login_rewards.beginner_total
    saved[LEDGER_KEY]["beginner_total"] = total + 1
    assert bundle.initialize(saved) == saved
    assert bundle.login_rewards.beginner_rules(saved)["changed"] is True


def test_startup_requires_login_reward_dependencies(bundle):
    assert service_readiness(bundle)["login_rewards"]["ready"] is True
    unavailable = copy(bundle)
    unavailable.login_rewards = None
    unavailable.login_reward_error = "test_unavailable"
    status = service_readiness(unavailable)
    assert status["login_rewards"] == {"ready": False, "error": "test_unavailable"}
    assert status["startup_ready"] is False

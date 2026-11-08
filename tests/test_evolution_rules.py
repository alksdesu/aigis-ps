from argparse import Namespace
import json

import pytest

from aigis_ps.cli import DEFAULT_ASSETS, DEFAULT_DATA, DEFAULT_RESOURCES, load_evolution_rules, service_readiness, services
from aigis_ps.errors import ProtocolError
from test_evolution_catalog import unit
from test_evolution_integration import seed
from test_quest_service import bundle


def test_default_and_explicit_experience_policy(tmp_path):
    assert load_evolution_rules(None).first_awake_experience == "overflow"
    path = tmp_path / "evolution-rules.json"
    path.write_text('{"first_awake_experience":"total"}', encoding="utf-8")
    assert load_evolution_rules(path).first_awake_experience == "total"


@pytest.mark.parametrize("settings", [[], {"unknown": 1}, {"first_awake_experience": True},
    {"first_awake_experience": "anything"}])
def test_invalid_rule_file_fails_before_service_construction(tmp_path, settings):
    path = tmp_path / "evolution-rules.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(ValueError):
        load_evolution_rules(path)


def args():
    return Namespace(asset_dir=DEFAULT_ASSETS, data_dir=DEFAULT_DATA, resource_dir=DEFAULT_RESOURCES, rules=None)


def test_old_reward_baseline_does_not_depend_on_training_availability(bundle, monkeypatch):
    def unavailable(*unused, **ignored):
        raise ProtocolError("training_unavailable", "fixture", 503)
    monkeypatch.setattr("aigis_ps.training_catalog.TrainingCatalog", unavailable)
    partial = services(args())
    assert partial.training is None and partial.evolution is None
    assert partial.sales is not None and partial.evolution_rewards is not None
    state, rows = seed(bundle, unit(bundle.evolution.catalog, 17, stage=3), [])
    initialized = partial.initialize(state)
    assert initialized["evolution_rewards"]["claimed"] == [17]
    assert "evolution_rewards" not in state
    initialized["player_state"]["units"] = initialized["player_state"]["units"][:1]
    assert partial.initialize(initialized)["evolution_rewards"]["claimed"] == [17]


def test_unavailable_baseline_prevents_unsafe_degraded_startup(bundle, monkeypatch):
    def unavailable(*unused, **ignored):
        raise ProtocolError("rewards_unavailable", "fixture", 503)
    monkeypatch.setattr("aigis_ps.evolution_rewards.EvolutionRewards", unavailable)
    partial = services(args())
    assert partial.gifts is not None and partial.evolution_rewards is None
    assert service_readiness(partial)["startup_ready"] is False
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    with pytest.raises(ProtocolError) as caught:
        partial.initialize(state)
    assert caught.value.code == "evolution_rewards_unavailable"

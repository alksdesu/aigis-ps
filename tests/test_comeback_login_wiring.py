from argparse import Namespace
from copy import copy
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from aigis_ps import cli
from aigis_ps.comeback_login_rules import load_comeback_login_rules
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from test_quest_service import bundle


PROJECT = Path(__file__).resolve().parents[1]


def test_default_observer_and_claim_paths_share_one_return_service(bundle):
    service = bundle.comeback_logins
    assert service is not None, bundle.comeback_login_error
    assert bundle.bootstrap.comeback_logins is service
    assert bundle.login_rewards.comeback_logins is service
    assert bundle.rank_rewards.comeback_logins is service
    assert service.rules.min_absence_days == 30
    assert cli.service_readiness(bundle)["comeback_logins"]["ready"] is True
    assert load_comeback_login_rules(PROJECT / "config/comeback-login-rules.example.json").min_absence_days == 30


def test_missing_return_service_preserves_existing_journal_by_rejecting_initialization(bundle):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: datetime(2026, 11, 1, 12, tzinfo=JST)
    state = bundle.initialize(bootstrap.initial_state())
    state["comeback_login"] = None
    broken = replace(bundle, comeback_logins=None, comeback_login_error="fixture_missing")
    with pytest.raises(ProtocolError) as error:
        broken.initialize(state)
    assert error.value.code == "comeback_login_service_unavailable"
    assert cli.service_readiness(broken)["startup_ready"] is False


def test_disabled_return_configuration_does_not_start_observation(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"enabled":false,"min_absence_days":30}', encoding="utf8")
    result = cli.services(Namespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA,
        resource_dir=cli.DEFAULT_RESOURCES, rules=None, comeback_login_rules=path))
    assert result.comeback_login_error is None
    bootstrap = copy(result.bootstrap)
    bootstrap.clock = lambda: datetime(2026, 11, 1, 12, tzinfo=JST)
    state = result.initialize(bootstrap.initial_state())
    assert result.comeback_logins.observe(state, bootstrap.clock()) == state
    assert "comeback_login" not in state


def test_doctor_forwards_return_rule_file_without_starting_server(bundle, tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "services", lambda args: seen.append(args) or bundle)
    path = PROJECT / "config/comeback-login-rules.example.json"
    assert cli.main(["doctor", "--comeback-login-rules", str(path), "--data-dir", str(tmp_path)]) == 0
    assert seen[0].comeback_login_rules == path
    assert '"comeback_logins"' in capsys.readouterr().out

from copy import copy
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from aigis_ps import cli
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from test_quest_service import bundle


PROJECT = Path(__file__).resolve().parents[1]


def test_sale_and_two_login_paths_share_the_same_entitlement_service(bundle):
    assert bundle.purchased_logins is not None, bundle.purchased_login_error
    assert bundle.packages.purchased_logins is bundle.purchased_logins
    assert bundle.login_rewards.purchased_logins is bundle.purchased_logins
    assert bundle.rank_rewards.purchased_logins is bundle.purchased_logins
    assert bundle.purchased_logins.rules.enabled is False
    assert cli.service_readiness(bundle)["purchased_logins"]["ready"] is True


def test_all_current_type2_rewards_have_real_mail_support(bundle):
    service = bundle.purchased_logins
    total = 0
    for offer in service.catalog.offers:
        for ordinal in range(1, offer.campaign.max_ordinal + 1):
            rewards = service._rewards(offer.campaign, ordinal)
            assert bundle.presents._prepare_reward_rows(rewards, 0)
            total += len(offer.campaign.rewards_for(ordinal))
    assert len(service.catalog.offers) == 16 and total == 168


def test_missing_purchased_service_cannot_drop_existing_qualification(bundle):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: datetime(2026, 11, 1, 12, tzinfo=JST)
    state = bundle.initialize(bootstrap.initial_state())
    state["purchased_login"] = None
    broken = replace(bundle, purchased_logins=None, purchased_login_error="fixture_missing")
    with pytest.raises(ProtocolError) as failure:
        broken.initialize(state)
    assert failure.value.code == "purchased_login_service_unavailable"
    assert cli.service_readiness(broken)["startup_ready"] is False


def test_doctor_passes_config_without_starting_server(bundle, tmp_path, monkeypatch, capsys):
    observed = []
    monkeypatch.setattr(cli, "services", lambda args: observed.append(args) or bundle)
    path = PROJECT / "config/purchased-login-rules.example.json"
    assert cli.main(["doctor", "--purchased-login-rules", str(path), "--data-dir", str(tmp_path)]) == 0
    assert observed[0].purchased_login_rules == path
    assert '"purchased_logins"' in capsys.readouterr().out

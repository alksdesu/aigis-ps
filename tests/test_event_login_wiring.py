from argparse import Namespace
from copy import copy
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from aigis_ps import cli
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_rules import load_event_login_rules
from test_quest_service import bundle


PROJECT = Path(__file__).resolve().parents[1]


def arguments(path):
    return Namespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA,
                     resource_dir=cli.DEFAULT_RESOURCES, rules=None, event_login_rules=path)


def test_default_login_and_rank_share_one_event_service(bundle):
    assert bundle.event_login_error is None
    assert bundle.login_rewards.event_logins is bundle.event_logins
    assert bundle.rank_rewards.event_logins is bundle.event_logins
    assert bundle.event_logins.rules.enabled is True
    assert cli.service_readiness(bundle)["event_logins"]["ready"] is True
    assert [campaign.campaign_id for campaign in bundle.event_logins.catalog.active(date(2026, 11, 1))] == [135]


def test_revival_uses_same_effective_resource_as_native_download(bundle):
    path = PROJECT / "config/event-login-revival.example.json"
    revived = cli.services(arguments(path))
    assert revived.event_login_error is None
    assert revived.bootstrap.asset_version != bundle.bootstrap.asset_version
    campaign = revived.event_logins.catalog.get(55)
    assert (campaign.start_date, campaign.end_date) == (date(2026, 11, 1), date(2026, 12, 1))
    from aigis_ps.event_login_catalog import EventLoginCatalog, RESOURCE_NAME
    wire = revived.native_cdn.assets.read(RESOURCE_NAME)[0]
    assert EventLoginCatalog(lambda _: wire).get(55) == campaign
    assert load_event_login_rules(path).revivals[55]["start_date"] == "2026-11-01"
    assert revived.login_rewards.event_logins is revived.rank_rewards.event_logins


def test_disabled_configuration_preserves_resource_version_and_has_no_grants(bundle, tmp_path):
    path = tmp_path / "disabled.json"
    path.write_text('{"enabled":false}', encoding="utf8")
    disabled = cli.services(arguments(path))
    assert disabled.event_login_error is None
    assert disabled.bootstrap.asset_version == bundle.bootstrap.asset_version
    bootstrap = copy(disabled.bootstrap)
    bootstrap.clock = lambda: datetime(2026, 11, 1, 12, tzinfo=JST)
    state = disabled.initialize(bootstrap.initial_state())
    updated, grants = disabled.event_logins.settle(state, bootstrap.clock())
    assert updated == state and grants == [] and "event_login" not in updated


def test_missing_event_service_cannot_silently_ignore_saved_history(bundle):
    broken = replace(bundle, event_logins=None, event_login_error="fixture_unavailable")
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: datetime(2026, 11, 1, 12, tzinfo=JST)
    state = bundle.initialize(bootstrap.initial_state())
    state["event_login"] = None
    with pytest.raises(ProtocolError) as error:
        broken.initialize(state)
    assert error.value.code == "event_login_service_unavailable"
    assert cli.service_readiness(broken)["startup_ready"] is False


def test_doctor_forwards_rule_path_without_starting_service(bundle, tmp_path, monkeypatch, capsys):
    observed = []
    monkeypatch.setattr(cli, "services", lambda args: observed.append(args) or bundle)
    rules = PROJECT / "config/event-login-rules.example.json"
    assert cli.main(["doctor", "--event-login-rules", str(rules), "--data-dir", str(tmp_path)]) == 0
    assert observed[0].event_login_rules == rules
    assert '"event_logins"' in capsys.readouterr().out

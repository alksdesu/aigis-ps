from argparse import Namespace
from dataclasses import replace
import json
from pathlib import Path

import pytest

from aigis_ps import cli
from aigis_ps.errors import ProtocolError
from test_quest_service import bundle


def test_trade_defaults_use_recommended_month_and_window():
    rules = cli.load_trade_rules(None)
    assert (rules.reset_type_1, rules.reset_type_2) == ("month", "window")
    example = Path(__file__).resolve().parents[1] / "config/trade-rules.example.json"
    assert cli.load_trade_rules(example) == rules


@pytest.mark.parametrize("values", [{"reset_type_1": "month", "reset_type_2": "window"},
    {"reset_type_1": "never", "reset_type_2": "never"},
    {"reset_type_1": "unconfigured", "reset_type_2": "unconfigured"}])
def test_explicit_trade_policies_load(tmp_path, values):
    path = tmp_path / "trade.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    rules = cli.load_trade_rules(path)
    assert rules.reset_type_1 == values["reset_type_1"] and rules.reset_type_2 == values["reset_type_2"]


@pytest.mark.parametrize("values,expected", [({}, ("month", "window")),
    ({"reset_type_1": "unconfigured"}, ("unconfigured", "window")),
    ({"reset_type_2": "never"}, ("month", "never"))])
def test_partial_trade_policy_uses_recommended_defaults(tmp_path, values, expected):
    path = tmp_path / "trade.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    rules = cli.load_trade_rules(path)
    assert (rules.reset_type_1, rules.reset_type_2) == expected


@pytest.mark.parametrize("values", [[], {"unknown": 1}, {"reset_type_1": True},
    {"reset_type_1": "day"}, {"reset_type_2": "month"}, {"reset_type_2": None}])
def test_invalid_trade_policies_are_rejected(tmp_path, values):
    path = tmp_path / "trade.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(ValueError):
        cli.load_trade_rules(path)


def test_builder_and_cli_wire_transactions_and_history_provider(bundle, tmp_path, monkeypatch):
    path = tmp_path / "trade.json"
    path.write_text('{"reset_type_1":"month","reset_type_2":"window"}', encoding="utf-8")
    updated = cli.services(Namespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA,
        resource_dir=cli.DEFAULT_RESOURCES, rules=None, trade_rules=path))
    assert updated.trades is not None, updated.trade_error
    assert updated.trades.rules.reset_type_1 == "month"
    assert updated.bootstrap.exchange_history_provider == updated.trades.history_response
    captured = {}
    monkeypatch.setattr(cli, "services", lambda _: updated)
    monkeypatch.setattr(cli, "client_fingerprint", lambda _: updated.masters.expected_hash)
    monkeypatch.setattr("aigis_ps.app.create_app", lambda *args, **kwargs: captured.update(kwargs))
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert cli.main(["serve", "--data-dir", str(tmp_path / "unused"), "--trade-rules", str(path)]) == 0
    for operation, handler in updated.trades.handlers().items():
        assert captured["handlers"][operation] == handler
    assert captured["status_provider"]()["trades"]["unconfigured_resets"] == []
    assert not (tmp_path / "unused").exists()


def test_missing_trade_dependency_cannot_discard_old_history(bundle):
    unavailable = replace(bundle, trades=None, trade_error="fixture_unavailable")
    state = bundle.bootstrap.initial_state()
    state["tables"]["exchange_history"] = [{"ShopItemID": 1, "ExchangeCount": 2}]
    with pytest.raises(ProtocolError) as caught:
        unavailable.initialize(state)
    assert caught.value.code == "trade_history_unavailable"
    assert state["tables"]["exchange_history"] == [{"ShopItemID": 1, "ExchangeCount": 2}]

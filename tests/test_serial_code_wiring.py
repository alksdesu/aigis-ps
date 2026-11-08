from dataclasses import replace
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.serial_code_service import INVALID_CODE, NAMESPACES, OPERATION, REDEMPTION_NAMESPACE
from test_cli import SERIAL_CODE_FLAG, assembled, serve_arguments
from test_evolution_integration import post
from test_quest_service import bundle
from test_serial_code_service import crystal, redemption


PROJECT = Path(__file__).resolve().parents[1]
EXAMPLE = PROJECT / "config/serial-code-rules.example.json"
UNKNOWN_FIELD_TABLE = {"schema_version": 1, "codes": [], "unknown": True}


@pytest.fixture(scope="module")
def served(bundle, tmp_path_factory):
    return serve_arguments(bundle, tmp_path_factory.mktemp("serve") / "unused")


def test_default_bundle_serves_32_as_invalid_codes(served, bundle, tmp_path):
    assert bundle.serial_codes is not None, bundle.serial_code_error
    assert served["handlers"][OPERATION] == bundle.serial_codes.redeem
    app = create_app(tmp_path, **served)
    store = app.state.store
    before = store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, groups = post(client, redemption(store, "abcde"))
    assert groups["SERIALCODE"].children[1].values == (INVALID_CODE,) and set(groups) == {"SYSTEM", "SERIALCODE"}
    assert store.profile() == before


def test_readiness_reports_the_service_outside_startup(bundle):
    ready = cli.service_readiness(bundle)
    assert ready["serial_codes"] == bundle.serial_codes.status()
    assert ready["serial_codes"]["ready"] is True and ready["serial_codes"]["code_count"] == 0
    broken = replace(bundle, serial_codes=None, serial_code_error="fixture_missing")
    assert cli.service_readiness(broken)["serial_codes"] == {"ready": False, "error": "fixture_missing"}
    assert cli.service_readiness(broken)["startup_ready"] == ready["startup_ready"]


def test_saved_redemptions_require_the_service(bundle):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    broken = replace(bundle, serial_codes=None, serial_code_error="fixture_missing")
    assert broken.initialize(state) == state
    for name in NAMESPACES:
        with pytest.raises(ProtocolError) as error:
            broken.initialize({**state, name: {}})
        assert (error.value.code, error.value.status) == ("serial_code_service_unavailable", 503)
    with pytest.raises(ProtocolError) as invalid:
        bundle.initialize({**state, REDEMPTION_NAMESPACE: {}})
    assert (invalid.value.code, invalid.value.status) == ("serial_code_redemptions_invalid", 503)


def test_server_routes_32_only_with_the_serial_code_service(bundle, tmp_path):
    broken = replace(bundle, serial_codes=None, serial_code_error="fixture_missing")
    assert OPERATION not in serve_arguments(broken, tmp_path / "unused")["handlers"]


def test_doctor_forwards_the_serial_code_table(bundle, tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "services", lambda args: seen.append(args) or bundle)
    assert cli.main(["doctor", SERIAL_CODE_FLAG, str(EXAMPLE), "--data-dir", str(tmp_path)]) == 0
    assert seen[0].serial_code_rules == EXAMPLE
    assert json.loads(capsys.readouterr().out)["readiness"]["serial_codes"]["ready"] is True


def test_invalid_serial_code_table_stops_doctor_without_creating_a_save(tmp_path, capsys):
    data, path = tmp_path / "server", tmp_path / "codes.json"
    path.write_text(json.dumps(UNKNOWN_FIELD_TABLE), encoding="utf-8")
    assert cli.main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "missing"),
                     SERIAL_CODE_FLAG, str(path)]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "ValueError",
                                                   "message": "Serial code configuration contains unknown fields"}
    assert not data.exists()


def test_services_load_a_configured_table(tmp_path):
    path = tmp_path / "codes.json"
    path.write_text(json.dumps({"schema_version": 1, "codes": [{"code": "AIGIS-LOCAL", "rewards": [crystal()]}]}),
                    encoding="utf-8")
    result = cli.services(assembled(serial_code_rules=path))
    assert result.serial_code_error is None
    assert [entry.code for entry in result.serial_codes.rules.codes] == ["aigislocal"]
    assert result.serial_codes.status()["unavailable_codes"] == {}

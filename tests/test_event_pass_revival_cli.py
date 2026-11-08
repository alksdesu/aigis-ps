from argparse import Namespace
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from aigis_ps import cli
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_revival import load_revival_rules
from aigis_ps.mission_state import _clock
from test_quest_service import bundle, unclosed_services


EXAMPLE = Path(__file__).resolve().parents[1] / "config/event-pass-revival.example.json"
POINTS = {1611: 2, 1607: 4, 1608: 4, 1609: 4, 1610: 6}
NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


def arguments(path=None):
    return Namespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA,
                     resource_dir=cli.DEFAULT_RESOURCES, rules=None, event_pass_revival=path)


def revived_services(build=cli.services):
    result = build(arguments(EXAMPLE))
    assert result.missions is not None, result.mission_error
    return result


@pytest.fixture(scope="module")
def revived():
    return revived_services()


@pytest.fixture(scope="module")
def golden_revived():
    return revived_services(unclosed_services)


def test_example_is_explicit_and_inherits_stable_source_dates():
    data = json.loads(EXAMPLE.read_text("utf-8"))
    assert set(data) == {"pass_id", "mission_points", "display_text"}
    rules = load_revival_rules(EXAMPLE)
    assert rules.pass_id == "event_pass_20261015"
    assert rules.mission_points == POINTS
    assert rules.start_at is None and rules.end_at is None
    assert rules.display_text == "ローカル復刻"
    assert load_revival_rules(None) is None


# The loader passes raw bytes to the strict reader, so rule files saved as UTF-16 or UTF-32 keep loading.
@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_rule_files_in_other_unicode_encodings_still_load(tmp_path, encoding):
    path = tmp_path / "revival.json"
    path.write_bytes(EXAMPLE.read_text("utf-8").encode(encoding))
    assert load_revival_rules(path) == load_revival_rules(EXAMPLE)


def test_default_builder_keeps_no_revival_metadata_but_retains_migration_service(bundle):
    assert bundle.event_pass_revival is None
    assert bundle.missions.ep_bindings is not None
    assert bundle.missions.ep_bindings.changed_ids == ()
    assert bundle.missions.pass_points.event_passes is bundle.event_passes
    assert cli.service_readiness(bundle)["event_pass_revival"] == {"enabled": False}


def test_effective_resources_and_server_catalog_share_exact_ep_bindings(bundle, revived):
    assert revived.event_pass_error is None and revived.mission_error is None
    assert revived.missions.ep_bindings.changed_ids == tuple(sorted(POINTS))
    assert revived.missions.pass_points.event_passes is revived.event_passes
    assert revived.bootstrap.asset_version != bundle.bootstrap.asset_version
    metadata = revived.event_pass_revival
    assert metadata["rules"]["pass_id"] == "event_pass_20261015"
    assert metadata["metadata"]["daily_points"] == 20
    assert cli.service_readiness(revived)["event_pass_revival"] == metadata
    before = bundle.native_cdn.assets.read("GloryConditionConfig.atb")[0]
    after = revived.native_cdn.assets.read("GloryConditionConfig.atb")[0]
    assert before != after
    parsed = parse_table(decompress_all4(after) if after.startswith(b"ALL4") else after)
    actual = {row["ConditionID"]: (row["EventPassItemID"], row["EventPassPoint"]) for row in parsed["rows"]}
    for identifier, definition in revived.missions.catalog.by_id.items():
        assert actual[identifier] == (definition.event_pass_item_id, definition.event_pass_point)
        original = bundle.missions.catalog.by_id[identifier]
        if identifier in POINTS:
            assert actual[identifier] == (metadata["rules"]["pass_id"], POINTS[identifier])
        else:
            assert definition == original
    selected = revived.event_passes.catalog.get(metadata["rules"]["pass_id"])
    original = bundle.event_passes.catalog.get(selected.item_id)
    assert selected.status == 1 and original.status == 2
    assert (selected.start_at, selected.end_at) == (original.start_at, original.end_at)
    assert selected.time_limit == "ローカル復刻"
    assert bundle.native_cdn.assets.read("GloryConditionConfig.atb")[0] == before


def test_turning_revival_on_then_off_preserves_unclaimed_progress(bundle, revived):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    original = bundle.initialize(bootstrap.initial_state())
    definition = bundle.missions.catalog.by_id[1611]
    entry = {"condition_id": 1611, "period_type": 1, "period": "day:2026-11-01",
        "definition_version": definition.definition_version, "goal": definition.clear_count_condition,
        "progress": 0, "status": 0, "read": False, "uids": []}
    original["mission_state"].update(entries=[entry], last_time=_clock(NOW)[1], last_login_day=None)
    before = deepcopy(original)
    activated = revived.initialize(original)
    assert activated["mission_state"]["entries"][0]["definition_version"] == revived.missions.catalog.by_id[1611].definition_version
    disabled = bundle.initialize(activated)
    assert disabled["mission_state"] == original["mission_state"]
    assert disabled["mission_ep_bindings"]["bindings"]["1611"]["item_id"] == ""
    assert disabled["mission_ep_bindings"]["bindings"]["1611"]["point"] == 0
    assert original == before


def test_doctor_argument_is_forwarded_without_starting_a_server(bundle, monkeypatch, capsys, tmp_path):
    observed = []
    monkeypatch.setattr(cli, "services", lambda args: observed.append(args) or bundle)
    assert cli.main(["doctor", "--event-pass-revival", str(EXAMPLE), "--data-dir", str(tmp_path / "empty")]) == 0
    assert observed[0].event_pass_revival == EXAMPLE
    assert json.loads(capsys.readouterr().out)["readiness"]["event_pass_revival"] == {"enabled": False}


@pytest.mark.parametrize("data", [[], {"unknown": True}, {"pass_id": "event_pass_20261015", "mission_points": {"1611": True}}])
def test_invalid_config_is_rejected_before_resource_view(tmp_path, monkeypatch, data):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(cli, "load_resource_view", lambda *args, **kwargs: pytest.fail("Invalid rules reached resource construction"))
    with pytest.raises(ValueError):
        cli.services(arguments(path))


def test_pass_points_failure_cannot_silently_disable_configured_revival(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ProtocolError("fixture_points_unavailable", "EP fixture is unavailable", 503)
    monkeypatch.setattr("aigis_ps.mission_pass_points.MissionPassPoints", unavailable)
    result = cli.services(arguments(EXAMPLE))
    assert result.event_pass_revival is not None
    assert result.missions is None and result.mission_error == "fixture_points_unavailable"
    assert cli.service_readiness(result)["startup_ready"] is False


def test_missing_mission_service_blocks_configured_revival_readiness(revived):
    broken = replace(revived, missions=None, mission_error="fixture")
    readiness = cli.service_readiness(broken)
    assert readiness["startup_ready"] is False
    assert readiness["missions"]["error"] == "fixture"
    assert readiness["event_pass_revival"] == revived.event_pass_revival


@pytest.mark.parametrize("key", ["mission_ep_bindings", "mission_pass_delivery"])
def test_missing_mission_service_cannot_ignore_saved_ep_domains(bundle, key):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    state = bundle.initialize(bootstrap.initial_state())
    for name in ("mission_state", "mission_history", "skin_acquisition_history", "mission_ep_bindings", "mission_pass_delivery"):
        state.pop(name, None)
    state[key] = None
    broken = replace(bundle, missions=None, mission_error="fixture")
    with pytest.raises(ProtocolError) as caught:
        broken.initialize(state)
    assert caught.value.code == "mission_service_unavailable"


def test_powershell_parameter_and_forwarding_parse_without_execution():
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell parser is unavailable")
    path = EXAMPLE.parents[1] / "start-server.ps1"
    script = ("$tokens=$null; $errors=$null; "
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile('" + str(path).replace("'", "''")
        + "',[ref]$tokens,[ref]$errors); "
        "if($errors.Count){throw 'PowerShell parse failed'}; "
        "$names=@($ast.ParamBlock.Parameters | ForEach-Object {$_.Name.VariablePath.UserPath}); "
        "$forward=$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] "
        "-and $n.Value -eq '--event-pass-revival'},$true); "
        "@{names=$names; forwardCount=@($forward).Count} | ConvertTo-Json -Compress")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", script],
                            check=True, capture_output=True, text=True, timeout=30)
    parsed = json.loads(result.stdout)
    assert "EventPassRevivalFile" in parsed["names"]
    assert parsed["forwardCount"] == 1
    assert "@('--event-pass-revival', $EventPassRevivalFile)" in path.read_text("utf-8")

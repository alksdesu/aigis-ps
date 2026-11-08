from argparse import Namespace
from copy import deepcopy
import dataclasses
from datetime import datetime
import json
import logging
from pathlib import Path
import shutil
import subprocess
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps import (cli, entrust_service, event_pass_service, event_pass_state, item_summon_tickets,
                      login_reward_service, mission_service, package_service, rank_reward_state, rank_rewards,
                      resource_view, select_tickets, sugoroku_service, tower_trade_service, view_events,
                      vote_quest_tickets, vote_service)
from aigis_ps.app import DATABASE_NAME
from aigis_ps.business_calendar import INVALID, LEGACY, MARKED, MARKER_KEY, new_marker
from aigis_ps.cli import load_entrust_rules, load_rules, main, readiness, service_readiness, services
from aigis_ps.config_registry import REGISTRY, ConfigError, TimeSemantics
from aigis_ps.energy_clock import JST, instant
from aigis_ps.entrust_service import EntrustRules
from aigis_ps.errors import ProtocolError
from aigis_ps.fairy_farm import LOAD as FAIRY_FARM_LOAD
from aigis_ps.login_cooking_state import LEDGER_KEY as COOKING_LEDGER, migrate_cooking
from aigis_ps.login_reward_state import LEDGER_KEY as LOGIN_REWARD_LEDGER, advance_login_rewards
from aigis_ps.login_stamp_pending import MAX_PENDING_STAMPS, PENDING_KEY
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.mission_tags import STATE_NAMES
from aigis_ps.mission_window_revival import (CAMPAIGN_MISMATCH, COOPERATIVE_REGISTRATIONS, KIND_CATEGORIES,
                                             WINDOW_MISMATCH, GrowthCampaignWindowMismatch, MissionWindowRules,
                                             load_mission_window_rules, mission_window_batches,
                                             verify_growth_campaign_windows)
from aigis_ps.quest_mode_reproduce import REPRODUCE_TYPE
from aigis_ps.quest_modes import OPEN
from aigis_ps.quest_record_service import OPERATIONS as QUEST_RECORDS
from aigis_ps.session_stub_service import OPERATIONS as SESSION_STUBS
from aigis_ps.silver_house import LOAD as SILVER_HOUSE_LOAD
from aigis_ps.startup_diagnostics import StartupLoad
from aigis_ps.storage import Store
from aigis_ps.subjugation_rules import RESOURCE_INVALID, SubjugationRules, load_subjugation_rules
from aigis_ps.summon_schedule import OPERATIONS as SUMMON_SCHEDULE
from aigis_ps.tower_service import FLOOR_INFO, TOWER_INFO
from aigis_ps.tower_state import DOMAIN as TOWER_STATE, TowerRules, load_tower_rules
from aigis_ps.tower_trade_state import (HISTORY_KEY as TOWER_TRADE_HISTORY, REBIND_OPERATION as TOWER_TRADE_REBIND,
                                        SCHEMA_VERSION as TOWER_TRADE_SCHEMA, TowerTradeRecord)
from test_admin_offline import clocked
from test_business_calendar import MARKER_INVALID
from test_login_reward_service import held_ledger
from test_masterdata import capture
from test_mission_window_revival import REOPENED, native


PROJECT = Path(__file__).resolve().parents[1]
ENTRUST_EXAMPLE = PROJECT / "config/entrust-rules.example.json"
ENTRUST_FLAG, ENTRUST_PARAMETER = "--entrust-rules", "EntrustRulesFile"
SERIAL_CODE_FLAG, SERIAL_CODE_PARAMETER = "--serial-code-rules", "SerialCodeRulesFile"
LETSTD_FLAG, LETSTD_PARAMETER = "--letstd-rewards", "LetsTDRewardsFile"
COMMAND_TEXT_FLAG, COMMAND_TEXT_PARAMETER = "--command-text-overrides", "CommandTextOverridesFile"
MISSION_WINDOWS_EXAMPLE = PROJECT / "config/mission-windows.example.json"
TOWER_EXAMPLE = PROJECT / "config/tower-rules.example.json"
SUBJUGATION_EXAMPLE = PROJECT / "config/subjugation-rules.example.json"
BATTLE_RULES = (("mission-windows", "MissionWindowsFile", MissionWindowRules, MISSION_WINDOWS_EXAMPLE),
                ("tower-rules", "TowerRulesFile", TowerRules, TOWER_EXAMPLE),
                ("subjugation-rules", "SubjugationRulesFile", SubjugationRules, SUBJUGATION_EXAMPLE))
SCRIPT_RULES = {ENTRUST_FLAG: ENTRUST_PARAMETER, **{"--" + key: parameter for key, parameter, *_ in BATTLE_RULES},
                COMMAND_TEXT_FLAG: COMMAND_TEXT_PARAMETER,
                SERIAL_CODE_FLAG: SERIAL_CODE_PARAMETER, LETSTD_FLAG: LETSTD_PARAMETER}
BACKGROUND_REPORT = frozenset(("rows", "abnormal_rows", "kinds", "findings", "truncated"))
BACKGROUNDS_INVALID = "background_state_invalid"
UNSUPPORTED_BACKGROUND_SCHEMA = 2
CONFIGURED_TOWER = TowerRules(first_access_settles_day=False, high_score="omit")
CONFIGURED_SUBJUGATION = SubjugationRules(kills_policy="raw", boost_policy="ignore")
INSIDE_REOPENED = datetime(2026, 11, 9, 12, tzinfo=JST)
TOWER_OPERATIONS = frozenset((66, 67, 68))
LOGIN_LEDGERS = (LOGIN_REWARD_LEDGER, login_reward_service.HISTORY_NAMESPACE, login_reward_service.DELIVERY_KEY,
                 COOKING_LEDGER)
LOGIN_REWARDS_UNAVAILABLE = "login_reward_service_unavailable"
TICKETS_UNAVAILABLE = vote_quest_tickets.DEPENDENCIES_UNAVAILABLE
TICKET_DEFINITION_INVALID = vote_quest_tickets.DEFINITION_INVALID
# The JSON scanner overflows the interpreter recursion limit near 3,000 levels, well inside a 16 KiB rule file.
SCANNER_OVERFLOW_DEPTH = 5000
NESTED_MANIFEST = "Resource manifest nesting exceeds the interpreter recursion limit"
RESOURCE_VERSION = "1" * 40
ABSENT_SERVICE_GATES = (
    (event_pass_service.HISTORY_NAMESPACE, "event_pass_service_unavailable"),
    (event_pass_state.LEDGER_KEY, "event_pass_service_unavailable"),
    (event_pass_service.DELIVERY_KEY, "event_pass_service_unavailable"),
    (event_pass_service.AUTOMATION_KEY, "event_pass_service_unavailable"),
    (package_service.HISTORY_NAMESPACE, "package_service_unavailable"),
    (view_events.HISTORY_NAMESPACE, "view_event_service_unavailable"),
    (tower_trade_service.RECEIPT_NAMESPACE, "tower_trade_unavailable"),
    (entrust_service.HISTORY_NAMESPACE, "entrust_unavailable"),
    (mission_service.HISTORY_NAMESPACE, "mission_service_unavailable"),
    (login_reward_service.HISTORY_NAMESPACE, LOGIN_REWARDS_UNAVAILABLE),
    (rank_rewards.HISTORY_NAMESPACE, "rank_reward_service_unavailable"),
    (rank_reward_state.LEDGER_KEY, "rank_reward_service_unavailable"),
    (rank_rewards.DELIVERY_KEY, "rank_reward_service_unavailable"),
    (sugoroku_service.HISTORY_NAMESPACE, "sugoroku_service_unavailable"),
    (item_summon_tickets.HISTORY_NAMESPACE, "item_summon_service_unavailable"),
    (select_tickets.HISTORY_NAMESPACE, "select_ticket_service_unavailable"),
)
SAVED_CALENDARS = (
    ({MARKER_KEY: new_marker()}, {"status": MARKED, "marker": new_marker(), "error": None}),
    ({}, {"status": LEGACY, "marker": None, "error": None}),
    ({MARKER_KEY: "damaged"}, {"status": INVALID, "marker": None, "error": MARKER_INVALID}),
)
DATABASE_BUSY, DATABASE_PATH_INVALID = "energy_migration_database_busy", "energy_migration_path_invalid"
CALENDAR_UNREADABLE = "saved_business_calendar_unreadable"
LOGIN_REWARDS_UNREADABLE, TOWER_TRADES_UNREADABLE = "saved_login_rewards_unreadable", "saved_tower_trades_unreadable"
LOGIN_REWARDS_INVALID, TOWER_HISTORY_INVALID = "login_rewards_invalid", "tower_trade_history_invalid"
REUSED_SOURCE = "f" * 64
WARNED_STAMPS_LEFT = login_reward_service.PENDING_STAMP_WARNING_DAYS - 1
MISSION_LOAD = 54
TOWER_LOADS = [FLOOR_INFO, TOWER_INFO]
# Packages, missions, votes, the reward board, login rewards, towers, the fairy farm, the silver house and quest
# records answer these, and each may fail to build.
OPTIONAL_AUTO_REQUESTS = sorted({46, MISSION_LOAD, 55, 62, 115, *TOWER_LOADS, FAIRY_FARM_LOAD, SILVER_HOUSE_LOAD,
                                 *QUEST_RECORDS})
ABSENT_AUTO_REQUEST_SERVICES = (
    ("missions", [MISSION_LOAD]),
    ("towers", TOWER_LOADS),
    ("fairy_farm", [FAIRY_FARM_LOAD]),
    ("silver_house", [SILVER_HOUSE_LOAD]),
    ("quest_records", sorted(QUEST_RECORDS)),
)
RESOURCE_FAILURE = {"code": RESOURCE_INVALID, "message": "Invalid for the fixture"}
UNREADABLE_CATALOG = {"ready": False, "error": RESOURCE_FAILURE}
LISTED_COOPERATIVE = {"mission_id": 400330, "subjugation_type": 1, "registration": COOPERATIVE_REGISTRATIONS[1],
                      "window": {"enable": 1, "start_at": "2026-11-03T12:00:00+09:00",
                                 "end_at": "2026-11-10T12:00:00+09:00", "source": "override"}, "state": "open"}
UNREADABLE_COOPERATIVE = {"mission_id": 400378, "error": RESOURCE_FAILURE}
COMMAND_FAILURES = (
    (ProtocolError("fixture_refused", "Refused for the fixture", 409), {"code": "fixture_refused", "status": 409}),
    (ConfigError("fixture_config_invalid", "Invalid for the fixture", 503),
     {"code": "fixture_config_invalid", "status": 503}),
    (ValueError("Invalid for the fixture"), {}),
    (OSError("Unreadable for the fixture"), {}),
    (RecursionError("Nested past the fixture"), {}),
)


def assembled(**rules):
    args = Namespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA, resource_dir=cli.DEFAULT_RESOURCES,
                     rules=None, **rules)
    if not (args.asset_dir / "Aigis_r.exe").is_file() or not (args.data_dir / "masterdata/active.json").is_file():
        pytest.skip("Verified local client resources and public masters are required")
    return args


def serve_arguments(bundle, data_dir):
    captured = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "services", lambda _: bundle)
        patch.setattr(cli, "client_fingerprint", lambda _: bundle.masters.expected_hash)
        patch.setattr("aigis_ps.app.create_app", lambda *args, **kwargs: captured.update(kwargs))
        patch.setattr("uvicorn.run", lambda *args, **kwargs: None)
        assert main(["serve", "--data-dir", str(data_dir)]) == 0
    return captured


def doctor_readiness(data, monkeypatch, capsys, bundle=None):
    if bundle is None:
        bundle = SimpleNamespace(masters=MasterDataStore.for_data_dir(data), backgrounds=None, login_rewards=None,
                                 tower_trades=None, native_cdn=None)
    monkeypatch.setattr(cli, "services", lambda arguments: bundle)
    # Everything else reports ready, so only the saved-state checks could withdraw startup readiness.
    monkeypatch.setattr(cli, "service_readiness", lambda source: {"startup_ready": True})
    monkeypatch.setattr(cli, "client_fingerprint", lambda directory: bundle.masters.expected_hash)
    assert main(["doctor", "--data-dir", str(data)]) == 0
    return json.loads(capsys.readouterr().out)["readiness"]


def saved_readiness(bundle, data, state, monkeypatch, capsys):
    Store(data / DATABASE_NAME, state)
    return doctor_readiness(data, monkeypatch, capsys, bundle)


def listed_cooperative(bundle, monkeypatch, *entries):
    status, clocks = bundle.quests.subjugation.status, []

    def listing(now=None):
        clocks.append(now)
        return {**status(now), "cooperative": list(entries)}

    monkeypatch.setattr(bundle.quests.subjugation, "status", listing)
    return clocks


def auto_request_warnings(caplog):
    return [record.getMessage() for record in caplog.records
            if record.levelno == logging.WARNING and record.funcName == cli.warn_auto_request_gaps.__name__]


def without_optional_services():
    names = (field.name for field in dataclasses.fields(cli.ServerServices))
    return cli.ServerServices(**{**dict.fromkeys(names), "player_state": SimpleNamespace(initialize=deepcopy)})


@pytest.fixture(scope="module")
def configured(tmp_path_factory):
    directory = tmp_path_factory.mktemp("battle-rules")
    tower, subjugation = directory / "tower-rules.json", directory / "subjugation-rules.json"
    tower.write_text(json.dumps(CONFIGURED_TOWER.to_dict()), encoding="utf-8")
    subjugation.write_text(json.dumps(CONFIGURED_SUBJUGATION.to_dict()), encoding="utf-8")
    bundle = services(assembled(mission_windows=MISSION_WINDOWS_EXAMPLE, tower_rules=tower, subjugation_rules=subjugation))
    assert bundle.quests is not None
    return bundle


def test_doctor_reports_missing_data_without_creating_a_save(tmp_path, capsys):
    data = tmp_path / "server"
    assert main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "missing")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["service"] == "aigis-ps"
    assert result["registered_operations"] == 156
    assert result["source_supported"] is False
    assert result["readiness"]["startup_ready"] is False
    assert result["readiness"]["masterdata"]["code"] == "masterdata_missing"
    assert result["readiness"]["unit_actions"]["implemented_operations"] == [25, 27, 119]
    assert result["readiness"]["filters"]["implemented_operations"] == [26]
    assert result["readiness"]["card_status"]["available"] is False
    assert result["readiness"]["presents"]["ready"] is False
    assert result["readiness"]["gifts"]["available"] is False
    assert result["readiness"]["peruse"]["implemented_operations"] == [75]
    assert result["readiness"]["peruse_episodes"]["implemented_operations"] == [136]
    assert result["readiness"]["peruse_episodes"]["ready"] is False
    assert result["readiness"]["session_stubs"]["implemented_operations"] == list(SESSION_STUBS)
    assert result["readiness"]["summon_schedule"]["implemented_operations"] == list(SUMMON_SCHEDULE)
    assert result["readiness"]["shop_window_closure"] == {"enabled": False}
    assert result["readiness"]["auto_requests"]["unhandled"] == OPTIONAL_AUTO_REQUESTS
    assert result["readiness"]["universal_flags"] == {"ready": False, "error": "universal_flags_dependencies_unavailable"}
    assert result["readiness"]["serial_codes"]["ready"] is True and result["readiness"]["serial_codes"]["code_count"] == 0
    assert "saved_backgrounds" not in result["readiness"] and "saved_business_calendar" not in result["readiness"]
    assert result["readiness"]["towers"] == {"ready": False, "error": "tower_dependencies_unavailable"}
    assert result["readiness"]["subjugation"] == {"ready": False, "error": "subjugation_dependencies_unavailable"}
    assert result["readiness"]["auto_requests"]["cooperative_subjugations"] is None
    assert result["readiness"]["mission_windows"] == {"enabled": False}
    assert not data.exists()


def test_rules_validate_known_fields_and_keep_bootstrap_token_private(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"money": 5000, "user_name": "Local"}))
    rules = load_rules(path)
    assert rules.money == 5000
    assert rules.user_name == "Local"
    for value in ({"money": True}, {"money": -1}, {"bootstrap_token": "custom"}, []):
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            load_rules(path)


def test_invalid_rules_fail_before_starting_server(tmp_path, capsys):
    data, path = tmp_path / "server", tmp_path / "rules.json"
    path.write_text('{"unknown":1}')
    assert main(["serve", "--rules", str(path), "--asset-dir", str(tmp_path), "--data-dir", str(data)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "ValueError"
    assert not data.exists()


def test_missing_bootstrap_resources_do_not_seed_an_incomplete_save(tmp_path, capsys):
    data = tmp_path / "server"
    assert main(["serve", "--asset-dir", str(tmp_path / "missing"), "--data-dir", str(data)]) == 1
    assert "bootstrap" in json.loads(capsys.readouterr().out)["message"]
    assert not data.exists()


def test_readiness_requires_data_and_implemented_bootstrap():
    class Status:
        def __init__(self, **value):
            self.value = value

        def status(self, *snapshot):
            return self.value

    assert not readiness(Status(ready=True), Status(startup_ready=False))["startup_ready"]
    assert not readiness(Status(ready=False), Status(startup_ready=True))["startup_ready"]
    assert readiness(Status(ready=True), Status(startup_ready=True))["startup_ready"]


def test_entrust_rules_are_strict_and_the_example_keeps_the_default(tmp_path):
    assert load_entrust_rules(None) == load_entrust_rules(ENTRUST_EXAMPLE) == EntrustRules()
    path = tmp_path / "entrust.json"
    path.write_text(json.dumps({"experience_up": False}))
    assert load_entrust_rules(path) == EntrustRules(experience_up=False)
    for value in ({"experience_up": 1}, {"experience_up": "false"}, {"experience_up": None}, {"unknown": True}, []):
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            load_entrust_rules(path)


def test_entrust_rules_register_their_flag_loader_and_example():
    spec = REGISTRY["entrust-rules"]
    assert (spec.cli_flag, spec.ps_parameter, spec.rules_type()) == (ENTRUST_FLAG, ENTRUST_PARAMETER, EntrustRules)
    assert spec.examples == (ENTRUST_EXAMPLE.name,)
    assert spec.default() == load_entrust_rules(None)
    assert spec.normalize(spec.load(ENTRUST_EXAMPLE)) == {"experience_up": True}


def test_invalid_entrust_rules_stop_doctor_without_creating_a_save(tmp_path, capsys):
    data, path = tmp_path / "server", tmp_path / "entrust.json"
    path.write_text('{"experience_up": "yes"}')
    assert main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "missing"),
                 ENTRUST_FLAG, str(path)]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "ValueError",
                                                   "message": "Entrust experience campaign rule must be boolean"}
    assert not data.exists()


def test_battle_rule_files_are_strict_and_the_examples_keep_their_defaults(tmp_path):
    assert load_tower_rules(None) == load_tower_rules(TOWER_EXAMPLE) == TowerRules()
    assert load_subjugation_rules(None) == load_subjugation_rules(SUBJUGATION_EXAMPLE) == SubjugationRules()
    assert load_mission_window_rules(None) is None
    path = tmp_path / "rules.json"
    for loader, value in ((load_mission_window_rules, {"enabled": True, "batches": {}, "missions": {}}),
                          (load_mission_window_rules, {"enabled": True, "missions": {"320424": REOPENED}}),
                          (load_mission_window_rules, {"enabled": True, "batches": {"Reproduce:320424x": REOPENED}}),
                          (load_tower_rules, {"generation_hour": 0, "unknown": True}), (load_tower_rules, []),
                          (load_subjugation_rules, {"kills_policy": "raw", "unknown": True})):
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            loader(path)


def test_the_mission_window_example_reopens_every_reproduce_batch(native):
    rules = load_mission_window_rules(MISSION_WINDOWS_EXAMPLE)
    reproduce = [batch.key for batch in mission_window_batches(native.__getitem__)
                 if batch.category == KIND_CATEGORIES["Reproduce"]]
    assert rules.enabled is True and list(rules.batches) == reproduce
    assert all(window.to_dict() == REOPENED for window in rules.batches.values())


@pytest.mark.parametrize("key,parameter,rules_type,example", BATTLE_RULES)
def test_battle_rules_register_their_flag_loader_and_example(key, parameter, rules_type, example):
    spec = REGISTRY[key]
    assert (spec.cli_flag, spec.ps_parameter, spec.rules_type()) == ("--" + key, parameter, rules_type)
    assert spec.examples == (example.name,)
    assert spec.normalize(spec.load(example)) == spec.load(example).to_dict()
    assert spec.resource_hint == (spec.time_semantics is TimeSemantics.RESOURCE) == (key == "mission-windows")


@pytest.mark.parametrize("flag,value,message", [
    ("--mission-windows", {"enabled": True, "missions": {}},
     "Mission window rules require exactly enabled and batches, plus an optional growth_campaigns"),
    ("--tower-rules", {"high_score": "everyone"}, "Tower high score policy must be self or omit"),
    ("--subjugation-rules", {"kills_policy": "clamp", "boost": 3},
     "Subjugation rules accept only kills_policy and boost_policy"),
])
def test_invalid_battle_rules_stop_doctor_without_creating_a_save(tmp_path, capsys, flag, value, message):
    data, path = tmp_path / "server", tmp_path / "rules.json"
    path.write_text(json.dumps(value))
    assert main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "missing"), flag, str(path)]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "ValueError", "message": message}
    assert not data.exists()


def test_start_script_declares_and_forwards_each_rule_file_once():
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell parser is unavailable")
    path = PROJECT / "start-server.ps1"
    flags = ",".join(f"'{flag}'" for flag in SCRIPT_RULES)
    script = ("$tokens=$null; $errors=$null; "
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile('" + str(path).replace("'", "''")
        + "',[ref]$tokens,[ref]$errors); "
        "if($errors.Count){throw 'PowerShell parse failed'}; "
        "$names=@($ast.ParamBlock.Parameters | ForEach-Object {$_.Name.VariablePath.UserPath}); "
        "$counts=@{}; foreach($flag in @(" + flags + ")){ $counts[$flag]=@($ast.FindAll({param($n) "
        "$n -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $n.Value -eq $flag},"
        "$true)).Count }; "
        "@{names=$names; counts=$counts} | ConvertTo-Json -Compress")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", script],
                            check=True, capture_output=True, text=True, timeout=30)
    parsed = json.loads(result.stdout)
    assert set(SCRIPT_RULES.values()) <= set(parsed["names"])
    assert parsed["counts"] == dict.fromkeys(SCRIPT_RULES, 1)
    text = path.read_text("utf-8")
    assert all(f"@('{flag}', ${parameter})" in text for flag, parameter in SCRIPT_RULES.items())


def test_configured_rules_reach_the_tower_and_subjugation_services(configured):
    assert configured.quests.tower is configured.towers and configured.towers.rules == CONFIGURED_TOWER != TowerRules()
    assert configured.towers.catalog.quests is configured.quests.catalog
    assert configured.quests.subjugation.rules == CONFIGURED_SUBJUGATION != SubjugationRules()
    assert configured.quests.subjugation.quests is configured.quests.catalog
    report = service_readiness(configured)
    assert report["towers"] == configured.towers.status() and report["towers"]["ready"] is True
    assert report["subjugation"] == configured.quests.subjugation.status(instant(configured.bootstrap.clock()))
    assert report["subjugation"]["ready"] is True
    assert (configured.tower_error, configured.subjugation_error) == (None, None)


def test_mission_window_rules_reach_the_quest_windows_and_readiness(configured, monkeypatch):
    monkeypatch.setattr(configured.bootstrap, "clock", lambda: INSIDE_REOPENED)
    rules = load_mission_window_rules(MISSION_WINDOWS_EXAMPLE)
    report = service_readiness(configured)["mission_windows"]
    assert report["enabled"] is True and report["rules"] == rules.to_dict() and "error" not in report
    assert [batch["key"] for batch in report["batches"]] == list(rules.batches)
    members = {member["mission_id"] for batch in report["batches"] for member in batch["members"]}
    assert report["verification"]["verified"] is True and report["verification"]["matches_build"] is True
    assert report["verification"]["members"] == len(members)
    assert all(batch["state"] == STATE_NAMES[OPEN] != batch["original_state"] for batch in report["batches"])
    windows = configured.quests.catalog.mission_windows(REPRODUCE_TYPE)
    assert members <= {mission.mission_id for mission in windows.ordered()
                       if mission.listed and mission.state(INSIDE_REOPENED) == OPEN}
    assert configured.mission_window_error is None
    assert service_readiness(dataclasses.replace(configured, mission_windows=None, mission_window_revival=None))[
        "mission_windows"] == {"enabled": False}


def test_mismatched_mission_windows_leave_the_quest_services_unassembled(monkeypatch):
    built = resource_view.build_mission_window_revival
    monkeypatch.setattr(resource_view, "build_mission_window_revival", lambda read_resource, rules: dataclasses.replace(
        built(read_resource, rules), replacements=MappingProxyType({})))
    bundle = services(assembled(mission_windows=MISSION_WINDOWS_EXAMPLE))
    assert bundle.quests is None and bundle.quest_error == bundle.mission_window_error == WINDOW_MISMATCH
    report = service_readiness(bundle)
    assert report["main_quests"] == {"available": False, "error": WINDOW_MISMATCH}
    windows = report["mission_windows"]
    assert windows["error"] == WINDOW_MISMATCH and windows["verification"]["verified"] is False
    rules = load_mission_window_rules(MISSION_WINDOWS_EXAMPLE)
    assert {mismatch["key"] for mismatch in windows["verification"]["mismatches"]} == set(rules.batches)
    assert (bundle.towers, bundle.entrust) == (None, None)
    assert report["towers"] == {"ready": False, "error": "tower_dependencies_unavailable"}
    assert report["subjugation"] == {"ready": False, "error": "subjugation_dependencies_unavailable"}
    assert bundle.vote_quest_ticket_error == TICKETS_UNAVAILABLE


def test_the_example_growth_windows_verify_against_the_served_table(configured):
    rules = load_mission_window_rules(MISSION_WINDOWS_EXAMPLE)
    reader = lambda name: configured.native_cdn.assets.read(name)[0]
    assert verify_growth_campaign_windows(rules, reader)["verification"]["verified"] is True
    report = service_readiness(configured)
    assert report["entrust"] == configured.entrust.status() and report["entrust"]["ready"] is True


def test_mismatched_growth_campaign_windows_withhold_the_entrust_catalog(tmp_path, monkeypatch):
    path = tmp_path / "mission-windows.json"
    path.write_text(json.dumps({"enabled": True, "batches": {}, "growth_campaigns": {"700101": REOPENED}}),
                    encoding="utf-8")
    built = resource_view.build_mission_window_revival
    monkeypatch.setattr(resource_view, "build_mission_window_revival", lambda read_resource, rules: dataclasses.replace(
        built(read_resource, rules), replacements=MappingProxyType({})))
    bundle = services(assembled(mission_windows=path))
    assert bundle.quests is not None and bundle.mission_window_error is None
    assert (bundle.entrust, bundle.entrust_error) == (None, CAMPAIGN_MISMATCH)
    assert service_readiness(bundle)["entrust"] == {"ready": False, "error": CAMPAIGN_MISMATCH}
    with pytest.raises(GrowthCampaignWindowMismatch):
        verify_growth_campaign_windows(load_mission_window_rules(path),
                                       lambda name: bundle.native_cdn.assets.read(name)[0])


def test_quest_tickets_attach_to_the_shared_event_rewards(configured):
    tickets = configured.quests.event_rewards.status()["vote_tickets"]
    assert tickets["attached"] is True and tickets["error"] is None and configured.vote_quest_ticket_error is None
    assert configured.entrust is not None and configured.entrust.quests is configured.quests


def test_quest_tickets_report_missing_vote_transactions(monkeypatch):
    class WithoutTransactions(vote_service.VoteService):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.transactions = None

    monkeypatch.setattr(vote_service, "VoteService", WithoutTransactions)
    bundle = services(assembled())
    assert bundle.quests is not None and bundle.votes is not None and bundle.votes.transactions is None
    assert bundle.vote_quest_ticket_error == TICKETS_UNAVAILABLE
    assert bundle.quests.event_rewards.status()["vote_tickets"] == {
        "attached": False, "error": TICKETS_UNAVAILABLE, "status": None}


def test_a_refused_ticket_provider_reports_its_code_beside_the_quest_services(monkeypatch):
    # The vote notices state a cap of 999, so lowering the contract cap refuses the provider.
    monkeypatch.setattr(vote_quest_tickets, "TICKET_CAP", vote_quest_tickets.TICKET_CAP - 1)
    bundle = services(assembled())
    assert bundle.votes is not None and bundle.votes.transactions is not None
    assert bundle.vote_quest_ticket_error == TICKET_DEFINITION_INVALID
    tickets = service_readiness(bundle)["main_quests"]["event_rewards"]["vote_tickets"]
    assert tickets == {"attached": False, "error": TICKET_DEFINITION_INVALID, "status": None}
    assert bundle.entrust is not None and bundle.towers is not None


def test_tower_tiers_are_judged_by_the_attached_ticket_pools(configured):
    tickets = configured.presents.item_tickets
    tiers = [tier for tower in configured.towers.catalog.towers for tier in tower.star_rewards
             if tier.item_id is not None and tickets.catalog.supports_item(tier.item_id)]
    assert tiers and configured.towers.status()["item_tickets_attached"] is True
    for tier in tiers:
        try:
            tickets.validate_item(tier.item_id)
            expected = None
        except ProtocolError as error:
            expected = error.code
        assert configured.towers.deliverable(tier.present()) == expected != "present_item_unsupported"


def test_saved_tower_progress_requires_the_tower_service(configured):
    initial = configured.bootstrap.initial_state()
    assert TOWER_STATE not in configured.initialize(deepcopy(initial))
    with pytest.raises(ProtocolError) as unavailable:
        dataclasses.replace(configured, towers=None).initialize({**deepcopy(initial), TOWER_STATE: {}})
    assert (unavailable.value.code, unavailable.value.status) == ("tower_service_unavailable", 503)
    with pytest.raises(ProtocolError) as invalid:
        configured.initialize({**deepcopy(initial), TOWER_STATE: {}})
    assert (invalid.value.code, invalid.value.status) == ("tower_state_invalid", 503)


@pytest.mark.parametrize("ledger", LOGIN_LEDGERS)
def test_saved_login_ledgers_require_the_login_reward_service(configured, ledger):
    initial = {key: value for key, value in configured.bootstrap.initial_state().items() if key not in LOGIN_LEDGERS}
    bundle = dataclasses.replace(configured, login_rewards=None)
    assert set(LOGIN_LEDGERS).isdisjoint(bundle.initialize(deepcopy(initial)))
    with pytest.raises(ProtocolError) as unavailable:
        bundle.initialize({**initial, ledger: {}})
    assert (unavailable.value.code, unavailable.value.status) == (LOGIN_REWARDS_UNAVAILABLE, 503)


@pytest.mark.parametrize("namespace,code", ABSENT_SERVICE_GATES)
def test_saved_history_without_its_service_stops_at_its_own_gate(namespace, code):
    absent, state = without_optional_services(), clocked("gates")
    state["tables"].update(universal_flags=[], backgrounds=[])
    assert absent.initialize(deepcopy(state)) == state
    with pytest.raises(ProtocolError) as unavailable:
        absent.initialize({**state, namespace: {}})
    assert (unavailable.value.code, unavailable.value.status) == (code, 503)


@pytest.mark.parametrize("towers", [True, False])
def test_server_routes_the_tower_operations_only_with_the_tower_service(configured, tmp_path, towers):
    bundle = configured if towers else dataclasses.replace(configured, towers=None)
    directory = tmp_path / "not-created"
    handlers = serve_arguments(bundle, directory)["handlers"]
    routed = {number: handler.__self__ for number, handler in handlers.items() if number in TOWER_OPERATIONS}
    assert routed == (dict.fromkeys(TOWER_OPERATIONS, bundle.towers) if towers else {})
    assert not directory.exists()


def test_server_records_every_native_startup_load_on_the_bootstrap_registry(configured, tmp_path):
    handlers = serve_arguments(configured, tmp_path / "not-created")["handlers"]
    for operation in cli.NATIVE_STARTUP_LOADS:
        load = handlers[operation]
        assert isinstance(load, StartupLoad), operation
        assert (load.diagnostics, load.operation) == (configured.bootstrap.startup_diagnostics, str(operation))
    assert handlers[39].handler.__self__ is handlers[41].handler.__self__ is configured.player_state


def test_readiness_reports_the_summon_schedule_shop_closure_and_auto_requests(configured):
    report = service_readiness(configured)
    assert report["summon_schedule"] == configured.summon_schedule.status()
    assert report["shop_window_closure"] == configured.shop_window_closure
    assert configured.shop_window_closure["products"]
    assert report["auto_requests"] == {"ready": True, "operations": list(cli.AUTO_SENT_OPERATIONS), "unhandled": [],
                                       "cooperative_subjugations": [], "client": cli.AUTO_REQUEST_AFTERMATH}
    assert set(cli.AUTO_SENT_OPERATIONS) == {*cli.NATIVE_STARTUP_LOADS, *SESSION_STUBS, *SUMMON_SCHEDULE,
                                              *OPTIONAL_AUTO_REQUESTS}
    unclosed = service_readiness(dataclasses.replace(configured, shop_window_closure=None))
    assert unclosed["shop_window_closure"] == {"enabled": False}


@pytest.mark.parametrize("service,operations", ABSENT_AUTO_REQUEST_SERVICES)
def test_an_absent_optional_service_shows_its_auto_request_without_gating_startup(configured, service, operations):
    report = service_readiness(dataclasses.replace(configured, **{service: None}))
    assert report["auto_requests"]["unhandled"] == operations and report["auto_requests"]["ready"] is False
    assert report["startup_ready"] is service_readiness(configured)["startup_ready"] is True


def test_a_listed_cooperative_subjugation_joins_the_auto_request_gaps(configured, monkeypatch):
    clocks = listed_cooperative(configured, monkeypatch, LISTED_COOPERATIVE, UNREADABLE_COOPERATIVE)
    gaps, registration = service_readiness(configured)["auto_requests"], LISTED_COOPERATIVE["registration"]
    assert gaps["operations"] == sorted({*cli.AUTO_SENT_OPERATIONS, registration})
    assert gaps["unhandled"] == [registration] and gaps["ready"] is False
    assert gaps["cooperative_subjugations"] == [LISTED_COOPERATIVE, UNREADABLE_COOPERATIVE]
    assert clocks and None not in clocks
    listed_cooperative(configured, monkeypatch, UNREADABLE_COOPERATIVE)
    unreadable = service_readiness(configured)["auto_requests"]
    assert (unreadable["operations"], unreadable["unhandled"]) == (list(cli.AUTO_SENT_OPERATIONS), [])
    assert unreadable["ready"] is False


def test_serve_warns_about_auto_request_gaps_only_when_there_are_some(configured, tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=cli.log.name):
        serve_arguments(configured, tmp_path / "complete")
        assert auto_request_warnings(caplog) == []
        listed_cooperative(configured, monkeypatch, LISTED_COOPERATIVE, UNREADABLE_COOPERATIVE)
        serve_arguments(dataclasses.replace(configured, missions=None), tmp_path / "gaps")
    unhandled, listed, unreadable = auto_request_warnings(caplog)
    assert f"Operations {MISSION_LOAD}, {LISTED_COOPERATIVE['registration']} have no handler" in unhandled
    assert all(cli.AUTO_REQUEST_AFTERMATH in message for message in (unhandled, listed))
    assert f"Cooperative subjugation {LISTED_COOPERATIVE['mission_id']} " in listed
    assert str(UNREADABLE_COOPERATIVE["mission_id"]) in unreadable
    assert UNREADABLE_COOPERATIVE["error"]["code"] in unreadable


def test_an_unreadable_subjugation_catalog_leaves_the_mission_menu_requests_unknown(
        configured, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(configured.quests.subjugation, "status", lambda now=None: deepcopy(UNREADABLE_CATALOG))
    gaps = service_readiness(configured)["auto_requests"]
    assert (gaps["ready"], gaps["cooperative_subjugations"], gaps["unhandled"]) == (False, None, [])
    assert gaps["operations"] == list(cli.AUTO_SENT_OPERATIONS)
    with caplog.at_level(logging.WARNING, logger=cli.log.name):
        serve_arguments(configured, tmp_path / "unknown")
    [unknown] = auto_request_warnings(caplog)
    assert "subjugation catalog is unreadable" in unknown
    assert all(str(registration) in unknown for registration in COOPERATIVE_REGISTRATIONS.values())


def test_quest_tickets_attach_before_the_entrust_service_reads_the_quests(monkeypatch):
    seen = []

    class Recording(entrust_service.EntrustService):
        def __init__(self, quests, *args, **kwargs):
            seen.append((quests.event_rewards.content_version, quests.event_rewards.status()["vote_tickets"]["attached"]))
            super().__init__(quests, *args, **kwargs)

    monkeypatch.setattr(entrust_service, "EntrustService", Recording)
    bundle = services(assembled())
    assert isinstance(bundle.entrust, Recording)
    assert seen == [(bundle.quests.event_rewards.content_version, True)]


def test_doctor_reports_the_saved_background_rows(configured, tmp_path, monkeypatch, capsys):
    Store(tmp_path / "aigis.sqlite3", configured.initialize(configured.bootstrap.initial_state()))
    monkeypatch.setattr(cli, "services", lambda _: configured)
    assert main(["doctor", "--data-dir", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)["readiness"]
    assert report["saved_energy_clock"]["status"] == "ready"
    assert set(report["saved_backgrounds"]) == BACKGROUND_REPORT
    assert report["saved_backgrounds"]["abnormal_rows"] == 0 and report["saved_backgrounds"]["truncated"] is False
    assert report["saved_business_calendar"]["status"] == MARKED


def test_doctor_reports_unreadable_background_rows_by_their_code(configured, tmp_path, monkeypatch, capsys):
    state = configured.initialize(configured.bootstrap.initial_state())
    Store(tmp_path / "aigis.sqlite3", {**state, "backgrounds_schema": UNSUPPORTED_BACKGROUND_SCHEMA})
    monkeypatch.setattr(cli, "services", lambda _: configured)
    assert main(["doctor", "--data-dir", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)["readiness"]
    assert report["saved_energy_clock"]["status"] == "ready"
    assert report["saved_backgrounds"] == {"error": BACKGROUNDS_INVALID}


def test_doctor_skips_the_background_rows_without_a_save(configured, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "services", lambda _: configured)
    assert main(["doctor", "--data-dir", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)["readiness"]
    assert report["saved_energy_clock"]["status"] == "new_database" and "saved_backgrounds" not in report
    assert {"saved_business_calendar", "saved_login_rewards", "saved_tower_trades"}.isdisjoint(report)


@pytest.mark.parametrize("calendar,expected", SAVED_CALENDARS, ids=(MARKED, LEGACY, INVALID))
def test_doctor_reports_the_saved_business_calendar_without_gating_startup(tmp_path, monkeypatch, capsys, calendar,
                                                                           expected):
    data = tmp_path / "server"
    Store(data / DATABASE_NAME, {**clocked("calendar"), **calendar})
    report = doctor_readiness(data, monkeypatch, capsys)
    assert report["saved_energy_clock"]["status"] == "ready" and report["startup_ready"] is True
    assert report["saved_business_calendar"] == expected


def test_doctor_reads_the_saved_business_calendar_only_beside_a_ready_energy_clock(tmp_path, monkeypatch, capsys):
    data = tmp_path / "server"
    report = doctor_readiness(data, monkeypatch, capsys)
    assert report["saved_energy_clock"]["status"] == "new_database" and "saved_business_calendar" not in report
    unconverted = {key: value for key, value in clocked("unconverted").items() if key != "energy_clock"}
    Store(data / DATABASE_NAME, {**unconverted, MARKER_KEY: new_marker()})
    report = doctor_readiness(data, monkeypatch, capsys)
    assert report["saved_energy_clock"]["status"] == "migration_required" and report["startup_ready"] is False
    assert "saved_business_calendar" not in report


def test_unreadable_saves_report_their_business_calendar_by_the_reading_error(tmp_path):
    store = Store(tmp_path / "busy" / DATABASE_NAME, {**clocked("busy"), MARKER_KEY: new_marker()})
    Path(f"{store.path}-wal").write_bytes(b"")
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"not a database")
    assert cli.diagnose_saved_business_calendar(store.path) == {"error": DATABASE_BUSY}
    assert cli.diagnose_saved_business_calendar(tmp_path / "absent.sqlite3") == {"error": DATABASE_PATH_INVALID}
    assert cli.diagnose_saved_business_calendar(garbage) == {"error": CALENDAR_UNREADABLE}


def test_doctor_reports_a_save_frozen_at_another_beginner_total_without_gating_startup(configured, tmp_path,
                                                                                      monkeypatch, capsys):
    rewards = configured.login_rewards
    assert rewards is not None, configured.login_reward_error
    total, saved = rewards.beginner_total, deepcopy(configured.initialize(configured.bootstrap.initial_state()))
    current = saved_readiness(configured, tmp_path / "current", saved, monkeypatch, capsys)
    assert current["saved_login_rewards"] == {"saved_total": total, "current_total": total, "changed": False,
                                              "cooking": rewards.cooking_rules(saved),
                                              "pending_stamps": rewards.pending_stamps(saved)}
    saved[LOGIN_REWARD_LEDGER]["beginner_total"] = total + 1
    changed = saved_readiness(configured, tmp_path / "changed", saved, monkeypatch, capsys)
    report = changed["saved_login_rewards"]
    assert report == rewards.saved_report(saved) and report["changed"] is True
    assert (report["saved_total"], report["current_total"]) == (total + 1, total)
    assert report["refused_operations"] == sorted(rewards.handlers())
    assert report["repair"] == login_reward_service.REBASE_OPERATION
    assert changed["startup_ready"] is current["startup_ready"] is True


def test_doctor_reports_the_saved_cooking_border_and_pending_stamps_without_gating_startup(configured, tmp_path,
                                                                                         monkeypatch, capsys):
    rewards = configured.login_rewards
    assert rewards is not None, configured.login_reward_error
    saved = configured.initialize(configured.bootstrap.initial_state())
    now, border = instant(configured.bootstrap.clock()), rewards.cooking_border
    foreign = migrate_cooking(saved, now, border=border + 1)
    cooking = saved_readiness(configured, tmp_path / "cooking", foreign, monkeypatch, capsys)
    report = cooking["saved_login_rewards"]
    assert report == rewards.saved_report(foreign) and report["changed"] is False
    assert (report["cooking"]["saved_border"], report["cooking"]["current_border"]) == (border + 1, border)
    assert report["cooking"]["changed"] is True
    assert report["cooking"]["refused_operations"] == sorted(rewards.handlers())
    assert report["cooking"]["repair"] == login_reward_service.COOKING_MIGRATION
    assert report["cooking"]["offline_repair"] == list(login_reward_service.OFFLINE_COOKING_MIGRATION)
    claimed, _ = advance_login_rewards(saved, now, beginner_count=rewards.beginner_total)
    held = {**claimed, PENDING_KEY: held_ledger(claimed, MAX_PENDING_STAMPS - WARNED_STAMPS_LEFT)}
    stamps = saved_readiness(configured, tmp_path / "stamps", held, monkeypatch, capsys)
    assert stamps["saved_login_rewards"] == rewards.saved_report(held)
    assert stamps["saved_login_rewards"]["pending_stamps"] == {
        "count": MAX_PENDING_STAMPS - WARNED_STAMPS_LEFT, "capacity": MAX_PENDING_STAMPS,
        "remaining": WARNED_STAMPS_LEFT, "warning": True}
    assert cooking["startup_ready"] is stamps["startup_ready"] is True


def test_doctor_lists_reused_tower_products_without_gating_startup(configured, tmp_path, monkeypatch, capsys):
    tower_trades = configured.tower_trades
    assert tower_trades is not None, configured.tower_trade_error
    kept, reused = sorted(tower_trades.catalog.by_id)[:2]
    saved = deepcopy(configured.initialize(configured.bootstrap.initial_state()))
    current = saved_readiness(configured, tmp_path / "current", saved, monkeypatch, capsys)
    assert current["saved_tower_trades"] == tower_trades.reuse_report(saved)
    assert current["saved_tower_trades"]["reused_products"] == []
    sources = {kept: tower_trades.history.source(tower_trades.catalog.product(kept)), reused: REUSED_SOURCE}
    # A resource that gives the ID to another reward leaves the saved source of its old reward behind.
    saved[TOWER_TRADE_HISTORY] = {"schema_version": TOWER_TRADE_SCHEMA, "records": {
        str(shop): TowerTradeRecord(source, None, 0, 1).saved() for shop, source in sources.items()}}
    changed = saved_readiness(configured, tmp_path / "reused", saved, monkeypatch, capsys)
    report = changed["saved_tower_trades"]
    assert report == tower_trades.reuse_report(saved) and report["reused_products"] == [reused]
    assert report["refused_operations"] == sorted(tower_trades.handlers())
    assert report["repair"] == TOWER_TRADE_REBIND
    assert changed["startup_ready"] is current["startup_ready"] is True


def test_doctor_reports_neither_repair_without_its_service(configured, tmp_path, monkeypatch, capsys):
    absent = dataclasses.replace(configured, login_rewards=None, tower_trades=None)
    report = saved_readiness(absent, tmp_path, configured.initialize(configured.bootstrap.initial_state()),
                             monkeypatch, capsys)
    assert report["saved_energy_clock"]["status"] == "ready" and "saved_business_calendar" in report
    assert {"saved_login_rewards", "saved_tower_trades"}.isdisjoint(report)


def test_unreadable_saves_report_their_repairs_by_the_reading_error(configured, tmp_path):
    saved = configured.initialize(configured.bootstrap.initial_state())
    damaged = Store(tmp_path / "damaged" / DATABASE_NAME, {
        **saved, LOGIN_REWARD_LEDGER: None, TOWER_TRADE_HISTORY: {"schema_version": 0, "records": {}}})
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"not a database")
    rewards, tower_trades = configured.login_rewards, configured.tower_trades
    assert cli.diagnose_saved_login_rewards(rewards, damaged.path) == {"error": LOGIN_REWARDS_INVALID}
    assert cli.diagnose_saved_tower_trades(tower_trades, damaged.path) == {"error": TOWER_HISTORY_INVALID}
    assert cli.diagnose_saved_login_rewards(rewards, garbage) == {"error": LOGIN_REWARDS_UNREADABLE}
    assert cli.diagnose_saved_tower_trades(tower_trades, garbage) == {"error": TOWER_TRADES_UNREADABLE}
    assert cli.diagnose_saved_tower_trades(tower_trades, tmp_path / "absent.sqlite3") == {"error": DATABASE_PATH_INVALID}


def test_sortie_gold_abilities_resolve_through_the_shared_image_aliases(configured):
    assert configured.gifts is not None, configured.gift_error
    assert configured.quests.image_aliases is configured.gifts.aliases
    assert service_readiness(configured)["main_quests"]["gold_abilities"]["image_aliases"] is True


def test_unit_sales_share_the_awakening_reward_ledger(configured):
    assert configured.sales is not None, configured.sale_error
    assert configured.sales.evolution_rewards is configured.evolution_rewards is not None


def test_the_fame_snapshot_reports_to_the_startup_registry(configured):
    assert configured.bootstrap.fame_provider.__self__ is configured.fame_repository
    assert configured.fame_repository.diagnostics is configured.bootstrap.startup_diagnostics


def test_rule_files_nested_past_the_scanner_stop_doctor_without_a_traceback(tmp_path, capsys):
    data, path = tmp_path / "server", tmp_path / "quest-rules.json"
    path.write_text("[" * SCANNER_OVERFLOW_DEPTH + "]" * SCANNER_OVERFLOW_DEPTH)
    assert path.stat().st_size <= cli.MAX_RULE_BYTES
    assert main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "missing"),
                 "--quest-rules", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == RecursionError.__name__
    assert not data.exists()


@pytest.mark.parametrize("error,refusal", COMMAND_FAILURES)
def test_failed_commands_print_the_protocol_code_and_status_beside_the_message(tmp_path, monkeypatch, capsys, error,
                                                                               refusal):
    def refuse(args):
        raise error

    monkeypatch.setattr(cli, "doctor", refuse)
    assert main(["doctor", "--data-dir", str(tmp_path / "server")]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": type(error).__name__, "message": str(error), **refusal}


@pytest.mark.parametrize("loader", ["load_resource_view", "load_bootstrap"])
def test_resource_manifests_nested_past_the_scanner_degrade_to_a_resource_error(tmp_path, monkeypatch, capsys, loader):
    def overflow(*args, **kwargs):
        raise RecursionError(NESTED_MANIFEST)

    if loader == "load_bootstrap":
        view = SimpleNamespace(manifest={"version": RESOURCE_VERSION, "base_version": RESOURCE_VERSION}, entries=(),
                               index=None, replacements=None, assets=None, mission_window=None, revival_metadata=None,
                               command_text_metadata=None, shop_window_closure_metadata=None)
        monkeypatch.setattr(cli, "load_resource_view", lambda *args, **kwargs: view)
    monkeypatch.setattr(cli, loader, overflow)
    data = tmp_path / "server"
    assert main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "missing")]) == 0
    readiness = json.loads(capsys.readouterr().out)["readiness"]
    assert readiness["native_cdn"] == {"ready": False, "error": NESTED_MANIFEST}
    assert readiness["startup_ready"] is False and not data.exists()


def test_import_masters_stages_a_capture_without_activating_it(tmp_path, capsys):
    data = tmp_path / "server"
    masters = MasterDataStore.for_data_dir(data)
    source = capture(tmp_path / "capture", fingerprint=masters.expected_hash)
    assert main(["import-masters", str(source), "--data-dir", str(data), "--stage"]) == 0
    staged = json.loads(capsys.readouterr().out)
    assert staged["activated"] is False and masters.pointer() is None
    assert [entry["snapshot"] for entry in masters.list_snapshots()] == [staged["snapshot"]]
    assert main(["import-masters", str(source), "--data-dir", str(data)]) == 0
    activated = json.loads(capsys.readouterr().out)
    assert activated == {**staged, "activated": True} and masters.pointer().snapshot == staged["snapshot"]

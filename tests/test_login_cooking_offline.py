from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps import cli
from aigis_ps.admin_app import BACKUP_DIRECTORY
from aigis_ps.admin_commit import InitializerRestartCheck
from aigis_ps.admin_offline_cooking import (AUDIT_KIND, COOKING_CURRENT, PLAN_INVALID, PLAN_STALE,
                                            apply_cooking_migration, plan_cooking_migration)
from aigis_ps.app import DATABASE_NAME
from aigis_ps.config_sets import ConfigSets, resolve
from aigis_ps.crystal_spending import settle_spending
from aigis_ps.energy_clock import JST, instant
from aigis_ps.errors import ProtocolError
from aigis_ps.generation import Generation, GenerationIdentity
from aigis_ps.login_cooking_state import advance_cooking, initialize_cooking, migrate_cooking, read_cooking, sync_cooking
from aigis_ps.login_reward_service import OFFLINE_COOKING_MIGRATION
from aigis_ps.storage import AUTO_BACKUP_KIND, Store
from test_admin_offline import MISSING_DATABASE, PATH_INVALID
from test_cli import LOGIN_REWARDS_UNAVAILABLE, SCANNER_OVERFLOW_DEPTH
from test_cli_admin_serve import NEVER_RESET, TRADE_KEY, defaults
from test_quest_service import bundle


BASES = Path(__file__).resolve().parent / "golden" / "p25a" / "sugoroku.json"
SAVED_BORDER, NEXT_BORDER, OVERDRAWN_BORDER = 250, 600, 700
MONTH_START = datetime(2026, 12, 1, 12, tzinfo=JST)
PLANNED_AT = datetime(2026, 12, 15, 12, tzinfo=JST)
NEXT_MONTH = datetime(2027, 1, 2, 12, tzinfo=JST)
ISSUED, OVERDRAWN = 2, 8
QUOTAS = {SAVED_BORDER: 25, NEXT_BORDER: 13}
QUOTA_BELOW_ISSUED = "login_cooking_quota_below_issued"
RESTART_INCOMPATIBLE = "admin_restart_incompatible"
OFFLINE_BUSY = "admin_offline_database_busy"
DATABASE_INVALID = "admin_offline_database_invalid"
AUDIT_TARGET = "/login_cooking"
RULES_CHANGED = "login_cooking_rules_changed"
SEPARATE_OUTPUT = "Cooking migration plans require a separate JSON output file"
FOREIGN_PLAN = "The cooking migration plan names the database of another data directory"
BORDER_UNKNOWN = "Login reward resources are unavailable"
PLAN_COMMAND, APPLY_COMMAND = OFFLINE_COOKING_MIGRATION


def cooking_state(issued=ISSUED):
    # The base month spent 800 crystals, so settling it gives December a quota at every border.
    state = sync_cooking(settle_spending(json.loads(BASES.read_bytes())["bases"]["spent"], MONTH_START), MONTH_START,
                         border=SAVED_BORDER)
    for day in range(1, issued + 1):
        state, granted = advance_cooking(state, MONTH_START + timedelta(days=day), border=SAVED_BORDER, eligible=True)
        assert granted
    return state


def restarted(state):
    return initialize_cooking(state, border=NEXT_BORDER)


class Recorded:
    def __init__(self):
        self.checked = []

    def __call__(self, state):
        self.checked.append(deepcopy(state))


class RecordingPreflight:
    is_noop = False
    domains = MappingProxyType({"login_cooking": None})

    def __init__(self):
        self.checked = []

    def check(self, before, candidate, domains, generation):
        self.checked.append((before, deepcopy(candidate), set(domains)))


@pytest.fixture
def saved(tmp_path):
    data = tmp_path / "server"
    return data, Store(data / DATABASE_NAME, cooking_state())


def refusal(action):
    with pytest.raises(ProtocolError) as caught:
        action()
    return caught.value.code, caught.value.status


def planned(store, preflight, *, border=NEXT_BORDER, now=PLANNED_AT, initializer=restarted):
    return plan_cooking_migration(store.path, border=border, now=now, initializer=initializer, preflight=preflight)


def applied(plan, data, preflight, *, border=NEXT_BORDER):
    return apply_cooking_migration(plan, border=border, initializer=restarted, preflight=preflight,
                                   backup_directory=data / BACKUP_DIRECTORY)


def backups(store):
    return [item["kind"] for item in store.list_backups().items]


def test_plan_and_apply_recount_the_quota_once_behind_one_backup_and_audit(saved):
    (data, store), preflight = saved, Recorded()
    before = store.profile()
    plan = planned(store, preflight)
    assert store.profile() == before
    old, new = plan["cooking_before"], plan["cooking_after"]
    assert (old["border"], new["border"]) == (SAVED_BORDER, NEXT_BORDER)
    assert (old["quota"], new["quota"]) == (QUOTAS[SAVED_BORDER], QUOTAS[NEXT_BORDER])
    assert old["issued"] == new["issued"] == ISSUED and plan["migrated_at"] == PLANNED_AT.isoformat()
    result = applied(plan, data, preflight)
    after = store.profile()
    assert result["status"] == "applied" and result["revision"] == after.revision == before.revision + 1
    assert read_cooking(after.state) == new and restarted(after.state) == after.state
    assert result["backup_before_migration"] and backups(store) == [AUTO_BACKUP_KIND]
    operation = store.find_admin_operation(plan["plan_sha256"])
    assert (operation["kind"], operation["targets"]) == (AUDIT_KIND, [AUDIT_TARGET])
    assert preflight.checked and all(checked == after.state for checked in preflight.checked)
    again = applied(plan, data, preflight)
    assert (again["status"], again["revision"]) == ("already_applied", after.revision)
    assert store.profile() == after and backups(store) == [AUTO_BACKUP_KIND]


def test_an_ended_month_only_changes_the_border(saved):
    _, store = saved
    plan = planned(store, Recorded(), now=NEXT_MONTH)
    old, new = plan["cooking_before"], plan["cooking_after"]
    assert (new["border"], new["quota"], new["period"]) == (NEXT_BORDER, old["quota"], old["period"])


def test_an_unchanged_border_and_an_overdrawn_month_are_refused(tmp_path):
    data = tmp_path / "server"
    current = Store(data / DATABASE_NAME, cooking_state())
    assert refusal(lambda: planned(current, Recorded(), border=SAVED_BORDER,
                                   initializer=lambda state: state)) == (COOKING_CURRENT, 409)
    overdrawn = Store(tmp_path / "overdrawn" / DATABASE_NAME, cooking_state(OVERDRAWN))
    assert refusal(lambda: planned(overdrawn, Recorded(), border=OVERDRAWN_BORDER)) == (QUOTA_BELOW_ISSUED, 409)


def test_the_plan_proves_the_restart_and_an_unchanged_preflight(saved):
    _, store = saved

    def remigrating(state):
        return migrate_cooking(state, PLANNED_AT, border=NEXT_BORDER + 1)

    def rewriting(state):
        state["login_cooking"]["quota"] = 0

    assert refusal(lambda: planned(store, Recorded(), initializer=remigrating))[0] == RESTART_INCOMPATIBLE
    assert refusal(lambda: planned(store, rewriting)) == ("admin_offline_preflight_invalid", 503)


def test_changed_plans_borders_and_databases_are_refused_without_a_backup(saved):
    (data, store), preflight = saved, Recorded()
    plan = planned(store, preflight)
    assert refusal(lambda: applied({**plan, "border": NEXT_BORDER + 1}, data, preflight,
                                   border=NEXT_BORDER + 1)) == (PLAN_INVALID, 400)
    assert refusal(lambda: applied(plan, data, preflight, border=NEXT_BORDER + 1)) == (PLAN_STALE, 409)
    profile = store.profile()
    store.update_state({**profile.state, "sentinel": True}, profile.revision)
    changed, stored = store.profile(), store.path.read_bytes()
    assert refusal(lambda: applied(plan, data, preflight)) == (PLAN_STALE, 409)
    assert store.path.read_bytes() == stored
    assert store.profile() == changed and not (data / BACKUP_DIRECTORY).exists()


def test_journal_files_mean_the_server_may_still_be_running(saved):
    _, store = saved
    Path(f"{store.path}-wal").write_bytes(b"")
    assert refusal(lambda: planned(store, Recorded())) == (OFFLINE_BUSY, 409)


def test_a_missing_database_is_refused_like_every_offline_command(tmp_path):
    with pytest.raises(ProtocolError) as missing:
        plan_cooking_migration(tmp_path / DATABASE_NAME, border=NEXT_BORDER, now=PLANNED_AT, initializer=restarted,
                               preflight=Recorded())
    assert (missing.value.code, missing.value.status, str(missing.value)) == (PATH_INVALID, 400, MISSING_DATABASE)
    assert not (tmp_path / DATABASE_NAME).exists()


def generations(built, *, border=NEXT_BORDER):
    def build(inputs, progress=None):
        bootstrap = SimpleNamespace(clock=lambda: PLANNED_AT, startup_diagnostics=inputs.diagnostics)
        login_rewards = None if border is None else SimpleNamespace(cooking_border=border)
        source = SimpleNamespace(bootstrap=bootstrap, initialize=restarted, login_rewards=login_rewards,
                                 login_reward_error="login_reward_dependencies_unavailable")
        identity = GenerationIdentity(code_digest="c" * 64, config_set=inputs.config_digest, content_versions={})
        generation = Generation(identity, source, {}, {"startup_ready": True}, diagnostics=inputs.diagnostics)
        built.append(generation)
        return generation

    return build


def offline_commands(monkeypatch, built, **options):
    preflight = RecordingPreflight()
    monkeypatch.setattr(cli, "admin_checks", lambda registry: (preflight, InitializerRestartCheck()))
    monkeypatch.setattr(cli, "build_generation", generations(built, **options))
    return preflight


def plan_command(data, output):
    return cli.main([PLAN_COMMAND, "--data-dir", str(data), "--output", str(output)])


def apply_command(data, plan_path):
    return cli.main([APPLY_COMMAND, "--data-dir", str(data), "--plan", str(plan_path)])


def test_the_commands_migrate_to_the_border_of_the_next_start(saved, tmp_path, monkeypatch, capsys):
    (data, store), built = saved, []
    preflight, plan_path = offline_commands(monkeypatch, built), tmp_path / "plans" / "cooking.json"
    assert plan_command(data, plan_path) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["plan_path"] == str(plan_path.resolve()) and json.loads(plan_path.read_bytes()) == printed["plan"]
    assert printed["plan"]["border"] == NEXT_BORDER and printed["plan"]["migrated_at"] == PLANNED_AT.isoformat()
    assert apply_command(data, plan_path) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    after = store.profile()
    assert read_cooking(after.state)["border"] == NEXT_BORDER and len(built) == 2
    assert backups(store) == [AUTO_BACKUP_KIND]
    assert preflight.checked and all(before == {} and candidate == after.state and domains == set(preflight.domains)
                                     for before, candidate, domains in preflight.checked)


def test_the_commands_refuse_plans_that_cannot_be_kept_apart_or_name_another_database(saved, tmp_path, monkeypatch,
                                                                                      capsys):
    data, store = saved
    offline_commands(monkeypatch, [])
    revision, plan_path, other = store.profile().revision, tmp_path / "cooking.json", tmp_path / "other"
    for output in (tmp_path / "cooking.txt", data / DATABASE_NAME):
        assert plan_command(data, output) == 1
        assert json.loads(capsys.readouterr().out)["message"] == SEPARATE_OUTPUT
    assert plan_command(data, plan_path) == 0
    capsys.readouterr()
    Store(other / DATABASE_NAME, cooking_state())
    assert apply_command(other, plan_path) == 1
    assert json.loads(capsys.readouterr().out)["message"] == FOREIGN_PLAN
    assert store.profile().revision == revision and not (other / BACKUP_DIRECTORY).exists()


def test_the_commands_need_the_login_reward_resources(saved, tmp_path, monkeypatch, capsys):
    data, store = saved
    offline_commands(monkeypatch, [], border=None)
    before, plan_path = store.profile(), tmp_path / "cooking.json"
    assert plan_command(data, plan_path) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused["error"] == ProtocolError.__name__ and BORDER_UNKNOWN in refused["message"]
    assert (refused["code"], refused["status"]) == (LOGIN_REWARDS_UNAVAILABLE, 503)
    assert not plan_path.exists() and store.profile() == before


def test_the_commands_build_the_generation_on_the_configuration_of_the_next_start(saved, tmp_path, monkeypatch,
                                                                                    capsys):
    (data, store), built = saved, []
    offline_commands(monkeypatch, built)
    sets, unswitched = ConfigSets(data), resolve(defaults(data)).digest
    switched = sets.create({TRADE_KEY: NEVER_RESET}, source="console")
    store.record_generation(switched.identifier, "hot")
    assert plan_command(data, tmp_path / "cooking.json") == 0
    assert json.loads(capsys.readouterr().out)["plan"]["border"] == NEXT_BORDER
    [generation] = built
    assert sets.read_active().set_id == switched.identifier
    assert generation.identity.config_set == resolve(defaults(data)).digest != unswitched


def test_the_commands_refuse_a_running_server_before_rolling_forward_or_building(saved, tmp_path, monkeypatch, capsys):
    (data, store), built = saved, []
    offline_commands(monkeypatch, built)
    sets = ConfigSets(data)
    store.record_generation(sets.create({TRADE_KEY: NEVER_RESET}, source="console").identifier, "hot")
    Path(f"{store.path}-wal").write_bytes(b"")
    plan_path = tmp_path / "cooking.json"
    assert plan_command(data, plan_path) == 1
    refused = json.loads(capsys.readouterr().out)
    assert (refused["error"], refused["code"], refused["status"]) == (ProtocolError.__name__, OFFLINE_BUSY, 409)
    assert built == [] and sets.read_active() is None and not plan_path.exists()


@pytest.mark.parametrize("contents,refused", [(None, (PATH_INVALID, 400)), (b"", (DATABASE_INVALID, 503))],
                         ids=["missing", "empty"])
def test_the_commands_refuse_a_missing_or_unreadable_database_before_writing_it(tmp_path, monkeypatch, capsys,
                                                                                contents, refused):
    data, built = tmp_path / "server", []
    offline_commands(monkeypatch, built)
    database = data / DATABASE_NAME
    if contents is not None:
        data.mkdir()
        database.write_bytes(contents)
    assert plan_command(data, tmp_path / "cooking.json") == 1
    printed = json.loads(capsys.readouterr().out)
    assert (printed["code"], printed["status"]) == refused and built == []
    assert (database.read_bytes() if database.exists() else None) == contents


def test_a_plan_nested_past_the_scanner_stops_apply_without_a_traceback(saved, tmp_path, monkeypatch, capsys):
    data, store = saved
    offline_commands(monkeypatch, [])
    before, plan_path = store.profile(), tmp_path / "cooking.json"
    plan_path.write_text("[" * SCANNER_OVERFLOW_DEPTH + "]" * SCANNER_OVERFLOW_DEPTH)
    assert apply_command(data, plan_path) == 1
    assert json.loads(capsys.readouterr().out)["error"] == RecursionError.__name__
    assert store.profile() == before and not (data / BACKUP_DIRECTORY).exists()


def test_the_real_services_start_on_the_migrated_save(bundle, tmp_path, monkeypatch, capsys):
    assert bundle.login_rewards is not None, bundle.login_reward_error
    border = bundle.login_rewards.cooking_border
    initial = bundle.initialize(bundle.bootstrap.initial_state())
    foreign = migrate_cooking(initial, instant(bundle.bootstrap.clock()), border=border + 1)
    assert bundle.initialize(deepcopy(foreign)) == foreign
    assert refusal(lambda: bundle.login_rewards.current_cooking(foreign)) == (RULES_CHANGED, 503)
    data, plan_path = tmp_path / "server", tmp_path / "cooking.json"
    store = Store(data / DATABASE_NAME, foreign)
    monkeypatch.setattr(cli, "services", lambda arguments: bundle)
    assert plan_command(data, plan_path) == 0
    assert json.loads(capsys.readouterr().out)["plan"]["cooking_after"]["border"] == border
    assert apply_command(data, plan_path) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    after = store.profile().state
    assert read_cooking(after)["border"] == border and bundle.initialize(deepcopy(after)) == after
    assert bundle.login_rewards.current_cooking(after) == read_cooking(after)

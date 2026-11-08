from copy import deepcopy
import json

import pytest

from aigis_ps import cli
from aigis_ps.energy_clock import PREFIXES, anchor_seconds, set_anchor, validate_clock
from aigis_ps.energy_migration import inspect_database
from aigis_ps.storage import Store
from test_quest_service import bundle


def legacy(bundle, tmp_path):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    state.pop("energy_clock")
    row = state["tables"]["game_param"][0]
    for prefix in PREFIXES:
        set_anchor(row, anchor_seconds(row, prefix) - 3600, prefix)
    return Store(tmp_path / "aigis.sqlite3", state)


def preview(store, tmp_path):
    path = tmp_path / "energy-plan.json"
    assert cli.main(["energy-clock-plan", "--database", str(store.path), "--source-offset-minutes", "480",
                     "--output", str(path)]) == 0
    return path


def test_cli_plan_apply_and_repeat_preserve_profile_and_create_verified_backup(bundle, tmp_path, capsys):
    store = legacy(bundle, tmp_path)
    before = store.profile()
    plan_path = preview(store, tmp_path)
    plan_output = json.loads(capsys.readouterr().out)
    assert store.profile() == before
    assert plan_output["plan"]["expected_revision"] == before.revision
    assert before.user_key not in plan_path.read_text() and before.method_key not in plan_path.read_text()
    args = ["energy-clock-apply", "--plan", str(plan_path), "--asset-dir", str(bundle.bootstrap.asset_root),
            "--backup-directory", str(tmp_path / "backups")]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    saved = store.profile()
    assert result["status"] == "applied" and result["backup"]
    assert saved.revision == before.revision + 1
    assert validate_clock(saved.state) is None and inspect_database(store.path)["ready"] is True
    restored = deepcopy(saved.state)
    restored.pop("energy_clock")
    for prefix in PREFIXES:
        row = restored["tables"]["game_param"][0]
        set_anchor(row, anchor_seconds(row, prefix) - 3600, prefix)
    assert restored == before.state
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_applied"
    assert store.profile() == saved


def test_cli_apply_preflight_rejects_invalid_native_player_without_backup(bundle, tmp_path, capsys):
    store = legacy(bundle, tmp_path)
    profile = store.profile()
    state = deepcopy(profile.state)
    state["tables"]["game_param"][0]["Money"] = -1
    before = store.update_state(state, profile.revision)
    path = preview(store, tmp_path)
    capsys.readouterr()
    backup = tmp_path / "backups"
    assert cli.main(["energy-clock-apply", "--plan", str(path), "--asset-dir", str(bundle.bootstrap.asset_root),
                     "--backup-directory", str(backup)]) == 1
    assert store.profile() == before and not backup.exists()


def test_cli_unknown_clock_stops_before_server_initialization(bundle, tmp_path, monkeypatch, capsys):
    store = legacy(bundle, tmp_path)
    before = store.profile()
    monkeypatch.setattr(cli, "services", lambda _: pytest.fail("Server initialization must wait for migration"))
    assert cli.main(["serve", "--data-dir", str(tmp_path)]) == 1
    assert "energy-clock-plan" in capsys.readouterr().out
    assert store.profile() == before


def test_cli_preview_cannot_overwrite_database(bundle, tmp_path, capsys):
    store = legacy(bundle, tmp_path)
    before = store.path.read_bytes()
    assert cli.main(["energy-clock-plan", "--database", str(store.path), "--source-offset-minutes", "480",
                     "--output", str(store.path)]) == 1
    assert store.path.read_bytes() == before


def test_doctor_distinguishes_ready_resources_from_unmigrated_save(bundle, tmp_path, monkeypatch, capsys):
    legacy(bundle, tmp_path)
    monkeypatch.setattr(cli, "services", lambda _: bundle)
    assert cli.main(["doctor", "--data-dir", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["readiness"]["startup_ready"] is False
    assert result["readiness"]["saved_energy_clock"]["status"] == "migration_required"
    assert result["readiness"]["bootstrap"]["startup_ready"] is True

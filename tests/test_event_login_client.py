from copy import deepcopy
import json
from pathlib import Path

import pytest

from aigis_ps.event_login_catalog import RESOURCE_NAME
from aigis_ps.event_login_rules import EventLoginRules
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.resource_view import load_resource_view
from client import prepare_client as client
from test_event_login_resources import event_resource, rules
from test_event_pass_resource_view import tree, index_resources
from test_event_pass_revival import resources
from test_package_text_overlay import package_resources


@pytest.fixture
def event_tree(tree, event_resource):
    tree.payloads[RESOURCE_NAME] = event_resource
    tree.raw_index = index_resources(tree, tree.payloads)
    return tree


def view(case, settings=None):
    master = MasterDataStore(case.master_root, expected_hash=case.profile.sha256)
    return load_resource_view(case.resources, case.source, case.profile.sha256,
        master_provider=master.get_xml, event_login_rules=settings)


def prepare(case, settings=None, **kwargs):
    return client.prepare(case.source, case.runtime, case.workspace, case.profile,
        resource_root=case.resources, master_root=case.master_root, event_login_rules=settings, **kwargs)


@pytest.mark.parametrize("settings", [None, EventLoginRules(), rules(enabled=False)])
def test_default_settings_preserve_old_resource_version(event_tree, settings):
    original, current = view(event_tree), view(event_tree, settings)
    assert original.index == current.index
    assert original.manifest == current.manifest
    assert current.event_login_metadata is None
    assert current.assets.read(RESOURCE_NAME)[0] == event_tree.payloads[RESOURCE_NAME]


def test_revival_plan_copy_and_tail_share_frozen_rules(event_tree, monkeypatch):
    settings = rules()
    current = view(event_tree, settings)
    observed, original = [], client.load_resource_view
    def load(*args, **kwargs):
        observed.append(kwargs["event_login_rules"])
        return original(*args, **kwargs)
    monkeypatch.setattr(client, "load_resource_view", load)
    result = prepare(event_tree, settings)
    assert len(observed) == 2 and all(value is settings for value in observed)
    assert result["manifest"]["event_login_revival"] == current.event_login_metadata
    assert result["manifest"]["resource_snapshot"]["version"] == current.manifest["version"]
    assert client.verify_runtime(event_tree.runtime, event_tree.workspace, event_tree.profile) == result["manifest"]
    assert prepare(event_tree, settings)["status"] == "already-prepared"
    assert current.base_assets.read(RESOURCE_NAME)[0] == event_tree.payloads[RESOURCE_NAME]


@pytest.mark.parametrize("field", ["rules_hash", "effective_sha256", "campaign_ids", "window_policy"])
def test_tampered_event_metadata_is_rejected(event_tree, field):
    manifest = prepare(event_tree, rules())["manifest"]
    altered = deepcopy(manifest)
    altered["event_login_revival"][field] = [] if field == "campaign_ids" else "invalid"
    (event_tree.runtime / client.MANIFEST).write_text(json.dumps(altered), encoding="utf8")
    with pytest.raises(client.ClientError, match="event login"):
        client.verify_runtime(event_tree.runtime, event_tree.workspace, event_tree.profile)


def test_old_runtime_without_event_metadata_remains_valid(event_tree):
    result = prepare(event_tree)
    assert "event_login_revival" not in result["manifest"]
    assert client.verify_runtime(event_tree.runtime, event_tree.workspace, event_tree.profile) == result["manifest"]
    client._check_requested_event_login(result["manifest"], EventLoginRules())
    with pytest.raises(client.ClientError, match="differs"):
        client._check_requested_event_login(result["manifest"], rules())


def test_index_change_during_event_copy_keeps_existing_runtime(event_tree):
    original = prepare(event_tree)["manifest"]
    def change(event):
        if event["event"] == "resources_copied":
            index_resources(event_tree, event_tree.payloads, "2" * 40)
    with pytest.raises(client.ClientError, match="index changed"):
        prepare(event_tree, rules(), progress=change)
    assert client.verify_runtime(event_tree.runtime, event_tree.workspace, event_tree.profile) == original


def test_client_main_inspect_forwards_frozen_event_rules(event_tree, monkeypatch, capsys):
    settings = rules()
    path = event_tree.workspace / "event.json"
    path.write_text(json.dumps(settings.to_dict()), encoding="utf8")
    monkeypatch.setattr(client, "PROJECT_ROOT", event_tree.workspace)
    monkeypatch.setattr(client, "WORKSPACE_ROOT", event_tree.workspace)
    monkeypatch.setattr(client, "PROFILE", event_tree.profile)
    monkeypatch.setattr(client.sys, "argv", ["prepare_client.py", "inspect", "--source", str(event_tree.source),
        "--runtime", str(event_tree.workspace / "runtime/client"), "--resource-dir", str(event_tree.resources),
        "--master-dir", str(event_tree.master_root), "--event-login-rules", str(path)])
    assert client.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["event_login_revival"]["rules"] == settings.to_dict()
    assert not (event_tree.workspace / "runtime/client").exists()


def test_dry_launch_rejects_mismatched_event_rules_without_process(event_tree, monkeypatch):
    prepare(event_tree, rules())
    original = client.verify_runtime
    monkeypatch.setattr(client, "verify_runtime", lambda runtime, _: original(runtime, event_tree.workspace, event_tree.profile))
    monkeypatch.setattr(client, "check_server", lambda **_: pytest.fail("Dry launch contacted a server"))
    monkeypatch.setattr(client.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Dry launch started a process"))
    assert client.launch(event_tree.runtime, dry_run=True, event_login_rules=rules())["status"] == "verified"
    with pytest.raises(client.ClientError, match="differs"):
        client.launch(event_tree.runtime, dry_run=True, event_login_rules=EventLoginRules())


def test_powershell_wrapper_forwards_event_rules():
    script = Path(client.__file__).with_name("prepare-client.ps1").read_text("utf8")
    assert "[string]$EventLoginRulesFile" in script
    assert "@('--event-login-rules', $EventLoginRulesFile)" in script

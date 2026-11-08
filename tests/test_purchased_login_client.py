from copy import deepcopy
import json
from pathlib import Path

import pytest

from aigis_ps.masterdata import MasterDataStore
from aigis_ps.purchased_login_rules import PurchasedLoginRules
from aigis_ps.resource_view import load_resource_view
from client import prepare_client as client
from test_event_login_client import event_tree
from test_event_login_resources import event_resource
from test_event_pass_resource_view import tree, index_resources, RULES as PASS_RULES
from test_event_pass_revival import resources
from test_package_text_overlay import package_resources


RULES = PurchasedLoginRules(True, (41, 136))


def view(case, settings=None, pass_rules=None):
    master = MasterDataStore(case.master_root, expected_hash=case.profile.sha256)
    return load_resource_view(case.resources, case.source, case.profile.sha256, master_provider=master.get_xml,
                              purchased_login_rules=settings, revival_rules=pass_rules)


def prepare(case, settings=None, **kwargs):
    return client.prepare(case.source, case.runtime, case.workspace, case.profile, resource_root=case.resources,
                          master_root=case.master_root, purchased_login_rules=settings, **kwargs)


def test_disabled_view_keeps_legacy_index_and_has_no_metadata(event_tree):
    before, after = view(event_tree), view(event_tree, PurchasedLoginRules())
    assert before.index == after.index and before.manifest == after.manifest
    assert after.purchased_login_metadata is None
    result = prepare(event_tree)
    assert "purchased_login" not in result["manifest"]
    assert client.verify_runtime(event_tree.runtime, event_tree.workspace, event_tree.profile) == result["manifest"]


def test_combined_overlays_prepare_one_frozen_resource_snapshot(event_tree, monkeypatch):
    current = view(event_tree, RULES, PASS_RULES)
    assert current.package_texts is not None and current.purchased_login is not None
    seen, original = [], client.load_resource_view
    def observe(*args, **kwargs):
        seen.append(kwargs["purchased_login_rules"])
        return original(*args, **kwargs)
    monkeypatch.setattr(client, "load_resource_view", observe)
    result = prepare(event_tree, RULES, revival_rules=PASS_RULES)
    assert len(seen) == 2 and all(settings is RULES for settings in seen)
    manifest = result["manifest"]
    assert manifest["purchased_login"] == current.purchased_login_metadata
    assert manifest["resource_snapshot"]["version"] == current.manifest["version"]
    assert client.verify_runtime(event_tree.runtime, event_tree.workspace, event_tree.profile) == manifest
    assert prepare(event_tree, RULES, revival_rules=PASS_RULES)["status"] == "already-prepared"


@pytest.mark.parametrize("field", ["rules_hash", "effective_hashes", "text_indices", "campaign_ids"])
def test_modified_purchased_metadata_is_rejected(event_tree, field):
    result = prepare(event_tree, RULES)
    manifest = deepcopy(result["manifest"])
    record = manifest["purchased_login"]
    record[field] = [] if field == "campaign_ids" else {} if field in ("effective_hashes", "text_indices") else "wrong"
    (event_tree.runtime / client.MANIFEST).write_text(json.dumps(manifest), encoding="utf8")
    with pytest.raises(client.ClientError, match="purchased login"):
        client.verify_runtime(event_tree.runtime, event_tree.workspace, event_tree.profile)


def test_index_change_at_tail_does_not_publish_partial_client(event_tree):
    before = prepare(event_tree)["manifest"]
    def change(event):
        if event["event"] == "resources_copied":
            index_resources(event_tree, event_tree.payloads, "2" * 40)
    with pytest.raises(client.ClientError, match="index changed"):
        prepare(event_tree, RULES, progress=change)
    assert client.verify_runtime(event_tree.runtime, event_tree.workspace, event_tree.profile) == before


def test_inspect_command_loads_purchased_rule_file(event_tree, monkeypatch, capsys):
    path = event_tree.workspace / "purchased.json"
    path.write_text(json.dumps(RULES.to_dict()), encoding="utf8")
    monkeypatch.setattr(client, "PROJECT_ROOT", event_tree.workspace)
    monkeypatch.setattr(client, "WORKSPACE_ROOT", event_tree.workspace)
    monkeypatch.setattr(client, "PROFILE", event_tree.profile)
    monkeypatch.setattr(client.sys, "argv", ["prepare_client.py", "inspect", "--source", str(event_tree.source),
        "--runtime", str(event_tree.workspace / "runtime/client"), "--resource-dir", str(event_tree.resources),
        "--master-dir", str(event_tree.master_root), "--purchased-login-rules", str(path)])
    assert client.main() == 0
    assert json.loads(capsys.readouterr().out)["purchased_login"]["rules"] == RULES.to_dict()
    assert not (event_tree.workspace / "runtime/client").exists()


def test_dry_launch_checks_config_without_starting_process(event_tree, monkeypatch):
    prepare(event_tree, RULES)
    original = client.verify_runtime
    monkeypatch.setattr(client, "verify_runtime", lambda runtime, _: original(runtime, event_tree.workspace, event_tree.profile))
    monkeypatch.setattr(client, "check_server", lambda **_: pytest.fail("Dry launch contacted a server"))
    monkeypatch.setattr(client.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Dry launch started a process"))
    assert client.launch(event_tree.runtime, dry_run=True, purchased_login_rules=RULES)["status"] == "verified"
    with pytest.raises(client.ClientError, match="differ"):
        client.launch(event_tree.runtime, dry_run=True, purchased_login_rules=PurchasedLoginRules())


def test_powershell_wrapper_forwards_purchased_rule_file():
    script = Path(client.__file__).with_name("prepare-client.ps1").read_text("utf8")
    assert "[string]$PurchasedLoginRulesFile" in script
    assert "@('--purchased-login-rules', $PurchasedLoginRulesFile)" in script

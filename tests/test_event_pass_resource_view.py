from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.event_pass_revival import (EventPassRevivalRules, MAX_RULES_BYTES, MISSION_RESOURCE,
    PASS_RESOURCE, build_event_pass_revival, load_revival_rules)
from aigis_ps.local_present_templates import build_present_template_overlay
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.resource_overlay import build_resource_overlay
from aigis_ps.resource_view import load_resource_view
from client import prepare_client as client
import test_client_prepare as client_fixtures
from test_event_pass_revival import resources
from test_package_text_overlay import package_resources
from aigis_ps.package_text_overlay import IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE


RULES = EventPassRevivalRules("event_pass_20261015", {1611: 2, 1607: 4, 1608: 4, 1609: 4, 1610: 6})


def decode(value):
    return decompress_all4(value) if value.startswith(b"ALL4") else value


def index_resources(case, payloads, version="1" * 40):
    rows = []
    for name, value in payloads.items():
        key = hashlib.sha256(name.encode()).hexdigest()
        (case.resources / ".cache" / key).write_bytes(value)
        rows.append(f"{'a' * 40},{hashlib.md5(value).hexdigest()},5,{len(value)},{key},{name}\n")
    raw = bytes(value ^ 0xDA for value in "".join(rows).encode())
    digest = hashlib.sha256(raw).hexdigest()
    snapshot = version + "-" + digest[:16]
    directory = case.resources / "indexes" / snapshot
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.bin").write_bytes(raw)
    manifest = {"schema_version": 1, "snapshot": snapshot, "version": version,
        "input_sha256": case.profile.sha256, "index_name": client.PC_INDEX_NAME, "index_sha256": digest}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    (case.resources / "active-index.json").write_text(json.dumps({"snapshot": snapshot, "index_sha256": digest}), encoding="utf8")
    return raw


@pytest.fixture
def tree(resources, package_resources):
    case = client_fixtures.ClientPreparationTests("runTest")
    case.setUp()
    try:
        master_root, cards = case.activate_present_templates()
        case.master_root, case.cards = master_root, cards
        case.payloads = {"PresentUnitData.atb": case.content,
                         PASS_RESOURCE: resources[0][PASS_RESOURCE], MISSION_RESOURCE: resources[0][MISSION_RESOURCE]}
        case.payloads.update({name: package_resources[name] for name in (IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE)})
        case.raw_index = index_resources(case, case.payloads)
        yield case
    finally:
        case.doCleanups()


def view(case, rules=None):
    master = MasterDataStore(case.master_root, expected_hash=case.profile.sha256)
    return load_resource_view(case.resources, case.source, case.profile.sha256,
                              master_provider=master.get_xml, revival_rules=rules)


def prepare(case, rules=None, **kwargs):
    return client.prepare(case.source, case.runtime, case.workspace, case.profile,
                          resource_root=case.resources, master_root=case.master_root, revival_rules=rules, **kwargs)


def test_disabled_view_matches_original_template_only_overlay(tree):
    current = view(tree)
    expected = build_resource_overlay("1" * 40, tree.raw_index,
        {"PresentUnitData.atb": build_present_template_overlay(tree.payloads["PresentUnitData.atb"], tree.cards)})
    explicit = view(tree, None)
    assert current.index == explicit.index == expected.index
    assert dict(current.manifest) == dict(explicit.manifest)
    assert current.manifest["version"] == expected.version
    assert current.revival is None and current.revival_metadata is None
    assert set(current.replacements) == {"PresentUnitData.atb"}
    for name in (PASS_RESOURCE, MISSION_RESOURCE):
        assert current.assets.read(name)[0] == current.base_assets.read(name)[0] == tree.payloads[name]


def test_revival_and_present_templates_share_one_effective_overlay(tree):
    current = view(tree, RULES)
    expected = build_resource_overlay("1" * 40, tree.raw_index, dict(current.replacements))
    assert current.index == expected.index and current.manifest["version"] == expected.version
    assert set(current.replacements) == {"PresentUnitData.atb", PASS_RESOURCE, MISSION_RESOURCE,
                                         IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE}
    assert current.package_texts.text_indices["LocalEventPassPackageName"] == 6433
    assert current.revival_rules is RULES and current.revival.rules_hash == RULES.content_version
    for name, original in tree.payloads.items():
        assert current.base_assets.read(name)[0] == original
        assert current.assets.read(name)[0] == current.replacements[name]
        assert (tree.resources / ".cache" / hashlib.sha256(name.encode()).hexdigest()).read_bytes() == original
    json.dumps(current.revival_metadata)
    changed = current.revival_metadata
    changed["rules"]["mission_points"]["1611"] = 99
    assert current.revival_metadata["rules"]["mission_points"]["1611"] == 2


def test_plan_copy_tail_check_and_manifest_share_same_frozen_rules(tree, monkeypatch):
    original = client.load_resource_view
    seen = []
    def observe(*args, **kwargs):
        seen.append(kwargs.get("revival_rules"))
        return original(*args, **kwargs)
    monkeypatch.setattr(client, "load_resource_view", observe)
    expected = view(tree, RULES)
    result = prepare(tree, RULES)
    assert seen == [RULES, RULES] and all(item is RULES for item in seen)
    manifest = result["manifest"]
    assert manifest["event_pass_revival"] == expected.revival_metadata
    assert manifest["resource_snapshot"]["version"] == expected.manifest["version"]
    for entry in expected.entries:
        target = tree.runtime / ".cache" / entry.cache_key
        assert target.read_bytes() == expected.assets.read(entry.name)[0]
        assert target.with_suffix(".md5").read_bytes() == b"\xef\xbb\xbf" + entry.md5.encode() + b"\r\n"
    assert client.verify_runtime(tree.runtime, tree.workspace, tree.profile) == manifest
    assert prepare(tree, RULES)["status"] == "already-prepared"


def test_old_manifest_without_revival_remains_valid(tree):
    result = prepare(tree)
    assert "event_pass_revival" not in result["manifest"]
    assert client.verify_runtime(tree.runtime, tree.workspace, tree.profile) == result["manifest"]
    assert prepare(tree)["status"] == "already-prepared"


def test_rule_change_preserves_old_runtime_as_backup_and_changes_version(tree):
    original = prepare(tree, RULES)["manifest"]
    changed = EventPassRevivalRules(RULES.pass_id, RULES.mission_points, display_text="different revival")
    result = prepare(tree, changed)
    assert result["manifest"]["resource_snapshot"] != original["resource_snapshot"]
    assert json.loads((Path(result["backup"]) / client.MANIFEST).read_text("utf8")) == original
    assert result["manifest"]["event_pass_revival"]["rules"] == changed.to_dict()


def test_resource_change_at_tail_check_never_publishes_partial_runtime(tree):
    old = prepare(tree, RULES)["manifest"]
    def mutate(event):
        if event["event"] == "resources_copied":
            index_resources(tree, tree.payloads, "2" * 40)
    with pytest.raises(client.ClientError, match="index changed"):
        prepare(tree, RULES, refresh=True, progress=mutate)
    assert client.verify_runtime(tree.runtime, tree.workspace, tree.profile) == old
    assert not list(tree.workspace.glob(".runtime.prepare-*"))
    assert not list(tree.workspace.glob("runtime.backup-*"))


def test_invalid_revival_preserves_existing_runtime(tree):
    original = prepare(tree)["manifest"]
    with pytest.raises(ValueError, match="does not exist"):
        prepare(tree, EventPassRevivalRules("missing", {1611: 1}))
    assert client.verify_runtime(tree.runtime, tree.workspace, tree.profile) == original
    assert not list(tree.workspace.glob("runtime.backup-*"))


@pytest.mark.parametrize("field", ["rules_hash", "effective_hashes", "metadata", "rules", "null_window"])
def test_modified_revival_manifest_metadata_is_rejected(tree, field):
    manifest = deepcopy(prepare(tree, RULES)["manifest"])
    record = manifest["event_pass_revival"]
    if field == "rules_hash":
        record[field] = "0" * 64
    elif field == "effective_hashes":
        record[field][PASS_RESOURCE] = "0" * 64
    elif field == "metadata":
        record[field]["daily_points"] += 1
    elif field == "null_window":
        record["metadata"]["start_at"] = None
    else:
        record[field]["mission_points"]["1611"] = 99
    (tree.runtime / client.MANIFEST).write_text(json.dumps(manifest), encoding="utf8")
    with pytest.raises(client.ClientError, match="revival metadata"):
        client.verify_runtime(tree.runtime, tree.workspace, tree.profile)


def test_dry_launch_checks_explicit_rules_without_process_or_health_contact(tree, monkeypatch):
    prepare(tree, RULES)
    original = client.verify_runtime
    monkeypatch.setattr(client, "verify_runtime", lambda runtime, _: original(runtime, tree.workspace, tree.profile))
    monkeypatch.setattr(client, "check_server", lambda **_: pytest.fail("Dry launch contacted server"))
    monkeypatch.setattr(client.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Dry launch started process"))
    assert client.launch(tree.runtime, dry_run=True, revival_rules=RULES)["status"] == "verified"
    with pytest.raises(client.ClientError, match="differs"):
        client.launch(tree.runtime, dry_run=True, revival_rules=EventPassRevivalRules(RULES.pass_id, {1611: 99}))


def test_rule_file_change_during_copy_does_not_replace_the_frozen_rule(tree):
    path = tree.workspace / "rules.json"
    path.write_text(json.dumps(RULES.to_dict()), encoding="utf8")
    frozen = load_revival_rules(path)
    def mutate(event):
        if event["event"] == "resources_copied":
            path.write_text(json.dumps(EventPassRevivalRules(RULES.pass_id, {1611: 99}).to_dict()), encoding="utf8")
    result = prepare(tree, frozen, progress=mutate)
    assert result["manifest"]["event_pass_revival"]["rules"] == RULES.to_dict()
    assert load_revival_rules(path).content_version != frozen.content_version


def test_shared_loader_round_trips_canonical_json(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(RULES.to_dict()), encoding="utf8")
    loaded = load_revival_rules(path)
    assert loaded.to_dict() == RULES.to_dict() and loaded.content_version == RULES.content_version
    assert load_revival_rules(None) is None
    assert all(type(key) is str for key in loaded.to_dict()["mission_points"])


@pytest.mark.parametrize("content", ["null", "[]", "{}", '{"pass_id":"x"}',
    '{"pass_id":"x","mission_points":{"1":2},"unknown":1}',
    '{"pass_id":"x","pass_id":"y","mission_points":{"1":2}}',
    '{"pass_id":"x","mission_points":{"1":2,"1":3}}', " " * (MAX_RULES_BYTES + 1)])
def test_shared_loader_rejects_unknown_missing_duplicate_or_oversized_json(tmp_path, content):
    path = tmp_path / "rules.json"
    path.write_text(content, encoding="utf8")
    with pytest.raises(ValueError):
        load_revival_rules(path)


def test_inspect_command_forwards_one_loaded_rules_object(tree, monkeypatch, capsys):
    path = tree.workspace / "rules.json"
    path.write_text(json.dumps(RULES.to_dict()), encoding="utf8")
    monkeypatch.setattr(client, "PROJECT_ROOT", tree.workspace)
    monkeypatch.setattr(client, "WORKSPACE_ROOT", tree.workspace)
    monkeypatch.setattr(client, "PROFILE", tree.profile)
    monkeypatch.setattr(client.sys, "argv", ["prepare_client.py", "inspect", "--source", str(tree.source),
        "--runtime", str(tree.workspace / "runtime/client"), "--resource-dir", str(tree.resources),
        "--master-dir", str(tree.master_root), "--event-pass-revival", str(path)])
    assert client.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["event_pass_revival"]["rules"] == RULES.to_dict()
    assert not (tree.workspace / "runtime/client").exists()


def test_powershell_wrapper_passes_the_explicit_rule_path():
    script = (Path(client.__file__).with_name("prepare-client.ps1")).read_text("utf8")
    assert "[string]$EventPassRevivalFile" in script
    assert "@('--event-pass-revival', $EventPassRevivalFile)" in script


def test_empty_display_text_is_rejected_by_rules():
    with pytest.raises(ValueError, match="bounded UTF-8"):
        EventPassRevivalRules(RULES.pass_id, RULES.mission_points, display_text="")


def test_empty_display_text_is_rejected_by_shared_loader(tmp_path):
    path = tmp_path / "empty-display.json"
    path.write_text(json.dumps({**RULES.to_dict(), "display_text": ""}), encoding="utf8")
    with pytest.raises(ValueError, match="bounded UTF-8"):
        load_revival_rules(path)


def test_builder_rejects_tampered_empty_display_rule_before_assembly(resources):
    invalid = EventPassRevivalRules(RULES.pass_id, RULES.mission_points)
    object.__setattr__(invalid, "display_text", "")
    with pytest.raises(ValueError, match="bounded UTF-8"):
        build_event_pass_revival(resources[0][PASS_RESOURCE], resources[0][MISSION_RESOURCE], invalid)


def test_invalid_package_text_resource_cannot_publish_a_partial_runtime(tree):
    old = prepare(tree)["manifest"]
    before = dict(tree.payloads)
    tree.payloads[SYSTEM_TEXT_RESOURCE] = b"invalid SystemText"
    index_resources(tree, tree.payloads, "3" * 40)
    with pytest.raises(ValueError):
        prepare(tree, RULES, refresh=True)
    assert client.verify_runtime(tree.runtime, tree.workspace, tree.profile) == old
    assert not list(tree.workspace.glob(".runtime.prepare-*"))
    assert not list(tree.workspace.glob("runtime.backup-*"))
    assert tree.payloads[IMPLANT_RESOURCE] == before[IMPLANT_RESOURCE]

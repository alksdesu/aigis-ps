from argparse import Namespace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from aigis_ps import cli, config_sets
from aigis_ps.config_registry import REGISTRY, ConfigError
from aigis_ps.config_sets import (ACTIVE_POINTER, LOCK_NAME, MANIFEST_NAME, SET_ID_LENGTH, ConfigSets, ConfigSource,
                                  KeyMode, config_identity, resolve)
from aigis_ps.local_io import exclusive_file
from aigis_ps.storage import serialize


PROJECT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT / "config"
NEVER = b'{"reset_type_1":"never","reset_type_2":"window"}'
MONTHLY = b'{"reset_type_1":"month"}'


def example(name):
    return (EXAMPLES / name).read_bytes()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def arguments(data_dir, **overrides):
    return Namespace(data_dir=data_dir, **{spec.dest: overrides.get(spec.dest) for spec in REGISTRY.values()})


def error_of(action):
    with pytest.raises(ConfigError) as caught:
        action()
    return caught.value


def leftovers(sets):
    return sorted(path.name for path in sets.staging.iterdir()) if sets.staging.exists() else []


def junction(monkeypatch, link):
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == link or original(path))


def test_without_a_pointer_resolution_equals_the_command_line(tmp_path):
    data, override = tmp_path / "data", tmp_path / "quest.json"
    override.write_bytes(example("quest-rules.example.json"))
    args = Namespace(data_dir=data, quest_rules=override, rules=None)
    resolved = resolve(args)
    assert resolved.pointer is None and resolved.config_set is None
    assert resolved.arguments() == {spec.dest: getattr(args, spec.dest, None) for spec in REGISTRY.values()}
    entry = resolved.entries["quest-rules"]
    assert (entry.source, entry.mode, entry.path, entry.read_only) == (ConfigSource.CLI, KeyMode.EXPLICIT, override, True)
    assert entry.sha256 == digest(override.read_bytes()) and not entry.shadowed
    assert all(item.source is ConfigSource.DEFAULT and item.path is None
               for key, item in resolved.entries.items() if key != "quest-rules")
    assert resolved.overridden == ("quest-rules",)
    assert resolved.load_rules()["quest-rules"] == cli.load_quest_rules(override)
    assert not data.exists()


def test_example_files_are_never_applied(tmp_path):
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    for spec in REGISTRY.values():
        for name in spec.examples:
            shutil.copyfile(EXAMPLES / name, data / "config" / name)
            shutil.copyfile(EXAMPLES / name, data / "config" / f"{spec.key}.json")
    resolved = resolve(arguments(data))
    assert all(entry.source is ConfigSource.DEFAULT for entry in resolved.entries.values())
    assert set(resolved.arguments().values()) == {None}
    assert resolved.digest == config_identity({})
    defaults = resolved.effective()
    assert defaults["purchased-login-rules"]["enabled"] is False
    assert defaults == {key: spec.normalize(spec.default()) for key, spec in REGISTRY.items()}


def test_set_identity_is_deterministic_and_content_addressed(tmp_path):
    sets = ConfigSets(tmp_path / "data")
    layout = b"\xef\xbb\xbf{\r\n  \"reset_type_2\": \"window\",\r\n  \"reset_type_1\": \"never\"\r\n}\r\n"
    first = sets.create({"trade-rules": NEVER}, source="console")
    assert len(first.identifier) == SET_ID_LENGTH
    assert first.identifier == config_identity({"trade-rules": digest(NEVER)})[:SET_ID_LENGTH]
    assert sets.create({"trade-rules": layout}, source="import") == first
    assert sets.create({"trade-rules": json.loads(NEVER)}, source="console") == first
    assert sets.create({"trade-rules": MONTHLY}, source="console").identifier != first.identifier
    assert first.path("trade-rules").read_bytes() == NEVER
    manifest = json.loads((first.directory / MANIFEST_NAME).read_bytes())
    assert manifest["keys"]["trade-rules"] == {
        "mode": "explicit", "sha256": digest(NEVER),
        "content_version": REGISTRY["trade-rules"].content_version(cli.load_trade_rules(first.path("trade-rules")))}
    assert {key for key, entry in manifest["keys"].items() if entry == {"mode": "default"}} == set(REGISTRY) - {"trade-rules"}
    assert (manifest["source"], manifest["parent"], manifest["set"]) == ("console", None, first.identifier)
    assert datetime.fromisoformat(manifest["created_at"]).tzinfo is not None
    assert leftovers(sets) == []


def test_child_sets_inherit_revert_and_reach_the_empty_set(tmp_path):
    sets = ConfigSets(tmp_path / "data")
    base = sets.create({"quest-rules": example("quest-rules.example.json"),
                        "vote-rules": example("vote-revival.example.json")}, source="console")
    child = sets.create({"quest-rules": None, "trade-rules": NEVER}, base=base.identifier, source="console")
    assert set(child.explicit) == {"trade-rules", "vote-rules"}
    assert child.explicit["vote-rules"] == base.explicit["vote-rules"] and child.parent == base.identifier
    empty = sets.create({"vote-rules": None, "trade-rules": None}, base=child.identifier, source="console")
    assert dict(empty.explicit) == {} and empty.identifier == config_identity({})[:SET_ID_LENGTH]


def test_rejected_changes_leave_no_set_or_staging_behind(tmp_path):
    sets = ConfigSets(tmp_path / "data")
    error = error_of(lambda: sets.create({"comeback-login-rules": example("comeback-login-rules.example.json"),
                                          "quest-rules": b'{"gold_percent": -1}'}, source="console"))
    assert (error.code, error.key) == ("config_rules_invalid", "quest-rules")
    assert leftovers(sets) == [] and not (sets.sets.exists() and list(sets.sets.iterdir()))
    cases = (({"unknown-rules": b"{}"}, {}, "config_key_unknown"), ([("trade-rules", NEVER)], {}, "config_changes_invalid"),
             ({"trade-rules": NEVER}, {"source": "Console UI"}, "config_source_invalid"),
             ({"trade-rules": NEVER}, {"base": "0" * SET_ID_LENGTH}, "config_set_unknown"),
             ({"trade-rules": NEVER}, {"base": "../sets"}, "config_set_unknown"))
    for changes, options, code in cases:
        assert error_of(lambda: sets.create(changes, **{"source": "console", **options})).code == code
    assert leftovers(sets) == []


def test_tampered_sets_fail_closed(tmp_path):
    sets = ConfigSets(tmp_path / "data")
    created = sets.create({"trade-rules": NEVER}, source="console")
    sets.activate(created.identifier, 1, expected=None)
    manifest = created.directory / MANIFEST_NAME
    original = manifest.read_bytes()
    manifest.write_bytes(original.replace(b'"console"', b'"edited"'))
    assert sets.load(created.identifier).source == "edited"
    assert error_of(lambda: sets.read_active()).code == "config_pointer_invalid"
    manifest.write_bytes(original.replace(b'"rules": {', b'"retired-rules": {"mode": "default"}, "rules": {'))
    assert error_of(lambda: sets.load(created.identifier)).code == "config_set_invalid"
    manifest.write_bytes(original)
    created.path("trade-rules").write_bytes(MONTHLY)
    for action in (lambda: sets.load(created.identifier), lambda: sets.read_document(created, "trade-rules")):
        assert error_of(action).code == "config_set_invalid"
    error = error_of(lambda: resolve(arguments(tmp_path / "data")))
    assert (error.code, error.status) == ("config_pointer_invalid", 503)
    manifest.write_bytes(original.replace(digest(NEVER).encode(), digest(MONTHLY).encode()))
    error = error_of(lambda: sets.load(created.identifier))
    assert (error.code, error.status) == ("config_set_invalid", 503) and "identity" in str(error)
    manifest.write_bytes(original)
    created.path("trade-rules").unlink()
    for action in (lambda: sets.load(created.identifier), lambda: sets.read_document(created, "trade-rules")):
        assert (error_of(action).code, error_of(action).status) == ("config_set_invalid", 503)


def with_entry(manifest, key, entry):
    return manifest | {"keys": manifest["keys"] | {key: entry}}


@pytest.mark.parametrize("change", [
    lambda manifest: manifest | {"extra": 1},
    lambda manifest: {name: value for name, value in manifest.items() if name != "parent"},
    lambda manifest: manifest | {"set": "0" * SET_ID_LENGTH},
    lambda manifest: manifest | {"created_at": "yesterday"},
    lambda manifest: manifest | {"parent": "../sets"},
    lambda manifest: manifest | {"source": "Console UI"},
    lambda manifest: with_entry(manifest, "trade-rules", manifest["keys"]["trade-rules"] | {"content_version": ""}),
    lambda manifest: with_entry(manifest, "trade-rules", manifest["keys"]["trade-rules"] | {"content_version": "v" * 129}),
    lambda manifest: with_entry(manifest, "trade-rules", {"mode": "default"}),
    lambda manifest: with_entry(manifest, "rules", {"mode": "default", "sha256": "0" * 64}),
], ids=("extra", "missing", "identity", "created_at", "parent", "source", "empty_version", "long_version",
        "dropped_document", "malformed_default"))
def test_manifest_structure_and_provenance_fail_closed(change, tmp_path):
    sets = ConfigSets(tmp_path / "data")
    created = sets.create({"trade-rules": NEVER}, source="console")
    manifest = created.directory / MANIFEST_NAME
    manifest.write_bytes(json.dumps(change(json.loads(manifest.read_bytes()))).encode())
    error = error_of(lambda: sets.load(created.identifier))
    assert (error.code, error.status) == ("config_set_invalid", 503)


def tampered(data, case, limit):
    if case == "schema":
        return json.dumps(json.loads(data) | {"schema_version": 2}).encode()
    if case == "duplicate":
        return data.replace(b'  "schema_version": 1,', b'  "schema_version": 1,\n  "schema_version": 1,')
    return b"[" * (limit // 2) + b"]" * (limit // 2)


@pytest.mark.parametrize("case", ("schema", "duplicate", "nested"))
def test_pointer_and_manifest_parsing_is_strict(case, tmp_path):
    sets = ConfigSets(tmp_path / "data")
    created = sets.create({"trade-rules": NEVER}, source="console")
    pointer = sets.activate(created.identifier, 1, expected=None)
    active, manifest = sets.root / ACTIVE_POINTER, created.directory / MANIFEST_NAME
    original = active.read_bytes()
    active.write_bytes(tampered(original, case, config_sets.MAX_POINTER_BYTES))
    error = error_of(sets.read_active)
    assert (error.code, error.status) == ("config_pointer_invalid", 503)
    assert sets.activate(created.identifier, 1, expected=sets.pointer_digest()) == pointer
    assert active.read_bytes() == original
    manifest.write_bytes(tampered(manifest.read_bytes(), case, config_sets.MAX_MANIFEST_BYTES))
    error = error_of(lambda: sets.load(created.identifier))
    assert (error.code, error.status) == ("config_set_invalid", 503)
    assert error_of(sets.read_active).code == "config_pointer_invalid"


def test_activation_replaces_pointer_and_sequence_atomically(tmp_path):
    sets = ConfigSets(tmp_path / "data")
    first = sets.create({"trade-rules": NEVER}, source="console")
    second = sets.create({"trade-rules": MONTHLY}, source="console")
    pointer = sets.activate(first.identifier, 1, expected=None)
    assert sets.read_active() == pointer and pointer.digest == sets.pointer_digest()
    assert json.loads((sets.root / ACTIVE_POINTER).read_bytes()) == {
        "schema_version": 1, "set": first.identifier, "manifest_sha256": first.manifest_sha256, "sequence": 1}
    with sets.prepare_activation(second.identifier, 2, expected=pointer.digest) as pending:
        assert sets.read_active() == pointer and pending.path.exists()
        switched = pending.publish()
    assert sets.read_active() == switched and (switched.set_id, switched.sequence) == (second.identifier, 2)
    for action in (sets.activate, sets.prepare_activation):
        conflict = error_of(lambda: action(first.identifier, 3, expected=pointer.digest))
        assert (conflict.code, conflict.status) == ("config_pointer_conflict", 409) and leftovers(sets) == []
    regressed = error_of(lambda: sets.activate(first.identifier, 1, expected=switched.digest))
    assert (regressed.code, regressed.status) == ("config_sequence_regressed", 409)
    for sequence in (-1, True, 2 ** 63, "3"):
        assert error_of(lambda: sets.activate(first.identifier, sequence, expected=switched.digest)).code == (
            "config_sequence_invalid")
    restored = sets.activate(first.identifier, 2, expected=switched.digest)
    assert sets.read_active() == restored and restored.set_id == first.identifier
    assert leftovers(sets) == []


def test_failed_or_stale_publication_keeps_the_previous_pointer(tmp_path, monkeypatch):
    sets = ConfigSets(tmp_path / "data")
    first, second, third = (sets.create({"trade-rules": document}, source="console")
                            for document in (NEVER, MONTHLY, b"{}"))
    pointer = sets.activate(first.identifier, 1, expected=None)

    def interrupted(source, destination):
        raise PermissionError("replacement interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(config_sets, "atomic_replace", interrupted)
        with pytest.raises(PermissionError), sets.prepare_activation(second.identifier, 2, expected=pointer.digest) as pending:
            pending.publish()
    assert sets.read_active() == pointer and leftovers(sets) == []
    with sets.prepare_activation(second.identifier, 2, expected=pointer.digest) as stale:
        sets.activate(third.identifier, 2, expected=pointer.digest)
        assert error_of(stale.publish).code == "config_pointer_conflict"
    assert sets.read_active().set_id == third.identifier and leftovers(sets) == []


def test_publication_rechecks_the_staged_set(tmp_path):
    sets = ConfigSets(tmp_path / "data")
    first, second = (sets.create({"trade-rules": document}, source="console") for document in (NEVER, MONTHLY))
    pointer = sets.activate(first.identifier, 1, expected=None)
    manifest = second.directory / MANIFEST_NAME
    with sets.prepare_activation(second.identifier, 2, expected=pointer.digest) as pending:
        manifest.write_bytes(manifest.read_bytes().replace(b'"console"', b'"edited"'))
        error = error_of(pending.publish)
    assert (error.code, error.status) == ("config_set_invalid", 503)
    assert sets.read_active() == pointer and leftovers(sets) == []


def test_concurrent_switch_is_reported_busy(tmp_path):
    sets = ConfigSets(tmp_path / "data")
    created = sets.create({"trade-rules": NEVER}, source="console")
    with exclusive_file(sets.root / LOCK_NAME):
        error = error_of(lambda: sets.activate(created.identifier, 1, expected=None))
    assert (error.code, error.status) == ("config_pointer_busy", 409)
    assert sets.read_active() is None and leftovers(sets) == []


def test_unreadable_pointer_requires_an_explicit_repair(tmp_path):
    data = tmp_path / "data"
    sets = ConfigSets(data)
    created = sets.create({"trade-rules": NEVER}, source="console")
    (sets.root / ACTIVE_POINTER).write_bytes(b'{"set": "broken"}')
    for action in (sets.read_active, lambda: resolve(arguments(data))):
        assert error_of(action).code == "config_pointer_invalid"
    assert error_of(lambda: sets.activate(created.identifier, 0, expected=None)).code == "config_pointer_conflict"
    repaired = sets.activate(created.identifier, 0, expected=sets.pointer_digest())
    assert sets.read_active() == repaired
    missing = json.loads((sets.root / ACTIVE_POINTER).read_bytes()) | {"set": "f" * SET_ID_LENGTH}
    (sets.root / ACTIVE_POINTER).write_bytes(json.dumps(missing).encode())
    assert error_of(sets.read_active).code == "config_pointer_invalid"
    (sets.root / ACTIVE_POINTER).write_bytes(b"{}".ljust(config_sets.MAX_POINTER_BYTES + 1))
    for action in (sets.read_active, sets.pointer_digest, lambda: sets.activate(created.identifier, 0, expected=None)):
        assert (error_of(action).code, error_of(action).status) == ("config_pointer_invalid", 503)


def test_command_line_overrides_the_active_set(tmp_path):
    data, override = tmp_path / "data", tmp_path / "quest.json"
    sets = ConfigSets(data)
    created = sets.create({"quest-rules": example("quest-rules.example.json"), "trade-rules": NEVER}, source="console")
    pointer = sets.activate(created.identifier, 4, expected=None)
    without = resolve(arguments(data))
    assert without.digest[:SET_ID_LENGTH] == created.identifier and without.overridden == ()
    override.write_bytes(b'{"gold_percent": 50}')
    resolved = resolve(arguments(data, quest_rules=override))
    quest, trade = resolved.entries["quest-rules"], resolved.entries["trade-rules"]
    assert (quest.source, quest.path, quest.shadowed, quest.read_only) == (ConfigSource.CLI, override, True, True)
    assert (trade.source, trade.path, trade.read_only) == (ConfigSource.SET, created.path("trade-rules"), False)
    assert resolved.pointer == pointer and resolved.config_set == created and resolved.overridden == ("quest-rules",)
    assert resolved.arguments()["quest_rules"] == override and resolved.arguments()["vote_rules"] is None
    assert resolved.digest == config_identity({"quest-rules": digest(override.read_bytes()),
                                               "trade-rules": created.explicit["trade-rules"]})
    effective = resolved.effective()
    assert effective["quest-rules"]["gold_percent"] == 50
    assert effective["trade-rules"] == {"reset_type_1": "never", "reset_type_2": "window"}
    assert sets.read_active() == pointer and sets.load(created.identifier) == created
    missing = error_of(lambda: resolve(arguments(data, vote_rules=tmp_path / "missing.json")))
    assert (missing.code, missing.key) == ("config_override_invalid", "vote-rules")
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{}".ljust(REGISTRY["trade-rules"].max_bytes + 1))
    error = error_of(lambda: resolve(arguments(data, trade_rules=oversized)))
    assert (error.code, error.key) == ("config_override_invalid", "trade-rules")


def test_candidate_sets_resolve_like_the_active_set(tmp_path):
    data, override = tmp_path / "data", tmp_path / "quest.json"
    sets = ConfigSets(data)
    running = sets.create({"quest-rules": example("quest-rules.example.json"), "trade-rules": NEVER}, source="console")
    pointer = sets.activate(running.identifier, 3, expected=None)
    candidate = sets.create({"trade-rules": MONTHLY, "vote-rules": None}, base=running.identifier, source="console")
    override.write_bytes(b'{"gold_percent": 50}')
    resolved = sets.resolve_candidate(candidate.identifier, arguments(data, quest_rules=override))
    assert (resolved.pointer, resolved.config_set, resolved.overridden) == (None, candidate, ("quest-rules",))
    quest, trade = resolved.entries["quest-rules"], resolved.entries["trade-rules"]
    assert (quest.source, quest.path, quest.shadowed) == (ConfigSource.CLI, override, True)
    assert (trade.source, trade.path, trade.sha256) == (ConfigSource.SET, candidate.path("trade-rules"), digest(MONTHLY))
    assert resolved.effective()["trade-rules"]["reset_type_1"] == "month"
    assert resolve(arguments(data, quest_rules=override)).entries["trade-rules"].sha256 == digest(NEVER)
    plain = sets.resolve_candidate(candidate.identifier, arguments(data))
    assert plain.digest[:SET_ID_LENGTH] == candidate.identifier and plain.entries["quest-rules"].source is ConfigSource.SET
    assert sets.read_active() == pointer
    assert error_of(lambda: sets.resolve_candidate("0" * SET_ID_LENGTH, arguments(data))).code == "config_set_unknown"


@pytest.mark.parametrize("key", list(REGISTRY))
def test_every_key_round_trips_through_a_set(key, tmp_path):
    spec, sets = REGISTRY[key], ConfigSets(tmp_path / "data")
    for document in [example(name) for name in spec.examples] or [b"{}"]:
        created = sets.create({key: document}, source="console")
        assert sets.load(created.identifier) == created and list(created.explicit) == [key]
        assert sets.read_document(created, key) == spec.canonical(document)
        rules = spec.load(created.path(key))
        assert created.content_versions[key] == spec.content_version(rules)
        resolved = sets.resolve_candidate(created.identifier, arguments(tmp_path / "data"))
        assert resolved.arguments()[spec.dest] == created.path(key)
        assert resolved.effective()[key] == spec.normalize(rules)
    assert leftovers(sets) == []


def test_links_and_junctions_are_rejected(tmp_path, monkeypatch):
    data = tmp_path / "data"
    sets = ConfigSets(data)
    created = sets.create({"trade-rules": NEVER}, source="console")
    sets.activate(created.identifier, 1, expected=None)
    override = tmp_path / "linked" / "trade.json"
    override.parent.mkdir()
    override.write_bytes(MONTHLY)
    assert resolve(arguments(data, trade_rules=override)).entries["trade-rules"].sha256 == digest(MONTHLY)
    with monkeypatch.context() as patch:
        junction(patch, data / "config")
        error = error_of(lambda: ConfigSets(data))
        assert (error.code, error.status) == ("config_path_rejected", 503) and "junction" in str(error)
    with monkeypatch.context() as patch:
        junction(patch, sets.staging)
        error = error_of(lambda: sets.create({"trade-rules": MONTHLY}, source="console"))
        assert error.code == "config_path_rejected" and "junction" in str(error)
    with monkeypatch.context() as patch:
        junction(patch, sets.sets)
        error = error_of(sets.read_active)
        assert error.code == "config_pointer_invalid" and "junction" in str(error)
        assert error_of(lambda: sets.load(created.identifier)).code == "config_path_rejected"
    with monkeypatch.context() as patch:
        junction(patch, override.parent)
        error = error_of(lambda: resolve(arguments(data, trade_rules=override)))
        assert error.code == "config_override_invalid" and "junction" in str(error)
    assert len(list(sets.sets.iterdir())) == 1 and leftovers(sets) == []


@pytest.mark.skipif(os.name != "nt", reason="Directory junctions are a Windows feature")
def test_real_directory_junction_is_rejected(tmp_path):
    import _winapi

    data = tmp_path / "data"
    sets = ConfigSets(data)
    created = sets.create({"trade-rules": NEVER}, source="console")
    sets.activate(created.identifier, 1, expected=None)
    moved = tmp_path / "moved-sets"
    sets.sets.rename(moved)
    _winapi.CreateJunction(str(moved), str(sets.sets))
    try:
        assert sets.sets.is_junction()
        error = error_of(sets.read_active)
        assert error.code == "config_pointer_invalid" and "junction" in str(error)
        with pytest.raises(ValueError, match="junction"):
            sets.create({"trade-rules": MONTHLY}, source="console")
        assert [path.name for path in moved.iterdir()] == [created.identifier]
    finally:
        os.rmdir(sets.sets)
    moved.rename(sets.sets)
    assert sets.read_active().set_id == created.identifier


def test_symlinked_pointer_is_rejected(tmp_path):
    sets = ConfigSets(tmp_path / "data")
    created = sets.create({"trade-rules": NEVER}, source="console")
    sets.activate(created.identifier, 1, expected=None)
    target = tmp_path / "pointer.json"
    target.write_bytes((sets.root / ACTIVE_POINTER).read_bytes())
    (sets.root / ACTIVE_POINTER).unlink()
    try:
        (sets.root / ACTIVE_POINTER).symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")
    with pytest.raises(ValueError, match="Links"):
        sets.read_active()


def test_describe_reports_the_console_fields(tmp_path):
    data, broken = tmp_path / "data", tmp_path / "vote.json"
    sets = ConfigSets(data)
    created = sets.create({"rules": example("local-rules.example.json"), "quest-rules": b'{"gold_percent": 75}'},
                          source="console")
    sets.activate(created.identifier, 1, expected=None)
    broken.write_bytes(b'{"enabled": "yes"}')
    description = sets.describe(arguments(data, vote_rules=broken))
    assert description["directory"] == str(sets.root)
    assert description["active"]["set"] == description["set"]["id"] == created.identifier
    assert description["set"]["explicit"] == ["quest-rules", "rules"] and description["overridden"] == ["vote-rules"]
    keys = {entry["key"]: entry for entry in description["keys"]}
    assert list(keys) == list(REGISTRY)
    quest = keys["quest-rules"]
    assert (quest["source"], quest["mode"], quest["read_only"], quest["error"]) == ("set", "explicit", False, None)
    assert quest["effective"]["gold_percent"] == 75 and quest["default"]["gold_percent"] == 100
    assert quest["field_semantics"] == {"charisma_recovery_seconds": "time_accumulating"}
    assert quest["content_version"] == REGISTRY["quest-rules"].content_version(cli.load_quest_rules(created.path("quest-rules")))
    assert keys["vote-rules"]["error"]["code"] == "config_rules_invalid" and keys["vote-rules"]["read_only"] is True
    assert keys["event-pass-revival"]["effective"] is None and keys["event-pass-revival"]["source"] == "default"
    assert "bootstrap_token" not in keys["rules"]["effective"] and "bootstrap_token" not in keys["rules"]["default"]
    rendered = json.dumps(description, ensure_ascii=False)
    assert REGISTRY["rules"].default().bootstrap_token not in rendered
    assert json.loads(rendered) == description


def test_deeply_nested_command_line_files_are_reported_not_raised(tmp_path):
    data, deep = tmp_path / "data", tmp_path / "daily.json"
    depth = REGISTRY["daily-gift-rules"].max_bytes // 2
    deep.write_bytes(b"[" * depth + b"]" * depth)
    args = arguments(data, daily_gift_rules=deep)
    keys = {entry["key"]: entry for entry in ConfigSets(data).describe(args)["keys"]}
    assert keys["daily-gift-rules"]["error"]["code"] == "config_nesting_too_deep"
    assert keys["daily-gift-rules"]["read_only"] is True and keys["daily-gift-rules"]["effective"] is None
    assert error_of(lambda: resolve(args).effective()).code == "config_nesting_too_deep"
    assert not data.exists()


def test_set_documents_are_the_canonical_bytes_the_loaders_read(tmp_path):
    sets = ConfigSets(tmp_path / "data")
    raw = b"\xef\xbb\xbf" + example("event-pass-revival.example.json")
    created = sets.create({"event-pass-revival": raw}, source="console")
    stored = created.path("event-pass-revival").read_bytes()
    assert stored == serialize(json.loads(raw.decode("utf-8-sig"))).encode("utf-8")
    assert sets.read_document(created, "event-pass-revival") == stored
    spec = REGISTRY["event-pass-revival"]
    assert spec.normalize(spec.load(created.path("event-pass-revival"))) == spec.normalize(
        spec.load(EXAMPLES / "event-pass-revival.example.json"))

import argparse
from dataclasses import fields, replace
import hashlib
import inspect
import json
from pathlib import Path
import re

import pytest

from aigis_ps import cli
from aigis_ps.command_text_overlay import CommandTextOverrides
from aigis_ps.config_registry import (REGISTRY, ConfigError, ConfigKey, ConfigRegistry, Normalizer, TimeSemantics,
                                      parse_document, resolve_reference)
from aigis_ps.local_io import MAX_JSON_DEPTH
from aigis_ps.storage import serialize


PROJECT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT / "config"
CONFIG_COMMANDS = ("serve", "doctor")
NO_DEFAULT_RULES = frozenset({"event-pass-revival", "mission-windows", "command-text-overrides"})
# Config-set manifests on disk list these keys, and a manifest naming an unregistered key no longer loads.
STORED_SET_KEYS = frozenset({"rules", "quest-rules", "training-rules", "silver-house-rules", "evolution-rules",
                             "recovery-rules", "trade-rules", "ticket-rules", "item-ticket-rules", "login-rules",
                             "event-pass-rules", "event-pass-revival", "event-login-rules", "comeback-login-rules",
                             "purchased-login-rules", "daily-gift-rules", "vote-rules", "vote-cooking-rules",
                             "entrust-rules", "mission-windows", "tower-rules", "subjugation-rules",
                             "serial-code-rules", "letstd-rewards"})
# Battles read these rules, directly or through the presents they mail, so each lists main_quests as a consumer.
MAIN_QUEST_RULE_KEYS = frozenset({"quest-rules", "item-ticket-rules", "vote-rules", "mission-windows", "tower-rules",
                                  "subjugation-rules"})
# Each listed domain fingerprints the rule itself or a service version that embeds it, receipt digests included.
TRANSITIVE_DOMAINS = {
    "quest-rules": ("entrust", "login_rewards", "main_quests", "recovery", "sugoroku"),
    "training-rules": ("entrust", "evolution", "fame", "missions", "skin_shop", "training", "unit_detail"),
    "silver-house-rules": ("entrust", "evolution", "fame", "missions", "silver_house", "skin_shop", "training",
                           "unit_detail"),
    "evolution-rules": ("entrust", "evolution", "fame", "missions", "skin_shop", "trades"),
    "recovery-rules": ("login_rewards", "recovery"),
    "ticket-rules": ("comeback_logins", "daily_gifts", "entrust", "event_logins", "login_rewards", "packages",
                     "purchased_logins", "rank_rewards", "serial_codes", "summon_tickets"),
    "item-ticket-rules": ("comeback_logins", "daily_gifts", "entrust", "event_logins", "event_passes", "fame",
                          "item_summon_tickets", "letstd", "login_rewards", "main_quests", "missions", "packages",
                          "purchased_logins", "rank_rewards", "serial_codes", "skin_shop"),
    "event-pass-rules": ("event_passes", "rank_rewards"),
    "event-pass-revival": ("entrust", "event_pass_revival", "event_passes", "fame", "missions", "native_cdn",
                           "packages", "rank_rewards", "skin_shop"),
    "event-login-rules": ("comeback_logins", "event_logins", "login_rewards", "native_cdn", "packages",
                          "purchased_logins", "rank_rewards"),
    "comeback-login-rules": ("comeback_logins", "login_rewards", "rank_rewards"),
    "purchased-login-rules": ("login_rewards", "native_cdn", "packages", "purchased_logins", "rank_rewards"),
    "daily-gift-rules": ("daily_gifts", "login_rewards", "rank_rewards"),
    "vote-rules": ("entrust", "login_rewards", "main_quests", "rank_rewards", "vote_assault_notifications",
                   "vote_cooking", "vote_cooking_notifications", "vote_expiry_notifications", "votes"),
    "vote-cooking-rules": ("login_rewards", "rank_rewards", "vote_cooking", "vote_cooking_notifications", "votes"),
    "mission-windows": ("entrust", "main_quests", "mission_windows", "native_cdn", "subjugation"),
    "command-text-overrides": ("entrust", "native_cdn"),
    "letstd-rewards": ("letstd", "login_rewards", "rank_rewards"),
}
BOM = b"\xef\xbb\xbf"


def script_flags(path, variable):
    pattern = rf"if \(\$(\w+File)\) \{{ \${variable} \+= @\('(--[a-z-]+)', \$\1\) \}}"
    return dict(re.findall(pattern, path.read_text(encoding="utf-8")))


def sample(spec):
    return (EXAMPLES / spec.examples[0]).read_bytes() if spec.examples else b"{}"


def staging(root, name="staging"):
    directory = root / name
    directory.mkdir(parents=True)
    return directory


def error_of(action):
    with pytest.raises(ConfigError) as caught:
        action()
    return caught.value


def parsed_commands(monkeypatch, data_dir, *options):
    parse, seen = argparse.ArgumentParser.parse_args, []

    class Parsed(Exception):
        pass

    def capture(parser, *arguments, **keywords):
        seen.append(parse(parser, *arguments, **keywords))
        raise Parsed

    with monkeypatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_args", capture)
        for command in CONFIG_COMMANDS:
            with pytest.raises(Parsed):
                cli.main([command, "--data-dir", str(data_dir), *options])
    return seen


def test_registry_keeps_keys_in_command_line_order(tmp_path, monkeypatch):
    order = [spec.dest for spec in REGISTRY.values()]
    for args in parsed_commands(monkeypatch, tmp_path / "data"):
        # argparse adds defaults in declaration order, so the namespace keeps the command line order.
        assert [name for name in vars(args) if name in order] == order
    assert all(spec.cli_flag == "--" + key for key, spec in REGISTRY.items())


def test_named_keys_stay_registered():
    assert STORED_SET_KEYS | NO_DEFAULT_RULES <= set(REGISTRY)


@pytest.mark.parametrize("key", list(REGISTRY))
def test_references_resolve_and_defaults_match_the_existing_loader(key):
    spec = REGISTRY[key]
    loader, rules_type = resolve_reference(spec.loader), spec.rules_type()
    assert callable(loader) and isinstance(rules_type, type)
    assert type(spec.max_bytes) is int and spec.max_bytes > 0
    default = spec.default()
    assert (default is None) == (key in NO_DEFAULT_RULES)
    assert default is None or isinstance(default, rules_type)
    normalized = spec.normalize(default)
    assert normalized == spec.normalize(loader(None))
    assert normalized is None or json.loads(json.dumps(normalized)) == normalized
    assert spec.content_version(default) == spec.content_version(loader(None))


@pytest.mark.parametrize("key", list(REGISTRY))
def test_documents_stage_through_the_existing_loader(key, tmp_path):
    spec = REGISTRY[key]
    for index, raw in enumerate([(EXAMPLES / name).read_bytes() for name in spec.examples] or [b"{}"]):
        source = tmp_path / f"source-{index}.json"
        source.write_bytes(raw)
        staged = spec.stage(raw, staging(tmp_path, f"staging-{index}"), root=tmp_path)
        assert staged.path.read_bytes() == staged.data == serialize(json.loads(raw)).encode("utf-8")
        assert staged.sha256 == hashlib.sha256(staged.data).hexdigest()
        assert isinstance(staged.rules, spec.rules_type())
        assert staged.normalized == spec.normalize(spec.load(source))
        assert staged.content_version == spec.content_version(spec.load(source))
        own = hasattr(spec.rules_type(), "content_version")
        assert staged.content_version == (staged.rules.content_version if own else
                                          hashlib.sha256(serialize(staged.normalized).encode("utf-8")).hexdigest())


@pytest.mark.parametrize("key", list(REGISTRY))
def test_size_limits_match_the_existing_loader_boundary(key, tmp_path):
    spec = REGISTRY[key]
    base, padded = tmp_path / "base.json", tmp_path / "padded.json"
    base.write_bytes(sample(spec))
    exact = sample(spec).ljust(spec.max_bytes)
    padded.write_bytes(exact)
    assert spec.normalize(spec.load(padded)) == spec.normalize(spec.load(base))
    assert spec.canonical(exact) == spec.canonical(sample(spec))
    padded.write_bytes(exact + b" ")
    assert error_of(lambda: spec.load(padded)).code == "config_rules_invalid"
    assert error_of(lambda: spec.canonical(exact + b" ")).code == "config_too_large"


@pytest.mark.parametrize("key", list(REGISTRY))
def test_loader_recursion_reports_the_nesting_limit(key, tmp_path):
    spec, deep = REGISTRY[key], tmp_path / "deep.json"
    depth = spec.max_bytes // 2
    deep.write_bytes(b"[" * depth + b"]" * depth)
    error = error_of(lambda: spec.load(deep))
    assert (error.code, error.key) == ("config_nesting_too_deep", key)


@pytest.mark.parametrize("key", list(REGISTRY))
def test_command_line_flag_reaches_the_loader_argument(key, tmp_path, monkeypatch):
    spec, path = REGISTRY[key], tmp_path / "rules.json"
    seen = parsed_commands(monkeypatch, tmp_path / "data", spec.cli_flag, str(path))
    assert [(args.command, getattr(args, spec.dest)) for args in seen] == [(name, path) for name in CONFIG_COMMANDS]
    assert all(getattr(args, other.dest) is None for args in seen for other in REGISTRY.values() if other is not spec)
    assert not (tmp_path / "data").exists()


def test_powershell_parameters_match_the_server_script():
    script = PROJECT / "start-server.ps1"
    flags = script_flags(script, "serverArguments")
    declared = set(re.findall(r"\[string\]\$(\w+File) = ''", script.read_text(encoding="utf-8")))
    assert {spec.ps_parameter: spec.cli_flag for spec in REGISTRY.values()} == flags
    assert {spec.ps_parameter for spec in REGISTRY.values()} == declared


def test_resource_hints_match_the_client_preparation_script():
    flags = script_flags(PROJECT / "client/prepare-client.ps1", "clientArguments")
    hinted = {spec.ps_parameter: spec.cli_flag for spec in REGISTRY.values() if spec.resource_hint}
    assert hinted == flags
    assert all(spec.resource_hint for spec in REGISTRY.values() if spec.time_semantics is TimeSemantics.RESOURCE)


def test_consumer_domains_name_readiness_domains():
    names = set(re.findall(r'result\["(\w+)"\]', inspect.getsource(cli.service_readiness)))
    names |= set(re.findall(r'"(\w+)":', inspect.getsource(cli.readiness)))
    assert {domain for spec in REGISTRY.values() for domain in spec.domains} <= names


def test_rules_read_by_battles_name_the_main_quests_domain():
    assert REGISTRY["item-ticket-rules"].domains == TRANSITIVE_DOMAINS["item-ticket-rules"]
    assert {key for key, spec in REGISTRY.items() if "main_quests" in spec.domains} == MAIN_QUEST_RULE_KEYS


@pytest.mark.parametrize("key", sorted(TRANSITIVE_DOMAINS))
def test_consumer_domains_include_every_fingerprint_that_embeds_the_rule(key):
    assert REGISTRY[key].domains == TRANSITIVE_DOMAINS[key]


def test_command_text_overrides_register_their_flag_loader_and_example():
    spec = REGISTRY["command-text-overrides"]
    assert (spec.cli_flag, spec.ps_parameter) == ("--command-text-overrides", "CommandTextOverridesFile")
    assert spec.rules_type() is CommandTextOverrides and spec.default() is None
    assert spec.examples == ("command-text-overrides.example.json",)
    assert spec.resource_hint and spec.time_semantics is TimeSemantics.RESOURCE
    loaded = spec.load(EXAMPLES / spec.examples[0])
    assert loaded.count == 1 and spec.normalize(loaded) == loaded.to_dict()
    assert spec.content_version(loaded) == loaded.content_version


def test_login_rules_field_semantics_cover_both_cooking_fields():
    assert dict(REGISTRY["login-rules"].field_semantics) == {"cooking_item_id": TimeSemantics.ELIGIBILITY,
                                                             "cooking_sender": TimeSemantics.ELIGIBILITY}


@pytest.mark.parametrize("key", list(REGISTRY))
def test_normalization_and_field_metadata_follow_the_rule_class(key):
    spec = REGISTRY[key]
    rules_type = spec.rules_type()
    assert hasattr(rules_type, "to_dict") == (spec.normalizer is Normalizer.TO_DICT)
    names = {item.name for item in fields(rules_type)}
    assert set(spec.field_semantics) <= names and set(spec.secret_fields) <= names


def test_every_shipped_example_is_registered_once():
    claimed = [name for spec in REGISTRY.values() for name in spec.examples]
    assert len(claimed) == len(set(claimed))
    assert set(claimed) == {path.name for path in EXAMPLES.glob("*.example.json")}


def test_time_semantics_cover_every_category_and_flag_time_accumulation():
    used = {spec.time_semantics for spec in REGISTRY.values()}
    used |= {value for spec in REGISTRY.values() for value in spec.field_semantics.values()}
    assert used == set(TimeSemantics)
    quests = REGISTRY["quest-rules"]
    before = quests.normalize(quests.default())
    assert quests.changed_semantics(before, dict(before)) == frozenset()
    assert quests.changed_semantics(before, {**before, "charisma_recovery_seconds": 120}) == {
        TimeSemantics.TIME_ACCUMULATING}
    assert quests.changed_semantics(before, {**before, "gold_percent": 50}) == {TimeSemantics.FROZEN_PER_OPERATION}
    revival = REGISTRY["event-pass-revival"]
    enabled = revival.normalize(revival.load(EXAMPLES / "event-pass-revival.example.json"))
    assert revival.changed_semantics(None, enabled) == {TimeSemantics.RESOURCE}


@pytest.mark.parametrize("raw,pointer", [
    (b'{"enabled": true, "enabled": false}', "/enabled"),
    (b'{"revivals": {"8": {"start_date": "2026-11-01", "end_date": "2026-11-02"},'
     b' "8": {"start_date": "2026-11-03", "end_date": "2026-11-04"}}}', "/revivals/8"),
    (b'{"gift_ids": [1], "notes": [{"a/b~c": 1, "a/b~c": 2}]}', "/notes/0/a~1b~0c"),
])
def test_duplicate_keys_report_their_json_pointer(raw, pointer, tmp_path):
    error = error_of(lambda: REGISTRY["daily-gift-rules"].stage(raw, staging(tmp_path), root=tmp_path))
    assert (error.code, error.key, error.pointer) == ("config_duplicate_key", "daily-gift-rules", pointer)
    assert pointer in str(error)
    assert not list((tmp_path / "staging").iterdir())


def test_byte_order_mark_and_layout_do_not_change_the_normalized_document():
    spec = REGISTRY["vote-cooking-rules"]
    compact = b'{"copies":2,"enabled":true,"minimum_votes":3}'
    layout = BOM + b'{\r\n  "minimum_votes": 3,\r\n  "enabled": true,\r\n  "copies": 2\r\n}\r\n'
    assert spec.canonical(layout) == spec.canonical(compact) == compact
    assert spec.canonical({"enabled": True, "copies": 2, "minimum_votes": 3}) == compact


@pytest.mark.parametrize("raw,code,pointer", [
    (b"[1]", "config_not_object", ""),
    (b"", "config_json_invalid", None),
    (b"{", "config_json_invalid", None),
    (b'{"a": NaN}', "config_number_invalid", "/a"),
    (b'{"a": [Infinity]}', "config_number_invalid", "/a/0"),
    (b'{"a": 1e400}', "config_number_invalid", "/a"),
    (b"\xff\xfe{\x00}\x00", "config_encoding_invalid", None),
    (b'{"a": "\\ud800"}', "config_encoding_invalid", "/a"),
    (b'{"\\udfff": 1}', "config_encoding_invalid", "/\udfff"),
    (b'{"a":' * MAX_JSON_DEPTH + b"{}" + b"}" * MAX_JSON_DEPTH, "config_nesting_too_deep", "/a" * MAX_JSON_DEPTH),
    (b"[" * 100000 + b"]" * 100000, "config_nesting_too_deep", None),
], ids=("array", "empty", "truncated", "nan", "infinity", "overflow", "utf16", "surrogate_value", "surrogate_key",
        "nested_objects", "recursion"))
def test_strict_parse_rejects_unsafe_documents(raw, code, pointer):
    error = error_of(lambda: parse_document(raw, limit=len(raw) + 1, key="quest-rules"))
    assert (error.code, error.key, error.pointer) == (code, "quest-rules", pointer)


def test_strict_parse_reads_only_raw_bytes():
    for document in ("{}", {"a": 1}, memoryview(b"{}")):
        assert error_of(lambda: parse_document(document, limit=16, key="quest-rules")).code == "config_document_invalid"
    assert parse_document(bytearray(b"{}"), limit=2) == {}


def test_nesting_limit_accepts_the_deepest_permitted_document():
    raw = b'{"a":' * (MAX_JSON_DEPTH - 1) + b"{}" + b"}" * (MAX_JSON_DEPTH - 1)
    assert parse_document(raw, limit=len(raw))["a"]["a"]


def test_oversized_documents_are_rejected_before_decoding(tmp_path):
    spec = REGISTRY["vote-cooking-rules"]
    error = error_of(lambda: spec.stage(b"\xff" * (spec.max_bytes + 1), staging(tmp_path), root=tmp_path))
    assert error.code == "config_too_large"


def test_normalized_documents_must_still_fit_the_limit(tmp_path):
    spec, directory = REGISTRY["vote-cooking-rules"], staging(tmp_path)
    exponents = b'{"minimum_votes": [' + b",".join([b"1e9"] * (spec.max_bytes // 8)) + b"]}"
    assert len(exponents) <= spec.max_bytes < len(serialize(json.loads(exponents)).encode("utf-8"))
    for document in (exponents, {"minimum_votes": ["あ" * spec.max_bytes]}):
        error = error_of(lambda: spec.stage(document, directory, root=tmp_path))
        assert (error.code, error.key) == ("config_too_large", "vote-cooking-rules")
    assert not list(directory.iterdir())


def test_bootstrap_token_is_rejected_and_never_reported(tmp_path):
    spec = REGISTRY["rules"]
    for document in (b'{"bootstrap_token": "chosen"}', {"money": 5, "bootstrap_token": "chosen"}):
        error = error_of(lambda: spec.stage(document, staging(tmp_path, "staging-" + type(document).__name__),
                                            root=tmp_path))
        assert (error.code, error.pointer) == ("config_secret_field", "/bootstrap_token")
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]
    default = spec.normalize(spec.default())
    assert "bootstrap_token" not in default and default["user_name"] == "Local Player"
    assert spec.default().bootstrap_token not in json.dumps(REGISTRY.describe(), ensure_ascii=False)


def test_loader_rejection_removes_the_staged_document(tmp_path):
    directory = staging(tmp_path)
    for raw in (b'{"gold_percent": -1}', b'{"unknown": 1}'):
        error = error_of(lambda: REGISTRY["quest-rules"].stage(raw, directory, root=tmp_path))
        assert (error.code, error.key) == ("config_rules_invalid", "quest-rules")
    assert error_of(lambda: REGISTRY["event-pass-revival"].stage(b"{}", directory, root=tmp_path)).code == (
        "config_rules_invalid")
    assert not list(directory.iterdir())


def test_mapping_documents_share_the_same_validation(tmp_path):
    spec = REGISTRY["item-ticket-rules"]
    staged = spec.stage({"enabled_pools": (428, 350)}, staging(tmp_path), root=tmp_path)
    assert staged.data == b'{"enabled_pools":[428,350]}'
    assert staged.normalized == {"enabled_pools": [350, 428]}
    for document, code in (({1: True}, "config_document_invalid"), ({"enabled_pools": {350}}, "config_document_invalid"),
                           ({"enabled_pools": [float("nan")]}, "config_number_invalid"), ([350], "config_not_object"),
                           ("{}", "config_not_object")):
        assert error_of(lambda: spec.canonical(document)).code == code


def test_staging_stays_inside_its_root(tmp_path):
    outside = staging(tmp_path, "outside")
    error = error_of(lambda: REGISTRY["trade-rules"].stage(b"{}", outside, root=tmp_path / "root"))
    assert (error.code, error.status) == ("config_path_rejected", 503) and "leaves" in str(error)
    assert not list(outside.iterdir())


def test_rules_must_expose_a_text_content_version():
    spec = REGISTRY["vote-rules"]
    rules = spec.default()

    class Versioned(type(rules)):
        content_version = None

    assert spec.content_version(rules) == rules.content_version
    with pytest.raises(TypeError, match="content version"):
        spec.content_version(Versioned())


def test_unknown_keys_are_rejected():
    for key in ("unknown-rules", None, ["rules"]):
        error = error_of(lambda: REGISTRY.require(key))
        assert error.code == "config_key_unknown"
    assert REGISTRY.require("rules") is REGISTRY["rules"]


def test_registry_rejects_ambiguous_or_malformed_entries():
    spec = REGISTRY["trade-rules"]
    with pytest.raises(ValueError, match="unique"):
        ConfigRegistry((spec, replace(spec, key="other-rules", ps_parameter="OtherRulesFile")))
    for change in ({"domains": ("trades", "evolution")}, {"loader": "cli.load_trade_rules"},
                   {"size_limit": 0}, {"field_semantics": {"Reset": TimeSemantics.ELIGIBILITY}},
                   {"examples": ("trade-rules.json",)}, {"cli_flag": "-trade-rules"}, {"domains": "trades"},
                   {"secret_fields": "reset_type_1"}, {"resource_hint": 1}):
        with pytest.raises(ValueError):
            replace(spec, **change)
    assert isinstance(replace(spec, field_semantics={"reset_type_1": "eligibility"}), ConfigKey)

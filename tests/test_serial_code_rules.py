from dataclasses import FrozenInstanceError
from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from aigis_ps.config_registry import REGISTRY, ConfigError, ConfigKey, ConfigRegistry, Normalizer, TimeSemantics
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.serial_code_rules import (
    CODE_GROUP, MAX_CODE_LENGTH, MAX_CODES, MAX_RULE_BYTES, SCHEMA_VERSION, SerialCode, SerialCodeRules, canonical_code,
    entered_form, load_serial_code_rules)
from aigis_ps.storage import serialize


EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "serial-code-rules.example.json"
CRYSTAL = {"kind": "free_crystal", "amount": 5, "description": "シリアルコード特典"}
UNIT = {"kind": "unit", "template_id": 2, "count": 1, "description": "シリアルコード特典"}
WINDOW = {"start_date": "2026-11-03", "end_date": "2026-11-05"}
# Mirrors the console registry entry; a registered serial-code-rules key must describe the same loader and limits.
PROPOSED_KEY = ConfigKey("serial-code-rules", "--serial-code-rules", "SerialCodeRulesFile",
                         rules_class="serial_code_rules:SerialCodeRules",
                         loader="serial_code_rules:load_serial_code_rules", normalizer=Normalizer.TO_DICT,
                         domains=("serial_codes",), time_semantics=TimeSemantics.ELIGIBILITY,
                         size_limit="serial_code_rules:MAX_RULE_BYTES", examples=("serial-code-rules.example.json",))


def code(value="abcde", **fields):
    return {"code": value, "rewards": [CRYSTAL], **fields}


def write(path, value, *, prefix=b""):
    path.write_bytes(prefix + json.dumps(value, ensure_ascii=False).encode("utf-8"))
    return path


def test_defaults_hold_no_codes():
    rules = SerialCodeRules()
    assert rules.codes == () and rules.get("abcde") is None
    assert rules.to_dict() == {"schema_version": SCHEMA_VERSION, "codes": []}
    assert load_serial_code_rules(None) == rules
    assert len(rules.content_version) == 64 and rules.content_version == SerialCodeRules().content_version


def test_shipped_example_grants_nothing():
    raw = EXAMPLE.read_bytes()
    assert b"\r" not in raw and json.loads(raw) == {"schema_version": 1, "codes": []}
    assert load_serial_code_rules(EXAMPLE) == SerialCodeRules()


def test_codes_are_stored_canonically_and_sorted():
    rules = SerialCodeRules(codes=[code("Zz-9"), code("AbCdE-fGhIj-K", limit=2), code("a-b-c")])
    assert [entry.code for entry in rules.codes] == ["abc", "abcdefghijk", "zz9"]
    assert rules.get("abcdefghijk").limit == 2 and rules.get("zz9").limit == 1
    assert rules.get("ABCDEFGHIJK") is None and rules.get("abcde-fghij-k") is None
    assert canonical_code("ABCDE-FGHIJ-KLMNO-PQR") == "abcdefghijklmnopqr"


@pytest.mark.parametrize("raw,entered", [
    ("a", "a"), ("abcd", "abcd"), ("abcde", "abcde"), ("abcdef", "abcde-f"), ("abcdefghij", "abcde-fghij"),
    ("abcdefghijk", "abcde-fghij-k"), ("abcdefghijklmnop", "abcde-fghij-klmno-p"),
    ("abcdefghijklmnopqr", "abcde-fghij-klmno-pqr")])
def test_entered_form_follows_the_native_grouping(raw, entered):
    assert CODE_GROUP == 5 and MAX_CODE_LENGTH == 18
    assert entered_form(raw) == entered and entered.replace("-", "") == raw


@pytest.mark.parametrize("value", ["", "-", "---", "a" * (MAX_CODE_LENGTH + 1), "abc_d", "abc d", "abc\n", "ａｂｃ",
                                   "\u212a", "\u00e9", 123, None, True, b"abc"])
def test_codes_the_native_keypad_cannot_type_are_rejected(value):
    with pytest.raises(ValueError):
        canonical_code(value)
    with pytest.raises(ValueError):
        SerialCodeRules(codes=[code(value)])


def test_codes_must_stay_distinct_without_hyphens_and_case():
    with pytest.raises(ValueError):
        SerialCodeRules(codes=[code("ABC"), code("a-b-c")])
    with pytest.raises(ValueError):
        SerialCodeRules(codes=[code("abc"), code("abc")])


def test_limits_and_single_use():
    entry = SerialCode("abc", [CRYSTAL])
    assert (entry.limit, entry.single_use, entry.window) == (1, False, None)
    assert SerialCode("abc", [CRYSTAL], limit=0x7FFFFFFF).limit == 0x7FFFFFFF
    assert SerialCode("abc", [CRYSTAL], single_use=True).single_use is True
    for limit in (0, -1, True, 1.0, "1", None, 2 ** 31):
        with pytest.raises(ValueError):
            SerialCode("abc", [CRYSTAL], limit=limit)
    for flag in (1, 0, "true", None):
        with pytest.raises(ValueError):
            SerialCode("abc", [CRYSTAL], single_use=flag)
    with pytest.raises(ValueError):
        SerialCode("abc", [CRYSTAL], limit=2, single_use=True)


def test_windows_are_inclusive_business_day_ranges():
    entry = SerialCode("abc", [CRYSTAL], window=WINDOW)
    start, end = date(2026, 11, 3), date(2026, 11, 5)
    assert [entry.open_on(start + timedelta(days=offset)) for offset in range(-1, 4)] == [False, True, True, True, False]
    assert SerialCode("abc", [CRYSTAL], window={"start_date": "2026-11-03", "end_date": "2026-11-03"}).open_on(start)
    assert SerialCode("abc", [CRYSTAL]).open_on(date(1, 1, 1)) and SerialCode("abc", [CRYSTAL]).open_on(end)
    assert entry.to_dict()["window"] == WINDOW
    with pytest.raises(TypeError):
        entry.window["start_date"] = "2026-11-01"


@pytest.mark.parametrize("window", [
    {"start_date": "2026-11-05", "end_date": "2026-11-03"}, {"start_date": "2026-11-3", "end_date": "2026-11-05"},
    {"start_date": "20261103", "end_date": "2026-11-05"}, {"start_date": "2026-11-03T00:00:00", "end_date": "2026-11-05"},
    {"start_date": "2026-11-03"}, {**WINDOW, "end_time": "04:00"}, {"start_date": None, "end_date": "2026-11-05"},
    {"start_date": 20261103, "end_date": "2026-11-05"}, ["2026-11-03", "2026-11-05"], "2026-11-03", True])
def test_malformed_windows_are_rejected(window):
    with pytest.raises(ValueError):
        SerialCode("abc", [CRYSTAL], window=window)


def test_rewards_are_flat_read_only_objects_handed_out_as_copies():
    entry = SerialCode("abc", (CRYSTAL, UNIT))
    first = entry.reward_list()
    assert first == [CRYSTAL, UNIT] and all(type(reward) is dict for reward in first)
    first[0]["amount"] = 999
    first.append(UNIT)
    assert entry.reward_list() == [CRYSTAL, UNIT]
    with pytest.raises(TypeError):
        entry.rewards[0]["amount"] = 1
    with pytest.raises(FrozenInstanceError):
        entry.limit = 2
    assert SerialCode("abc", [CRYSTAL] * MAX_PRESENTS).reward_list() == [CRYSTAL] * MAX_PRESENTS


@pytest.mark.parametrize("rewards", [
    [], (), [{}], ["free_crystal"], [None], [{"kind": True}], [{"amount": 1.5}], [{"amount": None}],
    [{"items": [1]}], [{"nested": {"a": 1}}], [{**CRYSTAL, "description": "\ud800"}], [{**CRYSTAL, "\udfff": 1}],
    [CRYSTAL] * (MAX_PRESENTS + 1), "free_crystal", CRYSTAL, None])
def test_malformed_rewards_are_rejected(rewards):
    with pytest.raises(ValueError):
        SerialCode("abc", rewards)


@pytest.mark.parametrize("entry", [{"code": "abc"}, {"rewards": [CRYSTAL]}, {**code(), "label": "x"},
                                   {**code(), "Code": "abc"}, ["abc", [CRYSTAL]], "abc", None])
def test_entries_name_only_the_known_fields(entry):
    with pytest.raises(ValueError):
        SerialCodeRules(codes=[entry])


def test_tables_are_bounded_sequences():
    entries = [code(f"c{index}") for index in range(MAX_CODES)]
    assert len(SerialCodeRules(codes=entries).codes) == MAX_CODES
    for codes in (entries + [code("overflow")], {"abc": code()}, None, "abc"):
        with pytest.raises(ValueError):
            SerialCodeRules(codes=codes)


def test_content_version_follows_every_field_but_not_the_written_form():
    base = SerialCodeRules(codes=[code("abcde"), code("fghij")])
    assert SerialCodeRules(codes=[code("F-G-H-I-J"), code("ABCDE")]).content_version == base.content_version
    variants = [SerialCodeRules(codes=[code("abcdf"), code("fghij")]),
                SerialCodeRules(codes=[code("abcde", limit=2), code("fghij")]),
                SerialCodeRules(codes=[code("abcde", single_use=True), code("fghij")]),
                SerialCodeRules(codes=[code("abcde", window=WINDOW), code("fghij")]),
                SerialCodeRules(codes=[{"code": "abcde", "rewards": [UNIT]}, code("fghij")]),
                SerialCodeRules(codes=[{"code": "abcde", "rewards": [{**CRYSTAL, "amount": 6}]}, code("fghij")]),
                SerialCodeRules(codes=[code("abcde")])]
    versions = {rules.content_version for rules in variants}
    assert base.content_version not in versions and len(versions) == len(variants)


def test_rule_file_roundtrip_byte_order_mark_and_minimal_forms(tmp_path):
    rules = SerialCodeRules(codes=[code("abcde-fghij", limit=3, window=WINDOW), code("one", single_use=True),
                                   {"code": "two", "rewards": [UNIT, CRYSTAL]}])
    assert load_serial_code_rules(write(tmp_path / "full.json", rules.to_dict())) == rules
    displayed = {"schema_version": 1, "codes": [code("ABCDE-FGHIJ", limit=3, window=WINDOW),
                                                code("ONE", single_use=True), {"code": "T-W-O", "rewards": [UNIT, CRYSTAL]}]}
    assert load_serial_code_rules(write(tmp_path / "bom.json", displayed, prefix=b"\xef\xbb\xbf")) == rules
    assert load_serial_code_rules(str(write(tmp_path / "minimal.json", {"schema_version": 1}))) == SerialCodeRules()


@pytest.mark.parametrize("text", [
    '{"schema_version":1,"schema_version":1}',
    '{"schema_version":1,"codes":[{"code":"abc","code":"abd","rewards":[{"kind":"gold","amount":1,"description":"x"}]}]}',
    '{"schema_version":1,"codes":[{"code":"abc","rewards":[{"kind":"gold","kind":"gold"}]}]}',
    '{"schema_version":1,"label":""}', '{"codes":[]}', '{"schema_version":2,"codes":[]}',
    '{"schema_version":true,"codes":[]}', '{"schema_version":"1","codes":[]}', '{"schema_version":1,"codes":null}',
    '{"schema_version":1,"codes":{}}', '[]', '"codes"',
    '{"schema_version":1,"codes":[{"code":"abc","rewards":[{"kind":"gold","amount":NaN,"description":"x"}]}]}',
    '{"schema_version":1,"codes":[{"code":"abc","rewards":[{"amount":1e400}]}]}', '{"schema_version":1',
    '{"schema_version":1,"codes":[{"code":"abc","rewards":[{"kind":"gold","amount":1,"description":"\\ud800"}]}]}'])
def test_rule_file_rejects_malformed_documents(tmp_path, text):
    path = tmp_path / "rules.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_serial_code_rules(path)


def test_rule_file_size_limit_is_exact(tmp_path):
    path = tmp_path / "rules.json"
    path.write_bytes(EXAMPLE.read_bytes().ljust(MAX_RULE_BYTES))
    assert load_serial_code_rules(path) == SerialCodeRules()
    path.write_bytes(EXAMPLE.read_bytes().ljust(MAX_RULE_BYTES + 1))
    with pytest.raises(ValueError):
        load_serial_code_rules(path)


def test_proposed_registry_entry_stages_the_example_through_the_loader(tmp_path):
    spec = PROPOSED_KEY
    others = tuple(entry for key, entry in REGISTRY.items() if key != spec.key)
    assert ConfigRegistry((*others, spec))[spec.key] is spec
    assert spec.key not in REGISTRY or REGISTRY[spec.key].describe() == spec.describe()
    assert spec.dest == "serial_code_rules" and spec.max_bytes == MAX_RULE_BYTES
    assert spec.rules_type() is SerialCodeRules and spec.default() == SerialCodeRules()
    assert spec.normalize(spec.default()) == SerialCodeRules().to_dict()
    raw = EXAMPLE.read_bytes()
    directory = tmp_path / "staging"
    directory.mkdir()
    staged = spec.stage(raw, directory, root=tmp_path)
    assert staged.data == serialize(json.loads(raw)).encode("utf-8") == staged.path.read_bytes()
    assert staged.rules == SerialCodeRules() and staged.content_version == SerialCodeRules().content_version
    assert staged.normalized == SerialCodeRules().to_dict()
    deep = tmp_path / "deep.json"
    deep.write_bytes(b"[" * (spec.max_bytes // 2) + b"]" * (spec.max_bytes // 2))
    with pytest.raises(ConfigError) as error:
        spec.load(deep)
    assert (error.value.code, error.value.key) == ("config_nesting_too_deep", "serial-code-rules")
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(raw.ljust(spec.max_bytes + 1))
    with pytest.raises(ConfigError) as error:
        spec.load(oversized)
    assert error.value.code == "config_rules_invalid"


def test_proposed_registry_entry_normalizes_a_configured_table(tmp_path):
    rules = SerialCodeRules(codes=[code("ABCDE", window=WINDOW), code("xyz", limit=4)])
    directory = tmp_path / "staging"
    directory.mkdir()
    staged = PROPOSED_KEY.stage(rules.to_dict(), directory, root=tmp_path)
    assert staged.rules == rules and staged.normalized == rules.to_dict()
    assert staged.content_version == rules.content_version
    rejected = tmp_path / "rejected"
    rejected.mkdir()
    with pytest.raises(ConfigError) as error:
        PROPOSED_KEY.stage({"schema_version": 1, "codes": [code("ab_c")]}, rejected, root=tmp_path)
    assert (error.value.code, error.value.key) == ("config_rules_invalid", "serial-code-rules")
    assert not list(rejected.iterdir())

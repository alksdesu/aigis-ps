from dataclasses import FrozenInstanceError
import json

import pytest

from aigis_ps.letstd_rules import GROUPS, MAX_RULE_BYTES, PARTNERS, LetsTDRules, load_letstd_rules


ALL_ON = {partner: {name: True for name in GROUPS.values()} for partner in PARTNERS}


def write(path, value, *, prefix=b""):
    path.write_bytes(prefix + json.dumps(value, ensure_ascii=False).encode("utf-8"))
    return path


def test_defaults_keep_every_partner_group_and_native_popup_off():
    rules = LetsTDRules()
    assert PARTNERS == ("oshirore", "monmusutd", "rlyeh")
    assert dict(GROUPS) == {1: "newChallenge", 2: "newPurchase"}
    assert rules.native_popup is False and rules.enabled_groups == ()
    assert all(rules.enabled(partner, group) is False for partner in PARTNERS for group in GROUPS)
    assert rules.to_dict() == {"schema_version": 1, "native_popup": False, "partners": {
        partner: {"newChallenge": False, "newPurchase": False} for partner in PARTNERS}}
    assert load_letstd_rules(None) == rules
    assert len(rules.content_version) == 64 and rules.content_version == LetsTDRules().content_version


def test_switches_follow_native_partner_and_group_order():
    rules = LetsTDRules(partners={"rlyeh": {"newPurchase": True}, "oshirore": {"newChallenge": True,
                                                                               "newPurchase": True}})
    assert rules.enabled_groups == (("oshirore", 1), ("oshirore", 2), ("rlyeh", 2))
    assert rules.enabled("monmusutd", 1) is False and rules.enabled("rlyeh", 2) is True
    every = tuple((partner, group) for partner in PARTNERS for group in (1, 2))
    assert LetsTDRules(partners=ALL_ON).enabled_groups == every
    with pytest.raises(FrozenInstanceError):
        rules.native_popup = True
    with pytest.raises(TypeError):
        rules.partners["oshirore"]["newChallenge"] = False


def test_each_switch_changes_the_rules_fingerprint():
    versions = {LetsTDRules().content_version, LetsTDRules(native_popup=True).content_version}
    for partner in PARTNERS:
        for name in GROUPS.values():
            versions.add(LetsTDRules(partners={partner: {name: True}}).content_version)
    assert len(versions) == 2 + len(PARTNERS) * len(GROUPS)


@pytest.mark.parametrize("values", [{"native_popup": 1}, {"native_popup": None}, {"partners": []},
    {"partners": {"td_shiro": {"newChallenge": True}}}, {"partners": {"oshirore": {"campaign": True}}},
    {"partners": {"oshirore": {"newChallenge": 1}}}, {"partners": {"oshirore": {"newChallenge": None}}},
    {"partners": {"oshirore": ["newChallenge"]}}, {"partners": {"oshirore": {1: True}}}])
def test_rules_reject_ambiguous_switches(values):
    with pytest.raises(ValueError):
        LetsTDRules(**values)


@pytest.mark.parametrize("partner,group", [("td_shiro", 1), ("oshirore", 3), ("oshirore", "1"), ("oshirore", True)])
def test_unknown_partner_or_group_has_no_switch(partner, group):
    with pytest.raises(ValueError):
        LetsTDRules().enabled(partner, group)


def test_rule_file_roundtrip_partial_defaults_and_byte_order_mark(tmp_path):
    rules = LetsTDRules(partners=ALL_ON, native_popup=True)
    assert load_letstd_rules(write(tmp_path / "all.json", rules.to_dict())) == rules
    partial = write(tmp_path / "partial.json", {"schema_version": 1, "partners": {"monmusutd": {"newPurchase": True}}},
                    prefix=b"\xef\xbb\xbf")
    assert load_letstd_rules(partial) == LetsTDRules(partners={"monmusutd": {"newPurchase": True}})
    assert load_letstd_rules(str(write(tmp_path / "minimal.json", {"schema_version": 1}))) == LetsTDRules()


@pytest.mark.parametrize("text", [
    '{"schema_version":1,"schema_version":1}',
    '{"schema_version":1,"partners":{"oshirore":{"newChallenge":true,"newChallenge":false}}}',
    '{"schema_version":1,"partners":{"oshirore":{},"oshirore":{}}}',
    '{"schema_version":1,"label":""}', '{"schema_version":1,"enabled":true}',
    '{"partners":{}}', '{"schema_version":2}', '{"schema_version":true}', '{"schema_version":"1"}',
    '[]', '"schema_version"', '{"schema_version":1,"partners":{"rlyeh":{"newPurchase":"yes"}}}'])
def test_rule_file_rejects_repeats_unknown_fields_and_versions(tmp_path, text):
    path = tmp_path / "rules.json"
    path.write_bytes(text.encode("utf-8"))
    with pytest.raises(ValueError):
        load_letstd_rules(path)


def test_rule_file_size_is_bounded(tmp_path):
    assert MAX_RULE_BYTES == 8 * 1024
    exact = b'{"schema_version":1}'
    path = tmp_path / "bounded.json"
    path.write_bytes(exact + b" " * (MAX_RULE_BYTES - len(exact)))
    assert load_letstd_rules(path) == LetsTDRules()
    path.write_bytes(exact + b" " * (MAX_RULE_BYTES - len(exact) + 1))
    with pytest.raises(ValueError):
        load_letstd_rules(path)
    with pytest.raises(ValueError):
        load_letstd_rules(tmp_path / "missing.json")

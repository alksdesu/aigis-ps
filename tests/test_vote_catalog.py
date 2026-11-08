from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.local_present_templates import LOCAL_TEMPLATE_CARDS
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.resource_view import load_resource_view
from aigis_ps.table_cell_patch import patch_table_cells
from aigis_ps.vote_catalog import TEMPLATE_FIELDS, VoteCatalog
from aigis_ps.vote_rules import VoteRules, load_vote_rules
from test_present_items import item_table


CLIENT_HASH = CURRENT_CLIENT_SHA256
RESOURCE_NAMES = ("SystemText.atb", "cfgImplantText.atb", "NameText.atb", "ItemConfigTable.atb", "PresentUnitData.atb")
RESEARCHED_CHOICES = {"闇エルフの女王ドロテア": 408, "女王子": 1737, "世界を超える者エフトラ": 1468,
    "一目連": 1783, "銀腕を掲げし者トラム": 1110, "魔導を宿す者ラーワル": 1369,
    "オークの格闘家ベストラ": 474, "傭兵クレイブ": 8, "山賊頭モーティマ": 14,
    "癒し手アリサ": 24, "魔女カリオペ": 42, "白の皇帝": 418, "闇の墓守リッチ": 1807,
    "ダーク王子": 1831, "ダークアンナ": 1832, "揺るがぬ黒き騎士デシウス": 2752,
    "鋼鉄の女レアン": 50, "魔術師パレス": 129}
CHOICES_WITH_LOCAL_ALTERNATIVES = {"闇エルフの女王ドロテア": 408, "一目連": 1783, "揺るがぬ黒き騎士デシウス": 2752}
CONTENT_POLICY = "local_vote_16_text_source_v1"


@pytest.fixture(scope="module")
def vote_sources():
    project = Path(__file__).resolve().parents[1]
    _, entries = ResourceLibrary(project / "runtime/resources", client_hash=CLIENT_HASH).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    data = {name: assets.read(name)[0] for name in RESOURCE_NAMES}
    data["CardData"] = MasterDataStore(project / "runtime/server/masterdata").get_xml("CardData")
    return data


@pytest.fixture(scope="module")
def effective_sources(vote_sources):
    project = Path(__file__).resolve().parents[1]
    view = load_resource_view(project / "runtime/resources", project.parent / "aigisr_exe", CLIENT_HASH,
                              master_provider=MasterDataStore(project / "runtime/server/masterdata").get_xml)
    return {**vote_sources, **{name: view.assets.read(name)[0] for name in RESOURCE_NAMES}}


def build(data, rules=None):
    return VoteCatalog(data.__getitem__, data.__getitem__, rules)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture(scope="module")
def current(vote_sources):
    return build(vote_sources, VoteRules(candidate_policy="strict"))


@pytest.fixture(scope="module")
def effective(effective_sources):
    return build(effective_sources)


def resolved_rules(current, **changes):
    choices = {choice.name: choice.card_ids[-1] for choice in current.unresolved_candidates}
    return VoteRules(candidate_overrides=choices, **changes)


def rows(data, name):
    raw = data[name]
    return parse_table(decompress_all4(raw) if raw.startswith(b"ALL4") else raw)["rows"]


def test_real_directory_preserves_source_and_reports_every_unresolved_candidate(current):
    assert len(current.groups) == 4
    assert len(current.candidates) == 938
    assert len(current.unresolved_candidates) == 18
    assert len(current.candidates) + len(current.unresolved_candidates) == 956
    assert current.event.source_start == datetime(2026, 8, 27, 15, tzinfo=JST)
    assert current.event.source_end == datetime(2026, 9, 11, 4, tzinfo=JST)
    assert current.event.result_end == datetime(2026, 9, 18, 15, tzinfo=JST)
    assert current.event.announcement_start == current.event.start_at
    assert current.event.announcement_end == current.event.result_end
    assert current.status()["qualification_ready"] is False
    assert current.active(datetime(2026, 11, 1, tzinfo=JST)) is False
    with pytest.raises(ProtocolError, match="identities") as error:
        current.active(datetime(2026, 8, 28, tzinfo=JST))
    assert error.value.code == "vote_candidates_unresolved"


def test_explicit_resolution_includes_every_name_and_each_group(vote_sources, current):
    catalog = build(vote_sources, resolved_rules(current))
    assert catalog.status()["qualification_ready"] is True
    assert not catalog.unresolved_candidates
    assert [sum(candidate.group_id == group for candidate in catalog.candidates) for group in (1, 2, 3)] == [318, 319, 319]
    assert len(catalog.candidates_by_id) == 956
    assert catalog.source_content_version == current.source_content_version
    assert catalog.content_version != current.content_version


def test_ticket_and_vote_windows_overlap_without_being_collapsed(current):
    first, second, _, final = current.groups
    assert first.dispersion_end == second.dispersion_start == second.vote_start
    assert first.vote_end - second.vote_start == timedelta(hours=1)
    assert final.vote_start - final.dispersion_start == timedelta(hours=2)
    assert final.vote_end - final.dispersion_end == timedelta(hours=17)
    assert final.final and not first.final


def test_revival_moves_every_window_by_one_offset_and_keeps_sources(vote_sources, current):
    catalog = build(vote_sources, resolved_rules(current, revival_start="2026-11-01T15:00:00+09:00"))
    offset = datetime(2026, 11, 1, 15, tzinfo=JST) - current.event.start_at
    for original, shifted in zip(current.groups, catalog.groups):
        for attribute in ("dispersion_start", "dispersion_end", "vote_start", "vote_end", "result_start"):
            assert getattr(shifted, attribute) == getattr(original, attribute) + offset
            assert getattr(shifted, "source_" + attribute) == getattr(original, attribute)
    assert catalog.active(catalog.event.start_at - timedelta(seconds=1)) is False
    assert catalog.active(catalog.event.start_at) is True
    assert catalog.active(catalog.event.end_at - timedelta(seconds=1)) is True
    assert catalog.active(catalog.event.end_at) is False
    assert catalog.event.source_start == current.event.source_start


def test_disabling_retains_catalog_but_does_not_require_unresolved_choices(vote_sources):
    catalog = build(vote_sources, VoteRules(enabled=False, candidate_policy="strict"))
    assert len(catalog.unresolved_candidates) == 18
    assert catalog.active(catalog.event.start_at) is False


def test_fixed_and_repeat_rewards_preserve_thresholds_templates_and_amounts(current):
    assert len(current.rewards) == 56
    assert sum(not reward.repeat_every for reward in current.rewards) == 52
    assert {(reward.group_id, reward.threshold, reward.repeat_every) for reward in current.rewards if reward.repeat_every} == {
        (1, 200, 100), (2, 200, 100), (3, 200, 100), (4, 300, 100)}
    main = [reward for reward in current.rewards if reward.card_id == 2871]
    assert main and all(reward.template_id == 2006 and reward.amount == 1 and reward.unit_level == 1 for reward in main)
    items = [reward for reward in current.rewards if reward.kind == "item"]
    assert len(items) == 5 and all(reward.amount == 5 for reward in items)
    fairy = [reward for reward in current.rewards if reward.card_id == 60]
    assert len(fairy) == 4 and all(reward.template_id == 6 and reward.skill_level == 1 for reward in fairy)


def test_skill_fairy_local_override_changes_only_its_reward_templates(vote_sources, current):
    changed = build(vote_sources, VoteRules(skill_fairy_template_id=840))
    for before, after in zip(current.rewards, changed.rewards):
        if before.card_id == 60:
            assert after.template_id == 840 and after.skill_level == 0
        else:
            assert before == after


@pytest.mark.parametrize("overrides", [{"Unknown": 1}, {"女王子": 1}, {"巡り咲く者アンブローズ": 1087}])
def test_overrides_cannot_add_unknown_names_or_wrong_rarity(vote_sources, overrides):
    with pytest.raises(ProtocolError) as error:
        build(vote_sources, VoteRules(candidate_overrides=overrides))
    assert error.value.code == "vote_catalog_invalid"


@pytest.mark.parametrize("field,value", [("Stack", 1000), ("EffectType", 0), ("CanAllGet", 0),
                                        ("Category", 1078486164), ("ReceiveAction", 1), ("EventID", "event")])
def test_vote_ticket_semantics_are_checked_without_widening_other_items(vote_sources, field, value):
    name = "ItemConfigTable.atb"
    index = next(index for index, row in enumerate(rows(vote_sources, name)) if row["ItemID"] == "item_vote_016001")
    changed = dict(vote_sources)
    changed[name] = patch_table_cells(changed[name], {index: {field: value}})
    with pytest.raises(ProtocolError) as error:
        build(changed)
    assert error.value.code == "vote_catalog_invalid"


@pytest.mark.parametrize("text_key,old,new", [
    ("SystemTextVoteInfomation6", "2026/8/30 14:59", "2026/8/29 14:59"),
    ("SystemTextVoteInfomation7", "【ブラック】", "【unknown】"),
    ("SystemTextVoteInfomation8", "盗賊ベルナ", "unknown candidate"),
    ("SystemTextVoteInfomation9", "各グループ上位10名", "各グループ上位5名"),
    ("SystemTextVoteInfomation5", "5：ゴールドアーマー", "7：ゴールドアーマー")])
def test_changed_source_contract_is_rejected(vote_sources, text_key, old, new):
    [text_index] = [row["_TextID"] for row in rows(vote_sources, "cfgImplantText.atb") if row["_ID"] == text_key]
    original = rows(vote_sources, "SystemText.atb")[text_index]["Data_Text"]
    assert old in original
    changed = dict(vote_sources)
    changed_rows = rows(vote_sources, "SystemText.atb")
    changed_rows[text_index]["Data_Text"] = original.replace(old, new)
    changed["SystemText.atb"] = item_table(changed_rows, (("Recode_Index", 1), ("Data_Text", 32)))
    with pytest.raises(ProtocolError) as error:
        build(changed)
    assert error.value.code == "vote_catalog_invalid"


def test_frozen_catalog_does_not_reopen_sources_or_accept_replacements(vote_sources):
    reads = []
    def reader(name):
        reads.append(name)
        return vote_sources[name]
    catalog = VoteCatalog(reader, reader)
    initial_reads = list(reads)
    catalog.status()
    catalog.active(datetime(2026, 11, 1, tzinfo=JST))
    assert reads == initial_reads
    with pytest.raises(FrozenInstanceError):
        catalog.groups[0].group_id = 90
    with pytest.raises(TypeError):
        catalog.groups_by_id[9] = catalog.groups[0]
    catalog.candidates = ()
    assert catalog.status()["ready"] is False
    with pytest.raises(ProtocolError) as error:
        catalog.active(datetime(2026, 11, 1, tzinfo=JST))
    assert error.value.code == "vote_catalog_changed"


@pytest.mark.parametrize("change", [{"enabled": 1}, {"candidate_overrides": []},
    {"candidate_policy": 1}, {"candidate_policy": None}, {"candidate_policy": "minimum_id"},
    {"candidate_overrides": {"name": True}}, {"candidate_overrides": {" name": 1}},
    {"skill_fairy_template_id": 5}, {"required_rank": 0}, {"vote_limit_count": 1000},
    {"earned_votes": False}, {"result_display_days": 367},
    {"revival_start": "2026-11-01T15:00:00"}, {"revival_start": "2026-11-01T15:00+09:00"},
    {"revival_start": "2026-11-01T15:00:00.001+09:00"}])
def test_invalid_local_rules_are_rejected(change):
    with pytest.raises(ValueError):
        VoteRules(**change)


@pytest.mark.parametrize("payload", ['{"enabled": true, "enabled": false}', '{"unknown": 1}',
    '{"candidate_overrides":{"name":1,"name":2}}', '[]'])
def test_rule_file_rejects_duplicates_and_unknown_fields(tmp_path, payload):
    path = tmp_path / "rules.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        load_vote_rules(path)


def test_rule_snapshot_and_file_roundtrip_preserve_all_local_policies(tmp_path):
    source = {"女王子": 1737}
    rules = VoteRules(candidate_overrides=source, required_rank=10, earned_votes=20, result_display_days=3)
    source["女王子"] = 1048
    assert rules.candidate_overrides["女王子"] == 1737
    with pytest.raises(TypeError):
        rules.candidate_overrides["女王子"] = 1048
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules.to_dict(), ensure_ascii=False), encoding="utf-8-sig")
    assert load_vote_rules(path) == rules
    assert load_vote_rules(path).content_version == rules.content_version
    assert load_vote_rules(None) == VoteRules()


def test_default_policy_resolves_real_candidates_with_template_evidence(vote_sources, current):
    catalog = build(vote_sources)
    assert {choice.name: choice.card_id for choice in catalog.local_resolutions} == RESEARCHED_CHOICES
    assert len(catalog.candidates) == 956 and not catalog.unresolved_candidates
    assert [sum(candidate.group_id == group for candidate in catalog.candidates) for group in (1, 2, 3)] == [318, 319, 319]
    assert catalog.status()["qualification_ready"] is True
    evidence = catalog.status()["local_resolution"]
    assert evidence["policy"] == "present_template" and evidence["official_vcc_confirmed"] is False
    assert evidence["resource"] == "PresentUnitData.atb" and evidence["excluded_present_ids"] == []
    templates = {row["PresentID"]: row for row in rows(vote_sources, "PresentUnitData.atb")}
    for choice in evidence["choices"]:
        assert choice["present_ids"]
        assert all(identifier > 0 and templates[identifier]["CardID"] == choice["card_id"]
                   for identifier in choice["present_ids"])
    assert catalog.source_content_version == current.source_content_version
    assert catalog.content_version != current.content_version
    evidence["choices"].clear()
    assert len(catalog.local_resolutions) == len(catalog.status()["local_resolution"]["choices"]) == 18
    with pytest.raises(FrozenInstanceError):
        catalog.local_resolutions[0].card_id = 1


@pytest.mark.parametrize("replacement", [1, 1048])
def test_changed_delivery_identity_keeps_unproven_candidate_unselectable(vote_sources, replacement):
    name = "PresentUnitData.atb"
    source_rows = rows(vote_sources, name)
    if replacement == 1:
        indices = [index for index, row in enumerate(source_rows) if row["CardID"] == 1737 and row["PresentID"] > 0]
    else:
        indices = [next(index for index, row in enumerate(source_rows) if row["PresentID"] == 2)]
    changed = dict(vote_sources)
    for index in indices:
        source_rows[index]["CardID"] = replacement
    changed[name] = item_table(source_rows, tuple(TEMPLATE_FIELDS.items()))
    catalog = build(changed)
    assert [(entry.name, entry.card_ids) for entry in catalog.unresolved_candidates] == [("女王子", (1048, 1737))]
    assert not ({1048, 1737} & catalog.candidates_by_id.keys())
    with pytest.raises(ProtocolError) as error:
        catalog.active(catalog.event.start_at)
    assert error.value.code == "vote_candidates_unresolved"


def test_multiple_templates_for_same_candidate_do_not_create_identity_ambiguity(vote_sources):
    name = "PresentUnitData.atb"
    source_rows = rows(vote_sources, name)
    index = next(index for index, row in enumerate(source_rows) if row["PresentID"] == 2)
    changed = dict(vote_sources)
    source_rows[index]["CardID"] = 1737
    changed[name] = item_table(source_rows, tuple(TEMPLATE_FIELDS.items()))
    catalog = build(changed)
    selected = next(entry for entry in catalog.local_resolutions if entry.name == "女王子")
    assert selected.card_id == 1737 and len(selected.template_ids) == 2 and 2 in selected.template_ids
    assert not catalog.unresolved_candidates


def test_explicit_override_takes_priority_without_being_reported_as_template_evidence(vote_sources):
    catalog = build(vote_sources, VoteRules(candidate_overrides={"女王子": 1048}))
    assert catalog.candidates_by_id[1048].name == "女王子" and 1737 not in catalog.candidates_by_id
    assert all(entry.name != "女王子" for entry in catalog.local_resolutions)
    assert catalog.status()["rules"]["candidate_overrides"] == {"女王子": 1048}
    assert len(catalog.candidates) == 956 and len(catalog.local_resolutions) == 17


def test_effective_view_counts_only_native_templates_as_identity_evidence(vote_sources, effective_sources, effective):
    native = build(vote_sources)
    appended = {row["PresentID"]: row["CardID"] for row in rows(effective_sources, "PresentUnitData.atb")
                if row["PresentID"] in LOCAL_TEMPLATE_CARDS}
    assert appended == dict(LOCAL_TEMPLATE_CARDS)
    assert effective.excluded_template_ids == tuple(sorted(LOCAL_TEMPLATE_CARDS)) and native.excluded_template_ids == ()
    assert {choice.name: choice.card_id for choice in effective.local_resolutions} == RESEARCHED_CHOICES
    assert {choice.name: choice.card_id for choice in effective.local_resolutions
            if set(choice.choices) & set(LOCAL_TEMPLATE_CARDS.values())} == CHOICES_WITH_LOCAL_ALTERNATIVES
    assert effective.local_resolutions == native.local_resolutions and effective.candidates == native.candidates
    assert [sum(candidate.group_id == group for candidate in effective.candidates) for group in (1, 2, 3)] == [318, 319, 319]
    assert not effective.unresolved_candidates and effective.status()["qualification_ready"] is True
    assert effective.active(effective.event.start_at) is True
    assert effective.rewards == native.rewards
    evidence = effective.status()["local_resolution"]
    assert evidence["excluded_present_ids"] == sorted(LOCAL_TEMPLATE_CARDS)
    assert not {identifier for choice in evidence["choices"] for identifier in choice["present_ids"]} & LOCAL_TEMPLATE_CARDS.keys()
    evidence["excluded_present_ids"].clear()
    assert effective.status()["local_resolution"]["excluded_present_ids"] == sorted(LOCAL_TEMPLATE_CARDS)


def test_strict_policy_keeps_every_ambiguous_name_on_effective_resources(effective_sources, current):
    catalog = build(effective_sources, VoteRules(candidate_policy="strict"))
    assert catalog.unresolved_candidates == current.unresolved_candidates
    assert len(catalog.candidates) == 938 and not catalog.local_resolutions
    assert catalog.excluded_template_ids == tuple(sorted(LOCAL_TEMPLATE_CARDS))


def test_override_may_select_an_alternative_backed_only_by_a_local_template(effective_sources):
    catalog = build(effective_sources, VoteRules(candidate_overrides={"闇エルフの女王ドロテア": 1605}))
    assert catalog.candidates_by_id[1605].name == "闇エルフの女王ドロテア" and 408 not in catalog.candidates_by_id
    assert all(entry.name != "闇エルフの女王ドロテア" for entry in catalog.local_resolutions)
    assert len(catalog.candidates) == 956 and len(catalog.local_resolutions) == 17 and not catalog.unresolved_candidates


def test_native_template_for_a_locally_delivered_card_keeps_its_name_unresolved(effective_sources):
    name = "PresentUnitData.atb"
    source_rows = rows(effective_sources, name)
    index = next(index for index, row in enumerate(source_rows) if row["PresentID"] == 2)
    source_rows[index]["CardID"] = 1605
    changed = dict(effective_sources)
    changed[name] = item_table(source_rows, tuple(TEMPLATE_FIELDS.items()))
    catalog = build(changed)
    assert catalog.excluded_template_ids == tuple(sorted(LOCAL_TEMPLATE_CARDS))
    assert [(entry.name, entry.card_ids) for entry in catalog.unresolved_candidates] == [("闇エルフの女王ドロテア", (408, 1605))]
    with pytest.raises(ProtocolError) as error:
        catalog.active(catalog.event.start_at)
    assert error.value.code == "vote_candidates_unresolved"


def test_reserved_present_id_naming_another_card_counts_as_native_evidence(effective_sources):
    name = "PresentUnitData.atb"
    reserved = next(identifier for identifier, card in LOCAL_TEMPLATE_CARDS.items() if card == 1605)
    source_rows = rows(effective_sources, name)
    index = next(index for index, row in enumerate(source_rows) if row["PresentID"] == reserved)
    source_rows[index]["CardID"] = 1785
    changed = dict(effective_sources)
    changed[name] = item_table(source_rows, tuple(TEMPLATE_FIELDS.items()))
    catalog = build(changed)
    assert catalog.excluded_template_ids == tuple(sorted(set(LOCAL_TEMPLATE_CARDS) - {reserved}))
    assert [(entry.name, entry.card_ids) for entry in catalog.unresolved_candidates] == [("一目連", (1783, 1785))]
    assert catalog.candidates_by_id[408].name == "闇エルフの女王ドロテア" and 1605 not in catalog.candidates_by_id


def test_native_evidence_rule_keeps_the_published_catalog_fingerprints(effective):
    assert effective.source_content_version == fingerprint({"event": 16, "resources": dict(effective.resource_hashes)})
    assert effective.content_version == fingerprint({"source": effective.source_content_version,
                                                     "rules": effective.rules.to_dict(), "policy": CONTENT_POLICY})


def test_replacing_excluded_template_evidence_marks_the_catalog_changed(effective_sources):
    catalog = build(effective_sources)
    catalog.excluded_template_ids = ()
    assert catalog.status()["ready"] is False
    with pytest.raises(ProtocolError) as error:
        catalog.active(catalog.event.start_at)
    assert error.value.code == "vote_catalog_changed"

from dataclasses import replace
import hashlib
import struct

import pytest

from aigis_ps.al_format import MAX_RESOURCE_BYTES
from aigis_ps.assets import asset_key
from aigis_ps import resource_plan
from aigis_ps.resource_fetch import ResourceEntry
from aigis_ps.resource_plan import build_plan


def entry(name, content=b"resource"):
    return ResourceEntry(name, "a" * 40, hashlib.md5(content).hexdigest(), 0, len(content))


def put(root, name, content, sidecar=None):
    cache = root / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / asset_key(name)
    path.write_bytes(content)
    if sidecar is not None:
        path.with_name(path.name + ".md5").write_text(sidecar)
    return path


def roots(tmp_path):
    source, overlay = tmp_path / "original", tmp_path / "overlay"
    source.mkdir()
    return source, overlay


def by_name(plan):
    return {row["name"]: row for row in plan["entries"]}


def mission_table(rows):
    schema = bytearray(struct.pack("<4sBBHH", b"ALRD", 1, 0, 2, 8))
    for offset, name in ((0, "QuestID"), (4, "MissionID")):
        encoded = name.encode()
        schema += struct.pack("<HBBBB", offset, 1, 8, len(encoded), 0) + encoded + b"\0\0"
        schema += b"\0" * (-len(schema) % 4)
        schema += b"\0" * 8
    data = struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(rows), 20, 20 + len(schema), 8, b"list")
    return data + schema + b"".join(struct.pack("<ii", *row) for row in rows)


def test_availability_uses_actual_content_not_sidecar_claims(tmp_path):
    source, overlay = roots(tmp_path)
    correct = b"current bytes"
    expected = entry("Good.aar", correct)
    put(source, expected.name, correct, "0" * 32)
    put(source, "Bad.aar", b"old", expected.md5)
    items = [expected, entry("Bad.aar", correct), entry("Missing.aar", b"absent")]
    plan = build_plan(items, source, overlay)
    result = by_name(plan)
    assert result["Good.aar"]["status"] == "available_source"
    assert result["Good.aar"]["locations"][0]["sidecar_status"] == "mismatch"
    assert result["Bad.aar"]["status"] == "stale"
    assert result["Missing.aar"]["status"] == "missing"
    assert plan["summary"]["download"] == 2
    assert plan["summary"]["download_bytes"] == len(correct) + len(b"absent")
    assert not overlay.exists()


def test_valid_overlay_wins_and_stale_overlay_does_not_hide_valid_source(tmp_path):
    source, overlay = roots(tmp_path)
    put(source, "First.aar", b"current")
    put(overlay, "First.aar", b"current")
    put(source, "Second.aar", b"current")
    put(overlay, "Second.aar", b"old")
    result = by_name(build_plan([entry("First.aar", b"current"), entry("Second.aar", b"current")], source, overlay))
    assert result["First.aar"]["status"] == "available_overlay"
    assert result["Second.aar"]["status"] == "available_source"
    assert len(result["Second.aar"]["locations"]) == 2
    assert result["Second.aar"]["locations"][0]["valid"] is False


def test_size_mismatch_is_stale_even_when_digest_matches(tmp_path):
    source, overlay = roots(tmp_path)
    put(source, "Resource.aar", b"same")
    plan = build_plan([replace(entry("Resource.aar", b"same"), size=99)], source, overlay)
    assert plan["entries"][0]["status"] == "stale"
    assert plan["summary"]["stale_bytes"] == 99


def test_orphan_sidecar_is_still_missing(tmp_path):
    source, overlay = roots(tmp_path)
    (source / (asset_key("Missing.aar") + ".md5")).write_text("0" * 32)
    plan = build_plan([entry("Missing.aar")], source, overlay)
    assert plan["entries"][0]["status"] == "missing"
    assert any(item["code"] == "orphan_md5" for item in plan["diagnostics"])


def test_each_existing_root_is_inventoried_once(tmp_path, monkeypatch):
    source, overlay = roots(tmp_path)
    overlay.mkdir()
    calls = []
    original = resource_plan.inventory_assets

    def tracked(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(resource_plan, "inventory_assets", tracked)
    build_plan([entry("One.aar"), entry("Two.aar")], source, overlay)
    assert calls == [source, overlay]


def test_scopes_use_exact_literals_and_indexed_story_families(tmp_path):
    source, overlay = roots(tmp_path)
    (source / "Aigis_r.exe").write_bytes(b"MZ\0Menu.aar\0Map%04d.aar\0PlayerDot%04d.aar\0Script.nut\0")
    names = ["Menu.aar", "Map1234.aar", "PlayerDot1234.aar", "Script.nut", "HiddenScript.nut",
             "Table.atb", "Table.ard", "HarlemText.aar", "EventArc.aar", "BattleTalkEvent123456.aar",
             "QuestNameText123456.atb", "MessageText123456.atb", "Map9002.aar", "_ev004422.aar"]
    plan = build_plan([entry(name) for name in names], source, overlay)
    items = by_name(plan)
    assert items["Menu.aar"]["scopes"] == ["startup", "all"]
    assert items["Map1234.aar"]["scopes"] == ["maps", "all"]
    assert items["PlayerDot1234.aar"]["scopes"] == ["all"]
    assert items["Script.nut"]["scopes"] == ["startup", "all"]
    assert items["HiddenScript.nut"]["scopes"] == ["all"]
    assert items["Table.atb"]["scopes"] == items["Table.ard"]["scopes"] == ["startup", "all"]
    assert items["HarlemText.aar"]["scopes"] == items["EventArc.aar"]["scopes"] == ["startup", "story", "all"]
    assert items["BattleTalkEvent123456.aar"]["scopes"] == ["story", "all"]
    assert items["QuestNameText123456.atb"]["scopes"] == ["startup", "story", "all"]
    assert items["Map9002.aar"]["scopes"] == ["startup", "maps", "all"]
    assert items["_ev004422.aar"]["scopes"] == ["all"]
    assert len(items) == len(names)
    assert plan["summary"]["by_scope"]["story"]["total"] == 5


def test_verified_mission_links_select_story_and_tutorial_maps(tmp_path):
    source, overlay = roots(tmp_path)
    story = mission_table([(2001, 100009)])
    tutorial = mission_table([(3001, 100002)])
    put(source, "StoryMissionQuestList.atb", story)
    put(source, "TutorialMissionQuestList.atb", tutorial)
    maps = ["Map0025.aar", "Map100009_0025.aar", "Map100008_0025.aar", "Map0018.aar", "Map9002.aar"]
    entries = [entry("StoryMissionQuestList.atb", story), entry("TutorialMissionQuestList.atb", tutorial), *(entry(name) for name in maps)]
    plan = build_plan(entries, source, overlay, quest_rows=[{"QuestID": 2001, "MapNo": 25}, {"QuestID": 3001, "MapNo": 9002}, {"QuestID": 999, "MapNo": 18}])
    items = by_name(plan)
    assert "startup" in items["Map0025.aar"]["scopes"]
    assert "startup" in items["Map100009_0025.aar"]["scopes"]
    assert "startup" not in items["Map100008_0025.aar"]["scopes"]
    assert "startup" not in items["Map0018.aar"]["scopes"]
    evidence = [item for item in items["Map0025.aar"]["evidence"] if item["kind"] == "verified_mission_quest"]
    assert evidence[0]["quest_id"] == 2001
    assert evidence[0]["source_table"] == "StoryMissionQuestList.atb"


def test_mission_lists_are_the_story_and_tutorial_quest_lists():
    assert resource_plan.MISSION_LISTS == ("StoryMissionQuestList.atb", "TutorialMissionQuestList.atb")


def test_executable_and_mission_lists_are_read_within_the_resource_limit(tmp_path, monkeypatch):
    source, overlay = roots(tmp_path)
    (source / "Aigis_r.exe").write_bytes(b"MZ\0Menu.aar\0")
    story, tutorial = mission_table([(2001, 100009)]), mission_table([(3001, 100002)])
    put(source, "StoryMissionQuestList.atb", story)
    put(source, "TutorialMissionQuestList.atb", tutorial)
    limits = []
    original = resource_plan.read_bounded

    def tracked(path, limit, *, root=None):
        limits.append(limit)
        return original(path, limit, root=root)

    monkeypatch.setattr(resource_plan, "read_bounded", tracked)
    lists = [entry("StoryMissionQuestList.atb", story), entry("TutorialMissionQuestList.atb", tutorial)]
    build_plan(lists, source, overlay, quest_rows=[{"QuestID": 2001, "MapNo": 25}])
    # `is` rather than ==: a literal respelled in resource_plan is a separate int object.
    assert len(limits) == 3 and all(limit is MAX_RESOURCE_BYTES for limit in limits)


def test_stale_mission_lists_cannot_expand_startup_scope(tmp_path):
    source, overlay = roots(tmp_path)
    table = mission_table([(2001, 100009)])
    put(source, "StoryMissionQuestList.atb", table)
    plan = build_plan([entry("StoryMissionQuestList.atb", b"new version"), entry("Map0025.aar"), entry("Map9002.aar")], source, overlay,
                      quest_rows=[{"QuestID": 2001, "MapNo": 25}])
    items = by_name(plan)
    assert "startup" not in items["Map0025.aar"]["scopes"]
    assert "startup" in items["Map9002.aar"]["scopes"]
    assert any(item["code"] == "mission_list_not_available" for item in plan["diagnostics"])


def test_verified_overlay_mission_list_is_used(tmp_path):
    source, overlay = roots(tmp_path)
    old = mission_table([(100, 100001)])
    current = mission_table([(2001, 100009)])
    put(source, "StoryMissionQuestList.atb", old)
    put(overlay, "StoryMissionQuestList.atb", current)
    plan = build_plan([entry("StoryMissionQuestList.atb", current), entry("Map0025.aar")], source, overlay,
                      quest_rows=[{"QuestID": 2001, "MapNo": 25}])
    assert "startup" in by_name(plan)["Map0025.aar"]["scopes"]


def test_invalid_current_mission_table_keeps_only_supported_fallbacks(tmp_path):
    source, overlay = roots(tmp_path)
    put(source, "StoryMissionQuestList.atb", b"invalid table")
    plan = build_plan([entry("StoryMissionQuestList.atb", b"invalid table"), entry("Map0025.aar"), entry("Map9002.aar")], source, overlay,
                      quest_rows=[{"QuestID": 2001, "MapNo": 25}])
    assert "startup" not in by_name(plan)["Map0025.aar"]["scopes"]
    assert "startup" in by_name(plan)["Map9002.aar"]["scopes"]
    assert any(item["code"] == "mission_list_invalid" for item in plan["diagnostics"])


@pytest.mark.parametrize("rows", [[{"QuestID": 1, "MapNo": -1}], [{"QuestID": True, "MapNo": 1}], [{"QuestID": 1, "MapNo": 1}, {"QuestID": 1, "MapNo": 2}], {}])
def test_invalid_quest_rows_are_rejected(tmp_path, rows):
    source, overlay = roots(tmp_path)
    with pytest.raises(ValueError):
        build_plan([], source, overlay, quest_rows=rows)


def test_duplicates_native_cache_key_and_empty_summary(tmp_path):
    source, overlay = roots(tmp_path)
    resource = replace(entry("One.aar"), native_cache_key=asset_key("One.aar"))
    plan = build_plan([entry("One.aar"), resource], source, overlay)
    assert plan["summary"]["total"] == 1
    assert plan["entries"][0]["native_cache_key"] == resource.cache_key
    with pytest.raises(ValueError, match="conflicting"):
        build_plan([resource, entry("One.aar", b"other")], source, overlay)
    empty = build_plan([], source, overlay)
    assert empty["summary"]["total_bytes"] == 0
    assert empty["summary"]["by_scope"]["all"]["total"] == 0

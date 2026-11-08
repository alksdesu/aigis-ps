from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from aigis_ps import mission_tags
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.mission_tags import (
    CONFIG_CREATED_CATEGORIES, LIST_LIMIT, MEMO_LIMIT, REGISTRY_CATEGORIES, REGISTRY_TABLES, SERVER_PROVIDED_CATEGORIES,
    TAGLESS_CATEGORIES, MissionTagIndex, mission_tables,
)
from aigis_ps.possessions import PossessionService
from aigis_ps.quest_catalog import QuestCatalog
from aigis_ps.quest_modes import MISSION_CATEGORIES, MODE_SPECS, NO_MISSION_RANK, PLATFORM_MASK_CATEGORIES, MissionWindows
from aigis_ps.resource_library import ResourceLibrary


NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
EPOCH = datetime(1, 1, 1, tzinfo=JST)
MILLISECOND = timedelta(milliseconds=1)
PAST = (datetime(2026, 1, 1, tzinfo=JST), datetime(2026, 1, 2, tzinfo=JST))
SOURCES = MappingProxyType({name: "0" * 64 for name in REGISTRY_TABLES})
MASTER = "fixture-master"
INT32_MIN, INT32_MAX = -(2 ** 31), 2 ** 31 - 1
PLATFORM, OTHER_PLATFORM = 1, 2
# Real masks enable platform bits 0-12.
NATIVE_MASK = 8191
MONDAY, TUESDAY = 2, 3
HISTORY_GATE = ("FCMenuQuestTypeList::IsEntrust_14",)
CURRENT_MISSIONS = MappingProxyType({0: 12, 1: 32, 2: 144, 3: 50, 5: 7, 6: 1, 7: 1, 8: 36, 9: 1, 10: 19, 11: 15,
                                     12: 3, 13: 2, 14: 241, 15: 6, 16: 12, 17: 5})
# Neither config row appears in its MissionQuestList (Emergency, Reproduce), so the client never creates them.
CURRENT_CONFIG_ONLY = (200554, 320118)
DEVIL_CATEGORY, DEVIL_MENU_TAG, DEVIL_MENU_MISSION, DEVIL_RANK = 8, 150, 500090, 50
TOWER_CATEGORY, HISTORY_CATEGORY = 12, 14


def words(moment):
    if moment is None:
        return [0, 0]
    milliseconds = (moment - EPOCH) // MILLISECOND
    return [milliseconds & 0xFFFFFFFF, milliseconds >> 32]


def mission(category, identifier, *, enable=None, tag=0, span=0, start=None, end=None, quests=None):
    # Most loaders only create missions their MissionQuestList names, so a mission lists itself unless told otherwise.
    members = (identifier,) if quests is None else tuple(quests)
    if enable is None:
        enable = NATIVE_MASK if category in PLATFORM_MASK_CATEGORIES else True
    return category, {"MissionID": identifier, "Enable": enable, "StartDateTime": words(start),
                      "EndDateTime": words(end), "TagID": tag, "TagIDRange": span}, members


def registry(*missions, ranks=None, overrides=None):
    tables = {category: ([], []) for category in REGISTRY_CATEGORIES}
    for category, row, quests in missions:
        tables[category][0].append(row)
        tables[category][1].extend({"MissionID": row["MissionID"], "QuestID": quest} for quest in quests)
    return MissionWindows(tables, ranks or {}, overrides=overrides)


def index(*missions, quest_rows=None, overrides=None, untagged=(), sources=SOURCES, master=MASTER):
    rows = {} if quest_rows is None else quest_rows
    ranks = {quest_id: row["Rank"] for quest_id, row in rows.items() if "Rank" in row}
    return MissionTagIndex(registry(*missions, ranks=ranks, overrides=overrides), quest_rows=rows,
                           source_sha256=sources, master_snapshot=master, overrides=overrides,
                           categories_without_tag_column=untagged)


def resolved(subject, tag, platform=PLATFORM):
    found = subject.tag_mission(tag, platform)
    return None if found is None else found.mission_id


def fail(code, status, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert (error.value.code, error.value.status) == (code, status)


def test_the_registry_holds_every_client_loaded_category_and_its_tables():
    assert SERVER_PROVIDED_CATEGORIES == {4} and TAGLESS_CATEGORIES == {TOWER_CATEGORY}
    assert CONFIG_CREATED_CATEGORIES == {HISTORY_CATEGORY, 15, 16, 17}
    assert REGISTRY_CATEGORIES == tuple(sorted(set(MISSION_CATEGORIES) - SERVER_PROVIDED_CATEGORIES))
    assert mission_tables(0) == ("StoryMissionConfig.atb", "StoryMissionQuestList.atb")
    assert mission_tables(14) == ("HistoryMissionConfig.atb",) and mission_tables(17) == ("GoddessRevivalMissionConfig.atb",)
    assert len(REGISTRY_TABLES) == len(set(REGISTRY_TABLES)) == 32
    assert not any(name.startswith(MISSION_CATEGORIES[4]) for name in REGISTRY_TABLES)


def test_tags_resolve_to_the_first_registered_mission_by_id_whatever_its_category_or_window():
    subject = index(mission(5, 700101, tag=801), mission(16, 500000, tag=801, span=10, start=PAST[0], end=PAST[1]),
                    mission(0, 100001, enable=False, tag=801), mission(7, 900001, tag=-10, span=20),
                    mission(14, 920001))
    assert resolved(subject, 801) == 500000 and resolved(subject, 811) == 500000 and resolved(subject, 812) is None
    assert not subject.is_open("Tag", 801, now=NOW, platform_code=PLATFORM)
    assert subject.lookup("Tag", 801, PLATFORM) is subject.tag_mission(801, PLATFORM)
    assert resolved(subject, 5) == 900001
    assert all(subject.tag_mission(tag, PLATFORM) is None for tag in (0, -5, INT32_MIN))


@pytest.mark.parametrize("tag,span,expected", [
    (140, 10, 400000), (140, 9, 400001), (151, 0, 400001), (150, 0, 400000), (100, INT32_MAX - 50, 400001),
    (-1, INT32_MIN, 400000), (150, -1, 400001),
], ids=("span_reaches_tag", "span_stops_short", "tag_above", "exact_tag", "end_wraps_negative", "end_wraps_positive",
        "negative_span"))
def test_tag_spans_end_where_the_native_32_bit_add_wraps(tag, span, expected):
    subject = index(mission(8, 400000, tag=tag, span=span), mission(8, 400001, tag=DEVIL_MENU_TAG))
    assert resolved(subject, DEVIL_MENU_TAG) == expected


def test_platform_masks_register_missions_per_platform_and_booleans_on_every_platform():
    subject = index(mission(2, 400183, enable=0b111, tag=140), mission(2, 400587, enable=(1 << 11) | (1 << 12), tag=140),
                    mission(7, 900001, tag=302), mission(15, 960001, enable=False, tag=302), mission(1, 200384, enable=0, tag=302),
                    mission(HISTORY_CATEGORY, 920002, enable=1 << 5, tag=160))
    assert [resolved(subject, 140, platform) for platform in (0, 1, 2, 3, 11, 12, 13, 31)] == [
        400183, 400183, 400183, None, 400587, 400587, None, None]
    assert {resolved(subject, 302, platform) for platform in range(32)} == {900001}
    assert {resolved(subject, 160, platform) for platform in range(32)} == {920002}


def test_id_lookups_only_return_missions_registered_on_the_requested_platform():
    subject = index(mission(2, 400183, enable=1 << OTHER_PLATFORM, tag=140), mission(15, 960001, enable=False),
                    mission(7, 900001))
    assert subject.lookup("ID", 400183, OTHER_PLATFORM).mission_id == 400183
    assert subject.lookup("ID", 400183, PLATFORM) is None and subject.lookup("ID", 960001, PLATFORM) is None
    assert all(subject.lookup("ID", identifier, PLATFORM) is None for identifier in (0, -900001, 123456))
    assert subject.lookup("ID", 900001, PLATFORM) is subject.registry.mission(900001)


def test_cross_category_duplicate_missions_never_form_a_registry():
    with pytest.raises(ValueError):
        registry(mission(0, 100001), mission(5, 100001))


def test_every_public_method_validates_the_platform_code_first():
    subject = index(mission(7, 900001, tag=302))
    calls = (lambda code: subject.lookup("Tag", 302, code), lambda code: subject.tag_mission(302, code),
             lambda code: subject.is_open("Tag", 302, now=NOW, platform_code=code),
             lambda code: subject.displayable("Tag", 302, now=NOW, platform_code=code),
             lambda code: subject.rank_reached("Tag", 302, rank=1, platform_code=code),
             lambda code: subject.explain("Tag", 302, platform_code=code), lambda code: subject.status(NOW, code))
    for call in calls:
        for code in (-1, 32, True, "1", 1.0):
            fail("quest_platform_invalid", 400, lambda call=call, code=code: call(code))
    fail("quest_platform_invalid", 400, lambda: subject.lookup("tag", "302", 32))
    fail("quest_platform_invalid", 400, lambda: subject.is_open("Tag", 302, now=None, platform_code=None))


@pytest.mark.parametrize("kind,value", [
    ("tag", 302), ("TAG", 302), ("Id", 900001), (None, 302), (1, 302), ("Tag", True), ("Tag", "302"), ("Tag", 302.0),
    ("Tag", None), ("ID", INT32_MAX + 1), ("Tag", INT32_MIN - 1),
])
def test_lookups_require_the_native_kinds_and_integers(kind, value):
    subject = index(mission(7, 900001, tag=302))
    fail("quest_tag_invalid", 400, lambda: subject.lookup(kind, value, PLATFORM))
    fail("quest_tag_invalid", 400, lambda: subject.explain(kind, value, platform_code=PLATFORM))
    if kind == "Tag":
        fail("quest_tag_invalid", 400, lambda: subject.tag_mission(value, PLATFORM))


def test_clocks_must_be_aware_even_when_nothing_resolves_and_ranks_must_be_native():
    subject = index(mission(7, 900001, tag=302))
    for tag in (302, 190):
        for call in (subject.is_open, subject.displayable):
            fail("quest_clock_invalid", 503, lambda call=call, tag=tag: call(
                "Tag", tag, now=datetime(2026, 11, 2, 12), platform_code=PLATFORM))
    fail("quest_clock_invalid", 503, lambda: subject.explain("Tag", 302, now="2026-11-02", platform_code=PLATFORM))
    for rank in ("50", None, True, 2 ** 31):
        with pytest.raises(ValueError) as error:
            subject.rank_reached("Tag", 190, rank=rank, platform_code=PLATFORM)
        assert not isinstance(error.value, ProtocolError)


def test_tag_resolution_is_memoized_per_tag_and_platform_and_cleared_when_full(monkeypatch):
    subject = index(mission(7, 900001, tag=302))
    first = subject.tag_mission(302, PLATFORM)
    assert first.mission_id == 900001
    assert subject.tag_mission(0, PLATFORM) is None and subject.status()["memo"] == {"entries": 1, "limit": MEMO_LIMIT}
    monkeypatch.setattr(subject, "_tag_walk", ())
    assert subject.tag_mission(302, PLATFORM) is first and subject.lookup("Tag", 302, PLATFORM) is first
    assert subject.tag_mission(302, OTHER_PLATFORM) is None
    for tag in range(1000, 1000 + MEMO_LIMIT - 2):
        subject.tag_mission(tag, PLATFORM)
    assert subject.status()["memo"]["entries"] == MEMO_LIMIT and subject.tag_mission(302, PLATFORM) is first
    subject.tag_mission(INT32_MAX, PLATFORM)
    assert subject.status()["memo"]["entries"] == 1 and subject.tag_mission(302, PLATFORM) is None


def test_concurrent_resolutions_stay_consistent_while_the_memo_clears():
    subject = index(mission(7, 900001, tag=1, span=INT32_MAX - 1))
    workers = 4

    def resolve(first):
        return {resolved(subject, tag) for tag in range(first, first + MEMO_LIMIT)}

    with ThreadPoolExecutor(workers * 2) as pool:
        resolutions = [pool.submit(resolve, 1 + offset * MEMO_LIMIT) for offset in range(workers)]
        statuses = [pool.submit(subject.status) for _ in range(workers)]
    assert [future.result() for future in resolutions] == [{900001}] * workers
    assert all(future.result()["memo"]["entries"] <= MEMO_LIMIT for future in statuses)


@pytest.mark.parametrize("moment,opened", [
    (NOW - MILLISECOND, False), (NOW, True), (NOW + timedelta(days=7) - MILLISECOND, True), (NOW + timedelta(days=7), False),
])
def test_open_missions_follow_their_window_without_the_departure_grace(moment, opened):
    subject = index(mission(7, 900001, tag=302, start=NOW, end=NOW + timedelta(days=7)), mission(7, 900002, tag=303, start=NOW))
    assert subject.is_open("Tag", 302, now=moment, platform_code=PLATFORM) is opened
    assert subject.is_open("ID", 900001, now=moment.astimezone(timezone.utc), platform_code=PLATFORM) is opened
    assert not subject.is_open("Tag", 303, now=moment, platform_code=PLATFORM)
    assert not subject.is_open("Tag", 190, now=moment, platform_code=PLATFORM)


def test_displayable_needs_an_open_mission_with_a_member_open_today():
    rows = {101: {"QuestID": 101, "OpenWeek": TUESDAY}, 102: {"QuestID": 102, "OpenWeek": MONDAY},
            103: {"QuestID": 103, "OpenWeek": TUESDAY}}
    subject = index(mission(5, 700101, tag=130, quests=(101, 102)), mission(5, 700102, tag=131, quests=(103, 104)),
                    mission(16, 940099, tag=132, quests=()),
                    mission(5, 700104, tag=133, start=PAST[0], end=PAST[1], quests=(102,)), quest_rows=rows)
    tuesday = NOW + timedelta(days=1)
    assert subject.displayable("Tag", 130, now=NOW, platform_code=PLATFORM)
    assert not subject.displayable("Tag", 131, now=NOW, platform_code=PLATFORM)
    assert subject.displayable("Tag", 131, now=tuesday, platform_code=PLATFORM)
    assert not subject.displayable("Tag", 132, now=NOW, platform_code=PLATFORM)
    assert subject.is_open("Tag", 132, now=NOW, platform_code=PLATFORM)
    assert not subject.displayable("Tag", 133, now=NOW, platform_code=PLATFORM)
    assert subject.displayable("ID", 700101, now=NOW, platform_code=PLATFORM)
    broken = index(mission(5, 700101, tag=130, quests=(101,)), quest_rows={101: {"QuestID": 101, "OpenWeek": "1"}})
    fail("quest_definition_invalid", 503, lambda: broken.displayable("Tag", 130, now=NOW, platform_code=PLATFORM))


def test_rank_requirements_use_the_lowest_member_rank_whether_or_not_the_mission_is_open():
    rows = {4497: {"QuestID": 4497, "Rank": DEVIL_RANK}, 4498: {"QuestID": 4498, "Rank": DEVIL_RANK + 10}}
    subject = index(mission(DEVIL_CATEGORY, DEVIL_MENU_MISSION, tag=DEVIL_MENU_TAG, start=PAST[0], end=PAST[1],
                            quests=(4497, 4498, 4499)),
                    mission(HISTORY_CATEGORY, 920091, tag=160, quests=()), quest_rows=rows)
    assert not subject.is_open("Tag", DEVIL_MENU_TAG, now=NOW, platform_code=PLATFORM)
    assert subject.rank_reached("Tag", DEVIL_MENU_TAG, rank=DEVIL_RANK, platform_code=PLATFORM)
    assert not subject.rank_reached("Tag", DEVIL_MENU_TAG, rank=DEVIL_RANK - 1, platform_code=PLATFORM)
    assert subject.rank_reached("ID", 920091, rank=NO_MISSION_RANK, platform_code=PLATFORM)
    assert not subject.rank_reached("Tag", 160, rank=NO_MISSION_RANK - 1, platform_code=PLATFORM)
    assert not subject.rank_reached("Tag", 190, rank=INT32_MAX, platform_code=PLATFORM)


def test_explanations_list_covering_missions_with_registration_state_and_shadowing():
    subject = index(mission(0, 100001, tag=302, quests=()), mission(7, 900001, tag=302, quests=(1001,)),
                    mission(DEVIL_CATEGORY, 500092, enable=1 << 5, tag=300, span=5),
                    mission(14, 920001, tag=302, start=PAST[0], end=PAST[1]),
                    quest_rows={1001: {"QuestID": 1001, "Rank": 1}})
    report = subject.explain("Tag", 302, now=NOW, platform_code=PLATFORM)
    assert {key: report[key] for key in ("kind", "value", "platform_code", "mission", "reason", "open", "minimum_rank",
                                         "candidate_count", "registered_count")} == {
        "kind": "Tag", "value": 302, "platform_code": PLATFORM, "mission": 900001, "reason": None, "open": True,
        "minimum_rank": 1, "candidate_count": 4, "registered_count": 2}
    assert [(entry["mission_id"], entry["created"], entry["registered"], entry["state"], entry["shadowed_by"])
            for entry in report["candidates"]] == [
        (100001, False, False, "open", None), (500092, True, False, "open", None),
        (900001, True, True, "open", None), (920001, True, True, "ended", 900001)]
    assert report["candidates"][1] == {"mission_id": 500092, "category": DEVIL_CATEGORY, "enable": 1 << 5, "start": None,
                                       "end": None, "tag": 300, "tag_span": 5, "created": True, "registered": False,
                                       "state": "open", "shadowed_by": None}
    assert report["clock"] == "2026-11-02T12:00:00.000000+09:00"
    timeless = subject.explain("Tag", 302, platform_code=PLATFORM)
    assert (timeless["clock"], timeless["open"], timeless["candidates"][1]["state"]) == (None, None, None)
    assert subject.explain("Tag", 0, platform_code=PLATFORM)["reason"] == "tag_not_positive"
    assert subject.explain("Tag", 190, platform_code=PLATFORM)["reason"] == "no_mission"
    assert subject.explain("ID", 100001, platform_code=PLATFORM)["reason"] == "not_registered"
    assert subject.explain("ID", 500092, platform_code=5)["candidates"][0]["registered"]


def test_explanations_bound_their_candidate_lists():
    subject = index(*(mission(14, 920000 + offset, tag=302) for offset in range(LIST_LIMIT + 5)))
    report = subject.explain("Tag", 302, platform_code=PLATFORM)
    assert report["candidate_count"] == LIST_LIMIT + 5 and len(report["candidates"]) == LIST_LIMIT
    assert {entry["shadowed_by"] for entry in report["candidates"][1:]} == {920000}


def test_list_driven_missions_exist_only_when_their_quest_list_names_them():
    config_created = sorted(CONFIG_CREATED_CATEGORIES)
    subject = index(mission(7, 800001, tag=302, quests=()), mission(7, 900001, tag=302), mission(0, 100001, quests=()),
                    *(mission(category, 990000 + category, tag=990, quests=()) for category in config_created),
                    overrides={800001: PAST, 900001: PAST})
    assert resolved(subject, 302) == 900001 and resolved(subject, 990) == 990000 + config_created[0]
    assert subject.lookup("ID", 800001, PLATFORM) is None and subject.lookup("ID", 100001, PLATFORM) is None
    assert [subject.lookup("ID", 990000 + category, PLATFORM).category for category in config_created] == config_created
    report = subject.explain("Tag", 302, now=NOW, platform_code=PLATFORM)
    assert [(entry["mission_id"], entry["created"], entry["registered"], entry["shadowed_by"])
            for entry in report["candidates"]] == [(800001, False, False, None), (900001, True, True, None)]
    assert subject.explain("ID", 800001, platform_code=PLATFORM)["reason"] == "not_registered"
    status = subject.status()
    assert (status["missions"], status["created_missions"], status["config_only_count"], status["config_only"]) == (
        7, 5, 2, [100001, 800001])
    assert (status["categories"][7]["missions"], status["categories"][7]["created"]) == (2, 1)
    assert [status["categories"][category]["creation"] for category in config_created] == ["config"] * 4
    assert {entry["mission_id"]: entry["effective"] for entry in status["overrides"]} == {800001: False, 900001: True}


def test_tower_tag_columns_are_never_read():
    subject = index(mission(TOWER_CATEGORY, 300001, tag=300, span=10), mission(7, 900001, tag=302))
    assert resolved(subject, 302) == 900001 and resolved(subject, 300) is None
    assert subject.lookup("ID", 300001, PLATFORM).category == TOWER_CATEGORY
    assert [entry["mission_id"] for entry in subject.explain("Tag", 302, platform_code=PLATFORM)["candidates"]] == [900001]
    assert subject.explain("Tag", 300, platform_code=PLATFORM)["reason"] == "no_mission"
    status = subject.status(NOW, PLATFORM)
    assert (status["tagged_missions"], status["categories"][TOWER_CATEGORY]["tagged"]) == (1, 0)
    assert [entry["value"] for entry in status["live"]["lookups"]] == [302]


def test_status_reports_the_registry_overrides_consumers_and_live_lookups():
    overrides = {900001: PAST, 960001: (None, None), 123456: (None, None)}
    subject = index(mission(7, 900001, tag=302, quests=(1001,)), mission(15, 960001, enable=False, tag=302),
                    mission(12, 930001, enable=NATIVE_MASK), mission(5, 700101, tag=130, span=1),
                    quest_rows={1001: {"QuestID": 1001, "Rank": 7}}, overrides=overrides, untagged=(12,))
    status = subject.status()
    assert (status["missions"], status["created_missions"], status["tagged_missions"], status["server_provided"]) == (
        4, 4, 3, [4])
    assert (status["config_only_count"], status["config_only"]) == (0, [])
    assert status["categories_without_tag_column"] == [12] and status["master_snapshot"] == MASTER
    assert status["categories"][12] == {"name": "Tower", "tables": ["TowerMissionConfig.atb", "TowerMissionQuestList.atb"],
                                        "creation": "quest_list", "missions": 1, "created": 1, "listed": 1, "tagged": 0,
                                        "tag_column": False, "tag_read": False}
    assert status["categories"][15] == {"name": "Colosseum", "tables": ["ColosseumMissionConfig.atb",
                                                                        "ColosseumMissionQuestList.atb"],
                                        "creation": "config", "missions": 1, "created": 1, "listed": 0, "tagged": 1,
                                        "tag_column": True, "tag_read": True}
    assert status["override_count"] == 3 and [(entry["mission_id"], entry["known"], entry["effective"])
                                              for entry in status["overrides"]] == [
        (123456, False, False), (900001, True, True), (960001, True, False)]
    assert status["overrides"][1]["start"] == "2026-01-01T00:00:00.000000+09:00"
    assert "live" not in status and status["consumers"] == [] and status["content_version"] == subject.content_version
    consumers = {("Tag", 302): HISTORY_GATE, ("ID", 960001): ("console",)}
    live = subject.status(NOW, PLATFORM, consumers)
    assert live["consumers"] == [{"kind": "Tag", "value": 302, "labels": list(HISTORY_GATE)},
                                 {"kind": "ID", "value": 960001, "labels": ["console"]}]
    lookups = live["live"]["lookups"]
    assert [(entry["kind"], entry["value"]) for entry in lookups] == [("Tag", 302), ("ID", 960001), ("Tag", 130)]
    assert lookups[0] == {"kind": "Tag", "value": 302, "consumers": list(HISTORY_GATE), "mission": 900001, "category": 7,
                          "state": "ended", "open": False, "displayable": False, "minimum_rank": 7}
    assert (lookups[1]["mission"], lookups[1]["open"], lookups[2]["mission"], lookups[2]["state"]) == (
        None, False, 700101, "open")
    assert live["live"]["clock"] == "2026-11-02T12:00:00.000000+09:00" and live["live"]["platform_code"] == PLATFORM
    with pytest.raises(ValueError):
        subject.status(NOW)
    with pytest.raises(ValueError):
        subject.status(platform_code=PLATFORM)
    fail("quest_clock_invalid", 503, lambda: subject.status(datetime(2026, 11, 2, 12), PLATFORM))


@pytest.mark.parametrize("consumers", [[("Tag", 302)], {302: ("menu",)}, {("Tag",): ("menu",)}, {("Tag", 302): "menu"},
                                       {("Tag", 302): ("menu", 7)}, {("Tag", 302): ("",)}])
def test_status_consumers_must_map_lookups_to_labels(consumers):
    with pytest.raises(ValueError) as error:
        index(mission(7, 900001, tag=302)).status(consumers=consumers)
    assert not isinstance(error.value, ProtocolError)


def test_status_consumers_use_the_native_lookup_kinds():
    fail("quest_tag_invalid", 400, lambda: index().status(consumers={("tag", 302): ("menu",)}))
    fail("quest_tag_invalid", 400, lambda: index().status(consumers={("Tag", "302"): ("menu",)}))


def test_content_versions_follow_sources_master_and_override_instants_but_not_the_clock():
    base = index(mission(7, 900001, tag=302))
    versions = {base.content_version, index(mission(7, 900001, tag=302), overrides={900001: (None, None)}).content_version,
                index(mission(7, 900001, tag=302), overrides={900001: PAST}).content_version,
                index(mission(7, 900001, tag=302), sources={**SOURCES, REGISTRY_TABLES[0]: "1" * 64}).content_version,
                index(mission(7, 900001, tag=302), master="other-master").content_version}
    assert len(versions) == 5
    utc = tuple(moment.astimezone(timezone.utc) for moment in PAST)
    assert (index(mission(7, 900001, tag=302), overrides={900001: utc}).content_version
            == index(mission(7, 900001, tag=302), overrides={900001: PAST}).content_version)
    assert index(mission(7, 900001, tag=302)).content_version == base.content_version
    before = base.content_version
    base.status(NOW, PLATFORM)
    assert base.is_open("Tag", 302, now=NOW, platform_code=PLATFORM) and base.content_version == before


@pytest.mark.parametrize("name,value", [("CONFIG_CREATED_CATEGORIES", frozenset((HISTORY_CATEGORY,))),
                                        ("TAGLESS_CATEGORIES", frozenset()), ("RULES", MappingProxyType({}))])
def test_content_versions_follow_the_native_registry_rules(monkeypatch, name, value):
    before = index(mission(7, 900001, tag=302)).content_version
    monkeypatch.setattr(mission_tags, name, value)
    assert index(mission(7, 900001, tag=302)).content_version != before


@pytest.mark.parametrize("change", [
    {"registry": MissionWindows({0: ([], [])}, {})},
    {"registry": MissionWindows({category: ([], []) for category in reversed(REGISTRY_CATEGORIES)}, {})},
    {"source_sha256": {name: digest for name, digest in SOURCES.items() if name != REGISTRY_TABLES[-1]}},
    {"source_sha256": {**SOURCES, "Extra.atb": "0" * 64}},
    {"source_sha256": {**SOURCES, REGISTRY_TABLES[0]: "A" * 64}},
    {"source_sha256": {**SOURCES, REGISTRY_TABLES[0]: None}},
    {"master_snapshot": ""}, {"master_snapshot": None}, {"quest_rows": [(1001, {})]},
    {"categories_without_tag_column": (4,)}, {"categories_without_tag_column": (True,)},
    {"overrides": {900001: (PAST[1], PAST[0])}}, {"overrides": {0: (None, None)}},
])
def test_tag_indexes_require_the_complete_registry_and_its_provenance(change):
    arguments = {"registry": registry(), "quest_rows": {}, "source_sha256": SOURCES, "master_snapshot": MASTER,
                 "overrides": None, "categories_without_tag_column": (), **change}
    with pytest.raises(ValueError) as error:
        MissionTagIndex(arguments.pop("registry"), **arguments)
    assert not isinstance(error.value, ProtocolError)


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    executable = project.parent / "aigisr_exe"
    if not (project / "runtime/server/masterdata/active.json").is_file() or not executable.is_dir():
        pytest.skip("Verified local master snapshot and client resources are required")
    master = MasterDataStore(project / "runtime/server/masterdata").snapshot()
    manifest, entries = ResourceLibrary(project / "runtime/resources", client_hash=master.client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", executable)
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(executable, asset_reader=reader)

    def build(overrides=None):
        return QuestCatalog(master, reader, possessions=possessions, resource_version=manifest["version"],
                            window_overrides=overrides)

    quests = build()
    return quests, quests.mission_tags(), build


def test_current_registry_reads_32_tables_and_registers_587_missions(current):
    quests, tags, _ = current
    status = tags.status()
    assert len(tags.registry.ordered()) == status["missions"] == 587
    assert {category: entry["missions"] for category, entry in status["categories"].items()} == CURRENT_MISSIONS
    assert (status["config_only"], status["created_missions"]) == (list(CURRENT_CONFIG_ONLY), 587 - len(CURRENT_CONFIG_ONLY))
    assert not any(tags.registry.mission(mission_id).listed for mission_id in CURRENT_CONFIG_ONLY)
    assert tuple(tags.source_sha256) == REGISTRY_TABLES
    assert all(tags.source_sha256[name] == quests.resource_sha256(name) for name in REGISTRY_TABLES)
    assert status["categories_without_tag_column"] == [12] and status["server_provided"] == [4]
    assert tags.master_snapshot == quests.snapshot.identifier and quests.mission_registry() is tags.registry


def test_current_type_windows_match_the_registry_mission_by_mission(current):
    quests, tags, _ = current
    compared = set()
    for quest_type, spec in MODE_SPECS.items():
        windows = quests.mission_windows(quest_type)
        for item in windows.ordered():
            assert tags.registry.mission(item.mission_id) == item
            assert tags.registry.members(item.mission_id) == windows.members(item.mission_id)
            compared.add(item.mission_id)
    assert {item.category for item in tags.registry.ordered() if item.mission_id not in compared} == {15}


def test_current_tags_resolve_like_the_native_mission_manager(current):
    _, tags, _ = current
    history = tags.tag_mission(302, PLATFORM)
    assert (history.mission_id, history.category) == (900001, 7)
    assert tags.is_open("Tag", 302, now=NOW, platform_code=PLATFORM)
    assert tags.explain("Tag", 302, now=NOW, platform_code=PLATFORM)["candidate_count"] == 248
    assert resolved(tags, 801) == 940012 and [resolved(tags, tag) for tag in range(902, 913)] == list(range(940001, 940012))
    assert resolved(tags, 140) == 400183 and resolved(tags, 140, 11) == resolved(tags, 140, 12) == 400587
    assert [resolved(tags, tag) for tag in (190, 401, 411, 501, 601, 701)] == [None] * 6


def test_current_devil_menu_tag_matches_a_scan_of_the_devil_table_on_every_platform(current):
    quests, tags, _ = current
    devil = quests.mission_windows(9).ordered()
    for platform in range(32):
        expected = next((item.mission_id for item in devil if item.covers(DEVIL_MENU_TAG) and item.enabled(platform)), None)
        assert resolved(tags, DEVIL_MENU_TAG, platform) == expected
    assert resolved(tags, DEVIL_MENU_TAG) == DEVIL_MENU_MISSION
    assert tags.rank_reached("Tag", DEVIL_MENU_TAG, rank=DEVIL_RANK, platform_code=PLATFORM)
    assert not tags.rank_reached("Tag", DEVIL_MENU_TAG, rank=DEVIL_RANK - 1, platform_code=PLATFORM)


def test_current_status_resolves_every_resource_tag_within_bounds(current):
    _, tags, _ = current
    live = tags.status(NOW, PLATFORM, {("Tag", 302): HISTORY_GATE})["live"]
    assert live["lookups"][0]["consumers"] == list(HISTORY_GATE) and live["lookups"][0]["mission"] == 900001
    assert len(live["lookups"]) == live["lookup_count"] <= LIST_LIMIT
    assert {entry["value"] for entry in live["lookups"]} == {item.tag for item in tags.registry.ordered() if item.tag > 0}


def test_current_closing_mission_900001_closes_tag_302_without_touching_the_catalog(current):
    quests, tags, build = current
    preview = quests.preview_mission_tags({900001: PAST})
    assert not preview.is_open("Tag", 302, now=NOW, platform_code=PLATFORM)
    assert tags.is_open("Tag", 302, now=NOW, platform_code=PLATFORM) and quests.mission_tags() is tags
    closed = build({900001: PAST}).mission_tags()
    assert not closed.is_open("Tag", 302, now=NOW, platform_code=PLATFORM)
    assert closed.content_version == preview.content_version != tags.content_version

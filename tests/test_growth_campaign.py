from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.growth_campaign import CAMPAIGN_RESOURCE, CampaignEntry, GrowthCampaign
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.native_clock import parse_native_date
from aigis_ps.possessions import PossessionService
from aigis_ps.quest_catalog import QuestCatalog
from aigis_ps.resource_library import ResourceLibrary


SOURCE = "a" * 64
TICKET = "item_consignment_ticket"
OPENS = datetime(2026, 8, 20, 11, tzinfo=JST)
CLOSES = datetime(2026, 8, 27, 11, tzinfo=JST)
INSIDE = datetime(2026, 8, 21, 12, tzinfo=JST)
AFTER = datetime(2026, 11, 2, 12, tzinfo=JST)
MILLISECOND = timedelta(milliseconds=1)


def entry(mission_id, entry_id, quest_id, *, opens="2026/8/20 11:00:00", closes="2026/8/27 11:00:00", **changes):
    row = {"MissionID": mission_id, "DateOpen": opens, "DateClose": closes, "ItemID": TICKET, "ID": entry_id,
           "QuestID": quest_id, "Count": 1, "Ratio": 100}
    row.update(changes)
    return row


def campaign(*rows, overrides=None):
    return GrowthCampaign.from_rows(list(rows), source_sha256=SOURCE, overrides=overrides)


def fail(code, callback, status=503):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == (code, status)
    return caught.value


def test_campaign_windows_open_and_close_on_the_float32_client_clock():
    subject = campaign(entry(700101, 1, 101), entry(700102, 2, 121, opens="2026/8/20 06:00:07",
                                                    closes="2026/8/27 12:00:08"))
    assert not subject.free(700101, 101, OPENS - MILLISECOND) and subject.free(700101, 101, OPENS)
    assert subject.free(700101, 101, CLOSES - MILLISECOND) and not subject.free(700101, 101, CLOSES)
    assert subject.free(700102, 121, datetime(2026, 8, 20, 6, 0, 7, tzinfo=JST))
    assert not subject.free(700102, 121, datetime(2026, 8, 20, 6, 0, 6, 999000, tzinfo=JST))
    assert subject.free(700102, 121, datetime(2026, 8, 27, 12, 0, 7, 999000, tzinfo=JST))
    assert not subject.free(700102, 121, datetime(2026, 8, 27, 12, 0, 8, tzinfo=JST))


def test_quest_zero_covers_the_whole_mission_and_other_quests_only_themselves():
    subject = campaign(entry(700101, 1, 0), entry(700102, 2, 121))
    assert subject.free(700101, 101, INSIDE) and subject.free(700101, 999, INSIDE)
    assert subject.free(700102, 121, INSIDE) and not subject.free(700102, 122, INSIDE)
    assert not subject.free(700103, 121, INSIDE) and not subject.free(700101, 101, AFTER)
    assert subject.entries[0].covers(5) and not subject.entries[1].covers(5)


@pytest.mark.parametrize("bounds", [{"opens": ""}, {"closes": ""}, {"opens": "", "closes": ""}, {"opens": "0"}])
def test_an_unset_bound_waives_the_ticket_at_any_time(bounds):
    subject = campaign(entry(700101, 1, 101, **bounds))
    assert all(subject.free(700101, 101, moment) for moment in (OPENS - MILLISECOND, INSIDE, AFTER))


def test_zero_missionid_continues_the_previous_row_and_leading_rows_are_orphans():
    subject = campaign(entry(0, 1, 101), entry(700101, 2, 101), entry(0, 3, 102), entry(700102, 4, 121),
                       entry(0, 5, 0))
    assert [item.mission_id for item in subject.entries] == [0, 700101, 700101, 700102, 700102]
    assert subject.status()["orphan_rows"] == [0]
    assert subject.free(700101, 102, INSIDE) and subject.free(700102, 999, INSIDE)
    assert not subject.free(700101, 121, INSIDE)


def test_a_later_row_with_the_same_id_replaces_the_earlier_entry():
    subject = campaign(entry(700101, 1, 101), entry(0, 1, 102))
    assert [(item.row, item.quest_id) for item in subject.entries] == [(1, 102)]
    assert not subject.free(700101, 101, INSIDE) and subject.free(700101, 102, INSIDE)
    revived = campaign(entry(700101, 1, 101, opens="$B4AE"), entry(0, 1, 101))
    assert revived.invalid_entries == () and not revived.unverified_missions
    assert revived.free(700101, 101, INSIDE)
    spoiled = campaign(entry(700101, 1, 101), entry(0, 1, 101, closes="2026/8"))
    assert spoiled.entries == () and spoiled.unverified_missions == {700101}
    fail("entrust_source_invalid", lambda: spoiled.free(700101, 101, INSIDE))


@pytest.mark.parametrize(("changes", "code", "field", "keyed"), [
    ({"DateOpen": "2026/8"}, "date_undetermined", "DateOpen", True),
    ({"DateClose": "-1"}, "date_unsupported", "DateClose", True),
    ({"DateOpen": 20260820}, "field_invalid", "DateOpen", True),
    ({"QuestID": "101"}, "field_invalid", "QuestID", True),
    ({"Count": True}, "field_invalid", "Count", True),
    ({"Ratio": 2 ** 31}, "field_invalid", "Ratio", True),
    ({"ItemID": None}, "field_invalid", "ItemID", True),
    ({"ID": "1"}, "field_invalid", "ID", False),
])
def test_an_invalid_entry_only_suspends_its_own_mission(changes, code, field, keyed):
    subject = campaign(entry(700101, 1, 101, **changes), entry(700101, 2, 102), entry(700102, 3, 121))
    assert subject.unverified is None and subject.unverified_missions == {700101}
    fault, = subject.invalid_entries
    assert (fault.code, fault.field, fault.row, fault.mission_id) == (code, field, 0, 700101)
    assert fault.entry_id == (1 if keyed else None)
    error = fail("entrust_source_invalid", lambda: subject.free(700101, 102, INSIDE))
    assert "700101" in str(error)
    assert subject.free(700102, 121, INSIDE) and not subject.free(700102, 122, INSIDE)
    assert subject.status()["invalid_entries"][0]["code"] == code


@pytest.mark.parametrize(("rows", "code", "row"), [
    ([entry(700101, 1, 101), entry("700102", 2, 121)], "mission_id_invalid", 1),
    ([entry(700101, 1, 101), entry(None, 2, 121)], "mission_id_invalid", 1),
    ([entry(700101, 1, 101), {key: value for key, value in entry(0, 2, 121).items() if key != "Ratio"}],
     "column_missing", 1),
    ([entry(700101, 1, 101), "MissionID"], "rows_invalid", 1),
])
def test_a_broken_mission_chain_or_table_unverifies_every_mission(rows, code, row):
    subject = GrowthCampaign.from_rows(rows, source_sha256=SOURCE)
    assert subject.unverified.code == code and subject.unverified.row == row
    assert len(subject.entries) == 1 and subject.status()["verified"] is False
    for mission_id, quest_id in ((700101, 101), (700102, 121), (1, 1)):
        error = fail("entrust_source_invalid", lambda: subject.free(mission_id, quest_id, INSIDE))
        assert code in str(error)


def test_non_table_rows_and_unavailable_resources_fail_closed():
    for subject in (GrowthCampaign.from_rows({"MissionID": 1}, source_sha256=SOURCE),
                    GrowthCampaign.unavailable("quest_asset_unavailable")):
        assert subject.entries == () and subject.status()["verified"] is False
        fail("entrust_source_invalid", lambda: subject.free(700101, 101, INSIDE))
    unavailable = GrowthCampaign.unavailable("quest_asset_unavailable", source_sha256=SOURCE)
    assert unavailable.status()["unverified"]["code"] == "quest_asset_unavailable"
    assert unavailable.source_sha256 == SOURCE and GrowthCampaign.unavailable("x").source_sha256 is None
    assert unavailable.preview({(700101, 1): (None, None)}).status()["overrides"][0]["known"] is False


def test_overrides_replace_dates_on_the_client_grid_without_touching_the_source():
    base = campaign(entry(700101, 1, 101, opens="2026/8"), entry(700102, 2, 121))
    window = (datetime(2026, 11, 1, 6, 0, 7, 500000, tzinfo=JST), datetime(2026, 11, 8, 11, tzinfo=JST))
    preview = base.preview({(700101, 1): window, (700199, 9): (None, None)})
    assert preview.free(700101, 101, AFTER) and not preview.free(700102, 121, AFTER)
    overridden, = (item for item in preview.entries if item.mission_id == 700101)
    assert overridden.overridden and overridden.opens == parse_native_date("2026/11/1 06:00:07")
    assert overridden.closes == datetime(2026, 11, 8, 11, tzinfo=JST)
    assert [(item["mission_id"], item["known"]) for item in preview.status()["overrides"]] == [(700101, True), (700199, False)]
    assert preview.content_version != base.content_version and preview.invalid_entries == ()
    fail("entrust_source_invalid", lambda: base.free(700101, 101, AFTER))
    assert base.preview(None).content_version == base.content_version
    assert base.preview({(700199, 9): (None, None)}).content_version != base.content_version
    unset = base.preview({(700102, 2): (None, datetime(2026, 8, 1, tzinfo=JST))})
    assert unset.free(700102, 121, AFTER)


@pytest.mark.parametrize("overrides", [
    [((700101, 1), (None, None))],
    {700101: (None, None)},
    {("700101", 1): (None, None)},
    {(700101, True): (None, None)},
    {(700101, 1): (datetime(2026, 8, 20), datetime(2026, 8, 27))},
    {(700101, 1): (OPENS, None)},
    {(700101, 1): (CLOSES, OPENS)},
    {(700101, 1): (OPENS,)},
])
def test_invalid_overrides_are_rejected(overrides):
    with pytest.raises(ValueError):
        campaign(entry(700101, 1, 101), overrides=overrides)


def test_content_version_tracks_rows_sources_and_overrides_but_not_time():
    rows = [entry(700101, 1, 101), entry(0, 2, 102)]
    subject = GrowthCampaign.from_rows(rows, source_sha256=SOURCE)
    assert GrowthCampaign.from_rows(list(rows), source_sha256=SOURCE).content_version == subject.content_version
    assert GrowthCampaign.from_rows(rows, source_sha256="b" * 64).content_version != subject.content_version
    assert campaign(entry(700101, 1, 101), entry(0, 2, 102, closes="2026/8/28")).content_version != subject.content_version
    assert campaign(entry(700101, 1, 101), entry(0, 2, 102, Ratio=5)).content_version != subject.content_version
    assert subject.status(INSIDE)["content_version"] == subject.status(AFTER)["content_version"] == subject.content_version
    rows[0]["QuestID"] = 999
    assert subject.entries[0].quest_id == 101


def test_status_lists_each_entry_with_the_client_clock():
    subject = campaign(entry(700101, 1, 101, Count=2, Ratio=85))
    status = subject.status(INSIDE)
    assert status["verified"] and status["clock"] == "2026-08-21T12:00:00.000+09:00"
    assert status["entries"] == [{"row": 0, "mission_id": 700101, "entry_id": 1, "quest_id": 101,
                                  "opens": "2026-08-20T11:00:00.000+09:00", "closes": "2026-08-27T11:00:00.000+09:00",
                                  "item_id": TICKET, "count": 2, "ratio": 85, "overridden": False, "active_now": True}]
    assert (status["rows"], status["parsed_rows"], status["entry_count"], status["invalid_count"]) == (1, 1, 1, 0)
    assert status["resource"] == CAMPAIGN_RESOURCE and status["source_sha256"] == SOURCE
    assert subject.status()["entries"][0]["active_now"] is None and subject.status()["clock"] is None
    assert subject.status(AFTER)["entries"][0]["active_now"] is False
    assert isinstance(subject.entries[0], CampaignEntry) and subject.entries[0].active(INSIDE)


def test_arguments_are_validated():
    subject = campaign(entry(700101, 1, 101))
    fail("quest_clock_invalid", lambda: subject.free(700101, 101, datetime(2026, 8, 21, 12)))
    fail("quest_clock_invalid", lambda: subject.status("2026-08-21"))
    for mission_id, quest_id in (("700101", 101), (700101, None), (True, 101), (700101, 2 ** 31)):
        with pytest.raises(ValueError):
            subject.free(mission_id, quest_id, INSIDE)
    for source in (None, "A" * 64, "a" * 63, b"a" * 64):
        with pytest.raises(ValueError):
            GrowthCampaign.from_rows([], source_sha256=source)
    for reason in ("", None, 503):
        with pytest.raises(ValueError):
            GrowthCampaign.unavailable(reason)


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
    quests = QuestCatalog(master, reader, possessions=possessions, resource_version=manifest["version"])
    rows = quests.resource_rows(CAMPAIGN_RESOURCE)
    source = quests.status()["mode_resource_sha256"][CAMPAIGN_RESOURCE]
    return quests, rows, GrowthCampaign.from_rows(rows, source_sha256=source)


def test_current_campaign_waives_daily_type2_tickets_inside_its_expired_window(current):
    quests, rows, subject = current
    assert len(rows) == 54 and subject.status()["verified"] and subject.invalid_entries == ()
    assert len(subject.entries) == 54 and subject.status()["orphan_rows"] == []
    assert Counter(item.mission_id for item in subject.entries) == {
        700101: 7, 700102: 7, 700103: 8, 700104: 8, 700105: 8, 700106: 2, 700201: 14}
    pairs = {(item.mission_id, item.quest_id) for item in subject.entries}
    members = {(row["MissionID"], row["QuestID"]) for row in quests.resource_rows("DailyMissionQuestList.atb")}
    assert len(pairs) == 28 and pairs <= members
    assert {quests.row(quest_id)["Type"] for _, quest_id in pairs} == {2}
    assert {(item.opens, item.closes) for item in subject.entries} == {(OPENS, CLOSES)}
    assert not any(item.active(AFTER) for item in subject.entries)
    assert subject.free(700101, 101, INSIDE) and not subject.free(700101, 101, AFTER)
    assert not subject.free(700101, 111, INSIDE) and not subject.free(700104, 101, INSIDE)
    assert subject.source_sha256 == quests.status()["mode_resource_sha256"][CAMPAIGN_RESOURCE]

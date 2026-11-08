from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from aigis_ps.al_format import parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.entrust_catalog import EntrustCatalog
from aigis_ps.growth_campaign import CAMPAIGN_RESOURCE, DATE_FIELDS, GrowthCampaign
from aigis_ps.masterdata import MasterDataStore
import aigis_ps.mission_window_revival as module
from aigis_ps.mission_window_revival import (
    CAMPAIGN_MISMATCH, CAMPAIGN_WINDOW_POLICY, MAX_CAMPAIGNS, GrowthCampaignWindowMismatch, MissionWindowRevival,
    MissionWindowRules, Window, build_mission_window_revival, growth_campaign_missions, load_mission_window_rules,
    parse_campaign_key, verify_growth_campaign_windows, verify_mission_windows,
)
from aigis_ps.native_clock import parse_native_date
from aigis_ps.possessions import PossessionService
from aigis_ps.quest_catalog import QuestCatalog
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.table_cell_patch import patch_table_cells
from test_entrust_catalog import CAMPAIGN_FIELDS, DAILY_MISSIONS, DAILY_QUEST, DAILY_TYPE, NOW, mission, modes
from test_growth_campaign import entry
from test_mission_window_revival import REOPENED as BATCH_WINDOW, world as mission_world
from test_quest_catalog import setup, table


PROJECT = Path(__file__).resolve().parents[1]
EXAMPLE = PROJECT / "config/mission-windows.example.json"
FIRST, SECOND, ABSENT = 700101, 700102, 700103
REOPENED = {"start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"}
OPENING = (datetime(2026, 11, 2, 15, tzinfo=JST), datetime(2026, 11, 16, 10, 59, 59, tzinfo=JST))
OPENING_TEXT = ("2026/11/02 15:00:00", "2026/11/16 10:59:59")
ORIGINAL = {"start_at": "2026-08-20T11:00:00+09:00", "end_at": "2026-08-27T11:00:00+09:00"}
CONSTANT = {"start_at": None, "end_at": None}
COVERING_NOW = {"start_at": "2026-11-01T15:00:00+09:00", "end_at": "2026-11-08T10:59:59+09:00"}
INSIDE = datetime(2026, 11, 9, 12, tzinfo=JST)
# The local PC client sends platform code 9 with every request.
CLIENT_PLATFORM = 9
MILLISECOND = timedelta(milliseconds=1)
# The client reads 15:59:59 four milliseconds early through its float32 day fraction.
SKEWED = {"start_at": REOPENED["start_at"], "end_at": "2026-11-16T15:59:59+09:00"}
SKEWED_END, SKEWED_TEXT = datetime(2026, 11, 16, 15, 59, 59, tzinfo=JST), "2026/11/16 15:59:59"
# Rows 1 and 2 inherit the first mission through MissionID 0; row 4 does the same for the second.
ROWS = (entry(FIRST, 1, 101), entry(0, 2, 103), entry(0, 3, 103, Count=2), entry(SECOND, 4, 121), entry(0, 5, 0))
FIRST_ROWS, SECOND_ROWS = (0, 1, 2), (3, 4)
CURRENT = {700101: (7, ORIGINAL), 700102: (7, ORIGINAL), 700103: (8, ORIGINAL), 700104: (8, ORIGINAL),
           700105: (8, ORIGINAL), 700106: (2, ORIGINAL), 700201: (14, ORIGINAL)}


def campaign_world(*entries):
    return {CAMPAIGN_RESOURCE: table(CAMPAIGN_FIELDS, list(entries or ROWS))}


def rules(campaigns, *, enabled=True, batches=None):
    return MissionWindowRules(enabled, batches or {}, campaigns)


def rows(payload):
    return parse_table(payload)["rows"]


def served(payloads, revival):
    return payloads | dict(revival.replacements)


def dates(row):
    return tuple(row[name] for name in DATE_FIELDS)


def write_rules(tmp_path, value):
    path = tmp_path / "mission-windows.json"
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    return path


def unread(name):
    raise AssertionError(f"{name} was read without enabled growth campaigns")


def test_growth_campaign_keys_name_positive_native_missions():
    assert parse_campaign_key("700101") == FIRST and parse_campaign_key("2147483647") == 2 ** 31 - 1
    for key in ("0", "0700101", "-700101", "+700101", " 700101", "700101 ", "2147483648", "Daily:700101", 700101):
        with pytest.raises(ValueError, match="positive native MissionID"):
            parse_campaign_key(key)


def test_growth_campaigns_normalize_into_their_own_section():
    current = rules({str(SECOND): REOPENED, str(FIRST): CONSTANT})
    assert list(current.growth_campaigns) == [str(FIRST), str(SECOND)]
    assert current.growth_campaigns[str(SECOND)] == Window(*OPENING)
    assert current.to_dict() == {"enabled": True, "batches": {},
                                 "growth_campaigns": {str(FIRST): CONSTANT, str(SECOND): REOPENED}}
    assert MissionWindowRules(**current.to_dict()) == current
    assert rules({str(FIRST): CONSTANT, str(SECOND): REOPENED}).content_version == current.content_version
    assert rules({str(FIRST): REOPENED, str(SECOND): REOPENED}).content_version != current.content_version
    with pytest.raises(TypeError):
        current.growth_campaigns[str(FIRST)] = Window(None, None)


def test_rules_without_growth_campaigns_keep_the_document_and_version_of_batch_rules():
    for current in (rules({}, batches={"Reproduce:300084": BATCH_WINDOW}), MissionWindowRules(True, {}, {})):
        assert "growth_campaigns" not in current.to_dict()
        canonical = json.dumps({"schema": 1, **current.to_dict()}, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        assert current.content_version == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert MissionWindowRules(True, {}, {}).content_version == MissionWindowRules(True, {}).content_version
    assert rules({str(FIRST): REOPENED}).content_version != MissionWindowRules(True, {}).content_version


def test_rule_files_load_the_optional_growth_section(tmp_path):
    loaded = load_mission_window_rules(write_rules(tmp_path, "\ufeff".encode() + json.dumps(
        {"enabled": True, "batches": {}, "growth_campaigns": {str(FIRST): REOPENED}}).encode()))
    assert loaded.to_dict()["growth_campaigns"] == {str(FIRST): REOPENED}
    empty = load_mission_window_rules(write_rules(tmp_path, {"enabled": True, "batches": {}, "growth_campaigns": {}}))
    assert empty == MissionWindowRules(True, {}) and empty.to_dict() == {"enabled": True, "batches": {}}


@pytest.mark.parametrize("campaigns", [
    [],
    {"0": REOPENED},
    {"0700101": REOPENED},
    {"-700101": REOPENED},
    {"2147483648": REOPENED},
    {"Daily:700101": REOPENED},
    {str(FIRST): {"start_at": REOPENED["start_at"]}},
    {str(FIRST): {**REOPENED, "display": "x"}},
    {str(FIRST): {"start_at": REOPENED["start_at"], "end_at": None}},
    {str(FIRST): {"start_at": None, "end_at": REOPENED["end_at"]}},
    {str(FIRST): {"start_at": REOPENED["end_at"], "end_at": REOPENED["start_at"]}},
    {str(FIRST): {**REOPENED, "start_at": "2026-11-02T15:00:00.500+09:00"}},
    {str(FIRST): {**REOPENED, "start_at": "2026-11-02T15:00:00"}},
    {str(FIRST): {**REOPENED, "start_at": "2026/11/02 15:00:00"}},
    {str(FIRST): {**REOPENED, "start_at": "1969-12-31T23:59:59+09:00"}},
    {str(FIRST): {**REOPENED, "end_at": "3001-01-20T00:00:00+09:00"}},
    {str(FIRST): None},
], ids=["list", "zero", "padded", "negative", "overflow", "batch key", "half key", "extra key", "open end", "open start",
        "reversed", "subsecond", "naive", "native text", "before 1970", "after 3001-01-19", "null"])
def test_rule_files_reject_invalid_growth_campaigns(tmp_path, campaigns):
    with pytest.raises(ValueError):
        load_mission_window_rules(write_rules(tmp_path, {"enabled": True, "batches": {}, "growth_campaigns": campaigns}))


def test_growth_windows_reach_the_client_calendar_bounds_but_not_beyond():
    edges = rules({str(FIRST): {"start_at": "1970-01-01T00:00:00+09:00", "end_at": "3001-01-19T23:59:59+09:00"}})
    window = edges.growth_campaigns[str(FIRST)]
    assert (window.start, window.end) == (datetime(1970, 1, 1, tzinfo=JST), datetime(3001, 1, 19, 23, 59, 59, tzinfo=JST))
    for bounds in (("1969-12-31T23:59:59+09:00", REOPENED["end_at"]), (REOPENED["start_at"], "3001-01-20T00:00:00+09:00")):
        with pytest.raises(ValueError, match="client calendar"):
            rules({str(FIRST): dict(zip(("start_at", "end_at"), bounds))})


def test_seconds_the_client_reads_off_its_grid_keep_their_second_and_boundary():
    assert parse_native_date(SKEWED_TEXT) == SKEWED_END - 4 * MILLISECOND
    current, payloads = rules({str(FIRST): SKEWED}), campaign_world()
    revival = build_mission_window_revival(payloads.__getitem__, current)
    after = rows(revival.replacements[CAMPAIGN_RESOURCE])
    assert {dates(after[index]) for index in FIRST_ROWS} == {(OPENING_TEXT[0], SKEWED_TEXT)}
    assert verify_growth_campaign_windows(current, served(payloads, revival).__getitem__)["verification"]["verified"]
    campaign = GrowthCampaign.from_rows(after, source_sha256="a" * 64)
    for moment, state in ((SKEWED_END - MILLISECOND, "open"), (SKEWED_END, "ended")):
        assert campaign.free(FIRST, 101, moment) == (state == "open")
        assert revival.describe(moment)["growth_campaigns"]["missions"][0]["state"] == state
    kept = campaign_world(entry(FIRST, 1, 101, closes=SKEWED_TEXT), entry(SECOND, 2, 121))
    original = growth_campaign_missions(kept.__getitem__)[0].original
    assert original.to_dict() == {"start_at": ORIGINAL["start_at"], "end_at": SKEWED["end_at"]}
    assert build_mission_window_revival(kept.__getitem__, rules({str(FIRST): original.to_dict()})).replacements == {}


def test_growth_campaigns_hold_at_most_their_limit():
    campaigns = {str(FIRST + index): CONSTANT for index in range(MAX_CAMPAIGNS)}
    assert len(rules(campaigns).growth_campaigns) == MAX_CAMPAIGNS
    with pytest.raises(ValueError, match="bounded mapping"):
        rules(campaigns | {str(FIRST + MAX_CAMPAIGNS): CONSTANT})


def test_inventory_groups_rows_by_their_inherited_mission():
    missions = {mission.mission_id: mission for mission in growth_campaign_missions(campaign_world().__getitem__)}
    assert list(missions) == [FIRST, SECOND]
    assert (missions[FIRST].rows, missions[FIRST].quests) == (FIRST_ROWS, (101, 103))
    assert (missions[SECOND].rows, missions[SECOND].quests) == (SECOND_ROWS, (0, 121))
    assert missions[FIRST].original == Window(datetime(2026, 8, 20, 11, tzinfo=JST), datetime(2026, 8, 27, 11, tzinfo=JST))
    assert missions[FIRST].to_dict() == {"mission_id": FIRST, "rows": 3, "quests": [101, 103], "original": ORIGINAL}
    mixed = campaign_world(entry(FIRST, 1, 101), entry(0, 2, 103, closes="2026/8/28 11:00:00"))
    assert growth_campaign_missions(mixed.__getitem__)[0].original is None
    orphaned = campaign_world(entry(0, 1, 101), entry(SECOND, 2, 121))
    assert [mission.mission_id for mission in growth_campaign_missions(orphaned.__getitem__)] == [SECOND]
    with pytest.raises(ValueError, match="resource reader"):
        growth_campaign_missions(None)


def test_patch_rewrites_only_the_dates_of_every_mission_row():
    payloads = campaign_world()
    revival = build_mission_window_revival(payloads.__getitem__, rules({str(FIRST): REOPENED}))
    assert set(revival.replacements) == {CAMPAIGN_RESOURCE}
    assert (revival.targets, revival.resources) == ((), (CAMPAIGN_RESOURCE,))
    before, after = rows(payloads[CAMPAIGN_RESOURCE]), rows(revival.replacements[CAMPAIGN_RESOURCE])
    assert len(before) == len(after) == len(ROWS)
    for index, (old, new) in enumerate(zip(before, after)):
        if index in FIRST_ROWS:
            assert dates(new) == OPENING_TEXT
            old, new = ({key: value for key, value in row.items() if key not in DATE_FIELDS} for row in (old, new))
        assert new == old
    assert [row["MissionID"] for row in after] == [FIRST, 0, 0, SECOND, 0]


def test_written_dates_read_back_through_the_client_parser_and_judge_the_server():
    payloads = campaign_world()
    revival = build_mission_window_revival(payloads.__getitem__, rules({str(FIRST): REOPENED, str(SECOND): CONSTANT}))
    after = rows(revival.replacements[CAMPAIGN_RESOURCE])
    assert all(tuple(map(parse_native_date, dates(after[index]))) == OPENING for index in FIRST_ROWS)
    assert all(dates(after[index]) == ("", "") for index in SECOND_ROWS)
    campaign = GrowthCampaign.from_rows(after, source_sha256="a" * 64)
    assert campaign.unverified is None and campaign.invalid_entries == ()
    assert campaign.free(FIRST, 101, INSIDE) and not campaign.free(FIRST, 101, OPENING[1])
    assert campaign.free(SECOND, 121, datetime(2030, 1, 1, tzinfo=JST))
    original = GrowthCampaign.from_rows(rows(payloads[CAMPAIGN_RESOURCE]), source_sha256="a" * 64)
    assert not original.free(FIRST, 101, INSIDE) and campaign.content_version != original.content_version


def test_dates_already_on_their_bound_keep_their_text_and_reapplying_is_idempotent():
    payloads = campaign_world(entry(FIRST, 1, 101, opens="2026/11/2 15:00:00"), entry(SECOND, 2, 121))
    revival = build_mission_window_revival(payloads.__getitem__, rules({str(FIRST): REOPENED}))
    assert dates(rows(revival.replacements[CAMPAIGN_RESOURCE])[0]) == ("2026/11/2 15:00:00", OPENING_TEXT[1])
    again = build_mission_window_revival(served(payloads, revival).__getitem__, rules({str(FIRST): REOPENED}))
    assert again.replacements == {}
    assert again.metadata["growth_campaigns"]["source_sha256"] == again.metadata["growth_campaigns"]["effective_sha256"]
    unchanged = build_mission_window_revival(campaign_world().__getitem__, rules({str(FIRST): ORIGINAL}))
    assert unchanged.replacements == {} and unchanged.resources == (CAMPAIGN_RESOURCE,)


def test_unknown_missions_and_unverified_tables_are_refused():
    with pytest.raises(ValueError, match=f"{ABSENT} names no mission of {CAMPAIGN_RESOURCE}"):
        build_mission_window_revival(campaign_world().__getitem__, rules({str(ABSENT): REOPENED}))
    unratioed = {CAMPAIGN_RESOURCE: table(tuple(item for item in CAMPAIGN_FIELDS if item[0] != "Ratio"),
                                          [{key: value for key, value in row.items() if key != "Ratio"} for row in ROWS])}
    with pytest.raises(ValueError, match="verified GrowthCampaignConfig.atb: column_missing"):
        build_mission_window_revival(unratioed.__getitem__, rules({str(FIRST): REOPENED}))
    numbered = {CAMPAIGN_RESOURCE: table(tuple((name, 1 if name == "DateOpen" else kind) for name, kind in CAMPAIGN_FIELDS),
                                         [{**row, "DateOpen": 0} for row in ROWS])}
    with pytest.raises(ValueError, match="columns differ from the native contract"):
        build_mission_window_revival(numbered.__getitem__, rules({str(FIRST): REOPENED}))
    with pytest.raises(ValueError, match="unavailable: GrowthCampaignConfig.atb"):
        build_mission_window_revival({}.__getitem__, rules({str(FIRST): REOPENED}))


@pytest.mark.parametrize("stray, reason", [
    (lambda patched: patch_table_cells(patched, {3: {"DateOpen": "2026/11/02 15:00:00"}}), "outside the configured dates"),
    (lambda patched: patch_table_cells(patched, {0: {"Count": 9}}), "outside the configured dates"),
    (lambda patched: patch_table_cells(patched, {1: {"DateClose": "2026/11/16 10:59:58"}}), "client date parser"),
], ids=["other mission", "other cell", "shifted date"])
def test_patches_that_stray_from_the_configured_dates_are_rejected(monkeypatch, stray, reason):
    monkeypatch.setattr(module, "patch_table_cells", lambda source, cells: stray(patch_table_cells(source, cells)))
    with pytest.raises(ValueError, match=reason):
        build_mission_window_revival(campaign_world().__getitem__, rules({str(FIRST): REOPENED}))


def test_absent_or_disabled_growth_rules_read_nothing():
    for current in (rules({str(FIRST): REOPENED}, enabled=False), MissionWindowRules(True, {}, {})):
        revival = build_mission_window_revival(unread, current)
        assert (revival.replacements, revival.campaigns, revival.resources) == ({}, (), ())
        assert "growth_campaigns" not in revival.metadata
        report = verify_growth_campaign_windows(current, unread)
        assert report["verification"] == {"verified": True, "missions": 0, "rows": 0, "served_sha256": None}
    assert verify_growth_campaign_windows(None, unread) == {"enabled": False}


def test_batches_and_growth_campaigns_patch_their_own_tables_from_one_rule_file():
    payloads = mission_world() | campaign_world()
    batches = {"Reproduce:300010": BATCH_WINDOW}
    batch_only = build_mission_window_revival(mission_world().__getitem__, rules({}, batches=batches))
    assert set(batch_only.replacements) == {"ReproduceMissionConfig.atb"} and batch_only.campaigns == ()
    both = build_mission_window_revival(payloads.__getitem__, rules({str(FIRST): REOPENED}, batches=batches))
    assert set(both.replacements) == {"ReproduceMissionConfig.atb", CAMPAIGN_RESOURCE}
    assert both.targets == ("ReproduceMissionConfig.atb",)
    assert both.resources == (CAMPAIGN_RESOURCE, "ReproduceMissionConfig.atb")
    assert both.replacements["ReproduceMissionConfig.atb"] == batch_only.replacements["ReproduceMissionConfig.atb"]
    metadata = both.metadata
    assert set(metadata["tables"]) == {"ReproduceMissionConfig.atb"}
    assert metadata["growth_campaigns"] == {
        "table": CAMPAIGN_RESOURCE, "window_policy": CAMPAIGN_WINDOW_POLICY,
        "source_sha256": hashlib.sha256(payloads[CAMPAIGN_RESOURCE]).hexdigest(),
        "effective_sha256": hashlib.sha256(both.replacements[CAMPAIGN_RESOURCE]).hexdigest(),
        "missions": [{"mission_id": FIRST, "rows": 3, "quests": [101, 103], "original": ORIGINAL, "window": REOPENED}]}
    json.dumps(metadata)
    assert verify_mission_windows(served(payloads, both).__getitem__, both)["verification"]["members"] == 2


def test_describe_reports_campaign_states_on_the_client_clock():
    payloads = campaign_world(*ROWS[:3], entry(SECOND, 4, 121), entry(0, 5, 0, closes="2026/8/28 11:00:00"))
    revival = build_mission_window_revival(payloads.__getitem__, rules({str(FIRST): REOPENED, str(SECOND): CONSTANT}))

    def states(moment):
        report = revival.describe(moment)["growth_campaigns"]
        return {entry["mission_id"]: (entry["state"], entry["original_state"]) for entry in report["missions"]}

    start, end = OPENING
    assert states(start - MILLISECOND) == {FIRST: ("upcoming", "ended"), SECOND: ("open", None)}
    assert states(start)[FIRST] == states(end - MILLISECOND)[FIRST] == ("open", "ended")
    assert states(end)[FIRST] == ("ended", "ended")
    assert states(datetime(2026, 8, 20, 2, tzinfo=timezone.utc))[FIRST] == ("upcoming", "open")
    assert revival.describe(start - MILLISECOND)["growth_campaigns"]["clock"] == "2026-11-02T14:59:59.000+09:00"
    with pytest.raises(ValueError, match="aware datetime"):
        revival.describe(start.replace(tzinfo=None))


def test_verification_reads_every_configured_mission_back_from_the_served_table():
    payloads = campaign_world()
    current = rules({str(FIRST): REOPENED})
    revival = build_mission_window_revival(payloads.__getitem__, current)
    effective = served(payloads, revival)
    report = verify_growth_campaign_windows(current, effective.__getitem__)
    assert report == {"enabled": True, "rules_hash": current.content_version, "table": CAMPAIGN_RESOURCE,
                      "window_policy": CAMPAIGN_WINDOW_POLICY, "missions": [{"mission_id": FIRST, "window": REOPENED}],
                      "verification": {"verified": True, "missions": 1, "rows": 3,
                                       "served_sha256": revival.effective_sha256[CAMPAIGN_RESOURCE]}}
    with pytest.raises(GrowthCampaignWindowMismatch, match=CAMPAIGN_MISMATCH) as caught:
        verify_growth_campaign_windows(current, payloads.__getitem__)
    assert caught.value.code == CAMPAIGN_MISMATCH and isinstance(caught.value, ValueError)
    assert caught.value.mismatches == ({"mission_id": FIRST, "rows": 3, "expected": REOPENED, "effective": [ORIGINAL]},)
    partly = {CAMPAIGN_RESOURCE: patch_table_cells(effective[CAMPAIGN_RESOURCE], {2: {"DateClose": "2026/8/27 11:00:00"}})}
    with pytest.raises(GrowthCampaignWindowMismatch) as caught:
        verify_growth_campaign_windows(current, partly.__getitem__)
    assert caught.value.mismatches[0]["effective"] == [REOPENED, {"start_at": REOPENED["start_at"],
                                                                  "end_at": ORIGINAL["end_at"]}]
    with pytest.raises(GrowthCampaignWindowMismatch) as caught:
        verify_growth_campaign_windows(current, campaign_world(*ROWS[3:]).__getitem__)
    assert caught.value.mismatches[0] == {"mission_id": FIRST, "rows": 0, "expected": REOPENED, "effective": []}
    with pytest.raises(GrowthCampaignWindowMismatch) as caught:
        verify_growth_campaign_windows(current, {}.__getitem__)
    assert caught.value.mismatches[0]["table"] == CAMPAIGN_RESOURCE
    for invalid in (current.to_dict(), "rules", MissionWindowRevival({})):
        with pytest.raises(ValueError, match="verified rules"):
            verify_growth_campaign_windows(invalid, effective.__getitem__)


def test_verification_flags_dates_the_server_cannot_read_and_rows_it_files_elsewhere(monkeypatch):
    payloads = campaign_world()
    current = rules({str(FIRST): REOPENED})
    effective = served(payloads, build_mission_window_revival(payloads.__getitem__, current))
    undetermined = {CAMPAIGN_RESOURCE: patch_table_cells(effective[CAMPAIGN_RESOURCE], {1: {"DateOpen": "2026/11"}})}
    with pytest.raises(GrowthCampaignWindowMismatch) as caught:
        verify_growth_campaign_windows(current, undetermined.__getitem__)
    assert None in caught.value.mismatches[0]["effective"]

    class OrphaningCampaign(GrowthCampaign):
        def _parse(self, rows):
            return super()._parse(tuple({**row, "MissionID": row["MissionID"] or FIRST + 1} for row in rows))

    monkeypatch.setattr(module, "GrowthCampaign", OrphaningCampaign)
    with pytest.raises(GrowthCampaignWindowMismatch) as caught:
        verify_growth_campaign_windows(current, effective.__getitem__)
    assert caught.value.mismatches[0]["mission_id"] == FIRST and caught.value.mismatches[0]["effective"] == [REOPENED]


def test_growth_windows_change_the_entrust_ticket_waiver_through_the_patched_resource(setup):
    plain = modes(setup, campaign=[entry(DAILY_MISSIONS[1], 1, DAILY_QUEST)])
    product = plain.product(DAILY_QUEST)
    assert not plain.ticket_free(product, mission(plain, DAILY_TYPE, DAILY_MISSIONS[1]), NOW)
    resources = setup[0]
    revival = build_mission_window_revival(resources.__getitem__, rules({str(DAILY_MISSIONS[1]): COVERING_NOW}))
    effective = served(resources, revival)
    shared = plain.quests
    quests = QuestCatalog(shared.snapshot, effective.__getitem__, possessions=shared._possessions,
                          resource_version=shared.resource_version)
    subject = EntrustCatalog(quests)
    reopened = subject.product(DAILY_QUEST)
    chosen = mission(subject, DAILY_TYPE, DAILY_MISSIONS[1])
    assert subject.ticket_free(reopened, chosen, NOW) and subject.ticket_cost(reopened, chosen, 3, NOW) == 0
    assert not subject.ticket_free(reopened, chosen, datetime(2026, 11, 8, 1, 59, 59, tzinfo=timezone.utc))
    assert subject.growth.content_version != plain.growth.content_version
    assert subject.content_version != plain.content_version
    assert subject.source_hashes[CAMPAIGN_RESOURCE] == hashlib.sha256(effective[CAMPAIGN_RESOURCE]).hexdigest()
    assert verify_growth_campaign_windows(rules({str(DAILY_MISSIONS[1]): COVERING_NOW}),
                                          effective.__getitem__)["verification"]["verified"]


@pytest.fixture(scope="module")
def native():
    resource_root, executable = PROJECT / "runtime/resources", PROJECT.parent / "aigisr_exe"
    try:
        master = MasterDataStore(PROJECT / "runtime/server/masterdata").snapshot()
        manifest, entries = ResourceLibrary(resource_root, client_hash=master.client_hash).load()
    except (OSError, ValueError):
        pytest.skip("Verified local client resources and public masters are required")
    assets = IndexedAssetService(entries, resource_root, executable)
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(executable, asset_reader=reader)

    def quests(read=reader):
        return QuestCatalog(master, read, possessions=possessions, resource_version=manifest["version"])
    return reader, quests


def test_native_campaigns_group_seven_missions_and_the_example_keeps_their_windows(native):
    reader, _ = native
    missions = growth_campaign_missions(reader)
    assert {mission.mission_id: (len(mission.rows), mission.original.to_dict()) for mission in missions} == CURRENT
    assert sum(len(mission.rows) for mission in missions) == 54
    example = load_mission_window_rules(EXAMPLE)
    assert {parse_campaign_key(key): window.to_dict() for key, window in example.growth_campaigns.items()} == {
        mission_id: window for mission_id, (_, window) in CURRENT.items()}
    revival = build_mission_window_revival(reader, example)
    assert CAMPAIGN_RESOURCE not in revival.replacements
    assert verify_growth_campaign_windows(example, reader)["verification"]["rows"] == 54


def test_native_reopening_patches_the_fourteen_dates_of_one_mission(native):
    reader, _ = native
    current = rules({str(FIRST): REOPENED})
    revival = build_mission_window_revival(reader, current)
    before, after = rows(reader(CAMPAIGN_RESOURCE)), rows(revival.replacements[CAMPAIGN_RESOURCE])
    changed = [index for index, (old, new) in enumerate(zip(before, after)) if old != new]
    assert len(changed) == 7 and all(dates(after[index]) == OPENING_TEXT for index in changed)
    assert {after[index]["MissionID"] for index in changed} == {FIRST, 0}
    effective = lambda name: revival.replacements[name] if name in revival.replacements else reader(name)
    assert verify_growth_campaign_windows(current, effective)["verification"] == {
        "verified": True, "missions": 1, "rows": 7, "served_sha256": revival.effective_sha256[CAMPAIGN_RESOURCE]}
    with pytest.raises(GrowthCampaignWindowMismatch):
        verify_growth_campaign_windows(current, reader)


def test_native_entrust_waives_daily_tickets_inside_a_reopened_campaign(native):
    reader, quests = native
    plain = EntrustCatalog(quests())
    revival = build_mission_window_revival(reader, rules({str(FIRST): REOPENED}))
    subject = EntrustCatalog(quests(lambda name: revival.replacements[name] if name in revival.replacements
                                    else reader(name)))
    assert subject.growth.free(FIRST, 101, INSIDE) and not plain.growth.free(FIRST, 101, INSIDE)
    assert not subject.growth.free(SECOND, 121, INSIDE)
    assert subject.growth.content_version != plain.growth.content_version
    assert subject.content_version != plain.content_version
    assert plain.availability(now=INSIDE, platform_code=CLIENT_PLATFORM)["types"][DAILY_TYPE]["free"] == 0
    # Mission 700101 waives its four daily quests, all open on this Monday.
    assert subject.availability(now=INSIDE, platform_code=CLIENT_PLATFORM)["types"][DAILY_TYPE]["free"] == 4

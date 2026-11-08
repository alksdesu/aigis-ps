from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.config_registry import REGISTRY, ConfigError
from aigis_ps.energy_clock import JST
from aigis_ps.local_io import MAX_JSON_DEPTH
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.mission_catalog import _date
import aigis_ps.mission_window_revival as module
from aigis_ps.mission_window_revival import (
    ALLOWED_CATEGORIES, CONFIG_TABLES, MAX_BATCHES, MAX_RULE_BYTES, QUEST_LISTS, WINDOW_MISMATCH, WINDOW_POLICY,
    MissionWindowMismatch, MissionWindowRevival, MissionWindowRules, Window, build_mission_window_revival,
    load_mission_window_rules, mission_window_batches, parse_batch_key, verify_mission_windows,
)
from aigis_ps.native_clock import NATIVE_EPOCH
from aigis_ps.quest_modes import MISSION_CATEGORIES, OPEN, MissionWindows
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.table_cell_patch import patch_table_cells
from test_storage import NONFINITE
from test_table_cell_patch import milliseconds, native_table


SUBJUGATION, REPRODUCE, RAID, TRAINING = 2, 3, 10, 13
CONFIG = (("MissionID", 1), ("Name", 32), ("Enable", 1), ("StartDateTime", 9), ("EndDateTime", 9))
SUBJUGATION_CONFIG = (*CONFIG, ("SubjugationType", 1))
BOOL_CONFIG = (("MissionID", 1), ("Name", 32), ("Enable", 5), ("StartDateTime", 9), ("EndDateTime", 9))
IDENTITY = (("MissionID", 1), ("Name", 32))
QUEST_LIST = (("MissionID", 1), ("QuestID", 1))
OTHER_MISSIONS = 10_000_000
ALL_PLATFORMS, LEGACY_PLATFORMS = 8191, 2047
GOLD = (datetime(2025, 12, 29, 15, tzinfo=JST), datetime(2026, 1, 8, 11, tzinfo=JST))
RANCE = (datetime(2026, 10, 1, 15, tzinfo=JST), datetime(2026, 10, 15, 10, 59, 59, tzinfo=JST))
LEGACY = (None, datetime(2022, 12, 1, 23, 59, 59, tzinfo=JST))
PERMANENT = (None, None)
REOPENED = {"start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"}
OPENING = (datetime(2026, 11, 2, 15, tzinfo=JST), datetime(2026, 11, 16, 10, 59, 59, tzinfo=JST))
CONSTANT = {"start_at": None, "end_at": None}
RANCE_ROW = 2
REPRODUCE_BATCHES = ("Reproduce:300084", "Reproduce:300217", "Reproduce:320180")
# Batch-only rules keep this version, so clients prepared with the reopened Reproduce batches stay valid.
REPRODUCE_VERSION = "a999deed4681a62b0b1867ac557186974f9f1cf96f5bda999e98202bf0336049"
BATCH_METADATA = frozenset(("schema_version", "enabled", "rules", "rules_hash", "window_policy", "tables", "batches"))
MISSIONS = {
    REPRODUCE: ((300010, "gold 9", ALL_PLATFORMS, GOLD), (300020, "gold 10", ALL_PLATFORMS, GOLD),
                (300030, "rance", ALL_PLATFORMS, RANCE), (300040, "disabled", 0, GOLD),
                (300050, "memberless", ALL_PLATFORMS, GOLD)),
    SUBJUGATION: ((400010, "legacy", LEGACY_PLATFORMS, LEGACY), (400020, "legacy 2", LEGACY_PLATFORMS, LEGACY)),
    RAID: ((900010, "raid", ALL_PLATFORMS, PERMANENT),),
    TRAINING: ((910010, "training", True, LEGACY), (910020, "hidden", False, LEGACY)),
}
QUESTS = {
    REPRODUCE: ((300010, 1001), (300010, 1002), (300010, 1002), (300020, 1003), (300030, 1004), (300040, 1005)),
    SUBJUGATION: ((400010, 2001), (400020, 2002)),
    RAID: ((900010, 3001),),
    TRAINING: ((910010, 4001), (910020, 4002)),
}
STANDARD_SUBJUGATION = 0
# The first shares the window of the standard batch; the second would form a batch of its own.
COOPERATIVE_MISSIONS = ((400030, "coop", LEGACY_PLATFORMS, LEGACY), (400040, "festival", LEGACY_PLATFORMS, RANCE))
COOPERATIVE_KINDS = {400030: 1, 400040: 2}
COOPERATIVE_QUESTS = ((400030, 2003), (400040, 2004))


def words(moment):
    return 0 if moment is None else milliseconds(moment)


def config_rows(missions):
    return [{"MissionID": identifier, "Name": name, "Enable": enable,
             "StartDateTime": words(start), "EndDateTime": words(end)}
            for identifier, name, enable, (start, end) in missions]


def subjugation_table(missions, kinds=None):
    kinds = kinds or {}
    return native_table(SUBJUGATION_CONFIG, [{**row, "SubjugationType": kinds.get(row["MissionID"], STANDARD_SUBJUGATION)}
                                             for row in config_rows(missions)])


def quest_list(members):
    return native_table(QUEST_LIST, [{"MissionID": mission, "QuestID": quest} for mission, quest in members])


def world(changes=None):
    payloads = {}
    for category, name in CONFIG_TABLES.items():
        if category == SUBJUGATION:
            payloads[name] = subjugation_table(MISSIONS[category])
        elif category in MISSIONS:
            payloads[name] = native_table(BOOL_CONFIG if category == TRAINING else CONFIG, config_rows(MISSIONS[category]))
        else:
            payloads[name] = native_table(IDENTITY, [{"MissionID": OTHER_MISSIONS + category, "Name": "other"}])
    for category, name in QUEST_LISTS.items():
        payloads[name] = quest_list(QUESTS[category])
    payloads.update(changes or {})
    return payloads


def cooperative_world():
    return world({CONFIG_TABLES[SUBJUGATION]: subjugation_table(MISSIONS[SUBJUGATION] + COOPERATIVE_MISSIONS,
                                                                COOPERATIVE_KINDS),
                  QUEST_LISTS[SUBJUGATION]: quest_list(QUESTS[SUBJUGATION] + COOPERATIVE_QUESTS)})


def parsed(payload):
    return parse_table(decompress_all4(payload) if payload.startswith(b"ALL4") else payload)


def rows(payload):
    return parsed(payload)["rows"]


def window_of(row):
    return _date(row["StartDateTime"]), _date(row["EndDateTime"])


def rules(batches, enabled=True):
    return MissionWindowRules(enabled, batches)


def effective(payloads, revival):
    return payloads | dict(revival.replacements)


def write_rules(tmp_path, value):
    path = tmp_path / "mission-windows.json"
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    return path


def test_allowed_categories_reuse_the_native_mission_names():
    assert ALLOWED_CATEGORIES == (SUBJUGATION, REPRODUCE, RAID, TRAINING)
    assert set(CONFIG_TABLES.values()) == {f"{kind}MissionConfig.atb" for kind in MISSION_CATEGORIES.values()}
    assert QUEST_LISTS == {category: f"{MISSION_CATEGORIES[category]}MissionQuestList.atb"
                           for category in ALLOWED_CATEGORIES}
    assert parse_batch_key("Reproduce:300084") == (REPRODUCE, 300084)


def test_rules_normalize_bounds_to_jst_and_round_trip():
    current = rules({"Training:910010": CONSTANT, "Reproduce:300010": {
        "start_at": "2026-11-02T06:00:00Z", "end_at": "2026-11-16T10:59:59+09:00"}})
    assert list(current.batches) == ["Reproduce:300010", "Training:910010"]
    assert current.to_dict() == {"enabled": True, "batches": {
        "Reproduce:300010": REOPENED, "Training:910010": CONSTANT}}
    assert current.batches["Reproduce:300010"].start.utcoffset() == timedelta(hours=9)
    assert MissionWindowRules(**current.to_dict()) == current
    assert len(current.content_version) == 64
    assert rules({"Reproduce:300010": REOPENED, "Training:910010": CONSTANT}).content_version == current.content_version
    assert rules(current.batches, enabled=False).content_version != current.content_version
    assert MissionWindowRules().to_dict() == {"enabled": False, "batches": {}}


def test_batch_rules_keep_their_document_content_version_and_metadata():
    current = rules({key: REOPENED for key in REPRODUCE_BATCHES})
    assert current.to_dict() == {"enabled": True, "batches": {key: REOPENED for key in REPRODUCE_BATCHES}}
    assert current.content_version == MissionWindowRules(True, current.batches, {}).content_version == REPRODUCE_VERSION
    revival = build_mission_window_revival(world().__getitem__, rules({"Reproduce:300010": REOPENED}))
    assert revival.campaigns == () and revival.resources == revival.targets == ("ReproduceMissionConfig.atb",)
    assert set(revival.metadata) == set(revival.describe(OPENING[0])) == BATCH_METADATA


def test_rules_accept_verified_window_objects_and_stay_immutable():
    start = datetime(2026, 11, 2, 6, tzinfo=timezone.utc)
    current = rules({"Raid:900010": Window(start, start + timedelta(days=7))})
    assert current.to_dict()["batches"]["Raid:900010"]["start_at"] == "2026-11-02T15:00:00+09:00"
    with pytest.raises(TypeError):
        current.batches["Raid:900010"] = Window(None, None)
    with pytest.raises(FrozenInstanceError):
        current.enabled = False


@pytest.mark.parametrize("window, reason", [
    (Window(NATIVE_EPOCH, OPENING[1]), "native epoch"),
    (Window(OPENING[0].replace(tzinfo=None), OPENING[1]), "explicit UTC offset"),
    (Window(OPENING[0].replace(microsecond=1), OPENING[1]), "whole seconds"),
    (Window(OPENING[0], None), "both bounds or neither"),
    (Window(OPENING[1], OPENING[0]), "precede its end"),
])
def test_window_objects_are_validated_like_rule_files(window, reason):
    with pytest.raises(ValueError, match=reason):
        rules({"Raid:900010": window})


@pytest.mark.parametrize("value", [
    {"enabled": True},
    {"batches": {}},
    {"enabled": True, "batches": {}, "missions": {}},
    {"enabled": 1, "batches": {}},
    {"enabled": True, "batches": []},
    {"enabled": True, "batches": {"300084": REOPENED}},
    {"enabled": True, "batches": {"Reproduce:0300084": REOPENED}},
    {"enabled": True, "batches": {"Reproduce:2147483648": REOPENED}},
    {"enabled": True, "batches": {"reproduce:300084": REOPENED}},
    {"enabled": True, "batches": {"History:920014": REOPENED}},
    {"enabled": True, "batches": {"Tower:110001": REOPENED}},
    {"enabled": True, "batches": {"Reproduce:300084": {"start_at": REOPENED["start_at"]}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "display": "x"}}},
    {"enabled": True, "batches": {"Reproduce:300084": {"start_at": REOPENED["start_at"], "end_at": None}}},
    {"enabled": True, "batches": {"Reproduce:300084": {"start_at": None, "end_at": REOPENED["end_at"]}}},
    {"enabled": True, "batches": {"Reproduce:300084": {"start_at": REOPENED["start_at"], "end_at": REOPENED["start_at"]}}},
    {"enabled": True, "batches": {"Reproduce:300084": {"start_at": REOPENED["end_at"], "end_at": REOPENED["start_at"]}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "start_at": "2026-11-02T15:00:00.500+09:00"}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "start_at": "2026-11-02T15:00:00+09:00:00.5"}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "start_at": "2026-11-02T15:00:00"}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "start_at": "2026-11-02"}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "start_at": "2026/11/02 15:00:00"}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "start_at": 1_762_063_200}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "start_at": "0001-01-01T00:00:00+09:00"}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "start_at": "0001-01-01T00:00:00+14:00"}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "end_at": "9999-12-31T23:59:59-05:00"}}},
    {"enabled": True, "batches": {"Reproduce:300084": {**REOPENED, "start_at": "2026-11-02T15:00:00+09:00" + " " * 64}}},
    {"enabled": True, "batches": {f"Reproduce:{300000 + index}": CONSTANT for index in range(1, MAX_BATCHES + 2)}},
])
def test_rule_files_reject_unknown_keys_and_invalid_windows(tmp_path, value):
    with pytest.raises(ValueError):
        load_mission_window_rules(write_rules(tmp_path, value))


@pytest.mark.parametrize("raw", [
    b'{"enabled": true, "enabled": false, "batches": {}}',
    b'{"enabled": true, "batches": {"Reproduce:300084": {"start_at": null, "start_at": null, "end_at": null}}}',
    b'{"enabled": true, "batches": {"Reproduce:300084": {}, "Reproduce:300084": {}}}',
    b'[]',
    b'{"enabled": true, "batches": {}',
    b'\xff{"enabled": true, "batches": {}}',
])
def test_rule_files_reject_duplicate_keys_and_malformed_json(tmp_path, raw):
    with pytest.raises(ValueError):
        load_mission_window_rules(write_rules(tmp_path, raw))


@pytest.mark.parametrize("number", NONFINITE)
def test_rule_files_refuse_nonfinite_numbers_while_parsing(tmp_path, number):
    path = write_rules(tmp_path, b'{"enabled": ' + number.encode("ascii") + b', "batches": {}}')
    with pytest.raises(ValueError, match="finite"):
        load_mission_window_rules(path)


def test_rule_file_nesting_is_reported_by_the_registry_and_stays_a_value_error(tmp_path):
    path = write_rules(tmp_path, b'{"enabled": false, "batches": {}, "nested": ' + b"[" * MAX_JSON_DEPTH
                       + b"]" * MAX_JSON_DEPTH + b"}")
    with pytest.raises(ValueError, match="nesting"):
        load_mission_window_rules(path)
    with pytest.raises(ConfigError) as error:
        REGISTRY["mission-windows"].load(path)
    assert (error.value.code, error.value.key) == ("config_nesting_too_deep", "mission-windows")


def test_rules_hold_at_most_the_batch_limit():
    batches = {f"Reproduce:{300001 + index}": CONSTANT for index in range(MAX_BATCHES)}
    assert len(rules(batches).batches) == MAX_BATCHES
    with pytest.raises(ValueError, match="bounded mapping"):
        rules(batches | {"Reproduce:299999": CONSTANT})


def test_rule_files_are_size_bounded_and_optional(tmp_path):
    assert load_mission_window_rules(None) is None
    padded = b'{"enabled": false, "batches": {}}' + b" " * MAX_RULE_BYTES
    with pytest.raises(ValueError):
        load_mission_window_rules(write_rules(tmp_path, padded))
    loaded = load_mission_window_rules(write_rules(tmp_path, "\ufeff".encode() + json.dumps(
        {"enabled": True, "batches": {"Reproduce:300084": REOPENED, "Raid:900170": CONSTANT}}).encode()))
    assert loaded.to_dict() == {"enabled": True, "batches": {"Reproduce:300084": REOPENED, "Raid:900170": CONSTANT}}
    assert list(loaded.batches) == ["Reproduce:300084", "Raid:900170"]


def test_batches_group_enabled_members_by_their_original_window():
    batches = {batch.key: batch for batch in mission_window_batches(world().__getitem__)}
    assert list(batches) == ["Subjugation:400010", "Reproduce:300010", "Reproduce:300030", "Raid:900010",
                             "Training:910010"]
    gold = batches["Reproduce:300010"]
    assert gold.mission_ids == (300010, 300020) and gold.table == "ReproduceMissionConfig.atb"
    assert [member.quests for member in gold.members] == [2, 1]
    assert gold.original == Window(*GOLD)
    assert batches["Subjugation:400010"].mission_ids == (400010, 400020)
    assert batches["Subjugation:400010"].original == Window(*LEGACY)
    assert batches["Raid:900010"].original == Window(None, None)
    assert batches["Training:910010"].mission_ids == (910010,)
    assert gold.to_dict() == {"key": "Reproduce:300010", "category": REPRODUCE, "kind": "Reproduce",
        "table": "ReproduceMissionConfig.atb",
        "original": {"start_at": "2025-12-29T15:00:00+09:00", "end_at": "2026-01-08T11:00:00+09:00"},
        "members": [{"mission_id": 300010, "name": "gold 9", "quests": 2},
                    {"mission_id": 300020, "name": "gold 10", "quests": 1}]}


@pytest.mark.parametrize("key, reason", [
    ("Reproduce:300020", "not the smallest; use Reproduce:300010"),
    ("Reproduce:300040", "Enable is 0"),
    ("Training:910020", "Enable is 0"),
    ("Reproduce:300050", "without member quests"),
    ("Reproduce:300099", "names no mission of ReproduceMissionConfig.atb$"),
    ("Reproduce:900010", "belongs to RaidMissionConfig.atb"),
    ("Subjugation:10000000", "belongs to StoryMissionConfig.atb"),
])
def test_batch_keys_must_name_the_smallest_enabled_member_with_quests(key, reason):
    with pytest.raises(ValueError, match=reason):
        build_mission_window_revival(world().__getitem__, rules({key: REOPENED}))


def test_cooperative_subjugations_never_join_a_batch_and_keep_their_windows():
    payloads = cooperative_world()
    batches = {batch.key: batch.mission_ids for batch in mission_window_batches(payloads.__getitem__)
               if batch.category == SUBJUGATION}
    assert batches == {"Subjugation:400010": (400010, 400020)}
    revival = build_mission_window_revival(payloads.__getitem__, rules({"Subjugation:400010": CONSTANT}))
    patched = rows(revival.replacements["SubjugationMissionConfig.atb"])
    assert {row["MissionID"]: window_of(row) for row in patched} == {400010: PERMANENT, 400020: PERMANENT,
                                                                      400030: LEGACY, 400040: RANCE}
    assert {row["MissionID"]: row["SubjugationType"] for row in patched} == {
        400010: STANDARD_SUBJUGATION, 400020: STANDARD_SUBJUGATION, **COOPERATIVE_KINDS}


@pytest.mark.parametrize("key, registration", [("Subjugation:400030", 143), ("Subjugation:400040", 147)])
def test_batch_keys_naming_a_cooperative_subjugation_are_refused(key, registration):
    # Its mission menu sends the request as it loads, so an opened window would loop the client on the refusal.
    with pytest.raises(ValueError, match=f"cooperative subjugation, whose mission menu sends {registration} on entry"):
        build_mission_window_revival(cooperative_world().__getitem__, rules({key: REOPENED}))


def test_batch_keys_follow_the_smallest_identifier_rather_than_the_row_order():
    reversed_rows = native_table(CONFIG, config_rows(MISSIONS[REPRODUCE][::-1]))
    payloads = world({"ReproduceMissionConfig.atb": reversed_rows})
    batches = {batch.key: batch for batch in mission_window_batches(payloads.__getitem__)}
    assert batches["Reproduce:300010"].mission_ids == (300010, 300020)
    with pytest.raises(ValueError, match="use Reproduce:300010"):
        build_mission_window_revival(payloads.__getitem__, rules({"Reproduce:300020": REOPENED}))
    revival = build_mission_window_revival(payloads.__getitem__, rules({"Reproduce:300010": REOPENED}))
    reopened = {row["MissionID"] for row in rows(revival.replacements["ReproduceMissionConfig.atb"])
                if window_of(row) == OPENING}
    assert reopened == {300010, 300020}


@pytest.mark.parametrize("table, schema, row", [
    ("StoryMissionConfig.atb", IDENTITY, {"MissionID": 300020, "Name": "clash"}),
    ("RaidMissionConfig.atb", CONFIG, {"MissionID": 300020, "Name": "clash", "Enable": 0,
                                        "StartDateTime": 0, "EndDateTime": 0}),
])
def test_members_must_be_unique_across_every_mission_table(table, schema, row):
    existing = [] if schema is IDENTITY else config_rows(MISSIONS[RAID])
    payloads = world({table: native_table(schema, existing + [row])})
    with pytest.raises(ValueError, match="once in ReproduceMissionConfig.atb and in no other mission table"):
        build_mission_window_revival(payloads.__getitem__, rules({"Reproduce:300010": REOPENED}))
    unrelated = build_mission_window_revival(payloads.__getitem__, rules({"Reproduce:300030": REOPENED}))
    assert set(unrelated.replacements) == {"ReproduceMissionConfig.atb"}


def test_duplicate_members_within_one_table_are_rejected():
    duplicated = MISSIONS[REPRODUCE] + ((300030, "rance copy", ALL_PLATFORMS, RANCE),)
    payloads = world({"ReproduceMissionConfig.atb": native_table(CONFIG, config_rows(duplicated))})
    with pytest.raises(ValueError, match="found ReproduceMissionConfig.atb, ReproduceMissionConfig.atb"):
        build_mission_window_revival(payloads.__getitem__, rules({"Reproduce:300030": REOPENED}))
    leading = MISSIONS[REPRODUCE] + ((300010, "gold copy", ALL_PLATFORMS, RANCE),)
    payloads = world({"ReproduceMissionConfig.atb": native_table(CONFIG, config_rows(leading))})
    with pytest.raises(ValueError, match="duplicated within ReproduceMissionConfig.atb"):
        build_mission_window_revival(payloads.__getitem__, rules({"Raid:900010": REOPENED}))


def test_revival_patches_every_member_and_keeps_all_other_cells():
    payloads = world()
    revival = build_mission_window_revival(payloads.__getitem__, rules({"Reproduce:300010": REOPENED}))
    assert set(revival.replacements) == {"ReproduceMissionConfig.atb"} and revival.targets == ("ReproduceMissionConfig.atb",)
    before, after = rows(payloads["ReproduceMissionConfig.atb"]), rows(revival.replacements["ReproduceMissionConfig.atb"])
    for old, new in zip(before, after):
        if old["MissionID"] in (300010, 300020):
            assert window_of(new) == OPENING
            old, new = ({key: value for key, value in row.items() if not key.endswith("DateTime")} for row in (old, new))
        assert new == old
    metadata = revival.metadata
    assert metadata["enabled"] is True and metadata["window_policy"] == WINDOW_POLICY
    assert metadata["rules_hash"] == revival.rules.content_version and metadata["rules"]["batches"] == {
        "Reproduce:300010": REOPENED}
    assert metadata["batches"][0]["window"] == REOPENED and metadata["batches"][0]["original"]["end_at"] == (
        "2026-01-08T11:00:00+09:00")
    assert metadata["tables"]["ReproduceMissionConfig.atb"]["effective_sha256"] != (
        metadata["tables"]["ReproduceMissionConfig.atb"]["source_sha256"])
    json.dumps(metadata)


def test_permanent_window_writes_zero_words_for_every_member():
    payloads = world()
    revival = build_mission_window_revival(payloads.__getitem__, rules({"Subjugation:400010": CONSTANT}))
    for row in rows(revival.replacements["SubjugationMissionConfig.atb"]):
        assert row["StartDateTime"] == row["EndDateTime"] == [0, 0]


def test_boolean_enable_tables_are_patched_without_touching_enable():
    payloads = world()
    revival = build_mission_window_revival(payloads.__getitem__, rules({"Training:910010": REOPENED}))
    after = rows(revival.replacements["TrainingMissionConfig.atb"])
    assert [row["Enable"] for row in after] == [True, False]
    assert window_of(after[0]) == OPENING
    assert window_of(after[1]) == LEGACY


def test_several_batches_of_one_table_share_a_single_patch():
    payloads = world()
    revival = build_mission_window_revival(payloads.__getitem__, rules(
        {"Reproduce:300010": REOPENED, "Reproduce:300030": REOPENED, "Raid:900010": REOPENED}))
    assert revival.targets == ("RaidMissionConfig.atb", "ReproduceMissionConfig.atb")
    changed = [row["MissionID"] for row in rows(revival.replacements["ReproduceMissionConfig.atb"])
               if window_of(row) != (GOLD if row["MissionID"] != 300030 else RANCE)]
    assert changed == [300010, 300020, 300030]
    assert [batch.batch.key for batch in revival.batches] == ["Reproduce:300010", "Reproduce:300030", "Raid:900010"]


def test_original_window_is_a_reported_noop_and_reapplying_is_idempotent():
    payloads = world()
    unchanged = build_mission_window_revival(payloads.__getitem__, rules({"Raid:900010": CONSTANT}))
    assert unchanged.replacements == {} and unchanged.targets == ("RaidMissionConfig.atb",)
    table = unchanged.metadata["tables"]["RaidMissionConfig.atb"]
    assert table["source_sha256"] == table["effective_sha256"]
    current = rules({"Reproduce:300030": REOPENED})
    first = build_mission_window_revival(payloads.__getitem__, current)
    second = build_mission_window_revival(effective(payloads, first).__getitem__, current)
    assert second.replacements == {} and second.metadata["tables"] == {"ReproduceMissionConfig.atb": {
        "source_sha256": first.effective_sha256["ReproduceMissionConfig.atb"],
        "effective_sha256": first.effective_sha256["ReproduceMissionConfig.atb"]}}


@pytest.mark.parametrize("stray, reason", [
    (lambda patched, source: patch_table_cells(patched, {0: {"Enable": 0}}), "outside the configured windows"),
    (lambda patched, source: patch_table_cells(patched, {RANCE_ROW: {"Name": "renamed"}}),
     "outside the configured windows"),
    (lambda patched, source: patch_table_cells(source, {RANCE_ROW: {
        "StartDateTime": milliseconds(OPENING[0] + timedelta(seconds=1)), "EndDateTime": milliseconds(OPENING[1])}}),
     "did not apply the configured window"),
    (lambda patched, source: native_table(CONFIG, config_rows(MISSIONS[REPRODUCE][:-1])), "changed the table layout"),
], ids=["other row", "member cell", "shifted window", "dropped row"])
def test_revival_rejects_patches_that_stray_from_the_configured_cells(monkeypatch, stray, reason):
    monkeypatch.setattr(module, "patch_table_cells",
                        lambda source, cells: stray(patch_table_cells(source, cells), source))
    with pytest.raises(ValueError, match=reason):
        build_mission_window_revival(world().__getitem__, rules({"Reproduce:300030": REOPENED}))


@pytest.mark.parametrize("current", [None, MissionWindowRules(), rules({"Reproduce:300010": REOPENED}, enabled=False),
                                     rules({}), MissionWindowRules(False, {}, {"700101": REOPENED})])
def test_absent_disabled_or_empty_rules_read_nothing_and_patch_nothing(current):
    def unavailable(name):
        raise AssertionError(f"{name} was read without enabled batches")
    revival = build_mission_window_revival(unavailable, current)
    assert isinstance(revival, MissionWindowRevival)
    assert revival.replacements == {} and revival.batches == () and revival.targets == ()
    metadata = revival.metadata
    assert metadata["enabled"] is bool(current and current.enabled) and metadata["batches"] == []
    assert metadata["rules"] == (current.to_dict() if current is not None else None)
    assert verify_mission_windows(unavailable, revival)["verification"]["members"] == 0


@pytest.mark.parametrize("current", [{}, {"enabled": True, "batches": {}}, "rules"])
def test_revival_requires_verified_rules(current):
    with pytest.raises(ValueError, match="verified rules"):
        build_mission_window_revival(world().__getitem__, current)


def test_unreadable_or_foreign_resources_are_rejected():
    payloads = world()
    with pytest.raises(ValueError, match="unavailable: ColosseumMissionConfig.atb"):
        build_mission_window_revival({name: value for name, value in payloads.items()
                                      if name != "ColosseumMissionConfig.atb"}.__getitem__, rules({"Raid:900010": REOPENED}))
    foreign = native_table(IDENTITY, [{"MissionID": 900010, "Name": "no dates"}])
    with pytest.raises(ValueError, match="columns differ"):
        build_mission_window_revival(world({"RaidMissionConfig.atb": foreign}).__getitem__, rules({"Raid:900010": REOPENED}))
    untyped = native_table(CONFIG, config_rows(MISSIONS[SUBJUGATION]))
    with pytest.raises(ValueError, match="columns differ from the native contract: SubjugationMissionConfig.atb"):
        build_mission_window_revival(world({"SubjugationMissionConfig.atb": untyped}).__getitem__,
                                     rules({"Raid:900010": REOPENED}))
    with pytest.raises(ValueError, match="native table"):
        build_mission_window_revival(world({"RaidMissionQuestList.atb": b"ALTB"}).__getitem__, rules({"Raid:900010": REOPENED}))
    with pytest.raises(ValueError, match="resource reader"):
        build_mission_window_revival(None, rules({"Raid:900010": REOPENED}))


@pytest.mark.parametrize("changes, reason", [
    ({"RaidMissionQuestList.atb": native_table(QUEST_LIST, [{"MissionID": 900010, "QuestID": 0}])},
     "positive identifiers: RaidMissionQuestList.atb"),
    ({"RaidMissionQuestList.atb": native_table(QUEST_LIST, [{"MissionID": -1, "QuestID": 3001}])},
     "positive identifiers: RaidMissionQuestList.atb"),
    ({"RaidMissionConfig.atb": native_table(CONFIG, config_rows(
        MISSIONS[RAID] + ((0, "zero", ALL_PLATFORMS, PERMANENT),)))}, "must be positive: RaidMissionConfig.atb"),
])
def test_every_allowed_table_requires_positive_identifiers(changes, reason):
    with pytest.raises(ValueError, match=reason):
        build_mission_window_revival(world(changes).__getitem__, rules({"Reproduce:300010": REOPENED}))


def test_verification_reads_every_member_from_the_served_resources():
    payloads = world()
    revival = build_mission_window_revival(payloads.__getitem__, rules({"Reproduce:300010": REOPENED,
                                                                        "Subjugation:400010": CONSTANT}))
    report = verify_mission_windows(effective(payloads, revival).__getitem__, revival)
    assert report["verification"] == {"verified": True, "members": 4, "matches_build": True,
        "served_sha256": dict(revival.effective_sha256)}
    assert [batch["key"] for batch in report["batches"]] == ["Subjugation:400010", "Reproduce:300010"]
    with pytest.raises(MissionWindowMismatch, match=WINDOW_MISMATCH) as caught:
        verify_mission_windows(payloads.__getitem__, revival)
    assert caught.value.code == WINDOW_MISMATCH and isinstance(caught.value, ValueError)
    assert sorted(item["mission_id"] for item in caught.value.mismatches) == [300010, 300020, 400010, 400020]
    first = next(item for item in caught.value.mismatches if item["mission_id"] == 400010)
    assert first["expected"] == CONSTANT and first["effective"] == {"start_at": None,
                                                                   "end_at": "2022-12-01T23:59:59+09:00"}
    assert verify_mission_windows(payloads.__getitem__, None) == {"enabled": False}


def test_verification_reports_missing_members_and_unreadable_tables():
    payloads = world()
    revival = build_mission_window_revival(payloads.__getitem__, rules({"Reproduce:300010": REOPENED}))
    served = effective(payloads, revival)
    without = served | {"ReproduceMissionConfig.atb": native_table(CONFIG, config_rows(MISSIONS[REPRODUCE][1:]))}
    with pytest.raises(MissionWindowMismatch) as caught:
        verify_mission_windows(without.__getitem__, revival)
    assert {(item["mission_id"], item["rows"]) for item in caught.value.mismatches} == {(300010, 0), (300020, 1)}
    with pytest.raises(MissionWindowMismatch) as caught:
        verify_mission_windows({}.__getitem__, revival)
    assert caught.value.mismatches[0]["table"] == "ReproduceMissionConfig.atb"
    with pytest.raises(ValueError, match="built revival"):
        verify_mission_windows(served.__getitem__, rules({"Reproduce:300010": REOPENED}))


def test_describe_reports_native_window_states_at_the_given_instant():
    payloads = world()
    revival = build_mission_window_revival(payloads.__getitem__, rules({"Reproduce:300010": REOPENED,
                                                                        "Raid:900010": CONSTANT}))
    states = {moment: {batch["key"]: (batch["state"], batch["original_state"]) for batch in revival.describe(moment)["batches"]}
              for moment in (datetime(2026, 11, 2, 14, 59, 59, tzinfo=JST), datetime(2026, 11, 2, 6, tzinfo=timezone.utc),
                             datetime(2026, 11, 16, 10, 59, 59, tzinfo=JST))}
    assert list(states.values()) == [
        {"Reproduce:300010": ("upcoming", "ended"), "Raid:900010": ("open", "open")},
        {"Reproduce:300010": ("open", "ended"), "Raid:900010": ("open", "open")},
        {"Reproduce:300010": ("ended", "ended"), "Raid:900010": ("open", "open")}]
    with pytest.raises(ValueError, match="aware datetime"):
        revival.describe(datetime(2026, 11, 2, 15))


def test_revival_results_are_immutable():
    revival = build_mission_window_revival(world().__getitem__, rules({"Reproduce:300010": REOPENED}))
    for mapping in (revival.replacements, revival.source_sha256, revival.effective_sha256, revival.rules.batches):
        with pytest.raises(TypeError):
            mapping["changed"] = b""
    with pytest.raises(FrozenInstanceError):
        revival.batches[0].window = Window(None, None)
    changed = revival.metadata
    changed["batches"][0]["members"].clear()
    assert len(revival.metadata["batches"][0]["members"]) == 2


@pytest.fixture(scope="module")
def native():
    project = Path(__file__).resolve().parents[1]
    resource_root = project / "runtime/resources"
    try:
        client_hash = MasterDataStore(project / "runtime/server/masterdata").expected_hash
        _, entries = ResourceLibrary(resource_root, client_hash=client_hash).load()
        assets = IndexedAssetService(entries, resource_root, project.parent / "aigisr_exe")
        return {name: assets.read(name)[0] for name in (*CONFIG_TABLES.values(), *QUEST_LISTS.values())}
    except (OSError, ValueError):
        pytest.skip("Verified local client resources are required")


def test_native_resources_hold_the_known_revival_batches(native):
    batches = mission_window_batches(native.__getitem__)
    summary = {batch.key: len(batch.members) for batch in batches}
    assert summary == {"Subjugation:400183": 2, "Subjugation:400587": 1,
                       "Reproduce:300084": 13, "Reproduce:300217": 12, "Reproduce:320180": 3,
                       "Raid:900170": 11, "Raid:900380": 3, "Raid:900456": 3, "Raid:900483": 1, "Raid:900580": 1,
                       "Training:910001": 1, "Training:910002": 1}
    by_key = {batch.key: batch for batch in batches}
    assert by_key["Reproduce:320180"].mission_ids == (320180, 320390, 320424)
    assert by_key["Subjugation:400183"].mission_ids == (400183, 400212)
    assert by_key["Reproduce:320180"].original.to_dict() == {
        "start_at": "2026-10-01T15:00:00+09:00", "end_at": "2026-10-15T10:59:59+09:00"}
    assert by_key["Training:910002"].original.to_dict() == {"start_at": None, "end_at": "2026-03-01T23:59:59+09:00"}
    assert sum(summary[key] for key in summary if key.startswith("Reproduce:")) == 28


def test_native_rance_batch_reopens_all_members_and_keeps_every_other_cell(native):
    revival = build_mission_window_revival(native.__getitem__, rules({"Reproduce:320180": REOPENED}))
    assert set(revival.replacements) == {"ReproduceMissionConfig.atb"}
    before, after = rows(native["ReproduceMissionConfig.atb"]), rows(revival.replacements["ReproduceMissionConfig.atb"])
    assert len(before) == len(after) == 50
    changed = 0
    for old, new in zip(before, after):
        if old["MissionID"] in (320180, 320390, 320424):
            changed += 1
            assert window_of(new) == OPENING
            assert {key: value for key, value in new.items() if not key.endswith("DateTime")} == {
                key: value for key, value in old.items() if not key.endswith("DateTime")}
        else:
            assert new == old
    assert changed == 3
    assert parsed(revival.replacements["ReproduceMissionConfig.atb"])["fields"] == parsed(
        native["ReproduceMissionConfig.atb"])["fields"]
    report = verify_mission_windows(effective(native, revival).__getitem__, revival)
    assert report["verification"]["members"] == 3 and report["verification"]["matches_build"] is True
    with pytest.raises(MissionWindowMismatch) as caught:
        verify_mission_windows(native.__getitem__, revival)
    assert sorted(item["mission_id"] for item in caught.value.mismatches) == [320180, 320390, 320424]


def test_native_subjugation_batch_becomes_permanent(native):
    revival = build_mission_window_revival(native.__getitem__, rules({"Subjugation:400183": CONSTANT}))
    before, after = rows(native["SubjugationMissionConfig.atb"]), rows(revival.replacements["SubjugationMissionConfig.atb"])
    for old, new in zip(before, after):
        if old["MissionID"] in (400183, 400212):
            assert new["StartDateTime"] == new["EndDateTime"] == [0, 0]
            assert window_of(new) == (None, None)
        else:
            assert new == old


def test_native_training_batch_with_boolean_enable_is_patched(native):
    revival = build_mission_window_revival(native.__getitem__, rules({"Training:910002": REOPENED}))
    table = parsed(revival.replacements["TrainingMissionConfig.atb"])
    enable = next(item for item in table["fields"] if item["name"] == "Enable")
    assert enable["type_name"] == "bool"
    current = {row["MissionID"]: row for row in table["rows"]}
    assert current[910002]["Enable"] is True and current[910001] == next(
        row for row in rows(native["TrainingMissionConfig.atb"]) if row["MissionID"] == 910001)
    assert window_of(current[910002]) == OPENING


@pytest.mark.parametrize("key, reason", [
    ("Reproduce:320118", "Enable is 0"),
    ("Reproduce:320424", "use Reproduce:320180"),
    ("Reproduce:300100", "use Reproduce:300084"),
    ("Raid:920014", "belongs to HistoryMissionConfig.atb"),
])
def test_native_keys_naming_disabled_or_non_leading_missions_are_rejected(native, key, reason):
    with pytest.raises(ValueError, match=reason):
        build_mission_window_revival(native.__getitem__, rules({key: REOPENED}))


def test_native_reproduce_batches_reopen_together_under_the_server_window_rule(native):
    current = rules({key: REOPENED for key in ("Reproduce:300084", "Reproduce:300217", "Reproduce:320180")})
    revival = build_mission_window_revival(native.__getitem__, current)
    served = effective(native, revival)
    windows = MissionWindows({REPRODUCE: (rows(served["ReproduceMissionConfig.atb"]),
                                          rows(served["ReproduceMissionQuestList.atb"]))}, {})
    inside = datetime(2026, 11, 9, 12, tzinfo=JST)
    reopened = [mission.mission_id for mission in windows.ordered() if mission.listed and mission.state(inside) == OPEN]
    assert len(reopened) == 28 and set(reopened) == {member.mission_id for batch in revival.batches
                                                     for member in batch.batch.members}
    assert verify_mission_windows(served.__getitem__, revival)["verification"]["members"] == 28

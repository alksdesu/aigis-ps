from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
import struct

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.login_reward_catalog import (BEGINNER_FIELDS, GRADE_FIELDS, STAMP_FIELDS,
    LIMITS, LoginReward, LoginRewardCatalog)
from aigis_ps.present_items import LEGACY_ITEMS
from aigis_ps.resource_library import ResourceLibrary
from test_al_format import schema_bytes
from test_present_items import item_table


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


def grade(identifier=0, **changes):
    result = {"_Name": f"grade_{identifier}", "ConfigId": identifier + 1, "_GradeID": identifier,
        "_GradeNameTextID": 100 + identifier, "_UseBorderCount": identifier * 50,
        "_MenuTagID": identifier + 2, "_MenuPattern": identifier, "_ResName": f"Stamp{identifier}.atb",
        "CookGainBorder": 0, "OpenDate": ""}
    result.update(changes)
    return result


def stamp(identifier=1101, **changes):
    result = {"StampID": identifier, "ItemType": 0, "ItemID": "", "ItemParam": 3, "_Param1": 0, "EvolutionCount": 0}
    result.update(changes)
    return result


def beginner(identifier=1, **changes):
    result = {"_ID": identifier, "_Type": 0, "_PresentID": 0, "_SummonConfigID": 0, "_Count": 3}
    result.update(changes)
    return result


def stamp_table(rows):
    fields = [(name, index * 4, kind, b"\0" * 8) for index, (name, kind) in enumerate(STAMP_FIELDS.items()) if name != "ItemID"]
    fields.append(("ItemID", 20, 33, struct.pack("<I", 32)))
    header = schema_bytes(fields, 52)
    records = bytearray()
    for row in rows:
        records += struct.pack("<iiiii", *(row[name] for name, _ in STAMP_FIELDS.items() if name != "ItemID"))
        name = row["ItemID"].encode("utf-8")
        records += name[:32].ljust(32, b"\0")
    start = 20 + len(header)
    return struct.pack("<4sBBHHHII", b"ALTB", 1, 0x10, len(rows), 20, start, 52, 0) + header + records


def resources(*, grades=None, stamps=None, beginners=None):
    grades = [grade(), grade(1)] if grades is None else grades
    stamps = [stamp(), stamp(1131, ItemType=-1, ItemParam=-1)] if stamps is None else stamps
    beginners = [beginner()] if beginners is None else beginners
    payloads = {"StampCardConfig.atb": item_table(grades, tuple(GRADE_FIELDS.items())),
                "BeginnerBonus.atb": item_table(beginners, tuple(BEGINNER_FIELDS.items()))}
    payloads.update({row["_ResName"]: stamp_table(stamps) for row in grades})
    return payloads


def catalog(**kwargs):
    return LoginRewardCatalog(resources(**kwargs).__getitem__, resource_version="fixture")


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    version, entries = ResourceLibrary(project / "runtime/resources", client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    return LoginRewardCatalog(lambda name: assets.read(name)[0], resource_version=version["version"])


def test_current_nine_grades_and_all_837_rows_are_preserved(current):
    assert tuple(row.grade_id for row in current.grades) == tuple(range(9))
    assert tuple(row.config_id for row in current.grades) == tuple(range(1, 10))
    assert tuple(row.minimum_spend for row in current.grades) == (0, 50, 100, 150, 250, 350, 500, 650, 800)
    assert current.status()["stamp_count"] == 837
    assert current.status()["unsupported_reasons"] == {"placeholder": 9}
    assert set(current.months.values()) == {(10, 11, 12)}
    assert len(current.status()["resource_hashes"]) == 11
    for definition in current.grades:
        for month in (10, 11, 12):
            for ordinal in range(1, 32):
                reward = current.stamp(definition.grade_id, month, ordinal)
                assert reward.record_id == month * 100 + ordinal
                assert reward.evolution_count == 0
                if month == 11 and ordinal == 31:
                    assert reward.unsupported_reason == "placeholder"
                else:
                    assert reward.unsupported_reason is None
                    assert reward.to_present_reward("login")["description"] == "login"


@pytest.mark.parametrize("spend,expected", [(0, 0), (49, 0), (50, 1), (99, 1), (100, 2), (149, 2),
    (150, 3), (249, 3), (250, 4), (349, 4), (350, 5), (499, 5), (500, 6), (649, 6),
    (650, 7), (799, 7), (800, 8), (2**31 - 1, 8)])
def test_current_spending_thresholds(current, spend, expected):
    assert current.grade(spend, NOW) == expected


def test_current_tickets_keep_exact_pools_instead_of_falling_back(current):
    for month in (10, 11, 12):
        assert current.stamp(0, month, 1).to_present_reward("stamp") == {
            "kind": "summon_ticket", "pool_id": 350, "count": 1, "description": "stamp"}
        assert current.stamp(0, month, 14).param1 == 350
        assert [current.stamp(level, month, 1).param1 for level in range(9)] == [350, 349, 349, 1, 1, 1, 51, 51, 51]
    assert current.status()["ticket_pools"] == [1, 51, 349, 350]


def test_current_beginner_seven_rewards_are_independent_three_crystal_mails(current):
    for index in range(1, 8):
        reward = current.beginner(index)
        assert reward.source == "beginner" and reward.record_id == index
        assert reward.present_id == reward.param1 == 0
        assert reward.to_present_reward("beginner") == {"kind": "free_crystal", "amount": 3, "description": "beginner"}
    with pytest.raises(ProtocolError) as error:
        current.beginner(8)
    assert error.value.code == "login_reward_position_unavailable"


@pytest.mark.parametrize("month", range(1, 10))
def test_missing_native_months_are_explicitly_unavailable(current, month):
    with pytest.raises(ProtocolError) as error:
        current.stamp(0, month, 1)
    assert error.value.code == "login_reward_month_unavailable" and error.value.status == 501


def test_grade_open_date_is_strict_and_timezone_is_jst():
    instance = catalog(grades=[grade(), grade(1, OpenDate="2026/11/01 04:00:00")])
    opening = datetime(2026, 11, 1, 4, tzinfo=JST)
    assert instance.grade(50, opening - timedelta(microseconds=1)) == 0
    assert instance.grade(50, opening) == 0
    assert instance.grade(50, opening + timedelta(microseconds=1)) == 1
    assert instance.grade(50, opening.astimezone(timezone.utc)) == 0
    assert instance.grade(50, datetime(2026, 11, 1, 4, 0, 0, 1)) == 1


def test_no_open_grade_does_not_silently_choose_zero():
    instance = catalog(grades=[grade(OpenDate="2030/01/01 00:00:00")])
    with pytest.raises(ProtocolError) as error:
        instance.grade(0, NOW)
    assert error.value.code == "login_reward_grade_unavailable"


@pytest.mark.parametrize("value", [True, False, -1, 2**31, None, "50"])
def test_bad_spend_values_are_rejected(value):
    with pytest.raises(ProtocolError) as error:
        catalog().grade(value, NOW)
    assert error.value.code == "login_reward_spend_invalid"


@pytest.mark.parametrize("value", [True, None, 0, "2026/11/01"])
def test_bad_clock_values_are_rejected(value):
    with pytest.raises(ProtocolError) as error:
        catalog().grade(0, value)
    assert error.value.code == "login_reward_clock_invalid"


@pytest.mark.parametrize("kind,amount,item,param,expected", [
    (0, 2, "", 0, {"kind": "free_crystal", "amount": 2}),
    (1, 61, "", 0, {"kind": "unit", "template_id": 61, "count": 1}),
    (2, 6500, "", 0, {"kind": "gold", "amount": 6500}),
    (16, 3, "", 350, {"kind": "summon_ticket", "pool_id": 350, "count": 3}),
    (22, 4, "trade_item_005000", 0, {"kind": "item", "item_id": "trade_item_005000", "amount": 4}),
])
def test_to_present_reward_preserves_kind_specific_parameters(kind, amount, item, param, expected):
    assert LoginReward(kind, item, amount, param, 0).to_present_reward("gift") == {**expected, "description": "gift"}


@pytest.mark.parametrize("kind", LEGACY_ITEMS)
def test_all_existing_legacy_mail_types_use_the_shared_present_kind(kind):
    assert LoginReward(kind, "", 7, 0, 0).to_present_reward("gift") == {
        "kind": "legacy_item", "present_type": kind, "amount": 7, "description": "gift"}


@pytest.mark.parametrize("changes,reason", [({"ItemType": 99}, "unverified_reward_type"),
    ({"EvolutionCount": 2}, "unverified_evolution_count"), ({"ItemParam": 0}, "nonpositive_reward"),
    ({"ItemID": "unexpected"}, "unexpected_item_identity"),
    ({"ItemType": 22}, "unexpected_item_identity"), ({"_Param1": 4}, "unexpected_reward_parameter"),
    ({"ItemType": 16}, "unexpected_reward_parameter"),
    ({"ItemType": -1, "ItemParam": -1}, "placeholder")])
def test_unverified_reward_is_retained_with_an_explicit_failure(changes, reason):
    instance = catalog(stamps=[stamp(**changes)])
    reward = instance.stamp(0, 11, 1)
    assert reward.unsupported_reason == reason
    with pytest.raises(ProtocolError) as error:
        reward.to_present_reward("gift")
    assert error.value.code == "login_reward_unsupported"
    assert instance.status()["ready"] is True


def test_every_built_catalog_is_ready_without_building_its_status(current, monkeypatch):
    built = catalog(stamps=[stamp(ItemType=99)])
    for instance in (built, current):
        assert instance.is_ready() is True and instance.status()["ready"] is True
    monkeypatch.setattr(built, "status", lambda: pytest.fail("Readiness must not build the status report"))
    assert built.is_ready() is True


def test_unverified_beginner_fields_are_not_discarded_or_reinterpreted():
    reward = catalog(beginners=[beginner(_Type=1, _PresentID=99, _SummonConfigID=350, _Count=2)]).beginner(1)
    assert (reward.type, reward.present_id, reward.param1, reward.amount) == (1, 99, 350, 2)
    assert reward.unsupported_reason == "unverified_beginner_shape"
    with pytest.raises(ProtocolError):
        reward.to_present_reward("gift")


@pytest.mark.parametrize("args", [(True, 11, 1), (99, 11, 1), (0, True, 1), (0, 0, 1),
                                  (0, 13, 1), (0, 11, False), (0, 11, 0), (0, 11, 32)])
def test_bad_stamp_positions_are_rejected(args):
    with pytest.raises(ProtocolError):
        catalog().stamp(*args)


def test_missing_position_is_not_replaced_by_a_neighbor():
    with pytest.raises(ProtocolError) as error:
        catalog(stamps=[stamp()]).stamp(0, 11, 2)
    assert error.value.code == "login_reward_position_unavailable"


@pytest.mark.parametrize("changes", [{"ConfigId": 0}, {"_GradeID": -1}, {"_UseBorderCount": -1},
    {"CookGainBorder": -1}, {"_Name": ""}, {"OpenDate": "2026/02/30 00:00:00"},
    {"OpenDate": "2026/11-01 00:00:00"}, {"_ResName": "../other.atb"},
    {"_ResName": "C:\\other.atb"}, {"_ResName": "BeginnerBonus.atb"}, {"_ResName": "StampCardConfig.atb"}])
def test_invalid_grade_resource_is_rejected(changes):
    with pytest.raises(ProtocolError) as error:
        catalog(grades=[grade(**changes)])
    assert error.value.code == "login_reward_catalog_invalid"


@pytest.mark.parametrize("grades", [[], [grade(1)], [grade(_UseBorderCount=10)],
    [grade(), grade()], [grade(), grade(1, ConfigId=1)], [grade(), grade(1, _UseBorderCount=0)],
    [grade(), grade(1, _Name="grade_0")]])
def test_ambiguous_grade_orders_are_rejected(grades):
    with pytest.raises(ProtocolError):
        catalog(grades=grades)


@pytest.mark.parametrize("stamps", [[], [stamp(), stamp()], [stamp(1301)], [stamp(1100)],
    [stamp(1132)], [stamp(1131)], [stamp(ItemType=-1)], [stamp(ItemType=-1, ItemParam=-1, _Param1=3)]])
def test_invalid_stamp_identity_and_placeholder_shapes_are_rejected(stamps):
    with pytest.raises(ProtocolError):
        catalog(stamps=stamps)


@pytest.mark.parametrize("beginners", [[], [beginner(0)], [beginner(), beginner()]])
def test_invalid_beginner_identity_is_rejected(beginners):
    with pytest.raises(ProtocolError):
        catalog(beginners=beginners)


def test_immutable_catalog_freezes_resources_and_reward_output():
    source = resources()
    before = deepcopy(source)
    instance = LoginRewardCatalog(source.__getitem__)
    reward = instance.stamp(0, 11, 1)
    with pytest.raises(FrozenInstanceError):
        reward.amount = 9
    with pytest.raises(FrozenInstanceError):
        instance.grades[0].minimum_spend = 5
    with pytest.raises(TypeError):
        instance.by_grade[0] = None
    with pytest.raises(TypeError):
        instance.months[0] = ()
    with pytest.raises(AttributeError):
        instance.content_version = "bad"
    output = reward.to_present_reward("gift")
    output["amount"] = 9
    assert reward.amount == 3 and source == before
    source["Stamp0.atb"] = b"changed"
    assert instance.stamp(0, 11, 1) is reward
    status = instance.status()
    status["resource_hashes"]["Stamp0.atb"] = "changed"
    assert instance.status()["resource_hashes"]["Stamp0.atb"] != "changed"


def test_fingerprint_covers_all_payloads_and_resource_version():
    source = resources()
    original = LoginRewardCatalog(source.__getitem__, resource_version="a").content_version
    assert LoginRewardCatalog(source.__getitem__, resource_version="a").content_version == original
    assert LoginRewardCatalog(source.__getitem__, resource_version="b").content_version != original
    source["Stamp1.atb"] = stamp_table([stamp(ItemParam=4)])
    assert LoginRewardCatalog(source.__getitem__, resource_version="a").content_version != original


@pytest.mark.parametrize("value", [b"", b"broken", None, "ALTB", bytearray(b"ALTB")])
def test_invalid_or_mutable_payloads_are_rejected(value):
    with pytest.raises(ProtocolError):
        LoginRewardCatalog(lambda _: value)


def test_native_all4_resources_are_supported():
    source = resources()
    packed = {name: struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw) for name, raw in source.items()}
    assert LoginRewardCatalog(packed.__getitem__).grades == LoginRewardCatalog(source.__getitem__).grades


def test_resource_size_is_checked_before_parsing():
    with pytest.raises(ProtocolError):
        LoginRewardCatalog(lambda _: b"x" * (LIMITS.max_input_bytes + 1))


@pytest.mark.parametrize("field", ["ConfigId", "_GradeID", "_UseBorderCount", "CookGainBorder"])
def test_boolean_resource_numbers_are_not_accepted(monkeypatch, field):
    row = grade()
    row[field] = False
    monkeypatch.setattr("aigis_ps.login_reward_catalog.parse_table", lambda *args: {
        "fields": [{"name": name, "type_code": kind} for name, kind in GRADE_FIELDS.items()], "rows": [row]})
    with pytest.raises(ProtocolError):
        LoginRewardCatalog(lambda _: b"synthetic")


@pytest.mark.parametrize("field", ["type", "amount", "param1", "evolution_count", "present_id", "record_id"])
def test_direct_reward_numbers_are_strict(field):
    values = {"type": 0, "item_id": "", "amount": 3, "param1": 0, "evolution_count": 0}
    values[field] = True
    with pytest.raises(ValueError):
        LoginReward(**values)

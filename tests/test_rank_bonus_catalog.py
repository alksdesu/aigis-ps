from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import struct

import lz4.frame
import pytest

import aigis_ps.rank_bonus_catalog as module
from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.errors import ProtocolError
from aigis_ps.rank_bonus_catalog import RANK_FIELDS, RANK_RESOURCE, REWARD_FIELDS, REWARD_RESOURCE, RankBonusCatalog
from aigis_ps.resource_library import ResourceLibrary
from test_present_items import item_table


def reward(identifier=78, **changes):
    return {"ID_Reward": identifier, "ID_Incentive": 1, "Type_Condition": 10, "Param_Condition": 10,
            "Param_ValidCondition": 0, "Param_Valid1": 0, "Param_Valid2": 0, "Type_MainIncentive": 5,
            "Type_SubIncentive": 20, "Param_Incentive": 1, "ID_Item": "", **changes}


def display(identifier=78, **changes):
    return {"_ID": identifier, "_Rank": 10, "_SysTxtID": 186, "_FlagID": 11, **changes}


def resources(rewards=None, displays=None):
    return {REWARD_RESOURCE: item_table([reward()] if rewards is None else rewards, tuple(REWARD_FIELDS.items())),
            RANK_RESOURCE: item_table([display()] if displays is None else displays, tuple(RANK_FIELDS.items()))}


def build(rewards=None, displays=None):
    return RankBonusCatalog(resources(rewards, displays).__getitem__)


def invalid(callback):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == ("rank_bonus_catalog_invalid", 503)


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    if not (project / "runtime/resources/active-index.json").is_file():
        pytest.skip("Verified local resource snapshot is required")
    _, entries = ResourceLibrary(project / "runtime/resources", client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    return RankBonusCatalog(lambda name: assets.read(name)[0])


def test_all_473_real_groups_and_118_current_bonuses_are_preserved(current):
    status = current.status()
    assert status["ready"] is True and status["error"] is None
    assert status["reward_groups"] == 473 and status["reward_rows"] == 9177
    assert status["bonuses"] == 118 and len(current.by_id) == 118
    assert status["excluded_rank_reward_ids"] == [94, 129, 179]
    assert status["unsupported_bonus_ids"] == []
    assert status["condition_counts"] == {4: 3337, 5: 1528, 6: 1090, 7: 335, 9: 2766, 10: 121}
    assert len(current.between(0, 0x7fffffff)) == 118


def test_real_display_joins_one_matching_unqualified_rank_condition(current):
    for bonus in current.bonuses:
        source, = current.groups[bonus.bonus_id]
        assert source.condition_type == 10 and source.condition_parameter == bonus.rank
        assert source.valid_condition == source.valid1 == source.valid2 == 0
        assert source.parameter == bonus.parameter and source.item_id == bonus.item_id
        assert source.source_index == bonus.source_index
        assert source.incentive_id == bonus.incentive_id == 1
    assert current.bonuses == tuple(sorted(current.bonuses, key=lambda value: (value.rank, value.config_index)))


def test_real_rank10_and25_selection_tickets_keep_distinct_config_ids(current):
    assert current.by_id[78].to_present_reward("rank") == {
        "kind": "selection_ticket", "config_id": 1, "count": 1, "description": "rank"}
    assert current.by_id[107].to_present_reward("rank") == {
        "kind": "selection_ticket", "config_id": 2, "count": 1, "description": "rank"}
    assert [bonus.bonus_id for bonus in current.between(9, 10)] == [7, 78, 264]
    assert [bonus.bonus_id for bonus in current.between(24, 25)] == [107]
    assert all(identifier not in current.by_id for identifier in (94, 129, 179))


def test_real_zero_parameters_become_one_item_without_hiding_resource_values(current):
    for identifier, item in ((143, "item_smn_ticket_10001"), (144, "item_smn_ticket_10001"),
                             (145, "item_smn_ticket_10001"), (180, "item_smn_ticket_15001")):
        bonus = current.by_id[identifier]
        assert bonus.parameter == 0
        assert bonus.to_present_reward("rank") == {"kind": "item", "item_id": item, "amount": 1, "description": "rank"}


def test_real_new_account_range_has_no_initial_roster_reward(current):
    assert current.between(0, 1) == ()
    assert min(bonus.rank for bonus in current.bonuses) == 5
    assert current.by_id[7].parameter == 135
    assert current.by_id[7].to_present_reward("")["template_id"] == 135


@pytest.mark.parametrize("kind,param,item,expected", [
    (0, 3, "", {"kind": "free_crystal", "amount": 3}),
    (1, 135, "", {"kind": "unit", "template_id": 135, "count": 1}),
    (16, 350, "", {"kind": "summon_ticket", "pool_id": 350, "count": 1}),
    (20, 2, "", {"kind": "selection_ticket", "config_id": 2, "count": 1}),
    (22, 5, "item", {"kind": "item", "item_id": "item", "amount": 5}),
    (22, 0, "item", {"kind": "item", "item_id": "item", "amount": 1}),
    (22, -5, "item", {"kind": "item", "item_id": "item", "amount": 1}),
])
def test_native_reward_mapping_has_no_pool_or_storage_side_effect(kind, param, item, expected):
    bonus = build([reward(Type_SubIncentive=kind, Param_Incentive=param, ID_Item=item)]).bonuses[0]
    assert bonus.to_present_reward("description") == {**expected, "description": "description"}
    assert bonus.parameter == param


@pytest.mark.parametrize("changes", [{"Type_MainIncentive": 7}, {"Type_SubIncentive": 999},
    {"Type_SubIncentive": 20, "Param_Incentive": 0}, {"ID_Item": "unexpected"},
    {"Type_SubIncentive": 22, "ID_Item": ""}])
def test_unknown_reward_shape_remains_visible_but_cannot_generate_mail(changes):
    catalog = build([reward(**changes)])
    assert catalog.status()["ready"] is True
    assert catalog.status()["unsupported_bonus_ids"] == [78]
    assert catalog.between(9, 10) == catalog.bonuses
    with pytest.raises(ProtocolError) as caught:
        catalog.by_id[78].to_present_reward("rank")
    assert (caught.value.code, caught.value.status) == ("rank_bonus_reward_unsupported", 501)


def test_equal_rank_order_uses_display_order_not_reward_id_or_reward_table_order():
    catalog = build([reward(2), reward(1), reward(3, Param_Condition=5)],
                    [display(1), display(2), display(3, _Rank=5)])
    assert [bonus.bonus_id for bonus in catalog.bonuses] == [3, 1, 2]
    assert [bonus.config_index for bonus in catalog.bonuses] == [2, 0, 1]
    assert [bonus.bonus_id for bonus in catalog.between(5, 10)] == [1, 2]


def test_nonrank_groups_and_unlisted_old_rank_rewards_do_not_become_claims():
    catalog = build([reward(), reward(94), reward(500, Type_Condition=4),
                     reward(0, ID_Incentive=2, Type_Condition=7)])
    assert list(catalog.groups) == [78, 94, 500]
    assert len(catalog.groups[500]) == 2
    assert catalog.groups[500][1].reward_id == 500
    assert catalog.between(0, 1000) == (catalog.by_id[78],)
    assert catalog.status()["excluded_rank_reward_ids"] == [94]


@pytest.mark.parametrize("old,new,expected", [(0, 9, 0), (0, 10, 1), (9, 10, 1), (10, 10, 0),
                                           (10, 100, 0), (0, 0, 0), (0, 0x7fffffff, 1)])
def test_rank_interval_is_open_at_old_and_closed_at_new(old, new, expected):
    assert len(build().between(old, new)) == expected


@pytest.mark.parametrize("old,new", [(-1, 10), (11, 10), (True, 10), (0, False), (0, "10"),
                                    (0, 10.0), (None, 10), (0, 0x80000000)])
def test_invalid_rank_intervals_are_not_silently_coerced(old, new):
    with pytest.raises(ProtocolError) as caught:
        build().between(old, new)
    assert caught.value.code == "rank_bonus_range_invalid"


@pytest.mark.parametrize("rewards", [[], [reward(0)], [reward(-1)], [reward(), reward()],
    [reward(), reward(0)], [reward(ID_Incentive=0)], [reward(ID_Incentive=-1)]],
    ids=["empty", "orphan", "negative", "duplicate_group", "duplicate_incentive", "zero_incentive", "negative_incentive"])
def test_invalid_full_reward_group_structure_rejects_catalog(rewards):
    invalid(lambda: build(rewards))


@pytest.mark.parametrize("rewards,displays", [([reward()], []), ([reward()], [display(1)]),
    ([reward()], [display(), display()]), ([reward(), reward(0, ID_Incentive=2)], [display()]),
    ([reward(Type_Condition=4)], [display()]), ([reward()], [display(_Rank=11)]),
    ([reward(Param_ValidCondition=1)], [display()]), ([reward(Param_Valid1=1)], [display()]),
    ([reward(Param_Valid2=1)], [display()]), ([reward()], [display(_FlagID=32)]),
    ([reward()], [display(_SysTxtID=0)]), ([reward()], [display(_Rank=0)])],
    ids=["no_display", "missing_group", "duplicate_display", "multiple_rewards", "wrong_condition", "rank_mismatch",
         "valid_condition", "valid_lower", "valid_upper", "flag_range", "text_zero", "rank_zero"])
def test_rank_join_requires_one_exact_condition10_reward(rewards, displays):
    invalid(lambda: build(rewards, displays))


@pytest.mark.parametrize("name,schema", [(REWARD_RESOURCE, REWARD_FIELDS), (RANK_RESOURCE, RANK_FIELDS)])
@pytest.mark.parametrize("change", ["missing", "extra", "wrong_type", "duplicate"])
def test_both_resource_schemas_are_strict(name, schema, change):
    fields = list(schema.items())
    source = reward() if name == REWARD_RESOURCE else display()
    if change == "missing":
        fields.pop()
    elif change == "extra":
        fields.append(("Unknown", 1))
        source["Unknown"] = 0
    elif change == "wrong_type":
        field_name, _ = fields[0]
        fields[0] = (field_name, 4)
    else:
        fields.append(fields[0])
    payloads = resources()
    payloads[name] = item_table([source], tuple(fields))
    invalid(lambda: RankBonusCatalog(payloads.__getitem__))


@pytest.mark.parametrize("raw", [b"", b"bad", b"ALTB", b"ALL4", "notbytes", bytearray(b"bad")])
def test_resource_payload_is_bounded_complete_and_immutable(raw):
    payloads = resources()
    payloads[REWARD_RESOURCE] = raw
    invalid(lambda: RankBonusCatalog(payloads.__getitem__))


def test_all4_decoding_preserves_definitions_and_binds_outer_payload():
    payloads = resources()
    first = RankBonusCatalog(payloads.__getitem__)
    raw = payloads[REWARD_RESOURCE]
    payloads[REWARD_RESOURCE] = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    second = RankBonusCatalog(payloads.__getitem__)
    assert first.bonuses == second.bonuses
    assert first.content_version != second.content_version


@pytest.mark.parametrize("description", [None, 1, True, "\x00", "x" * 4097])
def test_invalid_mail_description_cannot_pass_through(description):
    with pytest.raises(ProtocolError) as caught:
        build().bonuses[0].to_present_reward(description)
    assert caught.value.code == "rank_bonus_description_invalid"


def test_snapshots_mappings_and_rewards_are_independent_and_frozen():
    payloads = resources()
    original = deepcopy(payloads)
    catalog = RankBonusCatalog(payloads.__getitem__)
    output = catalog.bonuses[0].to_present_reward("description")
    output["count"] = 100
    assert catalog.bonuses[0].to_present_reward("description")["count"] == 1
    assert payloads == original
    with pytest.raises(TypeError):
        catalog.by_id[78] = None
    with pytest.raises(TypeError):
        catalog.groups[78] = ()
    with pytest.raises(FrozenInstanceError):
        catalog.bonuses[0].rank = 0
    with pytest.raises(FrozenInstanceError):
        catalog.groups[78][0].condition_type = 0


@pytest.mark.parametrize("resource", [REWARD_RESOURCE, RANK_RESOURCE])
def test_either_source_changes_the_content_version(resource):
    first = build()
    payloads = resources()
    if resource == REWARD_RESOURCE:
        payloads[resource] = item_table([reward(Param_Incentive=2)], tuple(REWARD_FIELDS.items()))
    else:
        payloads[resource] = item_table([display(_FlagID=12)], tuple(RANK_FIELDS.items()))
    second = RankBonusCatalog(payloads.__getitem__)
    assert first.content_version != second.content_version
    assert first.bonuses[0].parameter == 1 and first.bonuses[0].tutorial_flag_id == 11


def test_source_byte_and_display_count_limits_are_checked(monkeypatch):
    monkeypatch.setattr(module, "MAX_BONUSES", 1)
    invalid(lambda: build([reward(1), reward(2)], [display(1), display(2)]))
    monkeypatch.setattr(module, "LIMITS", replace(module.LIMITS, max_input_bytes=4))
    invalid(build)


def test_invalid_reader_does_not_become_an_empty_catalog():
    invalid(lambda: RankBonusCatalog(None))
    def unavailable(_):
        raise OSError("missing resource")
    invalid(lambda: RankBonusCatalog(unavailable))

from dataclasses import FrozenInstanceError
from datetime import date, datetime
import json
from pathlib import Path

import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.daily_gift_catalog import DailyGiftCatalog, RESOURCE_NAME, TEXT_RESOURCE
from aigis_ps.daily_gift_rules import DailyGiftRules, load_daily_gift_rules
from aigis_ps.errors import ProtocolError
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.table_cell_patch import patch_table_cells
from aigis_ps.table_row_append import append_table_rows


@pytest.fixture(scope="module")
def daily_sources():
    root = Path(__file__).resolve().parents[1]
    _, entries = ResourceLibrary(root / "runtime/resources", client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, root / "runtime/resources", root.parent / "aigisr_exe")
    return {name: assets.read(name)[0] for name in (RESOURCE_NAME, TEXT_RESOURCE)}


def make(data, rules=None, changes=None):
    changed = dict(data)
    if changes:
        changed[RESOURCE_NAME] = patch_table_cells(changed[RESOURCE_NAME], changes)
    return DailyGiftCatalog(changed.__getitem__, rules)


def parsed(data):
    raw = data[RESOURCE_NAME]
    return parse_table(decompress_all4(raw) if raw.startswith(b"ALL4") else raw)["rows"]


def test_current_source_retains_all_gifts_and_raw_zero_parameters(daily_sources):
    catalog = make(daily_sources)
    assert catalog.status()["ready"] is True
    assert len(catalog.campaigns) == catalog.status()["reward_count"] == 319
    assert tuple(catalog.by_id) == tuple(range(1, 320))
    assert sum(reward.parameter == 0 for campaign in catalog.campaigns for reward in campaign.rewards) == 116
    assert catalog.active(date(2026, 11, 1)) == ()
    assert catalog.get(8).description == "9/12まで\n毎日神聖結晶２個プレゼント！"
    assert catalog.get(8).rewards[0].parameter == 2


@pytest.mark.parametrize("when, expected", [(date(2015, 8, 13), False), (date(2015, 8, 14), True),
    (date(2015, 9, 12), True), (date(2015, 9, 13), False)])
def test_start_and_end_dates_are_both_included(daily_sources, when, expected):
    assert bool(make(daily_sources).active(when, (8,))) is expected


def test_single_day_and_parallel_window_rewards_are_not_lost(daily_sources):
    catalog = make(daily_sources)
    assert [item.gift_id for item in catalog.active(date(2026, 9, 3), (319,))] == [319]
    assert [item.gift_id for item in catalog.active(date(2020, 11, 21), (159, 160, 161))] == [159, 160, 161]


def test_subentries_preserve_all_rewards_under_one_daily_identity(daily_sources):
    addition = dict(parsed(daily_sources)[-1], ID_Gift=2, Param_Gift=3)
    source = dict(daily_sources, **{RESOURCE_NAME: append_table_rows(daily_sources[RESOURCE_NAME], [addition])})
    catalog = make(source)
    assert len(catalog.campaigns) == 319 and catalog.status()["reward_count"] == 320
    assert [reward.gift_entry_id for reward in catalog.get(319).rewards] == [1, 2]
    assert [reward.parameter for reward in catalog.get(319).rewards] == [0, 3]


@pytest.mark.parametrize("outer_id", [0, 319])
def test_continuation_keeps_first_header_and_appends_repeated_subentry(daily_sources, outer_id):
    addition = dict(parsed(daily_sources)[-1], ID_DailyGift=outer_id, ID_Gift=0,
                    Days_Begin=0, Days_End=0, ID_SystemText=-1, Param_Gift=3)
    source = dict(daily_sources, **{RESOURCE_NAME: append_table_rows(daily_sources[RESOURCE_NAME], [addition])})
    campaign = make(source).get(319)
    assert campaign.start_date == campaign.end_date == date(2026, 9, 3)
    assert [reward.gift_entry_id for reward in campaign.rewards] == [1, 1]
    assert [reward.parameter for reward in campaign.rewards] == [0, 3]
    assert [reward.source_index for reward in campaign.rewards] == [318, 319]


def test_inner_vectors_keep_source_order_and_default_key_resets_between_groups(daily_sources):
    base = parsed(daily_sources)[-1]
    additions = [dict(base, ID_DailyGift=0, ID_Gift=2, Param_Gift=2),
                 dict(base, ID_Gift=1, Param_Gift=3),
                 dict(base, ID_DailyGift=0, ID_Gift=2, Param_Gift=4),
                 dict(base, ID_DailyGift=320, ID_Gift=0, Param_Gift=5),
                 dict(base, ID_DailyGift=0, ID_Gift=0, Param_Gift=6)]
    source = dict(daily_sources, **{RESOURCE_NAME: append_table_rows(daily_sources[RESOURCE_NAME], additions)})
    catalog = make(source)
    assert [(r.gift_entry_id, r.parameter) for r in catalog.get(319).rewards] == [(1, 0), (1, 3), (2, 2), (2, 4)]
    assert [(r.gift_entry_id, r.parameter) for r in catalog.get(320).rewards] == [(0, 5), (0, 6)]


def test_noncontiguous_outer_group_reappearance_is_rejected(daily_sources):
    source = dict(daily_sources, **{RESOURCE_NAME: append_table_rows(daily_sources[RESOURCE_NAME], [parsed(daily_sources)[7]])})
    with pytest.raises(ProtocolError) as failure:
        make(source)
    assert failure.value.code == "daily_gift_catalog_invalid"


def test_unknown_reward_and_condition_values_remain_available_to_service(daily_sources):
    campaign = make(daily_sources, changes={7: {"Type_Gift": 99, "Type_Item": 78, "Param_Gift": -1,
        "TypeCondition": 66, "ParamCondition": 7}}).get(8)
    reward = campaign.rewards[0]
    assert (reward.gift_type, reward.item_type, reward.parameter, reward.condition_type, reward.condition_parameter) == (99, 78, -1, 66, 7)


def test_explicit_selection_is_not_expanded_by_active_argument_or_revival(daily_sources):
    settings = DailyGiftRules(gift_ids=(159,), revivals={160: {"start_date": "2020-11-21", "end_date": "2020-11-21"}})
    catalog = make(daily_sources, settings)
    assert [row.gift_id for row in catalog.active(date(2020, 11, 21), (159, 160, 161))] == [159]
    assert make(daily_sources, DailyGiftRules(gift_ids=())).active(date(2020, 11, 21)) == ()
    assert make(daily_sources, DailyGiftRules(enabled=False)).active(date(2020, 11, 21)) == ()


def test_revival_changes_effective_window_without_changing_source_identity(daily_sources):
    original = make(daily_sources)
    settings = DailyGiftRules(revivals={8: {"start_date": "2026-11-01", "end_date": "2026-11-01"}})
    current = make(daily_sources, settings)
    before, after = original.get(8), current.get(8)
    assert after.start_date == after.end_date == date(2026, 11, 1)
    assert after.source_start_date == before.start_date and after.source_end_date == before.end_date
    assert after.rewards == before.rewards and after.definition_version == before.definition_version
    assert current.resource_hashes == original.resource_hashes and current.content_version != original.content_version
    assert [row.gift_id for row in current.active(date(2026, 11, 1))] == [8]


@pytest.mark.parametrize("settings", [DailyGiftRules(gift_ids=(9999,)),
    DailyGiftRules(revivals={9999: {"start_date": "2026-11-01", "end_date": "2026-11-01"}})])
def test_unknown_configured_id_is_rejected(daily_sources, settings):
    with pytest.raises(ProtocolError):
        make(daily_sources, settings)


@pytest.mark.parametrize("changes", [{"ID_DailyGift": 0}, {"ID_Gift": -1}, {"ID_SystemText": -1},
    {"ID_SystemText": 99999}, {"Days_Begin": 20260230}, {"Days_End": 20140530}, {"ID_Item": "x" * 257}])
def test_invalid_source_identity_dates_and_text_are_rejected(daily_sources, changes):
    with pytest.raises(ProtocolError):
        make(daily_sources, changes={0: changes})


def test_text_lookup_uses_physical_index_even_when_record_column_differs(daily_sources):
    changed = dict(daily_sources, **{TEXT_RESOURCE: patch_table_cells(daily_sources[TEXT_RESOURCE], {227: {"Recode_Index": 123456}})})
    assert make(changed).get(8).description == make(daily_sources).get(8).description


def test_frozen_resources_do_not_reread_and_replaced_dependencies_fail(daily_sources):
    called = []
    source = dict(daily_sources)
    def read(name):
        called.append(name)
        return source[name]
    catalog = DailyGiftCatalog(read)
    source.clear()
    assert catalog.get(8).gift_id == 8 and len(called) == 2
    with pytest.raises(FrozenInstanceError):
        catalog.get(8).description = "changed"
    with pytest.raises(TypeError):
        catalog.by_id[8] = None
    catalog.rules = DailyGiftRules(enabled=False)
    assert catalog.status()["ready"] is False


@pytest.mark.parametrize("selection", [(8, 8), (True,), (0,), (99999,), "8"])
def test_invalid_active_selection_is_rejected(daily_sources, selection):
    with pytest.raises(ProtocolError):
        make(daily_sources).active(date(2015, 8, 14), selection)


def test_active_requires_an_already_computed_calendar_day(daily_sources):
    with pytest.raises(ProtocolError) as failure:
        make(daily_sources).active(datetime(2015, 8, 14, 3))
    assert failure.value.code == "daily_gift_day_invalid"


@pytest.mark.parametrize("settings", [{"enabled": 1}, {"gift_ids": [True]}, {"gift_ids": [8, 8]},
    {"gift_ids": [-1]}, {"revivals": {"08": {"start_date": "2026-11-01", "end_date": "2026-11-01"}}},
    {"revivals": {8: {"start_date": "20261101", "end_date": "2026-11-01"}}},
    {"revivals": {8: {"start_date": "2026-11-02", "end_date": "2026-11-01"}}},
    {"revivals": {8: {"start_date": "2026-11-01"}}}])
def test_invalid_rules_are_not_coerced(settings):
    with pytest.raises(ValueError):
        DailyGiftRules(**settings)


def test_rule_file_normalization_copy_and_nested_duplicate_rejection(tmp_path):
    window = {"start_date": "2026-11-01", "end_date": "2026-11-01"}
    settings = DailyGiftRules(gift_ids=[9, 8], revivals={"8": window})
    window["end_date"] = "2027-11-01"
    assert settings.gift_ids == (8, 9) and settings.revivals[8]["end_date"] == "2026-11-01"
    path = tmp_path / "daily.json"
    path.write_text(json.dumps(settings.to_dict()), encoding="utf-8-sig")
    assert load_daily_gift_rules(path) == settings
    assert load_daily_gift_rules(None) == DailyGiftRules()
    path.write_text('{"revivals":{"8":{"start_date":"2026-11-01","start_date":"2026-11-02","end_date":"2026-11-03"}}}', encoding="utf8")
    with pytest.raises(ValueError):
        load_daily_gift_rules(path)
    path.write_text('{"unknown":true}', encoding="utf8")
    with pytest.raises(ValueError):
        load_daily_gift_rules(path)


def test_example_keeps_original_windows_without_automatic_revival():
    path = Path(__file__).resolve().parents[1] / "config/daily-gift-rules.example.json"
    assert load_daily_gift_rules(path) == DailyGiftRules()

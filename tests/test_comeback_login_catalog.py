from dataclasses import FrozenInstanceError
from datetime import date
import json

import pytest

from aigis_ps.comeback_login_catalog import ComebackLoginCatalog
from aigis_ps.comeback_login_rules import ComebackLoginRules, load_comeback_login_rules
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_catalog import EventLoginCatalog
from aigis_ps.table_cell_patch import patch_table_cells
from test_event_login_resources import event_resource


def source(payload):
    return EventLoginCatalog(lambda _: payload)


def build(payload):
    return ComebackLoginCatalog(source(payload))


def test_all_seven_source_bands_are_preserved_without_combining_days(event_resource):
    current = build(event_resource)
    assert current.status()["ready"] is True
    assert current.campaign_id == 6 and current.start_date == date(2022, 12, 21)
    assert current.end_date is None and current.expiration == 20
    assert {pause: band.max_ordinal for pause, band in current.bands.items()} == {
        0: 10, 30: 10, 60: 10, 90: 10, 120: 15, 180: 15, 365: 15}
    assert sum(len(band.rewards) for band in current.bands.values()) == 85
    assert all(len(band.rewards_for(day)) == 1 for band in current.bands.values()
               for day in range(1, band.max_ordinal + 1))
    assert sum(row.item_param for row in current.bands[365].rewards if row.item_type == 0) == 100
    assert current.bands[365].rewards_for(15)[0].item_id == "item_smn_ticket_19114"


@pytest.mark.parametrize("gap,threshold", [(0, 0), (29, 0), (30, 30), (59, 30), (60, 60), (89, 60),
    (90, 90), (119, 90), (120, 120), (179, 120), (180, 180), (364, 180), (365, 365), (999, 365)])
def test_highest_eligible_band_is_selected(event_resource, gap, threshold):
    current = build(event_resource)
    assert current.choose(gap) is current.bands[threshold]


@pytest.mark.parametrize("gap", [-1, True, 1.5, "30", 0x80000000])
def test_invalid_absence_values_are_rejected(event_resource, gap):
    with pytest.raises(ProtocolError) as error:
        build(event_resource).choose(gap)
    assert error.value.code == "comeback_login_absence_invalid"


def test_source_start_and_optional_end_are_respected(event_resource):
    current = build(event_resource)
    assert not current.is_available(date(2022, 12, 20))
    assert current.is_available(date(2022, 12, 21))
    assert current.is_available(date(2026, 11, 1))
    index = source(event_resource).get(6).source_index
    changed = patch_table_cells(event_resource, {index: {"EndDate": date(2026, 12, 1).toordinal() - 1}})
    limited = build(changed)
    assert limited.is_available(date(2026, 11, 30))
    assert not limited.is_available(date(2026, 12, 1))
    assert current.bands[365].definition_version == limited.bands[365].definition_version
    assert current.content_version != limited.content_version


def test_snapshot_and_returned_bands_cannot_be_mutated(event_resource):
    original = source(event_resource)
    current = ComebackLoginCatalog(original)
    with pytest.raises(TypeError):
        current.bands[30] = current.bands[0]
    with pytest.raises(FrozenInstanceError):
        current.bands[30].pause_period = 40
    current.expiration = 30
    assert current.status()["ready"] is False
    with pytest.raises(ProtocolError):
        current.choose(30)


def test_underlying_event_source_change_invalidates_the_comeback_view(event_resource):
    original = source(event_resource)
    current = ComebackLoginCatalog(original)
    original.asset_sha256 = "0" * 64
    with pytest.raises(ProtocolError):
        current.require_band(30)


def test_multiple_rewards_on_one_day_stay_in_the_selected_band(event_resource):
    original = build(event_resource).bands[120]
    updates = {reward.source_index: {"LoginCount": reward.ordinal - 1} for reward in original.rewards[2:]}
    updates[original.rewards[1].source_index] = {"LoginCount": 1}
    current = build(patch_table_cells(event_resource, updates)).bands[120]
    assert current.max_ordinal == 14
    assert len(current.rewards_for(1)) == 2
    assert all(row.pause_period == 120 for row in current.rewards_for(1))


def test_band_order_in_the_resource_does_not_change_threshold_selection(event_resource):
    original = build(event_resource)
    updates = {reward.source_index: {"PausePeriod": 30} for reward in original.bands[0].rewards}
    updates.update({reward.source_index: {"PausePeriod": 0} for reward in original.bands[30].rewards})
    current = build(patch_table_cells(event_resource, updates))
    assert list(current.bands) == [0, 30, 60, 90, 120, 180, 365]
    assert current.choose(30).rewards_for(1)[0].item_param == 5


@pytest.mark.parametrize("field,value", [("LoginCount", 2), ("Expiration", 0), ("StartDate", 0), ("LoginBonusType", 2)])
def test_incomplete_comeback_header_or_day_sequence_fails_closed(event_resource, field, value):
    index = source(event_resource).get(6).source_index
    with pytest.raises(ProtocolError) as error:
        build(patch_table_cells(event_resource, {index: {field: value}}))
    assert error.value.code == "comeback_login_catalog_invalid"


def test_missing_zero_fallback_does_not_silently_select_another_band(event_resource):
    current = build(event_resource)
    updates = {reward.source_index: {"PausePeriod": 1} for reward in current.bands[0].rewards}
    with pytest.raises(ProtocolError):
        build(patch_table_cells(event_resource, updates))


def test_rules_use_a_positive_configurable_local_threshold(tmp_path):
    assert ComebackLoginRules().to_dict() == {"enabled": True, "min_absence_days": 30}
    assert load_comeback_login_rules(None) == ComebackLoginRules()
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"enabled": False, "min_absence_days": 60}), encoding="utf8")
    changed = load_comeback_login_rules(path)
    assert changed == ComebackLoginRules(False, 60)
    assert changed.content_version != ComebackLoginRules().content_version


@pytest.mark.parametrize("values", [{"enabled": 1}, {"enabled": None}, {"min_absence_days": 0},
    {"min_absence_days": -1}, {"min_absence_days": True}, {"min_absence_days": 1.5},
    {"min_absence_days": "30"}, {"min_absence_days": 0x80000000}])
def test_rules_reject_nonpositive_or_ambiguous_thresholds(values):
    with pytest.raises(ValueError):
        ComebackLoginRules(**values)


@pytest.mark.parametrize("text", ['[]', 'null', '{"unknown": 1}', '{"enabled":true,"enabled":false}',
    ' ' * 16385], ids=["array", "null", "unknown", "duplicate", "oversized"])
def test_loader_rejects_invalid_shape_duplicates_and_large_files(tmp_path, text):
    path = tmp_path / "invalid.json"
    path.write_text(text, encoding="utf8")
    with pytest.raises(ValueError):
        load_comeback_login_rules(path)

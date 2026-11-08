from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import struct

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_catalog import EventLoginCatalog, RESOURCE_NAME
from aigis_ps.event_login_revival import build_event_login_revival
from aigis_ps.event_login_rules import EventLoginRules, load_event_login_rules
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.table_cell_patch import patch_table_cells


@pytest.fixture(scope="module")
def event_resource():
    root = Path(__file__).resolve().parents[1]
    _, entries = ResourceLibrary(root / "runtime/resources", client_hash=
        CURRENT_CLIENT_SHA256).load()
    return IndexedAssetService(entries, root / "runtime/resources", root.parent / "aigisr_exe").read(RESOURCE_NAME)[0]


def decode(payload):
    return decompress_all4(payload) if payload.startswith(b"ALL4") else payload


def catalog(payload):
    return EventLoginCatalog(lambda name: payload)


def rules(identifier=55, start="2026-11-01", end="2026-12-01", **values):
    return EventLoginRules(revivals={identifier: {"start_date": start, "end_date": end}}, **values)


def test_real_catalog_public_window_and_nonpublic_boundaries(event_resource):
    result = catalog(event_resource)
    assert result.status()["ready"] is True
    assert (len(result.campaigns), len(result.ordinary_campaigns), result.status()["public_count"]) == (137, 120, 117)
    assert sum(len(row.rewards) for row in result.campaigns) == 1267
    assert [row.campaign_id for row in result.active(date(2026, 11, 1))] == [91, 135]
    assert [row.campaign_id for row in result.active(date(2026, 10, 31))] == [91, 130, 135]
    assert result.get(135).max_ordinal == 10
    for identifier in (4, 90, 136, 137):
        with pytest.raises(ProtocolError) as caught:
            result.require_public(identifier)
        assert caught.value.code == "event_login_campaign_unsupported"


@pytest.mark.parametrize("day,expected", [(date(2026, 10, 21), False), (date(2026, 10, 22), True),
    (date(2026, 11, 5), True), (date(2026, 11, 6), False)])
def test_source_window_is_start_inclusive_end_exclusive(event_resource, day, expected):
    assert bool(catalog(event_resource).active(day, (135,))) is expected


@pytest.mark.parametrize("selection", [(135, 135), (136,), (0,), (True,), (99999,)])
def test_invalid_selection_rejects_instead_of_enabling_other_campaigns(event_resource, selection):
    with pytest.raises(ProtocolError):
        catalog(event_resource).active(date(2026, 11, 1), selection)


def test_catalog_snapshot_is_immutable_and_does_not_reread_source(event_resource):
    source = {RESOURCE_NAME: event_resource}
    result = EventLoginCatalog(source.__getitem__)
    source[RESOURCE_NAME] = b"changed"
    assert [row.campaign_id for row in result.active(date(2026, 11, 1))] == [91, 135]
    with pytest.raises(TypeError):
        result.by_id[135] = None
    with pytest.raises(FrozenInstanceError):
        result.get(135).max_ordinal = 100
    result.asset_sha256 = "0" * 64
    assert result.status()["ready"] is False
    with pytest.raises(ProtocolError) as caught:
        result.get(135)
    assert caught.value.code == "event_login_catalog_changed"


def test_same_ordinal_preserves_multiple_rewards(event_resource):
    original = catalog(event_resource).get(135)
    replacement = patch_table_cells(event_resource, {original.rewards[1].source_index: {"LoginCount": 0},
        **{row.source_index: {"LoginCount": row.ordinal - 1} for row in original.rewards[2:]}})
    result = catalog(replacement).get(135)
    assert result.max_ordinal == 9
    assert [row.config_id for row in result.rewards_for(1)] == [row.config_id for row in original.rewards[:2]]


def test_new_group_metadata_does_not_inherit_prior_group(event_resource):
    result = catalog(event_resource)
    target = result.get(135)
    changed = patch_table_cells(event_resource, {target.source_index: {"EndDate": 0}})
    assert catalog(changed).get(135).end_date is None
    assert 135 not in [row.campaign_id for row in catalog(changed).active(date(2026, 11, 1))]


def test_unknown_public_eligibility_is_reported_and_not_automatically_granted(event_resource):
    source = catalog(event_resource)
    changed = patch_table_cells(event_resource, {source.get(135).source_index: {"Expiration": 30}})
    current = catalog(changed)
    assert [row.campaign_id for row in current.active(date(2026, 11, 1))] == [91]
    assert current.status()["unsupported_campaigns"][135] == "relative_or_pause_eligibility"
    with pytest.raises(ProtocolError):
        current.active(date(2026, 11, 1), (135,))


@pytest.mark.parametrize("updates", [{"ConfigID": 0}, {"LoginCount": -1}, {"PausePeriod": -1}, {"LoginBonusID": -1}])
def test_invalid_resource_counters_are_rejected(event_resource, updates):
    with pytest.raises(ProtocolError) as caught:
        catalog(patch_table_cells(event_resource, {0: updates}))
    assert caught.value.code == "event_login_catalog_invalid"


def test_revival_changes_only_two_native_day_cells(event_resource):
    original = catalog(event_resource)
    result = build_event_login_revival(event_resource, rules())
    changed = result.replacements[RESOURCE_NAME]
    first, second = parse_table(decode(event_resource)), parse_table(decode(changed))
    index = original.get(55).source_index
    expected = deepcopy(first["rows"])
    expected[index].update(StartDate=date(2026, 11, 1).toordinal() - 1, EndDate=date(2026, 12, 1).toordinal() - 1)
    assert second["rows"] == expected and second["fields"] == first["fields"]
    assert catalog(changed).get(55).rewards == original.get(55).rewards
    assert result.source_sha256 == hashlib.sha256(event_resource).hexdigest()
    assert result.effective_sha256 == hashlib.sha256(changed).hexdigest()
    assert result.metadata["rules"] == rules().to_dict()
    assert build_event_login_revival(changed, rules()).replacements == {}


def test_explicit_window_can_revive_unbounded_public_but_not_paid(event_resource):
    revived = build_event_login_revival(event_resource, rules(4))
    assert catalog(revived.replacements[RESOURCE_NAME]).require_public(4).start_date == date(2026, 11, 1)
    with pytest.raises(ValueError):
        build_event_login_revival(event_resource, rules(136))


@pytest.mark.parametrize("configuration", [EventLoginRules(), EventLoginRules(enabled=False), rules(enabled=False)])
def test_disabled_or_unconfigured_revival_preserves_exact_resource(event_resource, configuration):
    result = build_event_login_revival(event_resource, configuration)
    assert not result.replacements
    assert result.source_sha256 == result.effective_sha256


@pytest.mark.parametrize("value", [0, 0xFFFFFFFF])
def test_native_day_patch_keeps_unsigned_range_and_all4_frame(event_resource, value):
    raw = decode(event_resource)
    wrapped = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw, content_checksum=True)
    changed = patch_table_cells(wrapped, {0: {"EndDate": value}})
    first, second = parse_table(raw), parse_table(decode(changed))
    expected = deepcopy(first["rows"])
    expected[0]["EndDate"] = value
    assert second["rows"] == expected
    assert lz4.frame.get_frame_info(changed[12:])["content_checksum"] is True


@pytest.mark.parametrize("value", [-1, 0x100000000, True, 1.5])
def test_native_day_patch_rejects_non_uint32(event_resource, value):
    with pytest.raises(ValueError):
        patch_table_cells(event_resource, {0: {"EndDate": value}})


def test_rules_are_frozen_and_round_trip(tmp_path):
    value = rules(campaign_ids=[135, 55])
    path = tmp_path / "event-login.json"
    path.write_text(json.dumps(value.to_dict()), encoding="utf8")
    loaded = load_event_login_rules(path)
    assert loaded.to_dict() == value.to_dict()
    assert loaded.content_version == value.content_version
    assert loaded.campaign_ids == (55, 135)
    with pytest.raises(TypeError):
        loaded.revivals[55]["start_date"] = "2027-01-01"
    assert load_event_login_rules(None) == EventLoginRules()


@pytest.mark.parametrize("content", ['[]', 'null', '{"unknown":0}', '{"enabled":1}',
    '{"campaign_ids":[1,1]}', '{"campaign_ids":[true]}',
    '{"revivals":{"055":{"start_date":"2026-11-01","end_date":"2026-12-01"}}}',
    '{"revivals":{"55":{"start_date":"2026-11-01","end_date":"2026-11-01"}}}',
    '{"revivals":{"55":{"start_date":"2026-11-01","end_date":"2026-12-01","extra":1}}}',
    '{"enabled":true,"enabled":false}', ' ' * 32769], ids=lambda value: "oversized" if len(value) > 128 else None)
def test_invalid_rule_files_fail_closed(tmp_path, content):
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf8")
    with pytest.raises(ValueError):
        load_event_login_rules(path)


def test_zero_native_day_cannot_be_an_explicit_revival_boundary():
    with pytest.raises(ValueError):
        rules(start="0001-01-01")

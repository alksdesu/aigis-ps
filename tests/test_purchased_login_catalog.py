from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
import json

import pytest

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_catalog import EventLoginCatalog, RESOURCE_NAME
from aigis_ps.package_catalog import PSC_FIELDS
from aigis_ps.package_text_overlay import IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE
from aigis_ps.purchased_login_catalog import PurchasedLoginCatalog
from aigis_ps.purchased_login_rules import PurchasedLoginRules, load_purchased_login_rules
from aigis_ps.purchased_login_state import purchased_login_identity
from aigis_ps.purchased_login_texts import build_purchased_login_texts, purchased_package_keys
import aigis_ps.purchased_login_texts as text_module
from aigis_ps.table_cell_patch import patch_table_cells
from test_event_login_resources import event_resource
from test_login_reward_state import BUSINESS_EDGES, CLIENT_CALENDAR, OUTSIDE_CALENDAR, representations, shifted_day
from test_package_catalog import sources


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


def decode(raw):
    return decompress_all4(raw) if raw.startswith(b"ALL4") else raw


def open_until(event_resource, end):
    index = EventLoginCatalog(lambda _: event_resource).get(136).source_index
    return {index: {"StartDate": 0, "EndDate": end.toordinal() - 1}}


@pytest.fixture
def make(sources, event_resource):
    def build(settings=None, changes=None, plain=False):
        settings = PurchasedLoginRules(True) if settings is None else settings
        data = dict(sources[1]) | {RESOURCE_NAME: event_resource}
        if changes:
            data[RESOURCE_NAME] = patch_table_cells(data[RESOURCE_NAME], changes)
        events = EventLoginCatalog(data.__getitem__)
        result = build_purchased_login_texts(data[IMPLANT_RESOURCE], data[SYSTEM_TEXT_RESOURCE], events, settings)
        if not plain:
            data.update(result.replacements)
        return PurchasedLoginCatalog(events, data.__getitem__, settings), data, result
    return build


def test_all_type2_offers_are_distinct_and_use_no_premium_item(make):
    current, _, _ = make()
    assert current.status()["ready"] is True
    assert len(current.offers) == 16
    assert sum(offer.campaign.max_ordinal for offer in current.offers) == 168
    rows = current.response(NOW)
    assert tuple((field.name, field.type.value) for field in rows.children) == PSC_FIELDS
    values = {column.name: column.values for column in rows.children}
    assert values["Price"] == (0,) * 16
    assert values["PackageItemType"] == (10,) * 16
    assert values["ItemID"] == ("",) * 16
    assert values["WidgetID"] == (0,) * 16
    assert values["BtnPackObjectID"] == (4203149,) * 16
    for offer in current.offers:
        assert offer.package_id == offer.flag_id == 2_000_100_000 + offer.campaign_id
        assert offer.sku == f"local_login_{offer.campaign_id}"
        assert offer.definition_hash == purchased_login_identity(offer.campaign_id)


def test_disabled_default_keeps_identity_catalog_without_selling(make):
    current, _, result = make(PurchasedLoginRules())
    assert current.enabled is False and len(current.offers) == 16
    assert current.available(NOW) == () and not result.replacements
    assert all(not column.values for column in current.response(NOW).children)


def test_selection_and_purchase_limit_are_explicit(make):
    current, _, result = make(PurchasedLoginRules(True, (136, 41), 2))
    assert {offer.campaign_id for offer in current.available(NOW)} == {41, 136}
    assert all(offer.purchase_limit == 2 for offer in current.available(NOW))
    assert result.campaign_ids == (41, 136)
    with pytest.raises(ProtocolError):
        make(PurchasedLoginRules(True, (135,)))


def test_text_overlay_stacks_after_premium_text_without_overwriting_it(make, sources):
    current, data, result = make()
    before = parse_table(decode(sources[1][SYSTEM_TEXT_RESOURCE]))["rows"]
    after = parse_table(decode(data[SYSTEM_TEXT_RESOURCE]))["rows"]
    assert after[:len(before)] == before and len(after) == len(before) + 48
    for offer in current.offers:
        assert after[offer.caption_id]["Data_Text"].startswith("ローカル ")
        assert f"{offer.campaign.expiration}日間" in after[offer.description_id]["Data_Text"]
        assert "<ITEM0>" not in after[offer.description_id]["Data_Text"]
        assert set(purchased_package_keys(offer.campaign_id)) <= set(result.text_indices)
    repeated = build_purchased_login_texts(data[IMPLANT_RESOURCE], data[SYSTEM_TEXT_RESOURCE],
        current.event_catalog, current.rules)
    assert not repeated.replacements and repeated.text_indices == result.text_indices


def test_enabled_catalog_rejects_texts_not_served_by_resource_view(make):
    with pytest.raises(ProtocolError) as error:
        make(plain=True)
    assert error.value.code == "purchased_login_catalog_invalid"


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 11, 1, 3, 59, tzinfo=JST), True),
    (datetime(2026, 11, 1, 4, 0, tzinfo=JST), True),
    (datetime(2026, 11, 2, 3, 59, tzinfo=JST), True),
    (datetime(2026, 11, 2, 4, 0, tzinfo=JST), False)])
def test_finite_source_end_uses_next_logical_claim_day(make, event_resource, when, expected):
    events = EventLoginCatalog(lambda _: event_resource)
    index = events.get(136).source_index
    current, _, _ = make(PurchasedLoginRules(True, (136,)), {index: {
        "EndDate": date(2026, 11, 3).toordinal() - 1}})
    assert bool(current.available(when)) is expected


@pytest.mark.parametrize("moment", BUSINESS_EDGES + CLIENT_CALENDAR)
def test_first_claim_day_keeps_the_shifted_jst_formula_on_both_sides_of_four(make, event_resource, moment):
    end = moment.date() + timedelta(days=1)
    current, _, _ = make(PurchasedLoginRules(True, (136,)), open_until(event_resource, end))
    for now in representations(moment):
        assert bool(current.available(now)) is (shifted_day(now) + timedelta(days=1) < end)


@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_instants_outside_the_client_calendar_have_no_claim_window(make, moment):
    current, _, _ = make(PurchasedLoginRules(True, (136,)))
    for now in representations(moment):
        with pytest.raises(ProtocolError) as caught:
            current.available(now)
        assert (caught.value.code, caught.value.status) == ("purchased_login_window_invalid", 503)


def test_future_source_start_cannot_create_empty_entitlement(make, event_resource):
    index = EventLoginCatalog(lambda _: event_resource).get(136).source_index
    current, _, _ = make(PurchasedLoginRules(True, (136,)), {index: {
        "StartDate": date(2027, 1, 1).toordinal() - 1}})
    assert not current.available(NOW)
    assert current.available(datetime(2026, 12, 20, 12, tzinfo=JST))


def test_get_and_snapshot_do_not_allow_unknown_or_mutated_offers(make):
    current, _, _ = make()
    with pytest.raises(ProtocolError):
        current.get("official-sku")
    with pytest.raises(TypeError):
        current.by_sku["other"] = current.offers[0]
    with pytest.raises(FrozenInstanceError):
        current.offers[0].purchase_limit = 0
    current.rules = PurchasedLoginRules(False)
    assert current.status()["ready"] is False


@pytest.mark.parametrize("values", [{"enabled": 1}, {"purchase_limit": 0}, {"purchase_limit": True},
    {"purchase_limit": 1.5}, {"campaign_ids": [1, 1]}, {"campaign_ids": [True]}, {"campaign_ids": [-1]}])
def test_rules_reject_ambiguous_values(values):
    with pytest.raises(ValueError):
        PurchasedLoginRules(**values)


def test_rule_file_roundtrip_and_duplicate_rejection(tmp_path):
    path = tmp_path / "purchased.json"
    settings = PurchasedLoginRules(True, (136, 41), 2)
    path.write_text(json.dumps(settings.to_dict()), encoding="utf8")
    assert load_purchased_login_rules(path) == settings
    assert load_purchased_login_rules(None) == PurchasedLoginRules()
    path.write_text('{"enabled":true,"enabled":false}', encoding="utf8")
    with pytest.raises(ValueError):
        load_purchased_login_rules(path)


def test_text_builder_rejects_unverified_rule_type(sources, event_resource):
    with pytest.raises(ValueError):
        build_purchased_login_texts(sources[1][IMPLANT_RESOURCE], sources[1][SYSTEM_TEXT_RESOURCE],
                                   EventLoginCatalog(lambda _: event_resource), None)


def test_new_local_text_keys_cannot_collide_with_one_another(sources, event_resource, monkeypatch):
    original = text_module.zlib.crc32
    rows = parse_table(decode(sources[1][IMPLANT_RESOURCE]))["rows"]
    existing = {original(row["_ID"].encode()) for row in rows}
    forced = next(value for value in range(0xF0000000, 0xFFFFFFFF) if value not in existing)
    names = {value.encode() for value in purchased_package_keys(136)[:2]}
    monkeypatch.setattr(text_module.zlib, "crc32", lambda value: forced if value in names else original(value))
    with pytest.raises(ValueError, match="collid"):
        build_purchased_login_texts(sources[1][IMPLANT_RESOURCE], sources[1][SYSTEM_TEXT_RESOURCE],
                                   EventLoginCatalog(lambda _: event_resource), PurchasedLoginRules(True, (136,)))


def test_existing_local_text_content_cannot_be_silently_rebound(make):
    current, data, _ = make(PurchasedLoginRules(True, (136,)))
    offer = current.get("local_login_136")
    corrupted = patch_table_cells(data[SYSTEM_TEXT_RESOURCE], {offer.caption_id: {"Data_Text": "different"}})
    with pytest.raises(ValueError, match="conflict"):
        build_purchased_login_texts(data[IMPLANT_RESOURCE], corrupted, current.event_catalog, current.rules)

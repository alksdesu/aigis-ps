from datetime import date, timedelta
from pathlib import Path

import pytest

import aigis_ps.daily_gift_catalog as daily_gift_module
import aigis_ps.event_login_catalog as event_login_module
from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.daily_gift_catalog import DailyGiftCatalog, RESOURCE_NAME as DAILY_GIFT_RESOURCE, TEXT_RESOURCE
from aigis_ps.event_login_catalog import EventLoginCatalog, RESOURCE_NAME as EVENT_LOGIN_RESOURCE
from aigis_ps.resource_library import ResourceLibrary
from test_native_numbers import BOUNDARY_VALUES, admitted, fits_int32, override_decoded_cell


ONE_DAY = timedelta(days=1)
# First and last JST04 days of periods that the official notices close at 03:59 on the following morning.
DAILY_GIFT_PERIODS = {85: (date(2019, 2, 15), date(2019, 2, 21)), 287: (date(2025, 2, 7), date(2025, 2, 13)),
                      303: (date(2026, 1, 30), date(2026, 2, 5)), 304: (date(2026, 2, 20), date(2026, 2, 26))}
EVENT_LOGIN_PERIODS = {100: (date(2026, 2, 5), date(2026, 2, 19)), 125: (date(2026, 8, 27), date(2026, 9, 10)),
                       131: (date(2026, 9, 24), date(2026, 10, 28)), 133: (date(2026, 11, 5), date(2026, 11, 12))}


@pytest.fixture(scope="module")
def sources():
    root = Path(__file__).resolve().parents[1]
    _, entries = ResourceLibrary(root / "runtime/resources", client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, root / "runtime/resources", root.parent / "aigisr_exe")
    return {name: assets.read(name)[0] for name in (DAILY_GIFT_RESOURCE, TEXT_RESOURCE, EVENT_LOGIN_RESOURCE)}


@pytest.fixture(scope="module")
def catalogs(sources):
    return DailyGiftCatalog(sources.__getitem__), EventLoginCatalog(sources.__getitem__)


def open_days(active, first, last):
    return [day for day in (first - ONE_DAY, first, last, last + ONE_DAY) if active(day)]


@pytest.mark.parametrize("field,minimum", [("Param_Gift", None), ("TypeCondition", None), ("ID_Gift", 0)])
@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_daily_gift_integer_cells_admit_exactly_the_native_int32_range(sources, monkeypatch, field, minimum, value):
    override_decoded_cell(monkeypatch, daily_gift_module, field, value)
    expected = fits_int32(value) and (minimum is None or value >= minimum)
    assert admitted(lambda: DailyGiftCatalog(sources.__getitem__), "daily_gift_catalog_invalid") == expected


@pytest.mark.parametrize("field,minimum", [("ItemParam", None), ("CharacterOffsetX", None), ("ConfigID", 1)])
@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_event_login_integer_cells_admit_exactly_the_native_int32_range(sources, monkeypatch, field, minimum, value):
    override_decoded_cell(monkeypatch, event_login_module, field, value)
    expected = fits_int32(value) and (minimum is None or value >= minimum)
    assert admitted(lambda: EventLoginCatalog(sources.__getitem__), "event_login_catalog_invalid") == expected


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_catalog_lookups_admit_positive_native_int32_identities(catalogs, value):
    daily, events = catalogs
    expected = fits_int32(value) and value > 0
    assert admitted(lambda: daily.get(value), "daily_gift_id_invalid") == expected
    assert admitted(lambda: events.get(value), "event_login_campaign_invalid") == expected
    assert admitted(lambda: events.get(135).rewards_for(value), "event_login_ordinal_invalid") == expected


@pytest.mark.parametrize("gift_id", DAILY_GIFT_PERIODS)
def test_daily_gifts_stay_open_through_the_last_announced_day(catalogs, gift_id):
    daily, _ = catalogs
    first, last = DAILY_GIFT_PERIODS[gift_id]
    assert daily.get(gift_id).source_end_date == last
    assert open_days(lambda day: daily.active(day, (gift_id,)), first, last) == [first, last]


@pytest.mark.parametrize("campaign_id", EVENT_LOGIN_PERIODS)
def test_event_logins_close_on_the_announced_end_date(catalogs, campaign_id):
    _, events = catalogs
    first, last = EVENT_LOGIN_PERIODS[campaign_id]
    assert events.get(campaign_id).end_date == last + ONE_DAY
    assert open_days(lambda day: events.active(day, (campaign_id,)), first, last) == [first, last]

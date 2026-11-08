from datetime import timedelta
import hashlib
from types import SimpleNamespace

import pytest

from aigis_ps import (asset_service, barracks_service, select_ticket_catalog, summon_pool_catalog, tower_trade_catalog,
                      trade_catalog, unit_capacity)
from aigis_ps.al_format import MAX_RESOURCE_BYTES, ParseLimits
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.asset_service import ASSET_TOO_LARGE, AssetService, IndexedAssetService
from aigis_ps.assets import asset_key
from aigis_ps.barracks_service import EXTRA_SHIFT, MAXIMUM_EXTRA_BARRACKS, PURCHASE_FIELD, RETAINED_BITS
from aigis_ps.config_state import CONFIGURATION_FIELDS, CONFIGURATION_REQUIRED_FIELDS
from aigis_ps.errors import ProtocolError, caught
from aigis_ps.masterdata import MasterSnapshot
from aigis_ps.native_clock import NATIVE_EPOCH
from aigis_ps.native_numbers import INT32_MAX, INT32_MIN, UINT32_MASK
from aigis_ps.quest_entries import EnemyBand, classify_enemy
from aigis_ps.resource_fetch import ResourceEntry
from test_trade_catalog import catalog, effect, product, quote, resources, state as trade_state, table


NAME = "Limit.atb"
MASK_PROBES = (-1, 2 ** 31, 2 ** 32 - 1)
PURCHASE_PROBES = (*MASK_PROBES, INT32_MIN, 0, 1 << EXTRA_SHIFT, (MAXIMUM_EXTRA_BARRACKS << EXTRA_SHIFT) | RETAINED_BITS,
                   (MAXIMUM_EXTRA_BARRACKS + 1) << EXTRA_SHIFT)
INT32_PROBES = (INT32_MIN - 1, INT32_MIN, -1, 0, 1, INT32_MAX, INT32_MAX + 1, UINT32_MASK, True, False, 1.0, None)
# These int32 flags keep a unit in the first barracks, so only the int32 check can refuse them.
RECORD_PROBES = (INT32_MIN - 1, INT32_MIN, 0, 1, INT32_MAX + 1, UINT32_MASK, True, None)
QUEST_ROW = {"QuestID": 1001, "Type": 1, "MapNo": 1, "EntryNo": 1, "LocationNo": 1, "Capacity": 1}
COMMAND_FIELDS = {"_ID": 1, "_Cmnd": 32}


def fits_int32(value) -> bool:
    return type(value) is int and -2 ** 31 <= value < 2 ** 31


def expected_extra(value) -> int | None:
    return (value & 0xFFFFFFFF) >> EXTRA_SHIFT if fits_int32(value) else None


def expected_integer(value, minimum) -> bool:
    return type(value) is int and minimum <= value <= 0x7FFFFFFF


def outcome(action):
    try:
        return action()
    except ProtocolError as error:
        return error.code
    except ValueError:
        return ValueError


def refusal(action):
    error = caught(action)
    return None if error is None else (error.code, error.status)


def manifest(root, size, sha256="0" * 64):
    key = asset_key(NAME)
    return {"source_root": str(root), "entries": [{"filename_hash": key, "source_path": key, "magic": "ALTB",
                                                   "size": size, "sha256": sha256, "md5": {"status": "absent"}}]}


def indexed(size, md5="0" * 32):
    return ResourceEntry(NAME, "a" * 40, md5, 0, size)


def configuration_state(purchases):
    row = {name: False if CONFIGURATION_FIELDS[name][1] is bool else 0 for name in CONFIGURATION_REQUIRED_FIELDS}
    return {"bootstrap_schema": 1, "tables": {"config": [{**row, PURCHASE_FIELD: purchases}]}}


def capacity_state(*, purchases=0, achieve=0, flags=0):
    return {"bootstrap_schema": 1, "tables": {
        "game_param": [{"Rank": 1}], "config": [{PURCHASE_FIELD: purchases}],
        "quest_records": [{"QuestID": QUEST_ROW["QuestID"], "AchieveFlag": achieve}]},
        "player_state": {"units": [{"GeneralPurposeFlags": flags}]}}


@pytest.fixture(scope="module")
def payload():
    content = bytes(MAX_RESOURCE_BYTES)
    return SimpleNamespace(content=content, sha256=hashlib.sha256(content).hexdigest(),
                           md5=hashlib.md5(content).hexdigest())


@pytest.fixture
def reads(monkeypatch, payload):
    limits = []

    def read(path, limit, *, root=None):
        limits.append(limit)
        return payload.content

    monkeypatch.setattr(asset_service, "read_bounded", read)
    return limits


@pytest.fixture(scope="module")
def capacity():
    quests = dumps(Container(children=tuple(Column(name, "I", (value,)) for name, value in QUEST_ROW.items())))
    snapshot = MasterSnapshot("capacity-fixture", "a" * 64, {"CardData": b"unused", "QuestTable": quests})
    commands = table([], COMMAND_FIELDS)
    return unit_capacity.UnitCapacity(snapshot, {"OnSystemCommand.atb": commands, "OnCommand.atb": commands}.__getitem__)


def test_resource_limit_is_64_mebibytes_and_the_parse_default():
    assert MAX_RESOURCE_BYTES == 64 * 1024 * 1024 == ParseLimits().max_input_bytes


def test_resource_bounds_reference_the_format_limit():
    # `is` rather than ==: a literal respelled in another module is a separate int object.
    bounds = {
        "ParseLimits": ParseLimits().max_input_bytes,
        "AssetService": asset_service.MAX_RESOURCE_BYTES,
        "TowerTradeCatalog master": tower_trade_catalog.MAX_MASTER_BYTES,
        "SelectTicketCatalog master": select_ticket_catalog.MAX_MASTER_BYTES,
        "SummonPoolCatalog master": summon_pool_catalog.MAX_MASTER_BYTES,
        "BarracksService input": barracks_service.RESOURCE_LIMITS.max_input_bytes,
        "BarracksService decompressed": barracks_service.RESOURCE_LIMITS.max_decompressed_bytes,
        "TradeCatalog input": trade_catalog.RESOURCE_LIMITS.max_input_bytes,
        "TradeCatalog decompressed": trade_catalog.RESOURCE_LIMITS.max_decompressed_bytes,
    }
    assert [name for name, bound in bounds.items() if bound is not MAX_RESOURCE_BYTES] == []


def test_manifest_assets_are_served_up_to_the_resource_limit(tmp_path, payload, reads):
    exact = AssetService(manifest(tmp_path, MAX_RESOURCE_BYTES, payload.sha256))
    assert exact.read(NAME) == (payload.content, payload.sha256)
    assert reads == [MAX_RESOURCE_BYTES]
    oversized = AssetService(manifest(tmp_path, MAX_RESOURCE_BYTES + 1))
    assert refusal(lambda: oversized.read(NAME)) == (ASSET_TOO_LARGE, 413)
    assert reads == [MAX_RESOURCE_BYTES]


def test_indexed_assets_are_served_up_to_the_resource_limit(tmp_path, payload, reads):
    exact = indexed(MAX_RESOURCE_BYTES, payload.md5)
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / exact.cache_key).touch()
    assert IndexedAssetService([exact], tmp_path, tmp_path).read(NAME) == (payload.content, payload.sha256)
    assert reads == [MAX_RESOURCE_BYTES]
    oversized = IndexedAssetService([indexed(MAX_RESOURCE_BYTES + 1)], tmp_path, tmp_path)
    assert refusal(lambda: oversized.read(NAME)) == (ASSET_TOO_LARGE, 413)
    assert reads == [MAX_RESOURCE_BYTES]


def test_local_replacements_are_accepted_up_to_the_resource_limit(tmp_path, payload):
    exact = indexed(MAX_RESOURCE_BYTES, payload.md5)
    replaced = IndexedAssetService([exact], tmp_path, tmp_path, replacements={NAME: payload.content})
    assert replaced.read(NAME) == (payload.content, payload.sha256)
    oversized = payload.content + b"\0"
    entry = indexed(len(oversized), hashlib.md5(oversized).hexdigest())
    with pytest.raises(ValueError, match="replacement"):
        IndexedAssetService([entry], tmp_path, tmp_path, replacements={NAME: oversized})


def test_every_asset_bound_reads_the_shared_resource_limit(tmp_path, monkeypatch):
    content = b"bound"
    entry = indexed(len(content), hashlib.md5(content).hexdigest())
    monkeypatch.setattr(asset_service, "MAX_RESOURCE_BYTES", len(content) - 1)
    assert refusal(lambda: AssetService(manifest(tmp_path, len(content))).read(NAME)) == (ASSET_TOO_LARGE, 413)
    assert refusal(lambda: IndexedAssetService([entry], tmp_path, tmp_path).read(NAME)) == (ASSET_TOO_LARGE, 413)
    with pytest.raises(ValueError, match="replacement"):
        IndexedAssetService([entry], tmp_path, tmp_path, replacements={NAME: content})


@pytest.mark.parametrize("enemy_id,expected", [
    (-1, EnemyBand.SKIPPED), (2 ** 31, ValueError), (2 ** 32 - 1, ValueError),
    (INT32_MIN + 6000, EnemyBand.SKIPPED), (INT32_MIN + 7000, EnemyBand.SKIPPED),
])
def test_enemy_bands_compare_the_unsigned_identifier(enemy_id, expected):
    assert outcome(lambda: classify_enemy(enemy_id)) is expected


@pytest.mark.parametrize("value", PURCHASE_PROBES)
def test_barracks_purchase_reads_extra_slots_from_the_unsigned_flags(value):
    extra = expected_extra(value)
    expected = ("invalid_configuration_state" if extra is None
                else "barracks_purchase_limit" if extra >= MAXIMUM_EXTRA_BARRACKS else (value, extra))
    assert outcome(lambda: barracks_service._open_slot(configuration_state(value))) == expected


@pytest.mark.parametrize("value", PURCHASE_PROBES)
def test_capacity_reads_extra_barracks_from_the_unsigned_flags(capacity, value):
    extra = expected_extra(value)
    expected = ("unit_capacity_state_invalid" if extra is None
                else "unit_capacity_barracks_unsupported" if extra > MAXIMUM_EXTRA_BARRACKS else extra + 1)
    assert outcome(lambda: len(capacity.usage(capacity_state(purchases=value)).capacities)) == expected


@pytest.mark.parametrize("value", PURCHASE_PROBES)
def test_ownership_discount_reads_extra_barracks_from_the_unsigned_flags(value):
    subject = catalog(resources([product(Type=2)], specialty=[effect()]))
    saved = trade_state()
    discounted = quote(subject, saved, registration=105).unit_price
    saved["tables"]["config"][0][PURCHASE_FIELD] = value
    extra = expected_extra(value)
    expected = "trade_state_invalid" if extra is None or extra > MAXIMUM_EXTRA_BARRACKS else discounted
    assert outcome(lambda: quote(subject, saved, registration=105).unit_price) == expected


@pytest.mark.parametrize("word,expected", [
    (-1, ValueError), (0, None), (2 ** 31, NATIVE_EPOCH + timedelta(milliseconds=2 ** 31)),
    (2 ** 32 - 1, NATIVE_EPOCH + timedelta(milliseconds=2 ** 32 - 1)), (2 ** 32, ValueError),
])
def test_shop_date_words_span_the_unsigned_32_bit_range(word, expected):
    assert outcome(lambda: trade_catalog._date([word, 0])) == expected


@pytest.mark.parametrize("value", INT32_PROBES)
def test_trade_integers_with_the_int32_floor_admit_exactly_int32(value):
    assert outcome(lambda: trade_catalog._integer(value, INT32_MIN)) == (value if fits_int32(value) else ValueError)


@pytest.mark.parametrize("minimum", [INT32_MIN, 0, 1])
@pytest.mark.parametrize("value", INT32_PROBES)
def test_capacity_integers_keep_their_int32_bounds(value, minimum):
    assert unit_capacity._integer(value, minimum) == expected_integer(value, minimum)


@pytest.mark.parametrize("field", ["achieve", "flags"])
@pytest.mark.parametrize("value", RECORD_PROBES)
def test_capacity_record_and_unit_flags_admit_every_int32(capacity, field, value):
    expected = 1 if fits_int32(value) else "unit_capacity_state_invalid"
    assert outcome(lambda: len(capacity.usage(capacity_state(**{field: value})).capacities)) == expected

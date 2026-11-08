from copy import deepcopy
import hashlib
import json
import re
import struct
from types import SimpleNamespace
import zlib

import lz4.frame
import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.unit_skin_catalog import MAX_SKIN_NAME_BYTES, UnitSkinCatalog
from test_al_format import schema_bytes
from test_possessions import item_table
from test_quest_service import bundle


def identifier(name):
    return (zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000


def skin_table(rows, *, free_field="FreeCondition"):
    fields = [("ConfigId", 0, 1, b"\0" * 8), (free_field, 4, 1, b"\0" * 8),
              ("ConditionItemId", 8, 32, b""), ("SmartPhoneDiff", 12, 5, b"\xff"),
              ("Aggregate", 16, 1, b"\0" * 8)]
    schema = schema_bytes(fields, 20)
    records, pool = bytearray(), bytearray()
    for row in rows:
        name, free, item, smartphone = row[:4]
        aggregate = row[4] if len(row) == 5 else int(not free and bool(item))
        records += struct.pack("<iiIB3xi", identifier(name), free, len(pool), smartphone, aggregate)
        pool += item.encode() + b"\0"
    start = 28 + len(schema)
    return struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, len(rows), 28, start, 20,
                       len(pool), start + len(records), b"skin") + schema + records + pool


class Cards:
    def __init__(self, rows):
        self.rows = rows
        self.card_ids = tuple(rows)
        self.snapshot = SimpleNamespace(get_xml=lambda _: json.dumps(self.rows, sort_keys=True).encode())

    def card(self, card_id):
        return self.rows[card_id]


def card(card_id, *skins):
    return {"CardID": card_id, **{f"ExtendSkin{slot:02}": skins[slot - 1] if slot <= len(skins) else "null"
                                for slot in range(1, 5)}}


@pytest.fixture
def state():
    return {"bootstrap_schema": 1, "tables": {"possessions": [], "other": [1]}, "other": {"keep": True}}


@pytest.fixture
def possessions(tmp_path):
    raw = item_table((("get_skin_paid", identifier("get_skin_paid"), 1),
                      ("other_item", identifier("other_item"), 10)))
    return PossessionService(tmp_path, asset_reader=lambda _: raw)


def catalog(possessions, rows=None, cards=None):
    rows = [("free", 1, "", True), ("paid", 0, "get_skin_paid", False)] if rows is None else rows
    cards = Cards({10: card(10, "free", "paid"), 20: card(20)}) if cards is None else cards
    return UnitSkinCatalog(cards, possessions, lambda _: skin_table(rows))


@pytest.fixture(scope="module")
def current(bundle):
    return UnitSkinCatalog(bundle.gifts.catalog, bundle.possessions,
                           lambda name: bundle.native_cdn.assets.read(name)[0])


def test_current_free_cards_and_only_one_smartphone_skin(current, bundle):
    state = bundle.bootstrap.initial_state()
    expected = {416, 2630, 2638, 2778, 2808, 2873, 2884, 2931}
    actual = {card_id for card_id in bundle.gifts.catalog.card_ids
              if current.allowed_displays(card_id, state, smartphone_images=False)}
    assert actual == expected
    for card_id in expected:
        assert current.allowed_displays(card_id, state, smartphone_images=False) == frozenset({5})
        assert current.allowed_displays(card_id, state, smartphone_images=True) == frozenset(
            {5, 21} if card_id == 416 else {5})
    status = current.status()
    assert (status["packages"], status["cards"], status["links"]) == (48, 2932, 104)
    assert status["missing_unlock_items"] == {}
    assert re.fullmatch(r"[a-f0-9]{64}", current.content_version)


def test_current_paid_skin_count_zero_one_and_back_to_zero(current, bundle):
    state = bundle.bootstrap.initial_state()
    assert current.allowed_displays(29, state, smartphone_images=True) == frozenset()
    state["tables"]["possessions"] = [{"ItemID": "get_skin_00001", "Count": 1}]
    before = deepcopy(state)
    assert current.allowed_displays(29, state, smartphone_images=True) == frozenset({5})
    assert state == before
    state["tables"]["possessions"][0]["Count"] = 0
    assert current.allowed_displays(29, state, smartphone_images=True) == frozenset()


def test_slot_mapping_and_smartphone_requirement(possessions, state):
    rows = [(f"skin{slot}", 1, "", slot % 2 == 0) for slot in range(1, 5)]
    skins = catalog(possessions, rows, Cards({10: card(10, "skin1", "skin2", "skin3", "skin4")}))
    assert skins.allowed_displays(10, state, smartphone_images=False) == frozenset({5, 6, 7, 8})
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset({5, 6, 7, 8, 22, 24})


def test_paid_unlock_is_read_from_current_state_without_consumption(possessions, state):
    skins = catalog(possessions)
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset({5, 21})
    state["tables"]["possessions"] = [{"ItemID": "get_skin_paid", "Count": 1}]
    before = deepcopy(state)
    for _ in range(2):
        assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset({5, 6, 21})
    assert state == before
    state["tables"]["possessions"][0]["Count"] = 0
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset({5, 21})


def test_free_flag_and_empty_item_hash_allow_without_backpack_item(possessions, state):
    skins = catalog(possessions, [("free", -1, "missing_item", False), ("empty", 0, "", False)],
                    Cards({10: card(10, "free", "empty")}))
    assert skins.allowed_displays(10, state, smartphone_images=False) == frozenset({5, 6})
    assert skins.status()["missing_unlock_items"] == {}


def test_missing_item_definition_only_locks_affected_choices(possessions, state):
    skins = catalog(possessions, [("free", 1, "", True), ("missing", 0, "missing_item", True)],
                    Cards({10: card(10, "free", "missing"), 20: card(20, "missing")}))
    assert skins.status()["missing_unlock_items"] == {"missing_item": [10, 20]}
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset({5, 21})
    assert skins.allowed_displays(20, state, smartphone_images=True) == frozenset()


def test_original_card_links_are_not_replaced_by_image_aliases(possessions, state):
    cards = Cards({497: card(497), 512: card(512, "free")})
    skins = catalog(possessions, cards=cards)
    assert skins.allowed_displays(497, state, smartphone_images=True) == frozenset()
    assert skins.allowed_displays(512, state, smartphone_images=True) == frozenset({5, 21})


@pytest.mark.parametrize("empty", ["", "null", None])
def test_empty_card_skin_references_have_no_choice(possessions, state, empty):
    skins = catalog(possessions, [], Cards({10: card(10, empty)}))
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset()


def test_literal_null_item_name_is_not_an_empty_free_condition(possessions, state):
    skins = catalog(possessions, [("free", 0, "null", True)], Cards({10: card(10, "free")}))
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset()


def test_frozen_snapshot_and_defensive_status(possessions, state):
    cards = Cards({10: card(10, "free"), 20: card(20)})
    skins = catalog(possessions, cards=cards)
    version = skins.content_version
    cards.rows[10]["ExtendSkin01"] = "paid"
    cards.rows[20]["ExtendSkin01"] = "free"
    status = skins.status()
    status["resource_sha256"].clear()
    status["missing_unlock_items"]["fake"] = [10]
    assert skins.content_version == version
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset({5, 21})
    assert skins.allowed_displays(20, state, smartphone_images=True) == frozenset()
    assert len(skins.status()["resource_sha256"]) == 3
    assert skins.status()["missing_unlock_items"] == {}


def test_content_version_binds_cards_skins_and_possession_definitions(possessions, tmp_path):
    first = catalog(possessions)
    assert catalog(possessions).content_version == first.content_version
    changed_cards = catalog(possessions, cards=Cards({10: card(10, "paid"), 20: card(20)}))
    changed_skin = catalog(possessions, [("free", 1, "", False), ("paid", 0, "get_skin_paid", False)])
    item_raw = item_table((("get_skin_paid", identifier("get_skin_paid"), 2),))
    other = PossessionService(tmp_path, asset_reader=lambda _: item_raw)
    changed_items = catalog(other)
    assert len({first.content_version, changed_cards.content_version, changed_skin.content_version,
                changed_items.content_version}) == 4


def test_all4_resource_and_signed_identifier(possessions, state):
    name = next(f"skin_{n}" for n in range(20) if identifier(f"skin_{n}") < 0)
    raw = skin_table([(name, 1, "", True)])
    packed = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    skins = UnitSkinCatalog(Cards({10: card(10, name)}), possessions, lambda _: packed)
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset({5, 21})


@pytest.mark.parametrize("fault", ["missing_reference", "missing_field", "bad_reference", "bool_card", "duplicate_card", "duplicate_config", "bad_schema"])
def test_invalid_linked_resources_fail_explicitly(possessions, fault):
    cards = Cards({10: card(10, "free")})
    rows = [("free", 1, "", True)]
    if fault == "missing_reference":
        cards.rows[10]["ExtendSkin01"] = "unknown"
    elif fault == "missing_field":
        del cards.rows[10]["ExtendSkin04"]
    elif fault == "bad_reference":
        cards.rows[10]["ExtendSkin01"] = 42
    elif fault == "bool_card":
        cards.rows = {1: card(True, "free")}
        cards.card_ids = (1,)
    elif fault == "duplicate_card":
        cards.card_ids = (10, 10)
    elif fault == "duplicate_config":
        rows *= 2
    raw = skin_table(rows, free_field="Wrong" if fault == "bad_schema" else "FreeCondition")
    with pytest.raises(ProtocolError) as caught:
        UnitSkinCatalog(cards, possessions, lambda _: raw)
    assert caught.value.code == "unit_skin_catalog_invalid" and caught.value.status == 503


@pytest.mark.parametrize("payload", [None, "string", b"broken", b"x" * (8 * 1024 * 1024 + 1)],
                         ids=["null", "string", "malformed", "oversize"])
def test_bad_resource_payloads_do_not_become_empty_catalog(possessions, payload):
    with pytest.raises(ProtocolError) as caught:
        UnitSkinCatalog(Cards({10: card(10)}), possessions, lambda _: payload)
    assert caught.value.status == 503


@pytest.mark.parametrize("value", [True, -1, 0, 30, "10", 10.0])
def test_unknown_card_input_fails(possessions, state, value):
    with pytest.raises(ProtocolError) as caught:
        catalog(possessions).allowed_displays(value, state, smartphone_images=True)
    assert caught.value.code == "unit_skin_card_unknown"


@pytest.mark.parametrize("value", [1, 0, None, "true"])
def test_smartphone_flag_requires_bool(possessions, state, value):
    with pytest.raises(ProtocolError) as caught:
        catalog(possessions).allowed_displays(10, state, smartphone_images=value)
    assert caught.value.code == "unit_skin_selection_invalid"


@pytest.mark.parametrize("rows", [[{"ItemID": "get_skin_paid", "Count": -1}],
                                   [{"ItemID": "get_skin_paid", "Count": True}],
                                   [{"ItemID": "get_skin_paid", "Count": 1}] * 2,
                                   [{"ItemID": identifier("get_skin_paid"), "Count": 1}],
                                   [{"ItemID": "missing_item", "Count": 1}]])
def test_invalid_possession_state_is_not_silently_treated_as_locked(possessions, state, rows):
    state["tables"]["possessions"] = rows
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        catalog(possessions).allowed_displays(10, state, smartphone_images=True)
    assert caught.value.status == 503 and state == before


def test_possession_definition_replacement_after_snapshot_fails(possessions, state):
    skins = catalog(possessions)
    possessions.asset_sha256 = "0" * 64
    with pytest.raises(ProtocolError) as caught:
        skins.allowed_displays(10, state, smartphone_images=True)
    assert caught.value.code == "unit_skin_catalog_changed"


def test_current_counted_items_cover_verified_aggregate_packages(current, bundle):
    assert isinstance(current.counted_item_ids, frozenset)
    assert len(current.counted_item_ids) == 40
    assert {"get_skin_00046", "get_skin_00047"} <= current.counted_item_ids
    assert current.status()["missing_counted_items"] == []
    state = bundle.bootstrap.initial_state()
    assert current.acquired_items(state) == frozenset()
    state["tables"]["possessions"] = [{"ItemID": item_id, "Count": 1} for item_id in current.counted_item_ids]
    before = deepcopy(state)
    assert current.acquired_items(state) == current.counted_item_ids
    assert state == before


def test_acquisition_counts_package_once_without_owning_any_linked_card(possessions, state):
    skins = catalog(possessions, cards=Cards({10: card(10), 20: card(20)}))
    assert skins.counted_item_ids == frozenset({"get_skin_paid"})
    assert skins.acquired_items(state) == frozenset()
    state["tables"]["possessions"] = [{"ItemID": "get_skin_paid", "Count": 1}, {"ItemID": "other_item", "Count": 1}]
    assert skins.acquired_items(state) == frozenset({"get_skin_paid"})
    state["tables"]["possessions"][0]["Count"] = 0
    assert skins.acquired_items(state) == frozenset()


def test_verified_zero_stack_skin_uses_saved_possessions_without_enabling_credits(tmp_path, state):
    item = "get_skin_00046"
    raw = item_table(((item, identifier(item), 0),))
    possessions = PossessionService(tmp_path, asset_reader=lambda _: raw)
    skins = catalog(possessions, [("paid", 0, item, True)], Cards({10: card(10, "paid")}))
    assert skins.counted_item_ids == frozenset({item})
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset()
    assert skins.acquired_items(state) == frozenset()
    state["tables"]["possessions"] = [{"ItemID": item, "Count": 2}]
    original = deepcopy(state)
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset({5, 21})
    assert skins.acquired_items(state) == frozenset({item})
    with pytest.raises(ProtocolError) as error:
        possessions.credit(state, {item: 1})
    assert error.value.code == "possession_stack_unsupported"
    assert state == original


@pytest.mark.parametrize("invalid", [-1, True, "0"])
def test_counted_skin_still_rejects_unverified_stack_values(possessions, invalid):
    possessions._definitions["get_skin_paid"] = invalid
    with pytest.raises(ProtocolError) as error:
        catalog(possessions)
    assert error.value.code == "unit_skin_catalog_invalid"


def test_many_card_links_and_smartphone_variants_do_not_multiply_acquisition(possessions, state):
    skins = catalog(possessions, [("paid", 0, "get_skin_paid", True)],
                    Cards({10: card(10, "paid", "paid"), 20: card(20, "paid")}))
    state["tables"]["possessions"] = [{"ItemID": "get_skin_paid", "Count": 1}]
    assert skins.allowed_displays(10, state, smartphone_images=True) == frozenset({5, 6, 21, 22})
    assert skins.acquired_items(state) == frozenset({"get_skin_paid"})
    with pytest.raises(AttributeError):
        skins.counted_item_ids = frozenset()


def test_nonaggregate_skin_still_unlocks_but_does_not_count(possessions, state):
    skins = catalog(possessions, [("paid", 0, "get_skin_paid", False, 0)], Cards({10: card(10, "paid")}))
    state["tables"]["possessions"] = [{"ItemID": "get_skin_paid", "Count": 1}]
    assert skins.allowed_displays(10, state, smartphone_images=False) == frozenset({5})
    assert skins.counted_item_ids == skins.acquired_items(state) == frozenset()


@pytest.mark.parametrize("rows", [
    [("a", 0, "get_skin_paid", False, 1), ("b", 0, "get_skin_paid", False, 1)],
    [("a", 1, "get_skin_paid", False, 1)],
    [("a", 0, "", False, 1)],
])
def test_unrepresentable_aggregate_rules_do_not_collapse_to_item_count(possessions, rows):
    with pytest.raises(ProtocolError) as error:
        catalog(possessions, rows, Cards({10: card(10)}))
    assert error.value.code == "unit_skin_catalog_invalid"


def test_skin_references_and_unlock_items_keep_their_256_byte_bounds(possessions, state):
    assert MAX_SKIN_NAME_BYTES == STORED_ITEM_NAME_BYTES == 256
    skin, item = "s" * MAX_SKIN_NAME_BYTES, "i" * STORED_ITEM_NAME_BYTES
    skins = catalog(possessions, [(skin, 0, item, False, 0)], Cards({10: card(10, skin)}))
    assert skins.allowed_displays(10, state, smartphone_images=False) == frozenset()
    assert skins.status()["missing_unlock_items"] == {item: [10]}


@pytest.mark.parametrize("skin,item", [("s" * (MAX_SKIN_NAME_BYTES + 1), "get_skin_paid"),
                                       ("paid", "i" * (STORED_ITEM_NAME_BYTES + 1))], ids=["skin", "unlock_item"])
def test_names_past_their_bounds_fail(possessions, skin, item):
    with pytest.raises(ProtocolError) as error:
        catalog(possessions, [(skin, 0, item, False, 0)], Cards({10: card(10, skin)}))
    assert error.value.code == "unit_skin_catalog_invalid"


def test_missing_aggregate_item_is_not_made_grantable(possessions, state):
    skins = catalog(possessions, [("a", 0, "missing_item", False, 1)], Cards({10: card(10)}))
    assert skins.counted_item_ids == frozenset()
    assert skins.status()["missing_counted_items"] == ["missing_item"]
    assert skins.acquired_items(state) == frozenset()


@pytest.mark.parametrize("change", [lambda p: setattr(p, "asset_sha256", "0" * 64),
    lambda p: p.issues.update(unavailable="test"),
    lambda p: p._definitions.update(get_skin_paid=2),
    lambda p: p._names_by_hash.update({identifier("get_skin_paid"): "other_item"})])
def test_acquisition_revalidates_definition_dependencies(possessions, state, change):
    skins = catalog(possessions)
    change(possessions)
    with pytest.raises(ProtocolError) as error:
        skins.acquired_items(state)
    assert error.value.code == "unit_skin_catalog_changed"


@pytest.mark.parametrize("rows", [[{"ItemID": "get_skin_paid", "Count": -1}],
    [{"ItemID": "get_skin_paid", "Count": True}], [{"ItemID": "get_skin_paid", "Count": 1}] * 2,
    [{"ItemID": "missing_item", "Count": 1}]])
def test_invalid_acquisition_state_is_not_silently_ignored(possessions, state, rows):
    skins = catalog(possessions)
    state["tables"]["possessions"] = rows
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        skins.acquired_items(state)
    assert state == before


def test_every_built_catalog_is_ready_without_building_its_status(possessions, monkeypatch):
    skins = catalog(possessions)
    assert skins.is_ready() is skins.status()["ready"] is True
    monkeypatch.setattr(skins, "status", lambda: pytest.fail("Readiness built the full catalog status"))
    assert skins.is_ready() is True


def test_resource_hashes_are_read_only_and_match_status(possessions):
    rows = [("paid", 0, "get_skin_paid", True)]
    skins = catalog(possessions, rows, Cards({10: card(10, "paid")}))
    hashes = skins.resource_sha256
    assert dict(hashes) == skins.status()["resource_sha256"]
    assert hashes["SkinPackage.atb"] == hashlib.sha256(skin_table(rows)).hexdigest()
    assert hashes["ItemConfigTable.atb"] == possessions.asset_sha256
    with pytest.raises(TypeError):
        hashes["SkinPackage.atb"] = "0" * 64
    with pytest.raises(AttributeError):
        skins.resource_sha256 = {}


def test_possession_checks_never_build_the_possession_status(possessions, state, monkeypatch):
    monkeypatch.setattr(possessions, "status", lambda: pytest.fail("Skin checks built the possession status"))
    skins = catalog(possessions)
    state["tables"]["possessions"] = [{"ItemID": "get_skin_paid", "Count": 1}, {"ItemID": "other_item", "Count": 3}]
    before = deepcopy(state)
    assert skins.acquired_items(state) == frozenset({"get_skin_paid"})
    assert skins.allowed_displays(10, state, smartphone_images=False) == frozenset({5, 6})
    assert state == before

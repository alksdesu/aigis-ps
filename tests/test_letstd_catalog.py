from datetime import datetime
from pathlib import Path

import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.letstd_catalog import FIELDS, RESOURCE_NAME, TEXT_FIELDS, TEXT_RESOURCE, LetsTDCatalog
from aigis_ps.letstd_rules import PARTNER_ORDER, PARTNERS
from aigis_ps.resource_library import ResourceLibrary
from test_present_items import item_table


CLIENT_HASH = CURRENT_CLIENT_SHA256
SCHEMA = tuple(FIELDS.items())
TEXT_SCHEMA = (*TEXT_FIELDS.items(), ("GroupID", 1), ("GroupType", 1), ("GroupRecordID", 1))
BATCHES = ("2025/08/05 16:00:00", "2026/04/02 11:30:00", "2026/09/18 11:30:00")
SERVICE_NAMES = {"oshirore": "城プロRE", "monmusutd": "モンスター娘TD", "rlyeh": "ルルイエ少女隊"}
RECEIVE_TEXTS = {1: (-1896792027, 916842888), 2: (-167873995, -2000922814), 3: (-1896792027, 916842888),
                 4: (1523847063, 0)}
CHALLENGE = ((1, 1, 0, "item_smn_ticket_19113", 1, 0), (2, 1, 0, "gift_0003_diamond", 20, 0),
    (3, 2, 250, "", 1, 0), (4, 2, 136, "", 1, 0), (5, 2, 397, "", 1, 0), (6, 2, 136, "", 1, 0),
    (7, 2, 397, "", 1, 0), (8, 1, 0, "gift_0103_ring", 5, 0), (9, 1, 0, "item_smn_ticket_85106", 1, 0),
    (10, 2, 1269, "", 1, 0), (11, 2, 250, "", 1, 0), (12, 2, 1269, "", 1, 0),
    (13, 1, 0, "item_smn_ticket_60005", 1, 0), (14, 1, 0, "trade_item_005000", 4, 0), (15, 4, 0, "", 5000000, 0),
    (16, 2, 97, "", 1, 0), (17, 1, 0, "item_smn_ticket_40052", 1, 0), (18, 1, 0, "item_smn_ticket_85106", 1, 0),
    (19, 1, 0, "item_smn_ticket_85106", 1, 0), (20, 1, 0, "item_smn_ticket_19113", 1, 0),
    (21, 1, 0, "item_smn_ticket_19158", 1, 2), (22, 1, 0, "item_smn_ticket_19158", 1, 2),
    (23, 3, 0, "", 5, 2), (24, 3, 0, "", 5, 2), (25, 3, 0, "", 7, 2))
PURCHASE = ((1, 1, 0, "item_smn_ticket_40051", 2, 0), (2, 1, 0, "item_smn_ticket_40051", 1, 0),
    (3, 2, 250, "", 1, 0), (4, 1, 0, "item_smn_ticket_40062", 2, 1), (5, 1, 0, "item_smn_ticket_40062", 1, 1),
    (6, 2, 250, "", 1, 1), (8, 1, 0, "item_smn_ticket_40072", 2, 2), (9, 1, 0, "item_smn_ticket_40072", 1, 2),
    (10, 2, 250, "", 1, 2), (11, 1, 0, "item_smn_ticket_40072", 1, 2))
EMISSION = ("monmusutd", "oshirore", "rlyeh")


def text_id(partner, group):
    return 1 + PARTNER_ORDER[partner] * 2 + group - 1


def text_rows():
    rows = [{"Recode_Index": 0, "Data_Text": "", "GroupID": 0, "GroupType": 0, "GroupRecordID": 0}]
    for partner in PARTNERS:
        name = SERVICE_NAMES[partner]
        for title in (f"タワーディフェンスやろうぜ！\nキャンペーン報酬({name})", f"やろうぜパック購入特典\n({name})"):
            rows.append({"Recode_Index": len(rows), "Data_Text": title, "GroupID": 0, "GroupType": 0,
                         "GroupRecordID": 0})
    return rows


def reward_rows():
    rows = []
    for group, pattern in ((1, CHALLENGE), (2, PURCHASE)):
        for partner in EMISSION:
            for reward_id, kind, card, item, count, batch in pattern:
                single, multi = RECEIVE_TEXTS[kind]
                rows.append({"ConfigId": len(rows) + 1, "StartDate": BATCHES[batch], "EndDate": "",
                    "ServiceType": partner, "RewardGroupId": group, "RewardId": reward_id, "ItemType": kind,
                    "CardId": card, "GeneralItemID": item, "Count": count, "SystemTextID": text_id(partner, group),
                    "ReceiveTextIdSingle": single, "ReceiveTextIdMulti": multi})
    return rows


def resources(rows=None, texts=None, *, schema=SCHEMA, text_schema=TEXT_SCHEMA):
    return {RESOURCE_NAME: item_table(reward_rows() if rows is None else rows, schema),
            TEXT_RESOURCE: item_table(text_rows() if texts is None else texts, text_schema)}


def build(rows=None, texts=None, **kwargs):
    return LetsTDCatalog(resources(rows, texts, **kwargs).__getitem__)


def changed(index, **values):
    rows = reward_rows()
    rows[index].update(values)
    return rows


def invalid(action):
    with pytest.raises(ProtocolError) as failure:
        action()
    assert (failure.value.code, failure.value.status) == ("letstd_catalog_invalid", 503)


def keys(catalog):
    return [(row.partner, row.group, row.reward_id) for row in catalog.rows]


@pytest.fixture(scope="module")
def native_sources():
    root = Path(__file__).resolve().parents[1]
    try:
        _, entries = ResourceLibrary(root / "runtime/resources", client_hash=CLIENT_HASH).load()
        assets = IndexedAssetService(entries, root / "runtime/resources", root.parent / "aigisr_exe")
        return {name: assets.read(name)[0] for name in (RESOURCE_NAME, TEXT_RESOURCE)}
    except (OSError, ValueError) as error:
        pytest.skip(f"Verified local client resources are required: {error}")


def test_fixture_has_the_native_shape_and_canonical_order():
    catalog = build()
    status = catalog.status()
    assert len(SCHEMA) == 13 and status["ready"] is True and status["row_count"] == len(catalog.rows) == 105
    assert status["item_types"] == {"1": 57, "2": 36, "3": 9, "4": 3}
    assert status["start_dates"] == dict(zip(BATCHES, (69, 9, 27))) and status["open_ended_rows"] == 105
    assert all(status["groups"][partner] == {"newChallenge": {"rows": 25, "item_types": {"1": 12, "2": 9, "3": 3, "4": 1}},
        "newPurchase": {"rows": 10, "item_types": {"1": 7, "2": 3}}} for partner in PARTNERS)
    assert keys(catalog) == sorted(keys(catalog), key=lambda key: (PARTNER_ORDER[key[0]], key[1], key[2]))
    assert keys(catalog)[0] == ("oshirore", 1, 1) and keys(catalog)[-1] == ("rlyeh", 2, 11)
    assert [row.reward_id for row in catalog.rows_for("oshirore", 2)] == [1, 2, 3, 4, 5, 6, 8, 9, 10, 11]
    assert catalog.get("monmusutd", 2, 7) is None
    gold = catalog.get("rlyeh", 1, 15)
    assert (gold.item_type, gold.count, gold.card_id, gold.item_id, gold.end) == (4, 5000000, 0, "", None)
    assert gold.start == datetime(2025, 8, 5, 16, tzinfo=JST) and gold.start.utcoffset() == JST.utcoffset(None)
    assert gold.title == "タワーディフェンスやろうぜ！\nキャンペーン報酬(ルルイエ少女隊)"
    assert (gold.receive_single, gold.receive_multi) == (1523847063, 0)
    assert len({row.definition for row in catalog.rows}) == 105
    assert all(len(row.definition) == 64 for row in catalog.rows)
    assert build().content_version == catalog.content_version and len(catalog.content_version) == 64
    assert set(catalog.resource_hashes) == {RESOURCE_NAME, TEXT_RESOURCE}
    assert catalog.resource_sha256 == catalog.resource_hashes[RESOURCE_NAME]


def test_row_definitions_ignore_source_order_but_follow_each_value_and_title():
    original = build()
    reversed_rows = build(list(reversed(reward_rows())))
    assert keys(reversed_rows) == keys(original)
    assert [row.definition for row in reversed_rows.rows] == [row.definition for row in original.rows]
    assert reversed_rows.content_version != original.content_version
    index = next(index for index, row in enumerate(reward_rows()) if row["ServiceType"] == "oshirore"
                 and row["RewardGroupId"] == 1 and row["RewardId"] == 2)
    counted = build(changed(index, Count=21))
    differences = [row for row, before in zip(counted.rows, original.rows) if row.definition != before.definition]
    assert [(row.partner, row.group, row.reward_id) for row in differences] == [("oshirore", 1, 2)]
    texts = text_rows()
    texts[text_id("oshirore", 2)]["Data_Text"] = "やろうぜパック購入特典(城プロRE)"
    retitled = build(texts=texts)
    differences = {(row.partner, row.group) for row, before in zip(retitled.rows, original.rows)
                   if row.definition != before.definition}
    assert differences == {("oshirore", 2)}


@pytest.mark.parametrize("values", [{"ConfigId": 1}, {"RewardId": 1}])
def test_configuration_and_partner_reward_identities_are_unique(values):
    invalid(lambda: build(changed(1, **values)))


@pytest.mark.parametrize("values", [{"ServiceType": "td_shiro"}, {"ServiceType": "Oshirore"}, {"RewardGroupId": 0},
    {"RewardGroupId": 3}, {"RewardId": 0}, {"RewardId": -1}])
def test_partner_group_and_reward_domains_are_native(values):
    invalid(lambda: build(changed(1, **values)))


def first(kind):
    return next(index for index, row in enumerate(reward_rows()) if row["ItemType"] == kind)


@pytest.mark.parametrize("kind,values", [(1, {"GeneralItemID": ""}), (1, {"CardId": 250}), (2, {"CardId": 0}),
    (2, {"CardId": -4}), (2, {"Count": 2}), (2, {"Count": 0}), (3, {"Count": 0}), (4, {"Count": -1})])
def test_item_type_shapes_are_checked(kind, values):
    invalid(lambda: build(changed(first(kind), **values)))


@pytest.mark.parametrize("kind,values", [(1, {"Count": 0}), (3, {"GeneralItemID": "gift_0003_diamond"}),
    (4, {"CardId": 97}), (2, {"GeneralItemID": "trade_item_005000"}), (1, {"ItemType": 9})])
def test_shapes_outside_the_native_constraints_reach_the_service(kind, values):
    index = first(kind)
    source = changed(index, **values)[index]
    row = build(changed(index, **values)).get(source["ServiceType"], source["RewardGroupId"], source["RewardId"])
    assert (row.item_type, row.card_id, row.item_id, row.count) == (
        source["ItemType"], source["CardId"], source["GeneralItemID"], source["Count"])


def test_dates_are_canonical_jst_instants_and_empty_end_never_expires():
    row = build(changed(0, EndDate="2026/12/31 23:59:59")).rows_for("monmusutd", 1)[0]
    assert row.end == datetime(2026, 12, 31, 23, 59, 59, tzinfo=JST)
    assert build().rows_for("monmusutd", 1)[0].end is None


@pytest.mark.parametrize("values", [{"StartDate": "2025-08-05 16:00:00"}, {"StartDate": "2025/8/5 16:00:00"},
    {"StartDate": "2025/08/05"}, {"StartDate": ""}, {"StartDate": "2025/02/30 00:00:00"},
    {"StartDate": "0001/01/01 00:00:00"}, {"EndDate": "2026/12/31"}, {"EndDate": " "},
    {"EndDate": "2026/12/31 24:00:00"}])
def test_noncanonical_or_unrepresentable_dates_are_rejected(values):
    invalid(lambda: build(changed(0, **values)))


def test_native_columns_and_types_are_required():
    missing = tuple(field for field in SCHEMA if field[0] != "ReceiveTextIdMulti")
    invalid(lambda: build(schema=missing))
    invalid(lambda: build([{**row, "Extra": 0} for row in reward_rows()], schema=(*SCHEMA, ("Extra", 1))))
    retyped = tuple((name, 1 if name == "StartDate" else kind) for name, kind in SCHEMA)
    invalid(lambda: build([{**row, "StartDate": 0} for row in reward_rows()], schema=retyped))
    duplicated = (*SCHEMA, ("Count", 1))
    invalid(lambda: build(schema=duplicated))
    texts = [{"Recode_Index": row["Recode_Index"]} for row in text_rows()]
    invalid(lambda: build(texts=texts, text_schema=(("Recode_Index", 1),)))
    minimal = [{name: row[name] for name in TEXT_FIELDS} for row in text_rows()]
    assert build(texts=minimal, text_schema=tuple(TEXT_FIELDS.items())).status()["ready"] is True


@pytest.mark.parametrize("text_index", [-1, 7, 2 ** 31 - 1])
def test_titles_must_exist_in_the_text_table(text_index):
    invalid(lambda: build(changed(0, SystemTextID=text_index)))


def test_titles_are_bounded_and_xml_safe():
    for title in ("x" * 1025, "bad\x01title"):
        texts = text_rows()
        texts[text_id("monmusutd", 1)]["Data_Text"] = title
        invalid(lambda: build(texts=texts))


@pytest.mark.parametrize("reader", [None, "LetsTDRewards.atb", lambda name: None, lambda name: b"",
    lambda name: bytearray(resources()[name]), lambda name: b"ALTB"])
def test_reader_must_supply_bounded_native_tables(reader):
    invalid(lambda: LetsTDCatalog(reader))


def test_reader_failures_and_empty_tables_are_invalid():
    def missing(name):
        raise OSError(name)
    invalid(lambda: LetsTDCatalog(missing))
    invalid(lambda: build([]))


def test_frozen_catalog_rejects_later_mutation_and_bad_lookups():
    catalog = build()
    for name, value in (("_rows", ()), ("_content_version", "0" * 64)):
        current = build()
        setattr(current, name, value)
        with pytest.raises(ProtocolError) as failure:
            current.get("oshirore", 1, 1)
        assert failure.value.code == "letstd_catalog_changed"
        assert current.status()["ready"] is False and current.status()["error"] == "letstd_catalog_changed"
    for action in (lambda: catalog.get("td_shiro", 1, 1), lambda: catalog.rows_for("oshirore", "1"),
                   lambda: catalog.rows_for("oshirore", True), lambda: catalog.get("oshirore", 1, True)):
        with pytest.raises(ProtocolError) as failure:
            action()
        assert failure.value.code in ("letstd_group_invalid", "letstd_reward_invalid")


def test_native_resource_has_105_rows_with_the_recorded_distribution(native_sources):
    catalog = LetsTDCatalog(native_sources.__getitem__)
    status = catalog.status()
    assert status["ready"] is True and status["row_count"] == 105
    assert status["item_types"] == {"1": 57, "2": 36, "3": 9, "4": 3}
    assert status["start_dates"] == dict(zip(BATCHES, (69, 9, 27))) and status["open_ended_rows"] == 105
    for partner in PARTNERS:
        assert status["groups"][partner]["newChallenge"]["rows"] == 25
        assert status["groups"][partner]["newPurchase"]["rows"] == 10
        assert [row.reward_id for row in catalog.rows_for(partner, 2)] == [1, 2, 3, 4, 5, 6, 8, 9, 10, 11]
    assert len({row.config_id for row in catalog.rows}) == 105
    assert {row.card_id for row in catalog.rows if row.item_type == 2} == {97, 136, 250, 397, 1269}
    oshirore = catalog.get("oshirore", 1, 1)
    assert oshirore.title == "タワーディフェンスやろうぜ！\nキャンペーン報酬(城プロRE)"
    assert oshirore.item_id == "item_smn_ticket_19113" and oshirore.text_id == 5032
    assert catalog.get("rlyeh", 2, 3).title == "やろうぜパック購入特典\n(ルルイエ少女隊)"

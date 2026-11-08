from datetime import datetime
import hashlib
import json
import struct

import lz4.frame
import pytest

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.energy_clock import JST
from aigis_ps.shop_window_closure import (CLOSED_WINDOW, METADATA_FIELDS, PRODUCT_FIELDS, build_shop_window_closure,
                                          closure_terms)
from aigis_ps.trade_catalog import SHOP_FIELDS, SHOP_RESOURCE, product_headers
from test_trade_catalog import catalog, fails, native_date, product, resources, swapped_fields, table


WINDOW_FIELDS = tuple(CLOSED_WINDOW)
ENDED_CELLS = {name: [value, 0] for name, value in CLOSED_WINDOW.items()}
UNTIL = datetime(2026, 11, 12, 11, tzinfo=JST)
EARLIEST_CLOCK = datetime(1970, 1, 1, tzinfo=JST)


def summon(shop_id=15, **changes):
    return product(shop_id, ProductType=2, Type=4, CardID=10000, RequiredValue=500, ProductName="summon", **changes)


def tier(count, price, **changes):
    return product(0, **{"ProductType": 0, "Type": 0, "CardID": 0, "TradeCount": count, "RequiredValue": price,
                         **changes})


def shop_rows():
    return [product(1), summon(), tier(2, 1000), tier(3, 1500), product(14, Type=2, EndDateTime=native_date(UNTIL))]


def compressed(raw):
    frame = lz4.frame.compress(raw, block_size=lz4.frame.BLOCKSIZE_MAX64KB, block_linked=False,
                               content_checksum=True, block_checksum=True, store_size=True)
    return struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + frame


def cell_span(payload, row):
    parsed = parse_table(payload)
    fields = {field["name"]: field for field in parsed["fields"]}
    start = parsed["data_offset"] + row * parsed["record_size"]
    return {position for name in WINDOW_FIELDS
            for position in range(start + fields[name]["offset"], start + fields[name]["offset"] + fields[name]["size"])}


def closed_payload(rows):
    payload = table(rows, SHOP_FIELDS)
    return payload, build_shop_window_closure(payload)


def test_product_headers_open_a_product_unless_the_trade_count_keeps_rising():
    assert product_headers([1, 2, 3, 1, 1, 5, 2]) == (0, 3, 4, 6)
    assert product_headers([]) == ()


def test_only_the_two_window_cells_of_the_summon_header_change():
    payload, closure = closed_payload(shop_rows())
    effective = closure.replacements[SHOP_RESOURCE]
    assert len(effective) == len(payload)
    changed = {index for index, (before, after) in enumerate(zip(payload, effective)) if before != after}
    assert changed and changed <= cell_span(payload, 1)
    before, after = parse_table(payload)["rows"], parse_table(effective)["rows"]
    assert after[1] == {**before[1], **ENDED_CELLS}
    assert [row for index, row in enumerate(after) if index != 1] == [row for index, row in enumerate(before) if index != 1]
    assert closure.shop_ids == (15,)
    assert closure.source_sha256 == hashlib.sha256(payload).hexdigest()
    assert closure.effective_sha256 == hashlib.sha256(effective).hexdigest()
    assert [product.to_dict() for product in closure.products] == [{
        "shop_id": 15, "name": "summon", "row": 1, "rows": 3, "original": {"start_at": None, "end_at": None}}]


def test_products_are_selected_by_rule_and_only_at_their_header_rows():
    rows = [summon(), tier(2, 1000, ProductType=2, Type=4), summon(900, StartDateTime=native_date(UNTIL)),
            product(16, ProductType=2, Type=1), product(17, ProductType=1, Type=4)]
    payload, closure = closed_payload(rows)
    assert closure.shop_ids == (15, 900) and [product.row for product in closure.products] == [0, 2]
    assert closure.products[1].original == {"start_at": UNTIL.isoformat(timespec="milliseconds"), "end_at": None}
    after = parse_table(closure.replacements[SHOP_RESOURCE])["rows"]
    assert [index for index, row in enumerate(after) if row != parse_table(payload)["rows"][index]] == [0, 2]


def test_repeated_builds_are_identical_and_an_ended_table_patches_nothing():
    payload, first = closed_payload(shop_rows())
    assert build_shop_window_closure(payload) == first
    effective = first.replacements[SHOP_RESOURCE]
    again = build_shop_window_closure(effective)
    assert again.replacements == {} and again.shop_ids == (15,)
    assert again.source_sha256 == again.effective_sha256 == first.effective_sha256
    assert again.products[0].original == closure_terms()["window"]


def test_compressed_resources_keep_their_container_and_close_the_same_cells():
    raw, plain = closed_payload(shop_rows())
    payload = compressed(raw)
    closure = build_shop_window_closure(payload)
    effective = closure.replacements[SHOP_RESOURCE]
    assert effective.startswith(payload[:8]) and effective != payload
    assert decompress_all4(effective) == plain.replacements[SHOP_RESOURCE]
    assert closure.products == plain.products


def test_tables_without_summon_products_patch_nothing():
    payload, closure = closed_payload([product(1), product(2, Type=2)])
    assert closure.replacements == {} and closure.products == ()
    assert closure.source_sha256 == closure.effective_sha256 == hashlib.sha256(payload).hexdigest()
    assert closure.metadata["products"] == []


def test_metadata_names_the_native_terms_and_is_a_detached_json_value():
    _, closure = closed_payload(shop_rows())
    metadata = closure.metadata
    json.dumps(metadata)
    assert set(metadata) == METADATA_FIELDS and set(metadata["products"][0]) == PRODUCT_FIELDS
    assert {name: metadata[name] for name in closure_terms()} == closure_terms() == {
        "registration": 108, "policy": "ended_sale_window_818200", "table": SHOP_RESOURCE,
        "selection": {"ProductType": 2, "Type": 4},
        "window": {"start_at": "0001-01-01T00:00:00.001+09:00", "end_at": "0001-01-01T00:00:00.002+09:00"}}
    metadata["products"].clear()
    metadata["selection"]["Type"] = 0
    assert closure.metadata["products"] and closure_terms()["selection"]["Type"] == 4
    with pytest.raises(TypeError):
        closure.replacements[SHOP_RESOURCE] = b"changed"


@pytest.mark.parametrize("payload", [
    pytest.param(lambda: b"", id="empty"),
    pytest.param(lambda: bytearray(table(shop_rows(), SHOP_FIELDS)), id="mutable"),
    pytest.param(lambda: b"ALTB not a table", id="garbage"),
    pytest.param(lambda: table(shop_rows(), swapped_fields()), id="reordered"),
    pytest.param(lambda: table([{**row, "TradeCount": str(row["TradeCount"])} for row in shop_rows()],
                               {**SHOP_FIELDS, "TradeCount": 32}), id="retyped"),
    pytest.param(lambda: table(shop_rows(), {name: kind for name, kind in SHOP_FIELDS.items() if name != "UnitInitTrust"}),
                 id="missing"),
    pytest.param(lambda: table([summon(), tier(0, 1000)], SHOP_FIELDS), id="non_positive_count"),
    pytest.param(lambda: table([summon(), summon()], SHOP_FIELDS), id="duplicated_shop"),
    pytest.param(lambda: table([summon(0)], SHOP_FIELDS), id="unnamed_shop"),
])
def test_unexpected_table_structures_are_refused(payload):
    with pytest.raises(ValueError):
        build_shop_window_closure(payload())


def test_trade_catalog_reads_the_closed_window_as_ended_and_keeps_every_other_product():
    payloads = resources(shop_rows())
    before = catalog(payloads)
    payloads[SHOP_RESOURCE] = build_shop_window_closure(payloads[SHOP_RESOURCE]).replacements[SHOP_RESOURCE]
    after = catalog(payloads)
    closed = after.product(15)
    assert (closed.start_at, closed.end_at) == tuple(datetime.fromisoformat(value)
                                                     for value in closure_terms()["window"].values())
    assert closed.start_at < closed.end_at <= EARLIEST_CLOCK
    assert closed.tiers == before.product(15).tiers == ((1, 500), (2, 1000), (3, 1500))
    assert [item for item in after.products if item.shop_id != 15] == [item for item in before.products if item.shop_id != 15]
    fails("trade_product_unsupported", lambda: after.quote({}, 15, 1, 0, UNTIL, 107))
    assert after.content_version != before.content_version

from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.errors import ProtocolError
import aigis_ps.item_summon_catalog as module
from aigis_ps.item_summon_catalog import (CATEGORY, ENSURED_RULE, ITEM_FIELDS, MAX_RULE_LIST_BYTES, MAX_SUMMON_COUNT,
    RARITY_FIELDS, RULE_FIELDS, UNOWNED_RULE, ItemSummonCatalog, LotteryRule)
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.system_literals import encode_command
from test_native_numbers import BOUNDARY_VALUES, fits_int32
from test_present_items import item_table


ITEM = "item_smn_ticket_test"
# cfgRarity keeps ★5 (ConfigID 5 and 11) and ★6 (ConfigID 6 and 12) in shared groups; ConfigID 1006 is a placeholder.
RARITIES = ((5, 5), (6, 6), (11, 5), (12, 6), (1006, 0))


def signed_hash(value):
    return (zlib.crc32(value.encode()) ^ 0x80000000) - 0x80000000


def row(name=ITEM, **updates):
    result = dict.fromkeys(ITEM_FIELDS, 0)
    result.update(ItemID=name, ID=signed_hash(name), EffectType=2, Category=CATEGORY, CanPresent=1,
                  InvisibleContaint=1, NoAutoDelete=1, SummonConfigID=428, SummonCount=10, EventID="")
    result.update(updates)
    return result


def rule(identifier=6, rarities="6,", cards="", kind=ENSURED_RULE, ensured=1):
    return {"RuleID": identifier, "RarityIDList": rarities, "CardIDList": cards, "RuleType": kind, "EnsuredCount": ensured}


def pools():
    return SimpleNamespace(content_version="a" * 64, status=lambda: {"ready": True}, is_ready=lambda: True)


def payloads(rows=None, command='CMenuTask_CallCommand( "UseCommonItemSummonTicket" );', *, rules=None,
             rarities=RARITIES):
    return {"ItemConfigTable.atb": item_table(rows if rows is not None else [row()], tuple(ITEM_FIELDS.items())),
        "OnCommandMenuPresentList.atb": item_table([{"_ID": signed_hash(module.RECEIVE_COMMAND), "_Cmnd": encode_command(command)}],
            (("_ID", 1), ("_Cmnd", 32))),
        "LotteryRuleConfig.atb": item_table(rules if rules is not None else [rule()], tuple(RULE_FIELDS.items())),
        "cfgRarity.atb": item_table([{"ConfigID": config, "RarityGroupID": group} for config, group in rarities],
                                    tuple(RARITY_FIELDS.items()))}


def create(rows=None, *, source=None, dependency=None):
    source = payloads(rows) if source is None else source
    return ItemSummonCatalog(pools() if dependency is None else dependency, source.__getitem__)


def decoded_cell(monkeypatch, marker, field, value):
    parser = module.parse_table
    def changed(raw, limits):
        result = parser(raw, limits)
        if marker in {entry["name"] for entry in result["fields"]}:
            result["rows"][0][field] = value
        return result
    monkeypatch.setattr(module, "parse_table", changed)


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    root = project / "runtime/resources"
    _, entries = ResourceLibrary(root, client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, root, project.parent / "aigisr_exe")
    dependency = pools()
    dependency.content_version = hashlib.sha256(assets.read("SummonConfig.atb")[0]).hexdigest()
    reader = lambda name: assets.read(name)[0]
    return SimpleNamespace(catalog=ItemSummonCatalog(dependency, reader), reader=reader)


def test_current_all_effect_two_identities_are_preserved(current):
    catalog = current.catalog
    assert len(catalog.definitions) == 639
    assert catalog.status()["supported_shape_count"] == 571
    assert catalog.status()["count_distribution"] == {10: 244, 1: 395}
    assert len(catalog.status()["unsupported"]) == 68
    assert catalog.item_ids == frozenset(catalog.definitions)
    assert all(catalog.supports_item(name) for name in catalog.item_ids)
    raw = current.reader("ItemConfigTable.atb")
    data = decompress_all4(raw) if raw.startswith(b"ALL4") else raw
    assert catalog.item_resource_sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("name,pool,count", [
    ("item_smn_ticket_140001", 163, 1), ("item_smn_ticket_130001", 162, 10),
    ("item_smn_ticket_150001", 164, 1), ("item_smn_ticket_19138", 428, 10),
    ("item_smn_ticket_160047", 466, 1), ("item_smn_ticket_160048", 473, 1),
])
def test_six_current_mission_tickets_keep_exact_pool_and_count(current, name, pool, count):
    definition = current.catalog.definition(name)
    assert (definition.item_id, definition.pool_id, definition.count) == (name, pool, count)
    assert definition.unsupported_reason is None
    assert current.catalog.source_rows[name]["LotteryRuleID"] == 0


def test_current_pack_actions_stay_unsupported_while_mail_rules_resolve_per_item(current):
    for name, metadata in current.catalog.source_rows.items():
        definition = current.catalog.definition(name)
        if metadata["ReceiveAction"]:
            assert "receive_action_unsupported" in definition.unsupported_reason
        elif metadata["LotteryRuleID"]:
            assert definition.supported and definition.rule.rule_id == metadata["LotteryRuleID"]
        else:
            assert definition.rule is None
    assert not current.catalog.definition("item_sample_smn_0003").supported
    status = current.catalog.status()
    assert status["rule_distribution"] == {3: 3, 4: 27, 6: 7, 7: 1, 9: 3}
    assert set(status["unsupported_rules"]) == {1, 5, 8, 10}


def test_current_rules_resolve_rarity_groups_and_refuse_card_and_multiple_rarity_lists(current):
    rules = current.catalog.lottery_rules
    assert rules[4] == LotteryRule(4, UNOWNED_RULE, 6, 6, 0) and rules[3] == LotteryRule(3, UNOWNED_RULE, 5, 5, 0)
    assert rules[6] == LotteryRule(6, ENSURED_RULE, 6, 6, 1) and rules[7] == LotteryRule(7, ENSURED_RULE, 6, 6, 3)
    assert rules[9] == LotteryRule(9, ENSURED_RULE, 5, 5, 1) and rules[2] == LotteryRule(2, ENSURED_RULE, 12, 6, 1)
    assert rules[1] == rules[10] == ("lottery_rule_rarity_unsupported",)
    assert rules[5] == rules[8] == ("lottery_rule_type_unsupported", "lottery_rule_cards_unsupported",
                                    "lottery_rule_rarity_unsupported")
    assert current.catalog.definition("item_smn_ticket_19056").rule == rules[6]
    assert current.catalog.definition("item_smn_ticket_19055").rule is None
    assert [current.catalog.rarity_group(rare) for rare in (0, 3, 4, 5, 7, 10, 11)] == [1, 4, 5, 6, 8, 5, 6]


@pytest.mark.parametrize("rare", [-1, 1003, 0x7FFFFFFF, True, None, "4"])
def test_rarity_groups_ignore_placeholders_and_non_integers(rare):
    assert create().rarity_group(rare) is None


@pytest.mark.parametrize("rare", BOUNDARY_VALUES)
def test_rarity_groups_read_only_plain_nonnegative_int32_rarities(rare):
    groups = {1: 3, 2: 4}
    catalog = create(source=payloads(rarities=RARITIES + tuple(groups.items())))
    assert catalog.rarity_group(rare) == (groups.get(rare + 1) if fits_int32(rare) else None)


@pytest.mark.parametrize("entry,reason", [("2147483647,", None), ("2147483648,", "lottery_rule_rarity_unsupported")])
def test_rule_lists_read_identifiers_up_to_the_native_int32_maximum(entry, reason):
    catalog = create(source=payloads([row(LotteryRuleID=6)], rules=[rule(rarities=entry)],
                                     rarities=RARITIES + ((2147483647, 6),)))
    assert catalog.definition(ITEM).unsupported_reason == reason


@pytest.mark.parametrize("item,source_rule,reasons", [
    ({"LotteryRuleID": 7}, rule(), {"lottery_rule_unknown"}),
    ({"LotteryRuleID": 6}, rule(kind=3), {"lottery_rule_type_unsupported"}),
    ({"LotteryRuleID": 6}, rule(cards="2850,"), {"lottery_rule_cards_unsupported"}),
    ({"LotteryRuleID": 6}, rule(rarities="5,6,"), {"lottery_rule_rarity_unsupported"}),
    ({"LotteryRuleID": 6}, rule(rarities=""), {"lottery_rule_rarity_unsupported"}),
    ({"LotteryRuleID": 6}, rule(rarities="7,"), {"lottery_rule_rarity_unsupported"}),
    ({"LotteryRuleID": 6}, rule(rarities="1006,"), {"lottery_rule_rarity_unsupported"}),
    ({"LotteryRuleID": 6}, rule(rarities="6,,"), {"lottery_rule_rarity_unsupported"}),
    ({"LotteryRuleID": 6}, rule(rarities=" 6,"), {"lottery_rule_rarity_unsupported"}),
    ({"LotteryRuleID": 6}, rule(rarities="06,"), {"lottery_rule_rarity_unsupported"}),
    ({"LotteryRuleID": 6}, rule(rarities="6;12"), {"lottery_rule_rarity_unsupported"}),
    ({"LotteryRuleID": 6}, rule(rarities="4294967302,"), {"lottery_rule_rarity_unsupported"}),
    ({"LotteryRuleID": 6}, rule(ensured=0), {"lottery_rule_count_unsupported"}),
    ({"LotteryRuleID": 6}, rule(ensured=11), {"lottery_rule_count_unsupported"}),
    ({"LotteryRuleID": 4, "SummonCount": 10}, rule(4, kind=UNOWNED_RULE, ensured=0), {"lottery_rule_count_unsupported"}),
    ({"LotteryRuleID": 4, "SummonCount": 1}, rule(4, kind=UNOWNED_RULE, ensured=1), {"lottery_rule_count_unsupported"}),
    ({"LotteryRuleID": 5}, rule(5, rarities="", cards="2850,", kind=3),
     {"lottery_rule_type_unsupported", "lottery_rule_cards_unsupported", "lottery_rule_rarity_unsupported"}),
])
def test_unsupported_rule_shapes_keep_the_ticket_identifiable(item, source_rule, reasons):
    catalog = create(source=payloads([row(**item)], rules=[source_rule]))
    definition = catalog.definition(ITEM)
    assert set(definition.unsupported_reason.split(";")) == reasons and definition.rule is None
    assert catalog.supports_item(ITEM) and catalog.status()["ready"]


@pytest.mark.parametrize("item,source_rule,expected", [
    ({"LotteryRuleID": 6}, rule(), LotteryRule(6, ENSURED_RULE, 6, 6, 1)),
    ({"LotteryRuleID": 6}, rule(rarities="6"), LotteryRule(6, ENSURED_RULE, 6, 6, 1)),
    ({"LotteryRuleID": 7}, rule(7, ensured=10), LotteryRule(7, ENSURED_RULE, 6, 6, 10)),
    ({"LotteryRuleID": 9}, rule(9, rarities="11,"), LotteryRule(9, ENSURED_RULE, 11, 5, 1)),
    ({"LotteryRuleID": 4, "SummonCount": 1}, rule(4, rarities="12,", kind=UNOWNED_RULE, ensured=0),
     LotteryRule(4, UNOWNED_RULE, 12, 6, 0)),
])
def test_supported_rule_shapes_attach_a_frozen_rule(item, source_rule, expected):
    catalog = create(source=payloads([row(**item)], rules=[source_rule]))
    definition = catalog.definition(ITEM)
    assert definition.supported and definition.rule == expected
    assert catalog.status()["rule_distribution"] == {expected.rule_id: 1}
    with pytest.raises(FrozenInstanceError):
        definition.rule.ensured_count = 0


def test_no_random_selection_or_pool_completeness_is_inferred():
    dependency = pools()
    dependency.get = lambda _: pytest.fail("Catalog must not choose or validate a candidate pool")
    dependency.draw = dependency.get
    catalog = create([row(SummonConfigID=999999)], dependency=dependency)
    assert catalog.definition(ITEM).supported
    assert catalog.definition(ITEM).pool_id == 999999


@pytest.mark.parametrize("count", [1, 10, MAX_SUMMON_COUNT])
def test_supported_count_boundary(count):
    definition = create([row(SummonCount=count)]).definition(ITEM)
    assert definition.count == count and definition.supported


@pytest.mark.parametrize("updates,reason", [
    ({"SummonCount": 0}, "summon_count_unsupported"), ({"SummonCount": -1}, "summon_count_unsupported"),
    ({"SummonCount": 101}, "summon_count_unsupported"), ({"SummonConfigID": 0}, "pool_id_unsupported"),
    ({"SummonConfigID": -1}, "pool_id_unsupported"), ({"ReceiveAction": 3}, "receive_action_unsupported"),
    ({"LotteryRuleID": 7}, "lottery_rule_unknown"), ({"EventID": "event"}, "event_unsupported"),
    ({"Category": 0}, "item_shape_unsupported"), ({"Stack": 1}, "item_shape_unsupported"),
    ({"CanPresent": 0}, "item_shape_unsupported"), ({"InvisibleContaint": 0}, "item_shape_unsupported"),
    ({"CanAllGet": 1}, "item_shape_unsupported"), ({"NoAutoDelete": 0}, "item_shape_unsupported"),
])
def test_unknown_semantics_preserve_identity_and_source_values(updates, reason):
    catalog = create([row(**updates)])
    assert catalog.supports_item(ITEM)
    definition = catalog.definition(ITEM)
    assert reason in definition.unsupported_reason
    assert not definition.supported
    assert all(catalog.source_rows[ITEM][key] == value for key, value in updates.items())
    assert catalog.status()["ready"]


def test_combined_unsupported_reasons_are_not_lost():
    definition = create([row(SummonCount=101, EventID="event", LotteryRuleID=2, ReceiveAction=3)]).definition(ITEM)
    assert set(definition.unsupported_reason.split(";")) == {
        "summon_count_unsupported", "event_unsupported", "lottery_rule_unknown", "receive_action_unsupported"}


def test_pack_action_stays_unsupported_even_with_a_supported_rule():
    definition = create([row(ReceiveAction=3, LotteryRuleID=6)]).definition(ITEM)
    assert definition.unsupported_reason == "receive_action_unsupported"
    assert definition.rule == LotteryRule(6, ENSURED_RULE, 6, 6, 1) and not definition.supported


def test_selection_and_other_effects_are_not_random_tickets():
    catalog = create([row(), row("selection", EffectType=3), row("gift", EffectType=4)])
    assert catalog.supports_item(ITEM)
    assert not catalog.supports_item("selection")
    assert not catalog.supports_item("gift")
    assert set(catalog.definitions) == {ITEM}


@pytest.mark.parametrize("name", [None, True, 1, "unknown", [], {}])
def test_unknown_names_fail_without_type_confusion(name):
    catalog = create()
    assert catalog.supports_item(name) is False
    with pytest.raises(ProtocolError) as error:
        catalog.definition(name)
    assert (error.value.code, error.value.status) == ("item_summon_unknown", 501)


def test_frozen_entries_maps_and_status():
    catalog = create()
    with pytest.raises(FrozenInstanceError):
        catalog.definition(ITEM).count = 1
    with pytest.raises(TypeError):
        catalog.definitions[ITEM] = None
    with pytest.raises(TypeError):
        catalog.source_rows[ITEM]["LotteryRuleID"] = 0
    with pytest.raises(AttributeError):
        catalog.item_ids = frozenset()
    with pytest.raises(TypeError):
        catalog.lottery_rules[6] = None
    status = catalog.status()
    status["resource_sha256"].clear()
    status["rule_distribution"].clear()
    assert len(catalog.status()["resource_sha256"]) == 4
    assert catalog.status()["rule_distribution"] == {}


def test_rule_tickets_are_counted_per_rule():
    catalog = create([row(), row("ensured", LotteryRuleID=6), row("again", LotteryRuleID=6),
                      row("unknown", LotteryRuleID=9)])
    status = catalog.status()
    assert status["rule_distribution"] == {6: 2} and status["unsupported_rules"] == {}
    assert status["unsupported"] == {"unknown": "lottery_rule_unknown"}


@pytest.mark.parametrize("rows", [[], [row(), row()], [row(ID=12)], [row(EffectType=4)]],
    ids=["empty", "duplicate", "wrong-crc", "no-tickets"])
def test_invalid_identity_catalogs_fail(rows):
    with pytest.raises(ProtocolError) as error:
        create(rows)
    assert error.value.code == "item_summon_catalog_invalid"


@pytest.mark.parametrize("field", ["ID", "EffectType", "SummonCount", "SummonConfigID", "LotteryRuleID", "ReceiveAction"])
def test_decoded_bool_is_not_native_integer(monkeypatch, field):
    decoded_cell(monkeypatch, "ItemID", field, True)
    with pytest.raises(ProtocolError) as error:
        create()
    assert error.value.code == "item_summon_catalog_invalid"


@pytest.mark.parametrize("marker,field", [("RuleType", "RuleID"), ("RuleType", "RuleType"), ("RuleType", "EnsuredCount"),
                                          ("RarityGroupID", "ConfigID"), ("RarityGroupID", "RarityGroupID")])
def test_decoded_bool_rule_or_rarity_value_is_not_native_integer(monkeypatch, marker, field):
    decoded_cell(monkeypatch, marker, field, True)
    with pytest.raises(ProtocolError) as error:
        create()
    assert error.value.code == "item_summon_catalog_invalid"


@pytest.mark.parametrize("marker,field,minimum", [("ItemID", "TextID", None), ("RuleType", "EnsuredCount", None),
    ("RuleType", "RuleID", 1), ("RarityGroupID", "ConfigID", 1), ("RarityGroupID", "RarityGroupID", 0)])
@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_decoded_integer_cells_admit_exactly_the_native_int32_range(monkeypatch, marker, field, minimum, value):
    decoded_cell(monkeypatch, marker, field, value)
    if fits_int32(value) and (minimum is None or value >= minimum):
        assert create().status()["ready"]
    else:
        with pytest.raises(ProtocolError) as error:
            create()
        assert error.value.code == "item_summon_catalog_invalid"


@pytest.mark.parametrize("rules,rarities", [
    ([rule(), rule()], RARITIES), ([rule(0)], RARITIES), ([rule(-1)], RARITIES),
    ([rule(rarities="6," * (MAX_RULE_LIST_BYTES // 2 + 1))], RARITIES), ([rule(cards="1" + chr(1))], RARITIES),
    ([rule()], RARITIES + ((6, 6),)), ([rule()], ((0, 1),)), ([rule()], ((6, -1),)),
], ids=["duplicate-rule", "zero-rule", "negative-rule", "long-list", "control-character", "duplicate-rarity",
        "zero-rarity", "negative-group"])
def test_invalid_rule_or_rarity_tables_fail_the_catalog(rules, rarities):
    with pytest.raises(ProtocolError) as error:
        create(source=payloads(rules=rules, rarities=rarities))
    assert error.value.code == "item_summon_catalog_invalid"


def test_empty_rule_table_leaves_rule_tickets_unknown():
    catalog = create(source=payloads([row(), row("ensured", LotteryRuleID=6)], rules=[]))
    assert catalog.definition(ITEM).supported
    assert catalog.definition("ensured").unsupported_reason == "lottery_rule_unknown"


@pytest.mark.parametrize("command", ['', 'CMenuTaskSendMessage(0,"DoGetItem");',
    'CMenuTask_CallCommand("UseCommonItemSummonTicket"); Evil();'])
def test_changed_claim_dispatch_is_not_accepted(command):
    with pytest.raises(ProtocolError) as error:
        create(source=payloads(command=command))
    assert error.value.code == "item_summon_catalog_invalid"


@pytest.mark.parametrize("name", ["ItemConfigTable.atb", "OnCommandMenuPresentList.atb", "LotteryRuleConfig.atb",
                                  "cfgRarity.atb"])
def test_missing_resource_is_domain_error(name):
    source = payloads()
    del source[name]
    with pytest.raises(ProtocolError) as error:
        create(source=source)
    assert error.value.code == "item_summon_catalog_invalid"


def test_missing_field_and_wrong_type_are_rejected():
    for mode in ("missing", "wrong_type"):
        fields = dict(ITEM_FIELDS)
        source_row = row()
        if mode == "missing":
            fields.pop("LotteryRuleID")
        else:
            fields["SummonCount"] = 32
            source_row["SummonCount"] = "10"
        source = payloads()
        source["ItemConfigTable.atb"] = item_table([source_row], tuple(fields.items()))
        with pytest.raises(ProtocolError):
            create(source=source)


@pytest.mark.parametrize("mode", ["missing", "extra", "wrong_type"])
def test_rule_table_requires_exactly_the_native_columns(mode):
    fields, source_rule = dict(RULE_FIELDS), rule()
    if mode == "missing":
        fields.pop("EnsuredCount")
    elif mode == "extra":
        fields["Priority"] = source_rule["Priority"] = 1
    else:
        fields["RuleType"], source_rule["RuleType"] = 32, "1"
    source = payloads()
    source["LotteryRuleConfig.atb"] = item_table([source_rule], tuple(fields.items()))
    with pytest.raises(ProtocolError) as error:
        create(source=source)
    assert error.value.code == "item_summon_catalog_invalid"


def test_rarity_table_needs_its_group_column_but_tolerates_others():
    rows = [{"ConfigID": config, "RarityGroupID": group, "StarsCount": group} for config, group in RARITIES]
    source = payloads()
    source["cfgRarity.atb"] = item_table(rows, (*RARITY_FIELDS.items(), ("StarsCount", 1)))
    assert create(source=source).rarity_group(5) == 6
    source["cfgRarity.atb"] = item_table(rows, (("ConfigID", 1), ("StarsCount", 1)))
    with pytest.raises(ProtocolError) as error:
        create(source=source)
    assert error.value.code == "item_summon_catalog_invalid"


@pytest.mark.parametrize("name", ["x" * (STORED_ITEM_NAME_BYTES + 1), "x" + chr(1), chr(0x1F600) * 64 + "x"])
def test_item_name_bounds_and_xml_safety(name):
    with pytest.raises(ProtocolError):
        create([row(name)])


def test_maximum_utf8_name_is_retained():
    name = chr(0x1F600) * 63 + "xxx"
    assert len(name.encode("utf-8")) == MAX_ITEM_NAME_BYTES
    catalog = create([row(name)])
    assert catalog.supports_item(name) and catalog.definition(name).supported


@pytest.mark.parametrize("name", ["x" * STORED_ITEM_NAME_BYTES, chr(0x1F600) * 64], ids=["ascii", "utf8"])
def test_stored_bound_name_stays_identifiable_but_is_never_usable(name):
    assert len(name.encode("utf-8")) == STORED_ITEM_NAME_BYTES
    catalog = create([row(name), row()])
    definition = catalog.definition(name)
    assert catalog.supports_item(name) and name in catalog.item_ids
    assert definition.unsupported_reason == "item_name_unsupported" and not definition.supported
    assert (definition.pool_id, definition.count) == (428, 10)
    assert catalog.status()["unsupported"] == {name: "item_name_unsupported"}
    assert catalog.status()["ready"] and catalog.definition(ITEM).supported


def test_stored_bound_name_keeps_its_other_unsupported_reasons():
    name = "x" * STORED_ITEM_NAME_BYTES
    definition = create([row(name, ReceiveAction=3)]).definition(name)
    assert definition.unsupported_reason.split(";") == ["item_name_unsupported", "receive_action_unsupported"]


def test_compression_and_source_fingerprints_are_frozen():
    source = payloads()
    packed = {name: struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
              for name, raw in source.items()}
    first, second = create(source=source), create(source=packed)
    assert first.definition(ITEM) == second.definition(ITEM)
    assert first.lottery_rules == second.lottery_rules == {6: LotteryRule(6, ENSURED_RULE, 6, 6, 1)}
    assert first.item_resource_sha256 == second.item_resource_sha256
    assert first.content_version != second.content_version
    status = deepcopy(first.status())
    source["ItemConfigTable.atb"] = b"modified source"
    assert first.definition(ITEM).count == 10
    assert first.status() == status


def test_fingerprint_changes_with_metadata_and_pool_version():
    first = create()
    assert create().content_version == first.content_version
    assert create([row(SummonCount=1)]).content_version != first.content_version
    dependency = pools()
    dependency.content_version = "b" * 64
    assert create(dependency=dependency).content_version != first.content_version


@pytest.mark.parametrize("source", [payloads(rules=[rule(ensured=2)]), payloads(rules=[rule(), rule(9, "5,")]),
                                    payloads(rarities=RARITIES[:-1]), payloads(rarities=((5, 5), (6, 5)))],
                         ids=["ensured", "added-rule", "removed-placeholder", "regrouped"])
def test_fingerprint_follows_rule_and_rarity_tables(source):
    first = create()
    changed = create(source=source)
    assert changed.content_version != first.content_version
    assert changed.status()["resource_sha256"].keys() == first.status()["resource_sha256"].keys()
    assert changed.definition(ITEM) == first.definition(ITEM)


@pytest.mark.parametrize("changed", ["version", "readiness"])
def test_mutated_pool_dependency_blocks_access(changed):
    dependency = pools()
    catalog = create(dependency=dependency)
    assert catalog.is_ready() is catalog.status()["ready"] is True
    if changed == "version":
        dependency.content_version = "b" * 64
    else:
        dependency.is_ready = lambda: False
    assert catalog.is_ready() is catalog.status()["ready"] is False
    assert catalog.status()["error"] == "item_summon_catalog_changed"
    for callback in (lambda: catalog.definition(ITEM), lambda: catalog.supports_item(ITEM)):
        with pytest.raises(ProtocolError) as error:
            callback()
        assert error.value.code == "item_summon_catalog_changed"


def test_pool_readiness_is_checked_without_building_the_pool_status():
    dependency = pools()
    dependency.status = lambda: pytest.fail("Readiness built the full pool status")
    catalog = create(dependency=dependency)
    assert catalog.is_ready() is catalog.status()["ready"] is True
    assert catalog.supports_item(ITEM) and catalog.definition(ITEM).supported


@pytest.mark.parametrize("version", [None, "", "wrong", 1, "z" * 64])
def test_bad_dependency_identity_fails_construction(version):
    dependency = pools()
    dependency.content_version = version
    with pytest.raises(ProtocolError) as error:
        create(dependency=dependency)
    assert error.value.code == "item_summon_catalog_invalid"

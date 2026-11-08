import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps.al_format import decompress_all4, parse_archive, parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.errors import ProtocolError
import aigis_ps.evolution_materials as module
from aigis_ps.evolution_materials import EVOLUTION_MATERIALS_INVALID, EvolutionMaterials, MaterialCondition
from aigis_ps.gift_catalog import GiftCatalog
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.possessions import PossessionService
from aigis_ps.resource_library import ResourceLibrary


class Cards:
    def __init__(self):
        self.rows = {identifier: {"CardID": identifier, "InitClassID": cls, "Rare": rarity}
                     for identifier, cls, rarity in [(1, 100, 3), (2, 100, 2), (3, 200, 3), (4, 300, 2),
                         (5, 100, 3), (6, 300, 3), (7, 200, 2), (9, 900, 1),
                         (136, 11, 5), (397, 30, 5), (9136, 11, 5)]}
        self.card_ids = tuple(self.rows)

    def card(self, identifier):
        return self.rows[identifier]


def class_row(identifier, materials=(100, 200, 300)):
    return {"ClassID": identifier, **{f"JobChangeMaterial{i + 1}": value for i, value in enumerate(materials)},
            **{f"_LowRareEvolveMaterial_{i:02}": 0 for i in range(1, 4)}}


def rarity_row(identifier, *, group=None, material_rarity=2, level=30):
    return {"ConfigID": identifier, "ConfigGroupID": identifier if group is None else group,
            "HighRarity": 0, "RarityGroupID": 777,
            "RequireFairyCardIdOfEvolve1": 136, "RequireFairyCardIdOfEvolve2": 397,
            **{f"EvolutionRequire{key}{i:02}": value for i in range(3)
               for key, value in (("Rarity", material_rarity), ("LV", level), ("CC", 0), ("GeneralCardId", 0))}}


def resources():
    cards = Cards()
    classes = {identifier: class_row(identifier) for identifier in (11, 30, 100, 200, 300, 900, 901)}
    rarities = {identifier: rarity_row(identifier) for identifier in (2, 3, 4, 6)}
    rarities[1004] = rarity_row(1004, group=4, material_rarity=3, level=0)
    return cards, classes, rarities


def unit(data, card_id, *, current=None, level=1, stage=0):
    cards, classes, rarities = data
    card = cards.card(card_id)
    return SimpleNamespace(card=card, class_row=classes[card["InitClassID"] if current is None else current],
                           rarity=rarities[card["Rare"] + 1], level=level,
                           unit={"ClassChangeCount": stage})


def recipe(data, *, second=False):
    return [unit(data, 2, level=30), unit(data, 3), unit(data, 4, level=30), unit(data, 397 if second else 136)]


def test_native_slots_join_config_group_and_keep_zero_stage_as_wildcard():
    data = resources()
    subject = EvolutionMaterials(*data)
    slots = subject.slots(unit(data, 1), 7)
    assert isinstance(slots, tuple) and all(isinstance(slot, tuple) for slot in slots)
    assert slots == tuple((MaterialCondition(class_type=i, rarity=2, min_level=30),
                           MaterialCondition(class_type=i, rarity=3)) for i in (1, 2, 3)) + (
                           (MaterialCondition(class_id=11, rarity=5),),)
    with pytest.raises(FrozenInstanceError):
        slots[0][0].stage = 1


def test_same_config_group_is_used_instead_of_numeric_offset_or_rarity_group():
    data = resources()
    data[2][55] = data[2].pop(1004)
    data[2][55]["ConfigID"] = 55
    data[2][1004] = rarity_row(1004, group=66, material_rarity=1, level=99)
    assert EvolutionMaterials(*data).slots(unit(data, 1), 7)[0] == (
        MaterialCondition(class_type=1, rarity=2, min_level=30), MaterialCondition(class_type=1, rarity=3))


@pytest.mark.parametrize("operation", [7, 9])
def test_mixed_silver_gold_and_operation_specific_fairy_pass(operation):
    data = resources()
    materials = recipe(data, second=operation == 9)
    assert EvolutionMaterials(*data).validate(unit(data, 1), materials, operation) == (0, 1, 2, 3)


def test_fairy_matches_current_class_and_rarity_without_exact_card_id():
    data = resources()
    materials = recipe(data)
    materials[3] = unit(data, 9136)
    assert EvolutionMaterials(*data).validate(unit(data, 1), materials, 7) == (0, 1, 2, 3)
    materials[3].class_row = data[1][30]
    with pytest.raises(ProtocolError) as error:
        EvolutionMaterials(*data).validate(unit(data, 1), materials, 7)
    assert error.value.status == 409


@pytest.mark.parametrize("silver_level,accepted", [(29, False), (30, True), (31, True)])
def test_silver_minimum_level_is_inclusive(silver_level, accepted):
    data = resources()
    materials = recipe(data)
    materials[0].level = silver_level
    if accepted:
        assert EvolutionMaterials(*data).validate(unit(data, 1), materials, 7) == (0, 1, 2, 3)
    else:
        with pytest.raises(ProtocolError) as error:
            EvolutionMaterials(*data).validate(unit(data, 1), materials, 7)
        assert error.value.code == "evolution_materials_mismatch" and error.value.status == 409


def test_gold_level_and_other_growth_values_are_not_automatic_selection_restrictions():
    data = resources()
    materials = recipe(data)
    materials[1].level = 90
    materials[1].unit.update(Love=150, SkillLevel=5, CostModMixValue=-3)
    assert EvolutionMaterials(*data).validate(unit(data, 1), materials, 7) == (0, 1, 2, 3)


def test_current_target_class_low_rarity_override_and_zero_fallback():
    data = resources()
    data[1][900] = class_row(900, (0, 200, 300))
    data[1][900]["_LowRareEvolveMaterial_02"] = 100
    subject = EvolutionMaterials(*data)
    target = unit(data, 1, current=900)
    assert [slot[0].class_type for slot in subject.slots(target, 7)[:3]] == [9, 1, 3]
    data[2][4]["HighRarity"] = 1
    assert [slot[0].class_type for slot in EvolutionMaterials(*data).slots(target, 7)[:3]] == [9, 2, 3]


def test_positive_stage_is_equality_and_zero_is_not_stage_zero():
    data = resources()
    materials = recipe(data)
    materials[0].unit["ClassChangeCount"] = 2
    assert EvolutionMaterials(*data).validate(unit(data, 1), materials, 7) == (0, 1, 2, 3)
    data[2][4]["EvolutionRequireCC00"] = 1
    with pytest.raises(ProtocolError):
        EvolutionMaterials(*data).validate(unit(data, 1), materials, 7)
    materials[0].unit["ClassChangeCount"] = 1
    assert EvolutionMaterials(*data).validate(unit(data, 1), materials, 7) == (0, 1, 2, 3)


def test_general_card_is_an_or_shortcut_and_mismatch_continues_other_conditions():
    data = resources()
    data[2][4].update(EvolutionRequireGeneralCardId00=9, EvolutionRequireCC00=4, EvolutionRequireLV00=99)
    materials = recipe(data)
    materials[0] = unit(data, 9)
    assert EvolutionMaterials(*data).validate(unit(data, 1), materials, 7) == (0, 1, 2, 3)
    materials[0] = unit(data, 2, level=99, stage=4)
    assert EvolutionMaterials(*data).validate(unit(data, 1), materials, 7) == (0, 1, 2, 3)


def test_negative_native_conditions_are_omitted_and_division_truncates_towards_zero():
    data = resources()
    data[1][100]["JobChangeMaterial1"] = -1
    data[2][4].update(EvolutionRequireRarity00=-1, EvolutionRequireLV00=-1,
                      EvolutionRequireCC00=-1, EvolutionRequireGeneralCardId00=-1)
    assert EvolutionMaterials(*data).slots(unit(data, 1), 7)[0][0] == MaterialCondition(class_type=0)
    data[1][100]["JobChangeMaterial1"] = -100
    assert EvolutionMaterials(*data).slots(unit(data, 1), 7)[0][0] == MaterialCondition()


def test_complete_assignment_backtracks_instead_of_greedily_consuming_a_unique_match():
    data = resources()
    data[2][4]["EvolutionRequireGeneralCardId00"] = 7
    materials = [unit(data, 7, level=30), unit(data, 2, level=30), unit(data, 4, level=30), unit(data, 136)]
    assert EvolutionMaterials(*data).validate(unit(data, 1), materials, 7) == (1, 0, 2, 3)


def test_assignment_uses_each_input_position_once_and_accepts_same_card_instances():
    data = resources()
    data[1][100] = class_row(100, (100, 100, 100))
    materials = [unit(data, 2, level=30) for _ in range(3)] + [unit(data, 136)]
    assert EvolutionMaterials(*data).validate(unit(data, 1), materials, 7) == (0, 1, 2, 3)
    materials[1] = unit(data, 3)
    with pytest.raises(ProtocolError, match="every"):
        EvolutionMaterials(*data).validate(unit(data, 1), materials, 7)


def test_warehouse_level_one_does_not_acquire_a_silver_level_override():
    data = resources()
    materials = recipe(data)
    materials[0] = unit(data, 2)
    with pytest.raises(ProtocolError):
        EvolutionMaterials(*data).validate(unit(data, 1), materials, 7)
    materials[0] = unit(data, 5)
    assert EvolutionMaterials(*data).validate(unit(data, 1), materials, 7) == (0, 1, 2, 3)


def test_resources_results_and_inputs_are_independent_and_unmodified():
    data = resources()
    subject = EvolutionMaterials(*data)
    target, materials = unit(data, 1), recipe(data)
    before = deepcopy((target, materials))
    assert subject.validate(target, materials, 7) == (0, 1, 2, 3)
    assert (target, materials) == before
    old = subject.slots(target, 7)
    data[1][100]["JobChangeMaterial1"] = 900
    data[2][4]["EvolutionRequireLV00"] = 99
    data[0].rows[136]["InitClassID"] = 30
    assert subject.slots(target, 7) == old


@pytest.mark.parametrize("operation", [None, True, False, 6, 8, 10, "7", 7.0])
def test_unknown_or_mistyped_operation_is_invalid(operation):
    data = resources()
    with pytest.raises(ProtocolError) as error:
        EvolutionMaterials(*data).slots(unit(data, 1), operation)
    assert error.value.code == "evolution_materials_invalid" and error.value.status == 503


@pytest.mark.parametrize("count", [0, 1, 3, 5, 100])
def test_wrong_number_of_materials_is_a_recipe_mismatch(count):
    data = resources()
    with pytest.raises(ProtocolError) as error:
        EvolutionMaterials(*data).validate(unit(data, 1), [unit(data, 2)] * count, 7)
    assert error.value.status == 409


@pytest.mark.parametrize("materials", [None, "abcd", b"abcd", bytearray(4), {}, 4, iter(())])
def test_material_input_requires_a_sequence(materials):
    data = resources()
    with pytest.raises(ProtocolError) as error:
        EvolutionMaterials(*data).validate(unit(data, 1), materials, 7)
    assert error.value.status == 503


@pytest.mark.parametrize("field,value", [("level", True), ("level", 0), ("level", -1), ("level", 0x80000000),
                                         ("level", "30"), ("unit", {}), ("class_row", {}), ("rarity", {}), ("card", {})])
def test_malformed_material_is_an_input_error_without_changes(field, value):
    data = resources()
    materials = recipe(data)
    setattr(materials[0], field, value)
    before = deepcopy(materials)
    with pytest.raises(ProtocolError) as error:
        EvolutionMaterials(*data).validate(unit(data, 1), materials, 7)
    assert error.value.code == "evolution_materials_invalid" and error.value.status == 503
    assert materials == before


@pytest.mark.parametrize("field,value", [("ClassChangeCount", True), ("ClassChangeCount", -1),
                                         ("ClassChangeCount", 0x80000000)])
def test_malformed_stage_is_rejected(field, value):
    data = resources()
    materials = recipe(data)
    materials[0].unit[field] = value
    with pytest.raises(ProtocolError) as error:
        EvolutionMaterials(*data).validate(unit(data, 1), materials, 7)
    assert error.value.status == 503


@pytest.mark.parametrize("field,value", [("EvolutionRequireLV00", True), ("EvolutionRequireCC01", "0"),
    ("EvolutionRequireRarity02", 0x80000000), ("EvolutionRequireGeneralCardId00", 9999),
    ("RequireFairyCardIdOfEvolve1", 9999), ("ConfigID", 9999), ("ConfigGroupID", -1)])
def test_invalid_rarity_resources_are_not_silently_defaulted(field, value):
    data = resources()
    data[2][4][field] = value
    with pytest.raises(ProtocolError) as error:
        EvolutionMaterials(*data)
    assert error.value.code == "evolution_materials_invalid" and error.value.status == 503


@pytest.mark.parametrize("field,value", [("ClassID", 9999), ("JobChangeMaterial1", True),
    ("_LowRareEvolveMaterial_02", None), ("JobChangeMaterial3", -0x80000001)])
def test_invalid_class_resources_are_rejected(field, value):
    data = resources()
    data[1][100][field] = value
    with pytest.raises(ProtocolError) as error:
        EvolutionMaterials(*data)
    assert error.value.status == 503


def test_missing_metadata_unknown_units_and_inconsistent_rarity_are_invalid():
    data = resources()
    subject = EvolutionMaterials(*data)
    target = unit(data, 1)
    target.rarity = data[2][3]
    with pytest.raises(ProtocolError):
        subject.slots(target, 7)
    with pytest.raises(ProtocolError):
        subject.slots(None, 7)
    target = unit(data, 1)
    target.card = {**target.card, "CardID": 99999}
    with pytest.raises(ProtocolError):
        subject.slots(target, 7)
    del data[2][4]["EvolutionRequireLV00"]
    with pytest.raises(ProtocolError):
        EvolutionMaterials(*data)


@pytest.mark.parametrize("value", [True, -1, 0x80000000, "1", 1.0])
def test_public_condition_rejects_invalid_fields(value):
    with pytest.raises(ProtocolError) as error:
        MaterialCondition(min_level=value)
    assert error.value.status == 503


def test_the_invalid_materials_code_is_spelled_once_where_it_is_published():
    assert EVOLUTION_MATERIALS_INVALID == "evolution_materials_invalid"
    published = Path(module.__file__).resolve()
    spelled = [path.name for path in sorted(published.parent.glob("*.py"))
               for node in ast.walk(ast.parse(path.read_bytes()))
               if isinstance(node, ast.Constant) and node.value == EVOLUTION_MATERIALS_INVALID]
    assert spelled == [published.name]


@pytest.fixture(scope="module")
def native_resources():
    project = Path(__file__).resolve().parents[1]
    if not (project / "runtime/server/masterdata/active.json").is_file():
        pytest.skip("Verified local resources and public masters are required")
    snapshot = MasterDataStore(project / "runtime/server/masterdata").snapshot()
    manifest, entries = ResourceLibrary(project / "runtime/resources", client_hash=snapshot.client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(project.parent / "aigisr_exe", asset_reader=reader)
    cards = GiftCatalog(snapshot, reader, possessions, resource_version=manifest["version"])

    def unpack(raw):
        return decompress_all4(raw) if raw.startswith(b"ALL4") else raw

    raw = unpack(reader("PlayerUnitTable.aar"))
    member = next(member for member in parse_archive(raw).members if member.name == "ClassData.atb")
    classes = {row["ClassID"]: row for row in parse_table(unpack(raw[member.offset:member.offset + member.size]))["rows"]}
    rarities = {row["ConfigID"]: row for row in parse_table(unpack(reader("cfgRarity.atb")))["rows"]}
    return cards, classes, rarities


def test_actual_resources_build_all_normal_awakening_material_recipes(native_resources):
    cards, classes, rarities = native_resources
    subject = EvolutionMaterials(*native_resources)
    counts = {7: 0, 9: 0}
    for card_id in cards.card_ids:
        card = cards.card(card_id)
        cfg = rarities[card["Rare"] + 1]
        cls = classes[card["InitClassID"]]
        if not cfg["EvolutionConfig"] or cls["JobChange"] not in classes:
            continue
        evolved = classes[cls["JobChange"]]
        operations = [(7, cls)]
        if card["_AwakePattern"] and (evolved["AwakeType1"] or evolved["AwakeType2"]):
            operations.append((9, evolved))
        for operation, current in operations:
            target = SimpleNamespace(card=card, class_row=current, rarity=cfg)
            slots = subject.slots(target, operation)
            assert len(slots) == 4
            for i, slot in enumerate(slots[:3]):
                cls_type = (current[f"JobChangeMaterial{i + 1}"] or current["ClassID"]) // 100
                assert slot == (MaterialCondition(class_type=cls_type, rarity=2, min_level=30),
                                MaterialCondition(class_type=cls_type, rarity=3))
            fairy = cards.card(cfg[f"RequireFairyCardIdOfEvolve{1 if operation == 7 else 2}"])
            assert slots[3] == (MaterialCondition(class_id=fairy["InitClassID"], rarity=fairy["Rare"]),)
            counts[operation] += 1
    assert counts == {7: 1468, 9: 1198}


def test_actual_card_recipe_validates_in_reverse_wire_order(native_resources):
    cards, classes, rarities = native_resources
    subject = EvolutionMaterials(*native_resources)
    for identifier in cards.card_ids:
        target = unit(native_resources, identifier)
        if not target.rarity["EvolutionConfig"] or target.class_row["JobChange"] not in classes:
            continue
        slots = subject.slots(target, 7)
        selected = []
        for slot in slots[:3]:
            possible = [card_id for card_id in cards.card_ids if cards.card(card_id)["Rare"] == 3
                        and cards.card(card_id)["InitClassID"] // 100 == slot[0].class_type]
            if not possible:
                break
            selected.append(unit(native_resources, possible[0]))
        if len(selected) != 3:
            continue
        selected.append(unit(native_resources, target.rarity["RequireFairyCardIdOfEvolve1"]))
        materials = list(reversed(selected))
        assignment = subject.validate(target, materials, 7)
        assert sorted(assignment) == [0, 1, 2, 3] and assignment[3] == 0
        return
    pytest.fail("No complete actual awakening recipe was found")

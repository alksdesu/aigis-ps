from dataclasses import FrozenInstanceError
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import lz4.frame
import pytest

from aigis_ps.al_format import decompress_all4, parse_archive, parse_table
from aigis_ps.alxml import loads
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.public_tables import MASTER_LIMITS
from aigis_ps.resource_view import load_resource_view
from aigis_ps.unit_initialization import CARD_FIELDS, NativeUnitInitializer, UnitInitialValues
from test_al_format import schema_bytes


HERO_KEY = (zlib.crc32("英傑".encode()) ^ 0x80000000) - 0x80000000


def blood_bytes(rows=None):
    rows = rows if rows is not None else [(123, 0, 0), (HERO_KEY, 3, 4590)]
    fields = [(name, index * 4, 1, b"\0" * 8)
              for index, name in enumerate(("_ResID", "_TypeID", "_SystemTextID"))]
    schema = schema_bytes(fields, 12)
    return (struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(rows), 20, 20 + len(schema), 12, b"irtd")
            + schema + b"".join(struct.pack("<iii", *row) for row in rows))


@pytest.fixture
def source():
    cards = {identifier: {"CardID": identifier, "InitClassID": 100, "ClassLV0SkillID": 9,
                         "ClassLV1SkillID": 12, "Rare": 3, "Blood": 0,
                         "ExcludeFameDisplayFirstClass": 0} for identifier in (2, 3, 4, 80)}
    cards[3]["Blood"] = 3
    cards[4]["ExcludeFameDisplayFirstClass"] = 1
    return SimpleNamespace(cards=cards,
                           classes={100: {"ClassID": 100, "MaxLevel": 3, "JobChange": 200},
                                    200: {"ClassID": 200, "MaxLevel": 5, "JobChange": 0}},
                           experience=[0, 15, 17, 100, 200],
                           rarities={4: {"ConfigID": 4, "RequireExpRatio": 130,
                                         "LevelCap00": 3, "LevelCap02": 4, "EvolutionConfig": 1}},
                           blood=blood_bytes())


def initializer(source):
    return NativeUnitInitializer(source.cards, source.classes, source.experience, source.rarities, source.blood)


def test_default_forced_and_explicit_stages_use_original_skill(source):
    value = initializer(source)
    assert value.values(2, 0) == UnitInitialValues(100, 0, 9, 0)
    assert value.values(2, 1, evolution_count=2) == UnitInitialValues(200, 2, 9, 0)
    assert value.values(3, 1) == UnitInitialValues(200, 2, 9, 0)
    assert value.values(4, 1) == UnitInitialValues(200, 2, 9, 0)


def test_levels_use_final_class_rarity_cap_and_native_float32_rounding(source):
    value = initializer(source)
    assert value.values(2, 999).experience == 22
    assert value.values(3, 999).experience == 130
    assert value.values(3, 2).experience == 20
    source.rarities[4]["LevelCap02"] = 5
    assert initializer(source).values(3, 999).experience == 260


@pytest.mark.parametrize("disabled", ["JobChange", "EvolutionConfig"])
def test_native_awakening_gate_can_keep_forced_unit_at_initial_stage(source, disabled):
    if disabled == "JobChange":
        source.classes[100][disabled] = 0
    else:
        source.rarities[4][disabled] = 0
    assert initializer(source).values(3, 999) == UnitInitialValues(100, 0, 9, 22)


@pytest.mark.parametrize("flag,stage", [(0, 0), (1, 2), (-1, 2), (256, 0), (257, 2)])
def test_excluded_initial_class_uses_native_low_byte(source, flag, stage):
    source.cards[2]["ExcludeFameDisplayFirstClass"] = flag
    assert initializer(source).values(2, 1).stage == stage


def test_hero_type_is_resource_defined_and_unknown_types_reject(source):
    source.blood = blood_bytes([(123, 0, 0), (HERO_KEY, 7, 4590)])
    source.cards[3]["Blood"] = 7
    assert initializer(source).values(3, 1).stage == 2
    source.cards[3]["Blood"] = 3
    with pytest.raises(ProtocolError, match="Selected unit") as error:
        initializer(source).values(3, 1)
    assert error.value.code == "unit_initialization_reference"


def test_inputs_and_results_are_isolated_and_immutable(source):
    value = initializer(source)
    version = value.content_version
    source.cards[2]["ClassLV0SkillID"] = 99
    source.classes[100]["MaxLevel"] = 1
    source.rarities[4]["RequireExpRatio"] = 200
    source.experience[2] = 150
    result = value.values(2, 999)
    assert result == UnitInitialValues(100, 0, 9, 22)
    assert value.content_version == version
    with pytest.raises(FrozenInstanceError):
        result.stage = 2
    with pytest.raises(TypeError):
        value._cards[2]["Blood"] = 3


@pytest.mark.parametrize("changed", ["cards", "classes", "rarities", "experience", "blood"])
def test_content_version_tracks_all_relevant_inputs(source, changed):
    original = initializer(source).content_version
    if changed == "cards":
        source.cards[2]["Blood"] = 3
    elif changed == "classes":
        source.classes[100]["MaxLevel"] = 2
    elif changed == "rarities":
        source.rarities[4]["RequireExpRatio"] = 140
    elif changed == "experience":
        source.experience[2] = 18
    else:
        source.blood = blood_bytes([(123, 0, 1), (HERO_KEY, 3, 4590)])
    assert initializer(source).content_version != original


def test_unreferenced_incomplete_rows_do_not_block_valid_unit(source):
    source.cards[99] = {"CardID": 99, "InitClassID": 999, "Rare": 1000}
    source.classes[999] = {}
    source.rarities[1001] = {}
    value = initializer(source)
    assert value.values(2, 1).class_id == 100
    with pytest.raises(ProtocolError) as error:
        value.values(99, 1)
    assert error.value.code == "unit_initialization_reference"


@pytest.mark.parametrize("field,invalid", [
    ("card_id", True), ("card_id", 0), ("card_id", -1), ("card_id", 0x80000000),
    ("level", True), ("level", -1), ("level", 1.0), ("level", 0x80000000),
    ("evolution_count", False), ("evolution_count", -1),
])
def test_invalid_call_arguments(source, field, invalid):
    args = {"card_id": 2, "level": 1, "evolution_count": 0}
    args[field] = invalid
    with pytest.raises(ProtocolError) as error:
        initializer(source).values(**args)
    assert error.value.code == "unit_initialization_invalid"


@pytest.mark.parametrize("stage", [1, 3, 4, 99])
def test_unknown_requested_stages_are_not_silently_clamped(source, stage):
    with pytest.raises(ProtocolError) as error:
        initializer(source).values(2, 1, evolution_count=stage)
    assert error.value.code == "unit_initialization_unsupported" and error.value.status == 501


def classic_route(source, job_change=730):
    source.classes[710] = {"ClassID": 710, "MaxLevel": 4, "JobChange": job_change}
    source.classes[730] = {"ClassID": 730, "MaxLevel": 2, "JobChange": 0}
    return source


def test_card80_awakens_from_its_hardcoded_classic_class(source):
    source.classes[100]["JobChange"] = 0
    value = initializer(classic_route(source))
    assert value.values(80, 1) == UnitInitialValues(100, 0, 9, 0)
    assert value.values(80, 1, evolution_count=2) == UnitInitialValues(730, 2, 9, 0)
    assert value.values(80, 999, evolution_count=2) == UnitInitialValues(730, 2, 9, 20)
    assert value.values(2, 999, evolution_count=2) == UnitInitialValues(100, 0, 9, 22)


@pytest.mark.parametrize("disabled", ["JobChange", "EvolutionConfig"])
def test_card80_classic_gate_keeps_its_initial_class(source, disabled):
    classic_route(source, job_change=0 if disabled == "JobChange" else 730)
    if disabled == "EvolutionConfig":
        source.rarities[4]["EvolutionConfig"] = 0
    assert initializer(source).values(80, 999, evolution_count=2) == UnitInitialValues(100, 0, 9, 22)


@pytest.mark.parametrize("changed", ["route_missing", "route_key", "result_missing"])
def test_card80_classic_route_references_are_explicit(source, changed):
    classic_route(source)
    if changed == "route_missing":
        del source.classes[710]
    elif changed == "route_key":
        source.classes[710]["ClassID"] = 730
    else:
        del source.classes[730]
    value = initializer(source)
    assert value.values(80, 1).class_id == 100
    with pytest.raises(ProtocolError) as error:
        value.values(80, 1, evolution_count=2)
    assert error.value.code == "unit_initialization_reference"


@pytest.mark.parametrize("card_id", [1, 309])
def test_prince_initialization_requires_rank_aware_caller(source, card_id):
    source.cards[card_id] = {**source.cards[2], "CardID": card_id}
    with pytest.raises(ProtocolError) as error:
        initializer(source).values(card_id, 1)
    assert error.value.code == "unit_initialization_unsupported" and error.value.status == 501


@pytest.mark.parametrize("changed", ["missing_card", "card_field", "card_key", "class", "class_key", "rarity",
                                     "rarity_key", "job_change", "ratio", "cap", "cap_outside_table"])
def test_selected_missing_or_invalid_references_are_explicit(source, changed):
    if changed == "missing_card":
        del source.cards[3]
    elif changed == "card_field":
        del source.cards[3]["Blood"]
    elif changed == "card_key":
        source.cards[3]["CardID"] = 4
    elif changed == "class":
        del source.classes[100]
    elif changed == "class_key":
        source.classes[100]["ClassID"] = 200
    elif changed == "rarity":
        source.rarities.clear()
    elif changed == "rarity_key":
        source.rarities[4]["ConfigID"] = 5
    elif changed == "job_change":
        source.classes[100]["JobChange"] = 999
    elif changed == "ratio":
        source.rarities[4]["RequireExpRatio"] = 0
    elif changed == "cap":
        source.rarities[4]["LevelCap02"] = 0
    else:
        source.classes[200]["MaxLevel"] = source.rarities[4]["LevelCap02"] = 6
    with pytest.raises(ProtocolError) as error:
        initializer(source).values(3, 1)
    assert error.value.code == "unit_initialization_reference"


def test_experience_overflow_is_not_wrapped_or_truncated(source):
    source.experience = [0, 100, 200, 0x7FFFFFFF]
    with pytest.raises(ProtocolError) as error:
        initializer(source).values(3, 4)
    assert error.value.code == "unit_initialization_overflow"


@pytest.mark.parametrize("experience", [[], [1], [0, 0], [0, -1], [0, True], [0, 1.0], [0, 0x80000000]])
def test_invalid_experience_table_rejects_at_construction(source, experience):
    source.experience = experience
    with pytest.raises(ProtocolError) as error:
        initializer(source)
    assert error.value.code == "unit_initialization_catalog"


@pytest.mark.parametrize("kind", ["empty", "truncated", "oversized", "nonbytes", "missing_hero", "duplicate_key", "duplicate_type"])
def test_invalid_blood_definitions_are_not_defaulted(source, kind):
    choices = {"empty": b"", "truncated": blood_bytes()[:-1], "oversized": b"x" * (1024 * 1024 + 1),
               "nonbytes": "ALTB", "missing_hero": blood_bytes([(123, 0, 0)]),
               "duplicate_key": blood_bytes([(HERO_KEY, 0, 0), (HERO_KEY, 3, 0)]),
               "duplicate_type": blood_bytes([(123, 3, 0), (HERO_KEY, 3, 0)])}
    source.blood = choices[kind]
    with pytest.raises(ProtocolError) as error:
        initializer(source)
    assert error.value.code == "unit_initialization_catalog"


def test_all4_blood_resource_uses_same_rules(source):
    plain = initializer(source).values(3, 4)
    source.blood = struct.pack("<4sHHI", b"ALL4", 1, 0, len(source.blood)) + lz4.frame.compress(source.blood)
    assert initializer(source).values(3, 4) == plain


@pytest.fixture(scope="module")
def current():
    root = Path(__file__).resolve().parents[1]
    if not (root / "runtime/server/masterdata/active.json").is_file():
        pytest.skip("Verified current resources are required")
    snapshot = MasterDataStore(root / "runtime/server/masterdata").snapshot()
    view = load_resource_view(root / "runtime/resources", root.parent / "aigisr_exe", snapshot.client_hash,
                              master_provider=snapshot.get_xml)
    def read(name):
        raw = view.assets.read(name)[0]
        return decompress_all4(raw) if raw.startswith(b"ALL4") else raw
    columns = {column.name: column.values for column in loads(snapshot.get_xml("CardData"), limits=MASTER_LIMITS).children}
    cards = {row[0]: dict(zip(CARD_FIELDS, row)) for row in zip(*(columns[name] for name in CARD_FIELDS))}
    archive = read("PlayerUnitTable.aar")
    member = next(member for member in parse_archive(archive).members if member.name == "ClassData.atb")
    payload = archive[member.offset:member.offset + member.size]
    if payload.startswith(b"ALL4"):
        payload = decompress_all4(payload)
    classes = {row["ClassID"]: row for row in parse_table(payload)["rows"]}
    experience = [row["Exp"] for row in parse_table(read("ExperienceTable.atb"))["rows"]]
    rarities = {row["ConfigID"]: row for row in parse_table(read("cfgRarity.atb"))["rows"]}
    return SimpleNamespace(cards=cards, classes=classes, experience=experience, rarities=rarities,
                           blood=read("PlayerBloodType.atb"))


def test_all_2932_current_cards_and_112_hero_references(current):
    value = initializer(current)
    assert len(current.cards) == 2932
    heroes = 0
    for card_id, card in current.cards.items():
        if card_id in (1, 309):
            with pytest.raises(ProtocolError) as error:
                value.values(card_id, 0)
            assert error.value.code == "unit_initialization_unsupported"
            continue
        result = value.values(card_id, 0)
        hero = card["Blood"] == 3
        eligible = current.classes[card["InitClassID"]]["JobChange"] and current.rarities[card["Rare"] + 1]["EvolutionConfig"]
        expected = 2 if hero and eligible else 0
        assert result.stage == expected, card_id
        assert result.class_id == (current.classes[card["InitClassID"]]["JobChange"] if expected else card["InitClassID"]), card_id
        assert result.skill_id == card["ClassLV0SkillID"] and result.experience == 0, card_id
        assert value.values(card_id, 0x7FFFFFFF).experience >= 0, card_id
        heroes += hero
    assert heroes == 112


def test_current_explicit_awakened_shop_products_keep_verified_fields(current):
    value = initializer(current)
    assert value.values(2079, 1, evolution_count=2) == UnitInitialValues(141420, 2, 2800, 0)
    assert value.values(1989, 1, evolution_count=2) == UnitInitialValues(150920, 2, 2703, 0)


def test_current_card80_awakened_grant_follows_class710_to_class730(current):
    value = initializer(current)
    assert current.classes[current.cards[80]["InitClassID"]]["JobChange"] == 710
    assert value.values(80, 1) == UnitInitialValues(700, 0, 47, 0)
    assert value.values(80, 1, evolution_count=2) == UnitInitialValues(730, 2, 47, 0)
    assert value.values(99, 1, evolution_count=2) == UnitInitialValues(730, 2, 72, 0)
    for level in (30, 80, 99, 0x7FFFFFFF):
        experience = value.values(80, level, evolution_count=2).experience
        assert experience == value.values(99, level, evolution_count=2).experience > 0

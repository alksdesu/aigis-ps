from copy import deepcopy
import struct
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.al_format import parse_table
from aigis_ps.alxml import loads
from aigis_ps.errors import ProtocolError
from aigis_ps.presents import PresentService, TEMPLATE_CARDS, TEMPLATE_INITIAL
from aigis_ps.storage import Store
from test_player_state import schema_bytes
from test_presents import claim_request, queue, unit
from test_quest_service import bundle


NAMES = ("PresentID", "CardID", *TEMPLATE_INITIAL)


def row(identifier, card_id, **changes):
    return {"PresentID": identifier, "CardID": card_id, **TEMPLATE_INITIAL, **changes}


def rows(*additional):
    return [*(row(identifier, card_id) for identifier, card_id in TEMPLATE_CARDS.items()), *additional]


def table(source_rows):
    schema = schema_bytes([(name, index * 4, 5 if index == 8 else 1, b"\xff" if index == 8 else b"\0" * 8)
                           for index, name in enumerate(NAMES)], 40)
    data = b"".join(struct.pack("<8i?3xi", *(item[name] for name in NAMES)) for item in source_rows)
    return struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(source_rows), 20, 20 + len(schema), 40, b"pres") + schema + data


def service(tmp_path, *additional, lookup=None):
    players = SimpleNamespace(cards=lookup or (lambda: {17: {"CardID": 17}, 18: {"CardID": 18}}))
    return PresentService(SimpleNamespace(asset_root=tmp_path), players,
                          asset_reader=lambda _: table(rows(*additional)))


def test_initial_lookup_selects_smallest_supported_real_template_id(tmp_path):
    subject = service(tmp_path, row(300, 17), row(208, 17, InitSkillLevel=0), row(100, 17, InitExp=1), row(400, 18))
    assert subject.initial_unit_template(17) == 208
    assert subject.initial_unit_template(18) == 400
    assert subject.initial_unit_template(56) == 2
    assert subject.status()["normalized_skill_templates"] == [208]
    assert "normalizes InitSkillLevel 0 to SkillLevel 1" in subject.status()["initial_unit_policy"]
    assert subject._templates == {**TEMPLATE_CARDS, 208: 17, 300: 17, 400: 18}
    report = subject.status()
    report["normalized_skill_templates"].clear()
    assert subject.status()["normalized_skill_templates"] == [208]


@pytest.mark.parametrize("field,value", [
    ("InitExp", 1), ("InitExp", -1), ("InitSkillID", 0), ("InitSkillID", 123),
    ("InitSkillLevel", -1), ("InitSkillLevel", 2), ("CostMod", -1), ("CostMod", 1),
    ("InitLove", 1), ("EvolutionCount", 1), ("ClassChangeEnable", True), ("ClassChangeStatusMod", 1),
])
def test_complex_template_is_skipped_without_weakening_original_four(tmp_path, field, value):
    subject = service(tmp_path, row(208, 17, **{field: value}))
    assert subject.status()["ready"] is True and subject._templates == TEMPLATE_CARDS
    with pytest.raises(ProtocolError) as error:
        subject.initial_unit_template(17)
    assert error.value.code == "present_template_unsupported" and error.value.status == 501


@pytest.mark.parametrize("identifier,card_id", [(0, 17), (208, 1), (208, 0), (208, -1), (208, 999999)])
def test_zero_template_default_unit_and_unknown_card_are_not_supported(tmp_path, identifier, card_id):
    subject = service(tmp_path, row(identifier, card_id, InitSkillLevel=0))
    assert subject.status()["ready"] is True and subject._templates == TEMPLATE_CARDS


@pytest.mark.parametrize("identifier", [17, 1, 0, -1, True, False, "56", 56.0, None, 0x80000000])
def test_lookup_does_not_invent_an_id_for_missing_or_invalid_card(tmp_path, identifier):
    with pytest.raises(ProtocolError) as error:
        service(tmp_path).initial_unit_template(identifier)
    assert error.value.code == "present_template_unsupported"


@pytest.mark.parametrize("failure", ["raise", "none", "list"])
def test_unavailable_public_master_keeps_original_four_readable(tmp_path, failure):
    def lookup():
        if failure == "raise":
            raise ProtocolError("player_master_unavailable", "test unavailable", 503)
        return None if failure == "none" else []
    subject = service(tmp_path, row(208, 17, InitSkillLevel=0), lookup=lookup)
    assert subject.status()["ready"] is True
    assert subject.status()["initial_template_lookup_unavailable"] is True
    assert subject.initial_unit_template(56) == 2
    assert subject.rows({"bootstrap_schema": 1, "tables": {"presents": []}}) == []
    with pytest.raises(ProtocolError) as error:
        subject.initial_unit_template(17)
    assert error.value.code == "present_template_unsupported"


@pytest.mark.parametrize("players,unavailable", [
    (SimpleNamespace(), True),
    (SimpleNamespace(cards=lambda: MappingProxyType({17: {"CardID": 17}})), False),
], ids=["without_card_lookup", "read_only_lookup"])
def test_initial_templates_use_the_public_read_only_card_lookup(tmp_path, players, unavailable):
    subject = PresentService(SimpleNamespace(asset_root=tmp_path), players,
                             asset_reader=lambda _: table(rows(row(208, 17, InitSkillLevel=0))))
    assert subject.status()["ready"] is True
    assert subject.status()["initial_template_lookup_unavailable"] is unavailable
    assert dict(subject.templates) == (TEMPLATE_CARDS if unavailable else {**TEMPLATE_CARDS, 208: 17})


def test_master_is_not_required_when_there_are_no_additional_candidates(tmp_path):
    def lookup():
        pytest.fail("Original four templates must not need a public master lookup")
    subject = service(tmp_path, row(0, 17), row(208, 17, InitExp=99), lookup=lookup)
    assert subject._templates == TEMPLATE_CARDS and subject.status()["ready"]


@pytest.mark.parametrize("field,value", [
    ("CardID", 17.0), ("CardID", True), ("InitExp", False), ("InitSkillID", -1.0),
    ("InitSkillLevel", True), ("InitSkillLevel", False), ("InitSkillLevel", 0.0),
    ("CostMod", False), ("InitLove", False), ("EvolutionCount", False),
    ("ClassChangeEnable", 0), ("ClassChangeStatusMod", False),
])
def test_additional_initial_state_values_have_strict_types(tmp_path, monkeypatch, field, value):
    parsed = parse_table(table(rows(row(208, 17))))
    parsed["rows"][-1][field] = value
    monkeypatch.setattr("aigis_ps.presents.parse_table", lambda *args: parsed)
    subject = service(tmp_path)
    assert subject.status()["ready"] is True and subject._templates == TEMPLATE_CARDS


@pytest.mark.parametrize("field,value", [("InitSkillLevel", 0), ("InitExp", 1), ("ClassChangeEnable", True)])
def test_original_fairy_templates_remain_strict(tmp_path, field, value):
    original = rows()
    original[0][field] = value
    subject = PresentService(SimpleNamespace(asset_root=tmp_path), SimpleNamespace(cards=lambda: {}),
                             asset_reader=lambda _: table(original))
    assert subject.status()["ready"] is False
    with pytest.raises(ProtocolError) as error:
        subject.initial_unit_template(56)
    assert error.value.code == "present_templates_unavailable"


def test_duplicate_id_is_rejected_even_when_both_rows_would_be_unsupported(tmp_path):
    subject = service(tmp_path, row(208, 99999, InitExp=1), row(208, 99998, InitExp=2))
    assert subject.status()["ready"] is False
    assert subject._templates == {}


def test_constructor_does_not_mutate_parsed_template_rows(tmp_path, monkeypatch):
    parsed = parse_table(table(rows(row(208, 17, InitSkillLevel=0))))
    before = deepcopy(parsed)
    monkeypatch.setattr("aigis_ps.presents.parse_table", lambda *args: parsed)
    subject = service(tmp_path)
    assert subject.initial_unit_template(17) == 208 and parsed == before


def test_real_card17_has_supported_template208_with_native_skill_normalization(bundle):
    raw = bundle.player_state.read_asset("PresentUnitData.atb")
    template = next(item for item in parse_table(raw)["rows"] if item["PresentID"] == 208)
    assert template["CardID"] == 17 and template["InitSkillLevel"] == 0
    subject = PresentService(bundle.bootstrap, bundle.player_state, asset_reader=bundle.player_state.read_asset)
    assert subject.initial_unit_template(17) == 208
    assert 208 in subject.status()["normalized_skill_templates"]
    assert all(subject._templates[identifier] == card_id for identifier, card_id in TEMPLATE_CARDS.items())


@pytest.mark.parametrize("operation", [1, 2, 117])
def test_real_initial_template_claim_reopens_and_retries_once(bundle, tmp_path, operation):
    subject = PresentService(bundle.bootstrap, bundle.player_state, asset_reader=bundle.player_state.read_asset)
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    initial["independent_history"] = {"keep": [1, 2]}
    store = Store(tmp_path / "initial-template.sqlite3", initial)
    selected = [unit(subject.initial_unit_template(17))]
    if operation == 117:
        selected.append(unit(2))
    ids = queue(subject, store, selected)
    queued = store.profile()
    envelope = claim_request(store, ids, operation)
    response = subject.receive(envelope, store)
    after = store.profile()
    assert after.revision == queued.revision + 1 and after.state["tables"]["presents"] == []
    granted = after.state["player_state"]["units"][1:]
    assert [item["CardID"] for item in granted] == ([17, 56] if operation == 117 else [17])
    for item in granted:
        assert item["Exp"] == item["Love"] == item["CostModMixValue"] == item["ClassChangeCount"] == 0
        assert item["ClassChangeStatusMod"] == 0 and item["SkillLevel"] == 1
        assert item["SettingSkillID"] == bundle.gifts.catalog.card(item["CardID"])["ClassLV0SkillID"]
    assert after.state["independent_history"] == initial["independent_history"]
    ppu = next(child for child in loads(response).children if child.name == "PPU")
    assert next(column for column in ppu.children if column.name == "A6").values == (1,) * len(granted)
    restarted = PresentService(bundle.bootstrap, bundle.player_state, asset_reader=bundle.player_state.read_asset)
    reopened = Store(store.path)
    assert restarted.receive(envelope, reopened) == response
    assert reopened.profile() == after


def test_unknown_complex_template_is_not_downgraded_to_plain_card_claim(bundle, tmp_path):
    subject = PresentService(bundle.bootstrap, bundle.player_state,
                             asset_reader=lambda _: table(rows(row(208, 17, InitExp=100))))
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    before = deepcopy(initial)
    with pytest.raises(ProtocolError) as error:
        subject.enqueue(initial, [unit(208)])
    assert error.value.code == "present_template_unsupported" and initial == before

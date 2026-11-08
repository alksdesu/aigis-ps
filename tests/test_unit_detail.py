from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import SKILL_SWITCH_FLAG, UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.protocol import ProtocolRegistry, encode_response
from aigis_ps.request_ledger import HISTORY_LIMIT, LEGACY_SCHEMA_VERSION, PROCESSED_LIMIT, SCHEMA_VERSION
from aigis_ps.storage import Store
from aigis_ps.unit_detail import UnitDetailService
from test_quest_service import bundle, request
from test_training_service import unfarmed


class Cards:
    def __init__(self, source):
        self.source, self.overrides = source, {}

    def card(self, card_id):
        return self.overrides.get(card_id, self.source.card(card_id))


class Growth(SimpleNamespace):
    def replayable_content_versions(self):
        return frozenset((self.content_version,))


@pytest.fixture
def case(bundle, tmp_path):
    real = bundle.training.catalog
    growth = Growth(cards=Cards(real.cards), classes=real.classes,
                    skills=real.skills, content_version=real.content_version)
    card = dict(real.cards.card(7))
    card.update(CharaFace0=1, Face1=1, CharaFace3=1, CharaFace4=1, _AwakePattern=3,
                SmartPhoneImageEvolve0=1, SmartPhoneImageEvolve2=1,
                SmartPhoneImageEvolve3=1, SmartPhoneImageEvolve4=1)
    growth.cards.overrides[7] = card
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, added = bundle.player_state.credit_units(state, [{"card_id": 7, "level": 1, "count": 1}])
    unit = state["player_state"]["units"][-1]
    unit.update(ClassChangeCount=2, ClassID=bundle.evolution.catalog.route(card, 2)["ClassID"],
                SettingSkillID=card["ClassLV1SkillID"], SkillEvolved=1, SkillLevel=5,
                SkillExperience=11, CostExperience=7, _DisplayType=0)
    store = Store(tmp_path / "details.sqlite3", state)
    service = UnitDetailService(bundle.bootstrap, bundle.player_state, growth)
    return SimpleNamespace(store=store, service=service, growth=growth, bundle=bundle,
                           index=added[0]["GetIndex"], card=card)


def message(case, *, index=None, display=None, toggle=1, tick=1000):
    index = case.index if index is None else index
    display = target(case)["_DisplayType"] if display is None else display
    return request(case.store, 10, (Container("PU", (Column("A7", "I", (index,)),
        Column("B0", "I", (display,)), Column("SC", "I", (toggle,)))),), tick=tick)


def target(case):
    return next(row for row in case.store.profile().state["player_state"]["units"] if row["GetIndex"] == case.index)


def mutate(case, action):
    profile = case.store.profile()
    candidate = deepcopy(profile.state)
    action(candidate)
    case.store.update_state(candidate, profile.revision)


def change_target(case, **values):
    mutate(case, lambda state: next(row for row in state["player_state"]["units"]
                                    if row["GetIndex"] == case.index).update(values))


def set_stage(case, stage):
    cls = case.bundle.evolution.catalog.route(case.card, stage)["ClassID"]
    change_target(case, ClassChangeCount=stage, ClassID=cls)


def fails(case, envelope, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.save(envelope, case.store)
    assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


def test_handler_status_and_config_validation(case):
    assert set(case.service.handlers()) == {10}
    status = case.service.status()
    assert status["implemented_operations"] == [10] and not status["smartphone_images"]
    assert status["history_limit"] == HISTORY_LIMIT and status["processed_request_limit"] == PROCESSED_LIMIT
    for value in (0, 1, "true", None):
        with pytest.raises(ValueError):
            UnitDetailService(case.bundle.bootstrap, case.bundle.player_state, case.growth, smartphone_images=value)


def test_both_directions_preserve_growth_identity_and_all_unrelated_state(case):
    before = case.store.profile()
    original = target(case)
    first = case.service.save(message(case), case.store)
    awake = target(case)
    assert awake["SettingSkillID"] == case.card["EvoSkillID"]
    assert awake["GeneralPurposeFlags"] == original["GeneralPurposeFlags"] ^ SKILL_SWITCH_FLAG
    expected = {**original, "SettingSkillID": case.card["EvoSkillID"],
                "GeneralPurposeFlags": original["GeneralPurposeFlags"] ^ SKILL_SWITCH_FLAG}
    assert awake == expected
    parsed = {node.name: node for node in loads(first).children}
    assert set(parsed) == {"SYSTEM", "PPU"}
    assert {node.name: node.values for node in parsed["PPU"].children} == {
        alias: (awake[name],) for name, alias in UNIT_FIELDS}
    assert {node.name: node.type.value for node in parsed["PPU"].children} == {
        alias: "B" if name in UNIT_BOOLEANS else "I" for name, alias in UNIT_FIELDS}
    for key, value in before.state.items():
        if key != "player_state":
            assert case.store.profile().state[key] == value
    assert {key: value for key, value in case.store.profile().state["player_state"].items() if key != "units"} == {
        key: value for key, value in before.state["player_state"].items() if key != "units"}
    case.service.save(message(case, tick=1001), case.store)
    assert target(case) == original


def test_locked_organized_extra_barracks_and_signed_flags_are_preserved(case):
    flags = -0x80000000 | 0xF3
    change_target(case, GeneralPurposeFlags=flags, CanFame=True, Famed=True)
    mutate(case, lambda state: state["player_state"]["squads"].append(
        {"Address": 0x10000, "UnitIndex": case.index, "Name": "locked", "Flags": 3}))
    before = case.store.profile()
    case.service.save(message(case), case.store)
    assert target(case)["GeneralPurposeFlags"] == flags ^ SKILL_SWITCH_FLAG
    assert target(case)["CanFame"] and target(case)["Famed"]
    assert case.store.profile().state["player_state"]["squads"] == before.state["player_state"]["squads"]


def test_extra_barracks_target_saves_without_misaddressed_ppu(case):
    change_target(case, GeneralPurposeFlags=0x10)
    envelope = message(case)
    first = case.service.save(envelope, case.store)
    saved = case.store.profile()
    parsed = {node.name: node for node in loads(first).children}
    assert set(parsed) == {"SYSTEM", "PPU"} and all(not column.values for column in parsed["PPU"].children)
    assert target(case)["GeneralPurposeFlags"] == 0x14
    assert case.service.save(envelope, case.store) == first and case.store.profile() == saved


def test_legacy_awake_flag_allows_switch_back_without_inventing_b1(case):
    change_target(case, SkillEvolved=0, GeneralPurposeFlags=5, SettingSkillID=case.card["EvoSkillID"])
    case.service.save(message(case), case.store)
    assert target(case)["GeneralPurposeFlags"] == 1 and target(case)["SkillEvolved"] == 0
    assert target(case)["SettingSkillID"] == case.card["ClassLV1SkillID"]
    fails(case, message(case, tick=1001), "unit_detail_skill_ineligible")


@pytest.mark.parametrize("value", [-1, 2, 0x7FFFFFFF])
def test_any_nonzero_b1_is_preserved(case, value):
    change_target(case, SkillEvolved=value)
    case.service.save(message(case), case.store)
    assert target(case)["SkillEvolved"] == value


@pytest.mark.parametrize("changes", [{"SkillEvolved": 0}, {"ClassChangeCount": 0},
    {"ClassChangeCount": 1}, {"ClassChangeCount": 5}, {"ClassID": 100}])
def test_unavailable_skill_switch_never_changes_state(case, changes):
    change_target(case, **changes)
    assert fails(case, message(case), "unit_detail_skill_ineligible").status == 409


@pytest.mark.parametrize("field,value,code", [("EvoSkillID", 0, "unit_detail_skill_ineligible"),
    ("EvoSkillID", 0x7FFFFFFF, "unit_detail_skill_ineligible"),
    ("ClassLV1SkillID", 0x7FFFFFFF, "unit_detail_skill_ineligible"),
    ("EvoSkillID", True, "unit_detail_catalog_invalid"),
    ("ClassLV1SkillID", -1, "unit_detail_catalog_invalid")])
def test_skill_resource_bounds_and_types(case, field, value, code):
    case.card[field] = value
    fails(case, message(case), code)


def test_zero_normal_skill_is_a_valid_index_and_skill_level_is_not_clamped(case):
    case.card["ClassLV1SkillID"] = 0
    change_target(case, GeneralPurposeFlags=5, SettingSkillID=case.card["EvoSkillID"])
    case.service.save(message(case), case.store)
    assert target(case)["SettingSkillID"] == 0 and target(case)["SkillLevel"] == 5
    assert target(case)["SkillExperience"] == 11


def test_zero_toggle_only_changes_display_even_without_skill_unlock(case):
    set_stage(case, 0)
    change_target(case, SkillEvolved=0)
    before = target(case)
    case.service.save(message(case, toggle=0, display=1), case.store)
    assert target(case) == {**before, "_DisplayType": 1}


@pytest.mark.parametrize("display", [0, -1, 20, 12345])
def test_existing_display_and_noop_always_preserved_with_exact_retry(case, display):
    change_target(case, _DisplayType=display, SkillEvolved=0)
    before = case.store.profile()
    envelope = message(case, toggle=0)
    response = case.service.save(envelope, case.store)
    committed = case.store.profile()
    assert committed.revision == before.revision + 1
    assert committed.state["player_state"] == before.state["player_state"]
    assert case.service.save(envelope, case.store) == response and case.store.profile() == committed


@pytest.mark.parametrize("stage,display", [(0, 0), (0, 1), (2, 1), (2, 2), (3, 3), (4, 3)])
def test_all_basic_display_forms(case, stage, display):
    set_stage(case, stage)
    case.service.save(message(case, toggle=0, display=display), case.store)
    assert target(case)["_DisplayType"] == display


@pytest.mark.parametrize("stage,display", [(0, 2), (0, 3), (2, 3), (4, 4), (4, 20), (2, 17), (2, 18), (2, 19)])
def test_display_stage_and_pc_boundaries(case, stage, display):
    set_stage(case, stage)
    assert fails(case, message(case, toggle=0, display=display), "unit_detail_display_unsupported").status == 501


@pytest.mark.parametrize("stage,display,field", [(0, 1, "CharaFace0"), (2, 2, "Face1"),
    (3, 3, "CharaFace3"), (4, 3, "CharaFace4")])
def test_missing_basic_face_is_rejected(case, stage, display, field):
    set_stage(case, stage)
    case.card[field] = 0
    fails(case, message(case, toggle=0, display=display), "unit_detail_display_unsupported")


@pytest.mark.parametrize("stage,display,base,phone", [(0, 17, "CharaFace0", "SmartPhoneImageEvolve0"),
    (2, 18, "Face1", "SmartPhoneImageEvolve2"), (3, 19, "CharaFace3", "SmartPhoneImageEvolve3"),
    (4, 19, "CharaFace4", "SmartPhoneImageEvolve4")])
def test_smartphone_forms_require_both_resources(case, stage, display, base, phone):
    case.service = UnitDetailService(case.bundle.bootstrap, case.bundle.player_state, case.growth, smartphone_images=True)
    set_stage(case, stage)
    for field in (base, phone):
        case.card[field] = 0
        fails(case, message(case, toggle=0, display=display), "unit_detail_display_unsupported")
        case.card[field] = 1
    case.service.save(message(case, toggle=0, display=display), case.store)
    assert target(case)["_DisplayType"] == display


def test_same_unsupported_saved_display_is_allowed_during_toggle(case):
    change_target(case, _DisplayType=777)
    case.service.save(message(case), case.store)
    assert target(case)["_DisplayType"] == 777 and target(case)["GeneralPurposeFlags"] & SKILL_SWITCH_FLAG


def test_wrong_unit_identity_and_cross_account_requests_cannot_commit(case, tmp_path):
    fails(case, message(case, index=99999), "unit_detail_unit_missing")
    other = Store(tmp_path / "other.sqlite3", case.store.profile().state)
    envelope = request(other, 10, (Container("PU", (Column("A7", "I", (case.index,)),
        Column("B0", "I", (0,)), Column("SC", "I", (1,)))),))
    fails(case, envelope, "invalid_identity")


def test_prince_index_zero_can_save_display_without_consumption_rules(case):
    case.service.save(message(case, index=0, display=0, toggle=0), case.store)
    assert case.store.profile().state["player_state"]["units"][0]["GetIndex"] == 0


def test_exact_retry_survives_reopened_database_and_service(case):
    envelope = message(case)
    response = case.service.save(envelope, case.store)
    saved = case.store.profile()
    reopened = Store(case.store.path)
    service = UnitDetailService(case.bundle.bootstrap, case.bundle.player_state, case.growth)
    assert service.save(envelope, reopened) == response and reopened.profile() == saved


def test_parallel_duplicate_request_toggles_once(case):
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: case.service.save(envelope, case.store), range(2)))
    assert responses[0] == responses[1] and case.store.profile().revision == 1
    assert target(case)["GeneralPurposeFlags"] == 5


@pytest.mark.parametrize("change", ["state", "content", "phone"])
def test_stale_replay_cannot_return_old_unit_or_ignore_content_policy(case, change):
    envelope = message(case)
    case.service.save(envelope, case.store)
    if change == "state":
        mutate(case, lambda state: state.update(unrelated="changed"))
    elif change == "content":
        case.growth.content_version = "a" * 64
    else:
        case.service = UnitDetailService(case.bundle.bootstrap, case.bundle.player_state, case.growth, smartphone_images=True)
    fails(case, envelope, "unit_detail_retry_stale")


def test_client_clock_rollback_toggles_again_while_exact_replays_stay_rejected(case):
    original = target(case)["GeneralPurposeFlags"]
    for tick in (1793548799, 1793548800, 0x7FFFFFFF, -0x80000000):
        case.service.save(message(case, tick=tick), case.store)
    fails(case, message(case, tick=0x7FFFFFFF), "unit_detail_retry_stale")
    case.service.save(message(case, tick=1793548801), case.store)
    assert target(case)["GeneralPurposeFlags"] == original ^ SKILL_SWITCH_FLAG
    history = case.store.profile().state["unit_detail_history"]
    assert history["schema_version"] == SCHEMA_VERSION and len(history["processed_keys"]) == 5


def test_same_second_distinct_requests_and_separate_operation_histories(case):
    mutate(case, lambda state: state.update(training_history={"untouched": True}, evolution_history={"untouched": True}))
    case.service.save(message(case, display=1), case.store)
    case.service.save(message(case, display=2), case.store)
    state = case.store.profile().state
    assert len(state["unit_detail_history"]["processed_keys"]) == 2
    assert state["training_history"] == state["evolution_history"] == {"untouched": True}
    assert target(case)["GeneralPurposeFlags"] == 1


def test_same_second_evicted_receipt_cannot_toggle_again(case):
    current = case.store.profile()
    state, added = case.bundle.player_state.credit_units(current.state,
        [{"card_id": 7, "level": 1, "count": HISTORY_LIMIT + 1}])
    case.store.update_state(state, current.revision)
    envelopes = [message(case, index=row["GetIndex"], display=1, toggle=0) for row in added]
    for envelope in envelopes:
        case.service.save(envelope, case.store)
    history = case.store.profile().state["unit_detail_history"]
    assert len(history["receipts"]) == HISTORY_LIMIT and len(history["processed_keys"]) == HISTORY_LIMIT + 1
    fails(case, envelopes[0], "unit_detail_request_stale")


def test_full_legacy_second_upgrades_and_accepts_earlier_client_time(case):
    seen = [hashlib.sha256(str(index).encode()).hexdigest() for index in range(PROCESSED_LIMIT)]
    mutate(case, lambda state: state.update(unit_detail_history={"schema_version": LEGACY_SCHEMA_VERSION,
        "receipts": [], "last_client_time": 1000, "seen_keys": seen}))
    case.service.save(message(case, tick=999), case.store)
    history = case.store.profile().state["unit_detail_history"]
    assert history["schema_version"] == SCHEMA_VERSION and len(history["processed_keys"]) == PROCESSED_LIMIT
    assert history["processed_keys"][:-1] == seen[1:]
    assert target(case)["GeneralPurposeFlags"] == 5


@pytest.mark.parametrize("history", [None, {}, {"schema_version": 1, "receipts": []},
    {"schema_version": True, "receipts": [], "last_client_time": None, "seen_keys": []},
    {"schema_version": 2, "receipts": [], "last_client_time": None, "seen_keys": []},
    {"schema_version": 1, "receipts": [], "last_client_time": None, "seen_keys": ["a" * 64]},
    {"schema_version": 1, "receipts": [], "last_client_time": True, "seen_keys": []}])
def test_invalid_history_is_never_reset(case, history):
    mutate(case, lambda state: state.update(unit_detail_history=history))
    fails(case, message(case), "unit_detail_history_invalid")


@pytest.mark.parametrize("field,value", [("response", "<broken>"), ("response", "\ud800"),
    ("client_time", True), ("revision", True), ("request", "invalid"), ("content", "invalid")])
def test_invalid_receipt_rejected_before_replay(case, field, value):
    envelope = message(case)
    case.service.save(envelope, case.store)
    current = case.store.profile()
    state = deepcopy(current.state)
    state["unit_detail_history"]["receipts"][0][field] = value
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state, ensure_ascii=True),))
    fails(case, envelope, "unit_detail_history_invalid")


def test_replay_still_validates_current_roster(case, monkeypatch):
    envelope = message(case)
    case.service.save(envelope, case.store)
    def reject(state, **_):
        raise ProtocolError("player_reference_invalid", "unavailable master", 503)
    monkeypatch.setattr(case.bundle.player_state, "validate", reject)
    fails(case, envelope, "player_reference_invalid")


@pytest.mark.parametrize("boundary", ["units", "response", "receipt"])
def test_encoding_and_capacity_fail_before_any_saved_toggle(case, boundary, monkeypatch):
    def bounded(profile, xml):
        if profile.registration_keys == ((41,) if boundary == "units" else (10,)):
            raise ProtocolError("response_too_large", "injected capacity failure")
        return encode_response(profile, xml)
    if boundary == "receipt":
        monkeypatch.setattr("aigis_ps.unit_detail.RESPONSE_LIMIT", 1)
    else:
        monkeypatch.setattr("aigis_ps.unit_detail.encode_response", bounded)
    fails(case, message(case), "unit_detail_response_capacity" if boundary == "receipt" else "response_too_large")


def test_cas_retry_uses_new_flags_and_preserves_other_writes(case, monkeypatch):
    update, calls = case.store.update_state, []
    def conflict(candidate, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = case.store.profile()
            current.state["player_state"]["units"][-1]["GeneralPurposeFlags"] = 0x73
            current.state["marker"] = {"other": 8}
            update(current.state, current.revision)
        return update(candidate, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    case.service.save(message(case), case.store)
    assert calls == [0, 1] and case.store.profile().revision == 2
    assert target(case)["GeneralPurposeFlags"] == 0x77 and case.store.profile().state["marker"] == {"other": 8}


def test_cas_retry_revalidates_switch_eligibility(case, monkeypatch):
    update, calls = case.store.update_state, []
    def conflict(candidate, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = case.store.profile()
            current.state["player_state"]["units"][-1]["SkillEvolved"] = 0
            update(current.state, current.revision)
        return update(candidate, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as caught:
        case.service.save(message(case), case.store)
    assert caught.value.code == "unit_detail_skill_ineligible" and calls == [0]
    assert "unit_detail_history" not in case.store.profile().state


def test_cas_exhaustion_does_not_mutate_input_or_store(case, monkeypatch):
    calls = []
    def reject(state, revision):
        calls.append(revision)
        raise ValueError("concurrent update")
    monkeypatch.setattr(case.store, "update_state", reject)
    fails(case, message(case), "profile_changed")
    assert calls == [0, 0, 0]


def test_invalid_growth_identity_cannot_save(case):
    case.growth.content_version = "wrong"
    fails(case, message(case), "unit_detail_dependencies_invalid")


def test_original_skill_ids_ignore_all_current_image_aliases(case):
    real = case.bundle.training.catalog
    checked = []
    for identifier in real.cards.card_ids:
        alias = real.aliases.resolve(identifier).image_card_id
        if alias == identifier:
            continue
        card = real.cards.card(identifier)
        current = case.store.profile()
        state, added = case.bundle.player_state.credit_units(current.state,
            [{"card_id": identifier, "level": 1, "count": 1}])
        row = state["player_state"]["units"][-1]
        row.update(ClassChangeCount=2, ClassID=case.bundle.evolution.catalog.route(card, 2)["ClassID"],
                   SkillEvolved=1, GeneralPurposeFlags=1, SettingSkillID=card["ClassLV1SkillID"])
        case.store.update_state(state, current.revision)
        index = added[0]["GetIndex"]
        if card["EvoSkillID"] <= 0:
            fails(case, message(case, index=index, display=0, tick=1000 + len(checked) * 2),
                  "unit_detail_skill_ineligible")
            checked.append(identifier)
            continue
        case.service.save(message(case, index=index, display=0, tick=1000 + len(checked) * 2), case.store)
        found = next(unit for unit in case.store.profile().state["player_state"]["units"] if unit["GetIndex"] == index)
        assert found["SettingSkillID"] == card["EvoSkillID"]
        case.service.save(message(case, index=index, display=0, tick=1001 + len(checked) * 2), case.store)
        found = next(unit for unit in case.store.profile().state["player_state"]["units"] if unit["GetIndex"] == index)
        assert found["SettingSkillID"] == card["ClassLV1SkillID"]
        checked.append(identifier)
    assert len(checked) == 20 and 497 in checked
    assert real.cards.card(497)["ClassLV1SkillID"] == 648 and real.cards.card(512)["ClassLV1SkillID"] == 718


def test_original_faces_do_not_use_image_alias(case):
    card = dict(case.growth.cards.card(497))
    card["CharaFace0"] = 0
    case.growth.cards.overrides[497] = card
    current = case.store.profile()
    state, added = case.bundle.player_state.credit_units(current.state, [{"card_id": 497, "level": 1, "count": 1}])
    case.store.update_state(state, current.revision)
    fails(case, message(case, index=added[0]["GetIndex"], display=1, toggle=0), "unit_detail_display_unsupported")


def test_other_protocol_cannot_use_detail_handler(case):
    envelope = message(case)
    fails(case, replace(envelope, profile=ProtocolRegistry.load().by_registration[119]), "invalid_request_fields")


class Skins:
    content_version = "b" * 64

    def __init__(self, allowed):
        self.allowed, self.calls = allowed, []

    def allowed_displays(self, card_id, state, *, smartphone_images):
        self.calls.append((card_id, deepcopy(state), smartphone_images))
        state["mutation"] = "not saved"
        return self.allowed

    def status(self):
        return {"content_version": self.content_version}


@pytest.mark.parametrize("phone,display", [(False, 5), (False, 8), (True, 21), (True, 24)])
def test_verified_skin_choices_use_original_card_and_saved_entitlements(case, phone, display):
    skins = Skins(frozenset((display,)))
    case.service = UnitDetailService(case.bundle.bootstrap, case.bundle.player_state, case.growth,
                                     smartphone_images=phone, skin_catalog=skins)
    before = case.store.profile()
    case.service.save(message(case, display=display, toggle=0), case.store)
    assert target(case)["_DisplayType"] == display
    assert skins.calls == [(7, before.state, phone)]
    assert "mutation" not in case.store.profile().state
    assert case.service.status()["skin_catalog"] == skins.status()


@pytest.mark.parametrize("allowed", [set(), {6}])
def test_unowned_skin_is_rejected(case, allowed):
    case.service.skin_catalog = Skins(allowed)
    fails(case, message(case, display=5, toggle=0), "unit_detail_display_unsupported")


@pytest.mark.parametrize("allowed", [{True}, {0}, {3}, {20}, {21}, (5,), None])
def test_skin_provider_cannot_expand_the_verified_display_domain(case, allowed):
    case.service.skin_catalog = Skins(allowed)
    fails(case, message(case, display=5, toggle=0), "unit_detail_catalog_invalid")


def test_receipt_replays_after_the_skin_catalog_attaches(case):
    envelope = message(case)
    response = case.service.save(envelope, case.store)
    saved = case.store.profile()
    case.service.skin_catalog = Skins({5})
    assert case.service.save(envelope, case.store) == response
    assert case.store.profile() == saved
    case.service.skin_catalog.content_version = "invalid"
    fails(case, envelope, "unit_detail_dependencies_invalid")


@pytest.mark.parametrize("current", [None, "c" * 64], ids=["removed", "changed"])
def test_receipt_of_a_removed_or_changed_skin_catalog_is_stale(case, current):
    case.service.skin_catalog = Skins({5})
    envelope = message(case)
    case.service.save(envelope, case.store)
    if current is None:
        case.service.skin_catalog = None
    else:
        case.service.skin_catalog.content_version = current
    fails(case, envelope, "unit_detail_retry_stale")


def test_forged_receipt_digest_is_stale(case):
    envelope = message(case)
    case.service.save(envelope, case.store)
    state = deepcopy(case.store.profile().state)
    state["unit_detail_history"]["receipts"][-1]["content"] = "f" * 64
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    case.service.skin_catalog = Skins({5})
    fails(case, envelope, "unit_detail_retry_stale")


def test_receipt_replays_after_growth_and_costume_inputs_attach(case, unfarmed):
    bundle = case.bundle
    current = bundle.training.catalog
    assert unfarmed.content_version in current.replayable_content_versions() - {current.content_version}
    earlier = UnitDetailService(bundle.bootstrap, bundle.player_state, unfarmed)
    attached = UnitDetailService(bundle.bootstrap, bundle.player_state, current, skin_catalog=Skins({5}))
    envelope = message(case, toggle=0)
    response = earlier.save(envelope, case.store)
    saved = case.store.profile()
    assert attached.save(envelope, case.store) == response
    assert case.store.profile() == saved
    forged = attached._content_version(skins=False, growth="c" * 64)
    state = deepcopy(saved.state)
    state["unit_detail_history"]["receipts"][-1]["content"] = forged
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    case.service = attached
    fails(case, envelope, "unit_detail_retry_stale")


def test_default_and_existing_display_do_not_require_entitlement_lookup(case):
    skins = Skins(None)
    case.service.skin_catalog = skins
    change_target(case, _DisplayType=555)
    case.service.save(message(case, toggle=0), case.store)
    case.service.save(message(case, display=0, toggle=0, tick=1001), case.store)
    assert skins.calls == []

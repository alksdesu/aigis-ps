from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps import admin_cmd_guide
from aigis_ps.admin_cmd_guide import COMMANDS, GUIDE, GUIDE_WRITES, NOTHING_SELECTED, SET_GUIDE
from aigis_ps.admin_cmd_units import summary
from aigis_ps.admin_command import missing_service
from aigis_ps.admin_commit import InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.alxml import Column, loads
from aigis_ps.energy_clock import instant
from aigis_ps.errors import ProtocolError
from aigis_ps.guide_service import GUIDE_FIELD, NO_GUIDE, TRUST_THRESHOLD, GuideService
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.player_state import LOCK_FLAG
from aigis_ps.storage import Store
from test_admin_cmd_display import (Case, Slices, Step, configured, failure, kernel_result, live_save, outcome, recorded,
                                    unsupported_player, without_config)
from test_admin_cmd_units import commands, commit, console, preview_error
from test_admin_commit import FakeGeneration, FakeRuntime, pipeline
from test_admin_seams import MemoryStore, canonical
from test_image_aliases import command_id, command_table, encode
from test_player_state import row
from test_quest_service import bundle, request
from test_unit_removal import unit_at


GOLDEN = Path(__file__).resolve().parent / "golden" / "p21b"
SELECT_GOLDEN, RESTRICTION_GOLDEN = GOLDEN / "guide-select.json", GOLDEN / "guide-restrictions.json"
GUIDE_SELECT, CONFIG_LOAD = 121, 36
FOREIGN_KEY = "c" * 32
GLOBAL_COMMANDS = "OnCommand.atb"
RATING_RULE = 'IsRating("R18");'
TRUSTED_CARD, UNTRUSTED_CARD, RATED_CARD, UNCATALOGED_CARD = 17, 3, 22, 8
ORIGINAL_TRUST = {TRUSTED_CARD: True, UNTRUSTED_CARD: False, RATED_CARD: True, UNCATALOGED_CARD: True}
ELIGIBLE, BELOW_THRESHOLD, UNTRUSTED, RATED, UNCATALOGED, MISSING_UNIT = 1, 2, 3, 4, 5, 99
BASE_UNITS = {ELIGIBLE: (TRUSTED_CARD, TRUST_THRESHOLD), BELOW_THRESHOLD: (TRUSTED_CARD, TRUST_THRESHOLD - 1),
              UNTRUSTED: (UNTRUSTED_CARD, TRUST_THRESHOLD), RATED: (RATED_CARD, TRUST_THRESHOLD),
              UNCATALOGED: (UNCATALOGED_CARD, TRUST_THRESHOLD)}
DEVOTED = TRUST_THRESHOLD + 50
# The whole stored row is checked, so an integer in a boolean sound switch blocks the guide save too.
SWITCH_FIELD = "UseBgm"
REFUSAL_STATUSES = {SELECT_GOLDEN: {400, 403, 409, 503}, RESTRICTION_GOLDEN: {409, 503}}
CONFIG_READBACK = {"config": [CONFIG_LOAD]}
NO_SECRETARY = {"GetIndex": NO_GUIDE, "unit": None}


def a7(index: int) -> tuple:
    return (Column("A7", "I", (index,)),)


def select(index: int = ELIGIBLE, **options) -> Step:
    return Step(GUIDE_SELECT, a7(index), **options)


def clear(**options) -> Step:
    return select(NO_GUIDE, **options)


def stored_guide(index: int) -> Callable[[dict], None]:
    return configured(GUIDE_FIELD, index)


def saved_guide(state: dict) -> int:
    return state["tables"]["config"][0][GUIDE_FIELD]


def trust(index: int, love: int) -> Callable[[dict], None]:
    def store(state: dict) -> None:
        unit_at(state, index)["Love"] = love

    return store


def locked_member(state: dict) -> None:
    unit_at(state, ELIGIBLE)["GeneralPurposeFlags"] |= LOCK_FLAG
    state["player_state"]["squads"] = [row(unit=ELIGIBLE)]


def consumed(state: dict) -> None:
    player = state["player_state"]
    player["units"] = [unit for unit in player["units"] if unit["GetIndex"] != ELIGIBLE]


def combined(*changes: Callable[[dict], None]) -> Callable[[dict], None]:
    def store(state: dict) -> None:
        for change in changes:
            change(state)

    return store


GUIDE_SLICES = Slices(config=(GUIDE_FIELD,))
SELECT_CASES = {
    "select": Case((select(),)),
    "select_above_threshold": Case((select(),), stored=trust(ELIGIBLE, DEVOTED)),
    "select_locked_squad_member": Case((select(),), stored=locked_member),
    "select_same_twice": Case((select(), select(tick=1001))),
    "replay": Case((select(), select())),
    "switch": Case((select(), select(RATED, tick=1001))),
    "clear_selected": Case((select(), clear(tick=1001))),
    "clear_empty": Case((clear(),)),
    "clear_missing_instance": Case((clear(),), stored=stored_guide(MISSING_UNIT)),
    "clear_negative_stored_value": Case((clear(),), stored=stored_guide(-1)),
    "keep_missing_instance_on_refusal": Case((select(BELOW_THRESHOLD),), stored=stored_guide(MISSING_UNIT)),
    "reselect_missing_instance": Case((select(MISSING_UNIT),), stored=stored_guide(MISSING_UNIT)),
    "consumed_selection": Case((select(), select(tick=1001, prepare=consumed))),
    "below_threshold": Case((select(BELOW_THRESHOLD),)),
    "zero_trust": Case((select(),), stored=trust(ELIGIBLE, 0)),
    "untrusted_card": Case((select(UNTRUSTED),)),
    "not_owned": Case((select(MISSING_UNIT),)),
    "largest_index_not_owned": Case((select(INT32_MAX),)),
    "negative_index": Case((select(-1),)),
    "missing_value": Case((Step(GUIDE_SELECT, ()),)),
    "duplicate_value": Case((Step(GUIDE_SELECT, a7(ELIGIBLE) * 2),)),
    "wrong_type": Case((Step(GUIDE_SELECT, (Column("A7", "S", (str(ELIGIBLE),)),)),)),
    "unknown_field": Case((Step(GUIDE_SELECT, a7(ELIGIBLE) + (Column("AB", "I", (1,)),)),)),
    "wrong_user_key": Case((select(user_key=FOREIGN_KEY),)),
    "wrong_method_key": Case((select(method_key=FOREIGN_KEY),)),
    "priority_request_before_identity": Case((select(-1, user_key=FOREIGN_KEY),)),
    "priority_identity_before_state": Case((select(user_key=FOREIGN_KEY),), stored=unsupported_player),
    "stored_player_invalid": Case((clear(),), stored=unsupported_player),
    "priority_player_before_config": Case((select(MISSING_UNIT),), stored=combined(unsupported_player, without_config)),
    "stored_config_missing": Case((clear(),), stored=without_config),
    "priority_config_before_ownership": Case((select(MISSING_UNIT),), stored=without_config),
    "stored_guide_invalid": Case((clear(),), stored=configured(GUIDE_FIELD, str(ELIGIBLE))),
    "stored_switch_invalid": Case((clear(),), stored=configured(SWITCH_FIELD, 1)),
}
RESTRICTION_CASES = {
    "eligible": Case((select(),)),
    "rating_restricted": Case((select(RATED),)),
    "priority_rating_before_trust": Case((select(RATED),), stored=trust(RATED, 0)),
    "uncataloged": Case((select(UNCATALOGED),)),
    "priority_catalog_before_trust": Case((select(UNCATALOGED),), stored=trust(UNCATALOGED, 0)),
    "priority_ownership_before_catalog": Case((select(MISSING_UNIT),)),
    "keep_rated_selection_on_refusal": Case((select(RATED),), stored=stored_guide(RATED)),
    "clear_rated_selection": Case((clear(),), stored=stored_guide(RATED)),
    "switch_from_uncataloged": Case((select(),), stored=stored_guide(UNCATALOGED)),
}
GOLDEN_SETS = {
    SELECT_GOLDEN: ((GUIDE_SELECT,), SELECT_CASES, GUIDE_SLICES),
    RESTRICTION_GOLDEN: ((GUIDE_SELECT,), RESTRICTION_CASES, GUIDE_SLICES),
}


def restricted_guide(services) -> GuideService:
    catalog = services.gifts.catalog
    cards = SimpleNamespace(card_ids=tuple(card for card in catalog.card_ids if card != UNCATALOGED_CARD),
                            card=catalog.card, snapshot=catalog.snapshot)
    rating = command_table([(command_id(f"IsCanUseRating_{RATED_CARD}"), encode(RATING_RULE))])

    def read(name: str) -> bytes:
        return rating if name == GLOBAL_COMMANDS else services.native_cdn.assets.read(name)[0]

    return GuideService(services.bootstrap, services.player_state, cards, read)


@pytest.fixture(scope="module")
def restricted(bundle):
    return restricted_guide(bundle)


def guides(services, restricted: GuideService) -> dict:
    return {SELECT_GOLDEN: services.guide, RESTRICTION_GOLDEN: restricted}


def golden_handlers(services, restricted: GuideService) -> dict:
    return {path: guide.save for path, guide in guides(services, restricted).items()}


def test_golden_bases_hold_the_units_the_cases_name(bundle, restricted):
    for path in GOLDEN_SETS:
        state = recorded(path)["base_state"]
        held = {index: (unit_at(state, index)["CardID"], unit_at(state, index)["Love"]) for index in BASE_UNITS}
        assert held == BASE_UNITS and saved_guide(state) == NO_GUIDE
        assert MISSING_UNIT not in {unit["GetIndex"] for unit in state["player_state"]["units"]}
    catalog = bundle.gifts.catalog
    assert {card: catalog.card(card)["BonusType"] != 0 for card in ORIGINAL_TRUST} == ORIGINAL_TRUST
    # The verified resources rate no card, so only the restricted catalog reaches the rating refusal.
    assert bundle.guide.status()["rating_conditions_unavailable"] == []
    assert restricted.status()["rating_conditions_unavailable"] == [RATED_CARD]
    assert restricted.status()["cards"] == bundle.guide.status()["cards"] - 1


@pytest.mark.parametrize("path", GOLDEN_SETS, ids=lambda path: path.stem)
def test_goldens_cover_every_case_with_successes_and_refusals(path):
    registrations, cases, _ = GOLDEN_SETS[path]
    golden = recorded(path)
    assert golden["registrations"] == list(registrations) and golden["cases"].keys() == cases.keys()
    steps = [step for case in golden["cases"].values() for step in case["steps"]]
    assert any("response" in step for step in steps) and any("error" in step for step in steps)
    assert {step["error"]["status"] for step in steps if "error" in step} == REFUSAL_STATUSES[path]


@pytest.mark.parametrize("path, name", [(path, name) for path, (_, cases, _) in GOLDEN_SETS.items() for name in cases],
                         ids=lambda value: value.stem if isinstance(value, Path) else value)
def test_native_saves_match_the_recorded_golden_bytes(bundle, restricted, path, name):
    _, cases, slices = GOLDEN_SETS[path]
    golden = recorded(path)
    result = outcome(golden_handlers(bundle, restricted)[path], golden["base_state"], cases[name], slices)
    assert canonical(result) == canonical(golden["cases"][name])


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_guide.register(commands)
    return commands


def guide_set(index: int) -> tuple[str, dict]:
    return SET_GUIDE.operation, {"get_index": index}


def chosen(state: dict, index: int) -> dict:
    return {"GetIndex": index, "unit": summary(unit_at(state, index))}


def native_selection(save: Callable, state: dict, step: Step) -> dict:
    store = MemoryStore(state)
    try:
        save(step.envelope(), store)
    except ProtocolError as error:
        return failure(error)
    return {"state": store.profile().state}


def test_guide_command_registers_against_the_preflight_table():
    catalog = discover(PREFLIGHT_DOMAINS)
    assert "admin_cmd_guide" in catalog.modules and SET_GUIDE.operation in catalog.commands
    assert [(command.operation, command.domain, sorted(command.touched)) for command in COMMANDS] == [
        ("guide.set", "settings", ["config"])]
    assert not SET_GUIDE.destructive and not SET_GUIDE.preflight_none
    assert GUIDE_WRITES == {f"config.{GUIDE_FIELD}"}
    assert catalog.commands.field_owners()[f"config.{GUIDE_FIELD}"] == SET_GUIDE.operation


def test_no_other_command_can_claim_the_secretary_field():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    commands.add(SET_GUIDE)
    with pytest.raises(ValueError, match=SET_GUIDE.operation):
        commands.add(replace(SET_GUIDE, operation="guide.other.set"))


@pytest.mark.parametrize("params", [
    {}, {"get_index": -1}, {"get_index": INT32_MAX + 1}, {"get_index": 1.0}, {"get_index": True}, {"get_index": "1"},
    {"get_index": None}, {"get_index": ELIGIBLE, "unique_id": 2}, {"A7": ELIGIBLE},
])
def test_parameters_are_strict(params):
    with pytest.raises(ProtocolError) as caught:
        SET_GUIDE.parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


@pytest.mark.parametrize("index", [NO_GUIDE, INT32_MAX])
def test_parameters_accept_the_whole_native_index_range(index):
    assert SET_GUIDE.parse({"get_index": index}).get_index == index


KERNEL_CHOICES = {
    "select": (SELECT_GOLDEN, ELIGIBLE, None),
    "select_locked_squad_member": (SELECT_GOLDEN, ELIGIBLE, locked_member),
    "clear": (SELECT_GOLDEN, NO_GUIDE, None),
    "clear_missing_instance": (SELECT_GOLDEN, NO_GUIDE, stored_guide(MISSING_UNIT)),
    "clear_negative_stored_value": (SELECT_GOLDEN, NO_GUIDE, stored_guide(-1)),
    "below_threshold": (SELECT_GOLDEN, BELOW_THRESHOLD, None),
    "zero_trust": (SELECT_GOLDEN, ELIGIBLE, trust(ELIGIBLE, 0)),
    "untrusted_card": (SELECT_GOLDEN, UNTRUSTED, None),
    "not_owned": (SELECT_GOLDEN, MISSING_UNIT, None),
    "largest_index_not_owned": (SELECT_GOLDEN, INT32_MAX, None),
    "consumed": (SELECT_GOLDEN, ELIGIBLE, consumed),
    "invalid_player": (SELECT_GOLDEN, ELIGIBLE, unsupported_player),
    "player_before_config": (SELECT_GOLDEN, MISSING_UNIT, combined(unsupported_player, without_config)),
    "config_before_ownership": (SELECT_GOLDEN, MISSING_UNIT, without_config),
    "invalid_stored_guide": (SELECT_GOLDEN, NO_GUIDE, configured(GUIDE_FIELD, str(ELIGIBLE))),
    "invalid_stored_switch": (SELECT_GOLDEN, NO_GUIDE, configured(SWITCH_FIELD, 1)),
    "rating_restricted": (RESTRICTION_GOLDEN, RATED, None),
    "rating_before_trust": (RESTRICTION_GOLDEN, RATED, trust(RATED, 0)),
    "uncataloged": (RESTRICTION_GOLDEN, UNCATALOGED, None),
    "catalog_before_trust": (RESTRICTION_GOLDEN, UNCATALOGED, trust(UNCATALOGED, 0)),
}


@pytest.mark.parametrize("name", KERNEL_CHOICES)
def test_selection_kernel_matches_the_native_save(bundle, restricted, name):
    path, index, stored = KERNEL_CHOICES[name]
    state = live_save(bundle, path)
    if stored is not None:
        stored(state)
    guide = guides(bundle, restricted)[path]
    expected = native_selection(guide.save, state, select(index))
    assert canonical(kernel_result(lambda current: guide.set_selection(current, index), state)) == canonical(expected)


@pytest.mark.parametrize("index", [-1, INT32_MAX + 1, True, "1", 1.0, None])
def test_selection_kernel_refuses_values_the_native_parser_never_produces(bundle, index):
    # Like 121, the selection value is checked before the stored state.
    state = live_save(bundle, SELECT_GOLDEN)
    unsupported_player(state)
    result = kernel_result(lambda current: bundle.guide.set_selection(current, index), state)
    assert (result["error"][0], result["error"][2]) == ("invalid_request_fields", 400)


def test_guide_plans_and_commits_only_the_secretary(bundle, preflight, registry):
    state = live_save(bundle, SELECT_GOLDEN)
    store, subject = console(bundle, state, preflight)
    plan = subject.preview(commands(registry, guide_set(ELIGIBLE)))
    assert plan.committable and plan.preflight["readback"] == CONFIG_READBACK
    assert plan.steps[0].effects == {"before": NO_SECRETARY, "after": chosen(state, ELIGIBLE)}
    assert not plan.steps[0].warnings and plan.describe()["writes"] == sorted(GUIDE_WRITES)
    committed = commit(subject, plan)
    assert committed.result["readback"] == CONFIG_READBACK and committed.result["backup_before_commit"] is None
    expected = deepcopy(state)
    expected["tables"]["config"][0][GUIDE_FIELD] = ELIGIBLE
    assert canonical(store.state) == canonical(expected) and store.revision == 1


def test_switching_and_clearing_report_both_selections(bundle, preflight, registry):
    state = live_save(bundle, SELECT_GOLDEN)
    store, subject = console(bundle, state, preflight)
    commit(subject, subject.preview(commands(registry, guide_set(ELIGIBLE))))
    plan = subject.preview(commands(registry, guide_set(RATED)))
    assert plan.steps[0].effects == {"before": chosen(state, ELIGIBLE), "after": chosen(state, RATED)}
    commit(subject, plan)
    plan = subject.preview(commands(registry, guide_set(NO_GUIDE)))
    assert plan.steps[0].effects == {"before": chosen(state, RATED), "after": NO_SECRETARY}
    assert not plan.steps[0].warnings
    commit(subject, plan)
    assert saved_guide(store.state) == NO_GUIDE and store.revision == 3


def test_reselecting_the_secretary_commits_no_change(bundle, preflight, registry):
    state = live_save(bundle, SELECT_GOLDEN)
    stored_guide(ELIGIBLE)(state)
    store, subject = console(bundle, state, preflight)
    plan = subject.preview(commands(registry, guide_set(ELIGIBLE)))
    assert plan.steps[0].effects == {"before": chosen(state, ELIGIBLE), "after": chosen(state, ELIGIBLE)}
    assert not plan.steps[0].warnings
    committed = commit(subject, plan)
    assert committed.result["revision_before"] == committed.result["revision_after"] == store.revision == 0
    assert canonical(store.state) == canonical(state)


def test_clearing_without_a_secretary_only_warns(bundle, preflight, registry):
    store, subject = console(bundle, live_save(bundle, SELECT_GOLDEN), preflight)
    plan = subject.preview(commands(registry, guide_set(NO_GUIDE)))
    assert plan.steps[0].effects == {"before": NO_SECRETARY, "after": NO_SECRETARY}
    assert plan.steps[0].warnings == (NOTHING_SELECTED,)
    committed = commit(subject, plan)
    assert committed.result["revision_before"] == committed.result["revision_after"] == store.revision == 0


def test_a_saved_secretary_that_is_no_longer_owned_can_be_cleared(bundle, preflight, registry):
    state = live_save(bundle, SELECT_GOLDEN)
    stored_guide(MISSING_UNIT)(state)
    store, subject = console(bundle, state, preflight)
    plan = subject.preview(commands(registry, guide_set(NO_GUIDE)))
    assert plan.steps[0].effects == {"before": {"GetIndex": MISSING_UNIT, "unit": None}, "after": NO_SECRETARY}
    assert not plan.steps[0].warnings
    commit(subject, plan)
    assert saved_guide(store.state) == NO_GUIDE and store.revision == 1


@pytest.mark.parametrize("index, stored, code", [
    (BELOW_THRESHOLD, None, "guide_ineligible"), (UNTRUSTED, None, "guide_ineligible"),
    (MISSING_UNIT, None, "guide_not_owned"), (ELIGIBLE, consumed, "guide_not_owned"),
])
def test_refusals_keep_the_native_codes(bundle, preflight, registry, index, stored, code):
    state = live_save(bundle, SELECT_GOLDEN)
    if stored is not None:
        stored(state)
    _, subject = console(bundle, state, preflight)
    error = preview_error(subject, commands(registry, guide_set(index)), code, 409)
    assert error.details == {"step": 0, "operation": SET_GUIDE.operation}


@pytest.mark.parametrize("index, code", [(RATED, "guide_rating_unsupported"), (UNCATALOGED, "guide_card_unavailable")])
def test_the_command_follows_the_generation_guide_catalog(bundle, restricted, preflight, registry, index, code):
    _, subject = console(replace(bundle, guide=restricted), live_save(bundle, RESTRICTION_GOLDEN), preflight)
    preview_error(subject, commands(registry, guide_set(index)), code, 503)


def test_the_command_needs_the_guide_service(bundle, preflight, registry):
    _, subject = console(replace(bundle, guide=None), live_save(bundle, SELECT_GOLDEN), preflight)
    error = preview_error(subject, commands(registry, guide_set(ELIGIBLE)), "admin_command_unavailable", 503)
    assert str(error) == missing_service(GUIDE)


def test_console_selection_reaches_the_native_reload_and_later_game_saves(bundle, preflight, registry, tmp_path):
    store = Store(tmp_path / "save.sqlite3", live_save(bundle, SELECT_GOLDEN))
    in_flight = request(store, GUIDE_SELECT, a7(RATED))
    generation = FakeGeneration(id="gen-guide", bundle=bundle, clock=instant(bundle.bootstrap.clock()))
    subject, *_ = pipeline(store=store, runtime=FakeRuntime(generation), preflight=preflight,
                           restart_check=InitializerRestartCheck())
    commit(subject, subject.preview(commands(registry, guide_set(ELIGIBLE))))
    reload = loads(bundle.bootstrap.config(request(store, CONFIG_LOAD, ()), store))
    assert next(column.values for column in reload.children if column.name == "C1") == (ELIGIBLE,)
    # 121 keeps no receipts, so a selection the client sent before the console write still lands afterwards.
    bundle.guide.save(in_flight, store)
    assert saved_guide(store.profile().state) == RATED and store.profile().revision == 2

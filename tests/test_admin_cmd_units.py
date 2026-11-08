from copy import copy, deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from aigis_ps import admin_cmd_growth, admin_cmd_sale, admin_cmd_units, admin_views_units, evolution_rewards, gift_service
from aigis_ps.admin_cmd_units import (CAPACITY_REASON, COMMANDS, EDIT_GROWTH, GRANT, NOTHING_BLOCKED, OVERRIDES_KEY,
                                      OVERRIDES_PHRASE, PREVIEW_INITIAL, REMOVE, REMOVE_OVERRIDES, CardUnitParams,
                                      UnitParams, distinct_units, override_report, protected_rows)
from aigis_ps.admin_command import AdminError, CommandContext, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_diff import REDACTED, diff_states, json_digest, render_diff
from aigis_ps.admin_fields import AWAKENING_LEDGER, EVOLUTION_REWARDS, GIFT_LEDGER, GIFTS
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, ViewRegistry, discover
from aigis_ps.admin_routes_state import describe_command
from aigis_ps.admin_views import PageParams
from aigis_ps.admin_views_units import OVERRIDE_PREVIEW_BYTES
from aigis_ps.energy_clock import instant
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import BARRACKS_SHIFT, EMPTY_SQUAD_SLOT, LOCK_FLAG, MAX_PLAYER_UNITS, SKILL_SWITCH_FLAG
from aigis_ps.storage import AUTO_BACKUP_KIND, serialize
from aigis_ps.training_catalog import TrainingRules
from aigis_ps.unit_growth_edit import OVERFLOW_DROPPED
from aigis_ps.unit_reference import BARRACKS_STORAGE, FIRST_BARRACKS, UnitReference
from aigis_ps.unit_removal import Protection
import test_shop_growth_catalog as growth_shop_fixture
import test_silver_trade_catalog as silver_fixture
from test_admin_cmd_growth import TRAINING_CASES, console_seed, growth_batch
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_preflight import none_reasons, unread_reasons
from test_admin_views_tools import GROWTH_CARD, GROWTH_PRODUCT, SILVER_CARD, SILVER_PRODUCT
from test_unit_removal import PRINCE_INDEX, bundle, granted, new_save, unit_at


UNIT_CARD, RESERVED_CARD = 17, 8
UNIT_READBACK = {"units": [41]}
GRANT_READBACK = {"squads": [39, 72], "units": [41]}
VIEW_NAMES = ["units.barracks", "units.ledgers", "units.overrides", "units.roster", "units.squads", "units.storage"]
SECOND_BARRACKS, SECOND_SUB_TEAM = 1 << BARRACKS_SHIFT, 1 << 16
NOW = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)
OVERRIDES_DOMAIN, OVERRIDES_POINTER = "unit_overrides", "/unit_dynamic_overrides"
BATTLE_OVERRIDES = {"0": {"Exp": 1}}
OVERRIDE_VALUES = (None, {}, [], BATTLE_OVERRIDES, [1], "invalid", 0, False)
GUARDED_FLOWS = {"training": 5, "silver_trade": 106, "shop_growth": 113}
OVERRIDE_REFUSALS = {"silver_trade": "silver_trade_overrides_unsupported",
                     "shop_growth": "shop_growth_overrides_unsupported"}


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_units.register(commands)
    return commands


def console(services, state, preflight):
    store = FakeStore(state)
    generation = FakeGeneration(id="gen-units", bundle=services, clock=instant(services.bootstrap.clock()))
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def commands(registry, *steps):
    return registry.batch([{"operation": operation, "params": params} for operation, params in steps])


def commit(subject, plan, confirmations=()):
    return subject.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=plan.expected_revision,
                          op_id=str(uuid.uuid4()), confirmations=confirmations)


def preview_error(subject, batch, code, status):
    with pytest.raises(AdminError) as caught:
        subject.preview(batch)
    assert (caught.value.code, caught.value.status) == (code, status)
    return caught.value


def roster(state):
    return [unit["GetIndex"] for unit in state["player_state"]["units"]]


def selector(unit):
    return {"get_index": unit["GetIndex"], "unique_id": unit["UniqueID"]}


def test_unit_commands_register_against_the_preflight_table():
    catalog = discover(PREFLIGHT_DOMAINS)
    assert {command.operation for command in COMMANDS} <= {command.operation for command in catalog.commands}
    assert set(VIEW_NAMES) <= {view.name for view in catalog.views}
    assert {"admin_cmd_units", "admin_views_units"} <= set(catalog.modules)
    assert (GRANT.touched, PREVIEW_INITIAL.touched) == ({"units", "squads"}, {"units"})
    assert REMOVE.touched == {"units", "gifts", "evolution_rewards"} and EDIT_GROWTH.touched == {"units", "gifts"}
    assert (REMOVE_OVERRIDES.domain, REMOVE_OVERRIDES.touched) == ("units", {OVERRIDES_DOMAIN})
    assert all(not command.preflight_none and command.touched <= PREFLIGHT_DOMAINS.keys() for command in COMMANDS)
    assert none_reasons(GRANT.touched | PREVIEW_INITIAL.touched) == {}
    assert none_reasons(REMOVE.touched) == unread_reasons("gifts", "evolution_rewards")
    assert none_reasons(EDIT_GROWTH.touched) == unread_reasons("gifts")
    assert none_reasons(REMOVE_OVERRIDES.touched) == unread_reasons(OVERRIDES_DOMAIN)
    assert [command.destructive for command in COMMANDS] == [False, False, True, False, True]
    # Unit rows belong to many game flows, so no unit command claims exclusive ownership of their fields.
    assert all(not command.writes for command in COMMANDS)


def test_selections_name_each_unit_once_and_protections_list_per_unit():
    first, same_index, same_identity = (UnitParams(get_index=index, unique_id=unique)
                                        for index, unique in ((3, 4), (3, 5), (6, 4)))
    distinct_units([first, UnitParams(get_index=6, unique_id=7)])
    with pytest.raises(ValueError, match="^Unit GetIndex values must be distinct$"):
        distinct_units([first, same_index])
    with pytest.raises(ValueError, match="^Material UniqueID values must be distinct$"):
        distinct_units([first, same_identity], "Material")
    assert protected_rows({3: (Protection.PRINCE, Protection.SQUAD)}) == [
        {"GetIndex": 3, "protections": ["prince", "squad"]}]


def test_card_units_name_a_barracks_instance_for_growth_and_sale():
    unit = CardUnitParams(get_index=3, unique_id=4, card_id=5)
    assert unit.reference() == UnitReference(BARRACKS_STORAGE, FIRST_BARRACKS, 3, 4, 5)
    assert unit.reference(2) == UnitReference(BARRACKS_STORAGE, 2, 3, 4, 5)
    assert issubclass(admin_cmd_growth.HeldMaterial, CardUnitParams)
    assert all(command.params_model.model_fields["target"].annotation is CardUnitParams
               for command in (admin_cmd_growth.EVOLVE, admin_cmd_growth.TRAIN))
    assert admin_cmd_sale.SELL.params_model.model_fields["units"].annotation == list[CardUnitParams]
    with pytest.raises(AdminError) as caught:
        admin_cmd_sale.SELL.parse({"units": [{"get_index": 3, "unique_id": 4, "card_id": 0}]})
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_removal_phrases_count_the_selected_units():
    one = REMOVE.parse({"units": [{"get_index": 3, "unique_id": 4}]})
    two = REMOVE.parse({"units": [{"get_index": 3, "unique_id": 4}, {"get_index": 5, "unique_id": 6}]})
    assert (REMOVE.phrase(one), REMOVE.phrase(two)) == ("REMOVE 1 UNIT", "REMOVE 2 UNITS")


def test_override_removal_takes_no_parameters_and_a_fixed_phrase():
    assert REMOVE_OVERRIDES.phrase(REMOVE_OVERRIDES.parse({})) == OVERRIDES_PHRASE == "REMOVE UNIT OVERRIDES"
    assert REMOVE_OVERRIDES.describe()["params_schema"]["properties"] == {}


@pytest.mark.parametrize("command, params", [
    (GRANT, {"card_id": 1, "level": 1}), (GRANT, {"card_id": UNIT_CARD, "level": 1, "evolution_count": 1}),
    (GRANT, {"card_id": UNIT_CARD, "level": 1, "evolution_count": 2.0}), (GRANT, {"card_id": UNIT_CARD}),
    (GRANT, {"card_id": UNIT_CARD, "level": 1, "count": 101}), (GRANT, {"card_id": UNIT_CARD, "level": -1}),
    (REMOVE, {"units": []}), (REMOVE, {"units": [{"get_index": 1}]}), (REMOVE, {"units": [{"get_index": 1, "unique_id": 0}]}),
    (REMOVE, {"units": [{"get_index": -1, "unique_id": 1}]}),
    (REMOVE, {"units": [{"get_index": 1, "unique_id": 2}, {"get_index": 1, "unique_id": 3}]}),
    (REMOVE, {"units": [{"get_index": 1, "unique_id": 2}, {"get_index": 3, "unique_id": 2}]}),
    (REMOVE, {"units": [{"get_index": index, "unique_id": index + 1} for index in range(MAX_PLAYER_UNITS + 1)]}),
    (EDIT_GROWTH, {"get_index": 1, "unique_id": 2}), (EDIT_GROWTH, {"get_index": 1, "unique_id": 2, "skill_level": 0}),
    (EDIT_GROWTH, {"get_index": 1, "unique_id": 2, "love": True}), (EDIT_GROWTH, {"get_index": 1, "unique_id": 2, "Exp": 1}),
    (EDIT_GROWTH, {"get_index": -1, "unique_id": 2, "love": 1}), (EDIT_GROWTH, {"get_index": 1, "unique_id": 2, "love": -1}),
    (EDIT_GROWTH, {"get_index": 1, "unique_id": 2, "experience": -1}),
    (REMOVE_OVERRIDES, {"force": True}), (REMOVE_OVERRIDES, {"sha256": "0" * 64}), (REMOVE_OVERRIDES, None),
])
def test_parameters_are_strict(command, params):
    with pytest.raises(AdminError) as caught:
        command.parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_grants_plan_and_commit_new_units_with_their_readback(bundle, preflight, registry):
    store, subject = console(bundle, new_save(bundle), preflight)
    plan = subject.preview(commands(registry, ("units.grant", {"card_id": UNIT_CARD, "level": 1, "count": 2})))
    assert plan.committable and plan.preflight["readback"] == GRANT_READBACK
    added = plan.steps[0].effects["added"]
    assert [(unit["GetIndex"], unit["CardID"]) for unit in added] == [(1, UNIT_CARD), (2, UNIT_CARD)]
    outcome = commit(subject, plan)
    assert outcome.result["readback"] == GRANT_READBACK and outcome.result["backup_before_commit"] is None
    assert roster(store.state) == [PRINCE_INDEX, 1, 2] and store.revision == 1 and store.backups == []


def test_grants_respect_the_barracks_capacity(bundle, preflight, registry):
    players = copy(bundle.player_state)
    players.capacity_provider = lambda state: 1
    _, subject = console(replace(bundle, player_state=players), new_save(bundle), preflight)
    error = preview_error(subject, commands(registry, ("units.grant", {"card_id": UNIT_CARD, "level": 1, "count": 2})),
                          "unit_roster_capacity", 409)
    assert error.details == {"step": 0, "operation": "units.grant"}


def test_grants_keep_the_room_reserved_for_a_battle_in_progress(bundle, preflight, registry):
    state = new_save(bundle)
    # Reservations hold technical roster room, so they are sized to fill every remaining row.
    room = MAX_PLAYER_UNITS - len(state["player_state"]["units"])
    state["quest_state"] = {"active": {"unit_reservations": [{"card_id": RESERVED_CARD, "level": 1, "count": room}]}}
    _, subject = console(bundle, state, preflight)
    grant = commands(registry, ("units.grant", {"card_id": UNIT_CARD, "level": 1}))
    preview_error(subject, grant, "unit_roster_capacity", 409)
    state["quest_state"] = {"active": {"quest_id": 1001}}
    _, subject = console(bundle, state, preflight)
    preview_error(subject, grant, "quest_reservation_unavailable", 409)


def test_grant_refusals_carry_the_admin_prefix(bundle, preflight, registry):
    players = copy(bundle.player_state)
    players._initialization = lambda: SimpleNamespace(content_version="unverified")
    _, subject = console(replace(bundle, player_state=players), new_save(bundle), preflight)
    preview_error(subject, commands(registry, ("units.grant", {"card_id": UNIT_CARD, "level": 1})),
                  "admin_unit_grant_dependencies_invalid", 503)


def test_grants_need_the_verified_barracks_capacity(bundle, preflight, registry):
    _, subject = console(replace(bundle, unit_capacity=None), new_save(bundle), preflight)
    preview_error(subject, commands(registry, ("units.grant", {"card_id": UNIT_CARD, "level": 1})),
                  "admin_command_unavailable", 503)


def test_initial_previews_report_the_native_values_and_change_nothing(bundle, preflight, registry):
    store, subject = console(bundle, new_save(bundle), preflight)
    plan = subject.preview(commands(registry, ("units.preview_initial",
                                               {"card_id": UNIT_CARD, "level": 50, "evolution_count": 2})))
    expected = bundle.player_state.initial_unit_values(UNIT_CARD, 50, evolution_count=2)
    assert plan.steps[0].effects["initial"] == asdict(expected)
    assert plan.state_sha256_before == plan.state_sha256_after and plan.diff["total"] == 0
    commit(subject, plan)
    assert store.revision == 0


def test_removal_needs_its_phrase_and_backs_up_the_save_first(bundle, preflight, registry):
    state, added = granted(bundle, new_save(bundle), card_id=UNIT_CARD, count=2)
    store, subject = console(bundle, state, preflight)
    plan = subject.preview(commands(registry, ("units.remove", {"units": [selector(added[0])]})))
    assert [item.phrase for item in plan.confirmations] == ["REMOVE 1 UNIT"]
    assert plan.preflight["readback"] == UNIT_READBACK
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_confirmation_required" and store.revision == 0
    outcome = commit(subject, plan, ("REMOVE 1 UNIT",))
    assert len(store.backups) == 1 and store.backups[0]["commits_before"] == 0
    assert outcome.result["backup_before_commit"]["kind"] == AUTO_BACKUP_KIND
    assert roster(store.state) == [PRINCE_INDEX, added[1]["GetIndex"]] and "gift_state" in store.state
    effects = outcome.result["steps"][0]["effects"]
    assert effects["removed"][0]["UniqueID"] == added[0]["UniqueID"] and effects["last_unique_id"] == added[-1]["UniqueID"]


def guarded_save(bundle):
    state, added = granted(bundle, new_save(bundle), card_id=UNIT_CARD, count=4)
    state, reserved = granted(bundle, state, card_id=RESERVED_CARD)
    locked, organized, secretary, free = added
    unit_at(state, locked["GetIndex"])["GeneralPurposeFlags"] |= LOCK_FLAG
    team = state["player_state"]["battle"]["TN"]
    state["player_state"]["squads"] = [{"Address": team << 24, "UnitIndex": organized["GetIndex"], "Name": "team",
                                        "Flags": 0}]
    state["tables"]["config"][0]["_GuideUnitIndex"] = secretary["GetIndex"]
    state["quest_state"] = {"active": {"unit_reservations": [{"card_id": RESERVED_CARD, "level": 1, "count": 1}]}}
    prince = unit_at(state, PRINCE_INDEX)
    return state, [prince, locked, organized, secretary, reserved[0]], free


def test_removal_names_every_protection_that_keeps_a_unit(bundle, preflight, registry):
    state, guarded, free = guarded_save(bundle)
    _, subject = console(bundle, state, preflight)
    error = preview_error(subject, commands(registry, ("units.remove", {"units": [selector(unit) for unit in guarded]})),
                          "admin_unit_removal_protected", 409)
    assert error.details == {"step": 0, "operation": "units.remove", "protected": [
        {"GetIndex": unit["GetIndex"], "protections": reasons} for unit, reasons in zip(guarded, (
            [Protection.PRINCE], [Protection.LOCKED], [Protection.SQUAD, Protection.CURRENT_SQUAD],
            [Protection.SECRETARY], [Protection.BATTLE_RESERVATION]))]}
    plan = subject.preview(commands(registry, ("units.remove", {"units": [selector(free)]})))
    assert plan.committable and plan.restart_check == {"ok": True}


def test_removal_needs_both_reward_ledgers(bundle, preflight, registry):
    state, added = granted(bundle, new_save(bundle), card_id=UNIT_CARD)
    for missing in ("evolution_rewards", "gifts"):
        _, subject = console(replace(bundle, **{missing: None}), state, preflight)
        error = preview_error(subject, commands(registry, ("units.remove", {"units": [selector(added[0])]})),
                              "admin_command_unavailable", 503)
        assert missing in str(error)


def test_growth_plans_report_changes_limits_and_dropped_overflow(bundle, preflight, registry):
    state, added = granted(bundle, new_save(bundle), card_id=UNIT_CARD)
    growth = copy(bundle.training.catalog)
    growth.rules = TrainingRules(retain_overflow_experience=False)
    store, subject = console(replace(bundle, training=SimpleNamespace(catalog=growth)), state, preflight)
    plan = subject.preview(commands(registry, ("units.edit_growth", {**selector(added[0]), "experience": 2 ** 30,
                                                                     "love": 100})))
    step = plan.steps[0]
    limits = step.effects["limits"]
    assert step.warnings == (OVERFLOW_DROPPED,) and limits["level"] == limits["level_cap"]
    assert step.effects["changes"] == {"Exp": {"before": 0, "after": limits["cap_experience"]},
                                       "Love": {"before": 0, "after": 100}}
    outcome = commit(subject, plan)
    assert outcome.result["readback"] == UNIT_READBACK and store.revision == 1
    assert store.state["tables"]["presents"] == state["tables"]["presents"]


def test_growth_refusals_carry_the_admin_prefix(bundle, preflight, registry):
    state, added = granted(bundle, new_save(bundle), card_id=RESERVED_CARD)
    _, subject = console(bundle, state, preflight)
    error = preview_error(subject, commands(registry, ("units.edit_growth", {**selector(added[0]), "love": 101})),
                          "admin_unit_growth_limit", 409)
    assert error.details == {"step": 0, "operation": "units.edit_growth"}


def test_growth_edits_need_the_training_tables_and_the_trust_ledger(bundle, preflight, registry):
    state, added = granted(bundle, new_save(bundle), card_id=UNIT_CARD)
    for missing in ("training", "gifts"):
        _, subject = console(replace(bundle, **{missing: None}), state, preflight)
        preview_error(subject, commands(registry, ("units.edit_growth", {**selector(added[0]), "love": 1})),
                      "admin_command_unavailable", 503)


@pytest.mark.parametrize("command, missing, reason", [
    (GRANT, "unit_capacity", CAPACITY_REASON),
    (REMOVE, "gifts", missing_service("gifts")),
    (REMOVE, "evolution_rewards", missing_service("evolution_rewards")),
    (EDIT_GROWTH, "training", missing_service("training")),
    (EDIT_GROWTH, "gifts", missing_service("gifts")),
])
def test_the_command_catalog_lists_unit_commands_without_their_services_as_unavailable(bundle, command, missing, reason):
    assert describe_command(command, bundle)["available"] is True
    listed = describe_command(command, replace(bundle, **{missing: None}))
    assert (listed["available"], listed["unavailable_reason"]) == (False, reason)


def test_initial_previews_are_always_listed_as_available(bundle):
    assert describe_command(PREVIEW_INITIAL, replace(bundle, unit_capacity=None, gifts=None, evolution_rewards=None,
                                                     training=None))["available"] is True


def test_batches_of_unit_commands_write_no_game_receipts(bundle, preflight, registry):
    state = new_save(bundle)
    store, subject = console(bundle, state, preflight)
    plan = subject.preview(commands(registry, ("units.grant", {"card_id": UNIT_CARD, "level": 1, "count": 2}),
                                    ("units.edit_growth", {"get_index": 1, "unique_id": 2, "skill_level": 2}),
                                    ("units.remove", {"units": [{"get_index": 2, "unique_id": 3}]})))
    outcome = commit(subject, plan, ("REMOVE 1 UNIT",))
    assert outcome.result["readback"] == GRANT_READBACK and len(store.backups) == 1
    assert set(store.state) - set(state) == {"gift_state"}
    assert store.state["player_state"]["last_unique_id"] == 3 and roster(store.state) == [PRINCE_INDEX, 1]


def overridden_save(bundle, value=BATTLE_OVERRIDES):
    state = new_save(bundle)
    state[OVERRIDES_KEY] = deepcopy(value)
    return state


def removal(state):
    context = CommandContext(now=NOW, acquisition_day=1, revision=0, generation_id="gen-units")
    return REMOVE_OVERRIDES.apply(state, REMOVE_OVERRIDES.parse({}), context)


def refusal(call):
    try:
        call()
    except ProtocolError as error:
        return error.code
    return None


@pytest.mark.parametrize("value, shape, blocking, entries", [
    (None, "empty", False, None), ({}, "empty", False, 0), ([], "empty", False, 0),
    (BATTLE_OVERRIDES, "entries", True, 1), ([1, 2], "entries", True, 2), ({"ユニット": 1}, "entries", True, 1),
    ("invalid", "invalid", True, None), (0, "invalid", True, None), (False, "invalid", True, None),
])
def test_override_reports_describe_each_stored_shape(value, shape, blocking, entries):
    assert override_report({OVERRIDES_KEY: value}).to_dict() == {
        "present": True, "blocking": blocking, "shape": shape, "entries": entries,
        "bytes": len(serialize(value).encode("utf-8")), "sha256": json_digest(value)}


def test_override_reports_tell_a_missing_key_from_an_empty_one():
    assert override_report({}).to_dict() == {"present": False, "blocking": False, "shape": "absent", "entries": None,
                                             "bytes": None, "sha256": None}


@pytest.mark.parametrize("value", OVERRIDE_VALUES)
def test_reports_block_exactly_the_saves_the_exchange_guards_refuse(value):
    growth_shop = growth_shop_fixture.create(growth_shop_fixture.setup_case())
    silver = silver_fixture.create(silver_fixture.setup_case())
    for check, saved in ((lambda save: growth_shop_fixture.values(growth_shop, save), growth_shop_fixture.state()),
                         (lambda save: silver_fixture.select(silver, save), silver_fixture.state())):
        expected = check(deepcopy(saved))
        saved[OVERRIDES_KEY] = value
        assert (refusal(lambda: check(saved)) is not None) is override_report(saved).blocking
        assert check(removal(saved).state) == expected


@pytest.mark.parametrize("value", OVERRIDE_VALUES)
def test_override_removal_drops_only_the_overrides_and_reports_them(value):
    state = {"player_state": {"units": []}, OVERRIDES_KEY: deepcopy(value)}
    result = removal(state)
    assert result.state == {"player_state": {"units": []}} and state[OVERRIDES_KEY] == value
    assert result.effects == {"removed": override_report(state).to_dict()}
    assert result.warnings == (() if override_report(state).blocking else (NOTHING_BLOCKED,))


def test_override_removal_refuses_saves_without_overrides():
    with pytest.raises(AdminError) as caught:
        removal({"player_state": {"units": []}})
    assert (caught.value.code, caught.value.status) == ("admin_unit_overrides_absent", 409)
    assert OVERRIDES_KEY in str(caught.value)


def test_override_removal_needs_no_service():
    listed = describe_command(REMOVE_OVERRIDES, SimpleNamespace())
    assert (listed["available"], listed["destructive"], listed["touched"]) == (True, True, [OVERRIDES_DOMAIN])


def test_override_removal_needs_its_phrase_backs_up_and_audits_the_removed_value(bundle, preflight, registry):
    state = overridden_save(bundle)
    store, subject = console(bundle, state, preflight)
    removed = [{"op": "remove", "path": OVERRIDES_POINTER, "before": BATTLE_OVERRIDES}]
    plan = subject.preview(commands(registry, (REMOVE_OVERRIDES.operation, {})))
    assert [item.phrase for item in plan.confirmations] == [OVERRIDES_PHRASE]
    assert plan.preflight == {"ok": True, "readback": {}} and plan.restart_check == {"ok": True}
    assert plan.diff["entries"] == removed and plan.steps[0].warnings == ()
    assert dict(plan.steps[0].effects) == {"removed": override_report(state).to_dict()}
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_confirmation_required" and store.revision == 0 and store.backups == []
    outcome = commit(subject, plan, (OVERRIDES_PHRASE,))
    assert outcome.result["backup_before_commit"]["kind"] == AUTO_BACKUP_KIND and outcome.result["readback"] == {}
    assert len(store.backups) == 1 and store.backups[0]["commits_before"] == 0
    # Removal never discards silently: the automatic backup and the audit diff keep the removed value.
    assert store.backups[0]["state"][OVERRIDES_KEY] == BATTLE_OVERRIDES
    record = store.calls[-1]["record"]
    assert record["targets"] == [OVERRIDES_DOMAIN] and record["diff"]["entries"] == removed
    assert OVERRIDES_KEY not in store.state and store.revision == 1
    error = preview_error(subject, commands(registry, (REMOVE_OVERRIDES.operation, {})),
                          "admin_unit_overrides_absent", 409)
    assert error.details == {"step": 0, "operation": REMOVE_OVERRIDES.operation} and len(store.backups) == 1


def test_override_removal_commits_only_against_the_previewed_revision(bundle, preflight, registry):
    store, subject = console(bundle, overridden_save(bundle), preflight)
    plan = subject.preview(commands(registry, (REMOVE_OVERRIDES.operation, {})))
    store.revision += 1
    with pytest.raises(AdminError) as caught:
        commit(subject, plan, (OVERRIDES_PHRASE,))
    assert (caught.value.code, caught.value.current_revision) == ("admin_revision_conflict", 1)
    assert store.backups == [] and store.state[OVERRIDES_KEY] == BATTLE_OVERRIDES


def exchange_refusals(bundle, state, silver, growth):
    trades, player = bundle.trades, state["player_state"]
    return {"silver_trade": refusal(lambda: trades.silver.material(
                player, trades.silver.product(SILVER_PRODUCT), silver["GetIndex"], silver["UniqueID"], state=state)),
            "shop_growth": refusal(lambda: trades.growth_shop.values(
                player, trades.growth_shop.product(GROWTH_PRODUCT), growth["GetIndex"], state=state))}


def test_override_removal_lets_training_silver_exchange_and_shop_growth_run_again(bundle, preflight):
    case = TRAINING_CASES["dynamic_unit_overrides"]
    seeded = console_seed(bundle, case)
    state, (silver, growth) = bundle.player_state.credit_units(
        seeded.state, [{"card_id": card_id, "level": 1, "count": 1} for card_id in (SILVER_CARD, GROWTH_CARD)])
    seeded.state = bundle.initialize(state)
    growth_commands = CommandRegistry(PREFLIGHT_DOMAINS)
    for module in (admin_cmd_units, admin_cmd_growth):
        module.register(growth_commands)
    store, subject = console(bundle, seeded.state, preflight)
    preview_error(subject, growth_batch(growth_commands, case, seeded), "training_recipe_unsupported", 501)
    assert exchange_refusals(bundle, store.state, silver, growth) == OVERRIDE_REFUSALS
    removal_plan = subject.preview(commands(growth_commands, (REMOVE_OVERRIDES.operation, {})))
    commit(subject, removal_plan, (OVERRIDES_PHRASE,))
    assert OVERRIDES_KEY not in store.state and store.revision == 1
    assert subject.preview(growth_batch(growth_commands, case, seeded)).committable
    assert exchange_refusals(bundle, store.state, silver, growth) == dict.fromkeys(OVERRIDE_REFUSALS)


def view_registry():
    views = ViewRegistry()
    admin_views_units.register(views)
    return views


def read(name, services, state, params=None, revision=0):
    context = SimpleNamespace(state=state, revision=revision, now=NOW, generation=SimpleNamespace(bundle=services))
    return view_registry().get(name).read(context, params)


def test_unit_views_register_as_redacted_reads():
    views = view_registry()
    assert [view.name for view in views] == VIEW_NAMES and {view.domain for view in views} == {"units"}
    assert all(callable(getattr(view.read, "__wrapped__", None)) for view in views)
    assert views.get("units.roster").params_model is PageParams
    assert all(view.params_model is None for view in views if view.name != "units.roster")


def test_roster_pages_units_with_flags_protections_and_growth(bundle):
    state, guarded, free = guarded_save(bundle)
    unit_at(state, free["GetIndex"])["GeneralPurposeFlags"] |= SKILL_SWITCH_FLAG | SECOND_BARRACKS
    data = read("units.roster", bundle, state, PageParams(offset=1, limit=4))
    assert data["protections"] == {"ok": True} and data["growth"] == {"ok": True}
    player = state["player_state"]
    assert data["battle"] == player["battle"] and data["last_unique_id"] == player["last_unique_id"]
    assert (data["units"]["total"], data["units"]["offset"], data["units"]["next_offset"]) == (6, 1, 5)
    entries = {entry["unit"]["GetIndex"]: entry for entry in data["units"]["items"]}
    assert entries[guarded[1]["GetIndex"]]["protections"] == ["locked"]
    assert entries[free["GetIndex"]]["protections"] == []
    flags = entries[free["GetIndex"]]["flags"]
    assert flags["barracks"] == 1
    assert [(bit["bit"], bit["name"], bit["set"]) for bit in flags["bits"]] == [
        (1, "locked", False), (2, "awakened_skill_selected", True), (0, None, True)]
    growth = entries[free["GetIndex"]]["growth"]
    assert growth["ok"] is True and growth["level"] == 1 and growth["love_limit"] == 150


def test_roster_reports_unavailable_growth_and_protection_checks(bundle):
    state, _, _ = guarded_save(bundle)
    state["quest_state"] = {"active": {"quest_id": 1001}}
    data = read("units.roster", replace(bundle, training=None), state, PageParams())
    assert data["protections"]["error"]["code"] == "quest_reservation_unavailable"
    assert data["growth"]["error"]["code"] == "admin_view_service_unavailable"
    assert all(entry["protections"] is None and entry["growth"] is None for entry in data["units"]["items"])


def test_barracks_view_reports_usage_grantable_room_and_reservations(bundle):
    state, _, _ = guarded_save(bundle)
    data = read("units.barracks", bundle, state)
    usage = bundle.unit_capacity.usage(state)
    assert data["barracks"] == {"ok": True, "capacities": list(usage.capacities), "counts": list(usage.counts),
                                "primary_available": bundle.unit_capacity.available(state)}
    assert data["grantable"] == {"ok": True, "units": bundle.player_state.available_unit_slots(state)}
    assert data["battle_reservations"]["grants"] == [{"card_id": RESERVED_CARD, "level": 1, "count": 1}]
    missing = read("units.barracks", replace(bundle, unit_capacity=None), state)
    assert missing["barracks"]["error"]["code"] == "admin_view_service_unavailable"


def test_squads_view_groups_members_and_marks_the_current_teams(bundle):
    state, guarded, free = guarded_save(bundle)
    battle = state["player_state"]["battle"]
    state["player_state"]["squads"][:0] = [{"Address": battle["TN"] << 24 | 2, "UnitIndex": guarded[1]["GetIndex"],
                                            "Name": "team", "Flags": 0}]
    state["player_state"]["squads"].extend([
        {"Address": battle["RTN"] << 24 | SECOND_SUB_TEAM, "UnitIndex": free["GetIndex"], "Name": "raid", "Flags": 7},
        {"Address": battle["RTN"] << 24 | 1, "UnitIndex": EMPTY_SQUAD_SLOT, "Name": "raid", "Flags": 7}])
    data = read("units.squads", bundle, state)
    assert data["battle"] == battle and data["legacy_squad_imported"] is False
    assert data["squads"] == [
        {"group": battle["TN"], "name": "team", "flags": 0, "selected": True, "raid": False,
         "members": [{"team": 0, "slot": 0, "GetIndex": guarded[2]["GetIndex"], "CardID": UNIT_CARD},
                     {"team": 0, "slot": 2, "GetIndex": guarded[1]["GetIndex"], "CardID": UNIT_CARD}]},
        {"group": battle["RTN"], "name": "raid", "flags": 7, "selected": False, "raid": True,
         "members": [{"team": 0, "slot": 1, "GetIndex": None, "CardID": None},
                     {"team": 1, "slot": 0, "GetIndex": free["GetIndex"], "CardID": UNIT_CARD}]}]


def test_storage_view_reads_each_stock_through_its_service(bundle):
    state = new_save(bundle)
    data = read("units.storage", bundle, state)
    assert data["fame"] == {"ok": True, "rows": [], "deposit_limit": bundle.fame_repository.status()["deposit_limit"]}
    assert data["fairy"]["rows"] == bundle.fairy_farm.read(state) and data["fairy"]["per_card_capacity"] == bundle.fairy_farm.cap
    assert data["silver"] == {"ok": True, "rows": [], "per_card_capacity": bundle.silver_house.cap}
    missing = read("units.storage", replace(bundle, fame_repository=None), state)
    assert missing["fame"]["error"]["code"] == "admin_view_service_unavailable" and missing["fairy"]["ok"] is True


def test_ledgers_view_tells_stored_ledgers_from_projected_baselines(bundle):
    state, added = granted(bundle, new_save(bundle), card_id=UNIT_CARD)
    unit_at(state, added[0]["GetIndex"])["Love"] = 100
    data = read("units.ledgers", bundle, state)
    assert data["evolution_rewards"] == {"ok": True, "projected": False, "persisted": True,
                                         "ledger": state["evolution_rewards"]}
    trust = data["gifts"]
    assert (trust["projected"], trust["persisted"]) == (True, False)
    assert trust["ledger"] == {"claims": {str(UNIT_CARD): [1, 100]}, "receipts": 0, "processed_requests": 0}
    del state["evolution_rewards"]
    assert read("units.ledgers", bundle, state)["evolution_rewards"]["projected"] is True


def test_ledgers_view_counts_the_stored_gift_receipts(bundle):
    state = new_save(bundle)
    receipt = {"request": "a" * 64, "content": "b" * 64, "revision": 1, "response": "<DA/>", "committed_at": 1.0}
    state["gift_state"] = {"schema_version": 2, "reward_scope": "card", "claims": {str(UNIT_CARD): [1]},
                           "history": {"schema_version": 2, "receipts": [receipt],
                                       "processed_keys": ["c" * 64, receipt["request"]]}}
    trust = read("units.ledgers", bundle, state, revision=1)["gifts"]
    assert (trust["projected"], trust["persisted"]) == (False, True)
    assert trust["ledger"] == {"claims": {str(UNIT_CARD): [1]}, "receipts": 1, "processed_requests": 2}


@pytest.mark.parametrize("stored", [False, True], ids=["projected", "stored"])
def test_ledgers_view_reads_the_ledgers_the_next_write_saves_without_copying_the_save(bundle, monkeypatch, stored):
    state, added = granted(bundle, new_save(bundle), card_id=UNIT_CARD)
    unit_at(state, added[0]["GetIndex"])["Love"] = 100
    if stored:
        state = bundle.gifts.preserve_history(state, 0)
    else:
        del state[AWAKENING_LEDGER]
    copied = []
    for module in (evolution_rewards, gift_service):
        monkeypatch.setattr(module, "deepcopy", lambda value, *args: copied.append(value) or deepcopy(value, *args))
    data = read("units.ledgers", bundle, state)
    assert not any(value is state for value in copied)
    awakening = bundle.evolution_rewards.initialize(state)[AWAKENING_LEDGER]
    journal = bundle.gifts.preserve_history(state, 0)[GIFT_LEDGER]
    assert sum(value is state for value in copied) == 2
    assert bundle.evolution_rewards.read(state) == awakening and bundle.gifts.read(state, 0) == journal
    history = journal[gift_service.HISTORY_FIELD]
    assert data[EVOLUTION_REWARDS]["ledger"] == awakening
    assert data[GIFTS]["ledger"] == {"claims": journal["claims"], "receipts": len(history["receipts"]),
                                     "processed_requests": len(history["processed_keys"])}
    assert all(data[name]["persisted"] is stored for name in (EVOLUTION_REWARDS, GIFTS))


def test_unit_views_read_without_writing(bundle):
    state, _, _ = guarded_save(bundle)
    digest = json_digest(state)
    for name in VIEW_NAMES:
        read(name, bundle, state, PageParams() if name == "units.roster" else None)
    assert json_digest(state) == digest


def expected_overrides(report, value, *, truncated=False):
    return {**report.to_dict(), "pointer": OVERRIDES_POINTER, "truncated": truncated, "value": value,
            "guards": GUARDED_FLOWS, "removal": REMOVE_OVERRIDES.operation}


def test_overrides_view_reports_saves_without_overrides_without_any_service():
    assert read("units.overrides", None, {}) == expected_overrides(override_report({}), None)


@pytest.mark.parametrize("value", OVERRIDE_VALUES)
def test_overrides_view_shows_the_stored_value_and_whether_it_blocks_without_writing(value):
    state = {OVERRIDES_KEY: deepcopy(value)}
    digest = json_digest(state)
    data = read("units.overrides", None, state)
    assert data == expected_overrides(override_report(state), value) and json_digest(state) == digest


@pytest.mark.parametrize("extra, truncated", [(0, False), (1, True)])
def test_overrides_view_previews_values_up_to_the_plan_diff_limit(extra, truncated):
    value = "x" * (OVERRIDE_PREVIEW_BYTES - len(serialize("")) + extra)
    state = {OVERRIDES_KEY: value}
    data = read("units.overrides", None, state)
    assert data["bytes"] == OVERRIDE_PREVIEW_BYTES + extra
    assert data == expected_overrides(override_report(state), None if truncated else value, truncated=truncated)
    listed = {"truncated": True, "bytes": data["bytes"], "sha256": data["sha256"]} if truncated else value
    assert render_diff(diff_states(state, removal(state).state))["entries"] == [
        {"op": "remove", "path": data["pointer"], "before": listed}]


def test_overrides_view_redacts_account_secrets_in_the_shown_value():
    data = read("units.overrides", None, {OVERRIDES_KEY: {"user_key": "secret"}})
    assert data["value"] == {"user_key": REDACTED} and (data["shape"], data["blocking"]) == ("entries", True)


def test_oversized_overrides_carry_the_digest_the_removal_plan_lists(bundle, preflight, registry):
    state = overridden_save(bundle, {"rows": "x" * OVERRIDE_PREVIEW_BYTES})
    _, subject = console(bundle, state, preflight)
    plan = subject.preview(commands(registry, (REMOVE_OVERRIDES.operation, {})))
    data = read("units.overrides", bundle, state)
    assert (data["truncated"], data["value"]) == (True, None)
    assert plan.diff["entries"] == [{"op": "remove", "path": data["pointer"],
                                     "before": {"truncated": True, "bytes": data["bytes"], "sha256": data["sha256"]}}]

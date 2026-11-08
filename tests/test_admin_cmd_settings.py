from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient

from aigis_ps import admin_cmd_settings, admin_cmd_stories, admin_routes_state, admin_views_progress, admin_views_settings
from aigis_ps.admin_app import create_admin_app
from aigis_ps.admin_cmd_settings import (BACKGROUND_ROWS_UNSUPPORTED, BACKGROUND_SAVE_TIME_FUTURE, BACKGROUNDS, CLAIMED,
                                         DAMAGE_DISPLAY, DAMAGE_DISPLAY_FIELD, DAMAGE_DISPLAY_WRITES, FINISHED, FLAGS,
                                         MISSIONS_READ, NOTHING_MARKED, PERUSED, PURCHASE_FLAG_ACK,
                                         PURCHASE_FLAG_ACK_REQUIRED, SAVE_TIME_FORMAT, SKIN_HISTORY, SKIN_SHOP_REASON, SOUND,
                                         SOUND_WRITES, SoundParams, unread)
from aigis_ps.admin_command import (AdminError, Batch, BatchStep, CommandContext, NoopPreflight, NoopRestartCheck,
                                    PlanStore, counted, missing_service)
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck, requires_backup
from aigis_ps.admin_diff import json_digest
from aigis_ps.admin_fields import BIT_TABLES, TUTORIAL_BITS
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import AdminCatalog, CommandRegistry, RouteRegistry, ViewRegistry, discover
from aigis_ps.admin_security import API_ROOT
from aigis_ps.admin_views import PROJECTION_NOTE
from aigis_ps.admin_views_settings import NO_COMMAND, READ_ONLY_FIELDS, ownership
from aigis_ps.assets import asset_key
from aigis_ps.background_catalog import UNKNOWN_NAME
from aigis_ps.background_state import background_response, read_backgrounds
from aigis_ps.bootstrap import TABLES, BootstrapService, configuration_updates
from aigis_ps.card_status import CARD_STATUS_FIELDS
from aigis_ps.config_state import CONFIGURATION_ALIASES, CONFIGURATION_FIELDS, SOUND_OPTION_FIELDS, patch_configuration
from aigis_ps.crystal_spending import initialize_spending
from aigis_ps.energy_clock import JST, instant
from aigis_ps.generation import GenerationRuntime
from aigis_ps.login_cooking_state import LEDGER_KEY as COOKING_LEDGER, initialize_cooking
from aigis_ps.login_reward_service import COOKING_MIGRATION, OFFLINE_COOKING_MIGRATION, REBASE_OPERATION
from aigis_ps.login_reward_state import LEDGER_KEY as LOGIN_LEDGER, initialize_login_rewards
from aigis_ps.login_stamp_pending import MAX_PENDING_STAMPS
from aigis_ps.mission_service import HISTORY_NAMESPACE as MISSION_HISTORY
from aigis_ps.mission_state import MissionProgress
from aigis_ps.peruse_state import peruse_response, read_perused
from aigis_ps.player_state import GIFT_TUTORIAL_FLAG
from aigis_ps.possessions import PossessionService
from aigis_ps.rank_reward_state import bonus_fingerprint, initialize_rank_rewards
from aigis_ps.skin_shop_catalog import SkinProduct
from aigis_ps.storage import Store
from aigis_ps.universal_flags import flag_hash, flags_response, read_flags
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_preflight import none_reasons
from test_admin_views_core import generation_for
from test_bootstrap import asset_root
from test_mission_catalog import mission
from test_mission_state import catalog as mission_catalog
from test_possessions import item_table
from test_quest_service import bundle


NOW = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)
NOW_JST = NOW.astimezone(JST)
SOUND_VALUES = {"A2": 0, "A3": 1, "C2": 30, "C3": 70, "F9": 1, "FA": 2, "FD": 0}
SKIN, HELD_SKIN, UNDEFINED_SKIN = "skin_console_0001", "skin_console_0002", "skin_console_0003"
COLLIDING = ("plumless", "buckeroo")
KNOWN_BACKGROUNDS = frozenset(("system_default", "room_a"))
SECRET = "do-not-leak-0123456789"
PORT, GAME_PORT, TOKEN = 18104, 18103, "s" * 43
OPERATIONS = ("backgrounds.merge", "config.damage_display.set", "config.sound.set", "flags.set", "missions.mark_read",
              "perused.set", "stories.set_unlock")
SOUND_FIELDS = frozenset(CONFIGURATION_ALIASES[wire] for wire in SOUND_OPTION_FIELDS)
EXPECTED_OWNERS = {**{f"config.{CONFIGURATION_ALIASES[wire]}": "config.sound.set" for wire in SOUND_OPTION_FIELDS},
                   f"config.{DAMAGE_DISPLAY_FIELD}": "config.damage_display.set",
                   "card_status.A7": "stories.set_unlock"}
# The ownership table every console package follows: each shared field has exactly one writing command.
PLANNED_OWNERS = {
    **{f"config.{CONFIGURATION_ALIASES[wire]}": {"config.sound.set"} for wire in SOUND_OPTION_FIELDS},
    **{f"config.{name}": {"config.sort.set"} for name in ("_FlagSort", "_FlagMissionFilter", "TypeOfSortOrder")},
    **{f"config.{name}": {"config.auto_select.set"} for name in ("AutoSelectConfig", "IntegrationAutoSelectConfig")},
    "config.DamageDispEnable": {"config.damage_display.set"}, "config._GuideUnitIndex": {"guide.set"},
    "config.HaveUnitMaxSizeBuy": {"barracks.grant_slot"}, "config.EventViewRequest": {"view_event.set", "view_event.clear"},
    "config._BattleGodBlessFilter": {"blessings.set"}, "card_status.A7": {"stories.set_unlock"},
    **{f"card_status.B{stage}": {"fame.register"} for stage in range(5)},
}
SETTINGS_VIEWS = ("settings.backgrounds", "settings.card_status", "settings.config", "settings.event_view",
                  "settings.flags", "settings.perused")
PROGRESS_VIEWS = ("progress.login_cooking", "progress.login_rewards", "progress.missions", "progress.rank_rewards")
SERVICE_UNAVAILABLE = "admin_view_service_unavailable"


def product(number, item_id, *, supported=True):
    name = f"PurchasedSkin_{number:04d}"
    return SkinProduct(number, number, number, number, flag_hash(name), name, 100, None, None, number, item_id, 7, supported)


PRODUCTS = (product(1, SKIN), product(2, HELD_SKIN), product(3, UNDEFINED_SKIN, supported=False))


class Backgrounds:
    def __init__(self):
        self.catalog, self.audited = self, []

    def audit(self, rows):
        self.audited.append(deepcopy(rows))
        return tuple({"CardID": row["CardID"], "ConfigID": row["ConfigID"], "kinds": [UNKNOWN_NAME]}
                     for row in rows if row["ConfigID"] not in KNOWN_BACKGROUNDS)

    def diagnose(self, state):
        return {"rows": len(read_backgrounds(state)), "findings": list(self.audit(read_backgrounds(state)))}


class Missions:
    def __init__(self, *rows, sequence=None):
        self.progress = MissionProgress(mission_catalog(rows, sequence=sequence))
        self.catalog = self.progress.catalog

    def initialize(self, state):
        return self.progress.initialize(state)


@pytest.fixture
def possessions(tmp_path):
    root = tmp_path / "items"
    root.mkdir()
    (root / asset_key("ItemConfigTable.atb")).write_bytes(item_table([(name, flag_hash(name), 1) for name in (SKIN, HELD_SKIN)]))
    return PossessionService(root)


@pytest.fixture
def bootstrap(asset_root, possessions):
    return BootstrapService(asset_root, clock=lambda: NOW_JST.replace(tzinfo=None), asset_version="a" * 40,
                            possession_provider=possessions.startup_response, peruse_provider=peruse_response,
                            universal_flag_provider=flags_response, background_provider=background_response)


@pytest.fixture
def state(bootstrap):
    return bootstrap.initial_state()


@pytest.fixture
def services(bootstrap, possessions):
    return SimpleNamespace(bootstrap=bootstrap, possessions=possessions, backgrounds=Backgrounds(),
                           skin_shop=SimpleNamespace(catalog=SimpleNamespace(products=PRODUCTS)),
                           missions=SimpleNamespace(initialize=deepcopy))


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_settings.register(commands)
    admin_cmd_stories.register(commands)
    return commands


def context(services, now=NOW):
    return CommandContext(now=now, acquisition_day=1, revision=0, generation_id="gen-settings", services=services)


def run(command, state, raw, services, now=NOW):
    outcome = Batch((BatchStep(command, command.parse(raw)),)).apply(state, context(services, now))
    return outcome.state, outcome.steps[0]


def refused(command, state, raw, services, now=NOW):
    with pytest.raises(AdminError) as caught:
        run(command, state, raw, services, now)
    return caught.value


def restart(services):
    def initialize(state):
        services.bootstrap.table_rows(state, "config")
        read_flags(state)
        read_perused(state)
        read_backgrounds(state)
        return deepcopy(state)
    return initialize


def pipeline(store, services, preflight):
    runtime = FakeRuntime(FakeGeneration(id="gen-settings", bundle=services, content_versions={}, clock=NOW))
    return CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(restart(services)), runtime,
                          backup_directory=Path("backups"))


def commit(console, commands, confirmations=()):
    plan = console.preview(registry().batch(commands))
    assert plan.committable, plan.describe()
    outcome = console.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=plan.expected_revision,
                             op_id=str(uuid.uuid4()), confirmations=confirmations)
    return plan, outcome


def flagged(state, rows):
    updated = deepcopy(state)
    updated["universal_flags_schema"] = 1
    updated["tables"]["universal_flags"] = [{"ID": name, "Value": value} for name, value in rows]
    return updated


def with_skins(state, counts, seen=None):
    updated = deepcopy(state)
    updated["tables"]["possessions"] = [{"ItemID": item, "Count": count} for item, count in counts.items()]
    if seen is not None:
        updated[SKIN_HISTORY] = {"schema_version": 1, "seen": sorted(seen)}
    return updated


def test_settings_commands_register_under_their_preflight_domains():
    commands = registry()
    assert [command.operation for command in commands] == list(OPERATIONS)
    assert {command.operation: sorted(command.touched) for command in commands} == {
        "backgrounds.merge": ["backgrounds"], "config.damage_display.set": ["config"], "config.sound.set": ["config"],
        "flags.set": ["universal_flags"], "missions.mark_read": ["missions"], "perused.set": ["perused"],
        "stories.set_unlock": ["card_status", "gifts"]}
    assert all(not command.preflight_none and command.touched <= PREFLIGHT_DOMAINS.keys() for command in commands)
    unread = {command.operation: sorted(none_reasons(command.touched)) for command in commands}
    assert {operation: domains for operation, domains in unread.items() if domains} == {"stories.set_unlock": ["gifts"]}
    assert {command.operation for command in commands if command.destructive_by_params} == {
        "flags.set", "stories.set_unlock"}
    assert not any(command.destructive is True for command in commands)
    assert {command.domain for command in commands} == {"settings", "progress"}
    with pytest.raises(ValueError):
        admin_cmd_settings.register(commands)


def test_each_sound_field_is_written_only_by_the_sound_command():
    assert dict(registry().field_owners()) == EXPECTED_OWNERS
    assert set(SoundParams.model_fields) == SOUND_OPTION_FIELDS
    assert SOUND.writes == SOUND_WRITES == {field for field, owner in EXPECTED_OWNERS.items() if owner == SOUND.operation}
    assert DAMAGE_DISPLAY.writes == DAMAGE_DISPLAY_WRITES == {
        field for field, owner in EXPECTED_OWNERS.items() if owner == DAMAGE_DISPLAY.operation}
    assert all(not command.writes for command in (FLAGS, PERUSED, BACKGROUNDS, MISSIONS_READ))


def test_discovered_commands_follow_the_shared_field_ownership_table():
    owners = discover(PREFLIGHT_DOMAINS).commands.field_owners()
    assert {field: owner for field, owner in owners.items() if field in EXPECTED_OWNERS} == EXPECTED_OWNERS
    assert not READ_ONLY_FIELDS.keys() & owners.keys()
    assert all(owner in PLANNED_OWNERS[field] for field, owner in owners.items() if field in PLANNED_OWNERS)
    # The field dictionary resolves owners by stored field names, so wire aliases would never show up there.
    assert all(field.partition(".")[2] in CONFIGURATION_FIELDS for field in owners if field.startswith("config."))
    assert all(field.partition(".")[2] in CARD_STATUS_FIELDS for field in owners if field.startswith("card_status."))


def test_sound_set_applies_the_native_21_22_kernel(services, state):
    updated, step = run(SOUND, state, SOUND_VALUES, services)
    assert updated == patch_configuration(state, configuration_updates(SOUND_VALUES))
    row = updated["tables"]["config"][0]
    assert (row["UseBgm"], row["UseSe"], row["_VolumeBgm"], row["_VolumeSe"]) == (False, True, 30, 70)
    assert {key: value for key, value in row.items() if key not in SOUND_FIELDS} == {
        key: value for key, value in state["tables"]["config"][0].items() if key not in SOUND_FIELDS}
    assert step.effects["fields"] == [
        {"field": "UseBgm", "wire": "A2", "before": True, "after": False},
        {"field": "_VolumeBgm", "wire": "C2", "before": 0, "after": 30},
        {"field": "_VolumeSe", "wire": "C3", "before": 0, "after": 70},
        {"field": "BattlePlaceMagnetFlag", "wire": "F9", "before": 0, "after": 1},
        {"field": "BattleAcceleratorFlag", "wire": "FA", "before": 0, "after": 2}]
    assert {key: value for key, value in updated.items() if key != "tables"} == {
        key: value for key, value in state.items() if key != "tables"}


@pytest.mark.parametrize("wire", ["C1", "A5", "F8", "FE", "A4", "F7", "F1", "F6", "UserName"])
def test_sound_set_refuses_fields_owned_by_other_commands(services, state, wire):
    error = refused(SOUND, state, {**SOUND_VALUES, wire: 1}, services)
    assert (error.code, error.status) == ("admin_params_invalid", 422)
    assert [(field["loc"], field["type"]) for field in error.fields] == [([wire], "extra_forbidden")]


@pytest.mark.parametrize("wire", sorted(SOUND_OPTION_FIELDS))
def test_sound_set_needs_the_complete_group(services, state, wire):
    error = refused(SOUND, state, {key: value for key, value in SOUND_VALUES.items() if key != wire}, services)
    assert (error.code, error.status) == ("admin_params_invalid", 422)
    assert [(field["loc"], field["type"]) for field in error.fields] == [([wire], "missing")]


@pytest.mark.parametrize("changes, code, status", [
    ({"A2": 2}, "invalid_configuration_request", 400),
    ({"A3": -1}, "invalid_configuration_request", 400),
    ({"C2": 2 ** 31}, "invalid_configuration_request", 400),
    ({"FD": -(2 ** 31) - 1}, "invalid_configuration_request", 400),
    ({"A2": True}, "admin_params_invalid", 422),
    ({"C3": "5"}, "admin_params_invalid", 422),
    ({"F9": 1.0}, "admin_params_invalid", 422),
])
def test_sound_values_follow_the_native_ranges(services, state, changes, code, status):
    error = refused(SOUND, state, {**SOUND_VALUES, **changes}, services)
    assert (error.code, error.status) == (code, status)


def test_sound_set_refuses_a_stored_row_the_native_reader_rejects(services, state):
    broken = deepcopy(state)
    broken["tables"]["config"][0]["UserName"] = "n" * 40
    assert refused(SOUND, broken, SOUND_VALUES, services).code == "invalid_bootstrap_value"


def test_sound_set_commits_through_preflight_and_restart_check(services, state, preflight):
    store = FakeStore(state)
    plan, outcome = commit(pipeline(store, services, preflight),
                           [{"operation": SOUND.operation, "params": SOUND_VALUES}])
    assert plan.confirmations == () and not requires_backup(plan.batch)
    assert outcome.result["readback"] == {"config": [36]} and outcome.result["backup_before_commit"] is None
    assert store.revision == 1 and store.state["tables"]["config"][0]["_VolumeSe"] == 70


def test_damage_display_set_writes_the_native_config_flag(services, state):
    assert state["tables"]["config"][0][DAMAGE_DISPLAY_FIELD] == 0
    updated, step = run(DAMAGE_DISPLAY, state, {"enabled": 1}, services)
    assert updated == patch_configuration(state, {DAMAGE_DISPLAY_FIELD: 1})
    assert step.effects == {"fields": [{"field": DAMAGE_DISPLAY_FIELD, "wire": "F7", "before": 0, "after": 1}]}
    assert {key: value for key, value in updated["tables"]["config"][0].items() if key != DAMAGE_DISPLAY_FIELD} == {
        key: value for key, value in state["tables"]["config"][0].items() if key != DAMAGE_DISPLAY_FIELD}
    unchanged, step = run(DAMAGE_DISPLAY, updated, {"enabled": 1}, services)
    assert unchanged == updated and step.effects == {"fields": []}
    back, step = run(DAMAGE_DISPLAY, updated, {"enabled": 0}, services)
    assert back["tables"]["config"][0][DAMAGE_DISPLAY_FIELD] == 0
    assert step.effects["fields"] == [{"field": DAMAGE_DISPLAY_FIELD, "wire": "F7", "before": 1, "after": 0}]


@pytest.mark.parametrize("enabled, code, status", [
    (2, "quest_request_invalid", 400),
    (-1, "quest_request_invalid", 400),
    (2 ** 31, "quest_request_invalid", 400),
    (True, "admin_params_invalid", 422),
    ("1", "admin_params_invalid", 422),
    (1.0, "admin_params_invalid", 422),
    (None, "admin_params_invalid", 422),
])
def test_damage_display_set_only_accepts_the_native_zero_or_one(services, state, enabled, code, status):
    error = refused(DAMAGE_DISPLAY, state, {"enabled": enabled}, services)
    assert (error.code, error.status) == (code, status)
    error = refused(DAMAGE_DISPLAY, state, {}, services)
    assert (error.code, error.status) == ("admin_params_invalid", 422)
    error = refused(DAMAGE_DISPLAY, state, {"enabled": 1, "DamageDispEnable": 1}, services)
    assert (error.code, error.status) == ("admin_params_invalid", 422)


def test_damage_display_set_commits_without_a_phrase_through_the_config_readback(services, state, preflight):
    store = FakeStore(state)
    plan, outcome = commit(pipeline(store, services, preflight),
                           [{"operation": DAMAGE_DISPLAY.operation, "params": {"enabled": 1}}])
    assert plan.confirmations == () and not requires_backup(plan.batch)
    assert outcome.result["readback"] == {"config": [36]} and len(store.backups) == 0
    assert store.revision == 1 and store.state["tables"]["config"][0][DAMAGE_DISPLAY_FIELD] == 1


def test_flags_set_merges_values_and_keeps_other_flags(services, state):
    stored = flagged(state, [("Tutorial", 1), ("Other", 7)])
    updated, step = run(FLAGS, stored, {"flags": [{"ID": "Tutorial", "Value": 0}, {"ID": "New", "Value": -5}]}, services)
    assert read_flags(updated) == [{"ID": "Tutorial", "Value": 0}, {"ID": "Other", "Value": 7}, {"ID": "New", "Value": -5}]
    assert step.effects == {"flags": [{"ID": "Tutorial", "before": 1, "after": 0}, {"ID": "New", "before": None, "after": -5}],
                            "purchase_flags": []}
    assert step.warnings == ()


def test_flags_set_starts_the_flag_schema_on_an_empty_save(services, state):
    updated, _ = run(FLAGS, state, {"flags": [{"ID": "First", "Value": 1}]}, services)
    assert updated["universal_flags_schema"] == 1 and read_flags(updated) == [{"ID": "First", "Value": 1}]


def test_flag_name_hash_collisions_return_409(services, state):
    assert flag_hash(COLLIDING[0]) == flag_hash(COLLIDING[1])
    error = refused(FLAGS, flagged(state, [(COLLIDING[0], 1)]), {"flags": [{"ID": COLLIDING[1], "Value": 2}]}, services)
    assert (error.code, error.status) == ("universal_flag_name_conflict", 409)
    error = refused(FLAGS, state, {"flags": [{"ID": name, "Value": 1} for name in COLLIDING]}, services)
    assert (error.code, error.status) == ("universal_flag_name_conflict", 409)


@pytest.mark.parametrize("raw, code, status", [
    ({"flags": [{"ID": "A", "Value": 1}, {"ID": "A", "Value": 2}]}, "admin_params_invalid", 422),
    ({"flags": []}, "admin_params_invalid", 422),
    ({"flags": [{"ID": "A", "Value": True}]}, "admin_params_invalid", 422),
    ({"flags": [{"ID": "A", "Value": "1"}]}, "admin_params_invalid", 422),
    ({"flags": [{"ID": "A"}]}, "admin_params_invalid", 422),
    ({"flags": [{"ID": "A", "Value": 1}], "purchase_flag_ack": 1}, "admin_params_invalid", 422),
    ({"flags": [{"ID": "A", "Value": 2 ** 31}]}, "universal_flag_updates_invalid", 400),
    ({"flags": [{"ID": "", "Value": 1}]}, "universal_flag_updates_invalid", 400),
    ({"flags": [{"ID": "x" * 256, "Value": 1}]}, "universal_flag_updates_invalid", 400),
])
def test_flag_updates_are_validated_by_shape_and_by_the_native_kernel(services, state, raw, code, status):
    error = refused(FLAGS, state, raw, services)
    assert (error.code, error.status) == (code, status)


def test_purchase_flags_need_an_explicit_acknowledgement(services, state):
    owned = with_skins(state, {HELD_SKIN: 1}, seen={HELD_SKIN})
    raw = {"flags": [{"ID": "Unrelated", "Value": 1}, {"ID": PRODUCTS[1].flag_name, "Value": 0}]}
    error = refused(FLAGS, owned, raw, services)
    assert (error.code, error.status) == (PURCHASE_FLAG_ACK_REQUIRED, 422)
    assert [field["loc"] for field in error.fields] == [[PURCHASE_FLAG_ACK]]
    assert error.details["purchase_flags"] == [
        {"ID": "PurchasedSkin_0002", "before": None, "after": 0, "shop_id": 2, "item_id": HELD_SKIN, "supported": True,
         "holding": 1, "acquisition_recorded": True}]
    assert error.details["step"] == 0 and error.details["operation"] == FLAGS.operation


def test_acknowledged_purchase_flags_report_holdings_and_history_without_touching_them(services, state):
    owned = with_skins(flagged(state, [(PRODUCTS[0].flag_name, 0)]), {HELD_SKIN: 1}, seen=())
    raw = {"flags": [{"ID": product.flag_name, "Value": 1} for product in PRODUCTS], PURCHASE_FLAG_ACK: True}
    updated, step = run(FLAGS, owned, raw, services)
    assert step.effects["purchase_flags"] == [
        {"ID": "PurchasedSkin_0001", "before": 0, "after": 1, "shop_id": 1, "item_id": SKIN, "supported": True,
         "holding": 0, "acquisition_recorded": False},
        {"ID": "PurchasedSkin_0002", "before": None, "after": 1, "shop_id": 2, "item_id": HELD_SKIN, "supported": True,
         "holding": 1, "acquisition_recorded": False},
        {"ID": "PurchasedSkin_0003", "before": None, "after": 1, "shop_id": 3, "item_id": UNDEFINED_SKIN,
         "supported": False, "holding": None, "acquisition_recorded": False}]
    assert step.warnings == (
        f"PurchasedSkin_0001 marks shop product 1 as purchased while {SKIN} is not held",
        f"PurchasedSkin_0003 marks shop product 3 as purchased while {UNDEFINED_SKIN} is not held")
    assert updated["tables"]["possessions"] == owned["tables"]["possessions"]
    assert updated[SKIN_HISTORY] == owned[SKIN_HISTORY]


def test_purchase_flags_are_recognised_by_their_native_hash(services, state):
    services.skin_shop.catalog.products = (replace(product(9, SKIN), purchase_flag=flag_hash(COLLIDING[0])),)
    error = refused(FLAGS, state, {"flags": [{"ID": COLLIDING[1], "Value": 1}]}, services)
    assert error.code == PURCHASE_FLAG_ACK_REQUIRED and error.details["purchase_flags"][0]["shop_id"] == 9


def test_acquisition_history_is_unknown_without_the_mission_service(services, state):
    services.missions = None
    _, step = run(FLAGS, state, {"flags": [{"ID": PRODUCTS[0].flag_name, "Value": 0}], PURCHASE_FLAG_ACK: True}, services)
    assert step.effects["purchase_flags"][0]["acquisition_recorded"] is None


def test_flags_set_is_destructive_only_with_the_purchase_ack_and_needs_the_skin_shop_catalog(services):
    assert FLAGS.destructive_by_params
    assert FLAGS.is_destructive(FLAGS.parse({"flags": [{"ID": "A", "Value": 1}]})) is False
    assert FLAGS.is_destructive(FLAGS.parse({"flags": [{"ID": "A", "Value": 1}], PURCHASE_FLAG_ACK: True})) is True
    assert FLAGS.phrase(FLAGS.parse({"flags": [{"ID": "A", "Value": 1}]})) == "SET 1 FLAG"
    assert FLAGS.phrase(FLAGS.parse({"flags": [{"ID": "A", "Value": 1}, {"ID": "B", "Value": 1}],
                                     PURCHASE_FLAG_ACK: True})) == "SET 2 PURCHASE FLAGS"
    assert FLAGS.availability(services) is None
    assert FLAGS.availability(SimpleNamespace()) == SKIN_SHOP_REASON
    assert (counted(1, "CARD"), counted(3, "CARD")) == ("1 CARD", "3 CARDS")


def test_flags_set_needs_a_phrase_and_a_backup_only_when_it_acknowledges_a_purchase(services, state, preflight):
    store = FakeStore(state)
    console = pipeline(store, services, preflight)
    plain = [{"operation": FLAGS.operation, "params": {"flags": [{"ID": "Tutorial", "Value": 3}]}}]
    plan, outcome = commit(console, plain)
    assert plan.confirmations == () and not requires_backup(plan.batch)
    assert outcome.result["readback"] == {"universal_flags": [137]} and outcome.result["backup_before_commit"] is None
    assert len(store.backups) == 0 and read_flags(store.state) == [{"ID": "Tutorial", "Value": 3}]
    purchase = [{"operation": FLAGS.operation,
                 "params": {"flags": [{"ID": PRODUCTS[0].flag_name, "Value": 1}], PURCHASE_FLAG_ACK: True}}]
    with pytest.raises(AdminError) as caught:
        commit(console, purchase)
    assert caught.value.code == "admin_confirmation_required"
    plan, outcome = commit(console, purchase, ("SET 1 PURCHASE FLAG",))
    assert [item.phrase for item in plan.confirmations] == ["SET 1 PURCHASE FLAG"]
    assert outcome.result["readback"] == {"universal_flags": [137]}
    assert len(store.backups) == 1 and store.backups[0]["commits_before"] == 1
    assert {row["ID"]: row["Value"] for row in read_flags(store.state)} == {"Tutorial": 3, PRODUCTS[0].flag_name: 1}


def test_perused_set_writes_explicit_flags_and_keeps_cleared_rows(services, state):
    stored = deepcopy(state)
    stored["perused_events_schema"] = 1
    stored["tables"]["perused_events"] = [{"ID": "event_a", "FLG": 1}, {"ID": "event_b", "FLG": 3}]
    updated, step = run(PERUSED, stored, {"events": [{"ID": "event_a", "FLG": 0}, {"ID": "event_c", "FLG": 1}]}, services)
    assert read_perused(updated) == [{"ID": "event_a", "FLG": 0}, {"ID": "event_b", "FLG": 3}, {"ID": "event_c", "FLG": 1}]
    assert step.effects == {"events": [{"ID": "event_a", "before": 1, "after": 0},
                                       {"ID": "event_c", "before": None, "after": 1}]}


@pytest.mark.parametrize("raw, code, status", [
    ({"events": [{"ID": "event_a"}]}, "admin_params_invalid", 422),
    ({"events": [{"ID": "event_a", "FLG": None}]}, "admin_params_invalid", 422),
    ({"events": [{"ID": "event_a", "FLG": False}]}, "admin_params_invalid", 422),
    ({"events": [{"ID": "event_a", "FLG": 0}, {"ID": "event_a", "FLG": 1}]}, "admin_params_invalid", 422),
    ({"events": []}, "admin_params_invalid", 422),
    ({"events": [{"ID": "", "FLG": 1}]}, "peruse_updates_invalid", 400),
    ({"events": [{"ID": "event_a", "FLG": 2 ** 31}]}, "peruse_updates_invalid", 400),
    ({"events": [{"ID": name, "FLG": 1} for name in COLLIDING]}, "peruse_name_conflict", 409),
])
def test_perused_updates_need_explicit_valid_flags(services, state, raw, code, status):
    error = refused(PERUSED, state, raw, services)
    assert (error.code, error.status) == (code, status)


def test_perused_set_commits_through_the_startup_read(services, state, preflight):
    store = FakeStore(state)
    _, outcome = commit(pipeline(store, services, preflight),
                        [{"operation": PERUSED.operation, "params": {"events": [{"ID": "event_a", "FLG": 1}]}}])
    assert outcome.result["readback"] == {"perused": [137]} and store.revision == 1


def secret_response():
    return f'<?xml version="1.0"?><DA><SYSTEM><A1 T="S"><V>ExchangeKey</V></A1><A2 T="S"><V>{SECRET}</V></A2></SYSTEM></DA>'


def save_time(moment):
    return moment.astimezone(JST).strftime(SAVE_TIME_FORMAT)


def test_backgrounds_merge_replaces_and_adds_rows_with_explicit_times(services, state):
    earlier = save_time(NOW - timedelta(days=1))
    stored = deepcopy(state)
    stored["backgrounds_schema"] = 1
    stored["tables"]["backgrounds"] = [{"CardID": 0, "ConfigID": "room_a", "SaveTime": earlier},
                                       {"CardID": 9, "ConfigID": "room_a", "SaveTime": earlier}]
    rows = [{"CardID": 0, "ConfigID": "system_default", "SaveTime": save_time(NOW)},
            {"CardID": 5, "ConfigID": "room_a", "SaveTime": earlier}]
    updated, step = run(BACKGROUNDS, stored, {"rows": rows}, services)
    assert read_backgrounds(updated) == [rows[0], stored["tables"]["backgrounds"][1], rows[1]]
    assert step.effects == {"rows": [{"CardID": 0, "before": stored["tables"]["backgrounds"][0], "after": rows[0]},
                                     {"CardID": 5, "before": None, "after": rows[1]}]}
    assert services.backgrounds.audited == [rows]


@pytest.mark.parametrize("row, code, status", [
    ({"CardID": 0, "ConfigID": "room_a"}, "admin_params_invalid", 422),
    ({"CardID": 0, "ConfigID": "room_a", "SaveTime": 1}, "admin_params_invalid", 422),
    ({"CardID": 0, "ConfigID": "room_a", "SaveTime": "2026-11-03 12:00:00"}, "background_updates_invalid", 400),
    ({"CardID": 0, "ConfigID": "room_a", "SaveTime": "2026/02/30 12:00:00"}, "background_updates_invalid", 400),
    ({"CardID": -1, "ConfigID": "room_a", "SaveTime": "2026/11/03 12:00:00"}, "background_updates_invalid", 400),
    ({"CardID": 0, "ConfigID": "", "SaveTime": "2026/11/03 12:00:00"}, "background_updates_invalid", 400),
])
def test_background_rows_need_an_explicit_native_save_time(services, state, row, code, status):
    error = refused(BACKGROUNDS, state, {"rows": [row]}, services)
    assert (error.code, error.status) == (code, status)


def test_background_rows_must_pass_the_catalog_audit(services, state):
    rows = [{"CardID": 0, "ConfigID": "room_a", "SaveTime": save_time(NOW)},
            {"CardID": 3, "ConfigID": "page_only", "SaveTime": save_time(NOW)}]
    error = refused(BACKGROUNDS, state, {"rows": rows}, services)
    assert (error.code, error.status) == (BACKGROUND_ROWS_UNSUPPORTED, 409)
    assert error.details["findings"] == [{"CardID": 3, "ConfigID": "page_only", "kinds": [UNKNOWN_NAME]}]


def test_background_save_times_cannot_lock_the_native_menu(services, state):
    rows = [{"CardID": 0, "ConfigID": "room_a", "SaveTime": save_time(NOW + timedelta(seconds=1))}]
    error = refused(BACKGROUNDS, state, {"rows": rows}, services)
    assert (error.code, error.status) == (BACKGROUND_SAVE_TIME_FUTURE, 409)
    assert error.details["now"] == save_time(NOW) and error.details["card_ids"] == [0]
    duplicate = [{"CardID": 0, "ConfigID": "room_a", "SaveTime": save_time(NOW)}] * 2
    assert refused(BACKGROUNDS, state, {"rows": duplicate}, services).code == "background_updates_invalid"


def test_backgrounds_merge_needs_the_background_service(services, state):
    assert BACKGROUNDS.availability(services) is None
    assert BACKGROUNDS.availability(SimpleNamespace()) == missing_service("backgrounds")
    services.backgrounds = None
    rows = [{"CardID": 0, "ConfigID": "room_a", "SaveTime": save_time(NOW)}]
    assert (refused(BACKGROUNDS, state, {"rows": rows}, services).status) == 503


def test_backgrounds_merge_commits_through_the_startup_read(services, state, preflight):
    store = FakeStore(state)
    rows = [{"CardID": 0, "ConfigID": "system_default", "SaveTime": save_time(NOW)}]
    _, outcome = commit(pipeline(store, services, preflight),
                        [{"operation": BACKGROUNDS.operation, "params": {"rows": rows}}])
    assert outcome.result["readback"] == {"backgrounds": [137]} and read_backgrounds(store.state) == rows


def login_missions():
    return Missions(mission(1, ConditionType=1), mission(2, ConditionType=1), mission(3, ConditionType=4))


def logged_in(missions, state):
    return missions.progress.record(state, "login", {}, NOW)


def snapshot(missions, state):
    return {row["ID"]: row for row in missions.progress.snapshot(state, NOW)}


def test_mark_read_marks_finished_unread_missions_without_receipts(services, state):
    services.missions = login_missions()
    progressed = logged_in(services.missions, state)
    updated, step = run(MISSIONS_READ, progressed, {"ids": [1]}, services)
    rows = snapshot(services.missions, updated)
    assert (rows[1]["Read"], rows[2]["Read"]) == (1, 0)
    assert step.effects == {"marked": [1], "skipped": []} and step.warnings == ()
    assert MISSION_HISTORY not in updated
    assert updated == services.missions.progress.mark_read(progressed, [1], NOW)


def test_mark_read_also_marks_claimed_unread_missions_like_request_60(services, state):
    services.missions = login_missions()
    claimed = services.missions.progress.claim(logged_in(services.missions, state), [1], NOW)
    assert snapshot(services.missions, claimed)[1] == {"ID": 1, "Status": CLAIMED, "Read": 0, "Progress": 1}
    updated, step = run(MISSIONS_READ, claimed, {"ids": [1]}, services)
    assert step.effects == {"marked": [1], "skipped": []} and step.warnings == ()
    assert snapshot(services.missions, updated)[1]["Read"] == 1
    assert updated == services.missions.progress.mark_read(claimed, [1], NOW)


@pytest.mark.parametrize("status, read, expected", [
    (0, 0, False), (FINISHED, 0, True), (CLAIMED, 0, True), (FINISHED, 1, False), (CLAIMED, 1, False),
])
def test_unread_follows_the_rows_request_60_marks(status, read, expected):
    assert unread({"ID": 1, "Status": status, "Read": read, "Progress": 1}) is expected


def test_mark_read_skips_unfinished_or_read_missions_and_then_saves_nothing(services, state):
    services.missions = login_missions()
    progressed = logged_in(services.missions, state)
    once, _ = run(MISSIONS_READ, progressed, {"ids": [1]}, services)
    again, step = run(MISSIONS_READ, once, {"ids": [1, 3]}, services)
    assert again == once
    assert step.effects == {"marked": [], "skipped": [{"ID": 1, "Status": 1, "Read": 1}, {"ID": 3, "Status": 0, "Read": 0}]}
    assert step.warnings == (NOTHING_MARKED,)
    mixed, step = run(MISSIONS_READ, progressed, {"ids": [3, 2]}, services)
    assert step.effects["marked"] == [2] and step.warnings == (NOTHING_MARKED,) and snapshot(services.missions, mixed)[2]["Read"]


def test_mark_read_leaves_locked_sequential_steps_unread_like_request_60(services, state):
    steps = (mission(10001, PeriodType=100, ConditionType=1, DispTabType=0),
             mission(10002, PeriodType=100, ConditionType=1, DispTabType=0))
    progressed = logged_in(Missions(mission(1, ConditionType=1), *steps, sequence=[10002, 10001]), state)
    # Moving 10001 to the front locks the finished 10002, which the snapshot then shows as unfinished.
    services.missions = Missions(mission(1, ConditionType=1), *steps, sequence=[10001, 10002])
    assert snapshot(services.missions, progressed)[10002] == {"ID": 10002, "Status": 0, "Read": 0, "Progress": 0}
    updated, step = run(MISSIONS_READ, progressed, {"ids": [1, 10002]}, services)
    assert step.effects == {"marked": [1], "skipped": [{"ID": 10002, "Status": 0, "Read": 0}]}
    assert updated == services.missions.progress.mark_read(progressed, [1], NOW)
    entry = next(entry for entry in updated["mission_state"]["entries"] if entry["condition_id"] == 10002)
    assert (entry["status"], entry["read"]) == (1, False)


def test_mark_read_that_marks_nothing_keeps_the_unprepared_save(services, state):
    services.missions = login_missions()
    updated, step = run(MISSIONS_READ, state, {"ids": [3]}, services)
    assert updated == state and "mission_state" not in updated
    assert step.effects == {"marked": [], "skipped": [{"ID": 3, "Status": 0, "Read": 0}]}


@pytest.mark.parametrize("ids, code, status", [
    ([999], "mission_inactive", 409),
    ([0], "mission_selection_invalid", 400),
    ([1, 1], "mission_selection_invalid", 400),
    ([], "admin_params_invalid", 422),
    ([True], "admin_params_invalid", 422),
])
def test_mark_read_ids_are_checked_by_the_mission_kernel(services, state, ids, code, status):
    services.missions = login_missions()
    error = refused(MISSIONS_READ, logged_in(services.missions, state), {"ids": ids}, services)
    assert (error.code, error.status) == (code, status)


def test_mark_read_needs_the_mission_service(services):
    assert MISSIONS_READ.availability(services) is None
    assert MISSIONS_READ.availability(SimpleNamespace(missions=None)) == missing_service("missions")


def view_registry():
    views = ViewRegistry()
    admin_views_settings.register(views)
    admin_views_progress.register(views)
    return views


def read_view(name, state, services, params=None, now=NOW):
    view = view_registry().get(name)
    parsed = None if view.params_model is None else view.params_model(**(params or {}))
    request = SimpleNamespace(services=SimpleNamespace(catalog=SimpleNamespace(commands=registry())),
                              generation=SimpleNamespace(bundle=services), state=state, now=now, revision=0)
    return view.read(request, parsed)


def test_settings_and_progress_views_register_redacted_under_dotted_names():
    views = view_registry()
    assert [view.name for view in views] == sorted(PROGRESS_VIEWS + SETTINGS_VIEWS)
    assert {view.domain for view in views} == {"settings", "progress"}
    assert [view.name for view in views if view.live] == ["progress.missions"]
    assert all(callable(getattr(view.read, "__wrapped__", None)) for view in views)


def test_read_only_policy_outranks_any_owner():
    assert ownership("config.MenuTutorialFlags", {"config.MenuTutorialFlags": "config.tutorial.set"}) == {
        "owner": "config.tutorial.set", "read_only": True, "reason": READ_ONLY_FIELDS["config.MenuTutorialFlags"]}
    assert ownership("config.UseSe", {"config.UseSe": "config.sound.set"}) == {
        "owner": "config.sound.set", "read_only": False, "reason": None}
    assert ownership("config.UseSe", {}) == {"owner": None, "read_only": True, "reason": NO_COMMAND}


def test_config_view_marks_owners_and_read_only_fields(services, state):
    configured = deepcopy(state)
    configured["tables"]["config"][0]["MenuTutorialFlags"] = 0b1011
    result = read_view("settings.config", configured, services)
    fields = result["fields"]
    assert result["ok"] and list(fields) == [name for name, _, _ in TABLES["config"].fields]
    sound = fields["UseBgm"]
    assert (sound["wire"], sound["value"], sound["owner"], sound["read_only"], sound["reason"]) == (
        "A2", True, "config.sound.set", False, None)
    tutorial = fields["MenuTutorialFlags"]
    assert (tutorial["owner"], tutorial["read_only"]) == (None, True)
    assert tutorial["reason"] == READ_ONLY_FIELDS["config.MenuTutorialFlags"]
    assert BIT_TABLES["config.MenuTutorialFlags"] is TUTORIAL_BITS
    assert [bit.mask for bit in TUTORIAL_BITS.bits if bit.name == "gift_tutorial"] == [GIFT_TUTORIAL_FLAG]
    assert tutorial["bits"] == TUTORIAL_BITS.decode(0b1011) == [
        {"bit": 0, "name": "gift_tutorial", "level": "verified", "set": True},
        {"bit": 1, "name": "squad_tutorial", "level": "verified", "set": True},
        {"bit": 2, "name": "squad_tutorial", "level": "verified", "set": False},
        {"bit": 3, "name": None, "level": "unknown", "set": True}]
    assert fields["UserName"]["read_only"] and fields["UserName"]["reason"] == READ_ONLY_FIELDS["config.UserName"]
    assert (fields[DAMAGE_DISPLAY_FIELD]["owner"], fields[DAMAGE_DISPLAY_FIELD]["read_only"],
            fields[DAMAGE_DISPLAY_FIELD]["reason"]) == (DAMAGE_DISPLAY.operation, False, None)
    assert (fields["AutoSelectConfig"]["read_only"], fields["AutoSelectConfig"]["reason"]) == (True, NO_COMMAND)
    broken = deepcopy(state)
    del broken["tables"]["config"]
    assert read_view("settings.config", broken, services)["ok"] is False


@pytest.mark.parametrize("value, request_view", [
    ("", None),
    ("CardId=7,Id=2,Mode=1,Male=0", {"ok": True, "fields": {"CardId": 7, "Id": 2, "Mode": 1, "Male": 0}}),
    ("CardId=7", {"ok": False, "error": {"code": "view_event_request_invalid",
                                         "message": "Event view request does not match its native format"}}),
])
def test_event_view_pointer_is_shown_read_only(services, state, value, request_view):
    pointed = deepcopy(state)
    pointed["tables"]["config"][0]["EventViewRequest"] = value
    assert read_view("settings.event_view", pointed, services) == {
        "ok": True, "value": value, "request": request_view, "owner": None, "read_only": True, "reason": NO_COMMAND}


def test_flags_view_pages_flags_and_lists_every_purchase_flag(services, state):
    stored = with_skins(flagged(state, [("Tutorial", 1), (PRODUCTS[1].flag_name, 1)]), {HELD_SKIN: 1}, seen={HELD_SKIN})
    result = read_view("settings.flags", stored, services, {"offset": 1, "limit": 1})
    assert result["flags"] == {"ok": True, "items": [{"ID": "PurchasedSkin_0002", "Value": 1,
                                                       "hash": flag_hash("PurchasedSkin_0002")}],
                               "total": 2, "offset": 1, "limit": 1, "next_offset": None}
    assert result["purchases"] == {"ok": True, "products": [
        {"flag": "PurchasedSkin_0001", "stored": False, "value": 0, "shop_id": 1, "item_id": SKIN, "supported": True,
         "holding": 0, "acquisition_recorded": False},
        {"flag": "PurchasedSkin_0002", "stored": True, "value": 1, "shop_id": 2, "item_id": HELD_SKIN, "supported": True,
         "holding": 1, "acquisition_recorded": True},
        {"flag": "PurchasedSkin_0003", "stored": False, "value": 0, "shop_id": 3, "item_id": UNDEFINED_SKIN,
         "supported": False, "holding": None, "acquisition_recorded": False}]}
    services.skin_shop = None
    assert read_view("settings.flags", stored, services)["purchases"]["error"]["code"] == SERVICE_UNAVAILABLE


def test_flags_view_names_a_purchase_flag_by_the_colliding_name_the_save_stores(services, state):
    services.skin_shop.catalog.products = (replace(product(9, SKIN), purchase_flag=flag_hash(COLLIDING[0]),
                                                   flag_name=COLLIDING[0]),)
    stored = flagged(state, [(COLLIDING[1], 1)])
    listing = read_view("settings.flags", stored, services)["purchases"]["products"]
    assert [(row["flag"], row["stored"], row["value"]) for row in listing] == [(COLLIDING[1], True, 1)]
    error = refused(FLAGS, stored, {"flags": [{"ID": COLLIDING[0], "Value": 0}], "purchase_flag_ack": True}, services)
    assert (error.code, error.status) == ("universal_flag_name_conflict", 409)
    updated, step = run(FLAGS, stored, {"flags": [{"ID": listing[0]["flag"], "Value": 0}], "purchase_flag_ack": True},
                        services)
    assert read_flags(updated) == [{"ID": COLLIDING[1], "Value": 0}]
    assert [entry["shop_id"] for entry in step.effects["purchase_flags"]] == [9]


def test_perused_and_background_views_page_their_collections(services, state):
    stored = deepcopy(state)
    stored.update(perused_events_schema=1, backgrounds_schema=1)
    stored["tables"]["perused_events"] = [{"ID": f"event_{index}", "FLG": index} for index in range(3)]
    stored["tables"]["backgrounds"] = [{"CardID": 0, "ConfigID": "lost_room", "SaveTime": save_time(NOW)}]
    events = read_view("settings.perused", stored, services, {"limit": 2})["events"]
    assert (events["items"], events["total"], events["next_offset"]) == (stored["tables"]["perused_events"][:2], 3, 2)
    backgrounds = read_view("settings.backgrounds", stored, services)
    assert backgrounds["rows"]["items"] == stored["tables"]["backgrounds"]
    assert backgrounds["diagnostics"] == {"ok": True, "rows": 1, "findings": [
        {"CardID": 0, "ConfigID": "lost_room", "kinds": [UNKNOWN_NAME]}]}


def test_mission_view_projects_the_snapshot_and_redacts_receipts(services, state):
    services.missions = login_missions()
    progressed = logged_in(services.missions, state)
    progressed[MISSION_HISTORY] = {"schema_version": 2, "receipts": [{"response": secret_response(), "ids": [1]}],
                                   "processed_keys": []}
    result = read_view("progress.missions", progressed, services, {"limit": 3})
    missions = result["missions"]
    assert (missions["projected"], missions["persisted"], missions["note"]) == (True, False, PROJECTION_NOTE)
    assert missions["as_of"] == {"utc": NOW.isoformat(), "jst": NOW_JST.isoformat()}
    assert missions["items"][:2] == [
        {"ID": 1, "Status": 1, "Read": 0, "Progress": 1, "period_type": 1, "tab": 1, "claimable": True, "unread": True},
        {"ID": 2, "Status": 1, "Read": 0, "Progress": 1, "period_type": 1, "tab": 1, "claimable": True, "unread": True}]
    unfinished = missions["items"][2]
    assert (unfinished["ID"], unfinished["Status"], unfinished["Read"]) == (3, 0, 0)
    assert (unfinished["claimable"], unfinished["unread"]) == (False, False)
    receipt = result["history"]["receipts"][0]
    assert receipt["ids"] == [1] and receipt["response"]["redacted"] == "response" and SECRET not in repr(result)
    services.missions = None
    assert read_view("progress.missions", state, services)["missions"]["ok"] is False


def bonus(bonus_id, rank):
    return SimpleNamespace(bonus_id=bonus_id, rank=rank, incentive_id=1, reward_main_type=2, reward_sub_type=0,
                           parameter=100, item_id="")


def test_rank_reward_view_splits_pending_and_delivered_against_current_definitions(services, state):
    ranked = initialize_rank_rewards(state)
    ranked["tables"]["game_param"][0]["Rank"] = 3
    ranked["rank_rewards"].update(observed_rank=3, entries={
        "12": {"rank": 3, "definition": "0" * 64, "status": "delivered", "mail_ids": [5], "reason": None},
        "11": {"rank": 2, "definition": bonus_fingerprint(bonus(11, 2)), "status": "pending", "mail_ids": [],
               "reason": "present_capacity"}})
    services.rank_rewards = SimpleNamespace(catalog=SimpleNamespace(by_id={11: bonus(11, 2), 12: bonus(12, 3)}))
    result = read_view("progress.rank_rewards", ranked, services)
    assert (result["ok"], result["baseline_rank"], result["observed_rank"], result["definitions_checked"]) == (True, 1, 3, True)
    assert result["pending"] == [{"bonus_id": 11, **ranked["rank_rewards"]["entries"]["11"], "definition_current": True}]
    assert result["delivered"] == [{"bonus_id": 12, **ranked["rank_rewards"]["entries"]["12"], "definition_current": False}]
    services.rank_rewards = None
    unchecked = read_view("progress.rank_rewards", ranked, services)
    assert not unchecked["definitions_checked"] and unchecked["pending"][0]["definition_current"] is None
    assert read_view("progress.rank_rewards", state, services)["error"]["code"] == "rank_rewards_required"


def test_login_views_show_the_ledgers_with_their_clocks(services, state):
    login = initialize_login_rewards(initialize_spending(state, NOW, origin="new_local"), NOW, new_account=True)
    cooking = initialize_cooking(login, border=0)
    rewards = read_view("progress.login_rewards", cooking, services)
    assert rewards["ledger"]["ledger"] == cooking["login_rewards"]
    assert rewards["ledger"]["last_time_utc"] == NOW.isoformat()
    assert rewards["pending_stamps"] == {"ok": True, "entries": []}
    result = read_view("progress.login_cooking", cooking, services)
    assert (result["ok"], result["ledger"], result["last_time_utc"]) == (True, cooking["login_cooking"], NOW.isoformat())
    assert read_view("progress.login_cooking", state, services)["error"]["code"] == "login_cooking_required"
    checks = [rewards["rules"], rewards["stamp_capacity"], result["rules"]]
    assert [(check["ok"], check["error"]["code"]) for check in checks] == [(False, SERVICE_UNAVAILABLE)] * len(checks)


def test_login_views_check_the_saved_rules_against_the_running_generation(bundle):
    rewards = bundle.login_rewards
    state = bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))
    current = read_view("progress.login_rewards", state, bundle)
    assert current["rules"] == {"ok": True, "saved_total": rewards.beginner_total,
                                "current_total": rewards.beginner_total, "changed": False}
    assert current["stamp_capacity"] == {"ok": True, "count": 0, "capacity": MAX_PENDING_STAMPS,
                                         "remaining": MAX_PENDING_STAMPS, "warning": False}
    assert read_view("progress.login_cooking", state, bundle)["rules"] == {
        "ok": True, "saved_border": rewards.cooking_border, "current_border": rewards.cooking_border, "changed": False}
    changed = deepcopy(state)
    changed[LOGIN_LEDGER]["beginner_total"] = rewards.beginner_total + 1
    changed[COOKING_LEDGER]["border"] = rewards.cooking_border + 1
    login = read_view("progress.login_rewards", changed, bundle)
    assert login["ledger"]["ok"] and login["rules"] == {"ok": True, **rewards.beginner_rules(changed)}
    assert (login["rules"]["changed"], login["rules"]["repair"], login["rules"]["refused_operations"]) == (
        True, REBASE_OPERATION, [115])
    cooking = read_view("progress.login_cooking", changed, bundle)
    assert cooking["ok"] and cooking["ledger"] == changed[COOKING_LEDGER]
    assert cooking["rules"] == {"ok": True, **rewards.cooking_rules(changed)}
    assert (cooking["rules"]["changed"], cooking["rules"]["repair"], cooking["rules"]["offline_repair"],
            cooking["rules"]["refused_operations"]) == (True, COOKING_MIGRATION, list(OFFLINE_COOKING_MIGRATION), [115])


def console(tmp_path, services, state):
    commands, views, routes = registry(), view_registry(), RouteRegistry()
    admin_routes_state.register(routes)
    catalog = AdminCatalog(commands, views, routes, ("admin_cmd_settings", "admin_cmd_stories", "admin_routes_state",
                                                     "admin_views_progress", "admin_views_settings"))
    services.initialize = lambda value: value
    runtime = GenerationRuntime(generation_for(services, {"bootstrap": services.bootstrap.status()}))
    data = tmp_path / "data"
    store = Store(data / "aigis.sqlite3", state)
    application = create_admin_app(runtime, store, data_dir=data, admin_host="127.0.0.1", admin_port=PORT,
                                   game_port=GAME_PORT, token=TOKEN, preflight=NoopPreflight(),
                                   restart_check=NoopRestartCheck(), plan_store=PlanStore(), catalog=catalog,
                                   ui_directory=tmp_path / "ui")
    return TestClient(application, base_url=f"http://127.0.0.1:{PORT}"), store


def test_views_read_one_snapshot_through_the_admin_route_and_write_nothing(tmp_path, services, state):
    services.missions = login_missions()
    saved = logged_in(services.missions, state)
    saved[MISSION_HISTORY] = {"schema_version": 2, "receipts": [{"response": secret_response()}], "processed_keys": []}
    saved["tables"]["config"][0]["UserName"] = "Console"
    client, store = console(tmp_path, services, saved)
    before = store.profile()
    for name in SETTINGS_VIEWS + PROGRESS_VIEWS:
        response = client.get(f"{API_ROOT}/views/{name}", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200, (name, response.text)
        assert response.json()["view"] == name and response.json()["revision"] == before.revision
        assert SECRET not in response.text
    after = store.profile()
    assert (after.revision, json_digest(after.state)) == (before.revision, json_digest(before.state))
    commands = client.get(f"{API_ROOT}/commands", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert [command["operation"] for command in commands["commands"]] == list(OPERATIONS)
    assert commands["field_owners"] == EXPECTED_OWNERS


@pytest.fixture(scope="module")
def live_generation(bundle):
    return FakeGeneration(id="gen-live", bundle=bundle, content_versions={}, clock=instant(bundle.bootstrap.clock()))


def live_pipeline(store, bundle, generation, preflight):
    return CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(bundle.initialize), FakeRuntime(generation),
                          backup_directory=Path("backups"))


def test_live_settings_commands_pass_the_real_preflight_and_restart_check(bundle, live_generation, preflight):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    supported = next(item for item in bundle.skin_shop.catalog.products if item.supported)
    now = live_generation.clock.astimezone(JST).strftime(SAVE_TIME_FORMAT)
    store = FakeStore(state)
    plan, outcome = commit(live_pipeline(store, bundle, live_generation, preflight), [
        {"operation": SOUND.operation, "params": SOUND_VALUES},
        {"operation": FLAGS.operation, "params": {"flags": [{"ID": supported.flag_name, "Value": 0}], PURCHASE_FLAG_ACK: True}},
        {"operation": PERUSED.operation, "params": {"events": [{"ID": "console_event", "FLG": 1}]}},
        {"operation": BACKGROUNDS.operation, "params": {"rows": [{"CardID": 0, "ConfigID": "system_default", "SaveTime": now}]}},
    ], ("SET 1 PURCHASE FLAG",))
    flags_step = plan.steps[1].effects["purchase_flags"][0]
    assert (flags_step["item_id"], flags_step["holding"], flags_step["acquisition_recorded"]) == (supported.item_id, 0, False)
    assert outcome.result["readback"] == {"backgrounds": [137], "config": [36], "perused": [137], "universal_flags": [137]}
    assert store.revision == 1 and len(store.backups) == 1


def test_live_mark_read_settles_like_request_60(bundle, live_generation, preflight):
    now = live_generation.clock
    missions = bundle.missions
    state = missions.record(bundle.initialize(bundle.bootstrap.initial_state()), "login", {}, now)
    rows = missions.progress.snapshot(missions.initialize(state), now)
    unread = [row["ID"] for row in rows if row["Status"] == 1 and row["Read"] == 0]
    if not unread:
        pytest.skip("No login mission is active in the verified resources")
    store = FakeStore(state)
    plan, outcome = commit(live_pipeline(store, bundle, live_generation, preflight),
                           [{"operation": MISSIONS_READ.operation, "params": {"ids": unread[:1]}}])
    assert plan.steps[0].effects["marked"] == unread[:1] and outcome.result["readback"] == {"missions": [54]}
    assert MISSION_HISTORY not in store.state

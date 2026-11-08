from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps import admin_cmd_migrations
from aigis_ps.admin_cmd_economy import EXPERIENCE, RANK_RETRY
from aigis_ps.admin_cmd_migrations import (BEGINNER_CAPPED, COMMANDS, MISSION_REBUILD_UNNEEDED, REBASE_BEGINNER,
                                           REBIND_EVENT_PASS, REBUILD_MISSIONS, RESOLVE_COMEBACK_LOGIN,
                                           RESOLVE_DAILY_GIFT, RESOLVE_EVENT_LOGIN, RESOLVE_LOGIN_STAMPS,
                                           RESOLVE_PURCHASED_LOGIN, RESOLVE_RANK_REWARDS, SCHEDULE_STILL_CHANGED,
                                           SPLIT_TICKET_MAIL, STAMP_DEFINITION_MISSING, STAMP_RESOLVE_UNNEEDED,
                                           STILL_BUNDLED, STILL_CHANGED, STILL_PENDING, SlotStatus)
from aigis_ps.admin_command import AdminError, Batch, BatchStep, CommandContext, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.alxml import Column, Container
from aigis_ps.comeback_login_service import (DEFINITION_CHANGED as COMEBACK_CHANGED,
                                             DEFINITION_MISSING as COMEBACK_MISSING,
                                             RESOLVE_OPERATION as COMEBACK_OPERATION,
                                             RESOLVE_UNNEEDED as COMEBACK_UNNEEDED, comeback_reward_definition)
from aigis_ps.comeback_login_state import (DAILY_LIMIT as COMEBACK_DAILY_LIMIT, LEDGER_KEY as COMEBACK_LEDGER,
                                           begin_comeback_reward, read_comeback_logins)
from aigis_ps.daily_gift_service import (DEFINITION_CHANGED as GIFT_CHANGED, RESOLVE_OPERATION as GIFT_OPERATION,
                                         RESOLVE_UNNEEDED as GIFT_UNNEEDED, SOURCE_UNAVAILABLE, VOIDED,
                                         daily_gift_definition)
from aigis_ps.daily_gift_state import LEDGER_KEY as GIFT_LEDGER, begin_daily_gift
from aigis_ps.energy_clock import JST, UNIX_EPOCH
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_service import (DEFINITION_MISSING as EVENT_MISSING, RESOLVE_OPERATION as EVENT_OPERATION,
                                          RESOLVE_UNNEEDED as EVENT_UNNEEDED, event_login_definition)
from aigis_ps.event_login_state import (LEDGER_KEY as EVENT_LEDGER, begin_event_login, read_event_logins,
                                        validate_event_login_receipt)
from aigis_ps.event_pass_service import (DELIVERY_KEY as PASS_DELIVERY, REBIND_OPERATION as PASS_OPERATION,
                                         pass_identity)
from aigis_ps.event_pass_state import (ARCHIVED_KEY, DEFINITION_CHANGED as PASS_CHANGED, LEDGER_KEY as PASS_LEDGER,
                                       REBIND_UNNEEDED, delivered_mail_ids, initialize_event_passes, mark_event_pass,
                                       read_event_passes, read_pass_journal, rebind_definition)
from aigis_ps.login_reward_service import (DEFINITION_CHANGED as STAMP_CHANGED, REBASE_OPERATION, STAMP_DESCRIPTION,
                                           STAMP_RESOLVE_OPERATION)
from aigis_ps.login_reward_state import (LEDGER_KEY as LOGIN_LEDGER, REBASE_UNNEEDED, RULES_CHANGED,
                                         advance_login_rewards, read_login_rewards, rebase_beginner)
from aigis_ps.login_stamp_pending import PENDING_KEY as STAMP_LEDGER, hold_stamp, read_pending_stamps
from aigis_ps.mission_state import DAILY_PERIOD, JOURNAL_KEY, PERMANENT_PERIODS, SUPERSEDED_KEY
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES
from aigis_ps.presents import MAX_PRESENTS, SPLIT_UNNEEDED, PendingAction
from aigis_ps.purchased_login_service import (DEFINITION_CHANGED as PURCHASED_CHANGED,
                                              DEFINITION_MISSING as PURCHASED_MISSING,
                                              RESOLVE_OPERATION as PURCHASED_OPERATION,
                                              RESOLVE_UNNEEDED as PURCHASED_UNNEEDED)
from aigis_ps.purchased_login_state import (LEDGER_KEY as PURCHASED_LEDGER, add_purchased_login,
                                            begin_purchased_reward, purchase_window, purchased_login_clock,
                                            purchased_login_identity, purchased_login_time, read_purchased_logins)
from aigis_ps.rank_reward_state import LEDGER_KEY as RANK_LEDGER, bonus_fingerprint, read_rank_rewards
from aigis_ps.rank_rewards import (DEFINITION_CHANGED, DEFINITION_MISSING, DELIVERY_KEY as RANK_DELIVERY,
                                   RESOLVE_OPERATION as RANK_OPERATION, RESOLVE_UNNEEDED as RANK_UNNEEDED)
from aigis_ps.storage import AUTO_BACKUP_KIND
from test_admin_cmd_storage import commit, planned, preview_error
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_seams import MemoryStore
from test_daily_gift_catalog import daily_sources
from test_daily_gift_service import NOW as GIFT_NOW, factory, fail, initial, records
from test_event_pass_state import (IDENTITY, PASS, REWARD, delivery as pass_delivery, fail_unchanged, grant,
                                   row as pass_row, state as pass_state)
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 10, 12, tzinfo=JST)
GENERATION = "gen-migrations"
OLD_DEFINITION = "0" * 64
NEW_IDENTITY = "c" * 64
REQUEST = "d" * 64
OPERATIONS = ("comeback_login.resolve_changed", "daily_gift.resolve_changed", "event_login.resolve_changed",
              "event_pass.rebind_definition", "login_rewards.rebase_beginner", "login_rewards.resolve_pending_stamps",
              "missions.rebuild_changed", "presents.split_ticket_mail", "purchased_login.resolve_changed",
              "rank_rewards.resolve_changed")
DOMAINS = {"comeback_login.resolve_changed": "events", "daily_gift.resolve_changed": "events",
           "event_login.resolve_changed": "events", "event_pass.rebind_definition": "events",
           "login_rewards.rebase_beginner": "events", "login_rewards.resolve_pending_stamps": "events",
           "missions.rebuild_changed": "progress", "presents.split_ticket_mail": "presents",
           "purchased_login.resolve_changed": "events", "rank_rewards.resolve_changed": "progress"}
SERVICES = ((REBUILD_MISSIONS, "missions"), (RESOLVE_RANK_REWARDS, "rank_rewards"), (REBASE_BEGINNER, "login_rewards"),
            (REBIND_EVENT_PASS, "event_passes"), (RESOLVE_DAILY_GIFT, "daily_gifts"),
            (RESOLVE_EVENT_LOGIN, "event_logins"), (RESOLVE_COMEBACK_LOGIN, "comeback_logins"),
            (RESOLVE_PURCHASED_LOGIN, "purchased_logins"), (RESOLVE_LOGIN_STAMPS, "login_rewards"))
MAILBOX_ROOTS = frozenset(("last_present_id", "tables/presents"))
FIRST_MAIL_ROOTS = frozenset(("presents_schema", *MAILBOX_ROOTS))
CLAIM, MISSION_LIST = 1, 54
SUMMON_POOL, SELECTION_CONFIG, SELECTION_SLOT = 350, 1, 1
BUNDLED_TICKETS = 3
TICKET_DESCRIPTION = "移行券"
CLAIM_REFUSALS = {16: ("present_state_invalid", 503), 20: ("select_ticket_mail_unsupported", 501),
                  22: ("item_summon_mail_unsupported", 501)}
FILLER = {"kind": "gold", "amount": 1, "description": "Filler"}
RANK_UP_EXPERIENCE = 5000
CHANGED_RANK, UNREACHED_RANK = 5, 99
MISSING_REWARD = 999999
CATALOG_PASS, RETIRED_PASS = "event_pass_20261015", "event_pass_19990101"
PASS_POINT, PASS_MAIL, PASS_REWARD = 10, 900, 1
LATER_POINT, LATER_MAIL = 20, 901
PASS_RECEIPT_SCHEMA = 2
PASS_CLAIM_SCHEMA = 1
GIFT_ID, RETIRED_GIFT = 1, 999
EARNED_DAY, NEXT_DAY, WINDOW_END = "2026-11-01", "2026-11-02", "2026-11-03"
CHANGED_AMOUNT = 2
LOGIN_NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
LOGIN_DAY, NEXT_LOGIN_DAY, THIRD_LOGIN_DAY = "2026-11-01", "2026-11-02", "2026-11-03"
EVENT_CAMPAIGN, RETIRED_CAMPAIGN = 135, 2000
COMEBACK_GAP, COMEBACK_ROUND, NEXT_ROUND = 30, 1, 2
LOST_BAND = 1
PURCHASED_SKU, PURCHASE, RETIRED_PURCHASE_CAMPAIGN = "local_login_129", 1, 999999
STAMP_MONTH, TABLELESS_MONTH = "2026-11", "2027-01"
TABLELESS_NOW = datetime(2027, 1, 2, 12, tzinfo=JST)
UNKNOWN_GRADE = 99
TEMPLATE_GAP, UNSUPPORTED_GAP = "present_template_unsupported", "login_reward_unsupported"


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_migrations.register(commands)
    return commands


def initial_save(bundle, monkeypatch, now):
    # Claims and the mission list read the shared bootstrap clock, so a whole case runs at one instant.
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: now)
    return bundle.initialize(bundle.bootstrap.initial_state())


@pytest.fixture
def save(bundle, monkeypatch):
    return initial_save(bundle, monkeypatch, NOW)


@pytest.fixture
def login_save(bundle, monkeypatch):
    # Event login 135 is open on this day, and the login reward ledgers start from it.
    return initial_save(bundle, monkeypatch, LOGIN_NOW)


def console(services, state, preflight, now=NOW):
    store = FakeStore(state)
    generation = FakeGeneration(id=GENERATION, bundle=services, clock=now)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def changed_roots(plan) -> set[str]:
    diff = plan.describe()["diff"]
    assert not diff["truncated"]
    roots = set()
    for entry in diff["entries"]:
        parts = entry["path"].split("/")[1:]
        roots.add("/".join(parts[:2] if parts[0] == "tables" else parts[:1]))
    return roots


def migrate(services, state, preflight, registry, operation, params, roots, now=NOW):
    store, pipeline = console(services, state, preflight, now)
    plan = planned(pipeline, registry, operation, params)
    command = registry.get(operation)
    phrase = command.phrase(command.parse(params))
    assert [item.describe() for item in plan.confirmations] == [{"step": 0, "operation": operation, "phrase": phrase}]
    assert plan.restart_check == {"ok": True} and set(plan.preflight["readback"]) == command.touched
    assert changed_roots(plan) == set(roots)
    with pytest.raises(AdminError) as caught:
        commit(pipeline, plan)
    assert caught.value.code == "admin_confirmation_required" and store.calls == [] and store.backups == []
    commit(pipeline, plan, (phrase,))
    [backup] = store.backups
    assert backup["row"]["kind"] == AUTO_BACKUP_KIND and backup["state"] == state and store.revision == 1
    [step] = plan.describe()["steps"]
    return store.state, step


def refused(services, state, preflight, registry, operation, params, code, status, now=NOW):
    store, pipeline = console(services, state, preflight, now)
    preview_error(pipeline, registry, operation, params, code, status)
    assert store.calls == [] and store.backups == []


def run(services, command, state, raw, now=NOW):
    context = CommandContext(now=now, acquisition_day=now.astimezone(JST).date().toordinal() - 1, revision=0,
                             generation_id=GENERATION, services=services)
    outcome = Batch((BatchStep(command, command.parse(raw)),)).apply(state, context)
    return outcome.state, outcome.steps[0]


def test_migrations_register_on_the_pages_of_their_ledgers():
    catalog = discover(PREFLIGHT_DOMAINS)
    assert "admin_cmd_migrations" in catalog.modules
    assert {command.operation: command.domain for command in catalog.commands
            if command.operation in OPERATIONS} == DOMAINS
    assert tuple(sorted(command.operation for command in COMMANDS)) == OPERATIONS


@pytest.mark.parametrize("command, operation", [
    (RESOLVE_RANK_REWARDS, RANK_OPERATION), (REBIND_EVENT_PASS, PASS_OPERATION),
    (REBASE_BEGINNER, REBASE_OPERATION), (RESOLVE_LOGIN_STAMPS, STAMP_RESOLVE_OPERATION),
    (RESOLVE_DAILY_GIFT, GIFT_OPERATION), (RESOLVE_EVENT_LOGIN, EVENT_OPERATION),
    (RESOLVE_COMEBACK_LOGIN, COMEBACK_OPERATION), (RESOLVE_PURCHASED_LOGIN, PURCHASED_OPERATION),
])
def test_repair_commands_take_the_operation_names_their_services_publish(command, operation):
    assert command.operation is operation


def test_migrations_touch_mapped_domains_behind_a_phrase_and_a_backup(registry):
    assert [command.operation for command in registry] == list(OPERATIONS)
    for command in COMMANDS:
        assert command.touched <= set(PREFLIGHT_DOMAINS) and not command.preflight_none, command.operation
        assert command.destructive is True and not command.writes, command.operation
    phrases = ((SPLIT_TICKET_MAIL, {"present_id": 12}, "MIGRATE TICKET MAIL 12"),
               (REBUILD_MISSIONS, {}, "MIGRATE MISSIONS"),
               (RESOLVE_RANK_REWARDS, {"rank": 25, "action": "redeliver"}, "MIGRATE RANK 25 REWARDS REDELIVER"),
               (REBASE_BEGINNER, {}, "MIGRATE BEGINNER LOGIN REWARDS"),
               (REBIND_EVENT_PASS, {"pass_id": CATALOG_PASS}, "MIGRATE EVENT PASS"),
               (RESOLVE_DAILY_GIFT, {"gift_id": 3, "action": "void"}, "MIGRATE DAILY GIFT 3 VOID"),
               (RESOLVE_EVENT_LOGIN, {"campaign_id": EVENT_CAMPAIGN, "action": "redeliver"},
                "MIGRATE EVENT LOGIN 135 REDELIVER"),
               (RESOLVE_COMEBACK_LOGIN, {"round_id": NEXT_ROUND, "action": "void"}, "MIGRATE COMEBACK ROUND 2 VOID"),
               (RESOLVE_PURCHASED_LOGIN, {"purchase_id": PURCHASE, "action": "void"},
                "MIGRATE PURCHASED LOGIN 1 VOID"),
               (RESOLVE_LOGIN_STAMPS, {"month": STAMP_MONTH, "action": "redeliver"},
                "MIGRATE LOGIN STAMPS 2026-11 REDELIVER"))
    for command, params, phrase in phrases:
        assert command.phrase(command.parse(params)) == phrase
    for command, service in SERVICES:
        assert command.availability(SimpleNamespace(**{service: None})) == missing_service(service)
    with pytest.raises(ValueError):
        admin_cmd_migrations.register(registry)


@pytest.mark.parametrize("command, params", [
    (SPLIT_TICKET_MAIL, {}), (SPLIT_TICKET_MAIL, {"present_id": 0}), (SPLIT_TICKET_MAIL, {"present_id": True}),
    (SPLIT_TICKET_MAIL, {"present_id": "1"}), (SPLIT_TICKET_MAIL, {"present_id": INT32_MAX + 1}),
    (SPLIT_TICKET_MAIL, {"present_id": 1, "count": 2}), (REBUILD_MISSIONS, {"force": True}),
    (RESOLVE_RANK_REWARDS, {"rank": CHANGED_RANK}), (RESOLVE_RANK_REWARDS, {"action": "void"}),
    (RESOLVE_RANK_REWARDS, {"rank": 0, "action": "void"}), (RESOLVE_RANK_REWARDS, {"rank": 5.0, "action": "void"}),
    (RESOLVE_RANK_REWARDS, {"rank": CHANGED_RANK, "action": "VOID"}),
    (RESOLVE_RANK_REWARDS, {"rank": CHANGED_RANK, "action": "delete"}), (REBASE_BEGINNER, {"beginner_count": 7}),
    (REBIND_EVENT_PASS, {}), (REBIND_EVENT_PASS, {"pass_id": ""}), (REBIND_EVENT_PASS, {"pass_id": 1}),
    (REBIND_EVENT_PASS, {"pass_id": "x" * (STORED_ITEM_NAME_BYTES + 1)}),
    (RESOLVE_DAILY_GIFT, {"gift_id": GIFT_ID}), (RESOLVE_DAILY_GIFT, {"gift_id": -1, "action": "void"}),
    (RESOLVE_DAILY_GIFT, {"gift_id": GIFT_ID, "action": 1}),
    (RESOLVE_DAILY_GIFT, {"gift_id": GIFT_ID, "action": "void", "day": EARNED_DAY}),
    (RESOLVE_EVENT_LOGIN, {"campaign_id": EVENT_CAMPAIGN}), (RESOLVE_EVENT_LOGIN, {"campaign_id": 0, "action": "void"}),
    (RESOLVE_EVENT_LOGIN, {"campaign_id": EVENT_CAMPAIGN, "action": "void", "ordinal": 1}),
    (RESOLVE_COMEBACK_LOGIN, {"round_id": "1", "action": "void"}),
    (RESOLVE_COMEBACK_LOGIN, {"round_id": COMEBACK_ROUND, "action": "drop"}),
    (RESOLVE_PURCHASED_LOGIN, {"purchase_id": INT32_MAX + 1, "action": "void"}),
    (RESOLVE_PURCHASED_LOGIN, {"action": "redeliver"}),
    (RESOLVE_LOGIN_STAMPS, {"month": STAMP_MONTH}), (RESOLVE_LOGIN_STAMPS, {"month": "2026-13", "action": "void"}),
    (RESOLVE_LOGIN_STAMPS, {"month": NEXT_LOGIN_DAY, "action": "void"}),
])
def test_migration_parameters_are_strict(command, params):
    with pytest.raises(AdminError) as caught:
        command.parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def ticket_reward(bundle, kind, count):
    if kind == 16:
        return {"kind": "summon_ticket", "pool_id": SUMMON_POOL, "count": count, "description": TICKET_DESCRIPTION}
    if kind == 20:
        return {"kind": "selection_ticket", "config_id": SELECTION_CONFIG, "count": count,
                "description": TICKET_DESCRIPTION}
    return {"kind": "item", "item_id": bundle.presents.item_tickets.status()["enabled_items"][0], "amount": count,
            "description": TICKET_DESCRIPTION}


def bundled_mail(bundle, save, kind, *sizes):
    # Local writers mail one ticket per row, so imported mail holding several is rebuilt from such rows.
    state, rows = bundle.presents.enqueue(save, [ticket_reward(bundle, kind, sum(sizes))])
    kept, start = [], 0
    for size in sizes:
        kept.append({**rows[start], "Content": size})
        start += size
    state["tables"]["presents"] = kept
    return state


def selection_columns(bundle, kind):
    if kind != 20:
        return ()
    card = bundle.selection_tickets.catalog.get(SELECTION_CONFIG).candidates[SELECTION_SLOT - 1].card_id
    return Column("SelectID", "I", (SELECTION_SLOT,)), Column("SelectCardID", "I", (card,))


def claim(bundle, store, present_id, kind):
    columns = (Column("PresentID", "I", (present_id,)), *selection_columns(bundle, kind))
    return bundle.presents.receive(request(store, CLAIM, (Container("RT", (Container("PT2", columns),)),)), store)


@pytest.mark.parametrize("kind", sorted(CLAIM_REFUSALS))
def test_split_ticket_mail_makes_every_ticket_of_an_imported_mail_claimable(bundle, save, preflight, registry, kind):
    box = bundled_mail(bundle, save, kind, BUNDLED_TICKETS)
    [mail] = box["tables"]["presents"]
    store = MemoryStore(box)
    with pytest.raises(ProtocolError) as caught:
        claim(bundle, store, mail["PresentID"], kind)
    assert (caught.value.code, caught.value.status) == CLAIM_REFUSALS[kind] and store.profile().revision == 0
    split, step = migrate(bundle, box, preflight, registry, SPLIT_TICKET_MAIL.operation,
                          {"present_id": mail["PresentID"]}, MAILBOX_ROOTS)
    pieces = list(range(box["last_present_id"] + 1, box["last_present_id"] + BUNDLED_TICKETS + 1))
    assert step["warnings"] == [] and step["effects"] == {"present_id": mail["PresentID"], "type": kind,
                                                          "tickets": BUNDLED_TICKETS, "present_ids": pieces,
                                                          "still_bundled": []}
    assert bundle.presents.rows(split) == [{**mail, "PresentID": present_id, "Content": 1} for present_id in pieces]
    store = MemoryStore(split)
    units = len(split["player_state"]["units"])
    claim(bundle, store, pieces[0], kind)
    claimed = store.profile().state
    assert len(claimed["player_state"]["units"]) > units
    assert [row["PresentID"] for row in bundle.presents.rows(claimed)] == pieces[1:]


def test_summon_mail_holding_several_draws_needs_every_split_in_one_batch(bundle, save, preflight, registry):
    box = bundled_mail(bundle, save, 16, 2, 2)
    first, second = (row["PresentID"] for row in box["tables"]["presents"])
    store, pipeline = console(bundle, box, preflight)
    splits = [{"operation": SPLIT_TICKET_MAIL.operation, "params": {"present_id": identifier}}
              for identifier in (first, second)]
    single = pipeline.preview(registry.batch(splits[:1]))
    [step] = single.describe()["steps"]
    assert step["effects"]["still_bundled"] == [second] and step["warnings"] == [STILL_BUNDLED]
    assert not single.committable
    assert single.preflight["error"]["details"]["error"]["code"] == "present_state_invalid"
    both = pipeline.preview(registry.batch(splits))
    assert both.committable, both.describe()
    assert [outcome["warnings"] for outcome in both.describe()["steps"]] == [[STILL_BUNDLED], []]
    phrases = tuple(item.phrase for item in both.confirmations)
    assert phrases == (f"MIGRATE TICKET MAIL {first}", f"MIGRATE TICKET MAIL {second}")
    commit(pipeline, both, phrases)
    assert len(store.backups) == 1 and bundle.presents.bundled_tickets(store.state) == ()
    assert [row["Content"] for row in bundle.presents.rows(store.state)] == [1] * 4


def test_mail_that_needs_no_split_is_refused_before_any_backup(bundle, save, preflight, registry):
    state, rows = bundle.presents.enqueue(save, [ticket_reward(bundle, 20, 1), FILLER])
    for present_id, code in ((rows[0]["PresentID"], SPLIT_UNNEEDED), (rows[1]["PresentID"], SPLIT_UNNEEDED),
                             (rows[1]["PresentID"] + 1, "present_not_found")):
        refused(bundle, state, preflight, registry, SPLIT_TICKET_MAIL.operation, {"present_id": present_id}, code, 409)


def mission_list(bundle, state):
    store = MemoryStore(state)
    return bundle.missions.read(request(store, MISSION_LIST, ()), store)


def test_mission_rows_frozen_under_changed_definitions_are_rebuilt_for_the_mission_list(bundle, save, preflight,
                                                                                         registry):
    state = bundle.missions.record(save, "login", {}, NOW)
    entries = state[JOURNAL_KEY]["entries"]
    daily = next(entry for entry in entries if entry["period_type"] == DAILY_PERIOD and entry["status"] == 1)
    unclaimed, claimed = [entry for entry in entries if entry["period_type"] in PERMANENT_PERIODS
                          and entry["status"] == 1][:2]
    daily["definition_version"] = OLD_DEFINITION
    for entry in (unclaimed, claimed):
        entry["period_type"] = min(PERMANENT_PERIODS - {entry["period_type"]})
    claimed["status"] = 2
    with pytest.raises(ProtocolError) as caught:
        mission_list(bundle, state)
    assert (caught.value.code, caught.value.status) == ("mission_definition_changed", 503)
    rebuilt, step = migrate(bundle, state, preflight, registry, REBUILD_MISSIONS.operation, {}, {JOURNAL_KEY})
    effects = step["effects"]
    assert effects["actions"] == {"deleted": 1, "rebuilt": 1, "superseded": 1}
    assert {(row["condition_id"], row["action"]) for row in effects["rows"]} == {
        (daily["condition_id"], "rebuilt"), (unclaimed["condition_id"], "deleted"),
        (claimed["condition_id"], "superseded")}
    assert mission_list(bundle, rebuilt)
    journal = {(row["condition_id"], row["period"]): row for row in rebuilt[JOURNAL_KEY]["entries"]}
    current = bundle.missions.catalog.by_id[daily["condition_id"]]
    assert journal[daily["condition_id"], daily["period"]]["definition_version"] == current.definition_version
    assert not {(entry["condition_id"], entry["period"]) for entry in (unclaimed, claimed)} & journal.keys()
    assert rebuilt[JOURNAL_KEY][SUPERSEDED_KEY] == [claimed]
    refused(bundle, rebuilt, preflight, registry, REBUILD_MISSIONS.operation, {}, MISSION_REBUILD_UNNEEDED, 409)


def ranked(bundle, save):
    # A full mailbox keeps every reached rank reward pending, as rewards left by an earlier resource would be.
    full, _ = bundle.presents.enqueue(save, [FILLER] * MAX_PRESENTS)
    state, _ = run(bundle, EXPERIENCE, full, {"amount": RANK_UP_EXPERIENCE})
    return state


def emptied(state):
    result = deepcopy(state)
    result["tables"]["presents"] = []
    return result


def frozen_rank(state, rank=CHANGED_RANK):
    changed = deepcopy(state)
    entries = changed[RANK_LEDGER]["entries"]
    keys = sorted(int(key) for key, entry in entries.items() if entry["rank"] == rank)
    for key in keys:
        entries[str(key)]["definition"] = OLD_DEFINITION
    return changed, keys


def missing_reward(state, rank=CHANGED_RANK):
    changed = deepcopy(state)
    changed[RANK_LEDGER]["entries"][str(MISSING_REWARD)] = {"rank": rank, "definition": OLD_DEFINITION,
                                                             "status": "pending", "mail_ids": [], "reason": None}
    return changed


def resolution(action, rank=CHANGED_RANK):
    return {"rank": rank, "action": action}


def reasons(state, keys):
    entries = read_rank_rewards(state)["entries"]
    return {entries[str(key)]["reason"] for key in keys}


def test_redelivery_mails_changed_rank_rewards_under_the_current_definition(bundle, save, preflight, registry):
    full, frozen = frozen_rank(ranked(bundle, save))
    rewards = bundle.rank_rewards
    assert frozen and rewards.changed_rewards(full, CHANGED_RANK) == tuple(
        sorted(frozen, key=lambda key: rewards.catalog.by_id[key].config_index))
    waiting, step = run(bundle, RESOLVE_RANK_REWARDS, full, resolution("redeliver"))
    assert reasons(waiting, frozen) == {"present_capacity"} and rewards.changed_rewards(waiting, CHANGED_RANK) == ()
    assert step.effects["mail_ids"] == [] and step.warnings == (STILL_PENDING,)
    changed = emptied(full)
    retried, _ = run(bundle, RANK_RETRY, changed, {})
    assert reasons(retried, frozen) == {DEFINITION_CHANGED}
    resolved, step = migrate(bundle, changed, preflight, registry, RESOLVE_RANK_REWARDS.operation,
                             resolution("redeliver"), {RANK_LEDGER, *MAILBOX_ROOTS})
    effects, entries = step["effects"], read_rank_rewards(resolved)["entries"]
    assert [item["bonus_id"] for item in effects["resolved"]] == frozen and effects["still_changed"] == []
    for item in effects["resolved"]:
        assert item["before"]["definition"] == OLD_DEFINITION
        assert item["after"] == {name: entries[str(item["bonus_id"])][name] for name in item["after"]}
        assert item["after"]["definition"] == bonus_fingerprint(rewards.catalog.by_id[item["bonus_id"]])
    delivered = [item["after"]["mail_ids"] for item in effects["resolved"] if item["after"]["status"] == "delivered"]
    assert delivered and sorted(effects["mail_ids"]) == sorted(mail for ids in delivered for mail in ids)
    assert effects["mail_ids"] == [row["PresentID"] for row in bundle.presents.rows(resolved)]
    assert step["warnings"] == ([STILL_PENDING] if len(delivered) < len(frozen) else [])
    again, _ = run(bundle, RANK_RETRY, resolved, {})
    assert DEFINITION_CHANGED not in reasons(again, frozen)


def test_voided_rank_rewards_leave_the_ledger_and_are_never_offered_again(bundle, save, preflight, registry):
    changed, frozen = frozen_rank(emptied(ranked(bundle, save)))
    voided, step = migrate(bundle, changed, preflight, registry, RESOLVE_RANK_REWARDS.operation, resolution("void"),
                           {RANK_LEDGER})
    effects = step["effects"]
    assert [(item["bonus_id"], item["after"]) for item in effects["resolved"]] == [(key, None) for key in frozen]
    assert effects["mail_ids"] == [] and step["warnings"] == []
    ledger = read_rank_rewards(voided)
    assert not {str(key) for key in frozen} & ledger["entries"].keys()
    assert ledger["observed_rank"] == read_rank_rewards(changed)["observed_rank"]
    again, _ = run(bundle, RANK_RETRY, voided, {})
    assert not {str(key) for key in frozen} & read_rank_rewards(again)["entries"].keys()


def test_rewards_missing_from_the_current_catalog_can_only_be_voided(bundle, save, preflight, registry):
    state = ranked(bundle, save)
    changed, frozen = frozen_rank(state)
    partial, step = run(bundle, RESOLVE_RANK_REWARDS, missing_reward(changed), resolution("redeliver"))
    assert [item["bonus_id"] for item in step.effects["resolved"]] == frozen
    assert step.effects["still_changed"] == [MISSING_REWARD] and STILL_CHANGED in step.warnings
    rest, step = run(bundle, RESOLVE_RANK_REWARDS, partial, resolution("void"))
    assert [item["bonus_id"] for item in step.effects["resolved"]] == [MISSING_REWARD]
    assert str(MISSING_REWARD) not in read_rank_rewards(rest)["entries"]
    refused(bundle, missing_reward(state), preflight, registry, RESOLVE_RANK_REWARDS.operation,
            resolution("redeliver"), DEFINITION_MISSING, 409)
    for rank in (CHANGED_RANK, UNREACHED_RANK):
        refused(bundle, state, preflight, registry, RESOLVE_RANK_REWARDS.operation, resolution("void", rank),
                RANK_UNNEEDED, 409)


def test_rank_reward_resolution_validates_its_rank_and_action(bundle, save):
    for rank, action, code in ((0, PendingAction.VOID, "rank_reward_rank_invalid"),
                               (True, PendingAction.VOID, "rank_reward_rank_invalid"),
                               (CHANGED_RANK, "delete", "rank_reward_action_invalid")):
        with pytest.raises(ProtocolError) as caught:
            bundle.rank_rewards.resolve_changed(save, rank, action)
        assert caught.value.code == code


def pass_mail(point=PASS_POINT, mail=PASS_MAIL, reward=PASS_REWARD):
    return {point: {"definition": REWARD, "reward_ids": [reward], "mail_ids": [mail]}}


def pass_row_of(state, pass_id=CATALOG_PASS):
    return next(row for row in read_event_passes(state) if row["ID"] == pass_id)


def test_event_pass_history_under_an_earlier_identity_is_archived_by_the_rebind(bundle, save, preflight, registry):
    identity = pass_identity(bundle.event_passes.catalog.get(CATALOG_PASS))
    earlier = mark_event_pass(save, CATALOG_PASS, definition=OLD_DEFINITION, normal_deliveries=pass_mail())
    with pytest.raises(ProtocolError) as caught:
        mark_event_pass(earlier, CATALOG_PASS, definition=identity,
                        normal_deliveries=pass_mail(LATER_POINT, LATER_MAIL))
    assert caught.value.code == PASS_CHANGED
    params = {"pass_id": CATALOG_PASS}
    rebound, step = migrate(bundle, earlier, preflight, registry, REBIND_EVENT_PASS.operation, params,
                            {PASS_LEDGER})
    assert step["effects"] == {"pass_id": CATALOG_PASS, "identity": {"before": OLD_DEFINITION, "after": identity},
                               "baseline": {"normal": PASS_POINT, "premium": 0},
                               "archived_thresholds": {"normal": [PASS_POINT], "premium": []}, "archived_identities": 1}
    assert read_event_passes(rebound) == read_event_passes(earlier)
    assert read_pass_journal(rebound, CATALOG_PASS)[ARCHIVED_KEY] == [read_pass_journal(earlier, CATALOG_PASS)]
    assert delivered_mail_ids(rebound)[CATALOG_PASS] == {PASS_MAIL}
    later = mark_event_pass(rebound, CATALOG_PASS, definition=identity,
                            normal_deliveries=pass_mail(LATER_POINT, LATER_MAIL))
    assert pass_row_of(later)["ReceivedNormalPoint"] == LATER_POINT
    refused(bundle, rebound, preflight, registry, REBIND_EVENT_PASS.operation, params, REBIND_UNNEEDED, 409)
    refused(bundle, save, preflight, registry, REBIND_EVENT_PASS.operation, params, REBIND_UNNEEDED, 409)
    refused(bundle, save, preflight, registry, REBIND_EVENT_PASS.operation, {"pass_id": RETIRED_PASS},
            "event_pass_unknown", 501)


def rank_receipt(mail):
    return {"schema_version": PASS_RECEIPT_SCHEMA, "request": REQUEST, "mail_ids": [mail], "rank_mail_ids": [],
            "event_pass_mail_ids": {CATALOG_PASS: [mail]}}


def test_rank_receipts_still_find_automatic_pass_mail_after_a_rebind(bundle, save):
    earlier = mark_event_pass(save, CATALOG_PASS, definition=OLD_DEFINITION, normal_deliveries=pass_mail())
    rebound = rebind_definition(earlier, CATALOG_PASS, pass_identity(bundle.event_passes.catalog.get(CATALOG_PASS)))
    for state in (earlier, rebound):
        bundle.rank_rewards.initialize({**state, RANK_DELIVERY: rank_receipt(PASS_MAIL)})
    with pytest.raises(ProtocolError) as caught:
        bundle.rank_rewards.initialize({**rebound, RANK_DELIVERY: rank_receipt(LATER_MAIL)})
    assert caught.value.code == "rank_reward_history_invalid"


def claimed_by_159(state, mail=PASS_MAIL):
    return {**state, PASS_DELIVERY: {"schema_version": PASS_CLAIM_SCHEMA, "request": REQUEST,
                                     "pass_id": CATALOG_PASS, "mail_ids": [mail]}}


def test_a_pass_last_claimed_through_159_is_rebound_with_its_claim_receipt_intact(bundle, save, preflight, registry):
    claimed = claimed_by_159(mark_event_pass(save, CATALOG_PASS, definition=OLD_DEFINITION,
                                             normal_deliveries=pass_mail()))
    rebound, _ = migrate(bundle, claimed, preflight, registry, REBIND_EVENT_PASS.operation, {"pass_id": CATALOG_PASS},
                         {PASS_LEDGER})
    assert rebound[PASS_DELIVERY] == claimed[PASS_DELIVERY]
    bundle.event_passes.validate_delivery(rebound)
    with pytest.raises(ProtocolError) as caught:
        bundle.event_passes.validate_delivery(claimed_by_159(rebound, LATER_MAIL))
    assert caught.value.code == "event_pass_history_invalid"


def test_rebinding_keeps_the_watermark_and_lets_the_new_identity_continue_from_it():
    original = grant(pass_state(), {10: pass_delivery()}, {5: pass_delivery((2,), (200,))})
    before = deepcopy(original)
    rebound = rebind_definition(original, PASS, NEW_IDENTITY)
    assert original == before and read_event_passes(rebound) == read_event_passes(original)
    entry = rebound[PASS_LEDGER]["passes"][PASS]
    assert entry == {"definition": NEW_IDENTITY, "baseline_normal": 10, "baseline_premium": 5,
                     "deliveries": {"normal": {}, "premium": {}},
                     ARCHIVED_KEY: [before[PASS_LEDGER]["passes"][PASS]]}
    assert initialize_event_passes(rebound) == rebound
    fail_unchanged(rebound, lambda: grant(rebound, {10: pass_delivery((3,), (300,))}, definition=NEW_IDENTITY),
                   "event_pass_already_received")
    fail_unchanged(rebound, lambda: grant(rebound, {20: pass_delivery((3,), (100,))}, definition=NEW_IDENTITY),
                   "event_pass_delivery_duplicate")
    # Reward IDs belong to the archived definition, so only its mail stays spent.
    later = grant(rebound, {20: pass_delivery((1,), (300,))}, definition=NEW_IDENTITY)
    assert later["tables"]["event_pass_status"] == [pass_row(normal=20, premium=5)]
    again = rebind_definition(later, PASS, IDENTITY)
    archived = again[PASS_LEDGER]["passes"][PASS][ARCHIVED_KEY]
    assert [record["definition"] for record in archived] == [IDENTITY, NEW_IDENTITY]
    assert delivered_mail_ids(again)[PASS] == {100, 200, 300}


@pytest.mark.parametrize("corrupt", ["empty", "not_list", "unbound", "gap", "reused_mail", "extra_field", "nested"])
def test_corrupt_archived_identities_are_never_repaired(corrupt):
    original = rebind_definition(grant(pass_state(), {10: pass_delivery()}), PASS, NEW_IDENTITY)
    entry = original[PASS_LEDGER]["passes"][PASS]
    record, native = entry[ARCHIVED_KEY][0], original["tables"]["event_pass_status"][0]
    if corrupt == "empty":
        entry[ARCHIVED_KEY] = []
    elif corrupt == "not_list":
        entry[ARCHIVED_KEY] = record
    elif corrupt == "unbound":
        record.update(definition=None, deliveries={"normal": {}, "premium": {}})
    elif corrupt == "gap":
        entry["baseline_normal"] = native["ReceivedNormalPoint"] = 5
    elif corrupt == "reused_mail":
        entry["deliveries"]["normal"]["20"] = pass_delivery((2,), (100,))
        native["ReceivedNormalPoint"] = 20
    elif corrupt == "extra_field":
        record["future"] = 1
    else:
        record[ARCHIVED_KEY] = [deepcopy(record)]
    for action in (lambda: read_event_passes(original), lambda: initialize_event_passes(original),
                   lambda: delivered_mail_ids(original), lambda: rebind_definition(original, PASS, IDENTITY)):
        fail_unchanged(original, action, "event_pass_state_invalid")


def test_rebinding_needs_saved_history_under_another_identity():
    unbound = initialize_event_passes(pass_state([pass_row(normal=10)]))
    delivered = grant(pass_state(), {10: pass_delivery()})
    for original, identity in ((pass_state(), NEW_IDENTITY), (unbound, NEW_IDENTITY), (delivered, IDENTITY)):
        fail_unchanged(original, lambda: rebind_definition(original, PASS, identity), REBIND_UNNEEDED)
    fail_unchanged(delivered, lambda: rebind_definition(delivered, PASS, "bad"), "event_pass_definition_invalid")
    fail_unchanged(delivered, lambda: rebind_definition(delivered, "", NEW_IDENTITY), "event_pass_state_invalid")


def claimed_beginners(save, total, claimed):
    saved = deepcopy(save)
    saved[LOGIN_LEDGER]["beginner_total"] = total
    start = UNIX_EPOCH + timedelta(milliseconds=saved[LOGIN_LEDGER]["last_time"])
    for day in range(1, claimed + 1):
        saved, _ = advance_login_rewards(saved, start + timedelta(days=day), beginner_count=total)
    return saved


def test_rebasing_beginner_rewards_follows_the_current_total(bundle, save, preflight, registry):
    total = bundle.login_rewards.beginner_total
    saved = claimed_beginners(save, total + 3, total + 2)
    assert read_login_rewards(bundle.login_rewards.initialize(saved)) == read_login_rewards(saved)
    rules = bundle.login_rewards.beginner_rules(saved)
    assert (rules["saved_total"], rules["current_total"], rules["changed"]) == (total + 3, total, True)
    assert rules["repair"] == REBASE_BEGINNER.operation and rules["refused_operations"] == [115]
    with pytest.raises(ProtocolError) as caught:
        advance_login_rewards(saved, NOW + timedelta(days=total + 3), beginner_count=total)
    assert (caught.value.code, caught.value.status) == (RULES_CHANGED, 503)
    rebased, step = migrate(bundle, saved, preflight, registry, REBASE_BEGINNER.operation, {}, {LOGIN_LEDGER})
    assert step["effects"] == {"eligible": True, "total": {"before": total + 3, "after": total},
                               "claimed": {"before": total + 2, "after": total}}
    assert step["warnings"] == [BEGINNER_CAPPED]
    bundle.login_rewards.initialize(rebased)
    assert not bundle.login_rewards.beginner_rules(rebased)["changed"]
    assert rebased["tables"]["game_param2"] == saved["tables"]["game_param2"]
    refused(bundle, rebased, preflight, registry, REBASE_BEGINNER.operation, {}, REBASE_UNNEEDED, 409)


def test_a_larger_beginner_total_keeps_the_claimed_count(save):
    saved = claimed_beginners(save, 3, 2)
    rebased = rebase_beginner(saved, beginner_count=5)
    assert read_login_rewards(rebased) == {**read_login_rewards(saved), "beginner_total": 5}
    with pytest.raises(ProtocolError) as caught:
        rebase_beginner(saved, beginner_count=3)
    assert (caught.value.code, caught.value.status) == (REBASE_UNNEEDED, 409)
    for count in (0, 367, True, "5"):
        with pytest.raises(ProtocolError) as caught:
            rebase_beginner(saved, beginner_count=count)
        assert caught.value.code == "login_rewards_count_invalid"


def frozen_gift(factory, monkeypatch):
    case = factory()
    monkeypatch.setattr(case.presents, "enqueue_groups", fail("present_capacity", 409))
    pending, _ = case.service.settle(case.state, GIFT_NOW)
    changed = factory(changes={0: {"Param_Gift": CHANGED_AMOUNT}})
    deferred, grants = changed.service.settle(pending, GIFT_NOW)
    assert not grants and records(deferred)[EARNED_DAY]["reason"] == GIFT_CHANGED
    return changed, deferred


def gift_resolution(action, gift_id=GIFT_ID):
    return {"gift_id": gift_id, "action": action}


def test_redelivery_mails_a_changed_daily_gift_under_the_current_definition(bundle, factory, monkeypatch, preflight,
                                                                           registry):
    changed, deferred = frozen_gift(factory, monkeypatch)
    services = replace(bundle, daily_gifts=changed.service)
    resolved, step = migrate(services, deferred, preflight, registry, RESOLVE_DAILY_GIFT.operation,
                             gift_resolution("redeliver"), {GIFT_LEDGER, "presents_schema", *MAILBOX_ROOTS}, GIFT_NOW)
    record = records(resolved)[EARNED_DAY]
    current = daily_gift_definition(changed.catalog.get(GIFT_ID))
    assert (record["status"], record["definition"]) == ("delivered", current)
    assert step["warnings"] == [] and step["effects"]["mail_ids"] == record["mail_ids"]
    assert [item["earned_day"] for item in step["effects"]["resolved"]] == [EARNED_DAY]
    assert [row["Content"] for row in changed.presents.rows(resolved)] == [CHANGED_AMOUNT]
    assert changed.service.settle(resolved, GIFT_NOW) == (resolved, [])


def test_voided_daily_gifts_keep_their_earned_day_without_mail(bundle, factory, monkeypatch, preflight, registry):
    changed, deferred = frozen_gift(factory, monkeypatch)
    services = replace(bundle, daily_gifts=changed.service)
    voided, step = migrate(services, deferred, preflight, registry, RESOLVE_DAILY_GIFT.operation,
                           gift_resolution("void"), {GIFT_LEDGER}, GIFT_NOW)
    assert records(voided)[EARNED_DAY] == {**records(deferred)[EARNED_DAY], "reason": VOIDED}
    assert step["effects"]["mail_ids"] == [] and step["warnings"] == []
    assert changed.service.settle(voided, GIFT_NOW) == (voided, [])
    later, grants = changed.service.settle(voided, GIFT_NOW + timedelta(days=1))
    assert [grant["earned_day"] for grant in grants] == [NEXT_DAY]
    assert records(later)[EARNED_DAY] == records(voided)[EARNED_DAY]
    refused(services, voided, preflight, registry, RESOLVE_DAILY_GIFT.operation, gift_resolution("void"),
            GIFT_UNNEEDED, 409, GIFT_NOW)


def test_daily_gifts_whose_source_is_gone_can_only_be_voided(factory):
    case = factory()
    gone = begin_daily_gift(case.state, RETIRED_GIFT, OLD_DEFINITION, EARNED_DAY, WINDOW_END, GIFT_NOW)
    with pytest.raises(ProtocolError) as caught:
        case.service.resolve_changed(gone, RETIRED_GIFT, PendingAction.REDELIVER, GIFT_NOW)
    assert (caught.value.code, caught.value.status) == (SOURCE_UNAVAILABLE, 409)
    voided, grants = case.service.resolve_changed(gone, RETIRED_GIFT, PendingAction.VOID, GIFT_NOW)
    assert grants == [] and records(voided, RETIRED_GIFT) == {
        EARNED_DAY: {**records(gone, RETIRED_GIFT)[EARNED_DAY], "reason": VOIDED}}
    for gift_id, action, code in ((GIFT_ID, PendingAction.VOID, GIFT_UNNEEDED),
                                  (0, PendingAction.VOID, "daily_gift_selection_invalid"),
                                  (GIFT_ID, "delete", "daily_gift_action_invalid")):
        with pytest.raises(ProtocolError) as caught:
            case.service.resolve_changed(gone, gift_id, action, GIFT_NOW)
        assert caught.value.code == code


def slot(status, definition, delivery_day=None, mail_ids=(), reason=None):
    return {"status": status, "definition": definition, "reason": reason, "delivery_day": delivery_day,
            "mail_ids": list(mail_ids)}


def refusals(service, state, cases, now):
    for key, action, code in cases:
        with pytest.raises(ProtocolError) as caught:
            service.resolve_changed(state, key, action, now)
        assert caught.value.code == code


def changed_event(state, campaign_id=EVENT_CAMPAIGN, ordinal=1, now=LOGIN_NOW):
    return begin_event_login(state, campaign_id, ordinal, OLD_DEFINITION, now)


def event_record(state, campaign_id=EVENT_CAMPAIGN):
    return read_event_logins(state)["campaigns"][str(campaign_id)]


def event_resolution(action):
    return {"campaign_id": EVENT_CAMPAIGN, "action": action}


def test_redelivery_mails_a_changed_event_login_reward_under_the_current_definition(bundle, login_save, preflight,
                                                                                   registry):
    resolved, step = migrate(bundle, changed_event(login_save), preflight, registry, RESOLVE_EVENT_LOGIN.operation,
                             event_resolution("redeliver"), {EVENT_LEDGER, *FIRST_MAIL_ROOTS}, LOGIN_NOW)
    current = event_login_definition(bundle.event_logins.catalog.get(EVENT_CAMPAIGN), 1)
    delivery = event_record(resolved)["deliveries"]["1"]
    assert delivery["definition"] == current and delivery["mail_ids"]
    assert step["effects"] == {"campaign_id": EVENT_CAMPAIGN, "action": "redeliver", "ordinal": 1,
                               "earned_day": LOGIN_DAY, "before": slot(SlotStatus.PENDING, OLD_DEFINITION),
                               "after": slot(SlotStatus.DELIVERED, current, LOGIN_DAY, delivery["mail_ids"]),
                               "mail_ids": delivery["mail_ids"]}
    assert step["warnings"] == []
    assert [row["PresentID"] for row in bundle.presents.rows(resolved)] == delivery["mail_ids"]
    bundle.event_logins.validate_receipt(resolved, [{"campaign_id": EVENT_CAMPAIGN, "login_count": 1,
                                                     "purchase_time": "", **delivery}])
    assert bundle.event_logins.settle(resolved, LOGIN_NOW)[1] == []
    refused(bundle, resolved, preflight, registry, RESOLVE_EVENT_LOGIN.operation, event_resolution("redeliver"),
            EVENT_UNNEEDED, 409, LOGIN_NOW)


def test_a_voided_event_login_reward_uses_up_its_count_without_mail(bundle, login_save, preflight, registry):
    opened, grants = bundle.event_logins.settle(login_save, LOGIN_NOW)
    [opening] = [grant for grant in grants if grant["campaign_id"] == EVENT_CAMPAIGN]
    following = LOGIN_NOW + timedelta(days=1)
    voided, step = migrate(bundle, changed_event(opened, ordinal=2, now=following), preflight, registry,
                           RESOLVE_EVENT_LOGIN.operation, event_resolution("void"), {EVENT_LEDGER}, following)
    assert step["effects"]["after"] == slot(SlotStatus.VOIDED, OLD_DEFINITION, NEXT_LOGIN_DAY)
    assert step["warnings"] == [] and event_record(voided) == {"pending": None, "deliveries": {
        "1": event_record(opened)["deliveries"]["1"],
        "2": {"definition": OLD_DEFINITION, "earned_day": NEXT_LOGIN_DAY, "delivery_day": NEXT_LOGIN_DAY,
              "mail_ids": []}}}
    # The earlier 115 receipt still replays, and none can claim the voided count.
    bundle.event_logins.validate_receipt(voided, [opening])
    with pytest.raises(ProtocolError) as caught:
        validate_event_login_receipt(voided, [{**opening, "login_count": 2, **event_record(voided)["deliveries"]["2"]}])
    assert caught.value.code == "event_login_state_invalid"
    assert bundle.event_logins.settle(voided, following + timedelta(hours=1))[1] == []
    _, grants = bundle.event_logins.settle(voided, following + timedelta(days=1))
    assert [(grant["campaign_id"], grant["login_count"]) for grant in grants] == [(EVENT_CAMPAIGN, 3)]
    refused(bundle, voided, preflight, registry, RESOLVE_EVENT_LOGIN.operation, event_resolution("void"),
            EVENT_UNNEEDED, 409, following)


def test_event_login_rewards_of_a_retired_campaign_can_only_be_voided(bundle, login_save):
    gone = changed_event(login_save, RETIRED_CAMPAIGN)
    with pytest.raises(ProtocolError) as caught:
        bundle.event_logins.resolve_changed(gone, RETIRED_CAMPAIGN, PendingAction.REDELIVER, LOGIN_NOW)
    assert (caught.value.code, caught.value.status) == (EVENT_MISSING, 409)
    voided, grants = bundle.event_logins.resolve_changed(gone, RETIRED_CAMPAIGN, PendingAction.VOID, LOGIN_NOW)
    assert grants == [] and event_record(voided, RETIRED_CAMPAIGN)["deliveries"]["1"]["mail_ids"] == []
    refusals(bundle.event_logins, gone, ((EVENT_CAMPAIGN, PendingAction.VOID, EVENT_UNNEEDED),
                                         (0, PendingAction.VOID, "event_login_selection_invalid"),
                                         (RETIRED_CAMPAIGN, "delete", "event_login_action_invalid")), LOGIN_NOW)


def comeback_round(bundle, state, now=LOGIN_NOW):
    comebacks = bundle.comeback_logins
    return comebacks.observe(comebacks.observe(state, now - timedelta(days=COMEBACK_GAP)), now)


def frozen_round(state, round_id=COMEBACK_ROUND, now=LOGIN_NOW):
    # A round frozen under an earlier resource keeps every reward definition it started with.
    changed = begin_comeback_reward(state, round_id, now)
    record = changed[COMEBACK_LEDGER]["rounds"][str(round_id)]
    record["reward_definitions"] = [OLD_DEFINITION] * len(record["reward_definitions"])
    record["pending"]["definition"] = OLD_DEFINITION
    return changed


def comeback_record(state, round_id=COMEBACK_ROUND):
    return read_comeback_logins(state)["rounds"][str(round_id)]


def comeback_resolution(action, round_id=COMEBACK_ROUND):
    return {"round_id": round_id, "action": action}


def test_redelivery_moves_a_frozen_comeback_round_to_the_current_schedule(bundle, login_save, preflight, registry):
    changed = frozen_round(comeback_round(bundle, login_save))
    ordinals = range(1, len(comeback_record(changed)["reward_definitions"]) + 1)
    resolved, step = migrate(bundle, changed, preflight, registry, RESOLVE_COMEBACK_LOGIN.operation,
                             comeback_resolution("redeliver"), {COMEBACK_LEDGER, *FIRST_MAIL_ROOTS}, LOGIN_NOW)
    record = comeback_record(resolved)
    band = bundle.comeback_logins.catalog.require_band(record["pause_period"])
    assert record["reward_definitions"] == [comeback_reward_definition(band, ordinal) for ordinal in ordinals]
    effects, delivery = step["effects"], record["deliveries"]["1"]
    assert effects["after"] == slot(SlotStatus.DELIVERED, record["reward_definitions"][0], LOGIN_DAY,
                                    delivery["mail_ids"])
    assert effects["rebound_ordinals"] == list(ordinals) and effects["still_changed"] == [] and step["warnings"] == []
    assert effects["mail_ids"] == delivery["mail_ids"] and delivery["notify"] is True
    assert bundle.comeback_logins.settle(resolved, LOGIN_NOW)[1] == []
    _, grants = bundle.comeback_logins.settle(resolved, LOGIN_NOW + timedelta(days=1))
    assert [(grant["round_id"], grant["login_count"]) for grant in grants] == [(COMEBACK_ROUND, 2)]
    refused(bundle, resolved, preflight, registry, RESOLVE_COMEBACK_LOGIN.operation, comeback_resolution("redeliver"),
            COMEBACK_UNNEEDED, 409, LOGIN_NOW)


def test_a_voided_comeback_reward_uses_up_its_ordinal_and_leaves_the_rest_frozen(bundle, login_save, preflight,
                                                                               registry):
    changed = frozen_round(comeback_round(bundle, login_save))
    ordinals = len(comeback_record(changed)["reward_definitions"])
    voided, step = migrate(bundle, changed, preflight, registry, RESOLVE_COMEBACK_LOGIN.operation,
                           comeback_resolution("void"), {COMEBACK_LEDGER}, LOGIN_NOW)
    assert comeback_record(voided)["deliveries"] == {"1": {"definition": OLD_DEFINITION, "earned_day": LOGIN_DAY,
                                                           "delivery_day": LOGIN_DAY, "mail_ids": [], "notify": False}}
    assert step["effects"]["after"] == slot(SlotStatus.VOIDED, OLD_DEFINITION, LOGIN_DAY)
    assert step["effects"]["rebound_ordinals"] == []
    assert step["effects"]["still_changed"] == list(range(2, ordinals + 1))
    assert step["warnings"] == [SCHEDULE_STILL_CHANGED]
    same_day, grants = bundle.comeback_logins.settle(voided, LOGIN_NOW + timedelta(hours=1))
    assert grants == [] and comeback_record(same_day)["pending"] is None
    later, grants = bundle.comeback_logins.settle(voided, LOGIN_NOW + timedelta(days=1))
    assert grants == [] and comeback_record(later)["pending"] == {
        "ordinal": 2, "definition": OLD_DEFINITION, "earned_day": NEXT_LOGIN_DAY, "reason": COMEBACK_CHANGED}


def test_comeback_redelivery_waits_while_its_campaign_already_delivered_today(bundle, login_save):
    changed = frozen_round(comeback_round(bundle, login_save))
    returned = LOGIN_NOW + timedelta(days=COMEBACK_GAP)
    current, grants = bundle.comeback_logins.settle(bundle.comeback_logins.observe(changed, returned), returned)
    assert [(grant["round_id"], grant["login_count"]) for grant in grants] == [(NEXT_ROUND, 1)]
    waiting, step = run(bundle, RESOLVE_COMEBACK_LOGIN, current, comeback_resolution("redeliver"), returned)
    assert step.effects["after"]["status"] == SlotStatus.PENDING and step.effects["mail_ids"] == []
    assert step.effects["after"]["reason"] == COMEBACK_DAILY_LIMIT and step.warnings == (STILL_PENDING,)
    _, grants = bundle.comeback_logins.settle(waiting, returned + timedelta(days=1))
    assert [(grant["round_id"], grant["login_count"], grant["notify"]) for grant in grants] == [(COMEBACK_ROUND, 1,
                                                                                                 False)]


def test_comeback_rewards_of_a_band_the_catalog_lost_can_only_be_voided(bundle, login_save):
    lost = frozen_round(comeback_round(bundle, login_save))
    lost[COMEBACK_LEDGER]["rounds"][str(COMEBACK_ROUND)]["pause_period"] = LOST_BAND
    with pytest.raises(ProtocolError) as caught:
        bundle.comeback_logins.resolve_changed(lost, COMEBACK_ROUND, PendingAction.REDELIVER, LOGIN_NOW)
    assert (caught.value.code, caught.value.status) == (COMEBACK_MISSING, 409)
    voided, grants = bundle.comeback_logins.resolve_changed(lost, COMEBACK_ROUND, PendingAction.VOID, LOGIN_NOW)
    assert grants == [] and comeback_record(voided)["deliveries"]["1"]["mail_ids"] == []
    refusals(bundle.comeback_logins, voided, ((COMEBACK_ROUND, PendingAction.VOID, COMEBACK_UNNEEDED),
                                              (0, PendingAction.VOID, "comeback_login_selection_invalid"),
                                              (COMEBACK_ROUND, "delete", "comeback_login_action_invalid")), LOGIN_NOW)


def purchased(bundle, state, campaign_id=None):
    # This generation's local sales are off, so the qualification is written as a purchase would leave it.
    offer = bundle.purchased_logins.catalog.get(PURCHASED_SKU)
    campaign, identifier = offer.campaign, campaign_id or offer.campaign_id
    timestamp = purchased_login_clock(LOGIN_NOW)[1]
    source = [None if day is None else day.isoformat() for day in (campaign.start_date, campaign.end_date)]
    start, end = purchase_window(timestamp, *source, campaign.expiration)
    entry = {"campaign_id": identifier, "identity": purchased_login_identity(identifier), "sku": offer.sku,
             "package_id": offer.package_id, "flag_id": offer.flag_id, "request": REQUEST, "purchased_at": timestamp,
             "purchase_time": purchased_login_time(timestamp), "source_start": source[0], "source_end": source[1],
             "expiration": campaign.expiration, "start_day": start, "end_day": end,
             "reward_definitions": [OLD_DEFINITION] * campaign.max_ordinal, "deliveries": {}, "pending": None}
    bought, _ = add_purchased_login(state, entry, LOGIN_NOW)
    return begin_purchased_reward(bought, PURCHASE, LOGIN_NOW + timedelta(days=1))


def purchase_record(state):
    return read_purchased_logins(state)["purchases"][str(PURCHASE)]


def purchase_resolution(action):
    return {"purchase_id": PURCHASE, "action": action}


def test_redelivery_moves_a_frozen_purchase_to_the_current_schedule(bundle, login_save, preflight, registry):
    changed, following = purchased(bundle, login_save), LOGIN_NOW + timedelta(days=1)
    ordinals = range(1, len(purchase_record(changed)["reward_definitions"]) + 1)
    resolved, step = migrate(bundle, changed, preflight, registry, RESOLVE_PURCHASED_LOGIN.operation,
                             purchase_resolution("redeliver"), {PURCHASED_LEDGER, *FIRST_MAIL_ROOTS}, following)
    record, campaign = purchase_record(resolved), bundle.purchased_logins.catalog.get(PURCHASED_SKU).campaign
    assert record["reward_definitions"] == [event_login_definition(campaign, ordinal) for ordinal in ordinals]
    effects = step["effects"]
    assert effects["after"] == slot(SlotStatus.DELIVERED, record["reward_definitions"][0], NEXT_LOGIN_DAY,
                                    record["deliveries"]["1"]["mail_ids"])
    assert effects["rebound_ordinals"] == list(ordinals) and effects["still_changed"] == [] and step["warnings"] == []
    assert bundle.purchased_logins.settle(resolved, following)[1] == []
    _, grants = bundle.purchased_logins.settle(resolved, following + timedelta(days=1))
    assert [(grant["purchase_id"], grant["login_count"]) for grant in grants] == [(PURCHASE, 2)]
    refused(bundle, resolved, preflight, registry, RESOLVE_PURCHASED_LOGIN.operation, purchase_resolution("void"),
            PURCHASED_UNNEEDED, 409, following)


def test_a_voided_purchased_reward_uses_up_its_ordinal_and_leaves_the_rest_frozen(bundle, login_save, preflight,
                                                                                registry):
    changed, following = purchased(bundle, login_save), LOGIN_NOW + timedelta(days=1)
    ordinals = len(purchase_record(changed)["reward_definitions"])
    voided, step = migrate(bundle, changed, preflight, registry, RESOLVE_PURCHASED_LOGIN.operation,
                           purchase_resolution("void"), {PURCHASED_LEDGER}, following)
    assert purchase_record(voided)["deliveries"] == {"1": {"definition": OLD_DEFINITION, "earned_day": NEXT_LOGIN_DAY,
                                                           "delivery_day": NEXT_LOGIN_DAY, "mail_ids": []}}
    assert step["effects"]["after"] == slot(SlotStatus.VOIDED, OLD_DEFINITION, NEXT_LOGIN_DAY)
    assert step["effects"]["still_changed"] == list(range(2, ordinals + 1))
    assert step["warnings"] == [SCHEDULE_STILL_CHANGED]
    assert bundle.purchased_logins.settle(voided, following + timedelta(hours=1))[1] == []
    later, grants = bundle.purchased_logins.settle(voided, following + timedelta(days=1))
    assert grants == [] and purchase_record(later)["pending"] == {
        "ordinal": 2, "definition": OLD_DEFINITION, "earned_day": THIRD_LOGIN_DAY, "reason": PURCHASED_CHANGED}


def test_purchased_rewards_of_a_retired_campaign_can_only_be_voided(bundle, login_save):
    gone, following = purchased(bundle, login_save, RETIRED_PURCHASE_CAMPAIGN), LOGIN_NOW + timedelta(days=1)
    with pytest.raises(ProtocolError) as caught:
        bundle.purchased_logins.resolve_changed(gone, PURCHASE, PendingAction.REDELIVER, following)
    assert (caught.value.code, caught.value.status) == (PURCHASED_MISSING, 409)
    voided, grants = bundle.purchased_logins.resolve_changed(gone, PURCHASE, PendingAction.VOID, following)
    assert grants == [] and purchase_record(voided)["deliveries"]["1"]["mail_ids"] == []
    refusals(bundle.purchased_logins, voided, ((PURCHASE, PendingAction.VOID, PURCHASED_UNNEEDED),
                                               (True, PendingAction.VOID, "purchased_login_selection_invalid"),
                                               (PURCHASE, "delete", "purchased_login_action_invalid")), following)


def frozen_stamps(bundle, state, *claims):
    # Each claim day holds its stamp under a definition the resources no longer have.
    rewards = bundle.login_rewards
    for moment, grade in claims:
        state, advance = advance_login_rewards(state, moment, beginner_count=rewards.beginner_total)
        state = hold_stamp(state, day=advance.day, grade=grade, month=advance.month, ordinal=advance.ordinal,
                           definition=OLD_DEFINITION, reason=STAMP_CHANGED)
    return state


def stamp_resolution(action, month=STAMP_MONTH):
    return {"month": month, "action": action}


def test_redelivery_mails_the_changed_login_stamps_of_a_month(bundle, login_save, preflight, registry):
    grade = bundle.login_rewards.catalog.grade(0, LOGIN_NOW)
    third = LOGIN_NOW + timedelta(days=2)
    held = frozen_stamps(bundle, login_save, (LOGIN_NOW + timedelta(days=1), grade), (third, UNKNOWN_GRADE))
    resolved, step = migrate(bundle, held, preflight, registry, RESOLVE_LOGIN_STAMPS.operation,
                             stamp_resolution("redeliver"), {STAMP_LEDGER, *FIRST_MAIL_ROOTS}, third)
    mailed, unknown = read_pending_stamps(held)
    effects, rows = step["effects"], bundle.presents.rows(resolved)
    assert effects["resolved"] == [{"day": NEXT_LOGIN_DAY, "before": {"definition": OLD_DEFINITION,
                                                                      "reason": STAMP_CHANGED},
                                    "after": None, "mail_ids": effects["mail_ids"]}]
    assert effects["mail_ids"] == [row["PresentID"] for row in rows] and {row["Description"] for row in rows} == {
        STAMP_DESCRIPTION}
    assert effects["still_changed"] == [THIRD_LOGIN_DAY] and step["warnings"] == [STILL_CHANGED]
    assert read_pending_stamps(resolved) == [unknown] and mailed["day"] == NEXT_LOGIN_DAY
    voided, step = migrate(bundle, resolved, preflight, registry, RESOLVE_LOGIN_STAMPS.operation,
                           stamp_resolution("void"), {STAMP_LEDGER}, third)
    assert STAMP_LEDGER not in voided and step["warnings"] == [] and step["effects"]["mail_ids"] == []
    assert [item["day"] for item in step["effects"]["resolved"]] == [THIRD_LOGIN_DAY]
    assert read_login_rewards(voided) == read_login_rewards(held)
    refused(bundle, voided, preflight, registry, RESOLVE_LOGIN_STAMPS.operation, stamp_resolution("void"),
            STAMP_RESOLVE_UNNEEDED, 409, third)


def test_login_stamps_without_a_current_table_can_only_be_voided(bundle, login_save, preflight, registry):
    grade = bundle.login_rewards.catalog.grade(0, TABLELESS_NOW)
    held = frozen_stamps(bundle, login_save, (TABLELESS_NOW, grade))
    for month in (STAMP_MONTH, TABLELESS_MONTH):
        code = STAMP_RESOLVE_UNNEEDED if month == STAMP_MONTH else STAMP_DEFINITION_MISSING
        refused(bundle, held, preflight, registry, RESOLVE_LOGIN_STAMPS.operation,
                stamp_resolution("redeliver", month), code, 409, TABLELESS_NOW)
    voided, step = migrate(bundle, held, preflight, registry, RESOLVE_LOGIN_STAMPS.operation,
                           stamp_resolution("void", TABLELESS_MONTH), {STAMP_LEDGER}, TABLELESS_NOW)
    assert STAMP_LEDGER not in voided and step["effects"]["still_changed"] == []


def test_login_stamps_the_mailbox_cannot_hold_wait_under_the_current_definition(bundle, login_save, preflight,
                                                                               registry):
    following = LOGIN_NOW + timedelta(days=1)
    held = frozen_stamps(bundle, login_save, (following, bundle.login_rewards.catalog.grade(0, following)))
    full, _ = bundle.presents.enqueue(held, [FILLER] * MAX_PRESENTS)
    waiting, step = run(bundle, RESOLVE_LOGIN_STAMPS, full, stamp_resolution("redeliver"), following)
    [stamp] = read_pending_stamps(waiting)
    current = bundle.login_rewards.catalog.stamp(stamp["grade"], stamp["month"], stamp["ordinal"]).definition
    assert (stamp["definition"], stamp["reason"]) == (current, "present_capacity")
    assert step.effects["mail_ids"] == [] and step.warnings == (STILL_PENDING,)
    refused(bundle, waiting, preflight, registry, RESOLVE_LOGIN_STAMPS.operation, stamp_resolution("redeliver"),
            STAMP_RESOLVE_UNNEEDED, 409, following)


def test_login_stamps_whose_reward_115_cannot_mail_yet_wait_under_the_current_definition(bundle, login_save,
                                                                                        monkeypatch):
    following = LOGIN_NOW + timedelta(days=1)
    catalog = bundle.login_rewards.catalog
    held = frozen_stamps(bundle, login_save, (following, catalog.grade(0, following)))
    [frozen] = read_pending_stamps(held)
    current = catalog.stamp(frozen["grade"], frozen["month"], frozen["ordinal"]).definition
    for code, status in ((TEMPLATE_GAP, 400), (UNSUPPORTED_GAP, 501)):
        monkeypatch.setattr(bundle.presents, "enqueue", fail(code, status))
        waiting, step = run(bundle, RESOLVE_LOGIN_STAMPS, held, stamp_resolution("redeliver"), following)
        assert read_pending_stamps(waiting) == [{**frozen, "definition": current, "reason": code}]
        assert step.effects["resolved"][0]["after"] == {"definition": current, "reason": code}
        assert step.effects["mail_ids"] == [] and step.warnings == (STILL_PENDING,)
    monkeypatch.setattr(bundle.presents, "enqueue", fail("present_state_invalid", 503))
    with pytest.raises(ProtocolError) as caught:
        run(bundle, RESOLVE_LOGIN_STAMPS, held, stamp_resolution("redeliver"), following)
    assert (caught.value.code, caught.value.status) == ("present_state_invalid", 503)

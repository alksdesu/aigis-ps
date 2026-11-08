import ast
import base64
from codecs import BOM_UTF8
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError

import aigis_ps
from aigis_ps import admin_cmd_economy, admin_views_items, admin_views_presents
from aigis_ps.admin_cmd_economy import (CAPACITY, CAPACITY_MONTH, CAPACITY_OVERFLOW, CLASS_ROUTES, COMMANDS,
                                        CRYSTAL_DEBIT, ENERGY, ENQUEUE, ENQUEUE_OPERATION, EXPERIENCE,
                                        EXPERIENCE_CAPPED, FREE_CRYSTAL_CAPACITY, FREE_CRYSTAL_MAIL, FREE_CRYSTALS,
                                        FALLBACK_MAIL_ONLY, GOLD_CHANGED, GOLD_CREDIT, GOLD_DEBIT, GOLD_SET,
                                        GOLD_UNCHANGED, ITEM_CATALOG_REASON, ITEM_CREDIT, ITEM_DEBIT, ITEM_NOT_STOCK,
                                        NOT_DEBITABLE_MESSAGE, NOTHING_DELIVERED, NOTHING_PENDING, PACK_RECEIVE_ACTION,
                                        PACK_TICKET, PAID_CRYSTAL_CAPACITY, PAID_CRYSTALS, PAID_LOCAL, POINT_GRANT,
                                        RANK_RETRY, RANK_RETRY_OPERATION, RANK_REWARDS_WAITING, SKIN_GRANT,
                                        SKIN_GRANT_OPERATION, SKIN_ITEM_INVALID, SKIN_NOT_SOLD, SKIN_ONLY_MESSAGE,
                                        SKIN_OWNED, TICKET_GRANT, UNBOUGHT_PRODUCT, classifier, following_month,
                                        mail_only_items, shop_products, unrouted_items)
from aigis_ps.admin_cmd_settings import SKIN_SHOP_REASON, recorded_skins
from aigis_ps.admin_command import AdminError, Batch, BatchStep, CommandContext, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_diff import json_digest
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, ViewRegistry, discover
from aigis_ps.admin_views_presents import BACKLOG_INVALID, NOTIFICATION_FIELD, NOTIFICATION_POLICY
from aigis_ps.alxml import Column, Container, ValueType, dumps
from aigis_ps.business_calendar import MONTHS_PER_YEAR
from aigis_ps.crystal_wallet import (FREE_CRYSTAL_LIMIT, PAID_CRYSTAL_LIMIT, TOTAL_CRYSTAL_LIMIT, claim_crystal_space,
                                     credit_free_crystals, credit_paid_crystals, wallet_totals)
from aigis_ps.energy_clock import instant
from aigis_ps.item_classes import ItemClass
from aigis_ps.monthly_capacity import read_capacity
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.possessions import MAX_POSSESSION_ITEMS
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.rank_reward_state import read_rank_rewards
from aigis_ps.storage import AUTO_BACKUP_KIND
from aigis_ps.universal_flags import flag_value
from aigis_ps.vote_tickets import DAILY_ITEM as DAILY_VOTE_TICKET
from aigis_ps.wallet_money import GOLD_LIMIT, set_money
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_quest_service import bundle


SOURCE = Path(aigis_ps.__file__).resolve().parent
OPERATIONS = ("capacity.grant_next", "energy.restore", "possessions.credit", "possessions.debit", "presents.enqueue",
              "rank_rewards.retry_pending", "skins.grant", "wallet.crystals.credit_free",
              "wallet.crystals.credit_paid", "wallet.crystals.debit", "wallet.experience.add", "wallet.gold.credit",
              "wallet.gold.debit", "wallet.gold.set")
DESTRUCTIVE = frozenset(("wallet.gold.debit", "wallet.crystals.debit", "possessions.debit"))
FUNDED_GOLD = 6000
VIEWS = ("items.catalog", "items.holdings", "presents.backlog", "presents.mailbox", "presents.receipts")
VIEW_MODULES = ("admin_views_items", "admin_views_presents")
NOTIFICATION_SETTER = "set_notification_category"
PASSIVE, ENTRUST_TICKET = "gift_0002_ruby", "item_consignment_ticket"
ACQUISITION_DAY = 700_000
ITEM_PRESENT, GOLD_PRESENT = 22, 2
LEGACY_BOUQUET, BOUQUET = 3, "gift_0000_bouquet"
FIRST_TEMPLATE, UNKNOWN_TEMPLATE = 2, 999_999_999
FIRST_PACK, LAST_PACK, PACK_COUNT = "item_smn_ticket_90001", "item_smn_ticket_90067", 67
EVENT_PASS_CATEGORY = 7
RANK_UP_EXPERIENCE = 5000
CAPACITY_GRANT = 5
SECRET_RESPONSE = dumps(Container(children=(Container("SYSTEM", (
    Column("A1", ValueType.STRING, ("ExchangeKey",)), Column("A2", ValueType.STRING, ("do-not-leak",)))),)))
ROUTES = {ItemClass.SKIN: "skins.grant", ItemClass.OWNERSHIP: "presents.enqueue",
          ItemClass.EMBLEM: "presents.enqueue", ItemClass.RECOVERY: "presents.enqueue",
          ItemClass.ITEM_TICKET: "presents.enqueue", ItemClass.EVENT_PASS: "event_pass.grant_points",
          ItemClass.VOTE_TICKET: "vote_tickets.grant", ItemClass.STACK_ZERO: None, ItemClass.UNKNOWN: None}
CAPACITY_YEARS = (1970, 2026, 2027, 2099, 3001)
GAME_READBACK = {"game_param": [33, 34, 35]}
CRYSTAL_READBACK = {"crystals": [43, 44]}
PRESENT_READBACK = {"presents": [42]}
RANK_READBACK = {**PRESENT_READBACK, "rank_rewards": [42]}
POSSESSION_READBACK = {"possessions": [129, 137]}


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture
def save(bundle):
    return bundle.initialize(bundle.bootstrap.initial_state())


def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_economy.register(commands)
    return commands


def context(services, day=ACQUISITION_DAY):
    return CommandContext(now=instant(services.bootstrap.clock()), acquisition_day=day, revision=0,
                          generation_id="gen-economy", services=services)


def run(services, command, state, raw, frozen=None):
    outcome = Batch((BatchStep(command, command.parse(raw)),)).apply(state, frozen or context(services))
    return outcome.state, outcome.steps[0]


def refused(services, command, state, raw, frozen=None):
    with pytest.raises(AdminError) as caught:
        run(services, command, state, raw, frozen)
    return caught.value


def console(services, state, preflight):
    store = FakeStore(state)
    generation = FakeGeneration(id="gen-economy", bundle=services, clock=instant(services.bootstrap.clock()))
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def preview(pipeline, command, params):
    return pipeline.preview(registry().batch([{"operation": command.operation, "params": params}]))


def commit(pipeline, plan, confirmations=()):
    return pipeline.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest,
                           expected_revision=plan.expected_revision, op_id=str(uuid.uuid4()),
                           confirmations=confirmations)


def game(state):
    return state["tables"]["game_param"][0]


def holding(state, item_id):
    return next((row["Count"] for row in state["tables"]["possessions"] if row["ItemID"] == item_id), 0)


def without_table(state, table):
    return {**state, "tables": {**state["tables"], table: None}}


def catalog(bundle):
    return bundle.presents.item_catalog


def pack_tickets(bundle):
    return sorted(name for name, row in catalog(bundle).rows.items() if row["ReceiveAction"] == PACK_RECEIVE_ACTION)


def mailable_ticket(bundle):
    enabled = bundle.presents.item_tickets.status()["enabled_items"]
    assert enabled, "No item ticket pool is enabled"
    return enabled[0]


def sold_skins(bundle):
    skins = classifier(bundle).item_ids(ItemClass.SKIN)
    return sorted({product.item_id for product in bundle.skin_shop.catalog.products if product.item_id in skins})


def capacity(bundle, row):
    return bundle.bootstrap.rank_rows[row["Rank"] - 1]["CharismaMax"] + row.get("ExtendedMaxCharisma", 0)


def wallet(save):
    return credit_paid_crystals(credit_free_crystals(save, 300, ACQUISITION_DAY), 200, ACQUISITION_DAY + 1)


def test_economy_modules_register_through_discovery():
    found = discover(PREFLIGHT_DOMAINS)
    assert {"admin_cmd_economy", *VIEW_MODULES} <= set(found.modules)
    assert set(OPERATIONS) <= {command.operation for command in found.commands}
    assert set(VIEWS) <= {view.name for view in found.views}
    assert [command.operation for command in registry()] == list(OPERATIONS)


def test_economy_commands_touch_mapped_domains_and_own_no_shared_field():
    for command in COMMANDS:
        assert command.touched <= set(PREFLIGHT_DOMAINS), command.operation
        assert not command.preflight_none and not command.writes, command.operation
    assert {command.domain for command in COMMANDS} == {"currencies", "account", "items", "presents", "progress"}


def test_only_decreasing_commands_are_destructive_and_name_what_they_take():
    assert {command.operation for command in COMMANDS if command.destructive is True} == DESTRUCTIVE
    assert [command.operation for command in COMMANDS if command.destructive_by_params] == [GOLD_SET.operation]
    assert GOLD_SET.describe()["destructive"] and GOLD_SET.describe()["destructive_by_params"]
    assert GOLD_DEBIT.phrase(GOLD_DEBIT.parse({"amount": 100})) == "DEBIT 100 GOLD"
    assert GOLD_SET.phrase(GOLD_SET.parse({"value": 0, "current": 1})) == "SET GOLD TO 0"
    assert CRYSTAL_DEBIT.phrase(CRYSTAL_DEBIT.parse({"amount": 1})) == "DEBIT 1 CRYSTAL FREE FIRST"
    paid_only = CRYSTAL_DEBIT.parse({"amount": 50, "order": "paid_only"})
    assert CRYSTAL_DEBIT.phrase(paid_only) == "DEBIT 50 CRYSTALS PAID ONLY"
    items = ITEM_DEBIT.parse({"items": [{"item_id": PASSIVE, "count": 3}, {"item_id": ENTRUST_TICKET, "count": 1}]})
    assert ITEM_DEBIT.phrase(items) == "DEBIT 2 ITEMS"


@pytest.mark.parametrize("value, current, destructive", [(0, 1, True), (FUNDED_GOLD - 1, FUNDED_GOLD, True),
                                                       (FUNDED_GOLD, FUNDED_GOLD, False),
                                                       (FUNDED_GOLD + 1, FUNDED_GOLD, False)])
def test_gold_sets_are_destructive_only_when_they_lower_the_balance(value, current, destructive):
    assert GOLD_SET.is_destructive(GOLD_SET.parse({"value": value, "current": current})) is destructive


def test_no_console_command_writes_notification_categories():
    commands = discover(PREFLIGHT_DOMAINS).commands
    assert not any(field.endswith(f".{NOTIFICATION_FIELD}") for command in commands for field in command.writes)
    for path in sorted(SOURCE.glob("admin_cmd_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            assert not (isinstance(node, ast.Attribute) and node.attr == NOTIFICATION_SETTER), path.name
            assert not (isinstance(node, ast.Name) and node.id == NOTIFICATION_SETTER), path.name
            assert not (isinstance(node, ast.Constant) and node.value == NOTIFICATION_FIELD), path.name


@pytest.mark.parametrize("command, params", [
    (GOLD_CREDIT, {"amount": 0}), (GOLD_CREDIT, {"amount": -1}), (GOLD_CREDIT, {"amount": True}),
    (GOLD_CREDIT, {"amount": "5"}), (GOLD_CREDIT, {"amount": 1.0}), (GOLD_CREDIT, {"amount": INT32_MAX + 1}),
    (GOLD_CREDIT, {}), (GOLD_CREDIT, {"amount": 5, "cap": GOLD_LIMIT}),
    (GOLD_SET, {"value": -1, "current": 0}), (GOLD_SET, {"value": INT32_MAX + 1, "current": 0}),
    (GOLD_SET, {"amount": 5}), (GOLD_SET, {"value": 5}), (GOLD_SET, {"value": 5, "current": -1}),
    (GOLD_SET, {"value": 5, "current": INT32_MAX + 1}), (GOLD_SET, {"value": 5, "current": "5"}),
    (CRYSTAL_DEBIT, {"amount": 1, "order": "oldest_first"}), (CRYSTAL_DEBIT, {"amount": 1, "order": 1}),
    (CRYSTAL_DEBIT, {"amount": 1, "spending": True}),
    (CAPACITY, {"amount": 1}), (CAPACITY, {"amount": 1, "month": "2026-13"}),
    (CAPACITY, {"amount": 1, "month": "2026-1"}), (CAPACITY, {"amount": 1, "month": "26-12"}),
    (CAPACITY, {"amount": 1, "month": "2026-12\n"}), (CAPACITY, {"amount": 0, "month": "2026-12"}),
    (ITEM_CREDIT, {"items": []}), (ITEM_CREDIT, {"items": [{"item_id": "", "count": 1}]}),
    (ITEM_CREDIT, {"items": [{"item_id": PASSIVE, "count": 0}]}),
    (ITEM_CREDIT, {"items": [{"item_id": PASSIVE, "count": 1}, {"item_id": PASSIVE, "count": 2}]}),
    (ITEM_CREDIT, {"items": [{"item_id": f"item_{index}", "count": 1} for index in range(MAX_POSSESSION_ITEMS + 1)]}),
    (ITEM_DEBIT, {"items": [{"item_id": PASSIVE}]}),
    (SKIN_GRANT, {"item_id": "get_skin_00001"}), (SKIN_GRANT, {"item_id": "get_skin_00001", "purchase_flag": 1}),
    (ENQUEUE, {"rewards": []}), (ENQUEUE, {"rewards": [{"kind": "crystal", "amount": 1}]}),
    (ENQUEUE, {"rewards": [{"kind": "gold", "amount": 1, "item_id": PASSIVE}]}),
    (ENQUEUE, {"rewards": [{"kind": "gold", "amount": 1}] * (MAX_PRESENTS + 1)}),
    (ENQUEUE, {"rewards": [{"kind": "item", "item_id": PASSIVE, "amount": 1, NOTIFICATION_FIELD: 7}]}),
    (ENERGY, {"amount": 1}), (RANK_RETRY, {"force": True}), (EXPERIENCE, {"amount": 0}),
])
def test_economy_parameters_are_strict(command, params):
    with pytest.raises(AdminError) as caught:
        command.parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


@pytest.mark.parametrize("period, following", [("2026-11", "2026-12"), ("2026-12", "2027-01"),
                                               ("2027-01", "2027-02")])
def test_capacity_months_follow_the_calendar(period, following):
    assert following_month(period) == following


def carried_month(period: str) -> str:
    year, month = map(int, period.split("-"))
    return f"{year + month // MONTHS_PER_YEAR:04d}-{month % MONTHS_PER_YEAR + 1:02d}"


@pytest.mark.parametrize("year", CAPACITY_YEARS)
def test_capacity_months_match_the_carried_month_text_in_every_month(year):
    periods = [f"{year:04d}-{month:02d}" for month in range(1, MONTHS_PER_YEAR + 1)]
    assert [following_month(period) for period in periods] == [carried_month(period) for period in periods]


def test_commands_name_the_service_they_are_missing():
    empty = SimpleNamespace()
    assert EXPERIENCE.availability(empty) == missing_service("quests")
    assert EXPERIENCE.availability(SimpleNamespace(quests=object())) == missing_service("rank_rewards")
    assert ENERGY.availability(empty) == missing_service("recovery")
    assert CAPACITY.availability(empty) == missing_service("sugoroku")
    assert RANK_RETRY.availability(empty) == missing_service("rank_rewards")
    without_items = SimpleNamespace(presents=SimpleNamespace(item_catalog=None), missions=object())
    assert ITEM_CREDIT.availability(without_items) == ITEM_DEBIT.availability(without_items) == ITEM_CATALOG_REASON
    assert SKIN_GRANT.availability(without_items) == ITEM_CATALOG_REASON
    with_items = SimpleNamespace(presents=SimpleNamespace(item_catalog=object()))
    assert SKIN_GRANT.availability(with_items) == missing_service("missions")
    assert all(command.availability(empty) is None for command in (GOLD_CREDIT, FREE_CRYSTALS, ENQUEUE))


def test_every_command_is_available_with_the_verified_services(bundle):
    assert [command.operation for command in COMMANDS if command.availability(bundle) is not None] == []


def test_gold_credits_and_balances_stop_at_the_shared_limit(bundle, save):
    money = game(save)["Money"]
    updated, step = run(bundle, GOLD_CREDIT, save, {"amount": 500})
    assert game(updated)["Money"] == money + 500
    assert dict(step.effects) == {"before": money, "after": money + 500, "limit": GOLD_LIMIT} and step.warnings == ()
    full, _ = run(bundle, GOLD_CREDIT, save, {"amount": GOLD_LIMIT - money})
    assert game(full)["Money"] == GOLD_LIMIT
    error = refused(bundle, GOLD_CREDIT, full, {"amount": 1})
    assert (error.code, error.status) == ("admin_gold_capacity", 409)
    capped, _ = run(bundle, GOLD_SET, save, {"value": GOLD_LIMIT, "current": money})
    assert game(capped)["Money"] == GOLD_LIMIT
    assert refused(bundle, GOLD_SET, save, {"value": GOLD_LIMIT + 1, "current": money}).code == "admin_gold_capacity"
    assert refused(bundle, GOLD_CREDIT, save, {"amount": GOLD_LIMIT}).code == "admin_gold_capacity"


def test_a_balance_saved_above_the_limit_only_moves_down(bundle, save):
    legacy = deepcopy(save)
    saved = game(legacy)["Money"] = GOLD_LIMIT + 100
    assert refused(bundle, GOLD_CREDIT, legacy, {"amount": 1}).code == "admin_gold_capacity"
    assert refused(bundle, GOLD_SET, legacy, {"value": saved + 1, "current": saved}).code == "admin_gold_capacity"
    lowered, step = run(bundle, GOLD_DEBIT, legacy, {"amount": 50})
    assert game(lowered)["Money"] == GOLD_LIMIT + 50 and step.effects["before"] == GOLD_LIMIT + 100
    still_above, _ = run(bundle, GOLD_SET, legacy, {"value": GOLD_LIMIT + 10, "current": saved})
    assert game(still_above)["Money"] == GOLD_LIMIT + 10
    unchanged, step = run(bundle, GOLD_SET, legacy, {"value": saved, "current": saved})
    assert unchanged == legacy and step.warnings == (GOLD_UNCHANGED,)
    assert dict(step.effects) == {"before": saved, "after": saved, "limit": GOLD_LIMIT}


def test_gold_sets_refuse_a_balance_that_changed_since_the_edit_started(bundle, save):
    money = game(save)["Money"]
    for current in (money - 1, money + 1):
        error = refused(bundle, GOLD_SET, save, {"value": money + 10, "current": current})
        assert (error.code, error.status) == (GOLD_CHANGED, 409)
        assert error.details == {"money": money, "current": current, "step": 0, "operation": GOLD_SET.operation}


def test_gold_keeps_the_native_balance_rule(bundle, save):
    money = game(save)["Money"]
    assert game(save)["Experience"] == 0
    for command, params in ((GOLD_DEBIT, {"amount": money}), (GOLD_SET, {"value": 0, "current": money})):
        error = refused(bundle, command, save, params)
        assert (error.code, error.status) == ("admin_gold_native_balance", 409)
    assert refused(bundle, GOLD_DEBIT, save, {"amount": money + 1}).code == "admin_gold_insufficient"
    experienced = deepcopy(save)
    game(experienced)["Experience"] = 1
    emptied, _ = run(bundle, GOLD_DEBIT, experienced, {"amount": money})
    assert game(emptied)["Money"] == 0


def test_free_crystals_are_added_only_as_far_as_one_claimed_mail_could(bundle, save):
    updated, step = run(bundle, FREE_CRYSTALS, save, {"amount": 600})
    assert updated["tables"]["crystals"] == [{"FreeFlag": True, "Num": 600, "Date": ACQUISITION_DAY}]
    assert without_table(updated, "crystals") == without_table(save, "crystals")
    assert step.effects["totals"] == {"before": {"paid": 0, "free": 0, "total": 0},
                                      "after": {"paid": 0, "free": 600, "total": 600}}
    assert step.effects["acquisition_day"] == ACQUISITION_DAY and step.warnings == ()
    error = refused(bundle, FREE_CRYSTALS, updated, {"amount": FREE_CRYSTAL_LIMIT - 600 + 1})
    assert (error.code, error.status) == (FREE_CRYSTAL_CAPACITY, 409) and "free_crystal" in str(error)
    assert error.details["space"] == claim_crystal_space(updated) == FREE_CRYSTAL_LIMIT - 600
    filled, _ = run(bundle, FREE_CRYSTALS, updated, {"amount": FREE_CRYSTAL_LIMIT - 600})
    assert wallet_totals(filled)["free"] == FREE_CRYSTAL_LIMIT
    # Like a claim, a paid balance saved above its cap blocks free crystals too.
    legacy = credit_paid_crystals(save, PAID_CRYSTAL_LIMIT + 1, ACQUISITION_DAY)
    assert refused(bundle, FREE_CRYSTALS, legacy, {"amount": 1}).details["space"] == 0


def test_paid_crystals_are_local_and_stop_at_the_client_wallet_caps(bundle, save):
    updated, step = run(bundle, PAID_CRYSTALS, save, {"amount": PAID_CRYSTAL_LIMIT})
    assert updated["tables"]["crystals"] == [{"FreeFlag": False, "Num": PAID_CRYSTAL_LIMIT, "Date": ACQUISITION_DAY}]
    assert step.effects["local_operation"] is True and step.warnings == (PAID_LOCAL,)
    # No purchase is recorded, so monthly spending and everything outside the wallet stay as they were.
    assert without_table(updated, "crystals") == without_table(save, "crystals")
    error = refused(bundle, PAID_CRYSTALS, updated, {"amount": 1})
    assert (error.code, error.status) == (PAID_CRYSTAL_CAPACITY, 409) and error.details["space"] == 0
    free = credit_free_crystals(save, FREE_CRYSTAL_LIMIT, ACQUISITION_DAY)
    room = TOTAL_CRYSTAL_LIMIT - FREE_CRYSTAL_LIMIT
    assert refused(bundle, PAID_CRYSTALS, free, {"amount": room + 1}).details["space"] == room
    topped, _ = run(bundle, PAID_CRYSTALS, free, {"amount": room})
    assert wallet_totals(topped) == {"paid": PAID_CRYSTAL_LIMIT, "free": FREE_CRYSTAL_LIMIT,
                                     "total": TOTAL_CRYSTAL_LIMIT}


@pytest.mark.parametrize("order, amount, totals", [
    ("free_first", 350, {"paid": 150, "free": 0, "total": 150}),
    ("paid_first", 250, {"paid": 0, "free": 250, "total": 250}),
    ("paid_only", 150, {"paid": 50, "free": 300, "total": 350}),
])
def test_crystal_debits_follow_their_order_and_leave_spending_alone(bundle, save, order, amount, totals):
    funded = wallet(save)
    updated, step = run(bundle, CRYSTAL_DEBIT, funded, {"amount": amount, "order": order})
    assert wallet_totals(updated) == step.effects["totals"]["after"] == totals and step.effects["order"] == order
    # A console debit is no purchase, so monthly spending and its projection stay as they were.
    assert without_table(updated, "crystals") == without_table(funded, "crystals")


def test_crystal_debits_refuse_more_than_their_order_may_take(bundle, save):
    funded = wallet(save)
    for params in ({"amount": 201, "order": "paid_only"}, {"amount": 501}):
        error = refused(bundle, CRYSTAL_DEBIT, funded, params)
        assert (error.code, error.status) == ("crystal_insufficient", 409)


def test_experience_crosses_ranks_and_mails_rank_rewards_in_the_same_result(bundle, save):
    before = game(save)
    updated, step = run(bundle, EXPERIENCE, save, {"amount": RANK_UP_EXPERIENCE})
    after, effects = game(updated), step.effects
    assert after["Experience"] == RANK_UP_EXPERIENCE and after["Rank"] > before["Rank"]
    assert effects["experience"] == {"before": 0, "after": RANK_UP_EXPERIENCE, "requested": RANK_UP_EXPERIENCE,
                                     "added": RANK_UP_EXPERIENCE}
    assert effects["rank"] == {"before": before["Rank"], "after": after["Rank"]}
    refill = capacity(bundle, after) if bundle.quests.rules.refill_on_rank_up else before["NowCharisma"]
    assert effects["charisma"] == {"before": before["NowCharisma"], "after": max(before["NowCharisma"], refill)}
    outcome = effects["rank_rewards"]
    assert outcome["delivered"]
    assert outcome["mail_ids"] == [row["PresentID"] for row in updated["tables"]["presents"]]
    ledger = read_rank_rewards(updated)
    assert ledger["observed_rank"] == after["Rank"]
    entries = ledger["entries"]
    assert all(entries[str(entry["bonus_id"])]["mail_ids"] == entry["mail_ids"] for entry in outcome["delivered"])
    assert all(entries[str(entry["bonus_id"])]["status"] == "pending" for entry in outcome["pending"])
    assert step.warnings == ((RANK_REWARDS_WAITING,) if outcome["pending"] else ())


def test_experience_stops_one_point_below_the_last_rank_threshold(bundle, save):
    rank_rows = bundle.bootstrap.rank_rows
    updated, step = run(bundle, EXPERIENCE, save, {"amount": INT32_MAX})
    last = sum(row["NextExp"] for row in rank_rows) - 1
    assert game(updated)["Experience"] == step.effects["experience"]["added"] == last
    assert game(updated)["Rank"] == len(rank_rows) and step.warnings[0] == EXPERIENCE_CAPPED


def test_energy_restore_adds_one_full_capacity_within_the_charisma_limit(bundle, save):
    row, limit = game(save), bundle.recovery.rules.charisma_limit
    updated, step = run(bundle, ENERGY, save, {})
    assert game(updated)["NowCharisma"] == row["NowCharisma"] + capacity(bundle, row)
    assert dict(step.effects) == {"charisma": {"before": row["NowCharisma"], "after": game(updated)["NowCharisma"]},
                                  "limit": limit}
    # The console restores for free, so the wallet and monthly spending stay as they were.
    assert updated["tables"]["crystals"] == save["tables"]["crystals"]
    assert updated["crystal_spending"] == save["crystal_spending"]
    crowded = deepcopy(save)
    game(crowded)["NowCharisma"] = limit - capacity(bundle, row) + 1
    error = refused(bundle, ENERGY, crowded, {})
    assert (error.code, error.status) == ("recovery_cap_exceeded", 409)


def test_capacity_grants_name_the_month_they_take_effect(bundle, save):
    frozen = context(bundle)
    error = refused(bundle, CAPACITY, save, {"amount": CAPACITY_GRANT, "month": "2000-01"}, frozen)
    assert (error.code, error.status) == (CAPACITY_MONTH, 422) and error.fields[0]["loc"] == ["month"]
    business, effective = error.details["business_month"], error.details["effective_month"]
    assert effective == following_month(business)
    current = refused(bundle, CAPACITY, save, {"amount": CAPACITY_GRANT, "month": business}, frozen)
    assert current.code == CAPACITY_MONTH
    updated, step = run(bundle, CAPACITY, save, {"amount": CAPACITY_GRANT, "month": effective}, frozen)
    ledger = read_capacity(updated)
    assert (ledger["period"], ledger["next"]) == (business, CAPACITY_GRANT)
    assert (game(updated)["ExtendedMaxCharisma"], game(updated)["ExtendedMaxCharismaNextMonth"]) == (0, CAPACITY_GRANT)
    assert dict(step.effects) == {"business_month": business, "effective_month": effective, "current": 0,
                                  "next": {"before": 0, "after": CAPACITY_GRANT}}


def test_capacity_grants_refuse_a_next_month_past_int32(bundle, save):
    month = following_month(read_capacity(save)["period"])
    error = refused(bundle, CAPACITY, save, {"amount": INT32_MAX, "month": month})
    assert (error.code, error.status) == (CAPACITY_OVERFLOW, 409)


def test_possession_credits_take_only_passive_stock(bundle, save):
    updated, step = run(bundle, ITEM_CREDIT, save, {"items": [{"item_id": PASSIVE, "count": 3}]})
    assert holding(updated, PASSIVE) == 3
    assert step.effects["items"] == [{"ItemID": PASSIVE, "before": 0, "after": 3,
                                      "stack": catalog(bundle).rows[PASSIVE]["Stack"]}]
    classes, skipped = classifier(bundle), {*pack_tickets(bundle), *unrouted_items(bundle)}
    samples = {item_class: next(name for name in sorted(classes.item_ids(item_class)) if name not in skipped)
               for item_class in ROUTES if classes.item_ids(item_class)}
    error = refused(bundle, ITEM_CREDIT, save, {"items": [{"item_id": name, "count": 1} for name in samples.values()]})
    assert (error.code, error.status) == (ITEM_NOT_STOCK, 422)
    assert error.details["items"] == [{"item_id": name, "class": item_class.value, "use": ROUTES[item_class]}
                                      for item_class, name in samples.items()]
    unknown = refused(bundle, ITEM_CREDIT, save, {"items": [{"item_id": "missing_item", "count": 1}]})
    assert unknown.code == "item_class_unknown_item"
    stack = catalog(bundle).rows[PASSIVE]["Stack"]
    overfull = refused(bundle, ITEM_CREDIT, updated, {"items": [{"item_id": PASSIVE, "count": stack}]})
    assert overfull.code == "possession_stack_exceeded"


def test_every_item_route_names_a_registered_command():
    assert set(CLASS_ROUTES.values()) <= {command.operation for command in discover(PREFLIGHT_DOMAINS).commands}
    assert (CLASS_ROUTES[ItemClass.EVENT_PASS], CLASS_ROUTES[ItemClass.VOTE_TICKET]) == (POINT_GRANT, TICKET_GRANT)


@pytest.mark.parametrize("hint, command, operation", [
    (FREE_CRYSTAL_MAIL, ENQUEUE, ENQUEUE_OPERATION),
    (RANK_REWARDS_WAITING, RANK_RETRY, RANK_RETRY_OPERATION),
    (SKIN_ONLY_MESSAGE, SKIN_GRANT, SKIN_GRANT_OPERATION),
])
def test_hints_name_the_command_registered_from_the_same_constant(hint, command, operation):
    # Equal literals in one module compile to one object, so only the source shows a name spelled twice.
    spelled = [node for node in ast.walk(ast.parse(Path(admin_cmd_economy.__file__).read_bytes()))
               if isinstance(node, ast.Constant) and isinstance(node.value, str) and operation in node.value]
    assert command.operation is operation and operation in hint and len(spelled) == 1


def test_pass_points_and_group_tickets_route_per_item(bundle, save):
    entry = bundle.event_passes.catalog.passes[0]
    groups = sorted(classifier(bundle).item_ids(ItemClass.VOTE_TICKET) - {DAILY_VOTE_TICKET})
    assert groups and unrouted_items(bundle) >= {entry.premium_item_id, DAILY_VOTE_TICKET}
    expected = [(entry.item_id, ItemClass.EVENT_PASS, POINT_GRANT), (entry.premium_item_id, ItemClass.EVENT_PASS, None),
                (groups[0], ItemClass.VOTE_TICKET, TICKET_GRANT), (DAILY_VOTE_TICKET, ItemClass.VOTE_TICKET, None)]
    error = refused(bundle, ITEM_CREDIT, save, {"items": [{"item_id": name, "count": 1} for name, _, _ in expected]})
    assert error.details["items"] == [{"item_id": name, "class": item_class.value, "use": route}
                                      for name, item_class, route in expected]
    stocked = bundle.possessions.credit(save, {name: 1 for name, _, _ in expected})
    listing = read_view(bundle, "items.holdings", stocked)
    assert [(row["ItemID"], row["command"]) for row in listing["items"]["items"]] == [
        (name, route) for name, _, route in expected]
    assert unrouted_items(SimpleNamespace()) == {DAILY_VOTE_TICKET}


def test_pack_tickets_have_no_console_route(bundle, save):
    error = refused(bundle, ITEM_CREDIT, save, {"items": [{"item_id": FIRST_PACK, "count": 1}]})
    assert error.details["items"] == [{"item_id": FIRST_PACK, "class": ItemClass.ITEM_TICKET.value, "use": None}]


def test_entrust_tickets_are_topped_up_only_by_mail(bundle, save):
    assert bundle.entrust is None or bundle.entrust.catalog.ticket_item_ids == FALLBACK_MAIL_ONLY
    assert mail_only_items(bundle) == mail_only_items(SimpleNamespace()) == FALLBACK_MAIL_ONLY == {ENTRUST_TICKET}
    listed = SimpleNamespace(entrust=SimpleNamespace(catalog=SimpleNamespace(ticket_item_ids=frozenset({PASSIVE}))))
    assert mail_only_items(listed) == {PASSIVE}
    assert classifier(bundle).classify(ENTRUST_TICKET) is ItemClass.PASSIVE
    error = refused(bundle, ITEM_CREDIT, save, {"items": [{"item_id": ENTRUST_TICKET, "count": 1}]})
    assert error.details["items"] == [{"item_id": ENTRUST_TICKET, "class": "passive", "use": ENQUEUE.operation}]
    mailed, step = run(bundle, ENQUEUE, save, {"rewards": [{"kind": "item", "item_id": ENTRUST_TICKET, "amount": 5,
                                                            "description": "Entrust tickets"}]})
    assert [(row["Type"], row["Content"], row["ItemID"]) for row in mailed["tables"]["presents"]] == [
        (ITEM_PRESENT, 5, ENTRUST_TICKET)]
    assert holding(mailed, ENTRUST_TICKET) == 0 and step.effects["rewards"][0]["mails"] == 1


def test_an_entrust_catalog_listing_no_ticket_keeps_the_verified_ticket_mail_only():
    unlisted = SimpleNamespace(entrust=SimpleNamespace(catalog=SimpleNamespace(ticket_item_ids=frozenset())))
    assert mail_only_items(unlisted) == FALLBACK_MAIL_ONLY == {ENTRUST_TICKET}


def test_possession_debits_take_passive_stock_including_entrust_tickets(bundle, save):
    stocked = bundle.possessions.credit(save, {PASSIVE: 3, ENTRUST_TICKET: 2})
    updated, step = run(bundle, ITEM_DEBIT, stocked, {"items": [{"item_id": PASSIVE, "count": 1},
                                                                {"item_id": ENTRUST_TICKET, "count": 2}]})
    assert (holding(updated, PASSIVE), holding(updated, ENTRUST_TICKET)) == (2, 0)
    assert [(entry["ItemID"], entry["before"], entry["after"]) for entry in step.effects["items"]] == [
        (PASSIVE, 3, 2), (ENTRUST_TICKET, 2, 0)]
    error = refused(bundle, ITEM_DEBIT, stocked, {"items": [{"item_id": PASSIVE, "count": 4}]})
    assert (error.code, error.status) == ("possession_insufficient", 409)
    skin = sold_skins(bundle)[0]
    owned = bundle.possessions.credit(save, {skin: 1})
    error = refused(bundle, ITEM_DEBIT, owned, {"items": [{"item_id": skin, "count": 1}]})
    assert (error.code, error.status, str(error)) == (ITEM_NOT_STOCK, 422, NOT_DEBITABLE_MESSAGE)
    # No console command takes skins or other non-stock items away, so a refused debit names none.
    assert error.details["items"] == [{"item_id": skin, "class": ItemClass.SKIN.value, "use": None}]


def test_skin_grants_record_the_acquisition_and_set_the_chosen_purchase_flag(bundle, save):
    skin = sold_skins(bundle)[0]
    products = shop_products(bundle.skin_shop, skin)
    updated, step = run(bundle, SKIN_GRANT, save, {"item_id": skin, "purchase_flag": True})
    assert holding(updated, skin) == 1 and step.warnings == ()
    assert all(flag_value(updated, product.flag_name) == 1 for product in products)
    assert step.effects["purchase_flags"] == [{"ID": product.flag_name, "shop_id": product.shop_id, "before": 0,
                                               "after": 1} for product in products]
    assert step.effects["shop_ids"] == [product.shop_id for product in products]
    counted = skin in bundle.missions.skins.counted_item_ids
    assert step.effects["acquisition_recorded"] is counted and (skin in recorded_skins(bundle, updated)) is counted


def test_skin_grants_without_the_purchase_flag_warn_about_the_shop(bundle, save):
    skin = sold_skins(bundle)[1]
    products = shop_products(bundle.skin_shop, skin)
    updated, step = run(bundle, SKIN_GRANT, save, {"item_id": skin, "purchase_flag": False})
    assert holding(updated, skin) == 1 and step.effects["purchase_flags"] == []
    assert all(flag_value(updated, product.flag_name) == 0 for product in products)
    assert step.warnings == tuple(UNBOUGHT_PRODUCT.format(shop_id=product.shop_id, item_id=skin)
                                  for product in products)


def test_skin_grants_refuse_owned_unsold_and_other_items(bundle, save):
    skins, sold = classifier(bundle).item_ids(ItemClass.SKIN), sold_skins(bundle)
    owned, _ = run(bundle, SKIN_GRANT, save, {"item_id": sold[0], "purchase_flag": False})
    error = refused(bundle, SKIN_GRANT, owned, {"item_id": sold[0], "purchase_flag": True})
    assert (error.code, error.status) == (SKIN_OWNED, 409)
    unsold = sorted(skins - set(sold))
    assert unsold
    error = refused(bundle, SKIN_GRANT, save, {"item_id": unsold[0], "purchase_flag": True})
    assert (error.code, error.status) == (SKIN_NOT_SOLD, 422) and error.fields[0]["loc"] == ["purchase_flag"]
    granted, step = run(bundle, SKIN_GRANT, save, {"item_id": unsold[0], "purchase_flag": False})
    assert holding(granted, unsold[0]) == 1 and step.warnings == () and step.effects["shop_ids"] == []
    error = refused(bundle, SKIN_GRANT, save, {"item_id": PASSIVE, "purchase_flag": False})
    assert (error.code, error.status) == (SKIN_ITEM_INVALID, 422)
    assert (error.details["class"], error.details["use"]) == ("passive", ITEM_CREDIT.operation)


def test_skin_grants_need_the_mission_service(bundle, save):
    services = replace(bundle, missions=None)
    assert SKIN_GRANT.availability(services) == missing_service("missions")
    error = refused(services, SKIN_GRANT, save, {"item_id": sold_skins(bundle)[0], "purchase_flag": False})
    assert (error.code, error.status) == ("admin_command_unavailable", 503)


def test_skin_grants_need_the_skin_shop_only_to_set_purchase_flags(bundle, save):
    services, skin = replace(bundle, skin_shop=None), sold_skins(bundle)[0]
    assert SKIN_GRANT.availability(services) is None
    error = refused(services, SKIN_GRANT, save, {"item_id": skin, "purchase_flag": True})
    assert (error.code, error.status, str(error)) == ("admin_command_unavailable", 503, SKIN_SHOP_REASON)
    granted, step = run(services, SKIN_GRANT, save, {"item_id": skin, "purchase_flag": False})
    assert holding(granted, skin) == 1 and step.effects["shop_ids"] == [] and step.warnings == ()


def test_present_mail_splits_item_tickets_into_single_mails(bundle, save):
    ticket = mailable_ticket(bundle)
    updated, step = run(bundle, ENQUEUE, save, {"rewards": [
        {"kind": "item", "item_id": ticket, "amount": 3, "description": "Tickets"},
        {"kind": "gold", "amount": 100, "description": "Gold"}]})
    rows = updated["tables"]["presents"]
    assert [(row["Type"], row["Content"], row.get("ItemID")) for row in rows] == [(ITEM_PRESENT, 1, ticket)] * 3 + [
        (GOLD_PRESENT, 100, None)]
    assert step.effects["rewards"] == [{"kind": "item", "mails": 3, "present_ids": [1, 2, 3]},
                                       {"kind": "gold", "mails": 1, "present_ids": [4]}]
    assert step.effects["mailbox"] == {"before": 0, "after": 4, "capacity": MAX_PRESENTS}
    assert all(NOTIFICATION_FIELD not in row for row in rows)


def test_pack_tickets_are_never_mailed_by_hand(bundle, save):
    packs = pack_tickets(bundle)
    assert (packs[0], packs[-1], len(packs)) == (FIRST_PACK, LAST_PACK, PACK_COUNT)
    rewards = [{"kind": "gold", "amount": 1}, *({"kind": "item", "item_id": name, "amount": 1} for name in packs)]
    error = refused(bundle, ENQUEUE, save, {"rewards": rewards})
    assert (error.code, error.status) == (PACK_TICKET, 422) and error.details["items"] == packs
    recovery = refused(bundle, ENQUEUE, save, {"rewards": [{"kind": "charisma_recovery", "item_id": FIRST_PACK}]})
    assert recovery.code == PACK_TICKET


def test_present_refusals_name_the_reward_they_belong_to(bundle, save):
    error = refused(bundle, ENQUEUE, save, {"rewards": [
        {"kind": "gold", "amount": 1}, {"kind": "unit", "template_id": FIRST_TEMPLATE, "count": MAX_PRESENTS}]})
    assert (error.code, error.status, error.details["reward"]) == ("present_capacity", 409, 1)
    error = refused(bundle, ENQUEUE, save, {"rewards": [{"kind": "unit", "template_id": UNKNOWN_TEMPLATE,
                                                         "count": 1}]})
    assert (error.code, error.details["reward"]) == ("present_template_unsupported", 0)


def test_rank_reward_retry_delivers_what_now_fits(bundle, save):
    full, _ = bundle.presents.enqueue(save, [{"kind": "gold", "amount": 1, "description": "Filler"}] * MAX_PRESENTS)
    ranked, step = run(bundle, EXPERIENCE, full, {"amount": RANK_UP_EXPERIENCE})
    waiting = step.effects["rank_rewards"]
    assert waiting["mail_ids"] == [] and waiting["pending"] and RANK_REWARDS_WAITING in step.warnings
    assert {entry["reason"] for entry in waiting["pending"]} == {"present_capacity"}
    # Claiming every filler mail empties the box without touching the rank reward ledger.
    emptied = deepcopy(ranked)
    emptied["tables"]["presents"] = []
    retried, step = run(bundle, RANK_RETRY, emptied, {})
    assert step.effects["delivered"] and step.warnings == ()
    assert step.effects["mail_ids"] == [row["PresentID"] for row in retried["tables"]["presents"]]
    assert all(entry["reason"] != "present_capacity" for entry in step.effects["pending"])
    again, step = run(bundle, RANK_RETRY, retried, {})
    assert step.effects["mail_ids"] == [] and again["tables"]["presents"] == retried["tables"]["presents"]
    assert step.warnings == ((NOTHING_DELIVERED,) if step.effects["pending"] else (NOTHING_PENDING,))
    _, step = run(bundle, RANK_RETRY, save, {})
    assert dict(step.effects) == {"mail_ids": [], "delivered": [], "pending": []}
    assert step.warnings == (NOTHING_PENDING,)


@pytest.mark.parametrize("command, params, readback", [
    (GOLD_CREDIT, {"amount": 500}, GAME_READBACK),
    (FREE_CRYSTALS, {"amount": 300}, CRYSTAL_READBACK),
    (PAID_CRYSTALS, {"amount": 200}, CRYSTAL_READBACK),
    (EXPERIENCE, {"amount": RANK_UP_EXPERIENCE}, {**GAME_READBACK, **RANK_READBACK}),
    (ENERGY, {}, GAME_READBACK),
    (ITEM_CREDIT, {"items": [{"item_id": PASSIVE, "count": 3}]}, POSSESSION_READBACK),
    (ENQUEUE, {"rewards": [{"kind": "item", "item_id": ENTRUST_TICKET, "amount": 2}]}, PRESENT_READBACK),
    (RANK_RETRY, {}, RANK_READBACK),
])
def test_economy_plans_pass_preflight_and_restart_checks(bundle, preflight, save, command, params, readback):
    store, pipeline = console(bundle, save, preflight)
    plan = preview(pipeline, command, params)
    assert plan.committable and plan.restart_check == {"ok": True} and plan.preflight["readback"] == readback
    assert plan.confirmations == ()
    outcome = commit(pipeline, plan)
    assert outcome.result["backup_before_commit"] is None and store.backups == []


def test_capacity_and_skin_plans_pass_preflight_and_restart_checks(bundle, preflight, save):
    store, pipeline = console(bundle, save, preflight)
    month = following_month(read_capacity(save)["period"])
    plan = preview(pipeline, CAPACITY, {"amount": CAPACITY_GRANT, "month": month})
    assert plan.committable and plan.preflight["readback"] == GAME_READBACK
    commit(pipeline, plan)
    plan = preview(pipeline, SKIN_GRANT, {"item_id": sold_skins(bundle)[0], "purchase_flag": True})
    assert plan.committable and plan.restart_check == {"ok": True}
    assert plan.preflight["readback"] == {**POSSESSION_READBACK, "missions": [54], "universal_flags": [137]}
    commit(pipeline, plan)
    assert store.revision == 2


def test_experience_and_its_rank_mail_commit_as_one_revision(bundle, preflight, save):
    store, pipeline = console(bundle, save, preflight)
    commit(pipeline, preview(pipeline, EXPERIENCE, {"amount": RANK_UP_EXPERIENCE}))
    assert store.revision == 1 and game(store.state)["Experience"] == RANK_UP_EXPERIENCE
    entries = read_rank_rewards(store.state)["entries"].values()
    delivered = {mail for entry in entries if entry["status"] == "delivered" for mail in entry["mail_ids"]}
    assert delivered and delivered == {row["PresentID"] for row in store.state["tables"]["presents"]}


@pytest.mark.parametrize("command, params, phrase", [
    (GOLD_DEBIT, {"amount": 100}, "DEBIT 100 GOLD"),
    (ITEM_DEBIT, {"items": [{"item_id": PASSIVE, "count": 1}]}, "DEBIT 1 ITEM"),
])
def test_decreases_commit_only_with_their_phrase_after_a_backup(bundle, preflight, save, command, params, phrase):
    store, pipeline = console(bundle, bundle.possessions.credit(save, {PASSIVE: 3}), preflight)
    plan = preview(pipeline, command, params)
    assert plan.committable and [item.phrase for item in plan.confirmations] == [phrase]
    with pytest.raises(AdminError) as caught:
        commit(pipeline, plan)
    assert caught.value.code == "admin_confirmation_required" and store.revision == 0 and store.backups == []
    outcome = commit(pipeline, plan, (phrase,))
    assert outcome.result["backup_before_commit"]["kind"] == AUTO_BACKUP_KIND and len(store.backups) == 1
    assert store.revision == 1


@pytest.mark.parametrize("value, phrases", [(FUNDED_GOLD - 1000, (f"SET GOLD TO {FUNDED_GOLD - 1000}",)),
                                           (FUNDED_GOLD + 1000, ()), (FUNDED_GOLD, ())])
def test_gold_sets_take_a_phrase_and_a_backup_only_when_they_lower_the_balance(bundle, preflight, save, value,
                                                                              phrases):
    store, pipeline = console(bundle, set_money(save, FUNDED_GOLD, cap=GOLD_LIMIT), preflight)
    plan = preview(pipeline, GOLD_SET, {"value": value, "current": FUNDED_GOLD})
    assert plan.committable and plan.restart_check == {"ok": True}
    assert [item.phrase for item in plan.confirmations] == list(phrases)
    if phrases:
        with pytest.raises(AdminError) as caught:
            commit(pipeline, plan)
        assert caught.value.code == "admin_confirmation_required" and store.backups == []
    outcome = commit(pipeline, plan, phrases)
    assert game(store.state)["Money"] == value and len(store.backups) == len(phrases)
    assert (outcome.result["backup_before_commit"] is None) == (not phrases)
    # An unchanged balance commits only its audit record, so it neither replaces the backup nor moves the revision.
    assert store.revision == (0 if value == FUNDED_GOLD else 1)


def test_crystal_debit_commits_only_with_its_phrase(bundle, preflight, save):
    store, pipeline = console(bundle, credit_free_crystals(save, 300, ACQUISITION_DAY), preflight)
    plan = preview(pipeline, CRYSTAL_DEBIT, {"amount": 100})
    assert plan.preflight["readback"] == CRYSTAL_READBACK
    commit(pipeline, plan, ("DEBIT 100 CRYSTALS FREE FIRST",))
    assert wallet_totals(store.state)["free"] == 200 and store.backups[0]["row"]["kind"] == AUTO_BACKUP_KIND


def view_registry():
    views = ViewRegistry()
    admin_views_items.register(views)
    admin_views_presents.register(views)
    return views


def read_view(bundle, name, state, params=None, revision=0):
    view = view_registry().get(name)
    parsed = None if view.params_model is None else view.params_model.model_validate_json(json.dumps(params or {}))
    request = SimpleNamespace(generation=SimpleNamespace(bundle=bundle), state=state,
                              now=instant(bundle.bootstrap.clock()), revision=revision)
    return view.read(request, parsed)


def claimed(state, revision=1, response=SECRET_RESPONSE):
    result = deepcopy(state)
    result["present_claim_history_schema"] = 1
    result["present_claim_history"] = [{"profile": "a" * 64, "digest": "b" * 64, "revision": revision,
                                        "response": base64.b64encode(response).decode("ascii")}]
    return result


def journaled(state, pending):
    result = deepcopy(state)
    result["quest_state"] = {"pending_presents": pending}
    return result


def test_item_and_present_views_register_redacted():
    views = view_registry()
    assert [view.name for view in views] == list(VIEWS)
    assert {view.domain for view in views} == {"items", "presents"}
    assert all(callable(getattr(view.read, "__wrapped__", None)) for view in views)


def test_holdings_show_stack_class_enabled_and_the_command_that_changes_them(bundle, save):
    skin = sold_skins(bundle)[0]
    blessing = sorted(classifier(bundle).item_ids(ItemClass.OWNERSHIP))[0]
    stocked = bundle.possessions.credit(save, {PASSIVE: 3, ENTRUST_TICKET: 2, skin: 1, blessing: 1})
    stocked = bundle.possessions.set_enabled(stocked, {blessing: 1})
    listing = read_view(bundle, "items.holdings", stocked)
    rows = catalog(bundle).rows
    assert listing["classes"] == {"ok": True} and listing["items"]["ok"] is True
    assert listing["items"]["items"] == [
        {"ItemID": PASSIVE, "Count": 3, "Enabled": 0, "stack": rows[PASSIVE]["Stack"], "class": "passive",
         "command": ITEM_CREDIT.operation},
        {"ItemID": ENTRUST_TICKET, "Count": 2, "Enabled": 0, "stack": rows[ENTRUST_TICKET]["Stack"],
         "class": "passive", "command": ENQUEUE.operation},
        {"ItemID": skin, "Count": 1, "Enabled": 0, "stack": rows[skin]["Stack"], "class": "skin",
         "command": SKIN_GRANT.operation},
        {"ItemID": blessing, "Count": 1, "Enabled": 1, "stack": rows[blessing]["Stack"], "class": "ownership",
         "command": ENQUEUE.operation}]
    assert read_view(bundle, "items.holdings", save)["items"]["total"] == 0


def test_item_catalog_filters_by_class_and_shows_held_counts(bundle, save):
    stocked = bundle.possessions.credit(save, {PASSIVE: 3})
    classes = classifier(bundle)
    passive = sorted(classes.item_ids(ItemClass.PASSIVE))
    listing = read_view(bundle, "items.catalog", stocked,
                        {"item_class": "passive", "offset": passive.index(PASSIVE), "limit": 1})
    assert listing["classes"] == {"ok": True} and listing["items"]["total"] == len(passive)
    entry = listing["items"]["items"][0]
    assert (entry["ItemID"], entry["held"], entry["class"], entry["command"], entry["ReceiveAction"]) == (
        PASSIVE, 3, "passive", ITEM_CREDIT.operation, 0)
    tickets, packs = sorted(classes.item_ids(ItemClass.ITEM_TICKET)), pack_tickets(bundle)
    listing = read_view(bundle, "items.catalog", save,
                        {"item_class": "item_ticket", "offset": tickets.index(FIRST_PACK), "limit": len(packs)})
    assert [(item["ItemID"], item["command"], item["ReceiveAction"]) for item in listing["items"]["items"]] == [
        (name, None, PACK_RECEIVE_ACTION) for name in packs]
    assert read_view(bundle, "items.catalog", save, {"limit": 1})["items"]["total"] == len(catalog(bundle).rows)
    with pytest.raises(ValidationError):
        read_view(bundle, "items.catalog", save, {"item_class": "costume"})


def test_mailbox_decodes_mail_and_keeps_notification_categories_read_only(bundle, save):
    mailed, _ = bundle.presents.enqueue(save, [
        {"kind": "gold", "amount": 100, "description": "Gold"},
        {"kind": "unit", "template_id": FIRST_TEMPLATE, "count": 1, "description": "Unit"},
        {"kind": "legacy_item", "present_type": LEGACY_BOUQUET, "amount": 2, "description": "Bouquet"},
        {"kind": "item", "item_id": ENTRUST_TICKET, "amount": 2, "description": "Tickets"}])
    marked = bundle.presents.set_notification_category(mailed, [4], EVENT_PASS_CATEGORY)
    listing = read_view(bundle, "presents.mailbox", marked)
    assert listing["ok"] is True
    assert (listing["count"], listing["capacity"], listing["last_present_id"]) == (4, MAX_PRESENTS, 4)
    assert listing["notification_category"] == {"field": NOTIFICATION_FIELD, "wire": "A9", "read_only": True,
                                                "reason": NOTIFICATION_POLICY}
    assert [(mail["kind"], mail["target"], mail[NOTIFICATION_FIELD]) for mail in listing["mails"]["items"]] == [
        ("gold", None, 0),
        ("unit", {"template_id": FIRST_TEMPLATE, "card_id": bundle.presents.templates[FIRST_TEMPLATE]}, 0),
        ("legacy_item", {"item_id": BOUQUET}, 0),
        ("item", {"item_id": ENTRUST_TICKET}, EVENT_PASS_CATEGORY)]
    assert read_view(bundle, "presents.mailbox", save)["count"] == 0


def test_backlog_lists_waiting_battle_presents_read_only(bundle, save):
    pending = [{"reward": {"kind": "gold", "amount": 100, "description": "Battle gold"}, "copies": 2},
               {"reward": {"kind": "item", "item_id": ENTRUST_TICKET, "amount": 1, "description": "Battle ticket"},
                "copies": 1}]
    backlog = read_view(bundle, "presents.backlog", journaled(save, pending))
    assert backlog["ok"] is True and backlog["read_only"] is True and backlog["copies"] == 3
    assert [entry["delivery"] for entry in backlog["entries"]] == [{"ok": True, "mails_per_copy": 1}] * 2
    assert read_view(bundle, "presents.backlog", save)["entries"] == []
    broken = read_view(bundle, "presents.backlog", journaled(save, [{"reward": {"kind": "gold"}, "copies": 0}]))
    assert broken["ok"] is False and broken["error"]["code"] == BACKLOG_INVALID


# The claim loader accepts a byte-order mark, which hides the XML prefix the generic redaction looks for.
@pytest.mark.parametrize("response", [SECRET_RESPONSE, BOM_UTF8 + SECRET_RESPONSE])
def test_claim_receipts_are_redacted_to_digests(bundle, save, response):
    receipts = read_view(bundle, "presents.receipts", claimed(save, response=response), revision=1)
    assert receipts["ok"] is True and receipts["schema_version"] == 1 and receipts["receipts"]["total"] == 1
    assert receipts["receipts"]["items"][0]["response"] == {
        "redacted": "response", "encoding": "base64", "bytes": len(response),
        "sha256": hashlib.sha256(response).hexdigest()}
    assert base64.b64encode(response).decode("ascii") not in json.dumps(receipts)
    stale = read_view(bundle, "presents.receipts", claimed(save, revision=2), revision=1)
    assert stale["ok"] is False and stale["error"]["code"] == "present_history_invalid"


def test_views_save_nothing(bundle, save):
    stocked = journaled(claimed(bundle.possessions.credit(save, {PASSIVE: 3})), [])
    digest = json_digest(stocked)
    for name in VIEWS:
        read_view(bundle, name, stocked, revision=1)
    assert json_digest(stocked) == digest

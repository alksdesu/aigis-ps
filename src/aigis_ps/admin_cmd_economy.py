from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from http import HTTPStatus
from itertools import accumulate
from types import MappingProxyType
from typing import Any

from pydantic import Field, model_validator

from .admin_cmd_settings import FLAGS_DOMAIN, MISSIONS_DOMAIN, PROGRESS, SKIN_SHOP_REASON, recorded_skins
from .admin_command import (COMMAND_UNAVAILABLE, AdminCommand, AdminError, CommandContext, CommandParams, CommandResult,
                            counted, first_unavailable, require_distinct, required_service, requires)
from .admin_fields import EVENT_PASSES
from .admin_registry import CommandRegistry
from .admin_views_account import ACCOUNT, CRYSTALS, GAME_PARAM
from .admin_views_tools import Amount, ItemName, PresentReward, held
from .business_calendar import month_key, next_month
from .crystal_wallet import (CRYSTAL_LIMITS, claim_crystal_space, claim_crystals_fit, credit_free_crystals,
                             credit_paid_crystals, debit_crystals, paid_crystal_space, wallet_totals)
from .errors import ProtocolError
from .item_classes import ItemClass, ItemClasses
from .monthly_capacity import grant_next_capacity, read_capacity
from .native_numbers import INT32_MAX
from .possessions import MAX_POSSESSION_ITEMS
from .presents import MAX_PRESENTS
from .quest_economy import credit_quest, settle_energy
from .rank_reward_state import PENDING, read_rank_rewards
from .universal_flags import flag_value, resolve_flag_name, set_flags
from .vote_tickets import DAILY_ITEM
from .wallet_money import (GOLD_LIMIT, NATIVE_BALANCE_MESSAGE, MoneyRefusal, MoneyRefusals, credit_money, debit_money,
                           money_balance, set_money)


CURRENCIES, ITEMS = "currencies", "items"
POSSESSIONS, PRESENTS, QUESTS = "possessions", "presents", "quests"
RANK_REWARDS, SUGOROKU = "rank_rewards", "sugoroku"
GOLD_REFUSALS = MoneyRefusals(
    capacity=MoneyRefusal("admin_gold_capacity", f"Gold credits and balances stop at {GOLD_LIMIT}; "
                                                 "a balance saved above it may only decrease"),
    insufficient=MoneyRefusal("admin_gold_insufficient", "The debit exceeds the saved gold balance"),
    native_balance=MoneyRefusal("admin_gold_native_balance", NATIVE_BALANCE_MESSAGE))
GOLD_CHANGED = "admin_gold_current_changed"
FREE_CRYSTAL_CAPACITY, PAID_CRYSTAL_CAPACITY = "admin_free_crystal_capacity", "admin_paid_crystal_capacity"
CAPACITY_MONTH, CAPACITY_OVERFLOW = "admin_capacity_month", "admin_capacity_overflow"
ITEM_NOT_STOCK, SKIN_ITEM_INVALID = "admin_item_not_stock", "admin_skin_item_invalid"
SKIN_OWNED, SKIN_NOT_SOLD = "admin_skin_owned", "admin_skin_not_sold"
PACK_TICKET = "admin_present_pack_ticket"
# These commands live in modules that build on this one, so their routes name them by operation.
POINT_GRANT, TICKET_GRANT = "event_pass.grant_points", "vote_tickets.grant"
# ReceiveAction 3 opens a ticket pack on receipt, and such packs are never mailed by hand, whatever their pools allow.
PACK_RECEIVE_ACTION = 3
# Without the entrust service, or when its catalog lists no ticket, the verified entrust ticket still stays mail-only.
FALLBACK_MAIL_ONLY = frozenset(("item_consignment_ticket",))
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
SKIN_GRANT_OPERATION = "skins.grant"
ENQUEUE_OPERATION = "presents.enqueue"
RANK_RETRY_OPERATION = "rank_rewards.retry_pending"
ITEM_CATALOG_REASON = "Item commands need the verified item definitions"
FREE_CRYSTAL_MAIL = ("Free crystals are added only as far as claiming one free_crystal mail could add them; "
                     f"send the rest with {ENQUEUE_OPERATION} as free_crystal mail")
PAID_CRYSTAL_MESSAGE = "Paid crystals stop at the client wallet limits for paid and total crystals"
PAID_LOCAL = "Paid crystals were added on this server only; no platform purchase or monthly spending was recorded"
GOLD_STALE_MESSAGE = "The saved gold balance is no longer the balance this edit started from"
GOLD_UNCHANGED = "The gold balance already had this value"
EXPERIENCE_CAPPED = "Experience stops one point below the last rank threshold, so part of the amount was not added"
RANK_REWARDS_WAITING = f"Some rank rewards stay pending; {RANK_RETRY_OPERATION} delivers them once they fit"
NOTHING_PENDING = "No rank reward was pending"
NOTHING_DELIVERED = "No pending rank reward could be delivered yet"
NOT_STOCK_MESSAGE = ("Only passive stock items change by count; "
                     "the other items reach the player through their own commands")
NOT_DEBITABLE_MESSAGE = "Only passive stock items are debited by count; no console command removes the other items"
SKIN_ONLY_MESSAGE = f"{SKIN_GRANT_OPERATION} takes skin items; other items reach the player through their own commands"
NOT_SOLD_MESSAGE = "This skin is not sold in the skin shop, so it has no purchase flag to set"
PACK_MESSAGE = "Ticket packs that open on receipt are never mailed by hand"
UNBOUGHT_PRODUCT = ("Skin shop product {shop_id} still shows {item_id} as unbought, and the shop refuses to sell "
                    "a skin the player already owns")


class CrystalOrder(StrEnum):
    FREE_FIRST = "free_first"
    PAID_FIRST = "paid_first"
    PAID_ONLY = "paid_only"


DEBIT_ORDERS = MappingProxyType({CrystalOrder.FREE_FIRST: {"paid_only": False, "free_first": True},
                                 CrystalOrder.PAID_FIRST: {"paid_only": False, "free_first": False},
                                 CrystalOrder.PAID_ONLY: {"paid_only": True, "free_first": False}})


def field_error(name: str, code: str, message: str) -> dict:
    return {"loc": [name], "type": code, "msg": message}


class AmountParams(CommandParams):
    amount: Amount


class GoldSetParams(CommandParams):
    value: int = Field(ge=0, le=INT32_MAX)
    current: int = Field(ge=0, le=INT32_MAX, description="The saved balance this edit starts from")


class CrystalDebitParams(AmountParams):
    order: CrystalOrder = Field(default=CrystalOrder.FREE_FIRST,
                                description="free_first matches the server's own spending: free crystals first, "
                                            "oldest acquisition days first")


class RestoreParams(CommandParams):
    pass


class CapacityParams(CommandParams):
    amount: Amount
    month: str = Field(pattern=MONTH_PATTERN,
                       description="YYYY-MM whose extended charisma grows: the month after the JST business month")


class ItemCount(CommandParams):
    item_id: ItemName
    count: Amount


class ItemsParams(CommandParams):
    items: list[ItemCount] = Field(min_length=1, max_length=MAX_POSSESSION_ITEMS)

    @model_validator(mode="after")
    def _distinct(self) -> ItemsParams:
        require_distinct((item.item_id for item in self.items), "Item IDs")
        return self

    def counts(self) -> dict[str, int]:
        return {item.item_id: item.count for item in self.items}


class SkinParams(CommandParams):
    item_id: ItemName
    purchase_flag: bool = Field(description="Also mark every skin shop product selling this skin as purchased")


class EnqueueParams(CommandParams):
    rewards: list[PresentReward] = Field(min_length=1, max_length=MAX_PRESENTS)


class RetryParams(CommandParams):
    pass


def gold(state: dict) -> int:
    return money_balance(state).money


def gold_change(before: dict, after: dict) -> dict:
    return {"before": gold(before), "after": gold(after), "limit": GOLD_LIMIT}


def credit_gold(state: dict, params: AmountParams, context: CommandContext) -> CommandResult:
    updated = credit_money(state, params.amount, cap=GOLD_LIMIT, refusals=GOLD_REFUSALS)
    return CommandResult(updated, gold_change(state, updated))


def debit_gold(state: dict, params: AmountParams, context: CommandContext) -> CommandResult:
    updated = debit_money(state, params.amount, refusals=GOLD_REFUSALS)
    return CommandResult(updated, gold_change(state, updated))


def gold_debit_phrase(params: AmountParams) -> str:
    return f"DEBIT {params.amount} GOLD"


def lowers_gold(params: GoldSetParams) -> bool:
    return params.value < params.current


def set_gold(state: dict, params: GoldSetParams, context: CommandContext) -> CommandResult:
    money = gold(state)
    # The phrase and backup were decided from current, so a different saved balance refuses the edit.
    if money != params.current:
        raise AdminError(GOLD_CHANGED, GOLD_STALE_MESSAGE, HTTPStatus.CONFLICT,
                         details={"money": money, "current": params.current})
    updated = set_money(state, params.value, cap=GOLD_LIMIT, refusals=GOLD_REFUSALS)
    return CommandResult(updated, gold_change(state, updated), (GOLD_UNCHANGED,) if params.value == money else ())


def gold_set_phrase(params: GoldSetParams) -> str:
    return f"SET GOLD TO {params.value}"


def crystal_change(before: dict, after: dict) -> dict:
    return {"totals": {"before": wallet_totals(before), "after": wallet_totals(after)},
            "limits": dict(CRYSTAL_LIMITS)}


def credit_free(state: dict, params: AmountParams, context: CommandContext) -> CommandResult:
    if not claim_crystals_fit(state, params.amount):
        raise AdminError(FREE_CRYSTAL_CAPACITY, FREE_CRYSTAL_MAIL, details={
            "totals": wallet_totals(state), "space": claim_crystal_space(state), "limits": dict(CRYSTAL_LIMITS)})
    updated = credit_free_crystals(state, params.amount, context.acquisition_day)
    return CommandResult(updated, {**crystal_change(state, updated), "acquisition_day": context.acquisition_day})


def credit_paid(state: dict, params: AmountParams, context: CommandContext) -> CommandResult:
    totals = wallet_totals(state)
    space = paid_crystal_space(state)
    if params.amount > space:
        raise AdminError(PAID_CRYSTAL_CAPACITY, PAID_CRYSTAL_MESSAGE,
                         details={"totals": totals, "space": space, "limits": dict(CRYSTAL_LIMITS)})
    updated = credit_paid_crystals(state, params.amount, context.acquisition_day)
    effects = {**crystal_change(state, updated), "acquisition_day": context.acquisition_day, "local_operation": True}
    return CommandResult(updated, effects, (PAID_LOCAL,))


def debit_crystal_balance(state: dict, params: CrystalDebitParams, context: CommandContext) -> CommandResult:
    # A console debit is not a purchase, so monthly spending and its projection stay untouched.
    updated = debit_crystals(state, params.amount, **DEBIT_ORDERS[params.order])
    return CommandResult(updated, {**crystal_change(state, updated), "order": params.order.value})


def crystal_debit_phrase(params: CrystalDebitParams) -> str:
    return f"DEBIT {counted(params.amount, 'CRYSTAL')} {params.order.value.replace('_', ' ').upper()}"


def game_row(context: CommandContext, state: dict) -> dict:
    return required_service(context, "bootstrap").table_rows(state, GAME_PARAM)[0]


def rank_reward(key: str, entry: dict, detail: str) -> dict:
    return {"bonus_id": int(key), "rank": entry["rank"], detail: entry[detail]}


def rank_outcome(state: dict, mail_ids: Iterable[int]) -> dict:
    mailed = list(mail_ids)
    entries = sorted(read_rank_rewards(state)["entries"].items(), key=lambda item: (item[1]["rank"], int(item[0])))
    return {"mail_ids": mailed,
            "delivered": [rank_reward(key, entry, "mail_ids") for key, entry in entries
                          if set(entry["mail_ids"]) & set(mailed)],
            "pending": [rank_reward(key, entry, "reason") for key, entry in entries if entry["status"] == PENDING]}


def add_experience(state: dict, params: AmountParams, context: CommandContext) -> CommandResult:
    rules, rewards = required_service(context, QUESTS).rules, required_service(context, RANK_REWARDS)
    rank_rows = required_service(context, "bootstrap").rank_rows
    credited = credit_quest(state, rank_rows, context.now, experience=params.amount, money=0,
                            refill_on_rank_up=rules.refill_on_rank_up, recovery_seconds=rules.charisma_recovery_seconds)
    # Like battle settlement, the rank crossing and its reward mail land in the same write.
    updated, mail_ids = rewards.apply(state, credited)
    before, after = game_row(context, state), game_row(context, updated)
    added, outcome = after["Experience"] - before["Experience"], rank_outcome(updated, mail_ids)
    effects = {"experience": {"before": before["Experience"], "after": after["Experience"],
                              "requested": params.amount, "added": added},
               "rank": {"before": before["Rank"], "after": after["Rank"]},
               "charisma": {"before": before["NowCharisma"], "after": after["NowCharisma"]},
               "rank_rewards": outcome}
    warnings = (*((EXPERIENCE_CAPPED,) if added < params.amount else ()),
                *((RANK_REWARDS_WAITING,) if outcome["pending"] else ()))
    return CommandResult(updated, effects, warnings)


def restore_energy(state: dict, params: RestoreParams, context: CommandContext) -> CommandResult:
    recovery = required_service(context, "recovery")
    updated = recovery.restore_state(state, context.now)
    before, after = game_row(context, state), game_row(context, updated)
    return CommandResult(updated, {"charisma": {"before": before["NowCharisma"], "after": after["NowCharisma"]},
                                   "limit": recovery.rules.charisma_limit})


def following_month(period: str) -> str:
    year, month = map(int, period.split("-"))
    return month_key(next_month(date(year, month, 1)))


def grant_capacity(state: dict, params: CapacityParams, context: CommandContext) -> CommandResult:
    sugoroku, rank_rows = required_service(context, SUGOROKU), required_service(context, "bootstrap").rank_rows
    # The ledger must reach the current business month first, exactly as a board reward settles it.
    settled = settle_energy(state, rank_rows, context.now, recovery_seconds=sugoroku.recovery_seconds)
    ledger = read_capacity(settled)
    business, effective = ledger["period"], following_month(ledger["period"])
    if params.month != effective:
        message = f"Capacity granted now takes effect in {effective}, the month after the business month {business}"
        raise AdminError(CAPACITY_MONTH, message, HTTPStatus.UNPROCESSABLE_ENTITY,
                         fields=(field_error("month", CAPACITY_MONTH, message),),
                         details={"business_month": business, "effective_month": effective})
    updated = grant_next_capacity(settled, params.amount, context.now)
    granted = read_capacity(updated)
    # Next month this becomes the current capacity, and energy settlement refuses a total past int32.
    if max(row["CharismaMax"] for row in rank_rows) + granted["next"] > INT32_MAX:
        raise AdminError(CAPACITY_OVERFLOW, "Next month's charisma capacity would pass the native int32 field",
                         details={"next": ledger["next"], "amount": params.amount})
    return CommandResult(updated, {"business_month": business, "effective_month": effective,
                                   "current": ledger["current"],
                                   "next": {"before": ledger["next"], "after": granted["next"]}})


def item_catalog(services: Any) -> Any:
    return getattr(getattr(services, PRESENTS, None), "item_catalog", None)


def items_unavailable(services: Any) -> str | None:
    return None if item_catalog(services) is not None else ITEM_CATALOG_REASON


def checked_catalog(services: Any) -> Any:
    catalog = item_catalog(services)
    if catalog is None:
        raise AdminError(COMMAND_UNAVAILABLE, ITEM_CATALOG_REASON, HTTPStatus.SERVICE_UNAVAILABLE)
    return catalog


def classifier(services: Any) -> ItemClasses:
    catalog, presents = checked_catalog(services), getattr(services, PRESENTS)
    missions, votes = getattr(services, "missions", None), getattr(services, "votes", None)
    transactions = None if votes is None else votes.transactions
    return ItemClasses(catalog, recovery_catalog=presents.recovery_catalog, item_tickets=presents.item_tickets,
                       pass_points=None if missions is None else missions.pass_points,
                       vote_tickets=None if transactions is None else transactions.tickets)


def packed(catalog: Any, item_id: str) -> bool:
    row = catalog.rows.get(item_id)
    return row is not None and row["ReceiveAction"] == PACK_RECEIVE_ACTION


def mail_only_items(services: Any) -> frozenset[str]:
    # Entrust tickets are topped up only through present mail, never by changing the held count.
    entrust = getattr(services, "entrust", None)
    tickets = () if entrust is None else entrust.catalog.ticket_item_ids
    return frozenset(tickets) or FALLBACK_MAIL_ONLY


def unrouted_items(services: Any) -> frozenset[str]:
    # Only the normal pass item holds EP and the daily ticket joins no vote group, so their class commands refuse these.
    passes = getattr(services, EVENT_PASSES, None)
    premium = () if passes is None else (entry.premium_item_id for entry in passes.catalog.passes)
    return frozenset((DAILY_ITEM, *filter(None, premium)))


@dataclass(frozen=True)
class ItemRoutes:
    catalog: Any
    mail_only: frozenset[str]
    unrouted: frozenset[str]

    def route(self, item_id: str, item_class: ItemClass) -> str | None:
        if packed(self.catalog, item_id) or item_id in self.unrouted:
            return None
        if item_id in self.mail_only:
            return ENQUEUE.operation
        return CLASS_ROUTES.get(item_class)


def item_routes(services: Any, catalog: Any) -> ItemRoutes:
    return ItemRoutes(catalog, mail_only_items(services), unrouted_items(services))


def refused_item(routes: ItemRoutes, item_id: str, item_class: ItemClass, *, credit: bool = True) -> dict:
    return {"item_id": item_id, "class": item_class.value, "use": routes.route(item_id, item_class) if credit else None}


def require_stock(classes: ItemClasses, routes: ItemRoutes, item_ids: Iterable[str], *, credit: bool) -> None:
    classified = [(item_id, classes.classify(item_id)) for item_id in item_ids]
    refused = [refused_item(routes, item_id, item_class, credit=credit)
               for item_id, item_class in classified
               if item_class is not ItemClass.PASSIVE or (credit and item_id in routes.mail_only)]
    if refused:
        raise AdminError(ITEM_NOT_STOCK, NOT_STOCK_MESSAGE if credit else NOT_DEBITABLE_MESSAGE,
                         HTTPStatus.UNPROCESSABLE_ENTITY, details={"items": refused})


def stock_changes(possessions: Any, catalog: Any, before: dict, after: dict, item_ids: Iterable[str]) -> list[dict]:
    item_ids = tuple(item_ids)
    old, new = held(possessions, before, item_ids), held(possessions, after, item_ids)
    return [{"ItemID": item_id, "before": old[item_id], "after": new[item_id], "stack": catalog.rows[item_id]["Stack"]}
            for item_id in item_ids]


def change_stock(state: dict, params: ItemsParams, context: CommandContext, *, credit: bool) -> CommandResult:
    routes, counts = item_routes(context.services, checked_catalog(context.services)), params.counts()
    require_stock(classifier(context.services), routes, counts, credit=credit)
    possessions = required_service(context, POSSESSIONS)
    updated = possessions.credit(state, counts) if credit else possessions.debit(state, counts)
    return CommandResult(updated, {"items": stock_changes(possessions, routes.catalog, state, updated, counts)})


def credit_items(state: dict, params: ItemsParams, context: CommandContext) -> CommandResult:
    return change_stock(state, params, context, credit=True)


def debit_items(state: dict, params: ItemsParams, context: CommandContext) -> CommandResult:
    return change_stock(state, params, context, credit=False)


def item_debit_phrase(params: ItemsParams) -> str:
    return f"DEBIT {counted(len(params.items), 'ITEM')}"


def shop_products(skin_shop: Any, item_id: str) -> tuple:
    return () if skin_shop is None else tuple(product for product in skin_shop.catalog.products
                                             if product.item_id == item_id)


def purchase_flags(state: dict, products: Iterable[Any]) -> dict[str, Any]:
    return {resolve_flag_name(state, product.purchase_flag, default_name=product.flag_name): product
            for product in products}


def grant_skin(state: dict, params: SkinParams, context: CommandContext) -> CommandResult:
    item_id, catalog = params.item_id, checked_catalog(context.services)
    item_class = classifier(context.services).classify(item_id)
    if item_class is not ItemClass.SKIN:
        raise AdminError(SKIN_ITEM_INVALID, SKIN_ONLY_MESSAGE, HTTPStatus.UNPROCESSABLE_ENTITY,
                         fields=(field_error("item_id", SKIN_ITEM_INVALID, SKIN_ONLY_MESSAGE),),
                         details=refused_item(item_routes(context.services, catalog), item_id, item_class))
    possessions, missions = required_service(context, POSSESSIONS), required_service(context, "missions")
    if held(possessions, state, (item_id,))[item_id]:
        raise AdminError(SKIN_OWNED, "The skin is already owned", details={"item_id": item_id})
    shop = (required_service(context, "skin_shop", SKIN_SHOP_REASON) if params.purchase_flag
            else getattr(context.services, "skin_shop", None))
    products = shop_products(shop, item_id)
    if params.purchase_flag and not products:
        raise AdminError(SKIN_NOT_SOLD, NOT_SOLD_MESSAGE, HTTPStatus.UNPROCESSABLE_ENTITY,
                         fields=(field_error("purchase_flag", SKIN_NOT_SOLD, NOT_SOLD_MESSAGE),),
                         details={"item_id": item_id})
    granted = possessions.credit(state, {item_id: 1})
    flags = purchase_flags(state, products) if params.purchase_flag else {}
    if flags:
        granted = set_flags(granted, dict.fromkeys(flags, 1))
    updated = missions.record_acquisitions(state, granted, context.now)
    recorded = recorded_skins(context.services, updated)
    effects = {"item": {"ItemID": item_id, "before": 0, "after": 1},
               "shop_ids": [product.shop_id for product in products],
               "purchase_flags": [{"ID": name, "shop_id": product.shop_id, "before": flag_value(state, name),
                                   "after": 1} for name, product in flags.items()],
               "acquisition_recorded": None if recorded is None else item_id in recorded}
    warnings = () if params.purchase_flag else tuple(UNBOUGHT_PRODUCT.format(shop_id=product.shop_id, item_id=item_id)
                                                     for product in products)
    return CommandResult(updated, effects, warnings)


def refuse_pack_tickets(services: Any, rewards: list[dict]) -> None:
    item_ids = {reward["item_id"] for reward in rewards if "item_id" in reward}
    if not item_ids:
        return
    catalog = checked_catalog(services)
    packs = sorted(item_id for item_id in item_ids if packed(catalog, item_id))
    if packs:
        raise AdminError(PACK_TICKET, PACK_MESSAGE, HTTPStatus.UNPROCESSABLE_ENTITY, details={"items": packs})


def previewed_mails(presents: Any, rewards: list[dict], existing: int) -> list[int]:
    mails, total = [], existing
    for index, reward in enumerate(rewards):
        try:
            rows = presents.preview([reward], total)
        except ProtocolError as error:
            raise AdminError(error.code, str(error), error.status, details={"reward": index}) from error
        mails.append(len(rows))
        total += len(rows)
    return mails


def enqueue_presents(state: dict, params: EnqueueParams, context: CommandContext) -> CommandResult:
    presents = required_service(context, PRESENTS)
    rewards = [reward.model_dump(exclude_none=True) for reward in params.rewards]
    refuse_pack_tickets(context.services, rewards)
    existing = len(presents.rows(state))
    # Each reward is previewed on its own first, so a refusal names the reward it belongs to.
    mails = previewed_mails(presents, rewards, existing)
    updated, added = presents.enqueue(state, rewards)
    present_ids = [row["PresentID"] for row in added]
    return CommandResult(updated, {
        "mailbox": {"before": existing, "after": existing + len(added), "capacity": MAX_PRESENTS},
        "rewards": [{"kind": reward["kind"], "mails": count, "present_ids": present_ids[end - count:end]}
                    for reward, count, end in zip(rewards, mails, accumulate(mails))]})


def retry_rank_rewards(state: dict, params: RetryParams, context: CommandContext) -> CommandResult:
    updated, mail_ids = required_service(context, RANK_REWARDS).apply(state, state)
    outcome = rank_outcome(updated, mail_ids)
    warnings = () if mail_ids else ((NOTHING_DELIVERED if outcome["pending"] else NOTHING_PENDING),)
    return CommandResult(updated, outcome, warnings)


GOLD_CREDIT = AdminCommand("wallet.gold.credit", CURRENCIES, AmountParams, credit_gold, frozenset({GAME_PARAM}))
GOLD_DEBIT = AdminCommand("wallet.gold.debit", CURRENCIES, AmountParams, debit_gold, frozenset({GAME_PARAM}),
                          destructive=True, confirm_phrase=gold_debit_phrase)
GOLD_SET = AdminCommand("wallet.gold.set", CURRENCIES, GoldSetParams, set_gold, frozenset({GAME_PARAM}),
                        destructive=lowers_gold, confirm_phrase=gold_set_phrase)
FREE_CRYSTALS = AdminCommand("wallet.crystals.credit_free", CURRENCIES, AmountParams, credit_free,
                             frozenset({CRYSTALS}))
PAID_CRYSTALS = AdminCommand("wallet.crystals.credit_paid", CURRENCIES, AmountParams, credit_paid,
                             frozenset({CRYSTALS}))
CRYSTAL_DEBIT = AdminCommand("wallet.crystals.debit", CURRENCIES, CrystalDebitParams, debit_crystal_balance,
                             frozenset({CRYSTALS}), destructive=True, confirm_phrase=crystal_debit_phrase)
EXPERIENCE = AdminCommand("wallet.experience.add", ACCOUNT, AmountParams, add_experience,
                          frozenset({GAME_PARAM, RANK_REWARDS, PRESENTS}),
                          unavailable=first_unavailable(requires(QUESTS), requires(RANK_REWARDS)))
ENERGY = AdminCommand("energy.restore", ACCOUNT, RestoreParams, restore_energy, frozenset({GAME_PARAM}),
                      unavailable=requires("recovery"))
CAPACITY = AdminCommand("capacity.grant_next", ACCOUNT, CapacityParams, grant_capacity, frozenset({GAME_PARAM}),
                        unavailable=requires(SUGOROKU))
ITEM_CREDIT = AdminCommand("possessions.credit", ITEMS, ItemsParams, credit_items, frozenset({POSSESSIONS}),
                           unavailable=items_unavailable)
ITEM_DEBIT = AdminCommand("possessions.debit", ITEMS, ItemsParams, debit_items, frozenset({POSSESSIONS}),
                          destructive=True, confirm_phrase=item_debit_phrase, unavailable=items_unavailable)
SKIN_GRANT = AdminCommand(SKIN_GRANT_OPERATION, ITEMS, SkinParams, grant_skin,
                          frozenset({POSSESSIONS, MISSIONS_DOMAIN, FLAGS_DOMAIN}),
                          unavailable=first_unavailable(items_unavailable, requires("missions")))
ENQUEUE = AdminCommand(ENQUEUE_OPERATION, PRESENTS, EnqueueParams, enqueue_presents, frozenset({PRESENTS}))
RANK_RETRY = AdminCommand(RANK_RETRY_OPERATION, PROGRESS, RetryParams, retry_rank_rewards,
                          frozenset({RANK_REWARDS, PRESENTS}), unavailable=requires(RANK_REWARDS))
COMMANDS = (GOLD_CREDIT, GOLD_DEBIT, GOLD_SET, FREE_CRYSTALS, PAID_CRYSTALS, CRYSTAL_DEBIT, EXPERIENCE, ENERGY,
            CAPACITY, ITEM_CREDIT, ITEM_DEBIT, SKIN_GRANT, ENQUEUE, RANK_RETRY)
CLASS_ROUTES = MappingProxyType({ItemClass.PASSIVE: ITEM_CREDIT.operation, ItemClass.SKIN: SKIN_GRANT.operation,
                                 ItemClass.EVENT_PASS: POINT_GRANT, ItemClass.VOTE_TICKET: TICKET_GRANT,
                                 **dict.fromkeys((ItemClass.OWNERSHIP, ItemClass.EMBLEM, ItemClass.RECOVERY,
                                                  ItemClass.ITEM_TICKET), ENQUEUE.operation)})


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)

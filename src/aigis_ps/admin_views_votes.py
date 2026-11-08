from __future__ import annotations

from typing import Any

from .admin_fields import (ASSAULT_EXPIRY, ASSAULT_NOTIFIER, COOKING_NOTIFIER, EXPIRY_LEDGER, EXPIRY_NOTIFIER,
                           JOURNAL_LEDGER, LAST_ACTION, RANK_REWARDS, RANKING_LEDGER, REWARD_LEDGER, TICKET_LEDGER,
                           VOTE_COOKING, VOTE_COOKING_LEDGER, VOTE_COOKING_NOTICE_LEDGER, VOTES, transactions)
from .admin_registry import ViewRegistry
from .admin_views import ViewContext, service
from .admin_views_events import (EntryStatus, LedgerView, Readback, Rendered, definition_check, entry, ledger_views,
                                 numeric, read_ledger, unavailable, without)
from .vote_expiry import ASSAULT_KEY as ASSAULT_LEDGER, ASSAULT_NOTIFICATION_KEY as ASSAULT_NOTICE_LEDGER
from .vote_expiry import NOTIFICATION_KEY as EXPIRY_NOTICE_LEDGER
from .vote_state import read_vote_history
from .vote_transactions import HISTORY_NAMESPACE as RECEIPT_LEDGER


# 55 answers the vote screens with candidate counts, reward rows and the assault entitlement.
VOTE_READ = 55
# A resent 57 replays its stored receipt instead of voting again.
VOTE_SUBMIT = 57
FROZEN, LIVE, RECORDED = "frozen", "live", "recorded"
VOTE_GROUP_MISSING = "admin_view_vote_group_missing"


def group_definition(rewards: Any, item_id: str) -> str:
    group = next((group for group in rewards.catalog.groups if group.item_id == item_id), None)
    if group is None:
        raise unavailable(VOTE_GROUP_MISSING, "The current vote catalog has no group for this ticket")
    return rewards.group_definition(group)


def vote_history(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, rows = read_ledger(lambda: read_vote_history(state))
    if rows is None:
        return Rendered(status)
    # read_vote_history validates the journal against these rows before returning them.
    journal = state.get(JOURNAL_LEDGER, {}).get("entries", {})
    entries = []
    for row in sorted(rows, key=lambda row: (row["groupId"], row["itemId"], row["cardId"])):
        saved = journal.get(str(row["key"]))
        frozen = None if saved is None else saved["definition"]
        entries.append(entry({"item_id": row["itemId"], "card_id": row["cardId"]},
                             EntryStatus.BASELINE if frozen is None else EntryStatus.BOUND,
                             definition=definition_check(frozen, lambda: group_definition(
                                 transactions(bundle).rewards, row["itemId"])),
                             detail={"group_id": row["groupId"], "count": row["count"],
                                     "baseline_count": None if saved is None else saved["baseline_count"]}))
    return Rendered({**status, "journal": JOURNAL_LEDGER in state}, entries)


def vote_rewards(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, rewards = read_ledger(lambda: transactions(bundle).rewards)
    if rewards is None:
        return Rendered(status)
    status, validated = read_ledger(lambda: rewards.validated(state))
    if validated is None:
        return Rendered(status)
    totals, ledger = validated
    if ledger is None:
        return Rendered({**status, "stored": False}, sections={"totals": totals, "items": [], "assault": {}})
    records = sorted(ledger["items"].items())
    items = [{"item_id": item, **without(record, "deliveries", "definition"), "deliveries": len(record["deliveries"]),
              "definition": definition_check(record["definition"], lambda: group_definition(rewards, item))}
             for item, record in records]
    entries = [entry({"item_id": item, "point": int(point)}, EntryStatus.DELIVERED, detail=delivery)
               for item, record in records for point, delivery in sorted(record["deliveries"].items(), key=numeric)]
    return Rendered({**status, "stored": True}, entries,
                    {"totals": totals, "items": items, "assault": ledger["assault"]})


def vote_tickets(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, tickets = read_ledger(lambda: transactions(bundle).tickets)
    if tickets is None:
        return Rendered(status)
    status, ticket = read_ledger(lambda: tickets.ticket_status(state))
    if ticket is None:
        return Rendered(status)
    daily = entry({"item_id": ticket["item_id"]}, ticket["status"], reason=ticket["reason"],
                  detail={"count": ticket["count"], "ticket_day": ticket["ticket_day"]})
    return Rendered({**status, "journal": state.get(TICKET_LEDGER)}, [daily])


def vote_rankings(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, finalists = read_ledger(lambda: transactions(bundle).finalists)
    if finalists is None:
        return Rendered(status)
    status, snapshots = read_ledger(lambda: finalists.validated(state))
    if snapshots is None:
        return Rendered(status)
    ordered = sorted(snapshots.items(), key=lambda item: item[1]["group_id"])
    entries = [entry({"group_id": snapshot["group_id"], "card_id": row["CardID"]}, FROZEN,
                     detail={"count": row["Count"], "qualified": row["CardID"] in snapshot["qualified_ids"]})
               for _, snapshot in ordered for row in snapshot["counts"]]
    frozen = [{"item_id": item, **without(snapshot, "counts", "qualified_ids", "definition"),
               "definition": definition_check(snapshot["definition"], lambda: finalists.content_version)}
              for item, snapshot in ordered]
    live = []
    for group in finalists.catalog.groups:
        if group.item_id in snapshots:
            continue
        ranking, rows = read_ledger(lambda: finalists.ranking(state, group))
        entries.extend(entry({"group_id": group.group_id, "card_id": row["CardID"]}, LIVE,
                             detail={"count": row["Count"]}) for row in rows or ())
        live.append({"group_id": group.group_id, "item_id": group.item_id, "final": group.final, **ranking})
    return Rendered(status, entries, {"frozen": frozen, "live": live})


def vote_expiry(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, expiry = read_ledger(lambda: transactions(bundle).expiry)
    if expiry is None:
        return Rendered(status)
    status, items = read_ledger(lambda: expiry.ledger(state))
    if items is None:
        return Rendered(status)
    entries = [entry({"item_id": item, "group_id": record["group_id"]}, record["status"], reason=record["reason"],
                     detail=without(record, "group_id", "status", "reason"))
               for item, record in sorted(items.items())]
    notices, records = read_ledger(lambda: service(service(bundle, RANK_REWARDS), EXPIRY_NOTIFIER).records(state))
    return Rendered(status, entries, {"notifications": {**notices, "records": records}})


def assault_conversion(record: dict) -> dict:
    return {**without(record, "status", "reason"), "gold": record["amount"] * record["rate"]}


def vote_assault(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, assault = read_ledger(lambda: service(transactions(bundle), ASSAULT_EXPIRY))
    if assault is None:
        return Rendered(status)
    # The journal is listed only after it matches the saved challenge rights and its gold mails.
    status, events = read_ledger(lambda: assault.ledger(state))
    if events is None:
        return Rendered(status)
    entries = [entry({"event_id": int(key)}, record["status"], reason=record["reason"],
                     detail=assault_conversion(record))
               for key, record in sorted(events.items(), key=numeric)]
    notices, records = read_ledger(lambda: service(service(bundle, RANK_REWARDS), ASSAULT_NOTIFIER).records(state))
    return Rendered(status, entries, {"notifications": {**notices, "records": records}})


def vote_cooking(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, cooking = read_ledger(lambda: service(bundle, VOTE_COOKING))
    if cooking is None:
        return Rendered(status)
    status, groups = read_ledger(lambda: cooking.ledger(state))
    if groups is None:
        return Rendered(status)
    # Delivery waits while the stored recipe source differs from the current one.
    entries = [entry({"item_id": item, "group_id": record["group_id"]}, record["status"], reason=record["reason"],
                     definition=definition_check(record["source"], lambda: cooking.recipes.content_version),
                     detail=without(record, "group_id", "status", "reason", "source"))
               for item, record in sorted(groups.items())]
    notices, records = read_ledger(lambda: service(bundle, COOKING_NOTIFIER).ledger(state))
    return Rendered(status, entries, {"notifications": {**notices, "records": records}})


def vote_receipts(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, voting = read_ledger(lambda: transactions(bundle))
    if voting is None:
        return Rendered(status)
    status, history = read_ledger(lambda: voting.history(state, context.revision))
    if history is None:
        return Rendered(status)
    entries = [entry({"request": receipt["request"]}, RECORDED, detail=without(receipt, "request"))
               for receipt in history["receipts"]]
    return Rendered({**status, "processed_keys": len(history["processed_keys"])}, entries)


LEDGERS = (
    LedgerView("history", VOTES, (JOURNAL_LEDGER,), Readback(("vote",), requests=(VOTE_READ,)), vote_history),
    LedgerView("rewards", VOTES, (REWARD_LEDGER,), Readback(("presents",), requests=(VOTE_READ,)), vote_rewards),
    LedgerView("tickets", VOTES, (TICKET_LEDGER,), Readback(("possessions",)), vote_tickets),
    LedgerView("rankings", VOTES, (RANKING_LEDGER,), Readback(requests=(VOTE_READ,)), vote_rankings),
    LedgerView("expiry", VOTES, (EXPIRY_LEDGER, EXPIRY_NOTICE_LEDGER), Readback(("presents", "possessions")),
               vote_expiry),
    LedgerView("assault", VOTES, (ASSAULT_LEDGER, ASSAULT_NOTICE_LEDGER), Readback(("presents",), requests=(VOTE_READ,)),
               vote_assault),
    LedgerView("cooking", VOTE_COOKING, (VOTE_COOKING_LEDGER, VOTE_COOKING_NOTICE_LEDGER), Readback(("presents",)),
               vote_cooking),
    LedgerView("receipts", VOTES, (RECEIPT_LEDGER, LAST_ACTION), Readback(requests=(VOTE_SUBMIT,)), vote_receipts),
)
VIEWS = ledger_views(VOTES, LEDGERS)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)

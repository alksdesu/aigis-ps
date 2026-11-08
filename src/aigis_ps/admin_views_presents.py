from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
from http import HTTPStatus
from types import MappingProxyType
from typing import Any

from .admin_cmd_economy import PRESENTS
from .admin_command import AdminError
from .admin_commit import attempt
from .admin_diff import RESPONSE_FIELD
from .admin_registry import ViewRegistry
from .admin_views import PageParams, ViewContext, page, redacted_view, service
from .item_classes import ITEM_PRESENT_TYPE
from .present_backlog import EMPTY_MAILBOX, PresentBacklog
from .present_items import LEGACY_ITEMS
from .presents import MAX_PRESENTS, MAX_RETRIES, PresentService
from .quest_service import JOURNAL_KEY


NOTIFICATION_FIELD, NOTIFICATION_WIRE = "NotificationCategory", "A9"
NOTIFICATION_POLICY = "No console command writes notification categories; event pass mail sets its own category"
BACKLOG_POLICY = ("Waiting battle presents are mailed by the next battle settlement that finds mailbox room; "
                  "the console never edits them")
TYPE_KINDS = MappingProxyType({0: "free_crystal", 1: "unit", 2: "gold", 8: "charisma_recovery", 16: "summon_ticket",
                               20: "selection_ticket", 21: "vote_cooking", ITEM_PRESENT_TYPE: "item",
                               **dict.fromkeys(LEGACY_ITEMS, "legacy_item")})
UNIT_TEMPLATE, SUMMON_TICKET, SELECTION_TICKET, CHARACTER_COOKING = 1, 16, 20, 21
PENDING_PRESENTS = "pending_presents"
LAST_PRESENT_ID, CLAIM_HISTORY_SCHEMA = "last_present_id", "present_claim_history_schema"
RESPONSE_ENCODING = "base64"
BACKLOG_INVALID = "admin_present_backlog_invalid"


def mail_target(row: dict, templates: Mapping[int, int]) -> dict | None:
    kind = row["Type"]
    if kind == UNIT_TEMPLATE:
        return {"template_id": row["Content"], "card_id": templates.get(row["Content"])}
    if kind == SUMMON_TICKET:
        return {"pool_id": row["SummonID"]}
    if kind == SELECTION_TICKET:
        return {"config_id": row["SelectionConfigID"]}
    if kind == CHARACTER_COOKING:
        return {"card_id": row["Content"]}
    if kind == ITEM_PRESENT_TYPE:
        return {"item_id": row["ItemID"]}
    if kind in LEGACY_ITEMS:
        return {"item_id": LEGACY_ITEMS[kind][0]}
    return None


def mail_row(row: dict, templates: Mapping[int, int]) -> dict:
    return {"PresentID": row["PresentID"], "Type": row["Type"], "kind": TYPE_KINDS.get(row["Type"]),
            "Content": row["Content"], "Description": row["Description"], "target": mail_target(row, templates),
            NOTIFICATION_FIELD: row.get(NOTIFICATION_FIELD, 0)}


def mailbox(presents: Any, state: dict, params: PageParams) -> dict:
    rows, templates = presents.rows(state), presents.templates
    return {"count": len(rows), "capacity": MAX_PRESENTS, "last_present_id": state.get(LAST_PRESENT_ID, 0),
            "notification_category": {"field": NOTIFICATION_FIELD, "wire": NOTIFICATION_WIRE, "read_only": True,
                                      "reason": NOTIFICATION_POLICY},
            "mails": page(rows, params, lambda row: mail_row(row, templates))}


def mailbox_view(context: ViewContext, params: PageParams) -> dict:
    presents = service(context.generation.bundle, PRESENTS)
    report = attempt(lambda: mailbox(presents, context.state, params))
    # A summon ticket bundle fails the strict rows, so the splittable mails stay readable beside the failure.
    return {**report, "bundled_tickets": attempt(lambda: {"ids": list(presents.bundled_tickets(context.state))})}


def deliverable(presents: Any, reward: dict) -> dict:
    # The journal keeps only presents an empty mailbox accepts, so this shows how many mails each copy becomes.
    return attempt(lambda: {"mails_per_copy": len(presents.preview([reward], EMPTY_MAILBOX))})


def backlog(presents: Any, state: dict) -> dict:
    journal = state.get(JOURNAL_KEY) or {}
    entries = journal.get(PENDING_PRESENTS, []) if isinstance(journal, dict) else None
    if not PresentBacklog.valid(entries):
        raise AdminError(BACKLOG_INVALID, "The battle journal holds an invalid present backlog",
                         HTTPStatus.SERVICE_UNAVAILABLE)
    return {"read_only": True, "reason": BACKLOG_POLICY, "copies": sum(entry["copies"] for entry in entries),
            "entries": [{**entry, "delivery": deliverable(presents, entry["reward"])} for entry in entries]}


def backlog_view(context: ViewContext, params: None) -> dict:
    return attempt(lambda: backlog(service(context.generation.bundle, PRESENTS), context.state))


def digested(entry: dict) -> dict:
    # Claim receipts carry full 1/2/117 responses, and the generic redaction spots only replies that open with XML.
    raw = base64.b64decode(entry[RESPONSE_FIELD], validate=True)
    return {**entry, RESPONSE_FIELD: {"redacted": RESPONSE_FIELD, "encoding": RESPONSE_ENCODING, "bytes": len(raw),
                                      "sha256": hashlib.sha256(raw).hexdigest()}}


def receipts(state: dict, revision: int, params: PageParams) -> dict:
    entries = PresentService.history(state, revision)
    return {"schema_version": state.get(CLAIM_HISTORY_SCHEMA), "limit": MAX_RETRIES,
            "receipts": page(entries, params, digested)}


def receipts_view(context: ViewContext, params: PageParams) -> dict:
    return attempt(lambda: receipts(context.state, context.revision, params))


VIEWS = (
    redacted_view(f"{PRESENTS}.mailbox", PRESENTS, mailbox_view, PageParams),
    redacted_view(f"{PRESENTS}.backlog", PRESENTS, backlog_view),
    redacted_view(f"{PRESENTS}.receipts", PRESENTS, receipts_view, PageParams),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)

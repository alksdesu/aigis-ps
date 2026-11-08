from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .admin_command import CommandParams
from .admin_commit import attempt
from .admin_registry import ViewRegistry
from .admin_views import PageParams, ViewContext, page, redacted_view, service
from .admin_views_tools import Identifier
from .entrust_service import read_history
from .native_numbers import INT32_MAX, UINT32_MASK
from .quest_modes import (CLEAR_FLAG, EPISODE_FLAG, HARD_STAR_FLAG, MODE_PROGRESS, PLATFORM_CODES, SLOT_FLAGS,
                          THREE_STAR_FLAG)
from .quest_record_admin import QUEST_ID, RECORD_TABLE, master_quest_ids, read_archive
from .quest_service import active_identity, journal as read_journal


BATTLE = "battle"
BOOTSTRAP, QUESTS, ENTRUST = "bootstrap", "quests", "entrust"
# The local dmm-r18 client sends PlatformCode 9.
LOCAL_CLIENT_PLATFORM = 9
PlatformCode = Annotated[int, Field(ge=min(PLATFORM_CODES), le=max(PLATFORM_CODES))]
Quantity = Annotated[int, Field(ge=1, le=INT32_MAX)]


class RecordParams(PageParams):
    quest_id: Identifier | None = None


class AvailabilityParams(CommandParams):
    platform_code: PlatformCode = LOCAL_CLIENT_PLATFORM


class EntrustQuestParams(AvailabilityParams):
    quest_id: Identifier


class EntrustPreviewParams(EntrustQuestParams):
    quantity: Quantity = 1


def record_flags(flags: int) -> dict:
    word = flags & UINT32_MASK
    return {"cleared": bool(word & CLEAR_FLAG), "three_star": bool(word & THREE_STAR_FLAG),
            "four_star": bool(word & HARD_STAR_FLAG), "episode": bool(word & EPISODE_FLAG),
            "slots": [index + 1 for index, bit in enumerate(SLOT_FLAGS) if word & bit]}


def decoded_record(row: dict) -> dict:
    return {**row, "flags": record_flags(row["AchieveFlag"])}


def _frozen_active(active: dict | None, identity, quests: Any) -> dict | None:
    if active is None:
        return None
    frozen = {name: active.get(name) for name in (
        "sequence", "quest_id", "start_key", "registration", "started_at", "master_snapshot", "resource_version",
        "charge", "mode", "rules", "candidate_stars", "selected_mask", "mission_id", "unit_reservations")}
    matches = None
    if quests is not None and identity is not None:
        matches = identity[:2] == (quests.catalog.snapshot.identifier, quests.catalog.resource_version)
    return {**frozen, "definition": active.get("definition"), "matches_running_generation": matches}


def journal_view(context: ViewContext, params: None) -> dict:
    state, bundle = context.state, context.generation.bundle
    quests = getattr(bundle, QUESTS, None)

    def read() -> dict:
        journal = read_journal(state)
        identity = active_identity(state)
        return {"schema_version": journal["schema_version"], "next_sequence": journal["next_sequence"],
                "active": _frozen_active(journal["active"], identity, quests),
                "pending_presents": journal.get("pending_presents", []),
                MODE_PROGRESS: journal.get(MODE_PROGRESS, {}),
                "history": journal["history"], "processed_keys": len(journal["processed_keys"])}

    return {"journal": attempt(read)}


def records_view(context: ViewContext, params: RecordParams) -> dict:
    bootstrap = service(context.generation.bundle, BOOTSTRAP)
    rows = bootstrap.table_rows(context.state, RECORD_TABLE)
    if params.quest_id is not None:
        rows = [row for row in rows if row[QUEST_ID] == params.quest_id]
    return page(rows, params, decoded_record)


def _candidate_report(context: ViewContext, quest_ids: frozenset[int]) -> dict | None:
    candidate = context.services.runtime.candidate()
    if candidate is None:
        return None

    def read() -> dict:
        staged = master_quest_ids(candidate.bundle.bootstrap)
        return {"master_quest_ids": len(staged),
                "missing": sorted(quest_id for quest_id in quest_ids if quest_id not in staged)}

    return {"generation_id": candidate.id, "master_snapshot": candidate.identity.master_snapshot,
            **attempt(read)}


def record_archive_view(context: ViewContext, params: None) -> dict:
    bootstrap = service(context.generation.bundle, BOOTSTRAP)
    rows = bootstrap.table_rows(context.state, RECORD_TABLE)
    quest_ids = frozenset(row[QUEST_ID] for row in rows)

    def current() -> dict:
        running = master_quest_ids(bootstrap)
        return {"missing": sorted(quest_id for quest_id in quest_ids if quest_id not in running)}

    return {"archived": attempt(lambda: {"rows": read_archive(context.state)}),
            "running_master": attempt(current),
            "candidate": _candidate_report(context, quest_ids)}


def entrust_view(context: ViewContext, params: None) -> dict:
    state, revision, bundle = context.state, context.revision, context.generation.bundle
    return {"status": attempt(lambda: {"status": service(bundle, ENTRUST).status()}),
            "history": attempt(lambda: {"history": read_history(state, revision)})}


def entrust_availability_view(context: ViewContext, params: AvailabilityParams) -> dict:
    entrust = service(context.generation.bundle, ENTRUST)
    return attempt(lambda: entrust.catalog.availability(now=context.now, platform_code=params.platform_code))


def entrust_quest_view(context: ViewContext, params: EntrustQuestParams) -> dict:
    entrust = service(context.generation.bundle, ENTRUST)
    return attempt(lambda: entrust.catalog.describe(params.quest_id, now=context.now,
                                                    platform_code=params.platform_code))


def entrust_preview_view(context: ViewContext, params: EntrustPreviewParams) -> dict:
    entrust = service(context.generation.bundle, ENTRUST)
    return attempt(lambda: entrust.preview(context.profile, params.quest_id, quantity=params.quantity,
                                           now=context.now, platform_code=params.platform_code))


VIEWS = (
    # The journal and the entrust catalog follow the running generation, so their answers change without a revision.
    redacted_view("battle.journal", BATTLE, journal_view, live=True),
    redacted_view("battle.records", BATTLE, records_view, RecordParams),
    redacted_view("battle.record_archive", BATTLE, record_archive_view, live=True),
    redacted_view("battle.entrust", BATTLE, entrust_view, live=True),
    redacted_view("battle.entrust_availability", BATTLE, entrust_availability_view, AvailabilityParams, live=True),
    redacted_view("battle.entrust_quest", BATTLE, entrust_quest_view, EntrustQuestParams, live=True),
    redacted_view("battle.entrust_preview", BATTLE, entrust_preview_view, EntrustPreviewParams, live=True),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)

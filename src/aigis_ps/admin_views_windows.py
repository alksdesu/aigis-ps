from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from http import HTTPStatus
from typing import Any

from .admin_command import AdminError
from .admin_commit import attempt
from .admin_registry import ViewRegistry
from .admin_views import ViewContext, redacted_view, service
from .asset_service import IndexedAssetService
from .growth_campaign import CAMPAIGN_RESOURCE
from .mission_window_revival import (ALLOWED_CATEGORIES, CAMPAIGN_WINDOW_POLICY, CATEGORY_KINDS, CONFIG_TABLES,
                                     MAX_RULE_BYTES, SCHEMA_VERSION, WINDOW_POLICY, CampaignMission, MissionBatch,
                                     MissionWindowRules, Window, campaign_state, growth_campaign_missions,
                                     mission_window_batches)
from .native_clock import native_clock
from .quest_modes import support_status
from .resource_library import ResourceLibrary


BATTLE = "battle"
NATIVE_CDN, MASTERS = "native_cdn", "masters"
REVIVAL, REPORT, REPORT_ERROR = "mission_window_revival", "mission_windows", "mission_window_error"
VERIFICATION = "verification"
CONFIG_KEY = "mission-windows"
SWITCH_TIER = "content"
BASE_CHANGED = "admin_windows_base_changed"
RESOURCES_UNAVAILABLE = "admin_windows_resources_unavailable"


def _unavailable(error: Exception) -> AdminError:
    return AdminError(RESOURCES_UNAVAILABLE, str(error), HTTPStatus.SERVICE_UNAVAILABLE)


def base_resource_reader(bundle: Any) -> Callable[[str], bytes]:
    # The running bundle serves patched resources, so native windows come from the base index it was built on.
    cdn = service(bundle, NATIVE_CDN)
    try:
        manifest, entries = ResourceLibrary(cdn.assets.resource_root,
                                            client_hash=service(bundle, MASTERS).expected_hash).load()
    except (OSError, ValueError) as error:
        raise _unavailable(error) from error
    if manifest["version"] != cdn.base_version:
        raise AdminError(BASE_CHANGED, "The active resource index differs from this generation's base resources",
                         HTTPStatus.SERVICE_UNAVAILABLE)
    base = IndexedAssetService(entries, cdn.assets.resource_root, cdn.assets.source_root)
    return lambda name: base.read(name)[0]


def running_rules(bundle: Any) -> MissionWindowRules | None:
    revival = getattr(bundle, REVIVAL, None)
    return None if revival is None else revival.rules


def configured_windows(rules: MissionWindowRules | None) -> tuple[Mapping[str, Window], Mapping[str, Window]]:
    if rules is None or not rules.enabled:
        return {}, {}
    return rules.batches, rules.growth_campaigns


def batch_entry(batch: MissionBatch, configured: Window | None, now: datetime) -> dict:
    window = batch.original if configured is None else configured
    return {**batch.to_dict(), "window": window.to_dict(), "configured": configured is not None,
            "state": window.state(batch.category, now),
            "original_state": batch.original.state(batch.category, now)}


def category_section(category: int, batches: tuple[MissionBatch, ...], configured: Mapping[str, Window],
                     now: datetime, modes: Mapping[int, dict]) -> dict:
    return {"category": category, "kind": CATEGORY_KINDS[category], "table": CONFIG_TABLES[category],
            "support": {quest_type: entry for quest_type, entry in modes.items()
                        if entry["mission_category"] == category},
            "batches": [batch_entry(batch, configured.get(batch.key), now)
                        for batch in batches if batch.category == category]}


def campaign_entry(mission: CampaignMission, configured: Window | None, clock: datetime) -> dict:
    window = mission.original if configured is None else configured
    return {**mission.to_dict(), "window": None if window is None else window.to_dict(),
            "configured": configured is not None, "state": campaign_state(window, clock),
            "original_state": campaign_state(mission.original, clock)}


def campaign_section(missions: tuple[CampaignMission, ...], configured: Mapping[str, Window],
                     clock: datetime) -> dict:
    return {"table": CAMPAIGN_RESOURCE, "window_policy": CAMPAIGN_WINDOW_POLICY,
            "clock": clock.isoformat(timespec="milliseconds"),
            "missions": [campaign_entry(mission, configured.get(str(mission.mission_id)), clock)
                         for mission in missions]}


def preset(rules: MissionWindowRules | None) -> dict:
    document = (MissionWindowRules() if rules is None else rules).to_dict()
    return {"config_key": CONFIG_KEY, "tier": SWITCH_TIER, "max_bytes": MAX_RULE_BYTES, "document": document}


def windows_view(context: ViewContext, params: None) -> dict:
    bundle = context.generation.bundle
    rules = running_rules(bundle)

    def read() -> dict:
        reader = base_resource_reader(bundle)
        try:
            batches = mission_window_batches(reader)
            missions = growth_campaign_missions(reader)
        except ValueError as error:
            raise _unavailable(error) from error
        batch_rules, campaign_rules = configured_windows(rules)
        modes = support_status()
        report = getattr(bundle, REPORT, None) or {}
        return {"schema_version": SCHEMA_VERSION, "enabled": rules is not None and rules.enabled,
                "window_policy": WINDOW_POLICY,
                "rules_hash": None if rules is None else rules.content_version,
                VERIFICATION: report.get(VERIFICATION),
                "error": getattr(bundle, REPORT_ERROR, None),
                "categories": [category_section(category, batches, batch_rules, context.now, modes)
                               for category in ALLOWED_CATEGORIES],
                "growth_campaigns": campaign_section(missions, campaign_rules, native_clock(context.now)),
                "preset": preset(rules)}

    return attempt(read)


VIEWS = (
    # Windows follow the clock and batches the active resource pointer, so the answer changes without a revision.
    redacted_view("battle.windows", BATTLE, windows_view, live=True),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
from types import MappingProxyType

from .event_login_catalog import EventLoginCatalog, RESOURCE_NAME
from .event_login_rules import EventLoginRules
from .table_cell_patch import patch_table_cells


@dataclass(frozen=True)
class EventLoginRevivalResult:
    replacements: MappingProxyType
    campaign_ids: tuple[int, ...]
    source_sha256: str
    effective_sha256: str
    rules_hash: str
    rules: EventLoginRules

    @property
    def metadata(self):
        return {"schema_version": 1, "rules": self.rules.to_dict(), "rules_hash": self.rules_hash,
                "campaign_ids": list(self.campaign_ids), "source_sha256": self.source_sha256,
                "effective_sha256": self.effective_sha256,
                "window_policy": "explicit_local_eligibility_start_inclusive_end_exclusive"}


def build_event_login_revival(payload: bytes, rules: EventLoginRules | None = None) -> EventLoginRevivalResult:
    rules = EventLoginRules() if rules is None else rules
    if not isinstance(rules, EventLoginRules):
        raise ValueError("Event login revival requires verified rules")
    verified_rules = EventLoginRules(**rules.to_dict())
    if verified_rules.to_dict() != rules.to_dict():
        raise ValueError("Event login revival rules changed after validation")
    catalog = EventLoginCatalog(lambda _: payload)
    updates = {}
    if rules.enabled:
        for identifier, window in rules.revivals.items():
            campaign = catalog.get(identifier)
            if (campaign.login_bonus_type != 0 or campaign.expiration
                    or any(reward.pause_period or reward.evolution_count for reward in campaign.rewards)):
                raise ValueError("Only ordinary fixed-window event login campaigns can be revived")
            updates[campaign.source_index] = {"StartDate": date.fromisoformat(window["start_date"]).toordinal() - 1,
                                              "EndDate": date.fromisoformat(window["end_date"]).toordinal() - 1}
    effective = patch_table_cells(payload, updates) if updates else payload
    updated = EventLoginCatalog(lambda _: effective)
    for old, new in zip(catalog.campaigns, updated.campaigns):
        if old.campaign_id not in rules.revivals or not rules.enabled:
            if new != old:
                raise ValueError("Event login revival changed an unselected campaign")
        elif (new.rewards != old.rewards or new.max_ordinal != old.max_ordinal
                or new.login_bonus_type != old.login_bonus_type or new.expiration != old.expiration):
            raise ValueError("Event login revival changed rewards or eligibility counters")
        elif (new.start_date.isoformat() != rules.revivals[old.campaign_id]["start_date"]
                or new.end_date.isoformat() != rules.revivals[old.campaign_id]["end_date"]):
            raise ValueError("Event login revival did not apply the selected window")
    return EventLoginRevivalResult(MappingProxyType({RESOURCE_NAME: effective} if effective != payload else {}),
        tuple(sorted(rules.revivals)) if rules.enabled else (), hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(effective).hexdigest(), rules.content_version, verified_rules)

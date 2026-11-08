from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from enum import StrEnum
import hashlib
import json
import re
from types import MappingProxyType

from .errors import ProtocolError
from .event_pass_catalog import ITEM_RESOURCE_NAME
from .present_items import OWNERSHIP_EFFECTS, PASSIVE_EFFECTS
from .vote_tickets import DAILY_ITEM


ITEM_PRESENT_TYPE = 22
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class ItemClass(StrEnum):
    PASSIVE = "passive"
    SKIN = "skin"
    OWNERSHIP = "ownership"
    RECOVERY = "recovery"
    ITEM_TICKET = "item_ticket"
    EMBLEM = "emblem"
    EVENT_PASS = "event_pass"
    VOTE_TICKET = "vote_ticket"
    STACK_ZERO = "stack_zero"
    UNKNOWN = "unknown"


FALLBACK_CLASSES = (ItemClass.STACK_ZERO, ItemClass.UNKNOWN)


def _catalog_rows(items) -> Mapping[str, Mapping]:
    rows = items.rows
    if not isinstance(rows, Mapping):
        raise ValueError("Item classes require the verified item definitions")
    for name, row in rows.items():
        if (type(name) is not str or not isinstance(row, Mapping)
                or type(row.get("EffectType")) is not int or type(row.get("Stack")) is not int):
            raise ValueError("Item definitions require integer effect types and stack limits")
    return rows


def _ready_status(source, name: str) -> Mapping:
    status = source.status()
    if not isinstance(status, Mapping) or status.get("ready") is not True:
        raise ValueError(f"Item class source {name} is unavailable")
    return status


def _same_resource(value, resource: str, name: str) -> None:
    if value != resource:
        raise ValueError(f"Item class source {name} uses a different item resource")


def _mailable(items, name: str, *, single_only: bool) -> bool:
    try:
        definition = items.definition(ITEM_PRESENT_TYPE, name)
    except ProtocolError as error:
        if error.status != 501:
            raise
        return False
    if definition.single_only is not single_only:
        raise ValueError("Item mail ownership disagrees with its effect type")
    return True


def _owners(claims: Mapping[ItemClass, frozenset[str]]) -> dict[str, ItemClass]:
    owners = {}
    for item_class, names in claims.items():
        for name in names:
            if name in owners:
                raise ValueError(f"Item {name} is claimed by both {owners[name]} and {item_class}")
            owners[name] = item_class
    return owners


class ItemClasses:
    def __init__(self, items, *, recovery_catalog=None, item_tickets=None, pass_points=None, vote_tickets=None):
        self._items = items
        self._recovery = recovery_catalog
        self._item_tickets = item_tickets
        self._pass_points = pass_points
        self._vote_tickets = vote_tickets
        try:
            self._ticket_catalog = item_tickets.catalog if item_tickets is not None else None
            self._pass_catalog = pass_points.catalog if pass_points is not None else None
            self._vote_catalog = vote_tickets.catalog if vote_tickets is not None else None
            self._versions = self._current_versions()
            _ready_status(items, "items")
            rows = _catalog_rows(items)
            owners = _owners({**self._catalog_claims(rows), **self._source_claims(rows)})
            if owners.keys() - rows.keys():
                raise ValueError("Item class sources refer to items outside the item resource")
            classes = {name: owners.get(name, ItemClass.STACK_ZERO if row["Stack"] == 0 else ItemClass.UNKNOWN)
                       for name, row in rows.items()}
            self._classes = MappingProxyType(classes)
            self._members = MappingProxyType({item_class: frozenset(name for name, value in classes.items()
                                                                    if value is item_class)
                                              for item_class in ItemClass})
            self._fallback_effects = MappingProxyType({item_class: MappingProxyType(dict(sorted(Counter(
                rows[name]["EffectType"] for name in self._members[item_class]).items())))
                for item_class in FALLBACK_CLASSES})
            self._content_version = hashlib.sha256(json.dumps({
                "schema": 1, "sources": self._versions, "classes": [item_class.value for item_class in ItemClass],
                "passive_effects": sorted(PASSIVE_EFFECTS), "ownership_effects": dict(OWNERSHIP_EFFECTS),
                "passive": "positive_stack_verified_mail", "ownership": "verified_single_mail_except_skins",
                "fallback": "stack_zero_then_unknown"}, sort_keys=True).encode()).hexdigest()
        except (AttributeError, KeyError, TypeError, ValueError, ProtocolError) as error:
            raise ProtocolError("item_classes_invalid", "Item classes require consistent verified catalogs", 503) from error

    def _sources(self) -> dict[str, object]:
        return {"recovery": self._recovery, "item_tickets": self._ticket_catalog,
                "event_pass": self._pass_points, "vote_tickets": self._vote_tickets}

    def _current_versions(self) -> dict[str, str | None]:
        sources = self._sources()
        versions = {"items": self._items.content_version, "item_resource": self._items.asset_sha256}
        versions.update((name, source.content_version) for name, source in sources.items() if source is not None)
        if any(type(value) is not str or not _HASH.fullmatch(value) for value in versions.values()):
            raise ValueError("Item class sources require stable fingerprints")
        return {**versions, **{name: None for name, source in sources.items() if source is None}}

    def _catalog_claims(self, rows) -> dict[ItemClass, frozenset[str]]:
        skins = frozenset(self._items.skin_item_ids)
        passive, ownership = set(), set()
        for name, row in rows.items():
            effect = row["EffectType"]
            if effect in PASSIVE_EFFECTS and row["Stack"] > 0 and _mailable(self._items, name, single_only=False):
                passive.add(name)
            elif effect in OWNERSHIP_EFFECTS and name not in skins and _mailable(self._items, name, single_only=True):
                ownership.add(name)
        return {ItemClass.PASSIVE: frozenset(passive), ItemClass.SKIN: skins, ItemClass.OWNERSHIP: frozenset(ownership),
                ItemClass.EMBLEM: frozenset(self._items.emblem_item_ids)}

    def _source_claims(self, rows) -> dict[ItemClass, frozenset[str]]:
        resource = self._versions["item_resource"]
        claims = {}
        if self._recovery is not None:
            status = _ready_status(self._recovery, "recovery")
            _same_resource(status["resource_hashes"][ITEM_RESOURCE_NAME], resource, "recovery")
            claims[ItemClass.RECOVERY] = frozenset(name for name in rows if self._recovery.supports_item(name))
        if self._ticket_catalog is not None:
            _ready_status(self._ticket_catalog, "item_tickets")
            _same_resource(self._ticket_catalog.item_resource_sha256, resource, "item_tickets")
            claims[ItemClass.ITEM_TICKET] = frozenset(self._ticket_catalog.item_ids)
        if self._pass_points is not None:
            _ready_status(self._pass_points, "event_pass")
            _same_resource(self._pass_catalog.resource_hashes[ITEM_RESOURCE_NAME], resource, "event_pass")
            claims[ItemClass.EVENT_PASS] = frozenset(identifier for entry in self._pass_catalog.passes
                                                     for identifier in (entry.item_id, entry.premium_item_id)
                                                     if identifier)
        if self._vote_tickets is not None:
            status = _ready_status(self._vote_tickets, "vote_tickets")
            _same_resource(status["resource_sha256"], resource, "vote_tickets")
            claims[ItemClass.VOTE_TICKET] = frozenset((DAILY_ITEM, *(group.item_id for group in self._vote_catalog.groups)))
        return claims

    def _ready(self):
        try:
            if ((self._item_tickets is not None and self._item_tickets.catalog is not self._ticket_catalog)
                    or (self._pass_points is not None and self._pass_points.catalog is not self._pass_catalog)
                    or (self._vote_tickets is not None and self._vote_tickets.catalog is not self._vote_catalog)
                    or self._current_versions() != self._versions):
                raise ValueError("Item class sources changed")
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("item_classes_changed", "Item class sources changed after setup", 503) from error

    @property
    def content_version(self) -> str:
        return self._content_version

    def classify(self, item_id: str) -> ItemClass:
        self._ready()
        if type(item_id) is not str or item_id not in self._classes:
            raise ProtocolError("item_class_unknown_item", "Item is not a known canonical definition")
        return self._classes[item_id]

    def item_ids(self, item_class: ItemClass | str) -> frozenset[str]:
        self._ready()
        try:
            selected = ItemClass(item_class)
        except (TypeError, ValueError) as error:
            raise ProtocolError("item_class_invalid", "Expected a known item class") from error
        return self._members[selected]

    def status(self) -> dict:
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "content_version": self._content_version,
                "item_resource_sha256": self._versions["item_resource"], "item_count": len(self._classes),
                "counts": {item_class.value: len(self._members[item_class]) for item_class in ItemClass},
                "sources": {**{name: source is not None for name, source in self._sources().items()},
                            "emblems": bool(self._members[ItemClass.EMBLEM])},
                "fallback_effect_types": {item_class.value: dict(self._fallback_effects[item_class])
                                          for item_class in FALLBACK_CLASSES},
                "passive_effects": sorted(PASSIVE_EFFECTS), "ownership_effects": sorted(OWNERSHIP_EFFECTS),
                "policy": ("Each item belongs to the one verified catalog that owns it; only passive stock "
                           "accepts direct credits; unowned items fall back to stack_zero, then unknown")}

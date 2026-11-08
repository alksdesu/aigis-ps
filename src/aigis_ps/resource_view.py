from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
import hashlib
from importlib.resources import files
import json
from pathlib import Path
from types import MappingProxyType

from .asset_service import IndexedAssetService
from .command_text_overlay import CommandTextOverlay, CommandTextOverrides, build_command_text_overlay
from .local_io import read_bounded
from .local_present_templates import build_present_template_overlay
from .event_pass_revival import (EventPassRevivalResult, EventPassRevivalRules, MISSION_RESOURCE,
                                PASS_RESOURCE, build_event_pass_revival)
from .package_text_overlay import (IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE, PackageTextOverlay,
                                   build_package_text_overlay, event_pass_package_ids)
from .event_login_catalog import EventLoginCatalog, RESOURCE_NAME as EVENT_LOGIN_RESOURCE
from .event_login_revival import EventLoginRevivalResult, build_event_login_revival
from .event_login_rules import EventLoginRules
from .mission_window_revival import MissionWindowRevival, MissionWindowRules, build_mission_window_revival
from .purchased_login_rules import PurchasedLoginRules
from .purchased_login_texts import PurchasedLoginTexts, build_purchased_login_texts
from .public_tables import validate_public_master
from .resource_library import ResourceLibrary
from .resource_overlay import build_resource_overlay
from .shop_window_closure import ShopWindowClosure, build_shop_window_closure
from .trade_catalog import SHOP_RESOURCE


PRESENT_TEMPLATES = "PresentUnitData.atb"


@dataclass(frozen=True)
class ResourceView:
    manifest: Mapping
    entries: tuple
    index: bytes
    replacements: Mapping[str, bytes]
    assets: IndexedAssetService
    base_assets: IndexedAssetService | None = None
    revival: EventPassRevivalResult | None = None
    revival_rules: EventPassRevivalRules | None = None
    package_texts: PackageTextOverlay | None = None
    event_login: EventLoginRevivalResult | None = None
    purchased_login: PurchasedLoginTexts | None = None
    mission_window: MissionWindowRevival | None = None
    command_texts: CommandTextOverlay | None = None
    shop_window_closure: ShopWindowClosure | None = None
    base_index: bytes | None = None

    @property
    def revival_metadata(self):
        if self.revival is None:
            return None
        return {"schema_version": 1, "rules": self.revival_rules.to_dict(), "rules_hash": self.revival.rules_hash,
                "source_hashes": dict(self.revival.source_hashes), "effective_hashes": dict(self.revival.effective_hashes),
                "metadata": {key: list(value) if isinstance(value, tuple) else value
                             for key, value in self.revival.metadata.items()}}

    @property
    def event_login_metadata(self):
        return self.event_login.metadata if self.event_login is not None else None

    @property
    def purchased_login_metadata(self):
        return self.purchased_login.metadata if self.purchased_login is not None else None

    @property
    def mission_window_metadata(self):
        return self.mission_window.metadata if self.mission_window is not None else None

    @property
    def command_text_metadata(self):
        return self.command_texts.metadata if self.command_texts is not None else None

    @property
    def shop_window_closure_metadata(self):
        return self.shop_window_closure.metadata if self.shop_window_closure is not None else None

    @cached_property
    def unclosed_resources(self) -> Mapping[str, Mapping[str, str]]:
        # Requests committed before the closure existed name the version this view had without it.
        closure = self.shop_window_closure
        if closure is None or SHOP_RESOURCE not in self.replacements or self.base_index is None:
            return MappingProxyType({})
        others = {name: payload for name, payload in self.replacements.items() if name != SHOP_RESOURCE}
        version = build_resource_overlay(self.manifest["base_version"], self.base_index, others).version
        return MappingProxyType({version: MappingProxyType({SHOP_RESOURCE: closure.source_sha256})})


def _replace(replacements: dict, owners: dict, owner: str, updates: Mapping[str, bytes]) -> None:
    replacements.update(updates)
    owners.update(dict.fromkeys(updates, owner))


def _claim(owners: Mapping[str, str], label: str, targets) -> None:
    # Overlays patched from the base resources would silently drop the bytes of an earlier overlay.
    taken = [name for name in targets if name in owners]
    if taken:
        raise ValueError(f"{label} target a resource another local overlay already replaces: "
                         + ", ".join(f"{name} ({owners[name]})" for name in taken))


def load_resource_view(resource_root, source_root, client_hash, *, master_provider=None, revival_rules=None,
                       event_login_rules=None, purchased_login_rules=None, mission_window_rules=None,
                       command_text_overrides=None) -> ResourceView:
    resource_root, source_root = Path(resource_root), Path(source_root)
    manifest, entries = ResourceLibrary(resource_root, client_hash=client_hash).load()
    raw = read_bounded(resource_root / "indexes" / manifest["snapshot"] / "index.bin", 32 * 1024 * 1024, root=resource_root)
    if hashlib.sha256(raw).hexdigest() != manifest["index_sha256"]:
        raise ValueError("Resource index changed while constructing its effective view")
    replacements, owners = {}, {}
    base_assets = IndexedAssetService(entries, resource_root, source_root)

    def base_resource(name):
        return base_assets.read(name)[0]

    if any(entry.name == PRESENT_TEMPLATES for entry in entries):
        if not callable(master_provider):
            raise ValueError("Local present templates require a verified CardData provider")
        schemas = json.loads(files("aigis_ps").joinpath("data/master-schemas.json").read_text("utf-8"))
        fields = {name: [field["name"] for field in table["fields"]] for name, table in schemas["tables"].items()}
        document, _ = validate_public_master("CardData", master_provider("CardData"), fields)
        card_ids = next(column.values for column in document.children if column.name == "CardID")
        _replace(replacements, owners, "present_templates",
                 {PRESENT_TEMPLATES: build_present_template_overlay(base_resource(PRESENT_TEMPLATES), card_ids)})
    revival = package_texts = None
    if revival_rules is not None:
        if not isinstance(revival_rules, EventPassRevivalRules):
            raise ValueError("Effective resources require verified revival rules")
        revival = build_event_pass_revival(base_resource(PASS_RESOURCE), base_resource(MISSION_RESOURCE), revival_rules)
        _replace(replacements, owners, "revival", revival.replacements)
        package_texts = build_package_text_overlay(base_resource(IMPLANT_RESOURCE), base_resource(SYSTEM_TEXT_RESOURCE),
                                                   event_pass_package_ids(base_resource(PASS_RESOURCE)))
        _replace(replacements, owners, "package_texts", package_texts.replacements)
    event_login = None
    if event_login_rules is not None:
        if not isinstance(event_login_rules, EventLoginRules):
            raise ValueError("Effective resources require verified event login rules")
        if event_login_rules.enabled and event_login_rules.revivals:
            event_login = build_event_login_revival(base_resource(EVENT_LOGIN_RESOURCE), event_login_rules)
            _replace(replacements, owners, "event_login", event_login.replacements)
    purchased_login = None
    if purchased_login_rules is not None:
        if not isinstance(purchased_login_rules, PurchasedLoginRules):
            raise ValueError("Effective resources require verified purchased login rules")
        if purchased_login_rules.enabled:
            def effective_resource(name):
                return replacements[name] if name in replacements else base_resource(name)
            purchased_login = build_purchased_login_texts(effective_resource(IMPLANT_RESOURCE),
                effective_resource(SYSTEM_TEXT_RESOURCE), EventLoginCatalog(effective_resource), purchased_login_rules)
            _replace(replacements, owners, "purchased_login", purchased_login.replacements)
    mission_window = None
    if mission_window_rules is not None:
        if not isinstance(mission_window_rules, MissionWindowRules):
            raise ValueError("Effective resources require verified mission window rules")
        mission_window = build_mission_window_revival(base_resource, mission_window_rules)
        _claim(owners, "Mission windows", mission_window.resources)
        _replace(replacements, owners, "mission_window", mission_window.replacements)
    command_texts = None
    if command_text_overrides is not None:
        if not isinstance(command_text_overrides, CommandTextOverrides):
            raise ValueError("Effective resources require verified command text overrides")
        command_texts = build_command_text_overlay(base_resource, command_text_overrides)
        _claim(owners, "Command text overrides", command_texts.targets)
        _replace(replacements, owners, "command_texts", command_texts.replacements)
    shop_window_closure = None
    if any(entry.name == SHOP_RESOURCE for entry in entries):
        shop_window_closure = build_shop_window_closure(base_resource(SHOP_RESOURCE))
        _claim(owners, "Shop window closures", shop_window_closure.targets)
        _replace(replacements, owners, "shop_window_closure", shop_window_closure.replacements)
    overlay = build_resource_overlay(manifest["version"], raw, replacements)
    index_hash = hashlib.sha256(overlay.index).hexdigest()
    effective = {**manifest, "base_version": manifest["version"], "base_snapshot": manifest["snapshot"],
                 "version": overlay.version, "snapshot": overlay.version + "-" + index_hash[:16],
                 "index_sha256": index_hash, "index_bytes": len(overlay.index),
                 "entries": len(overlay.entries), "total_bytes": sum(entry.size for entry in overlay.entries),
                 "local_replacements": sorted(overlay.replacements)}
    assets = IndexedAssetService(overlay.entries, resource_root, source_root, replacements=overlay.replacements)
    return ResourceView(MappingProxyType(effective), overlay.entries, overlay.index, overlay.replacements, assets,
                        base_assets, revival, revival_rules, package_texts, event_login, purchased_login, mission_window,
                        command_texts, shop_window_closure, raw)

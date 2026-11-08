from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from types import MappingProxyType

from .al_format import decompress_all4, parse_archive, parse_table
from .alxml import Column, Container, dumps
from .business_calendar import business_date
from .errors import ProtocolError
from .event_login_catalog import EventLoginCampaign, RESOURCE_NAME
from .native_numbers import INT32_MAX
from .package_catalog import BUTTON_MEMBER, BUTTON_NAME, LIMITS, MENU_RESOURCE, PSC_FIELDS, SCENE, SHOP_RESOURCE
from .package_text_overlay import IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE
from .purchased_login_rules import PurchasedLoginRules
from .purchased_login_state import purchased_login_identity
from .purchased_login_texts import build_purchased_login_texts, purchased_campaigns, purchased_package_id, purchased_package_keys
from .system_literals import command_id


@dataclass(frozen=True)
class PurchasedLoginOffer:
    campaign_id: int
    package_id: int
    flag_id: int
    sku: str
    campaign: EventLoginCampaign
    purchase_limit: int
    caption_id: int | None
    description_id: int | None
    definition_hash: str


class PurchasedLoginCatalog:
    def __init__(self, event_catalog, asset_reader, rules=None):
        self.event_catalog = event_catalog
        self.rules = PurchasedLoginRules() if rules is None else rules
        try:
            if not isinstance(self.rules, PurchasedLoginRules) or not callable(asset_reader):
                raise ValueError("Purchased login catalog requires verified rules and a reader")
            self._selected = frozenset(campaign.campaign_id for campaign in purchased_campaigns(event_catalog, self.rules))
            campaigns = tuple(campaign for campaign in event_catalog.campaigns if campaign.login_bonus_type == 2)
            if not campaigns:
                raise ValueError("Purchased login source has no Type2 campaigns")
            hashes, snapshots = {}, {}

            def resource(name):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Purchased login resources must be bounded immutable bytes")
                hashes[name] = hashlib.sha256(raw).hexdigest()
                snapshots[name] = raw
                return raw

            if hashlib.sha256(resource(RESOURCE_NAME)).hexdigest() != event_catalog.resource_sha256:
                raise ValueError("Purchased login source differs from the served resource")
            self._button_id, text_indices = None, {}
            if self.rules.enabled:
                texts = build_purchased_login_texts(resource(IMPLANT_RESOURCE), resource(SYSTEM_TEXT_RESOURCE), event_catalog, self.rules)
                if texts.replacements:
                    raise ValueError("Purchased login text must already be in the effective resource view")
                text_indices = texts.text_indices
                menu = resource(MENU_RESOURCE)
                rows = parse_table(decompress_all4(menu, LIMITS) if menu.startswith(b"ALL4") else menu, LIMITS)["rows"]
                key = command_id(BUTTON_NAME)
                matches = [row["_ResID"] for row in rows if row["_CfgID"] == key]
                if len(matches) != 1 or type(matches[0]) is not int or not 0 < matches[0] <= INT32_MAX:
                    raise ValueError("Purchased login package button is unavailable")
                self._button_id = matches[0]
                shop = resource(SHOP_RESOURCE)
                shop = decompress_all4(shop, LIMITS) if shop.startswith(b"ALL4") else shop
                members = [member for member in parse_archive(shop, LIMITS).members if member.name == BUTTON_MEMBER]
                if len(members) != 1 or shop[members[0].offset:members[0].offset + 4] != b"ALOD":
                    raise ValueError("Purchased login button layout is unavailable")
            offers = []
            for campaign in campaigns:
                if (type(campaign.expiration) is not int or not 0 < campaign.expiration <= INT32_MAX
                        or not campaign.rewards or any(row.pause_period or row.evolution_count for row in campaign.rewards)
                        or sorted({row.ordinal for row in campaign.rewards}) != list(range(1, campaign.max_ordinal + 1))):
                    raise ValueError("Purchased login rewards need a contiguous relative schedule")
                identifier = purchased_package_id(campaign.campaign_id)
                caption, description, _ = purchased_package_keys(campaign.campaign_id)
                offers.append(PurchasedLoginOffer(campaign.campaign_id, identifier, identifier,
                    f"local_login_{campaign.campaign_id}", campaign, self.rules.purchase_limit,
                    text_indices.get(caption), text_indices.get(description), purchased_login_identity(campaign.campaign_id)))
            self.offers = tuple(offers)
            self.by_sku = MappingProxyType({offer.sku: offer for offer in offers})
            self.by_campaign_id = MappingProxyType({offer.campaign_id: offer for offer in offers})
            self._resources = MappingProxyType(snapshots)
            self._hashes = MappingProxyType(hashes)
            self._snapshot = event_catalog, event_catalog.content_version, self.rules, self.offers, self.by_sku, self.by_campaign_id
            self.content_version = hashlib.sha256(json.dumps({"policy": "local_type2_package_v1",
                "source": event_catalog.content_version, "rules": self.rules.to_dict(), "resources": hashes,
                "psc_fields": PSC_FIELDS, "button": self._button_id, "item_type": 10,
                "identity": {offer.sku: offer.definition_hash for offer in offers}}, sort_keys=True).encode()).hexdigest()
        except (ValueError, TypeError, AttributeError, KeyError, OSError, ProtocolError) as error:
            raise ProtocolError("purchased_login_catalog_invalid", "Purchased login catalog resources are unavailable", 503) from error

    @property
    def enabled(self):
        return self.rules.enabled

    def _ready(self):
        source, version, rules, offers, by_sku, by_campaign = self._snapshot
        source._ready()
        if (self.event_catalog is not source or source.content_version != version or self.rules is not rules
                or self.offers is not offers or self.by_sku is not by_sku or self.by_campaign_id is not by_campaign):
            raise ProtocolError("purchased_login_catalog_changed", "Purchased login catalog changed after setup", 503)

    def get(self, sku):
        self._ready()
        if type(sku) is not str or sku not in self.by_sku:
            raise ProtocolError("purchased_login_package_unknown", "Purchased login SKU is absent from the local catalog", 404)
        return self.by_sku[sku]

    def available(self, now):
        self._ready()
        if not self.rules.enabled:
            return ()
        try:
            next_day = business_date(now) + timedelta(days=1)
        except (OverflowError, ProtocolError) as error:
            raise ProtocolError("purchased_login_window_invalid", "Purchased login window exceeds the calendar", 503) from error
        result = []
        for offer in self.offers:
            if offer.campaign_id not in self._selected:
                continue
            first = max(offer.campaign.start_date, next_day) if offer.campaign.start_date else next_day
            try:
                last = next_day + timedelta(days=offer.campaign.expiration)
            except OverflowError as error:
                raise ProtocolError("purchased_login_window_invalid", "Purchased login duration exceeds the calendar", 503) from error
            if offer.campaign.end_date is not None:
                last = min(last, offer.campaign.end_date)
            if first < last:
                result.append(offer)
        return tuple(result)

    def response(self, now):
        rows = []
        for offer in self.available(now):
            row = {name: "" if kind == "S" else 0 for name, kind in PSC_FIELDS}
            row.update(RecordID=1, PackageID=offer.package_id, PackageDisplayScene=SCENE, SkuID=offer.sku,
                Price=0, PackageRepeatCount=offer.purchase_limit, PackageCaption=offer.caption_id,
                PackageDescription=offer.description_id, BtnPackObjectID=self._button_id,
                PackageFlag=offer.flag_id, OpenDays="null", CloseDays="null", DisplayIndex=offer.package_id,
                PackageItemType=10, PackageItemNum=1, LoginBonusID=offer.campaign_id)
            rows.append(row)
        result = Container("PSC", tuple(Column(name, kind, tuple(row[name] for row in rows)) for name, kind in PSC_FIELDS))
        dumps(Container(children=(result,)))
        return result

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "enabled": self.enabled,
                "offer_count": len(self.offers), "rules": self.rules.to_dict(), "content_version": self.content_version,
                "item_type": 10, "resource_hashes": dict(self._hashes),
                "availability": "Explicit local selection; first claim day must precede a finite source end",
                "native_tested": False}

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import MappingProxyType
import zlib

from .errors import ProtocolError
from .event_login_catalog import EventLoginCatalog
from .native_numbers import INT32_MAX
from .package_text_overlay import IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE, _table
from .purchased_login_rules import PurchasedLoginRules
from .table_row_append import append_table_rows


PACKAGE_ID_BASE = 2_000_100_000
CONFIRMATION = "無料でこのログインボーナスの受取資格を取得します。\n次の午前4時以降、活動開始日を満たしてから報酬を受け取れます。"


def purchased_package_id(campaign_id):
    if type(campaign_id) is not int or not 0 < campaign_id <= INT32_MAX - PACKAGE_ID_BASE:
        raise ValueError("Purchased login campaign exceeds the local package identity range")
    return PACKAGE_ID_BASE + campaign_id


def purchased_package_keys(campaign_id):
    identifier = purchased_package_id(campaign_id)
    return (f"PackageName_{identifier}", f"PackageInfo_{identifier}", f"dmmPackagePurchaseIntention_{identifier}")


def purchased_campaigns(catalog, rules):
    if not isinstance(catalog, EventLoginCatalog) or not isinstance(rules, PurchasedLoginRules):
        raise ValueError("Purchased login text requires verified campaign rules")
    catalog._ready()
    campaigns = tuple(campaign for campaign in catalog.campaigns if campaign.login_bonus_type == 2)
    indexed = {campaign.campaign_id: campaign for campaign in campaigns}
    if rules.campaign_ids is not None and any(identifier not in indexed for identifier in rules.campaign_ids):
        raise ProtocolError("purchased_login_campaign_invalid", "Only existing Type2 campaigns may be selected", 503)
    return tuple(campaign for campaign in campaigns
                 if rules.campaign_ids is None or campaign.campaign_id in rules.campaign_ids)


def _description(campaign, rules):
    return ("このローカルサーバー専用の無料パックです。\n"
        f"次の午前4時以降、活動開始日を満たしてから、1日1回、最大{campaign.max_ordinal}回の報酬を受け取れます。\n"
        f"受取期間は最大{campaign.expiration}日間です。取得上限は{rules.purchase_limit}回です。")


@dataclass(frozen=True)
class PurchasedLoginTexts:
    replacements: MappingProxyType
    text_indices: MappingProxyType
    campaign_ids: tuple[int, ...]
    source_hashes: MappingProxyType
    effective_hashes: MappingProxyType
    rules: PurchasedLoginRules

    @property
    def metadata(self):
        return {"schema_version": 1, "rules": self.rules.to_dict(), "rules_hash": self.rules.content_version,
            "campaign_ids": list(self.campaign_ids), "source_hashes": dict(self.source_hashes),
            "effective_hashes": dict(self.effective_hashes), "text_indices": dict(self.text_indices)}


def build_purchased_login_texts(implant_bytes, system_bytes, event_catalog, rules):
    if not isinstance(rules, PurchasedLoginRules):
        raise ValueError("Purchased login text requires verified rules")
    selected = purchased_campaigns(event_catalog, rules) if rules.enabled else ()
    implant = _table(implant_bytes, "itcg", (("_ID", 32), ("_TextID", 1)))
    system = _table(system_bytes, "sytx", (("Recode_Index", 1), ("Data_Text", 32),
        ("GroupID", 1), ("GroupType", 1), ("GroupRecordID", 1)))
    by_name, by_hash = {}, {}
    for row in implant["rows"]:
        name, index = row["_ID"], row["_TextID"]
        if not name or type(index) is not int or not 0 <= index < system["record_count"]:
            raise ValueError("Purchased login text points outside SystemText")
        hashed = zlib.crc32(name.encode("utf-8"))
        if name in by_name or hashed in by_hash:
            raise ValueError("Purchased login text keys collide")
        by_name[name], by_hash[hashed] = index, name
    additions, keys, indices = [], [], {}
    for campaign in selected:
        caption = campaign.header["Caption"]
        if not 0 <= caption < system["record_count"]:
            raise ValueError("Purchased login source caption is invalid")
        texts = ("ローカル " + system["rows"][caption]["Data_Text"], _description(campaign, rules), CONFIRMATION)
        names = purchased_package_keys(campaign.campaign_id)
        present = [name in by_name for name in names]
        if any(present) and not all(present):
            raise ValueError("Purchased login text keys are incomplete")
        for name, text in zip(names, texts):
            collision = by_hash.get(zlib.crc32(name.encode("utf-8")))
            if collision is not None and collision != name:
                raise ValueError("Purchased login text key hash collides")
            if name in by_name:
                index = by_name[name]
                if system["rows"][index] != {"Recode_Index": index, "Data_Text": text,
                        "GroupID": 0, "GroupType": 0, "GroupRecordID": 0}:
                    raise ValueError("Purchased login text conflicts with an existing row")
            else:
                index = system["record_count"] + len(additions)
                if any(row["Recode_Index"] == index for row in system["rows"]):
                    raise ValueError("Purchased login text record identifier collides")
                additions.append({"Recode_Index": index, "Data_Text": text, "GroupID": 0,
                                  "GroupType": 0, "GroupRecordID": 0})
                keys.append({"_ID": name, "_TextID": index})
                by_hash[zlib.crc32(name.encode("utf-8"))] = name
            indices[name] = index
    original = {IMPLANT_RESOURCE: implant_bytes, SYSTEM_TEXT_RESOURCE: system_bytes}
    effective = {IMPLANT_RESOURCE: append_table_rows(implant_bytes, keys),
                 SYSTEM_TEXT_RESOURCE: append_table_rows(system_bytes, additions)}
    return PurchasedLoginTexts(MappingProxyType({name: raw for name, raw in effective.items() if raw != original[name]}),
        MappingProxyType(indices), tuple(campaign.campaign_id for campaign in selected),
        MappingProxyType({name: hashlib.sha256(raw).hexdigest() for name, raw in original.items()}),
        MappingProxyType({name: hashlib.sha256(raw).hexdigest() for name, raw in effective.items()}), rules)

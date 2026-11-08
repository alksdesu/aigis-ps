from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import MappingProxyType
import zlib

from .al_format import ALFormatError, decompress_all4, parse_table
from .native_numbers import INT32_MAX
from .table_row_append import LIMITS, MAX_ROWS, append_table_rows


IMPLANT_RESOURCE = "cfgImplantText.atb"
SYSTEM_TEXT_RESOURCE = "SystemText.atb"
LOCAL_PACKAGE_ID_BASE = 2_000_000_000
CAPTION_KEY = "LocalEventPassPackageName"
DESCRIPTION_KEY = "LocalEventPassPackageInfo"
CAPTION_TEXT = "ローカル復刻プレミアムパス"
DESCRIPTION_TEXT = "このローカルサーバー用の無料パックです。プレミアム報酬の受取資格を1回だけ取得できます。"
CONFIRMATION_TEXT = "無料でプレミアム報酬の受取資格を取得します。"
MAX_PACKAGES = 256


def _invalid(message):
    raise ALFormatError(message, code="package_text_overlay_invalid")


def local_package_id(first_reward_id: int) -> int:
    if type(first_reward_id) is not int or not 0 < first_reward_id <= INT32_MAX - LOCAL_PACKAGE_ID_BASE:
        _invalid("Local package identity requires a bounded positive first reward ID")
    return LOCAL_PACKAGE_ID_BASE + first_reward_id


def confirmation_key(package_id: int) -> str:
    if type(package_id) is not int or not LOCAL_PACKAGE_ID_BASE < package_id <= INT32_MAX:
        _invalid("Local package identity is outside its reserved range")
    return f"dmmPackagePurchaseIntention_{package_id}"


def caption_key(package_id: int) -> str:
    confirmation_key(package_id)
    return f"PackageName_{package_id}"


def description_key(package_id: int) -> str:
    confirmation_key(package_id)
    return f"PackageInfo_{package_id}"


def event_pass_package_ids(payload: bytes) -> tuple[int, ...]:
    if type(payload) is not bytes or not 0 < len(payload) <= LIMITS.max_input_bytes:
        _invalid("Package pass identities require bounded resource bytes")
    data = decompress_all4(payload, LIMITS) if payload.startswith(b"ALL4") else payload
    table = parse_table(data, LIMITS)
    fields = {field["name"]: field["type_code"] for field in table["fields"]}
    if fields.get("ID") != 1 or fields.get("NormalPassItemID") != 32:
        _invalid("Package pass identity fields differ from the native schema")
    names, identifiers = set(), []
    for row in table["rows"]:
        if row["NormalPassItemID"]:
            if row["NormalPassItemID"] in names:
                _invalid("Package pass identities must be unique")
            names.add(row["NormalPassItemID"])
            identifiers.append(local_package_id(row["ID"]))
    if not identifiers or len(identifiers) > MAX_PACKAGES or len(set(identifiers)) != len(identifiers):
        _invalid("Package pass identities must form a bounded unique sequence")
    return tuple(identifiers)


def _table(raw, tag, fields):
    if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
        _invalid("Package text resources require bounded immutable bytes")
    data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
    table = parse_table(data, LIMITS)
    actual = tuple((field["name"], field["type_code"], field["offset"], field["size"])
                   for field in table["fields"])
    expected = tuple((name, kind, index * 4, 4) for index, (name, kind) in enumerate(fields))
    if table["tag"] != tag or table["flags"] != 0x14 or actual != expected or table["record_size"] != 4 * len(fields):
        _invalid("Package text resource schema differs from the native table")
    return table


@dataclass(frozen=True)
class PackageTextOverlay:
    replacements: MappingProxyType
    text_indices: MappingProxyType
    package_ids: tuple[int, ...]
    source_hashes: MappingProxyType
    effective_hashes: MappingProxyType

    @property
    def metadata(self):
        return {"schema_version": 1, "policy": "native_physical_text_indices_v1",
                "package_ids": list(self.package_ids), "text_indices": dict(self.text_indices),
                "source_hashes": dict(self.source_hashes), "effective_hashes": dict(self.effective_hashes)}


def build_package_text_overlay(implant_bytes: bytes, system_bytes: bytes, package_ids) -> PackageTextOverlay:
    if not isinstance(package_ids, (tuple, list)) or not 0 < len(package_ids) <= MAX_PACKAGES:
        _invalid("Local package text requires a nonempty bounded identity sequence")
    for identifier in package_ids:
        confirmation_key(identifier)
    if len(set(package_ids)) != len(package_ids):
        _invalid("Local package identities must be unique")
    package_ids = tuple(sorted(package_ids))
    keys = [CAPTION_KEY, DESCRIPTION_KEY] + [key(identifier) for identifier in package_ids
                                            for key in (caption_key, description_key, confirmation_key)]
    if len({zlib.crc32(key.encode("utf-8")) for key in keys}) != len(keys):
        _invalid("Local package text keys have colliding native hashes")
    implant = _table(implant_bytes, "itcg", (("_ID", 32), ("_TextID", 1)))
    system = _table(system_bytes, "sytx", (("Recode_Index", 1), ("Data_Text", 32),
        ("GroupID", 1), ("GroupType", 1), ("GroupRecordID", 1)))
    by_name, by_hash = {}, {}
    for row in implant["rows"]:
        name, text_id = row["_ID"], row["_TextID"]
        if not name or type(text_id) is not int or not 0 <= text_id < system["record_count"]:
            _invalid("Text keys must refer to existing physical SystemText rows")
        digest = zlib.crc32(name.encode("utf-8"))
        if name in by_name or digest in by_hash:
            _invalid("Text key names and native hashes must be unique")
        by_name[name], by_hash[digest] = text_id, name
    for key in keys:
        if (old := by_hash.get(zlib.crc32(key.encode("utf-8")))) is not None and old != key:
            _invalid("Local text key collides with an existing native hash")
    present = [key in by_name for key in keys[:2]]
    if any(present) and not all(present):
        _invalid("Local package text keys are incomplete")
    texts = (CAPTION_TEXT, DESCRIPTION_TEXT, CONFIRMATION_TEXT)
    if all(present):
        base = by_name[CAPTION_KEY]
        indices = (base, by_name[DESCRIPTION_KEY], base + 2)
        if indices != (base, base + 1, base + 2) or indices[-1] >= system["record_count"]:
            _invalid("Local text indices no longer match their appended physical rows")
        for index, text in zip(indices, texts):
            if system["rows"][index] != {"Recode_Index": index, "Data_Text": text,
                    "GroupID": 0, "GroupType": 0, "GroupRecordID": 0}:
                _invalid("Local package text conflicts with the current physical row")
        added_system = []
    else:
        base = system["record_count"]
        indices = (base, base + 1, base + 2)
        if base + 3 > MAX_ROWS or any(row["Recode_Index"] in indices for row in system["rows"]):
            _invalid("Local text rows exceed capacity or collide with existing record identifiers")
        added_system = [{"Recode_Index": index, "Data_Text": text, "GroupID": 0,
                         "GroupType": 0, "GroupRecordID": 0} for index, text in zip(indices, texts)]
    wanted = {CAPTION_KEY: indices[0], DESCRIPTION_KEY: indices[1]}
    for identifier in package_ids:
        wanted.update({caption_key(identifier): indices[0], description_key(identifier): indices[1],
                       confirmation_key(identifier): indices[2]})
    added_implant = []
    for key, index in wanted.items():
        if key in by_name:
            if by_name[key] != index:
                _invalid("Local text key is already bound to a different physical row")
        else:
            added_implant.append({"_ID": key, "_TextID": index})
    payloads = {IMPLANT_RESOURCE: implant_bytes, SYSTEM_TEXT_RESOURCE: system_bytes}
    effective = {IMPLANT_RESOURCE: append_table_rows(implant_bytes, added_implant),
                 SYSTEM_TEXT_RESOURCE: append_table_rows(system_bytes, added_system)}
    return PackageTextOverlay(MappingProxyType({name: value for name, value in effective.items()
                                                if value != payloads[name]}),
        MappingProxyType(wanted), package_ids,
        MappingProxyType({name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}),
        MappingProxyType({name: hashlib.sha256(value).hexdigest() for name, value in effective.items()}))

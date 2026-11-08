from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_archive, parse_table
from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .native_numbers import INT32_MIN, int32
from .possessions import STORED_ITEM_NAME_BYTES
from .system_literals import command_id


RECOVERY_LIMIT = 2000
HOME_EVENT_RESOURCE = "EventHomeArc.aar"
HOME_EVENT_PREFIX = "_"
HOME_EVENT_SUFFIX = ".aar"
RESOURCE_LIMIT = 64 * 1024 * 1024
LIMITS = ParseLimits(max_input_bytes=RESOURCE_LIMIT, max_decompressed_bytes=RESOURCE_LIMIT,
                     max_rows=65535, max_fields=64, max_members=4096)
ITEM_FIELDS = {"ItemID": 32, "ID": 1, "EffectType": 1, "Stack": 1, "ReceiveAction": 1,
               "InvisibleContaint": 1, "CanAllGet": 1, "ReceiveConfirmation": 1,
               "EventID": 32, "TextIDExplanation": 1}
CONFIRMATION_NAME = "SystemText_HomeCookingUseComfirmation"
EXPLANATION_NAME = "ItemInfoCharismaRecover"
CONFIRMATION_TEXT = ("[ITEM_NAME]を食べますか？\n食べるとカリスマが最大値分回復\nします。\n\n"
                     "※超過する場合は上限を超えて\n回復します。\n※[ITEM_NAME]は食べると\nなくなります。")
EXPLANATION_TEXT = "カリスマを最大値分回復します。\n超過する場合は上限を超えて回復します。"
EVENT_COMMANDS = frozenset(("FadeOut", "SetComeBackMenu", "PlayBgm", "LoadPrivateRoomResource",
                            "WaitLoadPrivateRoomResource", "WaitFade", "CreatePrivateRoomObject",
                            "CreateChara", "FadeIn", "ShowText", "WaitFrame", "CreateSprite",
                            "DoPeruseSavePresent"))
EVENT_FIELDS = {"_SceneID": 1, "_CommandName": 32, "_ObjectName": 32, "_AfterExpression": 32,
                **{f"_Param{index}": 32 for index in range(1, 7)}, "_CmndParam": 32}


@dataclass(frozen=True)
class PresentRecoveryDefinition:
    item_id: str | None
    event_id: str = ""
    can_all_get: bool = False


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _name(value, *, empty=False, maximum=STORED_ITEM_NAME_BYTES):
    if not isinstance(value, str) or (not value and not empty) or len(value.encode("utf-8")) > maximum:
        raise ValueError("Expected a bounded resource name")
    dumps(Container(children=(Column("ID", "S", (value,)),)))
    return value


def _home_event_package(event_id):
    # The client opens "_" + EventID and appends ".aar" only when the EventID has no dot.
    package = HOME_EVENT_PREFIX + event_id
    return package if "." in event_id else package + HOME_EVENT_SUFFIX


def _decoded(payload):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= RESOURCE_LIMIT:
        raise ValueError("Recovery resources must be bounded bytes")
    return decompress_all4(payload, LIMITS) if payload.startswith(b"ALL4") else payload


def _table(payload, schema):
    parsed = parse_table(_decoded(payload), LIMITS)
    fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
    if len(fields) != len(parsed["fields"]) or any(fields.get(name) != kind for name, kind in schema.items()):
        raise ValueError("Recovery resource fields differ from the native contract")
    return parsed["rows"]


def _archive(payload):
    data = _decoded(payload)
    members = parse_archive(data, LIMITS).members
    if sum(member.size for member in members) > RESOURCE_LIMIT:
        raise ValueError("Recovery archive members exceed the total byte limit")
    result = {}
    for member in members:
        if not isinstance(member.name, str) or member.name in result:
            raise ValueError("Recovery event archive names are missing or duplicated")
        result[member.name] = data[member.offset:member.offset + member.size]
    return result


def _event(payload):
    data = _decoded(payload)
    members = _archive(data)
    rows = _table(members["_evseq.atb"], EVENT_FIELDS)
    if not rows:
        raise ValueError("Recovery events require a nonempty command sequence")
    saves = 0
    for index, row in enumerate(rows):
        if not _integer(row["_SceneID"]):
            raise ValueError("Recovery event scene IDs must be native integers")
        if any(not isinstance(row[field], str) for field in EVENT_FIELDS if field != "_SceneID"):
            raise ValueError("Recovery event command fields must be strings")
        if row["_CommandName"] not in EVENT_COMMANDS or row["_AfterExpression"] not in ("", "DoAttachImageOfs();"):
            raise ValueError("Recovery event contains an unsupported command")
        if row["_CommandName"] == "DoPeruseSavePresent":
            saves += 1
            if (index != len(rows) - 1 or row["_SceneID"] != 0
                    or any(row[field] != "" for field in EVENT_FIELDS if field not in ("_SceneID", "_CommandName"))):
                raise ValueError("Recovery events must end with an unmodified present claim")
    if saves != 1:
        raise ValueError("Recovery events must claim exactly one present")
    return len(data)


class PresentRecoveryCatalog:
    def __init__(self, asset_reader):
        try:
            if not callable(asset_reader):
                raise ValueError("Recovery catalog requires a resource reader")
            hashes = {}

            def read(name):
                payload = asset_reader(name)
                data = _decoded(payload)
                hashes[name] = hashlib.sha256(data).hexdigest()
                return data

            source = _table(read("ItemConfigTable.atb"), ITEM_FIELDS)
            if not source:
                raise ValueError("Recovery item definitions cannot be empty")
            rows, identifiers = {}, set()
            for item in source:
                row = {name: item[name] for name in ITEM_FIELDS}
                name = _name(row["ItemID"])
                identifier = row["ID"]
                if (not _integer(identifier, INT32_MIN) or identifier != command_id(name)
                        or name in rows or identifier in identifiers):
                    raise ValueError("Recovery item identities are invalid or duplicated")
                for field in ("EffectType", "Stack", "ReceiveAction", "InvisibleContaint", "CanAllGet"):
                    if not _integer(row[field]):
                        raise ValueError("Recovery item flags must be nonnegative native integers")
                if any(not _integer(row[field], INT32_MIN) for field in ("ReceiveConfirmation", "TextIDExplanation")):
                    raise ValueError("Recovery text references must be native integers")
                _name(row["EventID"], empty=True, maximum=512)
                identifiers.add(identifier)
                rows[name] = row
            text_names, text_hashes = {}, set()
            for row in _table(read("cfgImplantText.atb"), {"_ID": 32, "_TextID": 1}):
                name = _name(row["_ID"])
                identifier = command_id(name)
                if name in text_names or identifier in text_hashes or not _integer(row["_TextID"]):
                    raise ValueError("Recovery text mappings are invalid or duplicated")
                text_names[name] = row["_TextID"]
                text_hashes.add(identifier)
            texts = {}
            for row in _table(read("SystemText.atb"), {"Recode_Index": 1, "Data_Text": 32}):
                identifier = row["Recode_Index"]
                if not _integer(identifier) or not isinstance(row["Data_Text"], str):
                    raise ValueError("Recovery texts are invalid")
                texts.setdefault(identifier, []).append(row["Data_Text"])
            for name, expected in ((CONFIRMATION_NAME, CONFIRMATION_TEXT), (EXPLANATION_NAME, EXPLANATION_TEXT)):
                if texts.get(text_names.get(name)) != [expected]:
                    raise ValueError("Recovery amount text no longer matches the verified rule")
            self._rows = MappingProxyType({name: MappingProxyType(row) for name, row in rows.items()})
            items = {name: PresentRecoveryDefinition(name, row["EventID"]) for name, row in rows.items()
                     if row["EffectType"] == 11 and row["Stack"] == 0 and row["ReceiveAction"] == 0
                     and row["InvisibleContaint"] == 1 and row["CanAllGet"] == 0
                     and row["ReceiveConfirmation"] == command_id(CONFIRMATION_NAME)
                     and row["TextIDExplanation"] in (0, command_id(EXPLANATION_NAME))}
            event_names = frozenset(item.event_id for item in items.values() if item.event_id)
            events = {}
            if event_names:
                packages = _archive(read(HOME_EVENT_RESOURCE))
                total_event_bytes = 0
                for name in sorted(event_names):
                    payload = packages[_home_event_package(name)]
                    total_event_bytes += _event(payload)
                    if total_event_bytes > RESOURCE_LIMIT:
                        raise ValueError("Recovery events exceed the total decoded byte limit")
                    events[name] = hashlib.sha256(payload).hexdigest()
            self._items = MappingProxyType(items)
            self._events = MappingProxyType(events)
            self._resource_hashes = MappingProxyType(hashes)
            content = {"schema": 1, "resources": hashes, "events": events,
                       "quantity": "one_current_maximum_per_mail", "limit": RECOVERY_LIMIT,
                       "can_all_get_zero": "single_only", "type8_content": 0,
                       "type22_content": "positive_i32_metadata", "event_commands": sorted(EVENT_COMMANDS),
                       "pev": "unchanged"}
            self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("present_recovery_catalog_invalid", "Recovery resources differ from the verified contract", 503) from error

    def definition(self, kind: int, content: int, item_id: str | None = None) -> PresentRecoveryDefinition:
        if type(kind) is not int or kind not in (8, 22):
            raise ProtocolError("present_recovery_unsupported", "This present type is not a supported recovery", 501)
        if kind == 8:
            if not _integer(content):
                raise ProtocolError("present_recovery_content_invalid", "Recovery content must be a native integer")
            if content != 0 or item_id is not None:
                raise ProtocolError("present_recovery_unsupported", "Decorated recovery presents are not supported", 501)
            return PresentRecoveryDefinition(None)
        if not _integer(content, 1):
            raise ProtocolError("present_recovery_content_invalid", "Recovery item content must be positive native metadata")
        if not isinstance(item_id, str) or item_id not in self._items:
            raise ProtocolError("present_recovery_unsupported", "This item requires an unsupported recovery action", 501)
        return self._items[item_id]

    def supports_item(self, item_id: str) -> bool:
        return isinstance(item_id, str) and item_id in self._items

    def is_ready(self) -> bool:
        # Construction refuses unverified resources, so every built catalog is ready.
        return True

    def status(self) -> dict:
        event_count = sum(bool(item.event_id) for item in self._items.values())
        return {"ready": self.is_ready(), "content_version": self.content_version,
                "resource_hashes": dict(self._resource_hashes), "supported_types": [8, 22],
                "item_count": len(self._items), "event_item_count": event_count,
                "no_event_item_count": len(self._items) - event_count, "event_count": len(self._events),
                "recognized_recovery_count": sum(row["EffectType"] == 11 for row in self._rows.values()),
                "can_all_get": False, "recovery_limit": RECOVERY_LIMIT,
                "quantity": "one_current_maximum_per_mail", "type8_content": 0,
                "type22_content": "positive_i32_metadata", "event_writeback": "present_claim_only",
                "pev": "unchanged"}

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING
import zlib

from .al_format import ParseLimits, decompress_all4, parse_archive, parse_table
from .alxml import Column, Container, dumps
from .assets import resolve_asset
from .errors import ProtocolError
from .local_io import read_bounded
from .native_numbers import int32
from .protocol import NATIVE_CELL_BYTES, ProtocolRegistry, RequestEnvelope, WireProfile, encode_response
from .request_fields import invalid, registered, scalar
from .storage import Store

if TYPE_CHECKING:
    from .bootstrap import BootstrapService


log = logging.getLogger("aigis_ps")

MAX_PERUSED_EVENTS = 65535
MAX_EVENT_ID_BYTES = NATIVE_CELL_BYTES
# History saved under the earlier 512-byte bound keeps loading and updating; only new IDs must fit the native cell.
STORED_EVENT_ID_BYTES = 512
RESOURCE_LIMIT = 64 * 1024 * 1024
PACKAGE_LIMIT = 4 * 1024 * 1024
LIMITS = ParseLimits(max_input_bytes=RESOURCE_LIMIT, max_decompressed_bytes=RESOURCE_LIMIT,
                     max_rows=65535, max_fields=64, max_members=4096)
PACKAGE_LIMITS = ParseLimits(max_input_bytes=PACKAGE_LIMIT, max_decompressed_bytes=PACKAGE_LIMIT,
                             max_rows=65535, max_fields=64, max_members=4096)
PACKAGE_ERRORS = (AttributeError, KeyError, OverflowError, TypeError, ValueError)
HOME_EVENT_RESOURCE = "EventHomeArc.aar"
HOME_EVENT_PREFIX = "_"
HOME_EVENT_SUFFIX = ".aar"
QUEST_EVENT_RESOURCE = "EventArc.aar"
QUEST_EVENT_PACKAGE = "_ev%06d.aar"
QUEST_EVENT_NAME = re.compile(r"_ev(-?[0-9]+)\.aar")
EVENT_SEQUENCE = "_evseq.atb"
EVENT_TEXT = "_evtxt.atb"
SEQUENCE_FIELDS = {"_CommandName": "string_ref", "_CmndParam": "string_ref"}
# DoPeruseEvent reads parameter slot 4, which the script loader fills from _Param1 rather than _CmndParam.
PERUSE_EVENT_FIELDS = {"_CommandName": "string_ref", "_Param1": "string_ref"}
SAVE_COMMAND = "DoPeruseSave"
PERUSE_EVENT_COMMAND = "DoPeruseEvent"
SAVE_ARGUMENT = "Save"
PARAMETER_SEPARATOR = ","
VALUE_SEPARATOR = "="
VALUE_SPLIT_LIMIT = 1
IGNORED_PARAMETER_CHARACTERS = (" ", "\r", "\n")
NUMERIC_VALUE = re.compile(r"[+-][0-9.]*|[0-9.]+")
# The script loader keeps a positional parameter made only of digits and signs as atol of its text.
NUMERIC_PARAMETER = re.compile(r"[0-9+-]*")
LEADING_INTEGER = re.compile(r"[+-]?[0-9]*")
NUMERIC_HYPHEN = "\u2010"
HASH_COLLISION = "Saved IDs collide at the native hash"


def event_hash(event_id: str) -> int:
    return zlib.crc32(event_id.encode("utf-8"))


def validate_event_id(event_id, limit: int) -> None:
    if not isinstance(event_id, str) or not event_id or len(event_id.encode("utf-8")) > limit:
        raise ValueError("Event IDs must be nonempty bounded strings")
    dumps(Container(children=(Column("ID", "S", (event_id,)),)))


def _validate_flag(flag):
    if not int32(flag):
        raise ValueError("Perused event flags must be signed int32 values")


def read_perused(state: dict) -> list[dict]:
    try:
        if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
            raise ValueError("Missing bootstrap state")
        tables = state.get("tables")
        rows = tables.get("perused_events") if isinstance(tables, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_PERUSED_EVENTS:
            raise ValueError("Missing or oversized event collection")
        if "perused_events_schema" in state:
            version = state["perused_events_schema"]
            if type(version) is not int or version != 1:
                raise ValueError("Unsupported perused event schema")
        elif rows:
            raise ValueError("Nonempty event history requires a known schema")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"ID", "FLG"}:
                raise ValueError("Perused event rows require ID and FLG")
            validate_event_id(row["ID"], STORED_EVENT_ID_BYTES)
            _validate_flag(row["FLG"])
            identifier = event_hash(row["ID"])
            if identifier in seen:
                raise ValueError("Event IDs are duplicated or collide at the native hash")
            seen.add(identifier)
        return deepcopy(rows)
    except (ValueError, TypeError) as error:
        raise ProtocolError("peruse_state_invalid", "Persisted event history is missing, invalid or unsupported", 503) from error


def peruse_response(state: dict, event_ids=None) -> Container:
    rows = read_perused(state)
    if event_ids is not None:
        try:
            if not isinstance(event_ids, (list, tuple)) or len(event_ids) > MAX_PERUSED_EVENTS:
                raise ValueError("Event selection must be a bounded list")
            hashes = set()
            for event_id in event_ids:
                validate_event_id(event_id, STORED_EVENT_ID_BYTES)
                identifier = event_hash(event_id)
                if identifier in hashes:
                    raise ValueError("Event selection contains duplicate or colliding IDs")
                hashes.add(identifier)
        except (ValueError, TypeError) as error:
            raise ProtocolError("peruse_selection_invalid", "Event selection must contain distinct valid IDs") from error
        indexed = {row["ID"]: row for row in rows}
        if set(event_ids) - indexed.keys():
            raise ProtocolError("peruse_event_not_found", "A selected event has no stored history", 409)
        rows = [indexed[event_id] for event_id in event_ids]
    return Container("PEV", (Column("ID", "S", tuple(row["ID"] for row in rows)),
                             Column("FLG", "I", tuple(row["FLG"] for row in rows))))


def set_perused_flags(state: dict, updates: dict[str, int]) -> dict:
    existing = read_perused(state)
    stored = {row["ID"] for row in existing}
    try:
        if not isinstance(updates, dict) or len(updates) > MAX_PERUSED_EVENTS:
            raise ValueError("Event updates must be a bounded mapping")
        for event_id, flag in updates.items():
            validate_event_id(event_id, STORED_EVENT_ID_BYTES if event_id in stored else MAX_EVENT_ID_BYTES)
            _validate_flag(flag)
    except (ValueError, TypeError) as error:
        raise ProtocolError("peruse_updates_invalid", "Event updates must contain valid names and signed int32 flags") from error
    merged = {event_hash(row["ID"]): row for row in existing}
    for event_id, flag in updates.items():
        identifier = event_hash(event_id)
        old = merged.get(identifier)
        if old is not None and old["ID"] != event_id:
            raise ProtocolError("peruse_name_conflict", "Event names collide at the same native hash", 409)
        merged[identifier] = {"ID": event_id, "FLG": flag}
    if len(merged) > MAX_PERUSED_EVENTS:
        raise ProtocolError("peruse_capacity", "Event history exceeds the local row limit", 409)
    updated = deepcopy(state)
    if not updates:
        return updated
    updated["perused_events_schema"] = 1
    updated["tables"]["perused_events"] = list(merged.values())
    return updated


def asset_root_reader(asset_root: Path) -> Callable[[str], bytes]:
    root = Path(asset_root)
    return lambda name: read_bounded(resolve_asset(root, name), RESOURCE_LIMIT, root=root)


def exclusion_reason(error: BaseException) -> str:
    message = str(error) if isinstance(error, ValueError) else ""
    return message or type(error).__name__


def colliding_sources(saved: dict[str, frozenset[str]]) -> frozenset[str]:
    names_by_hash: dict[int, set[str]] = {}
    for names in saved.values():
        for name in names:
            names_by_hash.setdefault(event_hash(name), set()).add(name)
    colliding = {name for names in names_by_hash.values() if len(names) > 1 for name in names}
    return frozenset(source for source, names in saved.items() if names & colliding)


def decoded_resource(payload: bytes, limits: ParseLimits = LIMITS) -> bytes:
    if not isinstance(payload, bytes) or not 0 < len(payload) <= limits.max_input_bytes:
        raise ValueError("Home event resources must be bounded bytes")
    return decompress_all4(payload, limits) if payload.startswith(b"ALL4") else payload


def _members(data: bytes, limits: ParseLimits = LIMITS) -> dict[str, bytes]:
    members = {}
    for member in parse_archive(data, limits).members:
        if not isinstance(member.name, str):
            raise ValueError("Home event archive names are missing")
        # The client looks member names up from the end of the index, so the last duplicate wins.
        members[member.name] = data[member.offset:member.offset + member.size]
    return members


def home_event_member(event_id: str) -> str:
    # The client opens "_" + EventID and appends ".aar" only when the EventID has no dot.
    return HOME_EVENT_PREFIX + event_id + ("" if "." in event_id else HOME_EVENT_SUFFIX)


def _home_event_id(member_name: str) -> str | None:
    event_id = member_name.removeprefix(HOME_EVENT_PREFIX)
    if event_id == member_name or "." not in event_id:
        return None
    stem = event_id.removesuffix(HOME_EVENT_SUFFIX)
    return stem if stem != event_id and "." not in stem else event_id


def _quest_event_id(member_name: str) -> str | None:
    match = QUEST_EVENT_NAME.fullmatch(member_name)
    if match is None:
        return None
    number = int(match[1])
    # The client opens an EventArc package only under the name it formats from an int32 event number.
    return match[1] if int32(number) and QUEST_EVENT_PACKAGE % number == member_name else None


def _split(text: str, separator: str, limit: int | None = None) -> list[str]:
    # The client splitter stops at a separator that leads the remaining text.
    parts, splits = [], 0
    while text:
        index = text.find(separator)
        parts.append(text[:index] if index >= 0 else text)
        if index <= 0:
            break
        text = text[index + len(separator):]
        if not text:
            parts.append("")
        splits += 1
        if limit is not None and splits >= limit:
            parts.append(text)
            break
    return parts


def command_arguments(parameters: str) -> dict[str, str]:
    for character in IGNORED_PARAMETER_CHARACTERS:
        parameters = parameters.replace(character, "")
    arguments, name, value = {}, "", ""
    for token in _split(parameters, PARAMETER_SEPARATOR):
        pair = _split(token, VALUE_SEPARATOR, VALUE_SPLIT_LIMIT)
        if len(pair) > 1:
            if name and value:
                arguments[name] = value
            name, value = pair[0], pair[1]
        elif pair:
            # A token without "=" continues the previous value as a comma list.
            value = PARAMETER_SEPARATOR.join((value, pair[0])) if value else pair[0]
    if name and value:
        arguments[name] = value
    return arguments


def client_string(value: str) -> bool:
    # Client scripts store numeric-looking values, and values led by U+2010, as numbers.
    return not value.startswith(NUMERIC_HYPHEN) and NUMERIC_VALUE.fullmatch(value) is None


@dataclass(frozen=True)
class EventPackage:
    member: str
    event_id: str
    commands: tuple[tuple[str, str], ...]

    def parameters(self, command: str) -> tuple[str, ...]:
        return tuple(parameters for name, parameters in self.commands if name == command)


class EventArchive:
    resource: str
    label: str
    # The command name column, then the argument column its handlers read.
    fields: Mapping[str, str]
    invalid_code: str
    event_id_of: Callable[[str], str | None]

    def __init__(self, asset_reader: Callable[[str], bytes]):
        try:
            if not callable(asset_reader):
                raise ValueError(f"The {self.label.lower()} catalog requires a resource reader")
            resource = decoded_resource(asset_reader(self.resource))
            members = _members(resource)
        except (OSError, *PACKAGE_ERRORS) as error:
            raise ProtocolError(self.invalid_code, f"{self.label} resources differ from the verified contract", 503) from error
        self.resource_hash = hashlib.sha256(resource).hexdigest()
        self.packages: dict[str, EventPackage] = {}
        self.excluded: dict[str, str] = {}
        self._decoded_bytes = len(resource)
        for member, payload in members.items():
            event_id = self.event_id_of(member)
            if event_id is None:
                continue
            try:
                package = self._package(member, event_id, payload)
            except PACKAGE_ERRORS as error:
                self.excluded[member] = exclusion_reason(error)
                continue
            if package is not None:
                self.packages[event_id] = package

    def _decode(self, payload: bytes) -> bytes:
        data = decoded_resource(payload, PACKAGE_LIMITS)
        self._decoded_bytes += len(data)
        if self._decoded_bytes > RESOURCE_LIMIT:
            raise ValueError(f"{self.label} packages exceed the decoded byte limit")
        return data

    def _commands(self, data: bytes) -> tuple[tuple[str, str], ...]:
        table = parse_table(data, PACKAGE_LIMITS)
        fields = {field["name"]: field["type_name"] for field in table["fields"]}
        if any(fields.get(name) != kind for name, kind in self.fields.items()):
            raise ValueError(f"{self.label} sequences differ from the native command table")
        command, argument = self.fields
        return tuple((row[command], row[argument]) for row in table["rows"])

    def _package(self, member: str, event_id: str, payload: bytes) -> EventPackage | None:
        scripts = _members(self._decode(payload), PACKAGE_LIMITS)
        if EVENT_SEQUENCE not in scripts or EVENT_TEXT not in scripts:
            return None
        # The client starts an event only after both of its tables load.
        parse_table(self._decode(scripts[EVENT_TEXT]), PACKAGE_LIMITS)
        return EventPackage(member, event_id, self._commands(self._decode(scripts[EVENT_SEQUENCE])))


class HomeEventArchive(EventArchive):
    resource = HOME_EVENT_RESOURCE
    label = "Home event"
    fields = SEQUENCE_FIELDS
    invalid_code = "peruse_catalog_invalid"
    event_id_of = staticmethod(_home_event_id)

    def excluded_event_ids(self) -> dict[str, str]:
        return {event_id: reason for member, reason in self.excluded.items()
                if (event_id := _home_event_id(member)) is not None}

    def launched(self, event_id: str) -> EventPackage | None:
        return self.packages.get(_home_event_id(home_event_member(event_id)))

    def launch_exclusion(self, event_id: str) -> str | None:
        return self.excluded.get(home_event_member(event_id))


class QuestEventArchive(EventArchive):
    resource = QUEST_EVENT_RESOURCE
    label = "Quest event"
    fields = PERUSE_EVENT_FIELDS
    invalid_code = "peruse_quest_catalog_invalid"
    event_id_of = staticmethod(_quest_event_id)


def _saved_event_id(event_id: str, parameters: str) -> str:
    if not client_string(event_id):
        raise ValueError("Saving home events must launch under a string EventID")
    saved = command_arguments(parameters).get(SAVE_ARGUMENT, event_id)
    # DoPeruseSave sends an empty ID when the client stores the Save argument as a number.
    return saved if client_string(saved) else ""


def _saved_ids(package: EventPackage) -> frozenset[str]:
    saved = set()
    for parameters in package.parameters(SAVE_COMMAND):
        saved_id = _saved_event_id(package.event_id, parameters)
        if not saved_id:
            raise ValueError("DoPeruseSave sends an empty ID for a numeric Save argument")
        validate_event_id(saved_id, MAX_EVENT_ID_BYTES)
        saved.add(saved_id)
    return frozenset(saved)


def _peruse_quest_id(parameter: str) -> int | None:
    if not NUMERIC_PARAMETER.fullmatch(parameter):
        raise ValueError("DoPeruseEvent quest IDs must be numeric script parameters")
    digits = LEADING_INTEGER.match(parameter)[0]
    value = int(digits) if digits.lstrip("+-") else 0
    if not int32(value):
        raise ValueError("DoPeruseEvent quest IDs must fit the native int32 parameter")
    # 9E9B90 queues no quest record request for a value below 1.
    return value if value > 0 else None


def _perused_quests(package: EventPackage) -> frozenset[int]:
    quest_ids = (_peruse_quest_id(parameter) for parameter in package.parameters(PERUSE_EVENT_COMMAND))
    return frozenset(quest_id for quest_id in quest_ids if quest_id is not None)


def peruse_quest_ids(read_asset: Callable[[str], bytes]) -> frozenset[int]:
    archive = QuestEventArchive(read_asset)
    excluded, quest_ids = dict(archive.excluded), set()
    for package in archive.packages.values():
        try:
            quest_ids |= _perused_quests(package)
        except PACKAGE_ERRORS as error:
            excluded[package.member] = exclusion_reason(error)
    if excluded:
        log.warning("Request 84 confirms no quest event of %d excluded package(s): %s", len(excluded),
                    "; ".join(f"{member}: {reason}" for member, reason in sorted(excluded.items())))
    return frozenset(quest_ids)


class PeruseCatalog:
    def __init__(self, asset_reader: Callable[[str], bytes]):
        archive = HomeEventArchive(asset_reader)
        self.resource_hash = archive.resource_hash
        self.home_event_count = len(archive.packages)
        self.excluded = dict(archive.excluded)
        self._excluded_ids = archive.excluded_event_ids()
        saved = {}
        for package in archive.packages.values():
            try:
                saved[package.member] = _saved_ids(package)
            except PACKAGE_ERRORS as error:
                self.excluded[package.member] = self._excluded_ids[package.event_id] = exclusion_reason(error)
        for member in colliding_sources(saved):
            self.excluded[member] = HASH_COLLISION
            self._excluded_ids.update(dict.fromkeys(saved.pop(member), HASH_COLLISION))
        self.event_ids = frozenset().union(*saved.values())

    def supports(self, event_id: str) -> bool:
        return event_id in self.event_ids

    def excluded_reason(self, event_id: str) -> str | None:
        return self._excluded_ids.get(event_id)

    def status(self) -> dict:
        return {"ready": True, "resource_hash": self.resource_hash, "home_event_count": self.home_event_count,
                "supported_event_count": len(self.event_ids), "supported_events": sorted(self.event_ids),
                "excluded_package_count": len(self.excluded),
                "excluded_packages": [{"package": member, "reason": reason}
                                      for member, reason in sorted(self.excluded.items())]}


class PeruseService:
    def __init__(self, bootstrap: BootstrapService, *, asset_reader: Callable[[str], bytes] | None = None):
        self.bootstrap = bootstrap
        self._startup_profile = ProtocolRegistry.load().by_registration[137]
        self.catalog = None
        self.catalog_error = None
        try:
            self.catalog = PeruseCatalog(asset_reader or asset_root_reader(bootstrap.asset_root))
        except ProtocolError as error:
            self.catalog_error = error.code
        if self.catalog is not None and self.catalog.excluded:
            log.warning("Request 75 serves home events without %d excluded package(s): %s", len(self.catalog.excluded),
                        "; ".join(f"{member}: {reason}" for member, reason in sorted(self.catalog.excluded.items())))

    def handlers(self):
        return {75: self.save}

    def status(self):
        catalog = self.catalog.status() if self.catalog is not None else {"ready": False, "error": self.catalog_error}
        return {"implemented_operations": [75], "schema_version": 1, **catalog,
                "save_policy": ("Mark a home event read when its native script saves it through DoPeruseSave; "
                                "preserve existing nonzero flags without granting rewards"),
                "exclusion_policy": "Packages outside the client contract are excluded one by one; the rest stay served",
                "reload_policy": "Full PEV snapshot through operation 137",
                "local_limits": {"events": MAX_PERUSED_EVENTS, "event_id_bytes": MAX_EVENT_ID_BYTES,
                                 "stored_event_id_bytes": STORED_EVENT_ID_BYTES}}

    def _native_save(self, event_id: str) -> str:
        if self.catalog is None:
            log.warning("Request 75 refused read history for %r: home event resources are unavailable (%s)",
                        event_id, self.catalog_error)
            raise ProtocolError("peruse_catalog_unavailable", "Verified home event resources are unavailable", 503)
        if self.catalog.supports(event_id):
            return event_id
        reason = self.catalog.excluded_reason(event_id)
        if reason is not None:
            log.warning("Request 75 refused read history for %r: its home event package is excluded (%s)", event_id, reason)
            raise ProtocolError("peruse_event_excluded", "The home event that saves this history is excluded", 503)
        log.warning("Request 75 refused read history for %r: no home event saves it through DoPeruseSave", event_id)
        raise ProtocolError("peruse_event_unsupported", "No native home event saves this read history", 501)

    def save(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = registered(envelope, {75}, {"ID"}, {"ID"})
        event_id = scalar(request["ID"], "S", max_bytes=MAX_EVENT_ID_BYTES)
        try:
            validate_event_id(event_id, MAX_EVENT_ID_BYTES)
        except (ValueError, TypeError) as error:
            invalid(str(error))
        return mark_read(self.bootstrap, self._startup_profile, envelope, store, lambda: self._native_save(event_id))


def mark_read(bootstrap: BootstrapService, startup_profile: WireProfile, envelope: RequestEnvelope, store: Store,
              resolve: Callable[[], str]) -> bytes:
    for _ in range(3):
        profile = bootstrap._authenticate(envelope, store)
        event_id = resolve()
        previous = next((row["FLG"] for row in read_perused(profile.state) if row["ID"] == event_id), 0)
        updated = set_perused_flags(profile.state, {event_id: previous or 1})
        encode_response(startup_profile, bootstrap._configuration_response(replace(profile, state=updated)))
        response = dumps(Container(children=(bootstrap._system(profile), peruse_response(updated, [event_id]))))
        encode_response(envelope.profile, response)
        if updated == profile.state:
            return response
        try:
            store.update_state(updated, profile.revision)
            return response
        except ValueError:
            continue
    raise ProtocolError("profile_changed", "Event history changed while saving", 409)

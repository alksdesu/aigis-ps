from __future__ import annotations

from dataclasses import dataclass
import re
import threading
from xml.etree import ElementTree as ET

from .errors import ProtocolError
from .native_numbers import UINT32_MASK, int32
from .protocol import RequestEnvelope
from .quest_modes import PLATFORM_CODES


MAX_CANDIDATES = 32
MAX_TEAM_ROWS = 4096
MAX_TEXT_BYTES = 4096
DAMAGE_DISPLAY_VALUES = (0, 1)
# RT serializes the prize slot as one byte; only slots 1–5 map to treasures and record bits.
MAX_PRIZE_SLOT = 255
# The client reports kills without a native bound; this local cap rejects absurd result logs.
MAX_KILLS = 100000
# 4CF530 copies the saved prince into BL as UX+1000+n, so these rows reuse his UniqueID and miss the roster.
AVATAR_INDEX_OFFSETS = range(1000, 1500)
# 6785B0 numbers QuestUnitTable members from UX+3001 in table order, and 4E57A0 sends them as HL.
FIXED_INDEX_OFFSET = 3001
_INTEGER = re.compile(r"[+-]?[0-9]{1,10}\Z")
_COMMON = frozenset(("UI", "UK", "MK", "CT", "PFN", "PlatformCode", "GPVI", "GPOT", "VN", "ST", "HS"))
_TEAMS = frozenset(("BL", "GL", "HL", "AL", "GG", "SallyContainer"))
_TEAM_COLUMNS = ("A1", "A7", "A8", "AB", "AA")
_GUEST_COLUMNS = ("TP", "A1", "A2", "A7", "AB")
_SALLY_INTS = ("InviteIndex", "PlainCost", "SallyCost", "PropCount")
_SALLY_BOOLS = ("TokenUnit", "GuestUnit", "HoldUnit", "AvatarUnit")
_SALLY_PROPERTIES = ("Mul", "Add", "MinMaxFlag")


@dataclass(frozen=True)
class QuestCandidate:
    id: int
    slot: int
    attributes: int


@dataclass(frozen=True)
class TeamMember:
    card_id: int
    get_index: int
    slot: int
    unique_id: int
    cost_mod: int


@dataclass(frozen=True)
class SallyRecord:
    invite_index: int
    plain_cost: int
    sally_cost: int
    prop_count: int
    token_unit: bool
    guest_unit: bool
    hold_unit: bool
    avatar_unit: bool


@dataclass(frozen=True)
class QuestStart:
    quest_id: int
    revision: str
    client_time: int
    candidates: tuple[QuestCandidate, ...]
    boost: int | None = None
    members: tuple[TeamMember, ...] = ()
    visitors: tuple[TeamMember, ...] = ()
    fixed: tuple[TeamMember, ...] = ()


@dataclass(frozen=True)
class QuestFinish:
    quest_id: int
    stars: int
    kills: int
    client_time: int
    obtained_mask: int
    selected_mask: int
    gold_reported: int
    damage_display: int | None = None
    gold_events: int = 0
    members: tuple[TeamMember, ...] = ()
    visitors: tuple[TeamMember, ...] = ()
    fixed: tuple[TeamMember, ...] = ()
    sallies: tuple[SallyRecord, ...] = ()


@dataclass(frozen=True)
class QuestExit:
    quest_id: int
    result_code: int


def _invalid(message: str) -> ProtocolError:
    return ProtocolError("quest_request_invalid", message)


def _integer(text: str) -> int:
    if not _INTEGER.fullmatch(text):
        raise _invalid("Expected a decimal int32 value")
    value = int(text)
    if not int32(value):
        raise _invalid("Integer value exceeds the native int32 range")
    return value


def _members(node: ET.Element) -> dict[str, ET.Element]:
    if node.attrib or (node.text or "").strip() or (node.tail or "").strip():
        raise _invalid("Quest containers must be untyped and contain no mixed text")
    result = {}
    for child in node:
        if not isinstance(child.tag, str) or child.tag in result or (child.tail or "").strip():
            raise _invalid("Quest fields must have distinct names and no mixed text")
        result[child.tag] = child
    return result


def _fields(node: ET.Element, allowed, required=()) -> dict[str, ET.Element]:
    result = _members(node)
    if set(result) - set(allowed) or set(required) - set(result):
        raise _invalid("Quest fields are unknown or incomplete")
    return result


def _column(node: ET.Element, kind: str, *, minimum: int = 0, maximum: int = MAX_TEAM_ROWS) -> tuple:
    if node.attrib != {"T": kind} or (node.text or "").strip() or not minimum <= len(node) <= maximum:
        raise _invalid("Quest column type or value count is invalid")
    values = []
    for child in node:
        if child.tag != "V" or child.attrib or len(child) or (child.tail or "").strip():
            raise _invalid("Quest column values must be plain V elements")
        text = child.text or ""
        if kind == "I":
            value = _integer(text)
        elif kind == "B":
            if text not in ("true", "false"):
                raise _invalid("Quest boolean values must be true or false")
            value = text == "true"
        else:
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise _invalid("Quest string value exceeds the local limit")
            value = text
        values.append(value)
    return tuple(values)


def _scalar(fields: dict[str, ET.Element], name: str, kind: str = "I"):
    return _column(fields[name], kind, minimum=1, maximum=1)[0]


def _parallel(fields: dict[str, ET.Element], names: tuple[str, ...], *, maximum: int = MAX_TEAM_ROWS) -> tuple[tuple, ...]:
    columns = tuple(_column(fields[name], "I", maximum=maximum) for name in names)
    if len({len(column) for column in columns}) != 1:
        raise _invalid("Quest parallel columns must have equal row counts")
    return tuple(zip(*columns))


def _transport_text(node: ET.Element) -> str:
    if node.attrib or len(node) or not node.text or len(node.text.encode("utf-8")) > 64:
        raise _invalid("Quest transport fields must be bounded untyped scalars")
    return node.text


def _header(envelope: RequestEnvelope, allowed: frozenset[str], required: frozenset[str], registrations: frozenset[int]) -> dict[str, ET.Element]:
    if not envelope.profile.registration_keys or not set(envelope.profile.registration_keys) <= registrations:
        raise _invalid("Unexpected registered quest operation")
    if envelope.document.tag != "DA":
        raise _invalid("Expected the native DA quest root")
    fields = _fields(envelope.document, _COMMON | allowed, _COMMON | required)
    for name in ("UI", "UK", "MK", "PFN", "GPVI", "GPOT"):
        _scalar(fields, name, "S")
    for name in ("CT", "PlatformCode"):
        _scalar(fields, name)
    _transport_text(fields["VN"])
    if _integer(_transport_text(fields["ST"])) != envelope.profile.st:
        raise _invalid("Quest ST does not match the decoded operation")
    _integer(_transport_text(fields["HS"]))
    return fields


def _sally(node: ET.Element) -> tuple[SallyRecord, ...]:
    fields = _members(node)
    if "SallyCount" not in fields:
        raise _invalid("SallyContainer requires its record count")
    count = _scalar(fields, "SallyCount")
    if not 1 <= count <= MAX_TEAM_ROWS or set(fields) != {"SallyCount", *(f"Sally{index:03d}" for index in range(count))}:
        raise _invalid("Sally records must match their count and consecutive names")
    records = []
    for index in range(count):
        record = _fields(fields[f"Sally{index:03d}"], _SALLY_INTS + _SALLY_BOOLS + _SALLY_PROPERTIES, _SALLY_INTS + _SALLY_BOOLS)
        values = tuple(_scalar(record, name) for name in _SALLY_INTS)
        flags = tuple(_scalar(record, name, "B") for name in _SALLY_BOOLS)
        sally = SallyRecord(*values, *flags)
        if not 0 <= sally.prop_count <= MAX_TEAM_ROWS:
            raise _invalid("Sally property count exceeds the local limit")
        present = set(record) & set(_SALLY_PROPERTIES)
        if sally.prop_count or present:
            if present != set(_SALLY_PROPERTIES) or len(_parallel(record, _SALLY_PROPERTIES)) != sally.prop_count:
                raise _invalid("Sally property columns do not match PropCount")
        records.append(sally)
    return tuple(records)


def _teams(fields: dict[str, ET.Element]) -> tuple[int, dict[str, tuple[TeamMember, ...]], tuple[SallyRecord, ...]]:
    teams = {}
    for name in ("BL", "GL", "HL", "AL"):
        teams[name] = ()
        if name not in fields:
            continue
        team = _fields(fields[name], _TEAM_COLUMNS)
        if team:
            if set(team) != set(_TEAM_COLUMNS):
                raise _invalid("Quest team columns are incomplete")
            # 4E5C40 writes A1 CardID, A7 GetIndex, A8 formation slot, AB UniqueID and AA CostModMixValue per row.
            teams[name] = tuple(TeamMember(*row) for row in _parallel(team, _TEAM_COLUMNS))
    gold_events = 0
    if "GG" in fields:
        guest = _fields(fields["GG"], ("I",) + _GUEST_COLUMNS, ("I",))
        gold_events = _scalar(guest, "I")
        if len(guest) > 1:
            if set(guest) != {"I", *_GUEST_COLUMNS}:
                raise _invalid("Quest guest columns are incomplete")
            _parallel(guest, _GUEST_COLUMNS)
    sallies = _sally(fields["SallyContainer"]) if "SallyContainer" in fields else ()
    return gold_events, teams, sallies


def parse_start(envelope: RequestEnvelope) -> QuestStart:
    fields = _header(envelope, _TEAMS | {"QI", "Rev", "RT", "Boost"}, frozenset(("QI", "Rev")), frozenset((77, 78, 81, 82)))
    quest_id = _scalar(fields, "QI")
    revision = _scalar(fields, "Rev", "S")
    if quest_id <= 0 or len(revision.encode("utf-8")) > 256:
        raise _invalid("Quest identity or resource revision is invalid")
    _, teams, _ = _teams(fields)
    candidates = ()
    if "RT" in fields:
        table = _fields(fields["RT"], ("ID", "TYPE", "ATTR"))
        if table:
            if set(table) != {"ID", "TYPE", "ATTR"}:
                raise _invalid("Quest candidate columns are incomplete")
            rows = _parallel(table, ("ID", "TYPE", "ATTR"), maximum=MAX_CANDIDATES)
            if any(identifier != index or not 1 <= slot <= MAX_PRIZE_SLOT or not 0 <= attributes <= 3
                   for index, (identifier, slot, attributes) in enumerate(rows)):
                raise _invalid("Quest candidates require consecutive IDs, prize slots and valid attributes")
            candidates = tuple(QuestCandidate(*row) for row in rows)
    if set(envelope.profile.registration_keys) & {78, 82} and candidates:
        raise _invalid("This native start profile does not serialize ordinary candidates")
    boost = None
    if "Boost" in fields:
        boost = _scalar(_fields(fields["Boost"], ("V",), ("V",)), "V")
        if boost <= 0:
            raise _invalid("Quest boost values must be positive")
    return QuestStart(quest_id, revision, _scalar(fields, "CT"), candidates, boost,
                      teams["BL"], teams["AL"], teams["HL"])


def platform_code(envelope: RequestEnvelope) -> int:
    return _scalar(_members(envelope.document), "PlatformCode")


class PlatformTally:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = [0] * len(PLATFORM_CODES)
        self._last: int | None = None

    def record(self, code: int) -> None:
        with self._lock:
            self._counts[code] += 1
            self._last = code

    def status(self) -> dict:
        with self._lock:
            return {"counts": {code: count for code, count in enumerate(self._counts) if count}, "last": self._last}


def validate_damage_display(value) -> int:
    if type(value) is not int or value not in DAMAGE_DISPLAY_VALUES:
        raise _invalid("Damage display setting must be zero or one")
    return value


def parse_finish(envelope: RequestEnvelope) -> QuestFinish:
    fields = _header(envelope, _TEAMS | {"RT", "DeadUnitList", "GC"}, frozenset(("RT",)), frozenset((80, 83)))
    gold_events, teams, sallies = _teams(fields)
    if gold_events < 0:
        raise _invalid("Gold event record count must be nonnegative")
    if "DeadUnitList" in fields:
        dead = _fields(fields["DeadUnitList"], ("UnitId",), ("UnitId",))
        _column(dead["UnitId"], "I")
    result = _fields(fields["RT"], ("LG", "DropResult"), ("LG", "DropResult"))
    log = _fields(result["LG"], ("TG", "TT"), ("TG", "TT"))
    if _scalar(log, "TG", "S") != "finishQuest":
        raise _invalid("Unexpected quest result log category")
    text = _scalar(log, "TT", "S")
    if not re.fullmatch(r"[0-9]{1,10} [0-9]{1,10} [0-9]{1,10}", text):
        raise _invalid("Quest result log requires three space-separated integers")
    quest_id, stars, kills = (_integer(value) for value in text.split(" "))
    # 462850 sends the kill count second for the score types 45A7D0 flags, so finish checks each mode's result set.
    if quest_id <= 0 or not 0 <= stars <= MAX_KILLS or kills > MAX_KILLS:
        raise _invalid("Quest identity, star count or kill count is invalid")
    drops = _fields(result["DropResult"], ("DP", "DPLIST", "GoldGet"), ("DP", "DPLIST", "GoldGet"))
    obtained, selected, gold = (_scalar(drops, name) for name in ("DP", "DPLIST", "GoldGet"))
    if gold < 0:
        raise _invalid("Reported quest gold must be nonnegative")
    damage_display = None
    if "GC" in fields:
        config = _fields(fields["GC"], ("F7",))
        if config:
            damage_display = validate_damage_display(_scalar(config, "F7"))
    # The native serializer writes obtained bit 2 as DP and selected bit 0 as DPLIST.
    return QuestFinish(quest_id, stars, kills, _scalar(fields, "CT"), obtained & UINT32_MASK,
                       selected & UINT32_MASK, gold, damage_display, gold_events,
                       teams["BL"], teams["AL"], teams["HL"], sallies)


def parse_exit(envelope: RequestEnvelope) -> QuestExit:
    profile = envelope.profile
    if (profile.name != "quest_exit" or profile.route != "ntwh57oy" or profile.registration_keys
            or profile.st is not None or profile.request_key != 0 or profile.response_key != 0
            or profile.checksum_required or envelope.document.tag != "DA"):
        raise _invalid("Unexpected direct quest exit operation")
    fields = _fields(envelope.document, ("UI", "UK", "TG", "TT"), ("UI", "UK", "TG", "TT"))
    for name in ("UI", "UK"):
        _scalar(fields, name, "S")
    if _scalar(fields, "TG", "S") != "finishQuest":
        raise _invalid("Unexpected quest exit log category")
    text = _scalar(fields, "TT", "S")
    if not re.fullmatch(r"[0-9]{1,10},(?:0|-1)", text):
        raise _invalid("Quest exit log requires QuestID and result zero or minus one")
    quest_id, result_code = (_integer(value) for value in text.split(","))
    if quest_id <= 0:
        raise _invalid("Quest exit identity must be positive")
    return QuestExit(quest_id, result_code)

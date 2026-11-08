from __future__ import annotations

import json
import secrets
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import cached_property
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from . import business_calendar
from .al_format import ParseLimits, Schema, decompress_all4, parse_schema, parse_table
from .alxml import AlXmlError, Column, Container, Limits, ValueType, dumps, loads
from .assets import resolve_asset
from .card_status import CARD_STATUS_LIMITS
from .config_state import (CONFIGURATION_ALIASES, CONFIGURATION_FIELDS, CONFIGURATION_RESPONSE_FIELDS, SOUND_OPTION_FIELDS,
                           configuration_delta, configuration_row, patch_configuration)
from .errors import ProtocolError, UnsupportedOperation
from .energy_clock import ENCODING, UTC_OFFSET_MINUTES, mark_new, utc_now, validate_clock
from .native_numbers import INT32_MAX, int32
from .protocol import ProtocolRegistry, RequestEnvelope, encode_response, parse_document
from .public_tables import MASTER_LIMITS, validate_public_master
from .quest_record_compat import retained_quest_ids
from .request_fields import COMMON_FIELDS
from .startup_diagnostics import StartupDiagnostics
from .storage import PlayerProfile, Store
from .wallet_money import GOLD_LIMIT


LOCAL_RULE_LIMITS = MappingProxyType({"money": GOLD_LIMIT, "experience": INT32_MAX, "rank": INT32_MAX,
                                      "action_points": INT32_MAX})


@dataclass(frozen=True)
class LocalRules:
    money: int = 1000
    experience: int = 0
    rank: int = 1
    charisma: int | None = None
    action_points: int = 0
    user_name: str = "Local Player"
    bootstrap_token: str = "local-only"

    def __post_init__(self) -> None:
        for name, limit in LOCAL_RULE_LIMITS.items():
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= limit:
                raise ValueError(f"Invalid local rule: {name}")
        if self.rank < 1 or self.action_points > 12:
            raise ValueError("Rank must be positive and action points cannot exceed the base maximum of 12")
        if self.charisma is not None and (type(self.charisma) is not int or self.charisma < 0):
            raise ValueError("Charisma must be a nonnegative integer")
        if self.money == self.experience == 0:
            raise ValueError("The native GameParam loader rejects zero money and zero experience together")
        if not isinstance(self.user_name, str) or not self.user_name or len(self.user_name.encode("utf-8")) > 39:
            raise ValueError("User name must fit the native 40-byte field")
        if not isinstance(self.bootstrap_token, str) or not 1 <= len(self.bootstrap_token) <= 256:
            raise ValueError("A bounded local bootstrap token is required")


@dataclass(frozen=True)
class TableSpec:
    resource: str
    fields: tuple[tuple[str, int, str], ...]


TABLES = {
    "game_param": TableSpec("GameParam.ard", (
        ("Money", 1, "A1"), ("Experience", 1, "A2"), ("Rank", 1, "A3"),
        ("NowCharisma", 1, "A4"), ("NowActionPoint", 1, "A5"),
        ("CharismaChangeDate", 7, "A6"), ("CharismaChangeTime", 8, "A7"),
        ("ActionPointChangeDate", 7, "A8"), ("ActionPointChangeTime", 8, "A9"),
        ("GiftItem1", 1, "AA"), ("GiftItem2", 1, "AB"), ("GiftItem3", 1, "AC"),
        ("GiftItem4", 1, "AD"), ("RedCrystal", 1, "AE"), ("EventParam", 1, "AF"),
        ("GiftItem5", 1, "AG"), ("GiftItem6", 1, "AH"), ("_PointSystem", 1, "AI"),
        ("ExtendedMaxCharisma", 1, "ExtendedMaxCharisma"),
        ("ExtendedMaxStamina", 1, "ExtendedMaxStamina"),
        ("ExtendedMaxCharismaNextMonth", 1, "ExtendedMaxCharismaNextMonth"),
        ("ExtendedMaxStaminaNextMonth", 1, "ExtendedMaxStaminaNextMonth"),
    )),
    "game_param2": TableSpec("GameParam2.ard", (
        ("LookHarlemFlag00", 1, "A1"), ("LookHarlemFlag01", 1, "A2"),
        ("LookHarlemFlag10", 1, "A3"), ("LookHarlemFlag11", 1, "A4"),
        ("LastLoginDate", 7, "A5"), ("LogInStampCount", 1, "A6"),
        ("NormalGachaFree", 1, "A7"), ("MaintenanceCheckDateTime", 1, "A8"),
        ("LookHarlemFlag02", 1, "A9"), ("LookHarlemFlag12", 1, "AA"),
        ("LookHarlemFlag03", 1, "AB"), ("LookHarlemFlag13", 1, "AC"),
        ("WatchEventFlag0", 33, "AD"), ("WatchEventFlag1", 33, "AE"),
        ("WatchEventFlag0_2", 33, "AF"), ("WatchEventFlag1_2", 33, "B1"),
        ("StampCardGrade", 1, "C1"), ("_EvCmb", 1, "C7"), ("GloryPoint", 1, "C8"),
        ("HomeCookingReceivedAmount", 1, "CC"), ("HomeCookingReceivedDate", 1, "CD"),
        ("BonusPremiumSummonCount", 1, "CA"), ("BonusPremiumSummonDate", 7, "CB"),
        ("GameStartDateTime", 9, "GameStartDateTime"),
        ("ComebackLoginDateTime", 9, "ComebackLoginDateTime"),
        ("LatestComebackLoginBonusDateTime", 9, "D0"), ("LatestComebackLoginBonusDays", 1, "D1"),
        ("LatestComebackSummonBonusDateTime", 9, "E0"), ("LatestComebackSummonBonusDays", 1, "E1"),
        ("GoddessRevivalTotalStarCount", 1, "GoddessRevivalTotalStarCount"),
    )),
    "config": TableSpec("Config.ard", (
        ("UserName", 33, "A1"), ("UseBgm", 5, "A2"), ("UseSe", 5, "A3"),
        ("MenuTutorialFlags", 1, "A4"), ("HaveUnitMaxSizeBuy", 1, "A5"),
        ("ExperienceUpCloseDateTime", 9, "A6"), ("MoneyUpCloseDateTime", 9, "A7"),
        ("CostUpCloseDateTime", 9, "A8"), ("SpecialEffectsFlags", 1, "AA"),
        ("SpecialEffectsFlags2", 1, "AB"), ("MissionRewardFlags2", 1, "AD"),
        ("PurposeSettingFlags", 1, "AE"), ("_BlsDT_DamageCut", 9, "B1"),
        ("_BlsDT_BoostAtk_F", 9, "B2"), ("_BlsDT_BoostAtk_R", 9, "B3"),
        ("_GuideUnitIndex", 1, "C1"), ("_VolumeBgm", 1, "C2"), ("_VolumeSe", 1, "C3"),
        ("_FlagSort", 33, "F1"), ("_FlagFilter", 33, "F2"), ("_FlagMissionFilter", 33, "F4"),
        ("TypeOfSortOrder", 33, "F5"), ("AutoSelectConfig", 1, "F6"), ("DamageDispEnable", 1, "F7"),
        ("EventViewRequest", 33, "F8"), ("BattlePlaceMagnetFlag", 1, "F9"),
        ("BattleAcceleratorFlag", 1, "FA"), ("IntegrationAutoSelectConfig", 1, "FB"),
        ("BattleDClickSkillDisableFlag", 1, "FD"), ("_BattleGodBlessFilter", 1, "FE"),
    )),
    "reproduction": TableSpec("ReproductionItem.ard", (
        ("I0", 1, "I0"), ("I1", 1, "I1"), ("F0", 1, "F0"), ("F0RT", 1, "F0RT"), ("EI", 1, "EI"),
    )),
    "crystals": TableSpec("CrystalParam.ard", (
        ("FreeFlag", 5, "A1"), ("Num", 1, "A2"), ("Date", 1, "Date"),
    )),
    "quest_records": TableSpec("QuestRecord.ard", (
        ("QuestID", 1, "QuestID"), ("MaxRank", 1, "A1"), ("AchieveFlag", 1, "A2"),
        ("RemainAssault", 1, "A3"), ("FirstDefeatDate", 1, "A4"), ("Shown", 5, "Shown"),
    )),
}
WIRE_TYPES = {1: ValueType.INT, 5: ValueType.BOOL, 7: ValueType.INT, 8: ValueType.FLOAT, 9: ValueType.DATETIME, 33: ValueType.STRING}
MULTI_ROW_TABLES = frozenset(("crystals", "quest_records"))
PGP2_FIELDS = (
    ("A5", "LastLoginDate"), ("A6", "LogInStampCount"), ("A7", "NormalGachaFree"),
    ("AD", "WatchEventFlag0"), ("AE", "WatchEventFlag1"), ("AF", "WatchEventFlag0_2"),
    ("B1", "WatchEventFlag1_2"), ("C1", "StampCardGrade"), ("C7", "_EvCmb"),
    ("C8", "GloryPoint"), ("CA", "BonusPremiumSummonCount"), ("CB", "BonusPremiumSummonDate"),
    ("CC", "HomeCookingReceivedAmount"), ("CD", "HomeCookingReceivedDate"),
    ("ComebackLoginDateTime", "ComebackLoginDateTime"),
    ("LatestComebackLoginBonusDateTime", "LatestComebackLoginBonusDateTime"),
    ("LatestComebackLoginBonusDays", "LatestComebackLoginBonusDays"),
    ("LatestComebackSummonBonusDateTime", "LatestComebackSummonBonusDateTime"),
    ("LatestComebackSummonBonusDays", "LatestComebackSummonBonusDays"),
    ("GoddessRevivalTotalStarCount", "GoddessRevivalTotalStarCount"),
)
EVENT_FLAGS_CONVERTED = 1
# The client rebuilds legacy flags this way itself whenever 33 fails, so saving it keeps both sides identical.
LEGACY_EVENT_IDS = range(1, 127)
LEGACY_FLAGGED_EVENT_BASE = 16
LEGACY_FLAG_BITS = 32
WATCH_FLAG_PADDING = 130
SECOND_WATCH_FLAG_PADDING = 256
WATCHED, UNWATCHED = "1", "0"
STARTUP_COLLECTIONS = {
    "PRM": "backgrounds",
    "PEV": "perused_events",
    "PVH": "vote_history",
    "GF": "universal_flags",
    "POSSESSION": "possessions",
    "ExchangeHistory": "exchange_history",
    "PlayerFilterParam": "filter_settings",
    "RecommendPurchaseList": "recommendations",
    "FameDepository": "fame_depository",
    "EventPassStatus": "event_pass_status",
}
COLLECTION_TABLES = ("card_status", "menu_blocks", "quest_records", *STARTUP_COLLECTIONS.values())
GAME_INFO = (("Major", 2), ("Minor", 400), ("Maintenance", 0), ("LimitType", 0), ("Priority", 0))
# Native DMM accounts without a second data slot report IsSucceeded 0, so the title offers only the data-1 start.
LOGIN_IS_SUCCEEDED = 0
QUEST_LIMITS = Limits(max_bytes=16 * 1024 * 1024, max_depth=4, max_elements=500_000, max_values=400_000)
UNPLAYED_QUEST_RECORD = MappingProxyType({"MaxRank": 0, "AchieveFlag": 0, "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False})
QUEST_RECORD_COUNTERS = tuple(name for name in UNPLAYED_QUEST_RECORD if name != "Shown")
MAX_QUEST_RECORDS = 65535
STARTUP_COLLECTION_LIMITS = Limits(max_elements=500_000, max_values=500_000)
TUTORIAL_OPTION_FIELDS = frozenset(("A4",))
CONFIGURATION_OPTION_GROUPS = (TUTORIAL_OPTION_FIELDS, SOUND_OPTION_FIELDS)
BOOLEAN_CONFIGURATION_FIELDS = frozenset(wire for wire, kind in CONFIGURATION_FIELDS.values() if kind is bool)


def _unavailable(code: str, message: str) -> ProtocolError:
    return ProtocolError(code, message, 503)


def _key_values(name: str, values: tuple[tuple[str, str], ...]) -> Container:
    return Container(name, (
        Column("A1", ValueType.STRING, tuple(key for key, _ in values)),
        Column("A2", ValueType.STRING, tuple(value for _, value in values)),
    ))


def _legacy_watch_flags(row: dict, kind: int) -> str:
    def watched(event_id: int) -> bool:
        if event_id < LEGACY_FLAGGED_EVENT_BASE:
            return False
        word, bit = divmod(event_id - LEGACY_FLAGGED_EVENT_BASE, LEGACY_FLAG_BITS)
        return bool(row[f"LookHarlemFlag{kind}{word}"] >> bit & 1)
    return "".join(WATCHED if watched(event_id) else UNWATCHED for event_id in LEGACY_EVENT_IDS)


def _option_group_error() -> ProtocolError:
    return ProtocolError("invalid_configuration_request", "Expected tutorial flags or the complete sound option group")


def configuration_updates(values: Mapping[str, int]) -> dict:
    if not isinstance(values, Mapping) or set(values) not in CONFIGURATION_OPTION_GROUPS:
        raise _option_group_error()
    updates = {}
    for wire, number in values.items():
        if type(number) is not int:
            raise ProtocolError("invalid_configuration_request", f"Expected one integer value in {wire}")
        boolean = wire in BOOLEAN_CONFIGURATION_FIELDS
        if not int32(number) or (boolean and number not in (0, 1)):
            raise ProtocolError("invalid_configuration_request", f"Configuration value is outside its range: {wire}")
        updates[CONFIGURATION_ALIASES[wire]] = bool(number) if boolean else number
    return updates


class _RequestIntegers(Mapping):
    # Parsing on lookup keeps each field's format error ahead of the range errors of later fields.
    def __init__(self, nodes: list):
        self._nodes = {node.tag: node for node in nodes}

    def __getitem__(self, wire: str) -> int:
        node = self._nodes[wire]
        if (node.attrib != {"T": "I"} or len(node) != 1 or (node.text or "").strip()
                or (node.tail or "").strip()):
            raise ProtocolError("invalid_configuration_request", f"Expected one integer value in {wire}")
        value = node[0]
        text = value.text or ""
        if (value.tag != "V" or value.attrib or len(value) or (value.tail or "").strip()
                or len(text) > 11 or not re.fullmatch(r"[+-]?[0-9]+", text)):
            raise ProtocolError("invalid_configuration_request", f"Invalid integer value in {wire}")
        return int(text)

    def __contains__(self, wire: object) -> bool:
        return wire in self._nodes

    def __iter__(self):
        return iter(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)


class BootstrapService:
    def __init__(
        self,
        asset_root: Path,
        master_provider: Callable[[str], bytes] | None = None,
        *,
        local_rules: LocalRules | None = None,
        clock: Callable[[], datetime] | None = None,
        asset_base_url: str = "http://127.0.0.1:18103/assets/",
        asset_version: str | None = None,
        possession_provider: Callable[[dict], Container] | None = None,
        filter_provider: Callable[[dict], Container] | None = None,
        card_status_provider: Callable[[dict], Container] | None = None,
        present_provider: Callable[[dict], Container] | None = None,
        peruse_provider: Callable[[dict], Container] | None = None,
        exchange_history_provider: Callable[[dict], Container] | None = None,
        universal_flag_provider: Callable[[dict], Container] | None = None,
        fame_provider: Callable[[dict], Container] | None = None,
        background_provider: Callable[[dict], Container] | None = None,
        event_pass_provider: Callable[[dict], Container] | None = None,
        vote_provider: Callable[[dict], Container] | None = None,
        login_reward_seed_provider: Callable[[dict, datetime], dict] | None = None,
        rank_reward_seed_provider: Callable[[dict, datetime], dict] | None = None,
        monthly_state_provider: Callable[[dict, datetime], dict] | None = None,
        startup_diagnostics: StartupDiagnostics | None = None,
    ):
        self.asset_root = Path(asset_root)
        self.master_provider = master_provider
        if possession_provider is not None and not callable(possession_provider):
            raise ValueError("Possession provider must be callable")
        self.possession_provider = possession_provider
        if filter_provider is not None and not callable(filter_provider):
            raise ValueError("Filter provider must be callable")
        self.filter_provider = filter_provider
        if card_status_provider is not None and not callable(card_status_provider):
            raise ValueError("Card-status provider must be callable")
        self.card_status_provider = card_status_provider
        if present_provider is not None and not callable(present_provider):
            raise ValueError("Present provider must be callable")
        self.present_provider = present_provider
        if peruse_provider is not None and not callable(peruse_provider):
            raise ValueError("Peruse provider must be callable")
        self.peruse_provider = peruse_provider
        if exchange_history_provider is not None and not callable(exchange_history_provider):
            raise ValueError("Exchange-history provider must be callable")
        self.exchange_history_provider = exchange_history_provider
        if universal_flag_provider is not None and not callable(universal_flag_provider):
            raise ValueError("Universal-flag provider must be callable")
        self.universal_flag_provider = universal_flag_provider
        if fame_provider is not None and not callable(fame_provider):
            raise ValueError("Fame-depository provider must be callable")
        self.fame_provider = fame_provider
        if background_provider is not None and not callable(background_provider):
            raise ValueError("Background provider must be callable")
        self.background_provider = background_provider
        if event_pass_provider is not None and not callable(event_pass_provider):
            raise ValueError("Event-pass provider must be callable")
        self.event_pass_provider = event_pass_provider
        if vote_provider is not None and not callable(vote_provider):
            raise ValueError("Vote provider must be callable")
        self.vote_provider = vote_provider
        if login_reward_seed_provider is not None and not callable(login_reward_seed_provider):
            raise ValueError("Login reward seed provider must be callable")
        self.login_reward_seed_provider = login_reward_seed_provider
        if rank_reward_seed_provider is not None and not callable(rank_reward_seed_provider):
            raise ValueError("Rank reward seed provider must be callable")
        self.rank_reward_seed_provider = rank_reward_seed_provider
        if monthly_state_provider is not None and not callable(monthly_state_provider):
            raise ValueError("Monthly state provider must be callable")
        self.monthly_state_provider = monthly_state_provider
        if startup_diagnostics is not None and not isinstance(startup_diagnostics, StartupDiagnostics):
            raise ValueError("Startup diagnostics must be a StartupDiagnostics registry")
        self.startup_diagnostics = StartupDiagnostics() if startup_diagnostics is None else startup_diagnostics
        self.missions = None
        self.comeback_logins = None
        self.rules = local_rules or LocalRules()
        self.clock = utc_now if clock is None else clock
        if not callable(self.clock):
            raise ValueError("Bootstrap clock must be callable")
        if not isinstance(asset_base_url, str) or len(asset_base_url) > 512 or any(ord(char) <= 32 for char in asset_base_url):
            raise ValueError("Asset base must be bounded URL text without whitespace")
        endpoint = urlsplit(asset_base_url)
        if endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost", "::1") or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or not endpoint.path.endswith("/"):
            raise ValueError("Asset base must be a loopback HTTP URL ending in a slash")
        if endpoint.port is not None and not 1 <= endpoint.port <= 65535:
            raise ValueError("Invalid asset port")
        self.asset_base_url = asset_base_url
        if asset_version is not None and (not isinstance(asset_version, str) or not re.fullmatch(r"[a-f0-9]{40}", asset_version)):
            raise ValueError("Asset version must be a verified 40-character directory")
        self.asset_version = asset_version
        master_schemas = json.loads(files("aigis_ps").joinpath("data/master-schemas.json").read_text(encoding="utf-8"))
        self.master_schemas = {name: [field["name"] for field in schema["fields"]] for name, schema in master_schemas["tables"].items()}
        self.schemas: dict[str, Schema] = {}
        self.issues: dict[str, str] = {}
        self.rank_rows: list[dict] = []
        for name, spec in TABLES.items():
            try:
                schema = parse_schema(self._read_asset(spec.resource))
                if {field.name: field.type_code for field in schema.fields} != {field: code for field, code, _ in spec.fields}:
                    raise ValueError("Schema fields differ from the verified native contract")
                self.schemas[name] = schema
            except (OSError, ValueError) as error:
                self.issues[name] = str(error)
        try:
            table = parse_table(self._read_asset("RankUp.atb"), ParseLimits(max_rows=10000))
            rows = table["rows"]
            if not rows or any(type(row.get("CharismaMax")) is not int or row["CharismaMax"] < 0 or type(row.get("NextExp")) is not int or row["NextExp"] <= 0 for row in rows):
                raise ValueError("RankUp.atb has invalid rank thresholds")
            self.rank_rows = rows
            self._check_rank(self.rules.rank, self.rules.experience)
            if self.rules.charisma is not None and self.rules.charisma > rows[self.rules.rank - 1]["CharismaMax"]:
                raise ValueError("Initial charisma exceeds the native rank maximum")
        except (OSError, ValueError, ProtocolError) as error:
            self.issues["ranks"] = str(error)

    def _read_asset(self, name: str) -> bytes:
        path = resolve_asset(self.asset_root, name)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Bootstrap asset exceeds its size limit")
        data = path.read_bytes()
        return decompress_all4(data, ParseLimits(max_input_bytes=4 * 1024 * 1024, max_decompressed_bytes=4 * 1024 * 1024)) if data.startswith(b"ALL4") else data

    def _check_rank(self, rank: int, experience: int) -> None:
        if type(rank) is not int or type(experience) is not int or not self.rank_rows or not 1 <= rank <= len(self.rank_rows) or experience < 0:
            raise _unavailable("invalid_player_rank", "Player rank or native rank data is unavailable")
        threshold = sum(row["NextExp"] for row in self.rank_rows[:rank - 1])
        if not threshold <= experience < threshold + self.rank_rows[rank - 1]["NextExp"]:
            raise _unavailable("invalid_player_rank", "Player rank and experience disagree with RankUp.atb")

    def initial_state(self) -> dict:
        current = self.clock()
        created = business_calendar.wall_text(current)
        tables = {}
        for name, schema in self.schemas.items():
            row = {field.name: ("" if field.type_code in (9, 33) else False if field.type_code == 5 else 0.0 if field.type_code == 8 else 0) for field in schema.fields}
            tables[name] = [] if name in MULTI_ROW_TABLES else [row]
        tables["presents"] = []
        tables.update({name: [] for name in COLLECTION_TABLES})
        if "game_param" in tables and "ranks" not in self.issues:
            charisma = self.rules.charisma
            tables["game_param"][0].update(
                Money=self.rules.money, Experience=self.rules.experience, Rank=self.rules.rank,
                NowCharisma=self.rank_rows[self.rules.rank - 1]["CharismaMax"] if charisma is None else charisma,
                NowActionPoint=self.rules.action_points,
            )
        elif "game_param" in tables:
            del tables["game_param"]
        if "game_param2" in tables:
            tables["game_param2"][0].update(_EvCmb=1, GameStartDateTime=created)
        if "config" in tables:
            tables["config"][0].update(UserName=self.rules.user_name, UseBgm=True, UseSe=True)
        state = {"bootstrap_schema": 1, "startup_collections_schema": 1, "created_local": created,
                 business_calendar.MARKER_KEY: business_calendar.new_marker(), "tables": tables}
        if "game_param" in tables:
            state = mark_new(state, current)
            if self.login_reward_seed_provider is not None:
                state = self.login_reward_seed_provider(state, current)
            if self.rank_reward_seed_provider is not None:
                state = self.rank_reward_seed_provider(state, current)
        return state

    def status(self) -> dict:
        rules = asdict(self.rules)
        rules.pop("bootstrap_token")
        return {
            "schemas": {name: len(schema.fields) for name, schema in self.schemas.items()},
            "unavailable": dict(self.issues),
            "energy_clock": {"encoding": ENCODING, "utc_offset_minutes": UTC_OFFSET_MINUTES,
                             "naive_clock_input": "JST server wall clock", "legacy_state": "Explicit migration required"},
            "business_calendar": business_calendar.status(),
            "local_rules": {**rules, "official_defaults": False, "rank_source": "RankUp.atb cumulative NextExp", "tutorial_flags": 0, "initial_presents": 0, "initial_crystals": 0, "event_conversion_current": True,
                            "legacy_event_conversion": "Request 33 saves the LookHarlemFlag conversion the client applies when 33 fails", "login_variants_share_player": True, "inactive_datetime": "empty state string -> DT numeric text 0", "date_storage": "native day count", "time_storage": "float32 fraction of day", "game_info": dict(GAME_INFO), "quest_records": "Uncompleted zero-flag rows for verified QuestTable IDs only"},
            "implemented_operations": [21, 22, 31, 33, 34, 35, 36, 38, 42, 43, 44, 51, 114, 129, 137, 142, 152, "heartbeat", "game_info", "quest_record", "session_flags"],
            "title_configuration": {"remote_overrides": False, "policy": "Use the native built-in title resources and link defaults"},
            "delegated_operations": ["master_cards", "master_quests"],
            "provider_configured": self.master_provider is not None,
            "rank_reward_seed_provider_configured": self.rank_reward_seed_provider is not None,
            "possession_provider_configured": self.possession_provider is not None,
            "filter_provider_configured": self.filter_provider is not None,
            "card_status_provider_configured": self.card_status_provider is not None,
            "present_provider_configured": self.present_provider is not None,
            "peruse_provider_configured": self.peruse_provider is not None,
            "exchange_history_provider_configured": self.exchange_history_provider is not None,
            "event_pass_provider_configured": self.event_pass_provider is not None,
            "vote_provider_configured": self.vote_provider is not None,
            "asset_base_url": self.asset_base_url,
            "asset_version": self.asset_version,
            "collection_scope": "Persisted possessions, filters, perused events, exchange history, universal flags, event pass status and vote history through verified providers; other collections remain empty",
            "startup_ready": not self.issues and self.master_provider is not None and self.asset_version is not None,
            "startup_scope": "Seeded local account with persisted possession counts and separately validated public master data",
            "startup_blockers": [*sorted(self.issues), *([] if self.master_provider is not None else ["master_provider"]), *([] if self.asset_version is not None else ["asset_version"])],
            "startup_diagnostics": self.startup_diagnostics.status(),
            "unimplemented_operations": [],
        }

    def handlers(self) -> dict[int | str, Callable[[RequestEnvelope, Store], bytes]]:
        save_configuration = self.save_configuration
        startup = {
            31: self.method_key, 114: self.login, 142: self.title_configuration, 152: self.platform_login, 33: self.game_param2_fields,
            34: self.game_param, 35: self.game_param2, 36: self.config,
            38: self.reproduction, 42: self.presents,
            43: self.paid_crystals, 44: self.free_crystals,
            "master_cards": self.delegated, "master_quests": self.delegated,
            51: self.card_status, 129: self.initialize_player, 137: self.configuration_database,
            "heartbeat": self.heartbeat, "game_info": self.game_info, "quest_record": self.quest_record,
            "session_flags": self.session_flags,
        }
        return {21: save_configuration, 22: save_configuration,
                **{operation: self.startup_diagnostics.wrap(operation, handler) for operation, handler in startup.items()}}

    def table_rows(self, state: dict, table: str) -> list[dict]:
        # Validated rows hold only scalars, so per-row copies fully detach the result from the state.
        return [dict(row) for row in self._table_rows(state, table, single=table not in MULTI_ROW_TABLES)]

    def player_row(self, state: dict) -> dict:
        row = self._loadable_player_row(state)
        self._check_player_consistency(state, row)
        return row

    def system_container(self, profile: PlayerProfile) -> Container:
        return self._system(profile)

    def possessions_container(self, profile: PlayerProfile) -> Container:
        return self._possessions(profile)

    def configuration_container(self, state: dict) -> Container:
        return Container(children=self._startup_collections(state))

    @staticmethod
    def _field(envelope: RequestEnvelope, name: str) -> str:
        if envelope.profile.name == "session_flags":
            nodes = [envelope.document] if envelope.document.tag == name else []
        else:
            nodes = envelope.document.findall(name)
        if len(nodes) != 1:
            raise ProtocolError("invalid_identity", f"Expected one {name} field")
        node = nodes[0]
        if (node.tail or "").strip():
            raise ProtocolError("invalid_identity", f"Unexpected text after {name}")
        if envelope.profile.name in ("quest_record", "session_flags"):
            if node.attrib or len(node):
                raise ProtocolError("invalid_identity", f"Expected one scalar {name}")
            return node.text or ""
        if node.attrib != {"T": "S"} or len(node) != 1 or (node.text or "").strip():
            raise ProtocolError("invalid_identity", f"Expected one string value in {name}")
        value = node[0]
        if value.tag != "V" or value.attrib or len(value) or (value.tail or "").strip():
            raise ProtocolError("invalid_identity", f"Expected one string value in {name}")
        return value.text or ""

    def _authenticate(self, envelope: RequestEnvelope, store: Store, *, require_method_key: bool = True) -> PlayerProfile:
        profile = store.profile()
        user = self._field(envelope, "UI")
        token = self._field(envelope, "UK")
        if user != profile.player_id or not secrets.compare_digest(token.encode("utf-8"), profile.user_key.encode("utf-8")):
            raise ProtocolError("invalid_identity", "Request does not match the local account", 403)
        if require_method_key or envelope.document.findall("MK"):
            method_key = self._field(envelope, "MK")
            if (require_method_key or method_key) and not secrets.compare_digest(method_key.encode("utf-8"), profile.method_key.encode("utf-8")):
                raise ProtocolError("invalid_method_key", "Request method key does not match the local account", 403)
        return profile

    @staticmethod
    def _system(profile: PlayerProfile) -> Container:
        return _key_values("SYSTEM", (("ExchangeKey", profile.method_key), ("PaymentTransaction", "0")))

    @staticmethod
    def _state_tables(state: dict) -> dict:
        if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict):
            raise _unavailable("bootstrap_state_unavailable", "Persisted bootstrap state is missing or unsupported")
        return state["tables"]

    def _rows(self, profile: PlayerProfile, table: str, *, single: bool = True) -> list[dict]:
        return self._table_rows(profile.state, table, single=single)

    def _table_rows(self, state: dict, table: str, *, single: bool) -> list[dict]:
        if table not in self.schemas:
            raise _unavailable("bootstrap_schema_unavailable", f"Verified schema is unavailable for {table}")
        rows = self._state_tables(state).get(table)
        limit = MAX_QUEST_RECORDS if table == "quest_records" else 4096
        if not isinstance(rows, list) or (single and len(rows) != 1) or len(rows) > limit:
            raise _unavailable("bootstrap_state_unavailable", f"Expected persisted local rows for {table}")
        expected = {field.name for field in self.schemas[table].fields}
        for row in rows:
            if not isinstance(row, dict) or set(row) != expected:
                raise _unavailable("bootstrap_state_unavailable", f"Persisted {table} fields do not match its schema")
            for field in self.schemas[table].fields:
                value = row[field.name]
                if field.type_code == 33 and (not isinstance(value, str) or len(value.encode("utf-8")) >= field.size):
                    raise _unavailable("invalid_bootstrap_value", f"String exceeds native field {field.name}")
                if field.type_code == 7 and (not int32(value) or value < 0):
                    raise _unavailable("invalid_bootstrap_value", f"Invalid native date in {field.name}")
                if field.type_code == 8 and (type(value) not in (int, float) or not 0 <= value < 1):
                    raise _unavailable("invalid_bootstrap_value", f"Invalid native time in {field.name}")
                if field.type_code == 9 and value != "":
                    try:
                        parsed = datetime.strptime(value, business_calendar.NATIVE_DATETIME)
                        if parsed.strftime(business_calendar.NATIVE_DATETIME) != value:
                            raise ValueError("Noncanonical datetime")
                    except (TypeError, ValueError) as error:
                        raise _unavailable("invalid_bootstrap_value", f"Invalid native datetime in {field.name}") from error
        try:
            if table == "quest_records":
                dumps(Container(children=self.columns(table, rows)), limits=QUEST_LIMITS)
            else:
                dumps(Container(children=self.columns(table, rows)))
        except (AlXmlError, TypeError, ValueError) as error:
            raise _unavailable("invalid_bootstrap_value", f"Persisted {table} has an invalid typed value") from error
        return rows

    @staticmethod
    def columns(table: str, rows: list[dict]) -> tuple[Column, ...]:
        # Native DT parsing dereferences the text node, so zero needs text rather than an empty V element.
        return tuple(Column(wire, WIRE_TYPES[code], tuple("0" if code == 9 and row[name] == "" else row[name] for row in rows)) for name, code, wire in TABLES[table].fields)

    def _player_row(self, profile: PlayerProfile) -> dict:
        return self.player_row(profile.state)

    def _loadable_player_row(self, state: dict) -> dict:
        row = self._table_rows(state, "game_param", single=True)[0]
        if row["Money"] == row["Experience"] == 0:
            raise _unavailable("invalid_player_state", "The native GameParam loader treats zero money and experience as a new account")
        return row

    def _check_player_consistency(self, state: dict, row: dict) -> None:
        self._check_rank(row["Rank"], row["Experience"])
        if row["Money"] < 0 or row["NowCharisma"] < 0 or row["NowActionPoint"] < 0:
            raise _unavailable("invalid_player_state", "Player progression is not a valid native GameParam state")
        validate_clock(state)

    def _collection_profile(self, envelope: RequestEnvelope, store: Store, *, require_method_key: bool = True) -> PlayerProfile:
        profile = self._authenticate(envelope, store, require_method_key=require_method_key)
        for _ in range(3):
            tables = self._state_tables(profile.state)
            self._player_row(profile)
            version = profile.state.get("startup_collections_schema")
            if version is not None:
                if type(version) is not int or version != 1:
                    raise _unavailable("collection_schema_unavailable", "Unsupported persisted startup collection version")
                return profile
            updated = {**profile.state, "startup_collections_schema": 1,
                       "tables": {**{name: [] for name in COLLECTION_TABLES}, **tables}}
            try:
                return store.update_state(updated, profile.revision)
            except ValueError:
                profile = store.profile()
        raise ProtocolError("profile_changed", "Profile changed while adding local startup collections", 409)

    def _empty_collections(self, state: dict, names: tuple[str, ...]) -> None:
        tables = self._state_tables(state)
        for name in names:
            if tables.get(name) != []:
                raise _unavailable("collection_rows_unavailable", f"Verified nonempty content is unavailable for {name}")

    def card_status(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self._collection_profile(envelope, store)
        if self.card_status_provider is None:
            self._empty_collections(profile.state, ("card_status",))
            return dumps(Container(children=(Column("CardID", ValueType.INT, ()),)))
        result = self.card_status_provider(profile.state)
        if not isinstance(result, Container) or result.name != "CardStatus" or result.index is not None:
            raise _unavailable("card_status_provider_invalid", "Card-status provider must return the native CardStatus container")
        return dumps(Container(children=result.children), limits=CARD_STATUS_LIMITS)

    def _possessions(self, profile: PlayerProfile) -> Container:
        return self._startup_possessions(profile.state)

    def _startup_possessions(self, state: dict) -> Container:
        if self.possession_provider is None:
            self._empty_collections(state, ("possessions",))
            return Container("POSSESSION")
        result = self.possession_provider(state)
        if not isinstance(result, Container) or result.name != "POSSESSION" or result.index is not None:
            raise _unavailable("possession_provider_invalid", "Possession provider must return the native POSSESSION container")
        return result

    def initialize_player(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self._collection_profile(envelope, store)
        return dumps(Container(children=(self._system(profile), self._possessions(profile))))

    def configuration_database(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self._collection_profile(envelope, store)
        return self._configuration_response(profile)

    def _configuration_response(self, profile: PlayerProfile) -> bytes:
        collections = self._startup_collections(profile.state)
        return dumps(Container(children=(self._system(profile), *collections)), limits=STARTUP_COLLECTION_LIMITS)

    def _startup_collections(self, state: dict) -> tuple[Container, ...]:
        self._empty_collections(state, tuple(name for name in STARTUP_COLLECTIONS.values()
                                            if name not in ("possessions", "filter_settings", "perused_events", "exchange_history", "universal_flags", "fame_depository", "backgrounds", "event_pass_status", "vote_history")))
        supplied = {"POSSESSION": self._startup_possessions(state)}
        if self.filter_provider is None:
            self._empty_collections(state, ("filter_settings",))
            supplied["PlayerFilterParam"] = Container("PlayerFilterParam")
        else:
            result = self.filter_provider(state)
            if not isinstance(result, Container) or result.name != "PlayerFilterParam" or result.index is not None:
                raise _unavailable("filter_provider_invalid", "Filter provider must return the native PlayerFilterParam container")
            supplied["PlayerFilterParam"] = result
        if self.peruse_provider is None:
            self._empty_collections(state, ("perused_events",))
            supplied["PEV"] = Container("PEV")
        else:
            result = self.peruse_provider(state)
            if not isinstance(result, Container) or result.name != "PEV" or result.index is not None:
                raise _unavailable("peruse_provider_invalid", "Peruse provider must return the native PEV container")
            supplied["PEV"] = result
        if self.exchange_history_provider is None:
            self._empty_collections(state, ("exchange_history",))
            supplied["ExchangeHistory"] = Container("ExchangeHistory")
        else:
            result = self.exchange_history_provider(state)
            if not isinstance(result, Container) or result.name != "ExchangeHistory" or result.index is not None:
                raise _unavailable("exchange_history_provider_invalid", "Exchange-history provider must return ExchangeHistory")
            supplied["ExchangeHistory"] = result
        if self.universal_flag_provider is None:
            self._empty_collections(state, ("universal_flags",))
            supplied["GF"] = Container("GF")
        else:
            result = self.universal_flag_provider(state)
            if not isinstance(result, Container) or result.name != "GF" or result.index is not None:
                raise _unavailable("universal_flag_provider_invalid", "Universal-flag provider must return GF")
            supplied["GF"] = result
        if self.fame_provider is None:
            self._empty_collections(state, ("fame_depository",))
            supplied["FameDepository"] = Container("FameDepository")
        else:
            result = self.fame_provider(state)
            if not isinstance(result, Container) or result.name != "FameDepository" or result.index is not None:
                raise _unavailable("fame_provider_invalid", "Fame-depository provider must return FameDepository")
            supplied["FameDepository"] = result
        if self.background_provider is None:
            self._empty_collections(state, ("backgrounds",))
            supplied["PRM"] = Container("PRM")
        else:
            result = self.background_provider(state)
            if not isinstance(result, Container) or result.name != "PRM" or result.index is not None:
                raise _unavailable("background_provider_invalid", "Background provider must return PRM")
            supplied["PRM"] = result
        if self.event_pass_provider is None:
            self._empty_collections(state, ("event_pass_status",))
            supplied["EventPassStatus"] = Container("EventPassStatus")
        else:
            result = self.event_pass_provider(state)
            if not isinstance(result, Container) or result.name != "EventPassStatus" or result.index is not None:
                raise _unavailable("event_pass_provider_invalid", "Event-pass provider must return EventPassStatus")
            supplied["EventPassStatus"] = result
        if self.vote_provider is None:
            self._empty_collections(state, ("vote_history",))
            supplied["PVH"] = Container("PVH")
        else:
            result = self.vote_provider(state)
            if not isinstance(result, Container) or result.name != "PVH" or result.index is not None:
                raise _unavailable("vote_provider_invalid", "Vote provider must return PVH")
            supplied["PVH"] = result
        return tuple(supplied.get(name, Container(name)) for name in STARTUP_COLLECTIONS)

    @staticmethod
    def _game_info_columns() -> tuple[Column, ...]:
        return (Column("KEY", ValueType.STRING, tuple(key for key, _ in GAME_INFO)),
                Column("VALUE", ValueType.INT, tuple(value for _, value in GAME_INFO)))

    def game_info(self, envelope: RequestEnvelope, store: Store) -> bytes:
        self._field(envelope, "Platform")
        return dumps(Container(children=self._game_info_columns()))

    def title_configuration(self, envelope: RequestEnvelope, store: Store) -> bytes:
        try:
            document = loads(envelope.payload_xml())
        except AlXmlError as error:
            raise ProtocolError("invalid_title_request", "Invalid title configuration request") from error
        fields = {"CT": ValueType.INT, "PlatformCode": ValueType.INT,
                  **{name: ValueType.STRING for name in ("PFN", "UI", "UK", "MK", "GPVI", "GPOT")}}
        for name, kind in fields.items():
            columns = [child for child in document.children if child.name == name]
            if (len(columns) != 1 or not isinstance(columns[0], Column) or columns[0].type is not kind
                    or len(columns[0].values) != 1):
                raise ProtocolError("invalid_title_request", f"Expected one typed {name} value")
            if kind is ValueType.STRING and len(columns[0].values[0].encode("utf-8")) > 256:
                raise ProtocolError("invalid_title_request", f"Title request {name} exceeds its limit")
        return dumps(Container(children=(self._system(store.profile()), Container("CONFIG"))))

    def heartbeat(self, envelope: RequestEnvelope, store: Store) -> bytes:
        if self.asset_version is None:
            raise _unavailable("asset_version_unavailable", "Import the current public resource index before native startup")
        profile = self._collection_profile(envelope, store, require_method_key=False)
        self._empty_collections(profile.state, ("menu_blocks",))
        return dumps(Container(children=(
            Container("AssetPath", (Column("A1", ValueType.STRING, (self.asset_version,)),)),
            Container("GameInfo", self._game_info_columns()), self._system(profile), Container("MBL"),
        )))

    def quest_record_rows(self, state: dict) -> tuple[list[dict], list[dict]]:
        raw, master = self.public_master("QuestTable")
        identifiers = next(column.values for column in master.children if column.name == "QuestID")
        identifier_set = set(identifiers)
        rows = self.table_rows(state, "quest_records")
        stored = {row["QuestID"] for row in rows}
        if len(stored) != len(rows) or not stored <= identifier_set | retained_quest_ids(raw):
            raise _unavailable("quest_record_master_mismatch", "Stored quest records do not match the verified master IDs")
        if any(row[field] < 0 for row in rows for field in QUEST_RECORD_COUNTERS):
            raise _unavailable("invalid_quest_record", "Quest record values cannot be negative")
        additions = [{"QuestID": quest_id, **UNPLAYED_QUEST_RECORD} for quest_id in identifiers if quest_id not in stored]
        visible = [row for row in rows if row["QuestID"] in identifier_set]
        return [*visible, *additions], additions

    def quest_record(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self._collection_profile(envelope, store, require_method_key=False)
        for _ in range(3):
            visible, additions = self.quest_record_rows(profile.state)
            response = dumps(Container(children=self.columns("quest_records", visible)), limits=QUEST_LIMITS)
            if not additions:
                return response
            rows = self._rows(profile, "quest_records", single=False)
            updated = {**profile.state, "tables": {**self._state_tables(profile.state), "quest_records": [*rows, *additions]}}
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                profile = store.profile()
        raise ProtocolError("profile_changed", "Profile changed while initializing verified quest records", 409)

    def method_key(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self._authenticate(envelope, store, require_method_key=False)
        return dumps(Container(children=(self._system(profile),)))

    def platform_identity(self, envelope: RequestEnvelope, store: Store, *, keeps_game_identity: bool = False) -> PlayerProfile:
        profile = store.profile()
        viewer = self._field(envelope, "VI")
        token = self._field(envelope, "OT")
        if viewer != profile.player_id or not secrets.compare_digest(token.encode("utf-8"), self.rules.bootstrap_token.encode("utf-8")):
            raise ProtocolError("invalid_platform_identity", "Platform request does not match the local launch identity", 403)
        # Native A64B50 strips UI/UK/MK from 152 alone; 153 keeps them from game start, still empty before 642E60 copies UI/UK.
        if not keeps_game_identity and any(envelope.document.findall(name) for name in ("UI", "UK", "MK")):
            raise ProtocolError("invalid_platform_identity", "Platform exchange must precede the game identity fields")
        for name, expected in (("GPVI", viewer), ("GPOT", token)):
            if envelope.document.findall(name):
                value = self._field(envelope, name)
                # The common serializer writes "unkown" before a platform context exists.
                if value != "unkown" and not secrets.compare_digest(value.encode("utf-8"), expected.encode("utf-8")):
                    raise ProtocolError("invalid_platform_identity", "Platform identity aliases disagree", 403)
        return profile

    def platform_login(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self.platform_identity(envelope, store)
        user_name = self._rows(profile, "config")[0]["UserName"]
        return dumps(Container(children=(Container("LI", (
            Column("UK", ValueType.STRING, (profile.user_key,)),
            Column("UI", ValueType.STRING, (profile.player_id,)),
            Column("NN", ValueType.STRING, (user_name,)),
        )),)))

    def session_flags(self, envelope: RequestEnvelope, store: Store) -> bytes:
        if self._field(envelope, "UI") != store.profile().player_id:
            raise ProtocolError("invalid_identity", "Session flags do not match the local account", 403)
        return b"0,0"

    def login(self, envelope: RequestEnvelope, store: Store) -> bytes:
        now = self.clock() if self.missions is not None or self.comeback_logins is not None else None
        profile = self._authenticate(envelope, store)
        for _ in range(3):
            response = self._login_response(envelope, profile)
            try:
                updated = self._login_records(profile, now)
            except ProtocolError as error:
                # The client sends 114 once and ignores the result, so an unrecorded login still returns the stored values.
                self.startup_diagnostics.degrade(error)
                return response
            if updated == profile.state:
                return response
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                profile = self._authenticate(envelope, store)
        self.startup_diagnostics.degrade(ProtocolError("profile_changed", "Player state changed while recording login", 409))
        return self._login_response(envelope, profile)

    def _login_response(self, envelope: RequestEnvelope, profile: PlayerProfile) -> bytes:
        row = self._loadable_player_row(profile.state)
        try:
            player_id = int(profile.player_id)
        except ValueError as error:
            raise _unavailable("invalid_player_id", "The local login requires an integer player ID") from error
        values = (1, player_id, LOGIN_IS_SUCCEEDED, row["Rank"], row["Rank"], player_id, profile.user_key, 0, 0,
                  profile.player_id, 0, 0, 0)
        login = Container("LGI", tuple(Column(f"A{index + 1}", ValueType.STRING if index in (6, 9) else ValueType.INT, (value,)) for index, value in enumerate(values)))
        response = dumps(Container(children=(self._system(profile), login)))
        encode_response(envelope.profile, response)
        return response

    def _login_records(self, profile: PlayerProfile, now: datetime | None) -> dict:
        self._player_row(profile)
        updated = self.comeback_logins.observe(profile.state, now) if self.comeback_logins is not None else profile.state
        updated = self.missions.record(updated, "login", {}, now) if self.missions is not None else updated
        if updated != profile.state and self.comeback_logins is not None:
            self._require_game_param2_projections(replace(profile, state=updated))
        return updated

    def game_param(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self._monthly_profile(envelope, store)
        row = self._loadable_player_row(profile.state)
        try:
            self._check_player_consistency(profile.state, row)
        except ProtocolError as error:
            # Refusing a loadable row would loop the native client on an error popup; writes keep enforcing this check.
            self.startup_diagnostics.degrade(error)
        return dumps(Container(children=self.columns("game_param", [row])))

    def _table_response(self, envelope: RequestEnvelope, store: Store, table: str) -> bytes:
        profile = self._authenticate(envelope, store)
        return dumps(Container(children=self.columns(table, self._rows(profile, table))))

    def game_param2(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self._monthly_profile(envelope, store)
        return dumps(Container(children=self.columns("game_param2", self._rows(profile, "game_param2"))))

    def _monthly_profile(self, envelope: RequestEnvelope, store: Store) -> PlayerProfile:
        profile = self._authenticate(envelope, store)
        if self.monthly_state_provider is None:
            return profile
        try:
            return self._prepare_month(envelope, store, profile)
        except ProtocolError as error:
            # The saved state stays authoritative: these loads serve it unsettled rather than loop the client on an error popup.
            self.startup_diagnostics.degrade(error)
            return self._authenticate(envelope, store)

    def _prepare_month(self, envelope: RequestEnvelope, store: Store, profile: PlayerProfile) -> PlayerProfile:
        now = self.clock()
        for _ in range(3):
            updated = self.monthly_state_provider(profile.state, now)
            if updated == profile.state:
                return profile
            candidate = replace(profile, state=updated)
            self._require_game_param2_projections(candidate)
            encode_response(self._registry.by_registration[34], dumps(Container(children=self.columns(
                "game_param", [self._player_row(candidate)]))))
            try:
                return store.update_state(updated, profile.revision)
            except ValueError:
                profile = self._authenticate(envelope, store)
        raise ProtocolError("profile_changed", "Monthly state changed while loading game parameters", 409)

    @cached_property
    def _registry(self) -> ProtocolRegistry:
        return ProtocolRegistry.load()

    def _require_game_param2_projections(self, candidate: PlayerProfile) -> None:
        profiles = self._registry.by_registration
        encode_response(profiles[35], dumps(Container(children=self.columns("game_param2", self._rows(candidate, "game_param2")))))
        encode_response(profiles[33], self.game_param2_fields_response(candidate))

    def config(self, envelope: RequestEnvelope, store: Store) -> bytes:
        return self._table_response(envelope, store, "config")

    @staticmethod
    def _configuration_updates(envelope: RequestEnvelope) -> dict:
        if envelope.document.tag != "DA" or envelope.document.attrib or (envelope.document.text or "").strip():
            raise ProtocolError("invalid_configuration_request", "Expected a configuration request container")
        nodes = [node for node in envelope.document if node.tag not in COMMON_FIELDS]
        values = _RequestIntegers(nodes)
        if len(values) != len(nodes):
            raise _option_group_error()
        return configuration_updates(values)

    def save_configuration(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self._authenticate(envelope, store)
        updates = self._configuration_updates(envelope)
        for _ in range(3):
            self._rows(profile, "config")
            updated = patch_configuration(profile.state, updates)
            response = dumps(Container(children=(self._system(profile), configuration_delta(
                configuration_row(updated), CONFIGURATION_RESPONSE_FIELDS,
            ))))
            encode_response(envelope.profile, response)
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                profile = store.profile()
        raise ProtocolError("profile_changed", "Configuration changed while saving", 409)

    def reproduction(self, envelope: RequestEnvelope, store: Store) -> bytes:
        return self._table_response(envelope, store, "reproduction")

    def game_param2_fields(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self._monthly_profile(envelope, store)
        return self.game_param2_fields_response(self._convert_legacy_event_flags(envelope, store, profile))

    def game_param2_fields_response(self, profile: PlayerProfile) -> bytes:
        row = self._rows(profile, "game_param2")[0]
        values = tuple((wire, str(row[field])) for wire, field in PGP2_FIELDS)
        return dumps(Container(children=(self._system(profile), _key_values("PGP2", values))))

    def _convert_legacy_event_flags(self, envelope: RequestEnvelope, store: Store, profile: PlayerProfile) -> PlayerProfile:
        for _ in range(3):
            row = self._rows(profile, "game_param2")[0]
            if row["_EvCmb"] & EVENT_FLAGS_CONVERTED:
                return profile
            updated = {**profile.state, "tables": {**self._state_tables(profile.state), "game_param2": [self._converted_event_flags(row)]}}
            self._require_game_param2_projections(replace(profile, state=updated))
            try:
                return store.update_state(updated, profile.revision)
            except ValueError:
                profile = self._authenticate(envelope, store)
        raise ProtocolError("profile_changed", "Player state changed while converting legacy event flags", 409)

    def _converted_event_flags(self, row: dict) -> dict:
        # Native fixed-width strings keep only the bytes that fit before their terminator.
        capacity = {field.name: field.size - 1 for field in self.schemas["game_param2"].fields if field.type_code == 33}

        def extended(name: str, suffix: str) -> str:
            return (row[name] + suffix).encode("utf-8")[:capacity[name]].decode("utf-8", "ignore")

        converted = dict(row)
        if not row["WatchEventFlag0"]:
            for kind in (0, 1):
                name = f"WatchEventFlag{kind}"
                converted[name] = extended(name, _legacy_watch_flags(row, kind) + UNWATCHED * WATCH_FLAG_PADDING)
        if not row["WatchEventFlag0_2"]:
            for name in ("WatchEventFlag0_2", "WatchEventFlag1_2"):
                converted[name] = extended(name, UNWATCHED * SECOND_WATCH_FLAG_PADDING)
        converted["_EvCmb"] = row["_EvCmb"] | EVENT_FLAGS_CONVERTED
        return converted

    def presents(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self._authenticate(envelope, store)
        if self.present_provider is not None:
            result = self.present_provider(profile.state)
            if not isinstance(result, Container) or result.name != "PPT" or result.index is not None:
                raise _unavailable("present_provider_invalid", "Present provider must return the native PPT container")
            return dumps(Container(children=(self._system(profile), result)))
        rows = self._state_tables(profile.state).get("presents")
        if rows != []:
            raise _unavailable("present_rows_unavailable", "Only the verified empty local present inventory is available")
        return dumps(Container(children=(self._system(profile), Container("PPT"))))

    def _crystals(self, envelope: RequestEnvelope, store: Store, free: bool) -> bytes:
        profile = self._authenticate(envelope, store)
        rows = [row for row in self._rows(profile, "crystals", single=False) if row["FreeFlag"] is free]
        if any(row["Num"] < 0 or row["Date"] < 0 for row in rows):
            raise _unavailable("invalid_crystal_state", "Crystal amounts and raw date values must be nonnegative")
        return dumps(Container(children=self.columns("crystals", rows)))

    def paid_crystals(self, envelope: RequestEnvelope, store: Store) -> bytes:
        return self._crystals(envelope, store, False)

    def free_crystals(self, envelope: RequestEnvelope, store: Store) -> bytes:
        return self._crystals(envelope, store, True)

    def public_master(self, name: str) -> tuple[bytes, Container]:
        if self.master_provider is None:
            raise _unavailable("native_response_unavailable", f"No captured or verified response is configured for {name}")
        try:
            data = self.master_provider(name)
            if not isinstance(data, bytes):
                raise ValueError("Response provider must return bytes")
            parse_document(data, max_bytes=MASTER_LIMITS.max_bytes, max_nodes=MASTER_LIMITS.max_elements)
            document, _ = validate_public_master(name, data, self.master_schemas)
            return data, document
        except (OSError, KeyError, ValueError) as error:
            raise _unavailable("native_response_unavailable", f"Verified response is unavailable for {name}") from error

    def delegated(self, envelope: RequestEnvelope, store: Store) -> bytes:
        names = {"master_cards": "CardData", "master_quests": "QuestTable"}
        name = names.get(envelope.profile.name)
        if name is None:
            raise UnsupportedOperation("No verified direct-response provider contract")
        data, _ = self.public_master(name)
        return data


from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
from importlib import import_module
import json
import math
from pathlib import Path
import re
from types import MappingProxyType

from .errors import ProtocolError
from .local_io import MAX_JSON_DEPTH, checked_path
from .storage import serialize


CLI_RULE_LIMIT = "cli:MAX_RULE_BYTES"
_KEY = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_FLAG = re.compile(r"--[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_PS_PARAMETER = re.compile(r"[A-Z][A-Za-z0-9]*\Z")
_REFERENCE = re.compile(r"[a-z_][a-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*\Z")
_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_EXAMPLE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.example\.json\Z")
_MISSING = object()


class TimeSemantics(StrEnum):
    NEW_SAVE = "new_save"
    FIRST_BASELINE = "first_baseline"
    FROZEN_PER_OPERATION = "frozen_per_operation"
    TIME_ACCUMULATING = "time_accumulating"
    ELIGIBILITY = "eligibility"
    RESOURCE = "resource"


class Normalizer(StrEnum):
    TO_DICT = "to_dict"
    ASDICT = "asdict"


class ConfigError(ProtocolError):
    def __init__(self, code: str, message: str, status: int = 400, *, key: str | None = None,
                 pointer: str | None = None):
        super().__init__(code, message, status)
        self.key = key
        self.pointer = pointer


class _Members(tuple):
    pass


def resolve_reference(reference: str):
    module, name = reference.split(":")
    return getattr(import_module(f".{module}", __package__), name)


def document_name(key: str) -> str:
    return f"{key}.json"


def checked_config_path(path: Path, root: Path | None = None) -> Path:
    try:
        return checked_path(path, root)
    except ValueError as error:
        raise ConfigError("config_path_rejected", str(error), 503) from error


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _child(pointer: str, name) -> str:
    return pointer + "/" + str(name).replace("~", "~0").replace("/", "~1")


def _where(pointer: str) -> str:
    return pointer or "the document root"


def _text(value: str, pointer: str, key: str | None) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ConfigError("config_encoding_invalid", f"Text at {_where(pointer)} is not valid Unicode",
                          key=key, pointer=pointer) from error
    return value


def _materialize(value, pointer: str, depth: int, key: str | None):
    if isinstance(value, (Mapping, list, tuple)) and depth > MAX_JSON_DEPTH:
        raise ConfigError("config_nesting_too_deep", f"JSON nesting exceeds {MAX_JSON_DEPTH} levels at {_where(pointer)}",
                          key=key, pointer=pointer)
    if isinstance(value, _Members):
        result = {}
        for name, item in value:
            child = _child(pointer, name)
            if name in result:
                raise ConfigError("config_duplicate_key", f"Duplicate JSON key at {child}", key=key, pointer=child)
            result[_text(name, child, key)] = _materialize(item, child, depth + 1, key)
        return result
    if isinstance(value, Mapping):
        result = {}
        for name, item in value.items():
            child = _child(pointer, name)
            if type(name) is not str:
                raise ConfigError("config_document_invalid", f"JSON object keys must be text at {child}",
                                  key=key, pointer=child)
            result[_text(name, child, key)] = _materialize(item, child, depth + 1, key)
        return result
    if isinstance(value, (list, tuple)):
        return [_materialize(item, _child(pointer, index), depth + 1, key) for index, item in enumerate(value)]
    if isinstance(value, str):
        return _text(value, pointer, key)
    if value is None or type(value) in (bool, int) or type(value) is float and math.isfinite(value):
        return value
    if type(value) is float:
        raise ConfigError("config_number_invalid", f"Number at {_where(pointer)} is not finite", key=key, pointer=pointer)
    raise ConfigError("config_document_invalid", f"Unsupported JSON value at {_where(pointer)}", key=key, pointer=pointer)


def _document(value, key: str | None) -> dict:
    document = _materialize(value, "", 1, key)
    if not isinstance(document, dict):
        raise ConfigError("config_not_object", "Configuration documents must be JSON objects", key=key, pointer="")
    return document


def parse_document(raw: bytes, *, limit: int, key: str | None = None) -> dict:
    if not isinstance(raw, (bytes, bytearray)):
        raise ConfigError("config_document_invalid", "Configuration files must be read as bytes", key=key)
    if len(raw) > limit:
        raise ConfigError("config_too_large", f"Configuration documents are limited to {limit} bytes", key=key)
    try:
        text = bytes(raw).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigError("config_encoding_invalid", "Configuration files must be UTF-8", key=key) from error
    try:
        value = json.loads(text, object_pairs_hook=_Members)
    except RecursionError as error:
        raise ConfigError("config_nesting_too_deep", f"JSON nesting exceeds {MAX_JSON_DEPTH} levels", key=key) from error
    except ValueError as error:
        raise ConfigError("config_json_invalid", f"Configuration JSON is malformed: {error}", key=key) from error
    return _document(value, key)


def _plain(values: Mapping) -> dict:
    return json.loads(serialize(values))


@dataclass(frozen=True)
class StagedDocument:
    key: str
    path: Path
    data: bytes
    sha256: str
    rules: object
    normalized: dict | None
    content_version: str | None


@dataclass(frozen=True)
class ConfigKey:
    key: str
    cli_flag: str
    ps_parameter: str
    rules_class: str
    loader: str
    normalizer: Normalizer
    domains: tuple[str, ...]
    time_semantics: TimeSemantics
    size_limit: int | str
    resource_hint: bool = False
    field_semantics: Mapping[str, TimeSemantics] = field(default_factory=dict)
    secret_fields: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()

    def __post_init__(self):
        if not (_KEY.fullmatch(self.key) and _FLAG.fullmatch(self.cli_flag) and _PS_PARAMETER.fullmatch(self.ps_parameter)):
            raise ValueError("Configuration keys require canonical names and parameters")
        if not (_REFERENCE.fullmatch(self.rules_class) and _REFERENCE.fullmatch(self.loader)):
            raise ValueError("Configuration references use the module:attribute form")
        if not (type(self.size_limit) is int and self.size_limit > 0
                or isinstance(self.size_limit, str) and _REFERENCE.fullmatch(self.size_limit)):
            raise ValueError("Configuration size limits are positive integers or references")
        if not all(isinstance(value, (tuple, list)) for value in (self.domains, self.secret_fields, self.examples)):
            raise ValueError("Configuration name groups are sequences of names")
        domains = tuple(self.domains)
        if not domains or list(domains) != sorted(set(domains)) or not all(_NAME.fullmatch(name) for name in domains):
            raise ValueError("Consumer domains must be sorted distinct readiness names")
        semantics = {name: TimeSemantics(value) for name, value in dict(self.field_semantics).items()}
        names = (*semantics, *self.secret_fields)
        if type(self.resource_hint) is not bool or not all(_NAME.fullmatch(name) for name in names):
            raise ValueError("Configuration field metadata requires canonical rule field names")
        if not all(_EXAMPLE.fullmatch(name) for name in self.examples):
            raise ValueError("Configuration examples are example JSON file names")
        object.__setattr__(self, "normalizer", Normalizer(self.normalizer))
        object.__setattr__(self, "time_semantics", TimeSemantics(self.time_semantics))
        object.__setattr__(self, "domains", domains)
        object.__setattr__(self, "field_semantics", MappingProxyType(semantics))
        object.__setattr__(self, "secret_fields", tuple(self.secret_fields))
        object.__setattr__(self, "examples", tuple(self.examples))

    @property
    def dest(self) -> str:
        return self.cli_flag[2:].replace("-", "_")

    @property
    def max_bytes(self) -> int:
        return self.size_limit if type(self.size_limit) is int else resolve_reference(self.size_limit)

    def rules_type(self) -> type:
        return resolve_reference(self.rules_class)

    def load(self, path: Path | None = None):
        loader = resolve_reference(self.loader)
        try:
            return loader(None if path is None else Path(path))
        except OSError as error:
            raise ConfigError("config_file_unreadable", f"{self.key}: {error}", key=self.key) from error
        except RecursionError as error:
            # Command-line files reach the loaders without the strict pre-parse that bounds set documents.
            raise ConfigError("config_nesting_too_deep", f"{self.key}: JSON nesting exceeds {MAX_JSON_DEPTH} levels",
                              key=self.key) from error
        except (ValueError, TypeError, OverflowError) as error:
            raise ConfigError("config_rules_invalid", f"{self.key}: {error}", key=self.key) from error

    def default(self):
        return self.load(None)

    def normalize(self, rules) -> dict | None:
        if rules is None:
            return None
        values = rules.to_dict() if self.normalizer is Normalizer.TO_DICT else asdict(rules)
        for name in self.secret_fields:
            values.pop(name, None)
        return _plain(values)

    def content_version(self, rules) -> str | None:
        if rules is None:
            return None
        if not hasattr(type(rules), "content_version"):
            return _digest(serialize(self.normalize(rules)).encode("utf-8"))
        version = rules.content_version
        if not isinstance(version, str) or not version:
            raise TypeError(f"{self.key} rules must expose their content version as text")
        return version

    def semantics_for(self, name: str) -> TimeSemantics:
        return self.field_semantics.get(name, self.time_semantics)

    def changed_semantics(self, before: Mapping | None, after: Mapping | None) -> frozenset[TimeSemantics]:
        if before == after:
            return frozenset()
        old, new = before or {}, after or {}
        changed = {self.semantics_for(name) for name in old.keys() | new.keys()
                   if old.get(name, _MISSING) != new.get(name, _MISSING)}
        if (before is None) != (after is None):
            changed.add(self.time_semantics)
        return frozenset(changed)

    def canonical(self, document: bytes | Mapping) -> bytes:
        limit = self.max_bytes
        if isinstance(document, (bytes, bytearray)):
            value = parse_document(document, limit=limit, key=self.key)
        else:
            value = _document(document, self.key)
        for name in self.secret_fields:
            if name in value:
                raise ConfigError("config_secret_field", f"{name} is generated locally and cannot be configured",
                                  key=self.key, pointer=_child("", name))
        try:
            data = serialize(value).encode("utf-8")
        except ValueError as error:
            raise ConfigError("config_document_invalid", f"Configuration JSON cannot be normalized: {error}",
                              key=self.key) from error
        if len(data) > limit:
            raise ConfigError("config_too_large", f"Normalized configuration exceeds {limit} bytes", key=self.key)
        return data

    def stage(self, document: bytes | Mapping, directory: Path, *, root: Path) -> StagedDocument:
        data = self.canonical(document)
        path = checked_config_path(Path(directory) / document_name(self.key), root)
        stream = path.open("xb")
        try:
            with stream:
                stream.write(data)
            rules = self.load(path)
            return StagedDocument(self.key, path, data, _digest(data), rules, self.normalize(rules),
                                  self.content_version(rules))
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def describe(self) -> dict:
        return {"key": self.key, "cli_flag": self.cli_flag, "dest": self.dest, "ps_parameter": self.ps_parameter,
                "rules_class": self.rules_class, "loader": self.loader, "normalizer": self.normalizer,
                "domains": list(self.domains), "time_semantics": self.time_semantics,
                "field_semantics": dict(self.field_semantics), "resource_hint": self.resource_hint,
                "max_bytes": self.max_bytes, "secret_fields": list(self.secret_fields), "examples": list(self.examples)}


class ConfigRegistry(Mapping):
    def __init__(self, entries: tuple[ConfigKey, ...]):
        entries = tuple(entries)
        if not entries or not all(isinstance(entry, ConfigKey) for entry in entries):
            raise ValueError("A configuration registry requires validated keys")
        for attribute in ("key", "dest", "cli_flag", "ps_parameter"):
            values = [getattr(entry, attribute) for entry in entries]
            if len(set(values)) != len(values):
                raise ValueError(f"Configuration keys require a unique {attribute}")
        self._entries = MappingProxyType({entry.key: entry for entry in entries})

    def __getitem__(self, key: str) -> ConfigKey:
        return self._entries[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def require(self, key) -> ConfigKey:
        if type(key) is not str or key not in self._entries:
            raise ConfigError("config_key_unknown", f"Unknown configuration key: {key!r}",
                              key=key if type(key) is str else None)
        return self._entries[key]

    def describe(self) -> list[dict]:
        return [entry.describe() for entry in self.values()]


# Domains name each readiness domain whose fingerprint embeds the rule, directly or through another service's version.
REGISTRY = ConfigRegistry((
    ConfigKey("rules", "--rules", "RulesFile", rules_class="bootstrap:LocalRules", loader="cli:load_rules",
              normalizer=Normalizer.ASDICT, domains=("bootstrap",), time_semantics=TimeSemantics.NEW_SAVE,
              size_limit=CLI_RULE_LIMIT, secret_fields=("bootstrap_token",), examples=("local-rules.example.json",)),
    ConfigKey("quest-rules", "--quest-rules", "QuestRulesFile", rules_class="quest_service:QuestRules",
              loader="cli:load_quest_rules", normalizer=Normalizer.ASDICT,
              domains=("entrust", "login_rewards", "main_quests", "recovery", "sugoroku"),
              time_semantics=TimeSemantics.FROZEN_PER_OPERATION, size_limit=CLI_RULE_LIMIT,
              field_semantics={"charisma_recovery_seconds": TimeSemantics.TIME_ACCUMULATING},
              examples=("quest-rules.example.json",)),
    ConfigKey("training-rules", "--training-rules", "TrainingRulesFile", rules_class="training_catalog:TrainingRules",
              loader="cli:load_training_rules", normalizer=Normalizer.ASDICT,
              domains=("entrust", "evolution", "fame", "missions", "skin_shop", "training", "unit_detail"),
              time_semantics=TimeSemantics.FROZEN_PER_OPERATION, size_limit=CLI_RULE_LIMIT),
    ConfigKey("silver-house-rules", "--silver-house-rules", "SilverHouseRulesFile",
              rules_class="silver_house:SilverHouseRules", loader="cli:load_silver_house_rules",
              normalizer=Normalizer.ASDICT,
              domains=("entrust", "evolution", "fame", "missions", "silver_house", "skin_shop", "training",
                       "unit_detail"),
              time_semantics=TimeSemantics.FROZEN_PER_OPERATION, size_limit=CLI_RULE_LIMIT),
    ConfigKey("evolution-rules", "--evolution-rules", "EvolutionRulesFile", rules_class="evolution_catalog:EvolutionRules",
              loader="cli:load_evolution_rules", normalizer=Normalizer.ASDICT,
              domains=("entrust", "evolution", "fame", "missions", "skin_shop", "trades"),
              time_semantics=TimeSemantics.FROZEN_PER_OPERATION, size_limit=CLI_RULE_LIMIT,
              examples=("evolution-rules.example.json",)),
    ConfigKey("recovery-rules", "--recovery-rules", "RecoveryRulesFile", rules_class="recovery_service:RecoveryRules",
              loader="cli:load_recovery_rules", normalizer=Normalizer.ASDICT, domains=("login_rewards", "recovery"),
              time_semantics=TimeSemantics.FROZEN_PER_OPERATION, size_limit=CLI_RULE_LIMIT,
              examples=("recovery-rules.example.json",)),
    ConfigKey("trade-rules", "--trade-rules", "TradeRulesFile", rules_class="trade_history:TradeRules",
              loader="cli:load_trade_rules", normalizer=Normalizer.ASDICT, domains=("trades",),
              time_semantics=TimeSemantics.ELIGIBILITY, size_limit=CLI_RULE_LIMIT,
              examples=("trade-rules.example.json",)),
    ConfigKey("ticket-rules", "--ticket-rules", "TicketRulesFile", rules_class="summon_tickets:SummonTicketRules",
              loader="cli:load_ticket_rules", normalizer=Normalizer.ASDICT,
              domains=("comeback_logins", "daily_gifts", "entrust", "event_logins", "login_rewards", "packages",
                       "purchased_logins", "rank_rewards", "serial_codes", "summon_tickets"),
              time_semantics=TimeSemantics.ELIGIBILITY, size_limit=CLI_RULE_LIMIT),
    ConfigKey("item-ticket-rules", "--item-ticket-rules", "ItemTicketRulesFile",
              rules_class="summon_tickets:SummonTicketRules", loader="cli:load_item_ticket_rules",
              normalizer=Normalizer.ASDICT,
              domains=("comeback_logins", "daily_gifts", "entrust", "event_logins", "event_passes", "fame",
                       "item_summon_tickets", "letstd", "login_rewards", "main_quests", "missions", "packages",
                       "purchased_logins", "rank_rewards", "serial_codes", "skin_shop"),
              time_semantics=TimeSemantics.ELIGIBILITY, size_limit=CLI_RULE_LIMIT,
              examples=("item-ticket-rules.example.json",)),
    ConfigKey("login-rules", "--login-rules", "LoginRulesFile", rules_class="login_reward_service:LoginRewardRules",
              loader="cli:load_login_rules", normalizer=Normalizer.ASDICT, domains=("login_rewards",),
              time_semantics=TimeSemantics.FIRST_BASELINE, size_limit=CLI_RULE_LIMIT,
              field_semantics={"cooking_item_id": TimeSemantics.ELIGIBILITY,
                               "cooking_sender": TimeSemantics.ELIGIBILITY},
              examples=("login-rules.example.json",)),
    ConfigKey("event-pass-rules", "--event-pass-rules", "EventPassRulesFile",
              rules_class="event_pass_rules:EventPassAutomationRules", loader="cli:load_event_pass_rules",
              normalizer=Normalizer.ASDICT, domains=("event_passes", "rank_rewards"),
              time_semantics=TimeSemantics.ELIGIBILITY, size_limit=CLI_RULE_LIMIT,
              examples=("event-pass-rules.example.json",)),
    ConfigKey("event-pass-revival", "--event-pass-revival", "EventPassRevivalFile",
              rules_class="event_pass_revival:EventPassRevivalRules", loader="event_pass_revival:load_revival_rules",
              normalizer=Normalizer.TO_DICT,
              domains=("entrust", "event_pass_revival", "event_passes", "fame", "missions", "native_cdn", "packages",
                       "rank_rewards", "skin_shop"),
              time_semantics=TimeSemantics.RESOURCE, size_limit="event_pass_revival:MAX_RULES_BYTES",
              resource_hint=True, examples=("event-pass-revival.example.json",)),
    ConfigKey("event-login-rules", "--event-login-rules", "EventLoginRulesFile",
              rules_class="event_login_rules:EventLoginRules", loader="event_login_rules:load_event_login_rules",
              normalizer=Normalizer.TO_DICT,
              domains=("comeback_logins", "event_logins", "login_rewards", "native_cdn", "packages", "purchased_logins",
                       "rank_rewards"),
              time_semantics=TimeSemantics.ELIGIBILITY, size_limit="event_login_rules:MAX_RULE_BYTES", resource_hint=True,
              examples=("event-login-revival.example.json", "event-login-rules.example.json")),
    ConfigKey("comeback-login-rules", "--comeback-login-rules", "ComebackLoginRulesFile",
              rules_class="comeback_login_rules:ComebackLoginRules",
              loader="comeback_login_rules:load_comeback_login_rules", normalizer=Normalizer.TO_DICT,
              domains=("comeback_logins", "login_rewards", "rank_rewards"), time_semantics=TimeSemantics.ELIGIBILITY,
              size_limit="comeback_login_rules:MAX_RULE_BYTES", examples=("comeback-login-rules.example.json",)),
    ConfigKey("purchased-login-rules", "--purchased-login-rules", "PurchasedLoginRulesFile",
              rules_class="purchased_login_rules:PurchasedLoginRules",
              loader="purchased_login_rules:load_purchased_login_rules", normalizer=Normalizer.TO_DICT,
              domains=("login_rewards", "native_cdn", "packages", "purchased_logins", "rank_rewards"),
              time_semantics=TimeSemantics.ELIGIBILITY, size_limit="purchased_login_rules:MAX_RULE_BYTES",
              resource_hint=True, examples=("purchased-login-rules.example.json",)),
    ConfigKey("daily-gift-rules", "--daily-gift-rules", "DailyGiftRulesFile", rules_class="daily_gift_rules:DailyGiftRules",
              loader="daily_gift_rules:load_daily_gift_rules", normalizer=Normalizer.TO_DICT,
              domains=("daily_gifts", "login_rewards", "rank_rewards"),
              time_semantics=TimeSemantics.ELIGIBILITY, size_limit="daily_gift_rules:MAX_RULE_BYTES",
              examples=("daily-gift-revival.example.json", "daily-gift-rules.example.json")),
    ConfigKey("vote-rules", "--vote-rules", "VoteRulesFile", rules_class="vote_rules:VoteRules",
              loader="vote_rules:load_vote_rules", normalizer=Normalizer.TO_DICT,
              domains=("entrust", "login_rewards", "main_quests", "rank_rewards", "vote_assault_notifications",
                       "vote_cooking", "vote_cooking_notifications", "vote_expiry_notifications", "votes"),
              time_semantics=TimeSemantics.ELIGIBILITY, size_limit="vote_rules:MAX_RULE_BYTES",
              examples=("vote-revival.example.json", "vote-rules.example.json")),
    ConfigKey("vote-cooking-rules", "--vote-cooking-rules", "VoteCookingRulesFile",
              rules_class="vote_cooking_rules:VoteCookingRules", loader="vote_cooking_rules:load_vote_cooking_rules",
              normalizer=Normalizer.TO_DICT,
              domains=("login_rewards", "rank_rewards", "vote_cooking", "vote_cooking_notifications", "votes"),
              time_semantics=TimeSemantics.ELIGIBILITY, size_limit="vote_cooking_rules:MAX_RULE_BYTES",
              examples=("vote-cooking-rules.example.json",)),
    ConfigKey("entrust-rules", "--entrust-rules", "EntrustRulesFile", rules_class="entrust_service:EntrustRules",
              loader="cli:load_entrust_rules", normalizer=Normalizer.ASDICT, domains=("entrust",),
              time_semantics=TimeSemantics.FROZEN_PER_OPERATION, size_limit=CLI_RULE_LIMIT,
              examples=("entrust-rules.example.json",)),
    ConfigKey("mission-windows", "--mission-windows", "MissionWindowsFile",
              rules_class="mission_window_revival:MissionWindowRules",
              loader="mission_window_revival:load_mission_window_rules", normalizer=Normalizer.TO_DICT,
              domains=("entrust", "main_quests", "mission_windows", "native_cdn", "subjugation"),
              time_semantics=TimeSemantics.RESOURCE, size_limit="mission_window_revival:MAX_RULE_BYTES",
              resource_hint=True, examples=("mission-windows.example.json",)),
    ConfigKey("command-text-overrides", "--command-text-overrides", "CommandTextOverridesFile",
              rules_class="command_text_overlay:CommandTextOverrides",
              loader="command_text_overlay:load_command_text_overrides", normalizer=Normalizer.TO_DICT,
              domains=("entrust", "native_cdn"),
              time_semantics=TimeSemantics.RESOURCE, size_limit="command_text_overlay:MAX_RULE_BYTES",
              resource_hint=True, examples=("command-text-overrides.example.json",)),
    ConfigKey("tower-rules", "--tower-rules", "TowerRulesFile", rules_class="tower_state:TowerRules",
              loader="tower_state:load_tower_rules", normalizer=Normalizer.TO_DICT, domains=("main_quests", "towers"),
              time_semantics=TimeSemantics.FROZEN_PER_OPERATION, size_limit="tower_state:MAX_RULE_BYTES",
              field_semantics={"generation_hour": TimeSemantics.TIME_ACCUMULATING,
                               "replenish_hour": TimeSemantics.TIME_ACCUMULATING,
                               "first_access_settles_day": TimeSemantics.FIRST_BASELINE},
              examples=("tower-rules.example.json",)),
    ConfigKey("subjugation-rules", "--subjugation-rules", "SubjugationRulesFile",
              rules_class="subjugation_rules:SubjugationRules", loader="subjugation_rules:load_subjugation_rules",
              normalizer=Normalizer.TO_DICT, domains=("main_quests", "subjugation"),
              time_semantics=TimeSemantics.FROZEN_PER_OPERATION, size_limit="subjugation_rules:MAX_RULE_BYTES",
              examples=("subjugation-rules.example.json",)),
    ConfigKey("serial-code-rules", "--serial-code-rules", "SerialCodeRulesFile",
              rules_class="serial_code_rules:SerialCodeRules", loader="serial_code_rules:load_serial_code_rules",
              normalizer=Normalizer.TO_DICT, domains=("serial_codes",), time_semantics=TimeSemantics.ELIGIBILITY,
              size_limit="serial_code_rules:MAX_RULE_BYTES", examples=("serial-code-rules.example.json",)),
    ConfigKey("letstd-rewards", "--letstd-rewards", "LetsTDRewardsFile", rules_class="letstd_rules:LetsTDRules",
              loader="letstd_rules:load_letstd_rules", normalizer=Normalizer.TO_DICT,
              domains=("letstd", "login_rewards", "rank_rewards"),
              time_semantics=TimeSemantics.ELIGIBILITY, size_limit="letstd_rules:MAX_RULE_BYTES",
              examples=("letstd-rewards.example.json",)),
))

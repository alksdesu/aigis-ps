from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .admin_command import DOMAIN_NAME, error_payload
from .admin_views import service
from .background_service import HISTORY_NAMESPACE as BACKGROUND_HISTORY
from .background_state import read_backgrounds
from .barracks_service import HISTORY_NAMESPACE as BARRACKS_HISTORY
from .blessing_service import HISTORY_NAMESPACE as BLESSING_HISTORY
from .bootstrap import EVENT_FLAGS_CONVERTED, MULTI_ROW_TABLES, PGP2_FIELDS, STARTUP_COLLECTIONS, TABLES, WIRE_TYPES
from .business_calendar import MARKER_KEY as BUSINESS_CALENDAR_KEY, NATIVE_DATETIME, business_date, read_marker
from .card_status import FIRST_EPISODE, SECOND_EPISODE, THIRD_EPISODE
from .comeback_login_state import LEDGER_KEY as COMEBACK_LOGIN_KEY, read_comeback_logins
from .config_state import CONFIGURATION_FIELDS
from .crystal_spending import read_spending
from .current_squad import HISTORY_NAMESPACE as CURRENT_SQUAD_HISTORY
from .daily_gift_state import LEDGER_KEY as DAILY_GIFT_KEY, read_daily_gifts
from .energy_clock import (JST, MARKER_KEY as ENERGY_CLOCK_KEY, PREFIXES, SECONDS_PER_DAY, UNIX_EPOCH, anchor_seconds,
                           validate_clock)
from .entrust_service import HISTORY_NAMESPACE as ENTRUST_HISTORY
from .errors import ProtocolError
from .event_login_state import LEDGER_KEY as EVENT_LOGIN_KEY, read_event_logins
from .event_pass_service import (AUTOMATION_KEY as PASS_AUTOMATION, DELIVERY_KEY as PASS_DELIVERY,
                                 HISTORY_NAMESPACE as EVENT_PASS_HISTORY, EventPassService)
from .event_pass_state import LEDGER_KEY as PASS_LEDGER, read_event_passes
from .evolution_rewards import LEDGER_KEY as AWAKENING_LEDGER
from .evolution_service import HISTORY_NAMESPACE as EVOLUTION_HISTORY
from .fairy_farm import HISTORY_NAMESPACE as FAIRY_FARM_HISTORY
from .fame_service import HISTORY_NAMESPACE as FAME_HISTORY
from .filter_state import read_filters
from .item_summon_tickets import HISTORY_NAMESPACE as ITEM_SUMMON_HISTORY
from .letstd_state import LEDGER_KEY as LETSTD_KEY, read_letstd
from .login_cooking_state import LEDGER_KEY as LOGIN_COOKING_KEY
from .login_reward_service import DELIVERY_KEY as LOGIN_REWARD_DELIVERY, HISTORY_NAMESPACE as LOGIN_REWARD_HISTORY
from .login_reward_state import LEDGER_KEY as LOGIN_REWARD_KEY
from .login_stamp_pending import PENDING_KEY, read_pending_stamps
from .mission_ep_bindings import LEDGER_KEY as BINDING_LEDGER, read_bindings
from .mission_service import HISTORY_NAMESPACE as MISSION_HISTORY, PASS_DELIVERY_KEY as MISSION_PASS_DELIVERY
from .mission_state import JOURNAL_KEY as MISSION_JOURNAL, SKIN_HISTORY_KEY as SKIN_HISTORY
from .monthly_capacity import read_capacity
from .native_clock import NATIVE_EPOCH
from .native_numbers import UINT32_MASK, float32
from .orb_exchange import HISTORY_NAMESPACE as ORB_EXCHANGE_HISTORY
from .package_service import HISTORY_NAMESPACE as PACKAGE_HISTORY
from .peruse_state import read_perused
from .player_state import (CLIENT_RUNTIME_FIELDS, GIFT_TUTORIAL_FLAG, LOCK_FLAG, SKILL_SWITCH_FLAG, SQUAD_FIELDS,
                           SQUAD_TUTORIAL_FLAGS, UNIT_BOOLEANS, UNIT_FIELDS)
from .purchased_login_state import LEDGER_KEY as PURCHASED_LOGIN_KEY, read_purchased_logins
from .quest_record_admin import ARCHIVE_KEY as QUEST_RECORD_ARCHIVE, read_archive
from .rank_reward_state import LEDGER_KEY as RANK_REWARD_LEDGER, read_rank_rewards
from .rank_rewards import DELIVERY_KEY as RANK_REWARD_DELIVERY, HISTORY_NAMESPACE as RANK_REWARD_HISTORY
from .recovery_service import HISTORY_NAMESPACE as RECOVERY_HISTORY
from .select_tickets import HISTORY_NAMESPACE as SELECT_TICKET_HISTORY
from .serial_code_service import (HISTORY_NAMESPACE as SERIAL_CODE_HISTORY, REDEMPTION_NAMESPACE as SERIAL_CODE_KEY,
                                  read_redemptions)
from .silver_house import HISTORY_NAMESPACE as SILVER_HOUSE_HISTORY
from .skin_shop_service import HISTORY_NAMESPACE as SKIN_SHOP_HISTORY
from .sugoroku_service import HISTORY_NAMESPACE as SUGOROKU_HISTORY
from .sugoroku_state import read_sugoroku
from .summon_tickets import HISTORY_NAMESPACE as SUMMON_TICKET_HISTORY
from .tower_state import DOMAIN as TOWER_STATE, read_state as read_tower_state
from .tower_trade_service import RECEIPT_NAMESPACE as TOWER_TRADE_RECEIPTS
from .tower_trade_state import HISTORY_KEY as TOWER_LEDGER
from .trade_service import RECEIPT_NAMESPACE as TRADE_RECEIPTS
from .training_service import HISTORY_NAMESPACE as TRAINING_HISTORY
from .unit_detail import HISTORY_NAMESPACE as UNIT_DETAIL_HISTORY
from .unit_sale import HISTORY_KEY as UNIT_SALE_HISTORY
from .universal_flags import read_flags
from .view_events import HISTORY_NAMESPACE as VIEW_EVENT_HISTORY
from .vote_expiry import (ASSAULT_KEY as ASSAULT_LEDGER, ASSAULT_NOTIFICATION_KEY as ASSAULT_NOTICE_LEDGER,
                          NOTIFICATION_KEY as EXPIRY_NOTICE_LEDGER)
from .vote_state import read_vote_history
from .vote_tickets import LEDGER_KEY as TICKET_LEDGER
from .vote_transactions import HISTORY_NAMESPACE as VOTE_RECEIPTS


TYPE_DATE, TYPE_TIME, TYPE_DATETIME = 7, 8, 9
FIELD_MASK = UINT32_MASK
FIELD_BITS = UINT32_MASK.bit_length()
# These integer columns hold native day counts although their schema type is a plain int.
NATIVE_DAY_FIELDS = frozenset(("crystals.Date", "quest_records.FirstDefeatDate", "game_param2.HomeCookingReceivedDate"))
OWNER_TABLES = MappingProxyType({"units": "units", "squads": "squads", "configuration": "config"})
GIFTS, EVOLUTION_REWARDS, UNIT_OVERRIDES = "gifts", "evolution_rewards", "unit_overrides"
GIFT_LEDGER, OVERRIDES_KEY = "gift_state", "unit_dynamic_overrides"
EVENT_LOGINS, COMEBACK_LOGINS, PURCHASED_LOGINS, DAILY_GIFTS = ("event_logins", "comeback_logins", "purchased_logins",
                                                                "daily_gifts")
LOGIN_REWARDS, EVENT_PASSES, MISSIONS, SUGOROKU = "login_rewards", "event_passes", "missions", "sugoroku"
PACKAGES, TRADES, TOWER_TRADES, TOWERS = "packages", "trades", "tower_trades", "towers"
VOTES, TRANSACTIONS, VOTE_COOKING, RANK_REWARDS = "votes", "transactions", "vote_cooking", "rank_rewards"
ASSAULT_EXPIRY, COOKING_NOTIFIER = "assault_expiry", "vote_cooking_notifier"
EXPIRY_NOTIFIER, ASSAULT_NOTIFIER = "vote_expiry_notifier", "vote_assault_notifier"
BOARD_LEDGER, TRADE_LEDGER = "sugoroku", "trade_history"
PACKAGE_LEDGER = "package_purchases"
JOURNAL_LEDGER, REWARD_LEDGER = "vote_state", "vote_rewards"
RANKING_LEDGER, EXPIRY_LEDGER, LAST_ACTION = "vote_finalists", "vote_expiry", "vote_last_action"
VOTE_COOKING_LEDGER, VOTE_COOKING_NOTICE_LEDGER = "vote_cooking", "vote_cooking_notifications"


class BitLevel(StrEnum):
    VERIFIED = "verified"
    NAMED = "named"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Bit:
    mask: int
    name: str
    level: BitLevel

    def __post_init__(self) -> None:
        if type(self.mask) is not int or self.mask <= 0 or self.mask & (self.mask - 1) or self.mask > FIELD_MASK:
            raise ValueError("Bit meanings name exactly one bit of a 32-bit field")
        if not isinstance(self.name, str) or not self.name or self.level not in (BitLevel.VERIFIED, BitLevel.NAMED):
            raise ValueError("Bit meanings need a name and a verified or named level")

    @property
    def index(self) -> int:
        return self.mask.bit_length() - 1


@dataclass(frozen=True)
class BitTable:
    bits: tuple[Bit, ...]

    def __post_init__(self) -> None:
        bits = tuple(self.bits)
        if not bits or any(not isinstance(bit, Bit) for bit in bits) or len({bit.mask for bit in bits}) != len(bits):
            raise ValueError("Bit tables list distinct bit meanings")
        object.__setattr__(self, "bits", tuple(sorted(bits, key=lambda bit: bit.mask)))

    def decode(self, value: int) -> list[dict]:
        unsigned = value & FIELD_MASK
        described = {bit.mask for bit in self.bits}
        decoded = [{"bit": bit.index, "name": bit.name, "level": bit.level, "set": bool(unsigned & bit.mask)}
                   for bit in self.bits]
        decoded.extend({"bit": index, "name": None, "level": BitLevel.UNKNOWN, "set": True}
                       for index in range(FIELD_BITS) if unsigned >> index & 1 and 1 << index not in described)
        return decoded

    def describe(self) -> list[dict]:
        return [{"bit": bit.index, "name": bit.name, "level": bit.level} for bit in self.bits]


def bit_masks(flags: int) -> tuple[int, ...]:
    return tuple(1 << index for index in range(flags.bit_length()) if flags >> index & 1)


TUTORIAL_BITS = BitTable((Bit(GIFT_TUTORIAL_FLAG, "gift_tutorial", BitLevel.VERIFIED),
                          *(Bit(mask, "squad_tutorial", BitLevel.VERIFIED)
                            for mask in bit_masks(SQUAD_TUTORIAL_FLAGS))))
STORY_BITS = BitTable((Bit(FIRST_EPISODE, "first_episode", BitLevel.VERIFIED),
                       Bit(SECOND_EPISODE, "second_episode", BitLevel.VERIFIED),
                       Bit(THIRD_EPISODE, "harlem_episode", BitLevel.VERIFIED)))
# Bits 4-7 hold the barracks index, a number rather than four flags, so only the single flags are listed.
UNIT_FLAG_BITS = BitTable((Bit(LOCK_FLAG, "locked", BitLevel.VERIFIED),
                           Bit(SKILL_SWITCH_FLAG, "awakened_skill_selected", BitLevel.VERIFIED)))
BIT_TABLES = MappingProxyType({
    "game_param2._EvCmb": BitTable((Bit(EVENT_FLAGS_CONVERTED, "legacy_event_flags_converted", BitLevel.VERIFIED),)),
    "config.MenuTutorialFlags": TUTORIAL_BITS,
    "card_status.A7": STORY_BITS,
    "units.GeneralPurposeFlags": UNIT_FLAG_BITS,
})


def native_date(day: int) -> date | None:
    return None if day == 0 else NATIVE_EPOCH.date() + timedelta(days=day)


def native_instant(seconds: int) -> datetime | None:
    return None if seconds == 0 else NATIVE_EPOCH + timedelta(seconds=seconds)


def native_datetime(text: str) -> datetime | None:
    return None if text == "" else datetime.strptime(text, NATIVE_DATETIME).replace(tzinfo=JST)


def _iso(moment: date | datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _time_of_day(fraction: Any) -> dict:
    if type(fraction) not in (int, float):
        raise TypeError("Native times are day fractions")
    seconds = round(float32(fraction) * SECONDS_PER_DAY)
    hours, rest = divmod(seconds, 3600)
    return {"seconds": seconds, "time": f"{hours:02d}:{rest // 60:02d}:{rest % 60:02d}"}


def decode_value(type_code: int, value: Any) -> dict:
    try:
        if type_code == TYPE_DATE:
            return {"date": _iso(native_date(value))}
        if type_code == TYPE_TIME:
            return _time_of_day(value)
        if type_code == TYPE_DATETIME:
            return {"datetime": _iso(native_datetime(value))}
    except (TypeError, ValueError, OverflowError):
        return {"invalid": True}
    return {}


def decode_field(table: str, name: str, type_code: int, value: Any) -> dict:
    reference = f"{table}.{name}"
    decoded = {"value": value, **decode_value(TYPE_DATE if reference in NATIVE_DAY_FIELDS else type_code, value)}
    bits = BIT_TABLES.get(reference)
    if bits is not None and type(value) is int:
        decoded["bits"] = bits.decode(value)
    return decoded


def decode_row(table: str, row: Mapping[str, Any]) -> dict:
    return {name: {"wire": wire, "type_code": code, **decode_field(table, name, code, row[name])}
            for name, code, wire in TABLES[table].fields}


def _anchor(row: dict, prefix: str) -> dict:
    try:
        seconds = anchor_seconds(row, prefix)
    except ProtocolError:
        return {"seconds": None, "jst": None, "invalid": True}
    try:
        return {"seconds": seconds, "jst": _iso(native_instant(seconds))}
    except OverflowError:
        return {"seconds": seconds, "jst": None, "invalid": True}


def anchors(row: dict) -> dict:
    return {prefix: _anchor(row, prefix) for prefix in PREFIXES}


def logical_day(now: datetime) -> date:
    return business_date(now)


def epoch_milliseconds(value: int) -> str | None:
    try:
        return (UNIX_EPOCH + timedelta(milliseconds=value)).isoformat()
    except (TypeError, OverflowError):
        return None


def _table_fields(table: str, bootstrap: Any, owners: Mapping[str, str]) -> dict:
    spec, schema = TABLES[table], bootstrap.schemas.get(table)
    sizes = {} if schema is None else {field.name: field.size for field in schema.fields}
    fields = []
    for name, code, wire in spec.fields:
        reference = f"{table}.{name}"
        fields.append({"field": name, "wire": wire, "type_code": code, "value_type": WIRE_TYPES[code].value,
                       "size": sizes.get(name), "owner": owners.get(reference),
                       "native_day": reference in NATIVE_DAY_FIELDS, "bits": reference in BIT_TABLES})
    return {"resource": spec.resource, "verified": schema is not None, "issue": bootstrap.issues.get(table),
            "single_row": table not in MULTI_ROW_TABLES, "fields": fields}


def field_dictionary(bootstrap: Any, owners: Mapping[str, str]) -> dict:
    def reference(group: str, name: str) -> str:
        return f"{OWNER_TABLES[group]}.{name}"

    def owner(group: str, name: str) -> str | None:
        return owners.get(reference(group, name))

    return {
        "tables": {table: _table_fields(table, bootstrap, owners) for table in TABLES},
        "collections": [{"container": container, "table": table} for container, table in STARTUP_COLLECTIONS.items()],
        "units": [{"field": name, "wire": wire, "boolean": name in UNIT_BOOLEANS, "owner": owner("units", name),
                   "bits": reference("units", name) in BIT_TABLES}
                  for name, wire in UNIT_FIELDS],
        "client_runtime": [{"field": name, "client_derived": True, "persisted": False}
                           for name in CLIENT_RUNTIME_FIELDS],
        "squads": [{"field": name, "wire": wire, "value_type": kind.value, "owner": owner("squads", name)}
                   for name, wire, kind in SQUAD_FIELDS],
        "configuration": [{"field": name, "wire": wire, "value_type": kind.__name__,
                           "owner": owner("configuration", name)}
                          for name, (wire, kind) in CONFIGURATION_FIELDS.items()],
        "pgp2": [{"wire": wire, "field": name} for wire, name in PGP2_FIELDS],
        "bits": {reference: table.describe() for reference, table in BIT_TABLES.items()},
        "state_keys": {key: claim.domain for key, claim in STATE_KEYS.items()},
        "owners": dict(owners),
    }


Reader = Callable[[dict, Any], object]


@dataclass(frozen=True)
class StateKey:
    domain: str
    read: Reader | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.domain, str) or not DOMAIN_NAME.fullmatch(self.domain):
            raise ValueError("State keys are claimed by a domain name")
        if self.read is not None and not callable(self.read):
            raise ValueError("State key readers must be callable")


def _state_reader(read: Callable[[dict], object]) -> Reader:
    return lambda state, bundle: read(state)


def transactions(bundle: Any) -> Any:
    return service(service(bundle, VOTES), TRANSACTIONS)


def _bootstrap_tables(state: dict, bundle: Any) -> dict:
    bootstrap = service(bundle, "bootstrap")
    return {table: bootstrap.table_rows(state, table) for table in bootstrap.schemas}


def _player_state(state: dict, bundle: Any) -> dict:
    return service(bundle, "player_state").validate(state)


def _card_status(state: dict, bundle: Any) -> list:
    return service(bundle, "card_status").read(state)


def _fame_depository(state: dict, bundle: Any) -> list:
    return service(bundle, "fame_repository").read(state)


def _fairy_farm(state: dict, bundle: Any) -> list:
    return service(bundle, "fairy_farm").read(state)


def _silver_house(state: dict, bundle: Any) -> list:
    return service(bundle, "silver_house").read(state)


def _presents(state: dict, bundle: Any) -> list:
    return service(bundle, "presents").rows(state)


def _awakening_rewards(state: dict, bundle: Any) -> dict:
    return service(bundle, EVOLUTION_REWARDS).read(state)


def _vote_rewards(state: dict, bundle: Any) -> tuple:
    return transactions(bundle).rewards.validated(state)


def _vote_tickets(state: dict, bundle: Any) -> dict:
    return transactions(bundle).tickets.ticket_status(state)


def _vote_finalists(state: dict, bundle: Any) -> dict:
    return transactions(bundle).finalists.validated(state)


def _vote_expiry(state: dict, bundle: Any) -> dict:
    return transactions(bundle).expiry.ledger(state)


def _assault_expiry(state: dict, bundle: Any) -> dict:
    return service(transactions(bundle), ASSAULT_EXPIRY).ledger(state)


def _expiry_notifications(state: dict, bundle: Any) -> dict:
    return service(service(bundle, RANK_REWARDS), EXPIRY_NOTIFIER).records(state)


def _assault_notifications(state: dict, bundle: Any) -> dict:
    return service(service(bundle, RANK_REWARDS), ASSAULT_NOTIFIER).records(state)


def _vote_cooking(state: dict, bundle: Any) -> dict:
    return service(bundle, VOTE_COOKING).ledger(state)


def _cooking_notifications(state: dict, bundle: Any) -> dict:
    return service(bundle, COOKING_NOTIFIER).ledger(state)


def _package_purchases(state: dict, bundle: Any) -> dict:
    return service(bundle, PACKAGES).purchases.read(state)


def _trade_history(state: dict, bundle: Any) -> dict:
    trades = service(bundle, TRADES)
    return trades.history.records(state, trades.clock())


def _tower_trade_history(state: dict, bundle: Any) -> Mapping:
    return service(bundle, TOWER_TRADES).history.records(state)


def _login_rewards(state: dict, bundle: Any) -> dict:
    return service(bundle, LOGIN_REWARDS).current_ledger(state)


def _login_cooking(state: dict, bundle: Any) -> dict:
    return service(bundle, LOGIN_REWARDS).current_cooking(state)


def _tower_state(state: dict, bundle: Any) -> dict:
    return read_tower_state(state, service(bundle, TOWERS).catalog)


# A schema marker versions a collection under tables, so the collection's reader checks it.
STATE_KEYS = MappingProxyType({
    "bootstrap_schema": StateKey("bootstrap", _bootstrap_tables),
    "startup_collections_schema": StateKey("bootstrap"),
    "created_local": StateKey("bootstrap"),
    "tables": StateKey("bootstrap", _bootstrap_tables),
    ENERGY_CLOCK_KEY: StateKey("account", _state_reader(validate_clock)),
    BUSINESS_CALENDAR_KEY: StateKey("account", _state_reader(read_marker)),
    "crystal_spending": StateKey("account", _state_reader(read_spending)),
    "monthly_capacity": StateKey("account", _state_reader(read_capacity)),
    "player_state": StateKey("player_state", _player_state),
    "card_status_schema": StateKey("card_status", _card_status),
    "universal_flags_schema": StateKey("bootstrap", _state_reader(read_flags)),
    "backgrounds_schema": StateKey("backgrounds", _state_reader(read_backgrounds)),
    "perused_events_schema": StateKey("peruse", _state_reader(read_perused)),
    "filter_settings_schema": StateKey("filters", _state_reader(read_filters)),
    "fame_depository_schema": StateKey("fame", _fame_depository),
    "fairy_farm_schema": StateKey("fairy_farm", _fairy_farm),
    "silver_house_schema": StateKey("silver_house", _silver_house),
    "presents_schema": StateKey("presents", _presents),
    "last_present_id": StateKey("presents", _presents),
    JOURNAL_LEDGER: StateKey(VOTES, _state_reader(read_vote_history)),
    REWARD_LEDGER: StateKey(VOTES, _vote_rewards),
    TICKET_LEDGER: StateKey(VOTES, _vote_tickets),
    RANKING_LEDGER: StateKey(VOTES, _vote_finalists),
    EXPIRY_LEDGER: StateKey(VOTES, _vote_expiry),
    EXPIRY_NOTICE_LEDGER: StateKey(VOTES, _expiry_notifications),
    ASSAULT_LEDGER: StateKey(VOTES, _assault_expiry),
    ASSAULT_NOTICE_LEDGER: StateKey(VOTES, _assault_notifications),
    VOTE_COOKING_LEDGER: StateKey(VOTE_COOKING, _vote_cooking),
    VOTE_COOKING_NOTICE_LEDGER: StateKey(VOTE_COOKING, _cooking_notifications),
    LOGIN_REWARD_KEY: StateKey(LOGIN_REWARDS, _login_rewards),
    LOGIN_COOKING_KEY: StateKey(LOGIN_REWARDS, _login_cooking),
    PENDING_KEY: StateKey(LOGIN_REWARDS, _state_reader(read_pending_stamps)),
    RANK_REWARD_LEDGER: StateKey(RANK_REWARDS, _state_reader(read_rank_rewards)),
    PASS_LEDGER: StateKey(EVENT_PASSES, _state_reader(read_event_passes)),
    PASS_AUTOMATION: StateKey(EVENT_PASSES, _state_reader(EventPassService.pending)),
    PASS_DELIVERY: StateKey(EVENT_PASSES, _state_reader(EventPassService.validate_delivery)),
    EVENT_LOGIN_KEY: StateKey(EVENT_LOGINS, _state_reader(read_event_logins)),
    COMEBACK_LOGIN_KEY: StateKey(COMEBACK_LOGINS, _state_reader(read_comeback_logins)),
    PURCHASED_LOGIN_KEY: StateKey(PURCHASED_LOGINS, _state_reader(read_purchased_logins)),
    DAILY_GIFT_KEY: StateKey(DAILY_GIFTS, _state_reader(read_daily_gifts)),
    BOARD_LEDGER: StateKey(SUGOROKU, _state_reader(read_sugoroku)),
    BINDING_LEDGER: StateKey(MISSIONS, _state_reader(read_bindings)),
    PACKAGE_LEDGER: StateKey(PACKAGES, _package_purchases),
    TRADE_LEDGER: StateKey(TRADES, _trade_history),
    TOWER_LEDGER: StateKey(TOWER_TRADES, _tower_trade_history),
    TOWER_STATE: StateKey(TOWERS, _tower_state),
    SERIAL_CODE_KEY: StateKey("serial_codes", _state_reader(read_redemptions)),
    LETSTD_KEY: StateKey("letstd", _state_reader(read_letstd)),
    QUEST_RECORD_ARCHIVE: StateKey("quest_records", _state_reader(read_archive)),
    # Validating the trust journal needs the save revision, so units.ledgers checks it rather than this claim.
    GIFT_LEDGER: StateKey(GIFTS),
    AWAKENING_LEDGER: StateKey(EVOLUTION_REWARDS, _awakening_rewards),
    OVERRIDES_KEY: StateKey("units"),
    # Receipts are checked against the save revision, which a state reader never receives.
    BACKGROUND_HISTORY: StateKey("backgrounds"),
    BARRACKS_HISTORY: StateKey("barracks"),
    BLESSING_HISTORY: StateKey("blessings"),
    CURRENT_SQUAD_HISTORY: StateKey("current_squad"),
    ENTRUST_HISTORY: StateKey("entrust"),
    EVENT_PASS_HISTORY: StateKey(EVENT_PASSES),
    EVOLUTION_HISTORY: StateKey("evolution"),
    FAIRY_FARM_HISTORY: StateKey("fairy_farm"),
    FAME_HISTORY: StateKey("fame"),
    ITEM_SUMMON_HISTORY: StateKey("item_summon_tickets"),
    LOGIN_REWARD_HISTORY: StateKey(LOGIN_REWARDS),
    MISSION_HISTORY: StateKey(MISSIONS),
    ORB_EXCHANGE_HISTORY: StateKey("orb_exchange"),
    PACKAGE_HISTORY: StateKey(PACKAGES),
    RANK_REWARD_HISTORY: StateKey(RANK_REWARDS),
    RECOVERY_HISTORY: StateKey("recovery"),
    SELECT_TICKET_HISTORY: StateKey("selection_tickets"),
    SERIAL_CODE_HISTORY: StateKey("serial_codes"),
    SILVER_HOUSE_HISTORY: StateKey("silver_house"),
    SKIN_SHOP_HISTORY: StateKey("skin_shop"),
    SUGOROKU_HISTORY: StateKey(SUGOROKU),
    SUMMON_TICKET_HISTORY: StateKey("summon_tickets"),
    TOWER_TRADE_RECEIPTS: StateKey(TOWER_TRADES),
    TRADE_RECEIPTS: StateKey(TRADES),
    TRAINING_HISTORY: StateKey("training"),
    UNIT_DETAIL_HISTORY: StateKey("unit_detail"),
    UNIT_SALE_HISTORY: StateKey("unit_sales"),
    VIEW_EVENT_HISTORY: StateKey("view_events"),
    VOTE_RECEIPTS: StateKey(VOTES),
    LAST_ACTION: StateKey(VOTES),
    "present_claim_history": StateKey("presents"),
    "present_claim_history_schema": StateKey("presents"),
    # Their owners check them only privately or while initializing a copy of the whole save.
    "quest_state": StateKey("main_quests"),
    MISSION_JOURNAL: StateKey(MISSIONS),
    SKIN_HISTORY: StateKey(MISSIONS),
    MISSION_PASS_DELIVERY: StateKey(MISSIONS),
    "package_delivery": StateKey(PACKAGES),
    LOGIN_REWARD_DELIVERY: StateKey(LOGIN_REWARDS),
    RANK_REWARD_DELIVERY: StateKey(RANK_REWARDS),
})


def _checked(read: Reader, state: dict, bundle: Any) -> dict:
    try:
        read(state, bundle)
    except Exception as error:
        # A reader bug on a malformed save must not hide the validity of the other ledgers.
        return {"valid": False, "error": error_payload(error)}
    return {"valid": True, "error": None}


def ledger_status(state: dict, bundle: Any, claims: Mapping[str, StateKey] = STATE_KEYS, *,
                  keys: Iterable[str] | None = None) -> list[dict]:
    results: dict[Reader, dict] = {}
    entries = []
    for key in state if keys is None else keys:
        claim = claims.get(key)
        entry = {"key": key, "claimed": claim is not None, "domain": None if claim is None else claim.domain,
                 "checked": claim is not None and claim.read is not None}
        if entry["checked"]:
            if claim.read not in results:
                results[claim.read] = _checked(claim.read, state, bundle)
            entry.update(results[claim.read])
        entries.append(entry)
    return entries

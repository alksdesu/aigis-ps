from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import date
from functools import partial
import hashlib
from itertools import product
import json
import logging
from operator import itemgetter
import re
from types import MappingProxyType

from .alxml import Column, Container, ValueType, dumps, loads
from .business_calendar import MONTH_TEXT
from .crystal_spending import (NEW_LOCAL as SPENDING_NEW_LOCAL, POLICY_VERSION, PROJECTION_CAP, _clock,
                               initialize_spending, read_spending, settle_spending)
from .energy_clock import instant
# Native 69FF70 sends 115 through 6A0F60; 66E8B0 answers any HTTP error with a dialog and resends the same body.
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, ProtocolError
from .event_login_service import CAPACITY_ERRORS
from .event_login_state import LEDGER_KEY as EVENT_LOGIN_KEY, validate_event_login_grants
from .comeback_login_state import DATE_FIELD, DAYS_FIELD, LEDGER_KEY as COMEBACK_LOGIN_KEY, validate_comeback_login_grants
from .comeback_login_service import combine_event_login_notifications
from .native_numbers import int32
from .purchased_login_state import LEDGER_KEY as PURCHASED_LOGIN_KEY, validate_purchased_login_grants
from .purchased_login_service import combine_login_notifications
from .daily_gift_state import LEDGER_KEY as DAILY_GIFT_KEY, validate_daily_gift_grants
from .vote_cooking_catalog import VoteCookingCatalog
from .vote_cooking_service import validate_vote_cooking_grants, vote_cooking_response
from .vote_tickets import validate_vote_ticket_observation
from .letstd_state import validate_letstd_grants
from .login_reward_state import (LEDGER_KEY, RESPONSE_CAPACITY, RULES_CHANGED, advance_login_rewards,
                                 initialize_login_rewards, read_current_login_rewards, read_login_rewards)
from .login_stamp_pending import (
    CAPACITY_EXCEEDED as PENDING_STAMPS_FULL, IDENTITY_FIELDS, MAX_PENDING_STAMPS, PENDING_KEY, hold_stamp,
    read_pending_stamps, release_stamp,
)
from .login_cooking_state import (
    CRYSTALS_PER_COOKING, LEDGER_KEY as COOKING_LEDGER, MAX_COOKING, POLICY_VERSION as COOKING_POLICY_VERSION,
    RULES_CHANGED as COOKING_RULES_CHANGED, SENDER_FIELD, advance_cooking, initialize_cooking, read_cooking,
    read_current_cooking, sync_cooking,
)
from .possessions import MAX_ITEM_NAME_BYTES
from .presents import GIFT_BOX_DEPENDENCIES, ITEM_CATALOG, MAX_PRESENTS, PRESENT_LIMITS
from .protocol import ProtocolRegistry, encode_response
from .public_tables import MASTER_LIMITS
from .request_fields import invalid, registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger


RESPONSE_LIMIT = 128 * 1024
HISTORY_NAMESPACE = "login_reward_history"
DELIVERY_KEY = "login_reward_delivery"
REBASE_OPERATION = "login_rewards.rebase_beginner"
STAMP_RESOLVE_OPERATION = "login_rewards.resolve_pending_stamps"
COOKING_MIGRATION = "login_cooking.migrate"
COOKING_MIGRATION_PHRASE = "MIGRATE LOGIN COOKING"
OFFLINE_COOKING_MIGRATION = ("login-cooking-migrate-plan", "login-cooking-migrate-apply")
SOURCES =("event_login_grants", "comeback_login_grants", "purchased_login_grants", "daily_gift_grants",
           "vote_cooking_grants", "vote_ticket_observation")
LETSTD_SOURCE = "letstd_grants"
DELIVERY_SOURCES = (*SOURCES, LETSTD_SOURCE)
SOURCE_SERVICES = MappingProxyType({
    "event_login_grants": "event_logins", "comeback_login_grants": "comeback_logins",
    "purchased_login_grants": "purchased_logins", "daily_gift_grants": "daily_gifts",
    "vote_cooking_grants": "vote_cooking", "vote_ticket_observation": "vote_tickets", LETSTD_SOURCE: "letstd"})
GIFT_BOX_SERVICES = ("event_logins", "comeback_logins", "purchased_logins", "daily_gifts", "letstd")
BASE_DEPENDENCIES = MappingProxyType({"items": ITEM_CATALOG, "tickets": "tickets", "cooking": "recovery_catalog"})
LISTED_SCHEMA = 8
STAMP_DESCRIPTION = "ログインボーナス"
BEGINNER_DESCRIPTION = "初心者ログインボーナス"
COOKING_DESCRIPTION = "ログインボーナス（追加報酬）"
ITEM_SENDER, AFFECTION_SENDER = "item", "affection"
COOKING_SENDERS = (ITEM_SENDER, AFFECTION_SENDER)
SENDER_POLICIES = MappingProxyType({
    ITEM_SENDER: "Configured local recovery item",
    AFFECTION_SENDER: ("Character cooking from a held female unit of the highest affection in any barracks; "
                       "affection of 100 or more ties, ties draw from the player, login day and issue, "
                       "and Anna sends without a female unit")})
# CardData.Kind is the client's GetGender(); its person event menu lists value 1 as the female units.
FEMALE_KIND = 1
# The official rule counts every affection of 100 or more as equal.
LOVE_TIE = 100
# Without a female unit the official rule has Anna send the cooking.
FALLBACK_SENDER, FALLBACK_NAME = 188, "政務官アンナ"
SENDER_SEED = "login_cooking_sender"
DEFINITION_CHANGED = "login_reward_definition_changed"
# One month of held stamps per request keeps each PPT response inside its receipt capacity.
MAX_STAMP_RELEASES = 31
# One stamp is held per login day, so 30 left is about a month before 115 refuses.
PENDING_STAMP_WARNING_DAYS = 30
# These report missing definitions with a client-error status, so they are named instead of matched by 501.
STAMP_GAPS = frozenset(("login_reward_grade_unknown", "present_template_unsupported"))
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_NO_VOTE_TICKETS = object()
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _history_invalid():
    raise ProtocolError("login_reward_history_invalid", "Stored login reward receipts are invalid", 503)


def _stamp_gap(failure):
    return failure.status == 501 or failure.code in STAMP_GAPS


@dataclass(frozen=True)
class DeliverySchema:
    version: int
    content: str
    sources: tuple[str, ...] = ()
    listed: bool = False


# Fixed schemas append one source each; the listed schema names LetsTD after the sources of any fixed schema.
DELIVERY_SCHEMAS = MappingProxyType({schema.version: schema for schema in (
    DeliverySchema(1, "content_version"),
    DeliverySchema(2, "_public_receive_content_version", SOURCES[:1]),
    DeliverySchema(3, "_comeback_receive_content_version", SOURCES[:2]),
    DeliverySchema(4, "_purchased_receive_content_version", SOURCES[:3]),
    DeliverySchema(5, "_daily_receive_content_version", SOURCES[:4]),
    DeliverySchema(6, "_cooking_receive_content_version", SOURCES[:5]),
    DeliverySchema(7, "receive_content_version", SOURCES),
    DeliverySchema(LISTED_SCHEMA, "_letstd_receive_content_version", listed=True))})


def _observed_mail_ids(observation):
    validate_vote_ticket_observation(observation)
    return []


_SOURCE_MAIL_IDS = MappingProxyType({
    "event_login_grants": validate_event_login_grants, "comeback_login_grants": validate_comeback_login_grants,
    "purchased_login_grants": validate_purchased_login_grants, "daily_gift_grants": validate_daily_gift_grants,
    "vote_cooking_grants": validate_vote_cooking_grants, "vote_ticket_observation": _observed_mail_ids,
    LETSTD_SOURCE: validate_letstd_grants})


def _source_names(sources):
    return tuple(name for name in DELIVERY_SOURCES if name in sources)


def _delivery_schema(sources):
    names = _source_names(sources)
    if len(names) != len(sources):
        return None
    listed = names[-1:] == (LETSTD_SOURCE,)
    fixed = names[:-1] if listed else names
    schema = next((schema for schema in DELIVERY_SCHEMAS.values() if not schema.listed and schema.sources == fixed), None)
    return DELIVERY_SCHEMAS[LISTED_SCHEMA] if listed and schema is not None else schema


def _last_fixed_source(names):
    # Earlier fixed sources may be empty placeholders; the last one was recorded by a configured service.
    return next((name for name in reversed(SOURCES) if name in names), None)


def _source_mail_ids(sources):
    return [mail for name in _source_names(sources) for mail in _SOURCE_MAIL_IDS[name](sources[name])]


def _response_sources(sources):
    # Daily gifts only add mail, so the 115 response has no notification for them.
    return {name: value for name, value in sources.items() if name != "daily_gift_grants"}


def _receipt_valid(entry):
    ids, ordinal, sources = entry["mail_ids"], entry["beginner_ordinal"], entry["sources"]
    if (not isinstance(ids, list) or len(ids) > MAX_PRESENTS or any(not int32(value) or value <= 0 for value in ids)
            or len(set(ids)) != len(ids) or not int32(ordinal) or not 0 <= ordinal <= 366 or (not ids and ordinal)
            or not isinstance(sources, dict) or _delivery_schema(sources) is None):
        return False
    granted = _source_mail_ids(sources)
    return len(set(granted)) == len(granted) and set(granted) <= set(ids)


@dataclass(frozen=True)
class LoginRewardRules:
    legacy_spending_period: str | None = None
    cooking_item_id: str = "item_cook_logbo_00004"
    legacy_cooking_period: str | None = None
    cooking_sender: str = ITEM_SENDER

    def __post_init__(self):
        for value in (self.legacy_spending_period, self.legacy_cooking_period):
            if value is not None:
                if not isinstance(value, str) or not MONTH_TEXT.fullmatch(value):
                    raise ValueError("Legacy reward periods must be YYYY-MM")
                date.fromisoformat(value + "-01")
        if type(self.cooking_item_id) is not str or not 0 < len(self.cooking_item_id.encode("utf-8")) <= MAX_ITEM_NAME_BYTES:
            raise ValueError("Cooking rewards require a bounded item identity")
        dumps(Container(children=(Column("ItemID", "S", (self.cooking_item_id,)),)))
        if type(self.cooking_sender) is not str or self.cooking_sender not in COOKING_SENDERS:
            raise ValueError("Cooking is sent as the configured item or by the unit of the highest affection")

    def fingerprint(self) -> dict:
        # Rules digested before the sender existed sent the item, so that default stays out of the digest.
        values = asdict(self)
        if self.cooking_sender == ITEM_SENDER:
            del values["cooking_sender"]
        return values


def cooking_sender(units, female, *, player, day, issue) -> int:
    candidates = sorted((unit for unit in units if unit["CardID"] in female), key=itemgetter("UniqueID"))
    if not candidates:
        return FALLBACK_SENDER
    affection = [min(unit["Love"], LOVE_TIE) for unit in candidates]
    best = max(affection)
    tied = [unit["CardID"] for unit, love in zip(candidates, affection) if love == best]
    return tied[int(_digest([SENDER_SEED, player, day, issue]), 16) % len(tied)]


@dataclass(frozen=True)
class CookingSenders:
    recipes: VoteCookingCatalog
    female: frozenset[int]

    @classmethod
    def verified(cls, recipes, cards):
        try:
            if type(cards) is not bytes or hashlib.sha256(cards).hexdigest() != recipes.resource_hashes["CardData"]:
                raise ValueError("Cooking senders must read the card data of the character cooking catalog")
            columns = {column.name: column for column in loads(cards, limits=MASTER_LIMITS).children
                       if isinstance(column, Column)}
            identities, kinds = columns["CardID"], columns["Kind"]
            if (identities.type is not ValueType.INT or kinds.type is not ValueType.INT
                    or len(identities.values) != len(kinds.values)):
                raise ValueError("Cooking senders require aligned native integer card kinds")
            female = frozenset(card for card, kind in zip(identities.values, kinds.values) if kind == FEMALE_KIND)
            if (FALLBACK_SENDER not in female or not female.issubset(recipes.definitions)
                    or recipes.definition(FALLBACK_SENDER).name != FALLBACK_NAME):
                raise ValueError("Cooking senders require character cooking for every female unit and the fallback")
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ProtocolError("login_cooking_sender_catalog_invalid",
                                "Cooking senders differ from their verified card data", 503) from error
        return cls(recipes, female)


class LoginRewardService:
    def __init__(self, bootstrap, players, presents, catalog, *, clock=None, rules=None, event_logins=None,
                 comeback_logins=None, purchased_logins=None, daily_gifts=None, vote_cooking=None, vote_tickets=None,
                 letstd=None):
        self.bootstrap, self.players, self.presents, self.catalog = bootstrap, players, presents, catalog
        self.clock = bootstrap.clock if clock is None else clock
        self.rules = LoginRewardRules() if rules is None else rules
        self.event_logins = event_logins
        self.comeback_logins = comeback_logins
        self.purchased_logins = purchased_logins
        self.daily_gifts = daily_gifts
        self.vote_cooking = vote_cooking
        self.vote_tickets = vote_tickets
        self.letstd = letstd
        self._pinned = MappingProxyType({})
        if not callable(self.clock) or not isinstance(self.rules, LoginRewardRules):
            raise ValueError("Login rewards require a clock and verified rules")
        self._senders = self._cooking_senders() if self.rules.cooking_sender == AFFECTION_SENDER else None
        self._beginner_total = catalog.status()["beginner_count"]
        if not int32(self._beginner_total) or not 1 <= self._beginner_total <= 366:
            raise ProtocolError("login_reward_catalog_invalid", "Beginner reward count is unsupported", 503)
        for ordinal in range(1, self._beginner_total + 1):
            catalog.beginner(ordinal).to_present_reward(BEGINNER_DESCRIPTION)
        borders = {grade.cook_gain_border for grade in catalog.grades if grade.cook_gain_border > 0}
        if len(borders) != 1:
            raise ProtocolError("login_reward_cooking_catalog_invalid", "Cooking eligibility requires one verified border", 503)
        self._cooking_border = borders.pop()
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (35, 42, 115, 129, 137)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "login_reward_history_invalid", "Stored login reward receipts are invalid",
                                     fields=("request", "content", "state", "revision", "response", "mail_ids",
                                             "beginner_ordinal", "sources"),
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid,
                                     legacy_nesting={"sources": SOURCES})
        self.content_version = self._content()
        self._record_receive_contents()
        self._ready()

    def _record_receive_contents(self):
        self._public_receive_content_version = self._public_receive_content()
        self._comeback_receive_content_version = self._comeback_receive_content()
        self._purchased_receive_content_version = self._purchased_receive_content()
        self._daily_receive_content_version = self._daily_receive_content()
        self._cooking_receive_content_version = self._cooking_receive_content()
        self.receive_content_version = self._receive_content()
        self._letstd_receive_content_version = self._letstd_receive_content()

    def _version(self, name):
        # A configured copy reads the digests its gift box pinned; the live service reads each setup digest.
        if name in self._pinned:
            return self._pinned[name]
        owner = self.presents if name in GIFT_BOX_DEPENDENCIES else self
        return getattr(getattr(owner, name), "content_version", None)

    def _content(self):
        try:
            versions = {"catalog": self.catalog.content_version, "presents": self.presents.asset_sha256,
                        **{key: self._version(name) for key, name in BASE_DEPENDENCIES.items()},
                        "recovery": self.presents.recovery.content_version}
            if any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in versions.values()):
                raise ValueError("Reward dependencies require stable fingerprints")
            content = {"policy": 2, "dependencies": versions, "rules": self.rules.fingerprint(),
                "templates": dict(self.presents.templates), "beginner_total": self._beginner_total,
                "spending_policy": POLICY_VERSION, "day": "JST_04", "missed_days": "no_backfill",
                "legacy_beginner": False, "cooking_border": self._cooking_border,
                "cooking": "one_per_login_day_up_to_monthly_quota", "delivery": "mail_only",
                "cooking_policy": COOKING_POLICY_VERSION, "cooking_cap": MAX_COOKING,
                "crystals_per_cooking": CRYSTALS_PER_COOKING,
                "cooking_date": "civil_jst_native_day", "cooking_legacy": "explicit_nonzero_baseline",
                "descriptions": [STAMP_DESCRIPTION, BEGINNER_DESCRIPTION, COOKING_DESCRIPTION]}
            if self.rules.cooking_sender == AFFECTION_SENDER:
                content["sender"] = self._sender_content()
            return _digest(content)
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("login_reward_dependencies_invalid", "Login reward dependencies are unavailable", 503) from error

    def _sender_content(self):
        # A copy rebuilt for another sender has no verified senders of its own and reads the mailbox catalog.
        recipes = self.presents.vote_cooking_catalog if self._senders is None else self._senders.recipes
        if type(recipes.content_version) is not str or not _HASH.fullmatch(recipes.content_version):
            raise ValueError("Character cooking requires a stable fingerprint")
        return {"recipes": recipes.content_version, "candidates": "held_units_in_every_barracks",
                "female_kind": FEMALE_KIND, "love_tie": LOVE_TIE, "ties": "seeded_by_player_login_day_and_issue",
                "fallback": FALLBACK_SENDER, "mail": "type21_card_id"}

    def _cooking_senders(self):
        recipes, provider = self.presents.vote_cooking_catalog, getattr(self.players, "master_provider", None)
        try:
            if recipes is None or not recipes.is_ready() or not callable(provider):
                raise ValueError("Character cooking or its card data is unavailable")
            cards = provider("CardData")
        except (KeyError, OSError, ValueError) as error:
            raise ProtocolError("login_cooking_sender_unavailable",
                                "Cooking by the unit of the highest affection requires verified character cooking",
                                503) from error
        return CookingSenders.verified(recipes, cards)

    def _senders_ready(self):
        return self._senders is None or (self.presents.vote_cooking_catalog is self._senders.recipes
                                         and self._senders.recipes.is_ready())

    def _ready(self):
        self.presents._ready()
        if (not self.catalog.is_ready() or self._content() != self.content_version
                or not self.presents.tickets.is_ready() or not self._senders_ready()):
            raise ProtocolError("login_reward_dependencies_changed", "Login reward definitions changed after setup", 503)
        self.presents.recovery._ready()
        self.presents.recovery_catalog.definition(22, 1, self.rules.cooking_item_id)

    @property
    def cooking_border(self) -> int:
        return self._cooking_border

    @property
    def beginner_total(self) -> int:
        return self._beginner_total

    def _public_receive_content(self):
        if self.event_logins is None:
            return self.content_version
        version = self._version("event_logins")
        if (type(version) is not str or not _HASH.fullmatch(version)
                or self.event_logins.presents is not self.presents):
            raise ProtocolError("login_event_dependencies_invalid", "Event login rewards require the same verified mailbox", 503)
        return _digest({"login": self.content_version, "event_logins": version, "delivery_schema": 2,
                        "order": "ordinary_then_event_campaign_id"})

    def _comeback_receive_content(self):
        previous = self._public_receive_content()
        if self.comeback_logins is None:
            return previous
        version = self._version("comeback_logins")
        if (type(version) is not str or not _HASH.fullmatch(version)
                or self.comeback_logins.presents is not self.presents):
            raise ProtocolError("login_comeback_dependencies_invalid", "Comeback rewards require the same verified mailbox", 503)
        return _digest({"previous": previous, "comeback_logins": version, "delivery_schema": 3,
                        "order": "ordinary_then_public_then_comeback"})

    def _purchased_receive_content(self):
        previous = self._comeback_receive_content()
        if self.purchased_logins is None:
            return previous
        version = self._version("purchased_logins")
        if (type(version) is not str or not _HASH.fullmatch(version)
                or self.purchased_logins.presents is not self.presents):
            raise ProtocolError("login_purchased_dependencies_invalid", "Purchased login rewards require the same mailbox", 503)
        return _digest({"previous": previous, "purchased_logins": version, "delivery_schema": 4,
                        "order": "ordinary_then_public_then_comeback_then_purchased"})

    def _daily_receive_content(self):
        previous = self._purchased_receive_content()
        if self.daily_gifts is None:
            return previous
        version = self._version("daily_gifts")
        if (type(version) is not str or not _HASH.fullmatch(version)
                or self.daily_gifts.presents is not self.presents):
            raise ProtocolError("login_daily_gift_dependencies_invalid", "Daily gifts require the same verified mailbox", 503)
        return _digest({"previous": previous, "daily_gifts": version, "delivery_schema": 5,
                        "order": "ordinary_then_public_then_comeback_then_purchased_then_daily_gift"})

    def _cooking_receive_content(self):
        previous = self._daily_receive_content()
        if self.vote_cooking is None:
            return previous
        version = self._version("vote_cooking")
        if (type(version) is not str or not _HASH.fullmatch(version)
                or self.vote_cooking.presents is not self.presents):
            raise ProtocolError("login_vote_cooking_dependencies_invalid", "Vote cooking requires the same verified mailbox", 503)
        return _digest({"previous": previous, "vote_cooking": version, "delivery_schema": 6,
                        "order": "ordinary_then_public_then_comeback_then_purchased_then_daily_gift_then_vote_cooking"})

    def _receive_content(self):
        previous = self._cooking_receive_content()
        if self.vote_tickets is None:
            return previous
        version = self._version("vote_tickets")
        if (type(version) is not str or not _HASH.fullmatch(version)
                or self.vote_tickets.possessions is not self.presents.possessions):
            raise ProtocolError("login_vote_ticket_dependencies_invalid", "Vote tickets require the same verified inventory", 503)
        return _digest({"previous": previous, "vote_tickets": version, "delivery_schema": 7,
                        "order": "login_rewards_then_daily_vote_ticket", "response": "full_possession_snapshot"})

    def _letstd_receive_content(self):
        previous = self._receive_content()
        if self.letstd is None:
            return previous
        version = self._version("letstd")
        if (type(version) is not str or not _HASH.fullmatch(version)
                or self.letstd.presents is not self.presents):
            raise ProtocolError("login_letstd_dependencies_invalid", "LetsTD rewards require the same verified mailbox", 503)
        return _digest({"previous": previous, "letstd": version, "delivery_schema": LISTED_SCHEMA,
                        "order": "login_rewards_then_daily_vote_ticket_then_letstd", "sources": "listed"})

    def _receipt_content(self, sources):
        return getattr(self, _delivery_schema(sources).content)

    def _attached(self):
        return {name for name in SOURCE_SERVICES.values() if getattr(self, name) is not None}

    def _detached(self, absent):
        detached = copy(self)
        for name in absent:
            setattr(detached, name, None)
        detached._record_receive_contents()
        return detached

    def _pinned_to(self, versions):
        pinned = copy(self)
        pinned._pinned = MappingProxyType(versions)
        pinned.content_version = pinned._content()
        return pinned

    def _configured(self, gift_box):
        # Each service digests the gift box once; every attached subset of this configuration reuses the result.
        configured = self._pinned_to({**gift_box, **{name: service.content_version_for(gift_box)
            for name in GIFT_BOX_SERVICES if (service := getattr(self, name)) is not None}})
        configured._record_receive_contents()
        return configured

    def _gift_boxes(self):
        # A gift box without a base dependency had no service, so it never committed a receipt.
        return [gift_box for gift_box in self.presents.gift_box_configurations()
                if all(gift_box[name] is not None for name in BASE_DEPENDENCIES.values())]

    def _with_sender(self, sender):
        switched = copy(self)
        switched.rules = replace(self.rules, cooking_sender=sender)
        try:
            switched.content_version = switched._content()
            switched._record_receive_contents()
        except ProtocolError:
            return None
        return switched

    def _sender_variants(self):
        yield self
        # A receipt committed before the cooking sender switched keeps the digest of the earlier sender.
        for sender in COOKING_SENDERS:
            if sender != self.rules.cooking_sender and (switched := self._with_sender(sender)) is not None:
                yield switched

    def _configurations(self):
        # The first gift box is the current wiring, which the live service already answers for.
        yield from self._sender_variants()
        for gift_box in self._gift_boxes()[1:]:
            yield from self._configured(gift_box)._sender_variants()

    def _present_choices(self, source, sources, last):
        if source not in sources:
            return ((),)
        service = SOURCE_SERVICES[source]
        attached = () if getattr(self, service) is None else (service,)
        # LetsTD is listed only while its service exists; the last fixed source was recorded by its own service.
        if source in (last, LETSTD_SOURCE):
            return (attached,) if attached else ()
        return (attached, ()) if attached else ((),)

    def _committed(self, sources, content):
        # Earlier fixed sources keep placeholders for absent services, so only the digest names the committed subset.
        last, attached = _last_fixed_source(sources), self._attached()
        subsets = [attached.difference(*present)
                   for present in product(*(self._present_choices(name, sources, last) for name in DELIVERY_SOURCES))]
        if not subsets:
            return None
        for configured in self._configurations():
            for absent in subsets:
                candidate = configured._detached(absent) if absent else configured
                if candidate._receipt_content(sources) == content:
                    return candidate
        return None

    def _events_ready(self):
        if self.event_logins is not None:
            self.event_logins._ready()
        if self._public_receive_content() != self._public_receive_content_version:
            raise ProtocolError("login_event_dependencies_changed", "Event login dependencies changed after setup", 503)

    def _comeback_ready(self):
        if self.comeback_logins is not None:
            self.comeback_logins._ready()
        if self._comeback_receive_content() != self._comeback_receive_content_version:
            raise ProtocolError("login_comeback_dependencies_changed", "Comeback dependencies changed after setup", 503)

    def _purchased_ready(self):
        if self.purchased_logins is not None:
            self.purchased_logins._ready()
        if self._purchased_receive_content() != self._purchased_receive_content_version:
            raise ProtocolError("login_purchased_dependencies_changed", "Purchased login dependencies changed after setup", 503)

    def _daily_gift_ready(self):
        if self.daily_gifts is not None:
            self.daily_gifts._ready()
        if self._daily_receive_content() != self._daily_receive_content_version:
            raise ProtocolError("login_daily_gift_dependencies_changed", "Daily gift dependencies changed after setup", 503)

    def _vote_cooking_ready(self):
        if self.vote_cooking is not None:
            self.vote_cooking._ready()
        if self._cooking_receive_content() != self._cooking_receive_content_version:
            raise ProtocolError("login_vote_cooking_dependencies_changed", "Vote cooking dependencies changed after setup", 503)

    def _vote_tickets_ready(self):
        if self.vote_tickets is not None:
            self.vote_tickets._ready()
        if self._receive_content() != self.receive_content_version:
            raise ProtocolError("login_vote_ticket_dependencies_changed", "Vote ticket dependencies changed after setup", 503)

    def _letstd_ready(self):
        if self.letstd is not None:
            self.letstd._ready()
        if self._letstd_receive_content() != self._letstd_receive_content_version:
            raise ProtocolError("login_letstd_dependencies_changed", "LetsTD dependencies changed after setup", 503)

    @staticmethod
    def _recorded_sources(marker):
        version = marker.get("schema_version")
        schema = DELIVERY_SCHEMAS.get(version) if type(version) is int else None
        if schema is None or not schema.listed:
            return () if schema is None else schema.sources
        listed = marker.get("sections")
        if not isinstance(listed, list) or _delivery_schema(listed) is not schema or list(_source_names(listed)) != listed:
            _history_invalid()
        return tuple(listed)

    def _initialize_events(self, state):
        if self.event_logins is not None:
            self._events_ready()
            state = self.event_logins.initialize(state)
        elif EVENT_LOGIN_KEY in state:
            raise ProtocolError("event_login_service_unavailable", "Existing event login history requires its service", 503)
        if self.comeback_logins is not None:
            self._comeback_ready()
            state = self.comeback_logins.initialize(state)
        elif COMEBACK_LOGIN_KEY in state:
            raise ProtocolError("comeback_login_service_unavailable", "Existing comeback history requires its service", 503)
        if self.purchased_logins is not None:
            self._purchased_ready()
            state = self.purchased_logins.initialize(state)
        elif PURCHASED_LOGIN_KEY in state:
            raise ProtocolError("purchased_login_service_unavailable", "Existing purchased login history requires its service", 503)
        if self.daily_gifts is not None:
            self._daily_gift_ready()
            state = self.daily_gifts.initialize(state)
        elif DAILY_GIFT_KEY in state:
            raise ProtocolError("daily_gift_service_unavailable", "Existing daily gift history requires its service", 503)
        marker = state.get(DELIVERY_KEY, {})
        if not isinstance(marker, dict):
            _history_invalid()
        recorded = self._recorded_sources(marker)
        if self.vote_cooking is not None:
            self._vote_cooking_ready()
            state = self.vote_cooking.initialize(state)
        elif "vote_cooking" in state or _last_fixed_source(recorded) == "vote_cooking_grants":
            raise ProtocolError("vote_cooking_service_unavailable", "Existing vote cooking history requires its service", 503)
        if "vote_cooking_grants" in recorded:
            grants = marker.get("vote_cooking_grants")
            validate_vote_cooking_grants(grants)
            if self.vote_cooking is not None:
                self.vote_cooking.validate_receipt(state, grants)
            elif grants:
                raise ProtocolError("vote_cooking_service_unavailable", "Vote cooking grants require their service", 503)
        if self.vote_tickets is not None:
            self._vote_tickets_ready()
            state = self.vote_tickets.initialize(state)
        elif "vote_ticket_observation" in recorded:
            raise ProtocolError("vote_ticket_service_unavailable", "Login ticket history requires its service", 503)
        if "vote_ticket_observation" in recorded:
            if "vote_ticket_observation" not in marker:
                _history_invalid()
            self.vote_tickets.validate_observation(state, marker["vote_ticket_observation"])
        if LETSTD_SOURCE in recorded:
            # Structure only: a later rules switch must not block startup or the monthly refresh.
            if LETSTD_SOURCE not in marker:
                _history_invalid()
            validate_letstd_grants(marker[LETSTD_SOURCE])
        return state

    def handlers(self):
        return {115: self.receive}

    def seed_new(self, state, now):
        self._ready()
        updated = initialize_spending(state, now, origin=SPENDING_NEW_LOCAL)
        updated = initialize_login_rewards(updated, now, new_account=True, beginner_count=self._beginner_total)
        return self._initialize_events(initialize_cooking(updated, border=self._cooking_border))

    def initialize(self, state, now=None):
        self._ready()
        if now is None and ("crystal_spending" not in state or LEDGER_KEY not in state):
            now = self.clock()
        updated = initialize_spending(state, now, baseline_period=self.rules.legacy_spending_period)
        updated = initialize_login_rewards(updated, now, beginner_count=self._beginner_total)
        read_pending_stamps(updated)
        return self._initialize_events(initialize_cooking(updated, border=self._cooking_border,
                                                          legacy_period=self.rules.legacy_cooking_period))

    def prepare_month(self, state, now):
        updated = self.initialize(state, now)
        ledger = read_spending(updated)
        period, timestamp = _clock(now)
        if timestamp < ledger["last_time"]:
            raise ProtocolError("crystal_spending_clock_backwards", "Spending clock precedes the saved instant", 409)
        if period != ledger["period"]:
            updated = settle_spending(updated, now)
        # Only 115 refuses a ledger at another border, so 33/35 still settle the month and leave cooking to its migration.
        if read_cooking(updated)["border"] != self._cooking_border:
            return updated
        return sync_cooking(updated, now, border=self._cooking_border)

    def current_ledger(self, state):
        return read_current_login_rewards(state, beginner_count=self._beginner_total)

    def beginner_rules(self, state):
        # Startup freezes a save without the ledger at the current total, so it has nothing to rebase.
        saved = read_login_rewards(state)["beginner_total"] if LEDGER_KEY in state else None
        rules = {"saved_total": saved, "current_total": self._beginner_total,
                 "changed": saved is not None and saved != self._beginner_total}
        if rules["changed"]:
            rules.update(refused_operations=sorted(self.handlers()), repair=REBASE_OPERATION, client=_REFUSAL_AFTERMATH)
        return rules

    def current_cooking(self, state):
        return read_current_cooking(state, border=self._cooking_border)

    def cooking_rules(self, state):
        saved = read_cooking(state)["border"] if COOKING_LEDGER in state else None
        rules = {"saved_border": saved, "current_border": self._cooking_border,
                 "changed": saved is not None and saved != self._cooking_border}
        if rules["changed"]:
            rules.update(refused_operations=sorted(self.handlers()), repair=COOKING_MIGRATION,
                         offline_repair=list(OFFLINE_COOKING_MIGRATION), client=_REFUSAL_AFTERMATH)
        return rules

    def pending_stamps(self, state):
        count = len(read_pending_stamps(state))
        remaining = MAX_PENDING_STAMPS - count
        report = {"count": count, "capacity": MAX_PENDING_STAMPS, "remaining": remaining,
                  "warning": remaining < PENDING_STAMP_WARNING_DAYS}
        if not remaining:
            report.update(refused_operations=sorted(self.handlers()), code=PENDING_STAMPS_FULL)
        return report

    def saved_report(self, state):
        return {**self.beginner_rules(state), "cooking": self.cooking_rules(state),
                "pending_stamps": self.pending_stamps(state)}

    def status(self):
        try:
            self._ready()
            self._events_ready()
            self._comeback_ready()
            self._purchased_ready()
            self._daily_gift_ready()
            self._vote_cooking_ready()
            self._vote_tickets_ready()
            self._letstd_ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "implemented_operations": [115],
                "content_version": self.content_version, "catalog": self.catalog.status(),
                "receive_content_version": self.receive_content_version,
                "event_logins": self.event_logins.status() if self.event_logins is not None else None,
                "comeback_logins": self.comeback_logins.status() if self.comeback_logins is not None else None,
                "purchased_logins": self.purchased_logins.status() if self.purchased_logins is not None else None,
                "daily_gifts": self.daily_gifts.status() if self.daily_gifts is not None else None,
                "vote_cooking": self.vote_cooking.status() if self.vote_cooking is not None else None,
                "vote_tickets": self.vote_tickets.status() if self.vote_tickets is not None else None,
                "letstd": self.letstd.status() if self.letstd is not None else None,
                "rules": asdict(self.rules), "history_limit": HISTORY_LIMIT,
                "processed_request_limit": PROCESSED_LIMIT, "request_order": REQUEST_ORDER,
                "beginner_total": self._beginner_total,
                "day_boundary": "JST 04:00", "delivery": "mail",
                "legacy_policy": "Preserve stamp history; no beginner or missed-day backfill",
                "cooking": {"sender": self.rules.cooking_sender, "item_id": self.rules.cooking_item_id,
                            "border": self._cooking_border, "monthly_cap": MAX_COOKING, "daily_cap": 1,
                            "kind_policy": SENDER_POLICIES[self.rules.cooking_sender]},
                "pending_stamps": {"state_key": PENDING_KEY, "limit": MAX_PENDING_STAMPS,
                                   "releases_per_request": MAX_STAMP_RELEASES,
                                   "policy": "A stamp whose month table, pool or reward support is missing waits at its "
                                             "claimed grade while other sources settle; it is mailed once on a later 115"},
                "unavailable": ["Stamp rewards for months or pools the resources lack (held as pending)",
                                "Official cooking appearance selection" if self._senders is None
                                else "Captured official cooking mail form"],
                "native_tested": False}

    def _receipts(self, state, revision):
        return self._ledger.read(state, revision)

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        row = self.bootstrap._rows(candidate, "game_param2")
        encode_response(self._profiles[35], dumps(Container(children=self.bootstrap.columns("game_param2", row))))
        encode_response(self._profiles[42], dumps(Container(children=(
            self.bootstrap._system(candidate), self.presents.response(state))), limits=PRESENT_LIMITS))

    def _stamp_today(self, state, grade, advance):
        definition = None
        try:
            stamp = self.catalog.stamp(grade, advance.month, advance.ordinal)
            definition = stamp.definition
            reward = stamp.to_present_reward(STAMP_DESCRIPTION)
            self.presents.preview([reward], len(self.presents.rows(state)))
        except ProtocolError as failure:
            if not _stamp_gap(failure):
                raise
            return hold_stamp(state, day=advance.day, grade=grade, month=advance.month, ordinal=advance.ordinal,
                              definition=definition, reason=failure.code), []
        return state, [reward]

    def _cooking_sender(self, state, player, day, issue):
        return cooking_sender(self.players.validate(state)["units"], self._senders.female,
                              player=player, day=day, issue=issue)

    def _cooking_reward(self, state):
        if self._senders is None:
            return {"kind": "charisma_recovery", "item_id": self.rules.cooking_item_id, "description": COOKING_DESCRIPTION}
        return {"kind": "vote_cooking", "card_id": read_cooking(state)[SENDER_FIELD], "description": COOKING_DESCRIPTION}

    def _release_held(self, profile, state, held, base_ids, beginner_ordinal):
        released = []
        for entry in held[:MAX_STAMP_RELEASES]:
            identity = {name: entry[name] for name in IDENTITY_FIELDS}
            try:
                stamp = self.catalog.stamp(entry["grade"], entry["month"], entry["ordinal"])
                if entry["definition"] not in (None, stamp.definition):
                    state = hold_stamp(state, **identity, reason=DEFINITION_CHANGED)
                    continue
                candidate, additions = self.presents.enqueue(state, [stamp.to_present_reward(STAMP_DESCRIPTION)])
                ids = [row["PresentID"] for row in additions]
                candidate, _ = release_stamp(candidate, entry["day"])
                self._preflight(profile, candidate)
                self._response(profile, candidate, [*base_ids, *released, *ids], beginner_ordinal,
                               comeback_login_grants=[] if self.comeback_logins is not None else None)
            except ProtocolError as failure:
                full = failure.code in CAPACITY_ERRORS
                if not full and not _stamp_gap(failure):
                    raise
                state = hold_stamp(state, **identity, reason=failure.code)
                if full:
                    break
                continue
            state, released = candidate, [*released, *ids]
        return state, released

    @staticmethod
    def _delivery(request, mail_ids, beginner_ordinal, base_ids, sources):
        schema = _delivery_schema(sources)
        delivery = {"schema_version": schema.version, "request": request, "mail_ids": mail_ids,
                    "beginner_ordinal": beginner_ordinal}
        if sources:
            delivery["base_mail_ids"] = base_ids
        if schema.listed:
            delivery["sections"] = list(_source_names(sources))
        delivery.update(deepcopy(sources))
        return delivery

    @staticmethod
    def _validate_delivery(state, request, mail_ids, beginner_ordinal, sources):
        delivery, schema, names = state.get(DELIVERY_KEY), _delivery_schema(sources), _source_names(sources)
        expected = {"schema_version", "request", "mail_ids", "beginner_ordinal", *names}
        if names:
            expected.add("base_mail_ids")
        if schema is not None and schema.listed:
            expected.add("sections")
        if (schema is None or not isinstance(delivery, dict)
                or set(delivery) != expected
                or type(delivery["schema_version"]) is not int or delivery["schema_version"] != schema.version
                or schema.listed and delivery["sections"] != list(names)
                or type(delivery["request"]) is not str or not _HASH.fullmatch(delivery["request"])
                or not isinstance(delivery["mail_ids"], list) or len(delivery["mail_ids"]) > MAX_PRESENTS
                or any(not int32(value) or value <= 0 for value in delivery["mail_ids"])
                or len(set(delivery["mail_ids"])) != len(delivery["mail_ids"])
                or not int32(delivery["beginner_ordinal"]) or not 0 <= delivery["beginner_ordinal"] <= 366
                or delivery["request"] != request or delivery["mail_ids"] != mail_ids
                or delivery["beginner_ordinal"] != beginner_ordinal
                or any(delivery[name] != sources[name] for name in names)):
            _history_invalid()
        if names:
            source_ids = _source_mail_ids(sources)
            base = delivery["base_mail_ids"]
            if (not isinstance(base, list)
                    or len(base) > MAX_PRESENTS or any(not int32(value) or value <= 0 for value in base)
                    or len(set(base)) != len(base) or len(set(source_ids)) != len(source_ids) or set(base) & set(source_ids)
                    or [*base, *source_ids] != mail_ids or beginner_ordinal and not base):
                _history_invalid()

    def _sources_ready(self, sources):
        last = _last_fixed_source(sources)
        if "event_login_grants" in sources:
            if self.event_logins is None and sources["event_login_grants"]:
                raise ProtocolError("event_login_service_unavailable", "Event login receipt requires its service", 503)
            self._events_ready()
        if "comeback_login_grants" in sources:
            if self.comeback_logins is None and sources["comeback_login_grants"]:
                raise ProtocolError("comeback_login_service_unavailable", "Comeback receipt requires its service", 503)
            self._comeback_ready()
        if "purchased_login_grants" in sources:
            if self.purchased_logins is None and sources["purchased_login_grants"]:
                raise ProtocolError("purchased_login_service_unavailable", "Purchased login receipt requires its service", 503)
            self._purchased_ready()
        if "daily_gift_grants" in sources:
            if self.daily_gifts is None and (sources["daily_gift_grants"] or last == "daily_gift_grants"):
                raise ProtocolError("daily_gift_service_unavailable", "Daily gift receipt requires its service", 503)
            self._daily_gift_ready()
        if "vote_cooking_grants" in sources:
            if self.vote_cooking is None and (sources["vote_cooking_grants"] or last == "vote_cooking_grants"):
                raise ProtocolError("vote_cooking_service_unavailable", "Vote cooking receipt requires its service", 503)
            self._vote_cooking_ready()
        if "vote_ticket_observation" in sources:
            if self.vote_tickets is None:
                raise ProtocolError("vote_ticket_service_unavailable", "Login ticket receipt requires its service", 503)
            self._vote_tickets_ready()
        if LETSTD_SOURCE in sources:
            if self.letstd is None and sources[LETSTD_SOURCE]:
                raise ProtocolError("letstd_unavailable", "LetsTD receipt requires its service", 503)
            self._letstd_ready()

    def _validate_source_receipts(self, state, sources):
        for name, owner in (("event_login_grants", self.event_logins), ("comeback_login_grants", self.comeback_logins),
                            ("purchased_login_grants", self.purchased_logins), ("daily_gift_grants", self.daily_gifts),
                            ("vote_cooking_grants", self.vote_cooking), (LETSTD_SOURCE, self.letstd)):
            if name in sources and owner is not None:
                owner.validate_receipt(state, sources[name])

    def _letstd_response(self, grants):
        validate_letstd_grants(grants)
        if self.letstd is None:
            if grants:
                raise ProtocolError("letstd_unavailable", "LetsTD notification requires its service", 503)
            return None
        return self.letstd.response(grants)

    def _response(self, profile, state, mail_ids, beginner_ordinal, event_login_grants=None,
                  comeback_login_grants=None, purchased_login_grants=None, vote_cooking_grants=None,
                  vote_ticket_observation=_NO_VOTE_TICKETS, letstd_grants=None, *, legacy_possessions=False):
        progress = read_login_rewards(state)
        read_cooking(state)
        row = state["tables"]["game_param2"][0]
        pairs = tuple(zip(("A5", "A6", "C1", "CC", "CD"), (str(row[name]) for name in (
            "LastLoginDate", "LogInStampCount", "StampCardGrade", "HomeCookingReceivedAmount", "HomeCookingReceivedDate"))))
        if comeback_login_grants is not None and self.comeback_logins is not None:
            comeback_pairs = self.comeback_logins.pgp2_pairs(state)
            if comeback_pairs != ((DATE_FIELD, row[DATE_FIELD]), (DAYS_FIELD, str(row[DAYS_FIELD]))):
                raise ProtocolError("login_comeback_projection_invalid", "Comeback projection differs from saved parameters", 503)
            pairs += tuple(comeback_pairs)
        children = [self.bootstrap._system(profile), Container("PGP2", (
            Column("A1", "S", tuple(key for key, _ in pairs)), Column("A2", "S", tuple(value for _, value in pairs))))]
        if mail_ids:
            children.append(self.presents.response(state, mail_ids))
        if beginner_ordinal:
            if beginner_ordinal != progress["beginner_claimed"] or beginner_ordinal > self._beginner_total:
                _history_invalid()
            children.append(Container("BBNS", (Column("GetCount", "I", (beginner_ordinal,)),
                Column("GetRemain", "I", (self._beginner_total - beginner_ordinal,)))))
        if purchased_login_grants is not None:
            notification = combine_login_notifications(event_login_grants or [], comeback_login_grants or [],
                                                        purchased_login_grants)
            if notification.children[0].values:
                children.append(notification)
        elif comeback_login_grants is not None:
            notification = combine_event_login_notifications(event_login_grants or [], comeback_login_grants)
            if notification.children[0].values:
                children.append(notification)
        elif event_login_grants:
            children.append(self.event_logins.response(event_login_grants))
        if vote_cooking_grants is not None:
            if self.vote_cooking is None and (vote_cooking_grants or vote_ticket_observation is _NO_VOTE_TICKETS):
                raise ProtocolError("vote_cooking_service_unavailable", "Vote cooking notification requires its service", 503)
            recipes = getattr(self.vote_cooking, "recipes", None)
            expected = vote_cooking_response(vote_cooking_grants, receive_text_id=getattr(recipes, "receive_text_id", None))
            notification = expected if self.vote_cooking is None else self.vote_cooking.response(vote_cooking_grants)
            if notification != expected:
                raise ProtocolError("login_vote_cooking_response_invalid", "Vote cooking notification differs from its grants", 503)
            children.append(notification)
        if vote_ticket_observation is not _NO_VOTE_TICKETS:
            if self.vote_tickets is None:
                raise ProtocolError("vote_ticket_service_unavailable", "Login ticket response requires its service", 503)
            self._vote_tickets_ready()
            self.vote_tickets.validate_observation(state, vote_ticket_observation, exact=True)
            inventory = self.vote_tickets.possessions.response(state, legacy=legacy_possessions)
            candidate = replace(profile, state=state)
            startup = self.bootstrap._possessions(candidate)
            if startup != self.vote_tickets.possessions.startup_response(state):
                raise ProtocolError("login_vote_ticket_projection_invalid", "Startup inventory differs from login tickets", 503)
            encode_response(self._profiles[129], dumps(Container(children=(self.bootstrap._system(candidate), startup))))
            encode_response(self._profiles[137], self.bootstrap._configuration_response(candidate))
            children.append(inventory)
        if letstd_grants is not None:
            notice = self._letstd_response(letstd_grants)
            if notice is not None:
                children.append(notice)
        payload = dumps(Container(children=tuple(children)), limits=PRESENT_LIMITS)
        if len(payload) > RESPONSE_LIMIT:
            raise ProtocolError(RESPONSE_CAPACITY, "Login reward response exceeds its receipt capacity", 503)
        encode_response(self._profiles[115], payload)
        return payload

    def _advance(self, state, now):
        try:
            return advance_login_rewards(state, now, beginner_count=self._beginner_total)
        except ProtocolError as error:
            if error.code == RULES_CHANGED:
                log.warning("Refused request 115 with %s: the save froze %d beginner rewards but the rules now give %d; "
                            "the native client %s, so run %s on the console", error.code,
                            read_login_rewards(state)["beginner_total"], self._beginner_total, _REFUSAL_AFTERMATH,
                            REBASE_OPERATION)
            raise

    def _advance_cooking(self, state, now, *, eligible, sender):
        try:
            return advance_cooking(state, now, border=self._cooking_border, eligible=eligible, sender=sender)
        except ProtocolError as error:
            if error.code == COOKING_RULES_CHANGED:
                log.warning("Refused request 115 with %s: the save froze cooking border %d but the rules now use %d; "
                            "the native client %s, so run %s on the console and confirm it with %s, "
                            "or run %s and %s while the server is stopped", error.code,
                            read_cooking(state)["border"], self._cooking_border, _REFUSAL_AFTERMATH,
                            COOKING_MIGRATION, COOKING_MIGRATION_PHRASE, *OFFLINE_COOKING_MIGRATION)
            raise

    def receive(self, envelope, store):
        self._ready()
        request = registered(envelope, {115}, {"SHOP", "NUM"}, {"SHOP", "NUM"})
        if scalar(request["SHOP"], "I") != 0 or scalar(request["NUM"], "I") != 0:
            invalid("Login rewards require the native zero SHOP and NUM values")
        key = hashlib.sha256(f"115:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        now = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            self.bootstrap._rows(profile, "game_param2")
            history = self._receipts(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                sources = cached["sources"]
                self._sources_ready(sources)
                committed = self._committed(sources, cached["content"]) if cached["revision"] == profile.revision else None
                if committed is None:
                    raise ProtocolError("login_reward_retry_stale", "Login rewards changed after this request", 409)
                if cached["state"] != _state_digest(profile.state):
                    _history_invalid()
                self._validate_delivery(profile.state, key, cached["mail_ids"], cached["beginner_ordinal"], sources)
                committed._validate_source_receipts(profile.state, sources)
                read_spending(profile.state)
                self._preflight(profile, profile.state)
                saved = cached["response"].encode("utf-8")
                rebuild = partial(committed._response, profile, profile.state, cached["mail_ids"],
                                  cached["beginner_ordinal"], **_response_sources(sources))
                # Receipts saved before every POSSESSION carried Enabled keep their two-column table.
                if rebuild() != saved and rebuild(legacy_possessions=True) != saved:
                    _history_invalid()
                encode_response(envelope.profile, saved)
                return saved
            if self._ledger.processed(history, key):
                log.warning("Refused request 115: it repeats a processed request whose receipt was evicted")
                raise ProtocolError("login_reward_request_stale", "Login reward request was already processed", 409)
            self._events_ready()
            self._comeback_ready()
            self._purchased_ready()
            self._daily_gift_ready()
            self._vote_cooking_ready()
            self._vote_tickets_ready()
            self._letstd_ready()
            if now is None:
                now = instant(self.clock())
            initialized = self.initialize(profile.state, now)
            if self.comeback_logins is not None:
                initialized = self.comeback_logins.observe(initialized, now)
            updated, advance = self._advance(initialized, now)
            updated = settle_spending(updated, now)
            sender = None if self._senders is None else partial(self._cooking_sender, updated, profile.player_id)
            updated, cooking_granted = self._advance_cooking(updated, now, eligible=advance.new_day, sender=sender)
            held = read_pending_stamps(updated)
            additions = []
            if advance.new_day:
                previous = min(read_spending(updated)["previous_spent"], PROJECTION_CAP)
                grade = self.catalog.grade(previous, now)
                updated, rewards = self._stamp_today(updated, grade, advance)
                if advance.beginner_ordinal:
                    rewards.append(self.catalog.beginner(advance.beginner_ordinal).to_present_reward(BEGINNER_DESCRIPTION))
                if cooking_granted:
                    rewards.append(self._cooking_reward(updated))
                updated, additions = self.presents.enqueue(updated, rewards)
            today_ids = [row["PresentID"] for row in additions]
            updated, released = self._release_held(profile, updated, held, today_ids, advance.beginner_ordinal)
            base_ids = [*today_ids, *released]
            ids, event_grants = base_ids, None
            if self.event_logins is not None:
                self._preflight(profile, updated)
                self._response(profile, updated, base_ids, advance.beginner_ordinal,
                               comeback_login_grants=[] if self.comeback_logins is not None else None)

                def validate(candidate, grants):
                    combined = [*base_ids, *validate_event_login_grants(grants)]
                    self._preflight(profile, candidate)
                    self._response(profile, candidate, combined, advance.beginner_ordinal, grants,
                                   [] if self.comeback_logins is not None else None)

                updated, event_grants = self.event_logins.settle(updated, now, validate=validate)
                self.event_logins.validate_receipt(updated, event_grants)
                ids = [*base_ids, *validate_event_login_grants(event_grants)]
            comeback_grants = None
            if self.comeback_logins is not None:
                event_grants = event_grants or []
                public_ids = list(ids)

                def validate_comeback(candidate, grants):
                    combined = [*public_ids, *validate_comeback_login_grants(grants)]
                    self._preflight(profile, candidate)
                    self._response(profile, candidate, combined, advance.beginner_ordinal, event_grants, grants)

                updated, comeback_grants = self.comeback_logins.settle(updated, now, validate=validate_comeback)
                self.comeback_logins.validate_receipt(updated, comeback_grants)
                ids = [*public_ids, *validate_comeback_login_grants(comeback_grants)]
            purchased_grants = None
            if self.purchased_logins is not None:
                event_grants, comeback_grants = event_grants or [], comeback_grants or []
                earlier_ids = list(ids)

                def validate_purchased(candidate, grants):
                    combined = [*earlier_ids, *validate_purchased_login_grants(grants)]
                    self._preflight(profile, candidate)
                    self._response(profile, candidate, combined, advance.beginner_ordinal,
                                   event_grants, comeback_grants, grants)

                updated, purchased_grants = self.purchased_logins.settle(updated, now, validate=validate_purchased)
                self.purchased_logins.validate_receipt(updated, purchased_grants)
                ids = [*earlier_ids, *validate_purchased_login_grants(purchased_grants)]
            daily_grants = None
            if self.daily_gifts is not None:
                event_grants, comeback_grants, purchased_grants = event_grants or [], comeback_grants or [], purchased_grants or []
                earlier_ids = list(ids)

                def validate_daily(candidate, grants):
                    combined = [*earlier_ids, *validate_daily_gift_grants(grants)]
                    self._preflight(profile, candidate)
                    self._response(profile, candidate, combined, advance.beginner_ordinal,
                                   event_grants, comeback_grants, purchased_grants)

                updated, daily_grants = self.daily_gifts.settle(updated, now, validate=validate_daily)
                self.daily_gifts.validate_receipt(updated, daily_grants)
                ids = [*earlier_ids, *validate_daily_gift_grants(daily_grants)]
            vote_grants = None
            if self.vote_cooking is not None:
                event_grants, comeback_grants = event_grants or [], comeback_grants or []
                purchased_grants, daily_grants = purchased_grants or [], daily_grants or []
                earlier_ids = list(ids)
                updated, vote_grants = self.vote_cooking.settle(updated, now)
                self.vote_cooking.validate_receipt(updated, vote_grants)
                ids = [*earlier_ids, *validate_vote_cooking_grants(vote_grants)]
            ticket_observation = _NO_VOTE_TICKETS
            if self.vote_tickets is not None:
                event_grants, comeback_grants = event_grants or [], comeback_grants or []
                purchased_grants, daily_grants, vote_grants = purchased_grants or [], daily_grants or [], vote_grants or []
                updated, ticket_observation = self.vote_tickets.observe(updated, now)
                self.vote_tickets.validate_observation(updated, ticket_observation, exact=True)
            sources = {name: deepcopy(value) for name, value in (
                ("event_login_grants", event_grants), ("comeback_login_grants", comeback_grants),
                ("purchased_login_grants", purchased_grants), ("daily_gift_grants", daily_grants),
                ("vote_cooking_grants", vote_grants)) if value is not None}
            if ticket_observation is not _NO_VOTE_TICKETS:
                sources["vote_ticket_observation"] = deepcopy(ticket_observation)
            if self.letstd is not None:
                earlier_ids, earlier = list(ids), dict(sources)

                def validate_letstd(candidate, grants):
                    combined = [*earlier_ids, *validate_letstd_grants(grants)]
                    self._preflight(profile, candidate)
                    self._response(profile, candidate, combined, advance.beginner_ordinal,
                                   **_response_sources({**earlier, LETSTD_SOURCE: grants}))

                updated, letstd_grants = self.letstd.settle(updated, now, source=115, validate=validate_letstd)
                # Settle already checked every mail against its current source; a second full pass doubles the first claim.
                self.letstd.validate_receipt(updated, letstd_grants, verify_current=False)
                sources[LETSTD_SOURCE] = deepcopy(letstd_grants)
                ids = [*earlier_ids, *validate_letstd_grants(letstd_grants)]
            self._preflight(profile, updated)
            response = self._response(profile, updated, ids, advance.beginner_ordinal, **_response_sources(sources))
            encode_response(envelope.profile, response)
            updated[DELIVERY_KEY] = self._delivery(key, ids, advance.beginner_ordinal, base_ids, sources)
            self._validate_delivery(updated, key, ids, advance.beginner_ordinal, sources)
            self._ledger.record(updated, history, {"request": key, "content": self._receipt_content(sources),
                                                   "state": _state_digest(updated), "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8"), "mail_ids": ids,
                                                   "beginner_ordinal": advance.beginner_ordinal, "sources": sources})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while claiming login rewards", 409)

from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass
from functools import partial
import hashlib
from itertools import combinations, product
import json
import logging
import re
from types import MappingProxyType, SimpleNamespace

from .alxml import Column, Container, dumps, loads
from .bootstrap import STARTUP_COLLECTION_LIMITS
from .comeback_login_service import combine_event_login_notifications
from .comeback_login_state import DATE_FIELD, DAYS_FIELD, LEDGER_KEY as COMEBACK_LOGIN_KEY, validate_comeback_login_grants
from .daily_gift_state import LEDGER_KEY as DAILY_GIFT_KEY, validate_daily_gift_grants
from .energy_clock import instant
from .errors import ProtocolError
from .event_login_state import validate_event_login_grants
from .event_pass_state import STORED_ITEM_BYTES, delivered_mail_ids
from .letstd_state import validate_letstd_grants
from .native_numbers import int32
from .presents import GIFT_BOX_DEPENDENCIES, ITEM_CATALOG, MAX_PRESENTS, PRESENT_LIMITS, PendingAction
from .protocol import RESPONSE_TOO_LARGE, ProtocolRegistry, encode_response
from .purchased_login_service import combine_login_notifications
from .purchased_login_state import LEDGER_KEY as PURCHASED_LOGIN_KEY, validate_purchased_login_grants
from .rank_reward_state import (
    DELIVERED, LEDGER_KEY, PENDING, RESPONSE_CAPACITY, bonus_fingerprint, initialize_rank_rewards, mark_rank_reward,
    observe_rank_rewards, read_rank_rewards,
)
from .request_fields import invalid, registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .vote_cooking_service import validate_vote_cooking_grants, vote_cooking_response
from .vote_expiry import (ASSAULT_NOTIFICATION_KEY, NOTIFICATION_KEY, validate_vote_assault_notifications,
                          validate_vote_expiry_notifications)
from .vote_tickets import validate_vote_ticket_observation


RESPONSE_LIMIT = 128 * 1024
HISTORY_NAMESPACE = "rank_reward_history"
DELIVERY_KEY = "rank_reward_delivery"
DESCRIPTION = "ランクアップボーナス"
DEFINITION_CHANGED = "rank_reward_definition_changed"
RESOLVE_OPERATION = "rank_rewards.resolve_changed"
RESOLVE_UNNEEDED = "rank_reward_resolve_unneeded"
DEFINITION_MISSING = "rank_reward_definition_missing"
SECTIONS = ("event_pass_mail_ids", "event_login_grants", "comeback_login_grants", "purchased_login_grants",
            "daily_gift_grants", "vote_features")
LETSTD_SECTION = "letstd_grants"
EXPIRY_SECTION = "vote_expiry_notifications"
ASSAULT_SECTION = "vote_assault_notifications"
LISTED_SCHEMA = 8
EXPIRY_SCHEMA = 9
ASSAULT_SCHEMA = 10
VOTE_FIELDS = ("vote_ticket_observation", "vote_cooking_deliveries", "vote_cooking_notifications",
               "vote_cooking_notification_count")
EXPIRY_FIELDS = ("vote_expiry_notifications", "vote_expiry_notification_count")
ASSAULT_FIELDS = ("vote_assault_notifications", "vote_assault_notification_count")
SECTION_FIELDS = MappingProxyType({**{name: (name,) for name in (*SECTIONS[:-1], LETSTD_SECTION)},
                                   "vote_features": VOTE_FIELDS, EXPIRY_SECTION: EXPIRY_FIELDS,
                                   ASSAULT_SECTION: ASSAULT_FIELDS})
SECTION_SERVICES = MappingProxyType({
    "event_pass_mail_ids": ("event_passes",), "event_login_grants": ("event_logins",),
    "comeback_login_grants": ("comeback_logins",), "purchased_login_grants": ("purchased_logins",),
    "daily_gift_grants": ("daily_gifts",), "vote_features": ("vote_tickets", "vote_cooking_notifier"),
    LETSTD_SECTION: ("letstd",), EXPIRY_SECTION: ("vote_expiry_notifier",), ASSAULT_SECTION: ("vote_assault_notifier",)})
BASE_DEPENDENCIES = MappingProxyType({"items": ITEM_CATALOG, "tickets": "tickets", "item_tickets": "item_tickets",
                                      "selection_tickets": "selection_tickets"})
_HASH = re.compile(r"[a-f0-9]{64}\Z")
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _history_invalid():
    raise ProtocolError("rank_reward_history_invalid", "Stored rank reward receipts are invalid", 503)


def _with_ledger(state, ledger):
    updated = deepcopy(state)
    updated[LEDGER_KEY] = ledger
    read_rank_rewards(updated)
    return updated


def _mail_ids(value):
    return (isinstance(value, (list, tuple)) and len(value) <= MAX_PRESENTS
            and all(int32(identifier) and identifier > 0 for identifier in value)
            and len(set(value)) == len(value))


def _receipt_valid(entry):
    return isinstance(entry["mail_ids"], list) and _mail_ids(entry["mail_ids"])


@dataclass(frozen=True)
class DeliverySchema:
    version: int
    content: str
    sections: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()

    @property
    def listed(self):
        return bool(self.extensions)


# Fixed schemas append one section each; a listed schema follows any fixed schema with its extensions, ending in its own.
DELIVERY_SCHEMAS = MappingProxyType({schema.version: schema for schema in (
    DeliverySchema(1, "content_version"),
    DeliverySchema(2, "_pass_receive_content_version", SECTIONS[:1]),
    DeliverySchema(3, "_event_receive_content_version", SECTIONS[:2]),
    DeliverySchema(4, "_comeback_receive_content_version", SECTIONS[:3]),
    DeliverySchema(5, "_purchased_receive_content_version", SECTIONS[:4]),
    DeliverySchema(6, "_daily_receive_content_version", SECTIONS[:5]),
    DeliverySchema(7, "receive_content_version", SECTIONS),
    DeliverySchema(LISTED_SCHEMA, "_letstd_receive_content_version", extensions=(LETSTD_SECTION,)),
    DeliverySchema(EXPIRY_SCHEMA, "_expiry_receive_content_version", extensions=(LETSTD_SECTION, EXPIRY_SECTION)),
    DeliverySchema(ASSAULT_SCHEMA, "_assault_receive_content_version",
                   extensions=(LETSTD_SECTION, EXPIRY_SECTION, ASSAULT_SECTION)))})
_LISTED_OWNERS = MappingProxyType({schema.extensions[-1]: schema for schema in DELIVERY_SCHEMAS.values() if schema.listed})
_GRANT_OWNERS = MappingProxyType({"event_login_grants": ("event_logins", "_event_login_ready"),
                                  "comeback_login_grants": ("comeback_logins", "_comeback_login_ready"),
                                  "purchased_login_grants": ("purchased_logins", "_purchased_login_ready"),
                                  "daily_gift_grants": ("daily_gifts", "_daily_gift_ready"),
                                  LETSTD_SECTION: ("letstd", "_letstd_ready")})


def _last_fixed_section(sections):
    # Earlier fixed sections may be empty placeholders; the last one was recorded by a configured service.
    return next((name for name in reversed(SECTIONS) if name in sections), None)


def _gift_box_version(service, gift_box):
    return service.content_version_for(gift_box)


def _pass_version(passes, gift_box):
    return passes.automation_content_version_for(gift_box[ITEM_CATALOG])


def _legacy_window_pass_version(passes, gift_box):
    return passes.automation_content_version_for(gift_box[ITEM_CATALOG], passes.catalog.legacy_window_content_version)


def _assault_version(notifier, gift_box):
    # Conversion notices digest the vote rewards, whose digest names the item catalog.
    expiry = notifier.expiry
    return notifier.content_version_for(expiry.content_version_for(expiry.rewards.content_version_for(gift_box[ITEM_CATALOG])))


_GIFT_BOX_VERSIONS = MappingProxyType({
    "event_passes": _pass_version, "vote_assault_notifier": _assault_version,
    **dict.fromkeys(("event_logins", "comeback_logins", "purchased_logins", "daily_gifts", "letstd"), _gift_box_version)})


class RankRewardService:
    def __init__(self, bootstrap, players, presents, catalog, *, event_passes=None, event_logins=None,
                 comeback_logins=None, purchased_logins=None, daily_gifts=None, vote_tickets=None,
                 vote_cooking_notifier=None, letstd=None, vote_expiry_notifier=None, vote_assault_notifier=None):
        self.bootstrap, self.players, self.presents, self.catalog = bootstrap, players, presents, catalog
        self.event_passes = event_passes
        self.event_logins = event_logins
        self.comeback_logins = comeback_logins
        self.purchased_logins = purchased_logins
        self.daily_gifts = daily_gifts
        self.vote_tickets = vote_tickets
        self.vote_cooking_notifier = vote_cooking_notifier
        self.letstd = letstd
        self.vote_expiry_notifier = vote_expiry_notifier
        self.vote_assault_notifier = vote_assault_notifier
        self._pinned = MappingProxyType({})
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (33, 35, 42, 116, 129, 137)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "rank_reward_history_invalid", "Stored rank reward receipts are invalid",
                                     fields=("request", "content", "state", "revision", "response", "mail_ids"),
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid)
        self.content_version = self._content()
        self._record_receive_contents()
        self._ready()

    def _record_receive_contents(self):
        self._pass_receive_content_version = self._pass_receive_content()
        self._event_receive_content_version = self._event_receive_content()
        self._comeback_receive_content_version = self._comeback_receive_content()
        self._purchased_receive_content_version = self._purchased_receive_content()
        self._daily_receive_content_version = self._daily_receive_content()
        self.receive_content_version = self._receive_content()
        self._letstd_receive_content_version = self._letstd_receive_content()
        self._expiry_receive_content_version = self._expiry_receive_content()
        self._assault_receive_content_version = self._assault_receive_content()

    def _version(self, name, field="content_version"):
        # A configured copy reads the digests its gift box pinned; the live service reads each setup digest.
        if name in self._pinned:
            return self._pinned[name]
        owner = self.presents if name in GIFT_BOX_DEPENDENCIES else self
        return getattr(getattr(owner, name), field, None)

    def _content(self):
        try:
            versions = {"catalog": self.catalog.content_version, "presents": self.presents.asset_sha256,
                        **{key: self._version(name) for key, name in BASE_DEPENDENCIES.items()}}
            if any(type(value) is not str or not _HASH.fullmatch(value) for value in versions.values()):
                raise ValueError("Rank reward dependencies require stable fingerprints")
            return _digest({"policy": 1, "dependencies": versions, "templates": dict(self.presents.templates),
                "description": DESCRIPTION, "eligibility": "cfgRnkbns_join_condition10_crossing",
                "legacy": "baseline_current_rank", "missing_reward": "retain_pending",
                "delivery": "mail", "notification": "omit_RANKUP"})
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("rank_reward_dependencies_invalid", "Rank reward dependencies are unavailable", 503) from error

    def _ready(self):
        self.presents._ready()
        if self._content() != self.content_version:
            raise ProtocolError("rank_reward_dependencies_changed", "Rank reward definitions changed after setup", 503)

    def replayable_content_versions(self):
        # Digests that embed this one keep the base of the gift box they were committed under.
        return frozenset(self._pinned_to(gift_box).content_version for gift_box in self._gift_boxes())

    def _pass_receive_content(self):
        if self.event_passes is None:
            return self.content_version
        version = self._version("event_passes", "automation_content_version")
        if type(version) is not str or not _HASH.fullmatch(version):
            raise ProtocolError("rank_event_pass_unavailable", "Automatic event pass rewards require verified rules", 503)
        return _digest({"rank": self.content_version, "event_passes": version, "delivery_schema": 2,
                        "order": "rank_then_pass_id", "clock": "one_instant_per_new_request"})

    def _event_receive_content(self):
        previous = self._pass_receive_content()
        if self.event_logins is None:
            return previous
        version = self._version("event_logins")
        if type(version) is not str or not _HASH.fullmatch(version):
            raise ProtocolError("rank_event_login_unavailable", "Event login rewards require verified rules", 503)
        return _digest({"previous": previous, "event_logins": version, "delivery_schema": 3,
                        "order": "rank_then_pass_then_event_login", "clock": "one_instant_per_new_request"})

    def _comeback_receive_content(self):
        previous = self._event_receive_content()
        if self.comeback_logins is None:
            return previous
        version = self._version("comeback_logins")
        if type(version) is not str or not _HASH.fullmatch(version):
            raise ProtocolError("rank_comeback_login_unavailable", "Comeback rewards require verified rules", 503)
        return _digest({"previous": previous, "comeback_logins": version, "delivery_schema": 4,
                        "order": "observe_then_rank_pass_public_comeback", "clock": "one_instant_per_new_request"})

    def _purchased_receive_content(self):
        previous = self._comeback_receive_content()
        if self.purchased_logins is None:
            return previous
        version = self._version("purchased_logins")
        if type(version) is not str or not _HASH.fullmatch(version):
            raise ProtocolError("rank_purchased_login_unavailable", "Purchased login rewards require verified rules", 503)
        return _digest({"previous": previous, "purchased_logins": version, "delivery_schema": 5,
                        "order": "rank_pass_public_comeback_purchased", "clock": "one_instant_per_new_request"})

    def _daily_receive_content(self):
        previous = self._purchased_receive_content()
        if self.daily_gifts is None:
            return previous
        version = self._version("daily_gifts")
        if type(version) is not str or not _HASH.fullmatch(version):
            raise ProtocolError("rank_daily_gift_unavailable", "Daily gifts require verified rules", 503)
        return _digest({"previous": previous, "daily_gifts": version, "delivery_schema": 6,
                        "order": "rank_pass_public_comeback_purchased_daily", "clock": "one_instant_per_new_request"})

    def _receive_content(self):
        previous = self._daily_receive_content()
        if self.vote_tickets is None and self.vote_cooking_notifier is None:
            return previous
        versions = {}
        for name in ("vote_tickets", "vote_cooking_notifier"):
            service = getattr(self, name)
            version = self._version(name)
            if service is not None and (type(version) is not str or not _HASH.fullmatch(version)):
                raise ProtocolError("rank_vote_dependencies_invalid", "Voting rewards require verified dependencies", 503)
            versions[name] = version
        return _digest({"previous": previous, **versions, "delivery_schema": 7,
                        "order": "rank_pass_public_comeback_purchased_daily_tickets_cooking",
                        "clock": "one_instant_per_new_request", "inventory": "full_authoritative_snapshot"})

    def _letstd_receive_content(self):
        previous = self._receive_content()
        if self.letstd is None:
            return previous
        version = self._version("letstd")
        if type(version) is not str or not _HASH.fullmatch(version):
            raise ProtocolError("rank_letstd_unavailable", "LetsTD rewards require verified rules", 503)
        return _digest({"previous": previous, "letstd": version, "delivery_schema": LISTED_SCHEMA,
                        "order": "rank_pass_public_comeback_purchased_daily_tickets_cooking_letstd",
                        "clock": "one_instant_per_new_request", "sections": "listed"})

    def _expiry_receive_content(self):
        # Chained after LetsTD so every earlier schema keeps its digest once notices are configured.
        previous = self._letstd_receive_content()
        if self.vote_expiry_notifier is None:
            return previous
        version = self._version("vote_expiry_notifier")
        if type(version) is not str or not _HASH.fullmatch(version):
            raise ProtocolError("rank_vote_expiry_unavailable", "Vote expiry notices require verified rules", 503)
        return _digest({"previous": previous, "vote_expiry_notifications": version, "delivery_schema": EXPIRY_SCHEMA,
                        "order": "rank_pass_public_comeback_purchased_daily_tickets_cooking_expiry_letstd",
                        "clock": "one_instant_per_new_request", "sections": "listed"})

    def _assault_receive_content(self):
        # Chained after expiry notices so every earlier schema keeps its digest once RET notices are configured.
        previous = self._expiry_receive_content()
        if self.vote_assault_notifier is None:
            return previous
        version = self._version("vote_assault_notifier")
        if type(version) is not str or not _HASH.fullmatch(version):
            raise ProtocolError("rank_vote_assault_unavailable", "Assault conversion notices require verified rules", 503)
        return _digest({"previous": previous, "vote_assault_notifications": version, "delivery_schema": ASSAULT_SCHEMA,
                        "order": "rank_pass_public_comeback_purchased_daily_tickets_cooking_expiry_assault_letstd",
                        "clock": "one_instant_per_new_request", "sections": "listed"})

    def _vote_ready(self):
        if self.vote_tickets is None and self.vote_cooking_notifier is None:
            raise ProtocolError("rank_vote_unavailable", "Voting reward history requires its services", 503)
        if self.vote_tickets is not None:
            self.vote_tickets._ready()
            if self.vote_tickets.possessions is not self.presents.possessions:
                raise ProtocolError("rank_vote_dependencies_changed", "Daily tickets must share the inventory service", 503)
        if self.vote_cooking_notifier is not None:
            self.vote_cooking_notifier._ready()
            if self.vote_cooking_notifier.presents is not self.presents:
                raise ProtocolError("rank_vote_dependencies_changed", "Cooking notices must share the mail service", 503)
        if self._receive_content() != self.receive_content_version:
            raise ProtocolError("rank_vote_dependencies_changed", "Voting reward definitions changed after setup", 503)

    @staticmethod
    def _notification_count(state):
        domain = state.get("vote_cooking_notifications", {})
        if not isinstance(domain, dict):
            _history_invalid()
        records = domain.get("records", {})
        if not isinstance(records, dict) or len(records) > 4096:
            _history_invalid()
        return len(records)

    def _validate_vote_sources(self, state, observation, deliveries, notices, notification_count, *, exact=False):
        self._vote_ready()
        if type(notification_count) is not int or not 0 <= notification_count <= 4096:
            _history_invalid()
        validate_vote_ticket_observation(observation)
        validate_vote_cooking_grants(deliveries)
        validate_vote_cooking_grants(notices)
        if self.vote_tickets is not None:
            self.vote_tickets.validate_observation(state, observation, exact=exact)
        elif observation is not None:
            raise ProtocolError("rank_vote_unavailable", "Daily ticket receipt requires its service", 503)
        if self.vote_cooking_notifier is not None:
            self.vote_cooking_notifier.validate_receipt(state, deliveries, notices)
            current = self._notification_count(state)
            if current < notification_count or exact and current != notification_count:
                raise ProtocolError("rank_vote_notification_proof_lost", "Cooking notification history lost permanent proof", 503)
        elif deliveries or notices or notification_count:
            raise ProtocolError("rank_vote_unavailable", "Cooking notice receipt requires its service", 503)

    def _automatic_ready(self):
        if self.event_passes is None:
            raise ProtocolError("rank_event_pass_unavailable", "Automatic event pass rewards are unavailable", 503)
        self.event_passes._automatic_ready()
        if self._pass_receive_content() != self._pass_receive_content_version:
            raise ProtocolError("rank_event_pass_changed", "Automatic event pass rules changed after setup", 503)

    def _event_login_ready(self):
        if self.event_logins is None:
            raise ProtocolError("rank_event_login_unavailable", "Event login rewards are unavailable", 503)
        self.event_logins._ready()
        if (self.event_logins.presents is not self.presents
                or self._event_receive_content() != self._event_receive_content_version):
            raise ProtocolError("rank_event_login_changed", "Event login rules changed after setup", 503)

    def _comeback_login_ready(self):
        if self.comeback_logins is None:
            raise ProtocolError("rank_comeback_login_unavailable", "Comeback login rewards are unavailable", 503)
        self.comeback_logins._ready()
        if (self.comeback_logins.presents is not self.presents
                or self._comeback_receive_content() != self._comeback_receive_content_version):
            raise ProtocolError("rank_comeback_login_changed", "Comeback login rules changed after setup", 503)

    def _purchased_login_ready(self):
        if self.purchased_logins is None:
            raise ProtocolError("rank_purchased_login_unavailable", "Purchased login rewards are unavailable", 503)
        self.purchased_logins._ready()
        if (self.purchased_logins.presents is not self.presents
                or self._purchased_receive_content() != self._purchased_receive_content_version):
            raise ProtocolError("rank_purchased_login_changed", "Purchased login rules changed after setup", 503)

    def _daily_gift_ready(self):
        if self.daily_gifts is None:
            raise ProtocolError("rank_daily_gift_unavailable", "Daily gift rewards are unavailable", 503)
        self.daily_gifts._ready()
        if self.daily_gifts.presents is not self.presents or self._daily_receive_content() != self._daily_receive_content_version:
            raise ProtocolError("rank_daily_gift_changed", "Daily gift rules changed after setup", 503)

    def _letstd_ready(self):
        if self.letstd is None:
            raise ProtocolError("rank_letstd_unavailable", "LetsTD rewards are unavailable", 503)
        self.letstd._ready()
        if self.letstd.presents is not self.presents or self._letstd_receive_content() != self._letstd_receive_content_version:
            raise ProtocolError("rank_letstd_changed", "LetsTD rules changed after setup", 503)

    def _vote_expiry_ready(self):
        if self.vote_expiry_notifier is None:
            raise ProtocolError("rank_vote_expiry_unavailable", "Vote expiry notices are unavailable", 503)
        self.vote_expiry_notifier._ready()
        if (self.vote_expiry_notifier.presents is not self.presents
                or self._expiry_receive_content() != self._expiry_receive_content_version):
            raise ProtocolError("rank_vote_expiry_changed", "Vote expiry notice rules changed after setup", 503)

    def _vote_assault_ready(self):
        if self.vote_assault_notifier is None:
            raise ProtocolError("rank_vote_assault_unavailable", "Assault conversion notices are unavailable", 503)
        self.vote_assault_notifier._ready()
        if (self.vote_assault_notifier.presents is not self.presents
                or self._assault_receive_content() != self._assault_receive_content_version):
            raise ProtocolError("rank_vote_assault_changed", "Assault conversion notice rules changed after setup", 503)

    def handlers(self):
        return {116: self.receive}

    def _validate_player(self, state):
        row = self.bootstrap.player_row(state)
        if LEDGER_KEY in state and read_rank_rewards(state)["observed_rank"] > row["Rank"]:
            raise ProtocolError("rank_reward_rank_backwards", "Player rank is below the saved reward observation", 409)

    def seed_new(self, state, now=None):
        self._ready()
        self._validate_player(state)
        return initialize_rank_rewards(state, new_account=True)

    def initialize(self, state):
        self._ready()
        self._validate_player(state)
        updated = initialize_rank_rewards(state)
        self._validate_delivery(updated)
        marker = updated.get(DELIVERY_KEY, {})
        sections = self._recorded_sections(marker)[1] if DELIVERY_KEY in updated else ()
        last, pass_mail = _last_fixed_section(sections), marker.get("event_pass_mail_ids")
        if last == "event_pass_mail_ids" and self.event_passes is None:
            raise ProtocolError("rank_event_pass_unavailable", "Mixed reward history requires automatic event pass rewards", 503)
        if last == "event_login_grants" or "event_login_grants" in sections and marker["event_login_grants"]:
            self._event_login_ready()
            self.event_logins.validate_receipt(updated, marker["event_login_grants"], verify_current=False)
            if pass_mail and self.event_passes is None:
                raise ProtocolError("rank_event_pass_unavailable", "Mixed reward history requires automatic event pass rewards", 503)
        if self.comeback_logins is not None:
            self._comeback_login_ready()
            updated = self.comeback_logins.initialize(updated)
        elif (COMEBACK_LOGIN_KEY in updated or last == "comeback_login_grants"
              or "comeback_login_grants" in sections and marker["comeback_login_grants"]):
            raise ProtocolError("rank_comeback_login_unavailable", "Comeback history requires its reward service", 503)
        if "comeback_login_grants" in sections and (self.comeback_logins is not None or marker["comeback_login_grants"]):
            self.comeback_logins.validate_receipt(updated, marker["comeback_login_grants"], verify_current=False)
            if pass_mail and self.event_passes is None:
                raise ProtocolError("rank_event_pass_unavailable", "Mixed reward history requires automatic event pass rewards", 503)
        if self.purchased_logins is not None:
            self._purchased_login_ready()
            updated = self.purchased_logins.initialize(updated)
        elif (PURCHASED_LOGIN_KEY in updated or last == "purchased_login_grants"
              or "purchased_login_grants" in sections and marker["purchased_login_grants"]):
            raise ProtocolError("rank_purchased_login_unavailable", "Purchased login history requires its service", 503)
        if "purchased_login_grants" in sections and (self.purchased_logins is not None or marker["purchased_login_grants"]):
            self.purchased_logins.validate_receipt(updated, marker["purchased_login_grants"], verify_current=False)
            if pass_mail and self.event_passes is None:
                raise ProtocolError("rank_event_pass_unavailable", "Mixed reward history requires automatic event pass rewards", 503)
        if self.daily_gifts is not None:
            self._daily_gift_ready()
            updated = self.daily_gifts.initialize(updated)
        elif DAILY_GIFT_KEY in updated or "daily_gift_grants" in sections and marker["daily_gift_grants"]:
            raise ProtocolError("rank_daily_gift_unavailable", "Daily gift history requires its reward service", 503)
        if "daily_gift_grants" in sections:
            if marker["daily_gift_grants"]:
                self.daily_gifts.validate_receipt(updated, marker["daily_gift_grants"], verify_current=False)
            if pass_mail and self.event_passes is None:
                raise ProtocolError("rank_event_pass_unavailable", "Mixed reward history requires automatic event pass rewards", 503)
        if self.vote_tickets is not None:
            self._vote_ready()
            updated = self.vote_tickets.initialize(updated)
        if self.vote_cooking_notifier is not None:
            self._vote_ready()
            updated = self.vote_cooking_notifier.initialize(updated)
        elif "vote_cooking_notifications" in updated:
            raise ProtocolError("rank_vote_unavailable", "Cooking notice history requires its service", 503)
        if "vote_features" in sections:
            self._validate_vote_sources(updated, marker["vote_ticket_observation"],
                marker["vote_cooking_deliveries"], marker["vote_cooking_notifications"],
                marker["vote_cooking_notification_count"])
        if self.vote_expiry_notifier is not None:
            self._vote_expiry_ready()
            updated = self.vote_expiry_notifier.initialize(updated)
        elif NOTIFICATION_KEY in updated:
            raise ProtocolError("rank_vote_expiry_unavailable", "Vote expiry notice history requires its service", 503)
        if EXPIRY_SECTION in sections:
            self._vote_expiry_ready()
            self.vote_expiry_notifier.validate_receipt(updated, marker["vote_expiry_notifications"],
                                                       marker["vote_expiry_notification_count"])
        if self.vote_assault_notifier is not None:
            self._vote_assault_ready()
            updated = self.vote_assault_notifier.initialize(updated)
        elif ASSAULT_NOTIFICATION_KEY in updated:
            raise ProtocolError("rank_vote_assault_unavailable", "Assault conversion notice history requires its service", 503)
        if ASSAULT_SECTION in sections:
            self._vote_assault_ready()
            self.vote_assault_notifier.validate_receipt(updated, marker["vote_assault_notifications"],
                                                        marker["vote_assault_notification_count"])
        return updated

    def status(self):
        try:
            self._ready()
            if self.event_passes is not None:
                self._automatic_ready()
            if self.event_logins is not None:
                self._event_login_ready()
            if self.comeback_logins is not None:
                self._comeback_login_ready()
            if self.purchased_logins is not None:
                self._purchased_login_ready()
            if self.daily_gifts is not None:
                self._daily_gift_ready()
            if self.vote_tickets is not None or self.vote_cooking_notifier is not None:
                self._vote_ready()
            if self.letstd is not None:
                self._letstd_ready()
            if self.vote_expiry_notifier is not None:
                self._vote_expiry_ready()
            if self.vote_assault_notifier is not None:
                self._vote_assault_ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "implemented_operations": [116],
                "content_version": self.content_version, "catalog": self.catalog.status(),
                "receive_content_version": self.receive_content_version,
                "automatic_event_passes": self.event_passes is not None,
                "event_logins": self.event_logins is not None,
                "comeback_logins": self.comeback_logins is not None,
                "purchased_logins": self.purchased_logins is not None,
                "daily_gifts": self.daily_gifts is not None,
                "vote_tickets": self.vote_tickets is not None,
                "vote_cooking_notifications": self.vote_cooking_notifier is not None,
                "letstd": self.letstd is not None,
                "vote_expiry_notifications": self.vote_expiry_notifier is not None,
                "vote_assault_notifications": self.vote_assault_notifier is not None,
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER,
                "eligibility": "Current display directory joined to rank-crossing rewards; local policy",
                "delivery": "Mail with persistent pending entries for unavailable rewards or capacity",
                "legacy_policy": "Current rank is the baseline; no inferred historical backfill",
                "unavailable": ["DMM membership rewards", "Non-rank RewardConfig triggers"],
                "native_tested": False}

    def _preflight(self, state, profile=None):
        candidate = SimpleNamespace(state=state, method_key=profile.method_key if profile is not None else "f" * 32)
        self._validate_player(state)
        encode_response(self._profiles[42], dumps(Container(children=(
            self.bootstrap._system(candidate), self.presents.response(state))), limits=PRESENT_LIMITS))

    def apply(self, before_state, candidate_state, profile=None):
        self._ready()
        initialized = self.initialize(before_state)
        if ((LEDGER_KEY in before_state) != (LEDGER_KEY in candidate_state)
                or before_state.get(LEDGER_KEY) != candidate_state.get(LEDGER_KEY)):
            raise ProtocolError("rank_reward_ledger_changed", "Parent operation changed the rank reward ledger", 409)
        updated = deepcopy(candidate_state)
        updated[LEDGER_KEY] = initialized[LEDGER_KEY]
        updated = observe_rank_rewards(updated, self.catalog)
        self._preflight(updated, profile)
        mail_ids = []
        for key, entry in sorted(read_rank_rewards(updated)["entries"].items(), key=self._delivery_order):
            if entry["status"] == DELIVERED:
                continue
            updated, ids = self._deliver(updated, key, entry, profile)
            mail_ids.extend(ids)
        self._ready()
        return updated, tuple(mail_ids)

    def _delivery_order(self, pair):
        bonus = self.catalog.by_id.get(int(pair[0]))
        return pair[1]["rank"], bonus.config_index if bonus is not None else int(pair[0])

    def _changed(self, key, entry):
        bonus = self.catalog.by_id.get(int(key))
        return bonus is None or bonus_fingerprint(bonus) != entry["definition"]

    def _deliver(self, state, key, entry, profile=None):
        if self._changed(key, entry):
            return mark_rank_reward(state, int(key), reason=DEFINITION_CHANGED), ()
        try:
            reward = self.catalog.by_id[int(key)].to_present_reward(DESCRIPTION)
            if reward["kind"] == "unit" and reward["template_id"] not in self.presents.templates:
                raise ProtocolError("rank_reward_template_unavailable", "Rank reward unit template is unavailable", 501)
            delivered, additions = self.presents.enqueue(state, [reward])
            self._preflight(delivered, profile)
        except ProtocolError as error:
            if error.status not in (409, 501) and error.code != RESPONSE_TOO_LARGE:
                raise
            return mark_rank_reward(state, int(key), reason=error.code), ()
        ids = tuple(row["PresentID"] for row in additions)
        return mark_rank_reward(delivered, int(key), mail_ids=ids), ids

    def changed_rewards(self, state, rank):
        if not int32(rank) or rank < 1:
            raise ProtocolError("rank_reward_rank_invalid", "Rank rewards are chosen by a positive native rank")
        entries = read_rank_rewards(state)["entries"] if LEDGER_KEY in state else {}
        return tuple(int(key) for key, entry in sorted(entries.items(), key=self._delivery_order)
                     if entry["rank"] == rank and entry["status"] == PENDING and self._changed(key, entry))

    def resolve_changed(self, state, rank, action):
        if action not in PendingAction:
            raise ProtocolError("rank_reward_action_invalid", "Changed rank rewards are redelivered or voided")
        self._ready()
        updated = deepcopy(state)
        updated[LEDGER_KEY] = self.initialize(state)[LEDGER_KEY]
        changed = self.changed_rewards(updated, rank)
        if not changed:
            raise ProtocolError(RESOLVE_UNNEEDED, "No pending reward of this rank has a changed definition", 409)
        ledger = read_rank_rewards(updated)
        if action == PendingAction.VOID:
            # The rank stays observed, so a voided reward is never offered again.
            for identifier in changed:
                del ledger["entries"][str(identifier)]
            return _with_ledger(updated, ledger), ()
        current = {identifier: bonus for identifier in changed if (bonus := self.catalog.by_id.get(identifier)) is not None}
        if not current:
            raise ProtocolError(DEFINITION_MISSING, "The current catalog no longer defines these rewards; void them", 409)
        for identifier, bonus in current.items():
            ledger["entries"][str(identifier)].update(definition=bonus_fingerprint(bonus), reason=None)
        updated, mail_ids = _with_ledger(updated, ledger), []
        for identifier in current:
            key = str(identifier)
            updated, ids = self._deliver(updated, key, read_rank_rewards(updated)["entries"][key])
            mail_ids.extend(ids)
        self._ready()
        return updated, tuple(mail_ids)

    @staticmethod
    def _pass_mail_map(value):
        if (not isinstance(value, dict) or len(value) > MAX_PRESENTS
                or any(type(identifier) is not str for identifier in value)):
            _history_invalid()
        result = {}
        for identifier in sorted(value):
            try:
                if type(identifier) is not str or not identifier or len(identifier.encode("utf-8")) > STORED_ITEM_BYTES:
                    _history_invalid()
                dumps(Container(children=(Column("ID", "S", (identifier,)),)))
            except (ValueError, TypeError, UnicodeError):
                _history_invalid()
            if not isinstance(value[identifier], list) or not value[identifier] or not _mail_ids(value[identifier]):
                _history_invalid()
            result[identifier] = list(value[identifier])
        return result

    @staticmethod
    def _event_grants(value):
        try:
            mails = validate_event_login_grants(value)
        except ProtocolError as error:
            raise ProtocolError("rank_reward_history_invalid", "Stored event login grants are invalid", 503) from error
        if not _mail_ids(mails):
            _history_invalid()
        return deepcopy(value)

    @staticmethod
    def _comeback_grants(value):
        try:
            mails = validate_comeback_login_grants(value)
        except ProtocolError as error:
            raise ProtocolError("rank_reward_history_invalid", "Stored comeback login grants are invalid", 503) from error
        if not _mail_ids(mails):
            _history_invalid()
        return deepcopy(value)

    @staticmethod
    def _purchased_grants(value):
        try:
            mails = validate_purchased_login_grants(value)
        except ProtocolError as error:
            raise ProtocolError("rank_reward_history_invalid", "Stored purchased login grants are invalid", 503) from error
        if not _mail_ids(mails):
            _history_invalid()
        return deepcopy(value)

    @staticmethod
    def _daily_grants(value):
        try:
            mails = validate_daily_gift_grants(value)
        except ProtocolError as error:
            raise ProtocolError("rank_reward_history_invalid", "Stored daily gift grants are invalid", 503) from error
        if not _mail_ids(mails):
            _history_invalid()
        return deepcopy(value)

    @staticmethod
    def _letstd_grants(value):
        try:
            mails = validate_letstd_grants(value)
        except ProtocolError as error:
            raise ProtocolError("rank_reward_history_invalid", "Stored LetsTD grants are invalid", 503) from error
        if not _mail_ids(mails):
            _history_invalid()
        return deepcopy(value)

    @staticmethod
    def _expiry_notices(value, count):
        try:
            validate_vote_expiry_notifications(value, count)
        except ProtocolError as error:
            raise ProtocolError("rank_reward_history_invalid", "Stored vote expiry notices are invalid", 503) from error
        return deepcopy(value)

    @staticmethod
    def _assault_notices(value, count):
        try:
            validate_vote_assault_notifications(value, count)
        except ProtocolError as error:
            raise ProtocolError("rank_reward_history_invalid", "Stored assault conversion notices are invalid", 503) from error
        return deepcopy(value)

    @staticmethod
    def _combined_mail_ids(rank_ids, pass_mail, event_grants=None, comeback_grants=None, purchased_grants=None,
                           daily_grants=None, vote_deliveries=None, letstd_grants=None):
        if not _mail_ids(rank_ids):
            _history_invalid()
        merged = [*rank_ids, *(identifier for values in pass_mail.values() for identifier in values),
                  *(identifier for grant in (event_grants or ()) for identifier in grant["mail_ids"]),
                  *(identifier for grant in (comeback_grants or ()) for identifier in grant["mail_ids"]),
                  *(identifier for grant in (purchased_grants or ()) for identifier in grant["mail_ids"]),
                  *(identifier for grant in (daily_grants or ()) for identifier in grant["mail_ids"]),
                  *(identifier for grant in (vote_deliveries or ()) for identifier in grant["mail_ids"]),
                  *(identifier for grant in (letstd_grants or ()) for identifier in grant["mail_ids"])]
        if not _mail_ids(merged):
            _history_invalid()
        return merged

    @staticmethod
    def _recorded_sections(delivery):
        version = delivery.get("schema_version") if isinstance(delivery, dict) else None
        schema = DELIVERY_SCHEMAS.get(version) if type(version) is int else None
        if schema is None:
            _history_invalid()
        if not schema.listed:
            return schema, schema.sections
        listed = delivery.get("sections")
        if not isinstance(listed, list):
            _history_invalid()
        fixed = [name for name in listed if name in SECTIONS]
        appended = listed[len(fixed):]
        if (listed[:len(fixed)] != fixed or appended[-1:] != list(schema.extensions[-1:])
                or appended != [name for name in schema.extensions if name in appended]
                or not any(not other.listed and list(other.sections) == fixed for other in DELIVERY_SCHEMAS.values())):
            _history_invalid()
        return schema, tuple(listed)

    @staticmethod
    def _delivery(request, mail_ids, rank_ids, level, values, appended=()):
        fixed = DELIVERY_SCHEMAS[level]
        schema = _LISTED_OWNERS[appended[-1]] if appended else fixed
        sections = (*fixed.sections, *appended)
        delivery = {"schema_version": schema.version, "request": request, "mail_ids": mail_ids}
        if sections:
            delivery["rank_mail_ids"] = list(rank_ids)
        if schema.listed:
            delivery["sections"] = list(sections)
        delivery.update({field: values[field] for name in sections for field in SECTION_FIELDS[name]})
        return delivery

    @staticmethod
    def _validate_delivery(state, request=None, mail_ids=None):
        delivery = state.get(DELIVERY_KEY)
        if delivery is None and DELIVERY_KEY not in state and request is None:
            return
        schema, sections = RankRewardService._recorded_sections(delivery)
        expected = {"schema_version", "request", "mail_ids", *(field for name in sections for field in SECTION_FIELDS[name])}
        if sections:
            expected.add("rank_mail_ids")
        if schema.listed:
            expected.add("sections")
        if (set(delivery) != expected
                or type(delivery["request"]) is not str or not _HASH.fullmatch(delivery["request"])
                or not isinstance(delivery["mail_ids"], list) or not _mail_ids(delivery["mail_ids"])
                or request is not None and (delivery["request"] != request or delivery["mail_ids"] != list(mail_ids))):
            _history_invalid()
        delivered = {identifier for entry in read_rank_rewards(state)["entries"].values()
                     if entry["status"] == DELIVERED for identifier in entry["mail_ids"]}
        if not sections:
            if not set(delivery["mail_ids"]) <= delivered:
                _history_invalid()
            return
        rank_ids = delivery["rank_mail_ids"]
        if not isinstance(rank_ids, list) or not _mail_ids(rank_ids) or not set(rank_ids) <= delivered:
            _history_invalid()
        pass_mail = (RankRewardService._pass_mail_map(delivery["event_pass_mail_ids"])
                     if "event_pass_mail_ids" in sections else {})
        grants = (RankRewardService._event_grants(delivery["event_login_grants"])
                  if "event_login_grants" in sections else [])
        comeback = (RankRewardService._comeback_grants(delivery["comeback_login_grants"])
                    if "comeback_login_grants" in sections else [])
        purchased = (RankRewardService._purchased_grants(delivery["purchased_login_grants"])
                     if "purchased_login_grants" in sections else [])
        daily = (RankRewardService._daily_grants(delivery["daily_gift_grants"])
                 if "daily_gift_grants" in sections else [])
        vote_deliveries = []
        if "vote_features" in sections:
            count = delivery["vote_cooking_notification_count"]
            if type(count) is not int or not 0 <= count <= 4096:
                _history_invalid()
            validate_vote_ticket_observation(delivery["vote_ticket_observation"])
            vote_deliveries = delivery["vote_cooking_deliveries"]
            validate_vote_cooking_grants(vote_deliveries)
            validate_vote_cooking_grants(delivery["vote_cooking_notifications"])
        letstd = RankRewardService._letstd_grants(delivery[LETSTD_SECTION]) if LETSTD_SECTION in sections else []
        if EXPIRY_SECTION in sections:
            RankRewardService._expiry_notices(delivery["vote_expiry_notifications"], delivery["vote_expiry_notification_count"])
        if ASSAULT_SECTION in sections:
            RankRewardService._assault_notices(delivery["vote_assault_notifications"],
                                               delivery["vote_assault_notification_count"])
        if RankRewardService._combined_mail_ids(rank_ids, pass_mail, grants, comeback, purchased, daily,
                                                vote_deliveries, letstd) != delivery["mail_ids"]:
            _history_invalid()
        if pass_mail:
            known = delivered_mail_ids(state)
            if any(not set(values) <= known.get(pass_id, frozenset()) for pass_id, values in pass_mail.items()):
                _history_invalid()

    def _receipts(self, state, revision):
        history = self._ledger.read(state, revision)
        if history["receipts"]:
            newest = history["receipts"][-1]
            self._validate_delivery(state, newest["request"], newest["mail_ids"])
        elif DELIVERY_KEY in state:
            _history_invalid()
        return history

    def _letstd_response(self, grants):
        grants = self._letstd_grants(grants)
        if self.letstd is None and not grants:
            return None
        self._letstd_ready()
        return self.letstd.response(grants)

    def response(self, profile, state, mail_ids, *, event_pass_mail_ids=None, event_login_grants=None,
                 comeback_login_grants=None, purchased_login_grants=None, vote_features=False,
                 vote_cooking_notifications=None, letstd_grants=None, vote_expiry_notifications=None,
                 vote_assault_notifications=None, legacy_possessions=False):
        if not _mail_ids(mail_ids):
            _history_invalid()
        children = [self.bootstrap._system(profile)]
        if mail_ids:
            children.append(self.presents.response(state, mail_ids))
        if event_pass_mail_ids:
            children.append(self.event_passes.response(state, sorted(event_pass_mail_ids)))
        if comeback_login_grants is not None:
            pairs = self._comeback_pairs(state)
            children.append(Container("PGP2", (Column("A1", "S", tuple(key for key, _ in pairs)),
                Column("A2", "S", tuple(value for _, value in pairs)))))
        if purchased_login_grants is not None:
            notification = combine_login_notifications(event_login_grants or [], comeback_login_grants or [],
                                                       self._purchased_grants(purchased_login_grants))
            if notification.children[0].values:
                children.append(notification)
        elif comeback_login_grants is not None:
            notification = combine_event_login_notifications(event_login_grants or [],
                self._comeback_grants(comeback_login_grants))
            if notification.children[0].values:
                children.append(notification)
        elif event_login_grants:
            children.append(self.event_logins.response(self._event_grants(event_login_grants)))
        if vote_features:
            self._vote_ready()
            if self.vote_tickets is not None:
                children.append(self.vote_tickets.possessions.response(state, legacy=legacy_possessions))
            if self.vote_cooking_notifier is not None:
                validate_vote_cooking_grants(vote_cooking_notifications)
                notice = self.vote_cooking_notifier.response(vote_cooking_notifications)
                cooking = getattr(self.vote_cooking_notifier, "cooking", None)
                recipes = getattr(cooking, "recipes", None)
                expected = vote_cooking_response(vote_cooking_notifications,
                    receive_text_id=getattr(recipes, "receive_text_id", None))
                if notice != expected:
                    raise ProtocolError("rank_vote_response_invalid", "Cooking notification differs from its proof", 503)
                children.append(notice)
        if letstd_grants is not None:
            notice = self._letstd_response(letstd_grants)
            if notice is not None:
                children.append(notice)
        if vote_expiry_notifications:
            self._vote_expiry_ready()
            children.append(self.vote_expiry_notifier.response(vote_expiry_notifications))
        if vote_assault_notifications:
            self._vote_assault_ready()
            children.append(self.vote_assault_notifier.response(vote_assault_notifications))
        payload = dumps(Container(children=tuple(children)), limits=PRESENT_LIMITS)
        if len(payload) > RESPONSE_LIMIT:
            raise ProtocolError(RESPONSE_CAPACITY, "Rank reward response exceeds receipt capacity", 503)
        encode_response(self._profiles[116], payload)
        return payload

    def _comeback_pairs(self, state):
        self._comeback_login_ready()
        pairs = self.comeback_logins.pgp2_pairs(state)
        candidate = SimpleNamespace(state=state, method_key="f" * 32)
        row = self.bootstrap._rows(candidate, "game_param2")[0]
        expected = ((DATE_FIELD, row[DATE_FIELD]), (DAYS_FIELD, str(row[DAYS_FIELD])))
        if pairs != expected:
            raise ProtocolError("rank_comeback_projection_invalid", "Comeback projection differs from saved parameters", 503)
        return pairs

    def _preflight_dispatch(self, profile, state, rank_ids, event_pass_mail_ids, event_login_grants=None,
                            comeback_login_grants=None, purchased_login_grants=None, daily_gift_grants=None,
                            *, vote_features=False, vote_ticket_observation=None, vote_cooking_deliveries=None,
                            vote_cooking_notifications=None, vote_cooking_notification_count=0, letstd_grants=None,
                            vote_expiry_notifications=None, vote_expiry_notification_count=0,
                            vote_assault_notifications=None, vote_assault_notification_count=0,
                            legacy_possessions=False):
        pass_mail = self._pass_mail_map(event_pass_mail_ids)
        grants = self._event_grants(event_login_grants) if event_login_grants is not None else []
        if event_login_grants is not None:
            self._event_login_ready()
            self.event_logins.validate_receipt(state, grants)
        comeback = self._comeback_grants(comeback_login_grants) if comeback_login_grants is not None else []
        if comeback_login_grants is not None:
            self._comeback_login_ready()
            self.comeback_logins.validate_receipt(state, comeback)
            self._comeback_pairs(state)
            candidate = SimpleNamespace(state=state, method_key=profile.method_key)
            rows = self.bootstrap._rows(candidate, "game_param2")
            encode_response(self._profiles[35], dumps(Container(children=self.bootstrap.columns("game_param2", rows))))
            encode_response(self._profiles[33], self.bootstrap.game_param2_fields_response(candidate))
        purchased = self._purchased_grants(purchased_login_grants) if purchased_login_grants is not None else []
        if purchased_login_grants is not None:
            self._purchased_login_ready()
            self.purchased_logins.validate_receipt(state, purchased)
        daily = self._daily_grants(daily_gift_grants) if daily_gift_grants is not None else []
        if daily or daily_gift_grants is not None and self.daily_gifts is not None:
            self._daily_gift_ready()
            self.daily_gifts.validate_receipt(state, daily)
        if vote_features:
            self._validate_vote_sources(state, vote_ticket_observation, vote_cooking_deliveries,
                                        vote_cooking_notifications, vote_cooking_notification_count, exact=True)
        letstd = self._letstd_grants(letstd_grants) if letstd_grants is not None else []
        if letstd or letstd_grants is not None and self.letstd is not None:
            self._letstd_ready()
            # Settle and replay compare each mail with its current source once; this ledger proof stays cheap per candidate.
            self.letstd.validate_receipt(state, letstd, verify_current=False)
        expiry = None
        if vote_expiry_notifications is not None:
            expiry = self._expiry_notices(vote_expiry_notifications, vote_expiry_notification_count)
            self._vote_expiry_ready()
            self.vote_expiry_notifier.validate_receipt(state, expiry, vote_expiry_notification_count, exact=True)
        assault = None
        if vote_assault_notifications is not None:
            assault = self._assault_notices(vote_assault_notifications, vote_assault_notification_count)
            self._vote_assault_ready()
            self.vote_assault_notifier.validate_receipt(state, assault, vote_assault_notification_count, exact=True)
        mail_ids = self._combined_mail_ids(rank_ids, pass_mail, grants, comeback, purchased, daily,
                                           vote_cooking_deliveries if vote_features else None, letstd)
        self._preflight(state, profile)
        if self.event_passes is not None:
            self.event_passes.initialize(state)
            candidate = SimpleNamespace(state=state, method_key=profile.method_key)
            payload = self.bootstrap._configuration_response(candidate)
            groups = [group for group in loads(payload, limits=STARTUP_COLLECTION_LIMITS).children
                      if group.name == "EventPassStatus"]
            if groups != [self.event_passes.response(state)]:
                raise ProtocolError("event_pass_provider_inconsistent", "Startup event pass state differs from the saved state", 503)
            encode_response(self._profiles[137], payload)
        elif pass_mail:
            raise ProtocolError("rank_event_pass_unavailable", "Automatic event pass rewards are unavailable", 503)
        if vote_features and self.vote_tickets is not None:
            candidate = SimpleNamespace(state=state, method_key=profile.method_key)
            inventory = self.vote_tickets.possessions.startup_response(state)
            encode_response(self._profiles[129], dumps(Container(children=(self.bootstrap._system(candidate), inventory))))
            payload = self.bootstrap._configuration_response(candidate)
            groups = [group for group in loads(payload, limits=STARTUP_COLLECTION_LIMITS).children
                      if group.name == "POSSESSION"]
            if groups != [inventory]:
                raise ProtocolError("vote_provider_inconsistent", "Startup inventory differs from saved vote tickets", 503)
            encode_response(self._profiles[137], payload)
        return self.response(profile, state, mail_ids, event_pass_mail_ids=pass_mail, event_login_grants=grants,
                             comeback_login_grants=comeback if comeback_login_grants is not None else None,
                             purchased_login_grants=purchased if purchased_login_grants is not None else None,
                             vote_features=vote_features, vote_cooking_notifications=vote_cooking_notifications,
                             letstd_grants=letstd if letstd_grants is not None else None,
                             vote_expiry_notifications=expiry, vote_assault_notifications=assault,
                             legacy_possessions=legacy_possessions)

    def _attached(self):
        return {name for names in SECTION_SERVICES.values() for name in names if getattr(self, name) is not None}

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

    def _configured(self, gift_box, *, legacy_window=False):
        # Each service digests the gift box once; every attached subset of this configuration reuses the result.
        versions = {name: version(service, gift_box)
                    for name, version in _GIFT_BOX_VERSIONS.items() if (service := getattr(self, name)) is not None}
        if legacy_window:
            versions["event_passes"] = _legacy_window_pass_version(self.event_passes, gift_box)
        configured = self._pinned_to({**gift_box, **versions})
        configured._record_receive_contents()
        return configured

    def _gift_boxes(self):
        # A gift box without a base dependency had no service, so it never committed a receipt.
        return [gift_box for gift_box in self.presents.gift_box_configurations()
                if all(gift_box[name] is not None for name in BASE_DEPENDENCIES.values())]

    def _configurations(self):
        # The first gift box is the current wiring, which the live service already answers for.
        yield self
        for gift_box in self._gift_boxes()[1:]:
            yield self._configured(gift_box)
        if self.event_passes is not None:
            # Receipts committed while pass windows compared JST instants digest the pass rules of that build.
            for gift_box in self._gift_boxes():
                yield self._configured(gift_box, legacy_window=True)

    def _present_choices(self, section, sections, last):
        if section not in sections:
            return ((),)
        owners = SECTION_SERVICES[section]
        attached = tuple(name for name in owners if getattr(self, name) is not None)
        # An extension is listed only while its service exists; the last fixed section needs at least one of its own.
        if section not in SECTIONS:
            return (owners,) if attached == owners else ()
        smallest = 1 if section == last else 0
        return tuple(subset for size in range(len(attached), smallest - 1, -1) for subset in combinations(attached, size))

    def _committed(self, schema, sections, content):
        # Earlier fixed sections keep placeholders for absent services, so only the digest names the committed subset.
        last, attached = _last_fixed_section(sections), self._attached()
        subsets = [attached.difference(*present)
                   for present in product(*(self._present_choices(name, sections, last) for name in SECTION_SERVICES))]
        if not subsets:
            return None
        for configured in self._configurations():
            for absent in subsets:
                candidate = configured._detached(absent) if absent else configured
                if getattr(candidate, schema.content) == content:
                    return candidate
        return None

    def _replay_sources(self, marker, sections):
        last = _last_fixed_section(sections)
        if "event_pass_mail_ids" in sections and (last == "event_pass_mail_ids" or self.event_passes is not None):
            self._automatic_ready()
        recorded = {}
        for name in sections:
            if name in _GRANT_OWNERS:
                owner, ready = _GRANT_OWNERS[name]
                service = getattr(self, owner)
                if name == last or marker[name] or service is not None:
                    getattr(self, ready)()
                recorded[name] = marker[name] if service is not None else None
        if "vote_features" in sections:
            recorded.update(vote_features=True, **{field: marker[field] for field in VOTE_FIELDS})
        if EXPIRY_SECTION in sections:
            self._vote_expiry_ready()
            recorded.update({field: marker[field] for field in EXPIRY_FIELDS})
        if ASSAULT_SECTION in sections:
            self._vote_assault_ready()
            recorded.update({field: marker[field] for field in ASSAULT_FIELDS})
        return recorded

    def receive(self, envelope, store):
        self._ready()
        business = {"SHOP", "NUM", "DmmPremiumReceiverId"}
        request = registered(envelope, {116}, business, business)
        if scalar(request["SHOP"], "I") != 0 or scalar(request["NUM"], "I") != 0:
            invalid("Rank rewards require the native zero SHOP and NUM values")
        if scalar(request["DmmPremiumReceiverId"], "I") != 0:
            raise ProtocolError("rank_reward_membership_unsupported", "DMM membership rewards are not implemented", 501)
        key = hashlib.sha256(f"116:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        now = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            history = self._receipts(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                marker = profile.state[DELIVERY_KEY]
                schema, sections = self._recorded_sections(marker)
                committed = self._committed(schema, sections, cached["content"]) if cached["revision"] == profile.revision else None
                if committed is None:
                    raise ProtocolError("rank_reward_retry_stale", "Rank rewards changed after this request", 409)
                if cached["state"] != _state_digest(profile.state):
                    _history_invalid()
                self._validate_delivery(profile.state, key, cached["mail_ids"])
                if sections:
                    replayed = committed._replay_sources(marker, sections)
                    if replayed.get(LETSTD_SECTION) is not None:
                        committed.letstd.validate_receipt(profile.state, replayed[LETSTD_SECTION])
                    rebuild = partial(committed._preflight_dispatch, profile, profile.state, marker["rank_mail_ids"],
                                      marker.get("event_pass_mail_ids", {}), **replayed)
                else:
                    self._preflight(profile.state, profile)
                    rebuild = partial(self.response, profile, profile.state, cached["mail_ids"])
                saved = cached["response"].encode("utf-8")
                # Receipts saved before every POSSESSION carried Enabled keep their two-column table.
                if rebuild() != saved and rebuild(legacy_possessions=True) != saved:
                    _history_invalid()
                encode_response(envelope.profile, saved)
                return saved
            if self._ledger.processed(history, key):
                log.warning("Refused request 116: it repeats a processed request whose receipt was evicted")
                raise ProtocolError("rank_reward_request_stale", "Rank reward request was already processed", 409)
            observed = profile.state
            if self.comeback_logins is not None:
                self._comeback_login_ready()
                if now is None:
                    now = instant(self.bootstrap.clock())
                observed = self.comeback_logins.observe(observed, now)
            updated, rank_ids = self.apply(observed, observed, profile)
            if not self._attached():
                ids = list(rank_ids)
                marker = self._delivery(key, ids, rank_ids, 1, {})
                response = self.response(profile, updated, ids)
            else:
                if now is None:
                    now = instant(self.bootstrap.clock())
                pass_mail = {}
                if self.event_passes is not None:
                    self._automatic_ready()
                    updated, pass_mail = self.event_passes.settle_expired(profile, updated, now,
                        validate=lambda candidate, groups: self._preflight_dispatch(profile, candidate, rank_ids, groups,
                            comeback_login_grants=[] if self.comeback_logins is not None else None,
                            purchased_login_grants=[] if self.purchased_logins is not None else None,
                            daily_gift_grants=[] if self.daily_gifts is not None else None))
                pass_mail = self._pass_mail_map(pass_mail)
                grants = None
                if self.event_logins is not None:
                    self._event_login_ready()
                    updated, grants = self.event_logins.settle(updated, now, validate=lambda candidate, events:
                        self._preflight_dispatch(profile, candidate, rank_ids, pass_mail, events,
                            [] if self.comeback_logins is not None else None,
                            [] if self.purchased_logins is not None else None,
                            [] if self.daily_gifts is not None else None))
                    grants = self._event_grants(grants)
                comeback = None
                if self.comeback_logins is not None:
                    updated, comeback = self.comeback_logins.settle(updated, now, validate=lambda candidate, rewards:
                        self._preflight_dispatch(profile, candidate, rank_ids, pass_mail, grants, rewards,
                            [] if self.purchased_logins is not None else None,
                            [] if self.daily_gifts is not None else None))
                    comeback = self._comeback_grants(comeback)
                purchased = None
                if self.purchased_logins is not None:
                    self._purchased_login_ready()
                    updated, purchased = self.purchased_logins.settle(updated, now, validate=lambda candidate, rewards:
                        self._preflight_dispatch(profile, candidate, rank_ids, pass_mail, grants, comeback, rewards,
                            [] if self.daily_gifts is not None else None))
                    purchased = self._purchased_grants(purchased)
                daily = None
                if self.daily_gifts is not None:
                    self._daily_gift_ready()
                    updated, daily = self.daily_gifts.settle(updated, now, validate=lambda candidate, rewards:
                        self._preflight_dispatch(profile, candidate, rank_ids, pass_mail, grants, comeback, purchased,
                            daily_gift_grants=rewards))
                    daily = self._daily_grants(daily)
                vote_features = self.vote_tickets is not None or self.vote_cooking_notifier is not None
                observation, deliveries, notices, notification_count = None, [], [], 0
                if vote_features:
                    self._vote_ready()
                    if self.vote_tickets is not None:
                        updated, observation = self.vote_tickets.observe(updated, now)
                    if self.vote_cooking_notifier is not None:
                        updated, deliveries, notices = self.vote_cooking_notifier.settle(updated, now)
                        notification_count = self._notification_count(updated)
                    self._validate_vote_sources(updated, observation, deliveries, notices, notification_count, exact=True)
                # Both notices settle before LetsTD so its capacity preflight already carries ItemExchange and RET.
                expiry, expiry_count = None, 0
                if self.vote_expiry_notifier is not None:
                    self._vote_expiry_ready()
                    updated, expiry = self.vote_expiry_notifier.settle(updated, now)
                    expiry_count = self.vote_expiry_notifier.count(updated)
                assault, assault_count = None, 0
                if self.vote_assault_notifier is not None:
                    self._vote_assault_ready()
                    updated, assault = self.vote_assault_notifier.settle(updated, now)
                    assault_count = self.vote_assault_notifier.count(updated)
                letstd = None
                if self.letstd is not None:
                    self._letstd_ready()
                    updated, letstd = self.letstd.settle(updated, now, source=116, validate=lambda candidate, rewards:
                        self._preflight_dispatch(profile, candidate, rank_ids, pass_mail, grants, comeback, purchased,
                            daily_gift_grants=daily, vote_features=vote_features, vote_ticket_observation=observation,
                            vote_cooking_deliveries=deliveries, vote_cooking_notifications=notices,
                            vote_cooking_notification_count=notification_count, letstd_grants=rewards,
                            vote_expiry_notifications=expiry, vote_expiry_notification_count=expiry_count,
                            vote_assault_notifications=assault, vote_assault_notification_count=assault_count))
                    letstd = self._letstd_grants(letstd)
                ids = self._combined_mail_ids(rank_ids, pass_mail, grants, comeback, purchased, daily, deliveries, letstd)
                level = (7 if vote_features else 6 if daily is not None else 5 if purchased is not None else 4 if comeback is not None
                         else 3 if grants is not None else 2 if self.event_passes is not None else 1)
                appended = tuple(name for name, value in ((LETSTD_SECTION, letstd), (EXPIRY_SECTION, expiry),
                                                          (ASSAULT_SECTION, assault)) if value is not None)
                marker = self._delivery(key, ids, rank_ids, level, {
                    "event_pass_mail_ids": pass_mail, "event_login_grants": grants or [],
                    "comeback_login_grants": comeback or [], "purchased_login_grants": purchased or [],
                    "daily_gift_grants": daily or [], "vote_ticket_observation": deepcopy(observation),
                    "vote_cooking_deliveries": deepcopy(deliveries), "vote_cooking_notifications": deepcopy(notices),
                    "vote_cooking_notification_count": notification_count, LETSTD_SECTION: deepcopy(letstd),
                    "vote_expiry_notifications": deepcopy(expiry), "vote_expiry_notification_count": expiry_count,
                    "vote_assault_notifications": deepcopy(assault), "vote_assault_notification_count": assault_count},
                    appended)
                response = self._preflight_dispatch(profile, updated, rank_ids, pass_mail, grants, comeback, purchased,
                    daily_gift_grants=daily, vote_features=vote_features, vote_ticket_observation=observation,
                    vote_cooking_deliveries=deliveries, vote_cooking_notifications=notices,
                    vote_cooking_notification_count=notification_count, letstd_grants=letstd,
                    vote_expiry_notifications=expiry, vote_expiry_notification_count=expiry_count,
                    vote_assault_notifications=assault, vote_assault_notification_count=assault_count)
            version = getattr(self, DELIVERY_SCHEMAS[marker["schema_version"]].content)
            encode_response(envelope.profile, response)
            updated[DELIVERY_KEY] = marker
            self._ledger.record(updated, history, {"request": key, "content": version, "state": _state_digest(updated),
                                                   "revision": profile.revision + 1, "response": response.decode("utf-8"),
                                                   "mail_ids": list(ids)})
            self._receipts(updated, profile.revision + 1)
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while receiving rank rewards", 409)

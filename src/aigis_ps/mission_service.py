from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
import hashlib
from itertools import chain, combinations, product
import json
import logging
import re
from types import MappingProxyType, SimpleNamespace
from typing import NamedTuple

from .alxml import Column, Container, Limits, dumps, loads
from .bootstrap import STARTUP_COLLECTION_LIMITS
from .business_calendar import business_date
from .energy_clock import instant
from .errors import ProtocolError
from .mission_catalog import PAGES
from .mission_state import MissionProgress, period_key
from .native_numbers import int32
from .possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from .present_items import LEGACY_ITEMS
from .presents import MAX_PRESENTS, PRESENT_LIMITS
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger


MAX_MISSIONS = 65535
WINDOW_DEPENDENCIES = frozenset(("ep_bindings", "pass_points"))
RESPONSE_LIMIT = 256 * 1024
MAX_PERIOD_CHARS = 32
HISTORY_NAMESPACE = "mission_history"
PASS_DELIVERY_KEY = "mission_pass_delivery"
HISTORY_INVALID = ("mission_history_invalid", "Stored mission receipts are invalid")
DEPENDENCIES_UNAVAILABLE = ("mission_dependencies_unavailable", "Mission dependencies are unavailable")
SELECTION_INVALID = ("mission_selection_invalid", "Mission selections name distinct missions of the catalog")
CLAIM_OPERATIONS = frozenset((58, 59))
WRITE_OPERATIONS = CLAIM_OPERATIONS | {60}
RECEIPT_FIELDS = ("request", "content", "state", "revision", "response", "operation", "period", "ids", "mail_ids",
                  "event_pass_points")
LEGACY_RECEIPT_DEFAULTS = {"event_pass_points": None}
PERIOD_TABS = {period: tuple(sorted(tab for page_period, tab in PAGES if page_period == period)) for period, _ in PAGES}
MISSION_LIMITS = Limits(max_values=MAX_MISSIONS * 4 + 16, max_elements=MAX_MISSIONS * 4 + 64)
REPLY_LIMITS = Limits(max_bytes=RESPONSE_LIMIT, max_values=MAX_PRESENTS * 9 + MAX_MISSIONS + 16,
                      max_elements=MAX_PRESENTS * 9 + MAX_MISSIONS + 64)
DESCRIPTION = "任務達成報酬"
GLORY_SUMMON_USED = 1
GLORY_SUMMON_NO_SNAPSHOT = 0
# The client has no glory summon request, so Consumed stays 1 and CanSummonOfGlory() stays false.
GLORY_SUMMON_STATUS = Container("SMN", (Column("A1", "S", ("Consumed", "Snapshot")),
                                        Column("A2", "I", (GLORY_SUMMON_USED, GLORY_SUMMON_NO_SNAPSHOT))))
# A receipt may predate any of these; the digest records each one as absent while it is missing.
OPTIONAL_DEPENDENCIES = ("skins", "fame", "item_tickets", "pass_points", "clear_specs")
PROGRESS_DEPENDENCIES = frozenset(("skins", "fame", "clear_specs"))
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_POINT_AMOUNTS = frozenset(("requested", "credited", "before", "after", "capped", "inactive"))
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _history_invalid():
    raise ProtocolError(*HISTORY_INVALID, 503)


def _point_audit(audit):
    if not isinstance(audit, list) or not 0 < len(audit) <= MAX_MISSIONS:
        _history_invalid()
    previous = ""
    for entry in audit:
        if (not isinstance(entry, dict) or set(entry) != {"pass_id", *_POINT_AMOUNTS}
                or not isinstance(entry["pass_id"], str) or not previous < entry["pass_id"]
                or any(not int32(entry[name]) or entry[name] < 0 for name in _POINT_AMOUNTS)):
            _history_invalid()
        try:
            if len(entry["pass_id"].encode("utf-8")) > STORED_ITEM_NAME_BYTES:
                _history_invalid()
        except UnicodeError:
            _history_invalid()
        if (entry["requested"] <= 0 or entry["inactive"] not in (0, entry["requested"])
                or entry["requested"] != entry["credited"] + entry["capped"] + entry["inactive"]
                or entry["after"] != entry["before"] + entry["credited"]):
            _history_invalid()
        previous = entry["pass_id"]


def _identifiers(values, limit):
    return (isinstance(values, list) and len(values) <= limit
            and all(int32(value) and value > 0 for value in values) and len(set(values)) == len(values))


class _Scope(NamedTuple):
    targets: tuple[int, ...]
    members: frozenset[int]


def _finished(row):
    return row["Status"] == 1


def _unread(row):
    # 6F8740 marks the whole page read locally, so claimed missions are read along with claimable ones.
    return row["Status"] != 0 and row["Read"] == 0


def _applies(operation, row):
    return _unread(row) if operation == 60 else _finished(row)


@dataclass(frozen=True)
class MissionSelection:
    state: dict
    requested: tuple[int, ...]
    rows: Mapping[int, Mapping[str, int]]

    @property
    def shown(self) -> tuple[int, ...]:
        return tuple(self.rows)

    @property
    def candidates(self) -> tuple[int, ...]:
        return tuple(identifier for identifier, row in self.rows.items() if _finished(row))

    @property
    def skipped(self) -> tuple[int, ...]:
        chosen = set(self.candidates)
        return tuple(identifier for identifier in self.requested if identifier not in chosen)


@dataclass(frozen=True)
class MissionClaim:
    state: dict
    claimed: tuple[int, ...]
    rewards: tuple[dict, ...]
    mail_ids: tuple[int, ...]
    point_audit: tuple[dict, ...]
    unsupported: tuple[int, ...]


def _receipt_valid(entry):
    if (type(entry["operation"]) is not int or entry["operation"] not in WRITE_OPERATIONS
            or not isinstance(entry["period"], str) or len(entry["period"]) > MAX_PERIOD_CHARS
            or not _identifiers(entry["ids"], MAX_MISSIONS) or not _identifiers(entry["mail_ids"], MAX_PRESENTS)):
        return False
    points = entry["event_pass_points"]
    if points is not None:
        if entry["operation"] not in CLAIM_OPERATIONS:
            return False
        _point_audit(points)
    return True


class MissionService:
    def __init__(self, bootstrap, players, presents, catalog, *, progress=None, skins=None, fame=None, fame_status=None,
                 ep_bindings=None, pass_points=None):
        if progress is None:
            progress = MissionProgress(catalog, skin_items=skins.counted_item_ids if skins is not None else (),
                fame_cards=fame.counted_card_ids if fame is not None else (),
                fame_support_cards=fame.support_card_ids if fame is not None else ())
        self.bootstrap, self.players, self.presents = bootstrap, players, presents
        self.catalog, self.progress = catalog, progress
        self.skins = skins
        self.fame, self.fame_status = fame, fame_status
        self.ep_bindings, self.pass_points = ep_bindings, pass_points
        self.item_tickets = getattr(presents, "item_tickets", None)
        self._verified_content = self._content_inputs()
        self.content_version = self._content(self._verified_content)
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (42, 54, 58, 59, 60, 129, 137)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, *HISTORY_INVALID, fields=RECEIPT_FIELDS,
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid,
                                     legacy_defaults=LEGACY_RECEIPT_DEFAULTS)
        self._load = self.bootstrap.startup_diagnostics.wrap(54, self.read)

    def _content_inputs(self):
        try:
            versions = {"catalog": self.catalog.content_version, "progress": self.progress.content_version,
                        "presents": self.presents.asset_sha256}
            if any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in versions.values()):
                raise ValueError("Mission dependencies need content fingerprints")
            items = self.presents.item_catalog
            skin_version = self.skins.content_version if self.skins is not None else None
            if self.skins is not None and (not isinstance(skin_version, str) or not _HASH.fullmatch(skin_version)
                    or self.progress.skin_items != self.skins.counted_item_ids):
                raise ValueError("Skin acquisition and mission definitions differ")
            fame_version = self.fame.content_version if self.fame is not None else None
            if self.fame is not None and (not isinstance(fame_version, str) or not _HASH.fullmatch(fame_version)
                    or self.fame_status is None or self.fame_status.snapshot != self.fame.snapshot
                    or self.progress.fame_cards != self.fame.counted_card_ids
                    or self.progress.fame_support_cards != self.fame.support_card_ids):
                raise ValueError("Fame registration and mission definitions differ")
            content = {"version": 1, "dependencies": versions,
                # A copy, so templates edited in place still differ from the verified inputs.
                "templates": dict(self.presents.templates),
                "items": items.content_version if items is not None else None,
                "skins": skin_version,
                "fame": fame_version,
                "rewards": "mail_only", "description": DESCRIPTION,
                "cycles": "jst_04_daily_monthly", "claim_group": "skip_unsupported",
                "legacy_items": dict(LEGACY_ITEMS)}
            if self.item_tickets is not None:
                if not isinstance(self.item_tickets.content_version, str) or not _HASH.fullmatch(self.item_tickets.content_version):
                    raise ValueError("Mission item tickets require a content fingerprint")
                content["item_tickets"] = self.item_tickets.content_version
            if self.ep_bindings is not None and self.ep_bindings.changed_ids:
                content["ep_bindings"] = self.ep_bindings.content_version
            if any(row.event_pass_item_id or row.event_pass_point for row in self.catalog.by_id.values()):
                if self.pass_points is not None:
                    content["pass_points"] = self.pass_points.content_version
            return content
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError(*DEPENDENCIES_UNAVAILABLE, 503) from error

    @staticmethod
    def _content(inputs):
        try:
            return _digest(inputs)
        except (TypeError, ValueError) as error:
            raise ProtocolError(*DEPENDENCIES_UNAVAILABLE, 503) from error

    def _content_changed(self):
        # Comparing with the inputs verified at setup is far cheaper than serializing and hashing them again.
        inputs = self._content_inputs()
        return inputs != self._verified_content and self._content(inputs) != self.content_version

    def _attached(self):
        # Pass points reach the digest only while the catalog awards EP.
        present = {"skins": self.skins, "fame": self.fame, "item_tickets": self.item_tickets,
                   "pass_points": self.pass_points if "pass_points" in self._verified_content else None,
                   "clear_specs": getattr(self.progress, "clear_specs", None)}
        return tuple(name for name in OPTIONAL_DEPENDENCIES if present[name] is not None)

    def _detached(self, absent):
        detached = copy(self)
        if "skins" in absent:
            detached.skins = None
        if "fame" in absent:
            detached.fame = detached.fame_status = None
        if "item_tickets" in absent:
            detached.item_tickets = None
        if "pass_points" in absent:
            detached.pass_points = None
        if absent & PROGRESS_DEPENDENCIES:
            progress = self.progress
            detached.progress = MissionProgress(progress.catalog, dgp_login=progress.dgp_login,
                skin_items=() if "skins" in absent else progress.skin_items,
                fame_cards=() if "fame" in absent else progress.fame_cards,
                fame_support_cards=() if "fame" in absent else progress.fame_support_cards,
                clear_specs=None if "clear_specs" in absent else progress.clear_specs)
        detached._verified_content = detached._content_inputs()
        detached.content_version = detached._content(detached._verified_content)
        return detached

    def _replacements(self, name, provider):
        if provider is None:
            return ()
        return tuple(sorted(provider.replayable_content_versions() - {self._verified_content[name]}))

    def _variants(self):
        # Optional inputs nested in fame or present items change only those versions, so variants replay alike.
        content = self._verified_content
        fames, items = self._replacements("fame", self.fame), self._replacements("items", self.presents.item_catalog)
        for fame, item in chain(((fame, content["items"]) for fame in fames),
                                ((content["fame"], item) for item in items), product(fames, items)):
            variant = copy(self)
            variant._verified_content = {**content, "fame": fame, "items": item}
            variant.content_version = variant._content(variant._verified_content)
            yield variant

    def _legacy_windows(self):
        # Receipts committed while mission and pass windows compared JST instants digest each window input that way.
        # A catalog that never compared windows by JST instants publishes no earlier digest and adds no configuration.
        catalog = getattr(self.catalog, "legacy_window_content_version", None)
        if catalog is None:
            return
        content = self._verified_content
        legacy = {**content, "dependencies": {**content["dependencies"], "catalog": catalog,
                                              "progress": self.progress.legacy_window_content_version}}
        for name in WINDOW_DEPENDENCIES & content.keys():
            legacy[name] = getattr(self, name).legacy_window_content_version
        variant = copy(self)
        variant._verified_content = legacy
        variant.content_version = variant._content(legacy)
        yield variant
        yield from variant._variants()

    def _configurations(self):
        # Fewest masked dependencies first, so a receipt of the current configuration matches without hashing.
        attached = self._attached()
        for size in range(len(attached) + 1):
            for absent in combinations(attached, size):
                configuration = self._detached(frozenset(absent)) if absent else self
                yield configuration
                yield from configuration._variants()
                yield from configuration._legacy_windows()

    def replayable_content_versions(self):
        return frozenset(configuration.content_version for configuration in self._configurations())

    def _committed(self, content):
        return next((configuration for configuration in self._configurations()
                     if configuration.content_version == content), None)

    def _ready(self):
        self.presents._ready()
        if getattr(self.presents, "item_tickets", None) is not self.item_tickets:
            raise ProtocolError("mission_dependencies_changed", "Mission item ticket service changed after setup", 503)
        if self.item_tickets is not None:
            self.item_tickets._ready()
        if not self.catalog.is_ready() or self._content_changed():
            raise ProtocolError("mission_dependencies_changed", "Mission dependencies changed after setup", 503)
        if self.fame is not None and not self.fame.is_ready():
            raise ProtocolError("mission_fame_unavailable", "Fame mission definitions are unavailable", 503)
        if self.ep_bindings is not None:
            self.ep_bindings._ready()
        if self.pass_points is not None:
            self.pass_points._ready()

    def handlers(self):
        return {54: self._load, 58: self.claim, 59: self.claim_group, 60: self.mark_read}

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "implemented_operations": [54, 58, 59, 60],
                "content_version": self.content_version, "history_limit": HISTORY_LIMIT,
                "processed_request_limit": PROCESSED_LIMIT, "request_order": REQUEST_ORDER,
                "rewards": "mail_and_optional_pass_points" if self.pass_points is not None else "mail_only",
                "group_claim_policy": ("Unsupported (501) rewards stay claimable while the rest are claimed; capacity failures "
                                       "preserve all missions; reward items and new EP inventory rows past the "
                                       f"{MAX_ITEM_NAME_BYTES}-byte native cell count as unsupported"),
                "unchanged_policy": ("58/59/60 with nothing claimable or unread on their scope, including a 58 on a mission of its "
                                     "page outside its validity dates, answer empty without a receipt and log a warning"),
                "read_policy": "60 marks every finished unread mission on all tabs of the page's PT read, claimed or not, as "
                               "the client does locally; unfinished missions keep no read flag",
                "load_policy": "54 serves stored progress while only reward dependencies are unready and records the issue",
                "retry_policy": ("Exact request, current revision, current period and the dependency fingerprints of its "
                                 "commit, also once optional skin, fame, item ticket, EP or quest clear definitions, or "
                                 "optional inputs of fame and present item definitions, attach later; missions whose "
                                 "validity dates closed after the receipt still replay"),
                "glory_summon": (f"Not offered: 54 sends SMN Consumed={GLORY_SUMMON_USED} and "
                                 f"Snapshot={GLORY_SUMMON_NO_SNAPSHOT}, so CanSummonOfGlory() is false"),
                "catalog": self.catalog.status(), "progress": self.progress.status(),
                "ep_binding_missions": list(self.ep_bindings.changed_ids) if self.ep_bindings is not None else [],
                "pass_points": self.pass_points.status() if self.pass_points is not None else None,
                "native_tested": False}

    @staticmethod
    def _validate_point_delivery(state, request=None, audit=None):
        marker = state.get(PASS_DELIVERY_KEY)
        if marker is None and PASS_DELIVERY_KEY not in state and request is None:
            return
        if (not isinstance(marker, dict) or set(marker) != {"schema_version", "request", "audit"}
                or type(marker["schema_version"]) is not int or marker["schema_version"] != 1
                or not isinstance(marker["request"], str) or not _HASH.fullmatch(marker["request"])):
            _history_invalid()
        _point_audit(marker["audit"])
        if request is not None and (marker["request"] != request or marker["audit"] != audit):
            _history_invalid()

    def _prepare_state(self, state):
        self._validate_point_delivery(state)
        if self.ep_bindings is not None:
            state = self.ep_bindings.prepare(state)
        return self.progress.initialize(state)

    def initialize(self, state):
        self._ready()
        return self._prepare_state(state)

    def record(self, state, kind, payload, now):
        self._ready()
        now = instant(now)
        updated = self.progress.record(self._prepare_state(state), kind, payload, now)
        self.preflight_snapshot(updated, now)
        return updated

    def _skins_unchanged(self, before, updated):
        definitions, possessions = self.presents.item_catalog, self.presents.possessions
        names = definitions.skin_item_ids if definitions is not None else frozenset()
        if not names:
            return True
        # Without the possession service no count can be validated, so a skin change cannot be ruled out.
        if possessions is None:
            return False
        previous, current = possessions.counts(before), possessions.counts(updated)
        return all(current.get(name, 0) == previous.get(name, 0) for name in names)

    def record_acquisitions(self, before, updated, now):
        self._ready()
        if self.skins is None:
            if not self._skins_unchanged(before, updated):
                raise ProtocolError("mission_skin_catalog_unavailable", "Skin acquisition requires verified package definitions", 503)
            return updated
        previous, current = self.skins.acquired_items(before), self.skins.acquired_items(updated)
        if previous == current:
            return updated
        return self.record(updated, "skin_acquired", {
            "previous_items": sorted(previous), "current_items": sorted(current)}, now)

    def record_fame(self, before, updated, now):
        self._ready()
        if self.fame is None or self.fame_status is None:
            raise ProtocolError("mission_fame_unavailable", "Fame registration requires verified history definitions", 503)

        def registered(state):
            return {(row["CardID"], stage): row[f"B{stage}"] for row in self.fame_status.read(state)
                    for stage in range(5) if row[f"B{stage}"] != 0}

        previous, current = registered(before), registered(updated)
        if any(current.get(key) != value for key, value in previous.items()):
            raise ProtocolError("mission_fame_history_changed", "Existing fame registration dates cannot be replaced", 503)
        added = sorted(key for key in current.keys() - previous.keys() if key[0] in self.fame.counted_card_ids)
        if not added:
            return updated
        return self.record(updated, "fame_registered", {"registrations": added}, now)

    def _snapshot(self, state, now):
        rows = self.progress.snapshot(self._prepare_state(state), now)
        if not isinstance(rows, (tuple, list)) or len(rows) > MAX_MISSIONS:
            raise ProtocolError("mission_snapshot_invalid", "Mission snapshot exceeds its native capacity", 503)
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"ID", "Status", "Read", "Progress"}
                    or any(not int32(value) for value in row.values())
                    or row["ID"] not in self.catalog.by_id or row["ID"] in seen
                    or row["Status"] not in (0, 1, 2) or row["Read"] not in (0, 1)
                    or row["Progress"] < 0):
                raise ProtocolError("mission_snapshot_invalid", "Mission snapshot fields are invalid", 503)
            seen.add(row["ID"])
        return rows

    def _gl(self, rows):
        return Container("GL", tuple(Column(name, "I", tuple(row[name] for row in rows))
                                     for name in ("ID", "Status", "Read", "Progress")))

    def preflight_snapshot(self, state, now, profile=None):
        rows = self._snapshot(state, now)
        identity = profile if profile is not None else SimpleNamespace(method_key="f" * 32)
        payload = dumps(Container(children=(self.bootstrap._system(identity), self._gl(rows), GLORY_SUMMON_STATUS)),
                        limits=MISSION_LIMITS)
        encode_response(self._profiles[54], payload)
        return payload

    def _preflight_mail(self, profile, state):
        payload = dumps(Container(children=(self.bootstrap._system(profile), self.presents.response(state))),
                        limits=PRESENT_LIMITS)
        encode_response(self._profiles[42], payload)

    def _preflight_points(self, profile, state):
        candidate = replace(profile, state=state)
        expected = self.pass_points.possessions.startup_response(state)
        actual = self.bootstrap._possessions(candidate)
        if actual != expected:
            raise ProtocolError("mission_pass_provider_inconsistent", "Mission EP inventory differs from its provider", 503)
        encode_response(self._profiles[129], dumps(Container(children=(self.bootstrap._system(candidate), actual))))
        startup = self.bootstrap._configuration_response(candidate)
        groups = [child for child in loads(startup, limits=STARTUP_COLLECTION_LIMITS).children
                  if child.name == "POSSESSION"]
        if groups != [expected]:
            raise ProtocolError("mission_pass_provider_inconsistent", "Startup inventory differs from mission EP", 503)
        encode_response(self._profiles[137], startup)

    def read(self, envelope, store):
        registered(envelope, {54}, ())
        profile = self.bootstrap._authenticate(envelope, store)
        self.players.validate_player(profile.state.get("player_state"), references=False)
        try:
            self._ready()
        except ProtocolError as error:
            # 54 reports stored progress only, so reward-side readiness must not stall the native reload loop.
            self.bootstrap.startup_diagnostics.degrade(error)
            if not self.catalog.is_ready():
                raise ProtocolError("mission_catalog_unavailable", "Mission definitions cannot report progress", 503) from error
        return self.preflight_snapshot(profile.state, instant(self.bootstrap.clock()), profile)

    def _scope(self, operation, requested, now):
        identifier, period, tab, group = (requested[name] for name in ("ID", "PT", "TabGroupId", "TaskGroupId"))
        if any(value < 0 for value in requested.values()):
            invalid("Mission scope fields cannot be negative")
        if operation == 58:
            if identifier <= 0 or group != 0 or identifier not in self.catalog.by_id:
                raise ProtocolError("mission_scope_invalid", "Single claims require one known mission and no group")
            if period == 100 and tab == 0:
                allowed = identifier in self.catalog.sequence
            elif period == 4 and tab == 0:
                allowed = any(identifier in members for members in self.catalog.groups.values())
            elif (period, tab) in PAGES:
                allowed = identifier in self.catalog.listed(period, tab)
            else:
                allowed = False
            if not allowed or self.catalog.by_id[identifier].period_type != period:
                raise ProtocolError("mission_scope_invalid", "Mission does not belong to the requested page")
            # A page's mission outside its validity dates is absent from the 54 snapshot, so it answers unchanged.
            return _Scope((identifier,), frozenset((identifier,)))
        if identifier != 0:
            raise ProtocolError("mission_scope_invalid", "Page operations require a zero mission ID")
        if operation == 60:
            if (period, tab) not in PAGES or group != 0:
                raise ProtocolError("mission_scope_invalid", "Read notifications require a supported ordinary page")
            # 6F8740 marks every listed mission of the page's PT read locally, whatever its tab.
            tabs = PERIOD_TABS[period]
            return _Scope(tuple(item.condition_id for sibling in tabs for item in self.catalog.page(period, sibling, 0, now)),
                          frozenset().union(*(self.catalog.listed(period, sibling) for sibling in tabs)))
        if not (((period, tab) in PAGES and group == 0) or (period == 4 and tab == 0 and group > 0)):
            raise ProtocolError("mission_scope_invalid", "Group claims require a supported page or attribute group")
        targets = tuple(item.condition_id for item in self.catalog.page(period, tab, group, now))
        return _Scope(targets, frozenset(self.catalog.groups[group]) if group else self.catalog.listed(period, tab))

    def _claimable(self, state, identifiers, now):
        # Native group claims (6FB100/5EC170) submit every Status1 mission, so a 501 reward stays claimable instead of failing the batch.
        claimable, rewards, refusal = [], [], None
        queued = len(self.presents.rows(state))
        for identifier in identifiers:
            try:
                group = self._rewards((identifier,))
                self._require_replayable_rows(state, identifier, group, now)
                queued += len(self.presents.preview(group, queued))
            except ProtocolError as error:
                if error.status != 501:
                    raise
                refusal = refusal or error
                continue
            claimable.append(identifier)
            rewards.extend(group)
        if not claimable and refusal is not None:
            raise refusal
        return tuple(claimable), rewards

    def _require_replayable_rows(self, state, identifier, rewards, now):
        # The mail and EP writers refuse such new rows with 503, which would fail the whole group claim.
        if any(len(reward["item_id"].encode("utf-8")) > MAX_ITEM_NAME_BYTES for reward in rewards if "item_id" in reward):
            raise ProtocolError("mission_reward_unsupported", "Mission reward item does not fit the native cell", 501)
        pass_id = self.catalog.by_id[identifier].event_pass_item_id
        if not pass_id or len(pass_id.encode("utf-8")) <= MAX_ITEM_NAME_BYTES:
            return
        passes = self.pass_points.catalog
        existing = self.pass_points.possessions.counts(state)
        if pass_id not in existing and passes.is_eligible(passes.get(pass_id), now):
            raise ProtocolError("mission_reward_unsupported", "Mission EP would open an inventory row past the native cell", 501)

    def _rewards(self, identifiers):
        rewards = []
        for identifier in identifiers:
            definition = self.catalog.by_id[identifier]
            if definition.reward_point or ((definition.event_pass_item_id or definition.event_pass_point)
                                            and self.pass_points is None):
                raise ProtocolError("mission_reward_unsupported", "Mission reward has unsupported point effects", 501)
            kind, amount, item = definition.reward_item_type, definition.reward_item_param, definition.reward_item_id
            if not int32(amount) or amount <= 0 or (kind not in (1, 22) and item):
                raise ProtocolError("mission_reward_unsupported", "Mission reward fields have an unsupported shape", 501)
            if kind == 0:
                reward = {"kind": "free_crystal", "amount": amount}
            elif kind == 1:
                reward = {"kind": "unit", "template_id": amount, "count": 1}
            elif kind == 2:
                reward = {"kind": "gold", "amount": amount}
            elif kind in LEGACY_ITEMS:
                reward = {"kind": "legacy_item", "present_type": kind, "amount": amount}
            elif kind == 22 and item:
                reward = {"kind": "item", "item_id": item, "amount": amount}
            else:
                raise ProtocolError("mission_reward_unsupported", "Mission reward type is not implemented", 501)
            rewards.append({**reward, "description": DESCRIPTION})
        return rewards

    def _select(self, state, requested, now):
        prepared = self._prepare_state(state)
        rows = {row["ID"]: row for row in self._snapshot(prepared, now)}
        return MissionSelection(prepared, requested, MappingProxyType(
            {identifier: MappingProxyType(rows[identifier]) for identifier in requested if identifier in rows}))

    def select_missions(self, state: dict, identifiers: Sequence[int], now: datetime) -> MissionSelection:
        self._ready()
        requested = tuple(identifiers)
        if (not _identifiers(list(requested), MAX_MISSIONS)
                or any(identifier not in self.catalog.by_id for identifier in requested)):
            raise ProtocolError(*SELECTION_INVALID)
        return self._select(state, requested, now)

    def grant_missions(self, selection: MissionSelection, now: datetime) -> MissionClaim:
        claimed, rewards = self._claimable(selection.state, selection.candidates, now)
        updated = self.progress.claim(selection.state, claimed, now)
        updated, mail = self.presents.enqueue(updated, rewards)
        definitions = [self.catalog.by_id[identifier] for identifier in claimed]
        point_audit = []
        if any(row.event_pass_item_id or row.event_pass_point for row in definitions):
            updated, point_audit = self.pass_points.award(updated, definitions, now)
            _point_audit(point_audit)
        chosen = set(claimed)
        unsupported = tuple(identifier for identifier in selection.candidates if identifier not in chosen)
        return MissionClaim(updated, claimed, tuple(rewards), tuple(row["PresentID"] for row in mail),
                            tuple(point_audit), unsupported)

    def _response(self, operation, profile, state, identifiers, mail_ids, point_audit=None, *, legacy_possessions=False):
        result_ids = identifiers if operation in CLAIM_OPERATIONS else ()
        children = [self.bootstrap._system(profile), Container("GLRW", (Column("Value", "I", tuple(result_ids)),))]
        if mail_ids:
            children.append(self.presents.response(state, mail_ids))
        point_ids = [entry["pass_id"] for entry in point_audit or () if entry["credited"] > 0]
        if point_ids:
            children.append(self.pass_points.possessions.response(state, point_ids, legacy=legacy_possessions))
        payload = dumps(Container(children=tuple(children)), limits=REPLY_LIMITS)
        encode_response(self._profiles[operation], payload)
        return payload

    def _replay(self, entry, operation, profile, members, period, now):
        committed = (self._committed(entry["content"])
                     if entry["revision"] == profile.revision and entry["period"] == period else None)
        if committed is None:
            raise ProtocolError("mission_retry_stale", "Mission state, period or content changed after this request", 409)
        if entry["operation"] != operation or entry["state"] != _state_digest(profile.state):
            _history_invalid()
        return committed._rebuild(entry, operation, profile, members, now)

    def _rebuild(self, entry, operation, profile, members, now):
        identifiers, mail_ids = tuple(entry["ids"]), tuple(entry["mail_ids"])
        point_audit = entry["event_pass_points"]
        rows = {row["ID"]: row for row in self._snapshot(profile.state, now)}
        if not identifiers or not set(identifiers) <= members:
            _history_invalid()
        # 54 drops missions whose validity dates closed after the receipt; the receipt's state digest still binds them.
        if any(identifier not in rows and self.catalog.is_active(self.catalog.by_id[identifier], now)
               for identifier in identifiers):
            _history_invalid()
        shown = [rows[identifier] for identifier in identifiers if identifier in rows]
        if operation == 60:
            if mail_ids or any(row["Read"] != 1 for row in shown):
                _history_invalid()
        else:
            if any(row["Status"] != 2 for row in shown):
                _history_invalid()
            rewards = self._rewards(identifiers)
            expected_rows = []
            for reward in rewards:
                kind = reward["kind"]
                expected_type = {"unit": 1, "free_crystal": 0, "gold": 2, "item": 22}.get(kind,
                                                                                           reward.get("present_type"))
                expected_content = reward.get("template_id", reward.get("amount"))
                count = 1
                if (kind == "item" and self.item_tickets is not None
                        and self.item_tickets.catalog.supports_item(reward["item_id"])):
                    self.item_tickets.validate_item(reward["item_id"])
                    count, expected_content = reward["amount"], 1
                if count > MAX_PRESENTS - len(expected_rows):
                    _history_invalid()
                expected_rows.extend((expected_type, expected_content, reward.get("item_id")) for _ in range(count))
            if len(mail_ids) != len(expected_rows):
                _history_invalid()
            indexed = {row["PresentID"]: row for row in self.presents.rows(profile.state)}
            for identifier, (expected_type, expected_content, item_id) in zip(mail_ids, expected_rows):
                row = indexed.get(identifier)
                if row is None or row["Description"] != DESCRIPTION:
                    _history_invalid()
                if (row["Type"] != expected_type or row["Content"] != expected_content
                        or row.get("ItemID") != item_id):
                    _history_invalid()
            definitions = [self.catalog.by_id[identifier] for identifier in identifiers]
            has_points = any(row.event_pass_item_id or row.event_pass_point for row in definitions)
            if has_points != (point_audit is not None):
                _history_invalid()
            if point_audit is not None:
                if self.pass_points is None:
                    _history_invalid()
                self._validate_point_delivery(profile.state, entry["request"], point_audit)
                self.pass_points.validate_receipt(profile.state, definitions, point_audit)
                self._preflight_points(profile, profile.state)
        self.preflight_snapshot(profile.state, now, profile)
        self._preflight_mail(profile, profile.state)
        saved = entry["response"].encode("utf-8")
        rebuild = partial(self._response, operation, profile, profile.state, identifiers, mail_ids, point_audit)
        # Receipts saved before every POSSESSION carried Enabled keep their two-column table.
        if rebuild() != saved and rebuild(legacy_possessions=True) != saved:
            _history_invalid()
        return saved

    def _write(self, operation, envelope, store):
        try:
            return self._commit(operation, envelope, store)
        except ProtocolError as error:
            log.warning("Mission %d refused with %s; the native client shows a communication error and resends "
                        "the same request", operation, error.code)
            raise

    def _unchanged(self, operation, profile, requested):
        # Native senders act on their last 54 snapshot and resend any refused request, so a stale scope answers empty.
        log.warning("Mission %d found nothing to apply for ID=%d PT=%d TabGroupId=%d TaskGroupId=%d; replying unchanged",
                    operation, requested["ID"], requested["PT"], requested["TabGroupId"], requested["TaskGroupId"])
        return self._response(operation, profile, profile.state, (), ())

    def _commit(self, operation, envelope, store):
        values = registered(envelope, {operation}, {"GL"}, {"GL"})
        requested = fields(values["GL"], {"ID", "PT", "TabGroupId", "TaskGroupId"},
                           {"ID", "PT", "TabGroupId", "TaskGroupId"})
        requested = {name: scalar(value, "I") for name, value in requested.items()}
        now = instant(self.bootstrap.clock())
        key = hashlib.sha256(f"{operation}:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        period = period_key(requested["PT"], business_date(now))
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            scope = self._scope(operation, requested, now)
            history = self._ledger.read(profile.state, profile.revision)
            self._validate_point_delivery(profile.state)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                return self._replay(cached, operation, profile, scope.members, period, now)
            if self._ledger.processed(history, key):
                raise ProtocolError("mission_request_stale", "Mission request was already processed", 409)
            selection = self._select(profile.state, scope.targets, now)
            identifiers = tuple(identifier for identifier, row in selection.rows.items() if _applies(operation, row))
            if not identifiers:
                return self._unchanged(operation, profile, requested)
            mail_ids, point_audit = (), ()
            if operation == 60:
                updated = self.progress.mark_read(selection.state, identifiers, now)
            else:
                claim = self.grant_missions(selection, now)
                updated, identifiers, mail_ids = claim.state, claim.claimed, claim.mail_ids
                point_audit = claim.point_audit
                # The delivery marker binds the EP audit to this receipt, so only native claims write it.
                if point_audit:
                    updated[PASS_DELIVERY_KEY] = {"schema_version": 1, "request": key,
                                                  "audit": deepcopy(list(point_audit))}
                    self._preflight_points(profile, updated)
            self.preflight_snapshot(updated, now, profile)
            self._preflight_mail(profile, updated)
            response = self._response(operation, profile, updated, identifiers, mail_ids, point_audit)
            self._ledger.record(updated, history, {
                "request": key, "content": self.content_version, "state": _state_digest(updated),
                "revision": profile.revision + 1, "response": response.decode("utf-8"), "operation": operation,
                "period": period, "ids": list(identifiers), "mail_ids": list(mail_ids),
                "event_pass_points": list(point_audit) or None})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving mission results", 409)

    def claim(self, envelope, store):
        return self._write(58, envelope, store)

    def claim_group(self, envelope, store):
        return self._write(59, envelope, store)

    def mark_read(self, envelope, store):
        return self._write(60, envelope, store)

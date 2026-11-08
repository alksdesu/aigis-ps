from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import re

from .alxml import Column, Container, Limits, dumps
from .card_status import CARD_STATUS_FIELDS
from .energy_clock import instant
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError, caught
from .fame_catalog import STAGES
from .mission_state import JOURNAL_KEY as MISSION_JOURNAL
from .native_numbers import int32
from .player_state import DISPLAY_FIELD, MAX_PLAYER_UNITS, PRINCE_CARD_IDS, UNIT_BOOLEANS, UNIT_FIELDS, barracks_index
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, scalar, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .unit_removal import consume_units


RESPONSE_LIMIT = 4 * 1024 * 1024
HISTORY_NAMESPACE = "fame_history"
REGISTER, DEPOSIT, WITHDRAW = 149, 150, 151
OPERATIONS = (REGISTER, DEPOSIT, WITHDRAW)
FAME_LIMITS = Limits(max_bytes=RESPONSE_LIMIT,
    max_values=MAX_PLAYER_UNITS * (len(UNIT_FIELDS) + len(CARD_STATUS_FIELDS) + 2) + 16,
    max_elements=MAX_PLAYER_UNITS * (len(UNIT_FIELDS) + len(CARD_STATUS_FIELDS) + 2) + 128)
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_MAXIMUM_FIELDS = frozenset(("ClassID", "ClassChangeCount", "Exp", "Love", "SkillLevel",
    "CostModMixValue", "SettingSkillID", "GeneralPurposeFlags", "SkillEvolved", "ClassChangeStatusMod",
    "SkillExperience", "CostExperience", DISPLAY_FIELD, "WatchLoveEv", "CanFame", "Famed"))
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _bad_history():
    raise ProtocolError("fame_history_invalid", "Stored fame transaction receipts are invalid", 503)


def _identifiers(values):
    return (isinstance(values, list) and 0 < len(values) <= MAX_PLAYER_UNITS and len(set(values)) == len(values)
            and all(int32(value) and value > 0 for value in values))


def _receipt_valid(entry):
    return (type(entry["operation"]) is int and entry["operation"] in OPERATIONS
            and _identifiers(entry["units"]) and _identifiers(entry["cards"]))


def _selection(indices, maximum):
    if (not isinstance(indices, tuple) or not 1 <= len(indices) <= maximum
            or any(not int32(index) or index < 0 for index in indices) or len(set(indices)) != len(indices)):
        invalid("Fame unit selections must contain distinct nonnegative identities")
    return indices


def _withdrawal(card_id, stage):
    if not int32(card_id) or card_id <= 0 or not int32(stage) or stage not in STAGES:
        invalid("Fame withdrawal requires a positive CardId and stage zero through four")
    return card_id, stage


@dataclass(frozen=True)
class FameRequest:
    operation: int
    indices: tuple[int, ...]
    card_id: int
    stage: int


@dataclass(frozen=True)
class FameChange:
    state: dict
    units: tuple[int, ...]
    cards: tuple[int, ...]
    skipped: tuple[tuple[int, str], ...] = ()


def _request(envelope, deposit_batch_limit):
    keys = envelope.profile.registration_keys
    if keys not in ((REGISTER,), (DEPOSIT,), (WITHDRAW,)):
        invalid("Expected a fame registration, deposit or withdrawal operation")
    operation = keys[0]
    group = {REGISTER: "FAMERS", DEPOSIT: "DEPOSIT", WITHDRAW: "WITHDRAW"}[operation]
    root = registered(envelope, {operation}, {group}, {group})
    if operation == WITHDRAW:
        row = fields(root[group], {"CardId", "EvolutionId"}, {"CardId", "EvolutionId"})
        card_id, stage = _withdrawal(scalar(row["CardId"], "I"), scalar(row["EvolutionId"], "I"))
        indices = ()
    else:
        name = "UnitId" if operation == REGISTER else "InviteId"
        row = fields(root[group], {name}, {name})
        maximum = MAX_PLAYER_UNITS if operation == REGISTER else deposit_batch_limit
        indices = _selection(values(row[name], "I", minimum=1, maximum=maximum), maximum)
        card_id, stage = 0, 0
    return FameRequest(operation, indices, card_id, stage)


class FameService:
    def __init__(self, bootstrap, players, repository, catalog, card_status, gifts,
                 evolution_rewards, missions, *, barracks):
        self.bootstrap, self.players = bootstrap, players
        self.repository, self.catalog, self.card_status = repository, catalog, card_status
        self.gifts, self.evolution_rewards, self.missions = gifts, evolution_rewards, missions
        self.barracks = barracks
        self.rank_min = getattr(catalog, "rank_min", None)
        self.deposit_batch_limit = getattr(catalog, "deposit_batch_limit", None)
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (41, 51, 137, REGISTER, DEPOSIT, WITHDRAW)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "fame_history_invalid", "Stored fame transaction receipts are invalid",
                                     fields=("request", "content", "state", "revision", "response", "operation", "units", "cards"),
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid)
        self.content_version = self._content()
        self._ready()

    def _content(self, missions=None, catalog=None, repository=None):
        try:
            versions = {"catalog": self.catalog.content_version if catalog is None else catalog,
                "repository": self.repository.content_version if repository is None else repository,
                "unit_initialization": self.players.initialization_version,
                "gifts": self.gifts.content_version, "evolution_rewards": self.evolution_rewards.content_version,
                "missions": self.missions.content_version if missions is None else missions,
                "barracks": self.barracks.content_version}
            if (any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in versions.values())
                    or not int32(self.rank_min) or self.rank_min <= 0 or self.rank_min != self.catalog.rank_min
                    or not int32(self.deposit_batch_limit) or not 0 < self.deposit_batch_limit <= MAX_PLAYER_UNITS
                    or self.deposit_batch_limit != self.catalog.deposit_batch_limit):
                raise ValueError("Fame dependencies require verified content versions")
            if (self.repository.catalog is not self.catalog or self.gifts.players is not self.players
                    or self.evolution_rewards.players is not self.players
                    or self.evolution_rewards.card_status is not self.card_status
                    or self.missions.players is not self.players or self.missions.fame is not self.catalog
                    or self.missions.fame_status is not self.card_status
                    or self.catalog.snapshot != self.card_status.snapshot):
                raise ValueError("Fame dependencies must share the player and history services")
            return _digest({"policy": 2, "dependencies": versions,
                "card_status": self.card_status.snapshot.identifier,
                "rank": self.rank_min, "deposit_batch_limit": self.deposit_batch_limit,
                "register": "primary_native_can_fame_each_unit_famed_reported_others_skipped_stage_time_preserved",
                "deposit": "primary_registered_stage_no_organized_units_locks_allowed",
                "withdraw": "one_new_identity_primary_maximum_template_no_bonus_rewards"})
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("fame_dependencies_invalid", "Fame dependencies are unavailable or inconsistent", 503) from error

    def _replayable(self, content):
        # Later dependencies reach this digest only via mission and catalog versions; repositories follow their catalog.
        if content == self.content_version:
            return True
        catalogs = {catalog: self.repository.content_version_for(catalog)
                    for catalog in self.catalog.replayable_content_versions()}
        return any(self._content(missions, catalog, repository) == content
                   for missions in self.missions.replayable_content_versions()
                   for catalog, repository in catalogs.items())

    def _missions_ready(self):
        return caught(self.missions._ready) is None

    def _ready(self):
        try:
            if (not self.catalog.is_ready() or not self.repository.is_ready() or not self._missions_ready()
                    or self.evolution_rewards.status().get("available") is not True
                    or self._content() != self.content_version):
                raise ValueError("Fame dependencies changed")
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("fame_dependencies_changed", "Fame dependencies changed after setup", 503) from error

    def is_ready(self):
        return caught(self._ready) is None

    def handlers(self):
        return {REGISTER: self.register, DEPOSIT: self.deposit, WITHDRAW: self.withdraw}

    def status(self):
        failure = caught(self._ready)
        return {"ready": failure is None, "error": None if failure is None else failure.code,
                "implemented_operations": list(OPERATIONS),
                "content_version": self.content_version, "catalog": self.catalog.status(),
                "minimum_rank": self.rank_min, "maximum_deposit_batch": self.deposit_batch_limit,
                "registration_barracks": [0],
                "deposit_barracks": [0],
                "registration_policy": "Each listed first-barracks instance that passes the native CanFame rule is registered, "
                                       "already registered ones are reported again, the rest are skipped and logged",
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER,
                "refusals": REFUSAL_POLICY,
                "withdrawal_policy": "New primary-barracks identity using the verified maximum-growth template",
                "retry_policy": "Exact request at the committed revision, state and content, "
                                "also once optional mission or growth dependencies attach later",
                "native_tested": False}

    @staticmethod
    def _ppu(units, *, updating=False):
        return Container("PPU", tuple(Column(alias,
            "B" if name in UNIT_BOOLEANS and not (updating and name == "Famed") else "I",
            tuple(int(unit[name]) if updating and name == "Famed" else unit[name] for unit in units))
            for name, alias in UNIT_FIELDS))

    @staticmethod
    def _instance(owned, usage, index):
        unit = owned.get(index)
        if unit is None:
            raise ProtocolError("fame_unit_missing", "Fame selection does not identify an owned instance", 409)
        source = barracks_index(unit)
        if source >= len(usage.capacities):
            raise ProtocolError("fame_barracks_unavailable", "Fame selection belongs to a locked barracks", 409)
        if unit["CardID"] in PRINCE_CARD_IDS:
            raise ProtocolError("fame_unit_protected", "Prince cannot be registered or deposited", 409)
        return unit, source

    def _owned(self, state, player, indices):
        usage = self.barracks.usage(state)
        owned = {unit["GetIndex"]: unit for unit in player["units"]}
        result = []
        for index in indices:
            unit, _ = self._instance(owned, usage, index)
            self.catalog.require_eligible(unit)
            result.append(unit)
        return result

    def _registrable(self, state, player, indices):
        usage = self.barracks.usage(state)
        owned = {unit["GetIndex"]: unit for unit in player["units"]}
        selected, refused = [], []
        for index in indices:
            try:
                unit, source = self._instance(owned, usage, index)
                if source:
                    raise ProtocolError("fame_registration_barracks", "Fame registration only accepts the first barracks", 409)
                if not unit["Famed"]:
                    self.catalog.require_eligible(unit)
            except ProtocolError as error:
                if error.status >= 500:
                    raise
                refused.append((index, error))
                continue
            selected.append(unit)
        # Native 71E340 resubmits every unfamed CanFame instance, so one divergent unit must not block the others forever.
        if not selected:
            raise refused[0][1]
        return selected, tuple((index, error.code) for index, error in refused)

    def _require_storable(self, statuses, card_id, stage):
        if stage not in self.catalog.stages(card_id):
            raise ProtocolError("fame_stage_unavailable", "Deposit stage has no supported withdrawal template", 409)
        if not self.catalog.registered(statuses, card_id, stage):
            raise ProtocolError("fame_registration_required", "Deposit requires a registered card stage", 409)

    def require_rank(self, row):
        if row["Rank"] < self.rank_min:
            raise ProtocolError("fame_rank_required", "Player rank is below the verified fame requirement", 409)

    def admit(self, state):
        # Console commands carry no request, so they pass the handler gates that precede the receipts on the save itself.
        self._ready()
        self.players.validate(state)
        self.require_rank(self.bootstrap.player_row(state))
        self.repository.read(state)
        self.card_status.read(state)

    def register_units(self, state, indices, now):
        player = self.players.validate(state)
        selected, skipped = self._registrable(state, player, _selection(indices, MAX_PLAYER_UNITS))
        now = instant(now)
        stamp = int(now.timestamp())
        if not int32(stamp) or stamp <= 0:
            raise ProtocolError("fame_clock_invalid", "Registration time must be a positive native Unix timestamp", 503)
        statuses = {row["CardID"]: row for row in self.card_status.read(state)}
        patches = {}
        fresh = [unit for unit in selected if not unit["Famed"]]
        for unit in fresh:
            card_id, stage = unit["CardID"], unit["ClassChangeCount"]
            if not int32(stage) or stage not in STAGES:
                raise ProtocolError("fame_stage_invalid", "Registration stage is outside the native history range", 503)
            field = f"B{stage}"
            if not statuses.get(card_id, {}).get(field, 0):
                patches.setdefault(card_id, {"CardID": card_id})[field] = stamp
        updated = self.card_status.update(state, list(patches.values()))
        saved = {unit["GetIndex"]: unit for unit in updated["player_state"]["units"]}
        for unit in fresh:
            saved[unit["GetIndex"]].update(Famed=True, CanFame=True)
        expected = deepcopy(updated)
        updated = self.missions.record_fame(state, updated, now)
        if (not isinstance(updated, dict)
                or {key: value for key, value in updated.items() if key != MISSION_JOURNAL}
                != {key: value for key, value in expected.items() if key != MISSION_JOURNAL}):
            raise ProtocolError("fame_hook_invalid", "Fame mission hook changed unrelated registration state", 503)
        return FameChange(updated, tuple(unit["GetIndex"] for unit in selected),
                          tuple(dict.fromkeys(unit["CardID"] for unit in selected)), skipped)

    def deposit_units(self, state, revision, indices):
        player = self.players.validate(state)
        indices = _selection(indices, self.deposit_batch_limit)
        selected = self._owned(state, player, indices)
        if any(barracks_index(unit) for unit in selected):
            raise ProtocolError("fame_deposit_barracks", "Fame deposit only accepts the verified first-barracks entry", 409)
        organized = {row["UnitIndex"] for row in player["squads"]}
        statuses = self.card_status.read(state)
        changes = Counter()
        for unit in selected:
            if unit["GetIndex"] in organized:
                raise ProtocolError("fame_unit_protected", "Organized instances cannot be deposited", 409)
            card_id, stage = unit["CardID"], unit["ClassChangeCount"]
            self._require_storable(statuses, card_id, stage)
            changes[card_id, stage] += 1
        updated = self.gifts.preserve_history(state, revision)
        updated = self.evolution_rewards.initialize(updated)
        updated = self.repository.adjust(updated, dict(changes))
        consume_units(updated["player_state"], selected)
        return FameChange(updated, indices, tuple(dict.fromkeys(unit["CardID"] for unit in selected)))

    def withdraw_unit(self, state, revision, card_id, stage):
        card_id, stage = _withdrawal(card_id, stage)
        if stage not in self.catalog.stages(card_id):
            raise ProtocolError("fame_stage_unavailable", "Withdrawal stage is not supported for this card", 409)
        if not self.catalog.registered(self.card_status.read(state), card_id, stage):
            raise ProtocolError("fame_registration_required", "Withdrawal requires existing card-stage registration", 409)
        maximum = self.catalog.maximum_values(card_id, stage)
        if (not isinstance(maximum, dict) or maximum.keys() != _MAXIMUM_FIELDS
                or any(type(value) is not bool if name in UNIT_BOOLEANS else not int32(value)
                       for name, value in maximum.items())
                or maximum["ClassChangeCount"] != stage
                or barracks_index(maximum)):
            raise ProtocolError("fame_template_invalid", "Withdrawal template contains invalid growth or identity fields", 503)
        self.players.require_unit_space(state, 1)
        updated = self.gifts.preserve_history(state, revision)
        updated = self.evolution_rewards.initialize(updated)
        updated = self.repository.adjust(updated, {(card_id, stage): -1})
        updated, added = self.players.credit_units(updated, [{"card_id": card_id, "level": 1, "count": 1}])
        index = added[0]["GetIndex"]
        target = next(unit for unit in updated["player_state"]["units"] if unit["GetIndex"] == index)
        target.update(maximum)
        self.catalog.require_eligible(target)
        updated = self.gifts.record_current_thresholds(updated, index, revision)
        reservations = self.players.quest_reservations(updated)
        if reservations:
            trial = deepcopy(updated)
            trial["quest_state"]["active"] = None
            self.players.credit_units(trial, reservations)
        return FameChange(updated, (index,), (card_id,))

    def adjust_stock(self, state, changes):
        self._ready()
        updated = self.repository.adjust(state, changes)
        statuses = self.card_status.read(state)
        # Added stock must stay withdrawable by 151, so increases follow the deposit rules.
        for (card_id, stage), delta in changes.items():
            if delta > 0:
                self._require_storable(statuses, card_id, stage)
        return updated

    def _response(self, profile, state, request, indices, card_ids):
        owned = {unit["GetIndex"]: unit for unit in state["player_state"]["units"]}
        indices, card_ids = tuple(indices), tuple(card_ids)
        if any(card_id not in self.catalog.card_ids for card_id in card_ids):
            _bad_history()
        if request.operation == REGISTER:
            reported = set(indices)
            if (not indices or len(reported) != len(indices)
                    or indices != tuple(index for index in request.indices if index in reported)
                    or any(index not in owned or not owned[index]["Famed"] for index in indices)):
                _bad_history()
            units = [owned[index] for index in indices]
            if tuple(dict.fromkeys(unit["CardID"] for unit in units)) != card_ids:
                _bad_history()
            children = (self._ppu(units, updating=True), self.card_status.response(state, card_ids),
                Container("FameRegister", (Column("InviteId", "I", indices),
                    Column("CardId", "I", tuple(unit["CardID"] for unit in units)))))
        elif request.operation == DEPOSIT:
            if indices != request.indices or any(index in owned for index in indices):
                _bad_history()
            children = (Container("PPU"), self.repository.response(state, card_ids))
        else:
            if (len(indices) != 1 or card_ids != (request.card_id,) or indices[0] not in owned
                    or owned[indices[0]]["CardID"] != request.card_id
                    or owned[indices[0]]["ClassChangeCount"] != request.stage):
                _bad_history()
            children = (self._ppu([owned[indices[0]]]), Container("PBP", (
                Column("A1", "S", ("UX",)), Column("A2", "I", (state["player_state"]["battle"]["UX"],)))),
                self.repository.response(state, card_ids))
        response = dumps(Container(children=(self.bootstrap._system(profile), *children)), limits=FAME_LIMITS)
        if len(response) > RESPONSE_LIMIT:
            raise ProtocolError("fame_response_capacity", "Fame response exceeds its receipt capacity", 503)
        encode_response(self._profiles[request.operation], response)
        return response

    def _preflight(self, profile, state, now):
        candidate = replace(profile, state=state)
        player = self.players.validate(state)
        self.bootstrap._player_row(candidate)
        self.repository.read(state)
        encode_response(self._profiles[41], self.players.unit_response(player["units"]))
        self.card_status.flat_response(state)
        encode_response(self._profiles[137], self.bootstrap._configuration_response(candidate))
        self.missions.preflight_snapshot(state, now, candidate)

    def register(self, envelope, store):
        return self._transact(envelope, store, REGISTER)

    def deposit(self, envelope, store):
        return self._transact(envelope, store, DEPOSIT)

    def withdraw(self, envelope, store):
        return self._transact(envelope, store, WITHDRAW)

    def _transact(self, envelope, store, operation):
        request = _request(envelope, self.deposit_batch_limit)
        if request.operation != operation:
            invalid("Fame request was sent to the wrong transaction handler")
        try:
            return self._commit(envelope, store, request)
        except ProtocolError as error:
            log.warning("Fame operation %d refused with %s; the native client %s",
                        operation, error.code, _REFUSAL_AFTERMATH)
            raise

    def _commit(self, envelope, store, request):
        operation = request.operation
        key = hashlib.sha256(f"{operation}:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        now = instant(self.bootstrap.clock())
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            self.require_rank(self.bootstrap._player_row(profile))
            self.repository.read(profile.state)
            self.card_status.read(profile.state)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                if cached["revision"] != profile.revision or not self._replayable(cached["content"]):
                    raise ProtocolError("fame_retry_stale", "Player state or fame content changed after this request", 409)
                if cached["state"] != _state_digest(profile.state) or cached["operation"] != operation:
                    _bad_history()
                self._preflight(profile, profile.state, now)
                response = self._response(profile, profile.state, request, cached["units"], cached["cards"])
                if response != cached["response"].encode("utf-8"):
                    _bad_history()
                return response
            if self._ledger.processed(history, key):
                raise ProtocolError("fame_request_stale", "Fame request was already processed", 409)
            if operation == REGISTER:
                change = self.register_units(profile.state, request.indices, now)
            elif operation == DEPOSIT:
                change = self.deposit_units(profile.state, profile.revision, request.indices)
            else:
                change = self.withdraw_unit(profile.state, profile.revision, request.card_id, request.stage)
            updated = change.state
            self._preflight(profile, updated, now)
            response = self._response(profile, updated, request, change.units, change.cards)
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                "state": _state_digest(updated), "revision": profile.revision + 1, "response": response.decode("utf-8"),
                "operation": operation, "units": list(change.units), "cards": list(change.cards)})
            try:
                store.update_state(updated, profile.revision)
            except ValueError:
                continue
            if change.skipped:
                log.warning("Fame registration %d left %s unregistered; the native client lists them again until they qualify",
                            operation, ", ".join(f"{index}:{code}" for index, code in change.skipped))
            return response
        raise ProtocolError("profile_changed", "Player state changed while saving fame transaction", 409)

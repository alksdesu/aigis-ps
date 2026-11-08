from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
import functools
import hashlib
import itertools
import json
import re
import secrets
from types import MappingProxyType

from . import quest_modes
from .alxml import Column, Container, Limits, dumps, loads
from .battle_events import ENTRUST, EVENT_PARAM_REWRITE, BattleEventContext, BattleEventGrant
from .energy_clock import instant, validate_clock
from .entrust_catalog import (
    REQUIRED_MAX_RANK, REWARD_UNSUPPORTED, RULES as CATALOG_RULES, CatalogCheck, EntrustDefinition,
)
from .errors import ProtocolError
from .native_numbers import INT32_MAX, int32
from .player_state import UNIT_BOOLEANS, UNIT_FIELDS
from .possessions import INSUFFICIENT as POSSESSION_SHORTAGE
from .protocol import ProtocolRegistry, encode_response
from .quest_economy import (
    GOLD_POLICY, _capacity, _ranks, _row, credit_quest, debit_quest, game_param_response, settle_energy,
)
from .quest_modes import (
    DAY_OPEN_GRACE, EntrustContext, EntrustDraw, EntrustRewards, MissionWindow, ModePolicy, SettleOutcome, StartContext,
)
from .quest_protocol import MAX_CANDIDATES, PlatformTally, _fields, _header, _parallel, _scalar, _teams, _TEAMS
from .quest_service import JOURNAL_KEY
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .storage import MAX_REVISION, PlayerProfile
from .wallet_money import GOLD_LIMIT


HISTORY_NAMESPACE = "entrust_history"
RECEIPT_FIELDS = ("request", "content", "state", "revision", "quest_id", "quantity", "response", "response_digest")
RESPONSE_LIMIT = 1024 * 1024
XML_LIMITS = Limits(max_values=500000, max_elements=500000)
REVISION_BYTES = 256
# Boost.V carries the native batch size of one entrust request.
RUNS = range(1, 4)
CONTENT_POLICY = 4
# Receipts committed before entrust gold took the public rates digest their record under policy 3 without "gold".
FLAT_GOLD_POLICY = 3
# An entrust repeats the three-star clear its eligibility requires.
CLEAR_STARS = REQUIRED_MAX_RANK
UNIT_GRANT_FIELDS = frozenset(("card_id", "level", "count"))
PASSED = "pass"
SCOPE = ("Audited QuestTable types whose native entrust gate is open and whose mode policy is enabled "
         "without undeclared battle hooks")
FREE_TICKET_RULE = "growth_campaign_entry_covering_the_quest_for_the_chosen_mission_at_the_jst_clock"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ABSENT = object()


class Stage(StrEnum):
    REQUEST = "request"
    RESOURCE_VERSION = "resource_version"
    AUTHENTICATION = "authentication"
    REPLAY = "exact_replay"
    PROCESSED = "processed_request"
    PRODUCT = "product"
    ELIGIBILITY = "eligibility"
    ADMISSION = "admission"
    CANDIDATES = "candidates"
    UNIT_SPACE = "unit_space"
    ENERGY = "energy_settlement"
    TICKET = "ticket"
    CHARISMA_CAPACITY = "charisma_capacity"
    STAMINA = "stamina"
    CHARISMA = "charisma"
    DRAW = "draw"
    REWARD_HOOK = "entrust_rewards"
    EVENT_HOOK = "battle_events"
    GRANT = "grant"
    EXPERIENCE = "experience_and_gold"
    SETTLE_HOOK = "entrust_settle"
    MISSIONS = "missions"
    RANK_REWARDS = "rank_rewards"
    PREFLIGHT = "preflight"
    RESPONSE = "response"
    RECEIPT = "receipt"


CHECK_ORDER = tuple(Stage)
# Receipts committed while the client CT still ordered requests named the processed-request check this way.
LEGACY_CHECK_ORDER = tuple("watermark" if stage is Stage.PROCESSED else stage for stage in CHECK_ORDER)
CHECK_ORDERS = (CHECK_ORDER, LEGACY_CHECK_ORDER)


@dataclass(frozen=True)
class EntrustRules:
    experience_up: bool = True

    def __post_init__(self):
        if type(self.experience_up) is not bool:
            raise ValueError("Entrust experience campaign rule must be boolean")


@dataclass(frozen=True)
class EntrustRequest:
    quest_id: int
    revision: str
    quantity: int
    candidates: tuple[tuple[int, int, int], ...] | None
    platform_code: int


@dataclass(frozen=True)
class EntrustPlan:
    mission: MissionWindow
    policy: ModePolicy
    ticket_cost: int
    state: dict
    max_rank: int


@dataclass
class EntrustTrace:
    checks: list[dict] = field(default_factory=list)
    failed: Stage | None = None
    mission: MissionWindow | None = None
    ticket_cost: int | None = None

    @contextmanager
    def stage(self, stage: Stage):
        try:
            yield
        except ProtocolError as error:
            self.checks.append({"stage": stage, "result": error.code})
            self.failed = stage
            raise
        self.checks.append({"stage": stage, "result": PASSED})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _content_digest(record: dict, check_order) -> str:
    return _digest({**record, "check_order": list(check_order)})


def _committed_records(record: dict) -> tuple[dict, dict]:
    flat_gold = {name: value for name, value in record.items() if name != "gold"}
    return record, {**flat_gold, "policy": FLAT_GOLD_POLICY}


def _replayable_versions(service) -> tuple:
    if service is None:
        return (None,)
    current = service.content_version
    return (current, *sorted(service.replayable_content_versions() - {current}))


def _fewest_masked(*choices) -> list[tuple]:
    # Every choice leads with its current version, so a receipt of one later attachment matches after few digests.
    return sorted(itertools.product(*choices), key=lambda versions: sum(
        version != choice[0] for version, choice in zip(versions, choices)))


def _state_digest(state):
    return _digest({name: value for name, value in state.items() if name != HISTORY_NAMESPACE})


def _request_invalid():
    return ProtocolError("entrust_request_invalid", "Entrust requires a quest, resource revision and one to three runs")


def _require_runs(quest_id, quantity) -> None:
    if type(quest_id) is not int or quest_id <= 0 or type(quantity) is not int or quantity not in RUNS:
        raise _request_invalid()


def _hook_invalid(message: str) -> ProtocolError:
    return ProtocolError("quest_mode_hook_invalid", message, 503)


def _native_count(value, minimum: int) -> bool:
    return int32(value) and value >= minimum


def _unit_grant(unit) -> bool:
    return (isinstance(unit, Mapping) and set(unit) == UNIT_GRANT_FIELDS and all(int32(value) for value in unit.values())
            and unit["count"] > 0)


def _clear_payload(product: EntrustDefinition, mission: MissionWindow) -> dict:
    return {"quest_id": product.quest_id, "quest_type": product.quest_type, "stars": CLEAR_STARS, "hard": False,
            "mission_id": mission.mission_id, "score_mode": product.mode.score_mode}


@functools.cache
def _reply_profile():
    return ProtocolRegistry.load().by_registration[76]


def _receipt_valid(receipt) -> bool:
    if (not int32(receipt["quest_id"]) or receipt["quest_id"] <= 0 or type(receipt["quantity"]) is not int
            or receipt["quantity"] not in RUNS or type(receipt["response_digest"]) is not str
            or not _HASH.fullmatch(receipt["response_digest"])):
        return False
    xml = receipt["response"].encode("utf-8")
    if hashlib.sha256(xml).hexdigest() != receipt["response_digest"]:
        return False
    loads(xml, limits=XML_LIMITS)
    encode_response(_reply_profile(), xml)
    return True


_LEDGER = RequestLedger(HISTORY_NAMESPACE, "entrust_history_invalid", "Stored entrust receipts are inconsistent",
                        fields=RECEIPT_FIELDS, response_limit=RESPONSE_LIMIT, check=_receipt_valid)


def read_history(state: dict, revision: int = MAX_REVISION) -> dict:
    # Readers without a stored revision bound the receipts by the store's ceiling.
    return _LEDGER.read(state, revision)


class EntrustService:
    def __init__(self, quests, catalog, *, rules=None, clock=None, randbelow=secrets.randbelow):
        self.quests, self.catalog = quests, catalog
        self.bootstrap, self.players, self.possessions = quests.bootstrap, quests.players, quests.possessions
        self.missions, self.rank_rewards = quests.missions, quests.rank_rewards
        self.rules = EntrustRules() if rules is None else rules
        self.clock = quests.clock if clock is None else clock
        if not isinstance(self.rules, EntrustRules):
            raise ValueError("Entrust rules must be verified entrust rules")
        if not callable(self.clock) or not callable(randbelow):
            raise ValueError("Entrust clock and random source must be callable")
        self.randbelow = randbelow
        self.platforms = PlatformTally()
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (34, 41, 42, 51, 54, 76, 129, 137)}
        self.catalog.check()
        record = self._content_record()
        self.content_version = _content_digest(record, CHECK_ORDER)
        self.legacy_content_version = _content_digest(record, LEGACY_CHECK_ORDER)
        self._committed_versions = frozenset(_content_digest(committed, order)
                                             for committed in _committed_records(record) for order in CHECK_ORDERS)

    def _content_record(self) -> dict:
        if (self.catalog.quests is not self.quests.catalog or self.catalog.possessions is not self.possessions
                or self.quests.players is not self.players or self.quests.bootstrap is not self.bootstrap
                or self.quests.missions is not self.missions or self.quests.rank_rewards is not self.rank_rewards):
            raise ProtocolError("entrust_dependencies_invalid", "Entrust dependencies must share quest and player services", 503)
        for service in (self.missions, self.rank_rewards):
            if service is not None:
                service._ready()
                if service.players is not self.players or service.bootstrap is not self.bootstrap:
                    raise ProtocolError("entrust_dependencies_invalid", "Entrust progression must use the same player state", 503)
        return {"policy": CONTENT_POLICY, "catalog": self.catalog.content_version,
            "initialization": self.players.initialization_version, "rules": asdict(self.quests.rules),
            "entrust_rules": asdict(self.rules), "ranks": self.bootstrap.rank_rows,
            "missions": None if self.missions is None else self.missions.content_version,
            "rank_rewards": None if self.rank_rewards is None else self.rank_rewards.content_version,
            **self._reward_sources(),
            "quantity": [RUNS.start, RUNS.stop - 1], "costs": "upfront", "gold": GOLD_POLICY,
            "capacity": "one_free_slot_then_technical_reservations", "battle": "preserve_active",
            "first_rewards": "never_repeat", "items": "stack_capped_overflow_reported",
            "mission_payload": "native_type_and_owning_mission"}

    def _content(self) -> str:
        return _content_digest(self._content_record(), CHECK_ORDER)

    def _replayable(self, content) -> bool:
        # A receipt reaches its digest through the catalog, mission, rank reward and event versions it was committed under.
        if content in self._committed_versions:
            return True
        records = _committed_records(self._content_record())
        choices = map(_replayable_versions, (self.catalog, self.missions, self.rank_rewards, self.quests.event_rewards))
        return any(_content_digest({**committed, "catalog": catalog, "missions": mission, "rank_rewards": rank,
                                    "event_rewards": event}, order) == content
                   for catalog, mission, rank, event in _fewest_masked(*choices)
                   for committed in records for order in CHECK_ORDERS)

    def _reward_sources(self) -> dict:
        return {"event_rewards": self.quests.event_rewards.content_version,
                "drop_bonus": self.quests.catalog.drop_bonus_table().content_version}

    def _ready(self) -> CatalogCheck:
        check = self.catalog.check()
        if self._content() != self.content_version:
            raise ProtocolError("entrust_dependencies_changed", "Entrust rules or definitions changed after setup", 503)
        return check

    def handlers(self):
        return {76: self.execute}

    @staticmethod
    def _request(envelope) -> EntrustRequest:
        columns = _header(envelope, _TEAMS | {"QI", "Rev", "RT", "Boost"}, frozenset(("QI", "Rev")), frozenset((76,)))
        _teams(columns)
        quest_id, revision = _scalar(columns, "QI"), _scalar(columns, "Rev", "S")
        quantity = _scalar(_fields(columns["Boost"], ("V",), ("V",)), "V") if "Boost" in columns else RUNS.start
        if not revision or len(revision.encode("utf-8")) > REVISION_BYTES:
            raise _request_invalid()
        _require_runs(quest_id, quantity)
        platform = quest_modes.require_platform(_scalar(columns, "PlatformCode"))
        candidates = None
        if "RT" in columns:
            table = _fields(columns["RT"], ("ID", "TYPE", "ATTR"))
            if table:
                if set(table) != {"ID", "TYPE", "ATTR"}:
                    raise ProtocolError("entrust_request_invalid", "Entrust candidate columns must be complete")
                candidates = _parallel(table, ("ID", "TYPE", "ATTR"), maximum=MAX_CANDIDATES)
        return EntrustRequest(quest_id, revision, quantity, candidates, platform)

    def _replay(self, receipt, profile, request) -> bytes:
        if (receipt["quest_id"], receipt["quantity"]) != (request.quest_id, request.quantity):
            _LEDGER.invalid()
        if receipt["revision"] != profile.revision or not self._replayable(receipt["content"]):
            raise ProtocolError("entrust_retry_stale", "Entrust state or content changed after this request", 409)
        if receipt["state"] != _state_digest(profile.state):
            _LEDGER.invalid()
        return receipt["response"].encode("utf-8")

    def initialize(self, state):
        self._ready()
        validate_clock(state)
        read_history(state)
        return deepcopy(state)

    def _plan(self, profile, product, quantity, candidates, platform, now, trace=None) -> EntrustPlan:
        trace = EntrustTrace() if trace is None else trace
        quest, rules = product.quest, self.quests.rules
        policy = self.catalog.scopes[product.quest_type].policy
        with trace.stage(Stage.ADMISSION):
            records = self.quests._records(profile)
            context = StartContext(
                catalog=self.quests.catalog, quest=quest, mode=product.mode, records=records,
                rank=self.bootstrap._player_row(profile)["Rank"], now=now, hard=False, platform_code=platform,
                state=deepcopy(profile.state), request=None, services=self.quests._services(),
                selector=self.catalog.selector(product, now=now, platform_code=platform))
            trace.mission = quest_modes.admit(policy, context)
            self.quests.event_missions.require_settled(quest.quest_id, trace.mission)
        # Like a battle start, the record's raw MaxRank selects the prize candidates.
        max_rank = self.quests._record(records, quest.quest_id)["MaxRank"]
        if candidates is not None:
            with trace.stage(Stage.CANDIDATES):
                expected = quest_modes.candidate_attributes(records.get(quest.quest_id), quest.candidates_for(max_rank))
                if candidates != expected:
                    raise ProtocolError("quest_candidates_mismatch", "Entrust candidates differ from the verified quest", 409)
        with trace.stage(Stage.UNIT_SPACE):
            self.players.require_unit_space(profile.state, 1)
        with trace.stage(Stage.ENERGY):
            state = settle_energy(profile.state, self.bootstrap.rank_rows, now,
                                  recovery_seconds=rules.charisma_recovery_seconds)
        with trace.stage(Stage.TICKET):
            trace.ticket_cost = self.catalog.ticket_cost(product, trace.mission, quantity, now)
            if trace.ticket_cost:
                state = self._debit_tickets(state, product.ticket_item_id, trace.ticket_cost)
        with trace.stage(Stage.CHARISMA_CAPACITY):
            wallet = _row(state)
            _, capacities = _ranks(self.bootstrap.rank_rows, wallet)
            if quest.charisma > _capacity(wallet, capacities, wallet["Rank"]):
                raise ProtocolError("entrust_energy_capacity", "The effective charisma maximum cannot cover one run", 409)
        with trace.stage(Stage.STAMINA):
            if quest.action_points:
                raise ProtocolError("entrust_stamina_capacity_unverified", "Entrust stamina limits require a verified native provider", 501)
        with trace.stage(Stage.CHARISMA):
            state = debit_quest(state, self.bootstrap.rank_rows, now, charisma=quest.charisma * quantity,
                                action_points=quest.action_points * quantity,
                                recovery_seconds=rules.charisma_recovery_seconds)
        return EntrustPlan(trace.mission, policy, trace.ticket_cost, state, max_rank)

    def _debit_tickets(self, state, item_id, cost):
        try:
            return self.possessions.debit(state, {item_id: cost})
        except ProtocolError as error:
            if error.code != POSSESSION_SHORTAGE:
                raise
            raise ProtocolError("entrust_ticket_insufficient", "Entrust tickets do not cover the requested runs", 409) from error

    def _drops(self, codes, quantity):
        counts = Counter()
        for _ in range(quantity):
            for code in codes:
                roll = self.randbelow(10000)
                if type(roll) is not int or not 0 <= roll < 10000:
                    raise ProtocolError("entrust_random_invalid", "Entrust random source returned an invalid roll", 503)
                if roll < self.quests.rules.drop_basis_points:
                    counts[code] += 1
        return counts

    def _draw(self, product, counts) -> EntrustDraw:
        units, items = [], {}
        for code, count in counts.items():
            reward = self.quests.catalog.decode_reward(code)
            amount = count * reward.count
            if reward.kind == "unit":
                units.append(MappingProxyType({"card_id": reward.card_id, "level": product.quest.unit_level, "count": amount}))
            elif reward.kind == "item":
                items[reward.item_id] = items.get(reward.item_id, 0) + amount
            elif reward.kind != "event_param":
                raise REWARD_UNSUPPORTED.error()
        return EntrustDraw(MappingProxyType(dict(counts)), tuple(units), MappingProxyType(items))

    def _creditable(self, items) -> bool:
        return isinstance(items, Mapping) and all(
            self.possessions.known(item_id) and _native_count(amount, 1) for item_id, amount in items.items())

    def _adjusted(self, codes, rewards) -> EntrustRewards:
        if not isinstance(rewards, EntrustRewards) or not isinstance(rewards.draw, EntrustDraw):
            raise _hook_invalid("Mode entrust reward hook must return entrust rewards")
        draw, codes = rewards.draw, frozenset(codes)
        if (not isinstance(draw.counts, Mapping)
                or any(type(code) is not int or code not in codes or not _native_count(count, 0)
                       for code, count in draw.counts.items())
                or not isinstance(draw.units, tuple) or any(not _unit_grant(unit) for unit in draw.units)
                or not self._creditable(draw.items)):
            raise _hook_invalid("Mode entrust reward hook returned an invalid draw")
        children = self.quests._hook_children(rewards.children)
        # TreasureInfo lists only committed drops, so a hook that zeroes a code removes it.
        counts = MappingProxyType({code: count for code, count in draw.counts.items() if count})
        units = tuple(MappingProxyType(dict(unit)) for unit in draw.units)
        return EntrustRewards(EntrustDraw(counts, units, MappingProxyType(dict(draw.items))), children)

    def _events(self, state, product, quantity, counts, now) -> BattleEventGrant:
        points = sum(count * reward.count for code, count in counts.items()
                     if (reward := self.quests.catalog.decode_reward(code)).kind == "event_param")
        grant = self.quests.event_rewards.evaluate(state, BattleEventContext(
            ENTRUST, product.quest_id, product.quest_type, product.quest.charisma, quantity, points, now,
            EVENT_PARAM_REWRITE[ENTRUST]))
        if not self._creditable(grant.items):
            raise ProtocolError("battle_event_grant_invalid", "Battle event grants must credit known items", 503)
        return grant

    def _grant(self, state, units, items) -> tuple[dict, list[dict], dict[str, int]]:
        updated, additions = self.players.credit_units(state, [dict(unit) for unit in units])
        discarded = {}
        if items:
            updated, discarded = self.possessions.credit_capped(updated, dict(items))
        return updated, additions, discarded

    def _settled(self, policy, context) -> SettleOutcome:
        outcome = policy.entrust_settle(replace(context, state=deepcopy(context.state)))
        if (not isinstance(outcome, SettleOutcome) or not isinstance(outcome.state, dict)
                or not isinstance(outcome.item_ids, tuple)
                or any(not self.possessions.known(item_id) for item_id in outcome.item_ids)
                or not isinstance(outcome.presents, tuple)):
            raise _hook_invalid("Mode entrust settlement hook returned an invalid outcome")
        if outcome.state.get(JOURNAL_KEY, _ABSENT) != context.state.get(JOURNAL_KEY, _ABSENT) or outcome.presents:
            raise _hook_invalid("Mode entrust settlement hooks cannot change the battle journal or send presents")
        self.quests._hook_children(outcome.children)
        return outcome

    def _preflight(self, profile, state, now):
        candidate = replace(profile, state=state)
        player = self.players.validate(state)
        encode_response(self._profiles[34], dumps(Container(children=(game_param_response(state),))))
        encode_response(self._profiles[41], self.players.unit_response(player["units"]))
        encode_response(self._profiles[51], dumps(Container(children=self.bootstrap.columns("quest_records",
            self.bootstrap._rows(candidate, "quest_records", single=False)))))
        encode_response(self._profiles[129], dumps(Container(children=(self.possessions.response(state, include_enabled=True),))))
        encode_response(self._profiles[137], self.bootstrap._configuration_response(candidate))
        if self.missions is not None:
            self.missions.preflight_snapshot(state, now, candidate)
        if self.rank_rewards is not None:
            self.rank_rewards._preflight(state, candidate)

    def _response(self, profile, state, *, units, counts, item_ids, discarded, mail_ids, event_param, children):
        before = self.bootstrap._player_row(profile)
        after = self.bootstrap.player_row(state)
        nodes = [self.bootstrap._system(profile), game_param_response(state, event_param=event_param),
            Container("PBP", (Column("A1", "S", ("UX",)), Column("A2", "I", (state["player_state"]["battle"]["UX"],)))),
            Container("PPU", tuple(Column(wire, "B" if name in UNIT_BOOLEANS else "I",
                tuple(unit[name] for unit in units)) for name, wire in UNIT_FIELDS)),
            self.quests._reward(after["Experience"] - before["Experience"], after["Money"] - before["Money"]),
            Container("TREASURE"), Container("TreasureInfo", (Column("Index", "I", tuple(range(len(counts)))),
                Column("DropID", "I", tuple(counts)), Column("Count", "I", tuple(counts.values()))))]
        if item_ids:
            nodes.append(self.possessions.response(state, item_ids, include_enabled=True))
        if discarded:
            nodes.append(self.possessions.overflow_response(discarded))
        if mail_ids:
            nodes.append(self.rank_rewards.presents.response(state, mail_ids))
        nodes.extend(children)
        return dumps(Container(children=tuple(nodes)), limits=XML_LIMITS)

    def execute(self, envelope, store):
        request = self._request(envelope)
        self.platforms.record(request.platform_code)
        quest_id, quantity = request.quest_id, request.quantity
        key = hashlib.sha256(b"76\0" + envelope.xml).hexdigest()
        now, draws = None, {}
        for _ in range(3):
            check = self._ready()
            if request.revision != self.quests.catalog.resource_version:
                raise ProtocolError("entrust_resource_mismatch", "Entrust resource revision differs from the current catalog", 409)
            profile = self.bootstrap._authenticate(envelope, store)
            validate_clock(profile.state)
            history = read_history(profile.state, profile.revision)
            receipt = _LEDGER.receipt(history, key)
            if receipt is not None:
                return self._replay(receipt, profile, request)
            if _LEDGER.processed(history, key):
                raise ProtocolError("entrust_request_stale", "Entrust request was already processed", 409)
            product = self.catalog.product(quest_id, check=check)
            self.catalog.require_eligible(profile.state, product, check=check)
            if now is None:
                now = instant(self.clock())
            plan = self._plan(profile, product, quantity, request.candidates, request.platform_code, now)
            codes = product.reward_codes(plan.max_rank)
            # Retries keep the first draw; only a MaxRank change between rounds brings a new candidate set to draw.
            if codes not in draws:
                draws[codes] = self._drops(codes, quantity)
            context = EntrustContext(catalog=self.quests.catalog, quest=product.quest, mode=product.mode,
                                     state=deepcopy(plan.state), mission_id=plan.mission.mission_id, quantity=quantity,
                                     now=now, platform_code=request.platform_code, services=self.quests._services())
            rewards = self._adjusted(codes, plan.policy.entrust_rewards(context, self._draw(product, draws[codes])))
            events = self._events(plan.state, product, quantity, rewards.draw.counts, now)
            items = Counter(rewards.draw.items)
            items.update(events.items)
            updated, additions, discarded = self._grant(plan.state, rewards.draw.units, items)
            if events.event_param is not None:
                updated["tables"]["game_param"][0]["EventParam"] = events.event_param
            experience = self.quests.experience_reward(product.quest, self.quests.rules, profile, now,
                                                       experience_up=self.rules.experience_up)
            # Entrusts count no GoldGet, pay the three-star rate and take the abilities of every owned unit.
            money = self.quests.gold_reward(product.quest, self.quests.rules, profile, now, stars=CLEAR_STARS,
                                            units=self.players.validate(profile.state)["units"])
            # Credits past the last rank or the gold cap are discarded, so larger sums settle like the caps themselves.
            updated = credit_quest(updated, self.bootstrap.rank_rows, now,
                experience=min(experience * quantity, INT32_MAX), money=min(money * quantity, GOLD_LIMIT),
                refill_on_rank_up=self.quests.rules.refill_on_rank_up,
                recovery_seconds=self.quests.rules.charisma_recovery_seconds)
            outcome = self._settled(plan.policy, replace(context, state=updated))
            updated = outcome.state
            if self.missions is not None:
                for _ in range(quantity):
                    updated = self.missions.record(updated, "quest_clear", _clear_payload(product, plan.mission), now)
            mail_ids = ()
            if self.rank_rewards is not None:
                updated, mail_ids = self.rank_rewards.apply(profile.state, updated, profile)
            self._preflight(profile, updated, now)
            tickets = (product.ticket_item_id,) if plan.ticket_cost else ()
            response = self._response(profile, updated, units=additions, counts=rewards.draw.counts,
                                      item_ids=tuple(dict.fromkeys((*items, *outcome.item_ids, *tickets))),
                                      discarded=discarded, mail_ids=mail_ids, event_param=events.event_param_changed,
                                      children=(*rewards.children, *outcome.children))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("entrust_response_capacity", "Entrust reply exceeds its receipt limit", 503)
            _LEDGER.record(updated, history, {"request": key, "content": self.content_version,
                "state": _state_digest(updated), "revision": profile.revision + 1, "quest_id": quest_id,
                "quantity": quantity, "response": response.decode("utf-8"),
                "response_digest": hashlib.sha256(response).hexdigest()})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving entrust rewards", 409)

    def preview(self, profile, quest_id, *, quantity=RUNS.start, now=None, platform_code) -> dict:
        if not isinstance(profile, PlayerProfile):
            raise ValueError("Entrust previews read a stored player profile")
        check = self._ready()
        trace, product, failure = EntrustTrace(), None, None
        try:
            with trace.stage(Stage.REQUEST):
                _require_runs(quest_id, quantity)
                platform = quest_modes.require_platform(platform_code)
                moment = instant(self.clock() if now is None else now)
            candidate = replace(profile, state=deepcopy(profile.state))
            with trace.stage(Stage.PRODUCT):
                product = self.catalog.product(quest_id, check=check)
            with trace.stage(Stage.ELIGIBILITY):
                self.catalog.require_eligible(candidate.state, product, check=check)
            self._plan(candidate, product, quantity, None, platform, moment, trace)
        except ProtocolError as error:
            failure = error
        return {"ok": failure is None, "code": None if failure is None else failure.code,
                "message": None if failure is None else str(failure),
                "status": None if failure is None else failure.status, "stage": trace.failed,
                "quest_type": self.quests.catalog.quest_type(quest_id),
                "mission_id": None if trace.mission is None else trace.mission.mission_id,
                "ticket": self._preview_ticket(product, trace.ticket_cost),
                "charisma": {"per_run": None if product is None else product.quest.charisma,
                             "total": None if product is None else product.quest.charisma * quantity},
                "checks": list(trace.checks)}

    @staticmethod
    def _preview_ticket(product, cost) -> dict:
        if product is None:
            return {"item": None, "per_run": None, "free": None, "cost": None}
        return {"item": product.ticket_item_id, "per_run": product.ticket_count,
                "free": None if cost is None else bool(product.ticket_count) and not cost, "cost": cost}

    def status(self):
        self._ready()
        return {"ready": True, "implemented_operations": [76], "content_version": self.content_version,
            "catalog": self.catalog.status(), "quantity": list(RUNS),
            "local_rules": {"quest": asdict(self.quests.rules), "entrust": asdict(self.rules),
                            "mission_choice": CATALOG_RULES["mission_choice"], "free_ticket": FREE_TICKET_RULE,
                            "check_order": list(CHECK_ORDER),
                            "day_grace_seconds": int(DAY_OPEN_GRACE.total_seconds()), "platform": "require_platform"},
            "platform_codes_seen": self.platforms.status(), "scope": SCOPE, "native_tested": False,
            "battle_policy": "Preserve active attempts and their reward reservations",
            "gold_policy": "Each run pays the three-star rate, the strongest reward-gold ability among the owned units "
                           "of every barracks and MoneyUp, never GoldGet; gold beyond the cap is discarded",
            "replay_policy": "Exact request at committed revision, content and state digest, also once optional "
                             "mission or rank reward dependencies or a quest ticket provider missing for a known "
                             "setup reason attach later",
            "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT, "request_order": REQUEST_ORDER,
            "candidate_policy": "The quest record's raw MaxRank selects the RT candidates, the reward codes every run "
                                "draws and the codes the reward hook may report, as at a battle start",
            "drop_bonus_policy": "The mode entrust_rewards hook scales the request total of the DropBonus item once by "
                                 "the JST weekday of the request and returns BONUS; TreasureInfo counts stay unscaled",
            "event_reward_policy": "After entrust_rewards the shared battle event hook adds quest tickets of the vote "
                                   "group open at the request for every run to the single capped credit and rewrites "
                                   "EventParam only for event drops",
            "event_mission_reward_policy": "Admission refuses with 501 before tickets or charisma are charged when the "
                                           "chosen mission shows event rewards that no mode settles, as a start does",
            "reward_sources": self._reward_sources()}

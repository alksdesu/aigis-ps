from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
from itertools import combinations
import json
import logging
from types import MappingProxyType

from .alxml import AlXmlError, Column, Container, dumps, loads
from .energy_clock import instant
from .errors import ProtocolError
from .present_backlog import require_deliverable
from .protocol import ProtocolRegistry, encode_response
from .request_fields import registered, scalar, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER
from .storage import MAX_REVISION
from .tower_catalog import GATE_QUEST, REWARD_POLICY, SCORE_SOURCE, SCORING_POLICY, STAR_SOURCE, TowerCatalog
from .tower_state import (
    LEDGER, RECEIPT_RESPONSE_LIMIT, RECEIPTS, TEAM_FIELDS, TowerRules, claim as claim_stock,
    describe as describe_state, read_state, release_charge, rewards_due, saved_records, settle_clock, write_state,
)


FLOOR_INFO, TOWER_INFO, CRYSTAL_CLAIM = 66, 67, 68
OPERATIONS = (FLOOR_INFO, TOWER_INFO, CRYSTAL_CLAIM)
# 7EBE40 sends 66 for every tower on entering the portal, before 7FC280 stores a selected floor.
PORTAL_QUEST = 0
COMMIT_ATTEMPTS = 3
SCHEMA_VERSION = 1
SELF_HIGH_SCORE = "self"
# A player's own best carries no ranking title or blessing.
NO_TITLE = NO_BLESSING = 0
SOURCES = (SCORE_SOURCE, STAR_SOURCE)
FORCED_DAYS = 1
# Receipts saved before the rule defaults followed the official sources name these values in their versions.
LEGACY_RULE_DEFAULTS = MappingProxyType({"generation_hour": 0, "member_count_prince": True})
LOCAL_RULES = MappingProxyType({
    "entry": f"Floors start once quest {GATE_QUEST} is cleared, the tower's DiscloseQuestID is zero or cleared, the "
             "floor's AppearCondition, Rank and mission window hold, the barracks has room and held plus pending "
             "challenge items reach one; four-star starts are refused",
    "start_cost": "A start charges nothing and answers the unspent POSSESSION count; the clear that settles a floor "
                  "uses one ConsumeItemID; defeat, retreat, give-up and a restart charge nothing; attempts saved "
                  "before settlement charges paid at their start and are neither charged again nor refunded",
    "challenge_refill": "Each JST day turning at replenish_hour adds the summed DailyAutoAddCount of the open towers "
                        "sharing the item, capped at its Stack",
    "crystal_generation": "Each JST day turning at generation_hour adds the GenerationCount of the highest generation row "
                          "whose Score is at most TotalScore, capped at its PossessionLimit, for open towers only",
    "lazy_settlement": "66, 67, 68, tower starts and tower results settle the elapsed days first, so a result changes "
                       "TotalScore only after its generation day is counted",
    "first_access": "A tower's first settlement counts one elapsed day unless first_access_settles_day is false",
    "crystal_claim": "68 credits each requested tower min(stock, Stack - held) and keeps the rest; a tower without a "
                     "record claims nothing",
    "scoring": SCORING_POLICY,
    "totals": "A floor keeps its best score; TotalScore sums the tower's floor bests; TowerResult reports this run's "
              "Score with the settled TotalScore",
    "rewards": f"{REWARD_POLICY}; a tier whose present the battle journal cannot hold or an empty mailbox cannot "
               "take stays deferred and the next result on any tower retries it first",
    "high_score": "HighScorePlayer carries the player's own positive floor best with Exp and zero Title and Blessing "
                  "when high_score is self; HighScoreTeam then carries the A1 to B1 rows that the result setting that "
                  "best saved for its real non-prince formation units in A8 order, at most 15, and is omitted for a "
                  "best no result set; TowerHighScore is never sent",
    "portal": "66 with QUEST 0 answers PersonalRecord 0/0 with the StackCrystal of every tower and the POSSESSION of "
              "every challenge item and tower crystal",
    "replay": "68 replays its exact receipt at the committed revision under the current content version or the one "
              "claims saved before RewardType 3 paid; 66 and 67 recompute their idempotent settlement; 83 replays "
              "through the battle response cache",
})
log = logging.getLogger("aigis_ps")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _rule_versions(rules: TowerRules) -> tuple[str, ...]:
    changed = sorted(name for name, value in LEGACY_RULE_DEFAULTS.items() if getattr(rules, name) != value)
    variants = [replace(rules, **{name: LEGACY_RULE_DEFAULTS[name] for name in names})
                for size in range(1, len(changed) + 1) for names in combinations(changed, size)]
    return (rules.content_version, *(variant.content_version for variant in variants))


def _changed() -> ProtocolError:
    return ProtocolError("profile_changed", "Player state changed while settling the towers", 409)


def _admin_invalid(message: str) -> ProtocolError:
    return ProtocolError("tower_admin_invalid", message)


def _personal_record(score: int, total: int) -> Container:
    # A69590 reads Score as the floor best and TotalScore as the tower total.
    return Container("PersonalRecord", (Column("Score", "I", (score,)), Column("TotalScore", "I", (total,))))


def _high_score(score: int, experience: int) -> Container:
    return Container("HighScorePlayer", (Column("Score", "I", (score,)), Column("Exp", "I", (experience,)),
                                         Column("Title", "I", (NO_TITLE,)), Column("Blessing", "I", (NO_BLESSING,))))


def _high_score_team(rows) -> Container:
    return Container("HighScoreTeam", tuple(Column(alias, "I", tuple(row[name] for row in rows))
                                            for name, alias in TEAM_FIELDS))


def _stack_crystal(domain: dict, towers) -> Container:
    records = domain["towers"]
    counts = tuple(records[str(tower.mission_id)]["stack"] if str(tower.mission_id) in records else 0 for tower in towers)
    return Container("StackCrystal", (Column("MissionID", "I", tuple(tower.mission_id for tower in towers)),
                                      Column("Count", "I", counts)))


def _tower_items(towers) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*(tower.consume_item_id for tower in towers),
                                *(tower.crystal_item_id for tower in towers))))


def _record(domain: dict, tower) -> dict:
    record = domain["towers"].get(str(tower.mission_id))
    if record is None:
        raise ProtocolError("tower_not_started", "The tower has no saved record yet", 409)
    return record


class TowerService:
    def __init__(self, bootstrap, players, possessions, presents, catalog: TowerCatalog, *, rules: TowerRules,
                 clock=None):
        if not isinstance(catalog, TowerCatalog) or catalog.possessions is not possessions or presents is None:
            raise ProtocolError("tower_dependencies_invalid",
                                "Tower services require the tower catalog's possessions and the present service", 503)
        if not isinstance(rules, TowerRules):
            raise ProtocolError("tower_rules_invalid", "Tower services require verified tower rules", 503)
        self.clock = bootstrap.clock if clock is None else clock
        if not callable(self.clock):
            raise ValueError("Tower clock must be callable")
        self.bootstrap, self.players, self.possessions, self.presents = bootstrap, players, possessions, presents
        self.catalog, self.rules = catalog, rules
        self._claim_profile = ProtocolRegistry.load().by_registration[CRYSTAL_CLAIM]
        self.content_version = self._content(catalog.content_version)
        # Claims never mail tiers, so receipts saved before RewardType 3 paid still describe the same claim.
        self.legacy_content_version = self._content(catalog.legacy_content_version)
        # Default-rule changes never rewrite old claims, so every older default combination stays replayable.
        self.replayable_versions = frozenset(
            self._content(catalog_version, rules_version)
            for catalog_version in (catalog.content_version, catalog.legacy_content_version)
            for rules_version in _rule_versions(self.rules))

    def _content(self, catalog_version: str, rules_version: str | None = None) -> str:
        return _digest({"schema": SCHEMA_VERSION, "catalog": catalog_version,
                        "rules": self.rules.content_version if rules_version is None else rules_version})

    def handlers(self) -> dict:
        return {FLOOR_INFO: self.floor_info, TOWER_INFO: self.towers_info, CRYSTAL_CLAIM: self.claim}

    def floor_info(self, envelope, store) -> bytes:
        return self._logged(FLOOR_INFO, self._floor_info, envelope, store)

    def towers_info(self, envelope, store) -> bytes:
        return self._logged(TOWER_INFO, self._towers_info, envelope, store)

    def claim(self, envelope, store) -> bytes:
        return self._logged(CRYSTAL_CLAIM, self._claim, envelope, store)

    @staticmethod
    def _logged(operation: int, action, envelope, store) -> bytes:
        try:
            return action(envelope, store)
        except ProtocolError as error:
            log.warning("Tower operation %d refused with %s", operation, error.code)
            raise

    def _now(self) -> datetime:
        return instant(self.clock())

    def settle(self, state: dict, now: datetime) -> tuple[dict, tuple[str, ...]]:
        return settle_clock(state, self.catalog, self.rules, saved_records(state), now)

    def held(self, state: dict) -> dict[str, int]:
        return self.possessions.counts(state)

    def challenges(self, state: dict, tower, now: datetime) -> int:
        # The refill a start would settle first counts as held, so a pending day lets the start through.
        settled, _ = self.settle(state, now)
        return self.held(settled).get(tower.consume_item_id, 0)

    def charge_challenge(self, state: dict, tower, quest_id: int) -> tuple[dict, int]:
        state, due = release_charge(state, self.catalog, quest_id)
        if not due:
            return state, 0
        charged = min(due, self.held(state).get(tower.consume_item_id, 0))
        if charged < due:
            log.warning("Tower floor %d settles %d challenge items short of its start reservation; the balance "
                        "stops at zero", quest_id, due - charged)
        return (self.possessions.debit(state, {tower.consume_item_id: charged}) if charged else state), charged

    def player(self, state: dict) -> dict:
        return self.players.validate(state, references=False)

    def deliverable(self, reward: dict) -> str | None:
        try:
            require_deliverable(self.presents, reward)
        except ProtocolError as error:
            return error.code
        return None

    def rewards(self, state: dict) -> tuple[dict, tuple[dict, ...]]:
        # Deferred tiers wait on presents, not on their tower, so each tower result retries every started tower.
        records, started = saved_records(state), read_state(state, self.catalog)["towers"]
        presents = []
        for tower in self.catalog.towers:
            if str(tower.mission_id) in started:
                state, due, _ = rewards_due(state, self.catalog, tower.mission_id, records, self.deliverable)
                presents.extend(due)
        return state, tuple(presents)

    def _domain(self, profile) -> dict:
        return read_state(profile.state, self.catalog, revision=profile.revision)

    @staticmethod
    def _response(envelope, children) -> bytes:
        response = dumps(Container(children=tuple(children)))
        encode_response(envelope.profile, response)
        return response

    @staticmethod
    def _stored(store, profile, state: dict) -> bool:
        if state is profile.state:
            return True
        try:
            store.update_state(state, profile.revision)
        except ValueError:
            return False
        return True

    def _settled_view(self, envelope, store, view) -> bytes:
        now = self._now()
        for _ in range(COMMIT_ATTEMPTS):
            profile = self.players.authenticate(envelope, store)
            self._domain(profile)
            state, _ = self.settle(profile.state, now)
            response = self._response(envelope, (self.bootstrap._system(profile), *view(profile, state)))
            if self._stored(store, profile, state):
                return response
        raise _changed()

    def _floor_info(self, envelope, store) -> bytes:
        columns = registered(envelope, {FLOOR_INFO}, ("QUEST",), ("QUEST",))
        quest_id = scalar(columns["QUEST"], "I")
        tower = None if quest_id == PORTAL_QUEST else self.catalog.floor_tower(quest_id)
        return self._settled_view(envelope, store,
                                  lambda profile, state: self._floor_view(profile, state, quest_id, tower))

    def _floor_view(self, profile, state: dict, quest_id: int, tower) -> tuple[Container, ...]:
        domain = read_state(state, self.catalog)
        if tower is None:
            towers = self.catalog.towers
            return (_personal_record(0, 0), _stack_crystal(domain, towers),
                    self.possessions.response(state, _tower_items(towers), include_enabled=True))
        record = domain["towers"].get(str(tower.mission_id))
        best = 0 if record is None else record["floors"].get(str(quest_id), 0)
        children = [_personal_record(best, 0 if record is None else record["total"])]
        # 8004D0 hides the high score panel for a zero score, the same state the omit rule picks for every floor.
        if best and self.rules.high_score == SELF_HIGH_SCORE:
            team = record["teams"].get(str(quest_id))
            if team is not None:
                children.append(_high_score_team(team))
            children.append(_high_score(best, self.bootstrap._player_row(profile)["Experience"]))
        return (*children, _stack_crystal(domain, (tower,)),
                self.possessions.response(state, _tower_items((tower,)), include_enabled=True))

    def _towers_info(self, envelope, store) -> bytes:
        registered(envelope, {TOWER_INFO}, ())
        return self._settled_view(envelope, store, lambda profile, state: ())

    def _claim(self, envelope, store) -> bytes:
        columns = registered(envelope, {CRYSTAL_CLAIM}, ("MISSION",), ("MISSION",))
        missions = values(columns["MISSION"], "I", minimum=1, maximum=len(self.catalog.towers))
        key = hashlib.sha256(f"{envelope.profile.identity}:{envelope.profile.route}:{envelope.digest}".encode()).hexdigest()
        now = self._now()
        for _ in range(COMMIT_ATTEMPTS):
            profile = self.players.authenticate(envelope, store)
            receipts = self._domain(profile)[RECEIPTS]
            receipt = LEDGER.receipt(receipts, key)
            if receipt is not None:
                return self._replay(profile, receipt)
            if LEDGER.processed(receipts, key):
                raise ProtocolError("tower_request_stale", "This tower claim was already processed", 409)
            state, changed = self.settle(profile.state, now)
            state, grants = claim_stock(state, self.catalog, missions, self.held(state))
            if grants:
                state = self.possessions.credit(state, grants)
            towers = tuple(map(self.catalog.tower, missions))
            # A refill the claim settled first changes the challenge item too, so POSSESSION reports it.
            items = tuple(dict.fromkeys((*(tower.crystal_item_id for tower in towers), *changed)))
            response = self._response(envelope, (self.bootstrap._system(profile),
                                                 self.possessions.response(state, items, include_enabled=True),
                                                 _stack_crystal(read_state(state, self.catalog), towers)))
            if state is not profile.state:
                state = self._receipted(state, profile, key, response)
            if self._stored(store, profile, state):
                return response
        raise _changed()

    def _receipted(self, state: dict, profile, key: str, response: bytes) -> dict:
        if len(response) > RECEIPT_RESPONSE_LIMIT:
            raise ProtocolError("tower_response_capacity", "Tower claim response exceeds its receipt limit", 503)
        domain = read_state(state, self.catalog, revision=profile.revision)
        LEDGER.record(domain, domain[RECEIPTS], {"request": key, "content": self.content_version,
                                                 "revision": profile.revision + 1, "response": response.decode("utf-8")})
        return write_state(state, self.catalog, domain)

    def _replay(self, profile, receipt: dict) -> bytes:
        if receipt["revision"] != profile.revision or receipt["content"] not in self.replayable_versions:
            raise ProtocolError("tower_retry_stale", "Tower state or content changed after this claim", 409)
        return self._receipt_response(receipt)

    def _receipt_response(self, receipt: dict) -> bytes:
        response = receipt["response"].encode("utf-8")
        try:
            loads(response)
            encode_response(self._claim_profile, response)
        except (AlXmlError, ProtocolError) as error:
            raise ProtocolError("tower_state_invalid", "Tower claim receipts hold invalid responses", 503) from error
        return response

    def initialize(self, state: dict) -> dict:
        # Startup has no stored revision to bound receipts, so the store's ceiling stands in for it.
        for receipt in read_state(state, self.catalog, revision=MAX_REVISION)[RECEIPTS]["receipts"]:
            self._receipt_response(receipt)
        return state

    def status(self) -> dict:
        return {"ready": True, "implemented_operations": list(OPERATIONS), "content_version": self.content_version,
                "legacy_content_version": self.legacy_content_version,
                "replayable_content_versions": sorted(self.replayable_versions), "catalog": self.catalog.status(),
                "rules": self.rules.to_dict(),
                "rules_version": self.rules.content_version, "local_rules": dict(LOCAL_RULES),
                "item_tickets_attached": self.presents.item_tickets is not None, "history_limit": HISTORY_LIMIT,
                "processed_request_limit": PROCESSED_LIMIT, "request_order": REQUEST_ORDER, "native_tested": False}

    def describe(self, state: dict, now: datetime) -> dict:
        return {**describe_state(state, self.catalog, self.rules, instant(now), self.deliverable),
                "receipt_content_version": self.content_version}

    def set_floor_best(self, state: dict, quest_id: int, score: int) -> dict:
        tower = self.catalog.floor_tower(quest_id)
        if type(score) is not int or not 0 <= score <= self.catalog.scoring(quest_id).ceiling:
            raise _admin_invalid("Floor bests must be scores the floor can record")
        domain = read_state(state, self.catalog)
        record, key = _record(domain, tower), str(quest_id)
        # A best set by hand has no run behind it, so the team saved for the replaced best no longer belongs to it.
        if record["floors"].get(key, 0) != score:
            record["teams"].pop(key, None)
        if score:
            record["floors"][key] = score
        else:
            record["floors"].pop(key, None)
        record["total"] = sum(record["floors"].values())
        if record["stack"] > tower.stack_bound(record["total"]):
            raise _admin_invalid("Lower the crystal stock before raising the tower total past its generation limit")
        # A corrected best never pays the tiers it reaches, so they count as claimed instead of due.
        waiting = {(entry["source"], tuple(entry["key"])) for entry in record["deferred"]}
        claimed = set(record["score_rewards"].get(key, ()))
        for tier in tower.score_rewards.get(quest_id, ()):
            if tier.threshold <= score and tier.key[1] not in claimed and (tier.source, tier.key) not in waiting:
                record["score_rewards"].setdefault(key, []).append(tier.key[1])
        return write_state(state, self.catalog, domain)

    def adjust_stack(self, state: dict, mission_id: int, stack: int) -> dict:
        tower = self.catalog.tower(mission_id)
        domain = read_state(state, self.catalog)
        record = _record(domain, tower)
        if type(stack) is not int or not 0 <= stack <= tower.generation_tier(record["total"]).limit:
            raise _admin_invalid("Crystal stock must stay within the current generation limit")
        record["stack"] = stack
        return write_state(state, self.catalog, domain)

    def settle_now(self, state: dict, now: datetime) -> tuple[dict, tuple[str, ...], bool]:
        now = instant(now)
        state, caught_up = self.settle(state, now)
        records = saved_records(state)
        opened = tuple(tower for tower in self.catalog.towers if self.catalog.accessible(tower, records, now))
        if not opened:
            return state, caught_up, False
        domain = read_state(state, self.catalog)
        for tower in opened:
            domain["towers"][str(tower.mission_id)]["generation_day"] -= FORCED_DAYS
        for item_id in {tower.consume_item_id for tower in opened if tower.daily_add}:
            domain["consume"][item_id]["day"] -= FORCED_DAYS
        state, forced_ids = self.settle(write_state(state, self.catalog, domain), now)
        return state, tuple(dict.fromkeys((*caught_up, *forced_ids))), True

    def redeliver_deferred(self, state: dict, mission_id: int) -> tuple[dict, tuple[int, ...], tuple[dict, ...]]:
        tower = self.catalog.tower(mission_id)
        _record(read_state(state, self.catalog), tower)
        state, presents, deferred = rewards_due(state, self.catalog, tower.mission_id, saved_records(state),
                                                self.deliverable)
        if not presents:
            return state, (), deferred
        state, rows = self.presents.enqueue(state, list(presents))
        return state, tuple(row["PresentID"] for row in rows), deferred

    def reset_rewards(self, state: dict, mission_id: int, sources=SOURCES) -> tuple[dict, tuple[dict, ...]]:
        tower = self.catalog.tower(mission_id)
        if (not isinstance(sources, (list, tuple)) or not sources or len(set(sources)) != len(sources)
                or not set(sources) <= set(SOURCES)):
            raise _admin_invalid("Reward resets name the score tiers, the star tiers or both")
        domain = read_state(state, self.catalog)
        record = _record(domain, tower)
        reset = []
        if SCORE_SOURCE in sources:
            reset.extend({"source": SCORE_SOURCE, "key": [int(floor), reward_id]}
                         for floor, reward_ids in record["score_rewards"].items() for reward_id in reward_ids)
            record["score_rewards"] = {}
        if STAR_SOURCE in sources:
            reset.extend({"source": STAR_SOURCE, "key": [required]} for required in record["star_rewards"])
            record["star_rewards"] = []
        return write_state(state, self.catalog, domain), tuple(reset)

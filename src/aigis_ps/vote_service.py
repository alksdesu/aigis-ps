from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from .alxml import Column, Container, Limits, dumps
from .business_calendar import NATIVE_DATETIME
from .energy_clock import JST, instant
from .errors import ProtocolError
from .protocol import encode_response
from .request_fields import registered
from .vote_catalog import CANDIDATES_UNRESOLVED
from .vote_expiry import ASSAULT_KEY, ASSAULT_NOTIFICATION_KEY, NOTIFICATION_KEY
from .vote_state import initialize_vote_state, read_vote_history, vote_history_response


VEC_FIELDS = (("RequiredRank", "I"), ("DateOpen", "S"), ("DateClose", "S"),
    ("DateOpenResult", "S"), ("DateCloseResult", "S"), ("DateOpenAnnouncement", "S"),
    ("DateCloseAnnouncement", "S"), ("VoteLimitCount", "I"), ("EarnedVotes", "I"))
VGC_FIELDS = (("GroupID", "I"), ("ItemID", "S"), ("DateOpenDispersion", "S"),
    ("DateCloseDispersion", "S"), ("DateOpenVote", "S"), ("DateCloseVote", "S"), ("Final", "I"))
VCC_FIELDS = (("CardID", "I"), ("GroupID", "I"), ("Count", "I"))
VRC_FIELDS = tuple((name, "S" if name == "ItemID" else "I") for name in
    ("GroupID", "Count", "Repeat", "ItemID", "GiftType", "Amount", "SummonID", "CardID",
     "UnitLevel", "SkillLevel", "ClassChange", "Likable", "Cost"))
REPLY_LIMITS = Limits(max_bytes=2 * 1024 * 1024, max_values=100000, max_elements=110000)
TRANSACTION_DOMAINS = ("vote_rewards", "vote_tickets", "vote_transactions", "vote_last_action",
                       "vote_finalists", "vote_expiry", "vote_cooking", NOTIFICATION_KEY,
                       ASSAULT_KEY, ASSAULT_NOTIFICATION_KEY)


def _table(name, fields, rows):
    return Container(name, tuple(Column(key, kind, tuple(row[key] for row in rows)) for key, kind in fields))


def _date(value):
    return value.astimezone(JST).strftime(NATIVE_DATETIME)


class VoteService:
    def __init__(self, bootstrap, players, catalog, *, possessions=None, presents=None, cooking=None):
        self.bootstrap, self.players, self.catalog = bootstrap, players, catalog
        self.transactions = None
        self.content_version = self._content()
        self._ready()
        if (possessions is None) != (presents is None):
            raise ValueError("Vote transactions require both inventory and mail services")
        if presents is not None:
            from .vote_expiry import VoteExpiryService
            from .vote_finalists import VoteFinalistService
            from .vote_rewards import VoteRewardService
            from .vote_tickets import VoteTicketService
            from .vote_transactions import VoteTransactions
            if presents.players is not players or presents.possessions is not possessions:
                raise ValueError("Vote transactions must share the player's asset services")
            finalists = VoteFinalistService(catalog)
            self.transactions = VoteTransactions(self, possessions,
                VoteRewardService(catalog, presents, finalists=finalists), VoteTicketService(possessions, catalog),
                finalists, VoteExpiryService(catalog, presents, possessions), cooking=cooking)

    def _content(self):
        return hashlib.sha256(json.dumps({"schema": 1, "catalog": self.catalog.content_version,
            "ranking": "local_player_only", "finalists": "unavailable", "operations": [55],
            "visibility": "announcement_window", "assault": "no_synthetic_entitlement"},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _ready(self):
        self.catalog._ready()
        if self._content() != self.content_version:
            raise ProtocolError("vote_dependencies_changed", "Vote sources changed after setup", 503)

    def initialize(self, state):
        self._ready()
        updated = initialize_vote_state(state)
        if self.transactions is not None:
            return self.transactions.initialize(updated)
        if any(key in state for key in TRANSACTION_DOMAINS):
            raise ProtocolError("vote_transactions_unavailable", "Existing vote operations require a transaction service", 503)
        return updated

    def handlers(self):
        return {55: self.read, **({57: self.transactions.submit, 137: self.transactions.startup}
                                if self.transactions is not None else {})}

    def response(self, state):
        try:
            self._ready()
        except ProtocolError as error:
            # PVH is the saved history, so 137 serves it instead of keeping the native startup loop resending.
            self.bootstrap.startup_diagnostics.degrade(error)
        return vote_history_response(state)

    def status(self):
        try:
            self._ready()
            if self.transactions is not None:
                self.transactions._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
            "catalog": self.catalog.status(), "implemented_operations": sorted(self.handlers()), "native_tested": False,
            "transactions": self.transactions is not None,
            "assault_expiry": None if self.transactions is None else self.transactions.assault_expiry.status(),
            "ranking_policy": "Local player's persisted votes; no official or multi-player totals",
            "unavailable": ([] if self.transactions is not None else ["57 vote debit and rewards", "Daily ticket refresh"])
                + (["Finalist selection, frozen rankings, expired ticket and challenge right conversion"]
                   if self.transactions is None else [])
                + ([] if self.transactions is not None and self.transactions.cooking is not None else ["Cooking gifts"])
                + ["Assault battles"]}

    def _reward_rows(self):
        rows = {}
        for reward in self.catalog.rewards:
            row = {"GroupID": reward.group_id, "Count": reward.threshold,
                "Repeat": reward.repeat_every, "ItemID": reward.item_id, "GiftType": 0,
                "Amount": reward.amount, "SummonID": 0, "CardID": reward.card_id,
                "UnitLevel": reward.unit_level, "SkillLevel": reward.skill_level,
                "ClassChange": reward.class_change, "Likable": reward.likable, "Cost": reward.cost}
            if self.transactions is not None:
                row = self.transactions.rewards.reward_row(reward)
                row["Repeat"] = reward.repeat_every
            key = reward.group_id, reward.threshold
            if key in rows:
                previous = rows[key]
                if (previous["Repeat"] or not reward.repeat_every
                        or {name: value for name, value in previous.items() if name != "Repeat"}
                        != {name: value for name, value in row.items() if name != "Repeat"}):
                    raise ProtocolError("vote_reward_definition_invalid", "Repeated vote reward must extend its initial reward", 503)
            rows[key] = row
        return tuple(rows.values())

    def _configuration(self, state, now):
        rows = read_vote_history(state)
        event, rules = self.catalog.event, self.catalog.rules
        visible = rules.enabled and event.announcement_start <= now < event.announcement_end
        if not visible:
            return tuple(_table(name, fields, ()) for name, fields in
                (("VEC", VEC_FIELDS), ("VGC", VGC_FIELDS), ("VCC", VCC_FIELDS), ("VRC", VRC_FIELDS)))
        if self.catalog.unresolved_candidates:
            raise ProtocolError(CANDIDATES_UNRESOLVED, "Vote candidate identities require explicit resolution", 503)
        final = next(group for group in self.catalog.groups if group.final)
        if now >= final.vote_start and self.transactions is None:
            raise ProtocolError("vote_finalists_unavailable", "Finalist selection and frozen rankings are not implemented", 501)
        counts = {}
        available = self.catalog.candidates
        if self.transactions is not None:
            available = (*available, *self.transactions.finalists.candidates(state, now))
            for group in self.catalog.groups:
                counts.update(((group.group_id, row["CardID"]), row["Count"])
                              for row in self.transactions.finalists.ranking(state, group))
        else:
            for row in rows:
                group = self.catalog.groups_by_id.get(row["groupId"])
                if group is None or group.item_id != row["itemId"]:
                    continue
                candidate = self.catalog.candidates_by_id.get(row["cardId"])
                if candidate is None or candidate.group_id != group.group_id:
                    raise ProtocolError("vote_candidate_history_invalid", "Stored votes conflict with the selected candidates", 503)
                counts[(group.group_id, row["cardId"])] = row["count"]
        event_rows = [{"RequiredRank": rules.required_rank, "DateOpen": _date(event.start_at),
            "DateClose": _date(event.result_end), "DateOpenResult": _date(event.result_start),
            "DateCloseResult": _date(event.result_end), "DateOpenAnnouncement": _date(event.end_at),
            "DateCloseAnnouncement": _date(event.result_start), "VoteLimitCount": rules.vote_limit_count,
            "EarnedVotes": rules.earned_votes}]
        groups = [{"GroupID": group.group_id, "ItemID": group.item_id,
            "DateOpenDispersion": _date(group.dispersion_start), "DateCloseDispersion": _date(group.dispersion_end),
            "DateOpenVote": _date(group.vote_start), "DateCloseVote": _date(group.vote_end), "Final": int(group.final)}
            for group in self.catalog.groups]
        candidates = [{"CardID": candidate.card_id, "GroupID": candidate.group_id,
            "Count": counts.get((candidate.group_id, candidate.card_id), 0)} for candidate in available]
        return (_table("VEC", VEC_FIELDS, event_rows), _table("VGC", VGC_FIELDS, groups),
                _table("VCC", VCC_FIELDS, candidates), _table("VRC", VRC_FIELDS, self._reward_rows()))

    def read(self, envelope, store):
        registered(envelope, {55}, ())
        now = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            if now is None:
                now = instant(self.bootstrap.clock())
            updated = profile.state
            if self.transactions is not None:
                self.transactions._ready()
                updated = self.transactions.observe_rankings(updated, now)
            candidate = replace(profile, state=updated)
            response = self._read_response(candidate, now)
            encode_response(envelope.profile, response)
            if updated == profile.state:
                return response
            self.transactions._preflight(profile, updated)
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while freezing vote rankings", 409)

    def _read_response(self, profile, now):
        configuration = self._configuration(profile.state, now)
        extra = ()
        if self.transactions is not None:
            self.transactions._ready()
            self.transactions.rewards.initialize(profile.state)
            groups = [group for group in self.catalog.groups if group.vote_start <= now < group.vote_end]
            group = max(groups, key=lambda value: value.vote_start) if groups else None
            extra = (self.transactions.rewards.assault_response(profile.state, group),)
        response = dumps(Container(children=(self.bootstrap._system(profile), *configuration, *extra)), limits=REPLY_LIMITS)
        return response

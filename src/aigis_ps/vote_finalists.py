from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import re

from .energy_clock import instant
from .errors import ProtocolError
from .native_numbers import int32
from .vote_catalog import CANDIDATES_UNRESOLVED
from .vote_state import read_vote_history, vote_key


TOP_COUNT = 10
MAX_CANDIDATES = 4096
REFREEZE_REFUSED = "vote_finalists_refreeze_refused"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_FIELDS = frozenset(("event_id", "group_id", "final", "definition", "source_sha256",
    "window_end", "frozen_at", "origin", "counts", "qualified_ids"))


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _invalid(message="Stored vote rankings are invalid"):
    raise ProtocolError("vote_finalists_state_invalid", message, 503)


def _timestamp(value):
    try:
        return instant(value).astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (OverflowError, ValueError, TypeError) as error:
        raise ProtocolError("vote_finalists_clock_invalid", "Vote ranking time is invalid") from error


def _saved_time(value):
    try:
        if type(value) is not str or len(value) != 32:
            raise ValueError("Invalid timestamp")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or _timestamp(parsed) != value:
            raise ValueError("Noncanonical timestamp")
        return parsed
    except (ValueError, TypeError, ProtocolError):
        _invalid("Frozen ranking timestamps must be canonical UTC instants")


def _qualified(rows):
    cutoff = rows[min(TOP_COUNT, len(rows)) - 1]["Count"]
    return [row["CardID"] for row in rows if row["Count"] >= cutoff]


class VoteFinalistService:
    def __init__(self, catalog):
        self.catalog = catalog
        catalog._ready()
        self._groups = tuple(catalog.groups)
        self._candidates = tuple(catalog.candidates)
        if (len(self._groups) != 4 or tuple(group.group_id for group in self._groups) != (1, 2, 3, 4)
                or tuple(group.final for group in self._groups) != (False, False, False, True)
                or len({group.item_id for group in self._groups}) != 4
                or not _hash(catalog.source_content_version)
                or not 0 < len(self._candidates) <= MAX_CANDIDATES
                or len({candidate.card_id for candidate in self._candidates}) != len(self._candidates)
                or any(candidate.group_id not in (1, 2, 3) or not _integer(candidate.card_id, 1)
                       for candidate in self._candidates)):
            raise ProtocolError("vote_finalists_catalog_invalid", "Vote rankings require four verified groups", 503)
        self._source = catalog.source_content_version
        self._definition = self._content()
        self.content_version = self._definition

    def _content(self):
        return _digest({"schema": 1, "source": self.catalog.source_content_version,
            "event": self.catalog.event.event_id,
            "groups": [(group.group_id, group.item_id, group.final) for group in self.catalog.groups],
            "candidates": sorted((candidate.group_id, candidate.card_id) for candidate in self.catalog.candidates),
            "ranking": "local_player_counts_desc_card_id_asc", "qualification": "top_ten_with_all_ties_including_zero"})

    def _ready(self):
        self.catalog._ready()
        if self._content() != self._definition:
            raise ProtocolError("vote_finalists_dependencies_changed", "Vote ranking sources changed after setup", 503)

    def _group(self, group):
        selected = self.catalog.groups_by_id.get(group if type(group) is int else getattr(group, "group_id", None))
        if selected is None or type(group) is not int and selected != group:
            raise ProtocolError("vote_finalists_group_invalid", "Vote rankings require a catalog group")
        return selected

    def validated(self, state, *, current=False):
        self._ready()
        read_vote_history(state)
        if "vote_finalists" not in state:
            return {}
        ledger = state["vote_finalists"]
        if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "groups"}
                or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
                or not isinstance(ledger["groups"], dict) or not 0 < len(ledger["groups"]) <= 4):
            _invalid()
        groups = ledger["groups"]
        identities, definitions, sources, events = set(), set(), set(), set()
        by_group = {}
        for item, saved in groups.items():
            vote_key(item, 1)
            if (not isinstance(saved, dict) or set(saved) != _FIELDS
                    or not _integer(saved["event_id"], 1) or type(saved["group_id"]) is not int
                    or saved["group_id"] not in (1, 2, 3, 4) or saved["group_id"] in identities
                    or type(saved["final"]) is not bool or saved["final"] != (saved["group_id"] == 4)
                    or not _hash(saved["definition"]) or not _hash(saved["source_sha256"])
                    or saved["origin"] != "first_observation_baseline"
                    or not isinstance(saved["counts"], list) or not 0 < len(saved["counts"]) <= MAX_CANDIDATES
                    or not isinstance(saved["qualified_ids"], list)):
                _invalid()
            identities.add(saved["group_id"])
            definitions.add(saved["definition"])
            sources.add(saved["source_sha256"])
            events.add(saved["event_id"])
            by_group[saved["group_id"]] = saved
            if _saved_time(saved["frozen_at"]) < _saved_time(saved["window_end"]):
                _invalid("Vote rankings cannot freeze before their recorded closing time")
            rows = saved["counts"]
            if (any(not isinstance(row, dict) or set(row) != {"CardID", "Count"}
                    or not _integer(row["CardID"], 1) or not _integer(row["Count"]) for row in rows)
                    or len({row["CardID"] for row in rows}) != len(rows)
                    or rows != sorted(rows, key=lambda row: (-row["Count"], row["CardID"]))
                    or any(not _integer(identifier, 1) for identifier in saved["qualified_ids"])
                    or saved["qualified_ids"] != ([] if saved["final"] else _qualified(rows))):
                _invalid("Vote ranking counts, order or tie qualification are inconsistent")
        if len(definitions) != 1 or len(sources) != 1 or len(events) != 1:
            _invalid("Frozen vote groups must share one source definition")
        if 4 in by_group:
            if not {1, 2, 3} <= by_group.keys():
                _invalid("Final results require all preliminary group snapshots")
            expected = [identifier for group in (1, 2, 3) for identifier in by_group[group]["qualified_ids"]]
            if (len(set(expected)) != len(expected)
                    or {row["CardID"] for row in by_group[4]["counts"]} != set(expected)
                    or any(_saved_time(by_group[4]["frozen_at"]) < _saved_time(by_group[group]["frozen_at"])
                           for group in (1, 2, 3))):
                _invalid("Final rankings differ from their frozen qualifying groups")
        if current:
            if (definitions != {self._definition} or sources != {self._source}
                    or events != {self.catalog.event.event_id}):
                raise ProtocolError("vote_finalists_definition_changed", "Frozen vote rankings require their original source definition", 503)
            for item, saved in groups.items():
                group = self.catalog.groups_by_id[saved["group_id"]]
                expected = (self._qualified_ids(groups) if group.final else
                            {candidate.card_id for candidate in self._candidates if candidate.group_id == group.group_id})
                if item != group.item_id or {row["CardID"] for row in saved["counts"]} != set(expected):
                    _invalid("Frozen vote identities differ from the current catalog")
                if self._live(state, group, expected) != saved["counts"]:
                    raise ProtocolError("vote_finalists_history_changed", "Persisted votes differ from their closed ranking snapshot", 503)
        return groups

    def initialize(self, state):
        self.validated(state)
        return deepcopy(state)

    def validate_qualification(self, state, card_id):
        groups = self.validated(state, current=False)
        preliminary = {saved["group_id"]: saved for saved in groups.values() if not saved["final"]}
        if set(preliminary) != {1, 2, 3}:
            raise ProtocolError("vote_finalists_qualification_missing", "Final vote receipts require all preliminary snapshots", 503)
        if not _integer(card_id, 1) or not any(card_id in saved["qualified_ids"] for saved in preliminary.values()):
            raise ProtocolError("vote_finalists_qualification_invalid", "Final vote receipt candidate was not qualified", 503)

    def _qualified_ids(self, groups):
        preliminary = self._groups[:3]
        if any(group.item_id not in groups for group in preliminary):
            return ()
        return tuple(identifier for group in preliminary for identifier in groups[group.item_id]["qualified_ids"])

    def _live(self, state, group, card_ids):
        counts = {identifier: 0 for identifier in card_ids}
        for row in read_vote_history(state):
            if row["itemId"] == group.item_id:
                if row["groupId"] != group.group_id or row["cardId"] not in counts:
                    raise ProtocolError("vote_finalists_history_invalid", "Vote history contains an ineligible candidate", 503)
                counts[row["cardId"]] = row["count"]
        return sorted(({"CardID": identifier, "Count": count} for identifier, count in counts.items()),
                      key=lambda row: (-row["Count"], row["CardID"]))

    def settle(self, state, now):
        groups = self.validated(state, current=True)
        current = instant(now)
        if groups and any(current < _saved_time(saved["frozen_at"]) for saved in groups.values()):
            raise ProtocolError("vote_finalists_clock_reversed", "Vote ranking clock moved behind a saved snapshot", 409)
        updated, changed = deepcopy(state), []
        if not self.catalog.rules.enabled:
            return updated, ()
        pending = [group for group in self._groups if group.item_id not in groups and current >= group.vote_end]
        if not pending:
            return updated, ()
        if self.catalog.unresolved_candidates:
            raise ProtocolError(CANDIDATES_UNRESOLVED, "Vote candidate identities require explicit resolution", 503)
        journal = updated.setdefault("vote_finalists", {"schema_version": 1, "groups": {}})["groups"]
        for group in pending:
            card_ids = (self._qualified_ids(journal) if group.final else
                        tuple(candidate.card_id for candidate in self._candidates if candidate.group_id == group.group_id))
            if not card_ids:
                raise ProtocolError("vote_finalists_unavailable", "Final rankings require completed preliminary groups", 409)
            rows = self._live(updated, group, card_ids)
            journal[group.item_id] = {"event_id": self.catalog.event.event_id, "group_id": group.group_id,
                "final": group.final, "definition": self._definition, "source_sha256": self._source,
                "window_end": _timestamp(group.vote_end), "frozen_at": _timestamp(current),
                "origin": "first_observation_baseline", "counts": rows,
                "qualified_ids": [] if group.final else _qualified(rows)}
            changed.append(group.item_id)
        self.validated(updated, current=True)
        return updated, tuple(changed)

    def adopt_current(self, state):
        groups = self.validated(state)
        source = {"definition": self._definition, "source_sha256": self._source, "event_id": self.catalog.event.event_id}
        updated = deepcopy(state)
        if all({name: saved[name] for name in source} == source for saved in groups.values()):
            return updated, ()
        for saved in updated["vote_finalists"]["groups"].values():
            saved.update(source)
        try:
            self.validated(updated, current=True)
        except ProtocolError as error:
            raise ProtocolError(REFREEZE_REFUSED, "Frozen rankings no longer match the current candidates or votes",
                                409) from error
        return updated, tuple(sorted(groups))

    def candidates(self, state, now):
        groups = self.validated(state, current=True)
        if instant(now) < self._groups[-1].vote_start:
            return ()
        qualified = set(self._qualified_ids(groups))
        return tuple(replace(candidate, group_id=4) for candidate in self._candidates if candidate.card_id in qualified)

    def is_candidate(self, state, group, card_id):
        group = self._group(group)
        groups = self.validated(state, current=True)
        if not _integer(card_id, 1):
            return False
        if group.final:
            return card_id in self._qualified_ids(groups)
        return any(candidate.card_id == card_id and candidate.group_id == group.group_id for candidate in self._candidates)

    def is_frozen(self, state, group):
        group = self._group(group)
        return group.item_id in self.validated(state, current=True)

    def require_open(self, state, group):
        if self.is_frozen(state, group):
            raise ProtocolError("vote_group_frozen", "Closed vote groups cannot accept additional votes", 409)

    def ranking(self, state, group):
        group = self._group(group)
        groups = self.validated(state, current=True)
        if group.item_id in groups:
            return tuple(deepcopy(groups[group.item_id]["counts"]))
        card_ids = (self._qualified_ids(groups) if group.final else
                    tuple(candidate.card_id for candidate in self._candidates if candidate.group_id == group.group_id))
        return tuple(self._live(state, group, card_ids))

    def status(self):
        self._ready()
        return {"ready": True, "content_version": self.content_version,
                "policy": "Local player votes; top ten with all ties including zero; immutable closing snapshots"}

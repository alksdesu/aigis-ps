from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re

from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .native_numbers import INT32_MIN, int32
from .possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from .system_literals import command_id


MAX_VOTE_ROWS = 65535
FIELDS = (("key", "I"), ("cardId", "I"), ("groupId", "I"), ("count", "I"), ("itemId", "S"))
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _invalid(message="Stored vote history is invalid"):
    raise ProtocolError("vote_state_invalid", message, 503)


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _item(value):
    try:
        if type(value) is not str or not value or len(value.encode("utf-8")) > STORED_ITEM_NAME_BYTES:
            raise ValueError("Invalid vote item identity")
        dumps(Container(children=(Column("itemId", "S", (value,)),)))
    except (TypeError, ValueError, UnicodeError):
        _invalid("Vote item identities require bounded native text")


def vote_key(item_id, card_id):
    _item(item_id)
    if not _integer(card_id, 1):
        _invalid("Vote cards require positive int32 identities")
    return command_id(f"{item_id}:{card_id}")


def _native_rows(state):
    if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
            or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
        _invalid("Vote history requires verified bootstrap storage")
    rows = state["tables"].get("vote_history")
    if not isinstance(rows, list) or len(rows) > MAX_VOTE_ROWS:
        _invalid("Vote history requires a bounded native row collection")
    keys, identities, groups = set(), set(), {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {name for name, _ in FIELDS}
                or not _integer(row["key"], INT32_MIN)
                or not _integer(row["cardId"], 1) or not _integer(row["groupId"], 1)
                or not _integer(row["count"])):
            _invalid("Vote rows require all five native fields")
        item, card, group = row["itemId"], row["cardId"], row["groupId"]
        key = vote_key(item, card)
        if row["key"] != key or key in keys or (item, card) in identities:
            _invalid("Vote identities are duplicated, collide or have an invalid checksum")
        if item in groups and groups[item] != group:
            _invalid("A vote item cannot belong to multiple stored groups")
        keys.add(key)
        identities.add((item, card))
        groups[item] = group
    return rows


def _validated(state):
    rows = _native_rows(state)
    if "vote_state" not in state:
        return rows, None
    journal = state["vote_state"]
    if (not isinstance(journal, dict) or set(journal) != {"schema_version", "entries"}
            or type(journal["schema_version"]) is not int or journal["schema_version"] != 1
            or not isinstance(journal["entries"], dict)
            or set(journal["entries"]) != {str(row["key"]) for row in rows}):
        _invalid("Vote journal does not match native identities")
    for row in rows:
        entry = journal["entries"][str(row["key"])]
        if (not isinstance(entry, dict) or set(entry) != {"definition", "baseline_count", "count"}
                or not _integer(entry["baseline_count"]) or not _integer(entry["count"])
                or not entry["baseline_count"] <= entry["count"] == row["count"]):
            _invalid("Vote journal counts disagree with native totals")
        definition = entry["definition"]
        if definition is None:
            if entry["count"] != entry["baseline_count"]:
                _invalid("An unbound vote baseline cannot contain new votes")
        elif type(definition) is not str or _HASH.fullmatch(definition) is None:
            _invalid("Vote definitions require semantic fingerprints")
    return rows, journal


def read_vote_history(state):
    return deepcopy(_validated(state)[0])


def vote_history_response(state):
    rows = read_vote_history(state)
    return Container("PVH", tuple(Column(name, kind, tuple(row[name] for row in rows)) for name, kind in FIELDS))


def initialize_vote_state(state):
    rows, journal = _validated(state)
    updated = deepcopy(state)
    if rows and journal is None:
        updated["vote_state"] = {"schema_version": 1, "entries": {
            str(row["key"]): {"definition": None, "baseline_count": row["count"], "count": row["count"]}
            for row in rows}}
    return updated


def record_vote(state, *, item_id, group_id, card_id, count, definition):
    # count is the candidate's total after this vote, not the increment.
    if (not _integer(group_id, 1) or not _integer(count)
            or type(definition) is not str or _HASH.fullmatch(definition) is None):
        raise ProtocolError("vote_update_invalid", "Vote updates require a group, absolute count and definition")
    try:
        key = vote_key(item_id, card_id)
    except ProtocolError as error:
        raise ProtocolError("vote_update_invalid", "Vote updates require valid item and card identities") from error
    updated = initialize_vote_state(state)
    rows = updated["tables"]["vote_history"]
    row = next((row for row in rows if row["key"] == key), None)
    if row is not None and (row["itemId"] != item_id or row["cardId"] != card_id):
        raise ProtocolError("vote_identity_collision", "Vote identity collides with an existing native checksum", 409)
    if any(row["itemId"] == item_id and row["groupId"] != group_id for row in rows):
        raise ProtocolError("vote_group_changed", "An existing vote item cannot change groups", 409)
    previous = 0 if row is None else row["count"]
    if count < previous:
        raise ProtocolError("vote_count_decreased", "Vote totals cannot decrease", 409)
    entry = None if row is None else updated["vote_state"]["entries"][str(key)]
    if entry is not None and entry["definition"] not in (None, definition):
        raise ProtocolError("vote_definition_changed", "Vote definition changed while history exists", 409)
    if count == previous:
        return updated
    if row is None:
        if len(rows) >= MAX_VOTE_ROWS:
            raise ProtocolError("vote_history_capacity", "Vote history has reached its supported capacity", 409)
        if len(item_id.encode("utf-8")) > MAX_ITEM_NAME_BYTES:
            raise ProtocolError("vote_item_unreplayable", "New vote rows need item identities that fit the native cell", 503)
        row = {"key": key, "cardId": card_id, "groupId": group_id, "count": 0, "itemId": item_id}
        rows.append(row)
        journal = updated.setdefault("vote_state", {"schema_version": 1, "entries": {}})
        entry = {"definition": None, "baseline_count": 0, "count": 0}
        journal["entries"][str(key)] = entry
    row["count"] = count
    entry.update(definition=definition, count=count)
    _validated(updated)
    return updated


def adopt_current(state, definitions):
    if not isinstance(definitions, Mapping) or any(
            type(definition) is not str or _HASH.fullmatch(definition) is None for definition in definitions.values()):
        raise ProtocolError("vote_update_invalid", "Vote adoption requires a current definition for each item")
    try:
        for item_id in definitions:
            _item(item_id)
    except ProtocolError as error:
        raise ProtocolError("vote_update_invalid", "Vote adoption requires valid item identities") from error
    rows, journal = _validated(state)
    updated, adopted = deepcopy(state), []
    if journal is None:
        return updated, adopted
    entries = updated["vote_state"]["entries"]
    for row in rows:
        entry, current = entries[str(row["key"])], definitions.get(row["itemId"])
        if current is None or entry["definition"] in (None, current):
            continue
        adopted.append({"item_id": row["itemId"], "card_id": row["cardId"], "count": row["count"],
                        "definition": {"before": entry["definition"], "after": current}})
        entry["definition"] = current
    _validated(updated)
    return updated, adopted

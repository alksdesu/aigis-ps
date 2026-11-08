from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
import json

from .alxml import AlXmlError, Column, Container, Limits, ValueType, dumps
from .errors import ProtocolError
from .masterdata import MasterSnapshot
from .native_numbers import int32
from .protocol import ProtocolRegistry, encode_response
from .public_tables import validate_public_master


CARD_STATUS_FIELDS = ("CardID", "A1", "A3", "A5", "A7", "B0", "B1", "B2", "B3", "B4")
MAX_CARD_STATUS_ROWS = 65535
CARD_STATUS_LIMITS = Limits(max_values=MAX_CARD_STATUS_ROWS * len(CARD_STATUS_FIELDS),
                            max_elements=MAX_CARD_STATUS_ROWS * len(CARD_STATUS_FIELDS) + 11)
# CardStatus.A7 episodes: gifts set the first at LoveEv1, the second at Love 100; 9B7060 opens the third once a harlem clear sets it.
FIRST_EPISODE, SECOND_EPISODE, THIRD_EPISODE = 1, 2, 4


class CardStatusService:
    def __init__(self, snapshot: MasterSnapshot):
        if not isinstance(snapshot, MasterSnapshot):
            raise ValueError("Card status requires one fixed public master snapshot")
        schemas = json.loads(files("aigis_ps").joinpath("data/master-schemas.json").read_text("utf-8"))
        fields = {name: [field["name"] for field in table["fields"]] for name, table in schemas["tables"].items()}
        try:
            document, _ = validate_public_master("CardData", snapshot.get_xml("CardData"), fields)
            self._card_ids = frozenset(next(column.values for column in document.children if column.name == "CardID"))
        except (ValueError, KeyError, StopIteration) as error:
            raise ProtocolError("card_status_master_invalid", "Card status requires verified CardData identifiers", 503) from error
        self.snapshot = snapshot
        self._response_profile = ProtocolRegistry.load().by_registration[51]

    def read(self, state: dict) -> list[dict[str, int]]:
        if not isinstance(state, dict) or not isinstance(state.get("tables"), dict):
            raise ProtocolError("card_status_state_invalid", "Card status tables are missing", 503)
        rows = state["tables"].get("card_status")
        if not isinstance(rows, list) or len(rows) > MAX_CARD_STATUS_ROWS:
            raise ProtocolError("card_status_state_invalid", "Card status rows are missing or exceed capacity", 503)
        if "card_status_schema" in state:
            version = state["card_status_schema"]
            if type(version) is not int or version != 1:
                raise ProtocolError("card_status_schema_unavailable", "Card status schema is unsupported", 503)
        elif rows:
            raise ProtocolError("card_status_schema_unavailable", "Nonempty card status requires an explicit schema", 503)
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(CARD_STATUS_FIELDS) or any(not int32(value) for value in row.values()):
                raise ProtocolError("card_status_state_invalid", "Card status requires complete native integer rows", 503)
            identifier = row["CardID"]
            if identifier not in self._card_ids or identifier in seen:
                raise ProtocolError("card_status_state_invalid", "Card status contains unknown or duplicate CardIDs", 503)
            seen.add(identifier)
            result.append({name: row[name] for name in CARD_STATUS_FIELDS})
        return result

    @staticmethod
    def _container(rows: list[dict[str, int]], name: str) -> Container:
        return Container(name, tuple(Column(field, ValueType.INT, tuple(row[field] for row in rows))
                                     for field in CARD_STATUS_FIELDS))

    def response(self, state: dict, card_ids: list[int] | tuple[int, ...] | None = None) -> Container:
        rows = self.read(state)
        if card_ids is not None:
            if not isinstance(card_ids, (list, tuple)) or len(card_ids) > MAX_CARD_STATUS_ROWS:
                raise ProtocolError("card_status_selection_invalid", "Card status selection must be a bounded ID sequence")
            if any(not int32(identifier) for identifier in card_ids) or len(set(card_ids)) != len(card_ids):
                raise ProtocolError("card_status_selection_invalid", "Card status selection requires distinct native integer IDs")
            by_id = {row["CardID"]: row for row in rows}
            if any(identifier not in by_id for identifier in card_ids):
                raise ProtocolError("card_status_not_found", "Card status selection references a missing record", 409)
            rows = [by_id[identifier] for identifier in card_ids]
        return self._container(rows, "CardStatus")

    def flat_response(self, state: dict) -> bytes:
        try:
            response = dumps(self._container(self.read(state), "DA"), limits=CARD_STATUS_LIMITS)
        except AlXmlError as error:
            raise ProtocolError("card_status_capacity_exceeded", "Full card status exceeds the native XML capacity", 409) from error
        try:
            encode_response(self._response_profile, response)
        except ProtocolError as error:
            raise ProtocolError("card_status_capacity_exceeded", "Full card status exceeds the native 51 response capacity", 409) from error
        return response

    def update(self, state: dict, patches: list[dict[str, int]]) -> dict:
        rows = self.read(state)
        if not isinstance(patches, list) or len(patches) > MAX_CARD_STATUS_ROWS:
            raise ProtocolError("card_status_patch_invalid", "Card status patches must be a bounded list")
        by_id = {row["CardID"]: row for row in rows}
        seen = set()
        for patch in patches:
            if not isinstance(patch, dict) or "CardID" not in patch or len(patch) < 2 or set(patch) - set(CARD_STATUS_FIELDS):
                raise ProtocolError("card_status_patch_invalid", "A patch requires CardID and known status fields")
            if any(not int32(value) for value in patch.values()):
                raise ProtocolError("card_status_patch_invalid", "Card status patches require native integers")
            identifier = patch["CardID"]
            if identifier in seen or identifier not in self._card_ids:
                raise ProtocolError("card_status_patch_invalid", "Card status patches contain duplicate or unknown CardIDs")
            seen.add(identifier)
            if identifier not in by_id:
                row = dict.fromkeys(CARD_STATUS_FIELDS, 0)
                row["CardID"] = identifier
                rows.append(row)
                by_id[identifier] = row
            by_id[identifier].update(patch)
        updated = deepcopy(state)
        if patches:
            updated["tables"]["card_status"] = rows
            updated["card_status_schema"] = 1
        self.flat_response(updated)
        return updated

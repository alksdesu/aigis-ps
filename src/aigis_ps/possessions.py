from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
import hashlib
from pathlib import Path

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, ValueType, dumps
from .assets import resolve_asset
from .errors import ProtocolError
from .local_io import read_bounded
from .native_numbers import INT32_MAX, int32
from .protocol import NATIVE_CELL_BYTES
from .system_literals import command_id


MAX_POSSESSION_ITEMS = 4096
MAX_ITEM_NAME_BYTES = NATIVE_CELL_BYTES
# Saves made under the earlier 256-byte bound keep loading; only new rows must fit the native cell.
STORED_ITEM_NAME_BYTES = 256
ITEM_RESOURCE_LIMIT = 8 * 1024 * 1024
INSUFFICIENT = "possession_insufficient"
ITEM_UNKNOWN = "possession_item_unknown"


def _name_bytes(name: str) -> int:
    return len(name.encode("utf-8"))


class PossessionService:
    def __init__(self, asset_root: Path, *, asset_reader: Callable[[str], bytes] | None = None):
        self._definitions: dict[str, int] = {}
        self._names_by_hash: dict[int, str] = {}
        self._unreplayable: frozenset[str] = frozenset()
        self.issues: dict[str, str] = {}
        self.asset_sha256: str | None = None
        try:
            root = Path(asset_root)
            data = (asset_reader("ItemConfigTable.atb") if asset_reader is not None else
                    read_bounded(resolve_asset(root, "ItemConfigTable.atb"), ITEM_RESOURCE_LIMIT, root=root))
            if not isinstance(data, bytes) or len(data) > ITEM_RESOURCE_LIMIT:
                raise ValueError("Item resource must be bounded bytes")
            limits = ParseLimits(max_input_bytes=ITEM_RESOURCE_LIMIT, max_decompressed_bytes=ITEM_RESOURCE_LIMIT)
            if data.startswith(b"ALL4"):
                data = decompress_all4(data, limits)
            table = parse_table(data, limits)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if len(fields) != len(table["fields"]) or any(fields.get(name) != kind for name, kind in (("ItemID", 32), ("ID", 1), ("Stack", 1))):
                raise ValueError("Item identity or stack fields differ from the verified native contract")
            definitions = {}
            identifiers = set()
            names_by_hash = {}
            for row in table["rows"]:
                name, identifier, stack = row["ItemID"], row["ID"], row["Stack"]
                if not isinstance(name, str) or not name or _name_bytes(name) > STORED_ITEM_NAME_BYTES:
                    raise ValueError("Item definitions require bounded nonempty names")
                expected = command_id(name)
                if not int32(identifier) or identifier != expected or name in definitions or identifier in identifiers:
                    raise ValueError("Item definition identities are invalid or duplicated")
                if not int32(stack) or stack < 0:
                    raise ValueError("Item stack limits must be nonnegative int32 values")
                definitions[name] = stack
                identifiers.add(identifier)
                names_by_hash[identifier] = name
            if not definitions:
                raise ValueError("ItemConfigTable must contain item definitions")
            dumps(Container(children=(Column("ItemID", ValueType.STRING, tuple(definitions)),)))
            self._definitions = definitions
            self._names_by_hash = names_by_hash
            self._unreplayable = frozenset(name for name in definitions if _name_bytes(name) > MAX_ITEM_NAME_BYTES)
            self.asset_sha256 = hashlib.sha256(data).hexdigest()
        except (OSError, ValueError) as error:
            self.issues["item_definitions"] = str(error)

    def is_ready(self) -> bool:
        return not self.issues

    def status(self) -> dict:
        return {"ready": self.is_ready(), "unavailable": dict(self.issues), "count": len(self._definitions),
                "creditable_count": sum(stack > 0 for stack in self._definitions.values()),
                "unsupported_stack_count": sum(stack == 0 for stack in self._definitions.values()),
                "local_credit_policy": "New credits require positive resource Stack and cannot exceed it; existing int32 counts remain readable",
                "stack_zero_policy": "Existing counts remain readable; credits are unsupported until zero-stack semantics are verified",
                "enabled_policy": ("Optional int32 zero or one; omitted means zero; quantity updates preserve it; "
                                   "every non-empty POSSESSION lists it; two-column receipts saved earlier still verify"),
                "name_policy": (f"New rows need names within the {MAX_ITEM_NAME_BYTES}-byte native cell; "
                                f"stored names up to {STORED_ITEM_NAME_BYTES} bytes stay readable"),
                "unreplayable_count": len(self._unreplayable),
                "asset_sha256": self.asset_sha256}

    def known(self, item_id: object) -> bool:
        return isinstance(item_id, str) and item_id in self._definitions

    def stack(self, item_id: str, *, stored: bool = False) -> int:
        if not self.known(item_id):
            raise ProtocolError(ITEM_UNKNOWN, "Possession ItemID is not a known canonical definition", 503 if stored else 400)
        return self._definitions[item_id]

    def _require_replayable_row(self, item_id: str, counts: dict[str, int]) -> None:
        if item_id not in counts and item_id in self._unreplayable:
            raise ProtocolError("possession_item_unreplayable", "New possession rows need names that fit the native cell", 503)

    def resolve_hash(self, item_hash: int) -> str:
        if not self.is_ready():
            raise ProtocolError("possession_definitions_unavailable", "Verified item definitions are unavailable", 503)
        if not int32(item_hash) or item_hash not in self._names_by_hash:
            raise ProtocolError(ITEM_UNKNOWN, "Item hash does not resolve to a known canonical definition", 503)
        return self._names_by_hash[item_hash]

    def _rows(self, state: dict) -> list[dict]:
        if not self.is_ready():
            raise ProtocolError("possession_definitions_unavailable", "Verified item definitions are unavailable", 503)
        if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
            raise ProtocolError("possession_state_invalid", "Possession storage schema is missing or unsupported", 503)
        tables = state.get("tables")
        rows = tables.get("possessions") if isinstance(tables, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_POSSESSION_ITEMS:
            raise ProtocolError("possession_state_invalid", "Possessions must be a bounded persisted list", 503)
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or not {"ItemID", "Count"} <= row.keys()
                    or row.keys() - {"ItemID", "Count", "Enabled"}):
                raise ProtocolError("possession_state_invalid", "Possession rows require ItemID and Count", 503)
            self.stack(row["ItemID"], stored=True)
            count = row["Count"]
            if row["ItemID"] in seen or not int32(count) or count < 0:
                raise ProtocolError("possession_state_invalid", "Possession counts or identities are invalid", 503)
            if "Enabled" in row and (type(row["Enabled"]) is not int or row["Enabled"] not in (0, 1)):
                raise ProtocolError("possession_state_invalid", "Possession Enabled must be zero or one", 503)
            seen.add(row["ItemID"])
        return rows

    def counts(self, state: dict) -> dict[str, int]:
        return {row["ItemID"]: row["Count"] for row in self._rows(state)}

    def _selected_rows(self, state: dict, item_ids: Iterable[str] | None) -> list[dict]:
        rows = self._rows(state)
        if item_ids is None:
            return rows
        if isinstance(item_ids, (str, bytes)):
            raise ProtocolError("possession_selection_invalid", "Expected a bounded collection of item definitions")
        try:
            selected = iter(item_ids)
        except TypeError as error:
            raise ProtocolError("possession_selection_invalid", "Expected a collection of item definitions") from error
        indexed = {row["ItemID"]: row for row in rows}
        rows, seen = [], set()
        for item_id in selected:
            self.stack(item_id)
            if item_id in seen or len(rows) >= MAX_POSSESSION_ITEMS:
                raise ProtocolError("possession_selection_invalid", "Requested item definitions must be bounded and distinct")
            rows.append(indexed.get(item_id, {"ItemID": item_id, "Count": 0}))
            seen.add(item_id)
        return rows

    @staticmethod
    def _table(rows: list[dict], *, legacy: bool = False) -> Container:
        if not rows:
            return Container("POSSESSION")
        columns = (Column("ItemID", ValueType.STRING, tuple(row["ItemID"] for row in rows)),
                   Column("Count", ValueType.INT, tuple(row["Count"] for row in rows)))
        # Native POSSESSION always carries Enabled; legacy rebuilds the two-column tables older receipts saved.
        if not legacy or any("Enabled" in row for row in rows):
            columns += (Column("Enabled", ValueType.INT, tuple(row.get("Enabled", 0) for row in rows)),)
        return Container("POSSESSION", columns)

    def response(self, state: dict, item_ids: Iterable[str] | None = None, *, include_enabled: bool = False,
                 legacy: bool = False) -> Container:
        if type(include_enabled) is not bool or type(legacy) is not bool:
            raise ProtocolError("possession_selection_invalid", "Enabled output selection must be boolean")
        return self._table(self._selected_rows(state, item_ids), legacy=legacy and not include_enabled)

    def table_matches(self, table: Container, state: dict, item_ids: Iterable[str] | None = None) -> bool:
        rows = self._selected_rows(state, item_ids)
        return table == self._table(rows) or table == self._table(rows, legacy=True)

    def startup_response(self, state: dict) -> Container:
        # Native 901BD0 keeps the column set of the first non-empty POSSESSION load, so 129/137 must carry Enabled.
        return self.response(state, include_enabled=True)

    @staticmethod
    def _with_counts(state, counts):
        updated = deepcopy(state)
        rows = updated["tables"]["possessions"]
        indexed = {row["ItemID"]: row for row in rows}
        for item_id, count in counts.items():
            if item_id in indexed:
                indexed[item_id]["Count"] = count
            else:
                rows.append({"ItemID": item_id, "Count": count})
        return updated

    def set_enabled(self, state: dict, settings: dict[str, int]) -> dict:
        counts = self.counts(state)
        if not isinstance(settings, dict) or len(settings) > MAX_POSSESSION_ITEMS:
            raise ProtocolError("possession_settings_invalid", "Enabled settings must be a bounded item mapping")
        for item_id, enabled in settings.items():
            self.stack(item_id)
            if type(enabled) is not int or enabled not in (0, 1):
                raise ProtocolError("possession_settings_invalid", "Enabled settings must be zero or one")
            if counts.get(item_id, 0) <= 0:
                raise ProtocolError("possession_not_owned", "Enabled settings require a positive owned quantity", 409)
        updated = deepcopy(state)
        for row in updated["tables"]["possessions"]:
            if row["ItemID"] in settings and row.get("Enabled", 0) != settings[row["ItemID"]]:
                row["Enabled"] = settings[row["ItemID"]]
        return updated

    def credit(self, state: dict, grants: dict[str, int]) -> dict:
        counts = self.counts(state)
        if not isinstance(grants, dict) or len(grants) > MAX_POSSESSION_ITEMS:
            raise ProtocolError("possession_grant_invalid", "Possession grants must be a bounded item mapping")
        for item_id, amount in grants.items():
            stack = self.stack(item_id)
            if not int32(amount) or amount <= 0:
                raise ProtocolError("possession_grant_invalid", "Possession grants must be positive int32 quantities")
            if stack == 0:
                raise ProtocolError("possession_stack_unsupported", "Credits require a verified positive item stack limit", 503)
            total = counts.get(item_id, 0) + amount
            if total > INT32_MAX or total > stack:
                raise ProtocolError("possession_stack_exceeded", "Possession grant exceeds the local stack or int32 limit")
            self._require_replayable_row(item_id, counts)
            counts[item_id] = total
        if len(counts) > MAX_POSSESSION_ITEMS:
            raise ProtocolError("possession_grant_invalid", "Possession grant exceeds the local item limit")
        return self._with_counts(state, counts)

    def credit_capped(self, state: dict, grants: dict[str, int]) -> tuple[dict, dict[str, int]]:
        counts = self.counts(state)
        if not isinstance(grants, dict) or len(grants) > MAX_POSSESSION_ITEMS:
            raise ProtocolError("possession_grant_invalid", "Possession grants must be a bounded item mapping")
        discarded = {}
        for item_id, amount in grants.items():
            stack = self.stack(item_id)
            if not int32(amount) or amount <= 0:
                raise ProtocolError("possession_grant_invalid", "Possession grants must be positive int32 quantities")
            # Like the native exchange credit (90F0D0), a Stack 0 item has no cap beyond the int32 count.
            limit = INT32_MAX if stack <= 0 else stack
            current = counts.get(item_id, 0)
            accepted = max(0, min(amount, limit - current))
            if accepted:
                self._require_replayable_row(item_id, counts)
                counts[item_id] = current + accepted
            if accepted < amount:
                discarded[item_id] = amount - accepted
        if len(counts) > MAX_POSSESSION_ITEMS:
            raise ProtocolError("possession_grant_invalid", "Possession grant exceeds the local item limit")
        return self._with_counts(state, counts), discarded

    @staticmethod
    def overflow_response(discarded: dict[str, int]) -> Container:
        if (not isinstance(discarded, dict) or len(discarded) > MAX_POSSESSION_ITEMS
                or any(not isinstance(item_id, str) or not int32(count) or count <= 0 for item_id, count in discarded.items())):
            raise ProtocolError("possession_overflow_invalid", "Discarded quantities must be a bounded positive item mapping", 503)
        return Container("IOF", (Column("ItemID", ValueType.STRING, tuple(discarded)),
                                 Column("Count", ValueType.INT, tuple(discarded.values()))))

    def debit(self, state: dict, costs: dict[str, int]) -> dict:
        counts = self.counts(state)
        if not isinstance(costs, dict) or len(costs) > MAX_POSSESSION_ITEMS:
            raise ProtocolError("possession_cost_invalid", "Possession costs must be a bounded item mapping")
        for item_id, amount in costs.items():
            self.stack(item_id)
            if not int32(amount) or amount <= 0:
                raise ProtocolError("possession_cost_invalid", "Possession costs must be positive int32 quantities")
            available = counts.get(item_id, 0)
            if amount > available:
                raise ProtocolError(INSUFFICIENT, "Possession costs exceed the available quantity", 409)
            counts[item_id] = available - amount
        return self._with_counts(state, counts)

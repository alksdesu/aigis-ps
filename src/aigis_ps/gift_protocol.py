from __future__ import annotations

from dataclasses import dataclass

from .possessions import MAX_ITEM_NAME_BYTES
from .protocol import RequestEnvelope
from .request_fields import fields, invalid, registered, scalar


# The female (Kind 1) and male (Kind 0) item menus override the base menu's 99, so the client never sends it.
GIFT_MENU_GENDERS = {100: 1, 101: 0}
GIFT_OPERATIONS = frozenset(GIFT_MENU_GENDERS)
GIFT_ITEM_FIELDS = tuple(f"ITEM{index}" for index in range(4))
GIFT_COUNT_FIELDS = tuple(f"NUM{index}" for index in range(4))


@dataclass(frozen=True)
class GiftSelection:
    item: str | int
    count: int


@dataclass(frozen=True)
class GiftRequest:
    registration: int
    unit_index: int
    unique_id: int
    alternative: int
    single_item: str | int
    selections: tuple[GiftSelection, ...]


def _item(node) -> str | int:
    kind = node.get("T")
    if kind not in ("I", "S"):
        invalid("Gift item keys must be native integer or canonical string values")
    value = scalar(node, kind, max_bytes=MAX_ITEM_NAME_BYTES)
    if kind == "S" and not value:
        invalid("Canonical gift item keys cannot be empty")
    return value


def parse_gift(envelope: RequestEnvelope) -> GiftRequest:
    names = {"BASE", "ITEM", *GIFT_ITEM_FIELDS, *GIFT_COUNT_FIELDS}
    request = registered(envelope, GIFT_OPERATIONS, names, names)
    base = fields(request["BASE"], {"A7", "AB", "ALTERNATIVE"}, {"A7", "AB", "ALTERNATIVE"})
    unit_index, unique_id, alternative = (scalar(base[name], "I") for name in ("A7", "AB", "ALTERNATIVE"))
    if unit_index < 0 or unique_id <= 0:
        invalid("Gift targets require a nonnegative GetIndex and positive UniqueID")
    selections = tuple(GiftSelection(_item(request[item]), scalar(request[count], "I"))
                       for item, count in zip(GIFT_ITEM_FIELDS, GIFT_COUNT_FIELDS))
    if any(selection.count < 0 for selection in selections):
        invalid("Gift counts must be nonnegative int32 values")
    return GiftRequest(envelope.profile.registration_keys[0], unit_index, unique_id, alternative,
                       _item(request["ITEM"]), selections)

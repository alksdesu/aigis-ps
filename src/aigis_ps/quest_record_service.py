from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import logging
from typing import TYPE_CHECKING

from .alxml import Column, Container, ValueType, dumps
from .bootstrap import UNPLAYED_QUEST_RECORD as UNPLAYED, BootstrapService
from .errors import ProtocolError
from .native_numbers import signed32
from .peruse_state import PERUSE_EVENT_COMMAND, QUEST_EVENT_RESOURCE, asset_root_reader, peruse_quest_ids
from .protocol import RequestEnvelope, encode_response
from .quest_mode_goddess import GODDESS_TYPE
from .quest_modes import EPISODE_FLAG
from .request_fields import registered, values
from .storage import PlayerProfile, Store

if TYPE_CHECKING:
    from .quest_catalog import QuestCatalog


log = logging.getLogger("aigis_ps")

PERUSE_QUESTS = 84
REPRODUCE_MISSIONS = 130
SAVE_SHOWN = 157
OPERATIONS = (PERUSE_QUESTS, REPRODUCE_MISSIONS, SAVE_SHOWN)
QUEST_FIELD = "QI"
RECORD_TABLE = "quest_records"
RECORD_GROUP = "QR"
WRITE_ATTEMPTS = 3
# A77850 registers these DailyReproduce columns; zero rows leave the client without daily revival missions.
MISSION_GROUP = Container("Mission", tuple(Column(name, kind, ()) for name, kind in (
    ("MissionID", ValueType.INT), ("TitleID", ValueType.INT), ("QuestID", ValueType.STRING),
    ("StartDateTime", ValueType.STRING), ("EndDateTime", ValueType.STRING), ("FaceIconID", ValueType.STRING))))


def _digest(quest_ids: frozenset[int]) -> str:
    return hashlib.sha256(",".join(map(str, sorted(quest_ids))).encode("ascii")).hexdigest()


def _quest_ids(request: Mapping) -> tuple[int, ...]:
    if QUEST_FIELD not in request:
        return ()
    # A quest listed twice asks for the same idempotent change once.
    return tuple(dict.fromkeys(values(request[QUEST_FIELD], "I", minimum=1)))


def _unplayed(quest_id: int) -> dict:
    return {"QuestID": quest_id, **UNPLAYED}


def _perused(record: Mapping) -> dict:
    return {**record, "AchieveFlag": signed32(record["AchieveFlag"] | EPISODE_FLAG)}


def _shown(record: Mapping) -> dict:
    return {**record, "Shown": True}


class QuestRecordService:
    def __init__(self, bootstrap: BootstrapService, catalog: QuestCatalog, *,
                 asset_reader: Callable[[str], bytes] | None = None):
        self.bootstrap = bootstrap
        self.catalog = catalog
        self.perusable: frozenset[int] | None = None
        self.peruse_error = None
        try:
            self.perusable = peruse_quest_ids(asset_reader or asset_root_reader(bootstrap.asset_root))
        except ProtocolError as error:
            self.peruse_error = error.code

    def handlers(self):
        return {PERUSE_QUESTS: self.peruse_quests, REPRODUCE_MISSIONS: self.reproduce_missions,
                SAVE_SHOWN: self.save_shown}

    def status(self):
        perusable = ({"ready": True, "quest_count": len(self.perusable), "quest_digest": _digest(self.perusable)}
                     if self.perusable is not None else {"ready": False, "error": self.peruse_error})
        return {"ready": perusable["ready"], "implemented_operations": list(OPERATIONS), "state_writes": True,
                "perusable_quests": perusable,
                "peruse_policy": (f"84 sets AchieveFlag {EPISODE_FLAG:#010x} on the QuestTable records of the quests a "
                                  f"{PERUSE_EVENT_COMMAND} of {QUEST_EVENT_RESOURCE} names and keeps every other bit "
                                  "and column; other named quests answer SYSTEM alone, unnamed ones are refused; "
                                  "no rewards"),
                "reproduce_policy": "130 answers zero DailyReproduce Mission rows; daily revival missions are not served",
                "shown_policy": (f"157 marks Type {GODDESS_TYPE} quests Shown and answers their full QR rows; "
                                 "other quests are refused"),
                "write_policy": ("Missing records start unplayed; unchanged records are not written; "
                                 f"{WRITE_ATTEMPTS} revision attempts against concurrent saves")}

    def reproduce_missions(self, envelope: RequestEnvelope, store: Store) -> bytes:
        registered(envelope, {REPRODUCE_MISSIONS}, ())
        return self._reply(envelope, self.bootstrap._authenticate(envelope, store), MISSION_GROUP)

    def peruse_quests(self, envelope: RequestEnvelope, store: Store) -> bytes:
        quest_ids = _quest_ids(registered(envelope, {PERUSE_QUESTS}, {QUEST_FIELD}, {QUEST_FIELD}))
        return self._save(envelope, store, lambda: self._perusable_quests(quest_ids), _perused)

    def save_shown(self, envelope: RequestEnvelope, store: Store) -> bytes:
        # 784230 sends 157 even when its mission shows no quest, and A64850 then writes no QI.
        quest_ids = _quest_ids(registered(envelope, {SAVE_SHOWN}, {QUEST_FIELD}))
        return self._save(envelope, store, lambda: self._goddess_quests(quest_ids), _shown, answer_records=True)

    def _perusable_quests(self, quest_ids: tuple[int, ...]) -> tuple[int, ...]:
        if self.perusable is None:
            log.warning("Request 84 refused quest events %s: quest event resources are unavailable (%s)",
                        list(quest_ids), self.peruse_error)
            raise ProtocolError("peruse_quest_catalog_unavailable", "Verified quest event resources are unavailable", 503)
        unknown = [quest_id for quest_id in quest_ids if quest_id not in self.perusable]
        if unknown:
            log.warning("Request 84 refused quest events %s: no native event confirms them through %s",
                        unknown, PERUSE_EVENT_COMMAND)
            raise ProtocolError("peruse_quest_unknown", "No native event confirms these quest events", 404)
        # foi6moes refuses records outside QuestTable, so the events of retired quests leave nothing to record.
        return tuple(quest_id for quest_id in quest_ids if self.catalog.quest_type(quest_id) is not None)

    def _goddess_quests(self, quest_ids: tuple[int, ...]) -> tuple[int, ...]:
        others = [quest_id for quest_id in quest_ids if self.catalog.quest_type(quest_id) != GODDESS_TYPE]
        if others:
            log.warning("Request 157 refused quests %s: they are not goddess revival quests", others)
            raise ProtocolError("quest_not_found", "Shown quest saves only name goddess revival quests", 404)
        return quest_ids

    def _records(self, state: dict) -> dict[int, dict]:
        rows = self.bootstrap.table_rows(state, RECORD_TABLE)
        records = {row["QuestID"]: row for row in rows}
        if len(records) != len(rows):
            raise ProtocolError("quest_state_invalid", "Quest records contain duplicate identifiers", 503)
        return records

    def _reply(self, envelope: RequestEnvelope, profile: PlayerProfile, *groups: Container) -> bytes:
        response = dumps(Container(children=(self.bootstrap.system_container(profile), *groups)))
        encode_response(envelope.profile, response)
        return response

    def _save(self, envelope: RequestEnvelope, store: Store, admit: Callable[[], tuple[int, ...]],
              mark: Callable[[Mapping], dict], *, answer_records: bool = False) -> bytes:
        for _ in range(WRITE_ATTEMPTS):
            profile = self.bootstrap._authenticate(envelope, store)
            quest_ids = admit()
            records = self._records(profile.state)
            marked = {quest_id: mark(records.get(quest_id, _unplayed(quest_id))) for quest_id in quest_ids}
            groups = ((Container(RECORD_GROUP, self.bootstrap.columns(RECORD_TABLE, list(marked.values()))),)
                      if answer_records else ())
            response = self._reply(envelope, profile, *groups)
            changed = {quest_id: row for quest_id, row in marked.items() if row != records.get(quest_id)}
            if not changed:
                return response
            updated = {**profile.state, "tables": {**profile.state["tables"],
                                                   RECORD_TABLE: list({**records, **changed}.values())}}
            self.bootstrap.table_rows(updated, RECORD_TABLE)
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Quest records changed while saving", 409)

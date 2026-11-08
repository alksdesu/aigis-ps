from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING

from .alxml import Column, Container, ValueType, dumps
from .errors import ProtocolError
from .native_numbers import UINT32_MASK
from .protocol import RequestEnvelope
from .request_fields import MAX_ROWS, MAX_STRING_BYTES, fields, invalid, registered, scalar, values
from .storage import PlayerProfile, Store

if TYPE_CHECKING:
    from .bootstrap import BootstrapService


log = logging.getLogger("aigis_ps")

SCHEME_URLS = 45
CHEAT_REPORT = 102
FATAL_CHEAT_REPORT = 103
PREMIUM_RECEIVER = 146
TOKEN_EXTENSION = 153
DMM_POINT = 155
OPERATIONS = (SCHEME_URLS, CHEAT_REPORT, FATAL_CHEAT_REPORT, PREMIUM_RECEIVER, TOKEN_EXTENSION, DMM_POINT)
STARTUP_LOADS = (SCHEME_URLS, PREMIUM_RECEIVER)
LAUNCH_IDENTITY_FIELDS = frozenset(("VI", "OT"))
CHEAT_FIELDS = frozenset(("HK", "LG"))
# Native A5AA30 sends the common PlatformCode again with this bit set.
SCHEME_PLATFORM_FLAG = 0x100
# Native 642E60 reloads C6 from the DMM session; only DMM premium info, which this server never sends, makes it nonzero.
LOCAL_PREMIUM_RECEIVER = 0
LOCAL_DMM_POINT = "0"
# Native 9D5EB0 keys detections by unsigned int, so a report lists each key once in unsigned order.
UNSIGNED_KEY_MASK = UINT32_MASK
LOGGED_CHEAT_ENTRIES = 16
LOGGED_CHEAT_TEXT_BYTES = 256
CHEAT_AFTERMATH = MappingProxyType({
    CHEAT_REPORT: (logging.WARNING, "keeps running"),
    FATAL_CHEAT_REPORT: (logging.ERROR, "stops after this report"),
})
# Zero rows empty the client's scheme table, and the home screen opens no link from an empty table.
SCHEME_GROUP = Container("SCHEME", (Column("Url", ValueType.STRING, ()),))
# Native A7CB30 parses Point with _strtoi64 from a string cell only.
POINT_GROUP = Container("default", (Column("Point", ValueType.STRING, (LOCAL_DMM_POINT,)),))


def _logged_entry(key: int, text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= LOGGED_CHEAT_TEXT_BYTES:
        return f"{key}={text!r}"
    shown = data[:LOGGED_CHEAT_TEXT_BYTES].decode("utf-8", "ignore")
    return f"{key}={shown!r}... ({len(data)} bytes)"


def _logged_report(keys: tuple[int, ...], texts: tuple[str, ...]) -> str:
    shown = [_logged_entry(key, text) for key, text in zip(keys[:LOGGED_CHEAT_ENTRIES], texts)]
    if len(keys) > LOGGED_CHEAT_ENTRIES:
        shown.append(f"{len(keys) - LOGGED_CHEAT_ENTRIES} more")
    return "; ".join(shown)


class SessionStubService:
    def __init__(self, bootstrap: BootstrapService):
        self.bootstrap = bootstrap
        diagnostics = bootstrap.startup_diagnostics
        self._startup_loads = MappingProxyType({
            SCHEME_URLS: diagnostics.wrap(SCHEME_URLS, self.scheme_urls),
            PREMIUM_RECEIVER: diagnostics.wrap(PREMIUM_RECEIVER, self.premium_receiver),
        })

    def handlers(self):
        return {**self._startup_loads, TOKEN_EXTENSION: self.extend_token, DMM_POINT: self.dmm_point,
                CHEAT_REPORT: self.cheat_report, FATAL_CHEAT_REPORT: self.cheat_report}

    def status(self):
        return {"ready": True, "implemented_operations": list(OPERATIONS), "startup_loads": list(STARTUP_LOADS),
                "state_writes": False, "native_tested": False,
                "scheme_urls": "SYSTEM with a zero-row SCHEME; the home screen opens no external links",
                "premium_receiver": (f"Only C6={LOCAL_PREMIUM_RECEIVER}; the local account gets no DMM premium info, "
                                     "and other receivers are refused with 501"),
                "token_extension": ("The 152 launch identity; the common UI/UK/MK may still be empty before data "
                                    "selection; no token expiry is kept"),
                "dmm_point": f"The identity check of 153; Point is the string {LOCAL_DMM_POINT}",
                "cheat_reports": {"policy": "Log only; no account action",
                                  "request_limits": {"entries": MAX_ROWS, "text_bytes": MAX_STRING_BYTES},
                                  "log_limits": {"entries": LOGGED_CHEAT_ENTRIES, "text_bytes": LOGGED_CHEAT_TEXT_BYTES}}}

    def _reply(self, profile: PlayerProfile, *groups: Container) -> bytes:
        return dumps(Container(children=(self.bootstrap.system_container(profile), *groups)))

    def scheme_urls(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = registered(envelope, {SCHEME_URLS}, {"Platform"}, {"Platform"})
        if scalar(request["Platform"], "I") != (scalar(request["PlatformCode"], "I") | SCHEME_PLATFORM_FLAG):
            invalid("Scheme requests carry the common PlatformCode with bit 0x100 set")
        return self._reply(self.bootstrap._authenticate(envelope, store), SCHEME_GROUP)

    def premium_receiver(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = registered(envelope, {PREMIUM_RECEIVER}, {"C6"}, {"C6"})
        receiver = scalar(request["C6"], "I")
        profile = self.bootstrap._authenticate(envelope, store)
        if receiver != LOCAL_PREMIUM_RECEIVER:
            raise ProtocolError("dmm_premium_receiver_unsupported", "DMM premium receivers are not implemented; "
                                f"the local account keeps receiver {LOCAL_PREMIUM_RECEIVER}", 501)
        return self._reply(profile)

    def extend_token(self, envelope: RequestEnvelope, store: Store) -> bytes:
        registered(envelope, {TOKEN_EXTENSION}, LAUNCH_IDENTITY_FIELDS, LAUNCH_IDENTITY_FIELDS)
        return self._reply(self.bootstrap.platform_identity(envelope, store, keeps_game_identity=True))

    def dmm_point(self, envelope: RequestEnvelope, store: Store) -> bytes:
        registered(envelope, {DMM_POINT}, LAUNCH_IDENTITY_FIELDS, LAUNCH_IDENTITY_FIELDS)
        return self._reply(self.bootstrap.platform_identity(envelope, store, keeps_game_identity=True), POINT_GROUP)

    def cheat_report(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = registered(envelope, {CHEAT_REPORT, FATAL_CHEAT_REPORT}, {"CH"}, {"CH"})
        report = fields(request["CH"], CHEAT_FIELDS, CHEAT_FIELDS)
        keys = values(report["HK"], "I", minimum=1)
        texts = values(report["LG"], "S", minimum=1)
        if len(keys) != len(texts):
            invalid("Cheat reports pair every HK key with one LG text")
        order = [key & UNSIGNED_KEY_MASK for key in keys]
        if any(left >= right for left, right in zip(order, order[1:])):
            invalid("Cheat report keys must be unique and in unsigned order")
        profile = self.bootstrap._authenticate(envelope, store)
        operation = envelope.profile.registration_keys[0]
        level, aftermath = CHEAT_AFTERMATH[operation]
        log.log(level, "Cheat check %d listed %d detection(s); the native client %s: %s",
                operation, len(keys), aftermath, _logged_report(keys, texts))
        return self._reply(profile)

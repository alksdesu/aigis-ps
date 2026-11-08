from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import json
import re

from .alxml import Container, dumps, loads
from .background_catalog import AUDIT_KINDS
from .background_state import MAX_CONFIG_BYTES, background_response, merge_backgrounds, read_backgrounds
from .bootstrap import STARTUP_COLLECTION_LIMITS
from .business_calendar import NATIVE_DATETIME
from .config_state import configuration_row
from .energy_clock import JST, instant
from .errors import ProtocolError
from .protocol import ProtocolRegistry, encode_response
from .request_fields import invalid, registered, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger


RESPONSE_LIMIT = 16384
HISTORY_NAMESPACE = "background_history"
DIAGNOSTIC_LIMIT = 32
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


class BackgroundService:
    def __init__(self, bootstrap, players, catalog, *, clock=None):
        self.bootstrap, self.players, self.catalog = bootstrap, players, catalog
        self.clock = bootstrap.clock if clock is None else clock
        if not callable(self.clock):
            raise ValueError("Background settings require a callable clock")
        self.content_version = self._content()
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "background_history_invalid",
                                     "Stored background-setting receipts are invalid",
                                     fields=("request", "content", "state", "revision", "response"),
                                     response_limit=RESPONSE_LIMIT)
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (135, 137)}
        self._ready()

    def _content(self, catalog=None):
        version = self.catalog.content_version if catalog is None else catalog
        if not isinstance(version, str) or not _HASH.fullmatch(version):
            raise ProtocolError("background_dependencies_invalid", "Background catalog requires a fixed content version", 503)
        return _digest({"policy": 1, "catalog": version, "time": "JST_second",
                        "new_action": "refresh_time", "retry": "exact_current_revision",
                        "rollback": "reject_before_latest_saved_time", "scope": "home_menu"})

    def replayable_content_versions(self):
        return frozenset(map(self._content, self.catalog.replayable_content_versions()))

    def _ready(self):
        if not self.catalog.is_ready() or self._content() != self.content_version:
            raise ProtocolError("background_dependencies_changed", "Background catalog changed after setup", 503)

    def handlers(self):
        return {135: self.save}

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "implemented_operations": [135],
                "catalog": self.catalog.status(), "content_version": self.content_version,
                "reload_operation": 137, "history_limit": HISTORY_LIMIT,
                "processed_request_limit": PROCESSED_LIMIT, "native_tested": False,
                "request_order": REQUEST_ORDER,
                "save_time_policy": "New selections refresh JST time, including the same background",
                "retry_policy": "Exact request at its committed revision, state and content",
                "legacy_policy": "Keep valid historical names; reject unversioned nonempty rows",
                "other_pages": "Native pages only display their own backgrounds; the background menu is the only 135 sender",
                "diagnostics": "Read-only annotations of stored rows; 137 still replays them and repairs need an explicit operator"}

    def diagnose(self, state):
        rows = read_backgrounds(state)
        findings = self.catalog.audit(rows)
        kinds = Counter(kind for finding in findings for kind in finding["kinds"])
        return {"rows": len(rows), "abnormal_rows": len(findings),
                "kinds": {kind: kinds[kind] for kind in AUDIT_KINDS if kinds[kind]},
                "findings": list(findings[:DIAGNOSTIC_LIMIT]),
                "truncated": len(findings) > DIAGNOSTIC_LIMIT}

    @staticmethod
    def _request(envelope):
        request = registered(envelope, {135}, {"CardID", "ConfigID"}, {"CardID", "ConfigID"})
        card_ids = values(request["CardID"], "I", minimum=1, maximum=2)
        names = values(request["ConfigID"], "S", minimum=1, maximum=2, max_bytes=MAX_CONFIG_BYTES)
        if (len(card_ids) != len(names) or card_ids[0] != 0 or any(card < 0 for card in card_ids)
                or len(set(card_ids)) != len(card_ids) or not names[0] or len(set(names)) != 1):
            invalid("Background selection requires aligned rows for global and current guide settings")
        return card_ids, names[0]

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        payload = self.bootstrap._configuration_response(candidate)
        prm = [node for node in loads(payload, limits=STARTUP_COLLECTION_LIMITS).children if node.name == "PRM"]
        if prm != [background_response(state)]:
            raise ProtocolError("background_provider_inconsistent", "Background startup response differs from saved rows", 503)
        encode_response(self._profiles[137], payload)

    def _response(self, profile, state, card_ids):
        payload = dumps(Container(children=(self.bootstrap._system(profile), background_response(state, card_ids))))
        if len(payload) > RESPONSE_LIMIT:
            raise ProtocolError("background_response_capacity", "Background response exceeds its receipt limit", 503)
        encode_response(self._profiles[135], payload)
        return payload

    def save(self, envelope, store):
        self._ready()
        card_ids, name = self._request(envelope)
        key = hashlib.sha256(f"135:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        now = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            player = self.players.validate(profile.state)
            config = configuration_row(profile.state)
            rows = read_backgrounds(profile.state)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                if cached["revision"] != profile.revision or cached["content"] not in self.replayable_content_versions():
                    raise ProtocolError("background_retry_stale", "Background state or content changed after this request", 409)
                by_id = {row["CardID"]: row for row in rows}
                if (cached["state"] != _state_digest(profile.state)
                        or any(card not in by_id or by_id[card]["ConfigID"] != name for card in card_ids)
                        or len({by_id[card]["SaveTime"] for card in card_ids}) != 1):
                    self._ledger.invalid()
                self._preflight(profile, profile.state)
                response = self._response(profile, profile.state, card_ids)
                if response != cached["response"].encode("utf-8"):
                    self._ledger.invalid()
                encode_response(envelope.profile, response)
                return response
            if self._ledger.processed(history, key):
                raise ProtocolError("background_request_stale", "Background request was already processed", 409)
            if now is None:
                now = instant(self.clock()).astimezone(JST)
            guide_id = self.catalog.guide_id(player, config)
            if card_ids != self.catalog.selection(name, guide_id, now):
                raise ProtocolError("background_guide_mismatch", "Background rows do not match the current guide", 409)
            saved_at = now.strftime(NATIVE_DATETIME)
            if any(row["SaveTime"] > saved_at for row in rows):
                raise ProtocolError("background_clock_rollback", "Background save time cannot precede an existing selection", 409)
            updated = merge_backgrounds(profile.state, [
                {"CardID": card, "ConfigID": name, "SaveTime": saved_at} for card in card_ids])
            self._preflight(profile, updated)
            response = self._response(profile, updated, card_ids)
            encode_response(envelope.profile, response)
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "state": _state_digest(updated), "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8")})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving background settings", 409)

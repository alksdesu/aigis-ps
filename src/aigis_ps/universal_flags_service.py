from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import json
import logging
import re
import threading

from .alxml import Container, dumps, loads
from .bootstrap import STARTUP_COLLECTION_LIMITS
# 914B10 clears its dirty rows only after A7FDF0 returns on a 200; 66E8B0 resends refused bodies.
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError
from .native_commands import STATUS_LIMIT
from .protocol import MAX_XML_BYTES, ProtocolRegistry, encode_response
from .request_fields import invalid, registered, values
from .system_literals import command_id
from .universal_flags import MAX_FLAG_NAME_BYTES, flags_response, read_flags, set_flags


OPERATION = 138
STARTUP_OPERATION = 137
FIELDS = ("ID", "VALUE")
# Unstorable names are answered like every other rejected row, so the field keeps only the document bound.
MAX_UPLOAD_NAME_BYTES = MAX_XML_BYTES
MALFORMED_NAME, PROTECTED_NAME, UNLISTED_NAME, VALUE_MISMATCH = (
    "malformed_name", "protected_name", "unlisted_name", "value_mismatch")
REJECTIONS = (MALFORMED_NAME, PROTECTED_NAME, UNLISTED_NAME, VALUE_MISMATCH)
_HASH = re.compile(r"[a-f0-9]{64}\Z")
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _describe(reason, name):
    return f"{reason} ({len(name.encode('utf-8'))} bytes)" if reason == MALFORMED_NAME else f"{reason} {name!r}"


class UniversalFlagsService:
    def __init__(self, bootstrap, catalog):
        self.bootstrap, self.catalog = bootstrap, catalog
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (STARTUP_OPERATION, OPERATION)}
        self.content_version = self._content()
        self._lock = threading.Lock()
        self._accepted = self._written = 0
        self._rejected = Counter()
        self._rejected_names = {reason: [] for reason in REJECTIONS if reason != MALFORMED_NAME}
        self._ready()

    def _content(self):
        version = getattr(self.catalog, "content_version", None)
        if not isinstance(version, str) or not _HASH.fullmatch(version):
            raise ProtocolError("universal_flags_dependencies_invalid",
                                "Universal-flag uploads require a catalog with a fixed content version", 503)
        return _digest({"policy": 1, "catalog": version, "write": "missing_or_zero_to_listed_value",
                        "rejection": "answer_without_storing_or_echoing", "response": "SYSTEM_and_accepted_rows",
                        "conditions": "not_rechecked", "replay": "monotonic_without_receipts"})

    def _ready(self):
        if (self.bootstrap.universal_flag_provider is None or not self.catalog.is_ready()
                or self._content() != self.content_version):
            raise ProtocolError("universal_flags_dependencies_unavailable",
                                "Universal-flag uploads need the verified catalog and the startup flag provider", 503)

    def handlers(self):
        return {OPERATION: self.save}

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        with self._lock:
            uploads = {"accepted": self._accepted, "written": self._written,
                       "rejected": {reason: self._rejected[reason] for reason in REJECTIONS},
                       "rejected_names": {reason: list(names) for reason, names in self._rejected_names.items()}}
        return {"ready": ready, "error": error, "implemented_operations": [OPERATION], "catalog": self.catalog.status(),
                "content_version": self.content_version, "reload_operation": STARTUP_OPERATION, "native_tested": False,
                "write_policy": "Listed names move from missing or 0 to their resource value once; nonzero values stay",
                "rejection_policy": "Unlisted, protected, malformed or mismatched rows answer 200 without being stored or echoed",
                "response_policy": "SYSTEM plus GF rows of the accepted names in request order with their stored values",
                "replay_policy": "No receipts; monotonic writes make repeated or reordered uploads harmless",
                "condition_policy": "Client-side rank, account-day and date conditions are not rechecked",
                "refusals": REFUSAL_POLICY,
                "uploads": uploads}

    @staticmethod
    def _request(envelope):
        request = registered(envelope, {OPERATION}, FIELDS)
        present = [name for name in FIELDS if name in request]
        if not present:
            return ()
        if len(present) != len(FIELDS):
            invalid("Universal-flag uploads require both ID and VALUE")
        names = values(request["ID"], "S", minimum=1, max_bytes=MAX_UPLOAD_NAME_BYTES)
        numbers = values(request["VALUE"], "I", minimum=1)
        if len(names) != len(numbers):
            invalid("Universal-flag uploads require aligned ID and VALUE rows")
        if len(set(map(command_id, names))) != len(names):
            invalid("Universal-flag uploads require names with distinct native hashes")
        return tuple(zip(names, numbers))

    def _rejection(self, name, value):
        if not name or len(name.encode("utf-8")) > MAX_FLAG_NAME_BYTES:
            return MALFORMED_NAME
        if self.catalog.protected(name):
            return PROTECTED_NAME
        definition = self.catalog.get(name)
        if definition is None:
            return UNLISTED_NAME
        return None if value in definition.values else VALUE_MISMATCH

    @staticmethod
    def _apply(state, accepted):
        if not accepted:
            return state, {}
        stored = {row["ID"]: row["Value"] for row in read_flags(state)}
        updates = {name: value for name, value in accepted if not stored.get(name, 0)}
        return (set_flags(state, updates) if updates else state), updates

    def _response(self, profile, state, names):
        flags = (flags_response(state, names),) if names else ()
        response = dumps(Container(children=(self.bootstrap._system(profile), *flags)))
        encode_response(self._profiles[OPERATION], response)
        return response

    def _preflight(self, profile, state):
        payload = self.bootstrap._configuration_response(replace(profile, state=state))
        flags = [node for node in loads(payload, limits=STARTUP_COLLECTION_LIMITS).children if node.name == "GF"]
        if flags != [flags_response(state)]:
            raise ProtocolError("universal_flags_provider_inconsistent", "Startup flags differ from the saved rows", 503)
        encode_response(self._profiles[STARTUP_OPERATION], payload)

    def _record(self, accepted, rejected, written):
        with self._lock:
            self._accepted += len(accepted)
            self._written += written
            for reason, name in rejected:
                self._rejected[reason] += 1
                names = self._rejected_names.get(reason)
                if names is not None and name not in names and len(names) < STATUS_LIMIT:
                    names.append(name)
        if rejected:
            log.warning("Universal flags %d kept %d uploaded row(s) out of storage and the reply: %s", OPERATION,
                        len(rejected), "; ".join(_describe(reason, name) for reason, name in rejected[:STATUS_LIMIT]))

    def save(self, envelope, store):
        try:
            return self._save(envelope, store)
        except ProtocolError as error:
            log.warning("Universal flags %d refused with %s; the native client %s", OPERATION, error.code, _REFUSAL_AFTERMATH)
            raise

    def _save(self, envelope, store):
        self._ready()
        accepted, rejected = [], []
        for name, value in self._request(envelope):
            reason = self._rejection(name, value)
            if reason is None:
                accepted.append((name, value))
            else:
                rejected.append((reason, name))
        names = [name for name, _ in accepted]
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            updated, updates = self._apply(profile.state, accepted)
            response = self._response(profile, updated, names)
            if updates:
                self._preflight(profile, updated)
                try:
                    store.update_state(updated, profile.revision)
                except ValueError:
                    continue
            self._record(accepted, rejected, len(updates))
            return response
        raise ProtocolError("profile_changed", "Saved universal flags changed while storing the upload", 409)

from __future__ import annotations

import hashlib
import re

from .alxml import AlXmlError, Column, Container, Limits, dumps, effective_name, loads


SCHEDULE_LIMITS = Limits(max_bytes=32 * 1024 * 1024, max_depth=32,
                         max_elements=1_000_000, max_values=1_000_000,
                         max_text_bytes=1024 * 1024)
SCHEDULE_GROUPS = {70: ("CSS", "FPR", "SH", "GacyaStatus", "LCC", "PLC"), 71: ("CSP",)}
_REQUIRED = {70: "CSS", 71: "CSP"}
CREDENTIAL_NAMES = frozenset({
    "ui", "uk", "mk", "uid", "userid", "userkey", "methodkey", "playerid", "accountid", "memberid", "dmmid",
    "authorization", "proxyauthorization", "auth", "authentication", "authkey", "authcode",
    "authtoken", "accesstoken", "refreshtoken", "idtoken", "bearertoken", "token",
    "cookie", "cookies", "setcookie", "password", "passwd", "passphrase", "secret",
    "clientsecret", "apisecret", "apikey", "privatekey", "sessionkey", "sessionid",
    "sessiontoken", "logintoken", "loginid", "loginpassword", "credential", "credentials",
    "oauthconsumerkey", "oauthconsumersecret", "oauthtoken", "oauthtokensecret",
    "gpvi", "gpot", "system",
})
SCHEDULE_PRIVATE_FIELDS = CREDENTIAL_NAMES
_SEPARATORS = re.compile(r"[_.-]")


class ScheduleCaptureError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _credential_name(name: str) -> str | None:
    normalized = _SEPARATORS.sub("", name.split("[", 1)[0].casefold())
    return normalized if normalized in CREDENTIAL_NAMES else None


def _sanitize_group(group: Container, removed: set[str]) -> Container:
    children = []
    seen = set()
    for child in group.children:
        credential = _credential_name(child.name)
        if credential is not None:
            removed.add(credential)
            continue
        name = effective_name(child)
        if name in seen:
            raise ScheduleCaptureError("duplicate_schedule_field")
        seen.add(name)
        children.append(_sanitize_group(child, removed) if isinstance(child, Container) else child)
    return Container(group.name, tuple(children), group.index)


def _describe_group(group: Container) -> dict:
    fields = {}
    nested = {}
    row_counts = set()
    for child in group.children:
        name = effective_name(child)
        if isinstance(child, Column):
            fields[name] = child.type.value
            row_counts.add(len(child.values))
        else:
            nested[name] = _describe_group(child)
    result = {"rows": max(row_counts, default=0), "row_counts": sorted(row_counts), "field_types": fields}
    if nested:
        result["groups"] = nested
    return result


def sanitize_schedule_response(operation: int, raw: bytes) -> tuple[bytes, dict]:
    if type(operation) is not int or operation not in SCHEDULE_GROUPS:
        raise ScheduleCaptureError("unsupported_schedule_operation")
    if not isinstance(raw, bytes) or not 0 < len(raw) <= SCHEDULE_LIMITS.max_bytes:
        raise ScheduleCaptureError("invalid_schedule_payload")
    try:
        document = loads(raw, limits=SCHEDULE_LIMITS)
    except (AlXmlError, ValueError, TypeError, RecursionError):
        raise ScheduleCaptureError("invalid_schedule_xml") from None
    allowed = SCHEDULE_GROUPS[operation]
    retained = {}
    removed: set[str] = set()
    for child in document.children:
        if child.name not in allowed:
            continue
        if not isinstance(child, Container) or child.index is not None:
            raise ScheduleCaptureError("invalid_schedule_group")
        if child.name in retained:
            raise ScheduleCaptureError("duplicate_schedule_group")
        retained[child.name] = _sanitize_group(child, removed)
    if _REQUIRED[operation] not in retained:
        raise ScheduleCaptureError("missing_schedule_group")
    clean = Container("DA", tuple(retained.values()))
    try:
        sanitized = dumps(clean, limits=SCHEDULE_LIMITS)
    except (AlXmlError, ValueError, TypeError, RecursionError):
        raise ScheduleCaptureError("invalid_schedule_output") from None
    metadata = {
        "operation": operation, "input_bytes": len(raw), "output_bytes": len(sanitized),
        "kept_groups": list(retained), "missing_groups": [name for name in allowed if name not in retained],
        "groups": {name: _describe_group(group) for name, group in retained.items()},
        "removed_columns": sorted(removed), "sha256": hashlib.sha256(sanitized).hexdigest(),
    }
    return sanitized, metadata

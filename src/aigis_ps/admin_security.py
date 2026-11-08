from __future__ import annotations

from http import HTTPStatus
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import uuid

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .admin_command import AdminError
from .generation import MAX_PORT
from .local_io import MAX_JSON_DEPTH, atomic_replace, checked_path, read_json_object
from .startup_diagnostics import INTERNAL_ERROR
from .storage import serialize, timestamp


log = logging.getLogger("aigis_ps")
ADMIN_ROOT = "/admin"
STATIC_ROOT = ADMIN_ROOT + "/static"
API_ROOT = ADMIN_ROOT + "/api/v1"
BIND_HOSTS = ("127.0.0.1", "::1")
ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost", "::1")
TOKEN_BYTES = 32
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
MAX_BODY_BYTES = 1024 * 1024
MAX_BODY_DIGITS = len(str(MAX_BODY_BYTES))
JSON_MEDIA_TYPE = "application/json"
JSON_CHARSET = "utf-8"
READ_METHODS = frozenset(("GET", "HEAD"))
WRITE_METHODS = frozenset(("POST", "PUT", "PATCH", "DELETE"))
ALLOWED_FETCH_SITES = frozenset(("same-origin", "none"))
BEARER = "bearer"
BODY_STATE = "admin_body"
DOCUMENT_STATE = "admin_document"
WEBSOCKET_POLICY_VIOLATION = 1008
ACCESS_DIRECTORY = "admin"
ACCESS_FILE = "access.json"
ACCESS_SCHEMA = 1
MAX_ACCESS_BYTES = 4096
CONTENT_SECURITY_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                           "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
SECURITY_HEADERS = (
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("cache-control", "no-store"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
)
_RAW_SECURITY_HEADERS = tuple((name.encode("latin-1"), value.encode("latin-1")) for name, value in SECURITY_HEADERS)
_SECURITY_HEADER_NAMES = frozenset(name for name, _ in _RAW_SECURITY_HEADERS)
_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def require_token(token: object) -> str:
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Admin tokens are URL-safe random text of at least 32 characters")
    return token


def require_bind_host(host: object) -> str:
    if not isinstance(host, str) or host not in BIND_HOSTS:
        raise ValueError("The admin console binds only to 127.0.0.1 or ::1")
    return host


def require_port(port: object) -> int:
    if type(port) is not int or not 0 < port <= MAX_PORT:
        raise ValueError("Ports are integers from 1 to 65535")
    return port


def authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def console_url(host: str, port: int, token: str) -> str:
    # The fragment keeps the token out of requests, server logs and the Referer header.
    return f"http://{authority(require_bind_host(host), require_port(port))}{ADMIN_ROOT}/#token={require_token(token)}"


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise AdminError("admin_json_duplicate_key", "JSON objects must not repeat a key", HTTPStatus.BAD_REQUEST)
    return document


def _nonfinite() -> AdminError:
    return AdminError("admin_json_invalid", "JSON numbers must be finite", HTTPStatus.BAD_REQUEST)


def _no_constants(name: str) -> object:
    raise _nonfinite()


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _nonfinite()
    return value


def _too_deep() -> AdminError:
    return AdminError("admin_json_too_deep", f"JSON nesting must not exceed {MAX_JSON_DEPTH} levels", HTTPStatus.BAD_REQUEST)


def _require_depth(document: object) -> None:
    pending = [(document, 1)]
    while pending:
        value, depth = pending.pop()
        children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else None
        if children is None:
            continue
        if depth > MAX_JSON_DEPTH:
            raise _too_deep()
        pending.extend((child, depth + 1) for child in children)


def decode_json(raw: bytes | str) -> object:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        document = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_no_constants,
                              parse_float=_finite_float)
    except AdminError:
        raise
    except RecursionError as error:
        raise _too_deep() from error
    except ValueError as error:
        raise AdminError("admin_json_invalid", "The body must be one UTF-8 JSON document", HTTPStatus.BAD_REQUEST) from error
    _require_depth(document)
    return document


def _json_media(value: str) -> bool:
    media, *parameters = (part.strip() for part in value.split(";"))
    if media.lower() != JSON_MEDIA_TYPE:
        return False
    for parameter in parameters:
        name, _, setting = parameter.partition("=")
        if name.strip().lower() != "charset" or setting.strip().strip('"').lower() != JSON_CHARSET:
            return False
    return True


def _headers(scope: Scope) -> dict[str, list[bytes]]:
    found: dict[str, list[bytes]] = {}
    for name, value in scope.get("headers", ()):
        found.setdefault(name.decode("latin-1").lower(), []).append(value)
    return found


def _header(headers: dict[str, list[bytes]], name: str) -> bytes | None:
    values = headers.get(name, ())
    if len(values) > 1:
        raise AdminError("admin_header_repeated", f"The {name} header must appear at most once", HTTPStatus.BAD_REQUEST)
    return values[0] if values else None


def _text(value: bytes | None) -> str | None:
    return None if value is None else value.decode("latin-1")


def _public(path: str, method: str) -> bool:
    return method in READ_METHODS and (path in (ADMIN_ROOT, ADMIN_ROOT + "/") or path.startswith(STATIC_ROOT + "/"))


def _too_large() -> AdminError:
    return AdminError("admin_body_too_large", f"Request bodies must not exceed {MAX_BODY_BYTES} bytes",
                      HTTPStatus.REQUEST_ENTITY_TOO_LARGE)


async def _read_body(receive: Receive, headers: dict[str, list[bytes]]) -> bytes:
    declared = _text(_header(headers, "content-length"))
    if declared is not None:
        if not declared.isascii() or not declared.isdigit():
            raise AdminError("admin_body_invalid", "Content-Length must be a byte count", HTTPStatus.BAD_REQUEST)
        significant = declared.lstrip("0") or "0"
        # Overlong counts are refused by length: int() rejects digit strings past its conversion limit.
        if len(significant) > MAX_BODY_DIGITS or int(significant) > MAX_BODY_BYTES:
            raise _too_large()
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise AdminError("admin_body_incomplete", "The client disconnected before sending the whole body",
                             HTTPStatus.BAD_REQUEST)
        body.extend(message.get("body", b""))
        if len(body) > MAX_BODY_BYTES:
            raise _too_large()
        if not message.get("more_body", False):
            return bytes(body)


def _replay(body: bytes, receive: Receive) -> Receive:
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if delivered:
            return await receive()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


def _secured(send: Send) -> Send:
    async def secured(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = [(name, value) for name, value in message.get("headers", ()) if name.lower() not in _SECURITY_HEADER_NAMES]
            message = {**message, "headers": [*headers, *_RAW_SECURITY_HEADERS]}
        await send(message)

    return secured


async def _reply(scope: Scope, receive: Receive, send: Send, error: AdminError) -> None:
    headers = _CHALLENGE if error.status == HTTPStatus.UNAUTHORIZED else None
    await JSONResponse({"error": error.payload()}, status_code=error.status, headers=headers)(scope, receive, send)


class AdminSecurityMiddleware:
    def __init__(self, app: ASGIApp, *, token: str, port: int):
        self.app = app
        self._token = require_token(token).encode("ascii")
        self._hosts = frozenset(authority(name, require_port(port)) for name in ALLOWED_HOSTNAMES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": WEBSOCKET_POLICY_VIOLATION})
            return
        secured = _secured(send)
        try:
            receive = await self._admit(scope, receive)
        except AdminError as error:
            log.warning("Refused admin %s %r: %s", scope.get("method"), scope.get("path"), error.code)
            await _reply(scope, receive, secured, error)
            return
        await self._serve(scope, receive, secured)

    async def _serve(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception:
            log.exception("Admin request failed: %s %s", scope.get("method"), scope.get("path"))
            if started:
                raise
            await _reply(scope, receive, send, AdminError(INTERNAL_ERROR, "The admin request failed",
                                                          HTTPStatus.INTERNAL_SERVER_ERROR))

    async def _admit(self, scope: Scope, receive: Receive) -> Receive:
        headers = _headers(scope)
        host = (_text(_header(headers, "host")) or "").lower()
        if host not in self._hosts:
            raise AdminError("admin_host_invalid", "The Host header must name this loopback console and its port",
                             HTTPStatus.BAD_REQUEST)
        origin = _text(_header(headers, "origin"))
        if origin is not None and origin != f"{scope.get('scheme', 'http')}://{host}":
            raise AdminError("admin_origin_denied", "Requests from other origins are refused", HTTPStatus.FORBIDDEN)
        site = _text(_header(headers, "sec-fetch-site"))
        if site is not None and site.lower() not in ALLOWED_FETCH_SITES:
            raise AdminError("admin_fetch_site_denied", "Cross-site requests are refused", HTTPStatus.FORBIDDEN)
        method = scope["method"]
        if method == "OPTIONS":
            raise AdminError("admin_method_not_allowed", "The admin console does not answer CORS preflight requests",
                             HTTPStatus.METHOD_NOT_ALLOWED)
        if not _public(scope["path"], method) and not self._authorized(headers):
            raise AdminError("admin_unauthorized", "A valid admin bearer token is required", HTTPStatus.UNAUTHORIZED)
        if method not in WRITE_METHODS:
            return receive
        content_type = _text(_header(headers, "content-type"))
        if content_type is None or not _json_media(content_type):
            raise AdminError("admin_content_type_unsupported", "Admin writes accept only application/json bodies",
                             HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        body = await _read_body(receive, headers)
        document = decode_json(body)
        state = scope.setdefault("state", {})
        state[BODY_STATE], state[DOCUMENT_STATE] = body, document
        return _replay(body, receive)

    def _authorized(self, headers: dict[str, list[bytes]]) -> bool:
        value = _header(headers, "authorization")
        if value is None:
            return False
        scheme, _, credentials = value.partition(b" ")
        return scheme.decode("latin-1").lower() == BEARER and secrets.compare_digest(credentials, self._token)


def access_path(data_dir: Path) -> Path:
    root = Path(data_dir)
    return checked_path(root / ACCESS_DIRECTORY / ACCESS_FILE, root)


def write_access_file(data_dir: Path, *, token: str, host: str, port: int) -> Path:
    document = {"schema_version": ACCESS_SCHEMA, "url": console_url(host, port, token), "token": token,
                "admin_host": host, "admin_port": port, "pid": os.getpid(), "created_at": timestamp()}
    path = access_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(serialize(document).encode("utf-8") + b"\n")
        atomic_replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def remove_access_file(data_dir: Path, token: str) -> bool:
    path = access_path(data_dir)
    try:
        recorded = read_json_object(path, limit=MAX_ACCESS_BYTES, root=Path(data_dir)).get("token")
    except (OSError, ValueError):
        return False
    # A newer console process may have replaced the file; only the owner of its token removes it.
    if not isinstance(recorded, str) or not secrets.compare_digest(recorded.encode("utf-8"), require_token(token).encode("ascii")):
        return False
    path.unlink(missing_ok=True)
    return True

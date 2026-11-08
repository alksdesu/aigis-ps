from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import MappingProxyType

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import __version__
from .asset_service import AssetService
from .errors import ProtocolError, UnsupportedOperation
from .generation import Generation, GenerationRuntime
from .protocol import MAX_REQUEST_BYTES, ProtocolRegistry, RequestEnvelope, WireProfile, encode_response
from .startup_diagnostics import StartupLoad
from .storage import Store
from .native_cdn import LOCAL_CDN_PATH, NativeCdnService, respond as cdn_response


Handler = Callable[[RequestEnvelope, Store], bytes]
log = logging.getLogger("aigis_ps")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
API_PREFIX = "/api"
DATABASE_NAME = "aigis.sqlite3"
HEALTH_DETAIL_FIELDS = MappingProxyType({"bootstrap": frozenset(("startup_blockers", "unavailable", "startup_diagnostics"))})
_SCALARS = (str, int, float, bool, type(None))


def requested_route(path: str) -> str:
    for prefix in (API_PREFIX + "/", "/"):
        name = path.removeprefix(prefix)
        if name != path and "/" not in name:
            return name
    return ""


def compact_readiness(readiness: Mapping[str, object]) -> dict:
    # start-client refuses health replies over 64 KiB, so nested details stay on /local/status.
    compact = {}
    for domain, status in readiness.items():
        if isinstance(status, Mapping):
            details = HEALTH_DETAIL_FIELDS.get(domain, frozenset())
            compact[domain] = {name: value for name, value in status.items()
                               if name in details or isinstance(value, _SCALARS)}
        elif isinstance(status, _SCALARS):
            compact[domain] = status
    return compact


def health_readiness(readiness: Mapping[str, object]) -> dict:
    # Every domain adds policy prose, so the health reply keeps only values and identifiers.
    health = {}
    for domain, status in compact_readiness(readiness).items():
        if isinstance(status, dict):
            health[domain] = {name: value for name, value in status.items() if not _prose(value)}
        elif not _prose(status):
            health[domain] = status
    return health


def _prose(value: object) -> bool:
    return isinstance(value, str) and any(character.isspace() for character in value)


def observed_operations(profile: WireProfile) -> tuple[int | str, ...]:
    return profile.registration_keys or (profile.name,)


@dataclass(frozen=True)
class ServedGeneration:
    handlers: Mapping[int | str, Handler]
    status_provider: Callable[[], dict] | None = None
    native_cdn: NativeCdnService | None = None

    @classmethod
    def of(cls, generation: Generation) -> ServedGeneration:
        return cls(generation.handlers, generation.live_status, generation.bundle.native_cdn)


class FixedGenerations:
    # Fixed handlers never switch, so no runtime needs to hear about client activity.
    def __init__(self, served: ServedGeneration):
        self._served = served

    @contextmanager
    def lease(self) -> Iterator[ServedGeneration]:
        yield self._served

    def observe_request(self, route: int | str) -> None:
        return None


class RuntimeGenerations:
    def __init__(self, runtime: GenerationRuntime):
        self._runtime = runtime

    @contextmanager
    def lease(self) -> Iterator[ServedGeneration]:
        with self._runtime.lease() as generation:
            yield ServedGeneration.of(generation)

    def observe_request(self, route: int | str) -> None:
        self._runtime.observe_request(route)


def create_app(
    data_dir: Path,
    *,
    registry: ProtocolRegistry | None = None,
    handlers: Mapping[int | str, Handler] | None = None,
    assets: AssetService | None = None,
    initial_state: dict | None = None,
    status_provider: Callable[[], dict] | None = None,
    native_cdn: NativeCdnService | None = None,
    state_initializer: Callable[[dict], dict] | None = None,
    runtime: GenerationRuntime | None = None,
) -> FastAPI:
    if runtime is None:
        generations = FixedGenerations(ServedGeneration(MappingProxyType(dict(handlers or {})), status_provider, native_cdn))
    elif isinstance(runtime, GenerationRuntime) and all(value is None for value in (handlers, status_provider, native_cdn)):
        generations = RuntimeGenerations(runtime)
    else:
        raise ValueError("Serve either a generation runtime or fixed handlers, readiness and native resources")
    registry = registry or ProtocolRegistry.load()
    store = Store(Path(data_dir) / DATABASE_NAME, initial_state)
    if state_initializer is not None:
        for _ in range(3):
            profile = store.profile()
            updated = state_initializer(deepcopy(profile.state))
            if not isinstance(updated, dict):
                raise ValueError("Player-state initializer must return an object")
            if updated == profile.state:
                break
            try:
                store.update_state(updated, profile.revision)
                break
            except ValueError:
                continue
        else:
            raise ValueError("Player state changed while initializing the local profile")
    application = FastAPI(title="Aigis local server", docs_url=None, redoc_url=None, openapi_url=None)
    application.state.store = store
    application.state.registry = registry

    def operation_error(route: str, status: int, code: str, message: str | None = None):
        if any(profile.response_kind == "integer_pair" for profile in registry.by_route.get(route, ())):
            # The native reader dereferences both fields even after an HTTP error.
            return Response(b"0,0", status_code=status, media_type="text/plain; charset=ascii")
        return JSONResponse({"error": {"code": code, **({"message": message} if message else {})}}, status_code=status)

    def local_error(error: ProtocolError) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": str(error)}}, status_code=error.status)

    def refuse(request: Request, status: int, code: str):
        log.warning("Refused %s %r for host %r: %s", request.method, request.url.path, request.url.hostname, code)
        return operation_error(requested_route(request.url.path), status, code)

    def record_request(envelope: RequestEnvelope | None, route: str, request_size: int, response_size: int,
                       status: int, error_code: str | None) -> None:
        try:
            store.record_request(envelope, route, request_size, response_size, status, error_code)
        except Exception:
            # A diagnostic write failure must not replace the reply the native reader already expects.
            log.exception("Request log write failed: %s", route)

    @application.middleware("http")
    async def local_origin(request: Request, call_next):
        if request.url.hostname not in LOCAL_HOSTS:
            return refuse(request, 400, "invalid_host")
        if request.headers.get("origin"):
            return refuse(request, 403, "cross_origin_denied")
        return await call_next(request)

    def service_report(served: ServedGeneration, *, compact: bool) -> dict:
        result = {"service": "aigis-ps", "version": __version__, "status": "ok",
                "protocol_profiles": len(registry.profiles), "registered_operations": len(registry.by_registration),
                "configured_handlers": sorted(map(str, served.handlers)), "assets": assets is not None}
        if served.status_provider is not None:
            readiness = served.status_provider()
            result["readiness"] = health_readiness(readiness) if compact else readiness
        result["native_cdn"] = served.native_cdn.status() if served.native_cdn else None
        return result

    @application.get("/health")
    def health():
        try:
            with generations.lease() as served:
                return service_report(served, compact=True)
        except ProtocolError as error:
            return local_error(error)

    @application.get("/local/status")
    def status():
        profile = store.profile()
        try:
            with generations.lease() as served:
                report = service_report(served, compact=False)
                configured = served.handlers
        except ProtocolError as error:
            return local_error(error)
        missing = [number for number, contract in registry.by_registration.items()
                   if not any(key in configured for key in contract.registration_keys)
                   and contract.name not in configured]
        missing.extend(report.get("readiness", {}).get("bootstrap", {}).get("unimplemented_operations", []))
        return {**report, "player_id": profile.player_id, "revision": profile.revision,
                "save_schema": 1, "unimplemented_operations": sorted(set(missing), key=str)}

    @application.get("/local/requests")
    def requests(limit: int = 50):
        if not 1 <= limit <= 200:
            return JSONResponse({"error": {"code": "invalid_limit"}}, status_code=400)
        return {"requests": store.recent_requests(limit)}

    @application.get("/assets/{name:path}")
    def asset(name: str, request: Request):
        if assets is None:
            return JSONResponse({"error": {"code": "assets_not_configured"}}, status_code=404)
        try:
            content, digest = assets.read(name)
        except ProtocolError as error:
            return local_error(error)
        etag = f'"{digest}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(content, media_type="application/octet-stream", headers={"ETag": etag, "Cache-Control": "no-cache"})

    @application.api_route(LOCAL_CDN_PATH + "/{group}/{object_name}", methods=["GET", "HEAD"])
    def cdn_object(group: str, object_name: str, request: Request):
        route = "cdn/" + group + "/" + object_name
        status_code, content_size, error_code = 200, 0, None
        try:
            generations.observe_request(route)
            with generations.lease() as served:
                if served.native_cdn is None:
                    raise ProtocolError("native_cdn_unavailable", "Native resources have not been configured", 503)
                payload = served.native_cdn.read(group, object_name)
            response = cdn_response(payload, method=request.method, range_header=request.headers.get("range"),
                                    if_none_match=request.headers.get("if-none-match"), if_range=request.headers.get("if-range"))
            status_code, content_size = response.status, len(response.content)
            return Response(response.content, status_code=response.status, media_type="application/octet-stream", headers=response.headers)
        except ProtocolError as error:
            status_code, error_code = error.status, error.code
            return local_error(error)
        finally:
            record_request(None, route, 0, content_size, status_code, error_code)

    def handler_for(handlers: Mapping[int | str, Handler], envelope: RequestEnvelope) -> Handler:
        candidates = [handlers[number] for number in envelope.profile.registration_keys if number in handlers]
        if not candidates and envelope.profile.name in handlers:
            candidates.append(handlers[envelope.profile.name])
        if not candidates:
            raise UnsupportedOperation(f"Native operation {envelope.profile.identity} has no local handler")
        if any(handler is not candidates[0] for handler in candidates):
            raise ProtocolError("handler_conflict", "Equivalent wire profiles have conflicting handlers", 500)
        return candidates[0]

    def dispatch(envelope: RequestEnvelope) -> tuple[bytes, str]:
        for observed in observed_operations(envelope.profile):
            generations.observe_request(observed)
        with generations.lease() as served:
            handler = handler_for(served.handlers, envelope)
            encode = partial(encode_response, envelope.profile)
            if isinstance(handler, StartupLoad):
                return handler.serve(encode, envelope, store)
            return encode(handler(envelope, store))

    @application.post(API_PREFIX + "/{route}")
    @application.post("/{route}")
    async def operation(route: str, request: Request):
        body = bytearray()
        envelope = None
        response_size = 0
        status_code = 200
        error_code = None
        try:
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    raise ProtocolError("request_too_large", "Request exceeds the wire size limit", 413)
                body.extend(chunk)
            envelope = registry.decode(route, bytes(body))
            wire, media_type = await run_in_threadpool(dispatch, envelope)
            response_size = len(wire)
            return Response(wire, media_type=media_type, headers={"Cache-Control": "no-store", "X-Aigis-Profile": envelope.profile.identity})
        except ProtocolError as error:
            status_code, error_code = error.status, error.code
            return operation_error(route, error.status, error.code, str(error))
        except Exception:
            status_code, error_code = 500, "internal_error"
            log.exception("Local operation failed: %s", route)
            return operation_error(route, 500, error_code)
        finally:
            await run_in_threadpool(record_request, envelope, route, len(body), response_size, status_code, error_code)

    return application


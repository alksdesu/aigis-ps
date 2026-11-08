from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .admin_app import ConsoleFiles, UI_DIRECTORY, ui_pages
from .admin_security import API_ROOT, BODY_STATE, STATIC_ROOT, AdminSecurityMiddleware
from .desktop_controller import DesktopController, DesktopError


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewProfileRequest(EmptyRequest):
    name: str = Field(min_length=1, max_length=64)


class SelectProfileRequest(EmptyRequest):
    profile_id: str = Field(min_length=1, max_length=64)


def create_desktop_app(controller: DesktopController, *, port: int, token: str, transport=None) -> FastAPI:
    upstream = httpx.Client(trust_env=False, timeout=httpx.Timeout(60, connect=3), follow_redirects=False,
                            transport=transport)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            upstream.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(AdminSecurityMiddleware, token=token, port=port)

    @app.exception_handler(DesktopError)
    async def desktop_error(request, error):
        return JSONResponse({"error": {"code": error.code, "message": str(error)}}, status_code=error.status)

    @app.exception_handler(ValueError)
    async def input_error(request, error):
        return JSONResponse({"error": {"code": "desktop_input_invalid", "message": str(error)}}, status_code=422)

    @app.get("/admin/")
    def index():
        return FileResponse(UI_DIRECTORY / "index.html", media_type="text/html")

    @app.get(STATIC_ROOT + "/pages/launcher.js")
    def launcher_module():
        return Response('export { default } from "../desktop-page.js";\n', media_type="text/javascript")

    app.mount(STATIC_ROOT, ConsoleFiles(directory=UI_DIRECTORY), name="desktop-static")

    @app.get(API_ROOT + "/ui-pages")
    def pages():
        return {"pages": [{"name": "launcher", "module": STATIC_ROOT + "/pages/launcher.js"},
                          *ui_pages(UI_DIRECTORY)]}

    @app.get(API_ROOT + "/desktop")
    def status():
        return controller.status()

    @app.post(API_ROOT + "/desktop/service/start")
    def start(body: EmptyRequest):
        return controller.start_service()

    @app.post(API_ROOT + "/desktop/service/stop")
    def stop(body: EmptyRequest):
        return controller.stop_service()

    @app.post(API_ROOT + "/desktop/game/start")
    def game(body: EmptyRequest):
        return controller.start_game()

    @app.post(API_ROOT + "/desktop/profiles")
    def create(body: NewProfileRequest):
        return controller.create_profile(body.name)

    @app.post(API_ROOT + "/desktop/profiles/select")
    def select(body: SelectProfileRequest):
        return controller.select_profile(body.profile_id)

    def forward(request: Request, *, session: bool = False) -> httpx.Response:
        profile = request.headers.get("x-aigis-profile")
        if not session and not profile:
            raise DesktopError("请先刷新当前存档会话。", "desktop_profile_required")
        endpoint, credential = controller.endpoint(None if session else profile)
        headers = {"Authorization": f"Bearer {credential}", "Origin": endpoint,
                   "Content-Type": "application/json"}
        path = request.url.path
        # The destination is fixed; request URLs and forwarding headers never select a host.
        url = endpoint + path
        if request.url.query:
            url += "?" + request.url.query
        body = getattr(request.state, BODY_STATE, b"")
        try:
            response = upstream.request(request.method, url, headers=headers, content=body)
        except httpx.HTTPError as error:
            raise DesktopError("服务连接中断。写入结果可能未知，请查看记录后重试同一次操作。",
                               "desktop_service_disconnected", 503) from error
        if response.is_redirect:
            raise DesktopError("服务返回了不允许的重定向。", "desktop_redirect_refused", 502)
        return response

    @app.get(API_ROOT + "/session")
    def session(request: Request):
        state = controller.status()
        profile = state["selected_profile"]
        result = {"player_id": None, "revision": None, "generation_id": None, "server_now": {},
                  "checks": {}, "client": {}}
        if profile is not None and state["service"]["state"] == "running":
            response = forward(request, session=True)
            if response.status_code != 200:
                return Response(response.content, status_code=response.status_code, media_type="application/json")
            result = response.json()
        result.update({"desktop": True, "service_state": state["service"]["state"],
                       "desktop_profile_id": None if profile is None else profile["id"],
                       "desktop_profile_name": None if profile is None else profile["name"]})
        return result

    @app.api_route(API_ROOT + "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    def proxy(path: str, request: Request):
        if path == "desktop" or path.startswith("desktop/"):
            raise DesktopError("此操作不存在。", "desktop_not_found", 404)
        response = forward(request)
        return Response(response.content, status_code=response.status_code,
                        media_type=response.headers.get("content-type", "application/json"))

    return app

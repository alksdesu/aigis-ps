import httpx
import pytest
from fastapi.testclient import TestClient

from aigis_ps.desktop_api import create_desktop_app
from aigis_ps.desktop_controller import DesktopError


TOKEN = "x" * 43
PORT = 18540
ROOT = "/admin/api/v1"


class Controller:
    def __init__(self, root):
        self.root = root
        self.running = False
        self.profile_id = "default"
        self.calls = []

    def status(self):
        profile = None if self.profile_id is None else {"id": self.profile_id, "name": "存档", "data_dir": "test", "selected": True}
        return {"service": {"state": "running" if self.running else "stopped", "pid": None, "error": None},
                "game": {"running": False, "state": "stopped", "pid": None, "error": None},
                "selected_profile": profile, "profiles": [] if profile is None else [profile], "log_path": None}

    def endpoint(self, profile_id=None):
        if not self.running:
            raise DesktopError("offline", "desktop_service_offline", 503)
        if profile_id is not None and profile_id != self.profile_id:
            raise DesktopError("profile changed", "desktop_profile_changed")
        return "http://127.0.0.1:18541", "internal-secret"

    def start_service(self):
        self.calls.append("start")
        return self.status()

    def stop_service(self):
        self.calls.append("stop")
        return self.status()

    def start_game(self):
        self.calls.append("game")
        return self.status()

    def create_profile(self, name):
        self.calls.append(("create", name))
        return self.status()

    def select_profile(self, profile_id):
        self.calls.append(("select", profile_id))
        return self.status()


@pytest.fixture
def desktop(tmp_path):
    controller = Controller(tmp_path)
    forwarded = []

    def handle(request):
        forwarded.append(request)
        return httpx.Response(200, json={"player_id": "player", "revision": 3})

    app = create_desktop_app(controller, port=PORT, token=TOKEN, transport=httpx.MockTransport(handle))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield controller, forwarded, client


def test_stopped_console_serves_ui_and_profile_without_accessing_service(desktop):
    controller, forwarded, client = desktop
    assert client.get("/admin/").status_code == 200
    wrapper = client.get("/admin/static/pages/launcher.js")
    assert '../desktop-page.js' in wrapper.text
    assert client.get("/admin/static/desktop-page.js").status_code == 200
    session = client.get(ROOT + "/session").json()
    assert session["desktop"] is True and session["service_state"] == "stopped"
    assert session["desktop_profile_id"] == "default" and session["player_id"] is None
    pages = client.get(ROOT + "/ui-pages").json()["pages"]
    assert {"launcher", "backups", "state"} <= {page["name"] for page in pages}
    assert not forwarded


def test_console_without_profiles_serves_session_without_upstream(desktop):
    controller, forwarded, client = desktop
    controller.profile_id = None
    status = client.get(ROOT + "/desktop")
    assert status.status_code == 200 and status.json()["profiles"] == []
    assert status.json()["selected_profile"] is None
    session = client.get(ROOT + "/session")
    assert session.status_code == 200
    assert session.json()["desktop_profile_id"] is None and session.json()["desktop_profile_name"] is None
    assert client.get(ROOT + "/ui-pages").status_code == 200
    assert controller.calls == [] and forwarded == []


def test_removed_dictionary_page_and_api_are_absent_from_both_consoles(tmp_path):
    from aigis_ps.admin_app import UI_DIRECTORY
    from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS
    from aigis_ps.admin_registry import discover
    from test_admin_http import AUTH as LEGACY_AUTH, BASE as LEGACY_BASE, build_console

    catalog = discover(PREFLIGHT_DOMAINS)
    assert "admin_routes_translation" not in catalog.modules
    assert "translation" not in {route.name for route in catalog.routes}
    legacy = build_console(tmp_path, admin_catalog=catalog, ui_directory=UI_DIRECTORY)
    forwarded = []

    def forward(request):
        forwarded.append(request.url.path)
        response = legacy.client.request(request.method, request.url.path, content=request.content,
                                         headers={**LEGACY_AUTH, "Origin": LEGACY_BASE, "Content-Type": "application/json"})
        return httpx.Response(response.status_code, content=response.content,
                              headers={"Content-Type": "application/json"})

    controller = Controller(tmp_path)
    controller.running = True
    app = create_desktop_app(controller, port=PORT, token=TOKEN, transport=httpx.MockTransport(forward))
    paths = [("GET", "/translations"), ("GET", "/translations/export"),
             ("POST", "/translations/preview"), ("POST", "/translations/import")]
    with legacy.client, TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as desktop:
        for client, headers in ((legacy.client, LEGACY_AUTH),
                                (desktop, {"Authorization": f"Bearer {TOKEN}", "X-Aigis-Profile": "default"})):
            pages = client.get(ROOT + "/ui-pages", headers=headers)
            assert pages.status_code == 200
            assert "translations" not in {page["name"] for page in pages.json()["pages"]}
            assert client.get("/admin/static/pages/translations.js", headers=headers).status_code == 404
            for method, path in paths:
                response = client.request(method, ROOT + path, headers=headers,
                                          json={} if method == "POST" else None)
                assert response.status_code == 404
    assert forwarded == [ROOT + path for _, path in paths]
    assert not (tmp_path / "runtime/translations").exists()


@pytest.mark.parametrize(("path", "body", "expected"), [
    ("/desktop/service/start", {}, "start"), ("/desktop/service/stop", {}, "stop"),
    ("/desktop/game/start", {}, "game"), ("/desktop/profiles", {"name": "新存档"}, ("create", "新存档")),
    ("/desktop/profiles/select", {"profile_id": "default"}, ("select", "default")),
])
def test_desktop_actions_only_call_the_fixed_operation(desktop, path, body, expected):
    controller, forwarded, client = desktop
    assert client.post(ROOT + path, json=body).status_code == 200
    assert controller.calls == [expected] and not forwarded
    assert client.post(ROOT + path, json={**body, "command": "ignored"}).status_code == 422
    assert controller.calls == [expected]


def test_admin_forwarding_uses_internal_identity_and_fixed_origin(desktop):
    controller, forwarded, client = desktop
    controller.running = True
    response = client.post(ROOT + "/plans", json={"commands": []},
                           headers={"X-Aigis-Profile": "default", "Origin": f"http://127.0.0.1:{PORT}"})
    assert response.status_code == 200
    request = forwarded[0]
    assert str(request.url) == "http://127.0.0.1:18541/admin/api/v1/plans"
    assert request.headers["authorization"] == "Bearer internal-secret"
    assert request.headers["origin"] == "http://127.0.0.1:18541"
    assert request.content == b'{"commands":[]}'
    assert "internal-secret" not in response.text


def test_previous_profile_and_missing_scope_never_reach_the_new_service(desktop):
    controller, forwarded, client = desktop
    controller.running, controller.profile_id = True, "new-save"
    for headers, expected in (({}, "desktop_profile_required"),
                              ({"X-Aigis-Profile": "default"}, "desktop_profile_changed")):
        result = client.post(ROOT + "/commits", json={}, headers=headers)
        assert result.status_code == 409 and result.json()["error"]["code"] == expected
    assert not forwarded


@pytest.mark.parametrize("headers", [{"Authorization": "Bearer wrong"}, {"Host": "evil.test"},
                                      {"Origin": "https://evil.test"}, {"Sec-Fetch-Site": "cross-site"}])
def test_security_checks_precede_mutating_actions(desktop, headers):
    controller, forwarded, client = desktop
    assert client.post(ROOT + "/desktop/service/start", json={}, headers=headers).status_code in (400, 401, 403)
    assert not controller.calls and not forwarded


def test_duplicate_json_keys_and_body_overflow_cannot_start_a_worker(desktop):
    controller, forwarded, client = desktop
    headers = {"Content-Type": "application/json"}
    assert client.post(ROOT + "/desktop/profiles", content='{"name":"a","name":"b"}', headers=headers).status_code == 400
    assert client.post(ROOT + "/desktop/profiles", content=' ' * (1024 * 1024 + 1), headers=headers).status_code == 413
    assert not controller.calls and not forwarded


def test_live_session_is_decorated_without_exposing_worker_credentials(desktop):
    controller, forwarded, client = desktop
    controller.running = True
    result = client.get(ROOT + "/session").json()
    assert result["player_id"] == "player" and result["desktop_profile_id"] == "default"
    assert result["service_state"] == "running" and len(forwarded) == 1

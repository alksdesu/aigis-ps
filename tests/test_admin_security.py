import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from aigis_ps.admin_command import AdminError
from aigis_ps.admin_security import (ACCESS_SCHEMA, ADMIN_ROOT, API_ROOT, BODY_STATE, CONTENT_SECURITY_POLICY,
                                     DOCUMENT_STATE, MAX_BODY_BYTES, SECURITY_HEADERS, STATIC_ROOT,
                                     AdminSecurityMiddleware, access_path, console_url, decode_json, new_token,
                                     remove_access_file, require_bind_host, require_port, require_token,
                                     write_access_file)
from aigis_ps.local_io import MAX_JSON_DEPTH


PORT = 18104
GAME_PORT = 18103
TOKEN = "s" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LOOPBACK = f"http://127.0.0.1:{PORT}"


def guarded_app():
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.add_middleware(AdminSecurityMiddleware, token=TOKEN, port=PORT)

    @application.get(API_ROOT + "/ping")
    def ping():
        return {"ok": True}

    @application.post(API_ROOT + "/echo")
    def echo(request: Request):
        return {"document": getattr(request.state, DOCUMENT_STATE), "bytes": len(getattr(request.state, BODY_STATE))}

    @application.post(API_ROOT + "/raw")
    async def raw(request: Request):
        return {"body": (await request.body()).decode("utf-8")}

    @application.get(API_ROOT + "/boom")
    def boom():
        raise RuntimeError("unexpected")

    @application.get(ADMIN_ROOT + "/")
    def console():
        return {"page": "index"}

    @application.get(STATIC_ROOT + "/app.js")
    def script():
        return {"page": "script"}

    return application


@pytest.fixture
def client():
    return TestClient(guarded_app(), base_url=LOOPBACK)


def error_code(response):
    return response.json()["error"]["code"]


def assert_secured(response):
    for name, value in SECURITY_HEADERS:
        assert response.headers.get(name) == value
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["cache-control"] == "no-store"


def nested(depth):
    return "[" * depth + "]" * depth


def test_token_requests_pass_and_carry_security_headers(client):
    response = client.get(API_ROOT + "/ping", headers=AUTH)
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert_secured(response)


@pytest.mark.parametrize("headers", [{}, {"Authorization": f"Bearer {'x' * 43}"}, {"Authorization": f"Basic {TOKEN}"},
                                     {"Authorization": TOKEN}, {"Authorization": f"Bearer  {TOKEN}"}])
def test_missing_or_wrong_tokens_are_unauthorized(client, headers):
    response = client.get(API_ROOT + "/ping", headers=headers)
    assert response.status_code == 401 and error_code(response) == "admin_unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"
    assert_secured(response)


def test_bearer_scheme_is_case_insensitive(client):
    assert client.get(API_ROOT + "/ping", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_repeated_authorization_headers_are_refused(client):
    response = client.get(API_ROOT + "/ping", headers=[("authorization", f"Bearer {TOKEN}"), ("authorization", "Bearer x")])
    assert response.status_code == 400 and error_code(response) == "admin_header_repeated"


def test_static_console_needs_no_token_but_other_paths_do(client):
    for path in (ADMIN_ROOT + "/", STATIC_ROOT + "/app.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert_secured(response)
    assert client.get("/elsewhere").status_code == 401
    assert client.post(STATIC_ROOT + "/app.js", json={}).status_code == 401


@pytest.mark.parametrize("host", ["evil.example:18104", f"127.0.0.1:{GAME_PORT}", "127.0.0.1", f"localhost.:{PORT}",
                                  f"127.0.0.2:{PORT}", f"0.0.0.0:{PORT}"])
def test_foreign_hosts_are_refused(client, host):
    response = client.get(API_ROOT + "/ping", headers={**AUTH, "host": host})
    assert response.status_code == 400 and error_code(response) == "admin_host_invalid"
    assert_secured(response)


@pytest.mark.parametrize("host", [f"localhost:{PORT}", f"LOCALHOST:{PORT}", f"[::1]:{PORT}"])
def test_every_loopback_name_of_the_admin_port_is_accepted(client, host):
    assert client.get(API_ROOT + "/ping", headers={**AUTH, "host": host}).status_code == 200


@pytest.mark.parametrize("origin", [f"http://127.0.0.1:{GAME_PORT}", f"http://localhost:{PORT}", "null",
                                    f"https://127.0.0.1:{PORT}", "http://evil.example"])
def test_other_origins_are_refused(client, origin):
    response = client.post(API_ROOT + "/echo", json={}, headers={**AUTH, "origin": origin})
    assert response.status_code == 403 and error_code(response) == "admin_origin_denied"


def test_the_own_origin_follows_the_validated_host(client):
    assert client.post(API_ROOT + "/echo", json={}, headers={**AUTH, "origin": LOOPBACK}).status_code == 200
    response = client.post(API_ROOT + "/echo", json={},
                           headers={**AUTH, "host": f"localhost:{PORT}", "origin": f"http://localhost:{PORT}"})
    assert response.status_code == 200


def test_a_browser_shaped_console_write_passes_with_its_real_origin(client):
    # The console fetches with referrerPolicy same-origin, so a spec-following browser sends this exact shape.
    headers = {**AUTH, "origin": LOOPBACK, "sec-fetch-site": "same-origin", "sec-fetch-mode": "same-origin",
               "content-type": "application/json"}
    response = client.post(API_ROOT + "/echo", content=b'{"op": 1}', headers=headers)
    assert response.status_code == 200 and response.json() == {"document": {"op": 1}, "bytes": 9}


@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site", "unknown"])
def test_cross_site_fetches_are_refused(client, site):
    response = client.get(API_ROOT + "/ping", headers={**AUTH, "sec-fetch-site": site})
    assert response.status_code == 403 and error_code(response) == "admin_fetch_site_denied"


@pytest.mark.parametrize("headers", [{"sec-fetch-site": "same-origin"}, {"sec-fetch-site": "none"}, {}])
def test_same_origin_navigation_and_non_browser_clients_pass_with_a_token(client, headers):
    assert client.get(API_ROOT + "/ping", headers={**AUTH, **headers}).status_code == 200
    assert client.get(API_ROOT + "/ping", headers=headers).status_code == 401


@pytest.mark.parametrize("headers", [AUTH, {}])
def test_options_requests_are_never_answered(client, headers):
    response = client.options(API_ROOT + "/echo", headers=headers)
    assert response.status_code == 405 and error_code(response) == "admin_method_not_allowed"
    assert_secured(response)


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data",
                                          "application/json; charset=latin-1", "application/jsonx", "application/json;"])
def test_writes_accept_only_json(client, content_type):
    response = client.post(API_ROOT + "/echo", content=b"{}", headers={**AUTH, "content-type": content_type})
    assert response.status_code == 415 and error_code(response) == "admin_content_type_unsupported"


def test_writes_without_a_content_type_are_unsupported(client):
    response = client.post(API_ROOT + "/echo", content=b"{}", headers=AUTH)
    assert response.status_code == 415


def test_json_with_utf8_charset_is_accepted(client):
    response = client.post(API_ROOT + "/echo", content='{"a":"值"}'.encode("utf-8"),
                           headers={**AUTH, "content-type": "Application/JSON; Charset=\"UTF-8\""})
    assert response.status_code == 200 and response.json() == {"document": {"a": "值"}, "bytes": 11}


def test_routes_still_read_the_checked_body(client):
    response = client.post(API_ROOT + "/raw", json={"value": 1}, headers=AUTH)
    assert response.status_code == 200 and json.loads(response.json()["body"]) == {"value": 1}


def test_declared_oversized_bodies_are_refused(client):
    body = b'{"a":"' + b"x" * MAX_BODY_BYTES + b'"}'
    response = client.post(API_ROOT + "/echo", content=body, headers={**AUTH, "content-type": "application/json"})
    assert response.status_code == 413 and error_code(response) == "admin_body_too_large"


@pytest.mark.parametrize("declared, status", [("9" * 5000, 413), ("0" * 5000 + "2", 200), ("0" + str(MAX_BODY_BYTES), 200),
                                              (str(MAX_BODY_BYTES + 1), 413)])
def test_declared_lengths_of_any_digit_count_are_checked(client, declared, status):
    response = client.post(API_ROOT + "/echo", content=b"{}",
                           headers={**AUTH, "content-type": "application/json", "content-length": declared})
    assert response.status_code == status
    assert_secured(response)


def test_streamed_oversized_bodies_are_refused(client):
    def chunks():
        yield b'{"a":"'
        for _ in range(MAX_BODY_BYTES // 4096 + 1):
            yield b"x" * 4096
        yield b'"}'

    response = client.post(API_ROOT + "/echo", content=chunks(), headers={**AUTH, "content-type": "application/json"})
    assert response.status_code == 413 and error_code(response) == "admin_body_too_large"


def test_bodies_at_the_limit_are_accepted(client):
    padding = MAX_BODY_BYTES - len(b'{"a":""}')
    body = b'{"a":"' + b"x" * padding + b'"}'
    response = client.post(API_ROOT + "/echo", content=body, headers={**AUTH, "content-type": "application/json"})
    assert response.status_code == 200 and response.json()["bytes"] == MAX_BODY_BYTES


@pytest.mark.parametrize("body, code", [
    pytest.param(b'{"a":1,"a":2}', "admin_json_duplicate_key", id="duplicate-key"),
    pytest.param(b'{"outer":{"b":1,"b":1}}', "admin_json_duplicate_key", id="nested-duplicate-key"),
    pytest.param(nested(1000).encode(), "admin_json_too_deep", id="nested-1000"),
    pytest.param(nested(200_000).encode(), "admin_json_too_deep", id="nested-past-recursion-limit"),
    pytest.param(nested(MAX_JSON_DEPTH + 1).encode(), "admin_json_too_deep", id="one-past-depth-limit"),
    pytest.param(b'{"a":NaN}', "admin_json_invalid", id="nan"),
    pytest.param(b'{"a":Infinity}', "admin_json_invalid", id="infinity"),
    pytest.param(b'{"a":1e999}', "admin_json_invalid", id="overflowing-float"),
    pytest.param(b"", "admin_json_invalid", id="empty"),
    pytest.param(b"{", "admin_json_invalid", id="truncated"),
    pytest.param(b"\xef\xbb\xbf{}", "admin_json_invalid", id="byte-order-mark"),
    pytest.param(b'{"a":"\xff"}', "admin_json_invalid", id="invalid-utf8"),
])
def test_malformed_json_is_a_client_error(client, body, code):
    response = client.post(API_ROOT + "/echo", content=body, headers={**AUTH, "content-type": "application/json"})
    assert response.status_code == 400 and error_code(response) == code
    assert_secured(response)


def test_json_at_the_depth_limit_is_accepted(client):
    response = client.post(API_ROOT + "/echo", content=nested(MAX_JSON_DEPTH).encode(),
                           headers={**AUTH, "content-type": "application/json"})
    assert response.status_code == 200


def test_unexpected_failures_answer_with_the_admin_error_shape(client):
    response = client.get(API_ROOT + "/boom", headers=AUTH)
    assert response.status_code == 500 and response.json() == {
        "error": {"code": "internal_error", "message": "The admin request failed"}}
    assert_secured(response)


def test_decode_json_reports_each_problem():
    assert decode_json('{"a":[1,{"b":null}],"c":1.5}') == {"a": [1, {"b": None}], "c": 1.5}
    for raw, code in (('{"a":1,"a":1}', "admin_json_duplicate_key"), (nested(MAX_JSON_DEPTH + 1), "admin_json_too_deep"),
                      ("[1e999]", "admin_json_invalid"), ("-Infinity", "admin_json_invalid"), ("[1,]", "admin_json_invalid")):
        with pytest.raises(AdminError) as caught:
            decode_json(raw)
        assert caught.value.code == code and caught.value.status == 400


def test_tokens_are_random_url_safe_text():
    first, second = new_token(), new_token()
    assert first != second and require_token(first) == first and len(first) >= 43
    for invalid in ("short", "x" * 31, "has space" + "x" * 32, "+" * 40, 1234, None):
        with pytest.raises(ValueError):
            require_token(invalid)


def test_bind_hosts_and_ports_are_validated():
    assert require_bind_host("127.0.0.1") == "127.0.0.1" and require_bind_host("::1") == "::1"
    for invalid in ("0.0.0.0", "localhost", "::", "", None):
        with pytest.raises(ValueError):
            require_bind_host(invalid)
    assert require_port(PORT) == PORT
    for invalid in (0, 65536, True, "18104", -1):
        with pytest.raises(ValueError):
            require_port(invalid)


def test_console_url_keeps_the_token_in_the_fragment():
    assert console_url("127.0.0.1", PORT, TOKEN) == f"http://127.0.0.1:{PORT}/admin/#token={TOKEN}"
    assert console_url("::1", PORT, TOKEN) == f"http://[::1]:{PORT}/admin/#token={TOKEN}"


def test_access_file_is_written_inside_the_data_directory(tmp_path):
    path = write_access_file(tmp_path, token=TOKEN, host="127.0.0.1", port=PORT)
    assert path == access_path(tmp_path) == tmp_path / "admin" / "access.json"
    raw = path.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    document = json.loads(raw)
    assert document["schema_version"] == ACCESS_SCHEMA and document["token"] == TOKEN
    assert document["url"] == console_url("127.0.0.1", PORT, TOKEN) and document["admin_port"] == PORT
    assert document["admin_host"] == "127.0.0.1" and isinstance(document["pid"], int)
    assert [item.name for item in path.parent.iterdir()] == ["access.json"]


def test_access_file_is_removed_only_by_its_owner(tmp_path):
    other = "o" * 43
    write_access_file(tmp_path, token=TOKEN, host="127.0.0.1", port=PORT)
    write_access_file(tmp_path, token=other, host="::1", port=PORT)
    assert remove_access_file(tmp_path, TOKEN) is False and access_path(tmp_path).exists()
    assert remove_access_file(tmp_path, other) is True and not access_path(tmp_path).exists()
    assert remove_access_file(tmp_path, other) is False


def test_access_file_rejects_invalid_settings(tmp_path):
    for options in ({"token": "short", "host": "127.0.0.1", "port": PORT},
                    {"token": TOKEN, "host": "0.0.0.0", "port": PORT},
                    {"token": TOKEN, "host": "127.0.0.1", "port": 0}):
        with pytest.raises(ValueError):
            write_access_file(tmp_path, **options)
    assert not access_path(tmp_path).parent.exists()


def test_middleware_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        AdminSecurityMiddleware(FastAPI(), token="short", port=PORT)
    with pytest.raises(ValueError):
        AdminSecurityMiddleware(FastAPI(), token=TOKEN, port=0)

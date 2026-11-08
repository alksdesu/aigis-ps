from datetime import datetime
import logging

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.bootstrap import BootstrapService, LocalRules
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import ProtocolRegistry, decode_wire, encode_response, encode_wire, prepare_request
from aigis_ps.request_fields import MAX_ROWS, MAX_STRING_BYTES
from aigis_ps.session_stub_service import (
    CHEAT_REPORT, DMM_POINT, FATAL_CHEAT_REPORT, LOGGED_CHEAT_ENTRIES, LOGGED_CHEAT_TEXT_BYTES, OPERATIONS,
    PREMIUM_RECEIVER, SCHEME_URLS, STARTUP_LOADS, TOKEN_EXTENSION, SessionStubService)
from aigis_ps.startup_diagnostics import StartupLoad
from aigis_ps.storage import Store
from test_bootstrap import asset_root


REGISTRY = ProtocolRegistry.load()
LAUNCH_TOKEN = LocalRules().bootstrap_token
FIELD_ERROR = ("invalid_request_fields", 400)
PLATFORM_STUBS = (TOKEN_EXTENSION, DMM_POINT)
ACCOUNT_STUBS = tuple(number for number in OPERATIONS if number not in PLATFORM_STUBS)
CHEAT_REPORTS = (CHEAT_REPORT, FATAL_CHEAT_REPORT)
COMMON_HEADER = ("UI", "UK", "MK", "CT", "PFN", "PlatformCode", "GPVI", "GPOT")
EMPTY_SCHEME = Container("SCHEME", (Column("Url", "S", ()),))
ZERO_POINT = Container("default", (Column("Point", "S", ("0",)),))


@pytest.fixture
def bootstrap(asset_root):
    return BootstrapService(asset_root, clock=lambda: datetime(2026, 10, 31, 12, 30), asset_version="a" * 40)


@pytest.fixture
def store(tmp_path, bootstrap):
    return Store(tmp_path / "stubs.sqlite3", bootstrap.initial_state())


@pytest.fixture
def service(bootstrap):
    return SessionStubService(bootstrap)


def envelope(store, number, *business, rewrite=None, **header):
    wire = REGISTRY.by_registration[number]
    player = store.profile()
    common = {"UI": player.player_id, "UK": player.user_key, "MK": player.method_key, "CT": 1, "PFN": "dmm-r18",
              "PlatformCode": 1, "GPVI": player.player_id, "GPOT": LAUNCH_TOKEN, **header}
    columns = tuple(Column(name, "I" if type(value) is int else "S", (value,))
                    for name, value in common.items() if value is not None)
    xml = dumps(Container(children=columns + business))
    xml = prepare_request(xml if rewrite is None else rewrite(xml), wire.st)
    return REGISTRY.decode(wire.route, encode_wire(xml, wire.request_key))


def platform(value):
    return Column("Platform", "I", (value,))


def receiver(value):
    return Column("C6", "I", (value,))


def launch(store, viewer=None, token=LAUNCH_TOKEN):
    return (Column("VI", "S", (store.profile().player_id if viewer is None else viewer,)), Column("OT", "S", (token,)))


def report(keys=(5,), texts=("Ori[ 1 ] Aft[ 2 ]",)):
    return Container("CH", (Column("HK", "I", tuple(keys)), Column("LG", "S", tuple(texts))))


def accepted_business(store, number):
    return {SCHEME_URLS: (platform(0x101),), PREMIUM_RECEIVER: (receiver(0),), CHEAT_REPORT: (report(),),
            FATAL_CHEAT_REPORT: (report(),), TOKEN_EXTENSION: launch(store), DMM_POINT: launch(store)}[number]


def served(service, store, request):
    before = store.profile()
    xml = service.handlers()[request.profile.registration_keys[0]](request, store)
    wire, _ = encode_response(request.profile, xml)
    assert decode_wire(wire, request.profile.response_key) == xml
    assert store.profile() == before
    return loads(xml).children


def refused(service, store, request):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.handlers()[request.profile.registration_keys[0]](request, store)
    assert store.profile() == before
    return error.value.code, error.value.status


def test_each_stub_has_its_own_registered_wire(service):
    handlers = service.handlers()
    assert sorted(handlers) == sorted(OPERATIONS)
    for number in OPERATIONS:
        profile = REGISTRY.by_registration[number]
        assert profile.registration_keys == (number,) and profile.name == "registered"
    assert handlers[CHEAT_REPORT] == handlers[FATAL_CHEAT_REPORT]


def test_startup_chain_stubs_are_recorded_loads(bootstrap, service):
    handlers = service.handlers()
    for number in STARTUP_LOADS:
        assert isinstance(handlers[number], StartupLoad)
        assert handlers[number].diagnostics is bootstrap.startup_diagnostics
        assert handlers[number].operation == str(number)
        assert service.handlers()[number] is handlers[number]
    assert not any(isinstance(handlers[number], StartupLoad) for number in set(OPERATIONS) - set(STARTUP_LOADS))


def test_status_names_every_local_rule(service):
    status = service.status()
    assert status["implemented_operations"] == [45, 102, 103, 146, 153, 155]
    assert status["startup_loads"] == [45, 146]
    assert status["state_writes"] is False and status["native_tested"] is False
    assert status["cheat_reports"]["request_limits"] == {"entries": MAX_ROWS, "text_bytes": MAX_STRING_BYTES}
    assert status["cheat_reports"]["log_limits"] == {"entries": LOGGED_CHEAT_ENTRIES, "text_bytes": LOGGED_CHEAT_TEXT_BYTES}


def test_scheme_urls_reply_system_and_an_empty_url_table(bootstrap, service, store):
    assert served(service, store, envelope(store, SCHEME_URLS, platform(0x101))) == (
        bootstrap.system_container(store.profile()), EMPTY_SCHEME)


@pytest.mark.parametrize("code", [0, 1, 2, 0x7FFFFEFF])
def test_scheme_platform_repeats_the_common_platform_code_with_the_scheme_bit(service, store, code):
    assert served(service, store, envelope(store, SCHEME_URLS, platform(code | 0x100), PlatformCode=code))[1] == EMPTY_SCHEME
    assert refused(service, store, envelope(store, SCHEME_URLS, platform(code), PlatformCode=code)) == FIELD_ERROR


@pytest.mark.parametrize("business", [
    (), (Column("Platform", "S", ("257",)),), (Column("Platform", "I", (257, 257)),),
    (platform(257), Column("Extra", "I", (1,))),
], ids=["missing", "string", "two_values", "unknown_field"])
def test_scheme_request_shape_is_strict(service, store, business):
    assert refused(service, store, envelope(store, SCHEME_URLS, *business)) == FIELD_ERROR


def test_premium_receiver_accepts_the_local_receiver(bootstrap, service, store):
    assert served(service, store, envelope(store, PREMIUM_RECEIVER, receiver(0))) == (bootstrap.system_container(store.profile()),)


@pytest.mark.parametrize("value", [1, 2, -1, 0x7FFFFFFF])
def test_premium_receiver_refuses_other_receivers_on_the_startup_record(bootstrap, service, store, value):
    assert refused(service, store, envelope(store, PREMIUM_RECEIVER, receiver(value))) == ("dmm_premium_receiver_unsupported", 501)
    load = bootstrap.status()["startup_diagnostics"]["loads"][str(PREMIUM_RECEIVER)]
    assert load["outcome"] == "refused" and [issue["code"] for issue in load["issues"]] == ["dmm_premium_receiver_unsupported"]
    served(service, store, envelope(store, PREMIUM_RECEIVER, receiver(0)))
    assert str(PREMIUM_RECEIVER) not in bootstrap.status()["startup_diagnostics"]["loads"]


@pytest.mark.parametrize("business", [
    (), (Column("C6", "S", ("0",)),), (Column("C6", "I", (0, 0)),), (receiver(0), Column("Extra", "I", (1,))),
], ids=["missing", "string", "two_values", "unknown_field"])
def test_premium_receiver_request_shape_is_strict(service, store, business):
    assert refused(service, store, envelope(store, PREMIUM_RECEIVER, *business)) == FIELD_ERROR


def test_premium_receiver_checks_the_account_before_the_receiver(service, store):
    assert refused(service, store, envelope(store, PREMIUM_RECEIVER, receiver(1), UK="c" * 32)) == ("invalid_identity", 403)


@pytest.mark.parametrize("number", ACCOUNT_STUBS)
@pytest.mark.parametrize("header,expected", [
    ({"UI": "2"}, ("invalid_identity", 403)), ({"UK": "c" * 32}, ("invalid_identity", 403)),
    ({"MK": "c" * 32}, ("invalid_method_key", 403)), ({"UI": "", "UK": "", "MK": ""}, ("invalid_identity", 403)),
], ids=["player", "user_key", "method_key", "empty_game_identity"])
def test_account_stubs_require_the_signed_in_account(service, store, number, header, expected):
    assert refused(service, store, envelope(store, number, *accepted_business(store, number), **header)) == expected


@pytest.mark.parametrize("number", OPERATIONS)
@pytest.mark.parametrize("name", COMMON_HEADER)
def test_every_stub_requires_the_whole_common_header(service, store, number, name):
    assert refused(service, store, envelope(store, number, *accepted_business(store, number), **{name: None})) == FIELD_ERROR


@pytest.mark.parametrize("number", PLATFORM_STUBS)
def test_platform_stubs_reply_after_the_launch_identity(bootstrap, service, store, number):
    groups = served(service, store, envelope(store, number, *launch(store)))
    assert groups == (bootstrap.system_container(store.profile()), *((ZERO_POINT,) if number == DMM_POINT else ()))
    assert served(service, store, envelope(store, number, *launch(store), GPVI="unkown", GPOT="unkown")) == groups


@pytest.mark.parametrize("number", PLATFORM_STUBS)
@pytest.mark.parametrize("header", [{"UI": "", "UK": "", "MK": ""}, {"UI": "", "UK": ""}],
                         ids=["empty_game_identity", "title_method_key"])
def test_platform_stubs_answer_before_the_game_identity_is_copied(bootstrap, service, store, number, header):
    groups = served(service, store, envelope(store, number, *launch(store), **header))
    assert groups == (bootstrap.system_container(store.profile()), *((ZERO_POINT,) if number == DMM_POINT else ()))


@pytest.mark.parametrize("number", PLATFORM_STUBS)
@pytest.mark.parametrize("viewer,token,header", [
    ("2", LAUNCH_TOKEN, {}), (None, "other", {}), (None, LAUNCH_TOKEN, {"GPVI": "2"}),
    (None, LAUNCH_TOKEN, {"GPOT": "other"}), (None, LAUNCH_TOKEN, {"GPVI": ""}),
], ids=["viewer", "token", "viewer_alias", "token_alias", "empty_alias"])
def test_platform_stubs_share_the_local_launch_identity_of_152(service, store, number, viewer, token, header):
    request = envelope(store, number, *launch(store, viewer, token), **header)
    assert refused(service, store, request) == ("invalid_platform_identity", 403)


@pytest.mark.parametrize("number", PLATFORM_STUBS)
def test_platform_stubs_require_both_launch_fields(service, store, number):
    viewer, token = launch(store)
    for business in ((viewer,), (token,), ()):
        assert refused(service, store, envelope(store, number, *business)) == FIELD_ERROR
    assert refused(service, store, envelope(store, number, viewer, token, Column("Extra", "I", (1,)))) == FIELD_ERROR
    integer_viewer = Column("VI", "I", (int(store.profile().player_id),))
    assert refused(service, store, envelope(store, number, integer_viewer, token)) == ("invalid_identity", 400)


@pytest.mark.parametrize("number,level", [(CHEAT_REPORT, logging.WARNING), (FATAL_CHEAT_REPORT, logging.ERROR)])
def test_cheat_reports_are_logged_without_account_action(bootstrap, service, store, caplog, number, level):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    request = envelope(store, number, report((5, -0x80000000), ("Ori[ 1 ] Aft[ 2 ]", "NoMsg Ori[ 3 ] Aft[ 4 ]")))
    assert served(service, store, request) == (bootstrap.system_container(store.profile()),)
    (record,) = caplog.records
    assert record.levelno == level
    message = record.getMessage()
    assert message.startswith(f"Cheat check {number} listed 2 detection(s); the native client ")
    assert message.endswith(": 5='Ori[ 1 ] Aft[ 2 ]'; -2147483648='NoMsg Ori[ 3 ] Aft[ 4 ]'")


def test_cheat_report_log_is_bounded_and_escaped(service, store, caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    long_text = "界" * 200
    count = LOGGED_CHEAT_ENTRIES + 3
    texts = ("line\nbreak", *(long_text,) * (count - 1))
    served(service, store, envelope(store, CHEAT_REPORT, report(range(count), texts)))
    message = caplog.records[0].getMessage()
    assert "\n" not in message and "0='line\\nbreak'" in message
    assert long_text not in message
    shown = repr("界" * (LOGGED_CHEAT_TEXT_BYTES // 3))
    assert message.count(f"{shown}... ({len(long_text.encode('utf-8'))} bytes)") == LOGGED_CHEAT_ENTRIES - 1
    assert message.endswith("; 3 more")


@pytest.mark.parametrize("keys", [(0, 1, 0x7FFFFFFF, -0x80000000, -1), (-5,), (7, -7)])
def test_cheat_report_keys_follow_the_native_unsigned_order(service, store, keys):
    served(service, store, envelope(store, CHEAT_REPORT, report(keys, ("x",) * len(keys))))


def without_cheat_container(xml):
    return xml.replace(b"<CH>", b"").replace(b"</CH>", b"")


@pytest.mark.parametrize("business,rewrite", [
    ((report((1, 2), ("a",)),), None),
    ((report((1, 1), ("a", "b")),), None),
    ((report((-1, 5), ("a", "b")),), None),
    ((report((), ()),), None),
    ((), None),
    ((Container("CH", (Column("HK", "I", (1,)), Column("LG", "S", ("a",)), Column("RP", "S", ("x",)))),), None),
    ((Container("CH", (Column("HK", "S", ("1",)), Column("LG", "S", ("a",)))),), None),
    ((Container("CH", (Column("HK", "I", (1,)), Column("LG", "I", (1,)))),), None),
    ((Container("CH", (Column("HK", "I", (1,)),)),), None),
    ((Column("CH", "S", ("x",)),), None),
    ((report(), report()), None),
    ((report(), Column("KC", "S", ("x",))), None),
    ((report(),), without_cheat_container),
    ((report(),), lambda xml: xml.replace(b"<CH>", b'<CH N="CH">')),
    ((report(),), lambda xml: xml.replace(b"<CH>", b"<CH>text")),
    ((report(range(MAX_ROWS + 1), ("x",) * (MAX_ROWS + 1)),), None),
    ((report((1,), ("x" * (MAX_STRING_BYTES + 1),)),), None),
], ids=["unpaired", "duplicate_key", "signed_order", "empty", "missing", "unknown_report_field", "string_key",
        "integer_text", "missing_text", "typed_container", "two_reports", "unknown_field", "flattened",
        "container_attribute", "container_text", "too_many_entries", "text_too_long"])
def test_cheat_report_shape_is_strict_and_unlogged(service, store, caplog, business, rewrite):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    for number in CHEAT_REPORTS:
        assert refused(service, store, envelope(store, number, *business, rewrite=rewrite)) == FIELD_ERROR
    assert caplog.records == []


def test_refused_cheat_report_identity_is_not_logged(service, store, caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    assert refused(service, store, envelope(store, FATAL_CHEAT_REPORT, report(), UK="c" * 32)) == ("invalid_identity", 403)
    assert caplog.records == []


def test_stubs_answer_over_http_and_leave_the_unimplemented_list(tmp_path, bootstrap):
    stubs = SessionStubService(bootstrap)
    app = create_app(tmp_path / "http", handlers=bootstrap.handlers() | stubs.handlers(), initial_state=bootstrap.initial_state())
    store = app.state.store
    before = store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        def post(request):
            profile = request.profile
            return client.post("/api/" + profile.route, content=encode_wire(request.xml, profile.request_key))

        for number in OPERATIONS:
            request = envelope(store, number, *accepted_business(store, number))
            response = post(request)
            assert response.status_code == 200, (number, response.text)
            groups = loads(decode_wire(response.content, request.profile.response_key)).children
            assert groups[0] == bootstrap.system_container(store.profile())
        response = post(envelope(store, PREMIUM_RECEIVER, receiver(2)))
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "dmm_premium_receiver_unsupported"
        status = client.get("/local/status").json()
    assert not set(OPERATIONS) & set(status["unimplemented_operations"])
    assert store.profile() == before

from datetime import datetime
import json

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import decode_wire, encode_response, encode_wire
from aigis_ps.schedule_capture import sanitize_schedule_response
from aigis_ps.storage import Store
from aigis_ps.summon_schedule import OPERATIONS, PACKAGES, SCHEDULE, UNIMPLEMENTED_SUMMONS, SummonScheduleService
from test_bootstrap import asset_root
from test_session_stub_service import COMMON_HEADER, FIELD_ERROR, REGISTRY, envelope


NOW = datetime(2026, 11, 5, 12, 30)
# Native A5DAB0 (70) and A5E080 (71) register these receivers after SYSTEM, in this order.
NATIVE_GROUPS = {70: ("CSS", "FPR", "SH", "GacyaStatus", "LCC", "PLC"), 71: ("CSP",)}
OTHER_WIRE = {SCHEDULE: PACKAGES, PACKAGES: SCHEDULE}
ZERO_ROWS = {"rows": 0, "row_counts": [], "field_types": {}}
WRONG_USER_KEY = {"UK": "c" * 32}


@pytest.fixture
def clock_reads():
    return []


@pytest.fixture
def bootstrap(asset_root, clock_reads):
    def clock():
        clock_reads.append(NOW)
        return NOW

    return BootstrapService(asset_root, clock=clock, asset_version="a" * 40)


@pytest.fixture
def store(tmp_path, bootstrap):
    return Store(tmp_path / "schedule.sqlite3", bootstrap.initial_state())


@pytest.fixture
def service(bootstrap):
    return SummonScheduleService(bootstrap)


def expected_groups(bootstrap, store, number):
    return (bootstrap.system_container(store.profile()), *(Container(name) for name in NATIVE_GROUPS[number]))


def served(service, store, request):
    before = store.profile()
    xml = service.handlers()[request.profile.registration_keys[0]](request, store)
    wire, _ = encode_response(request.profile, xml)
    assert decode_wire(wire, request.profile.response_key) == xml
    assert store.profile() == before
    return xml


def refused(service, store, number, request):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.handlers()[number](request, store)
    assert store.profile() == before
    return error.value.code, error.value.status


def test_each_operation_answers_on_its_own_registered_wire(service):
    assert sorted(service.handlers()) == sorted(OPERATIONS) == sorted(NATIVE_GROUPS)
    for number in OPERATIONS:
        profile = REGISTRY.by_registration[number]
        assert profile.registration_keys == (number,) and profile.name == "registered"


@pytest.mark.parametrize("number", OPERATIONS)
def test_reply_is_system_and_the_native_groups_without_rows(bootstrap, service, store, number):
    groups = loads(served(service, store, envelope(store, number))).children
    assert groups == expected_groups(bootstrap, store, number)
    assert all(group.children == () for group in groups[1:])


@pytest.mark.parametrize("number", OPERATIONS)
def test_empty_groups_are_written_without_columns(service, store, number):
    xml = served(service, store, envelope(store, number))
    for name in NATIVE_GROUPS[number]:
        assert f"<{name}></{name}>".encode() in xml
    assert b"/>" not in xml


@pytest.mark.parametrize("number", OPERATIONS)
def test_reply_reads_as_a_complete_zero_row_schedule(service, store, number):
    _, metadata = sanitize_schedule_response(number, served(service, store, envelope(store, number)))
    assert metadata["kept_groups"] == list(NATIVE_GROUPS[number]) and metadata["missing_groups"] == []
    assert metadata["removed_columns"] == []
    assert metadata["groups"] == {name: ZERO_ROWS for name in NATIVE_GROUPS[number]}


def test_replies_keep_the_save_and_never_read_the_clock(service, store, clock_reads):
    before = store.profile()
    reads = len(clock_reads)
    first = {number: served(service, store, envelope(store, number)) for number in OPERATIONS}
    assert {number: served(service, store, envelope(store, number)) for number in OPERATIONS} == first
    assert len(clock_reads) == reads
    assert store.profile() == before and store.profile().revision == before.revision


def test_status_reports_the_explicit_empty_schedule(service):
    status = service.status()
    assert status["ready"] is True and status["implemented_operations"] == [SCHEDULE, PACKAGES]
    assert status["state_writes"] is False and status["native_tested"] is False
    assert status["groups"] == {str(number): list(groups) for number, groups in NATIVE_GROUPS.items()}
    assert status["unimplemented_summons"] == [11, 12, 13]
    assert not set(UNIMPLEMENTED_SUMMONS) & set(service.handlers())
    assert isinstance(status["policy"], str) and status["policy"]
    # Readiness reaches doctor and /local/status as JSON, which turns integer keys into strings.
    assert json.loads(json.dumps(status)) == status


@pytest.mark.parametrize("number", OPERATIONS)
@pytest.mark.parametrize("business", [
    (Column("SC", "I", (1,)),), (Column("Extra", "S", ("x",)),), (Container("CSS"),), (Container("CSP"),),
], ids=["summon_lineup", "unknown_column", "schedule_group", "package_group"])
def test_requests_carry_no_business_fields(service, store, number, business):
    assert refused(service, store, number, envelope(store, number, *business)) == FIELD_ERROR


@pytest.mark.parametrize("number", OPERATIONS)
@pytest.mark.parametrize("name", COMMON_HEADER)
def test_requests_need_the_whole_common_header(service, store, number, name):
    assert refused(service, store, number, envelope(store, number, **{name: None})) == FIELD_ERROR


@pytest.mark.parametrize("number", OPERATIONS)
@pytest.mark.parametrize("header,expected", [
    ({"UI": "2"}, ("invalid_identity", 403)), (WRONG_USER_KEY, ("invalid_identity", 403)),
    ({"MK": "c" * 32}, ("invalid_method_key", 403)), ({"UI": "", "UK": "", "MK": ""}, ("invalid_identity", 403)),
], ids=["player", "user_key", "method_key", "empty_game_identity"])
def test_replies_require_the_signed_in_account(service, store, number, header, expected):
    assert refused(service, store, number, envelope(store, number, **header)) == expected


@pytest.mark.parametrize("number", OPERATIONS)
def test_request_shape_is_checked_before_the_account(service, store, number):
    request = envelope(store, number, Column("SC", "I", (1,)), **WRONG_USER_KEY)
    assert refused(service, store, number, request) == FIELD_ERROR


@pytest.mark.parametrize("number", OPERATIONS)
def test_each_handler_refuses_the_other_wire(service, store, number):
    assert refused(service, store, number, envelope(store, OTHER_WIRE[number])) == FIELD_ERROR


def test_schedule_answers_over_http_and_leaves_the_unimplemented_list(tmp_path, bootstrap):
    service = SummonScheduleService(bootstrap)
    app = create_app(tmp_path / "http", handlers=bootstrap.handlers() | service.handlers(),
                     initial_state=bootstrap.initial_state())
    store = app.state.store
    before = store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for number in OPERATIONS:
            request = envelope(store, number)
            profile = request.profile
            response = client.post("/api/" + profile.route, content=encode_wire(request.xml, profile.request_key))
            assert response.status_code == 200, (number, response.text)
            groups = loads(decode_wire(response.content, profile.response_key)).children
            assert groups == expected_groups(bootstrap, store, number)
        unimplemented = set(client.get("/local/status").json()["unimplemented_operations"])
    assert not set(OPERATIONS) & unimplemented
    assert set(UNIMPLEMENTED_SUMMONS) <= unimplemented
    assert store.profile() == before

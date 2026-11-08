from copy import deepcopy
from dataclasses import replace
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.errors import ProtocolError
from aigis_ps.native_commands import STATUS_LIMIT
from aigis_ps.protocol import ProtocolRegistry, decode_wire, encode_response, encode_wire, prepare_request
from aigis_ps.request_fields import MAX_ROWS
from aigis_ps.storage import Store
from aigis_ps.universal_flags import MAX_FLAG_NAME_BYTES, MAX_FLAGS, flag_hash, flags_response, read_flags, set_flags
from aigis_ps.universal_flags_catalog import HOME_TRIGGER, MISSION_PORTAL, SKIN_SHOP, UniversalFlagsCatalog
from aigis_ps.universal_flags_service import (
    MALFORMED_NAME, OPERATION, PROTECTED_NAME, REJECTIONS, STARTUP_OPERATION, UNLISTED_NAME, VALUE_MISMATCH,
    UniversalFlagsService)
from test_bootstrap import asset_root
from test_unit_actions import request
from test_universal_flags_catalog import COLLIDING, PAYMENT_ALIAS, current, home, plain_rows, portal, reader, resources


SCRIPT_TEXT = ('SetUniversalFlags("Flags=OpenUnitLibrarySystem","Value=1");\n'
               'SetUniversalFlags("Flags=Seven","Value=7");\nSaveUniversalFlags();')
PORTAL = "mission_portal_sp53_20230921"
HISTORY = "AppeardHistoryGroupFlag_3"
LISTED = ("OpenUnitLibrarySystem", PORTAL, "AppeardHistoryGroupFlag_1", HISTORY, "test_flag_002", "test_flag_011")
PURCHASE = "PurchasedSkin_0001"


def service_rows(**changes):
    rows = plain_rows(**{HOME_TRIGGER: [home(SCRIPT_TEXT)],
                         MISSION_PORTAL: [portal(4, "History", PORTAL), portal(5, "Event", COLLIDING[1])]})
    rows.update(changes)
    return rows


def flags_catalog(rows=None):
    return UniversalFlagsCatalog(reader(resources(service_rows() if rows is None else rows)))


def built(asset_root, path, catalog, provider=flags_response):
    bootstrap = BootstrapService(asset_root, universal_flag_provider=provider)
    return SimpleNamespace(service=UniversalFlagsService(bootstrap, catalog), bootstrap=bootstrap,
                           store=Store(path, bootstrap.initial_state()))


@pytest.fixture
def case(asset_root, tmp_path):
    bootstrap = BootstrapService(asset_root, universal_flag_provider=flags_response)
    state = bootstrap.initial_state()
    state["sentinel"] = {"preserved": [1]}
    return SimpleNamespace(service=UniversalFlagsService(bootstrap, flags_catalog()), bootstrap=bootstrap,
                           store=Store(tmp_path / "flags.sqlite3", state), directory=tmp_path)


def upload(store, rows=None, *, fields=None, platform=(1,), rewrite=None):
    if fields is None:
        fields = () if rows is None else (Column("ID", "S", tuple(name for name, _ in rows)),
                                          Column("VALUE", "I", tuple(value for _, value in rows)))
    registry = ProtocolRegistry.load()
    wire = registry.by_registration[OPERATION]
    player = store.profile()
    common = tuple(Column(name, kind, values) for name, kind, values in (
        ("UI", "S", (player.player_id,)), ("UK", "S", (player.user_key,)), ("MK", "S", (player.method_key,)),
        ("CT", "I", (1,)), ("PFN", "S", ("dmm-r18",)), ("PlatformCode", "I", platform),
        ("GPVI", "S", ("unkown",)), ("GPOT", "S", ("unkown",))))
    xml = dumps(Container(children=common + tuple(fields)))
    xml = prepare_request(xml if rewrite is None else rewrite(xml), wire.st)
    return registry.decode(wire.route, encode_wire(xml, wire.request_key))


def save(case, rows=None, **options):
    envelope = upload(case.store, rows, **options)
    response = case.service.save(envelope, case.store)
    encode_response(envelope.profile, response)
    return {node.name: node for node in loads(response).children}


def stored(case):
    return read_flags(case.store.profile().state)


def reloaded(case):
    envelope = request(case.store, 137, ())
    groups = {node.name: node for node in loads(case.bootstrap.handlers()[137](envelope, case.store)).children}
    return groups["GF"]


def gf(rows):
    return Container("GF", (Column("ID", "S", tuple(name for name, _ in rows)),
                            Column("Value", "I", tuple(value for _, value in rows))))


def seed(case, updates):
    profile = case.store.profile()
    case.store.update_state(set_flags(profile.state, updates), profile.revision)


def refused(case, envelope, code, status=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as error:
        case.service.save(envelope, case.store)
    assert error.value.code == code
    if status is not None:
        assert error.value.status == status
    assert case.store.profile() == before
    return error.value


def native_order(names):
    return sorted(names, key=flag_hash)


def test_listed_upload_is_stored_echoed_and_reloaded_through_137(case):
    before = case.store.profile()
    assert "universal_flags_schema" not in before.state and before.state["tables"]["universal_flags"] == []
    rows = [(name, 1) for name in native_order([PORTAL, HISTORY])]
    assert [name for name, _ in rows] == [PORTAL, HISTORY]
    groups = save(case, rows)
    assert list(groups) == ["SYSTEM", "GF"] and groups["GF"] == gf(rows)
    after = case.store.profile()
    assert after.revision == before.revision + 1 and after.state["universal_flags_schema"] == 1
    assert stored(case) == [{"ID": PORTAL, "Value": 1}, {"ID": HISTORY, "Value": 1}]
    assert reloaded(case) == gf(rows)
    assert after.state["sentinel"] == {"preserved": [1]}
    assert {key: value for key, value in after.state.items() if key not in ("tables", "universal_flags_schema")} == {
        key: value for key, value in before.state.items() if key != "tables"}


def test_repeated_upload_is_idempotent_and_keeps_the_revision(case):
    rows = [(name, 1) for name in LISTED]
    first = save(case, rows)
    committed = case.store.profile()
    assert save(case, rows) == first
    assert case.store.profile() == committed
    uploads = case.service.status()["uploads"]
    assert uploads["accepted"] == 2 * len(rows) and uploads["written"] == len(rows)
    assert sum(uploads["rejected"].values()) == 0


def test_nonzero_values_stay_and_zero_values_are_raised_in_place(case):
    seed(case, {"Legacy": -3, "test_flag_002": 5, "test_flag_011": 0, "Seven": -1})
    revision = case.store.profile().revision
    groups = save(case, [("test_flag_011", 1), ("test_flag_002", 1), ("Seven", 7)])
    assert groups["GF"] == gf([("test_flag_011", 1), ("test_flag_002", 5), ("Seven", -1)])
    assert stored(case) == [{"ID": "Legacy", "Value": -3}, {"ID": "test_flag_002", "Value": 5},
                            {"ID": "test_flag_011", "Value": 1}, {"ID": "Seven", "Value": -1}]
    assert case.store.profile().revision == revision + 1
    groups = save(case, [("test_flag_002", 1)])
    assert groups["GF"] == gf([("test_flag_002", 5)]) and case.store.profile().revision == revision + 1


def test_script_literal_values_are_the_only_accepted_values(case):
    groups = save(case, [("Seven", 1), ("OpenUnitLibrarySystem", 7), (PORTAL, 2), (HISTORY, -1)])
    assert list(groups) == ["SYSTEM"] and stored(case) == []
    groups = save(case, [("Seven", 7), ("OpenUnitLibrarySystem", 1)])
    assert groups["GF"] == gf([("Seven", 7), ("OpenUnitLibrarySystem", 1)])


def test_protected_names_never_change_through_this_upload(case):
    seed(case, {PURCHASE: 1})
    before = case.store.profile()
    for rows in ([(PURCHASE, 0)], [(PURCHASE, 1)], [("PurchasedSkin_0002", 1)], [("PurchasedSkin_9999", 1)],
                 [("PaymentBonusObtained:id=1", 1)], [("PaymentBonusObtained:id=7", 0)]):
        assert list(save(case, rows)) == ["SYSTEM"]
    assert case.store.profile() == before and stored(case) == [{"ID": PURCHASE, "Value": 1}]
    uploads = case.service.status()["uploads"]
    assert uploads["rejected"][PROTECTED_NAME] == 6 and uploads["accepted"] == uploads["written"] == 0


def test_protection_follows_native_hashes_for_other_spellings(asset_root, tmp_path):
    rows = service_rows(**{SKIN_SHOP: [{"ButtonImageId": 1, "PurchaseFlag": flag_hash(COLLIDING[0])}],
                           MISSION_PORTAL: [portal(4, "History", PORTAL)]})
    case = built(asset_root, tmp_path / "hashes.sqlite3", flags_catalog(rows))
    assert list(save(case, [(COLLIDING[1], 1), (PAYMENT_ALIAS[1], 1), (PORTAL, 1)])) == ["SYSTEM", "GF"]
    assert stored(case) == [{"ID": PORTAL, "Value": 1}]
    assert case.service.status()["uploads"]["rejected_names"][PROTECTED_NAME] == [COLLIDING[1], PAYMENT_ALIAS[1]]


def test_names_filling_the_native_cell_are_accepted(asset_root, tmp_path):
    names = ("x" * MAX_FLAG_NAME_BYTES, "界" * (MAX_FLAG_NAME_BYTES // 3))
    assert {len(name.encode("utf-8")) for name in names} == {MAX_FLAG_NAME_BYTES}
    script = "".join(f'SetUniversalFlags("Flags={name}","Value=1");' for name in names)
    case = built(asset_root, tmp_path / "cell.sqlite3", flags_catalog(service_rows(**{HOME_TRIGGER: [home(script)]})))
    rows = [(name, 1) for name in names]
    assert save(case, [*rows, (names[0] + "x", 1)])["GF"] == gf(rows)
    assert reloaded(case) == gf(rows)
    assert case.service.status()["uploads"]["rejected"][MALFORMED_NAME] == 1


def test_mixed_upload_commits_only_accepted_rows_in_request_order(case, caplog):
    rows = [("test_flag_011", 1), ("Unknown", 1), (PURCHASE, 1), ("", 1), (PORTAL, 1), ("Seven", 2),
            ("x" * (MAX_FLAG_NAME_BYTES + 1), 1), ("OpenUnitLibrarySystem", 1)]
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        groups = save(case, rows)
    accepted = [("test_flag_011", 1), (PORTAL, 1), ("OpenUnitLibrarySystem", 1)]
    assert groups["GF"] == gf(accepted)
    assert stored(case) == [{"ID": name, "Value": value} for name, value in accepted]
    assert reloaded(case) == gf(accepted)
    message = caplog.records[-1].getMessage()
    assert "kept 5 uploaded row(s)" in message and "unlisted_name 'Unknown'" in message
    assert f"protected_name '{PURCHASE}'" in message and "value_mismatch 'Seven'" in message
    assert f"malformed_name ({MAX_FLAG_NAME_BYTES + 1} bytes)" in message and "malformed_name (0 bytes)" in message
    uploads = case.service.status()["uploads"]
    assert uploads["accepted"] == uploads["written"] == 3
    assert uploads["rejected"] == {MALFORMED_NAME: 2, PROTECTED_NAME: 1, UNLISTED_NAME: 1, VALUE_MISMATCH: 1}
    assert uploads["rejected_names"] == {PROTECTED_NAME: [PURCHASE], UNLISTED_NAME: ["Unknown"], VALUE_MISMATCH: ["Seven"]}


def test_oversized_protected_names_stay_out_of_logs_and_status(case, caplog):
    name = PURCHASE + "x" * MAX_FLAG_NAME_BYTES
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        assert list(save(case, [(name, 1)])) == ["SYSTEM"]
    message = caplog.records[-1].getMessage()
    assert name not in message and f"malformed_name ({len(name)} bytes)" in message
    uploads = case.service.status()["uploads"]
    assert uploads["rejected"][MALFORMED_NAME] == 1 and uploads["rejected"][PROTECTED_NAME] == 0
    assert uploads["rejected_names"][PROTECTED_NAME] == []


def test_rejected_only_upload_answers_system_and_keeps_the_revision(case):
    before = case.store.profile()
    long_name = "界" * 1400
    groups = save(case, [("Unknown", 1), ("", 0), (long_name, 1), ("test_flag_002", 0)])
    assert list(groups) == ["SYSTEM"] and case.store.profile() == before
    assert case.service.status()["uploads"]["rejected"][MALFORMED_NAME] == 2


def test_upload_without_columns_is_a_native_no_op(case):
    before = case.store.profile()
    groups = save(case)
    assert list(groups) == ["SYSTEM"] and case.store.profile() == before
    assert groups["SYSTEM"] == loads(dumps(Container(children=(case.bootstrap._system(before),)))).children[0]


@pytest.mark.parametrize("fields", [
    (Column("ID", "S", ("test_flag_002",)),),
    (Column("VALUE", "I", (1,)),),
    (Column("ID", "S", ("test_flag_002", "test_flag_011")), Column("VALUE", "I", (1,))),
    (Column("ID", "I", (1,)), Column("VALUE", "I", (1,))),
    (Column("ID", "S", ("test_flag_002",)), Column("VALUE", "S", ("1",))),
    (Column("ID", "S", ("test_flag_002", "test_flag_002")), Column("VALUE", "I", (1, 1))),
    (Column("ID", "S", COLLIDING), Column("VALUE", "I", (1, 1))),
    (Column("ID", "S", ()), Column("VALUE", "I", ())),
    (Column("ID", "S", ("test_flag_002",)), Column("VALUE", "I", (1,)), Column("Extra", "I", (1,))),
    (Container("ID", (Column("V", "S", ("test_flag_002",)),)), Column("VALUE", "I", (1,))),
])
def test_shapes_the_native_sender_cannot_produce_are_rejected(case, fields):
    refused(case, upload(case.store, fields=fields), "invalid_request_fields", 400)


def test_out_of_range_and_oversized_batches_are_rejected(case):
    refused(case, upload(case.store, [(f"name_{index}", 1) for index in range(MAX_ROWS + 1)]), "invalid_request_fields")
    refused(case, upload(case.store, [("test_flag_002", 1)], platform=(1, 1)), "invalid_request_fields")

    def overflow(xml):
        changed = xml.replace(b"<V>2147483647</V></VALUE>", b"<V>2147483648</V></VALUE>")
        assert changed != xml
        return changed

    refused(case, upload(case.store, [("test_flag_002", 2147483647)], rewrite=overflow), "invalid_request_fields")


def test_full_native_batch_in_crc_order_is_accepted(case):
    rows = [(name, 7 if name == "Seven" else 1) for name in native_order([*LISTED, "Seven"])]
    groups = save(case, rows)
    assert groups["GF"] == gf(rows)
    assert {row["ID"] for row in stored(case)} == {*LISTED, "Seven"}


def test_legacy_names_and_order_are_preserved(case):
    seed(case, {"older": 2, "older_zero": 0, "older_negative": -5})
    save(case, [("test_flag_002", 1), ("older", 1)])
    assert stored(case) == [{"ID": "older", "Value": 2}, {"ID": "older_zero", "Value": 0},
                            {"ID": "older_negative", "Value": -5}, {"ID": "test_flag_002", "Value": 1}]


def test_stored_hash_conflict_keeps_the_save_intact(case, caplog):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state["universal_flags_schema"] = 1
    state["tables"]["universal_flags"] = [{"ID": COLLIDING[0], "Value": 1}]
    case.store.update_state(state, profile.revision)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        refused(case, upload(case.store, [(COLLIDING[1], 1)]), "universal_flag_name_conflict", 409)
    assert "Universal flags 138 refused with universal_flag_name_conflict" in caplog.records[-1].getMessage()


def test_corrupt_storage_is_refused_without_rewriting(case):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state["tables"]["universal_flags"] = [{"ID": "A", "Value": 1}]
    case.store.update_state(state, profile.revision)
    refused(case, upload(case.store, [("test_flag_002", 1)]), "universal_flags_invalid", 503)


def test_capacity_limit_is_refused_and_keeps_the_save(case):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state["universal_flags_schema"] = 1
    state["tables"]["universal_flags"] = [{"ID": f"f{index}", "Value": 1} for index in range(MAX_FLAGS)]
    case.store.update_state(state, profile.revision)
    refused(case, upload(case.store, [("test_flag_002", 1)]), "universal_flag_capacity", 409)
    assert list(save(case, [("f7", 1), ("Unknown", 1)])) == ["SYSTEM"]


def test_cas_retry_keeps_a_concurrent_purchase_flag(case, monkeypatch):
    original = case.store.update_state
    attempts = []

    def competing(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            current = case.store.profile()
            original(set_flags(current.state, {PURCHASE: 1}), current.revision)
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", competing)
    groups = save(case, [("test_flag_002", 1)])
    assert groups["GF"] == gf([("test_flag_002", 1)]) and len(attempts) == 2
    assert stored(case) == [{"ID": PURCHASE, "Value": 1}, {"ID": "test_flag_002", "Value": 1}]


def test_cas_retry_answers_a_value_set_by_a_concurrent_upload(case, monkeypatch):
    original = case.store.update_state
    attempts = []

    def competing(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            current = case.store.profile()
            original(set_flags(current.state, {"Seven": -2}), current.revision)
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", competing)
    groups = save(case, [("Seven", 7), ("test_flag_002", 1)])
    assert groups["GF"] == gf([("Seven", -2), ("test_flag_002", 1)])
    assert stored(case) == [{"ID": "Seven", "Value": -2}, {"ID": "test_flag_002", "Value": 1}]


def test_three_cas_failures_keep_the_save_and_log(case, monkeypatch, caplog):
    attempts = []

    def conflict(state, revision):
        attempts.append(revision)
        raise ValueError("fixture conflict")

    monkeypatch.setattr(case.store, "update_state", conflict)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        refused(case, upload(case.store, [("test_flag_002", 1)]), "profile_changed", 409)
    assert len(attempts) == 3
    assert "the native client shows a communication error and resends" in caplog.records[-1].getMessage()
    assert case.service.status()["uploads"]["accepted"] == 0


def test_wrong_identity_is_refused(case):
    envelope = upload(case.store, [("test_flag_002", 1)])
    other = Store(case.directory / "other.sqlite3", case.store.profile().state)
    with pytest.raises(ProtocolError) as error:
        case.service.save(envelope, other)
    assert error.value.code == "invalid_identity" and error.value.status == 403


def test_inconsistent_startup_provider_blocks_the_commit(asset_root, tmp_path):
    case = built(asset_root, tmp_path / "provider.sqlite3", flags_catalog(), provider=lambda state: Container("GF"))
    refused(case, upload(case.store, [("test_flag_002", 1)]), "universal_flags_provider_inconsistent", 503)
    assert list(save(case, [("Unknown", 1)])) == ["SYSTEM"]


@pytest.mark.parametrize("number", [STARTUP_OPERATION, OPERATION])
def test_both_native_response_capacity_checks_are_atomic(case, number):
    case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    refused(case, upload(case.store, [(PORTAL, 1)]), "response_too_large", 500)


def test_lost_startup_provider_refuses_uploads_and_reports_it(case, monkeypatch):
    monkeypatch.setattr(case.bootstrap, "universal_flag_provider", None)
    refused(case, upload(case.store, [(PORTAL, 1)]), "universal_flags_dependencies_unavailable", 503)
    status = case.service.status()
    assert status["ready"] is False and status["error"] == "universal_flags_dependencies_unavailable"


def test_service_requires_the_startup_flag_provider(asset_root):
    with pytest.raises(ProtocolError) as error:
        UniversalFlagsService(BootstrapService(asset_root), flags_catalog())
    assert error.value.code == "universal_flags_dependencies_unavailable" and error.value.status == 503


def test_readiness_checks_do_not_build_the_catalog_status(case, monkeypatch):
    monkeypatch.setattr(case.service.catalog, "status", lambda: pytest.fail("Readiness must not build the catalog status"))
    UniversalFlagsService(case.bootstrap, case.service.catalog)
    assert save(case, [(PORTAL, 1)])["GF"] == gf([(PORTAL, 1)])


def test_unready_catalog_refuses_service_and_uploads_with_the_dependency_code(case, monkeypatch):
    monkeypatch.setattr(case.service.catalog, "is_ready", lambda: False)
    refused(case, upload(case.store, [(PORTAL, 1)]), "universal_flags_dependencies_unavailable", 503)
    status = case.service.status()
    assert status["ready"] is False and status["error"] == "universal_flags_dependencies_unavailable"
    with pytest.raises(ProtocolError) as error:
        UniversalFlagsService(case.bootstrap, case.service.catalog)
    assert error.value.code == "universal_flags_dependencies_unavailable" and error.value.status == 503


def test_service_requires_a_versioned_catalog(asset_root):
    catalog = SimpleNamespace(content_version="not-a-digest", status=lambda: {"ready": True}, is_ready=lambda: True)
    with pytest.raises(ProtocolError) as error:
        UniversalFlagsService(BootstrapService(asset_root, universal_flag_provider=flags_response), catalog)
    assert error.value.code == "universal_flags_dependencies_invalid"


def test_status_describes_rules_and_bounds_rejected_names(case):
    for index in range(STATUS_LIMIT + 4):
        save(case, [(f"unknown_{index}", 1)])
    save(case, [("unknown_0", 1)])
    status = case.service.status()
    assert status["ready"] is True and status["error"] is None
    assert status["implemented_operations"] == [OPERATION] and status["reload_operation"] == 137
    assert status["native_tested"] is False and status["catalog"] == case.service.catalog.status()
    assert len(status["content_version"]) == 64
    uploads = status["uploads"]
    assert uploads["rejected"][UNLISTED_NAME] == STATUS_LIMIT + 5
    assert uploads["rejected_names"][UNLISTED_NAME] == [f"unknown_{index}" for index in range(STATUS_LIMIT)]
    assert set(uploads["rejected"]) == set(REJECTIONS) and MALFORMED_NAME not in uploads["rejected_names"]
    assert case.service.handlers() == {OPERATION: case.service.save}


def test_content_version_follows_the_catalog(asset_root):
    bootstrap = BootstrapService(asset_root, universal_flag_provider=flags_response)
    first = UniversalFlagsService(bootstrap, flags_catalog())
    assert UniversalFlagsService(bootstrap, flags_catalog()).content_version == first.content_version
    changed = flags_catalog(service_rows(**{MISSION_PORTAL: [portal(4, "History", PORTAL)]}))
    assert UniversalFlagsService(bootstrap, changed).content_version != first.content_version


def test_native_http_upload_and_137_reload_after_reopen(case):
    state = set_flags(case.store.profile().state, {"older": 4})

    def application():
        return create_app(case.directory / "http", initial_state=state,
                          handlers=case.bootstrap.handlers() | case.service.handlers())

    app = application()
    envelope = upload(app.state.store, [(PORTAL, 1), (HISTORY, 1), ("Unknown", 1)])
    assert envelope.profile.route == "m3AwvhTr" and envelope.profile.registration_keys == (OPERATION,)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert reply.status_code == 200
        groups = {node.name: node for node in loads(decode_wire(reply.content, envelope.profile.response_key)).children}
        assert list(groups) == ["SYSTEM", "GF"] and groups["GF"] == gf([(PORTAL, 1), (HISTORY, 1)])
    saved = app.state.store.profile()
    reopened = application()
    envelope = request(reopened.state.store, 137, ())
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        groups = {node.name: node for node in loads(decode_wire(reply.content, envelope.profile.response_key)).children}
        assert groups["GF"] == flags_response(saved.state) == gf([("older", 4), (PORTAL, 1), (HISTORY, 1)])


def test_current_resources_accept_the_native_portal_example(current, asset_root, tmp_path):
    case = built(asset_root, tmp_path / "current.sqlite3", current.catalog)
    rows = [("mission_portal_sp53_20230921", 1), ("AppeardHistoryGroupFlag_3", 1)]
    assert native_order([name for name, _ in rows]) == [name for name, _ in rows]
    assert save(case, rows)["GF"] == gf(rows)
    home_rows = [("OpenUnitLibrarySystem", 1), ("OpenGoddessRevival", 1), ("test_flag_015", 1), ("PurchasedSkin_0001", 1)]
    assert save(case, home_rows)["GF"] == gf(home_rows[:3])
    assert reloaded(case) == gf(rows + home_rows[:3])

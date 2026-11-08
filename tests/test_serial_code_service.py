from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import logging
import random
from string import ascii_lowercase, digits
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps import serial_code_service
from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST, instant
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.presents import MAX_DESCRIPTION_BYTES, MAX_PRESENTS, PresentService
from aigis_ps.protocol import ProtocolRegistry, decode_wire, encode_response, encode_wire, prepare_request
from aigis_ps.request_ledger import HISTORY_LIMIT
from aigis_ps.serial_code_rules import SerialCodeRules, entered_form
from aigis_ps.serial_code_service import (
    HISTORY_NAMESPACE, INVALID_CODE, NAMESPACES, OPERATION, PRESENTS_ADDED, REDEMPTION_NAMESPACE, RESPONSE_LIMIT,
    SILENT, STATUS_NAMES, USED_CODE, USED_UNIQUE_CODE, SerialCodeService, read_redemptions)
from aigis_ps.storage import PlayerProfile, Store
from test_bootstrap import asset_root
from test_business_calendar import FIRST_CLIENT_DAY, LAST_CLIENT_DAY
from test_player_state import assets, growth_setup
from test_presents import presents, state, template_table


DESCRIPTION = "シリアルコード特典"
WINDOW = {"start_date": "2026-11-03", "end_date": "2026-11-05"}
BUSINESS_EDGES = (datetime(2026, 12, 1, 3, 59, 59, 999999, tzinfo=JST), datetime(2026, 12, 1, 4, tzinfo=JST),
                  datetime(2026, 11, 4, 18, 59, 59, 999999, tzinfo=timezone.utc),
                  datetime(2026, 11, 4, 19, tzinfo=timezone.utc),
                  datetime(2026, 11, 5, 3, 59, 59, 999999), datetime(2026, 11, 5, 4))
CLIENT_CALENDAR = (datetime.combine(FIRST_CLIENT_DAY, time.min, JST), datetime.combine(LAST_CLIENT_DAY, time.max, JST))
OUTSIDE_CALENDAR = (CLIENT_CALENDAR[0] - timedelta(microseconds=1), CLIENT_CALENDAR[1] + timedelta(microseconds=1))


def crystal(amount=5, description=DESCRIPTION):
    return {"kind": "free_crystal", "amount": amount, "description": description}


def unit(template_id=2, count=1, description=DESCRIPTION):
    return {"kind": "unit", "template_id": template_id, "count": count, "description": description}


def code(value="abcde", rewards=None, **fields):
    return {"code": value, "rewards": [crystal()] if rewards is None else rewards, **fields}


def service_for(presents, *codes):
    return SerialCodeService(presents.bootstrap, presents, SerialCodeRules(codes=codes))


@pytest.fixture
def store(tmp_path, state):
    return Store(tmp_path / "serial.sqlite3", state)


def envelope(store, *business, ct=1, rewrite=None):
    registry = ProtocolRegistry.load()
    wire = registry.by_registration[OPERATION]
    player = store.profile()
    common = tuple(Column(name, kind, (value,)) for name, kind, value in (
        ("UI", "S", player.player_id), ("UK", "S", player.user_key), ("MK", "S", player.method_key),
        ("CT", "I", ct), ("PFN", "S", "dmm-r18"), ("PlatformCode", "I", 1),
        ("GPVI", "S", "unkown"), ("GPOT", "S", "unkown")))
    xml = dumps(Container(children=common + business))
    xml = prepare_request(xml if rewrite is None else rewrite(xml), wire.st)
    return registry.decode(wire.route, encode_wire(xml, wire.request_key))


def entry(entered, append=1):
    return Container("SC", (Column("CE", "S", (entered,)), Column("APPEND", "I", (append,))))


def redemption(store, entered="abcde", append=1, *, ct=1):
    return envelope(store, entry(entered, append), ct=ct)


def redeem(service, store, entered="abcde", append=1, *, ct=1, request=None):
    request = redemption(store, entered, append, ct=ct) if request is None else request
    response = service.redeem(request, store)
    encode_response(request.profile, response)
    return response


def groups(response):
    return {node.name: node for node in loads(response).children}


def status_of(response):
    keys, values = groups(response)["SERIALCODE"].children
    assert (keys.name, keys.type, keys.values, values.name, values.type) == ("A1", "S", ("STATUS",), "A2", "I")
    assert len(values.values) == 1 and values.values[0] in STATUS_NAMES
    return values.values[0]


def refused(service, store, request, code_name, status=400):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.redeem(request, store)
    assert (error.value.code, error.value.status) == (code_name, status)
    assert store.profile() == before
    return error.value


def mail_ids(state):
    return [row["PresentID"] for row in state["tables"]["presents"]]


def receipts(state):
    return state[HISTORY_NAMESPACE]["receipts"]


def seed(store, change):
    profile = store.profile()
    updated = change(profile.state)
    store.update_state(updated, profile.revision)
    return store.profile()


def at(moment):
    return lambda: moment


def shifted_day(now):
    return (instant(now).astimezone(JST) - timedelta(hours=4)).date()


def noise(count, variant=0):
    source = random.Random(variant)
    return ["".join(source.choices(ascii_lowercase + digits, k=MAX_DESCRIPTION_BYTES)) for _ in range(count)]


def test_only_native_statuses_exist():
    assert dict(STATUS_NAMES) == {0: "silent", 2: "presents_added", -10: "invalid_code", -11: "used_code",
                                  -13: "used_unique_code"}
    assert (SILENT, PRESENTS_ADDED, INVALID_CODE, USED_CODE, USED_UNIQUE_CODE) == (0, 2, -10, -11, -13)
    assert NAMESPACES == (REDEMPTION_NAMESPACE, HISTORY_NAMESPACE)


@pytest.mark.parametrize("entered", ["a", "0", "abcde", "abcde-f", "abcde-fghij", "abcde-fghij-klmno-pqr",
                                     "01234-56789"])
def test_default_table_answers_every_native_code_as_invalid_without_writing(presents, store, entered):
    service = SerialCodeService(presents.bootstrap, presents)
    before = store.profile()
    first = redeem(service, store, entered)
    assert list(groups(first)) == ["SYSTEM", "SERIALCODE"] and status_of(first) == INVALID_CODE
    assert groups(first)["SYSTEM"] == presents.bootstrap._system(before)
    assert redeem(service, store, entered, append=0, ct=2) == first
    assert redeem(service, store, entered) == first
    assert store.profile() == before


def test_result_group_is_one_native_key_value_row(presents, store):
    service = SerialCodeService(presents.bootstrap, presents)
    response = redeem(service, store)
    assert groups(response)["SERIALCODE"] == Container("SERIALCODE", (Column("A1", "S", ("STATUS",)),
                                                                     Column("A2", "I", (INVALID_CODE,))))


def drop_sc(xml):
    return xml.replace(b"<SC>", b"<XC>").replace(b"</SC>", b"</XC>")


@pytest.mark.parametrize("business,rewrite,error_code", [
    ((), None, "invalid_request_fields"),
    ((entry("abcde"), Column("EXTRA", "I", (1,))), None, "invalid_request_fields"),
    ((Container("SC", (Column("CE", "S", ("abcde",)), Column("APPEND", "I", (1,)), Column("X", "I", (1,)))),),
     None, "invalid_request_fields"),
    ((Container("SC", (Column("CE", "S", ("abcde",)),)),), None, "invalid_request_fields"),
    ((Container("SC", (Column("APPEND", "I", (1,)),)),), None, "invalid_request_fields"),
    ((Container("SC", (Column("CE", "I", (12345,)), Column("APPEND", "I", (1,)))),), None, "invalid_request_fields"),
    ((Container("SC", (Column("CE", "S", ("abcde",)), Column("APPEND", "S", ("1",)))),), None,
     "invalid_request_fields"),
    ((Container("SC", (Column("CE", "S", ("abcde", "abcdf")), Column("APPEND", "I", (1,)))),), None,
     "invalid_request_fields"),
    ((Container("SC", (Column("CE", "S", ("abcde",)), Column("APPEND", "I", (1, 1)))),), None,
     "invalid_request_fields"),
    ((entry("abcde"),), lambda xml: xml.replace(b"<SC>", b'<SC T="P">'), "invalid_request_fields"),
    ((entry("abcde"),), lambda xml: xml.replace(b"<SC>", b"<SC>text"), "invalid_request_fields"),
    ((entry("abcde"),), drop_sc, "invalid_request_fields"),
    ((entry("ABCDE"),), None, "serial_code_request_invalid"),
    ((entry("abcdef"),), None, "serial_code_request_invalid"),
    ((entry("abc-de"),), None, "serial_code_request_invalid"),
    ((entry("abcde-"),), None, "serial_code_request_invalid"),
    ((entry("-abcde"),), None, "serial_code_request_invalid"),
    ((entry("abcde--fghij"),), None, "serial_code_request_invalid"),
    ((entry(""),), None, "serial_code_request_invalid"),
    ((entry("-"),), None, "serial_code_request_invalid"),
    ((entry("abcde-fghij-klmno-pqrs"),), None, "serial_code_request_invalid"),
    ((entry("abcde_fghij"),), None, "serial_code_request_invalid"),
    ((entry("ａｂｃ"),), None, "serial_code_request_invalid"),
    ((entry("abcde", 2),), None, "serial_code_request_invalid"),
    ((entry("abcde", -1),), None, "serial_code_request_invalid"),
])
def test_non_native_requests_answer_400_without_writing(presents, store, caplog, business, rewrite, error_code):
    service = service_for(presents, code())
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        refused(service, store, envelope(store, *business, rewrite=rewrite), error_code)
    assert f"Serial code 32 refused with {error_code}" in caplog.text
    assert service.status()["answers"] == {name: 0 for name in STATUS_NAMES.values()}


def test_local_code_goes_to_the_present_box_with_status_2(presents, store):
    service = service_for(presents, code("abcde-fghij", [unit(2, 2), crystal(5)]))
    before = store.profile()
    response = redeem(service, store, "abcde-fghij")
    after = store.profile()
    assert status_of(response) == PRESENTS_ADDED and list(groups(response)) == ["SYSTEM", "SERIALCODE", "PPT"]
    assert after.revision == before.revision + 1
    assert mail_ids(after.state) == [1, 2, 3] and after.state["last_present_id"] == 3
    assert [(row["Type"], row["Content"]) for row in after.state["tables"]["presents"]] == [(1, 2), (1, 2), (0, 5)]
    assert groups(response)["PPT"] == presents.response(after.state, [1, 2, 3])
    assert groups(response)["PPT"].children[0].values == (1, 2, 3)
    assert read_redemptions(after.state) == {"abcdefghij": {"count": 1, "first_day": "2026-11-01",
                                                            "last_day": "2026-11-01", "last_present_ids": [1, 2, 3]}}
    (receipt,) = receipts(after.state)
    assert (receipt["code"], receipt["append"], receipt["present_ids"], receipt["revision"]) == (
        "abcdefghij", 1, [1, 2, 3], after.revision)
    assert receipt["response"].encode("utf-8") == response
    assert after.state["unrelated"] == before.state["unrelated"]
    assert after.state["player_state"] == before.state["player_state"]
    assert after.state["tables"]["crystals"] == before.state["tables"]["crystals"]


def test_exact_retry_replays_the_receipt_byte_for_byte(presents, store):
    service = service_for(presents, code())
    request = redemption(store)
    first = redeem(service, store, request=request)
    committed = store.profile()
    assert redeem(service, store, request=request) == first
    restarted = service_for(presents, code())
    assert redeem(restarted, Store(store.path), request=request) == first
    assert store.profile() == committed
    assert service.status()["answers"]["presents_added"] == 2


@pytest.mark.parametrize("single_use,expected", [(False, USED_UNIQUE_CODE), (True, USED_CODE)])
def test_a_new_request_for_a_used_code_answers_its_used_status(presents, store, single_use, expected):
    service = service_for(presents, code("abcde", single_use=single_use))
    assert status_of(redeem(service, store, ct=1)) == PRESENTS_ADDED
    committed = store.profile()
    assert status_of(redeem(service, store, ct=2)) == expected
    assert status_of(redeem(service, store, append=0, ct=3)) == expected
    assert store.profile() == committed


def test_limits_count_redemptions_per_code(presents, store):
    service = service_for(presents, code("abcde", limit=2), code("fghij"))
    assert status_of(redeem(service, store, ct=1)) == PRESENTS_ADDED
    presents.bootstrap.clock = at(datetime(2026, 11, 2, 4, tzinfo=JST))
    assert status_of(redeem(service, store, ct=2)) == PRESENTS_ADDED
    assert status_of(redeem(service, store, "fghij", ct=3)) == PRESENTS_ADDED
    assert status_of(redeem(service, store, ct=4)) == USED_UNIQUE_CODE
    journal = read_redemptions(store.profile().state)
    assert journal == {"abcde": {"count": 2, "first_day": "2026-11-01", "last_day": "2026-11-02", "last_present_ids": [2]},
                       "fghij": {"count": 1, "first_day": "2026-11-02", "last_day": "2026-11-02",
                                 "last_present_ids": [3]}}


def test_unknown_codes_answer_invalid_beside_a_local_table(presents, store):
    service = service_for(presents, code("abcde"))
    before = store.profile()
    assert status_of(redeem(service, store, "abcdf")) == INVALID_CODE
    assert status_of(redeem(service, store, "abcde-f")) == INVALID_CODE
    assert store.profile() == before


@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 11, 3, 3, 59, 59, tzinfo=JST), INVALID_CODE),
    (datetime(2026, 11, 3, 4, tzinfo=JST), PRESENTS_ADDED),
    (datetime(2026, 11, 2, 19, tzinfo=timezone.utc), PRESENTS_ADDED),
    (datetime(2026, 11, 6, 3, 59, 59, tzinfo=JST), PRESENTS_ADDED),
    (datetime(2026, 11, 6, 4, tzinfo=JST), INVALID_CODE),
    (datetime(2026, 11, 6, 4), INVALID_CODE),
    (datetime(2026, 11, 4, 12), PRESENTS_ADDED),
])
def test_windows_follow_inclusive_jst_business_days(presents, store, moment, expected):
    presents.bootstrap.clock = at(moment)
    service = service_for(presents, code("abcde", window=WINDOW))
    before = store.profile()
    assert status_of(redeem(service, store)) == expected
    assert (store.profile() == before) == (expected == INVALID_CODE)


def test_closed_windows_answer_invalid_before_the_used_status(presents, store):
    presents.bootstrap.clock = at(datetime(2026, 11, 4, 12, tzinfo=JST))
    service = service_for(presents, code("abcde", window=WINDOW))
    request = redemption(store, ct=1)
    first = redeem(service, store, request=request)
    assert status_of(first) == PRESENTS_ADDED
    assert status_of(redeem(service, store, ct=2)) == USED_UNIQUE_CODE
    presents.bootstrap.clock = at(datetime(2026, 11, 7, 12, tzinfo=JST))
    assert status_of(redeem(service, store, ct=3)) == INVALID_CODE
    assert redeem(service, store, request=request) == first


def test_the_clock_is_read_only_for_configured_codes(presents, store):
    service = service_for(presents, code("abcde"))
    presents.bootstrap.clock = lambda: "2026-11-03"
    assert status_of(redeem(service, store, "fghij")) == INVALID_CODE
    refused(service, store, redemption(store), "serial_code_clock_invalid", 503)


@pytest.mark.parametrize("moment", (*BUSINESS_EDGES, *CLIENT_CALENDAR))
def test_journal_days_keep_the_jst_four_oclock_formula(presents, store, moment):
    presents.bootstrap.clock = at(moment)
    assert status_of(redeem(service_for(presents, code()), store)) == PRESENTS_ADDED
    record = read_redemptions(store.profile().state)["abcde"]
    assert record["first_day"] == record["last_day"] == shifted_day(moment).isoformat()


@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_instants_outside_the_client_calendar_refuse_configured_codes(presents, store, moment):
    presents.bootstrap.clock = at(moment)
    service = service_for(presents, code())
    assert status_of(redeem(service, store, "fghij")) == INVALID_CODE
    refused(service, store, redemption(store), "serial_code_clock_invalid", 503)


def test_full_present_box_keeps_the_code_unused(presents, store, caplog):
    service = service_for(presents, code("abcde", [unit(2), crystal()]))
    full = seed(store, lambda state: presents.enqueue(state, [crystal(1)] * (MAX_PRESENTS - 1))[0])
    request = redemption(store)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        response = redeem(service, store, request=request)
    assert status_of(response) == SILENT and list(groups(response)) == ["SYSTEM", "SERIALCODE"]
    assert store.profile() == full
    assert "Serial code 32 kept 'abcde' unused because of present_capacity" in caplog.text
    assert service.status()["kept_unused"] == {"present_capacity": 1}
    seed(store, lambda state: {**state, "tables": {**state["tables"], "presents": state["tables"]["presents"][1:]}})
    assert status_of(redeem(service, store, request=request)) == PRESENTS_ADDED
    after = store.profile().state
    assert len(after["tables"]["presents"]) == MAX_PRESENTS and read_redemptions(after)["abcde"]["count"] == 1


def test_exhausted_present_identities_keep_the_code_unused(presents, store):
    service = service_for(presents, code("abcde", [crystal(), crystal()]))
    before = seed(store, lambda state: {**state, "presents_schema": 1, "last_present_id": INT32_MAX - 1})
    assert status_of(redeem(service, store)) == SILENT
    assert store.profile() == before
    assert service.status()["kept_unused"] == {"present_identity_exhausted": 1}


def test_full_journal_keeps_a_new_code_unused(presents, store, monkeypatch):
    service = service_for(presents, code("abcde"), code("fghij"))
    monkeypatch.setattr(serial_code_service, "MAX_REDEEMED_CODES", 1)
    assert status_of(redeem(service, store, "abcde")) == PRESENTS_ADDED
    before = store.profile()
    assert status_of(redeem(service, store, "fghij")) == SILENT
    assert store.profile() == before
    assert service.status()["kept_unused"] == {"serial_code_journal_capacity": 1}
    codes = before.state[REDEMPTION_NAMESPACE]["codes"]
    overfull = {**before.state, REDEMPTION_NAMESPACE: {"schema_version": 1, "codes": {**codes, "fghij": codes["abcde"]}}}
    with pytest.raises(ProtocolError) as error:
        service.initialize(overfull)
    assert error.value.code == "serial_code_redemptions_invalid"


def test_replies_over_the_receipt_limit_at_redemption_keep_the_code_unused(presents, store, monkeypatch):
    service = service_for(presents, code("abcde"))
    monkeypatch.setattr(serial_code_service, "RESPONSE_LIMIT", 64)
    before = store.profile()
    assert status_of(redeem(service, store)) == SILENT
    assert store.profile() == before
    assert service.status()["kept_unused"] == {"serial_code_response_capacity": 1}


def test_unavailability_is_judged_with_the_widest_present_identities(presents, store, monkeypatch):
    rewards = [crystal(1)] * 20
    narrow = redeem(service_for(presents, code("abcde", rewards)), store)
    assert groups(narrow)["PPT"].children[0].values == tuple(range(1, 21))
    seed(store, lambda state: {**state, "last_present_id": INT32_MAX - len(rewards)})
    widest = redeem(service_for(presents, code("fghij", rewards)), store, "fghij")
    assert groups(widest)["PPT"].children[0].values == tuple(range(INT32_MAX - 19, INT32_MAX + 1))
    for limit, unavailable in ((len(narrow), {"abcde": "serial_code_response_capacity"}),
                               (len(widest) - 1, {"abcde": "serial_code_response_capacity"}), (len(widest), {})):
        monkeypatch.setattr(serial_code_service, "RESPONSE_LIMIT", limit)
        assert dict(service_for(presents, code("abcde", rewards)).unavailable_codes) == unavailable


def test_stale_receipt_answers_the_current_status_instead_of_409(presents, store, caplog):
    service = service_for(presents, code("abcde"))
    request = redemption(store)
    assert status_of(redeem(service, store, request=request)) == PRESENTS_ADDED
    changed = seed(store, lambda state: {**state, "unrelated": [{"later": True}]})
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        assert status_of(redeem(service, store, request=request)) == USED_UNIQUE_CODE
    assert store.profile() == changed and mail_ids(changed.state) == [1]
    assert "answered -13 to a repeated request for 'abcde'" in caplog.text


def test_repeated_requests_never_redeem_again_even_with_uses_left(presents, store):
    service = service_for(presents, code("abcde", limit=3))
    request = redemption(store)
    redeem(service, store, request=request)
    seed(store, lambda state: {**state, "unrelated": []})
    assert status_of(redeem(service, store, request=request)) == USED_UNIQUE_CODE
    assert read_redemptions(store.profile().state)["abcde"]["count"] == 1
    assert status_of(redeem(service, store, ct=2)) == PRESENTS_ADDED


def test_evicted_receipts_still_never_redeem_again(presents, store):
    names = [f"c{index}" for index in range(HISTORY_LIMIT + 1)]
    service = service_for(presents, code(names[0], limit=3), *(code(name) for name in names[1:]))
    first = redemption(store, names[0])
    redeem(service, store, request=first)
    key = receipts(store.profile().state)[0]["request"]
    for name in names[1:]:
        assert status_of(redeem(service, store, name)) == PRESENTS_ADDED
    saved = store.profile()
    history = saved.state[HISTORY_NAMESPACE]
    assert len(history["receipts"]) == HISTORY_LIMIT and key not in [item["request"] for item in history["receipts"]]
    assert key in history["processed_keys"]
    assert status_of(redeem(service, store, request=first)) == USED_UNIQUE_CODE
    assert store.profile() == saved and read_redemptions(saved.state)[names[0]]["count"] == 1


def test_changed_tables_turn_old_receipts_into_current_answers(presents, store):
    request = redemption(store)
    redeem(service_for(presents, code("abcde")), store, request=request)
    saved = store.profile()
    assert status_of(redeem(SerialCodeService(presents.bootstrap, presents), store, request=request)) == INVALID_CODE
    changed = service_for(presents, code("abcde", [crystal(9)]))
    assert status_of(redeem(changed, store, request=request)) == USED_UNIQUE_CODE
    raised = service_for(presents, code("abcde", limit=5))
    assert status_of(redeem(raised, store, request=request)) == USED_UNIQUE_CODE
    assert status_of(redeem(raised, store, ct=2)) == PRESENTS_ADDED
    assert saved.revision + 1 == store.profile().revision


def test_undeliverable_codes_are_listed_and_answer_silently(presents, store, caplog):
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        service = service_for(presents, code("abcde"), code("unit9", [unit(9)]), code("item", [
            {"kind": "item", "item_id": "item_001", "amount": 1, "description": DESCRIPTION}]),
            code("nokind", [{"amount": 1, "description": DESCRIPTION}]))
    assert dict(service.unavailable_codes) == {"item": "present_items_unavailable", "nokind": "present_reward_invalid",
                                               "unit9": "present_template_unsupported"}
    assert "Serial code 'unit9' cannot reach the present box (present_template_unsupported)" in caplog.text
    status = service.status()
    assert status["ready"] is True and status["unavailable_codes"] == dict(service.unavailable_codes)
    before = store.profile()
    for name in ("unit9", "item", "nokind"):
        assert status_of(redeem(service, store, entered_form(name))) == SILENT
    assert store.profile() == before
    assert service.status()["kept_unused"] == {"present_template_unsupported": 1, "present_items_unavailable": 1,
                                               "present_reward_invalid": 1}
    assert status_of(redeem(service, store, "abcde")) == PRESENTS_ADDED


def test_codes_sharing_one_reward_list_share_one_preflight(presents, store, monkeypatch):
    previewed = []
    preview = presents.preview
    monkeypatch.setattr(presents, "preview", lambda rewards, count: previewed.append(rewards) or preview(rewards, count))
    service = service_for(presents, code("abcde"), code("fghij"), code("unit9", [unit(9)]), code("klmno", [unit(9)]),
                          code("pqrst", [crystal(6)]))
    assert previewed == [[crystal()], [unit(9)], [crystal(6)]]
    assert dict(service.unavailable_codes) == {"klmno": "present_template_unsupported",
                                               "unit9": "present_template_unsupported"}
    assert [status_of(redeem(service, store, name, ct=index)) for index, name in enumerate(
        ("abcde", "fghij", "klmno", "pqrst", "unit9"), start=1)] == [PRESENTS_ADDED, PRESENTS_ADDED, SILENT,
                                                                     PRESENTS_ADDED, SILENT]


def test_replies_beyond_the_receipt_or_wire_limit_are_undeliverable(presents, store):
    long_text = "あ" * (MAX_DESCRIPTION_BYTES // len("あ".encode("utf-8")))
    service = service_for(presents, code("fits", [unit(2, 3, long_text)]),
                          code("long", [unit(2, 40, long_text)]),
                          code("noisy", [crystal(1, text) for text in noise(40)]))
    assert dict(service.unavailable_codes) == {"long": "serial_code_response_capacity", "noisy": "response_too_large"}
    response = redeem(service, store, "fits")
    assert status_of(response) == PRESENTS_ADDED and len(response) <= RESPONSE_LIMIT


def test_mailboxes_the_native_listing_cannot_carry_keep_the_code_unused(presents, store, caplog):
    granted = [crystal(1, text) for text in noise(12, variant=1)]
    service = service_for(presents, code("abcde", granted))
    assert not service.unavailable_codes
    full = seed(store, lambda state: presents.enqueue(state, [crystal(1, text) for text in noise(24)])[0])
    request = redemption(store)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        assert status_of(redeem(service, store, request=request)) == SILENT
    assert store.profile() == full and service.status()["kept_unused"] == {"response_too_large": 1}
    assert "Serial code 32 kept 'abcde' unused because of response_too_large" in caplog.text
    seed(store, lambda state: {**state, "tables": {**state["tables"],
                                                   "presents": state["tables"]["presents"][len(granted):]}})
    assert status_of(redeem(service, store, request=request)) == PRESENTS_ADDED
    assert read_redemptions(store.profile().state)["abcde"]["count"] == 1


def test_replies_the_native_receiver_cannot_carry_keep_the_code_unused(presents, store):
    service = service_for(presents, code("abcde"))
    before = store.profile()
    silent = dumps(Container(children=(presents.bootstrap._system(before), Container("SERIALCODE", (
        Column("A1", "S", ("STATUS",)), Column("A2", "I", (SILENT,)))))))
    service._profile = replace(service._profile, response_limit=len(encode_wire(silent, service._profile.response_key)))
    assert status_of(redeem(service, store)) == SILENT
    assert store.profile() == before and service.status()["kept_unused"] == {"response_too_large": 1}


def test_initialize_accepts_old_saves_and_validates_both_namespaces(presents, store, state):
    service = service_for(presents, code("abcde"))
    assert not set(NAMESPACES) & set(state)
    initialized = service.initialize(state)
    assert initialized == state and initialized is not state
    redeem(service, store)
    saved = store.profile().state
    assert set(NAMESPACES) <= set(saved) and service.initialize(saved) == saved
    assert SerialCodeService(presents.bootstrap, presents).initialize(saved) == saved
    broken = {**saved, HISTORY_NAMESPACE: {**saved[HISTORY_NAMESPACE], "receipts": [{"request": "x"}]}}
    with pytest.raises(ProtocolError) as error:
        service.initialize(broken)
    assert (error.value.code, error.value.status) == ("serial_code_history_invalid", 503)


@pytest.mark.parametrize("field,value", [("code", "ABCDE"), ("code", "abc-d"), ("code", 12345), ("append", 2),
                                         ("append", True), ("present_ids", []), ("present_ids", [1, 1]),
                                         ("present_ids", [0]), ("present_ids", (1,))])
def test_malformed_receipts_are_refused_at_startup(presents, store, field, value):
    service = service_for(presents, code("abcde"))
    redeem(service, store)
    saved = store.profile().state
    receipts(saved)[0][field] = value
    with pytest.raises(ProtocolError) as error:
        service.initialize(saved)
    assert (error.value.code, error.value.status) == ("serial_code_history_invalid", 503)


def journal(**record):
    return {"schema_version": 1, "codes": {"abcde": {"count": 1, "first_day": "2026-11-01", "last_day": "2026-11-01",
                                                      "last_present_ids": [1], **record}}}


@pytest.mark.parametrize("value", [
    None, [], {"schema_version": 1}, {**journal(), "extra": 1}, {**journal(), "schema_version": 2},
    {**journal(), "schema_version": True}, {"schema_version": 1, "codes": []},
    {"schema_version": 1, "codes": {"ABCDE": journal()["codes"]["abcde"]}},
    {"schema_version": 1, "codes": {"abc-d": journal()["codes"]["abcde"]}},
    {"schema_version": 1, "codes": {"abcde": []}}, journal(count=0), journal(count=True), journal(count=2 ** 31),
    journal(first_day="2026-11-1"), journal(last_day="20261101"), journal(last_day=None), journal(last_present_ids=[]),
    journal(last_present_ids=[1, 1]), journal(last_present_ids=[0]), journal(last_present_ids=[True]),
    journal(last_present_ids=list(range(1, MAX_PRESENTS + 2))), journal(last_present_ids=(1,)), journal(extra=1),
])
def test_corrupt_journals_are_refused(presents, state, value):
    service = service_for(presents, code("abcde"))
    with pytest.raises(ProtocolError) as error:
        service.initialize({**state, REDEMPTION_NAMESPACE: value})
    assert (error.value.code, error.value.status) == ("serial_code_redemptions_invalid", 503)


def test_journals_of_removed_codes_stay_valid(presents, store, state):
    kept = {**state, REDEMPTION_NAMESPACE: {"schema_version": 1, "codes": {"retired": journal()["codes"]["abcde"]}}}
    assert SerialCodeService(presents.bootstrap, presents).initialize(kept) == kept
    assert read_redemptions({**state, REDEMPTION_NAMESPACE: {"schema_version": 1, "codes": {}}}) == {}
    copied = read_redemptions(kept)
    copied["retired"]["count"] = 9
    assert kept[REDEMPTION_NAMESPACE]["codes"]["retired"]["count"] == 1
    for value in (None, [], "state"):
        with pytest.raises(ProtocolError) as error:
            read_redemptions(value)
        assert error.value.code == "serial_code_redemptions_invalid"


class FixedStore:
    def __init__(self, profile):
        self._profile = profile

    def profile(self):
        return self._profile

    def update_state(self, state, revision):
        raise AssertionError("A replay must not write")


@pytest.mark.parametrize("tamper", [
    lambda state: receipts(state)[0].update(response=receipts(state)[0]["response"] + " "),
    lambda state: receipts(state)[0].update(present_ids=[99]),
    lambda state: receipts(state)[0].update(append=0),
    lambda state: receipts(state)[0].update(code="zzzzz"),
    lambda state: receipts(state)[0].update(revision=receipts(state)[0]["revision"] + 1),
    lambda state: state.update(unrelated=[{"tampered": True}]),
])
def test_tampered_receipts_are_refused_instead_of_replayed(presents, store, tamper):
    service = service_for(presents, code("abcde"))
    request = redemption(store)
    redeem(service, store, request=request)
    saved = store.profile()
    tamper(saved.state)
    forged = FixedStore(PlayerProfile(saved.player_id, saved.user_key, saved.method_key, saved.state, saved.revision))
    with pytest.raises(ProtocolError) as error:
        service.redeem(request, forged)
    assert (error.value.code, error.value.status) == ("serial_code_history_invalid", 503)


def test_reward_dependencies_must_not_change_after_setup(presents, store, caplog):
    service = service_for(presents, code("abcde"))
    default = SerialCodeService(presents.bootstrap, presents)
    presents.tickets = SimpleNamespace(content_version="f" * 64)
    assert service.status()["ready"] is False and service.status()["error"] == "serial_code_dependencies_changed"
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        refused(service, store, redemption(store), "serial_code_dependencies_changed", 503)
        refused(service, store, envelope(store, entry("ABCDE")), "serial_code_dependencies_changed", 503)
    assert "Serial code 32 refused with serial_code_dependencies_changed" in caplog.text
    with pytest.raises(ProtocolError) as error:
        service.initialize(store.profile().state)
    assert (error.value.code, error.value.status) == ("serial_code_dependencies_changed", 503)
    assert default.status()["ready"] is True and status_of(redeem(default, store)) == INVALID_CODE
    presents.tickets = SimpleNamespace(content_version="not a digest")
    assert service.status()["error"] == "serial_code_dependencies_invalid"


@pytest.mark.parametrize("name,value", [("asset_sha256", "d" * 64), *(
    (name, SimpleNamespace(content_version="d" * 64)) for name in (
        "item_catalog", "recovery_catalog", "tickets", "item_tickets", "selection_tickets", "vote_cooking_catalog"))])
def test_every_present_fingerprint_guards_configured_codes(presents, store, name, value):
    service = service_for(presents, code("abcde"))
    default = SerialCodeService(presents.bootstrap, presents)
    setattr(presents, name, value)
    refused(service, store, redemption(store), "serial_code_dependencies_changed", 503)
    assert status_of(redeem(default, store)) == INVALID_CODE


def test_the_default_table_does_not_need_present_templates(presents, store):
    broken = PresentService(presents.bootstrap, presents.players, asset_reader=lambda _: template_table(missing=True))
    assert broken.issues
    assert status_of(redeem(SerialCodeService(presents.bootstrap, broken), store)) == INVALID_CODE
    with pytest.raises(ProtocolError) as error:
        SerialCodeService(presents.bootstrap, broken, SerialCodeRules(codes=[code()]))
    assert error.value.code == "present_templates_unavailable"
    with pytest.raises(ValueError):
        SerialCodeService(presents.bootstrap, presents, {"codes": []})


class RacingStore:
    def __init__(self, store, interfere=None):
        self.store, self.interfere, self.attempts = store, interfere, 0

    def profile(self):
        return self.store.profile()

    def update_state(self, state, revision):
        self.attempts += 1
        if self.interfere is None:
            raise ValueError("Profile revision changed; reload before updating")
        if self.attempts == 1:
            self.interfere()
        return self.store.update_state(state, revision)


def test_concurrent_writes_retry_on_the_new_revision(presents, store):
    service = service_for(presents, code("abcde"))
    racing = RacingStore(store, lambda: seed(store, lambda state: {**state, "unrelated": ["concurrent"]}))
    assert status_of(redeem(service, racing)) == PRESENTS_ADDED
    final = store.profile().state
    assert racing.attempts == 2 and final["unrelated"] == ["concurrent"] and mail_ids(final) == [1]
    assert service.status()["answers"]["presents_added"] == 1


def test_a_concurrent_redemption_of_the_same_code_wins_once(presents, store):
    service = service_for(presents, code("abcde"))
    racing = RacingStore(store, lambda: redeem(service, store, ct=2))
    assert status_of(redeem(service, racing, ct=1)) == USED_UNIQUE_CODE
    final = store.profile().state
    assert mail_ids(final) == [1] and read_redemptions(final)["abcde"]["count"] == 1


def test_persistent_conflicts_end_in_a_logged_profile_change(presents, store, caplog):
    service = service_for(presents, code("abcde"))
    racing = RacingStore(store)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        refused(service, racing, redemption(store), "profile_changed", 409)
    assert racing.attempts == 3 and "refused with profile_changed" in caplog.text
    assert service.status()["answers"]["presents_added"] == 0


def test_append_zero_is_recorded_and_replayed(presents, store):
    service = service_for(presents, code("abcde"))
    request = redemption(store, append=0)
    first = redeem(service, store, request=request)
    assert status_of(first) == PRESENTS_ADDED and receipts(store.profile().state)[0]["append"] == 0
    assert redeem(service, store, request=request) == first


def test_native_http_round_trip_and_replay_after_reopening(presents, state, tmp_path):
    rules = SerialCodeRules(codes=[code("abcde")])

    def application():
        service = SerialCodeService(presents.bootstrap, presents, rules)
        return create_app(tmp_path / "http", initial_state=state, handlers=service.handlers(),
                          state_initializer=service.initialize)

    def post(app, request):
        with TestClient(app, base_url="http://127.0.0.1") as client:
            reply = client.post("/api/" + request.profile.route,
                                content=encode_wire(request.xml, request.profile.request_key))
        assert reply.status_code == 200, reply.text
        return decode_wire(reply.content, request.profile.response_key)

    app = application()
    request = redemption(app.state.store)
    assert request.profile.route == "4672tA0j" and request.profile.registration_keys == (OPERATION,)
    first = post(app, request)
    assert status_of(first) == PRESENTS_ADDED
    assert status_of(post(app, redemption(app.state.store, "zzzzz"))) == INVALID_CODE
    saved = app.state.store.profile()
    reopened = application()
    assert post(reopened, request) == first and reopened.state.store.profile() == saved
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        bad = redemption(reopened.state.store, "abcde", 2)
        reply = client.post("/api/" + bad.profile.route, content=encode_wire(bad.xml, bad.profile.request_key))
    assert reply.status_code == 400 and reply.json()["error"]["code"] == "serial_code_request_invalid"


def test_status_reports_policies_and_answer_counts(presents, store):
    service = service_for(presents, code("abcde"), code("unit9", [unit(9)]), code("fghij", single_use=True))
    redeem(service, store, "abcde", ct=1)
    redeem(service, store, "abcde", ct=2)
    redeem(service, store, "fghij", ct=3)
    redeem(service, store, "fghij", ct=4)
    redeem(service, store, "zzzzz", ct=5)
    redeem(service, store, "unit9", ct=6)
    status = service.status()
    assert status["ready"] is True and status["error"] is None and status["native_tested"] is False
    assert status["implemented_operations"] == [OPERATION] and service.handlers() == {OPERATION: service.redeem}
    assert status["answers"] == {"silent": 1, "presents_added": 2, "invalid_code": 1, "used_code": 1,
                                 "used_unique_code": 1}
    assert status["kept_unused"] == {"present_template_unsupported": 1}
    assert status["statuses"] == {name: value for value, name in STATUS_NAMES.items()}
    assert status["code_count"] == 3 and status["unavailable_codes"] == {"unit9": "present_template_unsupported"}
    assert status["rules_version"] == service.rules.content_version and len(status["content_version"]) == 64
    assert status["response_limit"] == RESPONSE_LIMIT and status["history_limit"] == HISTORY_LIMIT


def test_content_version_follows_the_table_and_reward_dependencies(presents):
    first = service_for(presents, code("abcde"))
    assert service_for(presents, code("A-B-C-D-E")).content_version == first.content_version
    assert service_for(presents, code("abcde", limit=2)).content_version != first.content_version
    default = SerialCodeService(presents.bootstrap, presents)
    assert default.content_version != first.content_version
    presents.selection_tickets = SimpleNamespace(content_version="e" * 64)
    assert SerialCodeService(presents.bootstrap, presents).content_version == default.content_version
    assert service_for(presents, code("abcde")).content_version != first.content_version

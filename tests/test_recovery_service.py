from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import struct
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient
import pytest

from aigis_ps.al_format import decompress_all4, parse_archive, parse_table
from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.bootstrap import BootstrapService, LocalRules
from aigis_ps.business_calendar import civil_day
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.crystal_spending import spend_crystals
from aigis_ps.energy_clock import energy_seconds, set_anchor
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.public_tables import validate_public_master
from aigis_ps.quest_economy import _anchor, _now_seconds
from aigis_ps.recovery_service import CHARISMA_LIMIT, RecoveryRules, RecoveryService
from aigis_ps.request_ledger import HISTORY_LIMIT, LEGACY_SCHEMA_VERSION, SCHEMA_VERSION
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.storage import Store
from aigis_ps.system_literals import command_id, decode_command
from test_bootstrap import asset_root


NOW = datetime(2026, 11, 1, 12)
PROJECT = Path(__file__).resolve().parents[1]
STAMINA_RECOVERY = 86
SHOP_TOP = "CMenuShopTop"
# The shop top's layout list (794920) names only these two archives.
SHOP_TOP_ARCHIVES = ("ShopMenu.aar", "ShopAppealIconMenu.aar")
SHOP_TOP_COMMANDS = "OnCommandMenuShop.atb"
CHARISMA_BUTTON, STAMINA_BUTTON = 1000, 1001
LAYOUT_HEADER = struct.Struct("<4sBxBB")
LAYOUT_TABLES = 16
LAYOUT_OBJECT = struct.Struct("<4s4sI")


@pytest.fixture
def case(asset_root, tmp_path):
    clock = SimpleNamespace(now=NOW)
    bootstrap = BootstrapService(asset_root, local_rules=LocalRules(charisma=0), clock=lambda: clock.now)
    service = RecoveryService(bootstrap)
    state = bootstrap.initial_state()
    state["tables"]["crystals"] = [{"FreeFlag": True, "Num": 100, "Date": civil_day(NOW)}]
    return SimpleNamespace(bootstrap=bootstrap, service=service, clock=clock,
                           store=Store(tmp_path / "recovery.sqlite3", state))


def message(case, tick=1000, *, number=85, platform="dmm-r18", extra=()):
    registry = ProtocolRegistry.load()
    wire = registry.by_registration[number]
    profile = case.store.profile()
    fields = tuple(Column(name, kind, (value,)) for name, kind, value in (
        ("UI", "S", profile.player_id), ("UK", "S", profile.user_key), ("MK", "S", profile.method_key),
        ("CT", "I", tick), ("PFN", "S", platform), ("PlatformCode", "I", 1),
        ("GPVI", "S", "unkown"), ("GPOT", "S", "unkown")))
    raw = prepare_request(dumps(Container(children=fields + tuple(extra))), wire.st)
    return registry.decode(wire.route, encode_wire(raw, wire.request_key))


def change(case, callback):
    profile = case.store.profile()
    callback(profile.state)
    case.store.update_state(profile.state, profile.revision)


def game(case):
    return case.store.profile().state["tables"]["game_param"][0]


def anchor_charisma(state, charisma, moment):
    row = state["tables"]["game_param"][0]
    row["NowCharisma"] = charisma
    set_anchor(row, energy_seconds(moment))


def fail(case, envelope, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as error:
        case.service.recover(envelope, case.store)
    assert error.value.code == code
    assert case.store.profile() == before


def test_full_response_and_exact_atomic_changes(case):
    before = case.store.profile()
    response = case.service.recover(message(case), case.store)
    after = case.store.profile()
    expected = spend_crystals(before.state, 1, NOW)
    expected["tables"]["game_param"][0]["NowCharisma"] = 60
    assert {key: value for key, value in after.state.items() if key != "recovery_history"} == expected
    groups = {node.name: node for node in loads(response).children}
    assert set(groups) == {"SYSTEM", "PPC", "PFC", "PGP", "PGP2"}
    assert [column.values for column in groups["PPC"].children] == [(), (), ()]
    assert groups["PFC"].children[-1].values == (99,)
    pgp = dict(zip(groups["PGP"].children[0].values, groups["PGP"].children[1].values))
    assert pgp["A4"] == "60" and set(pgp) == {f"A{i}" for i in range(1, 10)}
    assert after.revision == before.revision + 1
    assert case.service.handlers() == {85: case.service.recover}
    assert case.service.status()["reload_operations"] == [34, 35, 43, 44]


@pytest.mark.parametrize("free,paid,ok", [(0, 0, False), (1, 0, True), (0, 1, True)])
def test_zero_or_one_eligible_crystal(case, free, paid, ok):
    change(case, lambda state: state["tables"].update(crystals=[
        {"FreeFlag": True, "Num": free, "Date": 700001},
        {"FreeFlag": False, "Num": paid, "Date": 700000}]))
    if not ok:
        fail(case, message(case), "crystal_insufficient")
    else:
        case.service.recover(message(case), case.store)
        assert wallet_totals(case.store.profile().state)["total"] == 0
        assert game(case)["NowCharisma"] == 60


def test_free_before_paid_and_oldest_date_first(case):
    rows = [{"FreeFlag": False, "Num": 7, "Date": 699999},
            {"FreeFlag": True, "Num": 9, "Date": 700002},
            {"FreeFlag": True, "Num": 1, "Date": 700001}]
    change(case, lambda state: state["tables"].update(crystals=rows))
    case.service.recover(message(case), case.store)
    saved = case.store.profile().state["tables"]["crystals"]
    assert [row["Num"] for row in saved] == [7, 9, 0]
    case.service.recover(message(case, 1001), case.store)
    assert [row["Num"] for row in case.store.profile().state["tables"]["crystals"]] == [7, 8, 0]


@pytest.mark.parametrize("current,allowed", [(59, True), (60, True), (1940, True), (1941, False), (2000, False), (2001, False)])
def test_natural_capacity_may_be_exceeded_but_hard_limit_is_atomic(case, current, allowed):
    change(case, lambda state: state["tables"]["game_param"][0].update(NowCharisma=current))
    if allowed:
        case.service.recover(message(case), case.store)
        assert game(case)["NowCharisma"] == current + 60
        assert game(case)["NowCharisma"] <= CHARISMA_LIMIT
    else:
        fail(case, message(case), "recovery_cap_exceeded")


def test_extended_capacity_changes_amount_and_boundary(case):
    change(case, lambda state: state["tables"]["game_param"][0].update(ExtendedMaxCharisma=17, NowCharisma=1923))
    case.service.recover(message(case), case.store)
    assert game(case)["NowCharisma"] == 2000


def test_rank_specific_capacity(case):
    change(case, lambda state: state["tables"]["game_param"][0].update(Rank=2, Experience=16, NowCharisma=10))
    case.service.recover(message(case), case.store)
    assert game(case)["NowCharisma"] == 73


def test_natural_recovery_is_settled_first_and_new_anchor_is_now(case):
    earlier = NOW - timedelta(seconds=400)
    change(case, lambda state: anchor_charisma(state, 3, earlier))
    case.service.recover(message(case), case.store)
    assert game(case)["NowCharisma"] == 65
    assert _anchor(game(case)) == _now_seconds(NOW)


def test_natural_recovery_crosses_midnight(case):
    case.clock.now = datetime(2026, 11, 2, 0, 4)
    earlier = datetime(2026, 11, 1, 23, 58)
    change(case, lambda state: anchor_charisma(state, 4, earlier))
    case.service.recover(message(case), case.store)
    assert game(case)["NowCharisma"] == 66
    assert _anchor(game(case)) == _now_seconds(case.clock.now)


def test_natural_fill_does_not_suppress_paid_recovery(case):
    earlier = NOW - timedelta(days=1)
    change(case, lambda state: anchor_charisma(state, 3, earlier))
    case.service.recover(message(case), case.store)
    assert game(case)["NowCharisma"] == 120


def test_clock_regression_does_not_rewind_anchor_or_debit(case):
    case.clock.now = NOW - timedelta(seconds=1)
    fail(case, message(case), "recovery_clock_backwards")


def test_failed_debit_does_not_commit_elapsed_natural_recovery(case):
    case.clock.now = NOW + timedelta(minutes=6)
    change(case, lambda state: state["tables"].update(crystals=[]))
    fail(case, message(case), "crystal_insufficient")
    assert game(case)["NowCharisma"] == 0


def test_exact_retry_and_reopen_never_double_charge(case):
    envelope = message(case)
    first = case.service.recover(envelope, case.store)
    after = case.store.profile()
    case.clock.now += timedelta(days=1)
    assert case.service.recover(envelope, case.store) == first
    reopened = Store(case.store.path, case.bootstrap.initial_state())
    rebuilt = RecoveryService(case.bootstrap)
    assert rebuilt.recover(envelope, reopened) == first
    assert reopened.profile() == after


def test_new_second_allows_another_identical_recovery(case):
    case.service.recover(message(case), case.store)
    case.service.recover(message(case, 1001), case.store)
    assert game(case)["NowCharisma"] == 120
    assert wallet_totals(case.store.profile().state)["total"] == 98


def test_old_request_cannot_overwrite_later_state(case):
    envelope = message(case)
    case.service.recover(envelope, case.store)
    change(case, lambda state: state.update(unrelated={"keep": 1}))
    fail(case, envelope, "recovery_retry_stale")


def test_changed_rule_rejects_old_receipt(case):
    envelope = message(case)
    case.service.recover(envelope, case.store)
    replacement = RecoveryService(case.bootstrap, recovery_seconds=60)
    assert replacement.content_version != case.service.content_version
    case.service = replacement
    fail(case, envelope, "recovery_retry_stale")


def test_rolled_back_client_clock_still_recovers(case):
    case.service.recover(message(case, 0x7FFFFFFF), case.store)
    case.service.recover(message(case, -0x80000000), case.store)
    case.service.recover(message(case, 1000), case.store)
    assert game(case)["NowCharisma"] == 180
    assert len(case.store.profile().state["recovery_history"]["processed_keys"]) == 3


def test_evicted_request_is_still_rejected_and_logged(case, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    first = message(case, platform="dmm-first")
    case.service.recover(first, case.store)
    for index in range(HISTORY_LIMIT):
        case.service.recover(message(case, platform=f"dmm-{index}"), case.store)
    assert len(case.store.profile().state["recovery_history"]["receipts"]) == HISTORY_LIMIT
    fail(case, first, "recovery_request_stale")
    assert any("request 85" in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)


def test_watermark_history_upgrades_without_rejecting_an_earlier_client_time(case):
    envelope = message(case, 2000)
    response = case.service.recover(envelope, case.store)
    state = case.store.profile().state
    history = state["recovery_history"]
    state["recovery_history"] = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
        "seen_keys": list(history["processed_keys"]),
        "receipts": [{**entry, "client_time": 2000} for entry in history["receipts"]]}
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    saved = case.store.profile()
    assert case.service.recover(envelope, case.store) == response
    assert case.store.profile() == saved
    case.service.recover(message(case, 1000), case.store)
    history = case.store.profile().state["recovery_history"]
    assert history["schema_version"] == SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]
    assert game(case)["NowCharisma"] == 120


@pytest.mark.parametrize("history", [None, {}, {"schema_version": 2, "receipts": [], "last_client_time": None, "seen_keys": []},
    {"schema_version": True, "receipts": [], "last_client_time": None, "seen_keys": []},
    {"schema_version": SCHEMA_VERSION, "receipts": [], "processed_keys": ["short"]}])
def test_unknown_or_invalid_journal_is_preserved(case, history):
    change(case, lambda state: state.update(recovery_history=history))
    fail(case, message(case), "recovery_history_invalid")


def test_concurrent_identical_requests_commit_once(case):
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: case.service.recover(envelope, case.store), range(2)))
    assert results[0] == results[1]
    assert game(case)["NowCharisma"] == 60
    assert wallet_totals(case.store.profile().state)["total"] == 99


def test_cas_rechecks_cap_and_preserves_external_update(case, monkeypatch):
    original = case.store.update_state
    calls = 0

    def update(state, revision):
        nonlocal calls
        calls += 1
        profile = case.store.profile()
        profile.state["tables"]["game_param"][0]["NowCharisma"] = 2000
        profile.state["external"] = "kept"
        original(profile.state, profile.revision)
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", update)
    with pytest.raises(ProtocolError) as error:
        case.service.recover(message(case), case.store)
    assert error.value.code == "recovery_cap_exceeded" and calls == 1
    assert game(case)["NowCharisma"] == 2000
    assert wallet_totals(case.store.profile().state)["total"] == 100
    assert case.store.profile().state["external"] == "kept"


def test_three_cas_failures_are_bounded(case, monkeypatch):
    calls = 0

    def conflict(*_):
        nonlocal calls
        calls += 1
        raise ValueError("conflict")

    monkeypatch.setattr(case.store, "update_state", conflict)
    fail(case, message(case), "profile_changed")
    assert calls == 3


@pytest.mark.parametrize("number", [34, 43, 44, 85])
def test_each_wire_capacity_is_checked_before_commit(case, number):
    envelope = message(case)
    if number == 85:
        envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    else:
        case.service._load_profiles[number] = replace(case.service._load_profiles[number], response_limit=1)
    fail(case, envelope, "response_too_large")


@pytest.mark.parametrize("number", [84, STAMINA_RECOVERY])
def test_only_charisma_operation_is_supported(case, number):
    fail(case, message(case, number=number), "invalid_request_fields")


@pytest.mark.parametrize("mutation", ["extra", "missing", "wrong_ct_type", "ct_vector", "duplicate", "mixed", "root_type"])
def test_strict_empty_business_request(case, mutation):
    envelope = message(case)
    root = envelope.document
    if mutation == "extra":
        ET.SubElement(root, "Count", T="I")
    elif mutation == "missing":
        root.remove(root.find("CT"))
    elif mutation == "wrong_ct_type":
        root.find("CT").set("T", "S")
    elif mutation == "ct_vector":
        ET.SubElement(root.find("CT"), "V").text = "1001"
    elif mutation == "duplicate":
        root.append(deepcopy(root.find("CT")))
    elif mutation == "mixed":
        root.find("CT").tail = "unexpected"
    else:
        root.set("T", "I")
    fail(case, envelope, "invalid_request_fields")


@pytest.mark.parametrize("name", ["UI", "UK", "MK"])
def test_identity_is_verified(case, name):
    envelope = message(case)
    envelope.document.find(name + "/V").text = "other-account"
    before = case.store.profile()
    with pytest.raises(ProtocolError):
        case.service.recover(envelope, case.store)
    assert case.store.profile() == before


@pytest.mark.parametrize("interval", [True, 0, -1, 1.5, 0x80000000])
def test_invalid_recovery_interval_is_rejected(case, interval):
    with pytest.raises(ProtocolError) as error:
        RecoveryService(case.bootstrap, recovery_seconds=interval)
    assert error.value.code == "recovery_catalog_invalid"


def test_rank_catalog_is_frozen_and_change_requires_reconstruction(case):
    previous = case.service.content_version
    case.bootstrap.rank_rows[0]["CharismaMax"] += 1
    assert case.service.status()["ready"] is False
    fail(case, message(case), "recovery_catalog_changed")
    rebuilt = RecoveryService(case.bootstrap)
    assert rebuilt.content_version != previous


def test_invalid_clock_value_does_not_write(case):
    case.clock.now = None
    fail(case, message(case), "quest_economy_argument")


def test_full_readback_after_reopen(case):
    case.service.recover(message(case), case.store)
    reopened = Store(case.store.path, case.bootstrap.initial_state())
    for number, expected in ((34, 60), (44, 99)):
        envelope = message(case, number=number)
        response = case.bootstrap.handlers()[number](envelope, reopened)
        columns = {column.name: column.values for column in loads(response).children}
        assert columns["A4" if number == 34 else "A2"] == (expected,)
    assert reopened.profile() == case.store.profile()


def test_configurable_limit_defaults_are_frozen_and_compatible(case):
    rules = RecoveryRules()
    assert rules.charisma_limit == 2000
    with pytest.raises(FrozenInstanceError):
        rules.charisma_limit = 3000
    explicit = RecoveryService(case.bootstrap, rules=rules)
    assert explicit.content_version == case.service.content_version
    assert explicit.status()["local_rules"]["charisma_limit"] == 2000
    assert "not verified" in explicit.status()["local_rules"]["limit_source"]


@pytest.mark.parametrize("limit,current,allowed", [(120, 60, True), (120, 61, False), (30, 0, False),
                                                  (3000, 2000, True), (0x7FFFFFFF, 0x7FFFFFFF - 60, True),
                                                  (0x7FFFFFFF, 0x7FFFFFFF - 59, False)])
def test_configurable_limit_controls_full_recovery_atomically(case, limit, current, allowed):
    case.service = RecoveryService(case.bootstrap, rules=RecoveryRules(charisma_limit=limit))
    change(case, lambda state: state["tables"]["game_param"][0].update(NowCharisma=current))
    if allowed:
        case.service.recover(message(case), case.store)
        assert game(case)["NowCharisma"] == current + 60
        assert wallet_totals(case.store.profile().state)["total"] == 99
    else:
        fail(case, message(case), "recovery_cap_exceeded")
    assert case.service.status()["charisma_limit"] == limit


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1.5, "2000", None, 0x80000000])
def test_configurable_limit_rejects_invalid_rules(limit):
    with pytest.raises(ValueError, match="positive int32"):
        RecoveryRules(charisma_limit=limit)


@pytest.mark.parametrize("rules", [{"charisma_limit": 3000}, False, 2000, object()])
def test_configurable_limit_service_requires_rules_object(case, rules):
    with pytest.raises(ProtocolError) as error:
        RecoveryService(case.bootstrap, rules=rules)
    assert error.value.code == "recovery_catalog_invalid"


def test_configurable_limit_changes_fingerprint_and_invalidates_old_retry(case):
    envelope = message(case)
    first = case.service.recover(envelope, case.store)
    original_version = case.service.content_version
    compatible = RecoveryService(case.bootstrap, rules=RecoveryRules(charisma_limit=2000))
    assert compatible.recover(envelope, case.store) == first
    case.service = RecoveryService(case.bootstrap, rules=RecoveryRules(charisma_limit=3000))
    assert case.service.content_version != original_version
    fail(case, envelope, "recovery_retry_stale")
    case.service.recover(message(case, 1001), case.store)
    assert game(case)["NowCharisma"] == 120


@pytest.fixture(scope="module")
def native():
    masters, resources, client = PROJECT / "runtime/server/masterdata", PROJECT / "runtime/resources", PROJECT.parent / "aigisr_exe"
    if not all(path.is_file() for path in (client / "Aigis_r.exe", masters / "active.json", resources / "active-index.json")):
        pytest.skip("Verified local client resources and public masters are required")
    store = MasterDataStore(masters)
    snapshot = store.snapshot()
    _, entries = ResourceLibrary(resources, client_hash=snapshot.client_hash).load()
    assets = IndexedAssetService(entries, resources, client)

    def resource(name):
        data, _ = assets.read(name)
        return unpacked(data)

    return SimpleNamespace(store=store, snapshot=snapshot, resource=resource)


def unpacked(data):
    return decompress_all4(data) if data.startswith(b"ALL4") else data


def widget_tags(archive):
    for member in parse_archive(archive).members:
        layout = unpacked(archive[member.offset:member.offset + member.size])
        if not layout.startswith(b"ALOD"):
            continue
        _, _, objects, fields = LAYOUT_HEADER.unpack_from(layout)
        names = [layout[start:layout.index(b"\0", start)]
                 for start in struct.unpack_from(f"<{fields}H", layout, LAYOUT_TABLES + 2 * objects)]
        if b"WidgetTag" not in names:
            continue
        field = names.index(b"WidgetTag")
        for start in struct.unpack_from(f"<{objects}H", layout, LAYOUT_TABLES):
            _, _, count = LAYOUT_OBJECT.unpack_from(layout, start)
            values = start + LAYOUT_OBJECT.size
            indices = layout[values + 2 * count:values + 3 * count]
            if field in indices:
                offset = struct.unpack_from(f"<{count}H", layout, values)[indices.index(field)]
                yield struct.unpack_from("<i", layout, start + offset)[0]


def test_stamina_recovery_stays_unimplemented_like_the_native_client(case, tmp_path):
    app = create_app(tmp_path, initial_state=case.store.profile().state,
                     handlers=case.bootstrap.handlers() | case.service.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(SimpleNamespace(store=app.state.store), number=STAMINA_RECOVERY)
        before = app.state.store.profile()
        response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert response.status_code == 501 and response.json()["error"]["code"] == "not_implemented"
        assert app.state.store.profile() == before
    assert STAMINA_RECOVERY not in case.service.handlers()
    assert any(f"Operation {STAMINA_RECOVERY}" in text for text in case.service.status()["native_exclusions"])


def test_no_current_quest_spends_stamina(native):
    document, _ = validate_public_master("QuestTable", native.snapshot.get_xml("QuestTable"), native.store.schema_fields)
    costs = next(column.values for column in document.children if column.name == "ActionPoint")
    # Quest menus offer the paid stamina refill only after an ActionPoint shortage, which a zero cost never raises.
    assert costs and not any(costs)


def test_shop_top_offers_no_stamina_recovery_button(native):
    rows = parse_table(native.resource(SHOP_TOP_COMMANDS))["rows"]
    commands = {row["_ID"]: decode_command(row["_Cmnd"]) for row in rows}
    # The shop top runs the command row named after the tag of the pushed widget.
    assert '"CheckRemainCharisma"' in commands[command_id(f"{SHOP_TOP}::OnDecision_Tag_{CHARISMA_BUTTON}")]
    assert '"CheckRemainStamina"' in commands[command_id(f"{SHOP_TOP}::OnDecision_Tag_{STAMINA_BUTTON}")]
    tags = [tag for archive in SHOP_TOP_ARCHIVES for tag in widget_tags(native.resource(archive))]
    assert CHARISMA_BUTTON in tags and STAMINA_BUTTON not in tags

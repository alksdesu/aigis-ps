from copy import copy, deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

import aigis_ps.mission_service as module
import aigis_ps.request_ledger as ledger
from aigis_ps.alxml import Column, Container, ValueType, dumps, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_catalog import EventPassCatalog
from aigis_ps.mission_catalog import PAGES
from aigis_ps.mission_pass_points import MissionPassPoints
from aigis_ps.mission_service import HISTORY_LIMIT, MissionService
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.startup_diagnostics import StartupDiagnostics
from aigis_ps.storage import Store
from test_event_pass_catalog import item as pass_item, payloads as pass_payloads, reward as pass_reward
from test_mission_pass_points import definition as pass_definition
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
BOUQUET = "gift_0000_bouquet"


@dataclass(frozen=True)
class Definition:
    condition_id: int
    period_type: int = 1
    disp_tab_type: int = 1
    display_type: int = 0
    group_type: int = 0
    reward_item_type: int = 2
    reward_item_param: int = 25
    reward_item_id: str = ""
    reward_point: int = 0
    event_pass_item_id: str = ""
    event_pass_point: int = 0


class Catalog:
    def __init__(self, definitions=None):
        self.definitions = tuple(definitions or (
            Definition(101), Definition(102, reward_item_type=0, reward_item_param=3),
            Definition(103, 2, 2, reward_item_type=22, reward_item_param=2, reward_item_id=BOUQUET),
            Definition(201, 4, 6, 1, 1, 1, 2), Definition(202, 4, 6, 1, 1, 3, 4),
            Definition(301, 5, 5), Definition(401, 3, 3),
            Definition(10001, 100, 0, reward_item_type=1, reward_item_param=2),
            Definition(10002, 100, 0, reward_item_type=1, reward_item_param=3)))
        self.by_id = {row.condition_id: row for row in self.definitions}
        self.sequence = tuple(row.condition_id for row in self.definitions if row.period_type == 100)
        self.groups = {1: tuple(row.condition_id for row in self.definitions if row.group_type == 1)}
        self.content_version = hashlib.sha256(repr(self.definitions).encode()).hexdigest()
        self.expired = set()

    def listed(self, period, tab):
        if (period, tab) not in PAGES:
            raise ProtocolError("mission_page_unsupported", "Unsupported test page", 501)
        return frozenset(row.condition_id for row in self.definitions if row.period_type == period
                         and row.disp_tab_type == tab and row.display_type == 0)

    def page(self, period, tab, group, now):
        if (period, tab) in PAGES and group == 0:
            members = self.listed(period, tab)
            return tuple(row for row in self.definitions if row.condition_id in members and self.is_active(row, now))
        if (period, tab) == (4, 0) and group in self.groups:
            return tuple(self.by_id[key] for key in self.groups[group] if self.is_active(self.by_id[key], now))
        raise ProtocolError("mission_page_unsupported", "Unsupported test page", 501)

    def is_active(self, definition, now):
        return definition.condition_id not in self.expired

    def is_ready(self):
        return True

    def status(self):
        return {"ready": True}


class Progress:
    def __init__(self, catalog):
        self.catalog = catalog
        self.content_version = hashlib.sha256((catalog.content_version + ":progress").encode()).hexdigest()

    def initialize(self, state):
        result = deepcopy(state)
        result.setdefault("test_missions", {})
        return result

    def snapshot(self, state, now):
        entries = state.get("test_missions", {})
        return tuple({"ID": row.condition_id, **entries.get(str(row.condition_id),
            {"Status": 0, "Read": 0, "Progress": 0})} for row in self.catalog.definitions
            if self.catalog.is_active(row, now))

    def claim(self, state, identifiers, now):
        result = self.initialize(state)
        for key in identifiers:
            row = result["test_missions"].get(str(key))
            if row is None or row["Status"] != 1:
                raise ProtocolError("mission_not_ready", "Mission is not ready", 409)
        for key in identifiers:
            result["test_missions"][str(key)]["Status"] = 2
        return result

    def mark_read(self, state, identifiers, now):
        result = self.initialize(state)
        for key in identifiers:
            result["test_missions"][str(key)]["Read"] = 1
        return result

    def record(self, state, kind, payload, now):
        result = self.initialize(state)
        result["test_missions"][str(payload["id"])] = {"Status": 1, "Read": 0, "Progress": 1}
        return result

    def status(self):
        return {"ready": True}


def build(bundle, catalog=None, now=NOW, pass_points=None):
    catalog = catalog or Catalog()
    clock = SimpleNamespace(now=now)
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: clock.now
    presents = copy(bundle.presents)
    presents.bootstrap = bootstrap
    progress = Progress(catalog)
    service = MissionService(bootstrap, bundle.player_state, presents, catalog, progress=progress,
                             pass_points=pass_points)
    return SimpleNamespace(service=service, catalog=catalog, progress=progress, clock=clock,
                           bootstrap=bootstrap, presents=presents)


@pytest.fixture
def case(bundle, tmp_path):
    result = build(bundle)
    state = bundle.player_state.initialize(result.bootstrap.initial_state())
    state = result.service.initialize(state)
    state["test_missions"] = {str(row.condition_id): {"Status": 1, "Read": 0, "Progress": 1}
                              for row in result.catalog.definitions}
    state["test_missions"]["10002"] = {"Status": 0, "Read": 0, "Progress": 0}
    result.store = Store(tmp_path / "missions.sqlite3", state)
    return result


def message(case, operation=58, identifier=101, period=1, tab=1, group=0, tick=1000, custom=None):
    children = tuple(Column(key, "I", (value,)) for key, value in (
        ("ID", identifier), ("PT", period), ("TabGroupId", tab), ("TaskGroupId", group)))
    return request(case.store, operation, custom if custom is not None else (Container("GL", children),), tick=tick)


def nodes(payload):
    return {row.name: row for row in loads(payload).children}


def glory_summon(payload):
    # Same reading as the client: first-column string keys, later columns applied row by row.
    keys, *columns = nodes(payload)["SMN"].children
    assert keys.type is ValueType.STRING and columns and all(column.type is ValueType.INT for column in columns)
    fields = {key: column.values[row] for column in columns for row, key in enumerate(keys.values)}
    return {"can_summon": fields["Consumed"] != 1, "snapshot": fields["Snapshot"]}


def fail(case, envelope, code, method=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        (method or case.service.handlers()[envelope.profile.registration_keys[0]])(envelope, case.store)
    assert caught.value.code == code
    assert case.store.profile() == before


def mutate(case, callback):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    callback(state)
    return case.store.update_state(state, profile.revision)


def test_read_is_full_snapshot_without_fabricated_groups_or_mutation(case):
    before = case.store.profile()
    payload = case.service.read(request(case.store, 54, ()), case.store)
    result = nodes(payload)
    assert set(result) == {"SYSTEM", "GL", "SMN"}
    assert result["SMN"].children == (Column("A1", "S", ("Consumed", "Snapshot")), Column("A2", "I", (1, 0)))
    assert glory_summon(payload) == {"can_summon": False, "snapshot": 0}
    assert result["GL"].children[0].values == (101, 102, 103, 201, 202, 301, 401, 10001, 10002)
    assert result["GL"].children[1].values == (1, 1, 1, 1, 1, 1, 1, 1, 0)
    assert case.store.profile() == before
    handlers = case.service.handlers()
    assert {key: handlers[key] for key in (58, 59, 60)} == {58: case.service.claim, 59: case.service.claim_group,
                                                            60: case.service.mark_read}
    assert case.bootstrap.startup_diagnostics.wrap(54, handlers[54]) is handlers[54]
    assert handlers[54](request(case.store, 54, ()), case.store) == payload
    assert case.service.status()["catalog"] == case.catalog.status()
    keys, values = result["SMN"].children
    assert all(f"{key}={value}" in case.service.status()["glory_summon"] for key, value in zip(keys.values, values.values))


def recorded_load(case, bundle, monkeypatch):
    diagnostics = StartupDiagnostics()
    monkeypatch.setattr(case.bootstrap, "startup_diagnostics", diagnostics)
    service = MissionService(case.bootstrap, bundle.player_state, case.presents, case.catalog, progress=case.progress)
    return service.handlers()[54], diagnostics


def test_54_serves_stored_progress_while_reward_dependencies_are_unready(case, bundle, monkeypatch):
    load, diagnostics = recorded_load(case, bundle, monkeypatch)
    expected = case.service.read(request(case.store, 54, ()), case.store)

    def unready():
        raise ProtocolError("present_assets_changed", "Present assets changed after setup", 503)

    monkeypatch.setattr(case.presents, "_ready", unready)
    monkeypatch.setattr(bundle.player_state, "_verified_cards", lambda: pytest.fail("54 returns no units"))
    before = case.store.profile()
    assert load(request(case.store, 54, ()), case.store) == expected
    assert case.store.profile() == before
    assert diagnostics.status()["loads"]["54"]["outcome"] == "degraded"
    assert diagnostics.status()["loads"]["54"]["issues"] == [
        {"code": "present_assets_changed", "message": "Present assets changed after setup"}]
    fail(case, message(case), "present_assets_changed")


def test_54_serves_progress_for_a_roster_with_stale_card_references(case, bundle, tmp_path):
    state = deepcopy(case.store.profile().state)
    state["player_state"]["units"][0]["CardID"] = 0x7FFFFFFF
    store = Store(tmp_path / "stale.sqlite3", state)
    with pytest.raises(ProtocolError) as caught:
        bundle.player_state.validate(store.profile().state)
    assert caught.value.code == "player_reference_invalid"
    expected = nodes(case.service.read(request(case.store, 54, ()), case.store))["GL"]
    assert nodes(case.service.read(request(store, 54, ()), store))["GL"] == expected


def test_54_refuses_and_records_when_definitions_cannot_report_progress(case, bundle, monkeypatch):
    load, diagnostics = recorded_load(case, bundle, monkeypatch)
    monkeypatch.setattr(case.catalog, "is_ready", lambda: False)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        load(request(case.store, 54, ()), case.store)
    assert (caught.value.code, caught.value.status) == ("mission_catalog_unavailable", 503)
    assert case.store.profile() == before
    record = diagnostics.status()["loads"]["54"]
    assert record["outcome"] == "refused"
    assert [issue["code"] for issue in record["issues"]] == ["mission_dependencies_changed", "mission_catalog_unavailable"]


def test_readiness_and_degraded_loads_never_build_the_catalog_status(case, bundle, monkeypatch):
    load, diagnostics = recorded_load(case, bundle, monkeypatch)
    expected = case.service.read(request(case.store, 54, ()), case.store)
    monkeypatch.setattr(case.catalog, "status", lambda: pytest.fail("Mission readiness built the full catalog status"))
    case.service._ready()

    def unready():
        raise ProtocolError("present_assets_changed", "Present assets changed after setup", 503)

    monkeypatch.setattr(case.presents, "_ready", unready)
    assert load(request(case.store, 54, ()), case.store) == expected
    assert diagnostics.status()["loads"]["54"]["outcome"] == "degraded"


def test_fame_readiness_never_builds_the_fame_status(bundle, monkeypatch):
    real = bundle.missions
    service = MissionService(build(bundle).bootstrap, bundle.player_state, bundle.presents, real.catalog,
                             fame=real.fame, fame_status=real.fame_status)
    monkeypatch.setattr(real.fame, "status", lambda: pytest.fail("Mission readiness built the full fame status"))
    service._ready()
    monkeypatch.setattr(real.fame, "is_ready", lambda: False)
    with pytest.raises(ProtocolError) as caught:
        service._ready()
    assert (caught.value.code, caught.value.status) == ("mission_fame_unavailable", 503)


def test_readiness_compares_the_verified_inputs_instead_of_hashing_them_again(case, bundle, monkeypatch):
    monkeypatch.setattr(case.presents, "_templates", dict(case.presents._templates))
    service = MissionService(case.bootstrap, bundle.player_state, case.presents, case.catalog, progress=case.progress)
    hashed = []
    digest = module._digest
    monkeypatch.setattr(module, "_digest", lambda value: hashed.append(value) or digest(value))
    for _ in range(3):
        service._ready()
    assert hashed == []
    added = max(case.presents._templates) + 1
    case.presents._templates[added] = next(iter(case.presents._templates.values()))
    with pytest.raises(ProtocolError) as caught:
        service._ready()
    assert (caught.value.code, caught.value.status) == ("mission_dependencies_changed", 503)
    assert len(hashed) == 1
    del case.presents._templates[added]
    service._ready()
    assert len(hashed) == 1


@pytest.mark.parametrize("change,code", [
    ("catalog", "mission_dependencies_changed"), ("progress", "mission_dependencies_changed"),
    ("assets", "mission_dependencies_changed"), ("templates", "mission_dependencies_changed"),
    ("catalog_ready", "mission_dependencies_changed"), ("tickets", "mission_dependencies_changed"),
    ("fingerprint", "mission_dependencies_unavailable"), ("presents", "present_templates_unavailable")])
def test_readiness_keeps_its_conclusions_once_the_inputs_are_verified(case, monkeypatch, change, code):
    case.service._ready()
    if change == "catalog":
        monkeypatch.setattr(case.catalog, "content_version", "a" * 64)
    elif change == "progress":
        monkeypatch.setattr(case.progress, "content_version", "b" * 64)
    elif change == "assets":
        monkeypatch.setattr(case.presents, "asset_sha256", "c" * 64)
    elif change == "templates":
        monkeypatch.setattr(case.presents, "_templates", {**case.presents._templates, 0: 1})
    elif change == "catalog_ready":
        monkeypatch.setattr(case.catalog, "is_ready", lambda: False)
    elif change == "tickets":
        monkeypatch.setattr(case.presents, "item_tickets", SimpleNamespace())
    elif change == "fingerprint":
        monkeypatch.setattr(case.presents, "asset_sha256", "unverified")
    else:
        monkeypatch.setattr(case.presents, "issues", {"present_templates": "changed"})
    with pytest.raises(ProtocolError) as caught:
        case.service._ready()
    assert (caught.value.code, caught.value.status) == (code, 503)


def test_glory_summon_status_ignores_mission_progress_and_stays_out_of_receipts(case, bundle):
    before = case.service.read(request(case.store, 54, ()), case.store)
    envelope = message(case, 59, 0)
    claimed = case.service.claim_group(envelope, case.store)
    assert build(bundle).service.claim_group(envelope, case.store) == claimed
    marked = case.service.mark_read(message(case, 60, 0, 5, 5, tick=1001), case.store)
    receipts = case.store.profile().state["mission_history"]["receipts"]
    assert [entry["operation"] for entry in receipts] == [59, 60]
    assert all("SMN" not in nodes(reply) for reply in (claimed, marked))
    assert all("SMN" not in nodes(entry["response"].encode("utf-8")) for entry in receipts)
    after = case.service.read(request(case.store, 54, ()), case.store)
    assert nodes(after)["SMN"] == nodes(before)["SMN"]
    assert glory_summon(after) == {"can_summon": False, "snapshot": 0}


@pytest.mark.parametrize("identifier,period,tab,mail_type,content", [
    (101, 1, 1, 2, 25), (102, 1, 1, 0, 3), (103, 2, 2, 22, 2),
    (201, 4, 0, 1, 2), (202, 4, 0, 3, 4), (301, 5, 5, 2, 25), (401, 3, 3, 2, 25), (10001, 100, 0, 1, 2)])
def test_single_claims_enqueue_exact_rewards_without_direct_assets(case, identifier, period, tab, mail_type, content):
    before = case.store.profile()
    output = nodes(case.service.claim(message(case, identifier=identifier, period=period, tab=tab), case.store))
    saved = case.store.profile()
    assert saved.revision == before.revision + 1
    assert saved.state["test_missions"][str(identifier)]["Status"] == 2
    assert set(output) == {"SYSTEM", "GLRW", "PPT"}
    assert output["GLRW"].children == (Column("Value", "I", (identifier,)),)
    rows = case.presents.rows(saved.state)
    assert len(rows) == 1 and rows[0]["Type"] == mail_type and rows[0]["Content"] == content
    assert rows[0].get("ItemID") == (BOUQUET if mail_type == 22 else None)
    assert saved.state["player_state"] == before.state["player_state"]
    for table in ("game_param", "game_param2", "crystals", "possessions"):
        assert saved.state["tables"][table] == before.state["tables"][table]
    snapshot = nodes(case.service.read(request(case.store, 54, ()), case.store))["GL"]
    assert dict(zip(snapshot.children[0].values, snapshot.children[1].values))[identifier] == 2


@pytest.mark.parametrize("period,tab,group,expected", [(1, 1, 0, (101, 102)), (4, 0, 1, (201, 202))])
def test_group_claim_uses_only_selected_page_or_group(case, period, tab, group, expected):
    output = nodes(case.service.claim_group(message(case, 59, 0, period, tab, group), case.store))
    assert output["GLRW"].children[0].values == expected
    saved = case.store.profile().state
    assert len(case.presents.rows(saved)) == len(expected)
    assert {int(key) for key, row in saved["test_missions"].items() if row["Status"] == 2} == set(expected)


@pytest.mark.parametrize("operation,identifier,period,tab,group", [
    (58, 101, 2, 2, 0), (58, 101, 1, 0, 0), (58, 101, 1, 1, 1), (58, 999, 1, 1, 0),
    (58, 401, 1, 1, 0), (58, 401, 3, 4, 0), (58, 201, 4, 4, 0), (59, 101, 1, 1, 0), (59, 0, 100, 0, 0),
    (60, 0, 4, 0, 1), (60, 0, 3, 4, 0)])
def test_scope_mismatch_rejected_without_progress_or_reward_changes(case, operation, identifier, period, tab, group):
    fail(case, message(case, operation, identifier, period, tab, group), "mission_scope_invalid")


@pytest.mark.parametrize("damage", ["missing", "duplicate", "string", "multiple", "extra", "typed_group", "no_group"])
def test_strict_typed_request_fields(case, damage):
    children = [Column("ID", "I", (101,)), Column("PT", "I", (1,)),
                Column("TabGroupId", "I", (1,)), Column("TaskGroupId", "I", (0,))]
    if damage == "missing":
        children.pop()
    elif damage == "duplicate":
        children.append(children[0])
    elif damage == "string":
        children[0] = Column("ID", "S", ("101",))
    elif damage == "multiple":
        children[0] = Column("ID", "I", (101, 102))
    elif damage == "extra":
        children.append(Column("Reward", "I", (999,)))
    custom = () if damage == "no_group" else ((Column("GL", "S", ("101",)),)
        if damage == "typed_group" else (Container("GL", tuple(children)),))
    fail(case, message(case, custom=custom), "invalid_request_fields")


def test_wrong_identity_rejected_before_any_mutation(case):
    envelope = message(case)
    envelope.document.find("UK")[0].text = "not-this-account"
    fail(case, envelope, "invalid_identity")


def unchanged(case, envelope, caplog):
    before = case.store.profile()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        output = nodes(case.service.handlers()[envelope.profile.registration_keys[0]](envelope, case.store))
    assert set(output) == {"SYSTEM", "GLRW"} and output["GLRW"].children == (Column("Value", "I", ()),)
    assert case.store.profile() == before
    assert any(f"Mission {envelope.profile.registration_keys[0]} found nothing" in line for line in caplog.messages)


def test_unclaimable_targets_answer_unchanged_without_receipts(case, caplog):
    unchanged(case, message(case, identifier=10002, period=100, tab=0), caplog)
    case.service.claim(message(case), case.store)
    receipts = case.store.profile().state["mission_history"]["receipts"]
    unchanged(case, message(case, tick=1001), caplog)

    def stale(state):
        state["test_missions"]["101"]["Read"] = 1
        state["test_missions"]["102"].update(Status=0, Progress=0)

    mutate(case, stale)
    stale_page = message(case, 59, 0, tick=1002)
    unchanged(case, stale_page, caplog)
    unchanged(case, message(case, 60, 0, 1, 1, tick=1003), caplog)
    assert case.store.profile().state["mission_history"]["receipts"] == receipts
    mutate(case, lambda state: state["test_missions"]["102"].update(Status=1, Progress=1))
    output = nodes(case.service.claim_group(stale_page, case.store))
    assert output["GLRW"].children[0].values == (102,)


@pytest.mark.parametrize("identifier,period,tab", [(101, 1, 1), (301, 5, 5), (201, 4, 0), (10001, 100, 0)])
def test_listed_missions_past_their_validity_dates_answer_unchanged(case, caplog, identifier, period, tab):
    case.catalog.expired.add(identifier)
    unchanged(case, message(case, identifier=identifier, period=period, tab=tab), caplog)
    fail(case, message(case, identifier=identifier, period=2, tab=2, tick=1001), "mission_scope_invalid")


RECEIPT_MEMBERS = (pytest.param(58, 301, 5, 5, 0, 301, id="single"), pytest.param(59, 0, 4, 0, 1, 202, id="group"),
                   pytest.param(60, 0, 5, 5, 0, 301, id="read"))


@pytest.mark.parametrize("operation,identifier,period,tab,group,member", RECEIPT_MEMBERS)
def test_exact_resends_keep_replaying_after_validity_dates_close(case, operation, identifier, period, tab, group,
                                                                 member):
    envelope = message(case, operation, identifier, period, tab, group)
    payload = case.service.handlers()[operation](envelope, case.store)
    saved = case.store.profile()
    assert member in saved.state["mission_history"]["receipts"][-1]["ids"]
    case.catalog.expired.add(member)
    assert case.service.handlers()[operation](envelope, case.store) == payload
    assert case.store.profile() == saved


@pytest.mark.parametrize("operation,identifier,period,tab,group,member", RECEIPT_MEMBERS)
def test_replays_still_require_active_receipt_missions_in_the_snapshot(case, monkeypatch, operation, identifier, period,
                                                                       tab, group, member):
    envelope = message(case, operation, identifier, period, tab, group)
    case.service.handlers()[operation](envelope, case.store)
    shown = case.progress.snapshot
    monkeypatch.setattr(case.progress, "snapshot",
                        lambda state, now: tuple(row for row in shown(state, now) if row["ID"] != member))
    fail(case, envelope, "mission_history_invalid")


def test_remaining_refusals_are_logged_with_their_request_number(case, caplog):
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fail(case, message(case, 58, 101, 2, 2, 0), "mission_scope_invalid")
    assert any("Mission 58 refused with mission_scope_invalid" in line for line in caplog.messages)


def test_read_notification_preserves_claimability_and_never_adds_mail(case):
    before = case.store.profile()
    envelope = message(case, 60, 0)
    output = nodes(case.service.mark_read(envelope, case.store))
    saved = case.store.profile()
    assert output["GLRW"].children == (Column("Value", "I", ()),)
    assert set(output) == {"SYSTEM", "GLRW"}
    assert saved.state["test_missions"]["101"] == {"Status": 1, "Read": 1, "Progress": 1}
    assert saved.state["test_missions"]["102"]["Read"] == 1
    assert saved.state["test_missions"]["103"]["Read"] == 0
    assert saved.state["tables"]["presents"] == before.state["tables"]["presents"]
    assert case.service.mark_read(envelope, case.store) == dumps(Container(children=tuple(output.values())))
    assert case.store.profile() == saved
    case.service.mark_read(message(case, 60, 0, tick=1001), case.store)
    assert case.store.profile() == saved


def test_read_notification_also_marks_claimed_unread_missions(case):
    case.service.claim(message(case), case.store)
    mutate(case, lambda state: state["test_missions"]["102"].update(Status=0, Progress=0))
    envelope = message(case, 60, 0, tick=1001)
    payload = case.service.mark_read(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["test_missions"]["101"] == {"Status": 2, "Read": 1, "Progress": 1}
    assert saved.state["test_missions"]["102"] == {"Status": 0, "Read": 0, "Progress": 0}
    assert [(entry["operation"], entry["ids"]) for entry in saved.state["mission_history"]["receipts"]] == [
        (58, [101]), (60, [101])]
    assert case.service.mark_read(envelope, case.store) == payload and case.store.profile() == saved


def period_four_tabs(case, bundle):
    definitions = (*case.catalog.definitions, Definition(501, 4, 4), Definition(502, 4, 7), Definition(503, 4, 7))
    case.service = build(bundle, Catalog(definitions)).service
    mutate(case, lambda state: state["test_missions"].update({
        "501": {"Status": 1, "Read": 0, "Progress": 1}, "502": {"Status": 1, "Read": 0, "Progress": 1},
        "503": {"Status": 0, "Read": 0, "Progress": 0}}))


@pytest.mark.parametrize("tab", [4, 7])
def test_read_notification_covers_every_tab_of_the_page_period(case, bundle, tab):
    period_four_tabs(case, bundle)
    envelope = message(case, 60, 0, 4, tab)
    payload = case.service.mark_read(envelope, case.store)
    saved = case.store.profile()
    assert nodes(payload)["GLRW"].children == (Column("Value", "I", ()),)
    missions = saved.state["test_missions"]
    assert {key: missions[key]["Read"] for key in ("501", "502", "503", "201", "101")} == {
        "501": 1, "502": 1, "503": 0, "201": 0, "101": 0}
    assert saved.state["mission_history"]["receipts"][-1]["ids"] == [501, 502]
    assert case.service.mark_read(envelope, case.store) == payload and case.store.profile() == saved


def test_page_scoped_read_receipts_keep_replaying(case, bundle, monkeypatch):
    period_four_tabs(case, bundle)
    envelope = message(case, 60, 0, 4, 4)
    with monkeypatch.context() as page_scope:
        page_scope.setitem(module.PERIOD_TABS, 4, (4,))
        payload = case.service.mark_read(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["mission_history"]["receipts"][-1]["ids"] == [501]
    assert saved.state["test_missions"]["502"]["Read"] == 0
    assert case.service.mark_read(envelope, case.store) == payload and case.store.profile() == saved


def test_stored_point_audits_keep_the_stored_item_name_bound():
    entry = {"pass_id": "p" * STORED_ITEM_NAME_BYTES, "requested": 1, "credited": 1, "before": 0, "after": 1,
             "capped": 0, "inactive": 0}
    module._point_audit([entry])
    with pytest.raises(ProtocolError) as caught:
        module._point_audit([{**entry, "pass_id": entry["pass_id"] + "p"}])
    assert caught.value.code == "mission_history_invalid"


@pytest.mark.parametrize("field,value", [("reward_item_type", 99), ("reward_item_param", 0),
    ("reward_point", 1), ("event_pass_item_id", "unknown"), ("event_pass_point", 1)])
def test_unsupported_group_reward_stays_claimable_while_the_rest_is_claimed(case, bundle, field, value):
    definitions = tuple(replace(row, **{field: value}) if row.condition_id == 102 else row for row in case.catalog.definitions)
    case.service = build(bundle, Catalog(definitions)).service
    before = case.store.profile()
    envelope = message(case, 59, 0)
    first = case.service.claim_group(envelope, case.store)
    output = nodes(first)
    saved = case.store.profile()
    assert output["GLRW"].children == (Column("Value", "I", (101,)),)
    assert saved.state["test_missions"]["101"]["Status"] == 2
    assert saved.state["test_missions"]["102"] == before.state["test_missions"]["102"]
    assert [(row["Type"], row["Content"]) for row in case.presents.rows(saved.state)] == [(2, 25)]
    assert saved.state["mission_history"]["receipts"][-1]["ids"] == [101]
    assert case.service.claim_group(envelope, case.store) == first and case.store.profile() == saved
    fail(case, message(case, 58, 102, tick=1001), "mission_reward_unsupported")


def test_group_claim_with_only_unsupported_rewards_keeps_the_refusal(case, bundle):
    definitions = tuple(replace(row, reward_item_type=99) if row.condition_id in (101, 102) else row
                        for row in case.catalog.definitions)
    case.service = build(bundle, Catalog(definitions)).service
    fail(case, message(case, 59, 0), "mission_reward_unsupported")


def test_passive_item_policy_is_not_bypassed_for_mission_rewards(case, bundle):
    definitions = tuple(replace(row, reward_item_type=22, reward_item_param=1, reward_item_id="item_smn_ticket_140001")
                        if row.condition_id == 102 else row for row in case.catalog.definitions)
    case.service = build(bundle, Catalog(definitions)).service
    output = nodes(case.service.claim_group(message(case, 59, 0), case.store))
    saved = case.store.profile()
    assert output["GLRW"].children == (Column("Value", "I", (101,)),)
    assert saved.state["test_missions"]["102"]["Status"] == 1
    assert all(row.get("ItemID") != "item_smn_ticket_140001" for row in case.presents.rows(saved.state))
    fail(case, message(case, 58, 102, tick=1001), "summon_ticket_pool_disabled")


OVERSIZED = "x" * STORED_ITEM_NAME_BYTES


def changed_102(case, **changes):
    return Catalog(tuple(replace(row, **changes) if row.condition_id == 102 else row for row in case.catalog.definitions))


def oversized_pass_points(tmp_path, **changes):
    resources = pass_payloads([pass_reward(NormalPassItemID=OVERSIZED, **changes)], [pass_item(OVERSIZED)])
    possessions = PossessionService(tmp_path, asset_reader=resources.__getitem__)
    catalog = EventPassCatalog(resources.__getitem__, possessions)
    return MissionPassPoints(SimpleNamespace(catalog=catalog, possessions=possessions))


def test_reward_items_past_the_native_cell_stay_claimable_while_the_rest_is_claimed(case, bundle, monkeypatch):
    built = build(bundle, changed_102(case, reward_item_type=22, reward_item_param=1, reward_item_id=OVERSIZED))
    case.service = built.service
    known = built.presents._item_definition
    monkeypatch.setattr(built.presents, "_item_definition",
                        lambda row: None if row.get("ItemID") == OVERSIZED else known(row))
    reward = {"kind": "item", "item_id": OVERSIZED, "amount": 1, "description": module.DESCRIPTION}
    with pytest.raises(ProtocolError) as caught:
        built.presents.preview([reward], 0)
    assert (caught.value.code, caught.value.status) == ("present_item_unreplayable", 503)
    before = case.store.profile()
    envelope = message(case, 59, 0)
    first = case.service.claim_group(envelope, case.store)
    saved = case.store.profile()
    assert nodes(first)["GLRW"].children == (Column("Value", "I", (101,)),)
    assert saved.state["test_missions"]["102"] == before.state["test_missions"]["102"]
    assert [(row["Type"], row["Content"]) for row in case.presents.rows(saved.state)] == [(2, 25)]
    assert case.service.claim_group(envelope, case.store) == first and case.store.profile() == saved
    fail(case, message(case, 58, 102, tick=1001), "mission_reward_unsupported")


def test_ep_past_the_native_cell_stays_claimable_while_it_would_open_an_inventory_row(case, bundle, tmp_path):
    points = oversized_pass_points(tmp_path)
    with pytest.raises(ProtocolError) as caught:
        points.award(case.store.profile().state, [pass_definition(102, OVERSIZED, 1)], NOW)
    assert (caught.value.code, caught.value.status) == ("possession_item_unreplayable", 503)
    case.service = build(bundle, changed_102(case, event_pass_item_id=OVERSIZED, event_pass_point=1),
                         pass_points=points).service
    before = case.store.profile()
    envelope = message(case, 59, 0)
    first = case.service.claim_group(envelope, case.store)
    saved = case.store.profile()
    assert nodes(first)["GLRW"].children == (Column("Value", "I", (101,)),)
    assert saved.state["test_missions"]["102"] == before.state["test_missions"]["102"]
    assert saved.state["tables"]["possessions"] == [] and "mission_pass_delivery" not in saved.state
    assert case.service.claim_group(envelope, case.store) == first and case.store.profile() == saved
    fail(case, message(case, 58, 102, tick=1001), "mission_reward_unsupported")


@pytest.mark.parametrize("changes,rows", [({"Status": 2}, []), ({}, [{"ItemID": OVERSIZED, "Count": 0}])],
                         ids=["closed-pass", "saved-row"])
def test_ep_past_the_native_cell_is_claimable_when_no_inventory_row_opens(case, bundle, tmp_path, changes, rows):
    points = oversized_pass_points(tmp_path, **changes)
    service = build(bundle, changed_102(case, event_pass_item_id=OVERSIZED, event_pass_point=1), pass_points=points).service
    state = deepcopy(case.store.profile().state)
    state["tables"]["possessions"] = deepcopy(rows)
    points.award(state, [pass_definition(102, OVERSIZED, 1)], NOW)
    assert service._claimable(state, (102,), NOW) == (
        (102,), [{"kind": "free_crystal", "amount": 3, "description": module.DESCRIPTION}])


def test_unit_reward_uses_template_parameter_despite_unused_item_metadata(case, bundle):
    definitions = tuple(replace(row, reward_item_type=1, reward_item_param=5, reward_item_id="trade_item_005000")
                        if row.condition_id == 101 else row for row in case.catalog.definitions)
    case.service = build(bundle, Catalog(definitions)).service
    before = case.store.profile()
    case.service.claim(message(case), case.store)
    saved = case.store.profile()
    row, = case.presents.rows(saved.state)
    assert row["Type"] == 1 and row["Content"] == 5 and "ItemID" not in row
    assert saved.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert saved.state["player_state"] == before.state["player_state"]


def test_other_reward_type_with_unverified_item_metadata_remains_unsupported(case, bundle):
    definitions = tuple(replace(row, reward_item_id="trade_item_005000")
                        if row.condition_id == 101 else row for row in case.catalog.definitions)
    case.service = build(bundle, Catalog(definitions)).service
    fail(case, message(case), "mission_reward_unsupported")


def test_mail_capacity_failure_keeps_all_group_rewards_unclaimed(case):
    current = case.store.profile()
    full, _ = case.presents.enqueue(current.state, [{"kind": "gold", "amount": 1, "description": "既存"}] * 500)
    case.store.update_state(full, current.revision)
    fail(case, message(case, 59, 0), "present_capacity")


def test_exact_claim_replay_survives_service_and_store_reopen(case, bundle):
    envelope = message(case, 59, 0)
    payload = case.service.claim_group(envelope, case.store)
    saved = case.store.profile()
    replacement = build(bundle).service
    reopened = Store(case.store.path, {})
    assert replacement.claim_group(envelope, reopened) == payload
    assert reopened.profile() == saved


@pytest.mark.parametrize("change", ["revision", "period", "content"])
def test_claim_replay_rejects_stale_revision_period_or_content(case, bundle, change):
    envelope = message(case)
    case.service.claim(envelope, case.store)
    if change == "revision":
        mutate(case, lambda state: state["tables"]["game_param"][0].update(Money=555))
    elif change == "period":
        case.clock.now += timedelta(days=1)
    else:
        catalog = Catalog()
        catalog.content_version = "a" * 64
        case.service = build(bundle, catalog).service
    fail(case, envelope, "mission_retry_stale")


@pytest.mark.parametrize("damage", ["xml", "claimed_ids", "mail_ids", "saved_state"])
def test_receipts_cannot_forge_success_at_unchanged_revision(case, damage):
    envelope = message(case)
    case.service.claim(envelope, case.store)
    saved = case.store.profile()
    state = deepcopy(saved.state)
    entry = state["mission_history"]["receipts"][-1]
    if damage == "xml":
        entry["response"] = entry["response"].replace("101", "102")
    elif damage == "claimed_ids":
        entry["ids"] = [102]
    elif damage == "mail_ids":
        entry["mail_ids"] = [999]
    else:
        state["test_missions"]["101"]["Status"] = 0
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    fail(case, envelope, "mission_history_invalid")


def test_client_clock_rollback_claims_new_missions_and_keeps_exact_replays_stale(case):
    first = message(case, tick=0x7FFFFFFF)
    case.service.claim(first, case.store)
    case.service.claim(message(case, identifier=102, tick=-0x80000000), case.store)
    case.service.claim(message(case, identifier=301, period=5, tab=5, tick=999), case.store)
    assert len(case.presents.rows(case.store.profile().state)) == 3
    fail(case, first, "mission_retry_stale")
    history = case.store.profile().state["mission_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION and len(history["processed_keys"]) == 3
    assert all("client_time" not in entry and entry["event_pass_points"] is None for entry in history["receipts"])


def test_write_periods_refuse_a_server_clock_outside_the_client_calendar(case):
    before = case.store.profile()
    case.clock.now = datetime(3001, 2, 1, tzinfo=timezone.utc)
    with pytest.raises(ProtocolError) as caught:
        case.service.claim(message(case), case.store)
    assert (caught.value.code, caught.value.status) == ("business_clock_invalid", 503)
    assert case.store.profile() == before


def test_evicted_receipt_stays_processed(case, bundle):
    catalog = Catalog([Definition(identifier) for identifier in range(1, HISTORY_LIMIT + 3)])
    case.service = build(bundle, catalog).service
    mutate(case, lambda state: state.update(test_missions={str(identifier): {"Status": 1, "Read": 0, "Progress": 1}
                                                          for identifier in catalog.by_id}))
    first = message(case, identifier=1)
    for identifier in catalog.by_id:
        case.service.claim(message(case, identifier=identifier), case.store)
    saved = case.store.profile()
    assert len(saved.state["mission_history"]["receipts"]) == HISTORY_LIMIT
    assert len(saved.state["mission_history"]["processed_keys"]) == HISTORY_LIMIT + 2
    fail(case, first, "mission_request_stale")


def test_watermark_history_upgrades_and_keeps_replaying_its_receipts(case):
    envelope = message(case, 59, 0, tick=2000)
    payload = case.service.claim_group(envelope, case.store)
    saved = case.store.profile()
    state = deepcopy(saved.state)
    history = state["mission_history"]
    state["mission_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
        "seen_keys": list(history["processed_keys"]),
        "receipts": [{**{key: value for key, value in entry.items() if key != "event_pass_points"}, "client_time": 2000}
                     for entry in history["receipts"]]}
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    legacy = case.store.profile()
    assert case.service.claim_group(envelope, case.store) == payload
    assert case.store.profile() == legacy
    case.service.claim(message(case, identifier=301, period=5, tab=5, tick=1000), case.store)
    upgraded = case.store.profile().state["mission_history"]
    assert upgraded["schema_version"] == ledger.SCHEMA_VERSION
    assert upgraded["processed_keys"] == [entry["request"] for entry in upgraded["receipts"]]
    assert [entry["event_pass_points"] for entry in upgraded["receipts"]] == [None, None]
    assert all("client_time" not in entry for entry in upgraded["receipts"])


def test_cas_retry_preserves_intervening_balance_and_claims_once(case, monkeypatch):
    update = case.store.update_state
    attempts = []
    def conflict(candidate, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            before = case.store.profile()
            other = deepcopy(before.state)
            other["tables"]["game_param"][0]["Money"] += 17
            update(other, before.revision)
            raise ValueError("Concurrent update")
        return update(candidate, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    before = case.store.profile()
    case.service.claim(message(case), case.store)
    saved = case.store.profile()
    assert len(attempts) == 2
    assert saved.state["tables"]["game_param"][0]["Money"] == before.state["tables"]["game_param"][0]["Money"] + 17
    assert len(case.presents.rows(saved.state)) == 1


@pytest.mark.parametrize("operation", [42, 54, 58])
def test_all_response_capacity_checks_precede_commit(case, monkeypatch, operation):
    encode = module.encode_response
    def too_large(profile, payload):
        if operation in profile.registration_keys:
            raise ProtocolError("test_capacity", "Response exceeds capacity", 503)
        return encode(profile, payload)
    monkeypatch.setattr(module, "encode_response", too_large)
    fail(case, message(case), "test_capacity")


def test_record_is_pure_and_checks_future_read_capacity(case, monkeypatch):
    original = case.store.profile().state
    frozen = deepcopy(original)
    updated = case.service.record(original, "growth", {"id": 10002}, NOW)
    assert original == frozen and updated is not original
    assert updated["test_missions"]["10002"]["Status"] == 1
    def too_large(*args):
        raise ProtocolError("test_capacity", "Response exceeds capacity", 503)
    monkeypatch.setattr(module, "encode_response", too_large)
    with pytest.raises(ProtocolError, match="Response exceeds capacity"):
        case.service.record(original, "growth", {"id": 10002}, NOW)
    assert original == frozen


@pytest.mark.parametrize("history", [{}, {"schema_version": 2}, {"schema_version": True}])
def test_unknown_history_is_preserved_and_rejected(case, history):
    mutate(case, lambda state: state.update(mission_history=history))
    fail(case, message(case), "mission_history_invalid")

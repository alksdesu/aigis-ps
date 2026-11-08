from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
from types import MappingProxyType, SimpleNamespace
import uuid
import zlib

import pytest
from pydantic import ValidationError

from aigis_ps import admin_cmd_letstd, admin_views_letstd, admin_views_rules
from aigis_ps.admin_app import UI_DIRECTORY
from aigis_ps.admin_cmd_letstd import LETSTD, NOTHING_REARMED, REARM, rearm_phrase
from aigis_ps.admin_command import AdminError, Batch, BatchStep, CommandContext, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck, requires_backup
from aigis_ps.admin_config_switch import EMPTY_SET
from aigis_ps.admin_diff import json_digest
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, ViewRegistry, discover
from aigis_ps.admin_routes_state import describe_command
from aigis_ps.admin_views import MAX_PAGE_ITEMS, PROJECTION_NOTE, PageParams
from aigis_ps.admin_views_events import Readback
from aigis_ps.admin_views_letstd import (NATIVE_POPUP_NOTE, POPUP_LINES, POPUP_OFF_NOTE, PRESENTS, PROJECTION_SCOPE,
                                         RULES_REFERENCE, RULES_UNREGISTERED, SOURCE_UNAVAILABLE, Outcome,
                                         ProjectionParams, Retry, RowStatus, popup_windows, rules_key)
from aigis_ps.admin_views_rules import RUNNING_UNKNOWN
from aigis_ps.config_registry import REGISTRY, ConfigError, ConfigKey, ConfigRegistry, Normalizer, TimeSemantics
from aigis_ps.config_sets import ConfigSets, config_identity
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.letstd_catalog import FREE_CRYSTAL, GOLD, ITEM, LetsTDCatalog
from aigis_ps.letstd_rules import GROUPS, PARTNERS, LetsTDRules
from aigis_ps.letstd_service import LOCAL_LABEL, VERIFICATION, LetsTDService
from aigis_ps.letstd_state import (DELIVERED, LEDGER_KEY, PENDING, SOURCES, arm_group, complete_rows, defer_rows,
                                   group_record, letstd_clock, read_letstd)
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.system_literals import command_id
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_views_core import Tripwire, persisting_reads, view_sources
from test_letstd_catalog import build, native_sources
from test_letstd_service import DEFAULT_MAILS
from test_quest_service import bundle


NOW = datetime(2026, 11, 3, 12, tzinfo=JST)
NOW_UTC = NOW.astimezone(timezone.utc).isoformat()
OSHIRORE, CAMPAIGN, PURCHASE = "oshirore", 1, 2
CAMPAIGN_ONLY = {OSHIRORE: {GROUPS[CAMPAIGN]: True}}
OSHIRORE_BOTH = {OSHIRORE: dict.fromkeys(GROUPS.values(), True)}
ALL_GROUPS = {partner: dict.fromkeys(GROUPS.values(), True) for partner in PARTNERS}
VIEW_NAMES = ("letstd.overview", "letstd.ledger", "letstd.deliverability", "letstd.projection")
VIEWS = {view.name: view for view in admin_views_letstd.VIEWS}
HASH_A, HASH_B, HASH_C = "a" * 64, "b" * 64, "c" * 64
TICKET_PREFIX = "item_smn_ticket_"
TEMPLATES = {24: 97, 62: 136, 173: 250, 375: 397, 1043: 1269}
SERVICE_MISSING = "admin_view_service_unavailable"
ADDED, RESET, ADAPTED, REMOVED = 25, 5, 2, 99
CLOSED_POOL, POOL_REASON = "item_smn_ticket_40052", "summon_ticket_pool_disabled"
CAPACITY_REASON = "present_capacity"
REFUSALS = {CLOSED_POOL: (POOL_REASON, 501), "gift_0103_ring": (CAPACITY_REASON, 409),
            "trade_item_005000": ("present_reward_invalid", 422),
            "item_smn_ticket_60005": ("letstd_delivery_invalid", 503)}
# Merged lines of one partner when both of its groups deliver every row of the native resource.
PARTNER_POPUPS = (
    (("free_crystal", None, 17, "multi"), ("gold", None, 5000000, "single"), ("item", CLOSED_POOL, 1, "single"),
     ("item", "item_smn_ticket_40062", 3, "multi")),
    (("item", "item_smn_ticket_40072", 4, "multi"), ("item", "gift_0003_diamond", 20, "multi"),
     ("item", "item_smn_ticket_19158", 2, "multi"), ("item", "item_smn_ticket_85106", 3, "multi")),
    (("item", "item_smn_ticket_40051", 3, "multi"), ("item", "item_smn_ticket_60005", 1, "single"),
     ("item", "gift_0103_ring", 5, "multi"), ("item", "item_smn_ticket_19113", 2, "multi")),
    (("item", "trade_item_005000", 4, "multi"), ("unit", 97, 1, "single"), ("unit", 136, 2, "multi"),
     ("unit", 250, 5, "multi")),
    (("unit", 397, 2, "multi"), ("unit", 1269, 2, "multi")),
)
PAGE = UI_DIRECTORY / "pages" / "letstd.js"
PAGE_CONSTANT = r"^const {name} = (.+);$"
VIEW_CONSTANT = re.compile(r"^const [A-Z_]*VIEW = \"([^\"]+)\";$", re.MULTILINE)
NODE_TIMEOUT = 120


class Mailbox:
    asset_sha256 = HASH_A
    item_catalog = SimpleNamespace(content_version=HASH_B)
    item_tickets = None
    templates = MappingProxyType(TEMPLATES)

    def __init__(self, refusals=None):
        self.refusals = dict(refusals or {})
        self.previews = []

    def _ready(self):
        pass

    def initial_unit_template(self, card_id):
        templates = {card: template for template, card in self.templates.items()}
        if card_id not in templates:
            raise ProtocolError("present_template_unsupported", "No initial template for the fixture card", 501)
        return templates[card_id]

    def preview(self, rewards, existing):
        self.previews.append(existing)
        mails = []
        for reward in rewards:
            if reward.get("item_id") in self.refusals:
                code, status = self.refusals[reward["item_id"]]
                raise ProtocolError(code, "Refused for the fixture", status)
            # Summon tickets open one present each; every other reward is a single present.
            opened = reward["kind"] == "item" and reward["item_id"].startswith(TICKET_PREFIX)
            mails.extend({"Kind": reward["kind"], "Description": reward["description"]}
                         for _ in range(reward["amount"] if opened else 1))
        if existing + len(mails) > MAX_PRESENTS:
            raise ProtocolError(CAPACITY_REASON, "The fixture mailbox is full", 409)
        return mails

    def rows(self, state):
        return deepcopy(state["tables"].get("presents", []))

    def enqueue_groups(self, state, reward_groups):
        rows, groups, last = self.rows(state), [], state.get("last_present_id", 0)
        for rewards in reward_groups:
            added = sum(len(group) for group in groups)
            groups.append([{"PresentID": last + added + index + 1, **mail}
                           for index, mail in enumerate(self.preview(rewards, len(rows) + added))])
        additions = [row for group in groups for row in group]
        updated = deepcopy(state)
        updated.update(presents_schema=1, last_present_id=last + len(additions))
        updated["tables"]["presents"] = rows + deepcopy(additions)
        return updated, groups


def letstd(partners=CAMPAIGN_ONLY, *, native_popup=False, refusals=None):
    return LetsTDService(build(), Mailbox(refusals), LetsTDRules(partners=partners, native_popup=native_popup))


def saved():
    return {"bootstrap_schema": 1, "tables": {"presents": []}}


def specs(catalog, partner, group, skip=()):
    return [{"reward_id": row.reward_id, "definition": row.definition, "start_at": letstd_clock(row.start),
             "end_at": None if row.end is None else letstd_clock(row.end)}
            for row in catalog.rows_for(partner, group) if row.reward_id not in skip]


def armed(service, state=None, partner=OSHIRORE, group=CAMPAIGN, skip=()):
    return arm_group(saved() if state is None else state, partner, group, specs(service.catalog, partner, group, skip),
                     service.rules.content_version, service.catalog.resource_sha256, NOW)


def delivered(state, *rewards):
    updated, _ = complete_rows(state, [(OSHIRORE, CAMPAIGN, reward, [index])
                                       for index, reward in enumerate(rewards, 1)], NOW, SOURCES[0], False)
    updated["tables"]["presents"] = [{"PresentID": index} for index in range(1, len(rewards) + 1)]
    updated["last_present_id"] = len(rewards)
    return updated


def frozen(state, partner=OSHIRORE, group=CAMPAIGN):
    return state[LEDGER_KEY]["partners"][partner][str(group)]["rows"]


def data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return data


def read(name, state, params=None, *, data=None, config_set=None, **services):
    identity = SimpleNamespace(config_set=config_identity({}) if config_set is None else config_set)
    context = SimpleNamespace(state=state, now=NOW, revision=0, services=SimpleNamespace(data_dir=data),
                              generation=SimpleNamespace(bundle=SimpleNamespace(**services),
                                                         generation=SimpleNamespace(identity=identity)))
    view = VIEWS[name]
    if params is None and view.params_model is not None:
        params = view.params_model()
    # Views answer through JSONResponse, so every result must survive the same encoding.
    return json.loads(json.dumps(view.read(context, params), allow_nan=False))


def keyed(result, partner, group, reward_id):
    return next(item for item in result["entries"]["items"]
                if item["key"] == {"partner": partner, "group": group, "reward_id": reward_id})


def rearmed(state, service, partner=OSHIRORE, group=CAMPAIGN):
    context = CommandContext(now=NOW, acquisition_day=1, revision=0, generation_id="gen-letstd",
                             services=SimpleNamespace(letstd=service))
    outcome = Batch((BatchStep(REARM, REARM.parse({"partner": partner, "group": group})),)).apply(state, context)
    return outcome.state, outcome.steps[0]


def picked(mapping, *names):
    return {name: mapping[name] for name in names}


def changes_of(step):
    return {name: step.effects[name] for name in ("added", "reset", "adapted")}


def letstd_registry():
    # Without a registered key, one shaped like its neighbours stands in.
    key = rules_key() or ConfigKey(
        "letstd-rewards", "--letstd-rewards", "LetsTDRewardsFile", rules_class=RULES_REFERENCE,
        loader="letstd_rules:load_letstd_rules", normalizer=Normalizer.TO_DICT, domains=(LETSTD,),
        time_semantics=TimeSemantics.ELIGIBILITY, size_limit="letstd_rules:MAX_RULE_BYTES")
    return ConfigRegistry((*(spec for spec in REGISTRY.values() if spec.rules_class != RULES_REFERENCE), key))


def page_constant(text, name):
    found = re.search(PAGE_CONSTANT.format(name=name), text, re.MULTILINE)
    assert found, f"letstd.js defines no {name}"
    return json.loads(found.group(1))


def test_views_and_the_rearm_command_register_under_the_letstd_domain():
    views = ViewRegistry()
    admin_views_letstd.register(views)
    assert [view.name for view in views] == sorted(VIEW_NAMES)
    assert {view.domain for view in views} == {LETSTD}
    assert [VIEWS[name].params_model for name in VIEW_NAMES] == [None, PageParams, PageParams, ProjectionParams]
    # Only the projection settles against the server clock; the other pages keep their revision ETag.
    assert [view.name for view in views if view.live] == ["letstd.projection"]
    assert all(callable(getattr(view.read, "__wrapped__", None)) for view in views)
    with pytest.raises(ValueError):
        admin_views_letstd.register(views)
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_letstd.register(commands)
    assert [command.operation for command in commands] == [REARM.operation] == ["letstd.rearm"]
    assert REARM.destructive and REARM.touched == {LETSTD} and not REARM.writes
    catalog = discover(PREFLIGHT_DOMAINS)
    assert {"admin_cmd_letstd", "admin_views_letstd"} <= set(catalog.modules)
    assert all(name in catalog.views for name in VIEW_NAMES) and REARM.operation in catalog.commands
    sources = {path.stem: path for path in view_sources()}
    assert persisting_reads(sources["admin_views_letstd"].read_text("utf-8")) == []


@pytest.mark.parametrize("params", [
    {"partner": "OSHIRORE", "group": CAMPAIGN}, {"partner": "partner", "group": CAMPAIGN},
    {"partner": OSHIRORE, "group": 3}, {"partner": OSHIRORE, "group": 0}, {"partner": OSHIRORE, "group": True},
    {"partner": OSHIRORE, "group": 2.0}, {"partner": OSHIRORE, "group": "1"}, {"partner": OSHIRORE},
    {"partner": OSHIRORE, "group": CAMPAIGN, "force": True},
])
def test_rearm_names_exactly_one_native_partner_group(params):
    with pytest.raises(AdminError) as caught:
        REARM.parse(params)
    assert caught.value.code == "admin_params_invalid"


def test_rearm_phrases_name_the_partner_and_the_native_group():
    phrases = {(partner, group): rearm_phrase(REARM.parse({"partner": partner, "group": group}))
               for partner in PARTNERS for group in GROUPS}
    assert phrases[OSHIRORE, CAMPAIGN] == "REARM OSHIRORE NEWCHALLENGE"
    assert phrases["rlyeh", PURCHASE] == "REARM RLYEH NEWPURCHASE"
    assert len(set(phrases.values())) == len(PARTNERS) * len(GROUPS)
    properties = REARM.describe()["params_schema"]["properties"]
    assert properties["partner"]["enum"] == list(PARTNERS)
    assert (properties["group"]["type"], properties["group"]["enum"]) == ("integer", list(GROUPS))


def test_projection_settles_only_the_login_operations():
    assert ProjectionParams().source == SOURCES[0]
    assert ProjectionParams.model_validate_json(json.dumps({"source": SOURCES[1]})).source == SOURCES[1]
    for raw in ('{"source": 117}', '{"source": true}', '{"source": "115"}', '{"source": 115.0}', '{"other": 1}'):
        with pytest.raises(ValidationError):
            ProjectionParams.model_validate_json(raw)
    assert VIEWS["letstd.projection"].params_model.model_json_schema()["properties"]["source"]["enum"] == list(SOURCES)


def test_overview_marks_the_simulation_and_the_native_popup_that_cannot_be_labelled(tmp_path):
    service = letstd(native_popup=True)
    overview = read("letstd.overview", saved(), data=data_dir(tmp_path), letstd=service)
    assert overview["simulation"] == {"simulated": True, "native_tested": False, "label": LOCAL_LABEL,
                                      "label_configurable": False, "verification": VERIFICATION}
    switches = overview["switches"]
    assert switches["ok"] and switches["native_popup"] == {"enabled": True, "labelable": False,
                                                           "note": NATIVE_POPUP_NOTE}
    assert "cannot carry the local label" in NATIVE_POPUP_NOTE
    assert [(item["partner"], item["group"], item["name"], item["enabled"]) for item in switches["groups"]] == [
        (partner, group, name, (partner, group) == (OSHIRORE, CAMPAIGN))
        for partner in PARTNERS for group, name in GROUPS.items()]
    assert switches["content_version"] == service.rules.content_version
    status = overview["service"]
    assert status["ok"] and status["ready"] and status["label"] == LOCAL_LABEL and status["native_popup"] is True
    missing = read("letstd.overview", saved(), data=data_dir(tmp_path))
    assert missing["simulation"] == overview["simulation"]
    assert {missing[name]["error"]["code"] for name in ("service", "switches")} == {SERVICE_MISSING}


def test_switch_source_follows_the_configuration_key_that_loads_the_rules(tmp_path, monkeypatch):
    data, registry = data_dir(tmp_path), letstd_registry()

    def source(config_set=None):
        return read("letstd.overview", saved(), data=data, config_set=config_set, letstd=letstd())["source"]

    monkeypatch.setattr(admin_views_letstd, "REGISTRY", ConfigRegistry(
        tuple(spec for spec in REGISTRY.values() if spec.rules_class != RULES_REFERENCE)))
    assert source() == {"known": False, "key": None, "reason": RULES_UNREGISTERED, "error": None}
    monkeypatch.setattr(admin_views_letstd, "REGISTRY", registry)
    monkeypatch.setattr(admin_views_rules, "REGISTRY", registry)
    spec = rules_key()
    default = source()
    assert picked(default, "known", "set", "key", "source", "path") == {
        "known": True, "set": EMPTY_SET, "key": spec.key, "source": "default", "path": None}
    assert default["effective"] == spec.normalize(spec.default())
    assert picked(default, "cli_flag", "ps_parameter") == {"cli_flag": spec.cli_flag, "ps_parameter": spec.ps_parameter}
    switched = {"schema_version": 1, "partners": CAMPAIGN_ONLY, "native_popup": False}
    stored = ConfigSets(data, registry=registry).create({spec.key: switched}, source="console")
    chosen = source(config_identity(stored.explicit))
    assert picked(chosen, "known", "set", "source", "mode") == {
        "known": True, "set": stored.identifier, "source": "set", "mode": "explicit"}
    assert chosen["effective"] == spec.normalize(LetsTDRules(partners=CAMPAIGN_ONLY)) and chosen["path"]
    assert source("f" * 64) == {"known": False, "key": spec.key, "reason": RUNNING_UNKNOWN, "error": None}

    def unusable(*arguments, **options):
        raise ConfigError("config_path_invalid", "The fixture data directory is unusable", 503)

    monkeypatch.setattr(admin_views_rules, "ConfigSets", unusable)
    failed = source()
    assert (failed["known"], failed["key"], failed["error"]["code"]) == (False, spec.key, "config_path_invalid")


def ledger_state(service):
    state = delivered(armed(service, skip=(ADDED,)), 2, 3)
    state = defer_rows(state, [(OSHIRORE, CAMPAIGN, 17, POOL_REASON), (OSHIRORE, CAMPAIGN, 18, CAPACITY_REASON)],
                       service.dependency_version)
    rows = frozen(state)
    rows[str(ADAPTED)]["definition"] = rows[str(RESET)]["definition"] = HASH_C
    rows[str(REMOVED)] = {**rows["6"], "definition": HASH_C}
    read_letstd(state)
    return state


def test_ledger_lists_every_row_with_its_delivery_and_what_a_rearm_would_change():
    service = letstd()
    state = ledger_state(service)
    ledger = read("letstd.ledger", state, PageParams(limit=MAX_PAGE_ITEMS), letstd=service)
    assert ledger["service"] == {"name": LETSTD, "available": True, "content_version": service.content_version}
    assert ledger["ledger"]["ok"] and ledger["ledger"]["keys"] == {LEDGER_KEY: True}
    assert ledger["ledger"]["last_time_utc"] == NOW_UTC and ledger["catalog"] == {"ok": True}
    assert ledger["readback"] == Readback((LETSTD, PRESENTS)).describe()
    assert ledger["entries"]["total"] == len(service.catalog.rows) + 1
    groups = {(group["partner"], group["group"]): group for group in ledger["groups"]}
    assert list(groups) == [(partner, group) for partner in PARTNERS for group in GROUPS]
    campaign = groups[OSHIRORE, CAMPAIGN]
    assert picked(campaign, "enabled", "armed", "armed_at", "rearmed_at") == {
        "enabled": True, "armed": True, "armed_at": NOW_UTC, "rearmed_at": None}
    assert campaign["rows"] == len(service.catalog.rows_for(OSHIRORE, CAMPAIGN))
    assert campaign["statuses"] == {DELIVERED: 2, PENDING: 23}
    assert {name: campaign[name]["state"] for name in ("rules_version", "resource_sha256")} == {
        "rules_version": "matching", "resource_sha256": "matching"}
    assert campaign["rearm"] == {"needed": True, "added": [ADDED], "reset": [RESET], "adapted": [ADAPTED],
                                 "unavailable": [REMOVED]}
    purchase = groups[OSHIRORE, PURCHASE]
    assert picked(purchase, "enabled", "armed", "rearm", "rules_version") == {
        "enabled": False, "armed": False, "rearm": None, "rules_version": None}
    sent = keyed(ledger, OSHIRORE, CAMPAIGN, ADAPTED)
    assert (sent["status"], sent["definition"]["state"]) == (DELIVERED, "changed")
    assert picked(sent["detail"], "via", "popup", "mail_ids", "delivered_at", "rearm", "retry") == {
        "via": SOURCES[0], "popup": False, "mail_ids": [1], "delivered_at": NOW_UTC, "rearm": True, "retry": None}
    row = service.catalog.get(OSHIRORE, CAMPAIGN, ADAPTED)
    assert picked(sent["detail"], "config_id", "kind", "card_id", "count", "start") == {
        "config_id": row.config_id, "kind": "item", "card_id": None, "count": row.count, "start": row.start.isoformat()}
    restarted = keyed(ledger, OSHIRORE, CAMPAIGN, RESET)
    assert (restarted["status"], restarted["definition"]["state"]) == (PENDING, "changed")
    assert restarted["detail"]["rearm"] is True
    held = keyed(ledger, OSHIRORE, CAMPAIGN, 17)
    assert (held["reason"], held["detail"]["retry"]) == (POOL_REASON, Retry.DEPENDENCY_CHANGE)
    assert keyed(ledger, OSHIRORE, CAMPAIGN, 18)["detail"]["retry"] == Retry.NEXT_SETTLEMENT
    assert keyed(ledger, OSHIRORE, CAMPAIGN, 1)["detail"]["retry"] == Retry.NEXT_SETTLEMENT
    added = keyed(ledger, OSHIRORE, CAMPAIGN, ADDED)
    assert (added["status"], added["definition"]["state"]) == (RowStatus.ADDED, "unfrozen")
    assert picked(added["detail"], "rearm", "mail_ids", "kind") == {"rearm": True, "mail_ids": None,
                                                                    "kind": "free_crystal"}
    gone = keyed(ledger, OSHIRORE, CAMPAIGN, REMOVED)
    assert (gone["definition"]["state"], gone["definition"]["error"]["code"]) == ("unavailable", SOURCE_UNAVAILABLE)
    assert gone["detail"]["config_id"] is None and gone["detail"]["rearm"] is False
    waiting = keyed(ledger, "rlyeh", PURCHASE, 11)
    assert picked(waiting, "status", "reason") == {"status": RowStatus.UNARMED, "reason": None}
    assert waiting["definition"]["state"] == "unfrozen"
    updated, step = rearmed(state, service)
    assert changes_of(step) == {name: campaign["rearm"][name] for name in ("added", "reset", "adapted")}
    after = read("letstd.ledger", updated, PageParams(limit=MAX_PAGE_ITEMS), letstd=service)
    refreshed = next(group for group in after["groups"] if (group["partner"], group["group"]) == (OSHIRORE, CAMPAIGN))
    assert refreshed["rearm"] == {"needed": False, "added": [], "reset": [], "adapted": [], "unavailable": [REMOVED]}
    assert refreshed["rearmed_at"] == NOW_UTC
    adapted = keyed(after, OSHIRORE, CAMPAIGN, ADAPTED)
    assert picked(adapted["detail"], "needs_adaptation", "mail_ids") == {"needs_adaptation": True, "mail_ids": [1]}
    assert adapted["status"] == DELIVERED
    assert keyed(after, OSHIRORE, CAMPAIGN, ADDED)["status"] == PENDING


def test_ledger_reads_frozen_rows_without_the_service_and_reports_broken_ledgers():
    service = letstd()
    state = ledger_state(service)
    ledger = read("letstd.ledger", state, PageParams(limit=MAX_PAGE_ITEMS))
    assert ledger["service"] == {"name": LETSTD, "available": False, "content_version": None}
    assert ledger["catalog"]["error"]["code"] == SERVICE_MISSING
    assert ledger["entries"]["total"] == len(frozen(state))
    assert {item["definition"]["state"] for item in ledger["entries"]["items"]} == {"unavailable"}
    assert {item["detail"]["retry"] for item in ledger["entries"]["items"]} == {None}
    campaign = ledger["groups"][0]
    assert (campaign["enabled"], campaign["rows"], campaign["rearm"]) == (None, None, None)
    assert campaign["rules_version"]["error"]["code"] == SERVICE_MISSING
    broken = read("letstd.ledger", {**saved(), LEDGER_KEY: {"schema_version": 2}}, letstd=service)
    assert broken["ledger"]["ok"] is False and broken["ledger"]["error"]["code"] == "letstd_state_invalid"
    assert broken["entries"]["total"] == 0 and "groups" not in broken


def test_ledger_retries_follow_the_group_switch_and_the_dependencies():
    state = ledger_state(letstd())
    switched_off = read("letstd.ledger", state, PageParams(limit=MAX_PAGE_ITEMS), letstd=letstd({}))
    assert switched_off["groups"][0]["enabled"] is False
    retries = {item["key"]["reward_id"]: item["detail"]["retry"] for item in switched_off["entries"]["items"]
               if (item["key"]["partner"], item["key"]["group"]) == (OSHIRORE, CAMPAIGN)}
    assert {retries[reward] for reward in (1, 17, 18, REMOVED)} == {Retry.PAUSED}
    assert retries[2] is None and retries[ADDED] is None
    mailbox = Mailbox()
    mailbox.asset_sha256 = HASH_C
    moved = LetsTDService(build(), mailbox, LetsTDRules(partners=CAMPAIGN_ONLY))
    reread = read("letstd.ledger", state, PageParams(limit=MAX_PAGE_ITEMS), letstd=moved)
    assert keyed(reread, OSHIRORE, CAMPAIGN, 17)["detail"]["retry"] == Retry.NEXT_SETTLEMENT


def test_deliverability_previews_every_row_without_copying_the_save(monkeypatch):
    service = letstd(refusals=REFUSALS)
    state = delivered(armed(service, skip=(ADDED,)), 2, 3)
    before = json_digest(state)
    monkeypatch.setattr(admin_views_letstd, "deepcopy", Tripwire("deepcopy"))
    result = read("letstd.deliverability", state, PageParams(limit=MAX_PAGE_ITEMS), letstd=service)
    assert json_digest(state) == before and service.presents.previews == [0] * len(service.catalog.rows)
    assert result["ok"] and result["mailbox"] == {"held": 2, "capacity": MAX_PRESENTS, "room": MAX_PRESENTS - 2}
    affected = len(REFUSALS) * len(PARTNERS)
    assert result["outcomes"] == {Outcome.DELIVERABLE: len(service.catalog.rows) - affected,
                                  Outcome.DEFERRED: affected - len(PARTNERS), Outcome.FAILING: len(PARTNERS)}
    assert result["enabled_waiting"] == {"rows": 23, "mails": 19, "fits": True,
                                         "statuses": {PENDING: 22, RowStatus.ADDED: 1},
                                         "outcomes": {Outcome.DELIVERABLE: 19, Outcome.DEFERRED: 3, Outcome.FAILING: 1}}
    paused = keyed(result, OSHIRORE, CAMPAIGN, 17)
    assert picked(paused, "enabled", "status", "kind", "target", "count", "outcome", "mails", "retry") == {
        "enabled": True, "status": PENDING, "kind": "item", "target": CLOSED_POOL, "count": 1,
        "outcome": Outcome.DEFERRED, "mails": None, "retry": Retry.DEPENDENCY_CHANGE}
    assert paused["error"]["code"] == POOL_REASON
    assert keyed(result, OSHIRORE, CAMPAIGN, 8)["retry"] == Retry.EVERY_SETTLEMENT
    assert keyed(result, OSHIRORE, CAMPAIGN, 14)["retry"] == Retry.DEPENDENCY_CHANGE
    assert keyed(result, "rlyeh", CAMPAIGN, 13)["outcome"] == Outcome.FAILING
    assert keyed(result, OSHIRORE, CAMPAIGN, 2)["status"] == DELIVERED
    assert keyed(result, OSHIRORE, CAMPAIGN, ADDED)["status"] == RowStatus.ADDED
    tickets = keyed(result, "rlyeh", PURCHASE, 1)
    assert picked(tickets, "status", "enabled", "outcome", "mails") == {
        "status": RowStatus.UNARMED, "enabled": False, "outcome": Outcome.DELIVERABLE, "mails": 2}
    assert picked(keyed(result, OSHIRORE, CAMPAIGN, 15), "kind", "target") == {"kind": "gold", "target": None}
    window = read("letstd.deliverability", state, PageParams(offset=100, limit=10), letstd=service)["entries"]
    assert (window["total"], len(window["items"]), window["next_offset"]) == (len(service.catalog.rows), 5, None)
    assert read("letstd.deliverability", state)["error"]["code"] == SERVICE_MISSING


def test_projection_settles_a_private_copy_and_previews_the_native_popups():
    service = letstd(OSHIRORE_BOTH, refusals={CLOSED_POOL: (POOL_REASON, 501)})
    state = delivered(armed(service), 2, 3)
    before = json_digest(state)
    projection = read("letstd.projection", state, ProjectionParams(source=SOURCES[1]), letstd=service)
    assert json_digest(state) == before
    settled, grants = service.settle(deepcopy(state), NOW, source=SOURCES[1])
    assert picked(projection, "ok", "projected", "persisted", "note", "scope", "source") == {
        "ok": True, "projected": True, "persisted": False, "note": PROJECTION_NOTE, "scope": PROJECTION_SCOPE,
        "source": SOURCES[1]}
    assert projection["as_of"] == {"utc": NOW_UTC, "jst": NOW.isoformat()}
    assert projection["armed"] == [{"partner": OSHIRORE, "group": PURCHASE}]
    assert [picked(grant, "partner", "group", "reward_id", "mail_ids") for grant in projection["grants"]] == [
        picked(grant, "partner", "group", "reward_id", "mail_ids") for grant in grants]
    skipped = {(CAMPAIGN, reward) for reward in (2, 3, 17)}
    assert [(grant["group"], grant["reward_id"]) for grant in grants] == [
        (row.group, row.reward_id) for group in GROUPS for row in service.catalog.rows_for(OSHIRORE, group)
        if (row.group, row.reward_id) not in skipped]
    first = projection["grants"][0]
    row = service.catalog.get(OSHIRORE, CAMPAIGN, first["reward_id"])
    assert {name: first[name] for name in ("config_id", "kind", "target", "count", "mails")} == {
        "config_id": row.config_id, "kind": "item", "target": row.item_id, "count": row.count, "mails": 1}
    assert projection["changes"] == [{"key": {"partner": OSHIRORE, "group": CAMPAIGN, "reward_id": 17},
                                      "before": {"status": PENDING, "reason": None},
                                      "after": {"status": PENDING, "reason": POOL_REASON}}]
    mails = sum(len(grant["mail_ids"]) for grant in grants)
    assert projection["mailbox"] == {"before": 2, "after": 2 + mails, "capacity": MAX_PRESENTS}
    assert len(service.presents.rows(settled)) == 2 + mails
    popups = projection["popups"]
    assert (popups["shown"], popups["labelable"], popups["note"]) == (False, False, POPUP_OFF_NOTE)
    assert popups["reward_param"] == [{
        "ServiceId": OSHIRORE,
        "newChallenge": ",".join(str(grant["reward_id"]) for grant in grants if grant["group"] == CAMPAIGN),
        "newPurchase": ",".join(str(grant["reward_id"]) for grant in grants if grant["group"] == PURCHASE)}]
    assert popups["windows"] == json.loads(json.dumps(popup_windows(
        service.catalog.get(grant["partner"], grant["group"], grant["reward_id"]) for grant in grants)))
    shown = read("letstd.projection", state, letstd=letstd(OSHIRORE_BOTH, native_popup=True))["popups"]
    assert (shown["shown"], shown["note"]) == (True, NATIVE_POPUP_NOTE)
    assert read("letstd.projection", state)["error"]["code"] == SERVICE_MISSING


def line_rows(rows):
    return [(line["kind"], line["target"], line["count"], line["text"]) for line in rows]


def test_native_popups_merge_both_groups_in_the_client_order():
    catalog = build()
    windows = popup_windows(reversed(catalog.rows))
    assert [window["partner"] for window in windows] == [partner for partner in PARTNERS for _ in PARTNER_POPUPS]
    for partner in PARTNERS:
        own = [window for window in windows if window["partner"] == partner]
        assert [window["window"] for window in own] == list(range(1, len(PARTNER_POPUPS) + 1))
        assert {json.dumps(window["title"]) for window in own} == {json.dumps(
            {"format_key": "LetsTDCPTitleFormat", "name_key": f"LetsTDCPTitleName_{partner}"})}
        assert tuple(tuple(line_rows(window["lines"])) for window in own) == PARTNER_POPUPS
    assert max(len(window["lines"]) for window in windows) == POPUP_LINES
    lines = [line for window in windows if window["partner"] == OSHIRORE for line in window["lines"]]
    items = [line["target"] for line in lines if line["kind"] == "item"]
    assert [command_id(item) for item in items] == sorted(command_id(item) for item in items)
    assert items != sorted(items, key=lambda item: zlib.crc32(item.encode("utf-8"))) and items != sorted(items)
    units = [line["target"] for line in lines if line["kind"] == "unit"]
    assert units == sorted(units)
    crystal, gold = lines[0], lines[1]
    assert [reward["reward_id"] for reward in crystal["rewards"]] == [23, 24, 25]
    gold_row = catalog.get(OSHIRORE, CAMPAIGN, 15)
    assert (gold["count"], gold["text"], gold["text_key"]) == (gold_row.count, "single", gold_row.receive_single)


def test_native_popup_lines_take_the_last_texts_and_skip_unknown_types():
    rows = [row for row in build().rows if row.partner == OSHIRORE and row.group == CAMPAIGN]
    tickets = sorted((row for row in rows if row.item_id == "item_smn_ticket_85106"), key=lambda row: row.config_id)
    last = tickets[-1]
    rows = [replace(row, receive_single=7, receive_multi=11) if row is last else row for row in rows]
    unknown = replace(rows[0], item_type=max(FREE_CRYSTAL, GOLD, ITEM) + 5, item_id="unknown_item")
    lines = [line for window in popup_windows([*rows, unknown]) for line in window["lines"]]
    merged = next(line for line in lines if line["target"] == last.item_id)
    assert (merged["count"], merged["text"], merged["text_key"]) == (len(tickets), "multi", 11)
    assert [reward["config_id"] for reward in merged["rewards"]] == [row.config_id for row in tickets]
    assert all(line["target"] != "unknown_item" for line in lines)
    single = [replace(rows[0], item_type=FREE_CRYSTAL, item_id="", count=1)]
    assert line_rows(popup_windows(single)[0]["lines"]) == [("free_crystal", None, 1, "single")]


def test_rearm_commits_through_plan_and_commit_with_its_phrase_and_a_backup():
    service = letstd()
    store = FakeStore(armed(service, skip=(ADDED,)))
    generation = FakeGeneration(id="gen-letstd", bundle=SimpleNamespace(letstd=service), content_versions={}, clock=NOW)
    pipeline = CommitPipeline(store, PlanStore(), GenerationPreflight(), InitializerRestartCheck(service.initialize),
                              FakeRuntime(generation), backup_directory=Path("backups"))
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_letstd.register(commands)
    plan = pipeline.preview(commands.batch([{"operation": REARM.operation,
                                             "params": {"partner": OSHIRORE, "group": CAMPAIGN}}]))
    assert plan.committable and requires_backup(plan.batch)
    assert [item.phrase for item in plan.confirmations] == ["REARM OSHIRORE NEWCHALLENGE"]
    described = plan.describe()
    assert described["touched"] == [LETSTD] and described["steps"][0]["effects"]["added"] == [ADDED]
    arguments = dict(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=0, op_id=str(uuid.uuid4()))
    with pytest.raises(AdminError) as caught:
        pipeline.commit(**arguments, confirmations=("REARM OSHIRORE NEWPURCHASE",))
    assert caught.value.code == "admin_confirmation_required" and store.revision == 0 and not store.backups
    outcome = pipeline.commit(**arguments, confirmations=("REARM OSHIRORE NEWCHALLENGE",))
    assert outcome.result["backup_before_commit"] is not None and store.backups[0]["commits_before"] == 0
    assert store.revision == 1 and outcome.result["readback"] == {}
    record = group_record(read_letstd(store.state), OSHIRORE, CAMPAIGN)
    assert record["rows"][str(ADDED)]["status"] == PENDING and record["rearmed_at"] == letstd_clock(NOW)


def test_rearm_needs_an_armed_group_and_says_when_no_row_changed():
    service = letstd()
    with pytest.raises(AdminError) as unarmed:
        rearmed(saved(), service, OSHIRORE, PURCHASE)
    assert (unarmed.value.code, unarmed.value.status) == ("letstd_group_unarmed", 409)
    current = armed(service)
    _, step = rearmed(current, service)
    assert step.warnings == (NOTHING_REARMED,) and changes_of(step) == {"added": [], "reset": [], "adapted": []}
    assert (step.effects["partner"], step.effects["group"], step.effects["name"]) == (OSHIRORE, CAMPAIGN,
                                                                                      GROUPS[CAMPAIGN])
    assert step.effects["rearmed_at"] == letstd_clock(NOW)
    assert step.effects["rules_version"] == {"before": service.rules.content_version,
                                             "after": service.rules.content_version}
    widened = letstd(OSHIRORE_BOTH)
    _, refreshed = rearmed(current, widened)
    assert refreshed.effects["rules_version"] == {"before": service.rules.content_version,
                                                  "after": widened.rules.content_version}
    assert REARM.availability(SimpleNamespace(letstd=service)) is None
    assert REARM.availability(SimpleNamespace(letstd=None)) == missing_service(LETSTD)
    with pytest.raises(AdminError) as missing:
        rearmed(current, None)
    assert (missing.value.code, missing.value.status) == ("admin_command_unavailable", 503)


def test_page_mirrors_the_server_names_and_keeps_the_label_fixed():
    data = PAGE.read_bytes()
    assert b"\r" not in data
    text = data.decode("utf-8")
    assert set(VIEW_CONSTANT.findall(text)) == set(VIEW_NAMES)
    assert page_constant(text, "LOCAL_LABEL") == LOCAL_LABEL
    assert page_constant(text, "DOMAIN") == LETSTD and page_constant(text, "LEDGER_KEY") == LEDGER_KEY
    assert page_constant(text, "REARM_OPERATION") == REARM.operation
    assert page_constant(text, "SOURCES") == list(SOURCES)
    assert page_constant(text, "PAGE_LIMIT") <= MAX_PAGE_ITEMS
    assert "无法加本地标签" in page_constant(text, "NATIVE_POPUP_NOTE")


HARNESS = r'''
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [root, source] = process.argv.slice(2);
const payloads = JSON.parse(readFileSync(source, "utf-8"));

class FakeNode {
    constructor() { this.parentNode = null; this.childNodes = []; this.listeners = new Map(); }
    addEventListener(type, listener) { this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]); }
    removeEventListener(type, listener) {
        this.listeners.set(type, (this.listeners.get(type) ?? []).filter((item) => item !== listener));
    }
    dispatchEvent(event) {
        for (let node = this; node; node = event.bubbles ? node.parentNode : null) {
            for (const listener of [...(node.listeners.get(event.type) ?? [])]) listener.call(node, event);
        }
        return true;
    }
}

class FakeText extends FakeNode {
    constructor(text) { super(); this.data = String(text); }
    get nodeType() { return 3; }
    get textContent() { return this.data; }
}

class FakeElement extends FakeNode {
    constructor(tag) {
        super();
        Object.assign(this, { localName: tag, attributes: new Map(), dataset: {}, className: "", disabled: false,
            open: false, value: "" });
    }
    get nodeType() { return 1; }
    append(...nodes) {
        nodes.forEach((node) => {
            if (node === null || node === undefined) throw new TypeError(`append received ${node}`);
            this.appendChild(typeof node === "string" ? new FakeText(node) : node);
        });
    }
    appendChild(node) {
        node.parentNode?.removeChild(node);
        node.parentNode = this;
        this.childNodes.push(node);
        return node;
    }
    removeChild(node) {
        this.childNodes = this.childNodes.filter((child) => child !== node);
        node.parentNode = null;
        return node;
    }
    replaceChildren(...nodes) {
        this.childNodes.forEach((child) => { child.parentNode = null; });
        this.childNodes = [];
        this.append(...nodes);
    }
    remove() { this.parentNode?.removeChild(this); }
    get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
    set textContent(value) { this.replaceChildren(String(value)); }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    focus() {}
    click() { if (!this.disabled) this.dispatchEvent({ type: "click", bubbles: true }); }
}

globalThis.document = {
    body: new FakeElement("body"), hidden: false, createElement: (tag) => new FakeElement(tag),
    addEventListener() {}, removeEventListener() {},
};
globalThis.location = { hash: `#token=${"t".repeat(43)}`, pathname: "/admin/", search: "" };
globalThis.history = { replaceState() { location.hash = ""; } };

const requests = [];
const reply = (body, headers = {}) => new Response(JSON.stringify(body),
    { status: 200, headers: { "Content-Type": "application/json", ...headers } });
globalThis.fetch = async (url, init) => {
    const address = new URL(String(url), "http://console.test");
    const path = address.pathname.replace("/admin/api/v1", "");
    requests.push({ path, params: address.searchParams.get("params"), etag: init.headers["If-None-Match"] ?? null,
        body: init.body === undefined ? null : JSON.parse(init.body) });
    if (path === "/commands") return reply(payloads.commands);
    if (path === "/plans") return reply(payloads.plan);
    const name = decodeURIComponent(path.replace("/views/", ""));
    return reply({ view: name, revision: 3, generation_id: "gen-letstd", server_now: payloads.now,
        data: payloads.views[name] }, { ETag: '"3:gen-letstd"' });
};

async function until(condition) {
    const pause = () => new Promise((resolve) => setTimeout(resolve, 2));
    for (let index = 0; index < 1000 && !condition(); index += 1) await pause();
    return condition();
}
function all(node, predicate, found = []) {
    if (node.nodeType === 1 && predicate(node)) found.push(node);
    (node.childNodes ?? []).forEach((child) => all(child, predicate, found));
    return found;
}
const find = (node, predicate) => all(node, predicate)[0] ?? null;
const action = (node, name) => find(node, (item) => item.dataset.action === name);
const datasets = (node, kind, ...names) => all(node, (item) => item.className === kind)
    .map((item) => names.map((name) => item.dataset[name]));
const load = (name) => import(pathToFileURL(`${root}/${name}`).href);

const { h } = await load("diff-view.js");
const page = (await load("pages/letstd.js")).default;
const stop = new AbortController();
const mounted = h("div");
const navigations = [];
document.body.append(mounted);
page.mount(mounted, { signal: stop.signal, params: new URLSearchParams(), session: () => null,
    navigate: (name, params) => navigations.push([name, params ?? null]) });
const loaded = await until(() => all(mounted, (item) => item.className === "record").length === 6
    && find(mounted, (item) => item.dataset.switch === "native_popup") !== null
    && find(mounted, (item) => item.dataset.operation === "letstd.rearm") !== null
    && mounted.textContent.includes("可投递"));
const results = {
    page: { title: page.title, order: page.order, mount: typeof page.mount, frozen: Object.isFrozen(page) }, loaded,
    banner: { className: mounted.childNodes[0].className, text: mounted.childNodes[0].textContent },
    popupSwitch: find(mounted, (item) => item.dataset.switch === "native_popup")?.textContent ?? null,
    records: datasets(mounted, "record", "partner", "group"),
    firstRecord: find(mounted, (item) => item.className === "record")?.textContent ?? null,
    polled: requests.filter((item) => item.path.startsWith("/views/")).map((item) => [item.path, item.params]),
};
action(mounted, "edit-switches").click();
action(mounted, "rearm").click();
await until(() => requests.some((item) => item.path === "/plans"));
results.navigations = navigations;
results.plan = requests.find((item) => item.path === "/plans").body;
action(mounted, "project").click();
await until(() => mounted.textContent.includes("原生弹窗预览"));
const projected = requests.find((item) => item.path === "/views/letstd.projection");
results.projection = { params: projected.params, etag: projected.etag,
    windows: datasets(mounted, "mode", "partner", "window") };
stop.abort();
console.log(JSON.stringify(results));
process.exit(0);
'''


def page_payloads(data):
    service = letstd(OSHIRORE_BOTH, native_popup=True)
    state = delivered(armed(service, skip=(ADDED,)), 2, 3)
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_letstd.register(commands)
    services = SimpleNamespace(letstd=service)
    plan = {"plan_id": "p-1", "plan_digest": HASH_C, "committable": True, "expected_revision": 3, "touched": [LETSTD],
            "writes": [], "steps": [], "confirmations": [], "diff": {"entries": [], "total": 0},
            "preflight": {"ok": True, "readback": {}}, "restart_check": {"ok": True}}
    return {"views": {name: read(name, state, data=data, letstd=service) for name in VIEW_NAMES},
            "commands": {"commands": [describe_command(command, services) for command in commands]},
            "plan": plan, "now": {"utc": NOW_UTC, "jst": NOW.isoformat()}}


@pytest.fixture(scope="module")
def mounted_page(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not available to run the console page")
    workspace = tmp_path_factory.mktemp("letstd-page")
    copied = workspace / "ui"
    shutil.copytree(UI_DIRECTORY, copied, ignore=shutil.ignore_patterns("__pycache__"))
    (copied / "package.json").write_bytes(b'{"type": "module"}\n')
    harness, source = workspace / "harness.mjs", workspace / "payloads.json"
    payloads = page_payloads(data_dir(workspace))
    harness.write_bytes(HARNESS.encode("utf-8"))
    source.write_bytes(json.dumps(payloads, ensure_ascii=False).encode("utf-8"))
    completed = subprocess.run([node, str(harness), str(copied), str(source)], capture_output=True,
                               timeout=NODE_TIMEOUT, cwd=workspace)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    results = json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])
    return SimpleNamespace(results=results, payloads=payloads)


def test_the_page_opens_with_the_fixed_local_simulation_label(mounted_page):
    results = mounted_page.results
    assert results["page"] == {"title": "LetsTD 本地模拟", "order": 110, "mount": "function", "frozen": True}
    assert results["loaded"] is True
    banner = results["banner"]
    assert "notice-warn" in banner["className"] and LOCAL_LABEL in banner["text"] and "本地模拟" in banner["text"]
    assert "native_popup" in results["popupSwitch"] and "无法加本地标签" in results["popupSwitch"]
    assert results["records"] == [[partner, str(group)] for partner in PARTNERS for group in GROUPS]


def test_the_page_names_the_retry_and_fingerprint_states_of_an_armed_group(mounted_page):
    record = mounted_page.results["firstRecord"]
    assert "下次结算重试" in record and "与当前一致" in record


def test_the_page_polls_its_views_and_sends_rearm_and_projection_requests(mounted_page):
    results = mounted_page.results
    page_window = json.dumps({"offset": 0, "limit": page_constant(PAGE.read_text("utf-8"), "PAGE_LIMIT")},
                             separators=(",", ":"))
    assert len(results["polled"]) == 3 and dict(map(tuple, results["polled"])) == {
        "/views/letstd.overview": None, "/views/letstd.ledger": page_window,
        "/views/letstd.deliverability": page_window}
    assert results["navigations"] == [["config", None]]
    assert results["plan"] == {"commands": [{"operation": REARM.operation,
                                             "params": {"partner": OSHIRORE, "group": CAMPAIGN}}]}
    projection = results["projection"]
    # A projection depends on the server time, so it is never read through a cached revision tag.
    assert json.loads(projection["params"]) == {"source": SOURCES[0]} and projection["etag"] is None
    windows = mounted_page.payloads["views"]["letstd.projection"]["popups"]["windows"]
    assert windows and projection["windows"] == [[window["partner"], str(window["window"])] for window in windows]


def test_live_default_pools_mail_every_row_and_the_projection_leaves_the_save_alone(bundle, native_sources):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    state = bundle.initialize(bootstrap.initial_state())
    service = LetsTDService(LetsTDCatalog(native_sources.__getitem__), copy(bundle.presents),
                            LetsTDRules(partners=ALL_GROUPS, native_popup=True))
    rows = len(service.catalog.rows)
    checks = read("letstd.deliverability", state, PageParams(limit=MAX_PAGE_ITEMS), letstd=service)
    assert checks["outcomes"] == {Outcome.DELIVERABLE: rows}
    assert checks["enabled_waiting"] == {"rows": rows, "mails": DEFAULT_MAILS, "fits": True,
                                         "statuses": {RowStatus.UNARMED: rows}, "outcomes": {Outcome.DELIVERABLE: rows}}
    before = json_digest(state)
    projection = read("letstd.projection", state, letstd=service)
    assert json_digest(state) == before
    assert len(projection["grants"]) == rows and sum(grant["mails"] for grant in projection["grants"]) == DEFAULT_MAILS
    assert projection["mailbox"] == {"before": 0, "after": DEFAULT_MAILS, "capacity": MAX_PRESENTS}
    assert projection["armed"] == [{"partner": partner, "group": group} for partner in PARTNERS for group in GROUPS]
    assert projection["changes"] == []
    popups = projection["popups"]
    assert popups["shown"] and [row["ServiceId"] for row in popups["reward_param"]] == list(PARTNERS)
    # With every group on, one 115 opens more than a dozen modal popups, so the page advises one group at a time.
    assert len(popups["windows"]) > 12

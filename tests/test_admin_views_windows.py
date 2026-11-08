from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps import admin_views_windows
from aigis_ps.admin_diff import json_digest
from aigis_ps.admin_registry import ViewRegistry
from aigis_ps.admin_views import PageParams
from aigis_ps.admin_views_windows import BASE_CHANGED, CONFIG_KEY, RESOURCES_UNAVAILABLE, base_resource_reader
from aigis_ps.admin_views_presents import mailbox_view
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_window_revival import (ALLOWED_CATEGORIES, MAX_RULE_BYTES, WINDOW_POLICY, MissionWindowRules,
                                             build_mission_window_revival)
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.storage import PlayerProfile


CLIENT_HASH = CURRENT_CLIENT_SHA256
PROJECT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, 12, tzinfo=JST)
TRAINING = 13
PERMANENT_TRAINING, ENDED_TRAINING = "Training:910001", "Training:910002"
SUBJUGATION_BATCH = "Subjugation:400183"
SUBJUGATION_MEMBERS = [400183, 400212]
CAMPAIGN_MISSION = 700101
REOPENED = {"start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"}
REOPENED_RULES = MissionWindowRules(True, {SUBJUGATION_BATCH: REOPENED}, {str(CAMPAIGN_MISSION): REOPENED})
VIEW = admin_views_windows.VIEWS[0]


@pytest.fixture(scope="module")
def resources():
    root = PROJECT / "runtime/resources"
    manifest, entries = ResourceLibrary(root, client_hash=CLIENT_HASH).load()
    return SimpleNamespace(version=manifest["version"],
                           assets=IndexedAssetService(entries, root, PROJECT.parent / "aigisr_exe"))


def view_bundle(resources, rules=None, report=None, error=None, base_version=None):
    return SimpleNamespace(
        native_cdn=SimpleNamespace(assets=resources.assets, base_version=base_version or resources.version),
        masters=SimpleNamespace(expected_hash=CLIENT_HASH),
        mission_window_revival=None if rules is None else SimpleNamespace(rules=rules),
        mission_windows=report, mission_window_error=error)


def view_context(bundle, state=None, now=NOW):
    held = {} if state is None else state
    profile = PlayerProfile(player_id="1", user_key="0" * 32, method_key="0" * 32, state=held, revision=0)
    return SimpleNamespace(services=SimpleNamespace(), profile=profile, generation=SimpleNamespace(bundle=bundle),
                           now=now, state=held, revision=0)


def read(context):
    before = deepcopy(context.state)
    digest = json_digest(before)
    result = VIEW.read(context, None)
    assert context.state == before and json_digest(context.state) == digest and context.revision == 0
    return result


def batches_of(result):
    return {batch["key"]: batch for section in result["categories"] for batch in section["batches"]}


def campaigns_of(result):
    return {mission["mission_id"]: mission for mission in result["growth_campaigns"]["missions"]}


def test_the_view_registers_under_the_battle_domain():
    registry = ViewRegistry()
    admin_views_windows.register(registry)
    [view] = list(registry)
    assert (view.name, view.domain, view.live) == ("battle.windows", "battle", True)
    assert callable(getattr(view.read, "__wrapped__", None))


def test_native_and_effective_windows_are_listed_side_by_side_per_category(resources):
    result = read(view_context(view_bundle(resources)))
    assert result["ok"] is True and result["enabled"] is False and result["rules_hash"] is None
    assert result["window_policy"] == WINDOW_POLICY
    assert [section["category"] for section in result["categories"]] == list(ALLOWED_CATEGORIES)
    for section in result["categories"]:
        assert section["table"].endswith("MissionConfig.atb") and isinstance(section["support"], dict)
        for batch in section["batches"]:
            assert batch["configured"] is False and batch["window"] == batch["original"]
            assert batch["state"] == batch["original_state"]
    batches = batches_of(result)
    assert sorted(member["mission_id"] for member in batches[SUBJUGATION_BATCH]["members"]) == SUBJUGATION_MEMBERS
    assert batches[SUBJUGATION_BATCH]["state"] == "ended"
    assert batches[PERMANENT_TRAINING]["original"] == {"start_at": None, "end_at": None}
    assert batches[PERMANENT_TRAINING]["state"] == "open"
    assert batches[ENDED_TRAINING]["state"] == "ended"
    training = next(section for section in result["categories"] if section["category"] == TRAINING)
    assert all(entry["mission_category"] == TRAINING for entry in training["support"].values())


def test_configured_batches_and_campaigns_report_the_effective_window_beside_the_native_one(resources):
    result = read(view_context(view_bundle(resources, rules=REOPENED_RULES)))
    assert result["ok"] is True and result["enabled"] is True
    assert result["rules_hash"] == REOPENED_RULES.content_version
    reopened = batches_of(result)[SUBJUGATION_BATCH]
    assert reopened["configured"] is True and reopened["window"] == REOPENED
    assert (reopened["state"], reopened["original_state"]) == ("open", "ended")
    assert batches_of(result)[PERMANENT_TRAINING]["configured"] is False
    campaign = campaigns_of(result)[CAMPAIGN_MISSION]
    assert campaign["configured"] is True and campaign["window"] == REOPENED
    assert (campaign["state"], campaign["original_state"]) == ("open", "ended")
    untouched = campaigns_of(result)[CAMPAIGN_MISSION + 1]
    assert untouched["configured"] is False and untouched["window"] == untouched["original"]
    assert untouched["state"] == untouched["original_state"] == "ended"


@pytest.mark.parametrize("moment, state", [
    (datetime(2026, 11, 1, 12, tzinfo=JST), "upcoming"),
    (datetime(2026, 11, 2, 14, 59, 59, tzinfo=JST), "upcoming"),
    (datetime(2026, 11, 2, 15, tzinfo=JST), "open"),
    (datetime(2026, 11, 16, 10, 59, 58, tzinfo=JST), "open"),
    (datetime(2026, 11, 16, 10, 59, 59, tzinfo=JST), "ended"),
])
def test_window_states_turn_on_the_inclusive_start_and_exclusive_end(resources, moment, state):
    result = read(view_context(view_bundle(resources, rules=REOPENED_RULES), now=moment))
    assert batches_of(result)[SUBJUGATION_BATCH]["state"] == state
    assert campaigns_of(result)[CAMPAIGN_MISSION]["state"] == state


def test_growth_campaigns_list_their_quests_on_the_client_clock(resources):
    result = read(view_context(view_bundle(resources)))
    campaigns = result["growth_campaigns"]
    assert campaigns["table"] == "GrowthCampaignConfig.atb" and campaigns["clock"].endswith("+09:00")
    assert campaigns["missions"]
    for mission in campaigns["missions"]:
        assert mission["quests"] and mission["configured"] is False
        assert mission["window"] == mission["original"]


def test_the_preset_document_passes_load_and_build_validation(resources):
    bare = read(view_context(view_bundle(resources)))["preset"]
    assert (bare["config_key"], bare["tier"], bare["max_bytes"]) == (CONFIG_KEY, "content", MAX_RULE_BYTES)
    assert bare["document"] == {"enabled": False, "batches": {}}
    configured = read(view_context(view_bundle(resources, rules=REOPENED_RULES)))["preset"]
    assert configured["document"] == REOPENED_RULES.to_dict()
    assert len(json.dumps(configured["document"]).encode("utf-8")) <= MAX_RULE_BYTES
    rebuilt = MissionWindowRules(**configured["document"])
    assert rebuilt == REOPENED_RULES
    reader = base_resource_reader(view_bundle(resources))
    revival = build_mission_window_revival(reader, rebuilt)
    assert sorted(revival.replacements) == ["GrowthCampaignConfig.atb", "SubjugationMissionConfig.atb"]


def test_the_view_refuses_a_base_index_that_left_the_running_generation(resources):
    result = read(view_context(view_bundle(resources, base_version="f" * 40)))
    assert result["ok"] is False and result["error"]["code"] == BASE_CHANGED


def test_unreadable_base_resources_report_their_own_code(resources):
    bundle = view_bundle(resources)
    bundle.masters = SimpleNamespace(expected_hash="0" * 64)
    result = read(view_context(bundle))
    assert result["ok"] is False and result["error"]["code"] == RESOURCES_UNAVAILABLE


def test_the_startup_verification_and_its_error_pass_through(resources):
    verification = {"verified": False, "mismatches": [{"key": SUBJUGATION_BATCH}]}
    bundle = view_bundle(resources, rules=REOPENED_RULES, report={"verification": verification},
                         error="quest_windows_mismatch")
    result = read(view_context(bundle))
    assert result["verification"] == verification and result["error"] == "quest_windows_mismatch"
    silent = read(view_context(view_bundle(resources)))
    assert silent["verification"] is None and silent["error"] is None


class BundledPresents:
    templates = {}

    def rows(self, state):
        raise ProtocolError("present_state_invalid", "A summon mail bundles several tickets", 503)

    def bundled_tickets(self, state):
        return (7, 9)


class EmptyPresents(BundledPresents):
    def rows(self, state):
        return []

    def bundled_tickets(self, state):
        return ()


def test_the_mailbox_view_reports_bundled_tickets_beside_a_failing_strict_read():
    context = view_context(SimpleNamespace(presents=BundledPresents()))
    result = mailbox_view(context, PageParams())
    assert result["ok"] is False and result["error"]["code"] == "present_state_invalid"
    assert result["bundled_tickets"] == {"ok": True, "ids": [7, 9]}
    readable = mailbox_view(view_context(SimpleNamespace(presents=EmptyPresents())), PageParams())
    assert readable["ok"] is True and readable["count"] == 0
    assert readable["bundled_tickets"] == {"ok": True, "ids": []}

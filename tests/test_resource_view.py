from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.app import create_app
from aigis_ps.asset_service import (ASSET_CHANGED, ASSET_NOT_CACHED, ASSET_NOT_FOUND, ASSET_TOO_LARGE,
                                    AdditionalAssetService, IndexedAssetService)
from aigis_ps.assets import asset_key
from aigis_ps.command_text_overlay import CommandTextOverrides, load_command_text_overrides
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_revival import MISSION_RESOURCE, PASS_RESOURCE, EventPassRevivalRules
from aigis_ps.gift_catalog import GiftCatalog
from aigis_ps.growth_campaign import CAMPAIGN_RESOURCE
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.mission_window_revival import (GrowthCampaignWindowMismatch, MissionBatch, MissionWindowMismatch,
                                             MissionWindowRevival, MissionWindowRules, RevivedBatch, Window,
                                             verify_growth_campaign_windows, verify_mission_windows)
from aigis_ps.possessions import PossessionService
import aigis_ps.resource_view as resource_view
from aigis_ps.resource_fetch import PC_INDEX_NAME, ResourceEntry, parse_index
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.resource_view import load_resource_view
from aigis_ps.shop_window_closure import CLOSED_WINDOW
from aigis_ps.trade_catalog import SHOP_RESOURCE, TradeCatalog
from test_event_pass_revival import PASS_ID, POINTS
from test_quest_service import bundle


ALWAYS_REPLACED = frozenset(("PresentUnitData.atb", SHOP_RESOURCE))
SUMMON_SHOP = 15
EARLIEST_CLOCK = datetime(1970, 1, 1, tzinfo=JST)
RANCE_WINDOW = {"Reproduce:320180": {"start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"}}
GROWTH_WINDOW = {"700101": {"start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"}}
MISSION_TABLE = "ReproduceMissionConfig.atb"
COMMAND_TABLE = "OnSystemCommand.atb"
COMMAND_EXAMPLE = Path(__file__).resolve().parents[1] / "config/command-text-overrides.example.json"


def test_actual_server_and_catalog_share_one_effective_template_resource(bundle, tmp_path):
    from aigis_ps.cli import DEFAULT_ASSETS, DEFAULT_RESOURCES
    snapshot = bundle.masters.snapshot()
    original_manifest, original_entries = ResourceLibrary(DEFAULT_RESOURCES, client_hash=snapshot.client_hash).load()
    view = load_resource_view(DEFAULT_RESOURCES, DEFAULT_ASSETS, snapshot.client_hash, master_provider=snapshot.get_xml)
    assert view.manifest["version"] != original_manifest["version"]
    assert view.manifest["base_version"] == original_manifest["version"]
    assert view.manifest["version"] == bundle.native_cdn.version == bundle.gifts.catalog.resource_version
    assert view.manifest["version"] == bundle.quests.catalog.resource_version == bundle.bootstrap.asset_version
    assert view.manifest["snapshot"].startswith(view.manifest["version"] + "-")
    assert parse_index(view.index) == view.entries
    assert set(view.replacements) == ALWAYS_REPLACED
    modified = next(entry for entry in view.entries if entry.name == "PresentUnitData.atb")
    old = next(entry for entry in original_entries if entry.name == modified.name)
    assert {entry.name for before, entry in zip(original_entries, view.entries) if before != entry} == ALWAYS_REPLACED
    before = IndexedAssetService(original_entries, DEFAULT_RESOURCES, DEFAULT_ASSETS).read(modified.name)[0]
    after = view.assets.read(modified.name)[0]
    assert parse_table(decompress_all4(before))["record_count"] == 2039
    assert parse_table(decompress_all4(after))["record_count"] == 2043
    assert old.md5 == hashlib.md5(before).hexdigest() and modified.md5 == hashlib.md5(after).hexdigest()
    rows = parse_table(decompress_all4(after))["rows"]
    assert [row["PresentID"] for row in rows[-4:]] == [2000000001, 2000000002, 2000000003, 2000000004]
    assert bundle.presents.initial_unit_template(1605) == 2000000001
    initial = bundle.initialize(bundle.bootstrap.initial_state())
    app = create_app(tmp_path, initial_state=initial, native_cdn=bundle.native_cdn, assets=bundle.native_cdn.assets)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        index = client.get(f"/client-assets/{view.manifest['version']}/{PC_INDEX_NAME}")
        assert index.status_code == 200 and index.content == view.index
        content = client.get(f"/client-assets/{modified.group}/{modified.md5}")
        assert content.status_code == 200 and content.content == after
        assert client.get("/assets/PresentUnitData.atb").content == after
        assert client.get(f"/client-assets/{old.group}/{old.md5}").status_code == 404
    assert IndexedAssetService(original_entries, DEFAULT_RESOURCES, DEFAULT_ASSETS).read(modified.name)[0] == before


def test_memory_replacement_keeps_disk_content_and_cannot_bypass_effective_hash(tmp_path):
    name, content, original = "Resource.atb", b"ALTB replacement", b"old disk cache"
    entry = ResourceEntry(name, "a" * 40, hashlib.md5(content).hexdigest(), 5, len(content), asset_key(name))
    (tmp_path / ".cache").mkdir()
    disk = tmp_path / ".cache" / entry.cache_key
    disk.write_bytes(original)
    service = IndexedAssetService((entry,), tmp_path, tmp_path, replacements={name: content})
    assert service.read(name)[0] == content and disk.read_bytes() == original
    with pytest.raises(TypeError):
        service.replacements[entry.cache_key] = b"changed"
    with pytest.raises(ProtocolError) as caught:
        service.read_entry(replace(entry, size=entry.size + 1))
    assert caught.value.code == ASSET_CHANGED
    for replacements in ({name: b"wrong"}, {"Missing.atb": content}, {name: bytearray(content)}, []):
        with pytest.raises(ValueError):
            IndexedAssetService((entry,), tmp_path, tmp_path, replacements=replacements)


def uncached_service(directory):
    name, content = "Uncached.atb", b"ALTB not cached yet"
    entry = ResourceEntry(name, "a" * 40, hashlib.md5(content).hexdigest(), 5, len(content), asset_key(name))
    return IndexedAssetService((entry,), directory, directory), entry, content


def refusal(read, *args):
    with pytest.raises(ProtocolError) as caught:
        read(*args)
    return caught.value.code, caught.value.status


def test_indexed_resources_without_a_cached_copy_are_not_cached_rather_than_absent(tmp_path):
    service, entry, content = uncached_service(tmp_path)
    assert refusal(service.read, entry.name) == refusal(service.read, entry.cache_key) == (ASSET_NOT_CACHED, 404)
    assert refusal(service.read_entry, entry) == (ASSET_NOT_CACHED, 404)
    assert refusal(service.read, "Absent.atb") == (ASSET_NOT_FOUND, 404)
    additional = SimpleNamespace(read=lambda name: pytest.fail("Indexed names never fall back to the manifest"))
    assert refusal(AdditionalAssetService(service, additional).read, entry.name) == (ASSET_NOT_CACHED, 404)
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / entry.cache_key).write_bytes(content)
    assert service.read(entry.name)[0] == service.read_entry(entry)[0] == content


def test_assets_route_answers_the_not_cached_code_apart_from_absent_names(tmp_path):
    service, entry, _ = uncached_service(tmp_path / "resources")
    with TestClient(create_app(tmp_path / "save", assets=service), base_url="http://127.0.0.1") as client:
        uncached, absent = client.get(f"/assets/{entry.name}"), client.get("/assets/Absent.atb")
    assert (uncached.status_code, uncached.json()["error"]["code"]) == (404, ASSET_NOT_CACHED)
    assert (absent.status_code, absent.json()["error"]["code"]) == (404, ASSET_NOT_FOUND)


def test_resource_refusal_codes_keep_their_wire_values():
    assert (ASSET_NOT_FOUND, ASSET_NOT_CACHED, ASSET_TOO_LARGE, ASSET_CHANGED) == (
        "asset_not_found", "asset_not_cached", "asset_too_large", "asset_changed")


def test_effective_server_health_satisfies_native_launcher_guard(bundle, tmp_path, monkeypatch):
    from client import prepare_client
    from aigis_ps.cli import service_readiness
    app = create_app(tmp_path, initial_state=bundle.initialize(bundle.bootstrap.initial_state()),
        native_cdn=bundle.native_cdn, status_provider=lambda: service_readiness(bundle))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert len(response.content) <= 65536

    class HealthResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *unused):
            return False

        def read(self, limit):
            return response.content[:limit]

    class Opener:
        def open(self, url, timeout):
            assert url == prepare_client.HEALTH_URL and timeout == 3
            return HealthResponse()

    monkeypatch.setattr(prepare_client.urllib.request, "build_opener", lambda *unused: Opener())
    prepare_client.check_server(resource_version=bundle.native_cdn.version)
    with pytest.raises(prepare_client.ClientError, match="version differs"):
        prepare_client.check_server(resource_version=bundle.native_cdn.base_version)


@pytest.fixture(scope="module")
def local_snapshot():
    from aigis_ps.cli import DEFAULT_RESOURCES
    project = Path(__file__).resolve().parents[1]
    try:
        snapshot = MasterDataStore(project / "runtime/server/masterdata").snapshot()
        ResourceLibrary(DEFAULT_RESOURCES, client_hash=snapshot.client_hash).load()
    except (OSError, ValueError):
        pytest.skip("Verified local client resources and public masters are required")
    return snapshot


@pytest.fixture(scope="module")
def effective_view(local_snapshot):
    from aigis_ps.cli import DEFAULT_ASSETS, DEFAULT_RESOURCES

    def build(**rules):
        return load_resource_view(DEFAULT_RESOURCES, DEFAULT_ASSETS, local_snapshot.client_hash,
                                  master_provider=local_snapshot.get_xml, **rules)
    return build


def served_trade_catalog(view, snapshot, **options):
    from aigis_ps.cli import DEFAULT_ASSETS
    reader = lambda name: view.assets.read(name)[0]
    possessions = PossessionService(DEFAULT_ASSETS, asset_reader=reader)
    cards = GiftCatalog(snapshot, reader, possessions, resource_version=view.manifest["version"])
    return TradeCatalog(cards, possessions, reader, resource_version=view.manifest["version"], **options)


def test_mission_window_rules_only_change_the_effective_view_when_they_patch(effective_view):
    plain, explicit = effective_view(), effective_view(mission_window_rules=None)
    disabled = effective_view(mission_window_rules=MissionWindowRules(False, RANCE_WINDOW))
    for current in (explicit, disabled):
        assert dict(current.manifest) == dict(plain.manifest) and current.index == plain.index
        assert set(current.replacements) == set(plain.replacements) == ALWAYS_REPLACED
    assert plain.mission_window is None and plain.mission_window_metadata is None
    assert disabled.mission_window_metadata["enabled"] is False and disabled.mission_window_metadata["batches"] == []
    revived = effective_view(mission_window_rules=MissionWindowRules(True, RANCE_WINDOW))
    assert revived.manifest["base_version"] == plain.manifest["base_version"]
    assert revived.manifest["version"] != plain.manifest["version"]
    assert set(revived.replacements) == ALWAYS_REPLACED | {MISSION_TABLE}
    assert revived.manifest["local_replacements"] == sorted(revived.replacements)
    assert revived.assets.read(MISSION_TABLE)[0] == revived.replacements[MISSION_TABLE]
    assert revived.base_assets.read(MISSION_TABLE)[0] == plain.assets.read(MISSION_TABLE)[0]
    metadata = revived.mission_window_metadata
    json.dumps(metadata)
    assert metadata["rules"] == MissionWindowRules(True, RANCE_WINDOW).to_dict()
    assert [member["mission_id"] for member in metadata["batches"][0]["members"]] == [320180, 320390, 320424]
    assert metadata["tables"][MISSION_TABLE]["effective_sha256"] == hashlib.sha256(
        revived.replacements[MISSION_TABLE]).hexdigest()
    metadata["batches"].clear()
    assert len(revived.mission_window_metadata["batches"]) == 1
    later = effective_view(mission_window_rules=MissionWindowRules(True, {"Reproduce:320180": {
        "start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-23T10:59:59+09:00"}}))
    assert later.manifest["version"] not in (plain.manifest["version"], revived.manifest["version"])


def test_served_view_passes_verification_and_the_base_view_does_not(effective_view):
    revived = effective_view(mission_window_rules=MissionWindowRules(True, RANCE_WINDOW))
    report = verify_mission_windows(lambda name: revived.assets.read(name)[0], revived.mission_window)
    assert report["verification"]["members"] == 3 and report["verification"]["matches_build"] is True
    with pytest.raises(MissionWindowMismatch, match="quest_windows_mismatch"):
        verify_mission_windows(lambda name: revived.base_assets.read(name)[0], revived.mission_window)


def test_mission_windows_and_event_pass_revival_share_one_overlay_independently(effective_view):
    passes = EventPassRevivalRules(PASS_ID, POINTS)
    windows = MissionWindowRules(True, RANCE_WINDOW)
    revival_only = effective_view(revival_rules=passes)
    windows_only = effective_view(mission_window_rules=windows)
    both = effective_view(revival_rules=passes, mission_window_rules=windows)
    assert set(both.replacements) == set(revival_only.replacements) | set(windows_only.replacements)
    for name in (PASS_RESOURCE, MISSION_RESOURCE):
        assert both.replacements[name] == revival_only.replacements[name]
    assert both.replacements[MISSION_TABLE] == windows_only.replacements[MISSION_TABLE]
    assert both.revival_metadata == revival_only.revival_metadata
    assert both.mission_window_metadata == windows_only.mission_window_metadata
    assert len({both.manifest["version"], revival_only.manifest["version"], windows_only.manifest["version"]}) == 3


def test_mission_windows_refuse_unverified_rules_and_occupied_targets(effective_view, monkeypatch):
    with pytest.raises(ValueError, match="verified mission window rules"):
        effective_view(mission_window_rules=RANCE_WINDOW)
    original = Window(None, None)
    occupied = MissionWindowRevival({}, MissionWindowRules(True, RANCE_WINDOW), (RevivedBatch(MissionBatch(
        "Reproduce:320180", 3, "PresentUnitData.atb", (), original), original),))
    monkeypatch.setattr(resource_view, "build_mission_window_revival", lambda reader, rules: occupied)
    with pytest.raises(ValueError, match=r"another local overlay already replaces: PresentUnitData.atb \(present_templates\)"):
        effective_view(mission_window_rules=MissionWindowRules(True, RANCE_WINDOW))


def test_growth_campaign_windows_patch_the_effective_view_beside_the_batches(effective_view):
    plain = effective_view()
    growth = effective_view(mission_window_rules=MissionWindowRules(True, {}, GROWTH_WINDOW))
    both = effective_view(mission_window_rules=MissionWindowRules(True, RANCE_WINDOW, GROWTH_WINDOW))
    assert set(growth.replacements) == ALWAYS_REPLACED | {CAMPAIGN_RESOURCE}
    assert set(both.replacements) == ALWAYS_REPLACED | {MISSION_TABLE, CAMPAIGN_RESOURCE}
    assert growth.assets.read(CAMPAIGN_RESOURCE)[0] == growth.replacements[CAMPAIGN_RESOURCE] == both.replacements[
        CAMPAIGN_RESOURCE]
    assert growth.base_assets.read(CAMPAIGN_RESOURCE)[0] == plain.assets.read(CAMPAIGN_RESOURCE)[0]
    assert len({plain.manifest["version"], growth.manifest["version"], both.manifest["version"]}) == 3
    metadata = growth.mission_window_metadata
    json.dumps(metadata)
    assert (metadata["batches"], metadata["tables"]) == ([], {})
    assert metadata["growth_campaigns"]["effective_sha256"] == hashlib.sha256(
        growth.replacements[CAMPAIGN_RESOURCE]).hexdigest()
    rules = growth.mission_window.rules
    assert verify_growth_campaign_windows(rules, lambda name: growth.assets.read(name)[0])["verification"]["rows"] == 7
    with pytest.raises(GrowthCampaignWindowMismatch):
        verify_growth_campaign_windows(rules, lambda name: growth.base_assets.read(name)[0])
    batches = effective_view(mission_window_rules=MissionWindowRules(True, RANCE_WINDOW))
    assert "growth_campaigns" not in batches.mission_window_metadata


def test_command_text_overrides_patch_the_effective_view(effective_view):
    plain, example = effective_view(), load_command_text_overrides(COMMAND_EXAMPLE)
    closed = effective_view(command_text_overrides=example)
    assert set(closed.replacements) == ALWAYS_REPLACED | {COMMAND_TABLE}
    assert closed.manifest["version"] != plain.manifest["version"]
    assert closed.manifest["local_replacements"] == sorted(closed.replacements)
    assert closed.assets.read(COMMAND_TABLE)[0] == closed.replacements[COMMAND_TABLE]
    assert closed.base_assets.read(COMMAND_TABLE)[0] == plain.assets.read(COMMAND_TABLE)[0]
    assert closed.command_text_metadata["rules"] == example.to_dict() and plain.command_text_metadata is None
    json.dumps(closed.command_text_metadata)
    empty = effective_view(command_text_overrides=CommandTextOverrides())
    assert dict(empty.manifest) == dict(plain.manifest) and empty.command_text_metadata["rows"] == []
    both = effective_view(command_text_overrides=example, mission_window_rules=MissionWindowRules(True, RANCE_WINDOW))
    assert set(both.replacements) == ALWAYS_REPLACED | {COMMAND_TABLE, MISSION_TABLE}
    assert both.replacements[COMMAND_TABLE] == closed.replacements[COMMAND_TABLE]
    assert both.mission_window_metadata == effective_view(
        mission_window_rules=MissionWindowRules(True, RANCE_WINDOW)).mission_window_metadata
    with pytest.raises(ValueError, match="verified command text overrides"):
        effective_view(command_text_overrides=example.to_dict())


def test_growth_windows_claim_their_table_against_earlier_overlays(effective_view, monkeypatch):
    claimed = SimpleNamespace(replacements={CAMPAIGN_RESOURCE: b"claimed"})
    monkeypatch.setattr(resource_view, "build_package_text_overlay", lambda *unused: claimed)
    with pytest.raises(ValueError, match=r"Mission windows target a resource another local overlay already replaces: "
                                         r"GrowthCampaignConfig.atb \(package_texts\)"):
        effective_view(revival_rules=EventPassRevivalRules(PASS_ID, POINTS),
                       mission_window_rules=MissionWindowRules(True, {}, GROWTH_WINDOW))


def test_two_overlays_on_one_resource_are_refused_with_its_owner(effective_view, monkeypatch):
    claimed = SimpleNamespace(targets=(CAMPAIGN_RESOURCE,), replacements={})
    monkeypatch.setattr(resource_view, "build_command_text_overlay", lambda reader, rules: claimed)
    with pytest.raises(ValueError, match=r"Command text overrides target a resource another local overlay already "
                                         r"replaces: GrowthCampaignConfig.atb \(mission_window\)"):
        effective_view(mission_window_rules=MissionWindowRules(True, {}, GROWTH_WINDOW),
                       command_text_overrides=CommandTextOverrides())
    assert effective_view(command_text_overrides=CommandTextOverrides()).command_texts is claimed


def test_the_summon_shop_window_is_always_closed_in_the_served_view(effective_view):
    view = effective_view()
    closure = view.shop_window_closure
    assert closure.shop_ids == (SUMMON_SHOP,) and view.shop_window_closure_metadata == closure.metadata
    assert view.manifest["local_replacements"] == sorted(ALWAYS_REPLACED)
    served, base = view.assets.read(SHOP_RESOURCE)[0], view.base_assets.read(SHOP_RESOURCE)[0]
    assert served == view.replacements[SHOP_RESOURCE] != base
    assert (hashlib.sha256(base).hexdigest(), hashlib.sha256(served).hexdigest()) == (
        closure.source_sha256, closure.effective_sha256)
    before, after = (parse_table(decompress_all4(payload))["rows"] for payload in (base, served))
    header = closure.products[0].row
    assert closure.products[0].rows == 3 and before[header]["ShopID"] == SUMMON_SHOP
    assert [index for index, (old, new) in enumerate(zip(before, after)) if old != new] == [header]
    assert after[header] == {**before[header], **{name: [value, 0] for name, value in CLOSED_WINDOW.items()}}
    json.dumps(view.shop_window_closure_metadata)


def test_served_trades_see_the_summon_window_ended_and_replay_the_unclosed_identity(effective_view, local_snapshot,
                                                                                    monkeypatch):
    closed_view = effective_view()
    closed = served_trade_catalog(closed_view, local_snapshot, earlier_resources=lambda: closed_view.unclosed_resources)
    summon = closed.product(SUMMON_SHOP)
    assert summon.start_at < summon.end_at <= EARLIEST_CLOCK and not summon.ordinary
    original = resource_view.build_shop_window_closure
    monkeypatch.setattr(resource_view, "build_shop_window_closure",
                        lambda payload: replace(original(payload), replacements=MappingProxyType({})))
    unclosed_view = effective_view()
    assert set(unclosed_view.replacements) == ALWAYS_REPLACED - {SHOP_RESOURCE}
    assert unclosed_view.unclosed_resources == {}
    assert closed_view.unclosed_resources == {unclosed_view.manifest["version"]: {
        SHOP_RESOURCE: closed_view.shop_window_closure.source_sha256}}
    unclosed = served_trade_catalog(unclosed_view, local_snapshot)
    assert (unclosed.product(SUMMON_SHOP).start_at, unclosed.product(SUMMON_SHOP).end_at) == (None, None)
    assert closed.content_version != unclosed.content_version
    assert closed.replayable_content_versions() == {closed.content_version, unclosed.content_version}


def test_the_shop_window_closure_refuses_a_table_an_earlier_overlay_replaced(effective_view, monkeypatch):
    claimed = SimpleNamespace(targets=(), replacements={SHOP_RESOURCE: b"claimed"})
    monkeypatch.setattr(resource_view, "build_command_text_overlay", lambda reader, rules: claimed)
    with pytest.raises(ValueError, match=r"Shop window closures target a resource another local overlay already "
                                         r"replaces: Shop2ItemV2.atb \(command_texts\)"):
        effective_view(command_text_overrides=CommandTextOverrides())

from copy import copy, deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from aigis_ps.errors import ProtocolError
import aigis_ps.event_pass_catalog as event_pass_catalog
from aigis_ps.event_pass_catalog import ITEM_RESOURCE_NAME, EventPassCatalog
from aigis_ps.event_pass_revival import MISSION_RESOURCE, PASS_RESOURCE, EventPassRevivalRules, build_event_pass_revival
from aigis_ps.evolution_catalog import EvolutionCatalog
from aigis_ps.fame_catalog import FameCatalog
import aigis_ps.mission_catalog as mission_catalog
from aigis_ps.mission_catalog import MissionCatalog
from aigis_ps.mission_ep_bindings import MissionEpBindings
from aigis_ps.mission_pass_points import MissionPassPoints
from aigis_ps.mission_service import OPTIONAL_DEPENDENCIES, MissionService
from aigis_ps.mission_state import MissionProgress
from aigis_ps.possessions import PossessionService
from aigis_ps.present_items import PresentItemCatalog
from aigis_ps.storage import Store
from aigis_ps.training_catalog import TrainingCatalog
from test_event_pass_catalog import payloads as pass_payloads
from test_event_pass_revival import PASS_ID, POINTS, resources
from test_item_summon_integration import ITEM as TICKET
from test_mission_service import build, case, changed_102
from test_mission_service_integration import NOW, message, seed
from test_quest_service import bundle


DAILY_LOGIN = 1611
# The real resources configure each of these, so an earlier generation could have served without any of them.
ATTACHED_LATER = ("skins", "fame", "item_tickets", "clear_specs")
OPERATIONS = (pytest.param(58, DAILY_LOGIN, id="claim"), pytest.param(59, 0, id="group"),
              pytest.param(60, 0, id="read"))
FORGED = hashlib.sha256(b"forged").hexdigest()
NESTED = (pytest.param(("fame",), id="fame"), pytest.param(("items",), id="items"),
          pytest.param(("fame", "items"), id="both"))


@pytest.fixture
def bootstrap(bundle):
    result = copy(bundle.bootstrap)
    result.clock = lambda: NOW
    return result


@pytest.fixture(scope="module")
def nested_absent(bundle):
    fame, read = bundle.missions.fame, lambda name: bundle.native_cdn.assets.read(name)[0]
    growth, evolution = fame.growth, fame.evolution
    training = TrainingCatalog(growth.cards, bundle.player_state, read, growth.aliases, rules=growth.rules,
                               silver_house=growth.silver_house)
    unfarmed = EvolutionCatalog(training, evolution.possessions, evolution.materials, rules=evolution.rules)
    return SimpleNamespace(fame=FameCatalog(training, unfarmed, read),
                           items=PresentItemCatalog(bundle.presents.possessions, read(ITEM_RESOURCE_NAME)))


def configured(bundle, bootstrap, without=(), *, fame=None, items=None):
    real = bundle.missions
    fame = real.fame if fame is None else fame
    presents = copy(bundle.presents) if "item_tickets" in without or items is not None else bundle.presents
    if "item_tickets" in without:
        presents.item_tickets = None
    if items is not None:
        presents.item_catalog = items
    progress = real.progress
    return MissionService(bootstrap, bundle.player_state, presents, real.catalog,
        progress=MissionProgress(real.catalog, dgp_login=progress.dgp_login,
            skin_items=() if "skins" in without else progress.skin_items,
            fame_cards=() if "fame" in without else progress.fame_cards,
            fame_support_cards=() if "fame" in without else progress.fame_support_cards,
            clear_specs=None if "clear_specs" in without else progress.clear_specs),
        skins=None if "skins" in without else real.skins, fame=None if "fame" in without else fame,
        fame_status=None if "fame" in without else real.fame_status,
        ep_bindings=real.ep_bindings, pass_points=real.pass_points)


def masked_nested(bundle, bootstrap, nested_absent, names):
    return configured(bundle, bootstrap, fame=nested_absent.fame if "fame" in names else None,
                      items=nested_absent.items if "items" in names else None)


def committed(service, bundle, tmp_path, operation=58, identifier=DAILY_LOGIN):
    store = Store(tmp_path / "missions.sqlite3", service.record(seed(bundle, service), "login", {}, NOW))
    envelope = message(store, operation, identifier)
    payload = service.handlers()[operation](envelope, store)
    saved = store.profile()
    assert saved.state["mission_history"]["receipts"][-1]["operation"] == operation
    return store, envelope, payload, saved


def replace_without_revision(store, state):
    with store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def forge(store, content):
    state = deepcopy(store.profile().state)
    state["mission_history"]["receipts"][-1]["content"] = content
    replace_without_revision(store, state)


def stale(service, envelope, store):
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        service.handlers()[envelope.profile.registration_keys[0]](envelope, store)
    assert (caught.value.code, caught.value.status) == ("mission_retry_stale", 409)
    assert store.profile() == before


def event_pass_points(tmp_path):
    resources = pass_payloads()
    possessions = PossessionService(tmp_path, asset_reader=resources.__getitem__)
    return MissionPassPoints(SimpleNamespace(catalog=EventPassCatalog(resources.__getitem__, possessions),
                                             possessions=possessions))


def awarding_ep(case):
    return changed_102(case, event_pass_item_id="normal", event_pass_point=1)


def test_real_resources_attach_every_dependency_a_receipt_may_predate(bundle, bootstrap):
    attached = configured(bundle, bootstrap)
    assert set(ATTACHED_LATER) <= set(attached._attached()) <= set(OPTIONAL_DEPENDENCIES)
    fames = len(attached.fame.replayable_content_versions())
    items = len(attached.presents.item_catalog.replayable_content_versions())
    assert fames > 1 and items > 1
    # Nested fame versions multiply only the half of the masked subsets that keep fame attached.
    subsets = 2 ** len(attached._attached())
    # Every configuration also replays under the JST window rule.
    assert len(attached.replayable_content_versions()) == 2 * (subsets // 2 * (fames + 1) * items)


@pytest.mark.parametrize("operation, identifier", OPERATIONS)
@pytest.mark.parametrize("without", [*((name,) for name in ATTACHED_LATER), ATTACHED_LATER])
def test_receipt_replays_after_optional_dependencies_attach(bundle, bootstrap, tmp_path, without, operation, identifier):
    earlier, attached = configured(bundle, bootstrap, without), configured(bundle, bootstrap)
    store, envelope, payload, saved = committed(earlier, bundle, tmp_path, operation, identifier)
    assert attached.content_version != earlier.content_version
    assert earlier.content_version in attached.replayable_content_versions()
    assert attached.initialize(deepcopy(saved.state)) == saved.state
    assert attached.handlers()[operation](envelope, store) == payload
    assert store.profile() == saved


def test_unchanged_configuration_replays_without_masking(bundle, bootstrap, tmp_path, monkeypatch):
    service = configured(bundle, bootstrap)
    store, envelope, payload, saved = committed(service, bundle, tmp_path)
    monkeypatch.setattr(service, "_detached", lambda absent: pytest.fail("the committed configuration is current"))
    assert service.claim(envelope, store) == payload
    assert store.profile() == saved


@pytest.mark.parametrize("name", ATTACHED_LATER)
def test_receipt_of_a_removed_dependency_is_stale(bundle, bootstrap, tmp_path, name):
    store, envelope, _, saved = committed(configured(bundle, bootstrap), bundle, tmp_path)
    removed = configured(bundle, bootstrap, (name,))
    assert saved.state["mission_history"]["receipts"][-1]["content"] not in removed.replayable_content_versions()
    stale(removed, envelope, store)
    assert store.profile() == saved


def test_digest_outside_every_configuration_is_stale(bundle, bootstrap, tmp_path):
    attached = configured(bundle, bootstrap)
    store, envelope, _, _ = committed(configured(bundle, bootstrap, ATTACHED_LATER), bundle, tmp_path)
    assert FORGED not in attached.replayable_content_versions()
    forge(store, FORGED)
    stale(attached, envelope, store)


@pytest.mark.parametrize("names", NESTED)
def test_receipt_replays_after_nested_fame_or_item_inputs_attach(bundle, bootstrap, tmp_path, nested_absent, names,
                                                                 monkeypatch):
    earlier, attached = masked_nested(bundle, bootstrap, nested_absent, names), configured(bundle, bootstrap)
    store, envelope, payload, saved = committed(earlier, bundle, tmp_path)
    assert attached.content_version != earlier.content_version
    assert earlier.content_version in attached.replayable_content_versions()
    monkeypatch.setattr(attached, "_detached", lambda absent: pytest.fail("nested variants keep every dependency"))
    assert attached.claim(envelope, store) == payload
    assert store.profile() == saved


def test_replayable_versions_cover_nested_versions_with_and_without_fame(bundle, bootstrap):
    attached = configured(bundle, bootstrap)
    versions = attached.replayable_content_versions()
    items = attached.presents.item_catalog.replayable_content_versions()
    for configuration in (attached, attached._detached(frozenset(("fame",)))):
        content = configuration._verified_content
        fames = {None} if configuration.fame is None else configuration.fame.replayable_content_versions()
        assert {configuration._content({**content, "fame": fame, "items": item})
                for fame in fames for item in items} <= versions


def test_receipt_of_a_removed_nested_input_is_stale(bundle, bootstrap, tmp_path, nested_absent):
    store, envelope, _, saved = committed(configured(bundle, bootstrap), bundle, tmp_path)
    removed = masked_nested(bundle, bootstrap, nested_absent, ("fame", "items"))
    assert saved.state["mission_history"]["receipts"][-1]["content"] not in removed.replayable_content_versions()
    stale(removed, envelope, store)


@pytest.mark.parametrize("name", ("fame", "items"))
def test_nested_version_no_provider_publishes_is_stale(bundle, bootstrap, tmp_path, name):
    attached = configured(bundle, bootstrap)
    store, envelope, _, _ = committed(attached, bundle, tmp_path)
    forged = attached._content({**attached._verified_content, name: FORGED})
    assert forged not in attached.replayable_content_versions()
    forge(store, forged)
    stale(attached, envelope, store)


def test_reply_is_rebuilt_by_the_configuration_of_its_commit(case, bundle, monkeypatch):
    attached = build(bundle, changed_102(case, reward_item_type=22, reward_item_param=2, reward_item_id=TICKET))
    split = attached.presents.preview(attached.service._rewards((102,)), 0)
    assert [(row["Type"], row["Content"]) for row in split] == [(22, 1), (22, 1)]
    plain = copy(attached.presents)
    plain.item_tickets = None
    # The plain mailbox would refuse the ticket; as one mail it yields a reply only the commit's configuration rebuilds.
    known = plain._item_definition
    monkeypatch.setattr(plain, "_item_definition", lambda row: None if row.get("ItemID") == TICKET else known(row))
    earlier = MissionService(attached.bootstrap, bundle.player_state, plain, attached.catalog, progress=attached.progress)
    envelope = message(case.store, 58, 102)
    payload = earlier.claim(envelope, case.store)
    saved = case.store.profile()
    assert [(row["Type"], row["Content"]) for row in plain.rows(saved.state)] == [(22, 2)]
    assert attached.service.claim(envelope, case.store) == payload
    assert case.store.profile() == saved


def test_receipt_replays_after_pass_points_attach(case, bundle, tmp_path):
    catalog = awarding_ep(case)
    earlier = build(bundle, catalog).service
    attached = build(bundle, catalog, pass_points=event_pass_points(tmp_path)).service
    assert attached.content_version != earlier.content_version
    envelope = message(case.store, 58, 101)
    payload = earlier.claim(envelope, case.store)
    saved = case.store.profile()
    assert attached.claim(envelope, case.store) == payload
    assert case.store.profile() == saved


def test_receipt_of_removed_pass_points_is_stale(case, bundle, tmp_path):
    catalog = awarding_ep(case)
    attached = build(bundle, catalog, pass_points=event_pass_points(tmp_path)).service
    envelope = message(case.store, 58, 101)
    attached.claim(envelope, case.store)
    stale(build(bundle, catalog).service, envelope, case.store)


def test_pass_points_without_ep_missions_leave_the_digest_alone(bundle, tmp_path):
    plain, with_points = build(bundle).service, build(bundle, pass_points=event_pass_points(tmp_path)).service
    assert with_points.content_version == plain.content_version
    assert "pass_points" not in with_points._attached()
    assert with_points.replayable_content_versions() == plain.replayable_content_versions()


def awarding_revival(bundle, bootstrap, resources):
    # The revival overlay makes missions award EP, so bindings and pass points join the digest with the catalogs.
    payloads, version = resources
    overlay = build_event_pass_revival(payloads[PASS_RESOURCE], payloads[MISSION_RESOURCE],
                                       EventPassRevivalRules(PASS_ID, POINTS))
    effective = payloads | dict(overlay.replacements)
    catalog = MissionCatalog(effective.__getitem__, resource_version=version)
    passes = SimpleNamespace(catalog=EventPassCatalog(effective.__getitem__, bundle.possessions),
                             possessions=bundle.possessions)
    return MissionService(bootstrap, bundle.player_state, bundle.presents, catalog,
        ep_bindings=MissionEpBindings(MissionCatalog(payloads.__getitem__, resource_version=version), catalog),
        pass_points=MissionPassPoints(passes))


def jst_window_revival(bundle, bootstrap, resources):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mission_catalog, "WINDOW_RULE", mission_catalog.LEGACY_WINDOW_RULE)
        patch.setattr(event_pass_catalog, "ELIGIBILITY_RULE", event_pass_catalog.LEGACY_ELIGIBILITY_RULE)
        return awarding_revival(bundle, bootstrap, resources)


def test_receipt_committed_while_windows_compared_jst_instants_replays(bundle, bootstrap, resources, tmp_path):
    earlier, current = jst_window_revival(bundle, bootstrap, resources), awarding_revival(bundle, bootstrap, resources)
    assert {"ep_bindings", "pass_points"} <= current._verified_content.keys()
    assert earlier.content_version != current.content_version
    assert earlier.content_version in current.replayable_content_versions()
    store, envelope, payload, saved = committed(earlier, bundle, tmp_path)
    assert saved.state["mission_history"]["receipts"][-1]["event_pass_points"]
    assert current.initialize(deepcopy(saved.state)) == saved.state
    assert current.claim(envelope, store) == payload
    assert store.profile() == saved


@pytest.mark.parametrize("name", ["catalog", "progress", "ep_bindings", "pass_points"])
def test_digest_mixing_window_rules_is_stale(bundle, bootstrap, resources, tmp_path, name):
    earlier, current = jst_window_revival(bundle, bootstrap, resources), awarding_revival(bundle, bootstrap, resources)
    store, envelope, _, _ = committed(earlier, bundle, tmp_path)
    content, previous = current._verified_content, earlier._verified_content
    if name in content["dependencies"]:
        inputs = {**content, "dependencies": {**content["dependencies"], name: previous["dependencies"][name]}}
    else:
        inputs = {**content, name: previous[name]}
    assert inputs != content
    mixed = current._content(inputs)
    assert mixed not in current.replayable_content_versions()
    forge(store, mixed)
    stale(current, envelope, store)

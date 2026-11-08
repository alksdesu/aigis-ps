from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
from types import SimpleNamespace

import pytest

import aigis_ps.package_state as module
from aigis_ps.errors import ProtocolError
from aigis_ps.package_state import PackagePurchases, package_identity


NORMAL = "event_pass_20261015"
PREMIUM = "event_pass_premium_20261015"
REQUEST = "a" * 64


@dataclass(frozen=True)
class Offer:
    normal_pass_id: str = NORMAL
    item_id: str = PREMIUM
    package_id: int = 1001
    flag_id: int = 1002
    sku: str = "local_pass_20261015"
    definition_hash: str = package_identity(NORMAL, PREMIUM)


class Possessions:
    def __init__(self):
        self.calls = 0

    def counts(self, state):
        self.calls += 1
        rows = state["tables"]["possessions"]
        if any(type(row["Count"]) is not int or not 0 <= row["Count"] <= 0x7FFFFFFF for row in rows):
            raise ProtocolError("possession_state_invalid", "Bad inventory", 503)
        return {row["ItemID"]: row["Count"] for row in rows}


def make(offers=None):
    return PackagePurchases(SimpleNamespace(offers=tuple(offers or [Offer()]), content_version="b" * 64), Possessions())


def state(count=0):
    return {"bootstrap_schema": 1, "tables": {"possessions": [{"ItemID": PREMIUM, "Count": count}],
        "event_pass_status": [{"ID": NORMAL, "ReceivedNormalPoint": 10, "ReceivedPremiumPoint": 5}]},
        "event_pass_state": {"sentinel": [1]}, "sentinel": {"nested": [1]}}


def mark(service, original=None, request=REQUEST):
    original = service.initialize(state() if original is None else original)
    return service.mark(original, service.catalog.offers[0], request)


def fail_unchanged(original, callback, code=None):
    saved = deepcopy(original)
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert original == saved
    if code:
        assert caught.value.code == code
    return caught.value


def test_identity_is_versioned_normal_premium_pair_only():
    expected = hashlib.sha256(json.dumps({"schema_version": 1, "normal": NORMAL, "premium": PREMIUM},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert package_identity(NORMAL, PREMIUM) == expected
    assert package_identity(NORMAL + "x", PREMIUM) != expected
    assert package_identity(NORMAL, PREMIUM + "x") != expected


@pytest.mark.parametrize("value", [None, 1, True, "", "x" * 257, "あ" * 86, "bad\0text", "\ud800"])
def test_identity_requires_bounded_xml_text(value):
    with pytest.raises(ProtocolError):
        package_identity(value, PREMIUM)
    with pytest.raises(ProtocolError):
        package_identity(NORMAL, value)


def test_empty_state_initialization_only_adds_empty_journal_and_is_isolated():
    service, original = make(), state()
    expected = deepcopy(original)
    result = service.initialize(original)
    assert result.pop("package_purchases") == {"schema_version": 1, "entries": {}}
    assert result == original == expected
    result["sentinel"]["nested"].append(2)
    assert original == expected


@pytest.mark.parametrize("count", [1, 2, 0x7FFFFFFF])
def test_owned_legacy_baseline_never_changes_inventory_or_reward_watermarks(count):
    service, original = make(), state(count)
    result = service.initialize(original)
    assert service.read(result) == {NORMAL: {"identity": Offer().definition_hash, "count": 1,
        "source": "legacy_owned", "request": None}}
    assert {key: value for key, value in result.items() if key != "package_purchases"} == original
    assert service.initialize(result) == result
    fail_unchanged(result, lambda: service.mark(result, Offer(), REQUEST), "package_already_purchased")


def test_first_mark_is_pure_and_exact_request_is_idempotent():
    service = make()
    original = service.initialize(state())
    result = service.mark(original, Offer(), REQUEST)
    assert service.read(original) == {}
    assert service.read(result) == {NORMAL: {"identity": Offer().definition_hash, "count": 1,
        "source": "local_free", "request": REQUEST}}
    assert result["tables"] == original["tables"]
    repeated = service.mark(result, Offer(), REQUEST)
    assert repeated == result and repeated is not result
    repeated["sentinel"]["nested"].append(2)
    assert result["sentinel"] == {"nested": [1]}
    fail_unchanged(result, lambda: service.mark(result, Offer(), "c" * 64), "package_already_purchased")


def test_mark_does_not_reclassify_candidate_grant_as_legacy():
    service = make()
    prepared = service.initialize(state())
    prepared["tables"]["possessions"][0]["Count"] = 1
    calls = service.possessions.calls
    result = service.mark(prepared, Offer(), REQUEST)
    assert service.read(result)[NORMAL]["source"] == "local_free"
    assert service.possessions.calls == calls


def test_mark_requires_initialized_journal():
    service, original = make(), state()
    fail_unchanged(original, lambda: service.mark(original, Offer(), REQUEST), "package_purchase_state_invalid")


def test_metadata_change_does_not_reset_lifetime_limit():
    original = mark(make())
    changed = replace(Offer(), package_id=5001, flag_id=5002, sku="local_renamed")
    service = make([changed])
    assert service.initialize(original) == original
    fail_unchanged(original, lambda: service.mark(original, changed, "c" * 64), "package_already_purchased")
    assert service.content_version == make().content_version


def test_known_entitlement_reassignment_is_rejected():
    original = mark(make())
    changed = replace(Offer(), item_id="other", definition_hash=package_identity(NORMAL, "other"))
    service = make([changed])
    fail_unchanged(original, lambda: service.read(original), "package_purchase_identity_changed")


def test_retired_entries_are_retained_and_restored_catalog_still_blocks_duplicate():
    original = mark(make())
    absent = PackagePurchases(SimpleNamespace(offers=(), content_version="c" * 64), Possessions())
    assert absent.initialize(original) == original
    returned = make([replace(Offer(), package_id=9999)])
    fail_unchanged(original, lambda: returned.mark(original, returned.catalog.offers[0], "c" * 64),
                   "package_already_purchased")


def test_read_is_a_deep_copy():
    service = make()
    original = mark(service)
    result = service.read(original)
    result[NORMAL]["count"] = 50
    assert service.read(original)[NORMAL]["count"] == 1


@pytest.mark.parametrize("ledger", [None, [], {}, {"schema_version": True, "entries": {}},
    {"schema_version": 2, "entries": {}}, {"schema_version": 1, "entries": []},
    {"schema_version": 1, "entries": {}, "extra": 0}])
def test_invalid_ledger_rejected_without_overwrite(ledger):
    service, original = make(), state()
    original["package_purchases"] = ledger
    fail_unchanged(original, lambda: service.initialize(original), "package_purchase_state_invalid")


@pytest.mark.parametrize("field,value", [("identity", "bad"), ("identity", "A" * 64),
    ("count", True), ("count", 0), ("count", 2), ("source", "unknown"),
    ("source", None), ("request", None), ("request", 1), ("request", "A" * 64)])
def test_invalid_local_record_rejected(field, value):
    service = make()
    original = mark(service)
    original["package_purchases"]["entries"][NORMAL][field] = value
    fail_unchanged(original, lambda: service.initialize(original), "package_purchase_state_invalid")


def test_legacy_request_and_unrecognized_record_fields_rejected():
    service = make()
    original = service.initialize(state(1))
    original["package_purchases"]["entries"][NORMAL]["request"] = REQUEST
    fail_unchanged(original, lambda: service.read(original))
    original["package_purchases"]["entries"][NORMAL]["request"] = None
    original["package_purchases"]["entries"][NORMAL]["extra"] = 0
    fail_unchanged(original, lambda: service.read(original))


@pytest.mark.parametrize("count", [True, -1, 0x80000000])
def test_inventory_validation_failure_does_not_create_baseline(count):
    original, service = state(count), make()
    fail_unchanged(original, lambda: service.initialize(original), "possession_state_invalid")


@pytest.mark.parametrize("request_id", [None, "", "1", "A" * 64, 7, True])
def test_mark_request_must_be_sha256(request_id):
    service = make()
    original = service.initialize(state())
    fail_unchanged(original, lambda: service.mark(original, Offer(), request_id))


def test_unknown_offer_cannot_be_marked():
    service = make()
    original = service.initialize(state())
    fail_unchanged(original, lambda: service.mark(original, replace(Offer(), sku="other"), REQUEST),
                   "package_purchase_offer_unknown")


def test_resource_mutation_rejected_and_reported_unready():
    service, original = make(), state()
    service.catalog.offers = (replace(Offer(), sku="changed"),)
    fail_unchanged(original, lambda: service.initialize(original), "package_purchase_definition_changed")
    assert service.status()["ready"] is False


def test_possession_provider_must_offer_public_counts():
    catalog = SimpleNamespace(offers=(Offer(),), content_version="b" * 64)
    with pytest.raises(ProtocolError) as caught:
        PackagePurchases(catalog, SimpleNamespace(_rows=Possessions().counts))
    assert caught.value.code == "package_purchase_state_invalid"
    service = PackagePurchases(catalog, Possessions())
    assert service.read(service.initialize(state(1)))[NORMAL]["source"] == "legacy_owned"
    assert service.possessions.calls == 1


def test_catalog_version_mutation_rejected():
    service = make()
    service.catalog.content_version = "c" * 64
    with pytest.raises(ProtocolError, match="changed"):
        service._ready()


@pytest.mark.parametrize("field,value", [("package_id", 0), ("flag_id", -1), ("flag_id", True),
    ("sku", ""), ("definition_hash", "a" * 64), ("item_id", NORMAL)])
def test_bad_catalog_offer_is_rejected(field, value):
    with pytest.raises(ProtocolError):
        make([replace(Offer(), **{field: value})])


def test_two_passes_require_distinct_request_identity():
    second = Offer("second", "second_premium", 3001, 3002, "local_second", package_identity("second", "second_premium"))
    service = make([Offer(), second])
    original = service.mark(service.initialize(state()), Offer(), REQUEST)
    fail_unchanged(original, lambda: service.mark(original, second, REQUEST), "package_purchase_state_invalid")
    result = service.mark(original, second, "c" * 64)
    assert len(service.read(result)) == 2
    corrupted = deepcopy(result)
    corrupted["package_purchases"]["entries"]["second"]["request"] = REQUEST
    fail_unchanged(corrupted, lambda: service.read(corrupted))


def test_record_limit_preserves_retired_history(monkeypatch):
    monkeypatch.setattr(module, "MAX_PACKAGES", 1)
    service = make()
    original = service.initialize(state())
    original["package_purchases"]["entries"]["retired"] = {
        "identity": "d" * 64, "count": 1, "source": "legacy_owned", "request": None}
    fail_unchanged(original, lambda: service.mark(original, Offer(), REQUEST))
    original["tables"]["possessions"][0]["Count"] = 1
    fail_unchanged(original, lambda: service.initialize(original))


def test_stored_256_byte_identity_stays_loadable():
    legacy = "x" * (module.MAX_ITEM_BYTES + 1)
    service = make()
    original = service.initialize(state())
    original["package_purchases"]["entries"][legacy] = {
        "identity": "d" * 64, "count": 1, "source": "legacy_owned", "request": None}
    assert service.read(original)[legacy]["source"] == "legacy_owned"
    assert service.initialize(original) == original


def test_new_entitlement_requires_identities_that_fit_the_native_cell():
    oversized = "p" * (module.MAX_ITEM_BYTES + 1)
    offer = replace(Offer(), item_id=oversized, definition_hash=package_identity(NORMAL, oversized))
    service = make([offer])
    original = service.initialize(state())
    saved = deepcopy(original)
    with pytest.raises(ProtocolError) as caught:
        service.mark(original, offer, REQUEST)
    assert caught.value.code == module.IDENTITY_UNREPLAYABLE and caught.value.status == 503
    assert original == saved
    fitting = "p" * module.MAX_ITEM_BYTES
    offer = replace(Offer(), item_id=fitting, definition_hash=package_identity(NORMAL, fitting))
    service = make([offer])
    assert service.read(service.mark(service.initialize(state()), offer, REQUEST))[NORMAL]["source"] == "local_free"


def test_status_declares_local_lifetime_policy():
    result = make().status()
    assert result["ready"] is True
    assert result["local_rules"]["purchase_scope"] == "normal_pass_id"
    assert result["local_rules"]["legacy_owned"] == "record_without_grant"
    assert f"{module.MAX_ITEM_BYTES}-byte native cell" in result["identity_policy"]

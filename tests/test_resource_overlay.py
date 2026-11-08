from dataclasses import FrozenInstanceError, replace
import hashlib
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.assets import asset_key
from aigis_ps.resource_fetch import ResourceEntry, ResourceFetchError, parse_index
from aigis_ps.resource_overlay import MAX_REPLACEMENTS, MAX_REPLACEMENT_BYTES, build_resource_overlay
from test_resource_fetch import index_bytes


VERSION = "b" * 40
GROUP = "a" * 40


def entry(name, payload=b"original", *, pc=True, **fields):
    return replace(ResourceEntry(name, GROUP, hashlib.md5(payload).hexdigest(), 8, len(payload),
                                 asset_key(name) if pc else None), **fields)


@pytest.mark.parametrize("pc", [False, True])
def test_replacement_keeps_order_flags_names_and_original_index_format(pc):
    original = (entry("Second.aar", pc=pc, flags=-7), entry("folder/台詞,01.atb", pc=pc),
                entry("First.atb", pc=pc, flags=0x7FFFFFFF))
    raw = index_bytes(original)
    payload = b"new local content"
    result = build_resource_overlay(VERSION, raw, {original[1].name: payload})
    assert result.base_version == VERSION and result.version != VERSION and len(result.version) == 40
    assert result.index != raw and parse_index(raw) == original
    assert parse_index(result.index) == result.entries
    assert tuple(item.name for item in result.entries) == tuple(item.name for item in original)
    assert result.entries[0] is original[0] or result.entries[0] == original[0]
    assert result.entries[2] == original[2]
    updated = result.entries[1]
    assert updated.group == result.version and updated.md5 == hashlib.md5(payload).hexdigest()
    assert updated.size == len(payload) and updated.flags == original[1].flags
    assert updated.cache_key == original[1].cache_key and updated.native_cache_key == original[1].native_cache_key
    assert {item.index_format for item in result.entries} == ({"pc6"} if pc else {"web5"})
    assert result.replacements == {original[1].name: payload}
    assert result.object_overrides == {(result.version, updated.md5): payload}


@pytest.mark.parametrize("replacements", [{}, {"One.atb": b"original"}])
def test_no_effective_changes_return_original_bytes_and_version(replacements):
    original = (entry("One.atb"),)
    raw = b"\xef\xbb\xbf" + index_bytes(original).translate(bytes(value ^ 0xDA for value in range(256)))
    raw = (raw + b"\r\n").translate(bytes(value ^ 0xDA for value in range(256)))
    result = build_resource_overlay(VERSION, raw, replacements)
    assert result.version == result.base_version == VERSION and result.index is raw
    assert result.entries == original and result.replacements == result.object_overrides == {}


def test_effective_result_is_deterministic_and_noop_replacements_do_not_change_identity():
    raw = index_bytes((entry("One.atb"), entry("Two.atb"), entry("Three.atb")))
    first = build_resource_overlay(VERSION, raw, {"One.atb": b"one", "Two.atb": b"two"})
    reordered = build_resource_overlay(VERSION, raw, {"Two.atb": b"two", "Three.atb": b"original", "One.atb": b"one"})
    assert first == reordered
    reapplied = build_resource_overlay(first.version, first.index, {"One.atb": b"one", "Two.atb": b"two"})
    assert reapplied.version == first.version and reapplied.index == first.index and reapplied.replacements == {}


def test_identity_includes_payload_base_version_and_exact_base_index_bytes():
    raw = index_bytes((entry("One.atb"),))
    base = build_resource_overlay(VERSION, raw, {"One.atb": b"one"})
    versions = {base.version,
                build_resource_overlay(VERSION, raw, {"One.atb": b"two"}).version,
                build_resource_overlay("c" * 40, raw, {"One.atb": b"one"}).version,
                build_resource_overlay(VERSION, raw + bytes((10 ^ 0xDA,)), {"One.atb": b"one"}).version}
    assert len(versions) == 4


def test_replacements_and_object_overrides_are_detached_immutable_mappings():
    supplied = {"One.atb": b"local"}
    result = build_resource_overlay(VERSION, index_bytes((entry("One.atb"),)), MappingProxyType(supplied))
    supplied.clear()
    assert result.replacements == {"One.atb": b"local"}
    with pytest.raises(TypeError):
        result.replacements["One.atb"] = b"changed"
    with pytest.raises(TypeError):
        result.object_overrides[next(iter(result.object_overrides))] = b"changed"
    with pytest.raises(FrozenInstanceError):
        result.version = VERSION


def test_same_replacement_object_can_be_shared_by_distinct_names():
    raw = index_bytes((entry("One.atb"), entry("Two.atb")))
    result = build_resource_overlay(VERSION, raw, {"One.atb": b"shared", "Two.atb": b"shared"})
    assert len(result.replacements) == 2 and len(result.object_overrides) == 1
    assert result.entries[0].md5 == result.entries[1].md5


def test_empty_resource_is_valid_and_keeps_size_zero():
    result = build_resource_overlay(VERSION, index_bytes((entry("One.atb"),)), {"One.atb": b""})
    assert result.entries[0].size == 0 and next(iter(result.object_overrides.values())) == b""


@pytest.mark.parametrize("version", [None, True, 1, "", "B" * 40, "a" * 39, "../" + "a" * 40, "a" * 40 + "\n"])
def test_invalid_base_version_is_rejected(version):
    with pytest.raises(ResourceFetchError):
        build_resource_overlay(version, index_bytes((entry("One.atb"),)), {})


@pytest.mark.parametrize("raw", [None, "text", b"", bytearray(b"test"), b"not an index"])
def test_invalid_index_is_not_guessed(raw):
    with pytest.raises(ResourceFetchError):
        build_resource_overlay(VERSION, raw, {})


@pytest.mark.parametrize("replacements", [None, [], [("One.atb", b"new")], b"bytes"])
def test_replacements_require_a_mapping(replacements):
    with pytest.raises(ResourceFetchError):
        build_resource_overlay(VERSION, index_bytes((entry("One.atb"),)), replacements)


@pytest.mark.parametrize("name", [None, 1, "Missing.atb", "../One.atb", "One.atb\nother", "One.atb,other", "One.atb\x00", "a" * 64])
def test_only_exact_indexed_names_are_accepted(name):
    with pytest.raises(ResourceFetchError) as error:
        build_resource_overlay(VERSION, index_bytes((entry("One.atb"),)), {name: b"new"})
    assert error.value.code == "overlay_resource_unknown"


@pytest.mark.parametrize("payload", [None, "string", bytearray(b"new"), memoryview(b"new"), 1])
def test_payload_must_be_immutable_bytes(payload):
    with pytest.raises(ResourceFetchError) as error:
        build_resource_overlay(VERSION, index_bytes((entry("One.atb"),)), {"One.atb": payload})
    assert error.value.code == "invalid_overlay_payload"


def test_payload_limit_is_inclusive_and_applies_even_to_noop(monkeypatch):
    assert MAX_REPLACEMENT_BYTES == 64 * 1024 * 1024
    monkeypatch.setattr("aigis_ps.resource_overlay.MAX_REPLACEMENT_BYTES", 8)
    raw = index_bytes((entry("One.atb", b"x" * 9),))
    assert build_resource_overlay(VERSION, raw, {"One.atb": b"x" * 8}).entries[0].size == 8
    with pytest.raises(ResourceFetchError):
        build_resource_overlay(VERSION, raw, {"One.atb": b"x" * 9})


def test_replacement_count_bound_counts_submitted_noops_too():
    assert MAX_REPLACEMENTS == 256
    entries = tuple(entry(f"Row{i}.atb") for i in range(257))
    raw = index_bytes(entries)
    assert len(build_resource_overlay(VERSION, raw, {item.name: b"new" for item in entries[:256]}).replacements) == 256
    with pytest.raises(ResourceFetchError):
        build_resource_overlay(VERSION, raw, {item.name: b"original" for item in entries})


def test_conflicting_duplicate_names_and_mixed_formats_are_rejected():
    first = entry("One.atb")
    for items in ((first, replace(first, size=1)), (first, entry("Two.atb", pc=False))):
        with pytest.raises(ResourceFetchError):
            build_resource_overlay(VERSION, index_bytes(items), {})


def test_shared_base_object_must_have_one_size():
    first = entry("One.atb", pc=False)
    second = replace(first, name="Two.atb", size=first.size + 1)
    with pytest.raises(ResourceFetchError) as error:
        build_resource_overlay(VERSION, index_bytes((first, second)), {})
    assert error.value.code == "overlay_object_conflict"


def test_name_hash_collision_is_rejected(monkeypatch):
    raw = index_bytes((entry("One.atb", pc=False), entry("Two.atb", pc=False)))
    monkeypatch.setattr("aigis_ps.resource_fetch.asset_key", lambda name: "a" * 64)
    with pytest.raises(ResourceFetchError) as error:
        build_resource_overlay(VERSION, raw, {})
    assert error.value.code == "overlay_name_collision"


def test_distinct_payloads_cannot_alias_one_effective_md5_address(monkeypatch):
    raw = index_bytes((entry("One.atb"), entry("Two.atb")))
    monkeypatch.setattr("aigis_ps.resource_overlay.hashlib.md5", lambda *args, **kwargs: SimpleNamespace(hexdigest=lambda: "f" * 32))
    with pytest.raises(ResourceFetchError) as error:
        build_resource_overlay(VERSION, raw, {"One.atb": b"one", "Two.atb": b"two"})
    assert error.value.code == "overlay_object_collision"


def test_effective_index_is_checked_by_native_parser(monkeypatch):
    raw = index_bytes((entry("One.atb"),))
    calls = []
    def parse(value):
        calls.append(value)
        return parse_index(value) if len(calls) == 1 else ()
    monkeypatch.setattr("aigis_ps.resource_overlay.parse_index", parse)
    with pytest.raises(ResourceFetchError) as error:
        build_resource_overlay(VERSION, raw, {"One.atb": b"new"})
    assert error.value.code == "overlay_index_mismatch" and len(calls) == 2


def test_recorded_native_pc_index_is_preserved_except_replaced_entry():
    path = Path(__file__).resolve().parents[1] / "artifacts/cdn-contract-20261101/pc-historical-manifest.payload.bin"
    if not path.is_file():
        pytest.skip("Recorded native PC index is not available")
    raw = path.read_bytes()
    original = parse_index(raw)
    target = next(item for item in original if item.name == "PresentUnitData.atb")
    result = build_resource_overlay(VERSION, raw, {target.name: b"isolated fixture"})
    assert len(result.entries) == len(original)
    assert [(old.name, old.flags, old.native_cache_key) for old in original] == [
        (new.name, new.flags, new.native_cache_key) for new in result.entries]
    assert [old for old in original if old.name != target.name] == [new for new in result.entries if new.name != target.name]

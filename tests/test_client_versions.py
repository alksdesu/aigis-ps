from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps import barracks_service
from aigis_ps.client_versions import (CURRENT_CLIENT_SHA256, LEGACY_CLIENT_SHA256, SUPPORTED_CLIENT_SHA256,
                                      require_client_version)
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.recovery_service import RecoveryService
from client import prepare_client as client
import test_client_prepare as preparation
from test_image_aliases import command_table


@pytest.mark.parametrize("value", [None, [], {}, "", "a" * 64, CURRENT_CLIENT_SHA256.upper()])
def test_unknown_client_versions_are_rejected(value):
    with pytest.raises(ValueError, match="Unknown"):
        require_client_version(value)
    with pytest.raises(client.ClientError, match="Unknown"):
        client.select_profile(value)


def test_default_contracts_and_profiles_use_current_client(tmp_path):
    assert MasterDataStore(tmp_path).expected_hash == CURRENT_CLIENT_SHA256
    for name in ("master-schemas.json", "protocol-contracts.json"):
        contract = json.loads((client.PROJECT_ROOT / "src/aigis_ps/data" / name).read_text(encoding="utf-8"))
        assert contract["input_sha256"] == CURRENT_CLIENT_SHA256
    assert client.select_profile(CURRENT_CLIENT_SHA256) is client.PROFILE
    assert client.select_profile(LEGACY_CLIENT_SHA256) is client.LEGACY_PROFILE
    assert client.PROFILE.prefix_offset == 0xA6D8F0
    assert client.PROFILE.cdn_offset == 0xA6D90C
    assert client.LEGACY_PROFILE.prefix_offset == 0xA6D674
    assert client.LEGACY_PROFILE.cdn_offset == 0xA6D690
    assert client.PROFILE.resources == client.LEGACY_PROFILE.resources


@pytest.fixture
def versions(monkeypatch):
    case = preparation.ClientPreparationTests()
    case.setUp()
    previous = case.profile
    current_data = b"new\0" + case.data
    current = replace(previous, sha256=client.digest(current_data), size=len(current_data),
                      prefix_offset=previous.prefix_offset + 4, cdn_offset=previous.cdn_offset + 4)
    monkeypatch.setattr(client, "LEGACY_PROFILE", previous)
    monkeypatch.setattr(client, "PROFILE", current)
    monkeypatch.setattr(client, "SUPPORTED_CLIENT_SHA256", frozenset((previous.sha256, current.sha256)))
    try:
        yield case, previous, current, current_data
    finally:
        case.doCleanups()


@pytest.mark.parametrize("current", [False, True])
def test_preparation_selects_source_and_runtime_profile(versions, current):
    case, previous, latest, data = versions
    if current:
        case.profile = latest
        (case.source / client.EXECUTABLE).write_bytes(data)
        case.activate_index("2" * 40)
    result = client.prepare(case.source, case.runtime, case.workspace, resource_root=case.resources)
    manifest = client.verify_runtime(case.runtime, case.workspace)
    assert result["manifest"] == manifest
    assert manifest["source_sha256"] == case.profile.sha256
    assert manifest["patches"] == client.patch_records(case.profile)
    assert client.inspect_source(case.source, case.workspace)["patched"] == (case.runtime / client.EXECUTABLE).read_bytes()


def test_upgrade_verifies_previous_runtime_and_preserves_backup(versions):
    case, previous, latest, data = versions
    case.prepare()
    old_executable = (case.runtime / client.EXECUTABLE).read_bytes()
    case.profile = latest
    (case.source / client.EXECUTABLE).write_bytes(data)
    case.activate_index("2" * 40)
    result = client.prepare(case.source, case.runtime, case.workspace, resource_root=case.resources)
    assert result["manifest"]["source_sha256"] == latest.sha256
    backup = Path(result["backup"])
    assert (backup / client.EXECUTABLE).read_bytes() == old_executable
    assert client.verify_runtime(backup, case.workspace)["source_sha256"] == previous.sha256
    assert client.verify_runtime(case.runtime, case.workspace)["source_sha256"] == latest.sha256
    with pytest.raises(client.ClientError):
        client.verify_runtime(case.runtime, case.workspace, previous)


def test_upgrade_refuses_modified_previous_runtime(versions):
    case, _, latest, data = versions
    case.prepare()
    executable = case.runtime / client.EXECUTABLE
    corrupted = executable.read_bytes()[:-1] + b"!"
    executable.write_bytes(corrupted)
    case.profile = latest
    (case.source / client.EXECUTABLE).write_bytes(data)
    case.activate_index("2" * 40)
    with pytest.raises(client.ClientError):
        client.prepare(case.source, case.runtime, case.workspace, resource_root=case.resources)
    assert executable.read_bytes() == corrupted


def test_current_original_has_only_the_audited_patch_locations():
    executable = client.SOURCE_ROOT / client.EXECUTABLE
    if not executable.is_file():
        pytest.skip("Current original client is unavailable")
    raw = executable.read_bytes()
    assert client.digest(raw) == CURRENT_CLIENT_SHA256
    patched = client.patch_executable(raw)
    changed = {i for i, (before, after) in enumerate(zip(raw, patched)) if before != after}
    ranges = {i for record in client.patch_records(client.PROFILE)
              for i in range(record["offset"], record["offset"] + len(bytes.fromhex(record["original_hex"])))}
    assert changed and changed <= ranges and len(patched) == len(raw)
    for name, expected in client.PROFILE.resources:
        assert client.file_digest(client.SOURCE_ROOT / name) == expected
    with pytest.raises(client.ClientError, match="Unknown"):
        client.patch_executable(raw[:-1] + bytes((raw[-1] ^ 1,)))


def test_service_content_versions_include_the_selected_client(monkeypatch):
    bootstrap = SimpleNamespace(rank_rows=[{"NextExp": 100, "CharismaMax": 30}], clock=lambda: datetime(2026, 11, 6))
    capacity = SimpleNamespace(content_version="c" * 64)
    commands = command_table([])
    monkeypatch.setattr(barracks_service, "_COMMAND_HASHES", {"menu": hashlib.sha256(commands).hexdigest()})
    recoveries = [RecoveryService(bootstrap, client_sha256=value) for value in sorted(SUPPORTED_CLIENT_SHA256)]
    barracks = [barracks_service.BarracksService(bootstrap, None, capacity, lambda _: commands, client_sha256=value)
                for value in sorted(SUPPORTED_CLIENT_SHA256)]
    for services in (recoveries, barracks):
        assert {service.status()["native_client_sha256"] for service in services} == SUPPORTED_CLIENT_SHA256
        assert len({service.content_version for service in services}) == 2
        assert all(service.status()["ready"] for service in services)
    with pytest.raises(ProtocolError) as recovery:
        RecoveryService(bootstrap, client_sha256="a" * 64)
    assert recovery.value.code == "recovery_catalog_invalid"
    with pytest.raises(ProtocolError) as house:
        barracks_service.BarracksService(bootstrap, None, capacity, lambda _: commands, client_sha256="a" * 64)
    assert house.value.code == "barracks_catalog_invalid"

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from client import capture_all, capture_content, capture_masters, capture_resources, capture_responses, capture_schedule
from aigis_ps.assets import resolve_asset


CURRENT_SHA256 = "d0be510fb38b1d6dfe63c3562c97e45f49d318bf7e155dee309d88efe963f92b"


@pytest.fixture(params=["current", "previous"])
def executable(request, tmp_path):
    if request.param == "current":
        path = capture_masters.DEFAULT_EXE
        if not path.is_file():
            pytest.skip("Current original client is unavailable")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == CURRENT_SHA256
        return path, CURRENT_SHA256

    source = capture_masters.PROJECT_ROOT / "runtime" / "client"
    prepared = source / "Aigis_r.exe"
    manifest_path = source / "client-manifest.json"
    if not prepared.is_file() or not manifest_path.is_file():
        pytest.skip("Previous prepared client and restoration manifest are unavailable")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_sha256"] == capture_masters.EXPECTED_SHA256
    data = prepared.read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest["target_sha256"]
    restored = bytearray(data)
    for patch in manifest["patches"]:
        offset = patch["offset"]
        original = bytes.fromhex(patch["original_hex"])
        replacement = bytes.fromhex(patch["replacement_hex"])
        assert type(offset) is int and 0 <= offset <= len(data) - len(replacement)
        assert len(original) == len(replacement)
        assert data[offset:offset + len(replacement)] == replacement
        restored[offset:offset + len(original)] = original
    assert hashlib.sha256(restored).hexdigest() == capture_masters.EXPECTED_SHA256
    path = tmp_path / "Aigis_r.exe"
    path.write_bytes(restored)
    for name in capture_masters.TABLES:
        try:
            schema = resolve_asset(source, name + ".ard")
        except FileNotFoundError:
            pytest.skip("Previous client's public master schemas are unavailable")
        shutil.copyfile(schema, tmp_path / schema.name)
    return path, capture_masters.EXPECTED_SHA256


def test_all_profiles_accept_both_verified_versions_without_loading_frida(executable, monkeypatch):
    path, expected = executable
    monkeypatch.setattr(capture_all.importlib, "import_module", lambda _: pytest.fail("Static profiles must not import Frida"))
    identity, schemas, agents = capture_all._prepare(path)
    assert identity["sha256"] == expected == hashlib.sha256(path.read_bytes()).hexdigest()
    assert expected in capture_masters.SUPPORTED_SHA256
    assert Path(identity["path"]) == path.resolve()
    assert set(schemas) == set(capture_masters.TABLES)
    assert all(capture_masters.TABLES[name]["required"] <= fields for name, fields in schemas.items())
    assert set(agents) == {"masters", "resources", "responses"}
    assert all(agents.values())
    config = capture_resources.capture_config(path)
    assert config["input_sha256"] == expected


@pytest.mark.parametrize("profile", [
    capture_masters.verify_original_executable, capture_resources.capture_config,
    capture_content._prepare, capture_schedule.profile, capture_responses.profile, capture_all._prepare,
])
def test_unknown_executable_hash_is_rejected_by_every_profile(tmp_path, monkeypatch, profile):
    path = tmp_path / "Aigis_r.exe"
    path.write_bytes(b"MZ" + bytes(62))
    assert hashlib.sha256(path.read_bytes()).hexdigest() not in capture_masters.SUPPORTED_SHA256
    monkeypatch.setattr(capture_all.importlib, "import_module", lambda _: pytest.fail("Rejected profiles must not import Frida"))
    with pytest.raises(capture_masters.CaptureError, match="executable_hash_mismatch"):
        profile(path)


@pytest.mark.parametrize("digest", [capture_masters.EXPECTED_SHA256, CURRENT_SHA256])
def test_resource_check_reports_the_verified_input_hash(tmp_path, monkeypatch, capsys, digest):
    executable = tmp_path / "Aigis_r.exe"
    config = {"exePath": str(executable), "input_sha256": digest}
    monkeypatch.setattr(capture_resources, "capture_config", lambda _: config)
    monkeypatch.setattr(capture_resources, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(capture_resources.importlib, "import_module", lambda _: pytest.fail("Check must not import Frida"))
    output = tmp_path / "resources"
    assert capture_resources.main(["--check", "--exe", str(executable), "--output", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "checked" and result["attached"] is False
    assert result["input_sha256"] == digest
    assert not output.exists()

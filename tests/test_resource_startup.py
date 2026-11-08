from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from aigis_ps import cli
from aigis_ps.asset_service import AdditionalAssetService
from aigis_ps.assets import asset_key
from aigis_ps.errors import ProtocolError
from test_quest_service import bundle


@pytest.mark.parametrize("state", ["missing", "incomplete", "unready"])
def test_unready_effective_resources_stop_before_store_or_listener(bundle, tmp_path, monkeypatch, capsys, state):
    if state == "missing":
        broken = replace(bundle, native_cdn=None, resource_error="local template conflict")
    elif state == "incomplete":
        broken = replace(bundle, native_cdn=SimpleNamespace(status=lambda: {"ready": False}))
    else:
        broken = bundle
        monkeypatch.setattr(cli, "service_readiness", lambda _: {"startup_ready": False})
    monkeypatch.setattr(cli, "services", lambda _: broken)
    monkeypatch.setattr(cli, "client_fingerprint", lambda _: bundle.masters.expected_hash)
    monkeypatch.setattr("aigis_ps.app.create_app", lambda *args, **kwargs: pytest.fail("Store must not be created"))
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: pytest.fail("Listener must not be started"))
    directory = tmp_path / "uncreated-server"
    assert cli.main(["serve", "--data-dir", str(directory)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert "resource" in result["message"] if state != "unready" else "startup" in result["message"]
    assert not directory.exists()


def test_legacy_manifest_only_adds_nonindexed_assets(bundle, tmp_path, monkeypatch):
    root = tmp_path / "extra"
    root.mkdir()
    entries = []
    for name, content in (("PresentUnitData.atb", b"ALTB obsolete"), ("Additional.atb", b"ALTB additional")):
        key = asset_key(name)
        (root / key).write_bytes(content)
        entries.append({"magic": "ALTB", "md5": {"status": "match"}, "filename_hash": key,
                        "sha256": hashlib.sha256(content).hexdigest(), "size": len(content), "source_path": key})
    manifest = tmp_path / "assets.json"
    manifest.write_text(json.dumps({"source_root": str(root), "entries": entries}), encoding="utf-8")
    captured = {}
    def create(*args, **kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(cli, "services", lambda _: bundle)
    monkeypatch.setattr(cli, "client_fingerprint", lambda _: bundle.masters.expected_hash)
    monkeypatch.setattr("aigis_ps.app.create_app", create)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    directory = tmp_path / "uncreated-server"
    assert cli.main(["serve", "--data-dir", str(directory), "--asset-manifest", str(manifest)]) == 0
    reader = captured["assets"]
    expected = bundle.native_cdn.assets.read("PresentUnitData.atb")
    assert reader.read("PresentUnitData.atb") == expected
    assert reader.read(asset_key("PresentUnitData.atb")) == expected
    assert reader.read("Additional.atb")[0] == b"ALTB additional"
    assert (root / asset_key("PresentUnitData.atb")).read_bytes() == b"ALTB obsolete"
    assert not directory.exists()


def test_known_index_failure_never_falls_back_to_an_old_manifest():
    def fail(name):
        raise ProtocolError("asset_changed", "fixture", 409)
    indexed = SimpleNamespace(entries={asset_key("Known.atb"): object()}, read=fail)
    additional = SimpleNamespace(read=lambda _: pytest.fail("Known index failure must remain an error"))
    service = AdditionalAssetService(indexed, additional)
    with pytest.raises(ProtocolError) as caught:
        service.read("Known.atb")
    assert caught.value.code == "asset_changed"

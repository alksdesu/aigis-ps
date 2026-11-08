import hashlib
import json
import struct

import pytest

from aigis_ps.al_format import ParseLimits
from aigis_ps.assets import asset_key, export_asset, inventory_assets, recover_names, resolve_asset, write_inventory


def source_asset(tmp_path, name, content):
    root = tmp_path / "source"
    cache = root / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / asset_key(name)
    path.write_bytes(content)
    return root, path


def table_bytes():
    schema = b"ALRD\x01\x00\x01\x00\x04\x00\x00\x00\x01\x08\x02\x00ID\x00\x00" + b"\0" * 8
    header = struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, 1, 20, 20 + len(schema), 4, b"test")
    return header + schema + struct.pack("<i", 42)


def malicious_archive(payload):
    content = bytearray(96)
    struct.pack_into("<4sBBH", content, 0, b"ALAR", 3, 0x41, 1)
    struct.pack_into("<HH", content, 16, 96, 32)
    struct.pack_into("<IIIIH", content, 32, 1, 96, len(payload), 0x80000001, 0)
    content[50:63] = b"../escape.atb"
    return bytes(content) + payload


def test_inventory_hashes_names_sidecars_and_unmapped_files(tmp_path):
    root, path = source_asset(tmp_path, "QuestTable.atb", table_bytes())
    original = path.read_bytes()
    digest = hashlib.md5(original).hexdigest()
    path.with_name(path.name + ".md5").write_text(digest, encoding="utf-8-sig")
    (root / "Aigis_r.exe").write_bytes(b"MZQuestTable.atb\0QuestTable.atb\0Map%04d.aar\0")
    unknown = root / ".cache" / ("f" * 64)
    unknown.write_bytes(b"opaque")
    manifest = inventory_assets(root)
    assert manifest["summary"] == {"files": 2, "bytes": len(original) + 6, "mapped": 1, "unmapped": 1, "md5_mismatches": 0}
    entry = next(item for item in manifest["entries"] if item["name"])
    assert entry["name"] == "QuestTable.atb"
    assert entry["sha256"] == hashlib.sha256(original).hexdigest()
    assert entry["md5"]["status"] == "matched"
    assert path.read_bytes() == original
    output = write_inventory(manifest, tmp_path / "output")
    assert json.loads(output.read_text()) == manifest
    with pytest.raises(FileExistsError):
        write_inventory(manifest, tmp_path / "output")


def test_inventory_does_not_call_missing_name_missing_content(tmp_path):
    root, _ = source_asset(tmp_path, "Unknown.aar", b"ALAR")
    manifest = inventory_assets(root)
    assert manifest["entries"][0]["name"] is None
    assert manifest["summary"]["unmapped"] == 1
    assert "missing" not in manifest["summary"]


def test_sidecar_mismatch_invalid_and_orphan_are_reported(tmp_path):
    root, path = source_asset(tmp_path, "First.atb", b"data")
    path.with_name(path.name + ".md5").write_text("0" * 32)
    _, second = source_asset(tmp_path, "Second.atb", b"data")
    second.with_name(second.name + ".md5").write_bytes(b"\xff")
    (root / ".cache" / ("e" * 64 + ".md5")).write_text("0" * 32)
    manifest = inventory_assets(root)
    assert {item["md5"]["status"] for item in manifest["entries"]} == {"invalid", "mismatch"}
    assert manifest["summary"]["md5_mismatches"] == 1
    assert manifest["diagnostics"][0]["code"] == "orphan_md5"


def test_numbered_name_recovery_requires_executable_format_evidence(tmp_path):
    exe = tmp_path / "Aigis_r.exe"
    exe.write_bytes(b"Map%04d.aar\0")
    names = recover_names(exe, ["Extra.atb"])
    assert names[asset_key("Map1001.aar")][0]["name"] == "Map1001.aar"
    assert names[asset_key("Extra.atb")][0]["evidence"] == "provided_name"
    assert asset_key("PlayerDot0001.aar") not in names


def test_export_table_preserves_raw_data_and_generates_rows(tmp_path):
    data = table_bytes()
    root, source = source_asset(tmp_path, "Test.atb", data)
    report = export_asset(root, "Test.atb", tmp_path / "output")
    assert report["complete"]
    assert report["tables"][0]["record_count"] == 1
    table = json.loads((tmp_path / "output" / report["tables"][0]["path"]).read_text())
    assert table["rows"] == [{"ID": 42}]
    assert source.read_bytes() == data
    assert (tmp_path / "output" / report["tree"]["payload_path"]).read_bytes() == data


def test_unknown_and_malformed_formats_keep_raw_payload_and_diagnostics(tmp_path):
    root, _ = source_asset(tmp_path, "Unknown.aar", b"NEW!opaque")
    report = export_asset(root, "Unknown.aar", tmp_path / "output")
    assert not report["complete"]
    assert report["diagnostics"][0]["code"] == "unsupported_format"
    assert report["tree"]["status"] == "opaque"
    source_asset(tmp_path, "Broken.atb", b"ALTB")
    report = export_asset(root, "Broken.atb", tmp_path / "output")
    assert report["tree"]["status"] == "error"
    assert report["diagnostics"][0]["code"] == "bounds"


@pytest.mark.parametrize("name", ["../x", "/x", "C:\\x", "x/../y", "x:stream", "CON.atb", "x\\y", "x\0y", "x//y", "x."])
def test_asset_resolution_rejects_unsafe_names(tmp_path, name):
    root, _ = source_asset(tmp_path, "Good.atb", b"data")
    with pytest.raises(ValueError, match="Unsafe"):
        resolve_asset(root, name)


def test_malicious_archive_names_cannot_escape_export_directory(tmp_path):
    root, _ = source_asset(tmp_path, "Bad.aar", malicious_archive(table_bytes()))
    report = export_asset(root, "Bad.aar", tmp_path / "output")
    assert any(item["code"] == "unsafe_member_name" for item in report["diagnostics"])
    assert not (tmp_path / "escape.atb").exists()
    assert report["tables"][0]["record_count"] == 1
    assert "member_00000" in report["tables"][0]["path"]


def test_export_rejects_source_output_overlap_and_ambiguous_source(tmp_path):
    root, source = source_asset(tmp_path, "Test.atb", table_bytes())
    with pytest.raises(ValueError, match="separate"):
        export_asset(root, "Test.atb", root / "output")
    (root / source.name).write_bytes(source.read_bytes())
    with pytest.raises(ValueError, match="Ambiguous"):
        resolve_asset(root, "Test.atb")


def test_export_limit_failure_is_retained_in_report(tmp_path):
    root, _ = source_asset(tmp_path, "Bad.aar", malicious_archive(table_bytes()))
    report = export_asset(root, "Bad.aar", tmp_path / "output", ParseLimits(max_members=1))
    assert any(item["code"] == "member_limit" for item in report["diagnostics"])
    assert report["tree"]["children"][0]["status"] == "error"


def test_export_checks_nested_decompression_depth(tmp_path):
    lz4 = pytest.importorskip("lz4.frame")
    data = table_bytes()
    for _ in range(3):
        data = struct.pack("<4sHHI", b"ALL4", 1, 0, len(data)) + lz4.compress(data)
    root, _ = source_asset(tmp_path, "Nested.atb", data)
    report = export_asset(root, "Nested.atb", tmp_path / "output", ParseLimits(max_depth=1))
    assert any(item["code"] == "depth_limit" for item in report["diagnostics"])
    assert not report["complete"]


def test_export_rejects_linked_output_directory(tmp_path):
    root, _ = source_asset(tmp_path, "Test.atb", table_bytes())
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")
    with pytest.raises(ValueError, match="symlinks"):
        export_asset(root, "Test.atb", link)
    assert not list(actual.iterdir())

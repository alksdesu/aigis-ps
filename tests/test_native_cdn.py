import hashlib

import pytest
from fastapi.testclient import TestClient

from aigis_ps.app import create_app
from aigis_ps.asset_service import ASSET_CHANGED, ASSET_NOT_CACHED
from aigis_ps.assets import asset_key
from aigis_ps.native_cdn import CdnPayload, NativeCdnService, respond
from aigis_ps.resource_fetch import PC_INDEX_NAME, ResourceEntry, parse_index


def native_index(*entries):
    csv = "".join(f"{entry.group},{entry.md5},{entry.flags},{entry.size},{entry.cache_key},{entry.name}\n"
                  for entry in entries).encode()
    return bytes(value ^ 0xDA for value in csv)


@pytest.fixture
def cdn(tmp_path):
    source, overlay = tmp_path / "source", tmp_path / "overlay"
    (source / ".cache").mkdir(parents=True)
    overlay.mkdir()
    content = b"ALAR-test-resource"
    entry = ResourceEntry("Example.aar", "a" * 40, hashlib.md5(content).hexdigest(), 5, len(content), asset_key("Example.aar"))
    (source / ".cache" / entry.cache_key).write_bytes(content)
    return NativeCdnService("b" * 40, native_index(entry), (entry,), overlay, source), entry, content


def test_native_index_and_object_routes_use_original_encodings(tmp_path, cdn):
    service, entry, content = cdn
    with TestClient(create_app(tmp_path / "save", native_cdn=service), base_url="http://127.0.0.1") as client:
        index = client.get(f"/client-assets/{service.version}/{PC_INDEX_NAME}")
        assert index.status_code == 200 and parse_index(index.content) == (entry,)
        url = f"/client-assets/{entry.group}/{entry.md5}"
        resource = client.get(url)
        assert resource.content == content
        assert "content-encoding" not in resource.headers
        assert client.head(url).content == b""
        assert int(client.head(url).headers["content-length"]) == len(content)
        part = client.get(url, headers={"Range": "bytes=2-6"})
        assert part.status_code == 206 and part.content == content[2:7]
        assert client.get(url, headers={"If-None-Match": resource.headers["etag"]}).status_code == 304
        assert client.get("/client-assets/unknown/object").status_code == 404
        assert client.get(f"/client-assets/{'c' * 40}/{PC_INDEX_NAME}").status_code == 404
        assert client.get("/local/requests").json()["requests"][0]["status"] == 404


def test_objects_without_a_cached_candidate_answer_not_cached_unless_a_candidate_changed(tmp_path):
    source, overlay = tmp_path / "source", tmp_path / "overlay"
    (source / ".cache").mkdir(parents=True)
    overlay.mkdir()
    content = b"ALAR-shared-resource"
    entries = tuple(ResourceEntry(name, "a" * 40, hashlib.md5(content).hexdigest(), 5, len(content), asset_key(name))
                    for name in ("First.aar", "Second.aar"))
    service = NativeCdnService("b" * 40, native_index(*entries), entries, overlay, source)
    url = f"/client-assets/{entries[0].group}/{entries[0].md5}"
    cached = source / ".cache" / entries[1].cache_key
    with TestClient(create_app(tmp_path / "save", native_cdn=service), base_url="http://127.0.0.1") as client:
        uncached = client.get(url)
        assert (uncached.status_code, uncached.json()["error"]["code"]) == (404, ASSET_NOT_CACHED)
        cached.write_bytes(content[::-1])
        changed = client.get(url)
        assert (changed.status_code, changed.json()["error"]["code"]) == (409, ASSET_CHANGED)
        cached.write_bytes(content)
        assert client.get(url).content == content
        log = client.get("/local/requests").json()["requests"][:3]
    assert [(row["status"], row["error_code"]) for row in log] == [(200, None), (409, ASSET_CHANGED), (404, ASSET_NOT_CACHED)]


@pytest.mark.parametrize("header,expected", [("bytes=0-2", b"012"), ("bytes=7-", b"789"), ("bytes=-3", b"789"), ("bytes=8-99", b"89")])
def test_valid_single_ranges(header, expected):
    result = respond(CdnPayload(b"0123456789", "etag"), range_header=header)
    assert result.status == 206 and result.content == expected
    assert result.headers["Content-Length"] == str(len(expected))


@pytest.mark.parametrize("header", ["bytes=9-2", "bytes=10-", "bytes=-0", "bytes=-", "bytes=0-1,3-4", "items=1-2"])
def test_unsatisfiable_or_unsupported_ranges(header):
    result = respond(CdnPayload(b"0123456789", "etag"), range_header=header)
    assert result.status == 416 and result.headers["Content-Range"] == "bytes */10"


def test_changed_if_range_returns_full_representation():
    result = respond(CdnPayload(b"0123", "current"), range_header="bytes=0-1", if_range='"old"')
    assert result.status == 200 and result.content == b"0123"

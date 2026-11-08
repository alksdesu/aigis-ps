import csv
import gzip
import hashlib
import http.client
import io
import ssl
import zlib
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from aigis_ps.assets import asset_key
from aigis_ps.resource_fetch import (
    BASE_INDEX_NAME, CDN_ORIGIN, CHUNK_BYTES, FULL_INDEX_NAME, PC_INDEX_NAME,
    PooledCdnOpener, ResourceEntry, ResourceFetchError, ResourceFetcher, _CdnRedirect, fetch_index,
    parse_index,
)


GROUP = "a" * 40
VERSION = "b" * 40


def entry_for(data, name="Map9002.aar"):
    return ResourceEntry(name, GROUP, hashlib.md5(data, usedforsecurity=False).hexdigest(), 8, len(data))


def index_bytes(entries):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for entry in entries:
        suffix = (entry.native_cache_key, entry.name) if entry.native_cache_key is not None else (entry.name,)
        writer.writerow((entry.group, entry.md5, entry.flags, f"{entry.size:012d}", *suffix))
    return output.getvalue().encode().translate(bytes(number ^ 0xDA for number in range(256)))


class Response(io.BytesIO):
    def __init__(self, data, url, *, encoding=None, status=200, length=True, max_chunk=None):
        super().__init__(data)
        self.headers = {}
        if encoding is not None:
            self.headers["Content-Encoding"] = encoding
        if length:
            self.headers["Content-Length"] = str(len(data))
        self.status = status
        self.url = url
        self.read_sizes = []
        self.max_chunk = max_chunk

    def geturl(self):
        return self.url

    def read(self, size=-1):
        assert 0 <= size <= CHUNK_BYTES
        self.read_sizes.append(size)
        if self.max_chunk is not None:
            size = min(size, self.max_chunk)
        return super().read(size)


class Opener:
    def __init__(self, data=b"", *, encoding=None, status=200, length=True, response_url=None, error=None, max_chunk=None):
        self.data = data
        self.encoding = encoding
        self.status = status
        self.length = length
        self.response_url = response_url
        self.error = error
        self.max_chunk = max_chunk
        self.calls = []
        self.responses = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        response = Response(self.data, self.response_url or request.full_url, encoding=self.encoding,
                            status=self.status, length=self.length, max_chunk=self.max_chunk)
        self.responses.append(response)
        return response


def test_parse_index_preserves_names_flags_and_zero_padded_sizes():
    first = entry_for(b"ALTBexample", "folder/台詞,01.txt")
    second = replace(first, name="Another.aar", flags=-1, size=0)
    assert parse_index(index_bytes((first, second, first))) == (first, second)
    assert first.cache_key == asset_key(first.name)
    assert first.url == CDN_ORIGIN + GROUP + "/" + first.md5


def test_conflicting_duplicate_names_fail():
    first = entry_for(b"first")
    second = entry_for(b"other")
    with pytest.raises(ResourceFetchError) as result:
        parse_index(index_bytes((first, second)))
    assert result.value.code == "duplicate_resource_name"


def test_pc_index_preserves_native_cache_key_and_uses_sixth_column_name():
    entry = entry_for(b"ALTBexample", "Map9002.aar")
    entry = replace(entry, native_cache_key=entry.cache_key)
    parsed, = parse_index(index_bytes((entry,)))
    assert parsed.name == "Map9002.aar"
    assert parsed.native_cache_key == asset_key(parsed.name)
    assert parsed.index_format == "pc6"
    assert replace(parsed, native_cache_key=None).index_format == "web5"


def test_mixed_pc_and_web_index_rows_are_rejected():
    first = entry_for(b"x", "First.aar")
    second = entry_for(b"x", "Second.aar")
    second = replace(second, native_cache_key=second.cache_key)
    with pytest.raises(ResourceFetchError) as error:
        parse_index(index_bytes((first, second)))
    assert error.value.code == "mixed_index_formats"


@pytest.mark.parametrize("key", ["../outside", "0" * 64])
def test_native_cache_key_must_match_the_verified_name_hash(key):
    with pytest.raises(ResourceFetchError):
        replace(entry_for(b"x"), native_cache_key=key)


@pytest.mark.parametrize("name", ["", "../outside", "/absolute", "C:/file", "folder\\file", "name?query", "NUL.txt", "file. "])
def test_unsafe_logical_names_fail(name):
    with pytest.raises(ValueError):
        entry_for(b"x", name)


@pytest.mark.parametrize("changes", [{"group": "../host"}, {"md5": "not-a-hash"}, {"flags": True}, {"size": True}, {"size": -1}, {"size": 2 ** 32}])
def test_entry_metadata_is_bounded_and_typed(changes):
    with pytest.raises(ResourceFetchError):
        replace(entry_for(b"x"), **changes)


@pytest.mark.parametrize("raw", [b"", b"<html>error</html>", b"\x25", b"\n".translate(bytes(n ^ 0xDA for n in range(256)))])
def test_wrong_or_empty_index_encoding_is_not_guessed(raw):
    with pytest.raises(ResourceFetchError):
        parse_index(raw)


def test_invalid_index_csv_or_column_count_fails():
    for text in ('"unterminated', "a,b,c,d", "a,b,1,-1,x", "a,b,1,1,x,extra"):
        with pytest.raises(ResourceFetchError):
            parse_index(text.encode().translate(bytes(n ^ 0xDA for n in range(256))))


def test_index_limit_is_checked_before_decoding(monkeypatch):
    monkeypatch.setattr("aigis_ps.resource_fetch.MAX_INDEX_BYTES", 2)
    with pytest.raises(ResourceFetchError) as error:
        parse_index(b"123")
    assert error.value.code == "invalid_index_size"


@pytest.mark.parametrize("list_name", [PC_INDEX_NAME, FULL_INDEX_NAME, BASE_INDEX_NAME])
def test_fetch_index_decodes_http_gzip_then_fixed_xor(list_name):
    entry = entry_for(b"ALTBexample")
    if list_name == PC_INDEX_NAME:
        entry = replace(entry, native_cache_key=entry.cache_key)
    expected = (entry,)
    raw = index_bytes(expected)
    opener = Opener(gzip.compress(raw), encoding="gzip")
    downloaded, parsed = fetch_index(VERSION, opener=opener, list_name=list_name)
    assert downloaded == raw
    assert parsed == expected
    request, timeout = opener.calls[0]
    assert request.full_url == CDN_ORIGIN + VERSION + "/" + list_name
    assert timeout == 30
    assert request.get_header("Authorization") is None
    assert request.get_header("Cookie") is None
    assert opener.responses[0].closed


def test_index_default_is_the_verified_pc_filename():
    entry = entry_for(b"x")
    opener = Opener(index_bytes((replace(entry, native_cache_key=entry.cache_key),)))
    fetch_index(VERSION, opener=opener)
    assert opener.calls[0][0].full_url.endswith("/" + PC_INDEX_NAME)
    for value in ("../secret", "unknown-index"):
        with pytest.raises(ResourceFetchError):
            fetch_index(VERSION, opener=opener, list_name=value)
    assert len(opener.calls) == 1


def test_selected_index_name_requires_matching_pc_or_web_columns():
    with pytest.raises(ResourceFetchError) as error:
        fetch_index(VERSION, opener=Opener(index_bytes((entry_for(b"x"),))))
    assert error.value.code == "index_format_mismatch"


@pytest.mark.parametrize("version", ["", "b" * 39, "B" * 40, "../" + "b" * 40, 42])
def test_bad_index_version_never_requests(version):
    opener = Opener()
    with pytest.raises(ResourceFetchError):
        fetch_index(version, opener=opener)
    assert not opener.calls


def encoded(data, mode):
    if mode == "gzip":
        return gzip.compress(data), "gzip"
    if mode == "gzip-multi":
        point = len(data) // 2
        return gzip.compress(data[:point]) + gzip.compress(data[point:]), "gzip"
    if mode == "deflate":
        return zlib.compress(data), "deflate"
    if mode == "raw-deflate":
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        return compressor.compress(data) + compressor.flush(), "deflate"
    return data, None


@pytest.mark.parametrize("mode", ["identity", "gzip", "gzip-multi", "deflate", "raw-deflate"])
def test_download_validates_decoded_bytes_without_expanding_resource_format(tmp_path, mode):
    payload = b"ALL4" + bytes(range(256)) * 900
    entry = entry_for(payload)
    body, encoding = encoded(payload, mode)
    opener = Opener(body, encoding=encoding)
    fetcher = ResourceFetcher(tmp_path, opener=opener)
    result = fetcher.download(entry)
    target = tmp_path / ".cache" / entry.cache_key
    assert result["status"] == "downloaded"
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["bytes"] == len(payload)
    assert target.read_bytes() == payload
    assert target.with_name(target.name + ".md5").read_text().strip() == entry.md5
    assert not list((tmp_path / ".incoming").iterdir())
    assert opener.responses[0].closed
    assert max(opener.responses[0].read_sizes) <= CHUNK_BYTES
    assert fetcher.download(entry)["status"] == "cached"
    assert len(opener.calls) == 1


@pytest.mark.parametrize("mode", ["identity", "gzip", "deflate", "raw-deflate"])
def test_empty_resources_are_valid_when_indexed(tmp_path, mode):
    body, encoding = encoded(b"", mode)
    fetcher = ResourceFetcher(tmp_path, opener=Opener(body, encoding=encoding))
    assert fetcher.download(entry_for(b""))["bytes"] == 0


def test_raw_deflate_and_header_detection_handle_short_reads(tmp_path):
    body, encoding = encoded(b"a" * 100000, "raw-deflate")
    fetcher = ResourceFetcher(tmp_path, opener=Opener(body, encoding=encoding, max_chunk=1))
    assert fetcher.download(entry_for(b"a" * 100000))["bytes"] == 100000


@pytest.mark.parametrize("mode", ["identity", "gzip", "deflate", "raw-deflate"])
def test_oversized_decoded_body_does_not_replace_previous_file(tmp_path, mode):
    target_entry = entry_for(b"valid")
    target = tmp_path / ".cache" / target_entry.cache_key
    target.parent.mkdir()
    target.write_bytes(b"older")
    body, encoding = encoded(b"too much content" * 100, mode)
    fetcher = ResourceFetcher(tmp_path, opener=Opener(body, encoding=encoding))
    with pytest.raises(ResourceFetchError) as error:
        fetcher.download(target_entry)
    assert error.value.code == "response_too_large"
    assert target.read_bytes() == b"older"
    assert not list((tmp_path / ".incoming").iterdir())


def test_md5_failure_and_short_content_do_not_activate(tmp_path):
    desired = entry_for(b"valid")
    for body in (b"other", b"x"):
        with pytest.raises(ResourceFetchError) as error:
            ResourceFetcher(tmp_path, opener=Opener(body)).download(desired)
        assert error.value.code in ("md5_mismatch", "size_mismatch")
        assert not (tmp_path / ".cache" / desired.cache_key).exists()
        assert not list((tmp_path / ".incoming").iterdir())


@pytest.mark.parametrize("mode", ["gzip", "deflate", "raw-deflate"])
def test_truncated_or_trailing_compressed_bytes_fail(tmp_path, mode):
    payload = b"valid" * 100
    compressed, encoding = encoded(payload, mode)
    for body in (compressed[:-2], compressed + b"junk"):
        with pytest.raises(ResourceFetchError):
            ResourceFetcher(tmp_path, opener=Opener(body, encoding=encoding)).download(entry_for(payload))
        assert not (tmp_path / ".cache" / entry_for(payload).cache_key).exists()


def test_cached_content_repairs_missing_or_wrong_sidecar_without_network(tmp_path):
    payload = b"ALTBexisting"
    entry = entry_for(payload)
    target = tmp_path / ".cache" / entry.cache_key
    target.parent.mkdir()
    target.write_bytes(payload)
    opener = Opener(error=AssertionError("Unexpected network request"))
    fetcher = ResourceFetcher(tmp_path, opener=opener)
    for value in (None, b"wrong", b"\xff"):
        sidecar = target.with_name(target.name + ".md5")
        if value is not None:
            sidecar.write_bytes(value)
        assert fetcher.download(entry)["status"] == "cached"
        assert sidecar.read_text().strip() == entry.md5
    assert not opener.calls


def test_invalid_existing_cache_is_replaced_only_after_valid_download(tmp_path):
    payload = b"ALTBgood"
    entry = entry_for(payload)
    target = tmp_path / ".cache" / entry.cache_key
    target.parent.mkdir()
    target.write_bytes(b"old")
    assert ResourceFetcher(tmp_path, opener=Opener(payload)).download(entry)["status"] == "downloaded"
    assert target.read_bytes() == payload


@pytest.mark.parametrize("url", ["http://drc1bk94f7rq8.cloudfront.net/resource", "https://elsewhere.invalid/resource", "https://user@drc1bk94f7rq8.cloudfront.net/resource", CDN_ORIGIN + "resource?token=x", CDN_ORIGIN + "resource#fragment"])
def test_redirects_cannot_leave_fixed_public_origin(url):
    handler = _CdnRedirect()
    with pytest.raises(ResourceFetchError):
        handler.redirect_request(Request(CDN_ORIGIN + "source"), None, 302, "Found", {}, url)


def test_cross_origin_response_is_rejected_even_with_injected_opener(tmp_path):
    opener = Opener(b"valid", response_url="https://elsewhere.invalid/payload")
    with pytest.raises(ResourceFetchError):
        ResourceFetcher(tmp_path, opener=opener).download(entry_for(b"valid"))
    assert opener.responses[0].closed
    assert not list((tmp_path / ".incoming").iterdir())


def test_http_failure_propagates_and_leaves_no_partial_file(tmp_path):
    for opener in (Opener(status=503), Opener(error=URLError("connection failed"))):
        with pytest.raises((HTTPError, URLError)):
            ResourceFetcher(tmp_path, opener=opener).download(entry_for(b"valid"))
        assert not list((tmp_path / ".incoming").iterdir())


def test_size_limit_and_invalid_timeout_stop_before_network(tmp_path):
    opener = Opener(b"valid")
    with pytest.raises(ResourceFetchError, match="file limit"):
        ResourceFetcher(tmp_path, opener=opener, max_file_bytes=2).download(entry_for(b"valid"))
    for timeout in (0, -1, float("nan"), float("inf"), True, 301):
        with pytest.raises(ResourceFetchError):
            ResourceFetcher(tmp_path, opener=opener, timeout=timeout)
    assert not opener.calls
    assert not (tmp_path / ".cache").exists()


def test_junctions_and_sidecar_links_are_rejected_before_network(tmp_path, monkeypatch):
    entry = entry_for(b"valid")
    blocked = tmp_path / ".cache" / (entry.cache_key + ".md5")
    is_junction = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == blocked or is_junction(path))
    opener = Opener(b"valid")
    with pytest.raises(ValueError, match="junction"):
        ResourceFetcher(tmp_path, opener=opener).download(entry)
    assert not opener.calls


def test_content_length_and_encoding_fail_before_activation(tmp_path):
    for value in ("-1", "invalid", str(2 ** 35), "999"):
        class LengthOpener(Opener):
            def open(self, request, timeout):
                result = super().open(request, timeout)
                result.headers["Content-Length"] = value
                return result

        with pytest.raises(ResourceFetchError):
            ResourceFetcher(tmp_path, opener=LengthOpener(b"valid")).download(entry_for(b"valid"))
    with pytest.raises(ResourceFetchError, match="Content-Encoding"):
        ResourceFetcher(tmp_path, opener=Opener(b"valid", encoding="br")).download(entry_for(b"valid"))


def test_known_public_manifest_parses_offline():
    path = Path(__file__).resolve().parents[1] / "artifacts/cdn-contract-20261101/full-manifest.payload.bin"
    if not path.is_file():
        pytest.skip("The recorded public manifest is not present")
    records = parse_index(path.read_bytes())
    assert len(records) == 24413
    assert next(entry for entry in records if entry.name == "Map9002.aar").size == 627772


def test_known_pc_manifest_parses_offline_with_verified_hashes():
    path = Path(__file__).resolve().parents[1] / "artifacts/cdn-contract-20261101/pc-historical-manifest.payload.bin"
    if not path.is_file():
        pytest.skip("The recorded PC manifest is not present")
    records = parse_index(path.read_bytes())
    assert len(records) == 23634
    assert all(record.index_format == "pc6" and record.native_cache_key == record.cache_key for record in records)


class PoolResponse(io.BytesIO):
    def __init__(self, body=b"valid", *, status=200, headers=None, will_close=False, advertised_length=None, read_error=None):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}
        self.length = len(body) if advertised_length is None else advertised_length
        self.will_close = will_close
        self.read_error = read_error
        self.available = len(body)

    def read(self, size=-1):
        if self.read_error is not None:
            raise self.read_error
        data = super().read(size)
        self.length -= len(data)
        return data

    def isclosed(self):
        return self.closed or self.tell() == self.available


class PoolSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


class PoolConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.sock = None
        self.timeout = None
        self.closed = False

    def request(self, method, path, *, headers):
        assert not self.closed
        self.sock = self.sock or PoolSocket()
        self.requests.append((method, path, headers))

    def getresponse(self):
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self):
        self.closed = True
        self.sock = None


class PoolFactory:
    def __init__(self, *connections):
        self.connections = list(connections)
        self.calls = []

    def __call__(self, host, port, *, timeout, context):
        assert host == "drc1bk94f7rq8.cloudfront.net" and port == 443
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        self.calls.append((host, port, timeout, context))
        return self.connections[len(self.calls) - 1]


def test_pool_reuses_completed_http_connection_and_updates_timeout():
    connection = PoolConnection([PoolResponse(b"first"), PoolResponse(b"second")])
    factory = PoolFactory(connection)
    pool = PooledCdnOpener(connection_factory=factory)
    for payload, timeout in ((b"first", 12), (b"second", 24)):
        with pool.open(Request(CDN_ORIGIN + "resource"), timeout) as response:
            assert response.status == 200
            assert response.geturl() == CDN_ORIGIN + "resource"
            assert response.read() == payload
        assert response.closed
    assert len(factory.calls) == 1
    assert len(connection.requests) == 2
    assert connection.sock.timeouts == [24]
    assert not connection.closed
    pool.close()
    pool.close()
    assert connection.closed


def test_pool_discards_connection_when_body_is_closed_early():
    first = PoolConnection([PoolResponse()])
    second = PoolConnection([PoolResponse()])
    factory = PoolFactory(first, second)
    pool = PooledCdnOpener(connection_factory=factory)
    with pool.open(Request(CDN_ORIGIN + "first")) as response:
        assert response.read(2) == b"va"
    assert first.closed
    with pool.open(Request(CDN_ORIGIN + "second")) as response:
        assert response.read() == b"valid"
    assert len(factory.calls) == 2
    pool.close()


def test_pool_download_pipeline_reuses_gzip_connection_with_content_checks(tmp_path):
    bodies = (b"ALTBfirst", b"ALL4second" * 100)
    connection = PoolConnection([PoolResponse(gzip.compress(body), headers={"Content-Encoding": "gzip"}) for body in bodies])
    factory = PoolFactory(connection)
    with PooledCdnOpener(connection_factory=factory) as pool:
        fetcher = ResourceFetcher(tmp_path, opener=pool)
        for index, body in enumerate(bodies):
            entry = entry_for(body, f"Entry{index}.aar")
            result = fetcher.download(entry)
            assert result["status"] == "downloaded"
            assert Path(result["path"]).read_bytes() == body
        assert fetcher.download(entry)["status"] == "cached"
    assert len(factory.calls) == 1
    assert len(connection.requests) == 2
    assert connection.closed


def test_pool_reconnects_one_time_when_an_idle_connection_is_stale():
    first = PoolConnection([PoolResponse(b"first"), http.client.RemoteDisconnected("idle connection closed")])
    second = PoolConnection([PoolResponse(b"second")])
    factory = PoolFactory(first, second)
    with PooledCdnOpener(connection_factory=factory) as pool:
        for payload in (b"first", b"second"):
            with pool.open(Request(CDN_ORIGIN + "resource")) as response:
                assert response.read() == payload
    assert len(factory.calls) == 2
    assert len(first.requests) == 2
    assert len(second.requests) == 1
    assert first.closed and second.closed


def test_pool_does_not_retry_a_failing_new_connection():
    connection = PoolConnection([http.client.RemoteDisconnected("initial connection failed")])
    factory = PoolFactory(connection)
    with PooledCdnOpener(connection_factory=factory) as pool:
        with pytest.raises(URLError):
            pool.open(Request(CDN_ORIGIN + "resource"))
    assert len(factory.calls) == 1
    assert connection.closed


def test_pool_limits_stale_connection_retry_to_one_attempt():
    first = PoolConnection([PoolResponse(), http.client.BadStatusLine("stale")])
    second = PoolConnection([http.client.RemoteDisconnected("retry failed")])
    factory = PoolFactory(first, second)
    with PooledCdnOpener(connection_factory=factory) as pool:
        with pool.open(Request(CDN_ORIGIN + "first")) as response:
            response.read()
        with pytest.raises(URLError):
            pool.open(Request(CDN_ORIGIN + "second"))
    assert len(factory.calls) == 2
    assert first.closed and second.closed


@pytest.mark.parametrize("response", [PoolResponse(will_close=True), PoolResponse(advertised_length=10)])
def test_pool_drops_server_closed_or_truncated_connections(response):
    first = PoolConnection([response])
    second = PoolConnection([PoolResponse()])
    factory = PoolFactory(first, second)
    with PooledCdnOpener(connection_factory=factory) as pool:
        with pool.open(Request(CDN_ORIGIN + "first")) as result:
            result.read()
        assert first.closed
        with pool.open(Request(CDN_ORIGIN + "second")) as result:
            result.read()
    assert len(factory.calls) == 2


def test_pool_read_interruption_drops_connection_without_replaying_partial_body():
    first = PoolConnection([PoolResponse(read_error=ConnectionResetError("body interrupted"))])
    second = PoolConnection([PoolResponse()])
    factory = PoolFactory(first, second)
    with PooledCdnOpener(connection_factory=factory) as pool:
        with pytest.raises(ConnectionResetError):
            with pool.open(Request(CDN_ORIGIN + "first")) as response:
                response.read()
        assert first.closed
        assert len(factory.calls) == 1
        with pool.open(Request(CDN_ORIGIN + "second")) as response:
            response.read()
    assert len(factory.calls) == 2


def test_pool_follows_only_fixed_origin_redirects():
    connection = PoolConnection([PoolResponse(b"", status=302, headers={"Location": "/redirected"}), PoolResponse()])
    factory = PoolFactory(connection)
    with PooledCdnOpener(connection_factory=factory) as pool:
        with pool.open(Request(CDN_ORIGIN + "first")) as response:
            assert response.geturl() == CDN_ORIGIN + "redirected"
            response.read()
    assert len(factory.calls) == 1
    assert [request[1] for request in connection.requests] == ["/first", "/redirected"]


@pytest.mark.parametrize("location", ["https://elsewhere.invalid/payload", "http://drc1bk94f7rq8.cloudfront.net/payload", "/payload?token=x"])
def test_pool_rejects_unsafe_redirect_before_following(location):
    connection = PoolConnection([PoolResponse(b"", status=302, headers={"Location": location})])
    factory = PoolFactory(connection)
    with PooledCdnOpener(connection_factory=factory) as pool:
        with pytest.raises(ResourceFetchError):
            pool.open(Request(CDN_ORIGIN + "first"))
    assert len(connection.requests) == 1
    assert connection.closed


def test_pool_rejects_redirect_loops():
    connection = PoolConnection([PoolResponse(b"", status=302, headers={"Location": "/first"})])
    with PooledCdnOpener(connection_factory=PoolFactory(connection)) as pool:
        with pytest.raises(ResourceFetchError) as error:
            pool.open(Request(CDN_ORIGIN + "first"))
        assert error.value.code == "redirect_limit"
    assert connection.closed


def test_pool_requires_previous_response_to_close():
    connection = PoolConnection([PoolResponse(), PoolResponse()])
    factory = PoolFactory(connection)
    with PooledCdnOpener(connection_factory=factory) as pool:
        first = pool.open(Request(CDN_ORIGIN + "first"))
        with pytest.raises(ResourceFetchError) as error:
            pool.open(Request(CDN_ORIGIN + "second"))
        assert error.value.code == "response_not_closed"
        assert first.read() == b"valid"
        first.close()
        with pool.open(Request(CDN_ORIGIN + "second")) as second:
            second.read()
    assert len(factory.calls) == 1
    assert len(connection.requests) == 2


@pytest.mark.parametrize("http_request", [Request(CDN_ORIGIN + "resource", data=b"body"), Request(CDN_ORIGIN + "resource", method="HEAD"), Request(CDN_ORIGIN + "resource", headers={"Cookie": "private"}), Request(CDN_ORIGIN + "resource", headers={"Host": "elsewhere.invalid"}), Request("https://elsewhere.invalid/resource")])
def test_pool_rejects_nonpublic_request_before_connecting(http_request):
    factory = PoolFactory()
    with PooledCdnOpener(connection_factory=factory) as pool:
        with pytest.raises(ResourceFetchError):
            pool.open(http_request)
    assert not factory.calls


def test_pool_closed_opener_cannot_reconnect():
    factory = PoolFactory()
    pool = PooledCdnOpener(connection_factory=factory)
    pool.close()
    with pytest.raises(ResourceFetchError) as error:
        pool.open(Request(CDN_ORIGIN + "resource"))
    assert error.value.code == "opener_closed"
    assert not factory.calls

from __future__ import annotations

import csv
import gzip
import hashlib
import http.client
import io
import math
import os
import re
import ssl
import threading
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .assets import _safe_name, asset_key
from .local_io import atomic_replace, checked_path, read_bounded
from .native_numbers import UINT32_MASK, int32


CDN_ORIGIN = "https://drc1bk94f7rq8.cloudfront.net/"
PC_INDEX_NAME = "5nhhyzpc90mlwd9i06s8nfz781im220y"
FULL_INDEX_NAME = "1fp32igvpoxnb521p9dqypak5cal0xv0"
BASE_INDEX_NAME = "0fnnrb7mgd65pwa1uyu2u7jpsf0n0ge4"
INDEX_NAMES = frozenset((PC_INDEX_NAME, FULL_INDEX_NAME, BASE_INDEX_NAME))
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_INDEX_ENTRIES = 200_000
CHUNK_BYTES = 64 * 1024
_HASH40 = re.compile(r"[0-9a-f]{40}\Z")
_HASH32 = re.compile(r"[0-9a-f]{32}\Z")
_HASH64 = re.compile(r"[0-9a-f]{64}\Z")
_INTEGER = re.compile(r"-?[0-9]+\Z")
_SIZE = re.compile(r"[0-9]+\Z")
_XOR_TABLE = bytes(value ^ 0xDA for value in range(256))


class ResourceFetchError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResourceEntry:
    name: str
    group: str
    md5: str
    flags: int
    size: int
    native_cache_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or len(self.name.encode("utf-8")) > 4096:
            raise ResourceFetchError("invalid_resource_name", "Invalid public resource name")
        _safe_name(self.name)
        if not isinstance(self.group, str) or not _HASH40.fullmatch(self.group):
            raise ResourceFetchError("invalid_resource_group", "Resource group must be a lowercase 40-digit hash")
        if not isinstance(self.md5, str) or not _HASH32.fullmatch(self.md5):
            raise ResourceFetchError("invalid_resource_md5", "Resource content MD5 must be a lowercase 32-digit hash")
        if not int32(self.flags):
            raise ResourceFetchError("invalid_resource_flags", "Resource flags must fit a signed 32-bit integer")
        if type(self.size) is not int or not 0 <= self.size <= UINT32_MASK:
            raise ResourceFetchError("invalid_resource_size", "Resource size must fit an unsigned 32-bit integer")
        if self.native_cache_key is not None:
            if not isinstance(self.native_cache_key, str) or not _HASH64.fullmatch(self.native_cache_key):
                raise ResourceFetchError("invalid_native_cache_key", "Native cache key must be a lowercase 64-digit hash")
            if self.native_cache_key != self.cache_key:
                raise ResourceFetchError("native_cache_key_mismatch", "Native cache key does not match the resource-name hash")

    @property
    def cache_key(self) -> str:
        return asset_key(self.name)

    @property
    def index_format(self) -> str:
        return "pc6" if self.native_cache_key is not None else "web5"

    @property
    def url(self) -> str:
        return CDN_ORIGIN + self.group + "/" + self.md5


def parse_index(raw: bytes) -> tuple[ResourceEntry, ...]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_INDEX_BYTES:
        raise ResourceFetchError("invalid_index_size", "Resource index is empty or exceeds its size limit")
    try:
        text = raw.translate(_XOR_TABLE).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ResourceFetchError("invalid_index_encoding", "Resource index is not XOR-encoded UTF-8") from error
    entries = {}
    column_count = None
    try:
        for row_number, row in enumerate(csv.reader(io.StringIO(text, newline=""), strict=True), 1):
            if row_number > MAX_INDEX_ENTRIES:
                raise ResourceFetchError("index_row_limit", "Resource index exceeds its row limit")
            if not row:
                continue
            if len(row) not in (5, 6) or not _INTEGER.fullmatch(row[2]) or not _SIZE.fullmatch(row[3]):
                raise ResourceFetchError("invalid_index_row", f"Resource index row {row_number} has an invalid shape")
            if column_count is not None and len(row) != column_count:
                raise ResourceFetchError("mixed_index_formats", "Resource index mixes PC and web rows")
            column_count = len(row)
            entry = ResourceEntry(name=row[-1], group=row[0], md5=row[1], flags=int(row[2]), size=int(row[3]),
                                  native_cache_key=row[4] if column_count == 6 else None)
            previous = entries.setdefault(entry.name, entry)
            if previous != entry:
                raise ResourceFetchError("duplicate_resource_name", "Resource index assigns conflicting metadata to one name")
    except csv.Error as error:
        raise ResourceFetchError("invalid_index_csv", "Resource index contains invalid CSV") from error
    if not entries:
        raise ResourceFetchError("empty_index", "Resource index contains no resources")
    return tuple(entries.values())


def _timeout(value: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
        raise ResourceFetchError("invalid_timeout", "HTTP timeout must be between 0 and 300 seconds")
    return float(value)


def _cdn_url(value: str) -> str:
    if not isinstance(value, str) or any(ord(character) < 33 or character == "\\" for character in value):
        raise ResourceFetchError("invalid_cdn_url", "Invalid public resource URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "drc1bk94f7rq8.cloudfront.net"
            or parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ResourceFetchError("invalid_cdn_url", "Public resources must remain on the fixed HTTPS CDN origin")
    return value


class _CdnRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return super().redirect_request(request, fp, code, message, headers, _cdn_url(newurl))


class _PooledCdnResponse:
    def __init__(self, owner, response, url: str):
        self._owner = owner
        self._response = response
        self._closed = False
        self._url = url
        self.status = response.status
        self.headers = response.headers

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("Response is closed")
        try:
            return self._response.read(size)
        except BaseException:
            self._finish(False)
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    def _finish(self, reusable: bool) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            self._owner._release(reusable)

    def close(self) -> None:
        complete = self._response.length == 0 or (self._response.length is None and self._response.isclosed())
        self._finish(complete and not self._response.will_close)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class PooledCdnOpener:
    def __init__(self, *, connection_factory=None, max_redirects: int = 5):
        if type(max_redirects) is not int or not 0 <= max_redirects <= 10:
            raise ValueError("Redirect limit must be between 0 and 10")
        self._factory = connection_factory or http.client.HTTPSConnection
        self._tls = ssl.create_default_context()
        self._connection = None
        self._busy = False
        self._closed = False
        self._lock = threading.RLock()
        self.max_redirects = max_redirects

    def _discard(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _release(self, reusable: bool) -> None:
        if not reusable:
            self._discard()
        with self._lock:
            self._busy = False

    def _request(self, url: str, headers: dict, timeout: float):
        with self._lock:
            cached_connection = self._connection is not None
        for attempt in range(2 if cached_connection else 1):
            try:
                with self._lock:
                    if self._closed:
                        raise ResourceFetchError("opener_closed", "Resource connection pool is closed")
                    if self._connection is None:
                        self._connection = self._factory("drc1bk94f7rq8.cloudfront.net", 443,
                                                         timeout=timeout, context=self._tls)
                    connection = self._connection
                connection.timeout = timeout
                if connection.sock is not None:
                    connection.sock.settimeout(timeout)
                connection.request("GET", urlsplit(url).path or "/", headers=headers)
                return connection.getresponse()
            except (OSError, http.client.HTTPException) as error:
                self._discard()
                if not cached_connection or attempt == 1:
                    raise URLError(error) from error
        raise RuntimeError("Unreachable resource connection state")

    def open(self, request: Request, timeout: float = 30):
        url = _cdn_url(request.full_url)
        timeout = _timeout(timeout)
        if request.get_method() != "GET" or request.data is not None:
            raise ResourceFetchError("invalid_resource_method", "Pooled public resource requests must be bodyless GETs")
        headers = dict(request.header_items())
        lowered = {name.lower(): value for name, value in headers.items()}
        if {"authorization", "proxy-authorization", "cookie"}.intersection(lowered):
            raise ResourceFetchError("private_resource_header", "Public resource requests cannot carry account headers")
        if lowered.get("host", "drc1bk94f7rq8.cloudfront.net").lower() not in (
                "drc1bk94f7rq8.cloudfront.net", "drc1bk94f7rq8.cloudfront.net:443"):
            raise ResourceFetchError("invalid_cdn_url", "Resource Host header differs from the fixed CDN origin")
        with self._lock:
            if self._closed:
                raise ResourceFetchError("opener_closed", "Resource connection pool is closed")
            if self._busy:
                raise ResourceFetchError("response_not_closed", "Close the previous resource response before opening another")
            self._busy = True
        response = None
        try:
            visited = {url}
            for redirect in range(self.max_redirects + 1):
                response = self._request(url, headers, timeout)
                if response.status not in (301, 302, 303, 307, 308):
                    return _PooledCdnResponse(self, response, url)
                location = response.headers.get("Location")
                if not location:
                    return _PooledCdnResponse(self, response, url)
                target = _cdn_url(urljoin(url, location))
                if redirect == self.max_redirects or target in visited:
                    raise ResourceFetchError("redirect_limit", "Public resource redirect limit exceeded")
                complete = response.length == 0 or (response.length is None and response.isclosed())
                reusable = complete and not response.will_close
                response.close()
                response = None
                if not reusable:
                    self._discard()
                url = target
                visited.add(url)
            raise RuntimeError("Unreachable resource redirect state")
        except BaseException:
            try:
                if response is not None:
                    response.close()
            finally:
                self._release(False)
            raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._discard()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def _opener():
    return build_opener(ProxyHandler({}), _CdnRedirect())


class _BoundedReader:
    def __init__(self, source, limit: int):
        self.source = source
        self.limit = limit
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        requested = CHUNK_BYTES if size < 0 else size
        data = self.source.read(min(requested, CHUNK_BYTES, self.limit - self.count + 1))
        if not isinstance(data, bytes):
            raise ResourceFetchError("invalid_response_body", "HTTP reader returned non-byte content")
        self.count += len(data)
        if self.count > self.limit:
            raise ResourceFetchError("response_too_large", "Encoded HTTP body exceeds its size limit")
        return data


def _deflate_chunks(reader: _BoundedReader):
    first = reader.read(CHUNK_BYTES)
    while len(first) < 2:
        extra = reader.read(2 - len(first))
        if not extra:
            raise ResourceFetchError("invalid_compression", "Truncated deflate HTTP body")
        first += extra
    cmf, flags = first[:2]
    wrapped = cmf & 15 == 8 and cmf >> 4 <= 7 and ((cmf << 8) + flags) % 31 == 0
    decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
    chunk = first
    while chunk:
        if decoder.eof:
            raise ResourceFetchError("invalid_compression", "Trailing bytes after the deflate stream")
        pending = chunk
        drain = True
        while pending or drain:
            previous_size = len(pending)
            output = decoder.decompress(pending, CHUNK_BYTES)
            pending = decoder.unconsumed_tail
            if output:
                yield output
            if decoder.eof:
                if decoder.unused_data:
                    raise ResourceFetchError("invalid_compression", "Trailing bytes after the deflate stream")
                break
            drain = len(output) == CHUNK_BYTES
            if pending and len(pending) == previous_size and not output:
                raise ResourceFetchError("invalid_compression", "Deflate decoder made no progress")
        chunk = reader.read(CHUNK_BYTES)
    if not decoder.eof:
        raise ResourceFetchError("invalid_compression", "Truncated deflate HTTP body")


def _response_chunks(response, decoded_limit: int):
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity", "gzip", "x-gzip", "deflate"):
        raise ResourceFetchError("unsupported_content_encoding", "Unsupported HTTP Content-Encoding")
    encoded_limit = max(1024 * 1024, decoded_limit + decoded_limit // 4 + CHUNK_BYTES)
    length = response.headers.get("Content-Length")
    if length is not None:
        if not isinstance(length, str) or not _SIZE.fullmatch(length):
            raise ResourceFetchError("invalid_content_length", "Invalid HTTP Content-Length")
        length = int(length)
        if length > encoded_limit:
            raise ResourceFetchError("response_too_large", "Advertised HTTP body exceeds its size limit")
    reader = _BoundedReader(response, encoded_limit)
    count = 0
    try:
        if encoding in ("gzip", "x-gzip"):
            compressed = gzip.GzipFile(fileobj=reader, mode="rb")
            chunks = iter(lambda: compressed.read(CHUNK_BYTES), b"")
        elif encoding == "deflate":
            compressed = None
            chunks = _deflate_chunks(reader)
        else:
            compressed = None
            chunks = iter(lambda: reader.read(CHUNK_BYTES), b"")
        try:
            for chunk in chunks:
                count += len(chunk)
                if count > decoded_limit:
                    raise ResourceFetchError("response_too_large", "Decoded HTTP body exceeds its size limit")
                yield chunk
        finally:
            if compressed is not None:
                compressed.close()
    except (gzip.BadGzipFile, EOFError, zlib.error) as error:
        raise ResourceFetchError("invalid_compression", "Invalid or truncated compressed HTTP body") from error
    if length is not None and reader.count != length:
        raise ResourceFetchError("content_length_mismatch", "HTTP body length differs from Content-Length")


def _open(opener, url: str, timeout: float):
    request = Request(_cdn_url(url), headers={"User-Agent": "AigisLocalResources/0.1",
                                             "Accept-Encoding": "gzip, deflate", "Accept": "application/octet-stream"})
    response = opener.open(request, timeout=timeout)
    try:
        _cdn_url(response.geturl())
        if response.status != 200:
            raise HTTPError(url, response.status, "Unexpected resource response status", response.headers, None)
        return response
    except BaseException:
        response.close()
        raise


def fetch_index(version: str, *, timeout: float = 30, opener=None, list_name: str = PC_INDEX_NAME) -> tuple[bytes, tuple[ResourceEntry, ...]]:
    if not isinstance(version, str) or not _HASH40.fullmatch(version):
        raise ResourceFetchError("invalid_index_version", "Index version must be a lowercase 40-digit hash")
    if list_name not in INDEX_NAMES:
        raise ResourceFetchError("invalid_index_name", "Unsupported public resource index name")
    endpoint = CDN_ORIGIN + version + "/" + list_name
    with _open(opener or _opener(), endpoint, _timeout(timeout)) as response:
        raw = b"".join(_response_chunks(response, MAX_INDEX_BYTES))
    entries = parse_index(raw)
    expected_format = "pc6" if list_name == PC_INDEX_NAME else "web5"
    if any(entry.index_format != expected_format for entry in entries):
        raise ResourceFetchError("index_format_mismatch", "Resource index columns do not match the selected client format")
    return raw, entries


class ResourceFetcher:
    def __init__(self, root: Path, *, timeout: float = 30, max_file_bytes: int = 512 * 1024 * 1024, opener=None):
        if type(max_file_bytes) is not int or not 0 < max_file_bytes <= UINT32_MASK:
            raise ResourceFetchError("invalid_file_limit", "Maximum resource size must be a positive 32-bit size")
        self.root = checked_path(root)
        if self.root.exists() and not self.root.is_dir():
            raise ResourceFetchError("invalid_cache_root", "Resource root must be a directory")
        self.cache = checked_path(self.root / ".cache", self.root)
        self.incoming = checked_path(self.root / ".incoming", self.root)
        self.timeout = _timeout(timeout)
        self.max_file_bytes = max_file_bytes
        self.opener = opener or _opener()

    def _cached(self, entry: ResourceEntry, target: Path) -> str | None:
        checked_path(target, self.root)
        if not target.is_file() or target.stat().st_size != entry.size:
            return None
        md5 = hashlib.md5(usedforsecurity=False)
        sha256 = hashlib.sha256()
        count = 0
        with target.open("rb") as stream:
            while chunk := stream.read(CHUNK_BYTES):
                count += len(chunk)
                if count > entry.size:
                    return None
                md5.update(chunk)
                sha256.update(chunk)
        return sha256.hexdigest() if count == entry.size and md5.hexdigest() == entry.md5 else None

    def _sidecar(self, entry: ResourceEntry) -> None:
        sidecar = checked_path(self.cache / (entry.cache_key + ".md5"), self.root)
        if sidecar.is_file():
            try:
                if read_bounded(sidecar, 128, root=self.root).decode("ascii").strip() == entry.md5:
                    return
            except (ValueError, OSError):
                pass
        temporary = checked_path(self.incoming / (entry.cache_key + "-" + uuid.uuid4().hex + ".md5.part"), self.root)
        try:
            with temporary.open("xb") as stream:
                stream.write((entry.md5 + "\n").encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace(temporary, checked_path(sidecar, self.root))
        finally:
            checked_path(temporary, self.root).unlink(missing_ok=True)

    def download(self, entry: ResourceEntry) -> dict:
        if not isinstance(entry, ResourceEntry):
            raise TypeError("A validated ResourceEntry is required")
        if entry.size > self.max_file_bytes:
            raise ResourceFetchError("file_too_large", "Resource exceeds the configured file limit")
        checked_path(self.cache, self.root).mkdir(parents=True, exist_ok=True)
        checked_path(self.incoming, self.root).mkdir(parents=True, exist_ok=True)
        target = checked_path(self.cache / entry.cache_key, self.root)
        sidecar = checked_path(self.cache / (entry.cache_key + ".md5"), self.root)
        if any(path.exists() and not path.is_file() for path in (target, sidecar)):
            raise ResourceFetchError("invalid_cache_file", "Resource cache paths must be regular files")
        sha256 = self._cached(entry, target)
        if sha256 is not None:
            self._sidecar(entry)
            return {"name": entry.name, "status": "cached", "bytes": entry.size,
                    "path": str(target), "sha256": sha256, "md5": entry.md5}
        temporary = checked_path(self.incoming / (entry.cache_key + "-" + uuid.uuid4().hex + ".part"), self.root)
        md5 = hashlib.md5(usedforsecurity=False)
        sha256 = hashlib.sha256()
        count = 0
        try:
            with _open(self.opener, entry.url, self.timeout) as response, temporary.open("xb") as stream:
                for chunk in _response_chunks(response, entry.size):
                    count += len(chunk)
                    md5.update(chunk)
                    sha256.update(chunk)
                    stream.write(chunk)
                if count != entry.size:
                    raise ResourceFetchError("size_mismatch", "Resource content does not match its indexed size")
                if md5.hexdigest() != entry.md5:
                    raise ResourceFetchError("md5_mismatch", "Resource content does not match its indexed MD5")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace(temporary, checked_path(target, self.root))
            self._sidecar(entry)
            return {"name": entry.name, "status": "downloaded", "bytes": count,
                    "path": str(target), "sha256": sha256.hexdigest(), "md5": entry.md5}
        finally:
            checked_path(temporary, self.root).unlink(missing_ok=True)

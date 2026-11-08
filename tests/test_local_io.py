import json

import pytest

from aigis_ps import local_io, storage
from aigis_ps.local_io import MAX_JSON_DEPTH, JsonNestingTooDeep, read_json_object
from test_storage import NONFINITE, SCANNER_DEPTH, nested


DOCUMENT = '{"text": "ローカル", "ratio": 1.5, "tiny": 1e-999, "count": 9223372036854775808, "flags": [true, null]}'
VALUE = {"text": "ローカル", "ratio": 1.5, "tiny": 0.0, "count": 2 ** 63, "flags": [True, None]}


def write(path, raw):
    path.write_bytes(raw)
    return path


def test_storage_keeps_exporting_the_strict_reader():
    assert storage.deserialize is local_io.deserialize


@pytest.mark.parametrize("number", NONFINITE)
def test_local_objects_refuse_nonfinite_numbers(tmp_path, number):
    path = write(tmp_path / "local.json", b'{"rows": [' + number.encode("ascii") + b"]}")
    with pytest.raises(ValueError, match="finite"):
        read_json_object(path)


@pytest.mark.parametrize("raw", [b'{"value": 1, "value": 1}', b'{"rows": [{"value": 1, "value": 2}]}'])
def test_local_objects_refuse_duplicate_keys_at_any_depth(tmp_path, raw):
    with pytest.raises(ValueError, match="Duplicate"):
        read_json_object(write(tmp_path / "local.json", raw))


def test_local_objects_share_the_nesting_limit(tmp_path):
    deepest = {"rows": nested(MAX_JSON_DEPTH - 1)}
    assert read_json_object(write(tmp_path / "deepest.json", json.dumps(deepest).encode())) == deepest
    deeper = write(tmp_path / "deeper.json", json.dumps({"rows": nested(MAX_JSON_DEPTH)}).encode())
    with pytest.raises(JsonNestingTooDeep, match="nesting") as error:
        read_json_object(deeper)
    assert isinstance(error.value, ValueError)


def test_scanner_recursion_surfaces_unchanged(tmp_path):
    path = write(tmp_path / "scanner.json", b"[" * SCANNER_DEPTH + b"]" * SCANNER_DEPTH)
    with pytest.raises(RecursionError) as error:
        read_json_object(path, limit=path.stat().st_size)
    assert not isinstance(error.value, ValueError)


@pytest.mark.parametrize("raw", [DOCUMENT.encode(), b"\xef\xbb\xbf" + DOCUMENT.encode(), DOCUMENT.encode("utf-16")],
                         ids=("utf8", "byte-order-mark", "utf16"))
def test_previously_accepted_documents_still_read(tmp_path, raw):
    assert read_json_object(write(tmp_path / "local.json", raw)) == VALUE


@pytest.mark.parametrize("raw", [b"[]", b"null", b'"rows"', b"1"])
def test_local_data_must_still_be_an_object(tmp_path, raw):
    with pytest.raises(ValueError, match="object"):
        read_json_object(write(tmp_path / "local.json", raw))

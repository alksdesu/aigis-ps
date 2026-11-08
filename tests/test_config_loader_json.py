import pytest

from aigis_ps.config_registry import REGISTRY, ConfigError, resolve_reference
from aigis_ps.local_io import MAX_JSON_DEPTH
from aigis_ps.storage import serialize
from test_storage import NONFINITE, nested


DUPLICATES = (b'{"enabled": true, "enabled": true}', b'{"enabled": {"rows": 1, "rows": 1}}')
# No registered rule class has this field, so the deepest permitted document still fails the loader's own checks.
UNKNOWN_FIELD = "unregistered"


def load_error(key, path, raw):
    path.write_bytes(raw)
    with pytest.raises(ConfigError) as caught:
        REGISTRY[key].load(path)
    assert caught.value.key == key
    return caught.value


@pytest.mark.parametrize("key", list(REGISTRY))
@pytest.mark.parametrize("number", NONFINITE)
def test_rule_files_refuse_nonfinite_numbers_while_parsing(key, number, tmp_path):
    error = load_error(key, tmp_path / "rules.json", b'{"enabled": ' + number.encode("ascii") + b"}")
    assert error.code == "config_rules_invalid" and "finite" in str(error)


@pytest.mark.parametrize("key", list(REGISTRY))
@pytest.mark.parametrize("raw", DUPLICATES, ids=("top", "nested"))
def test_rule_files_refuse_duplicate_keys_while_parsing(key, raw, tmp_path):
    error = load_error(key, tmp_path / "rules.json", raw)
    assert error.code == "config_rules_invalid" and "Duplicate JSON object key" in str(error)


@pytest.mark.parametrize("key", list(REGISTRY))
def test_rule_files_share_the_strict_nesting_limit(key, tmp_path):
    path = tmp_path / "rules.json"
    deepest = load_error(key, path, serialize({UNKNOWN_FIELD: nested(MAX_JSON_DEPTH - 1)}).encode())
    assert deepest.code == "config_rules_invalid" and "nesting" not in str(deepest)
    deeper = load_error(key, path, serialize({UNKNOWN_FIELD: nested(MAX_JSON_DEPTH)}).encode())
    assert deeper.code == "config_nesting_too_deep"
    with pytest.raises(ValueError, match="nesting"):
        resolve_reference(REGISTRY[key].loader)(path)

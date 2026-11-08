import json

import pytest

from aigis_ps.cli import load_silver_house_rules


def test_default_and_explicit_silver_compensation_count(tmp_path):
    assert load_silver_house_rules(None).reward_count == 1
    path = tmp_path / "silver-rules.json"
    for count in (0, 2):
        path.write_text(json.dumps({"reward_count": count}), encoding="utf-8")
        assert load_silver_house_rules(path).reward_count == count


@pytest.mark.parametrize("value", [[], {"reward_count": True}, {"reward_count": -1}, {"reward_count": 4097}, {"unknown": 1}])
def test_invalid_silver_compensation_policy_is_rejected(tmp_path, value):
    path = tmp_path / "silver-rules.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_silver_house_rules(path)

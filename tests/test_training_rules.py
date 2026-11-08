import json

import pytest

from aigis_ps.cli import load_training_rules


def test_training_overflow_default_and_explicit_policy(tmp_path):
    assert load_training_rules(None).retain_overflow_experience is True
    path = tmp_path / "training-rules.json"
    path.write_text('{"retain_overflow_experience":false}', encoding="utf-8")
    assert load_training_rules(path).retain_overflow_experience is False


@pytest.mark.parametrize("settings", [[], {"retain_overflow_experience": 1},
    {"retain_overflow_experience": "true"}, {"bonus_percent": 100}])
def test_invalid_training_policy_is_rejected(tmp_path, settings):
    path = tmp_path / "training-rules.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(ValueError):
        load_training_rules(path)

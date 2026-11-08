from argparse import Namespace
import json

import pytest

from aigis_ps import cli
from test_quest_service import bundle


def test_recovery_rules_load_default_and_explicit_limit(tmp_path):
    assert cli.load_recovery_rules(None).charisma_limit == 2000
    path = tmp_path / "recovery.json"
    path.write_text('{"charisma_limit":3000}', encoding="utf-8")
    assert cli.load_recovery_rules(path).charisma_limit == 3000


@pytest.mark.parametrize("value", [[], {"unknown": 1}, {"charisma_limit": True},
    {"charisma_limit": 0}, {"charisma_limit": -1}, {"charisma_limit": 2147483648}])
def test_invalid_recovery_rules_are_rejected(tmp_path, value):
    path = tmp_path / "recovery.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        cli.load_recovery_rules(path)


def test_service_builder_applies_limit_and_existing_recovery_interval(bundle, tmp_path):
    policy = tmp_path / "recovery.json"
    policy.write_text('{"charisma_limit":3000}', encoding="utf-8")
    quest_policy = tmp_path / "quest.json"
    quest_policy.write_text('{"charisma_recovery_seconds":600}', encoding="utf-8")
    updated = cli.services(Namespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA,
        resource_dir=cli.DEFAULT_RESOURCES, rules=None, recovery_rules=policy, quest_rules=quest_policy))
    assert updated.recovery is not None, updated.recovery_error
    assert updated.recovery.rules.charisma_limit == 3000
    assert updated.recovery.recovery_seconds == updated.quests.rules.charisma_recovery_seconds == 600

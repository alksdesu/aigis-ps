from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from aigis_ps import quest_record_compat
from aigis_ps.admin_preflight import PreflightCandidate, QUEST_RECORDS_READ
from aigis_ps.alxml import loads
from aigis_ps.bootstrap import BootstrapService, QUEST_LIMITS
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import Store
from test_bootstrap import asset_root, columns, invoke, quest_master


OLD_MASTER = quest_master((1001, 7317))
NEW_MASTER = quest_master((1001, 40004))


@pytest.fixture
def versions(monkeypatch):
    registered = {hashlib.sha256(OLD_MASTER).hexdigest(): frozenset((40004,)),
                  hashlib.sha256(NEW_MASTER).hexdigest(): frozenset((7317,))}
    monkeypatch.setattr(quest_record_compat, "_versions", lambda: registered)


def test_packaged_versions_only_allow_reviewed_quest_differences():
    versions = quest_record_compat._versions()
    old = versions["ffe2ab63b79eb1801f40696acf10196d985e50b25a82d7996b7277b9caf90819"]
    new = versions["f58af9cb5a21552702f3b754e4649ba8fa3aa7e093cdbf1a2b2bb9fefce2b18a"]
    assert len(old) == 26 and len(new) == 84 and not old & new
    assert 7317 in new and 40004 in old
    assert quest_record_compat.retained_quest_ids(quest_master()) == frozenset()


def test_upgrade_and_rollback_keep_progress_and_only_answer_current_quests(asset_root, tmp_path, versions):
    current = OLD_MASTER
    service = BootstrapService(asset_root, master_provider=lambda _: current)
    store = Store(tmp_path / "save.sqlite3", service.initial_state())
    invoke(service, store, "quest_record")
    profile = store.profile()
    profile.state["tables"]["quest_records"][1].update(MaxRank=3, AchieveFlag=1, Shown=True)
    store.update_state(profile.state, profile.revision)
    history = deepcopy(store.profile().state["tables"]["quest_records"][1])

    current = NEW_MASTER
    reply = columns(invoke(service, store, "quest_record"))
    assert reply["QuestID"].values == (1001, 40004) and reply["A1"].values == (0, 0)
    upgraded = store.profile()
    assert upgraded.state["tables"]["quest_records"][1] == history
    assert len(upgraded.state["tables"]["quest_records"]) == 3
    invoke(service, store, "quest_record")
    assert store.profile() == upgraded

    current = OLD_MASTER
    reply = columns(invoke(service, store, "quest_record"))
    assert reply["QuestID"].values == (1001, 7317) and reply["A1"].values == (0, 3)
    assert reply["Shown"].values == (False, True) and store.profile() == upgraded


@pytest.mark.parametrize("case,code", [
    ("unknown", "quest_record_master_mismatch"), ("other_master", "quest_record_master_mismatch"),
    ("duplicate", "quest_record_master_mismatch"), ("negative", "invalid_quest_record"),
])
def test_retirement_does_not_bypass_identity_or_value_checks(asset_root, tmp_path, versions, case, code):
    master = quest_master((1001, 40004, 50000)) if case == "other_master" else NEW_MASTER
    service = BootstrapService(asset_root, master_provider=lambda _: master)
    state = service.initial_state()
    row = {"QuestID": 9999 if case == "unknown" else 7317, "MaxRank": 0, "AchieveFlag": 0,
           "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False}
    if case == "negative":
        row["MaxRank"] = -1
    state["tables"]["quest_records"] = [row, deepcopy(row)] if case == "duplicate" else [row]
    store = Store(tmp_path / "save.sqlite3", state)
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        invoke(service, store, "quest_record")
    assert caught.value.code == code and store.profile() == before


def test_admin_preflight_uses_the_same_projection_without_changing_history(asset_root, tmp_path, versions):
    service = BootstrapService(asset_root, master_provider=lambda _: NEW_MASTER)
    state = service.initial_state()
    retired = {"QuestID": 7317, "MaxRank": 3, "AchieveFlag": 1, "RemainAssault": 0,
               "FirstDefeatDate": 50, "Shown": True}
    state["tables"]["quest_records"] = [retired]
    before = deepcopy(state)
    generation = SimpleNamespace(bundle=SimpleNamespace(bootstrap=service))
    expected = loads(QUEST_RECORDS_READ.encode(PreflightCandidate(state, generation)), limits=QUEST_LIMITS)
    assert columns(expected)["QuestID"].values == (1001, 40004) and state == before
    store = Store(tmp_path / "save.sqlite3", state)
    assert invoke(service, store, "quest_record") == expected
    assert store.profile().state["tables"]["quest_records"][0] == retired

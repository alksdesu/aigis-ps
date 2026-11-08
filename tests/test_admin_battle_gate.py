from types import SimpleNamespace

import pytest

from aigis_ps.admin_command import AdminError
from aigis_ps.admin_gates import CLIENT_QUIET_SECONDS, QUEST_JOURNAL, ClientGate, require_no_active_battle
from aigis_ps.errors import ProtocolError
from aigis_ps.quest_service import JOURNAL_KEY


SNAPSHOT, RESOURCES = "snapshot-1", "resource-1"
MENU_HEARTBEAT_SECONDS = 180.0


def battle_state(snapshot=SNAPSHOT, resources=RESOURCES):
    return {JOURNAL_KEY: {"schema_version": 2, "next_sequence": 2, "processed_keys": [], "history": [],
                          "active": {"sequence": 1, "quest_id": 100, "start_key": "a" * 64,
                                     "master_snapshot": snapshot, "resource_version": resources}}}


def idle_state():
    return {JOURNAL_KEY: {"schema_version": 2, "next_sequence": 1, "processed_keys": [], "history": [],
                          "active": None}}


def quests_for(snapshot=SNAPSHOT, resources=RESOURCES):
    return SimpleNamespace(catalog=SimpleNamespace(snapshot=SimpleNamespace(identifier=snapshot),
                                                   resource_version=resources))


class Activity:
    def __init__(self):
        self.idle = None

    def idle_seconds(self):
        return self.idle

    def session_observed_since_switch(self):
        return None


def test_the_gate_reads_the_journal_under_the_quest_service_key():
    assert QUEST_JOURNAL == JOURNAL_KEY


def test_a_battle_frozen_on_the_running_snapshots_never_blocks():
    require_no_active_battle(battle_state(), source="backup", quests=quests_for())


def test_without_an_active_battle_nothing_blocks():
    for state in ({}, idle_state()):
        require_no_active_battle(state, source="backup", quests=quests_for())
        require_no_active_battle(state, source="backup")


@pytest.mark.parametrize("quests", [quests_for(snapshot="snapshot-2"), quests_for(resources="resource-2"), None],
                         ids=["other_master", "other_resources", "strict_without_quests"])
def test_battles_outside_the_running_generation_require_an_abort(quests):
    with pytest.raises(AdminError) as caught:
        require_no_active_battle(battle_state(), source="backup", quests=quests)
    assert (caught.value.code, caught.value.status) == ("admin_battle_abort_required", 409)
    assert caught.value.details == {"source": "backup", "battle": {
        "quest_id": 100, "sequence": 1, "start_key": "a" * 64,
        "master_snapshot": SNAPSHOT, "resource_version": RESOURCES}}


def test_identity_checks_refuse_a_corrupt_journal_as_invalid_state():
    with pytest.raises(ProtocolError) as caught:
        require_no_active_battle({JOURNAL_KEY: {"active": 1}}, source="backup", quests=quests_for())
    assert (caught.value.code, caught.value.status) == ("quest_state_invalid", 503)


def test_the_quiet_window_outlasts_one_menu_heartbeat_with_a_margin():
    assert CLIENT_QUIET_SECONDS == 240.0
    assert CLIENT_QUIET_SECONDS >= MENU_HEARTBEAT_SECONDS + 60.0


def test_the_gate_stays_busy_through_a_heartbeat_period_and_opens_at_the_threshold():
    activity = Activity()
    gate = ClientGate(activity, clock=lambda: 0.0)
    for idle in (MENU_HEARTBEAT_SECONDS, CLIENT_QUIET_SECONDS - 1):
        activity.idle = idle
        assert gate.quiet() is False
        with pytest.raises(AdminError) as caught:
            gate.require_closed(True)
        assert caught.value.code == "admin_client_active"
        assert caught.value.details == {"idle_seconds": idle, "quiet_seconds": CLIENT_QUIET_SECONDS}
    activity.idle = CLIENT_QUIET_SECONDS
    assert gate.quiet() is True
    gate.require_closed(True)

from copy import deepcopy

import pytest

from aigis_ps.alxml import loads
from aigis_ps.errors import ProtocolError
from test_player_state import assets, master, service, store, request, row


def add_unit(store, flags=1):
    profile = store.profile()
    unit = deepcopy(profile.state["player_state"]["units"][0])
    unit.update(CardID=2, ClassID=20, SettingSkillID=3, GetIndex=9, UniqueID=2, GeneralPurposeFlags=flags)
    profile.state["player_state"]["units"].append(unit)
    profile.state["player_state"]["battle"]["UX"] = 9
    store.update_state(profile.state, profile.revision)


@pytest.mark.parametrize("flags", [0x11, 0x23, -0x80000000 + 0x17])
@pytest.mark.parametrize("group", [0, 1, 254])
def test_new_squad_assignment_rejects_stored_units_atomically(service, store, flags, group):
    add_unit(store, flags)
    before = store.profile()
    address = ((group << 24) ^ 0x80000000) - 0x80000000
    with pytest.raises(ProtocolError) as caught:
        service.save_squads(request(73, store, [row(address, 9)]), store)
    assert caught.value.code == "squad_unit_in_barracks"
    assert store.profile() == before


@pytest.mark.parametrize("flags", [0, 1, 3, 7, -0x80000000 + 7])
def test_first_barracks_assignment_preserves_other_unit_flags(service, store, flags):
    add_unit(store, flags)
    before = deepcopy(store.profile().state["player_state"]["units"])
    service.save_squads(request(73, store, [row(0, 9)]), store)
    assert store.profile().state["player_state"]["units"] == before


def test_legacy_stored_members_remain_readable_and_can_be_removed(service, store):
    add_unit(store, 0x11)
    service.import_squads(request(74, store, [row(0, 9)]), store)
    before = store.profile()
    loaded = loads(service.load_squads(request(72, store), store))
    assert loaded.children[2].children[1].values == (9,)
    assert store.profile() == before
    service.save_squads(request(73, store, [row(1 << 24, 0)]), store)
    assert store.profile().state["player_state"]["squads"][0]["UnitIndex"] == 9
    service.save_squads(request(73, store, [row(0, -1)]), store)
    assert all(item["UnitIndex"] != 9 for item in store.profile().state["player_state"]["squads"])


def test_cas_retry_checks_barracks_again_after_concurrent_transfer(service, store, monkeypatch):
    add_unit(store)
    original = store.update_state
    calls = []
    def update(state, expected_revision):
        if not calls:
            calls.append(True)
            current = store.profile()
            current.state["player_state"]["units"][1]["GeneralPurposeFlags"] = 0x11
            original(current.state, current.revision)
            raise ValueError("Concurrent transfer")
        return original(state, expected_revision)
    monkeypatch.setattr(store, "update_state", update)
    with pytest.raises(ProtocolError) as caught:
        service.save_squads(request(73, store, [row(0, 9)]), store)
    assert caught.value.code == "squad_unit_in_barracks"
    assert store.profile().state["player_state"]["squads"] == []
    assert store.profile().state["player_state"]["units"][1]["GeneralPurposeFlags"] == 0x11

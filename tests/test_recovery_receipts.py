from copy import deepcopy
from dataclasses import replace

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.storage import serialize
from test_recovery_service import asset_root, case, fail, message


@pytest.mark.parametrize("mutation", ["energy", "free_balance", "paid_balance", "duplicate", "missing", "system"])
def test_cached_recovery_must_match_current_authoritative_response(case, mutation):
    envelope = message(case)
    response = case.service.recover(envelope, case.store)
    document = loads(response)
    groups = list(document.children)
    if mutation == "energy":
        index = next(index for index, node in enumerate(groups) if node.name == "PGP")
        group = groups[index]
        values = list(group.children[1].values)
        values[3] = "999999"
        groups[index] = replace(group, children=(group.children[0], replace(group.children[1], values=tuple(values))))
    elif mutation in ("free_balance", "paid_balance"):
        name = "PFC" if mutation == "free_balance" else "PPC"
        index = next(index for index, node in enumerate(groups) if node.name == name)
        groups[index] = Container(name, (Column("UserID", "I", (1,)), Column("Date", "I", (739000,)), Column("Num", "I", (777,))))
    elif mutation == "duplicate":
        groups.append(next(node for node in groups if node.name == "PGP"))
    elif mutation == "missing":
        groups = [node for node in groups if node.name != "PGP"]
    else:
        groups[0] = Container("SYSTEM", (Column("A1", "S", ("ExchangeKey",)), Column("A2", "S", ("wrong",))))
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state["recovery_history"]["receipts"][-1]["response"] = dumps(replace(document, children=tuple(groups))).decode()
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (serialize(state),))
    fail(case, envelope, "recovery_history_invalid")


def test_unchanged_recovery_receipt_remains_byte_exact(case):
    envelope = message(case)
    response = case.service.recover(envelope, case.store)
    saved = case.store.profile()
    assert case.service.recover(envelope, case.store) == response
    assert case.store.profile() == saved

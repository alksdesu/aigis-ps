import base64
import hashlib
import json

import pytest

from aigis_ps.admin_diff import (ADD, REDACTED, REMOVE, REORDER, REPLACE, DiffLimits, ResponseView, canonical,
                                 diff_digest, diff_states, json_digest, pointer, redact, render_diff, same)
from aigis_ps.alxml import Column, Container, ValueType, dumps, loads
from aigis_ps.storage import serialize


METHOD_KEY = "0123456789abcdef0123456789abcdef"
USER_KEY = "fedcba9876543210fedcba9876543210"


def unit(unique_id, get_index, **changes):
    row = {"CardID": 1, "ClassID": 1, "Exp": 0, "GetIndex": get_index, "UniqueID": unique_id}
    row.update(changes)
    return row


def save(units=(), possessions=(), presents=(), **extra):
    return {"player_state": {"units": list(units)},
            "tables": {"possessions": list(possessions), "presents": list(presents)}, **extra}


def system_xml(*extra, key_type=ValueType.STRING, key_value=METHOD_KEY):
    system = Container("SYSTEM", (Column("A1", ValueType.STRING, ("ExchangeKey", "PaymentTransaction")),
                                  Column("A2", key_type, (key_value, "0" if key_type is ValueType.STRING else 0))))
    return dumps(Container(children=(system, *extra)))


def battle_container():
    return Container("PBP", (Column("A1", ValueType.STRING, ("UX",)), Column("A2", ValueType.INT, (7,))))


def entries_with(entries, **fields):
    return [entry for entry in entries if all(entry.get(name) == value for name, value in fields.items())]


def test_units_align_by_unique_id_when_rows_move():
    before = save([unit(1, 10), unit(2, 11)])
    after = save([unit(2, 11), unit(1, 10, Exp=50)])
    entries = diff_states(before, after)
    assert entries_with(entries, op=REPLACE) == [{
        "op": REPLACE, "path": "/player_state/units", "key": {"UniqueID": 1}, "label": {"GetIndex": 10, "CardID": 1},
        "field": "/Exp", "before": 0, "after": 50}]
    assert entries_with(entries, op=REORDER) == [{
        "op": REORDER, "path": "/player_state/units",
        "before": [{"UniqueID": 1}, {"UniqueID": 2}], "after": [{"UniqueID": 2}, {"UniqueID": 1}]}]


def test_possessions_align_by_item_id():
    before = save(possessions=[{"ItemID": "Alpha", "Count": 1}, {"ItemID": "Beta", "Count": 2}])
    after = save(possessions=[{"ItemID": "Alpha", "Count": 1}, {"ItemID": "Beta", "Count": 3}, {"ItemID": "Gamma", "Count": 1}])
    assert diff_states(before, after) == (
        {"op": REPLACE, "path": "/tables/possessions", "key": {"ItemID": "Beta"}, "field": "/Count", "before": 2, "after": 3},
        {"op": ADD, "path": "/tables/possessions", "key": {"ItemID": "Gamma"}, "index": 2, "after": {"ItemID": "Gamma", "Count": 1}},
    )


def test_presents_align_by_present_id():
    kept, gone, new = ({"PresentID": number, "Type": 1, "Content": number} for number in (3, 4, 9))
    entries = diff_states(save(presents=[gone, kept]), save(presents=[kept, new]))
    assert entries == (
        {"op": REMOVE, "path": "/tables/presents", "key": {"PresentID": 4}, "before": gone},
        {"op": ADD, "path": "/tables/presents", "key": {"PresentID": 9}, "index": 1, "after": new},
    )


def test_table_rows_use_their_primary_keys():
    before = {"tables": {"quest_records": [{"QuestID": 5, "MaxRank": 1}, {"QuestID": 6, "MaxRank": 0}],
                         "crystals": [{"FreeFlag": True, "Num": 3, "Date": 700}, {"FreeFlag": False, "Num": 3, "Date": 700}]}}
    after = {"tables": {"quest_records": [{"QuestID": 6, "MaxRank": 2}, {"QuestID": 5, "MaxRank": 1}],
                        "crystals": [{"FreeFlag": True, "Num": 8, "Date": 700}, {"FreeFlag": False, "Num": 3, "Date": 700}]}}
    entries = diff_states(before, after)
    assert entries_with(entries, op=REPLACE) == [
        {"op": REPLACE, "path": "/tables/crystals", "key": {"FreeFlag": True, "Date": 700}, "field": "/Num", "before": 3, "after": 8},
        {"op": REPLACE, "path": "/tables/quest_records", "key": {"QuestID": 6}, "field": "/MaxRank", "before": 0, "after": 2},
    ]
    assert [entry["path"] for entry in entries_with(entries, op=REORDER)] == ["/tables/quest_records"]


def test_other_values_use_escaped_json_pointers():
    before = {"ledger": {"a/b": 1, "m~n": {"deep": [1, 2]}, "gone": True}}
    after = {"ledger": {"a/b": 2, "m~n": {"deep": [1, 3]}, "new": None}}
    assert diff_states(before, after) == (
        {"op": REPLACE, "path": "/ledger/a~1b", "before": 1, "after": 2},
        {"op": REMOVE, "path": "/ledger/gone", "before": True},
        {"op": REPLACE, "path": "/ledger/m~0n/deep/1", "before": 2, "after": 3},
        {"op": ADD, "path": "/ledger/new", "after": None},
    )
    assert pointer(()) == "" and pointer(("x", 0, "y/z")) == "/x/0/y~1z"


def test_unkeyed_lists_report_shifted_history_compactly():
    receipts = [{"request": f"r{index}"} for index in range(6)]
    entries = diff_states({"history": receipts[:5]}, {"history": receipts[1:]})
    assert entries == ({"op": REMOVE, "path": "/history/0", "before": receipts[0]},
                       {"op": ADD, "path": "/history/4", "after": receipts[5]})


def test_duplicate_heavy_lists_stay_compact():
    zeros = [0] * 20000
    assert diff_states({"bits": zeros}, {"bits": zeros[:7000] + [1] + zeros[7000:]}) == (
        {"op": ADD, "path": "/bits/7000", "after": 1},)
    assert diff_states({"bits": zeros}, {"bits": zeros[:7000] + [1] + zeros[7001:]}) == (
        {"op": REPLACE, "path": "/bits/7000", "before": 0, "after": 1},)
    assert diff_states({"bits": [1, 0, 0, 1]}, {"bits": [1, 0, 1]}) == ({"op": REMOVE, "path": "/bits/2", "before": 0},)


def test_strict_json_types_are_compared():
    assert not same(True, 1) and not same(1, 1.0) and same({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]})
    assert diff_states({"flag": True, "rate": 1}, {"flag": 1, "rate": 1.0}) == (
        {"op": REPLACE, "path": "/flag", "before": True, "after": 1},
        {"op": REPLACE, "path": "/rate", "before": 1, "after": 1.0},
    )
    assert diff_states({"a": 1}, [1]) == ({"op": REPLACE, "path": "", "before": {"a": 1}, "after": [1]},)


def test_duplicate_or_missing_keys_fall_back_to_positions():
    before = save(possessions=[{"ItemID": "Alpha", "Count": 1}, {"ItemID": "Alpha", "Count": 2}])
    after = save(possessions=[{"ItemID": "Alpha", "Count": 1}, {"ItemID": "Alpha", "Count": 5}])
    assert diff_states(before, after) == ({"op": REPLACE, "path": "/tables/possessions/1/Count", "before": 2, "after": 5},)
    loose = diff_states(save(possessions=[{"Count": 1}]), save(possessions=[{"Count": 2}]))
    assert loose == ({"op": REPLACE, "path": "/tables/possessions/0/Count", "before": 1, "after": 2},)


def test_diff_digest_is_deterministic_and_complete():
    before = save([unit(1, 10), unit(2, 11)])
    first = diff_states(before, save([unit(1, 10, Exp=5), unit(2, 11)]))
    again = diff_states(json.loads(json.dumps(before)), save([unit(1, 10, Exp=5), unit(2, 11)]))
    other = diff_states(before, save([unit(1, 10, Exp=6), unit(2, 11)]))
    inserted_first = diff_states(before, save([unit(3, 12), unit(1, 10), unit(2, 11)]))
    inserted_last = diff_states(before, save([unit(1, 10), unit(2, 11), unit(3, 12)]))
    assert diff_digest(first) == diff_digest(again) != diff_digest(other)
    assert diff_digest(inserted_first) != diff_digest(inserted_last)
    assert diff_digest(()) == json_digest([])


def test_render_truncates_entry_lists_with_a_marker():
    entries = diff_states({"values": list(range(30))}, {"values": [value + 100 for value in range(30)]})
    rendered = render_diff(entries, DiffLimits(max_entries=10))
    assert len(rendered["entries"]) == 10
    assert rendered["total"] == 30 and rendered["truncated"] is True and rendered["omitted"] == 20
    small = render_diff(entries, DiffLimits(max_bytes=200))
    assert small["truncated"] is True and len(serialize(small["entries"]).encode()) <= 200
    assert render_diff(entries)["truncated"] is False


def test_render_replaces_oversized_values_with_a_digest():
    large = "x" * 5000
    rendered = render_diff(diff_states({"note": ""}, {"note": large}), DiffLimits(max_value_bytes=1024))
    marker = rendered["entries"][0]["after"]
    assert marker == {"truncated": True, "bytes": len(serialize(large)), "sha256": hashlib.sha256(serialize(large).encode()).hexdigest()}
    assert rendered["entries"][0]["before"] == ""


def test_secret_fields_never_render():
    value = {"user_key": USER_KEY, "method_key": METHOD_KEY, "bootstrap_token": "local-only",
             "nested": [{"method_key": METHOD_KEY, "other": 1}]}
    assert redact(value) == {"user_key": REDACTED, "method_key": REDACTED, "bootstrap_token": REDACTED,
                             "nested": [{"method_key": REDACTED, "other": 1}]}
    rendered = render_diff(diff_states({"profile": {"method_key": "old"}}, {"profile": {"method_key": METHOD_KEY}}))
    assert rendered["entries"] == [{"op": REPLACE, "path": "/profile/method_key", "before": REDACTED, "after": REDACTED}]


def test_cached_responses_render_as_size_and_digest_by_default():
    xml = system_xml(battle_container())
    receipt = {"request": "a" * 64, "revision": 2, "response": xml.decode("utf-8")}
    view = redact({"receipts": [receipt]})
    assert view["receipts"][0]["response"] == {"redacted": "response", "encoding": "text", "bytes": len(xml),
                                               "sha256": hashlib.sha256(xml).hexdigest()}
    assert METHOD_KEY not in serialize(view)


def test_masked_view_hides_the_exchange_key_only():
    xml = system_xml(battle_container())
    masked = redact({"response": xml.decode("utf-8")}, responses=ResponseView.MASKED)["response"]
    document = loads(masked.encode("utf-8"))
    system, battle = document.children
    assert system.children[1].values == (REDACTED, "0") and system.children[0].values == ("ExchangeKey", "PaymentTransaction")
    assert battle == battle_container()
    assert METHOD_KEY not in masked


def test_present_receipts_are_decoded_before_masking():
    xml = system_xml(battle_container())
    history = {"present_claim_history": [{"profile": "b" * 64, "digest": "c" * 64, "revision": 1,
                                          "response": base64.b64encode(xml).decode("ascii")}]}
    digest_view = redact(history)["present_claim_history"][0]["response"]
    assert digest_view == {"redacted": "response", "encoding": "base64", "bytes": len(xml), "sha256": hashlib.sha256(xml).hexdigest()}
    masked = redact(history, responses=ResponseView.MASKED)["present_claim_history"][0]["response"]
    assert loads(masked.encode("utf-8")).children[0].children[1].values == (REDACTED, "0")
    assert METHOD_KEY not in serialize(redact(history, responses=ResponseView.MASKED))


def test_login_identity_columns_are_masked():
    login = Container("LGI", tuple(Column(f"A{index}", ValueType.STRING, (USER_KEY if index == 7 else str(index),))
                                   for index in range(1, 11)))
    platform = Container("LI", (Column("UK", ValueType.STRING, (USER_KEY,)), Column("UI", ValueType.STRING, ("1",))))
    masked = redact(dumps(Container(children=(login, platform))).decode("utf-8"), responses=ResponseView.MASKED)
    assert USER_KEY not in masked
    parsed_login, parsed_platform = loads(masked.encode("utf-8")).children
    assert parsed_login.children[6].values == (REDACTED,) and parsed_login.children[9].values == ("10",)
    assert parsed_platform.children[0].values == (REDACTED,) and parsed_platform.children[1].values == ("1",)


def test_unmaskable_documents_fall_back_to_digests():
    broken = '<?xml version="1.0"?>\n<DA><SYSTEM>'
    assert redact({"response": broken}, responses=ResponseView.MASKED)["response"]["redacted"] == "response"
    numeric = system_xml(key_type=ValueType.INT, key_value=7)
    assert redact({"response": numeric.decode("utf-8")}, responses=ResponseView.MASKED)["response"]["redacted"] == "response"


def test_descriptive_response_values_stay_readable():
    assert redact({"response": "SYSTEM_PBP_PPU", "note": "<b>"}) == {"response": "SYSTEM_PBP_PPU", "note": "<b>"}


def test_rendered_diffs_redact_new_receipts():
    xml = system_xml().decode("utf-8")
    before = {"blessing_history": {"receipts": []}}
    after = {"blessing_history": {"receipts": [{"request": "d" * 64, "response": xml}]}}
    replaced = {"blessing_history": {"receipts": [{"request": "d" * 64, "response": xml.replace("PaymentTransaction", "Other")}]}}
    for rendered in (render_diff(diff_states(before, after)), render_diff(diff_states(after, replaced)),
                     render_diff(diff_states(before, after), responses=ResponseView.MASKED)):
        assert METHOD_KEY not in serialize(rendered)
    field_change = render_diff(diff_states(after, replaced))["entries"][0]
    assert field_change["path"] == "/blessing_history/receipts/0/response" and field_change["after"]["encoding"] == "text"


def test_canonical_values_match_the_stored_json():
    value, digest = canonical({"b": (1, 2), "a": 1.0})
    assert value == {"a": 1.0, "b": [1, 2]} and type(value["b"]) is list
    assert digest == json_digest({"b": [1, 2], "a": 1.0}) == hashlib.sha256(serialize(value).encode()).hexdigest()


@pytest.mark.parametrize("limits", [{"max_entries": 0}, {"max_value_bytes": -1}, {"max_bytes": True}])
def test_diff_limits_must_be_positive_integers(limits):
    with pytest.raises(ValueError):
        DiffLimits(**limits)

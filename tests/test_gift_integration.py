from datetime import datetime
from copy import deepcopy

from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.gift_service import GiftService
from aigis_ps.presents import PresentService
from aigis_ps.protocol import decode_wire, encode_wire
from test_quest_service import bundle
from test_unit_actions import request


def test_native_gift_thresholds_mail_claim_and_restart_form_one_flow(bundle, tmp_path, monkeypatch):
    assert bundle.gifts is not None and bundle.card_status is not None
    catalog, aliases = bundle.gifts.catalog, bundle.gifts.aliases
    card_id = next(identifier for identifier in catalog.card_ids
                   if catalog.card(identifier)["Kind"] == 1 and catalog.card(identifier)["Rare"] == 3
                   and catalog.card(identifier)["BonusType"] and catalog.card(identifier)["BonusType3"]
                   and aliases.resolve(identifier).image_card_id == identifier)
    now = datetime(2026, 11, 1, 12)
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: now)
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    initial, units = bundle.player_state.credit_units(initial, [{"card_id": card_id, "level": 1, "count": 1}])
    target = units[0]
    initial = bundle.possessions.credit(initial, {"gift_0003_diamond": 20, "gift_0103_ring": 5})

    def application():
        presents = PresentService(bundle.bootstrap, bundle.player_state,
                                  asset_reader=lambda name: bundle.native_cdn.assets.read(name)[0])
        monkeypatch.setattr(bundle.bootstrap, "present_provider", presents.response)
        gifts = GiftService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.card_status,
                            presents, catalog, aliases, clock=lambda: now)
        handlers = (bundle.bootstrap.handlers() | bundle.player_state.handlers() | bundle.unit_actions.handlers()
                    | bundle.filters.handlers() | presents.handlers() | gifts.handlers() | bundle.peruse.handlers())
        return create_app(tmp_path, initial_state=initial, handlers=handlers)

    def send(client, envelope):
        result = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert result.status_code == 200, result.text
        return result.content, {child.name: child for child in loads(decode_wire(result.content, envelope.profile.response_key)).children}

    def gift_request(store, item, count):
        fields = [Container("BASE", (Column("A7", "I", (target["GetIndex"],)),
                                     Column("AB", "I", (target["UniqueID"],)), Column("ALTERNATIVE", "I", (0,)))),
                  Column("ITEM", "I", (0,))]
        for index in range(4):
            fields.extend((Column(f"ITEM{index}", "S" if index == 0 else "I", (item if index == 0 else 0,)),
                           Column(f"NUM{index}", "I", (count if index == 0 else 0,))))
        return request(store, 100, fields)

    def claim_request(store, number, ids):
        return request(store, number, (Container("RT", (Container("PT2", (Column("PresentID", "I", tuple(ids)),)),)),))

    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = gift_request(app.state.store, "gift_0003_diamond", 20)
        wire, response = send(client, first)
        first_save = app.state.store.profile()
        assert set(response["TrustReward"].children[0].values) == {8, 15}
        assert wallet_totals(first_save.state)["free"] == 0
        assert response["POSSESSION"].children[1].values == (0,)
        mail = first_save.state["tables"]["presents"]
        assert len(mail) == 2 and all(row["Type"] == 1 and row["Content"] == 3 for row in mail)
        assert send(client, first)[0] == wire
        assert app.state.store.profile() == first_save
        _, received = send(client, claim_request(app.state.store, 1, [mail[0]["PresentID"]]))
        assert received["PPU"].children[0].values == (57,)
        assert received["PBP"].children[1].values == (2,)
        _, advanced = send(client, gift_request(app.state.store, "gift_0103_ring", 5))
        assert advanced["TrustReward"].children[0].values == (1,)
        before_peruse = app.state.store.profile()
        completion = request(app.state.store, 75, (Column("ID", "S", ("evTrustFemale150",)),))
        _, acknowledgement = send(client, completion)
        assert set(acknowledgement) == {"SYSTEM", "PEV"}
        assert acknowledgement["PEV"].children[1].values == (1,)
        expected = deepcopy(before_peruse.state)
        expected["perused_events_schema"] = 1
        expected["tables"]["perused_events"] = [{"ID": "evTrustFemale150", "FLG": 1}]
        after_peruse = app.state.store.profile()
        assert after_peruse.state == expected
        send(client, completion)
        assert app.state.store.profile() == after_peruse
        before_claim = app.state.store.profile()
        assert wallet_totals(before_claim.state)["free"] == 0
        status = bundle.card_status.read(before_claim.state)[0]
        assert status["CardID"] == card_id and status["A7"] == 3
        assert before_claim.state["gift_state"]["claims"][str(card_id)] == [1, 100, 150]
        rows = before_claim.state["tables"]["presents"]
        assert sorted(row["Type"] for row in rows) == [0, 1]
        batch = claim_request(app.state.store, 117, [row["PresentID"] for row in rows])
        batch_wire, received = send(client, batch)
        assert received["PPU"].children[0].values == (57,)
        assert received["PFC"].children[2].values == (1,)
        final = app.state.store.profile()
        assert final.state["tables"]["presents"] == []
        assert wallet_totals(final.state)["free"] == 1
        assert [unit["CardID"] for unit in final.state["player_state"]["units"]] == [1, card_id, 57, 57]
        assert final.state["tables"]["config"][0]["MenuTutorialFlags"] == 1
    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert send(client, batch)[0] == batch_wire
        assert app.state.store.profile() == final
        assert all(not column.values for column in send(client, request(app.state.store, 42, ()))[1]["PPT"].children)
        status = send(client, request(app.state.store, 51, ()))[1]
        assert status["CardID"].values == (card_id,) and status["A7"].values == (3,)
        history = send(client, request(app.state.store, 137, ()))[1]["PEV"]
        assert history.children[0].values == ("evTrustFemale150",) and history.children[1].values == (1,)
        units = send(client, request(app.state.store, 41, ()))[1]
        assert units["A5"].values == (0, 150, 0, 0)
        assert app.state.store.profile() == final

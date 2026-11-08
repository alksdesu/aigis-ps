from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Container, dumps
from .config_state import configuration_delta, configuration_row, patch_configuration
from .errors import ProtocolError
from .native_numbers import int32
from .protocol import ProtocolRegistry, encode_response
from .request_fields import invalid, registered, scalar
from .system_literals import command_id, decode_command, rating_command_id


RESOURCE_LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024)
GUIDE_FIELD = "_GuideUnitIndex"
NO_GUIDE = 0
TRUST_THRESHOLD = 100
ELIGIBILITY_COMMAND = "CMenuGuideSelect::IsAppend"
ELIGIBILITY_EXPRESSION = f"(IsHaveTrustParam()==1)&&(GetLoyalty()>={TRUST_THRESHOLD});"
_SPACE = r"[ \t\r\n]*"
_ELIGIBILITY = re.compile(
    rf"{_SPACE}\({_SPACE}IsHaveTrustParam{_SPACE}\({_SPACE}\){_SPACE}=={_SPACE}1{_SPACE}\)"
    rf"{_SPACE}&&{_SPACE}\({_SPACE}GetLoyalty{_SPACE}\({_SPACE}\){_SPACE}>={_SPACE}{TRUST_THRESHOLD}{_SPACE}\){_SPACE};{_SPACE}"
)


class GuideService:
    def __init__(self, bootstrap, players, cards, asset_reader):
        self.bootstrap, self.players, self.cards = bootstrap, players, cards
        hashes = {}

        def commands(name):
            raw = asset_reader(name)
            if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMITS.max_input_bytes:
                raise ValueError("Guide resource must be bounded bytes")
            payload = decompress_all4(raw, RESOURCE_LIMITS) if raw.startswith(b"ALL4") else raw
            table = parse_table(payload, RESOURCE_LIMITS)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if len(fields) != len(table["fields"]) or fields != {"_ID": 1, "_Cmnd": 32}:
                raise ValueError("Guide resource has an unsupported command schema")
            result = {}
            for row in table["rows"]:
                if not int32(row["_ID"]) or not isinstance(row["_Cmnd"], str):
                    raise ValueError("Guide command has invalid fields")
                result.setdefault(row["_ID"], []).append(row["_Cmnd"])
            hashes[name] = hashlib.sha256(raw).hexdigest()
            return result

        try:
            if not callable(asset_reader):
                raise ValueError("Guide selection requires verified resources")
            event_commands = commands("OnCommandMenuEvent.atb")
            conditions = event_commands.get(command_id(ELIGIBILITY_COMMAND), ())
            if len(conditions) != 1:
                raise ValueError("Guide eligibility command is missing or ambiguous")
            if _ELIGIBILITY.fullmatch(decode_command(conditions[0])) is None:
                raise ValueError("Guide eligibility command differs from the verified expression")
            global_commands = commands("OnCommand.atb")
            bonuses, restricted = {}, set()
            for card_id in cards.card_ids:
                if not int32(card_id) or card_id <= 0 or card_id in bonuses or len(bonuses) >= 65536:
                    raise ValueError("Guide card identifiers are invalid or duplicated")
                card = cards.card(card_id)
                if card.get("CardID") != card_id or not int32(card.get("BonusType")):
                    raise ValueError("Guide card is missing its original trust parameter")
                bonuses[card_id] = card["BonusType"]
                entries = global_commands.get(rating_command_id(card_id), ())
                if len(entries) > 1 or (entries and decode_command(entries[0]).strip()):
                    restricted.add(card_id)
            if not bonuses:
                raise ValueError("Guide card catalog is empty")
            content = {"policy": 1, "master": cards.snapshot.identifier,
                       "client": cards.snapshot.client_hash, "resources": hashes, "bonus_types": bonuses}
            self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            self._bonus_types = MappingProxyType(bonuses)
            self._rating_restricted = frozenset(restricted)
            self._hashes = MappingProxyType(hashes)
            self._config_profile = ProtocolRegistry.load().by_registration[36]
        except (AttributeError, KeyError, TypeError, ValueError, OSError, ProtocolError) as error:
            raise ProtocolError("guide_catalog_invalid", "Guide selection resources are unavailable or unsupported", 503) from error

    def handlers(self):
        return {121: self.save}

    def status(self):
        return {"ready": True, "implemented_operations": [121], "content_version": self.content_version,
                "resource_sha256": dict(self._hashes), "cards": len(self._bonus_types),
                "eligibility": f"Owned instance with original BonusType != 0 and Love >= {TRUST_THRESHOLD}",
                "rating_conditions_unavailable": sorted(self._rating_restricted),
                "clear_value": NO_GUIDE, "reload_operation": 36,
                "native_tested": False,
                "missing_instance": "Preserve saved C1; the native client falls back to its default guide display"}

    @staticmethod
    def _check_selection(selected):
        if not int32(selected) or selected < 0:
            invalid("Guide selection requires a nonnegative instance index")

    def _require_eligible(self, units, selected):
        unit = next((unit for unit in units if unit["GetIndex"] == selected), None)
        if unit is None:
            raise ProtocolError("guide_not_owned", "Guide selection requires an owned instance", 409)
        bonus = self._bonus_types.get(unit["CardID"])
        if bonus is None:
            raise ProtocolError("guide_card_unavailable", "Guide card is absent from the verified catalog", 503)
        if unit["CardID"] in self._rating_restricted:
            raise ProtocolError("guide_rating_unsupported", "Guide card has an unverified rating restriction", 503)
        if bonus == 0 or unit["Love"] < TRUST_THRESHOLD:
            raise ProtocolError("guide_ineligible",
                                f"Guide selection requires a trust-enabled unit at {TRUST_THRESHOLD} or more", 409)

    def _with_selection(self, state, selected):
        units = self.players.validate(state)["units"]
        self.bootstrap.table_rows(state, "config")
        if selected != NO_GUIDE:
            self._require_eligible(units, selected)
        return patch_configuration(state, {GUIDE_FIELD: selected})

    def set_selection(self, state, selected):
        self._check_selection(selected)
        return self._with_selection(state, selected)

    def save(self, envelope, store):
        request = registered(envelope, {121}, {"A7"}, {"A7"})
        selected = scalar(request["A7"], "I")
        self._check_selection(selected)
        for _ in range(3):
            profile = self.players.authenticate(envelope, store)
            updated = self._with_selection(profile.state, selected)
            candidate = replace(profile, state=updated)
            rows = self.bootstrap._rows(candidate, "config")
            full_config = dumps(Container(children=self.bootstrap.columns("config", rows)))
            encode_response(self._config_profile, full_config)
            response = dumps(Container(children=(self.bootstrap._system(profile),
                configuration_delta(configuration_row(updated), (GUIDE_FIELD,)))))
            encode_response(envelope.profile, response)
            if updated == profile.state:
                return response
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving the guide selection", 409)

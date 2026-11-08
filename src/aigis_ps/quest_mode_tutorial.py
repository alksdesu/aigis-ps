from __future__ import annotations

from .quest_modes import ModePolicy


class TutorialPolicy(ModePolicy):
    name = "tutorial"
    quest_types = (4,)
    enabled = True
    gate_quest = 1004

    def validate_row(self, row, catalog) -> None:
        unit_list = row.get("UnitList")
        if type(unit_list) is not int or unit_list <= 0 or not catalog.fixed_units(unit_list):
            raise ValueError("Tactical guide quests require their fixed native team")


POLICY = TutorialPolicy()

from __future__ import annotations

from types import MappingProxyType

from .history_disclosure import HISTORY_TYPE, disclosure, require_disclosed
from .quest_modes import MissionWindow, ModePolicy, StartContext, require_row_values


ROW_VALUES = MappingProxyType({"Type": HISTORY_TYPE})


class HistoryPolicy(ModePolicy):
    name = "history"
    quest_types = (HISTORY_TYPE,)
    enabled = True
    gate_quest = 1004
    # The disclosure check reads no request field, and the client offers entrust only behind it.
    entrust_parity = frozenset({"check_start"})

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "History quest")
        disclosure(catalog)

    def check_start(self, context: StartContext) -> MissionWindow:
        mission = super().check_start(context)
        require_disclosed(context, mission)
        return mission


POLICY = HistoryPolicy()

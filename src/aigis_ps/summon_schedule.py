from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from .alxml import Container, dumps
from .protocol import RequestEnvelope
from .request_fields import registered
from .schedule_capture import SCHEDULE_GROUPS
from .storage import Store

if TYPE_CHECKING:
    from .bootstrap import BootstrapService


SCHEDULE = 70
PACKAGES = 71
OPERATIONS = (SCHEDULE, PACKAGES)
UNIMPLEMENTED_SUMMONS = (11, 12, 13)
# Native A7EE90 applies a childless group as an empty table and the FPR/SH entries zero their counts: nothing is open.
EMPTY_GROUPS = MappingProxyType({operation: tuple(Container(name) for name in SCHEDULE_GROUPS[operation])
                                 for operation in OPERATIONS})


class SummonScheduleService:
    def __init__(self, bootstrap: BootstrapService):
        self.bootstrap = bootstrap

    def handlers(self):
        return {SCHEDULE: self.schedule, PACKAGES: self.packages}

    def status(self):
        return {"ready": True, "implemented_operations": list(OPERATIONS),
                "state_writes": False, "native_tested": False,
                "groups": {str(operation): list(SCHEDULE_GROUPS[operation]) for operation in OPERATIONS},
                "unimplemented_summons": list(UNIMPLEMENTED_SUMMONS),
                "policy": ("Explicit empty schedule: SYSTEM and childless groups only, so no summon lineup, summon "
                           "campaign or servant package is open; summons stay unimplemented and no pool is made up; "
                           "the official schedule is not captured")}

    def schedule(self, envelope: RequestEnvelope, store: Store) -> bytes:
        return self._empty(envelope, store, SCHEDULE)

    def packages(self, envelope: RequestEnvelope, store: Store) -> bytes:
        return self._empty(envelope, store, PACKAGES)

    def _empty(self, envelope: RequestEnvelope, store: Store, operation: int) -> bytes:
        registered(envelope, {operation}, ())
        profile = self.bootstrap._authenticate(envelope, store)
        return dumps(Container(children=(self.bootstrap.system_container(profile), *EMPTY_GROUPS[operation])))

from __future__ import annotations

from .alxml import Column, Container, ValueType


def money_columns(money: int) -> Container:
    # Trade and orb-exchange PGP binds native CGameParamReceiver (A4B6D0): a row table where column A1 is Money.
    return Container("PGP", (Column("A1", ValueType.INT, (money,)),))

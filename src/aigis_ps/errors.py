from __future__ import annotations

from collections.abc import Callable


# Native A7FDF0 returns only after a 200; 66E8B0 answers any HTTP error with a dialog and resends the same body.
REFUSAL_AFTERMATH = "shows a communication error and resends the same request until the server accepts it"
REFUSAL_POLICY = ("Logged, because the native client shows a communication error and resends the refused request "
                  "until it is accepted")


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class UnsupportedOperation(ProtocolError):
    def __init__(self, message: str):
        super().__init__("not_implemented", message, 501)


def caught(action: Callable[[], object]) -> ProtocolError | None:
    try:
        action()
    except ProtocolError as error:
        return error
    return None

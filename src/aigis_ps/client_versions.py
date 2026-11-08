LEGACY_CLIENT_SHA256 = "babe8210a8652fb25b1c61056ee500b05bd9a3ae37f55d1682a2fb8b850802ba"
CURRENT_CLIENT_SHA256 = "d0be510fb38b1d6dfe63c3562c97e45f49d318bf7e155dee309d88efe963f92b"
SUPPORTED_CLIENT_SHA256 = frozenset((LEGACY_CLIENT_SHA256, CURRENT_CLIENT_SHA256))


def require_client_version(value: str) -> str:
    if not isinstance(value, str) or value not in SUPPORTED_CLIENT_SHA256:
        raise ValueError("Unknown or modified native client version")
    return value

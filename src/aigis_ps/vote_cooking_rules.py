from dataclasses import asdict, dataclass
from pathlib import Path

from .local_io import deserialize, read_bounded


MAX_RULE_BYTES = 8 * 1024


@dataclass(frozen=True)
class VoteCookingRules:
    enabled: bool = True
    minimum_votes: int = 1
    copies: int = 1

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Vote cooking enabled must be boolean")
        for name, maximum in (("minimum_votes", 999), ("copies", 10)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Vote cooking rules require bounded positive integers")

    def to_dict(self):
        return asdict(self)


def load_vote_cooking_rules(path):
    if path is None:
        return VoteCookingRules()
    data = deserialize(read_bounded(Path(path), MAX_RULE_BYTES).decode("utf-8-sig"))
    if not isinstance(data, dict) or set(data) - {"enabled", "minimum_votes", "copies"}:
        raise ValueError("Unknown vote cooking rules")
    return VoteCookingRules(**data)

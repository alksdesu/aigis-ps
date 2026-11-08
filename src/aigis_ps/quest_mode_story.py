from __future__ import annotations

from .quest_modes import ModePolicy


class StoryPolicy(ModePolicy):
    name = "story"
    quest_types = (0,)
    enabled = True
    hard_mode = True


POLICY = StoryPolicy()

"""Bounded, non-deterministic first-command reaction scheduling."""
from __future__ import annotations
import random

BASE_SECONDS = 0.150
JITTER_SECONDS = 0.300
MIN_PER_CHARACTER = 0.015
MAX_PER_CHARACTER = 0.025
MAX_SECONDS = 3.0


def reaction_delay(matched_text: str | None = None, *, immediate: bool = False) -> float:
    """Sample once per action; match length uses the actual matched substring."""
    if immediate:
        return 0.0
    characters = len(matched_text) if matched_text is not None else 0
    per_character = random.uniform(MIN_PER_CHARACTER, MAX_PER_CHARACTER)
    jitter = random.uniform(0.0, JITTER_SECONDS)
    return min(MAX_SECONDS, BASE_SECONDS + characters * per_character + jitter)

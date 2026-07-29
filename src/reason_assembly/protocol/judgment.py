"""Pure helpers for stable judgment aggregation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable


def aggregate_ballots(ballots: Iterable[str], candidates: Iterable[str]) -> str | None:
    """Return a deterministic plurality winner restricted to known candidates."""
    allowed = set(candidates)
    counts = Counter(ballot for ballot in ballots if ballot in allowed)
    if not counts:
        return None
    best = max(counts.values())
    return min(candidate for candidate, count in counts.items() if count == best)

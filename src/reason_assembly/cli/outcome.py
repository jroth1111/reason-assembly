"""Pure outcome attribution helpers."""

from __future__ import annotations

from collections.abc import Iterable


def confirmed_subjects(observations: Iterable[object]) -> set[str]:
    return {
        str(getattr(item, "subject_id"))
        for item in observations
        if getattr(item, "status", None) == "confirmed"
    }


def invalidated_subjects(observations: Iterable[object]) -> set[str]:
    return {
        str(getattr(item, "subject_id"))
        for item in observations
        if getattr(item, "status", None) == "disconfirmed"
    }

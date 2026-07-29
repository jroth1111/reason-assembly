"""Focused protocol responsibilities used by the council orchestrator."""

from .finality import finality_certificate, propagate_taint
from .judgment import aggregate_ballots

__all__ = ["aggregate_ballots", "finality_certificate", "propagate_taint"]

"""Engine-independent helpers for summarising paired arena matches."""

from .core import (
    Outcome,
    classify_outcome,
    summarize_latency,
    summarize_matches,
    wilson_interval,
)

__all__ = [
    "Outcome",
    "classify_outcome",
    "summarize_latency",
    "summarize_matches",
    "wilson_interval",
]

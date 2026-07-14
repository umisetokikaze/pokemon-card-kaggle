"""JSON-safe aggregation for two-player arena results.

The module deliberately accepts plain mappings and sequences, so runners do
not need to import any game-engine types.  Player data is always interpreted
by player index, not by a display order.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Any, Iterable, Mapping, Sequence


FAULT_KINDS = ("invalid", "exception", "timeout", "process_timeout", "engine_failure")


@dataclass(frozen=True)
class Outcome:
    """A classified game result. ``winner`` is a player index for normal wins."""

    kind: str
    winner: int | None = None


def _as_name(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "name"):
        value = value.name
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _fault_kind(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, Mapping):
        for key in ("kind", "outcome", "type", "fault", "reason", "status"):
            if key in value:
                result = _fault_kind(value[key])
                if result:
                    return result
        # A non-empty fault record without a known classification is an engine failure.
        return "engine_failure"
    name = _as_name(value)
    if not name or name in {"done", "ok", "success", "none", "null"}:
        return None
    if "process" in name and "timeout" in name:
        return "process_timeout"
    if "timeout" in name:
        return "timeout"
    if "invalid" in name:
        return "invalid"
    if "exception" in name or "error" in name:
        return "exception"
    if "engine" in name or "failure" in name or "fail" in name:
        return "engine_failure"
    return "engine_failure"


def _values(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (value[key] for key in sorted(value, key=str))
    if isinstance(value, (str, bytes)):
        return (value,)
    try:
        return iter(value)
    except TypeError:
        return (value,)


def classify_outcome(
    statuses: Sequence[Any] | Mapping[int, Any] | None,
    rewards: Sequence[Any] | Mapping[int, Any] | None,
    faults: Any = None,
) -> Outcome:
    """Classify final player-indexed data, giving explicit faults priority.

    ``kind`` is one of ``win``, ``draw``, ``invalid``, ``exception``,
    ``timeout``, ``process_timeout``, or ``engine_failure``.  A normal win
    carries its winning player index in ``winner``.
    """
    for fault in _values(faults):
        kind = _fault_kind(fault)
        if kind:
            return Outcome(kind)
    status_values = list(_values(statuses))
    for status in status_values:
        kind = _fault_kind(status)
        if kind:
            return Outcome(kind)
    if len(status_values) != 2 or any(
        _as_name(status) not in {"done", "ok", "success", ""}
        for status in status_values
    ):
        return Outcome("invalid")
    reward_values = list(_values(rewards))
    if len(reward_values) != 2 or not all(
        isinstance(value, Real) and math.isfinite(value) for value in reward_values
    ):
        return Outcome("invalid")
    if reward_values[0] == reward_values[1]:
        return Outcome("draw")
    return Outcome("win", 0 if reward_values[0] > reward_values[1] else 1)


def summarize_latency(
    values_ns: Iterable[int | float],
) -> dict[str, int | float | None]:
    """Summarise nanosecond values using deterministic nearest-rank percentiles."""
    values = sorted(
        int(value)
        for value in values_ns
        if isinstance(value, Real) and math.isfinite(value) and value >= 0
    )
    if not values:
        return {
            "count": 0,
            "mean_ns": None,
            "max_ns": None,
            "p95_ns": None,
            "p99_ns": None,
        }

    def percentile(percent: float) -> int:
        return values[max(0, math.ceil(percent * len(values)) - 1)]

    return {
        "count": len(values),
        "mean_ns": sum(values) / len(values),
        "max_ns": values[-1],
        "p95_ns": percentile(0.95),
        "p99_ns": percentile(0.99),
    }


def wilson_interval(
    successes: int, trials: int, z: float = 1.959963984540054
) -> tuple[float | None, float | None]:
    """Return the two-sided Wilson 95% binomial confidence interval."""
    if trials <= 0:
        return (None, None)
    successes = min(max(successes, 0), trials)
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials) / denominator
    return (centre - margin, centre + margin)


def summarize_matches(
    matches: Iterable[Mapping[str, Any]],
    *,
    subject_index: int = 0,
    latencies_ns: Iterable[int | float] | None = None,
) -> dict[str, Any]:
    """Return a subject-view summary from paired, player-indexed match records.

    Each record supplies ``statuses`` and ``rewards`` and may supply ``faults``,
    ``subject_index`` and ``latency_ns``.  Fault outcomes are excluded from
    normal-game rates. Draws are non-wins for ``win_rate``.
    """
    wins = losses = draws = 0
    faults = {kind: 0 for kind in FAULT_KINDS}
    observed_latencies = list(latencies_ns or ())
    for match in matches:
        seat = match.get("subject_index", subject_index)
        outcome = classify_outcome(
            match.get("statuses"),
            match.get("rewards"),
            match.get("faults", match.get("fault")),
        )
        if outcome.kind in faults:
            faults[outcome.kind] += 1
        elif outcome.kind == "draw":
            draws += 1
        elif outcome.winner == seat:
            wins += 1
        else:
            losses += 1
        if "latency_ns" in match:
            observed_latencies.append(match["latency_ns"])
    normal_games = wins + losses + draws
    win_rate: float | None
    score_rate: float | None
    decisive_win_rate: float | None
    ci_low: float | None
    ci_high: float | None
    if normal_games:
        win_rate = wins / normal_games
        score_rate = (wins + 0.5 * draws) / normal_games
        decisive_win_rate = wins / (wins + losses) if wins + losses else None
        ci_low, ci_high = wilson_interval(wins, normal_games)
    else:
        win_rate = score_rate = decisive_win_rate = ci_low = ci_high = None
    return {
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "normal_games": normal_games,
        "faults": faults,
        "win_rate": win_rate,
        "score_rate": score_rate,
        "decisive_win_rate": decisive_win_rate,
        "wilson_95": {"low": ci_low, "high": ci_high},
        "latency": summarize_latency(observed_latencies),
    }

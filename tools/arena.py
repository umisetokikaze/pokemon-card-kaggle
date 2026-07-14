from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arena.runner import (  # noqa: E402
    AgentArtifact,
    load_environment_factory,
    run_arena,
    write_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run sequential paired CABT matches.")
    parser.add_argument(
        "--champion", type=Path, default=Path("main.py"), help="Champion main.py path."
    )
    parser.add_argument(
        "--champion-deck",
        type=Path,
        default=Path("deck.csv"),
        help="Champion deck.csv path.",
    )
    parser.add_argument(
        "--challenger",
        type=Path,
        default=Path("main.py"),
        help="Challenger main.py path.",
    )
    parser.add_argument(
        "--challenger-deck",
        type=Path,
        default=Path("deck.csv"),
        help="Challenger deck.csv path.",
    )
    parser.add_argument(
        "--pairs", type=int, default=1, help="Number of two-leg seat-swapped pairs."
    )
    parser.add_argument(
        "--matchup", default="unspecified", help="Stable label stored in the report."
    )
    parser.add_argument(
        "--action-timeout-ms",
        type=int,
        default=1000,
        help="Per-agent-call local deadline; 0 disables it.",
    )
    parser.add_argument(
        "--game-timeout-seconds",
        type=float,
        default=900,
        help="Kill the isolated match process after this many seconds.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Python-side base seed; the native engine RNG is not controlled.",
    )
    parser.add_argument(
        "--output", type=Path, help="JSON output path. Defaults below artifacts/arena."
    )
    parser.add_argument(
        "--debug-engine",
        action="store_true",
        help="Enable Kaggle environment debug output.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = ROOT / "artifacts" / "arena" / f"arena-{stamp}.json"
    elif not output.is_absolute():
        output = ROOT / output

    def show_progress(record: dict) -> None:
        outcome = record["outcome"]
        winner = f" winner={outcome['winner']}" if outcome["winner"] else ""
        print(
            f"{record['match_id']}: {outcome['kind']}{winner}",
            file=sys.stderr,
            flush=True,
        )

    champion = AgentArtifact.load("champion", args.champion, args.champion_deck)
    challenger = AgentArtifact.load("challenger", args.challenger, args.challenger_deck)
    report = run_arena(
        load_environment_factory(),
        champion,
        challenger,
        pairs=args.pairs,
        matchup=args.matchup,
        action_timeout_ms=args.action_timeout_ms,
        seed=args.seed,
        root=ROOT,
        debug_engine=args.debug_engine,
        isolate_matches=True,
        game_timeout_seconds=args.game_timeout_seconds,
        progress=show_progress,
    )
    write_report(report, output)
    summary = report["summary"]
    fault_count = sum(summary["faults"].values())
    print(f"REPORT: {output}")
    print(
        f"RESULT[challenger]: W={summary['wins']} L={summary['losses']} D={summary['draws']} "
        f"faults={fault_count} win_rate={summary['win_rate']} wilson95={summary['wilson_95']}"
    )
    return 0 if fault_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

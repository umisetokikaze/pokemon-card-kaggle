from __future__ import annotations

import contextlib
import io
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import agent  # noqa: E402


def load_deck() -> list[int]:
    lines = (ROOT / "deck.csv").read_text(encoding="utf-8").splitlines()
    deck = [int(line) for line in lines if line.strip()]
    if len(deck) != 60:
        raise ValueError(f"deck.csv must contain 60 cards, found {len(deck)}")
    return deck


DECK = load_deck()


def load_environment_factory():
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            from kaggle_environments import make
    except Exception:
        sys.stdout.write(stdout.getvalue())
        sys.stderr.write(stderr.getvalue())
        raise
    return make


def random_agent(obs_dict: dict) -> list[int]:
    select = obs_dict["select"]
    if select is None:
        return DECK

    return random.sample(
        list(range(len(select["option"]))),
        select["maxCount"],
    )


def main() -> None:
    random.seed(0)
    make = load_environment_factory()
    env = make("cabt", configuration={"decks": [DECK, DECK]}, debug=True)
    env.run([agent, random_agent])

    statuses = [state.status for state in env.state]
    rewards = [state.reward for state in env.state]
    if any(status == "ERROR" for status in statuses):
        raise RuntimeError(f"self-play failed: statuses={statuses}, rewards={rewards}")

    print(f"PASS: statuses={statuses}, rewards={rewards}")


if __name__ == "__main__":
    main()

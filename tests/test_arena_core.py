import json
import unittest

from arena import (
    classify_outcome,
    summarize_latency,
    summarize_matches,
    wilson_interval,
)


def match(rewards, *, statuses=("DONE", "DONE"), **extra):
    return {"statuses": statuses, "rewards": rewards, **extra}


class ArenaCoreTests(unittest.TestCase):
    def test_zero_wins_and_draws_are_non_wins(self):
        result = summarize_matches([match((0, 1)), match((1, 1))])
        self.assertEqual(
            (result["wins"], result["losses"], result["draws"], result["normal_games"]),
            (0, 1, 1, 2),
        )
        self.assertEqual(result["win_rate"], 0)
        self.assertEqual(result["score_rate"], 0.25)
        self.assertEqual(result["decisive_win_rate"], 0)

    def test_all_wins_and_wilson_interval(self):
        result = summarize_matches([match((2, 0)), match((3, 1))])
        self.assertEqual(result["wins"], 2)
        self.assertEqual(result["win_rate"], 1)
        self.assertEqual(result["score_rate"], 1)
        self.assertEqual(result["decisive_win_rate"], 1)
        low, high = wilson_interval(2, 2)
        self.assertEqual(result["wilson_95"], {"low": low, "high": high})
        self.assertTrue(0 < low < high == 1)

    def test_no_normal_games_returns_null_rates_and_ci(self):
        result = summarize_matches([match((1, 0), faults="timeout")])
        self.assertEqual(result["normal_games"], 0)
        self.assertIsNone(result["win_rate"])
        self.assertIsNone(result["score_rate"])
        self.assertIsNone(result["decisive_win_rate"])
        self.assertEqual(result["wilson_95"], {"low": None, "high": None})

    def test_fault_precedence_over_done_status_and_rewards(self):
        outcome = classify_outcome(
            ("DONE", "DONE"), (10, 0), {1: {"kind": "process_timeout"}}
        )
        self.assertEqual((outcome.kind, outcome.winner), ("process_timeout", None))

    def test_subject_seat_normalization(self):
        result = summarize_matches(
            [match((0, 5), subject_index=1), match((6, 0), subject_index=1)]
        )
        self.assertEqual((result["wins"], result["losses"]), (1, 1))

    def test_latency_uses_nearest_rank_and_omits_bad_values(self):
        self.assertEqual(
            summarize_latency([1, 2, 3, 4, 5, -1, float("nan")]),
            {
                "count": 5,
                "mean_ns": 3,
                "max_ns": 5,
                "p95_ns": 5,
                "p99_ns": 5,
            },
        )
        self.assertEqual(
            summarize_latency([]),
            {
                "count": 0,
                "mean_ns": None,
                "max_ns": None,
                "p95_ns": None,
                "p99_ns": None,
            },
        )

    def test_summary_is_strict_json_safe(self):
        result = summarize_matches(
            [
                match((1, 0), latency_ns=float("inf")),
                match((0, 0), faults={"kind": "exception"}),
            ]
        )
        self.assertTrue(json.dumps(result, allow_nan=False))

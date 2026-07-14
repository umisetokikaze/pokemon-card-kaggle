from __future__ import annotations

from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

from arena.runner import (
    ActionDeadlineExceeded,
    AgentActionInvalid,
    AgentArtifact,
    AgentMonitor,
    LoadedAgent,
    load_deck,
    run_arena,
    run_match_isolated,
)


def slow_match_worker(connection, *_args):
    time.sleep(5)
    connection.close()


def large_result_worker(connection, *_args):
    connection.send(
        (
            "ok",
            (
                {"blob": "x" * (1024 * 1024)},
                {"champion": [], "challenger": []},
            ),
        )
    )
    connection.close()


def sticky_result_worker(connection, *_args):
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    connection.send(
        (
            "ok",
            (
                {"unused": True},
                {"champion": [], "challenger": []},
            ),
        )
    )
    time.sleep(10)


class FakeState:
    def __init__(self, status, reward):
        self.status = status
        self.reward = reward


class FakeEnvironment:
    def __init__(self, decks):
        self.decks = decks
        self.state = [FakeState("ACTIVE", None), FakeState("ACTIVE", None)]
        self.steps = []

    def run(self, agents):
        for index, agent in enumerate(agents):
            self.assert_deck(agent({"select": None, "current": None}), index)
        agents[1](
            {
                "select": {"minCount": 1, "maxCount": 1, "option": [{}]},
                "current": {"firstPlayer": 1},
            }
        )
        self.state = [FakeState("DONE", 1), FakeState("DONE", -1)]

    def assert_deck(self, deck, index):
        if deck != self.decks[index]:
            raise AssertionError("seat/deck mismatch")


class FakeFactory:
    def __init__(self):
        self.deck_orders = []

    def __call__(self, name, *, configuration, debug):
        if name != "cabt":
            raise AssertionError(name)
        self.deck_orders.append(configuration["decks"])
        return FakeEnvironment(configuration["decks"])


class InvalidFakeEnvironment(FakeEnvironment):
    def run(self, agents):
        for index, agent in enumerate(agents):
            self.assert_deck(agent({"select": None, "current": None}), index)
        self.state = [FakeState("INVALID", None), FakeState("DONE", None)]


class InvalidFakeFactory(FakeFactory):
    def __call__(self, name, *, configuration, debug):
        if name != "cabt":
            raise AssertionError(name)
        return InvalidFakeEnvironment(configuration["decks"])


class PeerFaultFakeEnvironment(FakeEnvironment):
    def run(self, agents):
        for index, agent in enumerate(agents):
            self.assert_deck(agent({"select": None, "current": None}), index)
        try:
            agents[0](
                {
                    "select": {"minCount": 1, "maxCount": 1, "option": []},
                    "current": {"firstPlayer": 0},
                }
            )
        except AgentActionInvalid:
            self.state = [FakeState("ERROR", None), FakeState("ERROR", None)]


class PeerFaultFakeFactory(FakeFactory):
    def __call__(self, name, *, configuration, debug):
        if name != "cabt":
            raise AssertionError(name)
        return PeerFaultFakeEnvironment(configuration["decks"])


class ChallengerWinsFakeEnvironment(FakeEnvironment):
    def run(self, agents):
        for index, agent in enumerate(agents):
            self.assert_deck(agent({"select": None, "current": None}), index)
        agents[0](
            {
                "select": {"minCount": 1, "maxCount": 1, "option": [{}]},
                "current": {"firstPlayer": 0},
            }
        )
        winner = next(index for index, deck in enumerate(self.decks) if deck[0] == 22)
        self.state = [
            FakeState("DONE", 1 if index == winner else -1) for index in range(2)
        ]


class ChallengerWinsFakeFactory(FakeFactory):
    def __call__(self, name, *, configuration, debug):
        if name != "cabt":
            raise AssertionError(name)
        return ChallengerWinsFakeEnvironment(configuration["decks"])


class ArenaRunnerTests(unittest.TestCase):
    def test_load_deck_requires_sixty_integers(self):
        with tempfile.TemporaryDirectory() as directory:
            deck_path = Path(directory) / "deck.csv"
            deck_path.write_text("\n".join(["7"] * 60), encoding="utf-8")
            self.assertEqual(load_deck(deck_path), [7] * 60)
            deck_path.write_text("7\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly 60"):
                load_deck(deck_path)

    def test_monitor_validates_action_and_records_first_player(self):
        module = type("Module", (), {})()

        def callback(observation):
            return [3] * 60 if observation["select"] is None else [0]

        loaded = LoadedAgent("fake", module, callback)
        monitor = AgentMonitor("champion", loaded, tuple([3] * 60), timeout_ms=0)
        self.assertEqual(monitor({"select": None, "current": None}), [3] * 60)
        self.assertEqual(
            monitor(
                {
                    "select": {"minCount": 1, "maxCount": 1, "option": [{}]},
                    "current": {"firstPlayer": 1},
                }
            ),
            [0],
        )
        self.assertEqual(monitor.first_player_index, 1)
        self.assertEqual(len(monitor.latencies_ns), 2)
        self.assertEqual(len(monitor.selection_latencies_ns), 1)
        self.assertEqual(monitor.kaggle_callback().__code__.co_argcount, 1)

    def test_monitor_marks_invalid_action_before_engine_dispatch(self):
        module = type("Module", (), {})()
        loaded = LoadedAgent("fake", module, lambda _observation: [9])
        monitor = AgentMonitor("challenger", loaded, tuple([3] * 60), timeout_ms=0)
        with self.assertRaises(AgentActionInvalid):
            monitor(
                {
                    "select": {"minCount": 1, "maxCount": 1, "option": [{}]},
                    "current": {"firstPlayer": 0},
                }
            )
        self.assertEqual(monitor.faults[0]["kind"], "invalid")

    def test_monitor_marks_deadline_separately_from_general_exceptions(self):
        module = type("Module", (), {})()

        def callback(_observation):
            raise ActionDeadlineExceeded("late")

        monitor = AgentMonitor(
            "challenger",
            LoadedAgent("fake", module, callback),
            tuple([3] * 60),
            timeout_ms=0,
        )
        with self.assertRaises(ActionDeadlineExceeded):
            monitor({"select": None, "current": None})
        self.assertEqual(monitor.faults[0]["kind"], "timeout")

    def test_run_arena_swaps_seats_and_normalizes_champion_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            champion = self.make_artifact(root, "champion", 11)
            challenger = self.make_artifact(root, "challenger", 22)
            factory = FakeFactory()
            report = run_arena(
                factory,
                champion,
                challenger,
                pairs=1,
                matchup="fake",
                action_timeout_ms=0,
                seed=4,
                root=root,
            )

        self.assertEqual(report["matches"][0]["seats"], ["champion", "challenger"])
        self.assertEqual(report["matches"][1]["seats"], ["challenger", "champion"])
        self.assertEqual(report["summary"]["wins"], 1)
        self.assertEqual(report["summary"]["losses"], 1)
        self.assertEqual(
            report["summary"]["faults"],
            {
                "invalid": 0,
                "exception": 0,
                "timeout": 0,
                "process_timeout": 0,
                "engine_failure": 0,
            },
        )
        self.assertEqual(
            report["summary"]["first_player_games"], {"champion": 1, "challenger": 1}
        )
        self.assertEqual(
            factory.deck_orders, [[[11] * 60, [22] * 60], [[22] * 60, [11] * 60]]
        )

    def test_engine_invalid_status_becomes_a_role_attributed_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = run_arena(
                InvalidFakeFactory(),
                self.make_artifact(root, "champion", 11),
                self.make_artifact(root, "challenger", 22),
                pairs=1,
                matchup="invalid",
                action_timeout_ms=0,
                seed=0,
                root=root,
            )

        self.assertEqual(report["summary"]["faults"]["invalid"], 2)
        self.assertEqual(report["matches"][0]["faults"][0]["role"], "champion")
        self.assertEqual(report["matches"][1]["faults"][0]["role"], "challenger")

    def test_peer_status_is_not_duplicated_after_an_attributed_agent_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = run_arena(
                PeerFaultFakeFactory(),
                self.make_artifact(root, "champion", 11),
                self.make_artifact(root, "challenger", 22),
                pairs=1,
                matchup="peer-fault",
                action_timeout_ms=0,
                seed=0,
                root=root,
            )

        self.assertEqual(report["summary"]["faults"]["invalid"], 2)
        self.assertEqual(len(report["matches"][0]["faults"]), 1)
        self.assertEqual(report["matches"][0]["faults"][0]["role"], "champion")
        self.assertEqual(len(report["matches"][1]["faults"]), 1)
        self.assertEqual(report["matches"][1]["faults"][0]["role"], "challenger")

    def test_sibling_modules_are_isolated_between_agents_and_legs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = run_arena(
                FakeFactory(),
                self.make_helper_artifact(root, "champion", 11),
                self.make_helper_artifact(root, "challenger", 22),
                pairs=1,
                matchup="module-isolation",
                action_timeout_ms=0,
                seed=0,
                root=root,
            )

        self.assertEqual(report["summary"]["faults"]["exception"], 0)
        self.assertNotIn("helper", sys.modules)

    def test_isolated_match_process_is_killed_at_the_hard_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = {
                "champion": self.make_artifact(root, "champion", 11),
                "challenger": self.make_artifact(root, "challenger", 22),
            }
            record, latency = run_match_isolated(
                artifacts,
                pair_id=0,
                leg=0,
                action_timeout_ms=0,
                seed=0,
                game_timeout_seconds=0.05,
                worker_target=slow_match_worker,
            )

        self.assertEqual(record["outcome"]["kind"], "process_timeout")
        self.assertEqual(record["faults"][0]["kind"], "process_timeout")
        self.assertEqual(latency, {"champion": [], "challenger": []})

    def test_parent_drains_large_child_result_before_joining(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = {
                "champion": self.make_artifact(root, "champion", 11),
                "challenger": self.make_artifact(root, "challenger", 22),
            }
            record, latency = run_match_isolated(
                artifacts,
                pair_id=0,
                leg=0,
                action_timeout_ms=0,
                seed=0,
                game_timeout_seconds=3,
                worker_target=large_result_worker,
            )

        self.assertEqual(len(record["blob"]), 1024 * 1024)
        self.assertEqual(latency, {"champion": [], "challenger": []})

    def test_result_process_that_does_not_exit_is_reaped_and_faulted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = {
                "champion": self.make_artifact(root, "champion", 11),
                "challenger": self.make_artifact(root, "challenger", 22),
            }
            record, latency = run_match_isolated(
                artifacts,
                pair_id=0,
                leg=0,
                action_timeout_ms=0,
                seed=0,
                game_timeout_seconds=8,
                worker_target=sticky_result_worker,
            )

        self.assertEqual(record["outcome"]["kind"], "engine_failure")
        self.assertIn("forced termination", record["faults"][0]["message"])
        self.assertEqual(latency, {"champion": [], "challenger": []})

    def test_top_level_rates_and_promotion_gate_use_challenger_perspective(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = run_arena(
                ChallengerWinsFakeFactory(),
                self.make_artifact(root, "champion", 11),
                self.make_artifact(root, "challenger", 22),
                pairs=1,
                matchup="challenger-wins",
                action_timeout_ms=0,
                seed=0,
                root=root,
            )

        summary = report["summary"]
        self.assertEqual(summary["perspective"], "challenger")
        self.assertEqual((summary["wins"], summary["losses"]), (2, 0))
        self.assertEqual(summary["perspectives"]["challenger"]["win_rate"], 1)
        self.assertEqual(summary["perspectives"]["champion"]["win_rate"], 0)
        self.assertFalse(summary["gate"]["promotion_candidate"])

    def make_artifact(self, root: Path, role: str, card_id: int) -> AgentArtifact:
        artifact_root = root / role
        artifact_root.mkdir()
        deck_path = artifact_root / "deck.csv"
        deck_path.write_text("\n".join([str(card_id)] * 60), encoding="utf-8")
        agent_path = artifact_root / "main.py"
        agent_path.write_text(
            f"DECK = [{card_id}] * 60\n"
            "def agent(observation):\n"
            "    return DECK if observation['select'] is None else [0]\n",
            encoding="utf-8",
        )
        return AgentArtifact.load(role, agent_path, deck_path)

    def make_helper_artifact(
        self, root: Path, role: str, card_id: int
    ) -> AgentArtifact:
        artifact_root = root / role
        artifact_root.mkdir()
        deck_path = artifact_root / "deck.csv"
        deck_path.write_text("\n".join([str(card_id)] * 60), encoding="utf-8")
        (artifact_root / "helper.py").write_text(
            f"CARD_ID = {card_id}\n",
            encoding="utf-8",
        )
        agent_path = artifact_root / "main.py"
        agent_path.write_text(
            "def agent(observation):\n"
            "    import helper\n"
            "    return [helper.CARD_ID] * 60 if observation['select'] is None else [0]\n",
            encoding="utf-8",
        )
        return AgentArtifact.load(role, agent_path, deck_path)


if __name__ == "__main__":
    unittest.main()

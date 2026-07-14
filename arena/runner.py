"""Sequential paired-match runner for the local CABT environment.

The native CABT engine keeps process-global battle state, so matches are run
one at a time.  Each leg reloads both agent modules to prevent policy globals
from leaking into the next game.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import random
import signal
import sys
import time
import traceback
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

from .core import classify_outcome, summarize_latency, summarize_matches


Agent = Callable[[dict[str, Any]], list[int]]
EnvironmentFactory = Callable[..., Any]
ProcessWorker = Callable[..., None]
_MISSING = object()
_SHARED_MODULE_ROOTS = {"arena", "cg"}


class ActionDeadlineExceeded(TimeoutError):
    """Raised when one local agent call exceeds its configured deadline."""


class AgentActionInvalid(ValueError):
    """Raised before engine dispatch when an agent violates the action contract."""


def load_deck(path: Path) -> list[int]:
    """Load and validate the structural 60-card deck contract."""
    try:
        values = [
            int(line.strip())
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError as exc:
        raise ValueError(f"Could not read deck file: {path}") from exc
    except ValueError as exc:
        raise ValueError(f"Deck contains a non-integer card ID: {path}") from exc
    if len(values) != 60:
        raise ValueError(
            f"Deck must contain exactly 60 card IDs, found {len(values)}: {path}"
        )
    return values


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class AgentArtifact:
    """Paths and immutable deck data for one arena role."""

    role: str
    agent_path: Path
    deck_path: Path
    deck: tuple[int, ...]

    @classmethod
    def load(cls, role: str, agent_path: Path, deck_path: Path) -> "AgentArtifact":
        agent_path = agent_path.resolve()
        deck_path = deck_path.resolve()
        if not agent_path.is_file():
            raise ValueError(f"Agent file does not exist: {agent_path}")
        return cls(role, agent_path, deck_path, tuple(load_deck(deck_path)))

    def metadata(self, root: Path) -> dict[str, Any]:
        return {
            "agent": _display_path(self.agent_path, root),
            "agent_sha256": _sha256(self.agent_path),
            "deck": _display_path(self.deck_path, root),
            "deck_sha256": _sha256(self.deck_path),
        }


@dataclass
class LoadedAgent:
    module_name: str
    module: ModuleType
    callback: Agent
    root: Path = field(default_factory=Path.cwd)
    local_modules: dict[str, ModuleType] = field(default_factory=dict)

    @contextlib.contextmanager
    def activate(self):
        """Expose this agent's sibling modules only for the current call."""
        previous_modules = {
            name: sys.modules.get(name, _MISSING) for name in self.local_modules
        }
        for name, module in self.local_modules.items():
            sys.modules[name] = module

        inserted_path = str(self.root)
        sys.path.insert(0, inserted_path)
        before_names = set(sys.modules)
        try:
            with _working_directory(self.root):
                yield
        finally:
            candidate_names = (set(sys.modules) - before_names) | set(
                self.local_modules
            )
            discovered = _detach_local_modules(
                self.root,
                candidate_names,
                excluded={self.module_name},
            )
            self.local_modules.update(discovered)
            for name, previous in previous_modules.items():
                if previous is _MISSING:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous  # type: ignore[assignment]
            if sys.path and sys.path[0] == inserted_path:
                sys.path.pop(0)
            else:
                with contextlib.suppress(ValueError):
                    sys.path.remove(inserted_path)

    def close(self) -> None:
        sys.modules.pop(self.module_name, None)
        for name, module in self.local_modules.items():
            if sys.modules.get(name) is module:
                sys.modules.pop(name, None)


@contextlib.contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _module_is_below(module: ModuleType, root: Path) -> bool:
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return False
    try:
        Path(module_file).resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _detach_local_modules(
    root: Path,
    names: Iterable[str],
    *,
    excluded: set[str],
) -> dict[str, ModuleType]:
    detached: dict[str, ModuleType] = {}
    for name in names:
        if name in excluded or name.split(".", 1)[0] in _SHARED_MODULE_ROOTS:
            continue
        module = sys.modules.get(name)
        if isinstance(module, ModuleType) and _module_is_below(module, root):
            detached[name] = module
            sys.modules.pop(name, None)
    return detached


def load_agent(artifact: AgentArtifact, match_id: str) -> LoadedAgent:
    """Load a fresh agent module while satisfying adjacent ``deck.csv`` reads."""
    safe_match_id = "".join(char if char.isalnum() else "_" for char in match_id)
    module_name = f"_arena_{artifact.role}_{safe_match_id}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, artifact.agent_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create a module spec for {artifact.agent_path}")
    module = importlib.util.module_from_spec(spec)
    before_names = set(sys.modules)
    sys.modules[module_name] = module
    inserted_path = str(artifact.agent_path.parent)
    sys.path.insert(0, inserted_path)
    try:
        with _working_directory(artifact.agent_path.parent):
            spec.loader.exec_module(module)
        callback = getattr(module, "agent", None)
        if not callable(callback):
            raise TypeError(
                f"Agent module has no callable 'agent': {artifact.agent_path}"
            )
        reset = getattr(module, "reset_for_match", None)
        if callable(reset):
            reset()
        local_modules = _detach_local_modules(
            artifact.agent_path.parent,
            set(sys.modules) - before_names,
            excluded={module_name},
        )
        return LoadedAgent(
            module_name,
            module,
            callback,
            artifact.agent_path.parent,
            local_modules,
        )
    except Exception:
        sys.modules.pop(module_name, None)
        _detach_local_modules(
            artifact.agent_path.parent,
            set(sys.modules) - before_names,
            excluded={module_name},
        )
        raise
    finally:
        if sys.path and sys.path[0] == inserted_path:
            sys.path.pop(0)
        else:
            with contextlib.suppress(ValueError):
                sys.path.remove(inserted_path)


@contextlib.contextmanager
def _action_deadline(timeout_ms: int):
    if timeout_ms <= 0:
        yield
        return
    if not hasattr(signal, "setitimer") or not hasattr(signal, "SIGALRM"):
        raise RuntimeError(
            "Per-action deadlines require Linux/WSL signal.setitimer support."
        )

    def handle_timeout(_signum: int, _frame: Any) -> None:
        raise ActionDeadlineExceeded(f"Agent call exceeded {timeout_ms} ms")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_ms / 1000)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _validate_action(
    action: Any, observation: Mapping[str, Any], deck: Sequence[int]
) -> None:
    if not isinstance(action, list) or not all(
        isinstance(index, int) and not isinstance(index, bool) for index in action
    ):
        raise AgentActionInvalid("Agent action must be list[int].")
    select = observation.get("select")
    if select is None:
        if action != list(deck):
            raise AgentActionInvalid(
                "Agent's initial deck does not match the arena deck file."
            )
        return
    if not isinstance(select, Mapping):
        raise AgentActionInvalid(
            "Observation select field is neither null nor an object."
        )
    minimum = select.get("minCount")
    maximum = select.get("maxCount")
    options = select.get("option")
    if (
        not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or not isinstance(options, Sequence)
    ):
        raise AgentActionInvalid("Observation select contract is incomplete.")
    if not minimum <= len(action) <= maximum:
        raise AgentActionInvalid(
            f"Action count {len(action)} is outside [{minimum}, {maximum}]."
        )
    if len(set(action)) != len(action):
        raise AgentActionInvalid("Agent action contains duplicate option indexes.")
    if any(index < 0 or index >= len(options) for index in action):
        raise AgentActionInvalid("Agent action contains an out-of-range option index.")


@dataclass
class AgentMonitor:
    """Measure and validate one freshly loaded agent for a single match."""

    role: str
    loaded: LoadedAgent
    deck: tuple[int, ...]
    timeout_ms: int
    latencies_ns: list[int] = field(default_factory=list)
    selection_latencies_ns: list[int] = field(default_factory=list)
    faults: list[dict[str, Any]] = field(default_factory=list)
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    first_player_index: int | None = None
    call_count: int = 0
    telemetry: dict[str, Any] = field(default_factory=dict)

    def _observe_first_player(self, observation: Mapping[str, Any]) -> None:
        current = observation.get("current")
        if isinstance(current, Mapping):
            first_player = current.get("firstPlayer")
            if isinstance(first_player, int) and first_player in (0, 1):
                self.first_player_index = first_player

    def _record_fault(self, kind: str, phase: str, exc: Exception) -> None:
        self.faults.append(
            {
                "kind": kind,
                "role": self.role,
                "phase": phase,
                "call_index": self.call_count,
                "exception_type": type(exc).__name__,
                "message": str(exc)[:500],
            }
        )

    def __call__(self, observation: dict[str, Any]) -> list[int]:
        self._observe_first_player(observation)
        phase = "initial_deck" if observation.get("select") is None else "selection"
        started = time.perf_counter_ns()
        self.call_count += 1
        try:
            with self.loaded.activate(), _action_deadline(self.timeout_ms):
                action = self.loaded.callback(observation)
            _validate_action(action, observation, self.deck)
            return action
        except ActionDeadlineExceeded as exc:
            self._record_fault("timeout", phase, exc)
            raise
        except AgentActionInvalid as exc:
            self._record_fault("invalid", phase, exc)
            raise
        except Exception as exc:
            self._record_fault("exception", phase, exc)
            raise
        finally:
            elapsed = time.perf_counter_ns() - started
            self.latencies_ns.append(elapsed)
            if phase == "selection":
                self.selection_latencies_ns.append(elapsed)

    def kaggle_callback(self) -> Agent:
        """Return a one-argument function accepted by Kaggle's arity probe."""

        def callback(observation: dict[str, Any]) -> list[int]:
            return self(observation)

        callback.__name__ = f"arena_{self.role}_agent"
        return callback

    def drain_telemetry(self) -> None:
        hook = getattr(self.loaded.module, "drain_arena_events", None)
        if not callable(hook):
            self.telemetry = {"supported": False, "event_count": 0, "search_leak": None}
            return
        try:
            with self.loaded.activate():
                events = hook()
            if events is None:
                events = []
            if not isinstance(events, list) or not all(
                isinstance(event, Mapping) for event in events
            ):
                raise TypeError("drain_arena_events() must return list[dict].")
            counts: dict[str, int] = {}
            for event in events:
                kind = str(event.get("kind", "unknown"))
                counts[kind] = counts.get(kind, 0) + 1
                if kind == "fallback":
                    self.fallbacks.append(dict(event))
            opened = counts.get("search_opened", 0) + counts.get("search_begin", 0)
            released = counts.get("search_released", 0) + counts.get(
                "search_release", 0
            )
            self.telemetry = {
                "supported": True,
                "event_count": len(events),
                "event_kinds": counts,
                "search_leak": max(0, opened - released),
            }
        except Exception as exc:
            self.telemetry = {
                "supported": True,
                "event_count": 0,
                "search_leak": None,
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }

    def record(self, seat: int, first_player_index: int | None) -> dict[str, Any]:
        return {
            "seat": seat,
            "went_first": None
            if first_player_index is None
            else seat == first_player_index,
            "latency": summarize_latency(self.latencies_ns),
            "selection_latency": summarize_latency(self.selection_latencies_ns),
            "faults": self.faults,
            "fallbacks": self.fallbacks,
            "telemetry": self.telemetry,
        }


def _status_name(status: Any) -> str | None:
    if status is None:
        return None
    return str(getattr(status, "name", status))


def _status_fault_kind(status: str | None) -> str | None:
    if status is None or status.upper() == "DONE":
        return None
    normalized = status.lower()
    if "invalid" in normalized:
        return "invalid"
    if "timeout" in normalized:
        return "timeout"
    if "error" in normalized or "exception" in normalized:
        return "exception"
    return "engine_failure"


def _finite_reward(reward: Any) -> int | float | None:
    if (
        isinstance(reward, (int, float))
        and not isinstance(reward, bool)
        and math.isfinite(reward)
    ):
        return reward
    return None


def _mapping_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _first_player_from_environment(
    env: Any, monitors: Iterable[AgentMonitor]
) -> int | None:
    observed = {
        monitor.first_player_index
        for monitor in monitors
        if monitor.first_player_index in (0, 1)
    }
    if len(observed) == 1:
        return observed.pop()
    for step in getattr(env, "steps", ()) or ():
        for state in step:
            observation = _mapping_value(state, "observation")
            current = _mapping_value(observation, "current")
            first_player = _mapping_value(current, "firstPlayer")
            if first_player in (0, 1):
                return first_player
    return None


def _empty_agent_record(
    seat: int, fault: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "seat": seat,
        "went_first": None,
        "latency": summarize_latency([]),
        "selection_latency": summarize_latency([]),
        "faults": [] if fault is None else [fault],
        "fallbacks": [],
        "telemetry": {"supported": False, "event_count": 0, "search_leak": None},
    }


def run_match(
    make_environment: EnvironmentFactory,
    artifacts: Mapping[str, AgentArtifact],
    *,
    pair_id: int,
    leg: int,
    action_timeout_ms: int,
    seed: int,
    debug_engine: bool = False,
) -> tuple[dict[str, Any], dict[str, list[int]]]:
    """Run one leg and return its JSON record plus raw latency samples."""
    seats = ["champion", "challenger"] if leg == 0 else ["challenger", "champion"]
    match_id = f"p{pair_id:04d}-leg{leg}"
    started = time.perf_counter_ns()
    loaded: dict[str, LoadedAgent] = {}
    monitors: dict[str, AgentMonitor] = {}
    load_faults: list[dict[str, Any]] = []

    for role in ("champion", "challenger"):
        try:
            loaded[role] = load_agent(artifacts[role], match_id)
            monitors[role] = AgentMonitor(
                role, loaded[role], artifacts[role].deck, action_timeout_ms
            )
        except Exception as exc:
            load_faults.append(
                {
                    "kind": "exception",
                    "role": role,
                    "phase": "load",
                    "exception_type": type(exc).__name__,
                    "message": str(exc)[:500],
                }
            )

    if load_faults:
        for item in loaded.values():
            item.close()
        agents = {}
        for seat, role in enumerate(seats):
            fault = next(
                (value for value in load_faults if value["role"] == role), None
            )
            agents[role] = _empty_agent_record(seat, fault)
        outcome = classify_outcome((None, None), (None, None), load_faults)
        return (
            {
                "match_id": match_id,
                "pair_id": pair_id,
                "leg": leg,
                "seed": seed,
                "seats": seats,
                "first_player_index": None,
                "first_player": None,
                "duration_ms": (time.perf_counter_ns() - started) / 1_000_000,
                "outcome": {
                    "kind": outcome.kind,
                    "winner": None,
                    "winner_player_index": None,
                    "statuses": [None, None],
                    "rewards": [None, None],
                    "engine_error": None,
                },
                "agents": agents,
                "faults": load_faults,
            },
            {"champion": [], "challenger": []},
        )

    random.seed(seed)
    env = None
    engine_error = None
    try:
        env = make_environment(
            "cabt",
            configuration={"decks": [list(artifacts[role].deck) for role in seats]},
            debug=debug_engine,
        )
        env.run([monitors[role].kaggle_callback() for role in seats])
    except Exception as exc:
        engine_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        for monitor in monitors.values():
            monitor.drain_telemetry()

    if env is None:
        statuses: list[str | None] = [None, None]
        rewards: list[int | float | None] = [None, None]
    else:
        state = getattr(env, "state", ()) or ()
        statuses = [_status_name(_mapping_value(item, "status")) for item in state]
        rewards = [_finite_reward(_mapping_value(item, "reward")) for item in state]
        if len(statuses) != 2:
            statuses = [None, None]
        if len(rewards) != 2:
            rewards = [None, None]

    first_player_index = (
        None if env is None else _first_player_from_environment(env, monitors.values())
    )
    faults = [fault for monitor in monitors.values() for fault in monitor.faults]
    if engine_error is not None:
        faults.append(
            {"kind": "engine_failure", "phase": "environment", "message": engine_error}
        )
    if not faults:
        for seat, status in enumerate(statuses):
            kind = _status_fault_kind(status)
            if kind is not None:
                faults.append(
                    {
                        "kind": kind,
                        "role": seats[seat],
                        "seat": seat,
                        "phase": "engine_status",
                        "status": status,
                    }
                )
    outcome = classify_outcome(statuses, rewards, faults)
    winner = (
        seats[outcome.winner]
        if outcome.kind == "win" and outcome.winner is not None
        else None
    )
    agents = {
        role: monitors[role].record(seats.index(role), first_player_index)
        for role in ("champion", "challenger")
    }
    raw_latencies = {role: list(monitors[role].latencies_ns) for role in monitors}
    for item in loaded.values():
        item.close()

    return (
        {
            "match_id": match_id,
            "pair_id": pair_id,
            "leg": leg,
            "seed": seed,
            "seats": seats,
            "first_player_index": first_player_index,
            "first_player": None
            if first_player_index is None
            else seats[first_player_index],
            "duration_ms": (time.perf_counter_ns() - started) / 1_000_000,
            "outcome": {
                "kind": outcome.kind,
                "winner": winner,
                "winner_player_index": outcome.winner,
                "statuses": statuses,
                "rewards": rewards,
                "engine_error": engine_error,
            },
            "agents": agents,
            "faults": faults,
        },
        raw_latencies,
    )


def _process_start_method() -> str:
    methods = multiprocessing.get_all_start_methods()
    return "fork" if "fork" in methods else "spawn"


def _match_process_entry(
    connection: Any,
    artifacts: dict[str, AgentArtifact],
    arguments: dict[str, Any],
) -> None:
    try:
        result = run_match(load_environment_factory(), artifacts, **arguments)
        connection.send(("ok", result))
    except BaseException as exc:
        error = {
            "exception_type": type(exc).__name__,
            "message": str(exc)[:500],
            "traceback": traceback.format_exc(limit=20)[-4000:],
        }
        with contextlib.suppress(Exception):
            connection.send(("error", error))
    finally:
        connection.close()


def _process_failure_record(
    *,
    pair_id: int,
    leg: int,
    seed: int,
    kind: str,
    message: str,
    duration_ms: float,
) -> tuple[dict[str, Any], dict[str, list[int]]]:
    seats = ["champion", "challenger"] if leg == 0 else ["challenger", "champion"]
    fault = {"kind": kind, "phase": "match_process", "message": message[:500]}
    return (
        {
            "match_id": f"p{pair_id:04d}-leg{leg}",
            "pair_id": pair_id,
            "leg": leg,
            "seed": seed,
            "seats": seats,
            "first_player_index": None,
            "first_player": None,
            "duration_ms": duration_ms,
            "outcome": {
                "kind": kind,
                "winner": None,
                "winner_player_index": None,
                "statuses": [None, None],
                "rewards": [None, None],
                "engine_error": message if kind == "engine_failure" else None,
            },
            "agents": {
                role: _empty_agent_record(seats.index(role))
                for role in ("champion", "challenger")
            },
            "faults": [fault],
        },
        {"champion": [], "challenger": []},
    )


def _reap_process(process: Any, *, initial_wait_seconds: float) -> tuple[bool, bool]:
    """Join a child, then escalate from terminate to kill when necessary."""
    if process.pid is None:
        return (False, True)
    process.join(initial_wait_seconds)
    forced = process.is_alive()
    if process.is_alive():
        process.terminate()
        process.join(2)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(2)
    return (forced, not process.is_alive())


def run_match_isolated(
    artifacts: Mapping[str, AgentArtifact],
    *,
    pair_id: int,
    leg: int,
    action_timeout_ms: int,
    seed: int,
    game_timeout_seconds: float,
    debug_engine: bool = False,
    worker_target: ProcessWorker = _match_process_entry,
) -> tuple[dict[str, Any], dict[str, list[int]]]:
    """Run one match in a killable process with independent native state."""
    if game_timeout_seconds <= 0:
        raise ValueError("game_timeout_seconds must be greater than zero")
    context: Any = multiprocessing.get_context(_process_start_method())
    receiving, sending = context.Pipe(duplex=False)
    arguments = {
        "pair_id": pair_id,
        "leg": leg,
        "action_timeout_ms": action_timeout_ms,
        "seed": seed,
        "debug_engine": debug_engine,
    }
    process = context.Process(
        target=worker_target,
        args=(sending, dict(artifacts), arguments),
        name=f"cabt-p{pair_id:04d}-leg{leg}",
    )
    started = time.perf_counter()
    try:
        process.start()
        sending.close()
        deadline = started + game_timeout_seconds
        message: tuple[str, Any] | None = None
        timed_out = False
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                timed_out = True
                break
            if receiving.poll(min(0.1, remaining)):
                try:
                    message = receiving.recv()
                except EOFError:
                    message = None
                break
            if not process.is_alive():
                if receiving.poll(1):
                    try:
                        message = receiving.recv()
                    except EOFError:
                        message = None
                break

        if timed_out:
            _reap_process(process, initial_wait_seconds=0)
            duration_ms = (time.perf_counter() - started) * 1000
            return _process_failure_record(
                pair_id=pair_id,
                leg=leg,
                seed=seed,
                kind="process_timeout",
                message=f"Match exceeded {game_timeout_seconds:g} seconds and was terminated.",
                duration_ms=duration_ms,
            )

        forced, reaped = _reap_process(process, initial_wait_seconds=2)
        if forced or not reaped:
            detail = "required forced termination" if reaped else "could not be reaped"
            return _process_failure_record(
                pair_id=pair_id,
                leg=leg,
                seed=seed,
                kind="engine_failure",
                message=f"Match process returned a result but {detail}.",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        if message is not None:
            status, payload = message
            if status == "ok":
                return payload
            error_message = (
                f"{payload.get('exception_type', 'Exception')}: "
                f"{payload.get('message', 'match worker failed')}"
            )
        else:
            error_message = (
                f"Match process exited with code {process.exitcode} without a result."
            )
        return _process_failure_record(
            pair_id=pair_id,
            leg=leg,
            seed=seed,
            kind="engine_failure",
            message=error_message,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:
        _reap_process(process, initial_wait_seconds=0)
        return _process_failure_record(
            pair_id=pair_id,
            leg=leg,
            seed=seed,
            kind="engine_failure",
            message=f"{type(exc).__name__}: {str(exc)[:500]}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    finally:
        receiving.close()
        with contextlib.suppress(OSError):
            sending.close()


def load_environment_factory() -> EnvironmentFactory:
    """Import Kaggle quietly while preserving diagnostics if the import fails."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            from kaggle_environments import make  # type: ignore[import-not-found]
    except Exception:
        sys.stdout.write(stdout.getvalue())
        sys.stderr.write(stderr.getvalue())
        raise
    return make


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def run_arena(
    make_environment: EnvironmentFactory,
    champion: AgentArtifact,
    challenger: AgentArtifact,
    *,
    pairs: int,
    matchup: str,
    action_timeout_ms: int,
    seed: int,
    root: Path,
    debug_engine: bool = False,
    isolate_matches: bool = False,
    game_timeout_seconds: float = 900,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run all paired legs and return a strict-JSON-compatible report."""
    if pairs <= 0:
        raise ValueError("pairs must be greater than zero")
    if action_timeout_ms < 0:
        raise ValueError("action_timeout_ms must be zero or greater")
    if isolate_matches and game_timeout_seconds <= 0:
        raise ValueError("game_timeout_seconds must be greater than zero")
    artifacts = {"champion": champion, "challenger": challenger}
    matches: list[dict[str, Any]] = []
    raw_latencies: dict[str, list[int]] = {"champion": [], "challenger": []}
    for pair_id in range(pairs):
        for leg in (0, 1):
            game_seed = seed + pair_id * 2 + leg
            match_arguments: dict[str, Any] = {
                "pair_id": pair_id,
                "leg": leg,
                "action_timeout_ms": action_timeout_ms,
                "seed": game_seed,
                "debug_engine": debug_engine,
            }
            if isolate_matches:
                record, latency = run_match_isolated(
                    artifacts,
                    game_timeout_seconds=game_timeout_seconds,
                    **match_arguments,
                )
            else:
                record, latency = run_match(
                    make_environment,
                    artifacts,
                    **match_arguments,
                )
            matches.append(record)
            for role in raw_latencies:
                raw_latencies[role].extend(latency[role])
            if progress is not None:
                progress(record)

    def summarize_role(role: str) -> dict[str, Any]:
        role_input = [
            {
                "statuses": match["outcome"]["statuses"],
                "rewards": match["outcome"]["rewards"],
                "faults": match["faults"],
                "subject_index": match["seats"].index(role),
            }
            for match in matches
        ]
        return summarize_matches(role_input, latencies_ns=raw_latencies[role])

    challenger_summary = summarize_role("challenger")
    champion_summary = summarize_role("champion")
    summary = dict(challenger_summary)
    summary["perspective"] = "challenger"
    summary["perspectives"] = {
        "challenger": challenger_summary,
        "champion": champion_summary,
    }
    summary["agent_latency"] = {
        role: summarize_latency(values) for role, values in raw_latencies.items()
    }
    summary["games_requested"] = pairs * 2
    summary["games_recorded"] = len(matches)
    summary["first_player_games"] = {
        role: sum(1 for match in matches if match["first_player"] == role)
        for role in ("champion", "challenger")
    }
    fault_count = sum(summary["faults"].values())
    wilson_low = summary["wilson_95"]["low"]
    summary["gate"] = {
        "fault_free": fault_count == 0,
        "screening_min_50_games": summary["normal_games"] >= 50,
        "promotion_min_200_games": summary["normal_games"] >= 200,
        "wilson_lower_gt_0_5": wilson_low is not None and wilson_low > 0.5,
    }
    summary["gate"]["promotion_candidate"] = all(
        (
            summary["gate"]["fault_free"],
            summary["gate"]["promotion_min_200_games"],
            summary["gate"]["wilson_lower_gt_0_5"],
        )
    )

    return {
        "schema_version": 1,
        "run": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pairs_requested": pairs,
            "games_requested": pairs * 2,
            "matchup": matchup,
            "action_timeout_ms": action_timeout_ms,
            "game_timeout_seconds": game_timeout_seconds if isolate_matches else None,
            "match_isolation": isolate_matches,
            "process_start_method": _process_start_method()
            if isolate_matches
            else None,
            "seed": seed,
            "native_rng_seed_controlled": False,
            "python": platform.python_version(),
            "kaggle_environments": _package_version("kaggle-environments"),
            "artifacts": {
                "champion": champion.metadata(root),
                "challenger": challenger.metadata(root),
            },
        },
        "matches": matches,
        "summary": summary,
    }


def write_report(report: Mapping[str, Any], output_path: Path) -> None:
    """Atomically write a strict JSON report."""
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(output_path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()

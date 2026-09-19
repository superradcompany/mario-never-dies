import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mnd import bird_guest
from mnd.bird import Game
from mnd.games import GAMES
from mnd.orchestrator import Orchestrator, Settings, Slot
from mnd.replay import HOLDS, Replay, recorded_runs
from mnd.simulation import SimulatedBackend
from mnd.web import ControlRoom
from tests.test_bird import StubPicture
from tests.test_replay import row, write_timeline


class EndlessGuestTests(unittest.TestCase):
    def test_real_flight_passes_25_and_stops_only_on_stop_file(self):
        # This seed clears 28 pipes under the ordinary heuristic with no recovery.
        # Keep real physics/decisions; replace only rendering and host gate I/O.
        with tempfile.TemporaryDirectory() as directory:
            root, gates = Path(directory), []

            def release(state):
                gates.append(state)
                if state["score"] >= 26:
                    (root / "stop.json").write_text("{}")
                return {"timeline": "flight", "force": None}

            with (
                mock.patch.object(Game, "render", lambda self: StubPicture()),
                mock.patch.object(bird_guest.Gate, "wait", side_effect=release),
            ):
                bird_guest.run(
                    SimpleNamespace(
                        root=root,
                        target=0,
                        policy="heuristic",
                        frames_per_decision=6,
                        max_decisions=0,
                        seed=17,
                        checkpoint_frames=12,
                    )
                )
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["phase"], "halted")
            self.assertEqual(state["stage"], "endless")
            self.assertGreaterEqual(state["score"], 26)
            self.assertGreater(state["decision"], 150)
            self.assertTrue(any(g["score"] == 25 for g in gates))
            self.assertTrue(all(g["phase"] == "preparing_gate" for g in gates))

    def test_browser_uses_unlimited_bird_budgets_and_preserves_mario_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            room = ControlRoom(Path(directory), "unused")
            room.output = Path(directory) / "run"
            for game in ("bird", "mario"):
                with mock.patch("mnd.web.Orchestrator") as controller:
                    controller.return_value.run = mock.AsyncMock(return_value="stopped")
                    room.execute("simulation", game=game)
                    settings = controller.call_args.kwargs["settings"]
                if game == "bird":
                    self.assertEqual(settings.stages, ("endless",))
                    self.assertEqual(
                        (settings.run_timeout, settings.max_rewinds, settings.max_decisions),
                        (0, 0, 0),
                    )
                else:
                    self.assertEqual(settings.stages, GAMES["mario"]["stages"])
                    self.assertEqual(
                        (settings.run_timeout, settings.max_rewinds, settings.max_decisions),
                        (4800, 80, 2000),
                    )


class SavingBackend(SimulatedBackend):
    def __init__(self, fail_one=False):
        super().__init__()
        self.pulled = []
        self.failed_export = None
        self.fail_one = fail_one

    async def pull(self, name, timeline, destination):
        assert name in self.owned, "export must happen before deleting the VM"
        assert not self.vms[name]["paused"], "never export frozen checkpoint VMs"
        if self.fail_one and "race" in name and self.failed_export is None:
            self.failed_export = name
            raise OSError("injected recording export failure")
        await super().pull(name, timeline, destination)
        self.pulled.append(name)


class EndlessHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_budget_allows_long_runs_but_stop_still_works(self):
        with tempfile.TemporaryDirectory() as folder:
            c = Orchestrator(
                SimulatedBackend(),
                Path(folder) / "run",
                settings=Settings(game="bird", run_timeout=0, max_rewinds=0, multiverse=False),
            )
            c.started = time.monotonic() - 100_000
            c.check_deadline()
            c.rewinds = 10_000
            await c.backend.create("slot")
            c.slots = [Slot("slot", copy.deepcopy(c.backend.vms["slot"]))]
            await c.backend.create("dead")
            self.assertTrue(await c.rewind("dead", c.backend.vms["dead"]))
            self.assertEqual(c.rewinds, 10_001)
            c.stop = lambda: True
            with self.assertRaises(asyncio.CancelledError):
                c.check_deadline()
            await c.backend.cleanup()

    async def test_stop_exports_the_trunk_or_all_racing_children_before_cleanup(self):
        for where in ("trunk", "race", "paused"):
            with self.subTest(where=where), tempfile.TemporaryDirectory() as folder:
                backend = SavingBackend()
                c = Orchestrator(
                    backend,
                    Path(folder) / "run",
                    settings=Settings(poll_seconds=0, run_timeout=0),
                )
                c.stop = lambda c=c, where=where: (
                    c.paused
                    if where == "paused"
                    else any(
                        e["type"] == ("multiverse" if where == "race" else "checkpoint")
                        for e in c.events
                    )
                )
                if where == "paused":
                    c.commands = lambda: {"type": "pause"}
                self.assertEqual(await c.run(), "stopped")
                names = c.running_vms()
                self.assertEqual(len(names), 4 if where == "race" else 1)
                self.assertTrue(set(names) <= set(backend.pulled))
                self.assertTrue(
                    all((c.output / "timelines" / n / "simulation.json").is_file() for n in names)
                )
                self.assertFalse(backend.owned)
                result = json.loads((c.output / "result.json").read_text())
                self.assertEqual(result["recording_errors"], [])
                self.assertEqual(result["cleanup_errors"], [])

    async def test_one_export_failure_does_not_lose_other_recordings_or_leak_vms(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = SavingBackend(fail_one=True)
            c = Orchestrator(
                backend,
                Path(folder) / "run",
                settings=Settings(poll_seconds=0),
            )
            c.stop = lambda: any(e["type"] == "multiverse" for e in c.events)
            self.assertEqual(await c.run(), "stopped")
            candidates = set(c.running_vms())
            self.assertEqual(candidates - set(backend.pulled), {backend.failed_export})
            self.assertFalse(backend.owned)
            result = json.loads((c.output / "result.json").read_text())
            self.assertEqual(
                result["recording_errors"], [{"sandbox": backend.failed_export, "error": "OSError"}]
            )
            self.assertEqual(result["cleanup_errors"], [])


class StoppedRecordingTests(unittest.TestCase):
    def test_stopped_recording_is_listed_and_plays_the_final_trunk_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "abcdef012345"
            source.mkdir()
            write_timeline(source, "flight", [row(0, 0, 0), row(1, 8, 32)], range(17))
            events = [
                {
                    "type": "created",
                    "game": "bird",
                    "stage": "endless",
                    "sandbox": "flight",
                    "t": 1,
                    "elapsed": 0,
                },
                {"type": "stopped", "stage": "endless", "t": 4, "elapsed": 3},
            ]
            (source / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            (source / "result.json").write_text(
                json.dumps({"status": "stopped", "simulated": False})
            )
            listed = recorded_runs(Path(folder))
            self.assertEqual([(r["id"], r["status"]) for r in listed], [(source.name, "stopped")])
            replay = Replay(source, Path(folder) / "replay")
            self.assertAlmostEqual(replay.end, HOLDS["created"] + 16 / 30, places=3)
            self.assertEqual(replay.timelines["flight"].frame_at(replay.end), 16)
            self.assertEqual(replay.view_at(replay.end)["status"], "stopped")

    def test_stopped_race_plays_candidates_in_parallel_to_their_last_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "abcdef012345"
            source.mkdir()
            write_timeline(source, "flight", [row(0, 0, 0)], range(9))
            write_timeline(source, "a", [row(0, 0, 0)], range(9))
            write_timeline(source, "b", [row(0, 0, 0), row(1, 8, 32)], range(17))
            events = [
                {"type": "created", "game": "bird", "sandbox": "flight"},
                {
                    "type": "checkpoint",
                    "sandbox": "flight",
                    "child": "slot",
                    "frame": 0,
                    "x_pos": 0,
                },
                {"type": "death", "sandbox": "flight", "frame": 8, "x_pos": 32},
                {"type": "multiverse", "parent": "slot", "children": ["a", "b"]},
                {"type": "stopped"},
            ]
            (source / "events.jsonl").write_text(
                "".join(
                    json.dumps({**e, "stage": "endless", "t": i, "elapsed": i}) + "\n"
                    for i, e in enumerate(events)
                )
            )
            replay = Replay(source, Path(folder) / "replay")
            self.assertEqual(replay.timelines["a"].frame_at(replay.end), 8)
            self.assertEqual(replay.timelines["b"].frame_at(replay.end), 16)
            race = next(e for e in replay.events if e["type"] == "multiverse")
            self.assertAlmostEqual(
                replay.end - race["elapsed"], HOLDS["multiverse"] + 16 / 30, places=3
            )

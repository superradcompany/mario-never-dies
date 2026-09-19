"""Recovery budgets and a regression flight reconstructed from recorded Jev inputs."""

import copy
import tempfile
import unittest
from pathlib import Path

from mnd.bird import FRAMES_PER_DECISION, Game
from mnd.orchestrator import Orchestrator, Settings, Slot
from mnd.recovery import consume_sequence, sequence_chunk
from mnd.simulation import SimulatedBackend
from tests.test_bird import follow

# Seed 61854's recorded flight hit pipe six at frame 255. These are actual controller
# inputs, not an invented course or a production policy; replay them to test the bad saves.
RECORDED_FLAPS = {18, 24, 30, 60, 72, 90, 96, 108, 126, 144, 174, 180, 204, 210, 222, 234}


def recorded_copies():
    game, copies = Game(seed=61854), {}
    while not game.dead:
        if game.frame % 12 == 0:
            copies[game.frame] = copy.deepcopy(game)
        game.step(game.frame in RECORDED_FLAPS)
    return game, copies


class FlightBackend(SimulatedBackend):
    """Real game physics through the host protocol; outlook-following pilot, no API/VM."""

    def __init__(self, copies):
        super().__init__()
        self.games = copies

    async def branch(self, source, child):
        await super().branch(source, child)
        self.games[child] = copy.deepcopy(self.games[source])

    async def state(self, name):
        state, game = self.vms[name], self.games[name]
        if state["phase"] == "playing":
            plan = state["force_pending"]
            chunk = sequence_chunk(plan, {"player": {"x": game.x_pos}}, FRAMES_PER_DECISION)
            action, frames = chunk or (
                follow(game.observe(plan["aim"])["outlook"]),
                FRAMES_PER_DECISION,
            )
            score = game.score
            for offset in range(frames):
                alive = game.step(action == "flap" and offset == 0)
                if chunk:
                    consume_sequence(plan, 1)
                if not alive:
                    break
            if game.dead:
                while game.fall():
                    pass
                state["phase"] = "dead"
            elif game.score != score:
                state.update(phase="gate", gate=f"{name}:{game.frame}", milestone=True)
            state.update(
                frame=game.frame, flight_frame=game.flown, x_pos=game.x_pos, score=game.score
            )
        return copy.deepcopy(state)


class BirdRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def controller(self, folder, backend=None, **settings):
        backend = backend or SimulatedBackend()
        controller = Orchestrator(
            backend,
            Path(folder) / "run",
            settings=Settings(game="bird", stages=("25",), poll_seconds=0, **settings),
        )
        return controller

    async def test_recent_copies_do_not_evict_every_older_anchor(self):
        for limit in (1, 2, 3, 12):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as folder:
                c = await self.controller(folder, max_slots=limit)
                await c.backend.create("trunk")
                for frame in range(0, 1201, 12):
                    c.backend.vms["trunk"].update(phase="gate", gate=str(frame), frame=frame)
                    await c.checkpoint("trunk", copy.deepcopy(c.backend.vms["trunk"]))
                    self.assertLessEqual(len(c.slots), limit)
                    self.assertEqual(len(c.backend.owned), len(c.slots) + 1)
                frames = [slot.state["frame"] for slot in c.slots]
                self.assertEqual(frames[-1], 1200)
                if limit > 1:
                    self.assertEqual(frames[0], 0)
                if limit == 12:
                    self.assertEqual(frames[-8:], list(range(1116, 1201, 12)))
                    self.assertEqual(len(frames[:-8]), 4)
                    self.assertLess(frames[2], 900, "older copies span more than the recent window")
                await c.backend.cleanup()

    async def test_rapid_failures_skip_retries_and_double_the_lookback(self):
        with tempfile.TemporaryDirectory() as folder:
            c = await self.controller(folder, races_per_checkpoint=3)
            for frame in range(0, 253, 12):
                name = f"slot{frame}"
                await c.backend.create(name)
                c.backend.vms[name].update(frame=frame, x_pos=frame * 4)
                c.slots.append(Slot(name, copy.deepcopy(c.backend.vms[name])))
            await c.backend.create("dead")
            attempts = []

            async def race(target, death_x, **kwargs):
                attempts.append(target.state["frame"])
                c.rapid_race_failure = True
                return "winner" if len(attempts) == 4 else None

            c.race = race
            await c.rewind("dead", {"frame": 270, "flight_frame": 255, "x_pos": 1020, "score": 5})
            self.assertEqual(attempts, [252, 240, 228, 204])
            self.assertEqual(c.slots[-1].state["frame"], 204)
            self.assertFalse(any(int(n[4:]) > 204 for n in c.backend.owned))
            await c.backend.cleanup()

    async def test_recorded_doomed_copy_backs_off_and_clears_the_failed_pipe(self):
        death, copies = recorded_copies()
        self.assertEqual((death.frame, death.x_pos, death.score), (255, 1020, 5))
        with tempfile.TemporaryDirectory() as folder:
            backend = FlightBackend({})
            c = await self.controller(folder, backend, races_per_checkpoint=3)
            for frame in (180, 204, 216, 228, 240, 252):
                name = f"slot{frame}"
                await backend.create(name)
                game = backend.games[name] = copies[frame]
                backend.vms[name].update(
                    frame=frame, flight_frame=frame, x_pos=game.x_pos, score=game.score
                )
                c.slots.append(Slot(name, copy.deepcopy(backend.vms[name])))
            await backend.create("dead")
            backend.games["dead"] = death
            try:
                winner = await c.rewind(
                    "dead", {"frame": 264, "flight_frame": 255, "x_pos": 1020, "score": 5}
                )
                races = [e for e in c.events if e["type"] == "multiverse"]
                self.assertEqual(sum(e["parent"] == "slot252" for e in races), 1)
                self.assertEqual(races[1]["parent"], "slot240")
                self.assertLessEqual(len(races), 5)
                self.assertGreaterEqual(c.states[winner]["score"], 6)
                self.assertEqual(c.states[winner]["phase"], "gate")
                rapid = next(e for e in c.events if e["type"] == "race_failed")
                self.assertTrue(rapid["rapid"], "fall animation must not hide an instant collision")
            finally:
                await backend.cleanup()

    async def test_only_four_actual_quick_deaths_mark_a_copy_doomed(self):
        class Outcomes(SimulatedBackend):
            def __init__(self, outcome):
                super().__init__()
                self.outcome = outcome

            async def branch_many(self, source, children):
                self.last = children[-1]
                if self.outcome == "startup_failed":
                    await super().branch_many(source, children[:-1])
                    return {self.last: "could not start"}
                return await super().branch_many(source, children)

            async def state(self, name):
                state = self.vms[name]
                if state["phase"] == "playing":
                    phase = self.outcome if name == self.last else "dead"
                    state.update(phase=phase, frame=330, flight_frame=255, x_pos=1020)
                    if self.outcome == "late" and name == self.last:
                        state.update(phase="dead", flight_frame=270)
                return copy.deepcopy(state)

        for outcome in ("dead", "error", "limit", "startup_failed", "late"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                backend = Outcomes(outcome)
                c = await self.controller(folder, backend)
                await backend.create("slot")
                backend.vms["slot"].update(frame=252, flight_frame=252, x_pos=1008)
                slot = Slot("slot", copy.deepcopy(backend.vms["slot"]))
                try:
                    self.assertIsNone(await c.race(slot, 1020, death_frame=255, death_score=5))
                    self.assertEqual(c.rapid_race_failure, outcome == "dead")
                finally:
                    await backend.cleanup()

    async def test_distance_and_older_pipe_clear_cannot_win(self):
        class Passing(SimulatedBackend):
            async def state(self, name):
                state = self.vms[name]
                if state["phase"] == "playing":
                    state["poll"] = state.get("poll", 0) + 1
                    # Already beyond death+64 and still only the fifth pipe passed.
                    state.update(x_pos=1100, score=5, frame=275)
                    state["force_pending"]["status"] = "complete"
                    if state["poll"] == 2:
                        state.update(phase="dying", score=6)
                elif state["phase"] == "dying":
                    state.update(phase="dead")
                return copy.deepcopy(state)

        with tempfile.TemporaryDirectory() as folder:
            c = await self.controller(folder, Passing())
            await c.backend.create("slot")
            slot = Slot("slot", copy.deepcopy(c.backend.vms["slot"]))
            try:
                self.assertIsNone(await c.race(slot, 1020, death_frame=255, death_score=5))
                self.assertFalse(any(e["type"] == "promote" for e in c.events))
            finally:
                await c.backend.cleanup()

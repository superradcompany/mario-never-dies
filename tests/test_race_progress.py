import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mnd.orchestrator import Orchestrator, Settings, Slot
from mnd.recovery import consume_sequence, sequence_chunk
from mnd.simulation import SimulatedBackend


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    time = perf_counter = monotonic


class SlowCandidates(SimulatedBackend):
    """Advance gameplay separately from wall time; no emulator/API timing claims."""

    def __init__(self, clock, mode="progress"):
        super().__init__()
        self.clock, self.mode = clock, mode

    async def state(self, name):
        state = self.vms[name]
        if state["phase"] == "playing" and "race" in name:
            self.clock.now += 1
            state["frame"] += 8
            plan = state["force_pending"]
            if self.mode == "approach_stall":
                pass  # Frames advance, but x never reaches the experiment.
            elif self.mode in {"missed_death", "aborted_death"}:
                state.update(phase="dead", x_pos=plan["x_min"])
                plan["status"] = self.mode.removesuffix("_death")
                if plan["status"] == "aborted":
                    plan["remaining"] = plan["steps"][0]["frames"]
            elif self.mode == "trial_stall":
                state["x_pos"] = plan["x_min"]
                plan["status"] = "complete"
            elif self.mode == "earlier_death":
                # Captured failure: copies from x1442 died at x1646 while
                # still waiting for variations scheduled near x2153–2249.
                if plan["x_min"] > 1700:
                    state.update(phase="dead", x_pos=1646, frame=1132)
                else:
                    state.update(x_pos=1726, frame=1200)
                    plan.update(status="complete", step_index=len(plan["steps"]))
            else:
                state["x_pos"] += 16
                if chunk := sequence_chunk(plan, {"player": {"x": state["x_pos"]}}, 8):
                    consume_sequence(plan, chunk[1])
            if state["phase"] == "playing" and state["frame"] % 128 == 0:
                state.update(phase="gate", gate=f"{name}:{state['frame']}")
        return copy.deepcopy(state)


class RaceProgressTests(unittest.IsolatedAsyncioTestCase):
    async def setup_race(self, directory, clock, mode="progress", **settings):
        backend = SlowCandidates(clock, mode)
        controller = Orchestrator(
            backend,
            Path(directory) / "run",
            settings=Settings(poll_seconds=0, run_timeout=2000, **settings),
        )
        await backend.create("source")
        slot = Slot("source", await backend.state("source"))
        return backend, controller, slot

    async def test_long_approach_is_not_killed_before_variations_can_begin(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("mnd.orchestrator.time", Clock()) as clock,
        ):
            backend, controller, slot = await self.setup_race(directory, clock)
            try:
                winner = await controller.race(slot, 2313, death_frame=1467)
                self.assertIsNotNone(winner)
                starts = [e for e in controller.events if e["type"] == "experiment_started"]
                self.assertEqual(len(starts), 4)
                self.assertTrue(all(e["elapsed"] > 45 for e in starts))
                self.assertGreater(controller.states[winner]["x_pos"], 2313 + 64)
                self.assertEqual(controller.events[-1]["type"], "promote")
            finally:
                await backend.cleanup()

    async def test_nonprogressing_approach_has_a_game_frame_limit(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("mnd.orchestrator.time", Clock()) as clock,
        ):
            backend, controller, slot = await self.setup_race(
                directory, clock, "approach_stall", checkpoint_frames=16
            )
            try:
                self.assertIsNone(await controller.race(slot, 2313, death_frame=100))
                results = [e for e in controller.events if e["type"] == "experiment_result"]
                self.assertEqual(len(results), 4)
                self.assertTrue(all(e["outcome"] == "approach_exhausted" for e in results))
                self.assertTrue(all(not e["trial_started"] for e in results))
                self.assertTrue(all(e["frame"] <= 124 for e in results))
                self.assertEqual(controller.experience, [])
            finally:
                await backend.cleanup()

    async def test_started_trials_still_have_the_wall_clock_limit(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("mnd.orchestrator.time", Clock()) as clock,
        ):
            backend, controller, slot = await self.setup_race(directory, clock, "trial_stall")
            try:
                self.assertIsNone(await controller.race(slot, 230, death_frame=200))
                results = [e for e in controller.events if e["type"] == "experiment_result"]
                self.assertTrue(
                    all(e["outcome"] == "timed_out" and e["trial_started"] for e in results)
                )
                self.assertLess(clock.now, 60)
                self.assertEqual(controller.experience, [])  # Timeout is not death evidence.
            finally:
                await backend.cleanup()

    async def test_pausing_does_not_consume_trial_time(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("mnd.orchestrator.time", Clock()) as clock,
        ):
            backend, controller, slot = await self.setup_race(directory, clock, race_timeout=90)
            pauses = []

            async def pause_once():
                if not pauses and any(e["type"] == "experiment_started" for e in controller.events):
                    pauses.append(120)
                    clock.now += 120
                    controller.paused_seconds += 120
                    return {"paused_for": 120}

            controller.service_commands = pause_once
            try:
                self.assertIsNotNone(await controller.race(slot, 230, death_frame=200))
                self.assertEqual(pauses, [120])
                self.assertFalse(any(e.get("outcome") == "timed_out" for e in controller.events))
            finally:
                await backend.cleanup()

    async def test_earlier_death_retargets_recovery_and_does_not_blame_untried_moves(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("mnd.orchestrator.time", Clock()) as clock,
        ):
            backend, controller, slot = await self.setup_race(directory, clock, "earlier_death")
            backend.vms["source"].update(x_pos=1442, frame=1062)
            slot.state = await backend.state("source")
            controller.slots = [slot]
            await backend.create("failed")
            failed = dict(slot.state, phase="dead", x_pos=2313, frame=1467)
            try:
                winner = await controller.rewind("failed", failed)
                retarget = next(e for e in controller.events if e["type"] == "recovery_retarget")
                self.assertEqual((retarget["from_x"], retarget["x_pos"]), (2313, 1646))
                races = [e for e in controller.events if e["type"] == "multiverse"]
                self.assertEqual([e["hazard_x"] for e in races], [2313, 1646])
                self.assertGreater(controller.states[winner]["x_pos"], 1646 + 64)
                self.assertTrue(all(e["outcome"] == "survived" for e in controller.experience))
            finally:
                await backend.cleanup()

    async def test_overall_run_deadline_and_stop_still_bound_an_approach(self):
        for stop in (False, True):
            with (
                self.subTest(stop=stop),
                tempfile.TemporaryDirectory() as directory,
                patch("mnd.orchestrator.time", Clock()) as clock,
            ):
                backend, controller, slot = await self.setup_race(
                    directory, clock, "approach_stall"
                )
                controller.settings.run_timeout = 5
                controller.stop = lambda stop=stop: stop
                try:
                    with self.assertRaises(asyncio.CancelledError if stop else TimeoutError):
                        await controller.race(slot, 2313, death_frame=1467)
                    self.assertLessEqual(clock.now, 6)
                finally:
                    await backend.cleanup()

    async def test_unexecuted_missed_or_aborted_sequences_do_not_become_death_evidence(self):
        for mode in ("missed_death", "aborted_death"):
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as directory,
                patch("mnd.orchestrator.time", Clock()) as clock,
            ):
                backend, controller, slot = await self.setup_race(directory, clock, mode)
                try:
                    self.assertIsNone(await controller.race(slot, 230, death_frame=200))
                    results = [e for e in controller.events if e["type"] == "experiment_result"]
                    self.assertTrue(all(e["sequence_frames"] == 0 for e in results))
                    self.assertEqual(controller.experience, [])
                finally:
                    await backend.cleanup()

import copy
import json
import tempfile
import unittest
from pathlib import Path

from mnd.orchestrator import Orchestrator, Settings, Slot
from mnd.recovery import (
    consume_sequence,
    experiments,
    interrupt_sequence,
    sequence_chunk,
    validate_sequence,
)
from mnd.simulation import SimulatedBackend


class SequenceTests(unittest.TestCase):
    def test_variants_are_bounded_unique_and_diverse_even_when_start_is_clamped(self):
        for checkpoint in (40, 764, 900):
            plans = experiments(checkpoint, 906)
            self.assertEqual(len(plans), len({p["experiment_id"] for p in plans}))
            for plan in plans:
                validate_sequence(plan)
                self.assertGreaterEqual(plan["x_min"], checkpoint)
            self.assertEqual(len({p["steps"][-2]["frames"] for p in plans[:4]}), 4)

    def test_sequence_survives_serialization_and_finishes_exactly_at_its_frame_budget(self):
        plan = experiments(764, 906)[0]
        self.assertIsNone(sequence_chunk(plan, {"player": {"x": 764}}, 8))
        consumed = 0
        while chunk := sequence_chunk(plan, {"player": {"x": 800}}, 8):
            for _ in range(chunk[1]):
                consume_sequence(plan, 1)
                consumed += 1
            plan = json.loads(json.dumps(plan))
        self.assertEqual(consumed, sum(s["frames"] for s in plan["steps"]))
        self.assertEqual(plan["status"], "complete")

    def test_danger_interrupts_a_non_jump_segment_and_returns_control(self):
        plan = experiments(764, 906)[3]
        state = {"player": {"x": 860}, "hazard": {"estimated_contact_frames": 1}}
        self.assertIsNotNone(sequence_chunk(plan, state, 8))
        self.assertTrue(interrupt_sequence(plan, state, "left"))
        self.assertIsNone(sequence_chunk(plan, state, 8))

    def test_invalid_or_unbounded_sequences_are_rejected(self):
        plan = copy.deepcopy(experiments(764, 906)[0])
        plan["steps"][0]["frames"] = 1000
        with self.assertRaises(ValueError):
            validate_sequence(plan)


class ExperimentMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_races_never_repeat_an_experiment_from_the_same_checkpoint(self):
        class DeadCandidates(SimulatedBackend):
            async def state(self, name):
                state = await super().state(name)
                if "race" in name:
                    state["phase"] = "dead"
                    self.vms[name]["phase"] = "dead"
                return state

        with tempfile.TemporaryDirectory() as directory:
            backend = DeadCandidates()
            controller = Orchestrator(
                backend, Path(directory) / "run", settings=Settings(poll_seconds=0.001)
            )
            await backend.create("source")
            slot = Slot("source", await backend.state("source"))
            try:
                self.assertIsNone(await controller.race(slot, 230))
                self.assertIsNone(await controller.race(slot, 230))
                results = [e for e in controller.events if e["type"] == "experiment_result"]
                self.assertEqual(len(results), 8)
                self.assertEqual(len({e["experiment_id"] for e in results}), 8)
                self.assertTrue(all(e["outcome"] == "dead" for e in results))
            finally:
                await backend.cleanup()

    async def test_equal_poll_survivors_are_ranked_by_rewards_not_child_order(self):
        class Rewarded(SimulatedBackend):
            async def state(self, name):
                state = await super().state(name)
                if "race" in name:
                    plan = state["force_pending"]
                    plan.update(status="complete", step_index=len(plan["steps"]))
                    state.update(
                        phase="playing",
                        x_pos=500,
                        coins=int(name.rsplit("race", 1)[1]),
                        score=200,
                    )
                return state

        with tempfile.TemporaryDirectory() as directory:
            backend = Rewarded()
            controller = Orchestrator(backend, Path(directory) / "run")
            await backend.create("source")
            slot = Slot("source", await backend.state("source"))
            try:
                winner = await controller.race(slot, 230)
                event = next(e for e in controller.events if e["type"] == "multiverse")
                self.assertEqual(winner, event["children"][-1])
                self.assertEqual(controller.experience[-1]["outcome"], "survived")
            finally:
                await backend.cleanup()

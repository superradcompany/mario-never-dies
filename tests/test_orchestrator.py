import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mnd.orchestrator import Orchestrator, Settings
from mnd.simulation import SimulatedBackend


class PartialFailureBackend(SimulatedBackend):
    async def branch_many(self, source, children):
        await super().branch_many(source, children[:3])
        return {children[3]: "injected child startup failure"}


class StalledBackend(SimulatedBackend):
    async def state(self, name):
        state = self.vms[name]
        return {**state, "phase": "playing"}


class CleanupFailureBackend(SimulatedBackend):
    async def cleanup(self):
        await super().cleanup()
        return [{"sandbox": "fixture", "error": "injected cleanup failure"}]


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, backend=None, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            backend = backend or SimulatedBackend()
            settings = Settings(poll_seconds=0.0001, stall_timeout=0.02, run_timeout=5)
            controller = Orchestrator(backend, Path(directory) / "run", settings=settings, **kwargs)
            status = await controller.run()
            result = json.loads((controller.output / "result.json").read_text())
            events = json.loads((controller.output / "events.json").read_text())
            journal = [
                json.loads(line)
                for line in (controller.output / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events, journal)
            self.assertEqual(backend.owned, set(), "Run leaked an owned VM")
            return status, result, events

    def test_published_history_keeps_ancestry_and_is_an_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots = []
            controller = Orchestrator(
                SimulatedBackend(), Path(directory) / "run", observer=snapshots.append
            )
            for index in range(120):
                controller.event("checkpoint", child=f"copy{index}")
            self.assertEqual(len(snapshots[-1]["events"]), 120)
            self.assertEqual(len(snapshots[0]["events"]), 1)
            self.assertEqual(snapshots[-1]["events"][0]["child"], "copy0")

    async def test_every_death_forks_four_ways_then_promotes_and_clears(self):
        status, result, events = await self.exercise()
        self.assertEqual(status, "complete")
        self.assertEqual(result["rewinds"], 1)
        kinds = [event["type"] for event in events]
        for required in ("checkpoint", "death", "multiverse", "promote", "clear"):
            self.assertIn(required, kinds)
        # No single retry from a checkpoint: the only recovery is the four-way fork.
        self.assertNotIn("rewind", kinds)
        self.assertLess(kinds.index("death"), kinds.index("multiverse"))
        race = next(event for event in events if event["type"] == "multiverse")
        self.assertEqual(len(race["children"]), 4)
        self.assertTrue(all(event["simulated"] for event in events))

    async def test_campaign_advances_in_order_and_isolates_stage_retries(self):
        class CampaignBackend(SimulatedBackend):
            def __init__(self):
                super().__init__()
                self.launched = []

            async def create(self, name, **kwargs):
                # Stage changes must remove both the old trunk and all its slots.
                if self.launched:
                    assert not self.owned
                await super().create(name, **kwargs)

            async def launch(self, name, **kwargs):
                self.launched.append(kwargs["stage"])

        with tempfile.TemporaryDirectory() as directory:
            backend = CampaignBackend()
            stages = ("1-1", "1-2", "1-3", "1-4")
            controller = Orchestrator(
                backend,
                Path(directory) / "run",
                settings=Settings(stages=stages, poll_seconds=0.0001, run_timeout=10),
            )
            self.assertEqual(await controller.run(), "complete")
            self.assertEqual(backend.launched, list(stages))
            self.assertEqual(controller.completed_stages, list(stages))
            self.assertEqual(backend.owned, set())
            clears = [e["stage"] for e in controller.events if e["type"] == "stage_clear"]
            self.assertEqual(clears, list(stages))
            # Every stage repeats the fixture's death and its race. Accumulated
            # hazard attempts from a previous stage would break this sequence.
            races = [e["stage"] for e in controller.events if e["type"] == "multiverse"]
            self.assertEqual(races, list(stages))
            completions = [e for e in controller.events if e["type"] == "clear"]
            self.assertEqual(len(completions), 1)
            self.assertEqual(completions[0]["stage"], "1-4")

    async def test_frame_zero_snapshot_gets_the_first_frame_seen(self):
        class Picturing(SimulatedBackend):
            async def frame(self, name, destination):
                state = self.vms[name]
                if state["frame"] == 0:
                    return  # like the guest: nothing saved before the first gate opens
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"PNG" + str(state["frame"]).encode())

        with tempfile.TemporaryDirectory() as directory:
            backend = Picturing()
            controller = Orchestrator(
                backend,
                Path(directory) / "run",
                settings=Settings(poll_seconds=0.0001, run_timeout=5),
            )
            self.assertEqual(await controller.run(), "complete")
            first = next(e for e in controller.events if e["type"] == "checkpoint")
            self.assertEqual(first["frame"], 0)
            picture = controller.output / "live" / f"{first['child']}.png"
            self.assertTrue(picture.is_file(), "frame-zero snapshot has a picture")
            self.assertEqual(picture.read_bytes(), b"PNG8", "the first frame its source showed")
            self.assertEqual(controller.unpictured, {})

    async def test_pause_freezes_every_running_vm_until_resume(self):
        commands = []
        seen = {}

        class Pausable(SimulatedBackend):
            async def state(self, name):
                state = await super().state(name)
                if not seen and state["frame"] >= 64:
                    seen["asked"] = True
                    commands.append({"type": "pause"})
                return state

            async def pause(self, name):
                await super().pause(name)
                if name.endswith("run1"):
                    # Once the trunk is frozen, let it sit a beat, then resume.
                    seen["paused_trunk"] = True
                    commands.append({"type": "resume"})

        backend = Pausable()
        status, result, events = await self.exercise(
            backend, commands=lambda: commands.pop(0) if commands else None
        )
        self.assertEqual(status, "complete")
        self.assertTrue(seen.get("paused_trunk"))
        kinds = [event["type"] for event in events]
        self.assertIn("paused", kinds)
        self.assertIn("resumed", kinds)
        self.assertLess(kinds.index("paused"), kinds.index("resumed"))
        resumed = next(event for event in events if event["type"] == "resumed")
        self.assertGreaterEqual(resumed["seconds"], 0)
        self.assertIn("clear", kinds)

    async def test_visibility_pause_preserves_every_vm_until_explicit_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = SimulatedBackend()
            controller = Orchestrator(
                backend, Path(directory) / "run", settings=Settings(poll_seconds=0.001)
            )
            await controller.start_stage({})
            child = controller.name("race")
            await backend.create(child)
            controller.roles[child] = "candidate"
            machines = set(backend.owned)
            clicks = [{"type": "pause", "reason": "connection_lost"}]
            controller.commands = lambda: clicks.pop(0) if clicks else None
            held = asyncio.create_task(controller.service_commands())
            try:
                await asyncio.sleep(0.02)
                self.assertFalse(held.done())
                self.assertTrue(controller.paused)
                self.assertEqual(controller.pause_reason, "connection_lost")
                self.assertEqual(backend.owned, machines)
                self.assertTrue(all(backend.vms[name]["paused"] for name in machines))
                self.assertFalse(any(e["type"] == "stopped" for e in controller.events))
                clicks.append({"type": "resume"})
                await asyncio.wait_for(held, 1)
                self.assertFalse(controller.paused)
                self.assertEqual(backend.owned, machines)
                self.assertTrue(all(not backend.vms[name]["paused"] for name in machines))
            finally:
                if not held.done():
                    held.cancel()
                await asyncio.gather(held, return_exceptions=True)
                await backend.cleanup()

    async def test_manual_rewind_branches_the_chosen_checkpoint(self):
        commands = []

        class Watching(SimulatedBackend):
            async def state(self, name):
                state = await super().state(name)
                # Once two frozen copies exist, ask for the older one exactly once.
                paused = [n for n, v in self.vms.items() if v["paused"]]
                if not commands and len(paused) >= 2 and not getattr(self, "asked", False):
                    self.asked = True
                    commands.append({"type": "rewind", "slot": paused[0]})
                return state

        backend = Watching()
        status, result, events = await self.exercise(
            backend, commands=lambda: commands.pop(0) if commands else None
        )
        self.assertEqual(status, "complete")
        manual = [event for event in events if event["type"] == "rewind" and event.get("manual")]
        self.assertEqual(len(manual), 1)
        self.assertLessEqual(manual[0]["to_x"], manual[0]["from_x"])
        self.assertEqual([event for event in events if event["type"] == "rewind_refused"], [])

    async def test_a_campaign_waits_at_each_flag_until_told_to_go_on(self):
        with tempfile.TemporaryDirectory() as directory:
            polls = {"count": 0}

            def commands():
                # The browser says nothing for a while, then "next".
                polls["count"] += 1
                return {"type": "next", "stage": "1-1"} if polls["count"] % 40 == 0 else None

            views = []
            controller = Orchestrator(
                SimulatedBackend(),
                Path(directory) / "run",
                settings=Settings(
                    stages=("1-1", "1-2"), intermission=True, poll_seconds=0.0001, run_timeout=10
                ),
                commands=commands,
                observer=views.append,
            )
            self.assertEqual(await controller.run(), "complete")
            kinds = [event["type"] for event in controller.events]
            self.assertEqual(kinds.count("intermission"), 1, "the last world ends the run instead")
            held = kinds.index("intermission")
            self.assertEqual(kinds[held - 1], "stage_clear")
            self.assertEqual(kinds[held + 1 : held + 3], ["intermission_over", "stage_started"])
            waiting = [view["intermission"] for view in views if view["intermission"]]
            self.assertEqual(waiting[0], {"stage": "1-1", "next": "1-2"})
            self.assertIsNone(views[-1]["intermission"])

    async def test_stale_next_world_click_does_not_release_the_following_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Orchestrator(
                SimulatedBackend(),
                Path(directory) / "run",
                settings=Settings(poll_seconds=0.001),
            )
            await controller.start_stage({})
            clicks = [{"type": "next", "stage": "1-1"}]
            controller.commands = lambda: clicks.pop(0) if clicks else None
            held = asyncio.create_task(controller.hold_at_flag("1-2", "1-3"))
            try:
                await asyncio.sleep(0.02)
                self.assertFalse(held.done(), "an earlier world's click released the next one")
                clicks.append({"type": "next", "stage": "1-2"})
                await asyncio.wait_for(held, 1)
            finally:
                if not held.done():
                    held.cancel()
                await asyncio.gather(held, return_exceptions=True)
                await controller.backend.cleanup()

    async def test_stopping_at_a_flag_ends_the_run_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = SimulatedBackend()
            controller = Orchestrator(
                backend,
                Path(directory) / "run",
                settings=Settings(
                    stages=("1-1", "1-2"), intermission=True, poll_seconds=0.0001, run_timeout=10
                ),
                stop=lambda: bool(controller.intermission),
            )
            self.assertEqual(await controller.run(), "stopped")
            self.assertEqual(backend.owned, set(), "Run leaked an owned VM")

    async def test_partial_branch_failure_preserves_successful_children(self):
        status, _, events = await self.exercise(PartialFailureBackend())
        self.assertEqual(status, "complete")
        race = next(event for event in events if event["type"] == "multiverse")
        self.assertEqual(len(race["errors"]), 1)

    async def test_cancellation_persists_result_and_cleans_owned_vms(self):
        status, _, events = await self.exercise(stop=lambda: True)
        self.assertEqual(status, "stopped")
        self.assertEqual(events[-1]["type"], "stopped")

    async def test_stalled_guest_fails_with_bounded_watchdog(self):
        status, result, _ = await self.exercise(StalledBackend())
        self.assertEqual(status, "failed")
        self.assertIn("stopped advancing", result["error"])

    async def test_cleanup_failure_cannot_report_success(self):
        status, result, _ = await self.exercise(CleanupFailureBackend())
        self.assertEqual(status, "cleanup_failed")
        self.assertTrue(result["cleanup_errors"])


if __name__ == "__main__":
    unittest.main()

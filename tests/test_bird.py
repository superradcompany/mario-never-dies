import copy
import json
import tempfile
import threading
import time
import unittest
import unittest.mock
import uuid
from pathlib import Path
from types import SimpleNamespace

from mnd import bird, bird_guest
from mnd.bird import FRAMES_PER_DECISION, GROUND, PIPE_GAP, Game, experiments, heuristic
from mnd.games import GAMES, game_of
from mnd.orchestrator import Orchestrator, Settings
from mnd.recovery import consume_sequence, sequence_chunk
from mnd.simulation import SimulatedBackend

try:
    import PIL  # noqa: F401

    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def fly(seed, frames):
    game = Game(seed=seed)
    while not game.dead and game.frame < frames:
        game.step(game.frame % FRAMES_PER_DECISION == 0 and heuristic(game) == "flap")
    return game


class EngineTests(unittest.TestCase):
    def test_gravity_is_perma_death(self):
        game = Game(seed=1)
        while game.step(False):
            pass
        self.assertTrue(game.dead)
        self.assertLess(game.frame, 40)
        self.assertFalse(game.step(True), "a dead bird stays dead")

    def test_the_same_seed_is_the_same_sky(self):
        first, second, other = fly(9, 500), fly(9, 500), Game(seed=10)
        self.assertEqual(
            (first.frame, first.score, first.y), (second.frame, second.score, second.y)
        )
        self.assertNotEqual(
            [p.gap_top for p in Game(seed=9).pipes], [p.gap_top for p in other.pipes]
        )

    def test_every_gap_is_reachable_from_the_last(self):
        game = Game(seed=4)
        tops = []
        for _ in range(3000):
            game.step(False)
            game.dead, game.y, game.velocity = False, 200.0, 0.0  # a ghost: only the pipes matter
            tops.extend(pipe.gap_top for pipe in game.pipes)
        self.assertTrue(all(60 <= top <= GROUND - PIPE_GAP - 60 for top in tops))
        ordered = Game(seed=4)
        gaps = [pipe.gap_top for pipe in ordered.pipes]
        self.assertTrue(all(abs(a - b) <= 90 for a, b in zip(gaps, gaps[1:], strict=False)))

    def test_the_crash_adds_pictures_not_distance(self):
        game = Game(seed=1)
        while game.step(False):
            pass
        frame, flown = game.frame, game.x_pos
        while game.fall():
            pass
        self.assertGreater(game.frame, frame, "the fall is drawn frame by frame")
        self.assertEqual(game.x_pos, flown, "but the death is where the bird hit")

    def test_progress_and_score(self):
        game = fly(3, 400)
        self.assertEqual(game.x_pos, game.frame * bird.SCROLL)
        self.assertGreaterEqual(game.score, 5, "the stand-in controller clears pipes")

    def test_what_jev_sees(self):
        state = Game(seed=3).observe()
        self.assertEqual(set(state["outlook"]), {"flap", "glide"})
        self.assertTrue(any(view["survivable"] for view in state["outlook"].values()))
        self.assertEqual(len(state["gaps_ahead"]), 2)
        gap = state["gaps_ahead"][0]
        self.assertEqual(gap["gap_bottom"] - gap["gap_top"], PIPE_GAP)
        json.dumps(state)  # Jev receives JSON

    def test_a_forks_verses_are_valid_and_differ(self):
        plans = experiments(checkpoint_x=400, death_x=640)
        self.assertEqual(len(plans), 12)
        self.assertEqual(len({plan["experiment_id"] for plan in plans}), len(plans))
        for plan in plans:
            bird.validate_sequence(plan)
            self.assertEqual(plan["x_min"], 400, "a verse differs from its first frame")
            self.assertEqual([step["action"] for step in plan["steps"]], ["glide"])
        for race in (plans[0:4], plans[4:8], plans[8:12]):
            self.assertEqual(len({plan["aim"] for plan in race}), 4)
            self.assertEqual(len({plan["steps"][0]["frames"] for plan in race}), 4)
        self.assertNotEqual(
            experiments(400, 640)[0]["experiment_id"], experiments(256, 640)[0]["experiment_id"]
        )
        with self.assertRaises(ValueError):
            bird.validate_sequence({"steps": [{"action": "right_run", "frames": 4}]})
        with self.assertRaises(ValueError):
            bird.validate_sequence({"aim": 1.4, "steps": [{"action": "glide", "frames": 2}]})

    def test_a_verse_steers_for_its_own_line(self):
        game = Game(seed=3)
        high, low = game.observe(aim=0.2), game.observe(aim=0.9)
        self.assertLess(high["steer_for_y"], low["steer_for_y"])
        self.assertEqual(high["steer_for_y"], int(game.pipes[0].gap_top + PIPE_GAP * 0.2))

    def test_a_verse_glides_then_hands_back(self):
        plan = experiments(0, 96)[0]
        game, flown = Game(seed=5), []
        while True:
            chunk = sequence_chunk(plan, {"player": {"x": game.x_pos}}, FRAMES_PER_DECISION)
            if chunk is None:
                if plan["status"] in {"complete", "missed"}:
                    break
                game.step(False)
                continue
            action, frames = chunk
            for offset in range(frames):
                game.step(action == "flap" and offset == 0)
                consume_sequence(plan, 1)
            flown.append((action, frames))
        self.assertEqual(plan["status"], "complete")
        self.assertEqual(
            sum(frames for _, frames in flown), sum(s["frames"] for s in plan["steps"])
        )

    @unittest.skipUnless(HAS_PIL, "needs Pillow, which only the guest image carries")
    def test_the_picture(self):
        image = fly(5, 150).render()
        self.assertEqual(image.size, tuple(GAMES["bird"]["size"]))


def follow(view):
    """A pilot that reads its outlook the way Jev is told to."""
    flap, glide = view["flap"], view["glide"]
    if flap["survivable"] != glide["survivable"]:
        return "flap" if flap["survivable"] else "glide"
    if flap["survivable"]:
        return "flap" if flap["ends_from_aim"] < glide["ends_from_aim"] else "glide"
    return "flap" if flap["frames_until_crash"] > glide["frames_until_crash"] else "glide"


def fly_to_gate(game, plan, target):
    """The worker's loop without the VM: to the next cleared pipe, the target, or a crash."""
    gate_score = game.score
    while not game.dead and game.score < target:
        if game.score != gate_score and not (plan and plan["status"] == "active"):
            return "gate"
        chunk = sequence_chunk(plan, {"player": {"x": game.x_pos}}, FRAMES_PER_DECISION)
        aim = plan["aim"] if plan else bird.AIM
        action, frames = chunk or (follow(bird.outlook(game, aim=aim)), FRAMES_PER_DECISION)
        for offset in range(frames):
            alive = game.step(action == "flap" and offset == 0)
            if chunk:
                consume_sequence(plan, 1)
            if not alive:
                break
    return "dead" if game.dead else "clear"


def rehearse(seed, target=25):
    """A whole campaign on the real engine: a copy at every pipe, four verses per death."""
    game = Game(seed=seed)
    slots, deaths, races = [copy.deepcopy(game)], 0, 0
    while True:
        outcome = fly_to_gate(game, None, target)
        if outcome == "clear":
            return deaths, races
        if outcome == "gate":
            slots.append(copy.deepcopy(game))
            continue
        deaths += 1
        index = max(i for i, slot in enumerate(slots) if i == 0 or slot.x_pos <= game.x_pos - 96)
        plans, survivor = experiments(slots[index].x_pos, game.x_pos), None
        while survivor is None:
            if not plans:  # three races failed here: fall back one copy
                assert index, "every verse from the oldest copy died"
                index -= 1
                plans = experiments(slots[index].x_pos, game.x_pos)
            races += 1
            race, plans = plans[:4], plans[4:]
            for plan in race:
                trial = copy.deepcopy(slots[index])
                while (result := fly_to_gate(trial, plan, target)) == "gate":
                    if trial.x_pos > game.x_pos:
                        break
                if result != "dead":
                    survivor = max(survivor or trial, trial, key=lambda t: t.x_pos)
        game, slots = survivor, [*slots[: index + 1], copy.deepcopy(survivor)]


class RehearsalTests(unittest.TestCase):
    def test_the_bird_dies_and_a_verse_always_makes_it(self):
        # The cadence, the foresight and the verses are tuned together: enough deaths to
        # watch, and a fork that nearly always rescues at the first race.
        runs = [rehearse(seed) for seed in range(1, 25)]
        deaths, races = sum(d for d, _ in runs), sum(r for _, r in runs)
        self.assertGreaterEqual(deaths, 48, "about four deaths a run, or there is no show")
        self.assertLessEqual(races / deaths, 1.5, "a death is usually rescued by its first fork")


class StubPicture:
    def save(self, path, optimize=False):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nstub")


class GuestTests(unittest.TestCase):
    def test_the_worker_gates_at_every_pipe_and_clears(self):
        """The host side of the protocol, played by a thread: release every gate the worker
        opens, exactly as the controller does, and let the stand-in controller fly."""
        with (
            tempfile.TemporaryDirectory() as directory,
            unittest.mock.patch.object(Game, "render", lambda self: StubPicture()),
        ):
            root = Path(directory)
            args = SimpleNamespace(
                root=root, target=3, policy="heuristic", frames_per_decision=4,
                max_decisions=2000, seed=11,
            )  # fmt: skip
            worker = threading.Thread(target=bird_guest.run, args=(args,), daemon=True)
            worker.start()
            gates, deadline = [], time.monotonic() + 20
            while worker.is_alive() and time.monotonic() < deadline:
                try:
                    state = json.loads((root / "state.json").read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.005)
                    continue
                if state["phase"] == "gate" and state["gate"] not in gates:
                    gates.append(state["gate"])
                    command = {
                        "gate": state["gate"],
                        "timeline": "run1",
                        "avoid": [],
                        "force": None,
                    }
                    (root / "control.json").write_text(json.dumps(command))
                time.sleep(0.005)
            worker.join(timeout=5)
            final = json.loads((root / "state.json").read_text())
            self.assertEqual(final["phase"], "clear")
            self.assertEqual(final["score"], 3)
            self.assertEqual(final["game"], "bird")
            self.assertEqual(final["x_pos"], final["frame"] * bird.SCROLL)
            self.assertEqual(len(gates), 3, "a copy is offered at the start and after each pipe")
            rows = [
                json.loads(line)
                for line in (root / "timelines/run1/run.jsonl").read_text().splitlines()
            ]
            self.assertTrue(all(row["action"] in bird.ACTIONS for row in rows))
            self.assertTrue(list((root / "timelines/run1/frames").glob("*.png")))


class SeamTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_controller_runs_a_bird_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                poll_seconds=0.0001, stall_timeout=0.02, run_timeout=5,
                game="bird", stages=GAMES["bird"]["stages"], seed=42,
            )  # fmt: skip
            controller = Orchestrator(
                SimulatedBackend(), Path(directory) / "run", settings=settings
            )
            self.assertEqual(await controller.run(), "complete")
            created = next(event for event in controller.events if event["type"] == "created")
            self.assertEqual((created["game"], created["seed"]), ("bird", 42))
            self.assertEqual(game_of(controller.events), "bird")
            race = next(event for event in controller.events if event["type"] == "multiverse")
            labels = [plan["label"] for plan in race["experiments"].values()]
            self.assertTrue(all(label.startswith("aims") for label in labels), labels)

    async def test_a_cleared_pipe_wins_the_race_and_is_copied_there(self):
        class Pipes(SimulatedBackend):
            """A trial that flapped reaches a milestone gate just past the death."""

            async def state(self, name):
                state = await super().state(name)
                live = self.vms[name]
                past = live["escaped"] and 236 <= live["x_pos"] < 294
                if live["phase"] == "playing" and past and not live.get("gated"):
                    live.update(phase="gate", gate=uuid.uuid4().hex, milestone=True, gated=True)
                    live["checkpoint_seq"] += 1
                    return copy.deepcopy(live)
                return state

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                poll_seconds=0.0001, stall_timeout=0.02, run_timeout=5,
                game="bird", stages=GAMES["bird"]["stages"], seed=42,
            )  # fmt: skip
            controller = Orchestrator(Pipes(), Path(directory) / "run", settings=settings)
            self.assertEqual(await controller.run(), "complete")
            events = controller.events
            death = next(event for event in events if event["type"] == "death")
            promote = next(event for event in events if event["type"] == "promote")
            self.assertLessEqual(promote["x_pos"], death["x_pos"] + 64, "won at the gate")
            after = events[events.index(promote) + 1 :]
            copied = next(event for event in after if event["type"] == "checkpoint")
            self.assertEqual(copied["x_pos"], promote["x_pos"], "and is copied there")

    def test_old_recordings_are_marios(self):
        self.assertEqual(game_of([{"type": "created", "sandbox": "run1"}]), "mario")
        self.assertEqual(game_of([]), "mario")


if __name__ == "__main__":
    unittest.main()

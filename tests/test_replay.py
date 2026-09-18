import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from mnd.replay import FPS, HOLDS, Replay, recorded_runs

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def write_timeline(root: Path, name: str, rows: list[dict], frames: range):
    directory = root / "timelines" / name
    directory.mkdir(parents=True)
    with tarfile.open(directory / "timeline.tar", "w") as tar:

        def add(path, data):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add("state.json", json.dumps({"phase": "dead" if name == "run1" else "clear"}).encode())
        add(f"timelines/{name}/run.jsonl", "".join(json.dumps(row) + "\n" for row in rows).encode())
        for frame in frames:
            add(f"timelines/{name}/frames/{frame:08d}.png", PNG + bytes([frame % 251]))


def row(decision, frame, x, action="right_run"):
    return {
        "decision": decision,
        "frame": frame,
        "end_frame": frame + 8,
        "x_pos": x,
        "y_pos": 79,
        "action": action,
        "proposed_action": action,
        "forced": False,
        "probabilities": {action: 0.9},
        "confidence": 0.9,
        "latency_ms": 1000.0,
        "input_tokens": 100,
    }


def make_recording(root: Path):
    events = [
        {"type": "created", "t": 1.0, "elapsed": 0.0, "sandbox": "run1"},
        {
            "type": "checkpoint",
            "t": 1.5,
            "elapsed": 0.5,
            "sandbox": "run1",
            "child": "slot2",
            "frame": 0,
            "x_pos": 40,
            "branch_ms": 150.0,
        },
        {
            "type": "checkpoint",
            "t": 11.0,
            "elapsed": 10.0,
            "sandbox": "run1",
            "child": "slot3",
            "frame": 80,
            "x_pos": 300,
            "branch_ms": 160.0,
        },
        {
            "type": "death",
            "t": 21.0,
            "elapsed": 20.0,
            "sandbox": "run1",
            "frame": 160,
            "x_pos": 600,
        },
        {
            "type": "rewind",
            "t": 21.2,
            "elapsed": 20.2,
            "sandbox": "run4",
            "parent": "slot3",
            "from_x": 600,
            "to_x": 300,
            "frame": 80,
            "branch_ms": 130.0,
        },
        {"type": "clear", "t": 41.2, "elapsed": 40.2, "sandbox": "run4", "frame": 240},
    ]
    root.mkdir(parents=True)
    (root / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    (root / "result.json").write_text(
        json.dumps({"status": "complete", "simulated": False, "rewinds": 1})
    )
    (root / "live").mkdir()
    (root / "live" / "slot3.png").write_bytes(PNG + b"slot3")
    write_timeline(root, "run1", [row(i, i * 8, 40 + i * 28) for i in range(20)], range(0, 161, 2))
    write_timeline(
        root, "run4", [row(i, 80 + i * 8, 300 + i * 30) for i in range(20)], range(80, 241, 2)
    )


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "runs"
        self.source = self.root / "abc123abc123"
        make_recording(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_view_keeps_ancestry_past_one_hundred_events(self):
        path = self.source / "events.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events[2:2] = [
            {"type": "retry_hypothesis", "t": 2.0 + i / 100, "elapsed": 1 + i / 100}
            for i in range(110)
        ]
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        replay = Replay(self.source, self.root / "replay-test")
        view = replay.view_at(replay.end)
        self.assertGreater(len(view["events"]), 100)
        self.assertEqual(view["events"][0]["type"], "created")

    def test_listing_only_reports_real_recordings_with_timelines(self):
        (self.root / "simulated0000").mkdir()
        (self.root / "simulated0000" / "result.json").write_text(
            '{"status":"complete","simulated":true}'
        )
        runs = recorded_runs(self.root)
        self.assertEqual([run["id"] for run in runs], ["abc123abc123"])
        self.assertEqual(runs[0]["rewinds"], 1)
        self.assertEqual(runs[0]["seconds"], 40)

    def test_events_are_retimed_onto_the_game_clock(self):
        replay = Replay(self.source, Path(self.temp.name) / "out")
        kinds = {}
        for event in replay.events:
            kinds.setdefault(event["type"], event)
        # run1: frames 0..160 at FPS, plus the holds after created and death.
        self.assertEqual(kinds["created"]["elapsed"], 0.0)
        self.assertAlmostEqual(kinds["checkpoint"]["elapsed"], HOLDS["created"], places=2)
        death = HOLDS["created"] + 160 / FPS
        self.assertAlmostEqual(kinds["death"]["elapsed"], death, places=2)
        self.assertAlmostEqual(kinds["rewind"]["elapsed"], death + HOLDS["death"], places=2)
        clear = death + HOLDS["death"] + HOLDS["rewind"] + 160 / FPS
        self.assertAlmostEqual(kinds["clear"]["elapsed"], clear, places=2)
        self.assertAlmostEqual(replay.end, clear + HOLDS["clear"], places=2)
        self.assertEqual(kinds["death"]["wall_elapsed"], 20.0)

    def test_roles_and_frames_follow_the_recorded_events(self):
        replay = Replay(self.source, Path(self.temp.name) / "out")
        early = replay.view_at(HOLDS["created"] + 1.0)
        self.assertEqual(early["trunk"], "run1")
        self.assertEqual(
            {item["name"]: item["role"] for item in early["timelines"]},
            {"run1": "trunk", "slot2": "checkpoint"},
        )
        run1 = next(item for item in early["timelines"] if item["name"] == "run1")
        self.assertEqual(run1["frame"], FPS, "one second of game time is FPS frames")
        self.assertEqual(run1["action"], "right_run")
        death = HOLDS["created"] + 160 / FPS
        dead = replay.view_at(death + 0.1)
        self.assertEqual(
            next(item for item in dead["timelines"] if item["name"] == "run1")["phase"], "dead"
        )
        self.assertEqual(dead["rewinds"], 1)
        after = replay.view_at(death + HOLDS["death"] + HOLDS["rewind"] + 1.0)
        self.assertEqual(after["trunk"], "run4")
        roles = {item["name"]: item["role"] for item in after["timelines"]}
        self.assertEqual(roles["run1"], "dead")
        self.assertEqual(roles["run4"], "trunk")
        self.assertEqual(roles["slot3"], "checkpoint")
        self.assertEqual(after["status"], "running")
        self.assertEqual(after["mode"], "replay")
        self.assertEqual(after["fps"], FPS)
        self.assertEqual(replay.view_at(replay.end)["status"], "complete")

    def test_frames_are_written_for_live_timelines_and_checkpoints(self):
        output = Path(self.temp.name) / "out"
        replay = Replay(self.source, output)
        written = {}
        replay.write_frames(replay.view_at(HOLDS["created"] + 2.0), written)
        self.assertTrue((output / "live" / "run1.png").is_file())
        self.assertEqual((output / "live" / "slot3.png").read_bytes(), PNG + b"slot3")
        # slot2 predates any copied frame in the recording: fall back to the frame itself.
        self.assertEqual((output / "live" / "slot2.png").read_bytes(), PNG + bytes([0]))

    def test_run_honours_stop_and_speed(self):
        output = Path(self.temp.name) / "out"
        views = []
        replay = Replay(
            self.source, output, observer=views.append, stop=lambda: len(views) > 3, speed=16
        )
        self.assertEqual(replay.run(), "stopped")
        self.assertEqual(views[-1]["status"], "stopped")
        self.assertEqual(replay.speed, 16)

    def test_transport_seeks_pauses_and_parks_at_the_end(self):
        replay = Replay(self.source, Path(self.temp.name) / "out", speed=1)
        replay.pause()
        after_rewind = HOLDS["created"] + 160 / FPS + HOLDS["death"] + HOLDS["rewind"] + 0.5
        replay.seek(after_rewind)
        self.assertTrue(replay.paused)
        self.assertEqual(replay.clock(), after_rewind)
        view = replay.view_at(replay.clock())
        self.assertEqual(view["trunk"], "run4")
        replay.set_speed(2)
        self.assertEqual(replay.speed, 2)
        self.assertEqual(view["serial"], 1)
        self.assertTrue(view["paused"])
        replay.seek(999)
        self.assertEqual(replay.clock(), replay.end)
        replay.play()  # playing from the end restarts from zero
        self.assertLess(replay.clock(), 1.0)
        self.assertEqual(replay.serial, 3)

    def test_a_replay_stops_at_each_flag_until_told_to_go_on(self):
        events = [
            json.loads(line) for line in (self.source / "events.jsonl").read_text().splitlines()
        ]
        flag = dict(events[-1], type="stage_clear")
        events[-1:] = [
            flag,
            {"type": "stage_started", "t": 42.0, "elapsed": 41.0, "stage": "1-2"},
            {"type": "created", "t": 42.1, "elapsed": 41.1, "stage": "1-2", "sandbox": "run4"},
            {
                "type": "clear",
                "t": 50.0,
                "elapsed": 49.0,
                "stage": "1-2",
                "sandbox": "run4",
                "frame": 300,
            },
        ]
        (self.source / "events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        replay = Replay(self.source, Path(self.temp.name) / "out")
        self.assertEqual([(stop[1], stop[2]) for stop in replay.stops], [("1-1", "1-2")])
        boundary = replay.stops[0][0]
        replay.seek(boundary - 0.01)
        replay.base -= 5  # five seconds go by
        replay.hold_at_flag()
        self.assertTrue(replay.paused)
        self.assertAlmostEqual(replay.clock(), boundary)
        held = replay.view_at(replay.clock())
        self.assertEqual(held["intermission"], {"stage": "1-1", "next": "1-2"})
        self.assertNotIn("stage_started", [event["type"] for event in held["events"]])
        replay.play()  # play at a flag means go on
        self.assertFalse(replay.paused)
        self.assertIsNone(replay.view_at(replay.clock())["intermission"])
        replay.base -= 5
        replay.hold_at_flag()
        self.assertFalse(replay.paused, "a flag that was passed does not stop the replay again")
        replay.seek(0)
        replay.seek(boundary - 0.01)
        replay.base -= 5
        replay.hold_at_flag()
        self.assertTrue(replay.paused, "seeking back arms the flag again")

    def test_pictures_and_traces_come_from_the_recording(self):
        replay = Replay(self.source, Path(self.temp.name) / "out")
        self.assertEqual(replay.picture("run1", 41), PNG + bytes([40]))
        self.assertEqual(replay.picture("run1", None), PNG + bytes([160]))
        self.assertIsNone(replay.picture("nope", 1))
        trace = replay.trace("run4")
        self.assertEqual(trace[0][:2], [80, 300])
        rewind_at = HOLDS["created"] + 160 / FPS + HOLDS["death"]
        self.assertAlmostEqual(trace[0][2], rewind_at, places=2)
        # Frames stand still through the rewind hold, then advance at FPS.
        self.assertAlmostEqual(trace[-1][2], rewind_at + HOLDS["rewind"] + 152 / FPS, places=2)


if __name__ == "__main__":
    unittest.main()

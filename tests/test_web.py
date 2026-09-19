import io
import json
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from mnd.web import ControlRoom, Server, graceful_signals, handler
from tests.test_replay import make_recording


def png(width=16, height=16, shade=0):
    """A real, tiny PNG: the encoder rejects anything that only looks like one."""

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + bytes([shade, 200, 90]) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.room = ControlRoom(Path(self.temp.name), "unused")
        self.server = Server(("127.0.0.1", 0), handler(self.room))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.room.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, data=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(self.origin + path, data=data, headers=headers)
        return urllib.request.urlopen(request, timeout=3)

    def test_state_contains_no_credentials(self):
        with self.request("/api/state") as response:
            state = json.load(response)
        self.assertEqual(state["status"], "idle")
        self.assertNotIn("TYPESAFE_API_KEY", state)

    def test_foreign_webpage_cannot_start_a_run(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/start", b'{"mode":"simulation"}', "https://untrusted.example")
        self.assertEqual(error.exception.code, 403)
        self.assertIsNone(self.room.worker)

    def test_live_execution_requires_explicit_server_configuration(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/start", b'{"mode":"live"}', self.origin)
        self.assertEqual(error.exception.code, 400)
        self.assertIsNone(self.room.worker)

    def test_start_and_stop_controls(self):
        with self.request("/api/start", b'{"mode":"simulation"}', self.origin) as response:
            self.assertEqual(response.status, 202)
        with self.request("/api/stop", b"{}", self.origin) as response:
            self.assertEqual(response.status, 202)
        self.room.worker.join(timeout=3)
        self.assertEqual(self.room.state()["status"], "stopped")

    def test_completion_reaches_the_stream_before_the_worker_thread_exits(self):
        published, cleanup, exiting, exit_worker = (threading.Event() for _ in range(4))
        execute = self.room.execute

        async def finish(env):
            self.room.observe({**self.room.view, "status": "stopped"})
            published.set()
            cleanup.wait(3)  # recording export and VM removal still in progress

        def worker(*args):
            execute(*args)
            exiting.set()
            exit_worker.wait(3)  # reproduce is_alive() still being true after publication

        def stream_state(response):
            while line := response.readline():
                if line.startswith(b"data: "):
                    return json.loads(line[6:])
            self.fail("stream closed before publishing completion")

        with mock.patch("mnd.web.Orchestrator") as controller:
            controller.return_value.run = finish
            self.room.execute = worker
            try:
                self.room.start("simulation")
                self.assertTrue(published.wait(2))
                with self.request("/api/stream") as response:
                    before = stream_state(response)
                    self.assertTrue(before["busy"])
                    cleanup.set()
                    self.assertTrue(exiting.wait(2))
                    after = stream_state(response)
                self.assertFalse(after["busy"])
                self.assertGreater(after["version"], before["version"])
                self.assertTrue(self.room.worker.is_alive())
            finally:
                cleanup.set()
                exit_worker.set()
                self.room.worker.join(3)

    def test_startup_failure_publishes_an_idle_worker(self):
        with mock.patch("mnd.web.SimulatedBackend", side_effect=RuntimeError("boot failed")):
            self.room.start("simulation")
            self.room.worker.join(3)
        state = self.room.state()
        self.assertEqual(state["status"], "failed")
        self.assertFalse(state["busy"])
        self.assertIn("output", state)
        self.assertGreaterEqual(state["version"], 3)  # start, failure, completion

    def live_fixture(self):
        # No VM or key: model a worker waiting for the normal stop signal.
        self.room.output = Path(self.temp.name) / "0123456789ab"
        self.room.view = {**self.room.view, "mode": "live", "status": "running"}
        self.room.worker = threading.Thread(
            target=lambda: self.room.stop_event.wait(5), daemon=True
        )
        self.room.worker.start()
        return str(self.room.output)

    def test_hidden_player_freezes_but_never_ends_the_game(self):
        output = self.live_fixture()
        self.room.presence("tab-a", output, True, 1)
        self.room.presence("tab-b", output, True, 1)
        self.room.presence("tab-a", output, False, 2)
        self.assertIsNone(self.room.next_command(), "another visible player still owns the run")
        self.room.presence("tab-b", output, False, 2)
        self.assertFalse(self.room.stop_event.is_set())
        self.assertEqual(self.room.next_command(), {"type": "pause", "reason": "player_hidden"})
        self.room.presence("tab-b", output, False, 3)
        self.room.keep_viewer("tab-b")
        self.assertNotIn("tab-b", self.room.viewers, "hidden SSE streams cannot renew visibility")
        self.assertIsNone(self.room.next_command(), "queue at most one automatic pause")
        with self.assertRaises(ValueError):
            self.room.control({"action": "play", "output": output})
        self.room.presence("tab-b", output, True, 4)
        self.assertIsNone(self.room.next_command(), "returning to a tab must not resume play")
        self.room.control({"action": "play", "output": output})
        self.assertEqual(self.room.next_command(), {"type": "resume"})
        self.assertFalse(self.room.stop_event.is_set())
        self.assertIsNone(self.room.state()["viewer_pause_reason"])

    def test_closing_the_last_player_still_stops_the_run(self):
        output = self.live_fixture()
        self.room.presence("tab-a", output, True, 1)
        self.room.presence("tab-b", output, True, 1)
        self.room.presence("tab-a", output, False, 2, departing=True)
        self.assertFalse(self.room.stop_event.is_set())
        self.room.presence("tab-b", output, False, 2, departing=True)
        self.assertTrue(self.room.stop_event.is_set())
        self.assertIn("closed", self.room.stop_reason)

    def test_event_stream_keeps_a_visible_player_alive_without_heartbeat_posts(self):
        output = self.live_fixture()
        self.room.presence("tab-a", output, True, 1)
        # The heartbeat POST would have expired, but the player's stream is healthy.
        self.room.viewers["tab-a"] = time.monotonic() - 1
        with self.request("/api/stream?client=tab-a") as response:
            self.assertTrue(response.readline().startswith(b"data: "))
            response.readline()
            self.assertIn(b"keepalive", response.readline())
        with self.room.changed:
            self.room.expire_viewers()
        self.assertFalse(self.room.stop_event.is_set())
        self.assertIsNone(self.room.next_command())
        self.assertGreater(self.room.viewers["tab-a"], time.monotonic())

    def test_a_crashed_browser_freezes_without_losing_progress(self):
        output = self.live_fixture()
        with mock.patch("mnd.web.VIEWER_TIMEOUT", 0.02):
            self.room.presence("tab-a", output, True, 1)
        monitor = threading.Thread(target=self.room.watch_viewers, args=(self.room.output,))
        before = self.room.version
        monitor.start()
        try:
            self.room.wait_for_change(before, 2)
            self.assertFalse(self.room.stop_event.is_set())
            self.assertEqual(
                self.room.next_command(), {"type": "pause", "reason": "connection_lost"}
            )
            self.assertTrue(self.room.busy(), "the worker must remain available for resume")
            self.assertEqual(str(self.room.output), output)
        finally:
            self.room.stop()
            monitor.join(2)
        self.assertFalse(monitor.is_alive())

    def test_delayed_visibility_and_stop_messages_do_not_affect_a_new_run(self):
        output = self.live_fixture()
        self.room.presence("tab-a", output, True, 3)
        self.room.presence("tab-a", output, False, 2)
        self.assertFalse(self.room.stop_event.is_set(), "older presence arrived out of order")
        self.room.presence("tab-a", "previous-run", False, 4)
        self.room.stop("previous-run")
        self.assertFalse(self.room.stop_event.is_set())

    def test_stop_publishes_cleanup_state_before_releasing_the_run(self):
        release = threading.Event()
        self.room.worker = threading.Thread(target=lambda: release.wait(3), daemon=True)
        self.room.worker.start()
        self.room.view = {**self.room.view, "mode": "live", "status": "running"}
        before = self.room.version
        try:
            with self.request("/api/stop", b"{}", self.origin) as response:
                state = json.load(response)["state"]
            self.assertTrue(state["busy"])
            self.assertTrue(state["stopping"])
            self.assertGreater(state["version"], before)
        finally:
            release.set()

    def test_next_world_needs_the_current_flag_and_cannot_queue_twice(self):
        output = self.live_fixture()
        self.room.presence("tab-a", output, True, 1)
        for stage in (None, "1-1"):
            with self.assertRaises(ValueError):
                self.room.control({"action": "next", "stage": stage, "output": output})
        self.room.view = {**self.room.view, "intermission": {"stage": "1-1", "next": "1-2"}}
        self.room.control({"action": "next", "stage": "1-1", "output": output})
        self.room.control({"action": "next", "stage": "1-1", "output": output})
        self.assertEqual(self.room.next_command(), {"type": "next", "stage": "1-1"})
        self.assertIsNone(self.room.next_command())
        self.room.view = {**self.room.view, "intermission": {"stage": "1-2", "next": "1-3"}}
        with self.assertRaises(ValueError):
            self.room.control({"action": "next", "stage": "1-1", "output": output})
        self.assertIsNone(self.room.next_command())

    def test_a_run_started_right_after_a_stop_waits_for_the_old_one_to_leave(self):
        import threading
        import time

        release = threading.Event()

        def lingering():  # a stopped run still tearing its machines down
            release.wait(2)

        self.room.worker = threading.Thread(target=lingering, daemon=True)
        self.room.worker.start()
        self.room.stop_event.set()
        threading.Timer(0.3, release.set).start()
        began = time.monotonic()
        with self.request("/api/start", b'{"mode":"simulation"}', self.origin) as response:
            self.assertEqual(response.status, 202)
        self.assertGreaterEqual(time.monotonic() - began, 0.25)
        self.room.stop_event.set()

    def test_a_recording_being_watched_gives_way_to_the_next_run(self):
        import threading

        # A replay never ends by itself: at its end it parks until it is stopped.
        self.room.view = {**self.room.view, "status": "complete", "mode": "replay"}
        self.room.worker = threading.Thread(
            target=lambda: self.room.stop_event.wait(10), daemon=True
        )
        self.room.worker.start()
        with self.request("/api/start", b'{"mode":"simulation"}', self.origin) as response:
            self.assertEqual(response.status, 202)
        self.assertEqual(self.room.state()["mode"], "simulation")
        self.room.stop_event.set()

    def test_a_run_in_progress_still_refuses_another(self):
        import threading

        release = threading.Event()
        self.room.worker = threading.Thread(target=lambda: release.wait(5), daemon=True)
        self.room.worker.start()
        self.room.view = {**self.room.view, "status": "running", "mode": "live"}
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/start", b'{"mode":"simulation"}', self.origin)
            self.assertEqual(error.exception.code, 400)
        finally:
            release.set()

    def test_a_game_marked_soon_only_starts_as_a_preview(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/start", b'{"mode":"simulation","game":"bird"}', self.origin)
        self.assertEqual(error.exception.code, 400)
        self.assertIn("soon", json.load(error.exception)["error"])
        self.assertIsNone(self.room.worker)
        body = b'{"mode":"simulation","game":"bird","preview":true}'
        with self.request("/api/start", body, self.origin) as response:
            self.assertEqual(response.status, 202)
        with self.request("/api/stop", b"{}", self.origin) as response:
            self.assertEqual(response.status, 202)
        self.room.worker.join(timeout=3)

    def test_replay_requires_an_existing_recording(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/start", b'{"mode":"replay","run":"../../etc"}', self.origin)
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/start", b'{"mode":"replay","run":"0123456789ab"}', self.origin)
        self.assertEqual(error.exception.code, 400)
        self.assertIsNone(self.room.worker)
        with self.request("/api/runs") as response:
            self.assertEqual(json.load(response), [])

    def test_sigterm_is_turned_into_a_graceful_interrupt(self):
        import os
        import signal

        previous = signal.getsignal(signal.SIGTERM)
        try:
            graceful_signals()
            with self.assertRaises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGTERM)
                signal.pause() if hasattr(signal, "pause") else None
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_open_streams_do_not_hold_up_shutdown(self):
        import socket

        client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        client.sendall(b"GET /api/stream HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b"text/event-stream", client.recv(4096))
        started = time.monotonic()
        self.room.close()
        self.server.shutdown()
        self.server.server_close()
        self.assertLess(time.monotonic() - started, 5, "shutdown waited on an open stream")
        client.close()

    def test_schedule_and_stream(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/schedule")
        self.assertEqual(error.exception.code, 404)
        with self.request("/api/stream") as response:
            self.assertEqual(response.headers["Content-Type"], "text/event-stream")
            first = response.readline().decode()
            self.assertTrue(first.startswith("data: "))
            self.assertEqual(json.loads(first[6:])["status"], "idle")

    def test_a_recording_can_be_read_without_replaying_it(self):
        make_recording(Path(self.temp.name) / "0123456789ab")
        with self.request("/api/schedule?run=0123456789ab") as response:
            schedule = json.loads(response.read())
        self.assertEqual(schedule["events"][0]["type"], "created")
        self.assertIn("run4", schedule["anchors"])
        with self.request("/api/frame/run4?f=100&run=0123456789ab") as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertTrue(response.read().startswith(b"\x89PNG"))
        with self.request("/api/thumb/0123456789ab") as response:
            self.assertTrue(response.read().startswith(b"\x89PNG"))
        self.assertEqual(self.room.state()["status"], "idle", "reading must not start a replay")
        for path in ("/api/schedule?run=../../etc", "/api/thumb/nothere00000", "/api/thumb/0123"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 404)

    def test_a_recording_downloads_as_a_zip_that_replays_elsewhere(self):
        make_recording(Path(self.temp.name) / "0123456789ab")
        with self.request("/api/download/0123456789ab.zip") as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            bundle = zipfile.ZipFile(io.BytesIO(response.read()))
        names = bundle.namelist()
        self.assertIn("0123456789ab/events.jsonl", names)
        self.assertIn("0123456789ab/result.json", names)
        self.assertIn("0123456789ab/timelines/run4/timeline.tar", names)
        with tempfile.TemporaryDirectory() as elsewhere:
            bundle.extractall(elsewhere)
            other = ControlRoom(Path(elsewhere), "unused")
            self.assertEqual([run["id"] for run in other.runs()], ["0123456789ab"])
            self.assertEqual(other.schedule("0123456789ab")["events"][-1]["type"], "clear")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/download/ffffffffffff.zip")
        self.assertEqual(error.exception.code, 404)

    def post(self, path, data, headers):
        request = urllib.request.Request(
            self.origin + path, data=data, headers={"Origin": self.origin, **headers}
        )
        return urllib.request.urlopen(request, timeout=30)

    def test_rendering_without_an_encoder_says_so(self):
        with (
            mock.patch("mnd.web.shutil.which", return_value=None),
            self.assertRaises(urllib.error.HTTPError) as error,
        ):
            self.post("/api/render/start", b'{"fps":30}', {"Content-Type": "application/json"})
        self.assertEqual(error.exception.code, 501)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg on the host")
    def test_frames_from_the_page_become_an_mp4(self):
        json_type = {"Content-Type": "application/json"}
        with self.post("/api/render/start", b'{"fps":30}', json_type) as response:
            job = json.loads(response.read())["job"]
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(f"/api/render/{job}/frames", b"not a picture", {"X-Frames": "1"})
        self.assertEqual(error.exception.code, 400)
        for shade, repeat in ((0, 10), (120, 1), (240, 19)):
            headers = {"Content-Type": "application/octet-stream", "X-Frames": str(repeat)}
            self.post(f"/api/render/{job}/frames", png(shade=shade), headers).close()
        with self.post(f"/api/render/{job}/finish", b"{}", json_type) as response:
            result = json.loads(response.read())
        self.assertEqual(result["frames"], 30)
        with self.request(f"{result['url']}?name=mario-never-dies-test.mp4") as response:
            self.assertIn("mario-never-dies-test.mp4", response.headers["Content-Disposition"])
            video = response.read()
        self.assertEqual(video[4:8], b"ftyp")
        directory = self.room.renders[job]["directory"]
        self.post(f"/api/render/{job}/cancel", b"{}", json_type).close()
        self.assertFalse(directory.exists(), "a cancelled render leaves no files behind")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_a_sound_plan_is_mixed_into_the_video(self):
        json_type = {"Content-Type": "application/json"}
        with self.post("/api/render/start", b'{"fps":30}', json_type) as response:
            job = json.loads(response.read())["job"]
        headers = {"Content-Type": "application/octet-stream", "X-Frames": "90"}
        self.post(f"/api/render/{job}/frames", png(shade=80), headers).close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            plan = b'{"sound":[{"id":"../../etc/passwd","from":0,"to":1}]}'
            self.post(f"/api/render/{job}/finish", plan, json_type)
        self.assertEqual(error.exception.code, 400)
        plan = {
            "sound": [
                {"id": "overworld", "from": 0, "to": 1.5},
                {"id": "fork", "from": 1.5, "to": 3},
            ]
        }
        with self.post(f"/api/render/{job}/finish", json.dumps(plan).encode(), json_type) as reply:
            result = json.loads(reply.read())
        target = self.room.renders[job]["target"]
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration"]
            + ["-of", "json", str(target)],
            capture_output=True,
            check=True,
        )
        described = json.loads(probe.stdout)
        kinds = sorted(stream["codec_type"] for stream in described["streams"])
        self.assertEqual(kinds, ["audio", "video"])
        self.assertAlmostEqual(float(described["format"]["duration"]), 3.0, delta=0.15)
        self.assertGreater(result["bytes"], 0)
        self.post(f"/api/render/{job}/cancel", b"{}", json_type).close()

    def test_arbitrary_files_cannot_be_served(self):
        for path in (
            "/../pyproject.toml",
            "/assets/../../pyproject.toml",
            "/%2e%2e/pyproject.toml",
        ):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 404, path)
        with self.request("/assets/favicon.svg") as response:
            self.assertEqual(response.headers["Content-Type"], "image/svg+xml")

    def test_controls_are_scoped_to_the_active_mode(self):
        for body in (b'{"action":"pause"}', b'{"action":"speed","speed":2}'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/control", body, self.origin)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/control", b'{"action":"rewind","slot":"x"}', self.origin)
        self.assertEqual(error.exception.code, 400)
        with self.request("/api/start", b'{"mode":"simulation"}', self.origin):
            pass
        body = b'{"action":"rewind","slot":"mnd-slot2"}'
        with self.request("/api/control", body, self.origin) as response:
            self.assertEqual(response.status, 202)
        self.assertEqual(self.room.next_command(), {"type": "rewind", "slot": "mnd-slot2"})
        with self.request("/api/control", b'{"action":"pause"}', self.origin) as response:
            self.assertEqual(response.status, 202)
        self.assertEqual(self.room.next_command(), {"type": "pause"})
        with self.request("/api/control", b'{"action":"play"}', self.origin) as response:
            self.assertEqual(response.status, 202)
        self.assertEqual(self.room.next_command(), {"type": "resume"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/frame/bad%20name")
        self.assertEqual(error.exception.code, 400)
        with self.request("/api/trace/mnd-run1") as response:
            self.assertEqual(json.load(response), {"samples": []})
        self.request("/api/stop", b"{}", self.origin).close()
        self.room.worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()

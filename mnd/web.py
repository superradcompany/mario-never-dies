"""Local browser control room. The browser never receives the TypeSafe key."""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import queue
import random
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .backend import MicroVMBackend
from .games import GAMES, game_of
from .orchestrator import Orchestrator, Settings
from .protocol import identifier
from .replay import Replay, recorded_runs
from .simulation import SimulatedBackend

ASSETS = Path(__file__).resolve().parents[1] / "web"
SOUND_FADE = 0.9  # seconds two loops overlap in a downloaded video, as on the page
SOUND_LEVEL = 0.6  # the music is the video's only sound, so it sits well above the page's level


def ui_version() -> str:
    """Changes whenever the served page, script or stylesheet changes, so an open page
    can notice a redeploy and reload itself instead of running stale code."""
    stamp = "".join(
        f"{name}:{(ASSETS / name).stat().st_mtime_ns}"
        for name in ("index.html", "timeline.js", "sound.js", "app.js", "style.css")
        if (ASSETS / name).exists()
    )
    return str(abs(hash(stamp)))[:10]


UI_VERSION = ui_version()


class EncoderMissing(Exception):
    """The host has no ffmpeg; the page falls back to recording the video itself."""


class ControlRoom:
    def __init__(self, root: Path, image: str, live_enabled=False, rom: Path | None = None):
        self.root, self.image, self.live_enabled = root, image, live_enabled
        self.rom = rom
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self.stop_event = threading.Event()
        self.worker = None
        self.output = None
        self.replay = None
        self.commands = queue.Queue()
        self.closing = False
        self.library: dict[str, Replay] = {}  # finished runs loaded for reading, newest last
        self.thumbs: dict[str, bytes] = {}
        self.renders: dict[str, dict] = {}
        self.view = {
            "status": "idle",
            "mode": "live" if live_enabled else "simulation",
            "timelines": [],
            "events": [],
            "rewinds": 0,
            "elapsed": 0,
            "trunk": None,
        }

    def observe(self, view):
        # Views are replaced, never mutated, so readers can share them without copying.
        with self.changed:
            self.view = view
            self.version += 1
            self.changed.notify_all()

    def state(self):
        with self.lock:
            return {
                **self.view,
                "live_enabled": self.live_enabled,
                "key_configured": bool(os.environ.get("TYPESAFE_API_KEY")),
                "busy": bool(self.worker and self.worker.is_alive()),
                "version": self.version,
                "ui": UI_VERSION,
            }

    def wait_for_change(self, seen, timeout):
        """Block until a view newer than ``seen`` is published (or the timeout passes)."""
        with self.changed:
            self.changed.wait_for(lambda: self.version != seen or self.closing, timeout=timeout)
            return self.version

    def close(self):
        """Stop the run and release every event stream so the process can exit."""
        self.stop_event.set()
        with self.changed:
            self.closing = True
            self.changed.notify_all()
        if self.worker:
            self.worker.join(timeout=60)
        for job in list(self.renders):
            self.render_cancel(job)

    # ------------------------------------------------------------------ recordings

    def recording_dir(self, run) -> Path:
        if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{12}", run):
            raise ValueError("Unknown recording")
        source = self.root / run
        if not (source / "events.jsonl").is_file():
            raise ValueError("That recording is not available")
        if self.active(source) and not self.at_flag(source):
            raise ValueError("That run is still going")
        return source

    def active(self, source) -> bool:
        with self.lock:
            return bool(self.worker and self.worker.is_alive()) and self.output == source

    def at_flag(self, source) -> bool:
        """A live run held between worlds is at rest: every timeline of the worlds it has
        cleared is exported and nothing is being written, so it can be read and downloaded."""
        with self.lock:
            return self.output == source and bool(self.view.get("intermission"))

    def recording(self, run) -> Replay:
        """A finished run loaded for reading (its schedule and frames) without playing it:
        the lobby's attract loop and the video render both read recordings this way."""
        source = self.recording_dir(run)
        # A run read at a flag grows afterwards: key its reading by how much it held then.
        key = f"{run}:{(source / 'events.jsonl').stat().st_size}" if self.active(source) else run
        with self.lock:
            if self.replay is not None and self.replay.source == source:
                return self.replay
            cached = self.library.get(key)
        if cached is not None:
            return cached
        try:
            replay = Replay(source, None)
        except OSError as error:
            raise ValueError("That recording has no exported timelines") from error
        with self.lock:
            self.library[key] = replay
            while len(self.library) > 2:
                self.library.pop(next(iter(self.library)))
        return replay

    def thumb(self, run) -> bytes | None:
        """One picture that stands for a recording: two thirds into the last machine it
        played, so it shows the furthest stretch rather than a death or a black transition."""
        source = self.recording_dir(run)
        if run in self.thumbs:
            return self.thumbs[run]
        trunk = None
        for line in (source / "events.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("type") in {"created", "rewind", "promote"} and event.get("sandbox"):
                trunk = event["sandbox"]
        data = None
        try:
            frames = source / "frames" / identifier(trunk or "")
        except ValueError:
            frames = None
        if frames is not None and frames.is_dir():
            numbered = sorted(frames.glob("*.png"), key=lambda path: int(path.stem))
            if numbered:
                data = numbered[len(numbered) * 2 // 3].read_bytes()
        if data is None:
            pictures = sorted(
                (source / "live").glob("*.png"), key=lambda path: path.stat().st_mtime
            )
            preferred = source / "live" / f"{trunk}.png"
            if preferred.is_file():
                data = preferred.read_bytes()
            elif pictures:
                data = pictures[-1].read_bytes()
        if data:
            self.thumbs[run] = data
        return data

    def archive(self, run, destination):
        """The whole recording as a zip whose top folder is the run id: unzipped into
        ``runs/web/`` on another machine it shows up in the lobby and replays."""
        source = self.recording_dir(run)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as bundle:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    bundle.write(path, Path(run) / path.relative_to(source))

    # ------------------------------------------------------------------ video renders
    #
    # The page draws every frame of the video (it has the fonts and the layout); the host
    # only encodes. Frames arrive as PNGs and go straight into ffmpeg's stdin, so a render
    # runs as fast as the page can draw rather than in real time.

    def render_start(self, body) -> str:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise EncoderMissing("ffmpeg is not installed on the host")
        fps = body.get("fps", 30)
        if not isinstance(fps, int) or not 1 <= fps <= 60:
            raise ValueError("Frames per second must be between 1 and 60")
        for stale in list(self.renders)[:-1]:
            self.render_cancel(stale)
        job = uuid.uuid4().hex[:12]
        directory = Path(tempfile.mkdtemp(prefix="mnd-render-"))
        target = directory / "video.mp4"
        command = [ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe"]
        command += ["-framerate", str(fps), "-c:v", "png", "-i", "-"]
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
        command += ["-movflags", "+faststart", str(target)]
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        self.renders[job] = {
            "directory": directory,
            "target": target,
            "process": process,
            "lock": threading.Lock(),
            "frames": 0,
            "fps": fps,
        }
        return job

    def render_job(self, job) -> dict:
        entry = self.renders.get(job) if isinstance(job, str) else None
        if entry is None:
            raise ValueError("Unknown render")
        return entry

    def render_frames(self, job, data: bytes, repeat: int):
        entry = self.render_job(job)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Frames must be PNG images")
        with entry["lock"]:
            try:
                for _ in range(repeat):
                    entry["process"].stdin.write(data)
            except (BrokenPipeError, ValueError, OSError) as error:
                detail = entry["process"].stderr.read().decode(errors="replace")[-400:]
                raise ValueError(f"The encoder stopped: {detail or error}") from error
            entry["frames"] += repeat

    def render_finish(self, job, sound=None) -> dict:
        entry = self.render_job(job)
        with entry["lock"]:
            process = entry["process"]
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            try:
                code = process.wait(timeout=600)
            except subprocess.TimeoutExpired as error:
                process.kill()
                raise ValueError("The encoder did not finish") from error
            if code != 0 or not entry["target"].is_file():
                detail = process.stderr.read().decode(errors="replace")[-400:]
                raise ValueError(f"The encoder failed: {detail}")
            if sound:
                entry["target"] = self.add_sound(entry, sound)
        return {
            "url": f"/api/render/{job}.mp4",
            "bytes": entry["target"].stat().st_size,
            "frames": entry["frames"],
        }

    def add_sound(self, entry, segments) -> Path:
        """Lay the looped music under a finished video. Each segment names a loop and the
        span of video time it covers; neighbours overlap by a short crossfade, and the last
        one fades out with the picture."""
        audio = ASSETS / "assets" / "audio"
        loops = {item["id"]: item for item in json.loads((audio / "loops.json").read_text())}
        length = entry["frames"] / entry["fps"]
        if not isinstance(segments, list) or not 0 < len(segments) <= 200:
            raise ValueError("Invalid sound plan")
        inputs, chains = [], []
        for index, segment in enumerate(segments):
            loop = loops.get(segment.get("id")) if isinstance(segment, dict) else None
            start, end = (segment.get(key) for key in ("from", "to")) if loop else (None, None)
            if (
                loop is None
                or not re.fullmatch(r"[a-z0-9_-]+\.mp3", loop["file"])
                or not all(isinstance(value, int | float) for value in (start, end))
                or not 0 <= start < end <= length + 1
            ):
                raise ValueError("Invalid sound plan")
            first, last = index == 0, index == len(segments) - 1
            begin = max(0.0, start - (0 if first else SOUND_FADE / 2))
            span = min(length, end + (0 if last else SOUND_FADE / 2)) - begin
            if span <= 0.1:
                continue
            fade_in = 0.05 if first else min(SOUND_FADE, span / 2)
            fade_out = min(1.5 if last else SOUND_FADE, span / 2)
            size = round((loop["loopEnd"] - loop["loopStart"]) * 44100)
            inputs += ["-i", str(audio / loop["file"])]
            number = len(chains) + 1
            chains.append(
                f"[{number}:a]aresample=44100,"
                f"atrim=start={loop['loopStart']}:end={loop['loopEnd']},asetpts=N/SR/TB,"
                f"aloop=loop=-1:size={size},atrim=duration={span:.3f},"
                f"afade=t=in:st=0:d={fade_in:.3f},"
                f"afade=t=out:st={span - fade_out:.3f}:d={fade_out:.3f},"
                f"adelay={round(begin * 1000)}:all=1[s{number}]"
            )
        if not chains:
            return entry["target"]
        mixed = "".join(f"[s{number}]" for number in range(1, len(chains) + 1))
        mix = f"amix=inputs={len(chains)}:normalize=0:duration=longest," if len(chains) > 1 else ""
        chains.append(f"{mixed}{mix}volume={SOUND_LEVEL},atrim=duration={length:.3f}[music]")
        target = entry["directory"] / "video-with-sound.mp4"
        command = [shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-i", str(entry["target"])]
        command += [*inputs, "-filter_complex", ";".join(chains), "-map", "0:v", "-map", "[music]"]
        command += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        result = subprocess.run([*command, str(target)], capture_output=True, timeout=600)
        if result.returncode != 0 or not target.is_file():
            raise ValueError(f"The sound could not be added: {result.stderr.decode()[-400:]}")
        return target

    def render_cancel(self, job):
        entry = self.renders.pop(job, None)
        if entry is None:
            return
        process = entry["process"]
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for stream in (process.stdin, process.stderr):
            if stream and not stream.closed:
                stream.close()
        shutil.rmtree(entry["directory"], ignore_errors=True)

    def schedule(self, run=None):
        """The whole replay, for client-side scrubbing: every event on the game clock and
        each timeline's frame anchors. With ``run``, the same for a recording at rest."""
        if run is not None:
            replay = self.recording(run)
        else:
            with self.lock:
                replay = self.replay
        if replay is None:
            return None
        return {
            "events": replay.events,
            "anchors": {name: timeline.anchors for name, timeline in replay.timelines.items()},
            "end": replay.end,
            "fps": replay.fps,
            "game": replay.game,
            "frame_step": GAMES[replay.game]["frame_step"],
        }

    def runs(self):
        return recorded_runs(self.root)

    def start(self, mode, run=None, speed=1.0, game="mario", preview=False):
        if game not in GAMES:
            raise ValueError("Unknown game")
        if GAMES[game].get("soon") and mode != "replay" and not preview:
            raise ValueError("That game is coming soon")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("A run is already active")
            if mode not in {"simulation", "live", "replay"}:
                raise ValueError("Unknown mode")
            source = None
            if mode == "replay":
                if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{12}", run):
                    raise ValueError("Choose a recorded run to replay")
                source = self.root / run
                if not (source / "events.jsonl").is_file():
                    raise ValueError("That recording is not available")
                if not isinstance(speed, int | float) or not 0.1 <= speed <= 16:
                    raise ValueError("Replay speed must be between 0.1 and 16")
            if mode == "live" and not self.live_enabled:
                raise ValueError(
                    "Restart the server with --enable-live after building the guest image"
                )
            if mode == "live" and not os.environ.get("TYPESAFE_API_KEY"):
                raise ValueError("TYPESAFE_API_KEY is not configured on the host")
            # The guest image carries no game data, so Mario needs a ROM from the host.
            if mode == "live" and game == "mario" and not (self.rom and self.rom.is_file()):
                raise ValueError("Restart the server with --rom /path/to/super-mario-bros.nes")
            self.stop_event.clear()
            self.replay = None
            self.commands = queue.Queue()
            self.output = self.root / (
                f"replay-{uuid.uuid4().hex[:8]}" if mode == "replay" else uuid.uuid4().hex[:12]
            )
            if source is not None:
                lines = (source / "events.jsonl").read_text(encoding="utf-8").splitlines()
                game = game_of([json.loads(line) for line in lines[:4]])
            self.view = {
                "status": "starting",
                "mode": mode,
                "game": game,
                "timelines": [],
                "events": [],
                "rewinds": 0,
                "elapsed": 0,
                "trunk": None,
            }
            self.worker = threading.Thread(
                target=self.execute, args=(mode, source, speed, game), daemon=True
            )
            self.worker.start()

    def next_command(self):
        try:
            return self.commands.get_nowait()
        except queue.Empty:
            return None

    def control(self, body):
        """Browser controls: replay transport, or a manual rewind of the live run."""
        action = body.get("action")
        if action == "rewind":
            slot = identifier(str(body.get("slot", "")))
            if not (self.worker and self.worker.is_alive()) or self.replay is not None:
                raise ValueError("Rewinding a snapshot needs a live run")
            self.commands.put({"type": "rewind", "slot": slot})
            return
        replay = self.replay
        if action == "next":
            if replay is not None:
                replay.next_world()
            elif self.worker and self.worker.is_alive():
                self.commands.put({"type": "next"})
            else:
                raise ValueError("There is no run waiting at a flag")
            return
        if replay is None:
            if action in {"pause", "play"} and self.worker and self.worker.is_alive():
                self.commands.put({"type": "pause" if action == "pause" else "resume"})
                return
            raise ValueError("Transport controls need a running replay or live run")
        if action == "pause":
            replay.pause()
        elif action == "play":
            replay.play()
        elif action == "seek":
            elapsed = body.get("elapsed")
            if not isinstance(elapsed, int | float):
                raise ValueError("Seek needs an elapsed time")
            replay.seek(elapsed)
        elif action == "speed":
            speed = body.get("speed")
            if not isinstance(speed, int | float) or not 0.1 <= speed <= 16:
                raise ValueError("Speed must be between 0.1 and 16")
            replay.set_speed(speed)
        else:
            raise ValueError("Unknown control")

    def picture(self, name, frame, run=None):
        """Bytes for a timeline frame: an exact stored frame when asked, else the latest."""
        if run is not None:
            wanted = int(frame) if frame not in (None, "slot") else None
            return self.recording(run).picture(name, wanted)
        with self.lock:
            output, replay = self.output, self.replay
        if output is None:
            return None
        if replay is not None and frame != "slot":
            data = replay.picture(name, int(frame) if frame is not None else None)
            if data:
                return data
        if frame not in (None, "slot"):
            stored = output / "frames" / name / f"{int(frame)}.png"
            if stored.is_file():
                return stored.read_bytes()
        latest = output / "live" / f"{name}.png"
        return latest.read_bytes() if latest.is_file() else None

    def trace(self, name):
        with self.lock:
            output, replay = self.output, self.replay
        if replay is not None:
            return replay.trace(name)
        path = output / "traces" / f"{name}.jsonl" if output else None
        if not path or not path.is_file():
            return []
        return [
            [row["frame"], row["x"], round(row["elapsed"], 3)]
            for row in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        ]

    def execute(self, mode, source=None, speed=1.0, game="mario"):
        try:
            if mode == "replay":
                self.replay = Replay(
                    source,
                    self.output,
                    observer=self.observe,
                    stop=self.stop_event.is_set,
                    speed=speed,
                )
                self.replay.run()
                return
            backend = (
                SimulatedBackend()
                if mode == "simulation"
                else MicroVMBackend(image=self.image, rom=self.rom)
            )
            settings = Settings(
                poll_seconds=0.18 if mode == "simulation" else 0.1,
                game=game,
                stages=GAMES[game]["stages"],
                # Flappy is one life and no end: many more deaths in the same time, and a
                # fresh sky every run.
                seed=random.randrange(1, 1_000_000) if game == "bird" else None,
                run_timeout=4800,
                max_rewinds=400 if game == "bird" else 80,
                # A pipe is a narrow target: give a copy one more fork before falling back.
                races_per_checkpoint=3 if game == "bird" else 2,
                # Every copy of a stage stays frozen (and switchable) rather than the
                # oldest being killed to make room: a paused 126 MiB VM costs RAM only.
                max_slots=12,
                intermission=True,
            )
            env = {"TYPESAFE_API_KEY": os.environ["TYPESAFE_API_KEY"]} if mode == "live" else {}
            controller = Orchestrator(
                backend,
                self.output,
                settings=settings,
                observer=self.observe,
                stop=self.stop_event.is_set,
                commands=self.next_command,
            )
            asyncio.run(controller.run(env))
        except Exception as error:
            with self.changed:
                self.view = {**self.view, "status": "failed", "error": str(error)}
                self.version += 1
                self.changed.notify_all()


def handler(room):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive: frames and polls reuse one connection

        def log_message(self, fmt, *args):
            pass

        def stream(self):
            """Server-sent events: push every published view instead of being polled."""
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            seen = None
            try:
                while not room.closing:
                    state = room.state()
                    if state["version"] != seen:
                        seen = state["version"]
                        payload = json.dumps(state)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    room.wait_for_change(seen, timeout=1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def send(self, status, body, content_type="application/json", cache="no-store"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "font-src 'self'; script-src 'self'; connect-src 'self'; "
                "media-src 'self' blob:; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def attachment(self, source, size, content_type, filename):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            shutil.copyfileobj(source, self.wfile)

        def download(self, run):
            try:
                with tempfile.TemporaryFile() as bundle:
                    room.archive(run, bundle)
                    size = bundle.tell()
                    bundle.seek(0)
                    title = GAMES[room.recording(run).game]["title"]
                    name = f"{title}-never-dies-{run}.zip"
                    self.attachment(bundle, size, "application/zip", name)
            except ValueError as error:
                self.send(404, json.dumps({"error": str(error)}).encode())

        def rendered(self, job, name):
            try:
                target = room.render_job(job)["target"]
            except ValueError as error:
                return self.send(404, json.dumps({"error": str(error)}).encode())
            if not target.is_file():
                return self.send(404, b'{"error":"That video is not ready"}')
            filename = (
                name if re.fullmatch(r"[A-Za-z0-9._-]{1,80}\.mp4", name) else "mario-never-dies.mp4"
            )
            with target.open("rb") as source:
                self.attachment(source, target.stat().st_size, "video/mp4", filename)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/state":
                return self.send(200, json.dumps(room.state()).encode())
            if path == "/api/stream":
                return self.stream()
            query = parse_qs(urlparse(self.path).query)
            run = query.get("run", [None])[0]
            if path == "/api/schedule":
                try:
                    schedule = room.schedule(run)
                except ValueError as error:
                    return self.send(404, json.dumps({"error": str(error)}).encode())
                if schedule is None:
                    return self.send(404, b'{"error":"No replay is active"}')
                return self.send(200, json.dumps(schedule).encode())
            if path.startswith("/api/thumb/"):
                try:
                    picture = room.thumb(path[len("/api/thumb/") :])
                except ValueError as error:
                    return self.send(404, json.dumps({"error": str(error)}).encode())
                if picture:
                    return self.send(200, picture, "image/png", "max-age=3600")
                return self.send(404, b'{"error":"No picture"}')
            if path.startswith("/api/download/") and path.endswith(".zip"):
                return self.download(path[len("/api/download/") : -4])
            if path.startswith("/api/render/") and path.endswith(".mp4"):
                return self.rendered(path[len("/api/render/") : -4], query.get("name", [""])[0])
            if path == "/api/runs":
                return self.send(200, json.dumps(room.runs()).encode())
            if path.startswith("/api/frame/") or path.startswith("/api/trace/"):
                kind, name = path[5:10], path[11:]
                try:
                    identifier(name)
                    wanted = parse_qs(urlparse(self.path).query).get("f", [None])[0]
                    if wanted not in (None, "slot"):
                        int(wanted)
                except ValueError:
                    return self.send(400, b'{"error":"Invalid timeline"}')
                if kind == "trace":
                    return self.send(200, json.dumps({"samples": room.trace(name)}).encode())
                try:
                    picture = room.picture(name, wanted, run)
                except ValueError as error:
                    return self.send(404, json.dumps({"error": str(error)}).encode())
                if picture:
                    # A numbered frame never changes, so the browser may keep it for
                    # smooth playback; "latest" and slot pictures stay uncached.
                    cache = "max-age=3600" if wanted not in (None, "slot") else "no-store"
                    return self.send(200, picture, "image/png", cache)
                return self.send(404, b'{"error":"No frame yet"}')
            asset = {"/": "index.html"}.get(path, path.lstrip("/"))
            target = (ASSETS / asset).resolve()
            if target.is_file() and ASSETS in target.parents and ".." not in asset:
                body = target.read_bytes()
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if target.suffix == ".woff2":
                    content_type = "font/woff2"
                # The loops never change under a name; the page and its scripts always do.
                cache = "max-age=86400" if target.suffix == ".mp3" else "no-store"
                return self.send(200, body, content_type, cache)
            self.send(404, b'{"error":"Not found"}')

        def do_POST(self):
            # Local controls launch VMs and spend API credits. Reject cross-origin browser
            # requests; the server intentionally binds loopback and has no public admin mode.
            origin = self.headers.get("Origin")
            if origin != f"http://{self.headers.get('Host')}":
                return self.send(403, b'{"error":"Same-origin controls only"}')
            parts = urlparse(self.path).path.strip("/").split("/")
            if parts[:2] == ["api", "render"] and len(parts) == 4 and parts[3] == "frames":
                # One PNG per request, repeated X-Frames times: a held picture costs one upload.
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    repeat = int(self.headers.get("X-Frames", "1"))
                    if not 0 < length <= 16 * 1024 * 1024 or not 1 <= repeat <= 3600:
                        raise ValueError("Invalid frame upload")
                    room.render_frames(parts[2], self.rfile.read(length), repeat)
                except (ValueError, TypeError) as error:
                    return self.send(400, json.dumps({"error": str(error)}).encode())
                return self.send(202, b'{"ok":true}')
            if self.headers.get("Content-Type") != "application/json":
                return self.send(415, b'{"error":"Expected JSON"}')
            try:
                length = int(self.headers.get("Content-Length", "0"))
                # A render's finish carries its sound plan; every other request is tiny.
                if not 0 < length <= (32 * 1024 if parts[:2] == ["api", "render"] else 1024):
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if parts[:2] == ["api", "render"]:
                    if parts[2:] == ["start"]:
                        return self.send(200, json.dumps({"job": room.render_start(body)}).encode())
                    if len(parts) == 4 and parts[3] == "finish":
                        result = room.render_finish(parts[2], body.get("sound"))
                        return self.send(200, json.dumps(result).encode())
                    if len(parts) == 4 and parts[3] == "cancel":
                        room.render_cancel(parts[2])
                        return self.send(202, b'{"ok":true}')
                    return self.send(404, b'{"error":"Not found"}')
                if self.path == "/api/start":
                    room.start(
                        body.get("mode", "simulation"),
                        body.get("run"),
                        body.get("speed", 1),
                        body.get("game") or "mario",
                        body.get("preview") is True,
                    )
                elif self.path == "/api/stop":
                    room.stop_event.set()
                elif self.path == "/api/control":
                    room.control(body)
                else:
                    return self.send(404, b'{"error":"Not found"}')
            except EncoderMissing as error:
                return self.send(501, json.dumps({"error": str(error)}).encode())
            except (ValueError, TypeError, AttributeError) as error:
                return self.send(400, json.dumps({"error": str(error)}).encode())
            self.send(202, b'{"ok":true}')

    return Handler


class Server(ThreadingHTTPServer):
    """Handler threads (including open event streams) never hold up shutdown."""

    daemon_threads = True
    block_on_close = False


def graceful_signals():
    """Make ``kill <pid>`` behave like Ctrl-C: the server loop stops, the controller is
    told to stop, and it kills every VM it owns before the process exits. Without this a
    SIGTERM ends the process mid-run and leaves running microVMs behind."""

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--image", default="mnd:local")
    parser.add_argument("--runs", type=Path, default=Path("runs/web"))
    parser.add_argument("--enable-live", action="store_true")
    parser.add_argument("--rom", type=Path, help="Your local Super Mario Bros. NES ROM")
    args = parser.parse_args()
    # Use a private catalog unless the user explicitly chooses one for image loading.
    if not os.environ.get("MSB_HOME"):
        os.environ["MSB_HOME"] = tempfile.mkdtemp(prefix="mnd-home-", dir="/tmp")
    os.environ["MSB_BACKEND"] = "local"
    room = ControlRoom(args.runs.resolve(), args.image, args.enable_live, args.rom)
    server = Server(("127.0.0.1", args.port), handler(room))
    print(f"Mario Never Dies: http://127.0.0.1:{server.server_port}", flush=True)
    print(f"MSB_HOME={os.environ['MSB_HOME']}", flush=True)
    graceful_signals()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        room.close()
        server.server_close()


if __name__ == "__main__":
    main()

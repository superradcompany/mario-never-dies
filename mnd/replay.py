"""Replay a recorded live run at its original cadence. No Jev requests, no VMs.

A run directory holds ``events.jsonl`` and one ``timeline.tar`` per timeline with that
timeline's decision rows and frames. Playback runs on *game time*: emulator frames advance
at ``FPS`` per second, the seconds Jev spent thinking are folded away, and each dramatic
beat holds for a fixed moment so the browser's narration and the picture stay in step.
Original wall-clock timestamps are kept on every event as ``wall_elapsed``.
"""

from __future__ import annotations

import bisect
import json
import shutil
import tarfile
import threading
import time
from pathlib import Path

from .games import GAMES, game_of
from .protocol import identifier

RETAINED_SLOTS = 3
FPS = 60  # emulator frames per replay second at 1x: real NES speed
# Seconds the clock stands still after each beat, matched by the browser's narration.
HOLDS = {
    "created": 0.4,
    "death": 1.5,
    "retry_hypothesis": 0.9,
    "rewind": 1.0,
    "rewind_refused": 0.9,
    "multiverse": 1.0,
    "promote": 1.3,
    "race_failed": 0.9,
    "stage_clear": 2.4,
    "stage_started": 0.6,
    "clear": 2.4,
}


def recorded_runs(root: Path) -> list[dict]:
    """Recorded live runs under ``root`` that exported at least one timeline."""
    runs = []
    for directory in sorted(root.iterdir() if root.is_dir() else []):
        result, timelines = directory / "result.json", directory / "timelines"
        if not (result.is_file() and timelines.is_dir() and any(timelines.iterdir())):
            continue
        try:
            outcome = json.loads(result.read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
        except (OSError, ValueError):
            continue
        if outcome.get("simulated") or not events:
            continue
        runs.append(
            {
                "id": directory.name,
                "game": game_of(events),
                "status": outcome.get("status"),
                "rewinds": outcome.get("rewinds", 0),
                "forks": sum(event["type"] == "multiverse" for event in events),
                "completed_stages": outcome.get("completed_stages", []),
                "seconds": round(events[-1]["elapsed"]),
                "recorded": events[0]["t"],
            }
        )
    runs.sort(key=lambda run: run["recorded"], reverse=True)
    return runs


class Timeline:
    def __init__(self, name: str):
        self.name = name
        self.rows: list[dict] = []
        self.frames: dict[int, bytes] = {}
        self.frame_numbers: list[int] = []
        self.anchors: list[tuple[float, int]] = []  # (elapsed, frame)
        self.final_phase = "playing"
        self.ended_at: float | None = None
        self.end_phase = "playing"

    def elapsed_for(self, frame: int) -> float:
        """Inverse of ``frame_at``: the wall-clock moment this frame was on screen."""
        if not self.anchors:
            return 0.0
        if frame <= self.anchors[0][1]:
            return self.anchors[0][0]
        for (t0, f0), (t1, f1) in zip(self.anchors, self.anchors[1:], strict=False):
            if frame <= f1:
                return t0 + (t1 - t0) * (frame - f0) / max(f1 - f0, 1)
        return self.anchors[-1][0]

    def frame_at(self, elapsed: float) -> int:
        if not self.anchors:
            return 0
        if elapsed <= self.anchors[0][0]:
            return self.anchors[0][1]
        for (t0, f0), (t1, f1) in zip(self.anchors, self.anchors[1:], strict=False):
            if elapsed <= t1:
                return int(round(f0 + (f1 - f0) * (elapsed - t0) / max(t1 - t0, 1e-6)))
        return self.anchors[-1][1]

    def row_at(self, frame: int) -> dict | None:
        index = bisect.bisect_right([row["frame"] for row in self.rows], frame) - 1
        return self.rows[index] if index >= 0 else None

    def picture(self, frame: int) -> bytes | None:
        index = bisect.bisect_right(self.frame_numbers, frame) - 1
        return self.frames[self.frame_numbers[index]] if index >= 0 else None


class Replay:
    def __init__(self, source: Path, output: Path, *, observer=None, stop=None, speed=1.0):
        self.source, self.output = source, output
        self.observer = observer or (lambda _: None)
        self.stop = stop or (lambda: False)
        self.speed = max(0.1, min(float(speed), 16.0))
        self.events = [
            json.loads(line)
            for line in (source / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        if not self.events:
            raise ValueError("The recording has no events")
        for event in self.events:
            event.setdefault("stage", "1-1")
        self.game = game_of(self.events)
        self.fps = GAMES[self.game]["fps"]  # frames per replay second at 1x
        self.timelines: dict[str, Timeline] = {}
        self.slot_frames: dict[str, dict] = {}
        self.load()
        # Playback clock. ``offset`` is the replay time at ``base``; seeking bumps ``serial``
        # so the browser knows its narration must catch up rather than continue.
        self.lock = threading.Lock()
        self.offset, self.base, self.paused, self.serial = 0.0, time.monotonic(), False, 0
        # A replay stops where a live run does: at each flag, just before the next world.
        self.stops = [
            (event["elapsed"] - 0.02, previous["stage"], event["stage"])
            for previous, event in zip(self.events, self.events[1:], strict=False)
            if event["type"] == "stage_started"
        ]
        self.passed: set[int] = set()
        self.held: int | None = None

    # ------------------------------------------------------------------ clock

    def clock(self) -> float:
        with self.lock:
            if self.paused:
                return self.offset
            return min(self.offset + (time.monotonic() - self.base) * self.speed, self.end)

    def seek(self, elapsed: float):
        with self.lock:
            self.offset = max(0.0, min(float(elapsed), self.end))
            self.base = time.monotonic()
            self.serial += 1
            # Flags behind the new position are done with; the ones ahead stop again.
            self.passed = {i for i, stop in enumerate(self.stops) if stop[0] < self.offset}
            self.held = None

    def hold_at_flag(self):
        """Park at the next flag the clock has reached. Called once per tick."""
        with self.lock:
            if self.paused or self.held is not None:
                return
            now = min(self.offset + (time.monotonic() - self.base) * self.speed, self.end)
            for index, stop in enumerate(self.stops):
                if index not in self.passed and now >= stop[0]:
                    self.offset, self.paused, self.held = stop[0], True, index
                    return

    def next_world(self):
        with self.lock:
            if self.held is None:
                return
            self.passed.add(self.held)
            self.offset = self.stops[self.held][0] + 0.02
            self.held = None
            self.base = time.monotonic()
            self.paused = False

    def set_speed(self, speed: float):
        with self.lock:
            if not self.paused:
                self.offset = min(
                    self.offset + (time.monotonic() - self.base) * self.speed, self.end
                )
                self.base = time.monotonic()
            self.speed = max(0.1, min(float(speed), 16.0))

    def pause(self):
        with self.lock:
            if not self.paused:
                self.offset = min(
                    self.offset + (time.monotonic() - self.base) * self.speed, self.end
                )
                self.paused = True

    def play(self):
        if self.held is not None:  # play at a flag means go on
            return self.next_world()
        with self.lock:
            if self.paused:
                if self.offset >= self.end:
                    self.offset = 0.0
                    self.serial += 1
                self.base = time.monotonic()
                self.paused = False

    # ------------------------------------------------------------------ lookups

    def picture(self, name: str, frame: int | None) -> bytes | None:
        """The recorded frame at or just before ``frame`` (latest when ``frame`` is None)."""
        timeline = self.timelines.get(name)
        if timeline is None:
            return None
        if frame is None:
            frame = timeline.frame_numbers[-1] if timeline.frame_numbers else 0
        return timeline.picture(frame)

    def trace(self, name: str) -> list[list]:
        timeline = self.timelines.get(name)
        if timeline is None:
            return []
        return [
            [frame, row["x_pos"], round(timeline.elapsed_for(frame), 3)]
            for row in timeline.rows
            for frame in [row["frame"]]
        ]

    # ------------------------------------------------------------------ loading

    def load(self):
        for directory in sorted((self.source / "timelines").iterdir()):
            archive = directory / "timeline.tar"
            if not archive.is_file():
                continue
            timeline = Timeline(identifier(directory.name))
            with tarfile.open(archive) as tar:
                for member in tar.getmembers():
                    parts = Path(member.name).parts
                    if member.name.endswith("run.jsonl"):
                        text = tar.extractfile(member).read().decode("utf-8")
                        timeline.rows = [json.loads(line) for line in text.splitlines()]
                    elif len(parts) >= 2 and parts[-2] == "frames" and parts[-1].endswith(".png"):
                        timeline.frames[int(parts[-1][:-4])] = tar.extractfile(member).read()
                    elif member.name == "state.json":
                        timeline.final_phase = json.loads(tar.extractfile(member).read()).get(
                            "phase", "playing"
                        )
            timeline.frame_numbers = sorted(timeline.frames)
            self.timelines[timeline.name] = timeline
        self.schedule()

    def schedule(self):
        """Re-time every event onto the game clock and anchor each timeline's frames to it.

        The clock only moves while a live timeline's frames move; between the same two
        events every running timeline advances at ``FPS``.
        """
        g = 0.0
        trunk = None
        cursor: dict[str, int] = {}  # frame each timeline has reached on the game clock
        checkpoint_frames: dict[str, int] = {}
        race: list[str] = []
        for event in self.events:
            kind = event["type"]
            event["wall_elapsed"] = event["elapsed"]
            if kind == "created":
                trunk = event["sandbox"]
                cursor[trunk] = 0
                self.anchor(trunk, g, 0)
            elif kind == "checkpoint":
                checkpoint_frames[event["child"]] = event["frame"]
                self.slot_frames[event["child"]] = {
                    "frame": event["frame"],
                    "x_pos": event["x_pos"],
                    "sandbox": event["sandbox"],
                }
                g = self.advance(event["sandbox"], event["frame"], g, cursor)
            elif kind == "death":
                g = self.advance(event["sandbox"], event["frame"], g, cursor)
                self.finish(event["sandbox"], g, event["frame"], "dead")
            elif kind == "rewind":
                trunk = event["sandbox"]
                cursor[trunk] = event["frame"]
                self.anchor(trunk, g, event["frame"])
            elif kind == "multiverse":
                frame = checkpoint_frames.get(event["parent"], 0)
                race = [child for child in event["children"] if child in self.timelines]
                for child in race:
                    cursor[child] = frame
                    self.anchor(child, g, frame)
            elif kind == "promote":
                winner = self.timelines.get(event["sandbox"])
                target = cursor.get(event["sandbox"], 0)
                if winner and winner.rows:
                    row = next(
                        (row for row in winner.rows if row["x_pos"] >= event["x_pos"]),
                        winner.rows[-1],
                    )
                    target = row["frame"]
                g_promote = g + max(0, target - cursor.get(event["sandbox"], target)) / self.fps
                for child in race:
                    self.settle_child(child, g, g_promote, cursor)
                    if child != event["sandbox"]:
                        self.finish(
                            child, min(g_promote, self.child_end(child, g, cursor)), None, None
                        )
                cursor[event["sandbox"]] = target
                g = g_promote
                trunk = event["sandbox"]
                race = []
            elif kind == "race_failed":
                ends = [self.child_end(child, g, cursor) for child in race]
                for child in race:
                    self.settle_child(child, g, max(ends) if ends else g, cursor)
                    self.finish(child, self.child_end(child, g, cursor), None, None)
                g = max(ends) if ends else g
                race = []
            elif kind in {"clear", "stage_clear"}:
                g = self.advance(event["sandbox"], event["frame"], g, cursor)
                self.finish(event["sandbox"], g, event["frame"], "clear")
            elif kind in {"failed", "stopped", "cleanup_failed"}:
                for timeline in self.timelines.values():
                    if timeline.ended_at is None:
                        self.finish(timeline.name, g, None, "halted")
            event["elapsed"] = round(g, 3)
            hold = HOLDS.get(kind, 0.0)
            if hold:
                # The picture stands still while the browser narrates the beat.
                g += hold
                for name, frame in cursor.items():
                    timeline = self.timelines.get(name)
                    if timeline and timeline.ended_at is None:
                        self.anchor(name, g, frame)
        self.end = round(g, 3)

    def advance(self, name: str, frame: int, g: float, cursor: dict) -> float:
        """Move the clock to when ``name`` reaches ``frame`` and anchor it there."""
        g = g + max(0, frame - cursor.get(name, frame)) / self.fps
        cursor[name] = frame
        self.anchor(name, g, frame)
        return g

    def child_end(self, name: str, g_fork: float, cursor: dict) -> float:
        """When a racing child runs out of recorded frames, on the game clock."""
        timeline = self.timelines.get(name)
        last = timeline.rows[-1]["end_frame"] if timeline and timeline.rows else cursor.get(name, 0)
        return g_fork + max(0, last - cursor.get(name, 0)) / self.fps

    def settle_child(self, name: str, g_fork: float, g_end: float, cursor: dict):
        """Anchor a racing child's frames from the fork to whichever comes first: its own
        last recorded frame or the moment the race was decided."""
        timeline = self.timelines.get(name)
        if timeline is None:
            return
        own_end = self.child_end(name, g_fork, cursor)
        g_stop = min(own_end, g_end)
        frame = cursor.get(name, 0) + int(round((g_stop - g_fork) * self.fps))
        self.anchor(name, g_stop, frame)
        cursor[name] = frame

    def anchor(self, name: str, elapsed: float, frame: int):
        timeline = self.timelines.get(name)
        if timeline is not None:
            timeline.anchors.append((elapsed, frame))

    def finish(self, name: str, elapsed: float, frame: int | None, phase: str | None):
        timeline = self.timelines.get(name)
        if timeline is None or timeline.ended_at is not None:
            return
        if frame is None:
            frame = timeline.anchors[-1][1] if timeline.anchors else 0
        if not timeline.anchors or timeline.anchors[-1] != (elapsed, frame):
            timeline.anchors.append((elapsed, frame))
        timeline.ended_at = elapsed
        timeline.end_phase = phase or ("dead" if timeline.final_phase == "dead" else "discarded")

    # ------------------------------------------------------------------ playback

    def roles_at(self, elapsed: float) -> tuple[dict, str | None, list[str], list]:
        """Reconstruct the controller's role table the way the orchestrator assigns it."""
        roles, trunk, slots = {}, None, []
        retained = RETAINED_SLOTS
        visible = [event for event in self.events if event["elapsed"] <= elapsed]
        for event in visible:
            kind = event["type"]
            if kind == "created":
                trunk = event["sandbox"]
                roles[trunk] = "trunk"
                retained = int(event.get("slots") or RETAINED_SLOTS)
            elif kind == "checkpoint":
                roles[event["child"]] = "checkpoint"
                slots.append(event["child"])
                while len(slots) > retained:
                    roles[slots.pop(0)] = "retired"
            elif kind == "death":
                roles[event["sandbox"]] = "dead"
            elif kind == "rewind":
                trunk = event["sandbox"]
                roles[trunk] = "trunk"
                if event["parent"] in slots:
                    for slot in slots[slots.index(event["parent"]) + 1 :]:
                        roles[slot] = "retired"
                    slots = slots[: slots.index(event["parent"]) + 1]
            elif kind == "multiverse":
                if event["parent"] in slots:
                    for slot in slots[slots.index(event["parent"]) + 1 :]:
                        roles[slot] = "retired"
                    slots = slots[: slots.index(event["parent"]) + 1]
                for child in event["children"]:
                    if child not in event.get("errors", {}):
                        roles[child] = "candidate"
            elif kind == "promote":
                trunk = event["sandbox"]
                for name, role in list(roles.items()):
                    if role == "candidate" and name != trunk:
                        timeline = self.timelines.get(name)
                        roles[name] = timeline.end_phase if timeline else "discarded"
                roles[trunk] = "trunk"
            elif kind == "stage_clear":
                roles[event["sandbox"]] = "cleared"
            elif kind == "stage_started":
                for slot in slots:
                    roles[slot] = "retired"
                slots = []
        return roles, trunk, slots, visible

    def view_at(self, elapsed: float) -> dict:
        roles, trunk, slots, visible = self.roles_at(elapsed)
        last = visible[-1] if visible else self.events[0]
        stages = sorted({event["stage"] for event in self.events}, key=stage_key)
        completed = [event["stage"] for event in visible if event["type"] == "stage_clear"]
        timelines = []
        for name, role in roles.items():
            timeline = self.timelines.get(name)
            if timeline is None:
                slot = self.slot_frames.get(name, {})
                timelines.append(
                    {
                        "name": name,
                        "role": role,
                        "timeline": name,
                        "phase": "gate",
                        "frame": slot.get("frame", 0),
                        "x_pos": slot.get("x_pos", 0),
                    }
                )
                continue
            frame = timeline.frame_at(elapsed)
            row = timeline.row_at(frame) or {}
            ended = timeline.ended_at is not None and elapsed >= timeline.ended_at
            phase = timeline.end_phase if ended else "playing"
            timelines.append(
                {
                    "name": name,
                    "role": role,
                    "timeline": name,
                    "phase": phase,
                    "frame": frame,
                    "decision": row.get("decision", 0),
                    "x_pos": row.get("x_pos", 0),
                    "y_pos": row.get("y_pos", 0),
                    "coins": row.get("coins_before"),
                    "score": row.get("score_before"),
                    "experiment": row.get("experiment"),
                    "action": row.get("action"),
                    "proposed_action": row.get("proposed_action"),
                    "forced": row.get("forced", False),
                    "probabilities": row.get("probabilities", {}),
                    "confidence": row.get("confidence"),
                    "latency_ms": row.get("latency_ms"),
                    "input_tokens": sum(
                        item.get("input_tokens") or 0
                        for item in timeline.rows
                        if item["frame"] <= frame
                    ),
                }
            )
        finished = elapsed >= self.end
        final = self.events[-1]["type"]
        status = "running"
        if finished:
            status = {"clear": "complete", "stopped": "stopped"}.get(final, "failed")
        with self.lock:
            paused, serial = self.paused, self.serial
            held = self.stops[self.held] if self.held is not None else None
        return {
            "intermission": {"stage": held[1], "next": held[2]} if held else None,
            "paused": paused,
            "serial": serial,
            "status": status,
            "stage": last["stage"],
            "stages": stages,
            "completed_stages": completed,
            "mode": "replay",
            "policy": "typesafe",
            "trunk": trunk,
            "elapsed": elapsed,
            "rewinds": sum(event["type"] == "death" for event in visible),
            "races": sum(event["type"] == "multiverse" for event in visible),
            "timelines": timelines,
            "events": visible,
            "output": str(self.output),
            "source": self.source.name,
            "speed": self.speed,
            "fps": self.fps,
            "game": self.game,
            "frame_step": GAMES[self.game]["frame_step"],
            "holds": HOLDS,
            "duration": self.end,
            "marks": [
                {"type": event["type"], "elapsed": event["elapsed"]}
                for event in self.events
                if event["type"] in {"death", "multiverse", "rewind", "stage_clear", "clear"}
            ],
        }

    def write_frames(self, view: dict, written: dict):
        live = self.output / "live"
        live.mkdir(parents=True, exist_ok=True)
        for item in view["timelines"]:
            name = item["name"]
            # Snapshot pictures are written once, whether the copy is still frozen or was
            # evicted before this tick (a seek can land after the eviction).
            if name in self.slot_frames and name not in written:
                source = self.source / "live" / f"{name}.png"
                if source.is_file():
                    shutil.copyfile(source, live / f"{name}.png")
                else:
                    # The frame-zero checkpoint predates the first copied frame; use the
                    # recorded frame at the moment the copy was taken.
                    slot = self.slot_frames.get(name, {})
                    parent = self.timelines.get(slot.get("sandbox"))
                    picture = parent.picture(slot.get("frame", 0)) if parent else None
                    if picture:
                        (live / f"{name}.png").write_bytes(picture)
                written[name] = -1
                continue
            timeline = self.timelines.get(name)
            if timeline is None or written.get(name) == item["frame"]:
                continue
            picture = timeline.picture(item["frame"])
            if picture:
                temporary = live / f"{name}.tmp"
                temporary.write_bytes(picture)
                temporary.replace(live / f"{name}.png")
            written[name] = item["frame"]

    def run(self):
        """Publish views until stopped. At the end the replay parks, paused, so the browser
        can still scrub and seek back into it."""
        self.output.mkdir(parents=True, exist_ok=True)
        written: dict[str, int] = {}
        with self.lock:
            self.base = time.monotonic()
        elapsed = 0.0
        while not self.stop():
            self.hold_at_flag()
            elapsed = self.clock()
            view = self.view_at(elapsed)
            self.write_frames(view, written)
            self.observer(view)
            if elapsed >= self.end:
                self.pause()
            time.sleep(0.05)
        view = self.view_at(elapsed)
        view["status"] = "stopped"
        self.observer(view)
        return "stopped"


def stage_key(stage: str):
    return tuple(int(part) for part in stage.split("-")) if "-" in stage else (0,)

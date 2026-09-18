"""Deterministic protocol rehearsal. No emulator, Jev, microVM, or benchmark claims."""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from .protocol import ACTIONS
from .recovery import consume_sequence, sequence_chunk


class SimulatedBackend:
    simulated = True

    def __init__(self):
        self.vms = {}
        self.owned = set()

    async def create(self, name, **kwargs):
        self.owned.add(name)
        self.vms[name] = {
            "timeline": name,
            "phase": "gate",
            "gate": uuid.uuid4().hex,
            "frame": 0,
            "decision": 0,
            "checkpoint_seq": 1,
            "x_pos": 32,
            "y_pos": 80,
            "action": "right_run",
            "probabilities": dict(
                zip(ACTIONS, (0.01, 0.04, 0.22, 0.4, 0.3, 0.02, 0.01, 0, 0, 0, 0, 0), strict=True)
            ),
            "confidence": 0.42,
            "latency_ms": None,
            "history": [],
            "force_pending": None,
            "policy": "simulation",
            "paused": False,
            "escaped": False,
        }

    async def launch(self, name, **kwargs):
        pass

    async def state(self, name):
        state = self.vms[name]
        if state["paused"]:
            raise RuntimeError("Cannot read a paused VM")
        if state["phase"] == "playing":
            state["frame"] += 8
            state["decision"] += 1
            state["x_pos"] += 7
            force = state["force_pending"]
            if force and "steps" in force:
                chunk = sequence_chunk(force, {"player": {"x": state["x_pos"]}}, 8)
                if chunk:
                    state["action"] = chunk[0]
                    # What gets a trial past the hazard: Mario's running jump, or a bird's
                    # verse, which steers for another part of the gap.
                    state["escaped"] |= chunk[0] == "right_run_jump" or "aim" in force
                    consume_sequence(force, chunk[1])
                    state["forced"] = True
            elif force and force["x_min"] <= state["x_pos"] <= force["x_max"]:
                state["action"] = force["action"]
                state["escaped"] = force["action"] == "right_run_jump"
                state["force_pending"] = None
                state["forced"] = True
            if state["x_pos"] >= 230 and not state["escaped"]:
                state["phase"] = "dead"
            elif state["x_pos"] >= 520:
                state["phase"] = "clear"
            elif state["frame"] % 128 == 0:
                state["phase"] = "gate"
                state["gate"] = uuid.uuid4().hex
                state["checkpoint_seq"] += 1
        return copy.deepcopy(state)

    async def release(self, name, state, timeline, avoid, force=None):
        current = self.vms[name]
        if current["phase"] != "gate" or current["gate"] != state["gate"]:
            raise RuntimeError("Release did not match the captured gate")
        current.update(timeline=timeline, phase="playing", force_pending=copy.deepcopy(force))

    async def branch(self, source, child):
        self.vms[child] = copy.deepcopy(self.vms[source])
        self.vms[child]["paused"] = False
        self.owned.add(child)
        return None  # Never present simulated operation times as microVM measurements.

    async def branch_many(self, source, children):
        for child in children:
            await self.branch(source, child)
        return {}

    async def pause(self, name):
        self.vms[name]["paused"] = True

    async def resume(self, name):
        self.vms[name]["paused"] = False

    async def frame(self, name, destination):
        pass  # The web view draws a visibly schematic test scene instead of game pixels.

    async def pull(self, name, timeline, destination: Path):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "simulation.json").write_text(json.dumps(self.vms[name]), encoding="utf-8")

    async def kill(self, name):
        self.owned.discard(name)
        self.vms.pop(name, None)

    async def cleanup(self):
        for name in list(self.owned):
            await self.kill(name)
        return []

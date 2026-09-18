"""Checkpoint, rewind, and race whole VMs. Both backends use the same state machine."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .bird import experiments as bird_experiments
from .games import GAMES
from .protocol import TERMINAL, append_json, atomic_json, retry_evidence
from .recovery import experiments


@dataclass
class Settings:
    max_slots: int = 3
    intermission: bool = False  # stop at each flag until the browser says go
    rewind_margin: int = 96
    max_rewinds: int = 20
    max_attempts: int = 4
    poll_seconds: float = 0.1
    run_timeout: float = 1200
    stall_timeout: float = 45
    race_timeout: float = 45
    multiverse: bool = True
    races_per_checkpoint: int = 2
    checkpoint_frames: int = 150
    max_decisions: int = 2000
    policy: str = "typesafe"
    stages: tuple[str, ...] = ("1-1",)
    game: str = "mario"
    seed: int | None = None


@dataclass
class Slot:
    name: str
    state: dict


class Orchestrator:
    def __init__(
        self, backend, output: Path, *, settings=None, observer=None, stop=None, commands=None
    ):
        self.backend, self.output = backend, output
        self.settings = settings or Settings()
        self.observer = observer or (lambda _: None)
        self.stop = stop or (lambda: False)
        # Browser requests such as "rewind to this snapshot"; polled once per loop turn.
        self.commands = commands or (lambda: None)
        self.prefix = "mnd-" + uuid.uuid4().hex[:8]
        self.events, self.slots, self.avoid, self.hazards = [], [], [], []
        self.states, self.roles, self.last_progress = {}, {}, {}
        self.released, self.exported = set(), set()
        self.unpictured = {}  # slot name -> sandbox it was copied from, until a frame arrives
        self.tried_experiments = set()
        self.early_race_death = None
        self.experience = []
        self.sequence = self.rewinds = 0
        self.trunk, self.status = None, "starting"
        self.stage_index = 0
        self.completed_stages = []
        self.started = time.monotonic()
        # A person can pause the whole run: every running VM is paused and the clocks
        # (run budget, stall watchdog, race deadline) stop counting until it resumes.
        self.paused = False
        self.paused_seconds = 0.0
        self.intermission = None
        self.output.mkdir(parents=True, exist_ok=False)

    def name(self, kind):
        self.sequence += 1
        return f"{self.prefix}-{kind}{self.sequence}"

    def elapsed(self):
        return time.monotonic() - self.started - self.paused_seconds

    def publish(self):
        view = {
            "status": "paused" if self.paused else self.status,
            "paused": self.paused,
            "intermission": self.intermission,
            "stage": self.settings.stages[self.stage_index],
            "stages": list(self.settings.stages),
            "completed_stages": list(self.completed_stages),
            "game": self.settings.game,
            "fps": GAMES[self.settings.game]["fps"],
            "frame_step": GAMES[self.settings.game]["frame_step"],
            "mode": "simulation" if self.backend.simulated else "live",
            "policy": self.settings.policy,
            "trunk": self.trunk,
            "elapsed": self.elapsed(),
            "rewinds": self.rewinds,
            "races": sum(event["type"] == "multiverse" for event in self.events),
            "timelines": [
                {"name": name, "role": self.roles.get(name, "retired"), **state}
                for name, state in self.states.items()
            ],
            "events": list(self.events),
            "output": str(self.output),
        }
        self.observer(view)

    def event(self, kind, **fields):
        row = {
            "type": kind,
            "t": time.time(),
            "elapsed": self.elapsed(),
            "simulated": self.backend.simulated,
            "stage": self.settings.stages[self.stage_index],
            **fields,
        }
        self.events.append(row)
        append_json(self.output / "events.jsonl", row)
        self.publish()

    def check_deadline(self):
        if self.stop():
            raise asyncio.CancelledError("Stopped from browser")
        if self.elapsed() > self.settings.run_timeout:
            raise TimeoutError("Run exceeded its wall-clock budget")

    def running_vms(self):
        return [name for name, role in self.roles.items() if role in {"trunk", "candidate"}]

    async def service_commands(self, state=None):
        """Handle browser commands. Pausing blocks here, with every running VM frozen,
        until a resume (or stop) arrives; the caller's clocks are compensated."""
        command = self.commands()
        if command is None:
            return None
        kind = command.get("type")
        if kind == "pause" and not self.paused:
            machines = self.running_vms()
            for name in machines:
                await self.backend.pause(name)
            self.paused = True
            self.event("paused", machines=machines)
            paused_at = time.monotonic()
            try:
                while self.paused:
                    if self.stop():
                        raise asyncio.CancelledError("Stopped from browser")
                    later = self.commands()
                    if later and later.get("type") == "resume":
                        break
                    await asyncio.sleep(self.settings.poll_seconds)
            finally:
                pause_length = time.monotonic() - paused_at
                self.paused_seconds += pause_length
                # A frozen VM made no progress; do not let the watchdog count the pause.
                self.last_progress = {
                    name: (stamp, since + pause_length)
                    for name, (stamp, since) in self.last_progress.items()
                }
                for name in machines:
                    if self.roles.get(name) in {"trunk", "candidate"}:
                        await self.backend.resume(name)
                self.paused = False
                self.event("resumed", machines=machines, seconds=pause_length)
            return {"paused_for": pause_length}
        if kind == "rewind" and state is not None:
            return command
        return None

    async def heartbeat(self, name):
        self.check_deadline()
        try:
            state = await self.backend.state(name)
        except Exception as error:
            since = self.last_progress.setdefault(name, (None, time.monotonic()))[1]
            if time.monotonic() - since > self.settings.stall_timeout:
                raise RuntimeError(
                    f"Heartbeat unavailable for {name}: {type(error).__name__}"
                ) from error
            return None
        stamp = (state.get("frame"), state.get("phase"), state.get("gate"))
        previous, since = self.last_progress.setdefault(name, (None, time.monotonic()))
        if previous != stamp:
            self.last_progress[name] = (stamp, time.monotonic())
        elif (
            state["phase"] not in TERMINAL
            and time.monotonic() - since > self.settings.stall_timeout
        ):
            raise TimeoutError(f"Guest stopped advancing: {name}")
        old = self.states.get(name, {})
        self.states[name] = state
        if old.get("frame") != state.get("frame"):
            frame = self.output / "live" / f"{name}.png"
            await self.backend.frame(name, frame)
            self.record_frame(name, state, frame)
        self.publish()
        return state

    def record_frame(self, name, state, frame: Path):
        # Keep every frame the host saw, so the browser can scrub any timeline's past.
        if not frame.exists():
            return
        store = self.output / "frames" / name
        store.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(frame, store / f"{state['frame']}.png")
        # A copy frozen before the first frame arrived (the frame-zero checkpoint) gets the
        # first frame its source shows: the closest picture of that moment there is.
        for slot in [slot for slot, source in self.unpictured.items() if source == name]:
            shutil.copyfile(frame, self.output / "live" / f"{slot}.png")
            del self.unpictured[slot]
        append_json(
            self.output / "traces" / f"{name}.jsonl",
            {
                "frame": state["frame"],
                "x": state["x_pos"],
                "elapsed": self.elapsed(),
            },
        )

    async def release(self, name, state, force=None):
        await self.backend.release(name, state, name, self.avoid + self.experience[-24:], force)
        self.released.add((name, state["gate"]))
        self.last_progress.pop(name, None)

    async def checkpoint(self, name, state):
        slot_name = self.name("slot")
        started = time.perf_counter()
        branch_ms = await self.backend.branch(name, slot_name)
        await self.backend.pause(slot_name)
        self.slots.append(Slot(slot_name, dict(state)))
        frame = self.output / "live" / f"{name}.png"
        if frame.exists():
            shutil.copyfile(frame, self.output / "live" / f"{slot_name}.png")
        else:
            self.unpictured[slot_name] = name
        self.roles[slot_name] = "checkpoint"
        self.states[slot_name] = dict(state)
        self.event(
            "checkpoint",
            sandbox=name,
            child=slot_name,
            frame=state["frame"],
            x_pos=state["x_pos"],
            branch_ms=branch_ms,
            capture_and_pause_ms=(time.perf_counter() - started) * 1000,
        )
        while len(self.slots) > self.settings.max_slots:
            old = self.slots.pop(0)
            await self.backend.kill(old.name)
            self.roles[old.name] = "retired"
        await self.release(name, state)

    async def export(self, name):
        if name not in self.exported:
            await self.backend.pull(name, name, self.output / "timelines" / name)
            self.exported.add(name)

    async def retire(self, name, role="discarded"):
        await self.export(name)
        await self.backend.kill(name)
        self.roles[name] = role

    def hazard(self, x):
        # Death coordinates drift slightly across retries. Keep a stable attempt counter.
        match = next((item for item in self.hazards if abs(item["x"] - x) <= 64), None)
        if match is None:
            match = {"x": x, "attempts": 0}
            self.hazards.append(match)
        match["attempts"] += 1
        return match

    async def rewind(self, dead, state):
        self.rewinds += 1
        self.event("death", sandbox=dead, frame=state["frame"], x_pos=state["x_pos"])
        if self.rewinds > self.settings.max_rewinds:
            raise RuntimeError("Rewind budget exhausted")
        if not self.slots:
            raise RuntimeError("Guest died before the initial checkpoint")
        await self.retire(dead, "dead")
        hazard = self.hazard(state["x_pos"])
        death_frame = state["frame"]
        # Which move to avoid next time is a Mario idea; a bird's verses differ by intent.
        mario = self.settings.game == "mario"
        evidence = retry_evidence(state.get("history", []), state["x_pos"]) if mario else None
        if evidence and evidence not in self.avoid:
            self.avoid.append(evidence)
            self.event("retry_hypothesis", **evidence)
        eligible = [
            slot
            for slot in self.slots
            if slot.state["x_pos"] <= state["x_pos"] - self.settings.rewind_margin
        ]
        target = (eligible or self.slots[:1])[-1]
        if hazard["attempts"] > self.settings.max_attempts:
            index = self.slots.index(target)
            if index == 0:
                raise RuntimeError("Retry budget exhausted at the oldest retained checkpoint")
            target = self.slots[index - 1]
            hazard["attempts"] = 1
            self.avoid.clear()
        # Discard checkpoints from the failed future; sequence numbers may repeat after restore.
        target_index = self.slots.index(target)
        for slot in self.slots[target_index + 1 :]:
            await self.backend.kill(slot.name)
            self.roles[slot.name] = "retired"
        self.slots = self.slots[: target_index + 1]
        if self.settings.multiverse:
            # Every death forks: four futures from the copy, never a single retry first.
            # When none of them makes it, fork again with fresh experiments, and after
            # races_per_checkpoint failures fall back one copy and fork from there.
            failed_here = 0
            while True:
                survivor = await self.race(target, hazard["x"], death_frame=death_frame)
                if survivor:
                    return survivor
                exhausted = bool(self.events) and self.events[-1]["type"] == "experiments_exhausted"
                if not exhausted:
                    self.event("race_failed", checkpoint=target.name)
                    failed_here += 1
                self.avoid.clear()
                if self.early_race_death:
                    # All children hit the same earlier obstacle before any of
                    # their assigned variations started. Try that observed death,
                    # rather than replaying identical approaches to a distant x.
                    earlier = self.early_race_death
                    previous_x = hazard["x"]
                    hazard = self.hazard(earlier["x_pos"])
                    death_frame = earlier["frame"]
                    evidence = retry_evidence(earlier.get("history", []), earlier["x_pos"])
                    if evidence and mario:
                        self.avoid.append(evidence)
                    self.event(
                        "recovery_retarget",
                        checkpoint=target.name,
                        from_x=previous_x,
                        x_pos=hazard["x"],
                        frame=death_frame,
                        reason="All candidates died here before their experiments began",
                    )
                if not exhausted and failed_here < self.settings.races_per_checkpoint:
                    continue
                index = self.slots.index(target)
                if index == 0:
                    raise RuntimeError(
                        "Recovery exhausted at the oldest retained checkpoint; "
                        "no candidate qualified (see experiment outcomes)"
                    )
                target = self.slots[index - 1]
                hazard["attempts"] = 1
                failed_here = 0
                for slot in self.slots[index:]:
                    await self.backend.kill(slot.name)
                    self.roles[slot.name] = "retired"
                self.slots = self.slots[:index]
        child = self.name("run")
        started = time.perf_counter()
        branch_ms = await self.backend.branch(target.name, child)
        self.roles[child] = "trunk"
        await self.release(child, target.state)
        self.event(
            "rewind",
            sandbox=child,
            parent=target.name,
            from_x=state["x_pos"],
            to_x=target.state["x_pos"],
            frame=target.state["frame"],
            branch_ms=branch_ms,
            branch_and_release_ms=(time.perf_counter() - started) * 1000,
        )
        return child

    async def manual_rewind(self, trunk, state, slot_name):
        """A person chose a frozen copy in the browser: branch it and make it the present."""
        target = next((slot for slot in self.slots if slot.name == slot_name), None)
        if target is None:
            self.event("rewind_refused", slot=slot_name, reason="not a retained checkpoint")
            return trunk
        await self.retire(trunk, "discarded")
        target_index = self.slots.index(target)
        for slot in self.slots[target_index + 1 :]:
            await self.backend.kill(slot.name)
            self.roles[slot.name] = "retired"
        self.slots = self.slots[: target_index + 1]
        child = self.name("run")
        started = time.perf_counter()
        branch_ms = await self.backend.branch(target.name, child)
        self.roles[child] = "trunk"
        await self.release(child, target.state)
        self.event(
            "rewind",
            manual=True,
            sandbox=child,
            parent=target.name,
            from_x=state["x_pos"],
            to_x=target.state["x_pos"],
            frame=target.state["frame"],
            branch_ms=branch_ms,
            branch_and_release_ms=(time.perf_counter() - started) * 1000,
        )
        return child

    async def race(self, target, death_x, *, death_frame=None):
        self.early_race_death = None
        # API time is not gameplay time. Earlier copies may need hundreds of
        # decisions to reach a trial; bound that approach by the previously
        # observed journey plus one checkpoint interval, not a 45-second race.
        start_frame = target.state["frame"]
        approach_frames = (
            max(self.settings.checkpoint_frames, (death_frame or start_frame) - start_frame)
            + self.settings.checkpoint_frames
        )
        scope = (self.settings.stages[self.stage_index], target.name, death_x)
        plans = [
            p
            for p in (bird_experiments if self.settings.game == "bird" else experiments)(
                target.state["x_pos"], death_x
            )
            if (*scope, p["experiment_id"]) not in self.tried_experiments
        ][:4]
        if not plans:
            self.event("experiments_exhausted", checkpoint=target.name, hazard_x=death_x)
            return None
        children = [self.name("race") for _ in plans]
        started = time.perf_counter()
        errors = await self.backend.branch_many(target.name, children)
        self.event(
            "multiverse",
            parent=target.name,
            children=children,
            errors=errors,
            branch_ms=(time.perf_counter() - started) * 1000
            if not self.backend.simulated
            else None,
            experiments=dict(zip(children, plans, strict=True)),
            hazard_x=death_x,
            approach_frame_budget=approach_frames,
        )
        active = []
        assigned = dict(zip(children, plans, strict=True))
        deadlines, early_deaths = {}, []

        def record(child, outcome, state=None):
            plan = assigned[child]
            state = state or self.states.get(child, {})
            pending = state.get("force_pending") or {}
            sequence_status = pending.get("status")
            index = pending.get("step_index", 0)
            steps = pending.get("steps", [])
            used_frames = sum(step["frames"] for step in steps[:index])
            if index < len(steps) and "remaining" in pending:
                used_frames += steps[index]["frames"] - pending["remaining"]
            # Waiting/missed sequences and safety aborts before the first input
            # were never tested. Do not credit or blame unexecuted moves.
            if outcome in {"survived", "dead"} and used_frames > 0:
                stage = self.settings.stages[self.stage_index].replace("-", ":")
                self.experience.append(
                    {
                        "kind": "experiment",
                        "context": f"{stage}:{target.state.get('area', 1)}:experiment",
                        "x_pos": death_x,
                        "action": "sequence",
                        "label": plan["label"],
                        "outcome": outcome,
                    }
                )
            self.event(
                "experiment_result",
                sandbox=child,
                checkpoint=target.name,
                hazard_x=death_x,
                experiment_id=plan["experiment_id"],
                label=plan["label"],
                outcome=outcome,
                x_pos=state.get("x_pos"),
                frame=state.get("frame"),
                sequence_status=sequence_status,
                sequence_frames=used_frames,
                trial_started=child in deadlines,
            )

        for child, plan in zip(children, plans, strict=True):
            if child in errors:
                record(child, "startup_failed")
                continue
            # Reserve before release: even a timeout must not schedule this same
            # checkpoint/experiment again. Outcomes are journaled separately below.
            self.tried_experiments.add((*scope, plan["experiment_id"]))
            self.roles[child] = "candidate"
            await self.release(child, target.state, plan)
            active.append(child)
        started_children = len(active)
        while active:
            qualified = []
            for child in list(active):
                state = await self.heartbeat(child)
                if not state:
                    continue
                plan = assigned[child]
                pending = state.get("force_pending") or {}
                if child not in deadlines and (
                    pending.get("experiment_id") == plan["experiment_id"]
                    and pending.get("status") in {"active", "complete", "aborted", "missed"}
                ):
                    deadlines[child] = time.monotonic() + self.settings.race_timeout
                    self.event(
                        "experiment_started",
                        sandbox=child,
                        frame=state["frame"],
                        x_pos=state["x_pos"],
                        experiment_id=plan["experiment_id"],
                    )
                # A trial has made it once it is well past the death, or waits at a
                # milestone gate beyond it (a cleared pipe): promoted there, the new
                # trunk is copied at that gate before it flies on.
                beyond = state["x_pos"] > death_x + 64 or (
                    state["phase"] == "gate"
                    and state.get("milestone")
                    and state["x_pos"] > death_x
                    and (child, state["gate"]) not in self.released
                )
                if state["phase"] == "clear" or (
                    state["phase"] not in TERMINAL
                    and beyond
                    and (state.get("force_pending") or {}).get("status")
                    not in {"waiting", "active"}
                ):
                    qualified.append((child, state))
                elif state["phase"] in TERMINAL:
                    if (
                        state["phase"] == "dead"
                        and child not in deadlines
                        and pending.get("status") == "waiting"
                        and state["x_pos"] < min(p["x_min"] for p in plans)
                        and state["x_pos"] < death_x - 64
                        and state.get("area") == target.state.get("area")
                    ):
                        early_deaths.append(state)
                    record(child, state["phase"], state)
                    await self.retire(child, state["phase"])
                    active.remove(child)
                elif child in deadlines and time.monotonic() >= deadlines[child]:
                    record(child, "timed_out", state)
                    await self.retire(child, "timed_out")
                    active.remove(child)
                elif child not in deadlines and state["frame"] - start_frame >= approach_frames:
                    record(child, "approach_exhausted", state)
                    await self.retire(child, "approach_exhausted")
                    active.remove(child)
                elif state["phase"] == "gate" and (child, state["gate"]) not in self.released:
                    await self.release(child, state, state.get("force_pending"))
            if qualified:
                # Do not add a waiting window or extra VMs. When multiple survivors
                # qualify in the same poll, prefer retained power-ups and rewards
                # instead of choosing whichever child happens to be listed first.
                def quality(candidate):
                    state = candidate[1]
                    return (
                        state["phase"] == "clear",
                        {"small": 0, "tall": 1, "fireball": 2}.get(state.get("powerup_status"), 0),
                        (state.get("coins", 0) - target.state.get("coins", 0)) % 100,
                        state.get("score", 0),
                        state["x_pos"],
                    )

                child, state = max(qualified, key=quality)
                for loser in active:
                    if loser != child:
                        record(loser, "cancelled_after_winner")
                        await self.retire(loser)
                record(child, "survived", state)
                self.roles[child] = "trunk"
                self.event(
                    "promote",
                    sandbox=child,
                    parent=target.name,
                    x_pos=state["x_pos"],
                    coins=state.get("coins"),
                    score=state.get("score"),
                    selection="qualified survivor; power-up, coins, score, then progress",
                )
                return child
            paused = await self.service_commands()
            if paused:
                deadlines = {name: end + paused["paused_for"] for name, end in deadlines.items()}
            await asyncio.sleep(self.settings.poll_seconds)
        if (
            early_deaths
            and len(early_deaths) == started_children
            and max(s["x_pos"] for s in early_deaths) - min(s["x_pos"] for s in early_deaths) <= 64
        ):
            self.early_race_death = min(early_deaths, key=lambda s: s["frame"])
        return None

    async def start_stage(self, env):
        self.trunk = self.name("run")
        self.roles[self.trunk] = "trunk"
        await self.backend.create(self.trunk, env=env)
        await self.backend.launch(
            self.trunk,
            policy=self.settings.policy,
            checkpoint_frames=self.settings.checkpoint_frames,
            max_decisions=self.settings.max_decisions,
            stage=self.settings.stages[self.stage_index],
            game=self.settings.game,
            seed=self.settings.seed,
        )
        self.status = "running"
        self.event(
            "created",
            sandbox=self.trunk,
            slots=self.settings.max_slots,
            game=self.settings.game,
            seed=self.settings.seed,
        )

    async def hold_at_flag(self, cleared, following):
        """Stop between worlds until the browser says go. The machine that touched the flag
        is frozen, nothing is spent, and the wait is kept off every clock."""
        await self.backend.pause(self.trunk)
        self.intermission = {"stage": cleared, "next": following}
        self.event("intermission", sandbox=self.trunk, next=following)
        waiting_since = time.monotonic()
        try:
            while True:
                if self.stop():
                    raise asyncio.CancelledError("Stopped from browser")
                command = self.commands()
                if command and command.get("type") == "next":
                    break
                await asyncio.sleep(self.settings.poll_seconds)
        finally:
            waited = time.monotonic() - waiting_since
            self.paused_seconds += waited
            self.intermission = None
        self.event("intermission_over", seconds=waited)

    async def advance_stage(self, env):
        # Retire every previous-stage checkpoint before creating the next stage.
        # Position-based retry rules and hazards must never cross a level boundary.
        await self.retire(self.trunk, "cleared")
        for slot in self.slots:
            await self.backend.kill(slot.name)
            self.roles[slot.name] = "retired"
        self.slots.clear()
        self.avoid.clear()
        self.hazards.clear()
        self.experience.clear()
        self.released.clear()
        self.last_progress.clear()
        self.stage_index += 1
        self.event("stage_started")
        await self.start_stage(env)

    async def run(self, env=None):
        failure = None
        try:
            await self.start_stage(env)
            while True:
                state = await self.heartbeat(self.trunk)
                if state:
                    phase = state["phase"]
                    if phase == "gate" and (self.trunk, state["gate"]) not in self.released:
                        await self.checkpoint(self.trunk, state)
                    elif phase == "dead":
                        self.trunk = await self.rewind(self.trunk, state)
                    elif phase == "clear":
                        await self.export(self.trunk)
                        self.completed_stages.append(self.settings.stages[self.stage_index])
                        self.event("stage_clear", sandbox=self.trunk, frame=state["frame"])
                        if self.stage_index + 1 < len(self.settings.stages):
                            if self.settings.intermission:
                                stages = self.settings.stages
                                await self.hold_at_flag(
                                    stages[self.stage_index], stages[self.stage_index + 1]
                                )
                            await self.advance_stage(env)
                        else:
                            self.event("clear", sandbox=self.trunk, frame=state["frame"])
                            self.status = "complete"
                            break
                    elif phase in {"error", "limit"}:
                        await self.export(self.trunk)
                        raise RuntimeError(
                            f"Guest ended with {phase}: {state.get('reason', state.get('error'))}"
                        )
                    else:
                        command = await self.service_commands(state)
                        if command and command.get("type") == "rewind":
                            self.trunk = await self.manual_rewind(
                                self.trunk, state, command.get("slot")
                            )
                await asyncio.sleep(self.settings.poll_seconds)
        except asyncio.CancelledError:
            self.status = "stopped"
            self.event("stopped")
        except Exception as error:
            failure = str(error)
            self.status = "failed"
            self.event("failed", error=failure)
        finally:
            cleanup = await self.backend.cleanup()
            if cleanup:
                self.status = "cleanup_failed"
                self.event("cleanup_failed", errors=cleanup)
            atomic_json(self.output / "events.json", self.events)
            atomic_json(
                self.output / "result.json",
                {
                    "status": self.status,
                    "simulated": self.backend.simulated,
                    "rewinds": self.rewinds,
                    "completed_stages": self.completed_stages,
                    "error": failure,
                    "cleanup_errors": cleanup,
                },
            )
            self.publish()
        return self.status

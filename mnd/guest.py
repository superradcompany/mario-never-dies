"""Headless emulator worker. A live VM branch resumes this process, not a new episode."""

from __future__ import annotations

import argparse
import shutil
import time
import traceback
from collections import deque
from pathlib import Path

from .perception import (
    DecisionContext,
    action_frames,
    enrich_questions,
    make_parser,
    retreat_finished,
)
from .protocol import Gate, GuestStopped, allowed_actions, append_json, atomic_json, context_key
from .recovery import consume_sequence, interrupt_sequence, sequence_chunk


class PerDecisionClient:
    """Close HTTP connections before checkpoints; retain the same state on retries."""

    def __init__(self, enrich=enrich_questions):
        self.enrich = enrich  # Mario adds map hints to the questions; other games do not
        self.input_tokens = None
        self.on_retry = lambda attempt: None
        self.should_stop = lambda: False

    def system_one(self, **kwargs):
        from typesafe_sdk import (
            RetryPolicy,
            TypeSafeAPIConnectionError,
            TypeSafeAPITimeoutError,
            TypeSafeClient,
            TypeSafeInternalServerError,
            TypeSafeRateLimitError,
        )

        self.input_tokens = None
        if self.enrich:
            kwargs["questions"] = self.enrich(kwargs["state"], kwargs["questions"])
        transient = (
            TypeSafeAPIConnectionError,
            TypeSafeAPITimeoutError,
            TypeSafeInternalServerError,
            TypeSafeRateLimitError,
        )
        # Three bounded request windows, with no emulator steps between them.
        # Authentication/validation errors still fail immediately. Client shutdown
        # occurs even on failure, so snapshots never inherit an open connection.
        for attempt in range(3):
            if self.should_stop():
                raise GuestStopped
            try:
                with TypeSafeClient(
                    retry=RetryPolicy(
                        max_retries=2,
                        timeout=8.0,
                        backoff_initial=0.2,
                        backoff_max=0.5,
                    )
                ) as client:
                    response = client.system_one(**kwargs)
                    self.input_tokens = int(response.usage.input_tokens)
                    return response
            except transient:
                if attempt == 2:
                    raise
                self.on_retry(attempt + 1)
                # Small interruptible backoff; the VM remains observable and stoppable.
                for _ in range(10 * (attempt + 1)):
                    if self.should_stop():
                        raise GuestStopped from None
                    time.sleep(0.1)

    def close(self):
        pass


def make_policy(mode: str):
    from typesafe_mario.policy import HeuristicPolicy, TypeSafePolicy

    if mode == "heuristic":
        return HeuristicPolicy(), None
    from .policy import ControllerPolicy

    policy = TypeSafePolicy() if mode == "baseline" else ControllerPolicy()
    policy.close()
    client = PerDecisionClient()
    # This adapts the upstream private client interface. The Dockerfile pins its
    # revision, and the image smoke check verifies this attribute before launch.
    policy._client = client
    return policy, client


def run(args):
    import gym_super_mario_bros  # noqa: F401
    import gymnasium as gym
    from nes_py.wrappers import JoypadSpace
    from PIL import Image

    from .actions import ACTION_TO_INDEX, JUMP_ACTIONS, JUMP_RELEASE_ACTION, MOVEMENT, Action

    root = args.root
    root.mkdir(parents=True, exist_ok=True)
    rom_directory = Path(gym_super_mario_bros.__file__).parent / "_roms"
    rom = Path("/rom.nes")
    # The image carries no game data: the host writes the ROM in when it creates the machine.
    if rom.is_file():
        if rom.read_bytes()[:4] != b"NES\x1a":
            raise RuntimeError("The supplied ROM is not an iNES file")
        shutil.copyfile(rom, rom_directory / "super-mario-bros.nes")
    elif not (rom_directory / "super-mario-bros.nes").is_file():
        raise RuntimeError("No ROM in this machine. Restart the launcher, or pass it --rom")
    env = JoypadSpace(gym.make(args.env, render_mode="rgb_array"), MOVEMENT)
    stage_name = args.env.removeprefix("SuperMarioBros-").removesuffix("-v0")
    parser = make_parser(
        goal=f"Clear World {stage_name} without dying.",
        decision_horizon_frames=args.frames_per_decision,
    )
    policy, client = make_policy(args.policy)
    frame, info = env.reset(seed=args.seed)
    terminated = truncated = False
    history = deque(maxlen=16)
    timeline, control = "initial", {"avoid": [], "force": None}
    frame_index = decision_index = checkpoint_seq = 0
    next_checkpoint = 0
    previous_action = None
    last_heartbeat, last_decision = {}, {}
    timeline_tokens = timeline_decisions = 0

    def publish(phase, **extra):
        nonlocal last_heartbeat
        last_heartbeat = {
            "timeline": timeline,
            "stage": f"{snapshot.world}-{snapshot.stage}",
            "phase": phase,
            "frame": frame_index,
            "decision": decision_index,
            "checkpoint_seq": checkpoint_seq,
            "x_pos": snapshot.x,
            "y_pos": snapshot.y,
            "area": snapshot.area,
            "coins": snapshot.coins,
            "score": snapshot.score,
            "powerup_status": snapshot.status,
            "action": previous_action,
            "history": list(history),
            "policy": args.policy,
            "ts": time.time(),
            "force_pending": control.get("force"),
            "input_tokens": timeline_tokens,
            "timeline_decisions": timeline_decisions,
            **last_decision,
            **extra,
        }
        atomic_json(root / "state.json", last_heartbeat)
        return last_heartbeat

    def save_frame():
        if frame_index % 2 == 0:
            path = root / "timelines" / timeline / "frames" / f"{frame_index:08d}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(frame).save(path, optimize=False)
            latest = root / "latest.tmp.png"
            Image.fromarray(frame).save(latest, optimize=False)
            latest.replace(root / "latest.png")

    if client:
        client.should_stop = lambda: (root / "stop.json").exists()
        client.on_retry = lambda attempt: publish("api_retry", api_retry=attempt)
    snapshot = parser.parse(info, env.unwrapped.ram, previous_response_delay_frames=0)
    try:
        while True:
            if (root / "stop.json").exists():
                raise GuestStopped
            if snapshot.clear or info.get("flag_get"):
                publish("clear")
                break
            if snapshot.dead or info.get("is_dying") or info.get("is_dead"):
                # Keep the first fatal position/history while recording the animation.
                publish("dying")
                for _ in range(90):
                    # Gym rejects steps after termination; some deaths terminate
                    # before the optional animation has finished.
                    if terminated or truncated or info.get("is_dead") or info.get("is_game_over"):
                        break
                    frame, _, terminated, truncated, info = env.step(ACTION_TO_INDEX[Action.NOOP])
                    frame_index += 1
                    save_frame()
                    if terminated or truncated:
                        break
                publish("dead")
                break
            if decision_index >= args.max_decisions:
                publish("limit", reason="max_decisions")
                break
            if frame_index >= next_checkpoint and (
                not control.get("force") or control["force"].get("status") != "active"
            ):
                checkpoint_seq += 1
                control = Gate(root).wait(publish("preparing_gate"))
                if timeline != control["timeline"]:
                    timeline_tokens = timeline_decisions = 0
                timeline = control["timeline"]
                directory = root / "timelines" / timeline
                directory.mkdir(parents=True, exist_ok=True)
                save_frame()
                publish("playing")
                next_checkpoint = frame_index + args.checkpoint_frames

            observation = DecisionContext(
                snapshot, control["avoid"], ram=env.unwrapped.ram, info=info, recent_actions=history
            )
            state = observation.to_state()
            allowed = allowed_actions(state, control["avoid"])
            force = control.get("force")
            chunk = sequence_chunk(force, state, args.frames_per_decision)
            if chunk and interrupt_sequence(force, state, chunk[0]):
                chunk = None
            decision = None
            if chunk:
                # Execute a bounded experiment without querying Jev for moves we ignore.
                applied, chunk_frames = chunk
                proposed, forced = None, True
                publish("experiment")
            else:
                publish("deciding")
                decision = policy.choose(
                    observation if args.policy == "typesafe" else snapshot,
                    tuple(Action(action) for action in allowed),
                )
                timeline_tokens += client.input_tokens if client else 0
                timeline_decisions += 1
                proposed = decision.action.value
                applied, forced = proposed, False
                chunk_frames = (
                    action_frames(state, applied, args.frames_per_decision)
                    if args.policy == "typesafe"
                    else args.frames_per_decision
                )
                if (
                    force
                    and "steps" not in force
                    and force["x_min"] <= snapshot.x <= force["x_max"]
                ):
                    applied, forced = force["action"], True
                    chunk_frames = args.frames_per_decision
                    control["force"] = None  # Legacy one-macro probe support.

            started_frame, total_reward = frame_index, 0.0
            action = Action(applied)
            extended_retreat = not forced and applied == "left" and chunk_frames > 8
            stationary_frames, last_x = 0, snapshot.x
            for offset in range(chunk_frames):
                # A fresh grounded jump needs a release edge if A was already held.
                step_action = action
                if (
                    offset == 0
                    and (
                        not chunk
                        or force.get("remaining") == force["steps"][force["step_index"]]["frames"]
                    )
                    and snapshot.grounded
                    and action in JUMP_ACTIONS
                    and previous_action in {item.value for item in JUMP_ACTIONS}
                ):
                    step_action = JUMP_RELEASE_ACTION[action]
                frame, reward, terminated, truncated, info = env.step(ACTION_TO_INDEX[step_action])
                frame_index += 1
                total_reward += float(reward)
                save_frame()
                # The parser computes velocities and airborne duration from successive
                # observations. Feed every frame, even though Jev acts once per macro.
                next_snapshot = parser.parse(
                    info,
                    env.unwrapped.ram,
                    previous_action=step_action.value,
                    previous_reward=float(reward),
                    previous_latency_ms=decision.latency_ms if decision else 0,
                    previous_response_delay_frames=0,
                )
                if (root / "stop.json").exists():
                    raise GuestStopped
                stationary_frames = stationary_frames + 1 if next_snapshot.x == last_x else 0
                last_x = next_snapshot.x
                if chunk:
                    consume_sequence(force, 1)
                    if force["status"] == "active" and interrupt_sequence(
                        force, next_snapshot.to_state(), applied
                    ):
                        break
                if (
                    terminated
                    or truncated
                    or info.get("is_dying")
                    or info.get("is_dead")
                    or info.get("flag_get")
                ):
                    break
                if extended_retreat and (
                    stationary_frames >= 8
                    or retreat_finished(snapshot, next_snapshot, env.unwrapped.ram)
                ):
                    break

            row = {
                "timeline": timeline,
                "decision": decision_index,
                "frame": started_frame,
                "end_frame": frame_index,
                "x_pos": snapshot.x,
                "y_pos": snapshot.y,
                "end_x": next_snapshot.x,
                "end_y": next_snapshot.y,
                "end_grounded": next_snapshot.grounded,
                "requested_frames": chunk_frames,
                "grounded": snapshot.grounded,
                "context": context_key(state),
                "state": state,
                "proposed_action": proposed,
                "action": applied,
                "forced": forced,
                "experiment": {k: force.get(k) for k in ("experiment_id", "label", "status")}
                if force and "steps" in force
                else None,
                "allowed": allowed,
                "probabilities": dict(decision.probabilities) if decision else {},
                "confidence": decision.confidence if decision else None,
                "latency_ms": decision.latency_ms if decision else None,
                "input_tokens": client.input_tokens if client and decision else 0,
                "danger_score": decision.danger_score if decision else None,
                "jump_needed_probability": decision.jump_needed_probability if decision else None,
                "reward": total_reward,
                "coins_before": snapshot.coins,
                "coins_after": next_snapshot.coins,
                "score_before": snapshot.score,
                "score_after": next_snapshot.score,
                "policy": args.policy,
            }
            append_json(root / "timelines" / timeline / "run.jsonl", row)
            history.append(
                {
                    key: row[key]
                    for key in (
                        "frame",
                        "x_pos",
                        "end_x",
                        "end_y",
                        "y_pos",
                        "end_grounded",
                        "grounded",
                        "context",
                        "action",
                        "forced",
                        "coins_before",
                        "coins_after",
                        "score_before",
                        "score_after",
                    )
                }
            )
            previous_action = applied
            last_decision = {
                "probabilities": dict(decision.probabilities) if decision else {},
                "confidence": decision.confidence if decision else None,
                "latency_ms": decision.latency_ms if decision else None,
                "proposed_action": proposed,
                "forced": forced,
                "experiment": {k: force.get(k) for k in ("experiment_id", "label", "status")}
                if force and "steps" in force
                else None,
            }
            decision_index += 1
            snapshot = next_snapshot
            publish("playing", latency_ms=decision.latency_ms if decision else None)
            if (terminated or truncated) and not (
                info.get("is_dead") or info.get("is_dying") or info.get("flag_get")
            ):
                publish("limit", reason="environment_terminated")
                break
    except GuestStopped:
        atomic_json(root / "state.json", {**last_heartbeat, "phase": "halted"})
    except Exception as error:
        # Avoid putting SDK exception strings in public artifacts: they may contain
        # request details. The exception class is sufficient for the host to abort.
        atomic_json(
            root / "state.json",
            {
                **last_heartbeat,
                "phase": "error",
                "error": type(error).__name__,
                "error_location": [
                    f"{Path(item.filename).name}:{item.lineno}:{item.name}"
                    for item in traceback.extract_tb(error.__traceback__)
                ],
            },
        )
        raise RuntimeError(f"Guest failed: {type(error).__name__}") from None
    finally:
        env.close()
        close = getattr(policy, "close", None)
        if close:
            close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/var/mnd"))
    parser.add_argument("--env", default="SuperMarioBros-1-1-v0")
    parser.add_argument("--policy", choices=("typesafe", "heuristic"), default="typesafe")
    parser.add_argument("--frames-per-decision", type=int, default=8)
    parser.add_argument("--checkpoint-frames", type=int, default=150)
    parser.add_argument("--max-decisions", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=123)
    args = parser.parse_args()
    if min(args.frames_per_decision, args.checkpoint_frames, args.max_decisions) < 1:
        parser.error("frame and decision limits must be positive")
    run(args)


if __name__ == "__main__":
    main()

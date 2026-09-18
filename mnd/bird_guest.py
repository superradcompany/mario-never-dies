"""Headless Flappy worker. A live VM branch resumes this process: the same bird, the same
pipes ahead, a different choice. Speaks the same files as the Mario guest, so the
controller, the recorder and the replay treat both games alike."""

from __future__ import annotations

import argparse
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .bird import ACTIONS, AIM, FRAMES_PER_DECISION, Game, heuristic, validate_sequence
from .protocol import Gate, GuestStopped, append_json, atomic_json
from .recovery import consume_sequence, sequence_chunk


@dataclass
class Decision:
    action: str
    confidence: float
    probabilities: dict
    latency_ms: float
    danger_score: float | None = None


class HeuristicPolicy:
    def choose(self, game: Game, state: dict) -> Decision:
        action = heuristic(game)
        return Decision(action, 1.0, {action: 1.0}, 0.0)


def jev_policy():
    """Jev answers one typed question per decision: flap, or glide for four frames."""
    from typesafe_mario.policy import TypeSafePolicy

    from .guest import PerDecisionClient

    class BirdPolicy(TypeSafePolicy):
        def choose(self, game: Game, state: dict) -> Decision:
            def reads(action, move):
                view = state["outlook"][action]
                if view["survivable"]:
                    return (
                        f"{move} Outlook: SAFE. A way through the next frames stays open, "
                        f"ending {view['ends_from_aim']} px from the line to steer for."
                    )
                return (
                    f"{move} Outlook: CRASH into {view['crashes_into']} within "
                    f"{view['frames_until_crash']} frames, whatever comes after."
                )

            questions = {
                "next_action": self._Choice(
                    criteria={
                        "flap": reads("flap", "Tap now: the bird jumps about 45 px upward."),
                        "glide": reads("glide", "Do nothing for 6 frames: the bird falls."),
                    },
                    instructions={
                        "question": "Flap now, or glide for the next 6 frames?",
                        "priority": (
                            "1. Pick a choice whose outlook is SAFE over one that is CRASH. "
                            "2. When both are SAFE, pick the one that ends nearer steer_for_y "
                            "(the smaller ends_from_aim). 3. When both are CRASH, pick the "
                            "later crash (the larger frames_until_crash)."
                        ),
                        "how_to_read": (
                            "outlook.flap and outlook.glide were flown 18 frames ahead on "
                            "the game's own physics: survivable means some sequence of flaps "
                            "after this choice still gets through that far."
                        ),
                    },
                ),
                "danger": self._Score(
                    instructions="How close is the bird to hitting a pipe or the ground?",
                    criteria=[
                        "Comfortably on a safe path",
                        "Needs a correction soon",
                        "About to hit a pipe or the ground",
                    ],
                ),
            }
            started = time.perf_counter()
            response = self._client.system_one(state=state, questions=questions)
            choice = self._answer(response, "next_action", "choices")
            action = str(choice.choice)
            if action not in ACTIONS:
                raise ValueError("Jev selected an action that was not offered")
            return Decision(
                action=action,
                confidence=float(choice.confidence),
                probabilities={str(k): float(v) for k, v in dict(choice.probabilities).items()},
                latency_ms=(time.perf_counter() - started) * 1000,
                danger_score=float(self._answer(response, "danger", "scores").score),
            )

    policy = BirdPolicy()
    policy.close()
    client = PerDecisionClient(enrich=None)
    policy._client = client
    return policy, client


def run(args):
    root = args.root
    root.mkdir(parents=True, exist_ok=True)
    game = Game(seed=args.seed)
    policy, client = (HeuristicPolicy(), None) if args.policy == "heuristic" else jev_policy()
    history = deque(maxlen=16)
    timeline, control = "initial", {"avoid": [], "force": None}
    decision_index = checkpoint_seq = 0
    gate_score = -1  # a new copy is offered at the start and after every pipe
    previous_action = None
    last_heartbeat, last_decision = {}, {}
    timeline_tokens = timeline_decisions = 0

    def publish(phase, **extra):
        nonlocal last_heartbeat
        last_heartbeat = {
            "timeline": timeline,
            "game": "bird",
            "stage": str(args.target),
            "phase": phase,
            "frame": game.frame,
            "decision": decision_index,
            "checkpoint_seq": checkpoint_seq,
            "x_pos": game.x_pos,
            "y_pos": int(game.y),
            "area": 1,
            "coins": 0,
            "score": game.score,
            "powerup_status": "small",
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
        image = game.render()
        path = root / "timelines" / timeline / "frames" / f"{game.frame:08d}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, optimize=False)
        latest = root / "latest.tmp.png"
        image.save(latest, optimize=False)
        latest.replace(root / "latest.png")

    if client:
        client.should_stop = lambda: (root / "stop.json").exists()
        client.on_retry = lambda attempt: publish("api_retry", api_retry=attempt)
    try:
        while True:
            if (root / "stop.json").exists():
                raise GuestStopped
            if game.score >= args.target:
                publish("clear")
                break
            if game.dead:
                publish("dying")
                for _ in range(60):
                    falling = game.fall()
                    save_frame()
                    if not falling:
                        break
                publish("dead")
                break
            if decision_index >= args.max_decisions:
                publish("limit", reason="max_decisions")
                break
            force = control.get("force")
            if game.score != gate_score and not (force and force.get("status") == "active"):
                gate_score = game.score
                checkpoint_seq += 1
                # A cleared pipe is a milestone: a trial that reaches this gate has beaten
                # the pipe that killed its parent, and the copy is taken right here.
                control = Gate(root, validate=validate_sequence).wait(
                    publish("preparing_gate", milestone=game.score > 0)
                )
                if timeline != control["timeline"]:
                    timeline_tokens = timeline_decisions = 0
                timeline = control["timeline"]
                (root / "timelines" / timeline).mkdir(parents=True, exist_ok=True)
                save_frame()
                publish("playing")
                force = control.get("force")

            chunk = sequence_chunk(force, {"player": {"x": game.x_pos}}, args.frames_per_decision)
            # A verse steers for its own part of the gap until it is released from its next
            # gate without one; the trunk steers for the usual line.
            seen = game.observe(force["aim"] if force and "aim" in force else AIM)
            decision = None
            if chunk:
                # A fork's scripted opening: no model call for moves we would ignore.
                applied, chunk_frames = chunk
                proposed, forced = None, True
                publish("experiment")
            else:
                publish("deciding")
                decision = policy.choose(game, seen)
                timeline_tokens += (client.input_tokens or 0) if client else 0
                timeline_decisions += 1
                proposed = applied = decision.action
                forced, chunk_frames = False, args.frames_per_decision

            started_frame, start_x, start_y = game.frame, game.x_pos, int(game.y)
            score_before = game.score
            for offset in range(chunk_frames):
                alive = game.step(applied == "flap" and offset == 0)
                save_frame()
                if chunk:
                    consume_sequence(force, 1)
                if (root / "stop.json").exists():
                    raise GuestStopped
                if not alive:
                    break

            experiment = (
                {k: force.get(k) for k in ("experiment_id", "label", "status")}
                if force and "steps" in force
                else None
            )
            row = {
                "timeline": timeline,
                "decision": decision_index,
                "frame": started_frame,
                "end_frame": game.frame,
                "x_pos": start_x,
                "y_pos": start_y,
                "end_x": game.x_pos,
                "end_y": int(game.y),
                "grounded": False,
                "end_grounded": False,
                "requested_frames": chunk_frames,
                "context": f"bird:{game.score}",
                "state": seen,
                "proposed_action": proposed,
                "action": applied,
                "forced": forced,
                "experiment": experiment,
                "allowed": list(ACTIONS),
                "probabilities": dict(decision.probabilities) if decision else {},
                "confidence": decision.confidence if decision else None,
                "latency_ms": decision.latency_ms if decision else None,
                "input_tokens": (client.input_tokens or 0) if client and decision else 0,
                "danger_score": decision.danger_score if decision else None,
                "score_before": score_before,
                "score_after": game.score,
                "policy": args.policy,
            }
            append_json(root / "timelines" / timeline / "run.jsonl", row)
            keys = ("frame", "x_pos", "end_x", "end_y", "y_pos", "grounded", "end_grounded")
            history.append({key: row[key] for key in (*keys, "context", "action", "forced")})
            previous_action = applied
            last_decision = {
                "probabilities": row["probabilities"],
                "confidence": row["confidence"],
                "latency_ms": row["latency_ms"],
                "proposed_action": proposed,
                "forced": forced,
                "experiment": experiment,
            }
            decision_index += 1
            publish("playing", latency_ms=row["latency_ms"])
    except GuestStopped:
        atomic_json(root / "state.json", {**last_heartbeat, "phase": "halted"})
    except Exception as error:
        # SDK exception strings may carry request details; the class name is enough.
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
        close = getattr(policy, "close", None)
        if close:
            close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/var/mnd"))
    parser.add_argument("--target", type=int, default=25, help="pipes to pass for a clear")
    parser.add_argument("--policy", choices=("typesafe", "heuristic"), default="typesafe")
    parser.add_argument("--frames-per-decision", type=int, default=FRAMES_PER_DECISION)
    parser.add_argument("--checkpoint-frames", type=int, default=0, help="unused: a copy per pipe")
    parser.add_argument("--max-decisions", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=123)
    args = parser.parse_args()
    if min(args.target, args.frames_per_decision, args.max_decisions) < 1:
        parser.error("target, frame and decision limits must be positive")
    run(args)


if __name__ == "__main__":
    main()

"""Bounded Jev gameplay evaluation inside the guest image; no rewinds or hidden routes."""

import argparse
import json
import time
from collections import deque
from pathlib import Path

import gym_super_mario_bros  # noqa: F401
import gymnasium as gym
from nes_py.wrappers import JoypadSpace
from typesafe_mario.state import MarioStateParser

from mnd.actions import ACTION_TO_INDEX, JUMP_ACTIONS, JUMP_RELEASE_ACTION, MOVEMENT, Action
from mnd.guest import make_policy
from mnd.perception import DecisionContext, action_frames, make_parser, retreat_finished


def trial(stage, variant, limit, seed, output):
    env = JoypadSpace(gym.make(f"SuperMarioBros-{stage}-v0", render_mode="rgb_array"), MOVEMENT)
    parser_factory = MarioStateParser if variant == "baseline" else make_parser
    parser = parser_factory(goal=f"Clear World {stage} without dying.", decision_horizon_frames=8)
    policy, client = make_policy("baseline" if variant == "baseline" else "typesafe")
    frame, info = env.reset(seed=seed)
    snapshot = parser.parse(info, env.unwrapped.ram, previous_response_delay_frames=0)
    previous = None
    history = deque(maxlen=8)
    tokens = calls = frames = collected = stalls = damage = 0
    started = time.monotonic()
    outcome = "budget"
    trace = output / f"{variant}-{stage}-{seed}.jsonl"
    try:
        for index in range(limit):
            observation = (
                snapshot
                if variant == "baseline"
                else DecisionContext(
                    snapshot, ram=env.unwrapped.ram, info=info, recent_actions=history
                )
            )
            # The production request adapter enriches instructions only when this state
            # explicitly identifies the new context; baseline uses the original prompt.
            decision = policy.choose(
                observation, tuple(Action)[:7] if variant == "baseline" else tuple(Action)
            )
            calls += 1
            tokens += client.input_tokens or 0
            action = decision.action
            before = snapshot
            release = snapshot.grounded and action in JUMP_ACTIONS and previous in JUMP_ACTIONS
            duration = action_frames(observation.to_state(), action.value)
            for offset in range(duration):
                step_action = JUMP_RELEASE_ACTION[action] if offset == 0 and release else action
                frame, reward, terminated, truncated, info = env.step(ACTION_TO_INDEX[step_action])
                frames += 1
                snapshot = parser.parse(
                    info,
                    env.unwrapped.ram,
                    previous_action=step_action.value,
                    previous_reward=reward,
                    previous_response_delay_frames=0,
                )
                if terminated or truncated or snapshot.dead or snapshot.clear:
                    break
                if duration > 8 and retreat_finished(before, snapshot, env.unwrapped.ram):
                    break
            collected += (snapshot.coins - before.coins) % 100
            damage += int(before.status != "small" and snapshot.status == "small")
            stalls += int(snapshot.x <= before.x)
            row = {
                "decision": index,
                "frame": frames,
                "x": snapshot.x,
                "y": snapshot.y,
                "action": action.value,
                "coins": collected,
                "score": snapshot.score,
                "powerup": snapshot.status,
                "state": observation.to_state(),
                "requested_frames": duration,
            }
            history.append(
                {
                    "action": action.value,
                    "x_pos": before.x,
                    "end_x": snapshot.x,
                    "y_pos": before.y,
                    "end_y": snapshot.y,
                    "grounded": before.grounded,
                    "end_grounded": snapshot.grounded,
                    "coins_before": before.coins,
                    "coins_after": snapshot.coins,
                    "score_before": before.score,
                    "score_after": snapshot.score,
                }
            )
            with trace.open("a") as stream:
                stream.write(json.dumps(row) + "\n")
            previous = action
            if snapshot.dead or info.get("is_dying") or info.get("is_dead"):
                outcome = "dead"
                break
            if snapshot.clear or info.get("flag_get"):
                outcome = "clear"
                break
            if terminated or truncated:
                outcome = "terminated"
                break
    except Exception as error:
        outcome = type(error).__name__
    finally:
        env.close()
        policy.close()
    return {
        "stage": stage,
        "variant": variant,
        "seed": seed,
        "outcome": outcome,
        "x": snapshot.x,
        "best_x": snapshot.best_progress,
        "coins": collected,
        "score": snapshot.score,
        "powerup": snapshot.status,
        "damage": damage,
        "stalled_decisions": stalls,
        "decisions": calls,
        "frames": frames,
        "tokens": tokens,
        "seconds": round(time.monotonic() - started, 2),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("baseline", "enhanced"), required=True)
    parser.add_argument("--stages", nargs="+", default=["1-1", "1-2", "1-3", "1-4"])
    parser.add_argument("--decisions", type=int, default=100)
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--output", type=Path, default=Path("/results"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for stage in args.stages:
        result = trial(stage, args.variant, args.decisions, args.seed, args.output)
        results.append(result)
        print(json.dumps(result), flush=True)
        (args.output / f"{args.variant}-{args.seed}.json").write_text(
            json.dumps(results, indent=2) + "\n"
        )


if __name__ == "__main__":
    main()

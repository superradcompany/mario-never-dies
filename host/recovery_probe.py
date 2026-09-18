"""Run inside the Mario image: replay the recorded 1-2 checkpoint and compare recovery."""

import argparse
import copy
import json
import sys
import time
from pathlib import Path

import gym_super_mario_bros  # noqa: F401
import gymnasium as gym
from nes_py.wrappers import JoypadSpace
from typesafe_mario.state import MarioStateParser

from mnd.actions import ACTION_TO_INDEX, JUMP_ACTIONS, JUMP_RELEASE_ACTION, MOVEMENT, Action
from mnd.guest import make_policy
from mnd.protocol import CANDIDATES
from mnd.recovery import consume_sequence, experiments, interrupt_sequence, sequence_chunk


def trial(fixture, policy_mode, plan=None, recorded=None, legacy=None):
    env = JoypadSpace(gym.make(fixture["env"], render_mode="rgb_array"), MOVEMENT)
    parser = MarioStateParser(goal="Clear World 1-2 without dying.", decision_horizon_frames=8)
    _, info = env.reset(seed=fixture["seed"])
    snapshot = parser.parse(info, env.unwrapped.ram, previous_response_delay_frames=0)
    previous = None
    calls = tokens = frames = 0
    terminated = truncated = False
    policy, client = make_policy(policy_mode)
    started = time.monotonic()

    def step(action):
        nonlocal snapshot, info, terminated, truncated, frames
        _, reward, terminated, truncated, info = env.step(ACTION_TO_INDEX[Action(action)])
        frames += 1
        snapshot = parser.parse(
            info,
            env.unwrapped.ram,
            previous_action=action,
            previous_reward=reward,
            previous_response_delay_frames=0,
        )

    try:
        for row in fixture["prefix"]:
            for offset in range(row["frames"]):
                action = Action(row["action"])
                if offset == 0 and row["release_jump"]:
                    action = JUMP_RELEASE_ACTION[action]
                step(action.value)
            previous = row["action"]
        assert frames == fixture["checkpoint_frame"] and snapshot.x == fixture["checkpoint_x"]
        started = time.monotonic()
        start_frames = frames
        if recorded:
            for row in recorded:
                for offset in range(row["frames"]):
                    action = Action(row["action"])
                    if offset == 0 and row["release_jump"]:
                        action = JUMP_RELEASE_ACTION[action]
                    step(action.value)
                    if terminated or truncated:
                        break
                if terminated or truncated:
                    break
        else:
            for _ in range(100):
                if terminated or truncated or info.get("is_dying") or info.get("is_dead"):
                    break
                if snapshot.x > fixture["death_x"] + 64 and (
                    not plan or plan["status"] not in {"waiting", "active"}
                ):
                    break
                chunk = sequence_chunk(plan, snapshot.to_state(), 8)
                if chunk and interrupt_sequence(plan, snapshot.to_state(), chunk[0]):
                    chunk = None
                if chunk:
                    applied, count = chunk
                else:
                    decision = policy.choose(snapshot, tuple(Action))
                    calls += 1
                    tokens += client.input_tokens if client else 0
                    applied, count = decision.action.value, 8
                    if legacy and fixture["death_x"] - 64 <= snapshot.x <= fixture["death_x"] + 16:
                        applied, legacy = legacy, None
                release = (
                    snapshot.grounded
                    and Action(applied) in JUMP_ACTIONS
                    and previous
                    and "jump" in previous
                    and (
                        not chunk
                        or plan["remaining"] == plan["steps"][plan["step_index"]]["frames"]
                    )
                )
                for offset in range(count):
                    action = Action(applied)
                    if offset == 0 and release:
                        action = JUMP_RELEASE_ACTION[action]
                    step(action.value)
                    if chunk:
                        consume_sequence(plan, 1)
                        if plan["status"] == "active" and interrupt_sequence(
                            plan, snapshot.to_state(), applied
                        ):
                            break
                    if terminated or truncated:
                        break
                previous = applied
        dead = bool(info.get("is_dying") or info.get("is_dead"))
        return {
            "x": snapshot.x,
            "dead": dead,
            "passed_hazard": not dead and snapshot.x > fixture["death_x"] + 64,
            "model_calls": calls,
            "input_tokens": tokens,
            "frames": frames - start_frames,
            "wall_seconds": round(time.monotonic() - started, 3),
            "experiment": plan,
        }
    finally:
        env.close()
        policy.close() if hasattr(policy, "close") else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=Path("/fixtures/world1-2-x906.json"))
    parser.add_argument("--policy", choices=("heuristic", "typesafe"), default="heuristic")
    parser.add_argument("--count", type=int, default=4)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    report = {"policy": args.policy, "recorded_baseline": [], "legacy": [], "sequences": []}
    for name, tail in fixture["baseline_tails"].items():
        result = trial(fixture, "heuristic", recorded=tail)
        report["recorded_baseline"].append({"name": name, **result})
        assert result["dead"] and abs(result["x"] - fixture["death_x"]) <= 1
    for action in CANDIDATES:
        report["legacy"].append({"action": action, **trial(fixture, args.policy, legacy=action)})
    for plan in experiments(fixture["checkpoint_x"], fixture["death_x"])[: args.count]:
        report["sequences"].append(trial(fixture, args.policy, plan=copy.deepcopy(plan)))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Probe failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None

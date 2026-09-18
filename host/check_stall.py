"""Reproduce a recorded stall; optionally ask real Jev to escape it.

The prefix is test setup only. Production never loads this fixture or route.
"""

import argparse
import json
from collections import deque
from pathlib import Path

import gym_super_mario_bros  # noqa: F401
import gymnasium as gym
from nes_py.wrappers import JoypadSpace

from mnd.actions import ACTION_TO_INDEX, JUMP_ACTIONS, JUMP_RELEASE_ACTION, MOVEMENT, Action
from mnd.guest import make_policy
from mnd.perception import DecisionContext, action_frames, make_parser, retreat_finished


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--jev", action="store_true", help="Use API calls after test setup")
    args = arguments.parse_args()
    fixture = json.loads(Path("/fixtures/stall-retreat.json").read_text())
    env = JoypadSpace(gym.make(fixture["env"]), MOVEMENT)
    parser = make_parser()
    _, info = env.reset(seed=fixture["seed"])
    snapshot = parser.parse(info, env.unwrapped.ram)
    previous, policy = None, None
    history = deque(maxlen=8)

    def step(action, duration=8):
        nonlocal snapshot, info, previous
        before = snapshot
        release = before.grounded and action in JUMP_ACTIONS and previous in JUMP_ACTIONS
        for offset in range(duration):
            move = JUMP_RELEASE_ACTION[action] if offset == 0 and release else action
            _, _, terminated, truncated, info = env.step(ACTION_TO_INDEX[move])
            snapshot = parser.parse(info, env.unwrapped.ram, previous_action=move.value)
            assert not terminated and not truncated and not snapshot.dead
            if duration > 8 and retreat_finished(before, snapshot, env.unwrapped.ram):
                break
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
        previous = action
        return offset + 1

    try:
        for action in fixture["prefix"]:
            step(Action(action))
        assert snapshot.x == fixture["expected_x"] and snapshot.grounded
        start_y = snapshot.y
        context = DecisionContext(
            snapshot, ram=env.unwrapped.ram, info=info, recent_actions=history
        )
        assert action_frames(context.to_state(), "left") == 64
        if args.jev:
            policy, _ = make_policy("typesafe")
        for index in range(30):
            context = DecisionContext(
                snapshot, ram=env.unwrapped.ram, info=info, recent_actions=history
            )
            # Without --jev, isolate controller physics from model variability.
            action = (
                policy.choose(context, tuple(Action)).action
                if policy
                else (Action.LEFT if index == 0 else Action.RIGHT)
            )
            duration = action_frames(context.to_state(), action.value)
            actual_frames = step(action, duration)
            print(
                json.dumps(
                    {
                        "decision": index,
                        "action": action.value,
                        "requested_frames": duration,
                        "actual_frames": actual_frames,
                        "x": snapshot.x,
                        "y": snapshot.y,
                        "coins": snapshot.coins,
                    }
                ),
                flush=True,
            )
            if snapshot.x > fixture["expected_x"] + 64:
                break
        assert snapshot.x > fixture["expected_x"] + 64
        assert snapshot.y < start_y and snapshot.coins >= fixture["expected_coins"]
        print(json.dumps({"passed": True, "jev": args.jev, "x": snapshot.x}), flush=True)
    finally:
        if policy:
            policy.close()
        env.close()


if __name__ == "__main__":
    main()

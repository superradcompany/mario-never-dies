"""Reproduce the pipe trap with recorded inputs, then test real controller choices."""

import argparse
import json
from collections import deque
from pathlib import Path

import gym_super_mario_bros  # noqa: F401
import gymnasium as gym
from gym_super_mario_bros.actions import COMPLEX_MOVEMENT
from nes_py.wrappers import JoypadSpace

from mnd.actions import ACTION_TO_INDEX, JUMP_ACTIONS, JUMP_RELEASE_ACTION, MOVEMENT, Action
from mnd.guest import make_policy
from mnd.perception import DecisionContext, action_frames, make_parser, retreat_finished


def main():
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--jev", action="store_true")
    options.add_argument(
        "--pipe", action="store_true", help="Start on the roof above a visible entrance"
    )
    args = options.parse_args()
    assert MOVEMENT == COMPLEX_MOVEMENT
    fixture = json.loads(Path("/fixtures/pipe-trap.json").read_text())
    env = JoypadSpace(gym.make(fixture["env"]), MOVEMENT)
    parser = make_parser()
    _, info = env.reset(seed=fixture["seed"])
    snapshot = parser.parse(info, env.unwrapped.ram)
    history, previous = deque(maxlen=8), None
    policy = None

    def step(action, duration, release=None):
        nonlocal snapshot, info, previous
        before = snapshot
        production = release is None
        if release is None:
            release = snapshot.grounded and action in JUMP_ACTIONS and previous in JUMP_ACTIONS
        still, last_x = 0, before.x
        for offset in range(duration):
            move = JUMP_RELEASE_ACTION[action] if offset == 0 and release else action
            _, _, terminated, truncated, info = env.step(ACTION_TO_INDEX[move])
            snapshot = parser.parse(info, env.unwrapped.ram, previous_action=move.value)
            if snapshot.clear or info.get("flag_get"):
                break
            if terminated or truncated or snapshot.dead or info.get("is_dying"):
                raise AssertionError("Test ended before escaping")
            still = still + 1 if snapshot.x == last_x else 0
            last_x = snapshot.x
            if (
                production
                and action == Action.LEFT
                and duration > 8
                and (still >= 8 or retreat_finished(before, snapshot, env.unwrapped.ram))
            ):
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

    try:
        for row in fixture["prefix"]:
            step(Action(row["action"]), row["frames"], row["release_jump"])
            if args.pipe and 2600 < snapshot.x < 2650:
                break
        if not args.pipe:
            assert (snapshot.x, snapshot.y) == (fixture["expected_x"], fixture["expected_y"])
        print(json.dumps({"start_x": snapshot.x, "start_y": snapshot.y}), flush=True)
        start_x, start_y = snapshot.x, snapshot.y
        if args.jev:
            policy, _ = make_policy("typesafe")
        for index in range(80 if args.pipe else 40):
            context = DecisionContext(
                snapshot, ram=env.unwrapped.ram, info=info, recent_actions=history
            )
            action = policy.choose(context, tuple(Action)).action if policy else Action.LEFT_JUMP
            step(action, action_frames(context.to_state(), action.value))
            print(
                json.dumps(
                    {
                        "i": index,
                        "action": action.value,
                        "x": snapshot.x,
                        "y": snapshot.y,
                        "coins": snapshot.coins,
                        "world": snapshot.world,
                        "stage": snapshot.stage,
                    }
                ),
                flush=True,
            )
            if not args.pipe and snapshot.x < start_x - 48:
                break
            if args.pipe and snapshot.clear:
                break
        if not args.pipe:
            assert snapshot.x < start_x - 48, "Did not jump back over the pipe"
            assert snapshot.y > start_y, "Did not leave the trapped floor"
        else:
            assert snapshot.clear, "Did not clear the stage through the ordinary exit"
            assert (snapshot.world, snapshot.stage) == (1, 2), "Took a warp to a different stage"
        print(json.dumps({"passed": True, "jev": args.jev, "side_pipe": args.pipe}), flush=True)
    finally:
        if policy:
            policy.close()
        env.close()


if __name__ == "__main__":
    main()

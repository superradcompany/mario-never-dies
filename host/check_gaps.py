"""Replay the recorded 1-3 approach, then cross visible platforms with fresh decisions."""

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
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--jev", action="store_true")
    cli.add_argument("--full", action="store_true", help="Fresh full 1-3 attempt, no prefix")
    args = cli.parse_args()
    fixture = json.loads(Path("/fixtures/world1-3-gap.json").read_text())
    env = JoypadSpace(gym.make(fixture["env"]), MOVEMENT)
    parser = make_parser()
    _, info = env.reset(seed=fixture["seed"])
    snapshot = parser.parse(info, env.unwrapped.ram)
    previous, policy, dead, frame = None, None, False, 0
    history = deque(maxlen=8)

    def step(action, count, release=None):
        nonlocal snapshot, info, previous, dead, frame
        before = snapshot
        if release is None:
            release = snapshot.grounded and action in JUMP_ACTIONS and previous in JUMP_ACTIONS
        still, last_x = 0, before.x
        for offset in range(count):
            move = JUMP_RELEASE_ACTION[action] if offset == 0 and release else action
            _, _, ended, truncated, info = env.step(ACTION_TO_INDEX[move])
            frame += 1
            snapshot = parser.parse(info, env.unwrapped.ram, previous_action=move.value)
            dead = bool(snapshot.dead or info.get("is_dying"))
            if ended or truncated or dead or snapshot.clear:
                break
            still = still + 1 if snapshot.x == last_x else 0
            last_x = snapshot.x
            if (
                action == Action.LEFT
                and count > 8
                and (still >= 8 or retreat_finished(before, snapshot, env.unwrapped.ram))
            ):
                break
        history.append(
            dict(
                action=action.value,
                x_pos=before.x,
                end_x=snapshot.x,
                y_pos=before.y,
                end_y=snapshot.y,
                grounded=before.grounded,
                end_grounded=snapshot.grounded,
                coins_before=before.coins,
                coins_after=snapshot.coins,
                score_before=before.score,
                score_after=snapshot.score,
            )
        )
        previous = action

    try:
        if not args.full:
            for row in fixture["prefix"]:
                step(Action(row["action"]), row["frames"], row["release_jump"])
                assert not dead
            assert snapshot.x == fixture["expected_x"]
        print(json.dumps({"start_x": snapshot.x}), flush=True)
        if args.jev:
            policy, _ = make_policy("typesafe")
        passed = False
        for index in range(350 if args.full else 80):
            context = DecisionContext(
                snapshot, ram=env.unwrapped.ram, info=info, recent_actions=history
            )
            state = context.to_state()
            if policy:
                action = policy.choose(context, tuple(Action)).action
            else:
                edge = (state.get("platforms") or {}).get("edge") or {}
                action = (
                    Action.LEFT
                    if (state.get("landing") or {}).get("brake_before_overshoot")
                    else Action.RIGHT_RUN_JUMP
                    if not snapshot.grounded or edge.get("jump_must_start_this_decision")
                    else Action.RIGHT_RUN
                )
            before_x = snapshot.x
            step(action, action_frames(state, action.value))
            print(
                json.dumps(
                    dict(
                        i=index,
                        frame=frame,
                        action=action.value,
                        before_x=before_x,
                        x=snapshot.x,
                        y=snapshot.y,
                        grounded=snapshot.grounded,
                        dead=dead,
                        coins=snapshot.coins,
                        score=snapshot.score,
                        clear=snapshot.clear,
                        platforms=state.get("platforms"),
                        terrain=state["terrain"],
                        landing=state.get("landing"),
                        input_tokens=policy._client.input_tokens if policy else None,
                    )
                ),
                flush=True,
            )
            if dead:
                break
            passed = snapshot.clear if args.full else snapshot.grounded and snapshot.x >= 400
            if passed:
                break
        print(
            json.dumps(
                {
                    "passed": passed,
                    "jev": args.jev,
                    "full": args.full,
                    "x": snapshot.x,
                    "clear": snapshot.clear,
                    "coins": snapshot.coins,
                }
            ),
            flush=True,
        )
        assert passed, (
            "Did not clear World 1-3"
            if args.full
            else "Did not safely land beyond the opening gaps"
        )
    finally:
        if policy:
            policy.close()
        env.close()


if __name__ == "__main__":
    main()

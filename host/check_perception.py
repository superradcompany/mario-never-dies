"""No-API emulator regressions from observed platform, collision, and gap failures."""

import json
from pathlib import Path

import gym_super_mario_bros  # noqa: F401
import gymnasium as gym
from nes_py.wrappers import JoypadSpace
from typesafe_mario.state import MarioStateParser

from mnd.actions import ACTION_TO_INDEX, JUMP_RELEASE_ACTION, MOVEMENT, Action
from mnd.perception import DecisionContext, make_parser

fixture = json.loads(Path("/fixtures/platform-edge.json").read_text())
env = JoypadSpace(gym.make(fixture["env"]), MOVEMENT)
old, new = MarioStateParser(), make_parser()
try:
    _, info = env.reset(seed=fixture["seed"])
    old.parse(info, env.unwrapped.ram)
    new.parse(info, env.unwrapped.ram)
    for row in fixture["prefix"]:
        for offset in range(row["frames"]):
            action = Action(row["action"])
            if offset == 0 and row["release_jump"]:
                action = JUMP_RELEASE_ACTION[action]
            _, reward, terminated, truncated, info = env.step(ACTION_TO_INDEX[action])
            a = old.parse(info, env.unwrapped.ram, previous_action=action.value)
            b = new.parse(info, env.unwrapped.ram, previous_action=action.value)
            assert not terminated and not truncated
    assert a.x == b.x == fixture["expected_x"]
    assert not a.grounded and b.grounded
    assert b.airborne_frames == 0 and b.jump_phase == "grounded"
    ram = bytearray(2048)
    ram[0x0F], ram[0x87], ram[0xCF] = 1, 100, 176
    ram[0x200] = 176
    enemies = new._extract_enemies({}, ram, 80, 176)
    assert len(enemies) == 1 and enemies[0].kind == "green_koopa"
    assert enemies[0].dx_pixels == 20
    print(
        json.dumps(
            {
                "x": b.x,
                "old_grounded": a.grounded,
                "new_grounded": b.grounded,
                "player_state_ram": int(env.unwrapped.ram[0x1D]),
                "passed": True,
            }
        )
    )
finally:
    env.close()

# At this observed walking-speed approach, another immediate jump falls short.
# The observation should call for a run-up; verify that the advised preparation
# actually clears the gap in the emulator. This fixture is never used in play.
fixture = json.loads(Path("/fixtures/gap-run-up.json").read_text())
env = JoypadSpace(gym.make(fixture["env"]), MOVEMENT)
parser = make_parser()
try:
    _, info = env.reset(seed=fixture["seed"])
    parser.parse(info, env.unwrapped.ram)
    for row in fixture["prefix"]:
        for offset in range(row["frames"]):
            action = Action(row["action"])
            if offset == 0 and row["release_jump"]:
                action = JUMP_RELEASE_ACTION[action]
            _, _, terminated, truncated, info = env.step(ACTION_TO_INDEX[action])
            snapshot = parser.parse(info, env.unwrapped.ram, previous_action=action.value)
            assert not terminated and not truncated
    assert snapshot.x == fixture["expected_x"]
    assert DecisionContext(snapshot, ram=env.unwrapped.ram, info=info).to_state()["gap_run_up"]
    for action, duration in ((Action.RIGHT_RUN, 8), (Action.RIGHT_RUN_JUMP, 48)):
        for _ in range(duration):
            _, _, terminated, truncated, info = env.step(ACTION_TO_INDEX[action])
            snapshot = parser.parse(info, env.unwrapped.ram, previous_action=action.value)
            assert not terminated and not truncated and not snapshot.dead
    assert snapshot.x > 1144
    print(
        json.dumps({"run_up_start": fixture["expected_x"], "after_gap": snapshot.x, "passed": True})
    )
finally:
    env.close()

# Replay a second recorded failure: the old estimate said 20 frames, but
# continuing to run collided four frames later. No Jev request is needed.
fixture = json.loads(Path("/fixtures/enemy-contact.json").read_text())
env = JoypadSpace(gym.make(fixture["env"]), MOVEMENT)
parser = make_parser()
try:
    _, info = env.reset(seed=fixture["seed"])
    parser.parse(info, env.unwrapped.ram)
    for row in fixture["prefix"]:
        for offset in range(row["frames"]):
            action = Action(row["action"])
            if offset == 0 and row["release_jump"]:
                action = JUMP_RELEASE_ACTION[action]
            _, _, terminated, truncated, info = env.step(ACTION_TO_INDEX[action])
            snapshot = parser.parse(info, env.unwrapped.ram, previous_action=action.value)
            assert not terminated and not truncated
    assert snapshot.x == fixture["expected_x"]
    old_timing = snapshot.to_state()["hazard"]["estimated_contact_frames"]
    improved = DecisionContext(snapshot, ram=env.unwrapped.ram, info=info).to_state()["hazard"]
    assert old_timing == 20 and improved["estimated_contact_frames"] <= 4
    assert improved["jump_must_start_this_decision"]
    frames = 0
    for _ in range(8):
        frames += 1
        _, _, terminated, truncated, info = env.step(ACTION_TO_INDEX[Action.RIGHT_RUN])
        observed = parser.parse(info, env.unwrapped.ram)
        if observed.dead or terminated or truncated:
            break
    assert frames == 4 and observed.dead
    print(
        json.dumps(
            {
                "old_contact_frames": old_timing,
                "observed_collision_frames": frames,
                "new_contact_frames": improved["estimated_contact_frames"],
                "passed": True,
            }
        )
    )
finally:
    env.close()

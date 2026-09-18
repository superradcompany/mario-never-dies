"""Check marker placement on actual rendered enemies during recorded game inputs."""

import json
from pathlib import Path

import gym_super_mario_bros  # noqa: F401
import gymnasium as gym
from nes_py.wrappers import JoypadSpace

from mnd.actions import ACTION_TO_INDEX, JUMP_RELEASE_ACTION, MOVEMENT, Action
from mnd.perception import DecisionContext, local_map, make_parser, object_on_screen


def main():
    observations, marked, kinds = 0, 0, set()
    for name in ("enemy-contact.json", "side-pipe-platform-stall.json"):
        fixture = json.loads((Path("/fixtures") / name).read_text())
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
                    _, _, ended, truncated, info = env.step(ACTION_TO_INDEX[action])
                    snapshot = parser.parse(info, env.unwrapped.ram, previous_action=action.value)
                    assert not ended and not truncated and not snapshot.dead
                state = DecisionContext(snapshot, ram=env.unwrapped.ram, info=info).to_state()
                if not snapshot.enemies:
                    assert "scene_map" not in state
                    continue
                scene = state["scene_map"]
                terrain, _ = local_map(snapshot, bytes(env.unwrapped.ram), info, whole_screen=True)
                assert len(scene["rows"]) == 13
                assert all(len(r) <= 17 for r in scene["rows"])
                observations += 1
                for enemy, details in zip(snapshot.enemies, state["visible_enemies"], strict=True):
                    assert object_on_screen(env.unwrapped.ram, enemy.slot)
                    assert details["kind"] == enemy.kind
                    kinds.add(enemy.kind)
                    if details["map_cell"] is None:
                        continue
                    r, c = details["map_cell"]
                    assert scene["rows"][r][c] in (details["map_marker"], "X", "*")
                    assert details["terrain_at_marker"] == terrain["rows"][r][c]
                    # Cross-check against the live object's pixel coordinates,
                    # independently of the relative-position-to-cell calculation.
                    ram = env.unwrapped.ram
                    x = int(ram[0x6E + enemy.slot]) * 256 + int(ram[0x87 + enemy.slot])
                    y = int(ram[0xCF + enemy.slot])
                    assert scene["origin_x"] + c * 16 <= x < scene["origin_x"] + (c + 1) * 16
                    assert 32 + r * 16 <= y < 32 + (r + 1) * 16
                    marked += 1
                if observations == 1:
                    print(
                        json.dumps(
                            {"scene_map": scene, "visible_enemies": state["visible_enemies"]}
                        )
                    )
        finally:
            env.close()
    assert observations > 10 and marked > 10
    print(
        json.dumps(
            dict(passed=True, observations=observations, markers=marked, enemy_kinds=sorted(kinds))
        )
    )


if __name__ == "__main__":
    main()

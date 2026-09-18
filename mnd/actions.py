"""Controller vocabulary, including the leftward and pipe-entry buttons."""

from enum import StrEnum


class Action(StrEnum):
    NOOP = "noop"
    RIGHT = "right"
    RIGHT_JUMP = "right_jump"
    RIGHT_RUN = "right_run"
    RIGHT_RUN_JUMP = "right_run_jump"
    JUMP = "jump"
    LEFT = "left"
    LEFT_JUMP = "left_jump"
    LEFT_RUN = "left_run"
    LEFT_RUN_JUMP = "left_run_jump"
    DOWN = "down"
    UP = "up"


# Preserve the original seven indices so existing recordings remain replayable.
# The appended controls match gym_super_mario_bros.actions.COMPLEX_MOVEMENT.
MOVEMENT = [
    ["NOOP"],
    ["right"],
    ["right", "A"],
    ["right", "B"],
    ["right", "A", "B"],
    ["A"],
    ["left"],
    ["left", "A"],
    ["left", "B"],
    ["left", "A", "B"],
    ["down"],
    ["up"],
]
ACTION_TO_INDEX = {action: index for index, action in enumerate(Action)}
JUMP_ACTIONS = frozenset(action for action in Action if "jump" in action.value)
JUMP_RELEASE_ACTION = {
    Action.JUMP: Action.NOOP,
    Action.RIGHT_JUMP: Action.RIGHT,
    Action.RIGHT_RUN_JUMP: Action.RIGHT_RUN,
    Action.LEFT_JUMP: Action.LEFT,
    Action.LEFT_RUN_JUMP: Action.LEFT_RUN,
}


def descriptions():
    # Retain the pinned policy's descriptions for its original controls.
    from typesafe_mario.actions import ACTION_DESCRIPTIONS

    return {
        **{Action(a.value): text for a, text in ACTION_DESCRIPTIONS.items()},
        Action.LEFT_JUMP: (
            "Jump left over a visible obstacle behind Mario, or hold left + jump "
            "while rising to maintain a leftward jump. Useful when walking left is blocked."
        ),
        Action.LEFT_RUN: "Run left; brake rightward momentum first, then accelerate left.",
        Action.LEFT_RUN_JUMP: (
            "Running jump left over a visible gap or obstacle. Hold while rising "
            "to sustain jump height; account for existing momentum."
        ),
        Action.DOWN: "Crouch, or enter a vertical pipe while standing on its opening.",
        Action.UP: "Climb a visible climbable vine; otherwise normally has no effect.",
    }

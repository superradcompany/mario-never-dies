import unittest

from mnd.actions import ACTION_TO_INDEX, JUMP_ACTIONS, JUMP_RELEASE_ACTION, MOVEMENT, Action
from mnd.protocol import ACTIONS


class ActionTests(unittest.TestCase):
    def test_original_recording_indices_remain_compatible(self):
        self.assertEqual(
            ACTIONS[:7],
            ("noop", "right", "right_jump", "right_run", "right_run_jump", "jump", "left"),
        )
        self.assertEqual(
            MOVEMENT[:7],
            [
                ["NOOP"],
                ["right"],
                ["right", "A"],
                ["right", "B"],
                ["right", "A", "B"],
                ["A"],
                ["left"],
            ],
        )

    def test_left_jump_controls_include_jump_and_release_without_reversing(self):
        for jump, release in (
            (Action.LEFT_JUMP, Action.LEFT),
            (Action.LEFT_RUN_JUMP, Action.LEFT_RUN),
        ):
            self.assertIn(jump, JUMP_ACTIONS)
            self.assertEqual(JUMP_RELEASE_ACTION[jump], release)
            pressed = MOVEMENT[ACTION_TO_INDEX[jump]]
            released = MOVEMENT[ACTION_TO_INDEX[release]]
            self.assertIn("left", pressed)
            self.assertIn("A", pressed)
            self.assertEqual(set(pressed) - {"A"}, set(released))
        self.assertEqual(MOVEMENT[ACTION_TO_INDEX[Action.DOWN]], ["down"])
        self.assertEqual(MOVEMENT[ACTION_TO_INDEX[Action.UP]], ["up"])

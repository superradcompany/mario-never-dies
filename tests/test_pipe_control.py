import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mnd.actions import Action
from mnd.perception import enrich_questions
from mnd.pipe_control import pipe_descent, pipe_feedback


class PipeControlTests(unittest.TestCase):
    def pipe(self, button="right", aligned=True):
        return dict(
            shape="left_facing_opening" if button == "right" else "upward_opening",
            entry_button=button,
            aligned=aligned,
            x=208,
            dx=14,
            feet_above_opening=0,
        )

    def history(self, action="down", count=8):
        return [dict(action=action, x_pos=194, end_x=194, y_pos=79, end_y=79) for _ in range(count)]

    def feedback(self, button="right", history=None, aligned=True):
        return pipe_feedback(
            [self.pipe(button, aligned)],
            SimpleNamespace(x=194, y=79),
            self.history() if history is None else history,
        )

    def test_wrong_down_inputs_do_not_count_as_failed_side_entry(self):
        feedback = self.feedback()
        self.assertEqual(feedback["stationary_inputs"], {"down": 8})
        self.assertEqual(feedback["stationary_entry_attempts"], 0)
        self.assertFalse(feedback["entry_not_observed"])

    def test_repeated_aligned_entry_is_bounded_but_short_attempt_is_allowed(self):
        self.assertTrue(self.feedback("down")["entry_not_observed"])
        self.assertFalse(self.feedback("down", self.history(count=3))["entry_not_observed"])
        self.assertTrue(self.feedback(history=self.history("right_run"))["entry_not_observed"])

    def test_animation_or_movement_resets_stationary_evidence(self):
        for key in ("x_pos", "end_x", "y_pos", "end_y"):
            history = self.history()
            history[-1][key] += 8
            self.assertFalse(self.feedback("down", history)["entry_not_observed"])
        history = self.history()
        history[-1]["forced"] = True
        self.assertEqual(self.feedback(history=history)["stationary_inputs"], {})

    def test_feedback_requires_visible_alignment_and_remains_bounded(self):
        self.assertIsNone(self.feedback(aligned=False))
        self.assertEqual(
            self.feedback(history=self.history(count=100))["stationary_inputs"], {"down": 8}
        )
        self.assertIsNone(pipe_feedback([], SimpleNamespace(x=194, y=79), self.history()))

    def questions(self, button="right"):
        state = dict(
            observation_version=2,
            objective="Survive and collect.",
            player={},
            pipes=[self.pipe(button)],
            pipe_interaction=self.feedback(button),
        )
        original = SimpleNamespace(criteria={a.value: a.value for a in Action}, instructions={})
        sdk = SimpleNamespace(Choice=lambda **kwargs: SimpleNamespace(**kwargs))
        with patch.dict(sys.modules, typesafe_sdk=sdk):
            return enrich_questions(state, {"next_action": original})["next_action"]

    def test_side_mouth_explicitly_offers_right_without_removing_choices(self):
        result = self.questions()
        self.assertEqual(set(result.criteria), {a.value for a in Action})
        self.assertIn("entry direction is RIGHT", result.instructions["priority"])
        self.assertIn("Does not enter", result.criteria["down"]["current_effect"])
        self.assertIn("attempt entry", result.criteria["right"]["current_effect"])

    def test_unproductive_vertical_entry_does_not_keep_prescribing_down(self):
        result = self.questions("down")
        self.assertIn("Stop repeating", result.instructions["priority"])
        self.assertIn("reassess", result.criteria["down"]["current_effect"])


class PipeDescentTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(x=164, grounded=True, status="small")
        self.mapping = dict(
            available=True,
            origin_x=0,
            rows=[
                "#...#######....P",
                "#..............P",
                "#..............P",
                "#....ooooo.....P",
                "#..............P",
                "#...ooooooo....P",
                "#.........M....P",
                "#..............P",
                "#...#######....P",
                "#...#######..PPP",
                "#...#######..PPP",
                "################",
                "################",
            ],
        )
        self.pipe = dict(
            shape="left_facing_opening",
            x=208,
            height=32,
            screen_y=176,
            feet_above_opening=48,
            aligned=False,
        )

    def descent(self):
        return pipe_descent(self.snapshot, self.mapping, 160, [self.pipe])

    def test_platform_right_edge_connects_to_mouth_but_left_edge_is_blocked(self):
        result = self.descent()
        self.assertEqual(result["direction"], "right")
        self.assertEqual(result["edge_x"], 176)
        self.assertEqual(result["lower_floor_screen_y"], 208)
        # Coordinates are observations, not a special-case level location.
        self.snapshot.x += 512
        self.mapping["origin_x"] += 512
        self.pipe["x"] += 512
        self.assertEqual(self.descent()["edge_x"], 688)

    def test_roof_can_require_a_left_drop(self):
        self.mapping["rows"][8] = "#........######P"
        self.mapping["rows"][9] = "#............PPP"
        self.mapping["rows"][10] = "#............PPP"
        self.assertEqual(self.descent()["direction"], "left")
        self.assertEqual(self.descent()["edge_x"], 144)

    def test_missing_floor_unknown_camera_or_blocked_corridor_is_not_a_route(self):
        original = self.mapping["rows"][:]
        for row, col, value in [(11, 11, "."), (8, 11, "u"), (10, 12, "#")]:
            self.mapping["rows"] = original[:]
            cells = list(self.mapping["rows"][row])
            cells[col] = value
            self.mapping["rows"][row] = "".join(cells)
            self.assertIsNone(self.descent())

    def test_requires_ground_contact_lower_mouth_and_body_clearance(self):
        self.snapshot.grounded = False
        self.assertIsNone(self.descent())
        self.snapshot.grounded = True
        self.pipe["feet_above_opening"] = 0
        self.assertIsNone(self.descent())
        self.pipe["feet_above_opening"] = 48
        self.mapping["rows"][9] = "#...#######.#PPP"
        self.assertIsNotNone(self.descent())  # Small Mario fits below the overhang.
        self.snapshot.status = "tall"
        self.assertIsNone(self.descent())

    def test_prompt_uses_observed_direction_and_explains_down_before_alignment(self):
        state = dict(
            observation_version=2,
            objective="Survive and collect.",
            player={},
            pipes=[self.pipe],
            pipe_descent=self.descent(),
        )
        original = SimpleNamespace(criteria={a.value: a.value for a in Action}, instructions={})
        sdk = SimpleNamespace(Choice=lambda **kwargs: SimpleNamespace(**kwargs))
        with patch.dict(sys.modules, typesafe_sdk=sdk):
            result = enrich_questions(state, {"next_action": original})["next_action"]
        self.assertIn("Walk right", result.instructions["pipe_approach"])
        self.assertIn("visible right drop", result.criteria["right"]["current_effect"])
        self.assertIn("Does not descend", result.criteria["down"]["current_effect"])
        self.assertEqual(set(result.criteria), {a.value for a in Action})

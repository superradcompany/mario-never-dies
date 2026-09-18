import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mnd.actions import Action
from mnd.perception import enrich_questions, landing_preview
from mnd.terrain import enrich_gap_state, platform_observation


class TerrainTests(unittest.TestCase):
    def mapping(self):
        # Ground ends at x256. A higher landing spans x288..352.
        rows = ["." * 13 for _ in range(13)]
        rows[10] = "......####..."
        rows[11] = "####........."
        rows[12] = "####........."
        return dict(available=True, origin_x=192, rows=rows)

    def snapshot(self, x=237, grounded=True):
        return SimpleNamespace(x=x, y=79, grounded=grounded, dx=3, dy=0)

    def test_gap_has_precise_edge_and_higher_landing(self):
        geo = platform_observation(self.snapshot(), self.mapping(), {"y_pixel": 176})
        edge = geo["edge"]
        self.assertEqual(edge["x"], 256)
        self.assertEqual(edge["distance_from_center_pixels"], 11)
        self.assertEqual(edge["frames_at_current_speed"], 3.7)
        self.assertEqual(edge["gap_width_pixels"], 32)
        self.assertEqual(edge["next_landing"]["x"], [288, 352])
        self.assertEqual(edge["next_landing"]["height_above_feet"], 16)
        self.assertTrue(edge["jump_must_start_this_decision"])
        earlier = platform_observation(self.snapshot(x=213), self.mapping(), {"y_pixel": 176})
        self.assertFalse(earlier["edge"]["jump_must_start_this_decision"])

    def test_airborne_gap_does_not_turn_into_clear_ground(self):
        snap = self.snapshot(x=264, grounded=False)
        geo = platform_observation(snap, self.mapping(), {"y_pixel": 120})
        self.assertTrue(geo["airborne_over_gap"])
        state = {"terrain": {"gap_ahead": False, "clear_forward_tiles": 8}}
        enrich_gap_state(snap, state, geo)
        self.assertTrue(state["terrain"]["gap_ahead"])
        self.assertEqual(state["terrain"]["clear_forward_tiles"], 0)
        self.assertEqual(geo["landing_candidates"][0]["x"], [288, 352])

    def test_cropped_platform_does_not_invent_an_edge_or_landing(self):
        mapping = self.mapping()
        mapping["rows"][11] = "####uuuuuuuuu"
        mapping["rows"][12] = "####uuuuuuuuu"
        mapping["rows"][10] = "....uuuuuuuuu"
        geo = platform_observation(self.snapshot(), mapping, {"y_pixel": 176})
        self.assertIsNone(geo["edge"])
        self.assertEqual(geo["landing_candidates"], [])

    def test_drop_to_visible_floor_is_not_an_urgent_pit_jump(self):
        mapping = self.mapping()
        mapping["rows"][12] = "#" * 13
        geo = platform_observation(self.snapshot(), mapping, {"y_pixel": 176})
        self.assertFalse(geo["edge"]["gap_beyond"])
        self.assertFalse(geo["edge"]["jump_must_start_this_decision"])
        self.assertEqual(geo["edge"]["lower_floor_screen_y"], 224)

    def test_body_overlap_keeps_support_at_edge_and_unknown_is_not_safe_floor(self):
        mapping = self.mapping()
        geo = platform_observation(self.snapshot(x=250), mapping, {"y_pixel": 176})
        self.assertEqual(geo["current_support"]["x"][1], 256)
        self.assertTrue(geo["edge"]["jump_must_start_this_decision"])
        mapping["rows"] = ["u" * 13] * 13
        geo = platform_observation(self.snapshot(x=264, grounded=False), mapping, {"y_pixel": 120})
        self.assertFalse(geo["airborne_over_gap"])
        self.assertEqual(geo["landing_candidates"], [])

    def test_narrow_landing_warning_includes_remaining_ascent(self):
        snap = self.snapshot(x=285, grounded=False)
        snap.dy = 3
        preview = landing_preview(snap, self.mapping(), {"y_pixel": 108}, anticipate_ascent=True)
        self.assertEqual(preview["surface_x"], [288, 352])
        self.assertTrue(preview["includes_remaining_ascent"])
        self.assertTrue(preview["brake_before_overshoot"])
        # The old descending-only estimate did not warn until x309, too late
        # to slow down before this recorded small-platform landing.
        snap.grounded = True
        self.assertIsNone(
            landing_preview(snap, self.mapping(), {"y_pixel": 108}, anticipate_ascent=True)
        )

    def test_ascent_warning_does_not_brake_back_onto_takeoff_ledge(self):
        snap = self.snapshot(x=329, grounded=False)
        snap.dy, snap.jump_distance_pixels = 3, 4
        preview = landing_preview(snap, self.mapping(), {"y_pixel": 107}, anticipate_ascent=True)
        self.assertTrue(preview["leaving_takeoff_support"])
        self.assertFalse(preview["brake_before_overshoot"])

    def test_slow_approach_can_use_remaining_ground_for_run_up(self):
        snap = self.snapshot(x=325)
        snap.dx = 0
        geo = platform_observation(snap, self.mapping(), {"y_pixel": 160})
        self.assertEqual(geo["edge"]["distance_from_center_pixels"], 19)
        self.assertFalse(geo["edge"]["jump_must_start_this_decision"])
        snap.dx = 3
        geo = platform_observation(snap, self.mapping(), {"y_pixel": 160})
        self.assertTrue(geo["edge"]["jump_must_start_this_decision"])

    def test_higher_landing_needs_height_before_its_near_face(self):
        mapping = self.mapping()
        mapping["rows"] = ["." * 13 for _ in range(13)]
        mapping["rows"][6] = "######......."  # Lower ledge ends at x288.
        mapping["rows"][2] = "......####..."  # Next top is 64 pixels higher.
        snap = self.snapshot(x=236)
        geo = platform_observation(snap, mapping, {"y_pixel": 96})
        self.assertGreater(geo["edge"]["distance_from_center_pixels"], 31)
        self.assertTrue(geo["edge"]["higher_landing_needs_early_takeoff"])
        self.assertTrue(geo["edge"]["jump_must_start_this_decision"])

    def test_ordinary_floor_keeps_original_landing_behavior(self):
        mapping = self.mapping()
        mapping["rows"][12] = "#" * 13
        snap = self.snapshot(x=285, grounded=False)
        snap.dy = 3
        geo = platform_observation(snap, mapping, {"y_pixel": 108})
        self.assertFalse(geo["has_visible_gap"])
        self.assertIsNone(landing_preview(snap, mapping, {"y_pixel": 108}))

    def test_urgent_gap_adds_takeoff_guidance_without_removing_choices(self):
        state = {
            "observation_version": 2,
            "objective": "Survive and collect reachable rewards.",
            "player": {},
            "platforms": {"edge": {"jump_must_start_this_decision": True}},
        }
        original = SimpleNamespace(criteria={a.value: a.value for a in Action}, instructions={})
        sdk = SimpleNamespace(Choice=lambda **kwargs: SimpleNamespace(**kwargs))
        with patch.dict(sys.modules, typesafe_sdk=sdk):
            result = enrich_questions(state, {"next_action": original})["next_action"]
        self.assertEqual(set(result.criteria), set(original.criteria))
        self.assertIn("NOW while grounded", result.instructions["priority"])
        self.assertIn("takeoff window", result.criteria["right_run"]["current_effect"])

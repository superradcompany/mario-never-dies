import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mnd.perception import (
    DecisionContext,
    action_frames,
    collision_timing,
    correct_visible_drop,
    descent_braking,
    enrich_questions,
    landing_preview,
    local_map,
    movement_feedback,
    object_on_screen,
    retreat_finished,
    stall_recovery,
    tile_symbol,
    visible_pipes,
    visible_powerup,
)


class Snapshot(SimpleNamespace):
    def to_state(self):
        return {
            "level": {"world": 1, "stage": 1, "area": 1},
            "player": {"x": self.x},
            "hazard": getattr(self, "hazard", {}),
            "terrain": {},
            "trajectory": {},
            "episode": {"best_progress": getattr(self, "best_progress", self.x)},
        }


class PerceptionTests(unittest.TestCase):
    def snapshot(self, x=248):
        return Snapshot(
            x=x,
            y=79,
            world=1,
            stage=1,
            coins=2,
            score=400,
            status="small",
            enemies=[],
            local_grid=[],
            grounded=True,
            dx=3,
            dy=0,
        )

    def test_map_spans_page_boundary_and_preserves_vertical_coordinates(self):
        ram = bytearray(2048)
        ram[0x71C] = 160
        # Last column of page zero: question block; first of page one: coin.
        ram[0x500 + 5 * 16 + 15] = 0xC0
        ram[0x500 + 208 + 5 * 16] = 0xC2
        ground, targets = local_map(self.snapshot(), ram, {"y_pixel": 176})
        airborne, _ = local_map(self.snapshot(), ram, {"y_pixel": 100})
        self.assertEqual(ground["rows"][5][2:4], "?o")
        self.assertEqual(airborne["rows"][5][2:4], "?o")
        self.assertEqual({t["kind"] for t in targets}, {"coin", "question_block"})
        self.assertEqual(next(t["x"] for t in targets if t["kind"] == "coin"), 264)

    def test_unknown_tile_does_not_invent_reward_and_request_freezes_ram(self):
        ram = bytearray(2048)
        ram[0x71C] = 160
        ram[0x500 + 208 + 5 * 16] = 0x80
        context = DecisionContext(self.snapshot(), ram=ram, info={"y_pixel": 176})
        ram[0x500 + 208 + 5 * 16] = 0xC2
        self.assertEqual(context.to_state()["collectibles"], [])
        self.assertEqual(context.to_state()["local_map"]["rows"][5][3], "#")

    def test_memory_is_bounded_local_and_scoped_to_area(self):
        evidence = [
            {
                "context": "1:1:1:experiment",
                "kind": "experiment",
                "x_pos": 250,
                "label": "wait then jump",
                "outcome": "dead",
            }
        ] * 6
        evidence += [
            {"context": "1:2:1:experiment", "x_pos": 250},
            {"context": "1:1:1:experiment", "x_pos": 2000},
        ]
        state = DecisionContext(self.snapshot(), evidence).to_state()
        self.assertEqual(len(state["previous_attempts"]), 4)
        self.assertEqual(state["previous_attempts"][0]["outcome"], "dead")
        self.assertEqual(state["performance"]["coins"], 2)
        self.assertFalse(state["local_map"]["available"])

    def test_landing_hint_brakes_before_narrow_platform_but_not_open_ground(self):
        snap = self.snapshot(x=309)
        snap.grounded, snap.dy = False, -2
        rows = ["............."] * 13
        rows[10] = ".####........"
        mapping = {"available": True, "origin_x": 272, "rows": rows}
        preview = landing_preview(snap, mapping, {"y_pixel": 93})
        self.assertEqual(preview["surface_x"], [288, 352])
        self.assertTrue(preview["brake_before_overshoot"])
        rows[12] = "#############"
        self.assertFalse(landing_preview(snap, mapping, {"y_pixel": 93})["brake_before_overshoot"])
        rows[12] = "............."
        rows[10] = "#############"
        self.assertFalse(landing_preview(snap, mapping, {"y_pixel": 93})["brake_before_overshoot"])
        rows[10] = "............."
        self.assertIsNone(landing_preview(snap, mapping, {"y_pixel": 93}))

    def test_high_staircase_edge_has_visible_floor_not_a_pit(self):
        snap = self.snapshot(x=3030)
        snap.grounded, snap.dy = False, -2
        rows = ["............."] * 13
        rows[3] = ".##.........."
        rows[11] = "#############"
        mapping = {"available": True, "origin_x": 2992, "rows": rows}
        self.assertFalse(landing_preview(snap, mapping, {"y_pixel": 11})["brake_before_overshoot"])
        state = {
            "local_map": mapping,
            "player": {"screen_feet_y": 80},
            "terrain": {
                "gap_ahead": True,
                "gap_distance_tiles": 1,
                "gap_width_tiles_visible": 8,
            },
        }
        correct_visible_drop(snap, state)
        self.assertFalse(state["terrain"]["gap_ahead"])
        self.assertEqual(
            state["terrain"]["drop_with_visible_floor"]["floor_screen_y_range"], [208, 208]
        )

    def test_real_or_unknown_gap_keeps_its_warning(self):
        for floor in ("####.########", "####uuuuuuuuu"):
            rows = ["............."] * 13
            rows[11] = floor
            state = {
                "local_map": {"available": True, "origin_x": 2992, "rows": rows},
                "player": {"screen_feet_y": 80},
                "terrain": {
                    "gap_ahead": True,
                    "gap_distance_tiles": 1,
                    "gap_width_tiles_visible": 8,
                },
            }
            correct_visible_drop(self.snapshot(x=3030), state)
            self.assertTrue(state["terrain"]["gap_ahead"])

    def test_offscreen_buffer_cells_cannot_create_collectibles(self):
        ram = bytearray(2048)
        ram[0x500 + 208 + 5 * 16] = 0xC2
        mapping, targets = local_map(self.snapshot(), ram, {"y_pixel": 176})
        self.assertEqual(mapping["rows"][5][3], "u")
        self.assertEqual(targets, [])

    def test_collection_window_is_suppressed_near_enemy(self):
        ram = bytearray(2048)
        ram[0x71C] = 160
        ram[0x500 + 5 * 16 + 15] = 0xC0
        snapshot = self.snapshot()
        state = DecisionContext(snapshot, ram=ram).to_state()
        self.assertTrue(state["collection_opportunity"]["block_bump_window"])
        snapshot.hazard = {"estimated_contact_frames": 30}
        state = DecisionContext(snapshot, ram=ram).to_state()
        self.assertFalse(state["collection_opportunity"]["block_bump_window"])

    def test_powerup_is_read_only_from_active_special_slot(self):
        ram = bytearray(2048)
        ram[0x14], ram[0x1B], ram[0x8C], ram[0xD4] = 1, 0x2E, 250, 160
        ram[0x200] = 160
        item = visible_powerup(self.snapshot(), ram, {"y_pixel": 176})
        self.assertEqual((item["kind"], item["dx"], item["dy"]), ("mushroom", 2, -16))
        ram[0x1B] = 6
        self.assertIsNone(visible_powerup(self.snapshot(), ram, {}))

    def test_hidden_contents_and_offscreen_objects_are_not_observations(self):
        self.assertEqual(tile_symbol(0x5F), ".")
        self.assertEqual(tile_symbol(0x60), ".")
        self.assertEqual(tile_symbol(0x55), tile_symbol(0x51))
        ram = bytearray(2048)
        ram[0x87], ram[0x200] = 100, 176
        self.assertTrue(object_on_screen(ram, 0))
        ram[0x6E] = 1
        self.assertFalse(object_on_screen(ram, 0))
        ram[0x6E] = 0
        ram[0x200:0x218:4] = bytes([248]) * 6
        self.assertFalse(object_on_screen(ram, 0))

    def test_block_collection_does_not_restart_old_detour_after_powerup_chase(self):
        ram = bytearray(2048)
        ram[0x71C] = 160
        ram[0x500 + 5 * 16 + 15] = 0xC0
        snapshot = self.snapshot()
        snapshot.best_progress = 400
        state = DecisionContext(snapshot, ram=ram).to_state()
        self.assertFalse(state["collection_opportunity"]["block_bump_window"])

    def test_enemy_behind_suppresses_optional_block_detour(self):
        ram = bytearray(2048)
        ram[0x71C] = 160
        ram[0x500 + 5 * 16 + 15] = 0xC0
        snapshot = self.snapshot()
        enemy = SimpleNamespace(dx_pixels=-24, dy_pixels=0, to_state=lambda: {})
        snapshot.enemies = [enemy]
        state = DecisionContext(snapshot, ram=ram).to_state()
        self.assertFalse(state["collection_opportunity"]["block_bump_window"])

    def test_collision_warning_includes_body_width_and_acceleration(self):
        snapshot = self.snapshot()
        snapshot.dx = 2
        snapshot.enemies = [SimpleNamespace(dx_pixels=20, dy_pixels=8, relative_velocity_x=-1)]
        state = {"hazard": {"estimated_contact_frames": 20}}
        collision_timing(snapshot, state)
        self.assertEqual(state["hazard"]["body_clearance_pixels"], 4)
        self.assertEqual(state["hazard"]["estimated_contact_frames"], 2)
        self.assertTrue(state["hazard"]["jump_must_start_this_decision"])
        # An enemy underneath a high platform must not force a needless jump.
        snapshot.enemies[0].dy_pixels = 80
        safe = {"hazard": {}}
        collision_timing(snapshot, safe)
        self.assertEqual(safe["hazard"], {})

    def test_landing_cue_survives_center_crossing_platform_edge(self):
        snap = self.snapshot(x=1097)
        snap.grounded, snap.dx, snap.dy = False, 1, -2
        rows = ["............."] * 13
        rows[11] = "###..########"
        mapping = {"available": True, "origin_x": 1056, "rows": rows}
        preview = landing_preview(snap, mapping, {"y_pixel": 116})
        self.assertEqual(preview["surface_x"], [1056, 1104])
        self.assertTrue(preview["brake_before_overshoot"])

    def stalled_history(self):
        return [
            {
                "action": "right_jump",
                "x_pos": 248,
                "end_x": 248,
                "y_pos": 79,
                "end_y": 79,
                "grounded": True,
                "end_grounded": True,
                "coins_before": 2,
                "coins_after": 2,
                "score_before": 400,
                "score_after": 400,
            }
            for _ in range(8)
        ]

    def test_retreat_is_offered_only_after_unproductive_repeated_attempts(self):
        snap = self.snapshot()
        history = self.stalled_history()
        state = DecisionContext(snap, recent_actions=history).to_state()
        self.assertEqual(action_frames(state, "left"), 64)
        self.assertEqual(action_frames(state, "right_jump"), 8)
        self.assertEqual(action_frames({}, "left"), 8)
        snap.grounded = False
        self.assertIsNone(stall_recovery(snap, history))
        snap.grounded = True
        self.assertIsNone(stall_recovery(snap, history[:7]))
        for key, value in (
            ("coins_after", 3),
            ("score_after", 500),
            ("end_x", 280),
            ("end_y", 143),
            ("forced", True),
        ):
            changed = self.stalled_history()
            changed[-1][key] = value
            self.assertIsNone(stall_recovery(snap, changed))

    def test_stall_reveals_visible_terrain_behind_but_no_extra_camera_columns(self):
        ram = bytearray(2048)
        ram[0x71C] = 161
        ram[0x500 + 5 * 16 + 10] = 0xC0
        ram[0x500 + 5 * 16 + 9] = 0xC2  # Just outside the camera.
        ordinary = DecisionContext(self.snapshot(), ram=ram).to_state()
        recovered = DecisionContext(
            self.snapshot(), ram=ram, recent_actions=self.stalled_history()
        ).to_state()
        self.assertEqual(ordinary["local_map"]["origin_x"], 208)
        self.assertEqual(recovered["local_map"]["origin_x"], 160)
        self.assertEqual(len(recovered["local_map"]["rows"][0]), 17)
        self.assertEqual([t["kind"] for t in recovered["collectibles"]], ["question_block"])

    def test_retreat_returns_control_on_descent_enemy_or_camera_edge(self):
        before, after = self.snapshot(), self.snapshot(x=220)
        ram = bytearray(2048)
        ram[0x71C] = 160
        self.assertFalse(retreat_finished(before, after, ram))
        after.y -= 32
        self.assertTrue(retreat_finished(before, after, ram))
        after.y = before.y
        after.enemies = [SimpleNamespace(dx_pixels=-30, dy_pixels=0)]
        self.assertTrue(retreat_finished(before, after, ram))
        after.enemies = []
        after.x = 168
        self.assertTrue(retreat_finished(before, after, ram))

    def test_blocked_left_is_recognized_and_no_longer_gets_a_long_hold(self):
        history = self.stalled_history()
        for row in history:
            row["action"] = "left"
        recovery = stall_recovery(self.snapshot(), history)
        self.assertTrue(recovery["left_walk_blocked"])
        self.assertEqual(recovery["left_max_frames"], 8)

    def test_left_jump_loop_is_detected_in_air_without_a_longer_left_hold(self):
        snap = self.snapshot()
        history = self.stalled_history()
        for row in history:
            row["action"] = "left_jump"
        recovery = stall_recovery(snap, history)
        self.assertEqual(recovery["left_max_frames"], 8)
        self.assertEqual(recovery["ineffective_actions"], {"left_jump": 8})
        snap.grounded = False
        feedback = movement_feedback(snap, history)
        self.assertEqual(feedback["blocked_directions"], ["left"])
        history[-1]["end_x"] += 12
        self.assertEqual(movement_feedback(snap, history)["blocked_directions"], [])

    def test_stall_hints_describe_failed_direction_without_blaming_untried_right(self):
        snap = self.snapshot()
        history = self.stalled_history()
        for row in history:
            row["action"] = "left_jump"
        state = DecisionContext(snap, recent_actions=history).to_state()
        original = SimpleNamespace(
            criteria={"left_jump": "left jump", "right_jump": "right jump"}, instructions={}
        )
        sdk = SimpleNamespace(Choice=lambda **kwargs: SimpleNamespace(**kwargs))
        with patch.dict(sys.modules, typesafe_sdk=sdk):
            result = enrich_questions(state, {"next_action": original})["next_action"]
        self.assertIn("Chosen 8 times", result.criteria["left_jump"]["current_effect"])
        self.assertEqual(result.criteria["right_jump"], "right jump")

    def test_camera_boundary_does_not_prescribe_more_leftward_jumping(self):
        snap = self.snapshot()
        ram = bytearray(2048)
        ram[0x71C] = 248
        history = self.stalled_history()
        for row in history:
            row["action"] = "left_jump"
        state = DecisionContext(snap, ram=ram, recent_actions=history).to_state()
        self.assertTrue(state["movement_feedback"]["camera"]["near_left_limit"])
        self.assertEqual(action_frames(state, "left"), 8)
        original = SimpleNamespace(
            criteria={a: a for a in ("left", "left_jump", "left_run", "left_run_jump", "right")},
            instructions={},
        )
        sdk = SimpleNamespace(Choice=lambda **kwargs: SimpleNamespace(**kwargs))
        with patch.dict(sys.modules, typesafe_sdk=sdk):
            result = enrich_questions(state, {"next_action": original})["next_action"]
        for action in ("left", "left_jump", "left_run", "left_run_jump"):
            self.assertIn("camera boundary", result.criteria[action]["current_effect"])
        self.assertEqual(set(result.criteria), set(original.criteria))
        self.assertTrue(result.instructions["priority"].startswith("Repeated left inputs"))
        ram[0x71C] = 160
        self.assertFalse(movement_feedback(snap, history, ram)["camera"]["near_left_limit"])

    def test_pipe_shapes_are_visible_not_hidden_destination_flags(self):
        ram = bytearray(2048)
        ram[0x71C] = 160
        # Side-facing mouth occupies one column and two rows.
        ram[0x500 + 6 * 16 + 15] = 0x1C
        ram[0x500 + 7 * 16 + 15] = 0x1F
        snap = self.snapshot(x=224)
        pipes = visible_pipes(snap, ram, {"y_pixel": 128})
        self.assertEqual(pipes[0]["shape"], "left_facing_opening")
        self.assertEqual(pipes[0]["entry_button"], "right")
        self.assertTrue(pipes[0]["aligned"])
        self.assertIn("unobserved", pipes[0]["destination"])
        ram[0x71A], ram[0x71C] = 1, 0
        self.assertEqual(visible_pipes(snap, ram, {}), [])

    def test_visible_pipe_top_requires_alignment_before_down_entry(self):
        ram = bytearray(2048)
        ram[0x500 + 8 * 16 + 10] = 0x10
        ram[0x500 + 8 * 16 + 11] = 0x11
        snap = self.snapshot(x=168)
        pipe = visible_pipes(snap, ram, {"y_pixel": 128})[0]
        self.assertEqual(pipe["entry_button"], "down")
        self.assertTrue(pipe["aligned"])
        self.assertEqual(tile_symbol(0x10), "P")
        self.assertFalse(visible_pipes(snap, ram, {"y_pixel": 80})[0]["aligned"])

    def test_brake_before_left_drop_only_when_a_lower_landing_is_visible(self):
        snap = self.snapshot(x=136)
        snap.dx = -2
        rows = ["........#####"] + ["............."] * 7 + [".......######"] * 5
        mapping = {"available": True, "origin_x": 0, "rows": rows}
        self.assertTrue(descent_braking(snap, mapping, 32)["countersteer_right_now"])
        snap.x = 116  # Only the right edge of Mario still overlaps the upper floor.
        self.assertTrue(descent_braking(snap, mapping, 32)["countersteer_right_now"])
        snap.x = 190
        self.assertFalse(descent_braking(snap, mapping, 32)["countersteer_right_now"])
        snap.dx = 1
        self.assertIsNone(descent_braking(snap, mapping, 32))
        snap.dx = -2
        rows[8:] = ["............."] * 5
        self.assertIsNone(descent_braking(snap, mapping, 32))

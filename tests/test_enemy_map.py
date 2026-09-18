import copy
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mnd.actions import Action
from mnd.enemy_map import enemy_scene
from mnd.perception import enrich_questions


class EnemyMapTests(unittest.TestCase):
    def setUp(self):
        self.mapping = dict(
            available=True,
            origin_x=160,
            origin_screen_y=32,
            tile_size_pixels=16,
            player_cell=[1, 2],
            rows=[".....P", "..M..P", "######"],
            legend={"M": "Mario", "P": "pipe", "#": "terrain", ".": "empty"},
            coordinates="rows down, columns right; screen y increases down",
        )
        self.snapshot = SimpleNamespace(x=192, enemies=[])

    def enemy(self, dx, dy=0, kind="goomba"):
        return SimpleNamespace(
            dx_pixels=dx,
            dy_pixels=dy,
            to_state=lambda: {"kind": kind, "dx_pixels": dx, "dy_pixels": dy},
        )

    def scene(self):
        return enemy_scene(self.snapshot, self.mapping, 48)

    def test_enemy_positions_are_linked_to_details_without_mutating_terrain(self):
        before = copy.deepcopy(self.mapping)
        self.snapshot.enemies = [self.enemy(32), self.enemy(48, -16, "piranha_plant")]
        scene, enemies = self.scene()
        self.assertEqual(scene["rows"], [".....2", "..M.1P", "######"])
        self.assertEqual(enemies[0]["map_cell"], [1, 4])
        self.assertEqual(enemies[1]["map_marker"], "2")
        self.assertEqual(enemies[1]["terrain_at_marker"], "P")
        self.assertEqual(self.mapping, before)

    def test_enemy_behind_mario_is_plotted_using_world_origin(self):
        self.snapshot.enemies = [self.enemy(-32)]
        scene, enemies = self.scene()
        self.assertEqual(scene["rows"][1], "1.M..P")
        self.snapshot.x += 512
        self.mapping["origin_x"] += 512
        shifted, details = self.scene()
        self.assertEqual(shifted["rows"], scene["rows"])
        self.assertEqual(details, enemies)

    def test_overlapping_enemies_and_mario_keep_every_observation(self):
        self.snapshot.enemies = [self.enemy(0), self.enemy(0), self.enemy(32), self.enemy(33)]
        scene, enemies = self.scene()
        self.assertEqual(scene["rows"][1], "..X.*P")
        self.assertEqual([e["map_marker"] for e in enemies], ["1", "2", "3", "4"])
        self.assertEqual([e["map_cell"] for e in enemies], [[1, 2], [1, 2], [1, 4], [1, 4]])

    def test_unmapped_positions_do_not_wrap_or_invent_a_marker(self):
        self.mapping["rows"][0] = "u....P"
        self.snapshot.enemies = [
            self.enemy(-48),
            self.enemy(64),
            self.enemy(0, -32),
            self.enemy(-32, -16),
        ]
        scene, enemies = self.scene()
        self.assertEqual(scene["rows"], self.mapping["rows"])
        self.assertTrue(all(e["map_cell"] is None for e in enemies))

    def test_markers_refresh_when_an_enemy_moves_or_disappears(self):
        self.snapshot.enemies = [self.enemy(32)]
        first, _ = self.scene()
        self.snapshot.enemies = [self.enemy(16)]
        second, _ = self.scene()
        self.assertEqual(first["rows"][1], "..M.1P")
        self.assertEqual(second["rows"][1], "..M1.P")
        self.snapshot.enemies = []
        self.assertEqual(self.scene(), (None, []))

    def test_missing_map_preserves_enemy_details_without_fabricating_locations(self):
        self.snapshot.enemies = [self.enemy(32)]
        self.mapping["available"] = False
        self.assertEqual(self.scene(), (None, [self.snapshot.enemies[0].to_state()]))

    def test_observation_stays_bounded_to_five_enemies(self):
        self.snapshot.enemies = [self.enemy(32)] * 8
        _, enemies = self.scene()
        self.assertEqual(len(enemies), 5)
        self.assertEqual(enemies[-1]["map_marker"], "5")

    def test_prompt_explains_markers_without_removing_controller_choices(self):
        original = SimpleNamespace(criteria={a.value: a.value for a in Action}, instructions={})
        state = dict(
            observation_version=2,
            objective="Survive and collect.",
            player={},
            scene_map={"rows": ["M.1"]},
        )
        sdk = SimpleNamespace(Choice=lambda **kwargs: SimpleNamespace(**kwargs))
        with patch.dict(sys.modules, typesafe_sdk=sdk):
            result = enrich_questions(state, {"next_action": original})["next_action"]
        self.assertIn("visible_enemies.map_marker", result.instructions["map"])
        self.assertIn("not collision outlines", result.instructions["map"])
        self.assertEqual(set(result.criteria), {a.value for a in Action})

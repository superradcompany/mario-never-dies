"""Bounded, inspectable observations added to the pinned upstream Jev policy."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from .enemy_map import enemy_scene
from .pipe_control import pipe_choice_hints, pipe_descent, pipe_feedback
from .terrain import enrich_gap_state, platform_observation

# SMB1's collision buffer is two alternating 16 × 13 metatile pages. Never
# reveal an invisible block or infer a brick's contents from its internal ID.
COINS = {0xC2, 0xC3}
QUESTION_BLOCKS = {0xC0, 0xC1}
INVISIBLE_BLOCKS = {0x5F, 0x60}
PIPE_TILES = {0x10, 0x11, 0x14, 0x15, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x21}


def object_on_screen(ram, slot):
    """Require camera overlap and a drawn sprite, not just an active RAM slot."""
    if ram is None or len(ram) < 0x71E:
        return False
    camera = int(ram[0x71A]) * 256 + int(ram[0x71C])
    x = int(ram[0x6E + slot]) * 256 + int(ram[0x87 + slot])
    if x + 16 <= camera or x >= camera + 256:
        return False
    # Hidden/offscreen sprites are moved to y=$f8 in SMB's rendered OAM buffer.
    offset = int(ram[0x6E5 + slot])
    return any(24 <= ram[0x200 + ((offset + part * 4) % 256)] < 240 for part in range(6))


def make_parser(**kwargs):
    from typesafe_mario.state import EnemyObservation, MarioStateParser

    class GroundAwareParser(MarioStateParser):
        """Use collision state, not a single sampled tile, for ground contact.

        The upstream tile sample uses Mario's left edge. His body can overlap a
        platform while that sample lies over empty space, falsely reporting an
        airborne stall. SMB1 Player_State ($1d) is 0 on ground, 1 jumping, 2 falling.
        Keep the upstream per-frame history machinery so takeoff timing resets.
        """

        def parse(self, info, ram=None, **options):
            self.ground_contact = bool(ram[0x1D] == 0) if ram is not None else None
            return super().parse(info, ram, **options)

        def _extract_enemies(self, info, ram, mario_x, mario_screen_y):
            enemies = super()._extract_enemies(info, ram, mario_x, mario_screen_y)
            # An active type-zero object is a green Koopa, not an empty slot.
            # The active flag distinguishes it from an unused zero-filled slot.
            if ram is not None:
                for slot in range(5):
                    if not ram[0x0F + slot] or ram[0x16 + slot] != 0:
                        continue
                    dx = int(ram[0x6E + slot]) * 256 + int(ram[0x87 + slot]) - mario_x
                    previous = self._last_enemy_dx.get(slot)
                    self._last_enemy_dx[slot] = dx
                    if -192 <= dx <= 320:
                        enemies.append(
                            EnemyObservation(
                                slot=slot,
                                kind_id=0,
                                kind="green_koopa",
                                dx_pixels=dx,
                                dy_pixels=int(ram[0xCF + slot]) - mario_screen_y,
                                relative_velocity_x=0 if previous is None else dx - previous,
                            )
                        )
            names = {0x0D: "piranha_plant", 0x0E: "green_paratroopa", 0x12: "spiny"}
            visible = []
            for enemy in enemies:
                if object_on_screen(ram, enemy.slot):
                    visible.append(replace(enemy, kind=names.get(enemy.kind_id, enemy.kind)))
                else:
                    # Do not learn an offscreen object's velocity before it appears.
                    self._last_enemy_dx.pop(enemy.slot, None)
            return visible

        def _extract_local_grid(self, ram, mario_x, mario_screen_y, enemies):
            if ram is None:
                return []
            observed = bytearray(ram)
            for address in range(0x500, 0x6A0):
                if observed[address] in INVISIBLE_BLOCKS | COINS:
                    observed[address] = 0
            grid = super()._extract_local_grid(observed, mario_x, mario_screen_y, enemies)
            camera = int(ram[0x71A]) * 256 + int(ram[0x71C])
            # Crop the upstream geometry too: masking only the new map would
            # still leak unseen terrain through its hazard/gap summaries.
            right = min(11, (camera + 255) // 16 - mario_x // 16 + 3)
            left = max(0, camera // 16 - mario_x // 16 + 2)
            return [row[left:right] for row in grid] if left < right else []

        def _has_support_below(self, grid):
            if self.ground_contact is not None:
                return self.ground_contact
            return super()._has_support_below(grid)

    return GroundAwareParser(**kwargs)


def tile_symbol(value):
    if value in INVISIBLE_BLOCKS:
        return "."
    if value in COINS:
        return "o"
    if value in QUESTION_BLOCKS:
        return "?"
    if value in PIPE_TILES:
        return "P"
    return "#" if value else "."


def local_map(snapshot, ram, info, *, whole_screen=False):
    if ram is None or len(ram) < 0x71E:
        return {"available": False, "rows": list(snapshot.local_grid)}, []
    # Fixed world tile rows preserve the ground/ceiling during a jump. The old
    # player-centered grid moves vertically and can drop the landing surface.
    origin = max(0, snapshot.x // 16 - 2)
    screen_y = int(info.get("y_pixel", 255 - snapshot.y))
    mario_row = (screen_y - 32) // 16
    mario_col = snapshot.x // 16 - origin
    # The ring buffer also contains old/offscreen columns. Mask those instead of
    # presenting stale coins or platforms as newly observed future terrain.
    camera_x = ram[0x71A] * 256 + ram[0x71C]
    visible_left, visible_right = camera_x // 16, (camera_x + 255) // 16
    if whole_screen:
        # A retreat needs the terrain behind Mario too, but never offscreen tiles.
        origin = visible_left
        mario_col = snapshot.x // 16 - origin
    width = visible_right - visible_left + 1 if whole_screen else 13
    rows, targets = [], []
    for row in range(13):
        cells = []
        for col in range(width):
            world_col = origin + col
            address = 0x500 + ((world_col // 16) % 2) * 208 + row * 16 + world_col % 16
            symbol = (
                tile_symbol(int(ram[address]))
                if visible_left <= world_col <= visible_right
                else "u"
            )
            cells.append(symbol)
            if symbol in "o?":
                targets.append(
                    {
                        "kind": {"o": "coin", "?": "question_block"}[symbol],
                        "x": world_col * 16 + 8,
                        "screen_y": row * 16 + 40,
                        "dx": world_col * 16 + 8 - snapshot.x,
                        "dy": row * 16 + 40 - screen_y,
                    }
                )
        if row == mario_row and 0 <= mario_col < width:
            cells[mario_col] = "M"
        rows.append("".join(cells))
    targets.sort(key=lambda item: abs(item["dx"]) + abs(item["dy"]))
    return {
        "available": True,
        "rows": rows,
        "tile_size_pixels": 16,
        "origin_x": origin * 16,
        "origin_screen_y": 32,
        "player_cell": [mario_row, mario_col],
        "legend": {
            ".": "empty",
            "u": "outside the visible camera; unknown",
            "#": "other nonempty terrain",
            "P": "visible pipe",
            "o": "coin",
            "?": "question block",
            "M": "Mario",
        },
        "coordinates": (
            "rows down, columns right; screen y increases down; targets are tile centers"
        ),
        "limits": (
            "Local RAM buffer, not a full level map. Unknown tiles are not guaranteed "
            "safe landing surfaces. Block contents are unobserved until released."
        ),
    }, targets[:8]


def visible_powerup(snapshot, ram, info):
    # Power-ups use the sixth object slot, which the upstream five-enemy parser
    # intentionally omits. Never relabel an enemy slot as a collectible.
    if not object_on_screen(ram, 5) or not ram[0x14] or ram[0x1B] != 0x2E:
        return None
    x = ram[0x73] * 256 + ram[0x8C]
    if not -64 <= x - snapshot.x <= 176:
        return None
    return {
        "kind": {0: "mushroom", 1: "fire_flower", 2: "star", 3: "extra_life"}.get(
            ram[0x39], "unknown"
        ),
        "dx": x - snapshot.x,
        "dy": ram[0xD4] - int(info.get("y_pixel", 255 - snapshot.y)),
        "emerging": not bool(ram[0x23] & 0x80),
        "note": "Beneficial object, not an enemy. Touch when reachable without an unsafe detour.",
    }


def landing_preview(snapshot, map_state, info, *, anticipate_ascent=False):
    """Conservative local landing hint; no emulator rollout or route knowledge.

    SMB's player coordinate has a 32-pixel offset to the feet (including the
    unused upper sprite area when small). A capped gravity estimate is useful
    for braking, but is never presented as an exact trajectory prediction.
    """
    rows = map_state.get("rows", [])
    if (
        not map_state.get("available")
        or snapshot.grounded
        or (snapshot.dy > 1 and not anticipate_ascent)
    ):
        return None
    center_col = (snapshot.x + 8 - map_state["origin_x"]) // 16
    body_cols = sorted(
        {(snapshot.x + edge - map_state["origin_x"]) // 16 for edge in (0, 8, 15)},
        key=lambda col: abs(col - center_col),
    )
    feet = int(info.get("y_pixel", 255 - snapshot.y)) + 32
    for r, row in enumerate(rows):
        top = 32 + r * 16
        if top < feet or top - feet > 128:
            continue
        # Keep a landing target while any part of Mario overlaps it. Dropping
        # the cue the instant his center crosses an edge makes braking oscillate
        # back to forward movement before his body has actually left the ledge.
        col = next((c for c in body_cols if 0 <= c < len(row) and row[c] in "#?BP"), None)
        if col is None:
            continue
        left = right = col
        while left > 0 and row[left - 1] in "#?BP":
            left -= 1
        while right + 1 < len(row) and row[right + 1] in "#?BP":
            right += 1
        # Include the remaining ascent: waiting until the apex can leave too little
        # horizontal braking distance for a narrow platform already underneath.
        velocity, fallen, frames = (
            (-snapshot.dy if anticipate_ascent else max(0, -snapshot.dy)),
            0,
            0,
        )
        while (fallen < top - feet or velocity < 0) and frames < 64:
            velocity = min(5, velocity + 0.5)
            fallen += velocity
            frames += 1
        x_min = map_state["origin_x"] + left * 16
        x_max = map_state["origin_x"] + (right + 1) * 16
        projected = snapshot.x + 8 + snapshot.dx * frames
        distance = getattr(snapshot, "jump_distance_pixels", None)
        takeoff_x = snapshot.x - distance if distance is not None else None
        leaving_takeoff = bool(
            anticipate_ascent
            and snapshot.dy > 1
            and takeoff_x is not None
            and x_min <= takeoff_x + 8 < x_max
        )
        # While rising away from a ledge, crossing its edge is intentional. Do
        # not confuse that ledge with a new landing and cancel the gap jump.
        beyond_col = (max(projected, x_max + 8) - map_state["origin_x"]) // 16
        # A pipe edge above continuous ground is not a fatal overshoot. Brake
        # for a gap, not merely because the current narrow surface will end.
        lower_support = 0 <= beyond_col < len(row) and any(
            rows[below][beyond_col] in "#?BP" for below in range(r, len(rows))
        )
        return {
            "surface_x": [x_min, x_max],
            "surface_screen_y": top,
            "drop_pixels": top - feet,
            "estimated_frames": frames,
            **(
                {
                    "includes_remaining_ascent": snapshot.dy > 0,
                    "leaving_takeoff_support": leaving_takeoff,
                }
                if anticipate_ascent
                else {}
            ),
            "projected_center_x": projected,
            "brake_before_overshoot": snapshot.dx > 0
            and not leaving_takeoff
            and projected >= x_max - 8
            and not lower_support,
            "lower_support_beyond_edge": lower_support,
            "uncertainty": "Approximate gravity and current speed; terrain solidity unverified.",
        }
    return None


def correct_visible_drop(snapshot, state):
    """Distinguish an elevated platform's edge from a pit using visible floor.

    The upstream height-centered grid can call the space beside a staircase a
    gap while the bottom of the same screen shows continuous ground. Only
    correct that warning when every column of the reported gap has support;
    an unknown column or a real hole keeps the original conservative warning.
    """
    terrain, mapping = state["terrain"], state["local_map"]
    distance, width = terrain.get("gap_distance_tiles"), terrain.get("gap_width_tiles_visible", 0)
    if (
        not terrain.get("gap_ahead")
        or distance is None
        or width < 1
        or not mapping.get("available")
    ):
        return
    rows = mapping["rows"]
    first = (snapshot.x - mapping["origin_x"]) // 16 + distance
    top = max(0, (state["player"]["screen_feet_y"] - 32) // 16)
    floors = []
    for col in range(first, first + width):
        if not rows or not 0 <= col < len(rows[0]):
            return
        support = next((r for r in range(top, len(rows)) if rows[r][col] in "#?BP"), None)
        if support is None:
            return
        floors.append(32 + support * 16)
    terrain.update(
        gap_ahead=False,
        gap_distance_tiles=None,
        gap_width_tiles_visible=0,
        drop_with_visible_floor={
            "floor_screen_y_range": [min(floors), max(floors)],
            "note": (
                "An edge above visible lower terrain, not an observed bottomless pit. "
                "Check enemies before descending."
            ),
        },
    )
    if snapshot.grounded and "last_grounded_preview" in terrain:
        terrain["last_grounded_preview"].update(gap_distance_tiles=None, gap_width_tiles_visible=0)


def collision_timing(snapshot, state):
    """Account for body width and possible acceleration in close ground encounters.

    The upstream estimate divides left-edge separation by the last frame's
    relative speed. That can report 20 safe frames immediately before a four-
    frame collision: Mario has width and can accelerate after choosing run.
    This is a conservative local warning, not an exact collision simulation.
    """
    nearby = [
        enemy
        for enemy in snapshot.enemies
        if 0 <= enemy.dx_pixels <= 48 and abs(enemy.dy_pixels) <= 24
    ]
    if not snapshot.grounded or not nearby:
        return
    enemy = min(nearby, key=lambda item: item.dx_pixels)
    enemy_speed = enemy.relative_velocity_x + snapshot.dx
    closing = max(1, max(3, snapshot.dx) - enemy_speed)
    clearance = max(0, enemy.dx_pixels - 16)
    contact = clearance / closing
    horizon = state.get("reaction_timing", {}).get("total_reaction_horizon_frames", 8)
    deadline = contact - 8
    state["hazard"].update(
        {
            "estimated_contact_frames": round(contact, 1),
            "takeoff_deadline_frames": round(deadline, 1),
            "jump_must_start_this_decision": deadline <= horizon,
            "takeoff_window_already_missed": deadline < 0,
            "contact_within_reaction_horizon": contact <= horizon,
            "projected_distance_after_reaction_pixels": max(0, clearance - closing * horizon),
            "closing_speed_pixels_per_frame": closing,
            "body_clearance_pixels": clearance,
            "timing_basis": (
                "Conservative ground encounter: 16px body clearance and possible run "
                "acceleration. Jump now when warned; waiting for sprite centers to meet is late."
            ),
        }
    )


def visible_pipes(snapshot, ram, info):
    """Describe rendered pipe shapes, never the hidden destination/entry flags.

    These metatiles are the visible cap and left-facing mouth in the standard
    ROM. Identifying a shape does not establish that the pipe is enterable.
    """
    if ram is None or len(ram) < 0x71E:
        return []
    camera = int(ram[0x71A]) * 256 + int(ram[0x71C])
    first, last = camera // 16, (camera + 255) // 16
    feet = int(info.get("y_pixel", 255 - snapshot.y)) + 32

    def tile(col, row):
        if not first <= col <= last or not 0 <= row < 13:
            return None
        return ram[0x500 + ((col // 16) % 2) * 208 + row * 16 + col % 16]

    pipes = []
    for row in range(13):
        for col in range(first, last + 1):
            value, top = tile(col, row), 32 + row * 16
            if value == 0x10 and tile(col + 1, row) == 0x11:
                pipes.append(
                    {
                        "shape": "upward_opening",
                        "x": col * 16,
                        "width": 32,
                        "screen_y": top,
                        "entry_button": "down",
                        "aligned": abs(snapshot.x + 8 - (col * 16 + 16)) <= 8
                        and abs(feet - top) <= 4
                        and snapshot.grounded,
                    }
                )
            elif value == 0x1C and tile(col, row + 1) == 0x1F:
                pipes.append(
                    {
                        "shape": "left_facing_opening",
                        "x": col * 16,
                        "height": 32,
                        "screen_y": top,
                        "entry_button": "right",
                        "aligned": -24 <= snapshot.x - col * 16 <= 0
                        and abs(feet - (top + 32)) <= 4
                        and snapshot.grounded,
                    }
                )
    for pipe in pipes:
        pipe["dx"] = pipe["x"] - snapshot.x
        pipe["feet_above_opening"] = (
            pipe["screen_y"] + (32 if pipe["shape"] == "left_facing_opening" else 0) - feet
        )
        pipe["destination"] = "unobserved; appearance alone does not prove enterability"
    return pipes[:8]


def descent_braking(snapshot, mapping, feet):
    """Flag momentum near a visible left ledge with a lower landing underneath."""
    if not snapshot.grounded or snapshot.dx >= 0 or not mapping.get("available"):
        return None
    row_index = (feet - 32) // 16
    rows = mapping["rows"]
    if not 0 <= row_index < len(rows):
        return None
    row = rows[row_index]
    cols = [(snapshot.x + edge - mapping["origin_x"]) // 16 for edge in (8, 15, 0)]
    col = next((c for c in cols if 0 <= c < len(row) and row[c] in "#?BP"), None)
    if col is None:
        return None
    left = col
    while left > 0 and row[left - 1] in "#?BP":
        left -= 1
    if left == 0 or row[left - 1] != ".":
        return None
    lower = next((i for i in range(row_index + 1, len(rows)) if rows[i][left - 1] in "#?BP"), None)
    if lower is None:
        return None
    edge = mapping["origin_x"] + left * 16
    # This is a conservative stopping-distance cue, not a simulated rollout.
    braking_distance = snapshot.dx**2 / 0.2 + 8
    return {
        "edge_x": edge,
        "lower_surface_screen_y": 32 + lower * 16,
        "countersteer_right_now": (
            snapshot.dx <= -2 and snapshot.x - edge <= max(16, braking_distance)
        )
        or snapshot.x + 15 - edge <= 4,
        "reason": "Brake BEFORE dropping, so leftward momentum does not miss the lower floor.",
    }


def movement_feedback(snapshot, history, ram=None):
    """Report observed control effects separately from the longer grounded stall.

    A direction can be blocked while Mario is airborne. Waiting for eight
    grounded attempts loses that evidence during repeated jumping at the
    camera boundary. This reports displacement without forbidding any move.
    """
    recent = list(history)[-8:]
    attempts = [
        {
            "action": row["action"],
            "dx": row["end_x"] - row["x_pos"],
            "dy": row["end_y"] - row["y_pos"],
            "forced": bool(row.get("forced")),
        }
        for row in recent
        if all(k in row for k in ("action", "end_x", "x_pos", "end_y", "y_pos"))
    ]
    blocked = []
    for direction in ("left", "right"):
        last = attempts[-3:]
        if (
            len(last) == 3
            and all(
                a["action"].startswith(direction) and abs(a["dx"]) <= 1 and not a["forced"]
                for a in last
            )
            and abs(snapshot.x - recent[-1]["end_x"]) <= 2
        ):
            blocked.append(direction)
    camera = None
    if ram is not None and len(ram) >= 0x71E:
        left = int(ram[0x71A]) * 256 + int(ram[0x71C])
        camera = {
            "left_x": left,
            "right_x": left + 255,
            "distance_from_left_edge": snapshot.x - left,
            "near_left_limit": snapshot.x <= left + 8,
            "note": (
                "The camera does not scroll backward. Holding left at its left limit "
                "cannot reveal or reach earlier terrain."
            ),
        }
    return {"recent_attempts": attempts, "blocked_directions": blocked, "camera": camera}


def stall_recovery(snapshot, history):
    """Detect repeated ineffective attempts, not ordinary braking or coin collecting."""
    recent = list(history)[-8:]
    if not snapshot.grounded or len(recent) < 8 or any("end_x" not in row for row in recent):
        return None
    grounded_heights = [snapshot.y]
    for row in recent:
        if row.get("grounded"):
            grounded_heights.append(row["y_pos"])
        if row.get("end_grounded"):
            grounded_heights.append(row["end_y"])
    # Climbing a step can pause x while making useful vertical progress. Require
    # a repeated support height, not merely a lack of forward distance.
    if len(grounded_heights) < 2 or max(grounded_heights) - min(grounded_heights) > 8:
        return None
    # Both endpoints matter: a productive detour must not look like standing still.
    positions = [x for row in recent for x in (row["x_pos"], row["end_x"])]
    if max(positions) - min(positions) > 24 or abs(snapshot.x - positions[-1]) > 8:
        return None
    if abs(positions[-1] - positions[0]) > 8:
        return None  # Slow but useful movement is not an unproductive loop.
    if any(
        row.get("forced")
        or row.get("coins_after") != row.get("coins_before")
        or row.get("score_after") != row.get("score_before")
        for row in recent
    ):
        return None
    if sum(row["action"].startswith(("right", "left")) for row in recent) < 4:
        return None
    blocked_left = (
        sum(
            row["action"] in {"left", "left_run"} and abs(row["end_x"] - row["x_pos"]) < 2
            for row in recent
        )
        >= 2
    )
    ineffective = {
        action: sum(row["action"] == action for row in recent)
        for action in {row["action"] for row in recent}
        if sum(row["action"] == action for row in recent) >= 2
    }
    return {
        "ineffective_actions": ineffective,
        "observation": (
            "Eight attempts stayed within 24 pixels at the same grounded height, "
            "without a coin or score gain."
        ),
        "recent_attempts": [
            {"action": row["action"], "from_x": row["x_pos"], "to_x": row["end_x"]}
            for row in recent
        ],
        "left_max_frames": 8 if any(a.startswith("left") for a in ineffective) else 64,
        "left_walk_blocked": blocked_left,
        "other_action_frames": 8,
        "purpose": (
            "Compare the observed failed actions with visible terrain on BOTH sides. "
            "A leftward loop needs a different approach just as a rightward loop does. "
            "Do not repeat an ineffective action just because it is called a retreat. "
            "If walking is blocked by a visible obstacle, an untried jump may help; "
            "if jumping that way also failed, reassess another direction or entrance. "
            "left is held for up to left_max_frames, ending early at an enemy, the "
            "camera's left edge, a 32-pixel descent, or eight stationary frames. "
            "All other moves last eight frames, then you decide again."
        ),
    }


def action_frames(state, action, default=8):
    """Honor only the extended move explicitly offered in this Jev request."""
    recovery = state.get("stall_recovery") or {}
    return recovery.get("left_max_frames", default) if action == "left" else default


def retreat_finished(before, after, ram):
    """Return control on new local danger/geometry instead of a blind long hold."""
    camera = int(ram[0x71A]) * 256 + int(ram[0x71C])
    return (
        after.x <= camera + 8
        or before.y - after.y >= 32
        or any(-40 <= e.dx_pixels <= 16 and abs(e.dy_pixels) <= 32 for e in after.enemies)
    )


class DecisionContext:
    """Expose richer state while preserving the snapshot interface used by policies."""

    def __init__(self, snapshot, evidence=(), *, ram=None, info=None, recent_actions=()):
        self.snapshot = snapshot
        self.evidence = evidence
        # Freeze the request input: the emulator mutates its RAM on the next step.
        self.ram = bytes(ram) if ram is not None else None
        self.info = dict(info or {})
        self.recent_actions = [dict(row) for row in recent_actions][-8:]

    def __getattr__(self, name):
        return getattr(self.snapshot, name)

    def to_state(self):
        state = deepcopy(self.snapshot.to_state())
        collision_timing(self.snapshot, state)
        state["objective"] = (
            f"Play World {self.snapshot.world}-{self.snapshot.stage} skillfully: survive, "
            "make steady progress, collect reachable coins and useful power-ups, and clear "
            "the stage. Take safe collectible opportunities. Do not chase unseen rewards, "
            "repeat stalled moves, or risk a known gap for a coin."
        )
        state["observation_version"] = 2
        state["performance"] = {
            "coins": self.snapshot.coins,
            "score": self.snapshot.score,
            "powerup_status": self.snapshot.status,
        }
        state["pipes"] = visible_pipes(self.snapshot, self.ram, self.info)
        interaction = pipe_feedback(state["pipes"], self.snapshot, self.recent_actions)
        if interaction:
            state["pipe_interaction"] = interaction
        state["player"]["screen_feet_y"] = int(self.info.get("y_pixel", 255 - self.snapshot.y)) + 32
        state["movement_feedback"] = movement_feedback(self.snapshot, self.recent_actions, self.ram)
        recovery = stall_recovery(self.snapshot, self.recent_actions)
        if recovery and (state["movement_feedback"]["camera"] or {}).get("near_left_limit"):
            recovery["left_max_frames"] = 8
        if recovery:
            state["stall_recovery"] = recovery
        state["local_map"], state["collectibles"] = local_map(
            self.snapshot,
            self.ram,
            self.info,
            whole_screen=bool(recovery)
            or any(p["shape"] == "left_facing_opening" for p in state["pipes"]),
        )
        if any(
            p["shape"] == "left_facing_opening" and p["feet_above_opening"] > 32
            for p in state["pipes"]
        ):
            state["pipe_descent"] = pipe_descent(
                self.snapshot, state["local_map"], state["player"]["screen_feet_y"], state["pipes"]
            )
            state["descent_braking"] = descent_braking(
                self.snapshot, state["local_map"], state["player"]["screen_feet_y"]
            )
            if (state["descent_braking"] or {}).get("countersteer_right_now"):
                state.pop("stall_recovery", None)
        correct_visible_drop(self.snapshot, state)
        # Analyze the entire visible camera so a takeoff ledge remains represented
        # during a jump. This never includes offscreen RAM-buffer columns.
        visible_map, _ = local_map(self.snapshot, self.ram, self.info, whole_screen=True)
        geometry = platform_observation(self.snapshot, visible_map, self.info)
        # Keep ordinary floor/pipe requests unchanged. These extra platform and
        # ascent cues are for visible gaps, not every jump over continuous floor.
        if geometry and not geometry["has_visible_gap"]:
            geometry = None
        enrich_gap_state(self.snapshot, state, geometry)
        # A timing feature helps the model act before it has already run past a
        # block. It is advisory only: Jev still selects every normal-play action.
        state["landing"] = landing_preview(
            self.snapshot, state["local_map"], self.info, anticipate_ascent=bool(geometry)
        )
        contact = state["hazard"].get("estimated_contact_frames")
        urgent = (contact is not None and contact <= 48) or any(
            abs(enemy.dx_pixels) <= 96 and abs(enemy.dy_pixels) <= 48
            for enemy in self.snapshot.enemies
        )
        gap = state["terrain"].get("gap_distance_tiles")
        obstacle = state["terrain"].get("obstacle_distance_tiles")
        state["gap_run_up"] = bool(
            self.snapshot.grounded
            and self.snapshot.dx < 3
            and gap is not None
            and (
                gap >= 4
                or (
                    ((geometry or {}).get("edge") or {}).get("gap_beyond")
                    and not ((geometry or {}).get("edge") or {}).get(
                        "jump_must_start_this_decision"
                    )
                )
            )
            and (obstacle is None or obstacle > 3)
            and not urgent
            and not ((geometry or {}).get("edge") or {}).get("jump_must_start_this_decision")
        )
        candidates = [
            t
            for t in state["collectibles"]
            if t["kind"] == "question_block"
            and t["x"] >= state["episode"]["best_progress"] - 32
            and 0 <= t["dx"] <= 40
            and -64 <= t["dy"] <= -8
        ]
        state["collection_opportunity"] = {
            "block_bump_window": bool(
                candidates and self.snapshot.grounded and not urgent and (gap is None or gap > 2)
            ),
            "target": candidates[0] if candidates else None,
            "reason": "Nearby overhead block; only pursue if block_bump_window is true."
            if candidates
            else "No nearby overhead block aligned for a bump.",
        }
        opportunity = state["collection_opportunity"]
        if opportunity["block_bump_window"]:
            target = opportunity["target"]
            # A block must be hit from below. Horizontal momentum can carry Mario
            # past its underside before the jump reaches it; align first.
            projected_center = self.snapshot.x + 8 + max(0, self.snapshot.dx) * 8
            opportunity["alignment"] = (
                "brake_left_then_reassess" if projected_center > target["x"] + 6 else "jump_now"
            )
        state["powerup"] = visible_powerup(self.snapshot, self.ram, self.info)
        if state["powerup"]:
            state["powerup"]["pursuit_safe"] = not urgent and not state["trajectory"].get(
                "crossing_known_gap", False
            )
        scene, state["visible_enemies"] = enemy_scene(
            self.snapshot, visible_map, state["player"]["screen_feet_y"] - 32
        )
        if scene:
            state["scene_map"] = scene
        # These are observations of old attempts, not causal claims about the move.
        level = state["level"]
        prefix = f"{level['world']}:{level['stage']}:{level['area']}:"
        memories = []
        for entry in self.evidence:
            if (
                not entry.get("context", "").startswith(prefix)
                or abs(entry.get("x_pos", -10000) - self.snapshot.x) > 192
            ):
                continue
            if entry.get("kind") == "experiment":
                memories.append(
                    {
                        "hazard_x": entry["x_pos"],
                        "sequence": entry["label"],
                        "outcome": entry["outcome"],
                        "limit": "Passed a local threshold, not proven safe afterward."
                        if entry["outcome"] == "survived"
                        else "Death during this attempt; cause uncertain.",
                    }
                )
            else:
                memories.append(
                    {
                        "x": entry["x_pos"],
                        "action": entry["action"],
                        "observation": (
                            "Occurred before a death; cause uncertain. Reconsider "
                            "timing and approach."
                        ),
                    }
                )
        state["previous_attempts"] = memories[-4:]
        return state


def enrich_questions(state, questions):
    """Retain the upstream questions, replacing its finish-only goal explicitly."""
    if state.get("observation_version") != 2:
        return questions
    from typesafe_sdk import Choice

    questions = dict(questions)
    original = questions["next_action"]
    instructions = dict(original.instructions)
    instructions.update(
        {
            "goal": state["objective"],
            "priority": (
                "1. Avoid an immediate death. If landing.brake_before_overshoot is true, use left "
                "briefly to brake onto the platform below you, then reassess. 2. When a powerup "
                "is within 64 pixels AND pursuit_safe is true, prioritize collecting it over "
                "moving ahead: wait nearby "
                "while it emerges; if it is behind, move left toward it once it is reachable. "
                "Do not abandon a mushroom just released from a block. 3. When "
                "collection_opportunity.block_bump_window "
                "is true, follow its alignment: brake_left_then_reassess means briefly use left "
                "to shed momentum; jump_now means jump (no right) if already underneath or "
                "right_jump if approaching. Running "
                "past wastes this opportunity. 4. Otherwise advance toward safe landing "
                "terrain. Once airborne, steer to land on a platform instead of overshooting."
            ),
            "trajectory": (
                "While rising, keep the selected direction's jump held to sustain height: "
                "left_jump/left_run_jump for a leftward jump, right_jump/right_run_jump "
                "for a rightward jump. Do not reverse a leftward jump just because the "
                "stage's usual progress direction is right. "
                "releasing A early cuts the jump short. Being airborne does NOT mean release A. "
                "When grounded and gap_run_up is true, use right_run to build speed BEFORE "
                "jumping. A slow jump too far from the edge can fall short; do not "
                "automatically jump again the moment you land. "
                "Preserve speed over a gap when no landing surface is underneath. "
                "When landing identifies a platform beneath Mario and predicts overshoot, "
                "briefly countersteer left to land on it; this takes priority over the stale "
                "crossing_known_gap flag. Do not continuously hold forward past a platform edge."
            ),
            "powerup": (
                "The powerup field identifies a beneficial moving object, not an enemy. "
                "When a mushroom or flower is nearby, approach or jump toward it if reachable "
                "safely; do not run away and abandon it. An emerging item needs time to emerge."
            ),
            "map": (
                "Use local_map and collectibles to locate nearby coins, question blocks "
                "and landing terrain. terrain.drop_with_visible_floor distinguishes a ledge "
                "above visible ground from a bottomless pit. Do not retreat just because "
                "a staircase ends above a lower floor. M marks Mario; y increases downward. "
                "Targets are "
                "observations, not promises that a jump can reach them."
            ),
            "pipes": (
                "P marks visible pipe tiles. A left_facing_opening may be a passage: "
                "approach at the mouth's height and hold right without jumping. When "
                "aligned on an upward_opening, down attempts entry. A pipe's destination "
                "and enterability are unknown until observed. A side opening below Mario "
                "requires descending to its floor; running along the ceiling above it "
                "misses the entrance. Use a visible edge to descend safely."
            ),
            "collect": (
                "Collect coins along safe forward paths. When safe and under a "
                "reachable question block, jump to hit its underside; slow down to "
                "align instead of running past. Avoid enemies first. Do not keep "
                "jumping at a used or unreachable block."
            ),
            "memory": (
                "Review previous_attempts. Vary jump timing or approach when a prior "
                "attempt died here; a failed action is not proof of causality."
            ),
            "timing": (
                "The selected action is held for 8 emulator frames. Game time pauses "
                "during API calls."
            ),
        }
    )
    if state.get("scene_map"):
        instructions["map"] += (
            " scene_map shows terrain and visible enemies together across the camera. "
            "Numbers 1-5 match visible_enemies.map_marker; map_cell links their row/column "
            "to type, relative position and velocity. M is Mario, X is an enemy sharing "
            "Mario's cell, and * marks multiple enemies. These are approximate position "
            "anchors, not collision outlines. Check both the landing and nearby enemies "
            "before committing; use hazard timing too. local_map keeps terrain symbols "
            "without enemy overlays. The maps may have different origin_x values."
        )
    # Describe the immediate effect of each candidate in the current context.
    # No action is removed or silently substituted: this remains a Jev Choice.
    criteria = dict(original.criteria)
    powerup = state.get("powerup")
    landing = state.get("landing") or {}
    opportunity = state.get("collection_opportunity", {})
    hints = {}
    side_pipe = next(
        (p for p in state.get("pipes", []) if p["shape"] == "left_facing_opening"), None
    )
    if side_pipe:
        instructions["priority"] = (
            "Avoid immediate danger first. Investigate the visible side-facing pipe "
            "passage. Align with its mouth at the lower floor before walking into it. "
            "Do not keep moving along a ceiling above the opening. " + instructions["priority"]
        )
        if not any(p["shape"] == "upward_opening" and p["aligned"] for p in state["pipes"]):
            hints["down"] = (
                "Does not descend through solid floors or enter a side-facing mouth. "
                "Walk to a visible ledge to descend; enter this mouth horizontally with right."
            )
        if side_pipe["feet_above_opening"] > 32:
            hints.update(
                left_jump=(
                    "Jumps upward away from the lower entrance; walking off an edge descends."
                ),
                left_run_jump=(
                    "Jumps farther away from the lower entrance; avoid overshooting its floor."
                ),
                right_jump="Jumps farther above a pipe entrance that is below Mario.",
                right_run_jump="Jumps farther above a pipe entrance that is below Mario.",
            )
            if descent := state.get("pipe_descent"):
                direction = descent["direction"]
                opposite = "right" if direction == "left" else "left"
                instructions["pipe_approach"] = (
                    f"A visible {direction} ledge at x={descent['edge_x']} leads down "
                    f"{descent['drop_pixels']} pixels to a floor with clear space to the pipe. "
                    f"Walk {direction} without jumping to descend, managing momentum; "
                    "then approach the mouth at floor height. Down cannot drop through bricks."
                )
                hints[direction] = f"Walks toward the visible {direction} drop to the pipe's floor."
                hints[f"{direction}_run"] = "Approaches the drop faster; manage landing momentum."
                hints[opposite] = "Moves away from the observed descent edge; useful only to brake."
            if state["player"].get("vertical_speed_px_per_frame", 0) < 0:
                hints.update(
                    right="Steer toward the visible pipe while descending onto its lower floor.",
                    left="Continues away from the pipe and its lower floor during descent.",
                )
                instructions["priority"] = (
                    "You are descending toward a lower pipe entrance. Countersteer right "
                    "toward its floor instead of continuing left away from the landing. "
                    + instructions["priority"]
                )
    if landing.get("brake_before_overshoot"):
        hints = {
            "left": "Brakes horizontal momentum to land on the platform below.",
            "right": "Continues toward the platform edge; projected overshoot.",
            "right_run": "Maintains speed toward a projected platform overshoot.",
            "right_jump": "Cannot restart a jump midair; keeps moving toward overshoot.",
        }
    elif (
        state.get("platforms")
        and landing
        and not landing.get("leaving_takeoff_support")
        and not landing.get("lower_support_beyond_edge")
        and (state.get("recent_control", {}).get("action") or "").startswith("left")
        and state["player"].get("horizontal_speed_px_per_frame", 0) > 0
        and landing["surface_x"][0] + 8
        <= landing["projected_center_x"]
        < landing["surface_x"][1] - 8
    ):
        instructions["priority"] = (
            "Current momentum projects a landing inside the visible platform. Stop "
            "countersteering left now; coast briefly and reassess instead of losing all "
            "forward speed. " + instructions["priority"]
        )
        hints.update(
            noop="Coasts toward the currently projected landing without extra acceleration.",
            left="Further braking is not currently required for this landing.",
        )
    elif powerup and powerup.get("pursuit_safe", False) and -64 <= powerup["dx"] < -16:
        hints = {
            "left": "Moves toward the visible beneficial power-up behind Mario.",
            "right": "Moves away from the visible power-up behind Mario.",
            "right_run": "Runs farther away from the visible power-up.",
            "right_jump": "Jumps away from the visible power-up behind Mario.",
        }
    elif opportunity.get("alignment") == "brake_left_then_reassess":
        hints = {
            "left": "Brakes to align under the nearby question block before jumping.",
            "right_run": "Overshoots the question block before a jump could hit it.",
        }
    elif state.get("gap_run_up"):
        hints = {
            "right_run": "Builds speed on safe ground before the upcoming gap jump.",
            "right_jump": "Jumps too early at walking speed; may land short in the gap.",
            "right_run_jump": "Starts the jump before building running speed on the ground.",
        }
    geometry = state.get("platforms") or {}
    edge = geometry.get("edge") or {}
    if geometry:
        instructions["platforms"] = (
            "If landing.leaving_takeoff_support is true, continue the jump toward the next "
            "platform; do not brake to land back on the ledge just left. "
            "platforms describes visible platform tops at different heights. Use their "
            "x spans and screen_y to choose a landing; empty space at the starting height "
            "can still have a higher or lower platform ahead. A candidate is not a guarantee "
            "of reachability. Jump must start on support; pressing jump after walking off "
            "the edge cannot create a new jump. Sustain A while rising, steer to a visible "
            "landing, and reassess after landing."
        )
    if edge.get("jump_must_start_this_decision"):
        instructions["priority"] = (
            "The visible gap or higher landing needs takeoff during this decision. "
            "Begin the forward jump "
            "NOW while grounded, aiming for a visible landing. Do not spend another full "
            "macro walking/running toward the edge or collecting optional rewards. "
            + instructions["priority"]
        )
        hints.update(
            right=(
                "Waiting another macro can miss the ledge or leave too little room "
                "to gain landing height."
            ),
            right_run=(
                "The takeoff window is now; another running macro risks a fall "
                "or hitting the higher ledge."
            ),
            right_jump="Begin the jump now, sustaining it while rising toward a visible landing.",
            right_run_jump=(
                "Begin a running jump now to cross the visible gap, sustaining jump height."
            ),
        )
    elif geometry.get("airborne_over_gap"):
        instructions["gap_crossing"] = (
            "Mario is airborne without visible static support below. The crossing is "
            "not finished. Use the visible landing candidates and current velocity; "
            "continue the jump while rising, and do not mistake empty space for safe ground."
        )
    if (state.get("descent_braking") or {}).get("countersteer_right_now"):
        instructions["priority"] = (
            "Countersteer RIGHT now to brake leftward momentum near the visible ledge. "
            "Waiting until already falling will carry Mario past the lower floor. "
            "Do not jump upward. Reassess after slowing down. " + instructions["priority"]
        )
        hints.update(
            right="Countersteers leftward momentum before the ledge; prepares the lower landing.",
            left="Keeps leftward speed; risks overshooting the lower floor after the drop.",
            left_jump="Jumps away from the intended lower floor.",
            right_jump="A jump delays the intended descent; brake without jumping first.",
        )
    if recovery := state.get("stall_recovery"):
        instructions["stall"] = recovery["purpose"]
        instructions["geometry"] = (
            "Read the visible map, including terrain behind Mario. Repeated jumps "
            "with no horizontal progress can mean a wall or low ceiling blocks that "
            "approach. Retreating to another height or approach is valid progress. "
            "Do not treat every nearby obstacle as requiring another forward jump."
        )
        instructions["priority"] = (
            "Avoid immediate danger first. Then escape the observed repeated-motion "
            "loop: choose a meaningfully different approach instead of repeating "
            "the failed moves in either direction. " + instructions["priority"]
        )
        instructions["timing"] = (
            f"left is held for up to {recovery['left_max_frames']} frames, with the "
            "early stops described in stall_recovery. All other moves last 8 frames. "
            "Game time pauses during API calls."
        )
        for action, count in recovery["ineffective_actions"].items():
            hints[action] = (
                f"Chosen {count} times in the stalled attempts without a useful change "
                "in position or reward. Reassess a different approach using the visible map."
            )
        if recovery["left_max_frames"] > 8:
            hints["left"] = (
                "Untried longer retreat to change approach; inspect visible terrain behind."
            )
    feedback = state.get("movement_feedback") or {}
    blocked = feedback.get("blocked_directions", [])
    camera = feedback.get("camera") or {}
    if blocked:
        instructions["movement_feedback"] = (
            "The last three commands in blocked_directions produced at most one pixel "
            "of horizontal movement each. This is observed, not a guess about the cause. "
            "Inspect the visible boundary or obstacle and change the approach. An "
            "airborne jump can still change height; do not treat this as a new jump opportunity."
        )
    if camera.get("near_left_limit"):
        instructions["camera_boundary"] = (
            "Mario is at the screen's left boundary. The camera does not scroll left: "
            "left, left_jump, and left_run_jump cannot get past this boundary. "
            "Choose a safe approach into the visible playfield to the right. If airborne, "
            "steer toward a visible landing and sustain jump height when needed. "
            "Do not pursue a target beyond this boundary."
        )
        for action in ("left", "left_jump", "left_run", "left_run_jump"):
            hints[action] = "Pushes into the left camera boundary; cannot travel beyond it."
        if "left" in blocked:
            instructions["priority"] = (
                "Repeated left inputs are blocked at the camera boundary. Reassess toward "
                "the visible playfield; another left jump will not move the boundary. "
                + instructions["priority"]
            )
    # Pipe orientation applies to aligned mouths only, after generic wall/stall
    # hints. Repeated down presses must not hide a side-facing entry direction.
    pipe_choice_hints(state, instructions, hints)
    for action, hint in hints.items():
        if action in criteria:
            criteria[action] = {"control": criteria[action], "current_effect": hint}
    instructions["momentum"] = (
        "noop releases buttons but Mario keeps coasting. left brakes RIGHTWARD momentum; "
        "once horizontal speed reaches zero, holding left moves backward. right brakes "
        "LEFTWARD momentum. Stop braking once the required speed or alignment is reached. "
        "Jump without right also retains momentum. Holding A while rising sustains the "
        "current jump; it cannot start a second jump until Mario lands."
    )
    questions["next_action"] = Choice(criteria=criteria, instructions=instructions)
    return questions

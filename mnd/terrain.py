"""Visible platform geometry and action-horizon warnings, without level routes."""

from math import ceil

SOLID = frozenset("#?BP")


def surfaces(mapping):
    """Group exposed tile tops; cropped ends are not known platform edges."""
    if not mapping.get("available"):
        return []
    rows, origin = mapping["rows"], mapping["origin_x"]
    result = []
    for r, row in enumerate(rows):
        c = 0
        while c < len(row):
            exposed = row[c] in SOLID and (r == 0 or rows[r - 1][c] in ".oM")
            if not exposed:
                c += 1
                continue
            left = c
            while (
                c + 1 < len(row) and row[c + 1] in SOLID and (r == 0 or rows[r - 1][c + 1] in ".oM")
            ):
                c += 1
            result.append(
                {
                    "x": [origin + left * 16, origin + (c + 1) * 16],
                    "screen_y": 32 + r * 16,
                    "width_pixels_visible": (c - left + 1) * 16,
                    # An adjacent higher step is not a platform edge over empty air.
                    "right_edge_known": c + 1 < len(row) and row[c + 1] in ".oM",
                }
            )
            c += 1
    return result


def column_floor(mapping, world_x, feet):
    """Return visible lower support, known empty space, or unknown camera data."""
    rows = mapping["rows"]
    col = (world_x - mapping["origin_x"]) // 16
    if not rows or not 0 <= col < len(rows[0]):
        return "unknown"
    start = max(0, ceil((feet - 32) / 16))
    for row in range(start, len(rows)):
        value = rows[row][col]
        if value == "u":
            return "unknown"
        if value in SOLID:
            return 32 + row * 16
    return None


def platform_observation(snapshot, mapping, info, *, horizon=8):
    """Describe visible takeoff/landing geometry rather than infer a winning jump.

    The center-to-edge margin is conservative: do not rely on the last pixels
    of body overlap to start a jump. The timing bound allows running speed in
    the next macro, including acceleration from a currently slow approach.
    """
    if not mapping.get("available"):
        return None
    feet = int(info.get("y_pixel", 255 - snapshot.y)) + 32
    center = snapshot.x + 8
    platforms = surfaces(mapping)
    support = next(
        (
            p
            for p in platforms
            if snapshot.grounded
            and abs(p["screen_y"] - feet) <= 4
            and p["x"][0] < snapshot.x + 16
            and p["x"][1] > snapshot.x
        ),
        None,
    )
    # Give Jev several visible choices. A top is not a promise it is reachable:
    # ceilings, enemies, and momentum still matter. Nothing offscreen is added.
    ahead = [
        dict(p, height_above_feet=feet - p["screen_y"])
        for p in platforms
        if p != support
        and p["x"][1] > center
        and -128 <= feet - p["screen_y"] <= 96
        and (support is None or p["x"][0] >= support["x"][1])
    ]
    ahead.sort(key=lambda p: (max(0, p["x"][0] - center), abs(p["screen_y"] - feet)))
    ahead = ahead[:5]
    below = [column_floor(mapping, snapshot.x + edge, feet) for edge in (0, 8, 15)]
    over_gap = not snapshot.grounded and all(value is None for value in below)
    result = {
        "has_visible_gap": any(
            column_floor(mapping, mapping["origin_x"] + col * 16 + 8, feet) is None
            for col in range(len(mapping["rows"][0]))
        ),
        "current_support": support,
        "landing_candidates": ahead,
        "airborne_over_gap": over_gap,
        "edge": None,
        "limits": (
            "Visible static tile tops only; moving platforms and exact jump reach are not "
            "predicted. Screen y increases downward."
        ),
    }
    if support and support["right_edge_known"]:
        edge = support["x"][1]
        floor = column_floor(mapping, edge + 8, support["screen_y"])
        distance = edge - center
        speed = max(0, snapshot.dx)
        # Bound acceleration over one decision, rather than pretending a stopped
        # Mario already travels at full speed. Otherwise a narrow platform
        # offers no run-up and the next jump repeatedly starts from rest.
        advance_bound = sum(min(3, speed + 0.25 * frame) for frame in range(1, horizon + 2))
        next_platform = ahead[0] if ahead else None
        gap = floor is None
        # A higher platform requires height before reaching its near face, even
        # when the horizontal gap is short. This bounded estimate uses visible
        # height and current speed, not a level-specific jump location.
        rise_frames = ceil(max(0, next_platform["height_above_feet"]) / 4) if next_platform else 0
        height_urgent = bool(
            gap
            and rise_frames
            and next_platform["x"][0] - center <= max(1, speed) * rise_frames + 8 + advance_bound
        )
        result["edge"] = {
            "x": edge,
            "distance_from_center_pixels": distance,
            "frames_at_current_speed": round(max(0, distance) / speed, 1) if speed else None,
            "gap_beyond": gap,
            "lower_floor_screen_y": floor,
            "gap_width_pixels": max(0, next_platform["x"][0] - edge)
            if gap and next_platform
            else None,
            "next_landing": next_platform,
            "higher_landing_needs_early_takeoff": height_urgent,
            "jump_must_start_this_decision": gap
            and (distance <= advance_bound + 4 or height_urgent),
            "reason": (
                "Another movement macro may leave the support before the next decision. "
                "A jump must begin while still grounded."
            ),
        }
    return result


def enrich_gap_state(snapshot, state, geometry):
    """Keep the summary consistent with the visible geometry during a crossing."""
    if not geometry:
        return
    state["platforms"] = geometry
    edge = geometry["edge"]
    terrain = state["terrain"]
    if edge and edge["gap_beyond"]:
        terrain.update(
            gap_ahead=True,
            gap_distance_tiles=max(0, ceil(edge["distance_from_center_pixels"] / 16)),
            gap_width_tiles_visible=ceil(edge["gap_width_pixels"] / 16)
            if edge["gap_width_pixels"] is not None
            else None,
        )
    elif geometry["airborne_over_gap"]:
        # The upstream player-centered grid can call a pit "clear" after Mario
        # leaves the ledge. Empty space below him must retain a hazard warning.
        terrain.update(gap_ahead=True, gap_distance_tiles=0, clear_forward_tiles=0)
        terrain["gap_warning_basis"] = "No visible static support below the airborne player."

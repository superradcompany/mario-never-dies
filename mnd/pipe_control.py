"""Visible approaches and observed entry feedback for pipe mouths."""

from collections import Counter

from .terrain import SOLID, column_floor, surfaces


def pipe_descent(snapshot, mapping, feet, pipes):
    """Find a visible drop whose lower floor connects to a side-facing mouth.

    Check both support edges. A ledge on the opposite side of a solid raised
    platform is not a route through that platform, and a camera crop is not an
    observed drop. This is geometry only, not a pipe destination or rollout.
    """
    if not snapshot.grounded or not mapping.get("available"):
        return None
    support = next(
        (
            p
            for p in surfaces(mapping)
            if abs(p["screen_y"] - feet) <= 4
            and p["x"][0] < snapshot.x + 16
            and p["x"][1] > snapshot.x
        ),
        None,
    )
    if support is None:
        return None
    rows, origin = mapping["rows"], mapping["origin_x"]
    routes = []
    body_rows = 1 if snapshot.status == "small" else 2
    for pipe in pipes:
        if pipe["shape"] != "left_facing_opening" or pipe["feet_above_opening"] <= 32:
            continue
        floor_y = pipe["screen_y"] + pipe["height"]
        floor_row = (floor_y - 32) // 16
        if not body_rows <= floor_row < len(rows):
            continue
        mouth_col = (pipe["x"] - 1 - origin) // 16
        for direction, edge, landing_x in (
            ("left", support["x"][0], support["x"][0] - 8),
            ("right", support["x"][1], support["x"][1] + 8),
        ):
            if landing_x >= pipe["x"] or column_floor(mapping, landing_x, feet) != floor_y:
                continue
            landing_col = (landing_x - origin) // 16
            # Require visible floor AND body clearance all the way to the mouth.
            # Unknown cells or a raised block invalidate this direct approach.
            if not 0 <= landing_col <= mouth_col < len(rows[0]):
                continue
            if any(
                rows[floor_row][col] not in SOLID
                or any(rows[r][col] not in ".oM" for r in range(floor_row - body_rows, floor_row))
                for col in range(landing_col, mouth_col + 1)
            ):
                continue
            routes.append(
                dict(
                    direction=direction,
                    edge_x=edge,
                    distance_to_edge_pixels=abs(edge - (snapshot.x + 8)),
                    lower_floor_screen_y=floor_y,
                    pipe_x=pipe["x"],
                    drop_pixels=floor_y - feet,
                    limit="Visible static floor and clearance only; check enemies and momentum.",
                )
            )
    return min(routes, key=lambda r: r["distance_to_edge_pixels"]) if routes else None


def pipe_feedback(pipes, snapshot, history):
    """Count stationary attempts at this mouth, without reading hidden entry flags.

    Movement, a different location, or a forced experiment ends the evidence.
    Pipe-entry animation therefore is not treated as a failed stationary input.
    """
    aligned = [pipe for pipe in pipes if pipe["aligned"]]
    if not aligned:
        return None
    pipe = min(aligned, key=lambda p: abs(p["dx"]))
    attempts = []
    for row in reversed(list(history)[-8:]):
        if row.get("forced") or not all(
            abs(row.get(key, float("inf")) - position) <= 1
            for key, position in (
                ("x_pos", snapshot.x),
                ("end_x", snapshot.x),
                ("y_pos", snapshot.y),
                ("end_y", snapshot.y),
            )
        ):
            break
        attempts.append(row["action"])
    counts = dict(Counter(attempts))
    expected = ("right", "right_run") if pipe["entry_button"] == "right" else ("down",)
    entry_attempts = sum(counts.get(action, 0) for action in expected)
    return {
        "shape": pipe["shape"],
        "x": pipe["x"],
        "entry_button": pipe["entry_button"],
        "stationary_inputs": counts,
        "stationary_entry_attempts": entry_attempts,
        "entry_not_observed": entry_attempts >= 4,
        "limit": "Visible alignment and recent movement only; entry and destination unknown.",
    }


def pipe_choice_hints(state, instructions, hints):
    """Explain orientation and stop unproductive entry loops; Jev keeps all choices."""
    interaction = state.get("pipe_interaction")
    if not interaction:
        return
    button = interaction["entry_button"]
    if button == "right":
        instruction = (
            "Mario is aligned with a LEFT-FACING pipe mouth. Its entry direction is RIGHT, "
            "not down. Try right while staying on the ground; right_run also presses right. "
            "Do not treat this visible opening as a wall that must always be jumped over. "
        )
        hints.update(
            right="Presses into the aligned side-facing mouth to attempt entry.",
            right_run="Also presses right into the aligned side-facing mouth.",
            down="Does not enter this side-facing mouth; down is for an upward-facing opening.",
            right_jump="Jumping moves out of the mouth's grounded entry alignment.",
            right_run_jump="Jumping moves out of the mouth's grounded entry alignment.",
        )
    else:
        instruction = (
            "Mario is aligned on top of an UPWARD-FACING pipe. Down attempts entry; "
            "right walks off the top rather than entering. Appearance does not prove "
            "that the pipe accepts entry. "
        )
        hints["down"] = "Attempts entry into the upward-facing opening underneath Mario."
    if interaction["entry_not_observed"]:
        instruction += (
            "However, at least four correctly directed entry attempts here produced no "
            "position change. Stop repeating that entry input for now. The pipe may not "
            "accept entry; choose a different approach or visible route. "
        )
        for action in ("right", "right_run") if button == "right" else ("down",):
            hints[action] = "Repeated aligned entry attempts have produced no movement; reassess."
    instructions["priority"] = (
        "Avoid immediate enemy or fall danger first. " + instruction + instructions["priority"]
    )

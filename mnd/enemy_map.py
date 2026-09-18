"""Render already-observed enemies without changing terrain used by the controller."""


def enemy_scene(snapshot, mapping, screen_y):
    """Link single-cell markers to the same request's enemy observations.

    Use the full visible camera map, not the narrower forward terrain window.
    Never write these markers into the terrain rows: replacing a pipe or floor
    with an actor symbol would change landing and gap calculations.
    """
    enemies = [dict(enemy.to_state()) for enemy in snapshot.enemies[:5]]
    if not enemies or not mapping.get("available"):
        return None, enemies
    rows = [list(row) for row in mapping["rows"]]
    legend = dict(mapping["legend"])
    for index, (observed, details) in enumerate(zip(snapshot.enemies, enemies, strict=False)):
        marker = str(index + 1)
        world_x = snapshot.x + observed.dx_pixels
        enemy_y = screen_y + observed.dy_pixels
        row = (enemy_y - mapping["origin_screen_y"]) // mapping["tile_size_pixels"]
        col = (world_x - mapping["origin_x"]) // mapping["tile_size_pixels"]
        details.update(map_marker=marker, map_cell=None)
        legend[marker] = f"Enemy {marker}; type and motion in visible_enemies."
        if not (0 <= row < len(rows) and 0 <= col < len(rows[row])) or rows[row][col] == "u":
            # Do not wrap negative indexes or clamp an unseen position onto an edge.
            # The existing relative position still describes an unplotted enemy.
            continue
        details["map_cell"] = [row, col]
        details["terrain_at_marker"] = mapping["rows"][row][col]
        occupied = rows[row][col]
        rows[row][col] = "X" if occupied in "MX" else "*" if occupied in "12345*" else marker
    legend.update(
        X="Mario and one or more enemy anchors share a cell; inspect matching map_cell entries.",
        **{"*": "Multiple enemy anchors share a cell; inspect matching map_cell entries."},
    )
    return {
        "rows": ["".join(row) for row in rows],
        "origin_x": mapping["origin_x"],
        "origin_screen_y": mapping["origin_screen_y"],
        "tile_size_pixels": mapping["tile_size_pixels"],
        "player_cell": mapping["player_cell"],
        "legend": legend,
        "coordinates": mapping["coordinates"],
        "limits": (
            "Current visible camera only. Numbers identify visible_enemies in this request, "
            "not permanent IDs. Markers are position anchors, not full sprite hitboxes. "
            "A null map_cell means the anchor is outside the mapped rows/columns. "
            "Terrain beneath a marker is in terrain_at_marker; local_map retains terrain."
        ),
    }, enemies

"""The games this demo can play. The controller, the recorder and the replay are shared;
a game only says how fast it runs, how big its picture is and what its stages are."""

GAMES = {
    "mario": {
        "title": "mario",
        "fps": 60,
        "frame_step": 2,  # the NES worker keeps every second frame
        "size": [256, 240],
        "stages": ("1-1", "1-2", "1-3", "1-4"),
    },
    "bird": {
        "title": "flappy",
        "fps": 30,
        "frame_step": 1,
        "size": [288, 512],
        "stages": ("25",),  # a stage is a number of pipes to pass
        "soon": True,  # on the menu, not open yet: only a preview request may start it
    },
}


def game_of(events: list[dict]) -> str:
    """Recordings made before there was a second game are Mario's."""
    for event in events:
        if event.get("type") == "created":
            return event.get("game") or "mario"
    return "mario"

"""Shared host/guest protocol. No emulator or cloud dependencies."""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .actions import Action

ACTIONS = tuple(action.value for action in Action)
CANDIDATES = ("right_run_jump", "right_jump", "jump", "left")
TERMINAL = frozenset({"dead", "clear", "error", "limit", "halted"})


class GuestStopped(Exception):
    """Host requested an artifact flush before retiring this timeline."""


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
        raise ValueError(f"Invalid timeline identifier: {value!r}")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def append_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Closing on every append leaves no buffered log handle alive at a checkpoint.
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, allow_nan=False) + "\n")


def context_key(state: dict) -> str:
    """Restrict retry exclusions to comparable position, level, and jump phase."""
    level, player = state["level"], state["player"]
    return ":".join(
        map(
            str,
            (
                level["world"],
                level["stage"],
                level["area"],
                player["x"] // 16,
                player["y"] // 16,
                player["jump_phase"],
            ),
        )
    )


def allowed_actions(state: dict, avoid: list[dict]) -> tuple[str, ...]:
    excluded = {entry["action"] for entry in avoid if entry["context"] == context_key(state)}
    remaining = tuple(action for action in ACTIONS if action not in excluded)
    # Exhausting a context is evidence that our attribution was wrong. Never strand Jev
    # with an empty Choice, or claim that the last action is a proven cause of death.
    return remaining if len(remaining) >= 2 else ACTIONS


def retry_evidence(history: list[dict], death_x: int) -> dict | None:
    recent = [row for row in history[-8:] if 0 <= death_x - row["x_pos"] <= 128]
    grounded = [row for row in recent if row.get("grounded")]
    chosen = grounded or recent
    if not chosen:
        return None
    # Prefer an earlier grounded decision, before momentum made the outcome inevitable.
    row = chosen[0]
    return {
        "context": row["context"],
        "action": row["action"],
        "frame": row["frame"],
        "x_pos": row["x_pos"],
        "reason": "retry hypothesis; not causal attribution",
    }


class Gate:
    """Block between model calls until this VM receives a matching private command.

    Every child inherits the wait, but each child has its own writable control file.
    A stale command from a previous checkpoint can never release a new checkpoint.
    """

    def __init__(self, root: Path, validate=None):
        self.root = root
        self.validate = validate  # a game's own check for a fork's scripted opening

    def wait(self, heartbeat: dict) -> dict:
        command_path = self.root / "control.json"
        command_path.unlink(missing_ok=True)
        token = uuid.uuid4().hex
        atomic_json(self.root / "state.json", {**heartbeat, "phase": "gate", "gate": token})
        while True:
            if (self.root / "stop.json").exists():
                raise GuestStopped
            try:
                command = json.loads(command_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.02)
                continue
            if command.get("gate") != token:
                time.sleep(0.02)
                continue
            identifier(command["timeline"])
            if command.get("force") and self.validate:
                self.validate(command["force"])
            elif command.get("force"):
                if "steps" in command["force"]:
                    from .recovery import validate_sequence

                    validate_sequence(command["force"])
                elif command["force"]["action"] not in ACTIONS:
                    raise ValueError("Unknown forced action")
            command_path.unlink()
            return command

"""Bounded recovery experiments. These are host-defined moves, not model predictions."""

from __future__ import annotations

import hashlib
import json

from .protocol import ACTIONS


def experiments(checkpoint_x: int, death_x: int) -> list[dict]:
    """Interleave timing, duration and approach variants; deduplicate clamped starts."""
    specs = [
        (128, 24, None, 0, "right_run_jump"),
        (96, 32, None, 0, "right_jump"),
        (64, 16, "noop", 16, "right_run_jump"),
        (160, 40, "left", 24, "right_run_jump"),
    ]
    # Delays and retreats change enemy timing, including hazards under low ceilings
    # where varying only a forward jump cannot produce a useful new trajectory.
    for approach, duration in (
        ("noop", 16),
        ("noop", 24),
        ("noop", 40),
        ("left", 16),
        ("left", 32),
        ("right", 16),
        (None, 0),
        ("right_run", 8),
    ):
        for offset in (160, 128, 96, 64, 32):
            for hold, jump in (
                (24, "right_run_jump"),
                (32, "right_jump"),
                (16, "right_run_jump"),
                (40, "right_jump"),
            ):
                specs.append((offset, hold, approach, duration, jump))
    plans, seen = [], set()
    for offset, hold, approach, duration, jump in specs:
        start = max(checkpoint_x, death_x - offset)
        steps = [] if approach is None else [{"action": approach, "frames": duration}]
        steps.append({"action": jump, "frames": hold})
        steps.append({"action": "right_run" if "run" in jump else "right", "frames": 8})
        if sum(step["frames"] for step in steps) > 80:
            continue
        signature = json.dumps([start, steps], sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        prefix = f"{approach} {duration}f → " if approach else ""
        plans.append(
            {
                "experiment_id": hashlib.sha256(signature.encode()).hexdigest()[:12],
                "label": f"x{start}: {prefix}{jump} {hold}f",
                "x_min": start,
                "x_max": death_x + 16,
                "steps": steps,
                "status": "waiting",
                "step_index": 0,
            }
        )
    return plans


def validate_sequence(plan: dict):
    steps = plan.get("steps", [])
    if not 1 <= len(steps) <= 4:
        raise ValueError("Recovery sequences require one to four steps")
    if any(
        step.get("action") not in ACTIONS
        or type(step.get("frames")) is not int
        or not 1 <= step["frames"] <= 48
        for step in steps
    ):
        raise ValueError("Invalid recovery step")
    if sum(step["frames"] for step in steps) > 80:
        raise ValueError("Recovery sequence exceeds 80 frames")


def sequence_chunk(plan: dict | None, state: dict, maximum: int) -> tuple[str, int] | None:
    if not plan or "steps" not in plan or plan.get("status") in {"complete", "aborted", "missed"}:
        return None
    x = state["player"]["x"]
    if plan.get("status", "waiting") == "waiting":
        if x > plan["x_max"]:
            plan["status"] = "missed"
            return None
        if x < plan["x_min"]:
            return None
        plan["status"] = "active"
    step = plan["steps"][plan["step_index"]]
    plan.setdefault("remaining", step["frames"])
    return step["action"], min(maximum, plan["remaining"])


def consume_sequence(plan: dict, frames: int):
    plan["remaining"] -= frames
    if plan["remaining"] == 0:
        plan["step_index"] += 1
        plan.pop("remaining")
        if plan["step_index"] == len(plan["steps"]):
            plan["status"] = "complete"


def interrupt_sequence(plan: dict, state: dict, action: str) -> bool:
    """Stop non-jump segments on imminent contact or braking over a known gap."""
    contact = state.get("hazard", {}).get("estimated_contact_frames")
    immediate_contact = contact is not None and 0 <= contact <= 2 and "jump" not in action
    braking_over_gap = state.get("trajectory", {}).get("crossing_known_gap") and action in {
        "noop",
        "left",
    }
    if immediate_contact or braking_over_gap:
        plan.update(status="aborted", reason="imminent_contact" if immediate_contact else "gap")
        return True
    return False

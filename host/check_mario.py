"""Verify four real emulator forks, using the heuristic policy or real Jev calls."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mnd.backend import MicroVMBackend  # noqa: E402
from mnd.protocol import CANDIDATES, atomic_json  # noqa: E402
from mnd.recovery import experiments  # noqa: E402


async def main(args):
    root = Path(__file__).resolve().parents[1]
    project_id = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    os.environ.setdefault("MSB_HOME", f"/tmp/mnd-{os.getuid()}-{project_id}")
    os.environ["MSB_BACKEND"] = "local"
    backend = MicroVMBackend(image="mnd:local", memory=args.memory)
    prefix = "mnd-check-" + uuid.uuid4().hex[:8]
    source, slot = prefix + "-source", prefix + "-slot"
    children = [prefix + f"-child{i}" for i in range(4)]
    report = {"simulated": False, "policy": args.policy, "memory_mib": args.memory, "children": []}

    async def gate(name, sequence):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                state = await backend.state(name)
            except RuntimeError:
                await asyncio.sleep(0.05)
                continue
            if state["phase"] == "gate" and state["checkpoint_seq"] == sequence:
                return state
            if state["phase"] in {"error", "dead", "limit"}:
                raise RuntimeError(f"Unexpected guest phase: {state['phase']}")
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Checkpoint {sequence} not reached")

    try:
        env = {}
        if args.policy == "typesafe":
            from mnd.launcher import load_key

            if not load_key(args.env_file or root / ".env"):
                raise RuntimeError("Configure a TypeSafe key for --policy typesafe")
            env["TYPESAFE_API_KEY"] = os.environ["TYPESAFE_API_KEY"]
        await backend.create(source, env=env)
        await backend.launch(
            source,
            policy=args.policy,
            checkpoint_frames=96 if args.sequences else 64,
            max_decisions=100,
        )
        initial = await gate(source, 1)
        await backend.branch(source, slot)
        await backend.pause(slot)
        failures = await backend.branch_many(slot, children)
        if failures:
            raise RuntimeError(f"Forks failed: {failures}")
        plans = (
            experiments(40, 104)[:4]
            if args.sequences
            else [{"action": action, "x_min": 0, "x_max": 100} for action in CANDIDATES]
        )
        for child, plan in zip(children, plans, strict=True):
            await backend.release(child, initial, child, [], plan)
        for child, plan in zip(children, plans, strict=True):
            action = plan["steps"][0]["action"] if args.sequences else plan["action"]
            state = await gate(child, 2)
            rows = [
                json.loads(line)
                for line in (
                    await backend.read(child, f"/var/mnd/timelines/{child}/run.jsonl")
                ).splitlines()
            ]
            first = rows[0]
            # SMB's per-frame horizontal speed is below four pixels. Sampling only
            # per macro incorrectly reports up to 24 and corrupts hazard timing.
            assert all(
                abs(row["state"]["player"]["horizontal_speed_px_per_frame"]) <= 4 for row in rows
            )
            assert first["forced"] and first["action"] == action
            if args.sequences:
                assert first["proposed_action"] is None and first["input_tokens"] == 0
                assert state["force_pending"]["status"] in {"complete", "aborted"}
            assert first["frame"] == initial["frame"] == 0
            assert state["timeline"] == child and state["frame"] == (96 if args.sequences else 64)
            await backend.frame(child, args.output / f"{child}.png")
            report["children"].append(
                {
                    "action": action,
                    "frame": state["frame"],
                    "x_pos": state["x_pos"],
                    "y_pos": state["y_pos"],
                    "input_tokens": state["input_tokens"],
                }
            )
        # The original remains at the same gate while its children advance independently.
        assert (await backend.state(source))["frame"] == 0
        report["passed"] = True
    finally:
        report["cleanup_errors"] = await backend.cleanup()
        atomic_json(args.output / "result.json", report)
    assert not report["cleanup_errors"]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sequences", action="store_true", help="Exercise recovery sequences")
    parser.add_argument("--memory", type=int, default=126, help="Guest RAM in MiB")
    parser.add_argument("--policy", choices=("heuristic", "typesafe"), default="heuristic")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("runs/mario-check"))
    args = parser.parse_args()
    if args.memory <= 0:
        parser.error("--memory must be positive")
    asyncio.run(main(args))

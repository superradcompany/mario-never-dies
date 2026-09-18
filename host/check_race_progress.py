"""Exercise a long recovery approach with real microVMs and Jev.

Supply a hazard/frame from a recorded death. This checks recovery from the
initial gate, not a fresh death or a full stage clear. Uses TYPESAFE_API_KEY
and the caller's MSB_HOME; every sandbox created here is removed on exit.
"""

import argparse
import asyncio
import json
import os
import tarfile
from pathlib import Path

from mnd.backend import MicroVMBackend
from mnd.orchestrator import Orchestrator, Settings, Slot


async def check(args):
    backend = MicroVMBackend(image=args.image)
    last_report = -15

    def observe(view):
        nonlocal last_report
        if view["elapsed"] - last_report < 15:
            return
        last_report = view["elapsed"]
        print(
            json.dumps(
                {
                    "elapsed": round(view["elapsed"], 1),
                    "candidates": [
                        {
                            k: state.get(k)
                            for k in ("name", "phase", "frame", "x_pos", "force_pending")
                        }
                        for state in view["timelines"]
                        if state["role"] == "candidate"
                    ],
                }
            ),
            flush=True,
        )

    controller = Orchestrator(
        backend,
        args.output,
        observer=observe,
        settings=Settings(stages=(args.stage,), run_timeout=args.timeout, max_decisions=300),
    )
    report = {
        "stage": args.stage,
        "recorded_hazard_x": args.hazard_x,
        "recorded_death_frame": args.death_frame,
        "passed": False,
    }
    try:
        await controller.start_stage({"TYPESAFE_API_KEY": os.environ["TYPESAFE_API_KEY"]})
        source = controller.trunk
        while True:
            state = await controller.heartbeat(source)
            if state and state["phase"] == "gate":
                break
            await asyncio.sleep(0.1)
        # Keep the initial guest itself as the paused source: no extra running
        # trunk or hidden policy is needed to seed this recorded-death test.
        await backend.pause(source)
        controller.roles[source] = "checkpoint"
        slot = Slot(source, state)
        controller.slots = [slot]
        winner = await controller.race(slot, args.hazard_x, death_frame=args.death_frame)
        if winner:
            await controller.export(winner)
        starts = [e for e in controller.events if e["type"] == "experiment_started"]
        report.update(
            winner=winner,
            winner_state=controller.states.get(winner),
            experiment_starts=starts,
            outcomes=[e for e in controller.events if e["type"] == "experiment_result"],
        )
        forced = {}
        for archive in args.output.glob("timelines/*/timeline.tar"):
            with tarfile.open(archive) as recording:
                rows = []
                for member in recording.getmembers():
                    if member.name.endswith("run.jsonl"):
                        rows += [json.loads(line) for line in recording.extractfile(member)]
                forced[archive.parent.name] = sum(bool(row.get("forced")) for row in rows)
        report["forced_decisions"] = forced
        report["passed"] = bool(
            winner and any(e["elapsed"] > 45 for e in starts) and any(forced.values())
        )
    finally:
        report["cleanup_errors"] = await backend.cleanup()
        report["elapsed"] = controller.elapsed()
        (args.output / "check-result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "winner_state"}), flush=True)
    if not report["passed"] or report["cleanup_errors"]:
        raise RuntimeError("Long-approach recovery check did not qualify; inspect its recordings")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stage", default="1-2")
    parser.add_argument("--hazard-x", type=int, default=910)
    parser.add_argument("--death-frame", type=int, default=554)
    parser.add_argument("--timeout", type=float, default=240)
    asyncio.run(check(parser.parse_args()))


if __name__ == "__main__":
    main()

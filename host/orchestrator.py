"""Run the controller without the browser server."""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mnd.backend import MicroVMBackend  # noqa: E402
from mnd.launcher import IMAGE  # noqa: E402
from mnd.orchestrator import Orchestrator, Settings  # noqa: E402
from mnd.simulation import SimulatedBackend  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--rom", type=Path, help="Path to your local Super Mario Bros. NES ROM")
    parser.add_argument("--policy", choices=("typesafe", "heuristic"), default="typesafe")
    parser.add_argument("--no-multiverse", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    )
    args = parser.parse_args()
    env = {}
    if not args.simulate and args.rom and not args.rom.is_file():
        parser.error("Live gameplay requires --rom /path/to/super-mario-bros.nes")
    if not args.simulate and args.policy == "typesafe":
        if not os.environ.get("TYPESAFE_API_KEY"):
            parser.error("Set TYPESAFE_API_KEY, or use --simulate / --policy heuristic")
        env["TYPESAFE_API_KEY"] = os.environ["TYPESAFE_API_KEY"]
    os.environ["MSB_BACKEND"] = "local"
    backend = (
        SimulatedBackend() if args.simulate else MicroVMBackend(image=args.image, rom=args.rom)
    )
    settings = Settings(multiverse=not args.no_multiverse, policy=args.policy)
    run = Orchestrator(backend, args.output.resolve(), settings=settings)
    status = asyncio.run(run.run(env))
    print(f"{status}: {run.output}")
    raise SystemExit(0 if status == "complete" else 1)


if __name__ == "__main__":
    main()

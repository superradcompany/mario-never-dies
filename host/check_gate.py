"""Exercise the real checkpoint handshake and four-way branch, without Mario or Jev."""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mnd.backend import MicroVMBackend, msb_home  # noqa: E402
from mnd.protocol import atomic_json  # noqa: E402

PROBE = """import os,uuid
from pathlib import Path
from protocol import Gate
root=Path('/var/mnd'); root.mkdir(parents=True,exist_ok=True)
identity=uuid.uuid4().hex
frame=0; timeline='initial'; forced=None
while True:
    command=Gate(root).wait(dict(frame=frame,timeline=timeline,identity=identity,forced=forced))
    timeline=command['timeline']; forced=command.get('force'); frame+=8
"""


async def check(output):
    output.mkdir(parents=True, exist_ok=False)
    os.environ["MSB_BACKEND"] = "local"
    backend = MicroVMBackend(image="python:3.13-slim", timeout=180)
    prefix = "mnd-gate-" + uuid.uuid4().hex[:8]
    source, slot = prefix + "-source", prefix + "-slot"
    report = {"status": "running", "msb_home": str(msb_home()), "simulated": False}

    async def wait(name, frame):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                state = await backend.state(name)
            except Exception:
                await asyncio.sleep(0.03)
                continue
            if state["phase"] == "gate" and state["frame"] == frame:
                return state
            await asyncio.sleep(0.03)
        raise TimeoutError(f"No expected gate at frame {frame} in {name}")

    try:
        await backend.create(source)
        protocol = Path(__file__).resolve().parents[1] / "mnd" / "protocol.py"
        await backend.write(source, "/tmp/protocol.py", protocol.read_bytes())
        await backend.write(source, "/tmp/check_gate.py", PROBE.encode())
        await backend.exec(
            source,
            [
                "python",
                "-c",
                "import subprocess; f=open('/tmp/check_gate.log','ab'); "
                "subprocess.Popen(['python','/tmp/check_gate.py'],stdin=subprocess.DEVNULL,"
                "stdout=f,stderr=f,start_new_session=True,close_fds=True)",
            ],
        )
        captured = await wait(source, 0)
        await backend.branch(source, slot)
        await backend.pause(slot)
        await backend.release(source, captured, "original", [])
        parent = await wait(source, 8)
        assert parent["timeline"] == "original"
        children = [prefix + f"-child{index}" for index in range(4)]
        started = time.perf_counter()
        errors = await backend.branch_many(slot, children)
        report["four_way_branch_ms"] = (time.perf_counter() - started) * 1000
        assert not errors, errors
        for index, child in enumerate(children):
            before = await wait(child, 0)
            assert before["identity"] == captured["identity"]
            assert before["gate"] == captured["gate"]
            await backend.release(
                child,
                captured,
                f"timeline-{index}",
                [],
                {"action": "right", "x_min": index, "x_max": index + 1},
            )
        observed = [await wait(child, 8) for child in children]
        assert len({state["timeline"] for state in observed}) == 4
        assert {state["forced"]["x_min"] for state in observed} == set(range(4))
        assert all(state["identity"] == captured["identity"] for state in observed)
        assert (await wait(source, 8))["forced"] is None
        report.update(status="passed", children=observed)
    except Exception as error:
        report.update(status="failed", error=str(error))
    finally:
        report["cleanup_errors"] = await backend.cleanup()
        if report["cleanup_errors"]:
            report["status"] = "failed"
        atomic_json(output / "gate-check.json", report)
    print(json.dumps(report, indent=2))
    return report["status"] == "passed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/gate-check"))
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(check(args.output.resolve())) else 1)

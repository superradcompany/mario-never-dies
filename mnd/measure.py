"""M0: measure whole-machine branching without a ROM or TypeSafe key."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
import traceback
import uuid
from pathlib import Path

from .backend import MicroVMBackend, msb_home
from .protocol import atomic_json

PROBE = """import json, os, time, uuid, urllib.request
identity = uuid.uuid4().hex
memory = bytearray(64 * 1024 * 1024)
counter = 0
network_ok = 0
while True:
    counter += 1
    for offset in range(0, len(memory), 4096):
        memory[offset] = counter % 256
    if os.environ.get("MND_NETWORK_PROBE") and counter % 20 == 1:
        try:
            with urllib.request.urlopen(os.environ["MND_NETWORK_PROBE"], timeout=3) as response:
                response.read(16)
            network_ok += 1
        except Exception:
            pass
    with open("/tmp/probe.tmp", "w") as stream:
        state = dict(identity=identity, pid=os.getpid(), counter=counter, network_ok=network_ok)
        json.dump(state, stream)
    os.replace("/tmp/probe.tmp", "/tmp/probe.json")
    time.sleep(0.05)
"""


def summarize(samples):
    ordered = sorted(samples)
    return {
        "samples": samples,
        "p50_ms": statistics.median(samples),
        "p95_ms": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
    }


async def measure(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    shared = output / "shared"
    shared.mkdir()
    (shared / "marker").write_text("branch-visible", encoding="utf-8")
    os.environ["MSB_BACKEND"] = "local"
    backend = MicroVMBackend(image=args.image, memory=args.memory, timeout=180)
    samples = {name: [] for name in ("branch", "pause", "branch_paused", "resume", "read", "kill")}
    report = {
        "status": "running",
        "simulated": False,
        "memory_mib": args.memory,
        "image": args.image,
        "msb_home": str(msb_home()),
        "checks": [],
        "network_probe": args.network_url,
        "bind_mount_check": args.check_bind_mount,
        "warmup_iterations": 1,
    }
    source = "mnd-m0-" + uuid.uuid4().hex[:8]

    async def timed(key, operation, record):
        report["last_operation"] = key
        start = time.perf_counter()
        result = await operation
        if record:
            samples[key].append((time.perf_counter() - start) * 1000)
        return result

    async def probe(name, previous=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                current = json.loads(await backend.read(name, "/tmp/probe.json"))
            except Exception:
                await asyncio.sleep(0.05)
                continue
            if previous is None or current["counter"] > previous["counter"]:
                return current
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Probe did not advance in {name}")

    try:
        await backend.create(
            source,
            shared=shared if args.check_bind_mount else None,
            env={"MND_NETWORK_PROBE": args.network_url or ""},
        )
        await backend.write(source, "/tmp/probe.py", PROBE.encode())
        await backend.exec(
            source,
            [
                "python",
                "-c",
                "import subprocess; f=open('/tmp/probe.log','ab'); "
                "subprocess.Popen(['python','/tmp/probe.py'],stdin=subprocess.DEVNULL,"
                "stdout=f,stderr=f,start_new_session=True,close_fds=True)",
            ],
        )
        original = await probe(source)
        for index in range(args.samples + 1):
            record = index > 0
            child, restored = f"{source}-c{index}", f"{source}-r{index}"
            await timed("branch", backend.branch(source, child), record)
            before = await probe(child)
            after = await probe(child, before)
            assert after["identity"] == original["identity"], "Probe restarted instead of resuming"
            assert after["pid"] == original["pid"], "Captured process PID changed"
            if args.check_bind_mount:
                assert await backend.read(child, "/shared/marker") == "branch-visible"
            await timed("pause", backend.pause(child), record)
            await timed("branch_paused", backend.branch(child, restored), record)
            resumed = await probe(restored, after)
            assert resumed["identity"] == original["identity"]
            # A child resumes; its user-paused source must remain paused.
            from microsandbox import SandboxStatus

            handle = await backend.Sandbox.get(child)
            assert handle.status == SandboxStatus.PAUSED
            try:
                await backend.read(child, "/tmp/probe.json")
            except Exception:
                pass  # Metadata above proves pause; SDK errors differ across runtime versions.
            else:
                raise AssertionError("Paused source unexpectedly accepted filesystem access")
            await backend.write(restored, "/tmp/child-only", b"private")
            assert (
                await backend.exec(
                    source, ["sh", "-c", "test ! -e /tmp/child-only && echo private"]
                )
                == "private\n"
            )
            await timed("resume", backend.resume(child), record)
            await timed("read", backend.read(restored, "/tmp/probe.json"), record)
            if args.network_url:
                deadline = time.monotonic() + 15
                while (await probe(restored))["network_ok"] <= resumed["network_ok"]:
                    if time.monotonic() > deadline:
                        raise AssertionError("Restored child could not reconnect to network probe")
                    await asyncio.sleep(0.1)
            await timed("kill", backend.kill(restored), record)
            await backend.kill(child)
            report["checks"].append({"iteration": index, "warmup": not record, "passed": True})
            atomic_json(output / "measurements.json", {**report, "samples": samples})
            print(
                f"M0 iteration {index}/{args.samples}: "
                "enabled process, disk, mount and pause checks passed",
                flush=True,
            )
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        (output / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        cleanup = await backend.cleanup()
        report["cleanup_errors"] = cleanup
        if cleanup:
            report["status"] = "failed"
        report["timings"] = {name: summarize(values) for name, values in samples.items() if values}
        atomic_json(output / "measurements.json", report)
        lines = [
            "# M0 measurements",
            "",
            f"Status: **{report['status']}**",
            "",
            f"Image: `{args.image}`. Memory: {args.memory} MiB. One warmup excluded.",
            "",
            "| Operation | n | p50 ms | p95 ms |",
            "| --- | ---: | ---: | ---: |",
        ]
        for name, values in report["timings"].items():
            lines.append(
                f"| {name} | {len(values['samples'])} | "
                f"{values['p50_ms']:.2f} | {values['p95_ms']:.2f} |"
            )
        if report.get("error"):
            lines.extend(["", f"Failure: {report['error']}"])
        lines.extend(
            [
                "",
                "Jev reconnection latency is not measured by M0. "
                "The optional network probe uses fresh public HTTPS requests.",
                "",
            ]
        )
        (output / "measurements.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report["status"] == "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.13-slim")
    parser.add_argument("--memory", type=int, default=512)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument(
        "--check-bind-mount",
        action="store_true",
        help="Also test inherited host mounts (not used by this demo)",
    )
    parser.add_argument(
        "--network-url", help="Optional public HTTPS endpoint for reconnection checks"
    )
    parser.add_argument(
        "--output", type=Path, default=Path("runs") / ("m0-" + time.strftime("%Y%m%d-%H%M%S"))
    )
    args = parser.parse_args()
    if args.samples < 1 or args.memory < 128:
        parser.error("Use at least one sample and 128 MiB")
    if args.network_url and not args.network_url.startswith("https://"):
        parser.error("The network probe must use HTTPS")
    raise SystemExit(0 if asyncio.run(measure(args)) else 1)


if __name__ == "__main__":
    main()

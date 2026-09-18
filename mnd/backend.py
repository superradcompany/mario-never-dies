"""Small adapter around the microsandbox Python API; imports are lazy for offline tests."""

from __future__ import annotations

import asyncio
import io
import json
import shlex
import time
import zipfile
from pathlib import Path

from .protocol import TERMINAL, identifier

GUEST_CODE = "/opt/mnd.zip"


def guest_code() -> bytes:
    """This package as one zip. The image carries only dependencies; a machine gets the
    code when it is created, so editing the code never rebuilds an image."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(Path(__file__).parent.glob("*.py")):
            # A fixed date keeps the bytes, and so their digest, the same for the same code.
            entry = zipfile.ZipInfo(f"mnd/{path.name}", date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, path.read_bytes())
    return buffer.getvalue()


# Packed when the host starts, so the guest runs the same code the host has loaded.
CODE = guest_code()


def sdk():
    try:
        from microsandbox import Sandbox, Volume
    except ImportError as error:
        raise RuntimeError("Install this demo with the [host] extra first") from error
    missing = [
        name for name in ("branch", "branch_many", "pause", "resume") if not hasattr(Sandbox, name)
    ]
    if missing:
        raise RuntimeError(f"This microsandbox SDK lacks {', '.join(missing)}; use 0.7.1 or newer")
    return Sandbox, Volume


class MicroVMBackend:
    simulated = False

    def __init__(
        self, *, image: str, memory: int = 126, timeout: float = 60, rom: Path | None = None
    ):
        self.Sandbox, self.Volume = sdk()
        self.image, self.memory, self.timeout = image, memory, timeout
        self.rom = rom
        self.handles = {}
        self.owned = set()

    async def call(self, operation):
        return await asyncio.wait_for(operation, timeout=self.timeout)

    async def create(self, name: str, *, env: dict | None = None, shared: Path | None = None):
        # Register before creating: a client timeout can leave a live VM behind.
        self.owned.add(name)
        volumes = (
            {"/shared": self.Volume.bind(str(shared.resolve()), readonly=True)} if shared else {}
        )
        self.handles[name] = await self.call(
            self.Sandbox.create(
                name,
                image=self.image,
                memory=self.memory,
                cpus=1,
                env=env or {},
                volumes=volumes,
                detached=True,
                replace=False,
            )
        )
        # Written once into the private disk: every branch of this machine carries it.
        await self.write(name, GUEST_CODE, CODE)
        if self.rom:
            contents = self.rom.read_bytes()
            if contents[:4] != b"NES\x1a":
                raise ValueError("The supplied file is not an iNES ROM")
            # Copy once into the private disk. Restored children do not rely on a host
            # bind mount remaining readable after whole-machine capture.
            await self.write(name, "/rom.nes", contents)

    async def launch(
        self,
        name: str,
        *,
        policy: str,
        checkpoint_frames: int,
        max_decisions: int,
        stage: str = "1-1",
        game: str = "mario",
        seed: int | None = None,
    ):
        worker = (
            ["mnd.bird_guest", "--target", str(int(stage)), "--seed", str(seed or 123)]
            if game == "bird"
            else ["mnd.guest", "--env", f"SuperMarioBros-{stage}-v0"]
        )
        argv = [
            "python",
            "-m",
            *worker,
            "--policy",
            policy,
            "--checkpoint-frames",
            str(checkpoint_frames),
            "--max-decisions",
            str(max_decisions),
        ]
        # An independent process group and file-backed output survive the exec channel.
        code = (
            "import os,subprocess; os.makedirs('/var/mnd',exist_ok=True); "
            "f=open('/var/mnd/worker.log','ab'); "
            f"subprocess.Popen({argv!r},stdin=subprocess.DEVNULL,stdout=f,"
            "stderr=subprocess.STDOUT,start_new_session=True,close_fds=True,"
            # The zip comes before site-packages, and `/` holds no stale copy of the package.
            f"cwd='/',env={{**os.environ,'PYTHONPATH':{GUEST_CODE!r}}})"
        )
        await self.exec(name, ["python", "-c", code])

    async def exec(self, name: str, argv: list[str]) -> str:
        output = await self.call(self.handles[name].exec(argv[0], argv[1:]))
        if not output.success:
            raise RuntimeError(f"Guest command failed in {name}: {output.stderr_text[-2000:]}")
        return output.stdout_text

    async def read(self, name: str, path: str) -> str:
        try:
            return await self.call(self.handles[name].fs.read_text(path))
        except Exception as error:
            raise RuntimeError(f"Read {path} in {name}: {error}") from error

    async def write(self, name: str, path: str, data: bytes):
        await self.call(self.handles[name].fs.write(path, data))

    async def state(self, name: str) -> dict:
        return json.loads(await self.read(name, "/var/mnd/state.json"))

    async def frame(self, name: str, destination: Path):
        fs = self.handles[name].fs
        if await self.call(fs.exists("/var/mnd/latest.png")):
            data = await self.call(fs.read("/var/mnd/latest.png"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(destination)

    async def release(self, name: str, state: dict, timeline: str, avoid: list, force=None):
        command = {
            "gate": state["gate"],
            "timeline": identifier(timeline),
            "avoid": avoid,
            "force": force,
        }
        fs = self.handles[name].fs
        await self.write(name, "/var/mnd/control.tmp", json.dumps(command).encode())
        await self.call(fs.rename("/var/mnd/control.tmp", "/var/mnd/control.json"))

    async def branch(self, source: str, child: str):
        self.owned.add(child)
        started = time.perf_counter()
        self.handles[child] = await self.call(self.handles[source].branch(child))
        return (time.perf_counter() - started) * 1000

    async def branch_many(self, source: str, children: list[str]) -> dict:
        self.owned.update(children)
        outcomes = await self.call(self.handles[source].branch_many(children))
        errors = {}
        # Partial failures do not discard successful children. Track all requested names
        # so that cleanup also finds children started just before a timeout or error.
        for outcome in outcomes:
            if outcome.sandbox is not None:
                self.handles[outcome.name] = outcome.sandbox
            else:
                errors[outcome.name] = str(outcome.error)
        return errors

    async def pause(self, name: str):
        await self.call(self.handles[name].pause())

    async def resume(self, name: str):
        await self.call(self.handles[name].resume())

    async def pull(self, name: str, timeline: str, destination: Path):
        identifier(timeline)
        state = await self.state(name)
        if state["phase"] not in TERMINAL:
            # A cooperative stop leaves the VM running for fs/exec while the emulator
            # stops writing. Pausing the VM would make artifact export unavailable.
            await self.write(name, "/var/mnd/stop.json", b"{}")
            deadline = time.monotonic() + 15
            while (await self.state(name))["phase"] not in TERMINAL:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Guest did not quiesce for export: {name}")
                await asyncio.sleep(0.05)
        destination.mkdir(parents=True, exist_ok=True)
        # Only export this timeline: a VM's disk also contains its ancestors' recordings.
        await self.exec(
            name,
            [
                "sh",
                "-ec",
                "cd /var/mnd && tar cf /tmp/mnd-export.tar "
                f"state.json worker.log timelines/{shlex.quote(timeline)}",
            ],
        )
        await self.call(
            self.handles[name].fs.copy_to_host(
                "/tmp/mnd-export.tar", str(destination / "timeline.tar")
            )
        )

    async def kill(self, name: str):
        handle = self.handles.get(name)
        if handle is None:
            # get() can resolve a VM whose create/branch call timed out.
            handle = await self.call(self.Sandbox.get(name))
        await self.call(handle.kill())
        await self.call(self.Sandbox.remove(name))
        self.owned.discard(name)
        self.handles.pop(name, None)

    async def cleanup(self) -> list[dict]:
        errors = []
        for name in sorted(self.owned):
            try:
                await self.kill(name)
            except Exception as error:
                errors.append({"sandbox": name, "error": str(error)})
        return errors

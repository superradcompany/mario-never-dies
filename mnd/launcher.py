"""Bring in the guest image and open the local browser control room."""

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import io
import json
import os
import subprocess
import threading
import urllib.request
import uuid
import webbrowser
import zipfile
from pathlib import Path

from dotenv import dotenv_values

from .web import ControlRoom, Server, graceful_signals, handler

ROOT = Path(__file__).resolve().parents[1]


def load_key(path: Path):
    # Parse data, never source shell code or copy unrelated project credentials.
    values = dotenv_values(path) if path.is_file() else {}
    key = os.environ.get("TYPESAFE_API_KEY") or values.get("TYPESAFE_API_KEY")
    if key:
        os.environ["TYPESAFE_API_KEY"] = key
    return bool(key)


IMAGE = "ghcr.io/superradcompany/mnd"
# The emulator's own package on PyPI carries the game data the published image leaves out.
ROM_WHEEL = "https://pypi.org/pypi/gym-super-mario-bros/9.1.0/json"
ROM_MEMBER = "gym_super_mario_bros/_roms/super-mario-bros.nes"


def invoke(method, *args, **kwargs):
    async def call():
        # Native SDK awaitables must be constructed inside a running event loop.
        return await method(*args, **kwargs)

    return asyncio.run(call())


def image_reference() -> str:
    """The published image for this checkout: `image/Dockerfile` is its only input, so its
    digest is the tag. CI pushes the same tag (.github/workflows/mnd-image.yml)."""
    digest = hashlib.sha256((ROOT / "image/Dockerfile").read_bytes()).hexdigest()
    return f"{IMAGE}:deps-{digest[:12]}"


def pull(reference: str):
    """The SDK pulls when a sandbox is created, so a throwaway one brings the image in."""
    from microsandbox import PullEventType, PullPolicy, Sandbox

    async def run():
        name = f"mnd-pull-{uuid.uuid4().hex[:10]}"
        session = Sandbox.create_with_progress(
            name, image=reference, cpus=1, memory=126, pull_policy=PullPolicy.IF_MISSING
        )
        try:
            async with session:
                async for event in session.progress:
                    if event.event_type is PullEventType.RESOLVED:
                        size = (event.total_download_bytes or 0) / 1e6
                        print(f"  {event.layer_count} layers, {size:.0f} MB", flush=True)
                sandbox = await session.result()
            await sandbox.stop()
        finally:
            with contextlib.suppress(Exception):  # nothing to remove if the pull failed
                await Sandbox.remove(name)

    asyncio.run(run())


def prepare_image(build: bool = False) -> str:
    from microsandbox import Image

    home = Path(os.environ["MSB_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    # Serialize launchers sharing a catalog, including their cache publication.
    with (home / "image-build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reference = image_reference()
        tag = reference.rsplit(":", 1)[1]
        handles = invoke(Image.list)
        if not build and any(h.reference == reference for h in handles):
            return reference
        local = built(home, tag, handles)
        if local:
            return local
        if not build:
            print(f"Pulling the guest image {reference}…", flush=True)
            try:
                pull(reference)
                return reference
            except Exception as error:
                reason = str(error).strip().splitlines()[-1][:200] if str(error).strip() else "?"
                print(f"  not available ({reason}). Building it here with Docker.", flush=True)
        return build_image(home, tag)


def built(home: Path, tag: str, handles) -> str | None:
    """An image this machine already built from the same Dockerfile, if it is still there."""
    stamp = home / "image-builds" / f"{tag}.json"
    if not stamp.exists():
        return None
    record = json.loads(stamp.read_text())
    # Image.list is local-only. A source stamp alone does not prove that
    # the imported image survived cache removal or external retagging.
    present = any(
        h.manifest_digest == record["manifest_digest"] and h.reference == record["reference"]
        for h in handles
    )
    return record["reference"] if present else None


def build_image(home: Path, tag: str) -> str:
    """The same image, built on this machine: for an edited Dockerfile, or no registry."""
    from microsandbox import Image

    stamps = home / "image-builds"
    stamps.mkdir(exist_ok=True)
    stamp = stamps / f"{tag}.json"
    # Never reuse a tag for a new import, even after cache loss or a rebuild
    # with an unchanged Dockerfile but changed base/dependencies. Keep old imports:
    # paused checkpoints and other running servers may still depend on them.
    reference = f"mnd:{tag}-{uuid.uuid4().hex[:12]}"
    cache = ROOT / "runs" / "setup"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"{reference.split(':')[1]}.tar"
    print(f"Building the guest image {reference}…", flush=True)
    subprocess.run(
        ["docker", "build", "-f", "image/Dockerfile", "-t", reference, "image"],
        cwd=ROOT,
        check=True,
    )
    try:
        subprocess.run(["docker", "save", "-o", str(archive), reference], check=True)
        invoke(Image.load, str(archive), tag=reference)
        handle = invoke(Image.get, reference)
        # Store the SDK's canonical reference for comparisons with Image.list.
        record = {"reference": handle.reference, "manifest_digest": handle.manifest_digest}
        temporary = stamp.with_suffix(".tmp")
        temporary.write_text(json.dumps(record) + "\n")
        temporary.replace(stamp)
        return record["reference"]
    finally:
        archive.unlink(missing_ok=True)


def packaged_rom() -> Path | None:
    """The image carries no game data. Without --rom, take the ROM from the emulator's own
    package on PyPI: fetched by this machine, checked against PyPI's digest, kept under runs/."""
    target = ROOT / "runs" / "setup" / "super-mario-bros.nes"
    if target.is_file() and target.read_bytes()[:4] == b"NES\x1a":
        return target
    try:
        with urllib.request.urlopen(ROM_WHEEL, timeout=30) as response:
            files = json.load(response)["urls"]
        wheel = next(item for item in files if item["packagetype"] == "bdist_wheel")
        with urllib.request.urlopen(wheel["url"], timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != wheel["digests"]["sha256"]:
            raise ValueError("the download does not match PyPI's digest")
        rom = zipfile.ZipFile(io.BytesIO(data)).read(ROM_MEMBER)
        if rom[:4] != b"NES\x1a":
            raise ValueError("the packaged file is not an iNES ROM")
    except Exception as error:
        print(f"No ROM for Mario ({error}). Pass --rom, or play Flappy.", flush=True)
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(rom)
    temporary.replace(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--rom", type=Path, help="your own Super Mario Bros. NES ROM")
    parser.add_argument(
        "--build-image",
        action="store_true",
        help="build image/Dockerfile here with Docker instead of pulling the published image",
    )
    args = parser.parse_args()
    if not load_key(args.env_file):
        parser.error("Set TYPESAFE_API_KEY, or put it in .env or a file given with --env-file")
    # A stable private catalog keeps image imports cached and avoids global runtimes.
    project_id = hashlib.sha256(str(ROOT).encode()).hexdigest()[:8]
    os.environ["MSB_HOME"] = f"/tmp/mnd-{os.getuid()}-{project_id}"
    Path(os.environ["MSB_HOME"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ["MSB_BACKEND"] = "local"
    image = prepare_image(args.build_image)
    room = ControlRoom(ROOT / "runs/web", image, True, args.rom or packaged_rom())
    server = Server(("127.0.0.1", args.port), handler(room))
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Mario Never Dies: {url}", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    graceful_signals()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        room.close()
        server.server_close()

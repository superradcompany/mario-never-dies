"""Bring in the guest image and open the local browser control room."""

import argparse
import asyncio
import collections
import contextlib
import fcntl
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
import zipfile
from pathlib import Path

from dotenv import dotenv_values

from .backend import msb_home
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


def span(seconds: float) -> str:
    seconds = max(0, round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class PullProgress:
    """The pull as one line that fills while the layers arrive: how much, how fast, how long
    is left. Layers download side by side, so the line adds them up. When the output is not a
    terminal (a log, a pipe) it prints a plain line every ten percent instead."""

    WIDTH = 24

    def __init__(self, stream=None, clock=time.monotonic):
        self.stream = stream or sys.stdout
        self.clock = clock
        self.live = self.stream.isatty()
        self.started = clock()
        self.total = 0
        self.layers = 0
        self.got = {}  # bytes so far, per layer
        self.size = {}  # a layer's size, for when the manifest gave no total
        self.finished = set()
        self.window = collections.deque()  # (time, bytes) for the speed of the last seconds
        self.drawn = -1.0
        self.step = 0

    def event(self, event):
        kind = event.event_type.value
        if kind == "resolved":
            self.total = event.total_download_bytes or 0
            self.layers = event.layer_count or 0
        elif kind in ("layer_download_progress", "layer_download_complete"):
            self.got[event.layer_index] = event.downloaded_bytes or 0
            if event.total_bytes:
                self.size[event.layer_index] = event.total_bytes
            if kind == "layer_download_complete":
                self.finished.add(event.layer_index)
            self.draw(force=kind == "layer_download_complete")
        elif kind == "stitch_merging_trees":
            self.line("  assembling the disk…")
        elif kind == "complete":
            self.done()

    def draw(self, force=False):
        now = self.clock()
        done = sum(self.got.values())
        total = max(self.total or sum(self.size.values()), done, 1)
        self.window.append((now, done))
        while len(self.window) > 2 and now - self.window[0][0] > 20:
            self.window.popleft()
        if not self.live:
            if done * 10 // total > self.step:
                self.step = done * 10 // total
                self.line(f"  {self.step * 10:3d}%  {done / 1e6:.0f} / {total / 1e6:.0f} MB")
            return
        if not force and now - self.drawn < 0.1:
            return
        self.drawn = now
        (t0, b0), (t1, b1) = self.window[0], self.window[-1]
        speed = (b1 - b0) / (t1 - t0) if t1 > t0 else 0
        # The speed is the last seconds'; the time left uses the whole pull so far, which a slow
        # or bursty line does not throw around.
        elapsed = now - self.started
        steady = done / elapsed if elapsed >= 5 else 0
        filled = self.WIDTH * done // total
        text = (
            f"  {'█' * filled}{'░' * (self.WIDTH - filled)} {done * 100 // total:3d}%"
            f"  {done / 1e6:.0f} / {total / 1e6:.0f} MB"
            f"  {len(self.finished)}/{self.layers or len(self.size)} layers"
        )
        if speed > 0:
            text += f"  {speed / 1e6:.1f} MB/s" if speed >= 1e6 else f"  {speed / 1e3:.0f} KB/s"
        if steady > 0 and done < total:
            text += f"  {span((total - done) / steady)} left"
        self.line(text)

    def line(self, text):
        if self.live:
            self.stream.write(f"\r{text}\x1b[K")  # over the last one, clearing what is left of it
        else:
            self.stream.write(text + "\n")
        self.stream.flush()

    def done(self):
        total = sum(self.got.values())
        took = span(self.clock() - self.started)
        self.line(f"  {total / 1e6:.0f} MB in {took} · {self.layers or len(self.got)} layers")
        if self.live:
            self.stream.write("\n")
            self.stream.flush()


def pull(reference: str):
    """The SDK pulls when a sandbox is created, so a throwaway one brings the image in."""
    from microsandbox import PullPolicy, Sandbox

    async def run():
        name = f"mnd-pull-{uuid.uuid4().hex[:10]}"
        session = Sandbox.create_with_progress(
            name, image=reference, cpus=1, memory=126, pull_policy=PullPolicy.IF_MISSING
        )
        progress = PullProgress()
        try:
            async with session:
                async for event in session.progress:
                    progress.event(event)
                sandbox = await session.result()
            await sandbox.stop()
        except BaseException:
            if progress.live:
                print(flush=True)  # leave the half-drawn line and start the error on its own
            raise
        finally:
            with contextlib.suppress(Exception):  # nothing to remove if the pull failed
                await Sandbox.remove(name)

    asyncio.run(run())


def prepare_image() -> str:
    """The guest image comes from the registry, once: the catalog keeps it after that."""
    from microsandbox import Image

    # Serialize launchers sharing msb's home: one of them pulls, the rest find it there. The
    # lock lives outside that home, which is msb's to fill.
    home = hashlib.sha256(str(msb_home()).encode()).hexdigest()[:12]
    with (Path(tempfile.gettempdir()) / f"mnd-image-{home}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reference = image_reference()
        if any(h.reference == reference for h in invoke(Image.list)):
            return reference
        print(f"Pulling the guest image {reference}…", flush=True)
        try:
            pull(reference)
        except Exception as error:
            reason = str(error).strip().splitlines()[-1][:200] if str(error).strip() else "?"
            raise SystemExit(
                f"The guest image could not be pulled: {reason}\n"
                "CI publishes it from image/Dockerfile (.github/workflows/mnd-image.yml), one tag "
                "per version of that file. An edited Dockerfile has no image until it is pushed; "
                "'not authorized' means the package is private."
            ) from None
        return reference


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
    args = parser.parse_args()
    if not load_key(args.env_file):
        parser.error("Set TYPESAFE_API_KEY, or put it in .env or a file given with --env-file")
    # The machines run here, in msb's own home: MSB_HOME if it is set, its default otherwise.
    os.environ["MSB_BACKEND"] = "local"
    image = prepare_image()
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

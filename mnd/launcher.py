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


# The guest image: dependencies only, built by CI from image/Dockerfile whenever it changes on
# main (.github/workflows/mnd-image.yml). The demo's own code is not in it.
IMAGE = "ghcr.io/superradcompany/mnd:latest"
# The emulator's own package on PyPI carries the game data the published image leaves out.
ROM_WHEEL = "https://pypi.org/pypi/gym-super-mario-bros/9.1.0/json"
ROM_MEMBER = "gym_super_mario_bros/_roms/super-mario-bros.nes"
MANIFESTS = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


def invoke(method, *args, **kwargs):
    async def call():
        # Native SDK awaitables must be constructed inside a running event loop.
        return await method(*args, **kwargs)

    return asyncio.run(call())


def published(reference: str) -> set[str] | None:
    """The digests the registry has under this tag right now: the index, and each platform's
    manifest, which is the one msb records. None when it cannot be asked (offline, or another
    registry than ghcr.io): then the image already here is the one to use."""
    host, _, rest = reference.partition("/")
    repository, _, tag = rest.rpartition(":")
    if host != "ghcr.io" or not repository:
        return None
    try:
        scope = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repository}:pull"
        with urllib.request.urlopen(scope, timeout=4) as response:
            token = json.load(response)["token"]
        request = urllib.request.Request(
            f"https://ghcr.io/v2/{repository}/manifests/{tag}",
            headers={"Authorization": f"Bearer {token}", "Accept": MANIFESTS},
        )
        with urllib.request.urlopen(request, timeout=4) as response:
            digests = {response.headers.get("Docker-Content-Digest")}
            digests.update(item.get("digest") for item in json.load(response).get("manifests", []))
        return digests - {None}
    except Exception:
        return None


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
        self.cached = set()  # layers an earlier pull already brought in
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
        elif kind == "layer_materialize_complete" and event.layer_index not in self.got:
            self.cached.add(event.layer_index)  # no download for it: it was already here
        elif kind == "stitch_merging_trees":
            self.line("  assembling the disk…")
        elif kind == "complete":
            self.done()

    def draw(self, force=False):
        now = self.clock()
        done = sum(self.got.values())
        # msb keeps what it has. Once every layer is either arriving or already here, the ones
        # already here are the rest of the manifest's total, and the line starts from there.
        if self.total and self.layers and len(self.cached) + len(self.size) >= self.layers:
            done += max(0, self.total - sum(self.size.values()))
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
            f"  {len(self.finished | self.cached)}/{self.layers or len(self.size)} layers"
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

    def note(self, text=""):
        """A line of its own, leaving the bar as it stood. With no text it only ends the bar."""
        if self.live and self.drawn >= 0:
            self.stream.write("\n")
        if text:
            self.stream.write(text + "\n")
        self.stream.flush()

    def done(self):
        total = sum(self.got.values())
        took = span(self.clock() - self.started)
        self.line(f"  {total / 1e6:.0f} MB in {took} · {self.layers or len(self.got)} layers")
        if self.live:
            self.stream.write("\n")
            self.stream.flush()


REFUSED = ("not authorized", "unauthorized", "denied", "manifest unknown", "not found")


def refused(error: Exception) -> bool:
    """The registry said no, as against the connection failing on the way."""
    return any(word in str(error).lower() for word in REFUSED)


def pull(reference: str, attempts: int = 5, refresh: bool = False):
    """The SDK pulls when a sandbox is created, so a throwaway one brings the image in. A
    connection that drops is tried again: msb keeps the layers it finished and the part of the
    one it was on, so each try carries on from there."""
    from microsandbox import PullPolicy, Sandbox

    progress = PullProgress()

    async def once():
        name = f"mnd-pull-{uuid.uuid4().hex[:10]}"
        # A tag that is already here is only looked up again when asked to: `latest` moves.
        policy = PullPolicy.ALWAYS if refresh else PullPolicy.IF_MISSING
        session = Sandbox.create_with_progress(
            name, image=reference, cpus=1, memory=126, pull_policy=policy
        )
        try:
            async with session:
                async for event in session.progress:
                    progress.event(event)
                sandbox = await session.result()
            await sandbox.stop()
        finally:
            with contextlib.suppress(Exception):  # nothing to remove if the pull failed
                await Sandbox.remove(name)

    for attempt in range(1, attempts + 1):
        try:
            asyncio.run(once())
            return
        except KeyboardInterrupt:
            progress.note("  stopped. what arrived is kept: the next pull carries on from it.")
            raise
        except Exception as error:
            if refused(error) or attempt == attempts:
                progress.note()
                raise
            reason = str(error).strip().splitlines()[-1][:120]
            progress.note(f"  the connection dropped ({reason}). carrying on, try {attempt + 1}…")
            time.sleep(min(2**attempt, 15))


def prepare_image(reference: str = IMAGE) -> str:
    """The guest image comes from the registry. msb keeps it, so a launch only asks the registry
    whether the tag has moved, and pulls again when it has."""
    from microsandbox import Image

    # Serialize launchers sharing msb's home: one of them pulls, the rest find it there. The
    # lock lives outside that home, which is msb's to fill.
    home = hashlib.sha256(str(msb_home()).encode()).hexdigest()[:12]
    with (Path(tempfile.gettempdir()) / f"mnd-image-{home}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        here = next((h for h in invoke(Image.list) if h.reference == reference), None)
        if here:
            digests = published(reference)
            if not digests or here.manifest_digest in digests:
                return reference
            print(f"A newer guest image is out. Pulling {reference}…", flush=True)
        else:
            print(f"Pulling the guest image {reference}…", flush=True)
        try:
            pull(reference, refresh=bool(here))
        except Exception as error:
            reason = str(error).strip().splitlines()[-1][:200] if str(error).strip() else "?"
            if here:
                print(f"  it did not arrive ({reason}). Using the one already here.", flush=True)
                return reference
            if refused(error):
                raise SystemExit(
                    f"The registry refused the guest image: {reason}\n"
                    "CI publishes it from image/Dockerfile when that file changes on main "
                    "(.github/workflows/mnd-image.yml). 'Not authorized' means a private package."
                ) from None
            raise SystemExit(
                f"The download kept failing: {reason}\n"
                "Nothing is lost: run it again and it carries on from what already arrived."
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
    parser.add_argument("--image", default=IMAGE, help="another guest image, such as a release tag")
    args = parser.parse_args()
    if not load_key(args.env_file):
        parser.error("Set TYPESAFE_API_KEY, or put it in .env or a file given with --env-file")
    # The machines run here, in msb's own home: MSB_HOME if it is set, its default otherwise.
    os.environ["MSB_BACKEND"] = "local"
    image = prepare_image(args.image)
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

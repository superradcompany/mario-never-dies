import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mnd.launcher import load_key


class KeyTests(unittest.TestCase):
    def test_key_is_loaded_without_importing_unrelated_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('TYPESAFE_API_KEY="test-key"\nOTHER_SECRET=unrelated\n')
            self.assertTrue(load_key(path))
            self.assertEqual(os.environ["TYPESAFE_API_KEY"], "test-key")
            self.assertNotIn("OTHER_SECRET", os.environ)

    def test_missing_file_and_missing_key_are_reported(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            self.assertFalse(load_key(Path(directory) / ".env"))
            self.assertNotIn("TYPESAFE_API_KEY", os.environ)

    def test_explicit_environment_wins_over_file(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"TYPESAFE_API_KEY": "explicit"}, clear=True),
        ):
            path = Path(directory) / ".env"
            path.write_text("TYPESAFE_API_KEY=from-file\n")
            self.assertTrue(load_key(path))
            self.assertEqual(os.environ["TYPESAFE_API_KEY"], "explicit")


class ImageTests(unittest.TestCase):
    """The image is dependencies only: the Dockerfile is its one input."""

    def setUp(self):
        import types
        from unittest.mock import AsyncMock

        from mnd import launcher

        self.launcher = launcher
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "image").mkdir()
        (self.root / "mnd").mkdir()
        (self.root / "image/Dockerfile").write_text("first")
        (self.root / "mnd/guest.py").write_text("first")
        self.catalog = {}

        async def load(path, *, tag):
            self.assertNotIn(tag, self.catalog)
            self.catalog[tag] = types.SimpleNamespace(
                reference=tag, manifest_digest=f"digest-{len(self.catalog)}"
            )

        async def get(tag):
            return self.catalog[tag]

        async def listing():
            return list(self.catalog.values())

        image = types.SimpleNamespace(
            load=AsyncMock(side_effect=load),
            get=AsyncMock(side_effect=get),
            list=AsyncMock(side_effect=listing),
        )
        for context in (
            patch.object(launcher, "ROOT", self.root),
            patch.dict(os.environ, {"MSB_HOME": str(self.root / "home")}),
            patch.dict("sys.modules", {"microsandbox": types.SimpleNamespace(Image=image)}),
        ):
            context.start()
            self.addCleanup(context.stop)

    def test_the_published_image_is_pulled_once(self):
        launcher = self.launcher
        reference = launcher.image_reference()
        self.assertRegex(reference, r"^ghcr\.io/superradcompany/mnd:deps-[0-9a-f]{12}$")

        def pulled(name):
            self.catalog[name] = types_namespace(name)

        with patch.object(launcher, "pull", side_effect=pulled) as pull:
            self.assertEqual(launcher.prepare_image(), reference)
            self.assertEqual(launcher.prepare_image(), reference)
            # Editing the demo's code is not a reason to fetch anything.
            (self.root / "mnd/guest.py").write_text("second")
            self.assertEqual(launcher.prepare_image(), reference)
            self.assertEqual(pull.call_count, 1)
            # A different Dockerfile is a different image.
            (self.root / "image/Dockerfile").write_text("second")
            self.assertNotEqual(launcher.prepare_image(), reference)
            self.assertEqual(pull.call_count, 2)

    def test_an_image_that_cannot_be_pulled_stops_the_launch_and_says_why(self):
        launcher = self.launcher
        with (
            patch.object(launcher, "pull", side_effect=RuntimeError("Not authorized: url …")),
            self.assertRaises(SystemExit) as stopped,
        ):
            launcher.prepare_image()
        message = str(stopped.exception)
        self.assertIn("Not authorized", message)
        self.assertIn("the package is private", message)
        self.assertEqual(self.catalog, {})  # and nothing is built here instead


class PullProgressTests(unittest.TestCase):
    """The events are the ones msb sends for the published image: seven layers, side by side."""

    class Screen:
        def __init__(self, terminal):
            self.terminal, self.text = terminal, ""

        def isatty(self):
            return self.terminal

        def write(self, text):
            self.text += text

        def flush(self):
            pass

    def replay(self, terminal):
        import types

        from mnd.launcher import PullProgress

        now = [0.0]
        screen = self.Screen(terminal)
        progress = PullProgress(screen, clock=lambda: now[0])

        def send(kind, **fields):
            blank = dict.fromkeys(
                (
                    "total_download_bytes",
                    "layer_count",
                    "layer_index",
                    "downloaded_bytes",
                    "total_bytes",
                )
            )
            progress.event(
                types.SimpleNamespace(
                    event_type=types.SimpleNamespace(value=kind), **blank | fields
                )
            )

        send("resolved", layer_count=2, total_download_bytes=200_000_000)
        for second in range(1, 101):  # two layers at 1 MB/s each, reporting half a second apart
            for layer in (0, 1):
                now[0] = second + layer / 2
                send(
                    "layer_download_progress",
                    layer_index=layer,
                    downloaded_bytes=second * 1_000_000,
                    total_bytes=100_000_000,
                )
            if second == 50:
                halfway = screen.text
        for layer in (0, 1):
            send("layer_download_complete", layer_index=layer, downloaded_bytes=100_000_000)
        send("stitch_merging_trees", layer_count=2)
        send("complete", layer_count=2)
        return halfway, screen.text

    def test_a_terminal_gets_one_line_that_fills(self):
        halfway, text = self.replay(terminal=True)
        last = halfway.split("\r")[-1]
        self.assertIn(" 50%", last)
        self.assertIn("100 / 200 MB", last)
        self.assertIn("2.0 MB/s", last)
        self.assertIn("50s left", last)
        self.assertEqual(last.count("█"), 12)
        self.assertEqual(text.count("\n"), 1)  # one line, redrawn; the newline comes at the end
        self.assertIn("200 MB in 1m 40s · 2 layers", text)

    def test_a_log_gets_a_line_every_ten_percent(self):
        _, text = self.replay(terminal=False)
        lines = text.splitlines()
        self.assertNotIn("\r", text)
        self.assertEqual(
            [line.split("%")[0].strip() for line in lines[:10]],
            [str(n) for n in range(10, 101, 10)],
        )
        self.assertEqual(lines[-1].strip(), "200 MB in 1m 40s · 2 layers")


def types_namespace(reference):
    import types

    return types.SimpleNamespace(reference=reference, manifest_digest="published")


class RomTests(unittest.TestCase):
    def wheel(self, rom):
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("gym_super_mario_bros/_roms/super-mario-bros.nes", rom)
        return buffer.getvalue()

    def serve(self, data, digest=None):
        import hashlib
        import io
        import json

        listing = {
            "urls": [
                {"packagetype": "sdist", "url": "https://files.example/x.tar.gz", "digests": {}},
                {
                    "packagetype": "bdist_wheel",
                    "url": "https://files.example/x.whl",
                    "digests": {"sha256": digest or hashlib.sha256(data).hexdigest()},
                },
            ]
        }

        def urlopen(url, timeout=None):
            body = data if str(url).endswith(".whl") else json.dumps(listing).encode()
            return io.BytesIO(body)

        return urlopen

    def test_the_rom_comes_out_of_the_package_once(self):
        from mnd import launcher

        data = self.wheel(b"NES\x1a" + b"\0" * 64)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(launcher, "ROOT", Path(directory)),
            patch.object(launcher.urllib.request, "urlopen", side_effect=self.serve(data)) as fetch,
        ):
            rom = launcher.packaged_rom()
            self.assertEqual(rom.read_bytes()[:4], b"NES\x1a")
            self.assertEqual(launcher.packaged_rom(), rom)
            self.assertEqual(fetch.call_count, 2)  # the listing and the wheel, one time

    def test_a_download_that_does_not_match_its_digest_is_refused(self):
        from mnd import launcher

        data = self.wheel(b"NES\x1a" + b"\0" * 64)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(launcher, "ROOT", Path(directory)),
        ):
            with patch.object(
                launcher.urllib.request, "urlopen", side_effect=self.serve(data, "0" * 64)
            ):
                self.assertIsNone(launcher.packaged_rom())
            self.assertFalse((Path(directory) / "runs/setup/super-mario-bros.nes").exists())

    def test_offline_means_no_rom_not_a_crash(self):
        from mnd import launcher

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(launcher, "ROOT", Path(directory)),
            patch.object(launcher.urllib.request, "urlopen", side_effect=OSError("offline")),
        ):
            self.assertIsNone(launcher.packaged_rom())


class GuestCodeTests(unittest.TestCase):
    def test_the_package_travels_as_one_reproducible_zip(self):
        import io
        import zipfile

        from mnd import backend

        self.assertEqual(backend.guest_code(), backend.guest_code())
        names = zipfile.ZipFile(io.BytesIO(backend.CODE)).namelist()
        for module in ("guest", "bird_guest", "protocol", "recovery", "__init__"):
            self.assertIn(f"mnd/{module}.py", names)

    def test_a_machine_gets_the_code_and_the_worker_runs_from_it(self):
        import asyncio
        import types

        from mnd import backend

        written, ran = {}, []

        async def write(path, data):
            written[path] = data

        async def run(program, arguments):
            ran.append((program, arguments))
            return types.SimpleNamespace(success=True, stdout_text="", stderr_text="")

        handle = types.SimpleNamespace(fs=types.SimpleNamespace(write=write), exec=run)

        async def create(name, **kwargs):
            return handle

        sandbox = types.SimpleNamespace(create=create)
        with patch.object(backend, "sdk", return_value=(sandbox, types.SimpleNamespace())):
            machines = backend.MicroVMBackend(image="ghcr.io/superradcompany/mnd:deps-test")

        async def scenario():
            await machines.create("trunk")
            await machines.launch(
                "trunk", policy="heuristic", checkpoint_frames=150, max_decisions=9
            )

        asyncio.run(scenario())
        self.assertEqual(written[backend.GUEST_CODE], backend.CODE)
        program = ran[0][1][1]
        self.assertIn("'PYTHONPATH':'/opt/mnd.zip'", program.replace(" ", ""))
        self.assertIn("cwd='/'", program)

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

    def test_the_published_image_is_pulled_once_and_never_built(self):
        launcher = self.launcher
        reference = launcher.image_reference()
        self.assertRegex(reference, r"^ghcr\.io/superradcompany/mnd:deps-[0-9a-f]{12}$")

        def pulled(name):
            self.catalog[name] = types_namespace(name)

        with (
            patch.object(launcher, "pull", side_effect=pulled) as pull,
            patch.object(launcher.subprocess, "run") as command,
        ):
            self.assertEqual(launcher.prepare_image(), reference)
            self.assertEqual(launcher.prepare_image(), reference)
            # Editing the demo's code is not a reason to fetch or build anything.
            (self.root / "mnd/guest.py").write_text("second")
            self.assertEqual(launcher.prepare_image(), reference)
            self.assertEqual(pull.call_count, 1)
            command.assert_not_called()
            # A different Dockerfile is a different image.
            (self.root / "image/Dockerfile").write_text("second")
            self.assertNotEqual(launcher.prepare_image(), reference)
            self.assertEqual(pull.call_count, 2)

    def test_without_the_registry_it_builds_here_and_keeps_earlier_imports(self):
        launcher = self.launcher
        with (
            patch.object(launcher, "pull", side_effect=RuntimeError("manifest unknown")),
            patch.object(launcher.subprocess, "run") as command,
        ):
            first = launcher.prepare_image()
            self.assertTrue(first.startswith("mnd:deps-"))
            self.assertEqual(launcher.prepare_image(), first)
            self.assertEqual(command.call_count, 2)  # docker build, docker save
            self.assertEqual(command.call_args_list[0].args[0][-1], "image")  # no code in context
            (self.root / "mnd/guest.py").write_text("second")
            self.assertEqual(launcher.prepare_image(), first)
            self.assertEqual(command.call_count, 2)
            (self.root / "image/Dockerfile").write_text("second")
            second = launcher.prepare_image()
            self.assertNotEqual(first, second)
            self.assertIn(first, self.catalog)
            (self.root / "image/Dockerfile").write_text("first")
            self.assertEqual(launcher.prepare_image(), first)
            del self.catalog[first]
            third = launcher.prepare_image()
            self.assertNotIn(third, (first, second))
            self.assertIn(second, self.catalog)

    def test_build_image_skips_the_registry(self):
        launcher = self.launcher
        with (
            patch.object(launcher, "pull") as pull,
            patch.object(launcher.subprocess, "run"),
        ):
            self.assertTrue(launcher.prepare_image(build=True).startswith("mnd:deps-"))
            pull.assert_not_called()


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

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
    """The image is the registry's `latest`: pulled once, and again only when the tag has moved."""

    def setUp(self):
        import types
        from unittest.mock import AsyncMock

        from mnd import launcher

        self.launcher = launcher
        self.catalog = {}

        async def listing():
            return list(self.catalog.values())

        image = types.SimpleNamespace(list=AsyncMock(side_effect=listing))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for context in (
            patch.dict(os.environ, {"MSB_HOME": directory.name}),
            patch.dict("sys.modules", {"microsandbox": types.SimpleNamespace(Image=image)}),
        ):
            context.start()
            self.addCleanup(context.stop)

    def have(self, digest):
        self.catalog[self.launcher.IMAGE] = types_namespace(self.launcher.IMAGE, digest)

    def test_the_first_launch_pulls_it(self):
        launcher = self.launcher
        self.assertEqual(launcher.IMAGE, "ghcr.io/superradcompany/mnd:latest")
        with (
            patch.object(launcher, "pull") as pull,
            patch.object(launcher, "published") as registry,
        ):
            self.assertEqual(launcher.prepare_image(), launcher.IMAGE)
        pull.assert_called_once_with(launcher.IMAGE, refresh=False)
        registry.assert_not_called()

    def test_an_image_that_is_still_the_published_one_is_used_as_it_is(self):
        launcher = self.launcher
        self.have("sha256:arm64")
        with (
            patch.object(launcher, "pull") as pull,
            patch.object(launcher, "published", return_value={"sha256:index", "sha256:arm64"}),
        ):
            self.assertEqual(launcher.prepare_image(), launcher.IMAGE)
        pull.assert_not_called()

    def test_a_tag_that_moved_is_pulled_again(self):
        launcher = self.launcher
        self.have("sha256:old")
        with (
            patch.object(launcher, "pull") as pull,
            patch.object(launcher, "published", return_value={"sha256:index", "sha256:new"}),
        ):
            self.assertEqual(launcher.prepare_image(), launcher.IMAGE)
        pull.assert_called_once_with(launcher.IMAGE, refresh=True)

    def test_offline_it_uses_what_is_here(self):
        launcher = self.launcher
        self.have("sha256:old")
        with (
            patch.object(launcher, "pull") as pull,
            patch.object(launcher, "published", return_value=None),
        ):
            self.assertEqual(launcher.prepare_image(), launcher.IMAGE)
        pull.assert_not_called()

    def test_a_newer_image_that_does_not_arrive_does_not_stop_the_launch(self):
        launcher = self.launcher
        self.have("sha256:old")
        with (
            patch.object(
                launcher, "pull", side_effect=RuntimeError("error decoding response body")
            ),
            patch.object(launcher, "published", return_value={"sha256:new"}),
        ):
            self.assertEqual(launcher.prepare_image(), launcher.IMAGE)

    def test_another_image_can_be_asked_for(self):
        launcher = self.launcher
        with patch.object(launcher, "pull") as pull:
            release = "ghcr.io/superradcompany/mnd:0.1.0"
            self.assertEqual(launcher.prepare_image(release), release)
        pull.assert_called_once_with(release, refresh=False)

    def test_a_download_that_keeps_failing_says_nothing_is_lost(self):
        launcher = self.launcher
        with (
            patch.object(
                launcher, "pull", side_effect=RuntimeError("error decoding response body")
            ),
            self.assertRaises(SystemExit) as stopped,
        ):
            launcher.prepare_image()
        self.assertIn("carries on from what already arrived", str(stopped.exception))
        self.assertNotIn("Dockerfile", str(stopped.exception))

    def test_a_refusal_stops_the_launch_and_says_why(self):
        launcher = self.launcher
        with (
            patch.object(launcher, "pull", side_effect=RuntimeError("Not authorized: url …")),
            self.assertRaises(SystemExit) as stopped,
        ):
            launcher.prepare_image()
        message = str(stopped.exception)
        self.assertIn("Not authorized", message)
        self.assertIn("private package", message)

    def test_only_ghcr_is_asked_whether_a_tag_moved(self):
        launcher = self.launcher
        with patch.object(launcher.urllib.request, "urlopen") as network:
            self.assertIsNone(launcher.published("docker.io/library/alpine:3.20"))
        network.assert_not_called()

    def test_the_registry_answers_with_the_index_and_each_platform(self):
        import io
        import json

        launcher = self.launcher

        class Reply(io.BytesIO):
            headers = {"Docker-Content-Digest": "sha256:index"}

        def urlopen(request, timeout=None):
            if isinstance(request, str):
                return Reply(json.dumps({"token": "anonymous"}).encode())
            self.assertEqual(
                request.full_url, "https://ghcr.io/v2/superradcompany/mnd/manifests/latest"
            )
            manifests = [{"digest": "sha256:amd64"}, {"digest": "sha256:arm64"}]
            return Reply(json.dumps({"manifests": manifests}).encode())

        with patch.object(launcher.urllib.request, "urlopen", side_effect=urlopen):
            found = launcher.published(launcher.IMAGE)
        self.assertEqual(found, {"sha256:index", "sha256:amd64", "sha256:arm64"})


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

    def test_a_resumed_pull_starts_from_the_layers_already_here(self):
        import types

        from mnd.launcher import PullProgress

        screen = self.Screen(terminal=True)
        now = [0.0]
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
            kind = types.SimpleNamespace(value=kind)
            progress.event(types.SimpleNamespace(event_type=kind, **blank | fields))

        send("resolved", layer_count=3, total_download_bytes=200_000_000)
        send("layer_materialize_complete", layer_index=2)  # 100 MB that an earlier pull brought in
        for layer, (have, size) in enumerate([(30_000_000, 60_000_000), (0, 40_000_000)]):
            now[0] += 1
            send(
                "layer_download_progress",
                layer_index=layer,
                downloaded_bytes=have,
                total_bytes=size,
            )
        last = screen.text.split("\r")[-1]
        self.assertIn(" 65%", last)
        self.assertIn("130 / 200 MB", last)
        self.assertIn("1/3 layers", last)

    def test_a_log_gets_a_line_every_ten_percent(self):
        _, text = self.replay(terminal=False)
        lines = text.splitlines()
        self.assertNotIn("\r", text)
        self.assertEqual(
            [line.split("%")[0].strip() for line in lines[:10]],
            [str(n) for n in range(10, 101, 10)],
        )
        self.assertEqual(lines[-1].strip(), "200 MB in 1m 40s · 2 layers")


class PullRetryTests(unittest.TestCase):
    """A connection that drops is tried again; a registry that says no is not."""

    def sessions(self, outcomes):
        import types

        made = []

        class Session:
            def __init__(self, outcome):
                self.outcome = outcome

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            @property
            def progress(self):
                async def events():
                    if isinstance(self.outcome, Exception):
                        raise self.outcome
                    return
                    yield

                return events()

            async def result(self):
                async def stop():
                    pass

                return types.SimpleNamespace(stop=stop)

        def create(name, **kwargs):
            made.append(name)
            return Session(outcomes[len(made) - 1])

        async def remove(name):
            pass

        sandbox = types.SimpleNamespace(create_with_progress=create, remove=remove)
        policy = types.SimpleNamespace(IF_MISSING="if-missing")
        return made, types.SimpleNamespace(Sandbox=sandbox, PullPolicy=policy)

    def test_a_dropped_connection_is_tried_again_and_the_pull_goes_through(self):
        from mnd import launcher

        made, sdk = self.sessions([RuntimeError("image error: error decoding response body"), None])
        with (
            patch.dict("sys.modules", {"microsandbox": sdk}),
            patch.object(launcher.time, "sleep") as wait,
            patch("sys.stdout", new_callable=__import__("io").StringIO) as out,
        ):
            launcher.pull("ghcr.io/superradcompany/mnd:latest")
        self.assertEqual(len(made), 2)
        wait.assert_called_once()
        self.assertIn("the connection dropped", out.getvalue())

    def test_a_refusal_is_not_tried_again(self):
        from mnd import launcher

        made, sdk = self.sessions([RuntimeError("registry error: Not authorized: url …")])
        with (
            patch.dict("sys.modules", {"microsandbox": sdk}),
            patch.object(launcher.time, "sleep") as wait,
            patch("sys.stdout", new_callable=__import__("io").StringIO),
            self.assertRaises(RuntimeError),
        ):
            launcher.pull("ghcr.io/superradcompany/mnd:latest")
        self.assertEqual(len(made), 1)
        wait.assert_not_called()

    def test_it_gives_up_after_its_tries_and_says_nothing_is_lost(self):
        from mnd import launcher

        made, sdk = self.sessions([RuntimeError("error decoding response body")] * 3)
        with (
            patch.dict("sys.modules", {"microsandbox": sdk}),
            patch.object(launcher.time, "sleep"),
            patch("sys.stdout", new_callable=__import__("io").StringIO),
            self.assertRaises(RuntimeError),
        ):
            launcher.pull("ghcr.io/superradcompany/mnd:latest", attempts=3)
        self.assertEqual(len(made), 3)


def types_namespace(reference, digest="published"):
    import types

    return types.SimpleNamespace(reference=reference, manifest_digest=digest)


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
            machines = backend.MicroVMBackend(image="ghcr.io/superradcompany/mnd:latest")

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

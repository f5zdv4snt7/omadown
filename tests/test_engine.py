import os
import tempfile
import threading
import unittest
from pathlib import Path

from omadown.backends import Backend, BackendRegistry, DownloadContext, Progress
from omadown.config import Config
from omadown.db import Store
from omadown.engine import Engine
from omadown.models import DownloadOptions, MediaFormat, MediaInfo, State


class FakeBackend(Backend):
    """Writes a small file after reporting progress - no network involved."""

    name = "fake"
    priority = 1
    fail = False

    def claim(self, url: str) -> bool:
        return url.startswith("fake://")

    def probe(self, url: str, ctx: DownloadContext) -> MediaInfo:
        return MediaInfo(
            url=url, title="Fake Video", id="xyz", extractor="fake",
            uploader="Fake Channel", duration=12.5,
            formats=[
                MediaFormat(format_id="a", ext="mp4", height=240, resolution="426x240"),
                MediaFormat(format_id="b", ext="mp4", height=480, resolution="854x480"),
                MediaFormat(format_id="c", ext="webm", height=1080, resolution="1920x1080"),
            ],
        )

    def download(self, ctx: DownloadContext, progress) -> Path:
        if self.fail:
            from omadown.backends import BackendError
            raise BackendError("boom")
        target = ctx.workdir / "raw.mp4"
        payload = b"\x00" * 4096
        target.write_bytes(payload)
        for step in range(1, 5):
            progress(Progress(stage="downloading", fraction=step / 4,
                              downloaded=1024 * step, total=4096, speed=2048.0))
        return target


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["OMADOWN_DATA_HOME"] = str(self.tmp / "data")
        self.out = self.tmp / "out"
        self.config = Config()
        self.config.general.download_dir = str(self.out)
        self.config.general.max_retries = 0
        self.config.postprocessing.enabled = False
        self.store = Store(self.tmp / "omadown.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _engine(self, backend: Backend) -> Engine:
        registry = BackendRegistry([backend])
        return Engine(self.config, self.store, registry=registry)

    def test_successful_download_lands_in_output_dir(self):
        engine = self._engine(FakeBackend())
        result = engine.submit(["fake://one"])
        self.assertEqual(len(result.added), 1)
        engine.run(exit_when_done=True)

        item = self.store.get(result.added[0].id)
        self.assertEqual(item.state, State.DONE.value)
        self.assertTrue(Path(item.output_path).exists())
        self.assertEqual(Path(item.output_path).name, "Fake Video.mp4")
        self.assertEqual(Path(item.output_path).parent, self.out)

    def test_resolution_token_reflects_the_cap_not_the_maximum(self):
        self.config.general.file_name_format = "{title} [{resolution}].{ext}"
        engine = self._engine(FakeBackend())
        result = engine.submit(["fake://capped"], DownloadOptions(max_resolution=480))
        engine.run(exit_when_done=True)
        item = self.store.get(result.added[0].id)
        self.assertEqual(Path(item.output_path).name, "Fake Video [480].mp4")

        self.config.general.file_name_format = "{title} [{resolution}].{ext}"
        second = engine.submit(["fake://uncapped"], DownloadOptions())
        engine.run(exit_when_done=True)
        uncapped = self.store.get(second.added[0].id)
        self.assertEqual(Path(uncapped.output_path).name, "Fake Video [1080].mp4")

    def test_forced_backend_is_used(self):
        engine = self._engine(FakeBackend())
        result = engine.submit(["fake://forced"], DownloadOptions(backend="fake"))
        engine.run(exit_when_done=True)
        item = self.store.get(result.added[0].id)
        self.assertEqual(item.state, State.DONE.value)
        self.assertEqual(item.backend, "fake")

    def test_unknown_forced_backend_fails_cleanly(self):
        engine = self._engine(FakeBackend())
        result = engine.submit(["fake://nope"], DownloadOptions(backend="does-not-exist"))
        engine.run(exit_when_done=True)
        item = self.store.get(result.added[0].id)
        self.assertEqual(item.state, State.FAILED.value)
        self.assertIn("未知的下载后端", item.error)

    def test_failure_is_recorded_with_message(self):
        backend = FakeBackend()
        backend.fail = True
        engine = self._engine(backend)
        result = engine.submit(["fake://fails"])
        engine.run(exit_when_done=True)
        item = self.store.get(result.added[0].id)
        self.assertEqual(item.state, State.FAILED.value)
        self.assertIn("boom", item.error)

    def test_duplicate_urls_are_skipped(self):
        engine = self._engine(FakeBackend())
        first = engine.submit(["fake://dup"])
        second = engine.submit(["fake://dup"])
        self.assertEqual(len(first.added), 1)
        self.assertEqual(len(second.added), 0)
        self.assertEqual(len(second.skipped), 1)

    def test_custom_name_scheme(self):
        self.config.general.file_name_format = "{extractor}/{title}.{ext}"
        engine = self._engine(FakeBackend())
        result = engine.submit(["fake://naming"])
        engine.run(exit_when_done=True)
        item = self.store.get(result.added[0].id)
        self.assertEqual(Path(item.output_path).relative_to(self.out),
                         Path("fake/Fake Video.mp4"))

    def test_snapshot_and_stats(self):
        engine = self._engine(FakeBackend())
        engine.submit(["fake://a", "fake://b"])
        engine.write_state()
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["stats"]["total"], 2)
        self.assertEqual(len(snapshot["items"]), 2)
        from omadown.util import state_path
        self.assertTrue(state_path().exists())

    def test_cancel_before_start_marks_cancelled(self):
        engine = self._engine(FakeBackend())
        result = engine.submit(["fake://cancel"])
        engine.cancel(result.added[0].id)
        item = self.store.get(result.added[0].id)
        self.assertEqual(item.state, State.CANCELLED.value)

    def test_playlist_expansion(self):
        class PlaylistBackend(FakeBackend):
            name = "fakeplaylist"

            def claim(self, url: str) -> bool:
                return url.startswith("pl://")

            def probe(self, url: str, ctx: DownloadContext) -> MediaInfo:
                entries = [
                    MediaInfo(url=f"fake://entry{i}", title=f"Entry {i}",
                              extractor="fake", playlist_index=i)
                    for i in range(1, 4)
                ]
                return MediaInfo(url=url, title="My Playlist", extractor="fake",
                                 is_playlist=True, playlist_title="My Playlist",
                                 entries=entries)

        registry = BackendRegistry([PlaylistBackend(), FakeBackend()])
        engine = Engine(self.config, self.store, registry=registry)
        result = engine.submit(["pl://channel"], DownloadOptions(playlist=True))
        engine.run(exit_when_done=True)

        parent = self.store.get(result.added[0].id)
        self.assertEqual(parent.state, State.CANCELLED.value)
        self.assertIn("展开", parent.error)

        rows = self.store.list(states=[State.DONE.value])
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertTrue(Path(row.output_path).exists())


if __name__ == "__main__":
    unittest.main()

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from omadown.config import Config
from omadown.models import DownloadItem
from omadown.postproc import PostProcessor, find_ffmpeg

from . import support


@unittest.skipUnless(support.FFMPEG, "ffmpeg required")
class PostProcessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["OMADOWN_DATA_HOME"] = str(self.tmp / "data")
        self.config = Config()
        self.workdir = self.tmp / "work"
        self.workdir.mkdir()
        self.source = self._make_ts()
        self.item = DownloadItem(url="https://example.com/v", title="Clip", id=1)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_ts(self) -> Path:
        target = self.tmp / "source.ts"
        subprocess.run([
            support.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(target),
        ], check=True, capture_output=True)
        return target

    def _processor(self) -> PostProcessor:
        return PostProcessor(self.config)

    def test_remux_returns_an_existing_path(self):
        result = self._processor().remux(self.source, "mp4", self.workdir)
        self.assertTrue(result.exists(), f"{result} should exist")
        self.assertEqual(result.suffix, ".mp4")
        self.assertGreater(support.probe_duration(result), 1.5)
        self.assertFalse(self.source.exists(), "the intermediate .ts should be gone")

    def test_full_pipeline_end_to_end(self):
        processor = self._processor()
        result = processor.run_for(self.item, self.source, self.workdir)
        self.assertTrue(result.path.exists())
        self.assertEqual(result.path.suffix, ".mp4")
        self.assertIn("remux:mp4", result.steps)
        self.assertGreater(support.probe_duration(result.path), 1.5)

    def test_extract_audio_produces_playable_file(self):
        result = self._processor().extract_audio(self.source, "m4a", self.workdir)
        self.assertTrue(result.exists())
        self.assertEqual(result.suffix, ".m4a")
        probe = subprocess.run(
            [support.FFPROBE, "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(result)],
            capture_output=True, text=True,
        )
        self.assertIn("audio", probe.stdout)
        self.assertNotIn("video", probe.stdout)

    def test_transcode_returns_existing_path(self):
        result = self._processor().transcode(self.source, "h264", self.workdir)
        self.assertTrue(result.exists(), f"{result} should exist")
        self.assertEqual(result.suffix, ".mp4")

    def test_promote_replaces_and_reports_truthfully(self):
        processor = self._processor()
        staged = self.workdir / "staged.bin"
        staged.write_bytes(b"new")
        final = processor._promote(self.source, staged, ".mp4")
        self.assertTrue(final.exists())
        self.assertEqual(final.read_bytes(), b"new")
        self.assertFalse(self.source.exists())

    def test_find_ffmpeg_respects_env_override(self):
        os.environ["OMADOWN_FFMPEG"] = support.FFMPEG or ""
        try:
            self.assertEqual(find_ffmpeg(), support.FFMPEG)
        finally:
            os.environ.pop("OMADOWN_FFMPEG", None)


if __name__ == "__main__":
    unittest.main()

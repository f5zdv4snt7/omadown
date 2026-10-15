import os
import tempfile
import unittest
from pathlib import Path

from omadown.backends import CancelToken, DownloadContext
from omadown.backends.sniff import UgeBackend
from omadown.config import Config
from omadown.models import DownloadItem

from . import support

PAGE = """
<!doctype html>
<html><head>
  <title>My Great Video Page</title>
  <meta property="og:video:secure_url" content="https://cdn.example.com/main.mp4?token=abc">
  <script type="application/ld+json">
  {"@type":"VideoObject","contentUrl":"https://cdn.example.com/ld.mp4","thumbnailUrl":"https://cdn.example.com/thumb.jpg"}
  </script>
</head><body>
  <video src="/media/player.mp4"></video>
  <iframe src="/embed/player.html"></iframe>
  <script>
    var cfg = {"hls":"https:\\/\\/cdn.example.com\\/live\\/index.m3u8?x=1",
               "poster":"https://cdn.example.com/poster.jpg"};
    var other = "https://cdn.example.com/sprite.jpg";
  </script>
</body></html>
"""

EMBED = """
<html><head><title>Embed</title></head><body>
  <source src="https://cdn.example.com/embed/stream_1080p.mp4">
</body></html>
"""


class SniffTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["OMADOWN_DATA_HOME"] = str(self.tmp / "data")
        self.config = Config()

    def tearDown(self):
        self._tmp.cleanup()

    def _backend(self):
        return UgeBackend()

    def test_static_discovery_ranks_manifest_first(self):
        backend = self._backend()
        candidates = backend._discover_static(PAGE, "https://example.com/watch/1")
        urls = [c.url for c in candidates]
        self.assertIn("https://cdn.example.com/main.mp4?token=abc", urls)
        self.assertIn("https://cdn.example.com/ld.mp4", urls)
        self.assertIn("https://example.com/media/player.mp4", urls)
        self.assertIn("https://cdn.example.com/live/index.m3u8?x=1", urls)

        ranked = backend._rank(candidates)
        self.assertEqual(ranked[0].kind, "hls", "HLS should outrank progressive files")
        self.assertTrue(all("thumb" not in c.url and "poster" not in c.url
                            and "sprite" not in c.url for c in ranked))

    def test_height_hint_from_url(self):
        backend = self._backend()
        candidates = backend._discover_static(EMBED, "https://example.com/embed/1")
        best = backend._rank(candidates)[0]
        self.assertEqual(best.height, 1080)

    def test_end_to_end_probe_over_http(self):
        self.tmp.joinpath("page.html").write_text(PAGE, encoding="utf-8")
        self.tmp.joinpath("embed").mkdir()
        self.tmp.joinpath("embed", "player.html").write_text(EMBED, encoding="utf-8")
        with support.LocalServer(self.tmp) as server:
            item = DownloadItem(url=server.url("page.html"), id=9)
            ctx = DownloadContext(item=item, workdir=self.tmp / "work",
                                  options={"cancel": CancelToken()},
                                  ffmpeg=None, config=self.config)
            info = self._backend().probe(server.url("page.html"), ctx)
        self.assertTrue(info.formats)
        self.assertEqual(item.meta["uge_candidates"][0]["kind"], "hls")
        self.assertTrue(item.meta["uge_best"].startswith("http"))

    def test_iframes_are_followed(self):
        backend = self._backend()
        frames = backend._iframes(PAGE, "https://example.com/watch/1")
        self.assertEqual(frames, ["https://example.com/embed/player.html"])


if __name__ == "__main__":
    unittest.main()

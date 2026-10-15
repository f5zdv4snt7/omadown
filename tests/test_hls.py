import os
import tempfile
import unittest
from pathlib import Path

from omadown.backends import CancelToken, DownloadContext, HlsBackend, parse_m3u8, select_variant
from omadown.config import Config
from omadown.models import DownloadItem, DownloadOptions

from . import support

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2",AUDIO="aud"
v720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="aud"
v1080/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:10
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x0000000000000000000000000000000A
#EXTINF:5.5,segment title, with comma
seg10.ts
#EXT-X-BYTERANGE:75232@0
seg11.ts
#EXTINF:4.0,
seg12.ts
"""

LIVE = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:1
#EXTINF:4.0,
a1.ts
#EXTINF:4.0,
a2.ts
"""


class ParserTests(unittest.TestCase):
    def test_attribute_parsing_handles_quoted_commas(self):
        attrs = parse_m3u8(MEDIA, "http://x/").segments
        self.assertEqual(len(attrs), 3)
        self.assertEqual(attrs[0].title, "segment title, with comma")

    def test_master_playlist(self):
        playlist = parse_m3u8(MASTER, "http://example.com/hls/master.m3u8")
        self.assertEqual(playlist.kind, "master")
        self.assertEqual(len(playlist.variants), 2)
        self.assertEqual(playlist.variants[1].height, 1080)
        self.assertEqual(playlist.renditions[0].kind, "AUDIO")
        self.assertEqual(playlist.renditions[0].language, "en")

    def test_media_playlist(self):
        playlist = parse_m3u8(MEDIA, "http://example.com/hls/index.m3u8")
        self.assertEqual(playlist.kind, "media")
        self.assertEqual(playlist.media_sequence, 10)
        self.assertEqual(len(playlist.segments), 3)
        self.assertEqual(playlist.segments[0].sequence, 10)
        self.assertEqual(playlist.segments[0].duration, 5.5)
        self.assertEqual(playlist.segments[0].uri, "http://example.com/hls/seg10.ts")
        self.assertTrue(playlist.endlist is False)

    def test_aes_key_is_carried_to_each_segment(self):
        playlist = parse_m3u8(MEDIA, "http://example.com/hls/index.m3u8")
        key = playlist.segments[0].key
        self.assertIsNotNone(key)
        self.assertEqual(key.method, "AES-128")
        self.assertEqual(key.uri, "http://example.com/hls/key.bin")
        self.assertEqual(key.iv[-1], 10)

    def test_byte_range_without_offset_continues_previous(self):
        playlist = parse_m3u8(MEDIA, "http://example.com/hls/index.m3u8")
        self.assertEqual(playlist.segments[1].byte_range, (0, 75232))

    def test_live_playlist_detected(self):
        playlist = parse_m3u8(LIVE, "http://x/index.m3u8")
        self.assertTrue(playlist.is_live)

    def test_not_a_playlist(self):
        from omadown.backends.hls import HlsParseError
        with self.assertRaises(HlsParseError):
            parse_m3u8("<html>nope</html>", "http://x/")

    def test_variant_selection_respects_cap(self):
        playlist = parse_m3u8(MASTER, "http://x/master.m3u8")
        self.assertEqual(select_variant(playlist, quality="best").height, 1080)
        self.assertEqual(select_variant(playlist, quality="best", max_resolution=720).height, 720)
        self.assertEqual(select_variant(playlist, quality="720p").height, 720)
        self.assertEqual(select_variant(playlist, quality="worst").height, 720)

    def test_cap_smaller_than_every_variant_falls_back_to_smallest(self):
        """Downie: "If you limit to e.g. 480p and only larger videos are available,
        then the smallest available video will be chosen.\""""
        playlist = parse_m3u8(MASTER, "http://x/master.m3u8")
        self.assertEqual(select_variant(playlist, quality="best", max_resolution=360).height, 720)


class HlsDownloadTests(unittest.TestCase):
    """End-to-end: ffmpeg renders a real HLS ladder, we download it over HTTP."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["OMADOWN_DATA_HOME"] = str(self.tmp / "data")
        self.config = Config()
        self.config.postprocessing.enabled = False

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, url: str, workdir: Path, **option_kwargs):
        options = DownloadOptions(**option_kwargs)
        item = DownloadItem(url=url, options=options, id=1)
        ctx = DownloadContext(
            item=item, workdir=workdir, options={"cancel": CancelToken()},
            ffmpeg=support.FFMPEG, config=self.config,
        )
        backend = HlsBackend()
        info = backend.probe(url, ctx)
        path = backend.download(ctx, lambda _p: None)
        return info, path, item

    @unittest.skipUnless(support.FFMPEG, "ffmpeg required")
    def test_downloads_plain_media_playlist(self):
        stream_dir = self.tmp / "stream"
        support.make_hls(stream_dir, seconds=4)
        work = self.tmp / "work"
        with support.LocalServer(stream_dir) as server:
            info, path, _item = self._run(server.url("index.m3u8"), work)
        self.assertTrue(path.exists(), "output file should exist")
        self.assertGreater(path.stat().st_size, 1000)
        duration = support.probe_duration(path)
        self.assertGreater(duration, 2.5)
        self.assertEqual(info.extractor, "hls")

    @unittest.skipUnless(support.FFMPEG, "ffmpeg required")
    def test_master_playlist_picks_capped_variant(self):
        stream_dir = self.tmp / "ladder"
        support.make_master(stream_dir, heights=(240, 480))
        work = self.tmp / "work2"
        with support.LocalServer(stream_dir) as server:
            info, path, item = self._run(
                server.url("master.m3u8"), work, quality="best", max_resolution=240,
            )
        self.assertEqual(item.meta["hls_plan"]["variant_resolution"], "320x240")
        self.assertEqual(info.height, 240)
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 1000)

    @unittest.skipUnless(support.FFMPEG, "ffmpeg required")
    def test_download_reports_progress(self):
        stream_dir = self.tmp / "progress"
        support.make_hls(stream_dir, seconds=3)
        work = self.tmp / "work3"
        seen = []
        options = DownloadOptions()
        with support.LocalServer(stream_dir) as server:
            item = DownloadItem(url=server.url("index.m3u8"), options=options, id=3)
            ctx = DownloadContext(item=item, workdir=work,
                                  options={"cancel": CancelToken()},
                                  ffmpeg=support.FFMPEG, config=self.config)
            HlsBackend().download(ctx, seen.append)
        self.assertTrue(seen)
        self.assertTrue(any(p.message for p in seen))


if __name__ == "__main__":
    unittest.main()

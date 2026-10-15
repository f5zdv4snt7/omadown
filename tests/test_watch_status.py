import json
import os
import tempfile
import unittest
from pathlib import Path

from omadown import status
from omadown.util import (display_width, fit, pad, progress_bar, shorten, shorten_wide)
from omadown.watch import ClipboardWatcher


class ClipboardFilterTests(unittest.TestCase):
    def test_known_hosts_pass(self):
        for url in ("https://www.youtube.com/watch?v=abc",
                    "https://youtu.be/abc",
                    "https://www.bilibili.com/video/BV1xx",
                    "https://vimeo.com/12345"):
            self.assertTrue(ClipboardWatcher.looks_downloadable(url), url)

    def test_media_extensions_pass(self):
        for url in ("https://example.com/a/b.mp4",
                    "https://cdn.example.com/live/index.m3u8",
                    "https://example.com/podcast.mp3",
                    "https://example.com/movie.mkv?token=1"):
            self.assertTrue(ClipboardWatcher.looks_downloadable(url), url)

    def test_plain_pages_are_ignored(self):
        for url in ("https://example.com/",
                    "https://news.example.org/2026/10/14/story.html",
                    "https://docs.example.com/guide/getting-started"):
            self.assertFalse(ClipboardWatcher.looks_downloadable(url), url)

    def test_filter_dedupes_and_keeps_order(self):
        watcher = ClipboardWatcher(lambda _urls: None)
        urls = ["https://youtu.be/a", "https://example.com/page", "https://youtu.be/a"]
        self.assertEqual(watcher.filter(urls), ["https://youtu.be/a"])

    def test_accept_all_bypasses_filtering(self):
        watcher = ClipboardWatcher(lambda _urls: None, accept_all=True)
        self.assertEqual(watcher.filter(["https://example.com/"]), ["https://example.com/"])

    def test_repeated_payloads_are_handled_once(self):
        seen: list[list[str]] = []
        watcher = ClipboardWatcher(seen.append)
        watcher._handle("https://youtu.be/a")
        watcher._handle("https://youtu.be/a")
        watcher._handle("https://youtu.be/b")
        self.assertEqual(seen, [["https://youtu.be/a"], ["https://youtu.be/b"]])


class StatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["OMADOWN_DATA_HOME"] = str(self.tmp / "data")
        os.environ["XDG_RUNTIME_DIR"] = str(self.tmp / "run")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, payload: dict) -> None:
        from omadown.util import state_path
        state_path().write_text(json.dumps(payload), encoding="utf-8")

    def test_empty_state_is_idle(self):
        self._write({"updated": 0, "stats": {}, "items": [], "active": []})
        module = status.waybar()
        self.assertEqual(module["class"], "idle")
        self.assertEqual(module["text"], "")
        self.assertIn("空闲", module["tooltip"])

    def test_active_download_reports_percentage(self):
        self._write({
            "updated": 0,
            "stats": {"queued": 1, "active": 1, "done": 0, "failed": 0, "total": 2, "bytes": 0},
            "items": [],
            "active": [{
                "id": 1, "title": "Some Video", "state": "downloading",
                "progress": 0.42, "speed": 2 * 1024 * 1024, "eta": 65, "url": "u",
            }],
        })
        module = status.waybar()
        self.assertEqual(module["class"], "downloading")
        self.assertIn("42%", module["text"])
        self.assertEqual(module["percentage"], 42)
        self.assertIn("Some Video", module["tooltip"])
        self.assertIn("ETA 01:05", module["tooltip"])

    def test_failed_state_is_surfaced(self):
        self._write({"updated": 0,
                     "stats": {"failed": 2, "done": 0, "queued": 0, "active": 0,
                               "total": 2, "bytes": 0},
                     "items": [], "active": []})
        module = status.waybar()
        self.assertEqual(module["class"], "failed")
        self.assertIn("2", module["text"])

    def test_compact_hides_when_idle(self):
        self._write({"updated": 0, "stats": {}, "items": [], "active": []})
        self.assertEqual(status.waybar(compact=True)["text"], "")

    def test_plain_output_lists_items(self):
        self._write({
            "updated": 0,
            "stats": {"total": 1, "active": 1, "queued": 0, "done": 0, "failed": 0, "bytes": 0},
            "items": [{"id": 1, "title": "T", "state": "downloading", "progress": 0.5,
                       "speed": 1024, "eta": 30, "url": "u", "output_path": ""}],
            "active": [],
        })
        text = status.plain()
        self.assertIn("50%", text)
        self.assertIn("共 1", text)


class FormattingTests(unittest.TestCase):
    def test_progress_bar(self):
        self.assertEqual(progress_bar(0.5, width=10), "#####-----")
        self.assertEqual(progress_bar(None, width=4), "----")
        self.assertEqual(progress_bar(2.0, width=4), "####")

    def test_shorten(self):
        self.assertEqual(shorten("abcdef", 4), "a...")
        self.assertEqual(shorten("abc", 10), "abc")

    def test_display_width_counts_cjk_as_two_cells(self):
        self.assertEqual(display_width("abc"), 3)
        self.assertEqual(display_width("中文"), 4)
        self.assertEqual(display_width("a中"), 3)

    def test_fit_never_exceeds_the_column_budget(self):
        # 20 CJK glyphs would be 40 cells; .ljust(20) would therefore overflow and
        # make curses refuse to draw - the bug this helper exists to prevent.
        text = "中文测试" * 5
        clipped = fit(text, 20)
        self.assertLessEqual(display_width(clipped), 20)
        self.assertEqual(display_width(clipped), 20)

    def test_pad_produces_exact_width(self):
        self.assertEqual(display_width(pad("中文", 10)), 10)
        self.assertEqual(display_width(pad("abcdefghijklmnop", 8)), 8)

    def test_shorten_wide_respects_cells(self):
        result = shorten_wide("中文标题很长很长很长", 10)
        self.assertLessEqual(display_width(result), 10)
        self.assertTrue(result.endswith("..."))

    def test_fit_with_zero_columns(self):
        self.assertEqual(fit("abc", 0), "")
        self.assertEqual(fit("abc", -5), "")


if __name__ == "__main__":
    unittest.main()

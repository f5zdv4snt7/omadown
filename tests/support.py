"""Shared test helpers: a local HTTP server and an ffmpeg-generated HLS ladder."""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):  # silence the test output
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


class LocalServer:
    """Serve a directory over HTTP on an ephemeral port."""

    def __init__(self, directory: Path) -> None:
        handler = functools.partial(_Handler, directory=str(directory))
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "LocalServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"


def make_hls(directory: Path, *, seconds: int = 4, segment_seconds: int = 1) -> Path:
    """Render a real HLS stream with ffmpeg. Returns the index.m3u8 path."""
    if not FFMPEG:
        raise RuntimeError("ffmpeg is required for the HLS integration test")
    directory.mkdir(parents=True, exist_ok=True)
    index = directory / "index.m3u8"
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", str(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        "-f", "hls", "-hls_time", str(segment_seconds), "-hls_list_size", "0",
        "-hls_segment_filename", str(directory / "seg%03d.ts"),
        str(index),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return index


def make_master(directory: Path, *, heights: tuple[int, ...] = (240, 480)) -> Path:
    """Build a master playlist with two variants pointing at real media playlists."""
    variants = []
    for height in heights:
        sub = directory / f"v{height}"
        make_hls(sub, seconds=3)
        bandwidth = height * 1000
        variants.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION=320x{height},'
            f'CODECS="avc1.4d401f,mp4a.40.2"\n'
            f"v{height}/index.m3u8\n"
        )
    master = directory / "master.m3u8"
    master.write_text("#EXTM3U\n#EXT-X-VERSION:3\n" + "".join(variants), encoding="utf-8")
    return master


def probe_duration(path: Path) -> float:
    if not FFPROBE:
        raise RuntimeError("ffprobe is required")
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float(proc.stdout.strip())


def free_env(monkeypatch_home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["OMADOWN_DATA_HOME"] = str(monkeypatch_home / "data")
    env["OMADOWN_CONFIG_HOME"] = str(monkeypatch_home / "config")
    return env
